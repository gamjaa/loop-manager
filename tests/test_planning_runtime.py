import unittest

from loop_manager_internal.planning.planning_runtime import (
    PlanningDecisionKind, PlanningLifecycleCoordinator, PlanningOutcomeKind,
    choose_planning_decision,
)
from loop_manager_internal.planning.planning_contracts import (
    _PlanningRun, local_run, snapshot_run,
)
from tests.support.planning_case import (
    PlanningCase, FakePorts, maintenance_cycle, planning_snapshot,
)


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

    def test_changed_full_input_activates_runnable_ready(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=False,
            runnable_ready=True,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(decision.kind, PlanningDecisionKind.ACTIVATE_READY)

    def test_changed_full_input_waits_or_completes_without_planning(self):
        decision = choose_planning_decision(
            has_active=False,
            specification_changed=False,
            runnable_ready=False,
            planning_input_changed=True,
            has_waiting_catalog=True,
        )

        self.assertEqual(
            decision.kind,
            PlanningDecisionKind.STABLE_WAIT,
        )


class PlanningRunConversionTest(PlanningCase):
    def test_local_run_uses_snapshot_fields_and_defaults_transient_state(self):
        run = _PlanningRun(
            context=self.context,
            row=planning_snapshot(),
            maintenance_cycle=maintenance_cycle(),
            base_sha=self.context.base_sha,
            maintenance_planning=False,
            dirty_files=["changed.md"],
            tiered_root_plan_links=({"plan_id": "P-1"},),
        )

        restored = local_run(snapshot_run(run))

        self.assertEqual(restored.dirty_files, ["changed.md"])
        self.assertEqual(restored.tiered_root_plan_links, ())


class PlanningLifecycleCoordinatorTest(PlanningCase):
    def test_matching_fingerprint_skips_agent_and_completes_no_change(self):
        ports = FakePorts(
            planning_snapshot(planning_fingerprint="fingerprint")
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.FINGERPRINT_SKIPPED)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.processed_sha, "base")
