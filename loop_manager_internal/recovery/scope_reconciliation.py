"""Independent plan and project safety gate reconciliation."""
from __future__ import annotations
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner

from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.git import rebase_block_reconciliation
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON
from loop_manager_internal.recovery.scoped_blocking import PR_CLOSED_BLOCK_REASON
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WORKSPACE_RESERVED
from loop_manager_internal.runtime.contracts import WORKSPACE_UNOWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.runtime_state import ACTIVE_STATUSES as ACTIVE_EXECPLAN_STATUSES
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class ResumeProjectBlockIfSafeEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    _agent_git_resources: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _clear_project_block_without_losing_planning_error: Callable[..., object]
    _reconcile_guidance_double_block: Callable[..., object]
    _reconcile_rebase_evidence_block: Callable[..., object]
    _reconciled_manager_scope_metadata: Callable[..., object]
    _requeue_blocked_execplan: Callable[..., object]
    _resume_owned_checkpoint_project_block: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    validate_planning_worktree_ownership: Callable[..., object]

@dataclass(frozen=True)
class ResumePlanBlocksEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    ROOT: Path
    load_managed_config: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _reconcile_published_blocked_checkpoint: Callable[..., object]
    _reconcile_rebase_evidence_block: Callable[..., object]
    _resume_interrupted_blocked_rebase: Callable[..., object]
    _resume_legacy_review_count_block: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    active_execplan: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]
    plan_blocked_execplans: Callable[..., object]
    project_execplan: Callable[..., object]
    resume_blocked_execplan_if_inputs_changed: Callable[..., object]
    resume_guidance_approval_if_decided: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    plan_recovery_effects: Callable[[], blocked_plan_reconciliation.PlanRecoveryEffects]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def resume_project_block_if_safe(effects: ResumeProjectBlockIfSafeEffects,
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
) -> bool:
    """Release only a project block whose original safety boundary is restored."""

    block = scoped_blocking.project_block(connection, repo)
    if block is None:
        return False
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, str(block['plan_id'] or ''))
    recovery = recovery_state.RecoveryRepository(connection).row(repo, str(block['plan_id'] or ''))
    if rebase_block_reconciliation.could_be_rebase_evidence_block(block, recovery, checkpoint):
        return effects._reconcile_rebase_evidence_block(
            connection, repo_path, state_dir, repo, block, plan_block=False,
        )
    if (block['plan_id'] and block['reason_code'] in rebase_block_reconciliation.REBASE_BLOCK_REASONS
            and checkpoint.operation == 'rebase'):
        # A pending rebase cannot borrow the generic branch-only release path
        # when its stored safety cause cannot be connected to this operation.
        return False
    if block["reason_code"] == BlockReasonCode.LEGACY_UNSCOPED_BLOCK.value:
        return False
    plan_id = str(block["plan_id"] or "")
    project = connection.execute(
        "SELECT * FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        return False
    planning_owns_slot = runtime_state.planning_owns_execution_slot(project)
    if not plan_id:
        if block['reason_code'] == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value:
            return False
        if scoped_blocking.is_scope_baseline_block(block) and project['agent_recovery']:
            return False
        try:
            worktree = Path(str(project['worktree'] or repo_path))
            checked = None
            if block["reason_code"] == BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value:
                worktree = Path(str(project["worktree"] or ""))
                fingerprint = str(project["planning_attempt_fingerprint"] or "")
                if not fingerprint:
                    return False
                effects.validate_planning_worktree_ownership(
                    connection,
                    repo_path,
                    state_dir,
                    repo,
                    fingerprint,
                    worktree,
                )
            elif project["agent_snapshot"]:
                before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
                after = checked = effects.capture_git_snapshot(worktree)
                resources = effects._agent_git_resources(connection, repo_path, worktree, before,
                                                str(project['agent_role'] or 'planner'))
                if scoped_blocking.is_scope_baseline_block(block) and project['agent_result_snapshot']:
                    result = GitSnapshot.deserialize(str(project['agent_result_snapshot']))
                    comparison = effects._reconciled_manager_scope_metadata(
                        connection, repo_path, state_dir, repo, None, before, after, resources)
                    result = effects._reconciled_manager_scope_metadata(
                        connection, repo_path, state_dir, repo, None, result, after, resources)
                    effects.validate_agent_scope(str(project['agent_role'] or 'planner'), worktree,
                        comparison, result, external_worktree=repo_path,
                        base_branch=str(project['base_branch']), resources=resources)
                before = effects._reconciled_manager_scope_metadata(
                    connection, repo_path, state_dir, repo, None, before, after, resources)
                effects.validate_agent_scope(
                    str(project["agent_role"] or "planner"),
                    worktree,
                    before,
                    after,
                    external_worktree=repo_path,
                    base_branch=str(project["base_branch"]),
                    connection=connection,
                )
            else:
                return False
        except (AgentScopeError, RuntimeError, WorkspaceOwnershipError, ValueError):
            return False
        with runtime_state._transaction(connection):
            if (not scoped_blocking.project_cause_matches(connection, block)
                    or dict(connection.execute('SELECT * FROM projects WHERE repo = ?',
                                               (repo,)).fetchone()) != dict(project)
                    or (checked is not None and effects.capture_git_snapshot(worktree) != checked)):
                return False
            effects._clear_project_block_without_losing_planning_error(connection, repo, project)
        return True
    plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    if plan is None:
        return False
    if planning_owns_slot and str(plan["status"]) in ACTIVE_EXECPLAN_STATUSES:
        return False
    if (
        planning_owns_slot
        and block['reason_code']
        == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value
    ):
        return False
    if not planning_owns_slot and effects._reconcile_guidance_double_block(
        connection, repo_path, state_dir, repo, block, project, plan,
    ):
        return True
    if block['reason_code'] == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value:
        return effects._resume_owned_checkpoint_project_block(
            connection, repo_path, state_dir, repo, block, project, plan,
        )
    if (
        block["reason_code"]
        == BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value
        and int(plan["workspace_owned"])
        in {WORKSPACE_UNOWNED, WORKSPACE_RESERVED}
    ):
        worktree = Path(str(plan["worktree"] or ""))
        branch = str(plan["branch"] or "")
        try:
            effects.validate_execplan_workspace_identity(
                state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree
            )
        except WorkspaceOwnershipError:
            return False
        branch_state = effects.git(
            repo_path,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        )
        if (
            worktree.exists()
            or worktree.is_symlink()
            or branch_state.returncode != 1
        ):
            return False
        status = str(plan["status"])
        with connection:
            effects._clear_project_block_without_losing_planning_error(connection, repo, project)
        if status in ACTIVE_EXECPLAN_STATUSES:
            effects._requeue_blocked_execplan(connection, repo, plan_id)
        else:
            if not planning_owns_slot:
                with connection:
                    effects._clear_active_in_current_transaction(connection, repo)
        return True
    if int(plan["workspace_owned"]) != WORKSPACE_OWNED:
        return False
    if scoped_blocking.is_scope_baseline_block(block) and (
            not plan['agent_snapshot'] or plan['agent_recovery']):
        return False
    worktree = Path(str(plan["worktree"] or ""))
    branch = str(plan["branch"] or "")
    try:
        checked = effects.capture_git_snapshot(worktree) if plan['agent_snapshot'] else None
        effects.validate_execplan_workspace_identity(
            state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree
        )
        effects.validate_managed_worktree(
            repo_path,
            worktree,
            branch,
        )
        if plan["agent_snapshot"]:
            effects.validate_blocked_agent_scope(
                connection, repo_path, repo, str(project['base_branch']),
                worktree, str(plan['path']), saved_plan=plan,
                require_safe_result=scoped_blocking.is_scope_baseline_block(block))
        if checked is not None and effects.capture_git_snapshot(worktree) != checked:
            return False
    except (AgentScopeError, RuntimeError, WorkspaceOwnershipError, ValueError):
        return False
    with runtime_state._transaction(connection):
        if (not scoped_blocking.project_cause_matches(connection, block)
                or dict(connection.execute('SELECT * FROM projects WHERE repo = ?',
                                           (repo,)).fetchone()) != dict(project)
                or dict(connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                           (repo, plan_id)).fetchone()) != dict(plan)
                or (checked is not None and effects.capture_git_snapshot(worktree) != checked)):
            return False
        effects._clear_project_block_without_losing_planning_error(connection, repo, project)
        if scoped_blocking.is_scope_baseline_block(block):
            current_checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
                repo, plan_id)
            if current_checkpoint != checkpoint:
                raise RuntimeError('project scope 차단 해제 중 checkpoint generation이 변경됐습니다.')
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(
                repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid,
                observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**(checkpoint.snapshot or {}), 'scope_block_release_audit': [
                    *(checkpoint.snapshot or {}).get('scope_block_release_audit', ()),
                    {'block': dict(block), 'candidate': checked.serialize() if checked else None}]},
            )
            recovery_state.RecoveryRepository(connection).resolve_rebase_block(repo, plan_id, block)
        if str(plan["status"]) in ACTIVE_EXECPLAN_STATUSES:
            effects.hydrate_execplan_project(connection, repo, plan)
        elif not planning_owns_slot:
            effects._clear_active_in_current_transaction(connection, repo)
    return True


