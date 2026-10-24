"""Review execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.runtime import operation_events as events
from dataclasses import dataclass
from typing import Callable, Any, Mapping
from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents import document_publication
import sys
from loop_manager_internal.storage import review_correction_state, runtime_state, verify_receipts
from loop_manager_internal.notifications import notifications
import json
from loop_manager_internal.agents.agent_invocation import AttemptSequence, TrackedAgentCall
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents.agent_scope_decision import _same_local_snapshot
from loop_manager_internal.git.managed_rebase_adoption import revalidation_values
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, classify_exception
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.execution.validation_feedback import normalize_feedback, redact_secrets
from loop_manager_internal.documents.reviewer_specification import SpecificationDeadlock, validate_locations
from loop_manager_internal.execution import stage_documents
from loop_manager_internal.execution import preliminary_review
from loop_manager_internal.storage import pre_review

from loop_manager_internal.execution.execution_effects import ExecutionContract, FeedbackEffects
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort
import hashlib
import re

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
    observe_verify_inputs: Callable[..., Any] | None = None
    checkpoint: Any = None
    pre_review_identity: Callable[..., Any] | None = None
    git_bytes: Callable[..., bytes] | None = None


class ReviewHandler:
    """Review policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: ReviewHandlerPorts):
        self.ports = ports

    def _pending_document_review(self, context):
        definition = self.ports.snapshot()._definition_snapshot
        if definition is None:
            return False
        candidate = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        return stage_documents.review_pending(self.ports.checkpoint, self.ports.identity.repo,
                                             context.plan.plan_id, definition, candidate)

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

        if self._pending_document_review(context):
            item = stage_documents.pending(self.ports.checkpoint, self.ports.identity.repo, context.plan.plan_id)
            return worker_publication, (
                f"원격 문서 변경 후 선행 Review입니다. 최신 완료 조건은 아직 검증되지 않았습니다. "
                f"과거 후보={item.get('before_head', '기록 없음')}, verify 기준 base={item['verify_base']}. "
                f"이전 선언·입력(새 조건의 검증 증명이 아님): {item.get('previous', '기록 없음')}\n"
                f"과거 출력(최신 조건의 PASS가 아님):\n"
                + redact_secrets(str(item['verify_result'] or '기록 없음')))

        preliminary = preliminary_review.active(self.ports, context, {'partial_pass', 'review_running'})
        if preliminary and self.ports._document_draft(context) is None:
            self._preliminary = preliminary
            return worker_publication, ('Manager 부분 verify PASS; 전체 verify는 아직 실행하지 않았습니다. '
                'PASS는 선택 모듈의 성공만 뜻하며 영향 범위 전체를 증명하지 않습니다. '
                '선정 진단의 남은 불확실성과 미선택 관련 모듈도 확인하세요. '
                '일반 선행 Review로 구현·회귀·요구사항을 검토하세요. PASS는 최종 게시 승인이 아닙니다.\n'
                + preliminary_review.review_summary(preliminary['partial']))

        candidate_head = self.ports.git(
            context.worktree, 'rev-parse', 'HEAD',
        ).stdout.strip()
        expected_verify = self.ports.snapshot()._definition_snapshot.verify_token(
            latest_sha, candidate_head, str(row['verify_result'] or ''),
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
        observation = None
        if self.ports.observe_verify_inputs is not None:
            try:
                observation = self.ports.observe_verify_inputs(context.worktree)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        verify_evidence = verify_receipts.candidate_evidence(
            self.ports.state.connection,
            self.ports.identity.repo,
            context.plan.plan_id,
            candidate_head,
            str(row['verify_result']),
            observation,
        )
        from loop_manager_internal.execution.manual_candidate import get_manual
        manual = get_manual(self.ports.state)
        if verify_evidence and verify_evidence['kind'] == 'waiver':
            waiver = manual.waive(context, 'verify', record=False) if manual else None
            if waiver != row['verify_result']:
                verify_evidence = None
        elif (verify_evidence and manual and manual.user_submission(row)
              and not manual.valid_evidence(context, 'verify', str(row['verify_result']))):
            verify_evidence = None
        if verify_evidence is None:
            detail = 'Reviewer 시작 전 현재 후보에 연결된 Manager verify receipt가 없습니다. 다시 검증합니다.'
            self.ports.state.update_project(
                self.ports.identity.repo, phase='VERIFYING', validated_base_sha=None,
                verify_result=None, review_result=None, verify_token=None,
                review_token=None, publication_token=None, last_error=detail,
            )
            self.ports.operation_print(detail, file=sys.stderr)
            return self.ports._retry(detail)

        return worker_publication, self._format_verify_evidence(
            verify_evidence, candidate_head, latest_sha,
        )

    @staticmethod
    def _format_verify_evidence(evidence, candidate_head, base_sha):
        receipt = evidence['receipt']
        output = '기록된 출력 없음' if not receipt['result'] else '비공개로 저장된 출력 있음'
        if evidence['kind'] == 'run':
            return (f"Manager 확인: 전체 verify PASS (실제 실행 receipt={receipt['id']}); "
                    f"candidate HEAD={candidate_head}; base={base_sha}; {output}.")
        if evidence['kind'] == 'reuse':
            origin = evidence['origin']
            return (f"Manager 확인: 전체 verify PASS (같은 작업 주기의 증명된 입력 재사용; "
                    f"현재 후보 receipt={receipt['id']}; 원래 실제 실행 receipt={origin['id']}; "
                    f"입력 fingerprint={receipt['fingerprint']}); candidate HEAD={candidate_head}; "
                    f"base={base_sha}; 새 verify 실행은 하지 않았고 {output}.")
        return (f"Manager 확인: 유효한 사용자 verify 생략 (SKIPPED_BY_USER; receipt={receipt['id']}); "
                f"candidate HEAD={candidate_head}; base={base_sha}; 전체 verify PASS가 아닙니다. "
                f"{redact_secrets(str(receipt['result']))}.")

    def _pr_review_candidate_proof(self, context, row, candidate_head):
        """Bind publication proof to the reviewed patch, not its rebase-specific OIDs."""
        definition = self.ports.snapshot()._definition_snapshot
        base = str(row['validated_base_sha'] or row['active_sha'] or '')
        if definition is None or not base or not candidate_head:
            return None
        try:
            if self.ports.git_bytes is None:
                raise RuntimeError('raw Git diff bytes are unavailable')
            diff = self.ports.git_bytes(
                context.worktree, 'diff', '--binary', '--unified=1', '--no-ext-diff',
                '--no-textconv', base, candidate_head,
            )
            if not isinstance(diff, bytes):
                raise TypeError('raw Git diff did not return bytes')
            # Rebase changes object IDs, numeric offsets, and Git's guessed
            # section label. Keep adjacent context as the location anchor.
            stable_diff = re.sub(
                rb'^index [0-9a-f]+\.\.[0-9a-f]+', b'index', diff,
                flags=re.MULTILINE,
            )
            stable_diff = re.sub(
                rb'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$',
                b'@@', stable_diff, flags=re.MULTILINE,
            )
            change_identity = hashlib.sha256(stable_diff).hexdigest()
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            events.emit(
                'Review 후보 변경 identity를 확인할 수 없어 PR 알림 후보를 보류',
                level='WARN', project=self.ports.identity.repo,
                plan=context.plan.plan_id, stage='REVIEW',
                error_type=type(error).__name__,
            )
            return None
        return (
            definition.publication_token(base, candidate_head),
            definition.review_candidate_identity(change_identity),
        )

    def _record_pr_review_candidate(self, proof, context):
        if proof is not None:
            notifications.record_pr_review_candidate(
                self.ports.state.connection, self.ports.identity.repo,
                context.plan.plan_id, publication_token=proof[0],
                review_input_identity=proof[1],
            )

    def review(self, context: ExecutionContext) -> ExecutionOutcome:
        self._preliminary = None
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
        worker_publication, verify_evidence = entry_evidence
        preliminary = self._preliminary
        if not preliminary and not self._pending_document_review(context):
            approved = preliminary_review.active(self.ports, context, {'review_pass', 'final_review_pending'})
            if (preliminary_review.reusable(self.ports, context, approved)
                    and preliminary_review.full_evidence(self.ports, context, approved)):
                candidate_head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
                candidate_proof = self._pr_review_candidate_proof(context, row, candidate_head)
                reusable = preliminary_review.reusable(self.ports, context, approved)
                full_evidence = preliminary_review.full_evidence(self.ports, context, approved)
                with runtime_state._transaction(self.ports.state.connection):
                    self.ports.state.check_context(context)
                    # Entry evidence already required current full token + receipt;
                    # repeat input identity at the state transition boundary.
                    if not reusable or not full_evidence:
                        return self.ports._retry('선행 Review 최종 승인 입력이 변경됐습니다.')
                    pre_review.update(self.ports.state.connection, approved, 'finalized_reuse')
                    self.ports.clear_validation_failure_history(self.ports.identity.repo, 'review')
                    self._record_pr_review_candidate(candidate_proof, context)
                    self.ports.state.update_project(self.ports.identity.repo, preserve_transaction=True,
                        phase='PUBLISHING', last_error=None, review_result=approved['review'],
                        review_token=self.ports.snapshot()._definition_snapshot.review_token(
                            str(row['validated_base_sha']), candidate_head),
                        publication_token=None)
                events.emit("선행 Review 결과 재사용: 최종 전체 verify 증거 확인 완료", next_stage="PUBLISHING")
                return self.ports._advanced()
            if approved:
                # Once a fresh final invocation is necessary, its failure or
                # interruption must never be overwritten by the old early PASS.
                with runtime_state._transaction(self.ports.state.connection):
                    self.ports.state.check_context(context)
                    pre_review.update(self.ports.state.connection, approved, 'final_review_running')
        preliminary_identity = preliminary_review.review_identity(self.ports, context) if preliminary else None
        if preliminary and preliminary['status'] == 'partial_pass':
            with runtime_state._transaction(self.ports.state.connection):
                preliminary = pre_review.update(self.ports.state.connection, preliminary, 'review_running')
        document_review = self._pending_document_review(context)
        document_instruction = (
            '\n최신 문서의 선행 Review: 구현·테스트 수정이 필요하면 기존 FAIL과 ISSUE_ID로 보고하라. '
            '구현 부족 없이 Manager 검증 근거만 부족하면 ISSUE_ID 없이 VERDICT: VERIFY_REQUIRED로 보고하라. '
            '과거 verify를 최신 조건의 성공으로 취급하지 마라.\n'
            if document_review else '')
        candidate_head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        correction = review_correction_state.load(
            self.ports.state.connection, self.ports.identity.repo, context.plan.plan_id,
        )
        candidate = correction.get('candidate')
        if candidate and candidate['completed'] and (candidate['verified_head'] == candidate_head
                or preliminary and candidate.get('partial_head') == candidate_head):
            review_level = candidate['level']
        elif 'level' in correction:
            pending = correction.get('pending')
            # Preserve the conservative interpretation of older stored candidates.
            review_level = (pending['level'] if pending and pending['completed']
                            and (pending['verified_head'] == candidate_head
                                 or preliminary and pending.get('partial_head') == candidate_head)
                            else correction['level'])
        from loop_manager_internal.execution.manual_candidate import get_manual
        manual = get_manual(self.ports.state)
        manual_snapshot = manual.capture_stage(context, 'review') if manual else None
        waiver = manual.waive(context, 'review', snapshot=manual_snapshot) if manual else None
        if waiver:
            self.ports.operation_print(waiver)
            candidate_head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
            candidate_proof = self._pr_review_candidate_proof(context, row, candidate_head)
            with runtime_state._transaction(self.ports.state.connection):
                self._record_pr_review_candidate(candidate_proof, context)
                self.ports.state.update_project(
                    self.ports.identity.repo, preserve_transaction=True,
                    phase='PUBLISHING', last_error=None, review_result=waiver,
                    review_token=self.ports.snapshot()._definition_snapshot.review_token(
                        str(row['validated_base_sha'] or row['active_sha']), candidate_head),
                    publication_token=None,
                )
            return self.ports._advanced()
        notice = manual.invalid_waiver_notice(context,'review',snapshot=manual_snapshot) if manual else None
        if notice:
            self.ports.operation_print(notice)
        from loop_manager_internal.execution.result_validation import reviewer_targeted_test_evidence
        targeted_test_evidence = (
            '수동 제출 후보: Worker targeted unit test는 실행되지 않았고 외부 targeted test 결과도 Manager가 확인하지 않았습니다.'
            if manual and manual.user_submission(row)
            else 'Worker 보고:\n' + reviewer_targeted_test_evidence(worker_publication.test_evidence)
        )
        protocol_error: str | None = None
        verdict: str | None = None
        review = ''
        for protocol_attempt in range(1, self.ports.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS + 1):
            notifications.flush_project_events(
                self.ports.state.connection, self.ports.identity.repo,
            )
            if protocol_attempt == 1:
                self.ports.operation_log(f"Reviewer 준비: {self.ports.snapshot().plan['id']} {self.ports.snapshot().plan['title']} ({'문서 선행' if document_review else '일반 선행' if preliminary else '최종'} Review)")
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
                        (self.ports.reviewer_prompt(
                            self.ports.identity.base,
                            targeted_test_evidence,
                            row['review_result'],
                            verify_evidence=verify_evidence,
                            session_recovery=session_recovery,
                            protocol_error=protocol_error,
                        ) or '') + document_instruction,
                        draft,
                    ),
                    read_only=True,
                    persist_session=True,
                    session_id=current_session_id,
                    resolved_profile=(manual_snapshot.resolved_profile if manual_snapshot else None),
                    on_session_started=lambda started_session_id: self.ports.state.update_project(self.ports.identity.repo, reviewer_session_id=started_session_id),
                    invocation=invocation,
                ), AttemptSequence(self.ports.identity.repo, str(self.ports.snapshot().plan['id']),
                                   str(self.ports.snapshot().catalog_plan['current_revision_sha']) if self.ports.snapshot().catalog_plan['current_revision_sha'] is not None else None,
                                   purpose="preliminary_review" if preliminary else "review")),
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
            if (isinstance(reviewer_scope_before, GitSnapshot)
                    and isinstance(reviewer_scope_after, GitSnapshot)
                    and not _same_local_snapshot(reviewer_scope_before, reviewer_scope_after)):
                if feedback_repo is not None and not code:
                    feedback_repo.record_result(
                        self.ports.identity.repo, str(self.ports.snapshot().plan['id']),
                        context.plan.definition_revision_sha, 'review', 'stale_result', review)
                self.ports.state.update_project(
                    self.ports.identity.repo, phase='VERIFYING', validated_base_sha=None,
                    verify_token=None, review_token=None, publication_token=None,
                    review_result=None)
                if self.ports.checkpoint is not None:
                    checkpoints = self.ports.checkpoint
                    current = checkpoints.get(self.ports.identity.repo, context.plan.plan_id)
                    checkpoints.record(self.ports.identity.repo, context.plan.plan_id,
                        expected_generation=current.generation, operation=current.operation,
                        expected_head=reviewer_scope_after.head, target_base_oid=current.target_base_oid,
                        observed_remote_oid=current.observed_remote_oid,
                        snapshot={**(current.snapshot or {}), 'stale_review_candidate': reviewer_scope_before.serialize()})
                return self.ports._retry('Review 중 후보가 변경되어 현재 작업물을 보존하고 다시 검증합니다.')
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
                with runtime_state._transaction(self.ports.state.connection):
                    self.ports.state.update_project(
                        self.ports.identity.repo, preserve_transaction=True,
                        phase='REVIEWING', last_error=reviewer_error, review_result=review,
                    )
                    notifications.enqueue_review_failure(
                        self.ports.state.connection, self.ports.identity.repo,
                        context.plan.plan_id, reviewer_error,
                        status=('사용량 제한 해제 후 다음 실행에서 재개'
                                if outcome is ExecutionOutcomeKind.USAGE_PAUSE
                                else '다음 실행에서 재개'),
                    )
                notifications.flush_project_events(
                    self.ports.state.connection, self.ports.identity.repo,
                )
                self.ports.operation_print(reviewer_error, file=sys.stderr)
                return ExecutionOutcome(outcome, detail=reviewer_error, error=True)
            try:
                verdict, review_details = self.ports.parse_reviewer_result(review)
                if verdict == 'VERIFY_REQUIRED' and not document_review:
                    raise self.ports.contract.ReviewerProtocolError('VERIFY_REQUIRED는 문서 변경 후 선행 Review에만 허용됩니다.')
                if manual:
                    manual.review_results(context, verdict, review, snapshot=manual_snapshot)
                if verdict == 'SPEC_BLOCKED':
                    if not isinstance(review_details, SpecificationDeadlock):
                        raise self.ports.contract.ReviewerProtocolError(
                            'SPEC_BLOCKED 증거 형식이 유효하지 않습니다.'
                        )
                    try:
                        validate_locations(
                            review_details,
                            str(self.ports.snapshot().catalog_plan['path']),
                            (tuple(path for item in json.loads(manual.group()['documents'])
                                   for path in (item['path'],*item['plan']['sources']))
                             if manual and manual.enabled() else
                             tuple(str(path) for path in self.ports.snapshot().plan.get('sources', ()))),
                        )
                    except ValueError as error:
                        raise self.ports.contract.ReviewerProtocolError(str(error)) from error
            except self.ports.contract.ReviewerProtocolError as error:
                if manual:
                    manual.record(context,'review',False,f'Reviewer protocol failure: {error}\n{review}',
                                  snapshot=manual_snapshot)
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
        if draft is not None and not document_publication.review_confirms_publication(review):
            return self.ports._document_block(
                context, draft,
                "Reviewer가 의미 보존과 승인 diff 일치를 명시적으로 확정하지 못했습니다. "
                + review[-6000:],
            )
        if verdict == 'SPEC_BLOCKED':
            if manual:
                manual.record(context,'review',False,review,snapshot=manual_snapshot)
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
        if document_review:
            item = stage_documents.pending(self.ports.checkpoint, self.ports.identity.repo, context.plan.plan_id)
            if verdict in {'PASS', 'VERIFY_REQUIRED'}:
                with runtime_state._transaction(self.ports.state.connection):
                    stage_documents.save(self.ports.checkpoint, self.ports.identity.repo, context.plan.plan_id,
                                         {**item, 'verification_pending': False, 'preliminary_review': review})
                    self.ports.state.update_project(self.ports.identity.repo, preserve_transaction=True, phase='VERIFYING',
                        validated_base_sha=None, verify_token=None, review_token=None, publication_token=None,
                        review_result=review, last_error=None)
                return self.ports._advanced()
            # FAIL 교정은 기존 전이 경로가 수행하며, 후보가 바뀌면 관측 marker도 만료된다.
        if preliminary:
            with runtime_state._transaction(self.ports.state.connection):
                same_candidate = preliminary['candidate'] == preliminary_review.candidate(self.ports, context)
                invocation_id = getattr(review_result, 'isolation_invocation_id', None)
                if not isinstance(invocation_id, str) or not invocation_id:
                    invocation_id = None
                actual_identity = (preliminary_review.review_identity(self.ports, context, invocation_id=invocation_id)
                                   if invocation_id else None)
                same_identity = (preliminary_identity is not None and preliminary_identity == actual_identity
                                 == preliminary_review.review_identity(self.ports, context))
                pre_review.update(self.ports.state.connection, preliminary,
                    'review_pass' if verdict == 'PASS' and same_candidate and same_identity else
                    'review_invalidated' if verdict == 'PASS' else 'review_fail',
                    review=review, identity={**actual_identity, 'invocation_id': invocation_id} if actual_identity else None)
                if verdict == 'PASS':
                    events.emit("선행 Review PASS: 최종 전체 verify로 진행", next_stage="VERIFYING")
                    self.ports.state.update_project(self.ports.identity.repo, preserve_transaction=True,
                        phase='VERIFYING', validated_base_sha=None, verify_token=None,
                        review_token=None, publication_token=None, last_error=None)
                    return self.ports._advanced()
        review_source_id = None
        if feedback_repo is not None:
            review_source_id = feedback_repo.observe(
                self.ports.identity.repo, str(self.ports.snapshot().plan['id']), context.plan.definition_revision_sha,
                'review', verdict == 'PASS', review,
                normalize_feedback('review', review) if verdict == 'FAIL' else (),
            )
        if verdict == 'FAIL':
            final_preliminary = pre_review.current(self.ports.state.connection, self.ports.identity.repo, context.plan.plan_id)
            if not preliminary and final_preliminary and final_preliminary['status'] == 'final_review_running':
                pre_review.update(self.ports.state.connection, final_preliminary, 'final_review_failed')
            decision = self.ports.transition_validation_failure(
                self.ports.identity.repo, context.worktree, 'review', review,
                review_level=review_level, candidate_head=candidate_head,
                review_source_id=review_source_id,
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
        events.emit("최종 Review PASS: 게시 단계로 진행", next_stage="PUBLISHING")
        self.ports.clear_validation_failure_history(self.ports.identity.repo, 'review')
        prior = pre_review.current(self.ports.state.connection, self.ports.identity.repo, context.plan.plan_id)
        if prior and prior['status'] in {'review_pass', 'final_review_running'}:
            pre_review.update(self.ports.state.connection, prior, 'finalized_review')
        candidate_head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        candidate_proof = self._pr_review_candidate_proof(context, row, candidate_head)
        with runtime_state._transaction(self.ports.state.connection):
            self._record_pr_review_candidate(candidate_proof, context)
            self.ports.state.update_project(
                self.ports.identity.repo,
                preserve_transaction=True,
                phase='PUBLISHING',
                last_error=None,
                review_result=review,
                review_token=(
                    self.ports.snapshot()._definition_snapshot.review_token(
                        str(row['validated_base_sha'] or row['active_sha'] or ''),
                        candidate_head,
                    )
                    if self.ports.snapshot()._definition_snapshot is not None else None
                ),
                publication_token=None,
            )
        return self.ports._advanced()
