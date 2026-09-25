"""Approved document plan identity and exact worktree publication gates."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Mapping

from document_drafts import (
    DocumentDraft, DocumentDraftError, R027_SOURCE, _complete_diff, _document_path,
    _git, _hash,
)


def _document_target(draft: DocumentDraft, worktree: Path) -> Path:
    if not _document_path(draft.target_path):
        raise DocumentDraftError("승인 문서 대상 경로가 유효하지 않습니다.")
    target = worktree / draft.target_path
    if not target.resolve().is_relative_to(worktree.resolve()):
        raise DocumentDraftError("승인 문서 대상이 관리 worktree 밖을 가리킵니다.")
    current = target
    while current != worktree:
        if current.is_symlink():
            raise DocumentDraftError("승인 문서 대상에 symlink가 있습니다.")
        current = current.parent
    if not target.is_file():
        raise DocumentDraftError("문서 대상이 일반 파일이 아닙니다.")
    return target


def plan_claim(text: str) -> str | None:
    match = re.search(r"(?m)^DOCUMENT_DRAFT_ID: ([0-9a-f]{32})$", text)
    if text.count("DOCUMENT_DRAFT_ID:") > 1:
        raise DocumentDraftError("문서 ExecPlan에 수정본 ID가 중복됐습니다.")
    if match is None and "DOCUMENT_DRAFT_ID:" in text:
        raise DocumentDraftError("문서 ExecPlan 수정본 ID 형식이 유효하지 않습니다.")
    return match.group(1) if match else None


def validate_document_plan(
    text: str, plan: Mapping[str, object], draft: DocumentDraft,
) -> None:
    """Bind the published declaration to one exact approved byte sequence."""
    if plan_claim(text) != draft.draft_id or draft.state != "approved":
        raise DocumentDraftError("문서 ExecPlan 승인 identity가 다릅니다.")
    if plan.get("sources") != [R027_SOURCE]:
        raise DocumentDraftError("문서 ExecPlan은 R-027만 근거로 사용해야 합니다.")
    claims = {
        "Target": draft.target_path,
        "Decision revision": draft.decision_revision,
        "Original revision": draft.original_revision,
        "Original SHA256": draft.original_hash,
        "Proposed SHA256": draft.proposed_hash,
        "Approved diff SHA256": _hash(draft.complete_diff.encode("utf-8")),
    }
    for label, expected in claims.items():
        if expected is None or text.count(f"{label}: {expected}\n") != 1:
            raise DocumentDraftError(f"문서 ExecPlan {label} identity가 다릅니다.")
    diff_block = f"```diff\n{draft.complete_diff}```"
    if text.count(diff_block) != 1:
        raise DocumentDraftError("문서 ExecPlan의 승인 diff가 정확히 일치하지 않습니다.")


def review_confirms_publication(review: str) -> bool:
    """Require exactly one explicit semantic and byte-diff confirmation."""
    return (
        re.findall(r"(?m)^DOCUMENT_MEANING: ([^\r\n]+)\r?$", review) == ["PASS"]
        and re.findall(r"(?m)^DOCUMENT_DIFF: ([^\r\n]+)\r?$", review) == ["PASS"]
    )


def planning_instruction(draft: DocumentDraft) -> str:
    """Give the Planner a bounded identity and exact diff contract."""
    return (
        "\n이번 호출에서는 다음 승인 문서 수정본 전용 ExecPlan 하나만 새로 작성하라. "
        "기존 계획, FUTURE_IDEAS 및 원본 명세를 수정하지 마라. "
        "sources는 아래 R-027 하나만 사용하고 Acceptance Criteria에는 원본 재검증, "
        "승인 바이트의 프로그램 적용, targeted test 사유, 전체 verify, read-only "
        "의미 보존 Review와 별도 PR·사용자 Merge를 명시하라. "
        "## Approved Document Draft 절에 아래 identity 줄을 정확히 넣고 "
        "승인 diff 전체를 다음 diff 코드 블록에 그대로 넣어라.\n"
        f"Source: {R027_SOURCE}\n"
        f"DOCUMENT_DRAFT_ID: {draft.draft_id}\n"
        f"Target: {draft.target_path}\n"
        f"Decision revision: {draft.decision_revision}\n"
        f"Original revision: {draft.original_revision}\n"
        f"Original SHA256: {draft.original_hash}\n"
        f"Proposed SHA256: {draft.proposed_hash}\n"
        f"Approved diff SHA256: {_hash(draft.complete_diff.encode('utf-8'))}\n"
        f"```diff\n{draft.complete_diff}```\n"
        "원본과 수정본의 실제 바이트는 프로그램의 승인 저장소가 제공한다. "
        "Worker Agent가 명세 원본을 수정하도록 지시하지 마라.\n"
    )


def apply_approved_bytes(
    draft: DocumentDraft, worktree: Path, base_revision: str,
) -> None:
    """Apply once in a managed worktree, accepting only an exact interrupted write."""
    target = _document_target(draft, worktree)
    original = _git(worktree, "show", f"{base_revision}:{draft.target_path}")
    if original != draft.original_content:
        raise DocumentDraftError("작업 공간의 원본 바이트가 승인 원본과 다릅니다.")
    changed = {
        path.decode("utf-8") for path in
        _git(worktree, "diff", "--name-only", "-z", base_revision).split(b"\0")
        if path
    }
    changed.update(
        path.decode("utf-8") for path in
        _git(worktree, "diff", "--cached", "--name-only", "-z", base_revision).split(b"\0")
        if path
    )
    changed.update(
        path.decode("utf-8") for path in
        _git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if path
    )
    current = target.read_bytes()
    if changed - {draft.target_path} or current not in {
        draft.original_content, draft.proposed_content,
    } or (changed and changed != {draft.target_path}):
        raise DocumentDraftError("작업 공간에 승인되지 않은 변경이 있습니다.")
    if current == draft.original_content:
        target.write_bytes(draft.proposed_content)
    validate_exact_worktree(draft, worktree, base_revision)


def validate_exact_worktree(
    draft: DocumentDraft, worktree: Path, base_revision: str,
) -> None:
    """Prove the whole proposed PR contains only the approved document bytes."""
    target = _document_target(draft, worktree)
    changed = {
        path.decode("utf-8") for path in
        _git(worktree, "diff", "--name-only", "-z", base_revision).split(b"\0")
        if path
    }
    changed.update(
        path.decode("utf-8") for path in
        _git(worktree, "diff", "--cached", "--name-only", "-z", base_revision).split(b"\0")
        if path
    )
    untracked = [path for path in
                 _git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
                 if path]
    if changed != {draft.target_path} or untracked:
        raise DocumentDraftError("문서 PR에 승인 대상 외의 변경이 있습니다.")
    if (_git(worktree, "diff", "--summary", base_revision, "--", draft.target_path)
        or _git(worktree, "diff", "--cached", "--summary", base_revision,
                "--", draft.target_path)):
        raise DocumentDraftError("문서 PR에 승인되지 않은 파일 mode 변경이 있습니다.")
    current = target.read_bytes()
    if current != draft.proposed_content:
        raise DocumentDraftError("문서 PR 바이트가 승인 수정본과 다릅니다.")
    original = _git(worktree, "show", f"{base_revision}:{draft.target_path}")
    if original != draft.original_content or _complete_diff(
        draft.target_path, original, current,
    ) != draft.complete_diff:
        raise DocumentDraftError("문서 PR diff가 승인 diff와 다릅니다.")
