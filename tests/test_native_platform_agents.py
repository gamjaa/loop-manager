"""실제 모델 호출 없이 OS별 Codex 격리·실행 경계를 검사한다."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.agents import agent_codex_sandbox as envelope
from loop_manager_internal.agents import agent_sandbox_exec as probe
from loop_manager_internal.agents import agent_write_isolation as isolation
from loop_manager_internal.agents import provider_adapters
from tests.support.workspace import native_agent_boundary


class NativePlatformAgentTests(unittest.TestCase):
    def test_windows_bootstrap_cannot_hide_real_agent_from_test_guard(self):
        from loop_manager_internal.agents import windows_runtime
        with mock.patch.dict(os.environ, {'LOOP_MANAGER_TEST_NO_AGENTS': '1'}), \
             mock.patch.object(windows_runtime, 'WindowsJob') as job:
            with self.assertRaisesRegex(RuntimeError, '실제 Agent'):
                windows_runtime.start(['codex', 'exec', '-'], mock.Mock())
            job.assert_not_called()

    def test_windows_assignment_failure_never_releases_agent_start_gate(self):
        from loop_manager_internal.agents import windows_runtime
        job, process = mock.Mock(), mock.Mock()
        job.assign.side_effect = OSError('assignment failed')
        with mock.patch.object(windows_runtime, 'WindowsJob', return_value=job):
            with self.assertRaises(OSError):
                windows_runtime.start(['fake'], mock.Mock(return_value=process), text=True)
        process.stdin.write.assert_not_called()
        process.kill.assert_called_once()
        job.close.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_windows_shared_runtime_directory_is_rejected(self):
        from loop_manager_internal.agents.windows_runtime import assert_private_directory
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'private'
            root.mkdir(mode=0o700)
            assert_private_directory(root)
            subprocess.run(['icacls', str(root), '/grant', '*S-1-1-0:R'],
                           check=True, capture_output=True, timeout=10)
            with self.assertRaisesRegex(RuntimeError, 'ACL'):
                assert_private_directory(root)

    def test_prepare_preserves_host_credentials_and_uses_native_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state, home = native_agent_boundary(Path(directory))
            for read_only in (False, True):
                launch = envelope.prepare_launch(boundary, state, 'worker', read_only,
                    isolation.codex_arguments(read_only), {'CODEX_HOME': str(home)})
                spec = json.loads(launch.specification)
                self.assertIn(str(home), spec['protected'])
                self.assertEqual(spec['writable'], [] if read_only else [str(launch.attempt), str(boundary.worktree)])
                self.assertEqual((home / 'auth.json').read_text(), 'private-auth-placeholder')
                if os.name == 'nt':
                    self.assertEqual(launch.environment['CODEX_HOME'], str(home))
                    self.assertEqual(launch.environment['TMP'], str(launch.attempt))
                    self.assertIn('windows.sandbox="elevated"', launch.arguments)
                    self.assertIn('windows.sandbox="elevated"', launch.probe_arguments)
                else:
                    self.assertTrue((Path(launch.environment['CODEX_HOME']) / 'auth.json').is_symlink())

    def test_supported_platforms_require_successful_probe_for_fresh_and_resumed_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary, state, home = native_agent_boundary(Path(directory))
            for system in ('Linux', 'Darwin', 'Windows'):
                for session in (None, 'saved-session'):
                    for failure in (None, RuntimeError('probe denied')):
                        records = []
                        launch = mock.Mock(arguments=('policy',), probe_arguments=('probe',))
                        launch.probe.side_effect = failure
                        with self.subTest(system=system, session=session, failure=failure), \
                             mock.patch.object(isolation.platform, 'system', return_value=system), \
                             mock.patch.object(isolation, 'inspect_worktree', return_value=boundary), \
                             mock.patch.object(isolation, '_configuration_digest', return_value='config'), \
                             mock.patch.object(isolation, '_assert_no_codex_execpolicy'), \
                             mock.patch.object(isolation, '_assert_no_codex_extensions'), \
                             mock.patch.object(isolation, 'prepare_launch', return_value=launch), \
                             mock.patch.object(isolation.subprocess, 'run', return_value=SimpleNamespace(
                                 returncode=0, stdout='codex-cli 0.159.3')):
                            def prepare():
                                return isolation.prepare_isolation('codex', 'worker', boundary.worktree, state,
                                    False, records.append, lambda: 'snapshot',
                                    expected_repository=str(boundary.source), session_id=session,
                                    environment_variables={'CODEX_HOME': str(home)})
                            if failure:
                                with self.assertRaises(isolation.IsolationError):
                                    prepare()
                            else:
                                self.assertIs(prepare(), launch)
                            self.assertEqual(records[0].status, 'rejected' if failure else 'verified')
                            launch.probe.assert_called_once()

    def test_non_linux_directory_checks_never_attempt_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {'protected': [str(root)], 'files': [], 'writable': []}
            for platform in ('win32', 'darwin'):
                function = '_windows_readonly_directory' if platform == 'win32' else '_darwin_readonly_path'
                with self.subTest(platform=platform), mock.patch.object(probe.sys, 'platform', platform), \
                     mock.patch.object(probe, function) as check, \
                     mock.patch.object(probe.tempfile, 'mkstemp', side_effect=AssertionError('mutation')):
                    probe.check_boundary(spec)
                    check.assert_called_once_with(root)
                    check.side_effect = probe.BoundaryError('denial not confirmed')
                    with self.assertRaises(probe.BoundaryError):
                        probe.check_boundary(spec)
                    self.assertEqual(list(root.iterdir()), [])

    def test_windows_directory_probe_checks_each_right_without_writes(self):
        kernel = mock.Mock()
        kernel.CreateFileW.return_value = ctypes.c_void_p(-1).value
        with mock.patch.object(probe.ctypes, 'WinDLL', return_value=kernel, create=True), \
             mock.patch.object(probe.ctypes, 'get_last_error', return_value=5, create=True) as error:
            probe._windows_readonly_directory(Path('protected'))
            self.assertEqual(kernel.CreateFileW.call_count, 5)
            for call in kernel.CreateFileW.call_args_list:
                self.assertEqual(call.args[4], 3)
            error.return_value = 32
            with self.assertRaisesRegex(probe.BoundaryError, 'inconclusive'):
                probe._windows_readonly_directory(Path('protected'))
            kernel.CreateFileW.return_value = 123
            with self.assertRaisesRegex(probe.BoundaryError, 'write was allowed'):
                probe._windows_readonly_directory(Path('protected'))
            kernel.CloseHandle.assert_called_once_with(123)

    def test_darwin_directory_probe_rejects_allowed_and_unknown_results(self):
        library = mock.Mock()
        with mock.patch.object(probe.ctypes, 'CDLL', return_value=library), \
             mock.patch.object(probe.ctypes.c_int, 'in_dll', return_value=SimpleNamespace(value=0x10000)):
            library.sandbox_check.return_value = 1
            probe._darwin_readonly_path(Path('protected'))
            self.assertEqual(library.sandbox_check.call_count, 4)
            for result in (0, -1):
                library.sandbox_check.return_value = result
                with self.assertRaises(probe.BoundaryError):
                    probe._darwin_readonly_path(Path('protected'))

    def test_native_launch_passes_unicode_prompt_and_return_code(self):
        launch = envelope.CodexLaunch((), (), {}, Path('.'), Path('.'), '{}')
        with tempfile.TemporaryFile() as output:
            result = launch.run([sys.executable, '-c',
                "import sys; assert sys.stdin.read() == '검증 입력'; print('ok'); raise SystemExit(7)"],
                launch_process=subprocess.Popen, input='검증 입력', timeout=10,
                text=True, encoding='utf-8', env=dict(os.environ, PYTHONIOENCODING='utf-8'), stdout=output)
            self.assertEqual(result.returncode, 7)
            output.seek(0)
            self.assertEqual(output.read().strip(), b'ok')

    def test_streamed_provider_uses_owned_launch_and_preserves_session(self):
        from loop_manager_internal.agents import windows_runtime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt = root / 'attempt'
            attempt.mkdir()
            launch = envelope.CodexLaunch((), (), dict(os.environ, PYTHONIOENCODING='utf-8'), root, attempt, '{}')
            def prepare(*args, **kwargs):
                args[5](SimpleNamespace(status='verified'))
                return launch
            def start(command, **kwargs):
                index = command.index('codex')
                original = command[index:]
                result_path = original[original.index('-o') + 1]
                script = ("import sys,json; from pathlib import Path; "
                          "assert sys.stdin.read() == '입력'; "
                          "Path(sys.argv[1]).write_text('완료', encoding='utf-8'); "
                          "print(json.dumps({'type':'thread.started','thread_id':'new-session'}),flush=True)")
                return subprocess.Popen([*command[:index], sys.executable, '-c', script, result_path], **kwargs)
            effects = provider_adapters.ProviderEffects(
                role=lambda *_: ('model', 'high'), worker_profile=mock.Mock(), _agent_profile=mock.Mock(),
                _handoff_popen=start, _handoff_run=mock.Mock())
            sessions = []
            with mock.patch.object(isolation, 'prepare_isolation', side_effect=prepare), \
                 mock.patch.object(isolation, 'assert_current'), \
                 mock.patch.object(windows_runtime, 'check_command'):
                result = provider_adapters.run_codex(root, root, root / 'config', 'planner', '입력',
                    read_only=False, persist_session=True, effects=effects,
                    on_isolation_checked=lambda _: None, isolation_snapshot_digest=lambda: 'snapshot',
                    on_session_started=sessions.append)
            self.assertEqual((result.code, result.message, result.session_id), (0, '완료', 'new-session'))
            self.assertEqual(sessions, ['new-session'])

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object')
    def test_windows_job_stops_descendants_after_parent_exit_and_repeated_stop(self):
        from loop_manager_internal.execution.windows_job import process_identity
        launch = envelope.CodexLaunch((), (), {}, Path('.'), Path('.'), '{}')
        process = launch.start([sys.executable, '-c',
            "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(p.pid, flush=True)"],
            launch_process=subprocess.Popen, text=True, encoding='utf-8', stdout=subprocess.PIPE)
        try:
            process.stdin.close()
            pid = int(process.stdout.readline())
            process.wait(timeout=10)
            self.assertIsNotNone(process_identity(pid))
            launch.stop(process)
            launch.stop(process)
            deadline = time.monotonic() + 2
            while process_identity(pid) is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(process_identity(pid))
        finally:
            launch.stop(process)
            process.stdout.close()
