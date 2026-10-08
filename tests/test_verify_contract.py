"""Opt-in, adapter and process contracts, without full verify or real CLI execution."""
from contextlib import ExitStack
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.verification import contract, core, manifest, runner, unittest_adapter
from loop_manager_internal.verification.timing import EventWriter, summarize, comparison
from loop_manager_internal.execution import result_validation, verify_process


class VerifyContractTests(unittest.TestCase):
    def setUp(self):
        self.cleanup = ExitStack()
        self.addCleanup(self.cleanup.close)
        self.root = Path(self.cleanup.enter_context(tempfile.TemporaryDirectory()))
        (self.root / 'scripts').mkdir()
        (self.root / 'tests').mkdir()
        (self.root / 'scripts/verify.sh').write_text('existing verify')
        self.declaration = {
            'version': 1, 'adapter': 'unittest', 'environment': {'python': 'launcher'},
            'stages': [{'id': 'tests', 'kind': 'tests', 'cwd': '.',
                        'collection': {'directory': 'tests', 'pattern': 'test*.py'}, 'execution': 'independent'},
                       {'id': 'compile', 'kind': 'command', 'cwd': '.', 'argv': ['{python}', 'compile.py']}]}

    def declare(self):
        (self.root / 'scripts/verify.json').write_text(json.dumps(self.declaration))
        return contract.load(self.root, sys.executable)

    def writer(self):
        writer = EventWriter(self.root / 'events.jsonl', identity={'run_id': 'run', 'stage_id': 'tests'})
        self.cleanup.callback(writer.close)
        return writer

    def test_opt_in_replaces_internal_file_detection_and_never_falls_back_on_error(self):
        internal = self.root / 'loop_manager_internal/verification/runner.py'
        internal.parent.mkdir(parents=True)
        internal.write_text('unrelated file')
        effects = mock.Mock()
        effects.run_verify_process.return_value = (True, False, 'legacy')
        from loop_manager_internal.verification import manager_adapter
        with mock.patch.object(manager_adapter, 'run', return_value=(True, False, 'connected')) as connected:
            self.assertTrue(result_validation.verify(effects, self.root)[0])
            connected.assert_not_called()
            self.declare()
            self.assertEqual(result_validation.verify(effects, self.root)[1], 'connected')
            connected.assert_called_once()
            effects.run_verify_process.reset_mock()
            self.declaration['adapter'] = 'unknown'
            self.declare_error()
            passed, output = result_validation.verify(effects, self.root)
            self.assertFalse(passed)
            self.assertEqual(output.exit_code, 2)
            effects.run_verify_process.assert_not_called()
            self.assertEqual(connected.call_count, 1)

    def declare_error(self):
        (self.root / 'scripts/verify.json').write_text(json.dumps(self.declaration))

    def test_invalid_connection_observation_stays_unknown_until_execution_reports_failure(self):
        from loop_manager_internal.verification import manager_adapter
        (self.root / 'src').mkdir()
        self.declaration['version'] = 99
        self.declare_error()
        with mock.patch.object(manager_adapter, 'isolated_compile_arguments', return_value=['compile']),                 mock.patch.object(manager_adapter.subprocess, 'run') as probe:
            observation = manager_adapter.observe_isolated_compile(self.root,
                self.root / 'scripts/verify.sh', ['compile'], {})
        self.assertIsNone(observation.fingerprint)
        self.assertIn('invalid verify connection', observation.reason)
        probe.assert_not_called()

    def test_schema_rejects_versions_duplicate_keys_stages_paths_and_shell_commands(self):
        valid = json.loads(json.dumps(self.declaration))
        for alter in (
            lambda d: d.update(version=True), lambda d: d.update(version=2),
            lambda d: d.update(adapter='pytest'), lambda d: d.update(stages=[]),
            lambda d: d['stages'].append(dict(d['stages'][0])),
            lambda d: d['stages'][0].update(cwd='../'),
            lambda d: d['stages'][0].update(execution='automatic'),
            lambda d: d['stages'][1].update(argv='python compile.py'),
            lambda d: d['environment'].update(python='missing-python'),
        ):
            self.declaration = json.loads(json.dumps(valid))
            alter(self.declaration)
            self.declare_error()
            with self.assertRaises(contract.ContractError):
                contract.load(self.root, sys.executable)
        path = self.root / 'scripts/verify.json'
        path.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(contract.ContractError, 'duplicate'):
            contract.load(self.root)

    def test_virtualenv_interpreter_symlink_keeps_its_environment_path(self):
        venv = self.root / '.venv/bin'
        venv.mkdir(parents=True)
        interpreter = venv / 'python'
        try:
            interpreter.symlink_to(sys.executable)
        except OSError as error:
            self.skipTest(f'symlink unavailable: {error}')
        self.declaration['environment']['python'] = '.venv/bin/python'
        self.assertEqual(self.declare().python, interpreter)
        self.declaration['environment']['python'] = 'launcher'
        self.declare_error()
        self.assertEqual(contract.load(self.root, interpreter).python, interpreter)

    def test_manifest_checks_empty_duplicate_unit_and_test_identities(self):
        for units in ([], [manifest.Module('x', ())], [manifest.Module('x', ('a', 'a'))],
                      [manifest.Module('x', ('a',)), manifest.Module('x', ('b',))],
                      [manifest.Module('x', ('a',)), manifest.Module('y', ('a',))]):
            with self.assertRaises(contract.ContractError):
                contract.validate_manifest(units)
        contract.validate_manifest([manifest.Module('x', ('a',)), manifest.Module('y', ('b',))])

    def test_project_collection_uses_declared_interpreter_and_validates_run_identity(self):
        connection = self.declare()
        stage = connection.stages[0]
        module = manifest.Module('example', ('example.Case.test_a',))
        def execute(argv, cwd, *args):
            self.assertEqual(argv[0], str(Path(sys.executable).absolute()))
            self.assertEqual(cwd, self.root)
            request = json.loads(Path(argv[-1]).read_text())
            self.assertEqual(request['collection']['directory'], str(self.root / 'tests'))
            Path(request['result']).write_text(json.dumps({'version': 1, 'run_id': 'run',
                'stage_id': 'tests', 'python': sys.version, 'modules': [asdict(module)]}))
            return True
        units, version = unittest_adapter.collect_project(connection, stage, self.root, 'run', Path('runtime'),
            10, execute, self.writer(), mock.Mock())
        self.assertEqual(units, [module])
        self.assertEqual(version, sys.version)
        with mock.patch.object(unittest_adapter, 'json') as parser:
            parser.loads.return_value = {'version': 2, 'run_id': 'run', 'stage_id': 'tests'}
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                unittest_adapter.collect_project(connection, stage, self.root, 'run', Path('runtime'),
                    10, lambda *args: True, self.writer(), mock.Mock())

    def test_adapter_checks_envelope_coverage_fixture_skip_and_internal_parallelism(self):
        connection = self.declare()
        unit = manifest.Module('example', ('a',), (('a', 'example', 'example.Case'),))
        def execute(units, directory, writer, client, **callbacks):
            validate = callbacks['validate_report']
            report = {'version': 1, 'run_id': 'run', 'stage_id': 'tests', 'unit_id': 'example',
                      'module': 'example', 'passed': True, 'completed': ['a']}
            self.assertTrue(validate(unit, report))
            for change in ({'version': 2}, {'run_id': 'old'}, {'unit_id': 'other'}, {'completed': []},
                           {'completed': ['a', 'a']}):
                with self.assertRaises(RuntimeError):
                    validate(unit, {**report, **change})
            self.assertFalse(validate(unit, {**report, 'passed': False, 'completed': []}))
            skipped = {**report, 'completed': [], 'fixture_skipped': ['a'],
                       'fixture_skip_sources': ['setUpClass (example.Case)']}
            self.assertTrue(validate(unit, skipped))
            callbacks['spawn']('token', 0, directory)
            return True
        with mock.patch.object(core, 'execute_manifest', side_effect=execute), \
                mock.patch.object(unittest_adapter, 'start_worker') as spawn:
            unittest_adapter.execute(connection, connection.stages[0], [unit], self.root, self.writer(), mock.Mock(), 'run', Path('runtime'))
        kwargs = spawn.call_args.kwargs
        self.assertEqual(kwargs['command'], [str(connection.python), '-u', 'runtime', '--worker'])
        self.assertNotIn('--jobs', kwargs['command'])
        self.assertEqual(kwargs['identity']['run_id'], 'run')

    def test_serial_stage_denies_second_process_but_reuses_existing_reservation(self):
        connection = self.declare()
        stage = {**connection.stages[0], 'execution': 'serial'}
        parent = mock.Mock()
        parent.request.return_value = {'granted': True, 'token': 'owned'}
        def execute(units, directory, writer, client, **kwargs):
            self.assertTrue(client.request('acquire')['granted'])
            self.assertFalse(client.request('acquire')['granted'])
            self.assertTrue(client.request('acquire', token='owned')['granted'])
            return True
        with mock.patch.object(core, 'execute_manifest', side_effect=execute):
            self.assertTrue(unittest_adapter.execute(connection, stage, [], self.root, self.writer(), parent, 'run', Path('runtime')))
        self.assertEqual(parent.request.call_count, 2)

    def test_command_reserves_binds_and_releases_after_confirmed_stop_without_test_results(self):
        client = mock.Mock()
        client.request.return_value = {'granted': True, 'reason': 'ready', 'token': 'owned'}
        def process(argv, cwd, timeout, **kwargs):
            self.assertEqual(argv, ['tool', 'argument with spaces'])
            self.assertNotIn('LOOP_MANAGER_VERIFY_SECRET', kwargs['environment'])
            kwargs['on_started'](SimpleNamespace(pid=123), None)
            return True, False, verify_process.VerifyProcessOutput('command done\n', exit_code=0)
        with mock.patch.object(sys, 'stdout', io.StringIO()):
            self.assertTrue(core.execute_command(['tool', 'argument with spaces'], self.root, 10,
                self.writer(), client, 'run', 'compile', run_process=process))
        operations = [call.args[0] for call in client.request.call_args_list]
        self.assertEqual(operations, ['acquire', 'bind', 'release'])
        summary = summarize(self.root)
        self.assertEqual(summary['tests'], [])
        events = [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines()]
        result = next(event for event in events if event['kind'] == 'unit_result')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['run_id'], 'run')

    def test_command_start_failure_and_uncertain_stop_preserve_correct_reservations(self):
        for launched in (False, True):
            client = mock.Mock()
            client.request.return_value = {'granted': True, 'reason': 'ready', 'token': 'owned'}
            def process(*args, **kwargs):
                if launched:
                    kwargs['on_started'](SimpleNamespace(pid=123), None)
                    raise verify_process.VerifyTerminationUncertain('children unknown')
                raise OSError('not started')
            with self.assertRaises((OSError, verify_process.VerifyTerminationUncertain)):
                core.execute_command(['tool'], self.root, 10, self.writer(), client, 'run', 'compile', run_process=process)
            operations = [call.args[0] for call in client.request.call_args_list]
            self.assertEqual(operations, ['acquire', 'bind'] if launched else ['acquire', 'not_started'])

    def test_resource_wait_counts_toward_command_deadline(self):
        client = mock.Mock()
        client.request.return_value = {'granted': False, 'reason': 'pressure'}
        process = mock.Mock()
        with mock.patch.object(core.time, 'monotonic', side_effect=[0, 0, 2]), self.assertRaises(TimeoutError):
            core.execute_command(['tool'], self.root, 1, self.writer(), client, 'run', 'compile',
                                 run_process=process, sleep=lambda seconds: None)
        process.assert_not_called()
        self.assertEqual([call.args[0] for call in client.request.call_args_list], ['acquire'])

    def test_runner_preserves_stage_order_and_stops_before_compile_on_test_failure(self):
        self.declare()
        unit = manifest.Module('example', ('a',))
        for passed in (True, False):
            logs = self.root / str(passed)
            with mock.patch.object(runner.Path, 'cwd', return_value=self.root), \
                    mock.patch.object(runner, 'install_guard'), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(runner, 'VerifyCoordinator') as coordinator, \
                    mock.patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(stdout='revision')), \
                    mock.patch.object(runner.unittest_adapter, 'collect_project', return_value=([unit], sys.version)), \
                    mock.patch.object(runner.unittest_adapter, 'execute', return_value=passed), \
                    mock.patch.object(runner, 'execute_command', return_value=True) as command, \
                    mock.patch.object(sys, 'stdout', io.StringIO()):
                coordinator.return_value.open.return_value = ('session', 'secret')
                coordinator.return_value.reservations = {}
                self.assertEqual(runner.run(logs, 1, project_python=sys.executable), 0 if passed else 1)
                self.assertEqual(command.call_count, int(passed))
                if passed:
                    self.assertEqual(command.call_args.args[0], [str(Path(sys.executable).absolute()), 'compile.py'])
            data = json.loads((logs / 'manifest.json').read_text())
            self.assertEqual(data['version'], 1)
            self.assertEqual(data['stages'][0]['units'][0]['ids'], ['a'])
            self.assertIs(summarize(logs)['result']['passed'], passed)

    def test_candidate_import_path_removes_runtime_modules_without_copying_packages(self):
        runtime = Path(unittest_adapter.__file__).resolve().parents[2]
        candidate = SimpleNamespace(__file__=str(self.root / 'loop_manager.py'))
        modules = {'loop_manager': candidate, 'loop_manager_internal.existing': candidate, 'third_party': candidate}
        with mock.patch.object(unittest_adapter.Path, 'cwd', return_value=self.root), \
                mock.patch.object(sys, 'modules', modules), mock.patch.object(sys, 'path', [str(runtime), '/dependency']):
            unittest_adapter.project_imports()
            self.assertNotIn('loop_manager', sys.modules)
            self.assertIn('third_party', sys.modules)
            self.assertEqual(sys.path, [str(self.root), '/dependency'])

    def test_versioned_nested_events_and_manifest_remain_comparable(self):
        logs = self.root / 'stage-0'
        logs.mkdir()
        writer = EventWriter(logs / 'worker.jsonl', clock=lambda: 1, identity={'run_id': 'run', 'unit_id': 'example'})
        writer.emit('test_start', id='a')
        writer.emit('test_end', id='a', duration=0, result='success')
        writer.close()
        summary = summarize(self.root)
        self.assertEqual(summary['counts'], {'success': 1})
        self.assertEqual(summary['tests'][0]['unit_id'], 'example')
        (self.root / 'manifest.json').write_text(json.dumps({'version': 1, 'stages': [{'id': 'tests', 'units': [{'ids': ['a']}]}]}))
        self.assertTrue(comparison([self.root], [summary])['scope_matches'])


