import json
import sqlite3
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
import loop_manager
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery import work_branch_integration
from loop_manager_internal.git import managed_rebase_validation as rebase_policy


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


    def _adoption(self, *, fetched_oid=None, observed_oid=None, diverged=False,
                  dirty=False, changed_paths=('code.py',), rewritten=False, candidate_race=False,
                  managed_rebased=False, local_rewritten=False, manager_waiting=False, base_conflict=False,
                  pending_restart=None, pending_input_rewritten=False, foreign_pending=False,
                  completed_base_restart=False):
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
        local = 'c' * 40 if diverged or managed_rebased or local_rewritten or pending_restart else original
        snapshot = loop_manager.GitSnapshot(local, branch, {}, {},
                                            {f'refs/heads/{branch}': (local, '')},
                                            {str(Path(worktree).resolve()): (f'branch=refs/heads/{branch}',)}, {}, {})
        if managed_rebased:
            acceptance = dict(kind=rebase_policy.ACCEPTANCE_KIND, policy_version=rebase_policy.POLICY_VERSION,
                repo='repo', repository_identity='identity', plan_id='G-106', plan_path=plan['path'],
                worktree=worktree, branch=branch, target_base_oid='e' * 40,
                remote=dict(repository='repo', branch=branch, oid=original, pull_request=None),
                head=local, generation=0, input_fingerprint='input', scope_fingerprint='scope',
                state_fingerprint='state', role='worker')
            acceptance['id'] = rebase_policy.fingerprint(acceptance)
            execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).record(
                'repo', 'G-106', expected_generation=0, operation=None, expected_head=local,
                target_base_oid='e' * 40, observed_remote_oid=original,
                snapshot={'current_rebase_acceptance': acceptance})
        if dirty:
            snapshot = replace(snapshot, dirty={'local.txt': (('untracked',), 'bytes')})
        restored = snapshot
        if pending_restart:
            pending_head, integration_remote = 'f' * 40, 'e' * 40
            if pending_restart == 'before_abort':
                snapshot = replace(snapshot, head='detached', branch=None,
                    operations={'rebase-merge': 'owned'},
                    index={'code.py': ('M 100644 ours 2', 'M 100644 theirs 3')},
                    dirty={'code.py': (('staged', 'unstaged'), 'manual conflict bytes')})
            execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).record(
                'repo', 'G-106', expected_generation=0, operation='rebase', expected_head=local,
                target_base_oid=integration_remote, observed_remote_oid=pending_head,
                snapshot={'work_branch_integration': {
                    'phase': 'rebasing', 'remote': integration_remote, 'before': restored.serialize(),
                    'branch': branch, 'worktree': worktree,
                    'restart': {'remote': pending_head, 'baseline': 'pinned-conflict'}},
                    'remote_adoption_intent': {
                        'head': pending_head, 'branch': 'foreign' if foreign_pending else branch,
                        'worktree': worktree, 'request_identity': self.request.request_identity,
                        'snapshot': restored.serialize(), 'agent_snapshot': None, 'agent_result_snapshot': None},
                    **({'base_rebase_restart': {'phase': 'complete', 'remote': '1' * 40}}
                       if completed_base_restart else {})})
            self.store.start_remote_adoption(self.request, pending_head)
        if base_conflict:
            execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).record(
                'repo', 'G-106', expected_generation=0, operation='rebase', expected_head=original,
                target_base_oid='e' * 40, observed_remote_oid=original, snapshot={})
            snapshot = replace(snapshot, head='detached', branch=None, operations={'rebase-merge': 'owned'},
                               dirty={'code.py': (('unstaged',), 'manual conflict bytes')})
        combined = 'd' * 40 if diverged or dirty or managed_rebased or pending_restart else successor
        adopted = replace(restored, head=combined, dirty={},
                          refs={f'refs/heads/{branch}': (combined, '')})
        confirmed = replace(adopted, dirty={'new.txt': (('untracked',), 'raced')}) if candidate_race else adopted
        def git(_, *args, **kwargs):
            output = ''
            code = 0
            if args[:2] == ('remote', 'get-url'):
                output = 'https://github.com/example/repo'
            elif args == ('rev-parse', 'FETCH_HEAD^{commit}'):
                output = fetched_oid or successor
            elif args == ('rev-parse', 'HEAD^{commit}'):
                output = combined
            elif args == ('merge-base', '--is-ancestor', original, successor):
                code = int(rewritten)
            elif args == ('merge-base', '--is-ancestor', original, local):
                code = int(managed_rebased or local_rewritten)
            elif args == ('merge-base', '--is-ancestor', local, successor):
                code = int(diverged or managed_rebased)
            elif pending_restart and args == ('merge-base', '--is-ancestor', pending_head, successor):
                code = int(pending_input_rewritten)
            elif completed_base_restart and args == ('merge-base', '--is-ancestor', '1' * 40, successor):
                code = 1  # A completed historical input may predate a published Manager rebase.
            elif args[:1] == ('ls-tree',):
                output = '100644 blob ' + successor + '\tcode.py'
            return subprocess.CompletedProcess([], code, output)
        with (mock.patch.object(loop_manager, '_BlockedCheckpointAdapter') as adapter,
              mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
              mock.patch.object(loop_manager, 'validate_managed_worktree'),
              mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=(
                  [snapshot, restored, adopted, confirmed] if base_conflict else [snapshot, adopted, confirmed])),
              mock.patch.object(loop_manager, '_nul_git_paths', side_effect=lambda _, *args:
                                changed_paths if args[0] == 'diff' else ()),
              mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False),
              mock.patch.object(loop_manager, '_blocked_execplan_revision_metadata_error', return_value=None),
              mock.patch.object(loop_manager, 'git', side_effect=git) as commands,
              mock.patch.object(loop_manager, 'validate_blocked_agent_scope'),
              mock.patch.object(loop_manager, '_current_rebase_service') as service,
              mock.patch.object(work_branch_integration, 'integrate', return_value=adopted) as integrate,
              mock.patch.object(work_branch_integration, 'restart_base_rebase') as restart,
              mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause') as evaluate):
            adapter.return_value.observe_remote.return_value = observed_oid or successor
            adapter.return_value._approved_guidance_targets.return_value = ()
            service.return_value.require_owned.return_value = (None, mock.Mock(status=mock.Mock(
                value='waiting_for_resolution' if manager_waiting else 'complete')))
            def finish_restart(*args):
                checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
                current = checkpoints.get('repo', 'G-106')
                return checkpoints.record('repo', 'G-106', expected_generation=current.generation,
                    operation=None, expected_head=original, target_base_oid=successor,
                    observed_remote_oid=original, snapshot=current.snapshot)
            restart.side_effect = finish_restart
            if pending_restart:
                def finish_integration(*args):
                    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
                    current = checkpoints.get('repo', 'G-106')
                    checkpoints.record('repo', 'G-106', expected_generation=current.generation,
                        operation=None, expected_head=combined, target_base_oid=successor,
                        observed_remote_oid=successor, snapshot=current.snapshot)
                    return adopted
                integrate.side_effect = finish_integration
            result = loop_manager._reconcile_published_blocked_checkpoint(
                self.connection, Path('/repo'), Path('/state'), 'repo', plan, self.row(),
                self.store.get('repo', 'G-106'), self.store, adopt_only=True, expected_remote_oid=successor)
        evaluate.assert_not_called()
        if base_conflict:
            restart.assert_called_once()
            self.assertEqual(restart.call_args.args[-1], snapshot)
        if ((diverged or dirty or managed_rebased or pending_restart)
                and not rewritten and not manager_waiting and not pending_input_rewritten and not foreign_pending):
            integrate.assert_called_once()
        if pending_restart and (pending_input_rewritten or foreign_pending):
            integrate.assert_not_called()
        self.assertEqual(scoped_blocking.plan_block(self.connection, 'repo', 'G-106')['evidence'], 'original failure')
        return result, commands


