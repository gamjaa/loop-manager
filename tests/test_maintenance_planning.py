import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path

import loop_manager
import maintenance_state


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def plan_text(
    plan_id: str,
    title: str,
    *,
    depends_on: tuple[str, ...] = (),
    priority: str = "normal",
    purpose: str = (
        "A concrete structural responsibility problem crosses the module boundary. "
        "Preserve user behavior, the public contract, and storage format."
    ),
    acceptance_criteria: str = (
        "Preserve user behavior, the public API contract, and storage format "
        "for `legacy/module.py`."
    ),
    implementation: str = "Separate the responsibility into a testable module boundary.",
) -> str:
    dependencies = "depends_on: []"
    if depends_on:
        dependencies = "depends_on:\n" + "\n".join(
            f"  - {dependency}" for dependency in depends_on
        )
    return f"""---
id: {plan_id}
title: {title}
sources:
  - docs/PRODUCT.md
{dependencies}
complexity: low
risk: low
priority: {priority}
---

## Purpose

{purpose}

## Acceptance Criteria

{acceptance_criteria}

## Implementation

{implementation}

## Validation

- Run the targeted unit test for this responsibility boundary.
- The full verify must preserve existing behavior.

## Progress

- [ ] Pending

## Decision Log

- Keep the change structural.
"""


class MaintenancePlanningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.remote = root / "remote.git"
        self.repo = root / "repo"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.remote)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.email", "loop-manager@example.com")
        git(self.repo, "config", "user.name", "Loop Manager Test")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "docs/plans").mkdir(parents=True)
        (self.repo / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")
        for name in ("legacy/module.py", "refactored/adapter.py"):
            source = self.repo / name
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("# tracked module\n", encoding="utf-8")
        self._write_plan("G-000", "Merged Foundation")
        self.ready_path = self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000",)
        )
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "Add initial catalog")
        git(self.repo, "push", "-u", "origin", "main")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")

        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(self.repo),),
        )
        self._insert_plan("G-000", "Merged Foundation", "MERGED")
        self._insert_plan("G-001", "Ready Feature", "READY")
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _path(self, plan_id: str, title: str) -> Path:
        return self.repo / "docs/plans" / f"[{plan_id}] {title}.md"

    def _write_plan(
        self,
        plan_id: str,
        title: str,
        *,
        depends_on: tuple[str, ...] = (),
        priority: str = "normal",
        purpose: str = (
            "A concrete structural responsibility problem crosses the module boundary. "
            "Preserve user behavior, the public contract, and storage format."
        ),
        acceptance_criteria: str = (
            "Preserve user behavior, the public API contract, and storage format "
            "for `legacy/module.py`."
        ),
        implementation: str = "Separate the responsibility into a testable module boundary.",
    ) -> Path:
        path = self._path(plan_id, title)
        path.write_text(
            plan_text(
                plan_id,
                title,
                depends_on=depends_on,
                priority=priority,
                purpose=purpose,
                acceptance_criteria=acceptance_criteria,
                implementation=implementation,
            ),
            encoding="utf-8",
        )
        return path

    def _insert_plan(self, plan_id: str, title: str, status: str) -> None:
        path = self._path(plan_id, title)
        parsed = loop_manager._parse_execplan(self.repo, path)
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(self.repo),
                plan_id,
                path.relative_to(self.repo).as_posix(),
                title,
                json.dumps(parsed["sources"], separators=(",", ":")),
                json.dumps(parsed["depends_on"], separators=(",", ":")),
                parsed["priority"],
                hashlib.sha256(path.read_bytes()).hexdigest(),
                status,
                self.base_sha,
            ),
        )

    def _begin_barrier(self) -> loop_manager.MaintenancePlanningContext:
        repository = maintenance_state.MaintenanceRepository(self.connection)
        repository.begin_assessment_if_due(
            str(self.repo), 5, self.base_sha
        )
        return loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha
        )

    def _write_barrier_batch(self) -> None:
        self._write_plan("G-002", "Extract Boundary")
        self._write_plan(
            "G-003", "Separate Adapter", depends_on=("G-002",)
        )
        self._write_plan(
            "G-001", "Ready Feature", depends_on=("G-000", "G-003")
        )

    def _validate_barrier(
        self, context: loop_manager.MaintenancePlanningContext
    ) -> loop_manager.ValidatedPlanningResult:
        result = loop_manager.validate_maintenance_planner_result(
            self.connection,
            context,
            self.repo,
            loop_manager.changed_files(self.repo),
        )
        self.assertIsNotNone(result)
        assert result is not None
        return result

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
        maintenance_state.MaintenanceRepository(
            self.connection
        ).begin_assessment_if_due(str(self.repo), 5, self.base_sha)
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 2 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "무소유 READY"
        ):
            loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), self.repo, self.base_sha
            )

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
            "UPDATE exec_plans SET plan_sha = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (mismatched_revision, str(self.repo)),
        )
        self.connection.commit()
        maintenance_state.MaintenanceRepository(
            self.connection
        ).begin_assessment_if_due(str(self.repo), 5, self.base_sha)

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "plan revision SHA"
        ):
            loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), self.repo, self.base_sha
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
            loop_manager.PlannerValidationError, "기존 dependency를 제거"
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

        body_reason = frontmatter_only_reason.replace(
            "A concrete structural responsibility problem",
            "Repeated manual recovery reveals a structural responsibility problem",
        )
        loop_manager.validate_refactoring_plan(
            "docs/plans/[G-002] Extract Boundary.md",
            parsed,
            body_reason,
        )

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

    def test_revalidation_preserves_goal_and_allows_only_current_structure_sections(self):
        repository = maintenance_state.MaintenanceRepository(self.connection)
        cycle, _created = repository.begin_assessment_if_due(
            str(self.repo), 5, self.base_sha
        )
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
            "SELECT depends_on, plan_sha FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_success_commit, plan_commit)
        self.assertEqual(repeated, completed)
        self.assertEqual(json.loads(row["depends_on"]), [])
        self.assertEqual(row["plan_sha"], plan_commit)

    def test_publication_requires_latest_single_commit_and_is_atomic_and_idempotent(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        loop_manager.validate_maintenance_publication_base(
            self.repo, "main", context
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Publish maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        self.assertEqual(intent.publication_commit_sha, plan_commit)

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "fast-forward push"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )

        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 2 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "snapshot"
        ):
            loop_manager.publish_maintenance_planning_batch(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 0 "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        cycle = loop_manager.publish_maintenance_planning_batch(
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
        rows = self.connection.execute(
            "SELECT plan_id, depends_on, plan_sha FROM exec_plans "
            "WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()

        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(repeated, cycle)
        self.assertEqual(cycle.refactoring_plan_ids, ("G-002", "G-003"))
        self.assertEqual(cycle.revalidation_plan_ids, ("G-001",))
        self.assertEqual(json.loads(rows[1]["depends_on"]), ["G-000", "G-003"])
        self.assertTrue(all(row["plan_sha"] == plan_commit for row in rows[1:]))

    def test_publication_rejects_body_drift_after_result_validation(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        validated = self.ready_path.read_text(encoding="utf-8")
        self.ready_path.write_text(
            validated.replace(
                "A concrete structural responsibility problem",
                "A tampered product goal",
            ),
            encoding="utf-8",
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Tamper with validated plan body")
        plan_commit = git(self.repo, "rev-parse", "HEAD")

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "결과 검증 후 변경"
        ):
            loop_manager.record_maintenance_publication_intent(
                self.connection,
                str(self.repo),
                self.repo,
                "main",
                result,
                plan_commit,
            )
        cycle = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        self.assertIsNone(cycle.publication_stage)

    def test_pushed_batch_recovers_from_durable_intent_after_restart(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare recoverable maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        row = self.connection.execute(
            "SELECT content_hash FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()
        expected_hash = str(row["content_hash"])
        self.connection.execute(
            "UPDATE exec_plans SET content_hash = 'catalog-drift' "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "content hash"
        ):
            loop_manager.recover_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main"
            )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )

        self.connection.execute(
            "UPDATE exec_plans SET content_hash = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (expected_hash, str(self.repo)),
        )
        self.connection.commit()
        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        repeated = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        rows = self.connection.execute(
            "SELECT plan_id, depends_on FROM exec_plans "
            "WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertEqual(repeated, recovered)
        self.assertEqual(
            [row["plan_id"] for row in rows],
            ["G-000", "G-001", "G-002", "G-003"],
        )
        self.assertEqual(json.loads(rows[1]["depends_on"]), ["G-000", "G-003"])

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
            "UPDATE exec_plans SET content_hash = ?, plan_sha = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (loop_manager.file_hash(self.ready_path), old_sha, str(self.repo)),
        )
        self.connection.commit()
        repository = maintenance_state.MaintenanceRepository(self.connection)
        cycle, _ = repository.begin_assessment_if_due(str(self.repo), 5, old_sha)
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

    def _assert_prepared_batch_restarts(self, revalidation=False):
        repository = maintenance_state.MaintenanceRepository(self.connection)
        context = self._begin_barrier()
        if revalidation:
            cycle = repository.transition(
                str(self.repo),
                expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                expected_revision=context.cycle_revision,
                next_phase=maintenance_state.MaintenancePhase.REFACTORING,
                refactoring_plan_ids=("G-000",),
                revalidation_plan_ids=("G-001",),
            )
            repository.transition(
                str(self.repo),
                expected_phase=cycle.phase,
                expected_revision=cycle.revision,
                next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            )
            context = loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), self.repo, self.base_sha
            )
            self._write_plan(
                "G-001", "Ready Feature", depends_on=("G-000",),
                implementation="Use the extracted adapter module.",
            )
        else:
            self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare local maintenance batch")
        commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main", result, commit
        )
        before_rows = [tuple(row) for row in self.connection.execute(
            "SELECT * FROM exec_plans ORDER BY plan_id"
        )]
        other = Path(self.temporary.name) / "advanced-base"
        git(self.repo, "worktree", "add", "--detach", str(other), self.base_sha)
        git(other, "commit", "--allow-empty", "-m", "Concurrent base update")
        git(other, "push", "origin", "HEAD:main")
        latest = git(other, "rev-parse", "HEAD")
        rejected = loop_manager.git(
            self.repo, "push", "origin", "HEAD:main", check=False
        )
        self.assertNotEqual(rejected.returncode, 0)
        git(self.repo, "fetch", "origin")
        restarted = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        self.assertEqual(restarted.phase, intent.phase)
        self.assertEqual(restarted.revision, intent.revision + 1)
        self.assertEqual(restarted.cycle_base_sha, latest)
        self.assertIsNone(restarted.publication_stage)
        self.assertIsNone(restarted.publication_commit_sha)
        self.assertEqual(restarted.last_success_commit, intent.last_success_commit)
        self.assertEqual(
            restarted.last_success_merged_count, intent.last_success_merged_count
        )
        self.assertEqual(restarted.revalidation_plan_ids, intent.revalidation_plan_ids)
        self.assertEqual(restarted.refactoring_plan_ids, intent.refactoring_plan_ids)
        self.assertEqual(
            before_rows,
            [tuple(row) for row in self.connection.execute(
                "SELECT * FROM exec_plans ORDER BY plan_id"
            )],
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), commit)
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.restart_unpublished_batch(
                str(self.repo), expected=intent, new_base_sha=latest
            )
        with self.assertRaises(loop_manager.PlannerValidationError):
            self._validate_barrier(context)
        fresh = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), other, latest
        )
        self.assertEqual(fresh.active_sha, latest)
        # A new publication intent can be recorded in the same phase.
        (other / self.ready_path.relative_to(self.repo)).write_bytes(
            self.ready_path.read_bytes()
        )
        if not revalidation:
            for plan_id, title in (
                ("G-002", "Extract Boundary"), ("G-003", "Separate Adapter")
            ):
                source = self._path(plan_id, title)
                (other / source.relative_to(self.repo)).write_bytes(source.read_bytes())
        fresh_result = loop_manager.validate_maintenance_planner_result(
            self.connection, fresh, other, loop_manager.changed_files(other)
        )
        git(other, "add", "docs/plans")
        git(other, "commit", "-m", "Replan maintenance at latest base")
        new_intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), other, "main",
            fresh_result, git(other, "rev-parse", "HEAD"),
        )
        self.assertEqual(new_intent.publication_base_sha, latest)
        self.assertEqual(new_intent.publication_stage, "prepared")

    def test_prepared_barrier_restarts_after_remote_wins_push_race(self):
        self._assert_prepared_batch_restarts()

    def test_prepared_revalidation_restarts_in_same_phase(self):
        self._assert_prepared_batch_restarts(revalidation=True)

    def test_published_batch_recovers_after_remote_tip_advances(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main",
            result, plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        other = Path(self.temporary.name) / "published-descendant"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        git(other, "commit", "--allow-empty", "-m", "Advance after publication")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        repeated = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main"
        )
        rows = self.connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()

        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertEqual(repeated, recovered)
        self.assertEqual(recovered.publication_commit_sha, plan_commit)
        self.assertEqual(recovered.publication_remote_oid, plan_commit)
        self.assertEqual(
            [row["plan_id"] for row in rows],
            ["G-000", "G-001", "G-002", "G-003"],
        )
        self.assertNotEqual(recovered, intent)

    def test_published_batch_recovery_rejects_changed_batch_path(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection, str(self.repo), self.repo, "main",
            result, plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        other = Path(self.temporary.name) / "changed-published-batch"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        changed_plan = other / self.ready_path.relative_to(self.repo)
        changed_plan.write_text(
            changed_plan.read_text(encoding="utf-8") + "\nRemote edit.\n",
            encoding="utf-8",
        )
        git(other, "add", changed_plan.relative_to(other).as_posix())
        git(other, "commit", "-m", "Change a published batch path")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "batch 경로"
        ):
            loop_manager.recover_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main"
            )
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection).get(str(self.repo)),
            intent,
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )

    def test_publication_preflight_restarts_when_origin_base_moves(self):
        context = self._begin_barrier()
        git(self.repo, "commit", "--allow-empty", "-m", "External base move")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "최신 기준"
        ):
            loop_manager.validate_maintenance_publication_base(
                self.repo, "main", context
            )


if __name__ == "__main__":
    unittest.main()
