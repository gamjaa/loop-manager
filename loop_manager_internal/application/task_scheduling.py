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


class PlanningDrain(Exception):
    """현재 프로젝트 호출을 비운 뒤 Planner로 돌아간다."""


class TaskRecheckRequired(Exception):
    """대기 중 실행 입력이 바뀌어 같은 작업의 단계를 다시 준비한다."""


@dataclass
class Task:
    repo: str
    plan_id: str
    connection: object
    resumed: bool = True
    retry_order: int | None = None

    def rank(self, priority_reader=None):
        from loop_manager_internal.catalog.effective_priority import effective_priorities
        manual = self.connection.execute(
            "SELECT MIN(r.created_at) FROM manual_work_members m "
            "JOIN manual_work_requests r ON r.repo=m.repo AND r.group_id=m.group_id "
            "JOIN manual_work_groups g ON g.repo=m.repo AND g.group_id=m.group_id "
            "WHERE m.repo=? AND m.plan_id=? AND g.request_id IS NOT NULL",
            (self.repo, self.plan_id),
        ).fetchone()[0]
        if manual is None and self.retry_order is not None:
            return (1, self.retry_order, ())
        if priority_reader is not None:
            ranks=dict(priority_reader(self.connection,self.repo).priority_ranks)
            priority=ranks.get(self.plan_id,(4,int(self.plan_id[2:])))
        else:
            rows = self.connection.execute("SELECT * FROM exec_plans WHERE repo=?", (self.repo,)).fetchall()
            value = effective_priorities(rows).get(self.plan_id)
            priority=value.sort_key(self.plan_id) if value else (4,int(self.plan_id[2:]))
        return (0 if manual is not None else 2 if self.resumed else 3, manual or "",priority)


class TaskScheduler:
    def __init__(self, config, connect_database, state_dir, connection, *, external_run_id=None, priority_reader=None):
        self.config, self.connect_database, self.state_dir = config, connect_database, state_dir
        self.external_run_id = external_run_id
        self.priority_reader = priority_reader
        self.condition = threading.Condition()
        self.waiters = {"agent": [], "verify": [], "management": []}
        self.running = {"agent": Counter(), "verify": Counter(), "management": Counter()}
        self.admitting = False
        self.draining_projects = set()
        self.retry_sequence = 0
        self.limits = {"agent": config.max_workers, "verify": config.verify_concurrency}
        self.uncertain=Counter()
        self.retained={}
        for call in task_calls.recover(connection,state_dir):
            self.running[call['kind']][call['repo']]+=1
            self.uncertain[call['kind']]+=1
            self.retained[(call['repo'],call['plan_id'],call['kind'])]=dict(call)
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
        with self.condition:
            self.retry_sequence += 1
            task.retry_order = self.retry_sequence
            task.resumed = True
            self.condition.notify_all()
        events.emit("작업 RETRY 우선 재배정", project=task.repo, plan=task.plan_id)

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
            live = {row['token'] for row in task_calls.recover(
                connection, self.state_dir, tokens={value['token'] for value in keys.values()})}
            with self.condition:
                for key, value in tuple(keys.items()):
                    if value['token'] not in live:
                        self.retained.pop(key, None)
                        self.running[key[2]][key[0]] -= 1
                        self.uncertain[key[2]] -= 1
                        del keys[key]
                if keys:
                    self.condition.wait(1)
                self.condition.notify_all()
        return True

    @contextmanager
    def admission(self, task):
        """호출과 별개로 새 관리 배정의 선택·확정을 순서대로 수행한다."""
        entry = [task, task.rank(self.priority_reader)]
        count = managed_count(task.connection, task.repo)
        with self.condition:
            self.check_project(task.repo)
            self.running['management'][task.repo] = count
            self.waiters['management'].append(entry)
            try:
                while self.admitting or self._next('management') is not entry:
                    self.check_project(task.repo)
                    self.condition.wait(0.2)
                self.admitting = True
            finally:
                self.waiters['management'].remove(entry)
        try:
            yield
        finally:
            try:
                count = managed_count(task.connection, task.repo)
                with runtime_state._transaction(task.connection):
                    task.connection.execute("INSERT INTO task_scheduling_cursor VALUES ('management',?) "
                        "ON CONFLICT(kind) DO UPDATE SET repo=excluded.repo", (task.repo,))
                with self.condition:
                    self.running['management'][task.repo] = count
                    self.cursor['management']=task.repo
            finally:
                with self.condition:
                    self.admitting = False
                    self.condition.notify_all()

    def _next(self, kind):
        waiting = self.waiters[kind]
        if not waiting:
            return None
        category = min(item[1][0] for item in waiting)
        candidates = [item for item in waiting if item[1][0] == category]
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
    def slot(self, kind, task):
        if task.connection.in_transaction or getattr(task.connection, '_depth', 0):
            raise runtime_state.RuntimeStateError("DB 쓰기 transaction을 유지한 채 호출 배정을 기다릴 수 없습니다.")
        entry = [task, task.rank(self.priority_reader)]
        started = time.monotonic()
        with self.condition:
            self.waiters[kind].append(entry)
            self.condition.notify_all()
        try:
            while True:
                rank = task.rank(self.priority_reader)
                with self.condition:
                    entry[1] = rank
                    self.check_project(task.repo)
                    if ((task.repo,task.plan_id,kind) in self.retained
                            or self.uncertain[kind]>=self.limits[kind]):
                        raise task_calls.CallDeferred("기존 호출의 종료가 확인되지 않아 실제 실행 자리를 보존합니다.")
                    if sum(self.running[kind].values()) < self.limits[kind] and self._next(kind) is entry:
                        allocation = ("retry" if entry[1][0] == 1 else "manual" if entry[1][0] == 0 else "resume" if entry[1][0] == 2
                                      else "basic" if not self.running[kind][task.repo] else "priority")
                        self.running[kind][task.repo] += 1
                        break
                    self.condition.wait(0.2)
        finally:
            with self.condition:
                self.waiters[kind].remove(entry)
                self.condition.notify_all()
        try:
            with runtime_state._transaction(task.connection):
                owner = task_calls.reserve(task.connection, self.state_dir, task.repo,
                                           task.plan_id, kind, self.external_run_id)
                task.connection.execute(
                    "INSERT INTO task_scheduling_cursor VALUES (?,?) "
                    "ON CONFLICT(kind) DO UPDATE SET repo=excluded.repo", (kind, task.repo))
            with self.condition:
                self.cursor[kind] = task.repo
        except BaseException:
            with self.condition:
                self.running[kind][task.repo] -= 1
                self.condition.notify_all()
            raise
        events.emit("작업 호출 배정", kind=kind, project=task.repo, plan=task.plan_id,
                    running=sum(self.running[kind].values()), limit=self.limits[kind],
                    waiting_seconds=round(time.monotonic()-started, 3), allocation=allocation,
                    waiting=len(self.waiters[kind]))
        uncertain = True
        owner_token=task_calls.current_call.set(owner)
        try:
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
                    with self.condition:
                        if released:
                            self.running[kind][task.repo] -= 1
                        else:
                            self.uncertain[kind] += 1
                            self.retained[(task.repo, task.plan_id, kind)] = {"token": owner.token}
                        task.resumed = True
                        task.retry_order = None
                        self.condition.notify_all()
                finally:
                    task_calls.current_call.reset(owner_token)

    @contextmanager
    def task(self, repo, plan_id, connection, *, resumed=True):
        token = current_task.set(Task(repo, plan_id, connection, resumed))
        try:
            from loop_manager_internal.runtime.resource_lock import resource_locks
            with resource_locks(self.state_dir, repo, ['plan:' + plan_id], owner='task-execution'), \
                 runtime_state.selected_execution(repo, plan_id):
                yield
        finally:
            current_task.reset(token)


