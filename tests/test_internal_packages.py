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

import loop_manager


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "loop_manager_internal"


class InternalPackageMigrationTests(unittest.TestCase):
    def test_each_module_imports_first_without_loading_the_entry_point(self):
        # Fresh interpreters expose import-order cycles hidden by the manager's imports.
        for path in sorted(PACKAGE.rglob("*.py")):
            if path.name == "__init__.py":
                continue
            module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c",
                     "import importlib, sys; sys.path.insert(0, sys.argv[1]); "
                     "importlib.import_module(sys.argv[2]); "
                     "assert 'loop_manager' not in sys.modules",
                     str(ROOT), module],
                    capture_output=True, text=True, check=False, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_package_initializers_do_not_initialize_services(self):
        for path in PACKAGE.rglob("__init__.py"):
            module = ".".join(path.parent.relative_to(ROOT).parts)
            with self.subTest(package=path.parent.name):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c",
                     "import importlib, sys; sys.path.insert(0, sys.argv[1]); "
                     "importlib.import_module(sys.argv[2]); "
                     "assert all(getattr(m, '__file__', '').endswith('__init__.py') "
                     "for name, m in sys.modules.items() if name.startswith('loop_manager_internal')); "
                     "assert 'loop_manager' not in sys.modules",
                     str(ROOT), module],
                    capture_output=True, text=True, check=False, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

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
                shutil.copy(ROOT / name, target)
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

    def test_both_verify_scripts_compile_every_relocated_module(self):
        required = {"loop_manager.py", *(p.relative_to(ROOT).as_posix()
                                         for p in PACKAGE.rglob("*.py"))}
        for name in ("verify.sh", "verify.ps1"):
            with self.subTest(script=name):
                lines = (ROOT / "scripts" / name).read_text(encoding="utf-8").splitlines()
                commands = [line for line in lines if "-m py_compile " in line]
                self.assertEqual(len(commands), 1)
                self.assertEqual(set(commands[0].split("-m py_compile ", 1)[1].split()), required)
                self.assertTrue(any("-m unittest discover -s tests" in line for line in lines))
