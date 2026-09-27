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

import document_drafts
import document_publication
import execplan_catalog
import full_assessment
import execplan_catalog_state
import loop_manager
import maintenance_planning
import maintenance_findings
import maintenance_file_inventory
import maintenance_file_ledger
import maintenance_file_publication
import maintenance_reconciliation
import maintenance_state
from maintenance_file_inventory import SourceInventory, TrackedSource
from planning_catalog import MutableReadySnapshot, mutable_snapshot_token


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


class ApprovedDocumentPlanTest(unittest.TestCase):
    def test_crlf_approved_diff_survives_planner_validation(self):
        original = b"# Product\r\nOld.\r\n"
        proposed = b"# Product\r\nNew.\r\n"
        draft = SimpleNamespace(
            draft_id="a" * 32, state="approved", target_path="docs/PRODUCT.md",
            decision_revision="b" * 32, original_revision="c" * 40,
            original_hash=hashlib.sha256(original).hexdigest(),
            proposed_hash=hashlib.sha256(proposed).hexdigest(),
            complete_diff=document_drafts._complete_diff(
                "docs/PRODUCT.md", original, proposed,
            ),
        )
        content = document_publication.planning_instruction(draft)
        for line in (
            f"DOCUMENT_DRAFT_ID: {draft.draft_id}",
            f"Target: {draft.target_path}",
            f"Decision revision: {draft.decision_revision}",
            f"Original revision: {draft.original_revision}",
            f"Original SHA256: {draft.original_hash}",
            f"Proposed SHA256: {draft.proposed_hash}",
            f"Approved diff SHA256: {hashlib.sha256(draft.complete_diff.encode()).hexdigest()}",
            "```diff",
        ):
            content = content.replace(line + "\n", line + "\r\n", 1)
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            path = "docs/plans/[G-300] Approved Product.md"
            target = worktree / path
            target.parent.mkdir(parents=True)
            target.write_bytes(content.encode("utf-8"))
            plan = {"id": "G-300", "sources": [document_publication.R027_SOURCE]}
            result = SimpleNamespace(
                plans=((path, plan),), revisions=(), deletions=(),
                publication_paths=(path,),
            )
            connection = sqlite3.connect(":memory:")
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(worktree),),
            )
            drafts = mock.Mock()
            drafts.planning_candidate.return_value = draft
            drafts.assert_current = mock.Mock()
            with (
                mock.patch.object(loop_manager, "validate_planner_result", return_value=result),
                mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=True),
                mock.patch.object(document_drafts, "DocumentDraftRepository", return_value=drafts),
            ):
                self.assertIs(
                    loop_manager._PlanningValidationPorts(connection).validate_general_result(
                        str(worktree), worktree, "d" * 40, [], None,
                    ),
                    result,
                )
            self.assertEqual(document_publication.read_plan_text(target), content)
            with self.assertRaises(document_drafts.DocumentDraftError):
                document_publication.validate_document_plan(
                    content.replace("\r\n", "\n"), plan, draft,
                )

    def test_planner_validation_audits_original_changed_after_candidate_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            connection = sqlite3.connect(':memory:')
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(worktree),),
            )
            path = 'docs/plans/[G-300] Approved Product.md'
            target = worktree / path
            target.parent.mkdir(parents=True)
            draft_id = 'a' * 32
            target.write_text('DOCUMENT_DRAFT_ID: ' + draft_id + '\n', encoding='utf-8')
            result = SimpleNamespace(
                plans=((path, {'id': 'G-300'}),), revisions=(), deletions=(),
                publication_paths=(path,),
            )
            drafts = mock.Mock()
            drafts.planning_candidate.return_value = SimpleNamespace(draft_id=draft_id)
            drafts.assert_current = mock.Mock(
                side_effect=document_drafts.DocumentDraftError('original changed'),
            )
            with (
                mock.patch.object(loop_manager, 'validate_planner_result', return_value=result),
                mock.patch.object(loop_manager, 'is_loop_manager_repository', return_value=True),
                mock.patch.object(document_drafts, 'DocumentDraftRepository', return_value=drafts),
            ):
                with self.assertRaises(loop_manager.PlannerValidationError):
                    loop_manager._PlanningValidationPorts(connection).validate_general_result(
                        str(worktree), worktree, 'b' * 40, [], None,
                    )
            drafts.record_publication_error.assert_called_once_with(
                draft_id, 'original changed',
            )

    def test_published_plan_mapping_is_atomic_and_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            connection = sqlite3.connect(":memory:")
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            maintenance_state.ensure_schema(connection)
            repo = str(worktree)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (repo,),
            )
            original, proposed = b"# Product\r\nOld.\r\n", b"# Product\r\nNew.\r\n"
            diff = document_drafts._complete_diff("docs/PRODUCT.md", original, proposed)
            draft_id = "a" * 32
            connection.execute(
                "INSERT INTO document_drafts (draft_id, repo, finding_ids, target_path, "
                "assessment_revision, original_revision, original_hash, original_content, "
                "proposed_hash, proposed_content, complete_diff, source_relationships, "
                "meaning_evidence, state, requested_decision, decision_revision) "
                "VALUES (?, ?, '[]', 'docs/PRODUCT.md', ?, ?, ?, ?, ?, ?, ?, '[]', "
                "'Only repeated wording changes.', 'approved', 'approve', ?)",
                (draft_id, repo, "b" * 40, "c" * 40,
                 hashlib.sha256(original).hexdigest(), original,
                 hashlib.sha256(proposed).hexdigest(), proposed, diff, "d" * 32),
            )
            draft = document_drafts.DocumentDraftRepository(connection).get(draft_id)
            path = "docs/plans/[G-300] Approved Product Wording.md"
            target = worktree / path
            target.parent.mkdir(parents=True)
            target.write_text(
                "## Approved Document Draft\n" + document_publication.planning_instruction(draft),
                encoding="utf-8",
            )
            plan = {"id": "G-300", "sources": [document_publication.R027_SOURCE]}
            snapshot = MutableReadySnapshot(
                repo=repo, active_sha="e" * 40, plans=(),
                token=mutable_snapshot_token(repo, "e" * 40, ()),
            )
            result = loop_manager.ValidatedPlanningResult(
                plans=((path, plan),), publication_paths=(path,),
                mutable_snapshot=snapshot,
            )
            intent = SimpleNamespace(
                stage="prepared", base_sha=snapshot.active_sha,
                commit_sha="f" * 40, snapshot_token=snapshot.token,
                specification_fingerprint="spec", planning_fingerprint="plan",
            )
            with (
                mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=True),
                mock.patch.object(loop_manager, "general_planning_publication", return_value=intent),
                mock.patch.object(loop_manager, "_assert_mutable_ready_snapshot_current"),
                mock.patch.object(loop_manager, "_register_execplan_rows"),
                mock.patch.object(loop_manager, "_clear_active_in_current_transaction"),
            ):
                for _ in range(2):
                    loop_manager._apply_general_planning_batch(
                        connection, repo, worktree, result, "f" * 40, "spec", "plan",
                    )
                plan["id"] = "G-301"
                with self.assertRaises(loop_manager.PlannerValidationError):
                    loop_manager._apply_general_planning_batch(
                        connection, repo, worktree, result, "f" * 40, "spec", "plan",
                    )
            self.assertEqual(
                document_drafts.DocumentDraftRepository(connection).get(draft_id).plan_id,
                "G-300",
            )

    def test_plan_claim_binds_one_approved_diff_and_r027_source(self):
        original = b"# Product\n\nRepeated rule.\n"
        proposed = b"# Product\n\nRule.\n"
        draft = SimpleNamespace(
            draft_id="a" * 32, state="approved", target_path="docs/PRODUCT.md",
            decision_revision="b" * 32, original_revision="c" * 40,
            original_hash=hashlib.sha256(original).hexdigest(),
            proposed_hash=hashlib.sha256(proposed).hexdigest(),
            complete_diff=document_drafts._complete_diff(
                "docs/PRODUCT.md", original, proposed,
            ),
        )
        text = "## Approved Document Draft\n" + document_publication.planning_instruction(draft)
        plan = {"sources": [document_publication.R027_SOURCE]}
        document_publication.validate_document_plan(text, plan, draft)
        self.assertEqual(document_publication.plan_claim(text), draft.draft_id)
        with self.assertRaises(document_drafts.DocumentDraftError):
            document_publication.validate_document_plan(
                text.replace("-Repeated rule.", "-Different rule."), plan, draft,
            )
        with self.assertRaises(document_drafts.DocumentDraftError):
            document_publication.validate_document_plan(text, {"sources": ["docs/PRODUCT.md"]}, draft)
        self.assertTrue(document_publication.review_confirms_publication(
            "DOCUMENT_MEANING: PASS\nDOCUMENT_DIFF: PASS\nVERDICT: PASS",
        ))
        self.assertTrue(document_publication.review_confirms_publication(
            "DOCUMENT_MEANING: PASS\r\nDOCUMENT_DIFF: PASS\r\nVERDICT: PASS",
        ))
        self.assertFalse(document_publication.review_confirms_publication(
            "DOCUMENT_MEANING: PASS\nDOCUMENT_MEANING: UNCERTAIN\n"
            "DOCUMENT_DIFF: PASS\nVERDICT: PASS",
        ))


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


