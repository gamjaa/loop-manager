"""Failure Policy execution policy regression tests."""

from tests.support.execution import ExecutionFixture
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from tests.support.worker import decision as worker_decision


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionFailurePolicyTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_published_legacy_dirty_rejection_preserves_journal_for_restored_files(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=True, rejected='unstaged', published_checkpoint=True,
        )


    def test_legacy_project_recovery_ignores_stale_remote_cache(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, stale_cache=True)


    def test_published_legacy_recovery_ignores_stale_remote_cache(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=False, published_checkpoint=True, stale_cache=True,
        )


    def test_project_resume_is_recorded_when_execution_phase_starts(self):
        notifications.configure_session(self.connection, enabled=True)
        with self.connection:
            self.connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence) "
                "VALUES ('repo', 'project', 'repository_state_unsafe', 'G-047', 'unsafe')"
            )
            notifications.enqueue_current_project_events(self.connection, 'repo')
        notifications.dispatch_pending(
            self.connection, 'token', 'chat', adapter=mock.Mock(),
        )
        with self.connection:
            scoped_blocking.clear_project_block(self.connection, 'repo')
        self.handler.work_started(self.context())
        self.assertIsNone(self.connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchone())
        with self.connection:
            notifications.record_process_start(self.connection, 'repo', 'worker', 'actual-start', plan_id='G-047')
        resumed = self.connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchall()
        self.assertEqual(len(resumed), 1)
        self.assertEqual(json.loads(resumed[0][0])['operation'],
                         'Worker')


    def test_worker_decision_without_file_change_bypasses_no_change_gate(self):
        self._decision_workspace()
        self.dirty = False
        self.effects.workspace.branch_has_changes.return_value = False
        message = worker_decision()
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, message, session_id='session')
        )
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'unchanged-scope'
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.connection.execute(
            "SELECT blocked_reason FROM exec_plans WHERE repo = 'repo' "
            "AND plan_id = 'G-047'"
        ).fetchone()[0], 'user_decision_required')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()


    def test_format_error_then_three_abnormal_results_reach_safety_gate(self):
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
        context = self.context()

        self.handler._rebase_conflict(context, 'conflict')
        invalid = self.handler._rebase_conflict(
            context, 'invalid format', corrected=True,
            worker_result='invalid worker result',
            recovery_reason=handlers.RecoveryReason.WORKER_CORRECTION,
        )
        self.assertEqual(invalid.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.handler.rebase_corrections.get('repo', 'G-047').occurrence, 1)

        for _ in range(2):
            retry = self.handler._rebase_conflict(
                context, 'abnormal exit', corrected=True,
                worker_result='same abnormal result', abnormal_result=True,
            )
            self.assertEqual(retry.kind, ExecutionOutcomeKind.RETRY)
            self.effects.blocking.block_active_execplan.assert_not_called()
        final = self.handler._rebase_conflict(
            context, 'abnormal exit', corrected=True,
            worker_result='same abnormal result', abnormal_result=True,
        )
        self.assertEqual(final.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.handler.rebase_corrections.get('repo', 'G-047').occurrence, 4)


    def test_saved_worker_result_finishes_after_restart_without_second_agent_turn(self):
        legacy_result = (
            'COMMIT_TITLE: Preserve resolved rebase changes\n'
            'PR_SUMMARY: Resolves the saved rebase conflict.\n'
            'TARGETED_TESTS:\ntargeted test: PASS'
        )
        context = self._save_rebase_worker_result(legacy_result, outcome_contract_version=0)

        resumed = self.handler.run(context)

        self.assertEqual(resumed.context.phase, ExecutionPhase.IMPLEMENTING)
        self.effects.agent.run_agent.assert_not_called()
        self.assertIsNone(self.state.row()['worker_result'])

        self.effects.validation.parse_worker_result.assert_any_call(legacy_result, self.plan)
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_changed_pending_turn_without_scope_baseline_blocks_before_agent(self):
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
        context = self.context()
        observation = self.handler._rebase_observation(context)
        stored = self.handler.rebase_corrections.observe('repo', 'G-047', observation)
        self.handler.rebase_corrections.begin_turn(
            'repo', 'G-047', expected_generation=stored.generation,
            session='same-session',
        )
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('unstaged',), 'changed-content')},
        )

        result = self.handler.run(context)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.agent.run_agent.assert_not_called()


    def test_new_activation_keeps_published_resume_lineage(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.handler.newly_activated = True
        marker = {
            'owner_oid': 'remote-head', 'before_head': 'old-execution-head',
            'after_head': 'owned-head', 'target_base_oid': 'base',
        }
        checkpoint.record(
            'repo', 'G-047', expected_generation=0,
            operation=None, expected_head='owned-head',
            target_base_oid='base', observed_remote_oid='remote-head',
            snapshot={'blocked_resume': marker},
        )
        self.effects.workspace.git.side_effect = self.observed_git
        with mock.patch.object(self.handler, '_owned_checkpoint_remote',
                               return_value=True):
            recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['blocked_resume'], marker)
        self.assertEqual(saved.snapshot['reactivated_from_head'], 'owned-head')


    def test_commit_command_failure_preserves_expected_head_intent(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects

        def failing_git(path, *args, **kwargs):
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='before-head\n', returncode=0)
            if args == ('write-tree',):
                return SimpleNamespace(stdout='c' * 40 + '\n', returncode=0)
            if args[0] == 'commit':
                raise lm.GitLockError('index.lock')
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = failing_git
        with self.assertRaises(lm.GitLockError):
            self.handler._commit_owned(self.context('VERIFYING'), 'change')
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.operation, 'commit')
        self.assertEqual(saved.expected_head, 'before-head')
        self.assertEqual(saved.snapshot['commit_tree_oid'], 'c' * 40)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')


    def test_real_handlers_complete_each_phase_and_reject_replay(self):
        context = self.handler.guard(self.context())
        for method in ('run', 'verify', 'review', 'publish'):
            old = context
            call = getattr(self.handler, method)
            args = (old, self.handler) if method in {'verify', 'publish'} else (old,)
            result = call(*args)
            context = result.context
            before = list(self.calls)
            with self.assertRaises(runtime_state.RuntimeStateError):
                call(*args)
            self.assertEqual(self.calls, before)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PR_OPEN')
        order = [c for c in self.calls if c in {'worker', 'verify', 'commit', 'reviewer', 'push', 'pr'}]
        self.assertEqual(order, ['worker', 'commit', 'verify', 'reviewer', 'push', 'pr'])
        self.assertEqual(self.calls.count('verify'), 1)


    def test_stale_callback_cannot_update_same_phase_new_snapshot(self):
        context = self.context()
        self.repository.update_project('repo', {'worker_result': 'newer observation'})
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.state.update_project('repo', worker_session_id='stale')
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.handler.run(context)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.repository.get('repo', 'G-047').worker_session_id)


    def test_working_body_change_clears_retry_artifacts_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            worker_session_id='worker-session',
            worker_result='previous worker result',
            verify_result='failed verify result',
            review_result='previous review result',
            validated_base_sha='base',
        )
        self.effects.revision.body_hash.return_value = 'revised-body'

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, ExecutionOutcome)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertIsNone(row['validated_base_sha'])
        self.assertEqual(row['worker_session_id'], 'worker-session')


    def test_working_source_change_invalidates_artifacts_but_preserves_retry_history(self):
        self.handler.guard(self.context())
        worker_token = DefinitionSnapshot.deserialize(
            self.state.row()['definition_snapshot'],
        ).worker_token
        self.state.update_project(
            'repo',
            worker_session_id='worker-session',
            reviewer_session_id='reviewer-session',
            worker_result='previous worker result',
            verify_result='failed verify result',
            review_result='previous review result',
            validated_base_sha='base',
            worker_input_token=worker_token,
            archive_token='previous-archive',
            verify_token='previous-verify',
            review_token='previous-review',
            publication_token='previous-publication',
        )
        self.repository.update_plan('repo', 'G-047', plan_values={
            'retry_count': 2, 'verify_failure_history': '{"past": 1}',
        }, enqueue=False)
        self.state.expected = self.repository.projected('repo')
        before = self.state.row()
        self.effects.revision.source_fingerprint.return_value = 'revised-source'

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, ExecutionOutcome)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        for field in ('worker_result', 'verify_result', 'review_result',
                      'validated_base_sha', 'worker_input_token', 'archive_token',
                      'verify_token', 'review_token', 'publication_token'):
            with self.subTest(field=field):
                self.assertIsNone(row[field])
        for field in ('worker_session_id', 'reviewer_session_id', 'branch', 'worktree'):
            with self.subTest(field=field):
                self.assertEqual(row[field], before[field])
        plan = self.repository.active_row('repo')
        self.assertEqual(plan['retry_count'], 2)
        self.assertEqual(plan['verify_failure_history'], '{"past": 1}')
        current = DefinitionSnapshot.deserialize(row['definition_snapshot'])
        self.assertEqual(current.source_fingerprint, 'revised-source')
        self.assertNotEqual(current.worker_token, worker_token)


    def test_dependency_gate_reports_unmerged_direct_and_indirect_plans(self):
        for plan_id, dependencies in (
            ('G-035', '[]'),
            ('G-036', '["G-035"]'),
        ):
            self.connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "('repo', ?, ?, ?, '[\"docs/PRODUCT.md\"]', ?, 'normal', "
                "'hash', 'READY')",
                (plan_id, f'docs/plans/[{plan_id}] Plan.md', 'Plan', dependencies),
            )
        self.connection.execute(
            "UPDATE exec_plans SET depends_on = '[\"G-036\"]' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()

        self.assertEqual(
            lm.unmet_execplan_dependencies(
                self.connection, 'repo', 'G-047'
            ),
            ('G-035', 'G-036'),
        )
        self.connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = 'repo' AND plan_id IN ('G-035', 'G-036')"
        )
        self.assertEqual(
            lm.unmet_execplan_dependencies(
                self.connection, 'repo', 'G-047'
            ),
            (),
        )