class BlockedCheckpointRefreshUnitTests(CheckpointRefreshFixture, unittest.TestCase):
    def test_successor_after_restart_save_retargets_pending_adoption(self):
        result, _ = self._adoption(pending_restart='before_abort')
        self.assertEqual(result, 'adopted')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertIsNone(saved.snapshot['remote_adoption_intent'])
        self.assertEqual(saved.observed_remote_oid, 'b' * 40)
        self.assertEqual(saved.snapshot['work_branch_integration']['restart']['baseline'], 'pinned-conflict')

    def test_successor_after_abort_retargets_pending_adoption(self):
        result, _ = self._adoption(pending_restart='after_abort')
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)

    def test_successor_must_include_pending_restart_input(self):
        result, commands = self._adoption(pending_restart='before_abort', pending_input_rewritten=True)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(saved.snapshot['remote_adoption_intent']['head'], 'f' * 40)
        self.assertFalse(any('merge' in call.args or 'rebase' in call.args for call in commands.call_args_list))

    def test_successor_does_not_retarget_foreign_pending_identity(self):
        result, _ = self._adoption(pending_restart='after_abort', foreign_pending=True)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)

    def test_completed_historical_base_restart_does_not_block_work_restart_successor(self):
        result, commands = self._adoption(pending_restart='after_abort', completed_base_restart=True)
        self.assertEqual(result, 'adopted')
        self.assertFalse(any(call.args[1:] == ('merge-base', '--is-ancestor', '1' * 40, 'b' * 40)
                             for call in commands.call_args_list))

    def test_new_successor_restarts_owned_base_conflict_before_normal_adoption(self):
        result, commands = self._adoption(base_conflict=True)
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)
        self.assertTrue(any('merge' in call.args for call in commands.call_args_list))

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
        self.connection.execute("UPDATE exec_plans SET verify_token = 'old', review_token = 'old', "
                                "publication_token = 'old', worker_result = 'preserved audit'")
        result, _ = self._adoption()
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)
        self.assertEqual(self.connection.execute('SELECT publication_remote_oid FROM exec_plans').fetchone()[0], 'b' * 40)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(checkpoint.observed_remote_oid, 'b' * 40)
        self.assertIsNone(checkpoint.operation)
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertIsNone(plan['verify_token'])
        self.assertIsNone(plan['review_token'])
        self.assertIsNone(plan['publication_token'])
        self.assertEqual(plan['worker_result'], 'preserved audit')


    def test_adoption_fetch_race_does_not_fast_forward_or_change_accepted_oid(self):
        result, commands = self._adoption(fetched_oid='c' * 40)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)
        self.assertFalse(any('merge' in call.args for call in commands.call_args_list))


    def test_adoption_observation_race_is_refused_before_fetch(self):
        result, commands = self._adoption(observed_oid='c' * 40)
        self.assertEqual(result, 'blocked')
        commands.assert_not_called()

    def test_diverged_branch_preserves_local_candidate_and_pins_remote_lease(self):
        result, commands = self._adoption(diverged=True)
        self.assertEqual(result, 'adopted')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(saved.expected_head, 'd' * 40)
        self.assertEqual(saved.observed_remote_oid, 'b' * 40)
        self.assertEqual(saved.snapshot['remote_adoption_scope']['remote'], 'b' * 40)
        self.assertTrue(loop_manager.blocked_plan_reconciliation.remote_adoption_is_current(saved))
        self.assertFalse(any('merge' in call.args for call in commands.call_args_list))

    def test_dirty_branch_uses_preserving_integration(self):
        result, _ = self._adoption(dirty=True)
        self.assertEqual(result, 'adopted')

    def test_user_guidance_successor_is_input_instead_of_worker_scope_violation(self):
        result, _ = self._adoption(changed_paths=('AGENTS.md', 'docs/ARCHITECTURE.md'))
        self.assertEqual(result, 'adopted')

    def test_remote_rewrite_remains_refused_without_integration(self):
        result, _ = self._adoption(rewritten=True)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)

    def test_candidate_race_before_sqlite_confirmation_retains_pending_input(self):
        result, _ = self._adoption(candidate_race=True)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(checkpoint.snapshot['remote_adoption_intent']['head'], 'b' * 40)
        self.assertTrue(self.row()['inspection_retryable'])

    def test_current_owned_manager_rebase_can_replace_local_ancestry_receipt(self):
        result, _ = self._adoption(managed_rebased=True)
        self.assertEqual(result, 'adopted')

    def test_unproven_local_rewrite_remains_refused(self):
        result, _ = self._adoption(local_rewritten=True)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)

    def test_incomplete_manager_rebase_is_not_adopted_as_success(self):
        result, _ = self._adoption(managed_rebased=True, manager_waiting=True)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)
        self.assertTrue(self.row()['inspection_retryable'])
