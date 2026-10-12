from tests.support.workspace import symlink_or_skip
from contextlib import contextmanager, ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.maintenance.maintenance_file_inventory import RootPlacement, TrackedSource
from tests.support.maintenance import result_for
from tests.support.git import isolated_git_test
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from loop_manager_internal.planning.input_fingerprints import (
    IsRequirementArchiveOnlyChangeEffects, _is_requirement_archive_only_change,
)
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
from loop_manager_internal.planning.diagnostics import (
    PreservePlanningDiagnosticsEffects, planning_diagnostics_path,
    preserve_planning_diagnostics,
)


from tests.support.planning_case import (
    PlanningCase, FakePorts, FakeStatePort, FakeGitPort, FakeAgentPort,
    FakeValidationPort, FakeEventPort, planning_snapshot, maintenance_cycle,
)

class PlanningDiagnosticsTest(unittest.TestCase):
    def test_successive_bases_keep_original_result_and_error_audit(self):
        effects = PreservePlanningDiagnosticsEffects(
            _nul_git_paths=lambda *_args: (),
            git=lambda *_args, **_kwargs: None,
            planning_diagnostics_path=planning_diagnostics_path,
        )
        with tempfile.TemporaryDirectory() as directory:
            row = {
                "planning_attempt_fingerprint": "same-input",
                "planning_error_fingerprint": "failed-input",
                "worktree": None,
                "planning_error": "old unsafe",
                "planning_error_incident_id": "incident-1",
                "planning_error_origin": "unsafe",
                "planning_validation_error": None,
                "last_error": "old unsafe",
                "planner_session_id": "saved-session",
                "planning_retry_count": 2,
                "planning_result_completed": 1,
                "planning_result": 'MAINTENANCE_RESULT: {"outcome":"unsafe"}',
                "active_sha": "base-1",
            }
            first = preserve_planning_diagnostics(effects, Path(directory), "/repo", row)
            second = preserve_planning_diagnostics(
                effects, Path(directory), "/repo", {**row, "active_sha": "base-2"},
            )
            self.assertNotEqual(first, second)
            self.assertIn("old unsafe", first.read_text())
            self.assertIn('"outcome":"unsafe"', first.read_text())
            self.assertIn("base-2", second.read_text())

