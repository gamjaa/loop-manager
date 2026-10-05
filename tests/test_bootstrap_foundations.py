"""Unit tests for bootstrap effects, ownership evidence and lock handoff."""
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.runtime import bootstrap, process_lock, runtime_identity
from loop_manager_internal.runtime.contracts import (
    BaseWorktreeSyncResult, BootstrapRefreshHandoff, RuntimeWorktree,
    ProcessLockBusyError, WorkspaceOwnershipError,
)


def runtime_service(root, **overrides):
    effects = {name: mock.Mock(name=name) for name in
               bootstrap.BootstrapGitEffects.__dataclass_fields__}
    effects.update(overrides)
    return bootstrap.BootstrapService(bootstrap.BootstrapGitEffects(**effects),
                                      runtime_root=root, interpreter='/chosen/python')


class BootstrapFoundationTests(unittest.TestCase):
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

    def test_refresh_handoff_and_failure_never_start_runtime(self):
        service = runtime_service(Path('/runtime'))
        sha = 'a' * 40
        service.fetch_bootstrap_revision = mock.Mock(return_value=('main', sha))
        service.prepare_runtime_worktree = mock.Mock()
        for status in ('updated', 'skipped'):
            service.effects.synchronize_base_worktree.return_value = BaseWorktreeSyncResult(status, 'reason', sha)
            if status == 'updated':
                self.assertEqual(service.prepare_bootstrap_runtime(Path('/source'), Path('/state'), 'b' * 32),
                                 BootstrapRefreshHandoff('main', sha))
            else:
                with self.assertRaisesRegex(RuntimeError, '갱신 불가'):
                    service.prepare_bootstrap_runtime(Path('/source'), Path('/state'), 'b' * 32)
        service.prepare_runtime_worktree.assert_not_called()

    def test_external_config_interpreter_and_exit_code_survive_cleanup_failure(self):
        service = runtime_service(Path('/runtime'))
        runtime = RuntimeWorktree(Path('/runtime'), 'a' * 40, 'b' * 32)
        config = runtime_identity.source_config_path(Path('/source'), 'custom/config.ini')
        events = []
        service.remove_runtime_worktree = mock.Mock(side_effect=lambda *a: (
            events.append('cleanup') or (_ for _ in ()).throw(RuntimeError('keep runtime'))))
        def child(command, **options):
            events.append('child-exit')
            self.assertEqual(command[:4], ['/chosen/python', '-B', '-u', str(Path('/runtime/loop_manager.py'))])
            self.assertEqual(command[command.index('--config') + 1], str(config))
            self.assertEqual(command[command.index('--runtime-mutation-id') + 1], 'b' * 32)
            self.assertEqual(options, {'cwd': Path('/runtime'), 'check': False})
            return SimpleNamespace(returncode=7)
        with mock.patch.object(bootstrap.subprocess, 'run', side_effect=child):
            self.assertEqual(service.run_runtime_child(Path('/source'), Path('/state'), config, runtime,
                                                      ['--config=old.ini', 'run']), 7)
        self.assertEqual(events, ['child-exit', 'cleanup'])
        service.effects.operation_log.assert_called_once_with('runtime 정리 보류: keep runtime', error=True)

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
