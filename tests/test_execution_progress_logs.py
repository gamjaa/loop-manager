import io
import errno
import os
import datetime
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.runtime import process_lock as process_lock_module


class ExecutionProgressLogTest(unittest.TestCase):
    def test_operation_log_uses_fixed_local_timestamp_for_both_streams(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        fixed = datetime.datetime(2026, 9, 19, 8, 7, 6)
        with mock.patch("loop_manager.local_now", return_value=fixed), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            loop_manager.operation_log("started")
            loop_manager.operation_log("failed", error=True)
        prefix = r"^2026-09-19 08:07:06[+-]\d{2}:\d{2} "
        self.assertRegex(stdout.getvalue(), prefix + r"INFO \[run=- project=- plan=- stage=-\] started\n$")
        self.assertRegex(stderr.getvalue(), prefix + r"ERROR \[run=- project=- plan=- stage=-\] failed\n$")

    def test_skip_if_running_only_silences_lock_contention(self):
        state_dir = Path("state")
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run", "--skip-if-running"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock",
            side_effect=loop_manager.ProcessLockBusyError("busy"),
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 0)
        output.assert_not_called()
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock",
            side_effect=loop_manager.ProcessLockBusyError("busy"),
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 1)
        self.assertTrue(output.called)
        with mock.patch(
            "sys.argv", ["loop_manager.py", "run", "--skip-if-running"]
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ), mock.patch(
            "loop_manager.process_lock", side_effect=RuntimeError("broken")
        ), mock.patch("builtins.print") as output:
            self.assertEqual(loop_manager.main(), 1)
        self.assertTrue(output.called)

    def test_process_lock_only_classifies_contention_errnos_as_busy(self):
        for platform, lock_module in (("posix", "fcntl"), ("nt", "msvcrt")):
            for error_number, expected in (
                (errno.EAGAIN, loop_manager.ProcessLockBusyError),
                (errno.ENOLCK, OSError),
            ):
                with self.subTest(platform=platform, errno=error_number):
                    error = OSError(error_number, "lock failed")
                    error.winerror = 0
                    lock = mock.Mock(side_effect=error)
                    handle = mock.Mock()
                    fake_module = (
                        types.SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lock)
                        if lock_module == "fcntl"
                        else types.SimpleNamespace(LK_NBLCK=1, locking=lock)
                    )
                    with mock.patch.object(process_lock_module.os, "name", platform), mock.patch.object(
                        process_lock_module.os, "open", return_value=10
                    ), mock.patch.object(
                        process_lock_module.os, "fdopen", return_value=handle
                    ), mock.patch.dict(sys.modules, {lock_module: fake_module}):
                        with self.assertRaises(expected) as raised:
                            with process_lock_module.process_lock(mock.MagicMock()):
                                pass
                    if expected is OSError:
                        self.assertEqual(raised.exception.errno, errno.ENOLCK)
                    handle.close.assert_called_once()

    def test_process_lock_closes_handle_when_unlock_fails_on_each_platform(self):
        for platform, lock_module in (("posix", "fcntl"), ("nt", "msvcrt")):
            with self.subTest(platform=platform):
                state_dir = mock.MagicMock()
                handle = mock.Mock()
                handle.fileno.return_value = 10
                entered = mock.Mock()
                lock = mock.Mock(side_effect=[None, OSError("unlock failed")])
                fake_msvcrt = types.SimpleNamespace(
                    LK_NBLCK=1, LK_UNLCK=2, locking=lock,
                )
                fake_fcntl = types.SimpleNamespace(
                    LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=lock,
                )
                patches = [
                    mock.patch.object(process_lock_module.os, "name", platform),
                    mock.patch.object(process_lock_module.os, "open", return_value=10),
                    mock.patch.object(process_lock_module.os, "fdopen", return_value=handle),
                    mock.patch.dict(sys.modules, {lock_module: fake_fcntl if lock_module == "fcntl" else fake_msvcrt}),
                ]
                with patches[0], patches[1], patches[2], patches[3], mock.patch.object(
                    process_lock_module.os, "fsync"
                ) as fsync:
                    with self.assertRaisesRegex(OSError, "unlock failed"):
                        with process_lock_module.process_lock(state_dir) as acquired:
                            entered(acquired)
                entered.assert_called_once_with(handle)
                fsync.assert_called_once_with(10)
                handle.close.assert_called_once()
                self.assertEqual(lock.call_args_list, [
                    mock.call(10, 1, 1), mock.call(10, 2, 1),
                ] if platform == "nt" else [
                    mock.call(10, 3), mock.call(10, 4),
                ])

    @unittest.skipIf(os.name == "nt", "POSIX 셸 실행 fixture")
    def test_bash_launcher_skips_git_and_preserves_run_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "run.sh"
            launcher.write_text((loop_manager.ROOT / "run.sh").read_text(encoding="utf-8"), encoding="utf-8")
            python = root / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/usr/bin/env bash\nprintf 'python:%s\\n' \"$*\"\n", encoding="utf-8")
            python.chmod(0o755)
            git = root / "bin/git"
            git.parent.mkdir()
            git.write_text("#!/usr/bin/env bash\nprintf 'git-called:%s\\n' \"$*\"\nexit 99\n", encoding="utf-8")
            git.chmod(0o755)
            env = {**os.environ, "PATH": f"{git.parent}:{os.environ['PATH']}"}
            completed = subprocess.run(["bash", str(launcher), "target-repo"], cwd=root, env=env, text=True, capture_output=True)
            self.assertEqual(completed.returncode, 0)
            self.assertNotIn("git-called:", completed.stdout)
            self.assertIn("python:-u", completed.stdout)
            self.assertIn("run target-repo", completed.stdout)

    def test_launchers_have_no_timestamp_banner_or_git_execution(self):
        powershell = (loop_manager.ROOT / "run.ps1").read_text(encoding="utf-8")
        bash = (loop_manager.ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("Get-Date -Format", powershell)
        self.assertNotIn("date ", bash)
        self.assertNotIn("git ", powershell.casefold())
        self.assertNotIn("git ", bash.casefold())