@isolated_git_test
class PlanningRecoveryTest(PlanningCase):
    def test_completed_general_result_rechecks_archive_mixed_with_plan_edit(self):
        requirement = b"docs/requirements/R.md"
        archived = b"docs/requirements/archive/R.md"
        plan = b"docs/plans/[G-001] Plan.md"
        previous = {
            requirement: (b"blob", b"requirement"),
            plan: (b"blob", b"original-plan"),
        }
        for plan_changed in (False, True):
            with self.subTest(plan_changed=plan_changed):
                current = {
                    archived: previous[requirement],
                    plan: (b"blob", b"edited-plan" if plan_changed else b"original-plan"),
                }
                effects = IsRequirementArchiveOnlyChangeEffects(
                    _git_tree_entries=lambda _repo, revision: (
                        previous if revision == "base" else current
                    ),
                    _is_planning_input=loop_manager._is_planning_input,
                )
                ports = FakePorts(planning_snapshot(
                    phase="PLANNING", active_sha="base", worktree="/planning",
                    planning_attempt_fingerprint="original-input",
                    planning_workspace_owned=1, planner_session_id="saved-session",
                    planning_result="saved result", planning_result_completed=True,
                    planning_fingerprint="previous-success",
                ))
                ports.git.remote = "new-base"
                ports.git.fingerprints = {"base": "old-input", "new-base": "new-input"}
                ports.git.relevant_document_changes = mock.Mock(return_value=())
                ports.git.requirement_archive_only = lambda _repo, old, new: (
                    _is_requirement_archive_only_change(effects, Path("/repo"), old, new)
                )

                coordinator = PlanningLifecycleCoordinator(ports)
                self.assertIsNone(coordinator._workspace._prepare_workspace(
                    coordinator._new_run(self.context),
                ))

                saved = ports.state.snapshot
                self.assertEqual(bool(saved.planning_result_pending_validation), plan_changed)
                self.assertEqual(bool(saved.planning_result_completed), not plan_changed)
                self.assertEqual(saved.planning_fingerprint, "previous-success")
                if plan_changed:
                    self.assertEqual(ports.state.diagnostics[-1].planning_result, "saved result")
                    self.assertIsNone(saved.planning_result)
                else:
                    self.assertEqual(saved.planning_result, "saved result")

    def test_completed_results_recheck_code_input_before_recording_success(self):
        for mode in ("general", "combined", "maintenance"):
            for change in ("code", "same-content", "archive"):
                with self.subTest(mode=mode, change=change):
                    maintenance = mode != "general"
                    cycle = maintenance_cycle(
                        phase=(maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
                               if maintenance else maintenance_state.MaintenancePhase.IDLE),
                        cycle_base_sha="base", revalidation_plan_ids=("G-066",),
                        include_general_planning=mode == "combined",
                    )
                    original = ('MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"old evidence"}'
                                if maintenance else "no further plans")
                    error = "old unsafe evidence" if maintenance else None
                    ports = FakePorts(planning_snapshot(
                        phase="PLANNING", active_sha="base", worktree="/planning",
                        planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
                        planner_session_id="saved-session", planning_result=original,
                        planning_result_completed=True, planning_error=error,
                        planning_error_origin="unsafe" if maintenance else None,
                        last_error=error,
                        planning_fingerprint="previous-success",
                    ), cycle)
                    ports.git.remote = "new-base"
                    ports.git.fingerprints = {
                        "base": "old-input",
                        "new-base": "old-input" if change == "same-content" else "new-input",
                    }
                    ports.git.relevant_document_changes = mock.Mock(return_value=())
                    ports.git.requirement_archive_only = mock.Mock(return_value=change == "archive")
                    ports.validation.prepare_maintenance_context = mock.Mock(
                        return_value=SimpleNamespace(active_sha="new-base"),
                    )
                    ports.validation.maintenance_change_context = mock.Mock(return_value="")
                    ports.validation.maintenance_prompt = mock.Mock(return_value="recheck inputs")
                    ports.validation.validate_maintenance_result = mock.Mock(return_value=None)
                    ports.agent.result = replace(ports.agent.result, message=(
                        'MAINTENANCE_RESULT: {"outcome":"revalidated"}' if maintenance
                        else "fresh no further plans"
                    ))
                    coordinator = PlanningLifecycleCoordinator(ports)
                    run = coordinator._new_run(self.context)
                    self.assertIsNone(coordinator._workspace._prepare_workspace(run))
                    recheck = mode != "maintenance" and change == "code"
                    saved = ports.state.snapshot
                    self.assertEqual(bool(saved.planning_result_pending_validation), recheck)
                    self.assertEqual(bool(saved.planning_result_completed), not recheck)
                    self.assertEqual(saved.planning_fingerprint, "previous-success")
                    self.assertEqual(saved.planning_error, error)
                    self.assertEqual(saved.planning_result, None if recheck else original)
                    if recheck:
                        self.assertEqual(ports.state.diagnostics[-1].planning_result, original)
                        self.assertIn("전체 계획 입력", saved.planning_validation_error)
                        self.assertEqual(saved.planning_correction_error, error)
                    # Restart after adopting the base must not lose or duplicate pending work.
                    diagnostic_count = len(ports.state.diagnostics)
                    run = coordinator._new_run(self.context)
                    self.assertIsNone(coordinator._workspace._prepare_workspace(run))
                    self.assertEqual(len(ports.state.diagnostics), diagnostic_count)
                    ports.git.relevant_document_changes.assert_called_once()
                    if maintenance and recheck:
                        outcome = coordinator._assessment._run_planner_turn(run)
                        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                        self.assertEqual(ports.agent.run_count, 0)
                        self.assertEqual(ports.state.snapshot.planning_error, error)
                        continue
                    self.assertIsNone(coordinator._assessment._run_planner_turn(run))
                    self.assertEqual(ports.agent.run_count, int(recheck))
                    if recheck:
                        self.assertEqual(ports.agent.resumed_session, "saved-session")
                        self.assertIsNone(coordinator._assessment._run_planner_turn(run))
                        self.assertEqual(ports.agent.run_count, 1)
                        self.assertIsNone(coordinator._assessment._validate_planner_result(run))
                        self.assertIsNone(ports.state.snapshot.planning_error)
                        # Validation alone must not advance the publication success baseline.
                        self.assertEqual(ports.state.snapshot.planning_fingerprint, "previous-success")

    def test_general_ready_result_rechecks_new_active_requirement(self):
        requirement = "docs/requirements/[R-053] New Goal.md"
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result="saved result", planning_result_completed=True,
        ))
        ports.state.execplan_rows = lambda _repo: [{"plan_id": "G-118", "status": "READY"}]
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(return_value=(requirement,))

        coordinator = PlanningLifecycleCoordinator(ports)
        self.assertIsNone(coordinator._workspace._prepare_workspace(
            coordinator._new_run(self.context),
        ))

        self.assertEqual(ports.git.relevant_document_changes.call_args.args[-1], ("G-118",))
        self.assertTrue(ports.git.relevant_document_changes.call_args.kwargs[
            "include_active_requirements"
        ])
        self.assertTrue(ports.state.snapshot.planning_result_pending_validation)
        self.assertFalse(ports.state.snapshot.planning_result_completed)
        self.assertIsNone(ports.state.snapshot.planning_result)
        self.assertEqual(ports.state.diagnostics[-1].planning_result, "saved result")
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")

    def test_planner_result_refreshes_remote_before_validation_and_reuses_session(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
        ))
        ports.git.relevant_document_changes = mock.Mock(
            return_value=("docs/requirements/R-053 New.md",),
        )
        ports.state.execplan_rows = lambda _repo: [{"plan_id": "G-118", "status": "READY"}]
        ports.validation.validate_general_result = mock.Mock(
            return_value=ports.validation.result,
        )
        original_run = ports.agent.run_planner

        def advance_remote_after_writer(*args, **kwargs):
            result = original_run(*args, **kwargs)
            ports.git.remote = "new-base"
            return result

        coordinator = PlanningLifecycleCoordinator(ports)
        with mock.patch.object(
            ports.agent, "run_planner", side_effect=advance_remote_after_writer,
        ) as invoke:
            outcome = coordinator.run(self.context)
            self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
            ports.validation.validate_general_result.assert_not_called()
            saved = ports.state.snapshot
            self.assertEqual(saved.active_sha, "base")
            self.assertTrue(saved.planning_result_pending_validation)
            self.assertTrue(saved.planning_result_completed)
            self.assertEqual(saved.planning_result, "planner result")
            self.assertEqual(saved.planner_session_id, "session-1")

            resumed = coordinator._new_run(replace(self.context, base_sha="new-base"))
            self.assertIsNone(coordinator._workspace._prepare_workspace(resumed))
            saved = ports.state.snapshot
            self.assertEqual(saved.active_sha, "new-base")
            self.assertTrue(saved.planning_result_pending_validation)
            self.assertFalse(saved.planning_result_completed)
            self.assertIsNone(saved.planning_result)
            self.assertIsNone(coordinator._assessment._run_planner_turn(resumed))
            self.assertEqual(ports.agent.resumed_session, "session-1")
            self.assertIsNone(coordinator._workspace._refresh_after_planner(resumed))
            self.assertIsNone(coordinator._assessment._validate_planner_result(resumed))

        self.assertEqual(invoke.call_count, 2)
        self.assertIsNone(invoke.call_args_list[0].args[2])
        self.assertEqual(invoke.call_args_list[1].args[2], "session-1")
        ports.validation.validate_general_result.assert_called_once()

    def test_related_remote_change_resets_only_stale_result_retry_count(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
            planner_session_id="saved-session", planning_retry_count=2,
            planning_validation_error="old input result error",
            planning_result="old result", planning_result_pending_validation=True,
        ))
        ports.git.relevant_document_changes = mock.Mock(
            return_value=("docs/requirements/R-053 New.md",),
        )
        ports.state.execplan_rows = lambda _repo: [{"plan_id": "G-118", "status": "READY"}]
        ports.validation.validate_general_result = mock.Mock(
            side_effect=PlanningResultInvalid("new input result error"),
        )
        original_run = ports.agent.run_planner

        def advance_remote_after_first_writer(*args, **kwargs):
            result = original_run(*args, **kwargs)
            if ports.agent.run_count == 1:
                ports.git.remote = "new-base"
            return result

        coordinator = PlanningLifecycleCoordinator(ports)
        with mock.patch.object(
            ports.agent, "run_planner", side_effect=advance_remote_after_first_writer,
        ) as invoke:
            outcome = coordinator.run(self.context)
            self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
            saved = ports.state.snapshot
            self.assertEqual(saved.active_sha, "base")
            self.assertEqual(saved.planning_retry_count, 2)
            self.assertTrue(saved.planning_result_pending_validation)
            self.assertTrue(saved.planning_result_completed)
            self.assertEqual(saved.planning_result, "planner result")
            ports.validation.validate_general_result.assert_not_called()

            resumed = coordinator._new_run(replace(self.context, base_sha="new-base"))
            self.assertIsNone(coordinator._workspace._prepare_workspace(resumed))
            saved = ports.state.snapshot
            self.assertEqual(saved.active_sha, "new-base")
            self.assertEqual(saved.planning_retry_count, 0)
            self.assertTrue(saved.planning_result_pending_validation)
            self.assertFalse(saved.planning_result_completed)
            self.assertIsNone(saved.planning_result)
            self.assertIn("현재 planning 결과의 관련 문서가 변경됐습니다", saved.planning_validation_error)
            self.assertEqual(ports.state.diagnostics[-1].planning_retry_count, 2)
            self.assertIsNone(coordinator._assessment._run_planner_turn(resumed))
            self.assertIsNone(coordinator._workspace._refresh_after_planner(resumed))
            retry = coordinator._assessment._validate_planner_result(resumed)
            self.assertEqual(retry.kind, PlanningOutcomeKind.VALIDATION_RETRY)
            self.assertEqual(ports.state.snapshot.planning_retry_count, 0)
            self.assertEqual(ports.state.snapshot.planning_validation_error, "new input result error")

        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(invoke.call_args_list[0].args[2], "saved-session")
        self.assertEqual(invoke.call_args_list[1].args[2], "session-1")
        ports.validation.validate_general_result.assert_called_once()

    def test_related_remote_change_before_correction_resets_stale_retry_budget(self):
        prior_error = "old input result error"
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
            planner_session_id="saved-session", planning_retry_count=2,
            planning_validation_error=prior_error,
            planning_result_completed=False,
            planning_result_pending_validation=False,
            planning_error=None,
        ))
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(
            return_value=("docs/requirements/R-053 New.md",),
        )
        ports.state.execplan_rows = lambda _repo: [{"plan_id": "G-118", "status": "READY"}]
        ports.validation.validate_general_result = mock.Mock(
            side_effect=PlanningResultInvalid("new input result error"),
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._workspace._prepare_workspace(run))
        saved = ports.state.snapshot
        self.assertEqual(saved.active_sha, "new-base")
        self.assertFalse(saved.planning_result_completed)
        self.assertFalse(saved.planning_result_pending_validation)
        self.assertEqual(saved.planning_retry_count, 0)
        self.assertIsNone(saved.planning_error)
        self.assertIn("현재 planning 결과의 관련 문서가 변경됐습니다", saved.planning_validation_error)
        self.assertEqual(ports.state.diagnostics[-1].planning_validation_error, prior_error)
        self.assertEqual(ports.state.diagnostics[-1].planning_retry_count, 2)

        self.assertIsNone(coordinator._assessment._run_planner_turn(run))
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertIsNone(coordinator._workspace._refresh_after_planner(run))
        outcome = coordinator._assessment._validate_planner_result(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.state.snapshot.planning_retry_count, 0)
        self.assertEqual(ports.state.snapshot.planning_validation_error, "new input result error")
        self.assertIsNone(ports.state.snapshot.planning_error)
        ports.validation.validate_general_result.assert_called_once()

    def test_linked_remote_correction_binding_allows_fresh_validation_retry(self):
        error = "old input validation failure"
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="fingerprint", planning_workspace_owned=1,
            planner_session_id="saved-session", planning_retry_count=1,
            planning_error=error, planning_error_fingerprint="fingerprint",
            planning_error_incident_id="incident", planning_error_origin="validation",
            planning_correction_error=error, planning_correction_incident_id="incident",
            planning_correction_sha="base", planning_correction_context="general",
            planning_validation_error="new input validation failure",
        ))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._selection._gate_preserved_error(run))
        self.assertTrue(run.error_input_changed)
        self.assertIsNone(coordinator._selection._reconcile_inputs(run))
        self.assertIsNone(coordinator._workspace._prepare_workspace(run))
        self.assertIsNone(coordinator._assessment._run_planner_turn(run))
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertIsNone(coordinator._workspace._refresh_after_planner(run))
        self.assertIsNone(coordinator._assessment._validate_planner_result(run))
        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertIsNone(ports.state.snapshot.planning_correction_error)

    def test_conflict_correction_refetches_after_owned_rebase_finishes(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
            planner_session_id="saved-session",
        ))
        ports.git.fetch_base = mock.Mock(side_effect=("target", "advanced"))
        ports.git.rebase_planning_workspace = mock.Mock(side_effect=(
            SimpleNamespace(target="target", conflict=True, detail="conflict"),
            SimpleNamespace(target="target", conflict=False),
        ))
        ports.git.complete_planning_rebase = mock.Mock()
        ports.validation.prepare_general_context = mock.Mock(
            return_value=ports.validation.snapshot,
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        with mock.patch(
            "loop_manager_internal.planning.workspace_service.PlanningConflictCorrection",
        ) as boundary:
            boundary.return_value.resolve.return_value = True
            outcome = coordinator._workspace._prepare_workspace(
                coordinator._new_run(self.context),
            )

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
        self.assertEqual(ports.state.snapshot.active_sha, "target")
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
        self.assertEqual(ports.git.fetch_base.call_count, 2)
        ports.git.complete_planning_rebase.assert_called_once_with(
            "/repo", Path("/planning"), "target",
        )
        ports.validation.prepare_general_context.assert_not_called()

    def test_unrelated_maintenance_requirement_does_not_reset_exhausted_error(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            cycle_base_sha="base", revalidation_plan_ids=("G-066",), revision=1,
        )
        original_input = "old-input"
        error = "saved validation failure"
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
            planner_session_id="saved-session", planning_retry_count=3,
            planning_error=error, planning_error_origin="validation",
            planning_validation_error=error,
            planning_error_fingerprint=maintenance_planning_fingerprint(cycle, original_input),
        ), cycle)
        ports.git.fingerprints = {"base": original_input, "new-base": "unrelated-input"}
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(return_value=())

        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.active_sha, "new-base")
        self.assertEqual(ports.state.snapshot.planning_retry_count, 3)
        self.assertFalse(ports.state.snapshot.planning_result_pending_validation)
        self.assertEqual(ports.state.snapshot.planning_error, error)
        self.assertIsNone(ports.state.snapshot.planning_correction_error)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertFalse(ports.git.relevant_document_changes.call_args.kwargs[
            "include_active_requirements"
        ])

    def test_combined_maintenance_code_change_resets_exhausted_error_once(self):
        for archive_only in (False, True):
            with self.subTest(archive_only=archive_only):
                cycle = maintenance_cycle(
                    phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                    cycle_base_sha="base", include_general_planning=True, revision=1,
                )
                error = "saved validation failure"
                ports = FakePorts(planning_snapshot(
                    phase="PLANNING", active_sha="base", worktree="/planning",
                    planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
                    planner_session_id="saved-session", planning_retry_count=3,
                    planning_error=error, planning_error_origin="validation",
                    planning_validation_error=error,
                    planning_error_fingerprint=maintenance_planning_fingerprint(cycle, "old-input"),
                ), cycle)
                ports.git.fingerprints = {"base": "old-input", "new-base": "code-input"}
                ports.git.remote = "new-base"
                ports.git.relevant_document_changes = mock.Mock(return_value=())
                ports.git.requirement_archive_only = mock.Mock(return_value=archive_only)
                ports.validation.prepare_maintenance_context = mock.Mock(return_value=SimpleNamespace())
                ports.validation.maintenance_change_context = mock.Mock(return_value="")
                coordinator = PlanningLifecycleCoordinator(ports)

                coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))

                saved = ports.state.snapshot
                self.assertEqual(saved.active_sha, "new-base")
                self.assertEqual(saved.planning_retry_count, 3 if archive_only else 0)
                self.assertEqual(bool(saved.planning_result_pending_validation), not archive_only)
                self.assertEqual(saved.planning_error, error)
                self.assertEqual(saved.planner_session_id, "saved-session")
                self.assertEqual(ports.state.diagnostics[-1].planning_retry_count, 3)
                if not archive_only:
                    self.assertEqual(saved.planning_correction_error, error)
                    self.assertEqual(saved.planning_correction_sha, "new-base")
                    coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))
                    self.assertEqual(ports.state.snapshot.planning_correction_incident_id,
                                     saved.planning_correction_incident_id)
                    self.assertEqual(len(ports.state.diagnostics), 1)
                self.assertEqual(ports.agent.run_count, 0)

    def test_fresh_unsafe_records_error_origin_for_later_correction(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_result='MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"needs review"}',
            planning_result_completed=True,
        ), maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            cycle_base_sha="base", revalidation_plan_ids=("G-066",),
        ))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")
        run.maintenance_context = SimpleNamespace(active_sha="base")
        run.dirty_files = []

        outcome = coordinator._assessment._validate_planner_result(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.UNSAFE_REVALIDATION)
        self.assertEqual(ports.state.snapshot.planning_error_origin, "unsafe")
        self.assertIsNone(ports.state.snapshot.planning_correction_error)

    def test_exhausted_validation_error_retries_once_after_relevant_change(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_retry_count=3, planning_error="old validation failure",
            planning_validation_error="old validation failure",
            planning_error_fingerprint="fingerprint",
        ))
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(return_value=("docs/PRODUCT.md",))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._workspace._prepare_workspace(run))
        self.assertEqual(ports.state.snapshot.planning_retry_count, 0)
        self.assertTrue(ports.state.snapshot.planning_result_pending_validation)
        self.assertIsNone(coordinator._assessment._run_planner_turn(run))
        self.assertEqual(ports.agent.run_count, 1)
        self.assertIsNone(coordinator._assessment._validate_planner_result(run))
        self.assertIsNone(ports.state.snapshot.planning_error)

    def test_saved_unsafe_target_change_preserves_error_without_new_post_planner(self):
        original_result = 'MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"old evidence"}'
        revised_result = 'MAINTENANCE_RESULT: {"outcome":"revalidated"}'
        old_error = "post-refactor revalidation이 제품 목표 보존을 확정하지 못했습니다: old evidence"
        for scenario in ("current", "legacy", "independent"):
            with self.subTest(scenario=scenario):
                linked = scenario != "independent"
                ports = FakePorts(planning_snapshot(
                    phase="PLANNING", active_sha="base", worktree="/planning",
                    planning_attempt_fingerprint="original-input",
                    planning_workspace_owned=1, planner_session_id="saved-session",
                    planning_result=original_result, planning_result_completed=True,
                    planning_error=old_error if linked else "independent error",
                    planning_error_origin="unsafe" if scenario == "current" else None,
                    last_error=old_error,
                ), maintenance_cycle(
                    phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
                    cycle_base_sha="base", revalidation_plan_ids=("G-066",),
                ))
                ports.git.remote = "new-base"
                ports.git.relevant_document_changes = mock.Mock(
                    return_value=("docs/plans/[G-066] Changed.md",),
                )
                ports.validation.prepare_maintenance_context = mock.Mock(
                    return_value=SimpleNamespace(active_sha="new-base"),
                )
                ports.validation.maintenance_change_context = mock.Mock(return_value="changes")
                ports.validation.maintenance_prompt = mock.Mock(return_value="correct the plan")
                ports.validation.validate_maintenance_result = mock.Mock(return_value=None)
                ports.agent.result = replace(ports.agent.result, message=revised_result)
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)

                self.assertIsNone(coordinator._workspace._prepare_workspace(run))
                self.assertEqual(
                    ports.git.relevant_document_changes.call_args.args[-1], ("G-066",),
                )
                self.assertTrue(ports.state.snapshot.planning_result_pending_validation)
                self.assertEqual(ports.state.snapshot.planning_correction_error,
                                 old_error if linked else None)
                run = coordinator._new_run(self.context)  # restart after base adoption
                self.assertIsNone(coordinator._workspace._prepare_workspace(run))
                ports.git.relevant_document_changes.assert_called_once()
                outcome = coordinator._assessment._run_planner_turn(run)
                self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                self.assertEqual(ports.agent.run_count, 0)
                self.assertEqual(ports.state.snapshot.planning_error,
                                 old_error if linked else "independent error")
                self.assertEqual(ports.state.diagnostics[-1].planning_result, original_result)

    def test_unrelated_advance_does_not_schedule_completed_unsafe_again(self):
        result = 'MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"old evidence"}'
        error = "post-refactor revalidation이 제품 목표 보존을 확정하지 못했습니다: old evidence"
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result=result, planning_result_completed=True,
            planning_error=error, planning_error_origin="unsafe", last_error=error,
        ), maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            cycle_base_sha="base", revalidation_plan_ids=("G-066",),
        ))
        ports.git.remote = "new-base"
        ports.validation.prepare_maintenance_context = mock.Mock(return_value=object())
        ports.validation.maintenance_change_context = mock.Mock(return_value="")
        coordinator = PlanningLifecycleCoordinator(ports)

        self.assertIsNone(coordinator._workspace._prepare_workspace(
            coordinator._new_run(self.context),
        ))
        self.assertTrue(ports.state.snapshot.planning_result_completed)
        self.assertFalse(ports.state.snapshot.planning_result_pending_validation)
        self.assertIsNone(ports.state.snapshot.planning_correction_error)

    def test_additional_related_advance_rebinds_one_pending_correction(self):
        result = 'MAINTENANCE_RESULT: {"outcome":"unsafe","reason":"old evidence"}'
        error = "post-refactor revalidation이 제품 목표 보존을 확정하지 못했습니다: old evidence"
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result=result, planning_result_completed=True,
            planning_error=error, planning_error_origin="unsafe", last_error=error,
        ), maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            cycle_base_sha="base", revalidation_plan_ids=("G-066",),
        ))
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(side_effect=(
            ("docs/plans/[G-066] Target.md",),
            ("docs/PRODUCT.md",),
        ))
        ports.validation.prepare_maintenance_context = mock.Mock(return_value=object())
        ports.validation.maintenance_change_context = mock.Mock(return_value="")
        coordinator = PlanningLifecycleCoordinator(ports)

        self.assertIsNone(coordinator._workspace._prepare_workspace(
            coordinator._new_run(self.context),
        ))
        ports.git.remote = "latest-base"
        self.assertIsNone(coordinator._workspace._prepare_workspace(
            coordinator._new_run(self.context),
        ))
        row = ports.state.snapshot
        self.assertEqual(row.planning_correction_sha, "latest-base")
        self.assertIn("[G-066] Target.md", row.planning_validation_error)
        self.assertIn("docs/PRODUCT.md", row.planning_validation_error)
        self.assertEqual(row.planner_session_id, "saved-session")
        self.assertTrue(row.planning_result_pending_validation)
        self.assertIsNone(row.planning_result)
        self.assertEqual(ports.git.relevant_document_changes.call_count, 2)

    def test_recorded_workspace_fetch_preserves_saved_planner_scope_during_remote_race(self):
        from tests.support.origin import remote_fixture, advance_remote, git
        from loop_manager_internal.storage import database
        for recovery in (False, True):
            with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
                _remote, source, writer = remote_fixture(directory)
                state_dir = Path(directory) / "state"
                state_dir.mkdir()
                connection = database.connect_database(state_dir, create=True)
                cleanup.callback(connection.close)
                tree = Path(directory) / "planning"
                old = git(source, "rev-parse", "HEAD")
                git(source, "worktree", "add", "--detach", str(tree), old)
                repo = str(source)
                connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,))
                connection.commit()
                context = replace(self.context, repo=repo, repo_path=source, state_dir=state_dir)
                if recovery:
                    (tree / "candidate.txt").write_text("prepared candidate\n")
                    git(tree, "add", "candidate.txt")
                    git(tree, "commit", "-m", "prepared")
                candidate = git(tree, "rev-parse", "HEAD")
                before = loop_manager.capture_git_snapshot(tree)
                snapshot = before.serialize()
                connection.execute(
                    "UPDATE projects SET agent_snapshot=?, agent_role='planner', worktree=? WHERE repo=?",
                    (snapshot, str(tree), repo),
                )
                connection.commit()
                loop_manager._recorded_base_fetch(source, state_dir, "main", owner="project")
                # Origin advances after the outer fetch, while a saved scope exists.
                latest = advance_remote(writer)
                ports = FakePorts(planning_snapshot(
                    repo=repo, phase="PLANNING", active_sha=old, worktree=str(tree),
                    planning_workspace_owned=1, planner_session_id="saved-session",
                    agent_snapshot=snapshot, agent_role="planner",
                ))
                real_git = loop_manager._PlanningGitPorts(connection, source, state_dir)
                ports.git.fetch_base = real_git.fetch_base
                ports.git.run = real_git.run
                ports.git.remote = latest
                real_agent = loop_manager._PlanningAgentPorts(connection, context, None)
                ports.agent.validate_saved_scope = mock.Mock(wraps=real_agent.validate_saved_scope)
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(context)
                if recovery:
                    ports.state.general_intent = SimpleNamespace(
                        stage="prepared", base_sha=old, commit_sha=candidate,
                    )
                    self.assertIsNone(coordinator._publication._recover_publication(run))
                    self.assertEqual(git(source, "rev-parse", "origin/main"), latest)
                self.assertIsNone(coordinator._workspace._prepare_workspace(run))
                self.assertIsNone(real_agent.validate_saved_scope("planner", tree, snapshot).error)
                self.assertEqual(ports.agent.validate_saved_scope.call_count, 1)
                self.assertEqual(git(source, "rev-parse", "origin/main"), latest)
                self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
                self.assertEqual(git(source, "rev-parse", "HEAD"), old)

    def test_rebased_workspace_refreshes_sqlite_catalog_before_ready_snapshot(self):
        from tests.support.catalog import make_catalog_root, write_execplan
        from tests.support.maintenance_planning_case import git
        from loop_manager_internal.storage import database, runtime_state

        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root = Path(directory)
            repository = make_catalog_root(str(root / "repo"))
            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.com")
            plan = write_execplan(repository, "G-001", "Unrelated")
            git(repository, "add", ".")
            git(repository, "commit", "-m", "base")
            old = git(repository, "rev-parse", "HEAD").strip()
            git(repository, "update-ref", "refs/remotes/origin/main", old)
            connection = database.connect_database(root, create=True)
            cleanup.callback(connection.close)
            repo = str(repository)
            connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,))
            connection.commit()
            with mock.patch.object(loop_manager, "pr_for_branch", return_value=None):
                loop_manager.reconcile_execplan_catalog(connection, repository, repo, "main")
            state = loop_manager._PlanningStatePorts(
                connection, runtime_state.RuntimeStateRepository(connection),
                maintenance_state.MaintenanceRepository(connection), root,
            )
            original = plan.read_text()
            plan.write_text(original.replace("Test plan.", "Remote acceptance change."), newline="\n")
            git(repository, "add", ".")
            git(repository, "commit", "-m", "remote")
            latest = git(repository, "rev-parse", "HEAD").strip()
            git(repository, "update-ref", "refs/remotes/origin/main", latest)
            worktree = root / "planning"
            git(repository, "worktree", "add", "--detach", str(worktree), latest)
            candidate = write_execplan(worktree, "G-002", "Saved")
            saved = candidate.read_bytes()
            # An unpublished intent prevents outer catalog reconciliation;
            # workspace adoption must supersede it before refreshing SQLite.
            connection.execute(
                "INSERT INTO general_planning_publications "
                "(repo, stage, base_sha, commit_sha, snapshot_token, "
                "specification_fingerprint, planning_fingerprint, manifest, manifest_hash) "
                "VALUES (?, 'prepared', ?, ?, 'snapshot', 'spec', 'input', '[]', ?)",
                (repo, old, old, hashlib.sha256(b"[]").hexdigest()),
            )
            connection.commit()
            with self.assertRaises(PlanningResultInvalid):
                loop_manager.mutable_ready_snapshot(connection, repo, worktree, latest)
            for active in (old, latest):
                with self.subTest(active=active):
                    ports = FakePorts(planning_snapshot(
                        repo=repo, phase="PLANNING", active_sha=active, worktree=str(worktree),
                        planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
                        planner_session_id="saved-session", planning_result="saved result",
                        planning_result_completed=True,
                    ))
                    ports.git.remote = latest
                    ports.state.reconcile_catalog = state.reconcile_catalog
                    ports.state.general_publication = state.general_publication
                    ports.state.supersede_unpublished_general_intent = state.supersede_unpublished_general_intent
                    ports.validation.prepare_general_context = (
                        lambda repo, worktree, revision: loop_manager.mutable_ready_snapshot(
                            connection, repo, worktree, revision,
                        )
                    )
                    coordinator = PlanningLifecycleCoordinator(ports)
                    context = replace(self.context, repo=repo, repo_path=repository, base_sha=latest)
                    run = coordinator._new_run(context)
                    self.assertIsNone(coordinator._workspace._prepare_workspace(run))
                    self.assertEqual(run.mutable_ready_snapshot.active_sha, latest)
                    self.assertEqual(run.mutable_ready_snapshot.plans[0].content_hash,
                                     hashlib.sha256(plan.read_bytes()).hexdigest())
                    self.assertIsNone(ports.state.snapshot.planning_error)
                    self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
                    self.assertTrue(ports.state.snapshot.planning_result_completed)
                    self.assertEqual(candidate.read_bytes(), saved)
                    self.assertIsNone(state.general_publication(repo))

    def test_unchanged_prepared_intent_retains_its_catalog_snapshot(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result_completed=True,
        ))
        ports.state.general_intent = object()
        ports.state.reconcile_catalog = mock.Mock(side_effect=AssertionError("intent owns catalog"))
        coordinator = PlanningLifecycleCoordinator(ports)
        self.assertIsNone(coordinator._workspace._prepare_workspace(
            coordinator._new_run(self.context),
        ))
        ports.state.reconcile_catalog.assert_not_called()

    def test_catalog_race_pauses_without_poisoning_planning_error(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result_completed=True,
        ))
        ports.state.reconcile_catalog = mock.Mock(side_effect=RuntimeError("catalog changed"))
        ports.validation.prepare_general_context = mock.Mock()
        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertIsNone(ports.state.snapshot.planning_error)
        ports.validation.prepare_general_context.assert_not_called()

    def test_saved_scope_violation_blocks_rebase_without_discarding_result(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            agent_snapshot="saved-scope", agent_role="planner",
            planning_error="scope violation", planning_result="saved result",
        ))
        ports.git.remote = "new-base"
        ports.agent.validate_saved_scope = mock.Mock(return_value=PlanningScopeResult(
            error="unauthorized source change",
        ))
        ports.git.rebase_planning_workspace = mock.Mock()
        coordinator = PlanningLifecycleCoordinator(ports)

        outcome = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        ports.git.rebase_planning_workspace.assert_not_called()
        self.assertEqual(ports.state.snapshot.active_sha, "base")
        self.assertEqual(ports.state.snapshot.planning_error, "scope violation")
        self.assertEqual(ports.state.snapshot.planning_result, "saved result")
        self.assertEqual(ports.state.clear_calls, [])

    def test_completed_no_change_turn_resumes_validation_without_agent_replay(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result="no further plans", planning_result_completed=True,
        ))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")

        self.assertIsNone(coordinator._assessment._run_planner_turn(run))

        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_result, "no further plans")
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")

    def test_recorded_base_finishes_interrupted_rebase_cleanup(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
        ))
        coordinator = PlanningLifecycleCoordinator(ports)

        self.assertIsNone(coordinator._workspace._prepare_workspace(
            coordinator._new_run(self.context),
        ))
        self.assertIn(
            (None, ("complete_planning_rebase", "/repo", Path("/planning"), "base"), True),
            ports.git.calls,
        )

    def test_remote_advance_preserves_owned_planner_session_and_result(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result="saved result", planning_result_completed=True,
        ))
        ports.git.remote = "new-base"
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        outcome = coordinator._workspace._prepare_workspace(run)

        self.assertIsNone(outcome)
        self.assertEqual(ports.state.snapshot.active_sha, "new-base")
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
        self.assertEqual(ports.state.snapshot.planning_result, "saved result")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertIn(("/repo", "original-input", Path("/planning")),
                      ports.git.ownership_checks)

    def test_related_document_change_requests_saved_planner_recheck(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result="saved result", planning_result_completed=True,
            planning_error="old validation failure",
            planning_validation_error="old validation failure",
            planning_error_origin="validation", planning_retry_count=3,
        ))
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(return_value=(
            "docs/requirements/[R-051] Relevant.md",
        ))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._workspace._prepare_workspace(run))

        self.assertFalse(ports.state.snapshot.planning_result_completed)
        self.assertIn("[R-051] Relevant.md",
                      ports.state.snapshot.planning_validation_error)
        self.assertEqual(ports.state.snapshot.planning_error, "old validation failure")
        self.assertEqual(ports.state.snapshot.planning_correction_error,
                         "old validation failure")
        self.assertEqual(ports.state.snapshot.planning_correction_sha, "new-base")
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
        self.assertIsNone(ports.state.snapshot.planning_result)

    def test_planner_resolves_owned_plan_file_rebase_conflict_in_saved_session(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            plan = worktree / "docs/plans/[G-001] Plan.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("resolved plan\n", encoding="utf-8")
            (worktree / ".git/rebase-merge").mkdir(parents=True)
            ports = FakePorts(planning_snapshot(
                phase="PLANNING", active_sha="base", worktree=str(worktree),
                planning_attempt_fingerprint="original-input",
                planning_workspace_owned=1, planner_session_id="saved-session",
            ))
            ports.git.remote = "new-base"
            ports.git.rebase_planning_workspace = mock.Mock(side_effect=(
                SimpleNamespace(target="new-base", changed=True, conflict=True,
                                detail="plan conflict"),
                SimpleNamespace(target="new-base", changed=True, conflict=False,
                                detail=""),
            ))
            original_run = ports.git.run

            def run(repo, *args, check=True):
                if args == ("diff", "--name-only", "--diff-filter=U", "-z"):
                    return SimpleNamespace(stdout="docs/plans/[G-001] Plan.md\0",
                                           returncode=0)
                if args == ("rev-parse", "--git-path", "rebase-merge"):
                    return SimpleNamespace(
                        stdout=str(worktree / ".git/rebase-merge"), returncode=0,
                    )
                return original_run(repo, *args, check=check)

            ports.git.run = run
            coordinator = PlanningLifecycleCoordinator(ports)
            outcome = coordinator._workspace._prepare_workspace(
                coordinator._new_run(self.context)
            )

            self.assertIsNone(outcome)
            self.assertEqual(ports.agent.run_count, 1)
            self.assertEqual(ports.agent.resumed_session, "saved-session")
            self.assertEqual(ports.state.snapshot.active_sha, "new-base")
            calls = [args for _repo, args, _check in ports.git.calls]
            self.assertIn(("add", "--", "docs/plans/[G-001] Plan.md"), calls)
            self.assertIn(("-c", "core.editor=true", "rebase", "--continue"), calls)

    def test_untracked_plan_id_collision_uses_saved_planner_session(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            remote = worktree / "docs/plans/[G-120] Candidate.md"
            remote.parent.mkdir(parents=True)
            remote.write_text("remote plan\n", encoding="utf-8")
            corrected = worktree / "docs/plans/[G-121] Candidate.md"
            corrected.write_text("corrected local plan\n", encoding="utf-8")
            ports = FakePorts(planning_snapshot(
                phase="PLANNING", active_sha="base", worktree=str(worktree),
                planning_attempt_fingerprint="original-input",
                planning_workspace_owned=1, planner_session_id="saved-session",
            ))
            ports.git.remote = "new-base"
            collision = "docs/plans/[G-120] Candidate.md"
            ports.git.rebase_planning_workspace = mock.Mock(side_effect=(
                SimpleNamespace(target="new-base", changed=True, conflict=True,
                                detail="untracked collision", conflict_paths=(collision,),
                                stash_sha="saved-stash"),
                SimpleNamespace(target="new-base", changed=True, conflict=False,
                                detail=""),
            ))
            ports.git.accept_resolved_stash_conflict = mock.Mock()
            original_run = ports.git.run

            def run(repo, *args, check=True):
                if args == ("diff", "--name-only", "--diff-filter=U", "-z"):
                    return SimpleNamespace(stdout="", returncode=0)
                if args[:4] == ("ls-tree", "-r", "--name-only", "saved-stash^3"):
                    return SimpleNamespace(stdout=collision + "\n", returncode=0)
                if args == ("ls-files", "--others", "--exclude-standard"):
                    return SimpleNamespace(stdout="docs/plans/[G-121] Candidate.md\n",
                                           returncode=0)
                return original_run(repo, *args, check=check)

            ports.git.run = run
            coordinator = PlanningLifecycleCoordinator(ports)
            outcome = coordinator._workspace._prepare_workspace(
                coordinator._new_run(self.context),
            )

            self.assertIsNone(outcome)
            self.assertEqual(ports.agent.resumed_session, "saved-session")
            ports.git.accept_resolved_stash_conflict.assert_called_once_with(
                "/repo", worktree, "new-base",
            )
            self.assertEqual(ports.state.snapshot.active_sha, "new-base")

    def test_validation_retry_resumes_session_and_persists_retry_transition(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planner_session_id="session-1",
                planning_validation_error="first invalid result",
                planning_retry_count=1,
            )
        )
        ports.validation.general_error = "second invalid result"

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.agent.resumed_session, "session-1")
        self.assertEqual(ports.state.snapshot.planning_retry_count, 2)
        self.assertEqual(
            ports.state.snapshot.planning_validation_error,
            "second invalid result",
        )
        self.assertFalse(
            ports.state.snapshot.planning_result_pending_validation
        )

    def test_transient_git_error_escapes_without_persisting_planning_error(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
            )
        )
        ports.git.workspace_error = RuntimeError("index.lock exists")

        with self.assertRaisesRegex(RuntimeError, "index.lock exists"):
            PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertFalse(
            any("planning_error" in update for update in ports.state.updates)
        )

    def test_workspace_ownership_error_is_persisted_for_same_fingerprint(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
            )
        )
        ports.git.workspace_error = PlanningWorkspaceOwnershipError(
            "workspace belongs to another task"
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(
            ports.state.snapshot.planning_error,
            "planning workspace 소유권 검증 실패: workspace belongs to another task",
        )

    def test_local_recheck_retries_same_failed_planning_input_once(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved tool failure",
            planning_error_fingerprint="fingerprint",
        ))
        ports.state.activate_ready = mock.Mock(side_effect=AssertionError("new Worker"))
        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, local_environment_recheck=True))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertIsNone(ports.state.snapshot.planning_error)
        ports.state.activate_ready.assert_not_called()

    def test_local_recheck_validates_owned_result_before_releasing_error(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/saved/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_attempt_fingerprint="fingerprint",
            planning_error="saved validation failure", planning_error_origin="validation",
            planning_error_fingerprint="fingerprint", planning_retry_count=3,
            planning_validation_error="saved diagnostic",
        ))
        ports.state.activate_ready = mock.Mock(side_effect=AssertionError("new Worker"))
        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, local_environment_recheck=True))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertTrue(any(row.planning_retry_count == 3 for row in ports.state.diagnostics))
        ports.state.activate_ready.assert_not_called()

    def test_local_recheck_exhausted_planner_preserves_failure_history(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/saved/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_attempt_fingerprint="fingerprint",
            planning_error="saved tool failure", planning_error_fingerprint="fingerprint",
            planning_validation_error="saved diagnostic", planning_retry_count=3,
        ))
        ports.agent.result = PlanningAgentResult(
            1, "tool still unavailable", "saved-session", PlanningAgentResultKind.TRANSIENT_FAILURE)
        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, local_environment_recheck=True))
        self.assertTrue(outcome.error)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertEqual(ports.state.snapshot.planning_retry_count, 3)
        self.assertEqual(ports.state.snapshot.planning_error, "saved tool failure")

    def test_local_recheck_restores_preserved_planner_and_keeps_failed_validation(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/saved/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_attempt_fingerprint="fingerprint",
            planning_error="saved validation failure", planning_error_origin="validation",
            planning_error_fingerprint="fingerprint", planning_retry_count=3,
            planning_validation_error="saved diagnostic",
        ))
        ports.state.suspend_planning(ports.state.snapshot)
        ports.validation.general_error = "validation still fails"
        ports.state.activate_ready = mock.Mock(side_effect=AssertionError("new Worker"))
        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, local_environment_recheck=True))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.agent.resumed_session, "saved-session")
        self.assertIsNotNone(ports.state.snapshot.planning_error)
        self.assertGreaterEqual(ports.state.snapshot.planning_retry_count, 3)
        self.assertEqual(ports.state.snapshot.worktree, "/saved/planning")
        ports.state.activate_ready.assert_not_called()

    def test_failed_planning_recheck_is_consumed_before_command_completion(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/saved/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_attempt_fingerprint="fingerprint", planning_error="tool failure",
            planning_error_fingerprint="fingerprint", planning_retry_count=3,
            planning_validation_error="saved diagnostic"))
        ports.agent.result = PlanningAgentResult(
            1, "tool unavailable", "saved-session", PlanningAgentResultKind.TRANSIENT_FAILURE)
        context = replace(self.context, local_environment_recheck=True, recovery_request_id="request")
        outcome = PlanningLifecycleCoordinator(ports).run(context)
        self.assertEqual(outcome.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertTrue(ports.state.local_recheck_consumed("request"))
        # Reconstruct the coordinator after the failure was saved but before
        # the recover command completed its request.
        second = PlanningLifecycleCoordinator(ports).run(context)
        self.assertEqual(second.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_retry_count, 3)

    def test_local_recheck_recovers_saved_publication_without_new_planner(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/saved/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_attempt_fingerprint="fingerprint", planning_error="publication failed",
            planning_error_fingerprint="fingerprint"))
        ports.state.general_intent = SimpleNamespace(base_sha="base", commit_sha="candidate")
        ports.state.general_recovery_result = "applied"
        ports.state.activate_ready = mock.Mock(side_effect=AssertionError("new Worker"))
        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, local_environment_recheck=True))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.PUBLICATION_RECOVERY)
        self.assertEqual(ports.agent.run_count, 0)
        ports.state.activate_ready.assert_not_called()

    def test_changed_failed_input_without_workspace_starts_new_planning(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.clear_calls[0], (None, None))
        self.assertIsNone(ports.state.snapshot.planning_error)

    def test_changed_input_without_saved_workspace_keeps_session_evidence(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
            planner_session_id="saved-session",
            planning_validation_error="saved diagnostics",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                         "old-input")
        self.assertEqual(ports.state.snapshot.planner_session_id,
                         "saved-session")
        self.assertEqual(ports.state.snapshot.planning_validation_error,
                         "saved diagnostics")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)

    def test_changed_input_without_workspace_preserves_phase_or_active_sha(self):
        for phase, active_sha in (("PLANNING", None), (None, "base")):
            with self.subTest(phase=phase, active_sha=active_sha):
                ports = FakePorts(planning_snapshot(
                    phase=phase,
                    active_sha=active_sha,
                    planning_error="saved failure",
                    planning_error_fingerprint="old-input",
                ))

                outcome = PlanningLifecycleCoordinator(ports).run(self.context)

                self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                self.assertEqual(ports.state.snapshot.phase, phase)
                self.assertEqual(ports.state.snapshot.active_sha, active_sha)
                self.assertEqual(ports.state.snapshot.planning_error,
                                 "saved failure")
                self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                                 "old-input")
                self.assertEqual(ports.state.clear_calls, [])
                self.assertEqual(ports.agent.run_count, 0)

    def test_changed_failed_input_keeps_owned_workspace_and_independent_error(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="old-input",
            planning_workspace_owned=1,
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 1)

    def test_changed_failed_input_keeps_error_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="old-input",
                planning_workspace_owned=1,
                planning_error="saved failure",
                planning_error_fingerprint="old-input",
                planning_validation_error="saved diagnostics",
            ))
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "old-input")
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 1)
            self.assertNotIn(("worktree", "remove", str(worktree)),
                             [args for _repo, args, _check in ports.git.calls])

    def test_changed_failed_input_keeps_unowned_workspace_and_diagnostics(self):
        before = planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="old-input",
            planning_workspace_owned=0,
            planning_error="saved ownership failure",
            planning_error_fingerprint="old-input",
            planning_validation_error="saved diagnostics",
            planner_session_id="saved-session",
        )
        ports = FakePorts(before)
        ports.git.ownership_error = PlanningWorkspaceOwnershipError(
            "workspace owner is unknown"
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.planning_error,
                         "saved ownership failure")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                         "old-input")
        self.assertEqual(ports.state.snapshot.planning_validation_error,
                         "saved diagnostics")
        self.assertEqual(ports.state.snapshot.planner_session_id,
                         "saved-session")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)

    def test_failed_planner_does_not_activate_stale_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="specification",
            planning_fingerprint="old-full",
        ))
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.agent.result = PlanningAgentResult(
            1, "planner failed", None,
            PlanningAgentResultKind.PERMANENT_CONFIGURATION,
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertNotEqual(ports.state.snapshot.phase, "WORKING")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")


class PlanningConflictCorrectionUnitTests(PlanningCase):
    """Exercise the correction boundary with fake ports, without real Git."""

    plan_path = "docs/plans/[G-001] Plan.md"

    def setUp(self):
        super().setUp()
        from loop_manager_internal.planning.conflict_correction import PlanningConflictCorrection
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.worktree = Path(self.directory.name) / "planning"
        self.plan = self.worktree / self.plan_path
        self.plan.parent.mkdir(parents=True)
        self.plan.write_text("resolved plan\n")
        self.git = FakeGitPort()
        self.agent = FakeAgentPort()
        self.events = FakeEventPort()
        self.effects = []
        self.unmerged = self.plan_path + "\0"
        self.candidates = ""
        self.remote_diff = 0
        self.continue_code = 0
        self.index_diff = ""
        self.role = "planner"
        self.rebase_dir = self.worktree / ".git/rebase-merge"
        self.rebase_dir.mkdir(parents=True)
        original_run = self.git.run

        def run(tree, *args, check=True):
            self.effects.append(args)
            result = original_run(tree, *args, check=check)
            if args == ("diff", "--name-only", "--diff-filter=U", "-z"):
                result.stdout = self.unmerged
            elif args[:2] == ("rev-parse", "--git-path"):
                result.stdout = str(self.worktree / ".git" / args[2])
            elif args == ("-c", "core.editor=true", "rebase", "--continue"):
                result.returncode = self.continue_code
                result.stdout = "continue failed"
            elif args[:3] == ("ls-tree", "-r", "--name-only"):
                result.stdout = self.plan_path + "\n"
            elif args == ("ls-files", "--others", "--exclude-standard"):
                result.stdout = self.candidates
            elif args[:3] == ("diff", "--quiet", "HEAD"):
                result.returncode = self.remote_diff
            elif args[:4] == ("diff", "--cached", "--binary", "HEAD"):
                result.stdout = self.index_diff
            return result

        self.git.run = run
        self.git.save_planning_correction = mock.Mock(
            side_effect=lambda *args: self.effects.append(("save", *args)),
        )
        self.git.accept_resolved_stash_conflict = mock.Mock(
            side_effect=lambda *args: self.effects.append(("accept", *args)),
        )
        self.agent.begin_scope = mock.Mock(side_effect=lambda role, tree: self.effects.append(("begin", role)))
        self.agent.finish_scope = mock.Mock(return_value=PlanningScopeResult())
        self.agent.clear_scope = mock.Mock()
        self.sessions = []
        self.correction = PlanningConflictCorrection(self.git, self.agent, self.events)

    def resolve(self, **kwargs):
        return self.correction.resolve(
            "/repo", self.worktree, "target", "conflict detail", self.role,
            "saved-session", self.sessions.append, **kwargs,
        )

    def assert_no_git_writes(self):
        self.git.save_planning_correction.assert_not_called()
        self.git.accept_resolved_stash_conflict.assert_not_called()
        self.assertFalse(any(args[0] in {"add", "-c"} for args in self.effects))

    def test_scoped_turn_saves_session_before_correction_and_stages_only_unmerged_paths(self):
        invoke = self.agent.run_planner

        def planner(tree, prompt, session, callback, **kwargs):
            self.assertIn("conflict detail", prompt)
            self.assertIn("git show stash^2:", prompt)
            self.assertIn("git show stash:", prompt)
            callback("started-session")
            return invoke(tree, prompt, session, callback, **kwargs)

        self.agent.run_planner = planner
        self.assertTrue(self.resolve(stash_sha="stash"))
        self.assertEqual(self.sessions, ["started-session", "session-1"])
        self.assertEqual(self.agent.resumed_session, "saved-session")
        self.assertEqual(self.agent.invocation_metadata[-1], ("conflict_resolution", "target"))
        self.agent.begin_scope.assert_called_once_with("planner", self.worktree)
        self.agent.clear_scope.assert_called_once_with("/repo")
        save_index = next(i for i, args in enumerate(self.effects) if args[0] == "save")
        add_index = self.effects.index(("add", "--", self.plan_path))
        continue_index = self.effects.index(("-c", "core.editor=true", "rebase", "--continue"))
        self.assertLess(save_index, add_index)
        self.assertLess(add_index, continue_index)
        self.git.accept_resolved_stash_conflict.assert_not_called()

    def test_scope_error_takes_priority_and_keeps_scope_evidence(self):
        self.agent.result = replace(self.agent.result, code=1, message="agent failed")
        self.agent.finish_scope.return_value = PlanningScopeResult(error="scope rejected")
        self.assertFalse(self.resolve())
        self.assertEqual(self.sessions, ["session-1"])
        self.assertEqual(self.events.messages, [("error", "scope rejected")])
        self.agent.clear_scope.assert_not_called()
        self.assert_no_git_writes()

    def test_agent_error_after_valid_scope_does_not_save_correction(self):
        self.agent.result = replace(self.agent.result, code=1, message="agent failed")
        self.assertFalse(self.resolve())
        self.agent.clear_scope.assert_called_once()
        self.assertEqual(self.events.messages, [("error", "agent failed")])
        self.assert_no_git_writes()

    def test_session_callback_failure_propagates_without_scope_cleanup_or_git_writes(self):
        for started in (True, False):
            with self.subTest(started=started):
                def fail(_session):
                    raise RuntimeError("session persistence failed")

                self.sessions = SimpleNamespace(append=fail)
                if started:
                    self.agent.run_planner = lambda tree, prompt, session, callback, **kwargs: callback("started")
                else:
                    self.agent.run_planner = lambda *args, **kwargs: self.agent.result
                with self.assertRaisesRegex(RuntimeError, "session persistence failed"):
                    self.resolve()
                self.agent.finish_scope.assert_not_called()
                self.agent.clear_scope.assert_not_called()
                self.assert_no_git_writes()

    def test_invalid_files_are_rejected_before_correction_save(self):
        for kind in ("symlink", "directory", "marker"):
            with self.subTest(kind=kind):
                self.plan.unlink(missing_ok=True)
                if kind == "symlink":
                    symlink_or_skip(self.plan, "missing-target")
                elif kind == "directory":
                    self.plan.mkdir()
                else:
                    self.plan.write_text("<<<<<<< ours\n=======\n>>>>>>> theirs\n")
                self.assertFalse(self.resolve())
                self.assertIn("잘못된 파일 형식", self.events.messages[-1][1])
                self.assert_no_git_writes()
                if kind == "directory":
                    self.plan.rmdir()

    def test_no_conflict_or_role_outside_path_never_invokes_agent(self):
        for paths in ("", "src/code.py\0", "docs/plans/nested/Plan.md\0"):
            with self.subTest(paths=paths):
                self.unmerged = paths
                self.assertFalse(self.resolve())
                self.assertEqual(self.agent.run_count, 0)
                self.assert_no_git_writes()

    def test_manager_code_map_conflict_is_resolved_without_agent(self):
        from loop_manager_internal.maintenance.code_map_artifact import ARTIFACT_PATH
        self.unmerged = ARTIFACT_PATH + "\0"
        self.role = "maintenance_planner"
        self.git.restore_code_map_conflict = mock.Mock()
        self.assertTrue(self.resolve())
        self.git.restore_code_map_conflict.assert_called_once_with("/repo", self.worktree, "")
        self.assertEqual(self.agent.run_count, 0)
        self.assertIn(("add", "--", ARTIFACT_PATH), self.effects)
        self.git.save_planning_correction.assert_called_once_with(
            "/repo", self.worktree, "target", (ARTIFACT_PATH,), True,
        )

    def test_code_map_stash_collision_uses_proven_stash_bytes_without_agent(self):
        from loop_manager_internal.maintenance.code_map_artifact import ARTIFACT_PATH
        self.unmerged = ""
        self.rebase_dir.rmdir()
        self.role = "maintenance_planner"
        self.git.restore_code_map_conflict = mock.Mock()
        self.assertTrue(self.resolve(collision_paths=(ARTIFACT_PATH,), stash_sha="stash"))
        self.git.restore_code_map_conflict.assert_called_once_with("/repo", self.worktree, "stash")
        self.assertEqual(self.agent.run_count, 0)
        self.git.accept_resolved_stash_conflict.assert_called_once()

    def test_unproven_code_map_conflict_preserves_files_and_general_scope(self):
        from loop_manager_internal.maintenance.code_map_artifact import ARTIFACT_PATH, CodeMapArtifactError
        self.unmerged = ARTIFACT_PATH + "\0"
        self.git.restore_code_map_conflict = mock.Mock(side_effect=CodeMapArtifactError("receipt mismatch"))
        self.assertFalse(self.resolve())
        self.git.restore_code_map_conflict.assert_not_called()
        self.role = "maintenance_planner"
        self.assertFalse(self.resolve())
        self.git.restore_code_map_conflict.assert_called_once()
        self.assertEqual(self.agent.run_count, 0)
        self.assert_no_git_writes()

    def test_mixed_code_map_conflict_keeps_manager_path_out_of_agent_prompt(self):
        from loop_manager_internal.maintenance.code_map_artifact import ARTIFACT_PATH
        self.unmerged = self.plan_path + "\0" + ARTIFACT_PATH + "\0"
        self.role = "maintenance_planner"
        self.git.restore_code_map_conflict = mock.Mock()
        invoke = self.agent.run_planner

        def planner(tree, prompt, session, callback, **kwargs):
            self.assertNotIn(ARTIFACT_PATH, prompt)
            return invoke(tree, prompt, session, callback, **kwargs)

        self.agent.run_planner = planner
        self.assertTrue(self.resolve())
        self.assertEqual(self.agent.run_count, 1)
        self.assertIn(("add", "--", *sorted((ARTIFACT_PATH, self.plan_path))), self.effects)

    def test_manager_code_map_correction_uses_receipt_backed_journal(self):
        from loop_manager_internal.maintenance import code_map_artifact
        from loop_manager_internal.planning.conflict_correction import PlanningConflictCorrection
        from loop_manager_internal.planning import planning_rebase
        from loop_manager_internal.planning.git_ports import _PlanningGitPorts

        artifact_path = code_map_artifact.ARTIFACT_PATH
        contents = b'{"version":1}\n'
        oid = "a" * 40
        scenarios = (
            ("standalone", (artifact_path,), "", True),
            ("stash", (artifact_path,), "stash", False),
            ("mixed", (self.plan_path, artifact_path), "", True),
        )
        for name, paths, stash_sha, was_rebase in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                worktree = root / "planning"
                worktree.mkdir()
                rebase_dir = worktree / ".git/rebase-merge"
                if was_rebase:
                    rebase_dir.mkdir(parents=True)
                if self.plan_path in paths:
                    plan = worktree / self.plan_path
                    plan.parent.mkdir(parents=True)
                    plan.write_text("approved plan\n")
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                self.addCleanup(connection.close)
                connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO projects VALUES ('/repo')")
                connection.execute(code_map_artifact._SCHEMA)
                connection.execute(
                    "INSERT INTO maintenance_code_map_exports VALUES (?, ?, ?, ?)",
                    ("/repo", 1, "base", hashlib.sha256(contents).hexdigest()),
                )

                effects = []
                head = {"value": "base"}
                interruptions = {"add": stash_sha != "", "continue": was_rebase,
                                 "reset": stash_sha != ""}

                def git(_worktree, *args, check=True):
                    effects.append(args)
                    if args[:2] == ("merge-base", "--is-ancestor"):
                        return SimpleNamespace(stdout="", returncode=0)
                    if args == ("stash", "list", "--format=%gd%x00%H%x00%gs"):
                        value = ("stash@{0}\0stash\0loop-manager-planning-rebase:nonce\n"
                                 if stash_sha else "")
                        return SimpleNamespace(stdout=value, returncode=0)
                    if args == ("diff", "--name-only", "--diff-filter=U", "-z"):
                        unmerged = "" if stash_sha else "\0".join(paths) + "\0"
                        return SimpleNamespace(stdout=unmerged, returncode=0)
                    if args == ("rev-parse", "--git-path", "rebase-merge"):
                        return SimpleNamespace(stdout=str(rebase_dir) + "\n", returncode=0)
                    if args == ("rev-parse", "--git-path", "rebase-apply"):
                        return SimpleNamespace(stdout=str(worktree / ".git/rebase-apply") + "\n", returncode=0)
                    if args == ("rev-parse", "HEAD"):
                        return SimpleNamespace(stdout=head["value"] + "\n", returncode=0)
                    if args == ("ls-files", "-u", "-z"):
                        return SimpleNamespace(stdout="", returncode=0)
                    if args[:2] == ("diff", "--binary") or args[:3] == ("diff", "--cached", "--binary"):
                        return SimpleNamespace(stdout="", returncode=0)
                    if args[:2] == ("status", "--porcelain"):
                        return SimpleNamespace(stdout=" M docs/maintenance/code-map.jsonl\n", returncode=0)
                    if args[:1] == ("add",):
                        if interruptions["add"]:
                            interruptions["add"] = False
                            raise RuntimeError("interrupted after journal save")
                        return SimpleNamespace(stdout="", returncode=0)
                    if args[:2] == ("reset", "--soft"):
                        if interruptions["reset"]:
                            interruptions["reset"] = False
                            return SimpleNamespace(stdout="reset interrupted", returncode=1)
                        head["value"] = args[2]
                        return SimpleNamespace(stdout="", returncode=0)
                    if args == ("-c", "core.editor=true", "rebase", "--continue"):
                        code = int(interruptions["continue"])
                        interruptions["continue"] = False
                        return SimpleNamespace(stdout="continue failed" if code else "", returncode=code)
                    if args[:3] == ("ls-tree", "-r", "--name-only"):
                        return SimpleNamespace(stdout="", returncode=0)
                    if args == ("ls-files", "--others", "--exclude-standard"):
                        return SimpleNamespace(stdout="", returncode=0)
                    if args[:3] == ("diff", "--quiet", "HEAD"):
                        return SimpleNamespace(stdout="", returncode=0)
                    raise AssertionError(f"unexpected Git command: {args}")

                def nul_paths(_worktree, *args):
                    if args in (
                        ("diff", "--name-only", "HEAD", "-z"),
                        ("ls-files", "--others", "--exclude-standard", "-z"),
                        ("ls-files", "-u", "-z"),
                    ):
                        return ()
                    raise AssertionError(f"unexpected NUL Git command: {args}")

                ports = _PlanningGitPorts(
                    SimpleNamespace(
                        git=git, _nul_git_paths=nul_paths,
                        validate_planning_worktree_ownership=lambda *_args: None,
                    ),
                    connection, worktree, root / "state",
                )
                journal = planning_rebase._journal_path(root / "state", "/repo", worktree)
                planning_rebase._write(journal, {
                    "repo": "/repo", "worktree": str(worktree.resolve()),
                    "old": "old", "target": "target", "head": "base",
                    "nonce": "nonce", "dirty": True, "phase": "conflict",
                    "restored_head": "base",
                })

                def read_conflict_blob(repository, *args):
                    self.assertEqual(repository, worktree)
                    if args[0] == "ls-files":
                        return f"100644 {oid} 3\t{artifact_path}\0".encode()
                    if args[0] == "ls-tree":
                        return f"100644 blob {oid}\t{artifact_path}\0".encode()
                    if args[:2] == ("cat-file", "-s"):
                        return str(len(contents)).encode()
                    if args[:2] == ("cat-file", "blob"):
                        return contents
                    raise AssertionError(f"unexpected artifact Git command: {args}")

                with mock.patch.object(code_map_artifact, "_git", side_effect=read_conflict_blob):
                    agent = FakeAgentPort()
                    events = FakeEventPort()
                    correction = PlanningConflictCorrection(ports, agent, events)
                    sessions = []
                    def resolve():
                        return correction.resolve(
                            "/repo", worktree, "target", "conflict", "maintenance_planner",
                            "saved-session", sessions.append,
                            collision_paths=(artifact_path,) if stash_sha else (), stash_sha=stash_sha,
                        )
                    if interruptions["add"]:
                        with self.assertRaisesRegex(RuntimeError, "interrupted after journal save"):
                            resolve()
                    else:
                        self.assertFalse(resolve())
                self.assertEqual((worktree / artifact_path).read_bytes(), contents)
                record = planning_rebase._read(journal, "/repo", worktree)
                self.assertEqual(tuple(record["correction"]["paths"]), tuple(sorted(paths)))
                self.assertEqual(
                    set(record["correction"]), {"paths", "was_rebase", "head", "digest"},
                )
                artifact = worktree / artifact_path
                for column, value in (("cycle_revision", 2), ("source_revision", "other")):
                    with self.subTest(receipt_identity=column):
                        connection.execute(
                            f"UPDATE maintenance_code_map_exports SET {column} = ? WHERE repo = ?",
                            (value, "/repo"),
                        )
                        self.assertEqual(artifact.read_bytes(), contents)
                        with self.assertRaises(planning_rebase.PlanningRebaseError):
                            ports.planning_correction("/repo", worktree, "target")
                        connection.execute(
                            f"UPDATE maintenance_code_map_exports SET {column} = ? WHERE repo = ?",
                            (1 if column == "cycle_revision" else "base", "/repo"),
                        )
                self.assertEqual(planning_rebase._read(journal, "/repo", worktree), record)

                if stash_sha:
                    with self.assertRaisesRegex(planning_rebase.PlanningRebaseError, "reset --soft"):
                        resolve()
                    accepting = planning_rebase._read(journal, "/repo", worktree)
                    self.assertEqual(accepting["phase"], "accepting")
                    self.assertEqual(artifact.read_bytes(), contents)
                    resets_before_rejection = sum(args[:2] == ("reset", "--soft") for args in effects)
                    connection.execute(
                        "UPDATE maintenance_code_map_exports SET source_revision = ? WHERE repo = ?",
                        ("other", "/repo"),
                    )
                    with self.assertRaises(planning_rebase.PlanningRebaseError):
                        ports.rebase_planning_workspace(
                            "/repo", "fingerprint", worktree, "old", "target",
                        )
                    self.assertEqual(head["value"], "base")
                    self.assertEqual(
                        sum(args[:2] == ("reset", "--soft") for args in effects),
                        resets_before_rejection,
                    )
                    self.assertEqual(planning_rebase._read(journal, "/repo", worktree), accepting)
                    self.assertFalse(any(args[:2] == ("stash", "drop") for args in effects))
                    connection.execute(
                        "UPDATE maintenance_code_map_exports SET source_revision = ? WHERE repo = ?",
                        ("base", "/repo"),
                    )
                    resumed = ports.rebase_planning_workspace(
                        "/repo", "fingerprint", worktree, "old", "target",
                    )
                    self.assertTrue(resumed.changed)
                    self.assertEqual(head["value"], "target")
                    self.assertEqual(planning_rebase._read(journal, "/repo", worktree)["phase"], "restored")
                else:
                    self.assertTrue(resolve())
                self.assertEqual(agent.run_count, int(name == "mixed"))
                if name == "mixed":
                    self.assertNotIn(artifact_path, agent.prompts[-1])
                self.assertIn(("add", "--", *tuple(sorted(paths))), effects)
                if not stash_sha:
                    self.assertEqual(
                        ports.planning_correction("/repo", worktree, "target"),
                        (tuple(sorted(paths)), was_rebase),
                    )

                if not stash_sha:
                    artifact.write_bytes(b"changed")
                    with self.assertRaises(planning_rebase.PlanningRebaseError):
                        ports.save_planning_correction(
                            "/repo", worktree, "target", tuple(sorted(paths)), was_rebase,
                        )
                    with self.assertRaises(planning_rebase.PlanningRebaseError):
                        ports.planning_correction("/repo", worktree, "target")
                    self.assertEqual(
                        planning_rebase._read(journal, "/repo", worktree), record,
                    )

    def test_continue_failure_retains_saved_correction_and_session(self):
        self.continue_code = 1
        self.assertFalse(self.resolve())
        self.git.save_planning_correction.assert_called_once()
        self.assertEqual(self.sessions, ["session-1"])
        self.assertEqual(self.events.messages, [("error", "continue failed")])
        self.git.accept_resolved_stash_conflict.assert_not_called()

    def test_completed_rebase_is_not_mistaken_for_stash_acceptance(self):
        self.rebase_dir.rmdir()
        self.git.planning_correction = mock.Mock(return_value=((self.plan_path,), True))
        self.assertTrue(self.resolve())
        self.assertEqual(self.agent.run_count, 0)
        self.git.accept_resolved_stash_conflict.assert_not_called()
        self.assertNotIn(("-c", "core.editor=true", "rebase", "--continue"), self.effects)

    def test_collision_requires_separate_local_file_and_preserves_remote(self):
        self.unmerged = ""
        self.rebase_dir.rmdir()
        corrected = "docs/plans/[G-002] Local.md"
        (self.worktree / corrected).write_text("local result\n")
        for candidates, diff, accepted in (("", 0, False), (corrected, 1, False), (corrected, 0, True)):
            with self.subTest(candidates=candidates, diff=diff):
                self.candidates = candidates
                self.remote_diff = diff
                self.assertEqual(self.resolve(collision_paths=(self.plan_path,), stash_sha="stash"), accepted)
                if not accepted:
                    self.assert_no_git_writes()
        self.git.save_planning_correction.assert_called_once_with("/repo", self.worktree, "target", (), False)
        self.git.accept_resolved_stash_conflict.assert_called_once_with("/repo", self.worktree, "target")
        self.assertFalse(any(args[0] == "add" for args in self.effects))
        self.assertEqual(self.plan.read_text(), "resolved plan\n")

    def test_future_ideas_collision_requires_merge_and_scope_can_reject_maintenance_changes(self):
        self.unmerged = ""
        self.rebase_dir.rmdir()
        future = "docs/FUTURE_IDEAS.md"
        (self.worktree / future).write_text("merged ideas\n")
        self.assertFalse(self.resolve(collision_paths=(future,), stash_sha="stash"))
        self.assert_no_git_writes()
        self.role = "maintenance_planner"
        self.remote_diff = 1
        self.agent.finish_scope.return_value = PlanningScopeResult(error="barrier forbids FUTURE_IDEAS")
        self.assertFalse(self.resolve(collision_paths=(future,), stash_sha="stash"))
        self.agent.begin_scope.assert_called_with("maintenance_planner", self.worktree)
        self.assert_no_git_writes()
        self.role = "planner"
        self.agent.finish_scope.return_value = PlanningScopeResult()
        self.assertTrue(self.resolve(collision_paths=(future,), stash_sha="stash"))
        self.git.accept_resolved_stash_conflict.assert_called_once()

    def test_durable_correction_reuses_agent_only_for_new_conflict_and_rejects_drift(self):
        from loop_manager_internal.planning import planning_rebase
        state_dir = Path(self.directory.name) / "state"
        path = planning_rebase._journal_path(state_dir, "/repo", self.worktree)
        record = dict(repo="/repo", worktree=str(self.worktree.resolve()), old="old",
                      target="target", head="base", nonce="nonce", dirty=True, phase="conflict")
        planning_rebase._write(path, record)
        self.git.save_planning_correction.side_effect = lambda *args: planning_rebase.save_planning_correction(
            self.git.run, state_dir, *args,
        )
        self.git.planning_correction = lambda *args: planning_rebase.planning_correction(self.git.run, state_dir, *args)
        self.assertTrue(self.resolve())
        self.assertTrue(self.resolve())
        self.assertEqual(self.agent.run_count, 1)
        self.assertEqual(self.sessions, ["session-1"])
        for drift in ("head", "file", "index", "target"):
            with self.subTest(drift=drift):
                self.git.head = "other" if drift == "head" else "base"
                self.plan.write_text("different\n" if drift == "file" else "resolved plan\n")
                self.index_diff = "different index" if drift == "index" else ""
                self.effects.clear()
                with self.assertRaises(planning_rebase.PlanningRebaseError):
                    if drift == "target":
                        self.correction.resolve("/repo", self.worktree, "other", "", self.role,
                                                "saved-session", self.sessions.append)
                    else:
                        self.resolve()
                self.assertEqual(self.agent.run_count, 1)
                self.assertFalse(any(args[0] in {"add", "-c"} for args in self.effects))
        self.index_diff = ""
        self.git.head = "new-conflict-head"
        saved_record = planning_rebase._read(path, "/repo", self.worktree)
        saved_record["phase"] = "rebasing"
        planning_rebase._write(path, saved_record)
        self.assertTrue(self.resolve())
        self.assertEqual(self.agent.run_count, 2)

    def test_workspace_wiring_preserves_error_and_commits_base_before_cleanup(self):
        for maintenance in (False, True):
            with self.subTest(maintenance=maintenance):
                ports = FakePorts(planning_snapshot(
                    phase="PLANNING", active_sha="base", worktree=str(self.worktree),
                    planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
                    planner_session_id="saved-session", planning_error="original error",
                    planning_error_fingerprint="fingerprint", planning_result="saved result",
                ))
                ports.git.remote = "target"
                order = []
                if maintenance:
                    ports.state.cycle = maintenance_cycle(
                        phase=maintenance_state.MaintenancePhase.ASSESSMENT, cycle_base_sha="base",
                    )
                    ports.state.snapshot = replace(
                        ports.state.snapshot,
                        planning_error_fingerprint=maintenance_planning_fingerprint(ports.state.cycle, "fingerprint"),
                    )
                    ports.state.restart_stale_maintenance = lambda repo, cycle, target: (
                        order.append("cycle") or replace(cycle, cycle_base_sha=target)
                    )
                    ports.validation.prepare_maintenance_context = lambda *args: "context"
                    ports.validation.maintenance_change_context = lambda *args: "changes"
                else:
                    ports.state.supersede_unpublished_general_intent = lambda *args: order.append("intent")
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                conflict = SimpleNamespace(target="target", conflict=True, detail="conflict",
                                           conflict_paths=(), stash_sha="stash")
                completed = SimpleNamespace(target="target", conflict=False)
                results = iter((conflict, completed))
                ports.git.rebase_planning_workspace = lambda *args: order.append("observe") or next(results)
                ports.state.preserve_diagnostics = mock.Mock(side_effect=lambda *args: order.append("diagnostics"))
                ports.state.reconcile_catalog = lambda *args: order.append("catalog")
                update = ports.state.update

                def save(repo, **values):
                    if "active_sha" in values:
                        order.append("base")
                    update(repo, **values)

                ports.state.update = save

                def cleanup(*args):
                    self.assertEqual(ports.state.snapshot.active_sha, "target")
                    self.assertEqual(ports.state.snapshot.planning_error, "original error")
                    self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
                    order.append("cleanup")

                ports.git.complete_planning_rebase = cleanup
                with mock.patch("loop_manager_internal.planning.workspace_service.PlanningConflictCorrection") as boundary:
                    boundary.return_value.resolve.side_effect = lambda *args: order.append("correct") or True
                    outcome = coordinator._workspace._prepare_workspace(run)
                    args = boundary.return_value.resolve.call_args.args
                    self.assertEqual(args[:6], ("/repo", self.worktree, "target", "conflict",
                                               "maintenance_planner" if maintenance else "planner", "saved-session"))
                    self.assertTrue(callable(args[6]))
                    self.assertEqual(args[7:], ((), "stash"))
                # The unchanged failed input remains gated after base adoption.
                self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                self.assertEqual(order, ["observe", "correct", "observe", "cycle" if maintenance else "intent",
                                         "diagnostics", "base", "cleanup"])
                self.assertEqual(ports.state.snapshot.planning_result, "saved result")
                self.assertEqual(ports.state.clear_calls, [])

    def test_workspace_without_conflict_never_constructs_correction_boundary(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree=str(self.worktree),
            planning_workspace_owned=1, planner_session_id="saved-session",
        ))
        ports.git.remote = "target"
        coordinator = PlanningLifecycleCoordinator(ports)
        with mock.patch("loop_manager_internal.planning.workspace_service.PlanningConflictCorrection") as boundary:
            self.assertIsNone(coordinator._workspace._prepare_workspace(coordinator._new_run(self.context)))
            boundary.assert_not_called()
        self.assertEqual(ports.agent.run_count, 0)

    def test_workspace_correction_failure_keeps_base_and_publication_intent(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree=str(self.worktree),
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_error="original error", planning_error_fingerprint="failed-input",
        ))
        ports.git.remote = "target"
        intent = object()
        ports.state.general_intent = intent
        ports.git.rebase_planning_workspace = mock.Mock(return_value=SimpleNamespace(
            target="target", conflict=True, detail="conflict remains",
        ))
        ports.git.complete_planning_rebase = mock.Mock()
        coordinator = PlanningLifecycleCoordinator(ports)
        with mock.patch("loop_manager_internal.planning.workspace_service.PlanningConflictCorrection") as boundary:
            boundary.return_value.resolve.side_effect = RuntimeError("session persistence failed")
            outcome = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(ports.state.snapshot.active_sha, "base")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint, "failed-input")
        self.assertEqual(ports.state.snapshot.planning_error, "original error")
        self.assertIs(ports.state.general_intent, intent)
        self.assertEqual(ports.state.diagnostics, [ports.state.snapshot])
        ports.git.complete_planning_rebase.assert_not_called()
        self.assertEqual(ports.state.clear_calls, [])

    def test_unresolved_workspace_conflict_dispatches_diagnostics_without_releasing_slot(self):
        for original_error in (None, 'original error'):
            with self.subTest(original_error=original_error):
                ports = FakePorts(planning_snapshot(
                    phase='PLANNING', active_sha='base', worktree=str(self.worktree),
                    planning_workspace_owned=1, planner_session_id='saved-session',
                    planning_error=original_error, planning_error_fingerprint='failed-input' if original_error else None))
                ports.git.remote = 'target'
                ports.git.rebase_planning_workspace = mock.Mock(return_value=SimpleNamespace(
                    target='target', conflict=True, detail='conflict remains'))
                coordinator = PlanningLifecycleCoordinator(ports)
                with mock.patch('loop_manager_internal.planning.workspace_service.PlanningConflictCorrection') as boundary:
                    boundary.return_value.resolve.return_value = False
                    outcome = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))
                self.assertEqual(outcome.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
                self.assertEqual(ports.state.snapshot.planning_error, original_error)
                self.assertEqual(ports.state.snapshot.active_sha, 'base')
                self.assertEqual(ports.state.diagnostics, [ports.state.snapshot])
                self.assertTrue(ports.state.snapshot.planning_error or ports.state.snapshot.planning_validation_error)
                self.assertEqual(ports.state.clear_calls, [])
