import unittest

from loop_manager_internal.planning.planning_runtime import (
    PlanningDecisionKind, PlanningLifecycleCoordinator, PlanningOutcomeKind,
    choose_planning_decision,
)
from tests.support.planning_case import PlanningCase, FakePorts, planning_snapshot


class PlanningDecisionTest(unittest.TestCase):
    def test_specification_planning_precedes_active_work(self):
        decision = choose_planning_decision(
            has_active=True,
            specification_changed=True,
            runnable_ready=True,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(decision.kind, PlanningDecisionKind.RUN_SPECIFICATION_PLANNING)

    def test_specification_change_precedes_runnable_ready(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=True,
            runnable_ready=True,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(
            decision.kind,
            PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
        )

    def test_changed_full_input_precedes_runnable_ready(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=False,
            runnable_ready=True,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(decision.kind, PlanningDecisionKind.RUN_FALLBACK_PLANNING)

    def test_changed_full_input_plans_without_runnable_ready(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=False,
            runnable_ready=False,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(
            decision.kind,
            PlanningDecisionKind.RUN_FALLBACK_PLANNING,
        )


class PlanningLifecycleCoordinatorTest(PlanningCase):
    def test_matching_fingerprint_skips_agent_and_completes_no_change(self):
        ports = FakePorts(
            planning_snapshot(planning_fingerprint="fingerprint")
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.FINGERPRINT_SKIPPED)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.processed_sha, "base")
