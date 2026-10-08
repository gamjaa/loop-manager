import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
import loop_manager
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import work_branch_integration
from loop_manager_internal.execution.worker_session import SessionBinding
from loop_manager_internal.execution.execution_checkpoint import controlling_git_operation


from tests.support.blocked_git import CheckpointGitRecoveryFixture


class ProjectCheckpointGitRecoveryTests(CheckpointGitRecoveryFixture, unittest.TestCase):
    def test_project_block_rebases_and_publishes_diagnostic_without_original_ownership(self):
        repo, original, coordinator = self._project_block_fixture()
        source = self.run_git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(coordinator.reconcile('G-034'), 'blocked')
        result = json.loads(self.connection.execute('SELECT payload FROM project_checkpoint_results').fetchone()[0])
        self.assertEqual(result['result'], 'published', result)
        self.assertTrue(result['diagnostic'])
        head = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        self.assertEqual(self.run_git(self.remote, 'rev-parse', f"refs/heads/{result['branch']}"), head)
        self.assertEqual(self.run_git(self.remote, 'rev-parse', 'refs/heads/loop-manager/G-034'), original)
        self.run_git(self.worktree, 'merge-base', '--is-ancestor', source, head)
        self.assertEqual(self.run_git(self.repo, 'rev-parse', 'HEAD'), source)
        self.assertIsNone(self.connection.execute('SELECT publication_remote_oid FROM exec_plans').fetchone()[0])
        with mock.patch.object(loop_manager, 'push_diagnostic_checkpoint', side_effect=AssertionError('duplicate push')):
            self.assertEqual(coordinator.reconcile('G-034'), 'blocked')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM blocked_diagnostic_publications').fetchone()[0], 1)
        self.assertEqual(scoped_blocking.project_block(self.connection, repo)['evidence'], 'preserved project cause')


    def test_project_diagnostic_push_sqlite_interruption_confirms_without_repush(self):
        _, _, coordinator = self._project_block_fixture()
        with mock.patch.object(blocked_checkpoint_state.DiagnosticPublicationRepository, 'result', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                coordinator.reconcile('G-034')
        intent = self.connection.execute('SELECT * FROM blocked_diagnostic_publications').fetchone()
        self.assertEqual(self.run_git(self.remote, 'rev-parse', f"refs/heads/{intent['ref']}"), intent['head_oid'])
        with mock.patch.object(loop_manager, 'push_diagnostic_checkpoint', side_effect=AssertionError('duplicate push')):
            self.assertEqual(coordinator.reconcile('G-034'), 'blocked')
        self.assertEqual(self.connection.execute('SELECT confirmed_oid FROM blocked_diagnostic_publications').fetchone()[0], intent['head_oid'])


    def test_project_diagnostic_collision_preserves_both_refs(self):
        _, original, coordinator = self._project_block_fixture()
        with mock.patch.object(loop_manager, 'push_diagnostic_checkpoint', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                coordinator.reconcile('G-034')
        intent = self.connection.execute('SELECT * FROM blocked_diagnostic_publications').fetchone()
        self.run_git(self.repo, 'push', 'origin', f"{original}:refs/heads/{intent['ref']}")
        self.assertEqual(coordinator.reconcile('G-034'), 'blocked')
        result = json.loads(self.connection.execute('SELECT payload FROM project_checkpoint_results').fetchone()[0])
        self.assertEqual(result['result'], 'local_only')
        self.assertIn('다른 OID', result['reason'])
        self.assertEqual(self.run_git(self.remote, 'rev-parse', f"refs/heads/{intent['ref']}"), original)


    def test_project_successor_fetch_ignores_configured_shared_ref_mapping(self):
        repo, original, coordinator = self._project_block_fixture(known=True, advance=False)
        successor = self.user_commit()
        self.run_git(self.repo, 'update-ref', 'refs/heads/unowned', original)
        self.run_git(self.repo, 'config', '--replace-all', 'remote.origin.fetch',
                     '+refs/heads/loop-manager/G-034:refs/heads/unowned')
        before = self.run_git(self.repo, 'for-each-ref', '--format=%(refname) %(objectname)')
        loop_manager.observe_blocked_plan_remotes(
            self.connection, self.repo, repo,
            self.connection.execute('SELECT * FROM projects').fetchone())
        # Stop at the adoption boundary so subsequent publication is unrelated
        # to this fetch regression; real Git effects above run only in verify.
        with mock.patch.object(coordinator, '_incorporate', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                coordinator.reconcile('G-034')
        self.assertEqual(self.run_git(self.worktree, 'rev-parse', 'HEAD'), successor)
        after = self.run_git(self.repo, 'for-each-ref', '--format=%(refname) %(objectname)')
        own_prefix = 'refs/heads/loop-manager/G-034 '
        self.assertEqual([line for line in before.splitlines() if not line.startswith(own_prefix)],
                         [line for line in after.splitlines() if not line.startswith(own_prefix)])
        self.assertEqual(self.run_git(self.repo, 'rev-parse', 'refs/heads/unowned'), original)


    def test_project_known_lineage_refreshes_original_branch_with_exact_lease(self):
        repo, original, coordinator = self._project_block_fixture(known=True)
        self.assertEqual(coordinator.reconcile('G-034'), 'recovered')
        result = json.loads(self.connection.execute('SELECT payload FROM project_checkpoint_results').fetchone()[0])
        self.assertEqual(result['result'], 'published', result)
        self.assertNotIn('diagnostic', result)
        head = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(original, head)
        self.assertEqual(self.run_git(self.remote, 'rev-parse', 'refs/heads/loop-manager/G-034'), head)
        self.assertIsNone(scoped_blocking.project_block(self.connection, repo))
        self.assertEqual(scoped_blocking.plan_block(self.connection, repo, 'G-034')['reason_code'], 'verify_retry_exhausted')


    def test_project_rebase_conflict_preserves_original_index_and_publishes_artifacts(self):
        repo, original, coordinator = self._project_block_fixture(advance=False)
        # Advance the same path so the owned rebase conflicts after preserving
        # the local artifact in a checkpoint commit.
        (self.repo / 'code.py').write_text('conflicting upstream\n', encoding='utf-8')
        self.run_git(self.repo, 'add', 'code.py')
        self.run_git(self.repo, 'commit', '-m', 'Conflicting base')
        self.run_git(self.repo, 'push', 'origin', 'main')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo,
                                                self.connection.execute('SELECT * FROM projects').fetchone())
        preserve = loop_manager.preserve_blocked_diagnostics
        originals = []
        def observe_preservation(*args, **kwargs):
            before = loop_manager.capture_git_snapshot(self.worktree).serialize()
            originals.append(before)
            result = preserve(*args, **kwargs)
            self.assertEqual(loop_manager.capture_git_snapshot(self.worktree).serialize(), before)
            return result
        with mock.patch.object(loop_manager, 'preserve_blocked_diagnostics', side_effect=observe_preservation):
            self.assertEqual(coordinator.reconcile('G-034'), 'blocked')
        result = json.loads(self.connection.execute('SELECT payload FROM project_checkpoint_results').fetchone()[0])
        self.assertEqual(result['result'], 'local_only')
        self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))
        self.assertEqual(self.run_git(self.remote, 'rev-parse', 'refs/heads/loop-manager/G-034'), original)
        self.assertEqual(len(originals), 1)
        receipt = json.loads(self.connection.execute(
            "SELECT payload FROM diagnostic_preservations WHERE scope = 'project' "
            "ORDER BY observed_order DESC LIMIT 1").fetchone()[0])
        self.assertEqual(receipt['result'], 'published', receipt)
        self.assertEqual(self.run_git(self.remote, 'rev-parse', f"refs/heads/{receipt['branch']}"), receipt['commit_oid'])
        manifest = json.loads(self.run_git(self.worktree, 'show', f"{receipt['commit_oid']}:manifest.json"))
        self.assertIn('code.py', manifest['files'], manifest)
        self.assertEqual(set(manifest['files']['code.py']['index']), {'1', '2', '3'})
        manual = manifest['files']['code.py']['worktree']['blob']
        preserved = subprocess.run(
            ['git', '-C', str(self.worktree), 'show', f"{receipt['commit_oid']}:blobs/{manual}"],
            check=True, stdout=subprocess.PIPE).stdout
        self.assertEqual(preserved, (self.worktree / 'code.py').read_bytes())
        with mock.patch.object(loop_manager, 'push_diagnostic_checkpoint', side_effect=AssertionError('duplicate push')):
            self.assertEqual(coordinator.reconcile('G-034'), 'blocked')
        self.assertEqual(scoped_blocking.project_block(self.connection, repo)['evidence'], 'preserved project cause')


