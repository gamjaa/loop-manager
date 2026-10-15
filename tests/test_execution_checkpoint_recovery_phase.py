"""Checkpoint Recovery Phase execution policy regression tests."""
from contextlib import closing

from tests.support.current_rebase import current_rebase_case
from tests.support.execution import ExecutionFixture
from dataclasses import replace
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.git import rebase_block_reconciliation
import loop_manager as lm
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionPhaseCoordinator, ExecutionOutcomeKind
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.recovery import blocked_checkpoint
from tests.support.worker import decision as worker_decision


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionCheckpointRecoveryPhaseTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_manual_detached_block_recovers_changed_plan_without_rebase_journal(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, manual='waiting')


    def test_legacy_rebase_false_block_recovers_after_restart_with_later_file_changes(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, continue_to_publication=True)


    def test_legacy_rebase_project_block_recovers_after_restart_with_later_file_changes(self):
        self._legacy_rebase_false_block_recovery(plan_block=False)


    def test_legacy_rebase_rejects_changes_after_git_completion(self):
        # Same plan block and absent remote; vary only the post-restart mutation.
        self._legacy_rebase_false_block_recovery(
            plan_block=True, rejected=('index-flags', 'remote-created', 'late-file'),
        )


    def test_legacy_rebase_rejects_remote_branch_changed_without_fetch(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, rejected='remote-changed')


    def test_legacy_rebase_rechecks_live_remote_after_journal_sealing(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, rejected='late-remote')


    def test_rebase_journal_match_requires_run_identity_only_for_v2(self):
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-047'),
            branch='loop-manager/G-047', worktree=Path('/state/G-047'),
            worker_session_id='worker-session',
        )
        observed = {'head': 'after', 'branch': context.branch}
        transition = {
            'intent': {
                'kind': 'managed_rebase',
                'target': str(context.worktree.resolve()),
                'expected': {
                    'branch': context.branch, 'run_id': 'other-run',
                    'checkpoint_generation': 2, 'before_head': 'before',
                    'target_base_oid': 'base', 'remote_oid': 'remote',
                },
            },
            'result': {'observed': observed},
        }
        current = SimpleNamespace(
            expected_head='before', target_base_oid='base', generation=2,
            snapshot={'rebase_evidence_version': 2, 'rebase_run_id': 'run'},
        )
        with mock.patch.object(
            handlers.manager_git_journal, 'verified_managed_transitions',
            return_value=[transition],
        ):
            self.assertFalse(self.handler._matching_rebase_journal_result(
                context, current, 'remote', observed,
            ))
            transition['intent']['expected']['run_id'] = 'run'
            self.assertTrue(self.handler._matching_rebase_journal_result(
                context, current, 'remote', observed,
            ))
            transition['intent']['expected']['checkpoint_generation'] = 3
            self.assertFalse(self.handler._matching_rebase_journal_result(
                context, current, 'remote', observed,
            ))


    def test_sealed_rebase_completion_updates_runtime_without_repeating_git(self):
        case = current_rebase_case(self)
        handlers.recovery_state.begin_external_run(case.fixture.connection)
        base = case.state.row()['active_sha']
        handlers.recovery_state.RecoveryRepository(case.fixture.connection).observe(
            handlers.RecoveryObservation(handlers.RecoveryReason.STATUS_FAILED, 'repo', 'G-047',
                'WORKING', 'plan-revision', base, 'old-failure', f'{base}|saved-worker|',
                rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC))
        # Production commits the run marker and recovery observation before
        # adopting a rebase; this fixture owns those setup writes.
        case.fixture.connection.commit()
        self.assertFalse(case.fixture.connection.in_transaction)
        with mock.patch.object(handlers.manager_git_journal, 'verified_rebase_completion',
                               side_effect=AssertionError('history is not a current gate')):
            case.handler._complete_rebase_intent(case.fixture.context())
        saved = case.checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['current_rebase_acceptance']['head'], 'owned-head')
        self.assertEqual(case.state.row()['phase'], 'WORKING')
        self.assertIsNone(case.state.row()['worker_result'])
        self.assertEqual(case.state.row()['worker_session_id'], 'saved-worker')
        failure = case.fixture.connection.execute(
            "SELECT active, resolved_at FROM persistent_execution_failures WHERE repo = 'repo'").fetchone()
        self.assertEqual(failure['active'], 0)
        self.assertIsNotNone(failure['resolved_at'])
        case.handler._complete_rebase_intent(case.handler._context())
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), saved)


    def test_worker_decision_checkpoint_wait_survives_three_restarted_runs(self):
        worktree = self._decision_workspace()
        notifications.configure_session(self.connection, enabled=True)
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 1, "
            "verify_failure_history = '{\"verify\":2}', "
            "review_failure_history = '{\"review\":3}' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        plan_file = worktree / 'docs/plans/[G-047] Runtime.md'
        plan_file.write_text(
            plan_file.read_text(encoding='utf-8') + '- Worker decision pending\n',
            encoding='utf-8',
        )
        message = worker_decision()
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, message, session_id='session')
        )
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection).get('repo', 'G-047').phase,
            blocked_checkpoint.CheckpointPhase.REQUESTED)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / 'state.sqlite'
        with closing(sqlite3.connect(database)) as saved, saved:
            self.connection.backup(saved)

        self.effects.agent.run_agent.reset_mock()
        self.effects.validation.verify.reset_mock()
        self.effects.validation.parse_reviewer_result.reset_mock()
        local_only = blocked_checkpoint.SafetyVerdict(
            False, blocked_checkpoint.SafetyScope.PLAN,
            'git_operation_in_progress', 'unresolved rebase', None, (),
        )
        for iteration in range(3):
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.row_factory = sqlite3.Row
                notifications.configure_session(connection, enabled=True)
                with (
                    mock.patch.object(lm.checkpoint_adapter.BlockedCheckpointAdapter, 'assess',
                                      return_value=local_only) as assess,
                    mock.patch.object(lm, '_revision_execplan_input_fingerprint',
                                      return_value='remote-input'),
                    mock.patch.object(lm, '_revision_review_issue_fingerprint') as review,
                    mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                    mock.patch.object(lm, 'make_worktree'),
                    mock.patch.object(lm, '_resume_interrupted_blocked_rebase',
                                      return_value=None),
                    mock.patch.object(lm, 'validate_blocked_agent_scope'),
                ):
                    self.assertEqual(lm.dispatch_blocked_checkpoints(
                        connection, Path('/repo'), Path('/state'), 'repo',
                    ), 1 if iteration == 0 else 0)
                    self.assertFalse(lm.resume_plan_blocks(
                        connection, Path('/repo'), Path('/state'), 'repo', 'main',
                    ))
                    self.assertEqual(assess.call_count, 1 if iteration == 0 else 0)
                    review.assert_not_called()
                row = connection.execute(
                    "SELECT status, blocked_reason, worker_result, worker_session_id, "
                    "verify_failure_history, review_failure_history "
                    "FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                self.assertEqual(tuple(row), (
                    'PLAN_BLOCKED', 'user_decision_required', message, 'session',
                    '{"verify":2}', '{"review":3}',
                ))
                checkpoint = connection.execute(
                    "SELECT phase, result, unpublished_reason "
                    "FROM blocked_checkpoints WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                self.assertEqual(tuple(checkpoint),
                                 ('unpublished', 'local_only', 'unresolved rebase'))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox "
                    "WHERE repo = 'repo' AND event_type = 'blocked' "
                    "AND canceled_at IS NULL"
                ).fetchone()[0], 1)
                # The artifact receipt replaces the initial checkpoint notice;
                # restarting the same wait does not create further notices.
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox "
                    "WHERE repo = 'repo' AND event_type = 'blocked'"
                ).fetchone()[0], 2)
        self.assertIn('Worker decision pending', plan_file.read_text(encoding='utf-8'))
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.validation.parse_reviewer_result.assert_not_called()


    def test_no_change_rebase_blocks_before_sealed_completion(self):
        self.effects.workspace.git.side_effect = None
        self.effects.workspace.git.return_value = SimpleNamespace(
            stdout='', returncode=0,
        )
        self.effects.workspace.branch_has_changes.return_value = False
        self.handler._record_rebase_intent = mock.Mock()
        self.handler._run_managed_rebase = mock.Mock(return_value=SimpleNamespace(
            returncode=0, stdout='',
        ))
        self.handler._post_rebase_observation = mock.Mock(return_value=None)
        self.handler._complete_rebase_intent = mock.Mock()

        outcome = self.handler._refresh_revision_base(
            self.context('VERIFYING'), None,
            SimpleNamespace(phase='VERIFYING'), 'new-base',
        )

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.handler._complete_rebase_intent.assert_not_called()


    def test_rebase_format_failure_starts_fresh_turn_until_no_progress_limit(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='same-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, 'invalid format', session_id='same-session',
        )
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError(
            'invalid format'
        )

        outcomes = [self.handler.run(self.context()) for _ in range(3)]

        self.assertEqual([outcome.kind for outcome in outcomes], [
            ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.RETRY,
            ExecutionOutcomeKind.FINAL_BLOCK,
        ])
        self.assertEqual(self.effects.agent.run_agent.call_count, 3)
        self.assertEqual(self.handler.rebase_corrections.get('repo', 'G-047').occurrence, 3)


    def test_saved_new_rebase_result_requires_outcome_before_finishing(self):
        message = (
            '사용자 승인을 기다려 구현을 중단했습니다.\n'
            'COMMIT_TITLE: Preserve resolved rebase changes\n'
            'PR_SUMMARY: Waiting for approval.\n'
            'TARGETED_TESTS:\ntargeted test: PASS'
        )
        context = self._save_rebase_worker_result(message)
        # Reconstruct the repository as after a Manager restart.
        self.handler.rebase_corrections = type(self.handler.rebase_corrections)(self.connection)
        with mock.patch.object(self.handler, '_finish_worker') as finish:
            resumed = self.handler.run(context)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.RETRY)
        self.assertIn('WORKER_OUTCOME', self.state.row()['last_error'])
        finish.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.assertNotIn('add', self.calls)


    def test_new_activation_drops_old_execution_head_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.handler.newly_activated = True
        checkpoint.record(
            'repo', 'G-047', expected_generation=0,
            operation=None, expected_head='old-execution-head',
            target_base_oid='old-base', observed_remote_oid=None,
            snapshot={'old_phase': 'PUBLISHING'},
        )
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.expected_head, 'owned-head')
        self.assertEqual(saved.snapshot['reactivated_from_head'],
                         'old-execution-head')


    def test_completed_published_rebase_keeps_remote_lease_only_with_sealed_owner(self):
        case = current_rebase_case(self)
        context = case.fixture.context()
        checkpoint = {'accepted_remote_oid': 'remote'}
        row = dict(case.state.row(), publication_remote_oid='remote')
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid', return_value='remote'), \
             mock.patch.object(handlers.manager_git_journal, 'verified_managed_transitions',
                               side_effect=AssertionError('no replay gate')):
            self.assertTrue(case.handler._owned_persisted_rebase(context, row, 'remote',
                            case.checkpoint.get('repo', 'G-047'), checkpoint, None))
            case.state.update_project('repo', worker_session_id='replacement-session')
            self.assertTrue(case.handler._owned_persisted_rebase(case.handler._context(), row, 'remote',
                            case.checkpoint.get('repo', 'G-047'), checkpoint, None))
            original = case.observed
            for change in ({'remote': replace(original.remote, oid='foreign')},
                           {'base_is_ancestor': False}, {'branch_ref': ('foreign', '')},
                           {'operations': (('MERGE_HEAD', 'foreign'),)}):
                with self.subTest(change=change):
                    case.observed = replace(original, **change)
                    self.assertFalse(case.handler._owned_persisted_rebase(context, row, 'remote',
                                     case.checkpoint.get('repo', 'G-047'), checkpoint, None))
            case.observed = original
            candidate = case.checkpoint.record('repo', 'G-047', expected_generation=1, operation='push',
                expected_head='candidate', target_base_oid='base', observed_remote_oid='remote',
                snapshot={'remote_before': 'remote'})
            case.observed = replace(original, head='candidate', branch_ref=('candidate', ''),
                registration=('HEAD=candidate', 'branch=refs/heads/loop-manager/G-047'),
                remote=replace(original.remote, oid='candidate'))
            self.assertTrue(case.handler._owned_persisted_rebase(context, row, 'candidate', candidate, checkpoint, None))
            self.assertFalse(case.handler._owned_persisted_rebase(context, row, 'foreign', candidate, checkpoint, None))
            self.assertFalse(case.handler._owned_persisted_rebase(context, row, 'candidate', candidate,
                                                                checkpoint, {'headRefOid': 'foreign'}))


    def test_published_rebase_completion_accepts_current_ref_without_remote_ancestry(self):
        self._published_rebase_completion(legacy=False)


    def test_legacy_published_rebase_completion_accepts_current_ref_without_remote_ancestry(self):
        self._published_rebase_completion(legacy=True)


    def test_preparation_resumes_durable_rebase_continue_without_worker(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', active_sha='old-base',
            validated_base_sha=None, worker_result='saved correction',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='old-head', target_base_oid='new-base',
            observed_remote_oid=None,
            snapshot={'pending': 'continue', 'worker_result': 'saved correction'},
        )
        self.remote = 'new-base'
        operation = {'rebase': True}

        def observed_git(path, *args, **kwargs):
            if args[0] == 'symbolic-ref':
                return SimpleNamespace(stdout='loop-manager/G-047\n', returncode=0)
            if args[0] == 'ls-files':
                return SimpleNamespace(stdout='', returncode=0)
            if args[:2] == ('diff', '--cached'):
                return SimpleNamespace(stdout='', returncode=0)
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='', returncode=0)
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='owned-head\n', returncode=0)
            if args[:2] == ('rebase', '--skip'):
                operation['rebase'] = False
                return SimpleNamespace(stdout='completed', returncode=0)
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace._rebase_in_progress_for_branch.side_effect = (
            lambda *_: operation['rebase']
        )
        context = self.context()

        recovered = self.handler.recover(context)

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.effects.agent.run_agent.assert_not_called()


    def test_conflicted_rebase_new_worker_input_resumes_same_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.revision.body_hash.return_value = 'revised-body'

        outcome = ExecutionPhaseCoordinator(self.handler).dispatch(
            self.handler._context()
        )

        self.assertNotEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.agent.run_agent.assert_called_once()
        self.assertEqual(self.effects.agent.run_agent.call_args.kwargs['session_id'], 'worker-session')
        prompt = self.effects.agent.run_agent.call_args.args[5]
        self.assertIn('rebase 충돌 복구의 최신 구현 조건', prompt)
        self.assertIn('git show', prompt)
        self.assertIsNone(self.effects.agent.worker_prompt.call_args.args[2])
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_transient_worker_pause_preserves_rebase_resume_marker(self):
        self.state.update_project(
            'repo', pr_conflict_recovery=1,
            last_error=lm.REBASE_IN_PROGRESS_ERROR,
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'temporary provider failure', 'worker-session',
            lm.AgentResultKind.TRANSIENT_FAILURE,
        )

        result = self.handler.run(self.handler._context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertTrue(
            self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX)
        )
        self.assertTrue(self.handler._context().resolving_rebase)
