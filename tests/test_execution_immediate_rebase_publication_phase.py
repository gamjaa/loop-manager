"""Immediate Rebase Publication Phase regression tests."""

from tests.support.current_rebase import current_rebase_case
from dataclasses import replace
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.execution.execution_runtime import ExecutionPhase




class ImmediateRebasePublicationTests(TestCase):
    """Only fake external effects; test real acceptance, intents and runtime CAS."""
    def setUp(self):
        self._reset_publication_case()

    def _reset_publication_case(self):
        self.seed = current_rebase_case(self)
        self.seed._prepare_current_checkpoint_recovery()
        self.handler = self.seed.handler
        self.handler._publish_rebased_checkpoint = handlers.ExecutionHandlers._publish_rebased_checkpoint.__get__(self.handler)
        self.state, self.checkpoints = self.seed.state, self.seed.checkpoint
        self.fixture = self.seed.fixture
        self.remote = 'remote'
        live = mock.patch.object(handlers.manager_git_journal, 'live_remote_branch_oid',
                                 side_effect=lambda *_: self.remote)
        live.start()
        self.addCleanup(live.stop)
        self.pushes = []
        self.events = []
        self.push_failure = False
        self.before_push = None
        self.handler.effects.workspace.remote_branch_oid.side_effect = lambda *_: self.remote
        self.handler.effects.workspace.git.side_effect = self.git
        factory = self.seed.factory
        self.handler.effects = replace(self.handler.effects, workspace=replace(
            self.handler.effects.workspace, current_rebase_service=lambda context, **options:
            factory(context, **{'remote_oid': self.state.row()['publication_remote_oid'] or self.seed.expected.remote.oid, **options})))

    def git(self, path, *args, **kwargs):
        if args[0] == 'ls-remote':
            return SimpleNamespace(returncode=0, stdout=(
                f'{self.remote}\trefs/heads/loop-manager/G-047\n' if self.remote else ''))
        if args[0] == 'push':
            self.events.append('push')
            self.pushes.append(args)
            if self.before_push:
                self.before_push()
            if self.push_failure:
                return SimpleNamespace(returncode=1, stdout='permission denied')
            self.remote = 'owned-head'
            pr = self.seed.expected.remote.pull_request
            self.seed.observed = replace(self.seed.observed,
                remote=replace(self.seed.expected.remote, oid=self.remote,
                               pull_request=replace(pr, head=self.remote) if pr else None))
            return SimpleNamespace(returncode=0, stdout='pushed')
        return self.fixture.observed_git(path, *args, **kwargs)

    def complete(self):
        self.handler._complete_rebase_intent(self.handler._context())

    def restart(self):
        self.state = ExecutionStatePort(self.fixture.connection,
            runtime_state.RuntimeStateRepository(self.fixture.connection), 'repo')
        self.handler.effects = replace(self.handler.effects, state=self.state)
        replacement = handlers.ExecutionHandlers(self.handler.effects,
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'), base='main',
            base_sha='later-base', provider='codex', config_path=Path('/config'), newly_activated=False)
        replacement.plan = self.handler.plan
        replacement.catalog_plan = self.handler.catalog_plan
        self.handler = replacement

    def direct_ports(self):
        def current_service(*_args, **options):
            options.setdefault('remote_oid', self.remote)
            service = self.seed.factory(None, **options)
            pr = service.expected.remote.pull_request
            if pr is not None:
                service = replace(service, expected=replace(service.expected,
                    remote=replace(service.expected.remote,
                                   pull_request=replace(pr, head=service.expected.remote.oid))))
            return service
        stack = ExitStack()
        for patch in (
            mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(lm, '_current_rebase_service', side_effect=current_service),
            mock.patch.object(lm, 'git', side_effect=self.git),
            mock.patch.object(lm, 'remote_branch_oid', side_effect=lambda *_: self.remote),
            mock.patch.object(lm, 'pr_for_branch', side_effect=self.handler.effects.publication.pr_for_branch),
        ):
            stack.enter_context(patch)
        return stack

    def test_bound_direct_rebase_retry_keeps_adoption_and_same_publication_intent(self):
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        self._existing_pr()
        self.push_failure = True
        complete = self.state.bind(lm._complete_direct_rebase_checkpoint, connected=True, mutates=True)
        arguments = ('repo', 'G-047', Path('/state/G-047').resolve())
        options = dict(target_base_oid=self.seed.expected.target_base_oid,
                       repo_path=Path('/repo'), branch='loop-manager/G-047',
                       agent_session_id=self.state.row()['worker_session_id'])
        with self.direct_ports():
            with self.assertRaises(RecoveryFailure) as caught:
                complete(*arguments, **options)
            self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
            pending = self.checkpoints.get('repo', 'G-047')
            self.assertEqual(pending.operation, 'rebase_publish')
            self.assertEqual(self.state.check().status, 'IMPLEMENTING')
            self.assertEqual(self.state.row()['publication_remote_oid'], 'remote')
            self.assertIsNone(self.state.row()['verify_result'])
            self.push_failure = False
            complete(*arguments, **options)
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        self.assertEqual(self.state.row()['pr_head_oid'], 'owned-head')
        self.assertIsNone(self.state.row()['review_token'])
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                         pending.snapshot['rebase_publication_intent'])
        self.assertEqual(len(self.pushes), 2)
        self.assertEqual(self.pushes[0], self.pushes[1])

    def _existing_pr(self):
        from loop_manager_internal.git.managed_rebase_validation import PullRequest
        pr = PullRequest('https://example.test/pr/47', 'repo', 'loop-manager/G-047', 'remote', 'OPEN')
        self.seed.expected = replace(self.seed.expected,
            remote=replace(self.seed.expected.remote, pull_request=pr))
        self.seed.observed = replace(self.seed.observed, remote=self.seed.expected.remote)
        self.state.update_project('repo', pr_url=pr.identity, pr_head_oid='remote',
            verify_token='old-verify', review_token='old-review', publication_token='old-publication')
        factory = self.handler.effects.workspace.current_rebase_service
        def current_pr_service(context, **options):
            service = factory(context, **options)
            return replace(service, expected=replace(service.expected,
                remote=replace(service.expected.remote,
                    pull_request=replace(pr, head=service.expected.remote.oid))))
        self.handler.effects = replace(self.handler.effects, workspace=replace(
            self.handler.effects.workspace, current_rebase_service=current_pr_service))
        self.pr_changes = {}
        self.handler.effects.publication.pr_for_branch.side_effect = lambda *_: {
            'url': pr.identity, 'state': 'OPEN', 'headRefName': pr.branch,
            'headRefOid': self.remote, 'baseRefName': 'main', **self.pr_changes}

    def test_existing_pr_checkpoint_uses_exact_lease_and_invalidates_approval(self):
        self._existing_pr()
        self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['rebase_publication_intent']['acceptance']['remote']['pull_request']['identity'],
                         'https://example.test/pr/47')
        self.assertEqual(self.state.row()['pr_head_oid'], 'owned-head')
        project = self.fixture.connection.execute("SELECT * FROM projects WHERE repo='repo'").fetchone()
        self.assertEqual(project['pr_head_oid'], 'owned-head')
        self.assertIsNone(project['verify_token'])
        self.assertIsNone(project['review_token'])
        for field in ('verify_token', 'review_token', 'publication_token', 'validated_base_sha',
                      'worker_result', 'verify_result', 'review_result'):
            self.assertIsNone(self.state.row()[field])
        self.assertEqual(self.pushes, [('push', '--force-with-lease=refs/heads/loop-manager/G-047:remote',
                                       'origin', 'owned-head:refs/heads/loop-manager/G-047')])
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(len(self.pushes), 1)

    def test_existing_pr_push_before_sqlite_confirmation_resumes_without_duplicate_push(self):
        self._existing_pr()
        class Exit(BaseException):
            pass
        with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
            with self.assertRaises(Exit):
                self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(pending.operation, 'rebase_publish')
        self.assertEqual(self.state.row()['pr_head_oid'], 'remote')
        self.restart()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.state.row()['pr_head_oid'], 'owned-head')
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                         pending.snapshot['rebase_publication_intent'])

    def _restart_through_project_entry(self):
        """Run the real project entry/PR recovery with unrelated preflight isolated."""
        from loop_manager_internal.application.project_service import ProjectService
        self.restart()
        events = []
        def prepare(service):
            self.fixture.connection.execute("UPDATE projects SET planning_attempt_fingerprint = NULL")
            service.state.repo, service.state.repo_path = 'repo', Path('/repo')
            service.state.row = self.fixture.connection.execute("SELECT * FROM projects WHERE repo='repo'").fetchone()
        def next_work(service):
            events.append('candidate-work')
            fresh = runtime_state.RuntimeStateRepository(self.fixture.connection).projected('repo')
            self.assertEqual(fresh.status, 'IMPLEMENTING')
            self.assertEqual(fresh.pr_head_oid, 'owned-head')
            self.assertEqual(fresh.pr_conflict_head_oid, 'owned-head')
            self.assertIsNone(fresh.verify_token)
            self.assertIsNone(fresh.review_token)
            return 0
        actual_validate = lm._validate_active_pr_conflict_recovery
        def validate(*args, **kwargs):
            events.append('PR-ownership')
            return actual_validate(*args, **kwargs)
        with self.direct_ports(), ExitStack() as ports:
            block = ports.enter_context(mock.patch.object(lm, 'block_active_execplan_from_revision'))
            for patch in (
                mock.patch.object(ProjectService, '_prepare_repository', autospec=True, side_effect=prepare),
                mock.patch.object(ProjectService, '_reconcile_catalog_and_cleanup', return_value=None),
                mock.patch.object(ProjectService, '_synchronize_pull_requests', return_value=None),
                mock.patch.object(ProjectService, '_run_coordinators', autospec=True, side_effect=next_work),
                mock.patch.object(lm, '_project_pr_merge_confirmed', return_value=False),
                mock.patch.object(lm, '_fetch_pr_conflict_recovery_refs'),
                mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                mock.patch.object(lm, 'make_worktree'),
                mock.patch.object(lm, '_validate_active_pr_conflict_recovery', side_effect=validate),
                mock.patch.object(lm, 'operation_print'),
            ):
                ports.enter_context(patch)
            result = lm._run_project_step(self.fixture.connection, Path('/state'), 'repo',
                                          Path('/config'), 'main', False)
        return result, events, block

    def test_project_entry_confirms_pushed_pr_checkpoint_before_old_head_validation(self):
        self._existing_pr()
        self.state.update_project('repo', pr_conflict_recovery=1,
            pr_conflict_head_oid='remote', pr_conflict_base_oid='base')
        class Exit(BaseException):
            pass
        with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
            with self.assertRaises(Exit):
                self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(self.state.row()['pr_conflict_head_oid'], 'remote')
        result, events, block = self._restart_through_project_entry()
        self.assertEqual(result, 0)
        self.assertEqual(events, ['PR-ownership', 'candidate-work'])
        block.assert_not_called()
        self.assertEqual(len(self.pushes), 1)
        self.assertIsNone(self.checkpoints.get('repo', 'G-047').operation)
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                         pending.snapshot['rebase_publication_intent'])

    def test_project_entry_refuses_changed_pr_after_checkpoint_push_before_next_work(self):
        self._existing_pr()
        self.state.update_project('repo', pr_conflict_recovery=1,
            pr_conflict_head_oid='remote', pr_conflict_base_oid='base')
        class Exit(BaseException):
            pass
        with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
            with self.assertRaises(Exit):
                self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        for changes in ({'url': 'different-pr'}, {'headRefOid': 'user-head'},
                        {'state': 'CLOSED', 'url': 'different-pr'},
                        {'state': 'MERGED', 'headRefName': 'other-branch'},
                        {'state': 'CLOSED', 'baseRefName': 'other-base'}):
            with self.subTest(changes=changes):
                self.pr_changes = changes
                result, events, block = self._restart_through_project_entry()
                self.assertEqual(result, 1)
                self.assertEqual(events, [])
                block.assert_called_once()
                self.assertEqual(len(self.pushes), 1)
                self.assertEqual(self.state.row()['pr_conflict_head_oid'], 'remote')
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)

    def _interrupt_pr_checkpoint(self, *, pushed):
        self._existing_pr()
        self.state.update_project('repo', pr_conflict_recovery=1,
            pr_conflict_head_oid='remote', pr_conflict_base_oid='base')
        if pushed:
            with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.complete()
        else:
            self.push_failure = True
            from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
            with self.assertRaises(RecoveryFailure):
                self.complete()
            self.push_failure = False
        return self.checkpoints.get('repo', 'G-047')

    def _terminal_project_step(self, next_status):
        """Keep project entry, waiting PRs, blocking and Merge transitions real."""
        from loop_manager_internal.application.project_service import ProjectService
        def prepare(service):
            self.fixture.connection.execute('UPDATE projects SET planning_attempt_fingerprint = NULL')
            service.state.repo, service.state.repo_path = 'repo', Path('/repo')
            service.state.base = 'main'
            service.state.row = self.fixture.connection.execute('SELECT * FROM projects').fetchone()
        def synchronize(service):
            result = lm.synchronize_waiting_pull_requests(
                self.fixture.connection, Path('/repo'), Path('/state'), 'repo')
            service.state.row = self.fixture.connection.execute('SELECT * FROM projects').fetchone()
            return result.step_result or None
        def next_work(_service):
            plan = runtime_state.RuntimeStateRepository(self.fixture.connection).get('repo', 'G-047')
            self.assertEqual(plan.status, next_status)
            if next_status == 'IMPLEMENTING':
                from loop_manager_internal.execution.execution_runtime import (
                    ExecutionOutcome, ExecutionOutcomeKind, ExecutionPhaseCoordinator)
                self.assertIsNone(self.checkpoints.get('repo', 'G-047').operation)
                self.assertEqual(plan.pr_head_oid, 'owned-head')
                self.assertIsNone(plan.worker_result)
                self.assertIsNone(plan.worker_input_token)
                self.assertIsNone(plan.verify_token)
                self.assertIsNone(plan.review_token)
                self.assertIsNone(plan.publication_token)
                self.restart()
                context = self.handler._context()
                execution_ports = mock.Mock()
                execution_ports.guard.side_effect = lambda context: context
                execution_ports.run.return_value = ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)
                ExecutionPhaseCoordinator(execution_ports).dispatch(context)
                execution_ports.run.assert_called_once_with(context)
                execution_ports.verify.assert_not_called()
                execution_ports.review.assert_not_called()
                execution_ports.publish.assert_not_called()
            return 0
        with self.direct_ports(), ExitStack() as ports:
            for patch in (
                mock.patch.object(ProjectService, '_prepare_repository', autospec=True, side_effect=prepare),
                mock.patch.object(ProjectService, '_reconcile_catalog_and_cleanup', return_value=None),
                mock.patch.object(ProjectService, '_synchronize_pull_requests', autospec=True, side_effect=synchronize),
                mock.patch.object(ProjectService, '_run_coordinators', autospec=True, side_effect=next_work),
                mock.patch.object(lm, 'remote_base', return_value='base'),
                mock.patch.object(lm, '_recorded_base_fetch', return_value='base'),
                mock.patch.object(lm, '_fetch_pr_conflict_recovery_refs'),
                mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                mock.patch.object(lm, 'make_worktree'),
                mock.patch.object(lm, 'classify_pull_request_mergeability', return_value='mergeable'),
                mock.patch.object(lm, '_revision_execplan_input_fingerprint', return_value='input'),
                mock.patch.object(lm, '_assess_missing_managed_cleanup',
                                  return_value=lm.CleanupAssessment('deferred', 'preserved workspace')),
                mock.patch.object(lm, 'operation_print'),
            ):
                ports.enter_context(patch)
            return lm._run_project_step(self.fixture.connection, Path('/state'), 'repo',
                                        Path('/config'), 'main', False)

    def test_pending_checkpoint_closed_pr_blocks_only_plan_and_same_pr_reopens(self):
        from loop_manager_internal.recovery import scoped_blocking
        for timing in ('before-push', 'after-push', 'during-push'):
            with self.subTest(timing=timing):
                self._reset_publication_case()
                pending = self._interrupt_pr_checkpoint(pushed=timing == 'after-push')
                if timing == 'during-push':
                    self.before_push = lambda: self.pr_changes.update(state='CLOSED')
                else:
                    self.pr_changes = {'state': 'CLOSED'}
                self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
                runtime = runtime_state.RuntimeStateRepository(self.fixture.connection)
                closed = runtime.get('repo', 'G-047')
                self.assertEqual(closed.status, 'PLAN_BLOCKED')
                self.assertEqual(scoped_blocking.plan_block(
                    self.fixture.connection, 'repo', 'G-047')['reason_code'], lm.PR_CLOSED_BLOCK_REASON)
                self.assertEqual(closed.pr_url, self.seed.expected.remote.pull_request.identity)
                self.assertEqual(closed.pr_head_oid, self.remote)
                self.assertIsNotNone(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047'))
                self.assertIsNone(scoped_blocking.project_block(self.fixture.connection, 'repo'))
                self.assertIsNone(runtime.projected('repo'))
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                self.assertEqual(len(self.pushes), 2 if timing == 'during-push' else 1)
                self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 0)
                self.pr_changes = {}
                self.assertEqual(self._terminal_project_step('IMPLEMENTING'), 0)
                reopened = runtime.get('repo', 'G-047')
                self.assertEqual(reopened.status, 'IMPLEMENTING')
                self.assertEqual(reopened.pr_url, closed.pr_url)
                self.assertEqual(reopened.pr_head_oid, 'owned-head')
                self.assertEqual(reopened.worker_session_id, closed.worker_session_id)
                self.assertIsNone(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047'))
                self.assertIsNone(scoped_blocking.project_block(self.fixture.connection, 'repo'))
                confirmed = self.checkpoints.get('repo', 'G-047')
                self.assertIsNone(confirmed.operation)
                self.assertEqual(confirmed.snapshot['rebase_publication_intent'],
                                 pending.snapshot['rebase_publication_intent'])
                self.assertEqual(confirmed.snapshot['rebase_publication_result']['head'], 'owned-head')
                self.assertEqual(len(self.pushes), 1 if timing == 'after-push' else 2)

    def test_confirmed_checkpoint_pr_close_preserves_active_candidate_revalidation(self):
        self._existing_pr()
        self.complete()
        confirmed = self.checkpoints.get('repo', 'G-047')
        self.pr_changes = {'state': 'CLOSED'}
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
        runtime_state.RuntimeStateRepository(self.fixture.connection).hydrate('repo', 'G-047')
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
        self.pr_changes = {}
        self.assertEqual(self._terminal_project_step('IMPLEMENTING'), 0)
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), confirmed)
        self.assertEqual(len(self.pushes), 1)

    def _approve_current_pr_candidate(self):
        from tests.support.publication import seed_validated_candidate
        self._existing_pr()
        self.complete()
        with (mock.patch('tests.support.publication.git_command', side_effect=['base', 'owned-head']),
              mock.patch.object(lm, '_revision_execplan_body_hash', return_value='body'),
              mock.patch.object(lm, '_revision_execplan_source_fingerprint', return_value='sources')):
            approval = seed_validated_candidate(self.fixture.connection, Path('repo'),
                Path('/state/G-047'), 'G-047')
        lm.update_project(self.fixture.connection, 'repo', phase='PUBLISHING',
            active_sha='base', validated_base_sha='base', pr_conflict_recovery=0,
            publication_token=approval.publication_token('base', 'owned-head'))
        self.restart()
        return approval

    def test_approved_final_candidate_closed_pr_reopens_into_merge_wait(self):
        from loop_manager_internal.recovery import scoped_blocking
        approval = self._approve_current_pr_candidate()
        runtime = runtime_state.RuntimeStateRepository(self.fixture.connection)
        confirmed = self.checkpoints.get('repo', 'G-047')
        self.pr_changes = {'state': 'CLOSED'}
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
        closed = runtime.get('repo', 'G-047')
        self.assertEqual(closed.status, 'PLAN_BLOCKED')
        self.assertEqual(closed.publication_token, approval.publication_token('base', 'owned-head'))
        self.assertIsNone(scoped_blocking.project_block(self.fixture.connection, 'repo'))
        self.pr_changes = {}
        for _ in range(2):
            self.assertEqual(self._terminal_project_step('PR_OPEN'), 0)
            reopened = runtime.get('repo', 'G-047')
            self.assertEqual(reopened.status, 'PR_OPEN')
            self.assertEqual(reopened.pr_url, closed.pr_url)
            self.assertEqual(reopened.pr_head_oid, closed.pr_head_oid)
            self.assertEqual(reopened.publication_token, closed.publication_token)
        self.assertIsNone(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047'))
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), confirmed)
        self.assertEqual(len(self.pushes), 1)

    def test_stale_final_approval_closed_pr_reopens_through_worker_revalidation(self):
        approval = self._approve_current_pr_candidate()
        self.state.update_project('repo', publication_token=approval.publication_token('base', 'previous-head'))
        confirmed = self.checkpoints.get('repo', 'G-047')
        self.pr_changes = {'state': 'CLOSED'}
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
        self.pr_changes = {}
        self.assertEqual(self._terminal_project_step('IMPLEMENTING'), 0)
        reopened = runtime_state.RuntimeStateRepository(self.fixture.connection).get('repo', 'G-047')
        self.assertEqual(reopened.status, 'IMPLEMENTING')
        self.assertIsNone(reopened.publication_token)
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), confirmed)
        self.assertEqual(len(self.pushes), 1)

    def test_confirmed_checkpoint_keeps_validation_stages_active_without_final_approval(self):
        for phase in ('WORKING', 'VERIFYING', 'REVIEWING', 'PUBLISHING'):
            with self.subTest(phase=phase):
                self._reset_publication_case()
                self._existing_pr()
                self.complete()
                self.state.update_project('repo', phase=phase)
                expected = 'IMPLEMENTING' if phase == 'WORKING' else phase
                self.assertEqual(self._terminal_project_step(expected), 0)
                self.assertEqual(runtime_state.RuntimeStateRepository(
                    self.fixture.connection).projected('repo').status, expected)
                self.assertEqual(len(self.pushes), 1)

    def test_changed_pr_after_confirmed_checkpoint_is_blocked_before_candidate_work(self):
        from loop_manager_internal.recovery import scoped_blocking
        for changes in ({'headRefOid': 'user-head'}, {'url': 'different-pr'},
                        {'headRefName': 'other-branch'}, {'baseRefName': 'other-base'}):
            with self.subTest(changes=changes):
                self._reset_publication_case()
                self._existing_pr()
                self.complete()
                confirmed = self.checkpoints.get('repo', 'G-047')
                self.pr_changes = changes
                self.assertEqual(self._terminal_project_step('unreachable'), 1)
                project_block = scoped_blocking.project_block(self.fixture.connection, 'repo')
                self.assertEqual(project_block['reason_code'], 'publication_ownership_uncertain')
                self.assertEqual(runtime_state.RuntimeStateRepository(
                    self.fixture.connection).get('repo', 'G-047').pr_head_oid, 'owned-head')
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), confirmed)
                self.assertEqual(len(self.pushes), 1)

    def test_pending_rebase_or_publication_cannot_recover_as_merge_wait(self):
        for operation in ('rebase', 'rebase_publish'):
            with self.subTest(operation=operation):
                self._reset_publication_case()
                if operation == 'rebase_publish':
                    pending = self._interrupt_pr_checkpoint(pushed=False)
                    self.state.update_project('repo', pr_conflict_recovery=0)
                else:
                    self._existing_pr()
                    pending = self.checkpoints.get('repo', 'G-047')
                project = self.fixture.connection.execute('SELECT * FROM projects').fetchone()
                pull_request = self.handler.effects.publication.pr_for_branch(Path('/repo'), project['branch'], project['pr_url'])
                with (self.direct_ports(),
                      mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                      mock.patch.object(lm, 'make_worktree'),
                      mock.patch.object(lm, 'operation_print')):
                    self.assertIsNone(lm.reconcile_existing_pull_request(
                        self.fixture.connection, Path('/repo'), Path('/state'), 'repo', project, pull_request))
                self.assertEqual(runtime_state.RuntimeStateRepository(
                    self.fixture.connection).projected('repo').status, 'IMPLEMENTING')
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)

    def test_legacy_closed_pr_with_pending_checkpoint_resumes_worker_revalidation(self):
        pending = self._interrupt_pr_checkpoint(pushed=True)
        self.pr_changes = {'state': 'CLOSED'}
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
        self.fixture.connection.execute("UPDATE recovery_records SET phase = 'PR_OPEN' WHERE subject = 'G-047'")
        self.fixture.connection.commit()
        self.pr_changes = {}
        self.assertEqual(self._terminal_project_step('IMPLEMENTING'), 0)
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                         pending.snapshot['rebase_publication_intent'])
        self.assertEqual(len(self.pushes), 1)

    def test_reopened_pending_candidate_waits_for_other_execution_slot(self):
        from loop_manager_internal.recovery import scoped_blocking
        pending = self._interrupt_pr_checkpoint(pushed=True)
        self.pr_changes = {'state': 'CLOSED'}
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
        self.fixture.connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "content_hash, priority, status, branch, worktree, workspace_owned, ever_started) "
            "VALUES ('repo', 'G-048', 'docs/plans/[G-048] Other.md', 'Other', '[]', '[]', "
            "'other', 'normal', 'IMPLEMENTING', 'loop-manager/G-048', '/state/G-048', 1, 1)")
        runtime = runtime_state.RuntimeStateRepository(self.fixture.connection)
        runtime.hydrate('repo', 'G-048')
        self.fixture.connection.commit()
        other = runtime.projected('repo')
        project = dict(self.fixture.connection.execute('SELECT * FROM projects').fetchone())
        self.pr_changes = {}
        with self.direct_ports(), mock.patch.object(lm, 'classify_pull_request_mergeability', return_value='mergeable'):
            lm.synchronize_waiting_pull_requests(self.fixture.connection, Path('/repo'), Path('/state'), 'repo')
        self.assertEqual(runtime.projected('repo'), other)
        self.assertEqual(dict(self.fixture.connection.execute('SELECT * FROM projects').fetchone()), project)
        self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
        self.assertIsNotNone(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047'))
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
        self.assertEqual(len(self.pushes), 1)

    def test_pending_checkpoint_merged_pr_records_merge_and_preserves_workspace(self):
        from loop_manager_internal.recovery import scoped_blocking
        for timing in ('before-push', 'after-push', 'during-push'):
            with self.subTest(timing=timing):
                self._reset_publication_case()
                pending = self._interrupt_pr_checkpoint(pushed=timing == 'after-push')
                if timing == 'during-push':
                    self.before_push = lambda: self.pr_changes.update(state='MERGED')
                else:
                    self.pr_changes = {'state': 'MERGED'}
                self.assertEqual(self._terminal_project_step('MERGED'), lm.PROJECT_STEP_PLANNING_PENDING)
                runtime = runtime_state.RuntimeStateRepository(self.fixture.connection)
                merged = runtime.get('repo', 'G-047')
                self.assertEqual(merged.status, 'MERGED')
                self.assertEqual(merged.pr_head_oid, self.remote)
                self.assertEqual(merged.worker_session_id, 'saved-worker')
                self.assertIsNone(runtime.projected('repo'))
                self.assertIsNone(scoped_blocking.project_block(self.fixture.connection, 'repo'))
                cleanup = self.fixture.connection.execute('SELECT * FROM cleanup_pending').fetchone()
                self.assertEqual(cleanup['worktree'], merged.worktree)
                self.assertEqual(cleanup['pr_url'], merged.pr_url)
                self.assertEqual(cleanup['pr_head_oid'], merged.pr_head_oid)
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                self.assertEqual(self._terminal_project_step('MERGED'), 0)
                self.assertEqual(len(self.pushes), 2 if timing == 'during-push' else 1)

    def test_pending_checkpoint_pr_close_and_reopen_preserve_independent_plan_block(self):
        from loop_manager_internal.recovery import scoped_blocking
        pending = self._interrupt_pr_checkpoint(pushed=True)
        runtime = runtime_state.RuntimeStateRepository(self.fixture.connection)
        runtime.block_plan('repo', 'G-047', reason_code='verify_retry_exhausted',
                           evidence='independent verify cause', release_fingerprint='input')
        runtime.hydrate('repo', 'G-047')
        original = dict(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047'))
        self.pr_changes = {'state': 'CLOSED'}
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
        self.assertEqual(dict(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047')), original)
        self.assertIsNone(scoped_blocking.project_block(self.fixture.connection, 'repo'))
        self.pr_changes = {}
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)
        self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
        self.assertEqual(dict(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047')), original)
        self.assertIsNone(scoped_blocking.project_block(self.fixture.connection, 'repo'))
        self.assertIsNone(self.checkpoints.get('repo', 'G-047').operation)
        self.assertEqual(len(self.pushes), 1)

    def test_pending_checkpoint_terminal_observation_failure_retries_without_project_block(self):
        from loop_manager_internal.recovery import scoped_blocking
        pending = self._interrupt_pr_checkpoint(pushed=True)
        observed = self.handler.effects.publication.pr_for_branch.side_effect
        reads = 0
        def lookup(*args):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise OSError('GitHub temporarily unavailable')
            return observed(*args)
        self.pr_changes = {'state': 'CLOSED'}
        self.handler.effects.publication.pr_for_branch.side_effect = lookup
        self.assertEqual(self._terminal_project_step('IMPLEMENTING'), lm.PROJECT_STEP_RETRY)
        self.assertEqual(runtime_state.RuntimeStateRepository(self.fixture.connection).get(
            'repo', 'G-047').status, 'IMPLEMENTING')
        self.assertIsNone(scoped_blocking.project_block(self.fixture.connection, 'repo'))
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
        self.assertEqual(self._terminal_project_step('PLAN_BLOCKED'), 1)

    def test_project_entry_retries_same_pending_pr_publication_before_candidate_work(self):
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
        self._existing_pr()
        self.state.update_project('repo', pr_conflict_recovery=1,
            pr_conflict_head_oid='remote', pr_conflict_base_oid='base')
        self.push_failure = True
        with self.assertRaises(RecoveryFailure):
            self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        result, events, block = self._restart_through_project_entry()
        self.assertEqual(result, lm.PROJECT_STEP_RETRY)
        self.assertEqual(events, [])
        block.assert_not_called()
        self.assertEqual(self.state.row()['pr_conflict_head_oid'], 'remote')
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
        self.push_failure = False
        result, events, block = self._restart_through_project_entry()
        self.assertEqual(result, 0)
        self.assertEqual(events, ['PR-ownership', 'candidate-work'])
        block.assert_not_called()
        self.assertEqual(len(self.pushes), 3)
        self.assertEqual(self.pushes, [self.pushes[0]] * 3)
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                         pending.snapshot['rebase_publication_intent'])

    def test_existing_pr_close_merge_identity_and_head_changes_preserve_the_lease(self):
        for changes in ({'state': 'CLOSED'}, {'state': 'MERGED'}, {'url': 'other-pr'},
                        {'headRefName': 'other-branch'}, {'headRefOid': 'user-head'}):
            with self.subTest(changes=changes):
                self._reset_publication_case()
                self._existing_pr()
                class Exit(BaseException):
                    pass
                self.before_push = lambda: (_ for _ in ()).throw(Exit())
                with self.assertRaises(Exit):
                    self.complete()
                pending = self.checkpoints.get('repo', 'G-047')
                attempts = len(self.pushes)
                self.before_push = None
                self.pr_changes = changes
                with self.assertRaises(RuntimeError):
                    self.handler._publish_rebased_checkpoint(self.handler._context())
                self.assertEqual(len(self.pushes), attempts)
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                self.assertEqual(self.remote, 'remote')

    def test_pending_publication_passes_persisted_agent_scope_before_handler_recovery(self):
        class Exit(BaseException):
            pass
        for stop in ('before-push', 'after-push'):
            with self.subTest(stop=stop):
                self._reset_publication_case()
                self.before_push = (lambda: (_ for _ in ()).throw(Exit())) if stop == 'before-push' else None
                with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
                    with self.assertRaises(Exit):
                        self.complete()
                pending = self.checkpoints.get('repo', 'G-047')
                branch, worktree = 'loop-manager/G-047', str(Path('/state/G-047').resolve())
                ref, remote_ref = f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'
                original = lm.GitSnapshot('before', branch, {}, {},
                    {ref: ('before', ''), remote_ref: ('remote', ''), 'refs/heads/other': ('other', '')},
                    {str(Path(worktree).resolve()): ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
                current = replace(original, head='owned-head', refs={**original.refs,
                    ref: ('owned-head', ''), remote_ref: (self.remote, '')},
                    worktrees={str(Path(worktree).resolve()): ('HEAD=owned-head', f'branch=refs/heads/{branch}')})
                self.state.update_project('repo', agent_role='worker',
                    agent_snapshot=original.serialize(), agent_result_snapshot=original.serialize())
                with self.direct_ports(), ExitStack() as ports:
                    capture = ports.enter_context(mock.patch.object(lm, 'capture_git_snapshot', return_value=current))
                    for patch in (
                        mock.patch.object(lm, '_config_entries', return_value={}),
                        mock.patch.object(lm, '_proven_merged_cleanup_paths', return_value=frozenset()),
                        mock.patch.object(Path, 'is_dir', return_value=True),
                        mock.patch.object(lm, 'git_bytes', return_value=b''),
                    ):
                        ports.enter_context(patch)
                    self.assertEqual(lm.validate_persisted_agent_scope(
                        self.fixture.connection, Path('/repo'), Path('/state'), 'repo'), current)
                    self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                    # Normalization never absorbs a different shared ref.
                    capture.return_value = replace(current, refs={**current.refs,
                        'refs/heads/other': ('foreign', '')})
                    with self.assertRaises(lm.AgentScopeError):
                        lm.validate_persisted_agent_scope(
                            self.fixture.connection, Path('/repo'), Path('/state'), 'repo')
                self.assertEqual(self.fixture.connection.execute(
                    "SELECT agent_snapshot FROM projects WHERE repo='repo'").fetchone()[0], original.serialize())
                self.before_push = None
                self.restart()
                self.handler._publish_rebased_checkpoint(self.handler._context())
                self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                                 pending.snapshot['rebase_publication_intent'])
                self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
                self.assertEqual(len(self.pushes), 2 if stop == 'before-push' else 1)

    def test_pending_publication_service_accepts_only_saved_lease_or_pushed_head(self):
        class Exit(BaseException):
            pass
        self.before_push = lambda: (_ for _ in ()).throw(Exit())
        with self.assertRaises(Exit):
            self.complete()
        from tests.support.revision import definition
        self.state.update_project('repo', definition_snapshot=definition(
            path='docs/plans/[G-047] Runtime.md').serialize())
        with ExitStack() as ports:
            for patch in (
                mock.patch.object(lm, '_revision_execplan_input_fingerprint', return_value='inputs'),
                mock.patch.object(lm, '_worktree_execplan_input_fingerprint', return_value='inputs'),
                mock.patch.object(lm, 'requirement_archive_candidates', return_value=()),
                mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False),
                mock.patch.object(lm, 'validate_active_execplan_metadata'),
                mock.patch.object(lm, '_owned_rebase_metadata', return_value=None),
                mock.patch.object(lm, 'git', return_value=SimpleNamespace(returncode=0, stdout='')),
            ):
                ports.enter_context(patch)
            for oid in ('remote', 'owned-head'):
                self.remote = oid
                service = lm._current_rebase_service(self.fixture.connection, Path('/repo'),
                    Path('/state'), 'repo', 'G-047', Path('/state/G-047').resolve(), 'loop-manager/G-047')
                self.assertEqual(service.expected.remote.oid, oid)
            for oid in (None, 'external'):
                self.remote = oid
                with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
                    lm._current_rebase_service(self.fixture.connection, Path('/repo'),
                        Path('/state'), 'repo', 'G-047', Path('/state/G-047').resolve(), 'loop-manager/G-047')

    def test_blocked_release_publishes_clean_head_before_restoring_staged_work(self):
        self.assert_blocked_release()

    def test_blocked_publication_interruption_restores_stage_once_before_requeue(self):
        self.assert_blocked_release(interrupted=True)

    def test_blocked_publication_rejection_keeps_stage_snapshot_and_retries_same_intent(self):
        self.assert_blocked_release(rejected=True)

    def test_blocked_publication_retry_finishes_saved_target_after_base_advances(self):
        self.assert_blocked_release(rejected=True, advanced_base=True)

    def test_plan_blocks_recovers_pushed_checkpoint_before_old_remote_check(self):
        self.assert_blocked_release(interrupted=True, entrypoint=True)

    def test_plan_blocks_retries_saved_publication_before_old_remote_check(self):
        self.assert_blocked_release(rejected=True, advanced_base=True, entrypoint=True)

    def assert_blocked_release(self, *, interrupted=False, rejected=False,
                               advanced_base=False, entrypoint=False):
        from loop_manager_internal.git.managed_rebase_validation import FileChange
        connection, runtime = self.fixture.connection, self.fixture.repository
        runtime.transition_and_clear('repo', 'G-047', plan_values={
            'status': 'PLAN_BLOCKED', 'blocked_fingerprint': 'stored'}, enqueue=False)
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo' AND plan_id='G-047'").fetchone()
        branch, worktree = 'loop-manager/G-047', str(Path('/state/G-047').resolve())
        before = lm.GitSnapshot('before', branch,
            {'app.py': ('H 100644 staged-blob 0',)}, {'app.py': (('staged',), 'saved-content')},
            {f'refs/heads/{branch}': ('before', '')},
            {str(Path(worktree).resolve()): ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
        local = {'head': 'before', 'staged': True, 'base': 'base'}
        def capture(_):
            return replace(before, head=local['head'],
                index=before.index if local['staged'] else {'app.py': ('H 100644 rebased-blob 0',)},
                dirty={'app.py': (('staged',) if local['staged'] else ('unstaged',), 'saved-content')},
                refs={f'refs/heads/{branch}': (local['head'], '')},
                worktrees={str(Path(worktree).resolve()): ('HEAD=' + local['head'], f'branch=refs/heads/{branch}')})
        original_git = self.git
        def git(path, *args, **kwargs):
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(returncode=0, stdout=local['head'])
            if args == ('rev-parse', 'refs/remotes/origin/main'):
                return SimpleNamespace(returncode=0, stdout=local['base'])
            if args == ('merge-base', '--is-ancestor', 'refs/remotes/origin/main', 'HEAD'):
                return SimpleNamespace(returncode=1, stdout='')
            return original_git(path, *args, **kwargs)
        def rebase(*_a, **_k):
            local['head'] = 'owned-head'
            local['staged'] = False
            return SimpleNamespace(returncode=0, stdout='')
        def restore(_worktree, snapshot):
            self.assertEqual(snapshot, before)
            self.assertEqual(self.remote, 'owned-head')
            self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
            self.events.append('restore-staging')
            local['staged'] = True
            self.seed.observed = replace(self.seed.observed, index_clean=False,
                staged=(FileChange('app.py', '100644', '100644', 'old', 'staged-blob', 'M'),))
        self.git = git
        with self.direct_ports(), ExitStack() as ports:
            for patch in (
                mock.patch.object(lm, '_plan_block_selected_for_release', return_value=plan),
                mock.patch.object(lm, '_revision_execplan_input_fingerprint', return_value='changed'),
                mock.patch.object(lm, '_revision_review_issue_fingerprint', return_value=None),
                mock.patch.object(lm, '_worktree_execplan_input_fingerprint', return_value='stored'),
                mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                mock.patch.object(lm, 'make_worktree'),
                mock.patch.object(lm, 'validate_blocked_agent_scope'),
                mock.patch.object(lm, 'preserve_blocked_agent_changes', return_value=before),
                mock.patch.object(lm, '_journaled_direct_rebase', side_effect=rebase),
                mock.patch.object(lm, '_blocked_execplan_metadata_error', return_value=None),
                mock.patch.object(lm, 'capture_git_snapshot', side_effect=capture),
                mock.patch.object(lm, '_owned_rebase_journal_state', return_value={'clean': 'rebase result'}),
                mock.patch.object(lm, '_restore_agent_staged_entries', side_effect=restore),
                mock.patch.object(Path, 'is_dir', return_value=True),
            ):
                ports.enter_context(patch)
            if interrupted:
                class Exit(BaseException):
                    pass
                with mock.patch.object(ExecutionStatePort, 'complete_rebase_publication', side_effect=Exit()):
                    with self.assertRaises(Exit):
                        lm.resume_blocked_execplan_if_inputs_changed(
                            connection, Path('/repo'), 'repo', 'main', Path('/state'))
                pending = self.checkpoints.get('repo', 'G-047')
                self.assertEqual(pending.operation, 'rebase_publish')
                self.assertEqual(self.events, ['push'])
                self.assertFalse(local['staged'])
                self.assertEqual(runtime.get('repo', 'G-047').publication_remote_oid, 'remote')
            if rejected:
                self.push_failure = True
                self.assertFalse(lm.resume_blocked_execplan_if_inputs_changed(
                    connection, Path('/repo'), 'repo', 'main', Path('/state')))
                pending = self.checkpoints.get('repo', 'G-047')
                self.assertEqual(pending.operation, 'rebase_publish')
                self.assertEqual(pending.snapshot['blocked_resume']['before_snapshot'], before.serialize())
                self.assertEqual(self.remote, 'remote')
                self.assertEqual(self.events, ['push'])
                self.assertFalse(local['staged'])
                self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
                self.push_failure = False
            if advanced_base:
                local['base'] = 'later-base'
            if entrypoint:
                with mock.patch.object(lm, '_reconcile_published_blocked_checkpoint',
                                       side_effect=AssertionError('old remote checked before recovery')) as reconcile:
                    # A retry refusal must preserve the pending release instead
                    # of falling through to the stale checkpoint remote gate.
                    with mock.patch.object(lm, '_complete_direct_rebase_checkpoint',
                                           side_effect=RuntimeError('temporary publication failure')):
                        self.assertFalse(lm.resume_plan_blocks(
                            connection, Path('/repo'), Path('/state'), 'repo', 'main'))
                    self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                    self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
                    self.assertFalse(local['staged'])
                    self.assertTrue(lm.resume_plan_blocks(
                        connection, Path('/repo'), Path('/state'), 'repo', 'main'))
                    reconcile.assert_not_called()
            else:
                self.assertTrue(lm.resume_blocked_execplan_if_inputs_changed(
                    connection, Path('/repo'), 'repo', 'main', Path('/state')))
            self.assertEqual(self.checkpoints.get('repo', 'G-047').target_base_oid, 'base')
            self.assertEqual(runtime.get('repo', 'G-047').status, 'READY')
            self.assertIsNone(runtime.get('repo', 'G-047').verify_result)
            if interrupted or rejected:
                self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                                 pending.snapshot['rebase_publication_intent'])
        self.assertEqual(self.events, ['push', 'push', 'restore-staging'] if rejected else ['push', 'restore-staging'])
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.observed_remote_oid, 'owned-head')
        self.assertEqual(saved.snapshot['blocked_resume']['owner_oid'], 'remote')
        self.assertEqual(runtime.get('repo', 'G-047').publication_remote_oid, 'owned-head')
        self.assertEqual(capture(None).index, before.index)
        self.assertEqual(capture(None).dirty, before.dirty)
        runtime.activate('repo', 'G-047', activation_sha='base', branch=branch, worktree=worktree)
        self.restart()
        self.handler._fetch_latest_base = mock.Mock(return_value='base')
        recovered = self.handler.recover(self.handler._context())
        self.assertIsInstance(recovered, handlers.ExecutionContext)
        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(self.seed.observed.staged[0].new_oid, 'staged-blob')
        self.assertEqual(len(self.pushes), 2 if rejected else 1)
        if rejected:
            self.assertEqual(self.pushes[0], self.pushes[1])
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()

    def test_completion_publishes_before_candidate_work_without_historical_receipts(self):
        self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(self.pushes, [('push',
            '--force-with-lease=refs/heads/loop-manager/G-047:remote', 'origin',
            'owned-head:refs/heads/loop-manager/G-047')])
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.observed_remote_oid, 'owned-head')
        self.assertEqual(saved.snapshot['last_adopted_remote_oid'], 'owned-head')
        self.assertEqual(saved.snapshot['failure'], 'old failure')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-worker')
        for key in ('worker_result', 'verify_result', 'review_result', 'publication_token'):
            self.assertIsNone(self.state.row()[key])
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()
        self.fixture.effects.publication.push_managed_publication_branch.assert_not_called()
        self.fixture.effects.publication.command.assert_not_called()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), saved)
        self.assertEqual(len(self.pushes), 1)

    def test_interruption_before_push_reuses_intent_before_later_base_fetch(self):
        class Exit(BaseException):
            pass
        self.before_push = lambda: (_ for _ in ()).throw(Exit())
        with self.assertRaises(Exit):
            self.complete()
        intent = self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent']
        self.before_push = None
        self.restart()
        def fetch():
            self.events.append('fetch-later-base')
            raise Exit()
        self.handler._fetch_latest_base = fetch
        with self.assertRaises(Exit):
            self.handler.guard(self.handler._context())
        self.assertEqual(self.events[-2:], ['push', 'fetch-later-base'])
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'], intent)
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')

    def test_interruption_after_push_confirms_once_without_repeating_push(self):
        class Exit(BaseException):
            pass
        with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
            with self.assertRaises(Exit):
                self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(pending.operation, 'rebase_publish')
        self.assertEqual(self.state.row()['publication_remote_oid'], 'remote')
        self.restart()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['rebase_publication_intent'], pending.snapshot['rebase_publication_intent'])
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), saved)
        self.assertEqual(len(self.pushes), 1)

    def test_transient_push_failure_retains_same_lease_and_intent(self):
        self.push_failure = True
        with self.assertRaises(handlers.RecoveryFailure):
            self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        self.push_failure = False
        self.restart()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.pushes[0], self.pushes[1])
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                         pending.snapshot['rebase_publication_intent'])

    def test_remote_race_deletion_or_new_pr_never_replaces_intent_lease(self):
        for raced in ('external-commit', None, 'pr'):
            with self.subTest(raced=raced):
                self._reset_publication_case()
                self.push_failure = True
                with self.assertRaises(handlers.RecoveryFailure):
                    self.complete()
                pending = self.checkpoints.get('repo', 'G-047')
                if raced == 'pr':
                    self.fixture.effects.publication.pr_for_branch.return_value = {'url': 'new-pr'}
                else:
                    self.remote = raced
                self.push_failure = False
                with self.assertRaises(handlers.RecoveryFailure):
                    self.handler._publish_rebased_checkpoint(self.handler._context())
                self.assertEqual(len(self.pushes), 1)
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                self.assertEqual(self.state.row()['publication_remote_oid'], 'remote')

    def test_new_branch_existing_pr_or_different_owner_never_gets_early_push(self):
        for excluded in ('new-branch', 'existing-pr', 'other-owner'):
            with self.subTest(excluded=excluded):
                self._reset_publication_case()
                if excluded == 'new-branch':
                    self.seed.expected = replace(self.seed.expected,
                        remote=replace(self.seed.expected.remote, oid=None))
                    self.seed.observed = replace(self.seed.observed, remote=self.seed.expected.remote)
                    current = self.checkpoints.get('repo', 'G-047')
                    self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
                        operation='rebase', expected_head='before', target_base_oid='base',
                        observed_remote_oid=None, snapshot=current.snapshot)
                    self.state.update_project('repo', publication_remote_oid=None)
                elif excluded == 'existing-pr':
                    self.state.update_project('repo', pr_url='existing-pr')
                else:
                    self.seed.observed = replace(self.seed.observed, branch='another-plan')
                with mock.patch.object(self.handler, '_owned_checkpoint_remote', return_value=True):
                    if excluded == 'other-owner':
                        with self.assertRaises(RuntimeError):
                            self.complete()
                    elif excluded == 'existing-pr':
                        # An unbound PR cannot inherit a no-PR acceptance.
                        saved = self.checkpoints.get('repo', 'G-047')
                        from tests.support.rebase_observations import decision_for
                        accepted = decision_for(self.seed.expected, self.seed.observed).acceptance
                        self.checkpoints.accept_current_rebase('repo', 'G-047', acceptance=accepted)
                        with self.assertRaises(RuntimeError):
                            self.handler._publish_rebased_checkpoint(self.handler._context())
                    else:
                        self.complete()
                self.assertEqual(self.pushes, [])

    def test_same_oid_completes_without_push_and_next_rebase_uses_adopted_oid(self):
        self.remote = 'owned-head'
        self.seed.expected = replace(self.seed.expected, remote=replace(self.seed.expected.remote, oid=self.remote))
        self.seed.observed = replace(self.seed.observed, remote=self.seed.expected.remote)
        self.state.update_project('repo', publication_remote_oid=self.remote)
        current = self.checkpoints.get('repo', 'G-047')
        self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head='before', target_base_oid='base',
            observed_remote_oid=self.remote, snapshot=current.snapshot)
        with mock.patch.object(self.handler, '_owned_checkpoint_remote', return_value=True):
            self.complete()
        self.assertEqual(self.pushes, [])
        with mock.patch.object(handlers.manager_git_journal, 'begin_current_rebase'):
            self.handler._record_rebase_intent(self.handler._context(), 'later-base')
        current = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(current.operation, 'rebase')
        self.assertEqual(current.observed_remote_oid, 'owned-head')
        self.assertEqual(current.target_base_oid, 'later-base')

    def test_final_push_resume_uses_latest_adopted_oid_instead_of_original_checkpoint(self):
        self.complete()
        current = self.checkpoints.get('repo', 'G-047')
        pending = self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='push', expected_head='worker-head', target_base_oid='base',
            observed_remote_oid='owned-head', snapshot={**current.snapshot, 'remote_before': 'owned-head'})
        self.seed.observed = replace(self.seed.observed, head='worker-head',
            branch_ref=('worker-head', ''),
            registration=('HEAD=worker-head', 'branch=refs/heads/loop-manager/G-047'),
            remote=replace(self.seed.expected.remote, oid='worker-head'))
        self.assertTrue(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'worker-head', pending))
        self.assertFalse(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'unrelated-head', pending))
        # A restart may see the confirmed runtime head while the final push
        # checkpoint still holds the intermediate checkpoint's fixed lease.
        self.state.update_project('repo', publication_remote_oid='worker-head')
        self.assertTrue(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'worker-head', pending))
        for unowned_before in ('remote', 'unrelated-head'):
            with self.subTest(unowned_before=unowned_before):
                changed = replace(pending, snapshot={**pending.snapshot,
                    'remote_before': unowned_before})
                self.assertFalse(self.handler._owned_checkpoint_remote(self.handler._context(),
                    self.state.row(), 'worker-head', changed))

    def test_publication_comparison_updates_only_selected_remote_tracking_ref(self):
        from loop_manager_internal.git.managed_rebase_adoption import selected_baseline
        self.complete()
        branch, worktree = 'loop-manager/G-047', str(Path('/state/G-047').resolve())
        ref, remote_ref = f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'
        before = lm.GitSnapshot('before', branch, {}, {},
            {ref: ('before', ''), remote_ref: ('remote', ''), 'refs/heads/other': ('original', '')},
            {str(Path(worktree).resolve()): ('HEAD=before',)}, {}, {})
        current = replace(before, head='owned-head', refs={ref: ('owned-head', ''),
            remote_ref: ('owned-head', ''), 'refs/heads/other': ('unauthorized', '')},
            worktrees={str(Path(worktree).resolve()): ('HEAD=owned-head',)})
        published = self.checkpoints.get('repo', 'G-047')
        accepted = selected_baseline(before, current, branch, str(Path(worktree).resolve()), publication=published)
        self.assertEqual(accepted.refs[remote_ref], ('owned-head', ''))
        self.assertEqual(accepted.refs['refs/heads/other'], ('original', ''))
        self.assertEqual(before.refs[remote_ref], ('remote', ''))
        tampered = replace(current, refs={**current.refs, remote_ref: ('foreign', '')})
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            selected_baseline(before, tampered, branch, str(Path(worktree).resolve()), publication=published)

    def test_existing_remote_without_durable_publication_identity_is_preserved(self):
        self.state.update_project('repo', publication_remote_oid=None)
        # Seed the durable missing-identity state before rebase adoption owns
        # its transaction, rather than leaving a fixture transaction open.
        with self.fixture.connection:
            self.fixture.connection.execute("DELETE FROM blocked_checkpoints WHERE repo = 'repo'")
        self.assertFalse(self.fixture.connection.in_transaction)
        with self.assertRaises(handlers.RecoveryFailure):
            self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.expected_head, 'owned-head')
        self.assertNotIn('rebase_publication_intent', saved.snapshot)
        self.assertEqual(self.remote, 'remote')
        self.assertEqual(self.pushes, [])

    def test_legacy_completed_result_without_journal_or_completion_markers_publishes(self):
        current = self.checkpoints.get('repo', 'G-047')
        self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation=None, expected_head='owned-head', target_base_oid='base',
            observed_remote_oid='remote', snapshot={'failure': 'preserved legacy audit'})
        original_git = self.git
        def legacy_git(path, *args, **kwargs):
            if args == ('merge-base', '--is-ancestor', 'base', 'remote'):
                return SimpleNamespace(returncode=1, stdout='')
            return original_git(path, *args, **kwargs)
        self.handler.effects.workspace.git.side_effect = legacy_git
        self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        self.assertEqual(saved.snapshot['failure'], 'preserved legacy audit')
        self.assertEqual(len(self.pushes), 1)
        self.assertNotIn('completed_rebase', saved.snapshot)
        self.assertNotIn('rebase_completion', saved.snapshot)