class CheckpointRebaseGitRecoveryTests(CheckpointGitRecoveryFixture, unittest.TestCase):
    def test_dirty_attributes_preserve_raw_bytes_and_integrate_normalized_blobs(self):
        repo, _ = self._remote_refresh_fixture(branch_changed=True)
        raw = {'crlf.txt': b'working\r\n', 'encoded.txt': b'\xff\xfe' + 'working\n'.encode('utf-16-le')}
        (self.worktree / '.gitattributes').write_text(
            'crlf.txt text eol=crlf\nencoded.txt text working-tree-encoding=UTF-16LE-BOM eol=lf\n',
            encoding='utf-8', newline='\n')
        for path, data in raw.items():
            (self.worktree / path).write_bytes(data)
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                               return_value=(False, 'same cause', None)):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(repo, 'G-034')
        intent = saved.snapshot['work_branch_integration']
        baseline = json.loads(intent['baseline'])
        self.assertEqual(intent['phase'], 'complete')
        for path, data in raw.items():
            self.assertEqual((self.worktree / path).read_bytes(), data)
            preserved = subprocess.run(
                ['git', '-C', str(self.worktree), 'show', f"{baseline['worktree_tree']}:{path}"],
                check=True, stdout=subprocess.PIPE).stdout
            self.assertEqual(preserved, data)
            normalized = subprocess.run(
                ['git', '-C', str(self.worktree), 'show', f'HEAD:{path}'],
                check=True, stdout=subprocess.PIPE).stdout
            self.assertEqual(normalized, b'working\n')
        self.assertIsNone(saved.operation)
        self.assertFalse(loop_manager.capture_git_snapshot(self.worktree).dirty)

    def test_dirty_preservation_commit_does_not_require_user_identity(self):
        repo, _ = self._remote_refresh_fixture(branch_changed=True)
        (self.worktree / 'local.txt').write_text('local bytes\n')
        self.run_git(self.worktree, 'config', '--unset', 'user.name')
        self.run_git(self.worktree, 'config', '--unset', 'user.email')
        self.run_git(self.worktree, 'config', 'user.useConfigOnly', 'true')
        # Stop after preserving local input; rebase identity policy is a
        # separate existing boundary from this commit-tree regression.
        with mock.patch.object(loop_manager, '_journaled_direct_rebase', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(repo, 'G-034')
        intent = saved.snapshot['work_branch_integration']
        self.assertEqual(intent['phase'], 'rebasing')
        self.assertEqual(self.run_git(self.worktree, 'rev-parse', 'HEAD'), intent['local_commit'])
        self.assertEqual(self.run_git(self.worktree, 'show', '-s', '--format=%an <%ae>;%cn <%ce>', 'HEAD'),
                         'Loop Manager <loop-manager@localhost>;Loop Manager <loop-manager@localhost>')
        self.assertEqual((self.worktree / 'local.txt').read_text(), 'local bytes\n')

    def test_user_push_after_base_conflict_preserves_conflict_and_resumes_after_restart(self):
        repo, _ = self._remote_refresh_fixture(branch_changed=True)
        (self.repo / 'code.py').write_text('base correction\n')
        self.run_git(self.repo, 'add', 'code.py')
        self.run_git(self.repo, 'commit', '-m', 'Conflicting latest base')
        self.run_git(self.repo, 'push', 'origin', 'main')
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))
        self.assertEqual(self.connection.execute(
            'SELECT stage FROM blocked_plan_reconciliations WHERE plan_id = ?', ('G-034',)).fetchone()[0], 'adopted')
        (self.worktree / 'code.py').write_text('manual local conflict bytes\n')
        user = Path(self.temporary.name) / 'user'
        self.run_git(user, 'fetch', 'origin', 'main')
        merge = subprocess.run(['git', '-C', str(user), 'merge', '--no-edit', 'FETCH_HEAD'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(merge.returncode, 1, merge.stdout)
        (user / 'code.py').write_text('resolved remote input\n')
        self.run_git(user, 'add', 'code.py')
        self.run_git(user, 'commit', '-m', 'Resolve base conflict remotely')
        self.run_git(user, 'push', 'origin', 'HEAD:refs/heads/loop-manager/G-034')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                               return_value=(False, 'same cause', None)) as probe:
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        probe.assert_called_once()
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(repo, 'G-034')
        restart = saved.snapshot['base_rebase_restart']
        self.assertEqual(json.loads(restart['baseline'])['version'], 2)
        self.assertEqual(restart['phase'], 'complete')
        self.assertIsNone(saved.operation)
        self.assertFalse(self.run_git(self.worktree, 'ls-files', '-u'))
        self.assertEqual((self.worktree / 'code.py').read_text(), 'resolved remote input\n')
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause') as duplicate_probe:
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        duplicate_probe.assert_not_called()

    def _complete_base_conflict(self, resolved):
        # Message/current-commit markers can survive completion. Continue only
        # a controlling rebase, preserving those markers as recovery evidence.
        for _ in range(3):
            operation = controlling_git_operation(loop_manager.capture_git_snapshot(self.worktree).operations)
            if operation is None:
                break
            self.assertIn(operation, {'rebase-merge', 'rebase-apply'})
            (self.worktree / 'code.py').write_text(resolved)
            self.run_git(self.worktree, 'add', 'code.py')
            result = subprocess.run(
                ['git', '-C', str(self.worktree), '-c', 'core.editor=true', 'rebase', '--continue'],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertIn(result.returncode, (0, 1), result.stdout)
            if result.returncode:
                self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'), result.stdout)
        self.assertIsNone(controlling_git_operation(loop_manager.capture_git_snapshot(self.worktree).operations))

    def _manual_base_completion_then_successor(self, *, after_save=False):
        repo, _ = self._remote_refresh_fixture(branch_changed=True)
        (self.repo / 'code.py').write_text('base correction\n')
        self.run_git(self.repo, 'add', 'code.py')
        self.run_git(self.repo, 'commit', '-m', 'Conflicting latest base')
        self.run_git(self.repo, 'push', 'origin', 'main')
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        before = checkpoints.get(repo, 'G-034')
        self.assertEqual(before.operation, 'rebase')
        audit = tuple(self.connection.execute(
            'SELECT agent_snapshot, agent_result_snapshot, worker_session_id FROM exec_plans '
            'WHERE plan_id = ?', ('G-034',)).fetchone())
        resolved = 'manually completed base input\n'
        # Both the checkpoint and the user's first commit may conflict when
        # replayed. Resolve each remaining step as a user would, with a bound.
        self._complete_base_conflict(resolved)
        completed = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(completed, before.expected_head)
        ancestry = subprocess.run(['git', '-C', str(self.worktree), 'merge-base', '--is-ancestor',
                                   before.observed_remote_oid, completed])
        self.assertEqual(ancestry.returncode, 1)
        self.assertEqual(checkpoints.get(repo, 'G-034'), before)
        user = Path(self.temporary.name) / 'user'
        self.run_git(user, 'fetch', 'origin', 'main')
        merge = subprocess.run(['git', '-C', str(user), 'merge', '--no-edit', 'FETCH_HEAD'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(merge.returncode, 1, merge.stdout)
        (user / 'code.py').write_text(resolved)
        self.run_git(user, 'add', 'code.py')
        self.run_git(user, 'commit', '-m', 'User resolves latest base conflict')
        self.run_git(user, 'push', 'origin', 'HEAD:refs/heads/loop-manager/G-034')
        successor = self.run_git(user, 'rev-parse', 'HEAD')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        if after_save:
            complete = work_branch_integration._complete_owned_rebase
            def interrupted(*args):
                complete(*args)
                raise KeyboardInterrupt
            with mock.patch.object(work_branch_integration, '_complete_owned_rebase', side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main')
            saved = checkpoints.get(repo, 'G-034')
            self.assertIsNone(saved.operation)
            self.assertEqual(saved.expected_head, completed)
            self.assertEqual(saved.snapshot['remote_adoption_intent']['head'], successor)
            successor = self.user_commit('dependency.py', 'later user dependency\n')
            loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        with (mock.patch.object(work_branch_integration, '_abort_preserved_rebase',
                                side_effect=AssertionError('completed base must not be aborted')),
              mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                                return_value=(False, 'same cause', None)) as probe):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        probe.assert_called_once()
        saved = checkpoints.get(repo, 'G-034')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['base_rebase_restart']['head'], completed)
        self.assertEqual(saved.snapshot['base_rebase_restart']['original_head'], before.expected_head)
        self.assertEqual(saved.snapshot['current_rebase_acceptance']['target_base_oid'], before.target_base_oid)
        self.assertEqual(json.loads(saved.snapshot['work_branch_integration']['before'])['head'], completed)
        self.run_git(self.worktree, 'merge-base', '--is-ancestor', successor, 'HEAD')
        self.assertEqual((self.worktree / 'code.py').read_text(), resolved)
        if after_save:
            self.assertEqual((self.worktree / 'dependency.py').read_text(), 'later user dependency\n')
        self.assertEqual(tuple(self.connection.execute(
            'SELECT agent_snapshot, agent_result_snapshot, worker_session_id FROM exec_plans '
            'WHERE plan_id = ?', ('G-034',)).fetchone()), audit)
        with mock.patch.object(loop_manager, '_journaled_direct_rebase',
                               side_effect=AssertionError('duplicate completed base recovery')):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))

    def test_manual_base_completion_then_successor_push_keeps_completed_result(self):
        self._manual_base_completion_then_successor()

    def test_successor_after_manual_base_completion_save_interruption_resumes(self):
        self._manual_base_completion_then_successor(after_save=True)

    def test_both_remotes_and_local_commits_index_worktree_binary_input_survive(self):
        repo, _ = self._remote_refresh_fixture(base_changed=True, branch_changed=True)
        (self.worktree / 'rename.txt').write_text('rename bytes\n')
        (self.worktree / 'remove.txt').write_text('remove bytes\n')
        self.run_git(self.worktree, 'add', 'rename.txt', 'remove.txt')
        self.run_git(self.worktree, 'commit', '-m', 'Local files before rename and delete')
        self.run_git(self.worktree, 'mv', 'rename.txt', 'renamed.txt')
        self.run_git(self.worktree, 'rm', 'remove.txt')
        self.run_git(self.worktree, 'commit', '-m', 'Local rename and delete')
        (self.worktree / 'staged.txt').write_text('staged version\n')
        self.run_git(self.worktree, 'add', 'staged.txt')
        (self.worktree / 'staged.txt').write_text('working version\n')
        (self.worktree / 'untracked.bin').write_bytes(b'\0\xff\x01local binary')
        (self.repo / 'source-only.txt').write_text('default checkout input\n')
        self.run_git(self.repo, 'add', 'source-only.txt')
        self.run_git(self.repo, 'branch', 'loop-manager/user-similar-name')
        source_head = self.run_git(self.repo, 'rev-parse', 'HEAD')
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                               return_value=(False, 'same cause', None)):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertEqual((self.worktree / 'code.py').read_text(), 'corrected\n')
        self.assertEqual((self.worktree / 'dependency.py').read_text(), 'dependency = True\n')
        self.assertEqual((self.worktree / 'renamed.txt').read_text(), 'rename bytes\n')
        self.assertFalse((self.worktree / 'rename.txt').exists())
        self.assertFalse((self.worktree / 'remove.txt').exists())
        self.assertEqual((self.worktree / 'staged.txt').read_text(), 'working version\n')
        self.assertEqual((self.worktree / 'untracked.bin').read_bytes(), b'\0\xff\x01local binary')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(repo, 'G-034')
        baseline = json.loads(saved.snapshot['work_branch_integration']['baseline'])
        self.assertEqual(self.run_git(self.worktree, 'show', baseline['index_tree'] + ':staged.txt'), 'staged version')
        self.assertEqual(self.run_git(self.repo, 'rev-parse', 'HEAD'), source_head)
        self.assertEqual(self.run_git(self.repo, 'rev-parse', 'refs/heads/loop-manager/user-similar-name'), source_head)
        self.assertEqual((self.repo / 'source-only.txt').read_text(), 'default checkout input\n')
        self.assertFalse((self.worktree / 'source-only.txt').exists())
        with mock.patch.object(loop_manager, '_journaled_direct_rebase', side_effect=AssertionError('duplicate rebase')):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))

    def test_user_push_during_work_branch_conflict_restarts_from_preserved_local_input(self):
        repo, _ = self._remote_refresh_fixture(branch_changed=True)
        (self.worktree / 'code.py').write_text('local resolution\n')
        self.run_git(self.worktree, 'add', 'code.py')
        self.run_git(self.worktree, 'commit', '-m', 'Local conflicting input')
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))
        self.user_commit('code.py', 'local resolution\n')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo,
            self.connection.execute('SELECT * FROM projects').fetchone())
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                               return_value=(False, 'same cause', None)):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertFalse(self.run_git(self.worktree, 'ls-files', '-u'))
        self.assertEqual((self.worktree / 'code.py').read_text(), 'local resolution\n')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(repo, 'G-034')
        restart = saved.snapshot['work_branch_integration_history'][0]['restart']
        self.assertEqual(json.loads(restart['baseline'])['version'], 2)
        self.assertIsNone(saved.operation)

    def test_manual_work_branch_completion_then_successor_push_keeps_completed_result(self):
        repo, _ = self._remote_refresh_fixture(branch_changed=True)
        (self.worktree / 'code.py').write_text('local resolution\n')
        self.run_git(self.worktree, 'add', 'code.py')
        self.run_git(self.worktree, 'commit', '-m', 'Local conflicting input')
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        original = checkpoints.get(repo, 'G-034').snapshot['work_branch_integration']
        audit = tuple(self.connection.execute(
            'SELECT agent_snapshot, agent_result_snapshot, worker_session_id FROM exec_plans '
            'WHERE plan_id = ?', ('G-034',)).fetchone())
        (self.worktree / 'code.py').write_text('local resolution\n')
        self.run_git(self.worktree, 'add', 'code.py')
        self.run_git(self.worktree, '-c', 'core.editor=true', 'rebase', '--continue')
        completed = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        self.assertEqual(checkpoints.get(repo, 'G-034').operation, 'rebase')
        successor = self.user_commit('dependency.py', 'new user dependency\n')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo,
            self.connection.execute('SELECT * FROM projects').fetchone())
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                               return_value=(False, 'same cause', None)) as probe, \
             mock.patch('loop_manager_internal.recovery.work_branch_integration._abort_preserved_rebase',
                        side_effect=AssertionError('completed rebase must not be aborted')):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        probe.assert_called_once()
        saved = checkpoints.get(repo, 'G-034')
        self.assertEqual(saved.snapshot['work_branch_integration_history'][0],
                         {**original, 'phase': 'complete', 'head': completed})
        self.assertEqual(json.loads(saved.snapshot['work_branch_integration']['before'])['head'], completed)
        self.assertIsNone(saved.operation)
        self.run_git(self.worktree, 'merge-base', '--is-ancestor', successor, 'HEAD')
        self.assertEqual((self.worktree / 'code.py').read_text(), 'local resolution\n')
        self.assertEqual((self.worktree / 'dependency.py').read_text(), 'new user dependency\n')
        self.assertEqual(tuple(self.connection.execute(
            'SELECT agent_snapshot, agent_result_snapshot, worker_session_id FROM exec_plans '
            'WHERE plan_id = ?', ('G-034',)).fetchone()), audit)
        with mock.patch.object(loop_manager, '_journaled_direct_rebase',
                               side_effect=AssertionError('duplicate integration')):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))

    def test_remote_refresh_base_only_with_same_specification(self):
        self._assert_remote_refresh(base_changed=True, branch_changed=False, resolved=False)

    def _user_document_conflict_then_successor(self, path):
        repo, _ = self._remote_refresh_fixture()
        remote = self.user_commit(path, 'remote user document\n')
        target = self.worktree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('local user document\n')
        self.run_git(self.worktree, 'add', '--', path)
        self.run_git(self.worktree, 'commit', '-m', 'Local user document input')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo,
            self.connection.execute('SELECT * FROM projects').fetchone())
        observed = self.connection.execute(
            'SELECT branch_oid FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
            (repo, 'G-034')).fetchone()
        self.assertEqual(observed['branch_oid'], remote)
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertIn(path, self.run_git(self.worktree, 'ls-files', '-u'))
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        pending = checkpoints.get(repo, 'G-034')
        self.assertEqual(pending.snapshot['work_branch_integration']['phase'], 'rebasing')
        self.assertEqual(pending.operation, 'rebase')
        self.user_commit(path, 'local user document\n')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo,
            self.connection.execute('SELECT * FROM projects').fetchone())
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                               return_value=(False, 'original cause still present', None)):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertFalse(self.run_git(self.worktree, 'ls-files', '-u'))
        self.assertEqual(target.read_text(), 'local user document\n')
        current = checkpoints.get(repo, 'G-034')
        self.assertIsNone(current.operation)
        restart = current.snapshot['work_branch_integration_history'][0]['restart']
        raw = json.loads(restart['baseline'])
        self.assertEqual(raw['version'], 2)
        self.assertEqual(set(raw['stage_trees']), {'0', '1', '2', '3'})
        self.assertIn('<<<<<<<', self.run_git(self.worktree, 'show', raw['worktree_tree'] + ':' + path))
        with mock.patch.object(loop_manager, '_journaled_direct_rebase',
                               side_effect=AssertionError('completed integration repeated')):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))

    def test_user_guidance_document_conflict_accepts_resolution_push_after_restart(self):
        self._user_document_conflict_then_successor('AGENTS.md')

    def test_user_architecture_document_conflict_accepts_resolution_push_after_restart(self):
        self._user_document_conflict_then_successor('docs/ARCHITECTURE.md')

    def _restart_successor_after_interruption(self, *, after_abort):
        repo, _ = self._remote_refresh_fixture(branch_changed=True)
        (self.worktree / 'code.py').write_text('local resolution\n')
        self.run_git(self.worktree, 'add', 'code.py')
        self.run_git(self.worktree, 'commit', '-m', 'Local conflicting input')
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))
        second = self.user_commit('code.py', 'local resolution\n')
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        git = loop_manager.git
        def interrupted(root, *args, **kwargs):
            if args[-2:] == ('rebase', '--abort'):
                if after_abort:
                    git(root, *args, **kwargs)
                raise KeyboardInterrupt
            return git(root, *args, **kwargs)
        with mock.patch.object(loop_manager, 'git', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main')
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        pending = checkpoints.get(repo, 'G-034')
        original = pending.snapshot['work_branch_integration']['restart']
        self.assertEqual(original['remote'], second)
        self.assertEqual(pending.snapshot['remote_adoption_intent']['head'], second)
        third = self.user_commit('dependency.py', 'third user input\n')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        with (mock.patch.object(loop_manager, 'git', side_effect=git) as calls,
              mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                                return_value=(False, 'original cause still present', None))):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        self.assertEqual(sum(call.args[-2:] == ('rebase', '--abort')
                             for call in calls.call_args_list), int(not after_abort))
        current = checkpoints.get(repo, 'G-034')
        history = current.snapshot['work_branch_integration_history']
        self.assertIn(original, [item.get('restart') for item in history])
        retargeted = next(item['restart'] for item in history
                          if (item.get('restart') or {}).get('remote') == third)
        for field in ('baseline', 'snapshot', 'original_head'):
            self.assertEqual(retargeted[field], original[field])
        self.assertEqual(current.snapshot['work_branch_integration']['remote'], third)
        self.assertEqual(current.snapshot['work_branch_integration']['phase'], 'complete')
        self.assertIsNone(current.operation)
        self.assertFalse(self.run_git(self.worktree, 'ls-files', '-u'))
        self.assertEqual((self.worktree / 'code.py').read_text(), 'local resolution\n')
        self.assertEqual((self.worktree / 'dependency.py').read_text(), 'third user input\n')
        raw = json.loads(original['baseline'])
        self.assertEqual(raw['version'], 2)
        self.assertIn('<<<<<<<', self.run_git(self.worktree, 'show', raw['worktree_tree'] + ':code.py'))
        with mock.patch.object(loop_manager, '_journaled_direct_rebase',
                               side_effect=AssertionError('completed integration repeated')):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))

    def test_successor_after_conflict_restart_save_resumes_preserved_input(self):
        self._restart_successor_after_interruption(after_abort=False)

    def test_successor_after_conflict_abort_resumes_without_repeating_abort(self):
        self._restart_successor_after_interruption(after_abort=True)

    def _pending_integration_successor(self, *, after_ref):
        repo, _ = self._remote_refresh_fixture(branch_changed=True)
        (self.worktree / 'staged.txt').write_text('original index\n')
        self.run_git(self.worktree, 'add', 'staged.txt')
        (self.worktree / 'staged.txt').write_text('original working bytes\n')
        session = self.connection.execute('SELECT worker_session_id FROM exec_plans').fetchone()[0]
        git = loop_manager.git
        def interrupted_git(root, *args, **kwargs):
            result = git(root, *args, **kwargs)
            if 'update-ref' in args and 'refs/heads/loop-manager/G-034' in args:
                raise KeyboardInterrupt
            return result
        patch = (mock.patch.object(loop_manager, 'git', side_effect=interrupted_git) if after_ref else
                 mock.patch.object(loop_manager, '_journaled_direct_rebase', side_effect=KeyboardInterrupt))
        with patch:
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main')
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        pending = checkpoints.get(repo, 'G-034')
        original = pending.snapshot['work_branch_integration']
        self.assertIsNone(pending.operation)
        self.assertEqual(original['phase'], 'preserving' if after_ref else 'rebasing')
        successor = self.user_commit('dependency.py', 'user successor dependency\n')
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo,
            self.connection.execute('SELECT * FROM projects').fetchone())
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                               return_value=(False, 'original cause still present', None)):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))
        current = checkpoints.get(repo, 'G-034')
        completed = current.snapshot['work_branch_integration']
        self.assertEqual(completed['phase'], 'complete')
        self.assertEqual(completed['remote'], successor)
        for key in ('baseline', 'before', 'local_commit', 'local_tree'):
            self.assertEqual(completed[key], original[key])
        raw = json.loads(original['baseline'])
        self.assertEqual(self.run_git(self.worktree, 'show', raw['index_tree'] + ':staged.txt'), 'original index')
        self.assertEqual((self.worktree / 'staged.txt').read_text(), 'original working bytes\n')
        self.assertEqual(self.connection.execute('SELECT worker_session_id FROM exec_plans').fetchone()[0], session)
        self.assertFalse(self.run_git(self.worktree, 'ls-files', '-u'))
        with mock.patch.object(loop_manager, '_journaled_direct_rebase',
                               side_effect=AssertionError('completed integration repeated')):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'))

    def test_successor_after_preservation_ref_interruption_resumes_original_input(self):
        self._pending_integration_successor(after_ref=True)

    def test_successor_before_work_rebase_start_resumes_original_input(self):
        self._pending_integration_successor(after_ref=False)


    def test_remote_refresh_successor_only_keeps_unresolved_cause(self):
        self._assert_remote_refresh(base_changed=False, branch_changed=True, resolved=False)


    def test_remote_refresh_adopts_successor_before_new_base_and_resumes(self):
        self._assert_remote_refresh(base_changed=True, branch_changed=True, resolved=True)


    def test_remote_refresh_simultaneous_changes_remain_blocked_and_are_idempotent(self):
        self._assert_remote_refresh(base_changed=True, branch_changed=True, resolved=False)


    def test_remote_refresh_completed_git_effect_survives_before_stage_persistence(self):
        repo, _ = self._remote_refresh_fixture(base_changed=True, branch_changed=True)
        save = blocked_plan_reconciliation.BlockedPlanReconciler._save
        interrupted = False
        def save_after_git(instance, plan_id, key, stage, context):
            nonlocal interrupted
            if stage == 'incorporated' and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return save(instance, plan_id, key, stage, context)
        with (mock.patch.object(blocked_plan_reconciliation.BlockedPlanReconciler, '_save', new=save_after_git),
              mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause', return_value=(False, 'same cause', None))):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main')
            head = self.run_git(self.worktree, 'rev-parse', 'HEAD')
            with mock.patch.object(loop_manager, '_journaled_direct_rebase') as rebase:
                self.assertFalse(loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main'))
            rebase.assert_not_called()
        self.assertEqual(self.run_git(self.worktree, 'rev-parse', 'HEAD'), head)


    def test_remote_refresh_manual_conflict_completion_has_no_worker_receipts(self):
        repo, _ = self._remote_refresh_fixture()
        (self.repo / 'code.py').write_text('base change\n', encoding='utf-8')
        self.run_git(self.repo, 'add', 'code.py')
        self.run_git(self.repo, 'commit', '-m', 'Conflicting base change')
        self.run_git(self.repo, 'push', 'origin', 'main')
        project = self.connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause', return_value=(False, 'same cause', None)) as assess:
            self.assertFalse(loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main'))
            assess.assert_not_called()
            self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))
            (self.worktree / 'code.py').write_text('manual resolution\n', encoding='utf-8')
            self.run_git(self.worktree, 'add', 'code.py')
            self.run_git(self.worktree, '-c', 'core.editor=true', 'rebase', '--continue')
            project = self.connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()
            loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
            self.assertFalse(loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main'))
            assess.assert_called_once()
        self.assertEqual((self.worktree / 'code.py').read_text(), 'manual resolution\n')


    def test_interrupted_rebase_preserves_dirty_worktree(self):
        self._owned_checkpoint_release(dirty=True)


    def test_interrupted_rebase_restores_staged_worktree(self):
        self._owned_checkpoint_release(dirty=True, staged=True)


    def test_interrupted_rebase_preserves_dirty_worktree_after_head_save(self):
        self._owned_checkpoint_release(dirty=True, after_save=True)


    def test_interrupted_rebase_rejects_new_dirty_changes_before_head_save(self):
        self._owned_checkpoint_release(dirty=True, tamper=True)


    def test_interrupted_rebase_rejects_new_dirty_changes_after_head_save(self):
        self._owned_checkpoint_release(dirty=True, tamper=True, after_save=True)


    def test_interrupted_rebase_rejects_changed_preserved_file(self):
        self._owned_checkpoint_release(dirty=True, tamper_preserved=True)


    def test_owned_checkpoint_release_publishes_rebased_head(self):
        self._owned_checkpoint_release()


    def test_new_block_ignores_previous_release_marker(self):
        self._owned_checkpoint_release()
        repo = str(self.repo.resolve())
        store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        previous_oid = self.connection.execute(
            "SELECT accepted_remote_oid FROM blocked_checkpoints "
            "WHERE repo = ? AND plan_id = 'G-034'", (repo,),
        ).fetchone()[0]
        checkpoint_store = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        )
        stale = checkpoint_store.get(repo, 'G-034').snapshot['blocked_resume']
        request = self.block(evidence='second failure')
        current = checkpoint_store.get(repo, 'G-034')
        self.assertNotIn('blocked_resume', current.snapshot or {})
        # Simulate a row written before block_plan cleared the previous marker.
        checkpoint_store.record(
            repo, 'G-034', expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={**(current.snapshot or {}), 'blocked_resume': stale},
        )
        new_oid = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(new_oid, previous_oid)
        tree = self.run_git(self.worktree, 'rev-parse', 'HEAD^{tree}')
        identity = blocked_checkpoint.checkpoint_identity(
            plan_id='G-034', reason_code=request.reason_code,
            evidence=request.evidence, parent_oid=new_oid,
            tree_oid=tree, included_paths=(),
        )
        store.record_prepared(request, blocked_checkpoint.PreparedCheckpoint(
            new_oid, tree, identity, '', (),
        ))
        store.record_local(request, new_oid)
        self.run_git(
            self.worktree, 'push',
            f'--force-with-lease=refs/heads/loop-manager/G-034:{previous_oid}',
            'origin', 'HEAD:refs/heads/loop-manager/G-034',
        )
        store.finish_published(request, new_oid)
        self.assertEqual(stale['owner_oid'], previous_oid)

        plan = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-034'",
            (repo,),
        ).fetchone()
        fingerprint = loop_manager._revision_execplan_input_fingerprint(
            self.repo, 'refs/remotes/origin/main', plan,
        )
        worktree_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
            self.worktree, plan,
        )
        self.connection.execute(
            "UPDATE exec_plans SET blocked_fingerprint = ?, "
            "blocked_worktree_fingerprint = ? "
            "WHERE repo = ? AND plan_id = 'G-034'",
            (fingerprint, worktree_fingerprint, repo),
        )
        self.connection.commit()
        (self.repo / 'docs/PRODUCT.md').write_text(
            'product\nnew input\nsecond input\n', encoding='utf-8',
        )
        self.run_git(self.repo, 'add', 'docs/PRODUCT.md')
        self.run_git(self.repo, 'commit', '-m', 'Change product input again')
        self.run_git(self.repo, 'push', 'origin', 'main')

        self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
            self.connection, self.repo, repo, 'main', self.state_dir,
        ))
        released = self.connection.execute(
            "SELECT status, publication_remote_oid FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-034'", (repo,),
        ).fetchone()
        rebased = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(rebased, new_oid)
        self.assertEqual(tuple(released), ('READY', rebased))
        self.assertEqual(self.run_git(self.repo, 'ls-remote', '--heads', 'origin',
                                      'refs/heads/loop-manager/G-034').split()[0], rebased)
        resumed = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(repo, 'G-034').snapshot['blocked_resume']
        self.assertEqual(resumed['owner_oid'], new_oid)
        self.assertEqual(resumed['before_head'], new_oid)


