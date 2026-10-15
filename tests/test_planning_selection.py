from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.planning.requirement_retirement import FinalInput
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import maintenance_state, planning_input_state
from loop_manager_internal.planning.input_fingerprints import (
    IsRequirementArchiveOnlyChangeEffects, _is_requirement_archive_only_change,
)
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.maintenance.maintenance_file_inventory import RootPlacement, TrackedSource
from tests.support.maintenance import result_for
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from loop_manager_internal.planning.planning_workspace import (
    planning_workspace_path, validate_planning_workspace_identity,
    ValidatePlanningWorkspaceIdentityEffects,
)
from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
from loop_manager_internal.maintenance.maintenance_orchestration import maintenance_planning_fingerprint
from loop_manager_internal.planning.planning_runtime import (
    PlanningAgentResult,
    PlanningAgentResultKind,
    PlanningContext,
    PlanningLifecycleCoordinator,
    PlanningOutcomeKind,
    PlanningResultInvalid,
    PlanningScopeResult,
    PlanningWorkspaceOwnershipError,
    PlanningDecisionKind,
    choose_planning_decision,
)
from loop_manager_internal.storage.runtime_state import PlanningSnapshot
from loop_manager_internal.planning.planning_contracts import snapshot_run

from tests.support.planning_case import (
    PlanningCase, FakePorts, FakeStatePort, FakeGitPort, FakeAgentPort,
    FakeValidationPort, FakeEventPort, planning_snapshot, maintenance_cycle,
)
from tests.support.catalog import plan_text
from tests.support.git import isolated_git_test

