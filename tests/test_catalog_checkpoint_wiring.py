"""Regression coverage for catalog/checkpoint wiring after rebasing."""

from pathlib import Path
import unittest

import blocked_checkpoint
import blocked_checkpoint_state
import execplan_catalog
import execplan_catalog_state
import loop_manager


class CatalogCheckpointWiringTest(unittest.TestCase):
    def test_manager_retains_both_feature_module_bindings(self):
        for module in (
            blocked_checkpoint, blocked_checkpoint_state,
            execplan_catalog, execplan_catalog_state,
        ):
            with self.subTest(module=module.__name__):
                self.assertIs(getattr(loop_manager, module.__name__), module)

    def test_verify_scripts_keep_both_features_and_runtime_modules(self):
        root = Path(__file__).resolve().parents[1]
        required = {
            "blocked_checkpoint.py", "blocked_checkpoint_state.py",
            "execplan_catalog.py", "execplan_catalog_state.py",
            "planning_runtime.py", "runtime_state.py",
            "recovery_dispatcher.py", "recovery_runtime.py",
            "recovery_state.py",
        }
        for name in ("verify.sh", "verify.ps1"):
            with self.subTest(script=name):
                lines = (root / "scripts" / name).read_text().splitlines()
                commands = [line for line in lines if "-m py_compile " in line]
                self.assertEqual(len(commands), 1)
                self.assertTrue(required.issubset(set(commands[0].split())))
