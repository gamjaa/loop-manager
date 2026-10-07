"""Planning lifecycle coordinator: dispatch durable state through phase services."""
from __future__ import annotations
import time
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
from loop_manager_internal.planning.check_service import PlanningCheckService

class PlanningLifecycleCoordinator:
    """Read the durable phase and advance the selected lifecycle step."""
    def __init__(self, ports: PlanningLifecyclePorts):
        self._ports = ports
        self._selection = PlanningSelectionService(ports)
        self._workspace = PlanningWorkspaceService(ports)
        self._assessment = PlanningAssessmentService(ports, ports.assessment)
        self._check = PlanningCheckService(ports)
        self._publication = PlanningPublicationService(ports)
        self._handlers = (
            (self._publication, "_recover_publication"),
            (self._selection, "_gate_preserved_error"),
            (self._selection, "_select_planning_work"),
            (self._selection, "_reconcile_inputs"),
            (self._check, "_resume_accepted"),
            (self._check, "_resume_handoff"),
            (self._workspace, "_prepare_workspace"),
            (self._check, "_run_check"),
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
        started, completed = time.monotonic(), 0
        while True:
            # Reload state and pass the normal input/workspace gates between batches.
            snapshot = snapshot_run(self._new_run(context))
            for service, phase in self._handlers:
                step = service.step(phase, snapshot)
                snapshot = step.snapshot
                outcome = step.outcome
                if outcome is None:
                    continue
                if outcome.kind is PlanningOutcomeKind.MAINTENANCE_COMPLETE:
                    from dataclasses import replace
                    latest = self._ports.git.fetch_base(context.repo_path, context.base_branch)
                    self._ports.state.reconcile_catalog(context.repo, context.repo_path, context.base_branch, latest)
                    context = replace(context, base_sha=latest)
                    if time.monotonic() - started >= ASSESSMENT_RUN_SECONDS:
                        return PlanningOutcome(PlanningOutcomeKind.NO_CHANGE)
                    break
                if outcome.kind is not PlanningOutcomeKind.ASSESSMENT_PROGRESS:
                    return outcome
                if context.local_environment_recheck:
                    return PlanningOutcome(PlanningOutcomeKind.WAITING)
                completed += 1
                if (completed >= MAX_ASSESSMENT_BATCHES
                    or time.monotonic() - started >= ASSESSMENT_RUN_SECONDS):
                    self._ports.events.log(
                        f"유지보수 실행 한도 도달: {completed}개 묶음 저장, 다음 실행에서 이어갑니다.")
                    return PlanningOutcome(PlanningOutcomeKind.WAITING)
                latest = self._ports.git.fetch_base(context.repo_path, context.base_branch)
                if latest != context.base_sha:
                    return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART, base_sha=latest)
                break
            else:
                raise RuntimeError("planning lifecycle ended without a typed outcome")


ASSESSMENT_RUN_SECONDS = 1800
MAX_ASSESSMENT_BATCHES = 128
