"""Preserve local inputs and resume an owned work-branch rebase.

The original index/worktree trees remain under the existing baseline ref.
An integration intent is not a publication receipt or an Agent result.
"""
from __future__ import annotations

import json
import hashlib
import tempfile
from pathlib import Path
from loop_manager_internal.runtime.resource_lock import resource_locks

from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from loop_manager_internal.git import managed_rebase_adoption
from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation


def _require_ignored_safe(effects, worktree, target=None, *, abort=False):
    """Observe current paths only at a pending file/index write boundary."""
    if abort:
        detail = effects._rebase_resume_ignored_overlap(worktree, '--abort')
    else:
        overlaps = effects._ignored_paths_overlapping_git_update(worktree, target)
        detail = ('원격 통합 tracked 경로와 겹치는 ignored 파일: ' + ', '.join(overlaps[:8])
                  if overlaps else None)
    if detail:
        raise RuntimeError(detail)


def normalized_tree(effects, worktree, snapshot, index_tree):
    """Build an application tree with Git attributes, retaining raw baselines."""
    with tempfile.TemporaryDirectory(prefix='loop-manager-integration-index-') as directory:
        index = Path(directory) / 'index'
        effects._git_with_index(worktree, index, 'read-tree', index_tree)
        effects._git_with_index(worktree, index, '--literal-pathspecs', 'add', '--all',
                                '--', *sorted(snapshot.dirty))
        return effects._git_with_index(worktree, index, 'write-tree').stdout.strip()


def _preserve_conflict(effects, repo, worktree, snapshot):
    baseline = effects._capture_agent_baseline(repo, worktree, snapshot, preserve_conflicts=True)
    captured = effects.capture_git_snapshot(worktree)
    if (captured.head, captured.branch, captured.index, captured.dirty, captured.operations, captured.config) != (
            snapshot.head, snapshot.branch, snapshot.index, snapshot.dirty, snapshot.operations, snapshot.config):
        raise RuntimeError('충돌 원형 보존 중 사용자 입력이 변경됐습니다.')
    if snapshot.dirty and not baseline:
        raise RuntimeError('현재 충돌 원형을 보존할 수 없습니다.')
    return baseline


def _successor_restart(effects, worktree, restart, remote):
    """Retarget preserved input only to a fetched, related task history."""
    relation = effects.git(worktree, 'merge-base',
                           restart['remote'], remote, check=False)
    if relation.returncode != 0:
        raise RuntimeError('보존된 충돌 재시작 입력의 후속 commit을 확인할 수 없습니다.')
    return {**restart, 'remote': remote}


def _abort_preserved_rebase(effects, connection, repo_path, state_dir, repo, plan, current, restart):
    worktree, branch = Path(plan['worktree']), plan['branch']
    now = effects.capture_git_snapshot(worktree)
    if controlling_git_operation(now.operations):
        effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan['plan_id'], worktree, branch,
            remote_oid=restart['remote']).require_owned(checkpoint=current)
        original = GitSnapshot.deserialize(restart['snapshot'])
        if (now.head, now.index, now.dirty, now.operations) != (
                original.head, original.index, original.dirty, original.operations):
            raise RuntimeError('보존된 충돌 원형과 현재 작업물이 다릅니다.')
        _require_ignored_safe(effects, worktree, abort=True)
        result = effects.git(worktree, '-c', 'core.hooksPath=/dev/null', 'rebase', '--abort', check=False)
        if result.returncode:
            raise RuntimeError('소유 rebase 재시작을 재시도합니다: ' + result.stdout[-2000:])
        now = effects.capture_git_snapshot(worktree)
    if now.head != restart['original_head'] or now.branch != branch or now.operations:
        raise RuntimeError('소유 rebase 복원 결과가 원래 로컬 HEAD와 다릅니다.')
    return now


def _complete_owned_rebase(checkpoints, repo, plan_id, current, service, observed, decision, remote, saved):
    """Accept a stable result at its original target under the fetched live lease."""
    confirmed = service.observe()[1]
    with runtime_state._transaction(checkpoints.connection):
        if confirmed != decision or checkpoints.get(repo, plan_id) != current:
            raise RuntimeError('관리 rebase 완료 관측이 변경됐습니다.')
        return checkpoints.record(
            repo, plan_id, expected_generation=current.generation, operation=None,
            expected_head=observed.head, target_base_oid=current.target_base_oid,
            observed_remote_oid=remote, snapshot=saved)