class PlanningSelectionTest(PlanningCase):
    def test_code_or_plan_change_never_starts_check_or_fallback_planner(self):
        for ready in ('runnable', 'waiting', 'none'):
            with self.subTest(ready=ready):
                ports = self._code_check_ports()
                ports.state.plan_declarations_match_result = False
                ports.state.schedule_result.has_ready = ready != 'none'
                ports.state.schedule_result.runnable_ready = object() if ready == 'runnable' else None
                outcome = PlanningLifecycleCoordinator(ports).run(self.context)
                self.assertEqual(ports.agent.run_count, 0)
                self.assertIsNone(ports.state.check_record)
                self.assertIn(outcome.kind, {PlanningOutcomeKind.EXECUTION_READY,
                    PlanningOutcomeKind.WAITING, PlanningOutcomeKind.FINGERPRINT_SKIPPED})

    def test_requirement_check_precedes_other_ready_work_and_new_maintenance(self):
        ports = self._code_check_ports()
        ports.state.maintenance_due_required = True
        ports.retirement = SimpleNamespace(pending=lambda *_: (FinalInput(
            path='docs/requirements/R.md', token='pinned', source_oid='blob',
            criteria=('Works',), related_ids=('G-002',)),))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(run.selection.kind, PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)
        self.assertFalse(run.ready_activated_before_planning)
        self.assertEqual(ports.state.cycle.phase, maintenance_state.MaintenancePhase.IDLE)
        for phase in (maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                      maintenance_state.MaintenancePhase.REFACTORING):
            ports.state.cycle = maintenance_cycle(phase=phase, refactoring_plan_ids=('G-001',))
            run = coordinator._new_run(self.context)
            self.assertIsNone(coordinator._selection._select_planning_work(run))
            self.assertEqual(run.selection.kind, PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)
            self.assertFalse(run.ready_activated_before_planning)
            self.assertEqual(ports.state.cycle.phase, phase)

    def test_requirement_check_parks_other_active_work_at_safe_stage_boundary(self):
        for writer in (False, True):
            with self.subTest(writer=writer):
                original = planning_snapshot(phase='VERIFYING', active_sha='base', worktree='/saved',
                    processed_sha='base', planning_fingerprint='fingerprint', specification_fingerprint='specification')
                ports = FakePorts(original)
                ports.state.schedule_result.active = {'plan_id': 'G-003', 'pr_url': None, 'pr_conflict_recovery': 0}
                ports.state.planning_release_pending = lambda _: writer
                ports.retirement = SimpleNamespace(pending=lambda *_: (
                    FinalInput('docs/requirements/R.md', 'token', 'blob', ('Works',), ('G-002',)),))
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                outcome = coordinator._selection._select_planning_work(run)
                if writer:
                    self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
                    self.assertEqual(ports.state.snapshot, original)
                else:
                    self.assertIsNone(outcome)
                    self.assertEqual(run.row.planning_resume_plan_id, 'G-003')
                    self.assertEqual(ports.state.paused_execution, original)
                    self.assertEqual(run.selection.kind, PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)

    def test_verified_draft_response_triggers_planning_once_after_baseline(self):
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        from loop_manager_internal.storage import execplan_draft_state
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        execplan_draft_state.ensure_schema(connection)
        path = 'docs/plans/[G-001] Decision.md'
        planning_input_state.record_general(connection, '/repo', 'old', {path: ('blob', 'before')})
        saved = {'path': path, 'user_changes': {path: {
            'verified': True, 'before': 'Question', 'after': 'Question\nAnswer'}}}
        connection.execute('INSERT INTO execplan_drafts VALUES (?, ?, ?, ?)',
            ('/repo', 'G-001', 1, json.dumps(saved)))
        state = object.__new__(_PlanningStatePorts)
        state._connection = connection
        entries = {path: ('blob', 'after')}
        self.assertTrue(state.pending_user_decision('/repo', entries))
        ports = self._code_check_ports()
        ports.state.pending_user_decision = lambda *_: state.pending_user_decision('/repo', entries)
        run = PlanningLifecycleCoordinator(ports)._new_run(self.context)
        PlanningLifecycleCoordinator(ports)._selection._select_planning_work(run)
        self.assertEqual(run.selection.kind, PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)
        planning_input_state.record_general(connection, '/repo', 'new', entries)
        self.assertFalse(state.pending_user_decision('/repo', entries))

    def test_draft_reassessment_and_requirement_answer_are_reviewed_once(self):
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        from loop_manager_internal.storage import execplan_draft_state
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        execplan_draft_state.ensure_schema(connection)
        path = 'docs/plans/[G-001] Decision.md'
        entries = {path: ('blob', 'unchanged')}
        planning_input_state.record_general(connection, '/repo', 'old', entries)
        state = object.__new__(_PlanningStatePorts)
        state._connection = connection
        for saved in (
                {'path': path, 'revision': 'basis-1', 'reassessment': True, 'user_changes': {}},
                {'path': path, 'revision': 'basis-2', 'user_changes': {'docs/requirements/R.md': {
                    'verified': True, 'before': 'Question', 'after': 'Question\nAnswer'}}}):
            with self.subTest(saved=saved):
                connection.execute('INSERT OR REPLACE INTO execplan_drafts VALUES (?, ?, ?, ?)',
                                   ('/repo', 'G-001', 1, json.dumps(saved)))
                self.assertTrue(state.pending_user_decision('/repo', entries))
                ports = self._code_check_ports()
                ports.state.pending_user_decision = lambda *_: state.pending_user_decision('/repo', entries)
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                coordinator._selection._select_planning_work(run)
                self.assertEqual(run.selection.kind, PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)
                planning_input_state.record_general(connection, '/repo', 'new', entries)
                self.assertFalse(state.pending_user_decision('/repo', entries))
                self.assertFalse(state.pending_user_decision('/repo', {path: ('blob', 'progress-only')}))
                self.assertIn('G-001', execplan_draft_state.pending_ids(connection, '/repo'))
                recorded = execplan_draft_state.evidence(connection, '/repo', 'G-001')
                recorded['revision'] = saved['revision'] + '-new-condition'
                connection.execute('UPDATE execplan_drafts SET evidence=?', (json.dumps(recorded),))
                self.assertTrue(state.pending_user_decision('/repo', entries))

    def test_general_planning_finishes_before_resuming_same_active_execution(self):
        original = planning_snapshot(phase='VERIFYING', active_sha='old-base', worktree='/saved',
            processed_sha='old-base', planning_fingerprint='old-full', specification_fingerprint='old-spec')
        ports = FakePorts(original)
        ports.git.specification_fingerprints['old-base'] = 'old-spec'
        ports.state.schedule_result.active = {'plan_id': 'G-001', 'pr_url': None,
            'pr_conflict_recovery': 0, 'worktree': '/saved'}
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.phase, 'VERIFYING')
        self.assertEqual(ports.state.snapshot.worktree, '/saved')
        self.assertEqual(ports.state.snapshot.processed_sha, 'base')
        self.assertIsNone(ports.state.snapshot.planning_resume_plan_id)
        PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(ports.agent.run_count, 1)

    def test_general_planning_restart_preserves_active_maintenance_and_resumes_same_execution(self):
        for phase in (maintenance_state.MaintenancePhase.REFACTORING,
                      maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION):
            with self.subTest(phase=phase):
                original = planning_snapshot(phase='VERIFYING', active_sha='old-base', worktree='/saved',
                    processed_sha='old-base', planning_fingerprint='old-full', specification_fingerprint='old-spec')
                cycle = maintenance_cycle(phase=phase, refactoring_plan_ids=('G-001',))
                ports = FakePorts(original, cycle)
                ports.git.specification_fingerprints['old-base'] = 'old-spec'
                ports.state.schedule_result.active = {'plan_id': 'G-001', 'pr_url': None,
                    'pr_conflict_recovery': 0, 'worktree': '/saved'}
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                coordinator._selection._select_planning_work(run)
                coordinator._selection._reconcile_inputs(run)
                self.assertEqual(ports.state.snapshot.phase, 'PLANNING')
                outcome = PlanningLifecycleCoordinator(ports).run(self.context)
                self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
                self.assertEqual(ports.agent.run_count, 1)
                self.assertEqual(ports.state.snapshot.phase, 'VERIFYING')
                self.assertEqual(ports.state.snapshot.worktree, '/saved')
                self.assertIsNone(ports.state.snapshot.planning_resume_plan_id)
                self.assertEqual(ports.state.cycle, cycle)

    def test_local_recheck_restores_parked_general_planning_with_active_maintenance(self):
        row = planning_snapshot(phase='PLANNING', active_sha='base', worktree='/planning',
            planning_resume_plan_id='G-001', planning_error='failed', planning_error_fingerprint='fingerprint')
        ports = FakePorts(row, maintenance_cycle(phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION))
        ports.state.schedule_result.active = {'plan_id': 'G-001'}
        ports.state.suspend_planning(row, unconfirmed_plan_ids=('G-001',))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(replace(self.context, local_environment_recheck=True))
        self.assertIsNone(coordinator._selection._gate_preserved_error(run))
        self.assertFalse(run.maintenance_planning)
        self.assertEqual(run.row.phase, 'PLANNING')
        self.assertEqual(run.row.planning_resume_plan_id, 'G-001')
        self.assertIsNone(run.row.planning_preserved_state)

    def test_specification_change_parks_active_execution_before_general_planning(self):
        original = planning_snapshot(phase='VERIFYING', active_sha='old-base', worktree='/saved',
            processed_sha='old-base', planning_fingerprint='old-full', specification_fingerprint='old-spec')
        ports = FakePorts(original)
        ports.git.specification_fingerprints['old-base'] = 'old-spec'
        ports.state.schedule_result.active = {'plan_id': 'G-001', 'pr_url': None, 'pr_conflict_recovery': 0}
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(run.selection.kind, PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)
        self.assertEqual(run.row.planning_resume_plan_id, 'G-001')
        self.assertEqual(ports.state.paused_execution, original)
        coordinator._selection._reconcile_inputs(run)
        self.assertEqual(run.row.phase, 'PLANNING')
        self.assertEqual(run.row.planning_resume_plan_id, 'G-001')

    def test_changed_specification_waits_for_running_agent_without_parking_execution(self):
        original = planning_snapshot(phase='VERIFYING', active_sha='old-base', worktree='/saved',
            processed_sha='old-base', planning_fingerprint='old-full', specification_fingerprint='old-spec')
        ports = FakePorts(original)
        ports.git.specification_fingerprints['old-base'] = 'old-spec'
        ports.state.schedule_result.active = {'plan_id': 'G-001', 'pr_url': None, 'pr_conflict_recovery': 0}
        ports.state.planning_release_pending = lambda _repo: True
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.state.snapshot, original)
        self.assertFalse(hasattr(ports.state, 'paused_execution'))
        self.assertEqual(ports.agent.run_count, 0)

    def test_stable_specification_resumes_active_without_planning_or_code_only_check(self):
        original = planning_snapshot(phase='VERIFYING', active_sha='base', worktree='/saved',
            processed_sha='base', planning_fingerprint='old-full', specification_fingerprint='fingerprint')
        ports = FakePorts(original)
        ports.state.schedule_result.active = {'plan_id': 'G-001', 'pr_url': None, 'pr_conflict_recovery': 0}
        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator.run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.state.snapshot, original)
        self.assertFalse(hasattr(ports.state, 'paused_execution'))

    def test_legacy_release_resumes_after_removal_and_state_clear_interruptions(self):
        for interruption in ("after_remove", "during_clear"):
            with self.subTest(interruption=interruption), tempfile.TemporaryDirectory() as directory:
                worktree = Path(directory) / "planning"
                worktree.mkdir()
                original = planning_snapshot(phase="PLANNING", active_sha="base", worktree=str(worktree),
                    planning_workspace_owned=1, planning_attempt_fingerprint="saved", planner_session_id="saved-session")
                ports = FakePorts(original, maintenance_cycle(
                    phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
                    refactoring_plan_ids=("G-001",)))
                ports.git.validate_planning_release = mock.Mock()
                ordinary_git = ports.git.run
                removals = []
                def remove_once(repo, *arguments, **kwargs):
                    if arguments[:2] == ("worktree", "remove"):
                        removals.append(arguments)
                        worktree.rmdir()
                        if interruption == "after_remove":
                            raise KeyboardInterrupt("after Git removal")
                    return ordinary_git(repo, *arguments, **kwargs)
                ports.git.run = remove_once
                clear = ports.state.clear_active_in_transaction
                if interruption == "during_clear":
                    def interrupt_clear(*args, **kwargs):
                        clear(*args, **kwargs)
                        raise KeyboardInterrupt("before SQLite commit")
                    ports.state.clear_active_in_transaction = interrupt_clear
                coordinator = PlanningLifecycleCoordinator(ports)
                with self.assertRaises(KeyboardInterrupt):
                    coordinator._selection._select_planning_work(coordinator._new_run(self.context))
                self.assertFalse(worktree.exists())
                self.assertEqual(ports.state.snapshot, original)
                self.assertIsNotNone(ports.state.cycle.legacy_release_identity)
                self.assertEqual(len(ports.state.diagnostics), 1)
                ports.state.clear_active_in_transaction = clear
                restarted = PlanningLifecycleCoordinator(ports)
                outcome = restarted._selection._select_planning_work(restarted._new_run(self.context))
                self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
                self.assertIsNone(ports.state.snapshot.phase)
                self.assertIsNone(ports.state.cycle.legacy_release_identity)
                self.assertEqual(len(removals), 1)
                self.assertEqual(len(ports.state.diagnostics), 1)
                ports.git.validate_planning_release.assert_called_with(original.repo, worktree, allow_missing=True)
                self.assertEqual(ports.agent.run_count, 0)

    def test_legacy_missing_workspace_requires_matching_intent_and_no_live_agent(self):
        from loop_manager_internal.planning.planning_workspace import legacy_release_identity
        with tempfile.TemporaryDirectory() as directory:
            row = planning_snapshot(phase="PLANNING", active_sha="base", worktree=str(Path(directory) / "missing"),
                planning_workspace_owned=1, planning_attempt_fingerprint="saved")
            cycle = maintenance_cycle(phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
                refactoring_plan_ids=("G-001",))
            identity = legacy_release_identity(row, cycle)
            for scenario in ("no_intent", "bad_identity", "live_agent", "changed_cycle", "changed_input"):
                with self.subTest(scenario=scenario):
                    intent = None if scenario == "no_intent" else "bad-identity" if scenario == "bad_identity" else identity
                    current_cycle = replace(cycle, legacy_release_identity=intent,
                        revision=cycle.revision + (scenario == "changed_cycle"))
                    current_row = replace(row, active_sha="different") if scenario == "changed_input" else row
                    ports = FakePorts(current_row, current_cycle)
                    ports.state.planning_release_pending = lambda _repo: scenario == "live_agent"
                    ports.git.validate_planning_release = mock.Mock()
                    coordinator = PlanningLifecycleCoordinator(ports)
                    outcome = coordinator._selection._select_planning_work(coordinator._new_run(self.context))
                    self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                    self.assertEqual(ports.state.snapshot, current_row)
                    self.assertEqual(ports.state.cycle.legacy_release_identity, intent)
                    self.assertEqual(ports.agent.run_count, 0)

    def test_released_workspace_validation_rejects_remaining_registration_and_mutations(self):
        from loop_manager_internal.planning.git_ports import _PlanningGitPorts
        with tempfile.TemporaryDirectory() as directory:
            root, worktree = Path(directory), Path(directory) / "missing"
            effects = SimpleNamespace(git=mock.Mock(return_value=SimpleNamespace(stdout="")), _nul_git_paths=mock.Mock())
            port = _PlanningGitPorts(effects, None, root, root / "state")
            with mock.patch("loop_manager_internal.git.manager_git_journal.has_pending_project_mutation", return_value=False):
                with self.assertRaises(PlanningWorkspaceOwnershipError):
                    port.validate_planning_release("/repo", worktree)
                port.validate_planning_release("/repo", worktree, allow_missing=True)
                effects.git.return_value.stdout = f"worktree {worktree}\0HEAD base\0detached\0\0"
                with self.assertRaisesRegex(PlanningWorkspaceOwnershipError, "Git 등록"):
                    port.validate_planning_release("/repo", worktree, allow_missing=True)
            with mock.patch("loop_manager_internal.git.manager_git_journal.has_pending_project_mutation", return_value=True):
                with self.assertRaisesRegex(PlanningWorkspaceOwnershipError, "미확정 Manager Git"):
                    port.validate_planning_release("/repo", worktree, allow_missing=True)

    def test_uncertain_maintenance_boundaries_preserve_diagnostics_without_activation(self):
        for operation in ("advance_maintenance_cycle", "maintenance_due"):
            with self.subTest(operation=operation):
                ports = FakePorts(planning_snapshot(), maintenance_cycle())
                ports.state.schedule_result.has_ready = True
                setattr(ports.state, operation, mock.Mock(side_effect=
                    maintenance_state.MaintenanceStateError("종료 경계 확인 불가")))
                ports.state.activate_ready = mock.Mock(side_effect=AssertionError("uncertain boundary"))
                coordinator = PlanningLifecycleCoordinator(ports)
                outcome = coordinator._selection._select_planning_work(coordinator._new_run(
                    replace(self.context, registered_only=True)))
                self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                self.assertEqual(ports.state.snapshot.planning_error, "종료 경계 확인 불가")
                self.assertEqual(ports.state.snapshot.planning_error_fingerprint, "maintenance-cycle:idle:0")
                self.assertEqual(ports.state.cycle.phase, maintenance_state.MaintenancePhase.IDLE)
                ports.state.activate_ready.assert_not_called()

    def test_project_safety_block_prevents_maintenance_completion_transition(self):
        ports = FakePorts(planning_snapshot(), maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.REFACTORING, refactoring_plan_ids=("G-001",)))
        ports.state.schedule_result.project_blocked = True
        ports.state.advance_maintenance_cycle = mock.Mock(side_effect=AssertionError("blocked cycle must remain active"))
        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator._selection._select_planning_work(coordinator._new_run(self.context))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
        ports.state.advance_maintenance_cycle.assert_not_called()

    def test_legacy_post_revalidation_clean_slot_releases_without_new_planner(self):
        with tempfile.TemporaryDirectory() as directory:
            ports = FakePorts(planning_snapshot(phase="PLANNING", active_sha="base", worktree=directory,
                planning_workspace_owned=1, planning_attempt_fingerprint="saved"), maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION, refactoring_plan_ids=("G-001",)))
            ports.git.validate_planning_release = mock.Mock()
            coordinator = PlanningLifecycleCoordinator(ports)
            outcome = coordinator._selection._select_planning_work(coordinator._new_run(self.context))
            self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
            self.assertIsNone(ports.state.snapshot.phase)
            self.assertEqual(len(ports.state.diagnostics), 1)
            self.assertEqual(ports.agent.run_count, 0)

    def test_legacy_post_revalidation_unpublished_edits_preserve_slot_without_new_planner(self):
        with tempfile.TemporaryDirectory() as directory:
            original = planning_snapshot(phase="PLANNING", active_sha="base", worktree=directory,
                planning_workspace_owned=1, planning_attempt_fingerprint="saved")
            ports = FakePorts(original, maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION, refactoring_plan_ids=("G-001",)))
            ports.git.validate_planning_release = mock.Mock()
            ports.git.dirty_files = ["docs/plans/[G-002] Preserved.md"]
            coordinator = PlanningLifecycleCoordinator(ports)
            outcome = coordinator._selection._select_planning_work(coordinator._new_run(self.context))
            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot, original)
            self.assertEqual(ports.agent.run_count, 0)

    def test_outstanding_requirement_work_adds_no_retirement_model_call(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        ports.retirement = SimpleNamespace(
            pending=lambda *_args: (), audit_restorations=lambda *_args: (),
            audit_pending=lambda *_args: (),
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(ports.agent.run_count, 0)
        self.assertIn(outcome.kind, {
            PlanningOutcomeKind.FINGERPRINT_SKIPPED, PlanningOutcomeKind.NO_CHANGE,
        })

    def test_final_requirement_check_uses_one_general_turn_not_quick_check(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        item = FinalInput(
            path="docs/requirements/R.md", token="pinned", source_oid="source-blob", criteria=("Works",),
            related_ids=("G-001",),
        )
        ports.retirement = SimpleNamespace(
            pending=lambda *_args: (item,),
            audit_restorations=lambda *_args: (),
            audit_pending=lambda *_args: (),
            validate_result=lambda *_args: (),
            record=lambda *_args: None,
            record_audit_result=lambda *_args: None,
            stage_restorations=lambda *_args: (),
            stage_moves=lambda *_args, **_kwargs: (),
            receipted_paths=lambda *_args: (),
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(ports.agent.run_count, 1)
        self.assertFalse(ports.agent.roles[0][1])
        self.assertIn("REQUIREMENT_JUDGMENTS", ports.agent.prompts[0])
        self.assertNotEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)

    def test_historical_audit_does_not_call_planner_or_resume_restoration(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        unused = mock.Mock(side_effect=AssertionError("Archived Requirement must not be reassessed"))
        ports.retirement = SimpleNamespace(
            pending=lambda *_args: (), audit_restorations=unused,
            audit_pending=unused,
            validate_result=lambda *_args: (), record=lambda *_args: None,
            record_audit_result=lambda *_args: None,
            stage_restorations=unused, stage_moves=lambda *_args, **_kwargs: (),
            receipted_paths=lambda *_args: (),
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(ports.agent.run_count, 0)
        unused.assert_not_called()
        self.assertIn(outcome.kind, {
            PlanningOutcomeKind.FINGERPRINT_SKIPPED, PlanningOutcomeKind.NO_CHANGE,
        })

    def test_unfulfilled_final_judgment_requires_source_linked_followup_plan(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        source = "docs/requirements/R.md"
        item = FinalInput(
            path=source, token="pinned", source_oid="source-blob", criteria=("Works",),
            related_ids=("G-001",),
        )
        ports.retirement = SimpleNamespace(
            pending=lambda *_args: (item,), audit_restorations=lambda *_args: (),
            audit_pending=lambda *_args: (),
            validate_result=lambda *_args: (SimpleNamespace(
                source_path=source, outcome="unfulfilled",
            ),),
            record=lambda *_args: None, record_audit_result=lambda *_args: None,
            stage_restorations=lambda *_args: (), stage_moves=lambda *_args, **_kwargs: (),
            receipted_paths=lambda *_args: (),
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.agent.run_count, 1)

    def test_planning_workspace_identity_uses_selected_path(self):
        state_dir = Path("/state")
        selected = planning_workspace_path(state_dir, "/repo", "fingerprint")
        validate = mock.Mock()
        validate_planning_workspace_identity(
            ValidatePlanningWorkspaceIdentityEffects(validate),
            state_dir, "/repo", "fingerprint", selected,
        )
        validate.assert_called_once_with(
            state_dir, selected, selected, "planning worktree"
        )

    def test_requirement_archive_does_not_plan_for_code_or_plan_only_changes(self):
        requirement = b"docs/requirements/R.md"
        plan = b"docs/plans/[G-001] Done.md"
        ready = b"docs/plans/[G-002] Ready.md"
        previous = {
            requirement: (b"blob", b"requirement"),
            plan: (b"blob", b"done"),
            ready: (b"blob", b"ready"),
        }
        for change in ("verified", "unverified", "plan-edit", "code", "archive-edit"):
            for has_ready in (False, True):
                with self.subTest(change=change, has_ready=has_ready):
                    current = {
                        b"docs/requirements/archive/R.md": previous[requirement],
                        b"docs/plans/archive/[G-001] Done.md": previous[plan],
                        ready: previous[ready],
                    }
                    if change == "plan-edit":
                        current[ready] = (b"blob", b"edited")
                    elif change == "code":
                        current[b"source.py"] = (b"blob", b"new-code")
                    elif change == "archive-edit":
                        current[b"docs/plans/archive/[G-001] Done.md"] = (b"blob", b"edited")
                    effects = IsRequirementArchiveOnlyChangeEffects(
                        _git_tree_entries=lambda _repo, sha: previous if sha == "old-base" else current,
                        _is_planning_input=loop_manager._is_planning_input,
                    )
                    ports = FakePorts(planning_snapshot(
                        processed_sha="old-base", planning_fingerprint="old-full",
                        specification_fingerprint="old-specification",
                    ))
                    ports.git.fingerprints = {"old-base": "old-full", "base": "archive-full"}
                    ports.git.specification_fingerprints["old-base"] = "old-specification"
                    ports.git.requirement_archive_only = lambda repo, old, new, **kwargs: (
                        _is_requirement_archive_only_change(effects, repo, old, new, **kwargs)
                    )
                    declarations = lambda tree: {
                        path.decode(): tuple(part.decode() for part in value)
                        for path, value in tree.items()
                        if loop_manager.execplan_catalog.is_active_execplan_path(path)
                    }
                    connection = sqlite3.connect(":memory:")
                    self.addCleanup(connection.close)
                    connection.row_factory = sqlite3.Row
                    planning_input_state.record_general(connection, "/repo", "old-base", declarations(previous))
                    if change != "unverified":
                        planning_input_state.advance_verified_delta(
                            connection, "/repo", "base", {plan.decode(): (("blob", "done"), None)},
                        )
                    ports.git.plan_declarations = lambda *_args: declarations(current)
                    ports.state.plan_declarations_match = lambda repo, entries: (
                        planning_input_state.matches(connection, repo, entries)
                    )
                    ports.state.schedule_result = SimpleNamespace(
                        active=None, has_ready=has_ready, has_waiting=False,
                        runnable_ready=object() if has_ready else None, project_blocked=False,
                    )
                    ports.state.activate_result = object() if has_ready else None
                    coordinator = PlanningLifecycleCoordinator(ports)
                    for _ in range(2):
                        outcome = coordinator.run(self.context)
                        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY
                                         if has_ready else PlanningOutcomeKind.FINGERPRINT_SKIPPED)
                        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
                    self.assertEqual(ports.agent.run_count, 0)
                    self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_changed_plan_declaration_activates_ready_without_planning(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        ports.state.plan_declarations_match_result = False
        ports.git.requirement_archive_only = mock.Mock(return_value=True)
        ports.state.schedule_result.has_ready = True
        ports.state.schedule_result.runnable_ready = object()
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(run.selection.kind, PlanningDecisionKind.ACTIVATE_READY)
        self.assertTrue(run.ready_activated_before_planning)
        ports.git.requirement_archive_only.assert_not_called()

    def test_legacy_plan_hash_does_not_force_planning_with_specification_baseline(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base", planning_fingerprint="legacy-v2",
            specification_fingerprint="specification",
        ))
        ports.state.plan_declarations_match_result = False
        ports.git.nonplan_fingerprints["base"] = "legacy-v2"
        ports.git.fingerprints["base"] = "current-v3"
        ports.state.schedule_result.has_ready = True
        ports.state.schedule_result.runnable_ready = object()
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(run.selection.kind, PlanningDecisionKind.ACTIVATE_READY)
        self.assertTrue(run.ready_activated_before_planning)

    def test_selection_step_returns_a_new_immutable_snapshot(self):
        ports = FakePorts(planning_snapshot())
        coordinator = PlanningLifecycleCoordinator(ports)
        initial = snapshot_run(coordinator._new_run(self.context))

        result = coordinator._selection.step("_select_planning_work", initial)

        self.assertIsNone(result.outcome)
        self.assertIsNone(initial.general_fingerprint)
        self.assertEqual(result.snapshot.general_fingerprint, "fingerprint")
        self.assertEqual(
            result.snapshot.selection.kind,
            PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
        )
        with self.assertRaises(FrozenInstanceError):
            result.snapshot.general_fingerprint = "changed"

    def test_new_document_approval_forces_planning_with_unchanged_fingerprints(self):
        ports = FakePorts(planning_snapshot(
            planning_fingerprint="fingerprint",
            specification_fingerprint="specification",
        ))
        ports.state.document_approval_pending = True
        ports.state.maintenance_due_required = True
        ports.state.schedule_result.has_ready = True
        ports.state.schedule_result.runnable_ready = object()
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(replace(
            self.context, registered_only=True, self_repository=True,
        ))

        self.assertIsNone(coordinator._selection._select_planning_work(run))
        self.assertEqual(
            run.selection.kind,
            PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
        )
        self.assertFalse(run.ready_activated_before_planning)
        self.assertEqual(ports.state.maintenance_begin_count, 0)

    def test_real_planner_port_records_resumption_at_agent_start(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        notifications.configure_session(connection, enabled=True)
        repo = "/projects/alpha"
        connection.execute(
            "INSERT INTO projects (repo, base_branch, planning_error) "
            "VALUES (?, 'main', 'bad plan')", (repo,),
        )
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        notifications.dispatch_pending(connection, "token", "chat", adapter=mock.Mock())
        loop_manager.update_project(connection, repo, planning_error=None)
        context = SimpleNamespace(
            repo=repo, provider="codex", state_dir=Path("/state"),
            config_path=Path("/config.ini"),
        )
        port = loop_manager._PlanningAgentPorts(connection, context, None)
        result = SimpleNamespace(
            code=0, message="", session_id=None,
            result_kind=loop_manager.AgentResultKind.SUCCESS,
        )
        with mock.patch.object(loop_manager, "run_agent_with_retries", return_value=result):
            port.run_planner(Path("/worktree"), "prompt", None, lambda _: None)
        self.assertIsNone(connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone())
        def physical_start(*_args, **_kwargs):
            with connection:
                notifications.record_process_start(connection, repo, "planner", "actual-start")
            return result
        with mock.patch.object(loop_manager, "run_agent_with_retries", side_effect=physical_start):
            port.run_planner(Path("/worktree"), "prompt", None, lambda _: None)
        resumed = connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchall()
        self.assertEqual(len(resumed), 1)
        self.assertEqual(json.loads(resumed[0][0])["operation"], "Planner")

    def test_registered_self_flag_only_reaches_maintenance_due_for_self_context(self):
        for self_repository in (False, True):
            ports = FakePorts(planning_snapshot())
            run = PlanningLifecycleCoordinator(ports)._new_run(
                replace(self.context, registered_only=True,
                        self_repository=self_repository)
            )
            PlanningLifecycleCoordinator(ports)._selection._select_planning_work(run)
            self.assertEqual(ports.state.maintenance_due_self_repository,
                             self_repository)

    def test_failed_general_input_without_worktree_survives_repeated_runs(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved failure",
            planning_error_fingerprint="fingerprint",
            planning_retry_count=3,
            planner_session_id="saved-session",
        ))
        before = ports.state.snapshot
        for attempt, base_sha in enumerate(
            ("base", "advanced-base", "advanced-base", "advanced-base")
        ):
            ports.state.schedule_result = SimpleNamespace(
                active=None,
                has_ready=attempt % 2 == 0,
                has_waiting=attempt % 2 == 1,
                runnable_ready=object() if attempt % 2 == 0 else None,
                project_blocked=False,
            )
            outcome = PlanningLifecycleCoordinator(ports).run(
                replace(self.context, base_sha=base_sha)
            )
            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot, before)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.updates, [])

    def test_failed_maintenance_input_ignores_base_only_advance(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base",
            revision=4,
        )
        fingerprint = maintenance_planning_fingerprint(cycle, "fingerprint")
        before = planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint=fingerprint,
            planning_error="saved maintenance failure",
            planning_error_fingerprint=fingerprint,
            planning_workspace_owned=1,
            planner_session_id="saved-session",
        )
        ports = FakePorts(before, cycle)
        ports.git.remote = "advanced-base"
        for attempt in range(4):
            outcome = PlanningLifecycleCoordinator(ports).run(
                replace(self.context, base_sha="advanced-base")
            )
            self.assertEqual(outcome.kind, (PlanningOutcomeKind.OPERATIONAL_ERROR
                if attempt == 0 else PlanningOutcomeKind.REMOTE_BASE_RESTART
                if attempt == 1 else PlanningOutcomeKind.WAITING))
            saved = (planning_snapshot(**{key: value for key, value in json.loads(ports.state.snapshot.planning_preserved_state).items()
                                          if key in PlanningSnapshot.__dataclass_fields__})
                     if ports.state.snapshot.planning_preserved_state else ports.state.snapshot)
            self.assertEqual(saved.active_sha, "advanced-base")
            self.assertEqual(saved.worktree, before.worktree)
            self.assertEqual(ports.state.snapshot.planning_error, before.planning_error)
            self.assertEqual(saved.planner_session_id, "saved-session")
            self.assertEqual(saved.planning_attempt_fingerprint, fingerprint)
            self.assertEqual(ports.state.cycle.cycle_base_sha, "advanced-base")
            self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.git.ownership_checks,
                         [("/repo", fingerprint, Path("/planning"))] * 2)

    def test_maintenance_mode_error_survives_base_and_source_advance(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base", revision=4,
        )
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint=maintenance_planning_fingerprint(cycle, "old-input"),
            planning_workspace_owned=1, planning_error="mode requires user action",
            planning_error_fingerprint="maintenance-cycle:assessment:4",
            planner_session_id="saved-session",
        ), cycle)
        ports.git.remote = "advanced-base"
        ports.git.fingerprints = {"base": "old-input", "advanced-base": "new-input"}

        for attempt in range(3):
            outcome = PlanningLifecycleCoordinator(ports).run(
                replace(self.context, base_sha="advanced-base"),
            )
            self.assertEqual(outcome.kind, (PlanningOutcomeKind.OPERATIONAL_ERROR
                if attempt == 0 else PlanningOutcomeKind.REMOTE_BASE_RESTART
                if attempt == 1 else PlanningOutcomeKind.WAITING))
            saved = (planning_snapshot(**{key: value for key, value in json.loads(ports.state.snapshot.planning_preserved_state).items()
                                          if key in PlanningSnapshot.__dataclass_fields__})
                     if ports.state.snapshot.planning_preserved_state else ports.state.snapshot)
            self.assertEqual(saved.active_sha, "advanced-base")
            self.assertEqual(ports.state.snapshot.planning_error, "mode requires user action")
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.clear_calls, [])

    def test_failed_maintenance_input_restarts_after_related_source_change(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base",
            revision=4,
        )
        fingerprint = maintenance_planning_fingerprint(cycle, "old-input")
        ports = FakePorts(planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint=fingerprint,
            planning_workspace_owned=1,
            planning_error="saved maintenance failure",
            planning_error_fingerprint=fingerprint,
            planner_session_id="saved-session",
        ), cycle)
        ports.git.fingerprints = {"base": "old-input", "advanced-base": "new-input"}
        ports.git.remote = "advanced-base"
        source = "docs/requirements/[R-052] Relevant.md"
        ports.state.execplan_rows = lambda _repo: [{"plan_id": "G-066", "status": "READY"}]
        ports.git.relevant_document_changes = mock.Mock(return_value=(source,))
        ports.validation.prepare_maintenance_context = mock.Mock(return_value=object())
        ports.validation.maintenance_change_context = mock.Mock(return_value="source changed")
        ports.validation.maintenance_prompt = mock.Mock(return_value="correct the saved failure")
        ports.agent.result = PlanningAgentResult(
            1, "same failure", "saved-session", PlanningAgentResultKind.TRANSIENT_FAILURE,
        )

        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="advanced-base")
        )

        self.assertEqual(outcome.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(ports.git.relevant_document_changes.call_args.args[-1], ("G-066",))
        self.assertFalse(ports.git.relevant_document_changes.call_args.kwargs[
            "include_active_requirements"
        ])
        self.assertEqual(ports.git.ownership_checks,
                         [("/repo", fingerprint, Path("/planning"))])
        self.assertEqual(ports.state.snapshot.planning_error, "saved maintenance failure")
        self.assertEqual(ports.state.snapshot.active_sha, "advanced-base")
        self.assertEqual(ports.state.snapshot.worktree, "/planning")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.state.cycle.cycle_base_sha, "advanced-base")
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        failed = ports.state.snapshot
        repeated = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="advanced-base"),
        )
        self.assertEqual(repeated.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        preserved = json.loads(ports.state.snapshot.planning_preserved_state)
        for field in ("active_sha", "worktree", "planner_session_id", "planning_result",
                      "planning_error", "planning_error_fingerprint", "planning_validation_error"):
            self.assertEqual(preserved[field], getattr(failed, field))
        waiting = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="advanced-base"),
        )
        self.assertEqual(waiting.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.clear_calls, [])

    def test_specification_change_plans_once_before_runnable_ready(self):
        ports = FakePorts(
            planning_snapshot(
                processed_sha="old-base",
                specification_fingerprint="old-specification",
                planning_fingerprint="old-full",
            )
        )
        ports.git.fingerprint = "new-full"
        ports.git.specification_fingerprints["old-base"] = "old-specification"
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint,
            "specification",
        )
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "new-full")

        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
        second = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)

    def _code_check_ports(self):
        ports = FakePorts(
            planning_snapshot(
                processed_sha="old-base",
                specification_fingerprint="specification",
                planning_fingerprint="old-full",
            )
        )
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.state.has_general_baseline = True
        text = plan_text("G-001", "Ready")
        plan = ReadyPlanSnapshot(
            plan_id="G-001", path="docs/plans/[G-001] Ready.md", title="Ready",
            sources=("docs/PRODUCT.md",), depends_on=(), complexity="low",
            risk="low", priority="normal", content_hash=hashlib.sha256(text.encode()).hexdigest(),
            plan_sha="base", text=text, current_complexity="low",
            current_risk="low", current_metadata_recorded=True,
        )
        ports.validation.snapshot = MutableReadySnapshot(
            repo="/repo", active_sha="base", plans=(plan,),
            token=mutable_snapshot_token("/repo", "base", (plan,)),
        )
        ports.agent.result = PlanningAgentResult(
            0, 'PLANNER_CHECK_RESULT: {"outcome":"no_change","evidence":"code paths reviewed","reason":"plans remain valid","plan_ids":[]}',
            "check-session", PlanningAgentResultKind.SUCCESS,
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()
        return ports

    def _interrupted_accepted_ports(self):
        ports = self._code_check_ports()
        from loop_manager_internal.storage.planner_check_state import target_fingerprint
        ports.state.check_record = {
            "stage": "accepted", "input_fingerprint": "new-full",
            "target_fingerprint": target_fingerprint(ports.validation.snapshot),
            "specification_fingerprint": "specification", "base_sha": "base",
            "comparison_sha": "old-base",
        }
        ports.state.snapshot = replace(
            ports.state.snapshot, phase="PLANNING", active_sha="base",
            planning_attempt_fingerprint="new-full", planning_workspace_owned=1,
            worktree="/state/worktrees/planning",
        )
        return ports

    def test_interrupted_accepted_reselects_changed_remote_input_or_target(self):
        for change in ("code", "document", "plan", "guidance", "owner"):
            with self.subTest(change=change):
                ports = self._interrupted_accepted_ports()
                ports.git.remote = "new-base"
                ports.git.fingerprints["new-base"] = "new-full"
                if change == "code":
                    ports.git.fingerprints["new-base"] = "newer-full"
                elif change == "document":
                    ports.git.specification_fingerprints["new-base"] = "changed-specification"
                elif change == "plan":
                    ports.state.plan_declarations_match_result = False
                elif change == "guidance":
                    ports.git.guidance_changed = True
                else:
                    ports.validation.snapshot = MutableReadySnapshot(
                        "/repo", "new-base", (),
                        mutable_snapshot_token("/repo", "new-base", ()),
                    )

                outcome = PlanningLifecycleCoordinator(ports).run(self.context)

                self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
                self.assertIsNone(ports.state.snapshot.phase)
                self.assertEqual(ports.agent.run_count, 0)
                self.assertEqual(ports.state.check_record["stage"], "accepted")
                if change == "code":
                    resumed = PlanningLifecycleCoordinator(ports).run(
                        replace(self.context, base_sha="new-base"),
                    )
                    self.assertEqual(resumed.kind, PlanningOutcomeKind.EXECUTION_READY)
                    self.assertEqual(ports.agent.roles, [])
                    self.assertEqual(
                        ports.state.check_record["input_fingerprint"], "new-full",
                    )

    def test_interrupted_accepted_reuses_identical_remote_revision(self):
        ports = self._interrupted_accepted_ports()
        ports.git.remote = "same-content-base"
        ports.git.fingerprints["same-content-base"] = "new-full"

        first = PlanningLifecycleCoordinator(ports).run(self.context)
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="same-content-base"),
        )

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_no_change_planner_result_advances_full_baseline_before_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="specification",
            planning_fingerprint="old-full",
        ))
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.validation.result = None
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")
        self.assertEqual(ports.state.snapshot.processed_sha, "old-base")

    def test_source_change_at_direct_activation_restarts_before_worker(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base",
            specification_fingerprint="specification",
            planning_fingerprint="fingerprint",
        ))
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()
        ports.git.remote = "new-base"
        ports.git.fingerprints["new-base"] = "new-full"

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertIsNone(ports.state.snapshot.phase)

        ports.git.head = "new-base"
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )
        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)

    def test_new_specification_at_direct_activation_restarts_before_worker(self):
        ports = FakePorts(
            planning_snapshot(
                specification_fingerprint="specification",
                planning_fingerprint="fingerprint",
            )
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()
        ports.validation.result = None
        ports.git.remote = "new-base"
        ports.git.specification_fingerprints["new-base"] = "new-specification"
        ports.git.fingerprints["new-base"] = "new-full"

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertIsNone(ports.state.snapshot.phase)
        self.assertEqual(ports.agent.run_count, 0)

        ports.git.head = "new-base"
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint,
            "new-specification",
        )

    def test_new_specification_after_planner_success_restarts_before_worker(self):
        ports = FakePorts(
            planning_snapshot(
                specification_fingerprint="old-specification",
                planning_fingerprint="old-full",
            )
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()
        ports.validation.result = None
        ports.git.specification_fingerprints["new-base"] = "new-specification"
        ports.git.fingerprints["new-base"] = "new-full"

        ports.git.relevant_document_changes = mock.Mock(return_value=("docs/PRODUCT.md",))
        original_planner = ports.agent.run_planner

        def advance_after_planner(*args, **kwargs):
            result = original_planner(*args, **kwargs)
            ports.git.remote = "new-base"
            return result

        ports.agent.run_planner = advance_after_planner
        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
        self.assertEqual(ports.state.snapshot.phase, "PLANNING")
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint, "old-specification"
        )
        self.assertEqual(ports.state.snapshot.planner_session_id, "session-1")
        self.assertTrue(ports.state.snapshot.planning_result_completed)
        self.assertEqual(ports.state.clear_calls, [])

        ports.git.head = "new-base"
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 2)
        self.assertEqual(ports.agent.resumed_session, "session-1")
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint,
            "new-specification",
        )

    def test_remote_input_identity_controls_completed_result_reuse_after_pause(self):
        for input_changed in (False, True):
            with self.subTest(input_changed=input_changed):
                ports = FakePorts(planning_snapshot(planning_fingerprint="old-input"))
                ports.validation.result = None
                ports.git.fingerprints["new-base"] = (
                    "new-input" if input_changed else ports.git.fingerprint
                )
                original_planner = ports.agent.run_planner

                def advance_after_planner(*args, **kwargs):
                    result = original_planner(*args, **kwargs)
                    ports.git.remote = "new-base"
                    return result

                ports.agent.run_planner = advance_after_planner
                coordinator = PlanningLifecycleCoordinator(ports)
                first = coordinator.run(self.context)
                preserved = ports.state.snapshot

                self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
                self.assertEqual(preserved.phase, "PLANNING")
                self.assertTrue(preserved.planning_result_completed)
                self.assertEqual(ports.state.clear_calls, [])
                self.assertEqual(preserved.planning_fingerprint, "old-input")
                self.assertEqual(ports.agent.run_count, 1)
                second = coordinator.run(replace(self.context, base_sha="new-base"))

                self.assertEqual(second.kind, PlanningOutcomeKind.NO_CHANGE)
                self.assertEqual(ports.agent.run_count, 2 if input_changed else 1)
                if input_changed:
                    self.assertEqual(ports.agent.resumed_session, "session-1")
                self.assertEqual(ports.state.snapshot.processed_sha, "new-base")
                self.assertEqual(ports.state.snapshot.planning_fingerprint,
                                 ports.git.fingerprints["new-base"])

    def test_failed_input_correction_resumes_after_base_adoption_before_agent(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="old-input", planning_workspace_owned=1,
            planning_error="saved failure", planning_error_fingerprint="old-input",
            planner_session_id="saved-session", planning_retry_count=3,
            planning_validation_error="saved failure",
        ))
        ports.git.fingerprints = {"base": "old-input", "new-base": "new-input"}
        ports.git.remote = "new-base"
        ports.agent.result = PlanningAgentResult(
            1, "same failure", "saved-session", PlanningAgentResultKind.TRANSIENT_FAILURE,
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        prepared = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))

        self.assertIsNone(prepared)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.active_sha, "new-base")
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint, "new-input")
        self.assertTrue(ports.state.snapshot.planning_result_pending_validation)
        self.assertEqual(ports.state.snapshot.planning_retry_count, 0)
        resumed = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base"),
        )

        self.assertEqual(resumed.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.clear_calls, [])

    def test_related_document_correction_survives_interruption_before_planner(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="old-input", planning_workspace_owned=1,
            planning_error="saved failure", planning_error_fingerprint="old-input",
            planner_session_id="saved-session", planning_retry_count=3,
            planning_validation_error="saved failure", planning_result_completed=True,
            planning_result="saved result",
        ))
        ports.git.fingerprints = {"base": "old-input", "new-base": "new-input"}
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(return_value=("docs/PRODUCT.md",))
        ports.agent.result = PlanningAgentResult(
            1, "same failure", "saved-session", PlanningAgentResultKind.TRANSIENT_FAILURE,
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        self.assertIsNone(coordinator._workspace._prepare_workspace(coordinator._new_run(self.context)))
        preserved = ports.state.snapshot

        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(preserved.active_sha, "new-base")
        self.assertIsNone(preserved.planning_result)
        self.assertEqual(ports.state.diagnostics[-1].planning_result, "saved result")
        self.assertEqual(ports.state.diagnostics[-1].planning_error, "saved failure")
        self.assertEqual(ports.state.diagnostics[-1].planning_validation_error, "saved failure")
        self.assertIn("docs/PRODUCT.md", preserved.planning_validation_error)
        self.assertTrue(preserved.planning_result_pending_validation)
        self.assertFalse(preserved.planning_result_completed)
        resumed = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base"),
        )

        self.assertEqual(resumed.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_error, preserved.planning_error)
        self.assertEqual(ports.state.clear_calls, [])

    def test_no_runnable_fallback_plans_once_then_stably_waits(self):
        ports = FakePorts(
            planning_snapshot(
                specification_fingerprint="specification",
                planning_fingerprint="old-full",
            )
        )
        ports.git.fingerprint = "new-full"
        ports.state.schedule_result = SimpleNamespace(
            active=None,
            has_ready=True,
            has_waiting=False,
            runnable_ready=None,
            project_blocked=False,
        )

        first = PlanningLifecycleCoordinator(ports).run(self.context)
        second = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(second.kind, PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.run_count, 0)

    def test_archive_only_change_keeps_ready_and_success_baseline(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="old-specification",
            planning_fingerprint="old-full",
        ))
        ports.git.fingerprint = "archive-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.git.requirement_archive_only = mock.Mock(return_value=True)
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")

    def test_plan_only_remote_change_reselects_ready_from_latest_catalog(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="base",
            specification_fingerprint="specification",
            planning_fingerprint="fingerprint",
        ))
        old_ready = SimpleNamespace(plan_id="G-001", priority="critical")
        new_ready = SimpleNamespace(plan_id="G-002", priority="critical")
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=old_ready, project_blocked=False,
        )
        ports.state.activate_result = object()
        ports.git.remote = "new-base"
        ports.git.fingerprints["new-base"] = "fingerprint"
        activated = []
        original_activate = ports.state.activate_ready

        def activate_selected(repo, base_sha):
            activated.append(ports.state.schedule_result.runnable_ready.plan_id)
            return original_activate(repo, base_sha)

        ports.state.activate_ready = activate_selected

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(activated, [])
        self.assertIsNone(ports.state.snapshot.phase)
        self.assertEqual(ports.agent.run_count, 0)

        ports.git.head = "new-base"
        ports.state.schedule_result.runnable_ready = new_ready
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(activated, ["G-002"])
        self.assertEqual(ports.agent.run_count, 0)

