"""git merge cleanup service boundaries and named composition effects."""
from __future__ import annotations
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.application.contracts import CleanupAssessment
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WORKSPACE_UNOWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import json
import sqlite3


@dataclass(frozen=True)
class RecordMergedWorktreeCleanupEffects:
    """Named effects for _record_merged_worktree_cleanup; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _ensure_cleanup_pending_schema: Callable[..., object]
    capture_git_snapshot: Callable[..., object]


def _record_merged_worktree_cleanup(
    effects: RecordMergedWorktreeCleanupEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    plan_id: str,
    worktree: Path,
    branch: str,
    pr_head_oid: str | None,
    assessment: CleanupAssessment,
    registration: tuple[str, ...] | None,
) -> None:
    """Save only a completed removal of a validated Merge workspace."""
    if assessment.status != "safe" or not registration or not pr_head_oid:
        return
    key = str(effects._canonical_path(worktree))
    if (
        f"branch=refs/heads/{branch}" not in registration
        or effects.capture_git_snapshot(repo_path).worktrees.get(key) is not None
        or worktree.exists() or worktree.is_symlink()
    ):
        raise WorkspaceOwnershipError("Merge cleanup 뒤 worktree 제거를 확인할 수 없습니다.")
    effects._ensure_cleanup_pending_schema(connection)
    connection.execute(
        "INSERT OR REPLACE INTO merged_worktree_cleanups "
        "(repo, plan_id, worktree, branch, registration, pr_head_oid) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (repo, plan_id, key, branch, json.dumps(registration), pr_head_oid),
    )


@dataclass(frozen=True)
class ProvenMergedCleanupPathsEffects:
    """Named effects for _proven_merged_cleanup_paths; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _ensure_cleanup_pending_schema: Callable[..., object]


def _proven_merged_cleanup_paths(
    effects: ProvenMergedCleanupPathsEffects, connection: sqlite3.Connection,
    repo: str,
    before: GitSnapshot,
    current: GitSnapshot,
) -> frozenset[str]:
    """Allow only the exact registration removed by a recorded Merge cleanup."""
    effects._ensure_cleanup_pending_schema(connection)
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'exec_plans'"
    ).fetchone() is None:
        return frozenset()
    proven: set[str] = set()
    for row in connection.execute(
        "SELECT c.*, p.status, p.branch AS plan_branch, p.worktree AS plan_worktree, "
        "p.workspace_owned, p.pr_head_oid AS plan_pr_head_oid "
        "FROM merged_worktree_cleanups c JOIN exec_plans p "
        "ON p.repo = c.repo AND p.plan_id = c.plan_id WHERE c.repo = ?",
        (repo,),
    ):
        path = str(row["worktree"])
        try:
            registration = tuple(json.loads(str(row["registration"])))
        except (TypeError, ValueError):
            continue
        if (
            row["status"] == "MERGED"
            and int(row["workspace_owned"] or 0) == WORKSPACE_OWNED
            and str(row["plan_branch"] or "") == row["branch"]
            and str(effects._canonical_path(Path(str(row["plan_worktree"] or "")))) == path
            and row["pr_head_oid"] == row["plan_pr_head_oid"]
            and before.worktrees.get(path) == registration
            and current.worktrees.get(path) is None
            and not Path(path).exists()
            and not Path(path).is_symlink()
        ):
            proven.add(path)
    return frozenset(proven)


@dataclass(frozen=True)
class CompleteMergedWorktreeEffects:
    """Named effects for complete_merged_worktree; no entry-point/module access."""
    _assess_missing_managed_cleanup: Callable[..., object]
    _canonical_path: Callable[..., object]
    _clear_active_values: Callable[..., object]
    _ensure_cleanup_pending_schema: Callable[..., object]
    _ensure_execplans_schema: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _record_merged_worktree_cleanup: Callable[..., object]
    _store_cleanup_pending: Callable[..., object]
    assess_cleanup: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    remove_cleanup_worktree: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    pr_for_branch: Callable[..., object] | None = None