def resume_plan_blocks(effects: ResumePlanBlocksEffects,
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    base: str,
    config_path: Path | None = None,
    verify_timeout_seconds: int | None = None,
    plan_id: str | None = None,
    force_timeout_recheck: bool = False,
    recovery_request_id: str | None = None,
) -> bool:
    """Reevaluate each plan block without letting one overwrite another."""

    if scoped_blocking.project_block(connection, repo) is not None:
        return False
    if not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo):
        return False
    if runtime_state.RuntimeStateRepository(connection).active_row(repo) is not None:
        return False
    held = scoped_blocking.catalog_held_plan_ids(connection, repo)
    for blocked in effects.plan_blocked_execplans(connection, repo):
        if plan_id is not None and str(blocked["plan_id"]) != plan_id:
            continue
        if str(blocked["plan_id"]) in held:
            continue
        current_block = scoped_blocking.plan_block(
            connection, repo, str(blocked["plan_id"]),
        )
        # An explicit local recheck may schedule the saved timeout validation
        # again. Other block causes still require their own user/input gate.
        current_reason = (
            str(current_block["reason_code"])
            if current_block is not None else str(blocked["blocked_reason"] or "")
        )
        local_verify_recheck = force_timeout_recheck and current_reason in {
            BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value,
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED.value,
        }
        if force_timeout_recheck and not local_verify_recheck:
            continue
        if scoped_blocking.is_scope_baseline_block(current_block):
            prior_projection = effects.project_execplan(connection, repo)
            if effects.resume_blocked_execplan_if_inputs_changed(
                    connection, repo_path, repo, base, state_dir, plan_id=str(blocked['plan_id'])):
                return True
            if scoped_blocking.project_block(connection, repo) is not None:
                return False
            projected = effects.project_execplan(connection, repo)
            if (projected is not None and projected['plan_id'] == blocked['plan_id']
                    and effects.active_execplan(connection, repo) is None):
                if prior_projection is None:
                    with connection:
                        effects._clear_active_in_current_transaction(connection, repo)
                elif prior_projection['plan_id'] != projected['plan_id']:
                    prior_plan = connection.execute(
                        'SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                        (repo, prior_projection['plan_id'])).fetchone()
                    effects.hydrate_execplan_project(connection, repo, prior_plan)
            continue
        if rebase_block_reconciliation.could_be_rebase_evidence_block(current_block):
            if effects._reconcile_rebase_evidence_block(
                connection, repo_path, state_dir, repo, current_block,
                plan_block=True,
            ):
                return True
            continue
        observed = connection.execute(
            'SELECT registered_origin FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
            (repo, blocked['plan_id'])).fetchone()
        if (observed is not None and observed['registered_origin']
                and blocked['blocked_reason'] == BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value
                and not effects.unmet_execplan_dependencies(connection, repo, str(blocked['plan_id']))
                and blocked_plan_reconciliation.slot_available(connection, repo, str(blocked['plan_id']))):
            effects.hydrate_execplan_project(connection, repo, blocked)
            if effects._resume_legacy_review_count_block(connection, repo_path, state_dir, repo, blocked):
                return True
            with connection:
                effects._clear_active_in_current_transaction(connection, repo)
        remote_result = blocked_plan_reconciliation.BlockedPlanReconciler(
            effects.plan_recovery_effects(), connection, repo_path, state_dir, repo, base,
            config_path, verify_timeout_seconds,
        ).reconcile(str(blocked['plan_id']), **({'local_verify_recheck': True} if local_verify_recheck else {}))
        if scoped_blocking.project_block(connection, repo) is not None:
            return False
        if remote_result == 'resumed':
            return True
        if remote_result in {'blocked', 'deferred'}:
            refresh = connection.execute(
                'SELECT stage FROM blocked_plan_reconciliations WHERE repo = ? AND plan_id = ?',
                (repo, blocked['plan_id']),
            ).fetchone()
            if (remote_result == 'blocked' and refresh is not None
                    and refresh['stage'] == 'complete'
                    and blocked['blocked_reason'] == GUIDANCE_BLOCK_REASON):
                refreshed = connection.execute(
                    'SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                    (repo, blocked['plan_id']),
                ).fetchone()
                effects.hydrate_execplan_project(connection, repo, refreshed)
                if effects.resume_guidance_approval_if_decided(
                        connection, repo_path, repo, base, state_dir,
                        plan_id=str(blocked['plan_id'])):
                    return True
            projected = effects.project_execplan(connection, repo)
            if (projected is not None and projected['plan_id'] == blocked['plan_id']
                    and effects.active_execplan(connection, repo) is None):
                with connection:
                    effects._clear_active_in_current_transaction(connection, repo)
            continue
        if blocked["blocked_reason"] == PR_CLOSED_BLOCK_REASON:
            continue
        # A push may have succeeded before its adopted OID reached SQLite.
        # Reconcile the saved release before comparing the old checkpoint ref
        # with the live remote (which may already contain the rebased HEAD).
        interrupted = effects._resume_interrupted_blocked_rebase(
            connection, repo_path, repo, base, blocked,
            Path(str(blocked["worktree"] or "")), str(blocked["branch"] or ""),
        )
        if interrupted is not None:
            if interrupted:
                return True
            continue
        checkpoint_row = connection.execute(
            "SELECT * FROM blocked_checkpoints "
            "WHERE repo = ? AND plan_id = ?",
            (repo, blocked["plan_id"]),
        ).fetchone()
        if (
            checkpoint_row is not None
            and checkpoint_row["phase"] == "published"
            and checkpoint_row["remote_oid"]
        ):
            checkpoint_repository = (
                blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            )
            checkpoint_request = checkpoint_repository.get(
                repo, str(blocked["plan_id"])
            )
            remote_result = effects._reconcile_published_blocked_checkpoint(
                connection, repo_path, state_dir, repo, blocked,
                checkpoint_row, checkpoint_request, checkpoint_repository,
                config_path, verify_timeout_seconds,
            )
            if remote_result == "project-blocked":
                return False
            if remote_result == "blocked":
                continue
            if remote_result == "resumed":
                return True
        else:
            remote_result = None
        if blocked["blocked_reason"] == BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value:
            # A changed published checkpoint needs the existing remote cause
            # and ownership review. Only an unchanged (or absent) checkpoint
            # can use the count-only legacy release path.
            if remote_result in {None, "unchanged"}:
                effects.hydrate_execplan_project(connection, repo, blocked)
                if effects._resume_legacy_review_count_block(
                    connection, repo_path, state_dir, repo, blocked,
                ):
                    return True
            with connection:
                effects._clear_active_in_current_transaction(connection, repo)
            continue
        if local_verify_recheck or blocked["blocked_reason"] == BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value:
            old_timeout = int(blocked["blocked_fingerprint"]) if not local_verify_recheck else 0
            new_timeout = (
                verify_timeout_seconds
                if verify_timeout_seconds is not None
                else effects.load_managed_config(config_path or effects.ROOT / "config/config.ini").verify_timeout_seconds
            )
            if new_timeout <= old_timeout and not force_timeout_recheck:
                continue
            effects.hydrate_execplan_project(connection, repo, blocked)
            worktree = Path(str(blocked["worktree"]))
            try:
                effects.validate_execplan_workspace_identity(
                    state_dir, repo, manual_workspace_owner(connection, repo, str(blocked["plan_id"])),
                    str(blocked["branch"]), worktree,
                )
                effects.validate_blocked_agent_scope(
                    connection, repo_path, repo, base, worktree,
                    str(blocked["path"]),
                )
            except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
                effects._update_blocked_error(connection, repo, str(blocked["plan_id"]), str(error))
                with connection:
                    effects._clear_active_in_current_transaction(connection, repo)
                continue
            with connection:
                if recovery_request_id is not None:
                    from loop_manager_internal.storage import local_recovery_state
                    if not local_recovery_state.consume_recheck(connection, recovery_request_id, str(blocked["plan_id"]), "verify"):
                        effects._clear_active_in_current_transaction(connection, repo)
                        continue
                runtime_state.RuntimeStateRepository(connection).transition(
                    repo, str(blocked["plan_id"]),
                    plan_values={
                        "status": "VERIFYING", "blocked_reason": None,
                        "blocked_fingerprint": None,
                        "blocked_worktree_fingerprint": None,
                        "blocked_review_fingerprint": None,
                        "last_error": None,
                    },
                    project_values={"phase": "VERIFYING", "last_error": None},
                    enqueue=False,
                )
                scoped_blocking.clear_plan_block(connection, repo, str(blocked["plan_id"]))
            return True
        effects.hydrate_execplan_project(connection, repo, blocked)
        resume = (
            effects.resume_guidance_approval_if_decided
            if blocked["blocked_reason"] == GUIDANCE_BLOCK_REASON
            else effects.resume_blocked_execplan_if_inputs_changed
        )
        resumed = resume(connection, repo_path, repo, base, state_dir,
                         plan_id=str(blocked['plan_id']))
        refreshed = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, blocked["plan_id"]),
        ).fetchone()
        if resumed and refreshed and refreshed["status"] in ACTIVE_EXECPLAN_STATUSES:
            effects.hydrate_execplan_project(connection, repo, refreshed)
            return True
        if effects.active_execplan(connection, repo) is None:
            with connection:
                effects._clear_active_in_current_transaction(connection, repo)
    return False


