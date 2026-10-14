"""Start/resume scope baselines while preserving repository transition ordering."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sqlite3
import secrets

from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents.agent_scope_decision import _same_local_snapshot
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService


@dataclass(frozen=True)
class ScopeLifecycleEffects:
    """Explicit effects supplied by the composition boundary."""

    repository: Callable[[sqlite3.Connection], RuntimeStateRepository]

    _agent_git_resources: Callable[..., agent_scope_decision.GitResources | None]
    _capture_agent_baseline: Callable[..., str | None]
    _current_rebase_service: Callable[..., ManagedRebaseService]
    _owned_rebase_state_dir: Callable[..., Path | None]
    _plan_id_from_path: Callable[..., str | None]
    _published_plan_allows_common_plans: Callable[..., bool]
    _reconciled_manager_scope_metadata: Callable[..., GitSnapshot]
    _remote_adoption_agent_baseline: Callable[..., tuple[GitSnapshot, Mapping[str, object]]]
    capture_git_snapshot: Callable[..., GitSnapshot]
    project_execplan: Callable[..., sqlite3.Row | None]
    update_project: Callable[..., None]
    validate_agent_scope: Callable[..., None]


def begin_agent_scope(
    connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    role_name: str,
    *,
    input_fingerprint: str | None = None,
    state_dir: Path | None = None,
    effects: ScopeLifecycleEffects,
) -> GitSnapshot:
    row = effects.repository(connection).scope_owner(repo)
    if row is None:
        raise RuntimeError("Agent scope를 저장할 프로젝트가 없습니다.")
    if row['agent_snapshot'] and row['agent_role'] == role_name:
        if role_name == 'worker' and input_fingerprint is not None:
            effects.update_project(connection, repo, agent_input_fingerprint=input_fingerprint)
        current = effects.capture_git_snapshot(worktree)
        try:
            stored = GitSnapshot.deserialize(str(row['agent_snapshot']))
        except (ValueError, TypeError, KeyError, RuntimeError):
            return current
        return replace(current, scope_id=stored.scope_id,
                       scope_start_digest=stored.scope_start_digest)
    if row['agent_snapshot']:
        plan_id = effects._plan_id_from_path(str(row['plan_path'] or '')) or ''
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
        checkpoint = checkpoints.get(repo, plan_id)
        saved = dict(checkpoint.snapshot or {})
        checkpoints.record(repo, plan_id, expected_generation=checkpoint.generation,
            operation=checkpoint.operation, expected_head=checkpoint.expected_head,
            target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
            snapshot={**saved, 'retired_agent_scopes': [*saved.get('retired_agent_scopes', ()),
                {key: dict(row).get(key) for key in ('agent_role', 'agent_snapshot',
                                          'agent_result_snapshot', 'agent_manager_snapshot')}]})
    preliminary_snapshot = effects.capture_git_snapshot(worktree)
    agent_baseline = effects._capture_agent_baseline(
        repo,
        worktree,
        preliminary_snapshot,
    )
    snapshot = (
        effects.capture_git_snapshot(worktree)
        if agent_baseline is not None
        else preliminary_snapshot
    )
    if not _same_local_snapshot(preliminary_snapshot, snapshot):
        raise RuntimeError("Agent 시작 상태를 보존하는 동안 worktree가 변경됐습니다.")
    snapshot = replace(snapshot, scope_id=secrets.token_hex(16),
                       scope_start_digest=hashlib.sha256(snapshot.serialize().encode()).hexdigest())
    plan_path = str(row["plan_path"] or "")
    plan_file = worktree / plan_path
    agent_input_plan = (
        plan_file.read_text(encoding="utf-8")
        if role_name in {"worker", "reviewer"} and plan_path and plan_file.is_file()
        else None
    )
    effects.update_project(
        connection,
        repo,
        agent_role=role_name,
        agent_snapshot=snapshot.serialize(),
        agent_input_fingerprint=input_fingerprint,
        agent_baseline=agent_baseline,
        agent_recovery=None,
        agent_input_plan=agent_input_plan,
        agent_result_plan=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )
    return snapshot


def clear_agent_scope(connection: sqlite3.Connection, repo: str, *, effects: ScopeLifecycleEffects) -> None:
    effects.update_project(
        connection,
        repo,
        agent_role=None,
        agent_snapshot=None,
        agent_baseline=None,
        agent_recovery=None,
        agent_input_plan=None,
        agent_result_plan=None,
        agent_input_fingerprint=None,
        agent_result_snapshot=None,
        agent_manager_snapshot=None,
    )
