"""Owned child-to-parent refresh receipts; exit status alone never authorizes restart."""
from contextlib import closing
from contextvars import ContextVar
from dataclasses import asdict
import json
from pathlib import Path
import re

from .contracts import RuntimeRefresh
from .process_lock import _held_process_lock_identity
from loop_manager_internal.storage.db_access import open_connection
from loop_manager_internal.git.manager_git_journal import _write_new


RESTART_EXIT = 75
UNSETTLED_EXIT = 76
channel = ContextVar('runtime_refresh_channel', default=None)


class RuntimePaused(BaseException):
    """A persisted user pause is distinct from a code refresh request."""


class RuntimeDrainUncertain(RuntimeError):
    """Keep the old runtime when a drained task could not confirm its state."""


def paused(state_dir: Path, repo: str | None = None) -> bool:
    # Consume G-236's persisted contract without adding pause control commands.
    path = state_dir / 'pause.db'
    if not path.exists():
        return False
    with closing(open_connection(path, read_only=True, timeout=5)) as connection:
        return connection.execute('SELECT 1 FROM pauses WHERE scope IN (?, ?)',
                                  ('', repo or '')).fetchone() is not None


def receipt_path(state_dir: Path, mutation_id: str, token: str) -> Path:
    if any(re.fullmatch('[0-9a-f]{32}', part) is None for part in (mutation_id, token)):
        raise RuntimeError('runtime 인계 channel identity가 유효하지 않습니다.')
    return state_dir / 'runtime-handoffs' / f'{mutation_id}-{token}.json'


def request_restart(connection, state_dir, runtime, request) -> int:
    owned = channel.get()
    if (runtime is None or request is None or owned is None
            or request.source != str(runtime.source) or request.running_sha != runtime.commit_sha
            or request.mutation_id != runtime.mutation_id):
        raise RuntimeError('runtime 인계의 소유 근거가 없습니다.')
    token, run_id = owned
    if connection.in_transaction:
        raise RuntimeError('runtime 인계 전에 상태 저장을 완료해야 합니다.')
    from loop_manager_internal.storage import task_calls
    task_calls.recover(connection, state_dir)
    if connection.execute('SELECT 1 FROM task_call_reservations WHERE run_id=? LIMIT 1', (run_id,)).fetchone():
        raise RuntimeError('runtime 인계 전에 기존 호출의 종료·저장을 확인해야 합니다.')
    if _held_process_lock_identity(state_dir) != 'runtime:' + runtime.mutation_id:
        raise RuntimeError('runtime 인계 부모 잠금이 다릅니다.')
    path = receipt_path(state_dir, runtime.mutation_id, token)
    if path.exists() or path.with_suffix('.consumed.json').exists():
        raise RuntimeError('이미 사용한 runtime 인계 channel입니다.')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise RuntimeError('runtime 인계 디렉터리가 symlink입니다.')
    _write_new(path, dict(version=1, token=token, run_id=run_id, request=asdict(request)))
    return RESTART_EXIT


def read_request(path, token, source, runtime, run_id) -> RuntimeRefresh:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise RuntimeError('runtime 인계 receipt가 없습니다.')
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        request = RuntimeRefresh(**payload['request'])
    except (ValueError, TypeError, KeyError) as error:
        raise RuntimeError('runtime 인계 receipt 형식이 유효하지 않습니다.') from error
    if (payload.get('version') != 1 or payload.get('token') != token or payload.get('run_id') != run_id
            or request.source != str(source) or request.running_sha != runtime.commit_sha
            or request.mutation_id != runtime.mutation_id
            or not isinstance(request.target_sha, str)
            or re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', request.target_sha) is None
            or request.target_sha == request.running_sha):
        raise RuntimeError('runtime 인계 receipt identity가 다릅니다.')
    return request
