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
from loop_manager_internal.storage import execution_checkpoint_state, blocked_checkpoint_state
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode, SCOPE_BASELINE_REASONS
from loop_manager_internal.git import managed_rebase_validation, managed_rebase_adoption, manager_git_journal
from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService
from loop_manager_internal.runtime.process_lock import _held_process_lock_identity
from loop_manager_internal.runtime.contracts import RuntimeMutation, WorkspaceOwnershipError
from loop_manager_internal.runtime import runtime_scope
from loop_manager_internal.git.base_fetch_service import reconcile_external_base

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
    before = current = saved_result = saved_checkpoint = None
    base_branch = 'main'
    diagnose = (runtime_mutation is not None
                and _canonical_path(repo_path) == _canonical_path(runtime_mutation.source))
    if diagnose:
        # Capture original evidence before any reconciliation or early rejection.
        project = effects.repository(connection).scope_owner(repo)
        if project is not None and 'base_branch' in project.keys():
            base_branch = str(project['base_branch'])
        if project is not None and project['agent_snapshot']:
            try:
                before = GitSnapshot.deserialize(str(project['agent_snapshot']))
                if 'agent_result_snapshot' in project.keys() and project['agent_result_snapshot']:
                    saved_result = GitSnapshot.deserialize(str(project['agent_result_snapshot']))
                if 'agent_manager_snapshot' in project.keys() and project['agent_manager_snapshot']:
                    saved_checkpoint = ManagerGitCheckpoint.deserialize(str(project['agent_manager_snapshot'])).snapshot
                worktree = Path(str(project['worktree'] or repo))
                if worktree.is_dir():
                    current = effects.capture_git_snapshot(worktree)
            except (RuntimeError, ValueError, TypeError, KeyError, OSError):
                pass  # Diagnostic availability must not change the validation verdict.
    try:
        result = _validate_persisted_agent_scope(
            connection, repo_path, state_dir, repo,
            allow_planner_worktree_correction=allow_planner_worktree_correction,
            manager_cleanup_worktrees=manager_cleanup_worktrees,
            runtime_mutation=runtime_mutation, effects=effects)
    except (RuntimeError, ValueError, KeyError, OSError):
        if diagnose:
            effects.operation_log(runtime_scope_diagnostic(before, current, runtime_mutation,
                                                          accepted=False, result=saved_result,
                                                          checkpoint=saved_checkpoint, base_branch=base_branch), error=True)
        raise
    if diagnose:
        effects.operation_log(runtime_scope_diagnostic(before, current, runtime_mutation,
                                                      accepted=True, result=saved_result,
                                                      checkpoint=saved_checkpoint, base_branch=base_branch))
    return result


