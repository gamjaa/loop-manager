from tests.support.catalog import plan_text
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents import document_publication
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state
import loop_manager
from loop_manager_internal.maintenance import maintenance_planning
from loop_manager_internal.maintenance import maintenance_publication
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, TrackedSource
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token


from tests.support.maintenance_planning_case import MaintenancePlanningCase, git

class MaintenanceAuthorityTest(MaintenancePlanningCase):
    def test_combined_maintenance_batch_normalizes_manifest_and_result_references(self):
        self._begin_legacy_assessment(self.base_sha)
        self.connection.execute(
            "UPDATE maintenance_cycles SET include_general_planning = 1 WHERE repo = ?",
            (str(self.repo),),
        )
        context = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha
        )
        self._write_plan("G-005", "Refactor Boundary")
        self._write_plan(
            "G-006", "General Follow-up", depends_on=("G-005",),
        )
        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000", "G-005"),
        )

        result = loop_manager.validate_maintenance_planner_result(
            self.connection, context, self.repo,
            loop_manager.changed_files(self.repo),
            refactoring_plan_ids=("G-005",), allow_general_plans=True,
        )

        self.assertEqual(
            [(path, plan["id"]) for path, plan in result.plans],
            [
                ("docs/plans/[G-002] Refactor Boundary.md", "G-002"),
                ("docs/plans/[G-003] General Follow-up.md", "G-003"),
            ],
        )
        self.assertEqual(result.plans[1][1]["depends_on"], ["G-002"])
        self.assertEqual(result.revisions[0].plan["depends_on"], ["G-000", "G-002"])
        self.assertEqual(result.refactoring_plan_ids, ("G-002",))
        self.assertEqual(result.revalidation_plan_ids, ("G-001", "G-003"))
        self.assertEqual(
            [(item.source_id, item.target_id) for item in result.id_allocations],
            [("G-005", "G-002"), ("G-006", "G-003")],
        )

        resumed = loop_manager.validate_maintenance_planner_result(
            self.connection, context, self.repo,
            loop_manager.changed_files(self.repo),
            refactoring_plan_ids=("G-002",), allow_general_plans=True,
            expected_id_allocations=result.id_allocations,
        )
        self.assertEqual(resumed.id_allocations, result.id_allocations)
        self.assertEqual(resumed.refactoring_plan_ids, ("G-002",))

        publication_effects = maintenance_publication.RecordMaintenancePublicationIntentEffects(
            _assert_maintenance_context_current=lambda *_args: None,
            _validate_maintenance_publication_commit=lambda *_args, **_kwargs: None,
            is_loop_manager_repository=lambda _path: True,
        )
        intent = maintenance_publication.record_maintenance_publication_intent(
            publication_effects, self.connection, str(self.repo), self.repo,
            "main", result, "c" * 40,
        )
        self.assertEqual(
            intent.publication_id_allocations,
            tuple(
                (item.source_path, item.source_id, item.target_path, item.target_id)
                for item in result.id_allocations
            ),
        )
        self.assertEqual(intent.publication_refactoring_plan_ids, ("G-002",))

    def test_maintenance_recovery_keeps_refactoring_ids_when_mapping_overlaps(self):
        self.connection.execute(
            "INSERT INTO execplan_cancellations "
            "(repo, plan_id, path, title, sources, depends_on, complexity, risk, "
            "priority, content_hash, plan_sha, deletion_commit) "
            "VALUES (?, 'G-010', 'docs/plans/[G-010] Cancelled.md', 'Cancelled', "
            "'[]', '[]', 'low', 'low', 'normal', 'hash', 'sha', 'commit')",
            (str(self.repo),),
        )
        context = self._begin_barrier()
        self._write_plan("G-002", "First Refactor")
        self._write_plan("G-011", "Second Refactor")
        self._write_plan(
            "G-001", "Ready Feature",
            depends_on=("G-000", "G-002", "G-011"),
        )

        initial = loop_manager.validate_maintenance_planner_result(
            self.connection, context, self.repo,
            loop_manager.changed_files(self.repo),
            refactoring_plan_ids=("G-002", "G-011"),
        )

        self.assertIsNotNone(initial)
        assert initial is not None
        self.assertEqual(
            [(item.source_id, item.target_id) for item in initial.id_allocations],
            [("G-002", "G-011"), ("G-011", "G-012")],
        )
        self.assertEqual(initial.refactoring_plan_ids, ("G-011", "G-012"))

        resumed = loop_manager.validate_maintenance_planner_result(
            self.connection, context, self.repo,
            loop_manager.changed_files(self.repo),
            refactoring_plan_ids=initial.refactoring_plan_ids,
            expected_id_allocations=initial.id_allocations,
        )

        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual(resumed.refactoring_plan_ids, ("G-011", "G-012"))
        self.assertEqual(
            [plan["id"] for _path, plan in resumed.plans], ["G-011", "G-012"],
        )

    def test_single_assessment_dag_accepts_required_ready_without_reverse_barrier(self):
        context = self._begin_barrier()
        self._write_plan("G-002", "Refactor Boundary", depends_on=("G-001",))
        result = loop_manager.validate_maintenance_planner_result(
            self.connection, context, self.repo, loop_manager.changed_files(self.repo),
            refactoring_plan_ids=("G-002",),
        )
        self.assertEqual(result.refactoring_plan_ids, ("G-002",))
        self.assertEqual(result.revisions, ())
        self.assertEqual(result.plans[0][1]["depends_on"], ["G-001"])
        self.connection.execute("UPDATE exec_plans SET branch = 'owned' WHERE plan_id = 'G-001'")
        with self.assertRaises(loop_manager.PlannerValidationError):
            loop_manager.validate_maintenance_planner_result(
                self.connection, context, self.repo, loop_manager.changed_files(self.repo),
                refactoring_plan_ids=("G-002",),
            )

    def test_maintenance_snapshot_excludes_historical_ready_identity(self):
        text = self.ready_path.read_text(encoding="utf-8")
        entry = execplan_catalog.snapshot_from_documents(
            {
                "docs/plans/archive/[G-000] Merged Foundation.md":
                    self._path("G-000", "Merged Foundation").read_text(encoding="utf-8"),
                "docs/plans/archive/[G-001] Ready Feature.md": text,
            }
        ).archived["G-001"]
        content_hash = hashlib.sha256(self.ready_path.read_bytes()).hexdigest()
        execplan_catalog_state.CatalogStateRepository(self.connection).record(
            str(self.repo),
            entry,
            original_path="docs/plans/[G-001] Ready Feature.md",
            original_content_hash=content_hash,
            plan_sha=self.base_sha,
            proof=execplan_catalog_state.MergeProof(False),
            diagnostic=execplan_catalog_state.CatalogDiagnostic(
                "G-001",
                entry.path,
                "merge-proof-missing",
                "missing",
            ),
            identity_verified=True,
        )
        self.connection.commit()

        context = self._begin_barrier()

        self.assertEqual(context.eligible_plans, ())
        self.assertEqual(
            loop_manager.compact_execplan_catalog(
                self.connection, str(self.repo)
            ),
            "[]",
        )

    def test_authority_is_sqlite_bound_and_general_planner_remains_create_only(self):
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "활성 유지보수"
        ):
            loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), self.repo, self.base_sha
            )

        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000", "G-099")
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "기존 ExecPlan을 수정"
        ):
            loop_manager.validate_planner_result(
                self.repo, self.base_sha, loop_manager.changed_files(self.repo)
            )

        git(self.repo, "restore", "docs/plans/[G-001] Ready Feature.md")
        self._begin_legacy_assessment(self.base_sha)
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 2 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        context = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha
        )
        self.assertEqual(context.eligible_plans, ())
        loop_manager._assert_maintenance_context_current(self.connection, context)
        self.assertEqual(self.connection.execute(
            "SELECT workspace_owned FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()[0], 2)

    def test_ready_snapshot_rejects_mismatched_plan_revision(self):
        original = self.ready_path.read_text(encoding="utf-8")
        self.ready_path.write_text(
            original.replace(
                "Separate the responsibility into a testable module boundary.",
                "Publish a different implementation boundary.",
            ),
            encoding="utf-8",
        )
        git(self.repo, "add", self.ready_path.relative_to(self.repo).as_posix())
        git(self.repo, "commit", "-m", "Publish a different plan revision")
        mismatched_revision = git(self.repo, "rev-parse", "HEAD")
        self.ready_path.write_text(original, encoding="utf-8")
        self.connection.execute(
            "UPDATE exec_plans SET plan_sha = ?, current_revision_sha = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (mismatched_revision, mismatched_revision, str(self.repo)),
        )
        self.connection.commit()
        self._begin_legacy_assessment(self.base_sha)

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "plan revision SHA"
        ):
            loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), self.repo, self.base_sha
            )

    def test_ready_snapshot_accepts_crlf_checkout_but_rejects_body_change(self):
        git(self.repo, "config", "core.autocrlf", "true")
        self.ready_path.write_bytes(
            self.ready_path.read_bytes().replace(b"\n", b"\r\n")
        )
        self.assertIn(b"\r\n", self.ready_path.read_bytes())

        context = self._begin_barrier()

        self.assertEqual(context.eligible_plans[0].text, self.ready_path.read_text(
            encoding="utf-8"
        ).replace("\r\n", "\n"))
        self.ready_path.write_text(
            self.ready_path.read_text(encoding="utf-8").replace(
                "Separate the responsibility into a testable module boundary.",
                "A real checkout modification must remain rejected.",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "평가 기준 revision"
        ):
            self._begin_barrier()

    def test_ready_snapshot_rejects_assume_unchanged_body_change(self):
        relative_path = self.ready_path.relative_to(self.repo).as_posix()
        git(self.repo, "update-index", "--assume-unchanged", relative_path)
        self.ready_path.write_text(
            self.ready_path.read_text(encoding="utf-8").replace(
                "Separate the responsibility into a testable module boundary.",
                "A hidden checkout modification must remain rejected.",
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "평가 기준 revision"
        ):
            self._begin_barrier()

    def test_assessment_mixed_ready_catalog_grants_only_unowned_authority(self):
        self.connection.execute(
            "UPDATE exec_plans SET status = 'READY', ever_started = 1, "
            "branch = 'existing', worktree = '/existing', worker_session_id = 'session' "
            "WHERE repo = ? AND plan_id = 'G-000'", (str(self.repo),),
        )
        context = self._begin_barrier()
        self.assertEqual(tuple(plan.plan_id for plan in context.eligible_plans), ("G-001",))
        loop_manager._assert_maintenance_context_current(self.connection, context)
        cycle = maintenance_state.MaintenanceRepository(self.connection).get(str(self.repo))
        pinned = loop_manager._maintenance_context_at_revision(
            self.connection, str(self.repo), self.repo, self.base_sha, cycle,
        )
        self.assertEqual(pinned, context)
        self.assertIsNone(self.connection.execute(
            "SELECT planning_error FROM projects WHERE repo = ?", (str(self.repo),),
        ).fetchone()[0])

    def test_revalidation_explicit_owned_target_is_not_filtered(self):
        repository = maintenance_state.MaintenanceRepository(self.connection)
        assessment, _ = self._begin_legacy_assessment(self.base_sha)
        refactoring = repository.transition(
            str(self.repo), expected_phase=assessment.phase,
            expected_revision=assessment.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-000",), revalidation_plan_ids=("G-001",),
        )
        repository.transition(
            str(self.repo), expected_phase=refactoring.phase,
            expected_revision=refactoring.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        for field, value in (("ever_started", 1), ("branch", "owned")):
            with self.subTest(field=field):
                self.connection.execute(
                    f"UPDATE exec_plans SET {field} = ? WHERE repo = ? AND plan_id = 'G-001'",
                    (value, str(self.repo)),
                )
                with self.assertRaisesRegex(loop_manager.MaintenancePlanningError, "무소유 READY"):
                    loop_manager.prepare_maintenance_planning_context(
                        self.connection, str(self.repo), self.repo, self.base_sha,
                    )

    def test_assessment_excludes_owned_ready_but_rejects_hash_drift(self):
        self._begin_legacy_assessment(self.base_sha)

        ownership_cases = (
            ("ever_started", 1),
            ("branch", "loop-manager/G-001"),
            ("branch", ""),
            ("worktree", "/tmp/managed-G-001"),
            ("workspace_owned", 2),
            ("worker_session_id", "worker-session"),
            ("reviewer_session_id", "reviewer-session"),
            ("pr_url", "https://example.test/pull/1"),
            ("pr_url", ""),
            ("pr_head_oid", "a" * 40),
            ("pr_conflict_recovery", 1),
            ("pr_conflict_head_oid", "b" * 40),
            ("pr_conflict_base_oid", "c" * 40),
        )
        for field, value in ownership_cases:
            with self.subTest(field=field):
                self.connection.execute(
                    f"UPDATE exec_plans SET {field} = ? "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (value, str(self.repo)),
                )
                self.connection.commit()
                context = loop_manager.prepare_maintenance_planning_context(
                    self.connection, str(self.repo), self.repo, self.base_sha
                )
                self.assertEqual(context.eligible_plans, ())
                loop_manager._assert_maintenance_context_current(self.connection, context)
                self.assertEqual(self.connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(self.repo),),
                ).fetchone()[field], value)
                reset = 0 if field in {
                    "ever_started", "workspace_owned", "pr_conflict_recovery"
                } else None
                self.connection.execute(
                    f"UPDATE exec_plans SET {field} = ? "
                    "WHERE repo = ? AND plan_id = 'G-001'",
                    (reset, str(self.repo)),
                )
                self.connection.commit()

        self.connection.execute(
            "UPDATE exec_plans SET content_hash = 'catalog-drift' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "content hash"
        ):
            loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), self.repo, self.base_sha
            )
        self.connection.execute(
            "UPDATE exec_plans SET content_hash = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (loop_manager.file_hash(self.ready_path), str(self.repo)),
        )
        self.connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        context = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha
        )
        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000", "G-099")
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "SQLite snapshot에 없는"
        ):
            loop_manager.validate_maintenance_planner_result(
                self.connection,
                context,
                self.repo,
                loop_manager.changed_files(self.repo),
            )

    def test_barrier_adds_every_dag_terminal_and_rejects_body_or_priority_drift(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        prompt = loop_manager.maintenance_planner_prompt(
            context, "[]", "bounded changes"
        )
        forged = replace(
            context,
            mode=loop_manager.MaintenancePlanMode.REVALIDATION,
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "SQLite 단계"
        ):
            loop_manager.validate_maintenance_planner_result(
                self.connection, forged, self.repo, loop_manager.changed_files(self.repo)
            )

        self.assertIn("maintenance barrier", prompt)
        self.assertIn("G-001", prompt)
        self.assertIn("docs/FUTURE_IDEAS.md", prompt)
        self.assertIn("사용자 편집을 대행", prompt)
        self.assertIn("Manager가 제공한 파일 또는 모듈 단위", prompt)
        self.assertIn("정적 코드맵은 의미 검토 성공을 뜻하지 않으며", prompt)
        self.assertEqual(
            [plan["id"] for _path, plan in result.plans],
            ["G-002", "G-003"],
        )
        self.assertEqual(
            result.revisions[0].plan["depends_on"],
            ["G-000", "G-003"],
        )

        self._write_plan(
            "G-001",
            "Ready Feature",
            depends_on=("G-000", "G-003"),
            purpose="Changed product goal.",
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "depends_on 이외 본문"
        ):
            self._validate_barrier(context)

        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-003",)
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "기존 dependency의 값과 순서"
        ):
            self._validate_barrier(context)
        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-003", "G-000")
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "기존 dependency의 값과 순서"
        ):
            self._validate_barrier(context)

        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000", "G-003")
        )
        self._write_plan("G-002", "Extract Boundary", priority="high")
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "상향된.*priority"
        ):
            self._validate_barrier(context)

    def test_refactoring_priority_justification_must_be_in_body(self):
        frontmatter_only_reason = plan_text(
            "G-002", "Extract Boundary", priority="high"
        ).replace(
            "docs/PRODUCT.md",
            "docs/requirements/[R-999] Security.md",
        )
        parsed = loop_manager.published_execplan(
            "docs/plans/[G-002] Extract Boundary.md",
            frontmatter_only_reason,
        )
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "상향된.*priority"
        ):
            loop_manager.validate_refactoring_plan(
                "docs/plans/[G-002] Extract Boundary.md",
                parsed,
                frontmatter_only_reason,
            )

        for negated_reason in (
            "There is no security risk and automatic execution is not blocked.",
            "security risk가 없고 automatic execution도 blocked되지 않는다.",
            "Repeated manual recovery is not required and data loss is impossible.",
            "Automatic execution remains unblocked with zero data loss.",
            "The security risk is nonexistent.",
        ):
            with self.subTest(reason=negated_reason), self.assertRaisesRegex(
                loop_manager.MaintenancePlanningError, "상향된.*priority"
            ):
                loop_manager.validate_refactoring_plan(
                    "docs/plans/[G-002] Extract Boundary.md",
                    parsed,
                    frontmatter_only_reason.replace(
                        "- Keep the change structural.",
                        f"- {negated_reason}",
                    ),
                )

        body_reason = frontmatter_only_reason.replace(
            "A concrete structural responsibility problem",
            "Repeated manual recovery reveals a structural responsibility problem",
        )
        loop_manager.validate_refactoring_plan(
            "docs/plans/[G-002] Extract Boundary.md",
            parsed,
            body_reason,
        )

    def test_priority_rejects_resolved_risks_but_accepts_current_evidence(self):
        path = "docs/plans/[G-002] Extract Boundary.md"
        for priority in ("high", "critical"):
            for reason in (
                "The security risk no longer exists.",
                "The security risk is no longer present.",
                "Data loss no longer occurs.",
                "Repeated manual recovery is no longer required.",
                "The security risk has been eliminated.",
                "The security risk has been resolved.",
                "The security risk has already been fully mitigated.",
                "We resolved the security risk.",
                "Data loss was fixed.",
                "Repeated manual recovery was eliminated.",
                "보안 위험을 제거했다.",
                "보안 위험이 해결되었다.",
                "데이터 손실 위험을 완화했다.",
            ):
                with self.subTest(priority=priority, reason=reason):
                    text = plan_text("G-002", "Extract Boundary", priority=priority)
                    text += "\n" + reason + "\n"
                    parsed = loop_manager.published_execplan(path, text)
                    with self.assertRaisesRegex(
                        loop_manager.MaintenancePlanningError, "상향된.*priority"
                    ):
                        loop_manager.validate_refactoring_plan(path, parsed, text)
                    # Historical evidence must not hide a separate current issue.
                    text += "Repeated manual recovery is required after each restart.\n"
                    loop_manager.validate_refactoring_plan(path, parsed, text)

    def test_revalidation_rejects_unsectioned_body_changes(self):
        context = self._begin_barrier()
        snapshot = context.eligible_plans[0]
        metadata = loop_manager.published_execplan(
            snapshot.path, snapshot.text
        )
        revised = snapshot.text.replace(
            "\n## Purpose",
            "\nUnsectioned product scope expansion.\n\n## Purpose",
        )
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "section 밖의 본문"
        ):
            loop_manager.validate_revalidation_revision(
                snapshot,
                snapshot.path,
                metadata,
                revised,
            )

        implementation = maintenance_planning.markdown_sections(snapshot.text).values[
            "Implementation"
        ]
        without_implementation = snapshot.text.replace(
            f"## Implementation\n\n{implementation}\n\n",
            "",
        )
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "section을 추가·삭제·재배치"
        ):
            loop_manager.validate_revalidation_revision(
                snapshot,
                snapshot.path,
                metadata,
                without_implementation,
            )

    def test_revalidation_accepts_priorityless_legacy_snapshot_revision(self):
        path = self.ready_path
        legacy_text = path.read_text(encoding="utf-8").replace(
            "priority: normal\n", "",
        )
        path.write_text(legacy_text, encoding="utf-8", newline="\n")
        relative_path = path.relative_to(self.repo).as_posix()
        git(self.repo, "add", relative_path)
        git(self.repo, "commit", "-m", "Create legacy priority-less plan snapshot")
        active_sha = git(self.repo, "rev-parse", "HEAD")
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self.connection.execute(
            "UPDATE exec_plans SET content_hash = ?, publication_content_hash = ?, "
            "publication_sha = ?, current_revision_sha = ?, plan_sha = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (content_hash, content_hash, active_sha, active_sha, active_sha, str(self.repo)),
        )
        self.connection.commit()

        cycle, _created = self._begin_legacy_assessment(active_sha)
        repository = maintenance_state.MaintenanceRepository(self.connection)
        refactoring = repository.transition(
            str(self.repo), expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-000",), revalidation_plan_ids=("G-001",),
        )
        repository.transition(
            str(self.repo), expected_phase=refactoring.phase,
            expected_revision=refactoring.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        context = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, active_sha,
        )
        self.assertNotIn("priority:", context.eligible_plans[0].text)

        revised_text = legacy_text.replace(
            "Separate the responsibility into a testable module boundary.",
            "Use the refactored adapter module boundary.",
        )
        path.write_text(revised_text, encoding="utf-8", newline="\n")
        result = loop_manager.validate_maintenance_planner_result(
            self.connection, context, self.repo,
            loop_manager.changed_files(self.repo),
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.revisions[0].plan["priority"], "normal")

    def test_revalidation_preserves_goal_and_allows_only_current_structure_sections(self):
        repository = maintenance_state.MaintenanceRepository(self.connection)
        cycle, _created = self._begin_legacy_assessment(self.base_sha)
        refactoring = repository.transition(
            str(self.repo),
            expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-000",),
            revalidation_plan_ids=("G-001",),
        )
        repository.transition(
            str(self.repo),
            expected_phase=maintenance_state.MaintenancePhase.REFACTORING,
            expected_revision=refactoring.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        context = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha
        )
        prompt = loop_manager.maintenance_planner_prompt(
            context, "[]", "bounded changes"
        )
        self.assertIn("post-refactor revalidation", prompt)
        self.assertIn("새 ExecPlan을 만들지 마라", prompt)
        original_purpose = (
            "A concrete structural responsibility problem crosses the module boundary. "
            "Preserve user behavior, the public contract, and storage format."
        )

        self._write_plan(
            "G-001",
            "Ready Feature",
            purpose="A different product goal.",
            implementation="Use the new module boundary.",
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "Purpose"
        ):
            loop_manager.validate_maintenance_planner_result(
                self.connection,
                context,
                self.repo,
                loop_manager.changed_files(self.repo),
            )

        self._write_plan(
            "G-001",
            "Ready Feature",
            purpose=original_purpose,
            acceptance_criteria=(
                "Add a billing workflow for `refactored/adapter.py`."
            ),
            implementation="Use the refactored adapter module boundary.",
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "Acceptance Criteria"
        ):
            loop_manager.validate_maintenance_planner_result(
                self.connection,
                context,
                self.repo,
                loop_manager.changed_files(self.repo),
            )

        self._write_plan(
            "G-001",
            "Ready Feature",
            purpose=original_purpose,
            acceptance_criteria=(
                "Preserve user behavior, the public API contract, and storage format "
                "for `legacy/module.py`."
            ),
            implementation="Use the refactored adapter module boundary.",
        )
        result = loop_manager.validate_maintenance_planner_result(
            self.connection,
            context,
            self.repo,
            loop_manager.changed_files(self.repo),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.plans, ())
        self.assertEqual(
            [revision.snapshot.plan_id for revision in result.revisions],
            ["G-001"],
        )
        git(self.repo, "add", "docs/plans/[G-001] Ready Feature.md")
        git(self.repo, "commit", "-m", "Publish revalidated plan")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        self.assertEqual(intent.publication_stage, "prepared")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        completed = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        repeated = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        row = self.connection.execute(
            "SELECT depends_on, plan_sha, current_revision_sha, publication_sha "
            "FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_cycle_commit, plan_commit)
        self.assertEqual(repeated, completed)
        self.assertEqual(json.loads(row["depends_on"]), [])
        self.assertEqual(row["plan_sha"], plan_commit)
        self.assertEqual(row["current_revision_sha"], plan_commit)
        self.assertEqual(row["publication_sha"], self.base_sha)

    def test_revalidation_rejects_dotted_api_and_setting_identifiers(self):
        context = self._begin_barrier()
        snapshot = context.eligible_plans[0]
        metadata = loop_manager.published_execplan(snapshot.path, snapshot.text)
        for old, new in (
            ("api.v1", "api.v2"),
            ("feature.enabled", "feature.disabled"),
        ):
            with self.subTest(identifier=old):
                original = snapshot.text.replace("legacy/module.py", old)
                revised = original.replace(old, new)
                with self.assertRaisesRegex(
                    loop_manager.MaintenancePlanningError, "Acceptance Criteria"
                ):
                    loop_manager.validate_revalidation_revision(
                        replace(snapshot, text=original),
                        snapshot.path, metadata, revised,
                    )
        loop_manager.validate_revalidation_revision(
            snapshot, snapshot.path, metadata,
            snapshot.text.replace("legacy/module.py", "refactored/adapter.py"),
            path_moves={"legacy/module.py": "refactored/adapter.py"},
        )

    def test_revalidation_uses_git_evidence_for_root_file_moves(self):
        names = ("loop_manager.py", "managed_config.py", "config.json")
        criteria = "Use " + ", ".join(f"`{name}`" for name in names)
        criteria += " while preserving `api.v1` and `feature.enabled`."
        criteria += " Keep authorization in `auth.py`."
        (self.repo / "auth.py").write_text("# authorization\n", encoding="utf-8")
        (self.repo / "billing.py").write_text("# billing\n", encoding="utf-8")
        for name in names:
            (self.repo / name).write_text(f"# original {name}\n", encoding="utf-8")
        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000",),
            acceptance_criteria=criteria,
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Record root modules before refactoring")
        old_sha = git(self.repo, "rev-parse", "HEAD")
        self.connection.execute(
            "UPDATE exec_plans SET content_hash = ?, plan_sha = ?, "
            "current_revision_sha = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (
                loop_manager.file_hash(self.ready_path),
                old_sha,
                old_sha,
                str(self.repo),
            ),
        )
        self.connection.commit()
        repository = maintenance_state.MaintenanceRepository(self.connection)
        cycle, _ = self._begin_legacy_assessment(old_sha)
        cycle = repository.transition(
            str(self.repo), expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-000",), revalidation_plan_ids=("G-001",),
        )
        repository.transition(
            str(self.repo), expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        # The old paths are absent from the current tree; evidence must be read
        # from the plan's original publication revision, not the working files.
        (self.repo / "modules").mkdir()
        destinations = ("modules/loop_manager.py", "settings.py", "settings.json")
        for old, new in zip(names, destinations):
            git(self.repo, "mv", old, new)
        git(self.repo, "commit", "-m", "Move modules and config")
        latest = git(self.repo, "rev-parse", "HEAD")
        context = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, latest
        )
        revised_criteria = criteria
        for old, new in zip(names, destinations):
            revised_criteria = revised_criteria.replace(f"`{old}`", f"`{new}`")

        def validate(criteria_text):
            self._write_plan(
                "G-001", "Ready Feature", depends_on=("G-000",),
                acceptance_criteria=criteria_text,
            )
            return loop_manager.validate_maintenance_planner_result(
                self.connection, context, self.repo,
                loop_manager.changed_files(self.repo),
            )

        result = validate(revised_criteria)
        self.assertEqual(len(result.revisions), 1)
        # Both paths exist in both revisions, but they are unrelated.
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "Acceptance Criteria"
        ):
            validate(revised_criteria.replace("auth.py", "billing.py"))
        # Verified moves are paired, not interchangeable path placeholders.
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "Acceptance Criteria"
        ):
            validate(revised_criteria.replace("settings.py", "settings.json"))
        for old, new in (
            ("api.v1", "api.v2"),
            ("feature.enabled", "feature.disabled"),
        ):
            with self.subTest(identifier=old):
                with self.assertRaisesRegex(
                    loop_manager.PlannerValidationError, "Acceptance Criteria"
                ):
                    validate(revised_criteria.replace(old, new))
        # A local untracked file cannot manufacture immutable path evidence.
        (self.repo / "invented.json").write_text("{}", encoding="utf-8")
        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000",),
            acceptance_criteria=revised_criteria.replace("settings.json", "invented.json"),
        )
        with self.assertRaisesRegex(
            loop_manager.PlannerValidationError, "Acceptance Criteria"
        ):
            loop_manager.validate_maintenance_planner_result(
                self.connection, context, self.repo,
                [self.ready_path.relative_to(self.repo).as_posix()],
            )
        self.ready_path.write_text(
            context.eligible_plans[0].text.replace(criteria, revised_criteria),
            encoding="utf-8",
        )
        real_tree_reader = loop_manager._git_tree_entries

        def unavailable_history(repo, revision):
            if revision == old_sha:
                raise loop_manager.GitLockError("historical tree unavailable")
            return real_tree_reader(repo, revision)

        with mock.patch.object(
            loop_manager, "_git_tree_entries", side_effect=unavailable_history
        ):
            with self.assertRaises(loop_manager.GitLockError):
                loop_manager.validate_maintenance_planner_result(
                    self.connection, context, self.repo,
                    [self.ready_path.relative_to(self.repo).as_posix()],
                )

    def test_git_operational_failures_do_not_become_planner_errors(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        for boundary in ("changed_files", "_git_tree_entries"):
            with self.subTest(boundary=boundary):
                failure = loop_manager.GitLockError("repository locked")
                with mock.patch.object(loop_manager, boundary, side_effect=failure):
                    with self.assertRaises(loop_manager.GitLockError) as caught:
                        self._validate_barrier(context)
                self.assertIs(caught.exception, failure)
        with self.assertRaises(RuntimeError) as caught:
            self._validate_barrier(replace(context, active_sha="0" * 40))
        self.assertNotIsInstance(caught.exception, loop_manager.PlannerValidationError)
        # A genuine catalog error still belongs to the correction boundary.
        self._write_plan("G-002", "Extract Boundary", depends_on=("G-099",))
        with self.assertRaises(loop_manager.PlannerValidationError):
            self._validate_barrier(context)

    def test_prepared_barrier_restarts_after_remote_wins_push_race(self):
        self._assert_prepared_batch_restarts()

    def test_prepared_revalidation_restarts_in_same_phase(self):
        self._assert_prepared_batch_restarts(revalidation=True)

    def test_revalidation_preflight_restarts_in_same_phase(self):
        repository = maintenance_state.MaintenanceRepository(self.connection)
        assessment, _created = self._begin_legacy_assessment(self.base_sha)
        refactoring = repository.transition(
            str(self.repo),
            expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=assessment.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-000",),
            revalidation_plan_ids=("G-001",),
        )
        before = repository.transition(
            str(self.repo),
            expected_phase=maintenance_state.MaintenancePhase.REFACTORING,
            expected_revision=refactoring.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        context = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha
        )
        git(self.repo, "commit", "--allow-empty", "-m", "Later refactoring base")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        latest = git(self.repo, "rev-parse", "HEAD")

        with self.assertRaises(loop_manager.MaintenancePlanningRestart):
            loop_manager.validate_maintenance_publication_base(
                self.connection, str(self.repo), self.repo, "main", context
            )

        restarted = repository.get(str(self.repo))
        self.assertEqual(
            restarted.phase,
            maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        self.assertEqual(restarted.revision, before.revision + 1)
        self.assertEqual(restarted.cycle_base_sha, latest)
        self.assertEqual(restarted.refactoring_plan_ids, ("G-000",))
        self.assertEqual(restarted.revalidation_plan_ids, ("G-001",))
        fresh = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, latest
        )
        self.assertEqual(fresh.mode, loop_manager.MaintenancePlanMode.REVALIDATION)
        self.assertEqual(fresh.cycle_revision, restarted.revision)

    def test_assessment_selected_plan_history_and_ownership_race_is_rejected(self):
        context = self._begin_barrier()
        self.assertEqual(tuple(plan.plan_id for plan in context.eligible_plans), ("G-001",))
        for field, value in (("ever_started", 1), ("worker_session_id", "resumed"),
                             ("current_revision_sha", "b" * 40)):
            with self.subTest(field=field):
                row = self.connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(self.repo),),
                ).fetchone()
                self.connection.execute(
                    f"UPDATE exec_plans SET {field} = ? WHERE repo = ? AND plan_id = 'G-001'",
                    (value, str(self.repo)),
                )
                with self.assertRaises(loop_manager.MaintenancePlanningError):
                    loop_manager._assert_maintenance_context_current(self.connection, context)
                self.connection.execute(
                    f"UPDATE exec_plans SET {field} = ? WHERE repo = ? AND plan_id = 'G-001'",
                    (row[field], str(self.repo)),
                )
