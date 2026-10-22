"""agents scope adapter boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, replace
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.agents import scope_baseline
from loop_manager_internal.agents import scope_lifecycle
from loop_manager_internal.agents import scope_observation
from loop_manager_internal.agents import scope_validation
from loop_manager_internal.agents.scope_contracts import AgentChange
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.application.contracts import ExecPlanMetadataError
from loop_manager_internal.execution import execution_revision
from pathlib import Path
import sqlite3
import subprocess


@dataclass(frozen=True)
class NulGitPathsEffects:
    """Named effects for _nul_git_paths; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _nul_git_paths(effects: NulGitPathsEffects, repo: Path, *arguments: str) -> list[str]:
    return scope_observation._nul_git_paths(repo, *arguments, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class IndexEntriesEffects:
    """Named effects for _index_entries; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _index_entries(effects: IndexEntriesEffects, worktree: Path) -> dict[str, tuple[str, ...]]:
    return scope_observation._index_entries(worktree, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class SharedRefEntriesEffects:
    """Named effects for _shared_ref_entries; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _shared_ref_entries(effects: SharedRefEntriesEffects, worktree: Path) -> dict[str, tuple[str, str]]:
    return scope_observation._shared_ref_entries(worktree, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class RegisteredWorktreeEntriesEffects:
    """Named effects for _registered_worktree_entries; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _registered_worktree_entries(effects: RegisteredWorktreeEntriesEffects, worktree: Path) -> dict[str, tuple[str, ...]]:
    return scope_observation._registered_worktree_entries(worktree, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class ConfigEntriesEffects:
    """Named effects for _config_entries; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _config_entries(effects: ConfigEntriesEffects, worktree: Path) -> dict[str, tuple[str, ...]]:
    return scope_observation._config_entries(worktree, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class WithProvenLegacyIgnoreConfigEffects:
    """Named effects for _with_proven_legacy_ignore_config; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _with_proven_legacy_ignore_config(effects: WithProvenLegacyIgnoreConfigEffects, saved: GitSnapshot, current_config: dict[str, tuple[str, ...]]) -> GitSnapshot:
    return scope_observation._with_proven_legacy_ignore_config(
        saved,
        current_config,
        effects=effects._scope_observation_effects(),
    )


@dataclass(frozen=True)
class GitOperationEntriesEffects:
    """Named effects for _git_operation_entries; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _git_operation_entries(effects: GitOperationEntriesEffects, worktree: Path) -> dict[str, str]:
    return scope_observation._git_operation_entries(worktree, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class PathSignatureEffects:
    """Named effects for _path_signature; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _path_signature(effects: PathSignatureEffects, worktree: Path, relative_path: str) -> str:
    return scope_observation._path_signature(worktree, relative_path, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class CaptureLocalWorktreeStateEffects:
    """Named effects for _capture_local_worktree_state; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _capture_local_worktree_state(
    effects: CaptureLocalWorktreeStateEffects, worktree: Path,
) -> tuple[str, str | None, dict[str, tuple[str, ...]], dict[str, tuple[tuple[str, ...], str]]]:
    return scope_observation._capture_local_worktree_state(worktree, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class WorktreeStateSignatureEffects:
    """Named effects for _worktree_state_signature; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _worktree_state_signature(effects: WorktreeStateSignatureEffects, worktree: Path) -> str:
    return scope_observation._worktree_state_signature(worktree, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class IgnoreRuleSourceEffects:
    """Named effects for _ignore_rule_source; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _ignore_rule_source(effects: IgnoreRuleSourceEffects, worktree: Path, path: str) -> str | None:
    return scope_observation._ignore_rule_source(worktree, path, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class NormalizeLegacyIgnoredSnapshotEffects:
    """Named effects for _normalize_legacy_ignored_snapshot; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _normalize_legacy_ignored_snapshot(effects: NormalizeLegacyIgnoredSnapshotEffects, worktree: Path, saved: GitSnapshot, current: GitSnapshot, *, start: GitSnapshot | None = None) -> GitSnapshot:
    return scope_observation._normalize_legacy_ignored_snapshot(
        worktree,
        saved,
        current,
        start=start,
        effects=effects._scope_observation_effects(),
    )


@dataclass(frozen=True)
class CaptureGitSnapshotEffects:
    """Named effects for capture_git_snapshot; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def capture_git_snapshot(effects: CaptureGitSnapshotEffects, worktree: Path,
                         *, details: dict | None = None) -> GitSnapshot:
    return scope_observation.capture_git_snapshot(
        worktree, effects=effects._scope_observation_effects(), details=details)


@dataclass(frozen=True)
class AgentSnapshotChangesEffects:
    """Named effects for agent_snapshot_changes; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def agent_snapshot_changes(effects: AgentSnapshotChangesEffects, worktree: Path, before: GitSnapshot, after: GitSnapshot) -> list[AgentChange]:
    return scope_observation.agent_snapshot_changes(worktree, before, after, effects=effects._scope_observation_effects())


@dataclass(frozen=True)
class ScopeSnapshotWithStartIgnoreRulesEffects:
    """Named effects for _scope_snapshot_with_start_ignore_rules; no entry-point/module access."""
    _scope_observation_effects: Callable[..., object]


def _scope_snapshot_with_start_ignore_rules(effects: ScopeSnapshotWithStartIgnoreRulesEffects, worktree: Path, before: GitSnapshot, after: GitSnapshot) -> GitSnapshot:
    return scope_observation._scope_snapshot_with_start_ignore_rules(
        worktree,
        before,
        after,
        effects=effects._scope_observation_effects(),
    )


@dataclass(frozen=True)
class AgentGitResourcesEffects:
    """Named effects for _agent_git_resources; no entry-point/module access."""
    _scope_validation_effects: Callable[..., object]


def _agent_git_resources(
    effects: AgentGitResourcesEffects, connection: sqlite3.Connection,
    repo_path: Path,
    worktree: Path,
    before: GitSnapshot,
    role: str,
    *,
    selected_plan_path: str | None = None,
) -> agent_scope_decision.GitResources | None:
    return scope_validation._agent_git_resources(
        connection,
        repo_path,
        worktree,
        before,
        role,
        selected_plan_path=selected_plan_path,
        effects=effects._scope_validation_effects(),
    )


@dataclass(frozen=True)
class ValidateAgentScopeEffects:
    """Named effects for validate_agent_scope; no entry-point/module access."""
    _scope_validation_effects: Callable[..., object]


def validate_agent_scope(
    effects: ValidateAgentScopeEffects, role_name: str,
    worktree: Path,
    before: GitSnapshot,
    after: GitSnapshot,
    *,
    selected_plan_path: str | None = None,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
    allowed_ref_changes: frozenset[str] = frozenset(),
    allowed_worktree_changes: frozenset[str] = frozenset(),
    external_worktree: Path | None = None,
    base_branch: str | None = None,
    connection: sqlite3.Connection | None = None,
    resources: agent_scope_decision.GitResources | None = None,
    owned_rebase: bool = False,
) -> None:
    return scope_validation.validate_agent_scope(
        role_name,
        worktree,
        before,
        after,
        selected_plan_path=selected_plan_path,
        allowed_requirement_archives=allowed_requirement_archives,
        allowed_guidance_targets=allowed_guidance_targets,
        allow_common_plans=allow_common_plans,
        allowed_ref_changes=allowed_ref_changes,
        allowed_worktree_changes=allowed_worktree_changes,
        external_worktree=external_worktree,
        base_branch=base_branch,
        connection=connection,
        resources=resources,
        owned_rebase=owned_rebase,
        effects=effects._scope_validation_effects(),
    )


@dataclass(frozen=True)
class GitWithIndexEffects:
    """Named effects for _git_with_index; no entry-point/module access."""
    _scope_baseline_effects: Callable[..., object]


def _git_with_index(effects: GitWithIndexEffects, worktree: Path, index_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return scope_baseline._git_with_index(worktree, index_path, *arguments, effects=effects._scope_baseline_effects())


@dataclass(frozen=True)
class HashGitBlobEffects:
    """Named effects for _hash_git_blob; no entry-point/module access."""
    _scope_baseline_effects: Callable[..., object]


def _hash_git_blob(effects: HashGitBlobEffects, worktree: Path, contents: bytes) -> str:
    return scope_baseline._hash_git_blob(worktree, contents, effects=effects._scope_baseline_effects())


@dataclass(frozen=True)
class AgentWorktreeTreeEffects:
    """Named effects for _agent_worktree_tree; no entry-point/module access."""
    _scope_baseline_effects: Callable[..., object]


def _agent_worktree_tree(effects: AgentWorktreeTreeEffects, worktree: Path, snapshot: GitSnapshot, index_tree: str) -> str:
    return scope_baseline._agent_worktree_tree(worktree, snapshot, index_tree, effects=effects._scope_baseline_effects())


@dataclass(frozen=True)
class CommitTreeEffects:
    """Named effects for _commit_tree; no entry-point/module access."""
    _scope_baseline_effects: Callable[..., object]


def _commit_tree(effects: CommitTreeEffects, worktree: Path, tree: str, parent: str, message: str) -> str:
    return scope_baseline._commit_tree(worktree, tree, parent, message, effects=effects._scope_baseline_effects())


@dataclass(frozen=True)
class CaptureAgentBaselineEffects:
    """Named effects for _capture_agent_baseline; no entry-point/module access."""
    _scope_baseline_effects: Callable[..., object]


def _capture_agent_baseline(effects: CaptureAgentBaselineEffects, repo: str, worktree: Path, snapshot: GitSnapshot,
                            *, preserve_conflicts: bool = False) -> str | None:
    return scope_baseline._capture_agent_baseline(repo, worktree, snapshot, effects=effects._scope_baseline_effects(),
                                                preserve_conflicts=preserve_conflicts)


@dataclass(frozen=True)
class BeginAgentScopeEffects:
    """Named effects for begin_agent_scope; no entry-point/module access."""
    _scope_lifecycle_effects: Callable[..., object]


def begin_agent_scope(
    effects: BeginAgentScopeEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    role_name: str,
    *,
    input_fingerprint: str | None = None,
    state_dir: Path | None = None,
) -> GitSnapshot:
    return scope_lifecycle.begin_agent_scope(
        connection,
        repo,
        worktree,
        role_name,
        input_fingerprint=input_fingerprint,
        state_dir=state_dir,
        effects=effects._scope_lifecycle_effects(),
    )


@dataclass(frozen=True)
class ClearAgentScopeEffects:
    """Named effects for clear_agent_scope; no entry-point/module access."""
    _scope_lifecycle_effects: Callable[..., object]


def clear_agent_scope(effects: ClearAgentScopeEffects, connection: sqlite3.Connection, repo: str) -> None:
    return scope_lifecycle.clear_agent_scope(connection, repo, effects=effects._scope_lifecycle_effects())


@dataclass(frozen=True)
class ValidateWorkerWorkspaceScopeEffects:
    """Named effects for validate_worker_workspace_scope; no entry-point/module access."""
    capture_git_snapshot: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    validate_active_execplan_metadata: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., bytes] | None = None


def validate_worker_workspace_scope(
    effects: ValidateWorkerWorkspaceScopeEffects, repo_path: Path,
    worktree: Path,
    branch: str,
    base_branch: str,
    selected_plan_path: str,
    catalog_plan: sqlite3.Row,
    before: GitSnapshot,
    *,
    allow_rebase: bool = False,
    rebase_definition: execution_revision.DefinitionSnapshot | None = None,
    allowed_requirement_archives: tuple[str, ...] = (),
    allowed_guidance_targets: tuple[str, ...] = (),
    allow_common_plans: bool = False,
    scope_phase: str | None = None,
    connection: sqlite3.Connection | None = None,
    state_dir: Path | None = None,
    on_frontmatter_restored: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Validate one Worker-controlled operation before Manager Git writes."""

    if rebase_definition is not None and not allow_rebase:
        raise ExecPlanMetadataError("rebase 기준 선언은 충돌 복구 중에만 사용할 수 있습니다.")
    after = effects.capture_git_snapshot(worktree)
    unsafe_index = [path for path, entries in after.index.items()
                    if agent_scope_decision._non_default_index_flags(entries)]
    if unsafe_index:
        from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
        raise WorkspaceOwnershipError('Manager Git 작업 전에 index flags를 확인해야 합니다: '
                                      + ', '.join(unsafe_index[:8]))
    if allow_rebase and connection is not None:
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from loop_manager_internal.git.managed_rebase_validation import ObservationFailure, RebaseStatus, rebase_content_pending
        from loop_manager_internal.execution.plan_validation import _current_execplan_metadata
        checkpoint = ExecutionCheckpointRepository(connection).get(str(repo_path), str(catalog_plan['plan_id']))
        if checkpoint.operation == 'rebase':
            from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
            owner_state = state_dir or effects._owned_rebase_state_dir(
                str(repo_path), str(catalog_plan['plan_id']), worktree)
            if owner_state is None:
                raise WorkspaceOwnershipError('현재 관리 rebase의 실행 대상을 확인할 수 없습니다.')
            service = effects._current_rebase_service(connection, repo_path, owner_state,
                str(repo_path), str(catalog_plan['plan_id']), worktree, branch)
            try:
                observed, decision = service.observe(checkpoint=checkpoint)
            except RuntimeError as error:
                from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
                if isinstance(error, RecoveryFailure):
                    raise
                raise WorkspaceOwnershipError(f'현재 관리 rebase를 수용할 수 없습니다: {error}') from error
            if isinstance(observed, ObservationFailure):
                raise WorkspaceOwnershipError(
                    f'현재 관리 rebase를 관측할 수 없습니다: {decision.reason}: '
                    f'{observed.area}: {observed.detail}')
            if decision.status is RebaseStatus.UNAVAILABLE:
                raise WorkspaceOwnershipError(
                    f'현재 관리 rebase를 관측할 수 없습니다: {decision.reason}')
            if decision.status is RebaseStatus.START:
                raise WorkspaceOwnershipError('완료된 Worker 결과에 현재 관리 rebase 시작 상태가 남아 있습니다.')
            content_pending = rebase_content_pending(service.expected, observed)
            if content_pending:
                if decision.status is not RebaseStatus.WAITING:
                    raise WorkspaceOwnershipError('현재 관리 rebase 진행 상태를 증명할 수 없습니다.')
                # The owned intermediate index is not the final Worker output.
                return _current_execplan_metadata(catalog_plan, {})
            if (decision.status is not RebaseStatus.COMPLETE
                    and not (decision.status is RebaseStatus.REJECTED
                             and decision.reason == 'current_scope_mismatch')):
                raise WorkspaceOwnershipError('완료된 Worker 결과의 현재 관리 rebase를 증명할 수 없습니다.')
    resources = (agent_scope_decision.used_git_resources(
        role="worker", phase=scope_phase, base_branch=base_branch,
        branch=branch, worktree_key=str(worktree.resolve()),
    ) if scope_phase is not None else None)
    candidate_base = catalog_plan['active_sha']
    origin = after.refs.get(f'refs/remotes/origin/{base_branch}')
    if origin is not None and not allow_rebase:
        observed = effects.git(worktree, 'merge-base', after.head, origin[0], check=False)
        if observed.returncode or not observed.stdout.strip():
            raise RuntimeError('Worker 범위 비교의 원본 브랜치 기준을 확인할 수 없습니다.')
        candidate_base = observed.stdout.strip()
    publication_head = candidate_base
    if connection is not None and not allow_rebase:
        manual_tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('manual_work_groups', 'manual_work_members')"
        )}
        if manual_tables == {'manual_work_groups', 'manual_work_members'}:
            from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
            from loop_manager_internal.runtime.runtime_identity import _canonical_path
            store = ManualWorkRepository(connection)
            plan_id = str(catalog_plan['plan_id'])
            group = store.for_plan(str(repo_path), plan_id)
            if (group is not None and group['kind'] == 'submit' and group['state'] == 'AUTOMATIC'
                    and group['request_id'] is not None and group['branch'] == branch
                    and group['worktree']
                    and _canonical_path(Path(group['worktree'])) == _canonical_path(worktree)):
                store.workspace_owner(str(repo_path), plan_id)
                publication_head = before.head
    if on_frontmatter_restored is not None and not allow_rebase:
        if after.head != before.head:
            raise RuntimeError('Worker 종료 전후 HEAD가 달라 frontmatter 복원을 보류합니다.')
        effects.validate_managed_worktree(repo_path, worktree, branch)
        if effects.git_bytes is None:
            raise RuntimeError('frontmatter 복원의 원본 byte reader가 없습니다.')
        from loop_manager_internal.execution.plan_validation import restore_worker_frontmatter
        revision = str(catalog_plan['current_revision_sha'] or catalog_plan['active_sha'] or '')
        if restore_worker_frontmatter(worktree, selected_plan_path, catalog_plan,
                                      repo=str(repo_path), revision=revision, connection=connection, git_bytes=effects.git_bytes):
            on_frontmatter_restored(selected_plan_path)
            after = effects.capture_git_snapshot(worktree)
    publication_before = before
    if not allow_rebase and isinstance(candidate_base, str) and candidate_base:
        publication_before = replace(after, head=publication_head, index=after.index, dirty={})
    effects.validate_agent_scope(
        "worker",
        worktree,
        publication_before,
        after,
        selected_plan_path=selected_plan_path,
        allowed_requirement_archives=allowed_requirement_archives,
        allowed_guidance_targets=allowed_guidance_targets,
        allow_common_plans=allow_common_plans,
        external_worktree=repo_path,
        base_branch=base_branch,
        connection=connection,
        resources=resources,
    )
    plan = effects.validate_active_execplan_metadata(
        repo_path,
        worktree,
        selected_plan_path,
        catalog_plan,
        **({'rebase_definition': rebase_definition} if allow_rebase else {}),
    )
    effects.validate_managed_worktree(
        repo_path,
        worktree,
        branch,
        allow_rebase=allow_rebase,
    )
    checkpoint = None
    if allow_rebase and connection is not None:
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        checkpoint = ExecutionCheckpointRepository(connection).get(str(repo_path), str(catalog_plan['plan_id']))
    if allow_rebase and (getattr(after, 'operations', {}) or checkpoint and checkpoint.operation == 'rebase'):
        from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
        plan_id = str(catalog_plan['plan_id'])
        state_dir = state_dir or effects._owned_rebase_state_dir(str(repo_path), plan_id, worktree)
        if state_dir is None or connection is None:
            raise WorkspaceOwnershipError('현재 관리 rebase의 실행 대상을 확인할 수 없습니다.')
        try:
            _observed, decision = effects._current_rebase_service(
                connection, repo_path, state_dir, str(repo_path),
                plan_id, worktree, branch).require_owned(checkpoint=checkpoint)
        except RuntimeError as error:
            raise WorkspaceOwnershipError(f'현재 관리 rebase를 수용할 수 없습니다: {error}') from error
        from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
        if decision.status is RebaseStatus.START:
            raise WorkspaceOwnershipError('현재 관리 rebase의 진행 또는 완료 근거가 없습니다.')
    return plan
