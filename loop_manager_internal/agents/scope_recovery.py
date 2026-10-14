"""Persisted and blocked scope evidence reconciliation before Git recovery."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
import re
from pathlib import Path
import sqlite3
import subprocess

from loop_manager_internal.runtime.runtime_identity import _canonical_path
from loop_manager_internal.agents.scope_contracts import GitSnapshot, ManagerGitCheckpoint, AgentScopeError
from loop_manager_internal.agents.agent_scope_decision import _non_default_index_flags
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.storage import blocked_checkpoint_state, recovery_state
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.git import managed_rebase_adoption, manager_git_journal
from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService
from loop_manager_internal.runtime.process_lock import _held_process_lock_identity
from loop_manager_internal.runtime.contracts import RuntimeMutation, WorkspaceOwnershipError

GUIDANCE_BLOCK_REASON = BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED.value

@dataclass(frozen=True)
class ScopeRecoveryEffects:
    """Explicit effects supplied by the composition boundary."""

    repository: Callable[[sqlite3.Connection], RuntimeStateRepository]

    _agent_git_resources: Callable[..., agent_scope_decision.GitResources | None]
    _config_entries: Callable[..., dict[str, tuple[str, ...]]]
    _current_rebase_service: Callable[..., ManagedRebaseService]
    _normalize_legacy_ignored_snapshot: Callable[..., GitSnapshot]
    _owned_checkpoint_remote_oid: Callable[..., str | None]
    _owned_rebase_state_dir: Callable[..., Path | None]
    _plan_id_from_path: Callable[..., str | None]
    _proven_manager_base_refs: Callable[..., frozenset[str]]
    _proven_merged_cleanup_paths: Callable[..., frozenset[str]]
    _published_plan_allows_common_plans: Callable[..., bool]
    _reconciled_manager_scope_metadata: Callable[..., GitSnapshot]
    _validate_runtime_lock_origin: Callable[..., None]
    _validated_runtime_registration_marker: Callable[..., str | None]
    _with_proven_legacy_ignore_config: Callable[..., GitSnapshot]
    active_execplan: Callable[..., sqlite3.Row | None]
    capture_git_snapshot: Callable[..., GitSnapshot]
    git: Callable[..., subprocess.CompletedProcess[str]]
    github_repository: Callable[[str], str]
    operation_log: Callable[..., None]
    project_execplan: Callable[..., sqlite3.Row | None]
    recover_persisted_agent_worktree: Callable[..., str]
    requirement_archive_candidates: Callable[..., tuple[str, ...]]
    update_project: Callable[..., None]
    validate_agent_scope: Callable[..., None]
    validate_runtime_worktree: Callable[..., None]


def ignored_comparison_view(worktree, start, saved, current, *, effects):
    """Normalize only paths proven ignored by the invocation's starting rules."""
    if any(key.startswith('ignore-source:') for key in current.config):
        saved = effects._with_proven_legacy_ignore_config(saved, current.config)
    if not any(kinds == ("untracked",) and path not in current.dirty
               for path, (kinds, _) in saved.dirty.items()):
        return saved
    return effects._normalize_legacy_ignored_snapshot(
        worktree, saved, current, start=start)


def _proven_recovered_worktree_baseline(
    before: GitSnapshot,
    current: GitSnapshot,
    state_dir: Path,
    repo_path: Path,
    plan_id: str,
    worktree: Path,
    *,
    effects: ScopeRecoveryEffects,
) -> GitSnapshot:
    """Account for only this run's journaled lock on a recreated worktree."""
    key = str(_canonical_path(worktree))
    old = before.worktrees.get(key)
    new = current.worktrees.get(key)
    if (
        old is None or new is None or before.head != current.head
        or before.branch != current.branch or before.index != current.index
        or before.dirty != current.dirty
        or f"HEAD={before.head}" not in old
        or f"branch=refs/heads/{before.branch}" not in old
    ):
        raise AgentScopeError("복구한 Agent worktree의 기존 파일·HEAD 상태가 다릅니다.")
    transitions = manager_git_journal.verified_managed_transitions(
        state_dir, str(_canonical_path(repo_path)), f"plan:{plan_id}",
        git=effects.git, kind='managed_worktree_create', target=key,
    )
    owned = any(
        item["intent"]["kind"] == "managed_worktree_create"
        and item["intent"]["target"] == key
        and item["intent"]["before"] == {
            "exists": False, "registration": None, "index": None,
        }
        and item["result"]["observed"]["registration"] == {
            "head": current.head,
            "branch": f"refs/heads/{current.branch}",
            "locked": item["intent"]["expected"]["registration"]["locked"],
        }
        and set(new) == set(old) | {
            "locked=" + item["intent"]["expected"]["registration"]["locked"]
        }
        for item in transitions
    )
    if not owned:
        raise AgentScopeError("복구한 Agent worktree의 Manager journal 소유권이 없습니다.")
    return replace(before, worktrees={**before.worktrees, key: new})


