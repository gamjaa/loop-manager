"""Preserve local inputs and resume an owned work-branch rebase.

The original index/worktree trees remain under the existing baseline ref.
An integration intent is not a publication receipt or an Agent result.
"""
from __future__ import annotations

import json
import hashlib
import tempfile
from pathlib import Path

from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation


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
        result = effects.git(worktree, '-c', 'core.hooksPath=/dev/null', 'rebase', '--abort', check=False)
        if result.returncode:
            raise RuntimeError('소유 rebase 재시작을 재시도합니다: ' + result.stdout[-2000:])
        now = effects.capture_git_snapshot(worktree)
    if now.head != restart['original_head'] or now.branch != branch or now.operations:
        raise RuntimeError('소유 rebase 복원 결과가 원래 로컬 HEAD와 다릅니다.')
    return now


def restart_base_rebase(effects, connection, repo_path, state_dir, repo, plan, remote, snapshot):
    """Pin interrupted base-conflict input before restarting for a successor."""
    plan_id, branch, worktree = plan['plan_id'], plan['branch'], Path(plan['worktree'])
    checkpoints = ExecutionCheckpointRepository(connection)
    current = checkpoints.get(repo, plan_id)
    restart = (current.snapshot or {}).get('base_rebase_restart')
    if restart is None or restart.get('phase') == 'complete':
        effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch,
            remote_oid=remote).require_owned(checkpoint=current)
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
        raise RuntimeError('보존 중인 base 충돌 재시작의 원격 입력이 변경됐습니다.')
    now = _abort_preserved_rebase(effects, connection, repo_path, state_dir, repo, plan, current, restart)
    if now.dirty:
        raise RuntimeError('base rebase 복원 뒤 미확인 로컬 작업물이 남았습니다.')
    current = checkpoints.get(repo, plan_id)
    return checkpoints.record(
        repo, plan_id, expected_generation=current.generation, operation=None,
        expected_head=now.head, target_base_oid=remote,
        observed_remote_oid=current.observed_remote_oid,
        snapshot={**(current.snapshot or {}), 'base_rebase_restart': {**restart, 'phase': 'complete'}})


def integrate(effects, connection, repo_path, state_dir, repo, plan, remote, snapshot):
    plan_id, branch = str(plan['plan_id']), str(plan['branch'])
    worktree = Path(plan['worktree'])
    checkpoints = ExecutionCheckpointRepository(connection)
    current = checkpoints.get(repo, plan_id)
    intent = (current.snapshot or {}).get('work_branch_integration')
    if intent and (intent['branch'], intent['worktree']) != (branch, str(worktree)):
        raise RuntimeError('진행 중인 작업 branch 통합의 소유권이 다릅니다.')
    if intent and intent['phase'] == 'complete' and intent['remote'] != remote and current.operation is None:
        current = checkpoints.record(
            repo, plan_id, expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot={**(current.snapshot or {}), 'work_branch_integration': None,
                      'work_branch_integration_history': [
                          *(current.snapshot or {}).get('work_branch_integration_history', ()), intent]})
        intent = None

    def save(value, *, head=None, operation=None, history=None):
        nonlocal current, intent
        current = checkpoints.get(repo, plan_id)
        current = checkpoints.record(
            repo, plan_id, expected_generation=current.generation,
            operation=operation, expected_head=head or current.expected_head,
            target_base_oid=current.target_base_oid if operation == 'rebase' else remote,
            observed_remote_oid=remote,
            snapshot={**(current.snapshot or {}), 'integration_repo': repo,
                      'work_branch_integration': value,
                      **({'work_branch_integration_history': history} if history is not None else {})})
        intent = value

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
            raise RuntimeError('보존 중인 충돌 재시작의 원격 입력이 다시 변경됐습니다.')
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
            result = effects._journaled_direct_rebase(
                connection, repo_path, repo, plan_id, worktree, branch, remote,
                observed_remote=(remote,), agent_session_id=plan['worker_session_id'])
            if result.returncode:
                raise RuntimeError('작업 branch 통합이 충돌 해결을 기다립니다: ' + result.stdout[-2000:])
        service = effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch,
            target=remote, remote_oid=remote)
        observed, decision = service.require_owned()
        if decision.status.value == 'ready_to_start':
            result = effects._journaled_direct_rebase(
                connection, repo_path, repo, plan_id, worktree, branch, remote,
                observed_remote=(remote,), agent_session_id=plan['worker_session_id'])
            if result.returncode:
                raise RuntimeError('작업 branch 통합이 충돌 해결을 기다립니다: ' + result.stdout[-2000:])
            observed, decision = service.require_owned()
        if decision.status.value != 'complete':
            raise RuntimeError('작업 branch 통합의 수동 해결을 기다립니다.')
        # Accept only the stable current result. Publication stays with the
        # existing checkpoint/base boundary and its exact remote lease.
        with runtime_state._transaction(connection):
            if service.observe()[1] != decision:
                raise RuntimeError('작업 branch 통합 완료 관측이 변경됐습니다.')
            save({**intent, 'phase': 'complete', 'head': observed.head}, head=observed.head)
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