from loop_manager_internal.agents.agent_scope_decision import _non_default_index_flags
from loop_manager_internal.git import managed_rebase_validation
from loop_manager_internal.recovery import checkpoint_ownership
import json

@dataclass(frozen=True)
class ResumeOwnedCheckpointProjectBlockEffects:
    """Effects for resume owned checkpoint project block; preserve existing safety gates."""
    _clear_active_in_current_transaction: Callable[..., object]
    _direct_rebase_history_proven: Callable[..., object]
    _published_review_commit_intent_matches: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    pr_for_branch: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


@dataclass(frozen=True)
class ResumeLegacyReviewCountBlockEffects:
    """Effects for resume legacy review count block; preserve existing safety gates."""
    _rebase_in_progress_for_branch: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    pr_for_branch: Callable[..., object]
    remote_branch_oid: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


@dataclass(frozen=True)
class ResumeInterruptedBlockedRebaseEffects:
    """Effects for resume interrupted blocked rebase; preserve existing safety gates."""
    _blocked_execplan_metadata_error: Callable[..., object]
    _blocked_resume_worktree_matches: Callable[..., object]
    _complete_direct_rebase_checkpoint: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _direct_rebase_history_proven: Callable[..., object]
    _finish_owned_direct_rebase: Callable[..., object]
    _owned_rebase_journal_state: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _reconcile_rebase_evidence_block: Callable[..., object]
    _record_blocked_resume_head: Callable[..., object]
    _requeue_blocked_execplan: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    clear_agent_scope: Callable[..., object]
    git: Callable[..., object]
    remote_branch_oid: Callable[..., object]


