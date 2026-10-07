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
    """Reserve a sorted resource set without waiting or taking over live work."""
    with ExitStack() as stack:
        for resource in sorted(set(resources)):
            name = resource_lock_name(repo, resource)
            try:
                stack.enter_context(process_lock(state_dir / 'resource-locks',
                    identity=json.dumps(owner, ensure_ascii=True), name=name))
            except ProcessLockBusyError as error:
                try:
                    held = _held_process_lock_identity(state_dir / 'resource-locks', name=name)
                except (RuntimeError, OSError):
                    held = '소유권 확인 중'
                raise ProcessLockBusyError(
                    f'자원 충돌: {resource}; 소유자={held}. 요청은 수락되지 않았습니다.'
                ) from error
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
