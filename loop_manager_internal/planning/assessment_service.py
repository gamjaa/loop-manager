"""Planner invocation, source-bound file assessment, and result validation."""
from __future__ import annotations
from pathlib import Path
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
            )
        )
        file_review_enabled = (
            self._file_assessment_active(run)
            and hasattr(self._ports, "file_review")
        )
        if should_run:
            final_page = None
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
                if (context.self_repository and run.maintenance_cycle.phase
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
            def save_final_session(session_id):
                if final_page is not None:
                    self._ports.reconciliation.synthesis_session(context.repo, final_page, session_id)
                self._ports.state.update(context.repo, planner_session_id=session_id)

            before = self._ports.agent.begin_scope(role, worktree)
            result = self._ports.agent.run_planner(
                worktree, prompt, final_page.session_id if final_page is not None else row.planner_session_id,
                save_final_session,
            )
            if result.session_id:
                save_final_session(result.session_id)
            scope_result = self._ports.agent.finish_scope(role, worktree, before)
            if scope_result.error:
                scope_error = scope_result.error
                if scope_result.result_snapshot is not None:
                    self._ports.state.update(
                        context.repo,
                        agent_result_snapshot=scope_result.result_snapshot,
                    )
            else:
                self._ports.agent.clear_scope(context.repo)

            if result.code:
                message = result.message or f"Planner 종료 코드 {result.code}"
                if result.kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
                    outcome = PlanningOutcomeKind.TRANSIENT_PAUSE
                elif result.kind is PlanningAgentResultKind.USAGE_LIMIT:
                    outcome = PlanningOutcomeKind.USAGE_LIMIT_PAUSE
                else:
                    outcome = PlanningOutcomeKind.OPERATIONAL_ERROR
                self._ports.state.update(
                    context.repo,
                    last_error=message,
                    planning_result_pending_validation=0,
                    planning_result_completed=0,
                    planning_result=None,
                )
                self._ports.events.error(message)
                return PlanningOutcome(outcome, error=True)
            self._ports.state.update(
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
        self._ports.git.run(context.repo_path, "fetch", "origin")
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
        with self._ports.state.transaction():
            self._ports.state.complete_file_assessment(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(row.active_sha), evidence,
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
        """Persist at most one source-bound review unit per scheduler turn."""
        context = run.context
        review = self._ports.file_review
        try:
            progress = review.progress(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(run.row.active_sha),
            )
            unit = progress.next_unit
            if unit is None:
                return None
            content = review.content(context.repo_path, context.base_branch, unit.source)
            prompt = maintenance_file_review.review_prompt(
                unit.source, content, root_placements=progress.root_placements,
            )
            if unit.error:
                prompt += "\n이전 검토 결과 오류를 수정하라: " + unit.error
            review.start(context.repo, run.maintenance_cycle.revision, unit.source)
        except (maintenance_file_review.FileReviewError, SourceInventoryError) as error:
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART,
                                   detail=str(error))
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        result = self._ports.agent.run_planner(
            worktree, prompt, unit.session_id,
            lambda session_id: review.session(
                context.repo, run.maintenance_cycle.revision, unit.source, session_id,
            ),
        )
        if result.session_id:
            review.session(context.repo, run.maintenance_cycle.revision,
                           unit.source, result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(context.repo)
        if result.code:
            self._ports.events.error(result.message)
            if result.kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
                kind = PlanningOutcomeKind.TRANSIENT_PAUSE
            elif result.kind is PlanningAgentResultKind.USAGE_LIMIT:
                kind = PlanningOutcomeKind.USAGE_LIMIT_PAUSE
            else:
                kind = PlanningOutcomeKind.OPERATIONAL_ERROR
            return PlanningOutcome(kind, error=True)
        try:
            # Re-read the current ref before accepting any Agent-supplied claim.
            content = review.content(context.repo_path, context.base_branch, unit.source)
            parsed = maintenance_file_review.parse_review(
                result.message, unit.source, content,
            )
            review.complete(context.repo, run.maintenance_cycle.revision,
                            unit.source, parsed)
        except (maintenance_file_review.FileReviewError, SourceInventoryError) as error:
            review.fail(context.repo, run.maintenance_cycle.revision,
                        unit.source, str(error))
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY,
                                   detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

    def _reconcile_one_relation(self, run: _PlanningRun, worktree: Path) -> PlanningOutcome | None:
        """Verify one cross-file candidate per turn, retaining session progress."""
        context = run.context
        port = self._ports.reconciliation
        try:
            progress = port.progress(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(run.row.active_sha),
            )
            if progress.pending_scan:
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            relation = progress.next_relation
            if relation is None:
                return self._synthesize_one_page(run, worktree, progress)
            left = port.content(context.repo_path, context.base_branch, relation.left)
            right = port.content(context.repo_path, context.base_branch, relation.right)
            prompt = maintenance_reconciliation.relation_prompt(relation, left, right)
            port.start(context.repo, run.maintenance_cycle.revision,
                       str(run.row.active_sha), relation)
            session_id, previous_error = port.unit(context.repo, relation)
            if previous_error:
                prompt += "\n이전 관계 확인 결과 오류를 수정하라: " + previous_error
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART, detail=str(error))
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        result = self._ports.agent.run_planner(
            worktree, prompt, session_id,
            lambda value: port.session(context.repo, run.maintenance_cycle.revision,
                                       relation, value),
        )
        if result.session_id:
            port.session(context.repo, run.maintenance_cycle.revision, relation, result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(context.repo)
        if result.code:
            self._ports.events.error(result.message)
            if result.kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
                kind = PlanningOutcomeKind.TRANSIENT_PAUSE
            elif result.kind is PlanningAgentResultKind.USAGE_LIMIT:
                kind = PlanningOutcomeKind.USAGE_LIMIT_PAUSE
            else:
                kind = PlanningOutcomeKind.OPERATIONAL_ERROR
            return PlanningOutcome(kind, error=True)
        try:
            left = port.content(context.repo_path, context.base_branch, relation.left)
            right = port.content(context.repo_path, context.base_branch, relation.right)
            parsed = maintenance_reconciliation.parse_relation(result.message, relation, left, right)
            port.complete(context.repo, run.maintenance_cycle.revision, relation, parsed)
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            port.fail(context.repo, run.maintenance_cycle.revision, relation, str(error))
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

    def _synthesize_one_page(self, run: _PlanningRun, worktree: Path,
                             progress) -> PlanningOutcome | None:
        """Assess one page independently; only its durable result crosses sessions."""
        context, port = run.context, self._ports.reconciliation
        page = port.synthesis(context.repo, run.maintenance_cycle.revision,
                              str(run.row.active_sha), progress)
        if page.index == page.total:
            return self._collect_findings_unit(run, worktree)
        manifest = port.synthesis_export(context.repo, page, progress,
                                         context.state_dir / "maintenance-evidence")
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        session_errors = []

        def save_session(session_id):
            try:
                port.synthesis_session(context.repo, page, session_id)
            except maintenance_reconciliation.ReconciliationError as error:
                session_errors.append(str(error))

        result = self._ports.agent.run_planner(
            worktree, maintenance_reconciliation.synthesis_prompt(page, manifest),
            page.session_id, save_session,
        )
        if result.session_id:
            save_session(result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(context.repo)
        if result.code:
            self._ports.events.error(result.message)
            kind = {PlanningAgentResultKind.TRANSIENT_FAILURE: PlanningOutcomeKind.TRANSIENT_PAUSE,
                    PlanningAgentResultKind.USAGE_LIMIT: PlanningOutcomeKind.USAGE_LIMIT_PAUSE}.get(
                        result.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            return PlanningOutcome(kind, error=True)
        try:
            if session_errors:
                raise maintenance_reconciliation.ReconciliationError(session_errors[0])
            current = port.progress(context.repo, context.repo_path, context.base_branch,
                                    run.maintenance_cycle.revision, str(run.row.active_sha))
            fresh = port.synthesis(context.repo, run.maintenance_cycle.revision,
                                   str(run.row.active_sha), current)
            if fresh.key != page.key:
                raise maintenance_reconciliation.ReconciliationError("종합 중 source가 바뀌었습니다.")
            port.synthesis_complete(context.repo, page, result.message)
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

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
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        result = self._ports.agent.run_planner(worktree, maintenance_findings.unit_prompt(unit, manifest),
            unit.session_id, lambda session_id: port.findings_session(context.repo, unit, session_id))
        if result.session_id:
            port.findings_session(context.repo, unit, result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(context.repo)
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
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

    def _resolve_one_page(self, run: _PlanningRun, worktree: Path,
                          unit: maintenance_reconciliation.ResolutionUnit) -> PlanningOutcome:
        """Keep the final Planner result pending while one coverage receipt is validated."""
        repo, port = run.context.repo, self._ports.reconciliation
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        result = self._ports.agent.run_planner(
            worktree, maintenance_reconciliation.resolution_prompt(unit), unit.session_id,
            lambda session_id: port.resolution_session(repo, unit, session_id),
        )
        if result.session_id:
            port.resolution_session(repo, unit, result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(repo)
        if result.code:
            self._ports.events.error(result.message)
            kind = {PlanningAgentResultKind.TRANSIENT_FAILURE: PlanningOutcomeKind.TRANSIENT_PAUSE,
                    PlanningAgentResultKind.USAGE_LIMIT: PlanningOutcomeKind.USAGE_LIMIT_PAUSE}.get(
                        result.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            return PlanningOutcome(kind, error=True)
        try:
            port.resolution_complete(repo, unit, result.message)
        except maintenance_reconciliation.ReconciliationError as error:
            port.resolution_fail(repo, unit, str(error))
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

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
                if (run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
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
                validated_result = self._ports.validation.validate_general_result(
                    context.repo,
                    worktree,
                    str(row.active_sha or ""),
                    dirty_files,
                    run.mutable_ready_snapshot,
                )
            if (
                decision is not None
                and context.self_repository
                and run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
                and not run.maintenance_cycle.full_assessment_completed
                and decision.outcome is not MaintenanceOutcome.PREREQUISITES
            ):
                try:
                    inventory = self._assessment.inventory(
                        context.repo_path, str(row.active_sha)
                    )
                    report = self._assessment.validate(
                        str(row.planning_result or ""), inventory,
                        str(row.active_sha),
                        decision.plan_ids
                        if decision.outcome is MaintenanceOutcome.REFACTORING else (),
                        {str(item["plan_id"]) for item in
                         self._ports.state.execplan_rows(context.repo)},
                    )
                    run.maintenance_cycle = self._ports.state.record_full_assessment(
                        context.repo,
                        expected_revision=run.maintenance_cycle.revision,
                        source_sha=str(row.active_sha), result=report,
                    )
                except (AssessmentError,
                        maintenance_state.MaintenanceStateError) as error:
                    raise PlanningResultInvalid(str(error)) from error
        except PlanningResultInvalid as error:
            message = str(error)
            retry_count = row.planning_retry_count
            if (
                row.planning_result_pending_validation
                and row.planning_validation_error
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
                )
            self._ports.state.update(context.repo, **values)
            self._ports.events.error(message)
            return PlanningOutcome(
                PlanningOutcomeKind.VALIDATION_RETRY, error=True
            )

        if (
            decision is not None
            and decision.outcome is MaintenanceOutcome.PREREQUISITES
        ):
            try:
                self._ports.git.validate_worktree_ownership(
                    context.repo,
                    str(row.planning_attempt_fingerprint or run.current_fingerprint or ""),
                    worktree,
                )
                with self._ports.state.transaction():
                    self._ports.state.transition_maintenance(
                        context.repo,
                        expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                        expected_revision=run.maintenance_cycle.revision,
                        next_phase=(
                            maintenance_state.MaintenancePhase.PREREQUISITE_WAIT
                        ),
                        prerequisite_plan_ids=decision.plan_ids,
                        include_general_planning=False,
                    )
                    self._ports.state.clear_active_in_transaction(context.repo)
                    self._ports.git.run(
                        context.repo_path,
                        "worktree",
                        "remove",
                        str(worktree),
                    )
            except RuntimeError as error:
                message = str(error)
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
            return PlanningOutcome(PlanningOutcomeKind.PREREQUISITE_WAIT)

        if decision is not None and decision.outcome is MaintenanceOutcome.UNSAFE:
            message = (
                "post-refactor revalidation이 제품 목표 보존을 확정하지 못했습니다: "
                + str(decision.reason)
            )
            self._ports.state.update(
                context.repo,
                planning_error=message,
                planning_error_fingerprint=run.current_fingerprint,
                last_error=message,
            )
            self._ports.events.error(message)
            return PlanningOutcome(
                PlanningOutcomeKind.UNSAFE_REVALIDATION, error=True
            )

        run.published_refactoring = bool(
            decision is not None
            and decision.outcome is MaintenanceOutcome.REFACTORING
        )
        run.maintenance_decision = decision
        run.validated_result = validated_result
        if row.planning_error and row.planning_error == row.planning_validation_error:
            self._ports.state.update(
                context.repo, planning_error=None, planning_error_fingerprint=None,
            )
            run.row = self._ports.state.planning(context.repo)
        return None
