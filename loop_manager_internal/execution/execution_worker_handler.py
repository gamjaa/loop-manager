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
from loop_manager_internal.documents import document_drafts, document_publication
from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
from loop_manager_internal.execution.result_validation import TargetedTestFailure
from loop_manager_internal.verification.targeted import RUNNER_INSTRUCTION
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
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot, classify_definition_delta
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.git.rebase_correction import CorrectionAction, next_action
from loop_manager_internal.storage.rebase_correction_state import CorrectionRace, RebaseCorrectionRepository
from loop_manager_internal.execution.validation_feedback import is_validation_error, render_delta
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.execution.worker_outcome import WorkerOutcome, WorkerOutcomeError, parse_worker_outcome
from loop_manager_internal.execution.common_defect_report import has_report, result_observation

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
    capture_git_snapshot: Callable[..., Any]
    validate_managed_worktree: Callable[..., Any]
    validate_execplan_workspace_identity: Callable[..., Any]
    _commit_owned: Callable[..., Any]
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
    _document_worker_prompt: Callable[..., str]
    _apply_linked_document: Callable[..., bool]
    _execute_rebase_step: Callable[..., Any]
    _fetch_latest_base: Callable[[], str]
    _final: Callable[..., ExecutionOutcome]
    _finish_worker: Callable[..., ExecutionOutcome]
    _metadata_correction: Callable[..., ExecutionOutcome]
    _post_effect_guard: Callable[[ExecutionContext], ExecutionContext | ExecutionOutcome]
    guard: Callable[[ExecutionContext], ExecutionContext | ExecutionOutcome]
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
    publish_worker_checkpoints: Callable[..., Any] | None = None
    _publish_rebased_checkpoint: Callable[..., Any] | None = None
    git_bytes: Callable[..., bytes] | None = None


