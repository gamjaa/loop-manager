"""planning planning workspace boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from pathlib import Path
import hashlib
import sqlite3


@dataclass(frozen=True)
class ValidatePlanningWorkspaceIdentityEffects:
    """Named effects for validate_planning_workspace_identity; no entry-point/module access."""
    _validate_managed_path_boundary: Callable[..., object]


def validate_planning_workspace_identity(
    effects: ValidatePlanningWorkspaceIdentityEffects, state_dir: Path,
    repo: str,
    fingerprint: str,
    worktree: Path,
) -> None:
    expected = planning_workspace_path(state_dir, repo, fingerprint)
    effects._validate_managed_path_boundary(
        state_dir,
        worktree,
        expected,
        "planning worktree",
    )


def planning_workspace_path(state_dir: Path, repo: str, fingerprint: str) -> Path:
    project_id = hashlib.sha256(repo.encode()).hexdigest()[:16]
    return state_dir / "worktrees" / project_id / "planning" / fingerprint


@dataclass(frozen=True)
class ValidatePlanningWorktreeOwnershipEffects:
    """Named effects for validate_planning_worktree_ownership; no entry-point/module access."""
    validate_managed_worktree: Callable[..., object]
    validate_planning_workspace_identity: Callable[..., object]


def validate_planning_worktree_ownership(
    effects: ValidatePlanningWorktreeOwnershipEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    fingerprint: str,
    worktree: Path,
) -> None:
    """Require confirmed Manager ownership before removing a planning worktree."""

    project = connection.execute(
        "SELECT worktree, planning_workspace_owned FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if worktree.exists() and (
        project is None
        or int(project["planning_workspace_owned"]) != WORKSPACE_OWNED
        or Path(str(project["worktree"] or "")) != worktree
    ):
        raise WorkspaceOwnershipError(
            "planning worktree의 Loop Manager 소유권이 확인되지 않았습니다: "
            f"{worktree}"
        )

    if not fingerprint:
        raise WorkspaceOwnershipError(
            "planning worktree의 입력 fingerprint가 저장되지 않았습니다."
        )
    effects.validate_planning_workspace_identity(
        state_dir,
        repo,
        fingerprint,
        worktree,
    )
    if worktree.exists():
        effects.validate_managed_worktree(repo_path, worktree, None)
