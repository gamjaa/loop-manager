"""Independent plan and project safety gate reconciliation."""
from __future__ import annotations

from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents.scope_recovery import ignored_comparison_view
from loop_manager_internal.git import rebase_block_reconciliation
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.git import workspace
from loop_manager_internal.git import managed_rebase_adoption
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.plan_recovery import ReservedPullRequestCompletion, reserved_workspace_ownership, workspace_ownership_isolated, recovery_admission
from loop_manager_internal.storage.task_calls import CallDeferred
from loop_manager_internal.runtime.contracts import ProcessLockBusyError
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
    _managed_worktree_marker: Callable[..., object]
    _managed_worktree_transition_state: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _clear_active_in_current_transaction: Callable[..., object]
    _clear_project_block_without_losing_planning_error: Callable[..., object]
    _reconcile_rebase_evidence_block: Callable[..., object]
    _reconciled_manager_scope_metadata: Callable[..., object]
    _requeue_blocked_execplan: Callable[..., object]
    _resume_owned_checkpoint_project_block: Callable[..., object]
    _with_proven_legacy_ignore_config: Callable[..., object]
    _normalize_legacy_ignored_snapshot: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    github_repository: Callable[..., object]
    pr_for_branch: Callable[..., object]
    reconcile_existing_pull_request: Callable[..., object]
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
    resume_retired_guidance_block: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    plan_recovery_effects: Callable[[], blocked_plan_reconciliation.PlanRecoveryEffects]
    workspace_recovery_effects: Callable[[], ResumeProjectBlockIfSafeEffects]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def resume_project_block_if_safe(effects: ResumeProjectBlockIfSafeEffects,
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *, workspace_plan_id: str | None = None, recovery_request_id: str | None = None,
) -> bool:
    block = scoped_blocking.project_block(connection, repo)
    if recovery_request_id is not None and (block is None or workspace_plan_id is not None
            or block['reason_code'] != BlockReasonCode.PLAN_INPUT_INVALID.value
            or not block['plan_id']):
        return False
    if workspace_plan_id is not None:
        if block is not None:
            return False
        block = scoped_blocking.plan_block(connection, repo, workspace_plan_id)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, workspace_plan_id)
        held = (checkpoint.snapshot or {}).get('workspace_ownership_hold')
        if held:
            block = held['block']
        elif block is None or block['reason_code'] != BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL.value:
            return False
    plan = (connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                               (repo, block['plan_id'])).fetchone() if block is not None and block['plan_id'] else None)
    if (plan is not None and int(plan['workspace_owned']) == WORKSPACE_OWNED
            and block['reason_code'] in {BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value,
                                         BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL.value}):
        try:
            effects.validate_execplan_workspace_identity(
                state_dir, repo, manual_workspace_owner(connection, repo, plan['plan_id']),
                str(plan['branch'] or ''), Path(str(plan['worktree'] or '')))
        except WorkspaceOwnershipError:
            try:
                if not _restore_checkpoint_workspace(effects, connection, repo_path, state_dir, repo, block, plan):
                    return False
            except (CallDeferred, ProcessLockBusyError, runtime_state.RuntimeStateError):
                return False
    ownership_block = block is not None and block['reason_code'] in {
        BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value,
        BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL.value}
    if (plan is None or int(plan['workspace_owned']) != WORKSPACE_RESERVED
            and not (ownership_block and int(plan['workspace_owned']) == WORKSPACE_OWNED)):
        return _resume_project_block_if_safe(effects, connection, repo_path, state_dir, repo,
                                                   workspace_block=block if workspace_plan_id else None,
                                                   recovery_request_id=recovery_request_id)
    try:
        with reserved_workspace_ownership(connection, state_dir, repo, str(plan['plan_id'])) as owner:
            expected_checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan['plan_id'])
            expected_project = runtime_state.project_row(connection, repo)
            expected_recovery = recovery_state.RecoveryRepository(connection).row(repo, plan['plan_id'])
            expected_request = blocked_checkpoint_state.scope_request_row(connection, repo, plan['plan_id'])
            result = _resume_project_block_if_safe(effects, connection, repo_path, state_dir, repo,
                                                   reservation_owner=owner,
                                                   workspace_block=block if workspace_plan_id else None,
                                                   recovery_request_id=recovery_request_id)
            if (not result and workspace_plan_id is None
                    and int(plan['workspace_owned']) == WORKSPACE_RESERVED):
                result = _narrow_workspace_project_block(effects, connection, repo_path, state_dir,
                                                        repo, block, plan, owner, expected_checkpoint, expected_project, expected_recovery, expected_request)
        if isinstance(result, ReservedPullRequestCompletion):
            with runtime_state.selected_execution(repo, result.plan_id, include_blocked=True):
                effects.reconcile_existing_pull_request(
                    connection, repo_path, state_dir, repo,
                    runtime_state.project_row(connection, repo), result.pull_request)
            return True
        return result
    except (CallDeferred, ProcessLockBusyError, runtime_state.RuntimeStateError):
        return False


