"""Verify execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.runtime import operation_events as events
from loop_manager_internal.execution.execution_runtime import LatestBasePort
from dataclasses import dataclass
from typing import Callable, Any, Mapping
from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents.pr_description import DocumentReportError, common_guidance_report
import sys
import json
import time
from loop_manager_internal.storage import review_correction_state, runtime_state
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure, classify_exception
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind, LatestBaseCheckpoint
from loop_manager_internal.execution.validation_feedback import normalize_feedback, redact_secrets
from loop_manager_internal.execution.worker_outcome import WorkerOutcomeError

from loop_manager_internal.execution.execution_effects import ExecutionContract, FeedbackEffects
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage import verify_receipts
from loop_manager_internal.verification.inputs import InputObservation
from loop_manager_internal.storage import pre_review
from loop_manager_internal.execution import preliminary_review

@dataclass(frozen=True)
class VerifyHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    update_snapshot: Callable[..., None]
    contract: ExecutionContract
    feedback: FeedbackEffects | None
    state: ExecutionStatePort
    _worktree_execplan_input_fingerprint: Callable[..., Any]
    begin_agent_scope: Callable[..., Any]
    branch_has_changes: Callable[..., Any]
    clear_agent_scope: Callable[..., Any]
    clear_validation_failure_history: Callable[..., Any]
    git: Callable[..., Any]
    operation_log: Callable[..., Any]
    operation_print: Callable[..., Any]
    parse_worker_result: Callable[..., Any]
    transition_validation_failure: Callable[..., Any]
    validate_active_execplan_metadata: Callable[..., Any]
    validate_worker_workspace_scope: Callable[..., Any]
    verify: Callable[..., Any]
    _advanced: Callable[[], ExecutionOutcome]
    _block_final: Callable[..., ExecutionOutcome]
    _block_worker_decision: Callable[..., ExecutionOutcome]
    _commit_owned: Callable[..., Any]
    _context: Callable[[], ExecutionContext]
    _document_block: Callable[..., ExecutionOutcome]
    _document_draft: Callable[..., Any]
    _document_guard: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _final: Callable[..., ExecutionOutcome]
    _metadata_correction: Callable[..., ExecutionOutcome]
    _parse_worker_outcome: Callable[..., Any]
    _post_effect_guard: Callable[[ExecutionContext], ExecutionContext | ExecutionOutcome]
    _recover: Callable[..., ExecutionOutcome]
    _retry: Callable[..., ExecutionOutcome]
    _row: Callable[[], Mapping[str, object]]
    observe_verify_inputs: Callable[..., Any] | None = None
    partial_verify_enabled: Callable[..., bool] | None = None
    partial_verify: Callable[..., Any] | None = None
    pre_review_identity: Callable[..., Any] | None = None
    checkpoint: Any = None


class VerifyHandler:
    """Verify policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: VerifyHandlerPorts):
        self.ports = ports

    def _observe(self, worktree):
        observer = self.ports.observe_verify_inputs
        if observer is None:
            return InputObservation(None, reason='verify input observer unavailable')
        try:
            value = observer(worktree)
            return value if isinstance(value, InputObservation) else InputObservation(None, reason='invalid verify observation')
        except (OSError, RuntimeError, ValueError) as error:
            return InputObservation(None, reason=str(error))

    def _verify_once(self, context, candidate_head: str | None = None, *, partial=False):
        self._verify_executed = False
        assert self.ports.snapshot().catalog_plan is not None and self.ports.snapshot().plan is not None
        from loop_manager_internal.execution.manual_candidate import get_manual
        manual = get_manual(self.ports.state)
        manual_snapshot = manual.capture_stage(context, 'verify') if manual and not partial else None
        waiver = manual.waive(context, 'verify', snapshot=manual_snapshot) if manual and not partial else None
        if waiver:
            self.ports.operation_print(waiver)
            return None, waiver
        notice = manual.invalid_waiver_notice(context,'verify',snapshot=manual_snapshot) if manual else None
        if notice:
            self.ports.operation_print(notice)
        self.ports.operation_log(f"{'부분' if partial else '전체'} verify 시작: {self.ports.snapshot().plan['id']} {self.ports.snapshot().plan['title']}")
        verify_scope_before = self.ports.begin_agent_scope(
            self.ports.identity.repo,
            context.worktree,
            'worker',
            input_fingerprint=self.ports._worktree_execplan_input_fingerprint(context.worktree, self.ports.snapshot().catalog_plan),
        )
        try:
            self._verify_executed = not partial
            if partial:
                passed, output = self.ports.partial_verify(context.worktree, context.active_sha)
                self._partial_report = getattr(output, 'partial_result', None)
                report = self._partial_report
                if (not isinstance(report, dict) or report.get('kind') != 'partial_verify'
                        or report.get('status') not in {'PASS', 'FAIL', 'FALLBACK', 'ERROR'}):
                    passed = False
                    self._partial_report = {'kind': 'partial_verify', 'status': 'ERROR'}
                elif report['status'] == 'PASS':
                    selection = report.get('selection') or {}
                    passed = bool(passed and selection.get('head') == candidate_head
                                  and selection.get('base') == context.active_sha
                                  and selection.get('modules') and selection.get('ids') and not selection.get('fallback'))
                    if not passed:
                        self._partial_report = {**report, 'status': 'ERROR'}
                elif report['status'] != 'FALLBACK':
                    passed = False
            else:
                passed, output = self.ports.verify(context.worktree)
        except self.ports.contract.VerifyTerminationUncertain as error:
            detail = str(error)
            self.ports._block_final(
                self.ports.identity.repo, context.worktree, detail, detail,
                reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE,
            )
            self.ports.operation_print(detail, file=sys.stderr)
            return (self.ports._final(detail), detail)
        raw_output = redact_secrets(str(getattr(output, 'fingerprint_source', output)))
        if manual and not partial:
            manual.record(context, 'verify', passed, raw_output, snapshot=manual_snapshot)
        if not hasattr(output, 'fingerprint_source'):
            output = raw_output
        definition = self.ports.snapshot()._definition_snapshot
        if not partial and passed and candidate_head and context.active_sha and definition is not None:
            observed_head = self.ports.git(
                context.worktree, 'rev-parse', 'HEAD',
            ).stdout.strip()
            if (observed_head == candidate_head and not self.ports.git(
                    context.worktree, 'status', '--porcelain',
            ).stdout.strip()):
                self.ports.state.update_project(
                    self.ports.identity.repo,
                    validated_base_sha=context.active_sha,
                    verify_result=output,
                    verify_token=definition.verify_token(
                        context.active_sha, candidate_head, str(output),
                    ),
                    review_result=None,
                    review_token=None,
                    publication_token=None,
                )
        feedback_repo = self.ports.feedback.repository if self.ports.feedback else None
        if feedback_repo is not None:
            fingerprints = () if passed else self.ports.feedback.fingerprints(
                'verify', output, context.worktree,
            )
            feedback_items = () if passed else normalize_feedback(
                'verify', raw_output, fingerprints,
                exit_code=getattr(output, 'exit_code', None),
                timed_out=bool(getattr(output, 'timed_out', False)),
            )
            def audit_rejected():
                feedback_repo.record_result(
                    self.ports.identity.repo, str(self.ports.snapshot().plan['id']), context.plan.definition_revision_sha,
                    'verify', 'success' if passed else 'failure', raw_output,
                    feedback_items,
                )
        try:
            self.ports.update_snapshot(plan=self.ports.validate_worker_workspace_scope(
                self.ports.identity.repo_path,
                context.worktree,
                context.branch,
                self.ports.identity.base,
                context.plan.path,
                self.ports.snapshot().catalog_plan,
                verify_scope_before,
                scope_phase='VERIFYING',
            ))
        except (self.ports.contract.AgentScopeError, self.ports.contract.WorkspaceOwnershipError) as error:
            if feedback_repo is not None:
                audit_rejected()
            scope_error = str(error)
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                scope_error,
                output,
                reason=getattr(error, 'reason_code', BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
            )
            self.ports.operation_print(scope_error, file=sys.stderr)
            return (self.ports._final(scope_error), output)
        except self.ports.contract.ExecPlanMetadataError as error:
            if feedback_repo is not None:
                audit_rejected()
            return (self.ports._metadata_correction(
                context, error, verify_result=output,
            ), output)
        except RuntimeError as error:
            if feedback_repo is not None:
                audit_rejected()
            detail = f"verify workspace 상태 조회 재시도: {error}"
            self.ports.operation_print(detail, file=sys.stderr)
            return (self.ports._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                detail, context=context,
            ), output)
        self.ports.clear_agent_scope(self.ports.identity.repo)
        guarded = self.ports._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            if feedback_repo is not None:
                audit_rejected()
            return (guarded, output)
        context = guarded
        if feedback_repo is not None and not (partial and passed):
            feedback_repo.observe(
                self.ports.identity.repo, str(self.ports.snapshot().plan['id']), context.plan.definition_revision_sha,
                'verify', passed, raw_output, feedback_items,
            )
        if not passed:
            self.ports.operation_log(
                f"Worker 반환: {self.ports.snapshot().plan['id']} {self.ports.snapshot().plan['title']} ({'부분' if partial else '전체'} verify 실패)",
                error=True,
            )
            decision = self.ports.transition_validation_failure(
                self.ports.identity.repo,
                context.worktree,
                'verify',
                output,
            )
            self.ports.operation_print(output, file=sys.stderr)
            if decision.blocked:
                return (ExecutionOutcome(
                    ExecutionOutcomeKind.FINAL_BLOCK, detail=output, error=True,
                    recovery=getattr(decision, 'recovery', None),
                ), output)
            return (self.ports._recover(
                RecoveryReason.WORKER_CORRECTION, output,
                context=context, fingerprint=decision.fingerprint,
                correction_limit=self.ports.contract.MAX_VALIDATION_FINGERPRINT_REPEATS + 1,
            ), output)
        events.emit("verify 결과 확인 완료", scope="partial" if partial else "full",
                    result=(self._partial_report or {}).get("status", "UNKNOWN") if partial else "PASS")
        return (None, output)

    def verify(self, context: ExecutionContext, latest_base: LatestBasePort) -> ExecutionOutcome:
        self.ports.state.check_context(context)
        row = self.ports._row()
        assert self.ports.snapshot().catalog_plan is not None and self.ports.snapshot().plan is not None
        draft = self.ports._document_draft(context)
        if draft is not None:
            try:
                self.ports._document_guard(context, draft)
            except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
                return self.ports._document_block(context, draft, error)
        try:
            from loop_manager_internal.execution.manual_candidate import get_manual
            manual = get_manual(self.ports.state)
            if not (manual and manual.user_submission(row)):
                disposition = self.ports._parse_worker_outcome(
                    context, str(row['worker_result'] or ''), allow_legacy_ready=True,
                )
                if disposition.kind == 'needs_user_decision':
                    return self.ports._block_worker_decision(
                        context, str(row['worker_result'] or ''), disposition,
                    )
            worker_publication = self.ports.parse_worker_result(str(row['worker_result'] or ''), self.ports.snapshot().plan)
        except (WorkerOutcomeError, self.ports.contract.WorkerResultError) as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=validation_error)
            self.ports.operation_print(validation_error, file=sys.stderr)
            return self.ports._recover(
                RecoveryReason.WORKER_CORRECTION, validation_error,
                context=context,
            )
        latest_sha = self.ports._fetch_latest_base()
        dirty = bool(self.ports.git(context.worktree, 'status', '--porcelain').stdout.strip())
        if not dirty and (not self.ports.branch_has_changes(context.worktree, self.ports.identity.base)):
            no_change_error = 'Worker가 변경을 만들지 않아 PR을 생성할 수 없습니다. ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다.'
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
        if dirty:
            self.ports.git(context.worktree, 'add', '-A')
            self.ports.update_snapshot(plan=self.ports.validate_active_execplan_metadata(
                self.ports.identity.repo_path,
                context.worktree,
                context.plan.path,
                self.ports.snapshot().catalog_plan,
            ))
            self.ports._commit_owned(context, worker_publication.commit_title, worker_confirmed=True)
        guarded = self.ports._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
        latest_outcome = latest_base.reconcile(
            context, LatestBaseCheckpoint.BEFORE_VERIFY, latest_sha,
        )
        if latest_outcome is not None:
            return latest_outcome
        context = self.ports._context()
        candidate_head = self.ports.git(
            context.worktree, 'rev-parse', 'HEAD'
        ).stdout.strip()
        try:
            origin_url = self.ports.git(context.worktree, 'remote', 'get-url', 'origin').stdout.strip()
            common_guidance_report(self.ports.git, context.worktree, context.active_sha,
                                   candidate_head, origin_url, worker_publication.summary,
                                   selected_execplan_path=context.plan.path)
        except DocumentReportError as error:
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING',
                                            validated_base_sha=None, last_error=str(error))
            return self.ports._recover(RecoveryReason.WORKER_CORRECTION, str(error), context=context)
        partial_outcome = self._maybe_partial(context, candidate_head)
        if partial_outcome is not None:
            return partial_outcome
        observation = self._observe(context.worktree)
        waived = bool(manual and manual.waive(context, 'verify', record=False))
        connection = self.ports.state.connection
        repo, plan_id = self.ports.identity.repo, context.plan.plan_id
        with runtime_state._transaction(connection):
            self.ports.state.check()
            selection = verify_receipts.selected(connection, repo, plan_id)
            if selection is None:
                # Legacy active executions get a new cycle, never a fabricated
                # success baseline from their old verify token.
                verify_receipts.start_cycle(connection, repo, plan_id)
                selection = verify_receipts.selected(connection, repo, plan_id)
        origin = (verify_receipts.success(connection, selection, observation)
                  if not waived and self.ports.snapshot()._definition_snapshot is not None else None)
        if preliminary_review.active(self.ports, context, {'review_pass', 'review_invalidated'}):
            origin = None  # General preliminary PASS always requires an actual full run.
        if origin is not None:
            with runtime_state._transaction(connection):
                self.ports.state.check_context(context)
                if (self._observe(context.worktree) != observation
                        or self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip() != candidate_head
                        or self.ports.git(context.worktree, 'status', '--porcelain').stdout.strip()):
                    return self.ports._retry('verify 재사용 판정 중 입력이 변경되어 다시 확인합니다.')
                verify_receipts.record(connection, selection, head=candidate_head,
                    observation=observation, kind='reuse', origin=origin,
                    reason='same proven verify inputs in the same execution cycle')
                output = origin['result']
                definition = self.ports.snapshot()._definition_snapshot
                if not (manual and manual.user_submission(self.ports._row())):
                    review_correction_state.verified(connection, repo, plan_id, candidate_head)
                self.ports.state.update_project(repo, preserve_transaction=True,
                    validated_base_sha=context.active_sha, verify_result=output,
                    verify_token=definition.verify_token(context.active_sha, candidate_head, output),
                    review_result=None, review_token=None, publication_token=None)
            self.ports.operation_log(f"전체 verify 재사용: {plan_id}, 원래 run={origin['id']}, 입력={observation.fingerprint}")
            self.ports.update_snapshot(_latest_verify_output=output)
            return self._finish_verified(context)
        # A new invocation invalidates any previous success marker first. If
        # this run fails before replacing its output, stale evidence cannot be
        # paired with the new candidate or a later scope block.
        with runtime_state._transaction(connection):
            self.ports.state.check()
            identifier, selection = verify_receipts.record(connection, selection,
                head=candidate_head, observation=observation, kind='run',
                reason=observation.reason or 'no matching successful verify receipt')
            self.ports.state.update_project(
                repo, preserve_transaction=True, validated_base_sha=None,
                verify_token=None, review_result=None, review_token=None, publication_token=None,
            )
        full_started = time.monotonic()
        outcome, output = self._verify_once(context, candidate_head)
        preliminary = preliminary_review.active(self.ports, context, {'review_pass', 'review_invalidated'})
        if preliminary:
            details = json.loads(preliminary['partial'])
            details['full_verify'] = {'passed': outcome is None, 'duration': time.monotonic() - full_started,
                                      'executed': self._verify_executed, 'receipt_id': identifier}
            with runtime_state._transaction(connection):
                pre_review.update(connection, preliminary, preliminary['status'], partial=details)
        if outcome is not None:
            with runtime_state._transaction(connection):
                verify_receipts.complete(connection, selection, identifier, passed=False, output=output,
                                         executed=self._verify_executed)
                preliminary = preliminary_review.active(self.ports, context, {'review_pass', 'review_invalidated'})
                if preliminary:
                    pre_review.update(connection, preliminary, 'full_failed')
            return outcome
        if self.ports.git(
            context.worktree, 'rev-parse', 'HEAD'
        ).stdout.strip() != candidate_head:
            self.ports.state.update_project(
                self.ports.identity.repo, validated_base_sha=None, verify_result=None,
                review_result=None, verify_token=None, review_token=None,
                publication_token=None,
            )
            return self.ports._retry('전체 verify 중 게시 후보 HEAD가 변경되어 다시 검증합니다.')
        if self.ports.git(
            context.worktree, 'status', '--porcelain'
        ).stdout.strip():
            # Scope validation in _verify_once accepted these paths, but the
            # verified HEAD has not included them. Commit and validate the
            # resulting candidate on the next VERIFYING turn.
            self.ports.update_snapshot(plan=self.ports.validate_active_execplan_metadata(
                self.ports.identity.repo_path, context.worktree, context.plan.path,
                self.ports.snapshot().catalog_plan,
            ))
            self.ports.git(context.worktree, 'add', '-A')
            self.ports._commit_owned(context, worker_publication.commit_title)
            return self.ports._retry('전체 verify 중 허용된 변경을 commit했습니다. 새 후보를 다시 검증합니다.')
        with runtime_state._transaction(self.ports.state.connection):
            self.ports.state.check()
            after = self._observe(context.worktree)
            if self._verify_executed and observation.proven and after != observation:
                verify_receipts.complete(connection, selection, identifier, passed=False,
                                         output='verify inputs changed during execution')
                self.ports.state.update_project(repo, preserve_transaction=True,
                    validated_base_sha=None, verify_token=None, review_token=None, publication_token=None)
                return self.ports._retry('전체 verify 실행 중 입력이 변경되어 다시 검증합니다.')
            verify_receipts.complete(connection, selection, identifier, passed=True, output=output,
                                     executed=self._verify_executed)
            if not (manual and manual.user_submission(self.ports._row())):
                review_correction_state.verified(
                    self.ports.state.connection, self.ports.identity.repo,
                    context.plan.plan_id, candidate_head,
                )
            self.ports.clear_validation_failure_history(self.ports.identity.repo, 'verify')
            self.ports.state.update_project(
                self.ports.identity.repo, preserve_transaction=True,
                validated_base_sha=context.active_sha,
                verify_result=output,
                verify_token=(
                    self.ports.snapshot()._definition_snapshot.verify_token(
                        context.active_sha, candidate_head, str(output),
                    )
                    if self.ports.snapshot()._definition_snapshot is not None else None
                ),
                review_token=None,
                publication_token=None,
            )
        self.ports.update_snapshot(_latest_verify_output=output)
        return self._finish_verified(context)

    def _maybe_partial(self, context, head):
        enabled = self.ports.partial_verify_enabled
        if enabled is None or enabled(context.worktree) is not True or self.ports.partial_verify is None:
            return None
        from loop_manager_internal.execution.manual_candidate import get_manual
        manual = get_manual(self.ports.state)
        if manual and manual.enabled() or self.ports._document_draft(context) is not None:
            return None
        from loop_manager_internal.execution import stage_documents
        if self.ports.checkpoint and stage_documents.pending(self.ports.checkpoint, self.ports.identity.repo, context.plan.plan_id):
            return None  # R-082 document preliminary Review has its own lifecycle.
        key = preliminary_review.candidate(self.ports, context)
        if key is None:
            return None
        connection = self.ports.state.connection
        previous = pre_review.current(connection, self.ports.identity.repo, context.plan.plan_id)
        if previous and previous['candidate'] == key:
            if previous['status'] == 'partial_pass':
                self.ports.state.update_project(self.ports.identity.repo, phase='REVIEWING', last_error=None)
                return self.ports._advanced()
            # Interrupted work, Review PASS/FAIL and prior fallback all proceed
            # through full verify. They cannot replay an uncompleted partial run.
            return None
        with runtime_state._transaction(connection):
            self.ports.state.check_context(context)
            receipt = pre_review.begin(connection, self.ports.identity.repo, context.plan.plan_id, key)
            if preliminary_review.review_identity(self.ports, context) is None:
                from loop_manager_internal.verification import impact
                fallback = ['review_environment_identity_unproven']
                try:
                    declaration = self.ports.git(context.worktree, 'show', f'{head}:scripts/verify.json').stdout
                except (OSError, RuntimeError):
                    # Legacy verification scripts need no adapter declaration.
                    # Missing fallback diagnostics must not prevent their full run.
                    declaration = None
                    fallback.append('verification_declaration_unavailable')
                pre_review.update(connection, receipt, 'fallback', partial={
                    'kind': 'partial_verify', 'status': 'FALLBACK',
                    'selection': {'head': head, 'base': context.active_sha, 'modules': [], 'ids': [], 'reasons': {},
                        'total_modules': None, 'total_tests': None, 'manifest_identity': None,
                        'configuration_identity': impact.digest({'settings': preliminary_review.verify_settings(self.ports),
                            'declaration': declaration}),
                        'algorithm': f'python-ast-impact-v{impact.VERSION}/index-v{impact.code_index.VERSION}',
                        'fallback': fallback}})
                self.ports.operation_log('부분 verify fallback: Review 실행 환경·정책 identity가 확인되지 않았습니다.')
                return None
            self.ports.state.update_project(self.ports.identity.repo, preserve_transaction=True,
                validated_base_sha=None, verify_token=None, review_token=None, publication_token=None)
        self._partial_report = None
        outcome, output = self._verify_once(context, head, partial=True)
        report = self._partial_report or {'kind': 'partial_verify', 'status': 'ERROR'}
        status = report['status']
        if key != preliminary_review.candidate(self.ports, context):
            status = 'ERROR'
        with runtime_state._transaction(connection):
            self.ports.state.check()
            pre_review.update(connection, receipt,
                'partial_pass' if status == 'PASS' and outcome is None else
                'fallback' if status == 'FALLBACK' and outcome is None else 'partial_failed',
                partial={**report, 'output': redact_secrets(str(output))})
            if status == 'PASS' and outcome is None:
                review_correction_state.partially_verified(connection, self.ports.identity.repo, context.plan.plan_id, head)
        if outcome is not None:
            return outcome
        if status == 'FALLBACK':
            self.ports.operation_log('부분 verify fallback: ' + str((report.get('selection') or {}).get('fallback')))
            return None
        if status != 'PASS':
            return self.ports._retry('부분 verify 후보가 변경되어 전체 검증 경로로 재확인합니다.')
        selection = report.get('selection') or {}
        events.emit("부분 verify PASS: 일반 선행 Review로 진행", selected_modules=len(selection.get('modules') or ()),
                    selected_tests=len(selection.get('ids') or ()), next_stage="REVIEWING")
        self.ports.state.update_project(self.ports.identity.repo, phase='REVIEWING', last_error=None)
        return self.ports._advanced()

    def _finish_verified(self, context):
        assert self.ports.snapshot().catalog_plan is not None
        output = self.ports.snapshot()._latest_verify_output
        try:
            self.ports.update_snapshot(plan=self.ports.validate_active_execplan_metadata(
                self.ports.identity.repo_path,
                context.worktree,
                context.plan.path,
                self.ports.snapshot().catalog_plan,
            ))
        except RecoveryFailure:
            raise
        except RuntimeError as error:
            metadata_error = str(error)
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                metadata_error,
                output,
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.ports.operation_print(metadata_error, file=sys.stderr)
            return self.ports._final(metadata_error)
        if not self.ports.branch_has_changes(context.worktree, self.ports.identity.base):
            no_change_error = 'rebase 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다. ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다.'
            self.ports._block_final(
                self.ports.identity.repo,
                context.worktree,
                no_change_error,
                output,
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
                recovery_reason=RecoveryReason.NO_CHANGE,
            )
            self.ports.operation_print(no_change_error, file=sys.stderr)
            return self.ports._final(no_change_error)
        self.ports.state.update_project(self.ports.identity.repo, phase='REVIEWING', last_error=None)
        return self.ports._advanced()
