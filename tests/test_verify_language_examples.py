"""Manager-only integration: both example projects traverse the common core."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.execution.verify_process import run_verify_process
from loop_manager_internal.execution.result_validation import verify_arguments
from loop_manager_internal.verification import manager_adapter, runner
from loop_manager_internal.verification.coordination import VerifyCoordinator, settings


class LanguageExampleIntegrationTests(unittest.TestCase):
    def _check_language(self, language):
        # Nested native runs need startup/shutdown room under full-suite load.
        verify_timeout = 90
        process_timeout = 120
        repository = Path(__file__).resolve().parents[1]
        entrypoint = Path(runner.__file__).with_name('entrypoint.py').resolve()
        git_file = repository / '.git'
        git_dir = (repository / git_file.read_text().strip().removeprefix('gitdir: ')).resolve() if git_file.is_file() else git_file
        with tempfile.TemporaryDirectory() as temporary, VerifyCoordinator() as coordinator:
            root = Path(temporary)
            token = settings.set(SimpleNamespace(coordinator=coordinator, cap=1, state_dir=root / 'state'))
            try:
                project = root / language
                shutil.copytree(repository / 'tests/support/verify_examples' / language, project)
                if language == 'perl':
                    executable = shutil.which('perl')
                    self.assertIsNotNone(executable, 'Perl is required for Linux example integration')
                    declaration = project / 'scripts/verify.json'
                    data = json.loads(declaration.read_text())
                    data['environment']['perl'] = str(Path(executable).absolute())
                    declaration.write_text(json.dumps(data))
                for coordinated, failing in ((False, False), (True, False), (False, True), (True, True)):
                    with self.subTest(language=language, coordinated=coordinated, failing=failing):
                        if failing:
                            test = project / ('tests/example.t' if language == 'perl' else 'tests/test_example.py')
                            test.write_text(test.read_text().replace(', 5,', ', 6,') if language == 'perl'
                                else test.read_text().replace('sum(self.fixture), 5', 'sum(self.fixture), 6'))
                        logs = root / f'logs-{language}-{coordinated}-{failing}'
                        environment = dict(os.environ, GIT_DIR=str(git_dir), GIT_WORK_TREE=str(project),
                            LOOP_MANAGER_VERIFY_LOG_DIR=str(logs))
                        for key in ('LOOP_MANAGER_VERIFY_ENDPOINT', 'LOOP_MANAGER_VERIFY_SESSION',
                                'LOOP_MANAGER_VERIFY_SECRET', 'LOOP_MANAGER_VERIFY_PARTIAL_BASE'):
                            environment.pop(key, None)
                        argv = [sys.executable, str(entrypoint), '--protocol-version', '1',
                            '--project-python', sys.executable, '--parallelism', '1', '--timeout', str(verify_timeout)]
                        with mock.patch.dict(os.environ, environment, clear=True):
                            if coordinated:
                                passed, timed_out, output = manager_adapter.run(
                                    SimpleNamespace(run_verify_process=run_verify_process),
                                    verify_arguments(project)[1], project, verify_timeout)
                                candidates = list((root / 'state/logs/verify').glob('*/manifest.json'))
                                manifest_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
                            else:
                                passed, timed_out, output = run_verify_process(argv, project, process_timeout, environment=environment, job_scope=True)
                                manifest_path = logs / 'manifest.json'
                        self.assertFalse(timed_out, str(output))
                        self.assertEqual(passed, not failing, str(output))
                        manifest = json.loads(manifest_path.read_text())
                        self.assertEqual([stage['kind'] for stage in manifest['stages']], ['command', 'tests'])
                        self.assertEqual(manifest['adapter'], 'perl-tap' if language == 'perl' else 'unittest')
                        report = json.loads(next((manifest_path.parent / 'stage-1').glob('module-*.json')).read_text())
                        self.assertEqual(report['run_id'], manifest['run_id'])
                        self.assertEqual(report['passed'], not failing)
                        self.assertFalse(coordinator.reservations)
            finally:
                settings.reset(token)

    def test_python_standalone_and_manager_preserve_success_and_native_failure(self):
        self._check_language('python')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Perl native example currently supports Linux')
    def test_perl_standalone_and_manager_preserve_success_and_native_failure(self):
        self._check_language('perl')
