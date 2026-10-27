"""Unit checks for effect boundaries; Git and GitHub are always fake ports."""
from dataclasses import fields, replace
from pathlib import Path
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.catalog import archive_git, execplan_archive
from loop_manager_internal.git import command_adapter, git_adapter, manager_git_journal, source_sync, workspace
from loop_manager_internal.recovery import pull_request_recovery
from loop_manager_internal.runtime.contracts import BaseWorktreeSnapshot, WorkspaceOwnershipError
from loop_manager_internal.storage.execplan_archive_state import MergedArchiveCandidate
from tests.support.database import runtime_connection


def fake_effects(contract):
    return contract(**{field.name: mock.Mock(name=field.name) for field in fields(contract)})


class CheckpointRecoveryBoundaryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch(
            "loop_manager_internal.recovery.plan_recovery.resource_locks",
            side_effect=lambda *args, **kwargs: nullcontext()))
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.snapshot = loop_manager.GitSnapshot('published', 'loop-manager/G-047', {}, {}, {}, {}, {}, {})
        self.connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED', branch = 'loop-manager/G-047', "
            "worktree = ?, blocked_reason = 'verify_retry_exhausted'", (str(Path('/state/G-047')),))
        self.connection.execute('UPDATE projects SET planning_attempt_fingerprint = NULL')
        self.connection.commit()

    def plan(self):
        return self.connection.execute('SELECT * FROM exec_plans').fetchone()

    def test_candidate_probe_uses_injected_config_and_preserves_errors(self):
        config = Path('/missing-runtime/config.ini')
        with (mock.patch.object(loop_manager, 'load_managed_config',
                  return_value=SimpleNamespace(verify_timeout_seconds=731)) as loader,
              mock.patch.object(loop_manager, '_remote_checkpoint_review_revision', return_value=('head', 'base')),
              mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=self.snapshot),
              mock.patch.object(loop_manager, 'validate_agent_scope'),
              mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')) as verify):
            def probe(timeout=None):
                return loop_manager._evaluate_remote_checkpoint_cause(
                    self.connection, Path('/repo'), Path('/state'), 'repo', self.plan(), config, timeout)
            self.assertEqual(probe(), (True, 'OK', None))
            loader.assert_called_once_with(config)
            verify.assert_called_once_with(Path('/state/G-047'), 731)
            loader.reset_mock()
            self.assertEqual(probe(19), (True, 'OK', None))
            loader.assert_not_called()
            verify.assert_called_with(Path('/state/G-047'), 19)
            for error in (loop_manager.VerifyTerminationUncertain('termination uncertain'), RuntimeError('retry candidate')):
                with self.subTest(error=type(error).__name__):
                    verify.side_effect = error
                    with self.assertRaises(type(error)) as raised:
                        probe()
                    self.assertIs(raised.exception, error)
            self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')

    def test_timeout_block_uses_injected_default_config_without_releasing(self):
        loop_manager.runtime_state.RuntimeStateRepository(self.connection).block_plan(
            'repo', 'G-047', reason_code='verify_timeout_limit_insufficient', evidence='timeout',
            release_fingerprint='600', plan_values={'blocked_fingerprint': '600'})
        with (mock.patch.object(loop_manager, 'load_managed_config',
                  return_value=SimpleNamespace(verify_timeout_seconds=600)) as loader,
              mock.patch.object(loop_manager.blocked_plan_reconciliation.BlockedPlanReconciler,
                  'reconcile', return_value='unchanged'),
              mock.patch.object(loop_manager, '_resume_interrupted_blocked_rebase', return_value=None)):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, Path('/repo'), Path('/state'), 'repo', 'main'))
        loader.assert_called_once_with(loop_manager.ROOT / 'config/config.ini')
        self.assertEqual(self.plan()['blocked_reason'], 'verify_timeout_limit_insufficient')

    def test_diagnostic_callback_is_resolved_after_reconciler_effects_creation(self):
        effects = loop_manager._project_blocked_recovery_effects()
        with mock.patch.object(loop_manager, 'push_diagnostic_checkpoint', side_effect=KeyboardInterrupt) as push:
            with self.assertRaises(KeyboardInterrupt):
                effects.push_diagnostic_checkpoint('adapter', 'request', 'head')
        push.assert_called_once_with('adapter', 'request', 'head')



