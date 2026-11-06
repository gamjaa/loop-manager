"""Planning input selection, safety gates, and READY activation."""
from __future__ import annotations
from pathlib import Path
import subprocess
from loop_manager_internal.maintenance.maintenance_orchestration import (
    maintenance_planning_fingerprint, cycle_scope_plan_ids, MaintenanceOrchestrationError,
)
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventoryError
from loop_manager_internal.storage import maintenance_state, runtime_state
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
from loop_manager_internal.planning.planner_check import parse_planner_check_result
from loop_manager_internal.planning.planning_workspace import planning_workspace_path, legacy_release_identity
from loop_manager_internal.planning.planning_service import PlanningService
from loop_manager_internal.planning.planning_contracts import (
    _PlanningRun, PlanningContext, PlanningDecisionKind, PlanningOutcome,
    PlanningOutcomeKind, PlanningWorkspaceOwnershipError, choose_planning_decision,
)

class PlanningSelectionService(PlanningService):
    def _maintenance_error(self, run: _PlanningRun, error: RuntimeError) -> PlanningOutcome:
        cycle = run.maintenance_cycle
        message = str(error)
        self._ports.state.update(run.context.repo, planning_error=message,
            planning_error_fingerprint=f"maintenance-cycle:{cycle.phase.value}:{cycle.revision}",
            last_error=message)
        self._ports.events.error(message)
        return self._operational_error()

    def _plan_declarations_match(
        self, context: PlanningContext, base_sha: str,
    ) -> bool:
        current = self._ports.git.plan_declarations(context.repo_path, base_sha)
        consume = getattr(self._ports.state, 'consume_owned_records', None)
        if consume is not None:
            consume(context.repo, base_sha, current)
        return self._ports.state.plan_declarations_match(context.repo, current)

    def _recover_ignored_error(self, run: _PlanningRun) -> bool:
        row, context = run.row, run.context
        recover = getattr(getattr(self._ports, 'agent', None), 'recover_ignored_scope_error', None)
        if not row.planning_error or recover is None:
            return False
        try:
            with self._ports.state.transaction():
                if self._ports.state.planning(context.repo) != row:
                    return False
                if self._ports.state.schedule(context.repo).active is not None:
                    return False
                worktree = Path(str(row.preserved_worktree or ''))
                values = (runtime_state._preserved_planning_values(row.planning_preserved_state)
                          if row.planning_preserved_state else None)
                fingerprint = (values['planning_attempt_fingerprint'] if values
                               else row.planning_attempt_fingerprint)
                self._ports.git.validate_workspace_identity(context.repo, str(fingerprint or ''), worktree)
                if self._ports.state.planning_release_pending(context.repo):
                    return False
                self._ports.state.preserve_diagnostics(context.repo, row)
                if not recover(row):
                    return False
        except (RuntimeError, OSError, ValueError):
            return False
        run.row = self._ports.state.planning(context.repo)
        return True

    def _user_decision_pending(self, context: PlanningContext, base_sha: str) -> bool:
        pending = getattr(self._ports.state, 'pending_user_decision', None)
        return bool(pending and pending(context.repo,
            self._ports.git.plan_declarations(context.repo_path, base_sha)))

    def _gate_preserved_error(self, run: _PlanningRun) -> PlanningOutcome | None:
        """Check the failed input before scheduling or stale-workspace cleanup."""

        self._recover_ignored_error(run)
        row = run.row
        if (run.context.recovery_request_id
                and self._ports.state.local_recheck_consumed(run.context.recovery_request_id)
                and not row.planning_result_completed):
            return self._operational_error()
        if run.context.local_environment_recheck and row.planning_error:
            if row.planning_preserved_state:
                if self._ports.state.schedule(run.context.repo).active is not None and not row.planning_resume_plan_id:
                    return PlanningOutcome(PlanningOutcomeKind.WAITING)
                self._ports.state.resume_planning(row)
                row = self._ports.state.planning(run.context.repo)
                run.row = row
                run.maintenance_planning = bool(not row.planning_resume_plan_id and row.phase == "PLANNING" and run.maintenance_cycle.phase in {
                    maintenance_state.MaintenancePhase.ASSESSMENT,
                    maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
                })
            # Authorize one current check, retaining failure and promotion history.
            run.error_input_changed = True
            return None
        if row.planning_preserved_state:
            decision = self._ports.state.schedule(run.context.repo)
            if getattr(decision, "project_blocked", False):
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            active = getattr(decision, 'active', None)
            if active is not None and not row.planning_resume_plan_id and (active['pr_url'] or active['pr_conflict_recovery']):
                return PlanningOutcome(PlanningOutcomeKind.EXECUTION_READY, base_sha=run.base_sha)
        correction_context = (
            f"{run.maintenance_cycle.phase.value}:{run.maintenance_cycle.revision}"
            if run.maintenance_planning else "general"
        )
        if (
            row.phase == "PLANNING"
            and row.planning_workspace_owned == 1
            and row.planning_error
            and row.planning_error == row.planning_correction_error
            and row.planning_error_incident_id is not None
            and row.planning_error_incident_id == row.planning_correction_incident_id
            and row.active_sha == row.planning_correction_sha
            and row.planning_correction_context == correction_context
        ):
            # The saved correction binding authorizes fresh validation and
            # bounded retries until that linked incident is resolved.
            run.error_input_changed = True
            return None
        if (row.phase == "PLANNING" and row.planning_workspace_owned == 1
                and row.planning_result_pending_validation
                and not row.planning_result_completed):
            # Resume an already-authorized correction after an interruption;
            # the failed-input gate still stops completed/exhausted attempts.
            return None
        if (row.phase == "PLANNING" and row.planning_workspace_owned == 1
                and row.active_sha != run.base_sha):
            # A remote advance is handled in the owned workspace. Keep any
            # independent planning error until the preserved result is checked.
            run.error_input_changed = True
            return None
        if not row.planning_error or run.publication_recovery_verified:
            return None
        cycle = run.maintenance_cycle
        related_source_changed = False
        if str(row.planning_error_fingerprint or "").startswith("maintenance-cycle:"):
            current = f"maintenance-cycle:{cycle.phase.value}:{cycle.revision}"
        else:
            run.general_fingerprint = self._ports.git.input_fingerprint(
                run.context.repo_path, run.base_sha
            )
            maintenance_error = cycle.phase in {
                maintenance_state.MaintenancePhase.ASSESSMENT,
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            } and not row.planning_resume_plan_id and (run.maintenance_planning or row.phase is None or row.planning_preserved_state)
            current = (
                maintenance_planning_fingerprint(cycle, run.general_fingerprint)
                if maintenance_error
                else run.general_fingerprint
            )
            if (
                maintenance_error
                and not cycle.include_general_planning
                and cycle.cycle_base_sha
                and cycle.cycle_base_sha != run.base_sha
            ):
                related_source_changed = (
                    self._ports.git.input_fingerprint(
                        run.context.repo_path, cycle.cycle_base_sha
                    ) != run.general_fingerprint
                )
        if not row.planning_error_fingerprint or (
            row.planning_error_fingerprint == current
            and not related_source_changed
        ):
            self._ports.events.error(
                f"계획 오류가 해결되지 않았습니다: {row.planning_error}\n"
                f"보존된 planning worktree: {row.preserved_worktree}"
            )
            if row.planning_preserved_state:
                self._ports.state.retry_diagnostic_preservation(run.context.repo, row)
                return self._continue_published_work(run)
            return self._suspend_failed_planning(run)
        if row.planning_preserved_state:
            if row.phase is not None:
                decision = self._ports.state.schedule(run.context.repo)
                if getattr(decision, 'active', None) is None:
                    return PlanningOutcome(PlanningOutcomeKind.WAITING)
                if self._ports.git.execution_blocks_planning(Path(str(row.worktree))):
                    return PlanningOutcome(PlanningOutcomeKind.WAITING)
                self._ports.state.pause_execution_for_planning(row, str(decision.active['plan_id']))
                row = self._ports.state.planning(run.context.repo)
            self._ports.state.resume_planning(row)
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)
        run.error_input_changed = True
        return None

    def _suspend_failed_planning(self, run: _PlanningRun) -> PlanningOutcome:
        row, context = run.row, run.context
        if (self._ports.state.general_publication(context.repo) is not None
                or run.maintenance_cycle.publication_stage == "prepared"):
            return self._operational_error()
        # A snapshot/session without a confirmed workspace is not safe evidence.
        if row.phase not in (None, "PLANNING"):
            return self._operational_error()
        worktree = Path(row.worktree) if row.worktree else None
        try:
            if self._ports.state.planning_release_pending(context.repo):
                raise PlanningWorkspaceOwnershipError("미종료 Agent 실행이 남아 있습니다.")
            if worktree is not None:
                if row.planning_workspace_owned != 1:
                    raise PlanningWorkspaceOwnershipError("planning worktree 소유권이 확인되지 않았습니다.")
                self._ports.git.validate_worktree_ownership(
                    context.repo, str(row.planning_attempt_fingerprint or ""), worktree,
                )
            elif (row.phase == "PLANNING" or row.active_sha or row.planning_workspace_owned
                  or row.planner_session_id or row.agent_snapshot or row.planning_result
                  or row.planning_attempt_fingerprint or row.planning_validation_error
                  or row.planning_retry_count or row.planning_result_pending_validation
                  or row.planning_result_completed):
                raise PlanningWorkspaceOwnershipError("planning worktree 경로가 저장되지 않았습니다.")
            # Preservation proves its own ownership; an unresolved operation
            # may be inspectable even though it cannot release the execution slot.
            self._ports.state.preserve_diagnostics(context.repo, row)
            self._ports.git.validate_planning_release(context.repo, worktree)
            if row.agent_snapshot:
                scope = self._ports.agent.validate_saved_scope(
                    row.agent_role or "planner", worktree, row.agent_snapshot,
                )
                if scope.error:
                    raise PlanningWorkspaceOwnershipError(scope.error)
            self._ports.state.suspend_planning(
                row, unconfirmed_plan_ids=self._unconfirmed_failed_plans(run, worktree),
            )
        except RuntimeError as error:
            self._ports.events.error(f"planning 실행 자리 보존 해제 대기: {error}")
            return self._operational_error()
        # Re-enter project synchronization so PR recovery sees the released slot.
        return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)

    def _unconfirmed_failed_plans(self, run: _PlanningRun, worktree: Path | None) -> tuple[str, ...]:
        """Retain the failed input's scope before exposing the execution slot."""
        row, context, cycle = run.row, run.context, run.maintenance_cycle
        if run.maintenance_planning and not cycle.include_general_planning:
            return ()  # Maintenance targets retain their existing cycle gate.
        rows = self._ports.state.execplan_rows(context.repo)
        ids = tuple(sorted(str(plan['plan_id']) for plan in rows))
        check = self._ports.state.planner_check(context.repo)
        if (check is not None and check['stage'] in {'result', 'handoff', 'superseded'}
                and check['base_sha'] == row.active_sha and check['result']):
            try:
                decision = parse_planner_check_result(check['result'], frozenset(ids))
                if decision.outcome in {'needs_revision', 'uncertain'}:
                    return decision.plan_ids or ids
            except PlanningResultInvalid:
                return ids
        if not row.processed_sha or not row.active_sha or worktree is None:
            return ids
        try:
            changed = self._ports.git.run(
                context.repo_path, 'diff', '--name-only', '-z', row.processed_sha, row.active_sha,
            ).stdout.split('\0')
            # Source comparisons alone cannot establish the effect of a code
            # or other unclassified change on existing goals and validation.
            if any(path and not (path in {'docs/PRODUCT.md', 'docs/ARCHITECTURE.md', 'docs/PLANS.md'}
                    or path.startswith(('docs/requirements/', 'docs/plans/'))) for path in changed):
                return ids
            affected = []
            for plan_id in ids:
                try:
                    changes = self._ports.git.relevant_document_changes(
                        worktree, row.processed_sha, row.active_sha, [], rows, (plan_id,),
                        include_active_requirements=False,
                    )
                except (RuntimeError, subprocess.SubprocessError):
                    affected.append(plan_id)  # An uncertain declaration is not independent.
                else:
                    if changes:
                        affected.append(plan_id)
            return tuple(affected)
        except (RuntimeError, subprocess.SubprocessError):
            return ids

    def _continue_published_work(self, run: _PlanningRun) -> PlanningOutcome:
        context = run.context
        decision = self._ports.state.schedule(context.repo)
        active = getattr(decision, 'active', None)
        unconfirmed = run.row.preserved_unconfirmed_plan_ids
        if (not getattr(decision, 'project_blocked', False) and active is not None
                and unconfirmed is not None and str(active['plan_id']) not in unconfirmed):
            if run.row.planning_resume_plan_id:
                self._ports.state.resume_execution_after_planning(context.repo)
            return PlanningOutcome(PlanningOutcomeKind.EXECUTION_READY, base_sha=run.base_sha)
        if (getattr(decision, "project_blocked", False)
                or decision.runnable_ready is None):
            return PlanningOutcome(PlanningOutcomeKind.WAITING)
        # The normal remote/catalog and activation gates still validate the plan.
        latest = self._fetch_remote_head(context)
        if latest != run.base_sha:
            return self._restart_for_new_input()
        selected = self._ports.state.activate_ready(context.repo, latest)
        if selected is None:
            return PlanningOutcome(PlanningOutcomeKind.WAITING)
        return PlanningOutcome(PlanningOutcomeKind.EXECUTION_READY,
                               base_sha=latest, newly_activated=True)

    def _select_planning_work(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        context = run.context
        row = run.row
        cycle = run.maintenance_cycle
        if row.planning_resume_plan_id and row.phase == 'PLANNING':
            decision = self._ports.state.schedule(context.repo)
            if getattr(decision, 'project_blocked', False):
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            if decision.active is None or str(decision.active['plan_id']) != row.planning_resume_plan_id:
                self._ports.events.error('planning 후 재개할 작업의 identity가 변경됐습니다.')
                return self._operational_error()
            run.maintenance_planning = False
            return None
        if row.phase != 'PLANNING' and (row.planning_resume_plan_id or row.phase in {
                'WORKING', 'IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING'}):
            decision = self._ports.state.schedule(context.repo)
            active = getattr(decision, 'active', None)
            if getattr(decision, 'project_blocked', False):
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            if row.planning_resume_plan_id and active is None:
                self._ports.events.error('planning 후 재개할 작업이 사라졌습니다.')
                return self._operational_error()
            if active is not None:
                run.general_fingerprint = self._ports.git.input_fingerprint(context.repo_path, run.base_sha)
                run.specification_fingerprint = self._ports.git.specification_fingerprint(context.repo_path, run.base_sha)
                if hasattr(self._ports, 'retirement') and not active['pr_url'] and not active['pr_conflict_recovery']:
                    run.retirement_pending = tuple(item for item in self._ports.retirement.pending(
                        context.repo, context.repo_path, run.base_sha) if item.related_ids)
                required = (self._specification_planning_required(row, context, run.base_sha,
                    run.specification_fingerprint, run.general_fingerprint)
                    or self._user_decision_pending(context, run.base_sha)
                    or bool(run.retirement_pending))
                if not required:
                    if row.planning_resume_plan_id:
                        self._ports.state.resume_execution_after_planning(context.repo)
                        run.row = self._ports.state.planning(context.repo)
                    return None
                if not row.planning_resume_plan_id:
                    if active['pr_url'] or active['pr_conflict_recovery']:
                        return None
                    if self._ports.state.planning_release_pending(context.repo):
                        return PlanningOutcome(PlanningOutcomeKind.WAITING)
                    if self._ports.git.execution_blocks_planning(Path(str(row.worktree))):
                        return None
                    self._ports.state.pause_execution_for_planning(row, str(active['plan_id']))
                run.row = self._ports.state.planning(context.repo)
                run.maintenance_planning = False
                run.selection = choose_planning_decision(has_active=True, specification_changed=True,
                    runnable_ready=False, planning_input_changed=True, has_waiting_catalog=True)
                return None
        if (not row.planning_resume_plan_id and row.phase == "PLANNING" and cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION
                and cycle.publication_stage != "prepared" and not row.planning_result_completed):
            worktree = Path(str(row.worktree or ""))
            try:
                if (not row.worktree or row.planning_workspace_owned != 1
                        or self._ports.state.planning_release_pending(context.repo)
                        or cycle.legacy_release_identity not in (None, legacy_release_identity(row, cycle))):
                    raise PlanningWorkspaceOwnershipError("기존 사후 단계의 workspace 해제 입력 또는 실행이 변경됐습니다.")
                self._ports.git.validate_worktree_ownership(context.repo, str(row.planning_attempt_fingerprint or ""), worktree)
                if worktree.exists():
                    self._ports.git.validate_planning_release(context.repo, worktree)
                    if self._ports.git.changed_files(worktree) or self._ports.git.run(worktree, "rev-parse", "HEAD").stdout.strip() != row.active_sha:
                        raise MaintenancePlanningError("기존 사후 재검증의 미확정 작성 결과를 보존해 게시 복구가 필요합니다.")
                    if row.agent_snapshot:
                        scope = self._ports.agent.validate_saved_scope("maintenance_planner", worktree, row.agent_snapshot)
                        if scope.error:
                            raise MaintenancePlanningError(scope.error)
                    if cycle.legacy_release_identity is None:
                        self._ports.state.preserve_diagnostics(context.repo, row)
                        cycle = self._ports.state.prepare_legacy_release(row, cycle)
                    self._ports.git.run(context.repo_path, "worktree", "remove", str(worktree))
                elif cycle.legacy_release_identity is not None:
                    self._ports.git.validate_planning_release(context.repo, worktree, allow_missing=True)
                else:
                    raise PlanningWorkspaceOwnershipError("workspace 해제 intent 없이 planning 경로가 사라졌습니다.")
                self._ports.state.complete_legacy_release(row, cycle)
            except RuntimeError as error:
                self._ports.events.error(str(error))
                return self._operational_error()
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)
        if row.phase:
            return None
        if getattr(self._ports.state.schedule(context.repo), "project_blocked", False):
            self._ports.events.info("PROJECT_BLOCKED 해제 전에는 유지보수 종료나 새 작업을 시작하지 않습니다.")
            return PlanningOutcome(PlanningOutcomeKind.WAITING)
        previous_phase = cycle.phase
        try:
            # An archive may advance base before an untouched assessment gets
            # its first workspace. Use the existing intent-free context restart;
            # saved results/workspaces still follow their own recovery path.
            if (cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
                    and cycle.cycle_base_sha != run.base_sha
                    and cycle.publication_stage is None
                    and cycle.full_assessment_result is None
                    and not cycle.full_assessment_completed
                    and not row.planning_preserved_state):
                cycle = self._ports.state.restart_stale_maintenance(context.repo, cycle, run.base_sha)
            cycle = self._ports.state.advance_maintenance_cycle(
                context.repo, run.base_sha
            )
        except (MaintenancePlanningError, maintenance_state.MaintenanceStateError) as error:
            return self._maintenance_error(run, error)
        if previous_phase is not maintenance_state.MaintenancePhase.IDLE and cycle.phase is maintenance_state.MaintenancePhase.IDLE:
            rows = self._ports.state.execplan_rows(context.repo)
            try:
                scope = cycle_scope_plan_ids(rows, cycle)
                if not scope and not cycle.cancelled_scope_plan_ids:
                    raise MaintenanceOrchestrationError('종료 범위를 확인할 수 없습니다.')
                statuses = {str(item['plan_id']): str(item['status']) for item in rows}
                merged = [plan_id for plan_id in scope if statuses.get(plan_id) == 'MERGED']
                if len(merged) != len(scope):
                    raise MaintenanceOrchestrationError('종료 후 범위 관측이 변경됐습니다.')
                facts = f"범위 내 실제 Merge={', '.join(merged) or '없음'}, 확정 취소={', '.join(cycle.cancelled_scope_plan_ids) or '없음'}"
            except MaintenanceOrchestrationError:
                facts = "종료 범위의 Merge·취소 상세 미확인"
            self._ports.events.log(f"유지보수 cycle 종료: {facts}, 남은 범위 없음, 다음 주기 기준={cycle.last_cycle_merged_count}, 일반 작업으로 복귀합니다.")
        if run.error_input_changed:
            run.maintenance_planning = cycle.phase in {
                maintenance_state.MaintenancePhase.ASSESSMENT,
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }
            run.row = row
            run.maintenance_cycle = cycle
            return None
        decision = self._ports.state.schedule(context.repo)
        if getattr(decision, "project_blocked", False):
            self._ports.events.info("PROJECT_BLOCKED 해제 전에는 새 점검이나 작업을 시작하지 않습니다.")
            return PlanningOutcome(PlanningOutcomeKind.WAITING)
        waiting_for_pr = any(str(item["status"]) == "PR_OPEN" for item in self._ports.state.execplan_rows(context.repo))
        waiting_for_manual = any(str(item["status"]) == "MANUAL_WORKING"
                                 for item in self._ports.state.execplan_rows(context.repo))
        if context.registered_only and waiting_for_pr and cycle.phase is maintenance_state.MaintenancePhase.IDLE:
            self._ports.events.log("실제 Open PR 대기: 새 due 유지보수는 PR 동기화·종료 확인 뒤 선택합니다.")
        if context.registered_only and waiting_for_manual and cycle.phase is maintenance_state.MaintenancePhase.IDLE:
            self._ports.events.log("수동 구현 대기: 새 유지보수 cycle을 보류하고 독립 일반 READY 선택을 계속합니다.")
        if (cycle.phase in {maintenance_state.MaintenancePhase.IDLE,
                maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                maintenance_state.MaintenancePhase.REFACTORING}
                and getattr(decision, "active", None) is None
                and hasattr(self._ports, "retirement")):
            run.retirement_pending = self._ports.retirement.pending(
                context.repo, context.repo_path, run.base_sha,
            )
        if (
            context.registered_only
            and cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and not waiting_for_pr
            and not waiting_for_manual
            and not run.retirement_pending
        ):
            tiered_selection = None
            try:
                due = self._ports.state.maintenance_due(
                    context.repo, context.refactoring_review_interval,
                    self_repository=context.self_repository,
                )
            except maintenance_state.MaintenanceStateError as error:
                return self._maintenance_error(run, error)
            if hasattr(self._ports, "tiered"):
                try:
                    due = self._ports.tiered.due(context)
                    if due.required:
                        tiered_selection = self._ports.tiered.select(context, run.base_sha)
                        self._ports.events.log(
                            f"유지보수 평가 시작: 이유={due.reason}, 정적={len(tiered_selection.index.records)}파일, "
                            f"범위={tiered_selection.mode}, interval={context.refactoring_review_interval}")
                    else:
                        self._ports.events.log(f"유지보수 다음 주기 대기: 이유={due.reason}, Merge={due.merged_since_baseline}/{context.refactoring_review_interval}")
                except maintenance_state.MaintenanceStateError as error:
                    return self._maintenance_error(run, error)
                except (SourceInventoryError, ValueError, OSError) as error:
                    self._ports.events.error(str(error))
                    return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART, detail=str(error), error=True)
            if due.required:
                include_general = False
                if not decision.has_ready and tiered_selection is None:
                    run.general_fingerprint = self._ports.git.input_fingerprint(
                        context.repo_path, run.base_sha
                    )
                    include_general = self._general_planning_required(
                        row, context, run.base_sha, run.general_fingerprint
                    )
                cycle, _started = self._ports.state.begin_maintenance_assessment(
                    context.repo,
                    context.refactoring_review_interval,
                    run.base_sha,
                    include_general_planning=include_general,
                    self_repository=context.self_repository,
                    **({"due_override": due} if tiered_selection is not None else {}),
                )
        if (
            waiting_for_pr
            and cycle.phase
            in {
                maintenance_state.MaintenancePhase.ASSESSMENT,
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }
        ):
            self._ports.events.info(
                "Merge 대기 PR 종료 전 유지보수 평가를 보류합니다."
            )
            return PlanningOutcome(PlanningOutcomeKind.WAITING)
        maintenance_specification_changed = False
        if cycle.phase in {
            maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
            maintenance_state.MaintenancePhase.REFACTORING,
        }:
            maintenance_specification_changed = (
                self._user_decision_pending(context, run.base_sha)
                or self._general_planning_required(
                    row, context, run.base_sha,
                    self._ports.git.input_fingerprint(context.repo_path, run.base_sha),
                )
            )
        if cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT:
            run.maintenance_planning = True
        elif not (run.retirement_pending or maintenance_specification_changed) and cycle.phase in {
            maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
            maintenance_state.MaintenancePhase.REFACTORING,
            maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        }:
            decision = self._ports.state.schedule(context.repo)
            if decision.runnable_ready is None:
                rows = self._ports.state.execplan_rows(context.repo)
                try:
                    scope = cycle_scope_plan_ids(rows,cycle)
                except MaintenanceOrchestrationError as error:
                    remaining = str(error)
                else:
                    remaining = ', '.join(f"{item['plan_id']}({item['status']})" for item in rows
                        if item['plan_id'] in scope and item['status'] != 'MERGED')
                detail = f"활성 유지보수 주기의 dependency 충족을 기다립니다. 남은 범위: {remaining}"
                self._ports.events.info(detail)
                return PlanningOutcome(PlanningOutcomeKind.WAITING,detail=detail)
            run.base_sha = self._fetch_remote_head(context)
            self._ports.state.activate_ready(context.repo, run.base_sha)
            row = self._ports.state.planning(context.repo)
            run.ready_activated_before_planning = True
        else:
            decision = self._ports.state.schedule(context.repo)
            if getattr(decision, "project_blocked", False):
                self._ports.events.info(
                    "PROJECT_BLOCKED 해제 전에는 새 Planner나 Worker를 시작하지 않습니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            run.general_fingerprint = self._ports.git.input_fingerprint(
                context.repo_path, run.base_sha
            )
            run.specification_fingerprint = (
                self._ports.git.specification_fingerprint(
                    context.repo_path, run.base_sha
                )
            )
            specification_changed = self._user_decision_pending(context, run.base_sha) or self._specification_planning_required(
                row,
                context,
                run.base_sha,
                run.specification_fingerprint,
                run.general_fingerprint,
            )
            if (hasattr(self._ports.state, "planning_review") and not specification_changed
                    and self._ports.state.final_review_candidate(context.repo,
                        context.refactoring_review_interval, self_repository=context.self_repository)):
                from loop_manager_internal.planning.specification_review import current_receipt
                run.final_review_required = not current_receipt(
                    self._ports.state.planning_review(context.repo, "final"),
                    repo=context.repo_path, revision=run.base_sha,
                    input_fingerprint=run.general_fingerprint,
                    specification_fingerprint=run.specification_fingerprint,
                    work_cycle=self._ports.state.review_work_cycle(context.repo),
                    archive_only=lambda *args, **kwargs: (
                        self._plan_declarations_match(context, run.base_sha)
                        and self._ports.git.requirement_archive_only(*args, **kwargs)),
                )
            run.selection = choose_planning_decision(
                has_active=getattr(decision, "active", None) is not None,
                specification_changed=(specification_changed or bool(run.retirement_pending)
                                       or run.final_review_required),
                runnable_ready=decision.runnable_ready is not None,
                planning_input_changed=specification_changed,
                has_waiting_catalog=bool(
                    decision.has_ready or decision.has_waiting
                ),
            )
            if run.selection.kind is PlanningDecisionKind.ACTIVATE_READY:
                latest_sha = self._fetch_remote_head(context)
                if self._remote_base_changed_before_activation(run, latest_sha):
                    return self._restart_for_new_input()
                run.base_sha = latest_sha
                self._ports.state.activate_ready(context.repo, run.base_sha)
                row = self._ports.state.planning(context.repo)
                run.ready_activated_before_planning = True
            elif run.selection.kind is PlanningDecisionKind.STABLE_WAIT:
                self._ports.events.info(
                    "계획 입력이 안정적이며 실행 가능한 READY가 없어 대기합니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
        run.row = row
        run.maintenance_cycle = cycle
        return None

    def execution_planning_required(self, context: PlanningContext) -> bool:
        """Reuse the project's successful specification baseline during execution."""
        row = self._ports.state.planning(context.repo)
        return self._user_decision_pending(context, context.base_sha) or self._general_planning_required(
            row, context, context.base_sha,
            self._ports.git.input_fingerprint(context.repo_path, context.base_sha),
        )

    def _general_planning_required(
        self,
        row: runtime_state.PlanningSnapshot,
        context: PlanningContext,
        base_sha: str,
        current_fingerprint: str,
    ) -> bool:
        return self._specification_planning_required(
            row, context, base_sha,
            self._ports.git.specification_fingerprint(context.repo_path, base_sha),
            current_fingerprint,
        )

    def _specification_planning_required(
        self,
        row: runtime_state.PlanningSnapshot,
        context: PlanningContext,
        base_sha: str,
        current_fingerprint: str,
        current_general_fingerprint: str,
    ) -> bool:
        if getattr(self._ports.git, 'runtime_guidance_changed', lambda _repo: False)(context.repo_path):
            return True
        baseline = row.specification_fingerprint
        if baseline is None and not row.processed_sha:
            reference = getattr(self._ports.state, 'general_reference_revision', lambda _repo: None)(context.repo)
            if reference:
                baseline = self._ports.git.specification_fingerprint(context.repo_path, reference)
        if baseline is None and row.planning_fingerprint == current_general_fingerprint:
            # Existing databases already have a successful full-input marker.
            # Treat an identical full input as the corresponding specification
            # baseline and persist the explicit value at the next success.
            return False
        if baseline is None and row.planning_fingerprint and row.processed_sha:
            baseline = self._ports.git.specification_fingerprint(
                context.repo_path, row.processed_sha
            )
        if baseline == current_fingerprint:
            return False
        if row.processed_sha:
            processed = self._ports.git.specification_fingerprint(
                context.repo_path, row.processed_sha
            )
            if processed == current_fingerprint:
                return False
            if self._ports.git.requirement_archive_only(
                context.repo_path, row.processed_sha, base_sha,
                specification_only=True,
            ):
                return False
        return True

    def _remote_base_changed_before_activation(
        self, run: _PlanningRun, latest_sha: str
    ) -> bool:
        # Even a plan-only revision can reorder READY declarations. Re-enter
        # the project step so catalog reconciliation reads the new revision.
        return not run.maintenance_planning and latest_sha != run.base_sha

    def _restart_for_new_input(self) -> PlanningOutcome:
        self._ports.events.info(
            "READY 시작 전 원격 기준이 변경되어 최신 catalog와 계획 입력을 다시 선택합니다."
        )
        return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)

    def _reconcile_inputs(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        context = run.context
        row = run.row
        cycle = run.maintenance_cycle
        if (
            run.maintenance_planning
            and cycle.cycle_base_sha != run.base_sha
            and cycle.publication_stage is None
            and row.phase == "PLANNING"
            and not row.planning_error
            and row.planning_workspace_owned != 1
        ):
            stale_worktree = Path(str(row.worktree or ""))
            stored_fingerprint = str(row.planning_attempt_fingerprint or "")
            self._ports.git.validate_worktree_ownership(
                context.repo, stored_fingerprint, stale_worktree
            )
            self._ports.state.preserve_diagnostics(context.repo, row)
            with self._ports.state.transaction():
                cycle = self._ports.state.restart_stale_maintenance(
                    context.repo, cycle, run.base_sha
                )
                self._ports.state.clear_active_in_transaction(context.repo)
            if stale_worktree.exists():
                self._ports.git.run(
                    context.repo_path,
                    "worktree",
                    "remove",
                    "--force",
                    str(stale_worktree),
                )
            row = self._ports.state.planning(context.repo)

        fingerprint_required = bool(
            run.maintenance_planning or not row.phase or row.phase == "PLANNING"
        )
        if fingerprint_required and run.general_fingerprint is None:
            run.general_fingerprint = self._ports.git.input_fingerprint(
                context.repo_path, run.base_sha
            )
        if fingerprint_required and run.specification_fingerprint is None:
            run.specification_fingerprint = (
                self._ports.git.specification_fingerprint(
                    context.repo_path, run.base_sha
                )
            )
        run.current_fingerprint = (
            maintenance_planning_fingerprint(
                cycle, run.general_fingerprint or ""
            )
            if run.maintenance_planning
            else run.general_fingerprint
            if fingerprint_required
            else None
        )
        recoverable_publication = bool(
            run.publication_recovery_verified
            or (
                run.maintenance_planning
                and row.phase == "PLANNING"
                and cycle.publication_stage == "prepared"
            )
        )
        if (
            row.planning_error
            and row.planning_error_fingerprint == run.current_fingerprint
            and not recoverable_publication
            and not run.error_input_changed
            and not (row.planning_result_pending_validation
                     and not row.planning_result_completed)
        ):
            self._ports.events.error(
                f"계획 오류가 해결되지 않았습니다: {row.planning_error}\n"
                f"보존된 planning worktree: {row.preserved_worktree}"
            )
            return self._operational_error()

        if (row.planning_error and not recoverable_publication
                and row.planning_workspace_owned != 1):
            outcome = self._discard_stale_planning(run, row)
            if outcome is not None:
                return outcome
            row = self._ports.state.planning(context.repo)
            if (
                run.maintenance_planning
                and cycle.cycle_base_sha != run.base_sha
                and cycle.publication_stage is None
            ):
                cycle = self._ports.state.restart_stale_maintenance(
                    context.repo, cycle, run.base_sha
                )
                run.maintenance_cycle = cycle
                return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)

        if row.phase == "PLANNING":
            started_fingerprint = (
                row.planning_attempt_fingerprint or run.current_fingerprint
            )
            if (started_fingerprint != run.current_fingerprint
                    and row.planning_workspace_owned != 1):
                outcome = self._discard_stale_planning(
                    run,
                    row,
                    fingerprint=str(started_fingerprint or ""),
                    preserve=bool(row.planning_validation_error),
                )
                if outcome is not None:
                    return outcome
                row = self._ports.state.planning(context.repo)

        planning_required = not row.phase
        if run.fingerprint_skipped and not row.phase:
            planning_required = False
        forced_general_planning = bool(
            run.selection
            and run.selection.kind
            in {
                PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
                PlanningDecisionKind.RUN_FALLBACK_PLANNING,
            }
        )
        if (not run.maintenance_planning and not row.phase
                and not forced_general_planning
                and not self._specification_planning_required(
                    row, context, run.base_sha, run.specification_fingerprint,
                    run.current_fingerprint)):
            self._ports.events.info("명세가 바뀌지 않아 Planner를 실행하지 않습니다.")
            if row.planning_fingerprint == run.current_fingerprint:
                self._ports.state.update(context.repo, processed_sha=run.base_sha,
                    specification_fingerprint=run.specification_fingerprint)
                row = self._ports.state.planning(context.repo)
            planning_required = False
            run.fingerprint_skipped = True

        if planning_required:
            assert run.current_fingerprint is not None
            if (run.selection and run.selection.kind in {
                    PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
                    PlanningDecisionKind.RUN_FALLBACK_PLANNING}):
                old_check = getattr(self._ports.state, "planner_check", lambda _repo: None)(context.repo)
                if old_check is not None and old_check["stage"] != "superseded":
                    superseded = self._ports.state.advance_planner_check(
                        context.repo, str(old_check["stage"]), "superseded",
                        expected_input=str(old_check["input_fingerprint"]),
                        expected_target=str(old_check["target_fingerprint"]),
                    )
                    if not superseded:
                        self._ports.events.error("일반 Planner 시작 전 점검 상태가 변경됐습니다.")
                        return self._operational_error()
            worktree = planning_workspace_path(
                context.state_dir, context.repo, run.current_fingerprint
            )
            self._ports.state.update(
                context.repo,
                active_sha=run.base_sha,
                plan_sha=None,
                planning_attempt_fingerprint=run.current_fingerprint,
                planning_workspace_owned=0,
                planning_retry_count=0,
                planner_session_id=None,
                worker_session_id=None,
                worker_result=None,
                verify_result=None,
                planning_validation_error=None,
                planning_result_pending_validation=0,
                planning_result_completed=0,
                planning_result=None,
                planning_error=None,
                planning_error_fingerprint=None,
                phase="PLANNING",
                branch=None,
                worktree=str(worktree),
                plan_path=None,
                pr_url=None,
                last_error=None,
            )
            row = self._ports.state.planning(context.repo)
        run.row = row
        run.maintenance_cycle = cycle
        return None

    def _discard_stale_planning(
        self,
        run: _PlanningRun,
        row: runtime_state.PlanningSnapshot,
        *,
        fingerprint: str | None = None,
        preserve: bool = True,
    ) -> PlanningOutcome | None:
        context = run.context
        worktree = Path(row.worktree) if row.worktree else None
        stored_fingerprint = fingerprint or str(
            row.planning_attempt_fingerprint
            or row.planning_error_fingerprint
            or ""
        )
        try:
            if worktree is None:
                if (
                    row.phase == "PLANNING"
                    or row.active_sha
                    or row.planning_workspace_owned
                    or row.planning_attempt_fingerprint
                    or row.planning_retry_count
                    or row.planner_session_id
                    or row.planning_validation_error
                    or row.planning_result_pending_validation
                    or row.planning_result_completed
                    or row.planning_result
                    or row.agent_snapshot
                    or row.agent_result_snapshot
                ):
                    raise RuntimeError("planning worktree 경로가 저장되지 않았습니다.")
            else:
                self._ports.git.validate_worktree_ownership(
                    context.repo, stored_fingerprint, worktree
                )
        except RuntimeError as error:
            message = f"planning workspace cleanup 소유권 검증 실패: {error}"
            self._ports.state.update(
                context.repo,
                planning_error=row.planning_error or message,
                planning_error_fingerprint=(
                    row.planning_error_fingerprint or run.current_fingerprint
                ),
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
        if preserve:
            self._ports.state.preserve_diagnostics(context.repo, row)
        try:
            if worktree is not None and worktree.exists():
                self._ports.git.run(
                    context.repo_path,
                    "worktree",
                    "remove",
                    str(worktree),
                )
        except RuntimeError as error:
            message = f"planning workspace cleanup 실패: {error}"
            self._ports.state.update(
                context.repo,
                planning_error=row.planning_error or message,
                planning_error_fingerprint=(
                    row.planning_error_fingerprint or run.current_fingerprint
                ),
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
        self._ports.state.clear_active(context.repo)
        return None

    def _fetch_remote_head(self, context: PlanningContext) -> str:
        self._ports.git.fetch_base(context.repo_path, context.base_branch)
        return self._ports.git.run(
            context.repo_path,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{context.base_branch}^{{commit}}",
        ).stdout.strip()

    def _activate_next_execplan(
        self, run: _PlanningRun
    ) -> PlanningOutcome:
        context = run.context
        row = run.row
        if context.local_environment_recheck:
            return PlanningOutcome(PlanningOutcomeKind.NO_CHANGE, base_sha=run.base_sha)
        newly_activated = run.ready_activated_before_planning
        if (hasattr(self._ports, "tiered") and run.maintenance_planning and not row.phase
                and self._ports.state.maintenance_cycle(context.repo).phase is maintenance_state.MaintenancePhase.IDLE):
            self._ports.events.log("유지보수 cycle 완료: 일반 작업 선택으로 복귀합니다.")
            return PlanningOutcome(PlanningOutcomeKind.MAINTENANCE_COMPLETE)
        if not row.phase:
            latest_sha = self._fetch_remote_head(context)
            if self._remote_base_changed_before_activation(run, latest_sha):
                return self._restart_for_new_input()
            decision = self._ports.state.schedule(context.repo)
            if row.planning_resume_plan_id:
                if getattr(decision, 'active', None) is None:
                    return self._operational_error()
                self._ports.state.resume_execution_after_planning(context.repo)
                return PlanningOutcome(PlanningOutcomeKind.EXECUTION_READY,
                                       base_sha=latest_sha, newly_activated=False)
            if not decision.has_ready and decision.has_waiting:
                self._ports.events.info(
                    "독립 READY가 없어 PLAN_BLOCKED 또는 PR_OPEN 종료를 기다립니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            if not decision.has_ready:
                latest_fingerprint = self._ports.git.input_fingerprint(
                    context.repo_path, latest_sha
                )
                expected_fingerprint = (
                    run.general_fingerprint
                    if run.maintenance_planning
                    else run.current_fingerprint
                )
                assert expected_fingerprint is not None
                if latest_fingerprint != expected_fingerprint:
                    self._ports.events.error(
                        "계획 중 입력이 변경되어 최신 기준으로 다시 계획합니다."
                    )
                    return PlanningOutcome(
                        PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                        error=True,
                    )
            elif decision.runnable_ready is None:
                self._ports.events.info(
                    "READY ExecPlan이 남아 있지만 dependency가 충족되지 않아 대기합니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            run.base_sha = latest_sha
            selected = self._ports.state.activate_ready(
                context.repo, run.base_sha
            )
            if selected is None:
                self._ports.events.info("실행 가능한 READY ExecPlan이 없습니다.")
                return PlanningOutcome(
                    PlanningOutcomeKind.FINGERPRINT_SKIPPED
                    if run.fingerprint_skipped
                    else PlanningOutcomeKind.NO_CHANGE
                )
            newly_activated = True
        return PlanningOutcome(
            PlanningOutcomeKind.REFACTORING
            if run.published_refactoring
            else PlanningOutcomeKind.EXECUTION_READY,
            base_sha=run.base_sha,
            newly_activated=newly_activated,
        )
