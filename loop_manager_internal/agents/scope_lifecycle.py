"""Start/resume scope baselines while preserving repository transition ordering."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sqlite3
import secrets

from loop_manager_internal.agents.scope_contracts import GitSnapshot, AgentScopeError
from loop_manager_internal.agents.agent_scope_decision import _same_local_snapshot
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.git import managed_rebase_adoption
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
    if row['agent_snapshot'] and row['agent_role'] == 'reviewer' and role_name == 'worker':
        # Rebase requires a new Worker turn. Recheck the preserved Reviewer
        # output and live Manager result before beginning a distinct scope.
        plan_id = effects._plan_id_from_path(str(row['plan_path'] or ''))
        scope_state_dir = state_dir or effects._owned_rebase_state_dir(repo, plan_id, worktree)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id or '')
        if (scope_state_dir is None or not plan_id
                or not (checkpoint.snapshot or {}).get('current_rebase_acceptance')):
            raise RuntimeError('Reviewer scope에서 Worker 재검증으로 전환할 rebase 증거가 없습니다.')
        _, verdict = effects._current_rebase_service(
            connection, Path(repo), scope_state_dir, repo, plan_id,
            worktree, row['branch']).require_owned(checkpoint=checkpoint)
        if verdict.status is not managed_rebase_adoption.RebaseStatus.COMPLETE:
            raise RuntimeError('Reviewer scope의 관리 rebase가 완료되지 않았습니다.')
        original = GitSnapshot.deserialize(str(row['agent_snapshot']))
        current = effects.capture_git_snapshot(worktree)
        comparison = agent_scope_decision.manager_comparison_baseline(
            original, original, checkpoint.snapshot or {}, 'reviewer')
        if row['agent_result_snapshot']:
            result = agent_scope_decision.manager_comparison_baseline(
                original, GitSnapshot.deserialize(str(row['agent_result_snapshot'])),
                checkpoint.snapshot or {}, 'reviewer')
            effects.validate_agent_scope('reviewer', worktree, comparison, result,
                external_worktree=Path(repo), base_branch=row['base_branch'], connection=connection)
        baseline = managed_rebase_adoption.selected_baseline(
            original, current, row['branch'], str(worktree.resolve()), publication=checkpoint)
        effects.validate_agent_scope('reviewer', worktree, baseline, current,
            external_worktree=Path(repo), base_branch=row['base_branch'], connection=connection)
        managed_rebase_adoption.remember_agent_baseline(
            execution_checkpoint_state.ExecutionCheckpointRepository(connection),
            repo, plan_id, original, baseline, 'reviewer')
        row = {**dict(row), 'agent_snapshot': None}
    if row["agent_snapshot"]:
        if row["agent_role"] != role_name:
            raise RuntimeError(
                f"저장된 Agent 역할이 현재 역할과 다릅니다: "
                f"{row['agent_role']} -> {role_name}"
            )
        if (
            role_name == "worker" and input_fingerprint is not None
            and row["agent_input_fingerprint"] != input_fingerprint
        ):
            # A resumed pre-contract Worker scope may have saved its local
            # plan fingerprint. The new turn's remote input is authoritative.
            effects.update_project(
                connection, repo, agent_input_fingerprint=input_fingerprint,
            )
        stored = GitSnapshot.deserialize(str(row['agent_snapshot']))
        original_scope = stored
        plan_id = effects._plan_id_from_path(str(row['plan_path'] or ''))
        scope_state_dir = state_dir or (
            effects._owned_rebase_state_dir(repo, plan_id, worktree) if plan_id else None)
        if scope_state_dir is not None:
            stored = effects._reconciled_manager_scope_metadata(
                connection, Path(repo), scope_state_dir, repo, plan_id, stored,
                effects.capture_git_snapshot(worktree),
                effects._agent_git_resources(connection, Path(repo), worktree, stored, role_name,
                                     selected_plan_path=str(row['plan_path'] or '')))
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id or '')
        if stored != original_scope:
            checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
            saved = dict(checkpoint.snapshot or {})
            previous = saved.get('manager_metadata_agent_baseline')
            if previous is not None:
                saved['manager_metadata_agent_baseline_history'] = [
                    *saved.get('manager_metadata_agent_baseline_history', ()), previous]
            checkpoints.record(repo, plan_id or '', expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**saved, 'manager_metadata_agent_baseline': {
                    'role': role_name, 'original': hashlib.sha256(original_scope.serialize().encode()).hexdigest(),
                    'snapshot': stored.serialize()}})
            checkpoint = checkpoints.get(repo, plan_id or '')
        if (plan_id and not blocked_plan_reconciliation.remote_adoption_is_current(checkpoint)
                and (checkpoint.operation == 'rebase'
                     or (checkpoint.snapshot or {}).get('current_rebase_acceptance'))):
            current = effects.capture_git_snapshot(worktree)
            if not managed_rebase_adoption.needs_agent_comparison(checkpoint, stored, current):
                return stored
            state_dir = effects._owned_rebase_state_dir(repo, plan_id, worktree)
            if state_dir is None:
                raise AgentScopeError('accepted rebase workspace unavailable')
            effects._current_rebase_service(connection, Path(repo), state_dir, repo,
                                    plan_id, worktree, row['branch']).require_owned()
            effective = managed_rebase_adoption.selected_baseline(
                stored, current, row['branch'], str(worktree.resolve()), publication=checkpoint)
            managed_rebase_adoption.remember_agent_baseline(
                execution_checkpoint_state.ExecutionCheckpointRepository(connection),
                repo, plan_id, original_scope, effective, role_name)
            return effective
        if plan_id and (checkpoint.snapshot or {}).get('remote_adoption_scope'):
            current = effects.capture_git_snapshot(worktree)
            effective, _ = effects._remote_adoption_agent_baseline(
                connection, repo, plan_id, worktree, row, stored, current, checkpoint)
            effects.validate_agent_scope(role_name, worktree, effective, current,
                                 selected_plan_path=row['plan_path'] if role_name == 'worker' else None,
                                 allow_common_plans=effects._published_plan_allows_common_plans(
                                     Path(repo), effects.project_execplan(connection, repo)),
                                 external_worktree=Path(repo), base_branch=row['base_branch'], connection=connection)
            return effective
        return stored
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
        if role_name == "reviewer" and plan_path and plan_file.is_file()
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
