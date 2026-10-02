"""User state location and the process lease held across runtime handoff."""
from __future__ import annotations

import contextlib
import errno
import os
from pathlib import Path

from .contracts import ProcessLockBusyError


def state_directory(value: str | None = None) -> Path:

    if value:
        return Path(value).expanduser().resolve()
    if configured := os.environ.get("LOOP_MANAGER_STATE_DIR"):
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        parent = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return parent / "loop-manager"


def is_lock_contention(error: OSError) -> bool:
    """Return whether a non-blocking lock failure means another process holds it."""

    return error.errno in {errno.EACCES, errno.EAGAIN} or (
        os.name == "nt" and error.winerror in {32, 33}
    )


@contextlib.contextmanager
def process_lock(state_dir: Path, *, identity: str = "0"):
    # ponytail: 부트스트랩은 전역 직렬화한다. 실제 병렬 처리 도입 시 프로젝트별 잠금으로 교체한다.
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "loop-manager.lock"
    handle = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        if is_lock_contention(error):
            raise ProcessLockBusyError("다른 Loop Manager 프로세스가 실행 중입니다.") from error
        raise
    try:
        _write_process_lock_identity(handle, identity)
        yield handle
    finally:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _write_process_lock_identity(handle, identity: str) -> None:
    handle.seek(0)
    handle.write(identity.encode("ascii"))
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())


def _held_process_lock_identity(state_dir: Path) -> str:
    """Read the parent lease only after proving another process still holds it."""
    with (state_dir / "loop-manager.lock").open("r+b") as handle:
        identity = handle.read(128).decode("ascii", errors="replace")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if is_lock_contention(error):
                return identity
            raise
        else:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    raise RuntimeError("runtime 부모 process lock이 유지되지 않습니다.")
