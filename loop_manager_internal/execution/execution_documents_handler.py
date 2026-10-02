"""Documents execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome
from dataclasses import dataclass
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

    def _document_draft(self, context):
        if not self.ports.is_loop_manager_repository(self.ports.identity.repo_path):
            return None
        draft = document_drafts.DocumentDraftRepository(
            self.ports.state.connection,
        ).for_plan(self.ports.identity.repo, context.plan.plan_id)
        try:
            claim = document_publication.plan_claim(
                document_publication.read_plan_text(context.worktree / context.plan.path)
            )
        except (OSError, UnicodeError) as error:
            raise document_drafts.DocumentDraftError(
                '문서 ExecPlan identity를 읽을 수 없습니다.'
            ) from error
        if (claim is None) != (draft is None) or (
            claim is not None and draft is not None and claim != draft.draft_id
        ):
            raise document_drafts.DocumentDraftError(
                '문서 ExecPlan과 승인 매핑 identity가 다릅니다.'
            )
        return draft

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
        document_publication.validate_document_plan(
            plan_text, legacy_plan_metadata(self.ports.snapshot().plan), draft,
        )
        document_publication.validate_exact_worktree(
            draft, context.worktree, str(self.ports._row()['active_sha'] or ''),
        )

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
        except (document_drafts.DocumentDraftError, OSError, UnicodeError) as error:
            return self.ports._document_block(context, draft, error)
        result = (
            f"COMMIT_TITLE: Apply approved document draft {draft.draft_id[:12]}\n"
            f"PR_SUMMARY: Apply the exact approved bytes to {draft.target_path}; "
            "retain the source and meaning-preservation review gates.\n"
            "TARGETED_TESTS: 문서 바이트 적용만 수행하므로 실행 가능한 관련 unit test가 없습니다."
        )
        return self.ports._finish_worker(context, self.ports._row(), result)

