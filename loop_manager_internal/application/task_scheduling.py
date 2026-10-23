"""작업 관리와 실제 호출 배정을 구분하는 한 Manager 실행의 조율."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from contextlib import ExitStack, closing
from contextvars import ContextVar
from contextvars import copy_context
from dataclasses import dataclass
from functools import wraps
import threading
import time
import traceback

from loop_manager_internal.runtime import operation_events as events
from loop_manager_internal.runtime.handoff_gate import observe_runtime
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage import task_calls
from loop_manager_internal.storage.task_calls import managed_count
from loop_manager_internal.execution.validation_feedback import redact_secrets


current_scheduler: ContextVar[TaskScheduler | None] = ContextVar("task_scheduler", default=None)
current_task: ContextVar[Task | None] = ContextVar("scheduled_task", default=None)
current_verify_preflight: ContextVar[Callable[[], None] | None] = ContextVar("verify_preflight", default=None)


@contextmanager
def verify_preflight(callback: Callable[[], None]):
    token = current_verify_preflight.set(callback)
    try:
        yield
    finally:
        current_verify_preflight.reset(token)


call_revalidation = ContextVar("scheduled_call_revalidation", default=None)
project_recheck = ContextVar("scheduled_project_recheck", default=None)


class SchedulingDeferred(task_calls.CallDeferred):
    """현재 자격이 없는 작업만 실패 횟수 없이 보류한다."""


class TaskRecheckRequired(Exception):
    """대기 중 실행 입력이 바뀌어 같은 작업의 단계를 다시 준비한다."""


class SchedulingChanged(TaskRecheckRequired):
    """미시작 호출의 입력이 바뀌어 단계 준비부터 다시 관측한다."""


class PlanningDrain(Exception):
    """현재 프로젝트 호출을 비운 뒤 Planner로 돌아간다."""


@dataclass
class Task:
    repo: str
    plan_id: str
    connection: object
    resumed: bool = True
    retry_order: int | None = None
    recovery: bool = False

    def rank(self, priority_reader=None):
        from loop_manager_internal.catalog.effective_priority import effective_priorities
        manual = self.connection.execute(
            "SELECT MIN(r.created_at) FROM manual_work_members m "
            "JOIN manual_work_requests r ON r.repo=m.repo AND r.group_id=m.group_id "
            "JOIN manual_work_groups g ON g.repo=m.repo AND g.group_id=m.group_id "
            "WHERE m.repo=? AND m.plan_id=? AND g.request_id IS NOT NULL",
            (self.repo, self.plan_id),
        ).fetchone()[0]
        admission = self.connection.execute('SELECT priority FROM task_management_admissions '
            'WHERE repo=? AND plan_id=? AND priority>0', (self.repo,self.plan_id)).fetchone()
        if manual is None and admission is not None:
            return (.5, admission[0], ())
        saved = self.connection.execute('SELECT retry_order,resumed FROM task_management_admissions '
            'WHERE repo=? AND plan_id=?', (self.repo,self.plan_id)).fetchone()
        retry_order = saved['retry_order'] if saved is not None else self.retry_order
        resumed = saved['resumed'] if saved is not None and saved['resumed'] is not None else self.resumed
        if manual is None and retry_order is not None:
            return (1, retry_order, ())
        if priority_reader is not None:
            ranks=dict(priority_reader(self.connection,self.repo).priority_ranks)
            priority=ranks.get(self.plan_id,(4,int(self.plan_id[2:])))
        else:
            rows = self.connection.execute("SELECT * FROM exec_plans WHERE repo=?", (self.repo,)).fetchall()
            value = effective_priorities(rows).get(self.plan_id)
            priority=value.sort_key(self.plan_id) if value else (4,int(self.plan_id[2:]))
        return (0 if manual is not None else 2 if resumed else 3, manual or "",priority)


class TaskScheduler:
    def __init__(self, config, connect_database, state_dir, connection, *, external_run_id=None, priority_reader=None):
        self.config, self.connect_database, self.state_dir = config, connect_database, state_dir
        self.external_run_id = external_run_id
        self.priority_reader = priority_reader
        self.condition = threading.Condition()
        self.git_waiters = {}
        self.waiters = {"agent": [], "verify": [], "management": []}
        self.running = {"agent": Counter(), "verify": Counter(), "management": Counter()}
        self.admitting = False
        self.draining_projects = set()
        self.limits = {"agent": config.max_workers, "verify": config.verify_concurrency}
        self.uncertain=Counter()
        self.retained={}
        self.storage_waiting = set()
        for call in task_calls.recover(connection,state_dir):
            storage_only = task_calls.execution_confirmed(connection, call, state_dir)
            if not storage_only:
                self.running[call['kind']][call['repo']]+=1
                self.uncertain[call['kind']]+=1
            self.retained[(call['repo'],call['plan_id'],call['kind'])]={**dict(call), 'storage_only': storage_only}
        connection.execute("CREATE TABLE IF NOT EXISTS task_scheduling_cursor "
                           "(kind TEXT PRIMARY KEY, repo TEXT NOT NULL)")
        self.cursor = dict(connection.execute("SELECT kind,repo FROM task_scheduling_cursor"))
        connection.commit()

    def pause_project(self, repo):
        with self.condition:
            self.draining_projects.add(repo)
            self.condition.notify_all()
        events.emit("프로젝트 배정 중단", project=repo, reason="planning", next_action="실행 호출 종료 후 Planner")

    def check_project(self, repo):
        from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
        _check_runtime_handoff()
        with self.condition:
            if repo in self.draining_projects:
                raise PlanningDrain()

    def requeue(self, task):
        with runtime_state._transaction(task.connection):
            sequence = task.connection.execute(
                'SELECT COALESCE(MAX(retry_order),0)+1 FROM task_management_admissions').fetchone()[0]
            task.connection.execute('INSERT INTO task_management_admissions(repo,plan_id,parked,retry_order,resumed) '
                'VALUES (?,?,0,?,1) ON CONFLICT(repo,plan_id) DO UPDATE SET retry_order=excluded.retry_order,resumed=1',
                (task.repo,task.plan_id,sequence))
        with self.condition:
            task.retry_order = sequence
            task.resumed = True
            self.condition.notify_all()
        events.emit("작업 RETRY 우선 재배정", project=task.repo, plan=task.plan_id)

    def finish_storage(self, task, operation, *, stopped=False):
        """Keep a completed effect on its stack while retrying only its DB confirmation."""
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        owner = task_calls.current_call.get()
        if stopped and owner is not None and owner.kind == 'agent':
            with self.condition:
                if owner.token not in self.storage_waiting:
                    self.storage_waiting.add(owner.token)
                    self.running['agent'][task.repo] -= 1
                    self.condition.notify_all()
        while True:
            self.check_project(task.repo)
            try:
                return operation()
            except DatabaseWaitTimeout:
                events.emit('작업 결과 DB 확정 재시도 대기', level='WARN', project=task.repo, plan=task.plan_id,
                            traceback=redact_secrets(traceback.format_exc()))
                with self.condition:
                    self.condition.wait(.2)

    def retry_inputs(self, task):
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        execution = runtime_state.RuntimeStateRepository(task.connection).get(task.repo, task.plan_id)
        saved = RecoveryRepository(task.connection).row(task.repo, task.plan_id)
        recovery = tuple(saved[field] for field in ('identity', 'fingerprint', 'progress_marker', 'pending_action')) if saved else None
        inputs = tuple(getattr(execution, field) for field in (
            'status', 'current_revision_sha', 'plan_sha', 'active_sha', 'validated_base_sha',
            'publication_remote_oid', 'pr_conflict_recovery', 'pr_conflict_head_oid', 'pr_conflict_base_oid',
            'branch', 'worktree', 'pr_url', 'pr_head_oid', 'definition_snapshot', 'worker_input_token',
            'archive_token', 'verify_token', 'review_token', 'publication_token', 'revision_resume_status', 'workspace_owned',
        ))
        return inputs, recovery

    def wait_recheck(self, task, observed):
        """A timer cannot authorize another effect on the same failed inputs."""
        events.emit('작업 조건 재관측 대기', project=task.repo, plan=task.plan_id,
                    next_action='입력·복구 상태 변경 후 현재 자격 재검사')
        while True:
            self.check_project(task.repo)
            current = self.retry_inputs(task)
            if (current[0][0] not in runtime_state.ACTIVE_STATUSES
                    or current[1] is not None and current[1][-1] == 'final_block'):
                return False
            if current != observed and self._eligible(task, task.connection):
                return True
            with self.condition:
                self.condition.wait(.2)

    def wait_database(self, task):
        events.emit('DB 접근 조건 재확인 대기', project=task.repo, plan=task.plan_id,
                    next_action='쓰기 요청 자리 재확보 후 작업 재준비')
        def available():
            # Acquire the same short write boundary as the failed request,
            # without changing product state or claiming execution progress.
            with runtime_state._transaction(task.connection):
                pass
        self.finish_storage(task, available)

    def wait_retry(self, connection, repo, plan_id=None, *, resume=False):
        from loop_manager_internal.storage import recovery_state
        from loop_manager_internal.recovery.recovery_runtime import TRANSIENT, RECONCILE, RecoveryReason
        repository = recovery_state.RecoveryRepository(connection)
        saved = repository.row(repo, plan_id)
        eligible = {reason.value for reason in TRANSIENT | RECONCILE} | {
            RecoveryReason.TARGETED_TEST_CORRECTION.value,
            RecoveryReason.TARGETED_COLLECTION_PENDING.value,
        }
        if (saved is None or saved['reason_code'] not in eligible or saved['pending_action'] == 'final_block'
                or resume and saved['retry_after'] is None):
            return False
        if (saved['reason_code'] == RecoveryReason.REMOTE_SNAPSHOT_RACE.value
                and saved['pending_action'] == 'pause' and saved['attempts'] >= 3):
            return False
        if saved['pending_action'] == 'pause' and saved['occurrence'] >= 3:
            events.emit('동일 입력의 무진전 재시도 보류', level='WARN', project=repo, plan=plan_id,
                        reason_code=saved['reason_code'], occurrence=saved['occurrence'],
                        next_action='작업 보존·입력 또는 복구 조건 변경 후 재검사')
            return False
        saved = repository.defer_retry(repo, plan_id)
        events.emit('작업 재시도 대기', project=repo, plan=plan_id, reason_code=saved['reason_code'],
                    retry_after=saved['retry_after'])
        while True:
            self.check_project(repo)
            remaining = saved['retry_after'] - time.time()
            if remaining <= 0:
                break
            with self.condition:
                self.condition.wait(min(remaining, .2))
        repository.finish_retry_wait(saved)
        return True

    def wait_usage(self, connection, repo, plan_id):
        """Only an independently recorded successful call can clear a quota wait."""
        saved = connection.execute(
            'SELECT result_kind, provider, model, finished_at FROM agent_invocations '
            'WHERE repo=? AND plan_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1', (repo, plan_id)).fetchone()
        if saved is None or saved['result_kind'] != 'usage_limit':
            return False
        events.emit('사용 한도 조건 대기', project=repo, plan=plan_id)
        while True:
            self.check_project(repo)
            if saved['finished_at'] and connection.execute(
                "SELECT 1 FROM agent_invocations WHERE repo=? AND provider=? AND model=? "
                "AND finished_at>? AND result_kind='success' AND result_code=0 "
                "AND execution_status='executed' AND execution_finished_at IS NOT NULL LIMIT 1",
                (repo, saved['provider'], saved['model'], saved['finished_at'])).fetchone():
                return True
            with self.condition:
                self.condition.wait(1)

    def wait_existing_call(self, connection, repo, plan_id):
        with self.condition:
            keys = {key: value for key, value in self.retained.items() if key[:2] == (repo, plan_id)}
        if not keys:
            return False
        while keys:
            self.check_project(repo)
            live = {row['token']: row for row in task_calls.recover(
                connection, self.state_dir, tokens={value['token'] for value in keys.values()})}
            stopped = {token for token, row in live.items()
                       if task_calls.execution_confirmed(connection, row, self.state_dir)}
            with self.condition:
                for key, value in tuple(keys.items()):
                    if value['token'] not in live:
                        self.retained.pop(key, None)
                        if not value.get('storage_only'):
                            self.running[key[2]][key[0]] -= 1
                            self.uncertain[key[2]] -= 1
                        del keys[key]
                    elif value['token'] in stopped and not value.get('storage_only'):
                        self.running[key[2]][key[0]] -= 1
                        self.uncertain[key[2]] -= 1
                        value['storage_only'] = True
                if keys:
                    self.condition.wait(1)
                self.condition.notify_all()
        return True

    def _eligible(self, task, connection, *, check_priority=True, check_management=True):
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage import execplan_draft_state
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        from loop_manager_internal.storage.management_admission import parked
        if check_management and parked(connection,task.repo,task.plan_id):
            return False
        project_block = scoped_blocking.project_block(connection, task.repo)
        if ((project_block is not None and not (task.recovery and project_block['plan_id'] == task.plan_id))
                or (not task.recovery and scoped_blocking.plan_block(connection, task.repo, task.plan_id) is not None)
                or task.plan_id in scoped_blocking.catalog_held_plan_ids(connection, task.repo)
                or task.plan_id in execplan_draft_state.pending_ids(connection, task.repo)):
            return False
        project = connection.execute("SELECT * FROM projects WHERE repo=?", (task.repo,)).fetchone()
        if project is None or runtime_state.planning_owns_execution_slot(project):
            return False
        row = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?",
                                 (task.repo, task.plan_id)).fetchone()
        if (row is None or row['status'] in {'PR_OPEN', 'MERGED', 'MANUAL_WORKING'}
                or row['status'] == 'PLAN_BLOCKED' and not task.recovery):
            return False
        group = ManualWorkRepository(connection).for_plan(task.repo, task.plan_id)
        if group is not None and (not group['request_id']
                or (group['active_id'] or group['owner_id']) != task.plan_id
                or (group['pending_source_head'] and not group['source_update_conflict'])):
            return False
        from loop_manager_internal.execution.plan_validation import unmet_execplan_dependencies
        if unmet_execplan_dependencies(connection, task.repo, task.plan_id):
            return False
        from loop_manager_internal.storage import maintenance_state
        from loop_manager_internal.maintenance.maintenance_orchestration import assessment_can_yield, cycle_allows_recovery, is_unowned_ready
        cycle = maintenance_state.MaintenanceRepository(connection).get(task.repo)
        if assessment_can_yield(cycle) and is_unowned_ready(row):
            return False
        if cycle.phase in {maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                           maintenance_state.MaintenancePhase.REFACTORING,
                           maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION}:
            rows = connection.execute('SELECT * FROM exec_plans WHERE repo=?', (task.repo,)).fetchall()
            from loop_manager_internal.storage.management_admission import explicit_request
            requested = bool(group is not None and group['request_id']) or explicit_request(connection,task.repo,task.plan_id)
            if not cycle_allows_recovery(rows,cycle,task.plan_id,explicit_request=requested):
                return False
        if check_priority and self.priority_reader is not None and not task.recovery:
            decision = self.priority_reader(connection, task.repo)
            if task.plan_id not in dict(decision.priority_ranks):
                return False
        return True

    def _observation(self, task):
        """호출 대기 동안 보존해야 하는 작업·공유 gate의 현재 원본."""
        from loop_manager_internal.storage import maintenance_state
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        row = task.connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?",
                                      (task.repo, task.plan_id)).fetchone()
        group = ManualWorkRepository(task.connection).for_plan(task.repo, task.plan_id)
        return (dict(row) if row is not None else None,
                maintenance_state.MaintenanceRepository(task.connection).get(task.repo),
                dict(group) if group is not None else None)

    @contextmanager
    def admission(self, task):
        """호출과 별개로 새 관리 배정의 선택·확정을 순서대로 수행한다."""
        if task.connection.in_transaction:
            raise runtime_state.RuntimeStateError("DB 쓰기 transaction을 유지한 채 관리 배정을 기다릴 수 없습니다.")
        entry = [task, task.rank(self.priority_reader)]
        count = managed_count(task.connection, task.repo)
        with self.condition:
            self.check_project(task.repo)
            self.running['management'][task.repo] = count
            self.waiters['management'].append(entry)
            self.condition.notify_all()
        try:
            while True:
                self.check_project(task.repo)
                if not self._eligible(task, task.connection):
                    raise SchedulingDeferred("작업의 관리 배정 자격이 변경되어 재관측을 기다립니다.")
                next_entry = self._next('management', task.connection)
                before = managed_count(task.connection, task.repo)
                with self.condition:
                    self.check_project(task.repo)
                    if not self.admitting and next_entry is entry:
                        self.admitting = True
                        break
                    self.condition.wait(0.2)
        finally:
            with self.condition:
                self.waiters['management'].remove(entry)
        try:
            yield
        finally:
            try:
                count = managed_count(task.connection, task.repo)
                if count > before:
                    with runtime_state._transaction(task.connection):
                        task.connection.execute("INSERT INTO task_scheduling_cursor VALUES ('management',?) "
                            "ON CONFLICT(kind) DO UPDATE SET repo=excluded.repo", (task.repo,))
                with self.condition:
                    self.running['management'][task.repo] = count
                    if count > before:
                        self.cursor['management']=task.repo
            finally:
                with self.condition:
                    self.admitting = False
                    self.condition.notify_all()

    def _next(self, kind, connection=None):
        with self.condition:
            registered = list(self.waiters[kind])
        waiting = registered
        if connection is not None:
            waiting = [item for item in waiting if self._eligible(item[0], connection)]
            cursor = connection.execute('SELECT repo FROM task_scheduling_cursor WHERE kind=?', (kind,)).fetchone()
            refreshed = []
            for item in waiting:
                # SQLite 연결은 해당 대기 스레드가 소유한 연결만 사용한다.
                task = item[0]
                count = managed_count(connection, task.repo) if kind == 'management' else None
                rank = Task(task.repo, task.plan_id, connection, task.resumed,
                            task.retry_order, task.recovery).rank(self.priority_reader)
                refreshed.append((item, rank, count))
        with self.condition:
            if self.waiters[kind] != registered:
                return None
            if connection is not None:
                if cursor is not None:
                    self.cursor[kind] = cursor[0]
                for item, rank, count in refreshed:
                    item[1] = rank
                    if count is not None:
                        self.running[kind][item[0].repo] = count
            if not waiting:
                return None
            category = min(item[1][0] for item in waiting)
            candidates = [item for item in waiting if item[1][0] == category]
            if category == 2:
                return candidates[0]
            if category < 3:
                return min(candidates, key=lambda item: (item[1], item[0].repo, item[0].plan_id))
            basic = [item for item in candidates if not self.running[kind][item[0].repo]]
            if basic:
                repositories = sorted({item[0].repo for item in basic})
                previous = self.cursor.get(kind, "")
                repo = next((repo for repo in repositories if repo > previous), repositories[0])
                candidates = [item for item in basic if item[0].repo == repo]
            return min(candidates, key=lambda item: (item[1][2], item[0].repo, item[0].plan_id))

    @contextmanager
    def slot(self, kind, task, *, before_start=None):
        if task.connection.in_transaction or getattr(task.connection, '_depth', 0):
            raise runtime_state.RuntimeStateError("DB 쓰기 transaction을 유지한 채 호출 배정을 기다릴 수 없습니다.")
        recheck = project_recheck.get()
        if recheck is not None:
            recheck(task.repo)
        entry = [task, task.rank(self.priority_reader)]
        observed = self._observation(task)
        waited = False
        started = time.monotonic()
        with self.condition:
            self.waiters[kind].append(entry)
            self.condition.notify_all()
        try:
            while True:
                with self.condition:
                    capacity = sum(self.running[kind].values()) < self.limits[kind]
                self.check_project(task.repo)
                current = self._observation(task)
                if not self._eligible(task, task.connection, check_priority=capacity):
                    raise SchedulingDeferred("작업의 호출 자격이 변경되어 재관측을 기다립니다.")
                if current != observed:
                    raise SchedulingChanged("호출 대기 중 작업·후보·유지보수 범위가 변경됐습니다.")
                if capacity:
                    rank = task.rank(self.priority_reader)
                    next_entry = self._next(kind, task.connection)
                    # Refresh Git inputs before claiming a call slot: a fetch
                    # lease may wait while independent calls use the capacity.
                    revalidate = call_revalidation.get()
                    if next_entry is entry and waited and revalidate is not None:
                        with self.condition:
                            self.waiters[kind].remove(entry)
                            self.condition.notify_all()
                        try:
                            revalidate(waited=True)
                        finally:
                            with self.condition:
                                self.waiters[kind].append(entry)
                                self.condition.notify_all()
                        waited = False
                        continue
                with self.condition:
                    if capacity:
                        entry[1] = rank
                    self.check_project(task.repo)
                    if (any(repo == task.repo and plan_id == task.plan_id for repo, plan_id, _ in self.retained)
                            or self.uncertain[kind]>=self.limits[kind]):
                        raise task_calls.CallDeferred("기존 호출의 종료가 확인되지 않아 실제 실행 자리를 보존합니다.")
                    if capacity and sum(self.running[kind].values()) < self.limits[kind] and next_entry is entry:
                        allocation = ("unblock" if entry[1][0] == .5 else "retry" if entry[1][0] == 1 else "manual" if entry[1][0] == 0 else "resume" if entry[1][0] == 2
                                      else "basic" if not self.running[kind][task.repo] else "priority")
                        self.running[kind][task.repo] += 1
                        break
                    waited = True
                    self.condition.wait(0.2)
        finally:
            with self.condition:
                self.waiters[kind].remove(entry)
                self.condition.notify_all()
        try:
            with runtime_state._transaction(task.connection):
                owner = task_calls.reserve(task.connection, self.state_dir, task.repo,
                                           task.plan_id, kind, self.external_run_id)
        except BaseException:
            with self.condition:
                self.running[kind][task.repo] -= 1
                self.condition.notify_all()
            raise
        uncertain = True
        launched = False
        owner_token=task_calls.current_call.set(owner)
        try:
            self.check_project(task.repo)
            if self._observation(task) != observed:
                raise SchedulingChanged("실행 직전 작업 입력이 변경됐습니다.")
            if not self._eligible(task, task.connection):
                raise SchedulingDeferred("실행 직전 작업의 호출 자격이 변경됐습니다.")
            revalidate = call_revalidation.get()
            if revalidate is not None:
                revalidate(waited=False)
            self.check_project(task.repo)
            if not self._eligible(task, task.connection):
                raise SchedulingDeferred("입력 재관측 중 작업의 호출 자격이 변경됐습니다.")
            if self._observation(task) != observed:
                raise SchedulingChanged("호출 입력 재관측 중 작업 상태가 변경됐습니다.")
            if before_start is not None:
                before_start()
            events.emit("작업 호출 배정", kind=kind, project=task.repo, plan=task.plan_id,
                        running=sum(self.running[kind].values()), limit=self.limits[kind],
                        managed=managed_count(task.connection, task.repo),
                        waiting_seconds=round(time.monotonic()-started, 3), allocation=allocation,
                        waiting=len(self.waiters[kind]))
            launched = True
            yield
            uncertain = not task_calls.confirmed(owner)
            if uncertain:
                raise task_calls.CallDeferred("호출 종료 근거가 없어 실행 자리와 관리 자리를 보존합니다.")
        except BaseException as error:
            from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
            try:
                uncertain = isinstance(error, VerifyTerminationUncertain) or not task_calls.confirmed(owner)
            except BaseException:
                uncertain = True
            raise
        finally:
            released = False
            try:
                if uncertain:
                    task_calls.mark_uncertain(owner)
                else:
                    task_calls.release(owner)
                    released = True
            finally:
                try:
                    try:
                        if launched:
                            saved = task.connection.execute('SELECT resumed,retry_order FROM task_management_admissions '
                                'WHERE repo=? AND plan_id=?', (task.repo,task.plan_id)).fetchone()
                            if saved is not None:
                                task.resumed = bool(saved['resumed'])
                                task.retry_order = saved['retry_order']
                    finally:
                        with self.condition:
                            storage_only = owner.token in self.storage_waiting
                            self.storage_waiting.discard(owner.token)
                            if released:
                                if not storage_only:
                                    self.running[kind][task.repo] -= 1
                            else:
                                if not storage_only:
                                    self.uncertain[kind] += 1
                                self.retained[(task.repo, task.plan_id, kind)] = {"token": owner.token,
                                                                              'storage_only': storage_only}
                            self.condition.notify_all()
                finally:
                    task_calls.current_call.reset(owner_token)

    @contextmanager
    def task(self, repo, plan_id, connection, *, resumed=True):
        with runtime_state._transaction(connection):
            connection.execute('INSERT INTO task_management_admissions(repo,plan_id,parked,resumed) VALUES (?,?,0,?) '
                'ON CONFLICT(repo,plan_id) DO UPDATE SET resumed=COALESCE(resumed,excluded.resumed)',
                (repo,plan_id,int(resumed)))
        saved = connection.execute('SELECT retry_order,resumed FROM task_management_admissions WHERE repo=? AND plan_id=?',
            (repo,plan_id)).fetchone()
        task = Task(repo, plan_id, connection, bool(saved['resumed']), saved['retry_order'])
        token = current_task.set(task)
        try:
            from loop_manager_internal.runtime.resource_lock import resource_locks
            with resource_locks(self.state_dir, repo, ['plan:' + plan_id], owner='task-execution'), \
                 runtime_state.selected_execution(repo, plan_id):
                yield
        finally:
            current_task.reset(token)


def scheduled_call(kind):
    def decorate(function):
        def call_once(args, kwargs, returned, raised, bound_task, request_state):
            scheduler, task = current_scheduler.get(), bound_task or current_task.get()
            role = kwargs.get("role_name", args[4] if len(args) > 4 else None)
            preflight = current_verify_preflight.get() if kind == "verify" else None
            if scheduler is None:
                if preflight is not None:
                    preflight()
                if kind == 'verify':
                    observe_runtime()
                return function(*args, **kwargs)
            if kind == "agent" and role not in {"worker", "reviewer"}:
                return function(*args, **kwargs)
            with ExitStack() as stack:
                if task is None:
                    invocation, connection = kwargs.get("invocation"), kwargs.get("connection")
                    if invocation is not None and invocation.plan_id and connection is not None:
                        task = Task(invocation.repo, invocation.plan_id, connection)
                    elif kind == "verify":
                        from pathlib import Path
                        worktree = kwargs.get("worktree", args[1] if len(args) > 1 else None)
                        connection = stack.enter_context(closing(scheduler.connect_database(
                            scheduler.state_dir, run_migrations=False)))
                        row = connection.execute("SELECT repo,plan_id FROM exec_plans WHERE worktree=?",
                            (str(Path(worktree).resolve()),)).fetchone()
                        if row is not None:
                            task = Task(row["repo"], row["plan_id"], connection)
                if task is not None:
                    if current_task.get() is None:
                        stack.callback(current_task.reset, current_task.set(task))
                    runtime = runtime_state.RuntimeStateRepository(task.connection)
                    expected = runtime.get(task.repo, task.plan_id)
                    if request_state and request_state[0] != expected:
                        raise TaskRecheckRequired('DB 재시도 대기 중 작업 입력이 변경됐습니다.')
                    if not request_state:
                        request_state.append(expected)
                    def before_start():
                        if runtime.get(task.repo, task.plan_id) != expected:
                            raise TaskRecheckRequired('호출 대기 중 작업 입력이 변경됐습니다.')
                        if not scheduler._eligible(task, task.connection):
                            raise SchedulingDeferred('호출 대기 중 현재 실행 자격이 변경됐습니다.')
                        if preflight is not None:
                            preflight()
                        if kind == 'verify':
                            observe_runtime()
                    stack.enter_context(scheduler.slot(kind, task, before_start=before_start))
                else:
                    if preflight is not None:
                        preflight()
                    if kind == 'verify':
                        observe_runtime()
                try:
                    result = function(*args, **kwargs)
                    returned.append(result)
                    return result
                except BaseException as error:
                    raised.append(error)
                    raise

        @wraps(function)
        def call(*args, **kwargs):
            from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
            scheduler = current_scheduler.get()
            invocation = kwargs.get('invocation')
            task = current_task.get()
            if task is None and invocation is not None and invocation.plan_id and kwargs.get('connection') is not None:
                task = Task(invocation.repo, invocation.plan_id, kwargs['connection'])
            role = kwargs.get('role_name', args[4] if len(args) > 4 else None)
            returned, raised, request_state = [], [], []
            while True:
                try:
                    result = returned[-1] if returned else call_once(args, kwargs, returned, raised, task, request_state)
                    if scheduler is not None and task is not None and kind == 'agent':
                        identity = getattr(result, 'isolation_invocation_id', None)
                        if identity:
                            row = task.connection.execute('SELECT process_receipt_path FROM agent_invocations '
                                'WHERE invocation_id=?', (identity,)).fetchone()
                            if row is not None and row[0]:
                                from loop_manager_internal.agents.process_receipt import delivered
                                try:
                                    delivered(row[0], scheduler.state_dir, identity)
                                except (OSError, ValueError, TypeError) as error:
                                    events.emit('Agent 결과 전달 표식 저장 실패', level='WARN', invocation_id=identity,
                                                error_type=type(error).__name__)
                                    # Keep the original response and request on this stack;
                                    # an outer task retry would add last_error to its prompt.
                                    scheduler.check_project(task.repo)
                                    with scheduler.condition:
                                        scheduler.condition.wait(.2)
                                    continue
                    return result
                except DatabaseWaitTimeout:
                    if raised and not isinstance(raised[-1], DatabaseWaitTimeout):
                        raise raised[-1]
                    if kind != 'agent' or scheduler is None or (task is None and invocation is None):
                        raise
                    request_scope = task or invocation
                    scheduler.check_project(request_scope.repo)
                    events.emit('Agent 요청 DB 재시도 대기', level='WARN', project=request_scope.repo, plan=request_scope.plan_id)
                    with scheduler.condition:
                        scheduler.condition.wait(.2)
                    try:
                        if task is not None:
                            scheduler.wait_existing_call(task.connection, task.repo, task.plan_id)
                    except DatabaseWaitTimeout:
                        continue
                    if task is not None:
                        scheduler.requeue(task)
                    raised.clear()
                except BaseException as error:
                    if raised and not isinstance(raised[-1], DatabaseWaitTimeout) and raised[-1] is not error:
                        events.emit('Agent 원래 예외 보존; 예약 정리 실패', level='WARN',
                                    error_type=type(error).__name__, original_error_type=type(raised[-1]).__name__)
                        raise raised[-1]
                    raise
        return call
    return decorate


def run_project_tasks(service, *, newly_activated, planning_context=None):
    """프로젝트 준비·planning 이후 독립 실행을 각자의 DB 연결에서 진행한다."""
    from concurrent.futures import Future, wait, FIRST_COMPLETED
    from contextlib import closing
    from dataclasses import replace
    from loop_manager_internal.application.project_service import ProjectService
    from loop_manager_internal.catalog import project_catalog_refresh
    from loop_manager_internal.runtime.contracts import PROJECT_STEP_RETRY, PROJECT_STEP_PLANNING_PENDING
    from loop_manager_internal.application.contracts import PROJECT_STEP_TRANSIENT_PAUSE
    from loop_manager_internal.application.project_runner import retry_pause_detail

    scheduler = current_scheduler.get()
    connection, repo = service.identity.connection, service.state.repo
    # 프로젝트 준비가 끝난 경계에서 TEMP session의 읽기 transaction도 닫는다.
    connection.commit()
    with scheduler.condition:
        scheduler.draining_projects.discard(repo)
    running, threads, visited, results = {}, [], set(), []
    task_results = {}
    rechecked_blocks = set()
    recovery_keys = {}
    setup_database_attempts = Counter()
    setup_exception_attempts = Counter()
    setup_waits, setup_inputs, deferred_inputs = {}, {}, {}
    readmitted = set()
    archive_boundary = True
    archive_retry_token = None
    initial = runtime_state.RuntimeStateRepository(connection).active(repo)

    requested_checks = []
    observed_base = service.state.base_sha
    if planning_context is not None:
        from loop_manager_internal.planning.selection_service import PlanningSelectionService
        from loop_manager_internal.storage import maintenance_state
        planning_ports = service.effects._PlanningLifecyclePorts(
            connection, runtime_state.RuntimeStateRepository(connection),
            maintenance_state.MaintenanceRepository(connection), planning_context, service.identity.planner_mutex)
        selection = PlanningSelectionService(planning_ports)

    def request_check(target_repo):
        if planning_context is None or target_repo != repo:
            return
        checked = Future()
        with scheduler.condition:
            scheduler.check_project(repo)
            requested_checks.append(checked)
            scheduler.condition.notify_all()
            while not checked.done():
                scheduler.check_project(repo)
                scheduler.condition.wait(.2)
            checked.result()

    def check_inputs():
        nonlocal observed_base
        if planning_context is None:
            return
        with scheduler.condition:
            checks = list(requested_checks)
            requested_checks.clear()
        try:
            latest = service.effects.git(service.state.repo_path, 'rev-parse', '--verify',
                f'refs/remotes/origin/{service.state.base}^{{commit}}').stdout.strip()
            if latest != observed_base:
                context = replace(planning_context, base_sha=latest)
                if selection.execution_planning_required(context):
                    scheduler.pause_project(repo)
                    results.append(PROJECT_STEP_RETRY)
                else:
                    service.state.base_sha = service._refresh_catalog()
                observed_base = latest
        except BaseException:
            scheduler.pause_project(repo)
            raise
        finally:
            with scheduler.condition:
                for checked in checks:
                    checked.set_result(None)
                scheduler.condition.notify_all()

    def execute(plan_id, resumed, result):
        try:
            with closing(scheduler.connect_database(service.identity.state_dir, run_migrations=False)) as owned:
                if plan_id in setup_waits:
                    task = Task(repo, plan_id, owned)
                    if setup_waits[plan_id] == 'database':
                        scheduler.wait_database(task)
                    elif not scheduler.wait_recheck(task, setup_inputs[plan_id]):
                        result.set_result(PROJECT_STEP_TRANSIENT_PAUSE)
                        return
                    del setup_waits[plan_id]
                setup_inputs[plan_id] = scheduler.retry_inputs(Task(repo, plan_id, owned))
                from loop_manager_internal.notifications import notifications
                from loop_manager_internal.storage import recovery_state
                notifications.configure_session(owned, enabled=scheduler.config.telegram_enabled,
                    verify_timeout_seconds=getattr(scheduler.config, 'verify_timeout_seconds', None))
                recovery_state.begin_external_run(owned, scheduler.external_run_id)
                owned.commit()
                child = ProjectService(service.effects, replace(service.identity, connection=owned))
                child.state = replace(service.state)
                child.state.catalog_refresh = project_catalog_refresh.ProjectCatalogRefresh(
                    owned, repo,
                    lambda: service.effects.git(child.state.repo_path, "rev-parse", "--verify",
                        f"refs/remotes/origin/{child.state.base}^{{commit}}").stdout.strip(),
                    lambda revision, remember: service.effects.reconcile_execplan_catalog(
                        owned, child.state.repo_path, repo, child.state.base, remote_sha=revision,
                        on_reconciled=remember, state_dir=service.identity.state_dir),
                )
                with scheduler.task(repo, plan_id, owned, resumed=resumed), notifications.immediate_delivery(
                        owned, scheduler.config.telegram_token, scheduler.config.telegram_chat_id, repo):
                    if setup_database_attempts[plan_id] or setup_exception_attempts[plan_id] or plan_id in readmitted:
                        readmitted.discard(plan_id)
                        if current_task.get().retry_order is None:
                            scheduler.requeue(current_task.get())
                        resumed = True
                    attempts = {}
                    pending_result = []
                    exception_attempts = 0
                    database_attempts = 0
                    while True:
                        scheduler.check_project(repo)
                        from loop_manager_internal.storage.management_admission import parked
                        if parked(owned,repo,plan_id):
                            code = PROJECT_STEP_TRANSIENT_PAUSE
                            break
                        scheduler.wait_usage(owned, repo, plan_id)
                        scheduler.wait_existing_call(owned, repo, plan_id)
                        scheduler.wait_retry(owned, repo, plan_id, resume=True)
                        try:
                            if not pending_result:
                                condition_before = scheduler.retry_inputs(current_task.get())
                                before = {str(row['subject']): int(row['generation']) for row in owned.execute(
                                    "SELECT subject,generation FROM recovery_records WHERE repo=?", (repo,))}
                                child.state.row = runtime_state.project_row(owned, repo)
                                code = child._dispatch_execution(
                                    runtime_state.RuntimeStateRepository(owned), newly_activated=not resumed,
                                    planning_context=replace(planning_context, refresh_catalog=child._refresh_catalog)
                                    if planning_context is not None else None)
                                pending_result.append((code, before))
                            code, before = pending_result[0]
                            scheduler.finish_storage(current_task.get(),
                                lambda: notifications.flush_project_events(owned, repo))
                            if code == PROJECT_STEP_RETRY and scheduler.finish_storage(current_task.get(),
                                    lambda: retry_pause_detail(owned, repo, before, attempts, plan_id=plan_id)) is not None:
                                code = PROJECT_STEP_TRANSIENT_PAUSE
                            pending_result.clear()
                        except PlanningDrain:
                            raise
                        except TaskRecheckRequired:
                            owned.rollback()
                            code = PROJECT_STEP_RETRY
                        except Exception as error:
                            owned.rollback()
                            if isinstance(error, SchedulingDeferred) and parked(owned,repo,plan_id):
                                code = PROJECT_STEP_TRANSIENT_PAUSE
                                break
                            from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
                            if isinstance(error, DatabaseWaitTimeout):
                                database_attempts += 1
                                events.emit("DB 접근 대기 종료; 작업 상태 보존", level="WARN", project=repo,
                                            plan=plan_id, reason=str(error))
                                if database_attempts >= 3:
                                    scheduler.wait_database(current_task.get())
                                with scheduler.condition:
                                    scheduler.condition.wait(.2)
                                scheduler.requeue(current_task.get())
                                resumed = True
                                continue
                            exception_attempts += 1
                            detail = redact_secrets(traceback.format_exc())
                            events.emit("작업 처리 예외", level="ERROR", project=repo, plan=plan_id,
                                reason=f"{type(error).__name__}: {error}", traceback=detail, attempts=exception_attempts,
                                next_action="작업 상태 재확인 후 재시도" if exception_attempts < 3 else "해당 작업 보류")
                            runtime = runtime_state.RuntimeStateRepository(owned)
                            if runtime.get(repo, plan_id).status not in runtime_state.ACTIVE_STATUSES and not pending_result:
                                code = 1
                                break
                            scheduler.finish_storage(current_task.get(), lambda: runtime.update_plan(
                                repo, plan_id, plan_values={"last_error": detail}, enqueue=False))
                            if isinstance(error, task_calls.CallDeferred) and scheduler.wait_existing_call(owned, repo, plan_id):
                                code = PROJECT_STEP_RETRY
                            elif isinstance(error, task_calls.CallDeferred):
                                code = PROJECT_STEP_TRANSIENT_PAUSE
                                break
                            elif exception_attempts >= 3:
                                deferred_inputs[plan_id] = (condition_before[0], scheduler.retry_inputs(current_task.get())[1])
                                code = PROJECT_STEP_TRANSIENT_PAUSE
                                break
                            else:
                                before, code = None, PROJECT_STEP_RETRY
                        if code == PROJECT_STEP_PLANNING_PENDING:
                            scheduler.pause_project(repo)
                            raise PlanningDrain()
                        if code == PROJECT_STEP_TRANSIENT_PAUSE:
                            if (scheduler.wait_existing_call(owned, repo, plan_id)
                                    or scheduler.wait_retry(owned, repo, plan_id)):
                                code = PROJECT_STEP_RETRY
                                attempts.clear()
                        from loop_manager_internal.application.contracts import PROJECT_STEP_USAGE_PAUSE
                        if code == PROJECT_STEP_USAGE_PAUSE:
                            if scheduler.wait_usage(owned, repo, plan_id):
                                code = PROJECT_STEP_RETRY
                        if code != PROJECT_STEP_RETRY:
                            break
                        scheduler.check_project(repo)
                        scheduler.requeue(current_task.get())
                        resumed = True
                if code == PROJECT_STEP_TRANSIENT_PAUSE:
                    deferred_inputs.setdefault(plan_id, scheduler.retry_inputs(Task(repo, plan_id, owned)))
            result.set_result(code)
        except PlanningDrain:
            result.set_result(PROJECT_STEP_RETRY)
        except BaseException as error:
            result.set_exception(error)

    def execute_recovery(plan_id, _resumed, result):
        try:
            with closing(scheduler.connect_database(service.identity.state_dir, run_migrations=False)) as owned:
                from loop_manager_internal.notifications import notifications
                from loop_manager_internal.storage import recovery_state
                notifications.configure_session(owned, enabled=scheduler.config.telegram_enabled,
                    verify_timeout_seconds=getattr(scheduler.config, 'verify_timeout_seconds', None))
                recovery_state.begin_external_run(owned, scheduler.external_run_id)
                owned.commit()
                token = current_task.set(Task(repo, plan_id, owned))
                try:
                    with notifications.immediate_delivery(owned, scheduler.config.telegram_token,
                            scheduler.config.telegram_chat_id, repo):
                        service.effects.resume_plan_blocks(owned, service.state.repo_path, service.identity.state_dir,
                            repo, service.state.base, service.identity.config_path,
                            service.identity.verify_timeout_seconds, plan_id=plan_id)
                finally:
                    current_task.reset(token)
            result.set_result(0)
        except PlanningDrain:
            result.set_result(PROJECT_STEP_RETRY)
        except BaseException as error:
            result.set_exception(error)

    def launch(row, resumed, *, recovery_key=None):
        plan_id = str(row["plan_id"])
        result = Future()
        task_results.pop(plan_id, None)
        if plan_id not in setup_inputs:
            setup_inputs[plan_id] = scheduler.retry_inputs(Task(repo, plan_id, connection))
        context = copy_context()
        if planning_context is not None:
            context.run(project_recheck.set, request_check)
        target = execute_recovery if recovery_key is not None else execute
        thread = threading.Thread(target=context.run, args=(target, plan_id, resumed, result),
                                  name=f"loop-task-{plan_id}")
        if recovery_key is not None:
            recovery_keys[plan_id] = recovery_key
        running[plan_id] = result
        visited.add(plan_id)
        threads.append(thread)
        thread.start()

    try:
        while True:
            from loop_manager_internal.runtime.handoff_gate import _check_runtime_handoff
            _check_runtime_handoff()
            check_inputs()
            for plan_id, result in tuple(running.items()):
                if result.done():
                    archive_boundary = True
                    try:
                        task_results[plan_id] = result.result()
                    except Exception as error:
                        events.emit("작업 처리 예외", level="ERROR", project=repo, plan=plan_id,
                            reason=f"{type(error).__name__}: {error}", traceback=traceback.format_exc(),
                            next_action="해당 작업 보존·독립 작업 계속")
                        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
                        if isinstance(error, DatabaseWaitTimeout):
                            setup_database_attempts[plan_id] += 1
                            if setup_database_attempts[plan_id] >= 3:
                                setup_waits[plan_id] = 'database'
                            visited.discard(plan_id)
                            with scheduler.condition:
                                scheduler.condition.wait(.2)
                        else:
                            setup_exception_attempts[plan_id] += 1
                            if setup_exception_attempts[plan_id] >= 3:
                                setup_waits[plan_id] = 'input'
                            visited.discard(plan_id)
                            with scheduler.condition:
                                scheduler.condition.wait(.2)
                    key = recovery_keys.pop(plan_id, None)
                    if key is not None:
                        visited.discard(plan_id)
                    del running[plan_id]
            for plan_id, observed in tuple(deferred_inputs.items()):
                task = Task(repo, plan_id, connection)
                current = scheduler.retry_inputs(task)
                if (plan_id not in running and current != observed
                        and (current[1] is None or current[1][-1] != 'final_block')
                        and scheduler._eligible(task, connection)):
                    visited.discard(plan_id)
                    readmitted.add(plan_id)
                    del deferred_inputs[plan_id]
            from loop_manager_internal.storage import management_admission
            _, management_limit, _ = project_registry.task_limit(connection,repo,scheduler.config.max_tasks_per_project)
            was_parked = {r[0] for r in connection.execute('SELECT plan_id FROM task_management_admissions WHERE repo=? AND parked=1',(repo,))}
            management_admission.reconcile(connection,repo,management_limit,busy=running,
                eligible=lambda pid: scheduler._eligible(Task(repo,pid,connection),connection,check_management=False))
            for pid in was_parked:
                if pid not in running and not management_admission.parked(connection,repo,pid):
                    visited.discard(pid)
            existing = connection.execute("SELECT * FROM exec_plans WHERE repo=? "
                "AND status IN ('IMPLEMENTING','VERIFYING','REVIEWING','PUBLISHING') ORDER BY plan_id", (repo,)).fetchall()
            for row in existing:
                if (repo not in scheduler.draining_projects and str(row["plan_id"]) not in visited
                        and not management_admission.parked(connection,repo,str(row["plan_id"]))):
                    launch(row, not (newly_activated and initial and initial.plan_id == row["plan_id"]))
            stop_admission = repo in scheduler.draining_projects or PROJECT_STEP_PLANNING_PENDING in results
            if not stop_admission:
                retry_token = service._archive_retry_token()
                if archive_boundary or retry_token != archive_retry_token:
                    archive_boundary = False
                    archive_retry_token = retry_token
                    service._archive_scheduling_boundary()
                    # Archive may advance origin by a proven rename. Recheck
                    # planning inputs before launching another execution.
                    check_inputs()
                    if repo in scheduler.draining_projects:
                        continue
                    if service._archive_retry_token() != retry_token:
                        # A MERGED transition during archive must be observed
                        # before admission, not acknowledged as already tried.
                        continue
                _, limit, source = project_registry.task_limit(connection, repo, scheduler.config.max_tasks_per_project)
                if managed_count(connection, repo) < limit:
                    if service.effects.resume_manual_work is not None:
                        before = managed_count(connection, repo)
                        service._resume_manual_work()
                        if managed_count(connection, repo) > before:
                            continue
                    recheck = getattr(service.effects, 'resume_plan_blocks', None)
                    recovered = False
                    if recheck is not None:
                        blocked = connection.execute("SELECT plan_id,current_revision_sha,content_hash,blocked_reason,"
                            "blocked_fingerprint FROM exec_plans WHERE repo=? AND status='PLAN_BLOCKED' ORDER BY plan_id",
                            (repo,)).fetchall()
                        for row in blocked:
                            from loop_manager_internal.storage.planning_review_state import reassessment_key
                            key = (*tuple(row), reassessment_key(connection, repo, str(row['plan_id'])))
                            if key in rechecked_blocks or str(row['plan_id']) in running:
                                continue
                            from loop_manager_internal.recovery.plan_recovery import target_available
                            from loop_manager_internal.runtime.resource_lock import resource_is_held
                            if (not target_available(connection, repo, str(row['plan_id']))
                                    or resource_is_held(service.identity.state_dir, repo, 'plan:' + str(row['plan_id']))
                                    or resource_is_held(service.identity.state_dir, repo, 'git:fetch')):
                                continue
                            rechecked_blocks.add(key)
                            launch(row, True, recovery_key=key)
                            recovered = True
                            break
                    if recovered:
                        continue
                    token = runtime_state.admitting_execution.set(True)
                    try:
                        # 기존 선택기는 dependency·draft·catalog·유지보수 gate와 우선순위를 유지한다.
                        activation = service.effects.activate_ready_execplan(
                            connection, repo, service.identity.state_dir, service.state.base_sha)
                    finally:
                        runtime_state.admitting_execution.reset(token)
                    if activation is not None:
                        events.emit("작업 관리 배정", project=repo, plan=activation['plan_id'],
                            managed=managed_count(connection, repo), limit=limit, source=source)
                        launch(activation, False)
                        archive_boundary = True
                        continue
            if not running:
                break
            wait(tuple(running.values()), timeout=0.2, return_when=FIRST_COMPLETED)
    except PlanningDrain:
        results.append(PROJECT_STEP_RETRY)
    except BaseException:
        scheduler.pause_project(repo)
        raise
    finally:
        for thread in threads:
            thread.join()
        repository = runtime_state.RuntimeStateRepository(connection)
        active = repository.active(repo)
        projected = repository.projected_row(repo)
        projected_id = str(projected['plan_id']) if projected is not None else None
        if active is not None:
            runtime_state.RuntimeStateRepository(connection).hydrate_snapshot(repo, active)
        elif not managed_count(connection, repo) and connection.execute(
                "SELECT 1 FROM task_call_reservations WHERE repo=? UNION ALL "
                "SELECT 1 FROM execution_checkpoints WHERE repo=? AND operation IS NOT NULL "
                "AND (? IS NULL OR plan_id=?) LIMIT 1",
                (repo, repo, projected_id, projected_id)).fetchone() is None:
            runtime_state.RuntimeStateRepository(connection).update_project(
                repo, runtime_state.EXECUTION_CLEAR_VALUES)
    final_results = [*results, *task_results.values()]
    return next((code for code in final_results if code in {PROJECT_STEP_PLANNING_PENDING, PROJECT_STEP_RETRY}),
                next((code for code in final_results if code), 0))
