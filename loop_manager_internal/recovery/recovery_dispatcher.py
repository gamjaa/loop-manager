"""Apply a recovery decision beside the existing final blocking transaction."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Callable

from loop_manager_internal.recovery.recovery_runtime import (
    RecoveryAction, RecoveryDecision, RecoveryDisposition,
    RecoveryObservation, require_final,
)
from loop_manager_internal.storage.recovery_state import RecoveryRepository
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
