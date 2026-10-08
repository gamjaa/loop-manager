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