class ManualRebaseContinuationUnitTests(unittest.TestCase):
    """Exercise the manual-completion fixture without any Git process."""
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.worktree = Path(directory.name)
        (self.worktree / 'code.py').write_text('completed input\n')
        self.run_git = mock.Mock(return_value='unmerged code.py')
        capture = mock.patch.object(loop_manager, 'capture_git_snapshot')
        command = mock.patch.object(subprocess, 'run')
        self.capture = capture.start()
        self.command = command.start()
        self.addCleanup(capture.stop)
        self.addCleanup(command.stop)

    def complete(self):
        CheckpointRebaseGitRecoveryTests._complete_base_conflict(self, 'resolved input\n')

    def test_completed_markers_do_not_repeat_continue_or_modify_files(self):
        retained = {'MERGE_MSG': 'message', 'MERGE_MODE': 'mode', 'REBASE_HEAD': 'previous commit'}
        self.capture.return_value = mock.Mock(operations=retained)
        self.complete()
        self.command.assert_not_called()
        self.run_git.assert_not_called()
        self.assertEqual((self.worktree / 'code.py').read_text(), 'completed input\n')
        self.assertEqual(retained, {'MERGE_MSG': 'message', 'MERGE_MODE': 'mode',
                                    'REBASE_HEAD': 'previous commit'})

    def test_multiple_conflicts_finish_before_retained_markers(self):
        retained = {'MERGE_MSG': 'message', 'REBASE_HEAD': 'previous commit'}
        self.capture.side_effect = [mock.Mock(operations=markers) for markers in (
            {'rebase-merge': 'first conflict'}, {'rebase-apply': 'next conflict'}, retained, retained)]
        self.command.side_effect = [subprocess.CompletedProcess([], code, b'rebase step') for code in (1, 0)]
        self.complete()
        self.assertEqual(self.command.call_count, 2)
        for call in self.command.call_args_list:
            self.assertEqual(call.args[0][-2:], ['rebase', '--continue'])
        self.assertEqual(self.run_git.call_args_list, [
            mock.call(self.worktree, 'add', 'code.py'),
            mock.call(self.worktree, 'ls-files', '-u'),
            mock.call(self.worktree, 'add', 'code.py')])
        self.assertEqual((self.worktree / 'code.py').read_text(), 'resolved input\n')

    def test_foreign_operation_is_refused_before_mutation(self):
        self.capture.return_value = mock.Mock(operations={'MERGE_HEAD': 'other operation'})
        with self.assertRaises(AssertionError):
            self.complete()
        self.command.assert_not_called()
        self.run_git.assert_not_called()
        self.assertEqual((self.worktree / 'code.py').read_text(), 'completed input\n')

    def test_unfinished_rebase_is_not_accepted_after_retry_bound(self):
        self.capture.return_value = mock.Mock(operations={'rebase-merge': 'remaining conflict'})
        self.command.return_value = subprocess.CompletedProcess([], 1, b'conflict')
        with self.assertRaises(AssertionError):
            self.complete()
        self.assertEqual(self.command.call_count, 3)


