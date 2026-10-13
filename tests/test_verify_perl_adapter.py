"""Narrow Perl adapter conformance checks; never invoke the whole runner."""
import io
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.verification import contract, perl_adapter, runner
from loop_manager_internal.verification.manifest import Module
from loop_manager_internal.verification.timing import EventWriter, diagnostic


class PerlAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'tests').mkdir()
        # Contract/transport tests use an inert tool fixture, never a native executable.
        self.tool = str(self.root / 'perl executable')
        Path(self.tool).write_bytes(b'fixture')
        self.declaration = {'version': 1, 'adapter': 'perl-tap', 'environment': {'perl': self.tool},
            'stages': [{'id': 'tests', 'kind': 'tests', 'cwd': '.',
                'collection': {'directory': 'tests', 'pattern': '*.t'}, 'execution': 'independent'}]}
        self.stage = self.declaration['stages'][0]
        self.unit = Module('tests/example.t', ('tests/example.t',), pattern='*.t')
        self.identity = {'version': 1, 'run_id': 'run', 'stage_id': 'tests'}

    def connection(self):
        (self.root / 'scripts/verify.json').write_text(json.dumps(self.declaration))
        return contract.load(self.root, sys.executable)

    def select_native_tool(self):
        executable = shutil.which('perl')
        self.assertIsNotNone(executable, 'Perl is required for Linux native conformance tests')
        self.tool = str(Path(executable).absolute())
        self.declaration['environment']['perl'] = self.tool

    def native(self, source):
        path = self.root / self.unit.name
        path.write_text(source)
        result = subprocess.run([self.tool, str(perl_adapter.REPORTER), 'run', str(path), str(self.root / 'native.tap'),
            json.dumps({**self.identity, 'unit_id': self.unit.name})],
            capture_output=True, text=True, timeout=10)
        return [json.loads(line) for line in result.stdout.splitlines()], result.returncode

    def report(self, records, returncode):
        complete, passed = perl_adapter.native_report(records, returncode)
        return {**self.identity, 'unit_id': self.unit.name, 'module': self.unit.name,
            'native': records, 'exit_code': returncode, 'passed': passed,
            'completed': list(self.unit.ids) if complete else []}

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Perl native conformance currently supports Linux')
    def test_native_plan_skip_todo_and_subtest_meaning(self):
        self.select_native_tool()
        source = Path('tests/support/verify_examples/perl/tests/example.t').read_text()
        records, code = self.native(source)
        self.assertTrue(perl_adapter.validate_report(self.unit, self.report(records, code), self.identity))
        tests = [item for item in records if item['type'] == 'test']
        self.assertEqual(len(tests), 4)
        self.assertTrue(tests[1]['skip'])
        self.assertTrue(tests[2]['todo'])
        self.assertFalse(tests[2]['actual_ok'])
        self.assertTrue((self.root / 'native.tap').is_file())
        records, code = self.native('print "1..1\\nok 1\\nBail out! after completed plan\\n"; exit 0;')
        self.assertEqual(code, 0)
        self.assertTrue(any(record['type'] == 'bailout' for record in records))
        self.assertEqual(perl_adapter.native_report(records, code), (False, False))
        for source, expected in (("use Test::More; plan skip_all => 'fixture unavailable';", True),
                ("use Test::More tests => 2; ok(1);", False),
                ("use Test::More tests => 1; ok(0);", False),
                ("use Test::More tests => 1; ok(1); die 'fixture error';", False),
                ("use Test::More; BAIL_OUT('fixture failure');", False),
                ("use Test::More tests => 1; subtest nested => sub { ok(0); done_testing; };", False)):
            with self.subTest(source=source):
                records, code = self.native(source)
                self.assertEqual(perl_adapter.validate_report(self.unit, self.report(records, code), self.identity), expected)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Perl native conformance currently supports Linux')
    def test_perl_agent_network_and_parallel_boundaries_are_blocked_before_execution(self):
        self.select_native_tool()
        for statement in ("system('codex', '--version')", "exec('claude')", '`codex --version`',
                'fork()', "socket(my $socket, 2, 1, 0)", "open(my $pipe, '-|', 'codex')",
                "use IO::Socket::INET; IO::Socket::INET->new(PeerAddr => '127.0.0.1', PeerPort => 1, Timeout => 1)"):
            with self.subTest(statement=statement):
                records, code = self.native('use Test::More tests => 1; ' + statement + '; ok(1);')
                self.assertFalse(perl_adapter.native_report(records, code)[1])
                self.assertEqual(records[-1]['run'], 0)
        argv = perl_adapter.command_argv(self.connection(), ['{perl}', '-c', 'tests/example.t'])
        self.assertIn('-MLoopManagerVerifyGuard', argv)
        (self.root / 'tests/example.t').write_text("BEGIN { system('codex') };")
        result = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('blocked', result.stderr)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Perl native conformance currently supports Linux')
    def test_partial_duplicate_missing_and_identity_results_fail_closed(self):
        self.select_native_tool()
        records, code = self.native('use Test::More tests => 1; ok(1);')
        report = self.report(records, code)
        for key, value in (('version', True), ('run_id', 'old'), ('stage_id', 'wrong'),
                ('unit_id', 'wrong'), ('completed', []), ('passed', False)):
            with self.subTest(key=key), self.assertRaises(contract.ContractError):
                perl_adapter.validate_report(self.unit, {**report, key: value}, self.identity)
        with self.assertRaisesRegex(contract.ContractError, 'native result identity'):
            perl_adapter.validate_report(self.unit, {**report, 'native': [
                {**record, 'run_id': 'old'} for record in records]}, self.identity)
        with self.assertRaisesRegex(contract.ContractError, 'partial'):
            perl_adapter.native_report(records[:-1], 0)
        with self.assertRaises(contract.ContractError):
            perl_adapter.native_report(records + [records[-1]], 0)
        duplicated = [next(item for item in records if item["type"] == "test"), *records]
        self.assertFalse(perl_adapter.native_report(duplicated, 0)[1])
        self.assertFalse(perl_adapter.native_report(records, -9)[1])
        corrupted = [*records[:-1], {**records[-1], 'run': True}]
        with self.assertRaisesRegex(contract.ContractError, 'native result fields'):
            perl_adapter.native_report(corrupted, 0)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Perl native conformance currently supports Linux')
    def test_collection_probe_identity_scope_and_missing_tool(self):
        self.select_native_tool()
        (self.root / self.unit.name).write_text('use Test::More tests => 1; ok(1);')
        connection = self.connection()
        def command(argv, *args):
            perl_adapter.collect(json.loads(Path(argv[-1]).read_text()))
            return True
        units, version = perl_adapter.collect_project(connection, self.stage, self.root, 'run', Path('entrypoint'),
            10, command, mock.Mock(), mock.Mock())
        self.assertEqual(units, [self.unit])
        self.assertEqual(version['perl'], 'v5.38.2')
        with self.assertRaisesRegex(contract.ContractError, 'probe'):
            perl_adapter.collect_project(connection, self.stage, self.root, 'run', Path('entrypoint'),
                10, lambda *args: False, mock.Mock(), mock.Mock())
        def wrong_identity(argv, *args):
            path = self.root / 'collection.json'
            result = json.loads(path.read_text())
            result['run_id'] = 'old'
            path.write_text(json.dumps(result))
            return True
        with self.assertRaisesRegex(contract.ContractError, 'identity'):
            perl_adapter.collect_project(connection, self.stage, self.root, 'run', Path('entrypoint'),
                10, wrong_identity, mock.Mock(), mock.Mock())
        self.declaration['environment']['perl'] = str(self.root / 'missing-perl')
        with self.assertRaises(contract.ContractError):
            self.connection()
        self.assertIs(runner.IMPLEMENTATIONS['perl-tap'], perl_adapter)

    def test_execution_rejects_changed_file_scope(self):
        (self.root / self.unit.name).write_text('use Test::More tests => 1; ok(1);')
        connection = self.connection()
        (self.root / 'tests/added.t').write_text('use Test::More tests => 1; ok(1);')
        with self.assertRaisesRegex(contract.ContractError, 'scope changed'):
            perl_adapter.execute(connection, self.stage, [self.unit], self.root, mock.Mock(), mock.Mock(), 'run', Path('entrypoint'))

    def test_common_process_transport_and_serial_budget(self):
        (self.root / self.unit.name).write_text('use Test::More tests => 1; ok(1);')
        connection = self.connection()
        parent = mock.Mock()
        parent.request.return_value = {'granted': True, 'reason': 'test', 'token': 'token'}
        def execute(units, logs, writer, client, **kwargs):
            self.assertTrue(client.request('acquire')['granted'])
            self.assertFalse(client.request('acquire')['granted'])
            self.assertTrue(client.request('acquire', token='token')['granted'])
            kwargs['spawn']('token', 0, logs)
            self.assertIs(kwargs['stop_worker'], perl_adapter.stop_worker)
            return True
        writer = EventWriter(self.root / 'events.jsonl')
        self.addCleanup(writer.close)
        with \
                mock.patch.object(perl_adapter, 'execute_manifest', side_effect=execute), \
                mock.patch.object(perl_adapter.unittest_adapter, 'start_worker') as spawn:
            stage = {**self.stage, 'execution': 'serial'}
            self.assertTrue(perl_adapter.execute(connection, stage, [self.unit], self.root, writer, parent, 'run', Path('entrypoint')))
            self.assertEqual(spawn.call_args.kwargs['command'], [sys.executable, '-u', 'entrypoint', '--perl-worker'])
            self.assertEqual(spawn.call_args.kwargs['environment']['LOOP_MANAGER_VERIFY_PERL'], self.tool)

    def test_both_examples_load_ordered_required_compile_and_tests(self):
        for language in ('python', 'perl'):
            root = self.root / language
            shutil.copytree(Path('tests/support/verify_examples') / language, root)
            if language == 'perl':
                declaration = root / 'scripts/verify.json'
                data = json.loads(declaration.read_text())
                data['environment']['perl'] = self.tool
                declaration.write_text(json.dumps(data))
            connection = contract.load(root, sys.executable)
            self.assertEqual([stage['kind'] for stage in connection.stages], ['command', 'tests'])

    def test_worker_start_or_communication_failure_preserves_partial_diagnostics(self):
        request = {**self.identity, 'unit_id': self.unit.name, 'module': self.unit.name,
            'ids': self.unit.ids, 'pattern': '*.t', 'owners': [], 'worker': 0,
            'directory': str(self.root), 'result': str(self.root / 'result.json')}
        for failure in (OSError('tool start failure'), RuntimeError('reporter communication failure')):
            with self.subTest(failure=failure), mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(request) + '\n')), \
                    mock.patch.dict(os.environ, LOOP_MANAGER_VERIFY_PERL=self.tool,
                        LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(self.root)), \
                    mock.patch.object(perl_adapter.subprocess, 'run', side_effect=failure), \
                    mock.patch.object(sys, 'stderr', io.StringIO()):
                self.assertEqual(perl_adapter.worker_main(), 0)
                report = json.loads((self.root / 'result.json').read_text())
                self.assertTrue(report['runner_error'])
                self.assertFalse(report['passed'])
                self.assertEqual(report['completed'], [])
                self.assertTrue((self.root / 'result.native.ndjson').is_file())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Perl native conformance currently supports Linux')
    def test_worker_native_success_and_interruption_keep_identity_and_partial_output(self):
        self.select_native_tool()
        (self.root / self.unit.name).write_text('use Test::More tests => 1; ok(1);')
        request = {**self.identity, 'unit_id': self.unit.name, 'module': self.unit.name,
            'ids': self.unit.ids, 'pattern': '*.t', 'owners': [], 'worker': 0,
            'directory': str(self.root), 'result': str(self.root / 'result.json')}
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(request) + '\n')), \
                mock.patch.dict(os.environ, LOOP_MANAGER_VERIFY_PERL=self.tool,
                    LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(self.root)):
            self.assertEqual(perl_adapter.worker_main(), 0)
        report = json.loads((self.root / 'result.json').read_text())
        self.assertTrue(perl_adapter.validate_report(self.unit, report, self.identity))
        native = [json.loads(line) for line in (self.root / 'result.native.ndjson').read_text().splitlines()]
        self.assertTrue(all(item['run_id'] == 'run' and item['unit_id'] == self.unit.name for item in native))
        diagnostic(self.root)
        summary = json.loads((self.root / 'summary.json').read_text())
        self.assertTrue(any(event['name'] == 'module:' + self.unit.name
            and event['result'] == 'complete' for event in summary['phases']))
        (self.root / 'result.json').unlink()
        def interrupted(argv, **kwargs):
            kwargs['stdout'].write(json.dumps(native[0]) + '\n')
            kwargs['stdout'].flush()
            raise KeyboardInterrupt()
        with mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(request) + '\n')), \
                mock.patch.dict(os.environ, LOOP_MANAGER_VERIFY_PERL=self.tool,
                    LOOP_MANAGER_VERIFY_PROJECT_ROOT=str(self.root)), \
                mock.patch.object(perl_adapter.subprocess, 'run', side_effect=interrupted), \
                self.assertRaises(KeyboardInterrupt):
            perl_adapter.worker_main()
        self.assertFalse((self.root / 'result.json').exists())
        self.assertEqual(json.loads((self.root / 'result.native.ndjson').read_text()), native[0])
        diagnostic(self.root)
        summary = json.loads((self.root / 'summary.json').read_text())
        self.assertTrue(any(event['name'] == 'module:' + self.unit.name
            and event['result'] == 'error' for event in summary['phases']))

    def test_runner_dispatches_perl_compile_manifest_and_partial_fallback(self):
        from types import SimpleNamespace
        self.declaration['stages'].insert(0, {'id': 'compile', 'kind': 'command', 'cwd': '.',
            'argv': ['{perl}', '-c', 'tests/example.t']})
        self.connection()
        for partial in (False, True):
            logs = self.root / str(partial)
            with mock.patch.object(runner.Path, 'cwd', return_value=self.root), \
                    mock.patch.object(runner, 'install_guard'), mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(runner, 'VerifyCoordinator') as coordinator, \
                    mock.patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(stdout='revision')), \
                    mock.patch.object(perl_adapter, 'collect_project', return_value=([self.unit], {'perl': 'v5.38.2'})) as collect, \
                    mock.patch.object(perl_adapter, 'execute', return_value=True) as execute, \
                    mock.patch.object(runner, 'execute_command', return_value=True) as command, \
                    mock.patch.object(sys, 'stdout', io.StringIO()):
                coordinator.return_value.open.return_value = ('session', 'secret')
                coordinator.return_value.reservations = {}
                self.assertEqual(runner.run(logs, 1, project_python=sys.executable,
                    partial_base='base' if partial else None), 0)
                if partial:
                    command.assert_not_called()
                    collect.assert_not_called()
                    execute.assert_not_called()
                    self.assertEqual(json.loads((logs / 'partial-result.json').read_text())['status'], 'FALLBACK')
                else:
                    self.assertEqual(command.call_args.args[0][:3], [self.tool,
                        '-I' + str(perl_adapter.REPORTER.parent), '-MLoopManagerVerifyGuard'])
                    manifest = json.loads((logs / 'manifest.json').read_text())
                    self.assertEqual(manifest['adapter'], 'perl-tap')
                    self.assertEqual(manifest['stages'][1]['tool']['perl'], 'v5.38.2')
                    self.assertEqual(manifest['stages'][1]['units'][0]['argv'][-1], '--perl-worker')

    def test_completed_plan_bailout_with_zero_exit_never_completes(self):
        records = [
            {'type': 'plan', 'raw': '1..1'},
            {'type': 'test', 'number': 1, 'ok': True, 'actual_ok': True, 'skip': False, 'todo': False},
            {'type': 'bailout', 'raw': 'Bail out! stop after completed plan'},
            {'type': 'summary', 'planned': 1, 'run': 1, 'good_plan': True, 'problems': False,
                'errors': [], 'exit': 0, 'wait': 0, 'skip_all': None},
        ]
        records = [{**self.identity, 'unit_id': self.unit.name, **record} for record in records]
        self.assertEqual(perl_adapter.native_report(records, 0), (False, False))
        report = self.report(records, 0)
        self.assertEqual(report['completed'], [])
        self.assertFalse(perl_adapter.validate_report(self.unit, report, self.identity))

    def test_windows_keeps_portable_unit_and_python_example_checks(self):
        import runpy
        # Check collection policy without executing either integration scenario.
        with mock.patch.object(sys, 'platform', 'win32'):
            unit_class = runpy.run_path(str(Path(__file__)))['PerlAdapterTests']
            integration_class = runpy.run_path(str(Path(__file__).with_name('test_verify_language_examples.py')))['LanguageExampleIntegrationTests']
        for name in ('test_native_plan_skip_todo_and_subtest_meaning',
                'test_perl_agent_network_and_parallel_boundaries_are_blocked_before_execution',
                'test_partial_duplicate_missing_and_identity_results_fail_closed',
                'test_collection_probe_identity_scope_and_missing_tool',
                'test_worker_native_success_and_interruption_keep_identity_and_partial_output'):
            self.assertTrue(getattr(unit_class, name).__unittest_skip__)
        for name in ('test_common_process_transport_and_serial_budget',
                'test_both_examples_load_ordered_required_compile_and_tests',
                'test_completed_plan_bailout_with_zero_exit_never_completes'):
            self.assertFalse(getattr(getattr(unit_class, name), '__unittest_skip__', False))
        self.assertFalse(getattr(integration_class.test_python_standalone_and_manager_preserve_success_and_native_failure,
            '__unittest_skip__', False))
        self.assertTrue(integration_class.test_perl_standalone_and_manager_preserve_success_and_native_failure.__unittest_skip__)
