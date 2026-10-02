"""Unit checks for effect boundaries; Git and GitHub are always fake ports."""
from dataclasses import fields, replace
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
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
from tests.support.database import runtime_connection


def fake_effects(contract):
    return contract(**{field.name: mock.Mock(name=field.name) for field in fields(contract)})


class CheckpointRecoveryBoundaryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.snapshot = loop_manager.GitSnapshot('published', 'loop-manager/G-047', {}, {}, {}, {}, {}, {})
        self.connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED', branch = 'loop-manager/G-047', "
            "worktree = '/state/G-047', blocked_reason = 'verify_retry_exhausted'")
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

    def test_guidance_double_block_restores_reason_and_request_identity(self):
        reason, evidence = 'managed_worktree_scope_violation', 'guidance wait'
        branch, worktree = self.snapshot.branch, '/state/G-047'
        self.connection.execute(
            "UPDATE exec_plans SET blocked_reason = ?, guidance_plan_hash = 'guidance-hash', "
            "publication_remote_oid = 'published', worker_session_id = 'session', "
            "agent_role = 'worker', agent_snapshot = ?", (reason, self.snapshot.serialize()))
        loop_manager.hydrate_execplan_project(self.connection, 'repo', self.plan())
        store = loop_manager.blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        store.request(repo='repo', plan_id='G-047', reason_code=reason, evidence='scope error',
            branch=branch, worktree=worktree, expected_remote_oid='published', rebase_recovery=False)
        block_reason = loop_manager.BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION
        loop_manager.scoped_blocking.record_plan_block(self.connection, 'repo', 'G-047', block_reason, 'scope error', None)
        loop_manager.scoped_blocking.record_project_block(self.connection, 'repo',
            loop_manager.BlockReasonCode.REPOSITORY_STATE_UNSAFE, 'scope error',
            'fingerprint', plan_id='G-047', previous_phase='PLAN_BLOCKED')
        guidance = dict(applied=False, invalidated_decision_hash='', decision_hash='rejected-decision',
            proposal_id='guidance', target_path='AGENTS.md', baseline_content=b'# Agents\n', baseline_mode=0o644)
        proposal = SimpleNamespace(decision=SimpleNamespace(identity='rejected-decision'))
        replacements = dict(
            validate_execplan_workspace_identity=None, validate_managed_worktree=None,
            capture_git_snapshot=self.snapshot, _owned_checkpoint_remote_oid='published',
            _guidance_checkpoint_commit_evidence=evidence,
            _guidance_document=SimpleNamespace(structure_hash='guidance-hash'), _guidance_rows=[guidance],
            _guidance_document_matches_rows=({'guidance': proposal}, ()),
            _guidance_target_state=(b'# Agents\n', 0o644), _proven_merged_cleanup_paths=frozenset(),
            _proven_manager_base_refs=frozenset(), operation_log=None)
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(mock.patch.object(loop_manager, name, return_value=value))
                     for name, value in replacements.items()}
            stack.enter_context(mock.patch.object(loop_manager, 'git', side_effect=[
                SimpleNamespace(stdout='Checkpoint-Identity: commit-identity\n'), SimpleNamespace(stdout='tree')]))
            def resume():
                return loop_manager._reconcile_guidance_double_block(
                    self.connection, Path('/repo'), Path('/state'), 'repo',
                    loop_manager.scoped_blocking.project_block(self.connection, 'repo'),
                    self.connection.execute('SELECT * FROM projects').fetchone(), self.plan())
            mocks['_guidance_target_state'].return_value = (b'tampered', 0o644)
            self.assertFalse(resume())
            self.assertIsNotNone(loop_manager.scoped_blocking.project_block(self.connection, 'repo'))
            mocks['_guidance_target_state'].return_value = (b'# Agents\n', 0o644)
            self.assertTrue(resume())
        restored = self.connection.execute('SELECT * FROM blocked_checkpoints').fetchone()
        expected_identity = loop_manager.blocked_checkpoint_state.request_identity(
            'repo', 'G-047', loop_manager.GUIDANCE_BLOCK_REASON, evidence, branch, worktree, 'guidance-hash')
        self.assertEqual(restored['request_identity'], expected_identity)
        self.assertEqual(restored['reason_code'], loop_manager.GUIDANCE_BLOCK_REASON)
        self.assertEqual(restored['phase'], 'published')
        self.assertEqual(restored['accepted_remote_oid'], 'published')
        self.assertEqual(self.plan()['blocked_reason'], loop_manager.GUIDANCE_BLOCK_REASON)
        self.assertIsNone(loop_manager.scoped_blocking.project_block(self.connection, 'repo'))
        self.assertEqual(guidance['decision_hash'], 'rejected-decision')


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
