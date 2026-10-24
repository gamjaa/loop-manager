"""Unit launch contract and Manager-only native integration for both examples."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from textwrap import indent
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.execution.verify_process import run_verify_process
from loop_manager_internal.execution.result_validation import verify_arguments
from loop_manager_internal.verification import manager_adapter, runner
from loop_manager_internal.verification.coordination import VerifyCoordinator, settings
from loop_manager_internal.verification.guard import _COORDINATION_ENV, MARKER, worker_environment
from loop_manager_internal.verification.resources import MIB, Snapshot
from tests.support.verify_fixture import nested_runner_source


def _example_test_coordinator():
    sampler = SimpleNamespace(sample=lambda: Snapshot(
        time.monotonic(), 8, 0, 4 * 1024 * MIB, 'language-example-test'))
    return VerifyCoordinator(sampler=sampler)


class LanguageExampleCoordinatorTests(unittest.TestCase):
    def test_nested_example_coordinator_uses_available_fixture_capacity(self):
        coordinator = _example_test_coordinator()
        session, secret = coordinator.open(1)
        try:
            reservation = coordinator.request(session, secret, 'acquire')
            self.assertTrue(reservation['granted'])
            coordinator.request(session, secret, 'not_started', token=reservation['token'])
        finally:
            coordinator.finish(session, stopped=True)


def example_launch(directory, entrypoint, command, **options):
    # Guarded subprocesses intentionally lose coordinator credentials. Restore
    # only this fixture's session in the runner, never in project test workers.
    script = directory / 'example-runner.py'
    script.with_suffix('.json').write_text(json.dumps({
        'entrypoint': str(entrypoint),
        'coordination': {key: options['env'][key] for key in _COORDINATION_ENV
                         if key in options['env']},
    }))
    script.write_text('''import json, os, sys
from pathlib import Path
config = json.loads(Path(__file__).with_suffix('.json').read_text())
os.environ.update(config['coordination'])
sys.argv[0] = config['entrypoint']
''' + f"sys.path.insert(0, {str(entrypoint.resolve().parents[2])!r})\n" + '''
from unittest.mock import patch
from loop_manager_internal.execution import verify_process
native_run = verify_process.run_verify_process
def standalone_run(argv, *args, **kwargs):
    # The standalone runner creates a new --execute process. Keep this
    # fixture's sampler there too; project workers use the real entrypoint.
    if '--execute' in argv:
        argv = [__file__ if arg == config['entrypoint'] else arg for arg in argv]
    return native_run(argv, *args, **kwargs)
with patch.object(verify_process, 'run_verify_process', standalone_run):
''' + indent(nested_runner_source(entrypoint), '    '))
    options['env'] = dict(options['env'], LOOP_MANAGER_VERIFY_RUNNER=str(script))
    # Direct entrypoint calls and shell launchers cross the same guard boundary.
    command = [str(script) if arg == str(entrypoint) else arg for arg in command]
    return subprocess.Popen(command, **options)


class CoordinatedLaunchTests(unittest.TestCase):
    def test_guarded_runner_restores_only_fixture_coordination_and_workers_stay_sanitized(self):
        import runpy
        from loop_manager_internal.verification.resources import ResourceSampler, decide
        from loop_manager_internal.execution import verify_process
        original_sampler = ResourceSampler.sample
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entrypoint = root / 'entrypoint.py'
            managed = dict(zip(_COORDINATION_ENV, (
                '["127.0.0.1", 1234]', 'test-secret', 'test-session', str(root / 'budget'))))
            for coordinated in (True, False):
                with self.subTest(coordinated=coordinated):
                    coordination = managed if coordinated else {
                        'LOOP_MANAGER_VERIFY_BUDGET_DIR': managed['LOOP_MANAGER_VERIFY_BUDGET_DIR']}
                    environment = dict(coordination, LOOP_MANAGER_VERIFY_RUNNER=str(entrypoint),
                                       UNRELATED='not a coordinator field')
                    original = dict(environment)
                    command = ['python', 'owned-process-bootstrap',
                               'verify.sh' if coordinated else str(entrypoint)]
                    with mock.patch.object(subprocess, 'Popen') as launch:
                        process = example_launch(root, entrypoint, command, env=environment, cwd=root)
                    self.assertIs(process, launch.return_value)
                    self.assertEqual(environment, original)
                    self.assertEqual(launch.call_args.kwargs['cwd'], root)
                    script = Path(launch.call_args.kwargs['env']['LOOP_MANAGER_VERIFY_RUNNER'])
                    self.assertEqual(launch.call_args.args[0], [*command[:-1],
                        'verify.sh' if coordinated else str(script)])
                    self.assertEqual(json.loads(script.with_suffix('.json').read_text())['coordination'], coordination)
                    sanitized = worker_environment(launch.call_args.kwargs['env'])
                    self.assertTrue(all(key not in sanitized for key in _COORDINATION_ENV))
                    def entered(path, **kwargs):
                        self.assertEqual(path, str(entrypoint))
                        self.assertEqual(kwargs, {'run_name': '__main__'})
                        self.assertEqual(sys.argv, [str(entrypoint), '--protocol-version', '1'])
                        self.assertEqual(os.environ[MARKER], '1')
                        self.assertTrue(all(os.environ[key] == value for key, value in coordination.items()))
                        self.assertTrue(all(key not in worker_environment(os.environ) for key in _COORDINATION_ENV))
                        observed = ResourceSampler().sample()
                        self.assertEqual((observed.cpus, observed.busy, observed.available),
                                         (2, 0, 4 * 1024 ** 3))
                        self.assertTrue(decide(observed, now=observed.at, running=0, cap=1).granted)
                        for mode in ('--execute', '--worker'):
                            argv = ['python', str(entrypoint), mode]
                            self.assertIs(verify_process.run_verify_process(argv, root, 90,
                                environment=sanitized), native.return_value)
                            native.assert_called_with(['python', str(script) if mode == '--execute'
                                else str(entrypoint), mode], root, 90, environment=sanitized)
                    with mock.patch.dict(os.environ, sanitized, clear=True), \
                            mock.patch.object(sys, 'path', list(sys.path)), \
                            mock.patch.object(sys, 'argv', [str(script), '--protocol-version', '1']), \
                            mock.patch.object(verify_process, 'run_verify_process') as native, \
                            mock.patch.object(runpy, 'run_path', side_effect=entered) as run:
                        exec(compile(script.read_bytes(), str(script), 'exec'), {'__file__': str(script)})
                        self.assertIs(verify_process.run_verify_process, native)
                    run.assert_called_once()
                    self.assertIs(ResourceSampler.sample, original_sampler)


class LanguageExampleIntegrationTests(unittest.TestCase):
    def _check_language(self, language):
        # Nested native runs need startup/shutdown room under full-suite load.
        verify_timeout = 90
        process_timeout = 120
        repository = Path(__file__).resolve().parents[1]
        entrypoint = Path(runner.__file__).with_name('entrypoint.py').resolve()
        git_file = repository / '.git'
        git_dir = (repository / git_file.read_text().strip().removeprefix('gitdir: ')).resolve() if git_file.is_file() else git_file
        # This Manager integration test runs inside the verify resource pool.
        # Give its nested coordinator stable capacity under unrelated suite load.
        with tempfile.TemporaryDirectory() as temporary, _example_test_coordinator() as coordinator:
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
                # The full suite can run another module beside this nested runner
                # on 2-CPU hosts; keep the behavior checks past that wait.
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
                        def launch(command, **options):
                            return example_launch(root, entrypoint, command, **options)
                        with mock.patch.dict(os.environ, environment, clear=True):
                            if coordinated:
                                passed, timed_out, output = manager_adapter.run(
                                    SimpleNamespace(run_verify_process=run_verify_process),
                                    verify_arguments(project)[1], project, verify_timeout,
                                    launch=launch)
                                candidates = list((root / 'state/logs/verify').glob('*/manifest.json'))
                                manifest_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
                            else:
                                passed, timed_out, output = run_verify_process(argv, project, process_timeout,
                                    environment=environment, job_scope=True, launch_process=launch)
                                manifest_path = logs / 'manifest.json'
                        diagnostic = str(output) + '\n' + '\n'.join(
                            path.read_text(encoding='utf-8', errors='replace')[-3000:]
                            for path in manifest_path.parent.rglob('*.jsonl'))
                        self.assertFalse(timed_out, diagnostic)
                        self.assertEqual(passed, not failing, diagnostic)
                        manifest = json.loads(manifest_path.read_text())
                        summary = json.loads((manifest_path.parent / 'summary.json').read_text())
                        self.assertEqual(summary['metadata']['coordinated'], coordinated)
                        if coordinated:
                            self.assertEqual(manifest['run_id'], manifest_path.parent.name)
                        self.assertEqual([stage['kind'] for stage in manifest['stages']], ['command', 'tests'])
                        self.assertEqual(manifest['adapter'], 'perl-tap' if language == 'perl' else 'unittest')
                        reports = list((manifest_path.parent / 'stage-1').glob('module-*.json'))
                        self.assertEqual(len(reports), 1, diagnostic)
                        report = json.loads(reports[0].read_text())
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
