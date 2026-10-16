"""application project service boundaries and named composition effects."""
from __future__ import annotations
from loop_manager_internal.runtime import operation_events as events
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from loop_manager_internal.agents.agent_profiles import AGENT_PROVIDERS
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents.scope_contracts import ManagerGitCheckpoint
from loop_manager_internal.catalog import project_catalog_refresh
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from loop_manager_internal.execution.execution_runtime import ExecutionPhaseCoordinator
from loop_manager_internal.planning.planning_runtime import PlanningContext
from loop_manager_internal.planning.planning_runtime import PlanningLifecycleCoordinator
from loop_manager_internal.planning.planning_runtime import PlanningOutcome
from loop_manager_internal.planning.planning_runtime import PlanningOutcomeKind
from loop_manager_internal.recovery import project_blocked_recovery
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.runtime.contracts import BaseWorktreeSyncResult
from loop_manager_internal.runtime.contracts import PROJECT_STEP_RETRY
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.runtime.contracts import RuntimeMutation
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.storage import execplan_catalog_state
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.recovery_state import RecoveryRepository
from pathlib import Path
import sqlite3
import sys
import threading


@dataclass(frozen=True)
class RunProjectStepEffects:
    """Named effects for _run_project_step; no entry-point/module access."""
    ExecutionPhaseCoordinator: Callable[..., ExecutionPhaseCoordinator]
    ManagerGitCheckpoint: type[ManagerGitCheckpoint]
    _ExecutionLifecyclePorts: Callable[..., object]
    _PlanningLifecyclePorts: Callable[..., object]
    _canonical_path: Callable[..., object]
    _check_runtime_handoff: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _ensure_cleanup_pending_schema: Callable[..., object]
    _ensure_execplans_schema: Callable[..., object]
    _execplan_archive_housekeeping_safe: Callable[..., object]
    _execution_outcome_step_result: Callable[..., object]
    _handoff_observed_self_merge: Callable[..., object]
    _journaled_manager_base_fetch: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _planning_outcome_step_result: Callable[..., object]
    _project_blocked_recovery_effects: Callable[..., object]
    _project_pr_merge_confirmed: Callable[..., object]
    _publish_direct_rebase_checkpoint: Callable[..., object]
    _record_manager_base_fetch_intent: Callable[..., object]
    _record_project_recovery: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    _self_runtime_project: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    active_execplan: Callable[..., object]
    backfill_plan_shas: Callable[..., object]
    block_active_execplan_from_revision: Callable[..., object]
    block_persisted_agent_scope_violation: Callable[..., object]
    dispatch_execplan_archive_housekeeping: Callable[..., object]
    ensure_safe_base_worktree: Callable[..., object]
    git: Callable[..., object]
    hydrate_active_project: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    log_base_worktree_sync: Callable[..., object]
    migrate_legacy_execplans: Callable[..., object]
    observe_blocked_plan_remotes: Callable[..., object]
    observe_waiting_pull_requests: Callable[..., object]
    operation_print: Callable[..., object]
    pr_for_branch: Callable[..., object]
    recheck_cleanup_pending: Callable[..., object]
    reconcile_execplan_catalog: Callable[..., object]
    reconcile_existing_pull_request: Callable[..., object]
    reconcile_false_positive_runtime_block: Callable[..., object]
    refresh_persisted_agent_scope_after_manager_git: Callable[..., object]
    remote_base: Callable[..., object]
    resume_interrupted_blocked_agent_recovery: Callable[..., object]
    resume_plan_blocks: Callable[..., object]
    resume_project_block_if_safe: Callable[..., object]
    synchronize_waiting_pull_requests: Callable[..., object]
    update_project: Callable[..., object]
    validate_persisted_agent_scope: Callable[..., object]
    validate_repository: Callable[..., object]
    resume_manual_work: Callable[..., object] | None = None


@dataclass(frozen=True)
class ProjectIdentity:
    connection: sqlite3.Connection
    state_dir: Path
    repo_arg: str
    config_path: Path
    requested_base: str | None
    allow_agents_mismatch: bool
    requested_provider: str | None
    registered_only: bool
    planner_mutex: threading.Lock | None
    refactoring_review_interval: int
    verify_timeout_seconds: int
    runtime_mutation: RuntimeMutation | None
    recovery_request_id: str | None = None
    recovery_plan_id: str | None = None


@dataclass
class ProjectObservations:
    """One project invocation's observations, never process-global state."""
    repo_path: Path
    repo: str = ""
    row: sqlite3.Row | None = None
    require_self_pr_base: bool = False
    provider: str = "codex"
    cleanup_candidates: frozenset[str] = frozenset()
    checkpoint_cleanup_candidates: frozenset[str] = frozenset()
    validated_agent_scope: GitSnapshot | None = None
    current_origin: str | None = None
    base: str = ""
    base_sha: str = ""
    catalog_refresh: project_catalog_refresh.ProjectCatalogRefresh | None = None
    history_revision: str | None = None
    recovered_planning_outcome: PlanningOutcome | None = None
    manager_worktree_changes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProjectStepResult:
    code: int


