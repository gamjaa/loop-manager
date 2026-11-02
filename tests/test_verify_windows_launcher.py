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
            original_mkdtemp = tempfile.mkdtemp
            requested_tmp_dirs = []
            def allocate_temp(suffix=None, prefix=None, dir=None, **options):
                # Keep checking the /tmp request while allocating under this writable fixture.
                if dir == '/tmp':
                    requested_tmp_dirs.append(dir)
                    dir = directory
                return original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir, **options)
            with mock.patch.object(launcher.subprocess, 'run'), \
                 mock.patch.object(verify_process, 'run_verify_process', return_value=(True, False, 'PASS')), \
                 mock.patch.object(launcher.tempfile, 'mkdtemp', side_effect=allocate_temp), \
                 mock.patch('builtins.print', side_effect=BrokenPipeError), self.assertRaises(BrokenPipeError):
                launcher.wsl_main(bundle, 30, 'auto')
            receipt = json.loads((bundle / 'cleanup.json').read_text())
            self.assertTrue(receipt['stopped'])
            self.assertTrue(receipt['removed'])
            self.assertEqual(requested_tmp_dirs, ['/tmp'])
            self.assertFalse(Path(receipt['root']).exists())

    def test_live_logs_are_shared_and_linked_to_parent_run(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ), \
             mock.patch.object(tempfile, 'tempdir', tempfile.tempdir), \
             mock.patch.object(sys, 'path', list(sys.path)):
            bundle = Path(directory)
            for name in ('project', 'runtime'):
                with tarfile.open(bundle / (name + '.tar'), 'w'):
                    pass
            run_root = bundle / 'supervised-run'
            observed_environment = {}
            def make_run_root(prefix='tmp-', **_options):
                target = run_root if prefix == 'loop-manager-verify-' else bundle / (prefix + 'budget')
                target.mkdir()
                return str(target)
            def run_verify(_arguments, _project, _timeout, *, environment, **_options):
                observed_environment.update(environment)
                log_directory = Path(environment['LOOP_MANAGER_VERIFY_LOG_DIR'])
                (log_directory / 'runner.jsonl').write_text('{}\n', encoding='utf-8')
                return True, False, 'PASS'
            with mock.patch.object(launcher.tempfile, 'mkdtemp', side_effect=make_run_root), \
                 mock.patch.object(launcher.subprocess, 'run'), \
                 mock.patch.object(verify_process, 'run_verify_process', side_effect=run_verify):
                self.assertEqual(launcher.wsl_main(bundle, 30, 'auto', parent_run_id='parent-run'), 0)
            receipt = json.loads((bundle / 'cleanup.json').read_text())
            self.assertTrue(receipt['stopped'])
            self.assertTrue(receipt['removed'])
            self.assertFalse(run_root.exists())
            self.assertEqual(observed_environment['LOOP_MANAGER_VERIFY_PARENT_RUN_ID'], 'parent-run')
            self.assertEqual(observed_environment['LOOP_MANAGER_VERIFY_LOG_DIR'], str(bundle / 'logs'))
            self.assertTrue((bundle / 'logs/runner.jsonl').is_file())


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
        self.temporary = self.root / 'selected temp ü'
        self.temporary.mkdir()
        self.cleanup.enter_context(mock.patch.dict(os.environ))
        self.cleanup.enter_context(mock.patch.object(launcher.tempfile, 'tempdir', None))
        for key in tuple(os.environ):
            if key.startswith('LOOP_MANAGER_VERIFY_'):
                os.environ.pop(key)
        os.environ.update(LOOP_MANAGER_VERIFY_LOG_DIR=str(self.logs), LOOP_MANAGER_VERIFY_TIMEOUT='60',
                          LOOP_MANAGER_VERIFY_TEMP_DIR=str(self.temporary), TEMP=str(self.temporary),
                          TMP=str(self.temporary), TMPDIR=str(self.temporary))
        self.cleanup.enter_context(mock.patch.object(launcher, 'runtime', return_value=(ENTRYPOINT, sys.executable)))
        self.snapshot = self.cleanup.enter_context(mock.patch.object(launcher, 'snapshot', return_value={'revision': 'a' * 40}))
        self.real_wsl = launcher.run_wsl
        self.wsl_result = True
        self.wsl = self.cleanup.enter_context(mock.patch.object(launcher, 'run_wsl', side_effect=self.run_wsl))
        self.native_result = (True, False, verify_process.VerifyProcessOutput('core PASS', exit_code=0))
        self.native = self.cleanup.enter_context(mock.patch.object(verify_process, 'run_verify_process',
            side_effect=self.run_native))

    def write_wsl_summary(self, directory):
        directory = Path(directory)
        (directory / 'summary.json').write_text(json.dumps({'metadata': {
            'run_id': 'linux-child', 'execution_context': {
                'temporary_directory': '/tmp/loop-manager-verify-unit/tmp'}}}), encoding='utf-8')

    def run_wsl(self, _root, _bundle, directory, _timeout, _cap, parent_run_id):
        self.write_wsl_summary(directory)
        summary_path = Path(directory) / 'summary.json'
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        summary['metadata']['parent_run_id'] = parent_run_id
        summary_path.write_text(json.dumps(summary), encoding='utf-8')
        return self.wsl_result

    def write_native_summary(self, arguments):
        directory = Path(arguments[arguments.index('--native') + 3])
        (directory / 'summary.json').write_text(json.dumps({
            'temporary_directory': str(Path(os.environ['LOOP_MANAGER_VERIFY_TEMP_DIR']).resolve())
        }), encoding='utf-8')

    def run_native(self, arguments, *_args, **_kwargs):
        self.write_native_summary(arguments)
        return self.native_result

    def report(self):
        return json.loads((self.logs / 'result.json').read_text(encoding='utf-8'))

    def test_wsl_failure_does_not_run_native_or_claim_whole_success(self):
        self.wsl_result = False
        self.assertEqual(launcher.windows_main(self.root), 1)
        self.native.assert_not_called()
        self.assertEqual(self.report()['wsl'], 'FAIL')
        self.assertEqual(self.report()['windows'], 'NOT_RUN')
        self.assertEqual(self.report()['temporary_directories']['wsl'], '/tmp/loop-manager-verify-unit/tmp')
        self.assertIsNone(self.report()['temporary_directories']['windows_native'])
        self.assertFalse(self.report()['passed'])

    def test_native_failure_and_timeout_share_the_remaining_total_budget(self):
        self.native_result = (False, True, verify_process.VerifyProcessOutput('timeout', exit_code=1))
        self.assertEqual(launcher.windows_main(self.root), 1)
        self.assertEqual(self.report()['wsl'], 'PASS')
        self.assertEqual(self.report()['windows'], 'FAIL')
        self.assertEqual(self.report()['temporary_directories']['windows_native'], str(self.temporary.resolve()))
        self.assertFalse(self.report()['passed'])
        self.assertEqual(self.native.call_args.args[2], 60)

    def test_parent_report_links_wsl_and_native_summary_identities(self):
        def run_wsl(_root, _bundle, logs, _timeout, _cap, parent_run_id):
            (logs / 'summary.json').write_text(json.dumps({
                'metadata': {'run_id': 'linux-child', 'parent_run_id': parent_run_id,
                             'clock_domain': 'linux', 'mode': 'full',
                             'execution_context': {'temporary_directory': '/tmp/loop-manager-verify-unit/tmp'}},
                'result': {'passed': True}, 'elapsed_seconds': 12.0,
                'counts': {'success': 2}, 'phases': [], 'assignments': [],
                'allocations': [], 'git_commands': []}), encoding='utf-8')
            return True
        self.wsl.side_effect = run_wsl
        native_environment = {}
        def run_native(arguments, _root, _timeout, *, environment, **_options):
            from loop_manager_internal.verification import unittest_adapter
            native_environment.update(environment)
            native_logs = Path(arguments[6])
            with mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(sys, 'path', list(sys.path)), \
                 mock.patch('loop_manager_internal.verification.guard.install_guard'), \
                 mock.patch.object(unittest_adapter, 'collect_project', return_value=([], sys.executable)), \
                 mock.patch.object(unittest_adapter, 'execute', return_value=True), \
                 mock.patch('builtins.print'):
                code = launcher.native_main(Path(arguments[4]), Path(arguments[5]),
                                            native_logs, arguments[7])
            return code == 0, False, verify_process.VerifyProcessOutput('core PASS', exit_code=code)
        self.native.side_effect = run_native

        self.assertEqual(launcher.windows_main(self.root), 0)

        report = self.report()
        self.assertEqual(report['wsl_run_id'], 'linux-child')
        self.assertEqual(native_environment['LOOP_MANAGER_VERIFY_PARENT_RUN_ID'], report['run_id'])
        self.assertEqual(report['temporary_directories'], {
            'windows_launcher': str(self.temporary.resolve()),
            'wsl': '/tmp/loop-manager-verify-unit/tmp',
            'windows_native': str(self.temporary.resolve()),
        })
        self.assertEqual(report['wsl'], 'PASS')
        summary = json.loads((self.logs / 'summary.json').read_text(encoding='utf-8'))
        pipeline = summary['windows_pipeline']
        self.assertEqual(pipeline['snapshot']['revision'], 'a' * 40)
        self.assertEqual(report['windows_run_id'], pipeline['children']['windows']['run_id'])
        self.assertEqual(pipeline['children']['wsl']['run_id'], 'linux-child')
        self.assertEqual(pipeline['children']['wsl']['parent_run_id'], report['run_id'])
        self.assertEqual(pipeline['children']['wsl']['elapsed_seconds'], 12.0)
        self.assertNotEqual(pipeline['children']['windows']['run_id'], report['run_id'])
        self.assertEqual(pipeline['children']['windows']['run_id'], report['windows_run_id'])
        self.assertEqual(pipeline['children']['windows']['parent_run_id'], report['run_id'])
        self.assertGreaterEqual(pipeline['children']['windows']['elapsed_seconds'], 0.0)
        self.assertIn('summary.json', pipeline['children']['wsl']['summary_path'])

    def test_explicit_temp_directory_is_used_and_stage_paths_are_observed(self):
        self.assertEqual(launcher.windows_main(self.root), 0)
        self.assertEqual(self.report()['temporary_directories'], {
            'windows_launcher': str(self.temporary.resolve()),
            'wsl': '/tmp/loop-manager-verify-unit/tmp',
            'windows_native': str(self.temporary.resolve()),
        })

    def test_temp_environment_mismatch_fails_before_bundle_or_stages(self):
        fallback = self.root / 'different-temp'
        fallback.mkdir()
        os.environ['TMPDIR'] = str(fallback)
        with self.assertRaisesRegex(RuntimeError, '실제 Python 임시 경로'):
            launcher.windows_main(self.root)
        self.snapshot.assert_not_called()
        self.wsl.assert_not_called()
        self.native.assert_not_called()

    def test_native_does_not_subtract_wsl_elapsed_time_twice(self):
        from loop_manager_internal.verification import execution_clock
        clock_type = execution_clock.ExecutionClock
        now = [0.0]
        def wsl(*args):
            self.write_wsl_summary(args[2])
            now[0] = 40.0
            return True
        def native(*args, **kwargs):
            self.write_native_summary(args[0])
            budget = kwargs['execution_clock']
            self.assertIs(execution_clock.current_execution_clock.get(), budget)
            self.assertEqual(budget.remaining(args[2]), 20.0)
            now[0] = 59.0
            self.assertEqual(budget.remaining(args[2]), 1.0)
            return True, False, verify_process.VerifyProcessOutput('core PASS', exit_code=0)
        self.wsl.side_effect = wsl
        self.native.side_effect = native
        with mock.patch.object(execution_clock, 'ExecutionClock', side_effect=lambda *args, **kwargs:
                clock_type(*args, **kwargs, clock=lambda: now[0])):
            self.assertEqual(launcher.windows_main(self.root), 0)

    def test_wsl_deadline_blocks_native(self):
        from loop_manager_internal.verification import execution_clock
        clock_type = execution_clock.ExecutionClock
        now = [0.0]
        def wsl(*args):
            self.write_wsl_summary(args[2])
            now[0] = 60.0
            return True
        self.wsl.side_effect = wsl
        with mock.patch.object(execution_clock, 'ExecutionClock', side_effect=lambda *args, **kwargs:
                clock_type(*args, **kwargs, clock=lambda: now[0])):
            with self.assertRaises(TimeoutError):
                launcher.windows_main(self.root)
        self.native.assert_not_called()
        self.assertEqual((self.report()['wsl'], self.report()['windows']), ('PASS', 'NOT_RUN'))
        self.assertFalse(self.report()['passed'])

    def test_native_completion_at_deadline_cannot_pass(self):
        from loop_manager_internal.verification import execution_clock
        clock_type = execution_clock.ExecutionClock
        now = [0.0]
        def native(*args, **kwargs):
            self.write_native_summary(args[0])
            now[0] = 60.0
            return self.native_result
        self.native.side_effect = native
        with mock.patch.object(execution_clock, 'ExecutionClock', side_effect=lambda *args, **kwargs:
                clock_type(*args, **kwargs, clock=lambda: now[0])):
            with self.assertRaises(TimeoutError):
                launcher.windows_main(self.root)
        self.assertEqual(self.report()['windows'], 'PASS')
        self.assertFalse(self.report()['passed'])

    def test_partial_request_falls_back_without_executing_either_phase(self):
        os.environ.update(LOOP_MANAGER_VERIFY_PARTIAL_BASE='base', LOOP_MANAGER_VERIFY_SESSION='session')
        self.assertEqual(launcher.windows_main(self.root), 0)
        self.snapshot.assert_not_called()
        self.wsl.assert_not_called()
        self.native.assert_not_called()
        report = json.loads((self.logs / 'partial-result.json').read_text())
        self.assertEqual((report['run_id'], report['status']), ('session', 'FALLBACK'))
        self.assertEqual(report['temporary_directories'], {
            'windows_launcher': str(self.temporary.resolve()), 'wsl': None, 'windows_native': None})
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
