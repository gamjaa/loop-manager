"""Publication Phase execution policy regression tests."""

from tests.support.execution import ExecutionFixture
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRecord, ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionOutcomeKind
from loop_manager_internal.storage import blocked_checkpoint_state
from tests.support.worker import decision as worker_decision
from tests.support.execution import READY_WORKER_RESULT


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios
from tests.support.execution_assertions import _assert_post_push_pr_attachment


class ExecutionPublicationPhaseTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_scope_stage_false_block_recovers_existing_pr_through_fresh_validation(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=True, existing_pr=True, manual='staged', scope_stage_block=True)


    def test_manual_stage_owned_pr_requires_fresh_validation_before_publication(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=True, existing_pr=True, manual='staged',
        )


    def test_manual_completed_owned_pr_recovers_changed_plan_without_rebase_journal(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, existing_pr=True, manual='complete')


    def test_legacy_rebase_existing_pr_recovers_after_restart(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, existing_pr=True)


    def test_worker_decision_blocks_before_publication_and_keeps_failure_history(self):
        self._decision_workspace()
        message = worker_decision()
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, message, session_id='session')
        )
        self.connection.execute(
            "UPDATE exec_plans SET verify_failure_history = ?, "
            "review_failure_history = ?, effective_worker_level = 7 "
            "WHERE repo = 'repo' AND plan_id = 'G-047'",
            ('{"verify":2}', '{"review":3}'),
        )
        self.connection.commit()
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        blocked = self.repository.get('repo', 'G-047')
        self.assertEqual(blocked.status, 'PLAN_BLOCKED')
        row = self.connection.execute(
            "SELECT blocked_reason, blocked_fingerprint, worker_result, "
            "worker_session_id, agent_snapshot, agent_result_snapshot, "
            "verify_failure_history, review_failure_history, effective_worker_level "
            "FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(row['blocked_reason'], 'user_decision_required')
        self.assertEqual(row['blocked_fingerprint'], 'remote-input')
        self.assertEqual(row['worker_result'], message)
        self.assertEqual(row['worker_session_id'], 'session')
        self.assertEqual(row['agent_snapshot'], 'before-scope')
        self.assertEqual(row['agent_result_snapshot'], 'after-scope')
        self.assertEqual(row['verify_failure_history'], '{"verify":2}')
        self.assertEqual(row['review_failure_history'], '{"review":3}')
        self.assertEqual(row['effective_worker_level'], 7)
        recovery = self.connection.execute(
            "SELECT final_evidence FROM recovery_records WHERE repo = 'repo' "
            "AND subject = 'G-047'"
        ).fetchone()
        self.assertEqual(json.loads(recovery[0])['worker_decision']['question'],
                         '이 변경을 승인합니까?')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.agent.clear_agent_scope.assert_not_called()
        self.assertNotIn('commit', self.calls)


    def test_owned_pr_conflict_uses_same_correction_history_without_publication(self):
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='pr-worker-session',
            pr_conflict_recovery=1, pr_conflict_base_oid='base',
            pr_conflict_head_oid='before',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'PR conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'abnormal exit', session_id='pr-worker-session',
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        saved = self.handler.rebase_corrections.get('repo', 'G-047')
        self.assertEqual((saved.target_base, saved.before_head), ('base', 'before'))
        self.assertEqual(saved.worker_session, 'pr-worker-session')
        self.effects.publication.push_owned_pr_conflict_recovery.assert_not_called()


    def test_owned_pr_correction_stages_only_publishable_resolution(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='pr-worker-session',
            pr_conflict_recovery=1, pr_conflict_base_oid='base',
            pr_conflict_head_oid='before',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'PR conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, READY_WORKER_RESULT, session_id='pr-worker-session',
        )
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={
                'app.py': (('unstaged',), 'resolved-content'),
                'cache/ignored.bin': (('untracked',), 'preserved-cache'),
            },
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.context.phase, ExecutionPhase.IMPLEMENTING)
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1] == 'add']
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].args[1:], ('add', '-A', '--', 'app.py'))
        self.assertEqual(self.state.row()['worker_session_id'], 'pr-worker-session')
        self.effects.publication.push_owned_pr_conflict_recovery.assert_not_called()


    def test_publication_intent_records_expected_head_and_completes(self):
        checkpoint = mock.Mock()
        checkpoint.get.side_effect = [
            CheckpointRecord(generation=1),
            CheckpointRecord(generation=2, operation='push',
                             expected_head='base', target_base_oid='base',
                             observed_remote_oid='old'),
        ]
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        context = self.context('PUBLISHING')

        self.handler._publication_intent(context, 'push', 'old')
        self.assertEqual(checkpoint.record.call_args_list[0].kwargs['operation'], 'push')
        self.assertEqual(checkpoint.record.call_args_list[0].kwargs['expected_head'], 'base')
        self.handler._complete_publication_intent(context)
        self.assertIsNone(checkpoint.record.call_args_list[1].kwargs['operation'])


    def test_confirmed_checkpoint_push_remains_owned_during_pr_creation(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.handler.effects = replace(self.effects, checkpoint=checkpoint)
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', 'evidence', "
            "'loop-manager/G-047', '/state/G-047', ?)",
            ('a' * 40,),
        )
        marker = {'owner_oid': 'a' * 40, 'before_head': 'before',
                  'after_head': 'after', 'target_base_oid': 'base'}
        row = {'pr_url': None, 'publication_remote_oid': 'b' * 40}
        current = SimpleNamespace(
            expected_head='b' * 40, operation='push',
            snapshot={'blocked_resume': marker, 'remote_before': 'a' * 40},
        )
        from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService
        from tests.support.rebase_observations import expectation, observation, scope_for
        def current_service(context, **options):
            expected = expectation(repo='repo', worktree='/state/G-047',
                                   branch=context.branch, plan_id='G-047',
                                   plan_path=context.plan.path)
            expected = replace(expected, remote=replace(expected.remote,
                               repository='repo', branch=context.branch, oid=options['remote_oid']))
            service = ManagedRebaseService(expected, mock.Mock())
            return service
        self.handler._current_rebase_service = current_service
        def current_observation(expected, _):
            actual = observation(expected, head='b' * 40, branch_ref=('b' * 40, ''),
                registration=('HEAD=' + 'b' * 40, 'branch=refs/heads/' + expected.branch))
            return actual, scope_for(expected, actual)
        def git(_repo, *args, **_kwargs):
            self.assertEqual(args[0], 'ls-remote')
            return SimpleNamespace(
                returncode=0,
                stdout='b' * 40 + '\trefs/heads/loop-manager/G-047\n',
            )
        self.effects.workspace.git.side_effect = git
        with (
            mock.patch('loop_manager_internal.git.managed_rebase_adoption.observe_current_rebase',
                       side_effect=current_observation),
            mock.patch.object(handlers.checkpoint_ownership,
                              'rebase_preserves_commits', return_value=True),
            mock.patch.object(handlers.checkpoint_ownership,
                              'owned_remote_oid', return_value='a' * 40),
        ):
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
            ))
            row['publication_remote_oid'] = 'a' * 40
            with mock.patch.object(self.effects.workspace.git, 'side_effect', new=lambda _repo, *args, **kw: (
                SimpleNamespace(stdout='b' * 40 + '\n', returncode=0)
                if args[0] == 'rev-parse' else git(_repo, *args, **kw)
            )):
                self.assertTrue(self.handler._owned_checkpoint_remote(
                    self.context(), row, 'b' * 40, current,
                ))
                current.snapshot['remote_before'] = 'external'
                self.assertFalse(self.handler._owned_checkpoint_remote(
                    self.context(), row, 'b' * 40, current,
                ))
                current.snapshot['remote_before'] = 'a' * 40
                current.expected_head = 'external'
                self.assertFalse(self.handler._owned_checkpoint_remote(
                    self.context(), row, 'b' * 40, current,
                ))
                current.expected_head = 'b' * 40
            row['publication_remote_oid'] = 'b' * 40
            current.operation = None
            current.snapshot['pushed_checkpoint_head'] = 'b' * 40
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
            ))
            self.assertFalse(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
                {'headRefOid': 'b' * 40, 'url': 'new-pr'},
            ))
            current.operation = 'pr_create'
            current.snapshot['remote_before'] = 'b' * 40
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
                {'headRefOid': 'b' * 40, 'url': 'new-pr'},
            ))


    def test_interrupted_owned_push_persists_oid_before_clearing_intent(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', 'evidence', "
            "'loop-manager/G-047', '/state/G-047', 'old-head')"
        )
        marker = {'owner_oid': 'old-head', 'before_head': 'old-head',
                  'after_head': 'owned-head', 'target_base_oid': 'base'}
        self.state.update_project('repo', publication_remote_oid='old-head')
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='push',
            expected_head='owned-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={'blocked_resume': marker, 'remote_before': 'old-head'},
        )
        def observed_git(path, *args, **kwargs):
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout='owned-head\trefs/heads/loop-manager/G-047\n',
                    returncode=0,
                )
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'owned-head'
        self.effects.publication.pr_for_branch.return_value = None
        with (
            mock.patch.object(handlers.checkpoint_ownership,
                              'rebase_preserves_commits', return_value=True),
            mock.patch.object(handlers.checkpoint_ownership,
                              'owned_remote_oid', return_value='old-head'),
        ):
            self.handler.recover(self.context())
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'owned-head')


    def test_interrupted_rebased_checkpoint_push_updates_projection_before_intent(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', 'evidence', "
            "'loop-manager/G-047', '/state/G-047', 'old-head')"
        )
        self.state.update_project('repo', publication_remote_oid='old-head',
                                  worker_session_id='worker-session')
        proof = {'generation': 1, 'remote_oid': 'old-head',
                 'after_head': 'rebased-head'}
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='old-head', target_base_oid='base',
            observed_remote_oid='old-head', snapshot={},
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=1, operation=None,
            expected_head='rebased-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={'completed_rebase': proof},
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=2, operation='push',
            expected_head='owned-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={'completed_rebase': proof, 'after_head': 'owned-head',
                      'remote_before': 'old-head'},
        )
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'owned-head'
        self.effects.publication.pr_for_branch.return_value = None
        original_record = checkpoint.record
        def record_after_projection(*args, **kwargs):
            if (kwargs.get('operation') is None
                    and kwargs.get('observed_remote_oid') == 'owned-head'):
                self.assertEqual(
                    self.state.row()['publication_remote_oid'], 'owned-head',
                )
            return original_record(*args, **kwargs)
        with (mock.patch.object(self.handler, '_owned_checkpoint_remote',
                                return_value=True),
              mock.patch.object(checkpoint, 'record',
                                side_effect=record_after_projection)):
            self.handler.recover(self.context())

        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.observed_remote_oid, 'owned-head')
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'owned-head')


    def test_completed_owned_push_persists_new_oid_in_execution_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        marker = {'owner_oid': 'a' * 40, 'before_head': 'before',
                  'after_head': 'after', 'target_base_oid': 'base'}
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='push',
            expected_head='b' * 40, target_base_oid='base',
            observed_remote_oid='a' * 40,
            snapshot={'blocked_resume': marker, 'remote_before': 'a' * 40},
        )

        self.handler._complete_publication_intent(self.context('PUBLISHING'))

        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.observed_remote_oid, 'b' * 40)
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'b' * 40)


    def test_external_remote_checkpoint_uses_publication_ownership_cause(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'external-head'

        result = self.handler.recover(self.context('PUBLISHING'))

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.REMOTE_OID_UNCERTAIN)
        self.assertIn('갱신을 거부', result.recovery.observation.detail)


    def test_old_pr_head_after_rebase_cannot_attach_or_push(self):
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'existing', 'headRefOid': 'old',
            'headRefName': 'loop-manager/G-047', 'state': 'OPEN',
        }
        self.effects.workspace.remote_branch_oid.return_value = 'old'
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.reconcile_existing_pull_request.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()


    def test_post_push_pr_with_different_head_is_not_attached(self):
        _assert_post_push_pr_attachment(self, self, 'other-head', False)


    def test_post_push_pr_with_verified_head_is_attached(self):
        _assert_post_push_pr_attachment(self, self, 'base', True)


    def test_existing_pr_recovery_does_not_push_or_create(self):
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'existing', 'headRefOid': 'base',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertTrue(self.calls)
        self.assertTrue(all(call == 'rev-parse' for call in self.calls))


    def test_pr_rebase_resume_operation_failure_retries_without_checkpoint(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.resume_pr_conflict_rebase.return_value = lm.PROJECT_STEP_RETRY
        self.state.update_project(
            'repo', pr_conflict_recovery=1,
            last_error=lm.REBASE_IN_PROGRESS_ERROR,
            worker_session_id='worker-session',
        )

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'rebase_continue_failed')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_publication_checkpoint_sha_lag_reconciles_from_owned_head(self):
        self.state.update_project(
            'repo', phase='PUBLISHING', active_sha='stale',
            validated_base_sha='base', worker_session_id='worker-session',
            verify_result='saved verify', review_result='saved review',
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'checkpoint_stale')
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['active_sha'], 'base')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertIsNone(self.state.row()['verify_result'])
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_publication_interruption_recovers_existing_pr_without_repush(self):
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.command.side_effect = RuntimeError('interrupted response')
        paused = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(paused.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(paused.recovery.observation.reason.value, 'publication_retry')
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'created', 'headRefOid': 'base',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        resumed = self.handler.publish(self.context(), self.handler)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.calls.count('push'), 1)
        self.assertEqual(self.effects.publication.command.call_count, 1)


    def test_pr_conflict_lease_rejection_blocks_without_opening_pr(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='PUBLISHING', active_sha='base',
            validated_base_sha='base', publication_remote_oid='old-head',
            pr_conflict_recovery=1, pr_conflict_head_oid='old-head',
            pr_conflict_base_oid='base', pr_head_oid='old-head',
            pr_url='https://example/pr/1',
        )
        self.effects.workspace.remote_branch_oid.return_value = 'old-head'
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'https://example/pr/1', 'state': 'OPEN',
            'headRefName': 'loop-manager/G-047', 'headRefOid': 'old-head',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = None
        self.effects.publication.push_owned_pr_conflict_recovery.side_effect = (
            lm.PublicationSafetyError('lease rejected')
        )

        prepared = self.handler.recover(self.context())
        self.assertEqual(prepared.phase, ExecutionPhase.PUBLISHING)
        result = self.handler.publish(prepared, self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs['reason'],
            lm.BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
        )
        self.effects.publication.push_owned_pr_conflict_recovery.assert_called_once()
        self.effects.state.check()
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')