def scheduled_call(kind):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            observe_runtime()
            scheduler, task = current_scheduler.get(), current_task.get()
            role = kwargs.get("role_name", args[4] if len(args) > 4 else None)
            preflight = current_verify_preflight.get() if kind == "verify" else None
            if scheduler is None:
                if preflight is not None:
                    preflight()
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
                    runtime = runtime_state.RuntimeStateRepository(task.connection)
                    expected = runtime.get(task.repo, task.plan_id)
                    stack.enter_context(scheduler.slot(kind, task))
                    from loop_manager_internal.recovery import scoped_blocking
                    from loop_manager_internal.storage import execplan_draft_state
                    if runtime.get(task.repo, task.plan_id) != expected:
                        raise TaskRecheckRequired('호출 대기 중 작업 입력이 변경됐습니다.')
                    if (scoped_blocking.project_block(task.connection, task.repo) is not None
                            or task.plan_id in scoped_blocking.catalog_held_plan_ids(task.connection, task.repo)
                            or task.plan_id in execplan_draft_state.pending_ids(task.connection, task.repo)):
                        raise task_calls.CallDeferred('호출 대기 중 차단·catalog/draft 보류가 확인됐습니다.')
                if preflight is not None:
                    preflight()
                observe_runtime()
                return function(*args, **kwargs)
        return call
    return decorate


