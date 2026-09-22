from pathlib import Path
import unittest

import loop_manager
from planning_runtime import (
    PhaseDisposition,
    PlanningContext,
    PlanningLifecycleCoordinator,
    PlanningOutcomeKind,
    PlanningPhase,
    PlanningPhaseResult,
)


class FakePlanningPorts:
    def __init__(
        self,
        terminal_phase: PlanningPhase = PlanningPhase.ACTIVATION,
        outcome: PlanningOutcomeKind = PlanningOutcomeKind.EXECUTION_READY,
    ):
        self.terminal_phase = terminal_phase
        self.outcome = outcome
        self.calls: list[PlanningPhase] = []

    def _result(self, phase: PlanningPhase) -> PlanningPhaseResult:
        self.calls.append(phase)
        if phase is self.terminal_phase:
            return PlanningPhaseResult.complete(phase, self.outcome)
        return PlanningPhaseResult.advance(phase)

    def recover_publication(self, _context):
        return self._result(PlanningPhase.RECOVERY)

    def select_planning_work(self, _context):
        return self._result(PlanningPhase.SELECTION)

    def reconcile_inputs(self, _context):
        return self._result(PlanningPhase.INPUTS)

    def prepare_workspace(self, _context):
        return self._result(PlanningPhase.WORKSPACE)

    def run_planner_turn(self, _context):
        return self._result(PlanningPhase.AGENT)

    def validate_planner_result(self, _context):
        return self._result(PlanningPhase.VALIDATION)

    def publish_planner_result(self, _context):
        return self._result(PlanningPhase.PUBLICATION)

    def activate_next_execplan(self, _context):
        return self._result(PlanningPhase.ACTIVATION)


class PlanningLifecycleCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.context = PlanningContext(
            repo="/repo",
            repo_path=Path("/repo"),
            state_dir=Path("/state"),
            base_branch="main",
            base_sha="base-sha",
            provider="codex",
            config_path=Path("/config.ini"),
            registered_only=True,
            refactoring_review_interval=5,
        )

    def test_full_lifecycle_runs_ordered_phases_and_returns_execution_handoff(self):
        ports = FakePlanningPorts()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.EXECUTION_READY)
        self.assertEqual(ports.calls, list(PlanningPhase))

    def test_typed_outcomes_stop_at_the_responsible_phase(self):
        scenarios = (
            (PlanningPhase.RECOVERY, PlanningOutcomeKind.PUBLICATION_RECOVERY),
            (PlanningPhase.VALIDATION, PlanningOutcomeKind.PREREQUISITE_WAIT),
            (PlanningPhase.ACTIVATION, PlanningOutcomeKind.FINGERPRINT_SKIPPED),
            (PlanningPhase.AGENT, PlanningOutcomeKind.TRANSIENT_PAUSE),
            (PlanningPhase.VALIDATION, PlanningOutcomeKind.VALIDATION_RETRY),
            (PlanningPhase.VALIDATION, PlanningOutcomeKind.UNSAFE_REVALIDATION),
            (PlanningPhase.ACTIVATION, PlanningOutcomeKind.NO_CHANGE),
            (PlanningPhase.PUBLICATION, PlanningOutcomeKind.REMOTE_BASE_RESTART),
            (
                PlanningPhase.PUBLICATION,
                PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
            ),
            (PlanningPhase.ACTIVATION, PlanningOutcomeKind.REFACTORING),
        )
        phases = list(PlanningPhase)
        for terminal_phase, outcome_kind in scenarios:
            with self.subTest(outcome=outcome_kind.value):
                ports = FakePlanningPorts(terminal_phase, outcome_kind)
                outcome = PlanningLifecycleCoordinator(ports).run(self.context)
                self.assertEqual(outcome.kind, outcome_kind)
                self.assertEqual(
                    ports.calls,
                    phases[: phases.index(terminal_phase) + 1],
                )

    def test_phase_result_contract_rejects_ambiguous_control_flow(self):
        with self.assertRaisesRegex(ValueError, "advance"):
            PlanningPhaseResult(
                PlanningPhase.AGENT,
                PhaseDisposition.ADVANCE,
                FakePlanningPorts()._result(PlanningPhase.ACTIVATION).outcome,
            )
        with self.assertRaisesRegex(ValueError, "requires"):
            PlanningPhaseResult(
                PlanningPhase.AGENT,
                PhaseDisposition.COMPLETE,
            )

    def test_process_adapter_preserves_existing_step_results(self):
        expected = {
            PlanningOutcomeKind.EXECUTION_READY: None,
            PlanningOutcomeKind.REFACTORING: None,
            PlanningOutcomeKind.NO_CHANGE: 0,
            PlanningOutcomeKind.FINGERPRINT_SKIPPED: 0,
            PlanningOutcomeKind.WAITING: 0,
            PlanningOutcomeKind.PREREQUISITE_WAIT: loop_manager.PROJECT_STEP_RETRY,
            PlanningOutcomeKind.REMOTE_BASE_RESTART: loop_manager.PROJECT_STEP_RETRY,
            PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED: 1,
            PlanningOutcomeKind.PUBLICATION_RECOVERY: loop_manager.PROJECT_STEP_RETRY,
            PlanningOutcomeKind.TRANSIENT_PAUSE: loop_manager.PROJECT_STEP_TRANSIENT_PAUSE,
            PlanningOutcomeKind.USAGE_LIMIT_PAUSE: loop_manager.PROJECT_STEP_USAGE_PAUSE,
            PlanningOutcomeKind.VALIDATION_RETRY: 1,
            PlanningOutcomeKind.UNSAFE_REVALIDATION: 1,
            PlanningOutcomeKind.OPERATIONAL_ERROR: 1,
        }
        for kind, step_result in expected.items():
            with self.subTest(kind=kind.value):
                outcome = PlanningPhaseResult.complete(
                    PlanningPhase.ACTIVATION, kind
                ).outcome
                assert outcome is not None
                self.assertEqual(
                    loop_manager._planning_outcome_step_result(outcome),
                    step_result,
                )


if __name__ == "__main__":
    unittest.main()
