"""Planning workspace creation and ownership checks."""
from __future__ import annotations
from pathlib import Path
from uuid import uuid4
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
from loop_manager_internal.planning.planning_rebase import PlanningRebaseError
from loop_manager_internal.maintenance.maintenance_orchestration import (
    maintenance_planning_fingerprint, MaintenanceOrchestrationError,
    MaintenanceOutcome, parse_maintenance_planner_decision,
)
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.planning.planning_service import PlanningService
from loop_manager_internal.planning.conflict_correction import PlanningConflictCorrection
from loop_manager_internal.planning.planning_contracts import (
    _PlanningRun, PlanningOutcome, PlanningOutcomeKind,
    PlanningWorkspaceOwnershipError, MAX_PLANNER_VALIDATION_RETRIES,
    WORKSPACE_OWNED, WORKSPACE_RESERVED,
)

class PlanningWorkspaceService(PlanningService):
    @staticmethod
    def _correction_context(run: _PlanningRun) -> str:
        if not run.maintenance_planning:
            return "general"
        cycle = run.maintenance_cycle
        return f"{cycle.phase.value}:{cycle.revision}"

    @staticmethod
    def _linked_failed_result(run: _PlanningRun) -> bool:
        row = run.row
        if not row.planning_error:
            return False
        if (row.planning_error_origin == "validation"
                and row.planning_retry_count >= MAX_PLANNER_VALIDATION_RETRIES):
            return True
        if (row.planning_error_origin is None
                and row.planning_retry_count >= MAX_PLANNER_VALIDATION_RETRIES
                and row.planning_error == row.planning_validation_error):
            # Legacy rows have no cause marker; exhausted retries and the
            # original diagnostic together establish this older connection.
            return True
        if not row.planning_result_completed:
            return False
        if not run.maintenance_planning or row.planning_error_origin not in (None, "unsafe"):
            return False
        try:
            decision = parse_maintenance_planner_decision(
                str(row.planning_result or ""), run.maintenance_cycle.phase,
            )
        except MaintenanceOrchestrationError:
            return False
        return (decision.outcome is MaintenanceOutcome.UNSAFE
                and (row.planning_error_origin == "unsafe"
                     or row.last_error == row.planning_error))

    @staticmethod
    def _assessment_targets(run: _PlanningRun, rows) -> tuple[str, ...]:
        ids = set(run.maintenance_cycle.revalidation_plan_ids if run.maintenance_planning else ())
        if (run.maintenance_planning and
                run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION):
            return tuple(sorted(ids))
        for plan in rows:
            keys = plan.keys()
            if plan["status"] != "READY":
                continue
            if any(plan[key] for key in (
                "branch", "worktree", "workspace_owned", "ever_started",
                "worker_session_id", "reviewer_session_id", "agent_snapshot", "pr_url",
            ) if key in keys):
                continue
            ids.add(str(plan["plan_id"]))
        return tuple(sorted(ids))

    def _resolve_rebase_conflict(
        self, run: _PlanningRun, worktree: Path, target: str, detail: str,
        collision_paths: tuple[str, ...] = (), stash_sha: str = "",
    ) -> bool:
        return PlanningConflictCorrection(
            self._ports.git, self._ports.agent, self._ports.events,
        ).resolve(
            run.context.repo, worktree, target, detail,
            "maintenance_planner" if run.maintenance_planning else "planner",
            run.row.planner_session_id,
            lambda session_id: self._ports.state.update(
                run.context.repo, planner_session_id=session_id,
            ),
            collision_paths, stash_sha,
        )

    def _preserve_rebase_conflict(self, repo: str, detail: str) -> None:
        current = self._ports.state.planning(repo)
        if not (current.planning_error or current.planning_validation_error):
            self._ports.state.update(repo, planning_validation_error='planning rebase 충돌: ' + detail)
        self._ports.state.preserve_diagnostics(repo, self._ports.state.planning(repo))

    def _prepare_workspace(
        self, run: _PlanningRun, *, defer_on_remote_advance: bool = False,
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

        # This is a safe boundary: no Planner or verifier is using the
        # worktree. Recheck origin even when the project step began earlier.
        conflict_detail = None
        correction_completed = False
        try:
            latest = self._ports.git.fetch_base(context.repo_path, context.base_branch)
            if defer_on_remote_advance and latest != row.active_sha:
                return PlanningOutcome(
                    PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                    detail="Planner 종료 후 원격 base가 전진해 완료 결과와 작업물을 보존하고 다음 실행에서 갱신합니다.",
                    error=True,
                )
            if latest == row.active_sha:
                # The SQLite base update may have committed immediately before
                # a crash that left the preservation stash and journal behind.
                self._ports.git.complete_planning_rebase(
                    context.repo, worktree, latest,
                )
            if latest != row.active_sha:
                includes_general = (
                    not run.maintenance_planning
                    or run.maintenance_cycle.include_general_planning
                )
                mode_failure = str(row.planning_error_fingerprint or "").startswith("maintenance-cycle:")
                general_input_changed = False
                if includes_general:
                    previous_input = self._ports.git.input_fingerprint(
                        context.repo_path, str(row.active_sha or ""),
                    )
                    latest_input = self._ports.git.input_fingerprint(context.repo_path, latest)
                    general_input_changed = (
                        previous_input != latest_input
                        and not self._ports.git.requirement_archive_only(
                            context.repo_path, str(row.active_sha or ""), latest,
                        )
                    )
                catalog_rows = self._ports.state.execplan_rows(context.repo)
                changed_documents = self._ports.git.relevant_document_changes(
                    worktree, str(row.active_sha or ""), latest,
                    sorted(set(self._ports.git.changed_files(
                        worktree, str(row.active_sha or ""),
                    )) | set(self._ports.git.changed_files(worktree))),
                    catalog_rows, self._assessment_targets(run, catalog_rows),
                    include_active_requirements=includes_general,
                )
                relevant_input_changed = bool(changed_documents or general_input_changed)
                recheck_completed = row.planning_result_completed and relevant_input_changed
                linked_error = self._linked_failed_result(run)
                if row.agent_snapshot:
                    check = getattr(self._ports.state, "planner_check", lambda _repo: None)(context.repo)
                    role = ("maintenance_planner" if run.maintenance_planning else
                            "planner_check" if check is not None and check["stage"] in {"pending", "result"}
                            else "planner")
                    if row.agent_role != role:
                        self._ports.events.error("보존된 Agent 역할이 현재 Planner와 다릅니다.")
                        return self._operational_error()
                    scope = self._ports.agent.validate_saved_scope(
                        role, worktree, row.agent_snapshot,
                    )
                    if scope.error:
                        self._ports.events.error(scope.error)
                        return self._operational_error()
                    self._ports.agent.clear_scope(context.repo)
                result = self._ports.git.rebase_planning_workspace(
                    context.repo, str(row.planning_attempt_fingerprint or ""),
                    worktree, str(row.active_sha or ""), latest,
                )
                if result.conflict:
                    conflict_detail = result.detail
                    if self._resolve_rebase_conflict(
                        run, worktree, result.target, result.detail,
                        getattr(result, "conflict_paths", ()),
                        getattr(result, "stash_sha", ""),
                    ):
                        correction_completed = True
                        result = self._ports.git.rebase_planning_workspace(
                            context.repo,
                            str(row.planning_attempt_fingerprint or ""),
                            worktree, str(row.active_sha or ""), latest,
                        )
                if result.conflict:
                    self._preserve_rebase_conflict(context.repo, result.detail)
                    self._ports.events.error(
                        f"planning rebase 충돌; 작업 보존: {worktree}; {result.detail}"
                    )
                    return PlanningOutcome(
                        PlanningOutcomeKind.TRANSIENT_PAUSE,
                        detail=result.detail, error=True,
                    )
                conflict_detail = None
                if run.maintenance_planning:
                    cycle = self._ports.state.maintenance_cycle(context.repo)
                    if cycle.publication_stage == "prepared":
                        cycle = self._ports.state.supersede_unpublished_maintenance_intent(
                            context.repo, cycle, result.target,
                        )
                    elif cycle.cycle_base_sha != result.target:
                        cycle = self._ports.state.restart_stale_maintenance(
                            context.repo, cycle, result.target,
                        )
                    run.maintenance_cycle = cycle
                else:
                    self._ports.state.supersede_unpublished_general_intent(context.repo)
                values: dict[str, object] = {"active_sha": result.target}
                if row.planning_error:
                    # A new cycle/base is not evidence that the failed input
                    # changed. Rebind the gate to the adopted input and retain
                    # the original failure fingerprint in its diagnostics.
                    self._ports.state.preserve_diagnostics(context.repo, row)
                    adopted_input = self._ports.git.input_fingerprint(context.repo_path, result.target)
                    values["planning_error_fingerprint"] = (
                        f"maintenance-cycle:{run.maintenance_cycle.phase.value}:{run.maintenance_cycle.revision}"
                        if mode_failure else maintenance_planning_fingerprint(run.maintenance_cycle, adopted_input)
                        if run.maintenance_planning else adopted_input
                    )
                    if (relevant_input_changed
                            and not row.planning_result_completed
                            and not mode_failure):
                        # Keep the authorized correction durable across the
                        # crash window before the next Planner invocation.
                        values["planning_result_pending_validation"] = 1
                    if (relevant_input_changed and not mode_failure
                            and linked_error):
                        values["planning_retry_count"] = 0
                if recheck_completed:
                    if not row.planning_error:
                        self._ports.state.preserve_diagnostics(context.repo, row)
                    recheck_message = (
                        "현재 planning 결과의 관련 문서가 변경됐습니다: "
                        + ", ".join(changed_documents)
                        if changed_documents else
                        "현재 planning 결과의 전체 계획 입력이 변경됐습니다. 최신 코드와 테스트를 다시 평가하세요."
                    )
                    values.update(
                        planning_result_pending_validation=1,
                        planning_result_completed=0,
                        planning_result=None,
                    )
                    can_restart_result = (
                        not mode_failure
                        and (not row.planning_error or linked_error)
                        and (not row.last_error or linked_error)
                        and not row.agent_result_snapshot
                        and not str(row.planning_validation_error or "").startswith(
                            "planning rebase 충돌:"
                        )
                    )
                    if can_restart_result:
                        prior_error = row.planning_error if linked_error else None
                        values.update(
                            planning_validation_error=(
                                recheck_message + (
                                    "\n이전 planning 실패를 최신 입력에서 다시 확인: "
                                    + prior_error if prior_error else ""
                                )
                            ),
                            planning_retry_count=0,
                        )
                    else:
                        values["planning_validation_error"] = recheck_message
                elif changed_documents and not row.planning_result_completed:
                    additional = [path for path in changed_documents
                                  if path not in str(row.planning_validation_error or "")]
                    if additional:
                        values["planning_validation_error"] = (
                            str(row.planning_validation_error or "").rstrip()
                            + "\n관련 원격 입력 변경: " + ", ".join(additional)
                        ).lstrip()
                correction_pending = bool(
                    recheck_completed
                    or (not row.planning_result_completed
                        and relevant_input_changed and not mode_failure)
                    or (row.planning_correction_error == row.planning_error
                        and row.planning_result_pending_validation)
                )
                if (row.planning_error and correction_pending
                        and (linked_error or row.planning_correction_error == row.planning_error)):
                    incident_id = row.planning_error_incident_id or uuid4().hex
                    values.update(
                        planning_error_incident_id=incident_id,
                        planning_correction_error=row.planning_error,
                        planning_correction_incident_id=incident_id,
                        planning_correction_sha=result.target,
                        planning_correction_context=self._correction_context(run),
                    )
                with self._ports.state.transaction():
                    self._ports.state.update(context.repo, **values)
                self._ports.git.complete_planning_rebase(
                    context.repo, worktree, result.target,
                )
                observed_latest = (
                    self._ports.git.fetch_base(context.repo_path, context.base_branch)
                    if correction_completed else latest
                )
                self._ports.events.info(
                    f"planning base 반영: {row.active_sha} -> {result.target}; "
                    f"후보={self._ports.git.run(worktree, 'rev-parse', 'HEAD').stdout.strip()}; "
                    f"재개={'validation' if row.planning_result_completed and not recheck_completed else 'planner'}; "
                    f"남은 원인={row.planning_error or row.planning_validation_error or '없음'}"
                )
                row = self._ports.state.planning(context.repo)
                if result.target != observed_latest:
                    return PlanningOutcome(
                        PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                        detail="원격 base가 다시 전진해 다음 안전 경계에서 반영합니다.",
                        error=True,
                    )
                if (row.planning_error and (not relevant_input_changed or mode_failure)
                        and not row.planning_result_completed
                        and not row.planning_result_pending_validation
                        and not context.local_environment_recheck):
                    self._ports.events.error(
                        f"원격 base만 반영하고 동일 입력의 planning 오류를 보존합니다: {row.planning_error}"
                    )
                    run.row = row
                    return self._operational_error()
            # Reconcile even when SQLite already adopted this base before a
            # crash. Snapshot authority must describe the remote catalog, not
            # the Planner's dirty checkout or a superseded publication intent.
            pending_intent = (
                self._ports.state.general_publication(context.repo) is not None
                or run.maintenance_planning
                and run.maintenance_cycle.publication_stage == "prepared"
            )
            if not pending_intent:
                if context.refresh_catalog is not None:
                    context.refresh_catalog(latest)
                else:
                    self._ports.state.reconcile_catalog(
                        context.repo, context.repo_path, context.base_branch, latest,
                    )
            run.base_sha = latest
            run.general_fingerprint = self._ports.git.input_fingerprint(
                context.repo_path, latest,
            )
            run.specification_fingerprint = self._ports.git.specification_fingerprint(
                context.repo_path, latest,
            )
            run.current_fingerprint = (
                maintenance_planning_fingerprint(
                    run.maintenance_cycle, run.general_fingerprint,
                ) if run.maintenance_planning else run.general_fingerprint
            )
        except (RuntimeError, PlanningRebaseError) as error:
            if conflict_detail is not None:
                self._preserve_rebase_conflict(context.repo, conflict_detail)
            self._ports.events.error(f"planning 원격 반영 대기: {error}")
            return PlanningOutcome(
                PlanningOutcomeKind.TRANSIENT_PAUSE, detail=str(error), error=True,
            )
        run.row = row

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
            except (RuntimeError, OSError) as error:
                self._ports.events.error(f"maintenance 관측 재시도: {error}")
                return PlanningOutcome(
                    PlanningOutcomeKind.TRANSIENT_PAUSE, detail=str(error), error=True,
                )
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
            except (RuntimeError, OSError) as error:
                self._ports.events.error(f"Planner 기준 관측 재시도: {error}")
                return PlanningOutcome(
                    PlanningOutcomeKind.TRANSIENT_PAUSE, detail=str(error), error=True,
                )
        if (context.local_environment_recheck and row.planning_error
                and (row.planning_error_origin != "unsafe" or self._linked_failed_result(run))):
            # Link this checked workspace to the existing incident. Only fresh
            # result validation may release it; retry counts remain untouched.
            self._ports.state.preserve_diagnostics(context.repo, row)
            incident_id = row.planning_error_incident_id or uuid4().hex
            self._ports.state.update(
                context.repo, planning_error_incident_id=incident_id,
                planning_correction_error=row.planning_error,
                planning_correction_incident_id=incident_id,
                planning_correction_sha=str(row.active_sha),
                planning_correction_context=self._correction_context(run),
            )
            run.row = self._ports.state.planning(context.repo)
        run.planning_worktree = worktree
        return None

    def _refresh_after_planner(self, run: _PlanningRun) -> PlanningOutcome | None:
        """Refresh the candidate after its writer exits and before result validation."""
        if run.row.phase != "PLANNING" or not run.row.planning_result_completed:
            return None
        outcome = self._prepare_workspace(run, defer_on_remote_advance=True)
        if outcome is not None:
            return outcome
        if run.row.planning_result_pending_validation and not run.row.planning_result_completed:
            return PlanningOutcome(PlanningOutcomeKind.ASSESSMENT_PROGRESS)
        return None