def restart_base_rebase(effects, connection, repo_path, state_dir, repo, plan, remote, snapshot):
    """Accept a finished base rebase or preserve its conflict for a successor."""
    plan_id, branch, worktree = plan['plan_id'], plan['branch'], Path(plan['worktree'])
    checkpoints = ExecutionCheckpointRepository(connection)
    current = checkpoints.get(repo, plan_id)
    restart = (current.snapshot or {}).get('base_rebase_restart')
    service = effects._current_rebase_service(
        connection, repo_path, state_dir, repo, plan_id, worktree, branch, remote_oid=remote)
    observed, decision = service.require_owned(checkpoint=current)
    if current.operation == 'rebase' and decision.status.value == 'complete':
        # A completed base rebase may have rewritten the accepted work-branch
        # input. Keep its original start and acceptance together for subsequent
        # adoption; a successor lease does not make this an aborted operation.
        saved = current.snapshot or {}
        history = list(saved.get('base_rebase_restart_history', ()))
        if restart and (restart.get('phase') == 'complete' or restart['remote'] != remote):
            history.append(restart)
            restart = (None if restart.get('phase') == 'complete' else
                       _successor_restart(effects, worktree, restart, remote))
        acceptance = decision.acceptance
        return _complete_owned_rebase(
            checkpoints, repo, plan_id, current, service, observed, decision, remote,
            {**saved, 'integration_repo': repo,
             'current_rebase_acceptance': acceptance,
             'current_rebase_acceptance_history': [
                 *saved.get('current_rebase_acceptance_history', ()),
                 *([saved['current_rebase_acceptance']] if saved.get('current_rebase_acceptance') else [])],
             'base_rebase_restart_history': history,
             'base_rebase_restart': {**(restart or {}), 'phase': 'complete', 'remote': remote,
                 'remote_before': current.observed_remote_oid, 'original_head': current.expected_head,
                 'head': observed.head, 'acceptance_id': acceptance['id']}})
    if (current.operation is None and restart and restart.get('acceptance_id')
            and (restart.get('head'), restart['remote']) == (observed.head, remote)
            and decision.status.value == 'complete'):
        return current
    if restart is None or restart.get('phase') == 'complete':
        baseline = _preserve_conflict(effects, repo, worktree, snapshot)
        restart = dict(remote=remote, original_head=current.expected_head,
                       snapshot=snapshot.serialize(), baseline=baseline, phase='preserving')
        history = (current.snapshot or {}).get('base_rebase_restart_history', ())
        previous = (current.snapshot or {}).get('base_rebase_restart')
        current = checkpoints.record(
            repo, plan_id, expected_generation=current.generation, operation=current.operation,
            expected_head=current.expected_head, target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={**(current.snapshot or {}), 'integration_repo': repo, 'base_rebase_restart': restart,
                      'base_rebase_restart_history': [*history, *([previous] if previous else [])]})
    if restart['remote'] != remote:
        effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch,
            remote_oid=remote).require_owned(checkpoint=current)
        previous = restart
        restart = _successor_restart(effects, worktree, restart, remote)
        current = checkpoints.record(
            repo, plan_id, expected_generation=current.generation, operation=current.operation,
            expected_head=current.expected_head, target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={**(current.snapshot or {}), 'base_rebase_restart': restart,
                      'base_rebase_restart_history': [
                          *(current.snapshot or {}).get('base_rebase_restart_history', ()), previous]})
    now = _abort_preserved_rebase(effects, connection, repo_path, state_dir, repo, plan, current, restart)
    if now.dirty:
        raise RuntimeError('base rebase 복원 뒤 미확인 로컬 작업물이 남았습니다.')
    current = checkpoints.get(repo, plan_id)
    return checkpoints.record(
        repo, plan_id, expected_generation=current.generation, operation=None,
        expected_head=now.head, target_base_oid=remote,
        observed_remote_oid=current.observed_remote_oid,
        snapshot={**(current.snapshot or {}), 'base_rebase_restart': {**restart, 'phase': 'complete'}})


