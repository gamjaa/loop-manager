"""Planning workspace creation and ownership checks."""
from __future__ import annotations
from pathlib import Path
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
from loop_manager_internal.maintenance.maintenance_orchestration import MaintenanceOrchestrationError
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.planning.planning_service import PlanningService
from loop_manager_internal.planning.planning_contracts import (
    _PlanningRun, PlanningOutcome, PlanningWorkspaceOwnershipError,
    WORKSPACE_OWNED, WORKSPACE_RESERVED,
)

class PlanningWorkspaceService(PlanningService):
    def _prepare_workspace(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = Path(str(row.worktree or ""))
        try:
            self._ports.git.validate_workspace_identity(
                context.repo,
                str(row.planning_attempt_fingerprint or ""),
                worktree,
            )
            planning_started = bool(
                row.planner_session_id
                or row.agent_snapshot
                or row.planning_result_pending_validation
                or row.planning_result_completed
                or row.planning_validation_error
                or row.planning_retry_count
                or row.last_error
            )
            owned = row.planning_workspace_owned == WORKSPACE_OWNED
            reserved = row.planning_workspace_owned == WORKSPACE_RESERVED
            self._ports.git.make_planning_worktree(
                context.repo_path,
                worktree,
                context.base_branch,
                allow_create=not planning_started and (not owned or reserved),
                allow_existing=owned or reserved,
                ownership_reserved=reserved,
                expected_head=str(row.active_sha or ""),
                before_create=lambda: self._ports.state.update(
                    context.repo,
                    planning_workspace_owned=WORKSPACE_RESERVED,
                ),
            )
            self._ports.state.update(
                context.repo,
                planning_workspace_owned=WORKSPACE_OWNED,
            )
        except PlanningWorkspaceOwnershipError as error:
            message = f"planning workspace 소유권 검증 실패: {error}"
            self._ports.state.update(
                context.repo,
                planning_error=message,
                planning_error_fingerprint=run.current_fingerprint,
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()

        if run.maintenance_planning:
            try:
                cycle = self._ports.state.maintenance_cycle(context.repo)
                if cycle.publication_stage == "prepared":
                    maintenance_context = (
                        self._ports.validation.maintenance_context_at_revision(
                            context.repo,
                            worktree,
                            str(row.active_sha or ""),
                            cycle,
                        )
                    )
                    change_context = ""
                else:
                    maintenance_context = (
                        self._ports.validation.prepare_maintenance_context(
                            context.repo,
                            worktree,
                            str(row.active_sha or ""),
                        )
                    )
                    change_context = (
                        self._ports.validation.maintenance_change_context(
                            context.repo_path,
                            context.base_branch,
                            cycle,
                        )
                    )
            except (MaintenancePlanningError, MaintenanceOrchestrationError) as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            run.maintenance_cycle = cycle
            run.maintenance_context = maintenance_context
            run.maintenance_change_context = change_context
        else:
            try:
                run.mutable_ready_snapshot = (
                    self._ports.validation.prepare_general_context(
                        context.repo,
                        worktree,
                        str(row.active_sha or ""),
                    )
                )
            except PlanningResultInvalid as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
        run.planning_worktree = worktree
        return None
