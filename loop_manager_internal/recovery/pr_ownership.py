"""recovery pr ownership boundaries and named composition effects."""
from __future__ import annotations

from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from loop_manager_internal.storage import runtime_state
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.runtime.contracts import ManagedWorktreeConflictError
from loop_manager_internal.runtime.contracts import PR_CONFLICT_PREFLIGHT_BLOCK_REASON
from loop_manager_internal.runtime.contracts import PR_CONFLICT_PREFLIGHT_ERROR_PREFIX
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from pathlib import Path
import re
import sqlite3


@dataclass(frozen=True)
class FetchPrConflictRecoveryRefsEffects:
    """Named effects for _fetch_pr_conflict_recovery_refs; no entry-point/module access."""
    _recorded_base_fetch: Callable[..., object]


def _fetch_pr_conflict_recovery_refs(
    effects: FetchPrConflictRecoveryRefsEffects, repo_path: Path,
    state_dir: Path,
    base: str,
    branch: str,
) -> None:
    for ref in dict.fromkeys((base, branch)):
        effects._recorded_base_fetch(repo_path, state_dir, ref, owner="project")


@dataclass(frozen=True)
class ValidatePrConflictRecoveryIdentityEffects:
    """Named effects for _validate_pr_conflict_recovery_identity; no entry-point/module access."""
    _plan_id_from_path: Callable[..., object]
    project_execplan: Callable[..., object]


def _validate_pr_conflict_recovery_identity(
    effects: ValidatePrConflictRecoveryIdentityEffects, connection: sqlite3.Connection,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
    *,
    expected_status: str,
) -> tuple[sqlite3.Row, Path, str, str]:
    plan = effects.project_execplan(connection, repo)
    plan_id = effects._plan_id_from_path(str(project["plan_path"] or ""))
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    url = str(pull_request.get("url") or "")
    head_name = str(pull_request.get("headRefName") or "")
    head_oid = str(pull_request.get("headRefOid") or "")
    stored_url = str(project["pr_url"] or "")
    stored_head = str(project["pr_head_oid"] or "")
    if (
        plan is None
        or plan["status"] != expected_status
        or plan_id is None
        or str(plan["plan_id"]) != plan_id
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "PR 충돌 복구 workspace의 SQLite 소유권을 확인할 수 없습니다."
        )
    if not stored_url or url != stored_url:
        raise PublicationSafetyError(
            "PR 충돌 복구 URL이 저장된 소유 PR과 일치하지 않습니다."
        )
    if not branch or head_name != branch:
        raise PublicationSafetyError(
            "PR 충돌 복구 head branch가 관리 branch와 일치하지 않습니다."
        )
    if (
        not head_oid
        or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", head_oid) is None
    ):
        raise PublicationSafetyError(
            "PR 충돌 복구 head OID가 저장된 PR head와 일치하지 않습니다."
        )
    return plan, worktree, branch, head_oid


@dataclass(frozen=True)
class ValidatePrConflictRecoveryOwnershipEffects:
    """Named effects for _validate_pr_conflict_recovery_ownership; no entry-point/module access."""
    _ignored_paths_overlapping_git_update: Callable[..., object]
    _ignored_worktree_paths: Callable[..., object]
    _non_default_index_flags: Callable[..., object]
    _validate_pr_conflict_recovery_identity: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    make_worktree: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    remote_integration_effects: Callable[..., object] | None = None


