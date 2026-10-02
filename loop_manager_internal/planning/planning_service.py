"""Shared typed lifecycle service boundary."""
from __future__ import annotations
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.planning.planning_contracts import (
    _PlanningRun, PlanningRunSnapshot, PlanningStepResult, local_run, snapshot_run,
    PlanningOutcome, PlanningOutcomeKind, PlanningLifecyclePorts,
)

class PlanningService:
    """Execute one phase against a private copy of its immutable input."""
    def __init__(self, ports: PlanningLifecyclePorts):
        self._ports = ports

    @staticmethod
    def _operational_error() -> PlanningOutcome:
        return PlanningOutcome(PlanningOutcomeKind.OPERATIONAL_ERROR, error=True)

    @staticmethod
    def _file_assessment_active(run: _PlanningRun) -> bool:
        cycle = run.maintenance_cycle
        return bool(
            run.maintenance_planning
            and cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
            and cycle.file_assessment_start_merged_count is not None
            and (not run.context.self_repository or cycle.full_assessment_completed)
        )

    def step(self, phase: str, snapshot: PlanningRunSnapshot) -> PlanningStepResult:
        run = local_run(snapshot)
        outcome = getattr(self, phase)(run)
        return PlanningStepResult(snapshot_run(run), outcome)
