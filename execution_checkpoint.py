"""Pure assessment of a durable execution marker against observed Git state."""

from __future__ import annotations

import rebase_file_evidence

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping


class CheckpointAction(str, Enum):
    CONTINUE = "continue"
    CORRECT = "correct"
    REBASE = "rebase"
    CONFLICT = "conflict"
    PAUSE = "pause"
    USER_ACTION = "user_action"


# Git may retain message and current-commit files after an operation has
# completed. They are useful evidence in the durable snapshot, but only the
# operation's controlling state proves that another Git command is unsafe.
_CONTROLLING_GIT_OPERATIONS = frozenset({
    'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge',
    'rebase-apply', 'sequencer', 'BISECT_HEAD', 'BISECT_LOG',
    'BISECT_START', 'BISECT_TERMS',
})


@dataclass(frozen=True)
class CheckpointSnapshot:
    plan_id: str
    phase: str
    definition_revision: str | None
    base_oid: str
    branch: str
    worktree_owned: bool
    local_branch: str | None
    local_head: str | None
    recorded_local_head: str | None
    pending_operation: str | None
    base_is_ancestor: bool | None
    active_sha: str | None
    validated_base_sha: str | None
    verify_token: str | None
    review_token: str | None
    archive_token: str | None
    publication_token: str | None
    git_operation: str | None
    git_operation_markers: tuple[tuple[str, str], ...]
    unmerged_stages: tuple[tuple[str, int, str], ...]
    rebase_intent: bool
    rebase_before_head: str | None
    rebase_target_oid: str | None
    stash_oid: str | None
    staged_paths: tuple[str, ...]
    remote_oid: str | None
    recorded_remote_oid: str | None
    pr_state: str | None
    pr_head_oid: str | None
    recorded_pr_head_oid: str | None


@dataclass(frozen=True)
class CheckpointDecision:
    action: CheckpointAction
    phase: str | None = None
    active_sha: str | None = None
    validated_base_sha: str | None = None
    invalidate_validation: bool = False
    reason: str = ""


def owned_detached_rebase(
    *, operation: str | None, expected_head: str | None,
    target_base_oid: str | None, recorded_before_head: str | None,
    branch: str, remote_oid: str | None, accepted_remote_oid: str | None,
    recorded_remote_oid: str | None, checkpoint_remote_oid: str | None,
    head_name: str | None,
    orig_head: str | None, onto: str | None,
) -> bool:
    """A detached conflict is owned only by the persisted rebase and Git metadata."""
    return bool(
        operation == "rebase"
        and expected_head and target_base_oid
        and (not recorded_before_head or recorded_before_head == expected_head)
        and accepted_remote_oid and remote_oid == accepted_remote_oid
        and recorded_remote_oid in (None, "", accepted_remote_oid)
        and checkpoint_remote_oid in (None, accepted_remote_oid)
        and head_name == f"refs/heads/{branch}"
        and orig_head == expected_head and onto == target_base_oid
    )


def rebase_preserves_worktree_files(before: Mapping[str, object],
                                    after: Mapping[str, object], *,
                                    before_evidence=None, after_evidence=None) -> bool:
    """Preserve contents or prove the local edits survived a disjoint base merge."""
    return set(before) == set(after) and all(
        isinstance(before[path], (list, tuple))
        and isinstance(after[path], (list, tuple))
        and len(before[path]) == len(after[path]) == 2
        and (before[path][1] == after[path][1] or rebase_file_evidence.preserved(
            (before_evidence or {}).get(path), (after_evidence or {}).get(path),
            before[path][1], after[path][1],
        ))
        for path in before
    )