def _validate_pr_conflict_recovery_ownership(
    effects: ValidatePrConflictRecoveryOwnershipEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
    *,
    expected_status: str = "PR_OPEN",
) -> tuple[sqlite3.Row, Path, str, str, str]:
    """Prove the saved URL, branch, workspace, and observed remote head."""

    plan, worktree, branch, head_oid = (
        effects._validate_pr_conflict_recovery_identity(
            connection,
            repo,
            project,
            pull_request,
            expected_status=expected_status,
        )
    )
    plan_id = str(plan["plan_id"])
    effects.validate_execplan_workspace_identity(
        state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree
    )
    effects.make_worktree(
        repo_path,
        worktree,
        branch,
        str(project["base_branch"]),
        journal_state_dir=state_dir,
        journal_owner=f"plan:{plan_id}",
        allow_create_branch=False,
        allow_recovery=True,
        allow_existing=True,
    )
    if effects.remote_integration_effects is not None:
        from loop_manager_internal.recovery.work_branch_integration import synchronize
        synchronize(effects.remote_integration_effects(), connection, repo_path, state_dir,
                    repo, plan, head_oid, pull_request)
    snapshot = effects.capture_git_snapshot(worktree)
    ignored_paths = frozenset(effects._ignored_worktree_paths(worktree))
    publishable_dirty = {
        path: state
        for path, state in snapshot.dirty.items()
        if path not in ignored_paths
    }
    flagged_paths = sorted(
        path for path, entries in snapshot.index.items()
        if effects._non_default_index_flags(entries)
    )
    if (
        snapshot.branch != branch or effects.git(worktree, 'merge-base', '--is-ancestor', head_oid, snapshot.head, check=False).returncode != 0
        or publishable_dirty or flagged_paths or snapshot.operations
    ):
        details = []
        if snapshot.branch != branch:
            details.append(f"branch {snapshot.branch} (expected {branch})")
        if snapshot.head != head_oid:
            details.append(f"HEAD {snapshot.head} (expected {head_oid})")
        if publishable_dirty:
            details.append(
                "게시 가능 변경 " + ", ".join(sorted(publishable_dirty)[:8])
            )
        if flagged_paths:
            details.append("index flags " + ", ".join(flagged_paths[:8]))
        if controlling_git_operation(snapshot.operations):
            details.append(
                "Git operation " + ", ".join(sorted(snapshot.operations))
            )
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 시작 전 관리 worktree가 관측한 원격 head의 "
            "clean 상태와 일치하지 않습니다: " + "; ".join(details)
        )
    remote_oid = effects.remote_branch_oid(repo_path, branch)
    if remote_oid != head_oid:
        raise PublicationSafetyError(
            "PR 충돌 복구 시작 전 원격 branch가 관측한 PR head와 "
            "일치하지 않습니다."
        )
    base_oid = effects.git(
        repo_path,
        "rev-parse",
        f"refs/remotes/origin/{project['base_branch']}^{{commit}}",
    ).stdout.strip()
    overlaps = effects._ignored_paths_overlapping_git_update(worktree, base_oid)
    if overlaps:
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 tracked 경로와 겹치는 ignored 파일: "
            + ", ".join(overlaps[:8])
        )
    return plan, worktree, branch, head_oid, base_oid


