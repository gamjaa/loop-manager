"""명시적인 삭제 작업 재시작: 이전 worktree 전체를 보존하고 새 실행을 만든다."""
from pathlib import Path
from uuid import uuid4

from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.storage import runtime_state, recovery_state, verify_receipts
from loop_manager_internal.storage.rebase_correction_state import RebaseCorrectionRepository
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository


def current_pr(checkpoint, pull_request):
    """새 실행에서 닫힌 과거 PR의 결과를 재사용하지 않는다."""
    restart = (checkpoint.snapshot or {}).get('deleted_branch_restart') or {}
    old = (restart.get('plan') or {}).get('pr_url')
    if pull_request and old and pull_request.get('url') == old and pull_request.get('state') != 'OPEN':
        return None
    return pull_request


def restart(effects, connection, repo_path, state_dir, repo, plan_id, *, complete_merged, unmet_dependencies, lookup_pr):
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
    block = scoped_blocking.plan_block(connection, repo, plan_id)
    if plan is not None and plan['status'] == 'READY' and block is None:
        current = ExecutionCheckpointRepository(connection).get(repo, plan_id)
        if ((current.snapshot or {}).get('deleted_branch_restart') or {}).get('state') == 'complete':
            return '같은 작업의 새 실행이 이미 READY로 준비됐습니다.'
    if plan is None or block is None or block['reason_code'] != BlockReasonCode.REMOTE_BRANCH_DELETED.value:
        raise ValueError('reset은 원격 작업 branch 삭제로 중단된 자동 작업에만 사용할 수 있습니다.')
    project = connection.execute('SELECT * FROM projects WHERE repo=? AND registered=1', (repo,)).fetchone()
    runtime = runtime_state.RuntimeStateRepository(connection)
    if (project is None or not runtime.recovery_slot_available(repo) or runtime.active_row(repo) is not None
            or scoped_blocking.project_block(connection, repo) is not None
            or plan_id in scoped_blocking.catalog_held_plan_ids(connection, repo)
            or unmet_dependencies(connection, repo, plan_id)
            or ManualWorkRepository(connection).for_plan(repo, plan_id)):
        raise RuntimeError('현재 작업의 writer·catalog·의존 관계 또는 독립 gate를 먼저 해소해야 합니다.')
    branch, worktree = str(plan['branch']), Path(plan['worktree'])
    effects.validate_execplan_workspace_identity(state_dir, repo, plan_id, branch, worktree)
    for location in (repo_path, worktree if worktree.exists() else repo_path):
        for args in (('remote', 'get-url', '--all', 'origin'), ('remote', 'get-url', '--push', '--all', 'origin')):
            urls = effects.git(location, *args).stdout.splitlines()
            if len(urls) != 1 or effects.github_repository(urls[0]) != project['origin']:
                raise RuntimeError('등록 origin과 재시작 대상이 다릅니다.')
    pr = lookup_pr(repo_path, branch, str(plan['pr_url'] or '') or None)
    if plan['pr_url'] and (not pr or pr.get('url') != plan['pr_url']
            or pr.get('headRefName') != branch or pr.get('baseRefName') != project['base_branch']):
        raise RuntimeError('재시작할 PR의 실제 대상을 확인할 수 없습니다.')
    if pr and (pr.get('state') == 'MERGED' or pr.get('mergedAt')):
        complete_merged(connection, repo_path, repo, state_dir, plan, str(pr['url']), pr.get('headRefOid'))
        return '이미 Merge된 작업을 완료 처리했습니다.'
    if manager_git_journal.live_remote_branch_oid(effects.git, repo_path, branch) is not None:
        raise RuntimeError('중단 뒤 동일 원격 branch가 다시 생성됐습니다. 현재 writer와 대상을 확인해야 합니다.')
    checkpoints = ExecutionCheckpointRepository(connection)
    current = checkpoints.get(repo, plan_id)
    saved = current.snapshot or {}
    intent = saved.get('deleted_branch_restart')
    if intent is None or intent.get('state') == 'complete':
        effects.validate_managed_worktree(repo_path, worktree, branch)
        before = effects.capture_git_snapshot(worktree)
        if before.operations:
            raise RuntimeError('진행 중인 Git 작업이 끝나야 재시작할 수 있습니다.')
        effects.git(repo_path, 'fetch', '--no-tags', 'origin',
                    f'+refs/heads/{project["base_branch"]}:refs/remotes/origin/{project["base_branch"]}')
        base = effects.git(repo_path, 'rev-parse', 'FETCH_HEAD^{commit}').stdout.strip()
        token = uuid4().hex
        intent = {'state': 'prepared', 'base': base, 'plan': dict(plan), 'block': dict(block),
            'snapshot': before.serialize(), 'archive': str(worktree.parent / 'retired' / token),
            'archive_branch': f'loop-manager/retired/{plan_id}/{token}',
            'checkpoint': dict(saved), 'operation': current.operation,
            'publication': dict(row) if (row := connection.execute(
                'SELECT * FROM blocked_checkpoints WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()) else None,
            'recovery': dict(row) if (row := recovery_state.RecoveryRepository(connection).row(repo, plan_id)) else None}
        correction = RebaseCorrectionRepository(connection).get(repo, plan_id)
        if correction is not None:
            from dataclasses import asdict
            intent['correction'] = asdict(correction)
        if effects.capture_git_snapshot(worktree) != before:
            raise RuntimeError('보존 준비 중 작업물이 변경됐습니다.')
        current = checkpoints.record(repo, plan_id, expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head, target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid, snapshot={**saved, 'deleted_branch_restart': intent})
        connection.commit()
    archive = Path(intent['archive'])
    from loop_manager_internal.agents.scope_contracts import GitSnapshot
    before = GitSnapshot.deserialize(intent['snapshot'])
    if not archive.exists():
        if effects.capture_git_snapshot(worktree) != before:
            raise RuntimeError('재시작 보존 원형과 현재 작업물이 다릅니다.')
        archive.parent.mkdir(parents=True, exist_ok=True)
        effects.git(repo_path, 'worktree', 'move', str(worktree), str(archive))
    archived = effects.capture_git_snapshot(archive)
    if (archived.head, archived.index, archived.dirty, archived.operations) != (
            before.head, before.index, before.dirty, before.operations):
        raise RuntimeError('이전 worktree 보존을 확인할 수 없습니다.')
    if archived.branch == branch:
        effects.git(archive, 'branch', '-m', branch, intent['archive_branch'])
    elif archived.branch != intent['archive_branch']:
        raise RuntimeError('보존한 branch identity가 다릅니다.')
    if not worktree.exists():
        if manager_git_journal.live_remote_branch_oid(effects.git, repo_path, branch) is not None:
            raise RuntimeError('새 실행 준비 중 원격 branch가 생성됐습니다.')
        effects.git(repo_path, 'worktree', 'add', '-b', branch, str(worktree), intent['base'])
    effects.validate_managed_worktree(repo_path, worktree, branch)
    fresh = effects.capture_git_snapshot(worktree)
    if fresh.head != intent['base'] or fresh.dirty or fresh.operations:
        raise RuntimeError('새 실행의 원본 상태가 변경됐습니다.')
    with runtime_state._transaction(connection):
        latest_block = scoped_blocking.plan_block(connection, repo, plan_id)
        if latest_block is None or dict(latest_block) != dict(block):
            raise RuntimeError('재시작 중 차단 원인이 변경됐습니다.')
        values = {key: value for key, value in runtime_state.EXECUTION_CLEAR_VALUES.items()
                  if key in runtime_state.EXECPLAN_RUNTIME_FIELDS and key not in {'branch', 'worktree', 'plan_sha'}}
        values.update(status='READY', retry_count=0, blocked_reason=None,
            blocked_fingerprint=None, blocked_worktree_fingerprint=None, blocked_review_fingerprint=None,
            verify_failure_history='{}', review_failure_history='{}',
            pr_url=str(pr['url']) if pr and pr.get('state') == 'OPEN' else None)
        runtime.transition_and_clear(repo, plan_id, expected_status='PLAN_BLOCKED', plan_values=values)
        scoped_blocking.clear_plan_block(connection, repo, plan_id)
        recovery_state.RecoveryRepository(connection).clear(repo, plan_id)
        RebaseCorrectionRepository(connection).clear(repo, plan_id)
        verify_receipts.ensure_schema(connection)
        verify_receipts.start_cycle(connection, repo, plan_id)
        connection.execute('DELETE FROM blocked_checkpoints WHERE repo=? AND plan_id=?', (repo, plan_id))
        current = checkpoints.get(repo, plan_id)
        checkpoints.record(repo, plan_id, expected_generation=current.generation, operation=None,
            expected_head=fresh.head, target_base_oid=intent['base'], observed_remote_oid=None,
            snapshot={'deleted_branch_restart': {**intent, 'state': 'complete'}, 'fresh_remote_creation': True})
    return f'새 실행을 READY로 준비했습니다. 이전 작업물 보존: {archive}'
