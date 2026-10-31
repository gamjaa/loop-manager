"""Bounded verify subprocess with a confirmed process-tree shutdown."""

from __future__ import annotations

from collections.abc import Callable
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class VerifyTerminationUncertain(RuntimeError):
    """The process tree may still be running; no further work is safe."""


class VerifyProcessOutput(str):
    """Captured verify output with the subprocess's observed exit code."""

    exit_code: int | None

    def __new__(cls, output: str, *, exit_code: int | None) -> VerifyProcessOutput:
        instance = super().__new__(cls, output)
        instance.exit_code = exit_code
        return instance


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
                except (FileNotFoundError, ProcessLookupError):
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
    except (FileNotFoundError, ProcessLookupError):
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


def _wait_linux_process_stopped(
    pid: int, start_time: int, deadline: float, *, required: bool = False,
) -> None:
    while True:
        info = _linux_process_info(pid)
        if info is None or info[3] != start_time:
            if required:
                raise VerifyTerminationUncertain(
                    f"verify 부모 정지 전 소실 (PID {pid})"
                )
            return
        if required and info[2] in {"Z", "X"}:
            raise VerifyTerminationUncertain(
                f"verify 부모 종료로 자식 추적 불가 (PID {pid})"
            )
        if info[2] in {"T", "t", "Z", "X"}:
            return
        if time.monotonic() >= deadline:
            raise VerifyTerminationUncertain(
                f"verify 자식 정지 확인 실패 (PID {pid})"
            )
        time.sleep(0.05)


def _stop_linux_tree(root_pid: int) -> dict[int, int]:
    """Freeze every observed descendant before the final ancestry snapshot."""

    try:
        os.killpg(root_pid, signal.SIGSTOP)
    except ProcessLookupError:
        raise VerifyTerminationUncertain(
            f"verify 자식 추적 전 process group 소실 (PGID {root_pid})"
        ) from None
    except OSError as error:
        raise VerifyTerminationUncertain(
            f"verify process group 정지 실패 (PGID {root_pid}): {error}"
        ) from error

    descendants: dict[int, int] = {}
    try:
        root = _linux_process_info(root_pid)
        if root is None:
            raise VerifyTerminationUncertain(
                f"verify 부모 상태 확인 실패 (PID {root_pid})"
            )
        deadline = time.monotonic() + 2
        _wait_linux_process_stopped(
            root_pid, root[3], deadline, required=True,
        )
        while True:
            discovered = _linux_descendants(root_pid)
            new = {
                pid: started for pid, started in discovered.items()
                if descendants.get(pid) != started
            }
            if not new:
                return descendants
            descendants.update(new)
            for pid, started in new.items():
                _signal_linux_process(pid, started, signal.SIGSTOP)
            for pid, started in new.items():
                _wait_linux_process_stopped(pid, started, deadline)
            if time.monotonic() >= deadline:
                raise VerifyTerminationUncertain(
                    f"verify 자식 추적 상한 초과 (PGID {root_pid})"
                )
    except Exception as primary:
        cleanup_errors = []
        try:
            os.killpg(root_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as error:
            cleanup_errors.append(str(error))
        for pid, started in descendants.items():
            try:
                _signal_linux_process(pid, started, signal.SIGKILL)
            except Exception as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            raise VerifyTerminationUncertain(f"{primary}; 자식 정리 오류: " + "; ".join(cleanup_errors)) from primary
        raise


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


def terminate_verify_process(process) -> None:
    """확인된 모든 정리 경계를 시도하고 종료 불명을 보존한다."""
    errors, descendants = [], {}
    if os.name == "nt":
        try:
            killed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10, check=False,
            )
            if killed.returncode:
                errors.append(f"taskkill {killed.returncode}")
        except (OSError, subprocess.TimeoutExpired) as error:
            errors.append(str(error))
    else:
        if sys.platform.startswith("linux"):
            try:
                descendants = _stop_linux_tree(process.pid)
            except Exception as error:
                errors.append(str(error))
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(str(error))
        for pid, started in descendants.items():
            try:
                _signal_linux_process(pid, started, signal.SIGKILL)
            except Exception as error:
                errors.append(str(error))
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        errors.append(str(error))
    if os.name != "nt":
        for confirm in (lambda: _confirm_posix_group_stopped(process.pid),
                        lambda: _confirm_linux_descendants_stopped(descendants)):
            try:
                confirm()
            except Exception as error:
                errors.append(str(error))
    if errors:
        raise VerifyTerminationUncertain(f"verify 자식 종료 불명 (PID {process.pid}): " + "; ".join(errors))


def _terminate_windows_job(job, process) -> list[str]:
    """Try every owned Windows cleanup boundary before reporting uncertainty."""

    errors = []
    for cleanup in (job.terminate, lambda: process.wait(timeout=10), lambda: job.wait(10)):
        try:
            cleanup()
        except Exception as error:
            errors.append(str(error))
    return errors


_WINDOWS_VERIFY_BOOTSTRAP = """\\
import subprocess, sys
if sys.stdin.readline().strip() != 'start':
    raise SystemExit(2)
sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))
"""


