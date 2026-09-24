"""Focused process-tree termination tests for verify subprocesses."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import verify_process
from verify_process import (
    _confirm_posix_group_stopped, _linux_group_has_live_members,
    run_verify_process, VerifyTerminationUncertain,
)


class VerifyProcessTests(unittest.TestCase):
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
        with mock.patch('verify_process.os.killpg'), mock.patch(
            'verify_process._linux_group_has_live_members', return_value=True
        ), mock.patch('verify_process.time.monotonic', side_effect=(0, 3)):
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
                    self.assertEqual(result, (code == 0, False, "result\n"))

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
            passed, timed_out, output = run_verify_process(
                [sys.executable, "-c", parent], root, 1,
            )
            self.assertEqual((passed, timed_out), (False, True))
            self.assertIn("progress", output)
            pid = int(child_pid.read_text())
            status = Path(f"/proc/{pid}/stat")
            if status.exists():
                self.assertEqual(status.read_text().split()[2], "Z")

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

    @unittest.skipIf(os.name == "nt", "POSIX process-group assertion")
    def test_unconfirmed_group_kill_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            'verify_process.subprocess.Popen'
        ) as started, mock.patch(
            'verify_process.os.killpg', side_effect=PermissionError('denied')
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

    def _assert_windows_tree_kill_failure(self, root: Path):
        with mock.patch.object(
            verify_process.os, 'name', 'nt'
        ), mock.patch.object(
            subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, create=True
        ), mock.patch(
            'verify_process.subprocess.Popen'
        ) as started, mock.patch(
            'verify_process.subprocess.run'
        ) as taskkill:
            started.return_value.pid = 123
            started.return_value.poll.return_value = 0
            started.return_value.wait.side_effect = subprocess.TimeoutExpired(
                ['verify'], 1,
            )
            taskkill.return_value.returncode = 1
            with self.assertRaisesRegex(VerifyTerminationUncertain, 'taskkill 1'):
                run_verify_process(['verify'], root, 1)
