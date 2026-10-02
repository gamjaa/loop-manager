from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
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
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token
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

class PlanningSelectionTest(PlanningCase):
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
        for _ in range(4):
            outcome = PlanningLifecycleCoordinator(ports).run(
                replace(self.context, base_sha="advanced-base")
            )
            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot, before)
            self.assertEqual(ports.state.cycle, cycle)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.git.ownership_checks, [])

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
        ), cycle)
        ports.git.fingerprints = {"base": "old-input", "advanced-base": "new-input"}

        outcome = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="advanced-base")
        )

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertEqual(ports.git.ownership_checks,
                         [("/repo", fingerprint, Path("/planning"))])
        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertEqual(ports.state.cycle.cycle_base_sha, "advanced-base")
        self.assertEqual(ports.agent.run_count, 0)

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

    def test_source_change_with_ready_runs_planner_once_before_activation(self):
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
            runnable_ready=object(),
            project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "new-full")

        ports.state.snapshot = replace(ports.state.snapshot, phase=None)
        PlanningLifecycleCoordinator(ports).run(self.context)
        self.assertEqual(ports.agent.run_count, 1)

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
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "new-full")
        self.assertEqual(ports.state.snapshot.processed_sha, "base")

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
        self.assertEqual(ports.agent.run_count, 1)

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
        ports.git.remote = "new-base"
        ports.git.specification_fingerprints["new-base"] = "new-specification"
        ports.git.fingerprints["new-base"] = "new-full"

        first = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(first.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART)
        self.assertIsNone(ports.state.snapshot.phase)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint, "specification"
        )

        ports.git.head = "new-base"
        second = PlanningLifecycleCoordinator(ports).run(
            replace(self.context, base_sha="new-base")
        )

        self.assertEqual(second.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.agent.run_count, 2)
        self.assertEqual(
            ports.state.snapshot.specification_fingerprint,
            "new-specification",
        )

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
        self.assertEqual(ports.agent.run_count, 1)

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