def _restore_checkpoint_workspace(effects, connection, repo_path, state_dir, repo, block, plan):
    """Repair only the resource identity of an already owned, stopped candidate."""
    plan_id = str(plan['plan_id'])
    repository = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    checkpoint = repository.get(repo, plan_id)
    saved = checkpoint.snapshot or {}
    branch, path = saved.get('branch'), saved.get('worktree')
    project = connection.execute('SELECT * FROM projects WHERE repo=?', (repo,)).fetchone()
    if (not isinstance(branch, str) or not branch or not isinstance(path, str) or not path
            or not checkpoint.expected_head or checkpoint.operation is not None
            or project is None or runtime_state.planning_owns_execution_slot(project)):
        return False
    worktree = Path(path)
    try:
        effects.validate_execplan_workspace_identity(
            state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree)
        with reserved_workspace_ownership(connection, state_dir, repo, plan_id,
                                          workspace=(branch, path)) as owner:
            effects.validate_managed_worktree(repo_path, worktree, branch)
            checked = effects.capture_git_snapshot(worktree)
            if (checked.head != checkpoint.expected_head or checked.branch != branch
                    or controlling_git_operation(checked.operations) or any(agent_scope_decision.index_security_flags(entries)
                                                 for entries in checked.index.values())):
                return False
            confirmed = effects.capture_git_snapshot(worktree)
            if (not agent_scope_decision._same_local_snapshot(checked, confirmed)
                    or not workspace_ownership_isolated(checked, confirmed, repo_path=repo_path,
                        worktree=worktree, branch=branch, base=str(project['base_branch']))):
                return False
            with runtime_state._transaction(connection):
                owner.check()
                if (repository.get(repo, plan_id) != checkpoint
                        or dict(connection.execute('SELECT * FROM projects WHERE repo=?', (repo,)).fetchone()) != dict(project)
                        or dict(connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                                   (repo, plan_id)).fetchone()) != dict(plan)
                        or dict((scoped_blocking.project_block(connection, repo)
                                 if block['scope'] == 'project' else
                                 scoped_blocking.plan_block(connection, repo, plan_id)) or {}) != dict(block)):
                    return False
                repository.record(repo, plan_id, expected_generation=checkpoint.generation,
                    operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                    target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
                    snapshot={**saved, 'workspace_identity_recovery': {
                        'branch': plan['branch'], 'worktree': plan['worktree'], 'block': dict(block)}})
                runtime_state.RuntimeStateRepository(connection).update_plan(
                    repo, plan_id, plan_values={'branch': branch, 'worktree': path}, enqueue=False)
            return True
    except RecoveryFailure:
        raise
    except (AgentScopeError, OSError, RuntimeError, ValueError):
        return False


def _cancel_workspace_checkpoint(connection, repo, plan_id, block):
    """Cancel only this cause's pending publication; keep its receipt and audit."""
    repository = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
    if blocked_checkpoint_state.scope_request_row(connection, repo, plan_id) is None:
        return
    request = repository.get(repo, plan_id)
    if (request is not None and request.reason_code == block['reason_code']
            and request.evidence == block['evidence'] and request.phase.value != 'published'):
        repository._update(request, phase='unpublished', retryable=0,
                           unpublished_reason='workspace 소유권 원인의 범위 재평가로 요청을 종료했습니다.')


def _narrow_workspace_project_block(effects, connection, repo_path, state_dir, repo, block, plan, owner, checkpoint, project, recovery, request):
    """Preserve a local unresolved cause before atomically removing its gate."""
    if block['reason_code'] != BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value:
        return False
    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    existing = scoped_blocking.plan_block(connection, repo, plan['plan_id'])
    try:
        worktree = Path(plan['worktree'])
        effects.validate_execplan_workspace_identity(state_dir, repo,
            manual_workspace_owner(connection, repo, plan['plan_id']), plan['branch'], worktree)
        effects.validate_managed_worktree(repo_path, worktree, plan['branch'])
        # 과거 Agent 감사 대신 이번 관측 구간의 대상 자원 안정성을 확인한다.
        baseline = effects.capture_git_snapshot(worktree)
        observed = effects.capture_git_snapshot(worktree)
        if (not workspace_ownership_isolated(baseline, observed, repo_path=repo_path,
                worktree=worktree, branch=plan['branch'], base=project['base_branch'])
                or not agent_scope_decision._same_local_snapshot(baseline, observed)):
            return False
    except (RuntimeError, OSError, ValueError, TypeError, KeyError):
        return False
    with runtime_state._transaction(connection):
        owner.check()
        current = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                     (repo, plan['plan_id'])).fetchone()
        current_block = scoped_blocking.plan_block(connection, repo, plan['plan_id'])
        current_recovery = recovery_state.RecoveryRepository(connection).row(repo, plan['plan_id'])
        current_request = blocked_checkpoint_state.scope_request_row(connection, repo, plan['plan_id'])
        if (not scoped_blocking.project_cause_matches(connection, block)
                or dict(current) != dict(plan) or dict(runtime_state.project_row(connection, repo)) != dict(project)
                or (dict(current_block) if current_block else None) != (dict(existing) if existing else None)
                or checkpoints.get(repo, plan['plan_id']) != checkpoint
                or (dict(current_recovery) if current_recovery else None) != (dict(recovery) if recovery else None)
                or (dict(current_request) if current_request else None) != (dict(request) if request else None)):
            return False
        request = blocked_checkpoint_state.scope_request_row(connection, repo, plan['plan_id'])
        prior_request = (dict(request) if request is not None and request['reason_code'] == block['reason_code']
                         and request['evidence'] == block['evidence'] else None)
        _cancel_workspace_checkpoint(connection, repo, plan['plan_id'], block)
        recovery_state.RecoveryRepository(connection).resolve_workspace_block(repo, plan['plan_id'], block)
        if existing is None:
            scoped_blocking.record_plan_block(connection, repo, plan['plan_id'],
                BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL, block['evidence'], block['release_fingerprint'])
        # Preserve a separate approval/review cause in its original block row.
        # The supplemental hold is consumed before every general plan release.
        saved = checkpoints.get(repo, plan['plan_id'])
        checkpoints.record(repo, plan['plan_id'], expected_generation=saved.generation,
            operation=saved.operation, expected_head=saved.expected_head,
            target_base_oid=saved.target_base_oid, observed_remote_oid=saved.observed_remote_oid,
            snapshot={**(saved.snapshot or {}), 'workspace_ownership_hold': {
                'block': dict(block), 'resume_status': plan['status'],
                'baseline': baseline.serialize(), 'observation': observed.serialize()},
                'workspace_scope_audit': [*(saved.snapshot or {}).get('workspace_scope_audit', ()),
                    {'block': dict(block), 'scope': 'plan', 'plan_id': plan['plan_id'],
                     'branch': plan['branch'], 'worktree': plan['worktree'],
                     'resource_waiters': list(owner.observed[7]), 'checkpoint_request': prior_request}]})
        effects._clear_project_block_without_losing_planning_error(connection, repo, project)
        if not runtime_state.planning_owns_execution_slot(project) and project['plan_path'] == plan['path']:
            effects._clear_active_in_current_transaction(connection, repo)
    return True