@dataclass(frozen=True)
class ValidateBlockedPrWithoutConflictEffects:
    """Named effects for _validate_blocked_pr_without_conflict; no entry-point/module access."""
    _ignored_worktree_paths: Callable[..., object]
    _non_default_index_flags: Callable[..., object]
    _validate_pr_conflict_recovery_identity: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def _validate_blocked_pr_without_conflict(
    effects: ValidateBlockedPrWithoutConflictEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> None:
    """Read-only proof for releasing an old conflict preflight false positive."""

    plan, worktree, branch, head_oid = effects._validate_pr_conflict_recovery_identity(
        connection, repo, project, pull_request, expected_status="PLAN_BLOCKED"
    )
    effects.validate_execplan_workspace_identity(
        state_dir, repo, manual_workspace_owner(connection, repo, str(plan["plan_id"])), branch, worktree
    )
    if not worktree.is_dir():
        raise WorkspaceOwnershipError("PR 충돌 복구 worktree가 존재하지 않습니다.")
    snapshot = effects.capture_git_snapshot(worktree)
    ignored = frozenset(effects._ignored_worktree_paths(worktree))
    dirty = sorted(path for path in snapshot.dirty if path not in ignored)
    flags = sorted(
        path for path, entries in snapshot.index.items()
        if effects._non_default_index_flags(entries)
    )
    if snapshot.branch != branch or snapshot.head != head_oid or dirty or flags or controlling_git_operation(snapshot.operations):
        raise ManagedWorktreeConflictError(
            "PR 충돌 복구 차단 해제 전 관리 worktree 상태 불일치: "
            + "; ".join(filter(None, (
                f"branch {snapshot.branch} (expected {branch})" if snapshot.branch != branch else "",
                f"HEAD {snapshot.head} (expected {head_oid})" if snapshot.head != head_oid else "",
                "게시 가능 변경 " + ", ".join(dirty[:8]) if dirty else "",
                "index flags " + ", ".join(flags[:8]) if flags else "",
                "Git operation " + ", ".join(sorted(snapshot.operations)) if snapshot.operations else "",
            )))
        )
    remote = effects.git(
        repo_path, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
    ).stdout.splitlines()
    if len(remote) != 1 or remote[0].split("\t", 1)[0] != head_oid:
        raise PublicationSafetyError(
            "PR 충돌 복구 차단 해제 전 원격 branch가 관측한 PR head와 일치하지 않습니다."
        )


def _is_pr_conflict_preflight_block(
    project: sqlite3.Row,
    plan: sqlite3.Row | None,
) -> bool:
    if plan is None:
        return False
    return bool(
        plan["blocked_reason"] == PR_CONFLICT_PREFLIGHT_BLOCK_REASON
        or str(project["last_error"] or "").startswith(
            PR_CONFLICT_PREFLIGHT_ERROR_PREFIX
        )
    )


@dataclass(frozen=True)
class ValidateActivePrConflictRecoveryEffects:
    """Named effects for _validate_active_pr_conflict_recovery; no entry-point/module access."""
    _fetch_pr_conflict_recovery_refs: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    active_execplan: Callable[..., object]
    make_worktree: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    remote_integration_effects: Callable[..., object] | None = None


def _validate_active_pr_conflict_recovery(
    effects: ValidateActivePrConflictRecoveryEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
) -> None:
    """Revalidate races without resetting an in-progress recovery stage."""

    plan = effects.active_execplan(connection, repo)
    branch = str(project["branch"] or "")
    worktree = Path(str(project["worktree"] or ""))
    observed_head = str(project["pr_conflict_head_oid"] or "")
    if (
        plan is None
        or not int(plan["pr_conflict_recovery"])
        or int(plan["workspace_owned"]) != WORKSPACE_OWNED
        or str(plan["branch"] or "") != branch
        or Path(str(plan["worktree"] or "")) != worktree
    ):
        raise PublicationSafetyError(
            "진행 중인 PR 충돌 복구의 SQLite 소유권을 확인할 수 없습니다."
        )
    if (
        str(pull_request.get("url") or "") != str(project["pr_url"] or "")
        or str(pull_request.get("headRefName") or "") != branch
    ):
        raise PublicationSafetyError(
            "진행 중인 PR의 URL, head branch 또는 head OID가 복구 시작 시 "
            "관측값과 달라졌습니다."
        )
    if effects.remote_integration_effects is not None:
        from loop_manager_internal.git.manager_git_journal import live_remote_branch_oid
        from loop_manager_internal.recovery.work_branch_integration import synchronize
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        integration_effects = effects.remote_integration_effects()
        live = live_remote_branch_oid(integration_effects.git, repo_path, branch)
        if live != observed_head or str(pull_request.get('headRefOid') or '') != observed_head:
            synchronize(integration_effects, connection, repo_path, state_dir, repo, plan, live, pull_request)
            runtime_state.RuntimeStateRepository(connection).update_project(repo, {'pr_conflict_head_oid': live})
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '새 PR 입력의 현재 단계를 다시 선택합니다.')
    effects._fetch_pr_conflict_recovery_refs(
        repo_path, state_dir, str(project["base_branch"]), branch
    )
    if effects.remote_branch_oid(repo_path, branch) != observed_head:
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE,
            "진행 중인 PR의 원격 head가 복구 시작 시 관측값과 달라졌습니다."
        )
    effects.validate_execplan_workspace_identity(
        state_dir, repo, manual_workspace_owner(connection, repo, str(plan["plan_id"])), branch, worktree
    )
    effects.make_worktree(
        repo_path,
        worktree,
        branch,
        str(project["base_branch"]),
        journal_state_dir=state_dir,
        journal_owner=f"plan:{plan['plan_id']}",
        allow_create_branch=False,
        allow_recovery=True,
        allow_existing=True,
        allow_rebase=effects._rebase_in_progress_for_branch(worktree, branch),

    )
