"""git git adapter boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.catalog import archive_git
from loop_manager_internal.git import command_adapter
from loop_manager_internal.git import github_observation
from loop_manager_internal.git import source_sync
from loop_manager_internal.git import workspace
from loop_manager_internal.recovery import checkpoint_adapter
from loop_manager_internal.recovery import project_blocked_recovery
from loop_manager_internal.runtime.contracts import BaseWorktreeSnapshot
from loop_manager_internal.runtime.contracts import BaseWorktreeSyncResult
from pathlib import Path
import os
import re
import subprocess


@dataclass(frozen=True)
class CommandAdapterEffectsGitEffects:
    """Named effects for _command_adapter_effects_git; no entry-point/module access."""
    _ignored_paths_overlapping_git_update: Callable[..., object]
    _rebase_resume_ignored_overlap: Callable[..., object]
    command: Callable[..., object]
    is_git_lock_failure: Callable[..., object]


def _command_adapter_effects_git(effects: CommandAdapterEffectsGitEffects, ) -> command_adapter.GitEffects:
    return command_adapter.GitEffects(
        _ignored_paths_overlapping_git_update=effects._ignored_paths_overlapping_git_update,
        _rebase_resume_ignored_overlap=effects._rebase_resume_ignored_overlap,
        command=effects.command,
        is_git_lock_failure=effects.is_git_lock_failure,
    )


@dataclass(frozen=True)
class CommandAdapterEffectsGitBytesEffects:
    """Named effects for _command_adapter_effects_git_bytes; no entry-point/module access."""
    _handoff_run: Callable[..., object]
    is_git_lock_failure: Callable[..., object]


def _command_adapter_effects_git_bytes(effects: CommandAdapterEffectsGitBytesEffects, ) -> command_adapter.GitBytesEffects:
    return command_adapter.GitBytesEffects(
        _handoff_run=effects._handoff_run,
        is_git_lock_failure=effects.is_git_lock_failure,
    )


@dataclass(frozen=True)
class GithubObservationEffectsPrForBranchEffects:
    """Named effects for _github_observation_effects_pr_for_branch; no entry-point/module access."""
    command: Callable[..., object]


def _github_observation_effects_pr_for_branch(effects: GithubObservationEffectsPrForBranchEffects, ) -> github_observation.PrForBranchEffects:
    return github_observation.PrForBranchEffects(
        command=effects.command,
    )


@dataclass(frozen=True)
class WorkspaceEffectsValidateManagedWorktreeEffects:
    """Named effects for _workspace_effects_validate_managed_worktree; no entry-point/module access."""
    _rebase_in_progress_for_branch: Callable[..., object]
    _worktree_catalog: Callable[..., object]


def _workspace_effects_validate_managed_worktree(effects: WorkspaceEffectsValidateManagedWorktreeEffects, ) -> workspace.ValidateManagedWorktreeEffects:
    return workspace.ValidateManagedWorktreeEffects(
        _rebase_in_progress_for_branch=effects._rebase_in_progress_for_branch,
        _worktree_catalog=effects._worktree_catalog,
    )


@dataclass(frozen=True)
class WorkspaceEffectsValidateReservedWorktreeEffects:
    """Named effects for _workspace_effects_validate_reserved_worktree; no entry-point/module access."""
    capture_git_snapshot: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _workspace_effects_validate_reserved_worktree(effects: WorkspaceEffectsValidateReservedWorktreeEffects, ) -> workspace.ValidateReservedWorktreeEffects:
    return workspace.ValidateReservedWorktreeEffects(
        capture_git_snapshot=effects.capture_git_snapshot,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class WorkspaceEffectsMakeWorktreeEffects:
    """Named effects for _workspace_effects_make_worktree; no entry-point/module access."""
    _managed_worktree_marker: Callable[..., object]
    _managed_worktree_transition_state: Callable[..., object]
    _worktree_catalog: Callable[..., object]
    _write_managed_worktree_receipt: Callable[..., object]
    git: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_reserved_worktree: Callable[..., object]


def _workspace_effects_make_worktree(effects: WorkspaceEffectsMakeWorktreeEffects, ) -> workspace.MakeWorktreeEffects:
    return workspace.MakeWorktreeEffects(
        _managed_worktree_marker=effects._managed_worktree_marker,
        _managed_worktree_transition_state=effects._managed_worktree_transition_state,
        _worktree_catalog=effects._worktree_catalog,
        _write_managed_worktree_receipt=effects._write_managed_worktree_receipt,
        git=effects.git,
        validate_managed_worktree=effects.validate_managed_worktree,
        validate_reserved_worktree=effects.validate_reserved_worktree,
    )


@dataclass(frozen=True)
class WorkspaceEffectsMakePlanningWorktreeEffects:
    """Named effects for _workspace_effects_make_planning_worktree; no entry-point/module access."""
    git: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_reserved_worktree: Callable[..., object]


def _workspace_effects_make_planning_worktree(effects: WorkspaceEffectsMakePlanningWorktreeEffects, ) -> workspace.MakePlanningWorktreeEffects:
    return workspace.MakePlanningWorktreeEffects(
        git=effects.git,
        validate_managed_worktree=effects.validate_managed_worktree,
        validate_reserved_worktree=effects.validate_reserved_worktree,
    )


@dataclass(frozen=True)
class WorkspaceEffectsValidateExecplanWorkspaceIdentityEffects:
    """Named effects for _workspace_effects_validate_execplan_workspace_identity; no entry-point/module access."""
    _validate_managed_path_boundary: Callable[..., object]
    expected_execplan_workspace: Callable[..., object]


def _workspace_effects_validate_execplan_workspace_identity(effects: WorkspaceEffectsValidateExecplanWorkspaceIdentityEffects, ) -> workspace.ValidateExecplanWorkspaceIdentityEffects:
    return workspace.ValidateExecplanWorkspaceIdentityEffects(
        _validate_managed_path_boundary=effects._validate_managed_path_boundary,
        expected_execplan_workspace=effects.expected_execplan_workspace,
    )


@dataclass(frozen=True)
class SourceSyncEffectsBaseWorktreeSnapshotEffects:
    """Named effects for _source_sync_effects_base_worktree_snapshot; no entry-point/module access."""
    _base_local_path_signature: Callable[..., object]
    _base_worktree_index_error: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    _ignored_worktree_paths: Callable[..., object]
    _index_entries: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    git: Callable[..., object]


def _source_sync_effects_base_worktree_snapshot(effects: SourceSyncEffectsBaseWorktreeSnapshotEffects, ) -> source_sync.BaseWorktreeSnapshotEffects:
    return source_sync.BaseWorktreeSnapshotEffects(
        _base_local_path_signature=effects._base_local_path_signature,
        _base_worktree_index_error=effects._base_worktree_index_error,
        _git_operation_entries=effects._git_operation_entries,
        _ignored_worktree_paths=effects._ignored_worktree_paths,
        _index_entries=effects._index_entries,
        _nul_git_paths=effects._nul_git_paths,
        git=effects.git,
    )


@dataclass(frozen=True)
class SourceSyncEffectsBasePathsOverlapEffects:
    """Named effects for _source_sync_effects_base_paths_overlap; no entry-point/module access."""
    _paths_overlap: Callable[..., object]


def _source_sync_effects_base_paths_overlap(effects: SourceSyncEffectsBasePathsOverlapEffects, ) -> source_sync.BasePathsOverlapEffects:
    return source_sync.BasePathsOverlapEffects(
        _paths_overlap=effects._paths_overlap,
    )


@dataclass(frozen=True)
class SourceSyncEffectsSourceTransitionStateEffects:
    """Named effects for _source_sync_effects_source_transition_state; no entry-point/module access."""
    _base_worktree_snapshot: Callable[..., object]


def _source_sync_effects_source_transition_state(effects: SourceSyncEffectsSourceTransitionStateEffects, ) -> source_sync.SourceTransitionStateEffects:
    return source_sync.SourceTransitionStateEffects(
        _base_worktree_snapshot=effects._base_worktree_snapshot,
    )


@dataclass(frozen=True)
class SourceSyncEffectsSourceTransitionMarkerEffects:
    """Named effects for _source_sync_effects_source_transition_marker; no entry-point/module access."""
    git: Callable[..., object]


def _source_sync_effects_source_transition_marker(effects: SourceSyncEffectsSourceTransitionMarkerEffects, ) -> source_sync.SourceTransitionMarkerEffects:
    return source_sync.SourceTransitionMarkerEffects(
        git=effects.git,
    )


@dataclass(frozen=True)
class SourceSyncEffectsSourceExpectedIndexEffects:
    """Named effects for _source_sync_effects_source_expected_index; no entry-point/module access."""
    git_bytes: Callable[..., object]


def _source_sync_effects_source_expected_index(effects: SourceSyncEffectsSourceExpectedIndexEffects, ) -> source_sync.SourceExpectedIndexEffects:
    return source_sync.SourceExpectedIndexEffects(
        git_bytes=effects.git_bytes,
    )


@dataclass(frozen=True)
class SourceSyncEffectsSynchronizeBaseWorktreeEffects:
    """Named effects for _source_sync_effects_synchronize_base_worktree; no entry-point/module access."""
    _base_paths_overlap: Callable[..., object]
    _base_worktree_index_error: Callable[..., object]
    _base_worktree_snapshot: Callable[..., object]
    _git_operation_entries: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    _source_expected_index: Callable[..., object]
    _source_transition_marker: Callable[..., object]
    _source_transition_state: Callable[..., object]
    git: Callable[..., object]
    is_git_lock_failure: Callable[..., object]


def _source_sync_effects_synchronize_base_worktree(effects: SourceSyncEffectsSynchronizeBaseWorktreeEffects, ) -> source_sync.SynchronizeBaseWorktreeEffects:
    return source_sync.SynchronizeBaseWorktreeEffects(
        _base_paths_overlap=effects._base_paths_overlap,
        _base_worktree_index_error=effects._base_worktree_index_error,
        _base_worktree_snapshot=effects._base_worktree_snapshot,
        _git_operation_entries=effects._git_operation_entries,
        _nul_git_paths=effects._nul_git_paths,
        _source_expected_index=effects._source_expected_index,
        _source_transition_marker=effects._source_transition_marker,
        _source_transition_state=effects._source_transition_state,
        git=effects.git,
        is_git_lock_failure=effects.is_git_lock_failure,
    )


@dataclass(frozen=True)
class SourceSyncEffectsEnsureSafeBaseWorktreeEffects:
    """Named effects for _source_sync_effects_ensure_safe_base_worktree; no entry-point/module access."""
    synchronize_base_worktree: Callable[..., object]


def _source_sync_effects_ensure_safe_base_worktree(effects: SourceSyncEffectsEnsureSafeBaseWorktreeEffects, ) -> source_sync.EnsureSafeBaseWorktreeEffects:
    return source_sync.EnsureSafeBaseWorktreeEffects(
        synchronize_base_worktree=effects.synchronize_base_worktree,
    )


@dataclass(frozen=True)
class SourceSyncEffectsLogBaseWorktreeSyncEffects:
    """Named effects for _source_sync_effects_log_base_worktree_sync; no entry-point/module access."""
    operation_log: Callable[..., object]


def _source_sync_effects_log_base_worktree_sync(effects: SourceSyncEffectsLogBaseWorktreeSyncEffects, ) -> source_sync.LogBaseWorktreeSyncEffects:
    return source_sync.LogBaseWorktreeSyncEffects(
        operation_log=effects.operation_log,
    )


@dataclass(frozen=True)
class ArchiveGitEffectsEffects:
    """Named effects for _archive_git_effects; no entry-point/module access."""
    _recorded_base_fetch: Callable[..., object]
    _validate_managed_path_boundary: Callable[..., object]
    command: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., object]
    is_git_lock_failure: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _archive_git_effects(effects: ArchiveGitEffectsEffects, ) -> archive_git.ArchiveGitEffects:
    return archive_git.ArchiveGitEffects(
        _recorded_base_fetch=effects._recorded_base_fetch,
        _validate_managed_path_boundary=effects._validate_managed_path_boundary,
        command=effects.command,
        git=effects.git,
        git_bytes=effects.git_bytes,
        is_git_lock_failure=effects.is_git_lock_failure,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class CheckpointSafetyEffectsEffects:
    """Named effects for _checkpoint_safety_effects; no entry-point/module access."""
    _agent_git_resources: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _guidance_document: Callable[..., object]
    _guidance_document_matches_rows: Callable[..., object]
    _guidance_rows: Callable[..., object]
    _owned_checkpoint_remote_oid: Callable[..., object]
    _proven_manager_base_refs: Callable[..., object]
    _proven_merged_cleanup_paths: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    github_repository: Callable[..., object]
    pr_for_branch: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _checkpoint_safety_effects(effects: CheckpointSafetyEffectsEffects, ) -> checkpoint_adapter.CheckpointSafetyEffects:
    return checkpoint_adapter.CheckpointSafetyEffects(
        _agent_git_resources=effects._agent_git_resources,
        _guidance_document=effects._guidance_document,
        _guidance_document_matches_rows=effects._guidance_document_matches_rows,
        _guidance_rows=effects._guidance_rows,
        _owned_checkpoint_remote_oid=effects._owned_checkpoint_remote_oid,
        _proven_manager_base_refs=effects._proven_manager_base_refs,
        _proven_merged_cleanup_paths=effects._proven_merged_cleanup_paths,
        _published_plan_allows_common_plans=effects._published_plan_allows_common_plans,
        _rebase_in_progress_for_branch=effects._rebase_in_progress_for_branch,
        capture_git_snapshot=effects.capture_git_snapshot,
        current_scope_effects=lambda: project_blocked_recovery.CurrentScopeEffects(effects._current_rebase_service),
        git=effects.git,
        github_repository=effects.github_repository,
        pr_for_branch=effects.pr_for_branch,
        validate_agent_scope=effects.validate_agent_scope,
        validate_blocked_agent_scope=effects.validate_blocked_agent_scope,
        validate_execplan_workspace_identity=effects.validate_execplan_workspace_identity,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class CheckpointPublicationEffectsEffects:
    """Named effects for _checkpoint_publication_effects; no entry-point/module access."""
    _git_with_index: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    command: Callable[..., object]
    git: Callable[..., object]


def _checkpoint_publication_effects(effects: CheckpointPublicationEffectsEffects, ) -> checkpoint_adapter.CheckpointPublicationEffects:
    return checkpoint_adapter.CheckpointPublicationEffects(
        _git_with_index=effects._git_with_index,
        capture_git_snapshot=effects.capture_git_snapshot,
        command=effects.command,
        git=effects.git,
    )


@dataclass(frozen=True)
class DiagnosticPublicationEffectsEffects:
    """Named effects for _diagnostic_publication_effects; no entry-point/module access."""
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]


def _diagnostic_publication_effects(effects: DiagnosticPublicationEffectsEffects, ) -> checkpoint_adapter.PushDiagnosticCheckpointEffects:
    return checkpoint_adapter.PushDiagnosticCheckpointEffects(
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
    )


@dataclass(frozen=True)
class CommandEffects:
    """Named effects for command; no entry-point/module access."""
    _handoff_run: Callable[..., object]


def command(
    effects: CommandEffects, arguments: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = effects._handoff_run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode:
        output = result.stdout[-6000:].strip()
        raise RuntimeError(f"명령 실패 ({' '.join(arguments[:3])}):\n{output}")
    return result


def is_git_lock_failure(output: str) -> bool:
    normalized = " ".join(output.casefold().split())
    return any(
        marker in normalized
        for marker in (
            "another git process",
            "index.lock': file exists",
            "index.lock: file exists",
            "cannot lock ref",
            "could not lock",
            "failed to lock",
            "unable to create",
        )
    ) and ("lock" in normalized or ".lock" in normalized)


@dataclass(frozen=True)
class GitEffects:
    """Named effects for git; no entry-point/module access."""
    _command_adapter_effects_git: Callable[..., object]


def git(
    effects: GitEffects, repo: Path, *arguments: str, check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return command_adapter.git(effects._command_adapter_effects_git(), repo, *arguments, check=check, env=env)


def github_repository(origin_url: str) -> str:
    match = re.fullmatch(
        r"(?:git@|ssh://git@|https://)(?:www\.)?github\.com[:/](?P<owner>[^/:\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?",
        origin_url.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("origin이 GitHub 저장소가 아닙니다.")
    return f"{match.group('owner')}/{match.group('repo')}".lower()


@dataclass(frozen=True)
class IgnoredWorktreePathsEffects:
    """Named effects for _ignored_worktree_paths; no entry-point/module access."""
    _nul_git_paths: Callable[..., object]


def _ignored_worktree_paths(effects: IgnoredWorktreePathsEffects, repo: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            effects._nul_git_paths(
                repo,
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            )
        )
    )


def _paths_overlap(left: str, right: str) -> bool:
    left_parts = tuple(part for part in left.replace("\\", "/").split("/") if part)
    right_parts = tuple(part for part in right.replace("\\", "/").split("/") if part)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


@dataclass(frozen=True)
class IgnoredPathsOverlappingGitUpdateEffects:
    """Named effects for _ignored_paths_overlapping_git_update; no entry-point/module access."""
    _ignored_worktree_paths: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    _paths_overlap: Callable[..., object]
    git: Callable[..., object]


def _ignored_paths_overlapping_git_update(effects: IgnoredPathsOverlappingGitUpdateEffects, worktree: Path, target: str) -> list[str]:
    ignored = effects._ignored_worktree_paths(worktree)
    if not ignored:
        return []
    changed = effects._nul_git_paths(
        worktree, "diff", "--name-only", "-z", "HEAD", target
    )
    fork = effects.git(worktree, "merge-base", "HEAD", target).stdout.strip()
    for commit in effects.git(worktree, "rev-list", "--reverse", f"{fork}..HEAD").stdout.splitlines():
        changed.extend(effects._nul_git_paths(
            worktree, "diff-tree", "-m", "--root", "--no-commit-id",
            "--name-only", "-r", "-z", commit,
        ))
    return sorted(
        path for path in ignored
        if any(effects._paths_overlap(path, candidate) for candidate in changed)
    )


@dataclass(frozen=True)
class RemainingRebaseCommitsEffects:
    """Named effects for _remaining_rebase_commits; no entry-point/module access."""
    git_bytes: Callable[..., object]


def _remaining_rebase_commits(
    effects: RemainingRebaseCommitsEffects, worktree: Path,
    metadata_directory: Path,
    backend: str,
    original_head: str,
    onto: str,
    action: str,
) -> list[str] | None:
    if backend == "rebase-merge":
        todo = metadata_directory / "git-rebase-todo"
        if not todo.is_file():
            return None
        commits = []
        for line in todo.read_text(encoding="utf-8").splitlines():
            fields = line.split(maxsplit=2)
            if (
                not fields
                or fields[0].startswith("#")
                or fields[0] in {"drop", "d"}
            ):
                continue
            if fields[0] not in {
                "pick", "p", "reword", "r", "edit", "e",
                "squash", "s", "fixup", "f",
            } or len(fields) < 2:
                return None
            commits.append(
                effects.git_bytes(
                    worktree, "rev-parse", "--verify", f"{fields[1]}^{{commit}}"
                ).decode("ascii").strip()
            )
        return commits

    next_path = metadata_directory / "next"
    last_path = metadata_directory / "last"
    if not next_path.is_file() or not last_path.is_file():
        return None
    commits = effects.git_bytes(
        worktree, "rev-list", "--reverse", original_head, f"^{onto}"
    ).decode("ascii").splitlines()
    try:
        next_index = int(next_path.read_text(encoding="ascii").strip())
        last_index = int(last_path.read_text(encoding="ascii").strip())
    except ValueError:
        return None
    if len(commits) != last_index or not 1 <= next_index <= last_index + 1:
        return None
    return commits[next_index - (1 if action == "--continue" else 0):]


@dataclass(frozen=True)
class RebaseResumeIgnoredOverlapEffects:
    """Named effects for _rebase_resume_ignored_overlap; no entry-point/module access."""
    _ignored_worktree_paths: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    _paths_overlap: Callable[..., object]
    _remaining_rebase_commits: Callable[..., object]
    git_bytes: Callable[..., object]


def _rebase_resume_ignored_overlap(effects: RebaseResumeIgnoredOverlapEffects, worktree: Path, action: str) -> str | None:
    ignored = effects._ignored_worktree_paths(worktree)
    if not ignored:
        return None
    metadata: tuple[str, str, Path, str] | None = None
    for directory in ("rebase-merge", "rebase-apply"):
        raw_directory = os.fsdecode(
            effects.git_bytes(worktree, "rev-parse", "--git-path", directory).strip()
        )
        metadata_directory = Path(raw_directory)
        if not metadata_directory.is_absolute():
            metadata_directory = worktree / metadata_directory
        values = []
        for name in ("orig-head", "onto"):
            path = metadata_directory / name
            if not path.is_file():
                break
            values.append(path.read_text(encoding="ascii").strip())
        if len(values) == 2:
            metadata = values[0], values[1], metadata_directory, directory
            break
    if metadata is None:
        return "rebase 재개 전 남은 tracked 경로를 확인할 수 없습니다."
    original_head, onto, metadata_directory, backend = metadata
    if action == "--abort":
        changed = effects._nul_git_paths(
            worktree, "diff", "--name-only", "-z", "HEAD", original_head
        )
    else:
        commits = effects._remaining_rebase_commits(
            worktree, metadata_directory, backend, original_head, onto, action
        )
        if commits is None:
            return "rebase 재개 전 남은 tracked 경로를 확인할 수 없습니다."
        changed = [
            path
            for commit in commits
            for path in effects._nul_git_paths(
                worktree, "diff-tree", "-m", "--root", "--no-commit-id",
                "--name-only", "-r", "-z", commit,
            )
        ]
    overlaps = sorted(
        path for path in ignored
        if any(effects._paths_overlap(path, candidate) for candidate in changed)
    )
    if overlaps:
        return (
            "rebase 재개 tracked 경로와 겹치는 ignored 파일: "
            + ", ".join(overlaps[:8])
        )
    return None


@dataclass(frozen=True)
class BaseWorktreeSnapshotEffects:
    """Named effects for _base_worktree_snapshot; no entry-point/module access."""
    _source_sync_effects_base_worktree_snapshot: Callable[..., object]


def _base_worktree_snapshot(
    effects: BaseWorktreeSnapshotEffects, repo: Path,
) -> BaseWorktreeSnapshot:
    return source_sync._base_worktree_snapshot(effects._source_sync_effects_base_worktree_snapshot(), repo)


@dataclass(frozen=True)
class BasePathsOverlapEffects:
    """Named effects for _base_paths_overlap; no entry-point/module access."""
    _source_sync_effects_base_paths_overlap: Callable[..., object]


def _base_paths_overlap(effects: BasePathsOverlapEffects, left: str, right: str) -> bool:
    # A remote checkout can be case insensitive even when the current host is not.
    return source_sync._base_paths_overlap(effects._source_sync_effects_base_paths_overlap(), left, right)


@dataclass(frozen=True)
class SourceTransitionStateEffects:
    """Named effects for _source_transition_state; no entry-point/module access."""
    _source_sync_effects_source_transition_state: Callable[..., object]


def _source_transition_state(effects: SourceTransitionStateEffects, repo: Path) -> dict[str, object]:
    return source_sync._source_transition_state(effects._source_sync_effects_source_transition_state(), repo)


@dataclass(frozen=True)
class SourceTransitionMarkerEffects:
    """Named effects for _source_transition_marker; no entry-point/module access."""
    _source_sync_effects_source_transition_marker: Callable[..., object]


def _source_transition_marker(effects: SourceTransitionMarkerEffects, repo: Path, base: str, intent: Mapping[str, object]) -> bool:
    return source_sync._source_transition_marker(effects._source_sync_effects_source_transition_marker(), repo, base, intent)


@dataclass(frozen=True)
class SourceExpectedIndexEffects:
    """Named effects for _source_expected_index; no entry-point/module access."""
    _source_sync_effects_source_expected_index: Callable[..., object]


def _source_expected_index(
    effects: SourceExpectedIndexEffects, repo: Path, selected_remote: str, before_index: Mapping[str, object],
    changed_paths: tuple[str, ...],
) -> dict[str, object]:
    return source_sync._source_expected_index(effects._source_sync_effects_source_expected_index(), repo, selected_remote, before_index, changed_paths)


@dataclass(frozen=True)
class SynchronizeBaseWorktreeEffects:
    """Named effects for synchronize_base_worktree; no entry-point/module access."""
    _source_sync_effects_synchronize_base_worktree: Callable[..., object]


def synchronize_base_worktree(
    effects: SynchronizeBaseWorktreeEffects, repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
    owner: str = "project",
) -> BaseWorktreeSyncResult:
    return source_sync.synchronize_base_worktree(effects._source_sync_effects_synchronize_base_worktree(), repo, base, remote_sha=remote_sha, state_dir=state_dir, owner=owner)


@dataclass(frozen=True)
class EnsureSafeBaseWorktreeEffects:
    """Named effects for ensure_safe_base_worktree; no entry-point/module access."""
    _source_sync_effects_ensure_safe_base_worktree: Callable[..., object]


def ensure_safe_base_worktree(
    effects: EnsureSafeBaseWorktreeEffects, repo: Path,
    base: str,
    *,
    remote_sha: str | None = None,
    state_dir: Path | None = None,
) -> BaseWorktreeSyncResult:
    return source_sync.ensure_safe_base_worktree(effects._source_sync_effects_ensure_safe_base_worktree(), repo, base, remote_sha=remote_sha, state_dir=state_dir)


@dataclass(frozen=True)
class LogBaseWorktreeSyncEffects:
    """Named effects for log_base_worktree_sync; no entry-point/module access."""
    _source_sync_effects_log_base_worktree_sync: Callable[..., object]


def log_base_worktree_sync(effects: LogBaseWorktreeSyncEffects, repo: Path, result: BaseWorktreeSyncResult) -> None:
    return source_sync.log_base_worktree_sync(effects._source_sync_effects_log_base_worktree_sync(), repo, result)
