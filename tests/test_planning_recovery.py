from contextlib import contextmanager
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


from tests.support.planning_case import (
    PlanningCase, FakePorts, FakeStatePort, FakeGitPort, FakeAgentPort,
    FakeValidationPort, FakeEventPort, planning_snapshot, maintenance_cycle,
)

class PlanningRecoveryTest(PlanningCase):
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

    def test_changed_failed_input_cleans_owned_workspace_before_replanning(self):
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

        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.git.ownership_checks[0],
                         ("/repo", "old-input", Path("/planning")))
        self.assertEqual(ports.state.clear_calls[0], (None, None))
        self.assertEqual(ports.agent.run_count, 1)

    def test_changed_failed_input_keeps_error_when_cleanup_fails(self):
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
            self.assertEqual(ports.agent.run_count, 0)

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

