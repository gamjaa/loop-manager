"""Planning lifecycle state machine independent of the CLI bootstrap."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from planning_catalog import MutableReadySnapshot

from maintenance_orchestration import (
    MaintenanceOrchestrationError,
    assessment_can_yield,
    MaintenanceOutcome,
    MaintenancePlannerDecision,
    maintenance_planning_fingerprint,
    parse_maintenance_planner_decision,
    validate_prerequisite_plan_ids,
)
from maintenance_planning import (
    MaintenancePlanningContext,
    MaintenancePlanningError,
    MaintenancePlanningRestart,
)
import maintenance_state
import maintenance_file_ledger
import maintenance_file_review
import maintenance_reconciliation
import maintenance_findings
from maintenance_file_inventory import SourceInventory, SourceInventoryError
import full_assessment
import runtime_state


MAX_PLANNER_VALIDATION_RETRIES = 3
WORKSPACE_OWNED = 1
WORKSPACE_RESERVED = 2


class PlanningPhase(str, Enum):
    """Ordered planning lifecycle phases owned by the coordinator."""

    RECOVERY = "recovery"
    SELECTION = "selection"
    INPUTS = "inputs"
    WORKSPACE = "workspace"
    AGENT = "agent"
    VALIDATION = "validation"
    PUBLICATION = "publication"
    ACTIVATION = "activation"


class PlanningOutcomeKind(str, Enum):
    """Stable coordinator results interpreted by the process adapter."""

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


class PlanningAgentResultKind(str, Enum):
    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient_failure"
    USAGE_LIMIT = "usage_limit"
    PERMANENT_CONFIGURATION = "permanent_configuration"


class PlanningDecisionKind(str, Enum):
    """One safe-boundary scheduling choice, before effects are performed."""

    RESUME_ACTIVE = "resume_active"
    RUN_SPECIFICATION_PLANNING = "run_specification_planning"
    ACTIVATE_READY = "activate_ready"
    RUN_FALLBACK_PLANNING = "run_fallback_planning"
    STABLE_WAIT = "stable_wait"
    COMPLETE = "complete"


@dataclass(frozen=True)
class PlanningDecision:
    kind: PlanningDecisionKind


def choose_planning_decision(
    *,
    has_active: bool,
    specification_changed: bool,
    runnable_ready: bool,
    planning_input_changed: bool,
    has_waiting_catalog: bool,
) -> PlanningDecision:
    """Choose active work, changed planning input, or READY at a safe boundary."""

    if has_active:
        kind = PlanningDecisionKind.RESUME_ACTIVE
    elif specification_changed:
        kind = PlanningDecisionKind.RUN_SPECIFICATION_PLANNING
    elif planning_input_changed:
        kind = PlanningDecisionKind.RUN_FALLBACK_PLANNING
    elif runnable_ready:
        kind = PlanningDecisionKind.ACTIVATE_READY
    elif has_waiting_catalog:
        kind = PlanningDecisionKind.STABLE_WAIT
    else:
        kind = PlanningDecisionKind.COMPLETE
    return PlanningDecision(kind)


class PlanningResultInvalid(RuntimeError):
    """A Planner result that may be repaired in the same Agent session."""


class PlanningWorkspaceOwnershipError(RuntimeError):
    """A planning worktree cannot be proven to belong to this lifecycle."""


@dataclass(frozen=True)
class PlanningContext:
    """Prepared project identity and immutable inputs for one lifecycle run."""

    repo: str
    repo_path: Path
    state_dir: Path
    base_branch: str
    base_sha: str
    provider: str
    config_path: Path
    registered_only: bool
    refactoring_review_interval: int
    self_repository: bool = False


@dataclass(frozen=True)
class PlanningOutcome:
    """A typed next action with process details kept out of the coordinator."""

    kind: PlanningOutcomeKind
    detail: str | None = None
    error: bool = False
    base_sha: str | None = None
    newly_activated: bool = False


@dataclass(frozen=True)
class PlanningAgentResult:
    code: int
    message: str
    session_id: str | None
    kind: PlanningAgentResultKind


@dataclass(frozen=True)
class PlanningScopeResult:
    error: str | None = None
    result_snapshot: str | None = None


class PlanningStatePort(Protocol):
    """Durable planning and maintenance repository boundary."""

    def planning(self, repo: str) -> runtime_state.PlanningSnapshot: ...

    def maintenance_cycle(self, repo: str) -> maintenance_state.MaintenanceCycle: ...

    def update(self, repo: str, **values: object) -> None: ...

    def transaction(self) -> AbstractContextManager[object]: ...

    def advance_maintenance_cycle(
        self, repo: str, base_sha: str
    ) -> maintenance_state.MaintenanceCycle: ...

    def schedule(self, repo: str) -> Any: ...

    def pending_document_approval(
        self, repo: str, repository: Path, base_branch: str,
    ) -> bool: ...

    def maintenance_due(
        self, repo: str, interval: int, *, completion_candidate: bool = False,
        self_repository: bool = False,
    ) -> Any: ...

    def begin_maintenance_assessment(
        self,
        repo: str,
        interval: int,
        base_sha: str,
        *,
        include_general_planning: bool,
        self_repository: bool = False,
        completion_candidate: bool = False,
    ) -> tuple[maintenance_state.MaintenanceCycle, bool]: ...

    def settle_empty_stabilization_assessment(
        self, repo: str, interval: int, *, source_sha: str,
        repository_path: str, base_branch: str, inventory: SourceInventory,
    ) -> maintenance_state.MaintenanceCycle: ...

    def record_full_assessment(
        self, repo: str, *, expected_revision: int, source_sha: str, result: str
    ) -> maintenance_state.MaintenanceCycle: ...

    def restart_stale_maintenance(
        self,
        repo: str,
        expected: maintenance_state.MaintenanceCycle,
        base_sha: str,
    ) -> maintenance_state.MaintenanceCycle: ...

    def activate_ready(self, repo: str, base_sha: str) -> Any | None: ...

    def preserve_diagnostics(
        self, repo: str, snapshot: runtime_state.PlanningSnapshot
    ) -> None: ...

    def clear_active(
        self,
        repo: str,
        *,
        processed_sha: object = None,
        specification_fingerprint: object = None,
        planning_fingerprint: object = None,
    ) -> None: ...

    def clear_active_in_transaction(
        self,
        repo: str,
        *,
        processed_sha: object = None,
        specification_fingerprint: object = None,
        planning_fingerprint: object = None,
    ) -> None: ...

    def clear_validation(self, repo: str) -> None: ...

    def compact_catalog(self, repo: str) -> str: ...

    def execplan_rows(self, repo: str) -> Sequence[Any]: ...

    def transition_maintenance(
        self,
        repo: str,
        *,
        expected_phase: maintenance_state.MaintenancePhase,
        expected_revision: int,
        next_phase: maintenance_state.MaintenancePhase,
        prerequisite_plan_ids: Sequence[str],
        include_general_planning: bool,
    ) -> maintenance_state.MaintenanceCycle: ...

    def advance_success_baseline(
        self,
        repo: str,
        *,
        expected_phase: maintenance_state.MaintenancePhase,
        expected_revision: int,
        remote_commit: str,
    ) -> maintenance_state.MaintenanceCycle: ...

    def file_assessment_evidence(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str,
    ) -> Any: ...

    def complete_file_assessment(
        self, repo: str, repository: Path, base_branch: str,
        cycle_revision: int, source_sha: str, evidence: Any,
    ) -> None: ...

    def recover_maintenance_batch(
        self, repo: str, worktree: Path, base: str
    ) -> maintenance_state.MaintenanceCycle: ...

    def record_maintenance_intent(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: Any,
        plan_commit: str,
    ) -> maintenance_state.MaintenanceCycle: ...

    def publish_maintenance_batch(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: Any,
        plan_commit: str,
    ) -> maintenance_state.MaintenanceCycle: ...

    def register_execplans(
        self,
        repo: str,
        worktree: Path,
        plans: Sequence[Any],
        plan_commit: str,
        fingerprint: str,
    ) -> None: ...

    def general_publication(self, repo: str) -> Any | None: ...

    def recover_general_batch(
        self, repo: str, worktree: Path, base: str
    ) -> str: ...

    def record_general_intent(
        self,
        repo: str,
        worktree: Path,
        validated_result: Any,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> Any: ...

    def publish_general_batch(
        self,
        repo: str,
        worktree: Path,
        base: str,
        validated_result: Any,
        plan_commit: str,
        specification_fingerprint: str,
        planning_fingerprint: str,
    ) -> None: ...


class PlanningGitPort(Protocol):
    """Git and managed planning-worktree boundary."""

    def run(
        self, repo: Path, *arguments: str, check: bool = True
    ) -> Any: ...

    def input_fingerprint(self, repo_path: Path, sha: str) -> str: ...

    def specification_fingerprint(self, repo_path: Path, sha: str) -> str: ...

    def requirement_archive_only(
        self, repo_path: Path, old_sha: str, new_sha: str
    ) -> bool: ...

    def validate_workspace_identity(
        self, repo: str, fingerprint: str, worktree: Path
    ) -> None: ...

    def validate_worktree_ownership(
        self, repo: str, fingerprint: str, worktree: Path
    ) -> None: ...

    def make_planning_worktree(
        self,
        repo_path: Path,
        worktree: Path,
        base: str,
        *,
        allow_create: bool,
        allow_existing: bool,
        ownership_reserved: bool,
        expected_head: str,
        before_create: Callable[[], None],
    ) -> None: ...

    def changed_files(
        self, worktree: Path, since: str | None = None
    ) -> list[str]: ...

    def validate_managed_worktree(self, worktree: Path) -> None: ...


class PlanningAgentPort(Protocol):
    """Planner CLI and Agent scope boundary."""

    def begin_scope(self, role: str, worktree: Path) -> Any: ...

    def run_planner(
        self,
        worktree: Path,
        prompt: str,
        session_id: str | None,
        on_session_started: Callable[[str], None],
    ) -> PlanningAgentResult: ...

    def finish_scope(
        self, role: str, worktree: Path, before: Any
    ) -> PlanningScopeResult: ...

    def validate_saved_scope(
        self, role: str, worktree: Path, snapshot: str
    ) -> PlanningScopeResult: ...

    def clear_scope(self, repo: str) -> None: ...


class PlanningValidationPort(Protocol):
    """Prompt, pure validation and maintenance context boundary."""

    def maintenance_context_at_revision(
        self,
        repo: str,
        worktree: Path,
        active_sha: str,
        cycle: maintenance_state.MaintenanceCycle,
    ) -> MaintenancePlanningContext: ...

    def prepare_maintenance_context(
        self, repo: str, worktree: Path, active_sha: str
    ) -> MaintenancePlanningContext: ...

    def maintenance_change_context(
        self,
        repo_path: Path,
        base: str,
        cycle: maintenance_state.MaintenanceCycle,
    ) -> str: ...

    def prepare_general_context(
        self, repo: str, worktree: Path, active_sha: str
    ) -> MutableReadySnapshot: ...

    def planner_prompt(
        self,
        error: str | None,
        catalog: str,
        mutable_snapshot: MutableReadySnapshot,
    ) -> str: ...

    def maintenance_prompt(
        self,
        context: MaintenancePlanningContext,
        catalog: str,
        change_context: str,
        error: str | None,
        *,
        include_general_planning: bool,
    ) -> str: ...

    def validate_general_result(
        self,
        repo: str,
        worktree: Path,
        active_sha: str,
        dirty_files: Sequence[str],
        mutable_snapshot: MutableReadySnapshot,
    ) -> Any: ...

    def validate_maintenance_result(
        self,
        context: MaintenancePlanningContext,
        worktree: Path,
        dirty_files: Sequence[str],
        *,
        refactoring_plan_ids: Sequence[str],
        allow_general_plans: bool,
    ) -> Any: ...

    def validate_publication_base(
        self,
        repo: str,
        repo_path: Path,
        base: str,
        context: MaintenancePlanningContext,
    ) -> None: ...


class PlanningEventPort(Protocol):
    """Clock-backed operational log boundary."""

    def log(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...


class PlanningLifecyclePorts(Protocol):
    state: PlanningStatePort
    git: PlanningGitPort
    agent: PlanningAgentPort
    validation: PlanningValidationPort
    events: PlanningEventPort
    file_review: Any


@dataclass
class _PlanningRun:
    context: PlanningContext
    row: runtime_state.PlanningSnapshot
    maintenance_cycle: maintenance_state.MaintenanceCycle
    base_sha: str
    maintenance_planning: bool
    ready_activated_before_planning: bool = False
    general_fingerprint: str | None = None
    specification_fingerprint: str | None = None
    current_fingerprint: str | None = None
    selection: PlanningDecision | None = None
    fingerprint_skipped: bool = False
    published_refactoring: bool = False
    planning_worktree: Path | None = None
    maintenance_context: MaintenancePlanningContext | None = None
    mutable_ready_snapshot: MutableReadySnapshot | None = None
    maintenance_change_context: str = ""
    dirty_files: list[str] | None = None
    planner_scope_error: str | None = None
    maintenance_decision: MaintenancePlannerDecision | None = None
    validated_result: Any = None
    error_input_changed: bool = False
    publication_recovery_verified: bool = False


class PlanningLifecycleCoordinator:
    """Own planning transitions while delegating effects to explicit ports."""

    def __init__(self, ports: PlanningLifecyclePorts):
        self._ports = ports

    @staticmethod
    def _operational_error() -> PlanningOutcome:
        return PlanningOutcome(PlanningOutcomeKind.OPERATIONAL_ERROR, error=True)

    @staticmethod
    def _file_assessment_active(run: _PlanningRun) -> bool:
        cycle = run.maintenance_cycle
        return bool(
            run.maintenance_planning
            and cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
            and cycle.file_assessment_start_merged_count is not None
            and (not run.context.self_repository or cycle.full_assessment_completed)
        )

    def _new_run(self, context: PlanningContext) -> _PlanningRun:
        row = self._ports.state.planning(context.repo)
        cycle = self._ports.state.maintenance_cycle(context.repo)
        maintenance_planning = bool(
            row.phase == "PLANNING"
            and cycle.phase
            in {
                maintenance_state.MaintenancePhase.ASSESSMENT,
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }
        )
        return _PlanningRun(
            context=context,
            row=row,
            maintenance_cycle=cycle,
            base_sha=context.base_sha,
            maintenance_planning=maintenance_planning,
        )

    def run(self, context: PlanningContext) -> PlanningOutcome:
        run = self._new_run(context)
        handlers = (
            self._recover_publication,
            self._gate_preserved_error,
            self._select_planning_work,
            self._reconcile_inputs,
            self._prepare_workspace,
            self._run_planner_turn,
            self._validate_planner_result,
            self._publish_planner_result,
            self._activate_next_execplan,
        )
        for handler in handlers:
            outcome = handler(run)
            if outcome is not None:
                return outcome
        raise RuntimeError("planning lifecycle ended without a typed outcome")

    def _gate_preserved_error(self, run: _PlanningRun) -> PlanningOutcome | None:
        """Check the failed input before scheduling or stale-workspace cleanup."""

        row = run.row
        if not row.planning_error or run.publication_recovery_verified:
            return None
        cycle = run.maintenance_cycle
        related_source_changed = False
        if str(row.planning_error_fingerprint or "").startswith("maintenance-cycle:"):
            current = f"maintenance-cycle:{cycle.phase.value}:{cycle.revision}"
        else:
            run.general_fingerprint = self._ports.git.input_fingerprint(
                run.context.repo_path, run.base_sha
            )
            maintenance_error = cycle.phase in {
                maintenance_state.MaintenancePhase.ASSESSMENT,
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            } and (run.maintenance_planning or row.phase is None)
            current = (
                maintenance_planning_fingerprint(cycle, run.general_fingerprint)
                if maintenance_error
                else run.general_fingerprint
            )
            if (
                maintenance_error
                and not cycle.include_general_planning
                and cycle.cycle_base_sha
                and cycle.cycle_base_sha != run.base_sha
            ):
                related_source_changed = (
                    self._ports.git.input_fingerprint(
                        run.context.repo_path, cycle.cycle_base_sha
                    ) != run.general_fingerprint
                )
        if not row.planning_error_fingerprint or (
            row.planning_error_fingerprint == current
            and not related_source_changed
        ):
            self._ports.events.error(
                f"계획 오류가 해결되지 않았습니다: {row.planning_error}\n"
                f"보존된 planning worktree: {row.worktree}"
            )
            return self._operational_error()
        run.error_input_changed = True
        return None

    def _recover_publication(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        context = run.context
        cycle = run.maintenance_cycle
        row = run.row
        general_intent = self._ports.state.general_publication(context.repo)
        if general_intent is not None and row.phase == "PLANNING":
            worktree = Path(str(row.worktree or ""))
            fingerprint = str(row.planning_attempt_fingerprint or "")
            try:
                self._ports.git.validate_worktree_ownership(
                    context.repo, fingerprint, worktree
                )
                self._ports.git.run(context.repo_path, "fetch", "origin")
                with self._ports.state.transaction():
                    recovery = self._ports.state.recover_general_batch(
                        context.repo, worktree, context.base_branch
                    )
                    if recovery != "pending" and worktree.exists():
                        self._ports.git.run(
                            context.repo_path, "worktree", "remove", str(worktree)
                        )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=row.planning_error or message,
                    planning_error_fingerprint=(
                        row.planning_error_fingerprint or fingerprint
                    ),
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            if recovery == "pending":
                run.publication_recovery_verified = True
                return None
            self._ports.events.info(
                "게시된 일반 planning batch를 복구했습니다."
                if recovery == "applied"
                else "원격 변경에 맞춰 일반 planning batch를 다시 시작합니다."
            )
            return PlanningOutcome(PlanningOutcomeKind.PUBLICATION_RECOVERY)
        if not (
            run.maintenance_planning
            and cycle.publication_stage == "prepared"
            and row.phase == "PLANNING"
        ):
            return None
        worktree = Path(str(row.worktree or ""))
        fingerprint = str(row.planning_attempt_fingerprint or "")
        try:
            self._ports.git.validate_worktree_ownership(
                context.repo, fingerprint, worktree
            )
            self._ports.git.run(context.repo_path, "fetch", "origin")
            remote_sha = self._ports.git.run(
                context.repo_path,
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{context.base_branch}^{{commit}}",
            ).stdout.strip()
            run.base_sha = remote_sha
            recovered_cycle = None
            if remote_sha != cycle.publication_base_sha:
                with self._ports.state.transaction():
                    recovered_cycle = self._ports.state.recover_maintenance_batch(
                        context.repo, worktree, context.base_branch
                    )
                    if worktree.exists():
                        self._ports.git.run(
                            context.repo_path, "worktree", "remove", str(worktree)
                        )
        except RuntimeError as error:
            message = str(error)
            self._ports.state.update(
                context.repo,
                planning_error=row.planning_error or message,
                planning_error_fingerprint=(
                    row.planning_error_fingerprint or fingerprint
                ),
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
        if recovered_cycle is None:
            run.publication_recovery_verified = True
            return None
        self._ports.events.info(
            "게시된 유지보수 batch를 fingerprint 정리 전에 복구했습니다."
            if recovered_cycle.publication_stage == "remote_confirmed"
            else "원격 변경에 맞춰 미게시 유지보수 batch를 다시 시작합니다."
        )
        return PlanningOutcome(PlanningOutcomeKind.PUBLICATION_RECOVERY)

    def _select_planning_work(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        context = run.context
        row = run.row
        cycle = run.maintenance_cycle
        if (
            row.phase == "PLANNING"
            and assessment_can_yield(cycle)
            and not row.planning_error
            and not row.planning_validation_error
            and self._ports.state.general_publication(context.repo) is None
        ):
            decision = self._ports.state.schedule(context.repo)
            if (decision.runnable_ready is not None
                and getattr(decision, "active", None) is None
                and not getattr(decision, "project_blocked", False)):
                worktree = Path(str(row.worktree or ""))
                self._ports.git.validate_worktree_ownership(
                    context.repo, str(row.planning_attempt_fingerprint or ""), worktree,
                )
                clean = not self._ports.git.run(worktree, "status", "--porcelain").stdout.strip()
                head = self._ports.git.run(worktree, "rev-parse", "HEAD").stdout.strip()
                if clean and head == row.active_sha:
                    self._ports.state.preserve_diagnostics(context.repo, row)
                    with self._ports.state.transaction():
                        if worktree.exists():
                            self._ports.git.run(context.repo_path, "worktree", "remove", str(worktree))
                        self._ports.state.clear_active_in_transaction(
                            context.repo,
                            processed_sha=row.processed_sha,
                            specification_fingerprint=row.specification_fingerprint,
                            planning_fingerprint=row.planning_fingerprint,
                        )
                    return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)
        if row.phase:
            return None
        try:
            cycle = self._ports.state.advance_maintenance_cycle(
                context.repo, run.base_sha
            )
        except MaintenancePlanningError as error:
            message = str(error)
            fingerprint = f"maintenance-cycle:{cycle.phase.value}:{cycle.revision}"
            self._ports.state.update(
                context.repo,
                planning_error=message,
                planning_error_fingerprint=fingerprint,
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
        if run.error_input_changed:
            run.maintenance_planning = cycle.phase in {
                maintenance_state.MaintenancePhase.ASSESSMENT,
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }
            run.row = row
            run.maintenance_cycle = cycle
            return None
        decision = self._ports.state.schedule(context.repo)
        approval_pending = (
            context.self_repository
            and cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and self._ports.state.pending_document_approval(
                context.repo, context.repo_path, context.base_branch,
            )
        )
        if (
            context.registered_only
            and cycle.phase is maintenance_state.MaintenancePhase.IDLE
            and not decision.has_waiting
            and not approval_pending
        ):
            completion_candidate = bool(
                not decision.has_ready and not decision.has_waiting
                and getattr(decision, "active", None) is None
                and not getattr(decision, "project_blocked", False)
                and row.planning_fingerprint
            )
            if completion_candidate:
                run.general_fingerprint = self._ports.git.input_fingerprint(
                    context.repo_path, run.base_sha,
                )
                run.specification_fingerprint = self._ports.git.specification_fingerprint(
                    context.repo_path, run.base_sha,
                )
                completion_candidate = not (
                    self._general_planning_required(
                        row, context, run.base_sha, run.general_fingerprint,
                    ) or self._specification_planning_required(
                        row, context, run.base_sha,
                        run.specification_fingerprint, run.general_fingerprint,
                    )
                )
            due = self._ports.state.maintenance_due(
                context.repo, context.refactoring_review_interval,
                self_repository=context.self_repository,
                completion_candidate=completion_candidate,
            )
            if due.reason == "stabilization_due" and hasattr(
                self._ports, "file_review"
            ):
                try:
                    empty_inventory = self._ports.file_review.empty_selection_inventory(
                        context.repo, context.repo_path, context.base_branch,
                        run.base_sha,
                    )
                except (
                    SourceInventoryError,
                    maintenance_file_ledger.FileReviewLedgerError,
                ) as error:
                    self._ports.events.error(str(error))
                    return PlanningOutcome(
                        PlanningOutcomeKind.REMOTE_BASE_RESTART,
                        detail=str(error),
                        error=True,
                    )
                if empty_inventory is not None:
                    try:
                        cycle = self._ports.state.settle_empty_stabilization_assessment(
                            context.repo,
                            context.refactoring_review_interval,
                            source_sha=run.base_sha,
                            repository_path=str(context.repo_path),
                            base_branch=context.base_branch,
                            inventory=empty_inventory,
                        )
                    except (
                        maintenance_state.MaintenanceStateError,
                        maintenance_file_ledger.FileReviewLedgerError,
                        SourceInventoryError,
                    ) as error:
                        self._ports.events.error(str(error))
                        return PlanningOutcome(
                            PlanningOutcomeKind.REMOTE_BASE_RESTART,
                            detail=str(error),
                            error=True,
                        )
                    due = maintenance_state.ReviewDue(
                        False, "no_selected_files", due.merged_since_baseline,
                        cycle.phase,
                    )
            if due.required:
                include_general = False
                if not decision.has_ready:
                    run.general_fingerprint = self._ports.git.input_fingerprint(
                        context.repo_path, run.base_sha
                    )
                    include_general = self._general_planning_required(
                        row, context, run.base_sha, run.general_fingerprint
                    )
                cycle, _started = self._ports.state.begin_maintenance_assessment(
                    context.repo,
                    context.refactoring_review_interval,
                    run.base_sha,
                    include_general_planning=include_general,
                    self_repository=context.self_repository,
                    completion_candidate=completion_candidate,
                )
        if assessment_can_yield(cycle):
            decision = self._ports.state.schedule(context.repo)
            if (
                decision.runnable_ready is not None
                and getattr(decision, "active", None) is None
                and not getattr(decision, "project_blocked", False)
            ):
                latest_sha = self._fetch_remote_head(context)
                if latest_sha != run.base_sha:
                    return self._restart_for_new_input()
                selected = self._ports.state.activate_ready(context.repo, latest_sha)
                if selected is None:
                    return PlanningOutcome(PlanningOutcomeKind.WAITING)
                self._ports.events.info("기존 READY 실행을 재개하고 미게시 유지보수 평가를 보류합니다.")
                return PlanningOutcome(PlanningOutcomeKind.EXECUTION_READY)
        if (
            decision.has_waiting
            and cycle.phase
            in {
                maintenance_state.MaintenancePhase.ASSESSMENT,
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }
        ):
            self._ports.events.info(
                "PLAN_BLOCKED 또는 PR_OPEN 종료 전 유지보수 평가를 보류합니다."
            )
            return PlanningOutcome(PlanningOutcomeKind.WAITING)
        if cycle.phase in {
            maintenance_state.MaintenancePhase.ASSESSMENT,
            maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        }:
            run.maintenance_planning = True
        elif cycle.phase in {
            maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
            maintenance_state.MaintenancePhase.REFACTORING,
        }:
            decision = self._ports.state.schedule(context.repo)
            if decision.runnable_ready is None:
                self._ports.events.info(
                    "활성 유지보수 주기의 dependency 충족을 기다립니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            run.base_sha = self._fetch_remote_head(context)
            self._ports.state.activate_ready(context.repo, run.base_sha)
            row = self._ports.state.planning(context.repo)
            run.ready_activated_before_planning = True
        else:
            decision = self._ports.state.schedule(context.repo)
            if getattr(decision, "project_blocked", False):
                self._ports.events.info(
                    "PROJECT_BLOCKED 해제 전에는 새 Planner나 Worker를 시작하지 않습니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            run.general_fingerprint = self._ports.git.input_fingerprint(
                context.repo_path, run.base_sha
            )
            run.specification_fingerprint = (
                self._ports.git.specification_fingerprint(
                    context.repo_path, run.base_sha
                )
            )
            specification_changed = self._specification_planning_required(
                row,
                context,
                run.base_sha,
                run.specification_fingerprint,
                run.general_fingerprint,
            )
            planning_input_changed = self._general_planning_required(
                row,
                context,
                run.base_sha,
                run.general_fingerprint,
            )
            run.selection = choose_planning_decision(
                has_active=getattr(decision, "active", None) is not None,
                specification_changed=specification_changed or approval_pending,
                runnable_ready=decision.runnable_ready is not None,
                planning_input_changed=planning_input_changed,
                has_waiting_catalog=bool(
                    decision.has_ready or decision.has_waiting
                ),
            )
            if run.selection.kind is PlanningDecisionKind.ACTIVATE_READY:
                latest_sha = self._fetch_remote_head(context)
                if self._remote_base_changed_before_activation(run, latest_sha):
                    return self._restart_for_new_input()
                run.base_sha = latest_sha
                self._ports.state.activate_ready(context.repo, run.base_sha)
                row = self._ports.state.planning(context.repo)
                run.ready_activated_before_planning = True
            elif run.selection.kind is PlanningDecisionKind.STABLE_WAIT:
                self._ports.events.info(
                    "계획 입력이 안정적이며 실행 가능한 READY가 없어 대기합니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
        run.row = row
        run.maintenance_cycle = cycle
        return None

    def _general_planning_required(
        self,
        row: runtime_state.PlanningSnapshot,
        context: PlanningContext,
        base_sha: str,
        current_fingerprint: str,
    ) -> bool:
        if row.planning_fingerprint == current_fingerprint:
            return False
        if row.planning_fingerprint and row.processed_sha:
            processed = self._ports.git.input_fingerprint(
                context.repo_path, row.processed_sha
            )
            if processed == current_fingerprint:
                return False
            if self._ports.git.requirement_archive_only(
                context.repo_path, row.processed_sha, base_sha
            ):
                return False
        return True

    def _specification_planning_required(
        self,
        row: runtime_state.PlanningSnapshot,
        context: PlanningContext,
        base_sha: str,
        current_fingerprint: str,
        current_general_fingerprint: str,
    ) -> bool:
        baseline = row.specification_fingerprint
        if baseline is None and row.planning_fingerprint == current_general_fingerprint:
            # Existing databases already have a successful full-input marker.
            # Treat an identical full input as the corresponding specification
            # baseline and persist the explicit value at the next success.
            return False
        if baseline is None and row.planning_fingerprint and row.processed_sha:
            baseline = self._ports.git.specification_fingerprint(
                context.repo_path, row.processed_sha
            )
        if baseline == current_fingerprint:
            return False
        if row.processed_sha:
            processed = self._ports.git.specification_fingerprint(
                context.repo_path, row.processed_sha
            )
            if processed == current_fingerprint:
                return False
            if self._ports.git.requirement_archive_only(
                context.repo_path, row.processed_sha, base_sha
            ):
                return False
        return True

    def _remote_base_changed_before_activation(
        self, run: _PlanningRun, latest_sha: str
    ) -> bool:
        # Even a plan-only revision can reorder READY declarations. Re-enter
        # the project step so catalog reconciliation reads the new revision.
        return not run.maintenance_planning and latest_sha != run.base_sha

    def _restart_for_new_input(self) -> PlanningOutcome:
        self._ports.events.info(
            "READY 시작 전 원격 기준이 변경되어 최신 catalog와 계획 입력을 다시 선택합니다."
        )
        return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)

    def _reconcile_inputs(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        context = run.context
        row = run.row
        cycle = run.maintenance_cycle
        if (
            run.maintenance_planning
            and cycle.cycle_base_sha != run.base_sha
            and cycle.publication_stage is None
            and row.phase == "PLANNING"
            and not row.planning_error
        ):
            stale_worktree = Path(str(row.worktree or ""))
            stored_fingerprint = str(row.planning_attempt_fingerprint or "")
            self._ports.git.validate_worktree_ownership(
                context.repo, stored_fingerprint, stale_worktree
            )
            self._ports.state.preserve_diagnostics(context.repo, row)
            with self._ports.state.transaction():
                cycle = self._ports.state.restart_stale_maintenance(
                    context.repo, cycle, run.base_sha
                )
                self._ports.state.clear_active_in_transaction(context.repo)
            if stale_worktree.exists():
                self._ports.git.run(
                    context.repo_path,
                    "worktree",
                    "remove",
                    "--force",
                    str(stale_worktree),
                )
            row = self._ports.state.planning(context.repo)

        fingerprint_required = bool(
            run.maintenance_planning or not row.phase or row.phase == "PLANNING"
        )
        if fingerprint_required and run.general_fingerprint is None:
            run.general_fingerprint = self._ports.git.input_fingerprint(
                context.repo_path, run.base_sha
            )
        if fingerprint_required and run.specification_fingerprint is None:
            run.specification_fingerprint = (
                self._ports.git.specification_fingerprint(
                    context.repo_path, run.base_sha
                )
            )
        run.current_fingerprint = (
            maintenance_planning_fingerprint(
                cycle, run.general_fingerprint or ""
            )
            if run.maintenance_planning
            else run.general_fingerprint
            if fingerprint_required
            else None
        )
        recoverable_publication = bool(
            run.publication_recovery_verified
            or (
                run.maintenance_planning
                and row.phase == "PLANNING"
                and cycle.publication_stage == "prepared"
            )
        )
        if (
            row.planning_error
            and row.planning_error_fingerprint == run.current_fingerprint
            and not recoverable_publication
            and not run.error_input_changed
        ):
            self._ports.events.error(
                f"계획 오류가 해결되지 않았습니다: {row.planning_error}\n"
                f"보존된 planning worktree: {row.worktree}"
            )
            return self._operational_error()

        if row.planning_error and not recoverable_publication:
            outcome = self._discard_stale_planning(run, row)
            if outcome is not None:
                return outcome
            row = self._ports.state.planning(context.repo)
            if (
                run.maintenance_planning
                and cycle.cycle_base_sha != run.base_sha
                and cycle.publication_stage is None
            ):
                cycle = self._ports.state.restart_stale_maintenance(
                    context.repo, cycle, run.base_sha
                )
                run.maintenance_cycle = cycle
                return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART)

        if row.phase == "PLANNING":
            started_fingerprint = (
                row.planning_attempt_fingerprint or run.current_fingerprint
            )
            if started_fingerprint != run.current_fingerprint:
                outcome = self._discard_stale_planning(
                    run,
                    row,
                    fingerprint=str(started_fingerprint or ""),
                    preserve=bool(row.planning_validation_error),
                )
                if outcome is not None:
                    return outcome
                row = self._ports.state.planning(context.repo)

        planning_required = not row.phase
        forced_general_planning = bool(
            run.selection
            and run.selection.kind
            in {
                PlanningDecisionKind.RUN_SPECIFICATION_PLANNING,
                PlanningDecisionKind.RUN_FALLBACK_PLANNING,
            }
        )
        if (
            not run.maintenance_planning
            and not row.phase
            and row.planning_fingerprint == run.current_fingerprint
            and not forced_general_planning
        ):
            self._ports.state.update(
                context.repo,
                processed_sha=run.base_sha,
                specification_fingerprint=run.specification_fingerprint,
            )
            self._ports.events.info(
                "계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다."
            )
            planning_required = False
            run.fingerprint_skipped = True

        if (
            planning_required
            and not run.maintenance_planning
            and row.planning_fingerprint
            and row.processed_sha
            and not forced_general_planning
        ):
            processed = self._ports.git.input_fingerprint(
                context.repo_path, row.processed_sha
            )
            if processed == run.current_fingerprint:
                self._ports.state.update(
                    context.repo,
                    processed_sha=run.base_sha,
                    specification_fingerprint=run.specification_fingerprint,
                )
                self._ports.events.info(
                    "계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다."
                )
                planning_required = False
                run.fingerprint_skipped = True
            elif self._ports.git.requirement_archive_only(
                context.repo_path, row.processed_sha, run.base_sha
            ):
                self._ports.state.update(
                    context.repo,
                    processed_sha=run.base_sha,
                )
                self._ports.events.info(
                    "Requirement archive 이동 외의 계획 입력이 바뀌지 않아 Agent를 실행하지 않습니다."
                )
                planning_required = False
                run.fingerprint_skipped = True

        if planning_required:
            assert run.current_fingerprint is not None
            project_id = hashlib.sha256(context.repo.encode()).hexdigest()[:16]
            worktree = (
                context.state_dir
                / "worktrees"
                / project_id
                / "planning"
                / run.current_fingerprint
            )
            self._ports.state.update(
                context.repo,
                active_sha=run.base_sha,
                plan_sha=None,
                planning_attempt_fingerprint=run.current_fingerprint,
                planning_workspace_owned=0,
                planning_retry_count=0,
                planner_session_id=None,
                worker_session_id=None,
                worker_result=None,
                verify_result=None,
                planning_validation_error=None,
                planning_result_pending_validation=0,
                planning_result_completed=0,
                planning_result=None,
                planning_error=None,
                planning_error_fingerprint=None,
                phase="PLANNING",
                branch=None,
                worktree=str(worktree),
                plan_path=None,
                pr_url=None,
                last_error=None,
            )
            row = self._ports.state.planning(context.repo)
        run.row = row
        run.maintenance_cycle = cycle
        return None

    def _discard_stale_planning(
        self,
        run: _PlanningRun,
        row: runtime_state.PlanningSnapshot,
        *,
        fingerprint: str | None = None,
        preserve: bool = True,
    ) -> PlanningOutcome | None:
        context = run.context
        worktree = Path(row.worktree) if row.worktree else None
        stored_fingerprint = fingerprint or str(
            row.planning_attempt_fingerprint
            or row.planning_error_fingerprint
            or ""
        )
        try:
            if worktree is None:
                if (
                    row.phase == "PLANNING"
                    or row.active_sha
                    or row.planning_workspace_owned
                    or row.planning_attempt_fingerprint
                    or row.planning_retry_count
                    or row.planner_session_id
                    or row.planning_validation_error
                    or row.planning_result_pending_validation
                    or row.planning_result_completed
                    or row.planning_result
                    or row.agent_snapshot
                    or row.agent_result_snapshot
                ):
                    raise RuntimeError("planning worktree 경로가 저장되지 않았습니다.")
            else:
                self._ports.git.validate_worktree_ownership(
                    context.repo, stored_fingerprint, worktree
                )
        except RuntimeError as error:
            message = f"planning workspace cleanup 소유권 검증 실패: {error}"
            self._ports.state.update(
                context.repo,
                planning_error=row.planning_error or message,
                planning_error_fingerprint=(
                    row.planning_error_fingerprint or run.current_fingerprint
                ),
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
        if preserve:
            self._ports.state.preserve_diagnostics(context.repo, row)
        try:
            if worktree is not None and worktree.exists():
                self._ports.git.run(
                    context.repo_path,
                    "worktree",
                    "remove",
                    str(worktree),
                )
        except RuntimeError as error:
            message = f"planning workspace cleanup 실패: {error}"
            self._ports.state.update(
                context.repo,
                planning_error=row.planning_error or message,
                planning_error_fingerprint=(
                    row.planning_error_fingerprint or run.current_fingerprint
                ),
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
        self._ports.state.clear_active(context.repo)
        return None

    def _fetch_remote_head(self, context: PlanningContext) -> str:
        self._ports.git.run(context.repo_path, "fetch", "origin")
        return self._ports.git.run(
            context.repo_path,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{context.base_branch}^{{commit}}",
        ).stdout.strip()

    def _prepare_workspace(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = Path(str(row.worktree or ""))
        try:
            self._ports.git.validate_workspace_identity(
                context.repo,
                str(row.planning_attempt_fingerprint or ""),
                worktree,
            )
            planning_started = bool(
                row.planner_session_id
                or row.agent_snapshot
                or row.planning_result_pending_validation
                or row.planning_result_completed
                or row.planning_validation_error
                or row.planning_retry_count
                or row.last_error
            )
            owned = row.planning_workspace_owned == WORKSPACE_OWNED
            reserved = row.planning_workspace_owned == WORKSPACE_RESERVED
            self._ports.git.make_planning_worktree(
                context.repo_path,
                worktree,
                context.base_branch,
                allow_create=not planning_started and (not owned or reserved),
                allow_existing=owned or reserved,
                ownership_reserved=reserved,
                expected_head=str(row.active_sha or ""),
                before_create=lambda: self._ports.state.update(
                    context.repo,
                    planning_workspace_owned=WORKSPACE_RESERVED,
                ),
            )
            self._ports.state.update(
                context.repo,
                planning_workspace_owned=WORKSPACE_OWNED,
            )
        except PlanningWorkspaceOwnershipError as error:
            message = f"planning workspace 소유권 검증 실패: {error}"
            self._ports.state.update(
                context.repo,
                planning_error=message,
                planning_error_fingerprint=run.current_fingerprint,
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()

        if run.maintenance_planning:
            try:
                cycle = self._ports.state.maintenance_cycle(context.repo)
                if cycle.publication_stage == "prepared":
                    maintenance_context = (
                        self._ports.validation.maintenance_context_at_revision(
                            context.repo,
                            worktree,
                            str(row.active_sha or ""),
                            cycle,
                        )
                    )
                    change_context = ""
                else:
                    maintenance_context = (
                        self._ports.validation.prepare_maintenance_context(
                            context.repo,
                            worktree,
                            str(row.active_sha or ""),
                        )
                    )
                    change_context = (
                        self._ports.validation.maintenance_change_context(
                            context.repo_path,
                            context.base_branch,
                            cycle,
                        )
                    )
            except (MaintenancePlanningError, MaintenanceOrchestrationError) as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            run.maintenance_cycle = cycle
            run.maintenance_context = maintenance_context
            run.maintenance_change_context = change_context
        else:
            try:
                run.mutable_ready_snapshot = (
                    self._ports.validation.prepare_general_context(
                        context.repo,
                        worktree,
                        str(row.active_sha or ""),
                    )
                )
            except PlanningResultInvalid as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
        run.planning_worktree = worktree
        return None

    def _run_planner_turn(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = run.planning_worktree
        assert worktree is not None
        head = self._ports.git.run(worktree, "rev-parse", "HEAD").stdout.strip()
        dirty_files = self._ports.git.changed_files(worktree)
        if row.planning_validation_error and head != row.active_sha:
            self._ports.git.run(
                worktree, "reset", "--mixed", str(row.active_sha)
            )
            head = self._ports.git.run(
                worktree, "rev-parse", "HEAD"
            ).stdout.strip()
            dirty_files = self._ports.git.changed_files(worktree)

        result_in_flight = bool(
            row.planning_result_pending_validation
            and not row.planning_result_completed
        )
        scope_error: str | None = None
        should_run = result_in_flight or (
            head == row.active_sha
            and (
                not dirty_files
                or row.planning_validation_error
                or row.last_error
            )
            and not row.planning_result_pending_validation
            and not (
                row.planning_validation_error
                and row.planning_retry_count >= MAX_PLANNER_VALIDATION_RETRIES
            )
        )
        file_review_enabled = (
            self._file_assessment_active(run)
            and hasattr(self._ports, "file_review")
        )
        if should_run:
            final_page = None
            if file_review_enabled:
                progress = self._ports.file_review.progress(
                    context.repo, context.repo_path, context.base_branch,
                    run.maintenance_cycle.revision, str(row.active_sha),
                )
                if (not progress.selected
                    and not run.maintenance_cycle.include_general_planning
                    and run.maintenance_cycle.publication_stage is None
                    and not row.planning_result_completed
                    and not result_in_flight):
                    return self._complete_empty_file_assessment(run, worktree)
                review_outcome = self._review_one_file(run, worktree)
                if review_outcome is not None:
                    return review_outcome
                if hasattr(self._ports, "reconciliation"):
                    relation_outcome = self._reconcile_one_relation(run, worktree)
                    if relation_outcome is not None:
                        return relation_outcome
            self._ports.events.log("Planner 시작: 다음 ExecPlan 계획을 생성합니다.")
            if not result_in_flight:
                self._ports.state.update(
                    context.repo,
                    last_error=None,
                    planning_result_pending_validation=1,
                    planning_result_completed=0,
                    planning_result=None,
                )
                row = self._ports.state.planning(context.repo)
            role = "maintenance_planner" if run.maintenance_planning else "planner"
            if run.maintenance_planning:
                assert run.maintenance_context is not None
                prompt = self._ports.validation.maintenance_prompt(
                    run.maintenance_context,
                    self._ports.state.compact_catalog(context.repo),
                    run.maintenance_change_context,
                    row.planning_validation_error or row.last_error,
                    include_general_planning=(
                        run.maintenance_cycle.include_general_planning
                    ),
                )
                if file_review_enabled:
                    if hasattr(self._ports, "reconciliation"):
                        progress = self._ports.reconciliation.progress(
                            context.repo, context.repo_path, context.base_branch,
                            run.maintenance_cycle.revision, str(row.active_sha),
                        )
                        final_page = self._ports.reconciliation.synthesis(
                            context.repo, run.maintenance_cycle.revision, str(row.active_sha), progress,
                        )
                        if final_page.index != final_page.total:
                            return PlanningOutcome(PlanningOutcomeKind.WAITING)
                        manifest = self._ports.reconciliation.synthesis_export(
                            context.repo, final_page, progress, context.state_dir / "maintenance-evidence",
                        )
                        existing = self._active_plan_statuses(context.repo)
                        collection, result_manifest = self._ports.reconciliation.findings_unit(
                            context.repo, context.repo_path, context.base_branch,
                            run.maintenance_cycle.revision, str(row.active_sha), existing,
                            context.state_dir / "maintenance-evidence",
                        )
                        if collection.receipt is None:
                            return PlanningOutcome(PlanningOutcomeKind.WAITING)
                        prompt += (
                            "\n\n검증된 발견·계획 비교 결과 manifest: " + str(result_manifest)
                            + "\nmanifest의 unit_pattern으로 저장된 발견·비교 원문을 필요한 부분만 조회하라. "
                            "제안된 새 계획을 실제 ExecPlan으로 작성하고 사용자 동작·공개 계약·저장 형식 "
                            "보존과 targeted unit test·전체 verify 회귀 조건을 명시하라. "
                            "모든 발견·비교는 이미 저장돼 있으므로 최종 응답에 배열을 다시 출력하지 마라. "
                            "페이지별 발견 연결·제외 검증은 이후 별도 단위에서 진행한다. "
                            "최종 응답에 아래 식별자·digest·개수·요약만 그대로 포함하라:\n"
                            + maintenance_findings.RESULT_PREFIX + maintenance_findings.encode(collection.receipt)
                        )
                if (context.self_repository and run.maintenance_cycle.phase
                    is maintenance_state.MaintenancePhase.ASSESSMENT
                    and not run.maintenance_cycle.full_assessment_completed):
                    inventory = full_assessment.source_inventory(
                        context.repo_path, str(row.active_sha)
                    )
                    prompt += full_assessment.assessment_prompt(
                        inventory, str(row.active_sha),
                        run.maintenance_cycle.full_assessment_result,
                    )
            else:
                assert run.mutable_ready_snapshot is not None
                prompt = self._ports.validation.planner_prompt(
                    row.planning_validation_error or row.last_error,
                    self._ports.state.compact_catalog(context.repo),
                    run.mutable_ready_snapshot,
                )
            def save_final_session(session_id):
                if final_page is not None:
                    self._ports.reconciliation.synthesis_session(context.repo, final_page, session_id)
                self._ports.state.update(context.repo, planner_session_id=session_id)

            before = self._ports.agent.begin_scope(role, worktree)
            result = self._ports.agent.run_planner(
                worktree, prompt, final_page.session_id if final_page is not None else row.planner_session_id,
                save_final_session,
            )
            if result.session_id:
                save_final_session(result.session_id)
            scope_result = self._ports.agent.finish_scope(role, worktree, before)
            if scope_result.error:
                scope_error = scope_result.error
                if scope_result.result_snapshot is not None:
                    self._ports.state.update(
                        context.repo,
                        agent_result_snapshot=scope_result.result_snapshot,
                    )
            else:
                self._ports.agent.clear_scope(context.repo)

            if result.code:
                message = result.message or f"Planner 종료 코드 {result.code}"
                if result.kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
                    outcome = PlanningOutcomeKind.TRANSIENT_PAUSE
                elif result.kind is PlanningAgentResultKind.USAGE_LIMIT:
                    outcome = PlanningOutcomeKind.USAGE_LIMIT_PAUSE
                else:
                    outcome = PlanningOutcomeKind.OPERATIONAL_ERROR
                self._ports.state.update(
                    context.repo,
                    last_error=message,
                    planning_result_pending_validation=0,
                    planning_result_completed=0,
                    planning_result=None,
                )
                self._ports.events.error(message)
                return PlanningOutcome(outcome, error=True)
            self._ports.state.update(
                context.repo,
                last_error=None,
                planning_result_completed=1,
                planning_result=result.message,
            )
            row = self._ports.state.planning(context.repo)
            dirty_files = self._ports.git.changed_files(worktree)

        if scope_error is None:
            scope_row = self._ports.state.planning(context.repo)
            if scope_row.agent_snapshot:
                expected_role = (
                    "maintenance_planner" if run.maintenance_planning else "planner"
                )
                if scope_row.agent_role != expected_role:
                    scope_error = "저장된 Agent 역할이 Planner와 다릅니다."
                else:
                    scope_result = self._ports.agent.validate_saved_scope(
                        expected_role,
                        worktree,
                        scope_row.agent_snapshot,
                    )
                    scope_error = scope_result.error
                    if scope_result.error and scope_result.result_snapshot is not None:
                        self._ports.state.update(
                            context.repo,
                            agent_result_snapshot=scope_result.result_snapshot,
                        )
                    elif not scope_result.error:
                        self._ports.agent.clear_scope(context.repo)
        run.row = row
        run.dirty_files = dirty_files
        run.planner_scope_error = scope_error
        return None

    def _complete_empty_file_assessment(
        self, run: _PlanningRun, worktree: Path,
    ) -> PlanningOutcome:
        """A verified empty selection needs no assessment Agent or plan commit."""
        context = run.context
        row = run.row
        assert run.maintenance_context is not None
        self._ports.git.validate_worktree_ownership(
            context.repo, str(run.current_fingerprint or ""), worktree,
        )
        self._ports.git.run(context.repo_path, "fetch", "origin")
        try:
            self._ports.validation.validate_publication_base(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_context,
            )
        except MaintenancePlanningRestart:
            self._ports.git.run(context.repo_path, "worktree", "remove", "--force", str(worktree))
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART, error=True)
        evidence = self._ports.state.file_assessment_evidence(
            context.repo, context.repo_path, context.base_branch,
            run.maintenance_cycle.revision, str(row.active_sha),
        )
        if evidence.reviewed:
            raise PlanningResultInvalid("빈 선정 집합의 검토 결과가 예상과 다릅니다.")
        with self._ports.state.transaction():
            self._ports.state.complete_file_assessment(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(row.active_sha), evidence,
            )
            self._ports.state.advance_success_baseline(
                context.repo,
                expected_phase=run.maintenance_cycle.phase,
                expected_revision=run.maintenance_cycle.revision,
                remote_commit=str(row.active_sha),
            )
            self._ports.state.clear_active_in_transaction(context.repo)
        self._ports.git.run(context.repo_path, "worktree", "remove", str(worktree))
        return PlanningOutcome(PlanningOutcomeKind.NO_CHANGE)

    def _review_one_file(self, run: _PlanningRun, worktree: Path) -> PlanningOutcome | None:
        """Persist at most one source-bound review unit per scheduler turn."""
        context = run.context
        review = self._ports.file_review
        try:
            progress = review.progress(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(run.row.active_sha),
            )
            unit = progress.next_unit
            if unit is None:
                return None
            content = review.content(context.repo_path, context.base_branch, unit.source)
            prompt = maintenance_file_review.review_prompt(
                unit.source, content, root_placements=progress.root_placements,
            )
            if unit.error:
                prompt += "\n이전 검토 결과 오류를 수정하라: " + unit.error
            review.start(context.repo, run.maintenance_cycle.revision, unit.source)
        except (maintenance_file_review.FileReviewError, SourceInventoryError) as error:
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART,
                                   detail=str(error))
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        result = self._ports.agent.run_planner(
            worktree, prompt, unit.session_id,
            lambda session_id: review.session(
                context.repo, run.maintenance_cycle.revision, unit.source, session_id,
            ),
        )
        if result.session_id:
            review.session(context.repo, run.maintenance_cycle.revision,
                           unit.source, result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(context.repo)
        if result.code:
            self._ports.events.error(result.message)
            if result.kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
                kind = PlanningOutcomeKind.TRANSIENT_PAUSE
            elif result.kind is PlanningAgentResultKind.USAGE_LIMIT:
                kind = PlanningOutcomeKind.USAGE_LIMIT_PAUSE
            else:
                kind = PlanningOutcomeKind.OPERATIONAL_ERROR
            return PlanningOutcome(kind, error=True)
        try:
            # Re-read the current ref before accepting any Agent-supplied claim.
            content = review.content(context.repo_path, context.base_branch, unit.source)
            parsed = maintenance_file_review.parse_review(
                result.message, unit.source, content,
            )
            review.complete(context.repo, run.maintenance_cycle.revision,
                            unit.source, parsed)
        except (maintenance_file_review.FileReviewError, SourceInventoryError) as error:
            review.fail(context.repo, run.maintenance_cycle.revision,
                        unit.source, str(error))
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY,
                                   detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

    def _reconcile_one_relation(self, run: _PlanningRun, worktree: Path) -> PlanningOutcome | None:
        """Verify one cross-file candidate per turn, retaining session progress."""
        context = run.context
        port = self._ports.reconciliation
        try:
            progress = port.progress(
                context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(run.row.active_sha),
            )
            if progress.pending_scan:
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            relation = progress.next_relation
            if relation is None:
                return self._synthesize_one_page(run, worktree, progress)
            left = port.content(context.repo_path, context.base_branch, relation.left)
            right = port.content(context.repo_path, context.base_branch, relation.right)
            prompt = maintenance_reconciliation.relation_prompt(relation, left, right)
            port.start(context.repo, run.maintenance_cycle.revision,
                       str(run.row.active_sha), relation)
            session_id, previous_error = port.unit(context.repo, relation)
            if previous_error:
                prompt += "\n이전 관계 확인 결과 오류를 수정하라: " + previous_error
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.REMOTE_BASE_RESTART, detail=str(error))
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        result = self._ports.agent.run_planner(
            worktree, prompt, session_id,
            lambda value: port.session(context.repo, run.maintenance_cycle.revision,
                                       relation, value),
        )
        if result.session_id:
            port.session(context.repo, run.maintenance_cycle.revision, relation, result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(context.repo)
        if result.code:
            self._ports.events.error(result.message)
            if result.kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
                kind = PlanningOutcomeKind.TRANSIENT_PAUSE
            elif result.kind is PlanningAgentResultKind.USAGE_LIMIT:
                kind = PlanningOutcomeKind.USAGE_LIMIT_PAUSE
            else:
                kind = PlanningOutcomeKind.OPERATIONAL_ERROR
            return PlanningOutcome(kind, error=True)
        try:
            left = port.content(context.repo_path, context.base_branch, relation.left)
            right = port.content(context.repo_path, context.base_branch, relation.right)
            parsed = maintenance_reconciliation.parse_relation(result.message, relation, left, right)
            port.complete(context.repo, run.maintenance_cycle.revision, relation, parsed)
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            port.fail(context.repo, run.maintenance_cycle.revision, relation, str(error))
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

    def _synthesize_one_page(self, run: _PlanningRun, worktree: Path,
                             progress) -> PlanningOutcome | None:
        """Assess one page independently; only its durable result crosses sessions."""
        context, port = run.context, self._ports.reconciliation
        page = port.synthesis(context.repo, run.maintenance_cycle.revision,
                              str(run.row.active_sha), progress)
        if page.index == page.total:
            return self._collect_findings_unit(run, worktree)
        manifest = port.synthesis_export(context.repo, page, progress,
                                         context.state_dir / "maintenance-evidence")
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        session_errors = []

        def save_session(session_id):
            try:
                port.synthesis_session(context.repo, page, session_id)
            except maintenance_reconciliation.ReconciliationError as error:
                session_errors.append(str(error))

        result = self._ports.agent.run_planner(
            worktree, maintenance_reconciliation.synthesis_prompt(page, manifest),
            page.session_id, save_session,
        )
        if result.session_id:
            save_session(result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(context.repo)
        if result.code:
            self._ports.events.error(result.message)
            kind = {PlanningAgentResultKind.TRANSIENT_FAILURE: PlanningOutcomeKind.TRANSIENT_PAUSE,
                    PlanningAgentResultKind.USAGE_LIMIT: PlanningOutcomeKind.USAGE_LIMIT_PAUSE}.get(
                        result.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            return PlanningOutcome(kind, error=True)
        try:
            if session_errors:
                raise maintenance_reconciliation.ReconciliationError(session_errors[0])
            current = port.progress(context.repo, context.repo_path, context.base_branch,
                                    run.maintenance_cycle.revision, str(run.row.active_sha))
            fresh = port.synthesis(context.repo, run.maintenance_cycle.revision,
                                   str(run.row.active_sha), current)
            if fresh.key != page.key:
                raise maintenance_reconciliation.ReconciliationError("종합 중 source가 바뀌었습니다.")
            port.synthesis_complete(context.repo, page, result.message)
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

    def _active_plan_statuses(self, repo: str) -> dict[str, str]:
        return {str(item["plan_id"]): str(item["status"])
                for item in self._ports.state.execplan_rows(repo) if str(item["status"]) != "MERGED"}

    def _collect_findings_unit(self, run: _PlanningRun, worktree: Path) -> PlanningOutcome | None:
        context, port = run.context, self._ports.reconciliation
        plans = self._active_plan_statuses(context.repo)
        unit, manifest = port.findings_unit(context.repo, context.repo_path, context.base_branch,
            run.maintenance_cycle.revision, str(run.row.active_sha), plans, context.state_dir / "maintenance-evidence")
        if unit.receipt is not None:
            return None
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        result = self._ports.agent.run_planner(worktree, maintenance_findings.unit_prompt(unit, manifest),
            unit.session_id, lambda session_id: port.findings_session(context.repo, unit, session_id))
        if result.session_id:
            port.findings_session(context.repo, unit, result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(context.repo)
        if result.code:
            self._ports.events.error(result.message)
            kind = {PlanningAgentResultKind.TRANSIENT_FAILURE: PlanningOutcomeKind.TRANSIENT_PAUSE,
                    PlanningAgentResultKind.USAGE_LIMIT: PlanningOutcomeKind.USAGE_LIMIT_PAUSE}.get(
                        result.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            return PlanningOutcome(kind, error=True)
        try:
            port.findings_complete(context.repo, context.repo_path, context.base_branch,
                run.maintenance_cycle.revision, str(run.row.active_sha), self._active_plan_statuses(context.repo),
                unit, result.message)
        except (maintenance_reconciliation.ReconciliationError, maintenance_file_review.FileReviewError,
                SourceInventoryError) as error:
            try:
                port.findings_fail(context.repo, unit, str(error))
            except maintenance_reconciliation.ReconciliationError:
                pass  # A new snapshot already invalidated the old unit.
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

    def _resolve_one_page(self, run: _PlanningRun, worktree: Path,
                          unit: maintenance_reconciliation.ResolutionUnit) -> PlanningOutcome:
        """Keep the final Planner result pending while one coverage receipt is validated."""
        repo, port = run.context.repo, self._ports.reconciliation
        before = self._ports.agent.begin_scope("maintenance_planner", worktree)
        result = self._ports.agent.run_planner(
            worktree, maintenance_reconciliation.resolution_prompt(unit), unit.session_id,
            lambda session_id: port.resolution_session(repo, unit, session_id),
        )
        if result.session_id:
            port.resolution_session(repo, unit, result.session_id)
        scope = self._ports.agent.finish_scope("maintenance_planner", worktree, before)
        if scope.error:
            self._ports.events.error(scope.error)
            return self._operational_error()
        self._ports.agent.clear_scope(repo)
        if result.code:
            self._ports.events.error(result.message)
            kind = {PlanningAgentResultKind.TRANSIENT_FAILURE: PlanningOutcomeKind.TRANSIENT_PAUSE,
                    PlanningAgentResultKind.USAGE_LIMIT: PlanningOutcomeKind.USAGE_LIMIT_PAUSE}.get(
                        result.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            return PlanningOutcome(kind, error=True)
        try:
            port.resolution_complete(repo, unit, result.message)
        except maintenance_reconciliation.ReconciliationError as error:
            port.resolution_fail(repo, unit, str(error))
            self._ports.events.error(str(error))
            return PlanningOutcome(PlanningOutcomeKind.VALIDATION_RETRY, detail=str(error))
        return PlanningOutcome(PlanningOutcomeKind.WAITING)

    def _validate_planner_result(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = run.planning_worktree
        assert worktree is not None
        dirty_files = run.dirty_files or []
        decision: MaintenancePlannerDecision | None = None
        try:
            if run.planner_scope_error is not None:
                raise PlanningResultInvalid(run.planner_scope_error)
            if run.maintenance_planning:
                assert run.maintenance_context is not None
                try:
                    decision = parse_maintenance_planner_decision(
                        str(row.planning_result or ""),
                        run.maintenance_cycle.phase,
                    )
                except MaintenanceOrchestrationError as error:
                    raise PlanningResultInvalid(str(error)) from error
                if (run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
                    and run.maintenance_cycle.file_assessment_start_merged_count is not None
                    and (not context.self_repository or run.maintenance_cycle.full_assessment_completed)
                    and hasattr(self._ports, "reconciliation")):
                    try:
                        resolution = self._ports.reconciliation.validate(
                            context.repo, context.repo_path, context.base_branch,
                            run.maintenance_cycle.revision, str(row.active_sha),
                            str(row.planning_result or ""),
                            {str(item["plan_id"]): str(item["status"])
                             for item in self._ports.state.execplan_rows(context.repo)
                             if str(item["status"]) != "MERGED"},
                            decision.plan_ids if decision.outcome is MaintenanceOutcome.REFACTORING else (),
                            resolution_directory=context.state_dir / "maintenance-evidence",
                        )
                        if isinstance(resolution, maintenance_reconciliation.ResolutionUnit):
                            return self._resolve_one_page(run, worktree, resolution)
                    except (maintenance_reconciliation.ReconciliationError,
                            maintenance_file_review.FileReviewError, SourceInventoryError) as error:
                        raise PlanningResultInvalid(str(error)) from error
                changed = sorted(
                    set(dirty_files)
                    | set(
                        self._ports.git.changed_files(
                            worktree, str(row.active_sha or "")
                        )
                    )
                )
                if decision.outcome is MaintenanceOutcome.PREREQUISITES:
                    if run.maintenance_cycle.include_general_planning:
                        raise PlanningResultInvalid(
                            "combined planning에는 prerequisite 결과를 사용할 수 없습니다."
                        )
                    if changed:
                        raise PlanningResultInvalid(
                            "prerequisites 결과는 planning 파일을 변경할 수 없습니다."
                        )
                    try:
                        validate_prerequisite_plan_ids(
                            self._ports.state.execplan_rows(context.repo),
                            decision.plan_ids,
                        )
                    except MaintenanceOrchestrationError as error:
                        raise PlanningResultInvalid(str(error)) from error
                    validated_result = None
                elif decision.outcome is MaintenanceOutcome.UNSAFE:
                    if changed:
                        raise PlanningResultInvalid(
                            "unsafe revalidation 결과는 계획 파일을 변경할 수 없습니다."
                        )
                    validated_result = None
                else:
                    refactoring_ids = (
                        decision.plan_ids
                        if decision.outcome is MaintenanceOutcome.REFACTORING
                        else ()
                    )
                    validated_result = (
                        self._ports.validation.validate_maintenance_result(
                            run.maintenance_context,
                            worktree,
                            dirty_files,
                            refactoring_plan_ids=refactoring_ids,
                            allow_general_plans=(
                                run.maintenance_cycle.include_general_planning
                            ),
                        )
                    )
                    if (
                        decision.outcome is MaintenanceOutcome.REFACTORING
                        and validated_result is None
                    ):
                        raise PlanningResultInvalid(
                            "refactoring 결과에는 검증 가능한 planning batch가 필요합니다."
                        )
            else:
                assert run.mutable_ready_snapshot is not None
                validated_result = self._ports.validation.validate_general_result(
                    context.repo,
                    worktree,
                    str(row.active_sha or ""),
                    dirty_files,
                    run.mutable_ready_snapshot,
                )
            if (
                decision is not None
                and context.self_repository
                and run.maintenance_cycle.phase is maintenance_state.MaintenancePhase.ASSESSMENT
                and not run.maintenance_cycle.full_assessment_completed
                and decision.outcome is not MaintenanceOutcome.PREREQUISITES
            ):
                try:
                    inventory = full_assessment.source_inventory(
                        context.repo_path, str(row.active_sha)
                    )
                    report = full_assessment.parse_assessment_result(
                        str(row.planning_result or ""), inventory,
                        str(row.active_sha),
                        decision.plan_ids
                        if decision.outcome is MaintenanceOutcome.REFACTORING else (),
                        {str(item["plan_id"]) for item in
                         self._ports.state.execplan_rows(context.repo)},
                    )
                    run.maintenance_cycle = self._ports.state.record_full_assessment(
                        context.repo,
                        expected_revision=run.maintenance_cycle.revision,
                        source_sha=str(row.active_sha), result=report,
                    )
                except (full_assessment.FullAssessmentError,
                        maintenance_state.MaintenanceStateError) as error:
                    raise PlanningResultInvalid(str(error)) from error
        except PlanningResultInvalid as error:
            message = str(error)
            retry_count = row.planning_retry_count
            if (
                row.planning_result_pending_validation
                and row.planning_validation_error
            ):
                retry_count += 1
            values: dict[str, object] = {
                "planning_validation_error": message,
                "planning_result_pending_validation": 0,
                "planning_result_completed": 0,
                "last_error": None,
                "planning_retry_count": retry_count,
            }
            if retry_count >= MAX_PLANNER_VALIDATION_RETRIES:
                values.update(
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                )
            self._ports.state.update(context.repo, **values)
            self._ports.events.error(message)
            return PlanningOutcome(
                PlanningOutcomeKind.VALIDATION_RETRY, error=True
            )

        if (
            decision is not None
            and decision.outcome is MaintenanceOutcome.PREREQUISITES
        ):
            try:
                self._ports.git.validate_worktree_ownership(
                    context.repo,
                    str(run.current_fingerprint or ""),
                    worktree,
                )
                with self._ports.state.transaction():
                    self._ports.state.transition_maintenance(
                        context.repo,
                        expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                        expected_revision=run.maintenance_cycle.revision,
                        next_phase=(
                            maintenance_state.MaintenancePhase.PREREQUISITE_WAIT
                        ),
                        prerequisite_plan_ids=decision.plan_ids,
                        include_general_planning=False,
                    )
                    self._ports.state.clear_active_in_transaction(context.repo)
                    self._ports.git.run(
                        context.repo_path,
                        "worktree",
                        "remove",
                        str(worktree),
                    )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=row.planning_error or message,
                    planning_error_fingerprint=(
                        row.planning_error_fingerprint or run.current_fingerprint
                    ),
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            return PlanningOutcome(PlanningOutcomeKind.PREREQUISITE_WAIT)

        if decision is not None and decision.outcome is MaintenanceOutcome.UNSAFE:
            message = (
                "post-refactor revalidation이 제품 목표 보존을 확정하지 못했습니다: "
                + str(decision.reason)
            )
            self._ports.state.update(
                context.repo,
                planning_error=message,
                planning_error_fingerprint=run.current_fingerprint,
                last_error=message,
            )
            self._ports.events.error(message)
            return PlanningOutcome(
                PlanningOutcomeKind.UNSAFE_REVALIDATION, error=True
            )

        run.published_refactoring = bool(
            decision is not None
            and decision.outcome is MaintenanceOutcome.REFACTORING
        )
        run.maintenance_decision = decision
        run.validated_result = validated_result
        return None

    def _publish_planner_result(
        self, run: _PlanningRun
    ) -> PlanningOutcome | None:
        row = run.row
        if row.phase != "PLANNING":
            return None
        context = run.context
        worktree = run.planning_worktree
        assert worktree is not None
        validated_result = run.validated_result
        if validated_result is None:
            completed_fingerprint = self._ports.git.input_fingerprint(
                context.repo_path, str(row.active_sha or "")
            )
            completed_specification_fingerprint = (
                self._ports.git.specification_fingerprint(
                    context.repo_path, str(row.active_sha or "")
                )
            )
            self._ports.git.validate_worktree_ownership(
                context.repo,
                str(run.current_fingerprint or ""),
                worktree,
            )
            if run.maintenance_planning:
                assert run.maintenance_context is not None
                try:
                    self._ports.git.run(context.repo_path, "fetch", "origin")
                    self._ports.validation.validate_publication_base(
                        context.repo,
                        context.repo_path,
                        context.base_branch,
                        run.maintenance_context,
                    )
                except MaintenancePlanningRestart:
                    self._ports.git.run(
                        context.repo_path,
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    )
                    self._ports.events.error(
                        "유지보수 평가 중 base가 변경되어 최신 기준으로 다시 평가합니다."
                    )
                    return PlanningOutcome(
                        PlanningOutcomeKind.REMOTE_BASE_RESTART,
                        error=True,
                    )
                except RuntimeError as error:
                    message = str(error)
                    self._ports.state.update(
                        context.repo,
                        planning_error=message,
                        planning_error_fingerprint=run.current_fingerprint,
                        last_error=message,
                    )
                    self._ports.events.error(message)
                    return self._operational_error()
                evidence = None
                if self._file_assessment_active(run):
                    evidence = self._ports.state.file_assessment_evidence(
                        context.repo, context.repo_path, context.base_branch,
                        run.maintenance_cycle.revision, str(row.active_sha),
                    )
            try:
                with self._ports.state.transaction():
                    if run.maintenance_planning:
                        if evidence is not None:
                            self._ports.state.complete_file_assessment(
                                context.repo, context.repo_path, context.base_branch,
                                run.maintenance_cycle.revision, str(row.active_sha),
                                evidence,
                            )
                        self._ports.state.advance_success_baseline(
                            context.repo,
                            expected_phase=run.maintenance_cycle.phase,
                            expected_revision=run.maintenance_cycle.revision,
                            remote_commit=str(row.active_sha or ""),
                        )
                        self._ports.state.clear_active_in_transaction(
                            context.repo,
                            processed_sha=(
                                str(row.active_sha or "")
                                if run.maintenance_cycle.include_general_planning
                                else None
                            ),
                            specification_fingerprint=(
                                completed_specification_fingerprint
                                if run.maintenance_cycle.include_general_planning
                                else None
                            ),
                            planning_fingerprint=(
                                completed_fingerprint
                                if run.maintenance_cycle.include_general_planning
                                else None
                            ),
                        )
                    else:
                        self._ports.state.clear_active_in_transaction(
                            context.repo,
                            processed_sha=row.active_sha,
                            specification_fingerprint=(
                                completed_specification_fingerprint
                            ),
                            planning_fingerprint=completed_fingerprint,
                        )
                    self._ports.git.run(
                        context.repo_path,
                        "worktree",
                        "remove",
                        str(worktree),
                    )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=row.planning_error or message,
                    planning_error_fingerprint=(
                        row.planning_error_fingerprint or run.current_fingerprint
                    ),
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            self._ports.events.info(
                "유지보수 평가와 재검증을 완료했습니다."
                if run.maintenance_planning
                else "추가로 계획할 작업이 없습니다."
            )
            run.row = self._ports.state.planning(context.repo)
            return None

        plans = list(validated_result.plans)
        if run.dirty_files:
            self._ports.git.run(
                worktree,
                "add",
                "--",
                *validated_result.publication_paths,
            )
            if run.maintenance_planning:
                message = "Update periodic maintenance planning batch"
            elif not plans and not (
                validated_result.revisions or validated_result.deletions
            ):
                message = "Update future ideas from planning"
            elif validated_result.revisions or validated_result.deletions:
                message = "Update mutable READY planning batch"
            else:
                first = plans[0][1]
                last = plans[-1][1]
                message = (
                    f"Add ExecPlan {first['id']}: {first['title']}"
                    if len(plans) == 1
                    else f"Add ExecPlans {first['id']}-{last['id']}"
                )
            self._ports.git.run(worktree, "commit", "-m", message)

        plan_commit = self._ports.git.run(
            worktree, "rev-parse", "HEAD"
        ).stdout.strip()
        if run.maintenance_planning:
            assert run.maintenance_context is not None
            try:
                self._ports.git.validate_worktree_ownership(
                    context.repo,
                    str(run.current_fingerprint or ""),
                    worktree,
                )
                if run.maintenance_cycle.publication_stage is None:
                    self._ports.validation.validate_publication_base(
                        context.repo,
                        context.repo_path,
                        context.base_branch,
                        run.maintenance_context,
                    )
                    run.maintenance_cycle = (
                        self._ports.state.record_maintenance_intent(
                            context.repo,
                            worktree,
                            context.base_branch,
                            validated_result,
                            plan_commit,
                        )
                    )
                elif run.maintenance_cycle.publication_stage != "prepared":
                    raise MaintenancePlanningError(
                        "복구할 수 없는 maintenance publication 단계입니다."
                    )
            except MaintenancePlanningRestart:
                self._ports.git.run(
                    context.repo_path,
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                )
                self._ports.events.error(
                    "유지보수 계획 중 base가 변경되어 최신 기준으로 다시 평가합니다."
                )
                return PlanningOutcome(
                    PlanningOutcomeKind.REMOTE_BASE_RESTART, error=True
                )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()

        if not run.maintenance_planning:
            assert run.specification_fingerprint is not None
            assert run.general_fingerprint is not None
            try:
                self._ports.git.validate_worktree_ownership(
                    context.repo,
                    str(run.current_fingerprint or ""),
                    worktree,
                )
                self._ports.state.record_general_intent(
                    context.repo,
                    worktree,
                    validated_result,
                    plan_commit,
                    run.specification_fingerprint,
                    run.general_fingerprint,
                )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()

        self._ports.git.run(context.repo_path, "fetch", "origin")
        latest_sha = self._ports.git.run(
            context.repo_path,
            "rev-parse",
            f"origin/{context.base_branch}",
        ).stdout.strip()
        if (
            not run.maintenance_planning
            and latest_sha not in {row.active_sha, plan_commit}
        ):
            try:
                with self._ports.state.transaction():
                    recovery = self._ports.state.recover_general_batch(
                        context.repo, worktree, context.base_branch
                    )
                    if recovery == "pending":
                        raise RuntimeError(
                            "원격 base가 이동했지만 publication recovery가 pending을 반환했습니다."
                        )
                    self._ports.git.run(
                        context.repo_path,
                        "worktree",
                        "remove",
                        str(worktree),
                    )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=row.planning_error or message,
                    planning_error_fingerprint=(
                        row.planning_error_fingerprint or run.current_fingerprint
                    ),
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            self._ports.events.info(
                "게시된 일반 planning batch를 복구했습니다."
                if recovery == "applied"
                else "원격 변경에 맞춰 일반 planning batch를 다시 시작합니다."
            )
            return PlanningOutcome(PlanningOutcomeKind.PUBLICATION_RECOVERY)
        if run.maintenance_planning and latest_sha not in {
            row.active_sha,
            plan_commit,
        }:
            try:
                with self._ports.state.transaction():
                    recovered_cycle = self._ports.state.recover_maintenance_batch(
                        context.repo, worktree, context.base_branch
                    )
                    self._ports.git.run(
                        context.repo_path,
                        "worktree",
                        "remove",
                        str(worktree),
                    )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=row.planning_error or message,
                    planning_error_fingerprint=(
                        row.planning_error_fingerprint or run.current_fingerprint
                    ),
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()
            if recovered_cycle.phase in {
                maintenance_state.MaintenancePhase.ASSESSMENT,
                maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            }:
                self._ports.events.info(
                    "미게시 유지보수 batch를 폐기하고 최신 기준으로 다시 평가합니다."
                )
            else:
                self._ports.events.info("게시된 유지보수 batch를 복구했습니다.")
            return PlanningOutcome(PlanningOutcomeKind.PUBLICATION_RECOVERY)

        if not run.maintenance_planning:
            assert run.specification_fingerprint is not None
            assert run.general_fingerprint is not None
            try:
                # The first call durably records the intent. Repeating the
                # idempotent operation after fetch closes the activation race
                # between validation and the fast-forward push.
                self._ports.state.record_general_intent(
                    context.repo,
                    worktree,
                    validated_result,
                    plan_commit,
                    run.specification_fingerprint,
                    run.general_fingerprint,
                )
            except RuntimeError as error:
                message = str(error)
                self._ports.state.update(
                    context.repo,
                    planning_error=message,
                    planning_error_fingerprint=run.current_fingerprint,
                    last_error=message,
                )
                self._ports.events.error(message)
                return self._operational_error()

        if latest_sha not in {row.active_sha, plan_commit}:
            published = (
                self._ports.git.run(
                    worktree,
                    "merge-base",
                    "--is-ancestor",
                    plan_commit,
                    f"origin/{context.base_branch}",
                    check=False,
                ).returncode
                == 0
            )
            if not published:
                self._ports.git.validate_worktree_ownership(
                    context.repo,
                    str(run.current_fingerprint or ""),
                    worktree,
                )
                self._ports.git.run(
                    context.repo_path,
                    "worktree",
                    "remove",
                    str(worktree),
                )
                self._ports.state.clear_active(context.repo)
                self._ports.events.error(
                    "계획 중 base가 변경되어 최신 기준으로 다시 계획합니다."
                )
                return PlanningOutcome(
                    PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                    error=True,
                )

        if latest_sha != plan_commit and latest_sha == row.active_sha:
            pushed = self._ports.git.run(
                worktree,
                "push",
                "origin",
                f"HEAD:refs/heads/{context.base_branch}",
                check=False,
            )
            if pushed.returncode:
                message = f"ExecPlan push 실패:\n{pushed.stdout[-8000:]}"
                self._ports.state.update(context.repo, last_error=message)
                self._ports.events.error(pushed.stdout)
                return self._operational_error()
            self._ports.git.run(context.repo_path, "fetch", "origin")
            latest_sha = self._ports.git.run(
                context.repo_path,
                "rev-parse",
                f"origin/{context.base_branch}",
            ).stdout.strip()
            if latest_sha != plan_commit:
                message = (
                    "ExecPlan push 결과를 origin/base에서 확인할 수 없습니다."
                )
                self._ports.state.update(context.repo, last_error=message)
                self._ports.events.error(
                    "ExecPlan push 결과 확인에 실패했습니다."
                )
                return self._operational_error()

        try:
            with self._ports.state.transaction():
                if run.maintenance_planning:
                    self._ports.state.publish_maintenance_batch(
                        context.repo,
                        worktree,
                        context.base_branch,
                        validated_result,
                        plan_commit,
                    )
                else:
                    self._ports.git.validate_worktree_ownership(
                        context.repo,
                        str(run.current_fingerprint or ""),
                        worktree,
                    )
                    assert run.specification_fingerprint is not None
                    assert run.general_fingerprint is not None
                    self._ports.state.publish_general_batch(
                        context.repo,
                        worktree,
                        context.base_branch,
                        validated_result,
                        plan_commit,
                        run.specification_fingerprint,
                        run.general_fingerprint,
                    )
                self._ports.git.run(
                    context.repo_path,
                    "worktree",
                    "remove",
                    str(worktree),
                )
        except RuntimeError as error:
            message = str(error)
            self._ports.state.update(
                context.repo,
                planning_error=row.planning_error or message,
                planning_error_fingerprint=(
                    row.planning_error_fingerprint or run.current_fingerprint
                ),
                last_error=message,
            )
            self._ports.events.error(message)
            return self._operational_error()
        run.base_sha = latest_sha
        run.row = self._ports.state.planning(context.repo)
        return None

    def _activate_next_execplan(
        self, run: _PlanningRun
    ) -> PlanningOutcome:
        context = run.context
        row = run.row
        newly_activated = run.ready_activated_before_planning
        if not row.phase:
            latest_sha = self._fetch_remote_head(context)
            if self._remote_base_changed_before_activation(run, latest_sha):
                return self._restart_for_new_input()
            decision = self._ports.state.schedule(context.repo)
            if not decision.has_ready and decision.has_waiting:
                self._ports.events.info(
                    "독립 READY가 없어 PLAN_BLOCKED 또는 PR_OPEN 종료를 기다립니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            if not decision.has_ready:
                latest_fingerprint = self._ports.git.input_fingerprint(
                    context.repo_path, latest_sha
                )
                expected_fingerprint = (
                    run.general_fingerprint
                    if run.maintenance_planning
                    else run.current_fingerprint
                )
                assert expected_fingerprint is not None
                if latest_fingerprint != expected_fingerprint:
                    self._ports.events.error(
                        "계획 중 입력이 변경되어 최신 기준으로 다시 계획합니다."
                    )
                    return PlanningOutcome(
                        PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                        error=True,
                    )
            elif decision.runnable_ready is None:
                self._ports.events.info(
                    "READY ExecPlan이 남아 있지만 dependency가 충족되지 않아 대기합니다."
                )
                return PlanningOutcome(PlanningOutcomeKind.WAITING)
            run.base_sha = latest_sha
            selected = self._ports.state.activate_ready(
                context.repo, run.base_sha
            )
            if selected is None:
                self._ports.events.info("실행 가능한 READY ExecPlan이 없습니다.")
                return PlanningOutcome(
                    PlanningOutcomeKind.FINGERPRINT_SKIPPED
                    if run.fingerprint_skipped
                    else PlanningOutcomeKind.NO_CHANGE
                )
            newly_activated = True
        return PlanningOutcome(
            PlanningOutcomeKind.REFACTORING
            if run.published_refactoring
            else PlanningOutcomeKind.EXECUTION_READY,
            base_sha=run.base_sha,
            newly_activated=newly_activated,
        )
