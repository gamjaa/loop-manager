"""storage project state adapter boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import hashlib
import json
import sqlite3


ACTIVE_EXECPLAN_STATUSES = runtime_state.ACTIVE_STATUSES


WAITING_EXECPLAN_STATUSES = frozenset({"PR_OPEN", "PLAN_BLOCKED"})


KNOWN_EXECPLAN_STATUSES = ACTIVE_EXECPLAN_STATUSES | WAITING_EXECPLAN_STATUSES | {
    "READY",
    "MERGED",
}


FIELDS = runtime_state.PROJECT_UPDATE_FIELDS


@dataclass(frozen=True)
class UpdateProjectEffects:
    """Named effects for update_project; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]


def update_project(effects: UpdateProjectEffects, connection: sqlite3.Connection, repo: str, **values: object) -> None:
    if not values or not set(values).issubset(FIELDS):
        raise ValueError("유효하지 않은 프로젝트 상태 필드입니다.")
    current = connection.execute(
        "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if current is None:
        notifications.enqueue_current_project_events(connection, repo)
        connection.commit()
        return
    if (
        current["plan_path"]
        and not ("phase" in values and values["phase"] is None)
    ):
        effects._ensure_execplans_schema(connection)
    runtime_state.RuntimeStateRepository(connection).update_project(repo, values)
    connection.commit()


@dataclass(frozen=True)
class ActivateReadyExecplanEffects:
    """Named effects for activate_ready_execplan; no entry-point/module access."""
    assessment_can_yield: Callable[..., object]
    git: Callable[..., object]
    is_unowned_ready: Callable[..., object]
    select_ready_execplan: Callable[..., object]


def activate_ready_execplan(
    effects: ActivateReadyExecplanEffects, connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    base_sha: str,
) -> sqlite3.Row | None:
    selected = effects.select_ready_execplan(connection, repo)
    if selected is None:
        return None
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    branch = str(selected["branch"] or f"loop-manager/{selected['plan_id']}")
    worktree = (
        Path(str(selected["worktree"]))
        if selected["worktree"]
        else state_dir / "worktrees" / project_id / str(selected["plan_id"])
    )
    activation_sha = (
        effects.git(worktree, "rev-parse", "HEAD").stdout.strip()
        if worktree.is_dir()
        else base_sha
    )
    try:
        runtime_state.RuntimeStateRepository(connection).activate(
            repo,
            str(selected["plan_id"]),
            activation_sha=activation_sha,
            branch=branch,
            worktree=str(worktree),
            preserve_worker_session=(
                effects.assessment_can_yield(
                    maintenance_state.MaintenanceRepository(connection).get(repo)
                )
                and not effects.is_unowned_ready(selected)
            ),
        )
    except runtime_state.RuntimeStateError as error:
        raise RuntimeError(
            "READY ExecPlan 선택 상태가 변경되었습니다."
        ) from error
    connection.commit()
    return connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, selected["plan_id"]),
    ).fetchone()


@dataclass(frozen=True)
class HydrateActiveProjectEffects:
    """Named effects for hydrate_active_project; no entry-point/module access."""
    active_execplan: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]


def hydrate_active_project(effects: HydrateActiveProjectEffects, connection: sqlite3.Connection, repo: str) -> None:
    """Refresh the legacy execution mirror from the plan-state source of truth."""

    if not runtime_state.RuntimeStateRepository(connection).recovery_slot_available(repo):
        return
    plan = effects.active_execplan(connection, repo)
    if plan is None:
        return
    effects.hydrate_execplan_project(connection, repo, plan)


def hydrate_execplan_project(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
) -> None:
    """Hydrate one explicitly selected plan into the compatibility mirror."""

    if connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is None:
        connection.commit()
        return
    snapshot = runtime_state.ExecutionSnapshot.from_row(plan)
    if snapshot.repo != repo:
        snapshot = replace(snapshot, repo=repo)
    runtime_state.RuntimeStateRepository(connection).hydrate_snapshot(
        repo, snapshot
    )
    connection.commit()


@dataclass(frozen=True)
class MirrorProjectUpdateToExecplanEffects:
    """Named effects for _mirror_project_update_to_execplan; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]


def _mirror_project_update_to_execplan(
    effects: MirrorProjectUpdateToExecplanEffects, connection: sqlite3.Connection,
    repo: str,
    current: sqlite3.Row | None,
    values: dict[str, object],
) -> None:
    plan_path = (
        str(current["plan_path"])
        if current is not None and current["plan_path"]
        else None
    )
    compatible_values = {
        field: value for field, value in values.items() if field in FIELDS
    }
    if not compatible_values:
        return
    if plan_path and not ("phase" in values and values["phase"] is None):
        effects._ensure_execplans_schema(connection)
    runtime_state.RuntimeStateRepository(connection).mirror_project_values(
        repo, plan_path, compatible_values
    )


@dataclass(frozen=True)
class ClearPlanningValidationStateEffects:
    """Named effects for clear_planning_validation_state; no entry-point/module access."""
    update_project: Callable[..., object]


def clear_planning_validation_state(
    effects: ClearPlanningValidationStateEffects, connection: sqlite3.Connection,
    repo: str,
) -> None:
    effects.update_project(
        connection,
        repo,
        planning_attempt_fingerprint=None,
        planning_retry_count=0,
        planner_session_id=None,
        worker_session_id=None,
        worker_session_binding=None,
        worker_session_transition=None,
        reviewer_session_id=None,
        worker_result=None,
        verify_result=None,
        review_result=None,
        planning_validation_error=None,
        planning_result_pending_validation=0,
        planning_result_completed=0,
        planning_result=None,
        planning_error=None,
        planning_error_fingerprint=None,
        planning_error_incident_id=None,
        planning_error_origin=None,
        planning_correction_error=None,
        planning_correction_incident_id=None,
        planning_correction_sha=None,
        planning_correction_context=None,
        last_error=None,
        agent_role=None,
        agent_snapshot=None,
        agent_input_fingerprint=None,
        agent_baseline=None,
        agent_recovery=None,
        agent_input_plan=None,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )


def _clear_active_values(
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> dict[str, object]:
    return runtime_state.clear_project_values(
        processed_sha=processed_sha,
        specification_fingerprint=specification_fingerprint,
        planning_fingerprint=planning_fingerprint,
    )


def _clear_active_in_current_transaction(
    connection: sqlite3.Connection,
    repo: str,
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    try:
        runtime_state.RuntimeStateRepository(connection).clear(
            repo,
            processed_sha=processed_sha,
            specification_fingerprint=specification_fingerprint,
            planning_fingerprint=planning_fingerprint,
            enqueue=False,
        )
    except runtime_state.RuntimeStateError as error:
        raise RuntimeError(
            "project planning 상태를 원자적으로 정리할 수 없습니다."
        ) from error


def clear_active(
    connection: sqlite3.Connection,
    repo: str,
    *,
    processed_sha: str | None = None,
    specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    if connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone() is None:
        connection.commit()
        return
    runtime_state.RuntimeStateRepository(connection).clear(
        repo,
        processed_sha=processed_sha,
        specification_fingerprint=specification_fingerprint,
        planning_fingerprint=planning_fingerprint,
        enqueue=True,
    )
    connection.commit()


def _execplan_input_paths(plan: sqlite3.Row) -> list[str]:
    sources = json.loads(plan["sources"])
    return [str(plan["path"]), *(str(source) for source in sources)]
