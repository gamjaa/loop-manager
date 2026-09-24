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


def run_verify_process(
    arguments: list[str], worktree: Path, timeout_seconds: int,
) -> tuple[bool, bool, str]:
    """Return (passed, timed_out, bounded output), or fail closed on live descendants."""

    with tempfile.TemporaryFile() as output:
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        process = subprocess.Popen(
            arguments, cwd=worktree, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, **options,
        )
        try:
            code = process.wait(timeout=timeout_seconds)
            output.seek(0)
            return code == 0, False, output.read().decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
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
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    raise VerifyTerminationUncertain(
                        f"verify process group 종료 실패 (PGID {process.pid}): {error}"
                    ) from error
            try:
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise VerifyTerminationUncertain(
                    f"verify process 종료 확인 실패 (PID {process.pid})"
                ) from error
            if os.name != "nt":
                _confirm_posix_group_stopped(process.pid)
            return False, True, _tail(output)
