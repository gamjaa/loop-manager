from contextlib import nullcontext
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.agents.agent_profiles import agent_environment
from loop_manager_internal.config import execution_path
from loop_manager_internal.config.managed_config import ManagedConfigError
from loop_manager_internal.runtime.bootstrap import BootstrapService


class ExecutionPathTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.source = Path(directory)
        self.saved = self.source / 'saved tools'
        self.current = self.source / 'current'
        self.system = self.source / 'system'
        for path in (self.saved, self.current, self.system):
            path.mkdir()
        self.cache = self.source / 'config/execution-path.txt'
        self.cache.parent.mkdir()
        self.enterContext(mock.patch.object(execution_path.sys, 'platform', 'linux'))
        self.enterContext(mock.patch.object(execution_path, 'SYSTEM_DIRECTORIES', (str(self.system),)))
        self.enterContext(mock.patch.dict(os.environ, {'PATH': str(self.current)}))

    def write_cache(self, value):
        self.cache.write_text(value, encoding='utf-8', newline='\n')

    def test_normalization_preserves_order_spaces_and_excludes_unusable_paths(self):
        value = os.pathsep.join((str(self.saved), '', '.', 'relative', str(self.saved),
                                str(self.source / 'missing'), str(self.current)))
        self.assertEqual(execution_path.normalize_path(value), os.pathsep.join((str(self.saved), str(self.current))))

    def test_setup_saves_only_path_and_refreshes_existing_cache(self):
        with mock.patch.object(execution_path.shutil, 'which', return_value='/tool') as which:
            os.environ.update(PATH=str(self.saved), PRIVATE_SETUP_SECRET='must-not-save')
            execution_path.save_execution_path(self.source)
            self.assertEqual(self.cache.read_text(), str(self.saved) + '\n')
            self.assertEqual([call.args[0] for call in which.call_args_list], ['git', 'gh', 'codex'])
            self.assertTrue(all(call.kwargs['path'] == str(self.saved) for call in which.call_args_list))
            os.environ['PATH'] = str(self.current)
            execution_path.save_execution_path(self.source)
            self.assertEqual(self.cache.read_text(), str(self.current) + '\n')
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])

    def test_setup_rejects_missing_tools_and_preserves_existing_cache(self):
        self.write_cache('old cache')
        for missing in ('git', 'gh', 'providers'):
            with self.subTest(missing=missing), mock.patch.object(execution_path.shutil, 'which',
                    side_effect=lambda name, **kwargs: None if name == missing or
                    missing == 'providers' and name in ('codex', 'claude') else '/tool'):
                with self.assertRaises(ManagedConfigError):
                    execution_path.save_execution_path(self.source)
                self.assertEqual(self.cache.read_text(), 'old cache')

    def test_failed_atomic_replacement_preserves_cache_and_removes_temporary_file(self):
        self.write_cache('old cache')
        with mock.patch.object(execution_path.shutil, 'which', return_value='/tool'), \
                mock.patch.object(execution_path.os, 'replace', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                execution_path.save_execution_path(self.source)
        self.assertEqual(self.cache.read_text(), 'old cache')
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])

    def test_absent_cache_uses_explicit_current_path_then_system_candidates(self):
        execution_path.apply_execution_path(self.source)
        self.assertEqual(os.environ['PATH'], os.pathsep.join((str(self.current), str(self.system))))
        self.assertFalse(self.cache.exists())

    def test_saved_path_precedes_current_and_is_shared_by_agent_and_runtime(self):
        self.write_cache(os.pathsep.join((str(self.saved), str(self.current), str(self.source / 'gone'))) + '\n')
        execution_path.apply_execution_path(self.source)
        expected = os.pathsep.join((str(self.saved), str(self.current), str(self.system)))
        self.assertEqual(os.environ['PATH'], expected)
        self.assertEqual(agent_environment('codex')['PATH'], expected)
        self.assertEqual(BootstrapService(mock.Mock(), runtime_root=self.source)._runtime_child_environment()['PATH'], expected)

    def test_invalid_or_unreadable_cache_fails_without_mutating_environment_or_cache(self):
        for contents in (b'', b'\n', b'relative', b'/bad\0path', b'/one\n/two', b'\xff'):
            with self.subTest(contents=contents):
                self.cache.write_bytes(contents)
                with self.assertRaisesRegex(ManagedConfigError, 'setup'):
                    execution_path.apply_execution_path(self.source)
                self.assertEqual(os.environ['PATH'], str(self.current))
                self.assertEqual(self.cache.read_bytes(), contents)
        with mock.patch.object(Path, 'read_bytes', side_effect=PermissionError('denied')):
            with self.assertRaises(ManagedConfigError):
                execution_path.apply_execution_path(self.source)

    def test_other_platforms_leave_environment_and_cache_untouched(self):
        self.write_cache('unchanged')
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform), mock.patch.object(execution_path.sys, 'platform', platform):
                execution_path.save_execution_path(self.source)
                execution_path.apply_execution_path(self.source)
                self.assertEqual(self.cache.read_text(), 'unchanged')
                self.assertEqual(os.environ['PATH'], str(self.current))

    def test_source_main_applies_path_before_bootstrap_and_keeps_run_snapshot(self):
        self.write_cache(str(self.saved) + '\n')
        expected = os.pathsep.join((str(self.saved), str(self.current), str(self.system)))
        def prepare(*args, **kwargs):
            self.assertEqual(os.environ['PATH'], expected)
            self.write_cache(str(self.system))
            return mock.Mock()
        def child(*args, **kwargs):
            self.assertEqual(os.environ['PATH'], expected)
            return 7
        with mock.patch.object(loop_manager, 'ROOT', self.source), \
                mock.patch.object(sys, 'argv', ['loop_manager.py', '--config', '/other/config.ini', 'config', 'init']), \
                mock.patch.object(loop_manager, 'process_lock', return_value=nullcontext(mock.Mock())), \
                mock.patch.object(loop_manager, '_write_process_lock_identity'), \
                mock.patch.object(loop_manager, 'prepare_bootstrap_runtime', side_effect=prepare), \
                mock.patch.object(loop_manager, 'run_runtime_child', side_effect=child):
            self.assertEqual(loop_manager.main(), 7)

    def test_bad_cache_blocks_bootstrap_before_any_external_command(self):
        self.write_cache('')
        with mock.patch.object(loop_manager, 'ROOT', self.source), \
                mock.patch.object(sys, 'argv', ['loop_manager.py', 'config', 'init']), \
                mock.patch.object(loop_manager, 'prepare_bootstrap_runtime') as prepare, \
                mock.patch.object(loop_manager, 'process_lock') as lock, \
                mock.patch.object(loop_manager, 'operation_log'):
            self.assertEqual(loop_manager.main(), 1)
        prepare.assert_not_called()
        lock.assert_not_called()

    def test_runtime_child_does_not_read_or_rebuild_path(self):
        self.write_cache('invalid runtime cache')
        arguments = ['loop_manager.py', '--runtime-child', '--runtime-source', str(self.source),
                     '--runtime-sha', 'a' * 40, 'config', 'list']
        with mock.patch.object(sys, 'argv', arguments), \
                mock.patch.object(execution_path, 'apply_execution_path') as apply, \
                mock.patch.object(loop_manager, 'validate_runtime_child'), \
                mock.patch.object(loop_manager, 'validate_runtime_mutation'), \
                mock.patch.object(loop_manager, 'execute_runtime_command', return_value=0):
            self.assertEqual(loop_manager.main(), 0)
        apply.assert_not_called()
        self.assertEqual(os.environ['PATH'], str(self.current))


