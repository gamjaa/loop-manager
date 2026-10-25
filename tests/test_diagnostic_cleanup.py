"""Real SQLite/bundles with mocked remote effects, including deletion and interruption."""
from dataclasses import replace
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.git.managed_rebase_validation import Remote, fingerprint, valid_acceptance
from loop_manager_internal.application.contracts import ProjectRunKind, ProjectRunResult
from loop_manager_internal.application.project_runner import RunProjectResultEffects, run_project_result
from loop_manager_internal.recovery import diagnostic_cleanup as cleanup, scoped_blocking
from loop_manager_internal.storage import blocked_checkpoint_state as storage, runtime_state
from tests.support.database import runtime_connection
from tests.support.rebase_observations import expectation, observation, decision_for
from tests.support.diagnostic import ArtifactRecoveryFixture
from tests.support.git import isolated_git_test


class DiagnosticCleanupTests(ArtifactRecoveryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.worktree = str(self.state / 'managed')
        self.connection.execute("UPDATE projects SET origin='owner/repo'")
        self.published = 'e' * 40
        self.connection.execute("UPDATE exec_plans SET branch='branch', worktree=?, publication_remote_oid=?, "
                                "status='PR_OPEN'", (self.worktree, self.published))
        self.connection.execute('DELETE FROM diagnostic_ref_uses')
        self.connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES ('repo', 'G-047', 'plan', 'managed_worktree_conflict', 'conflict')")
        self.block = dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.cause = hashlib.sha256(json.dumps(self.block, sort_keys=True).encode()).hexdigest()
        self.store = storage.DiagnosticPublicationRepository(self.connection)
        self.record(self.saved, self.cause)
        expected = expectation(repo='repo', plan_id='G-047', branch='branch', worktree=self.worktree,
                               plan_path='docs/plans/[G-047] Runtime.md',
                               remote=Remote('repo', 'branch', 'remote'))
        self.acceptance = decision_for(expected, observation(expected)).acceptance
        self.assertIsNotNone(self.acceptance)
        self.assertEqual(self.acceptance['remote']['repository'], self.acceptance['repo'])
        self.assertNotEqual(self.acceptance['remote']['repository'], 'owner/repo')
        self.current = GitSnapshot('after', 'branch', {}, {}, {}, {}, {}, {})
        self.normal = self.published
        self.diagnostic = self.saved['commit_oid']
        self.fail_remote = False
        self.ancestor = True
        self.adapter = mock.Mock()
        self.adapter._validated_push_url.return_value = 'https://github.com/owner/repo.git'
        self.effects = cleanup.CleanupEffects(mock.Mock(side_effect=self.git),
            mock.Mock(side_effect=lambda *_: self.current), mock.Mock(), mock.Mock(),
            mock.Mock(return_value=self.adapter), mock.Mock())
        self.connection.commit()

    def record(self, saved, cause):
        payload = dict(json.loads(saved['payload']), cause_identity=cause, result='local_only')
        self.store.preserve('repo', 'plan', 'G-047', saved['input_identity'], payload,
            ref=saved['ref'], tree=saved['tree_oid'], commit=saved['commit_oid'], retryable=True)
        payload['result'] = 'published'
        self.store.preserve('repo', 'plan', 'G-047', saved['input_identity'], payload,
            ref=saved['ref'], tree=saved['tree_oid'], commit=saved['commit_oid'])

    def git(self, path, *args, **kwargs):
        if 'ls-remote' in args:
            ref = args[-1]
            oid = self.normal if ref == 'refs/heads/branch' else self.diagnostic
            return SimpleNamespace(returncode=int(self.fail_remote),
                stdout=(oid + '\t' + ref + '\n') if oid else '')
        if args[:2] == ('merge-base', '--is-ancestor'):
            return SimpleNamespace(returncode=0 if self.ancestor else 1)
        raise AssertionError(args)

    def release(self, *, acceptance=True):
        with runtime_state._transaction(self.connection):
            scoped_blocking.clear_revalidated_rebase_block(self.connection, self.block,
                is_plan_block=True, acceptance=self.acceptance if acceptance else None)

    def prepare(self):
        return cleanup.prepare_recorded(self.effects, self.connection, self.state, 'repo')

    def test_ready_during_pr_wait_with_independent_block_and_no_side_effects(self):
        self.release()
        resolution = self.connection.execute('SELECT * FROM diagnostic_cause_resolutions').fetchone()
        self.assertIsNotNone(resolution)
        self.assertEqual(resolution['origin'], 'owner/repo')
        self.assertEqual(json.loads(resolution['payload'])['acceptance'], self.acceptance)
        self.connection.execute("INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
            "VALUES ('repo', 'project', 'repository_state_unsafe', 'independent')")
        before = {table: [dict(row) for row in self.connection.execute('SELECT * FROM ' + table)]
            for table in ('projects', 'exec_plans', 'project_blocks', 'diagnostic_preservations',
                          'diagnostic_ref_uses', 'diagnostic_cause_resolutions', 'notification_outbox')}
        result = self.prepare()
        self.assertTrue(result[0]['ready'], result)
        self.assertEqual(result[0]['expected_oid'], self.diagnostic)
        self.assertTrue(all('push' not in call.args and 'update-ref' not in call.args
                            for call in self.effects.git.call_args_list))
        for table, rows in before.items():
            self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM ' + table)], rows)
        self.assertEqual(self.prepare(), result)

    def test_publication_and_resolution_are_both_required_in_either_order(self):
        self.assertFalse(self.prepare()[0]['ready'])
        self.connection.execute('UPDATE exec_plans SET publication_remote_oid=NULL')
        self.release()
        self.assertFalse(self.prepare()[0]['ready'])
        self.connection.execute('UPDATE exec_plans SET publication_remote_oid=?', (self.published,))
        self.assertTrue(self.prepare()[0]['ready'])

    def test_absent_block_and_local_resolution_without_sealed_acceptance_are_insufficient(self):
        self.release(acceptance=False)
        self.assertIsNone(self.connection.execute('SELECT * FROM diagnostic_cause_resolutions').fetchone())
        self.assertFalse(self.prepare()[0]['ready'])

    def test_foreign_acceptance_repository_cannot_resolve_or_prepare(self):
        for repository in ('owner/repo', 'foreign-repository'):
            with self.subTest(repository=repository):
                foreign = json.loads(json.dumps(self.acceptance))
                foreign['remote']['repository'] = repository
                foreign['id'] = fingerprint({key: value for key, value in foreign.items() if key != 'id'})
                self.assertTrue(valid_acceptance(foreign))
                storage.record_diagnostic_cause_resolution(self.connection, self.block, foreign)
                self.assertIsNone(self.connection.execute('SELECT * FROM diagnostic_cause_resolutions').fetchone())
        self.release()
        state = cleanup._snapshot(self.connection, 'repo', self.saved['ref'])
        facts = cleanup._observe(self.effects, self.connection, self.state, state)
        facts['resolutions'][self.cause]['acceptance'] = foreign
        self.assertIsNotNone(cleanup.assess_ref(state, facts))

    def test_resolution_and_release_rollback_together(self):
        with self.assertRaises(KeyboardInterrupt), runtime_state._transaction(self.connection):
            scoped_blocking.clear_revalidated_rebase_block(self.connection, self.block,
                is_plan_block=True, acceptance=self.acceptance)
            raise KeyboardInterrupt()
        self.assertIsNotNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.assertIsNone(self.connection.execute('SELECT * FROM diagnostic_cause_resolutions').fetchone())

    def test_ref_mismatch_unknown_remote_and_unpublished_resolution_are_held(self):
        self.release()
        self.diagnostic = 'd' * 40
        self.assertFalse(self.prepare()[0]['ready'])
        self.diagnostic = self.saved['commit_oid']
        self.fail_remote = True
        self.assertFalse(self.prepare()[0]['ready'])
        self.fail_remote = False
        self.normal = None
        self.assertFalse(self.prepare()[0]['ready'])
        self.normal = self.published
        self.ancestor = False
        self.assertFalse(self.prepare()[0]['ready'])

    def test_already_absent_diagnostic_ref_is_ready_without_recreation(self):
        self.release()
        self.diagnostic = None
        self.assertTrue(self.prepare()[0]['ready'])
        self.assertTrue(all('push' not in call.args for call in self.effects.git.call_args_list))

    def test_missing_or_corrupt_bundle_keeps_ref(self):
        self.release()
        (self.bundle / 'manifest.json').unlink()
        self.assertFalse(self.prepare()[0]['ready'])
        self.seal()
        (self.bundle / 'manifest.json').write_bytes(b'broken')
        self.assertFalse(self.prepare()[0]['ready'])

    def test_origin_change_and_current_git_operation_are_held(self):
        self.release()
        self.connection.execute("UPDATE projects SET origin='other/repo'")
        self.assertFalse(self.prepare()[0]['ready'])
        self.connection.execute("UPDATE projects SET origin='owner/repo'")
        self.current = replace(self.current, operations={'rebase-merge': 'in progress'})
        self.assertFalse(self.prepare()[0]['ready'])

    def test_running_or_uncertain_call_on_same_plan_preserves_the_ref(self):
        self.release()
        self.connection.execute("INSERT INTO task_call_reservations(token,repo,plan_id,kind,uncertain) "
            "VALUES ('call','repo','G-047','agent',1)")
        self.assertFalse(self.prepare()[0]['ready'])

    def test_reactivated_exact_cause_is_not_resolved_by_old_audit(self):
        self.release()
        columns = ','.join(self.block)
        self.connection.execute('INSERT INTO execplan_blocks (' + columns + ') VALUES (' +
                                ','.join('?' for _ in self.block) + ')', tuple(self.block.values()))
        self.assertFalse(self.prepare()[0]['ready'])

    def test_multiple_causes_and_in_progress_publication_require_every_use(self):
        self.release()
        original = self.saved
        self.identity = 'b' * 64
        self.bundle = self.bundle.parent / self.identity
        self.saved = dict(self.saved, input_identity=self.identity)
        self.seal()
        second = dict(self.block, evidence='second conflict')
        columns = ','.join(second)
        self.connection.execute('INSERT INTO execplan_blocks (' + columns + ') VALUES (' +
                                ','.join('?' for _ in second) + ')', tuple(second.values()))
        cause = hashlib.sha256(json.dumps(second, sort_keys=True).encode()).hexdigest()
        self.record(self.saved, cause)
        self.assertEqual(self.saved['ref'], original['ref'])
        self.assertFalse(self.prepare()[0]['ready'])
        self.block = second
        self.release()
        self.assertTrue(self.prepare()[0]['ready'])
        self.connection.execute('UPDATE diagnostic_ref_uses SET publication_pending=1 WHERE cause_identity=?',
                                (cause,))
        self.assertFalse(self.prepare()[0]['ready'])
        self.connection.execute('UPDATE diagnostic_ref_uses SET publication_pending=0')
        self.connection.commit()
        self.enable_delete()
        self.delete()
        self.assertEqual(self.cleanup_row()['status'], 'complete')

    def test_legacy_record_without_target_use_is_held(self):
        self.release()
        self.connection.execute('DELETE FROM diagnostic_ref_uses')
        result = self.prepare()
        self.assertFalse(result[0]['ready'])
        self.assertIn('부족', result[0]['reason'])

    def test_legacy_exact_generation_release_audit_is_sufficient_after_publication(self):
        self.release(acceptance=False)
        checkpoints = cleanup.ExecutionCheckpointRepository(self.connection)
        checkpoints.record('repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid=self.acceptance['target_base_oid'],
            observed_remote_oid=self.acceptance['remote']['oid'], snapshot={})
        checkpoints.accept_current_rebase('repo', 'G-047', acceptance=self.acceptance,
            prior_artifacts={'released_rebase_block': self.block})
        self.current = replace(self.current, head=self.published)
        self.assertTrue(self.prepare()[0]['ready'])
        state = cleanup._snapshot(self.connection, 'repo', self.saved['ref'])
        self.assertEqual(state['resolutions'][0]['origin'], 'owner/repo')

    def test_database_generation_change_during_observation_does_not_seal(self):
        self.release()
        real = self.effects.capture_git_snapshot.side_effect
        def changed(*args):
            self.connection.execute('UPDATE diagnostic_ref_uses SET generation=generation+1')
            return real(*args)
        self.effects.capture_git_snapshot.side_effect = changed
        self.assertEqual(self.prepare(), [])
        self.assertIsNone(self.connection.execute('SELECT * FROM diagnostic_cleanup_preparations').fetchone())

    def test_remote_change_between_observations_is_held(self):
        self.release()
        calls = 0
        def changing(path, *args, **kwargs):
            nonlocal calls
            if 'ls-remote' in args:
                calls += 1
                if calls == 3:
                    self.normal = 'f' * 40
            return self.git(path, *args, **kwargs)
        self.effects.git.side_effect = changing
        self.assertFalse(self.prepare()[0]['ready'])

    def test_preparation_transaction_interruption_preserves_all_original_evidence(self):
        self.release()
        self.assertTrue(self.prepare()[0]['ready'])
        before = dict(self.connection.execute('SELECT * FROM diagnostic_cleanup_preparations').fetchone())
        transaction = runtime_state._transaction
        @contextmanager
        def interrupted(connection):
            with transaction(connection):
                yield
                raise sqlite3.OperationalError('interrupted after preparation write')
        with mock.patch.object(runtime_state, '_transaction', interrupted):
            self.assertEqual(self.prepare(), [])
        self.assertEqual(dict(self.connection.execute('SELECT * FROM diagnostic_cleanup_preparations').fetchone()), before)
        self.assertTrue(self.prepare()[0]['ready'])

    def test_project_resolution_retains_both_existing_cause_identity_formats(self):
        self.release()
        self.connection.execute("INSERT INTO project_blocks (repo, scope, plan_id, reason_code, evidence) "
            "VALUES ('repo', 'project', 'G-047', 'workspace_ownership_uncertain', 'conflict')")
        block = scoped_blocking.project_block(self.connection, 'repo')
        with runtime_state._transaction(self.connection):
            scoped_blocking.clear_revalidated_rebase_block(self.connection, block,
                is_plan_block=False, acceptance=self.acceptance)
        identities = {row[0] for row in self.connection.execute(
            "SELECT cause_identity FROM diagnostic_cause_resolutions WHERE scope='project'")}
        self.assertEqual(identities, {scoped_blocking.project_cause_identity(block),
            hashlib.sha256(json.dumps(dict(block), sort_keys=True).encode()).hexdigest()})

    def test_uncommitted_work_at_release_requires_a_clean_published_successor(self):
        with runtime_state._transaction(self.connection):
            scoped_blocking.clear_revalidated_rebase_block(self.connection, self.block,
                is_plan_block=True, acceptance=self.acceptance, pending_work=True)
        self.current = replace(self.current, dirty={'app.bin': (('unstaged',), 'new')})
        self.assertFalse(self.prepare()[0]['ready'])
        self.current = replace(self.current, dirty={})
        self.assertFalse(self.prepare()[0]['ready'])
        self.current = replace(self.current, head=self.published)
        self.assertTrue(self.prepare()[0]['ready'])
        self.enable_delete()
        self.delete()
        self.assertEqual(self.cleanup_row()['status'], 'complete')

    def test_waiting_release_requires_later_current_complete_with_same_target(self):
        cleanup.ExecutionCheckpointRepository(self.connection)
        with runtime_state._transaction(self.connection):
            scoped_blocking.clear_revalidated_rebase_block(self.connection, self.block,
                is_plan_block=True, pending_target=self.acceptance['target_base_oid'], pending_work=True)
        service = self.effects.current_rebase_service.return_value
        service.require_owned.return_value = (SimpleNamespace(files=()),
            SimpleNamespace(status=SimpleNamespace(value='waiting_for_resolution'), acceptance=None))
        self.assertFalse(self.prepare()[0]['ready'])
        service.require_owned.return_value = (SimpleNamespace(files=()),
            SimpleNamespace(status=SimpleNamespace(value='complete'), acceptance=self.acceptance))
        self.assertTrue(self.prepare()[0]['ready'])
        self.enable_delete()
        self.delete()
        self.assertEqual(self.cleanup_row()['status'], 'complete')

    def test_multiple_refs_are_prepared_independently(self):
        self.release()
        first = self.saved['ref']
        self.identity = 'b' * 64
        self.bundle = self.bundle.parent / self.identity
        self.saved = dict(self.saved, input_identity=self.identity)
        self.assets['diff/worktree.patch'] = b'another diagnostic diff'
        self.seal()
        self.record(self.saved, self.cause)
        # The second immutable ref is not currently at its stored OID.
        results = {row['ref']: row for row in self.prepare()}
        self.assertTrue(results[first]['ready'])
        self.assertFalse(results[self.saved['ref']]['ready'])

    def test_planning_nonconflict_and_original_refs_cannot_be_promoted(self):
        self.release()
        state = cleanup._snapshot(self.connection, 'repo', self.saved['ref'])
        facts = cleanup._observe(self.effects, self.connection, self.state, state)
        for scope in ('planning', 'other'):
            altered = json.loads(json.dumps(state))
            altered['uses'][0]['scope'] = scope
            self.assertIsNotNone(cleanup.assess_ref(altered, facts))
        altered = json.loads(json.dumps(state))
        altered['ref'] = altered['plans'][0]['branch']
        self.assertIsNotNone(cleanup.assess_ref(altered, facts))
        for artifact in facts['artifacts'].values():
            artifact['conflict'] = False
        self.assertIsNotNone(cleanup.assess_ref(state, facts))

    def test_conflict_reason_without_original_unmerged_stages_is_insufficient(self):
        self.release()
        self.identity = 'b' * 64
        self.bundle = self.bundle.parent / self.identity
        self.saved = dict(self.saved, input_identity=self.identity)
        self.manifest['detail']['reason'] = 'managed_worktree_conflict'
        self.manifest['files']['app.bin']['index'] = {}
        self.seal()
        self.record(self.saved, self.cause)
        self.diagnostic = self.saved['commit_oid']
        result = {item['ref']: item for item in self.prepare()}[self.saved['ref']]
        self.assertFalse(result['ready'])
        self.assertIn('충돌', result['reason'])

    def delete(self):
        return cleanup.cleanup_recorded(self.effects, self.connection, self.state, 'repo')

    def cleanup_row(self):
        return self.connection.execute('SELECT * FROM diagnostic_cleanup_attempts').fetchone()

    def enable_delete(self):
        def git(path, *args, **kwargs):
            if 'push' in args:
                self.assertFalse(self.connection.in_transaction)
                self.diagnostic = None
                return SimpleNamespace(returncode=0, stdout='deleted')
            return self.git(path, *args, **kwargs)
        self.effects.git.side_effect = git

    def test_delete_pins_stored_oid_and_keeps_original_evidence_during_pr_wait(self):
        self.release()
        self.enable_delete()
        self.connection.execute("INSERT INTO project_blocks (repo, scope, reason_code, evidence) VALUES ('repo', 'project', 'repository_state_unsafe', 'independent')")
        self.connection.commit()
        tables = ('projects', 'exec_plans', 'project_blocks', 'diagnostic_preservations', 'diagnostic_ref_uses',
                  'diagnostic_cause_resolutions', 'notification_outbox')
        before = {table: [tuple(row) for row in self.connection.execute('SELECT * FROM ' + table)]
                  for table in tables}
        self.delete()
        self.assertEqual(self.cleanup_row()['status'], 'complete')
        pushes = [call for call in self.effects.git.call_args_list if 'push' in call.args]
        self.assertEqual(len(pushes), 1)
        self.assertIn('--force-with-lease=refs/heads/' + self.saved['ref'] + ':' + self.saved['commit_oid'],
                      pushes[0].args)
        self.assertEqual(pushes[0].args[-1], ':refs/heads/' + self.saved['ref'])
        for table in tables:
            self.assertEqual([tuple(row) for row in self.connection.execute('SELECT * FROM ' + table)], before[table])
        self.delete()
        self.assertEqual(sum('push' in call.args for call in self.effects.git.call_args_list), 1)
        payload = self.store.latest_preservation('repo', 'plan', 'G-047')
        self.assertEqual(payload['remote_state'], 'absent')
        self.assertEqual(payload['result'], 'local_only')
        self.assertTrue(payload['historical_publication'])
        self.assertIsNone(self.store.cleanup_receipt('repo', self.saved['ref'], 'new conflict'))

    def test_completed_publication_is_not_redeleted_after_unrelated_basis_change(self):
        self.release()
        self.enable_delete()
        self.delete()
        completed = dict(self.cleanup_row())
        self.diagnostic = self.saved['commit_oid']
        self.effects.git.reset_mock()
        self.connection.execute("INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
            "VALUES ('repo', 'project', 'repository_state_unsafe', 'independent')")
        self.connection.execute('UPDATE diagnostic_ref_uses SET generation=generation+1')
        self.connection.commit()
        state = cleanup._snapshot(self.connection, 'repo', self.saved['ref'])
        self.assertNotEqual(cleanup._digest(state), completed['basis'])
        self.delete()
        self.assertEqual(self.diagnostic, self.saved['commit_oid'])
        self.assertFalse(any('push' in call.args for call in self.effects.git.call_args_list))
        self.assertEqual([dict(row) for row in self.connection.execute(
            'SELECT * FROM diagnostic_cleanup_attempts')], [completed])

    def test_new_publication_use_can_be_cleaned_after_prior_completion(self):
        self.release()
        self.enable_delete()
        self.delete()
        original_ref = self.saved['ref']
        self.identity = 'b' * 64
        self.bundle = self.bundle.parent / self.identity
        self.saved = dict(self.saved, input_identity=self.identity)
        self.seal()
        second = dict(self.block, evidence='new conflict after cleanup')
        columns = ','.join(second)
        self.connection.execute('INSERT INTO execplan_blocks (' + columns + ') VALUES (' +
            ','.join('?' for _ in second) + ')', tuple(second.values()))
        cause = hashlib.sha256(json.dumps(second, sort_keys=True).encode()).hexdigest()
        self.record(self.saved, cause)
        self.assertEqual(self.saved['ref'], original_ref)
        self.diagnostic = self.saved['commit_oid']
        self.effects.git.reset_mock()
        self.delete()
        self.assertFalse(any('push' in call.args for call in self.effects.git.call_args_list))
        self.block = second
        self.release()
        self.delete()
        self.assertIsNone(self.diagnostic)
        self.assertEqual(sum('push' in call.args for call in self.effects.git.call_args_list), 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM diagnostic_cleanup_attempts "
            "WHERE status='complete'").fetchone()[0], 2)

    def test_absence_needs_no_push_and_ambiguous_query_is_not_absence(self):
        self.release()
        self.diagnostic = None
        self.delete()
        self.assertEqual(self.cleanup_row()['status'], 'complete')
        self.assertFalse(any('push' in call.args for call in self.effects.git.call_args_list))

    def test_query_and_oid_mismatch_leave_no_delete_intent(self):
        self.release()
        for mode in ('query failure', 'mismatch', 'ambiguous'):
            with self.subTest(mode=mode):
                self.fail_remote = mode == 'query failure'
                self.diagnostic = 'd' * 40 if mode == 'mismatch' else self.saved['commit_oid']
                if mode == 'ambiguous':
                    self.effects.git.side_effect = lambda *a, **kw: SimpleNamespace(returncode=0, stdout='invalid')
                self.delete()
                self.assertIsNone(self.cleanup_row())
                self.assertFalse(any('push' in call.args for call in self.effects.git.call_args_list))

    def test_old_preparation_is_reobserved_after_generation_origin_or_artifact_change(self):
        self.release()
        original = cleanup.prepare_recorded
        for change in ('generation', 'origin', 'artifact', 'reservation', 'checkpoint'):
            with self.subTest(change=change):
                def prepared(*args):
                    rows = original(*args)
                    if change == 'generation':
                        self.connection.execute('UPDATE diagnostic_ref_uses SET generation=generation+1, publication_pending=1')
                    elif change == 'origin':
                        self.connection.execute("UPDATE projects SET origin='other/repo'")
                    elif change == 'artifact':
                        (self.bundle / 'manifest.json').unlink()
                    elif change == 'reservation':
                        self.connection.execute("INSERT INTO task_call_reservations(token,repo,plan_id,kind) VALUES ('call','repo','G-047','agent')")
                    else:
                        cleanup.ExecutionCheckpointRepository(self.connection).record('repo', 'G-047',
                            expected_generation=0, operation='rebase_publish', snapshot={},
                            expected_head=None, target_base_oid=None, observed_remote_oid=None)
                    self.connection.commit()
                    return rows
                with mock.patch.object(cleanup, 'prepare_recorded', side_effect=prepared):
                    self.delete()
                self.assertFalse(any('push' in call.args for call in self.effects.git.call_args_list))
                self.connection.execute('UPDATE diagnostic_ref_uses SET publication_pending=0')
                self.connection.execute("UPDATE projects SET origin='owner/repo'")
                self.connection.execute('DELETE FROM task_call_reservations')
                self.connection.execute('DELETE FROM execution_checkpoints')
                self.connection.commit()
                self.seal()

    def test_interruption_at_each_durable_boundary_resumes_without_recreation(self):
        self.release()
        self.enable_delete()
        record = cleanup._record_attempt
        for stage in ('intent', 'pushed', 'absent', 'complete'):
            with self.subTest(stage=stage):
                self.connection.execute('DELETE FROM diagnostic_cleanup_attempts')
                self.connection.commit()
                self.diagnostic = self.saved['commit_oid']
                stopped = False
                def interrupted(connection, state, facts, status, **kwargs):
                    nonlocal stopped
                    record(connection, state, facts, status, **kwargs)
                    if status == stage and not stopped:
                        stopped = True
                        raise KeyboardInterrupt()
                with mock.patch.object(cleanup, '_record_attempt', side_effect=interrupted):
                    with self.assertRaises(KeyboardInterrupt):
                        self.delete()
                self.delete()
                self.assertEqual(self.cleanup_row()['status'], 'complete')
                self.assertIsNone(self.diagnostic)

    def test_post_delete_query_failure_keeps_uncertain_and_resumes_on_absence(self):
        self.release()
        def git(path, *args, **kwargs):
            if 'push' in args:
                self.diagnostic = None
                self.fail_remote = True
                return SimpleNamespace(returncode=0, stdout='deleted')
            return self.git(path, *args, **kwargs)
        self.effects.git.side_effect = git
        self.delete()
        self.assertEqual(self.cleanup_row()['status'], 'unconfirmed')
        self.fail_remote = False
        self.delete()
        self.assertEqual(self.cleanup_row()['status'], 'complete')
        self.assertEqual(sum('push' in call.args for call in self.effects.git.call_args_list), 1)

    def test_transient_retry_is_bounded_and_policy_or_lease_rejection_is_held(self):
        self.release()
        for error, count, status in (('network unavailable', 3, 'unconfirmed'),
                                     ('permission denied', 1, 'held'), ('stale info', 1, 'held')):
            with self.subTest(error=error):
                self.connection.execute('DELETE FROM diagnostic_cleanup_attempts')
                self.connection.commit()
                self.effects.git.reset_mock()
                def git(path, *args, **kwargs):
                    return SimpleNamespace(returncode=1, stdout=error) if 'push' in args else self.git(path, *args, **kwargs)
                self.effects.git.side_effect = git
                for _ in range(4):
                    self.delete()
                self.assertEqual(self.cleanup_row()['status'], status)
                self.assertEqual(sum('push' in call.args for call in self.effects.git.call_args_list), count)

    def test_origin_preflight_cannot_push_after_use_generation_changes(self):
        self.release()
        calls = 0
        def validated(_request):
            nonlocal calls
            calls += 1
            if calls == 5:  # preparation twice, locked observations twice, push preflight
                self.connection.execute('UPDATE diagnostic_ref_uses SET generation=generation+1')
                self.connection.commit()
            return 'https://github.com/owner/repo.git'
        self.adapter._validated_push_url.side_effect = validated
        self.delete()
        self.assertGreaterEqual(calls, 5)
        self.assertFalse(any('push' in call.args for call in self.effects.git.call_args_list))
        self.assertEqual(self.cleanup_row()['status'], 'intent')

    def test_database_writes_during_remote_effect_are_free_and_stale_result_is_rejected(self):
        self.release()
        path = self.state / 'database.sqlite'
        other = sqlite3.connect(path)
        self.addCleanup(other.close)
        self.connection.backup(other)
        other.commit()
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        self.connection = connection
        def git(path, *args, **kwargs):
            if 'push' in args:
                other.execute('UPDATE diagnostic_ref_uses SET generation=generation+1')
                other.commit()
                self.diagnostic = None
                return SimpleNamespace(returncode=0, stdout='deleted')
            return self.git(path, *args, **kwargs)
        self.effects.git.side_effect = git
        self.delete()
        self.assertEqual(self.cleanup_row()['status'], 'intent')
        self.assertEqual(self.connection.execute('SELECT generation FROM diagnostic_ref_uses').fetchone()[0], 3)
        self.delete()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM diagnostic_cleanup_attempts WHERE status='complete'").fetchone()[0], 1)
        self.assertEqual(sum('push' in call.args for call in self.effects.git.call_args_list), 1)

    def test_pending_handoff_starts_no_cleanup_observation(self):
        self.release()
        with mock.patch.object(cleanup, '_check_runtime_handoff', side_effect=cleanup.SelfMergeRuntimeHandoff()):
            self.delete()
        self.effects.git.assert_not_called()
        self.assertIsNone(self.cleanup_row())

    def test_busy_plan_or_ref_and_caller_transaction_defer_cleanup(self):
        self.release()
        for resource in ('plan:G-047', 'diagnostic:' + self.saved['ref']):
            with cleanup.resource_locks(self.state, 'repo', [resource], owner='other writer'):
                self.delete()
                self.assertIsNone(self.cleanup_row())
        self.connection.execute("UPDATE projects SET origin='uncommitted'")
        self.delete()
        self.assertTrue(self.connection.in_transaction)
        self.assertIsNone(self.cleanup_row())


