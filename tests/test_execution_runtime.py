from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest

from execution_runtime import (
    ExecutionContext,
    ExecutionOutcome,
    ExecutionOutcomeKind,
    ExecutionPhase,
    ExecutionPhaseCoordinator,
    ExecutionPlanMetadata,
)
import loop_manager
from recovery_runtime import (
    RecoveryFailure, RecoveryObservation, RecoveryReason, decide,
)


def execution_context(phase=ExecutionPhase.IMPLEMENTING, **overrides):
    values = {
        "repo": "/repo",
        "repo_path": Path("/repo"),
        "state_dir": Path("/state"),
        "base_branch": "main",
        "base_sha": "base",
        "provider": "codex",
        "config_path": Path("/config.ini"),
        "plan": ExecutionPlanMetadata(
            "G-049",
            "Coordinator extraction",
            "docs/plans/[G-049] Coordinator extraction.md",
            "plan-sha",
            ("docs/PRODUCT.md",),
        ),
        "phase": phase,
        "branch": "loop-manager/G-049",
        "worktree": Path("/state/worktrees/repo/G-049"),
        "active_sha": "base",
        "validated_base_sha": None,
        "worker_session_id": "worker-session",
        "reviewer_session_id": "reviewer-session",
    }
    values.update(overrides)
    return ExecutionContext(**values)


class FakeWorkspacePort:
    def __init__(self, prepared):
        self.prepared = prepared
        self.calls = 0

    def prepare(self):
        self.calls += 1
        return self.prepared


class FakeWorkerPort:
    def __init__(self):
        self.calls = []

    def run(self, context):
        self.calls.append(context)
        return ExecutionOutcome.advanced(
            context.at_phase(ExecutionPhase.VERIFYING)
        )


class FakeLatestBasePort:
    def __init__(self):
        self.calls = []

    def reconcile(self, context, checkpoint):
        self.calls.append((context, checkpoint))
        return None


class FakeValidationPort:
    def __init__(self):
        self.verify_calls = []
        self.review_calls = []

    def verify(self, context, latest_base):
        self.verify_calls.append((context, latest_base))
        return ExecutionOutcome.advanced(
            context.at_phase(
                ExecutionPhase.REVIEWING,
                validated_base_sha=context.active_sha,
            )
        )

    def review(self, context):
        self.review_calls.append(context)
        return ExecutionOutcome.advanced(
            context.at_phase(ExecutionPhase.PUBLISHING)
        )


class FakePublicationPort:
    def __init__(self):
        self.calls = []

    def publish(self, context, latest_base):
        self.calls.append((context, latest_base))
        return ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)


class FakePorts:
    def __init__(self, prepared):
        self.workspace = FakeWorkspacePort(prepared)
        self.worker = FakeWorkerPort()
        self.latest_base = FakeLatestBasePort()
        self.validation = FakeValidationPort()
        self.publication = FakePublicationPort()
        self.guard_calls = []

    def prepare(self):
        return self.workspace.prepare()

    def guard(self, context):
        self.guard_calls.append(context)
        return context

    def run(self, context):
        return self.worker.run(context)

    def verify(self, context, latest_base):
        return self.validation.verify(context, self.latest_base)

    def review(self, context):
        return self.validation.review(context)

    def publish(self, context, latest_base):
        return self.publication.publish(context, self.latest_base)