class PreservedPlanningSelectionTest(PlanningCase):
    def failed_ports(self, **values):
        defaults = dict(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_workspace_owned=1, planning_attempt_fingerprint="fingerprint",
            planner_session_id="saved-session", planning_result="unpublished-result",
            planning_validation_error="failed validation", planning_retry_count=3,
            planning_error="saved failure", planning_error_fingerprint="fingerprint",
        )
        defaults.update(values)
        return FakePorts(planning_snapshot(**defaults))

    def test_same_input_releases_then_runs_independent_ready_without_planner(self):
        ports = self.failed_ports()
        original = ports.state.snapshot
        ports.state.schedule_result.runnable_ready = object()
        ports.state.schedule_result.has_ready = True
        ports.state.activate_result = object()
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                         PlanningOutcomeKind.REMOTE_BASE_RESTART)
        saved = ports.state.snapshot.planning_preserved_state
        self.assertEqual(json.loads(saved)["planner_session_id"], original.planner_session_id)
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertTrue(outcome.newly_activated)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_preserved_state, saved)
        self.assertEqual(ports.state.snapshot.planning_error, original.planning_error)
        self.assertFalse(any(args[:1] == ("push",) for _, args, _ in ports.git.calls))

    def test_no_independent_work_waits_without_completion_or_retirement(self):
        ports = self.failed_ports()
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                         PlanningOutcomeKind.REMOTE_BASE_RESTART)
        ports.retirement = SimpleNamespace(pending=mock.Mock())
        for _ in range(3):
            outcome = PlanningLifecycleCoordinator(ports).run(self.context)
            self.assertEqual(outcome.kind, PlanningOutcomeKind.WAITING)
        ports.retirement.pending.assert_not_called()
        self.assertEqual(ports.agent.run_count, 0)

    def test_release_refuses_uncertain_ownership_operations_scope_and_publication(self):
        for reason in ("ownership", "operation", "agent", "scope", "intent", "maintenance-intent"):
            with self.subTest(reason=reason):
                ports = self.failed_ports()
                if reason == "ownership":
                    ports.git.ownership_error = PlanningWorkspaceOwnershipError("unknown owner")
                elif reason == "operation":
                    ports.git.validate_planning_release = mock.Mock(
                        side_effect=PlanningWorkspaceOwnershipError("unsealed Git operation"))
                elif reason == "agent":
                    ports.state.planning_release_pending = lambda _repo: True
                elif reason == "scope":
                    ports.state.snapshot = replace(ports.state.snapshot, agent_snapshot="scope")
                    ports.agent.validate_saved_scope = lambda *_args: PlanningScopeResult("shared ref changed")
                elif reason == "intent":
                    ports.state.general_intent = object()
                else:
                    ports.state.cycle = replace(ports.state.cycle, publication_stage="prepared")
                original = ports.state.snapshot
                run = PlanningLifecycleCoordinator(ports)._new_run(self.context)
                outcome = PlanningLifecycleCoordinator(ports)._selection._gate_preserved_error(run)
                self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                self.assertEqual(ports.state.snapshot, original)
                self.assertEqual(ports.agent.run_count, 0)

    def test_conflict_diagnostics_dispatch_before_release_refusal_and_keep_context(self):
        ports = self.failed_ports()
        original = ports.state.snapshot
        order = []
        preserve = ports.state.preserve_diagnostics
        def capture(repo, row):
            order.append('preservation')
            preserve(repo, row)
        def refuse(*args):
            order.append('release')
            raise PlanningWorkspaceOwnershipError('unresolved planning conflict')
        ports.state.preserve_diagnostics = capture
        ports.git.validate_planning_release = refuse
        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator._selection._suspend_failed_planning(coordinator._new_run(self.context))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(order, ['preservation', 'release'])
        self.assertEqual(ports.state.diagnostics, [original])
        self.assertEqual(ports.state.snapshot, original)
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)

    def test_active_pr_correction_keeps_slot_even_when_failed_input_changes(self):
        ports = self.failed_ports()
        PlanningLifecycleCoordinator(ports).run(self.context)
        saved = ports.state.snapshot.planning_preserved_state
        ports.state.snapshot = replace(ports.state.snapshot, phase="WORKING")
        ports.state.schedule_result.active = {'plan_id': 'G-001', 'pr_url': 'https://example/pr/1', 'pr_conflict_recovery': 1}
        ports.git.fingerprint = "changed-input"
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertFalse(outcome.newly_activated)
        self.assertEqual(ports.state.snapshot.planning_preserved_state, saved)
        self.assertEqual(ports.agent.run_count, 0)

    def test_changed_input_restores_same_attempt_before_revalidation(self):
        ports = self.failed_ports()
        original = ports.state.snapshot
        PlanningLifecycleCoordinator(ports).run(self.context)
        ports.git.fingerprint = "changed-input"
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.state.snapshot, original)
        self.assertEqual(ports.agent.run_count, 0)

    def test_restored_validation_error_clears_only_after_saved_session_revalidation(self):
        ports = self.failed_ports(planning_error="failed validation", planning_error_origin="validation")
        PlanningLifecycleCoordinator(ports).run(self.context)
        ports.git.remote = "new-base"
        ports.git.fingerprints["new-base"] = "changed-input"
        ports.git.relevant_document_changes = mock.Mock(return_value=("docs/PRODUCT.md",))
        ports.validation.result = None
        updated = replace(self.context, base_sha="new-base")
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(updated).kind,
                         PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.state.snapshot.planning_error, "failed validation")
        self.assertEqual(ports.agent.run_count, 0)
        outcome = PlanningLifecycleCoordinator(ports).run(updated)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertIsNone(ports.state.snapshot.planning_preserved_state)

    def test_remote_change_at_activation_keeps_preserved_result(self):
        ports = self.failed_ports()
        PlanningLifecycleCoordinator(ports).run(self.context)
        saved = ports.state.snapshot
        ports.state.schedule_result.runnable_ready = object()
        ports.git.remote = "new-base"
        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.state.snapshot, saved)
        self.assertEqual(ports.agent.run_count, 0)

