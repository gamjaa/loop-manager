"""Select a saved rebase cause, independently of current-result validation.

Selection grants no Git or publication permission. Callers recheck the owned
current operation and release only this exact block in a transaction.
"""

from __future__ import annotations

import hashlib
import json

from execution_checkpoint import controlling_git_operation

from recovery_runtime import RecoveryReason
from scoped_blocking import BlockReasonCode


LEGACY_COMPLETION_DIAGNOSTIC = (
    "Manager rebase 결과의 HEAD·ref·index·등록 상태가 다릅니다."
)
REBASE_BLOCK_REASONS = frozenset({
    BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value,
    BlockReasonCode.REPOSITORY_STATE_UNSAFE.value,
    BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value,
    BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value,
})


def saved_failure_matches(recovery, checkpoint) -> bool:
    """Require the old failure and its still-pending rebase intent."""

    if recovery is None or checkpoint is None or checkpoint.operation != "rebase":
        return False
    return bool(
        recovery["reason_code"] == RecoveryReason.STATUS_FAILED.value
        and recovery["detail"] == LEGACY_COMPLETION_DIAGNOSTIC
        and recovery["pending_action"] in {"retry", "pause"}
        and checkpoint.expected_head
        and checkpoint.target_base_oid
    )


def block_matches(block, recovery, checkpoint) -> bool:
    """Connect a structured cause to its selected plan's rebase intent.

    Conflict causes already identify rebase recovery. Generic project safety
    causes additionally need a saved diagnosis connecting them to rebase. Past
    sessions, correction counts and occurrence history are not ownership proof.
    """
    if (block is None or block['reason_code'] not in REBASE_BLOCK_REASONS
            or checkpoint is None or not checkpoint.expected_head
            or not checkpoint.target_base_oid):
        return False
    saved = getattr(checkpoint, 'snapshot', None) or {}
    if checkpoint.operation != 'rebase' and not (
            checkpoint.operation is None and any(saved.get(key) for key in (
                'current_rebase_acceptance', 'completed_rebase', 'rebase_completion',
                'blocked_resume'))):
        return False
    if block['reason_code'] == BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value:
        return True
    if block['evidence'] == LEGACY_COMPLETION_DIAGNOSTIC:
        return True
    if (recovery is None or recovery['detail'] != block['evidence']
            or recovery['reason_code'] not in {
                RecoveryReason.REBASE_CONFLICT.value, RecoveryReason.STATUS_FAILED.value,
                RecoveryReason.MANAGED_CONFLICT_UNRECOVERABLE.value,
                RecoveryReason.STATE_UNSAFE.value, RecoveryReason.REMOTE_OID_UNCERTAIN.value,
                RecoveryReason.OWNERSHIP_UNCERTAIN.value}):
        return False
    if recovery['pending_action'] != 'final_block':
        return recovery['reason_code'] == RecoveryReason.REBASE_CONFLICT.value
    try:
        evidence = json.loads(recovery["final_evidence"] or "null")
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(evidence, dict)
        and evidence.get("block_reason_code") == block["reason_code"]
    )


def could_be_rebase_evidence_block(block, recovery=None, checkpoint=None) -> bool:
    """Quarantine legacy-looking blocks even when their proof is missing."""

    return bool(
        block is not None
        and block["reason_code"] in REBASE_BLOCK_REASONS
        and (block['reason_code'] == BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value
             or block["evidence"] == LEGACY_COMPLETION_DIAGNOSTIC
             or block_matches(block, recovery, checkpoint))
    )


def pending_block_unchanged(plan, block, recovery, before, current, *,
                            revision_input, worktree_input) -> bool:
    """A saved blocked state suppresses unchanged retries, never proves ownership.

    Missing historical baselines permit current-state recovery. Only an actual
    unchanged selected worktree and unchanged inputs retain an exhausted block.
    """
    return bool(
        block['reason_code'] == BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value
        and recovery is not None and recovery['pending_action'] == 'final_block'
        and recovery['reason_code'] == RecoveryReason.REBASE_CONFLICT.value
        and recovery['detail'] == block['evidence']
        and plan['blocked_fingerprint'] and plan['blocked_worktree_fingerprint']
        and revision_input == plan['blocked_fingerprint']
        and worktree_input == plan['blocked_worktree_fingerprint']
        and all(getattr(before, field) == getattr(current, field)
                for field in ('head', 'branch', 'index', 'dirty', 'operations'))
    )


def require_clean_completion(observed) -> None:
    """Do not seal unapproved post-rebase files or index changes as completion."""

    index = observed.get("index")
    flagged_paths = sorted(
        path for path, entries in (index or {}).items()
        if any(not entry.startswith("H ") for entry in entries)
    ) if isinstance(index, dict) else []
    if (not isinstance(index, dict) or flagged_paths
            or observed.get("metadata") is not None
            or observed.get("unmerged") is not False
            or observed.get("index_clean") is not True
            or observed.get("dirty") != {}
            or not isinstance(observed.get("operations"), dict)
            or controlling_git_operation(observed["operations"])):
        index_fingerprint = hashlib.sha256(json.dumps(
            observed.get("index"), sort_keys=True,
        ).encode()).hexdigest()
        raise ValueError(
            "R-045 rebase final worktree/index: expected=clean "
            f"observed index_clean={observed.get('index_clean')} "
            f"dirty={sorted(observed.get('dirty') or {})} "
            f"unmerged={observed.get('unmerged')} flags={flagged_paths} "
            f"index_fingerprint={index_fingerprint}"
        )
