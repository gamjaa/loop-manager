"""Apply a recovery decision beside the existing final blocking transaction."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import sqlite3
from typing import Callable

from loop_manager_internal.recovery.recovery_runtime import (
    RecoveryAction, RecoveryDecision, RecoveryDisposition,
    RecoveryObservation, require_final,
    RecoveryReason, decide,
)
from loop_manager_internal.storage.recovery_state import RecoveryRepository, conflict_evidence_current
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode


class RecoveryDispatcher:
    """Only final user-action decisions may reach the blocking effects."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        block_worktree: Callable[..., object],
        block_revision: Callable[..., object],
    ):
        self.connection = connection
        self.block_worktree = block_worktree
        self.block_revision = block_revision

    def apply(
        self,
        observation: RecoveryObservation,
        *,
        worktree: Path,
        repo_path: Path,
        revision: str,
        verify_result: str,
        from_revision: bool = False,
        correction_limit: int | None = None,
        attempts: int = 0,
        final_block_reason: BlockReasonCode | None = None,
        worker_result: str | None = None,
    ) -> RecoveryDecision:
        if observation.reason is RecoveryReason.MANAGED_CONFLICT_UNRECOVERABLE:
            if self.connection.in_transaction or from_revision:
                observation = replace(observation, conflict_evidence=None)
            decision = decide(observation, attempts=attempts, correction_limit=correction_limit,
                              final_block_reason=final_block_reason)
            if (decision.action is RecoveryAction.FINAL_BLOCK
                    and conflict_evidence_current(self.connection, observation.repo, observation.plan_id,
                                                  observation.conflict_evidence)):
                # The block owner observes Git first, then stores this assessment
                # and its block in the same short transaction.
                options = {'reason': decision.block_reason, 'recovery_decision': decision}
                if worker_result is not None:
                    options['worker_result'] = worker_result
                result = self.block_worktree(observation.repo, worktree, observation.detail,
                                             verify_result, **options)
                return result if isinstance(result, RecoveryDecision) else decision
        with runtime_state._transaction(self.connection):
            decision = RecoveryRepository(self.connection).observe(
                observation, attempts=attempts,
                correction_limit=correction_limit,
                final_block_reason=final_block_reason,
            ).decision
            if decision.action is RecoveryAction.FINAL_BLOCK:
                require_final(decision)
                if from_revision:
                    self.block_revision(
                        repo_path, observation.repo, revision,
                        observation.detail, verify_result,
                        reason=decision.block_reason,
                        recovery_decision=decision,
                    )
                else:
                    block_kwargs = {
                        "reason": decision.block_reason,
                        "recovery_decision": decision,
                    }
                    if worker_result is not None:
                        block_kwargs["worker_result"] = worker_result
                    self.block_worktree(
                        observation.repo, worktree,
                        observation.detail, verify_result, **block_kwargs,
                    )
            else:
                assert decision.disposition is not RecoveryDisposition.USER_ACTION_REQUIRED
            return decision
