"""자체 Windows launcher의 복사·실패·부분 검증·정리 경계."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import verify_windows as launcher
from loop_manager_internal.execution import verify_process
from tests.support.git import isolated_git_test, make_remote_repository


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / 'loop_manager_internal/verification/entrypoint.py'


class WindowsVerifyRuntimeTests(unittest.TestCase):
    def test_incomplete_manager_runtime_or_coordination_fails_closed(self):
        contexts = [
            {'LOOP_MANAGER_VERIFY_RUNNER': str(ENTRYPOINT)},
            {'LOOP_MANAGER_VERIFY_RUNNER': str(ENTRYPOINT), 'LOOP_MANAGER_VERIFY_VERSION': '2',
             'LOOP_MANAGER_VERIFY_RUNTIME_PYTHON': sys.executable},
            {'LOOP_MANAGER_VERIFY_ENDPOINT': '["127.0.0.1", 1]'},
        ]
        for context in contexts:
            with self.subTest(context=context), mock.patch.dict(os.environ, context, clear=True), \
                 self.assertRaises(RuntimeError):
                launcher.runtime(ROOT)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux supervisor 정리 경계')
class WslCleanupTests(unittest.TestCase):
    def test_output_disconnect_after_child_exit_still_removes_the_copy(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ), \
             mock.patch.object(tempfile, 'tempdir', tempfile.tempdir), \
             mock.patch.object(sys, 'path', list(sys.path)):
            bundle = Path(directory)
            for name in ('project', 'runtime'):
                with tarfile.open(bundle / (name + '.tar'), 'w'):
                    pass
            with mock.patch.object(launcher.subprocess, 'run'), \
                 mock.patch.object(verify_process, 'run_verify_process', return_value=(True, False, 'PASS')), \
                 mock.patch('builtins.print', side_effect=BrokenPipeError), self.assertRaises(BrokenPipeError):
                launcher.wsl_main(bundle, 30, 'auto')
            receipt = json.loads((bundle / 'cleanup.json').read_text())
            self.assertTrue(receipt['stopped'])
            self.assertTrue(receipt['removed'])
            self.assertFalse(Path(receipt['root']).exists())


@isolated_git_test
class WindowsVerifySnapshotTests(unittest.TestCase):
    def test_snapshot_contains_working_changes_without_ignored_state_or_windows_git_pointer(self):
        # 실제 Git의 tracked/untracked/ignore 판정을 사용해 복사 범위를 확인한다.
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            (root / '.gitignore').write_text('private/\n', encoding='utf-8')
            (root / 'private').mkdir()
            (root / 'private/auth.json').write_text('private-placeholder')
            (root / 'src/app.py').write_bytes(b'VALUE = 9\r\n')
            (root / '새 파일.py').write_bytes(b'NEW = 1\n')
            (root / 'README.md').unlink()
            bundle = Path(directory) / 'bundle'
            bundle.mkdir()
            evidence = launcher.snapshot(root, bundle, ENTRYPOINT)
            with tarfile.open(bundle / 'project.tar') as archive:
                names = archive.getnames()
                self.assertNotIn('.git', names)
                self.assertNotIn('private/auth.json', names)
                self.assertNotIn('README.md', names)
                self.assertEqual(archive.extractfile('src/app.py').read(), b'VALUE = 9\r\n')
                self.assertEqual(archive.extractfile('새 파일.py').read(), b'NEW = 1\n')
            self.assertEqual(len(evidence['revision']), 40)
            self.assertEqual(len(evidence['snapshot_sha256']), 64)
            self.assertTrue((root / 'private/auth.json').is_file())

    def test_source_change_during_copy_rejects_the_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_remote_repository(directory)
            bundle = Path(directory) / 'bundle'
            bundle.mkdir()
            original = tarfile.TarFile.add
            def add(archive, path, **options):
                original(archive, path, **options)
                if Path(path).name == 'app.py':
                    Path(path).write_text('CHANGED_DURING_COPY = 1\n')
            with mock.patch.object(tarfile.TarFile, 'add', new=add), \
                 self.assertRaisesRegex(RuntimeError, '복사 중 파일'):
                launcher.snapshot(root, bundle, ENTRYPOINT)


class WindowsVerifyPipelineTests(unittest.TestCase):
    def setUp(self):
        self.cleanup = ExitStack()
        self.addCleanup(self.cleanup.close)
        self.root = Path(self.cleanup.enter_context(tempfile.TemporaryDirectory()))
        self.logs = self.root / 'logs'
        self.cleanup.enter_context(mock.patch.dict(os.environ))
        for key in tuple(os.environ):
            if key.startswith('LOOP_MANAGER_VERIFY_'):
                os.environ.pop(key)
        os.environ.update(LOOP_MANAGER_VERIFY_LOG_DIR=str(self.logs), LOOP_MANAGER_VERIFY_TIMEOUT='60')
        self.cleanup.enter_context(mock.patch.object(launcher, 'runtime', return_value=(ENTRYPOINT, sys.executable)))
        self.snapshot = self.cleanup.enter_context(mock.patch.object(launcher, 'snapshot', return_value={'revision': 'a' * 40}))
        self.real_wsl = launcher.run_wsl
        self.wsl = self.cleanup.enter_context(mock.patch.object(launcher, 'run_wsl', return_value=True))
        self.native = self.cleanup.enter_context(mock.patch.object(verify_process, 'run_verify_process',
            return_value=(True, False, verify_process.VerifyProcessOutput('core PASS', exit_code=0))))

    def report(self):
        return json.loads((self.logs / 'result.json').read_text(encoding='utf-8'))

    def test_wsl_failure_does_not_run_native_or_claim_whole_success(self):
        self.wsl.return_value = False
        self.assertEqual(launcher.windows_main(self.root), 1)
        self.native.assert_not_called()
        self.assertEqual(self.report()['wsl'], 'FAIL')
        self.assertEqual(self.report()['windows'], 'NOT_RUN')
        self.assertFalse(self.report()['passed'])

    def test_native_failure_and_timeout_share_the_remaining_total_budget(self):
        self.native.return_value = (False, True, verify_process.VerifyProcessOutput('timeout', exit_code=1))
        self.assertEqual(launcher.windows_main(self.root), 1)
        self.assertEqual(self.report()['wsl'], 'PASS')
        self.assertEqual(self.report()['windows'], 'FAIL')
        self.assertFalse(self.report()['passed'])
        self.assertEqual(self.native.call_args.args[2], 60)

    def test_native_does_not_subtract_wsl_elapsed_time_twice(self):
        from loop_manager_internal.verification import execution_clock
        clock_type = execution_clock.ExecutionClock
        now = [0.0]
        def wsl(*args):
            now[0] = 40.0
            return True
        def native(*args, **kwargs):
            budget = kwargs['execution_clock']
            self.assertIs(execution_clock.current_execution_clock.get(), budget)
            self.assertEqual(budget.remaining(args[2]), 20.0)
            now[0] = 60.0
            self.assertEqual(budget.remaining(args[2]), 0.0)
            return True, False, verify_process.VerifyProcessOutput('core PASS', exit_code=0)
        self.wsl.side_effect = wsl
        self.native.side_effect = native
        with mock.patch.object(execution_clock, 'ExecutionClock', side_effect=lambda *args, **kwargs:
                clock_type(*args, **kwargs, clock=lambda: now[0])):
            self.assertEqual(launcher.windows_main(self.root), 0)

    def test_partial_request_falls_back_without_executing_either_phase(self):
        os.environ.update(LOOP_MANAGER_VERIFY_PARTIAL_BASE='base', LOOP_MANAGER_VERIFY_SESSION='session')
        self.assertEqual(launcher.windows_main(self.root), 0)
        self.snapshot.assert_not_called()
        self.wsl.assert_not_called()
        self.native.assert_not_called()
        report = json.loads((self.logs / 'partial-result.json').read_text())
        self.assertEqual((report['run_id'], report['status']), ('session', 'FALLBACK'))
        self.assertFalse((self.logs / 'result.json').exists())

    def test_windows_copy_cleanup_failure_cannot_claim_whole_success(self):
        with mock.patch.object(launcher.tempfile, 'TemporaryDirectory') as temporary:
            temporary.return_value.__enter__.return_value = str(self.root / 'bundle')
            temporary.return_value.__exit__.side_effect = OSError('cleanup failed')
            with self.assertRaisesRegex(OSError, 'cleanup failed'):
                launcher.windows_main(self.root)
        self.assertEqual((self.report()['wsl'], self.report()['windows']), ('PASS', 'PASS'))
        self.assertFalse(self.report()['passed'])

    def test_successful_exit_without_linux_cleanup_proof_is_not_success(self):
        bundle = self.root / 'bundle'
        bundle.mkdir()
        (bundle / 'cleanup.json').write_text(json.dumps({'stopped': True, 'removed': False}))
        with mock.patch.object(launcher.subprocess, 'run', return_value=SimpleNamespace(stdout='/tmp/bundle')), \
             mock.patch.object(verify_process, 'run_verify_process', return_value=(True, False, 'exit 0')), \
             self.assertRaises(verify_process.VerifyTerminationUncertain):
            self.real_wsl(self.root, bundle, self.logs, 30, 'auto')


if __name__ == '__main__':
    unittest.main()
