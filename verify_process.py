"""Bounded verify subprocess with a confirmed process-tree shutdown."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class VerifyTerminationUncertain(RuntimeError):
    """The process tree may still be running; no further work is safe."""


_LINUX_SUBREAPER = """\
import ctypes
import os
import subprocess
import sys

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_CHILD_SUBREAPER')
verify = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)
code = verify.wait()
while True:
    try:
        os.waitpid(-1, 0)
    except ChildProcessError:
        break
sys.exit(code)
"""


def _tail(stream, limit: int = 8000) -> str:
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - limit))
    return stream.read().decode("utf-8", errors="replace")


def _linux_group_has_live_members(pgid: int, proc_root: Path = Path("/proc")) -> bool:
    """Zombies hold a PGID briefly but cannot consume CPU or run again."""

    found = False
    try:
        with os.scandir(proc_root) as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    stat = (Path(entry.path) / "stat").read_text(encoding="ascii")
                except FileNotFoundError:
                    continue
                end = stat.rfind(")")
                fields = stat[end + 2:].split() if end >= 0 else []
                if len(fields) < 3 or not fields[2].isdecimal():
                    raise VerifyTerminationUncertain(
                        f"verify process group 상태 해석 실패 (PGID {pgid})"
                    )
                if int(fields[2]) == pgid:
                    found = True
                    if fields[0] not in {"Z", "X"}:
                        return True
    except (OSError, UnicodeError) as error:
        raise VerifyTerminationUncertain(
            f"verify process group 상태 확인 실패 (PGID {pgid}): {error}"
        ) from error
    # killpg reported a group, so an invisible member is uncertain.
    return not found


def _confirm_posix_group_stopped(pgid: int) -> None:
    deadline = time.monotonic() + 2
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except OSError as error:
            raise VerifyTerminationUncertain(
                f"verify process group 종료 상태 불명 (PGID {pgid}): {error}"
            ) from error
        if sys.platform.startswith("linux") and not _linux_group_has_live_members(pgid):
            return
        if time.monotonic() >= deadline:
            raise VerifyTerminationUncertain(
                f"verify process group 잔존 (PGID {pgid})"
            )
        time.sleep(0.05)


def _linux_process_info(pid: int) -> tuple[int, int, str, int] | None:
    """Return (parent, group, state, start time) for one process identity."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise VerifyTerminationUncertain(
            f"verify 자식 상태 확인 실패 (PID {pid}): {error}"
        ) from error
    end = stat.rfind(")")
    fields = stat[end + 2:].split() if end >= 0 else []
    if len(fields) < 20:
        raise VerifyTerminationUncertain(f"verify 자식 상태 해석 실패 (PID {pid})")
    try:
        return int(fields[1]), int(fields[2]), fields[0], int(fields[19])
    except ValueError as error:
        raise VerifyTerminationUncertain(
            f"verify 자식 상태 해석 실패 (PID {pid})"
        ) from error


def _linux_descendants(root_pid: int) -> dict[int, int]:
    """Snapshot descendants, including children that created a new session."""

    processes = {}
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if entry.name.isdecimal():
                    info = _linux_process_info(int(entry.name))
                    if info is not None:
                        processes[int(entry.name)] = info
    except OSError as error:
        raise VerifyTerminationUncertain(
            f"verify 자식 목록 확인 실패 (PID {root_pid}): {error}"
        ) from error
    if root_pid not in processes:
        raise VerifyTerminationUncertain(
            f"verify 부모 종료 전 자식 추적 실패 (PID {root_pid})"
        )
    descendants = {}
    parents = {root_pid}
    while parents:
        children = {
            pid for pid, (parent, _, _, _) in processes.items()
            if parent in parents and pid not in descendants and pid != root_pid
        }
        for pid in children:
            descendants[pid] = processes[pid][3]
        parents = children
    return descendants


def _signal_linux_process(pid: int, start_time: int, action: int) -> None:
    info = _linux_process_info(pid)
    if info is None or info[3] != start_time or info[2] in {"Z", "X"}:
        return
    try:
        os.kill(pid, action)
    except ProcessLookupError:
        return
    except OSError as error:
        raise VerifyTerminationUncertain(
            f"verify 자식 종료 실패 (PID {pid}): {error}"
        ) from error


def _confirm_linux_descendants_stopped(descendants: dict[int, int]) -> None:
    deadline = time.monotonic() + 2
    while True:
        live = [
            pid for pid, started in descendants.items()
            if (info := _linux_process_info(pid)) is not None
            and info[3] == started and info[2] not in {"Z", "X"}
        ]
        if not live:
            return
        if time.monotonic() >= deadline:
            raise VerifyTerminationUncertain(
                f"verify 분리된 자식 잔존 (PID {live[0]})"
            )
        time.sleep(0.05)


def run_verify_process(
    arguments: list[str], worktree: Path, timeout_seconds: int,
) -> tuple[bool, bool, str]:
    """Return (passed, timed_out, bounded output), or fail closed on live descendants."""

    with tempfile.TemporaryFile() as output:
        command = (
            [sys.executable, "-c", _LINUX_SUBREAPER, *arguments]
            if sys.platform.startswith("linux") and os.name != "nt"
            else arguments
        )
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        process = subprocess.Popen(
            command, cwd=worktree, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, **options,
        )
        try:
            code = process.wait(timeout=timeout_seconds)
            output.seek(0)
            return code == 0, False, output.read().decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            descendants = {}
            if os.name == "nt":
                try:
                    killed = subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=10, check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise VerifyTerminationUncertain(
                        f"verify process tree 종료 확인 실패 (PID {process.pid}): {error}"
                    ) from error
                if killed.returncode:
                    raise VerifyTerminationUncertain(
                        f"verify process tree 종료 실패 (PID {process.pid}, taskkill {killed.returncode})"
                    )
            else:
                if sys.platform.startswith("linux"):
                    try:
                        os.killpg(process.pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        raise VerifyTerminationUncertain(
                            f"verify 자식 추적 전 process group 소실 (PGID {process.pid})"
                        ) from None
                    except OSError as error:
                        raise VerifyTerminationUncertain(
                            f"verify process group 정지 실패 (PGID {process.pid}): {error}"
                        ) from error
                    try:
                        deadline = time.monotonic() + 2
                        while True:
                            discovered = _linux_descendants(process.pid)
                            new = {
                                pid: started for pid, started in discovered.items()
                                if pid not in descendants
                            }
                            if not new:
                                break
                            descendants.update(new)
                            for pid, started in new.items():
                                _signal_linux_process(pid, started, signal.SIGSTOP)
                            if time.monotonic() >= deadline:
                                raise VerifyTerminationUncertain(
                                    f"verify 자식 추적 상한 초과 (PGID {process.pid})"
                                )
                    except Exception:
                        os.killpg(process.pid, signal.SIGKILL)
                        for pid, started in descendants.items():
                            _signal_linux_process(pid, started, signal.SIGKILL)
                        raise
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    raise VerifyTerminationUncertain(
                        f"verify process group 종료 실패 (PGID {process.pid}): {error}"
                    ) from error
                for pid, started in descendants.items():
                    _signal_linux_process(pid, started, signal.SIGKILL)
            try:
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise VerifyTerminationUncertain(
                    f"verify process 종료 확인 실패 (PID {process.pid})"
                ) from error
            if os.name != "nt":
                _confirm_posix_group_stopped(process.pid)
                if descendants:
                    _confirm_linux_descendants_stopped(descendants)
            return False, True, _tail(output)
