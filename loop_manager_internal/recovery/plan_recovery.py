"""차단 재평가의 대상 소유권과 자원 예약을 작업별로 분리한다."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import json
import re
from pathlib import Path

from loop_manager_internal.runtime.contracts import ProcessLockBusyError
from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage import task_calls, work_claims, database
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError


_selected_recovery = ContextVar("selected_plan_recovery", default=frozenset())


def _reservation_owners(connection, repo, plan_id):
    """Read only this reservation's writer and durable resource claims."""
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                              (repo, plan_id)).fetchone()
    if plan is None:
        raise runtime_state.RuntimeStateError('예약 workspace의 작업이 없습니다.')
    manual, owned = work_claims.manual_plan_ownership(connection, repo, plan_id, plan)
    ids, paths = work_claims.publication_claims(connection, repo)
    if owned or plan_id in ids or {plan['path'], *json.loads(plan['sources'])} & paths:
        raise task_calls.CallDeferred('예약 workspace에 수동 또는 게시 소유자가 있습니다.')
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    claims = list(connection.execute('SELECT branch,worktree FROM exec_plans WHERE repo=? AND plan_id<>?',
                                     (repo, plan_id)))
    if 'manual_work_groups' in tables:
        claims.extend(connection.execute("SELECT branch,worktree FROM manual_work_groups WHERE repo=? "
                                         "AND state NOT IN ('PR_OPEN','CLOSED','MERGED')", (repo,)))
    for other in claims:
        if ((plan['branch'] and plan['branch'] == other['branch'])
                or (plan['worktree'] and other['worktree']
                    and Path(plan['worktree']).resolve() == Path(other['worktree']).resolve())):
            raise task_calls.CallDeferred('예약 workspace에 다른 작업의 자원 소유자가 있습니다.')
    calls = (tuple(dict(row) for row in connection.execute(
        'SELECT * FROM task_call_reservations WHERE repo=? AND plan_id=? ORDER BY token',
        (repo, plan_id))) if 'task_call_reservations' in tables else ())
    invocations = (tuple(dict(row) for row in connection.execute(
        'SELECT invocation_id,scope_id,role,session_id,execution_status,execution_started_at,execution_finish_evidence,'
        'process_receipt_path,finished_at FROM agent_invocations WHERE repo=? AND plan_id=? ORDER BY invocation_id',
        (repo, plan_id))) if 'agent_invocations' in tables else ())
    grant = (connection.execute('SELECT value FROM manager_metadata WHERE key=?',
                                (f'remote_adoption:{repo}:{plan_id}',)).fetchone()
             if 'manager_metadata' in tables else None)
    project = connection.execute('SELECT origin,base_branch FROM projects WHERE repo=?', (repo,)).fetchone()
    try:
        eligible = database.remote_work_adoption_allowed(connection, repo, plan_id)
    except (KeyError, TypeError, ValueError) as error:
        raise runtime_state.RuntimeStateError('저장된 원격 인수 자격이 손상됐습니다.') from error
    adoption = (grant[0] if grant else None, eligible,
                project['origin'], project['base_branch'])
    return (plan['branch'], plan['worktree'], plan['workspace_owned'], manual, calls, invocations, adoption)