class CandidateEnvironmentTests(unittest.TestCase):
    def test_child_python_path_keeps_guard_but_selects_candidate_dependencies(self):
        from loop_manager_internal.verification.guard import worker_environment
        prepared = worker_environment({'LOOP_MANAGER_VERIFY_PROJECT_ROOT': '/candidate', 'PYTHONPATH': '/dependency'})
        entries = prepared['PYTHONPATH'].split(os.pathsep)
        self.assertEqual(entries[1:], ['/candidate', '/dependency'])
        self.assertTrue(entries[0].endswith('child_guard'))

    def test_candidate_fixture_helpers_share_runtime_instrumentation_context(self):
        candidate = SimpleNamespace()
        with mock.patch.dict(sys.modules, {'loop_manager_internal.verification.timing': candidate}):
            unittest_adapter.bind_instrumentation()
        for name in ('_current_writer', '_fixture_timing_active', '_phase_names'):
            self.assertIs(getattr(candidate, name), getattr(unittest_adapter.instrumentation, name))

    def test_compile_targets_include_all_sources_and_reject_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'package'
            package.mkdir()
            (root / 'entry.py').write_text('pass')
            (package / 'module.py').write_text('pass')
            with mock.patch.object(unittest_adapter.py_compile, 'compile') as compile_source:
                unittest_adapter.compile_paths(root, [Path('entry.py'), Path('package')])
                self.assertEqual({call.args[0] for call in compile_source.call_args_list},
                                 {str(root / 'entry.py'), str(package / 'module.py')})
                self.assertTrue(all(call.kwargs['doraise'] for call in compile_source.call_args_list))
            with self.assertRaises(contract.ContractError):
                unittest_adapter.compile_paths(root, [Path('../outside.py')])


