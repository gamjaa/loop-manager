"""Impact decisions use complete old/new graphs, never filename matching."""
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest

from loop_manager_internal.maintenance import code_index
from loop_manager_internal.verification import impact
from loop_manager_internal.verification.manifest import Module
from tests.support.git import isolated_git_test


@isolated_git_test
class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.sources = {'pkg/value.py': b'def _value():\n    return 1\n',
            'pkg/consumer.py': b'from pkg import value\n',
            'tests/test_direct.py': b'from pkg import value\n',
            'tests/test_indirect.py': b'from pkg import consumer\n'}
        self.modules = [Module(name, (name + '.Case.test_value',))
                        for name in ('test_direct', 'test_indirect', *(f'test_other{i}' for i in range(6)))]
        self.sources.update({'tests/' + module.name + '.py': b'import unittest\n'
                             for module in self.modules if 'other' in module.name})

    def graph(self, sources):
        return code_index.resolve({path: code_index.parse_source(path, content)
                                   for path, content in sources.items()}, sources)

    def choose(self, changes=None, before=None, after=None, modules=None):
        return impact.select_records(self.graph(before or self.sources), self.graph(after or self.sources),
            changes or [('M', 'pkg/value.py', 'pkg/value.py')], modules or self.modules,
            head='head', base='base', configuration={'version': 1})

    def test_direct_and_transitive_consumers_with_changed_test(self):
        result = self.choose([('M', 'pkg/value.py', 'pkg/value.py'),
                              ('M', 'tests/test_other0.py', 'tests/test_other0.py')])
        self.assertEqual(result.modules, ('test_direct', 'test_indirect', 'test_other0'))
        self.assertFalse(result.fallback)
        self.assertEqual(result.total_tests, 8)
        self.assertEqual(len(result.ids), 3)
        self.assertTrue(result.reasons['test_indirect'])
        self.assertTrue(any('indirect_reference:pkg/value.py' in reason for reason in result.reasons['test_indirect']))
        self.assertTrue(any('direct_reference:pkg/value.py' in reason for reason in result.reasons['test_direct']))

    def test_filename_similarity_without_dependency_selects_nothing(self):
        self.sources['pkg/other0.py'] = b'def _value():\n    return 1\n'
        result = self.choose([('M', 'pkg/other0.py', 'pkg/other0.py')])
        self.assertFalse(result.modules)
        self.assertIn('no_tests_selected', result.fallback)

    def test_new_test_cases_are_not_implementation_public_api_changes(self):
        after = dict(self.sources)
        after['tests/test_other0.py'] = b'import unittest\nclass NewCase(unittest.TestCase):\n    def test_new(self):\n        pass\n'
        result = self.choose([('M', 'tests/test_other0.py', 'tests/test_other0.py')], after=after)
        self.assertEqual(result.modules, ('test_other0',))
        self.assertFalse(result.fallback)

    def test_deleted_module_retains_old_consumers_and_unresolved_import_falls_back(self):
        after = dict(self.sources)
        del after['pkg/value.py']
        result = self.choose([('D', 'pkg/value.py', None)], after=after)
        self.assertEqual(result.modules, ('test_direct', 'test_indirect'))
        self.assertTrue(any('unresolved_internal_import' in reason for reason in result.fallback))

    def test_rename_package_move_merges_consumers_from_both_trees(self):
        after = dict(self.sources)
        after['moved/value.py'] = after.pop('pkg/value.py')
        after['pkg/consumer.py'] = b'from moved import value\n'
        after['tests/test_direct.py'] = b'from moved import value\n'
        result = self.choose([('R100', 'pkg/value.py', 'moved/value.py')], after=after)
        self.assertEqual(result.modules, ('test_direct', 'test_indirect'))
        self.assertFalse(result.fallback)
        self.assertEqual(len(result.reasons['test_direct']), 2)

    def test_uncertain_unselected_consumer_cannot_hide_dynamic_relationship(self):
        for source in (b'__import__(name)\n', b'from pkg import missing\n', b'def broken(\n'):
            with self.subTest(source=source):
                self.sources['pkg/opaque.py'] = source
                self.assertTrue(any('uncertain_' in reason for reason in self.choose().fallback))

    def test_aliased_dynamic_import_reflection_and_runtime_module_selection(self):
        for source in (b'from importlib import import_module as load\nload(name)\n',
                       b'getattr(module, selected)\n', b'import sys\nsys.modules[name]\n',
                       b'import sys as runtime\nruntime.modules[name]\n',
                       b'import subprocess\nsubprocess.run(["python", "-m", selected])\n',
                       b'import runpy\nrunpy.run_module(name)\n'):
            with self.subTest(source=source):
                facts = impact.source_facts('pkg/opaque.py', source)
                self.assertIn('dynamic_import_or_reflection', facts['uncertainties'])

    def test_shared_support_risky_code_settings_entrypoint_and_initialization(self):
        for path in ('tests/support/fixture.py', 'pkg/git.py', 'pkg/state.py',
                     'pkg/recovery.py', 'pkg/permission.py', 'scripts/verify.json',
                     'pkg/__init__.py', 'loop_manager.py'):
            with self.subTest(path=path):
                self.assertTrue(self.choose([('M', path, path)]).fallback)

    def test_public_contract_change_is_conservative(self):
        after = dict(self.sources, **{'pkg/value.py': b'def public(value):\n    return value\n'})
        self.assertIn('public_api_changed:pkg/value.py', self.choose(after=after).fallback)

    def test_too_broad_counts_modules_and_tests(self):
        self.assertIn('selection_too_broad', self.choose(modules=self.modules[:4]).fallback)
        big = [replace(self.modules[0], ids=tuple(str(i) for i in range(100))), *self.modules[1:]]
        self.assertIn('selection_too_broad', self.choose(modules=big).fallback)

    def test_manifest_identity_and_selection_are_exact(self):
        selection = self.choose()
        self.assertEqual(impact.selected_modules(selection, self.modules), self.modules[:2])
        for altered in (list(reversed(self.modules)),
                        [replace(self.modules[0], ids=('other',)), *self.modules[1:]]):
            with self.assertRaises(ValueError):
                impact.selected_modules(selection, altered)
        with self.assertRaises(ValueError):
            impact.selected_modules(replace(selection, modules=('missing',)), self.modules)

    def test_discovery_support_imports_use_static_aliases_not_filename_similarity(self):
        sources = dict(self.sources)
        sources['tests/support/helper.py'] = b'from pkg import value\n'
        sources['tests/test_indirect.py'] = b'from support import helper\n'
        facts = {path: code_index.parse_source(path, content) for path, content in sources.items()}
        graph = code_index.resolve(facts, facts, module_aliases={'support.helper': 'tests/support/helper.py'})
        result = impact.select_records(graph, graph, [('M', 'pkg/value.py', 'pkg/value.py')], self.modules,
            head='head', base='base', configuration={})
        self.assertEqual(result.modules, ('test_direct', 'test_indirect'))
        self.assertFalse(result.fallback)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            code_index.resolve(facts, facts, module_aliases={'pkg.value': 'tests/support/helper.py'})

    def test_actual_git_trees_diff_and_candidate_change(self):
        # One Git scenario establishes immutable body reads and the candidate gate;
        # all selection policy combinations above use pure source graphs.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(['git', '-C', directory, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            for path, source in self.sources.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source)
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base')
            base = git('rev-parse', 'HEAD')
            (root / 'pkg/value.py').write_text('def _value():\n    return 2\n')
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'candidate')
            result = impact.select(root, base, self.modules, {'version': 1})
            self.assertFalse(result.fallback)
            self.assertEqual(result.modules, ('test_direct', 'test_indirect'))
            (root / 'pkg/value.py').write_text('def _value():\n    return 3\n')
            with self.assertRaisesRegex(ValueError, 'changed'):
                impact.assert_candidate(root, result.head, result.base)
            git('add', '.')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'new candidate')
            with self.assertRaisesRegex(ValueError, 'changed'):
                impact.assert_candidate(root, result.head, result.base)