class WorkerHandler:
    """Worker policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: WorkerHandlerPorts):
        self.ports = ports

    def _targeted_test_block(self, context):
        repository = TargetedTestRepository(self.ports.state.connection)
        repo, plan_id = self.ports.identity.repo, context.plan.plan_id
        repository.reconcile(state_dir=self.ports.identity.state_dir, repo=repo, plan_id=plan_id)
        self.ports.state.check_context(context)
        repeated = repository.repeated(repo, plan_id)
        if not repeated:
            return None
        detail = '동일 targeted unit test가 연속 5회 실패했습니다: ' + ', '.join(repeated)
        with runtime_state._transaction(self.ports.state.connection):
            self.ports.state.check_context(context)
            outcome = self.ports._recover(RecoveryReason.TARGETED_TEST_EXHAUSTED,
                                          detail, context=context)
            repository.acknowledge(repo, plan_id)
        return outcome

    def _targeted_collection_pause(self, context, detail, *, worker_result=None):
        self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=detail)
        if context.resolving_rebase:
            return self.ports._rebase_conflict(
                context, detail, corrected=True, worker_result=worker_result,
                recovery_reason=RecoveryReason.TARGETED_COLLECTION_PENDING,
                targeted_evidence_missing=True,
            )
        return self.ports._recover(RecoveryReason.TARGETED_COLLECTION_PENDING,
                                  detail, context=context, attempts=1)

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

    def _common_defect_pending(self, context, message):
        from loop_manager_internal.execution.common_defect_report import parse_report
        report = parse_report(message)
        assert report is not None
        return self.ports._recover(RecoveryReason.COMMON_DEFECT,
            '범위 밖 공통 교정 반영 대기: ' + str(report['problem']), context=context,
            final_block_reason=BlockReasonCode.COMMON_DEFECT_PENDING, worker_result=message)

    def run(self, context: ExecutionContext) -> ExecutionOutcome:
        self.ports.state.check_context(context)
        if self.ports.checkpoint is not None and not context.resolving_rebase:
            from loop_manager_internal.execution import worker_checkpoint
            try:
                resumed = worker_checkpoint.resume_request(self.ports, context)
                if resumed is not None:
                    return resumed
            except (RuntimeError, OSError) as error:
                return self.ports._recover(classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                                           str(error), context=context)
        if self.ports.publish_worker_checkpoints is not None and not context.resolving_rebase:
            try:
                checkpoint_result = self.ports.publish_worker_checkpoints(
                    context,
                    allowed_requirement_archives=self.ports.snapshot().archive_scope_sources,
                )
                if isinstance(checkpoint_result, ExecutionOutcome):
                    return checkpoint_result
                if checkpoint_result is not None and checkpoint_result.get('candidate_changed'):
                    return self.ports._retry(
                        '원격 작업 branch 통합으로 후보가 변경되어 기존 Worker 결과를 폐기했습니다.'
                    )
            except RecoveryFailure as error:
                return self.ports._recover(error.reason, str(error), context=context)
            except (RuntimeError, OSError) as error:
                return self.ports._recover(
                    classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                    str(error), context=context,
                )
        blocked = self._targeted_test_block(context)
        if blocked is not None:
            return blocked
        targeted_tests = TargetedTestRepository(self.ports.state.connection)
        if targeted_tests.pending(self.ports.identity.repo, context.plan.plan_id):
            return self._targeted_collection_pause(context, 'targeted test 실행 근거의 종료 확인·재수집 대기')
        targeted_sequence = targeted_tests.sequence(self.ports.identity.repo, context.plan.plan_id)
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
        legacy_document_plan = False
        if draft is not None:
            plan_text = document_publication.read_plan_text(
                context.worktree / context.plan.path,
            )
            legacy_document_plan = (
                document_publication.plan_claim(plan_text) == draft.draft_id
            )
        if legacy_document_plan:
            return self.ports._run_document(context, draft)
        self.ports.operation_log(f"Worker 준비: {plan['id']} {plan['title']}")
        correction_turn = None
        if context.resolving_rebase:
            try:
                observation = self.ports._rebase_observation(context)
                current = self.ports.rebase_corrections.get(self.ports.identity.repo, context.plan.plan_id)
                if current is not None and current.worker_result is not None:
                    return self._resume_saved_rebase_worker(
                        context, current, observation,
                    )
                from loop_manager_internal.git.rebase_correction import conflict_wait_fingerprint
                paused = recovery_state.RecoveryRepository(self.ports.state.connection).row(
                    self.ports.identity.repo, context.plan.plan_id)
                if (paused is not None and paused['reason_code'] == RecoveryReason.REBASE_CONFLICT.value
                        and paused['pending_action'] == 'pause' and current is not None
                        and not current.pending_turn
                        and paused['fingerprint'] == conflict_wait_fingerprint(observation, row,
                            checkpoint=self.ports.checkpoint, service=self.ports.current_rebase_service,
                            git_bytes=self.ports.git_bytes)
                        and paused['progress_marker'] == observation.progress):
                    return ExecutionOutcome(ExecutionOutcomeKind.TRANSIENT_PAUSE,
                        detail=str(paused['detail']))
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
        turn_definition = self.ports.snapshot()._definition_snapshot
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
                    from loop_manager_internal.runtime.operation_events import emit
                    emit("Review 교정 묶음 한도 도달: 재시도 대기", used=used, limit=recovery_state.MAX_REVIEW_CORRECTIONS_PER_RUN)
                    detail = (
                        f"Review 교정 실행 한도 도달: run={review_turn_run_id}, "
                        f"plan={plan['id']}, used={used}/"
                        f"{recovery_state.MAX_REVIEW_CORRECTIONS_PER_RUN}. "
                        "재시도 대기 뒤 저장된 Review 피드백으로 재개합니다.\n"
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
        invocation_purpose = (
            "conflict_resolution" if context.resolving_rebase else
            "review_correction" if review_result_id is not None else
            "validation_correction" if is_validation_error(
                row['last_error'], row['verify_result'], row['review_result'],
            ) else "worker_implementation"
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
            targeted_tests.reconcile(state_dir=self.ports.identity.state_dir,
                                     repo=self.ports.identity.repo, plan_id=context.plan.plan_id)
            if targeted_tests.repeated(self.ports.identity.repo, context.plan.plan_id):
                return AgentResult(1, '동일 targeted test 연속 5회 실패: 추가 CLI 재시도를 중단합니다.',
                                   current_session_id, AgentResultKind.PERMANENT_CONFIGURATION)
            if targeted_tests.pending(self.ports.identity.repo, context.plan.plan_id):
                return AgentResult(1, 'targeted test 실행 근거의 종료 확인·재수집 대기',
                                   current_session_id, AgentResultKind.PERMANENT_CONFIGURATION,
                                   targeted_collection_pending=True)
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
            prompt_error = row['last_error']
            if is_validation_error(prompt_error, row['verify_result'], row['review_result']):
                prompt_error = None
            definition = self.ports.snapshot()._definition_snapshot
            checkpoint = self.ports.checkpoint
            saved = (checkpoint.get(self.ports.identity.repo, str(plan['id'])).snapshot or {}) if checkpoint else {}
            accepted = saved.get('current_rebase_acceptance')
            context_values = {
                'task': json.dumps({'path': row['plan_path'], 'worktree': str(context.worktree),
                    'conditions': replace(definition, archive_candidates=(), worker_guidance_hash='').worker_token
                        if definition is not None else None,
                    'allow_common_plans': allow_common_plans}, sort_keys=True),
                'archives': json.dumps(self.ports.snapshot().archive_candidates),
                'error': str(prompt_error or ''),
                'start_head': str(getattr(worker_scope_before, 'head', '')),
                'checkpoint': json.dumps(saved.get('worker_checkpoint_result'), ensure_ascii=False),
                'frontmatter': json.dumps([{key: item.get(key) for key in ('path', 'revision', 'body_sha256')}
                    for item in saved.get('worker_frontmatter_restores', [])[-1:]], ensure_ascii=False),
                'rebase': json.dumps(accepted, ensure_ascii=False, sort_keys=True),
            }
            cause_assessment = saved.get('common_defect_assessment') or {}
            if cause_assessment.get('resolved'):
                context_values['cause_assessment'] = json.dumps(cause_assessment, ensure_ascii=False, sort_keys=True)
            from loop_manager_internal.execution.manual_candidate import get_manual
            manual = get_manual(self.ports.state)
            if manual and manual.enabled():
                context_values.update(manual.worker_context())
            if feedback_repo is not None:
                delivery = feedback_repo.prepare(
                    self.ports.identity.repo, str(plan['id']), current_session_id,
                    recovery=recovering, context=context_values,
                )
                recovering = delivery.recovery
                validation_feedback = render_delta(delivery.rows)
                changed = delivery.context
                initial = delivery.initial
            else:
                validation_feedback = ''
                changed = context_values
                initial = not current_session_id or recovering
            extras = []
            if 'cause_assessment' in changed:
                extras.append('Manager의 현재 원인 재평가(검증 성공 아님): ' + context_values['cause_assessment']
                              + '\n과거 실패는 감사로 보존한다. 현재 코드·후보에서 다시 확인하고 과거 로그만으로 같은 결함을 새 실패로 보고하지 마라.')
            if 'task' in changed:
                extras.append('작업 공간: ' + str(context.worktree))
            if 'archives' in changed and not self.ports.snapshot().archive_candidates:
                extras.append('현재 archive 후보: 없음.')
            if 'start_head' in changed:
                extras.append('checkpoint 요청의 이번 호출 시작 HEAD: ' + context_values['start_head'])
            if 'checkpoint' in changed and saved.get('worker_checkpoint_result'):
                result_info = saved['worker_checkpoint_result']
                extras.append('Manager가 확인한 복구점: ' + result_info['head'] + ' ' + result_info['title']
                              + '. 미완료 작업을 이어가라.')
            if ('frontmatter' in changed and saved.get('worker_frontmatter_restores')
                    and 'frontmatter' not in str(prompt_error or '')):
                extras.append('Manager가 선택된 ExecPlan의 frontmatter만 공식 선언으로 복원했다. '
                              '보존된 본문·현재 diff로 문서 보고를 갱신하라.')
            extras.extend(value for key, value in changed.items() if key.startswith('manual_'))
            if context.resolving_rebase and definition is not None and ('task' in changed or initial):
                extras.append('rebase 충돌 복구의 최신 구현 조건: '
                    + json.dumps({'revision': definition.revision_sha,
                                  'path': definition.path, 'sources': definition.sources}, ensure_ascii=False)
                    + '\n이 revision의 ExecPlan·연결 명세를 git show로 확인하고 현재 작업 형상과 '
                    '대조해 충돌을 해결하라. 기존 변경을 보존하며 rebase의 계속 진행·완료는 Manager에 맡겨라.')
            if accepted and not context.resolving_rebase and 'rebase' in changed:
                extras.append('관리 rebase 결과를 현재 상태로 수용했다. 현재 ExecPlan과 근거 입력을 다시 읽고 '
                    '현재 HEAD의 base 대비 최종 diff 전체를 확인하라. 이전 후보의 성공을 재사용하지 말고 '
                    '관련 최소 범위 targeted unit test로 현재 후보를 확인하라.')
            prompt = self.ports.worker_prompt(
                row['plan_path'], prompt_error if 'error' in changed else None,
                None, validation_feedback,
                archive_candidates=(self.ports.snapshot().archive_candidates if 'archives' in changed else ()),
                session_recovery=recovering, allow_common_plans=allow_common_plans,
                session_resume=not initial and not recovering,
                task_changed='task' in changed, context_delta='\n'.join(extras))
            if draft is not None and not legacy_document_plan:
                prompt = self.ports._document_worker_prompt(prompt, draft)
            if initial or recovering:
                prompt = (prompt or '') + query_context
            try:
                recovery_state.start_review_correction(
                    self.ports.state.connection, self.ports.identity.repo, str(plan['id']),
                    review_turn_run_id, review_result_id,
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
                purpose=invocation_purpose,
            )),
            session_id=active_session_id,
        )
        code, message = result
        if feedback_repo is not None and has_report(message):
            observation = result_observation(context,
                invocation_id=result.isolation_invocation_id, session_id=result.session_id)
            try:
                observed = self.ports.capture_git_snapshot(context.worktree)
            except (RuntimeError, OSError) as error:
                observation['candidate_observation_error'] = redact_secrets(str(error))
            else:
                observation.update(result_observation(context, candidate_head=observed.head,
                    candidate_snapshot=observed.serialize()))
            feedback_repo.record_result(
                self.ports.identity.repo, context.plan.plan_id, context.plan.definition_revision_sha,
                'worker', 'returned' if code == 0 else 'failure', message,
                observation=observation,
            )
        if feedback_repo is not None and delivery is not None and result.session_id:
            feedback_repo.bind_session(delivery.attempt_id, result.session_id)
        if result.session_id and observed_session_id != result.session_id:
            session_started(result.session_id)
        if correction_turn is not None:
            if turn_definition is not None:
                delivered_guidance = common_guidance.worker_content_hash(
                    self.ports.state.connection, self.ports.identity.repo, context.plan.plan_id)
                if delivered_guidance:
                    # Delivery is recorded by the invocation, after its input capture.
                    # Enrich only that evidence; never replace the invocation's conditions.
                    turn_definition = replace(turn_definition, worker_guidance_hash=delivered_guidance)
            try:
                marker = self.ports._rebase_observation(context).progress
                self.ports.rebase_corrections.finish_turn(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=correction_turn.generation,
                    turn=correction_turn.pending_turn,
                    session=result.session_id or self.ports._row()['worker_session_id'],
                    result=message, scope_marker=marker, code=code,
                    result_kind=result.result_kind.value,
                    input_snapshot=turn_definition.serialize() if turn_definition else None,
                )
            except CorrectionRace:
                return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                     'rebase Worker result changed', context=context)
            except (RuntimeError, OSError, ValueError) as error:
                return self.ports._recover(
                    classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                    str(error), context=context,
                )
        restored_frontmatter = []
        try:
            self.ports.update_snapshot(plan=self.ports.validate_worker_workspace_scope(
                self.ports.identity.repo_path,
                context.worktree,
                context.branch,
                self.ports.identity.base,
                str(row['plan_path']),
                catalog_plan,
                worker_scope_before,
                scope_phase='IMPLEMENTING',
                on_frontmatter_restored=restored_frontmatter.append if not code else None,
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
        guarded = self.ports._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            self.ports.clear_agent_scope(self.ports.identity.repo)
            return guarded
        context = guarded
        row = self.ports._row()
        blocked = self._targeted_test_block(context)
        if blocked is not None:
            self.ports.clear_agent_scope(self.ports.identity.repo)
            return blocked
        targeted_evidence_missing = targeted_tests.sequence(
            self.ports.identity.repo, context.plan.plan_id) == targeted_sequence
        if code and result.targeted_collection_pending:
            return self._targeted_collection_pause(context, 'targeted test 실행 근거의 종료 확인·재수집 대기',
                                                  worker_result=message)
        catalog_plan = self.ports.snapshot().catalog_plan
        assert catalog_plan is not None
        if restored_frontmatter:
            detail = ('Manager가 선택된 ExecPlan의 frontmatter만 공식 선언으로 복원했습니다: '
                      + ', '.join(restored_frontmatter)
                      + '. 구현·검증 본문은 보존했습니다. frontmatter를 다시 수정하지 말고 최종 diff·문서 보고를 갱신하세요.')
            self.ports.operation_log(detail)
            self.ports.operation_print(detail)
            if not code:
                self.ports.clear_agent_scope(self.ports.identity.repo)
                self.ports.state.update_project(self.ports.identity.repo, phase='WORKING',
                                                last_error=detail, worker_result=message,
                                                validated_base_sha=None, worker_input_token=None,
                                                verify_result=None, review_result=None,
                                                verify_token=None, review_token=None, publication_token=None)
                return self.ports._retry(detail)
            message = detail + '\n' + message
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
            self._persist_return_failure(context, worker_error, outcome)
            self.ports.operation_print(worker_error, file=sys.stderr)
            return ExecutionOutcome(outcome, detail=worker_error, error=True)
        try:
            disposition = self._parse_worker_outcome(context, message)
            if disposition.kind == 'common_defect_pending':
                return self._common_defect_pending(context, message)
            if disposition.kind == 'needs_user_decision':
                return self._block_worker_decision(context, message, disposition)
            if disposition.kind == 'checkpoint':
                if result.targeted_collection_pending or targeted_tests.pending(self.ports.identity.repo, context.plan.plan_id):
                    return self._targeted_collection_pause(context, 'targeted test 실행 근거의 종료 확인·재수집 대기')
                from loop_manager_internal.execution import worker_checkpoint
                try:
                    return worker_checkpoint.save_request(self.ports, context, disposition, message,
                        worker_scope_before, delivery=delivery,
                        session=result.session_id or self.ports._row()['worker_session_id'],
                        invocation_id=result.isolation_invocation_id)
                except (RuntimeError, OSError) as error:
                    return self.ports._recover(
                        classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                        str(error), context=context)
            if context.resolving_rebase:
                checked = self._guard_rebase_worker_result(
                    context, turn_definition.serialize() if turn_definition else None, refresh=False,
                )
                if isinstance(checked, ExecutionOutcome):
                    return checked
                context = checked
            self.ports.parse_worker_result(message, self.ports.snapshot().plan)
        except (WorkerOutcomeError, self.ports.contract.WorkerResultError) as error:
            if isinstance(error, TargetedTestFailure) and targeted_tests.pending(
                    self.ports.identity.repo, context.plan.plan_id):
                return self._targeted_collection_pause(context, 'targeted test 실행 근거의 종료 확인·재수집 대기',
                                                      worker_result=message)
            self.ports.clear_agent_scope(self.ports.identity.repo)
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            if isinstance(error, TargetedTestFailure) and targeted_evidence_missing:
                validation_error += '\n새 테스트 실행 근거가 없습니다. ' + RUNNER_INSTRUCTION
            if context.resolving_rebase:
                validation_error = self.ports.contract.REBASE_CONFLICT_PREFIX + validation_error
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=validation_error)
            self.ports.operation_print(validation_error, file=sys.stderr)
            if isinstance(error, TargetedTestFailure):
                if context.resolving_rebase:
                    return self.ports._rebase_conflict(
                        context, validation_error, corrected=True, worker_result=message,
                        recovery_reason=RecoveryReason.TARGETED_TEST_CORRECTION,
                        targeted_evidence_missing=targeted_evidence_missing,
                    )
                return self.ports._recover(
                    RecoveryReason.TARGETED_TEST_CORRECTION, validation_error,
                    context=context, attempts=int(targeted_evidence_missing),
                )
            if context.resolving_rebase:
                return self.ports._rebase_conflict(
                    context, validation_error, corrected=True, worker_result=message,
                    recovery_reason=RecoveryReason.WORKER_CORRECTION,
                )
            return self.ports._recover(
                RecoveryReason.WORKER_CORRECTION, validation_error,
                context=context,
            )
        if self.ports.publish_worker_checkpoints is not None and not context.resolving_rebase:
            try:
                self.ports._fetch_latest_base()
                checkpoint_result = self.ports.publish_worker_checkpoints(
                    context,
                    allowed_requirement_archives=self.ports.snapshot().archive_scope_sources,
                    worker_start_head=getattr(worker_scope_before, 'head', None),
                )
                if isinstance(checkpoint_result, ExecutionOutcome):
                    self.ports.clear_agent_scope(self.ports.identity.repo)
                    return checkpoint_result
                if checkpoint_result is not None and checkpoint_result.get('candidate_changed'):
                    self.ports.clear_agent_scope(self.ports.identity.repo)
                    return self.ports._retry(
                        'Worker 실행 중 원격 작업 branch가 통합되어 현재 Worker 결과를 폐기했습니다.'
                    )
            except RecoveryFailure as error:
                return self.ports._recover(error.reason, str(error), context=context)
            except (RuntimeError, OSError) as error:
                return self.ports._recover(
                    classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                    str(error), context=context,
                )
        if result.targeted_collection_pending or targeted_tests.pending(self.ports.identity.repo, context.plan.plan_id):
            return self._targeted_collection_pause(context, 'targeted test 실행 근거의 종료 확인·재수집 대기',
                                                  worker_result=message)
        self.ports.clear_agent_scope(self.ports.identity.repo)
        delivered_session = result.session_id or self.ports._row()['worker_session_id']
        if feedback_repo is not None and delivery is not None and delivered_session:
            feedback_repo.delivered(delivery.attempt_id, str(delivered_session))
        if draft is not None and not legacy_document_plan:
            try:
                self.ports._apply_linked_document(context, draft)
            except document_drafts.DocumentDraftError as error:
                detail = f'승인 문서 bytes 적용 대기: {error}'
                self.ports.state.update_project(
                    self.ports.identity.repo, phase='WORKING', last_error=detail,
                )
                return self.ports._recover(
                    RecoveryReason.WORKER_CORRECTION, detail, context=context,
                )
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

    def _persist_return_failure(self, context, cause, outcome):
        with runtime_state._transaction(self.ports.state.connection):
            self.ports.state.update_project(
                self.ports.identity.repo, preserve_transaction=True,
                phase='WORKING', last_error=cause,
            )
            notifications.enqueue_worker_failure(
                self.ports.state.connection, self.ports.identity.repo,
                context.plan.plan_id, cause,
                status=('사용량 제한 해제 후 다음 실행에서 재개'
                        if outcome is ExecutionOutcomeKind.USAGE_PAUSE
                        else '다음 실행에서 재개'),
            )
        notifications.flush_project_events(self.ports.state.connection, self.ports.identity.repo)

    def _guard_rebase_worker_result(self, context, input_snapshot, *, refresh=True):
        """Reconcile inputs before consuming READY or its pending Git continuation."""
        if refresh:
            context = self.ports.guard(context)
            if isinstance(context, ExecutionOutcome):
                return context
        blocked = self._targeted_test_block(context)
        if blocked is not None:
            return blocked
        current = self.ports.snapshot()._definition_snapshot
        previous = DefinitionSnapshot.deserialize(input_snapshot)
        if (previous is not None and current is not None
                and previous.worker_token == current.worker_token
                and previous.archive_token == current.archive_token):
            return context
        checkpoint = self.ports.checkpoint
        saved = self.ports.rebase_corrections.get(self.ports.identity.repo, context.plan.plan_id)
        message = saved.worker_result if saved else None
        with runtime_state._transaction(self.ports.state.connection):
            if checkpoint is not None:
                intent = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                snapshot = dict(intent.snapshot or {})
                message = message or snapshot.get('worker_result')
                history = snapshot.setdefault('rebase_worker_input_history', [])
                history.append({
                    'input_snapshot': input_snapshot,
                    'worker_result': message,
                    'stage_evidence': snapshot.get('stage_evidence'),
                    'pending': snapshot.get('pending'),
                    'turn': saved.pending_turn if saved else None,
                })
                for key in ('pending', 'worker_result', 'stage_evidence', 'rebase_worker_input'):
                    snapshot.pop(key, None)
                checkpoint.record(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=intent.generation, operation=intent.operation,
                    expected_head=intent.expected_head, target_base_oid=intent.target_base_oid,
                    observed_remote_oid=intent.observed_remote_oid, snapshot=snapshot,
                )
            if saved is not None:
                self.ports.rebase_corrections.abandon_turn(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=saved.generation,
                )
            detail = self.ports.contract.REBASE_CONFLICT_PREFIX + (
                '저장된 교정의 최신 구현 조건 근거가 없어 같은 Worker 세션에서 다시 확인합니다.\n'
                + json.dumps({
                    'previous_revision': previous.revision_sha if previous else None,
                    'current_revision': current.revision_sha if current else None,
                    'deltas': sorted(delta.value for delta in classify_definition_delta(previous, current))
                        if previous and current else ['unproven_worker_input'],
                }, ensure_ascii=False)
            )
            self.ports.state.update_project(
                self.ports.identity.repo, preserve_transaction=True,
                phase='WORKING', last_error=detail, validated_base_sha=None, worker_result=message,
                worker_input_token=None, archive_token=None, verify_token=None,
                review_token=None, publication_token=None, verify_result=None, review_result=None,
            )
        if self.ports._row()['agent_snapshot'] is not None:
            self.ports.clear_agent_scope(self.ports.identity.repo)
        return self.ports._retry(detail)

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
            if saved.result_kind == 'usage_limit':
                self._persist_return_failure(context, detail, ExecutionOutcomeKind.USAGE_PAUSE)
                return ExecutionOutcome(ExecutionOutcomeKind.USAGE_PAUSE,
                                        detail=detail)
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=detail)
            if saved.result_kind == 'transient_failure':
                return self.ports._recover(RecoveryReason.SERVICE_UNAVAILABLE, detail,
                                     context=context, attempts=3)
            return self.ports._rebase_conflict(
                context, detail, corrected=True, worker_result=saved.worker_result,
                abnormal_result=True,
            )
        guarded = self.ports.guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
        try:
            disposition = self._parse_worker_outcome(
                context, saved.worker_result,
                allow_legacy_ready=saved.outcome_contract_version == 0,
            )
            if disposition.kind == 'common_defect_pending':
                return self._common_defect_pending(context, saved.worker_result)
            if disposition.kind == 'needs_user_decision':
                return self._block_worker_decision(
                    context, saved.worker_result, disposition,
                )
            checked = self._guard_rebase_worker_result(
                context, getattr(saved, 'input_snapshot', None), refresh=False,
            )
            if isinstance(checked, ExecutionOutcome):
                return checked
            context = checked
            self.ports.parse_worker_result(saved.worker_result, self.ports.snapshot().plan)
        except (WorkerOutcomeError, self.ports.contract.WorkerResultError) as error:
            if self.ports._row()['agent_snapshot'] is not None:
                self.ports.clear_agent_scope(self.ports.identity.repo)
            detail = self.ports.contract.REBASE_CONFLICT_PREFIX + (
                f'Worker 결과 검증 실패:\n{error}'
            )
            if isinstance(error, TargetedTestFailure):
                detail += '\n새 테스트 실행 근거가 없습니다. ' + RUNNER_INSTRUCTION
            self.ports.state.update_project(self.ports.identity.repo, phase='WORKING', last_error=detail)
            if isinstance(error, TargetedTestFailure):
                return self.ports._rebase_conflict(
                    context, detail, corrected=True, worker_result=saved.worker_result,
                    recovery_reason=RecoveryReason.TARGETED_TEST_CORRECTION,
                    targeted_evidence_missing=True,
                )
            return self.ports._rebase_conflict(
                context, detail, corrected=True,
                worker_result=saved.worker_result,
                recovery_reason=RecoveryReason.WORKER_CORRECTION,
            )
        if self.ports._row()['agent_snapshot'] is not None:
            self.ports.clear_agent_scope(self.ports.identity.repo)
        return self.ports._finish_worker(context, self.ports._row(), saved.worker_result)

    def _bind_document_report(self, context, message, base):
        from loop_manager_internal.documents.pr_description import common_guidance_report, DocumentReportError
        worker = self.ports.parse_worker_result(message, self.ports.snapshot().plan)
        # active_sha may advance before this candidate has integrated that base.
        observed = self.ports.git(context.worktree, 'merge-base', base, 'HEAD', check=False)
        if observed.returncode or not observed.stdout.strip():
            return self.ports._recover(RecoveryReason.STATUS_FAILED,
                f'문서 보고의 작업 base를 확인할 수 없습니다: base={base}, candidate=HEAD', context=context)
        base = observed.stdout.strip()
        try:
            common_guidance_report(self.ports.git, context.worktree, base, '', '', worker.summary,
                selected_execplan_path=context.plan.path,
                evidence=(self.ports.state.connection, self.ports.identity.repo, context.plan.plan_id, message),
                bind=True)
        except DocumentReportError as error:
            self.ports.state.update_project(self.ports.identity.repo, worker_result=message, last_error=str(error))
            return self.ports._recover(RecoveryReason.WORKER_CORRECTION, str(error), context=context)

    def _finish_worker(self, context, row, message, *, staged=False):
        assert self.ports.snapshot().catalog_plan is not None and self.ports.snapshot().plan is not None
        latest_sha = str(row['active_sha'] or '')
        if not context.resolving_rebase:
            from loop_manager_internal.execution.worker_checkpoint import stage_candidate
            stage_candidate(self.ports, context)
            report_outcome = self._bind_document_report(context, message, latest_sha)
            if report_outcome is not None:
                return report_outcome
        if context.resolving_rebase:
            definition = self.ports.snapshot()._definition_snapshot
            if definition is not None:
                self.ports.state.update_project(
                    self.ports.identity.repo, worker_result=message,
                    worker_input_token=definition.worker_token, archive_token=definition.archive_token,
                )
            if self.ports.current_rebase_service is not None:
                _, decision = self.ports._current_rebase_service(context).require_owned()
                if decision.status is RebaseStatus.COMPLETE:
                    base = self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id).target_base_oid or latest_sha
                    report_outcome = self._bind_document_report(context, message, base)
                    if report_outcome is not None:
                        return report_outcome
                    self.ports._complete_rebase_intent(context, worker_definition=definition)
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
            report_outcome = self._bind_document_report(context, message, latest_sha)
            if report_outcome is not None:
                return report_outcome
            self.ports._complete_rebase_intent(context, project_values={
                'phase': 'WORKING', 'active_sha': latest_sha,
                'validated_base_sha': None, 'last_error': None,
                'verify_result': None, 'review_result': None,
                'verify_token': None, 'review_token': None,
                'publication_token': None,
            }, worker_definition=definition)
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
