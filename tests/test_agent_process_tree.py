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

from loop_manager_internal.agents.agent_codex_sandbox import CodexLaunch
from loop_manager_internal.execution.verify_process import _linux_process_info


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux subreaper boundary')
class AgentProcessTreeTests(unittest.TestCase):
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