def published_branch(connection, repo, plan):
    if plan['publication_remote_oid']:
        return plan['publication_remote_oid']
    published = connection.execute(
        'SELECT published_oid FROM blocked_checkpoints WHERE repo=? AND plan_id=?',
        (repo, plan['plan_id'])).fetchone()
    return published['published_oid'] if published else None


def require_remote_present(connection, repo, plan, remote, pull_request):
    from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
    if remote is not None or pull_request and (pull_request.get('state') == 'MERGED' or pull_request.get('mergedAt')):
        return
    current = ExecutionCheckpointRepository(connection).get(repo, str(plan['plan_id']))
    if (current.snapshot or {}).get('fresh_remote_creation'):
        return
    if published_branch(connection, repo, plan):
        raise RecoveryFailure(RecoveryReason.REMOTE_BRANCH_DELETED,
            'Manager 소유 원격 작업 브랜치가 Merge 전에 삭제됐습니다. '
            f"명시적 재시작 요청이 필요합니다: {plan['branch']}")


def synchronize(effects, connection, repo_path, state_dir, repo, plan, remote, pull_request):
    """현재 작업의 원격 입력을 보존해 통합하고 게시 lease와 구분한다."""
    from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
    from loop_manager_internal.storage import runtime_state
    branch, worktree = str(plan['branch']), Path(plan['worktree'])
    require_remote_present(connection, repo, plan, remote, pull_request)
    if remote is None:
        return False
    if pull_request:
        project = connection.execute('SELECT base_branch FROM projects WHERE repo=?', (repo,)).fetchone()
        if (plan['pr_url'] and pull_request.get('url') != plan['pr_url']
                or pull_request.get('headRefName') != branch
                or pull_request.get('baseRefName') not in (None, project['base_branch'])):
            raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, '현재 작업과 PR 대상이 다릅니다.')
        if pull_request.get('headRefOid') != remote:
            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '원격 branch와 PR head 관측 시점이 다릅니다.')
    current = ExecutionCheckpointRepository(connection).get(repo, str(plan['plan_id']))
    acceptance = (current.snapshot or {}).get('current_rebase_acceptance') or {}
    known = (plan['publication_remote_oid'] or published_branch(connection, repo, plan)
             or (acceptance.get('remote') or {}).get('oid'))
    integration = (current.snapshot or {}).get('work_branch_integration') or {}
    pending = bool(integration and integration.get('phase') != 'complete')
    if remote == (current.snapshot or {}).get('accepted_work_branch') and not pending:
        return False
    effects.validate_execplan_workspace_identity(state_dir, repo,
        manual_workspace_owner(connection, repo, str(plan['plan_id'])), branch, worktree)
    effects.validate_managed_worktree(repo_path, worktree, branch, allow_rebase=current.operation == 'rebase')
    project = connection.execute('SELECT origin FROM projects WHERE repo=?', (repo,)).fetchone()
    for context in (repo_path, worktree):
        for args in (('remote', 'get-url', '--all', 'origin'), ('remote', 'get-url', '--push', '--all', 'origin')):
            urls = effects.git(context, *args).stdout.splitlines()
            if len(urls) != 1 or effects.github_repository(urls[0]) != project['origin']:
                raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, '현재 등록 origin과 Git 대상이 다릅니다.')
    with resource_locks(state_dir, str(repo_path.resolve()), ['git:fetch'], owner='work-branch-fetch'):
        effects.git(repo_path, 'fetch', '--no-tags', 'origin',
                    f'+refs/heads/{branch}:refs/remotes/origin/{branch}')
        fetched = effects.git(repo_path, 'rev-parse', 'FETCH_HEAD^{commit}').stdout.strip()
    live = effects.git(repo_path, 'ls-remote', '--heads', 'origin', f'refs/heads/{branch}').stdout.splitlines()
    if fetched != remote or [line.split() for line in live] != [[remote, f'refs/heads/{branch}']]:
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '작업 branch가 fetch 사이에 변경됐습니다.')
    metadata_error = effects._blocked_execplan_revision_metadata_error(repo_path, remote, plan)
    if metadata_error:
        raise RecoveryFailure(RecoveryReason.PLAN_INPUT_INVALID, metadata_error)
    before = effects.capture_git_snapshot(worktree)
    if current.operation == 'push' and remote != current.expected_head:
        contains = effects.git(worktree, 'merge-base', '--is-ancestor', current.expected_head, remote, check=False)
        if contains.returncode not in (0, 1):
            raise RecoveryFailure(RecoveryReason.STATUS_FAILED, '중단된 게시 후보의 현재 원격 포함 여부를 확인할 수 없습니다.')
        current = ExecutionCheckpointRepository(connection).record(repo, str(plan['plan_id']),
            expected_generation=current.generation, operation=None, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot={**(current.snapshot or {}), 'retired_publications': [
                *(current.snapshot or {}).get('retired_publications', ()),
                {'operation': 'push', 'head': current.expected_head,
                 'remote_before': (current.snapshot or {}).get('remote_before'),
                 'observed_remote': remote, 'candidate_present': contains.returncode == 0,
                 'command_success_confirmed': False}]})
    if current.operation == 'rebase' and remote != known and not integration:
        service = effects._current_rebase_service(connection, repo_path, state_dir, repo,
            str(plan['plan_id']), worktree, branch, remote_oid=remote)
        _, decision = service.require_owned(checkpoint=current)
        if decision.status.value != 'complete':
            # Finish the pinned operation first; a successor must not erase
            # resolutions already present in this index/worktree.
            checkpoints = ExecutionCheckpointRepository(connection)
            checkpoints.record(repo, str(plan['plan_id']), expected_generation=current.generation,
                operation=current.operation, expected_head=current.expected_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=remote,
                snapshot={**(current.snapshot or {}), 'rebase_remote_oid': remote,
                          'pending_work_branch_input': remote})
            if pull_request and plan['pr_url']:
                values = {'pr_head_oid': remote}
                if plan['pr_conflict_recovery']:
                    values['pr_conflict_head_oid'] = remote
                runtime_state.RuntimeStateRepository(connection).update_project(repo, values)
            return False
        restart_base_rebase(effects, connection, repo_path, state_dir, repo, plan, remote, before)
        current = ExecutionCheckpointRepository(connection).get(repo, str(plan['plan_id']))
        before = effects.capture_git_snapshot(worktree)
    if controlling_git_operation(before.operations) and current.operation != 'rebase':
        raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '사용자 Git 작업이 진행 중입니다.')
    if current.operation == 'rebase' and remote == known and not pending:
        return False
    ancestor = effects.git(worktree, 'merge-base', '--is-ancestor', remote, before.head, check=False)
    if ancestor.returncode not in (0, 1):
        raise RecoveryFailure(RecoveryReason.STATUS_FAILED, '현재 작업 branch의 변경 관계를 확인할 수 없습니다.')
    after = before
    if (ancestor.returncode == 1 and remote != known) or pending:
        if effects.git(worktree, 'merge-base', before.head, remote, check=False).returncode:
            raise RecoveryFailure(RecoveryReason.PLAN_INPUT_INVALID, '작업 branch의 공통 이력을 확인할 수 없습니다.')
        try:
            after = integrate(effects, connection, repo_path, state_dir, repo, plan, remote, before)
        except RuntimeError as error:
            if effects.capture_git_snapshot(worktree).operations:
                raise RecoveryFailure(RecoveryReason.REBASE_CONFLICT, str(error)) from error
            raise
    checkpoints = ExecutionCheckpointRepository(connection)
    current = checkpoints.get(repo, str(plan['plan_id']))
    checkpoints.record(repo, str(plan['plan_id']), expected_generation=current.generation,
        operation=current.operation, expected_head=after.head,
        target_base_oid=current.target_base_oid, observed_remote_oid=remote,
        snapshot={**(current.snapshot or {}), 'accepted_work_branch': remote, 'pending_work_branch_input': None})
    if pull_request and plan['pr_url']:
        runtime_state.RuntimeStateRepository(connection).update_project(repo, {'pr_head_oid': remote})
    changed = after.head != before.head or after.index != before.index or after.dirty != before.dirty
    if changed and plan['status'] in runtime_state.ACTIVE_STATUSES:
        values = managed_rebase_adoption.revalidation_values(plan['active_sha'])
        values.pop('worker_result', None)
        values['phase'] = 'VERIFYING' if plan['worker_result'] else 'WORKING'
        runtime_state.RuntimeStateRepository(connection).update_project(repo, values)
    return changed