@unittest.skipUnless(sys.platform == 'linux', 'Linux 실행 파일 탐색 계약')
class LinuxExecutionPathTests(unittest.TestCase):
    def test_setup_script_saves_path_from_outside_checkout_and_preserves_cache_on_prepare_failure(self):
        # 실제 Bash 연결은 setup의 cwd·환경 전달과 준비 실패 시 캐시 보존을 확인한다.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'project'
            (source / 'scripts').mkdir(parents=True)
            package = source / 'loop_manager_internal/config'
            package.mkdir(parents=True)
            for name in ('execution_path.py', 'managed_config.py'):
                shutil.copyfile(root / 'loop_manager_internal/config' / name, package / name)
            shutil.copyfile(root / 'scripts/setup.sh', source / 'scripts/setup.sh')
            (source / 'requirements.txt').write_text('# 의존성 없음\n', encoding='utf-8')
            tools = source / 'tools'
            tools.mkdir()
            for name in ('git', 'gh', 'codex'):
                tool = tools / name
                tool.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
                tool.chmod(0o755)
            python = source / '.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then exit 0; fi\n'
                              f'exec "{sys.executable}" "$@"\n', encoding='utf-8')
            python.chmod(0o755)
            environment = dict(os.environ, PATH=f'{tools}:/usr/bin:/bin')
            command = ['/bin/bash', str(source / 'scripts/setup.sh')]
            result = subprocess.run(command, cwd=directory, env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            cache = source / 'config/execution-path.txt'
            self.assertEqual(cache.read_text(), environment['PATH'] + '\n')
            previous = cache.read_bytes()
            python.write_text('#!/bin/sh\nexit 37\n', encoding='utf-8')
            result = subprocess.run(command, cwd=directory, env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 37)
            self.assertEqual(cache.read_bytes(), previous)

    def test_symlinked_tools_and_env_interpreter_are_found_from_cron_path(self):
        # 실제 프로세스는 PATH 탐색과 env shebang의 의존 실행만 확인한다.
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ):
            source = Path(directory)
            tools = source / 'tools with spaces'
            tools.mkdir()
            for name in ('git', 'gh', 'codex', 'path-interpreter'):
                tool = tools / name
                tool.write_text('#!/bin/sh\nprintf path-found\\n\n', encoding='utf-8')
                tool.chmod(0o755)
            probe = tools / 'path-probe'
            probe.write_text('#!/usr/bin/env path-interpreter\n', encoding='utf-8')
            probe.chmod(0o755)
            link = source / 'selected-tools'
            link.symlink_to(tools, target_is_directory=True)
            os.environ['PATH'] = str(link)
            execution_path.save_execution_path(source)
            self.assertEqual((source / 'config/execution-path.txt').read_text(), str(link) + '\n')
            os.environ['PATH'] = '/usr/bin:/bin'
            execution_path.apply_execution_path(source)
            self.assertEqual(shutil.which('codex'), str(link / 'codex'))
            result = subprocess.run(['path-probe'], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('path-found', result.stdout)
            cache = source / 'config/execution-path.txt'
            cache.unlink()
            cache.symlink_to(source / 'missing-cache')
            with self.assertRaises(ManagedConfigError):
                execution_path.apply_execution_path(source)
