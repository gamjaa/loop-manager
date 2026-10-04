"""recovery blocked plan service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.git import rebase_block_reconciliation
from loop_manager_internal.git.managed_rebase_publication import RebaseObservationRetry
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.recovery import scope_reconciliation
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.runtime.contracts import REBASE_IN_PROGRESS_ERROR
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class RequeueBlockedExecplanEffects:
    """Named effects for _requeue_blocked_execplan; no entry-point/module access."""
    _clear_active_values: Callable[..., object]


def _requeue_blocked_execplan(
    effects: RequeueBlockedExecplanEffects, connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
    *, publication_remote_oid: str | None = None,
) -> None:
    plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is not None and int(plan["pr_conflict_recovery"]):
        with connection:
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                plan_id,
                plan_values={
                    "status": "IMPLEMENTING",
                    "blocked_fingerprint": None,
                    "blocked_worktree_fingerprint": None,
                    "blocked_review_fingerprint": None,
                    "retry_count": 0,
                    "blocked_reason": None,
                    "guidance_plan_hash": None,
                    "validated_base_sha": None,
                    "verify_result": None,
                    "review_result": None,
                    "last_error": REBASE_IN_PROGRESS_ERROR,
                    "verify_failure_history": "{}",
                    "review_failure_history": "{}",
                    "effective_worker_level": plan["initial_worker_level"],
                    "review_level_history": "{}",
                    "worker_session_transition": None,
                    "worker_session_binding": (
                        plan["worker_session_binding"]
                        if plan["worker_session_id"] is not None else None
                    ),
                },
                project_values={
                    "phase": "WORKING",
                    "validated_base_sha": None,
                    "verify_result": None,
                    "review_result": None,
                    "last_error": REBASE_IN_PROGRESS_ERROR,
                    "agent_role": None,
                    "agent_snapshot": None,
                    "agent_input_fingerprint": None,
                    "agent_baseline": None,
                    "agent_recovery": None,
                    "agent_input_plan": None,
                    "agent_result_plan": None,
                    "agent_result_snapshot": None,
                    "agent_manager_snapshot": None,
                    "worker_session_id": plan["worker_session_id"],
                    "worker_session_transition": None,
                    "worker_session_binding": (
                        plan["worker_session_binding"]
                        if plan["worker_session_id"] is not None else None
                    ),
                },
                enqueue=False,
            )
            scoped_blocking.clear_plan_block(connection, repo, plan_id)
        return
    with connection:
        runtime_state.RuntimeStateRepository(connection).transition(
            repo,
            plan_id,
            plan_values={
                "status": "READY",
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "blocked_review_fingerprint": None,
                "retry_count": 0,
                "blocked_reason": None,
                "guidance_plan_hash": None,
                "last_error": None,
                "worker_session_id": None,
                "worker_session_binding": None,
                "worker_session_transition": None,
                "reviewer_session_id": None,
                "worker_result": None,
                "verify_result": None,
                "review_result": None,
                "validated_base_sha": None,
                "publication_remote_oid": publication_remote_oid,
                "pr_url": None,
                "pr_head_oid": None,
                "verify_failure_history": "{}",
                "review_failure_history": "{}",
                "effective_worker_level": plan["initial_worker_level"],
                "review_level_history": "{}",
            },
            project_values=effects._clear_active_values(),
            enqueue=False,
        )
        scoped_blocking.clear_plan_block(connection, repo, plan_id)


def _record_blocked_resume_head(
    connection: sqlite3.Connection, repo: str, plan_id: str,
    owner_oid: str, before_head: str, target_base_oid: str,
    after_head: str | None, before_snapshot: str | None = None,
) -> None:
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoint.get(repo, plan_id)
    active_rebase = current.operation == 'rebase'
    intent = current.rebase_publication_intent()
    result = (current.snapshot or {}).get('rebase_publication_result') or {}
    published_oid = (intent['head'] if intent is not None
                     and result.get('intent_id') == intent['id']
                     and result.get('head') == after_head == intent['head']
                     else owner_oid)
    marker = {
        'owner_oid': owner_oid, 'before_head': before_head,
        'target_base_oid': target_base_oid, 'after_head': after_head,
        'before_snapshot': (before_snapshot or
            (current.snapshot or {}).get('blocked_resume', {}).get('before_snapshot')),
    }
    checkpoint.record(
        repo, plan_id, expected_generation=current.generation,
        operation='rebase' if active_rebase else None,
        expected_head=current.expected_head if active_rebase else after_head or before_head,
        target_base_oid=target_base_oid,
        observed_remote_oid=published_oid,
        snapshot={**(current.snapshot or {}), 'blocked_resume': marker},
    )


@dataclass(frozen=True)
class BlockedResumeWorktreeMatchesEffects:
    """Named effects for _blocked_resume_worktree_matches; no entry-point/module access."""
    _restore_agent_staged_entries: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]


def _blocked_resume_worktree_matches(
    effects: BlockedResumeWorktreeMatchesEffects, worktree: Path, marker: dict, current: GitSnapshot, *, restore_staged: bool = True,
) -> bool:
    """Match preserved files and staging, allowing only autostash unstaging."""
    serialized = marker.get('before_snapshot')
    if not serialized:
        return False
    before = GitSnapshot.deserialize(serialized)
    if before.head != marker.get('before_head') or before.branch != current.branch:
        return False
    if before.config != current.config or not current.shared_metadata_complete:
        return False
    # Rebase may change blobs, but cannot legitimately introduce index flags.
    for path, entries in current.index.items():
        tags = tuple(entry.split()[0] for entry in entries)
        old_tags = tuple(entry.split()[0] for entry in before.index.get(path, ()))
        if tags != (old_tags if path in before.index else ('H',)):
            return False
    if set(before.dirty) != set(current.dirty):
        return False
    unstaged_by_rebase = False
    for path, (kinds, signature) in before.dirty.items():
        now_kinds, now_signature = current.dirty[path]
        if signature != now_signature:
            return False
        if kinds == now_kinds:
            if 'staged' in kinds and before.index.get(path) != current.index.get(path):
                return False
        elif (not marker.get('after_head') and 'staged' in kinds
              and now_kinds == ('unstaged',)):
            unstaged_by_rebase = True
        else:
            return False
    if unstaged_by_rebase:
        # Only a clean index can be attributed to rebase --autostash.
        if effects.git(worktree, 'diff', '--cached', '--quiet', check=False).returncode:
            return False
        if not restore_staged:
            return True
        effects._restore_agent_staged_entries(worktree, before)
        restored = effects.capture_git_snapshot(worktree)
        return restored.dirty == before.dirty and all(
            restored.index.get(path) == before.index.get(path)
            for path, (kinds, _) in before.dirty.items() if 'staged' in kinds
        )
    return True


@dataclass(frozen=True)
class ResumeInterruptedBlockedRebaseEffects:
    """Named effects for _resume_interrupted_blocked_rebase; no entry-point/module access."""
    _scope_reconciliation_effects_resume_interrupted_blocked_rebase: Callable[..., object]


def _resume_interrupted_blocked_rebase(
    effects: ResumeInterruptedBlockedRebaseEffects, connection: sqlite3.Connection, repo_path: Path,
    repo: str, base: str, plan: sqlite3.Row, worktree: Path, branch: str,
) -> bool | None:
    return scope_reconciliation._resume_interrupted_blocked_rebase(
        effects._scope_reconciliation_effects_resume_interrupted_blocked_rebase(),
        connection, repo_path, repo, base, plan, worktree, branch,
    )


@dataclass(frozen=True)
class ResumeBlockedExecplanIfInputsChangedEffects:
    """Named effects for resume_blocked_execplan_if_inputs_changed; no entry-point/module access."""
    _blocked_execplan_metadata_error: Callable[..., object]
    _blocked_execplan_revision_metadata_error: Callable[..., object]
    _blocked_resume_worktree_matches: Callable[..., object]
    _complete_direct_rebase_checkpoint: Callable[..., object]
    _direct_rebase_history_proven: Callable[..., object]
    _journaled_direct_rebase: Callable[..., object]
    _owned_rebase_journal_state: Callable[..., object]
    _plan_block_selected_for_release: Callable[..., object]
    _reconcile_rebase_evidence_block: Callable[..., object]
    _reconcile_scope_baseline_plan_block: Callable[..., object]
    _record_blocked_resume_head: Callable[..., object]
    _requeue_blocked_execplan: Callable[..., object]
    _restore_agent_staged_entries: Callable[..., object]
    _resume_interrupted_blocked_rebase: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _revision_review_issue_fingerprint: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    clear_agent_scope: Callable[..., object]
    git: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]
    make_worktree: Callable[..., object]
    preserve_blocked_agent_changes: Callable[..., object]
    project_execplan: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def resume_blocked_execplan_if_inputs_changed(
    effects: ResumeBlockedExecplanIfInputsChangedEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
    *, plan_id: str | None = None,
) -> bool:
    scope_blocks = [scoped_blocking.plan_block(connection, repo, block['plan_id'])
                    for block in scoped_blocking.plan_blocks(connection, repo)
                    if scoped_blocking.is_scope_baseline_block(block)
                    and (plan_id is None or block['plan_id'] == plan_id)]
    for scope_block in scope_blocks:
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, scope_block['plan_id'])
        recovery = recovery_state.RecoveryRepository(connection).row(repo, scope_block['plan_id'])
        if rebase_block_reconciliation.block_matches(scope_block, recovery, checkpoint):
            return effects._reconcile_rebase_evidence_block(
                connection, repo_path, state_dir, repo, scope_block, plan_block=True)
        if effects._reconcile_scope_baseline_plan_block(connection, repo_path, state_dir, repo, scope_block):
            return True
    # Select rebase causes without hydrating a projection or requiring a past
    # input fingerprint. Current Git state, rather than input change, decides.
    rebase_blocks = [scoped_blocking.plan_block(connection, repo, str(block['plan_id']))
                     for block in scoped_blocking.plan_blocks(connection, repo)
                     if rebase_block_reconciliation.could_be_rebase_evidence_block(block)
                     and (plan_id is None or block['plan_id'] == plan_id)]
    if rebase_blocks:
        runtime = runtime_state.RuntimeStateRepository(connection)
        if not runtime.recovery_slot_available(repo) or runtime.active_row(repo) is not None:
            return False
        projected = effects.project_execplan(connection, repo)
        selected = next((block for block in rebase_blocks
                         if projected is not None and block['plan_id'] == projected['plan_id']), None)
        if selected is None and projected is None and len(rebase_blocks) == 1:
            selected = rebase_blocks[0]
        if selected is not None:
            return effects._reconcile_rebase_evidence_block(
                connection, repo_path, state_dir, repo, selected, plan_block=True)
        if projected is None:
            return False
    if plan_id is not None:
        runtime = runtime_state.RuntimeStateRepository(connection)
        if (not runtime.recovery_slot_available(repo) or runtime.active_row(repo) is not None
                or scoped_blocking.project_block(connection, repo) is not None):
            return False
    plan = effects._plan_block_selected_for_release(
        connection,
        repo,
        require_fingerprint=True,
        plan_id=plan_id,
    )
    if plan is None:
        return False
    if plan_id is not None:
        effects.hydrate_execplan_project(connection, repo, plan)
    stored = str(plan["blocked_fingerprint"])
    worktree = Path(str(plan["worktree"]))
    branch = str(plan["branch"] or "")
    revision = f"refs/remotes/origin/{base}"
    execplan_input_changed = (
        effects._revision_execplan_input_fingerprint(repo_path, revision, plan) != stored
    )
    review_input_changed = False
    if plan["blocked_reason"] != BlockReasonCode.USER_DECISION_REQUIRED.value:
        current_review_fingerprint = effects._revision_review_issue_fingerprint(
            repo_path, revision, plan["review_result"]
        )
        stored_review_fingerprint = plan["blocked_review_fingerprint"]
        if current_review_fingerprint is None:
            review_input_changed = False
        elif stored_review_fingerprint:
            review_input_changed = current_review_fingerprint != str(
                stored_review_fingerprint
            )
        else:
            blocked_revision = str(
                plan["active_sha"]
                or plan["current_revision_sha"]
                or plan["plan_sha"]
                or ""
            )
            blocked_review_fingerprint = (
                effects._revision_review_issue_fingerprint(
                    repo_path, blocked_revision, plan["review_result"]
                )
                if blocked_revision
                else None
            )
            review_input_changed = (
                blocked_review_fingerprint is not None
                and current_review_fingerprint != blocked_review_fingerprint
            )
    remote_input_changed = execplan_input_changed or review_input_changed
    if int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        if not remote_input_changed:
            return False
        try:
            effects.validate_execplan_workspace_identity(
                state_dir,
                repo,
                str(plan["plan_id"]),
                branch,
                worktree,
            )
        except WorkspaceOwnershipError as error:
            message = f"BLOCKED workspace 충돌 해제 검증 실패:\n{error}"
            effects._update_blocked_error(connection, repo, str(plan["plan_id"]), message)
            return False

        branch_state = effects.git(
            repo_path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        conflicts = []
        if worktree.exists() or worktree.is_symlink():
            conflicts.append(f"path {worktree}")
        if branch_state.returncode == 0:
            conflicts.append(f"branch {branch}")
        elif branch_state.returncode != 1:
            conflicts.append(f"branch 상태 확인 실패 {branch}")
        if conflicts:
            message = (
                "BLOCKED workspace 충돌이 해소되지 않았습니다: "
                + ", ".join(conflicts)
            )
            effects._update_blocked_error(connection, repo, str(plan["plan_id"]), message)
            return False

        metadata_error = effects._blocked_execplan_revision_metadata_error(
            repo_path,
            revision,
            plan,
        )
        if metadata_error:
            effects._update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                metadata_error,
            )
            return False
        effects._requeue_blocked_execplan(connection, repo, str(plan["plan_id"]))
        return True

    try:
        effects.validate_execplan_workspace_identity(
            state_dir,
            repo,
            str(plan["plan_id"]),
            branch,
            worktree,
        )
        effects.make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
        )
    except (RuntimeError, WorkspaceOwnershipError) as error:
        message = f"BLOCKED worktree 복구 실패:\n{error}"
        effects._update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    interrupted = effects._resume_interrupted_blocked_rebase(
        connection, repo_path, repo, base, plan, worktree, branch,
    )
    if interrupted is not None:
        return interrupted
    try:
        effects.validate_blocked_agent_scope(
            connection,
            repo_path,
            repo,
            base,
            worktree,
            str(plan["path"]),
        )
    except (AgentScopeError, RuntimeError) as error:
        message = f"BLOCKED Agent 범위 재검증 실패:\n{error}"
        effects._update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    current_worktree_fingerprint = (
        effects._worktree_execplan_input_fingerprint(worktree, plan)
        if worktree.is_dir()
        else None
    )
    blocked_worktree_fingerprint = plan["blocked_worktree_fingerprint"]
    worktree_changed = bool(
        current_worktree_fingerprint != stored
        and current_worktree_fingerprint != blocked_worktree_fingerprint
    )
    if not worktree_changed and not remote_input_changed:
        return False
    owned_oid = None
    checkpoint_row = connection.execute(
        "SELECT phase FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
        (repo, plan['plan_id']),
    ).fetchone()
    if checkpoint_row is not None and checkpoint_row['phase'] == 'published':
        remote_oid = effects.remote_branch_oid(repo_path, branch)
        owned_oid = checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, str(plan['plan_id']), branch,
            worktree, remote_oid, effects.git,
            before_head=effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip(),
            recorded_oid=str(plan['publication_remote_oid'] or '') or None,
            strict_observation=True,
        ) if not plan['pr_url'] else None
        if owned_oid is None:
            effects._update_blocked_error(
                connection, repo, str(plan['plan_id']),
                '게시 checkpoint의 원격 OID와 계획 소유권을 증명할 수 없습니다.',
            )
            return False
    try:
        preserved_before = effects.preserve_blocked_agent_changes(
            connection,
            repo,
            worktree,
            str(plan["plan_id"]),
            preserved_input_path=(str(plan["path"]) if worktree_changed else None),
        )
    except (AgentScopeError, RuntimeError) as error:
        message = f"BLOCKED Agent 변경 보존 실패:\n{error}"
        effects._update_blocked_error(connection, repo, str(plan["plan_id"]), message)
        return False
    target_base_oid = effects.git(repo_path, 'rev-parse', revision).stdout.strip()
    if owned_oid is not None:
        before_head = effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        ancestry = effects.git(repo_path, 'merge-base', '--is-ancestor', owned_oid,
                               before_head, check=False)
        if ancestry.returncode not in (0, 1):
            raise recovery_runtime.RecoveryFailure(
                recovery_runtime.RecoveryReason.PUBLICATION_RETRY,
                'checkpoint ancestry observation failed',
            )
        if ancestry.returncode == 1:
            effects._update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '게시 checkpoint 이후 로컬 HEAD 계보가 다릅니다.')
            return False
        effects._record_blocked_resume_head(
            connection, repo, str(plan['plan_id']), owned_oid,
            before_head, target_base_oid, None,
            effects.capture_git_snapshot(worktree).serialize(),
        )
    base_ancestry = effects.git(
        worktree,
        "merge-base",
        "--is-ancestor",
        revision,
        "HEAD",
        check=False,
    )
    if base_ancestry.returncode not in (0, 1):
        raise recovery_runtime.RecoveryFailure(
            recovery_runtime.RecoveryReason.PUBLICATION_RETRY,
            "BLOCKED 해제 전 원격 base ancestry 확인 실패",
        )
    if base_ancestry.returncode == 1:
        rebased = effects._journaled_direct_rebase(
            connection, repo_path, repo, str(plan['plan_id']), worktree,
            branch, target_base_oid, autostash=True,
            agent_session_id=plan['worker_session_id'],
        )
        if rebased.returncode:
            error = f"BLOCKED 해제 전 rebase 실패:\n{rebased.stdout[-8000:]}"
            effects._update_blocked_error(connection, repo, str(plan["plan_id"]), error)
            return False
    if worktree.is_dir():
        metadata_error = effects._blocked_execplan_metadata_error(repo_path, worktree, plan)
        if metadata_error:
            effects._update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                metadata_error,
            )
            return False

    if owned_oid is not None:
        after_head = effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        if not effects._direct_rebase_history_proven(
            connection, repo_path, repo, str(plan['plan_id']), worktree,
            branch, before_head, after_head, target_base_oid,
            plan['worker_session_id'],
        ):
            effects._update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '차단 해제 rebase의 commit 계보를 증명할 수 없습니다.')
            return False
        pending = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, str(plan['plan_id']),
        )
        if not effects._blocked_resume_worktree_matches(
            worktree, pending.snapshot['blocked_resume'], effects.capture_git_snapshot(worktree),
            restore_staged=False,
        ):
            effects._update_blocked_error(connection, repo, str(plan['plan_id']),
                                  '차단 해제 rebase 이후 보존한 작업 상태가 다릅니다.')
            return False
        # Adopt the clean rebase result before restoring the original staging.
        # Restored work remains unverified and belongs to the next Worker turn.
        preserved_observation = effects._owned_rebase_journal_state(worktree, branch)
        if base_ancestry.returncode == 1:
            try:
                effects._complete_direct_rebase_checkpoint(
                    connection, repo, str(plan['plan_id']), worktree,
                    target_base_oid, repo_path=repo_path, branch=branch,
                    agent_session_id=plan['worker_session_id'],
                    preserved_worktree=True,
                    preserved_marker=pending.snapshot['blocked_resume'],
                    preserved_observation=preserved_observation,
                )
            except recovery_runtime.RecoveryFailure as error:
                if isinstance(error, RebaseObservationRetry):
                    raise
                effects._update_blocked_error(connection, repo, str(plan['plan_id']),
                                      f'차단 해제 rebase 게시를 완료할 수 없습니다: {error}')
                return False
    elif base_ancestry.returncode == 1:
        effects._complete_direct_rebase_checkpoint(
            connection, repo, str(plan['plan_id']), worktree, target_base_oid,
            repo_path=repo_path, branch=branch,
            agent_session_id=plan['worker_session_id'],
        )

    if base_ancestry.returncode == 1 and preserved_before is not None:
        try:
            effects._restore_agent_staged_entries(worktree, preserved_before)
        except (AgentScopeError, RuntimeError) as error:
            restore_error = f"BLOCKED 해제 후 index 복원 실패:\n{error}"
            effects._update_blocked_error(
                connection,
                repo,
                str(plan["plan_id"]),
                restore_error,
            )
            return False

    if owned_oid is not None:
        effects._record_blocked_resume_head(
            connection, repo, str(plan['plan_id']), owned_oid,
            before_head, target_base_oid, after_head,
        )

    effects.clear_agent_scope(connection, repo)
    effects._requeue_blocked_execplan(connection, repo, str(plan["plan_id"]),
                              publication_remote_oid=runtime_state.RuntimeStateRepository(connection).get(
                                  repo, str(plan['plan_id'])).publication_remote_oid or owned_oid)
    return True
