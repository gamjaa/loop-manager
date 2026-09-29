"""Identify the historical R-045 completion false positive by durable cause.

The diagnostic is only a selector.  A caller must still verify the entire
managed rebase journal and current Git/remote ownership before release.
"""

from __future__ import annotations

import json

from recovery_runtime import RecoveryReason
from scoped_blocking import BlockReasonCode


LEGACY_COMPLETION_DIAGNOSTIC = (
    "Manager rebase 결과의 HEAD·ref·index·등록 상태가 다릅니다."
)
REBASE_BLOCK_REASONS = frozenset({
    BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value,
    BlockReasonCode.REPOSITORY_STATE_UNSAFE.value,
    BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value,
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
    """Never release a different final cause using an old retry record."""

    if not could_be_rebase_evidence_block(block) or recovery is None:
        return False
    if saved_failure_matches(recovery, checkpoint):
        return True
    if checkpoint is None or checkpoint.operation != "rebase":
        return False
    if (recovery["pending_action"] != "final_block"
            or recovery["detail"] != LEGACY_COMPLETION_DIAGNOSTIC
            or not recovery["occurrence_index"]):
        return False
    try:
        evidence = json.loads(recovery["final_evidence"] or "null")
        occurrences = json.loads(recovery["occurrence_index"])
        history = [json.loads(key) for key in occurrences]
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(evidence, dict)
        and evidence.get("block_reason_code") == block["reason_code"]
        and isinstance(occurrences, dict)
        and any(
            isinstance(parts, list)
            and len(parts) == 3
            and parts[0] == RecoveryReason.STATUS_FAILED.value
            and parts[2] == recovery["progress_marker"]
            for parts in history
        )
        and checkpoint.expected_head and checkpoint.target_base_oid
    )


def could_be_rebase_evidence_block(block) -> bool:
    """Quarantine legacy-looking blocks even when their proof is missing."""

    return bool(
        block is not None
        and block["reason_code"] in REBASE_BLOCK_REASONS
        and block["evidence"] == LEGACY_COMPLETION_DIAGNOSTIC
    )