def _resume_owned_checkpoint_project_block(effects: ResumeOwnedCheckpointProjectBlockEffects,
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, block: sqlite3.Row, project: sqlite3.Row, plan: sqlite3.Row,
) -> bool:
    """Restore only a proven published checkpoint mistaken for external work."""
    plan_id = str(plan['plan_id'])
    branch = str(plan['branch'] or '')
    worktree = Path(str(plan['worktree'] or ''))
    original_phase = ('WORKING' if plan['status'] == 'IMPLEMENTING'
                      else str(plan['status']))
    blocked_projection = plan['status'] == 'PLAN_BLOCKED'
    projection_matches = (
        project['plan_path'] in (None, plan['path'])
        and project['branch'] in (None, branch)
        and project['worktree'] in (None, str(worktree))
    ) if blocked_projection else (
        project['plan_path'] == plan['path']
        and project['branch'] == branch
        and project['worktree'] == str(worktree)
    )
    if (
        block['plan_id'] != plan_id
        or project['phase'] != 'PROJECT_BLOCKED'
        or block['previous_phase'] != original_phase
        or plan['status'] not in ACTIVE_EXECPLAN_STATUSES | {'PLAN_BLOCKED'}
        or not projection_matches
        or plan['pr_url'] or project['pr_url']
        or int(plan['workspace_owned'] or 0) != WORKSPACE_OWNED
    ):
        return False
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, plan_id,
    )
    snapshot = checkpoint.snapshot or {}
    marker = snapshot.get('blocked_resume')
    review_marker = snapshot.get('published_review_resume')
    if review_marker is not None and not isinstance(review_marker, dict):
        return False
    try:
        effects.validate_execplan_workspace_identity(state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree)
        effects.validate_managed_worktree(repo_path, worktree, branch)
        current = effects.capture_git_snapshot(worktree)
        if (current.operations or current.dirty or current.branch != branch
                or (not marker and any(_non_default_index_flags(entries)
                                       for entries in current.index.values()))
                or not current.head):
            return False
        remote_oid = effects.remote_branch_oid(repo_path, branch)
        if effects.pr_for_branch(repo_path, branch) is not None:
            return False
        if marker:
            before_head = str(marker.get('before_head') or '')
            after_head = str(marker.get('after_head') or '')
            target_base = str(marker.get('target_base_oid') or '')
            if (not before_head or not after_head or not target_base
                    or marker.get('owner_oid') != remote_oid
                    or after_head != current.head):
                return False
        else:
            before_head = str(
                (review_marker.get('head') if isinstance(review_marker, dict) else None)
                or snapshot.get('reactivated_from_head') or ''
            )
            after_head = current.head
            target_base = str(snapshot.get('base_oid') or checkpoint.target_base_oid or '')
            if not before_head:
                return False
        pending_review_commit = (
            not marker and review_marker is not None
            and effects._published_review_commit_intent_matches(
                repo_path, checkpoint, current.head,
            )
        )
        pushed_review = (
            not marker and review_marker is not None
            and remote_oid != before_head
            and remote_oid == current.head == checkpoint.expected_head
            and plan['publication_remote_oid'] in (before_head, remote_oid)
            and (
                (snapshot.get('pushed_checkpoint_head') == remote_oid
                 and checkpoint.observed_remote_oid == remote_oid)
                or (checkpoint.operation == 'push'
                    and snapshot.get('remote_before') == before_head
                    and checkpoint.observed_remote_oid in (before_head, remote_oid))
            )
        )
        direct_review = (
            pushed_review or (
                not marker and before_head == remote_oid
                and (after_head == remote_oid or review_marker is not None)
                and (checkpoint.expected_head == current.head or pending_review_commit)
                and checkpoint.observed_remote_oid == remote_oid
                and (not plan['publication_remote_oid']
                     or plan['publication_remote_oid'] == remote_oid)
            )
        )
        if direct_review:
            published = connection.execute(
                'SELECT request_identity, phase, reason_code, branch, worktree, '
                'accepted_remote_oid FROM blocked_checkpoints '
                'WHERE repo = ? AND plan_id = ?', (repo, plan_id),
            ).fetchone()
            if (published is None or published['phase'] != 'published'
                    or published['reason_code'] != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value
                    or published['branch'] != branch
                    or published['worktree'] != str(worktree)
                    or published['accepted_remote_oid'] != before_head
                    or not published['request_identity']
                    or (review_marker and (
                        review_marker.get('request_identity') != published['request_identity']
                        or review_marker.get('owner_oid') != before_head
                        or review_marker.get('head') != before_head
                    ))
                    or not current.shared_metadata_complete
                    or current.refs.get(f'refs/heads/{branch}') != (current.head, '')
                    or current.refs.get(f'refs/remotes/origin/{branch}') != (remote_oid, '')):
                return False
        elif review_marker is not None and not marker:
            return False
        elif not target_base:
            return False
        if (
            (checkpoint.expected_head != current.head and not pending_review_commit)
            or (checkpoint.observed_remote_oid != remote_oid and not pushed_review)
            or (not direct_review and not effects._direct_rebase_history_proven(
                connection, repo_path, repo, plan_id, worktree, branch,
                before_head, after_head, target_base,
                plan['worker_session_id'],
            ))
        ):
            return False
        if pushed_review:
            original_oid = checkpoint_ownership.owned_remote_oid(
                connection, repo_path, repo, plan_id, branch, worktree,
                before_head, effects.git, before_head=current.head,
                recorded_oid=before_head, require_live=False,
                strict_observation=True,
            )
            live = effects.git(repo_path, 'ls-remote', '--heads', 'origin',
                       f'refs/heads/{branch}', check=False)
            if live.returncode:
                raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                      'checkpoint remote observation failed')
            ref = f'refs/heads/{branch}'
            rows = [line.split() for line in live.stdout.splitlines() if line.strip()]
            if rows and (len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref):
                raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                      'checkpoint remote response malformed')
            live_oid = rows[0][0] if rows else None
            if (original_oid != before_head
                    or live_oid != remote_oid):
                return False
            owned_oid = remote_oid
        else:
            owned_oid = checkpoint_ownership.owned_remote_oid(
                connection, repo_path, repo, plan_id, branch, worktree,
                remote_oid, effects.git,
                before_head=current.head if direct_review else before_head,
                recorded_oid=str(plan['publication_remote_oid'] or '') or None,
                strict_observation=True,
            )
        if owned_oid is None:
            return False
    except RecoveryFailure:
        raise
    except (OSError, RuntimeError, WorkspaceOwnershipError, ValueError):
        return False
    with runtime_state._transaction(connection):
        if direct_review and review_marker is None:
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(
                repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid,
                observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**snapshot, 'published_review_resume': {
                    'request_identity': str(published['request_identity']),
                    'owner_oid': owned_oid, 'head': current.head,
                }},
            )
        elif not marker and not direct_review:
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(
                repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid,
                observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**snapshot, 'blocked_resume': {
                    'owner_oid': owned_oid, 'before_head': before_head,
                    'after_head': after_head, 'target_base_oid': target_base,
                }},
            )
        connection.execute(
            'UPDATE exec_plans SET publication_remote_oid = ? '
            'WHERE repo = ? AND plan_id = ?', (owned_oid, repo, plan_id),
        )
        scoped_blocking.clear_project_block(connection, repo)
        if plan['status'] in ACTIVE_EXECPLAN_STATUSES:
            refreshed = connection.execute(
                'SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                (repo, plan_id),
            ).fetchone()
            runtime_state.RuntimeStateRepository(connection).hydrate_snapshot(
                repo, runtime_state.ExecutionSnapshot.from_row(refreshed),
            )
        else:
            effects._clear_active_in_current_transaction(connection, repo)
    return True