@dataclass(frozen=True)
class ReservationOwnership:
    connection: object
    repo: str
    plan_id: str
    observed: tuple

    @property
    def worker_sessions(self):
        return tuple(dict.fromkeys(call['session_id'] for call in self.observed[5] if call['role'] == 'worker'))

    @property
    def adoption_eligible(self):
        return self.observed[6][1]

    def remote_adoption(self, plan, checkpoint, repo_path, worktree, *, git, github_repository, pr_for_branch):
        """Read the saved intake's live repository/PR link without adopting again."""
        from loop_manager_internal.git.workspace import validate_registered_remote
        adoption = (checkpoint.snapshot or {}).get('remote_adoption')
        if adoption is None:
            return None
        if (not isinstance(adoption, dict) or adoption.get('version') != 1
                or any(adoption.get(key) != value for key, value in {
                    'repo': self.repo, 'plan_id': self.plan_id, 'branch': plan['branch'],
                    'base': self.observed[6][3]}.items())
                or not isinstance(adoption.get('head'), str)
                or re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', adoption['head']) is None):
            raise WorkspaceOwnershipError('기존 예약의 원격 인수 identity가 유효하지 않습니다.')
        if not self.adoption_eligible:
            raise WorkspaceOwnershipError('기존 예약의 원격 인수 자격이 저장된 선언과 다릅니다.')
        validate_registered_remote(repo_path, worktree, self.observed[6][2],
                                   git=git, github_repository=github_repository)
        pr = pr_for_branch(repo_path, str(plan['branch']), history=True)
        if ((plan['pr_url'] and (pr is None or pr.get('url') != plan['pr_url']))
                or (pr is not None and (pr.get('headRefName') != plan['branch']
                                        or pr.get('baseRefName') != self.observed[6][3]))):
            raise WorkspaceOwnershipError('기존 예약의 원격 인수 PR 연결이 다릅니다.')
        return tuple((key, pr.get(key)) for key in ('url', 'headRefName', 'baseRefName', 'headRefOid', 'state')) if pr else ()

    def agent_scopes(self, plan, checkpoint):
        """Offer recorded local outputs only for physically stopped owned calls."""
        from loop_manager_internal.agents.scope_contracts import GitSnapshot
        records = [dict(plan), *(checkpoint.snapshot or {}).get('retired_agent_scopes', ())]
        scopes = []
        for record in records:
            if (not isinstance(record, dict) or record.get('agent_role') != 'worker'
                    or not record.get('agent_snapshot') or not record.get('agent_result_snapshot')):
                continue
            try:
                start = GitSnapshot.deserialize(record['agent_snapshot'])
                result = GitSnapshot.deserialize(record['agent_result_snapshot'])
            except (RuntimeError, ValueError, TypeError, KeyError) as error:
                raise runtime_state.RuntimeStateError('예약 workspace의 저장 Worker snapshot이 손상됐습니다.') from error
            digest = hashlib.sha256(replace(start, scope_id=None, scope_start_digest=None).serialize().encode()).hexdigest()
            calls = [call for call in self.observed[5] if call['scope_id'] == start.scope_id]
            if (start.scope_id and start.scope_start_digest == digest and calls
                    and all(call['role'] == 'worker' for call in calls)
                    and result.scope_id in (None, start.scope_id)):
                scopes.append((start, result))
        return tuple(scopes)

    def check(self):
        """CAS-side check: no Git, process observation or waiting under the writer."""
        if _reservation_owners(self.connection, self.repo, self.plan_id) != self.observed:
            raise runtime_state.RuntimeStateError('예약 workspace 검증 중 writer 또는 소유권이 변경됐습니다.')


@contextmanager
def reserved_workspace_ownership(connection, state_dir, repo, plan_id):
    """Hold the existing task/resource boundary before observing reserved Git."""
    from loop_manager_internal.agents.process_receipt import recover
    observed = _reservation_owners(connection, repo, plan_id)
    branch, worktree, _, _, calls, invocations, _adoption = observed
    resources = ['plan:' + plan_id]
    resources += [prefix + value for prefix, value in (('branch:', branch), ('worktree:', worktree)) if value]
    if worktree:
        resources.append('git:' + str(Path(worktree).resolve()))
    missing = [resource for resource in resources if not resource_owned(state_dir, repo, resource)]
    with resource_locks(state_dir, repo, missing, owner='workspace-reservation-recovery'):
        proof = ReservationOwnership(connection, repo, plan_id, observed)
        proof.check()
        if connection.in_transaction:
            raise task_calls.CallDeferred('예약 writer 종료 관측 전에 DB 쓰기 처리를 종료해야 합니다.')
        for call in calls:
            if not task_calls.execution_confirmed(connection, call, state_dir):
                raise task_calls.CallDeferred('예약 workspace의 이전 호출 종료를 확인할 수 없습니다.')
        for invocation in invocations:
            if invocation['execution_finish_evidence']:
                continue
            if (invocation['execution_status'] == 'not_executed'
                    and invocation['execution_started_at'] is None and invocation['finished_at']):
                continue
            call = next((item for item in calls if item['kind'] == 'agent'
                         and item['invocation_id'] == invocation['invocation_id']), None)
            if call is not None and (call['receipt_path'] is not None
                                     or invocation['execution_started_at'] is None):
                # The matching call was physically confirmed above. A call
                # without a receipt proves non-launch only before start.
                continue
            stopped = recover(invocation['process_receipt_path'], state_dir) if invocation['process_receipt_path'] else None
            if (stopped is None or stopped.get('evidence') == 'not_started'
                    and invocation['execution_started_at'] is not None):
                raise task_calls.CallDeferred('예약 workspace의 Agent 실제 종료를 확인할 수 없습니다.')
        proof.check()
        yield proof


def target_available(connection, repo, plan_id):
    from loop_manager_internal.storage.planning_review_state import general_pending
    if general_pending(connection, repo):
        return False
    runtime = runtime_state.RuntimeStateRepository(connection)
    if not runtime.recovery_slot_available(repo):
        return False
    project = connection.execute("SELECT * FROM projects WHERE repo=?", (repo,)).fetchone()
    plan = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?", (repo, plan_id)).fetchone()
    if plan is None or plan['status'] != 'PLAN_BLOCKED':
        return False
    if project['pr_conflict_recovery'] and not project['plan_path'] and not plan['pr_conflict_recovery']:
        return False
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, pending in (('task_call_reservations', '1'), ('agent_invocations', 'finished_at IS NULL OR (execution_started_at IS NOT NULL AND execution_finish_evidence IS NULL)')):
        if table in tables and connection.execute(
                f"SELECT 1 FROM {table} WHERE repo=? AND plan_id=? AND ({pending}) LIMIT 1",
                (repo, plan_id)).fetchone():
            return False
    return connection.execute(
        "SELECT 1 FROM exec_plans WHERE repo=? AND plan_id<>? "
        "AND status IN ('IMPLEMENTING','VERIFYING','REVIEWING','PUBLISHING') "
        "AND ((? IS NOT NULL AND branch=?) OR (? IS NOT NULL AND worktree=?)) LIMIT 1",
        (repo, plan_id, plan['branch'], plan['branch'], plan['worktree'], plan['worktree'])).fetchone() is None


@contextmanager
def recovering_plan(connection, state_dir, repo, plan_id):
    key = (repo, plan_id)
    if key in _selected_recovery.get():
        yield True
        return
    if not target_available(connection, repo, plan_id):
        yield False
        return
    plan = connection.execute("SELECT branch,worktree FROM exec_plans WHERE repo=? AND plan_id=?", key).fetchone()
    resources = ['plan:' + plan_id]
    resources += [name + ':' + plan[name] for name in ('branch', 'worktree') if plan[name]]
    entered = False
    try:
        with resource_locks(state_dir, repo, resources, owner='plan-block-recheck'):
            if not target_available(connection, repo, plan_id):
                yield False
                return
            token = _selected_recovery.set(_selected_recovery.get() | {key})
            try:
                with runtime_state.selected_execution(repo, plan_id, include_blocked=True):
                    entered = True
                    yield True
            finally:
                _selected_recovery.reset(token)
                runtime = runtime_state.RuntimeStateRepository(connection)
                current = runtime.get(repo, plan_id)
                projection = connection.execute("SELECT plan_path FROM projects WHERE repo=?", (repo,)).fetchone()
                if projection[0] in (None, current.path):
                    if current.status in runtime_state.ACTIVE_STATUSES:
                        runtime.hydrate_snapshot(repo, current)
                    elif current.status == 'READY' and projection[0] == current.path:
                        runtime.update_project(repo, runtime_state.EXECUTION_CLEAR_VALUES)
    except ProcessLockBusyError:
        if entered:
            raise
        yield False


@contextmanager
def recovery_admission(connection, repo, plan_id):
    from loop_manager_internal.application.task_scheduling import current_scheduler, Task
    scheduler = current_scheduler.get()
    if scheduler is None:
        yield
    else:
        with scheduler.admission(Task(repo, plan_id, connection, resumed=True)):
            yield
