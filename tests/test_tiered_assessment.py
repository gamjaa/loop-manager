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
from loop_manager_internal.planning.planning_runtime import PlanningAgentResult, PlanningAgentResultKind, PlanningLifecycleCoordinator, PlanningOutcomeKind
from loop_manager_internal.storage.maintenance_state import MaintenancePhase, ReviewDue
from loop_manager_internal.storage import maintenance_file_ledger
from tests.support.planning_case import PlanningCase, FakePorts, planning_snapshot, maintenance_cycle
from tests.support.maintenance import inventory


class TieredAssessmentTest(PlanningCase):
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
