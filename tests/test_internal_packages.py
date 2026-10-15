"""Unit regressions for implementation relocation and the root entry point."""

import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import FunctionType, ModuleType
import unittest
from unittest import mock

import loop_manager
from tests.support.fixed_samples import PROJECT_FILES
from tests.support import fresh_imports


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "loop_manager_internal"


class InternalPackageMigrationTests(unittest.TestCase):
    def test_fresh_import_guard_rejects_sqlite_and_process_creation(self):
        # Real children prove the same guard used by every distributed target.
        for effect in ("sqlite3.connect(':memory:')", "subprocess.run(['git', '--version'])",
                       "subprocess.Popen(['git', '--version'])"):
            with self.subTest(effect=effect):
                result = subprocess.run(
                    [sys.executable, '-I', '-B', '-c', fresh_imports.IMPORT_CODE + '\n' + effect,
                     str(ROOT), 'loop_manager', 'module'], capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('resource effect during import', result.stderr)

    def test_public_module_bindings_and_helper_identity_are_preserved(self):
        for name, value in vars(loop_manager).items():
            module = getattr(value, "__module__", "")
            if isinstance(value, (type, FunctionType)) and module.startswith("loop_manager_internal."):
                with self.subTest(helper=name):
                    self.assertIs(getattr(importlib.import_module(module), value.__name__), value)
            if isinstance(value, ModuleType) and value.__name__.startswith("loop_manager_internal."):
                with self.subTest(binding=name):
                    self.assertEqual(value.__name__.rsplit(".", 1)[-1], name)
                    self.assertIs(importlib.import_module(value.__name__), value)

    def test_copied_runtime_uses_its_root_resources_and_external_config(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir()
            shutil.copy(ROOT / "loop_manager.py", runtime / "loop_manager.py")
            shutil.copytree(PACKAGE, runtime / PACKAGE.name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in ("AGENTS.md", "docs/PLANS.md", "docs/PRODUCT.template.md",
                         "docs/ARCHITECTURE.template.md"):
                target = runtime / name
                target.parent.mkdir(exist_ok=True)
                shutil.copy(PROJECT_FILES / name, target)
            config = Path(directory) / "source/config/config.ini"
            config.parent.mkdir(parents=True)
            config.write_text("[telegram]\ntoken =\nchat_id =\n", encoding="utf-8")
            code = (
                "import json, pathlib, sys; "
                "sys.path.insert(0, sys.argv[1]); import loop_manager as lm; "
                "root = pathlib.Path(sys.argv[1]); config = pathlib.Path(sys.argv[2]); "
                "assert lm.ROOT == root; "
                "assert lm.build_parser().parse_args(['run']).config == str(root / 'config/config.ini'); "
                "assert lm.load_managed_config(config).max_workers == 3; "
                "assert all((lm.ROOT / p).is_file() for p in "
                "['AGENTS.md', 'docs/PLANS.md', 'docs/PRODUCT.template.md', 'docs/ARCHITECTURE.template.md']); "
                "args = lm.runtime_child_arguments(['run'], source=config.parents[2], "
                "state_dir=config.parents[2] / 'state', config_path=config, "
                "runtime=lm.RuntimeWorktree(root, 'a' * 40)); "
                "assert args[args.index('--config') + 1] == str(config); "
                "print(json.dumps(str(lm.ROOT)))"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code, str(runtime), str(config)],
                cwd=directory, capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), str(runtime))
            # argparse exits before bootstrap side effects; exercise direct-file imports.
            help_result = subprocess.run(
                [sys.executable, "-B", str(runtime / "loop_manager.py"), "--help"],
                cwd=directory, capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("--config", help_result.stdout)
            self.assertFalse(list(runtime.rglob("*.pyc")))

    def test_verify_compile_stage_covers_every_relocated_module(self):
        from loop_manager_internal.verification import contract, unittest_adapter
        connection = contract.load(ROOT, project_python=sys.executable)
        command = next(stage['argv'] for stage in connection.stages if stage['id'] == 'compile')
        self.assertEqual(command[:3], ['{python}', '{runner}', '--compile'])
        required = {"loop_manager.py", *(p.relative_to(ROOT).as_posix()
                                         for p in PACKAGE.rglob("*.py"))}
        with mock.patch.object(unittest_adapter.py_compile, "compile") as compile_source:
            unittest_adapter.compile_paths(ROOT, command[3:])
        self.assertEqual({Path(call.args[0]).relative_to(ROOT).as_posix() for call in compile_source.call_args_list}, required)
        self.assertTrue(all(call.kwargs['doraise'] for call in compile_source.call_args_list))
