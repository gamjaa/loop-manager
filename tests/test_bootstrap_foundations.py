"""Unit tests for bootstrap effects, ownership evidence and lock handoff."""
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import py_compile
import shutil
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.runtime import bootstrap, process_lock, runtime_identity
from loop_manager_internal.runtime.contracts import (
    BaseWorktreeSyncResult, RuntimeWorktree,
    ProcessLockBusyError, WorkspaceOwnershipError,
)


def runtime_service(root, **overrides):
    effects = {name: mock.Mock(name=name) for name in
               bootstrap.BootstrapGitEffects.__dataclass_fields__}
    effects.update(overrides)
    return bootstrap.BootstrapService(bootstrap.BootstrapGitEffects(**effects),
                                      runtime_root=root, interpreter='/chosen/python')


class BootstrapFoundationTests(unittest.TestCase):
    def test_runtime_bytecode_cache_prefix_separates_source_revision_and_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            other_source = root / 'other-source'; other_source.mkdir()
            runtime = RuntimeWorktree(root / 'runtime', 'a' * 40)
            service = runtime_service(root / 'runtime')
            service.interpreter = str(root / 'python-one')
            prefix = service._runtime_bytecode_cache_prefix(source, root / 'state', runtime)
            self.assertEqual(prefix, service._runtime_bytecode_cache_prefix(source, root / 'state', runtime))
            self.assertNotEqual(prefix, service._runtime_bytecode_cache_prefix(
                source, root / 'state', replace(runtime, commit_sha='b' * 40)))
            self.assertNotEqual(prefix, service._runtime_bytecode_cache_prefix(
                other_source, root / 'state', runtime))
            service.interpreter = str(root / 'python-two')
            self.assertNotEqual(prefix, service._runtime_bytecode_cache_prefix(
                source, root / 'state', runtime))


    def test_runtime_child_reuses_external_bytecode_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            runtime_path = root / 'runtime'; runtime_path.mkdir()
            state = root / 'state'
            package = runtime_path / 'loop_manager_internal'; package.mkdir()
            (package / '__init__.py').write_text('', encoding='utf-8')
            helper = package / 'runtime_helper.py'
            helper.write_text('VALUE = 7\n', encoding='utf-8')
            (runtime_path / 'loop_manager.py').write_text(
                'from loop_manager_internal import runtime_helper\nfrom pathlib import Path\n'
                'Path("observed-cache-path").write_text(runtime_helper.__cached__)\n'
                'raise SystemExit(runtime_helper.VALUE)\n', encoding='utf-8')
            service = runtime_service(runtime_path)
            service.interpreter = sys.executable
            service.effects.git.return_value = SimpleNamespace(
                stdout=('loop_manager.py\0loop_manager_internal/__init__.py\0'
                        'loop_manager_internal/runtime_helper.py\0'),
                returncode=0,
            )
            runtime = RuntimeWorktree(runtime_path, 'a' * 40)
            prefix = service._runtime_bytecode_cache_prefix(source, state, runtime)
            prepared_cache_bytes = []
            def record_prepared_cache(message, **_options):
                if 'checked-hash' in message:
                    caches = list(prefix.rglob('runtime_helper.*.pyc'))
                    if caches:
                        prepared_cache_bytes.append(caches[0].read_bytes())
            service.effects.operation_log.side_effect = record_prepared_cache
            with mock.patch.object(bootstrap.sys, 'dont_write_bytecode', False), \
                    mock.patch.object(bootstrap.sys, 'pycache_prefix', None), mock.patch.dict(os.environ, {
                        'PYTHONDONTWRITEBYTECODE': '', 'PYTHONPYCACHEPREFIX': '',
                    }):
                self.assertEqual(service.run_runtime_child(source, state, source / 'config.ini', runtime, []), 7)
                cache_files = list(prefix.rglob('runtime_helper.*.pyc'))
                self.assertEqual(len(cache_files), 1)
                self.assertFalse((runtime_path / '__pycache__').exists())
                self.assertEqual(Path((runtime_path / 'observed-cache-path').read_text()), cache_files[0])
                self.assertTrue(prepared_cache_bytes, service.effects.operation_log.call_args_list)
                self.assertEqual(int.from_bytes(prepared_cache_bytes[0][4:8], 'little'), 3)
                self.assertEqual(prepared_cache_bytes[0][8:16], importlib.util.source_hash(helper.read_bytes()))
                cached = cache_files[0].read_bytes()
                self.assertEqual(int.from_bytes(cached[4:8], 'little'), 3)
                self.assertEqual(cached[8:16], importlib.util.source_hash(helper.read_bytes()))
                metadata = helper.stat()
                os.utime(helper, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000))

                self.assertEqual(service.run_runtime_child(source, state, source / 'config.ini', runtime, []), 7)
                cached = cache_files[0].read_bytes()
                self.assertEqual(int.from_bytes(cached[4:8], 'little'), 3)
                self.assertEqual(cached[8:16], importlib.util.source_hash(helper.read_bytes()))

                cache_files[0].write_bytes(cached[:16])
                self.assertEqual(service.run_runtime_child(source, state, source / 'config.ini', runtime, []), 7)
                cached = cache_files[0].read_bytes()
                self.assertEqual(int.from_bytes(cached[4:8], 'little'), 3)
                self.assertEqual(cached[8:16], importlib.util.source_hash(helper.read_bytes()))

                cache_files[0].write_bytes(b'corrupt cache')
                self.assertEqual(service.run_runtime_child(source, state, source / 'config.ini', runtime, []), 7)

            cached = cache_files[0].read_bytes()
            self.assertEqual(int.from_bytes(cached[4:8], 'little'), 3)
            self.assertEqual(cached[8:16], importlib.util.source_hash(helper.read_bytes()))
            logs = [call.args[0] for call in service.effects.operation_log.call_args_list]
            self.assertTrue(any('created=3 reused=0' in line for line in logs), logs)
            self.assertTrue(any('created=0 reused=3' in line for line in logs), logs)
            self.assertEqual(
                sum('created=0 reused=2 replaced=1' in line for line in logs), 2, logs,
            )


    def test_runtime_child_propagates_command_line_cache_prefix_to_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            runtime_path = root / 'runtime'; runtime_path.mkdir()
            cache_prefix = root / 'command-line-cache'
            service = runtime_service(runtime_path)
            service._prepare_runtime_bytecode_cache = mock.Mock(return_value=False)
            runtime = RuntimeWorktree(runtime_path, 'a' * 40)
            with mock.patch.object(bootstrap.sys, 'dont_write_bytecode', False), \
                    mock.patch.object(bootstrap.sys, 'pycache_prefix', str(cache_prefix)), \
                    mock.patch.dict(os.environ, {
                        'PYTHONDONTWRITEBYTECODE': '', 'PYTHONPYCACHEPREFIX': '',
                    }), mock.patch.object(
                        bootstrap.subprocess, 'run', return_value=SimpleNamespace(returncode=0),
                    ) as child:
                self.assertEqual(service.run_runtime_child(
                    source, root / 'state', source / 'config.ini', runtime, []), 0)

            command = child.call_args.args[0]
            self.assertEqual(command[command.index('-X') + 1], f'pycache_prefix={cache_prefix}')
            self.assertEqual(child.call_args.kwargs['env']['PYTHONPYCACHEPREFIX'], str(cache_prefix))


    def test_runtime_child_uses_fresh_prefix_when_corrupt_cache_cannot_be_repaired(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            runtime_path = root / 'runtime'; runtime_path.mkdir()
            helper = runtime_path / 'runtime_helper.py'
            helper.write_text('VALUE = 7\n', encoding='utf-8')
            (runtime_path / 'loop_manager.py').write_text(
                'import runtime_helper\nfrom pathlib import Path\n'
                'Path("observed-cache-path").write_text(runtime_helper.__cached__)\n'
                'raise SystemExit(runtime_helper.VALUE)\n', encoding='utf-8')
            service = runtime_service(runtime_path)
            service.interpreter = sys.executable
            service.effects.git.return_value = SimpleNamespace(
                stdout='loop_manager.py\0runtime_helper.py\0', returncode=0,
            )
            runtime = RuntimeWorktree(runtime_path, 'a' * 40)
            cache_prefix = root / 'user-cache'
            with mock.patch.object(bootstrap.sys, 'dont_write_bytecode', False), \
                    mock.patch.object(bootstrap.sys, 'pycache_prefix', str(cache_prefix)), \
                    mock.patch.dict(os.environ, {
                        'PYTHONDONTWRITEBYTECODE': '',
                        'PYTHONPYCACHEPREFIX': str(cache_prefix),
                    }):
                cache = Path(importlib.util.cache_from_source(str(helper)))
                cache.parent.mkdir(parents=True)
                py_compile.compile(
                    str(helper), str(cache), doraise=True,
                    invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
                )
                cache.write_bytes(cache.read_bytes()[:16])
                corrupt_cache = cache.read_bytes()
                real_run = subprocess.run

                def fail_cache_repair(command, *args, **options):
                    if '-c' in command:
                        return subprocess.CompletedProcess(
                            command, 0,
                            stdout='{"created":0,"reused":0,"replaced":0,"unavailable":1,"missing":0}',
                            stderr='',
                        )
                    return real_run(command, *args, **options)

                with mock.patch.object(bootstrap.subprocess, 'run', side_effect=fail_cache_repair):
                    self.assertEqual(service.run_runtime_child(
                        source, root / 'state', source / 'config.ini', runtime, []), 7)

            self.assertEqual(cache.read_bytes(), corrupt_cache)
            used_cache = Path((runtime_path / 'observed-cache-path').read_text())
            self.assertFalse(used_cache.is_relative_to(cache_prefix))


    def test_runtime_child_respects_explicit_bytecode_disable_and_cache_prefix(self):
        for disable_with_flag in (False, True):
            with self.subTest(disable_with_flag=disable_with_flag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'source'; source.mkdir()
                runtime_path = root / 'runtime'; runtime_path.mkdir()
                custom_prefix = root / 'user-cache'
                (runtime_path / 'runtime_helper.py').write_text('VALUE = 7\n', encoding='utf-8')
                (runtime_path / 'loop_manager.py').write_text(
                    'import runtime_helper\nraise SystemExit(runtime_helper.VALUE)\n', encoding='utf-8')
                service = runtime_service(runtime_path)
                service.interpreter = sys.executable
                runtime = RuntimeWorktree(runtime_path, 'a' * 40)
                environment = {
                    'PYTHONDONTWRITEBYTECODE': '1',
                    'PYTHONPYCACHEPREFIX': str(custom_prefix),
                }
                with mock.patch.object(bootstrap.sys, 'pycache_prefix', None), \
                        mock.patch.dict(os.environ, environment):
                    if disable_with_flag:
                        with mock.patch.object(bootstrap.sys, 'dont_write_bytecode', True):
                            result = service.run_runtime_child(
                                source, root / 'state', source / 'config.ini', runtime, [])
                    else:
                        result = service.run_runtime_child(
                            source, root / 'state', source / 'config.ini', runtime, [])
                self.assertEqual(result, 7)
                self.assertFalse(custom_prefix.exists())
                self.assertFalse((runtime_path / '__pycache__').exists())


    def test_runtime_child_executes_source_when_cache_path_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            runtime_path = root / 'runtime'; runtime_path.mkdir()
            cache_path = root / 'user-cache'
            cache_path.write_text('preserve user file\n', encoding='utf-8')
            (runtime_path / 'runtime_helper.py').write_text('VALUE = 7\n', encoding='utf-8')
            (runtime_path / 'loop_manager.py').write_text(
                'import runtime_helper\nfrom pathlib import Path\nimport sys\n'
                'Path("observed-cache-prefix").write_text(sys.pycache_prefix)\n'
                'raise SystemExit(runtime_helper.VALUE)\n', encoding='utf-8')
            service = runtime_service(runtime_path)
            service.interpreter = sys.executable
            service.effects.git.return_value = SimpleNamespace(
                stdout='loop_manager.py\0runtime_helper.py\0', returncode=0,
            )
            runtime = RuntimeWorktree(runtime_path, 'a' * 40)
            with mock.patch.object(bootstrap.sys, 'pycache_prefix', None), \
                    mock.patch.dict(os.environ, {
                        'PYTHONDONTWRITEBYTECODE': '',
                        'PYTHONPYCACHEPREFIX': str(cache_path),
                    }):
                self.assertEqual(service.run_runtime_child(
                    source, root / 'state', source / 'config.ini', runtime, []), 7)

            self.assertEqual(cache_path.read_text(encoding='utf-8'), 'preserve user file\n')
            self.assertFalse((runtime_path / '__pycache__').exists())
            self.assertNotEqual(
                (runtime_path / 'observed-cache-prefix').read_text(encoding='utf-8'),
                str(cache_path),
            )


    def test_runtime_child_executes_source_when_fallback_cache_creation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'; source.mkdir()
            runtime_path = root / 'runtime'; runtime_path.mkdir()
            user_cache = root / 'user-cache'; user_cache.mkdir()
            sentinel = user_cache / 'preserve.txt'
            sentinel.write_text('keep user cache\n', encoding='utf-8')
            (runtime_path / 'runtime_helper.py').write_text('VALUE = 7\n', encoding='utf-8')
            (runtime_path / 'loop_manager.py').write_text(
                'import runtime_helper\nfrom pathlib import Path\nimport sys\n'
                'Path("observed-cache-prefix").write_text(sys.pycache_prefix)\n'
                'raise SystemExit(runtime_helper.VALUE)\n', encoding='utf-8')
            service = runtime_service(runtime_path)
            service.interpreter = sys.executable
            service._prepare_runtime_bytecode_cache = mock.Mock(return_value=True)
            runtime = RuntimeWorktree(runtime_path, 'a' * 40)
            with mock.patch.object(bootstrap.sys, 'dont_write_bytecode', False), \
                    mock.patch.object(bootstrap.sys, 'pycache_prefix', str(user_cache)), \
                    mock.patch.dict(os.environ, {
                        'PYTHONDONTWRITEBYTECODE': '',
                        'PYTHONPYCACHEPREFIX': str(user_cache),
                    }), mock.patch.object(
                        bootstrap.tempfile, 'TemporaryDirectory',
                        side_effect=OSError('temporary storage unavailable'),
                    ), mock.patch.object(
                        bootstrap.tempfile, 'gettempdir',
                        side_effect=FileNotFoundError('no writable temporary directory'),
                    ):
                self.assertEqual(service.run_runtime_child(
                    source, root / 'state', source / 'config.ini', runtime, []), 7)

            observed_prefix = Path((runtime_path / 'observed-cache-prefix').read_text())
            self.assertEqual(observed_prefix, runtime_path / 'loop_manager.py')
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep user cache\n')
            self.assertTrue(any(
                '임시 디렉터리 생성 실패' in call.args[0]
                for call in service.effects.operation_log.call_args_list
            ))


    def test_lock_identity_contention_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with process_lock.process_lock(state, identity='bootstrap:abc') as handle:
                with self.assertRaises(ProcessLockBusyError):
                    with process_lock.process_lock(state):
                        self.fail('second owner acquired the process lease')
                process_lock._write_process_lock_identity(handle, 'runtime:abc')
                self.assertEqual(process_lock._held_process_lock_identity(state), 'runtime:abc')
            with self.assertRaisesRegex(RuntimeError, 'process lock'):
                process_lock._held_process_lock_identity(state)
            with process_lock.process_lock(state, identity='new'):
                self.assertEqual(process_lock._held_process_lock_identity(state), 'new')

    def test_bootstrap_checks_revision_before_and_after_runtime_selection(self):
        service = runtime_service(Path('/runtime'))
        selected = RuntimeWorktree(Path('/runtime'), 'a' * 40, 'b' * 32)
        events = []
        service.fetch_bootstrap_revision = mock.Mock(side_effect=lambda *a: (
            events.append('fetch') or ('main', selected.commit_sha)))
        service.effects.synchronize_base_worktree.return_value = BaseWorktreeSyncResult(
            'equal', 'dirty source preserved', selected.commit_sha)
        service.validate_bootstrap_revision = mock.Mock(side_effect=lambda *a: events.append('validate'))
        service.prepare_runtime_worktree = mock.Mock(side_effect=lambda *a, **k: (
            events.append('select') or selected))
        self.assertEqual(service.prepare_bootstrap_runtime(Path('/source'), Path('/state'), 'b' * 32), selected)
        self.assertEqual(events, ['fetch', 'validate', 'select', 'validate'])
        service.effects.synchronize_base_worktree.assert_called_once_with(
            Path('/source'), 'main', remote_sha=selected.commit_sha,
            state_dir=Path('/state'), owner='bootstrap')

    def test_sync_update_skip_failure_and_exception_still_select_remote_runtime(self):
        service = runtime_service(Path('/runtime'))
        sha = 'a' * 40
        service.fetch_bootstrap_revision = mock.Mock(return_value=('main', sha))
        selected = RuntimeWorktree(Path('/runtime'), sha, 'b' * 32)
        service.prepare_runtime_worktree = mock.Mock(return_value=selected)
        service.validate_bootstrap_revision = mock.Mock()
        for status in ('updated', 'skipped', 'failed'):
            service.effects.synchronize_base_worktree.return_value = BaseWorktreeSyncResult(status, 'reason', sha)
            self.assertEqual(service.prepare_bootstrap_runtime(Path('/source'), Path('/state'), 'b' * 32), selected)
        service.effects.synchronize_base_worktree.side_effect = RuntimeError('source unavailable')
        self.assertEqual(service.prepare_bootstrap_runtime(Path('/source'), Path('/state'), 'b' * 32), selected)
        self.assertEqual(service.prepare_runtime_worktree.call_count, 4)
        service.prepare_runtime_worktree.assert_called_with(
            Path('/source'), Path('/state'), mutation_id='b' * 32, commit_sha=sha)

    def test_remote_revision_race_prevents_runtime_selection(self):
        service = runtime_service(Path('/runtime'), git=lambda *a: SimpleNamespace(stdout='c' * 40))
        with self.assertRaisesRegex(RuntimeError, 'bootstrap revision 변경'):
            service.validate_bootstrap_revision(Path('/source'), 'develop', 'a' * 40)
        service.fetch_bootstrap_revision = mock.Mock(return_value=('develop', 'a' * 40))
        service.effects.synchronize_base_worktree.return_value = BaseWorktreeSyncResult('failed', 'local failure', 'a' * 40)
        service.prepare_runtime_worktree = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, 'bootstrap revision 변경'):
            service.prepare_bootstrap_runtime(Path('/source'), Path('/state'), 'b' * 32)
        service.prepare_runtime_worktree.assert_not_called()
        service.effects = replace(service.effects, git=mock.Mock(side_effect=[SimpleNamespace(stdout='a' * 40), SimpleNamespace(stdout='c' * 40)]))
        service.prepare_runtime_worktree.return_value = RuntimeWorktree(Path('/runtime'), 'a' * 40, 'b' * 32)
        with self.assertRaisesRegex(RuntimeError, 'bootstrap revision 변경'):
            service.prepare_bootstrap_runtime(Path('/source'), Path('/state'), 'b' * 32)
        service.prepare_runtime_worktree.assert_called_once()

    def test_external_config_interpreter_and_exit_code_survive_cleanup_failure(self):
        service = runtime_service(Path('/runtime'))
        runtime = RuntimeWorktree(Path('/runtime'), 'a' * 40, 'b' * 32)
        config = runtime_identity.source_config_path(Path('/source'), 'custom/config.ini')
        service.effects.git.return_value = SimpleNamespace(stdout='', returncode=0)
        service._prepare_runtime_bytecode_cache = mock.Mock(return_value=True)
        events = []
        service.remove_runtime_worktree = mock.Mock(side_effect=lambda *a: (
            events.append('cleanup') or (_ for _ in ()).throw(RuntimeError('keep runtime'))))
        def fail_cache_cleanup():
            events.append('cache-cleanup')
            raise OSError('cache cleanup denied')
        fallback_cache = SimpleNamespace(
            name='/tmp/loop-manager-runtime-bytecode-test',
            cleanup=mock.Mock(side_effect=fail_cache_cleanup),
        )
        def child(command, **options):
            events.append('child-exit')
            self.assertEqual(command[:2], ['/chosen/python', '-X'])
            self.assertTrue(command[2].startswith('pycache_prefix='))
            self.assertIn(str(Path('/runtime/loop_manager.py')), command)
            self.assertNotIn('-B', command)
            self.assertEqual(command[command.index('--config') + 1], str(config))
            self.assertEqual(command[command.index('--runtime-mutation-id') + 1], 'b' * 32)
            self.assertEqual(options['cwd'], Path('/runtime'))
            self.assertFalse(options['check'])
            self.assertFalse(options['env'].get('PYTHONPYCACHEPREFIX'))
            return SimpleNamespace(returncode=7)
        with mock.patch.object(bootstrap.sys, 'dont_write_bytecode', False), \
                mock.patch.object(bootstrap.sys, 'pycache_prefix', None), \
                mock.patch.dict(os.environ, {
                    'PYTHONDONTWRITEBYTECODE': '', 'PYTHONPYCACHEPREFIX': '',
                }), mock.patch.object(
                    bootstrap.tempfile, 'TemporaryDirectory', return_value=fallback_cache,
                ), mock.patch.object(bootstrap.subprocess, 'run', side_effect=child):
            self.assertEqual(service.run_runtime_child(Path('/source'), Path('/state'), config, runtime,
                                                      ['--config=old.ini', 'run']), 7)
        self.assertEqual(events, ['child-exit', 'cache-cleanup', 'cleanup'])
        fallback_cache.cleanup.assert_called_once()
        service.effects.operation_log.assert_any_call(
            'runtime Python bytecode 임시 cache 정리 보류: cache cleanup denied', error=True,
        )
        service.effects.operation_log.assert_any_call('runtime 정리 보류: keep runtime', error=True)

    def test_owned_runtime_can_be_recreated_at_same_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            state = Path(directory) / 'state'
            sha = 'a' * 40
            records = []
            operations = []
            def git(repo, *args, **kwargs):
                operations.append(args[:2])
                if args[:2] == ('worktree', 'add'):
                    path = Path(args[-2]); path.mkdir()
                    reason = next(a.removeprefix('--reason=') for a in args if a.startswith('--reason='))
                    records.append({'worktree': str(path), 'HEAD': sha, 'detached': True, 'locked': reason})
                elif args[:2] == ('worktree', 'unlock'):
                    records[0].pop('locked')
                elif args[:2] == ('worktree', 'remove'):
                    shutil.rmtree(args[-1]); records.clear()
                return SimpleNamespace(stdout=sha if args[0] == 'rev-parse' else '', returncode=0)
            service = runtime_service(source, git=git, _worktree_catalog=lambda source: records,
                                      _git_operation_entries=lambda path: {}, _ignored_worktree_paths=lambda path: ())
            runtime = service.prepare_runtime_worktree(source, state, mutation_id='b' * 32, commit_sha=sha)
            remove = service.remove_runtime_worktree
            def checked_remove(*args):
                self.assertFalse((runtime.path / '__pycache__').exists())
                remove(*args)
            with mock.patch.object(service, 'remove_runtime_worktree', side_effect=checked_remove) as cleanup, \
                    mock.patch.object(bootstrap.subprocess, 'run', return_value=SimpleNamespace(returncode=7)):
                self.assertEqual(service.run_runtime_child(source, state, source / 'config.ini', runtime, []), 7)
                cleanup.assert_called_once_with(source, state, runtime)
            self.assertFalse(runtime.path.exists())
            replacement = service.prepare_runtime_worktree(source, state, mutation_id='c' * 32, commit_sha=sha)
            self.assertEqual(replacement.path, runtime.path)
            self.assertEqual(replacement.commit_sha, sha)
            self.assertEqual(runtime_identity._read_runtime_intent(state, 'c' * 32, source, runtime.path, sha),
                             (True, 'loop-manager-runtime:' + 'c' * 32))
            self.assertEqual(operations.count(('worktree', 'add')), 2)

    def test_existing_runtime_without_ownership_evidence_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'; source.mkdir()
            state = Path(directory) / 'state'; sha = 'a' * 40
            runtime = runtime_identity.expected_runtime_worktree(state, source, sha)
            runtime.mkdir(parents=True)
            sentinel = runtime / 'user.txt'; sentinel.write_text('keep')
            git = mock.Mock(return_value=SimpleNamespace(stdout='', returncode=0))
            service = runtime_service(source, git=git,
                _worktree_catalog=lambda path: [{'worktree': str(runtime), 'HEAD': sha, 'detached': True}],
                _git_operation_entries=lambda path: {})
            service.validate_runtime_worktree = mock.Mock()
            with self.assertRaisesRegex(WorkspaceOwnershipError, '소유 증거'):
                service.prepare_runtime_worktree(source, state, mutation_id='b' * 32, commit_sha=sha)
            self.assertEqual(sentinel.read_text(), 'keep')
            git.assert_not_called()

    def test_child_identity_uses_explicit_runtime_root_and_parent_lease(self):
        source = Path('/source'); state = Path('/state'); sha = 'a' * 40
        runtime = runtime_identity.expected_runtime_worktree(state, source, sha)
        service = runtime_service(runtime, repository_origin=lambda path: 'origin',
                                  git=lambda *a: SimpleNamespace(stdout=sha))
        service.bootstrap_base_branch = mock.Mock(return_value='main')
        service.validate_runtime_worktree = mock.Mock()
        service._validated_runtime_registration_marker = mock.Mock(return_value='owned')
        with mock.patch.object(bootstrap, '_held_process_lock_identity', return_value='runtime:' + 'b' * 32):
            identity = service.validate_runtime_mutation(source, state, sha, 'b' * 32)
        self.assertEqual(identity.path, runtime)
        self.assertEqual(identity.registration_lock_reason, 'owned')
        with mock.patch.object(bootstrap, '_held_process_lock_identity', return_value='0'):
            legacy_identity = service.validate_runtime_mutation(source, state, sha, None)
        self.assertTrue(legacy_identity.legacy)
        self.assertEqual(legacy_identity.path, runtime)
        service._validated_runtime_registration_marker.assert_called_with(
            source, state, runtime, sha, None, legacy=True, record_legacy_adoption=True)
        with mock.patch.object(bootstrap, '_held_process_lock_identity', return_value='runtime:' + 'b' * 32):
            with self.assertRaisesRegex(RuntimeError, 'mutation ID가 없는 새 형식'):
                service.validate_runtime_mutation(source, state, sha, None)
        with mock.patch.object(bootstrap, '_held_process_lock_identity', return_value='runtime:other'):
            with self.assertRaisesRegex(RuntimeError, '부모 process lock'):
                service.validate_runtime_mutation(source, state, sha, 'b' * 32)
        service.runtime_root = Path('/other-runtime')
        with self.assertRaisesRegex(RuntimeError, 'runtime 경로'):
            service.validate_runtime_mutation(source, state, sha, 'b' * 32)