def _resume_project_block_if_safe(effects, connection, repo_path, state_dir, repo, *, reservation_owner=None, workspace_block=None, recovery_request_id=None):
    """Release only a project block whose original safety boundary is restored."""

    block = workspace_block if workspace_block is not None else scoped_blocking.project_block(connection, repo)
    if block is None:
        return False
    original_block = block
    expected_plan_block = scoped_blocking.plan_block(connection, repo, block['plan_id'])
    ownership_cause = (workspace_block is not None
                       or block['reason_code'] == BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value)
    expected_request = (blocked_checkpoint_state.scope_request_row(connection, repo, block['plan_id'])
                        if ownership_cause else None)
    expected_recovery = (recovery_state.RecoveryRepository(connection).row(repo, block['plan_id'])
                         if ownership_cause else None)
    admission = (recovery_admission(connection, repo, block['plan_id'])
                 if workspace_block is not None and expected_plan_block is not None
                 and expected_plan_block['reason_code'] == BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL.value
                 else nullcontext())
    workspace_checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, str(block['plan_id'] or ''))
    if recovery_request_id is not None and workspace_checkpoint.operation != 'rebase':
        return False
    if workspace_block is not None:
        block = {**dict(block), 'reason_code': BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value}

    def cause_matches():
        if ownership_cause:
            current_request = blocked_checkpoint_state.scope_request_row(connection, repo, block['plan_id'])
            current_recovery = recovery_state.RecoveryRepository(connection).row(repo, block['plan_id'])
            current_block = scoped_blocking.plan_block(connection, repo, block['plan_id'])
            if (execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
                    repo, str(block['plan_id'] or '')) != workspace_checkpoint
                    or (dict(current_request) if current_request else None) != (dict(expected_request) if expected_request else None)
                    or (dict(current_recovery) if current_recovery else None) != (dict(expected_recovery) if expected_recovery else None)
                    or (dict(current_block) if current_block else None) != (dict(expected_plan_block) if expected_plan_block else None)):
                return False
        if workspace_block is None:
            return scoped_blocking.project_cause_matches(connection, block)
        if scoped_blocking.project_block(connection, repo) is not None:
            return False
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, block['plan_id'])
        held = (saved.snapshot or {}).get('workspace_ownership_hold')
        current = scoped_blocking.plan_block(connection, repo, block['plan_id'])
        return ((dict(current) if current else None) == (dict(expected_plan_block) if expected_plan_block else None)
                and saved == workspace_checkpoint and
                (held['block'] == dict(original_block) if held else
                 current is not None and dict(current) == dict(original_block)))

    def clear_cause(project):
        if workspace_block is None:
            effects._clear_project_block_without_losing_planning_error(connection, repo, project)
            if block['reason_code'] == BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value:
                recovery_state.RecoveryRepository(connection).resolve_workspace_block(repo, block['plan_id'], block)
                _cancel_workspace_checkpoint(connection, repo, block['plan_id'], block)
            return
        repository = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
        current = repository.get(repo, block['plan_id'])
        saved = dict(current.snapshot or {})
        held = saved.pop('workspace_ownership_hold', None)
        saved['workspace_scope_release_audit'] = [*saved.get('workspace_scope_release_audit', ()),
                                                 {'block': dict(original_block)}]
        repository.record(repo, block['plan_id'], expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot=saved)
        current_block = scoped_blocking.plan_block(connection, repo, block['plan_id'])
        if current_block is not None and current_block['reason_code'] == BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL.value:
            recovery_state.RecoveryRepository(connection).resolve_workspace_block(repo, block['plan_id'], current_block)
            _cancel_workspace_checkpoint(connection, repo, block['plan_id'], current_block)
            scoped_blocking.clear_plan_block(connection, repo, block['plan_id'])
            status = (held or {}).get('resume_status') or saved.get('scope_block_resume_status')
            if status not in {*ACTIVE_EXECPLAN_STATUSES, 'READY', 'PR_OPEN'}:
                raise runtime_state.RuntimeStateError('workspace 차단의 저장 재개 단계가 없습니다.')
            plan_row = connection.execute('SELECT last_error FROM exec_plans WHERE repo=? AND plan_id=?',
                                          (repo, block['plan_id'])).fetchone()
            values = {'status': status, 'blocked_reason': None}
            if plan_row['last_error'] == current_block['evidence']:
                values['last_error'] = None
            runtime_state.RuntimeStateRepository(connection).update_plan(repo, block['plan_id'],
                plan_values=values, enqueue=False)
        _cancel_workspace_checkpoint(connection, repo, block['plan_id'], original_block)

    if block['reason_code'] == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value and block['plan_id']:
        project = runtime_state.project_row(connection, repo)
        plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                  (repo, block['plan_id'])).fetchone()
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, str(block['plan_id']))
        reconcile_input = checkpoint.operation != 'rebase'
        if not reconcile_input and plan is not None and plan['branch']:
            try:
                reconcile_input = manager_git_journal.live_remote_branch_oid(
                    effects.git, repo_path, str(plan['branch'])) != checkpoint.observed_remote_oid
            except (RuntimeError, OSError):
                pass
        if project is not None and plan is not None and reconcile_input:
            return effects._resume_owned_checkpoint_project_block(connection, repo_path, state_dir,
                                                                  repo, block, project, plan)
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, str(block['plan_id'] or ''))
    workspace_recovery = bool(reservation_owner is not None
        and block['reason_code'] == BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value)
    recovery = recovery_state.RecoveryRepository(connection).row(repo, str(block['plan_id'] or ''))
    if not workspace_recovery and (rebase_block_reconciliation.could_be_rebase_evidence_block(block, recovery, checkpoint)
            or scoped_blocking.is_scope_baseline_block(block) and checkpoint.operation == 'rebase'):
        return effects._reconcile_rebase_evidence_block(
            connection, repo_path, state_dir, repo, block, plan_block=False,
            **({'recovery_request_id': recovery_request_id} if recovery_request_id else {}),
        )
    if (not workspace_recovery and block['plan_id'] and block['reason_code'] in rebase_block_reconciliation.REBASE_BLOCK_REASONS
            and checkpoint.operation == 'rebase'):
        # A pending rebase cannot borrow the generic branch-only release path
        # when its stored safety cause cannot be connected to this operation.
        return False
    if (block["reason_code"] == BlockReasonCode.LEGACY_UNSCOPED_BLOCK.value
            and not scoped_blocking.is_scope_baseline_block(block)):
        return False
    plan_id = str(block["plan_id"] or "")
    project = runtime_state.project_row(connection, repo)
    if project is None:
        return False
    planning_owns_slot = runtime_state.planning_owns_execution_slot(project)
    if not plan_id:
        if block['reason_code'] == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value:
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
            elif scoped_blocking.is_scope_baseline_block(block):
                fingerprint = str(project['planning_attempt_fingerprint'] or '')
                if fingerprint:
                    if not project['worktree'] or not project['planning_workspace_owned']:
                        return False
                    effects.validate_planning_worktree_ownership(
                        connection, repo_path, state_dir, repo, fingerprint, worktree)
                    checked = effects.capture_git_snapshot(worktree)
                elif (runtime_state.RuntimeStateRepository(connection).scope_catalog_available()
                      and runtime_state.RuntimeStateRepository(connection).active_row(repo) is not None):
                    return False
            else:
                return False
        except RecoveryFailure:
            raise
        except (AgentScopeError, RuntimeError, WorkspaceOwnershipError, ValueError):
            return False
        if checked is not None:
            try:
                effects.validate_planning_worktree_ownership(
                    connection, repo_path, state_dir, repo,
                    str(project['planning_attempt_fingerprint'] or ''), worktree)
            except (RuntimeError, WorkspaceOwnershipError, ValueError):
                return False
        confirmed = effects.capture_git_snapshot(worktree) if checked is not None else None
        with admission, runtime_state._transaction(connection):
            if (not cause_matches()
                    or dict(runtime_state.project_row(connection, repo)) != dict(project)
                    or (checked is not None and not agent_scope_decision._same_local_snapshot(
                        confirmed, checked))):
                return False
            clear_cause(project)
            if scoped_blocking.is_scope_baseline_block(block):
                checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
                previous = checkpoints.get(repo, '')
                checkpoints.record(repo, '', expected_generation=previous.generation,
                    operation=previous.operation, expected_head=previous.expected_head,
                    target_base_oid=previous.target_base_oid, observed_remote_oid=previous.observed_remote_oid,
                    snapshot={**(previous.snapshot or {}), 'scope_block_release_audit': [
                        *(previous.snapshot or {}).get('scope_block_release_audit', ()),
                        {'block': dict(block), 'prior_agent_recovery': project['agent_recovery']}]})
                runtime_state.RuntimeStateRepository(connection)._update_project_row(
                    repo, {'agent_recovery': None})
            recovery_state.RecoveryRepository(connection).resolve_git_scope_failure(repo, None)
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
    if block['reason_code'] == BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value:
        return effects._resume_owned_checkpoint_project_block(
            connection, repo_path, state_dir, repo, block, project, plan,
        )
    if (scoped_blocking.is_scope_baseline_block(block) and not plan['worktree']
            and plan['status'] not in {*ACTIVE_EXECPLAN_STATUSES, 'PLAN_BLOCKED'}):
        with admission, runtime_state._transaction(connection):
            if reservation_owner is not None:
                reservation_owner.check()
            if (not cause_matches()
                    or dict(runtime_state.project_row(connection, repo)) != dict(project)
                    or dict(connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                               (repo, plan_id)).fetchone()) != dict(plan)):
                return False
            saved = dict(checkpoint.snapshot or {})
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(
                repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**saved, 'scope_block_release_audit': [
                    *saved.get('scope_block_release_audit', ()),
                    {'block': dict(block), 'prior_scope': {key: project[key] for key in (
                        'agent_snapshot', 'agent_result_snapshot', 'agent_manager_snapshot')}}]})
            clear_cause(project)
            recovery_state.RecoveryRepository(connection).resolve_git_scope_failure(repo, plan_id)
            recovery_state.RecoveryRepository(connection).resolve_git_scope_failure(repo, None)
            if not planning_owns_slot:
                effects._clear_active_in_current_transaction(connection, repo)
        return True
    if (not scoped_blocking.is_scope_baseline_block(block)
            and block['reason_code'] != BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value):
        return False
    def rebase_source(snapshot):
        if checkpoint.operation != 'rebase':
            return None
        return workspace.reserved_rebase_before_head(effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch,
            allow_reserved=True), checkpoint, snapshot)

    reserved_recovered = False
    terminal_pr = None
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
        if int(plan['workspace_owned']) == WORKSPACE_RESERVED and worktree.is_dir():
            try:
                manager_git_journal.recover_active(
                    state_dir, repo=str(repo_path.resolve()), kind='managed_worktree_create',
                    target=str(worktree.resolve()),
                    observe=lambda: effects._managed_worktree_transition_state(repo_path, worktree),
                    marker=lambda intent: effects._managed_worktree_marker(state_dir, intent, 'create'),
                )
                live = effects._managed_worktree_transition_state(repo_path, worktree)
                checked = effects.capture_git_snapshot(worktree)
                adoption_observation = reservation_owner.remote_adoption(
                    plan, checkpoint, repo_path, worktree, git=effects.git,
                    github_repository=effects.github_repository, pr_for_branch=effects.pr_for_branch)
                adopted_pr = dict(adoption_observation or ())
                if adopted_pr and (str(adopted_pr.get('state') or '').upper() == 'MERGED'
                                   or adopted_pr.get('mergedAt')):
                    terminal_pr = adopted_pr
                elif adopted_pr and str(adopted_pr.get('state') or '').upper() == 'CLOSED':
                    reservation_owner.check()
                    if execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id) != checkpoint:
                        return False
                    with runtime_state.selected_execution(repo, plan_id, include_blocked=True):
                        effects.reconcile_existing_pull_request(
                            connection, repo_path, state_dir, repo,
                            runtime_state.project_row(connection, repo), adopted_pr)
                    return False
                head = workspace.validate_reserved_worktree_history(
                    state_dir, repo_path, worktree, branch, plan_id, live, checked,
                    checkpoint=checkpoint, git=effects.git, base=str(project['base_branch']),
                    session=plan['worker_session_id'],
                    sessions=reservation_owner.worker_sessions if reservation_owner else (),
                    adoption_eligible=reservation_owner.adoption_eligible,
                    rebase_before_head=rebase_source(checked),
                    agent_scopes=reservation_owner.agent_scopes(plan, checkpoint) if reservation_owner else ())
                reserved_recovered = True
            except (RuntimeError, OSError, ValueError):
                return False
        else:
            if worktree.exists() or worktree.is_symlink() or branch_state.returncode != 1:
                return False
            status = str(plan["status"])
            with admission, runtime_state._transaction(connection):
                if reservation_owner is not None:
                    reservation_owner.check()
                if (not cause_matches()
                        or dict(runtime_state.project_row(connection, repo)) != dict(project)
                        or dict(connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                                   (repo, plan_id)).fetchone()) != dict(plan)
                        or execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, plan_id) != checkpoint):
                    return False
                clear_cause(project)
                if status in ACTIVE_EXECPLAN_STATUSES:
                    effects._requeue_blocked_execplan(connection, repo, plan_id)
                elif not planning_owns_slot and project['plan_path'] in (None, plan['path']):
                    effects._clear_active_in_current_transaction(connection, repo)
            return True
    if int(plan["workspace_owned"]) != WORKSPACE_OWNED and not reserved_recovered:
        return False
    worktree = Path(str(plan["worktree"] or ""))
    branch = str(plan["branch"] or "")
    try:
        reservation_snapshot = checked if reserved_recovered else None
        checked = effects.capture_git_snapshot(worktree)
        effects.validate_execplan_workspace_identity(
            state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree
        )
        effects.validate_managed_worktree(
            repo_path,
            worktree,
            branch,
            **({'allow_rebase': True} if ownership_cause and checkpoint.operation == 'rebase' else {}),
        )
        if ownership_cause and checkpoint.operation == 'rebase' and not reserved_recovered:
            rebase_source(checked)
        if reserved_recovered and (not agent_scope_decision._same_local_snapshot(checked, reservation_snapshot)
                or effects._managed_worktree_transition_state(repo_path, worktree) != live):
            return False
        if plan["agent_snapshot"] and not reserved_recovered:
            effects.validate_blocked_agent_scope(
                connection, repo_path, repo, str(project['base_branch']),
                worktree, str(plan['path']), saved_plan=plan,
                require_safe_result=scoped_blocking.is_scope_baseline_block(block))
        if not agent_scope_decision._same_local_snapshot(effects.capture_git_snapshot(worktree), checked):
            return False
    except RecoveryFailure:
        raise
    except (AgentScopeError, RuntimeError, WorkspaceOwnershipError, ValueError):
        return False
    confirmed = effects.capture_git_snapshot(worktree) if checked is not None else None
    if ownership_cause and checkpoint.operation == 'rebase':
        try:
            rebase_source(confirmed)
        except (RuntimeError, OSError, ValueError):
            return False
    if reserved_recovered:
        try:
            if reservation_owner.remote_adoption(plan, checkpoint, repo_path, worktree, git=effects.git,
                    github_repository=effects.github_repository, pr_for_branch=effects.pr_for_branch) != adoption_observation:
                return False
        except (RuntimeError, OSError, ValueError):
            return False
    if reserved_recovered and effects._managed_worktree_transition_state(repo_path, worktree) != live:
        return False
    with admission, runtime_state._transaction(connection):
        if reservation_owner is not None:
            reservation_owner.check()
        if (not cause_matches()
                or dict(runtime_state.project_row(connection, repo)) != dict(project)
                or dict(connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                           (repo, plan_id)).fetchone()) != dict(plan)
                or (checked is not None and not agent_scope_decision._same_local_snapshot(
                        confirmed, checked))):
            return False
        if reserved_recovered:
            if reservation_owner is not None:
                reservation_owner.check()
            repository = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
            if repository.get(repo, plan_id) != checkpoint:
                return False
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo, plan_id, plan_values={'workspace_owned': WORKSPACE_OWNED}, enqueue=False)
            repository.record(
                repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**(checkpoint.snapshot or {}), 'workspace_reservation_recovered': {
                    'head': head, 'block': dict(block), 'worktree': str(worktree), 'branch': branch},
                    **({'accepted_work_branch': checkpoint.snapshot['remote_adoption']['head']}
                       if (checkpoint.snapshot or {}).get('remote_adoption')
                       and 'accepted_work_branch' not in checkpoint.snapshot else {})},
            )
            plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                      (repo, plan_id)).fetchone()
            recovery_state.RecoveryRepository(connection).resolve_rebase_block(repo, plan_id, block)
        clear_cause(project)
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
                    {'block': dict(block), 'candidate': checked.serialize() if checked else None,
                     'prior_agent_recovery': plan['agent_recovery'],
                     'prior_artifacts': {key: plan[key] for key in (
                         'worker_result', 'verify_result', 'review_result', 'last_error')}}]},
            )
            recovery_state.RecoveryRepository(connection).resolve_rebase_block(repo, plan_id, block)
            recovery_state.RecoveryRepository(connection).resolve_git_scope_failure(repo, plan_id)
            recovery_state.RecoveryRepository(connection).resolve_git_scope_failure(repo, None)
            from loop_manager_internal.recovery.project_block_service import _saved_verify_matches_candidate
            matches = not checked.dirty and _saved_verify_matches_candidate(plan, checked.head)
            if plan['agent_result_snapshot']:
                try:
                    result = GitSnapshot.deserialize(plan['agent_result_snapshot'])
                    matches |= agent_scope_decision._same_local_snapshot(result, checked)
                except (ValueError, TypeError, KeyError, RuntimeError):
                    pass
            if checked.dirty and plan['status'] in {'REVIEWING', 'PUBLISHING'}:
                matches = False
            values = {'agent_recovery': None}
            if not matches:
                values.update(managed_rebase_adoption.revalidation_values(plan['active_sha']))
                values.pop('phase')
                if str(plan['status']) in ACTIVE_EXECPLAN_STATUSES:
                    values['status'] = 'IMPLEMENTING'
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo, plan_id, plan_values=values, enqueue=False)
            plan = connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                      (repo, plan_id)).fetchone()
        if workspace_block is not None:
            plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                      (repo, plan_id)).fetchone()
        if str(plan["status"]) in ACTIVE_EXECPLAN_STATUSES and project['plan_path'] in (None, plan['path']):
            runtime_state.RuntimeStateRepository(connection).hydrate_snapshot(
                repo, runtime_state.ExecutionSnapshot.from_row(plan))
        elif not planning_owns_slot and project['plan_path'] in (None, plan['path']):
            effects._clear_active_in_current_transaction(connection, repo)
    if terminal_pr is not None:
        return ReservedPullRequestCompletion(plan_id, terminal_pr)
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
    from loop_manager_internal.recovery.plan_recovery import recovering_plan
    from loop_manager_internal.application.task_scheduling import current_scheduler
    workspace_only = (current_scheduler.get() is not None and plan_id is None
                      and recovery_request_id is None and not force_timeout_recheck)
    for blocked in effects.plan_blocked_execplans(connection, repo):
        selected = str(blocked['plan_id'])
        if plan_id is not None and selected != plan_id:
            continue
        try:
            with recovering_plan(connection, state_dir, repo, selected) as available:
                if available and _resume_plan_blocks(effects, connection, repo_path, state_dir, repo, base,
                        config_path, verify_timeout_seconds, selected, force_timeout_recheck, recovery_request_id,
                        workspace_only=workspace_only):
                    return True
        except RecoveryFailure:
            raise
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            effects._update_blocked_error(connection, repo, selected, str(error), preserve_cause=True)
    return False


