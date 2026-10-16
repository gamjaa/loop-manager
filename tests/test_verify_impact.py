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
        return impact.resolve_records({path: impact.source_facts(path, content)
                                       for path, content in sources.items()})

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

    def test_deleted_module_retains_old_consumers_and_records_unresolved_import(self):
        after = dict(self.sources)
        del after['pkg/value.py']
        result = self.choose([('D', 'pkg/value.py', None)], after=after)
        self.assertEqual(result.modules, ('test_direct', 'test_indirect'))
        self.assertFalse(result.fallback)
        self.assertTrue(any('unresolved_internal_import' in reason for reasons in result.analysis['remaining_uncertainties'].values() for reason in reasons))

    def test_rename_package_move_merges_consumers_from_both_trees(self):
        after = dict(self.sources)
        after['moved/value.py'] = after.pop('pkg/value.py')
        after['pkg/consumer.py'] = b'from moved import value\n'
        after['tests/test_direct.py'] = b'from moved import value\n'
        result = self.choose([('R100', 'pkg/value.py', 'moved/value.py')], after=after)
        self.assertEqual(result.modules, ('test_direct', 'test_indirect'))
        self.assertFalse(result.fallback)
        self.assertEqual(len(result.reasons['test_direct']), 2)

    def test_global_uncertainty_is_recorded_without_blocking_precheck(self):
        for source in (b'__import__(name)\n', b'from pkg import missing\n', b'def broken(\n'):
            with self.subTest(source=source):
                self.sources['pkg/opaque.py'] = source
                self.assertFalse(self.choose().fallback)
                self.assertTrue(self.choose().analysis['remaining_uncertainties'])

    def test_aliased_dynamic_import_reflection_and_runtime_module_selection(self):
        for source in (b'from importlib import import_module as load\nload(name)\n',
                       b'getattr(module, selected)\n', b'import sys\nsys.modules[name]\n',
                       b'import sys as runtime\nruntime.modules[name]\n',
                       b'import subprocess\nsubprocess.run(["python", "-m", selected])\n',
                       b'import runpy\nrunpy.run_module(name)\n'):
            with self.subTest(source=source):
                facts = impact.source_facts('pkg/opaque.py', source)
                self.assertTrue(facts['impact']['uncertainties'])

    def test_shared_support_risky_code_settings_entrypoint_and_initialization(self):
        for path in ('loop_manager_internal/storage/schema.py',
                     'loop_manager_internal/git/workspace.py', 'scripts/verify.json',
                     'pkg/__init__.py', 'loop_manager.py'):
            with self.subTest(path=path):
                self.assertTrue(self.choose([('M', path, path)]).fallback)

    def test_public_contract_change_expands_known_consumers(self):
        after = dict(self.sources, **{'pkg/value.py': b'def public(value):\n    return value\n'})
        result = self.choose(after=after)
        self.assertFalse(result.fallback)
        self.assertEqual(result.modules, ('test_direct', 'test_indirect'))
        self.assertIn('public_api_changed:pkg/value.py', result.analysis['expansions'])

    def scoped(self, *, before=None, after=None, changed=None, documents=None, configuration=None, files=None):
        return impact.select_records(self.graph(before or self.sources), self.graph(after or self.sources),
            changed or [('M', 'pkg/value.py', 'pkg/value.py')], self.modules,
            head='head', base='base', configuration=configuration or {'stages': [{'kind': 'tests'}]},
            documents=documents, files=files)

    def test_code_and_review_document_keep_all_document_evidence(self):
        path = 'docs/requirements/[R-086] Impact.md'
        versions = {'base': ('100644', b'# old'), 'head': ('100644', b'# new')}
        selected = self.scoped(changed=[('M', 'pkg/value.py', 'pkg/value.py'), ('M', path, path)],
                               documents={path: versions})
        self.assertFalse(selected.fallback)
        self.assertEqual(selected.modules, ('test_direct', 'test_indirect'))
        proof = selected.analysis['documents'][0]
        self.assertEqual(proof['path'], path)
        self.assertNotEqual(proof['source_identity']['base'], proof['source_identity']['head'])
        only_document = self.scoped(changed=[('M', path, path)], documents={path: versions})
        self.assertIn('no_tests_selected', only_document.fallback)

    def test_document_roles_require_actual_sources_and_bounded_consumption(self):
        path = 'docs/plans/[G-086] Plan.md'
        document = {path: {'head': ('100644', b'# plan')}}
        for consumer in (b'from pathlib import Path\nPath("docs/plans/[G-086] Plan.md").read_text()\n',
                         b'from pathlib import Path\nPath(location).read_text()\n',
                         b'from pathlib import Path\nPath(location).open("r")\n',
                         b'from pathlib import Path\n(Path("") / location).read_text()\n'):
            with self.subTest(consumer=consumer):
                sources = dict(self.sources, **{'pkg/reader.py': consumer})
                sources['tests/test_other0.py'] = b'from pkg import reader\n'
                result = self.scoped(before=sources, after=sources,
                    changed=[('M', 'pkg/value.py', 'pkg/value.py'), ('A', None, path)], documents=document)
                self.assertFalse(result.fallback)
                self.assertTrue(any(reason.startswith('document_') for reason in result.analysis['scope_limits']))
        for versions in (None, {path: {'head': ('120000', b'../code.py')}},
                         {path: {'head': ('100644', b'\xff')}}, {path: {'head': ('100644', b'\0')}}):
            with self.subTest(versions=versions):
                self.assertTrue(self.scoped(changed=[('A', None, path)], documents=versions).fallback)
        for other in ('README.md', 'docs/AGENTS.reviewer.md', 'tests/fixtures/input.md', 'config/config.ini'):
            with self.subTest(other=other):
                self.assertIn('non_python_input:' + other, self.scoped(changed=[('A', None, other)]).analysis['scope_limits'])

    def test_document_command_input_and_unknown_configuration_are_not_excluded(self):
        path = 'docs/PRODUCT.md'
        for configuration in ({'version': 1}, {'stages': [{'kind': 'command', 'argv': ['validate', 'docs']}]},
                              {'stages': [{'kind': 'command', 'argv': ['{python}', 'check.py', '--compile', 'pkg']}]},
                              {'stages': [{'kind': 'command', 'argv': ['{python}', 'check.py']}]}):
            with self.subTest(configuration=configuration):
                result = self.scoped(changed=[('A', None, path)], configuration=configuration,
                    documents={path: {'head': ('100644', b'# Product')}})
                self.assertTrue(any('document_command_consumption_unproven' in reason for reason in result.analysis['scope_limits']))
        context = self.scoped(changed=[('A', None, path)],
            configuration={'stages': [{'kind': 'tests', 'cwd': 'elsewhere'}]},
            documents={path: {'head': ('100644', b'# Product')}})
        self.assertIn('document_execution_context_unproven:' + path, context.analysis['scope_limits'])

    def test_document_reads_include_aliases_enumeration_metadata_and_copy(self):
        path = 'docs/PRODUCT.md'
        for consumer in (b'from builtins import open as read\nread("docs/PRODUCT.md")\n',
                         b'from pathlib import Path\nread = Path("docs/PRODUCT.md").read_text\nread()\n',
                         b'from pathlib import Path\nPath("docs").glob("*.md")\n',
                         b'from pathlib import Path\nPath("docs/PRODUCT.md").stat()\n',
                         b'import shutil\nshutil.copyfile("docs/PRODUCT.md", "out")\n'):
            with self.subTest(consumer=consumer):
                sources = dict(self.sources, **{'pkg/reader.py': consumer,
                    'tests/test_other0.py': b'from pkg import reader\n'})
                result = self.scoped(before=sources, after=sources,
                    changed=[('M', 'pkg/value.py', 'pkg/value.py'), ('A', None, path)],
                    documents={path: {'head': ('100644', b'# Product')}})
                self.assertFalse(result.fallback)
                self.assertTrue(any(reason.startswith('document_') for reason in result.analysis['scope_limits']))

    def test_document_exclusion_requires_other_read_sources_to_be_regular(self):
        path = 'docs/PRODUCT.md'
        sources = dict(self.sources, **{'pkg/reader.py': b'from pathlib import Path\nPath("data.json").read_text()\n',
            'tests/test_other0.py': b'from pkg import reader\n'})
        for mode in ('100644', '120000', None):
            with self.subTest(mode=mode):
                result = self.scoped(before=sources, after=sources,
                    changed=[('M', 'pkg/value.py', 'pkg/value.py'), ('A', None, path)],
                    documents={path: {'head': ('100644', b'# Product')}},
                    files={'data.json': {'base': mode, 'head': mode}})
                if mode == '100644':
                    self.assertFalse(result.fallback)
                else:
                    self.assertFalse(result.fallback)
                    self.assertTrue(any('document_read_source_unproven' in reason for reason in result.analysis['scope_limits']))
        alias = dict(sources)
        alias['pkg/reader.py'] = b'from pathlib import Path\nPath("data.json").symlink_to("docs/PRODUCT.md")\nPath("data.json").read_text()\n'
        result = self.scoped(before=alias, after=alias, changed=[('A', None, path)],
            documents={path: {'head': ('100644', b'# Product')}},
            files={'data.json': {'base': '100644', 'head': '100644'}})
        self.assertTrue(any('document_consumption_unproven' in reason for reason in result.analysis['scope_limits']))

    def test_repeated_resolution_preserves_source_facts_and_same_selection(self):
        facts = {path: impact.source_facts(path, content) for path, content in self.sources.items()}
        original = code_index.encode(facts)
        first = impact.resolve_records(facts)
        second = impact.resolve_records(facts)
        self.assertEqual(code_index.encode(first), code_index.encode(second))
        self.assertEqual(code_index.encode(facts), original)

    def test_literal_and_finite_dynamic_imports_add_their_hidden_consumers(self):
        for loader in (b'import importlib as loader\nloader.import_module("pkg.value")\n',
                       b'from importlib import import_module as load\nload("pkg." + "value")\n',
                       b'for target in ("pkg.value", "unittest"):\n    __import__(target)\n'):
            with self.subTest(loader=loader):
                sources = dict(self.sources, **{'pkg/opaque.py': loader,
                    'tests/test_other0.py': b'from pkg import opaque\n'})
                result = self.scoped(before=sources, after=sources)
                self.assertFalse(result.fallback)
                self.assertEqual(result.modules, ('test_direct', 'test_indirect', 'test_other0'))
                self.assertTrue(result.analysis['resolved']['head:pkg/opaque.py'])

    def test_original_index_uncertainty_is_preserved_and_unresolved_target_rejected(self):
        sources = dict(self.sources, **{'pkg/opaque.py': b'__import__("pkg.value")\n'})
        facts = self.graph(sources)['pkg/opaque.py']
        self.assertIn('dynamic_reference', facts['raw_uncertainties'])
        self.assertIn('dynamic_reference', facts['uncertainties'])
        self.assertFalse(facts['selection_uncertainties'])
        sources['pkg/opaque.py'] = b'__import__("pkg.missing")\n'
        result = self.scoped(before=sources, after=sources)
        self.assertFalse(result.fallback)
        self.assertTrue(any('unresolved_bounded_import' in reason for reasons in result.analysis['remaining_uncertainties'].values() for reason in reasons))

    def test_escaped_shadowed_and_unbounded_loaders_record_scope_limits(self):
        for loader in (b'from importlib import import_module as load\nother = load\nother(name)\n',
                       b'from importlib import import_module as load\ndef f(load):\n    load(name)\n',
                       b'from importlib import import_module as load\nfrom other import fn as load\nload(name)\n',
                       b'from importlib import import_module as load\nload(target)\n',
                       b'def local():\n    target = "unittest"\ndef other():\n    __import__(target)\n',
                       b'for target in ("unittest",):\n    pass\n__import__(target)\n',
                       b'__import__("value", {"__package__": "pkg"}, {}, [], 1)\n',
                       b'import builtins\ngetattr(builtins, name)(target)\n',
                       b'code = "import pkg.value"\nexec(code)\n'):
            with self.subTest(loader=loader):
                sources = dict(self.sources, **{'pkg/unselected.py': loader})
                result = self.scoped(before=sources, after=sources)
                self.assertFalse(result.fallback)
                self.assertTrue(result.analysis['remaining_uncertainties'])

    def test_known_module_reflection_registry_and_external_version_have_proofs(self):
        for source in (b'from pkg import value\ngetattr(value, "_value")\n',
                       b'import pkg.value\nhasattr(pkg.value, "_value")\n',
                       b'import sys as runtime\nruntime.modules["pkg.value"]\n',
                       b'import sys as runtime\nruntime.modules.get("pkg.value")\n',
                       b'import subprocess as process\nprocess.run(["git", "--version"])\n',
                       b'import sys, subprocess\nsubprocess.run([sys.executable, "-m", "pkg.value"])\n'):
            with self.subTest(source=source):
                facts = impact.source_facts('pkg/known.py', source)
                self.assertFalse(facts['impact']['uncertainties'])
                self.assertTrue(facts['impact']['resolved'])
        for source in (b'import sys\nsys.modules[key]\n',
                       b'import sys\nsys.modules.get(key)\n',
                       b'import sys\nlookup = sys.modules.get\nlookup(key)\n',
                       b'import sys\nsys.modules.values()\n',
                       b'import sys\nsys.modules["pkg.value"] = fake\n',
                       b'import subprocess\nsubprocess.run(["git", "--version"], **options)\n',
                       b'import subprocess\nsubprocess.run(["git", "--version"], shell=True)\n',
                       b'import subprocess\nsubprocess.run(["git", "commit"])\n'):
            with self.subTest(source=source):
                self.assertTrue(impact.source_facts('pkg/unknown.py', source)['impact']['uncertainties'])

    def test_support_changes_expand_consumers_without_path_word_rejection(self):
        sources = dict(self.sources)
        sources['tests/support/state.py'] = b'def value():\n    return 1\n'
        sources['tests/test_other0.py'] = b'from support.state import value\n'
        result = self.scoped(before=sources, after=sources,
            changed=[('M', 'tests/support/state.py', 'tests/support/state.py')])
        self.assertFalse(result.fallback)
        self.assertEqual(result.modules, ('test_other0',))
        self.assertIn('test_support_consumers:tests/support/state.py', result.analysis['expansions'])
        sources['pkg/state.py'] = b'def value():\n    return 1\n'
        sources['tests/test_other0.py'] = b'from pkg.state import value\n'
        self.assertFalse(self.scoped(before=sources, after=sources,
            changed=[('M', 'pkg/state.py', 'pkg/state.py')]).fallback)

    def test_common_risk_paths_and_effects_expand_instead_of_falling_back(self):
        for path, body in (('loop_manager_internal/storage/value.py', b'def value():\n    return 1\n'),
                           ('pkg/value.py', b'def value(db):\n    db.commit()\n'),
                           ('pkg/value.py', b'import sqlite3\ndef value():\n    return 1\n')):
            with self.subTest(path=path):
                sources = dict(self.sources, **{path: body})
                sources['tests/test_direct.py'] = ('import ' + code_index.module_name(path) + '\n').encode()
                result = self.scoped(before=sources, after=sources, changed=[('M', path, path)])
                self.assertFalse(result.fallback)
                self.assertTrue(any(reason.startswith(('shared_high_risk_code:', 'boundary_code:', 'database_code:')) for reason in result.analysis['expansions']))

    def test_passive_facade_uses_the_reexport_owner_and_preserves_initialization(self):
        sources = dict(self.sources)
        sources['pkg/other.py'] = b'def other():\n    return 1\n'
        sources['pkg/api.py'] = b'from pkg.value import _value as value\nfrom pkg.other import other\n'
        sources['tests/test_direct.py'] = b'import pkg.api as api\napi.value()\n'
        sources['tests/test_indirect.py'] = b'from pkg.api import other\nother()\n'
        result = self.scoped(before=sources, after=sources)
        self.assertEqual(result.modules, ('test_direct',))
        self.assertFalse(result.fallback)
        self.assertTrue(any(proof['kind'] == 'passive_reexport' for proof in result.analysis['resolved']['head:tests/test_direct.py']))
        for effect in (b'initialize()\n', b'tokens = [initialize()]\n',
                       b'def _value(arg=initialize()):\n    return arg\n'):
            with self.subTest(effect=effect):
                changed = dict(sources)
                changed['pkg/value.py'] = effect
                preserved = self.scoped(before=changed, after=changed)
                self.assertIn('test_indirect', preserved.modules)

    def test_facade_escape_and_dynamic_attribute_keep_the_broad_relation(self):
        sources = dict(self.sources, **{'pkg/other.py': b'def other():\n    return 1\n',
            'pkg/api.py': b'from pkg.value import _value as value\nfrom pkg.other import other\n'})
        for consumer in (b'import pkg.api as api\nconsume(api)\n',
                         b'import pkg.api as api\ngetattr(api, name)\n'):
            with self.subTest(consumer=consumer):
                sources['tests/test_other0.py'] = consumer
                result = self.scoped(before=sources, after=sources)
                self.assertIn('test_other0', result.modules)
                if b'getattr' in consumer:
                    self.assertFalse(result.fallback)
                    self.assertTrue(result.analysis['remaining_uncertainties'])

    def test_all_facade_aliases_and_facade_itself_retain_their_consumers(self):
        sources = dict(self.sources, **{'pkg/other.py': b'def other():\n    return 1\n',
            'pkg/api.py': b'from pkg.value import _value as value\nfrom pkg.other import other\n'})
        sources['tests/test_other0.py'] = b'import pkg.api as first\nimport pkg.api as second\nfirst.value()\nsecond.other()\n'
        result = self.scoped(before=sources, after=sources, changed=[('M', 'pkg/other.py', 'pkg/other.py')])
        self.assertIn('test_other0', result.modules)
        facade = self.scoped(before=sources, after=sources, changed=[('M', 'pkg/api.py', 'pkg/api.py')])
        self.assertIn('test_other0', facade.modules)
        self.assertFalse(facade.fallback)

    def test_facade_refinement_keeps_implicit_package_initialization(self):
        sources = dict(self.sources, **{'pkg/other.py': b'def other():\n    return 1\n',
            'pkg/__init__.py': b'from pkg.value import _value\ncache = _value()\n',
            'pkg/api.py': b'from pkg.value import _value as value\nfrom pkg.other import other\n',
            'tests/test_direct.py': b'from pkg.api import value\nvalue()\n',
            'tests/test_indirect.py': b'from pkg.api import other\nother()\n'})
        result = self.scoped(before=sources, after=sources)
        self.assertEqual(result.modules, ('test_direct', 'test_indirect'))
        self.assertFalse(result.fallback)

    def test_algorithm_identity_change_rejects_old_selection(self):
        selected = self.choose()
        with self.assertRaisesRegex(ValueError, 'algorithm'):
            impact.selected_modules(replace(selected, algorithm=f'python-ast-impact-v3/index-v{code_index.VERSION}'), self.modules)

    def test_builtin_namespaces_and_reexported_loader_aliases_remain_uncertain(self):
        for consumer in (b'__builtins__["__import__"](name)\n',
                         b'import builtins\nbuiltins.__dict__["__import__"](name)\n',
                         b'from pkg import value\ngetattr(value, "__dict__")[key](name)\n'):
            with self.subTest(consumer=consumer):
                self.assertTrue(impact.source_facts('pkg/hidden.py', consumer)['impact']['uncertainties'])
        for consumer in (b'from pkg.loader import load as invoke\ninvoke(name)\n',
                         b'from pkg import loader\nloader.load(name)\n',
                         b'from pkg import loader\ngetattr(loader, "load")(name)\n'):
            with self.subTest(consumer=consumer):
                sources = dict(self.sources, **{'pkg/loader.py': b'from importlib import import_module as load\n',
                    'tests/test_other0.py': consumer})
                selected = self.scoped(before=sources, after=sources)
                self.assertFalse(selected.fallback)
                self.assertTrue(any('escaped_project_dynamic_binding' in reason for reasons in selected.analysis['remaining_uncertainties'].values() for reason in reasons))

    def test_half_manifest_and_large_module_do_not_force_fallback(self):
        self.assertFalse(self.choose(modules=self.modules[:4]).fallback)
        big = [replace(self.modules[0], ids=tuple(str(i) for i in range(100))), *self.modules[1:]]
        self.assertFalse(self.choose(modules=big).fallback)
        self.assertEqual(len(self.choose(modules=big).ids), 101)

    def test_broad_selection_is_bounded_and_changed_tests_remain_required(self):
        sources = dict(self.sources)
        modules = [Module(f'test_wide{i:02}', (f'test_wide{i:02}.Case.test_value',)) for i in range(50)]
        sources.update({'tests/' + module.name + '.py': b'from pkg import value\n' for module in modules})
        selected = self.choose(before=sources, after=sources, modules=modules)
        self.assertFalse(selected.fallback)
        self.assertEqual(len(selected.modules), impact.MAX_MODULES)
        self.assertEqual(len(selected.analysis['not_selected_related_modules']), 26)
        self.assertEqual(selected.analysis['coverage'], 'selected_modules_only')
        changed = [('M', 'pkg/value.py', 'pkg/value.py')]
        changed.extend(('M', 'tests/' + m.name + '.py', 'tests/' + m.name + '.py') for m in modules[-25:])
        mandatory = self.choose(changed, before=sources, after=sources, modules=modules)
        self.assertEqual(mandatory.modules, tuple(m.name for m in modules[-25:]))
        self.assertFalse(mandatory.fallback)
        changed.extend(('M', 'tests/' + m.name + '.py', 'tests/' + m.name + '.py') for m in modules[:25])
        self.assertIn('entire_manifest_required', self.choose(changed,
            before=sources, after=sources, modules=modules).fallback)

    def test_direct_and_actual_facade_users_precede_broad_indirect_consumers(self):
        sources = dict(self.sources, **{'pkg/entry.py': b'from pkg.value import _value as value\ninitialize()\n'})
        modules = [Module(f'test_broad{i:02}', (f'test_broad{i:02}.Case.test_value',)) for i in range(30)]
        sources.update({'tests/' + module.name + '.py': b'import pkg.entry\n' for module in modules})
        modules += [Module('test_z_actual', ('test_z_actual.Case.test_value',))]
        sources['tests/test_z_actual.py'] = b'import pkg.entry as entry\nentry.value()\n'
        result = self.choose(before=sources, after=sources, modules=modules)
        self.assertFalse(result.fallback)
        self.assertIn('test_z_actual', result.modules)
        self.assertEqual(len(result.modules), impact.MAX_MODULES)
        self.assertTrue(result.analysis['not_selected_related_modules'])

    def test_common_checks_enable_documents_no_relationships_and_risky_entrypoints(self):
        sources = dict(self.sources, **{'pkg/unrelated.py': b'__import__(name)\n',
            'loop_manager.py': b'import sqlite3\n'})
        modules = self.modules + [Module(name, (name + '.Case.test_value',)) for name in impact._SMOKE]
        sources.update({'tests/' + m.name + '.py': b'import unittest\n' for m in modules[-3:]})
        for path in ('docs/PRODUCT.md', 'README.md', 'tests/fixtures/data.json',
                     'pkg/unrelated.py', 'loop_manager.py'):
            with self.subTest(path=path):
                result = self.choose([('M', path, path)], before=sources, after=sources, modules=modules)
                self.assertFalse(result.fallback)
                self.assertEqual(set(result.modules), set(impact._SMOKE))
                self.assertTrue(result.analysis['remaining_uncertainties'])
                self.assertTrue(all('common_regression_precheck' in result.reasons[name] for name in result.modules))

    def test_document_consumers_are_selected_without_claiming_exclusion(self):
        path = 'tests/fixtures/input.md'
        sources = dict(self.sources, **{'pkg/reader.py': b'from pathlib import Path\nPath("tests/fixtures/input.md").read_text()\n',
            'tests/test_other0.py': b'from pkg import reader\n'})
        result = self.scoped(before=sources, after=sources, changed=[('M', path, path)])
        self.assertFalse(result.fallback)
        self.assertEqual(result.modules, ('test_other0',))
        self.assertIn('non_python_input:' + path, result.analysis['scope_limits'])

    def test_verification_contract_changes_fall_back_even_with_common_checks(self):
        for path in ('scripts/verify.json', 'scripts/verify.sh',
                     'loop_manager_internal/verification/runner.py', 'config/config.ini.example',
                     'loop_manager_internal/execution/verify_process.py',
                     'loop_manager_internal/execution/windows_job.py'):
            with self.subTest(path=path):
                result = self.choose([('M', 'pkg/value.py', 'pkg/value.py'), ('M', path, path)])
                self.assertIn('verification_contract_changed:' + path, result.fallback)

    def test_process_contract_deletion_and_rename_are_verification_changes(self):
        for old, new in (('loop_manager_internal/execution/verify_process.py', None),
                         ('loop_manager_internal/execution/verify_process.py',
                          'loop_manager_internal/execution/process_runner.py'),
                         ('loop_manager_internal/execution/windows_job.py', None),
                         ('loop_manager_internal/execution/windows_job.py',
                          'loop_manager_internal/execution/job_scope.py')):
            with self.subTest(old=old, new=new):
                status = 'D' if new is None else 'R100'
                result = self.choose([('M', 'pkg/value.py', 'pkg/value.py'), (status, old, new)])
                self.assertIn('verification_contract_changed:' + old, result.fallback)

    def test_manifest_identity_and_selection_are_exact(self):
        selection = self.choose()
        self.assertEqual(impact.selected_modules(selection, self.modules), self.modules[:2])
        for altered in (list(reversed(self.modules)),
                        [replace(self.modules[0], ids=('other',)), *self.modules[1:]]):
            with self.assertRaises(ValueError):
                impact.selected_modules(selection, altered)
        with self.assertRaises(ValueError):
            impact.selected_modules(replace(selection, modules=('missing',)), self.modules)

    def test_fresh_import_inventory_and_shard_changes_keep_opaque_consumer_fallback(self):
        # Child-code imports stay uncertain in diagnostics. Inventory or
        # collection-input changes require the full collection boundary.
        root = Path(__file__).resolve().parents[1]
        helper = 'tests/support/fresh_imports.py'
        shard = 'tests/test_direct.py'
        sources = dict(self.sources)
        sources[helper] = (root / helper).read_bytes()
        sources[shard] = (root / 'tests/test_internal_imports_0.py').read_bytes()
        for change in (('A', None, 'pkg/new.py'), ('D', 'pkg/value.py', None),
                       ('R100', 'pkg/value.py', 'pkg/moved.py'), ('M', shard, shard),
                       ('M', helper, helper)):
            with self.subTest(change=change):
                after = dict(sources)
                status, old, new = change
                if status == 'A':
                    after[new] = b'def _value():\n    return 1\n'
                elif status == 'D':
                    del after[old]
                elif status.startswith('R'):
                    after[new] = after.pop(old)
                else:
                    after[new] += b'\n# consumer changed\n'
                selection = impact.select_records(self.graph(sources), self.graph(after), [change],
                    self.modules, head='head', base='base', configuration={'version': 1})
                for label in ('base', 'head'):
                    self.assertTrue(selection.analysis['remaining_uncertainties'][label + ':' + helper])
                expected = ('custom_collection:' + shard,) if old == shard else (
                    ('no_tests_selected',) if status == 'A' else ())
                expected = tuple(sorted((*expected, 'uncertain_collection_input:' + helper)))
                self.assertEqual(selection.fallback, expected)
                with self.assertRaises(ValueError):
                    impact.selected_modules(selection, self.modules)

        # Ordinary implementation edits and unrelated opaque readers do not
        # change the collection inventory or its consumer/helper contract.
        selection = impact.select_records(self.graph(sources), self.graph(sources),
            [('M', 'pkg/value.py', 'pkg/value.py')], self.modules,
            head='head', base='base', configuration={'version': 1})
        self.assertFalse(selection.fallback)
        unrelated = dict(sources, **{shard: self.sources[shard]})
        selection = impact.select_records(self.graph(unrelated), self.graph(unrelated),
            [('A', None, 'pkg/new.py')], self.modules,
            head='head', base='base', configuration={'version': 1})
        self.assertFalse(any(reason.startswith('uncertain_collection_input:')
                             for reason in selection.fallback))

    def test_opaque_inventory_reaches_collection_through_shared_helpers(self):
        sources = dict(self.sources, **{
            'tests/support/inventory.py': b'def inventory(root):\n    return tuple(root.rglob("*.py"))\n',
            'tests/support/forward.py': b'from tests.support import inventory\n',
            'tests/test_direct.py': b'from tests.support import forward\ndef load_tests(loader, tests, pattern):\n    return tests\n',
        })
        after = dict(sources, **{'pkg/new.py': b'pass\n'})
        selection = self.choose([('A', None, 'pkg/new.py')], before=sources, after=after)
        self.assertIn('uncertain_collection_input:tests/support/inventory.py', selection.fallback)
        with self.assertRaises(ValueError):
            impact.selected_modules(selection, self.modules)

    def test_new_fresh_import_ids_cannot_reuse_an_old_selection(self):
        selection = self.choose()
        from tests.support import fresh_imports
        target = fresh_imports.inventory()[0]
        current = replace(self.modules[0], ids=(
            'test_direct.FreshImportTests.' + target.method_name,))
        with self.assertRaisesRegex(ValueError, 'manifest identity'):
            impact.selected_modules(selection, [current, *self.modules[1:]])

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
