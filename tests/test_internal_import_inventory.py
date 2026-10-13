"""Inventory, partition and process decisions use mocked process observations."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import fresh_imports
from loop_manager_internal.verification.manifest import test_cases as cases


class FreshImportInventoryTests(unittest.TestCase):
    def test_inventory_matches_both_original_target_sets(self):
        root = fresh_imports.ROOT
        package = root / 'loop_manager_internal'
        modules = {'loop_manager', *('.'.join(path.relative_to(root).with_suffix('').parts)
                    for path in package.rglob('*.py') if path.name != '__init__.py')}
        packages = {'.'.join(path.parent.relative_to(root).parts)
                    for path in package.rglob('__init__.py')}
        targets = fresh_imports.inventory()
        self.assertEqual({target.module for target in targets if target.kind == 'module'}, modules)
        self.assertEqual({target.module for target in targets if target.kind == 'package'}, packages)
        self.assertEqual(len(targets), len(modules) + len(packages))
        self.assertEqual(len({target.legacy_id for target in targets}), 2)

    def test_inventory_includes_new_implementation_and_package_without_fixed_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'loop_manager_internal/new_package'
            package.mkdir(parents=True)
            (package.parent / '__init__.py').touch()
            (package / '__init__.py').touch()
            before = fresh_imports.inventory(root)
            (package / 'new_module.py').touch()
            added = fresh_imports.ImportTarget('module', 'loop_manager_internal.new_package.new_module')
            self.assertEqual(set(fresh_imports.inventory(root)) - set(before), {added})
            self.assertIn(fresh_imports.ImportTarget('package', 'loop_manager_internal.new_package'), before)

    def test_stable_partition_and_identity_with_order_and_inventory_changes(self):
        targets = fresh_imports.inventory()
        shards = fresh_imports.partition(targets)
        self.assertEqual(shards, fresh_imports.partition(tuple(reversed(targets))))
        new = fresh_imports.ImportTarget('module', 'loop_manager_internal.new_module')
        extended = fresh_imports.partition((*targets, new))
        for old, current in zip(shards, extended):
            self.assertEqual(old, tuple(target for target in current if target != new))
        names = {target.method_name for target in targets}
        self.assertEqual(len(names), len(targets))
        self.assertNotEqual(fresh_imports.ImportTarget('module', 'a.b').method_name,
                            fresh_imports.ImportTarget('module', 'a__b').method_name)

    def test_partition_rejects_missing_duplicate_and_unknown_targets(self):
        a, b = (fresh_imports.ImportTarget('module', name) for name in ('a', 'b'))
        for targets, shards in (((a, b), ((a,),)), ((a, b), ((a, b), (a,))),
                                ((a,), ((a, b),)), ((a, a), ((a, a),))):
            with self.subTest(targets=targets, shards=shards), self.assertRaises(ValueError):
                fresh_imports.validate_partition(targets, shards)

    def test_every_target_has_one_sequential_fresh_command_and_owned_identity(self):
        targets = fresh_imports.inventory()
        collected = []
        with mock.patch.object(fresh_imports.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            for shard in range(fresh_imports.SHARD_COUNT):
                owner = f'test_internal_imports_{shard}'
                suite = fresh_imports.suite(unittest.TestLoader(), owner, shard)
                tests = list(cases(suite))
                self.assertTrue(tests)
                collected.extend(test.id() for test in tests)
                self.assertTrue(all(test.__class__.__module__ == owner for test in tests))
                result = unittest.TestResult()
                suite.run(result)
                self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(collected), len(set(collected)))
        self.assertEqual(len(commands), len(targets))
        self.assertEqual({(command[-1], command[-2]) for command in commands},
                         {(target.kind, target.module) for target in targets})
        self.assertTrue(all(command[:4] == [sys.executable, '-I', '-B', '-c'] for command in commands))
        self.assertTrue(all(call.kwargs['timeout'] == 30 for call in run.call_args_list))

    def test_failure_identifies_target_interpreter_and_error(self):
        target = fresh_imports.ImportTarget('module', 'broken.module')
        for observation in (subprocess.CompletedProcess([], 1, '', 'ImportError: cycle'),
                            subprocess.TimeoutExpired(['python'], 30), OSError('interpreter unavailable')):
            with self.subTest(observation=observation):
                kwargs = {'side_effect': observation} if isinstance(observation, Exception) else {'return_value': observation}
                with mock.patch.object(fresh_imports.subprocess, 'run', **kwargs), self.assertRaises(AssertionError) as failed:
                    fresh_imports.check_import(self, target)
                self.assertIn(target.module, str(failed.exception))
                self.assertIn(sys.executable, str(failed.exception))
                self.assertIn('cycle' if not isinstance(observation, Exception) else str(observation), str(failed.exception))
