"""Worker execution policy and narrow typed ports."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Callable, Any, Mapping
import hashlib
import json
from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import review_correction_state, runtime_state
from loop_manager_internal.storage import recovery_state
import re
import subprocess
import sys
from uuid import uuid4
from loop_manager_internal.agents.agent_invocation import AgentResult, AgentResultKind, AttemptSequence, TrackedAgentCall, ResolvedProfile
from loop_manager_internal.agents import common_guidance
from loop_manager_internal.execution.worker_session import (
    SessionBinding, SessionTransition, WorkerSessionDataError, decide_session,
    unavailable_transition,
)
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure, classify_exception
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.git.rebase_correction import CorrectionAction, next_action
from loop_manager_internal.storage.rebase_correction_state import CorrectionRace, RebaseCorrectionRepository
from loop_manager_internal.execution.validation_feedback import is_validation_error, render_delta
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.execution.worker_outcome import WorkerOutcome, WorkerOutcomeError, parse_worker_outcome

from loop_manager_internal.execution.execution_effects import ExecutionContract, FeedbackEffects
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository

@dataclass(frozen=True)
class WorkerHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    update_snapshot: Callable[..., None]
    checkpoint: ExecutionCheckpointRepository | None
    contract: ExecutionContract
    feedback: FeedbackEffects | None
    rebase_corrections: RebaseCorrectionRepository
    state: ExecutionStatePort
    _published_plan_allows_common_plans: Callable[..., Any]
    _rebase_in_progress_for_branch: Callable[..., Any]
    _revision_execplan_input_fingerprint: Callable[..., Any]
    begin_agent_scope: Callable[..., Any]
    block_verify_timeout_limit: Callable[..., Any]
    branch_has_changes: Callable[..., Any]
    clear_agent_scope: Callable[..., Any]
    current_rebase_service: Callable[..., Any] | None
    git: Callable[..., Any]
    is_loop_manager_repository: Callable[..., Any]
    operation_log: Callable[..., Any]
    operation_print: Callable[..., Any]
    parse_worker_result: Callable[..., Any]
    requirement_archive_move_set: Callable[..., Any]
    requirement_archive_reconciliation_error: Callable[..., Any]
    resolve_worker_profile: Callable[..., ResolvedProfile]
    run_agent: Callable[..., Any]
    run_agent_with_retries: Callable[..., Any]
    validate_worker_workspace_scope: Callable[..., Any]
    worker_prompt: Callable[..., Any]
    _advanced: Callable[[], ExecutionOutcome]
    _archive_candidates_at: Callable[..., tuple[str, ...]]
    _archive_collision: Callable[..., ExecutionOutcome]
    _block_final: Callable[..., ExecutionOutcome]
    _commit_rebase_resolution: Callable[..., Any]
    _complete_rebase_intent: Callable[..., Any]
    _current_rebase_service: Callable[..., Any]
    _document_draft: Callable[..., Any]
    _execute_rebase_step: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _final: Callable[..., ExecutionOutcome]
    _finish_worker: Callable[..., ExecutionOutcome]
    _metadata_correction: Callable[..., ExecutionOutcome]
    _post_effect_guard: Callable[[ExecutionContext], ExecutionContext | ExecutionOutcome]
    _post_rebase_observation: Callable[..., Any]
    _rebase_conflict: Callable[..., Any]
    _rebase_observation: Callable[..., Any]
    _record_archive_target: Callable[..., None]
    _record_rebase_suboperation: Callable[..., Any]
    _recover: Callable[..., ExecutionOutcome]
    _retry: Callable[..., ExecutionOutcome]
    _row: Callable[[], Mapping[str, object]]
    _run_document: Callable[..., ExecutionOutcome]
    _stage_rebase_correction: Callable[..., Any]
    _unmerged_stages: Callable[..., Any]


class WorkerHandler:
    """Worker policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: WorkerHandlerPorts):
        self.ports = ports

    def _parse_worker_outcome(self, context, message, *, allow_legacy_ready=False):
        allowed = {context.plan.path, *context.plan.sources,
                   'docs/PRODUCT.md', 'docs/ARCHITECTURE.md'}
        documents = {}
        root = context.worktree.resolve()
        for path in allowed:
            target = root / path
            if target.is_file() and target.resolve() == target:
                documents[path] = target.read_text(encoding='utf-8')
        return parse_worker_outcome(
            message, plan_path=context.plan.path,
            sources=context.plan.sources, documents=documents,
            allow_legacy_ready=allow_legacy_ready,
        )

    def _block_worker_decision(self, context, message, outcome: WorkerOutcome):
        if not self.ports._row()['agent_input_fingerprint']:
            # Legacy saved results may have cleared their Agent projection
            # before the disposition contract existed. Anchor release to an
            # immutable remote definition instead of the Worker's local edit.
            revision = context.plan.definition_revision_sha or context.base_sha
            self.ports.state.update_project(
                self.ports.identity.repo,
                agent_input_fingerprint=self.ports._revision_execplan_input_fingerprint(
                    self.ports.identity.repo_path, revision, self.ports.snapshot().catalog_plan,
                ),
            )
        evidence = outcome.evidence()
        for field in ('problem', 'proposal', 'question'):
            evidence[field] = redact_secrets(str(evidence[field]))
        locations = [
            redact_secrets(f"{item.path}#{item.section}")
            for item in outcome.locations
        ]
        evidence['locations'] = [
            {'path': item.path, 'section': redact_secrets(item.section)}
            for item in outcome.locations
        ]
        detail = (
            f"Worker 사용자 결정 대기: {evidence['problem']}\n"
            f"제안: {evidence['proposal']}\n"
            f"질문: {evidence['question']}\n"
            f"근거: {', '.join(locations)}"
        )
        return self.ports._recover(
            RecoveryReason.USER_DECISION, detail, context=context,
            final_block_reason=BlockReasonCode.USER_DECISION_REQUIRED,
            user_targets=tuple(locations),
            worker_result=message, worker_decision=evidence,
        )

    def run(self, context: ExecutionContext) -> ExecutionOutcome:
        self.ports.state.check_context(context)
        row = self.ports._row()
        try:
            worker_binding = SessionBinding.deserialize(row['worker_session_binding'])
            worker_transition = SessionTransition.deserialize(row['worker_session_transition'])
        except WorkerSessionDataError as error:
            raise RecoveryFailure(
                RecoveryReason.STATUS_FAILED,
                f'저장된 Worker session 상태가 유효하지 않습니다: {error}',
            ) from error
        catalog_plan = self.ports.snapshot().catalog_plan
        plan = self.ports.snapshot().plan
        assert catalog_plan is not None and plan is not None
        draft = self.ports._document_draft(context)
        if draft is not None:
            return self.ports._run_document(context, draft)
        self.ports.operation_log(f"Worker 시작: {plan['id']} {plan['title']}")
        correction_turn = None
        if context.resolving_rebase:
            try:
                observation = self.ports._rebase_observation(context)
                current = self.ports.rebase_corrections.get(self.ports.identity.repo, context.plan.plan_id)
                if current is not None and current.worker_result is not None:
                    return self._resume_saved_rebase_worker(
                        context, current, observation,
                    )
                if current is not None and current.pending_turn:
                    # Agent scope retains the pre-turn snapshot across a
                    # process restart, including partial Worker file edits.
                    if (observation.progress != current.progress and row['agent_snapshot'] is None):
                        if self.ports.current_rebase_service is None:
                            return self.ports._recover(
                                RecoveryReason.OWNERSHIP_UNCERTAIN,
                                'pending rebase Worker changed files without its scope baseline',
                                context=context, final_block_reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE)
                        self.ports._current_rebase_service(context).require_owned()
                        self.ports.rebase_corrections.abandon_turn(
                            self.ports.identity.repo, context.plan.plan_id, expected_generation=current.generation)
                        current = self.ports.rebase_corrections.observe(
                            self.ports.identity.repo, context.plan.plan_id, observation,
                            worker_session=row['worker_session_id'], persist_worker_result=False)
                        current = self.ports.rebase_corrections.begin_turn(
                            self.ports.identity.repo, context.plan.plan_id, expected_generation=current.generation,
                            session=row['worker_session_id'])
                    correction_turn = current
                else:
                    current = self.ports.rebase_corrections.observe(
                        self.ports.identity.repo, context.plan.plan_id, observation,
                        expected_generation=current.generation if current else None,
                        worker_session=row['worker_session_id'],
                    )
                    correction_turn = self.ports.rebase_corrections.begin_turn(
                        self.ports.identity.repo, context.plan.plan_id,
                        expected_generation=current.generation,
                        session=row['worker_session_id'],
                    )
            except CorrectionRace:
                return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                     'rebase Worker turn changed', context=context)
            except (RuntimeError, OSError, ValueError) as error:
                return self.ports._recover(
                    classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                    str(error), context=context,
                )
        resolved = self.ports.resolve_worker_profile(
            self.ports.identity.config_path, self.ports.identity.provider, self.ports.snapshot().level, context.worktree,
        )
        review_result_id = None
        review_turn_run_id = None
        if not context.resolving_rebase:
            review_result_id = recovery_state.pending_review_correction(
                self.ports.state.connection, self.ports.identity.repo, str(plan['id']),
                str(row['review_result'] or ''),
            )
            if review_result_id is None and not row['review_result']:
                pending = review_correction_state.load(
                    self.ports.state.connection, self.ports.identity.repo, str(plan['id']),
                ).get('pending')
                if (pending is not None and not pending['completed']
                        and pending['level'] == self.ports.snapshot().level):
                    review_result_id = recovery_state.pending_review_correction(
                        self.ports.state.connection, self.ports.identity.repo, str(plan['id']),
                        None, source_id=pending['source'],
                    )
            if review_result_id is not None:
                allowed, used, review_turn_run_id = recovery_state.reserve_review_correction(
                    self.ports.state.connection, self.ports.identity.repo, str(plan['id']),
                    review_result_id,
                )
                if not allowed:
                    detail = (
                        f"Review 교정 실행 한도 도달: run={review_turn_run_id}, "
                        f"plan={plan['id']}, used={used}/"
                        f"{recovery_state.MAX_REVIEW_CORRECTIONS_PER_RUN}. "
                        "다음 외부 실행에서 저장된 Review 피드백으로 재개합니다.\n"
                        + str(row['review_result'] or '')[-8000:]
                    )
                    return self.ports._recover(
                        RecoveryReason.REVIEW_CORRECTION_BUDGET, detail,
                        context=context, attempts=3,
                        fingerprint=hashlib.sha256(
                            ("review-correction-budget|" + str(plan['id']) + "|"
                             + str(row['review_result'] or '')).encode()
                        ).hexdigest(),
                    )
        if not context.resolving_rebase:
            review_correction_state.begin_worker(
                self.ports.state.connection, self.ports.identity.repo, str(plan['id']),
                self.ports.snapshot().level, review_result_id,
            )
        decision = decide_session(
            row['worker_session_id'],
            worker_binding,
            worker_transition,
            self.ports.identity.provider, resolved, uuid4().hex,
        )
        active_transition = decision.transition
        target = decision.target
        if active_transition is not None and row['worker_session_transition'] is None:
            self.ports.state.update_project(
                self.ports.identity.repo, worker_session_id=None,
                worker_session_transition=active_transition.serialize(),
            )
            row = self.ports._row()
        active_session_id = decision.session_id
        observed_session_id = None
        from loop_manager_internal.maintenance.tiered_ports import prepare_query_context, query_instruction
        from loop_manager_internal.maintenance import code_index
        import sqlite3
        query_context = query_instruction()
        try:
            base = self.ports.identity.base
            revision = code_index.git(self.ports.identity.repo_path, "rev-parse", "--verify",
                                      f"refs/remotes/origin/{base}^{{commit}}").decode().strip()
            query_context = prepare_query_context(self.ports.state.connection, self.ports.identity.repo_path,
                base, revision, self.ports.identity.state_dir)
        except (ValueError, OSError, sqlite3.Error):
            pass  # Source facts are optional context; never block implementation.
        worker_scope_before = self.ports.begin_agent_scope(
            self.ports.identity.repo,
            context.worktree,
            'worker',
            input_fingerprint=self.ports._revision_execplan_input_fingerprint(
                self.ports.identity.repo_path, context.base_sha, catalog_plan,
            ),
        )
        feedback_repo = self.ports.feedback.repository if self.ports.feedback else None
        delivery = None
        def session_started(started_session_id):
            nonlocal observed_session_id
            if observed_session_id == started_session_id:
                return
            self.ports.state.observe_worker_session(
                active_transition.identity if active_transition else None,
                started_session_id, target.serialize(),
            )
            observed_session_id = started_session_id
            if feedback_repo is not None and delivery is not None:
                feedback_repo.bind_session(delivery.attempt_id, started_session_id)
        def worker_call(current_session_id, session_recovery, invocation):
            nonlocal delivery, active_transition
            previous_session = observed_session_id or active_session_id
            if (session_recovery and previous_session
                    and (active_transition is None
                         or self.ports._row()['worker_session_transition'] is None)):
                active_transition = unavailable_transition(
                    str(previous_session),
                    SessionBinding.deserialize(self.ports._row()['worker_session_binding']),
                    target, uuid4().hex,
                )
                self.ports.state.update_project(
                    self.ports.identity.repo, worker_session_id=None,
                    worker_session_transition=active_transition.serialize(),
                )
            previous = active_transition.previous_binding if active_transition else None
            invocation = replace(
                invocation,
                transition_id=active_transition.identity if active_transition else None,
                previous_provider=previous.provider if previous else None,
                previous_model=previous.model if previous else None,
                previous_effort=previous.effort if previous else None,
                previous_session_id=(active_transition.previous_session
                                     if active_transition else None),
            )
            recovering = bool(session_recovery or (
                active_transition is not None and current_session_id is None
            ))
            if feedback_repo is not None:
                delivery = feedback_repo.prepare(
                    self.ports.identity.repo, str(plan['id']), current_session_id,
                    recovery=recovering,
                )
                recovering = delivery.recovery
                validation_feedback = render_delta(delivery.rows)
            else:
                validation_feedback = ''
            prompt_error = row['last_error']
            if is_validation_error(prompt_error, row['verify_result'], row['review_result']):
                prompt_error = None
            prompt = self.ports.worker_prompt(
                row['plan_path'], prompt_error, row['worker_result'], validation_feedback,
                archive_candidates=self.ports.snapshot().archive_candidates, session_recovery=recovering,
                allow_common_plans=allow_common_plans)
            if not current_session_id or recovering:
                prompt = (prompt or '') + query_context
            checkpoint = self.ports.checkpoint
            accepted = (checkpoint.get(self.ports.identity.repo, str(plan['id'])).snapshot or {}).get('current_rebase_acceptance') if checkpoint else None
            if accepted and not context.resolving_rebase:
                prompt = (prompt or '') + (
                    '\n관리 rebase 결과를 현재 상태로 수용했다. 현재 ExecPlan과 근거 입력을 다시 읽고 '
                    '현재 HEAD의 base 대비 최종 diff 전체를 확인하라. 이전 후보의 성공을 재사용하지 말고 '
                    '관련 최소 범위 targeted unit test를 실행해 현재 후보의 결과를 보고하라. '
                    '전체 verify와 Review는 이후 Manager가 수행한다.\n')
            try:
                recovery_state.start_review_correction(
                    self.ports.state.connection, self.ports.identity.repo, str(plan['id']),
                    review_turn_run_id, review_result_id,
                )
                with self.ports.state.connection:
                    resumed = notifications.record_work_start(
                        self.ports.state.connection, self.ports.identity.repo, "Worker",
                        plan_id=str(plan['id']),
                    )
                if resumed:
                    self.ports.operation_log(
                        f"차단 해제 후 Worker 작업 재개: {plan['id']}"
                    )
                return self.ports.run_agent(
                    target.provider, context.worktree, self.ports.identity.state_dir,
                    self.ports.identity.config_path, 'worker',
                    prompt,
                    read_only=False, level=self.ports.snapshot().level, persist_session=True,
                    session_id=current_session_id,
                    on_session_started=session_started,
                    invocation=invocation,
                    resolved_profile=ResolvedProfile(target.model, target.effort),
                )
            except (subprocess.TimeoutExpired, OSError) as error:
                if observed_session_id is None:
                    raise
                detail = (f'Agent CLI timeout: {error}' if isinstance(error, subprocess.TimeoutExpired)
                          else f'Agent CLI 실행 실패: {error}')
                return AgentResult(1, detail, observed_session_id,
                                   AgentResultKind.TRANSIENT_FAILURE)
        allow_common_plans = self.ports._published_plan_allows_common_plans(self.ports.identity.repo_path, catalog_plan)
        result = self.ports.run_agent_with_retries(
            TrackedAgentCall(worker_call, AttemptSequence(
                self.ports.identity.repo, str(plan['id']),
                str(catalog_plan['current_revision_sha']) if catalog_plan['current_revision_sha'] is not None else None,
                initial_transition=decision.kind,
                initial_cause=decision.cause,
            )),
            session_id=active_session_id,
        )
        code, message = result
        if feedback_repo is not None and delivery is not None and result.session_id:
            feedback_repo.bind_session(delivery.attempt_id, result.session_id)
        if result.session_id and observed_session_id != result.session_id:
            session_started(result.session_id)
        try:
            self.ports.update_snapshot(plan=self.ports.validate_worker_workspace_scope(
                self.ports.identity.repo_path,
                context.worktree,
                context.branch,
                self.ports.identity.base,
                str(row['plan_path']),
                catalog_plan,
                worker_scope_before,
                allow_rebase=context.resolving_rebase,
                **({'rebase_definition': DefinitionSnapshot.deserialize(
                    row['definition_snapshot']
                )} if context.resolving_rebase else {}),
                allowed_requirement_archives=self.ports.snapshot().archive_scope_sources,
                allow_common_plans=allow_common_plans,
            ))
        except (self.ports.contract.AgentScopeError, self.ports.contract.WorkspaceOwnershipError) as error:
            scope_error = str(error)
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                scope_error,
                str(row['verify_result'] or ''),
                reason=getattr(error, 'reason_code', BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
            )
            self.ports.operation_print(scope_error, file=sys.stderr)
            return self.ports._final(scope_error)
        except self.ports.contract.ExecPlanMetadataError as error:
            return self.ports._metadata_correction(context, error)
        except RuntimeError as error:
            detail = f"Worker workspace 상태 조회 재시도: {error}"
            self.ports.operation_print(detail, file=sys.stderr)
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                detail, context=context,
            )
        if correction_turn is not None:
            try:
                marker = self.ports._rebase_observation(context).progress
                self.ports.rebase_corrections.finish_turn(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=correction_turn.generation,
                    turn=correction_turn.pending_turn,
                    session=result.session_id or self.ports._row()['worker_session_id'],
                    result=message, scope_marker=marker, code=code,
                    result_kind=result.result_kind.value,
                )
            except CorrectionRace:
                return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                     'rebase Worker result changed', context=context)
            except (RuntimeError, OSError, ValueError) as error:
                return self.ports._recover(
                    classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                    str(error), context=context,
                )
        guarded = self.ports._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            self.ports.clear_agent_scope(self.ports.identity.repo)
            return guarded
        context = guarded
        row = self.ports._row()
        catalog_plan = self.ports.snapshot().catalog_plan
        assert catalog_plan is not None
        if code:
            self.ports.clear_agent_scope(self.ports.identity.repo)
            if context.resolving_rebase:
                saved_turn = self.ports.rebase_corrections.get(self.ports.identity.repo, context.plan.plan_id)
                self.ports.rebase_corrections.abandon_turn(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=saved_turn.generation,
                )
            worker_error = message or f"Agent 종료 코드 {code}"
            if result.result_kind is self.ports.contract.AgentResultKind.TRANSIENT_FAILURE:
                worker_error = f"{self.ports.contract.TRANSIENT_AGENT_ERROR_PREFIX} {worker_error}"
                if context.resolving_rebase:
                    worker_error = self.ports.contract.REBASE_CONFLICT_PREFIX + worker_error
                self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=worker_error)
                self.ports.operation_print(worker_error, file=sys.stderr)
                return self.ports._recover(
                    RecoveryReason.SERVICE_UNAVAILABLE, worker_error,
                    context=context, attempts=3,
                )
            elif result.result_kind is self.ports.contract.AgentResultKind.USAGE_LIMIT:
                worker_error = f"{self.ports.contract.USAGE_LIMIT_ERROR_PREFIX} {worker_error}"
                outcome = ExecutionOutcomeKind.USAGE_PAUSE
            elif context.resolving_rebase:
                worker_error = self.ports.contract.REBASE_CONFLICT_PREFIX + worker_error
                self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=worker_error)
                self.ports.operation_print(worker_error, file=sys.stderr)
                return self.ports._rebase_conflict(
                    context, worker_error, corrected=True, worker_result=message,
                    abnormal_result=True,
                )
            else:
                outcome = ExecutionOutcomeKind.OPERATIONAL_ERROR
            if context.resolving_rebase:
                worker_error = self.ports.contract.REBASE_CONFLICT_PREFIX + worker_error
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=worker_error)
            self.ports.operation_print(worker_error, file=sys.stderr)
            return ExecutionOutcome(outcome, detail=worker_error, error=True)
        try:
            disposition = self._parse_worker_outcome(context, message)
            if disposition.kind == 'needs_user_decision':
                return self._block_worker_decision(context, message, disposition)
            self.ports.parse_worker_result(message, self.ports.snapshot().plan)
        except (WorkerOutcomeError, self.ports.contract.WorkerResultError) as error:
            self.ports.clear_agent_scope(self.ports.identity.repo)
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            if context.resolving_rebase:
                validation_error = self.ports.contract.REBASE_CONFLICT_PREFIX + validation_error
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=validation_error)
            self.ports.operation_print(validation_error, file=sys.stderr)
            if context.resolving_rebase:
                return self.ports._rebase_conflict(
                    context, validation_error, corrected=True, worker_result=message,
                    recovery_reason=RecoveryReason.WORKER_CORRECTION,
                )
            return self.ports._recover(
                RecoveryReason.WORKER_CORRECTION, validation_error,
                context=context,
            )
        self.ports.clear_agent_scope(self.ports.identity.repo)
        delivered_session = result.session_id or self.ports._row()['worker_session_id']
        if feedback_repo is not None and delivery is not None and delivered_session:
            feedback_repo.delivered(delivery.attempt_id, str(delivered_session))
        evidence = re.search(
            r"(?m)^VERIFY_TIMEOUT_LIMIT_INSUFFICIENT:\s*(\S[^\r\n]*)$",
            message,
        )
        if (
            evidence is not None
            and not context.resolving_rebase
            and "전체 verify 시간 초과:" in str(row['verify_result'] or '')
        ):
            detail = self.ports.block_verify_timeout_limit(
                self.ports.identity.repo, context.worktree, message, evidence.group(1),
                str(row['verify_result']),
            )
            self.ports.operation_print(detail, file=sys.stderr)
            return self.ports._final(detail)
        return self.ports._finish_worker(context, row, message)

    def _resume_saved_rebase_worker(self, context, saved, observation):
        """Consume a completed Agent turn after interruption without invoking it twice."""
        checkpoint = self.ports.checkpoint
        intent = checkpoint.get(self.ports.identity.repo, context.plan.plan_id) if checkpoint else None
        if intent and (intent.snapshot or {}).get('pending') in {'stage', 'continue'}:
            return self.ports._retry('saved rebase Worker turn awaits Git recovery')
        if observation.progress != saved.scope_marker:
            if self.ports.current_rebase_service is not None:
                self.ports._current_rebase_service(context).require_owned()
                self.ports.rebase_corrections.abandon_turn(
                    self.ports.identity.repo, context.plan.plan_id, expected_generation=saved.generation)
                self.ports.rebase_corrections.observe(
                    self.ports.identity.repo, context.plan.plan_id, observation,
                    worker_session=self.ports._row()['worker_session_id'], persist_worker_result=False)
                return self.ports._retry('현재 수동 해결 상태를 보존하고 필요한 교정만 다시 확인합니다.')
            return self.ports._recover(
                RecoveryReason.OWNERSHIP_UNCERTAIN,
                'saved rebase Worker result no longer matches the accepted worktree',
                context=context, final_block_reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE)
        if saved.worker_code:
            if self.ports._row()['agent_snapshot'] is not None:
                self.ports.clear_agent_scope(self.ports.identity.repo)
            self.ports.rebase_corrections.abandon_turn(
                self.ports.identity.repo, context.plan.plan_id,
                expected_generation=saved.generation,
            )
            detail = self.ports.contract.REBASE_CONFLICT_PREFIX + (
                saved.worker_result or f'Agent 종료 코드 {saved.worker_code}'
            )
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=detail)
            if saved.result_kind == 'usage_limit':
                return ExecutionOutcome(ExecutionOutcomeKind.USAGE_PAUSE,
                                        detail=detail)
            if saved.result_kind == 'transient_failure':
                return self.ports._recover(RecoveryReason.SERVICE_UNAVAILABLE, detail,
                                     context=context, attempts=3)
            return self.ports._rebase_conflict(
                context, detail, corrected=True, worker_result=saved.worker_result,
                abnormal_result=True,
            )
        guarded = self.ports._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            if self.ports._row()['agent_snapshot'] is not None:
                self.ports.clear_agent_scope(self.ports.identity.repo)
            return guarded
        context = guarded
        try:
            disposition = self._parse_worker_outcome(
                context, saved.worker_result,
                allow_legacy_ready=saved.outcome_contract_version == 0,
            )
            if disposition.kind == 'needs_user_decision':
                return self._block_worker_decision(
                    context, saved.worker_result, disposition,
                )
            self.ports.parse_worker_result(saved.worker_result, self.ports.snapshot().plan)
        except (WorkerOutcomeError, self.ports.contract.WorkerResultError) as error:
            if self.ports._row()['agent_snapshot'] is not None:
                self.ports.clear_agent_scope(self.ports.identity.repo)
            detail = self.ports.contract.REBASE_CONFLICT_PREFIX + (
                f'Worker 결과 검증 실패:\n{error}'
            )
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=detail)
            return self.ports._rebase_conflict(
                context, detail, corrected=True,
                worker_result=saved.worker_result,
                recovery_reason=RecoveryReason.WORKER_CORRECTION,
            )
        if self.ports._row()['agent_snapshot'] is not None:
            self.ports.clear_agent_scope(self.ports.identity.repo)
        return self.ports._finish_worker(context, self.ports._row(), saved.worker_result)

    def _finish_worker(self, context, row, message, *, staged=False):
        assert self.ports.snapshot().catalog_plan is not None and self.ports.snapshot().plan is not None
        latest_sha = str(row['active_sha'] or '')
        if context.resolving_rebase:
            if self.ports.current_rebase_service is not None:
                _, decision = self.ports._current_rebase_service(context).require_owned()
                if decision.status is RebaseStatus.COMPLETE:
                    self.ports._complete_rebase_intent(context)
                    return self.ports._advanced()
            self.ports.state.update_project(self.ports.identity.repo, worker_result=message)
            if not staged:
                self.ports._record_rebase_suboperation(
                    context, 'stage', worker_result=message,
                )
                try:
                    stage = self.ports._stage_rebase_correction(context)
                except (RuntimeError, OSError) as error:
                    return self.ports._recover(
                        classify_exception(error, operation=RecoveryReason.STAGE_FAILED),
                        str(error), context=context,
                    )
                if stage.returncode:
                    return self.ports._recover(RecoveryReason.STAGE_FAILED,
                                         stage.output[-8000:], context=context)
            if self.ports._unmerged_stages(context.worktree):
                detail = self.ports.contract.REBASE_CONFLICT_PREFIX + 'unmerged index remains after Worker correction'
                self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=detail)
                return self.ports._rebase_conflict(context, detail, corrected=True,
                                             worker_result=message)
            staged = self.ports.git(context.worktree, 'diff', '--cached', '--quiet', check=False)
            if staged.returncode not in {0, 1}:
                staging_error = f"rebase 충돌 해결 결과 확인 실패 (git 종료 코드 {staged.returncode})."
                self.ports.operation_print(staging_error, file=sys.stderr)
                return self.ports._recover(RecoveryReason.STAGE_FAILED, staging_error,
                                     context=context)
            self.ports._record_rebase_suboperation(context, 'continue')
            observation = self.ports._rebase_observation(context)
            action = next_action(
                observation, worker_result=True, staged=staged.returncode == 1,
            )
            if action is CorrectionAction.USER_ACTION:
                return self.ports._recover(
                    RecoveryReason.OWNERSHIP_UNCERTAIN,
                    'autostash_identity_unavailable', context=context,
                    final_block_reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                )
            try:
                continued = self.ports._execute_rebase_step(context, action)
            except (RuntimeError, OSError) as error:
                return self.ports._recover(
                    classify_exception(error, operation=RecoveryReason.REBASE_CONTINUE_FAILED),
                    str(error), context=context,
                )
            if continued.returncode and not self.ports._unmerged_stages(context.worktree):
                if self.ports._rebase_in_progress_for_branch(
                    context.worktree, context.branch
                ):
                    return self.ports._recover(
                        RecoveryReason.REBASE_CONTINUE_FAILED,
                        continued.output[-8000:], context=context,
                    )
            if action is CorrectionAction.VERIFY and staged.returncode == 1 and self.ports.checkpoint is not None:
                title = self.ports.parse_worker_result(message, self.ports.snapshot().plan).commit_title
                self.ports._commit_rebase_resolution(context, title)
            post_conflict = self.ports._post_rebase_observation(
                context, str(
                    (self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id).target_base_oid
                     if self.ports.checkpoint is not None else None)
                    or row['pr_conflict_base_oid'] or row['active_sha'] or ''
                ),
                continued.output, corrected=True, worker_result=message,
            )
            if post_conflict is not None:
                return post_conflict
            if continued.returncode:
                continuation_error = self.ports.contract.REBASE_CONFLICT_PREFIX + continued.output[-8000:]
                in_progress = self.ports._rebase_in_progress_for_branch(context.worktree, context.branch)
                if in_progress:
                    self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=continuation_error)
                self.ports.operation_print(continuation_error, file=sys.stderr)
                return self.ports._recover(
                    RecoveryReason.REBASE_CONFLICT if in_progress else RecoveryReason.REBASE_CONTINUE_FAILED,
                    continuation_error, context=context,
                )
            if not self.ports.branch_has_changes(context.worktree, self.ports.identity.base):
                no_change_error = 'rebase 충돌 해결 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
                self.ports._block_final(
                    self.ports.identity.repo,
                    context.worktree,
                    no_change_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PLAN_INPUT_INVALID,
                    recovery_reason=RecoveryReason.NO_CHANGE,
                )
                self.ports.operation_print(no_change_error, file=sys.stderr)
                return self.ports._final(no_change_error)
            # The conflict may have been resolved on an older rebase target
            # while origin advanced again. Keep the last *proven* base until
            # the next revision gate can refresh the completed worktree.
            latest_sha = (
                str(row['pr_conflict_base_oid'])
                if int(row['pr_conflict_recovery'])
                else str(
                    (self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id).target_base_oid
                     if self.ports.checkpoint is not None else None)
                    or row['active_sha'] or ''
                )
            )
            self.ports._complete_rebase_intent(context, project_values={
                'phase': 'WORKING', 'active_sha': latest_sha,
                'validated_base_sha': None, 'last_error': None,
                'verify_result': None, 'review_result': None,
                'verify_token': None, 'review_token': None,
                'publication_token': None,
            })
            return self.ports._advanced()
        if context.archive_sources:
            post_worker_revision = latest_sha if context.resolving_rebase else str(row['active_sha'] or '')
            try:
                expected_archives = self.ports._archive_candidates_at(
                    context, post_worker_revision
                )
            except RecoveryFailure as error:
                return self.ports._recover(error.reason, str(error), context=context)
            except self.ports.contract.RequirementArchiveCollisionError as error:
                return self.ports._archive_collision(context, error)
            except self.ports.contract.RequirementArchiveError as error:
                archive_error = f"Requirement archive eligibility 확인 실패: {error}"
                if self.ports._fetch_latest_base() != post_worker_revision:
                    return self.ports._recover(
                        RecoveryReason.REMOTE_SNAPSHOT_RACE,
                        archive_error, context=context,
                    )
                outcome = self.ports._recover(
                    RecoveryReason.CATALOG_INVALID,
                    archive_error, context=context,
                )
                self.ports.operation_print(archive_error, file=sys.stderr)
                return outcome
            try:
                actual_archives = self.ports.requirement_archive_move_set(
                    self.ports.identity.repo_path,
                    post_worker_revision,
                    context.worktree,
                )
            except self.ports.contract.GitLockError as error:
                return self.ports._recover(RecoveryReason.GIT_LOCK, str(error), context=context)
            except self.ports.contract.RequirementArchiveCollisionError as error:
                return self.ports._archive_collision(context, error)
            except self.ports.contract.RequirementArchiveError as error:
                archive_error = (
                    f"Requirement archive 이동 검증 실패: {error}\n"
                    f"기준 revision: {post_worker_revision}; "
                    f"기대: {json.dumps(expected_archives, ensure_ascii=False)}"
                )
                self.ports.state.update_project(
                    self.ports.identity.repo, phase='WORKING', validated_base_sha=None,
                    worker_result=message,
                    worker_input_token=None, archive_token=None,
                    verify_token=None, review_token=None,
                    publication_token=None, last_error=archive_error,
                )
                self.ports.operation_print(
                    'Requirement archive 이동을 같은 Worker 세션에서 다시 확인합니다.',
                    file=sys.stderr,
                )
                try:
                    self.ports._record_archive_target('CORRECTION_PENDING')
                except RecoveryFailure as race:
                    return self.ports._recover(race.reason, str(race), context=context)
                return self.ports._recover(
                    RecoveryReason.ARCHIVE_MOVE_MISMATCH,
                    archive_error, context=context,
                )
            archive_error = self.ports.requirement_archive_reconciliation_error(
                expected_archives, actual_archives,
                revision=post_worker_revision,
            )
            if archive_error is not None:
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    worker_result=message,
                    worker_input_token=None,
                    archive_token=None,
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                    last_error=archive_error,
                )
                self.ports.operation_print(
                    'Requirement archive 이동 집합을 같은 Worker 세션에서 다시 맞춥니다.',
                    file=sys.stderr,
                )
                try:
                    self.ports._record_archive_target(
                        'CORRECTION_PENDING', moves=actual_archives
                    )
                except RecoveryFailure as error:
                    return self.ports._recover(error.reason, str(error), context=context)
                return self.ports._recover(
                    RecoveryReason.ARCHIVE_MOVE_MISMATCH,
                    archive_error, context=context,
                    fingerprint=hashlib.sha256(
                        json.dumps((post_worker_revision, context.archive_snapshot_token,
                                    sorted(expected_archives or ()), sorted(actual_archives or ()))).encode()
                    ).hexdigest(),
                )
            try:
                self.ports._record_archive_target(
                    'WORKTREE_UPDATED', moves=actual_archives
                )
            except RecoveryFailure as error:
                return self.ports._recover(error.reason, str(error), context=context)
        definition = self.ports.snapshot()._definition_snapshot
        if definition is not None:
            definition = replace(definition, worker_guidance_hash=common_guidance.worker_content_hash(
                self.ports.state.connection, self.ports.identity.repo, context.plan.plan_id,
            ))
            self.ports.update_snapshot(_definition_snapshot=definition)
        if self.ports.checkpoint is not None and self.ports.snapshot()._definition_snapshot is not None:
            current = self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            if (current.snapshot or {}).get('current_rebase_acceptance'):
                head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
                self.ports.checkpoint.record(
                    self.ports.identity.repo, context.plan.plan_id, expected_generation=current.generation,
                    operation=current.operation, expected_head=head,
                    target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid,
                    snapshot={**current.snapshot, 'worker_candidate_token':
                              self.ports.snapshot()._definition_snapshot.worker_candidate_token(head)},
                )
        with runtime_state._transaction(self.ports.state.connection):
            if not context.resolving_rebase:
                review_correction_state.complete_worker(
                    self.ports.state.connection, self.ports.identity.repo,
                    context.plan.plan_id, self.ports.snapshot().level,
                )
            self.ports.state.update_project(
                self.ports.identity.repo, preserve_transaction=True,
                phase='VERIFYING',
                **({'definition_snapshot': definition.serialize()} if definition is not None else {}),
                validated_base_sha=None,
                worker_result=message,
                verify_result=None,
                review_result=None,
                worker_input_token=(
                    self.ports.snapshot()._definition_snapshot.worker_token
                    if self.ports.snapshot()._definition_snapshot is not None else None
                ),
                archive_token=(
                    self.ports.snapshot()._definition_snapshot.archive_token
                    if self.ports.snapshot()._definition_snapshot is not None else None
                ),
                verify_token=None,
                review_token=None,
                publication_token=None,
                last_error=None,
            )
        return self.ports._advanced()
