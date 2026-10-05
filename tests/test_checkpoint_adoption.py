import json
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
import loop_manager
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import checkpoint_ownership


class CheckpointRefreshFixture:
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")
        self.connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status) "
            "VALUES ('repo', 'G-106', 'docs/plans/[G-106] Work.md', 'Work', '[]', '[]', 'critical', 'hash', 'PLAN_BLOCKED')")
        self.store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        self.request = self.store.request(repo='repo', plan_id='G-106', reason_code='verify_retry_exhausted',
                                          evidence='original failure', branch='loop-manager/G-106', worktree=str(Path('/state/G-106')),
                                          expected_remote_oid='a' * 40, rebase_recovery=False)
        self.store.record_prepared(self.request, blocked_checkpoint.PreparedCheckpoint(
            'a' * 40, 'c' * 40, 'identity', 'message', ()))
        self.store.finish_published(self.request, 'a' * 40)


    def row(self):
        return self.connection.execute('SELECT * FROM blocked_checkpoints').fetchone()


    def _adoption(self, *, fetched_oid=None, observed_oid=None):
        branch, worktree = 'loop-manager/G-106', str(Path('/state/G-106'))
        original, successor = 'a' * 40, 'b' * 40
        self.connection.execute("UPDATE projects SET origin = 'example/repo'")
        self.connection.execute(
            "UPDATE exec_plans SET branch = ?, worktree = ?, workspace_owned = 1, blocked_reason = 'verify_retry_exhausted'",
            (branch, worktree))
        self.connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES ('repo', 'G-106', 'plan', 'verify_retry_exhausted', 'original failure')")
        self.connection.commit()
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        snapshot = loop_manager.GitSnapshot(original, branch, {}, {},
                                            {f'refs/heads/{branch}': (original, '')},
                                            {str(Path(worktree).resolve()): (f'branch=refs/heads/{branch}',)}, {}, {})
        def git(_, *args, **kwargs):
            output = ''
            if args[:2] == ('remote', 'get-url'):
                output = 'https://github.com/example/repo'
            elif args == ('rev-parse', 'FETCH_HEAD^{commit}'):
                output = fetched_oid or successor
            elif args == ('rev-parse', 'HEAD^{commit}'):
                output = successor
            elif args[:1] == ('ls-tree',):
                output = '100644 blob ' + successor + '\tcode.py'
            return subprocess.CompletedProcess([], 0, output)
        with (mock.patch.object(loop_manager, '_BlockedCheckpointAdapter') as adapter,
              mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
              mock.patch.object(loop_manager, 'validate_managed_worktree'),
              mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
              mock.patch.object(loop_manager, '_nul_git_paths', side_effect=lambda _, *args:
                                ('code.py',) if args[0] == 'diff' else ()),
              mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False),
              mock.patch.object(loop_manager, '_blocked_execplan_revision_metadata_error', return_value=None),
              mock.patch.object(loop_manager, 'git', side_effect=git) as commands,
              mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause') as evaluate):
            adapter.return_value.observe_remote.return_value = observed_oid or successor
            adapter.return_value._approved_guidance_targets.return_value = ()
            result = loop_manager._reconcile_published_blocked_checkpoint(
                self.connection, Path('/repo'), Path('/state'), 'repo', plan, self.row(),
                self.store.get('repo', 'G-106'), self.store, adopt_only=True, expected_remote_oid=successor)
        evaluate.assert_not_called()
        self.assertEqual(scoped_blocking.plan_block(self.connection, 'repo', 'G-106')['evidence'], 'original failure')
        return result, commands


class BlockedCheckpointRefreshUnitTests(CheckpointRefreshFixture, unittest.TestCase):
    def test_refresh_keeps_published_ownership_and_same_request(self):
        request = self.store.get('repo', 'G-106')
        self.store.refresh(request, expected_remote_oid='a' * 40)
        row = self.row()
        self.assertEqual(row['phase'], 'requested')
        self.assertEqual(row['published_oid'], 'a' * 40)
        self.assertEqual(row['commit_identity'], 'identity')
        self.assertEqual(row['request_identity'], request.request_identity)
        self.assertEqual(row['evidence'], 'original failure')


    def test_refresh_does_not_overwrite_pending_push_lease(self):
        request = self.store.get('repo', 'G-106')
        self.store.refresh(request, expected_remote_oid='a' * 40)
        self.store.record_local(request, 'b' * 40)
        self.store.finish_unpublished(request, 'network error', retryable=True)
        self.store.refresh(self.store.get('repo', 'G-106'), expected_remote_oid='different')
        row = self.row()
        self.assertEqual(row['phase'], 'retry_pending')
        self.assertEqual(row['expected_remote_oid'], 'a' * 40)
        self.assertEqual(row['local_oid'], 'b' * 40)


    def test_rebased_result_preserves_prior_identity_and_accepts_next_successor(self):
        request = self.store.get('repo', 'G-106')
        with self.connection:
            self.store.record_rebased_publication(request, 'b' * 40, 'd' * 40)
        first = dict(self.row())
        with self.connection:
            self.store.record_rebased_publication(request, 'b' * 40, 'd' * 40)
        self.assertEqual(dict(self.row()), first)
        history = json.loads(self.row()['publication_history'])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['published_oid'], 'a' * 40)
        self.assertEqual(history[0]['commit_identity'], 'identity')
        self.assertEqual(self.row()['reason_code'], 'verify_retry_exhausted')
        def git(_, *args, **kwargs):
            output = ''
            if args[:1] == ('rev-parse',):
                output = 'd' * 40
            elif args[:1] == ('show',):
                output = 'Rebased result with no checkpoint trailer'
            elif args[:1] == ('ls-remote',):
                output = 'b' * 40 + '\trefs/heads/loop-manager/G-106\n'
            return mock.Mock(returncode=0, stdout=output)
        self.assertEqual(checkpoint_ownership.owned_remote_oid(
            self.connection, Path('/repo'), 'repo', 'G-106', 'loop-manager/G-106',
            Path('/state/G-106'), 'b' * 40, git, before_head='b' * 40, recorded_oid='b' * 40), 'b' * 40)


    def test_refresh_preparation_retains_old_publication_audit(self):
        request = self.store.get('repo', 'G-106')
        self.store.refresh(request, expected_remote_oid='a' * 40)
        prepared = blocked_checkpoint.PreparedCheckpoint('a' * 40, 'new-tree', 'new-identity', 'new-message', ())
        self.store.record_prepared(request, prepared)
        history = json.loads(self.row()['publication_history'])
        self.assertEqual(history[0]['commit_identity'], 'identity')
        self.assertEqual(self.row()['commit_identity'], 'new-identity')


    def test_adoption_defers_cause_check_and_records_exact_successor_lease(self):
        result, _ = self._adoption()
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)
        self.assertEqual(self.connection.execute('SELECT publication_remote_oid FROM exec_plans').fetchone()[0], 'b' * 40)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(checkpoint.observed_remote_oid, 'b' * 40)
        self.assertIsNone(checkpoint.operation)


    def test_adoption_fetch_race_does_not_fast_forward_or_change_accepted_oid(self):
        result, commands = self._adoption(fetched_oid='c' * 40)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)
        self.assertFalse(any('merge' in call.args for call in commands.call_args_list))


    def test_adoption_observation_race_is_refused_before_fetch(self):
        result, commands = self._adoption(observed_oid='c' * 40)
        self.assertEqual(result, 'blocked')
        commands.assert_not_called()