def _blocked_agent_local_baseline(
    before: GitSnapshot,
    result: GitSnapshot,
    current: GitSnapshot,
    worktree: Path,
    *,
    allowed_dirty_paths: frozenset[str] = frozenset(),
    accept_current_input: bool = False,
) -> GitSnapshot:
    """Compatibility surface for the pure BLOCKED snapshot decision."""
    try:
        return agent_scope_decision.blocked_agent_local_baseline(
            before, result, current, str(_canonical_path(worktree)),
            allowed_dirty_paths=allowed_dirty_paths,
            accept_current_input=accept_current_input,
        )
    except agent_scope_decision.ScopeDecisionError as error:
        raise AgentScopeError(str(error)) from error


def _diagnosed_blocked_commit_allowances(
    before: GitSnapshot,
    blocked_result: GitSnapshot,
    current: GitSnapshot,
    worktree: Path,
    scope_before: GitSnapshot,
) -> tuple[GitSnapshot, frozenset[str], frozenset[str]]:
    """Compatibility surface for the pure diagnosed-commit decision."""
    return agent_scope_decision.diagnosed_blocked_commit_allowances(
        before, blocked_result, current, str(_canonical_path(worktree)), scope_before
    )


def runtime_scope_diagnostic(before: GitSnapshot | None, current: GitSnapshot | None,
                             mutation: RuntimeMutation, *, accepted: bool,
                             result: GitSnapshot | None = None,
                             checkpoint: GitSnapshot | None = None, base_branch: str = 'main') -> str:
    """Report identities and changed keys, never snapshot values or file contents."""
    def identity(snapshot):
        return hashlib.sha256(snapshot.serialize().encode()).hexdigest()[:16] if snapshot else "unavailable"
    base_oid = current.refs.get(f'refs/remotes/origin/{base_branch}', ('', ''))[0] if current else ''
    if not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', base_oid):
        base_oid = 'unavailable'
    def delta(observed):
        changes = []
        if before is not None and observed is not None:
            for label in ("refs", "worktrees", "config", "operations", "index", "dirty"):
                older, newer = getattr(before, label), getattr(observed, label)
                changed = sorted(key for key in older.keys() | newer.keys()
                                 if older.get(key) != newer.get(key))
                if changed:
                    changes.append(f"{label}={','.join(changed[:8])}")
            changes.extend(key for key in ("head", "branch")
                           if getattr(before, key) != getattr(observed, key))
        return '; '.join(changes) or ('none' if observed is not None else 'unavailable')
    return (f"runtime scope {'수락' if accepted else '거부'}: "
            f"before={identity(before)} current={identity(current)} "
            f"result={identity(result)} checkpoint={identity(checkpoint)} "
            f"path={mutation.path} sha={mutation.commit_sha} "
            f"current_base={base_oid} "
            f"delta={delta(current)} result_delta={delta(result)} checkpoint_delta={delta(checkpoint)}")


def validate_persisted_agent_scope(
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path, repo: str, *,
    allow_planner_worktree_correction: bool = False,
    manager_cleanup_worktrees: frozenset[str] = frozenset(),
    runtime_mutation: RuntimeMutation | None = None,
    effects: ScopeRecoveryEffects,
) -> GitSnapshot | None:
    """Inspect the current resume target without replaying an Agent Git delta."""
    if runtime_mutation is not None and _canonical_path(repo_path) == _canonical_path(runtime_mutation.source):
        lease_options = ({'name': f'manual-{runtime_mutation.mutation_id}.lock'}
                         if runtime_mutation.concurrent else {})
        expected = '0' if runtime_mutation.legacy else f'runtime:{runtime_mutation.mutation_id}'
        if _held_process_lock_identity(state_dir, **lease_options) != expected:
            raise WorkspaceOwnershipError('runtime 재개 중 process lock identity가 다릅니다.')
        effects.validate_runtime_worktree(
            runtime_mutation.source, state_dir, runtime_mutation.path, runtime_mutation.commit_sha,
            **({'command_id': runtime_mutation.mutation_id} if runtime_mutation.concurrent else {}))
        if effects._validated_runtime_registration_marker(
                runtime_mutation.source, state_dir, runtime_mutation.path,
                runtime_mutation.commit_sha, runtime_mutation.mutation_id,
                legacy=runtime_mutation.legacy) != runtime_mutation.registration_lock_reason:
            raise WorkspaceOwnershipError('runtime 재개 중 등록 identity가 다릅니다.')
    project = effects.repository(connection).scope_owner(repo)
    if project is None:
        return None
    if not project['worktree']:
        if project['agent_snapshot']:
            raise WorkspaceOwnershipError('재개할 Agent 작업 공간이 없습니다.')
        return None
    worktree = Path(str(project['worktree']))
    if not worktree.is_dir():
        effects.recover_persisted_agent_worktree(connection, repo_path, state_dir, repo, project)
    current = effects.capture_git_snapshot(worktree)
    recovery = recovery_state.RecoveryRepository(connection)
    recovery.resolve_git_scope_failure(repo, effects._plan_id_from_path(str(project['plan_path'] or '')))
    recovery.resolve_git_scope_failure(repo, None)
    return current


