"""Run the real phase policies with in-memory state and fake external effects."""
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import execution_handlers as handlers
from execution_revision import DefinitionSnapshot
import loop_manager as lm
import runtime_state
from execution_state import ExecutionStatePort
from execution_runtime import (
    ExecutionPhase,
    ExecutionPhaseCoordinator,
    ExecutionOutcome,
    ExecutionOutcomeKind,
)
from tests import test_runtime_state


class ExecutionHandlerTests(TestCase):
    def setUp(self):
        self.connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(self.connection.close)
        self.repository = runtime_state.RuntimeStateRepository(self.connection)
        self.repository.activate('repo', 'G-047', activation_sha='base',
                                 branch='loop-manager/G-047', worktree='/state/G-047')
        self.state = ExecutionStatePort(self.connection, self.repository, 'repo')
        self.plan = {'id': 'G-047', 'title': 'Runtime', 'sources': ['docs/PRODUCT.md']}
        self.calls = []
        self.remote = 'base'
        self.dirty = True

        def bundle(cls):
            return cls(**{f.name: mock.Mock(name=f.name, return_value=None)
                          for f in fields(cls)})

        self.effects = handlers.ExecutionEffects(
            state=self.state, contract=lm,
            workspace=bundle(handlers.WorkspaceEffects),
            agent=bundle(handlers.AgentEffects),
            validation=bundle(handlers.ValidationEffects),
            archive=bundle(handlers.ArchiveEffects),
            blocking=bundle(handlers.BlockingEffects),
            publication=bundle(handlers.PublicationEffects),
            events=bundle(handlers.EventsEffects),
            revision=bundle(handlers.RevisionEffects),
        )
        self.effects.workspace.is_directory.return_value = False
        self.effects.workspace.branch_has_changes.return_value = True
        self.effects.workspace.git.side_effect = self.git
        self.effects.agent.validate_worker_workspace_scope.return_value = self.plan
        self.effects.validation.validate_active_execplan_metadata.return_value = self.plan
        self.effects.validation._deserialize_failure_history.return_value = {}
        self.effects.validation.parse_worker_result.return_value = SimpleNamespace(commit_title='Concrete change')
        self.effects.validation.verify.side_effect = lambda _: (self.calls.append('verify') or True, 'OK')
        self.effects.validation.parse_reviewer_result.return_value = ('PASS', ())
        self.effects.agent.run_agent_with_retries.side_effect = lambda call, session_id: call(session_id, False)
        self.effects.agent.run_agent.side_effect = self.agent
        self.effects.publication.published_execplan.return_value = self.plan
        self.effects.publication.push_managed_publication_branch.side_effect = lambda *a: self.calls.append('push') or 'head'
        self.effects.publication.command.side_effect = lambda *a, **k: self.calls.append('pr') or SimpleNamespace(stdout='https://example/pr/1')
        self.effects.revision.reconcile_catalog.side_effect = lambda *_: SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha=self.remote
        )
        self.effects.revision.body_hash.return_value = 'body'
        self.effects.revision.source_fingerprint.return_value = 'source'
        self.effects.revision.changed_paths.return_value = ()
        self.effects.revision.unmet_dependencies.return_value = ()
        self.effects.archive.requirement_archive_sources.return_value = ()
        self.handler = handlers.ExecutionHandlers(
            self.effects, repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')

    def git(self, path, *args, **kwargs):
        self.calls.append(args[0])
        if args[0] == 'rebase' and '--skip' not in args:
            self.assertEqual(self.state.row()['last_error'], lm.REBASE_IN_PROGRESS_ERROR)
        if args[0] == 'commit':
            self.dirty = False
        output = self.remote if args[0] == 'rev-parse' else ''
        if args[0] == 'status' and self.dirty:
            output = ' M app.py'
        return SimpleNamespace(stdout=output, returncode=0)

    def agent(self, *args, **kwargs):
        self.calls.append(args[4])
        kwargs['on_session_started']('session')
        return lm.AgentResult(0, 'result', session_id='session')

    def context(self, phase=None):
        if phase is not None:
            self.state.update_project('repo', phase=phase)
        return self.handler._context()

    def test_real_handlers_complete_each_phase_and_reject_replay(self):
        context = self.context()
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
        self.assertEqual(order, ['worker', 'verify', 'commit', 'reviewer', 'push', 'pr'])

    def test_revision_guard_tokens_follow_complete_phase_lifecycle(self):
        context = self.handler.guard(self.context())
        self.assertNotIsInstance(context, handlers.ExecutionOutcome)

        for method in ('run', 'verify', 'review', 'publish'):
            call = getattr(self.handler, method)
            args = (context, self.handler) if method in {'verify', 'publish'} else (context,)
            result = call(*args)
            context = result.context

        completed = self.repository.get('repo', 'G-047')
        self.assertEqual(completed.status, 'PR_OPEN')
        self.assertIsNotNone(completed.definition_snapshot)
        self.assertIsNotNone(completed.worker_input_token)
        self.assertIsNotNone(completed.archive_token)
        self.assertIsNotNone(completed.verify_token)
        self.assertIsNotNone(completed.review_token)
        self.assertIsNotNone(completed.publication_token)

    def test_latest_base_uses_same_intent_and_rebase_for_both_checkpoints(self):
        for phase in ('VERIFYING', 'PUBLISHING'):
            with self.subTest(phase=phase):
                self.state.update_project('repo', phase=phase, active_sha='base', validated_base_sha='base')
                self.remote = 'new-base'
                self.calls.clear()
                context = self.context()
                if phase == 'VERIFYING':
                    result = self.handler.verify(context, self.handler)
                    self.assertEqual(result.context.phase, ExecutionPhase.REVIEWING)
                    self.assertEqual(self.calls.count('verify'), 2)
                else:
                    result = self.handler.publish(context, self.handler)
                    self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                    self.assertEqual(self.calls.count('verify'), 0)
                    self.assertIsNone(self.state.row()['review_result'])
                self.assertEqual(self.calls.count('rebase'), 1)
                self.assertEqual(self.state.row()['active_sha'], 'new-base')

    def test_stale_callback_cannot_update_same_phase_new_snapshot(self):
        context = self.context()
        self.repository.update_project('repo', {'worker_result': 'newer observation'})
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.state.update_project('repo', worker_session_id='stale')
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.handler.run(context)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.repository.get('repo', 'G-047').worker_session_id)

    def test_source_revision_invalidates_artifacts_but_preserves_sessions_and_workspace(self):
        guarded = self.handler.guard(self.context())
        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        snapshot = DefinitionSnapshot.deserialize(
            self.state.row()['definition_snapshot']
        )
        assert snapshot is not None
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_session_id='worker-session',
            reviewer_session_id='reviewer-session',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            worker_input_token=snapshot.worker_token,
            archive_token=snapshot.archive_token,
            verify_token=snapshot.verify_token('base'),
            review_token=snapshot.review_token('base'),
            publication_token=snapshot.publication_token('base'),
        )
        self.effects.revision.source_fingerprint.return_value = 'changed-source'

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['reviewer_session_id'], 'reviewer-session')
        self.assertEqual(row['branch'], 'loop-manager/G-047')
        self.assertEqual(row['worktree'], '/state/G-047')

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

    def test_working_source_change_clears_retry_artifacts_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            worker_result='previous worker result',
            verify_result='failed verify result',
            worker_input_token='stale-worker-token',
        )
        self.effects.revision.source_fingerprint.return_value = 'revised-source'

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, ExecutionOutcome)
        row = self.state.row()
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['worker_input_token'])

    def test_conflicted_rebase_waits_for_new_dependency_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.revision.unmet_dependencies.return_value = ('G-048',)

        outcome = ExecutionPhaseCoordinator(self.handler).dispatch(
            self.handler._context()
        )

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.STABLE_WAIT)
        self.assertIn('G-048', outcome.detail)
        self.effects.agent.run_agent.assert_not_called()
        self.assertNotIn('rebase', self.calls)
        row = self.state.row()
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['worker_result'], 'previous worker result')
        self.assertTrue(row['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
        self.effects.revision.unmet_dependencies.return_value = ()
        worker_outcome = ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        with mock.patch.object(self.handler, 'run', return_value=worker_outcome) as worker:
            resumed = ExecutionPhaseCoordinator(self.handler).dispatch(
                self.handler._context()
            )
        self.assertIs(resumed, worker_outcome)
        worker.assert_called_once()

    def test_conflicted_rebase_blocks_new_worker_input_before_worker(self):
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

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs['reason'],
            lm.BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
        )

    def test_conflict_resolution_rechecks_new_remote_base_before_verify(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.remote = 'new-base'
        guarded = self.handler.guard(self.handler._context())
        self.assertNotIsInstance(guarded, ExecutionOutcome)

        resolved = self.handler._finish_worker(
            guarded, self.state.row(), 'resolved worker result'
        )

        self.assertEqual(resolved.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['active_sha'], 'base')
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertEqual(self.calls.count('rebase'), 2)

    def test_revision_guard_adopts_legacy_phase_artifacts_without_worker_replay(self):
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            definition_snapshot=None,
            worker_input_token=None,
            archive_token=None,
            verify_token=None,
            review_token=None,
            publication_token=None,
        )

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        row = self.state.row()
        snapshot = DefinitionSnapshot.deserialize(row['definition_snapshot'])
        assert snapshot is not None
        self.assertEqual(row['phase'], 'PUBLISHING')
        self.assertEqual(row['worker_result'], 'worker-result')
        self.assertEqual(row['verify_result'], 'verify-result')
        self.assertEqual(row['review_result'], 'review-result')
        self.assertEqual(row['worker_input_token'], snapshot.worker_token)
        self.assertEqual(row['archive_token'], snapshot.archive_token)
        self.assertEqual(row['verify_token'], snapshot.verify_token('base'))
        self.assertEqual(row['review_token'], snapshot.review_token('base'))

    def test_revision_guard_retries_when_base_and_catalog_shas_disagree(self):
        self.effects.revision.reconcile_catalog.side_effect = None
        self.effects.revision.reconcile_catalog.return_value = SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha='other-base'
        )

        outcome = self.handler.guard(self.context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertIsNone(self.state.row()['definition_snapshot'])
        self.effects.revision.body_hash.assert_not_called()
        self.effects.revision.source_fingerprint.assert_not_called()

    def test_revision_guard_rejects_legacy_artifacts_after_source_change(self):
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            definition_snapshot=None,
            worker_input_token=None,
            archive_token=None,
            verify_token=None,
            review_token=None,
            publication_token=None,
        )
        self.remote = 'new-base'
        self.effects.revision.changed_paths.return_value = (
            'docs/PRODUCT.md',
        )

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])

    def test_successful_revision_refresh_clears_rebase_recovery_marker(self):
        guarded = self.handler.guard(self.context())
        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        self.remote = 'new-base'

        outcome = self.handler.guard(guarded)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertEqual(row['active_sha'], 'new-base')
        self.assertIsNone(row['revision_resume_status'])
        self.assertIsNone(row['last_error'])

    def test_revision_refresh_checkpoints_dirty_verified_worker_patch_before_rebase(self):
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result='worker-result'
        )
        self.remote = 'new-base'

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertLess(self.calls.index('commit'), self.calls.index('rebase'))
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertEqual(self.state.row()['worker_result'], 'worker-result')

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

    def test_verify_interruption_resumes_without_duplicate_commit(self):
        context = self.context('VERIFYING')
        original = self.git
        def interrupted(path, *args, **kwargs):
            if args[0] == 'fetch':
                raise RuntimeError('interruption')
            return original(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = interrupted
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.handler.verify(context, self.handler)
        self.effects.workspace.git.side_effect = original
        self.handler.verify(self.context(), self.handler)
        self.assertEqual(self.calls.count('commit'), 1)

    def test_protocol_correction_reuses_session_before_publication(self):
        self.effects.validation.parse_reviewer_result.side_effect = [
            lm.ReviewerProtocolError('malformed'), ('PASS', ())]
        result = self.handler.review(self.context('REVIEWING'))
        self.assertEqual(result.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(self.calls, ['reviewer', 'reviewer'])
        self.assertEqual(self.effects.agent.run_agent.call_args.kwargs['session_id'], 'session')

    def test_archive_change_returns_to_worker_before_push(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.archive.requirement_archive_reconciliation_error.return_value = 'archive changed'
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertNotIn('push', self.calls)

    def test_existing_pr_recovery_does_not_push_or_create(self):
        self.effects.publication.pr_for_branch.return_value = {'url': 'existing'}
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.calls, [])

    def test_worker_format_scope_and_guidance_stop_before_verify(self):
        scenarios = ('format', 'scope', 'guidance')
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.effects.validation.parse_worker_result.side_effect = None
                self.effects.agent.validate_worker_workspace_scope.side_effect = None
                self.effects.agent.inspect_worker_guidance_result.return_value = False
                self.state.update_project('repo', phase='WORKING')
                if scenario == 'format':
                    self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError('format')
                elif scenario == 'scope':
                    self.effects.agent.validate_worker_workspace_scope.side_effect = lm.AgentScopeError('scope')
                else:
                    self.effects.agent.inspect_worker_guidance_result.return_value = True
                result = self.handler.run(self.context())
                self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
                self.assertNotIn('verify', self.calls)

    def test_validation_retry_and_final_block_remain_distinct(self):
        for stage in ('verify', 'review'):
            for blocked in (False, True):
                with self.subTest(stage=stage, blocked=blocked):
                    self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(blocked=blocked)
                    self.effects.validation.verify.side_effect = lambda _: (False, 'diagnostics')
                    self.effects.validation.parse_reviewer_result.return_value = ('FAIL', ('issue',))
                    phase = 'VERIFYING' if stage == 'verify' else 'REVIEWING'
                    context = self.context(phase)
                    result = (self.handler.verify(context, self.handler) if stage == 'verify'
                              else self.handler.review(context))
                    self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK if blocked else ExecutionOutcomeKind.RETRY)

    def test_rebase_conflict_preserves_intent_for_both_checkpoints(self):
        self.remote = 'changed'
        original = self.git
        def conflict(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if args[0] == 'rebase':
                result.returncode = 1
                result.stdout = 'conflict details'
            return result
        self.effects.workspace.git.side_effect = conflict
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        for phase in ('VERIFYING', 'PUBLISHING'):
            with self.subTest(phase=phase):
                self.state.update_project('repo', phase=phase, active_sha='base', validated_base_sha='base')
                context = self.context()
                result = (self.handler.verify(context, self.handler) if phase == 'VERIFYING'
                          else self.handler.publish(context, self.handler))
                self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
                self.assertEqual(self.state.row()['phase'], 'WORKING')
                self.assertTrue(self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
                self.assertIsNone(self.state.row()['validated_base_sha'])

    def test_publication_interruption_recovers_existing_pr_without_repush(self):
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.command.side_effect = RuntimeError('interrupted response')
        with self.assertRaisesRegex(RuntimeError, 'interrupted response'):
            self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.effects.publication.pr_for_branch.return_value = {'url': 'created'}
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        self.handler.publish(self.context(), self.handler)
        self.assertEqual(self.calls.count('push'), 1)
        self.assertEqual(self.effects.publication.command.call_count, 1)

    def test_publication_uses_immutable_plan_link_with_reconciled_metadata(self):
        self.connection.execute(
            "UPDATE exec_plans SET title = 'Current Runtime', "
            "publication_path = 'docs/plans/[G-047] Original Runtime.md', "
            "publication_content_hash = 'original-hash', "
            "publication_sha = ? WHERE repo = 'repo' AND plan_id = 'G-047'",
            ("a" * 40,),
        )
        self.connection.commit()
        self.plan = {
            'id': 'G-047', 'title': 'Current Runtime',
            'sources': ['docs/PRODUCT.md'], 'depends_on': [],
            'complexity': 'low', 'risk': 'low', 'priority': 'normal',
        }
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')
        self.effects.publication.published_execplan.return_value = {
            **self.plan, 'title': 'Original Runtime',
        }
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        show = [
            call for call in self.effects.workspace.git.call_args_list
            if len(call.args) >= 3 and call.args[1] == 'show'
        ]
        self.assertEqual(
            show[-1].args[2],
            f"{'a' * 40}:docs/plans/[G-047] Original Runtime.md",
        )
        build = self.effects.publication.build_pr_body.call_args.args
        self.assertEqual(build[1]['title'], 'Current Runtime')
        self.assertEqual(
            build[2], 'docs/plans/[G-047] Original Runtime.md'
        )
        self.assertEqual(build[5], 'a' * 40)

    def test_workspace_violation_blocks_before_agent(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.validate_execplan_workspace_identity.side_effect = lm.WorkspaceOwnershipError('ownership')
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan_from_revision.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_guidance_mismatch_reads_blocked_plan_after_projection_release(self):
        self.connection.execute(lm.GUIDANCE_APPROVALS_SCHEMA)
        proposal = SimpleNamespace(
            proposal_id='guidance-change', line=10,
            decision=SimpleNamespace(line=20),
        )
        document = SimpleNamespace(proposals=(proposal,), structure_hash='structure')
        approval = {'applied': 0, 'proposal_id': proposal.proposal_id,
                    'target_path': 'AGENTS.md'}
        self.effects.agent.inspect_worker_guidance_result.side_effect = self.state.bind(
            lm.inspect_worker_guidance_result, connected=True, mutates=True,
        )
        context = self.context()
        with (
            mock.patch.object(lm, '_guidance_document', return_value=document),
            mock.patch.object(lm, '_guidance_rows', return_value=(approval,)),
            mock.patch.object(lm, '_validate_guidance_proposal_scope'),
            mock.patch.object(lm, 'is_loop_manager_repository', return_value=True),
            mock.patch.object(lm, '_guidance_document_matches_rows',
                              return_value=({proposal.proposal_id: proposal}, ())),
            mock.patch.object(lm, '_guidance_final_expected',
                              return_value=({('batch', 'AGENTS.md'): (b'approved', 0o100644)}, {})),
            mock.patch.object(lm, '_guidance_target_state',
                              return_value=(b'mismatched', 0o100644)),
            mock.patch.object(lm, 'capture_git_snapshot') as snapshot,
        ):
            snapshot.return_value.serialize.return_value = '{}'
            result = self.handler.run(context)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIsNone(self.repository.projected('repo'))
        self.assertIsNone(self.state.expected)
        blocked = self.repository.get('repo', 'G-047')
        self.assertEqual(blocked.status, 'PLAN_BLOCKED')
        self.assertIn('실제 AGENTS.md 결과가 승인된 patch 결과와 다릅니다.', blocked.last_error)
        self.assertEqual(self.state.blocked()['last_error'], blocked.last_error)
        self.assertEqual(
            self.effects.events.operation_print.call_args.args[0], blocked.last_error,
        )
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.handler.run(context)
