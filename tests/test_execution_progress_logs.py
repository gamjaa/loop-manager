import io
import errno
import os
import datetime
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.runtime import process_lock as process_lock_module


class TimestampedExecutionProgressLogsTest(unittest.TestCase):
    def test_operation_log_uses_fixed_local_timestamp_for_both_streams(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("loop_manager.local_now", return_value=datetime.datetime(2026, 9, 19, 8, 7, 6)), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            loop_manager.operation_log("started")
            loop_manager.operation_log("failed", error=True)
        self.assertEqual(stdout.getvalue(), "2026-09-19 08:07:06 started\n")
        self.assertEqual(stderr.getvalue(), "2026-09-19 08:07:06 failed\n")

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
        target = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch(target, side_effect=OSError(errno.EAGAIN, "busy")):
                with self.assertRaises(loop_manager.ProcessLockBusyError):
                    with process_lock_module.process_lock(Path(directory)):
                        pass
            with mock.patch(target, side_effect=OSError(errno.ENOLCK, "lock system failure")):
                with self.assertRaises(OSError) as raised:
                    with process_lock_module.process_lock(Path(directory)):
                        pass

        self.assertEqual(raised.exception.errno, errno.ENOLCK)

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

    def test_launchers_have_no_timestamp_banner_and_scheduler_option_is_documented(self):
        powershell = (loop_manager.ROOT / "run.ps1").read_text(encoding="utf-8")
        bash = (loop_manager.ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("Get-Date -Format", powershell)
        self.assertNotIn("date ", bash)
        self.assertNotIn("git ", powershell.casefold())
        self.assertNotIn("git ", bash.casefold())
        self.assertIn("--skip-if-running", (loop_manager.ROOT / "README.md").read_text(encoding="utf-8"))
