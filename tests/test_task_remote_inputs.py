"""현재 작업 입력, 삭제 재시작 및 파일 단위 복구의 실제 Git 효과."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock
import json
import subprocess

import loop_manager as lm
from tests.support.blocked_git import blocked_git_fixture
from loop_manager_internal.git import rebase_documents
from loop_manager_internal.recovery import work_branch_integration, deleted_branch_restart, scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository


class TaskRemoteInputTests(TestCase):
    def setUp(self):
        self.fx = blocked_git_fixture(self)
        self.connection = self.fx.connection
        self.repo = str(self.fx.repo.resolve())
        self.store = ExecutionCheckpointRepository(self.connection)
        self.effects = lm._checkpoint_recovery_effects_reconcile_published_blocked_checkpoint()
        RuntimeStateRepository(self.connection).hydrate(self.repo, 'G-034')

    def plan(self):
        return self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (self.repo, 'G-034')).fetchone()

    def sync(self, remote, pr=None):
        return work_branch_integration.synchronize(self.effects, self.connection, self.fx.repo,
            self.fx.state_dir, self.repo, self.plan(), remote, pr)

    def push(self):
        self.fx.run_git(self.fx.worktree, 'push', 'origin', 'HEAD:refs/heads/loop-manager/G-034')
        return self.fx.run_git(self.fx.worktree, 'rev-parse', 'HEAD')

    def deleted(self, pr_url=None):
        head = self.push()
        RuntimeStateRepository(self.connection).update_project(self.repo, {'publication_remote_oid': head, 'pr_url': pr_url})
        self.fx.run_git(self.fx.repo, 'push', 'origin', ':refs/heads/loop-manager/G-034')
        scoped_blocking.record_plan_block(self.connection, self.repo, 'G-034',
            BlockReasonCode.REMOTE_BRANCH_DELETED, 'deleted', None)
        self.connection.commit()
        return head

    def restart(self, pr=None, complete=None):
        return deleted_branch_restart.restart(self.effects, self.connection, self.fx.repo,
            self.fx.state_dir, self.repo, 'G-034', lookup_pr=mock.Mock(return_value=pr),
            complete_merged=complete or mock.Mock(), unmet_dependencies=lambda *_: ())

    def test_local_ahead_accepts_current_input_without_publication_receipt(self):
        remote = self.push()
        (self.fx.worktree / 'code.py').write_text('local\n')
        self.fx.run_git(self.fx.worktree, 'add', 'code.py')
        self.fx.run_git(self.fx.worktree, 'commit', '-m', 'local input')
        self.assertFalse(self.sync(remote))
        self.assertEqual(self.store.get(self.repo, 'G-034').snapshot['accepted_work_branch'], remote)
        self.assertIsNone(self.plan()['publication_remote_oid'])
        self.assertEqual((self.fx.worktree / 'code.py').read_text(), 'local\n')

    def test_remote_ahead_preserves_dirty_inputs_before_integrating(self):
        self.push()
        remote = self.fx.user_commit('dependency.py', 'remote dependency\n')
        (self.fx.worktree / 'code.py').write_text('staged\n')
        self.fx.run_git(self.fx.worktree, 'add', 'code.py')
        (self.fx.worktree / 'code.py').write_text('working\n')
        (self.fx.worktree / 'local.bin').write_bytes(b'\0local')
        self.assertTrue(self.sync(remote))
        saved = self.store.get(self.repo, 'G-034').snapshot['work_branch_integration']
        baseline = json.loads(saved['baseline'])
        self.assertEqual(self.fx.run_git(self.fx.worktree, 'show', baseline['index_tree'] + ':code.py'), 'staged')
        self.assertEqual((self.fx.worktree / 'code.py').read_text(), 'working\n')
        self.assertEqual((self.fx.worktree / 'local.bin').read_bytes(), b'\0local')
        self.assertEqual((self.fx.worktree / 'dependency.py').read_text(), 'remote dependency\n')
        self.assertFalse(self.sync(remote))

    def test_same_pr_head_advance_is_input_but_wrong_pr_identity_is_refused(self):
        remote = self.push()
        RuntimeStateRepository(self.connection).update_project(self.repo, {'pr_url': 'https://example/pr/1', 'pr_head_oid': 'old'})
        pr = dict(url='https://example/pr/1', state='OPEN', headRefName='loop-manager/G-034',
                  headRefOid=remote, baseRefName='main')
        self.assertFalse(self.sync(remote, pr))
        self.assertEqual(self.plan()['pr_head_oid'], remote)
        with self.assertRaises(RecoveryFailure) as caught:
            self.sync(remote, {**pr, 'url': 'https://example/pr/2'})
        self.assertIs(caught.exception.reason, RecoveryReason.OWNERSHIP_UNCERTAIN)

    def test_deleted_known_branch_requires_explicit_restart(self):
        head = self.push()
        RuntimeStateRepository(self.connection).update_project(self.repo, {'publication_remote_oid': head})
        self.fx.run_git(self.fx.repo, 'push', 'origin', ':refs/heads/loop-manager/G-034')
        with self.assertRaises(RecoveryFailure) as caught:
            self.sync(None)
        self.assertIs(caught.exception.reason, RecoveryReason.REMOTE_BRANCH_DELETED)

    def test_restart_archives_all_local_states_and_invalidates_old_proofs(self):
        self.deleted()
        (self.fx.worktree / 'code.py').write_text('staged\n')
        self.fx.run_git(self.fx.worktree, 'add', 'code.py')
        (self.fx.worktree / 'code.py').write_text('working\n')
        (self.fx.worktree / 'private.bin').write_bytes(b'\0raw')
        self.restart()
        saved = self.store.get(self.repo, 'G-034').snapshot['deleted_branch_restart']
        archive = Path(saved['archive'])
        self.assertEqual((archive / 'code.py').read_text(), 'working\n')
        self.assertEqual(self.fx.run_git(archive, 'show', ':code.py'), 'staged')
        self.assertEqual((archive / 'private.bin').read_bytes(), b'\0raw')
        self.assertEqual(self.fx.run_git(self.fx.worktree, 'rev-parse', 'HEAD'), self.fx.base_oid)
        self.assertEqual(self.plan()['status'], 'READY')
        self.assertIsNone(self.plan()['publication_remote_oid'])
        self.assertIsNone(self.plan()['worker_result'])
        self.assertIsNone(scoped_blocking.plan_block(self.connection, self.repo, 'G-034'))

    def test_restart_requires_current_live_remote_observation(self):
        self.deleted()
        before = lm.capture_git_snapshot(self.fx.worktree)
        with mock.patch('loop_manager_internal.git.manager_git_journal.live_remote_branch_oid',
                        side_effect=RuntimeError('remote observation unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'remote observation unavailable'):
                self.restart()
        self.assertEqual(lm.capture_git_snapshot(self.fx.worktree), before)
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')

    def test_interrupted_restart_resumes_the_same_archive(self):
        self.deleted()
        original = self.effects.git
        failed = False
        def git(location, *args, **kwargs):
            nonlocal failed
            if args[:2] == ('worktree', 'add') and not failed:
                failed = True
                raise RuntimeError('interrupted')
            return original(location, *args, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            deleted_branch_restart.restart(replace(self.effects, git=git), self.connection,
                self.fx.repo, self.fx.state_dir, self.repo, 'G-034', lookup_pr=lambda *_: None,
                complete_merged=mock.Mock(), unmet_dependencies=lambda *_: ())
        archive = self.store.get(self.repo, 'G-034').snapshot['deleted_branch_restart']['archive']
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')
        self.restart()
        self.assertEqual(self.store.get(self.repo, 'G-034').snapshot['deleted_branch_restart']['archive'], archive)
        self.assertEqual(self.plan()['status'], 'READY')

    def test_recreated_branch_does_not_automatically_resume_a_deleted_task(self):
        self.deleted()
        self.push()
        self.assertFalse(lm.resume_blocked_execplan_if_inputs_changed(self.connection,
            self.fx.repo, self.repo, 'main', self.fx.state_dir, plan_id='G-034'))
        self.assertEqual(self.plan()['status'], 'PLAN_BLOCKED')

    def test_actual_merge_before_restart_completes_without_archiving(self):
        self.deleted('https://example/pr/1')
        complete = mock.Mock()
        self.restart(dict(url='https://example/pr/1', headRefName='loop-manager/G-034',
            baseRefName='main', headRefOid=self.fx.base_oid, state='MERGED'), complete)
        complete.assert_called_once()
        self.assertFalse((self.fx.worktree.parent / 'retired').exists())

    def conflict_context(self):
        for location, value in ((self.fx.worktree, 'local\n'), (self.fx.repo, 'remote\n')):
            (location / 'code.py').write_text(value)
            (location / 'dependency.py').write_text(value)
            self.fx.run_git(location, 'add', 'code.py', 'dependency.py')
            self.fx.run_git(location, 'commit', '-m', value.strip())
        head = self.fx.run_git(self.fx.worktree, 'rev-parse', 'HEAD')
        base = self.fx.run_git(self.fx.repo, 'rev-parse', 'HEAD')
        result = subprocess.run(['git', '-C', str(self.fx.worktree), 'rebase', base], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        (self.fx.worktree / 'kept.bin').write_bytes(b'\0kept')
        self.fx.run_git(self.fx.worktree, 'add', 'kept.bin')
        (self.fx.worktree / 'unstaged.bin').write_bytes(b'\0unstaged')
        before = lm.capture_git_snapshot(self.fx.worktree)
        current = self.store.get(self.repo, 'G-034')
        self.store.record(self.repo, 'G-034', expected_generation=current.generation,
            operation='rebase', expected_head=head, target_base_oid=base, observed_remote_oid=None, snapshot={})
        context = SimpleNamespace(worktree=self.fx.worktree, plan=SimpleNamespace(plan_id='G-034'))
        return context, before

    def resolve_files(self, context, paths, git=lm.git):
        return rebase_documents.resolve(context=context, identity=SimpleNamespace(repo=self.repo,
            repo_path=self.fx.repo, base='main'), checkpoints=self.store, git=git,
            git_bytes=lm.git_bytes, require_owned=mock.Mock(), capture_snapshot=lm.capture_git_snapshot,
            lookup_pr=mock.Mock(), failed_files=paths)

    def test_file_fallback_preserves_other_conflicts_staging_and_untracked_bytes(self):
        context, before = self.conflict_context()
        self.resolve_files(context, ('code.py',))
        after = lm.capture_git_snapshot(self.fx.worktree)
        self.assertEqual((self.fx.worktree / 'code.py').read_text(), 'remote\n')
        self.assertEqual(rebase_documents.workspace_evidence(before, ('code.py',)),
                         rebase_documents.workspace_evidence(after, ('code.py',)))
        self.assertTrue(any(' 2' in entry for entry in after.index['dependency.py']))

    def test_file_fallback_interruption_preserves_new_correction_and_finishes_remaining_file(self):
        context, before = self.conflict_context()
        def interrupted(location, *args, **kwargs):
            if 'update-index' in args:
                raise RuntimeError('interrupted')
            return lm.git(location, *args, **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.resolve_files(context, ('code.py', 'dependency.py'), interrupted)
        revision = self.store.get(self.repo, 'G-034').snapshot['rebase_files']['revision']
        (self.fx.worktree / 'code.py').write_text('new user correction\n')
        self.fx.run_git(self.fx.worktree, 'add', 'code.py')
        self.resolve_files(context, ('code.py', 'dependency.py'))
        self.assertEqual((self.fx.worktree / 'code.py').read_text(), 'new user correction\n')
        self.assertEqual(self.fx.run_git(self.fx.worktree, 'show', ':code.py'), 'new user correction')
        self.assertEqual((self.fx.worktree / 'dependency.py').read_text(), 'remote\n')
        saved = self.store.get(self.repo, 'G-034').snapshot['rebase_files']
        self.assertEqual(saved['revision'], revision)
        self.assertEqual(saved['skipped_user_changes'], ['code.py'])
        self.assertEqual(rebase_documents.workspace_evidence(before, ('code.py', 'dependency.py')),
                         rebase_documents.workspace_evidence(lm.capture_git_snapshot(self.fx.worktree), ('code.py', 'dependency.py')))

    def test_observed_oid_without_integration_does_not_grant_overwrite(self):
        own = self.push()
        RuntimeStateRepository(self.connection).update_project(self.repo, {'publication_remote_oid': own})
        remote = self.fx.user_commit('dependency.py', 'incoming\n')
        current = self.store.get(self.repo, 'G-034')
        self.store.record(self.repo, 'G-034', expected_generation=current.generation, operation=None,
            expected_head=own, target_base_oid=self.fx.base_oid, observed_remote_oid=remote,
            snapshot={'remote_oid': remote})
        self.assertTrue(self.sync(remote))
        self.assertEqual((self.fx.worktree / 'dependency.py').read_text(), 'incoming\n')
        self.assertEqual(self.fx.run_git(self.fx.worktree, 'rev-parse', 'HEAD'), remote)