class DiagnosticCleanupRunBoundaryTests(unittest.TestCase):
    def test_preparation_failure_cannot_change_run_result(self):
        repo = str(Path('repo').resolve())
        connection = runtime_connection(repo)
        self.addCleanup(connection.close)
        outcome = ProjectRunResult(ProjectRunKind.WAITING, 0)
        prepare = mock.Mock(side_effect=sqlite3.OperationalError('preparation unavailable'))
        effects = RunProjectResultEffects(mock.Mock(return_value=frozenset()),
            mock.Mock(return_value=outcome), prepare)
        self.assertEqual(run_project_result(effects, connection, Path('/state'), repo,
            Path('/config'), None, False), outcome)
        prepare.assert_called_once_with(connection, Path('/state'), repo)

    def test_composition_connects_preparation_at_the_run_boundary(self):
        import loop_manager
        repo = str(Path('repo').resolve())
        connection = runtime_connection(repo)
        self.addCleanup(connection.close)
        outcome = ProjectRunResult(ProjectRunKind.WAITING, 0)
        with (mock.patch.object(loop_manager, '_run_project_result', return_value=outcome),
              mock.patch.object(loop_manager, '_blocked_recovery_subjects', return_value=frozenset()),
              mock.patch.object(loop_manager, 'cleanup_diagnostics') as prepare):
            self.assertEqual(loop_manager.run_project_result(connection, Path('/state'), repo,
                Path('/config'), None, False), outcome)
            prepare.assert_called_once_with(connection, Path('/state'), repo)

    def test_runtime_handoff_does_not_observe_with_the_old_runtime(self):
        from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
        repo = str(Path('repo').resolve())
        connection = runtime_connection(repo)
        self.addCleanup(connection.close)
        prepare = mock.Mock()
        effects = RunProjectResultEffects(mock.Mock(return_value=frozenset()),
            mock.Mock(side_effect=SelfMergeRuntimeHandoff()), prepare)
        with self.assertRaises(SelfMergeRuntimeHandoff):
            run_project_result(effects, connection, Path('/state'), repo, Path('/config'), None, False)
        prepare.assert_not_called()


