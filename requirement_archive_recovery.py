"""Bounded latest-base archive reconciliation using injected external ports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from recovery_runtime import (
    RecoveryDecision, RecoveryFailure, RecoveryObservation, RecoveryReason, decide,
)
from requirement_archive import ArchiveSnapshot
from requirement_archive_state import ArchiveTargetRepository, ArchiveTargetConflict
import runtime_state


@dataclass(frozen=True)
class ArchiveRecoveryPorts:
    fetch_base: Callable[[], str]
    observe_pull_request: Callable[[], None]
    reconcile_catalog: Callable[[str], object]
    workspace_owned: Callable[[], bool]
    applied_base: Callable[[], str]
    refresh_worktree: Callable[[str], None]
    calculate_snapshot: Callable[[str], ArchiveSnapshot]
    current_base: Callable[[], str]
    execution_snapshot: Callable[[], runtime_state.ExecutionSnapshot]
    plan_revision: Callable[[], str | None] = lambda: None


@dataclass(frozen=True)
class ArchiveRecoveryResult:
    snapshot: ArchiveSnapshot | None
    decision: RecoveryDecision | None
    target_generation: int | None = None


class ArchiveRecoveryService:
    """Converge a valid catalog and owned worktree before using eligibility."""

    def __init__(self, repository: ArchiveTargetRepository,
                 ports: ArchiveRecoveryPorts, *, max_attempts: int = 3):
        self.repository = repository
        self.ports = ports
        self.max_attempts = max_attempts

    def _decision(self, context: runtime_state.ExecutionSnapshot,
                  reason: RecoveryReason, base: str | None, detail: str,
                  attempts: int, user_targets: tuple[str, ...] = ()) -> RecoveryDecision:
        observation = RecoveryObservation(
            reason=reason, repo=context.repo, plan_id=context.plan_id,
            phase=context.phase, plan_revision=self.ports.plan_revision(),
            base_revision=base,
            fingerprint=f"{reason.value}:{base or ''}",
            progress_marker=f"{context.active_sha or ''}:{context.worker_session_id or ''}",
            detail=detail, user_targets=user_targets,
        )
        return decide(
            observation, attempts=attempts,
            same_run_retry_limit=self.max_attempts,
        )

    def run(self) -> ArchiveRecoveryResult:
        context = self.ports.execution_snapshot()
        previous = self.repository.get(context.repo, context.plan_id)
        for attempt in range(1, self.max_attempts + 1):
            try:
                base = self.ports.fetch_base()
            except RecoveryFailure:
                raise
            except (RuntimeError, OSError) as error:
                return ArchiveRecoveryResult(None, self._decision(
                    context, RecoveryReason.FETCH_FAILED, None, str(error), attempt
                ))
            try:
                self.ports.observe_pull_request()
            except RecoveryFailure:
                raise
            except (RuntimeError, OSError) as error:
                return ArchiveRecoveryResult(None, self._decision(
                    context, RecoveryReason.SERVICE_UNAVAILABLE,
                    base, str(error), attempt,
                ))
            try:
                result = self.ports.reconcile_catalog(base)
            except RecoveryFailure:
                raise
            except (RuntimeError, OSError) as error:
                return ArchiveRecoveryResult(None, self._decision(
                    context, RecoveryReason.STATUS_FAILED, base,
                    str(error), attempt,
                ))
            kind = getattr(getattr(result, "kind", None), "value", None)
            if kind == "invalid-catalog":
                diagnostic = getattr(result, "diagnostic", None)
                detail = str(getattr(diagnostic, "message", None) or "원격 ExecPlan catalog가 유효하지 않습니다.")
                return ArchiveRecoveryResult(None, self._decision(
                    context, RecoveryReason.CATALOG_INVALID, base, detail, attempt,
                    tuple(path for path in (
                        getattr(diagnostic, "path", None),
                        getattr(diagnostic, "source", None),
                    ) if path),
                ))
            if kind == "stale" or getattr(result, "revision_sha", None) != base:
                continue
            if not self.ports.workspace_owned():
                return ArchiveRecoveryResult(None, self._decision(
                    context, RecoveryReason.OWNERSHIP_UNCERTAIN, base,
                    "Requirement archive 복구 worktree의 소유권을 확인할 수 없습니다.",
                    attempt))
            # The port checks the actual HEAD and rebase state. A persisted
            # target alone is never proof that rebase completed.
            if self.ports.applied_base() != base:
                self.ports.refresh_worktree(base)
            if self.ports.current_base() != base:
                continue
            snapshot = self.ports.calculate_snapshot(base)
            if snapshot.base_oid != base or self.ports.current_base() != base:
                continue
            stage = "WORKTREE_UPDATED"
            if previous is not None and previous.snapshot_token == snapshot.token:
                if previous.stage in {"CORRECTION_PENDING", "VALIDATED"}:
                    stage = previous.stage
                if previous.snapshot.move_set is not None:
                    snapshot = snapshot.with_moves(previous.snapshot.move_set)
            try:
                stored = self.repository.put(
                    context.repo, snapshot,
                    expected_execution=self.ports.execution_snapshot(),
                    base_is_current=lambda oid: self.ports.current_base() == oid,
                    snapshot_is_current=lambda: (
                        self.ports.calculate_snapshot(base).token == snapshot.token
                    ),
                    expected_generation=previous.generation if previous else None,
                    attempts=attempt,
                    stage=stage,
                )
            except ArchiveTargetConflict:
                previous = self.repository.get(context.repo, context.plan_id)
                continue
            return ArchiveRecoveryResult(snapshot, None, stored.generation)
        observed_base = self.ports.current_base()
        try:
            self.repository.pause(
                context.repo, context.plan_id, observed_base,
                expected_execution=self.ports.execution_snapshot(),
                attempts=self.max_attempts,
                expected_generation=previous.generation if previous else None,
            )
        except ArchiveTargetConflict:
            pass
        return ArchiveRecoveryResult(None, self._decision(
            context, RecoveryReason.REMOTE_SNAPSHOT_RACE,
            observed_base,
            "Requirement archive 원격 base가 제한된 재조정 동안 계속 변경됐습니다.",
            self.max_attempts))
