"""차단 재평가의 대상 소유권과 자원 예약을 작업별로 분리한다."""
from contextlib import contextmanager
from contextvars import ContextVar

from loop_manager_internal.runtime.contracts import ProcessLockBusyError
from loop_manager_internal.runtime.resource_lock import resource_locks
from loop_manager_internal.storage import runtime_state


_selected_recovery = ContextVar("selected_plan_recovery", default=frozenset())


def target_available(connection, repo, plan_id):
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
