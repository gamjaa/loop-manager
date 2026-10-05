"""Focused process-tree termination tests for verify subprocesses."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from loop_manager_internal.execution import verify_process
from loop_manager_internal.execution import windows_job
from loop_manager_internal.execution.verify_process import (
    _confirm_posix_group_stopped, _linux_group_has_live_members,
    _stop_linux_tree, _wait_linux_process_stopped,
    run_verify_process, VerifyTerminationUncertain,
)


class VerifyProcessTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux stop state assertion")
    def test_freeze_waits_for_stop_before_final_descendant_scan(self):
        events = []

        def descendants(_root_pid):
            events.append("scan")
            return (
                {101: 1} if events.count("scan") == 1
                else {101: 1, 102: 2}
            )

        with mock.patch("loop_manager_internal.execution.verify_process.os.killpg") as group_signal, mock.patch(
            "loop_manager_internal.execution.verify_process._linux_process_info", return_value=(1, 50, "R", 3)
        ), mock.patch(
            "loop_manager_internal.execution.verify_process._linux_descendants", side_effect=descendants
        ), mock.patch(
            "loop_manager_internal.execution.verify_process._signal_linux_process",
            side_effect=lambda pid, _started, _action: events.append(f"stop {pid}"),
        ), mock.patch(
            "loop_manager_internal.execution.verify_process._wait_linux_process_stopped",
            side_effect=lambda pid, _started, _deadline, **_kwargs: events.append(f"wait {pid}"),
        ):
            self.assertEqual(_stop_linux_tree(50), {101: 1, 102: 2})
        group_signal.assert_called_once_with(50, verify_process.signal.SIGSTOP)
        self.assertEqual(
            events,
            ["wait 50", "scan", "stop 101", "wait 101", "scan", "stop 102", "wait 102", "scan"],
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux stop state assertion")
    def test_child_that_never_stops_fails_closed(self):
        with mock.patch(
            "loop_manager_internal.execution.verify_process._linux_process_info", return_value=(1, 50, "R", 3)
        ), mock.patch("loop_manager_internal.execution.verify_process.time.monotonic", return_value=2):
            with self.assertRaisesRegex(VerifyTerminationUncertain, "PID 101"):
                _wait_linux_process_stopped(101, 3, 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper assertion")
    def test_exited_subreaper_cannot_confirm_orphan_shutdown(self):
        with mock.patch(
            "loop_manager_internal.execution.verify_process._linux_process_info", return_value=(1, 50, "Z", 3)
        ):
            with self.assertRaisesRegex(VerifyTerminationUncertain, "PID 50"):
                _wait_linux_process_stopped(50, 3, 1, required=True)

    def test_linux_group_check_ignores_zombies_but_detects_running_children(self):
        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            process = proc_root / '123'
            process.mkdir()
            stat = process / 'stat'
            stat.write_text('123 (test child) Z 1 456 456 0\n', encoding='ascii')
            self.assertFalse(_linux_group_has_live_members(456, proc_root))
            stat.write_text('123 (test child) R 1 456 456 0\n', encoding='ascii')
            self.assertTrue(_linux_group_has_live_members(456, proc_root))

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_live_group_after_kill_fails_closed(self):
        with mock.patch('loop_manager_internal.execution.verify_process.os.killpg'), mock.patch(
            'loop_manager_internal.execution.verify_process._linux_group_has_live_members', return_value=True
        ), mock.patch('loop_manager_internal.execution.verify_process.time.monotonic', side_effect=(0, 3)):
            with self.assertRaisesRegex(VerifyTerminationUncertain, 'PGID 456'):
                _confirm_posix_group_stopped(456)

    def test_success_and_failure_preserve_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for code in (0, 3):
                with self.subTest(code=code):
                    result = run_verify_process(
                        [sys.executable, "-c", f"print('result'); raise SystemExit({code})"],
                        root, 2,
                    )
                    self.assertEqual(result, (code == 0, False, "result" + os.linesep))

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_timeout_kills_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid = root / "child.pid"
            parent = (
                "import subprocess,sys,pathlib; "
                "child=subprocess.Popen([sys.executable,'-c','while True: pass']); "
                f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
                "print('progress',flush=True); "
                "child.wait()"
            )
            try:
                passed, timed_out, output = run_verify_process(
                    [sys.executable, "-c", parent], root, 1,
                )
                self.assertEqual((passed, timed_out), (False, True))
                self.assertIn("progress", output)
                pid = int(child_pid.read_text())
                status = Path(f"/proc/{pid}/stat")
                if status.exists():
                    self.assertEqual(status.read_text().split()[2], "Z")
            finally:
                if child_pid.exists():
                    try:
                        os.kill(int(child_pid.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux /proc assertion")
    def test_timeout_kills_child_in_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid = root / "detached.pid"
            parent = (
                "import subprocess,sys,pathlib; "
                "child=subprocess.Popen([sys.executable,'-c','while True: pass'], "
                "start_new_session=True); "
                f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
                "print('detached child started',flush=True); child.wait()"
            )
            try:
                passed, timed_out, output = run_verify_process(
                    [sys.executable, "-c", parent], root, 1,
                )
                self.assertEqual((passed, timed_out), (False, True))
                self.assertIn("detached child started", output)
                pid = int(child_pid.read_text())
                status = Path(f"/proc/{pid}/stat")
                if status.exists():
                    self.assertEqual(status.read_text().split()[2], "Z")
            finally:
                if child_pid.exists():
                    try:
                        os.kill(int(child_pid.read_text()), 9)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper assertion")
    def test_timeout_kills_orphaned_grandchild_in_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grandchild_pid = root / "orphan.pid"
            intermediate = (
                "import subprocess,sys,pathlib; "
                "grandchild=subprocess.Popen([sys.executable,'-c','while True: pass'], "
                "start_new_session=True); "
                f"pathlib.Path({str(grandchild_pid)!r}).write_text(str(grandchild.pid))"
            )
            verify = (
                "import subprocess,sys; "
                f"subprocess.run([sys.executable,'-c',{intermediate!r}],check=True); "
                "print('intermediate exited',flush=True)"
            )
            try:
                passed, timed_out, output = run_verify_process(
                    [sys.executable, "-c", verify], root, 1,
                )
                self.assertEqual((passed, timed_out), (False, True))
                self.assertIn("intermediate exited", output)
                pid = int(grandchild_pid.read_text())
                status = Path(f"/proc/{pid}/stat")
                if status.exists():
                    self.assertEqual(status.read_text().split()[2], "Z")
            finally:
                if grandchild_pid.exists():
                    try:
                        os.kill(int(grandchild_pid.read_text()), 9)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-stop race assertion")
    def test_fork_during_stop_is_discovered_and_killed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid = root / "child.pid"
            ready = root / "ready"
            grandchild_pid = root / "grandchild.pid"
            child = (
                "import pathlib,signal,subprocess,sys,time\n"
                "def fork_on_signal(*_):\n"
                "    grandchild=subprocess.Popen([sys.executable,'-c','while True: pass'], "
                "start_new_session=True)\n"
                f"    pathlib.Path({str(grandchild_pid)!r}).write_text(str(grandchild.pid))\n"
                "signal.signal(signal.SIGUSR1, fork_on_signal)\n"
                f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
                "time.sleep(30)\n"
            )
            verify = (
                "import pathlib,subprocess,sys; "
                f"child=subprocess.Popen([sys.executable,'-c',{child!r}], "
                "start_new_session=True); "
                f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid)); "
                "print('child started',flush=True); child.wait()"
            )
            original_signal = verify_process._signal_linux_process

            def wait_for(path):
                deadline = time.monotonic() + 2
                while not path.exists():
                    if time.monotonic() >= deadline:
                        self.fail(f"process did not create {path.name}")
                    time.sleep(0.01)

            def fork_before_stop(pid, started, action):
                if action == signal.SIGSTOP and child_pid.exists() and pid == int(child_pid.read_text()):
                    wait_for(ready)
                    os.kill(pid, signal.SIGUSR1)
                    wait_for(grandchild_pid)
                original_signal(pid, started, action)

            try:
                with mock.patch(
                    "loop_manager_internal.execution.verify_process._signal_linux_process", side_effect=fork_before_stop,
                ):
                    passed, timed_out, output = run_verify_process(
                        [sys.executable, "-c", verify], root, 1,
                    )
                self.assertEqual((passed, timed_out), (False, True))
                self.assertIn("child started", output)
                pid = int(grandchild_pid.read_text())
                status = Path(f"/proc/{pid}/stat")
                if status.exists():
                    self.assertEqual(status.read_text().split()[2], "Z")
            finally:
                for path in (child_pid, grandchild_pid):
                    if path.exists():
                        try:
                            os.kill(int(path.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_unconfirmed_group_kill_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            'loop_manager_internal.execution.verify_process.subprocess.Popen'
        ) as started, mock.patch(
            'loop_manager_internal.execution.verify_process.os.killpg', side_effect=PermissionError('denied')
        ):
            started.return_value.pid = 123
            started.return_value.wait.side_effect = subprocess.TimeoutExpired(
                ['verify'], 1,
            )
            with self.assertRaisesRegex(VerifyTerminationUncertain, 'PGID 123'):
                run_verify_process(['verify'], Path(directory), 1)

    def test_windows_tree_kill_failure_is_unsafe_even_if_parent_exited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._assert_windows_tree_kill_failure(root)

    def test_windows_timeout_cleanup_preserves_timeout_and_attempts_every_boundary(self):
        root = Path.cwd()
        process, job = mock.Mock(pid=123), mock.Mock()
        timeout = subprocess.TimeoutExpired(["verify"], 1)
        process.poll.return_value = None
        process.wait.side_effect = [timeout, OSError("wait failed")]
        job.terminate.side_effect = OSError("terminate failed")
        job.wait.side_effect = TimeoutError("child active")
        with mock.patch.object(verify_process.os, "name", "nt"), mock.patch.object(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True
        ), mock.patch.object(windows_job, "WindowsJob", return_value=job):
            with self.assertRaisesRegex(VerifyTerminationUncertain, "verify 시간 초과") as raised:
                run_verify_process(
                    ["verify"], root, 1,
                    launch_process=mock.Mock(return_value=process), job_scope=True,
                )
        self.assertIs(raised.exception.__cause__, timeout)
        self.assertIn("terminate failed", str(raised.exception))
        self.assertIn("wait failed", str(raised.exception))
        self.assertIn("child active", str(raised.exception))
        job.terminate.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        job.wait.assert_called_once_with(10)
        job.close.assert_called_once()

    def test_windows_failure_cleans_job_even_after_parent_exit(self):
        root = Path.cwd()
        process, job = mock.Mock(pid=123), mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        primary = OSError("job accounting failed")
        job.wait.side_effect = [primary, TimeoutError("child active")]
        job.terminate.side_effect = OSError("terminate failed")
        with mock.patch.object(verify_process.os, "name", "nt"), mock.patch.object(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True
        ), mock.patch.object(windows_job, "WindowsJob", return_value=job):
            with self.assertRaisesRegex(VerifyTerminationUncertain, "job accounting failed") as raised:
                run_verify_process(
                    ["verify"], root, 1,
                    launch_process=mock.Mock(return_value=process), job_scope=True,
                )
        self.assertIs(raised.exception.__cause__, primary)
        self.assertIn("terminate failed", str(raised.exception))
        self.assertIn("child active", str(raised.exception))
        job.terminate.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(job.wait.call_count, 2)
        job.close.assert_called_once()

    def test_cleanup_uncertainty_keeps_the_primary_failure(self):
        root = Path.cwd()
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        primary = RuntimeError("started callback failed")
        cleanup = VerifyTerminationUncertain("child cleanup uncertain")

        def fail_start(*_args):
            raise primary

        with mock.patch(
            "loop_manager_internal.execution.verify_process.subprocess.Popen", return_value=process,
        ), mock.patch(
            "loop_manager_internal.execution.verify_process.terminate_verify_process", side_effect=cleanup,
        ):
            with self.assertRaisesRegex(VerifyTerminationUncertain, "started callback failed") as raised:
                run_verify_process(["verify"], root, 1, on_started=fail_start)
        self.assertIs(raised.exception.__cause__, primary)
        self.assertIn("child cleanup uncertain", str(raised.exception))

    def _assert_windows_tree_kill_failure(self, root: Path):
        with mock.patch.object(
            verify_process.os, 'name', 'nt'
        ), mock.patch.object(
            subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, create=True
        ), mock.patch(
            'loop_manager_internal.execution.verify_process.subprocess.Popen'
        ) as started, mock.patch(
            'loop_manager_internal.execution.verify_process.subprocess.run'
        ) as taskkill:
            started.return_value.pid = 123
            started.return_value.poll.return_value = 0
            started.return_value.wait.side_effect = subprocess.TimeoutExpired(
                ['verify'], 1,
            )
            taskkill.return_value.returncode = 1
            with self.assertRaisesRegex(VerifyTerminationUncertain, 'taskkill 1'):
                run_verify_process(['verify'], root, 1)