class ExecutionPhaseCoordinatorTests(unittest.TestCase):
    def test_typed_git_failure_reaches_common_recovery_dispatcher(self):
        context = execution_context()
        ports = FakePorts(context)

        def fail(_context):
            raise RecoveryFailure(RecoveryReason.FETCH_FAILED, "same diagnostic")

        ports.guard = fail
        ports.recover_failure = lambda current, error: ExecutionOutcome(
            ExecutionOutcomeKind.RETRY,
            context=current,
            recovery=decide(RecoveryObservation(
                error.reason, current.repo, current.plan.plan_id,
                current.phase.value, current.plan.plan_sha, current.base_sha,
                "fetch", "worker-session", str(error),
            ), attempts=3),
        )

        outcome = ExecutionPhaseCoordinator(ports).dispatch(context)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertIs(outcome.context, context)
        self.assertEqual(ports.worker.calls, [])

    def test_context_is_immutable_and_keeps_resume_identity(self):
        context = execution_context()

        with self.assertRaises(FrozenInstanceError):
            context.branch = "other"

        resumed = context.at_phase(ExecutionPhase.VERIFYING)
        self.assertEqual(resumed.worktree, context.worktree)
        self.assertEqual(resumed.worker_session_id, "worker-session")
        self.assertEqual(resumed.plan, context.plan)

    def test_prepare_returns_workspace_outcome_without_phase_dispatch(self):
        blocked = ExecutionOutcome(
            ExecutionOutcomeKind.FINAL_BLOCK,
            detail="workspace ownership",
            error=True,
        )
        ports = FakePorts(blocked)
        coordinator = ExecutionPhaseCoordinator(ports)

        self.assertIs(coordinator.prepare(), blocked)
        self.assertEqual(ports.workspace.calls, 1)
        self.assertEqual(ports.worker.calls, [])

    def test_dispatches_only_the_current_phase_handler(self):
        implementing = execution_context()
        ports = FakePorts(implementing)
        coordinator = ExecutionPhaseCoordinator(ports)

        worker_outcome = coordinator.dispatch(implementing)
        verifying = worker_outcome.context
        verify_outcome = coordinator.dispatch(verifying)
        reviewing = verify_outcome.context
        review_outcome = coordinator.dispatch(reviewing)
        publishing = review_outcome.context
        publication_outcome = coordinator.dispatch(publishing)

        self.assertEqual(worker_outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(verify_outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(review_outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(publication_outcome.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(ports.worker.calls, [implementing])
        self.assertEqual(ports.validation.verify_calls, [(verifying, ports.latest_base)])
        self.assertEqual(ports.validation.review_calls, [reviewing])
        self.assertEqual(ports.publication.calls, [(publishing, ports.latest_base)])
        self.assertEqual(
            ports.guard_calls,
            [implementing, verifying, reviewing, publishing],
        )

    def test_stable_dependency_wait_stops_before_every_phase_port(self):
        implementing = execution_context()
        ports = FakePorts(implementing)
        ports.guard = lambda context: ExecutionOutcome(
            ExecutionOutcomeKind.STABLE_WAIT,
            context=context,
            detail="G-036",
        )

        outcome = ExecutionPhaseCoordinator(ports).dispatch(implementing)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.STABLE_WAIT)
        self.assertEqual(ports.worker.calls, [])
        self.assertEqual(ports.validation.verify_calls, [])
        self.assertEqual(ports.validation.review_calls, [])
        self.assertEqual(ports.publication.calls, [])

    def test_resume_dispatch_uses_persisted_phase_and_markers(self):
        context = execution_context(
            ExecutionPhase.PUBLISHING,
            validated_base_sha="base",
            worker_result="worker result",
            verify_result="verify result",
            review_result="review result",
            pr_url="https://example.test/pr/49",
        )
        ports = FakePorts(context)

        outcome = ExecutionPhaseCoordinator(ports).dispatch(context)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(ports.worker.calls, [])
        self.assertEqual(ports.validation.verify_calls, [])
        self.assertEqual(ports.validation.review_calls, [])
        self.assertEqual(ports.publication.calls[0][0], context)


class ExecutionOutcomeAdapterTests(unittest.TestCase):
    def test_agent_correction_defers_only_manager_rebase_and_worker_protocol(self):
        cases = (
            (RecoveryReason.REBASE_CONFLICT, ExecutionPhase.VERIFYING, 1),
            (RecoveryReason.REBASE_CONFLICT, ExecutionPhase.IMPLEMENTING,
             loop_manager.PROJECT_STEP_RETRY),
            (RecoveryReason.WORKER_CORRECTION, ExecutionPhase.IMPLEMENTING, 1),
            (RecoveryReason.WORKER_CORRECTION, ExecutionPhase.REVIEWING,
             loop_manager.PROJECT_STEP_RETRY),
        )
        for reason, phase, expected in cases:
            with self.subTest(reason=reason, phase=phase):
                decision = decide(RecoveryObservation(
                    reason, "/repo", "G-042", phase.value,
                    "plan-sha", "base-sha", "fingerprint",
                    "worker-session:head", "same failure",
                ))
                self.assertEqual(
                    loop_manager._execution_outcome_step_result(ExecutionOutcome(
                        ExecutionOutcomeKind.RETRY,
                        context=execution_context(phase),
                        recovery=decision,
                    )),
                    expected,
                )

    def test_maps_all_typed_outcomes_to_existing_step_codes(self):
        expected = {
            ExecutionOutcomeKind.SUCCESS: 0,
            ExecutionOutcomeKind.STABLE_WAIT: 0,
            ExecutionOutcomeKind.PHASE_ADVANCED: loop_manager.PROJECT_STEP_RETRY,
            ExecutionOutcomeKind.RETRY: loop_manager.PROJECT_STEP_RETRY,
            ExecutionOutcomeKind.TRANSIENT_PAUSE:
                loop_manager.PROJECT_STEP_TRANSIENT_PAUSE,
            ExecutionOutcomeKind.USAGE_PAUSE:
                loop_manager.PROJECT_STEP_USAGE_PAUSE,
            ExecutionOutcomeKind.PLANNING_PENDING:
                loop_manager.PROJECT_STEP_PLANNING_PENDING,
            ExecutionOutcomeKind.OPERATIONAL_ERROR: 1,
            ExecutionOutcomeKind.FINAL_BLOCK: 1,
        }

        for kind, result in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(
                    loop_manager._execution_outcome_step_result(
                        ExecutionOutcome(kind)
                    ),
                    result,
                )


if __name__ == "__main__":
    unittest.main()