def assess(snapshot: CheckpointSnapshot) -> CheckpointDecision:
    """Return one safe next step; never infer ownership from marker agreement."""
    if not snapshot.worktree_owned or snapshot.local_branch != snapshot.branch or not snapshot.local_head:
        return CheckpointDecision(CheckpointAction.USER_ACTION, reason="workspace_ownership_uncertain")
    if snapshot.remote_oid not in {None, snapshot.local_head, snapshot.recorded_remote_oid}:
        return CheckpointDecision(CheckpointAction.USER_ACTION, reason="external_remote_oid")
    if snapshot.pr_state == "CLOSED":
        # The existing PR reconciliation owns its closed-PR block and URL.
        return CheckpointDecision(CheckpointAction.CONTINUE)
    if snapshot.pr_head_oid and snapshot.recorded_pr_head_oid and snapshot.pr_head_oid != snapshot.recorded_pr_head_oid:
        return CheckpointDecision(CheckpointAction.USER_ACTION, reason="external_pr_head")
    if snapshot.unmerged_stages:
        if snapshot.git_operation == "rebase":
            return CheckpointDecision(CheckpointAction.CONFLICT, phase="IMPLEMENTING", reason="rebase_index_conflict")
        if snapshot.rebase_intent:
            if not snapshot.stash_oid:
                return CheckpointDecision(CheckpointAction.USER_ACTION, reason="autostash_identity_unavailable")
            return CheckpointDecision(CheckpointAction.CONFLICT, phase="IMPLEMENTING", reason="rebase_index_conflict")
        return CheckpointDecision(CheckpointAction.USER_ACTION, reason="unowned_index_conflict")
    if snapshot.git_operation == "rebase":
        return CheckpointDecision(CheckpointAction.CONFLICT, phase="IMPLEMENTING", reason="rebase_in_progress")
    if snapshot.git_operation:
        return CheckpointDecision(CheckpointAction.PAUSE, reason="git_operation_in_progress")
    if snapshot.recorded_local_head and snapshot.local_head != snapshot.recorded_local_head:
        if snapshot.pending_operation == "commit":
            if snapshot.base_is_ancestor is False:
                return CheckpointDecision(CheckpointAction.REBASE,
                                          reason="commit_completed_before_rebase")
            return CheckpointDecision(
                CheckpointAction.CORRECT, phase="VERIFYING",
                active_sha=snapshot.base_oid, validated_base_sha=None,
                invalidate_validation=True,
                reason="commit_completed_before_checkpoint",
            )
        if not snapshot.rebase_intent:
            return CheckpointDecision(CheckpointAction.USER_ACTION,
                                      reason="unobserved_local_head")
    if snapshot.base_is_ancestor is None:
        return CheckpointDecision(CheckpointAction.PAUSE, reason="ancestry_unavailable")
    if not snapshot.base_is_ancestor:
        return CheckpointDecision(CheckpointAction.REBASE, reason="base_stale")
    if snapshot.rebase_intent:
        return CheckpointDecision(
            CheckpointAction.CORRECT, phase="VERIFYING", active_sha=snapshot.base_oid,
            validated_base_sha=None, invalidate_validation=True,
            reason="rebase_completed_before_checkpoint",
        )
    if snapshot.phase == "IMPLEMENTING":
        return CheckpointDecision(CheckpointAction.CONTINUE)
    if snapshot.active_sha != snapshot.base_oid or snapshot.validated_base_sha not in {None, snapshot.base_oid}:
        return CheckpointDecision(
            CheckpointAction.CORRECT, phase="VERIFYING", active_sha=snapshot.base_oid,
            validated_base_sha=None, invalidate_validation=True,
            reason="base_marker_stale",
        )
    if snapshot.phase in {"REVIEWING", "PUBLISHING"} and snapshot.validated_base_sha != snapshot.base_oid:
        if snapshot.validated_base_sha is None and snapshot.verify_token is None:
            # Legacy rows may carry a completed verify result without a base
            # marker or revision token. The revision gate adopts or rejects
            # that result from its full artifact snapshot.
            return CheckpointDecision(CheckpointAction.CONTINUE)
        return CheckpointDecision(
            CheckpointAction.CORRECT, phase="VERIFYING", active_sha=snapshot.base_oid,
            validated_base_sha=None, invalidate_validation=True,
            reason="validation_marker_stale",
        )
    return CheckpointDecision(CheckpointAction.CONTINUE)


def observe(
    *, plan_id: str, row: Mapping[str, object], definition_revision: str | None,
    base_oid: str, branch: str, worktree: object, owned: bool,
    git: Callable[..., object], rebase_in_progress: Callable[..., bool],
    remote_oid: str | None, pull_request: Mapping[str, object] | None,
    rebase_intent: bool, rebase_before_head: str | None,
    rebase_target_oid: str | None, stash_oid: str | None,
    recorded_local_head: str | None = None,
    pending_operation: str | None = None,
    operation_markers: Mapping[str, object] | None = None,
) -> CheckpointSnapshot:
    """Read the index as well as Git operation metadata before any phase action."""
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    branch_name = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch_name.returncode not in {0, 1}:
        raise RuntimeError("local branch observation failed")
    local_branch = branch_name.stdout.strip() if branch_name.returncode == 0 else None
    rebase = rebase_in_progress(worktree, branch)
    if rebase and local_branch is None:
        local_branch = branch
    unmerged = git(worktree, "ls-files", "-u", "-z").stdout
    stages: list[tuple[str, int, str]] = []
    for entry in unmerged.split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            _mode, oid, stage = metadata.split(" ")
            stages.append((path, int(stage), oid))
    staged = git(worktree, "diff", "--cached", "--name-only", "-z").stdout
    ancestor = git(worktree, "merge-base", "--is-ancestor", base_oid, "HEAD", check=False)
    if ancestor.returncode not in {0, 1}:
        raise RuntimeError("base ancestry observation failed")
    return CheckpointSnapshot(
        plan_id=plan_id, phase=str(row["phase"]),
        definition_revision=definition_revision, base_oid=base_oid,
        branch=branch, worktree_owned=owned, local_branch=local_branch,
        local_head=head, recorded_local_head=recorded_local_head,
        pending_operation=pending_operation,
        base_is_ancestor=ancestor.returncode == 0,
        active_sha=str(row["active_sha"]) if row["active_sha"] else None,
        validated_base_sha=(str(row["validated_base_sha"])
                            if row["validated_base_sha"] else None),
        verify_token=str(row["verify_token"]) if row["verify_token"] else None,
        review_token=str(row["review_token"]) if row["review_token"] else None,
        archive_token=str(row["archive_token"]) if row["archive_token"] else None,
        publication_token=(str(row["publication_token"])
                           if row["publication_token"] else None),
        git_operation=("rebase" if rebase else next(iter(sorted(
            _CONTROLLING_GIT_OPERATIONS.intersection(operation_markers or {})
        )), None)),
        git_operation_markers=tuple(sorted(
            (str(name), str(signature))
            for name, signature in (operation_markers or {}).items()
        )),
        unmerged_stages=tuple(stages), rebase_intent=rebase_intent,
        rebase_before_head=rebase_before_head,
        rebase_target_oid=rebase_target_oid, stash_oid=stash_oid,
        staged_paths=tuple(path for path in staged.split("\0") if path),
        remote_oid=remote_oid,
        recorded_remote_oid=(str(row["publication_remote_oid"])
                             if row["publication_remote_oid"] else None),
        pr_state=(str(pull_request.get("state") or "").upper()
                  if pull_request else None),
        pr_head_oid=(str(pull_request.get("headRefOid") or "")
                     if pull_request else None),
        recorded_pr_head_oid=(str(row["pr_head_oid"])
                              if row["pr_head_oid"] else None),
    )