class CheckpointCauseGitRevalidationTests(CheckpointGitRecoveryFixture, unittest.TestCase):
    def test_review_successor_candidate_rechecks_worker_without_stale_reviewer_evidence(self):
        published = self.publish_block(
            reason="review_retry_exhausted",
            evidence="code.py: reviewer requested a code correction",
        )
        corrected = self.user_commit("code.py", "review correction\n")
        self.assertNotEqual(corrected, published)

        resumed = loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        )

        self.assertTrue(resumed)
        plan = self.connection.execute(
            "SELECT status, worker_result FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(plan), ("IMPLEMENTING", None))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.verify_probe.assert_called_once()
        self.reviewer_probe.assert_not_called()

    def test_published_remote_code_successor_resumes_worker_on_same_workspace(self):
        published = self.publish_block()
        binding = SessionBinding("codex", "model", "high").serialize()
        self.connection.execute(
            "UPDATE exec_plans SET worker_result = 'old', verify_result = 'old', "
            "review_result = 'old', worker_session_id = 'session', "
            "worker_session_binding = ? WHERE plan_id = 'G-034'",
            (binding,),
        )
        self.connection.commit()
        corrected = self.user_commit()
        self.assertNotEqual(corrected, published)

        resumed = loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        )

        self.assertTrue(resumed)
        plan = self.connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(plan["status"], "IMPLEMENTING")
        self.assertEqual(plan["worker_session_id"], "session")
        self.assertEqual(plan["worker_session_binding"], binding)
        self.assertIsNone(plan["worker_result"])
        self.assertIsNone(plan["verify_result"])
        self.assertIsNone(plan["review_result"])
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "main"), self.base_oid)
        stored = self.connection.execute(
            "SELECT published_oid, accepted_remote_oid FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(stored), (published, corrected))


    def test_pathless_diagnostics_reassess_checkpoint_implementation_changes(self):
        self.publish_block(evidence='File "tests/test_code.py", line 12\nAssertionError: 1 != 2')
        self.connection.execute(
            "UPDATE exec_plans SET worker_result = 'old', verify_result = 'AssertionError', "
            "review_result = 'old' WHERE plan_id = 'G-034'"
        )
        self.connection.commit()
        corrected = self.user_commit("code.py", "corrected implementation\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, worker_result, verify_result, review_result "
            "FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", None, None, None))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "main"), self.base_oid)


    def test_rewritten_remote_branch_keeps_local_checkpoint_blocked(self):
        published = self.publish_block()
        before_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        self.user_commit(rewrite=True)

        resumed = loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        )

        self.assertFalse(resumed)
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        plan = self.connection.execute(
            "SELECT status, last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIn("후속이 아닙니다", plan["last_error"])
        after_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        self.assertEqual(after_events, before_events + 1)
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], after_events)


    def test_conflicting_dirty_local_input_is_preserved_before_remote_integration(self):
        published = self.publish_block()
        self.user_commit()
        (self.worktree / "code.py").write_text("local work\n", encoding="utf-8")

        resumed = loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        )

        self.assertFalse(resumed)
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(
            str(self.repo.resolve()), 'G-034')
        intent = saved.snapshot['work_branch_integration']
        baseline = json.loads(intent['baseline'])
        self.assertEqual(self.run_git(self.worktree, 'show', baseline['ref'] + ':code.py'), 'local work')
        self.assertEqual(json.loads(intent['before'])['head'], published)
        self.assertEqual(saved.operation, 'rebase')
        self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))


    def test_local_unpublished_commit_is_not_overwritten_by_remote_divergence(self):
        self.publish_block()
        corrected = self.user_commit()
        (self.worktree / "code.py").write_text("local commit\n", encoding="utf-8")
        self.run_git(self.worktree, "add", "code.py")
        self.run_git(self.worktree, "commit", "-m", "Local unpublished work")
        local = self.run_git(self.worktree, "rev-parse", "HEAD")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(
            str(self.repo.resolve()), 'G-034')
        self.assertEqual(saved.snapshot['work_branch_integration']['local_commit'], local)
        self.assertEqual(self.run_git(self.worktree, 'show', local + ':code.py'), 'local commit')
        self.assertTrue(self.run_git(self.worktree, 'ls-files', '-u'))
        self.assertEqual(self.run_git(self.repo, "ls-remote", "origin",
                                      "refs/heads/loop-manager/G-034").split()[0],
                         corrected)


    def test_unpublished_checkpoint_does_not_adopt_remote_commit(self):
        self.block()
        self.run_git(self.worktree, "push", "origin",
                     "HEAD:refs/heads/loop-manager/G-034")
        corrected = self.user_commit()
        with mock.patch.object(loop_manager, "resume_blocked_execplan_if_inputs_changed",
                               return_value=False):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        self.assertNotEqual(self.run_git(self.worktree, "rev-parse", "HEAD"),
                            corrected)
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM blocked_checkpoints"
        ).fetchone()[0], "requested")


    def test_legacy_published_oid_cannot_be_inferred_from_remote_head(self):
        published = self.publish_block()
        self.connection.execute(
            "UPDATE blocked_checkpoints SET published_oid = NULL, "
            "accepted_remote_oid = NULL"
        )
        self.connection.commit()
        self.user_commit()

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIn("legacy checkpoint", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])


    def test_remote_user_guidance_is_adopted_without_releasing_original_cause(self):
        published = self.publish_block()
        successor = self.user_commit("AGENTS.md", "user guidance\n")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertNotEqual(successor, published)
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), successor)
        self.assertEqual((self.worktree / 'AGENTS.md').read_text(), 'user guidance\n')
        self.assertEqual(scoped_blocking.plan_block(self.connection, str(self.repo.resolve()), 'G-034')['reason_code'],
                         'verify_retry_exhausted')


    def test_remote_noncode_change_remains_blocked_and_records_inspection(self):
        published = self.publish_block()
        before_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        corrected = self.user_commit("docs/FUTURE_IDEAS.md", "later\n")
        with mock.patch.object(loop_manager, "resume_blocked_execplan_if_inputs_changed",
                               return_value=False):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        row = self.connection.execute("SELECT * FROM blocked_checkpoints").fetchone()
        self.assertEqual(row["published_oid"], published)
        self.assertEqual(row["accepted_remote_oid"], corrected)
        self.assertEqual(row["inspected_remote_oid"], corrected)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        after_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        self.assertEqual(after_events, before_events + 1)

        remote_calls = loop_manager.git
        with mock.patch.object(loop_manager, "git", wraps=remote_calls) as git_calls, \
             mock.patch.object(loop_manager, "resume_blocked_execplan_if_inputs_changed",
                               return_value=False):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        self.assertFalse(any(
            call.args[1:2] == ("fetch",) for call in git_calls.call_args_list
        ))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], after_events)

        next_oid = self.user_commit("code.py", "next correction\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), next_oid)


    def test_readme_stays_blocked_but_new_execution_input_is_reassessed(self):
        self.publish_block(evidence='File "tests/test_code.py", line 12\nAssertionError: 1 != 2')
        readme_oid = self.user_commit("README.md", "unrelated notes\n")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        corrected = self.user_commit("dependency.py", "corrected_dependency = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, worker_result, verify_result, review_result "
            "FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", None, None, None))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "main"), self.base_oid)
        self.assertNotEqual(readme_oid, corrected)


    def test_unrelated_code_keeps_cause_and_history_until_probe_passes(self):
        self.publish_block(evidence="AssertionError: original cause")
        self.connection.execute(
            "UPDATE exec_plans SET verify_failure_history = ?, "
            "retry_count = 3, verify_result = 'AssertionError: original cause' WHERE plan_id = 'G-034'",
            ('{"old": {"occurrences": 3}}',),
        )
        self.connection.commit()
        self.verify_probe.return_value = (False, "AssertionError: original cause")
        unrelated = self.user_commit("unrelated.py", "unrelated = True\n")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, retry_count, verify_failure_history, verify_result "
            "FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("PLAN_BLOCKED", 3, '{"old": {"occurrences": 3}}', "AssertionError: original cause"))
        inspected = self.connection.execute(
            "SELECT inspected_remote_oid, inspection_reason FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(inspected), (unrelated, "원격 checkpoint 원인 유지:\nAssertionError: original cause"))
        evidence = self.connection.execute(
            "SELECT evidence FROM execplan_blocks WHERE plan_id = 'G-034'"
        ).fetchone()[0]
        self.assertEqual(evidence, inspected["inspection_reason"])
        payloads = self.connection.execute("SELECT payload FROM notification_outbox").fetchall()
        self.assertTrue(any(evidence in json.loads(row[0]).get("cause", "") for row in payloads))
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.verify_probe.assert_called_once()
        self.verify_probe.return_value = (True, "OK")
        self.user_commit("dependency.py", "corrected_dependency = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.verify_probe.call_count, 2)


    def test_new_verify_failure_resumes_worker_with_new_diagnostic(self):
        self.publish_block(evidence="AssertionError: original cause")
        self.verify_probe.return_value = (False, "AssertionError: new cause")
        self.user_commit("dependency.py", "fixed = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, verify_result FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", "AssertionError: new cause"))


    def test_diagnostic_resume_interruption_rolls_back_release(self):
        self.publish_block(evidence="AssertionError: original cause")
        history = '{"previous": {"occurrences": 3}}'
        self.connection.execute(
            "UPDATE exec_plans SET verify_failure_history = ?, review_failure_history = ? "
            "WHERE plan_id = 'G-034'", (history, history),
        )
        self.connection.commit()
        database = Path(self.temporary.name) / "resume.sqlite"
        durable = sqlite3.connect(database)
        self.connection.backup(durable)
        self.connection.close()
        self.connection = durable
        self.connection.row_factory = sqlite3.Row
        self.verify_probe.return_value = (False, "AssertionError: new cause")
        corrected = self.user_commit("dependency.py", "fixed = True\n")
        original = loop_manager.scoped_blocking.clear_plan_block

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt

        with mock.patch.object(loop_manager.scoped_blocking, "clear_plan_block", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
                )
        self.connection.close()
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        row = self.connection.execute(
            "SELECT status, verify_failure_history, review_failure_history FROM exec_plans "
            "WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("PLAN_BLOCKED", history, history))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_blocks").fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints"
        ).fetchone()[0], corrected)
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, verify_result, verify_failure_history, review_failure_history "
            "FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", "AssertionError: new cause", history, history))
        self.assertEqual(self.connection.execute(
            "SELECT verify_result FROM projects"
        ).fetchone()[0], "AssertionError: new cause")


    def test_new_review_issue_resumes_worker(self):
        old = "ISSUE_ID: code.py::old::bug\nOld issue\nVERDICT: FAIL"
        self.publish_block(reason="review_retry_exhausted", evidence=old)
        self.user_commit("dependency.py", "fixed = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, worker_result, review_result FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", None, None))
        self.verify_probe.assert_called_once()
        self.reviewer_probe.assert_not_called()


    def test_uncertain_probe_termination_blocks_project_without_retry(self):
        self.publish_block()
        self.user_commit("dependency.py", "fixed = True\n")
        self.verify_probe.side_effect = loop_manager.VerifyTerminationUncertain("process may be alive")
        for _ in range(2):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
            ))
        block = loop_manager.scoped_blocking.project_block(self.connection, str(self.repo.resolve()))
        self.assertEqual(block["reason_code"], "repository_state_unsafe")
        self.verify_probe.assert_called_once()
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")


    def test_cause_probe_error_retries_same_candidate_without_release(self):
        self.publish_block()
        self.user_commit("dependency.py", "fixed = True\n")
        self.verify_probe.side_effect = RuntimeError("temporarily unavailable")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        self.verify_probe.side_effect = None
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.verify_probe.call_count, 2)


    def test_review_cause_requires_pass_before_release(self):
        self.publish_block(reason="review_retry_exhausted", evidence="original issue")
        self.reviewer_probe.return_value = (
            0, "ISSUE_ID: code.py::function::bug\nStill present\nVERDICT: FAIL",
        )
        self.user_commit("unrelated.py", "unrelated = True\n")
        # Changed HEAD invalidates the saved Worker result, so Review waits for a fresh Worker turn.
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        plan = self.connection.execute(
            "SELECT status, worker_result, review_result FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(plan), ("IMPLEMENTING", None, None))
        self.verify_probe.assert_called_once()
        self.reviewer_probe.assert_not_called()


    def test_review_result_path_controls_remote_release(self):
        self.publish_block(
            reason="review_retry_exhausted", evidence="review failed",
        )
        self.connection.execute(
            "UPDATE exec_plans SET review_result = 'code.py: blocking issue' "
            "WHERE plan_id = 'G-034'"
        )
        self.connection.commit()
        self.user_commit("README.md", "unrelated notes\n")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")

        corrected = self.user_commit("code.py", "fixed review issue\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"),
                         corrected)


    def test_interruption_after_release_does_not_restore_old_agent_snapshot(self):
        # Preserve the real dispatcher's tree and commit identity for the
        # ownership gate exercised by remote refresh.
        self._remote_refresh_fixture()
        stale = loop_manager.capture_git_snapshot(self.worktree).serialize()
        self.connection.execute(
            "UPDATE exec_plans SET agent_role = 'worker', agent_snapshot = ?, "
            "agent_result_snapshot = ?, agent_manager_snapshot = ?, "
            "agent_baseline = 'old' WHERE plan_id = 'G-034'",
            (stale, stale, loop_manager.ManagerGitCheckpoint(
                loop_manager.GitSnapshot.deserialize(stale), frozenset()).serialize()),
        )
        self.connection.commit()
        corrected = self.user_commit()
        # Project entry observes remote inputs before resume_plan_blocks;
        # exercise that route rather than the unobserved legacy fallback.
        repo = str(self.repo.resolve())
        loop_manager.observe_blocked_plan_remotes(
            self.connection, self.repo, repo,
            self.connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone())
        original = blocked_plan_reconciliation.BlockedPlanReconciler._resume

        def interrupted(*arguments, **keywords):
            original(*arguments, **keywords)
            raise KeyboardInterrupt

        with mock.patch.object(blocked_plan_reconciliation.BlockedPlanReconciler, "_resume",
                               new=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir,
                    str(self.repo.resolve()), "main",
                )

        stored = self.connection.execute(
            "SELECT status, agent_role, agent_snapshot, agent_result_snapshot, "
            "agent_manager_snapshot, agent_baseline FROM exec_plans "
            "WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(stored["status"], "IMPLEMENTING")
        self.assertEqual(stored['agent_snapshot'], stale)
        self.assertEqual(stored['agent_result_snapshot'], stale)
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(
            str(self.repo.resolve()), 'G-034')
        self.assertEqual(loop_manager.GitSnapshot.deserialize(
            saved.snapshot['remote_adoption_scope']['snapshot']).head, corrected)
        loop_manager.hydrate_active_project(
            self.connection, str(self.repo.resolve()),
        )
        project = self.connection.execute(
            "SELECT agent_role, agent_snapshot, agent_result_snapshot, "
            "agent_manager_snapshot FROM projects"
        ).fetchone()
        self.assertEqual(project['agent_snapshot'], stale)
        self.assertEqual(project['agent_result_snapshot'], stale)
        resumed_scope = loop_manager.begin_agent_scope(
            self.connection, str(self.repo.resolve()), self.worktree, "worker",
        )
        self.assertEqual(resumed_scope.head, corrected)


    def test_remote_plan_input_correction_restarts_worker(self):
        self.publish_block(reason="plan_input_invalid")
        blocked = self.connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        original_input = loop_manager._revision_execplan_input_fingerprint(
            self.repo, "HEAD", blocked,
        )
        self.connection.execute(
            "UPDATE exec_plans SET blocked_fingerprint = ? WHERE plan_id = 'G-034'",
            (original_input,),
        )
        self.connection.commit()
        user = Path(self.temporary.name) / "user"
        self.run_git(self.repo, "clone", str(self.remote), str(user))
        self.run_git(user, "config", "user.name", "User")
        self.run_git(user, "config", "user.email", "user@example.com")
        self.run_git(user, "checkout", "-b", "loop-manager/G-034",
                     "origin/loop-manager/G-034")
        path = "docs/plans/[G-034] Checkpoint.md"
        target = user / path
        target.write_text(target.read_text() + "\n- corrected acceptance\n")
        self.run_git(user, "add", "--", path)
        self.run_git(user, "commit", "-m", "Correct plan input")
        self.run_git(user, "push", "origin", "HEAD:refs/heads/loop-manager/G-034")
        corrected = self.run_git(user, "rev-parse", "HEAD")

        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "IMPLEMENTING")


    def test_deleted_remote_branch_is_reported_without_local_mutation(self):
        published = self.publish_block()
        self.run_git(self.worktree, "push", "origin",
                     ":refs/heads/loop-manager/G-034")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIn("삭제", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])


    def test_remote_plan_identity_change_is_rejected(self):
        published = self.publish_block()
        plan_path = "docs/plans/[G-034] Checkpoint.md"
        user = Path(self.temporary.name) / "user"
        self.run_git(self.repo, "clone", str(self.remote), str(user))
        self.run_git(user, "config", "user.name", "User")
        self.run_git(user, "config", "user.email", "user@example.com")
        self.run_git(user, "checkout", "-b", "loop-manager/G-034",
                     "origin/loop-manager/G-034")
        target = user / plan_path
        target.write_text(target.read_text().replace("id: G-034", "id: G-999"))
        self.run_git(user, "add", "--", plan_path)
        self.run_git(user, "commit", "-m", "wrong plan")
        self.run_git(user, "push", "origin", "HEAD:refs/heads/loop-manager/G-034")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIn("ExecPlan", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])


    def test_ignored_path_overlap_refuses_fast_forward(self):
        published = self.publish_block()
        exclude = self.run_git(self.worktree, "rev-parse", "--git-path", "info/exclude")
        Path(exclude).write_text("cache.txt\n", encoding="utf-8")
        (self.worktree / "cache.txt").write_text("local cache\n", encoding="utf-8")
        cache_oid = self.user_commit("cache.txt", "remote cache\n")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertEqual((self.worktree / "cache.txt").read_text(), "local cache\n")
        self.assertIn("ignored", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])

        (self.worktree / "cache.txt").unlink()
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), cache_oid)
        self.assertEqual((self.worktree / "cache.txt").read_text(), "remote cache\n")
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "IMPLEMENTING")


    def test_unrelated_ignored_file_survives_remote_fast_forward(self):
        self.publish_block()
        exclude = self.run_git(self.worktree, "rev-parse", "--git-path", "info/exclude")
        Path(exclude).write_text("cache.txt\n", encoding="utf-8")
        (self.worktree / "cache.txt").write_text("local cache\n", encoding="utf-8")
        corrected = self.user_commit("code.py", "remote code\n")

        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual((self.worktree / "cache.txt").read_text(), "local cache\n")


    def test_crash_after_fast_forward_retries_same_candidate(self):
        self.publish_block()
        corrected = self.user_commit()
        with mock.patch.object(
            blocked_checkpoint_state.BlockedCheckpointRepository,
            "record_remote_inspection", side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir,
                    str(self.repo.resolve()), "main",
                )
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints"
        ).fetchone()[0], corrected)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints"
        ).fetchone()[0], None)


    def test_crash_after_fetch_retries_without_touching_local_head(self):
        published = self.publish_block()
        corrected = self.user_commit()
        with mock.patch.object(
            blocked_checkpoint_state.BlockedCheckpointRepository,
            "start_remote_adoption", side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir,
                    str(self.repo.resolve()), "main",
                )
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIsNone(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints"
        ).fetchone()[0])
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)


    def test_fetch_failure_marks_remote_inspection_retryable(self):
        published = self.publish_block()
        before_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        corrected = self.user_commit()
        original_git = loop_manager.git

        def unavailable_fetch(path, *arguments, **options):
            if arguments and arguments[0] == "fetch":
                return subprocess.CompletedProcess(
                    ["git", "fetch"], 1, "network unavailable",
                )
            return original_git(path, *arguments, **options)

        with mock.patch.object(loop_manager, "git", side_effect=unavailable_fetch):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        inspection = self.connection.execute(
            "SELECT inspected_remote_oid, inspection_retryable "
            "FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(inspection), (corrected, 1))
        failure_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        self.assertEqual(failure_events, before_events + 1)
        with mock.patch.object(loop_manager, "git", side_effect=unavailable_fetch):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], failure_events)

        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)


    def test_remote_refusal_and_notification_retry_after_interruption(self):
        published = self.publish_block()
        self.user_commit(rewrite=True)
        with mock.patch.object(
            loop_manager.notifications, "enqueue_current_project_events",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir,
                    str(self.repo.resolve()), "main",
                )
        self.assertIsNone(self.connection.execute(
            "SELECT inspected_input FROM blocked_checkpoints"
        ).fetchone()[0])
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertIsNotNone(self.connection.execute(
            "SELECT inspected_input FROM blocked_checkpoints"
        ).fetchone()[0])