def run_project_tasks(service, *, newly_activated):
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
    rechecked_blocks = set()
    recovery_keys = {}
    initial = runtime_state.RuntimeStateRepository(connection).active(repo)

    def execute(plan_id, resumed, result):
        try:
            with closing(scheduler.connect_database(service.identity.state_dir, run_migrations=False)) as owned:
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
                        on_reconciled=remember),
                )
                with scheduler.task(repo, plan_id, owned, resumed=resumed), notifications.immediate_delivery(
                        owned, scheduler.config.telegram_token, scheduler.config.telegram_chat_id, repo):
                    attempts = {}
                    exception_attempts = 0
                    while True:
                        scheduler.check_project(repo)
                        scheduler.wait_usage(owned, repo, plan_id)
                        scheduler.wait_existing_call(owned, repo, plan_id)
                        scheduler.wait_retry(owned, repo, plan_id, resume=True)
                        try:
                            before = {str(row['subject']): int(row['generation']) for row in owned.execute(
                                "SELECT subject,generation FROM recovery_records WHERE repo=?", (repo,))}
                            child.state.row = runtime_state.project_row(owned, repo)
                            code = child._dispatch_execution(
                                runtime_state.RuntimeStateRepository(owned), newly_activated=not resumed)
                            notifications.flush_project_events(owned, repo)
                            if code == PROJECT_STEP_RETRY and retry_pause_detail(
                                    owned, repo, before, attempts, plan_id=plan_id) is not None:
                                code = PROJECT_STEP_TRANSIENT_PAUSE
                        except PlanningDrain:
                            raise
                        except TaskRecheckRequired:
                            owned.rollback()
                            code = PROJECT_STEP_RETRY
                        except Exception as error:
                            owned.rollback()
                            from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
                            if isinstance(error, DatabaseWaitTimeout):
                                events.emit("DB 접근 대기 종료; 작업 상태 보존", level="WARN", project=repo,
                                            plan=plan_id, reason=str(error))
                                code = PROJECT_STEP_TRANSIENT_PAUSE
                                break
                            exception_attempts += 1
                            detail = redact_secrets(traceback.format_exc())
                            events.emit("작업 처리 예외", level="ERROR", project=repo, plan=plan_id,
                                reason=f"{type(error).__name__}: {error}", traceback=detail, attempts=exception_attempts,
                                next_action="작업 상태 재확인 후 재시도" if exception_attempts < 3 else "해당 작업 보류")
                            runtime = runtime_state.RuntimeStateRepository(owned)
                            if runtime.get(repo, plan_id).status not in runtime_state.ACTIVE_STATUSES:
                                code = 1
                                break
                            runtime.update_plan(repo, plan_id, plan_values={"last_error": detail}, enqueue=False)
                            if isinstance(error, task_calls.CallDeferred) and scheduler.wait_existing_call(owned, repo, plan_id):
                                code = PROJECT_STEP_RETRY
                            elif isinstance(error, task_calls.CallDeferred) or exception_attempts >= 3:
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
                        observe_runtime()
                        scheduler.requeue(current_task.get())
                        resumed = True
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
        context = copy_context()
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
            for plan_id, result in tuple(running.items()):
                if result.done():
                    try:
                        results.append(result.result())
                    except Exception as error:
                        events.emit("작업 처리 예외", level="ERROR", project=repo, plan=plan_id,
                            reason=f"{type(error).__name__}: {error}", traceback=traceback.format_exc(),
                            next_action="해당 작업 보존·독립 작업 계속")
                        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
                        results.append(PROJECT_STEP_TRANSIENT_PAUSE if isinstance(error, DatabaseWaitTimeout) else 1)
                    key = recovery_keys.pop(plan_id, None)
                    if key is not None:
                        visited.discard(plan_id)
                        _, limit, _ = project_registry.task_limit(connection, repo, scheduler.config.max_tasks_per_project)
                        if (managed_count(connection, repo) >= limit
                                and runtime_state.RuntimeStateRepository(connection).get(repo, plan_id).status == 'PLAN_BLOCKED'):
                            rechecked_blocks.discard(key)
                    del running[plan_id]
            existing = connection.execute("SELECT * FROM exec_plans WHERE repo=? "
                "AND status IN ('IMPLEMENTING','VERIFYING','REVIEWING','PUBLISHING') ORDER BY plan_id", (repo,)).fetchall()
            for row in existing:
                if repo not in scheduler.draining_projects and str(row["plan_id"]) not in visited:
                    launch(row, not (newly_activated and initial and initial.plan_id == row["plan_id"]))
            stop_admission = repo in scheduler.draining_projects or PROJECT_STEP_PLANNING_PENDING in results
            if not stop_admission:
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
                            key = tuple(row)
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
                        continue
            if not running:
                break
            wait(tuple(running.values()), timeout=0.2, return_when=FIRST_COMPLETED)
    except PlanningDrain:
        results.append(PROJECT_STEP_RETRY)
    finally:
        for thread in threads:
            thread.join()
        active = runtime_state.RuntimeStateRepository(connection).active(repo)
        if active is not None:
            runtime_state.RuntimeStateRepository(connection).hydrate_snapshot(repo, active)
        elif not managed_count(connection, repo) and connection.execute(
                "SELECT 1 FROM task_call_reservations WHERE repo=? UNION ALL "
                "SELECT 1 FROM execution_checkpoints WHERE repo=? AND operation IS NOT NULL LIMIT 1",
                (repo, repo)).fetchone() is None:
            runtime_state.RuntimeStateRepository(connection).update_project(
                repo, runtime_state.EXECUTION_CLEAR_VALUES)
    return next((code for code in results if code in {PROJECT_STEP_PLANNING_PENDING, PROJECT_STEP_RETRY}),
                next((code for code in results if code), 0))