def _resume_legacy_review_count_block(effects: ResumeLegacyReviewCountBlockEffects,
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, plan: sqlite3.Row,
) -> bool:
    """Release only a Review occurrence block with proven workspace ownership."""

    plan_id = str(plan["plan_id"])
    block = scoped_blocking.plan_block(connection, repo, plan_id)
    if (
        block is None
        or block["reason_code"] != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value
        or scoped_blocking.project_block(connection, repo) is not None
        or plan["status"] != "PLAN_BLOCKED"
        or plan["blocked_reason"] != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value
        or plan["pr_url"] or plan["pr_head_oid"]
        or plan["agent_recovery"] or int(plan["pr_conflict_recovery"] or 0)
        or not str(plan["review_result"] or "").strip().endswith("VERDICT: FAIL")
        or not plan["agent_result_snapshot"]
        or int(plan["workspace_owned"] or 0) != WORKSPACE_OWNED
    ):
        return False
    record = recovery_state.RecoveryRepository(connection).row(repo, plan_id)
    if record is not None and record["final_evidence"]:
        try:
            evidence = json.loads(str(record["final_evidence"]))
        except (TypeError, ValueError):
            return False
        if evidence.get("block_reason_code") != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value:
            return False
    worktree = Path(str(plan["worktree"] or ""))
    branch = str(plan["branch"] or "")
    if not branch or not worktree.is_dir():
        return False
    checkpoint = connection.execute(
        "SELECT phase, reason_code, published_oid, local_oid, "
        "tree_oid, commit_identity FROM blocked_checkpoints "
        "WHERE repo = ? AND plan_id = ?", (repo, plan_id),
    ).fetchone()
    if checkpoint is not None and checkpoint["reason_code"] != BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value:
        return False
    if checkpoint is not None and checkpoint["phase"] not in {"published", "unpublished"}:
        return False
    published_resume = None
    try:
        effects.validate_execplan_workspace_identity(state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree)
        effects.validate_managed_worktree(repo_path, worktree, branch)
        if effects._rebase_in_progress_for_branch(worktree, branch):
            return False
        snapshot = effects.capture_git_snapshot(worktree)
        if snapshot.branch != branch or snapshot.operations or not snapshot.head:
            return False
        blocked_snapshot = GitSnapshot.deserialize(str(plan["agent_result_snapshot"]))
        if (not snapshot.shared_metadata_complete
                or not blocked_snapshot.shared_metadata_complete):
            return False
        if checkpoint is None or (
            checkpoint["phase"] == "unpublished" and not checkpoint["local_oid"]
        ):
            if (
                snapshot.head != blocked_snapshot.head
                or snapshot.branch != blocked_snapshot.branch
                or snapshot.index != blocked_snapshot.index
                or snapshot.dirty != blocked_snapshot.dirty
            ):
                return False
        elif checkpoint["phase"] == "unpublished":
            local_oid = str(checkpoint["local_oid"])
            commit_identity = str(checkpoint["commit_identity"] or "")
            tree_oid = str(checkpoint["tree_oid"] or "")
            if (
                snapshot.head != local_oid or not commit_identity or not tree_oid
                or effects.git(repo_path, "rev-parse", f"{local_oid}^{{tree}}").stdout.strip() != tree_oid
                or f"Checkpoint-Identity: {commit_identity}" not in effects.git(
                    repo_path, "show", "-s", "--format=%B", local_oid,
                ).stdout
            ):
                return False
        effects.validate_blocked_agent_scope(
            connection, repo_path, repo,
            str(connection.execute(
                "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
            ).fetchone()[0]),
            worktree, str(plan["path"]),
        )
        remote_oid = effects.remote_branch_oid(repo_path, branch)
        observed_remote = effects.git(
            repo_path, "ls-remote", "--heads", "origin",
            f"refs/heads/{branch}", check=False,
        )
        remote_lines = [
            line.split() for line in observed_remote.stdout.splitlines() if line.strip()
        ]
        expected_lines = ([[remote_oid, f"refs/heads/{branch}"]]
                          if remote_oid is not None else [])
        if observed_remote.returncode or remote_lines != expected_lines:
            return False
        if effects.pr_for_branch(repo_path, branch) is not None:
            return False
        if checkpoint is not None and checkpoint["phase"] == "published":
            published = str(checkpoint["published_oid"] or "")
            request_row = connection.execute(
                "SELECT request_identity, reason_code, branch, worktree, "
                "accepted_remote_oid FROM blocked_checkpoints "
                "WHERE repo = ? AND plan_id = ?", (repo, plan_id),
            ).fetchone()
            if (not published or remote_oid != published
                    or snapshot.head != published or snapshot.dirty
                    or any(_non_default_index_flags(entries)
                           for entries in snapshot.index.values())
                    or request_row is None
                    or request_row["reason_code"] != block["reason_code"]
                    or request_row["branch"] != branch
                    or request_row["worktree"] != str(worktree)
                    or request_row["accepted_remote_oid"] != published
                    or request_row["request_identity"] !=
                    blocked_checkpoint_state.request_identity(
                        repo, plan_id, str(block["reason_code"]),
                        str(block["evidence"]), branch, str(worktree),
                        str(block["release_fingerprint"] or "") or None,
                    )
                    or checkpoint_ownership.owned_remote_oid(
                        connection, repo_path, repo, plan_id, branch,
                        worktree, remote_oid, effects.git, before_head=snapshot.head,
                        recorded_oid=str(plan["publication_remote_oid"] or "") or None,
                        strict_observation=True,
                    ) != remote_oid):
                return False
            published_resume = {
                "request_identity": str(request_row["request_identity"]),
                "owner_oid": published,
                "head": snapshot.head,
            }
        elif remote_oid is not None and remote_oid != plan["publication_remote_oid"]:
            return False
        if remote_oid is not None:
            ancestry = effects.git(
                repo_path, "merge-base", "--is-ancestor", remote_oid,
                snapshot.head, check=False,
            )
            if ancestry.returncode not in (0, 1):
                raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                      'checkpoint ancestry observation failed')
            if ancestry.returncode == 1:
                return False
    except RecoveryFailure:
        raise
    except (AgentScopeError, OSError, RuntimeError, WorkspaceOwnershipError,
            KeyError, TypeError, ValueError):
        return False
    with runtime_state._transaction(connection):
        if published_resume is not None:
            execution = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
            current = execution.get(repo, plan_id)
            execution.record(
                repo, plan_id, expected_generation=current.generation,
                operation=None, expected_head=snapshot.head,
                target_base_oid=current.target_base_oid,
                observed_remote_oid=remote_oid,
                snapshot={**(current.snapshot or {}),
                          "published_review_resume": published_resume},
            )
        runtime_state.RuntimeStateRepository(connection).transition(
            repo, plan_id,
            plan_values={
                "status": "IMPLEMENTING", "blocked_reason": None,
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "blocked_review_fingerprint": None,
                "validated_base_sha": None,
                **({"publication_remote_oid": remote_oid}
                   if published_resume is not None else {}),
                "last_error": f"Review 실패:\n{str(plan['review_result'] or '')[-8000:]}",
            },
            project_values={
                "phase": "WORKING", "validated_base_sha": None,
                **({"publication_remote_oid": remote_oid}
                   if published_resume is not None else {}),
                "last_error": f"Review 실패:\n{str(plan['review_result'] or '')[-8000:]}",
            },
            enqueue=False,
        )
        scoped_blocking.clear_plan_block(connection, repo, plan_id)
    return True


