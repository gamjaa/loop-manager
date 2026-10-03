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
from loop_manager_internal.maintenance import maintenance_validation
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.storage import maintenance_file_ledger
from loop_manager_internal.maintenance import maintenance_file_publication
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory, TrackedSource
from loop_manager_internal.planning.assessment_port import MaintenanceAssessmentPort
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token


from tests.support.maintenance_planning_case import git


class MaintenanceSnapshotResumeTest(unittest.TestCase):
    def test_resumed_planner_reads_original_ready_blob(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE projects (repo TEXT, planner_session_id TEXT, "
            "agent_snapshot TEXT, planning_result_pending_validation INTEGER, "
            "planning_result_completed INTEGER, planning_validation_error TEXT, "
            "planning_retry_count INTEGER)"
        )
        connection.execute(
            "INSERT INTO projects VALUES ('/repo', 'saved-session', NULL, 1, 0, NULL, 0)"
        )
        strict = mock.Mock(side_effect=AssertionError("checkout is Planner output"))
        original = mock.Mock(return_value="original-ready-blob")
        effects = maintenance_validation.PrepareMaintenancePlanningContextEffects(
            _maintenance_context=lambda _repo, _sha, _cycle, _mode, plans: plans,
            _maintenance_mode_and_rows=lambda *_args: ("barrier", ("ready-row",)),
            _ready_plan_snapshot=strict,
            _ready_plan_snapshot_at_revision=original,
        )
        with mock.patch.object(maintenance_state.MaintenanceRepository, "get", return_value=object()):
            result = maintenance_validation.prepare_maintenance_planning_context(
                effects, connection, "/repo", Path("/planning"), "start-revision"
            )
        self.assertEqual(result, ("original-ready-blob",))
        original.assert_called_once_with(Path("/planning"), "start-revision", "ready-row")
        strict.assert_not_called()

    def test_new_planner_checks_checkout_before_granting_ready_authority(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE projects (repo TEXT, planner_session_id TEXT, "
            "agent_snapshot TEXT, planning_result_pending_validation INTEGER, "
            "planning_result_completed INTEGER, planning_validation_error TEXT, "
            "planning_retry_count INTEGER)"
        )
        connection.execute("INSERT INTO projects VALUES ('/repo', NULL, NULL, 0, 0, NULL, 0)")
        strict = mock.Mock(return_value="strict-snapshot")
        original = mock.Mock(side_effect=AssertionError("new checkout must be checked"))
        effects = maintenance_validation.PrepareMaintenancePlanningContextEffects(
            _maintenance_context=lambda _repo, _sha, _cycle, _mode, plans: plans,
            _maintenance_mode_and_rows=lambda *_args: ("barrier", ("ready-row",)),
            _ready_plan_snapshot=strict,
            _ready_plan_snapshot_at_revision=original,
        )
        with mock.patch.object(maintenance_state.MaintenanceRepository, "get", return_value=object()):
            result = maintenance_validation.prepare_maintenance_planning_context(
                effects, connection, "/repo", Path("/planning"), "start-revision"
            )
        self.assertEqual(result, ("strict-snapshot",))
        strict.assert_called_once()
        original.assert_not_called()

class MaintenancePublicationTransactionTest(unittest.TestCase):
    def test_stale_publication_restart_rolls_back_if_workspace_cleanup_fails(self):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker (value) VALUES ('preserved')")
        connection.commit()
        cycle = SimpleNamespace(
            publication_stage="prepared",
            publication_base_sha="base",
            publication_commit_sha="plan-commit",
            publication_remote_oid=None,
        )

        class Repository:
            def get(self, _repo):
                return cycle

            def restart_prepared_batch(self, _repo, *, expected, new_base_sha):
                if expected is not cycle or new_base_sha != "advanced":
                    raise AssertionError("unexpected maintenance restart input")
                connection.execute("UPDATE marker SET value = 'restarted'")
                return cycle

        with mock.patch("loop_manager.maintenance_state.MaintenanceRepository",
                        return_value=Repository()), \
                mock.patch("loop_manager.git",
                           return_value=SimpleNamespace(stdout="advanced\n")), \
                mock.patch("loop_manager._git_commit_is_ancestor",
                           side_effect=(True, False)), \
                mock.patch("loop_manager._git_tree_entries",
                           side_effect=({}, {}, {})), \
                mock.patch("loop_manager._clear_active_in_current_transaction"):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                with loop_manager.runtime_state._transaction(connection):
                    loop_manager.restart_stale_maintenance_publication(
                        connection, "/repo", Path("/planning"), "main"
                    )
                    self.assertEqual(connection.execute(
                        "SELECT value FROM marker"
                    ).fetchone()[0], "restarted")
                    raise RuntimeError("cleanup failed")
        self.assertEqual(connection.execute(
            "SELECT value FROM marker"
        ).fetchone()[0], "preserved")
        connection.close()

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
                mock.patch.object(loop_manager, "plan_declarations", return_value={path: ("blob", "f" * 40)}),
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

class FullAssessmentEvidenceTest(unittest.TestCase):
    def test_saved_report_accepts_historical_finding_and_rejects_drift(self):
        revision = "a" * 40
        inventory = {"module.py": "b" * 64}
        report = {
            "source_revision": revision,
            "coverage": {key: "Reviewed source responsibility evidence."
                         for key in ("code", "tests", "documents")},
            "findings": [{
                "id": "agent-scope", "kind": "code", "path": "module.py",
                "source_hash": inventory["module.py"],
                "evidence": "G-051 already separated Agent scope responsibilities.",
                "status": "resolved", "plan_ids": [],
            }],
        }
        saved = json.dumps(report)
        self.assertEqual(json.loads(maintenance_validation.validate_saved_assessment_result(
            saved, inventory, revision,
        )), report)
        self.assertIn(maintenance_validation.validate_saved_assessment_result(
            saved, inventory, revision,
        ), MaintenanceAssessmentPort().prompt(
            inventory, revision, saved,
        ))
        with self.assertRaises(maintenance_file_inventory.AssessmentError):
            MaintenanceAssessmentPort().prompt(
                {"module.py": "c" * 64}, revision, saved,
            )
        with self.assertRaises(maintenance_file_inventory.AssessmentError):
            maintenance_validation.validate_saved_assessment_result(
                saved.replace('"source_hash": "', '"source_hash": "0'),
                inventory, revision,
            )

    def test_new_report_does_not_require_historical_plan_finding(self):
        revision = "a" * 40
        inventory = {"module.py": "b" * 64}
        report = {
            "source_revision": revision,
            "coverage": {key: "Reviewed source responsibility evidence."
                         for key in ("code", "tests", "documents")},
            "findings": [{
                "id": "current-source", "kind": "code", "path": "module.py",
                "source_hash": inventory["module.py"],
                "evidence": "Current source responsibilities remain separated.",
                "status": "no_issue", "plan_ids": [],
            }],
        }
        result = maintenance_validation.parse_assessment_result(
            maintenance_validation.RESULT_PREFIX + json.dumps(report),
            inventory, revision, (), set(),
        )
        self.assertEqual(json.loads(result), report)
        self.assertNotIn("G-051", maintenance_planning.assessment_prompt(
            inventory, revision, None,
        ))

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
                with self.assertRaises(maintenance_file_inventory.AssessmentError):
                    maintenance_validation.parse_assessment_result(
                        maintenance_validation.RESULT_PREFIX + json.dumps(report),
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
            inventory = maintenance_file_inventory.source_inventory(repo, sha)
            self.assertIn("tests/test_module.py", inventory)
            self.assertNotIn("docs/requirements/archive/old.md", inventory)
            self.assertNotIn("node_modules/vendor.py", inventory)
            prompt = maintenance_planning.assessment_prompt(inventory, sha, None)
            self.assertIn(inventory["module.py"], prompt)
            self.assertNotIn("docs/requirements/archive/old.md", prompt)
            self.assertIn("saved evidence", maintenance_planning.assessment_prompt(
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
            output = maintenance_validation.RESULT_PREFIX + json.dumps(report)
            saved = maintenance_validation.parse_assessment_result(
                output, inventory, sha, ("G-100",), set())
            self.assertEqual(json.loads(saved), report)
            report["findings"][1]["source_hash"] = "0" * 64
            with self.assertRaises(maintenance_file_inventory.AssessmentError):
                maintenance_validation.parse_assessment_result(
                    maintenance_validation.RESULT_PREFIX + json.dumps(report),
                    inventory, sha, ("G-100",), set())

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

    def test_excluded_root_moves_link_new_and_existing_plans_and_reject_omissions(self):
        from copy import deepcopy
        from dataclasses import replace
        roots = tuple({"path": path, "category": "internal_implementation", "destination": destination,
                       "references": [{"path": "README.md", "excerpt": f"Internal material: {path}"}],
                       "user_path": "This material is not a user entry point.",
                       "ownership": "Documentation team owns this internal material.",
                       "reason": f"Move internal material from {path} to {destination}."}
                      for path, destination in (("notes.md", "docs/notes.md"), ("scratch.png", "assets/scratch.png")))
        for with_source in (False, True):
            content = b"def calculate(): return 1\n"
            src = TrackedSource("src/calc.py", hashlib.sha256(content).hexdigest(), "100644", "a" * 40, "source")
            inventory = SourceInventory(src.revision, (src,) if with_source else (), ())
            progress = maintenance_reconciliation.ReconciliationProgress(
                (src,) if with_source else (), (), (), None, root_reviews=roots)
            findings = [{"issue_key": "root-" + root["path"], "targets": [root["path"]],
                "evidence": [{"path": root["path"], "root_review_hash": maintenance_file_ledger.root_review_hash(root),
                              "excerpt": root["reason"]}], "relation_keys": [],
                "boundary": root["reason"] + " Preserve import, relative paths, launcher, config, documentation links and verify.",
                "context_reduction": "Move internal material out of the user entry directory.",
                "integration_cost": "Update references and validate preserved execution and public contracts.",
                "status": "new_plan", "plan_id": f"G-{100 + index}", "plan_comparisons": []}
                for index, root in enumerate(roots)]
            payload = {"source_revision": src.revision, "findings": findings, "no_issue_reason": None}
            parse = lambda value, existing={}, new=("G-100", "G-101"), texts={}: maintenance_reconciliation.parse_findings(
                value, progress, inventory, {src.path: content}, existing, new, texts)
            self.assertEqual(parse(payload), payload)
            for invalid in ("omission", "uncertain", "forged", "wrong_destination", "invalid_boundary", "no_plan", "source_hash"):
                bad = deepcopy(payload)
                bad_progress = progress
                if invalid == "omission":
                    bad["findings"].pop()
                elif invalid == "uncertain":
                    bad_progress = replace(progress, root_reviews=tuple(dict(root, category="uncertain", destination=None) for root in roots))
                elif invalid == "forged":
                    bad["findings"][0]["evidence"][0]["root_review_hash"] = "0" * 64
                elif invalid == "wrong_destination":
                    bad["findings"][0]["boundary"] = "Move this material to a different place without its assessed destination."
                elif invalid == "invalid_boundary":
                    bad["findings"][0]["boundary"] = None
                elif invalid == "source_hash":
                    bad["findings"][0]["evidence"] = [{"path": "notes.md", "content_hash": src.content_hash,
                                                        "mode": src.mode, "excerpt": "def calculate()"}]
                else:
                    bad["findings"] = []
                    bad["no_issue_reason"] = {"files": [], "relations": []}
                with self.subTest(source=with_source, invalid=invalid), self.assertRaises(maintenance_reconciliation.ReconciliationError):
                    maintenance_reconciliation.parse_findings(bad, bad_progress, inventory, {src.path: content}, {}, ("G-100", "G-101"))
            existing_payload = deepcopy(payload)
            for finding in existing_payload["findings"]:
                finding.update(status="existing_plan", plan_id="G-100", plan_comparisons=[{
                    "plan_id": "G-100", "plan_excerpt": "Move internal notes and the scratch image into their assessed destinations.",
                    "finding_excerpt": finding["boundary"], "same_issue": True,
                    "reason": "The existing plan owns this same assessed root placement change.",
                }])
            self.assertEqual(parse(existing_payload, {"G-100": "READY"}, (), {
                "G-100": "Move internal notes and the scratch image into their assessed destinations."}), existing_payload)

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
