"""Workspace ownership boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.git import workspace
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from pathlib import Path
import os


@dataclass(frozen=True)
class ValidateManagedPathBoundaryEffects:
    """Named effects for _validate_managed_path_boundary; no entry-point/module access."""
    _canonical_path: Callable[..., object]


def _validate_managed_path_boundary(
    effects: ValidateManagedPathBoundaryEffects, state_dir: Path,
    worktree: Path,
    expected: Path,
    label: str,
) -> None:
    lexical_worktree = os.path.normcase(
        os.path.abspath(worktree.expanduser())
    )
    lexical_expected = os.path.normcase(
        os.path.abspath(expected.expanduser())
    )
    if lexical_worktree != lexical_expected:
        raise WorkspaceOwnershipError(
            f"{label} 경로가 결정적 소유 경계와 다릅니다: "
            f"{worktree} (expected {expected})"
        )
    state_root = effects._canonical_path(state_dir)
    resolved = effects._canonical_path(worktree)
    try:
        resolved.relative_to(state_root)
    except ValueError as error:
        raise WorkspaceOwnershipError(
            f"{label} 경로가 Loop Manager 상태 디렉터리 밖을 가리킵니다: "
            f"{worktree}"
        ) from error


@dataclass(frozen=True)
class WorktreeCatalogEffects:
    """Named effects for _worktree_catalog; no entry-point/module access."""
    git: Callable[..., object]


def _worktree_catalog(effects: WorktreeCatalogEffects, repo: Path) -> list[dict[str, str | bool]]:
    records: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in effects.git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value if value else True
    if current:
        records.append(current)
    return records


@dataclass(frozen=True)
class RebaseHeadNameEffects:
    """Named effects for _rebase_head_name; no entry-point/module access."""
    git: Callable[..., object]


def _rebase_head_name(effects: RebaseHeadNameEffects, worktree: Path) -> str | None:
    for directory in ("rebase-merge", "rebase-apply"):
        result = effects.git(worktree, "rev-parse", "--git-path", f"{directory}/head-name", check=False)
        if result.returncode:
            continue
        git_path = Path(result.stdout.strip())
        if not git_path.is_absolute():
            git_path = worktree / git_path
        if git_path.is_file():
            return git_path.read_text(encoding="utf-8").strip()
    return None


@dataclass(frozen=True)
class RebaseInProgressForBranchEffects:
    """Named effects for _rebase_in_progress_for_branch; no entry-point/module access."""
    _rebase_head_name: Callable[..., object]


def _rebase_in_progress_for_branch(effects: RebaseInProgressForBranchEffects, worktree: Path, branch: str) -> bool:
    return effects._rebase_head_name(worktree) == f"refs/heads/{branch}"


@dataclass(frozen=True)
class ChangedFilesEffects:
    """Named effects for changed_files; no entry-point/module access."""
    _nul_git_paths: Callable[..., object]


def changed_files(effects: ChangedFilesEffects, worktree: Path, since: str | None = None) -> list[str]:
    def paths(*args: str) -> set[str]:
        return set(effects._nul_git_paths(worktree, *args, "-z"))

    if since:
        return sorted(paths("diff", "--no-renames", "--name-only", f"{since}..HEAD"))
    return sorted(paths("diff", "--no-renames", "--name-only")
                  | paths("diff", "--cached", "--no-renames", "--name-only")
                  | paths("ls-files", "--others", "--exclude-standard"))


@dataclass(frozen=True)
class BranchHasChangesEffects:
    """Named effects for branch_has_changes; no entry-point/module access."""
    git: Callable[..., object]


def branch_has_changes(effects: BranchHasChangesEffects, worktree: Path, base: str) -> bool:
    result = effects.git(
        worktree,
        "diff",
        "--quiet",
        f"origin/{base}...HEAD",
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            f"관리 branch 변경 확인에 실패했습니다 (git 종료 코드 {result.returncode})."
        )
    return result.returncode == 1


@dataclass(frozen=True)
class ValidateManagedWorktreeEffects:
    """Named effects for validate_managed_worktree; no entry-point/module access."""
    _workspace_effects_validate_managed_worktree: Callable[..., object]


def validate_managed_worktree(
    effects: ValidateManagedWorktreeEffects, repo: Path,
    worktree: Path,
    branch: str | None,
    *,
    allow_rebase: bool = False,
) -> None:
    return workspace.validate_managed_worktree(effects._workspace_effects_validate_managed_worktree(), repo, worktree, branch, allow_rebase=allow_rebase)


@dataclass(frozen=True)
class ValidateReservedWorktreeEffects:
    """Named effects for validate_reserved_worktree; no entry-point/module access."""
    _workspace_effects_validate_reserved_worktree: Callable[..., object]


def validate_reserved_worktree(
    effects: ValidateReservedWorktreeEffects, repo: Path,
    worktree: Path,
    branch: str | None,
    expected_head: str,
) -> None:
    return workspace.validate_reserved_worktree(effects._workspace_effects_validate_reserved_worktree(), repo, worktree, branch, expected_head)


@dataclass(frozen=True)
class MakeWorktreeEffects:
    """Named effects for make_worktree; no entry-point/module access."""
    _workspace_effects_make_worktree: Callable[..., object]


def make_worktree(
    effects: MakeWorktreeEffects, repo: Path,
    worktree: Path,
    branch: str,
    base: str,
    *,
    allow_recovery: bool = True,
    allow_existing: bool = True,
    allow_rebase: bool = False,
    allow_create_branch: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
    before_create: Callable[[], None] | None = None,
    journal_state_dir: Path | None = None,
    journal_owner: str | None = None,
) -> str:
    return workspace.make_worktree(effects._workspace_effects_make_worktree(), repo, worktree, branch, base, allow_recovery=allow_recovery, allow_existing=allow_existing, allow_rebase=allow_rebase, allow_create_branch=allow_create_branch, ownership_reserved=ownership_reserved, expected_head=expected_head, before_create=before_create, journal_state_dir=journal_state_dir, journal_owner=journal_owner)


@dataclass(frozen=True)
class MakePlanningWorktreeEffects:
    """Named effects for make_planning_worktree; no entry-point/module access."""
    _workspace_effects_make_planning_worktree: Callable[..., object]


def make_planning_worktree(
    effects: MakePlanningWorktreeEffects, repo: Path,
    worktree: Path,
    base: str,
    *,
    allow_create: bool = True,
    before_create: Callable[[], None] | None = None,
    allow_existing: bool = True,
    ownership_reserved: bool = False,
    expected_head: str | None = None,
) -> str:
    return workspace.make_planning_worktree(effects._workspace_effects_make_planning_worktree(), repo, worktree, base, allow_create=allow_create, before_create=before_create, allow_existing=allow_existing, ownership_reserved=ownership_reserved, expected_head=expected_head)


@dataclass(frozen=True)
class ValidateExecplanWorkspaceIdentityEffects:
    """Named effects for validate_execplan_workspace_identity; no entry-point/module access."""
    _workspace_effects_validate_execplan_workspace_identity: Callable[..., object]


def validate_execplan_workspace_identity(
    effects: ValidateExecplanWorkspaceIdentityEffects, state_dir: Path,
    repo: str,
    plan_id: str,
    branch: str,
    worktree: Path,
) -> None:
    return workspace.validate_execplan_workspace_identity(effects._workspace_effects_validate_execplan_workspace_identity(), state_dir, repo, plan_id, branch, worktree)
