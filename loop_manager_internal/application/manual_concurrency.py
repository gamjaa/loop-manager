"""Resource-scoped manual preparation, separate from the automatic execution slot."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from functools import wraps
import json
from pathlib import Path

from loop_manager_internal.git import manager_git_journal, workspace
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.runtime.contracts import ProcessLockBusyError
from loop_manager_internal.runtime.process_lock import process_lock
from loop_manager_internal.runtime.resource_lock import resource_locks
from loop_manager_internal.storage import maintenance_state, runtime_state, work_claims

from .manual_errors import ManualConflictError


def group_resources(group):
    resources = ['group:' + group['group_id']]
    resources.extend('plan:' + key for key in json.loads(group['members']))
    resources.extend('requirement:' + path for path in json.loads(group['requirements']))
    if group['branch']:
        resources.extend(('branch:' + group['branch'], 'worktree:' + group['worktree']))
    return resources


def preparation_gate(service, *, plan_ids=(), group=None, requirements=(), inspect_workspace=True):
    connection, repo = service.connection, service.repo
    if scoped_blocking.project_block(connection, repo):
        raise ManualConflictError('프로젝트 승인·차단 gate가 활성 상태입니다. 수락되지 않았습니다.')
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.phase is not maintenance_state.MaintenancePhase.IDLE:
        raise ManualConflictError(f'유지보수 gate: {cycle.phase.value}; 수락되지 않았습니다.')
    if requirements:
        for other in connection.execute("SELECT group_id,requirements FROM manual_work_groups WHERE repo=? "
                "AND kind='submit' AND state NOT IN ('MERGED','PREPARATION_CONFLICT')", (repo,)):
            if group is not None and other['group_id']==group['group_id']:
                continue
            overlap = set(requirements) & set(json.loads(other['requirements']))
            if overlap:
                raise ManualConflictError(f"수동 제출 소유 예약과 충돌: {other['group_id']}; R={', '.join(sorted(overlap))}")
    claimed_ids, claimed_paths = work_claims.publication_claims(connection, repo)
    from loop_manager_internal.storage.requirement_correction_state import claimed_paths as correction_paths
    claimed_paths = claimed_paths | correction_paths(connection, repo)
    overlap = set(plan_ids) & claimed_ids
    requested_paths = set(requirements)
    for key in plan_ids:
        row = connection.execute('SELECT sources FROM exec_plans WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
        if row:
            requested_paths.update(json.loads(row['sources']))
    paths = requested_paths & claimed_paths
    if requirements:
        paths.update(set(requirements) & work_claims.publication_intake_sources(connection, repo))
    if overlap or paths:
        raise ManualConflictError('Planner 게시 소유 예약과 충돌: ' + ', '.join(sorted(overlap | paths)))
    holds = scoped_blocking.catalog_held_plan_ids(connection, repo)
    invocation_columns = {item[1] for item in connection.execute('PRAGMA table_info(agent_invocations)')}
    for key in plan_ids:
        row = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, key)).fetchone()
        if key in holds:
            raise ManualConflictError(f'{key}: catalog hold 상태입니다.')
        unfinished = bool('finished_at' in invocation_columns and connection.execute(
            "SELECT 1 FROM agent_invocations WHERE repo=? AND plan_id=? AND (finished_at IS NULL "
            "OR result_code IS NULL AND result_kind IN ('interrupted','exception','timeout'))",
            (repo,key)).fetchone())
        if row and (unfinished or row['status'] in runtime_state.ACTIVE_STATUSES):
            raise ManualConflictError(f"{key}: {row['status']} 실행 소유권과 충돌합니다; branch={row['branch']}; worktree={row['worktree']}")
    if group is not None and group['kind']=='submit':
        from loop_manager_internal.agents.agent_invocation import InvocationContext
        from loop_manager_internal.agents.common_guidance import task_identity
        identity = task_identity(InvocationContext(repo,None,task_id='manual-submit:'+group['group_id']),
                                 'planner',service.repo_path)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(agent_invocations)')}
        if 'guidance_task_id' in columns and connection.execute(
                "SELECT 1 FROM agent_invocations WHERE repo=? AND guidance_task_id=? AND (finished_at IS NULL "
                "OR result_code IS NULL AND result_kind IN ('interrupted','exception','timeout'))",
                (repo,identity)).fetchone():
            raise ManualConflictError(f"수동 Planner 종료 상태 불명: 묶음 {group['group_id']}; 작업물을 보존합니다.")
    if group is None or not group['branch']:
        return
    members = set(json.loads(group['members']))
    for row in connection.execute('SELECT plan_id,branch,worktree FROM exec_plans WHERE repo=? '
            'AND (branch=? OR worktree=?)', (repo, group['branch'], group['worktree'])):
        if row['plan_id'] not in members:
            raise ManualConflictError(f"자원 소유자 {row['plan_id']}와 충돌: branch={row['branch']}; worktree={row['worktree']}")
    project = connection.execute('SELECT plan_path,worktree,phase FROM projects WHERE repo=?', (repo,)).fetchone()
    if project['worktree'] == group['worktree'] and project['phase'] == 'PLANNING':
        raise ManualConflictError(f"Planner worktree 소유권과 충돌: {group['worktree']}")
    targets = {group['worktree'], f"refs/heads/{group['branch']}",
               f"refs/remotes/origin/{group['branch']}", f'refs/remotes/origin/{service.base}'}
    def recoverable(intent):
        return intent['owner'] == f"plan:{group['owner_id']}" and intent['kind'] in {
            'managed_worktree_create', 'manual_branch_push'}
    if manager_git_journal.has_pending_project_mutation(service.state_dir, repo,
                                                       targets=targets, recoverable=recoverable):
        raise ManualConflictError('미완료 Git operation과 충돌: ' + ', '.join(sorted(targets)))
    path = Path(group['worktree'])
    if path.is_dir() and group['workspace_created']:
        service.effects.validate_workspace(service.repo_path, path, group['branch'])
    if inspect_workspace and path.is_dir() and service.effects.capture_snapshot(path).operations:
        raise ManualConflictError(f'미완료 Git operation을 보존합니다: {path}')


def manual_preparation(function):
    """Hold live claims through preparation; release before serial automatic driving."""
    @wraps(function)
    def guarded(service, argument):
        if function.__name__ == 'submit':
            requirements = sorted(set(argument.requirement))
            group = service.store.for_source(service.repo, argument.branch, requirements)
            resources = ['requirement:' + path for path in requirements]
            plan_ids = json.loads(group['members']) if group else []
        else:
            key = argument if function.__name__ == 'start' else argument.plan_id
            group = service.store.for_plan(service.repo, key)
            plan_ids = json.loads(group['members']) if group else [key]
            requirements = []
            branch, worktree = workspace.expected_execplan_workspace(service.state_dir, service.repo, key)
            resources = ['plan:' + key, 'branch:' + branch, 'worktree:' + str(worktree)]
        if group:
            resources.extend(group_resources(group))
        try:
            with resource_locks(service.state_dir, service.repo, resources, owner='manual-' + function.__name__):
                preparation_gate(service, plan_ids=plan_ids, group=group, requirements=requirements)
                result = function(service, argument)
        except ProcessLockBusyError as error:
            raise ManualConflictError(str(error)) from error
        return service.drive(result) if isinstance(result, str) else result
    return guarded


@contextmanager
def serial_manual_drive(state_dir):
    with ExitStack() as stack:
        try:
            stack.enter_context(process_lock(state_dir, identity='manual-followup'))
        except ProcessLockBusyError:
            yield False
            return
        yield True