class FullAssessmentEvidenceTest(unittest.TestCase):
    def test_malformed_finding_types_raise_validation_error(self):
        revision = "a" * 40
        inventory = {"module.py": "b" * 64}
        finding = {
            "id": "agent-scope", "kind": "code", "path": "module.py",
            "evidence": "G-051 already separated Agent scope responsibilities.",
            "status": "resolved", "plan_ids": [], "source_hash": "b" * 64,
        }
        report = {
            "source_revision": revision,
            "coverage": {key: "Reviewed responsibility boundaries."
                         for key in ("code", "tests", "documents")},
            "findings": [finding],
        }
        for key, value in (("kind", ["code"]), ("status", ["resolved"]),
                           ("plan_ids", [["G-051"]]),
                           ("plan_ids", ["G-051", ["G-051"]])):
            with self.subTest(key=key, value=value):
                malformed = dict(finding, **{key: value})
                report["findings"] = [malformed]
                with self.assertRaises(full_assessment.FullAssessmentError):
                    full_assessment.parse_assessment_result(
                        full_assessment.RESULT_PREFIX + json.dumps(report),
                        inventory, revision, (), set(),
                    )

    def test_inventory_and_findings_bind_source_identity_and_plan_links(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "--initial-branch=main")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "Test")
            for path in ("docs/PRODUCT.md", "docs/ARCHITECTURE.md",
                         "docs/requirements/current.md", "docs/requirements/archive/old.md",
                         "module.py", "tests/test_module.py", "node_modules/vendor.py"):
                target = repo / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(path, encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-m", "Fixture")
            sha = git(repo, "rev-parse", "HEAD")
            inventory = full_assessment.source_inventory(repo, sha)
            self.assertIn("tests/test_module.py", inventory)
            self.assertNotIn("docs/requirements/archive/old.md", inventory)
            self.assertNotIn("node_modules/vendor.py", inventory)
            prompt = full_assessment.assessment_prompt(inventory, sha, None)
            self.assertIn(inventory["module.py"], prompt)
            self.assertNotIn("docs/requirements/archive/old.md", prompt)
            self.assertIn("saved evidence", full_assessment.assessment_prompt(
                inventory, sha, "saved evidence"))
            report = {
                "source_revision": sha,
                "coverage": {key: "Reviewed responsibilities and evidence."
                             for key in ("code", "tests", "documents")},
                "findings": [{"id": "code-one", "kind": "code",
                              "path": "module.py", "source_hash": inventory["module.py"],
                              "evidence": "Repeated adapter responsibility in module.py.",
                              "status": "planned", "plan_ids": ["G-100"]},
                             {"id": "agent-scope", "kind": "code",
                              "path": "module.py", "source_hash": inventory["module.py"],
                              "evidence": "G-051 already separated Agent scope responsibilities.",
                              "status": "resolved", "plan_ids": []},
                             {"id": "doc-one", "kind": "document",
                              "path": "docs/PRODUCT.md",
                              "source_hash": inventory["docs/PRODUCT.md"],
                              "evidence": "A decision and its exception are repeated here.",
                              "status": "candidate", "plan_ids": []}],
            }
            output = full_assessment.RESULT_PREFIX + json.dumps(report)
            saved = full_assessment.parse_assessment_result(
                output, inventory, sha, ("G-100",), set())
            self.assertEqual(json.loads(saved), report)
            report["findings"][1]["source_hash"] = "0" * 64
            with self.assertRaises(full_assessment.FullAssessmentError):
                full_assessment.parse_assessment_result(
                    full_assessment.RESULT_PREFIX + json.dumps(report),
                    inventory, sha, ("G-100",), set())


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
        relative_path = path.relative_to(self.repo).as_posix()
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, current_complexity, current_risk,
                current_revision_sha, publication_path,
                publication_content_hash, publication_sha, status, plan_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(self.repo),
                plan_id,
                relative_path,
                title,
                json.dumps(parsed["sources"], separators=(",", ":")),
                json.dumps(parsed["depends_on"], separators=(",", ":")),
                parsed["priority"],
                content_hash,
                parsed["complexity"],
                parsed["risk"],
                self.base_sha,
                relative_path,
                content_hash,
                self.base_sha,
                status,
                self.base_sha,
            ),
        )

    def _begin_barrier(self) -> loop_manager.MaintenancePlanningContext:
        self._begin_legacy_assessment(self.base_sha)
        return loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, self.base_sha
        )

    def _begin_legacy_assessment(self, source_sha: str):
        """Model a cycle already active before file schedules were installed."""
        maintenance_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, phase, cycle_base_sha, cycle_start_merged_count, revision) "
            "VALUES (?, 'assessment', ?, 1, 1) "
            "ON CONFLICT(repo) DO UPDATE SET phase = 'assessment', "
            "cycle_base_sha = excluded.cycle_base_sha, "
            "cycle_start_merged_count = excluded.cycle_start_merged_count, "
            "revision = maintenance_cycles.revision + 1",
            (str(self.repo), source_sha),
        )
        return maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        ), True

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

    def test_ready_snapshot_rejects_all_ownership_nonready_and_hash_drift(self):
        self._begin_legacy_assessment(self.base_sha)

        ownership_cases = (
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
                with self.assertRaisesRegex(
                    loop_manager.MaintenancePlanningError, "무소유 READY"
                ):
                    loop_manager.prepare_maintenance_planning_context(
                        self.connection, str(self.repo), self.repo, self.base_sha
                    )
                reset = 0 if field in {
                    "workspace_owned", "pr_conflict_recovery"
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
        self.assertIn("원본을 실제로 읽는 파일별 단위", prompt)
        self.assertIn("성공 ledger가 아니다", prompt)
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
        self.assertEqual(completed.last_success_commit, plan_commit)
        self.assertEqual(repeated, completed)
        self.assertEqual(json.loads(row["depends_on"]), [])
        self.assertEqual(row["plan_sha"], plan_commit)
        self.assertEqual(row["current_revision_sha"], plan_commit)
        self.assertEqual(row["publication_sha"], self.base_sha)

    def test_publication_requires_latest_single_commit_and_is_atomic_and_idempotent(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        loop_manager.validate_maintenance_publication_base(
            self.connection, str(self.repo), self.repo, "main", context
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

        self.connection.execute(
            "UPDATE exec_plans SET pr_head_oid = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("b" * 40, str(self.repo)),
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
            "UPDATE exec_plans SET pr_head_oid = NULL "
            "WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        )
        self.connection.commit()

        self.connection.execute(
            "UPDATE exec_plans SET pr_conflict_recovery = 1, "
            "pr_conflict_head_oid = ?, pr_conflict_base_oid = ? "
            "WHERE repo = ? AND plan_id = 'G-001'",
            ("c" * 40, "d" * 40, str(self.repo)),
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
            "UPDATE exec_plans SET pr_conflict_recovery = 0, "
            "pr_conflict_head_oid = NULL, pr_conflict_base_oid = NULL "
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
            "SELECT plan_id, depends_on, plan_sha, current_revision_sha, "
            "publication_sha FROM exec_plans "
            "WHERE repo = ? ORDER BY plan_id",
            (str(self.repo),),
        ).fetchall()

        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(repeated, cycle)
        self.assertEqual(cycle.refactoring_plan_ids, ("G-002", "G-003"))
        self.assertEqual(cycle.revalidation_plan_ids, ("G-001",))
        self.assertEqual(json.loads(rows[1]["depends_on"]), ["G-000", "G-003"])
        self.assertTrue(all(row["plan_sha"] == plan_commit for row in rows[1:]))
        self.assertTrue(
            all(row["current_revision_sha"] == plan_commit for row in rows[1:])
        )
        self.assertEqual(rows[1]["publication_sha"], self.base_sha)
        self.assertTrue(
            all(row["publication_sha"] == plan_commit for row in rows[2:])
        )

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

    def test_publication_rolls_back_new_and_revised_rows_if_cycle_update_fails(self):
        context = self._begin_barrier()
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Prepare atomic maintenance batch")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        intent = loop_manager.record_maintenance_publication_intent(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        before = tuple(
            self.connection.execute(
                "SELECT depends_on, content_hash, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(self.repo),),
            ).fetchone()
        )

        with mock.patch.object(
            maintenance_state.MaintenanceRepository,
            "transition",
            side_effect=maintenance_state.MaintenanceStateError(
                "injected cycle update failure"
            ),
        ):
            with self.assertRaisesRegex(
                maintenance_state.MaintenanceStateError, "injected"
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
                "SELECT 1 FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-002'",
                (str(self.repo),),
            ).fetchone()
        )
        after = tuple(
            self.connection.execute(
                "SELECT depends_on, content_hash, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(self.repo),),
            ).fetchone()
        )
        self.assertEqual(after, before)
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection).get(
                str(self.repo)
            ),
            intent,
        )

        completed = loop_manager.publish_maintenance_planning_batch(
            self.connection,
            str(self.repo),
            self.repo,
            "main",
            result,
            plan_commit,
        )
        self.assertEqual(
            completed.phase, maintenance_state.MaintenancePhase.REFACTORING
        )

    def test_refactoring_publication_promotes_file_evidence_once_after_remote_push(self):
        context = self._begin_barrier()
        self.connection.execute(
            "UPDATE maintenance_cycles SET file_assessment_start_merged_count = 1 "
            "WHERE repo = ?", (str(self.repo),),
        )
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, 'catalog')",
            (str(self.repo), self.base_sha),
        )
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        inventory = maintenance_file_inventory.tracked_source_inventory(
            self.repo, "main", self.base_sha,
        )
        reviewed = tuple(maintenance_file_ledger.ReviewedSource(
            source.path, source.content_hash, source.mode, source.revision,
            "Reviewed implementation, dependencies, and related tests.",
        ) for source in inventory.sources)
        maps = tuple((source.path, json.dumps({
            "responsibility": "Review the module responsibility",
            "entry_points": [], "public_symbols": [],
            "direct_references": [], "related_tests": [],
        })) for source in inventory.sources)
        evidence = maintenance_file_publication.AssessmentEvidence(
            inventory, reviewed, maps, 1,
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Publish refactoring plans")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        with mock.patch.object(
            maintenance_file_publication, "collect", return_value=evidence,
        ):
            loop_manager.record_maintenance_publication_intent(
                self.connection, str(self.repo), self.repo, "main", result,
                plan_commit,
            )
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)))
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        with mock.patch.object(
            maintenance_state.MaintenanceRepository, "transition",
            side_effect=maintenance_state.MaintenanceStateError("injected"),
        ):
            with self.assertRaisesRegex(maintenance_state.MaintenanceStateError, "injected"):
                loop_manager.publish_maintenance_planning_batch(
                    self.connection, str(self.repo), self.repo, "main", result,
                    plan_commit,
                )
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)))
        self.assertIsNone(maintenance_state.MaintenanceRepository(
            self.connection,
        ).get(str(self.repo)).last_success_file_assessment_merged_count)
        published = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(published.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(published.last_success_file_assessment_merged_count, 1)
        self.assertEqual(len(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).reviews(str(self.repo))), len(inventory.sources))
        repeated = loop_manager.publish_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main", result,
            plan_commit,
        )
        self.assertEqual(repeated, published)

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
            repository.restart_prepared_batch(
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

    def _prepare_pushed_file_assessment_batch(self):
        context = self._begin_barrier()
        self.connection.execute(
            "UPDATE maintenance_cycles SET file_assessment_start_merged_count = 1 "
            "WHERE repo = ?", (str(self.repo),),
        )
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, 'catalog')",
            (str(self.repo), self.base_sha),
        )
        self._write_barrier_batch()
        result = self._validate_barrier(context)
        inventory = maintenance_file_inventory.tracked_source_inventory(
            self.repo, "main", self.base_sha,
        )
        reviewed = tuple(maintenance_file_ledger.ReviewedSource(
            source.path, source.content_hash, source.mode, source.revision,
            "Reviewed implementation, dependencies, and related tests.",
        ) for source in inventory.sources)
        maps = tuple((source.path, json.dumps({
            "responsibility": "Review the module responsibility",
            "entry_points": [], "public_symbols": [],
            "direct_references": [], "related_tests": [],
        })) for source in inventory.sources)
        evidence = maintenance_file_publication.AssessmentEvidence(
            inventory, reviewed, maps, 1,
        )
        git(self.repo, "add", "docs/plans")
        git(self.repo, "commit", "-m", "Publish refactoring plans")
        plan_commit = git(self.repo, "rev-parse", "HEAD")
        with mock.patch.object(
            maintenance_file_publication, "collect", return_value=evidence,
        ):
            intent = loop_manager.record_maintenance_publication_intent(
                self.connection, str(self.repo), self.repo, "main", result,
                plan_commit,
            )
        git(self.repo, "push", "origin", "HEAD:main")
        return plan_commit, intent, inventory

    def test_published_batch_unchanged_sources_promote_saved_evidence(self):
        plan_commit, _intent, inventory = self._prepare_pushed_file_assessment_batch()
        other = Path(self.temporary.name) / "unchanged-source-descendant"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        git(other, "commit", "--allow-empty", "-m", "Advance after publication")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        recovered = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(recovered.phase, maintenance_state.MaintenancePhase.REFACTORING)
        self.assertEqual(recovered.last_success_file_assessment_merged_count, 1)
        self.assertEqual(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)).source_revision, self.base_sha)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM successful_code_maps WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()[0], len(inventory.sources))

    def test_published_batch_source_drift_restarts_file_assessment(self):
        plan_commit, intent, _inventory = self._prepare_pushed_file_assessment_batch()
        other = Path(self.temporary.name) / "source-drift-descendant"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        source = other / "legacy/module.py"
        source.write_text("# changed after plan publication\n", encoding="utf-8")
        git(other, "add", "legacy/module.py")
        git(other, "commit", "-m", "Change reviewed source")
        git(other, "push", "origin", "HEAD:main")
        latest = git(other, "rev-parse", "HEAD")
        git(self.repo, "fetch", "origin")
        self.connection.execute(
            "UPDATE projects SET phase = 'PLANNING', active_sha = ?, worktree = ? "
            "WHERE repo = ?", (self.base_sha, str(self.repo), str(self.repo)),
        )

        with mock.patch(
            "loop_manager._clear_active_in_current_transaction",
            side_effect=RuntimeError("injected project cleanup failure"),
        ), self.assertRaisesRegex(RuntimeError, "injected project cleanup failure"):
            loop_manager.recover_maintenance_planning_batch(
                self.connection, str(self.repo), self.repo, "main",
            )
        self.assertEqual(
            maintenance_state.MaintenanceRepository(self.connection).get(str(self.repo)),
            intent,
        )

        restarted = loop_manager.recover_maintenance_planning_batch(
            self.connection, str(self.repo), self.repo, "main",
        )
        self.assertEqual(restarted.phase, maintenance_state.MaintenancePhase.ASSESSMENT)
        self.assertEqual(restarted.revision, intent.revision + 1)
        self.assertEqual(restarted.cycle_base_sha, latest)
        self.assertIsNone(restarted.publication_stage)
        self.assertIsNone(restarted.last_success_file_assessment_merged_count)
        self.assertIsNone(maintenance_file_ledger.FileReviewRepository(
            self.connection,
        ).last_success(str(self.repo)))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM file_review_ledger WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()[0], 0)
        maintenance_reconciliation.ensure_schema(self.connection)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM successful_code_maps WHERE repo = ?",
            (str(self.repo),),
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (str(self.repo),),
        ).fetchone()[0], None)
        self.assertTrue((other / "docs/plans/[G-002] Extract Boundary.md").is_file())
        loop_manager.reconcile_execplan_catalog(
            self.connection, self.repo, str(self.repo), "main", remote_sha=latest,
        )
        self.assertEqual({row[0] for row in self.connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo = ?", (str(self.repo),),
        )}, {"G-000", "G-001", "G-002", "G-003"})
        self.assertEqual(
            loop_manager.prepare_maintenance_planning_context(
                self.connection, str(self.repo), other, latest,
            ).active_sha,
            latest,
        )

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

    def _assert_published_batch_recovery_rejects_catalog_drift(
        self, drift: str
    ) -> None:
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
        other = Path(self.temporary.name) / f"catalog-{drift}"
        git(self.repo, "worktree", "add", "--detach", str(other), plan_commit)
        if drift == "addition":
            changed_plan = (
                other / "docs/plans/[G-004] Out Of Batch Plan.md"
            )
            changed_plan.write_text(
                plan_text("G-004", "Out Of Batch Plan"), encoding="utf-8"
            )
        elif drift == "modification":
            changed_plan = other / self._path(
                "G-000", "Merged Foundation"
            ).relative_to(self.repo)
            changed_plan.write_text(
                changed_plan.read_text(encoding="utf-8") + "\nOut-of-batch edit.\n",
                encoding="utf-8",
            )
        else:
            self.fail(f"unknown catalog drift fixture: {drift}")
        git(other, "add", changed_plan.relative_to(other).as_posix())
        git(other, "commit", "-m", f"Catalog {drift} after publication")
        git(other, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")

        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "ExecPlan catalog"
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

    def test_published_batch_recovery_rejects_added_catalog_plan(self):
        self._assert_published_batch_recovery_rejects_catalog_drift("addition")

    def test_published_batch_recovery_rejects_modified_catalog_plan(self):
        self._assert_published_batch_recovery_rejects_catalog_drift("modification")

    def test_publication_preflight_restarts_when_origin_base_moves(self):
        context = self._begin_barrier()
        before = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        git(self.repo, "commit", "--allow-empty", "-m", "External base move")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "fetch", "origin")
        latest = git(self.repo, "rev-parse", "HEAD")

        with self.assertRaises(loop_manager.MaintenancePlanningRestart) as caught:
            loop_manager.validate_maintenance_publication_base(
                self.connection, str(self.repo), self.repo, "main", context
            )
        restarted = maintenance_state.MaintenanceRepository(self.connection).get(
            str(self.repo)
        )
        self.assertEqual(caught.exception.new_base_sha, latest)
        self.assertEqual(caught.exception.cycle_revision, restarted.revision)
        self.assertEqual(restarted.phase, before.phase)
        self.assertEqual(restarted.revision, before.revision + 1)
        self.assertEqual(restarted.cycle_base_sha, latest)
        self.assertEqual(restarted.refactoring_plan_ids, before.refactoring_plan_ids)
        self.assertEqual(restarted.revalidation_plan_ids, before.revalidation_plan_ids)
        self.assertIsNone(restarted.publication_stage)
        with self.assertRaisesRegex(
            loop_manager.MaintenancePlanningError, "단계 또는 revision"
        ):
            loop_manager.validate_maintenance_publication_base(
                self.connection, str(self.repo), self.repo, "main", context
            )
        fresh = loop_manager.prepare_maintenance_planning_context(
            self.connection, str(self.repo), self.repo, latest
        )
        self.assertEqual(fresh.active_sha, latest)
        self.assertEqual(fresh.cycle_revision, restarted.revision)
        loop_manager.validate_maintenance_publication_base(
            self.connection, str(self.repo), self.repo, "main", fresh
        )

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


