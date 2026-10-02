"""Unit checks for effect boundaries; Git and GitHub are always fake ports."""
from dataclasses import fields, replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.catalog import archive_git, execplan_archive
from loop_manager_internal.git import command_adapter, manager_git_journal, source_sync, workspace
from loop_manager_internal.recovery import pull_request_recovery
from loop_manager_internal.runtime.contracts import BaseWorktreeSnapshot, WorkspaceOwnershipError
from loop_manager_internal.storage.execplan_archive_state import MergedArchiveCandidate


def fake_effects(contract):
    return contract(**{field.name: mock.Mock(name=field.name) for field in fields(contract)})


class WorkspaceEffectBoundaryTests(unittest.TestCase):
    def test_rebase_ignored_overlap_refuses_before_subprocess(self):
        effects = fake_effects(command_adapter.GitEffects)
        effects._ignored_paths_overlapping_git_update.return_value = ['cache/file']
        result = command_adapter.git(effects, Path('/repo'), 'rebase', 'base', check=False)
        self.assertEqual(result.returncode, 1)
        effects.command.assert_not_called()

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
        with mock.patch.object(loop_manager, 'git') as git:
            self.assertIs(checkpoint.safety_effects.git, git)
            self.assertIs(checkpoint.publication_effects.git, git)
            self.assertIs(archive.effects.git, git)
        self.assertFalse(hasattr(archive.effects, 'resume_project_block_if_safe'))

    def test_extracted_boundaries_import_without_the_root_module(self):
        modules = ('git.command_adapter', 'git.github_observation', 'git.workspace', 'git.source_sync',
                   'git.managed_rebase_runtime', 'recovery.checkpoint_adapter', 'catalog.archive_git',
                   'recovery.pull_request_recovery', 'recovery.scope_reconciliation', 'recovery.checkpoint_recovery')
        for module in modules:
            with self.subTest(module=module):
                result = subprocess.run([
                    sys.executable, '-I', '-B', '-c',
                    "import importlib, sys; sys.path.insert(0, sys.argv[1]); "
                    "importlib.import_module('loop_manager_internal.' + sys.argv[2]); "
                    "assert 'loop_manager' not in sys.modules",
                    str(loop_manager.ROOT), module], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
