"""Recovery execution policy and narrow typed ports."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any, Mapping
from pathlib import Path
import hashlib
import json
from loop_manager_internal.recovery.recovery_runtime import RecoveryAction, RecoveryObservation, RecoveryReason, TRANSIENT, RECONCILE, reason_from_block
from loop_manager_internal.recovery.recovery_dispatcher import RecoveryDispatcher
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind

from loop_manager_internal.execution.execution_effects import ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
from loop_manager_internal.execution.execution_state import ExecutionStatePort

@dataclass(frozen=True)
class RecoveryHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    contract: ExecutionContract
    recovery: RecoveryDispatcher
    state: ExecutionStatePort
    _row: Callable[[], Mapping[str, object]]
    capture_git_snapshot: Callable[..., Any]


class RecoveryHandler:
    """Recovery policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: RecoveryHandlerPorts):
        self.ports = ports

    def recover_failure(self, context, error):
        return self._recover(error.reason, str(error), context=context)

    @staticmethod
    def _final(detail=None):
        return ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK, detail=detail, error=True)

    @staticmethod
    def _retry(detail=None):
        return ExecutionOutcome(ExecutionOutcomeKind.RETRY, detail=detail, error=detail is not None)

    def _recover(self, reason, detail, *, context=None, fingerprint=None,
                 progress_marker=None, final_block_reason=None,
                 from_revision=False, correction_limit=None, attempts=0,
                 user_targets=(), verify_result=None, worker_result=None,
                 worker_decision=None):
        """Persist a typed decision and apply the final gate in one transaction."""

        row = self.ports._row()
        plan_id = str(self.ports.snapshot().plan['id']) if self.ports.snapshot().plan is not None else self.ports.state.plan_id
        base = str(row['active_sha'] or self.ports.identity.base_sha)
        progress = progress_marker or '|'.join(str(row[key] or '') for key in (
            'active_sha', 'worker_session_id', 'reviewer_session_id',
            'worker_input_token', 'verify_token', 'review_token'))
        if progress_marker is None and reason is RecoveryReason.WORKER_CORRECTION:
            try:
                candidate = self.ports.capture_git_snapshot(Path(str(row['worktree'])))
            except (RuntimeError, OSError) as error:
                return self._recover(RecoveryReason.STATUS_FAILED, str(error), context=context)
            definition = self.ports.snapshot()._definition_snapshot
            # Validation logs and sessions are evidence, not candidate progress.
            progress = hashlib.sha256(json.dumps((
                base, definition.definition_token if definition is not None else row['worker_input_token'],
                candidate.head, candidate.index, candidate.dirty,
            ), sort_keys=True).encode()).hexdigest()
        fingerprint_input = (
            f"{reason.value}|{row['phase']}|{base}"
            if reason in TRANSIENT or reason in RECONCILE
            else f"{reason.value}|{detail}"
        )
        stable_fingerprint = fingerprint or hashlib.sha256(
            fingerprint_input.encode()
        ).hexdigest()
        observation = RecoveryObservation(
            reason=reason, repo=self.ports.identity.repo, plan_id=plan_id,
            phase=str(row['phase'] or ''),
            plan_revision=str(self.ports.snapshot().catalog_plan['current_revision_sha'] or '')
            if self.ports.snapshot().catalog_plan is not None else None,
            base_revision=base, fingerprint=stable_fingerprint,
            progress_marker=progress, detail=detail,
            user_targets=tuple(user_targets),
            worker_decision=worker_decision,
        )
        decision = self.ports.recovery.apply(
            observation,
            worktree=Path(str(row['worktree'] or '')),
            repo_path=self.ports.identity.repo_path,
            revision=f"refs/remotes/origin/{self.ports.identity.base}",
            verify_result=(str(row['verify_result'] or '')
                           if verify_result is None else verify_result),
            from_revision=from_revision,
            correction_limit=correction_limit,
            attempts=attempts,
            final_block_reason=final_block_reason,
            worker_result=worker_result,
        )
        kind = {
            RecoveryAction.RETRY: ExecutionOutcomeKind.RETRY,
            RecoveryAction.RECONCILE: ExecutionOutcomeKind.RETRY,
            RecoveryAction.CORRECT: ExecutionOutcomeKind.RETRY,
            RecoveryAction.PAUSE: ExecutionOutcomeKind.TRANSIENT_PAUSE,
            RecoveryAction.FINAL_BLOCK: ExecutionOutcomeKind.FINAL_BLOCK,
        }[decision.action]
        return ExecutionOutcome(kind, context=context, detail=detail,
                                error=kind is ExecutionOutcomeKind.FINAL_BLOCK,
                                recovery=decision)

    def _block_final(self, repo, worktree, detail, verify_result, *, reason,
                     recovery_reason=None, user_targets=()):
        return self._recover(recovery_reason or reason_from_block(reason), detail,
                             final_block_reason=reason,
                             user_targets=user_targets,
                             verify_result=verify_result)

    def _block_from_revision_final(self, repo_path, repo, revision, detail,
                                   verify_result='', *, reason):
        return self._recover(
            RecoveryReason.OWNERSHIP_UNCERTAIN, detail,
            final_block_reason=reason, from_revision=True,
            verify_result=verify_result,
        )

    def _step_outcome(self, step_result):
        kind = {0: ExecutionOutcomeKind.SUCCESS, 1: ExecutionOutcomeKind.FINAL_BLOCK, self.ports.contract.PROJECT_STEP_RETRY: ExecutionOutcomeKind.RETRY, self.ports.contract.PROJECT_STEP_TRANSIENT_PAUSE: ExecutionOutcomeKind.TRANSIENT_PAUSE, self.ports.contract.PROJECT_STEP_USAGE_PAUSE: ExecutionOutcomeKind.USAGE_PAUSE, self.ports.contract.PROJECT_STEP_PLANNING_PENDING: ExecutionOutcomeKind.PLANNING_PENDING}.get(
            step_result,
            ExecutionOutcomeKind.OPERATIONAL_ERROR,
        )
        return ExecutionOutcome(kind, error=kind in {
            ExecutionOutcomeKind.FINAL_BLOCK,
            ExecutionOutcomeKind.OPERATIONAL_ERROR,
        })