class FailedSpecificationPlanningTest(PlanningCase):
    def test_no_check_specification_failure_holds_affected_ready_and_runs_independent(self):
        from tests.support.database import AutomationCatalogFixture
        from tests.support.catalog import execplan_header
        from loop_manager_internal.storage import runtime_state, planner_check_state
        from loop_manager_internal.planning.planning_document_relevance import relevant_document_changes
        from loop_manager_internal.planning.planning_contracts import MAX_PLANNER_VALIDATION_RETRIES
        connection = AutomationCatalogFixture._connection(self, '/repo')
        for plan_id, status, dependency in (
                ('G-001', 'READY', ()), ('G-002', 'READY', ()),
                ('G-003', 'MERGED', ()), ('G-004', 'MERGED', ('G-003',)),
                ('G-005', 'READY', ('G-004',))):
            AutomationCatalogFixture._insert_plan(
                self, connection, plan_id, status, repo='/repo', depends_on=dependency,
                priority='low' if plan_id == 'G-002' else 'critical',
            )
        source = 'docs/requirements/A.md'
        connection.execute("UPDATE exec_plans SET sources = ? WHERE plan_id IN ('G-001', 'G-003')",
                           (json.dumps([source]),))
        connection.execute("UPDATE exec_plans SET sources = ? WHERE plan_id = 'G-002'",
                           (json.dumps(['docs/requirements/B.md']),))
        ports = FakePorts(planning_snapshot(
            processed_sha='old-base', specification_fingerprint='old-spec',
            planning_fingerprint='old-input',
        ))
        ports.state.execplan_rows = lambda _repo: connection.execute('SELECT * FROM exec_plans').fetchall()
        ports.git.fingerprint = 'new-input'
        ports.git.fingerprints['old-base'] = 'old-input'
        ports.git.specification_fingerprints['base'] = 'new-spec'
        documents = {
            plan['path']: execplan_header(
                plan['plan_id'], plan['title'], sources=tuple(json.loads(plan['sources'])),
                depends_on=tuple(json.loads(plan['depends_on'])),
            ) for plan in ports.state.execplan_rows('/repo')
        }
        documents['docs/requirements/B.md'] = 'unchanged requirement'
        documents['docs/PRODUCT.md'] = 'unchanged product'
        original_run = ports.git.run
        def remote_document_git(repo, *args, **kwargs):
            if args[:3] == ('diff', '--name-only', '-z'):
                return SimpleNamespace(stdout=source + '\0', returncode=0)
            if args[:4] == ('ls-tree', '-r', '-z', '--name-only'):
                return SimpleNamespace(stdout='\0'.join(path for path in documents if path.startswith('docs/plans/')), returncode=0)
            if args[0] == 'show' or args[:2] == ('rev-parse', '--verify') and ':' in args[-1]:
                revision, path = args[-1].split(':', 1)
                content = ('old requirement' if revision == 'old-base' else 'changed requirement') if path == source else documents.get(path)
                if content is None:
                    return SimpleNamespace(stdout='', returncode=1)
                return SimpleNamespace(stdout=content if args[0] == 'show' else hashlib.sha256(content.encode()).hexdigest(), returncode=0)
            return original_run(repo, *args, **kwargs)
        ports.git.run = remote_document_git
        ports.git.relevant_document_changes = mock.Mock(side_effect=lambda *args, **kwargs:
            relevant_document_changes(remote_document_git, self.context.repo_path, *args, **kwargs))
        ports.validation.general_error = 'specification correction invalid'
        for _ in range(MAX_PLANNER_VALIDATION_RETRIES + 1):
            self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                             PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertIsNone(ports.state.check_record)
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                         PlanningOutcomeKind.REMOTE_BASE_RESTART)
        preserved = ports.state.snapshot.planning_preserved_state
        self.assertEqual(ports.state.snapshot.preserved_unconfirmed_plan_ids, ('G-001', 'G-003'))
        for call in ports.git.relevant_document_changes.call_args_list:
            self.assertEqual(call.args[1:3], ('old-base', 'base'))
            self.assertFalse(call.kwargs['include_active_requirements'])
        connection.execute('UPDATE projects SET planning_preserved_state = ?, planning_error = ? WHERE repo = ?',
                           (preserved, ports.state.snapshot.planning_error, '/repo'))
        self.assertIsNone(planner_check_state.get(connection, '/repo'))
        ports.state.schedule = lambda repo: loop_manager.catalog_schedule_decision(connection, repo)
        runtime = runtime_state.RuntimeStateRepository(connection)
        def activate(repo, base):
            selected = loop_manager.select_ready_execplan(connection, repo)
            runtime.activate(repo, selected['plan_id'], activation_sha=base,
                             branch='independent', worktree='/independent')
            return selected
        ports.state.activate_ready = activate
        calls = ports.agent.run_count
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                         PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(runtime.active_row('/repo')['plan_id'], 'G-002')
        connection.execute("UPDATE exec_plans SET status = 'PR_OPEN' WHERE plan_id = 'G-002'")
        runtime.clear('/repo')
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                         PlanningOutcomeKind.WAITING)
        self.assertEqual(ports.agent.run_count, calls)
        self.assertEqual(runtime.planning('/repo').planning_preserved_state, preserved)
        self.assertEqual(ports.state.snapshot.planning_preserved_state, preserved)

    def test_unclassified_change_or_missing_baseline_does_not_certify_independence(self):
        for reason in ('code', 'baseline', 'unreadable'):
            with self.subTest(reason=reason):
                ports = FakePorts(planning_snapshot(
                    phase='PLANNING', active_sha='base', worktree='/planning',
                    processed_sha=None if reason == 'baseline' else 'old-base',
                    planning_workspace_owned=1, planning_error='invalid result',
                    planning_attempt_fingerprint='fingerprint', planning_error_fingerprint='fingerprint',
                ))
                ports.state.execplan_rows = lambda _repo: [{'plan_id': 'G-001'}, {'plan_id': 'G-002'}]
                original_run = ports.git.run
                def run(repo, *args, **kwargs):
                    if args[:3] == ('diff', '--name-only', '-z'):
                        if reason == 'unreadable':
                            raise subprocess.CalledProcessError(1, ['git', 'diff'])
                        return SimpleNamespace(stdout='src/app.py\0', returncode=0)
                    return original_run(repo, *args, **kwargs)
                ports.git.run = run
                self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind,
                                 PlanningOutcomeKind.REMOTE_BASE_RESTART)
                self.assertEqual(ports.state.snapshot.preserved_unconfirmed_plan_ids, ('G-001', 'G-002'))
                self.assertEqual(ports.agent.run_count, 0)

