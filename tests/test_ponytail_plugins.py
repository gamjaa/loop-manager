"""Installed plugin identity, failure fallback and process-local lazy caching."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
from unittest import TestCase, mock

from loop_manager_internal.agents.ponytail_plugins import PonytailPlugins, ponytail_installed


def codex_entry(name='ponytail', *, installed=True, enabled=True):
    return {'pluginId': name + '@ponytail', 'name': name,
            'installed': installed, 'enabled': enabled}


def output(provider, entries):
    return json.dumps({'installed': entries, 'available': []} if provider == 'codex' else entries)


class PonytailPluginTests(TestCase):
    def test_official_codex_and_claude_json_ignore_activation(self):
        for enabled in (True, False):
            for provider, entries in (
                ('codex', [codex_entry(enabled=enabled)]),
                ('claude', [{'id': 'ponytail@ponytail', 'enabled': enabled, 'scope': 'user'}]),
            ):
                with self.subTest(provider=provider, enabled=enabled):
                    self.assertTrue(ponytail_installed(provider, output(provider, entries)))

    def test_available_uninstalled_and_similar_names_do_not_match(self):
        self.assertFalse(ponytail_installed('codex', json.dumps(
            {'installed': [], 'available': [codex_entry(installed=False)]})))
        self.assertFalse(ponytail_installed('codex', output('codex', [codex_entry(installed=False)])))
        for name in ('ponytail-review', 'not-ponytail', 'Ponytail'):
            self.assertFalse(ponytail_installed('codex', output('codex', [codex_entry(name)])))
            self.assertFalse(ponytail_installed('claude', output('claude', [{'id': name + '@ponytail'}])))

    def test_empty_successful_inventories_mean_not_installed(self):
        for provider in ('codex', 'claude'):
            self.assertFalse(ponytail_installed(provider, output(provider, [])))

    def test_unknown_shapes_and_incomplete_entries_are_not_a_successful_inventory(self):
        for provider, value in (
            ('codex', []), ('codex', {}), ('codex', {'installed': {}}),
            ('codex', {'installed': [{}]}), ('codex', {'installed': [codex_entry(installed='true')]}),
            ('codex', {'installed': [{**codex_entry(), 'name': 'different'}]}),
            ('claude', {}), ('claude', [None]), ('claude', [{}]),
            ('claude', [{'id': 'ponytail'}]), ('claude', [{'id': 'ponytail@'}]),
            ('claude', [{'id': '@ponytail'}]),
        ):
            with self.subTest(provider=provider, value=value), self.assertRaises(ValueError):
                ponytail_installed(provider, json.dumps(value))

    def test_queries_are_lazy_and_independent_and_once_even_for_concurrent_workers(self):
        run = mock.Mock(side_effect=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, output(argv[0], [codex_entry()] if argv[0] == 'codex' else [])))
        log = mock.Mock()
        inventory = PonytailPlugins(run=run, log=log)
        run.assert_not_called()
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(inventory.installed, ['codex', 'claude'] * 8))
        self.assertEqual(results, [True, False] * 8)
        self.assertEqual(run.call_count, 2)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [['codex', 'plugin', 'list', '--json'], ['claude', 'plugin', 'list', '--json']])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['timeout'], 15)
            self.assertEqual(call.kwargs['encoding'], 'utf-8')
        self.assertIn('설치 확인', log.call_args_list[0].args[0])
        self.assertIn('설치 없음', log.call_args_list[1].args[0])
        inventory.reset()
        self.assertTrue(inventory.installed('codex'))
        self.assertEqual(run.call_count, 4)

    def test_failed_probe_is_cached_but_does_not_erase_other_provider_installation(self):
        failures = (
            FileNotFoundError('CLI missing'), subprocess.TimeoutExpired(['codex'], 15),
            subprocess.CompletedProcess(['codex'], 2, 'unsupported'),
            subprocess.CompletedProcess(['codex'], 0, 'not JSON'),
            subprocess.CompletedProcess(['codex'], 0, '{}'),
            UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid'),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                def run(argv, **kwargs):
                    if argv[0] == 'codex':
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return subprocess.CompletedProcess(argv, 0, output('claude', [{'id': 'ponytail@ponytail'}]))
                execute, log = mock.Mock(side_effect=run), mock.Mock()
                inventory = PonytailPlugins(run=execute, log=log)
                for _ in range(2):
                    self.assertFalse(inventory.installed('codex'))
                    self.assertTrue(inventory.installed('claude'))
                self.assertEqual(execute.call_count, 2)
                self.assertIn('조회 실패', log.call_args_list[0].args[0])
                self.assertIn('Manager 지침 추가', log.call_args_list[0].args[0])

    def test_next_execution_refreshes_changed_installation(self):
        run = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0, output('codex', [])),
            subprocess.CompletedProcess([], 0, output('claude', [])),
            subprocess.CompletedProcess([], 0, output('codex', [codex_entry(enabled=False)])),
            subprocess.CompletedProcess([], 0, output('claude', [])),
        ])
        inventory = PonytailPlugins(run=run, log=mock.Mock())
        self.assertFalse(inventory.installed('codex'))
        self.assertFalse(inventory.installed('codex'))
        inventory.reset()
        self.assertTrue(inventory.installed('codex'))
        self.assertEqual(run.call_count, 4)
