"""초기 Git fixture 재사용과 파일 격리의 최소 unit 회귀."""
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import subprocess
import sys
from unittest import mock

from tests.support import git as fixture_git
import loop_manager


class GitFixtureSeedTests(unittest.TestCase):
    def setUp(self):
        fixture_git._remote_repository_seed.cache_clear()
        self.addCleanup(fixture_git._remote_repository_seed.cache_clear)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.seed = SimpleNamespace(name=str(self.root / "seed"), cleanup=mock.Mock())
        Path(self.seed.name).mkdir()
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.initialize = patches.enter_context(mock.patch.object(
            fixture_git, "_initialize_remote_repository", side_effect=self.prepare_seed,
        ))
        self.temporary = patches.enter_context(mock.patch.object(
            fixture_git.tempfile, "TemporaryDirectory", return_value=self.seed,
        ))
        self.on_exit = patches.enter_context(mock.patch.object(fixture_git.atexit, "register"))
        self.git = patches.enter_context(mock.patch.object(fixture_git, "git_command", return_value=""))

    def prepare_seed(self, directory):
        root = Path(directory)
        self.files = {
            "remote.git/refs/heads/main": b"initial-ref\n",
            "remote.git/objects/seed": b"initial-object",
            "repo/.git/refs/heads/main": b"initial-ref\n",
            "repo/.git/index": b"initial-index",
            "repo/.git/config": b"initial-config",
            "repo/.git/objects/seed": b"initial-object",
            "repo/src/app.py": b"VALUE = 1\n",
            "repo/docs/requirements/Active.md": b"# Active\n",
        }
        for relative, contents in self.files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        return root / "repo"

    def make_repository(self, name):
        parent = self.root / name
        parent.mkdir()
        return fixture_git.make_remote_repository(str(parent))

    def test_repeated_calls_initialize_once_and_rebind_each_destination(self):
        first = self.make_repository("first")
        second = self.make_repository("second")
        self.initialize.assert_called_once_with(self.seed.name)
        self.temporary.assert_called_once_with(prefix="loop-manager-test-seed-")
        self.on_exit.assert_called_once_with(self.seed.cleanup)
        self.git.assert_has_calls([
            mock.call(first, "init", "--initial-branch=main"),
            mock.call(first, "remote", "set-url", "origin", str(first.parent / "remote.git")),
            mock.call(second, "init", "--initial-branch=main"),
            mock.call(second, "remote", "set-url", "origin", str(second.parent / "remote.git")),
        ])
        self.assertEqual(self.git.call_count, 4)
        for relative, contents in self.files.items():
            self.assertEqual((Path(self.seed.name) / relative).read_bytes(), contents)

    def test_files_refs_index_config_and_objects_are_independent(self):
        first = self.make_repository("first")
        second = self.make_repository("second")
        for relative, contents in self.files.items():
            with self.subTest(path=relative):
                (first.parent / relative).write_bytes(b"changed")
                self.assertEqual((second.parent / relative).read_bytes(), contents)
                self.assertEqual((Path(self.seed.name) / relative).read_bytes(), contents)

    def test_initialization_failure_is_cleaned_and_not_cached(self):
        self.initialize.side_effect = RuntimeError("initialization failed")
        with self.assertRaisesRegex(RuntimeError, "initialization failed"):
            fixture_git._remote_repository_seed()
        self.seed.cleanup.assert_called_once_with()
        self.on_exit.assert_not_called()
        self.assertEqual(fixture_git._remote_repository_seed.cache_info().currsize, 0)

        self.initialize.side_effect = self.prepare_seed
        self.assertIs(fixture_git._remote_repository_seed(), self.seed)
        self.assertEqual(self.initialize.call_count, 2)
        self.on_exit.assert_called_once_with(self.seed.cleanup)

    def test_copy_failure_preserves_seed_and_can_retry_elsewhere(self):
        self.make_repository("first")
        occupied = self.root / "occupied"
        (occupied / "repo").mkdir(parents=True)
        user_file = occupied / "repo" / "user.txt"
        user_file.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            fixture_git.make_remote_repository(str(occupied))
        self.assertEqual(user_file.read_bytes(), b"keep")
        for relative, contents in self.files.items():
            self.assertEqual((Path(self.seed.name) / relative).read_bytes(), contents)
        self.make_repository("retry")
        self.initialize.assert_called_once_with(self.seed.name)