def _resume_interrupted_blocked_rebase(effects: ResumeInterruptedBlockedRebaseEffects,
    connection: sqlite3.Connection, repo_path: Path,
    repo: str, base: str, plan: sqlite3.Row, worktree: Path, branch: str,
) -> bool | None:
    """Complete a saved release only after its Git effect is independently proven."""
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, str(plan['plan_id']),
    )
    block = scoped_blocking.plan_block(connection, repo, str(plan['plan_id']))
    if rebase_block_reconciliation.could_be_rebase_evidence_block(block):
        state_dir = effects._owned_rebase_state_dir(repo, str(plan['plan_id']), worktree)
        return bool(state_dir is not None and effects._reconcile_rebase_evidence_block(
            connection, repo_path, state_dir, repo, block, plan_block=True))
    marker = (checkpoint.snapshot or {}).get('blocked_resume')
    if not marker:
        return None
    published = connection.execute(
        'SELECT phase, accepted_remote_oid FROM blocked_checkpoints '
        'WHERE repo = ? AND plan_id = ?',
        (repo, plan['plan_id']),
    ).fetchone()
    if (published is None or published['phase'] != 'published'
            or marker.get('owner_oid') != published['accepted_remote_oid']):
        # A later block may publish a new checkpoint while retaining the old
        # execution snapshot. It must start a fresh release proof.
        return None
    intent = checkpoint.rebase_publication_intent()
    if intent is not None:
        try:
            # Finish the saved lease before restoring staging or observing a
            # later base. A completed publication is an idempotent no-op.
            effects._complete_direct_rebase_checkpoint(
                connection, repo, str(plan['plan_id']), worktree,
                checkpoint.target_base_oid, repo_path=repo_path, branch=branch,
                agent_session_id=plan['worker_session_id'])
        except RecoveryFailure:
            raise
        except (RuntimeError, OSError, ValueError) as error:
            effects._update_blocked_error(connection, repo, str(plan['plan_id']),
                                  f'중단된 차단 해제 rebase 게시를 완료할 수 없습니다: {error}')
            return False
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, str(plan['plan_id']))
        plan = connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                  (repo, plan['plan_id'])).fetchone()
    current_head = effects.git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    before_head = str(marker.get('before_head') or '')
    after_head = str(marker.get('after_head') or '')
    if not after_head and current_head == before_head:
        return None
    try:
        # Finish the saved release against its own target. A newer base is
        # applied by the normal lifecycle after this checkpoint is requeued.
        target_base = checkpoint.target_base_oid
        snapshot = effects.capture_git_snapshot(worktree)
        remote_oid = effects.remote_branch_oid(repo_path, branch)
        adopted_oid = (checkpoint.snapshot or {}).get('last_adopted_remote_oid')
        original_oid = published['accepted_remote_oid']
        published_result = (checkpoint.snapshot or {}).get('rebase_publication_result') or {}
        confirmed = (intent is not None and published_result.get('intent_id') == intent['id']
                     and published_result.get('head') == adopted_oid == remote_oid
                     and plan['publication_remote_oid'] == adopted_oid)
        owned_oid = checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, str(plan['plan_id']), branch,
            worktree, original_oid if confirmed else remote_oid, effects.git,
            before_head=None if confirmed else before_head,
            recorded_oid=original_oid if confirmed else str(plan['publication_remote_oid'] or '') or None,
            require_live=not confirmed,
            strict_observation=True,
        ) if not plan['pr_url'] else None
        safe = (
            owned_oid is not None
            and marker.get('owner_oid') == owned_oid
            and bool(target_base)
            and marker.get('target_base_oid') == target_base
            and checkpoint.observed_remote_oid == (adopted_oid if confirmed else owned_oid)
            and snapshot.head == current_head and snapshot.branch == branch
            and not snapshot.operations
            and (not after_head or after_head == current_head)
            and effects._blocked_execplan_metadata_error(repo_path, worktree, plan) is None
        )
        if safe:
            state_dir = effects._owned_rebase_state_dir(repo, str(plan['plan_id']), worktree)
            if state_dir is None:
                safe = False
            else:
                accepted = (checkpoint.snapshot or {}).get('current_rebase_acceptance')
                if (checkpoint.operation is None and isinstance(accepted, dict)
                        and managed_rebase_validation.valid_acceptance(accepted)
                        and (accepted['repo'], accepted['plan_id'], accepted['branch'], accepted['worktree'])
                            == (repo, str(plan['plan_id']), branch, str(worktree.resolve()))):
                    # A prior clean result was already adopted. Original stage
                    # restoration is preservation, not another completed candidate.
                    observed, decision = effects._current_rebase_service(
                        connection, repo_path, state_dir, repo, str(plan['plan_id']),
                        worktree, branch, target=target_base,
                    ).observe(checkpoint=checkpoint)
                    restored_stage = (decision.reason == 'adopted_checkpoint_work_pending'
                                      and observed.metadata is None and not observed.operations
                                      and observed.index_clean is False and bool(observed.staged)
                                      and not any(entry.stage for entry in observed.index))
                    safe = (accepted['head'] == current_head == checkpoint.expected_head
                            and accepted['target_base_oid'] == target_base
                            and accepted['remote']['oid'] == (intent['remote_before'] if confirmed else owned_oid)
                            and (decision.status.value == 'complete' or restored_stage))
                else:
                    effects._finish_owned_direct_rebase(
                        state_dir, connection, repo_path, repo,
                        str(plan['plan_id']), worktree, branch,
                        agent_session_id=plan['worker_session_id'],
                    )
                    safe = effects._direct_rebase_history_proven(
                        connection, repo_path, repo, str(plan['plan_id']), worktree,
                        branch, before_head, current_head, target_base,
                        plan['worker_session_id'],
                    )
                safe = safe and effects._blocked_resume_worktree_matches(
                    worktree, marker, snapshot, restore_staged=False)
    except RecoveryFailure:
        raise
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        safe = False
    if not safe:
        effects._update_blocked_error(connection, repo, str(plan['plan_id']),
                              '중단된 차단 해제 rebase의 게시 소유권과 HEAD를 증명할 수 없습니다.')
        return False
    try:
        preserved_observation = effects._owned_rebase_journal_state(worktree, branch)
        effects._complete_direct_rebase_checkpoint(
            connection, repo, str(plan['plan_id']), worktree, target_base,
            repo_path=repo_path, branch=branch,
            agent_session_id=plan['worker_session_id'],
            preserved_worktree=True,
            preserved_observation=preserved_observation,
        )
    except (RuntimeError, OSError, ValueError) as error:
        effects._update_blocked_error(connection, repo, str(plan['plan_id']),
                              f'중단된 차단 해제 rebase 완료 근거가 다릅니다: {error}')
        return False
    if not effects._blocked_resume_worktree_matches(worktree, marker, effects.capture_git_snapshot(worktree)):
        effects._update_blocked_error(connection, repo, str(plan["plan_id"]),
                              "중단된 차단 해제 rebase의 index 복원 결과가 다릅니다.")
        return False
    if not after_head:
        effects._record_blocked_resume_head(
            connection, repo, str(plan['plan_id']), owned_oid,
            before_head, target_base, current_head,
        )
    effects.clear_agent_scope(connection, repo)
    effects._requeue_blocked_execplan(
        connection, repo, str(plan['plan_id']),
        publication_remote_oid=runtime_state.RuntimeStateRepository(connection).get(
            repo, str(plan['plan_id'])).publication_remote_oid or owned_oid,
    )
    return True