class CoordinatorIdentityTests(unittest.TestCase):
    def test_reservations_bind_run_unit_process_and_reject_invalid_identity_before_mutation(self):
        from loop_manager_internal.verification.coordination import VerifyCoordinator
        from loop_manager_internal.verification.resources import Snapshot, MIB
        identities = {123: 7}
        sampler = SimpleNamespace(sample=lambda: Snapshot(10, 4, 0, 4096 * MIB, 'fake'))
        coordinator = VerifyCoordinator(sampler=sampler, clock=lambda: 10, identity=identities.get)
        session, secret = coordinator.open(1)
        with self.assertRaises(RuntimeError):
            coordinator.request(session, secret, 'acquire', unit_id=[])
        self.assertFalse(coordinator.reservations)
        reply = coordinator.request(session, secret, 'acquire', run_id='run', unit_id='compile')
        token = reply['token']
        coordinator.request(session, secret, 'bind', token=token, pid=123)
        reservation = coordinator.reservations[token]
        self.assertEqual((reservation['run_id'], reservation['unit_id'], reservation['pid'], reservation['identity']),
                         ('run', 'compile', 123, 7))
        with self.assertRaises(RuntimeError):
            coordinator.request(session, secret, 'release', token=token)
        identities.clear()
        coordinator.request(session, secret, 'release', token=token)
        self.assertFalse(coordinator.reservations)


