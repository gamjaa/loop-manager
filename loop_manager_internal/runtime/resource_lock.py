"""Process leases for concrete resources, independent of automatic run ownership."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
import os
from pathlib import Path
from threading import get_ident

from .contracts import ProcessLockBusyError
from .process_lock import process_lock, _held_process_lock_identity


_resource_leases = ContextVar('resource_leases', default=())


def resource_owned(state_dir, repo, resource, *, owner=None):
    """Reuse only an open lease acquired by this process and thread's scope."""
    key = (str(Path(state_dir).resolve()), repo, resource, os.getpid(), get_ident())
    return any(identity == key and not handle.closed and (owner is None or owner == held_owner)
               for identity, handle, held_owner in _resource_leases.get())


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
def _git_turn(scheduler, repo, owner):
    """One FIFO turn per project's Git scope; no model or verify slot is reserved."""
    from loop_manager_internal.storage.db_access import DatabaseWaitTimeout, _waiting
    from loop_manager_internal.application.task_scheduling import current_task
    from .handoff_gate import _check_runtime_handoff
    task = current_task.get()
    if task is not None and (task.connection.in_transaction or getattr(task.connection, '_depth', 0)):
        raise DatabaseWaitTimeout('Git 순번 대기 전에 DB 처리를 종료해야 합니다.')
    ticket = object()
    with scheduler.condition:
        queue = scheduler.git_waiters.setdefault(repo, [])
        queue.append(ticket)
        scheduler.condition.notify_all()
    waiting = False
    try:
        while True:
            _check_runtime_handoff()
            if task is not None and owner != 'base-fetch':
                scheduler.check_project(repo)
            with scheduler.condition:
                if queue[0] is ticket:
                    break
                if not waiting:
                    from . import operation_events as events
                    events.emit('공유 Git 자원 배정 대기', project=repo,
                                resource='git:project', owner=owner, reason='fifo')
                    waiting = True
                with _waiting():
                    scheduler.condition.wait(.05)
        yield
    finally:
        with scheduler.condition:
            queue.remove(ticket)
            if not queue:
                del scheduler.git_waiters[repo]
            scheduler.condition.notify_all()


@contextmanager
def resource_locks(state_dir: Path, repo: str, resources, *, owner: str, wait: bool = True):
    """자동 Git scope는 프로젝트 FIFO, 외부·구체 자원은 기존 잠금으로 보호한다."""
    from loop_manager_internal.application.task_scheduling import current_scheduler, current_task
    from loop_manager_internal.storage.db_access import DatabaseWaitTimeout, _waiting
    from .handoff_gate import _check_runtime_handoff
    scheduler = current_scheduler.get()
    resources = sorted(set(resources))
    shared = any(resource.startswith('git:') for resource in resources)
    reentrant = shared and resource_owned(state_dir, repo, 'git:project')
    if shared:
        resources = [resource for resource in resources if resource != 'git:project']
        if not reentrant:
            resources.insert(0, 'git:project')
    with ExitStack() as turn:
        if shared and not reentrant and scheduler is not None:
            turn.enter_context(_git_turn(scheduler, repo, owner))
        waiting = False
        while True:
            stack = ExitStack()
            leases = []
            try:
                for resource in resources:
                    handle = stack.enter_context(process_lock(state_dir / 'resource-locks',
                        identity=json.dumps(owner, ensure_ascii=True), name=resource_lock_name(repo, resource)))
                    leases.append(((str(state_dir.resolve()), repo, resource, os.getpid(), get_ident()), handle, owner))
                break
            except ProcessLockBusyError as error:
                stack.close()
                if wait and scheduler is not None and resource.startswith('git:'):
                    task = current_task.get()
                    if task is not None and (task.connection.in_transaction or getattr(task.connection, '_depth', 0)):
                        raise DatabaseWaitTimeout('Git 자원 대기 전에 DB 처리를 종료해야 합니다.') from error
                    _check_runtime_handoff()
                    if task is not None and owner != 'base-fetch':
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
                # A base fetch is itself a prerequisite for planning, so a drain
                # must not reject it while it waits for or acquires the fetch lock.
                if current_task.get() is not None and owner != 'base-fetch':
                    scheduler.check_project(repo)
            token = _resource_leases.set((*_resource_leases.get(), *leases))
            try:
                yield
            finally:
                _resource_leases.reset(token)


@contextmanager
def git_command_turn(repo, command, arguments):
    """Cover direct Git writes outside an existing journal/resource scope."""
    from loop_manager_internal.application.task_scheduling import current_scheduler, current_task
    scheduler, task = current_scheduler.get(), current_task.get()
    writes = command in {'fetch', 'add', 'commit', 'push', 'rebase', 'checkout', 'switch',
                         'reset', 'restore', 'clean', 'update-index', 'update-ref', 'merge',
                         'cherry-pick', 'revert', 'branch', 'tag', 'worktree', 'stash', 'config', 'symbolic-ref'}
    if command == 'symbolic-ref' and len([arg for arg in arguments if not arg.startswith('-')]) < 2:
        writes = '--delete' in arguments
    if command == 'config' and any(arg in {'--get', '--get-all', '--get-regexp', '--get-urlmatch',
                                         '--list', '-l'} for arg in arguments):
        writes = False
    mutation_flags = {'-d', '-D', '--delete', '-m', '-M', '--move', '-c', '-C', '--copy',
                      '-f', '--force', '--set-upstream-to', '--unset-upstream'}
    if command in {'branch', 'tag'} and not any(
            arg.split('=', 1)[0] in mutation_flags for arg in arguments) and (not arguments or any(
            arg in {'--list', '--show-current', '--contains', '--merged', '--no-merged', '-a', '-r'}
            for arg in arguments)):
        writes = False
    if command in {'worktree', 'stash'} and arguments[:1] in {('list',), ('show',)}:
        writes = False
    if scheduler is None or not writes:
        yield
        return
    # Detached publication worktrees inherit their registered repository's turn.
    held = next((identity[1] for identity, handle, _owner in _resource_leases.get()
                 if identity[3:] == (os.getpid(), get_ident())
                 and identity[2] == 'git:project' and not handle.closed), None)
    if held is not None:
        yield
        return
    registered = task.repo if task is not None else str(repo.resolve())
    with resource_locks(scheduler.state_dir, registered, ['git:command'], owner='git-command'):
        yield


def journal_resource(function):
    @wraps(function)
    def guarded(state_dir, *, repo, target, **kwargs):
        if (kwargs.get('kind') in {'managed_worktree_create', 'managed_worktree_remove'}
                and resource_owned(state_dir, repo, 'git:' + target, owner='workspace-reservation-recovery')
                and (kwargs.get('owner') is None or resource_owned(state_dir, repo, kwargs['owner']))):
            return function(state_dir, repo=repo, target=target, **kwargs)
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
        if resource_owned(state, str(repo.resolve()), 'git:fetch', owner='execplan-archive'):
            return function(effects, *args, **kwargs)
        with resource_locks(state, str(repo.resolve()), ['git:fetch'], owner='base-fetch'):
            return function(effects, *args, **kwargs)
    return guarded