class GitEnvironmentTests(unittest.TestCase):
    def test_direct_init_and_followup_receive_isolation_without_changing_caller(self):
        hostile = {'GIT_CONFIG_GLOBAL': 'user-config', 'GIT_CONFIG_SYSTEM': 'system-config',
                   'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
                   'GIT_CONFIG_VALUE_0': 'user-hooks', 'GIT_CONFIG_PARAMETERS': 'user-parameters',
                   'GIT_TEMPLATE_DIR': 'user-template', 'XDG_CONFIG_HOME': 'user-xdg',
                   'GIT_ATTR_NOSYSTEM': '0'}
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, hostile), \
             mock.patch.object(fixture_git.subprocess, 'run', return_value=
                               subprocess.CompletedProcess([], 0, '')) as process:
            caller = dict(os.environ)
            fixture_git._initialize_remote_repository(directory)
            self.assertEqual(dict(os.environ), caller)
            self.assertEqual(process.call_args_list[0].args[0][1:3], ['init', '--bare'])
            for call in process.call_args_list:
                environment = call.kwargs['env']
                self.assertEqual(environment, fixture_git.git_test_environment())
                self.assertEqual(environment['GIT_CONFIG_GLOBAL'], os.devnull)
                self.assertEqual(environment['GIT_CONFIG_SYSTEM'], os.devnull)
                self.assertEqual(environment['GIT_ATTR_NOSYSTEM'], '1')
                self.assertEqual(environment['XDG_CONFIG_HOME'], environment['GIT_TEMPLATE_DIR'])
                self.assertNotIn('GIT_CONFIG_COUNT', environment)
                self.assertNotIn('GIT_CONFIG_PARAMETERS', environment)
                self.assertTrue(Path(environment['GIT_TEMPLATE_DIR'], 'info/exclude').is_file())

    def test_environment_restores_after_setup_failure_and_next_test(self):
        @fixture_git.isolated_git_test
        class Probe(unittest.TestCase):
            def setUp(self):
                self.assertEqual(os.environ['GIT_CONFIG_GLOBAL'], os.devnull)
                os.environ['G224_TEMP_INPUT'] = 'changed'
                if self._testMethodName == 'test_failure':
                    raise RuntimeError('setup interrupted')

            def test_failure(self):
                self.fail('unreachable')

            def test_next(self):
                self.assertEqual(os.environ['GIT_CONFIG_SYSTEM'], os.devnull)

        caller = dict(os.environ)
        failed = unittest.TestResult()
        Probe('test_failure').run(failed)
        self.assertEqual(len(failed.errors), 1)
        self.assertIn('setup interrupted', failed.errors[0][1])
        self.assertEqual(dict(os.environ), caller)
        success = unittest.TestResult()
        Probe('test_next').run(success)
        self.assertTrue(success.wasSuccessful(), success.errors)
        self.assertEqual(dict(os.environ), caller)

    def test_real_seed_adapter_and_child_ignore_hostile_settings_but_explicit_inputs_apply(self):
        # 최초 init/template·hook 실행·설정 상속은 실제 Git과 새 interpreter로 확인한다.
        fixture_git._remote_repository_seed.cache_clear()
        self.addCleanup(fixture_git._remote_repository_seed.cache_clear)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks = root / 'hooks'
            hooks.mkdir()
            marker = root / 'hook-ran'
            hook = hooks / 'pre-commit'
            hook.write_text('#!/bin/sh\nprintf ran > "' + marker.as_posix() + '"\nexit 1\n', encoding='utf-8')
            hook.chmod(0o755)
            template = root / 'template'
            template.mkdir()
            (template / 'ambient-file').write_bytes(b'user template')
            xdg = root / 'xdg'
            (xdg / 'git').mkdir(parents=True)
            global_ignore = xdg / 'git/ignore'
            global_attributes = xdg / 'git/attributes'
            global_ignore.write_text('src/app.py\nreview.txt\n', encoding='utf-8')
            global_attributes.write_text('*.py text eol=crlf\n', encoding='utf-8')
            global_config = root / 'global'
            system_config = root / 'system'
            global_config.write_text(
                '[core]\n hooksPath = "' + hooks.as_posix() + '"\n fsmonitor = missing-fsmonitor\n'
                '[commit]\n gpgSign = true\n[gpg]\n program = missing-signing-program\n'
                '[filter "hostile"]\n clean = missing-filter\n required = true\n'
                '[test]\n globalMarker = explicit\n', encoding='utf-8')
            system_config.write_text('[test]\n systemMarker = explicit\n', encoding='utf-8')
            hostile = {'GIT_CONFIG_GLOBAL': str(global_config), 'GIT_CONFIG_SYSTEM': str(system_config),
                       'GIT_CONFIG_NOSYSTEM': '0', 'GIT_TEMPLATE_DIR': str(template),
                       'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath',
                       'GIT_CONFIG_VALUE_0': str(hooks), 'XDG_CONFIG_HOME': str(xdg),
                       'GIT_ATTR_NOSYSTEM': '0'}
            inputs = (global_config, system_config, global_ignore, global_attributes)
            preserved = tuple(path.read_bytes() for path in inputs)

            @fixture_git.isolated_git_test
            class Probe(unittest.TestCase):
                def runTest(inner):
                    repo = fixture_git.make_remote_repository(str(root / 'first'))
                    second = fixture_git.make_remote_repository(str(root / 'second'))
                    inner.assertFalse((repo / '.git/ambient-file').exists())
                    inner.assertFalse((repo.parent / 'remote.git/ambient-file').exists())
                    inner.assertIn('src/app.py', fixture_git.git_command(repo, 'ls-files').splitlines())
                    inner.assertEqual(fixture_git.git_command(repo, 'check-attr', 'eol', '--', 'src/app.py'),
                                      'src/app.py: eol: unspecified')
                    (repo / 'review.txt').write_text('visible untracked input\n', encoding='utf-8')
                    inner.assertEqual(loop_manager.git(repo, 'status', '--porcelain').stdout,
                                      '?? review.txt\n')
                    child = subprocess.run([sys.executable, '-c',
                        'import subprocess; '
                        'assert subprocess.check_output(["git","config","--global","--list"]) == b""; '
                        'assert subprocess.check_output(["git","status","--porcelain"]) == b"?? review.txt\\n"; '
                        'assert subprocess.check_output(["git","check-attr","eol","--","src/app.py"]) '
                        '== b"src/app.py: eol: unspecified\\n"'],
                        cwd=repo, env=os.environ.copy(), check=True)
                    inner.assertEqual(child.returncode, 0)
                    explicit_xdg = {**fixture_git.git_test_environment(), 'XDG_CONFIG_HOME': str(xdg)}
                    inner.assertIn('git/ignore', fixture_git.git_command(
                        repo, 'check-ignore', '-v', 'review.txt', env=explicit_xdg))
                    inner.assertEqual(fixture_git.git_command(repo, 'check-attr', 'eol', '--', 'src/app.py',
                                                             env=explicit_xdg), 'src/app.py: eol: crlf')
                    (repo / 'review.txt').unlink()
                    (repo / '.gitattributes').write_text('*.py filter=hostile\n', encoding='utf-8')
                    loop_manager.git(repo, 'add', '-A')
                    loop_manager.git(repo, 'commit', '-m', 'Adapter with isolated settings')
                    inner.assertEqual(loop_manager.git(repo, 'config', '--global', '--list').stdout, '')
                    explicit = fixture_git.git_test_environment()
                    explicit.update(GIT_CONFIG_GLOBAL=str(global_config),
                                    GIT_CONFIG_SYSTEM=str(system_config), GIT_CONFIG_NOSYSTEM='0')
                    explicit_template = {**explicit, 'GIT_TEMPLATE_DIR': str(template)}
                    fixture_git.git_command(root, 'init', str(root / 'explicit'), env=explicit_template)
                    inner.assertEqual((root / 'explicit/.git/ambient-file').read_bytes(), b'user template')
                    for key in ('globalMarker', 'systemMarker'):
                        inner.assertEqual(fixture_git.git_command(repo, 'config', 'test.' + key, env=explicit), 'explicit')
                    (repo / 'src/app.py').write_text('VALUE = 2\n', encoding='utf-8')
                    with inner.assertRaises(subprocess.CalledProcessError):
                        fixture_git.git_command(repo, 'add', 'src/app.py', env=explicit)
                    with inner.assertRaises(subprocess.CalledProcessError) as signing:
                        fixture_git.git_command(second, '-c', 'core.hooksPath=' + os.devnull,
                            'commit', '--allow-empty', '-m', 'Explicit signing fails', env=explicit)
                    inner.assertIn('missing-signing-program', signing.exception.output)
                    fixture_git.git_command(repo, 'config', 'core.hooksPath', str(hooks))
                    (repo / 'README.md').write_text('explicit hook\n', encoding='utf-8')
                    fixture_git.git_command(repo, 'add', 'README.md')
                    with inner.assertRaises(subprocess.CalledProcessError):
                        fixture_git.git_command(repo, 'commit', '-m', 'Explicit hook rejects')
                    inner.assertTrue(marker.is_file())
                    inner.assertEqual(fixture_git.git_command(second, 'status', '--porcelain'), '')
                    inner.assertEqual(fixture_git.git_command(second, 'config', '--local', '--get', 'user.name'),
                                      'Loop Manager Test')

            with mock.patch.dict(os.environ, hostile):
                caller = dict(os.environ)
                result = unittest.TestResult()
                Probe().run(result)
                self.assertTrue(result.wasSuccessful(), result.errors or result.failures)
                self.assertEqual(dict(os.environ), caller)
            self.assertEqual(tuple(path.read_bytes() for path in inputs), preserved)
