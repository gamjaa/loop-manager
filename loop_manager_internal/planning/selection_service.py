"""Planning input selection, safety gates, and READY activation."""
from __future__ import annotations
import hashlib
from pathlib import Path
from loop_manager_internal.maintenance.maintenance_orchestration import assessment_can_yield, maintenance_planning_fingerprint
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventoryError
from loop_manager_internal.storage import maintenance_state, maintenance_file_ledger, runtime_state
from loop_manager_internal.planning.planning_service import PlanningService
from loop_manager_internal.planning.planning_contracts import (
    _PlanningRun, PlanningContext, PlanningDecisionKind, PlanningOutcome,
    PlanningOutcomeKind, choose_planning_decision,
)

class PlanningSelectionService(PlanningService):
    def _plan_declarations_match(
        self, context: PlanningContext, base_sha: str,
    ) -> bool:
        current = self._ports.git.plan_declarations(context.repo_path, base_sha)
        return self._ports.state.plan_declarations_match(context.repo, current)

    def _gate_preserved_error(self, run: _PlanningRun) -> PlanningOutcome | None:
        """Check the failed input before scheduling or stale-workspace cleanup."""

        row = run.row
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
            } and (run.maintenance_planning or row.phase is None)
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
                f"보존된 planning worktree: {row.worktree}"
            )
            return self._operational_error()
        run.error_input_changed = True
        return None

    def _select_planning_work(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        context = run.context
        row = run.row
        cycle = run.maintenance_cycle
        if (
            row.phase == "PLANNING"
            and assessment_can_yield(cycle)
            and not hasattr(self._ports, "tiered")
            and not row.planning_error
            and not row.planning_validation_error
            and self._ports.state.general_publication(context.repo) is None
        ):
            decision = self._ports.state.schedule(context.repo)
            if (decision.runnable_ready is not None
                and getattr(decision, "active", None) is None
                and not getattr(decision, "project_blocked", False)):
                worktree = Path(str(row.worktree or ""))
                self._ports.git.validate_worktree_ownership(
                    context.repo, str(row.planning_attempt_fingerprint or ""), worktree,
                )
                clean = not self._ports.git.run(worktree, "status", "--porcelain").stdout.strip()
                head = self._ports.git.run(worktree, "rev-parse", "HEAD").stdout.strip()
                if clean and head == row.active_sha:
                    self._ports.state.preserve_diagnostics(context.repo, row)
                    with self._ports.state.transaction():
                        if worktree.exists():
                            self._ports.git.run(context.repo_path, "worktree", "remove", str(worktree))
                        self._ports.state.clear_active_in_transaction(
                            context.repo,
                            processed_sha=row.processed_sha,
                            specification_fingerprint=row.specification_fingerprint,
                            planning_fingerprint=row.planning_fingerprint,
                        )
                    return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)
        if row.phase:
            return None
        try:
            cycle = self._ports.state.advance_maintenance_cycle(
                context.repo, run.base_sha
            )
        except MaintenancePlanningError as error:
            message = str(error)
            fingerprint = f"maintenance-cycle:{cycle.phase.value}:{cycle.revision}"
            self._ports.state.update(
                context.repo,
                planning_error=message,
                planning_error_fingerprint=fingerprint,
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
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
        waiting_for_pr = decision.has_waiting
        if hasattr(self._ports, "tiered"):
            waiting_for_pr = any(str(item["status"]) == "PR_OPEN" for item in self._ports.state.execplan_rows(context.repo))
        approval_pending = (
            context.self_repository
            and cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and self._ports.state.pending_document_approval(
                context.repo, context.repo_path, context.base_branch,
            )
        )
        if (
            context.registered_only
            and cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and not waiting_for_pr
            and not approval_pending
        ):
            completion_candidate = bool(
                not decision.has_ready and not decision.has_waiting
                and getattr(decision, "active", None) is None
                and not getattr(decision, "project_blocked", False)
                and row.planning_fingerprint
            )
            if completion_candidate:
                run.general_fingerprint = self._ports.git.input_fingerprint(
                    context.repo_path, run.base_sha,
                )
                run.specification_fingerprint = self._ports.git.specification_fingerprint(
                    context.repo_path, run.base_sha,
                )
                completion_candidate = not (
                    self._general_planning_required(
                        row, context, run.base_sha, run.general_fingerprint,
                    ) or self._specification_planning_required(
                        row, context, run.base_sha,
                        run.specification_fingerprint, run.general_fingerprint,
                    )
                )
            tiered_selection = None
            if hasattr(self._ports, "tiered"):
                try:
                    tiered_selection = self._ports.tiered.select(context, run.base_sha)
                    self._ports.events.log(
                        f"유지보수 정적 분석: {len(tiered_selection.index.records)}파일, "
                        f"갱신 {tiered_selection.index.parsed}, 재사용 {tiered_selection.index.reused}, "
                        f"선택 {tiered_selection.mode}, 이유={','.join(tiered_selection.reasons)}")
                except (SourceInventoryError, ValueError, OSError) as error:
                    self._ports.events.error(str(error))
                    return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART, detail=str(error), error=True)
            due = (maintenance_state.ReviewDue(tiered_selection.required,
                tiered_selection.mode, 0, cycle.phase) if tiered_selection is not None else self._ports.state.maintenance_due(
                context.repo, context.refactoring_review_interval,
                self_repository=context.self_repository,
                completion_candidate=completion_candidate,
            ))
            if due.reason == "stabilization_due" and hasattr(
                self._ports, "file_review"
            ):
                try:
                    empty_inventory = self._ports.file_review.empty_selection_inventory(
                        context.repo, context.repo_path, context.base_branch,
                        run.base_sha,
                    )
                except (
                    SourceInventoryError,
                    maintenance_file_ledger.FileReviewLedgerError,
                ) as error:
                    self._ports.events.error(str(error))
                    return PlanningOutcome(
                        PlanningOutcomeKind.REMOTE_BASE_RESTART,
                        detail=str(error),
                        error=True,
                    )
                if empty_inventory is not None:
                    try:
                        cycle = self._ports.state.settle_empty_stabilization_assessment(
                            context.repo,
                            context.refactoring_review_interval,
                            source_sha=run.base_sha,
                            repository_path=str(context.repo_path),
                            base_branch=context.base_branch,
                            inventory=empty_inventory,
                        )
                    except (
                        maintenance_state.MaintenanceStateError,
                        maintenance_file_ledger.FileReviewLedgerError,
                        SourceInventoryError,
                    ) as error:
                        self._ports.events.error(str(error))
                        return PlanningOutcome(
                            PlanningOutcomeKind.REMOTE_BASE_RESTART,
                            detail=str(error),
                            error=True,
                        )
                    due = maintenance_state.ReviewDue(
                        False, "no_selected_files", due.merged_since_baseline,
                        cycle.phase,
                    )
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
                    completion_candidate=completion_candidate,
                    **({"due_override": due} if tiered_selection is not None else {}),
                )
        if assessment_can_yield(cycle) and not hasattr(self._ports, "tiered"):
            decision = self._ports.state.schedule(context.repo)
            if (
                decision.runnable_ready is not None
                and getattr(decision, "active", None) is None
                and not getattr(decision, "project_blocked", False)
            ):
                latest_sha = self._fetch_remote_head(context)
                if latest_sha != run.base_sha:
                    return self._restart_for_new_input()
                selected = self._ports.state.activate_ready(context.repo, latest_sha)
                if selected is None:
                    return PlanningOutcome(PlanningOutcomeKind.WAITING)
                self._ports.events.info("기존 READY 실행을 재개하고 미게시 유지보수 평가를 보류합니다.")
                return PlanningOutcome(PlanningOutcomeKind.EXECUTION_READY)
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
                if hasattr(self._ports, "tiered") else "PLAN_BLOCKED 또는 PR_OPEN 종료 전 유지보수 평가를 보류합니다."
            )
            return PlanningOutcome(PlanningOutcomeKind.WAITING)
        if cycle.phase in {
            maintenance_state.MaintenancePhase.ASSESSMENT,
            maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        }:
            run.maintenance_planning = True
        elif cycle.phase in {
            maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
            maintenance_state.MaintenancePhase.REFACTORING,
        }:
            decision = self._ports.state.schedule(context.repo)
            if decision.runnable_ready is None:
                self._ports.events.info(
                    "활성 유지보수 주기의 dependency 충족을 기다립니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
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
            specification_changed = self._specification_planning_required(
                row,
                context,
                run.base_sha,
                run.specification_fingerprint,
                run.general_fingerprint,
            )
            planning_input_changed = self._general_planning_required(
                row,
                context,
                run.base_sha,
                run.general_fingerprint,
            )
            run.selection = choose_planning_decision(
                has_active=getattr(decision, "active", None) is not None,
                specification_changed=specification_changed or approval_pending,
                runnable_ready=decision.runnable_ready is not None,
                planning_input_changed=planning_input_changed,
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

    def _general_planning_required(
        self,
        row: runtime_state.PlanningSnapshot,
        context: PlanningContext,
        base_sha: str,
        current_fingerprint: str,
    ) -> bool:
        if not self._plan_declarations_match(context, base_sha):
            return True
        if row.planning_fingerprint == current_fingerprint:
            return False
        if row.planning_fingerprint and row.processed_sha:
            processed = self._ports.git.nonplan_fingerprint(
                context.repo_path, row.processed_sha
            )
            current = self._ports.git.nonplan_fingerprint(context.repo_path, base_sha)
            if processed == current:
                return False
            if self._ports.git.requirement_archive_only(
                context.repo_path, row.processed_sha, base_sha
            ):
                return False
        return True

    def _specification_planning_required(
        self,
        row: runtime_state.PlanningSnapshot,
        context: PlanningContext,
        base_sha: str,
        current_fingerprint: str,
        current_general_fingerprint: str,
    ) -> bool:
        baseline = row.specification_fingerprint
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
                context.repo_path, row.processed_sha, base_sha
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
                f"보존된 planning worktree: {row.worktree}"
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
        forced_general_planning = bool(
            run.selection
            and run.selection.kind
            in {
                PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
                PlanningDecisionKind.RUN_FALLBACK_PLANNING,
            }
        )
        if (
            not run.maintenance_planning
            and not row.phase
            and row.planning_fingerprint == run.current_fingerprint
            and self._plan_declarations_match(context, run.base_sha)
            and not forced_general_planning
        ):
            self._ports.state.update(
                context.repo,
                processed_sha=run.base_sha,
                specification_fingerprint=run.specification_fingerprint,
            )
            self._ports.events.info(
                "계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다."
            )
            planning_required = False
            run.fingerprint_skipped = True

        if (
            planning_required
            and not run.maintenance_planning
            and row.planning_fingerprint
            and row.processed_sha
            and not forced_general_planning
        ):
            declarations_match = self._plan_declarations_match(context, run.base_sha)
            processed = self._ports.git.nonplan_fingerprint(
                context.repo_path, row.processed_sha,
            )
            current = self._ports.git.nonplan_fingerprint(
                context.repo_path, run.base_sha,
            )
            if declarations_match and processed == current:
                self._ports.state.update(
                    context.repo,
                    processed_sha=run.base_sha,
                    specification_fingerprint=run.specification_fingerprint,
                )
                self._ports.events.info(
                    "계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다."
                )
                planning_required = False
                run.fingerprint_skipped = True
            elif declarations_match and self._ports.git.requirement_archive_only(
                context.repo_path, row.processed_sha, run.base_sha
            ):
                self._ports.state.update(
                    context.repo,
                    processed_sha=run.base_sha,
                )
                self._ports.events.info(
                    "Requirement archive 이동 외의 계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다."
                )
                planning_required = False
                run.fingerprint_skipped = True

        if planning_required:
            assert run.current_fingerprint is not None
            project_id = hashlib.sha256(context.repo.encode()).hexdigest()[:16]
            worktree = (
                context.state_dir
                / "worktrees"
                / project_id
                / "planning"
                / run.current_fingerprint
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
