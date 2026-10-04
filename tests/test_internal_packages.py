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


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "loop_manager_internal"


class InternalPackageMigrationTests(unittest.TestCase):
    def test_each_module_imports_first_without_loading_the_entry_point(self):
        # 모듈마다 새 interpreter에서 순환 import와 resource effect를 함께 검사한다.
        code = """
import importlib, sqlite3, subprocess, sys
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError('resource effect during import')
sqlite3.connect = forbidden
subprocess.run = forbidden
# Windows asyncio는 import 시 Popen을 상속하므로 생성만 금지한다.
class ForbiddenPopen(subprocess.Popen):
    __init__ = forbidden
subprocess.Popen = ForbiddenPopen
importlib.import_module(sys.argv[2])
if sys.argv[2] != 'loop_manager':
    assert 'loop_manager' not in sys.modules
"""
        modules = ['loop_manager', *(".".join(path.relative_to(ROOT).with_suffix("").parts)
                   for path in sorted(PACKAGE.rglob("*.py")) if path.name != "__init__.py")]
        for module in modules:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", code, str(ROOT), module],
                    capture_output=True, text=True, check=False, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_fresh_import_guard_rejects_sqlite_and_process_creation(self):
        real_run = subprocess.run
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.test_each_module_imports_first_without_loading_the_entry_point()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len({command[-1] for command in commands}), len(commands))
        self.assertTrue(all(command[1:3] == ['-I', '-B'] for command in commands))
        code = commands[0][4]
        for effect in ("sqlite3.connect(':memory:')", "subprocess.run(['git', '--version'])",
                       "subprocess.Popen(['git', '--version'])"):
            with self.subTest(effect=effect):
                result = real_run([sys.executable, '-I', '-B', '-c', code + '\n' + effect,
                                   str(ROOT), 'sqlite3'], capture_output=True, text=True, timeout=30)
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
        from loop_manager_internal.verification import runner
        required = {"loop_manager.py", *(p.relative_to(ROOT).as_posix()
                                         for p in PACKAGE.rglob("*.py"))}
        with mock.patch.object(runner.py_compile, "compile") as compile_source:
            runner.compile_project(ROOT)
        self.assertEqual({Path(call.args[0]).relative_to(ROOT).as_posix() for call in compile_source.call_args_list}, required)
        self.assertTrue(all(call.kwargs['doraise'] for call in compile_source.call_args_list))
        for name in ("verify.sh", "verify.ps1"):
            with self.subTest(script=name):
                lines = (ROOT / "scripts" / name).read_text(encoding="utf-8").splitlines()
                commands = [line for line in lines if "-m loop_manager_internal.verification.runner" in line]
                self.assertEqual(len(commands), 1)
