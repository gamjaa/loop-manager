"""Typed recovery policy shared by execution and project coordinators."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json

from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode, BlockScope, reason_scope


class RecoveryDisposition(str, Enum):
    TRANSIENT_RETRY = "transient_retry"
    STATE_RECONCILE = "state_reconcile"
    AGENT_CORRECTION = "agent_correction"
    USER_ACTION_REQUIRED = "user_action_required"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    RECONCILE = "reconcile"
    CORRECT = "correct"
    PAUSE = "pause"
    FINAL_BLOCK = "final_block"


class RecoveryReason(str, Enum):
    GIT_LOCK = "git_lock"
    TIMEOUT = "timeout"
    SERVICE_UNAVAILABLE = "service_unavailable"
    FETCH_FAILED = "fetch_failed"
    STATUS_FAILED = "status_failed"
    STAGE_FAILED = "stage_failed"
    REBASE_CONTINUE_FAILED = "rebase_continue_failed"
    WORKTREE_RECOVERY_FAILED = "worktree_recovery_failed"
    PUBLICATION_RETRY = "publication_retry"
    REMOTE_SNAPSHOT_RACE = "remote_snapshot_race"
    BASE_BEHIND = "base_behind"
    CATALOG_CACHE_STALE = "catalog_cache_stale"
    CHECKPOINT_STALE = "checkpoint_stale"
    ARCHIVE_MOVE_MISMATCH = "archive_move_mismatch"
    REBASE_CONFLICT = "rebase_conflict"
    WORKER_CORRECTION = "worker_correction"
    TARGETED_TEST_CORRECTION = "targeted_test_correction"
    TARGETED_COLLECTION_PENDING = "targeted_collection_pending"
    TARGETED_TEST_EXHAUSTED = "targeted_test_exhausted"
    REVIEW_FEEDBACK_CORRECTION = "review_feedback_correction"
    REVIEW_CORRECTION_BUDGET = "review_correction_budget"
    GUIDANCE_APPROVAL_REQUIRED = "guidance_approval_required"
    PULL_REQUEST_CLOSED = "pull_request_closed"
    OWNERSHIP_UNCERTAIN = "ownership_uncertain"
    AGENT_SCOPE_VIOLATION = "agent_scope_violation"
    REMOTE_OID_UNCERTAIN = "remote_oid_uncertain"
    REMOTE_BRANCH_DELETED = "remote_branch_deleted"
    STATE_UNSAFE = "state_unsafe"
    VERIFY_EXHAUSTED = "verify_exhausted"
    VERIFY_TIMEOUT_LIMIT_INSUFFICIENT = "verify_timeout_limit_insufficient"
    REVIEW_EXHAUSTED = "review_exhausted"
    REVIEW_PROTOCOL_EXHAUSTED = "review_protocol_exhausted"
    REVIEW_SPECIFICATION_DEADLOCK = "review_specification_deadlock"
    MANAGED_CONFLICT_UNRECOVERABLE = "managed_conflict_unrecoverable"
    WORKER_OUTPUT_EXHAUSTED = "worker_output_exhausted"
    DEFAULT_WORKTREE_MUTATION = "default_worktree_mutation"
    SHARED_REF_MUTATION = "shared_ref_mutation"
    TURN_DIFF_REF_MUTATION = "turn_diff_ref_mutation"
    OUTSIDE_MANAGED_WORKTREE_MUTATION = "outside_managed_worktree_mutation"
    SHARED_GIT_CONFIG_MUTATION = "shared_git_config_mutation"
    LEGACY_UNSCOPED_BLOCK = "legacy_unscoped_block"
    NO_CHANGE = "no_change"
    PLAN_INPUT_INVALID = "plan_input_invalid"
    CATALOG_INVALID = "catalog_invalid"
    USER_DECISION = "user_decision"


TRANSIENT = frozenset({
    RecoveryReason.GIT_LOCK, RecoveryReason.TIMEOUT,
    RecoveryReason.SERVICE_UNAVAILABLE, RecoveryReason.FETCH_FAILED,
    RecoveryReason.STATUS_FAILED, RecoveryReason.STAGE_FAILED,
    RecoveryReason.REBASE_CONTINUE_FAILED,
    RecoveryReason.WORKTREE_RECOVERY_FAILED,
    RecoveryReason.PUBLICATION_RETRY,
    RecoveryReason.REMOTE_SNAPSHOT_RACE,
    RecoveryReason.REVIEW_CORRECTION_BUDGET,
})
RECONCILE = frozenset({
    RecoveryReason.BASE_BEHIND, RecoveryReason.CATALOG_CACHE_STALE,
    RecoveryReason.CHECKPOINT_STALE,
})
CORRECTION = frozenset({
    RecoveryReason.ARCHIVE_MOVE_MISMATCH, RecoveryReason.REBASE_CONFLICT,
    RecoveryReason.WORKER_CORRECTION,
    RecoveryReason.REVIEW_FEEDBACK_CORRECTION,
})

BLOCK_REASON = {
    RecoveryReason.GUIDANCE_APPROVAL_REQUIRED: BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
    RecoveryReason.PULL_REQUEST_CLOSED: BlockReasonCode.PULL_REQUEST_CLOSED,
    RecoveryReason.OWNERSHIP_UNCERTAIN: BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
    RecoveryReason.AGENT_SCOPE_VIOLATION: BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
    RecoveryReason.REMOTE_OID_UNCERTAIN: BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
    RecoveryReason.REMOTE_BRANCH_DELETED: BlockReasonCode.REMOTE_BRANCH_DELETED,
    RecoveryReason.STATE_UNSAFE: BlockReasonCode.REPOSITORY_STATE_UNSAFE,
    RecoveryReason.VERIFY_EXHAUSTED: BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
    RecoveryReason.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT: BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
    RecoveryReason.REVIEW_EXHAUSTED: BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
    RecoveryReason.REVIEW_PROTOCOL_EXHAUSTED: BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
    RecoveryReason.REVIEW_SPECIFICATION_DEADLOCK: BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
    RecoveryReason.MANAGED_CONFLICT_UNRECOVERABLE: BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
    RecoveryReason.WORKER_OUTPUT_EXHAUSTED: BlockReasonCode.WORKER_OUTPUT_INVALID,
    RecoveryReason.TARGETED_TEST_EXHAUSTED: BlockReasonCode.WORKER_OUTPUT_INVALID,
    RecoveryReason.DEFAULT_WORKTREE_MUTATION: BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
    RecoveryReason.SHARED_REF_MUTATION: BlockReasonCode.SHARED_REF_MUTATION,
    RecoveryReason.TURN_DIFF_REF_MUTATION: BlockReasonCode.TURN_DIFF_REF_MUTATION,
    RecoveryReason.OUTSIDE_MANAGED_WORKTREE_MUTATION: BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION,
    RecoveryReason.SHARED_GIT_CONFIG_MUTATION: BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
    RecoveryReason.LEGACY_UNSCOPED_BLOCK: BlockReasonCode.LEGACY_UNSCOPED_BLOCK,
    RecoveryReason.NO_CHANGE: BlockReasonCode.PLAN_INPUT_INVALID,
    RecoveryReason.PLAN_INPUT_INVALID: BlockReasonCode.PLAN_INPUT_INVALID,
    RecoveryReason.CATALOG_INVALID: BlockReasonCode.EXECPLAN_CATALOG_INVALID,
    RecoveryReason.USER_DECISION: BlockReasonCode.USER_DECISION_REQUIRED,
    RecoveryReason.ARCHIVE_MOVE_MISMATCH: BlockReasonCode.PLAN_INPUT_INVALID,
    RecoveryReason.REBASE_CONFLICT: BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
    RecoveryReason.WORKER_CORRECTION: BlockReasonCode.WORKER_OUTPUT_INVALID,
}

RECOVERY_REASON_BY_BLOCK = {
    BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED: RecoveryReason.GUIDANCE_APPROVAL_REQUIRED,
    BlockReasonCode.PULL_REQUEST_CLOSED: RecoveryReason.PULL_REQUEST_CLOSED,
    BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN: RecoveryReason.OWNERSHIP_UNCERTAIN,
    BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION: RecoveryReason.AGENT_SCOPE_VIOLATION,
    BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN: RecoveryReason.REMOTE_OID_UNCERTAIN,
    BlockReasonCode.REMOTE_BRANCH_DELETED: RecoveryReason.REMOTE_BRANCH_DELETED,
    BlockReasonCode.REPOSITORY_STATE_UNSAFE: RecoveryReason.STATE_UNSAFE,
    BlockReasonCode.VERIFY_RETRY_EXHAUSTED: RecoveryReason.VERIFY_EXHAUSTED,
    BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT: RecoveryReason.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
    BlockReasonCode.REVIEW_RETRY_EXHAUSTED: RecoveryReason.REVIEW_EXHAUSTED,
    BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED: RecoveryReason.REVIEW_PROTOCOL_EXHAUSTED,
    BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK: RecoveryReason.REVIEW_SPECIFICATION_DEADLOCK,
    BlockReasonCode.MANAGED_WORKTREE_CONFLICT: RecoveryReason.MANAGED_CONFLICT_UNRECOVERABLE,
    BlockReasonCode.WORKER_OUTPUT_INVALID: RecoveryReason.WORKER_OUTPUT_EXHAUSTED,
    BlockReasonCode.DEFAULT_WORKTREE_MUTATION: RecoveryReason.DEFAULT_WORKTREE_MUTATION,
    BlockReasonCode.SHARED_REF_MUTATION: RecoveryReason.SHARED_REF_MUTATION,
    BlockReasonCode.TURN_DIFF_REF_MUTATION: RecoveryReason.TURN_DIFF_REF_MUTATION,
    BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION: RecoveryReason.OUTSIDE_MANAGED_WORKTREE_MUTATION,
    BlockReasonCode.SHARED_GIT_CONFIG_MUTATION: RecoveryReason.SHARED_GIT_CONFIG_MUTATION,
    BlockReasonCode.LEGACY_UNSCOPED_BLOCK: RecoveryReason.LEGACY_UNSCOPED_BLOCK,
    BlockReasonCode.USER_DECISION_REQUIRED: RecoveryReason.USER_DECISION,
    BlockReasonCode.PLAN_INPUT_INVALID: RecoveryReason.PLAN_INPUT_INVALID,
    BlockReasonCode.EXECPLAN_CATALOG_INVALID: RecoveryReason.CATALOG_INVALID,
}


def reason_from_block(reason: BlockReasonCode) -> RecoveryReason:
    """Give legacy final blocks a stable typed cause without reading prose."""

    return RECOVERY_REASON_BY_BLOCK[reason]


@dataclass(frozen=True)
class RecoveryObservation:
    reason: RecoveryReason
    repo: str
    plan_id: str | None
    phase: str
    plan_revision: str | None
    base_revision: str | None
    fingerprint: str
    progress_marker: str
    detail: str = ""
    operation: str | None = None
    user_targets: tuple[str, ...] = ()
    worker_decision: dict[str, object] | None = None
    # Notification evidence supplied by the recovery owner, never inferred
    # from the disposition or the number of exhausted attempts.
    unavailable_recovery_routes: dict[str, str] | None = None

    @property
    def identity(self) -> str:
        fields = (self.repo, self.plan_id, self.reason.value,
                  self.plan_revision, self.base_revision, self.fingerprint)
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class RecoveryDecision:
    observation: RecoveryObservation
    disposition: RecoveryDisposition
    action: RecoveryAction
    occurrence: int = 1
    attempts: int = 0
    block_reason: BlockReasonCode | None = None
    scope: BlockScope | None = None
    unresolved: str | None = None

    @property
    def identity(self) -> str:
        return self.observation.identity


class RecoveryFailure(RuntimeError):
    """Operation failure with a code independent of diagnostic prose."""

    def __init__(self, reason: RecoveryReason, detail: str):
        super().__init__(detail)
        self.reason = reason


def classify_exception(error: BaseException, *, operation: RecoveryReason) -> RecoveryReason:
    """Only explicit operation context or exception type determines recovery."""

    if isinstance(error, RecoveryFailure):
        return error.reason
    if isinstance(error, TimeoutError):
        return RecoveryReason.TIMEOUT
    if isinstance(error, (ConnectionError, BrokenPipeError)):
        return RecoveryReason.SERVICE_UNAVAILABLE
    return operation


def decide(
    observation: RecoveryObservation,
    *,
    occurrence: int = 1,
    attempts: int = 0,
    correction_limit: int | None = None,
    same_run_retry_limit: int = 3,
    final_block_reason: BlockReasonCode | None = None,
) -> RecoveryDecision:
    """Choose the next action; scope exists only for a final block."""

    reason = observation.reason
    if correction_limit is None:
        correction_limit = 4 if reason is RecoveryReason.REBASE_CONFLICT else 3
    if reason in TRANSIENT:
        disposition = RecoveryDisposition.TRANSIENT_RETRY
        action = (RecoveryAction.RETRY if attempts < same_run_retry_limit
                  else RecoveryAction.PAUSE)
    elif reason in RECONCILE:
        disposition = RecoveryDisposition.STATE_RECONCILE
        action = (RecoveryAction.RECONCILE if attempts < same_run_retry_limit
                  else RecoveryAction.PAUSE)
    elif reason is RecoveryReason.REVIEW_FEEDBACK_CORRECTION:
        disposition = RecoveryDisposition.AGENT_CORRECTION
        action = RecoveryAction.CORRECT
    elif reason is RecoveryReason.TARGETED_COLLECTION_PENDING:
        disposition = RecoveryDisposition.STATE_RECONCILE
        action = RecoveryAction.PAUSE
    elif reason is RecoveryReason.TARGETED_TEST_CORRECTION:
        disposition = RecoveryDisposition.AGENT_CORRECTION
        action = RecoveryAction.PAUSE if attempts else RecoveryAction.CORRECT
    elif reason in CORRECTION and occurrence < correction_limit:
        disposition = RecoveryDisposition.AGENT_CORRECTION
        action = RecoveryAction.CORRECT
    else:
        disposition = RecoveryDisposition.USER_ACTION_REQUIRED
        action = RecoveryAction.FINAL_BLOCK
    block_reason = (final_block_reason or BLOCK_REASON.get(reason, BlockReasonCode.REPOSITORY_STATE_UNSAFE)) if action is RecoveryAction.FINAL_BLOCK else None
    return RecoveryDecision(
        observation, disposition, action, occurrence, attempts, block_reason,
        reason_scope(block_reason) if block_reason is not None else None,
        observation.detail if action is RecoveryAction.FINAL_BLOCK else None,
    )


def require_final(decision: RecoveryDecision) -> RecoveryDecision:
    if decision.disposition is not RecoveryDisposition.USER_ACTION_REQUIRED or decision.action is not RecoveryAction.FINAL_BLOCK or decision.block_reason is None:
        raise ValueError("final blocking requires USER_ACTION_REQUIRED")
    return decision
