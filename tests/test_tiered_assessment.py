from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.maintenance import code_index, maintenance_file_review, maintenance_reconciliation, tiered_review
from loop_manager_internal.maintenance.tiered_ports import TieredMaintenancePort
from loop_manager_internal.planning import tiered_assessment
from loop_manager_internal.planning.planning_runtime import PlanningAgentResult, PlanningAgentResultKind, PlanningLifecycleCoordinator, PlanningOutcomeKind, PlanningDecisionKind
from loop_manager_internal.storage.maintenance_state import MaintenancePhase, ReviewDue
from loop_manager_internal.storage import maintenance_file_ledger
from tests.support.planning_case import PlanningCase, FakePorts, planning_snapshot, maintenance_cycle
from tests.support.maintenance import inventory


class TieredAssessmentTest(PlanningCase):
    def test_first_run_selects_general_planner_or_ready_without_maintenance(self):
        self.ports.tiered.due = lambda context: TieredMaintenancePort.due(self.ports.tiered, context)
        self.ports.git.remote = self.sha
        context = replace(self.context, registered_only=True, base_sha=self.sha)
        for reviewed in (False, True):
            with self.subTest(reviewed=reviewed):
                self.ports.state.snapshot = planning_snapshot(**({
                    "processed_sha": self.sha, "planning_fingerprint": "fingerprint",
                    "specification_fingerprint": "specification",
                } if reviewed else {}))
                self.ports.state.cycle = maintenance_cycle()
                self.ports.state.schedule_result.has_ready = True
                self.ports.state.schedule_result.runnable_ready = object()
                run = self.coordinator._new_run(context)
                self.assertIsNone(self.coordinator._selection._select_planning_work(run))
                self.assertEqual(run.selection.kind, PlanningDecisionKind.ACTIVATE_READY
                                 if reviewed else PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)
                self.assertFalse(run.maintenance_planning)
                self.assertEqual(self.ports.state.maintenance_begin_count, 0)
                self.ports.tiered.select.assert_not_called()

    def setUp(self):
        super().setUp()
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
        self.connection.execute("INSERT INTO projects VALUES ('/repo')")
        self.connection.execute("CREATE TABLE exec_plans (repo TEXT, plan_id TEXT, status TEXT, path TEXT)")
        maintenance_file_review.ensure_schema(self.connection)
        maintenance_file_ledger.ensure_schema(self.connection)
        maintenance_reconciliation.ensure_schema(self.connection)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.context = replace(self.context, state_dir=Path(temporary.name), self_repository=True)
        self.contents = {"src/a.py": b"def calculate(value):\n    return value + 1\n", "src/b.py": b"from src import a\n"}
        self.sha = "a" * 40
        self.index = code_index.build_index(self.connection, "/repo:remote", inventory(self.contents, self.sha),
                                            lambda source: self.contents[source.path])
        self.ports = FakePorts(planning_snapshot(phase="PLANNING", active_sha=self.sha),
            maintenance_cycle(phase=MaintenancePhase.ASSESSMENT, cycle_base_sha=self.sha, revision=1, cycle_start_merged_count=0))
        self.ports.git.head = self.sha
        self.ports.tiered = TieredMaintenancePort(self.connection)
        self.ports.tiered.content = lambda _repo, _sha, path: self.contents[path]
        self.ports.tiered.select = mock.Mock(return_value=tiered_review.Selection(
            True, "full", ("initial_full",), tuple(self.contents), self.index, (), 0))
        self.ports.tiered.due = mock.Mock(return_value=ReviewDue(True, "initial_full", 0, MaintenancePhase.IDLE))
        self.ports.validation.validate_maintenance_result = mock.Mock(return_value=None)
        self.coordinator = PlanningLifecycleCoordinator(self.ports)
        self.run = self.coordinator._new_run(self.context)
        self.run.planning_worktree = self.context.state_dir / "planning"
        self.run.planning_worktree.mkdir()
        self.run.maintenance_context = object()

    def model(self, output=None, findings=None):
        def invoke(worktree, prompt, session, callback, **profile):
            self.assertEqual(profile["profile_role"], "maintenance_planner")
            self.assertTrue(profile["read_only"])
            self.assertEqual(profile["purpose"], "maintenance_review")
            self.assertTrue(profile["target"])
            self.ports.agent.run_count += 1
            callback("unit-session")
            assessment = self.ports.tiered.repository.active("/repo", 1, self.sha)
            unit = self.ports.tiered.repository.pending("/repo", assessment)
            value = {"digest": unit["digest"], "reviews": [{"module": part["module"],
                "summary": "The module has a cohesive responsibility and concrete source relations.",
                "citations": [{"path": next(iter(part["records"])), "excerpt": (self.contents[next(iter(part["records"]))].decode().splitlines() or [""])[0]}]}
                for part in unit["parts"]], "findings": findings or []}
            return PlanningAgentResult(0, output if output is not None else tiered_review.RESULT_PREFIX + json.dumps(value), "unit-session", PlanningAgentResultKind.SUCCESS)
        self.ports.agent.run_planner = invoke

    def turn(self):
        self.run.row = self.ports.state.planning("/repo")
        return self.coordinator._assessment._run_planner_turn(self.run)

    def test_all_blank_assessment_finishes_without_any_agent_call(self):
        self.contents = {"src/a.py": b"", "src/b.py": b" \t\n"}
        index = code_index.build_index(self.connection, "/repo:remote", inventory(self.contents, self.sha),
            lambda source: self.contents[source.path])
        self.ports.tiered.select.return_value = tiered_review.Selection(
            True, "full", ("initial_full",), tuple(self.contents), index, (), 0)
        self.ports.agent.run_planner = mock.Mock(side_effect=AssertionError("blank facts must not call the model"))
        outcome, context = tiered_assessment.prepare_turn(self.ports, self.run, self.run.planning_worktree)
        self.assertIsNone(outcome)
        self.assertEqual(context, "no_model_needed")
        self.ports.agent.run_planner.assert_not_called()
        evidence = self.ports.tiered.repository.evidence("/repo", 1, self.sha)
        self.assertEqual(evidence["receipts"], [])
        self.assertEqual(set(evidence["records"]), set(self.contents))
        outcome, context = tiered_assessment.prepare_turn(self.ports, self.run, self.run.planning_worktree)
        self.assertEqual(context, "no_model_needed")
        self.ports.agent.run_planner.assert_not_called()

    def test_legacy_active_blank_assessment_is_reparsed_at_actual_turn_without_model(self):
        self.contents = {"src/a.py": b"", "src/b.py": b" \t\n"}
        index = code_index.build_index(self.connection, "/repo:remote", inventory(self.contents, self.sha),
            lambda source: self.contents[source.path])
        selection = tiered_review.Selection(True, "full", ("initial_full",), tuple(self.contents), index, (), 0)
        self.ports.tiered.select.return_value = selection
        active = self.ports.tiered.prepare(self.run)
        legacy = json.loads(json.dumps(active))
        legacy.pop('analysis_version')
        for record in legacy['records'].values():
            record['facts'].pop('whitespace_only')
        self.connection.execute('UPDATE tiered_maintenance_assessments SET assessment_json = ?', (json.dumps(legacy),))
        self.ports.agent.run_planner = mock.Mock(side_effect=AssertionError('blank source facts must not call the model'))
        outcome, context = tiered_assessment.prepare_turn(self.ports, self.run, self.run.planning_worktree)
        self.assertIsNone(outcome)
        self.assertEqual(context, 'no_model_needed')
        self.ports.agent.run_planner.assert_not_called()
        refreshed = self.ports.tiered.repository.active('/repo', 1, self.sha)
        self.assertEqual(refreshed['prior_analysis_assessment'], legacy)
        self.assertEqual(refreshed['paths'], legacy['paths'])

    def test_uncertain_empty_module_still_gets_a_source_bound_semantic_review(self):
        self.contents = {"src/empty.js": b""}
        index = code_index.build_index(self.connection, "/repo:remote", inventory(self.contents, self.sha),
            lambda source: self.contents[source.path])
        self.ports.tiered.select.return_value = tiered_review.Selection(
            True, "full", ("initial_full",), tuple(self.contents), index, (), 0)
        value = {"reviews": [{"summary": "The empty module has no implementation; unsupported relations remain uncertain.",
            "citations": [{"path": "src/empty.js", "excerpt": ""}]}], "findings": []}
        self.model(output=tiered_review.RESULT_PREFIX + json.dumps(value))
        self.assertEqual(self.turn().kind, PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertIsNone(self.turn())
        self.assertEqual(self.ports.agent.run_count, 1)
        self.assertEqual(self.ports.state.snapshot.planning_retry_count, 0)
        evidence = self.ports.tiered.repository.evidence('/repo', 1, self.sha)
        self.assertEqual(evidence['records']['src/empty.js']['facts']['uncertainties'], ['unsupported_source_format'])

    def test_formatted_module_result_needs_one_call_and_no_validation_retry(self):
        self.model()
        original = self.ports.agent.run_planner
        def formatted(*args, **kwargs):
            result = original(*args, **kwargs)
            value = json.loads(result.message.removeprefix(tiered_review.RESULT_PREFIX))
            value.pop('digest')
            for item in value['reviews']:
                item.pop('module')
            return replace(result, message='**MODULE_REVIEW_RESULT** =\n' + json.dumps(value, indent=2))
        self.ports.agent.run_planner = formatted
        self.assertEqual(self.turn().kind, PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertIsNone(self.turn())
        self.assertEqual(self.ports.agent.run_count, 1)
        self.assertEqual(self.ports.state.snapshot.planning_retry_count, 0)

    def test_full_module_review_then_no_extra_synthesis_or_planner_call(self):
        self.model()
        self.assertEqual(self.turn().kind, PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.assertIsNone(self.turn())
        self.assertEqual(self.ports.agent.run_count, 1)
        self.assertIn('"outcome":"no_change"', self.run.row.planning_result)
        self.assertIsNone(self.coordinator._assessment._validate_planner_result(self.run))
        self.assertIsNone(self.ports.tiered.repository.success("/repo"))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM file_review_ledger").fetchone())

    def test_fast_scan_has_zero_model_calls_and_no_semantic_receipts(self):
        self.ports.tiered.select.return_value = replace(self.ports.tiered.select.return_value,
            mode="fast", reasons=("known_incremental_change",))
        self.ports.agent.run_planner = mock.Mock(side_effect=AssertionError("fast scan cannot invoke a model"))
        self.assertIsNone(self.turn())
        self.assertIsNone(self.coordinator._assessment._validate_planner_result(self.run))
        self.assertEqual(self.ports.tiered.repository.evidence("/repo", 1, self.sha)["receipts"], [])
        self.ports.agent.run_planner.assert_not_called()

    def test_confirmed_root_move_candidate_prevents_no_change_and_enters_plan_context(self):
        root = {"path": "notes.md", "category": "internal_implementation",
            "destination": "docs/notes.md", "reason": "Move confirmed internal notes to their safe destination.",
            "references": [{"path": "notes.md", "excerpt": "internal notes content"}],
            "user_path": "This file is not a user entry point.",
            "ownership": "The application module owns this internal note."}
        self.ports.tiered.repository.evidence = mock.Mock(return_value={
            "receipts": [], "root_reviews": [root], "root_plan_links": None,
        })
        self.ports.tiered.prepare = mock.Mock(return_value={
            "mode": "fast", "source_revision": self.sha, "cycle": 1,
            "digest": "d" * 64, "records": {}, "roots": [], "reasons": [], "root_reviews": [root],
        })
        self.ports.tiered.repository.pending = mock.Mock(return_value=None)
        self.ports.tiered.repository.units = mock.Mock(return_value=())
        outcome, context = tiered_assessment.prepare_turn(
            self.ports, self.run, self.run.planning_worktree,
        )
        self.assertIsNone(outcome)
        manifest = Path(context.split("manifest: ", 1)[1].strip())
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["root_move_candidates"]["notes.md"]["destination"], "docs/notes.md")
        self.assertIn("읽기 전용 Review", context)

    def test_root_move_plan_requires_location_and_regression_contracts(self):
        from loop_manager_internal.maintenance.tiered_ports import _root_move_plan_covers

        text = """## Purpose
Move root notes.md to docs/notes.md while preserving user behavior, the public contract, and storage format.

## Acceptance Criteria
The plan moves notes.md to docs/notes.md and preserves import paths, execution commands, build and deploy, configuration discovery, relative paths, and documentation links.

## Implementation
Update references from notes.md to docs/notes.md, including import users and config discovery.

## Validation
Run the targeted unit test, the full verify, and a read-only Review.
"""
        self.assertTrue(_root_move_plan_covers("notes.md", "docs/notes.md", text))
        self.assertFalse(_root_move_plan_covers("notes.md", "docs/notes.md", text.replace("full verify", "unit test")))
        self.assertFalse(_root_move_plan_covers("notes.md", "docs/notes.md", text.replace("docs/notes.md", "docs/other.md")))
        self.assertEqual(tiered_review.plan_content_hash(b"plan\r\ntext\r\n"),
                         tiered_review.plan_content_hash(b"plan\ntext\n"))

    def test_root_move_candidate_rejects_cancelled_existing_plan(self):
        from loop_manager_internal.maintenance.maintenance_orchestration import MaintenancePlannerDecision, MaintenanceOutcome

        root = {"path": "notes.md", "category": "internal_implementation",
            "destination": "docs/notes.md", "reason": "Move the internal note to its safe destination.",
            "references": [{"path": "notes.md", "excerpt": "internal notes content"}],
            "user_path": "This file is not a user entry point.",
            "ownership": "The application module owns this internal note."}
        relative = "docs/plans/[G-001] Existing.md"
        plan = self.run.planning_worktree / relative
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("""## Purpose
Move notes.md to docs/notes.md while preserving user behavior, public API contract, and storage format.

## Acceptance Criteria
Move notes.md to docs/notes.md and preserve imports, execution commands, build and deploy, configuration discovery, relative paths, and documentation links.

## Implementation
Update imports, commands, build and deploy references, configuration discovery, relative paths, and documentation links while preserving user behavior, public API contract, and storage format.

## Validation
Run targeted unit tests, full verify, and a read-only Review.
""", encoding="utf-8")
        self.connection.execute("INSERT INTO exec_plans VALUES (?, ?, ?, ?)",
                                 ("/repo", "G-001", "CANCELLED", relative))
        self.ports.tiered.repository.evidence = mock.Mock(return_value={
            "receipts": [], "root_reviews": [root], "root_plan_links": None,
        })
        self.ports.tiered.repository.record_root_plan_links = mock.Mock()
        disposition = [{"index": 0, "status": "existing_plan", "plan_id": "G-001",
                        "reason": "This path and preservation plan are connected to the root evidence."}]
        self.run.row = replace(self.run.row,
            planning_result="MODULE_FINDINGS_RESULT: " + json.dumps(disposition))

        with self.assertRaises(tiered_review.TieredReviewError):
            self.ports.tiered.validate_final(
                self.run, MaintenancePlannerDecision(MaintenanceOutcome.NO_CHANGE),
            )
        self.ports.tiered.repository.record_root_plan_links.assert_not_called()

    def test_root_plan_links_bind_to_id_allocations_and_final_barrier_bytes(self):
        from loop_manager_internal.application.contracts import PlanIdAllocation, ValidatedPlanningResult
        from loop_manager_internal.maintenance.maintenance_orchestration import (
            MaintenanceOutcome, MaintenancePlannerDecision,
        )
        from tests.support.catalog import plan_text as execplan_text

        def plan_text(plan_id, path, destination, *, depends_on=()):
            text = execplan_text(plan_id, f"Preserve {path}", depends_on=depends_on,
                purpose=(f"Move {path} to {destination} while preserving user behavior, "
                         "the public API contract, and storage format."),
                acceptance_criteria=(f"Move {path} to {destination}; preserve import paths, execution commands, "
                    "build and deploy, configuration discovery, relative paths, and documentation links."),
                implementation=("Update imports, command references, build/deploy, configuration discovery, "
                    "relative paths, and documentation links while preserving user behavior, public API contract, "
                    "and storage format."))
            return text.replace(
                "- The full verify must preserve existing behavior.",
                "- Run the full verify and a read-only Review.",
            )

        roots = [
            {"path": "assets.md", "category": "internal_implementation", "destination": "docs/assets.md",
             "reason": "Move the internal asset note to its safe destination."},
            {"path": "notes.md", "category": "internal_implementation", "destination": "docs/notes.md",
             "reason": "Move the internal note to its safe destination."},
        ]
        plan_dir = self.run.planning_worktree / "docs/plans"
        plan_dir.mkdir(parents=True)
        existing_path = "docs/plans/[G-001] Preserve Assets.md"
        existing = self.run.planning_worktree / existing_path
        existing.write_text(plan_text("G-001", "assets.md", "docs/assets.md", depends_on=("G-000",)),
                            encoding="utf-8")
        source_path = "docs/plans/[G-003] Preserve Notes.md"
        source = self.run.planning_worktree / source_path
        source.write_text(plan_text("G-003", "notes.md", "docs/notes.md"), encoding="utf-8")
        self.connection.execute("INSERT INTO exec_plans VALUES (?, ?, ?, ?)",
            ("/repo", "G-001", "READY", existing_path))
        self.ports.tiered.repository.evidence = mock.Mock(return_value={
            "digest": "a" * 64, "receipts": [], "root_reviews": roots,
        })
        dispositions = [
            {"index": 0, "status": "existing_plan", "plan_id": "G-001",
             "reason": "The existing READY plan preserves the root asset and its references."},
            {"index": 1, "status": "new_plan", "plan_id": "G-003",
             "reason": "The new plan preserves the root note and its references."},
        ]
        self.run.row = replace(self.run.row,
            planning_result="MODULE_FINDINGS_RESULT: " + json.dumps(dispositions))
        decision = MaintenancePlannerDecision(MaintenanceOutcome.REFACTORING, ("G-003",))
        self.assertEqual(self.ports.tiered.validate_final(self.run, decision), ("G-001",))
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM tiered_maintenance_root_plan_links").fetchone())

        target_path = "docs/plans/[G-004] Preserve Notes.md"
        target = self.run.planning_worktree / target_path
        target.write_text(plan_text("G-004", "notes.md", "docs/notes.md"), encoding="utf-8")
        source.unlink()
        source_allocation = PlanIdAllocation(source_path, "G-003", target_path, "G-004")
        existing.write_text(plan_text("G-001", "assets.md", "docs/assets.md",
                                       depends_on=("G-000", "G-004")), encoding="utf-8")
        general_source_path = "docs/plans/[G-001] New General Plan.md"
        general_target_path = "docs/plans/[G-002] New General Plan.md"
        (self.run.planning_worktree / general_target_path).write_text(
            "A separate newly normalized general plan.", encoding="utf-8",
        )
        general_allocation = PlanIdAllocation(
            general_source_path, "G-001", general_target_path, "G-002",
        )
        validated_result = ValidatedPlanningResult(
            plans=((target_path, {"id": "G-004", "title": "Preserve notes.md"}),
                   (general_target_path, {"id": "G-002", "title": "New General Plan"})),
            publication_paths=(existing_path, target_path, general_target_path),
            id_allocations=(general_allocation, source_allocation),
        )
        drafts = self.run.tiered_root_plan_links
        mismatched_allocation = PlanIdAllocation(
            "docs/plans/[G-003] Different Source.md", "G-003", target_path, "G-004",
        )
        invalid_result = ValidatedPlanningResult(
            plans=validated_result.plans,
            publication_paths=validated_result.publication_paths,
            id_allocations=(general_allocation, mismatched_allocation),
        )
        with self.assertRaisesRegex(tiered_review.TieredReviewError, "원본 경로"):
            self.ports.tiered.record_final_root_plan_links(self.run, invalid_result)
        self.assertEqual(self.run.tiered_root_plan_links, drafts)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM tiered_maintenance_root_plan_links").fetchone())

        self.ports.tiered.record_final_root_plan_links(self.run, validated_result)

        saved = json.loads(self.connection.execute(
            "SELECT links_json FROM tiered_maintenance_root_plan_links").fetchone()[0])
        by_root = {item["path"]: item for item in saved}
        self.assertEqual((by_root["assets.md"]["plan_id"], by_root["assets.md"]["plan_path"]),
                         ("G-001", existing_path))
        self.assertEqual((by_root["notes.md"]["plan_id"], by_root["notes.md"]["plan_path"]),
                         ("G-004", target_path))
        self.assertEqual(by_root["notes.md"]["plan_hash"],
                         tiered_review.plan_content_hash(target.read_bytes()))
        self.assertEqual(by_root["assets.md"]["plan_hash"],
                         tiered_review.plan_content_hash(existing.read_bytes()))
        self.assertEqual(self.run.tiered_root_plan_links, ())

    def test_planner_result_saves_root_links_after_maintenance_validation(self):
        events = []
        result = object()
        self.run.maintenance_planning = True
        self.run.dirty_files = []
        self.run.tiered_root_plan_links = ({"path": "notes.md"},)
        self.run.row = replace(self.run.row,
            planning_result='MAINTENANCE_RESULT: {"outcome":"no_change"}')
        self.ports.tiered.validate_final = mock.Mock(
            side_effect=lambda *_args: (events.append("disposition"), ())[1])
        self.ports.validation.validate_maintenance_result = mock.Mock(
            side_effect=lambda *_args, **_kwargs: (events.append("validated"), result)[1])
        self.ports.tiered.record_final_root_plan_links = mock.Mock(
            side_effect=lambda _run, validated: (events.append("links"), self.assertIs(validated, result)))

        with mock.patch.object(tiered_assessment, "enabled", return_value=True):
            outcome = self.coordinator._assessment._validate_planner_result(self.run)

        self.assertIsNone(outcome)
        self.assertEqual(events, ["disposition", "validated", "links"])

    def test_invalid_model_result_is_bounded_and_session_is_preserved(self):
        self.model(output="invalid model output")
        for _ in range(3):
            self.assertEqual(self.turn().kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(self.ports.state.snapshot.planning_retry_count, 3)
        self.assertTrue(self.ports.state.snapshot.planning_error)
        assessment = self.ports.tiered.repository.active("/repo", 1, self.sha)
        self.assertEqual(self.ports.tiered.repository.pending("/repo", assessment)["session_id"], "unit-session")
        self.assertIsNone(self.ports.tiered.repository.success("/repo"))

    def test_findings_require_final_disposition_instead_of_no_change_shortcut(self):
        finding = {"path": "src/a.py", "excerpt": "def calculate(value):", "reason": "This boundary needs a specific additional investigation."}
        self.model(findings=[finding])
        self.assertEqual(self.turn().kind, PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.run.row = self.ports.state.planning("/repo")
        outcome, context = tiered_assessment.prepare_turn(self.ports, self.run, self.run.planning_worktree)
        self.assertIsNone(outcome)
        self.assertIn("MODULE_FINDINGS_RESULT", context)
        self.run.row = replace(self.run.row, planning_result='MAINTENANCE_RESULT: {"outcome":"no_change"}', planning_result_completed=True)
        self.assertEqual(self.coordinator._assessment._validate_planner_result(self.run).kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertIsNone(self.ports.tiered.repository.success("/repo"))

    def test_partial_evidence_survives_repository_reconstruction(self):
        self.model()
        self.assertEqual(self.turn().kind, PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.ports.tiered = TieredMaintenancePort(self.connection)
        self.ports.tiered.select = mock.Mock(side_effect=AssertionError("resume cannot rebuild the selection"))
        self.assertIsNone(self.turn())
        self.assertEqual(self.ports.agent.run_count, 1)

    def test_preserved_legacy_final_result_keeps_original_validation_path(self):
        self.run.row = replace(self.run.row, planning_result_completed=True)
        self.assertFalse(tiered_assessment.enabled(self.ports, self.run))

    def test_due_assessment_keeps_priority_over_ready_and_blocked_other_plan(self):
        self.ports.state.snapshot = planning_snapshot()
        self.ports.state.cycle = maintenance_cycle()
        self.ports.state.schedule_result.has_ready = True
        self.ports.state.schedule_result.has_waiting = True
        self.ports.state.schedule_result.runnable_ready = object()
        self.ports.state.execplan_rows = lambda _: [{"status": "PLAN_BLOCKED"}]
        context = replace(self.context, registered_only=True, base_sha=self.sha)
        run = self.coordinator._new_run(context)
        self.assertIsNone(self.coordinator._selection._select_planning_work(run))
        self.assertEqual(self.ports.state.maintenance_begin_count, 1)
        self.assertTrue(run.maintenance_planning)
        self.assertFalse(run.ready_activated_before_planning)

    def test_open_pr_and_project_safety_block_defer_new_assessment(self):
        self.ports.state.snapshot = planning_snapshot()
        self.ports.state.cycle = maintenance_cycle()
        self.ports.state.schedule_result.has_waiting = True
        self.ports.state.execplan_rows = lambda _: [{"status": "PR_OPEN"}]
        context = replace(self.context, registered_only=True, base_sha=self.sha)
        self.coordinator._selection._select_planning_work(self.coordinator._new_run(context))
        self.ports.tiered.select.assert_not_called()
        self.ports.state.schedule_result.project_blocked = True
        self.assertEqual(self.coordinator._selection._select_planning_work(
            self.coordinator._new_run(context)).kind, PlanningOutcomeKind.WAITING)
        self.ports.tiered.select.assert_not_called()

    def test_finished_cycle_reenters_normal_selection_instead_of_starting_ready_directly(self):
        self.ports.state.snapshot = planning_snapshot()
        self.ports.state.cycle = maintenance_cycle()
        self.run.row = self.ports.state.snapshot
        self.assertEqual(self.coordinator._selection._activate_next_execplan(self.run).kind,
                         PlanningOutcomeKind.MAINTENANCE_COMPLETE)

    def test_interval_pending_skips_static_assessment_even_with_changed_code_and_completion_candidate(self):
        self.ports.state.snapshot = planning_snapshot(processed_sha=self.sha,
            planning_fingerprint="fingerprint", specification_fingerprint="specification")
        self.ports.state.cycle = maintenance_cycle(last_cycle_merged_count=5)
        self.ports.tiered.due.return_value = ReviewDue(False, "interval_pending", 19, MaintenancePhase.IDLE)
        context = replace(self.context, registered_only=True, base_sha=self.sha)
        run = self.coordinator._new_run(context)
        self.coordinator._selection._select_planning_work(run)
        self.ports.tiered.select.assert_not_called()
        self.assertEqual(self.ports.state.maintenance_begin_count, 0)
        self.assertFalse(run.maintenance_planning)
        self.assertEqual(self.ports.agent.run_count, 0)

    def test_source_drift_resumes_original_scope_and_request_without_exploring_new_module(self):
        self.model()
        self.assertEqual(self.turn().kind, PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        self.contents["src/a.py"] = b"def calculate(value):\n    return value + 2\n"
        self.contents["src/c.py"] = b"def same_module():\n    return 4\n"
        self.contents["unrelated/new.py"] = b"def independent():\n    return 3\n"
        revision = "b" * 40
        index = code_index.build_index(self.connection, "/repo:remote", inventory(self.contents, revision),
            lambda source: self.contents[source.path])
        self.ports.tiered.select.return_value = tiered_review.Selection(
            True, "full", ("requested_full",), tuple(self.contents), index, (), 0, "new-request")
        self.run.row = replace(self.run.row, active_sha=revision)
        self.run.maintenance_cycle = replace(self.run.maintenance_cycle, revision=2)
        resumed = self.ports.tiered.prepare(self.run)
        self.assertNotIn("unrelated/new.py", resumed["paths"])
        self.assertIn("src/c.py", resumed["paths"])
        self.assertIsNone(resumed["request_id"])
        self.assertEqual(resumed["reasons"], ["resume_related_scope"])
        self.assertEqual(resumed["mode"], "targeted")
        self.assertIsNone(self.ports.tiered.repository.success("/repo"))

    def test_unrelated_source_drift_reuses_receipts_without_claiming_current_full_review(self):
        self.model()
        self.turn()
        self.contents["unrelated/new.py"] = b"def independent():\n    return 3\n"
        revision = "b" * 40
        index = code_index.build_index(self.connection, "/repo:remote", inventory(self.contents, revision),
            lambda source: self.contents[source.path])
        self.ports.tiered.select.return_value = tiered_review.Selection(
            True, "full", ("initial_full",), tuple(self.contents), index, (), 0)
        self.run.row = replace(self.run.row, active_sha=revision)
        self.run.maintenance_cycle = replace(self.run.maintenance_cycle, revision=2)
        assessment = self.ports.tiered.prepare(self.run)
        self.assertIsNone(self.ports.tiered.repository.pending("/repo", assessment))
        evidence = self.ports.tiered.repository.evidence("/repo", 2, revision)
        self.ports.tiered.repository.promote("/repo", evidence)
        success = self.ports.tiered.repository.success("/repo")
        self.assertIsNone(success["full_source_revision"])
        self.assertIsNone(success["full_merged_count"])
        self.assertEqual(self.ports.agent.run_count, 1)

    def test_rejected_and_existing_findings_need_dispositions_and_keep_existing_scope(self):
        from loop_manager_internal.maintenance.maintenance_orchestration import MaintenancePlannerDecision, MaintenanceOutcome
        finding = {"path": "src/a.py", "excerpt": "def calculate(value):",
                   "reason": "This boundary is handled by an existing declared plan."}
        self.model(findings=[finding])
        self.turn()
        plan_dir = self.run.planning_worktree / "docs/plans"
        plan_dir.mkdir(parents=True)
        (plan_dir / "[G-001] Existing.md").write_text("Implementation references src/a.py", encoding="utf-8")
        self.connection.execute("INSERT INTO exec_plans VALUES ('/repo', 'G-001', 'READY', 'docs/plans/[G-001] Existing.md')")
        decision = MaintenancePlannerDecision(MaintenanceOutcome.NO_CHANGE)
        for status, plan_id, expected in (("existing_plan", "G-001", ("G-001",)), ("rejected", None, ())):
            disposition = [{"index": 0, "status": status, "plan_id": plan_id,
                            "reason": "Concrete source evidence explains this disposition."}]
            self.run.row = replace(self.run.row, planning_result="MODULE_FINDINGS_RESULT: " + json.dumps(disposition))
            self.assertEqual(self.ports.tiered.validate_final(self.run, decision), expected)


if __name__ == "__main__":
    unittest.main()
