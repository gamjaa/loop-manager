"""Partial runner retains discovery, fixtures, identity and process boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.verification import contract, manifest, runner, unittest_adapter
from loop_manager_internal.execution.verify_process import run_verify_process
from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
from loop_manager_internal.execution import result_validation
from loop_manager_internal.execution.verify_process import VerifyProcessOutput
from loop_manager_internal.verification import manager_adapter
from types import SimpleNamespace
import threading
from loop_manager_internal.verification.timing import comparison
from tests.support.git import isolated_git_test


@isolated_git_test
class PartialRunnerTests(unittest.TestCase):
    def test_timeout_normalization_preserves_partial_diagnostic_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            (root / 'scripts/verify.sh').write_text('verify')
            output = VerifyProcessOutput('timeout output', exit_code=1)
            report = {'kind': 'partial_verify', 'status': 'ERROR', 'diagnostic': '/logs/partial/summary.json'}
            output.partial_result = report
            with mock.patch.object(contract, 'load', return_value=object()), \
                    mock.patch.object(manager_adapter, 'run', return_value=(False, True, output)):
                passed, normalized = result_validation.verify(mock.Mock(), root, 1, partial_base='base')
            self.assertFalse(passed)
            self.assertTrue(normalized.timed_out)
            self.assertEqual(normalized.partial_result, report)

    def test_comparison_uses_executed_scope_not_the_full_discovery_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / 'partial', root / 'full']
            for index, path in enumerate(paths):
                path.mkdir()
                (path / 'manifest.json').write_text(json.dumps([
                    {'name': 'selected', 'ids': ['selected.Case.a'], 'status': 'SELECTED'},
                    {'name': 'other', 'ids': ['other.Case.a'], 'status': 'NOT_SELECTED' if index == 0 else 'SELECTED'}]))
            summaries = [{'metadata': {}, 'phases': [], 'active': [], 'result': {'passed': True}, 'counts': {}}] * 2
            result = comparison(paths, summaries)
            self.assertFalse(result['scope_matches'])
            self.assertFalse(result['comparable'])
    def test_manager_partial_timeout_and_unknown_termination_keep_normal_policy(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                coordinator = mock.Mock()
                coordinator.open.return_value = ('session', 'secret')
                coordinator.endpoint = {}
                coordinator.lock = threading.RLock()
                coordinator.reservations = {'owned': {'session': 'session'}}
                context = SimpleNamespace(coordinator=coordinator, cap=1, state_dir=root)
                effects = mock.Mock()
                if uncertain:
                    effects.run_verify_process.side_effect = VerifyTerminationUncertain('child exit unknown')
                else:
                    effects.run_verify_process.return_value = (False, True, 'timeout')
                with mock.patch.object(manager_adapter, 'settings', SimpleNamespace(get=lambda: context)):
                    if uncertain:
                        with self.assertRaises(VerifyTerminationUncertain):
                            manager_adapter.run(effects, ['verify'], root, 1, partial_base='base')
                        coordinator.finish.assert_called_once_with('session', stopped=False)
                        self.assertTrue((root / 'logs/verify/session/uncertain-reservations.json').is_file())
                    else:
                        passed, timed_out, output = manager_adapter.run(effects, ['verify'], root, 1, partial_base='base')
                        self.assertFalse(passed)
                        self.assertTrue(timed_out)
                        self.assertEqual(output.partial_result['status'], 'ERROR')
                        coordinator.finish.assert_called_once_with('session', stopped=True)
                environment = effects.run_verify_process.call_args.kwargs['environment']
                self.assertEqual(environment['LOOP_MANAGER_VERIFY_PARTIAL_BASE'], 'base')
                self.assertTrue(effects.run_verify_process.call_args.kwargs['job_scope'])

    def test_selected_report_requires_all_ids_and_fixture_skip_ownership(self):
        unit = manifest.Module('test_selected', ('test_selected.Case.a', 'test_selected.Case.b'),
            (('test_selected.Case.a', 'test_selected', 'test_selected.Case'),
             ('test_selected.Case.b', 'test_selected', 'test_selected.Case')))
        good = {'module': unit.name, 'passed': True, 'completed': list(unit.ids)}
        self.assertTrue(manifest.validate_report(unit, good))
        for report in ({**good, 'completed': [unit.ids[0]]},
                       {**good, 'completed': [unit.ids[0], unit.ids[0]]},
                       {**good, 'module': 'unselected'},
                       {**good, 'completed': [], 'fixture_skipped': list(unit.ids)}):
            with self.assertRaises(RuntimeError):
                manifest.validate_report(unit, report)
        self.assertTrue(manifest.validate_report(unit, {**good, 'completed': [],
            'fixture_skipped': list(unit.ids), 'fixture_skip_sources': ['setUpModule (test_selected)']}))
        self.assertFalse(manifest.validate_report(unit, {**good, 'passed': False, 'completed': []}))

    def test_serial_and_independent_partial_execution_use_existing_core(self):
        unit = manifest.Module('chosen', ('chosen.Case.a',))
        connection = contract.Connection(Path('/project'), 'unittest', Path(sys.executable), ())
        for mode in ('serial', 'independent'):
            with self.subTest(mode=mode), mock.patch('loop_manager_internal.verification.core.execute_manifest', return_value=True) as core:
                stage = {'id': 'tests', 'cwd': '.', 'execution': mode, 'collection': {'directory': 'tests'}}
                client = mock.Mock()
                client.request.return_value = {'granted': True, 'token': 'reservation'}
                writer = mock.Mock()
                self.assertTrue(unittest_adapter.execute(connection, stage, [unit], Path('/logs'),
                    writer, client, 'run', Path('/runtime/entrypoint.py')))
                self.assertEqual(core.call_args.args[0], [unit])
                selected_client = core.call_args.args[3]
                self.assertTrue(selected_client.request('acquire')['granted'])
                if mode == 'serial':
                    self.assertFalse(selected_client.request('acquire')['granted'])
                    self.assertTrue(selected_client.request('acquire', token='reservation')['granted'])
                validate = core.call_args.kwargs['validate_report']
                with self.assertRaisesRegex(RuntimeError, 'identity'):
                    validate(unit, {'version': 1, 'run_id': 'other', 'unit_id': unit.name, 'stage_id': 'tests'})

    def test_actual_partial_and_full_runs_preserve_manifest_and_separate_results(self):
        # Two actual runs prove subprocess discovery/execution and partial/full
        # separation. Policy combinations use existing unit adapters above.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'project'
            root.mkdir()
            (root / 'tests').mkdir()
            (root / 'pkg').mkdir()
            (root / 'scripts').mkdir()
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            (root / '.gitignore').write_text('__pycache__/\n')
            (root / 'pkg/value.py').write_text('def _value():\n    return 1\n')
            (root / 'tests/test_selected.py').write_text(
                'import unittest\nfrom pkg.value import _value\n'
                'class Case(unittest.TestCase):\n'
                '    def test_value(self):\n        self.assertEqual(_value(), 2)\n'
                '    @unittest.skip("intentional skip")\n'
                '    def test_skip(self):\n        pass\n')
            for index in range(5):
                (root / f'tests/test_other{index}.py').write_text(
                    'import unittest\nclass Case(unittest.TestCase):\n'
                    '    def test_ok(self):\n        self.assertTrue(True)\n')
            declaration = {'version': 1, 'adapter': 'unittest', 'environment': {'python': 'launcher'},
                'stages': [{'id': 'tests', 'kind': 'tests', 'cwd': '.',
                            'collection': {'directory': 'tests', 'pattern': 'test*.py'}, 'execution': 'serial'},
                           {'id': 'compile', 'kind': 'command', 'cwd': '.',
                            'argv': ['{python}', '{runner}', '--compile', 'pkg']}]}
            (root / 'scripts/verify.json').write_text(json.dumps(declaration))
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (root / 'pkg/value.py').write_text('def _value():\n    return 2\n')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'candidate')
            entrypoint = Path(runner.__file__).with_name('entrypoint.py')
            reports = []
            for mode in ('partial', 'full'):
                logs = Path(temporary) / mode
                environment = dict(os.environ, LOOP_MANAGER_VERIFY_LOG_DIR=str(logs))
                environment.pop('LOOP_MANAGER_VERIFY_PARTIAL_BASE', None)
                for name in ('LOOP_MANAGER_VERIFY_ENDPOINT', 'LOOP_MANAGER_VERIFY_SESSION', 'LOOP_MANAGER_VERIFY_SECRET'):
                    environment.pop(name, None)
                arguments = [sys.executable, str(entrypoint), '--execute', '--parallelism', '1', '--timeout', '30']
                if mode == 'partial':
                    arguments += ['--partial-base', base]
                passed, timed_out, output = run_verify_process(arguments, root, 30, environment=environment, job_scope=True)
                self.assertTrue(passed, str(output))
                self.assertFalse(timed_out)
                reports.append(json.loads((logs / 'manifest.json').read_text()))
                if mode == 'partial':
                    result = json.loads((logs / 'partial-result.json').read_text())
                    self.assertEqual(result['status'], 'PASS')
                    self.assertEqual(result['kind'], 'partial_verify')
                    self.assertEqual(result['selection']['modules'], ['test_selected'])
                    self.assertEqual(len(result['selection']['ids']), 2)
                    self.assertEqual(result['selection']['total_tests'], 7)
                    units = reports[-1]['stages'][0]['units']
                    self.assertEqual(sum(unit['status'] == 'NOT_SELECTED' for unit in units), 5)
                    events = [json.loads(line) for line in (logs / 'runner.jsonl').read_text().splitlines()]
                    self.assertEqual([e['unit_id'] for e in events if e['kind'] == 'module_result'], ['test_selected'])
                    self.assertFalse(any(e.get('unit_id') == 'compile' for e in events))
                else:
                    self.assertFalse((logs / 'partial-result.json').exists())
                    self.assertEqual(len(reports[-1]['stages']), 2)
            self.assertEqual([unit['payload'] for unit in reports[0]['stages'][0]['units']],
                             [unit['payload'] for unit in reports[1]['stages'][0]['units']])
