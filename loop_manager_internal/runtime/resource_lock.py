"""Process leases for concrete resources, independent of automatic run ownership."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from functools import wraps
import hashlib
import inspect
import json
from pathlib import Path

from .contracts import ProcessLockBusyError
from .process_lock import process_lock, _held_process_lock_identity


def resource_lock_name(repo, resource):
    identity = json.dumps([repo, resource], ensure_ascii=True)
    return hashlib.sha256(identity.encode()).hexdigest() + '.lock'


def resource_is_held(state_dir, repo, resource):
    try:
        _held_process_lock_identity(state_dir / 'resource-locks', name=resource_lock_name(repo, resource))
        return True
    except (FileNotFoundError, RuntimeError):
        return False


@contextmanager
def resource_locks(state_dir: Path, repo: str, resources, *, owner: str):
    """자동 Git 경합은 기다리고 수동 요청·계획 소유권 충돌은 거부한다."""
    from loop_manager_internal.application.task_scheduling import current_scheduler, current_task
    from loop_manager_internal.storage.db_access import DatabaseWaitTimeout, _waiting
    from .handoff_gate import _check_runtime_handoff
    scheduler = current_scheduler.get()
    resources = sorted(set(resources))
    waiting = False
    while True:
        stack = ExitStack()
        try:
            for resource in resources:
                stack.enter_context(process_lock(state_dir / 'resource-locks',
                    identity=json.dumps(owner, ensure_ascii=True), name=resource_lock_name(repo, resource)))
            break
        except ProcessLockBusyError as error:
            stack.close()
            if scheduler is not None and resource.startswith('git:'):
                task = current_task.get()
                if task is not None and (task.connection.in_transaction or getattr(task.connection, '_depth', 0)):
                    raise DatabaseWaitTimeout('Git 자원 대기 전에 DB 처리를 종료해야 합니다.') from error
                _check_runtime_handoff()
                if task is not None:
                    scheduler.check_project(repo)
                if not waiting:
                    from . import operation_events as events
                    events.emit('공유 Git 자원 배정 대기', project=repo, resource=resource, owner=owner)
                    waiting = True
                with _waiting(), scheduler.condition:
                    scheduler.condition.wait(.05)
                continue
            try:
                held = _held_process_lock_identity(state_dir / 'resource-locks', name=resource_lock_name(repo, resource))
            except (RuntimeError, OSError):
                held = '소유권 확인 중'
            raise ProcessLockBusyError(
                f'자원 충돌: {resource}; 소유자={held}. 요청은 수락되지 않았습니다.'
            ) from error
        except BaseException:
            stack.close()
            raise
    with stack:
        if scheduler is not None:
            _check_runtime_handoff()
            # 프로젝트 준비·planning의 Git 접근은 작업 배정 중단 중에도 필요하다.
            if current_task.get() is not None:
                scheduler.check_project(repo)
        yield


def journal_resource(function):
    @wraps(function)
    def guarded(state_dir, *, repo, target, **kwargs):
        with resource_locks(state_dir, repo, ['git:' + target],
                            owner=kwargs.get('owner', 'git-recovery')):
            return function(state_dir, repo=repo, target=target, **kwargs)
    return guarded


def fetch_resource(function):
    signature = inspect.signature(function)
    @wraps(function)
    def guarded(effects, *args, **kwargs):
        arguments = signature.bind(effects, *args, **kwargs).arguments
        repo = arguments.get('repo', arguments.get('repo_path'))
        state = arguments['state_dir']
        with resource_locks(state, str(repo.resolve()), ['git:fetch'], owner='base-fetch'):
            return function(effects, *args, **kwargs)
    return guarded