def refresh_persisted_agent_scope_after_manager_git(
    connection: sqlite3.Connection,
    repo: str,
    validated: GitSnapshot,
    manager_worktree_changes: frozenset[str],
    *,
    state_dir: Path | None = None,
    effects: ScopeRecoveryEffects,
) -> GitSnapshot:
    """Record the Manager result without attributing Git changes to an Agent."""
    project = effects.repository(connection).scope_owner(repo)
    if project is None or not project['worktree']:
        return validated
    current = effects.capture_git_snapshot(Path(str(project['worktree'])))
    effects.update_project(connection, repo,
        agent_manager_snapshot=ManagerGitCheckpoint(current, frozenset()).serialize())
    return current


def _remote_adoption_agent_baseline(
    connection,
    repo,
    plan_id,
    worktree,
    project,
    before,
    current,
    checkpoint,
    *,
    saved_plan = None,
    accepted_rebase = False,
    effects: ScopeRecoveryEffects,
):
    """Share the exact Manager adoption boundary across all Agent entry points."""
    serialized = project['agent_snapshot']
    adopted = (checkpoint.snapshot or {}).get('remote_adoption_scope')
    pending = (checkpoint.snapshot or {}).get('remote_adoption_intent')
    if pending and not accepted_rebase and saved_plan is not None:
        request = blocked_checkpoint_state.scope_request_row(connection, repo, plan_id)
        if (request is None or request['request_identity'] != pending['request_identity']
                or request['pending_remote_oid'] not in {None, pending['head']}
                or pending['branch'] != saved_plan['branch'] or pending['worktree'] != str(worktree)
                or pending['agent_snapshot'] != serialized
                or pending['agent_result_snapshot'] != project['agent_result_snapshot']
                or project['agent_recovery']):
            raise AgentScopeError('미완료 원격 채택 identity가 현재 범위와 다릅니다.')
        original = GitSnapshot.deserialize(pending['snapshot'])
        if current.head == pending['head']:
            own_ref = f"refs/heads/{pending['branch']}"
            registration = current.worktrees.get(str(_canonical_path(worktree)), ())
            if (current.branch != pending['branch'] or current.refs.get(own_ref) != (current.head, '')
                    or f'branch={own_ref}' not in registration
                    or current.operations or current.dirty
                    or any(_non_default_index_flags(entries) for entries in current.index.values())
                    or effects.git(worktree, 'diff', '--cached', '--quiet', check=False).returncode != 0):
                raise AgentScopeError('미완료 원격 채택의 HEAD·index·작업 범위가 다릅니다.')
            before = managed_rebase_adoption.selected_baseline(
                original, current, pending['branch'], str(_canonical_path(worktree)))
            remote_ref = f"refs/remotes/origin/{pending['branch']}"
            if current.refs.get(remote_ref) == (pending['head'], ''):
                before = replace(before, refs={**before.refs, remote_ref: (pending['head'], '')})
            project = {**dict(project), 'agent_result_snapshot': None}
        elif current.head != original.head:
            raise AgentScopeError('미완료 원격 채택과 다른 로컬 HEAD를 보존합니다.')
        else:
            before = original
            project = {**dict(project), 'agent_result_snapshot': None}
    adoption_plan = saved_plan
    if (adopted and not accepted_rebase and adoption_plan is None
            and effects.repository(connection).scope_catalog_available()):
        adoption_plan = effects.repository(connection).scope_owner(repo, plan_id)
    if (adopted and not pending and not accepted_rebase and adoption_plan is not None
            and adopted['agent_snapshot'] == serialized
            and adopted['branch'] == adoption_plan['branch']
            and adopted['worktree'] == str(worktree)
            and adopted['head'] == checkpoint.expected_head
            and adopted.get('remote', adopted['head']) == checkpoint.observed_remote_oid
            and not project['agent_recovery']):
        # R-039 validated the preserved Agent output before the Manager's
        # fast-forward. Compare later changes to that exact durable result;
        # never rewrite the Agent audit or absorb subsequent local Git writes.
        before = GitSnapshot.deserialize(adopted['snapshot'])
        if project['agent_result_snapshot'] == adopted['agent_result_snapshot']:
            project = {**dict(project), 'agent_result_snapshot': None}
    return before, project


def validate_blocked_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    worktree: Path,
    selected_plan_path: str,
    *,
    accepted_rebase: bool = False,
    saved_plan=None,
    require_safe_result: bool = False,
    changed_candidate: bool = False,
    effects: ScopeRecoveryEffects,
) -> GitSnapshot | None:
    """Return current candidate evidence; callers validate their Git operation."""
    project = saved_plan if saved_plan is not None else effects.repository(connection).scope_owner(repo)
    if project is None:
        raise WorkspaceOwnershipError('재개할 작업을 찾을 수 없습니다.')
    if not (project['agent_snapshot'] or changed_candidate or require_safe_result):
        return None
    return effects.capture_git_snapshot(worktree)
