"""Regression coverage for catalog/checkpoint wiring after rebasing."""

from pathlib import Path
import unittest

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state
import loop_manager


class CatalogCheckpointWiringTest(unittest.TestCase):
    def test_manager_retains_both_feature_module_bindings(self):
        for module in (
            blocked_checkpoint, blocked_checkpoint_state, blocked_plan_reconciliation,
            execplan_catalog, execplan_catalog_state,
        ):
            with self.subTest(module=module.__name__):
                self.assertIs(getattr(loop_manager, module.__name__.rsplit(".", 1)[-1]), module)

    def test_verify_scripts_keep_both_features_and_runtime_modules(self):
        root = Path(__file__).resolve().parents[1]
        required = {
            "loop_manager_internal/recovery/blocked_checkpoint.py", "loop_manager_internal/storage/blocked_checkpoint_state.py",
            "loop_manager_internal/catalog/execplan_catalog.py", "loop_manager_internal/storage/execplan_catalog_state.py",
            "loop_manager_internal/planning/planning_runtime.py", "loop_manager_internal/storage/runtime_state.py",
            "loop_manager_internal/recovery/recovery_dispatcher.py", "loop_manager_internal/recovery/recovery_runtime.py",
            "loop_manager_internal/storage/recovery_state.py",
        }
        for name in ("verify.sh", "verify.ps1"):
            with self.subTest(script=name):
                lines = (root / "scripts" / name).read_text().splitlines()
                commands = [line for line in lines if "-m py_compile " in line]
                self.assertEqual(len(commands), 1)
                self.assertTrue(required.issubset(set(commands[0].split())))
                if name == 'verify.sh':
                    self.assertIn('loop_manager_internal/recovery/blocked_plan_reconciliation.py', commands[0].split())