@isolated_git_test
class PlanningReleaseGitTest(unittest.TestCase):
    def test_checkout_operation_metadata_is_preserved_and_shared_journal_still_blocks(self):
        from tests.support.git import git_command
        from loop_manager_internal.planning.git_ports import _PlanningGitPorts
        from loop_manager_internal.git import manager_git_journal
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            root.mkdir()
            git_command(root, 'init', '--initial-branch=main')
            git_command(root, 'config', 'user.name', 'Release Test')
            git_command(root, 'config', 'user.email', 'release@example.com')
            (root / 'tracked').write_text('original\n', encoding='utf-8')
            git_command(root, 'add', 'tracked')
            git_command(root, 'commit', '-m', 'Initial')
            worktree = Path(directory) / 'planning'
            git_command(root, 'worktree', 'add', '--detach', str(worktree), 'HEAD')
            state = Path(directory) / 'state'
            effects = SimpleNamespace(git=loop_manager.git, _nul_git_paths=mock.Mock(),
                                      _git_operation_entries=loop_manager._git_operation_entries)
            port = _PlanningGitPorts(effects, None, root, state)
            head = git_command(root, 'rev-parse', 'HEAD')
            for operation in ('rebase-merge/head-name', 'BISECT_LOG'):
                metadata = root / git_command(root, 'rev-parse', '--git-path', operation)
                metadata.parent.mkdir(exist_ok=True)
                metadata.write_bytes(b'user operation\n')
                self.assertTrue(loop_manager._git_operation_entries(root))
                port.validate_planning_release(str(root), worktree)
                self.assertEqual(metadata.read_bytes(), b'user operation\n')
                self.assertEqual(git_command(root, 'rev-parse', 'HEAD'), head)
                self.assertEqual(git_command(root, 'status', '--porcelain'), '')
            manager_git_journal.begin_rebase(
                state, owner='plan:G-165', repo=str(root), target=str(worktree),
                before={'head': head}, expected={'run_id': 'saved-run', 'branch': 'planning'},
            )
            with self.assertRaisesRegex(PlanningWorkspaceOwnershipError, '미확정 Manager Git'):
                port.validate_planning_release(str(root), worktree)
            self.assertEqual(metadata.read_bytes(), b'user operation\n')

    def test_release_checks_managed_operations_unmerged_index_and_workspace(self):
        from loop_manager_internal.planning.git_ports import _PlanningGitPorts
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / 'planning'
            worktree.mkdir()
            for failure in ('none', 'worktree-operation', 'local-rebase', 'local-bisect', 'unmerged', 'missing', 'shared-write'):
                with self.subTest(failure=failure):
                    effects = SimpleNamespace(
                        git=mock.Mock(side_effect=lambda _repo, *args, **_kwargs: SimpleNamespace(
                            stdout='conflict' if args == ('ls-files', '--unmerged') and failure == 'unmerged' else '')),
                        _nul_git_paths=mock.Mock(),
                        _git_operation_entries=lambda path: {'operation': 'active'} if (
                            failure == 'worktree-operation' and path == worktree
                            or failure in ('local-rebase', 'local-bisect') and path == root) else {},
                    )
                    port = _PlanningGitPorts(effects, None, root, root / 'state')
                    target = root / 'missing' if failure == 'missing' else worktree
                    with mock.patch('loop_manager_internal.git.manager_git_journal.has_pending_project_mutation',
                                    return_value=failure == 'shared-write'):
                        if failure in ('none', 'local-rebase', 'local-bisect'):
                            port.validate_planning_release('/repo', target)
                        else:
                            with self.assertRaises(PlanningWorkspaceOwnershipError):
                                port.validate_planning_release('/repo', target)

    def test_release_pending_agent_is_read_without_modifying_invocations(self):
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        from loop_manager_internal.storage import agent_invocation_state
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        agent_invocation_state.ensure_schema(connection)
        connection.execute("""INSERT INTO agent_invocations (
            invocation_id, repo, role, provider, model, effort, transition, attempt, started_at
        ) VALUES ('call', '/repo', 'planner', 'codex', 'model', 'high', 'start', 1, 'time')""")
        port = _PlanningStatePorts(None, connection, None, None, Path('/state'))
        self.assertTrue(port.planning_release_pending('/repo'))
        self.assertFalse(port.planning_release_pending('/other'))
        connection.execute("UPDATE agent_invocations SET finished_at = 'time'")
        self.assertFalse(port.planning_release_pending('/repo'))

