"""application project runner boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import PROJECT_STEP_TRANSIENT_PAUSE
from loop_manager_internal.application.contracts import PROJECT_STEP_USAGE_PAUSE
from loop_manager_internal.application.contracts import ProjectRunKind
from loop_manager_internal.application.contracts import ProjectRunResult
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.runtime.contracts import PROJECT_STEP_PLANNING_PENDING
from loop_manager_internal.runtime.contracts import PROJECT_STEP_RETRY
from loop_manager_internal.runtime.contracts import RuntimeMutation
from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import sqlite3
import threading


@dataclass(frozen=True)
class RunProjectResultEffects:
    """Named effects for run_project_result; no entry-point/module access."""
    _blocked_recovery_subjects: Callable[..., object]
    _run_project_result: Callable[..., object]


def run_project_result(
    effects: RunProjectResultEffects, connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    *,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    on_step_completed: Callable[[], None] | None = None,
    runtime_mutation: RuntimeMutation | None = None,
    external_run_id: str | None = None,
    recovery_request_id: str | None = None,
    recovery_plan_id: str | None = None,
) -> ProjectRunResult:
    """Run one scheduler invocation and project its external failure outcome."""

    repo = str(Path(repo_arg).expanduser().resolve())
    if not getattr(connection, "execplans_schema_ready", False):
        recovery_state.ensure_schema(connection)
    recovery_repository = recovery_state.RecoveryRepository(connection)
    run_id = recovery_state.begin_external_run(connection, external_run_id)
    # The TEMP run marker must not hold the transaction that later makes each
    # recovery observation durable on its own bounded state transition.
    connection.commit()
    outcome: ProjectRunResult | None = None
    recovery_active = runtime_state.RuntimeStateRepository(connection).active_row(repo) if recovery_request_id else None
    try:
        outcome = effects._run_project_result(
            connection, state_dir, repo_arg, config_path, requested_base,
            allow_agents_mismatch, requested_provider,
            registered_only=registered_only, planner_mutex=planner_mutex,
            refactoring_review_interval=refactoring_review_interval,
            verify_timeout_seconds=verify_timeout_seconds,
            on_step_completed=on_step_completed, runtime_mutation=runtime_mutation,
            **({
                "recovery_request_id": recovery_request_id,
                "recovery_plan_id": recovery_plan_id,
            } if recovery_request_id is not None else {}),
        )
        return outcome
    except SelfMergeRuntimeHandoff:
        outcome = ProjectRunResult(ProjectRunKind.RUNTIME_HANDOFF, 0)
        raise
    finally:
        # Failure observations are saved in their recovery transaction.  This
        # boundary only resolves an execution that reached a stable outcome.
        with connection:
            resolved_subjects = None
            if recovery_request_id is not None:
                # Local recovery may resolve only inspected subjects. Planning
                # errors are independent of the selected execution's outcome.
                if recovery_plan_id is not None:
                    subjects = {recovery_plan_id}
                    project = connection.execute("SELECT planning_error FROM projects WHERE repo = ?", (repo,)).fetchone()
                    if project is not None and not project[0]:
                        subjects.add("__project__")
                    resolved_subjects = frozenset(subjects)
                else:
                    project = connection.execute("SELECT planning_error FROM projects WHERE repo = ?", (repo,)).fetchone()
                    subjects = {str(recovery_active["plan_id"])} if recovery_active is not None else set()
                    current_active = runtime_state.RuntimeStateRepository(connection).active_row(repo)
                    if current_active is not None:
                        subjects.add(str(current_active["plan_id"]))
                    if project is not None and not project[0]:
                        subjects.add("__project__")
                    resolved_subjects = frozenset(subjects)
            recovery_repository.finish_external_run(
                repo, run_id,
                resolved=(
                    outcome is not None
                    and outcome.kind not in {
                        ProjectRunKind.TRANSIENT_PAUSE,
                        ProjectRunKind.USAGE_LIMIT_PAUSE,
                        ProjectRunKind.OPERATIONAL_ERROR,
                        ProjectRunKind.BLOCKED,
                        ProjectRunKind.RUNTIME_HANDOFF,
                    }
                ),
                resolved_subjects=resolved_subjects,
                blocked_subjects=(
                    frozenset() if outcome is not None
                    and outcome.kind is ProjectRunKind.RUNTIME_HANDOFF
                    else effects._blocked_recovery_subjects(connection, repo)
                ),
            )


@dataclass(frozen=True)
class InternalRunProjectResultEffects:
    """Named effects for _run_project_result; no entry-point/module access."""
    _checkpoint_dispatch_ready: Callable[..., object]
    _record_project_recovery: Callable[..., object]
    _run_project_step: Callable[..., object]
    _stable_work_reason: Callable[..., object]
    catalog_schedule_decision: Callable[..., object]
    dispatch_blocked_checkpoints: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    operation_log: Callable[..., object]


def _run_project_result(
    effects: InternalRunProjectResultEffects, connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    *,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
    on_step_completed: Callable[[], None] | None = None,
    runtime_mutation: RuntimeMutation | None = None,
    recovery_request_id: str | None = None,
    recovery_plan_id: str | None = None,
) -> ProjectRunResult:
    """Run retryable validation progress to a stable project-level outcome."""

    repo = str(Path(repo_arg).expanduser().resolve())
    checkpoint_attempts: set[str] = set()
    if not getattr(connection, "execplans_schema_ready", False):
        recovery_state.ensure_schema(connection)
    recovery_attempts: dict[str, int] = {}
    pause_detail: str | None = None

    def checkpoint_dispatch_scope() -> tuple[bool, str | None]:
        if recovery_request_id is None or recovery_plan_id is None:
            return True, None
        active = runtime_state.RuntimeStateRepository(connection).active_row(repo)
        if active is not None and str(active["plan_id"]) != recovery_plan_id:
            return False, recovery_plan_id
        return True, recovery_plan_id

    while True:
        dispatch_checkpoints, checkpoint_plan_id = checkpoint_dispatch_scope()
        if dispatch_checkpoints and effects._checkpoint_dispatch_ready(connection, repo):
            project_before = connection.execute(
                "SELECT phase, pr_conflict_recovery FROM projects WHERE repo = ?",
                (repo,),
            ).fetchone()
            schedule_before = effects.catalog_schedule_decision(connection, repo)
            maintenance_before = maintenance_state.MaintenanceRepository(
                connection
            ).get(repo)
            if (
                schedule_before.active is not None
                or schedule_before.runnable_ready is not None
                or project_before["phase"] not in {
                    None, "PR_OPEN", "PLAN_BLOCKED", "PROJECT_BLOCKED",
                }
                or project_before["pr_conflict_recovery"]
                or maintenance_before.phase is not maintenance_state.MaintenancePhase.IDLE
            ):
                notifications.mark_work_available(connection, repo)
            pending_before = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    connection
                ).pending(repo, plan_id=checkpoint_plan_id)
            )
            if any(
                request.request_identity not in checkpoint_attempts
                for request in pending_before
            ):
                effects.dispatch_blocked_checkpoints(
                    connection,
                    Path(repo),
                    state_dir,
                    repo,
                    plan_id=checkpoint_plan_id,
                    skip_identities=frozenset(checkpoint_attempts),
                )
                checkpoint_attempts.update(
                    request.request_identity for request in pending_before
                )
        try:
            recovery_before = {
                str(record["subject"]): int(record["generation"])
                for record in connection.execute(
                    "SELECT subject, generation FROM recovery_records WHERE repo = ?",
                    (repo,),
                )
            }
            step_arguments = (
                connection,
                state_dir,
                repo_arg,
                config_path,
                requested_base,
                allow_agents_mismatch,
                requested_provider,
                registered_only,
                planner_mutex,
                refactoring_review_interval,
                verify_timeout_seconds,
            )
            step_result = effects._run_project_step(
                *step_arguments,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
                **({
                    "recovery_request_id": recovery_request_id,
                    "recovery_plan_id": recovery_plan_id,
                } if recovery_request_id is not None else {}),
            )
        except GitLockError as error:
            effects._record_project_recovery(
                connection, repo, recovery_runtime.RecoveryReason.GIT_LOCK,
                str(error),
                attempts=3,
            )
            effects.operation_log(
                f"일시적 Git lock 충돌 ({repo_arg}): {error}",
                error=True,
            )
            return ProjectRunResult(
                ProjectRunKind.TRANSIENT_PAUSE,
                0,
                str(error),
            )
        if on_step_completed is not None:
            on_step_completed()
        if step_result == PROJECT_STEP_RETRY:
            for record in connection.execute(
                "SELECT subject, generation, identity, disposition, detail "
                "FROM recovery_records WHERE repo = ?", (repo,),
            ):
                if int(record["generation"]) <= recovery_before.get(str(record["subject"]), 0):
                    continue
                if record["disposition"] not in {"transient_retry", "state_reconcile"}:
                    continue
                identity = str(record["identity"])
                run_key = f"{record['subject']}|{record['disposition']}"
                recovery_attempts[run_key] = recovery_attempts.get(run_key, 0) + 1
                if recovery_attempts[run_key] >= 3:
                    with connection:
                        recovery_state.RecoveryRepository(connection).mark_paused(
                            repo, str(record["subject"]), identity,
                            int(record["generation"]), recovery_attempts[run_key],
                        )
                    step_result = PROJECT_STEP_TRANSIENT_PAUSE
                    pause_detail = str(record["detail"])
                    break
        dispatch_checkpoints, checkpoint_plan_id = checkpoint_dispatch_scope()
        if dispatch_checkpoints and effects._checkpoint_dispatch_ready(connection, repo):
            pending_after = (
                blocked_checkpoint_state.BlockedCheckpointRepository(
                    connection
                ).pending(repo, plan_id=checkpoint_plan_id)
            )
            if any(
                request.request_identity not in checkpoint_attempts
                for request in pending_after
            ):
                effects.dispatch_blocked_checkpoints(
                    connection,
                    Path(repo),
                    state_dir,
                    repo,
                    plan_id=checkpoint_plan_id,
                    skip_identities=frozenset(checkpoint_attempts),
                )
                checkpoint_attempts.update(
                    request.request_identity for request in pending_after
                )
            schedule = effects.catalog_schedule_decision(connection, repo)
            if (
                recovery_request_id is None
                and step_result not in {
                    PROJECT_STEP_TRANSIENT_PAUSE,
                    PROJECT_STEP_USAGE_PAUSE,
                    PROJECT_STEP_PLANNING_PENDING,
                }
                and schedule.active is None
                and schedule.runnable_ready is not None
                and not schedule.project_blocked
            ):
                project_after = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                if project_after is not None and not runtime_state.planning_owns_execution_slot(project_after):
                    step_result = PROJECT_STEP_RETRY
        if recovery_request_id is not None and step_result == PROJECT_STEP_RETRY:
            step_result = PROJECT_STEP_TRANSIENT_PAUSE
        if step_result != PROJECT_STEP_RETRY:
            break

    row = connection.execute(
        "SELECT * FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    detail = str(row["last_error"] or "") if row else None
    if step_result == PROJECT_STEP_TRANSIENT_PAUSE:
        return ProjectRunResult(ProjectRunKind.TRANSIENT_PAUSE, 0, pause_detail or detail)
    if step_result == PROJECT_STEP_USAGE_PAUSE:
        return ProjectRunResult(ProjectRunKind.USAGE_LIMIT_PAUSE, 0, detail)
    if step_result == PROJECT_STEP_PLANNING_PENDING:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    if row is not None and row["phase"] == "PROJECT_BLOCKED":
        project_block = scoped_blocking.project_block(connection, repo)
        reason = (
            f"PROJECT_BLOCKED {project_block['reason_code']}: {project_block['evidence']}"
            if project_block is not None else detail
        )
        effects.operation_log(str(reason), error=True)
        return ProjectRunResult(ProjectRunKind.BLOCKED, 1, reason)
    if row is not None and row["planning_error"]:
        reason = f"planning_error: {row['planning_error']}"
        if runtime_state.planning_owns_execution_slot(row):
            effects.operation_log(reason, error=True)
            return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, reason)
        if step_result == 0:
            effects.operation_log(reason)
            return ProjectRunResult(ProjectRunKind.WAITING, 0, reason)
    if recovery_request_id is not None and step_result != 0:
        return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, detail)
    snapshot = None
    if step_result == 0 or (step_result == 1 and connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status = 'PLAN_BLOCKED' "
        "LIMIT 1", (repo,),
    ).fetchone() is not None):
        snapshot = effects._stable_work_reason(
            connection, repo, refactoring_review_interval=refactoring_review_interval,
        )
    if snapshot is not None:
        reason = snapshot.describe()
        effects.operation_log(f"작업 선택 결과 ({repo}): {reason}")
        if snapshot.completed and step_result == 0:
            notifications.enqueue_completion_if_ready(connection, repo, snapshot)
            return ProjectRunResult(ProjectRunKind.COMPLETED, 0, reason)
        if not snapshot.completed:
            notifications.enqueue_waiting_reason(connection, repo, snapshot)
            return ProjectRunResult(ProjectRunKind.WAITING, 0, reason)
    plan_block = connection.execute(
        "SELECT evidence FROM execplan_blocks WHERE repo = ? "
        "ORDER BY updated_at DESC, plan_id LIMIT 1",
        (repo,),
    ).fetchone()
    if step_result != 0 and plan_block is not None:
        return ProjectRunResult(
            ProjectRunKind.WAITING,
            0,
            str(plan_block["evidence"]),
        )
    if step_result != 0:
        return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1, detail)
    if row is not None and row["phase"] is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    maintenance_repository = maintenance_state.MaintenanceRepository(connection)
    maintenance_cycle = maintenance_repository.get(repo)
    if (
        maintenance_cycle.phase is not maintenance_state.MaintenancePhase.IDLE
        or maintenance_repository.file_assessment_due(
            repo, refactoring_review_interval,
            self_repository=effects.is_loop_manager_repository(Path(repo)),
        ).required
    ):
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    unfinished = connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo "
        "AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id) LIMIT 1",
        (repo,),
    ).fetchone()
    if unfinished is not None:
        return ProjectRunResult(ProjectRunKind.WAITING, 0, detail)
    return ProjectRunResult(ProjectRunKind.WAITING, 0, detail or "Planner 확인 대기")


@dataclass(frozen=True)
class RunProjectEffects:
    """Named effects for run_project; no entry-point/module access."""
    run_project_result: Callable[..., object]


def run_project(
    effects: RunProjectEffects, connection: sqlite3.Connection,
    state_dir: Path,
    repo_arg: str,
    config_path: Path,
    requested_base: str | None,
    allow_agents_mismatch: bool,
    requested_provider: str | None = None,
    *,
    registered_only: bool = False,
    planner_mutex: threading.Lock | None = None,
    refactoring_review_interval: int = 20,
    verify_timeout_seconds: int = 600,
) -> int:
    """CLI-compatible adapter for the reusable structured project result."""

    return effects.run_project_result(
        connection,
        state_dir,
        repo_arg,
        config_path,
        requested_base,
        allow_agents_mismatch,
        requested_provider,
        registered_only=registered_only,
        planner_mutex=planner_mutex,
        refactoring_review_interval=refactoring_review_interval,
        verify_timeout_seconds=verify_timeout_seconds,
    ).exit_code