class EntrypointAndGuidanceTests(unittest.TestCase):
    def test_absolute_entrypoint_is_importable_without_project_package_installation(self):
        # A fresh interpreter is the contract: sys.path bootstrap must work outside the runtime cwd.
        import subprocess
        entrypoint = Path(runner.__file__).with_name('entrypoint.py').resolve()
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', str(entrypoint), '--version'],
                cwd=directory, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['version'], 1)
        self.assertEqual(set(json.loads(result.stdout)['adapters']), {'unittest'})

    def test_role_prompts_only_link_declaration_and_support_context(self):
        from loop_manager_internal.agents import agent_prompts
        for prompt in (agent_prompts.planner_prompt(None), agent_prompts.worker_prompt('plan', None),
                       agent_prompts.reviewer_prompt('main')):
            self.assertIn('scripts/verify.json', prompt)
            self.assertIn('docs/VERIFY.md', prompt)
            self.assertNotIn('테스트마다 자원 배정', prompt)


class ManagerDeadlineHandoffTests(unittest.TestCase):
    def test_coordinated_calls_inherit_missing_timeout_and_preserve_supplied_limit(self):
        for supplied, argv, expected in ((None, [], None), ('1200', [], 1200),
                                         ('1200', ['--timeout', '30'], 30)):
            environment = {'LOOP_MANAGER_VERIFY_SESSION': 'manager',
                           'LOOP_MANAGER_VERIFY_ENDPOINT': '["127.0.0.1", 1]',
                           'LOOP_MANAGER_VERIFY_SECRET': 'test'}
            if supplied is not None:
                environment['LOOP_MANAGER_VERIFY_TIMEOUT'] = supplied
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(sys, 'argv', ['runner', *argv]), mock.patch.object(runner, 'run', return_value=0) as run:
                self.assertEqual(runner.main(), 0)
                self.assertEqual(run.call_args.args[2], expected)

    def test_partial_coordination_cannot_disable_the_standalone_deadline(self):
        with mock.patch.dict(os.environ, {'LOOP_MANAGER_VERIFY_SESSION': 'manager'}, clear=True),                 mock.patch.object(sys, 'argv', ['runner']), mock.patch.object(runner, 'run') as run:
            with self.assertRaisesRegex(contract.ContractError, 'incomplete Manager'):
                runner.main()
            run.assert_not_called()

    def test_invalid_explicit_deadline_fails_before_execution(self):
        for value in ('0', '-1', 'nan', 'inf'):
            with mock.patch.dict(os.environ, {}, clear=True),                     mock.patch.object(sys, 'argv', ['runner', '--timeout=' + value]),                     mock.patch.object(sys, 'stderr', io.StringIO()), mock.patch.object(runner, 'run') as run:
                with self.assertRaises(SystemExit) as error:
                    runner.main()
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()

    def test_standalone_execute_keeps_default_timeout(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(sys, 'argv', ['runner', '--execute']), \
                mock.patch.object(runner, 'run', return_value=0) as run:
            self.assertEqual(runner.main(), 0)
        self.assertEqual(run.call_args.args[2], 600)

    def test_standalone_wrapper_keeps_configured_and_explicit_limits(self):
        for argv, expected in (([], 900), (['--timeout', '30'], 30)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'config').mkdir()
                (root / 'config/config.ini').write_text('[loop]\nverify_timeout_seconds = 900\n')
                with mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(sys, 'argv', ['runner', *argv]), \
                        mock.patch.object(runner.Path, 'cwd', return_value=root), \
                        mock.patch.object(runner.Path, 'is_file', return_value=True), \
                        mock.patch('loop_manager_internal.config.managed_config.load_managed_config',
                                   return_value=SimpleNamespace(verify_timeout_seconds=900, verify_parallelism=1)), \
                        mock.patch.object(verify_process, 'run_verify_process', return_value=(True, False, '')) as process, \
                        mock.patch.object(sys, 'stdout', io.StringIO()):
                    self.assertEqual(runner.main(), 0)
                    self.assertEqual(process.call_args.args[2], expected)
                    command = process.call_args.args[0]
                    self.assertEqual(float(command[command.index('--timeout') + 1]), expected)

    def test_compile_after_612_seconds_obeys_the_manager_limit_instead_of_a_new_default(self):
        stage = {'id': 'tests', 'kind': 'tests', 'cwd': '.', 'execution': 'independent'}
        compile_stage = {'id': 'compile', 'kind': 'command', 'cwd': '.', 'argv': ['compile']}
        for timeout in (None, 600):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                connection = contract.Connection(root, 'unittest', Path(sys.executable), (stage, compile_stage))
                clock = [0]
                def finish_tests(*args):
                    clock[0] = 612
                    return True
                with mock.patch.dict(os.environ, {'LOOP_MANAGER_VERIFY_SESSION': 'manager',
                        'LOOP_MANAGER_VERIFY_ENDPOINT': '["127.0.0.1", 1]', 'LOOP_MANAGER_VERIFY_SECRET': 'test'}, clear=True),                         mock.patch.object(runner, 'load', return_value=connection), mock.patch.object(runner, 'install_guard'),                         mock.patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]),                         mock.patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(stdout='revision')),                         mock.patch.object(unittest_adapter, 'collect_project', return_value=([manifest.Module('unit', ('a',))], sys.version)),                         mock.patch.object(unittest_adapter, 'execute', side_effect=finish_tests),                         mock.patch.object(runner, 'execute_command', return_value=True) as compile_command,                         mock.patch.object(sys, 'stdout', io.StringIO()):
                    if timeout is None:
                        self.assertEqual(runner.run(root / 'logs', 1, timeout), 0)
                        self.assertIsNone(compile_command.call_args.args[2])
                    else:
                        with self.assertRaisesRegex(TimeoutError, 'verify total timeout'):
                            runner.run(root / 'logs', 1, timeout)
                        compile_command.assert_not_called()

    def test_command_with_inherited_limit_preserves_binding_and_confirmed_release(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = EventWriter(Path(directory) / 'events.jsonl')
            client = mock.Mock()
            client.request.return_value = {'granted': True, 'reason': 'ready', 'token': 'owned'}
            def process(argv, cwd, timeout, **options):
                self.assertIsNone(timeout)
                options['on_started'](SimpleNamespace(pid=123), None)
                return True, False, verify_process.VerifyProcessOutput('', exit_code=0)
            try:
                self.assertTrue(core.execute_command(['compile'], Path(directory), None, writer, client,
                                                    'manager', 'compile', run_process=process))
            finally:
                writer.close()
            self.assertEqual([call.args[0] for call in client.request.call_args_list], ['acquire', 'bind', 'release'])

    def test_process_and_windows_job_wait_inherit_outer_deadline(self):
        from loop_manager_internal.execution import windows_job
        process = mock.Mock(pid=123, returncode=0)
        process.wait.return_value = 0
        job = mock.Mock()
        with mock.patch.object(verify_process.os, 'name', 'nt'), \
                mock.patch.object(verify_process.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0, create=True), \
                mock.patch.object(windows_job, 'WindowsJob', return_value=job):
            passed, timed_out, output = verify_process.run_verify_process(['compile'], Path('.'), None,
                launch_process=mock.Mock(return_value=process), job_scope=True)
        self.assertTrue(passed)
        self.assertFalse(timed_out)
        process.wait.assert_called_once_with(timeout=None)
        job.wait.assert_called_once_with(None)
        job.close.assert_called_once()
        fake_job = SimpleNamespace(active=mock.Mock(side_effect=[1, 0]))
        with mock.patch.object(windows_job.time, 'sleep'):
            windows_job.WindowsJob.wait(fake_job, None)


class ReviewBoundaryRegressionTests(unittest.TestCase):
    def test_windows_assignment_and_cleanup_failure_before_callback_retains_reservation(self):
        from loop_manager_internal.execution import windows_job
        from loop_manager_internal.verification.coordination import VerifyCoordinator
        from loop_manager_internal.verification.resources import Snapshot, MIB
        sampler = SimpleNamespace(sample=lambda: Snapshot(10, 4, 0, 4096 * MIB, 'fake'))
        coordinator = VerifyCoordinator(sampler=sampler, clock=lambda: 10)
        session, secret = coordinator.open(1)
        client = SimpleNamespace(request=lambda operation, **data: coordinator.request(session, secret, operation, **data))
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        job = mock.Mock()
        job.assign.side_effect = OSError('assignment failed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = EventWriter(root / 'events.jsonl')
            try:
                with mock.patch.object(verify_process, 'os', SimpleNamespace(name='nt')), \
                        mock.patch.object(verify_process.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0, create=True), \
                        mock.patch.object(windows_job, 'WindowsJob', return_value=job), \
                        mock.patch.object(verify_process.subprocess, 'Popen', return_value=process), \
                        mock.patch.object(verify_process, 'terminate_verify_process',
                                          side_effect=verify_process.VerifyTerminationUncertain('termination unknown')):
                    with self.assertRaises(verify_process.VerifyTerminationUncertain):
                        core.execute_command(['tool'], root, 10, writer, client, 'run', 'compile')
            finally:
                writer.close()
        self.assertEqual(len(coordinator.reservations), 1)
        reservation = next(iter(coordinator.reservations.values()))
        self.assertEqual(reservation['session'], session)
        self.assertEqual(reservation['unit_id'], 'compile')
        self.assertIsNone(reservation['pid'])
        process.stdin.write.assert_not_called()
        job.close.assert_called_once()

    def test_separate_candidate_imports_share_context_before_git_helpers_load(self):
        # Purging runtime imports requires a fresh interpreter; mock Git process effects.
        import shutil
        import subprocess
        runtime = Path(unittest_adapter.__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory)
            package = candidate / 'loop_manager_internal/verification'
            package.mkdir(parents=True)
            for relative in ('loop_manager_internal/__init__.py',
                             'loop_manager_internal/verification/__init__.py',
                             'loop_manager_internal/verification/timing.py',
                             'loop_manager_internal/verification/git_timing.py'):
                shutil.copyfile(runtime / relative, candidate / relative)
            code = '''
import dataclasses, pathlib, subprocess, sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
from loop_manager_internal.verification import unittest_adapter as adapter
adapter.project_imports()
from loop_manager_internal.verification import git_timing as candidate
from loop_manager_internal.verification import timing
assert pathlib.Path(candidate.__file__).is_relative_to(pathlib.Path(sys.argv[2]))
assert candidate._current_writer is adapter.instrumentation._current_writer
assert candidate._phase_names is adapter.instrumentation._phase_names
writer = adapter.instrumentation.EventWriter(pathlib.Path('events.jsonl'))
fields = ('git', 'git_bytes', '_worktree_catalog', '_handoff_run', 'run_config')
Effects = dataclasses.make_dataclass('Effects', [(name, object) for name in fields])
effects = Effects(*[lambda *args: 'mock result' for name in fields])
def start(process, args, *rest, **kwargs):
    process.returncode = None
def wait(process, *args, **kwargs):
    process.returncode = 0
    return 0
try:
    with writer.activate(), adapter.instrumentation.phase('candidate:cost'):
        with candidate.snapshot_timing(effects) as measured:
            measured.git(pathlib.Path('.'), 'status')
        with mock.patch.object(subprocess.Popen, '__init__', start), mock.patch.object(subprocess.Popen, 'wait', wait):
            with candidate.git_command_timing():
                process = subprocess.Popen(['git', 'status'])
                process.wait()
finally:
    writer.close()
summary = timing.summarize(pathlib.Path('.'))
assert sum(item['count'] for item in summary['git_commands']) == 1, summary
assert summary['git_commands'][0]['phases'] == ['candidate:cost'], summary
assert sum(cost['commands']['status']['count'] for cost in summary['git_snapshots'].values()) == 1, summary
assert not summary['active'], summary
'''
            result = subprocess.run([sys.executable, '-I', '-c', code, str(runtime), str(candidate)],
                cwd=candidate, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