@isolated_git_test
class DiagnosticCleanupGitTests(ArtifactRecoveryFixture, unittest.TestCase):
    def test_native_delete_and_competing_commit_preserve_all_other_refs_and_bundle(self):
        # Manager-only native contract: one deletion and one lease race. Policy
        # combinations use DiagnosticCleanupTests without repeating native Git.
        import subprocess
        from tests.support.git import make_remote_repository, git_command, git_test_environment
        from loop_manager_internal.recovery.checkpoint_adapter import delete_diagnostic_checkpoint
        from loop_manager_internal.recovery.blocked_checkpoint import CheckpointPushError
        repo = make_remote_repository(str(self.state))
        remote = self.state / 'remote.git'
        def git(path, *args, **kwargs):
            environment = git_test_environment()
            environment.update(kwargs.get('env', {}))
            completed = subprocess.run(['git', '-C', str(path), *args],
                env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            if kwargs.get('check', True) and completed.returncode:
                raise RuntimeError(completed.stdout)
            return completed
        effects = cleanup.diagnostic_preservation.PreservationEffects(git, mock.Mock(), mock.Mock())
        tree, head = cleanup.diagnostic_preservation._commit(effects, self.state,
            SimpleNamespace(worktree=repo), self.assets)
        self.assertEqual((tree, head), (self.saved['tree_oid'], self.saved['commit_oid']))
        ref = self.saved['ref']
        git_command(repo, 'push', str(remote), head + ':refs/heads/' + ref,
                    'main:refs/heads/work', 'main:refs/heads/user', 'main:refs/heads/other')
        git_command(repo, 'update-ref', 'refs/remotes/origin/' + ref, head)
        local_before = git_command(repo, 'show-ref')
        protected = {line for line in git_command(remote, 'show-ref').splitlines() if not line.endswith('/' + ref)}
        hook = repo / '.git/hooks/pre-push'
        hook.write_text('#!/bin/sh\nexit 99\n')
        hook.chmod(0o755)
        adapter = mock.Mock()
        adapter._validated_push_url.return_value = str(remote)
        request = SimpleNamespace(repo=str(repo), worktree=str(repo), branch=ref)
        arguments = dict(target_digest=hashlib.sha256(str(remote).encode()).hexdigest())
        delete_diagnostic_checkpoint(git, adapter, request, head, revalidate=lambda: None, **arguments)
        self.assertEqual(git_command(repo, 'ls-remote', '--heads', str(remote), 'refs/heads/' + ref), '')
        self.assertEqual(set(git_command(remote, 'show-ref').splitlines()), protected)
        self.assertEqual(git_command(repo, 'show-ref'), local_before)
        self.assertEqual(cleanup.diagnostic_preservation.load_verified_artifacts(self.state, self.saved)[0], self.assets)
        git_command(remote, 'update-ref', 'refs/heads/' + ref, head)
        competitor = git_command(repo, 'rev-parse', 'main')
        def race():
            git_command(remote, 'update-ref', 'refs/heads/' + ref, competitor)
        with self.assertRaises(CheckpointPushError):
            delete_diagnostic_checkpoint(git, adapter, request, head, revalidate=race, **arguments)
        self.assertEqual(git_command(remote, 'rev-parse', 'refs/heads/' + ref), competitor)
        self.assertEqual(git_command(repo, 'show-ref'), local_before)
        self.assertEqual({line for line in git_command(remote, 'show-ref').splitlines()
                          if not line.endswith('/' + ref)}, protected)
