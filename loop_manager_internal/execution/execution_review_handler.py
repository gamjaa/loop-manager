"""Review execution policy and narrow typed ports."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any, Mapping
from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents import document_publication
import sys
from loop_manager_internal.storage import review_correction_state
from loop_manager_internal.agents.agent_invocation import AttemptSequence, TrackedAgentCall
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, classify_exception
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.execution.validation_feedback import normalize_feedback, redact_secrets
from loop_manager_internal.documents.reviewer_specification import SpecificationDeadlock, validate_locations

from loop_manager_internal.execution.execution_effects import ExecutionContract, FeedbackEffects
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort

@dataclass(frozen=True)
class ReviewHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    contract: ExecutionContract
    feedback: FeedbackEffects | None
    state: ExecutionStatePort
    _worktree_execplan_input_fingerprint: Callable[..., Any]
    begin_agent_scope: Callable[..., Any]
    capture_git_snapshot: Callable[..., Any]
    clear_agent_scope: Callable[..., Any]
    clear_validation_failure_history: Callable[..., Any]
    git: Callable[..., Any]
    operation_log: Callable[..., Any]
    operation_print: Callable[..., Any]
    parse_reviewer_result: Callable[..., Any]
    parse_worker_result: Callable[..., Any]
    reviewer_prompt: Callable[..., Any]
    run_agent: Callable[..., Any]
    run_agent_with_retries: Callable[..., Any]
    transition_validation_failure: Callable[..., Any]
    validate_agent_scope: Callable[..., Any]
    validate_managed_worktree: Callable[..., Any]
    _advanced: Callable[[], ExecutionOutcome]
    _block_final: Callable[..., ExecutionOutcome]
    _document_block: Callable[..., ExecutionOutcome]
    _document_draft: Callable[..., Any]
    _document_guard: Callable[..., Any]
    _document_review_prompt: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _final: Callable[..., ExecutionOutcome]
    _latest_before_verify: Callable[..., Any]
    _post_effect_guard: Callable[[ExecutionContext], ExecutionContext | ExecutionOutcome]
    _recover: Callable[..., ExecutionOutcome]
    _retry: Callable[..., ExecutionOutcome]
    _row: Callable[[], Mapping[str, object]]


class ReviewHandler:
    """Review policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: ReviewHandlerPorts):
        self.ports = ports

    def _run_reviewer_attempt(self, context, *args, **kwargs):
        """Persist returned provider failures before the retry runner advances."""
        result = self.ports.run_agent(*args, **kwargs)
        code, raw = result
        if code and self.ports.feedback is not None:
            self.ports.feedback.repository.record_result(
                self.ports.identity.repo, context.plan.plan_id, context.plan.definition_revision_sha,
                'review', getattr(result, 'ledger_kind', 'agent_failure'), raw,
            )
        return result

    def _review_entry_evidence(self, context):
        """Require current Worker and verify evidence before invoking Reviewer."""
        row = self.ports._row()
        try:
            worker_publication = self.ports.parse_worker_result(
                str(row['worker_result'] or ''), self.ports.snapshot().plan,
            )
        except self.ports.contract.WorkerResultError as error:
            detail = f"Reviewer 시작 전 Worker 결과 검증 실패:\n{error}"
            self.ports.state.update_project(
                self.ports.identity.repo, phase='WORKING', validated_base_sha=None,
                verify_result=None, review_result=None, verify_token=None,
                review_token=None, publication_token=None, last_error=detail,
            )
            self.ports.operation_print(detail, file=sys.stderr)
            return self.ports._recover(
                RecoveryReason.WORKER_CORRECTION, detail, context=context,
            )

        # Every normal execution turn installs this durable definition snapshot
        # in guard(). Keep legacy direct callers from inventing a verification
        # identity; they must return through the normal correction path.
        if self.ports.snapshot()._definition_snapshot is None:
            detail = 'Reviewer 시작 전 검증 대상 ExecPlan 선언 증거가 없습니다.'
            self.ports.state.update_project(
                self.ports.identity.repo, phase='VERIFYING', validated_base_sha=None,
                verify_result=None, review_result=None, verify_token=None,
                review_token=None, publication_token=None, last_error=detail,
            )
            self.ports.operation_print(detail, file=sys.stderr)
            return self.ports._retry(detail)

        latest_sha = self.ports._fetch_latest_base()
        if latest_sha != row['active_sha']:
            return self.ports._latest_before_verify(context, latest_sha)

        candidate_head = self.ports.git(
            context.worktree, 'rev-parse', 'HEAD',
        ).stdout.strip()
        expected_verify = self.ports.snapshot()._definition_snapshot.verify_token(
            latest_sha, candidate_head,
        )
        if (
            row['verify_result'] is None
            or row['validated_base_sha'] != latest_sha
            or row['verify_token'] != expected_verify
        ):
            detail = 'Reviewer 시작 전 현재 후보 revision의 성공한 전체 verify 증거가 없습니다. 다시 검증합니다.'
            self.ports.state.update_project(
                self.ports.identity.repo, phase='VERIFYING', validated_base_sha=None,
                verify_result=None, review_result=None, verify_token=None,
                review_token=None, publication_token=None, last_error=detail,
            )
            self.ports.operation_print(detail, file=sys.stderr)
            return self.ports._retry(detail)
        return worker_publication

    def review(self, context: ExecutionContext) -> ExecutionOutcome:
        self.ports.state.check_context(context)
        row = self.ports._row()
        assert self.ports.snapshot().catalog_plan is not None and self.ports.snapshot().plan is not None
        draft = self.ports._document_draft(context)
        if draft is not None:
            try:
                self.ports._document_guard(context, draft)
            except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
                return self.ports._document_block(context, draft, error)
        review_level = self.ports.snapshot().level
        entry_evidence = self._review_entry_evidence(context)
        if isinstance(entry_evidence, ExecutionOutcome):
            return entry_evidence
        candidate_head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        correction = review_correction_state.load(
            self.ports.state.connection, self.ports.identity.repo, context.plan.plan_id,
        )
        candidate = correction.get('candidate')
        if candidate and candidate['completed'] and candidate['verified_head'] == candidate_head:
            review_level = candidate['level']
        elif 'level' in correction:
            pending = correction.get('pending')
            # Preserve the conservative interpretation of older stored candidates.
            review_level = (pending['level'] if pending and pending['completed']
                            and pending['verified_head'] == candidate_head
                            else correction['level'])
        targeted_test_reason = self.ports.contract.reviewer_targeted_test_reason(
            entry_evidence.test_evidence,
        )
        protocol_error: str | None = None
        verdict: str | None = None
        review = ''
        for protocol_attempt in range(1, self.ports.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS + 1):
            if protocol_attempt == 1:
                self.ports.operation_log(f"Reviewer 시작: {self.ports.snapshot().plan['id']} {self.ports.snapshot().plan['title']}")
            else:
                self.ports.operation_log(
                    f"Reviewer 프로토콜 교정 재시도: {self.ports.snapshot().plan['id']} {self.ports.snapshot().plan['title']} ({protocol_attempt}/{self.ports.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS})",
                )
            reviewer_scope_before = self.ports.begin_agent_scope(
                self.ports.identity.repo,
                context.worktree,
                'reviewer',
                input_fingerprint=self.ports._worktree_execplan_input_fingerprint(context.worktree, self.ports.snapshot().catalog_plan),
            )
            review_result = self.ports.run_agent_with_retries(
                TrackedAgentCall(lambda current_session_id, session_recovery, invocation: self._run_reviewer_attempt(
                    context,
                    self.ports.identity.provider,
                    context.worktree,
                    self.ports.identity.state_dir,
                    self.ports.identity.config_path,
                    'reviewer',
                    self.ports._document_review_prompt(
                        self.ports.reviewer_prompt(
                            self.ports.identity.base,
                            targeted_test_reason,
                            row['review_result'],
                            session_recovery=session_recovery,
                            protocol_error=protocol_error,
                        ),
                        draft,
                    ),
                    read_only=True,
                    persist_session=True,
                    session_id=current_session_id,
                    on_session_started=lambda started_session_id: self.ports.state.update_project(self.ports.identity.repo, reviewer_session_id=started_session_id),
                    invocation=invocation,
                ), AttemptSequence(self.ports.identity.repo, str(self.ports.snapshot().plan['id']),
                                   str(self.ports.snapshot().catalog_plan['current_revision_sha']) if self.ports.snapshot().catalog_plan['current_revision_sha'] is not None else None)),
                session_id=row['reviewer_session_id'],
            )
            code, review = review_result
            review = redact_secrets(str(review))
            # Nonzero returned attempts are already audited before retries;
            # post-call rejection records classify only unaudited zero exits.
            feedback_repo = self.ports.feedback.repository if self.ports.feedback else None
            if review_result.session_id:
                self.ports.state.update_project(self.ports.identity.repo, reviewer_session_id=review_result.session_id)
            try:
                reviewer_scope_after = self.ports.capture_git_snapshot(context.worktree)
                self.ports.validate_agent_scope(
                    'reviewer',
                    context.worktree,
                    reviewer_scope_before,
                    reviewer_scope_after,
                    external_worktree=self.ports.identity.repo_path,
                    base_branch=self.ports.identity.base,
                )
                self.ports.validate_managed_worktree(self.ports.identity.repo_path, context.worktree, context.branch)
            except (self.ports.contract.AgentScopeError, self.ports.contract.WorkspaceOwnershipError) as error:
                if feedback_repo is not None and not code:
                    feedback_repo.record_result(
                        self.ports.identity.repo, str(self.ports.snapshot().plan['id']), context.plan.definition_revision_sha,
                        'review', 'scope_failure', review,
                    )
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
            except RuntimeError as error:
                if feedback_repo is not None and not code:
                    feedback_repo.record_result(
                        self.ports.identity.repo, str(self.ports.snapshot().plan['id']), context.plan.definition_revision_sha,
                        'review', 'scope_failure', review,
                    )
                detail = f"Reviewer workspace 상태 조회 재시도: {error}"
                self.ports.operation_print(detail, file=sys.stderr)
                return self.ports._recover(
                    classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                    detail, context=context,
                )
            self.ports.clear_agent_scope(self.ports.identity.repo)
            guarded = self.ports._post_effect_guard(context)
            if isinstance(guarded, ExecutionOutcome):
                if feedback_repo is not None and not code:
                    feedback_repo.record_result(
                        self.ports.identity.repo, str(self.ports.snapshot().plan['id']), context.plan.definition_revision_sha,
                        'review', 'stale_result', review,
                    )
                return guarded
            context = guarded
            row = self.ports._row()
            if code:
                reviewer_error = review or f"Reviewer 종료 코드 {code}"
                if review_result.result_kind is self.ports.contract.AgentResultKind.TRANSIENT_FAILURE:
                    reviewer_error = f"{self.ports.contract.TRANSIENT_AGENT_ERROR_PREFIX} {reviewer_error}"
                    self.ports.state.update_project(
                        self.ports.identity.repo, phase='REVIEWING', last_error=reviewer_error,
                        review_result=review,
                    )
                    self.ports.operation_print(reviewer_error, file=sys.stderr)
                    return self.ports._recover(
                        RecoveryReason.SERVICE_UNAVAILABLE, reviewer_error,
                        context=context, attempts=3,
                    )
                elif review_result.result_kind is self.ports.contract.AgentResultKind.USAGE_LIMIT:
                    reviewer_error = f"{self.ports.contract.USAGE_LIMIT_ERROR_PREFIX} {reviewer_error}"
                    outcome = ExecutionOutcomeKind.USAGE_PAUSE
                else:
                    outcome = ExecutionOutcomeKind.OPERATIONAL_ERROR
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    phase='REVIEWING',
                    last_error=reviewer_error,
                    review_result=review,
                )
                self.ports.operation_print(reviewer_error, file=sys.stderr)
                return ExecutionOutcome(outcome, detail=reviewer_error, error=True)
            try:
                verdict, review_details = self.ports.parse_reviewer_result(review)
                if verdict == 'SPEC_BLOCKED':
                    if not isinstance(review_details, SpecificationDeadlock):
                        raise self.ports.contract.ReviewerProtocolError(
                            'SPEC_BLOCKED 증거 형식이 유효하지 않습니다.'
                        )
                    try:
                        validate_locations(
                            review_details,
                            str(self.ports.snapshot().catalog_plan['path']),
                            tuple(str(path) for path in self.ports.snapshot().plan.get('sources', ())),
                        )
                    except ValueError as error:
                        raise self.ports.contract.ReviewerProtocolError(str(error)) from error
            except self.ports.contract.ReviewerProtocolError as error:
                if feedback_repo is not None:
                    feedback_repo.record_result(
                        self.ports.identity.repo, str(self.ports.snapshot().plan['id']), context.plan.definition_revision_sha,
                        'review', 'protocol_failure', review,
                    )
                protocol_error = str(error)
                reviewer_error = f"Reviewer 결과 프로토콜 오류: {protocol_error}"
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    phase='REVIEWING',
                    last_error=reviewer_error,
                    review_result=review,
                )
                if protocol_attempt == self.ports.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS:
                    exhausted_error = f"{reviewer_error} (프로토콜 교정 {self.ports.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS}회 실패)"
                    self.ports._block_final(
                        self.ports.identity.repo,
                        context.worktree,
                        exhausted_error,
                        str(row['verify_result'] or ''),
                        reason=BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
                    )
                    self.ports.operation_print(exhausted_error, file=sys.stderr)
                    return self.ports._final(exhausted_error)
                self.ports.operation_print(reviewer_error, file=sys.stderr)
                row = self.ports._row()
                continue
            break
        assert verdict is not None
        if draft is not None and (
            verdict != 'PASS'
            or not document_publication.review_confirms_publication(review)
        ):
            return self.ports._document_block(
                context, draft,
                "Reviewer가 의미 보존과 승인 diff 일치를 명시적으로 확정하지 못했습니다. "
                + review[-6000:],
            )
        if verdict == 'SPEC_BLOCKED':
            self.ports.state.update_project(self.ports.identity.repo, review_result=review)
            self.ports.operation_log(
                f"사용자 명세 결정 필요: {self.ports.snapshot().plan['id']} {self.ports.snapshot().plan['title']}",
                error=True,
            )
            return self.ports._block_final(
                self.ports.identity.repo, context.worktree, review,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
                user_targets=review_details.paths,
            )
        if feedback_repo is not None:
            feedback_repo.observe(
                self.ports.identity.repo, str(self.ports.snapshot().plan['id']), context.plan.definition_revision_sha,
                'review', verdict == 'PASS', review,
                normalize_feedback('review', review) if verdict == 'FAIL' else (),
            )
        if verdict == 'FAIL':
            decision = self.ports.transition_validation_failure(
                self.ports.identity.repo, context.worktree, 'review', review,
                review_level=review_level, candidate_head=candidate_head,
            )
            self.ports.operation_log(
                f"Worker 반환: {self.ports.snapshot().plan['id']} {self.ports.snapshot().plan['title']} (Review 실패)",
                error=True,
            )
            self.ports.operation_print(review or 'Reviewer 결과가 유효하지 않습니다.', file=sys.stderr)
            if decision.blocked:
                return ExecutionOutcome(
                    ExecutionOutcomeKind.FINAL_BLOCK, detail=review, error=True,
                    recovery=getattr(decision, 'recovery', None),
                )
            return self.ports._recover(
                RecoveryReason.REVIEW_FEEDBACK_CORRECTION, review,
                context=context, fingerprint=decision.fingerprint,
            )
        self.ports.clear_validation_failure_history(self.ports.identity.repo, 'review')
        self.ports.state.update_project(
            self.ports.identity.repo,
            phase='PUBLISHING',
            last_error=None,
            review_result=review,
            review_token=(
                self.ports.snapshot()._definition_snapshot.review_token(
                    str(row['validated_base_sha'] or row['active_sha'] or ''),
                    self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip(),
                )
                if self.ports.snapshot()._definition_snapshot is not None else None
            ),
            publication_token=None,
        )
        return self.ports._advanced()
