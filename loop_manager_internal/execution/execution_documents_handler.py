"""Documents execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome
from dataclasses import dataclass
import json
from typing import Callable, Any, Mapping
from loop_manager_internal.documents import document_drafts
from loop_manager_internal.documents import document_publication
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode

from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot, legacy_plan_metadata
from loop_manager_internal.execution.execution_state import ExecutionStatePort

@dataclass(frozen=True)
class DocumentsHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    state: ExecutionStatePort
    is_loop_manager_repository: Callable[..., Any]
    _block_final: Callable[..., ExecutionOutcome]
    _document_block: Callable[..., ExecutionOutcome]
    _finish_worker: Callable[..., ExecutionOutcome]
    _row: Callable[[], Mapping[str, object]]


class DocumentsHandler:
    """Documents policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: DocumentsHandlerPorts):
        self.ports = ports

    @staticmethod
    def _saved_relationships(draft):
        try:
            relationships = json.loads(draft.source_relationships)
        except (TypeError, json.JSONDecodeError) as error:
            raise document_drafts.DocumentDraftError(
                '승인 당시 비종료 작업 source 관계가 손상됐습니다.'
            ) from error
        if not isinstance(relationships, list):
            raise document_drafts.DocumentDraftError(
                '승인 당시 비종료 작업 source 관계가 유효하지 않습니다.'
            )
        parsed = []
        for item in relationships:
            if (not isinstance(item, list) or len(item) != 2
                    or not isinstance(item[0], str) or not isinstance(item[1], list)
                    or any(not isinstance(source, str) for source in item[1])):
                raise document_drafts.DocumentDraftError(
                    '승인 당시 비종료 작업 source 관계가 유효하지 않습니다.'
                )
            parsed.append((item[0], tuple(sorted(item[1]))))
        return tuple(parsed)

    def _linked_plan(self, context, draft, *, allow_unbound=False):
        if (draft.repo != self.ports.identity.repo
                or (draft.plan_id != context.plan.plan_id
                    and not (allow_unbound and draft.plan_id is None))):
            raise document_drafts.DocumentDraftError('승인 수정본의 기존 작업 identity가 다릅니다.')
        relationships = self._saved_relationships(draft)
        sources = tuple(sorted(str(item) for item in context.plan.sources))
        if (context.plan.plan_id, sources) not in relationships:
            raise document_drafts.DocumentDraftError(
                '승인 당시 source snapshot에 현재 작업의 명시적 연결이 없습니다.'
            )
        plan = self.ports.state.connection.execute(
            'SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
            (self.ports.identity.repo, context.plan.plan_id),
        ).fetchone()
        if plan is None or plan['path'] != context.plan.path:
            raise document_drafts.DocumentDraftError('현재 작업 선언을 확인할 수 없습니다.')
        try:
            stored_sources = json.loads(plan['sources'])
        except (TypeError, json.JSONDecodeError) as error:
            raise document_drafts.DocumentDraftError('현재 작업 source 선언이 손상됐습니다.') from error
        if (not isinstance(stored_sources, list)
                or tuple(sorted(str(item) for item in stored_sources)) != sources):
            raise document_drafts.DocumentDraftError('현재 작업 source 선언이 승인 snapshot과 다릅니다.')
        from loop_manager_internal.storage import work_claims
        try:
            _ownership, manually_owned = work_claims.manual_plan_ownership(
                self.ports.state.connection, self.ports.identity.repo,
                context.plan.plan_id, plan,
            )
        except RuntimeError as error:
            raise document_drafts.DocumentDraftError(
                f'현재 작업 소유권을 확인할 수 없습니다: {error}'
            ) from error
        if manually_owned:
            raise document_drafts.DocumentDraftError('현재 작업이 수동 소유 상태입니다.')
        return plan

    def _hold(self, drafts, draft, reason):
        with self.ports.state.connection:
            drafts.record_publication_error(draft.draft_id, reason)

    def _document_draft(self, context):
        if not self.ports.is_loop_manager_repository(self.ports.identity.repo_path):
            return None
        drafts = document_drafts.DocumentDraftRepository(self.ports.state.connection)
        draft = drafts.for_plan(self.ports.identity.repo, context.plan.plan_id)
        try:
            claim = document_publication.plan_claim(
                document_publication.read_plan_text(context.worktree / context.plan.path)
            )
        except (OSError, UnicodeError) as error:
            raise document_drafts.DocumentDraftError(
                '문서 ExecPlan identity를 읽을 수 없습니다.'
            ) from error
        if claim is not None and (draft is None or claim != draft.draft_id):
            raise document_drafts.DocumentDraftError(
                '문서 ExecPlan과 승인 매핑 identity가 다릅니다.'
            )
        if claim is not None:
            return draft  # Preserve the already-published document-only plan contract.
        if draft is not None:
            phase = getattr(context.phase, 'value', str(context.phase))
            applied = document_publication.approved_bytes_present(draft, context.worktree)
            try:
                plan = self._linked_plan(context, draft)
                drafts.assert_current(
                    draft, self.ports.identity.repo_path, self.ports.identity.base,
                )
            except document_drafts.DocumentDraftError as error:
                self._hold(drafts, draft, f'승인 수정본 미적용: {error}')
                if applied:
                    return draft
                return None
            if phase != 'IMPLEMENTING' and not applied:
                self._hold(drafts, draft, 'Worker 구현 단계가 지난 기존 작업에는 승인 bytes를 추가하지 않았습니다.')
                return None
            if (phase == 'IMPLEMENTING' and not applied
                    and (plan['status'] != 'IMPLEMENTING' or plan['pr_url']
                         or plan['pr_head_oid'] or plan['pr_conflict_recovery'])):
                self._hold(drafts, draft, 'PR 또는 다른 실행 상태가 확인되어 승인 bytes를 추가하지 않았습니다.')
                return None
            return draft
        if getattr(context.phase, 'value', str(context.phase)) != 'IMPLEMENTING':
            return None

        approved = drafts.unplanned_approved(
            self.ports.identity.repo, self.ports.identity.repo_path,
            self.ports.identity.base,
        )
        plan_id = context.plan.plan_id
        plan_sources = tuple(sorted(str(item) for item in context.plan.sources))
        candidates = []
        for item in approved:
            if self._saved_relationships(item) == ((plan_id, plan_sources),):
                candidates.append(item)
        if len(candidates) != 1:
            for item in approved:
                if any(related_id == plan_id
                       for related_id, _sources in self._saved_relationships(item)):
                    self._hold(
                        drafts, item,
                        '승인 당시 연결된 기존 작업이 하나로 확정되지 않아 수정본을 적용하지 않았습니다.',
                    )
            return None

        candidate = candidates[0]
        try:
            plan = self._linked_plan(context, candidate, allow_unbound=True)
            if (plan['status'] != 'IMPLEMENTING' or plan['pr_url']
                    or plan['pr_head_oid'] or plan['pr_conflict_recovery']):
                raise document_drafts.DocumentDraftError(
                    '기존 작업이 Worker 실행 전 상태가 아니거나 PR이 연결돼 있습니다.'
                )
            drafts.assert_current(
                candidate, self.ports.identity.repo_path, self.ports.identity.base,
            )
            drafts.bind_plan(self.ports.identity.repo, candidate.draft_id, plan_id)
            candidate = drafts.for_plan(self.ports.identity.repo, plan_id)
            drafts.assert_current(
                candidate, self.ports.identity.repo_path, self.ports.identity.base,
            )
        except document_drafts.DocumentDraftError as error:
            self._hold(drafts, candidate, f'승인 수정본 미적용: {error}')
            return None
        return candidate

    def _document_block(self, context, draft, error):
        detail = f"승인 문서 게시 중단: {error}"
        with self.ports.state.connection:
            document_drafts.DocumentDraftRepository(
                self.ports.state.connection,
            ).record_publication_error(draft.draft_id, detail)
        return self.ports._block_final(
            self.ports.identity.repo, context.worktree, detail,
            str(self.ports._row()['verify_result'] or ''),
            reason=BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
            user_targets=(draft.target_path,),
        )

    def _document_guard(self, context, draft):
        drafts = document_drafts.DocumentDraftRepository(
            self.ports.state.connection,
        )
        drafts.assert_current(draft, self.ports.identity.repo_path, self.ports.identity.base)
        plan_text = document_publication.read_plan_text(
            context.worktree / context.plan.path,
        )
        if document_publication.plan_claim(plan_text) is not None:
            document_publication.validate_document_plan(
                plan_text, legacy_plan_metadata(self.ports.snapshot().plan), draft,
            )
            document_publication.validate_exact_worktree(
                draft, context.worktree, str(self.ports._row()['active_sha'] or ''),
            )
        else:
            self._linked_plan(context, draft)
            document_publication.validate_linked_worktree(
                draft, context.worktree, str(self.ports._row()['active_sha'] or ''),
            )

    def _document_worker_prompt(self, base_prompt, draft):
        return (
            (base_prompt or '')
            + '\n기존 작업에 명시적으로 연결된 승인 문서 수정본 입력이다. 같은 Worker 세션에서 '
              '현재 ExecPlan의 구현 적합성을 확인하고 필요한 코드·테스트 보완을 수행하라. '
              '아래 승인 대상 파일은 직접 수정하지 마라. Manager가 현재 원본·source 관계를 '
              '재검증한 뒤 승인 bytes만 적용하며, 승인 자체를 구현·검증·Review·게시 성공으로 '
              '취급하지 않는다. 별도의 Planner 호출이나 새 ExecPlan을 만들지 마라.\n'
            + document_drafts.review_artifact(draft)
        )

    def _apply_linked_document(self, context, draft):
        drafts = document_drafts.DocumentDraftRepository(
            self.ports.state.connection,
        )
        try:
            current = drafts.for_plan(self.ports.identity.repo, context.plan.plan_id)
            if current != draft:
                raise document_drafts.DocumentDraftError(
                    'Worker 실행 중 승인 수정본 identity·revision·bytes가 변경됐습니다.'
                )
            draft = current
            plan = self._linked_plan(context, draft)
            if (getattr(context.phase, 'value', str(context.phase)) == 'IMPLEMENTING'
                    and (plan['status'] != 'IMPLEMENTING' or plan['pr_url']
                         or plan['pr_head_oid'] or plan['pr_conflict_recovery'])):
                raise document_drafts.DocumentDraftError(
                    'PR 또는 다른 실행 상태에서 승인 bytes를 추가할 수 없습니다.'
                )
            drafts.assert_current(
                draft, self.ports.identity.repo_path, self.ports.identity.base,
            )
        except document_drafts.DocumentDraftError as error:
            self._hold(drafts, draft, f'승인 수정본 미적용: {error}')
            return False
        try:
            document_publication.apply_linked_approved_bytes(
                draft, context.worktree, str(self.ports._row()['active_sha'] or ''),
            )
        except document_drafts.DocumentDraftError as error:
            self._hold(drafts, draft, f'승인 수정본 미적용: {error}')
            raise
        with self.ports.state.connection:
            drafts.clear_publication_error(draft.draft_id)
        return True

    def _document_review_prompt(self, base_prompt, draft):
        if draft is None:
            return base_prompt
        return (
            base_prompt
            + "\n문서 전용 검토: 아래 승인 artifact의 원본·수정본·전체 diff와 "
            "현재 PR diff를 비교하고 PRODUCT·Requirement·ARCHITECTURE 역할, "
            "기존 동작·결정·Acceptance Criteria·문서 간 일관성의 의미 보존을 "
            "판정하라. 마지막 VERDICT 앞에 DOCUMENT_MEANING: PASS 또는 "
            "DOCUMENT_MEANING: UNCERTAIN 및 DOCUMENT_DIFF: PASS 또는 "
            "DOCUMENT_DIFF: FAIL을 각각 한 줄로 명시하라.\n"
            + document_drafts.review_artifact(draft)
        )

    def _run_document(self, context, draft):
        try:
            drafts = document_drafts.DocumentDraftRepository(
                self.ports.state.connection,
            )
            drafts.assert_current(draft, self.ports.identity.repo_path, self.ports.identity.base)
            plan_text = document_publication.read_plan_text(
                context.worktree / context.plan.path,
            )
            document_publication.validate_document_plan(
                plan_text, legacy_plan_metadata(self.ports.snapshot().plan), draft,
            )
            document_publication.apply_approved_bytes(
                draft, context.worktree, str(self.ports._row()['active_sha'] or ''),
            )
            with self.ports.state.connection:
                drafts.clear_publication_error(draft.draft_id)
        except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
            return self.ports._document_block(context, draft, error)
        result = (
            f"COMMIT_TITLE: Apply approved document draft {draft.draft_id[:12]}\n"
            f"PR_SUMMARY: Apply the exact approved bytes to {draft.target_path}; "
            "retain the source and meaning-preservation review gates.\n"
            "TARGETED_TESTS: 문서 바이트 적용만 수행하므로 실행 가능한 관련 unit test가 없습니다."
        )
        return self.ports._finish_worker(context, self.ports._row(), result)

