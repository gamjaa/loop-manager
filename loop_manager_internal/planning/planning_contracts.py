"""Planning lifecycle state machine independent of the CLI bootstrap."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
from loop_manager_internal.maintenance.maintenance_orchestration import MaintenancePlannerDecision
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningContext
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance.maintenance_file_inventory import SourceInventory
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.planning.assessment_port import AssessmentPort


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
    ASSESSMENT_PROGRESS = "assessment_progress"
    MAINTENANCE_COMPLETE = "maintenance_complete"
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
    refresh_catalog: Callable[[str], str] | None = None


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

    def reconcile_catalog(
        self, repo: str, repository: Path, base_branch: str, revision: str,
    ) -> None: ...

    def planning(self, repo: str) -> runtime_state.PlanningSnapshot: ...

    def maintenance_cycle(self, repo: str) -> maintenance_state.MaintenanceCycle: ...

    def update(self, repo: str, **values: object) -> None: ...

    def release_planning_correction(
        self, repo: str, *, error: str, active_sha: str, context: str,
    ) -> bool: ...

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

    def supersede_unpublished_general_intent(self, repo: str) -> None: ...

    def supersede_unpublished_maintenance_intent(
        self, repo: str, cycle: maintenance_state.MaintenanceCycle,
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

    def prepare_file_assessment_artifact(
        self, repo: str, repository: Path, base: str, context: Any, worktree: Path, result: Any,
    ) -> Any: ...

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

    def fetch_base(self, repo_path: Path, base: str) -> str: ...

    def save_planning_correction(
        self, repo: str, worktree: Path, target: str,
        paths: tuple[str, ...], was_rebase: bool,
    ) -> None: ...

    def planning_correction(
        self, repo: str, worktree: Path, target: str,
    ) -> tuple[tuple[str, ...], bool] | None: ...

    def restore_code_map_conflict(
        self, repo: str, worktree: Path, stash_sha: str = "",
    ) -> None: ...

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

    def rebase_planning_workspace(
        self, repo: str, fingerprint: str, worktree: Path,
        old_base: str, target: str,
    ) -> Any: ...

    def complete_planning_rebase(
        self, repo: str, worktree: Path, target: str,
    ) -> None: ...

    def accept_resolved_stash_conflict(
        self, repo: str, worktree: Path, target: str,
    ) -> None: ...

    def relevant_document_changes(
        self, worktree: Path, old: str, target: str,
        edited_paths: list[str], catalog_rows: Sequence[Any],
        target_plan_ids: Sequence[str],
    ) -> tuple[str, ...]: ...

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
        *, profile_role: str | None = None, read_only: bool = False,
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
    assessment: AssessmentPort


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



@dataclass(frozen=True)
class PlanningRunSnapshot:
    """Immutable state transferred between lifecycle services."""
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
    dirty_files: tuple[str, ...] | None = None
    planner_scope_error: str | None = None
    maintenance_decision: MaintenancePlannerDecision | None = None
    validated_result: Any = None
    error_input_changed: bool = False
    publication_recovery_verified: bool = False

@dataclass(frozen=True)
class PlanningStepResult:
    snapshot: PlanningRunSnapshot
    outcome: PlanningOutcome | None

def snapshot_run(run: _PlanningRun) -> PlanningRunSnapshot:
    values = {name: getattr(run, name) for name in PlanningRunSnapshot.__dataclass_fields__}
    if values["dirty_files"] is not None:
        values["dirty_files"] = tuple(values["dirty_files"])
    return PlanningRunSnapshot(**values)

def local_run(snapshot: PlanningRunSnapshot) -> _PlanningRun:
    values = {name: getattr(snapshot, name) for name in _PlanningRun.__dataclass_fields__}
    if values["dirty_files"] is not None:
        values["dirty_files"] = list(values["dirty_files"])
    return _PlanningRun(**values)
