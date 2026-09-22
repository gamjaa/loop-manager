"""Run the real phase policies with in-memory state and fake external effects."""
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import execution_handlers as handlers
import loop_manager as lm
import runtime_state
from execution_state import ExecutionStatePort
from execution_runtime import ExecutionPhase, ExecutionOutcomeKind
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
        self.handler = handlers.ExecutionHandlers(
            self.effects, repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')

    def git(self, path, *args, **kwargs):
        self.calls.append(args[0])
        if args[0] == 'rebase':
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

    def test_workspace_violation_blocks_before_agent(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.validate_execplan_workspace_identity.side_effect = lm.WorkspaceOwnershipError('ownership')
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan_from_revision.assert_called_once()
        self.assertEqual(self.calls, [])