class WorkspaceEffectBoundaryTests(unittest.TestCase):
    def test_changed_files_preserves_nul_paths_in_all_git_modes(self):
        from loop_manager_internal.git import workspace_service
        from loop_manager_internal.agents import scope_observation
        raw_names = [b' leading.txt', b'tab\tname', b'line\nname',
                     b'carriage\rname', b'back\\slash', '한글.txt'.encode('utf-8')]
        if os.name != 'nt':
            raw_names.append(b'raw-\xff')  # POSIX에서만 유효한 비 UTF-8 파일명도 보존한다.
        names = {os.fsdecode(name) for name in raw_names}
        effects = fake_effects(workspace_service.ChangedFilesEffects)
        raw_effects = fake_effects(scope_observation.ObservationEffects)
        raw_effects.git_bytes.side_effect = lambda repo, *args: (
            b''.join(b'1\t0\t' + name + b'\0' for name in raw_names)
            if '--numstat' in args else b'\0'.join(raw_names) + b'\0')
        effects._nul_git_paths.side_effect = lambda repo, *args: (
            scope_observation._nul_git_paths(repo, *args, effects=raw_effects))
        effects._unstaged_git_paths.side_effect = lambda repo: (
            scope_observation._unstaged_git_paths(repo, effects=raw_effects))
        self.assertEqual(workspace_service.changed_files(effects, Path('/repo')), sorted(names))
        self.assertEqual(workspace_service.changed_files(effects, Path('/repo'), 'base'), sorted(names))
        self.assertTrue(all('-z' in call.args for call in raw_effects.git_bytes.call_args_list))
        self.assertTrue(any('--cached' in call.args for call in raw_effects.git_bytes.call_args_list))
        self.assertTrue(all('--no-renames' in call.args
                            for call in raw_effects.git_bytes.call_args_list
                            if 'diff' in call.args))

    def test_changed_files_uses_content_observation_in_planner_composition(self):
        calls = []

        def git_bytes(repo, *args):
            calls.append(args)
            if '--numstat' in args:
                return b'1\t0\treal edit\0-\t-\tbinary.bin\0'  # No stat-only paths.
            if '--cached' in args:
                return b'staged.txt\0'
            if args[0] == 'ls-files':
                return b'new.txt\0'
            return b'unchanged copied file\0'  # Stale name-only index stat.

        with mock.patch.object(loop_manager, 'git_bytes', side_effect=git_bytes):
            self.assertEqual(loop_manager.changed_files(Path('/repo')),
                             ['binary.bin', 'new.txt', 'real edit', 'staged.txt'])
        self.assertFalse(any('--name-only' in args and '--cached' not in args
                             for args in calls))

    def test_changed_files_uses_content_evidence_without_refreshing_selected_index(self):
        from loop_manager_internal.git import workspace_service
        effects = fake_effects(workspace_service.ChangedFilesEffects)
        effects._unstaged_git_paths.return_value = ['binary.bin', 'deleted.py', 'mode.sh']
        effects._nul_git_paths.side_effect = lambda root, *args: (
            ['staged.py'] if '--cached' in args else ['untracked.py'])
        self.assertEqual(workspace_service.changed_files(effects, Path('/repo')),
                         ['binary.bin', 'deleted.py', 'mode.sh', 'staged.py', 'untracked.py'])
        effects._unstaged_git_paths.assert_called_once_with(Path('/repo'))
        self.assertFalse(any('diff' in call.args and '--cached' not in call.args
                             for call in effects._nul_git_paths.call_args_list))

    def test_changed_files_composition_ignores_stat_only_diff_names(self):
        def git_bytes(root, *args):
            return b'unchanged.py\0' if '--name-only' in args and '--cached' not in args else b''
        with mock.patch.object(loop_manager, 'git_bytes', side_effect=git_bytes) as read:
            self.assertEqual(loop_manager.changed_files(Path('/repo')), [])
        self.assertTrue(any('--numstat' in call.args for call in read.call_args_list))
        self.assertFalse(any('--name-only' in call.args and '--cached' not in call.args
                             for call in read.call_args_list))

    def test_rebase_ignored_overlap_refuses_before_subprocess(self):
        effects = fake_effects(command_adapter.GitEffects)
        effects._ignored_paths_overlapping_git_update.return_value = ['cache/file']
        result = command_adapter.git(effects, Path('/repo'), 'rebase', 'base', check=False)
        self.assertEqual(result.returncode, 1)
        effects.command.assert_not_called()

    def test_ignored_git_update_overlap_folds_case_for_same_parent_and_child_paths(self):
        for ignored, tracked in (('CACHE/file', 'cache/file'), ('CACHE', 'cache/child'),
                                  ('CACHE/file/child', 'cache/file')):
            with self.subTest(ignored=ignored, tracked=tracked):
                effects = fake_effects(git_adapter.IgnoredPathsOverlappingGitUpdateEffects)
                effects._ignored_worktree_paths.return_value = (ignored,)
                effects._nul_git_paths.return_value = [tracked]
                effects._paths_overlap.side_effect = git_adapter._paths_overlap
                effects.git.side_effect = lambda _repo, *args: subprocess.CompletedProcess(
                    [], 0, 'base' if args[0] == 'merge-base' else '')
                self.assertEqual(git_adapter._ignored_paths_overlapping_git_update(
                    effects, Path('/repo'), 'target'), [ignored])

    def test_ignored_rebase_resume_overlap_folds_case_for_every_action(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory)
            (metadata / 'orig-head').write_text('before')
            (metadata / 'onto').write_text('target')
            for action in ('--continue', '--skip', '--abort'):
                with self.subTest(action=action):
                    effects = fake_effects(git_adapter.RebaseResumeIgnoredOverlapEffects)
                    effects._ignored_worktree_paths.return_value = ('CACHE/file',)
                    effects._nul_git_paths.return_value = ['cache/file']
                    effects._paths_overlap.side_effect = git_adapter._paths_overlap
                    effects._remaining_rebase_commits.return_value = ['next']
                    effects.git_bytes.return_value = os.fsencode(metadata)
                    reason = git_adapter._rebase_resume_ignored_overlap(effects, Path('/repo'), action)
                    self.assertIn('CACHE/file', reason)

    def test_owned_detached_rebase_is_the_only_branch_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            effects = fake_effects(workspace.ValidateManagedWorktreeEffects)
            effects._worktree_catalog.return_value = [{'worktree': directory, 'detached': True}]
            effects._rebase_in_progress_for_branch.return_value = False
            with self.assertRaises(WorkspaceOwnershipError):
                workspace.validate_managed_worktree(effects, worktree, worktree, 'owned', allow_rebase=True)
            effects._rebase_in_progress_for_branch.return_value = True
            workspace.validate_managed_worktree(effects, worktree, worktree, 'owned', allow_rebase=True)
            with self.assertRaises(WorkspaceOwnershipError):
                workspace.validate_managed_worktree(effects, worktree, worktree, 'owned')

    def test_reserved_workspace_dirty_or_wrong_head_never_proves_creation(self):
        effects = fake_effects(workspace.ValidateReservedWorktreeEffects)
        clean = loop_manager.GitSnapshot('expected', 'owned', {}, {}, {}, {}, {}, {})
        for snapshot in (replace(clean, head='foreign'), replace(clean, dirty={'app.py': (('unstaged',), 'hash')})):
            with self.subTest(snapshot=snapshot):
                effects.capture_git_snapshot.return_value = snapshot
                with self.assertRaises(WorkspaceOwnershipError):
                    workspace.validate_reserved_worktree(effects, Path('/repo'), Path('/managed'), 'owned', 'expected')

    def source_effects(self, snapshots, *, current_remote='target'):
        effects = fake_effects(source_sync.SynchronizeBaseWorktreeEffects)
        effects._base_worktree_snapshot.side_effect = snapshots
        effects._base_worktree_index_error.return_value = None
        effects._nul_git_paths.return_value = ('remote.py',)
        effects._base_paths_overlap.return_value = False
        reads = iter(('target', current_remote))

        def git(_repo, *arguments, **_options):
            if arguments[:1] == ('rev-parse',):
                output = 'before' if arguments[1] == 'HEAD' else next(reads)
                return subprocess.CompletedProcess([], 0, output)
            if arguments[:1] == ('merge-base',):
                return subprocess.CompletedProcess([], 0, '')
            if arguments[:1] == ('merge',):
                return subprocess.CompletedProcess([], 0, '')
            raise AssertionError(arguments)

        effects.git.side_effect = git
        return effects

    def test_source_oid_race_is_reobserved_before_mutation(self):
        before = BaseWorktreeSnapshot('main', 'before', (), (), ())
        effects = self.source_effects([before, before], current_remote='raced')
        result = source_sync.synchronize_base_worktree(effects, Path('/repo'), 'main', remote_sha='target')
        self.assertEqual(result.status, 'skipped')
        self.assertFalse(any(call.args[1] == 'merge' for call in effects.git.call_args_list))

    def test_source_dirty_state_is_preserved_and_rechecked(self):
        before = BaseWorktreeSnapshot('main', 'before', (), (('unstaged', 'local.py', 'hash'),), ())
        effects = self.source_effects([before, replace(before, changes=(('unstaged', 'local.py', 'changed'),))])
        result = source_sync.synchronize_base_worktree(effects, Path('/repo'), 'main', remote_sha='target')
        self.assertEqual(result.status, 'skipped')
        effects._base_paths_overlap.assert_called_once_with('local.py', 'remote.py')
        self.assertFalse(any(call.args[1] == 'merge' for call in effects.git.call_args_list))