def run_verify_process(
    arguments: list[str], worktree: Path, timeout_seconds: int | float | None,
    *,
    launch_process: Callable[..., subprocess.Popen] | None = None,
    environment: dict[str, str] | None = None,
    job_scope: bool = False,
    on_started: Callable[..., None] | None = None,
    execution_clock=None,
    on_wait: Callable[[], None] | None = None,
    on_stopped: Callable[[], None] | None = None,
    on_output: Callable[[str], None] | None = None,
) -> tuple[bool, bool, str]:
    """Confirm owned descendants; None inherits an enclosing Manager deadline."""
    job = None
    assigned = False
    stopped = False
    process = None
    from contextlib import nullcontext
    from loop_manager_internal.verification.execution_clock import ExecutionClock, current_execution_clock, BUDGET_DIRECTORY
    inherited_clock = execution_clock or current_execution_clock.get()
    with (nullcontext(inherited_clock) if inherited_clock is not None else ExecutionClock(environment)) as budget, tempfile.TemporaryFile() as output:
        active_clock = budget
        command = (
            [sys.executable, "-c", _LINUX_SUBREAPER, *arguments]
            if sys.platform.startswith("linux") and os.name != "nt"
            else arguments
        )
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        if os.name == "nt" and job_scope:
            from loop_manager_internal.execution.windows_job import WindowsJob
            job = WindowsJob()
            command = [sys.executable, "-c", _WINDOWS_VERIFY_BOOTSTRAP, *arguments]
        options["env"] = dict(os.environ if environment is None else environment)
        options["env"][BUDGET_DIRECTORY] = str(budget.directory)
        from loop_manager_internal.storage import task_calls
        receipt=None
        def confirmed_stop():
            nonlocal stopped
            try:
                budget.require_stopped()
            except VerifyTerminationUncertain:
                task_calls.verify_waiting_for_wsl(receipt,budget.directory)
                raise
            task_calls.verify_stopped(receipt)
            stopped = True
            if on_stopped:
                on_stopped()
        try:
            receipt=task_calls.prepare_verify_receipt()
            process = (launch_process or subprocess.Popen)(
                command, cwd=worktree,
                stdin=subprocess.PIPE if job else subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, **options,
            )
            budget.active_processes += 1
            task_calls.verify_started(receipt,process)
            if job:
                job.assign(process)
                assigned = True
            if on_started:
                on_started(process, job)
            if job:
                process.stdin.write(b"start\n")
                process.stdin.close()
            while True:
                if on_wait:
                    on_wait()
                remaining = active_clock.remaining(timeout_seconds)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("verify actual execution timeout")
                try:
                    code = process.wait(timeout=min(0.1, remaining) if on_wait and remaining is not None
                                        else 0.1 if on_wait else remaining)
                    break
                except subprocess.TimeoutExpired:
                    if on_wait:
                        continue
                    if (active_clock.remaining(timeout_seconds) <= 0
                            or active_clock.waiting() <= active_clock.initial_wait):
                        raise
            if job:
                job.wait(None if timeout_seconds is None else max(0.001, active_clock.remaining(timeout_seconds)))
            elif sys.platform.startswith("linux") and code != 0:
                _confirm_posix_group_stopped(process.pid)
            confirmed_stop()
            remaining = active_clock.remaining(timeout_seconds)
            timed_out = remaining is not None and remaining <= 0
            output.seek(0)
            return code == 0 and not timed_out, timed_out, VerifyProcessOutput(
                output.read().decode("utf-8", errors="replace"), exit_code=code,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as primary:
            if job and assigned:
                errors = _terminate_windows_job(job, process)
                if errors:
                    raise VerifyTerminationUncertain(
                        f"verify 시간 초과 ({primary}); Windows verify 자식 종료 불명: "
                        + "; ".join(errors)
                    ) from primary
            else:
                try:
                    terminate_verify_process(process)
                except VerifyTerminationUncertain as cleanup_error:
                    raise VerifyTerminationUncertain(
                        f"verify 시간 초과 ({primary}); {cleanup_error}"
                    ) from primary
            confirmed_stop()
            return False, True, VerifyProcessOutput(_tail(output), exit_code=process.returncode)
        except BaseException as primary:
            if not stopped and process is not None and (assigned or process.poll() is None):
                if job and assigned:
                    errors = _terminate_windows_job(job, process)
                    if errors:
                        raise VerifyTerminationUncertain(
                            f"verify 실패 ({primary}); Windows verify 취소 뒤 종료 불명: "
                            + "; ".join(errors)
                        ) from primary
                else:
                    try:
                        terminate_verify_process(process)
                    except VerifyTerminationUncertain as cleanup_error:
                        raise VerifyTerminationUncertain(
                            f"verify 실패 ({primary}); {cleanup_error}"
                        ) from primary
            if not stopped and not isinstance(primary,VerifyTerminationUncertain):
                confirmed_stop()
            raise
        finally:
            primary = sys.exc_info()[1] if on_output else None
            try:
                if on_output:
                    try:
                        on_output(_tail(output))
                    except Exception as error:
                        if primary is None:
                            raise
                        primary.add_note(f"verify output preservation failed: {error}")
            finally:
                if process is not None:
                    budget.active_processes -= 1
                if job:
                    job.close()
