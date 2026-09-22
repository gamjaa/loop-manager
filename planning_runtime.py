"""Typed planning lifecycle coordination independent of the CLI bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class PlanningPhase(str, Enum):
    """Ordered, independently testable planning lifecycle phases."""

    RECOVERY = "recovery"
    SELECTION = "selection"
    INPUTS = "inputs"
    WORKSPACE = "workspace"
    AGENT = "agent"
    VALIDATION = "validation"
    PUBLICATION = "publication"
    ACTIVATION = "activation"


class PlanningOutcomeKind(str, Enum):
    """Stable coordinator results interpreted by the top-level adapter."""

    EXECUTION_READY = "execution_ready"
    FINGERPRINT_SKIPPED = "fingerprint_skipped"
    PREREQUISITE_WAIT = "prerequisite_wait"
    NO_CHANGE = "no_change"
    REFACTORING = "refactoring"
    WAITING = "waiting"
    UNSAFE_REVALIDATION = "unsafe_revalidation"
    VALIDATION_RETRY = "validation_retry"
    REMOTE_BASE_RESTART = "remote_base_restart"
    REMOTE_BASE_RESTART_DEFERRED = "remote_base_restart_deferred"
    TRANSIENT_PAUSE = "transient_pause"
    USAGE_LIMIT_PAUSE = "usage_limit_pause"
    PUBLICATION_RECOVERY = "publication_recovery"
    OPERATIONAL_ERROR = "operational_error"


@dataclass(frozen=True)
class PlanningContext:
    """Prepared project identity and immutable inputs for one coordinator run."""

    repo: str
    repo_path: Path
    state_dir: Path
    base_branch: str
    base_sha: str
    provider: str
    config_path: Path
    registered_only: bool
    refactoring_review_interval: int


@dataclass(frozen=True)
class PlanningOutcome:
    """A typed next action with optional user-facing diagnostic text."""

    kind: PlanningOutcomeKind
    detail: str | None = None
    error: bool = False
    base_sha: str | None = None
    newly_activated: bool = False


class PhaseDisposition(str, Enum):
    ADVANCE = "advance"
    COMPLETE = "complete"


@dataclass(frozen=True)
class PlanningPhaseResult:
    """Port response that cannot encode control flow as a string or integer."""

    phase: PlanningPhase
    disposition: PhaseDisposition
    outcome: PlanningOutcome | None = None

    def __post_init__(self) -> None:
        if self.disposition is PhaseDisposition.ADVANCE and self.outcome is not None:
            raise ValueError("advance planning phase cannot include an outcome")
        if self.disposition is PhaseDisposition.COMPLETE and self.outcome is None:
            raise ValueError("completed planning phase requires an outcome")

    @classmethod
    def advance(cls, phase: PlanningPhase) -> "PlanningPhaseResult":
        return cls(phase, PhaseDisposition.ADVANCE)

    @classmethod
    def complete(
        cls,
        phase: PlanningPhase,
        kind: PlanningOutcomeKind,
        detail: str | None = None,
        *,
        error: bool = False,
    ) -> "PlanningPhaseResult":
        return cls(
            phase,
            PhaseDisposition.COMPLETE,
            PlanningOutcome(kind, detail, error),
        )

    @classmethod
    def finish(
        cls,
        phase: PlanningPhase,
        outcome: PlanningOutcome,
    ) -> "PlanningPhaseResult":
        return cls(phase, PhaseDisposition.COMPLETE, outcome)


class PlanningLifecyclePorts(Protocol):
    """Injected durable-state, Git, Agent, validation and publication boundary."""

    def recover_publication(
        self, context: PlanningContext
    ) -> PlanningPhaseResult: ...

    def select_planning_work(
        self, context: PlanningContext
    ) -> PlanningPhaseResult: ...

    def reconcile_inputs(
        self, context: PlanningContext
    ) -> PlanningPhaseResult: ...

    def prepare_workspace(
        self, context: PlanningContext
    ) -> PlanningPhaseResult: ...

    def run_planner_turn(
        self, context: PlanningContext
    ) -> PlanningPhaseResult: ...

    def validate_planner_result(
        self, context: PlanningContext
    ) -> PlanningPhaseResult: ...

    def publish_planner_result(
        self, context: PlanningContext
    ) -> PlanningPhaseResult: ...

    def activate_next_execplan(
        self, context: PlanningContext
    ) -> PlanningPhaseResult: ...


class PlanningLifecycleCoordinator:
    """Run planning phases while keeping process-code mapping outside the core."""

    def __init__(self, ports: PlanningLifecyclePorts):
        self._ports = ports

    @staticmethod
    def _next(
        expected: PlanningPhase,
        result: PlanningPhaseResult,
    ) -> PlanningOutcome | None:
        if result.phase is not expected:
            raise RuntimeError(
                "planning port returned the wrong phase: "
                f"{result.phase.value} (expected {expected.value})"
            )
        if result.disposition is PhaseDisposition.COMPLETE:
            assert result.outcome is not None
            return result.outcome
        return None

    def run(self, context: PlanningContext) -> PlanningOutcome:
        phases = (
            (PlanningPhase.RECOVERY, self._ports.recover_publication),
            (PlanningPhase.SELECTION, self._ports.select_planning_work),
            (PlanningPhase.INPUTS, self._ports.reconcile_inputs),
            (PlanningPhase.WORKSPACE, self._ports.prepare_workspace),
            (PlanningPhase.AGENT, self._ports.run_planner_turn),
            (PlanningPhase.VALIDATION, self._ports.validate_planner_result),
            (PlanningPhase.PUBLICATION, self._ports.publish_planner_result),
            (PlanningPhase.ACTIVATION, self._ports.activate_next_execplan),
        )
        for phase, handler in phases:
            outcome = self._next(phase, handler(context))
            if outcome is not None:
                return outcome
        raise RuntimeError("planning lifecycle ended without a typed outcome")