def integrate(effects, connection, repo_path, state_dir, repo, plan, remote, snapshot):
    plan_id, branch = str(plan['plan_id']), str(plan['branch'])
    worktree = Path(plan['worktree'])
    checkpoints = ExecutionCheckpointRepository(connection)
    current = checkpoints.get(repo, plan_id)
    intent = (current.snapshot or {}).get('work_branch_integration')
    if intent and (intent['branch'], intent['worktree']) != (branch, str(worktree)):
        raise RuntimeError('진행 중인 작업 branch 통합의 소유권이 다릅니다.')
    def save(value, *, head=None, operation=None, history=None):
        nonlocal current, intent
        current = checkpoints.get(repo, plan_id)
        current = checkpoints.record(
            repo, plan_id, expected_generation=current.generation,
            operation=operation, expected_head=head or current.expected_head,
            target_base_oid=current.target_base_oid if operation == 'rebase' else value['remote'],
            observed_remote_oid=remote,
            snapshot={**(current.snapshot or {}), 'integration_repo': repo,
                      'work_branch_integration': value,
                      **({'work_branch_integration_history': history} if history is not None else {})})
        intent = value

    def complete(service, observed, decision):
        nonlocal current, intent
        # Bind completion to both the stable current result and this intent's
        # generation. A newer fetched lease does not change the completed target.
        completed = {**intent, 'phase': 'complete', 'head': observed.head}
        current = _complete_owned_rebase(
            checkpoints, repo, plan_id, current, service, observed, decision, remote,
            {**(current.snapshot or {}), 'integration_repo': repo, 'work_branch_integration': completed})
        intent = completed

    if intent and intent['remote'] != remote and current.operation == 'rebase':
        service = effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch,
            remote_oid=remote)
        observed, decision = service.require_owned(checkpoint=current)
        if decision.status.value == 'complete':
            # Manual continue may have finished Git before SQLite caught up.
            # Accept that result before integrating the successor; never abort it.
            complete(service, observed, decision)
            snapshot = effects.capture_git_snapshot(worktree)

    if intent and intent['phase'] == 'complete' and intent['remote'] != remote and current.operation is None:
        current = checkpoints.record(
            repo, plan_id, expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot={**(current.snapshot or {}), 'work_branch_integration': None,
                      'work_branch_integration_history': [
                          *(current.snapshot or {}).get('work_branch_integration_history', ()), intent]})
        intent = None

    if (intent and intent['remote'] != remote and current.operation is None
            and intent['phase'] in {'preserving', 'rebasing'}):
        # The caller fetched and verified a successor. No rebase has started:
        # keep the original raw baseline/index and pinned commit, and retry the
        # idempotent preservation checks before starting against the new input.
        before = GitSnapshot.deserialize(intent['before'])
        if (snapshot.branch != branch or snapshot.config != before.config
                or controlling_git_operation(snapshot.operations)):
            raise RuntimeError('중단된 작업 branch 통합의 로컬 소유권이 변경됐습니다.')
        save({**intent, 'remote': remote, 'phase': 'preserving'},
             history=[*(current.snapshot or {}).get('work_branch_integration_history', ()), intent])

    if intent and intent['remote'] != remote and current.operation == 'rebase':
        # A user pushed a newer successor while our owned rebase was paused.
        # Preserve the complete conflict index and working bytes before abort.
        restart = intent.get('restart')
        if restart is None:
            service = effects._current_rebase_service(
                connection, repo_path, state_dir, repo, plan_id, worktree, branch,
                remote_oid=remote)
            service.require_owned(checkpoint=current)
            preserved = _preserve_conflict(effects, repo, worktree, snapshot)
            restart = dict(remote=remote, original_head=current.expected_head,
                           snapshot=snapshot.serialize(), baseline=preserved)
            save({**intent, 'restart': restart}, head=current.expected_head, operation='rebase')
        elif restart['remote'] != remote:
            effects._current_rebase_service(
                connection, repo_path, state_dir, repo, plan_id, worktree, branch,
                remote_oid=remote).require_owned(checkpoint=current)
            restart = _successor_restart(effects, worktree, restart, remote)
            save({**intent, 'restart': restart}, head=current.expected_head, operation='rebase',
                 history=[*(current.snapshot or {}).get('work_branch_integration_history', ()), intent])
        now = _abort_preserved_rebase(effects, connection, repo_path, state_dir, repo, plan, current, restart)
        current = checkpoints.get(repo, plan_id)
        current = checkpoints.record(
            repo, plan_id, expected_generation=current.generation, operation=None,
            expected_head=now.head, target_base_oid=remote, observed_remote_oid=remote,
            snapshot={**(current.snapshot or {}), 'work_branch_integration': None,
                      'work_branch_integration_history': [
                          *(current.snapshot or {}).get('work_branch_integration_history', ()), intent]})
        intent, snapshot = None, now

    if intent is None:
        # The caller validated saved Agent output, current ownership, origin,
        # remote ancestry and ignored-path collisions before this boundary.
        _require_ignored_safe(effects, worktree, remote)
        baseline = effects._capture_agent_baseline(repo, worktree, snapshot)
        preserved = json.loads(baseline) if baseline else None
        captured = effects.capture_git_snapshot(worktree)
        if (captured.head != snapshot.head or captured.branch != snapshot.branch
                or captured.index != snapshot.index or captured.dirty != snapshot.dirty
                or captured.config != snapshot.config or captured.operations != snapshot.operations):
            raise RuntimeError('보존 tree 수집 중 사용자 작업물이 변경됐습니다.')
        local_tree = (normalized_tree(effects, worktree, snapshot, preserved['index_tree']) if preserved else
                      effects.git(worktree, 'rev-parse', 'HEAD^{tree}').stdout.strip())
        local_commit = snapshot.head
        if preserved:
            local_commit = effects._commit_tree(
                worktree, local_tree, snapshot.head, f'Preserve local input for {plan_id}')
        intent = dict(remote=remote, branch=branch, worktree=str(worktree),
                      before=snapshot.serialize(), baseline=baseline,
                      local_tree=local_tree, local_commit=local_commit,
                      phase='preserving')
        save(intent, head=snapshot.head)
    elif (intent['remote'], intent['branch'], intent['worktree']) != (remote, branch, str(worktree)):
        raise RuntimeError('진행 중인 작업 branch 통합의 원격·소유권이 다릅니다.')

    if intent['phase'] == 'preserving':
        before = GitSnapshot.deserialize(intent['before'])
        now = effects.capture_git_snapshot(worktree)
        if now.head == before.head:
            if (now.index != before.index or now.dirty != before.dirty
                    or now.operations != before.operations):
                raise RuntimeError('보존 commit 적용 전 로컬 작업물이 변경됐습니다.')
            if intent['local_commit'] != before.head:
                _require_ignored_safe(effects, worktree, intent['local_commit'])
                effects.git(worktree, '-c', 'core.hooksPath=/dev/null',
                            'update-ref', f'refs/heads/{branch}',
                            intent['local_commit'], before.head)
        elif now.head != intent['local_commit']:
            raise RuntimeError('보존 commit과 다른 로컬 HEAD를 보존합니다.')
        # Only the index moves to the pinned tree. No working bytes are reset,
        # cleaned or copied; staged originals remain in the baseline ref.
        if effects.git(worktree, 'write-tree').stdout.strip() != intent['local_tree']:
            if now.index != before.index:
                raise RuntimeError('보존 commit의 index가 사용자에 의해 변경됐습니다.')
            _require_ignored_safe(effects, worktree, remote)
            effects.git(worktree, 'read-tree', intent['local_tree'])
            # Refresh only stat evidence for the newly pinned index. In
            # particular, binary entries may otherwise look unstaged until
            # Git refreshes them; changed working bytes still cause refusal.
            effects.git(worktree, 'update-index', '--refresh')
        clean = effects.capture_git_snapshot(worktree)
        if (clean.dirty or clean.head != intent['local_commit'] or clean.branch != branch
                or clean.config != before.config):
            raise RuntimeError('보존 commit 적용 중 새 로컬 작업물이 나타났습니다.')
        save({**intent, 'phase': 'rebasing'}, head=intent['local_commit'])

    if intent['phase'] == 'rebasing':
        current = checkpoints.get(repo, plan_id)
        if current.operation is None:
            _require_ignored_safe(effects, worktree, remote)
            result = effects._journaled_direct_rebase(
                connection, repo_path, repo, plan_id, worktree, branch, remote,
                observed_remote=(remote,), agent_session_id=plan['worker_session_id'])
            if result.returncode:
                detail = '작업 branch 통합이 충돌 해결을 기다립니다: ' + result.stdout[-2000:]
                save({**intent, 'conflict_detail': detail}, head=current.expected_head, operation='rebase')
                raise RuntimeError(detail)
        current = checkpoints.get(repo, plan_id)
        service = effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch,
            target=remote, remote_oid=remote)
        observed, decision = service.require_owned()
        if decision.status.value == 'ready_to_start':
            _require_ignored_safe(effects, worktree, remote)
            result = effects._journaled_direct_rebase(
                connection, repo_path, repo, plan_id, worktree, branch, remote,
                observed_remote=(remote,), agent_session_id=plan['worker_session_id'])
            if result.returncode:
                detail = '작업 branch 통합이 충돌 해결을 기다립니다: ' + result.stdout[-2000:]
                save({**intent, 'conflict_detail': detail}, head=current.expected_head, operation='rebase')
                raise RuntimeError(detail)
            current = checkpoints.get(repo, plan_id)
            service = effects._current_rebase_service(
                connection, repo_path, state_dir, repo, plan_id, worktree, branch,
                target=remote, remote_oid=remote)
            observed, decision = service.require_owned()
        if decision.status.value != 'complete':
            raise RuntimeError(intent.get('conflict_detail') or '작업 branch 통합의 수동 해결을 기다립니다.')
        # Accept only the stable current result. Publication stays with the
        # existing checkpoint/base boundary and its exact remote lease.
        complete(service, observed, decision)
    return effects.capture_git_snapshot(worktree)