def complete_merged_worktree(
    effects: CompleteMergedWorktreeEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
    worktree: Path,
    pr_url: str,
    pr_head_oid: str | None,
) -> CleanupAssessment:
    project = connection.execute(
        "SELECT plan_path, branch FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        raise WorkspaceOwnershipError("Merge cleanup 프로젝트를 찾을 수 없습니다.")
    plan_id = effects._plan_id_from_path(str(project["plan_path"] or ""))
    if plan_id is None:
        raise WorkspaceOwnershipError("Merge cleanup ExecPlan ID를 확인할 수 없습니다.")
    branch = str(project["branch"] or "")
    effects._ensure_execplans_schema(connection)
    plan = connection.execute(
        "SELECT * FROM exec_plans "
        "WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is None:
        raise WorkspaceOwnershipError(
            f"Merge cleanup ExecPlan을 찾을 수 없습니다: {plan_id}"
        )
    from loop_manager_internal.recovery import manual_work_recovery
    from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
    manual_store=ManualWorkRepository(connection)
    manual_work_recovery.validate_merge(connection,repo,plan,pr_url,pr_head_oid,
                                       pr_for_branch=effects.pr_for_branch)
    workspace_owned = WORKSPACE_OWNED
    cleanup_registration: tuple[str, ...] | None = None
    worktree_present = worktree.exists() or worktree.is_symlink()
    if not worktree_present:
        assessment = effects._assess_missing_managed_cleanup(state_dir, repo_path, worktree)
    elif (
        int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        workspace_owned = 0
        assessment = CleanupAssessment(
            "unverifiable",
            "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}",
        )
    else:
        try:
            effects.validate_execplan_workspace_identity(
                state_dir,
                repo,
                manual_workspace_owner(connection, repo, plan_id),
                branch,
                worktree,
            )
            effects.validate_managed_worktree(repo_path, worktree, branch)
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_owned = 0
            assessment = CleanupAssessment(
                "unverifiable",
                f"Merge cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = effects.assess_cleanup(worktree, pr_head_oid)
            if assessment.status == "safe":
                cleanup_registration = effects.capture_git_snapshot(repo_path).worktrees.get(
                    str(effects._canonical_path(worktree))
                )
            assessment = effects.remove_cleanup_worktree(
                repo_path, worktree, assessment, state_dir=state_dir,
                owner=f"plan:{plan_id}",
            )
    with connection:
        effects._ensure_execplans_schema(connection)
        effects._ensure_cleanup_pending_schema(connection)
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            effects._store_cleanup_pending(
                connection,
                repo,
                worktree,
                pr_url,
                pr_head_oid,
                assessment.reason or assessment.status,
                workspace_owned=workspace_owned,
            )
        project_values = effects._clear_active_values()
        runtime_state.RuntimeStateRepository(connection).mark_merged(
            repo,
            plan_id,
            project_values=project_values,
            enqueue=False,
        )
        manual_store.mark_group_merged(repo,plan_id)
        effects._record_merged_worktree_cleanup(
            connection, repo_path, repo, plan_id, worktree, branch,
            pr_head_oid, assessment, cleanup_registration,
        )
        notifications.record_merged_plan(connection, repo, plan_id, str(plan["title"]))
    return assessment


@dataclass(frozen=True)
class CompleteMergedExecplanWorktreeEffects:
    """Named effects for complete_merged_execplan_worktree; no entry-point/module access."""
    _assess_missing_managed_cleanup: Callable[..., object]
    _canonical_path: Callable[..., object]
    _clear_active_values: Callable[..., object]
    _ensure_cleanup_pending_schema: Callable[..., object]
    _record_merged_worktree_cleanup: Callable[..., object]
    _store_cleanup_pending: Callable[..., object]
    assess_cleanup: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    remove_cleanup_worktree: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    pr_for_branch: Callable[..., object] | None = None


def complete_merged_execplan_worktree(
    effects: CompleteMergedExecplanWorktreeEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    state_dir: Path,
    plan: sqlite3.Row,
    pr_url: str,
    pr_head_oid: str | None,
) -> CleanupAssessment:
    """Complete exactly one merge-wait plan without hydrating another plan."""

    from loop_manager_internal.recovery import manual_work_recovery
    from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
    manual_store = ManualWorkRepository(connection)
    manual_work_recovery.validate_merge(connection,repo,plan,pr_url,pr_head_oid,
                                       pr_for_branch=effects.pr_for_branch)

    plan_id = str(plan['plan_id'])
    def record_merge(current_connection):
        current = current_connection.execute(
            'SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id),
        ).fetchone()
        identity_fields = ('path', 'content_hash', 'catalog_generation', 'branch',
                           'worktree', 'workspace_owned', 'worker_session_id')
        if (current is None or any(current[key] != plan[key] for key in identity_fields)
                or current['status'] not in {plan['status'], 'MERGED'}
                or current['pr_url'] and current['pr_url'] != pr_url):
            raise WorkspaceOwnershipError('Merge 관측 뒤 ExecPlan identity/소유 상태가 변경됐습니다.')
        effects._ensure_cleanup_pending_schema(current_connection)
        effects._store_cleanup_pending(
            current_connection, repo, Path(str(current['worktree'] or '')),
            pr_url, pr_head_oid, 'Merge 확인; worktree 정리 대기',
            workspace_owned=int(current['workspace_owned']),
        )
        mirrored = runtime_state.project_row(current_connection, repo)
        project_values = (effects._clear_active_values()
            if mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
            and mirrored['plan_path'] == current['path'] else {})
        runtime_state.RuntimeStateRepository(current_connection).mark_merged(
            repo, plan_id, project_values=project_values, enqueue=False,
            pr_url=pr_url, pr_head_oid=pr_head_oid,
        )
        manual_store.mark_group_merged(repo, plan_id)
        scoped_blocking.clear_plan_block(current_connection, repo, plan_id)
        notifications.record_merged_plan(current_connection, repo, plan_id, str(current['title']))
        notifications.enqueue_current_project_events(current_connection, repo)

    from loop_manager_internal.storage.db_access import CoordinatedConnection
    if isinstance(connection, CoordinatedConnection):
        connection.run_request(record_merge)
    else:
        with connection:
            record_merge(connection)

    effects._ensure_cleanup_pending_schema(connection)
    branch = str(plan["branch"] or "")
    worktree = Path(str(plan["worktree"] or ""))
    workspace_owned = WORKSPACE_OWNED
    cleanup_registration: tuple[str, ...] | None = None
    # Merge recording is plan-scoped; managed worktree cleanup can wait until
    # the planning attempt releases the shared repository mutation slot.
    planning_owns_slot = not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo)
    if planning_owns_slot:
        assessment = CleanupAssessment("deferred", "planning 실행 자리 보존")
    elif not (worktree.exists() or worktree.is_symlink()):
        assessment = effects._assess_missing_managed_cleanup(state_dir, repo_path, worktree)
    elif int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        workspace_owned = WORKSPACE_UNOWNED
        assessment = CleanupAssessment(
            "unverifiable",
            "Merge cleanup worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}",
        )
    else:
        try:
            effects.validate_execplan_workspace_identity(
                state_dir,
                repo,
                manual_store.workspace_owner(repo,str(plan['plan_id'])),
                branch,
                worktree,
            )
            effects.validate_managed_worktree(repo_path, worktree, branch)
        except (RuntimeError, WorkspaceOwnershipError) as error:
            workspace_owned = WORKSPACE_UNOWNED
            assessment = CleanupAssessment(
                "unverifiable",
                f"Merge cleanup workspace 소유권 검증 실패: {error}",
            )
        else:
            assessment = effects.assess_cleanup(worktree, pr_head_oid)
            if assessment.status == "safe":
                cleanup_registration = effects.capture_git_snapshot(repo_path).worktrees.get(
                    str(effects._canonical_path(worktree))
                )
            assessment = effects.remove_cleanup_worktree(
                repo_path,
                worktree,
                assessment,
                state_dir=state_dir,
                owner=f"plan:{plan['plan_id']}",
            )

    effects._record_merged_worktree_cleanup(
        connection, repo_path, repo, plan_id, worktree, branch,
        pr_head_oid, assessment, cleanup_registration,
    )
    with connection:
        if assessment.status in {"safe", "missing"}:
            connection.execute(
                "DELETE FROM cleanup_pending WHERE repo = ? AND worktree = ?",
                (repo, str(worktree)),
            )
        else:
            effects._store_cleanup_pending(
                connection,
                repo,
                worktree,
                pr_url,
                pr_head_oid,
                assessment.reason or assessment.status,
                workspace_owned=workspace_owned,
            )
        if assessment.status == "unverifiable":
            scoped_blocking.record_project_block(
                connection,
                repo,
                BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
                str(assessment.reason or assessment.status),
                None,
                plan_id=str(plan["plan_id"]),
                previous_phase=None,
            )
        notifications.enqueue_current_project_events(connection, repo)
    return assessment
