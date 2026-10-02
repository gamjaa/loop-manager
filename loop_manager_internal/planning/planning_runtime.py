"""Planning lifecycle coordinator: dispatch durable state through phase services."""
from __future__ import annotations
from loop_manager_internal.planning.planning_contracts import (
    MAX_PLANNER_VALIDATION_RETRIES, PlanningAgentResult, PlanningAgentResultKind,
    PlanningAgentPort, PlanningContext, PlanningDecision, PlanningDecisionKind,
    PlanningEventPort, PlanningGitPort, PlanningLifecyclePorts, PlanningOutcome,
    PlanningOutcomeKind, PlanningPhase, PlanningRunSnapshot, PlanningScopeResult,
    PlanningStatePort, PlanningStepResult, PlanningValidationPort,
    PlanningWorkspaceOwnershipError, _PlanningRun, choose_planning_decision,
    snapshot_run,
)
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.planning.selection_service import PlanningSelectionService
from loop_manager_internal.planning.workspace_service import PlanningWorkspaceService
from loop_manager_internal.planning.assessment_service import PlanningAssessmentService
from loop_manager_internal.planning.publication_service import PlanningPublicationService

class PlanningLifecycleCoordinator:
    """Read the durable phase and advance the selected lifecycle step."""
    def __init__(self, ports: PlanningLifecyclePorts):
        self._ports = ports
        self._selection = PlanningSelectionService(ports)
        self._workspace = PlanningWorkspaceService(ports)
        self._assessment = PlanningAssessmentService(ports, ports.assessment)
        self._publication = PlanningPublicationService(ports)
        self._handlers = (
            (self._publication, "_recover_publication"),
            (self._selection, "_gate_preserved_error"),
            (self._selection, "_select_planning_work"),
            (self._selection, "_reconcile_inputs"),
            (self._workspace, "_prepare_workspace"),
            (self._assessment, "_run_planner_turn"),
            (self._assessment, "_validate_planner_result"),
            (self._publication, "_publish_planner_result"),
            (self._selection, "_activate_next_execplan"),
        )

    def _new_run(self, context: PlanningContext) -> _PlanningRun:
        row = self._ports.state.planning(context.repo)
        cycle = self._ports.state.maintenance_cycle(context.repo)
        maintenance_planning = bool(row.phase == "PLANNING" and cycle.phase in {
            maintenance_state.MaintenancePhase.ASSESSMENT,
            maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        })
        return _PlanningRun(context=context, row=row, maintenance_cycle=cycle,
                            base_sha=context.base_sha, maintenance_planning=maintenance_planning)

    def run(self, context: PlanningContext) -> PlanningOutcome:
        snapshot = snapshot_run(self._new_run(context))
        for service, phase in self._handlers:
            step = service.step(phase, snapshot)
            snapshot = step.snapshot
            if step.outcome is not None:
                return step.outcome
        raise RuntimeError("planning lifecycle ended without a typed outcome")