class PublicationObservationBoundaryTests(unittest.TestCase):
    def test_archive_requires_the_current_regular_file_and_original_hash(self):
        effects = fake_effects(archive_git.ArchiveGitEffects)
        adapter = archive_git.ExecPlanArchiveGitPort(Path('/repo'), Path('/state'), 'repo', effects=lambda: effects)
        candidate = MergedArchiveCandidate('G-001', 'docs/plans/Work.md', 'docs/plans/archive/Work.md', 'hash', 'base')
        original = execplan_archive.ArchiveTreeEntry('100644', 'blob', 'oid', 'hash')
        for entry, expected in ((original, True), (replace(original, content_hash='edited'), False),
                                (replace(original, mode='120000'), False), (None, False)):
            with self.subTest(entry=entry), mock.patch.object(adapter, '_entry', side_effect=[entry, None]):
                self.assertEqual(adapter.definitions_current('base', (candidate,)), expected)
        effects.git.assert_not_called()

    def test_archive_remote_race_does_not_report_a_confirmed_publication(self):
        effects = fake_effects(archive_git.ArchiveGitEffects)
        effects.git.side_effect = [subprocess.CompletedProcess([], 0, 'https://example.test/repo\n'),
                                   subprocess.CompletedProcess([], 1, 'remote changed')]
        effects._recorded_base_fetch.return_value = 'foreign'
        adapter = archive_git.ExecPlanArchiveGitPort(Path('/repo'), Path('/state'), 'repo', effects=lambda: effects)
        result = adapter.push_fast_forward(execplan_archive.PreparedArchive('operation', 'base', 'tree', 'archive'), 'main')
        self.assertFalse(result.success)
        self.assertTrue(result.base_changed)
        self.assertEqual(result.remote_oid, 'foreign')
        self.assertEqual(effects.git.call_args.args[1:], ('push', '--', 'https://example.test/repo', 'archive:refs/heads/main'))
        effects._recorded_base_fetch.assert_called_once_with(Path('/repo'), Path('/state'), 'main', owner='project')

    def test_exact_remote_parser_rejects_wrong_ref_duplicate_and_invalid_oid(self):
        ref, oid = 'refs/heads/owned', 'a' * 40
        self.assertIsNone(manager_git_journal.parse_remote_branch_oid('', ref))
        self.assertEqual(manager_git_journal.parse_remote_branch_oid(oid + '\t' + ref, ref), oid)
        for output in (oid + '\trefs/heads/foreign', (oid + '\t' + ref + '\n') * 2, 'invalid\t' + ref):
            with self.subTest(output=output), self.assertRaises(manager_git_journal.MutationEvidenceError):
                manager_git_journal.parse_remote_branch_oid(output, ref)

    def test_shared_lease_keeps_absence_separate_from_an_exact_oid(self):
        self.assertEqual(manager_git_journal.exact_branch_lease('owned', None), '--force-with-lease=refs/heads/owned:')
        self.assertEqual(manager_git_journal.exact_branch_lease('owned', 'a' * 40), '--force-with-lease=refs/heads/owned:' + 'a' * 40)

    def test_pr_open_observation_does_not_grant_validation_or_rebase(self):
        effects = fake_effects(pull_request_recovery.ReconcileExistingPullRequestEffects)
        effects.project_execplan.return_value = None
        project = dict(branch='owned', pr_url='url', pr_head_oid='head', phase='PR_OPEN',
                       pr_conflict_recovery=0, base_branch='main')
        for mergeability in ('unknown', 'mergeable'):
            with self.subTest(mergeability=mergeability):
                effects.classify_pull_request_mergeability.return_value = mergeability
                self.assertEqual(pull_request_recovery.reconcile_existing_pull_request(
                    effects, mock.Mock(), Path('/repo'), Path('/state'), 'repo', project,
                    dict(url='url', headRefName='owned', headRefOid='head', state='OPEN')), 0)
        effects.update_project.assert_not_called()
        effects._validate_pr_conflict_recovery_ownership.assert_not_called()
        effects._fetch_pr_conflict_recovery_refs.assert_not_called()
        effects.make_worktree.assert_not_called()

    def test_checkpoint_and_archive_factories_inject_role_specific_effects(self):
        checkpoint = loop_manager._BlockedCheckpointAdapter(mock.Mock(), Path('/repo'), Path('/state'), 'repo')
        archive = loop_manager._ExecPlanArchiveGitPort(Path('/repo'), Path('/state'), 'repo')
        self.assertEqual(type(checkpoint).__module__, 'loop_manager_internal.recovery.checkpoint_adapter')
        self.assertEqual(type(archive).__module__, 'loop_manager_internal.catalog.archive_git')
        callbacks = (checkpoint.safety_effects.git, checkpoint.publication_effects.git, archive.effects.git)
        with mock.patch.object(loop_manager, 'git', return_value='observed') as git:
            for callback in callbacks:
                self.assertEqual(callback(Path('/repo'), 'rev-parse', 'HEAD', check=False), 'observed')
            self.assertEqual(git.call_args_list, [mock.call(Path('/repo'), 'rev-parse', 'HEAD', check=False)] * 3)
        self.assertFalse(hasattr(archive.effects, 'resume_project_block_if_safe'))