def user_changes(git, worktree, target, sources):
    """Pin exact user blobs against the current target; paths alone grant nothing."""
    from loop_manager_internal.agents.scope_observation import RAW_DIFF_OPTIONS
    from loop_manager_internal.git.managed_rebase_observation import parse_raw_changes

    result = {}
    for source in sources:
        if not source:
            continue
        changed = git(worktree, 'diff', *RAW_DIFF_OPTIONS, target, source, '--')
        for change in parse_raw_changes(changed.stdout):
            result.setdefault(change.path, set()).add((change.new_mode, change.new_oid))
    return result


def matching_user_paths(observed, blobs):
    """Require exact pinned bytes for every observed version of a user path."""
    allowed = set(blobs)
    for change in (*observed.committed, *observed.staged):
        if (change.new_mode, change.new_oid) not in blobs.get(change.path, ()):
            allowed.discard(change.path)
    for file in observed.files:
        allowed.discard(file.path)
    for entry in observed.index:
        if entry.stage:
            allowed.discard(entry.path)
    return allowed


def preserved_refs(repo, saved, refs):
    """Recognize only exact baseline refs recorded by this integration."""
    prefix = 'refs/loop-manager/baselines/' + hashlib.sha256(repo.encode()).hexdigest()[:16] + '/'
    allowed = set()
    for intent in [*saved.get('work_branch_integration_history', ()),
                   saved.get('work_branch_integration') or {},
                   *saved.get('base_rebase_restart_history', ()),
                   saved.get('base_rebase_restart') or {}]:
        for encoded in (intent.get('baseline'), (intent.get('restart') or {}).get('baseline')):
            if not encoded:
                continue
            baseline = json.loads(encoded)
            ref = baseline['ref']
            if (ref.startswith(prefix) and refs.get(ref) == (baseline['ref_target'], '')
                    and len(ref.removeprefix(prefix)) == 64):
                allowed.add(ref)
    return frozenset(allowed)