class ProjectService:
    """Apply safe project boundaries, then delegate selection and phase transitions."""

    def __init__(self, effects: RunProjectStepEffects, identity: ProjectIdentity):
        self.effects = effects
        self.identity = identity
        self.state = ProjectObservations(Path(identity.repo_arg))

    def run(self) -> ProjectStepResult:
        steps = [
            ("prepare_repository", "프로젝트 준비", self._prepare_repository),
            ("reconcile_catalog_and_cleanup", "catalog 반영·정리", self._reconcile_catalog_and_cleanup),
        ]
        if self.identity.recovery_request_id is None:
            steps.append(("synchronize_pull_requests", "PR 상태 동기화", self._synchronize_pull_requests))
            steps.append(("resume_manual_work", "수동 작업 재개 확인", self._resume_manual_work))
        else:
            steps.append(("recheck_local_plan_blocks", "차단 재검사", self._recheck_local_plan_blocks))
        steps.extend((("resume_existing_execution", "기존 작업 복구", self._resume_existing_execution),
                      ("run_coordinators", "계획·작업 처리", self._run_coordinators)))
        for stage, label, step in steps:
            try:
                with events.log_context(stage=stage, error_level="WARN"), events.activity(
                        label, heartbeat=stage in {"prepare_repository", "reconcile_catalog_and_cleanup", "synchronize_pull_requests"}) as report:
                    result = step()
                    report.update(result="다음 경계로 진행" if result is None else result)
            except recovery_runtime.RecoveryFailure as error:
                if error.reason not in recovery_runtime.TRANSIENT | recovery_runtime.RECONCILE:
                    raise
                self.effects._record_project_recovery(self.identity.connection, self.state.repo,
                                                      error.reason, str(error))
                return ProjectStepResult(PROJECT_STEP_RETRY)
            if result is not None:
                return ProjectStepResult(result)
        raise AssertionError("execution coordinator must return a project result")

    def _resume_manual_work(self):
        if self.effects.resume_manual_work is not None:
            self.effects.resume_manual_work(self.identity.connection, self.identity.state_dir,
                self.state.repo_path, self.identity.config_path, self.identity.runtime_mutation)
            self.state.row = self.identity.connection.execute('SELECT * FROM projects WHERE repo=?',
                (self.state.repo,)).fetchone()

    def _prepare_repository(self) -> int | None:
        self.effects._check_runtime_handoff()
        # Resolve the persisted project key without consulting repository config.
        # An interrupted Agent may have changed origin or hooksPath, so the saved
        # snapshot must be checked before normal repository validation reads it.
        self.state.repo_path = Path(self.identity.repo_arg).expanduser().resolve()
        self.state.repo = str(self.state.repo_path)
        if not self.identity.registered_only:
            project_registry.ensure_registry_schema(self.identity.connection)
            self.effects._ensure_execplans_schema(self.identity.connection)
        self.state.row = self.identity.connection.execute(
            "SELECT * FROM projects WHERE repo = ?",
            (self.state.repo,),
        ).fetchone()
        if self.state.row is not None and int(self.state.row["registered"]) != 1:
            raise RuntimeError(f"등록되지 않은 프로젝트입니다: {self.state.repo}")
        if self.state.row is None and self.identity.registered_only:
            raise RuntimeError(f"등록되지 않은 프로젝트입니다: {self.state.repo}")
        if self.identity.recovery_request_id and self.identity.recovery_plan_id is not None:
            active = runtime_state.RuntimeStateRepository(self.identity.connection).active_row(
                self.state.repo,
            )
            if active is not None and str(active["plan_id"]) != self.identity.recovery_plan_id:
                self.effects.operation_print(
                    "다른 계획이 프로젝트 실행 자리를 사용 중이어서 선택한 계획을 재검사하지 않았습니다: "
                    + str(active["plan_id"]), file=sys.stderr,
                )
                return 1
        self.state.require_self_pr_base = self.effects._self_runtime_project(
            self.identity.connection, self.state.repo, self.identity.state_dir, self.identity.runtime_mutation,
        ) is not None
        self.state.provider = str(self.state.row["provider"]) if self.state.row else (self.identity.requested_provider or "codex")
        if self.state.provider not in AGENT_PROVIDERS:
            raise RuntimeError(f"지원하지 않는 Agent provider입니다: {self.state.provider}")
        if (
            self.state.row is not None
            and self.identity.requested_provider is not None
            and self.identity.requested_provider != self.state.provider
        ):
            raise RuntimeError(
                f"등록된 provider는 {self.state.provider}이며 {self.identity.requested_provider}로 변경할 수 없습니다."
            )
        if self.state.row is not None:
            self.effects.observe_blocked_plan_remotes(
                self.identity.connection, self.state.repo_path, self.state.repo,
                self.state.row,
                **({"plan_id": self.identity.recovery_plan_id}
                   if self.identity.recovery_request_id and self.identity.recovery_plan_id
                   else {}),
            )
        self.effects._ensure_cleanup_pending_schema(self.identity.connection)
        initial_project_block = (
            scoped_blocking.project_block(self.identity.connection, self.state.repo) if self.state.row is not None else None
        )
        if self.state.row is not None and initial_project_block is not None and self.identity.runtime_mutation is not None:
            self.effects.reconcile_false_positive_runtime_block(
                self.identity.connection, self.state.repo_path, self.identity.state_dir, self.state.repo, self.state.row,
                initial_project_block, self.identity.runtime_mutation,
            )
        if (
            self.state.row is not None
            and initial_project_block is not None
            and initial_project_block["reason_code"]
            != BlockReasonCode.EXECPLAN_CATALOG_INVALID.value
        ):
            if self.identity.recovery_request_id:
                self.effects.operation_print(
                    "프로젝트 안전 차단은 로컬 재검사 명령으로 해제하지 않습니다: "
                    + str(initial_project_block["reason_code"]), file=sys.stderr,
                )
                return 1
            self.effects.observe_waiting_pull_requests(
                self.identity.connection, self.state.repo_path, self.identity.state_dir, self.state.repo,
                require_base_identity=self.state.require_self_pr_base, read_only=True,
                config_path=self.identity.config_path,
            )
            self.effects.resume_project_block_if_safe(self.identity.connection, self.state.repo_path, self.identity.state_dir, self.state.repo)
            project_block = scoped_blocking.project_block(self.identity.connection, self.state.repo)
            if project_block is not None and project_block["plan_id"]:
                reconciler = project_blocked_recovery.ProjectBlockedReconciler(
                    self.effects._project_blocked_recovery_effects(), self.identity.connection, self.state.repo_path, self.identity.state_dir, self.state.repo,
                    str(self.state.row["base_branch"]), self.identity.config_path, self.identity.verify_timeout_seconds,
                )
                linked_plan_id = str(project_block["plan_id"])
                if reconciler.refresh_catalog(linked_plan_id):
                    reconciler.reconcile(linked_plan_id)
                project_block = scoped_blocking.project_block(self.identity.connection, self.state.repo)
            if project_block is not None:
                self.effects.operation_print(
                    "프로젝트 안전 차단 상태입니다: "
                    + str(project_block["evidence"]),
                    file=sys.stderr,
                )
                return 1
            self.state.row = self.identity.connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
            ).fetchone()
        self.state.cleanup_candidates = frozenset(
            str(self.effects._canonical_path(Path(item["worktree"])))
            for item in self.identity.connection.execute(
                "SELECT worktree FROM cleanup_pending "
                "WHERE repo = ? AND workspace_owned = ?",
                (self.state.repo, WORKSPACE_OWNED),
            ).fetchall()
        )
        self.state.checkpoint_cleanup_candidates = self.state.cleanup_candidates
        active_plan = self.effects.active_execplan(self.identity.connection, self.state.repo)
        active_plan_matches_recovery = (
            self.identity.recovery_request_id is None
            or self.identity.recovery_plan_id is None
            or active_plan is not None
            and str(active_plan["plan_id"]) == self.identity.recovery_plan_id
        )
        if self.state.row and self.state.row["agent_recovery"] and active_plan_matches_recovery:
            try:
                self.effects.resume_interrupted_blocked_agent_recovery(
                    self.identity.connection,
                    self.state.repo_path,
                    self.identity.state_dir,
                    self.state.repo,
                )
            except (AgentScopeError, RuntimeError) as error:
                recovery_error = f"BLOCKED Agent 중단 복구 실패:\n{error}"
                plan = self.effects.active_execplan(self.identity.connection, self.state.repo)
                if plan is not None:
                    self.effects._update_blocked_error(
                        self.identity.connection,
                        self.state.repo,
                        str(plan["plan_id"]),
                        recovery_error,
                    )
                else:
                    self.effects.update_project(self.identity.connection, self.state.repo, last_error=recovery_error)
                self.effects.operation_print(recovery_error, file=sys.stderr)
                return 1
            self.state.row = self.identity.connection.execute(
                "SELECT * FROM projects WHERE repo = ?",
                (self.state.repo,),
            ).fetchone()
        self.state.validated_agent_scope = None
        saved_scope = bool(self.state.row and self.state.row['agent_snapshot'])
        if self.state.row and self.state.row['worktree'] and not saved_scope:
            recovery = RecoveryRepository(self.identity.connection)
            plan_id = self.effects._plan_id_from_path(str(self.state.row['plan_path'] or ''))
            saved_scope = (recovery.has_git_scope_failure(self.state.repo, plan_id)
                           or recovery.has_git_scope_failure(self.state.repo, None))
        if saved_scope:
            planner_correction = bool(
                self.state.row["phase"] == "PLANNING" and self.state.row["planning_validation_error"]
            )
            try:
                self.state.validated_agent_scope = self.effects.validate_persisted_agent_scope(
                    self.identity.connection,
                    self.state.repo_path,
                    self.identity.state_dir,
                    self.state.repo,
                    allow_planner_worktree_correction=planner_correction,
                    manager_cleanup_worktrees=self.state.cleanup_candidates,
                    **({"runtime_mutation": self.identity.runtime_mutation} if self.identity.runtime_mutation is not None else {}),
                )
            except recovery_runtime.RecoveryFailure as error:
                if error.reason is not recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE:
                    raise
                self.effects._record_project_recovery(
                    self.identity.connection, self.state.repo, error.reason, str(error),
                    attempts=3,
                )
                return PROJECT_STEP_RETRY
            except AgentScopeError as error:
                detail = str(error)
                scope_result_snapshot = error.result_snapshot
                scope_reason = error.reason_code
            except WorkspaceOwnershipError as error:
                detail = str(error)
                scope_result_snapshot = None
                scope_reason = BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN
            except RuntimeError as error:
                detail = f"현재 작업 공간을 확인할 수 없습니다: {error}"
                scope_result_snapshot = None
                scope_reason = BlockReasonCode.REPOSITORY_STATE_UNSAFE
            else:
                detail = None
                scope_result_snapshot = None
                scope_reason = BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
            if detail is not None:
                message = self.effects.block_persisted_agent_scope_violation(
                    self.identity.connection,
                    self.state.repo_path,
                    self.state.repo,
                    self.state.row,
                    detail,
                    result_snapshot=scope_result_snapshot,
                    reason=scope_reason,
                )
                self.effects.operation_print(message, file=sys.stderr)
                return 1
            if self.state.validated_agent_scope is not None:
                if self.state.row["agent_manager_snapshot"]:
                    try:
                        self.state.checkpoint_cleanup_candidates |= self.effects.ManagerGitCheckpoint.deserialize(
                            str(self.state.row["agent_manager_snapshot"])
                        ).cleanup_worktrees
                    except (ValueError, TypeError, KeyError, RuntimeError):
                        pass  # Historical audit is not a current operation precondition.
                self.effects.update_project(
                    self.identity.connection,
                    self.state.repo,
                    agent_manager_snapshot=self.effects.ManagerGitCheckpoint(
                        self.state.validated_agent_scope,
                        self.state.checkpoint_cleanup_candidates,
                    ).serialize(),
                )
        validated_origins: list[str] = []
        if self.state.row is not None:
            self.state.repo_path = self.effects.validate_repository(
                self.state.repo_path,
                allow_agents_mismatch=False,
                provider=self.state.provider,
                config_path=self.identity.config_path,
                state_dir=self.identity.state_dir,
                registration_checks=False,
                expected_origin=(str(self.state.row["origin"]) if self.state.row["origin"] else None),
                origin_result=validated_origins,
            )
            self.state.current_origin = (
                str(self.state.row["origin"])
                if self.state.row["origin"]
                else (validated_origins[0] if validated_origins else None)
            )
            if self.state.row["origin"] is None and self.state.current_origin is not None:
                with self.identity.connection:
                    self.identity.connection.execute(
                        "UPDATE projects SET origin = ? WHERE repo = ?",
                        (self.state.current_origin, self.state.repo),
                    )
                self.state.row = self.identity.connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
                ).fetchone()
        else:
            self.state.repo_path = self.effects.validate_repository(
                self.state.repo_path,
                allow_agents_mismatch=self.identity.allow_agents_mismatch,
                provider=self.state.provider,
                config_path=self.identity.config_path,
                state_dir=self.identity.state_dir,
                origin_result=validated_origins,
            )
            self.state.current_origin = validated_origins[0] if validated_origins else None

    def _reconcile_catalog_and_cleanup(self) -> int | None:
        selected_base = self.identity.requested_base or (self.state.row["base_branch"] if self.state.row else None)
        fetch_intent: int | None = None
        if (
            self.state.row is not None and self.state.validated_agent_scope is not None
            and self.state.row["phase"] == "PLAN_BLOCKED"
            and selected_base == self.state.row["base_branch"] and self.state.row["plan_path"]
        ):
            plan_id = self.effects._plan_id_from_path(str(self.state.row["plan_path"]))
            if plan_id:
                waiting_plan = self.identity.connection.execute(
                    "SELECT blocked_reason FROM exec_plans WHERE repo = ? AND plan_id = ?",
                    (self.state.repo, plan_id),
                ).fetchone()
                if waiting_plan and waiting_plan["blocked_reason"] == GUIDANCE_BLOCK_REASON:
                    fetch_intent = self.effects._record_manager_base_fetch_intent(
                        self.identity.connection, self.state.repo_path, self.state.repo, plan_id,
                        str(selected_base), self.state.validated_agent_scope,
                    )
        if fetch_intent is not None:
            self.state.base = self.effects._journaled_manager_base_fetch(
                self.identity.connection, self.state.repo_path, self.identity.state_dir, fetch_intent, str(selected_base),
                str(plan_id),
            )
        else:
            self.state.base = self.effects.remote_base(self.state.repo_path, selected_base, state_dir=self.identity.state_dir)
        self.state.catalog_refresh = project_catalog_refresh.ProjectCatalogRefresh(
            self.identity.connection, self.state.repo,
            lambda: self.effects.git(self.state.repo_path, "rev-parse", "--verify",
                        f"refs/remotes/origin/{self.state.base}^{{commit}}").stdout.strip(),
            lambda revision, remember: self.effects.reconcile_execplan_catalog(
                self.identity.connection, self.state.repo_path, self.state.repo, self.state.base, remote_sha=revision,
                on_reconciled=remember,
            ),
        )

        self.state.history_revision = None





        self.state.recovered_planning_outcome = None
        self.state.row = self.identity.connection.execute("SELECT * FROM projects WHERE repo = ?", (self.state.repo,)).fetchone()
        if self.state.row is not None and execplan_catalog_state.DefinitionStateRepository(
            self.identity.connection
        ).has_publication_intent(self.state.repo):
            if self.identity.recovery_request_id and self.identity.recovery_plan_id is not None:
                self.effects.operation_print(
                    "계획별 재검사에서는 planning 게시 복구를 실행하지 않습니다.",
                    file=sys.stderr,
                )
                return 1
            # The owning publication must recover its manifest before the catalog
            # can classify new IDs. No PR/archive/selection consumer runs here.
            context = PlanningContext(
                repo=self.state.repo, repo_path=self.state.repo_path, state_dir=self.identity.state_dir, base_branch=self.state.base,
                base_sha=self.state.catalog_refresh.current_revision(), provider=self.state.provider,
                config_path=self.identity.config_path, registered_only=self.identity.registered_only,
                refactoring_review_interval=self.identity.refactoring_review_interval,
                self_repository=self.identity.registered_only and self.effects.is_loop_manager_repository(self.state.repo_path),
                refresh_catalog=self._refresh_catalog,
                local_environment_recheck=bool(self.identity.recovery_request_id),
                recovery_request_id=self.identity.recovery_request_id,
            )
            self.state.recovered_planning_outcome = PlanningLifecycleCoordinator(self.effects._PlanningLifecyclePorts(
                self.identity.connection, runtime_state.RuntimeStateRepository(self.identity.connection),
                maintenance_state.MaintenanceRepository(self.identity.connection), context, self.identity.planner_mutex,
            )).run(context)
            recovery_step_result = self.effects._planning_outcome_step_result(self.state.recovered_planning_outcome)
            if recovery_step_result is not None:
                return recovery_step_result
            # Publication may finish and select its Worker in this same turn.
            # Reconcile the published catalog before consumers, then carry the
            # outcome to execution without selecting or planning a second time.
            recovered_project = self.identity.connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (self.state.repo,),
            ).fetchone()
            if recovered_project is not None and not recovered_project["agent_snapshot"]:
                # The coordinator completed and cleared the validated Planner
                # scope; it no longer belongs to the selected Worker workspace.
                self.state.validated_agent_scope = None
        try:
            self.state.base_sha = self._refresh_catalog()
        except project_catalog_refresh.CatalogRefreshStopped as error:
            return self._catalog_stop(error)
        self.effects.backfill_plan_shas(self.identity.connection, target_repo=self.state.repo)
        self.state.row = self.identity.connection.execute("SELECT * FROM projects WHERE repo = ?", (self.state.repo,)).fetchone()
        if self.state.row:
            self.effects.hydrate_active_project(self.identity.connection, self.state.repo)
            if (
                self.effects.active_execplan(self.identity.connection, self.state.repo) is None
                and self.state.row["phase"] in {"PR_OPEN", "PLAN_BLOCKED", "BLOCKED"}
                and not runtime_state.planning_owns_execution_slot(self.state.row)
                and scoped_blocking.project_block(self.identity.connection, self.state.repo) is None
                and self.state.validated_agent_scope is None
            ):
                with self.identity.connection:
                    self.effects._clear_active_in_current_transaction(self.identity.connection, self.state.repo)
            self.state.row = self.identity.connection.execute("SELECT * FROM projects WHERE repo = ?", (self.state.repo,)).fetchone()
        self.effects._ensure_cleanup_pending_schema(self.identity.connection)
        self.state.cleanup_candidates = frozenset(
            str(self.effects._canonical_path(Path(item["worktree"])))
            for item in self.identity.connection.execute(
                "SELECT worktree FROM cleanup_pending "
                "WHERE repo = ? AND workspace_owned = ?",
                (self.state.repo, WORKSPACE_OWNED),
            ).fetchall()
        )
        if (self.identity.recovery_request_id is not None
                or self.state.row is not None and runtime_state.planning_owns_execution_slot(self.state.row)):
            cleanup_pending = self.identity.connection.execute(
                "SELECT * FROM cleanup_pending WHERE repo = ?", (self.state.repo,)
            ).fetchall()
        else:
            cleanup_pending = self.effects.recheck_cleanup_pending(
                self.identity.connection, self.state.repo_path, self.state.repo, self.identity.state_dir
            )
        remaining_cleanup = frozenset(
            str(self.effects._canonical_path(Path(item["worktree"])))
            for item in cleanup_pending
        )
        self.state.manager_worktree_changes = self.state.checkpoint_cleanup_candidates - remaining_cleanup
        for pending in cleanup_pending:
            self.effects.operation_print(
                f"보존된 worktree: {pending['worktree']} ({pending['reason']})",
                file=sys.stderr,
            )

    def _synchronize_pull_requests(self) -> int | None:
        try:
            pr_sync = self.effects.synchronize_waiting_pull_requests(
                self.identity.connection, self.state.repo_path, self.identity.state_dir, self.state.repo,
                after_reconciliation=self._synchronize_source_after_pr_reconciliation,
                require_base_identity=self.state.require_self_pr_base,
                config_path=self.identity.config_path,
                after_observation=lambda observation: self.effects._handoff_observed_self_merge(
                    self.identity.connection, self.state.repo, self.identity.state_dir, self.identity.runtime_mutation, observation,
                ),
            )
            self.state.base_sha = self._refresh_catalog()
        except project_catalog_refresh.CatalogRefreshStopped as error:
            return self._catalog_stop(error)
        if pr_sync.step_result not in {None, 0}:
            return pr_sync.step_result
        self.state.row = self.identity.connection.execute("SELECT * FROM projects WHERE repo = ?", (self.state.repo,)).fetchone()
        project_block = scoped_blocking.project_block(self.identity.connection, self.state.repo)
        if project_block is not None:
            self.effects.resume_project_block_if_safe(
                self.identity.connection, self.state.repo_path, self.identity.state_dir, self.state.repo
            )
            project_block = scoped_blocking.project_block(self.identity.connection, self.state.repo)
        if project_block is not None:
            self.effects.operation_print(
                "프로젝트 안전 차단 상태입니다: "
                + str(project_block["evidence"]),
                file=sys.stderr,
            )
            return 1
        if (
            self.state.row is not None
            and int(self.state.row["registered"]) == 1
            and self.state.row["phase"] is None
            and self.effects.active_execplan(self.identity.connection, self.state.repo) is None
        ):
            repository = maintenance_state.MaintenanceRepository(self.identity.connection)
            state = repository.get(self.state.repo)
            if (
                state.phase is maintenance_state.MaintenancePhase.IDLE
                and state.file_assessment_start_merged_count is None
            ):
                current_sha = self.effects.git(
                    self.state.repo_path, "rev-parse", "--verify",
                    f"refs/remotes/origin/{self.state.base}^{{commit}}",
                ).stdout.strip()
                if current_sha != self.state.base_sha:
                    return PROJECT_STEP_RETRY
                repository.initialize_file_assessment_start(
                    self.state.repo, source_sha=self.state.base_sha,
                    expected_merged_count=repository.merged_count(self.state.repo),
                )
        if pr_sync.step_result not in {None, 0}:
            return pr_sync.step_result
        if self.state.validated_agent_scope is not None:
            try:
                self.state.validated_agent_scope = self.effects.refresh_persisted_agent_scope_after_manager_git(
                    self.identity.connection,
                    self.state.repo,
                    self.state.validated_agent_scope,
                    self.state.manager_worktree_changes,
                    state_dir=self.identity.state_dir,
                )
            except (AgentScopeError, RuntimeError) as error:
                self.state.row = self.identity.connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
                ).fetchone()
                message = self.effects.block_persisted_agent_scope_violation(
                    self.identity.connection,
                    self.state.repo_path,
                    self.state.repo,
                    self.state.row,
                    str(error),
                    reason=getattr(
                        error,
                        "reason_code",
                        BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                    ),
                )
                self.effects.operation_print(message, file=sys.stderr)
                return 1
        return self._recheck_local_plan_blocks()

    def _recheck_local_plan_blocks(self) -> int | None:
        """Use the same block policy without synchronizing unrelated PRs."""
        try:
            self.effects.resume_plan_blocks(
                self.identity.connection, self.state.repo_path, self.identity.state_dir,
                self.state.repo, self.state.base, self.identity.config_path,
                self.identity.verify_timeout_seconds,
                **({
                    "plan_id": self.identity.recovery_plan_id,
                    "force_timeout_recheck": True,
                    "recovery_request_id": self.identity.recovery_request_id,
                } if self.identity.recovery_request_id else {}),
            )
        except recovery_runtime.RecoveryFailure as error:
            if error.reason is not recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE:
                raise
            self.effects._record_project_recovery(
                self.identity.connection, self.state.repo, error.reason, str(error),
                attempts=3,
            )
            return PROJECT_STEP_RETRY
        if scoped_blocking.project_block(self.identity.connection, self.state.repo) is not None:
            return 1
        self.state.row = self.identity.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
        ).fetchone()

    def _resume_existing_execution(self) -> int | None:
        if (self.identity.recovery_request_id
                and self.identity.recovery_plan_id is not None):
            active = runtime_state.RuntimeStateRepository(
                self.identity.connection,
            ).active_row(self.state.repo)
            if active is None or str(active["plan_id"]) != self.identity.recovery_plan_id:
                return None
        if (
            self.state.row and self.state.row["branch"]
            and not runtime_state.planning_owns_execution_slot(self.state.row)
            and self.effects._project_pr_merge_confirmed(self.identity.connection, self.state.repo, self.state.row)
        ):
            # MERGED is durable even if its compatibility projection survived
            # the transition. Release that execution slot without changing the
            # plan's audit, sessions, worktree or pending cleanup evidence.
            if self.effects.active_execplan(self.identity.connection, self.state.repo) is None:
                with self.identity.connection:
                    self.effects._clear_active_in_current_transaction(self.identity.connection, self.state.repo)
            else:
                self.effects.hydrate_active_project(self.identity.connection, self.state.repo)
            self.state.row = self.identity.connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
            ).fetchone()

        if (self.state.row and self.state.row["branch"] and not runtime_state.planning_owns_execution_slot(self.state.row)
                and not (self.state.recovered_planning_outcome is not None
                         and self.state.recovered_planning_outcome.newly_activated)):
            pending_result = self._recover_pending_pr_checkpoint()
            if pending_result is not None:
                return pending_result
            existing_pr = self.effects.pr_for_branch(self.state.repo_path, self.state.row["branch"], self.state.row["pr_url"])
            selected_id = self.effects._plan_id_from_path(str(self.state.row['plan_path'] or ''))
            if selected_id:
                from loop_manager_internal.recovery.deleted_branch_restart import current_pr
                checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.identity.connection).get(self.state.repo, selected_id)
                accepted_pr = current_pr(checkpoint, existing_pr)
                if (accepted_pr is None and existing_pr and self.state.row['pr_url']
                        and (checkpoint.snapshot or {}).get('fresh_remote_creation')
                        and existing_pr.get('headRefName') == self.state.row['branch']
                        and existing_pr.get('baseRefName') == self.state.row['base_branch']):
                    runtime_state.RuntimeStateRepository(self.identity.connection).update_project(
                        self.state.repo, {'pr_url': None, 'pr_head_oid': None})
                    self.state.row = self.identity.connection.execute('SELECT * FROM projects WHERE repo=?', (self.state.repo,)).fetchone()
                existing_pr = accepted_pr
            from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
            manual = ManualWorkRepository(self.identity.connection).for_plan(
                self.state.repo, self.effects._plan_id_from_path(str(self.state.row['plan_path'] or '')))
            defer_manual_pr = bool(manual and manual['state']=='AUTOMATIC'
                                   and self.state.row['phase']!='PUBLISHING')
            if existing_pr and not defer_manual_pr:
                try:
                    reconciliation = self.effects.reconcile_existing_pull_request(
                        self.identity.connection,
                        self.state.repo_path,
                        self.identity.state_dir,
                        self.state.repo,
                        self.state.row,
                        existing_pr,
                        config_path=self.identity.config_path,
                    )
                    if reconciliation is not None:
                        return reconciliation
                    self.state.row = self.identity.connection.execute(
                        "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
                    ).fetchone()
                except PublicationSafetyError as error:
                    publication_error = f"기존 PR 복구 거부: {error}"
                    self.effects.block_active_execplan_from_revision(
                        self.identity.connection,
                        self.state.repo_path,
                        self.state.repo,
                        f"refs/remotes/origin/{self.state.row['base_branch']}",
                        publication_error,
                        str(self.state.row["verify_result"] or ""),
                        reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                    )
                    self.effects.operation_print(publication_error, file=sys.stderr)
                    return 1

        if not self.state.row:
            self.identity.connection.execute(
                "INSERT INTO projects (repo, base_branch, provider, origin) "
                "VALUES (?, ?, ?, ?)",
                (self.state.repo, self.state.base, self.state.provider, self.state.current_origin),
            )
            self.identity.connection.commit()
            self.effects.migrate_legacy_execplans(
                self.identity.connection,
                reconcile_history=True,
                target_repo=self.state.repo,
                ensure_schema=not self.identity.registered_only,
            )
            self.state.row = self.identity.connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
            ).fetchone()
        elif not self.state.row["phase"]:
            self.effects.update_project(self.identity.connection, self.state.repo, base_branch=self.state.base)
            self.state.row = self.identity.connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
            ).fetchone()

    def _recover_pending_pr_checkpoint(self) -> int | None:
        """Confirm intermediate publication before checking the saved PR head."""
        selected = runtime_state.RuntimeStateRepository(self.identity.connection).projected(self.state.repo)
        if selected is None or not selected.pr_url:
            return None
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.identity.connection).get(
            self.state.repo, selected.plan_id)
        if checkpoint.operation != 'rebase_publish':
            return None
        try:
            self.effects._publish_direct_rebase_checkpoint(
                self.identity.connection, self.state.repo_path, self.identity.state_dir,
                self.state.repo, selected.plan_id, Path(selected.worktree), selected.branch)
        except recovery_runtime.RecoveryFailure as error:
            if error.reason is recovery_runtime.RecoveryReason.PUBLICATION_RETRY:
                self.effects._record_project_recovery(
                    self.identity.connection, self.state.repo, error.reason, str(error))
                return PROJECT_STEP_RETRY
            # GitHub terminal state takes priority over an unfinished receipt.
            # Reobserve after refusal as the PR may have ended during push.
            try:
                pull_request = self.effects.pr_for_branch(
                    self.state.repo_path, selected.branch, selected.pr_url)
            except (OSError, RuntimeError) as observation_error:
                self.effects._record_project_recovery(
                    self.identity.connection, self.state.repo,
                    recovery_runtime.RecoveryReason.PUBLICATION_RETRY,
                    str(observation_error))
                return PROJECT_STEP_RETRY
            if (pull_request is not None
                    and pull_request.get('url') == selected.pr_url
                    and pull_request.get('headRefName') == selected.branch
                    and pull_request.get('baseRefName', self.state.row['base_branch'])
                    == self.state.row['base_branch']
                    and str(pull_request.get('state') or '').upper() in {'CLOSED', 'MERGED'}):
                return self.effects.reconcile_existing_pull_request(
                    self.identity.connection, self.state.repo_path, self.identity.state_dir,
                    self.state.repo, self.state.row, pull_request,
                    config_path=self.identity.config_path)
            self.effects.block_active_execplan_from_revision(
                self.identity.connection, self.state.repo_path, self.state.repo,
                f"refs/remotes/origin/{self.state.row['base_branch']}", str(error),
                str(self.state.row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN)
            self.effects.operation_print(str(error), file=sys.stderr)
            return 1
        self.state.row = self.identity.connection.execute(
            'SELECT * FROM projects WHERE repo = ?', (self.state.repo,)).fetchone()
        return None

    def _run_coordinators(self) -> int | None:
        assert self.state.row is not None
        self.effects._recorded_base_fetch(self.state.repo_path, self.identity.state_dir, self.state.base, owner="project")
        try:
            self.state.base_sha = self._refresh_catalog()
        except project_catalog_refresh.CatalogRefreshStopped as error:
            return self._catalog_stop(error)
        runtime_repository = runtime_state.RuntimeStateRepository(self.identity.connection)
        maintenance_repository = maintenance_state.MaintenanceRepository(self.identity.connection)
        if self.identity.recovery_request_id:
            active = runtime_repository.active_row(self.state.repo)
            if self.identity.recovery_plan_id is not None:
                target = self.identity.connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = ?",
                    (self.state.repo, self.identity.recovery_plan_id),
                ).fetchone()
                if target is None:
                    self.effects.operation_print(
                        f"선택한 계획이 더 이상 존재하지 않습니다: {self.identity.recovery_plan_id}",
                        file=sys.stderr,
                    )
                    return 1
                if active is not None and str(active["plan_id"]) != self.identity.recovery_plan_id:
                    self.effects.operation_print(
                        "다른 계획이 프로젝트 실행 자리를 사용 중이어서 재검사하지 않았습니다: "
                        + str(active["plan_id"]), file=sys.stderr,
                    )
                    return 1
                if active is None:
                    self.effects.operation_print(
                        f"선택한 계획에 실행할 로컬 복구 단계가 없습니다: {target['status']}",
                    )
                    return 0
            elif active is None and not (
                self.state.row["planning_error"]
                or runtime_state.planning_owns_execution_slot(self.state.row)
                or self.state.recovered_planning_outcome is not None
            ):
                self.effects.operation_print(
                    "실행 중인 로컬 복구 단계가 없습니다. 프로젝트 환경과 저장된 차단 원인을 확인했습니다."
                )
                return 0
            if self.identity.recovery_plan_id is not None or (
                active is not None and not runtime_state.planning_owns_execution_slot(self.state.row)
            ):
                return self._dispatch_execution(runtime_repository, newly_activated=False)
        planning_context = PlanningContext(
            repo=self.state.repo,
            repo_path=self.state.repo_path,
            state_dir=self.identity.state_dir,
            base_branch=self.state.base,
            base_sha=self.state.base_sha,
            provider=self.state.provider,
            config_path=self.identity.config_path,
            registered_only=self.identity.registered_only,
            refactoring_review_interval=self.identity.refactoring_review_interval,
            self_repository=self.identity.registered_only and self.effects.is_loop_manager_repository(self.state.repo_path),
            refresh_catalog=self._refresh_catalog,
            local_environment_recheck=bool(self.identity.recovery_request_id),
            recovery_request_id=self.identity.recovery_request_id,
        )
        self.effects._check_runtime_handoff()
        if self.state.recovered_planning_outcome is not None:
            planning_outcome = replace(self.state.recovered_planning_outcome, base_sha=self.state.base_sha)
        else:
            planning_outcome = PlanningLifecycleCoordinator(
                self.effects._PlanningLifecyclePorts(
                    self.identity.connection,
                    runtime_repository,
                    maintenance_repository,
                    planning_context,
                    self.identity.planner_mutex,
                )
            ).run(planning_context)
        if planning_outcome.kind in {
            PlanningOutcomeKind.REMOTE_BASE_RESTART,
            PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
            PlanningOutcomeKind.TRANSIENT_PAUSE,
        }:
            reason = (
                recovery_runtime.RecoveryReason.SERVICE_UNAVAILABLE
                if planning_outcome.kind is PlanningOutcomeKind.TRANSIENT_PAUSE
                else recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE
                if planning_outcome.kind is PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED
                else recovery_runtime.RecoveryReason.BASE_BEHIND
            )
            self.effects._record_project_recovery(
                self.identity.connection, self.state.repo, reason,
                planning_outcome.detail or planning_outcome.kind.value,
                attempts=(
                    3 if planning_outcome.kind in {
                        PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED,
                        PlanningOutcomeKind.TRANSIENT_PAUSE,
                    } else 0
                ),
            )
        planning_step_result = self.effects._planning_outcome_step_result(planning_outcome)
        if planning_step_result is not None:
            return planning_step_result
        self.state.base_sha = planning_outcome.base_sha or self.state.base_sha
        if self.identity.recovery_request_id:
            return 0
        newly_activated = planning_outcome.newly_activated
        return self._dispatch_execution(
            runtime_repository, newly_activated=newly_activated, planning_context=planning_context,
        )

    def _dispatch_execution(self, runtime_repository, *, newly_activated: bool, planning_context=None) -> int:
        execution_ports = self.effects._ExecutionLifecyclePorts(
            self.identity.connection,
            runtime_repository,
            repo=self.state.repo,
            repo_path=self.state.repo_path,
            state_dir=self.identity.state_dir,
            base=self.state.base,
            base_sha=self.state.base_sha,
            provider=self.state.provider,
            config_path=self.identity.config_path,
            verify_timeout_seconds=self.identity.verify_timeout_seconds,
            newly_activated=newly_activated,
        )
        execution = self.effects.ExecutionPhaseCoordinator(execution_ports)
        self.effects._check_runtime_handoff()
        prepared = execution.prepare()
        if isinstance(prepared, ExecutionOutcome):
            from loop_manager_internal.recovery import manual_work_recovery
            manual_work_recovery.release_wait(self.identity.connection,self.state.repo,prepared,
                self.identity.state_dir,self.effects.git)
            return self.effects._execution_outcome_step_result(prepared)
        execution_context = prepared
        while True:
            self.effects._check_runtime_handoff()
            execution_outcome = execution.dispatch(execution_context)
            if (
                execution_outcome.kind is ExecutionOutcomeKind.PHASE_ADVANCED
                and execution_outcome.context is not None
            ):
                execution_context = execution_outcome.context
                if planning_context is not None:
                    before_planning = runtime_repository.active(self.state.repo)
                    self.effects._recorded_base_fetch(self.state.repo_path, self.identity.state_dir,
                                                      self.state.base, owner='project')
                    self.state.base_sha = self._refresh_catalog()
                    planning_context = replace(planning_context, base_sha=self.state.base_sha)
                    planning = PlanningLifecycleCoordinator(self.effects._PlanningLifecyclePorts(
                        self.identity.connection, runtime_repository,
                        maintenance_state.MaintenanceRepository(self.identity.connection),
                        planning_context, self.identity.planner_mutex,
                    )).run(planning_context)
                    step = self.effects._planning_outcome_step_result(planning)
                    if step is not None:
                        return step
                    self.state.base_sha = planning.base_sha or self.state.base_sha
                    after_planning = runtime_repository.active(self.state.repo)
                    unchanged_execution = (before_planning is not None and after_planning is not None
                        and replace(after_planning, catalog_generation=before_planning.catalog_generation,
                                    current_revision_sha=before_planning.current_revision_sha) == before_planning)
                    if not unchanged_execution:
                        prepared = execution.prepare()
                        if isinstance(prepared, ExecutionOutcome):
                            return self.effects._execution_outcome_step_result(prepared)
                        execution_context = prepared
                continue
            from loop_manager_internal.recovery import manual_work_recovery
            manual_work_recovery.release_wait(self.identity.connection,self.state.repo,execution_outcome,
                self.identity.state_dir,self.effects.git)
            return self.effects._execution_outcome_step_result(execution_outcome)

    def _refresh_catalog(self, revision: str | None = None) -> str:
        projected = runtime_state.RuntimeStateRepository(self.identity.connection).projected_row(self.state.repo)
        revision = self.state.catalog_refresh.refresh(revision)
        if (projected is not None and str(projected["plan_id"]) in
                scoped_blocking.catalog_held_plan_ids(self.identity.connection, self.state.repo)):
            # The verified scope remains with its paused plan's runtime row.
            self.state.validated_agent_scope = None
        if self.state.history_revision != revision:
            self.effects.migrate_legacy_execplans(
                self.identity.connection, reconcile_history=True, target_repo=self.state.repo, ensure_schema=False,
                pinned_catalog=self.state.catalog_refresh.pinned,
            )
            self.state.history_revision = revision
            self.state.catalog_refresh.adopt_history()
        return revision

    def _catalog_stop(self, error) -> int:
        result = error.result
        if result.kind is execplan_catalog_state.ReconciliationKind.STALE:
            self.effects._record_project_recovery(
                self.identity.connection, self.state.repo, recovery_runtime.RecoveryReason.REMOTE_SNAPSHOT_RACE,
                "ExecPlan catalog snapshot이 적용 전에 변경되어 다시 조회합니다: "
                + str(result.revision_sha or ""),
            )
            return PROJECT_STEP_RETRY
        assert result.diagnostic is not None
        self.effects.operation_print(result.diagnostic.message, file=sys.stderr)
        return 1

    def _archive_housekeeping_safe(self) -> bool:
        return self.effects._execplan_archive_housekeeping_safe(
            self.identity.connection,
            self.state.repo,
            self.state.validated_agent_scope,
            maintenance_assessment_enabled=self.identity.registered_only,
            refactoring_review_interval=self.identity.refactoring_review_interval,
        )

    def _archive_housekeeping(self) -> None:
        if not self._archive_housekeeping_safe():
            return
        from loop_manager_internal.git import manager_git_journal
        if manager_git_journal.has_pending_project_mutation(self.identity.state_dir, self.state.repo):
            self.effects.operation_print('Requirement·archive 정리 보류: 기존 Manager Git 작업을 먼저 복구합니다.')
            return
        try:
            self.effects.dispatch_execplan_archive_housekeeping(
                self.identity.connection,
                self.state.repo_path,
                self.identity.state_dir,
                self.state.repo,
                self.state.base,
                catalog_refresh=self.state.catalog_refresh,
                archive_safety=self._archive_housekeeping_safe,
            )
        except project_catalog_refresh.CatalogRefreshStopped:
            raise
        except Exception as error:
            from loop_manager_internal.catalog.requirement_correction_publication import CorrectionPending
            if isinstance(error, CorrectionPending):
                raise
            # Archive maintenance is deliberately non-gating: MERGED state,
            # READY selection, planning, and completion continue unchanged.
            self.effects.operation_print(
                f"ExecPlan archive housekeeping 실패 (다음 실행에서 재시도): {error}",
                file=sys.stderr,
            )

    def _synchronize_source_after_pr_reconciliation(self) -> None:
        # PR observation can introduce a new ownership safety block.
        if scoped_blocking.project_block(self.identity.connection, self.state.repo) is not None:
            return
        # Read the durable project state after all PR transitions and cleanup.
        current_project = self.identity.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.state.repo,)
        ).fetchone()
        if current_project is not None and str(current_project["base_branch"]) != self.state.base:
            raise RuntimeError("PR 상태 동기화 중 프로젝트 base branch가 변경됐습니다.")
        self.effects._recorded_base_fetch(self.state.repo_path, self.identity.state_dir, self.state.base, owner="project")
        self.state.base_sha = self._refresh_catalog()
        self._archive_housekeeping()
        self.state.base_sha = self._refresh_catalog()
        if scoped_blocking.project_block(self.identity.connection, self.state.repo) is None:
            waiting_open = self.identity.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'PR_OPEN' LIMIT 1",
                (self.state.repo,),
            ).fetchone() is not None
            confirmed_merge = self.identity.connection.execute(
                "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'MERGED' LIMIT 1",
                (self.state.repo,),
            ).fetchone() is not None
            conflict_recovery = bool(
                current_project is not None
                and int(current_project["pr_conflict_recovery"])
            )
            if (waiting_open or conflict_recovery) and not confirmed_merge:
                sync_result = BaseWorktreeSyncResult(
                    "skipped",
                    "PR 충돌 감지/복구 중 source checkout 보존",
                    self.state.base_sha,
                )
            else:
                sync_result = self.effects.ensure_safe_base_worktree(
                    self.state.repo_path, self.state.base, remote_sha=self.state.base_sha, state_dir=self.identity.state_dir,
                )
            if isinstance(sync_result, BaseWorktreeSyncResult):
                self.effects.log_base_worktree_sync(self.state.repo_path, sync_result)


def _run_project_step(
    effects: RunProjectStepEffects, connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    runtime_mutation: RuntimeMutation | None = None,
    recovery_request_id: str | None = None,
    recovery_plan_id: str | None = None,
) -> int:
    """Integer compatibility adapter for the typed project service result."""
    identity = ProjectIdentity(
        connection=connection,
        state_dir=state_dir,
        repo_arg=repo_arg,
        config_path=config_path,
        requested_base=requested_base,
        allow_agents_mismatch=allow_agents_mismatch,
        requested_provider=requested_provider,
        registered_only=registered_only,
        planner_mutex=planner_mutex,
        refactoring_review_interval=refactoring_review_interval,
        verify_timeout_seconds=verify_timeout_seconds,
        runtime_mutation=runtime_mutation,
        recovery_request_id=recovery_request_id,
        recovery_plan_id=recovery_plan_id,
    )
    return ProjectService(effects, identity).run().code
