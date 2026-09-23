"""ExecPlan execution phase coordination independent of process adapters."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from recovery_runtime import RecoveryAction, RecoveryDecision, RecoveryFailure


class ExecutionPhase(str, Enum):
    """Durable execution phases dispatched by the coordinator."""

    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    PUBLISHING = "PUBLISHING"


class LatestBaseCheckpoint(str, Enum):
    """Points that share the latest-base reconciliation boundary."""

    AFTER_VERIFY = "after_verify"
    BEFORE_PUBLICATION = "before_publication"


class ExecutionOutcomeKind(str, Enum):
    """Stable phase results interpreted by the process adapter."""

    SUCCESS = "success"
    PHASE_ADVANCED = "phase_advanced"
    STABLE_WAIT = "stable_wait"
    RETRY = "retry"
    TRANSIENT_PAUSE = "transient_pause"
    USAGE_PAUSE = "usage_pause"
    PLANNING_PENDING = "planning_pending"
    OPERATIONAL_ERROR = "operational_error"
    FINAL_BLOCK = "final_block"


@dataclass(frozen=True)
class ExecutionPlanMetadata:
    """Published plan identity needed throughout an execution turn."""

    plan_id: str
    title: str
    path: str
    plan_sha: str | None
    sources: tuple[str, ...] = ()
    definition_revision_sha: str | None = None
    content_hash: str | None = None
    depends_on: tuple[str, ...] = ()
    complexity: str | None = None
    risk: str | None = None
    priority: str | None = None


@dataclass(frozen=True)
class ExecutionContext:
    """Immutable execution identity, workspace, and validation markers."""

    repo: str
    repo_path: Path
    state_dir: Path
    base_branch: str
    base_sha: str
    provider: str
    config_path: Path
    plan: ExecutionPlanMetadata
    phase: ExecutionPhase
    branch: str
    worktree: Path
    active_sha: str
    validated_base_sha: str | None
    worker_session_id: str | None = None
    reviewer_session_id: str | None = None
    worker_result: str | None = None
    verify_result: str | None = None
    review_result: str | None = None
    pr_url: str | None = None
    pr_conflict_recovery: bool = False
    pr_conflict_base_oid: str | None = None
    resolving_rebase: bool = False
    archive_sources: tuple[str, ...] = ()
    archive_snapshot_token: str | None = None
    archive_candidate_scope: tuple[str, ...] = ()
    current_definition_token: str | None = None
    current_base_token: str | None = None
    worker_input_token: str | None = None
    archive_token: str | None = None
    verify_token: str | None = None
    review_token: str | None = None
    publication_token: str | None = None

    def at_phase(
        self, phase: ExecutionPhase, **changes: object
    ) -> "ExecutionContext":
        """Return the next immutable snapshot after a durable transition."""

        return replace(self, phase=phase, **changes)


@dataclass(frozen=True)
class ExecutionOutcome:
    """Typed phase result with an optional refreshed execution snapshot."""

    kind: ExecutionOutcomeKind
    context: ExecutionContext | None = None
    detail: str | None = None
    error: bool = False
    recovery: RecoveryDecision | None = None

    @classmethod
    def advanced(cls, context: ExecutionContext) -> "ExecutionOutcome":
        return cls(ExecutionOutcomeKind.PHASE_ADVANCED, context=context)


class ExecutionWorkspacePort(Protocol):
    """Managed-worktree preparation and recovery boundary."""

    def prepare(self) -> ExecutionContext | ExecutionOutcome: ...


class ExecutionRevisionPort(Protocol):
    """Reconciled definition, dependency, and stage-token safety gate."""

    def guard(self, context: ExecutionContext) -> ExecutionContext | ExecutionOutcome: ...


class ExecutionWorkerPort(Protocol):
    """Worker Agent, scope, guidance, and archive boundary."""

    def run(self, context: ExecutionContext) -> ExecutionOutcome: ...


class LatestBasePort(Protocol):
    """Shared rebase and validation-invalidation boundary."""

    def reconcile(
        self,
        context: ExecutionContext,
        checkpoint: LatestBaseCheckpoint,
    ) -> ExecutionOutcome | None: ...


class ExecutionValidationPort(Protocol):
    """Full verify and read-only Reviewer boundary."""

    def verify(
        self,
        context: ExecutionContext,
        latest_base: LatestBasePort,
    ) -> ExecutionOutcome: ...

    def review(self, context: ExecutionContext) -> ExecutionOutcome: ...


class ExecutionPublicationPort(Protocol):
    """Publication preflight, push, and GitHub boundary."""

    def publish(
        self,
        context: ExecutionContext,
        latest_base: LatestBasePort,
    ) -> ExecutionOutcome: ...


class ExecutionLifecyclePorts(
    ExecutionWorkspacePort, ExecutionRevisionPort, ExecutionWorkerPort, ExecutionValidationPort,
    ExecutionPublicationPort, LatestBasePort, Protocol,
):
    """Phase policy interface; concrete handlers receive granular effect ports."""

    def recover_failure(
        self, context: ExecutionContext | None, error: RecoveryFailure
    ) -> ExecutionOutcome: ...



class ExecutionPhaseCoordinator:
    """Prepare an execution once and dispatch exactly its current phase."""

    def __init__(self, ports: ExecutionLifecyclePorts):
        self._ports = ports

    def prepare(self) -> ExecutionContext | ExecutionOutcome:
        try:
            return self._ports.prepare()
        except RecoveryFailure as error:
            return self._recovery_outcome(self._ports.recover_failure(None, error))

    def dispatch(self, context: ExecutionContext) -> ExecutionOutcome:
        try:
            guarded = self._ports.guard(context)
            if isinstance(guarded, ExecutionOutcome):
                return self._recovery_outcome(guarded)
            context = guarded
            handlers = {
                ExecutionPhase.IMPLEMENTING: self._run_worker,
                ExecutionPhase.VERIFYING: self._run_verify,
                ExecutionPhase.REVIEWING: self._run_review,
                ExecutionPhase.PUBLISHING: self._run_publication,
            }
            return self._recovery_outcome(handlers[context.phase](context))
        except RecoveryFailure as error:
            return self._recovery_outcome(self._ports.recover_failure(context, error))

    @staticmethod
    def _recovery_outcome(outcome: ExecutionOutcome) -> ExecutionOutcome:
        """Map a typed decision at the common dispatch boundary."""

        if outcome.recovery is None:
            return outcome
        kind = {
            RecoveryAction.RETRY: ExecutionOutcomeKind.RETRY,
            RecoveryAction.RECONCILE: ExecutionOutcomeKind.RETRY,
            RecoveryAction.CORRECT: ExecutionOutcomeKind.RETRY,
            RecoveryAction.PAUSE: ExecutionOutcomeKind.TRANSIENT_PAUSE,
            RecoveryAction.FINAL_BLOCK: ExecutionOutcomeKind.FINAL_BLOCK,
        }[outcome.recovery.action]
        return replace(outcome, kind=kind, error=kind is ExecutionOutcomeKind.FINAL_BLOCK)

    def _run_worker(self, context: ExecutionContext) -> ExecutionOutcome:
        return self._ports.run(context)

    def _run_verify(self, context: ExecutionContext) -> ExecutionOutcome:
        return self._ports.verify(context, self._ports)

    def _run_review(self, context: ExecutionContext) -> ExecutionOutcome:
        return self._ports.review(context)

    def _run_publication(self, context: ExecutionContext) -> ExecutionOutcome:
        return self._ports.publish(context, self._ports)