def _resume_plan_blocks(effects: ResumePlanBlocksEffects,
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
    *, workspace_only: bool = False,
) -> bool:
    """Reevaluate each plan block without letting one overwrite another."""

    if scoped_blocking.project_block(connection, repo) is not None:
        return False
    if not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo):
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
        workspace_checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, str(blocked['plan_id']))
        if (current_reason == BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL.value
                or (workspace_checkpoint.snapshot or {}).get('workspace_ownership_hold')):
            if resume_project_block_if_safe(effects.workspace_recovery_effects(), connection,
                    repo_path, state_dir, repo, workspace_plan_id=str(blocked['plan_id'])):
                return True
            continue
        if workspace_only:
            continue
        local_verify_recheck = force_timeout_recheck and current_reason in {
            BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value,
            BlockReasonCode.VERIFY_RETRY_EXHAUSTED.value,
        }
        local_metadata_recheck = bool(force_timeout_recheck and recovery_request_id
            and current_reason == BlockReasonCode.PLAN_INPUT_INVALID.value
            and workspace_checkpoint.operation == 'rebase')
        local_conflict_recheck = bool(force_timeout_recheck and recovery_request_id
            and current_reason == BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value
            and workspace_checkpoint.operation == 'rebase')
        if force_timeout_recheck and not (local_verify_recheck or local_metadata_recheck or local_conflict_recheck):
            continue
        if current_reason == BlockReasonCode.REMOTE_BRANCH_DELETED.value:
            continue
        if current_reason == BlockReasonCode.USER_DECISION_REQUIRED.value:
            if not blocked_plan_reconciliation.slot_available(connection, repo, str(blocked['plan_id'])):
                continue
            resolved, _detail, _stage = effects.plan_recovery_effects()._evaluate_remote_checkpoint_cause(
                connection, repo_path, state_dir, repo, blocked, config_path, verify_timeout_seconds)
            if not resolved:
                continue
        changed_plan_input = False
        if current_reason == BlockReasonCode.PLAN_INPUT_INVALID.value:
            checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
                repo, str(blocked['plan_id']))
            if checkpoint.operation == 'rebase':
                if local_metadata_recheck:
                    if effects._reconcile_rebase_evidence_block(
                            connection, repo_path, state_dir, repo, current_block,
                            plan_block=True, recovery_request_id=recovery_request_id):
                        return True
                    continue
                if effects.resume_blocked_execplan_if_inputs_changed(
                        connection, repo_path, repo, base, state_dir, plan_id=str(blocked['plan_id'])):
                    return True
                continue
            original_revision = str(
                blocked['active_sha'] or blocked['current_revision_sha']
                or blocked['plan_sha'] or ''
            )
            try:
                plan_effects = effects.plan_recovery_effects()
                if original_revision:
                    current_fingerprint = plan_effects._revision_execplan_input_fingerprint(
                        repo_path, f'refs/remotes/origin/{base}', blocked,
                    )
                    original_fingerprint = plan_effects._revision_execplan_input_fingerprint(
                        repo_path, original_revision, blocked,
                    )
                    changed_plan_input = current_fingerprint != original_fingerprint
                if not changed_plan_input and blocked['worktree']:
                    current_fingerprint = plan_effects._worktree_execplan_input_fingerprint(
                        Path(blocked['worktree']), blocked,
                    )
                    changed_plan_input = bool(
                        blocked['blocked_worktree_fingerprint']
                        and current_fingerprint != blocked['blocked_worktree_fingerprint']
                        and current_fingerprint != blocked['blocked_fingerprint']
                    )
            except (OSError, RuntimeError, ValueError):
                changed_plan_input = False
        if (scoped_blocking.is_scope_baseline_block(current_block)
                or current_reason == BlockReasonCode.PLAN_INPUT_INVALID.value
                and changed_plan_input):
            # A changed specification/plan input has its own local handoff
            # path, which isolates the stale Agent result before it can be
            # checkpointed as the next candidate.
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
                **({'recovery_request_id': recovery_request_id} if local_conflict_recheck else {}),
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
                if effects.resume_retired_guidance_block(
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
                    if not local_recovery_state.consume_recheck(connection, recovery_request_id, str(blocked["plan_id"]), "verify", repo=repo):
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
            effects.resume_retired_guidance_block
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
    validate_blocked_agent_scope: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    remote_integration_effects: Callable[..., object] | None = None


@dataclass(frozen=True)
class ResumeLegacyReviewCountBlockEffects:
    """Effects for resume legacy review count block; preserve existing safety gates."""
    _rebase_in_progress_for_branch: Callable[..., object]
    _with_proven_legacy_ignore_config: Callable[..., object]
    _normalize_legacy_ignored_snapshot: Callable[..., object]
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
    """Restore a currently owned task independently of old publication receipts."""
    if effects.remote_integration_effects is not None:
        from loop_manager_internal.recovery import work_branch_integration
        if (not plan['branch'] or not plan['worktree'] or int(plan['workspace_owned'] or 0) != WORKSPACE_OWNED
                or effects.unmet_execplan_dependencies(connection, repo, str(plan['plan_id']))
                or project['plan_path'] not in (None, plan['path'])
                or project['branch'] not in (None, plan['branch'])
                or project['worktree'] not in (None, plan['worktree'])
                or runtime_state.planning_owns_execution_slot(project)):
            return False
        try:
            if plan['agent_snapshot']:
                effects.validate_blocked_agent_scope(connection, repo_path, repo, str(project['base_branch']),
                    Path(plan['worktree']), str(plan['path']), saved_plan=plan, require_safe_result=True)
            remote = manager_git_journal.live_remote_branch_oid(effects.git, repo_path, str(plan['branch']))
            pr = effects.pr_for_branch(repo_path, str(plan['branch']), str(plan['pr_url'] or '') or None)
            work_branch_integration.synchronize(effects.remote_integration_effects(), connection,
                repo_path, state_dir, repo, plan, remote, pr)
        except RecoveryFailure as error:
            if error.reason is RecoveryReason.REMOTE_BRANCH_DELETED:
                with runtime_state._transaction(connection):
                    scoped_blocking.record_plan_block(connection, repo, str(plan['plan_id']),
                        BlockReasonCode.REMOTE_BRANCH_DELETED, str(error), None)
                    scoped_blocking.clear_project_block(connection, repo)
                return True
            return False
        except (WorkspaceOwnershipError, RuntimeError, OSError, ValueError):
            return False
        with runtime_state._transaction(connection):
            if not scoped_blocking.project_cause_matches(connection, block):
                return False
            current = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(repo, str(plan['plan_id']))
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(repo, str(plan['plan_id']),
                expected_generation=current.generation, operation=current.operation, expected_head=current.expected_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'publication_block_release_audit': [
                    *(current.snapshot or {}).get('publication_block_release_audit', ()), dict(block)]})
            scoped_blocking.clear_project_block(connection, repo)
            phase = 'VERIFYING' if plan['worker_result'] else 'WORKING'
            runtime_state.RuntimeStateRepository(connection).transition(repo, str(plan['plan_id']),
                plan_values={'status': 'VERIFYING' if plan['worker_result'] else 'IMPLEMENTING', 'last_error': None,
                             'verify_token': None, 'review_token': None, 'publication_token': None,
                             'validated_base_sha': None, 'review_result': None},
                project_values={'phase': phase, 'last_error': None}, enqueue=False)
            recovery_state.RecoveryRepository(connection).resolve_git_scope_failure(repo, str(plan['plan_id']))
        return True
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
        or effects.unmet_execplan_dependencies(connection, repo, plan_id)
    ):
        return False
    checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
        repo, plan_id,
    )
    snapshot = checkpoint.snapshot or {}
    publication_before = connection.execute(
        'SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
        (repo, plan_id)).fetchone()
    marker = snapshot.get('blocked_resume')
    review_marker = snapshot.get('published_review_resume')
    if review_marker is not None and not isinstance(review_marker, dict):
        return False
    try:
        effects.validate_execplan_workspace_identity(state_dir, repo, manual_workspace_owner(connection, repo, plan_id), branch, worktree)
        effects.validate_managed_worktree(repo_path, worktree, branch)
        current = effects.capture_git_snapshot(worktree)
        if (controlling_git_operation(current.operations) or current.dirty or current.branch != branch
                or (not marker and any(_non_default_index_flags(entries)
                                       for entries in current.index.values()))
                or not current.head):
            return False
        if plan['agent_snapshot']:
            effects.validate_blocked_agent_scope(
                connection, repo_path, repo, str(project['base_branch']), worktree,
                str(plan['path']), saved_plan=plan, require_safe_result=True)
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
                or snapshot.get('reactivated_from_head') or plan['publication_remote_oid'] or ''
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
                and (checkpoint.expected_head in (None, current.head) or pending_review_commit)
                and checkpoint.observed_remote_oid in (None, remote_oid)
                and (not plan['publication_remote_oid']
                     or plan['publication_remote_oid'] == remote_oid)
            )
        )
        if direct_review:
            published = connection.execute(
                'SELECT * FROM blocked_checkpoints '
                'WHERE repo = ? AND plan_id = ?', (repo, plan_id),
            ).fetchone()
            published = checkpoint_ownership.publication_receipt(published, before_head)
            if (published is None or published['phase'] != 'published'
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
            (checkpoint.expected_head != current.head and not pending_review_commit
             and not (direct_review and checkpoint.expected_head is None))
            or (checkpoint.observed_remote_oid != remote_oid and not pushed_review
                and not (direct_review and checkpoint.observed_remote_oid is None))
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
                strict_observation=True, allow_history=True,
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
                strict_observation=True, allow_history=True,
            )
        if owned_oid is None:
            return False
    except RecoveryFailure:
        raise
    except (OSError, RuntimeError, WorkspaceOwnershipError, ValueError):
        return False
    confirmed_snapshot = effects.capture_git_snapshot(worktree)
    confirmed_remote = effects.remote_branch_oid(repo_path, branch)
    confirmed_pr = effects.pr_for_branch(repo_path, branch)
    if plan['agent_snapshot']:
        effects.validate_blocked_agent_scope(
            connection, repo_path, repo, str(project['base_branch']), worktree,
            str(plan['path']), saved_plan=plan, require_safe_result=True)
    if checkpoint_ownership.owned_remote_oid(
            connection, repo_path, repo, plan_id, branch, worktree,
            before_head if pushed_review else remote_oid, effects.git,
            before_head=current.head if direct_review else before_head,
            recorded_oid=before_head if pushed_review else str(plan['publication_remote_oid'] or '') or None,
            require_live=not pushed_review, strict_observation=True, allow_history=True) is None:
        return False
    if pushed_review:
        try:
            if manager_git_journal.live_remote_branch_oid(effects.git, repo_path, branch) != remote_oid:
                return False
        except (RuntimeError, OSError) as error:
            raise RecoveryFailure(RecoveryReason.PUBLICATION_RETRY,
                                  'checkpoint remote confirmation failed') from error
    with runtime_state._transaction(connection):
        if (not scoped_blocking.project_cause_matches(connection, block)
                or dict(connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                           (repo, plan_id)).fetchone()) != dict(plan)
                or dict(runtime_state.project_row(connection, repo)) != dict(project)
                or execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
                    repo, plan_id) != checkpoint
                or connection.execute('SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                                      (repo, plan_id)).fetchone() != publication_before
                or confirmed_snapshot != current
                or confirmed_remote != remote_oid
                or confirmed_pr is not None
                or effects.unmet_execplan_dependencies(connection, repo, plan_id)):
            return False
        if direct_review and review_marker is None:
            execution_checkpoint_state.ExecutionCheckpointRepository(connection).record(
                repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head or current.head,
                target_base_oid=checkpoint.target_base_oid,
                observed_remote_oid=checkpoint.observed_remote_oid or remote_oid,
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
        if snapshot.branch != branch or controlling_git_operation(snapshot.operations) or not snapshot.head:
            return False
        blocked_snapshot = GitSnapshot.deserialize(str(plan["agent_result_snapshot"]))
        if plan['agent_snapshot']:
            start = GitSnapshot.deserialize(str(plan['agent_snapshot']))
            blocked_snapshot = ignored_comparison_view(
                worktree, start, blocked_snapshot, snapshot, effects=effects)
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
    if rebase_block_reconciliation.could_be_rebase_evidence_block(block, checkpoint=checkpoint):
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
            and not controlling_git_operation(snapshot.operations)
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
                                      and observed.metadata is None and not controlling_git_operation(dict(observed.operations))
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