class ReconciledPlanLinkTest(unittest.TestCase):
    def test_findings_port_persists_manifest_and_rejects_changed_catalog(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        port = loop_manager._PlanningReconciliationPorts(connection)
        progress = maintenance_reconciliation.ReconciliationProgress((), (), (), None)
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.object(port, "progress", return_value=progress),
              mock.patch.object(port, "_plan_texts", return_value={}),
              mock.patch.object(loop_manager.maintenance_file_inventory, "cached_source_inventory",
                                return_value=SourceInventory("a" * 40, (), ()))):
            args = ("repo", Path("repo"), "main", 1, "a" * 40)
            unit, manifest = port.findings_unit(*args, {}, Path(directory))
            self.assertTrue(manifest.exists())
            port.findings_session("repo", unit, "resume")
            port.findings_fail("repo", unit, "retry bounded unit")
            resumed, _ = port.findings_unit(*args, {}, Path(directory))
            self.assertEqual(resumed.session_id, "resume")
            self.assertEqual(resumed.error, "retry bounded unit")
            output = maintenance_findings.UNIT_PREFIX + json.dumps({
                "manifest_id": unit.manifest_id, "index": 0, "parts": [], "complete": True,
                "counts": {"finding_count": 0, "comparison_count": 0},
                "summary": "No selected structural evidence requires a new refactoring plan."})
            port.findings_complete(*args, {}, resumed, output)
            sealed, _ = port.findings_unit(*args, {}, Path(directory))
            self.assertEqual(sealed.receipt["unit_count"], 1)
            final = maintenance_findings.RESULT_PREFIX + json.dumps(sealed.receipt)
            self.assertEqual(port.validate(*args, final, {}, ())["findings"], [])
            with mock.patch.object(port, "_plan_texts", return_value={"G-100": "Changed scope"}):
                with self.assertRaises(maintenance_reconciliation.ReconciliationError):
                    port.findings_complete(*args, {"G-100": "READY"}, resumed, output)

    def test_compact_no_issue_waits_for_every_durable_resolution(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES ('repo')")
        port = loop_manager._PlanningReconciliationPorts(connection)
        progress = maintenance_reconciliation.ReconciliationProgress((), (), (), None,
            file_reviews=({"path": "src/a.py", "evidence": "This reviewed module has one cohesive responsibility."},))
        page = port.synthesis("repo", 1, "a" * 40, progress)
        port.synthesis_session("repo", page, "review-session")
        port.synthesis_complete("repo", page, maintenance_reconciliation.SYNTHESIS_PREFIX + json.dumps({
            "key": page.key, "excerpt": "This reviewed module has one cohesive responsibility.",
            "has_issue": False, "assessment": "This independently reviewed module needs no structural change.",
        }))
        collection = port._findings.prepare("repo", 1, "a" * 40, page.digest, {}, {})
        port._findings.complete("repo", collection, maintenance_findings.UNIT_PREFIX + json.dumps({
            "manifest_id": collection.manifest_id, "index": 0, "parts": [], "complete": True,
            "counts": {"finding_count": 0, "comparison_count": 0},
            "summary": "Reviewed responsibilities do not need structural changes."}), {}, lambda value: None)
        collection = port._findings.prepare("repo", 1, "a" * 40, page.digest, {}, {})
        output = maintenance_findings.RESULT_PREFIX + json.dumps(collection.receipt)
        with (tempfile.TemporaryDirectory() as directory,
              mock.patch.object(port, "progress", return_value=progress),
              mock.patch.object(loop_manager.maintenance_file_inventory, "cached_source_inventory",
                                return_value=SourceInventory("a" * 40, (), ())),
              mock.patch.object(loop_manager, "_git_tree_entries", return_value={})):
            args = ("repo", Path("repo"), "main", 1, "a" * 40, output, {}, ())
            with self.assertRaisesRegex(maintenance_reconciliation.ReconciliationError, "저장된 판단 처리"):
                port.validate(*args)
            unit = port.validate(*args, resolution_directory=Path(directory))
            self.assertIsInstance(unit, maintenance_reconciliation.ResolutionUnit)
            port.resolution_complete("repo", unit, maintenance_reconciliation.RESOLUTION_PREFIX + json.dumps({
                "key": unit.assessment["key"], "excerpt": unit.assessment["assessment"],
                "issue_keys": [], "disposition": "no_issue",
                "reason": "The reviewed responsibility has no duplicate or mixed integration boundary."}))
            self.assertEqual(port.validate(*args)["findings"], [])
            bad_args = (*args[:5], output.replace(collection.receipt["digest"], "stale-digest"), *args[6:])
            with self.assertRaisesRegex(maintenance_reconciliation.ReconciliationError, "manifest"):
                port.validate(*bad_args)

    def test_unconsumed_evidence_pages_block_final_validation(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO projects VALUES ('repo')")
        port = loop_manager._PlanningReconciliationPorts(connection)
        progress = maintenance_reconciliation.ReconciliationProgress((), (), (), None,
            file_reviews=({"path": "src/a.py", "evidence": "A single file issue must be reviewed."},))
        with (mock.patch.object(port, "progress", return_value=progress),
              mock.patch.object(maintenance_reconciliation, "parse_findings") as parse):
            with self.assertRaisesRegex(maintenance_reconciliation.ReconciliationError, "페이지 검토"):
                port.validate("repo", Path("repo"), "main", 1, "a" * 40, "output", {}, ())
        parse.assert_not_called()

    def test_validation_reads_plan_scope_from_pinned_active_tree(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        port = loop_manager._PlanningReconciliationPorts(connection)
        progress = maintenance_reconciliation.ReconciliationProgress((), (), (), None)
        inventory = SourceInventory("a" * 40, (), ())
        text = plan_text("G-100", "Extract Boundary")
        with (mock.patch.object(port, "progress", return_value=progress),
              mock.patch.object(loop_manager.maintenance_file_inventory, "cached_source_inventory", return_value=inventory),
              mock.patch.object(loop_manager, "_git_tree_entries", return_value={
                  b"docs/plans/[G-100] Extract Boundary.md": (b"blob", b"oid"),
                  b"docs/plans/archive/[G-099] Old.md": (b"blob", b"old"),
                  b"src/invalid-\xff.py": (b"blob", b"source"),
              }),
              mock.patch.object(loop_manager, "git_bytes", return_value=text.encode()) as read,
              mock.patch.object(port._findings, "resolve", return_value={}),
              mock.patch.object(maintenance_reconciliation, "parse_findings", return_value={}) as parse):
            port.validate("repo", Path("repo"), "main", 1, inventory.revision,
                          "output", {"G-100": "READY"}, ())
        read.assert_called_once_with(Path("repo"), "show",
                                     f"{inventory.revision}:docs/plans/[G-100] Extract Boundary.md")
        self.assertEqual(parse.call_args.args[-1], {"G-100": text})

    def test_refactoring_plan_preserves_all_three_contracts(self):
        path = "docs/plans/[G-100] Extract Boundary.md"
        text = plan_text("G-100", "Extract Boundary")
        plan = {"priority": "normal"}
        maintenance_planning.validate_refactoring_plan(path, plan, text)
        changed = text.replace("the public contract, and ", "").replace(
            "the public API contract, and ", ""
        )
        with self.assertRaisesRegex(maintenance_planning.MaintenancePlanningError,
                                    "공개 계약"):
            maintenance_planning.validate_refactoring_plan(path, plan, changed)

    def test_findings_link_existing_plan_without_duplicate_and_allow_grounded_no_issue(self):
        content = b"def calculate(): return 1\n"
        source = TrackedSource("src/calc.py", hashlib.sha256(content).hexdigest(),
                               "100644", "a" * 40, "source")
        inventory = SourceInventory(source.revision, (source,), ())
        progress = maintenance_reconciliation.ReconciliationProgress(
            (source,), (), (), None, file_reviews=({"path": source.path, "evidence": {
                "responsibility_and_integration": "Calculation has one pure responsibility and no external integration."}},))
        finding = {
            "issue_key": "mixed-calculation", "targets": [source.path],
            "evidence": [{"path": source.path, "content_hash": source.content_hash,
                          "mode": source.mode, "excerpt": "def calculate()"}],
            "relation_keys": [],
            "boundary": "Move calculation from the interface module into its own unit.",
            "context_reduction": "Later interface changes avoid unrelated calculation details.",
            "integration_cost": "One import and a focused unit test are added for the boundary.",
            "status": "existing_plan", "plan_id": "G-100",
            "plan_comparisons": [{"plan_id": "G-100",
                "plan_excerpt": "Extract calculation from the interface module.",
                "finding_excerpt": "Move calculation from the interface module into its own unit.",
                "same_issue": True, "reason": "Both isolate calculation from the same interface responsibility."}],
        }
        payload = {"source_revision": source.revision, "findings": [finding],
                   "no_issue_reason": None}
        parse = lambda value, existing, new: maintenance_reconciliation.parse_findings(
            maintenance_reconciliation.FINDINGS_PREFIX + json.dumps(value), progress,
            inventory, {source.path: content}, existing, new,
            {"G-100": "Extract calculation from the interface module."},
        )
        self.assertEqual(parse(payload, {"G-100": "READY"}, ()), payload)
        with self.assertRaisesRegex(maintenance_reconciliation.ReconciliationError, "계획 연결"):
            parse(payload, {}, ())
        with self.assertRaisesRegex(maintenance_reconciliation.ReconciliationError, "새 리팩터링"):
            parse(payload, {"G-100": "READY"}, ("G-101",))
        no_issue = {"source_revision": source.revision, "findings": [],
                    "no_issue_reason": {"files": [{"id": source.path,
                        "excerpt": "Calculation has one pure responsibility",
                        "reason": "The reviewed function has no mixed integration responsibility."}], "relations": []}}
        self.assertEqual(parse(no_issue, {}, ()), no_issue)
        import copy
        for change in ("unrelated", "duplicate", "forged", "missing"):
            changed = copy.deepcopy(payload)
            candidate = changed["findings"][0]
            new = ()
            if change == "unrelated":
                candidate["plan_comparisons"][0]["same_issue"] = False
            elif change == "duplicate":
                candidate.update(status="new_plan", plan_id="G-101")
                new = ("G-101",)
            elif change == "forged":
                candidate["plan_comparisons"][0]["plan_excerpt"] = "A made up scope that is absent from the plan."
            else:
                candidate["plan_comparisons"] = []
            with self.subTest(change=change), self.assertRaises(maintenance_reconciliation.ReconciliationError):
                parse(changed, {"G-100": "READY"}, new)
        for reason in ("Everything looks fine after careful review.",
                       {"files": [], "relations": []},
                       {"files": [{"id": source.path, "excerpt": "This review evidence was fabricated.",
                                   "reason": "There is no issue requiring any refactoring."}], "relations": []}):
            with self.subTest(reason=reason), self.assertRaises(maintenance_reconciliation.ReconciliationError):
                parse({**no_issue, "no_issue_reason": reason}, {}, ())



if __name__ == "__main__":
    unittest.main()
