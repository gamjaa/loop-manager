"""planning git ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.catalog import archive_git
from loop_manager_internal.planning import planning_rebase
from loop_manager_internal.planning import planning_document_relevance
from loop_manager_internal.planning.planning_runtime import PlanningWorkspaceOwnershipError
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from pathlib import Path
import sqlite3
import subprocess
from loop_manager_internal.agents import common_guidance
from loop_manager_internal.storage import planning_input_state


@dataclass(frozen=True)
class PlanningGitPortsEffects:
    """Named effects for _PlanningGitPorts; no entry-point/module access."""
    _capture_local_worktree_state: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    _ignored_worktree_paths: Callable[..., object]
    _is_requirement_archive_only_change: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    _planning_input_fingerprint: Callable[..., object]
    _nonplan_input_fingerprint: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    _assert_remote_preserves_maintenance_batch: Callable[..., object]
    plan_declarations: Callable[..., object]
    _specification_input_fingerprint: Callable[..., object]
    changed_files: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., bytes]
    make_planning_worktree: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_planning_workspace_identity: Callable[..., object]
    validate_planning_worktree_ownership: Callable[..., object]


@dataclass(frozen=True)
class _RebaseGit:
    """Keep planning rebase's ordinary Git calls and NUL paths on their own ports."""
    git: Callable[..., object]
    nul_paths: Callable[..., object]

    def __call__(self, *args, **kwargs):
        return self.git(*args, **kwargs)


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
        self._rebase_git = _RebaseGit(effects.git, effects._nul_git_paths)

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

    def run_bytes(self, repo: Path, *arguments: str) -> bytes:
        return self._effects.git_bytes(repo, *arguments)

    def fetch_base(self, repo_path: Path, base: str) -> str:
        return self._effects._recorded_base_fetch(
            repo_path, self._state_dir, base, owner="project",
        )

    def confirm_publication_release(
        self, repository: Path, base: str, base_revision: str, revision: str,
    ) -> str:
        self.fetch_base(repository, base)
        remote = self._effects.git(repository, 'rev-parse', '--verify',
                                   f'refs/remotes/origin/{base}^{{commit}}').stdout.strip()
        if remote != revision:
            self._effects._assert_remote_preserves_maintenance_batch(repository, base_revision, revision, remote)
        return remote

    def validate_publication_release(self, repo: str, worktree: Path, revision: str) -> None:
        self.validate_planning_release(repo, worktree, allow_missing=True)
        if worktree.exists() and self._effects.git(worktree, 'rev-parse', '--verify', 'HEAD^{commit}').stdout.strip() != revision:
            raise PlanningWorkspaceOwnershipError('게시 후 planning worktree HEAD가 변경됐습니다.')

    def save_planning_correction(
        self, repo: str, worktree: Path, target: str,
        paths: tuple[str, ...], was_rebase: bool,
    ) -> None:
        planning_rebase.save_planning_correction(
            self._rebase_git, self._state_dir, repo, worktree, target, paths, was_rebase,
            manager_path_validator=lambda names: self._validate_manager_correction_paths(
                repo, worktree, names,
            ),
        )

    def planning_correction(
        self, repo: str, worktree: Path, target: str,
    ) -> tuple[tuple[str, ...], bool] | None:
        return planning_rebase.planning_correction(
            self._rebase_git, self._state_dir, repo, worktree, target,
            manager_path_validator=lambda names: self._validate_manager_correction_paths(
                repo, worktree, names,
            ),
        )

    def _validate_manager_correction_paths(
        self, repo: str, worktree: Path, names: tuple[str, ...],
    ) -> tuple[int, str, str] | None:
        from loop_manager_internal.maintenance.code_map_artifact import (
            ARTIFACT_PATH, validate_restored,
        )
        if names != (ARTIFACT_PATH,):
            return None
        return validate_restored(self._connection, repo, worktree)

    def restore_code_map_conflict(
        self, repo: str, worktree: Path, stash_sha: str = "",
    ) -> None:
        from loop_manager_internal.maintenance.code_map_artifact import restore_conflict
        restore_conflict(self._connection, repo, worktree, stash_sha)

    def input_fingerprint(self, repo_path: Path, sha: str) -> str:
        return self._effects._planning_input_fingerprint(repo_path, sha, **self._guidance_argument(repo_path))

    def nonplan_fingerprint(self, repo_path: Path, sha: str) -> str:
        return self._effects._nonplan_input_fingerprint(repo_path, sha, **self._guidance_argument(repo_path))

    def plan_declarations(self, repo_path: Path, sha: str) -> dict[str, tuple[str, str]]:
        return self._effects.plan_declarations(repo_path, sha)

    def execution_blocks_planning(self, worktree: Path) -> bool:
        from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation
        if not worktree.is_dir():
            return True
        # 별도 planning worktree의 게시를 먼저 끝내고 보존한 rebase를 재개한다.
        operations = self._effects._git_operation_entries(worktree)
        return controlling_git_operation({name: value for name, value in operations.items()
            if name not in {'rebase-merge', 'rebase-apply'}}) is not None

    def specification_fingerprint(self, repo_path: Path, sha: str) -> str:
        return self._effects._specification_input_fingerprint(repo_path, sha, **self._guidance_argument(repo_path))

    def _guidance_argument(self, repo_path: Path) -> dict:
        saved = common_guidance.planning_content_hash(self._connection, str(repo_path))
        return {'guidance_hash': saved} if saved else {}

    def runtime_guidance_changed(self, repo_path: Path) -> bool:
        return planning_input_state.guidance_changed(self._connection, str(repo_path))

    def requirement_archive_only(
        self, repo_path: Path, old_sha: str, new_sha: str,
        *, verified_plan_declarations: bool = False,
        allow_execplan_only: bool = False,
        specification_only: bool = False,
    ) -> bool:
        return self._effects._is_requirement_archive_only_change(
            repo_path, old_sha, new_sha,
            verified_plan_declarations=verified_plan_declarations,
            allow_execplan_only=allow_execplan_only,
            **({'specification_only': True} if specification_only else {}),
        )

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

    def validate_planning_release(self, repo: str, worktree: Path | None, *, allow_missing: bool = False) -> None:
        from loop_manager_internal.git import manager_git_journal
        if manager_git_journal.has_conflicting_project_mutation(
                self._state_dir, repo, stage='planning_release', connection=self._connection,
                git=self._effects.git, worktrees=(worktree,) if worktree is not None else ()):
            raise PlanningWorkspaceOwnershipError("미확정 Manager Git 쓰기가 남아 있습니다.")
        if worktree is not None:
            if not worktree.exists() and allow_missing:
                registrations = self._effects.git(self._repo_path, "worktree", "list", "--porcelain", "-z").stdout
                if any(Path(item.removeprefix("worktree ")).resolve() == worktree.resolve()
                       for item in registrations.split("\0") if item.startswith("worktree ")):
                    raise PlanningWorkspaceOwnershipError("삭제된 planning 경로의 Git 등록이 남아 있습니다.")
                return
            if not worktree.exists() or self._effects._git_operation_entries(worktree):
                raise PlanningWorkspaceOwnershipError("planning worktree가 없거나 Git operation이 진행 중입니다.")
            self._effects.git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
            if self._effects.git(worktree, "ls-files", "--unmerged").stdout.strip():
                raise PlanningWorkspaceOwnershipError("planning index에 미해결 충돌이 남아 있습니다.")
        # Rebase/bisect metadata belongs to its checkout, including the primary
        # checkout. Shared mutation evidence is checked through the Manager
        # journal here and the saved Agent scope at the release boundary.

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

    def rebase_planning_workspace(
        self, repo: str, fingerprint: str, worktree: Path,
        old_base: str, target: str,
    ) -> planning_rebase.PlanningRebaseResult:
        self.validate_worktree_ownership(repo, fingerprint, worktree)
        return planning_rebase.rebase_planning_worktree(
            self._rebase_git, self._repo_path, self._state_dir, repo,
            worktree, old_base, target,
            manager_path_validator=lambda names: self._validate_manager_correction_paths(
                repo, worktree, names,
            ),
        )

    def complete_planning_rebase(self, repo: str, worktree: Path, target: str) -> None:
        planning_rebase.complete_planning_rebase(
            self._rebase_git, self._state_dir, repo, worktree, target,
        )

    def accept_resolved_stash_conflict(
        self, repo: str, worktree: Path, target: str,
    ) -> None:
        planning_rebase.accept_resolved_stash_conflict(
            self._rebase_git, self._state_dir, repo, worktree, target,
            manager_path_validator=lambda names: self._validate_manager_correction_paths(
                repo, worktree, names,
            ),
        )

    def relevant_document_changes(
        self, worktree: Path, old: str, target: str,
        edited_paths: list[str], catalog_rows, target_plan_ids=(),
        *, include_active_requirements: bool = False,
    ) -> tuple[str, ...]:
        return planning_document_relevance.relevant_document_changes(
            self._effects.git, self._repo_path, worktree, old, target,
            edited_paths, catalog_rows, target_plan_ids,
            include_active_requirements=include_active_requirements,
            git_bytes=self._effects.git_bytes,
        )

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
