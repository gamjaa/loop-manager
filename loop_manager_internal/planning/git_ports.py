"""planning git ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.catalog import archive_git
from loop_manager_internal.planning.planning_runtime import PlanningWorkspaceOwnershipError
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from pathlib import Path
import sqlite3
import subprocess


@dataclass(frozen=True)
class PlanningGitPortsEffects:
    """Named effects for _PlanningGitPorts; no entry-point/module access."""
    _capture_local_worktree_state: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    _ignored_worktree_paths: Callable[..., object]
    _is_requirement_archive_only_change: Callable[..., object]
    _planning_input_fingerprint: Callable[..., object]
    _specification_input_fingerprint: Callable[..., object]
    changed_files: Callable[..., object]
    git: Callable[..., object]
    make_planning_worktree: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_planning_workspace_identity: Callable[..., object]
    validate_planning_worktree_ownership: Callable[..., object]


class _PlanningGitPorts:
    """Bind coordinator Git operations to the managed-worktree helpers."""

    def __init__(
        self,
        effects: PlanningGitPortsEffects, connection: sqlite3.Connection,
        repo_path: Path,
        state_dir: Path,
    ):
        self._effects = effects
        self._connection = connection
        self._repo_path = repo_path
        self._state_dir = state_dir

    def run(
        self, repo: Path, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if len(arguments) == 3 and arguments[:2] == ("worktree", "remove"):
            workspace = Path(arguments[2])
            self._effects.validate_managed_worktree(repo, workspace, None)
            _head, _branch, _index, dirty = self._effects._capture_local_worktree_state(workspace)
            if dirty or self._effects._git_operation_entries(workspace):
                raise WorkspaceOwnershipError(
                    f"planning worktree에 권위 있는 변경이 남았습니다: {workspace}"
                )
            if self._effects._ignored_worktree_paths(workspace):
                arguments = ("worktree", "remove", "--force", str(workspace))
        return self._effects.git(repo, *arguments, check=check)

    def input_fingerprint(self, repo_path: Path, sha: str) -> str:
        return self._effects._planning_input_fingerprint(repo_path, sha)

    def specification_fingerprint(self, repo_path: Path, sha: str) -> str:
        return self._effects._specification_input_fingerprint(repo_path, sha)

    def requirement_archive_only(
        self, repo_path: Path, old_sha: str, new_sha: str
    ) -> bool:
        return self._effects._is_requirement_archive_only_change(repo_path, old_sha, new_sha)

    def validate_workspace_identity(
        self, repo: str, fingerprint: str, worktree: Path
    ) -> None:
        try:
            self._effects.validate_planning_workspace_identity(
                self._state_dir, repo, fingerprint, worktree
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

    def validate_worktree_ownership(
        self, repo: str, fingerprint: str, worktree: Path
    ) -> None:
        try:
            self._effects.validate_planning_worktree_ownership(
                self._connection,
                self._repo_path,
                self._state_dir,
                repo,
                fingerprint,
                worktree,
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

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
    ) -> None:
        try:
            self._effects.make_planning_worktree(
                repo_path,
                worktree,
                base,
                allow_create=allow_create,
                allow_existing=allow_existing,
                ownership_reserved=ownership_reserved,
                expected_head=expected_head,
                before_create=before_create,
            )
        except WorkspaceOwnershipError as error:
            raise PlanningWorkspaceOwnershipError(str(error)) from error

    def changed_files(
        self, worktree: Path, since: str | None = None
    ) -> list[str]:
        return self._effects.changed_files(worktree, since)

    def validate_managed_worktree(self, worktree: Path) -> None:
        self._effects.validate_managed_worktree(self._repo_path, worktree, None)


@dataclass(frozen=True)
class ExecPlanArchiveGitPortEffects:
    """Named effects for _ExecPlanArchiveGitPort; no entry-point/module access."""
    _archive_git_effects: Callable[..., object]


def _ExecPlanArchiveGitPort(
    effects: ExecPlanArchiveGitPortEffects, repo_path: Path, state_dir: Path, repo: str,
) -> archive_git.ExecPlanArchiveGitPort:
    return archive_git.ExecPlanArchiveGitPort(
        repo_path, state_dir, repo, effects=effects._archive_git_effects,
    )