def _validate_persisted_agent_scope(
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    allow_planner_worktree_correction: bool = False,
    manager_cleanup_worktrees: frozenset[str] = frozenset(),
    runtime_mutation: RuntimeMutation | None = None,
    effects: ScopeRecoveryEffects,
) -> GitSnapshot | None:
    """Validate persisted Agent output before repository-aware Git work."""

    project = effects.repository(connection).scope_owner(repo)
    if project is None or not project["agent_snapshot"]:
        return None
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"저장된 Agent Git snapshot이 유효하지 않습니다: {error}"
        ) from error
    if not before.shared_metadata_complete:
        raise AgentScopeError("저장된 Agent Git metadata 기준 snapshot이 없습니다.")

    saved_worktree = Path(str(project["worktree"] or ""))
    resources = effects._agent_git_resources(
        connection, repo_path, saved_worktree, before, str(project['agent_role'] or ''),
        selected_plan_path=str(project['plan_path'] or ''),
    ) if saved_worktree.is_dir() else None
    config_context = saved_worktree if saved_worktree.is_dir() else repo_path
    current_config = effects._config_entries(config_context)
    before = effects._with_proven_legacy_ignore_config(before, current_config)
    before_config = resources.config(before.config) if resources is not None else before.config
    scope_config = resources.config(current_config) if resources is not None else current_config
    changed_keys = sorted(
        key
        for key in set(before_config) | set(scope_config)
        if before_config.get(key) != scope_config.get(key)
    )
    if changed_keys:
        displayed = ", ".join(changed_keys[:8])
        remainder = len(changed_keys) - 8
        if remainder > 0:
            displayed += f", 외 {remainder}개"
        raise AgentScopeError(
            f"저장된 Agent Git config 변경 ({displayed})",
            reason_code=BlockReasonCode.SHARED_GIT_CONFIG_MUTATION,
        )
    role_name = str(project["agent_role"] or "")
    if role_name not in {"planner", "planner_check", "maintenance_planner", "worker", "reviewer"}:
        raise AgentScopeError(
            f"저장된 Agent 역할이 유효하지 않습니다: {role_name or '없음'}"
        )
    selected_plan_path = str(project["plan_path"] or "")
    if role_name == "worker" and not selected_plan_path:
        raise AgentScopeError("저장된 Worker ExecPlan 경로가 없습니다.")
    guidance_targets: tuple[str, ...] = ()
    archive_sources: tuple[str, ...] = ()
    allow_common_plans = False
    if role_name == "worker":
        active_plan = effects.active_execplan(connection, repo) or effects.project_execplan(
            connection, repo
        )
        if active_plan is None:
            raise AgentScopeError("저장된 Worker ExecPlan 상태가 없습니다.")
        allow_common_plans = effects._published_plan_allows_common_plans(
            repo_path, active_plan
        )
        archive_sources = effects.requirement_archive_candidates(
            connection, repo_path, repo,
            str(active_plan['current_revision_sha'] or active_plan['plan_sha']),
            str(active_plan['plan_id']))
    recovered_worktree = False
    if not saved_worktree.is_dir():
        recovered_worktree = effects.recover_persisted_agent_worktree(
            connection, repo_path, state_dir, repo, project
        ) == "recovered"

    current = effects.capture_git_snapshot(saved_worktree)
    if recovered_worktree:
        plan_id = effects._plan_id_from_path(selected_plan_path)
        if not plan_id:
            raise AgentScopeError("복구한 Agent worktree의 ExecPlan identity가 없습니다.")
        previous_registration = before.worktrees.get(
            str(_canonical_path(saved_worktree))
        )
        before = _proven_recovered_worktree_baseline(
            before, current, state_dir, repo_path, plan_id, saved_worktree,
            effects=effects,
        )
        recovered_baseline = before
    else:
        recovered_baseline = None
    proven_cleanups = effects._proven_merged_cleanup_paths(
        connection, repo, before, current,
    )
    manager_cleanup_worktrees = proven_cleanups
    phase = str(project["phase"] or "")
    guidance_ref_allowances: frozenset[str] | None = None
    if phase == "PLAN_BLOCKED" and role_name == "worker":
        plan_id = effects._plan_id_from_path(selected_plan_path)
        plan_row = effects.repository(connection).scope_owner(repo, plan_id) if plan_id else None
        if plan_row is not None and plan_row["blocked_reason"] == GUIDANCE_BLOCK_REASON:
            owned = set(effects._proven_manager_base_refs(
                connection, repo_path, repo, plan_id, before, current,
            ))
            checkpoint_row = blocked_checkpoint_state.scope_request_row(connection, repo, plan_id)
            if checkpoint_row is not None:
                request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
                    repo, plan_id,
                )
                if effects._owned_checkpoint_remote_oid(
                    connection, repo_path, request, plan_row, current,
                ) is not None:
                    owned.add(f"refs/remotes/origin/{plan_row['branch']}")
                    owned.add(f"refs/heads/{plan_row['branch']}")
            guidance_ref_allowances = frozenset(owned)
    result: GitSnapshot | None = None
    checkpoint: ManagerGitCheckpoint | None = None
    if allow_planner_worktree_correction and project["agent_result_snapshot"]:
        try:
            result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 Planner 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error
    elif project["agent_manager_snapshot"]:
        try:
            checkpoint = ManagerGitCheckpoint.deserialize(
                str(project["agent_manager_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 Manager Git checkpoint가 유효하지 않습니다: {error}"
            ) from error
    elif phase == "PLAN_BLOCKED" and project["agent_result_snapshot"]:
        try:
            result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Agent 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error
    recovered_result_baseline = None
    if recovered_worktree and result is not None:
        key = str(_canonical_path(saved_worktree))
        if result.worktrees.get(key) != previous_registration:
            raise AgentScopeError(
                "복구한 Agent worktree의 저장된 결과 등록 상태가 다릅니다."
            )
        result = replace(
            result, worktrees={**result.worktrees, key: current.worktrees[key]},
        )
        recovered_result_baseline = result
    recovered_manager_checkpoint = None
    if recovered_worktree and checkpoint is not None:
        key = str(_canonical_path(saved_worktree))
        if checkpoint.snapshot.worktrees.get(key) != previous_registration:
            raise AgentScopeError(
                "복구한 Agent worktree의 Manager checkpoint 등록 상태가 다릅니다."
            )
        checkpoint = replace(checkpoint, snapshot=replace(
            checkpoint.snapshot,
            worktrees={**checkpoint.snapshot.worktrees,
                       key: current.worktrees[key]},
        ))
        recovered_manager_checkpoint = checkpoint

    start = before
    def comparison_view(value):
        return ignored_comparison_view(saved_worktree, start, value, current, effects=effects)

    before = comparison_view(before)
    if result is not None:
        result = effects._with_proven_legacy_ignore_config(result, current_config)
        result = comparison_view(result)
    if checkpoint is not None:
        checkpoint = replace(
            checkpoint,
            snapshot=comparison_view(checkpoint.snapshot),
        )

    def reconcile_metadata(value, observed=current):
        return effects._reconciled_manager_scope_metadata(
            connection, repo_path, state_dir, repo,
            effects._plan_id_from_path(selected_plan_path), value, observed, resources)
    rebase_checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, effects._plan_id_from_path(selected_plan_path) or '')
    remote_adoption_current = blocked_plan_reconciliation.remote_adoption_is_current(rebase_checkpoint)
    before = reconcile_metadata(before)
    if result is not None:
        result = reconcile_metadata(result)
    if checkpoint is not None:
        checkpoint = replace(checkpoint, snapshot=reconcile_metadata(checkpoint.snapshot))
        checkpoint = replace(checkpoint, cleanup_worktrees=checkpoint.cleanup_worktrees & proven_cleanups)

    if (role_name in {'worker', 'reviewer'} and not remote_adoption_current
            and (rebase_checkpoint.snapshot or {}).get('current_rebase_agent_baseline')):
        comparison = managed_rebase_adoption.agent_comparison_baseline(
            GitSnapshot.deserialize(str(project['agent_snapshot'])),
            rebase_checkpoint.snapshot or {}, role_name)
        before = comparison_view(comparison)
        before = reconcile_metadata(before)
    if role_name in {'worker', 'reviewer'} and remote_adoption_current:
        plan_id = effects._plan_id_from_path(selected_plan_path)
        plan = effects.repository(connection).scope_owner(repo, plan_id)
        if plan is None or plan['branch'] != project['branch'] or plan['worktree'] != str(saved_worktree):
            raise AgentScopeError('원격 채택의 계획·worktree identity가 다릅니다.')
        before, adoption_project = _remote_adoption_agent_baseline(
            connection, repo, plan_id, saved_worktree, project, before, current,
            rebase_checkpoint, saved_plan=plan, effects=effects)
        if adoption_project['agent_result_snapshot'] is None:
            result = None
            checkpoint = None
        else:
            result = comparison_view(GitSnapshot.deserialize(str(adoption_project['agent_result_snapshot'])))
            effects.validate_agent_scope(
                role_name, saved_worktree, before, result,
                selected_plan_path=selected_plan_path if role_name == 'worker' else None,
                allowed_guidance_targets=guidance_targets, allow_common_plans=allow_common_plans,
                allowed_requirement_archives=archive_sources,
                external_worktree=repo_path, base_branch=str(project['base_branch']), connection=connection)
    rebase_scope_normalized = False
    if (role_name in {'worker', 'reviewer'}
            and not remote_adoption_current
            and managed_rebase_adoption.needs_agent_comparison(rebase_checkpoint,
                GitSnapshot.deserialize(str(project['agent_snapshot'])), current)):
        _, rebase_verdict = effects._current_rebase_service(
            connection, repo_path, state_dir, repo,
            effects._plan_id_from_path(selected_plan_path), saved_worktree, project['branch'],
        ).require_owned(checkpoint=rebase_checkpoint)
        agent_result = result or (checkpoint.snapshot if checkpoint else None)
        if agent_result is None and project['agent_result_snapshot']:
            agent_result = GitSnapshot.deserialize(str(project['agent_result_snapshot']))
        if agent_result is not None:
            if project['agent_result_snapshot']:
                agent_result = GitSnapshot.deserialize(str(project['agent_result_snapshot']))
            comparison = managed_rebase_adoption.agent_comparison_baseline(
                GitSnapshot.deserialize(str(project['agent_snapshot'])),
                rebase_checkpoint.snapshot or {}, role_name)
            comparison = agent_scope_decision.manager_comparison_baseline(
                GitSnapshot.deserialize(str(project['agent_snapshot'])), comparison,
                rebase_checkpoint.snapshot or {}, role_name)
            agent_result = agent_scope_decision.manager_comparison_baseline(
                GitSnapshot.deserialize(str(project['agent_snapshot'])), agent_result,
                rebase_checkpoint.snapshot or {}, role_name)
            comparison = comparison_view(comparison)
            agent_result = comparison_view(agent_result)
            effects.validate_agent_scope(
                role_name, saved_worktree, comparison, agent_result,
                selected_plan_path=selected_plan_path if role_name == 'worker' else None,
                allowed_guidance_targets=guidance_targets,
                allowed_requirement_archives=archive_sources,
                allow_common_plans=allow_common_plans,
                external_worktree=repo_path, base_branch=str(project['base_branch']), connection=connection,
                owned_rebase=rebase_verdict.status in {
                    managed_rebase_validation.RebaseStatus.WAITING,
                    managed_rebase_validation.RebaseStatus.COMPLETE})
        def accepted_baseline(value):
            return managed_rebase_adoption.selected_baseline(
                value, current, project['branch'], str(saved_worktree.resolve()),
                publication=rebase_checkpoint)
        rebase_scope_normalized = True
        before = accepted_baseline(before)
        if result is not None:
            result = accepted_baseline(result)
        if checkpoint is not None:
            checkpoint = replace(checkpoint, snapshot=accepted_baseline(checkpoint.snapshot))

    manager_runtime: tuple[str, str] | None = None
    runtime_base_oid = None
    runtime_recreation_proven = False
    if runtime_mutation is not None and _canonical_path(repo_path) == _canonical_path(runtime_mutation.source):
        try:
            lease_options = ({"name": f"manual-{runtime_mutation.mutation_id}.lock"}
                             if runtime_mutation.concurrent else {})
            held_identity = _held_process_lock_identity(state_dir, **lease_options)
            expected_identity = (
                "0" if runtime_mutation.legacy
                else f"runtime:{runtime_mutation.mutation_id}"
            )
            if held_identity != expected_identity:
                raise WorkspaceOwnershipError(
                    "runtime scope 검증 중 부모 process lock identity가 변경됐습니다."
                )
            effects.validate_runtime_worktree(
                runtime_mutation.source, state_dir,
                runtime_mutation.path, runtime_mutation.commit_sha,
                **({"command_id": runtime_mutation.mutation_id} if runtime_mutation.concurrent else {}),
            )
            current_lock_reason = effects._validated_runtime_registration_marker(
                runtime_mutation.source, state_dir,
                runtime_mutation.path, runtime_mutation.commit_sha,
                runtime_mutation.mutation_id, legacy=runtime_mutation.legacy,
            )
            if current_lock_reason != runtime_mutation.registration_lock_reason:
                raise WorkspaceOwnershipError(
                    "runtime 등록의 잠금 표식이 자식 시작 이후 변경됐습니다."
                )
        except RuntimeError as error:
            raise AgentScopeError(f"runtime mutation 검증 실패: {error}") from error
        manager_runtime = (
            str(_canonical_path(runtime_mutation.path)), runtime_mutation.commit_sha,
        )
        ref = f"refs/remotes/origin/{project['base_branch']}"
        runtime_selection = replace(before, refs={**before.refs, ref: (runtime_mutation.commit_sha, '')})
        if runtime_selection.refs.get('refs/remotes/origin/HEAD') is not None:
            runtime_selection = replace(runtime_selection, refs={**runtime_selection.refs,
                'refs/remotes/origin/HEAD': (runtime_mutation.commit_sha, ref)})
        verified = reconcile_external_base(effects.git, repo_path, project['base_branch'],
                                           runtime_selection, current, resources,
                                           registered_origin=str(project['origin'] or ''))
        if verified is not runtime_selection:
            runtime_base_oid = verified.refs[ref][0]
        runtime_recreation_proven = runtime_scope.proven_runtime_recreation(
            state_dir, _canonical_path(runtime_mutation.source),
            _canonical_path(runtime_mutation.path), runtime_mutation.commit_sha,
            before.worktrees.get(manager_runtime[0]), current.worktrees.get(manager_runtime[0]),
            validate_lock=lambda lock: effects._validate_runtime_lock_origin(
                state_dir, runtime_mutation.source, runtime_mutation.path,
                runtime_mutation.commit_sha, lock,
            ),
        )

    try:
        decision = agent_scope_decision.decide_persisted_scope(
            role=role_name,
            phase=phase,
            base_branch=str(project["base_branch"]),
            selected_plan_path=selected_plan_path,
            before=before,
            current=current,
            result=result,
            checkpoint=checkpoint,
            worktree_key=str(_canonical_path(saved_worktree)),
            repo_key=str(_canonical_path(repo_path)),
            planner_correction=allow_planner_worktree_correction,
            manager_cleanup_worktrees=manager_cleanup_worktrees,
            manager_runtime=manager_runtime,
            runtime_recreation_proven=runtime_recreation_proven,
            manager_refs=guidance_ref_allowances or frozenset(),
            runtime_base_oid=runtime_base_oid,
        )
    except agent_scope_decision.ScopeDecisionError as error:
        raise AgentScopeError(str(error)) from error
    if (project['agent_result_snapshot'] and not allow_planner_worktree_correction
            and (phase != 'PLAN_BLOCKED' or guidance_ref_allowances is not None)
            and not remote_adoption_current
            and rebase_checkpoint.operation != 'rebase'
            and not (rebase_checkpoint.snapshot or {}).get('current_rebase_acceptance')):
        original = GitSnapshot.deserialize(str(project['agent_snapshot']))
        comparison = agent_scope_decision.manager_comparison_baseline(
            original, original, rebase_checkpoint.snapshot or {}, role_name)
        comparison = comparison_view(comparison)
        actual_result = comparison_view(GitSnapshot.deserialize(str(project['agent_result_snapshot'])))
        if manager_runtime is not None:
            # The runtime identity and exact registration were checked above.
            # Carry only unchanged original keys; retain actual Agent mutations.
            comparison = agent_scope_decision.reconcile_runtime_snapshot(
                comparison, original, current, manager_runtime[0], str(project["base_branch"]))
            actual_result = agent_scope_decision.reconcile_runtime_snapshot(
                actual_result, original, current, manager_runtime[0], str(project["base_branch"]))
        effects.validate_agent_scope(role_name, saved_worktree, comparison, actual_result,
            selected_plan_path=selected_plan_path if role_name == 'worker' else None,
            allowed_guidance_targets=guidance_targets, allow_common_plans=allow_common_plans,
            allowed_requirement_archives=archive_sources,
            external_worktree=repo_path, base_branch=str(project['base_branch']), resources=resources,
            connection=connection)
    for check in decision.checks:
        try:
            check_refs = check.allowed_refs
            check_worktrees = check.allowed_worktrees
            if guidance_ref_allowances is not None:
                check_refs &= guidance_ref_allowances
                check_worktrees = (
                    check_worktrees & (proven_cleanups | frozenset((str(_canonical_path(saved_worktree)),)))
                ) | proven_cleanups
            effects.validate_agent_scope(
                check.role,
                saved_worktree,
                check.before,
                check.after,
                selected_plan_path=(
                    selected_plan_path if check.role == "worker" else None
                ),
                allowed_guidance_targets=(
                    guidance_targets if check.role == "worker" else ()
                ),
                allow_common_plans=(
                    allow_common_plans if check.role == "worker" else False
                ),
                allowed_requirement_archives=archive_sources if check.role == 'worker' else (),
                allowed_ref_changes=check_refs,
                allowed_worktree_changes=check_worktrees,
                external_worktree=repo_path,
                base_branch=str(project["base_branch"]),
                connection=connection,
                resources=resources,
                owned_rebase=rebase_scope_normalized,
            )
        except AgentScopeError as error:
            if check.checkpoint_result:
                raise AgentScopeError(
                    f"중단된 Manager Git 작업 후 Agent scope 검증 실패: {error}"
                ) from error
            raise
    if recovered_baseline is not None:
        recovered_values = {"agent_snapshot": recovered_baseline.serialize()}
        if recovered_result_baseline is not None:
            recovered_values["agent_result_snapshot"] = (
                recovered_result_baseline.serialize()
            )
        if recovered_manager_checkpoint is not None:
            recovered_values["agent_manager_snapshot"] = (
                recovered_manager_checkpoint.serialize()
            )
        effects.update_project(connection, repo, **recovered_values)
    if manager_runtime is not None and not rebase_scope_normalized:
        original = GitSnapshot.deserialize(str(project['agent_snapshot']))
        effective = decision.reconciled_before
        if effective is not None and effective != original:
            checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
            plan_id = effects._plan_id_from_path(selected_plan_path) or ''
            audit = checkpoints.get(repo, plan_id)
            saved = dict(audit.snapshot or {})
            baseline = {'role': role_name, 'original': hashlib.sha256(original.serialize().encode()).hexdigest(),
                        'snapshot': effective.serialize()}
            previous = saved.get('manager_metadata_agent_baseline')
            if previous != baseline:
                if previous is not None:
                    saved['manager_metadata_agent_baseline_history'] = [
                        *saved.get('manager_metadata_agent_baseline_history', ()), previous]
                checkpoints.record(repo, plan_id, expected_generation=audit.generation,
                    operation=audit.operation, expected_head=audit.expected_head,
                    target_base_oid=audit.target_base_oid, observed_remote_oid=audit.observed_remote_oid,
                    snapshot={**saved, 'manager_metadata_agent_baseline': baseline})
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
    """Advance only proven metadata, retaining the original Agent snapshot."""

    project = effects.repository(connection).scope_owner(repo)
    if project is None or not project["agent_snapshot"]:
        raise AgentScopeError("갱신할 저장된 Agent Git snapshot이 없습니다.")
    stored = GitSnapshot.deserialize(str(project["agent_snapshot"]))
    worktree = Path(str(project["worktree"] or ""))
    if not worktree.is_dir():
        raise AgentScopeError(
            f"저장된 Agent worktree를 확인할 수 없습니다: {worktree}"
        )

    current = effects.capture_git_snapshot(worktree)
    resources = effects._agent_git_resources(connection, Path(repo), worktree, stored,
                                    str(project['agent_role'] or ''),
                                    selected_plan_path=str(project['plan_path'] or ''))
    plan_id = effects._plan_id_from_path(str(project['plan_path'] or ''))
    state_dir = state_dir or (effects._owned_rebase_state_dir(repo, plan_id, worktree) if plan_id else None)
    if state_dir is None:
        raise AgentScopeError('Manager 기준선의 상태 디렉터리를 확인할 수 없습니다.')
    effective = effects._reconciled_manager_scope_metadata(
        connection, Path(repo), state_dir, repo, plan_id, validated, current, resources)
    try:
        effects.validate_agent_scope(
            "reviewer",
            worktree,
            effective,
            current,
            external_worktree=Path(repo),
            base_branch=str(project["base_branch"]),
            resources=resources,
        )
    except AgentScopeError as error:
        raise AgentScopeError(
            f"Manager Git 작업 후 Agent scope 갱신 실패: {error}"
        ) from error
    refreshed = effects._reconciled_manager_scope_metadata(
        connection, Path(repo), state_dir, repo, plan_id, stored, current, resources)
    effects.update_project(
        connection,
        repo,
        agent_manager_snapshot=ManagerGitCheckpoint(current, frozenset()).serialize(),
    )
    return refreshed


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
    saved_plan = None,
    require_safe_result: bool = False,
    changed_candidate: bool = False,
    effects: ScopeRecoveryEffects,
) -> GitSnapshot | None:
    """Revalidate preserved Agent output before a BLOCKED plan can resume."""

    project = effects.repository(connection).scope_owner(repo)
    if saved_plan is not None:
        project = saved_plan
    if project is None:
        raise AgentScopeError("BLOCKED Agent 범위를 확인할 프로젝트가 없습니다.")
    serialized = project["agent_snapshot"]
    if not serialized:
        return effects.capture_git_snapshot(worktree) if changed_candidate else None
    role_name = str(project["agent_role"] or "")
    if role_name not in {"planner", "worker", "reviewer"}:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent 역할이 유효하지 않습니다: {role_name or '없음'}"
        )
    try:
        before = GitSnapshot.deserialize(str(serialized))
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise AgentScopeError(
            f"저장된 BLOCKED Agent Git snapshot이 유효하지 않습니다: {error}"
        ) from error
    current = effects.capture_git_snapshot(worktree)
    start = before
    def comparison_view(value):
        return ignored_comparison_view(worktree, start, value, current, effects=effects)
    before = comparison_view(before)
    plan_id = effects._plan_id_from_path(selected_plan_path)
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id)
    if blocked_plan_reconciliation.remote_adoption_is_current(checkpoint):
        accepted_rebase = False
    before, project = _remote_adoption_agent_baseline(
        connection, repo, plan_id, worktree, project, before, current, checkpoint,
        saved_plan=saved_plan, accepted_rebase=accepted_rebase, effects=effects)
    scope_result = (GitSnapshot.deserialize(str(project['agent_result_snapshot']))
                    if project['agent_result_snapshot'] else None)
    if scope_result is not None:
        scope_result = comparison_view(scope_result)
    scope_state_dir = effects._owned_rebase_state_dir(repo, plan_id, worktree) if plan_id else None
    def reconcile_metadata(value, observed=current):
        if scope_state_dir is None:
            return value
        return effects._reconciled_manager_scope_metadata(
            connection, repo_path, scope_state_dir, repo, plan_id, value, observed,
            effects._agent_git_resources(connection, repo_path, worktree, value, role_name,
                                 selected_plan_path=selected_plan_path))
    observed_refs = blocked_plan_reconciliation.validated_scope_refs(
        connection, repo_path, repo, base, plan_id, before, scope_result, current,
        git=effects.git, github_repository=effects.github_repository,
        owned_remote_oid=checkpoint_ownership.owned_remote_oid)
    # A remote observation proves its value, not who moved the local tracking
    # ref. Base fetch allowances come from the operation-specific evidence.
    observed_refs -= frozenset({f'refs/remotes/origin/{base}'})
    if accepted_rebase:
        if role_name not in {'worker', 'reviewer'} or project['agent_recovery']:
            raise AgentScopeError('현재 rebase와 별도 Agent 복구 범위를 분리할 수 없습니다.')
        plan_id = effects._plan_id_from_path(selected_plan_path)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id)
        comparison = managed_rebase_adoption.agent_comparison_baseline(
            before, checkpoint.snapshot or {}, role_name)
        comparison = agent_scope_decision.manager_comparison_baseline(
            before, comparison, checkpoint.snapshot or {}, role_name)
        result = (GitSnapshot.deserialize(str(project['agent_result_snapshot']))
                  if project['agent_result_snapshot'] else None)
        guidance = ()
        common = effects._published_plan_allows_common_plans(repo_path, saved_plan)
        archive_sources = effects.requirement_archive_candidates(
            connection, repo_path, repo,
            str(saved_plan['current_revision_sha'] or saved_plan['plan_sha']), plan_id)
        scope_options = dict(
            selected_plan_path=selected_plan_path if role_name == 'worker' else None,
            allowed_guidance_targets=guidance, allow_common_plans=common,
            allowed_requirement_archives=archive_sources,
            external_worktree=repo_path, base_branch=base, owned_rebase=True)
        # Check actual Agent output before substituting the accepted selected
        # rebase state. Shared and external mutations are never normalized.
        if result is not None:
            effects.validate_agent_scope(role_name, worktree, comparison_view(comparison), comparison_view(result),
                                 **scope_options, connection=connection)
        comparison = reconcile_metadata(comparison)
        normalized = managed_rebase_adoption.selected_baseline(
            comparison, current, str(saved_plan['branch']), str(worktree.resolve()),
            publication=checkpoint)
        published_refs: frozenset[str] = frozenset()
        remote_ref = f"refs/remotes/origin/{saved_plan['branch']}"
        if current.refs.get(remote_ref) != normalized.refs.get(remote_ref):
            publication = blocked_checkpoint_state.scope_request_row(connection, repo, plan_id)
            if publication is not None:
                request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(repo, plan_id)
                if effects._owned_checkpoint_remote_oid(connection, repo_path, request, saved_plan, current) is not None:
                    published_refs = frozenset({remote_ref})
        effects.validate_agent_scope(
            role_name, worktree, normalized, current,
            allowed_ref_changes=effects._proven_manager_base_refs(
                connection, repo_path, repo, plan_id, before, current) | published_refs | observed_refs,
            allowed_worktree_changes=effects._proven_merged_cleanup_paths(connection, repo, before, current),
            **scope_options, connection=connection)
        return current
    before = reconcile_metadata(before)
    scope_before = before
    allowed_ref_changes: frozenset[str] = observed_refs
    allowed_worktree_changes: frozenset[str] = frozenset()
    allowed_worktree_changes |= effects._proven_merged_cleanup_paths(
        connection, repo, before, current,
    )
    plan_id = effects._plan_id_from_path(selected_plan_path)
    has_execplans = effects.repository(connection).scope_catalog_available()
    guidance_plan = effects.repository(connection).scope_owner(repo, plan_id) if plan_id and has_execplans else None
    archive_sources = effects.requirement_archive_candidates(
        connection, repo_path, repo,
        str(guidance_plan['current_revision_sha'] or guidance_plan['plan_sha']), plan_id
    ) if role_name == 'worker' and guidance_plan is not None else ()
    if plan_id is not None:
        allowed_ref_changes |= effects._proven_manager_base_refs(
            connection, repo_path, repo, plan_id, before, current,
        )
    if guidance_plan is not None and guidance_plan["blocked_reason"] == GUIDANCE_BLOCK_REASON:
        checkpoint_row = blocked_checkpoint_state.scope_request_row(connection, repo, plan_id)
        if checkpoint_row is not None:
            request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
                repo, plan_id,
            )
            if effects._owned_checkpoint_remote_oid(
                connection, repo_path, request, guidance_plan, current,
            ) is not None:
                allowed_ref_changes |= frozenset((
                    f"refs/remotes/origin/{guidance_plan['branch']}",
                ))
    if project["agent_manager_snapshot"]:
        try:
            manager_checkpoint = ManagerGitCheckpoint.deserialize(
                str(project["agent_manager_snapshot"])
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Manager Git checkpoint가 유효하지 않습니다: {error}"
            ) from error
        # A planned cleanup is not proof it ran. The exact completed removal
        # has already been checked by _proven_merged_cleanup_paths.
    result_serialized = project["agent_result_snapshot"]
    post_agent_resources = None
    if result_serialized:
        try:
            blocked_result = GitSnapshot.deserialize(str(result_serialized))
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise AgentScopeError(
                f"저장된 BLOCKED Agent 결과 snapshot이 유효하지 않습니다: {error}"
            ) from error

        blocked_result = reconcile_metadata(comparison_view(blocked_result))
        # Validate saved Agent output before treating later selected-worktree
        # edits as user input. Missing legacy isolation never weakens its
        # shared-resource comparison.
        resources = effects._agent_git_resources(
            connection, repo_path, worktree, before, role_name,
            selected_plan_path=selected_plan_path)
        owner = saved_plan if saved_plan is not None else guidance_plan
        user_input_cause = (owner is not None and bool(owner['blocked_reason'])
                            and owner['blocked_reason'] not in SCOPE_BASELINE_REASONS
                            and owner['blocked_reason'] != BlockReasonCode.LEGACY_UNSCOPED_BLOCK.value)
        if (role_name in {'worker', 'reviewer'} and before.scope_id and before.scope_start_digest
                and user_input_cause and current.branch is not None
                and resources is not None and not current.operations
                and not project['agent_recovery'] and before.branch == current.branch
                and current.branch == blocked_result.branch):
            result_safe = True
            try:
                effects.validate_agent_scope(
                    role_name, worktree, before, blocked_result,
                    selected_plan_path=selected_plan_path if role_name == 'worker' else None,
                    allow_common_plans=effects._published_plan_allows_common_plans(repo_path, guidance_plan),
                    allowed_requirement_archives=archive_sources,
                    external_worktree=repo_path, base_branch=base, resources=resources,
                    connection=connection)
            except AgentScopeError:
                if require_safe_result:
                    raise
                # Existing scope correction may still isolate a diagnosed
                # Agent commit. Never normalize that result as user input;
                # adoption separately requires the actual result to be safe.
                result_safe = False
            if result_safe:
                relation = (0 if blocked_result.head == current.head else
                            effects.git(worktree, 'merge-base', '--is-ancestor',
                                        blocked_result.head, current.head, check=False).returncode)
                if relation == 0:
                    if any(_non_default_index_flags(entries) for entries in current.index.values()):
                        raise AgentScopeError('사용자 입력의 index flags가 안전하지 않습니다.')
                    normalized = managed_rebase_adoption.selected_baseline(
                        blocked_result, current, current.branch, str(worktree.resolve()))
                    effects.validate_agent_scope(
                        'reviewer', worktree, normalized, current,
                        allowed_ref_changes=allowed_ref_changes,
                        allowed_worktree_changes=allowed_worktree_changes,
                        external_worktree=repo_path, base_branch=base,
                        resources=resources, connection=connection)
                    return current
                if relation not in {0, 1}:
                    raise AgentScopeError('사용자 입력의 commit 관계를 확인할 수 없습니다.')
        if result_serialized and not changed_candidate:
            original = GitSnapshot.deserialize(str(serialized))
            comparison = agent_scope_decision.manager_comparison_baseline(
                original, original, checkpoint.snapshot or {}, role_name)
            effects.validate_agent_scope(
                role_name, worktree, comparison_view(comparison), scope_result,
                selected_plan_path=selected_plan_path if role_name == 'worker' else None,
                allow_common_plans=effects._published_plan_allows_common_plans(repo_path, guidance_plan),
                allowed_requirement_archives=archive_sources,
                external_worktree=repo_path, base_branch=base, connection=connection)

            # Validate historical Agent output before accepting current state
            # as post-BLOCKED user input. Isolation evidence is not invented
            # when a legacy invocation lacks its ledger.
            owner = saved_plan if saved_plan is not None else guidance_plan
            if (role_name in {'worker', 'reviewer'} and owner is not None
                    and 'workspace_owned' in owner.keys() and int(owner['workspace_owned']) == 1
                    and owner['branch'] == current.branch and owner['worktree'] == str(worktree)):
                post_agent_resources = resources

        if (
            before.shared_metadata_complete
            and blocked_result.shared_metadata_complete
            and current.shared_metadata_complete
        ):
            scope_before = _blocked_agent_local_baseline(
                before,
                blocked_result,
                current,
                worktree,
                allowed_dirty_paths=frozenset((selected_plan_path,)),
                accept_current_input=True,
            )
            (
                scope_before,
                commit_ref_changes,
                commit_worktree_changes,
            ) = _diagnosed_blocked_commit_allowances(
                before,
                blocked_result,
                current,
                worktree,
                scope_before)
            allowed_ref_changes |= commit_ref_changes
            allowed_worktree_changes |= commit_worktree_changes
        elif (before.shared_metadata_complete and current.shared_metadata_complete):
            scope_before = _blocked_agent_local_baseline(
                before, before, current, worktree,
                allowed_dirty_paths=frozenset((selected_plan_path,)),
                accept_current_input=True,
            )

    effects.validate_agent_scope(
        role_name,
        worktree,
        scope_before,
        current,
        selected_plan_path=(
            selected_plan_path if role_name == "worker" else None
        ),
        allowed_ref_changes=allowed_ref_changes,
        allowed_worktree_changes=allowed_worktree_changes,
        allowed_requirement_archives=archive_sources,
        external_worktree=repo_path,
        base_branch=base,
        connection=connection,
        resources=post_agent_resources,
    )
    return current
