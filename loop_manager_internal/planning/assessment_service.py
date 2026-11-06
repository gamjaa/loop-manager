"""Planner invocation, source-bound file assessment, and result validation."""
from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
from loop_manager_internal.planning.agent_ports import run_scoped_planner_turn, planner_progress
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
from loop_manager_internal.maintenance.maintenance_orchestration import (
    MaintenanceOrchestrationError, MaintenanceOutcome, MaintenancePlannerDecision,
    parse_maintenance_planner_decision, validate_prerequisite_plan_ids,
)
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningRestart
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventoryError
from loop_manager_internal.maintenance import (
    maintenance_file_review, maintenance_reconciliation, maintenance_findings,
)
from loop_manager_internal.storage import maintenance_state, maintenance_file_ledger
from loop_manager_internal.planning.assessment_port import AssessmentError, AssessmentPort
from loop_manager_internal.planning.planning_service import PlanningService
from loop_manager_internal.planning.assessment_batches import MaintenanceAssessmentBatches
from loop_manager_internal.planning.planning_contracts import (
    _PlanningRun, PlanningOutcome, PlanningOutcomeKind, PlanningAgentResultKind,
    MAX_PLANNER_VALIDATION_RETRIES,
)

class PlanningAssessmentService(PlanningService):
    def __init__(self, ports, assessment: AssessmentPort):
        super().__init__(ports)
        self._assessment = assessment

    def _run_planner_turn(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = run.planning_worktree
        assert worktree is not None
        head = self._ports.git.run(worktree, "rev-parse", "HEAD").stdout.strip()
        dirty_files = self._ports.git.changed_files(worktree)
        if row.planning_validation_error and head != row.active_sha:
            self._ports.git.run(
                worktree, "reset", "--mixed", str(row.active_sha)
            )
            head = self._ports.git.run(
                worktree, "rev-parse", "HEAD"
            ).stdout.strip()
            dirty_files = self._ports.git.changed_files(worktree)

        result_in_flight = bool(
            row.planning_result_pending_validation
            and not row.planning_result_completed
        )
        scope_error: str | None = None
        should_run = result_in_flight or (
            head == row.active_sha
            and (
                not dirty_files
                or row.planning_validation_error
                or row.last_error
            )
            and not row.planning_result_pending_validation
            and not row.planning_result_completed
            and not (
                row.planning_validation_error
                and row.planning_retry_count >= MAX_PLANNER_VALIDATION_RETRIES
                and not context.local_environment_recheck
            )
        )
        file_review_enabled = (
            self._file_assessment_active(run)
            and hasattr(self._ports, "file_review")
        )
        from loop_manager_internal.planning import tiered_assessment
        from loop_manager_internal.maintenance.tiered_review import TieredReviewError
        module_enabled = tiered_assessment.enabled(self._ports, run)
        if module_enabled:
            file_review_enabled = False
        if should_run:
            if run.maintenance_planning and run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION:
                self._ports.events.error("기존 사후 재검증의 미확정 결과를 보존합니다. 추가 Planner 호출은 실행하지 않습니다.")
                return self._operational_error()
            final_page = None
            module_context = None
            if module_enabled:
                try:
                    module_outcome, module_context = tiered_assessment.prepare_turn(self._ports, run, worktree)
                except (TieredReviewError, SourceInventoryError, OSError) as error:
                    self._ports.events.error(str(error))
                    return PlanningOutcome(PlanningOutcomeKind.OPERATIONAL_ERROR, detail=str(error), error=True)
                if module_outcome is not None:
                    return module_outcome
                if module_context == "no_model_needed":
                    return None
            if file_review_enabled:
                progress = self._ports.file_review.progress(
                    context.repo, context.repo_path, context.base_branch,
                    run.maintenance_cycle.revision, str(row.active_sha),
                )
                if (not progress.selected
                    and progress.next_unit is None
                    and not maintenance_file_ledger.root_move_candidates(progress.root_reviews)
                    and not run.maintenance_cycle.include_general_planning
                    and run.maintenance_cycle.publication_stage is None
                    and not row.planning_result_completed
                    and not result_in_flight):
                    return self._complete_empty_file_assessment(run, worktree)
                review_outcome = self._review_one_file(run, worktree)
                if review_outcome is not None:
                    return review_outcome
                if hasattr(self._ports, "reconciliation"):
                    relation_outcome = self._reconcile_one_relation(run, worktree)
                    if relation_outcome is not None:
                        return relation_outcome
            self._ports.events.log("Planner 시작: 다음 ExecPlan 계획을 생성합니다.")
            if not result_in_flight:
                self._ports.state.update(
                    context.repo,
                    last_error=None,
                    planning_result_pending_validation=1,
                    planning_result_completed=0,
                    planning_result=None,
                )
                row = self._ports.state.planning(context.repo)
            role = "maintenance_planner" if run.maintenance_planning else "planner"
            if run.maintenance_planning:
                assert run.maintenance_context is not None
                prompt = self._ports.validation.maintenance_prompt(
                    run.maintenance_context,
                    self._ports.state.compact_catalog(context.repo),
                    run.maintenance_change_context,
                    row.planning_validation_error or row.last_error,
                    include_general_planning=(
                        run.maintenance_cycle.include_general_planning
                    ),
                )
                if module_context:
                    prompt += module_context
                if file_review_enabled:
                    if hasattr(self._ports, "reconciliation"):
                        progress = self._ports.reconciliation.progress(
                            context.repo, context.repo_path, context.base_branch,
                            run.maintenance_cycle.revision, str(row.active_sha),
                        )
                        final_page = self._ports.reconciliation.synthesis(
                            context.repo, run.maintenance_cycle.revision, str(row.active_sha), progress,
                        )
                        if final_page.index != final_page.total:
                            return PlanningOutcome(PlanningOutcomeKind.WAITING)
                        manifest = self._ports.reconciliation.synthesis_export(
                            context.repo, final_page, progress, context.state_dir / "maintenance-evidence",
                        )
                        existing = self._active_plan_statuses(context.repo)
                        collection, result_manifest = self._ports.reconciliation.findings_unit(
                            context.repo, context.repo_path, context.base_branch,
                            run.maintenance_cycle.revision, str(row.active_sha), existing,
                            context.state_dir / "maintenance-evidence",
                        )
                        if collection.receipt is None:
                            return PlanningOutcome(PlanningOutcomeKind.WAITING)
                        prompt += (
                            "\n\n검증된 발견·계획 비교 결과 manifest: " + str(result_manifest)
                            + "\nmanifest의 unit_pattern으로 저장된 발견·비교 원문을 필요한 부분만 조회하라. "
                            "제안된 새 계획을 실제 ExecPlan으로 작성하고 사용자 동작·공개 계약·저장 형식 "
                            "보존과 targeted unit test·전체 verify 회귀 조건을 명시하라. "
                            "모든 발견·비교는 이미 저장돼 있으므로 최종 응답에 배열을 다시 출력하지 마라. "
                            "페이지별 발견 연결·제외 검증은 이후 별도 단위에서 진행한다. "
                            "최종 응답에 아래 식별자·digest·개수·요약만 그대로 포함하라:\n"
                            + maintenance_findings.RESULT_PREFIX + maintenance_findings.encode(collection.receipt)
                        )
                if (not module_enabled and context.self_repository and run.maintenance_cycle.phase
                    is maintenance_state.MaintenancePhase.ASSESSMENT
                    and not run.maintenance_cycle.full_assessment_completed):
                    try:
                        inventory = self._assessment.inventory(
                            context.repo_path, str(row.active_sha)
                        )
                        prompt += self._assessment.prompt(
                            inventory, str(row.active_sha),
                            run.maintenance_cycle.full_assessment_result,
                        )
                    except AssessmentError as error:
                        message = str(error)
                        retry_count = row.planning_retry_count + 1
                        values: dict[str, object] = {
                            "planning_validation_error": message,
                            "planning_result_pending_validation": 0,
                            "planning_retry_count": retry_count,
                        }
                        if retry_count >= MAX_PLANNER_VALIDATION_RETRIES:
                            values.update(
                                planning_error=message,
                                planning_error_fingerprint=run.current_fingerprint,
                            )
                        self._ports.state.update(context.repo, **values)
                        self._ports.events.error(message)
                        return PlanningOutcome(
                            PlanningOutcomeKind.VALIDATION_RETRY, error=True,
                        )
            else:
                assert run.mutable_ready_snapshot is not None
                prompt = self._ports.validation.planner_prompt(
                    row.planning_validation_error or row.last_error,
                    self._ports.state.compact_catalog(context.repo),
                    run.mutable_ready_snapshot,
                )
                if row.planning_resume_plan_id:
                    active = self._ports.state.schedule(context.repo).active
                    prompt += ('\n현재 작업을 초기화하지 않고 일반 planning 뒤 재개한다: '
                        + str(row.planning_resume_plan_id) + '\n보존된 구현 작업 공간: ' + str(active['worktree'])
                        + '\n필요하면 이 후보를 읽기 전용으로 확인하고 이미 수행한 구현·기록을 보존하라. '
                          '그 작업 공간·Git·실행 이력은 수정하지 마라. 새 조건을 관련 ExecPlan에 반영하되 '
                          '후보 확인만으로 verify·Review 성공을 확정하지 마라.\n')
                if hasattr(self._ports, "retirement"):
                    run.retirement_pending = self._ports.retirement.pending(
                        context.repo, context.repo_path, str(row.active_sha),
                    )
                if hasattr(self._ports.state, "planning_review"):
                    from loop_manager_internal.planning.specification_review import prepare_review
                    requirement_only = bool(run.retirement_pending)
                    if run.maintenance_cycle.phase not in {
                            maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                            maintenance_state.MaintenancePhase.REFACTORING}:
                        requirement_only = bool(requirement_only and row.planning_fingerprint
                            and row.specification_fingerprint == run.specification_fingerprint
                            and not getattr(self._ports.git, 'runtime_guidance_changed', lambda *_: False)(context.repo_path)
                            and not getattr(self._ports.state, 'pending_user_decision', lambda *_: False)(
                                context.repo, self._ports.git.plan_declarations(context.repo_path, str(row.active_sha))))
                    instruction, attempt = prepare_review(
                        self._ports.state, self._ports.git, context, str(row.active_sha),
                        final_required=run.final_review_required, requirement_only=requirement_only,
                    )
                    prompt += instruction
                    self._ports.events.log(f"일반 planning 검토: purpose={attempt['purpose']}, revision={attempt['revision']}, 비교={attempt['comparison_revision']}, cycle={attempt['work_cycle']}")
                from loop_manager_internal.planning.common_defect_context import planning_context
                reports = getattr(self._ports.state, "common_defect_reports", lambda _repo: [])(context.repo)
                if hasattr(self._ports.state, "bind_common_defect_snapshot"):
                    self._ports.state.bind_common_defect_snapshot(context.repo, str(row.active_sha),
                                                                 [int(item['id']) for item in reports])
                prompt += planning_context(reports, context.state_dir)
                if hasattr(self._ports, "retirement"):
                    from loop_manager_internal.planning.requirement_retirement import assessment_prompt
                    prompt += assessment_prompt(run.retirement_pending)
                check = getattr(self._ports.state, "planner_check", lambda _repo: None)(context.repo)
                if check is not None and check["stage"] == "handoff":
                    prompt += (
                        "\n\n읽기 전용 점검 Planner 인계 근거: "
                        + str(check["error"] or "수정 필요")
                        + "\n점검 기준 revision: " + str(check["base_sha"])
                        + "\n이 근거를 최신 입력과 대조하고 일반 Planner 권한 안에서 판단하라."
                    )
                from loop_manager_internal.maintenance.tiered_ports import query_instruction
                prompt_session = getattr(self._ports.agent, 'prompt_session', lambda session: session)(row.planner_session_id)
                if not prompt_session:
                    prompt += (self._ports.tiered.query_context(context.repo_path, context.base_branch,
                        str(row.active_sha), context.state_dir) if hasattr(self._ports, "tiered") else query_instruction())
            def save_final_session(session_id):
                if final_page is not None:
                    self._ports.reconciliation.synthesis_session(context.repo, final_page, session_id)
                self._ports.state.update(context.repo, planner_session_id=session_id)

            with planner_progress(self._ports.events, f"{role} 계획 작성 ({context.repo})"):
                result, scope_result = run_scoped_planner_turn(
                    self._ports.agent, context.repo, role, worktree, prompt,
                    final_page.session_id if final_page is not None else row.planner_session_id,
                    save_final_session,
                    purpose=("maintenance_plan" if run.maintenance_planning else
                             "final_confirmation" if run.final_review_required else "general_planning"),
                    target=str(row.active_sha),
                )
            if scope_result.error:
                scope_error = scope_result.error
                if scope_result.result_snapshot is not None:
                    self._ports.state.update(
                        context.repo,
                        agent_result_snapshot=scope_result.result_snapshot,
                    )

            save_result = self._ports.state.update
            if context.recovery_request_id:
                save_result = lambda repo, **values: self._ports.state.update_local_recheck_result(
                    repo, context.recovery_request_id, **values)
            if result.code:
                message = result.message or f"Planner 종료 코드 {result.code}"
                if result.kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
                    outcome = PlanningOutcomeKind.TRANSIENT_PAUSE
                elif result.kind is PlanningAgentResultKind.USAGE_LIMIT:
                    outcome = PlanningOutcomeKind.USAGE_LIMIT_PAUSE
                else:
                    outcome = PlanningOutcomeKind.OPERATIONAL_ERROR
                save_result(
                    context.repo,
                    last_error=message,
                    planning_result_pending_validation=0,
                    planning_result_completed=0,
                    planning_result=None,
                )
                self._ports.events.error(message)
                return PlanningOutcome(outcome, error=True)
            save_result(
                context.repo,
                last_error=None,
                planning_result_completed=1,
                planning_result=result.message,
            )
            row = self._ports.state.planning(context.repo)
            dirty_files = self._ports.git.changed_files(worktree)

        if scope_error is None:
            scope_row = self._ports.state.planning(context.repo)
            if scope_row.agent_snapshot:
                expected_role = (
                    "maintenance_planner" if run.maintenance_planning else "planner"
                )
                if scope_row.agent_role != expected_role:
                    scope_error = "저장된 Agent 역할이 Planner와 다릅니다."
                else:
                    scope_result = self._ports.agent.validate_saved_scope(
                        expected_role,
                        worktree,
                        scope_row.agent_snapshot,
                    )
                    scope_error = scope_result.error
                    if scope_result.error and scope_result.result_snapshot is not None:
                        self._ports.state.update(
                            context.repo,
                            agent_result_snapshot=scope_result.result_snapshot,
                        )
                    elif not scope_result.error:
                        self._ports.agent.clear_scope(context.repo)
        run.row = row
        run.dirty_files = dirty_files
        run.planner_scope_error = scope_error
        return None

    def _complete_empty_file_assessment(
        self, run: _PlanningRun, worktree: Path,
    ) -> PlanningOutcome:
        """A verified empty selection needs no assessment Agent or plan commit."""
        context = run.context
        row = run.row
        assert run.maintenance_context is not None
        self._ports.git.validate_worktree_ownership(
            context.repo, str(row.planning_attempt_fingerprint or run.current_fingerprint or ""), worktree,
        )
        self._ports.git.fetch_base(context.repo_path, context.base_branch)
        try:
            self._ports.validation.validate_publication_base(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_context,
            )
        except MaintenancePlanningRestart:
            return PlanningOutcome(
                PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED, error=True,
            )
        evidence = self._ports.state.file_assessment_evidence(
            context.repo, context.repo_path, context.base_branch,
            run.maintenance_cycle.revision, str(row.active_sha),
        )
        if evidence.reviewed:
            raise PlanningResultInvalid("빈 선정 집합의 검토 결과가 예상과 다릅니다.")
        prepared = self._ports.state.prepare_file_assessment(
            context.repo, context.repo_path, context.base_branch, str(row.active_sha), evidence,
        )
        with self._ports.state.transaction():
            self._ports.state.complete_file_assessment(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(row.active_sha), prepared,
            )
            self._ports.state.advance_success_baseline(
                context.repo,
                expected_phase=run.maintenance_cycle.phase,
                expected_revision=run.maintenance_cycle.revision,
                remote_commit=str(row.active_sha),
            )
            self._ports.state.clear_active_in_transaction(context.repo)
        self._ports.git.run(context.repo_path, "worktree", "remove", str(worktree))
        return PlanningOutcome(PlanningOutcomeKind.NO_CHANGE)

    def _review_one_file(self, run: _PlanningRun, worktree: Path) -> PlanningOutcome | None:
        """Review a bounded batch while preserving each source-bound receipt."""
        return MaintenanceAssessmentBatches(self._ports).review_files(run, worktree)

    def _reconcile_one_relation(self, run: _PlanningRun, worktree: Path) -> PlanningOutcome | None:
        """Batch related candidates, then advance durable synthesis evidence."""
        context = run.context
        port = self._ports.reconciliation
        try:
            progress = port.progress(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(run.row.active_sha),
            )
            if progress.pending_scan:
                return PlanningOutcome(PlanningOutcomeKind.ASSESSMENT_PROGRESS)
            relation = progress.next_relation
            if relation is None:
                return self._synthesize_one_page(run, worktree, progress)
            return MaintenanceAssessmentBatches(self._ports).review_relations(run, worktree, progress)
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART, detail=str(error))
    def _synthesize_one_page(self, run: _PlanningRun, worktree: Path,
                             progress) -> PlanningOutcome | None:
        """Batch synthesis receipts, then collect the final findings."""
        context, port = run.context, self._ports.reconciliation
        page = port.synthesis(context.repo, run.maintenance_cycle.revision,
                              str(run.row.active_sha), progress)
        if page.index == page.total:
            return self._collect_findings_unit(run, worktree)
        return MaintenanceAssessmentBatches(self._ports).synthesize(run, worktree, progress, page)

    def _active_plan_statuses(self, repo: str) -> dict[str, str]:
        return {str(item["plan_id"]): str(item["status"])
                for item in self._ports.state.execplan_rows(repo) if str(item["status"]) != "MERGED"}

    def _collect_findings_unit(self, run: _PlanningRun, worktree: Path) -> PlanningOutcome | None:
        context, port = run.context, self._ports.reconciliation
        plans = self._active_plan_statuses(context.repo)
        unit, manifest = port.findings_unit(context.repo, context.repo_path, context.base_branch,
            run.maintenance_cycle.revision, str(run.row.active_sha), plans, context.state_dir / "maintenance-evidence")
        if unit.receipt is not None:
            return None
        result, scope = run_scoped_planner_turn(
            self._ports.agent, context.repo, "maintenance_planner", worktree,
            maintenance_findings.unit_prompt(unit, manifest),
            unit.session_id, lambda session_id: port.findings_session(context.repo, unit, session_id),
            purpose="maintenance_discovery",
            target=f"findings:{unit.manifest_id}:{unit.index}")
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        if result.code:
            self._ports.events.error(result.message)
            kind = {PlanningAgentResultKind.TRANSIENT_FAILURE: PlanningOutcomeKind.TRANSIENT_PAUSE,
                    PlanningAgentResultKind.USAGE_LIMIT: PlanningOutcomeKind.USAGE_LIMIT_PAUSE}.get(
                        result.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            return PlanningOutcome(kind, error=True)
        try:
            port.findings_complete(context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(run.row.active_sha), self._active_plan_statuses(context.repo),
                unit, result.message)
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            try:
                port.findings_fail(context.repo, unit, str(error))
            except maintenance_reconciliation.ReconciliationError:
                pass  # A new snapshot already invalidated the old unit.
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.ASSESSMENT_PROGRESS)

    def _resolve_one_page(self, run: _PlanningRun, worktree: Path,
                          unit: maintenance_reconciliation.ResolutionUnit) -> PlanningOutcome:
        """Keep the final Planner result pending while batch coverage is validated."""
        return MaintenanceAssessmentBatches(self._ports).resolve(run, worktree, unit)

    def _validate_planner_result(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = run.planning_worktree
        assert worktree is not None
        dirty_files = run.dirty_files or []
        decision: MaintenancePlannerDecision | None = None
        existing_plan_ids = ()
        try:
            if run.planner_scope_error is not None:
                raise PlanningResultInvalid(run.planner_scope_error)
            if run.maintenance_planning:
                assert run.maintenance_context is not None
                try:
                    decision = parse_maintenance_planner_decision(
                        str(row.planning_result or ""),
                        run.maintenance_cycle.phase,
                    )
                except MaintenanceOrchestrationError as error:
                    raise PlanningResultInvalid(str(error)) from error
                from loop_manager_internal.planning import tiered_assessment
                from loop_manager_internal.maintenance.tiered_review import TieredReviewError
                module_enabled = tiered_assessment.enabled(self._ports, run)
                if module_enabled:
                    try:
                        existing_plan_ids = self._ports.tiered.validate_final(run, decision)
                    except TieredReviewError as error:
                        raise PlanningResultInvalid(str(error)) from error
                if (not module_enabled and run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
                    and run.maintenance_cycle.file_assessment_start_merged_count is not None
                    and (not context.self_repository or run.maintenance_cycle.full_assessment_completed)
                    and hasattr(self._ports, "reconciliation")):
                    try:
                        resolution = self._ports.reconciliation.validate(
                            context.repo, context.repo_path, context.base_branch,
                            run.maintenance_cycle.revision, str(row.active_sha),
                            str(row.planning_result or ""),
                            {str(item["plan_id"]): str(item["status"])
                             for item in self._ports.state.execplan_rows(context.repo)
                             if str(item["status"]) != "MERGED"},
                            decision.plan_ids if decision.outcome is MaintenanceOutcome.REFACTORING else (),
                            resolution_directory=context.state_dir / "maintenance-evidence",
                        )
                        if isinstance(resolution, maintenance_reconciliation.ResolutionUnit):
                            return self._resolve_one_page(run, worktree, resolution)
                        if isinstance(resolution, dict):
                            existing_plan_ids = tuple(sorted({item["plan_id"] for item in resolution["findings"]
                                if item["status"] == "existing_plan"}))
                    except (maintenance_reconciliation.ReconciliationError,
                            maintenance_file_review.FileReviewError, SourceInventoryError) as error:
                        raise PlanningResultInvalid(str(error)) from error
                changed = sorted(
                    set(dirty_files)
                    | set(
                        self._ports.git.changed_files(
                            worktree, str(row.active_sha or "")
                        )
                    )
                )
                if decision.outcome is MaintenanceOutcome.PREREQUISITES:
                    if run.maintenance_cycle.include_general_planning:
                        raise PlanningResultInvalid(
                            "combined planning에는 prerequisite 결과를 사용할 수 없습니다."
                        )
                    if changed:
                        raise PlanningResultInvalid(
                            "prerequisites 결과는 planning 파일을 변경할 수 없습니다."
                        )
                    try:
                        validate_prerequisite_plan_ids(
                            self._ports.state.execplan_rows(context.repo),
                            decision.plan_ids,
                        )
                    except MaintenanceOrchestrationError as error:
                        raise PlanningResultInvalid(str(error)) from error
                    validated_result = None
                elif decision.outcome is MaintenanceOutcome.UNSAFE:
                    if changed:
                        raise PlanningResultInvalid(
                            "unsafe revalidation 결과는 계획 파일을 변경할 수 없습니다."
                        )
                    validated_result = None
                else:
                    refactoring_ids = (
                        decision.plan_ids
                        if decision.outcome is MaintenanceOutcome.REFACTORING
                        else ()
                    )
                    validated_result = (
                        self._ports.validation.validate_maintenance_result(
                            run.maintenance_context,
                            worktree,
                            dirty_files,
                            refactoring_plan_ids=refactoring_ids,
                            allow_general_plans=(
                                run.maintenance_cycle.include_general_planning
                            ),
                        )
                    )
                    if (
                        decision.outcome is MaintenanceOutcome.REFACTORING
                        and validated_result is None
                    ):
                        raise PlanningResultInvalid(
                            "refactoring 결과에는 검증 가능한 planning batch가 필요합니다."
                        )
            else:
                assert run.mutable_ready_snapshot is not None
                manager_paths = ()
                if hasattr(self._ports, "retirement"):
                    from loop_manager_internal.planning.requirement_retirement import (
                        RetirementError, RetirementReassessment,
                    )
                    try:
                        manager_paths = self._ports.retirement.receipted_paths(
                            context.repo, context.repo_path, str(row.active_sha), worktree,
                        )
                    except RetirementReassessment as error:
                        raise PlanningResultInvalid(str(error)) from error
                    except RetirementError as error:
                        self._ports.state.update(
                            context.repo, planning_error=str(error),
                            planning_error_fingerprint=run.current_fingerprint,
                            last_error=str(error),
                        )
                        self._ports.events.error(str(error))
                        return self._operational_error()
                validated_result = self._ports.validation.validate_general_result(
                    context.repo,
                    worktree,
                    str(row.active_sha or ""),
                    dirty_files,
                    run.mutable_ready_snapshot,
                    **({"manager_paths": manager_paths} if manager_paths else {}),
                )
                if hasattr(self._ports.state, "planning_review"):
                    from loop_manager_internal.planning.specification_review import validate_final
                    attempt = self._ports.state.planning_review(context.repo, "attempt")
                    if attempt is not None:
                        if attempt["revision"] != row.active_sha or attempt["work_cycle"] != self._ports.state.review_work_cycle(context.repo):
                            raise PlanningResultInvalid("일반 planning 검토 입력이 변경됐습니다. 최신 입력을 다시 확인하세요.")
                        if (attempt['purpose'] == 'requirement' and validated_result is not None
                                and (validated_result.revisions or validated_result.deletions)):
                            raise PlanningResultInvalid('Requirement 완료 점검은 진행 중인 다른 작업을 개정할 수 없습니다.')
                        if attempt['purpose'] != 'requirement':
                            from loop_manager_internal.planning.planning_document_relevance import validate_blocked_plan_inputs
                            try:
                                validate_blocked_plan_inputs(
                                    str(row.planning_result or ''), str(row.active_sha),
                                    self._ports.state.execplan_rows(context.repo), self._ports.git.run,
                                    context.repo_path)
                            except ValueError as error:
                                raise PlanningResultInvalid(str(error)) from error
                        outcome = (validate_final(attempt, str(row.planning_result or ""), validated_result,
                                   self._ports.git, context.repo_path,
                                   manager_source_validator=lambda path, plan:
                                       self._ports.validation.validate_manager_source(
                                           context.repo, worktree, path, plan))
                                   if attempt["purpose"] == "final" else "reviewed")
                if hasattr(self._ports, "retirement"):
                    from loop_manager_internal.application.contracts import ValidatedPlanningResult
                    try:
                        judgments = self._ports.retirement.validate_result(
                            context.repo, context.repo_path, str(row.active_sha),
                            str(row.planning_result or ""),
                        )
                        planned_sources = {
                            source
                            for _path, plan in (validated_result.plans if validated_result else ())
                            for source in plan.get("sources", ())
                        } | {
                            source
                            for revision in (validated_result.revisions if validated_result else ())
                            for source in revision.plan.get("sources", ())
                        }
                        for judgment in judgments:
                            if judgment.outcome == "fulfilled" and judgment.source_path in planned_sources:
                                raise RetirementError(
                                    f"충족 판단과 새 ExecPlan source가 충돌합니다: {judgment.source_path}"
                                )
                            if judgment.outcome == "unfulfilled" and judgment.source_path not in planned_sources:
                                raise RetirementError(
                                    f"미충족 Requirement의 후속 ExecPlan이 없습니다: {judgment.source_path}"
                                )
                        self._ports.retirement.record(context.repo, judgments)
                    except RetirementError as error:
                        raise PlanningResultInvalid(str(error)) from error
                    try:
                        self._ports.retirement.stage_moves(
                            context.repo, context.repo_path, str(row.active_sha), worktree,
                            excluded_sources=tuple(sorted(planned_sources)),
                        )
                        # Include every proven pending retirement after restart.
                        manager_paths = self._ports.retirement.receipted_paths(
                            context.repo, context.repo_path, str(row.active_sha), worktree,
                        )
                    except RetirementReassessment as error:
                        raise PlanningResultInvalid(str(error)) from error
                    except RetirementError as error:
                        self._ports.state.update(
                            context.repo, planning_error=str(error),
                            planning_error_fingerprint=run.current_fingerprint,
                            last_error=str(error),
                        )
                        self._ports.events.error(str(error))
                        return self._operational_error()
                    if manager_paths:
                        if validated_result is None:
                            validated_result = ValidatedPlanningResult(
                                (), (), mutable_snapshot=run.mutable_ready_snapshot,
                            )
                        paths = tuple(sorted(set(validated_result.publication_paths) | set(manager_paths)))
                        hashes = dict(validated_result.publication_hashes)
                        import hashlib
                        for path in manager_paths:
                            target = worktree / path
                            if target.is_file():
                                hashes[path] = hashlib.sha256(target.read_bytes()).hexdigest()
                        validated_result = replace(
                            validated_result, publication_paths=paths,
                            publication_hashes=tuple(sorted(hashes.items())),
                            retirement_moves=tuple(
                                source for source in manager_paths[::2]
                            ),
                            restoration_moves=(),
                        )
                        run.dirty_files = sorted(set(dirty_files) | set(manager_paths))
            if not run.maintenance_planning and hasattr(self._ports.state, "planning_review") and attempt is not None:
                from loop_manager_internal.planning.common_defect_links import validate as validate_links
                try:
                    links = []
                    if 'COMMON_DEFECT_LINKS' in str(row.planning_result or '').upper():
                        ids = json.loads(attempt['common_defect_snapshot']) if 'common_defect_snapshot' in attempt.keys() else []
                        links = validate_links(str(row.planning_result or ''), str(row.active_sha),
                            [item for item in self._ports.state.common_defect_reports(context.repo) if item['id'] in ids]
                                if ids else [],
                            self._ports.state.execplan_rows(context.repo), validated_result,
                            self._ports.git.run, context.repo_path)
                    if links and attempt['purpose'] == 'requirement':
                        raise ValueError('Requirement 완료 점검에서 공통 교정 계획을 연결할 수 없습니다.')
                    self._ports.state.validate_planning_review(
                        context.repo, str(row.active_sha), str(row.planning_result or ""), outcome,
                        **({'common_defect_links': links} if links else {}),
                    )
                except ValueError as error:
                    raise PlanningResultInvalid(str(error)) from error
            if (
                decision is not None
                and not tiered_assessment.enabled(self._ports, run)
                and context.self_repository
                and run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
                and not run.maintenance_cycle.full_assessment_completed
            ):
                try:
                    inventory = self._assessment.inventory(
                        context.repo_path, str(row.active_sha)
                    )
                    report = self._assessment.validate(
                        str(row.planning_result or ""), inventory,
                        str(row.active_sha),
                        decision.plan_ids if decision.outcome is MaintenanceOutcome.REFACTORING else (),
                        {str(item["plan_id"]) for item in
                         self._ports.state.execplan_rows(context.repo)},
                    )
                    if validated_result is not None and validated_result.id_allocations:
                        id_mapping = {
                            allocation.source_id: allocation.target_id
                            for allocation in validated_result.id_allocations
                        }
                        normalized_report = json.loads(report)
                        for finding in normalized_report["findings"]:
                            if finding["status"] == "planned":
                                finding["plan_ids"] = [
                                    id_mapping.get(plan_id, plan_id)
                                    for plan_id in finding["plan_ids"]
                                ]
                        report = json.dumps(
                            normalized_report, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        )
                    run.maintenance_cycle = self._ports.state.record_full_assessment(
                        context.repo,
                        expected_revision=run.maintenance_cycle.revision,
                        source_sha=str(row.active_sha), result=report,
                    )
                    existing_plan_ids = tuple(sorted({plan_id for item in json.loads(report)["findings"]
                        if item["status"] == "existing_plan" for plan_id in item["plan_ids"]}))
                except (AssessmentError,
                        maintenance_state.MaintenanceStateError) as error:
                    raise PlanningResultInvalid(str(error)) from error
            if (getattr(run, "tiered_root_plan_links", ()) and decision is not None
                    and decision.outcome is not MaintenanceOutcome.UNSAFE):
                try:
                    self._ports.tiered.record_final_root_plan_links(
                        run, validated_result,
                    )
                except TieredReviewError as error:
                    raise PlanningResultInvalid(str(error)) from error
        except PlanningResultInvalid as error:
            message = str(error)
            retry_count = row.planning_retry_count
            recheck_context = str(row.planning_validation_error or "").startswith(
                "현재 planning 결과의 "
            )
            if (
                row.planning_result_pending_validation
                and row.planning_validation_error
                and not recheck_context
            ):
                retry_count += 1
            values: dict[str, object] = {
                "planning_validation_error": message,
                "planning_result_pending_validation": 0,
                "planning_result_completed": 0,
                "last_error": None,
                "planning_retry_count": retry_count,
            }
            if retry_count >= MAX_PLANNER_VALIDATION_RETRIES:
                values.update(
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    planning_error_origin="validation",
                    planning_correction_error=None,
                    planning_correction_incident_id=None,
                    planning_correction_sha=None,
                    planning_correction_context=None,
                )
            self._ports.state.update(context.repo, **values)
            self._ports.events.error(message)
            return PlanningOutcome(
                PlanningOutcomeKind.VALIDATION_RETRY, error=True
            )

        if decision is not None and decision.outcome is MaintenanceOutcome.UNSAFE:
            message = (
                "post-refactor revalidation이 제품 목표 보존을 확정하지 못했습니다: "
                + str(decision.reason)
            )
            self._ports.state.update(
                context.repo,
                planning_error=message,
                planning_error_fingerprint=run.current_fingerprint,
                planning_error_origin="unsafe",
                last_error=message,
                planning_correction_error=None,
                planning_correction_incident_id=None,
                planning_correction_sha=None,
                planning_correction_context=None,
            )
            self._ports.events.error(message)
            return PlanningOutcome(
                PlanningOutcomeKind.UNSAFE_REVALIDATION, error=True
            )

        run.published_refactoring = bool(
            decision is not None
            and decision.outcome is MaintenanceOutcome.REFACTORING
        )
        if (
            decision is not None
            and decision.outcome is MaintenanceOutcome.REFACTORING
            and validated_result is not None
        ):
            decision = replace(
                decision, plan_ids=validated_result.refactoring_plan_ids,
            )
        run.maintenance_decision = decision
        run.validated_result = validated_result
        record_result = getattr(self._ports.state, "record_planning_result", None)
        if record_result is not None:
            record_result(context.repo, str(run.current_fingerprint), validated_result)
        if decision is not None and run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT:
            prerequisites = decision.plan_ids if decision.outcome is MaintenanceOutcome.PREREQUISITES else ()
            run.maintenance_cycle = self._ports.state.record_maintenance_scope(context.repo, run.maintenance_cycle.revision, existing_plan_ids,
                prerequisite_plan_ids=prerequisites)
            self._ports.events.log(f"유지보수 평가 확정 범위: 결과={decision.outcome.value}, 계획={','.join((*decision.plan_ids, *existing_plan_ids)) or '없음'} (필요 dependency 포함)")
        if row.planning_error and row.planning_correction_error:
            if self._ports.git.fetch_base(context.repo_path, context.base_branch) != row.active_sha:
                return PlanningOutcome(
                    PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                    detail="검증 중 원격 입력이 전진해 최신 기준에서 다시 확인합니다.",
                    error=True,
                )
            cycle = run.maintenance_cycle
            correction_context = (
                f"{cycle.phase.value}:{cycle.revision}"
                if run.maintenance_planning else "general"
            )
            if self._ports.state.release_planning_correction(
                context.repo, error=row.planning_error,
                active_sha=str(row.active_sha or ""), context=correction_context,
            ):
                run.row = self._ports.state.planning(context.repo)
        return None