class SpecificationReviewSelectionTest(PlanningCase):
    def test_requirement_only_review_preserves_general_comparison_receipt(self):
        ports = self.review_ports(planning_snapshot(processed_sha='base',
            planning_fingerprint='fingerprint', specification_fingerprint='specification'))
        self.record(ports, revision='base')
        ports.state.schedule_result.has_ready = True
        ports.state.schedule_result.runnable_ready = object()
        item = FinalInput('docs/requirements/R.md', 'input', 'blob', ('Works',), ('G-001',))
        ports.retirement = SimpleNamespace(pending=lambda *_: (item,),
            validate_result=lambda *_: (), record=lambda *_: None,
            record_audit_result=lambda *_: None, stage_restorations=lambda *_: (),
            stage_moves=lambda *_, **__: (), receipted_paths=lambda *_: ())
        PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertIn('해당 Acceptance Criteria', ports.agent.prompts[0])
        self.assertNotIn('활성 Requirement 전문을 읽고', ports.agent.prompts[0])
        self.assertEqual(ports.state.planning_review('/repo', 'requirement')['purpose'], 'requirement')
        self.assertEqual(ports.state.planning_review('/repo', 'general')['purpose'], 'delta')

    """Exercise real receipt storage through the existing lifecycle test ports."""

    def review_ports(self, snapshot=None):
        from loop_manager_internal.storage import planning_review_state as reviews
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        ports = FakePorts(snapshot or planning_snapshot())
        ports.review_connection = connection
        ports.state.planning_review = lambda repo, slot: reviews.current_final(connection, repo) if slot == 'final' else reviews.get(connection, repo, slot)
        ports.state.review_work_cycle = lambda _repo: f'cycle-{ports.state.cycle.revision}'
        ports.state.final_review_candidate = lambda _repo, _interval, **_kwargs: not (
            ports.state.schedule_result.active or ports.state.schedule_result.has_ready
            or ports.state.schedule_result.has_waiting or ports.state.schedule_result.project_blocked
            or ports.state.snapshot.planning_error or ports.state.snapshot.planning_preserved_state
            or ports.state.cycle.phase is not maintenance_state.MaintenancePhase.IDLE
            or ports.state.general_intent or ports.state.maintenance_due_required)
        def begin(repo, **values):
            with connection:
                reviews.begin(connection, repo, **values)
        def validate(*args):
            with connection:
                reviews.validate(connection, *args)
        ports.state.begin_planning_review = begin
        ports.state.validate_planning_review = validate
        ports.state.confirm_planning_review = lambda *args: reviews.confirm(connection, *args)
        old_transaction = ports.state.transaction
        @contextmanager
        def transaction():
            with old_transaction(), connection:
                yield
        ports.state.transaction = transaction
        ports.validation.result = None
        original_git = ports.git.run
        def run(repo, *args, **kwargs):
            if args[:3] == ('ls-tree', '-r', '-z'):
                paths = args[args.index('--') + 1:]
                return SimpleNamespace(stdout=''.join(f'100644 blob oid\t{path}\0' for path in paths if path in self.evidence), returncode=0)
            if args[:4] == ('ls-tree', '-r', '--name-only', '-z'):
                return SimpleNamespace(stdout='docs/PRODUCT.md\0docs/ARCHITECTURE.md\0', returncode=0)
            return original_git(repo, *args, **kwargs)
        ports.git.run = run
        original_agent = ports.agent.run_planner
        def agent(*args, **kwargs):
            attempt = ports.state.planning_review('/repo', 'attempt')
            if attempt is not None and attempt['purpose'] == 'final':
                ports.agent.result = replace(ports.agent.result, message=self.final_result(attempt))
            return original_agent(*args, **kwargs)
        ports.agent.run_planner = agent
        return ports

    evidence = ('docs/PRODUCT.md', 'docs/ARCHITECTURE.md', 'src/app.py', 'tests/test_app.py')

    def final_result(self, attempt, **changes):
        result = {key: attempt[key] for key in (
            'revision', 'input_fingerprint', 'specification_fingerprint', 'work_cycle')}
        result.update(outcome='fulfilled', evidence=list(self.evidence), reason='All rules have implementation and unit coverage')
        result.update(changes)
        return 'FINAL_SPECIFICATION_REVIEW: ' + json.dumps(result)

    def record(self, ports, purpose='delta', outcome='reviewed', revision='old-base', result='reviewed changes'):
        from loop_manager_internal.storage import planning_review_state as reviews
        reviews.begin(ports.review_connection, '/repo', purpose=purpose, revision=revision,
            input_fingerprint=ports.git.input_fingerprint(Path('/repo'), revision),
            specification_fingerprint=ports.git.specification_fingerprint(Path('/repo'), revision),
            work_cycle=ports.state.review_work_cycle('/repo'), comparison_revision=None)
        if purpose == 'final':
            result = self.final_result(ports.state.planning_review('/repo', 'attempt'), outcome=outcome)
        reviews.validate(ports.review_connection, '/repo', revision, result, outcome)
        reviews.confirm(ports.review_connection, '/repo', revision, revision)
        ports.review_connection.commit()

    def test_initial_then_final_then_restart_reuses_receipt(self):
        ports = self.review_ports()
        coordinator = PlanningLifecycleCoordinator(ports)
        self.assertEqual(coordinator.run(self.context).kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertIn('활성 Requirement 전문', ports.agent.prompts[0])
        self.assertEqual(ports.state.planning_review('/repo', 'general')['purpose'], 'full')
        self.assertIsNone(ports.state.planning_review('/repo', 'final'))
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertIn('최종 전문 확인', ports.agent.prompts[1])
        self.assertEqual(ports.state.planning_review('/repo', 'final')['outcome'], 'fulfilled')
        for _ in range(2):
            self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind, PlanningOutcomeKind.FINGERPRINT_SKIPPED)
        self.assertEqual(ports.agent.run_count, 2)
        ports.state.cycle = replace(ports.state.cycle, revision=1)
        PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(ports.agent.run_count, 3)

    def test_merged_plan_archive_reuses_final_receipt_without_planner(self):
        from loop_manager_internal.planning.git_ports import _PlanningGitPorts
        plan = b'docs/plans/[G-001] Done.md'
        archived = b'docs/plans/archive/[G-001] Done.md'
        previous = {plan: (b'blob', b'plan')}
        for change in ('verified', 'unverified', 'archive-edit', 'code', 'specification', 'cycle'):
            with self.subTest(change=change):
                current = {archived: previous[plan]}
                if change == 'archive-edit':
                    current[archived] = (b'blob', b'edited')
                elif change == 'code':
                    current[b'src/app.py'] = (b'blob', b'new-code')
                elif change == 'specification':
                    current[b'docs/PRODUCT.md'] = (b'blob', b'new-specification')
                ports = self.review_ports(planning_snapshot(
                    processed_sha='old-base', planning_fingerprint='old-full',
                    specification_fingerprint='specification',
                ))
                ports.git.fingerprints = {'old-base': 'old-full', 'base': 'archived-full'}
                ports.git.nonplan_fingerprints = {'old-base': 'same-code', 'base': 'same-code'}
                ports.git.specification_fingerprints['old-base'] = 'specification'
                effects = IsRequirementArchiveOnlyChangeEffects(
                    _git_tree_entries=lambda _repo, sha: previous if sha == 'old-base' else current,
                    _is_planning_input=loop_manager._is_planning_input,
                )
                # Exercise the real Git port's forwarding and the archive classifier.
                git_port = _PlanningGitPorts(SimpleNamespace(
                    _is_requirement_archive_only_change=lambda *args, **kwargs: (
                        _is_requirement_archive_only_change(effects, *args, **kwargs)),
                    git=mock.Mock(), _nul_git_paths=mock.Mock(),
                ), ports.review_connection, Path('/repo'), Path('/state'))
                ports.git.requirement_archive_only = git_port.requirement_archive_only
                if change == 'verified':
                    self.assertFalse(git_port.requirement_archive_only(
                        Path('/repo'), 'old-base', 'base', verified_plan_declarations=True))
                    self.assertFalse(git_port.requirement_archive_only(
                        Path('/repo'), 'old-base', 'base', allow_execplan_only=True))
                planning_input_state.record_general(
                    ports.review_connection, '/repo', 'old-base', {plan.decode(): ('blob', 'plan')},
                )
                if change != 'unverified':
                    planning_input_state.advance_verified_delta(
                        ports.review_connection, '/repo', 'base', {plan.decode(): (('blob', 'plan'), None)},
                    )
                ports.state.plan_declarations_match = lambda repo, entries: planning_input_state.matches(
                    ports.review_connection, repo, entries)
                self.record(ports, purpose='final', outcome='fulfilled')
                if change == 'cycle':
                    ports.state.cycle = replace(ports.state.cycle, revision=1)
                if change == 'verified':
                    for _ in range(2):
                        outcome = PlanningLifecycleCoordinator(ports).run(self.context)
                        self.assertEqual(outcome.kind, PlanningOutcomeKind.FINGERPRINT_SKIPPED)
                    self.assertEqual(ports.agent.run_count, 0)
                    self.assertEqual(ports.state.snapshot.planning_fingerprint, 'old-full')
                    self.assertEqual(ports.state.planning_review('/repo', 'final')['revision'], 'old-base')
                else:
                    PlanningLifecycleCoordinator(ports).run(self.context)
                    self.assertEqual(ports.agent.run_count, 1)

    def test_final_attempt_rebases_with_its_purpose_and_invalid_result_retries(self):
        from loop_manager_internal.planning.specification_review import prepare_review
        ports = self.review_ports(planning_snapshot(processed_sha='base',
            planning_fingerprint='fingerprint', specification_fingerprint='fingerprint'))
        instruction, first = prepare_review(ports.state, ports.git, self.context, 'base', final_required=True)
        instruction, resumed = prepare_review(ports.state, ports.git, self.context, 'advanced', final_required=False)
        self.assertEqual(resumed['purpose'], 'final')
        self.assertEqual(resumed['revision'], 'advanced')
        self.assertIn('최종 전문 확인', instruction)
        ports.state.begin_planning_review('/repo', purpose='final', revision='base',
            input_fingerprint='fingerprint', specification_fingerprint='fingerprint',
            work_cycle='cycle-0', comparison_revision=None)
        original_agent = ports.agent.run_planner
        call_count = 0
        def agent(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = original_agent(*args, **kwargs)
            return replace(result, message='missing full review') if call_count == 1 else result
        ports.agent.run_planner = agent
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertIsNone(ports.state.planning_review('/repo', 'final'))
        self.assertEqual(PlanningLifecycleCoordinator(ports).run(self.context).kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.agent.resumed_session, 'session-1')
        self.assertEqual(ports.state.planning_review('/repo', 'final')['outcome'], 'fulfilled')

    def test_later_general_success_revokes_old_final_without_erasing_evidence(self):
        from loop_manager_internal.storage import planning_review_state as reviews
        ports = self.review_ports()
        self.record(ports, purpose='final', outcome='fulfilled', revision='base')
        self.assertIsNotNone(ports.state.planning_review('/repo', 'final'))
        self.record(ports, purpose='delta', revision='changed')
        self.assertIsNone(ports.state.planning_review('/repo', 'final'))
        self.assertEqual(reviews.get(ports.review_connection, '/repo', 'final')['outcome'], 'fulfilled')

    def test_delta_reads_deletion_rename_and_catalog_without_full_scan(self):
        ports = self.review_ports(planning_snapshot(processed_sha='old-base',
            planning_fingerprint='old-input', specification_fingerprint='old-spec'))
        ports.git.fingerprints['old-base'] = 'old-input'
        ports.git.nonplan_fingerprints['old-base'] = 'old-input'
        ports.git.specification_fingerprints['old-base'] = 'old-spec'
        self.record(ports)
        ports.git.diff_name_status = 'D\0docs/requirements/A.md\0R100\0src/old.py\0src/new.py\0M\0docs/PRODUCT.md\0'
        ports.state.schedule_result.has_waiting = True
        PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(ports.state.planning_review('/repo', 'general')['purpose'], 'delta')
        self.assertIn('docs/requirements/A.md', ports.agent.prompts[0])
        self.assertIn('src/old.py', ports.agent.prompts[0])
        self.assertIn('dependency', ports.agent.prompts[0])
        self.assertNotIn('활성 Requirement 전문을 읽고', ports.agent.prompts[0])
        PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(ports.agent.run_count, 1)

    def test_missing_corrupt_or_unreadable_comparison_uses_full_review(self):
        for fault in ('missing', 'fingerprint', 'ancestry', 'diff'):
            with self.subTest(fault=fault):
                ports = self.review_ports(planning_snapshot(processed_sha='old-base', planning_fingerprint='old-input'))
                ports.git.specification_fingerprints['old-base'] = 'old-spec'
                ports.state.schedule_result.has_waiting = True
                ports.git.fingerprints['old-base'] = 'old-input'
                ports.git.nonplan_fingerprints['old-base'] = 'old-input'
                if fault != 'missing':
                    self.record(ports)
                if fault == 'fingerprint':
                    ports.git.fingerprints['old-base'] = 'corrupted'
                elif fault == 'ancestry':
                    ports.git.ancestor = False
                elif fault == 'diff':
                    original = ports.git.run
                    def unreadable(repo, *args, **kwargs):
                        if args[:2] == ('diff', '--name-status'):
                            raise RuntimeError('comparison unavailable')
                        return original(repo, *args, **kwargs)
                    ports.git.run = unreadable
                PlanningLifecycleCoordinator(ports).run(self.context)
                self.assertEqual(ports.state.planning_review('/repo', 'general')['purpose'], 'full')
                self.assertIn('활성 Requirement 전문', ports.agent.prompts[0])

    def test_waiting_catalog_active_and_maintenance_do_not_trigger_final(self):
        for gate in ('ready', 'draft-or-pr', 'active', 'blocked', 'maintenance', 'due'):
            with self.subTest(gate=gate):
                ports = self.review_ports(planning_snapshot(processed_sha='base',
                    planning_fingerprint='fingerprint', specification_fingerprint='fingerprint'))
                if gate == 'ready':
                    ports.state.schedule_result.has_ready = True
                elif gate == 'draft-or-pr':
                    ports.state.schedule_result.has_waiting = True
                elif gate == 'active':
                    ports.state.snapshot = replace(ports.state.snapshot, phase='WORKING')
                    ports.state.schedule_result.active = object()
                elif gate == 'blocked':
                    ports.state.schedule_result.project_blocked = True
                elif gate == 'maintenance':
                    ports.state.cycle = replace(ports.state.cycle, phase=maintenance_state.MaintenancePhase.REFACTORING)
                else:
                    ports.state.maintenance_due_required = True
                PlanningLifecycleCoordinator(ports).run(self.context)
                self.assertEqual(ports.agent.run_count, 0)
                self.assertIsNone(ports.state.planning_review('/repo', 'final'))

    def test_final_remote_race_and_cleanup_failure_do_not_confirm(self):
        for fault in ('remote', 'cleanup'):
            with self.subTest(fault=fault):
                ports = self.review_ports(planning_snapshot(processed_sha='base',
                    planning_fingerprint='fingerprint', specification_fingerprint='fingerprint'))
                original_agent = ports.agent.run_planner
                def agent(*args, **kwargs):
                    result = original_agent(*args, **kwargs)
                    if fault == 'remote':
                        ports.git.remote = 'advanced'
                    else:
                        ports.git.remove_error = RuntimeError('cleanup failed')
                    return result
                ports.agent.run_planner = agent
                outcome = PlanningLifecycleCoordinator(ports).run(self.context)
                self.assertTrue(outcome.error)
                self.assertIsNone(ports.state.planning_review('/repo', 'final'))
                self.assertIsNotNone(ports.state.planning_review('/repo', 'attempt'))

    def test_final_format_normalization_binds_only_verified_attempt_identity(self):
        from loop_manager_internal.planning.specification_review import validate_final, valid_final_receipt
        ports = self.review_ports()
        ports.state.begin_planning_review('/repo', purpose='final', revision='base',
            input_fingerprint='fingerprint', specification_fingerprint='fingerprint',
            work_cycle='cycle-0', comparison_revision=None)
        attempt = ports.state.planning_review('/repo', 'attempt')
        value = {'outcome': 'fulfilled', 'reason': 'All source contracts and validation evidence were reviewed',
                 'evidence': list(self.evidence)}
        output = '**FINAL_SPECIFICATION_REVIEW** =\n' + json.dumps(value, indent=2)
        self.assertEqual(validate_final(attempt, output, None, ports.git, Path('/repo')), 'fulfilled')
        self.assertTrue(valid_final_receipt(dict(attempt) | {'result': output, 'outcome': 'fulfilled'}))
        for change in ({'revision': 'other'}, {'evidence': []}, {'reason': ''}, {'outcome': 'followup'}):
            with self.subTest(change=change), self.assertRaises(PlanningResultInvalid):
                validate_final(attempt, 'FINAL_SPECIFICATION_REVIEW: ' + json.dumps(value | change), None, ports.git, Path('/repo'))

    def test_final_followup_and_decision_require_matching_plans(self):
        from loop_manager_internal.planning.specification_review import validate_final
        ports = self.review_ports()
        ports.state.begin_planning_review('/repo', purpose='final', revision='base',
            input_fingerprint='fingerprint', specification_fingerprint='fingerprint',
            work_cycle='cycle-0', comparison_revision=None)
        attempt = ports.state.planning_review('/repo', 'attempt')
        batch = SimpleNamespace(plans=(('docs/plans/G.md', {'id': 'G-001', 'sources': ['docs/PRODUCT.md']}),),
            revisions=(), deletions=(), restoration_moves=())
        self.assertEqual(validate_final(attempt, self.final_result(attempt, outcome='followup'), batch,
            ports.git, Path('/repo')), 'followup')
        with self.assertRaises(PlanningResultInvalid):
            validate_final(attempt, self.final_result(attempt, outcome='needs_decision'), batch, ports.git, Path('/repo'))
        batch.plans[0][1]['draft'] = True
        self.assertEqual(validate_final(attempt, self.final_result(attempt, outcome='needs_decision'), batch,
            ports.git, Path('/repo')), 'needs_decision')
        for output in ('', self.final_result(attempt, outcome='followup'),
                       self.final_result(attempt, revision='other'),
                       self.final_result(attempt, evidence=['src/app.py']),
                       self.final_result(attempt, evidence=['docs/PRODUCT.md', 'docs/ARCHITECTURE.md']),
                       self.final_result(attempt, evidence=[*self.evidence, '../secret']),
                       self.final_result(attempt) + '\n' + self.final_result(attempt)):
            with self.subTest(output=output), self.assertRaises(PlanningResultInvalid):
                validate_final(attempt, output, None, ports.git, Path('/repo'))
        with self.assertRaises(PlanningResultInvalid):
            validate_final(attempt, self.final_result(attempt), batch, ports.git, Path('/repo'))

    def test_final_review_without_product_uses_requirements_and_rejects_symlink_evidence(self):
        from loop_manager_internal.planning.specification_review import validate_final
        ports = self.review_ports()
        source = 'docs/requirements/Only.md'
        self.evidence = (*self.evidence, source)
        ports.state.begin_planning_review('/repo', purpose='final', revision='base',
            input_fingerprint='fingerprint', specification_fingerprint='fingerprint',
            work_cycle='cycle-0', comparison_revision=None)
        attempt = ports.state.planning_review('/repo', 'attempt')
        output = self.final_result(attempt, evidence=['docs/ARCHITECTURE.md', source, 'src/app.py'])
        original = ports.git.run
        def without_product(repo, *args, **kwargs):
            if args[:4] == ('ls-tree', '-r', '--name-only', '-z'):
                return SimpleNamespace(stdout='docs/ARCHITECTURE.md\0' + source + '\0', returncode=0)
            return original(repo, *args, **kwargs)
        ports.git.run = without_product
        self.assertEqual(validate_final(attempt, output, None, ports.git, Path('/repo')), 'fulfilled')
        def symlink(repo, *args, **kwargs):
            result = without_product(repo, *args, **kwargs)
            if args[:3] == ('ls-tree', '-r', '-z'):
                result.stdout = result.stdout.replace('100644 blob oid\tsrc/app.py', '120000 blob oid\tsrc/app.py')
            return result
        ports.git.run = symlink
        with self.assertRaises(PlanningResultInvalid):
            validate_final(attempt, output, None, ports.git, Path('/repo'))
