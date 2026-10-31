"""Real Linux effects for Agent descendants that escape the provider group."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import threading

from loop_manager_internal.agents.agent_codex_sandbox import CodexLaunch
from loop_manager_internal.execution.verify_process import _linux_process_info


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux subreaper boundary')
class AgentProcessTreeTests(unittest.TestCase):
    def test_concurrent_stop_waits_for_the_same_descendant_confirmation(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        CodexLaunch.start(['provider'], launch_process=lambda *a, **kw: process)
        entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
        def stop_tree(_process):
            if terminate.call_count == 1:
                entered.set()
                self.assertTrue(release.wait(3))
        with mock.patch('loop_manager_internal.execution.verify_process.terminate_verify_process', side_effect=stop_tree) as terminate:
            first = threading.Thread(target=CodexLaunch.stop, args=(process,))
            second = threading.Thread(target=lambda: (CodexLaunch.stop(process), second_done.set()))
            first.start()
            self.assertTrue(entered.wait(3))
            second.start()
            try:
                self.assertFalse(second_done.wait(.1))
            finally:
                release.set()
                first.join(3)
                second.join(3)
            self.assertTrue(second_done.is_set())
            terminate.assert_called_once_with(process)

    def test_failed_tree_stop_cannot_become_finished_when_supervisor_exits(self):
        process = mock.Mock(pid=4321)
        process.poll.return_value = None
        CodexLaunch.start(['provider'], launch_process=lambda *a, **kw: process)
        with mock.patch('loop_manager_internal.execution.verify_process.terminate_verify_process', side_effect=RuntimeError('child remains')) as terminate, \
             mock.patch('os.killpg'), mock.patch('loop_manager_internal.execution.verify_process._confirm_posix_group_stopped'):
            with self.assertRaisesRegex(RuntimeError, 'child remains'):
                CodexLaunch.stop(process)
            process.poll.return_value = 0
            with self.assertRaisesRegex(RuntimeError, 'child remains'):
                CodexLaunch.stop(process)
            terminate.assert_called_once_with(process)

    def test_provider_exit_keeps_escaped_child_owned_until_tree_stop(self):
        child = None
        process = CodexLaunch.start([sys.executable, '-c',
            'import json,os,subprocess,sys; '
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"], '
            'start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
            'print(json.dumps([os.getpid(),p.pid]),flush=True)'],
            launch_process=subprocess.Popen, stdout=subprocess.PIPE, text=True)
        process.stdin.close()
        try:
            provider, child = json.loads(process.stdout.readline())
            deadline = time.monotonic() + 2
            while (info := _linux_process_info(provider)) is not None and info[2] not in {'Z', 'X'}:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.assertIsNone(process.poll(), 'A live escaped child must retain its supervisor')
            CodexLaunch.stop(process)
            process.wait(timeout=3)
            info = _linux_process_info(child)
            self.assertTrue(info is None or info[2] in {'Z', 'X'})
        finally:
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            process.stdout.close()

    def test_supervisor_preserves_provider_stdin_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = root / 'call-one'
            attempt.mkdir()
            target = root / 'input.txt'
            launch = CodexLaunch((), (), {}, root, attempt, '{}')
            result = launch.run([sys.executable, '-c',
                'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text(sys.stdin.read());sys.exit(7)',
                str(target)], launch_process=subprocess.Popen, input='original prompt', timeout=5, text=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(target.read_text(), 'original prompt')
