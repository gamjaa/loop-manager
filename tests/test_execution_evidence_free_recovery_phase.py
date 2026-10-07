"""Evidence Free Recovery Phase regression tests."""

from tests.support.execution import recovered_rebase_workspace, recovered_rebase_publication
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionPhaseCoordinator, ExecutionOutcome, ExecutionOutcomeKind




class EvidenceFreeRecoveryLifecycleTests(TestCase):
    """Manager phase policies with SQLite and fake Git/Agent/process adapters."""

    def setUp(self):
        from tests.support.rebase_block import rebase_block_fixture
        self.blocked = rebase_block_fixture(self)
        self.case = self.blocked.case
        self.fixture = self.case.fixture
        self.handler, self.state = self.case.handler, self.case.state
        workspace = recovered_rebase_workspace(self.handler.effects.workspace,
            connection=self.fixture.connection, repo_path=self.handler.repo_path,
            state_dir=self.handler.state_dir, repo='repo', plan_id='G-047',
            worktree=Path('/state/G-047').resolve())
        # Substitute external Git effects after constructing the same bindings
        # as the real lifecycle fixture; no Git process runs in these cases.
        self.effects = replace(self.handler.effects, workspace=replace(workspace,
            _rebase_in_progress_for_branch=mock.Mock(side_effect=lambda *_: self.pending),
            git_operation_entries=mock.Mock(side_effect=lambda *_:
                {'rebase-merge': 'operation'} if self.pending else {})))
        self.handler.effects = self.effects
        self.case.expected = replace(self.case.expected,
            input_fingerprint='changed-plan-and-source',
            remote=replace(self.case.expected.remote, oid=None, repository='repo'))
        self.case.observed = replace(self.case.observed,
            input_fingerprint=self.case.expected.input_fingerprint,
            remote=self.case.expected.remote)
        self.final_head = 'c' * 40
        self.case.observed = replace(self.case.observed, head=self.final_head,
            branch_ref=(self.final_head, ''), registration=(
                f'HEAD={self.final_head}', 'branch=refs/heads/loop-manager/G-047'))
        self.completed = self.case.observed
        self.pending = True
        self.remote = None
        self.lifecycle = []
        self.worker_result = (
            'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
            'COMMIT_TITLE: Preserve current manual conflict resolution\n'
            'PR_SUMMARY: Recheck the changed plan and current diff.\n'
            'TARGETED_TESTS:\ncurrent candidate unit case: OK\n'
        )
        self.effects.validation.parse_worker_result.side_effect = lm.parse_worker_result
        self.effects.revision.body_hash.return_value = 'changed-plan-body'
        self.effects.revision.source_fingerprint.return_value = 'changed-source'
        self.effects.workspace.git.side_effect = self.git
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: self.remote
        self.effects.agent.capture_git_snapshot.side_effect = lambda *_: lm.GitSnapshot(
            'partial' if self.pending else self.final_head,
            None if self.pending else 'loop-manager/G-047', {},
            {'app.py': (('staged',), 'manual')} if self.pending else {}, {}, {}, {}, {})
        self.effects.agent.run_agent.side_effect = self.agent
        self.effects.validation.verify.side_effect = (
            lambda _: (self.lifecycle.append('verify') or True, 'fresh candidate: OK'))
        self.effects.publication.command.side_effect = self.fixture.publication_command
        saved = self.case.checkpoint.get('repo', 'G-047')
        # There is no saved Worker correction, stage approval or journal proof.
        self.case.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
            operation='rebase', expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={'failure': 'old failure'})
        self.blocked.block()
        self.blocked.pending(staged=True)
        self.assertTrue(self.blocked.resume())
        self.assertIsNone(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047'))
        self.assertTrue(self.pending)
        self.effects.workspace.git.assert_not_called()
        # A resumed Manager opens a new port bound to the released execution.
        self.state = ExecutionStatePort(self.fixture.connection, self.fixture.repository, 'repo')
        self.case.state = self.fixture.state = self.state
        with mock.patch.object(lm, 'push_managed_publication_branch', side_effect=self.push), \
                mock.patch.object(lm, 'push_owned_pr_conflict_recovery', side_effect=self.push):
            publication = recovered_rebase_publication(self.effects.publication, self.state)
        self.effects = replace(self.effects, state=self.state, publication=publication)
        self.handler.effects = self.effects
        self.handler.catalog_plan = self.state.active_execplan('repo')
        self.coordinator = ExecutionPhaseCoordinator(self.handler)
        for patch in (
                mock.patch.object(lm, 'git', side_effect=self.git),
                mock.patch.object(lm, '_owned_publication_plan',
                                  side_effect=lambda *_: self.state.active_execplan('repo'))):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, _path, *args, **_kwargs):
        self.assertNotIn(args[0], ('add', 'commit', 'checkout', 'reset'),
                         'the manually staged candidate must be preserved')
        if 'rebase' in args:
            self.assertIn('--continue', args)
            self.assertTrue(self.pending)
            self.pending = False
            self.case.observed = self.completed
            self.lifecycle.append('manager_continue')
        output, code = '', 0
        if args == ('rev-parse', 'HEAD'):
            output = 'partial' if self.pending else self.final_head
        elif args == ('rev-parse', '--verify', 'refs/heads/loop-manager/G-047'):
            output = self.final_head
        elif args[:2] == ('rev-parse', '--git-path'):
            code = 1
        elif args[0] == 'rev-parse':
            output = 'base'
        elif args[0] == 'symbolic-ref':
            output, code = ('', 1) if self.pending else ('loop-manager/G-047', 0)
        elif args == ('diff', '--cached', '--quiet'):
            code = int(self.pending)
        elif args[:2] == ('ls-files', '--cached'):
            output = 'app.py\0'
        elif args[0] == 'ls-remote' and self.remote:
            output = f'{self.remote}\trefs/heads/loop-manager/G-047\n'
        return SimpleNamespace(stdout=output, returncode=code)

    def agent(self, *args, **kwargs):
        role = args[4]
        if role == 'worker':
            self.assertEqual(kwargs['session_id'], 'saved-worker')
            self.assertIn('현재 HEAD의 base 대비 최종 diff 전체', args[5])
            self.assertIn('targeted unit test', args[5])
            self.assertEqual(self.handler._definition_snapshot.body_hash, 'changed-plan-body')
            self.assertEqual(self.handler._definition_snapshot.source_fingerprint, 'changed-source')
            self.lifecycle.extend(('worker_confirmation', 'targeted_tests'))
            kwargs['on_session_started']('saved-worker')
            return lm.AgentResult(0, self.worker_result, session_id='saved-worker')
        self.assertEqual(role, 'reviewer')
        self.lifecycle.append('review')
        return lm.AgentResult(0, 'VERDICT: PASS', session_id='review-session')

    def push(self, connection, *_args):
        self.assertEqual(self.state.row()['verify_result'], 'fresh candidate: OK')
        self.assertEqual(self.state.row()['review_result'], 'VERDICT: PASS')
        current = self.handler._definition_snapshot
        self.assertEqual(self.state.row()['verify_token'], current.verify_token(
            'base', self.final_head, 'fresh candidate: OK',
        ))
        self.assertEqual(self.state.row()['review_token'], current.review_token('base', self.final_head))
        self.remote = self.final_head
        self.case.expected = replace(self.case.expected,
            remote=replace(self.case.expected.remote, oid=self.remote))
        self.case.observed = replace(self.case.observed, remote=self.case.expected.remote)
        self.lifecycle.append('publication')
        lm.update_project(connection, 'repo', publication_remote_oid=self.remote)
        return self.remote

    def recover(self):
        result = self.handler.recover(self.handler._context())
        self.assertIsInstance(result, handlers.ExecutionContext)
        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        self.assertFalse(self.pending)
        self.assertIsNone(self.case.checkpoint.get('repo', 'G-047').operation)
        for field in ('worker_result', 'verify_result', 'review_result',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(self.state.row()[field], field)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()

    def confirm_worker(self):
        context = self.handler.guard(self.handler._context())
        self.assertIsInstance(context, handlers.ExecutionContext)
        outcome = self.coordinator.dispatch(context)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        saved = self.case.checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['worker_candidate_token'],
                         self.handler._definition_snapshot.worker_candidate_token(self.final_head))

    def existing_pr(self):
        from loop_manager_internal.git.managed_rebase_validation import PullRequest
        self.remote = self.prior_remote = 'a' * 40
        pr = PullRequest('https://example.test/pr/47', 'repo', 'loop-manager/G-047',
                         self.remote, 'OPEN')
        self.case.expected = replace(self.case.expected,
            remote=replace(self.case.expected.remote, oid=self.remote, pull_request=pr))
        self.case.observed = replace(self.case.observed, remote=self.case.expected.remote)
        self.completed = replace(self.completed, remote=self.case.expected.remote)
        current = self.case.checkpoint.get('repo', 'G-047')
        self.case.checkpoint.record('repo', 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=self.remote,
            snapshot=current.snapshot)
        self.state.update_project('repo', pr_url=pr.identity, pr_head_oid=pr.head)
        self.effects.publication.pr_for_branch.return_value = dict(
            url=pr.identity, state=pr.state, headRefName=pr.branch,
            headRefOid=pr.head, baseRefName='main')

    def test_current_pr_lease_survives_recovery_and_fresh_publication(self):
        self.existing_pr()
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: 'stale-cache'
        self.recover()
        self.handler.recover(self.handler._context())
        self.effects.workspace.remote_branch_oid.assert_not_called()
        self.confirm_worker()
        for _ in range(2):
            self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                             ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.publication.push_owned_pr_conflict_recovery.assert_not_called()
        self.state.update_project('repo', pr_conflict_recovery=1,
            pr_conflict_head_oid=self.prior_remote, pr_conflict_base_oid='base')
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: self.remote
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.lifecycle, ['manager_continue', 'worker_confirmation',
            'targeted_tests', 'verify', 'review', 'publication'])
        self.effects.publication.push_owned_pr_conflict_recovery.assert_called_once()
        self.effects.publication.command.assert_called_once()
        self.assertEqual(self.effects.publication.command.call_args.args[0][:3], ['gh', 'pr', 'view'])
        self.assertEqual(self.state.repository.get('repo', 'G-047').status, 'PR_OPEN')

    def test_absent_live_remote_ignores_stale_tracking_ref_after_adoption(self):
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: 'stale-cache'
        self.recover()
        result = self.handler.recover(self.handler._context())
        self.assertIsInstance(result, handlers.ExecutionContext)
        self.effects.workspace.remote_branch_oid.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()

    def test_stale_cache_recovery_reaches_actual_push_adapter_after_fresh_validation(self):
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: 'stale-cache'
        self.recover()
        self.confirm_worker()
        for _ in range(2):
            self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                             ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects = replace(self.effects, publication=recovered_rebase_publication(
            self.effects.publication, self.state))
        self.handler.effects = self.effects
        pushes = []
        def git(path, *args, **kwargs):
            if args[0] == 'push':
                self.assertEqual(self.state.row()['verify_result'], 'fresh candidate: OK')
                self.assertEqual(self.state.row()['review_result'], 'VERDICT: PASS')
                pushes.append(args)
                self.remote = self.final_head
                self.case.expected = replace(self.case.expected,
                    remote=replace(self.case.expected.remote, oid=self.remote))
                self.case.observed = replace(self.case.observed, remote=self.case.expected.remote)
                self.lifecycle.append('publication')
            return self.git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = git
        with mock.patch.object(lm, 'git', side_effect=git), \
                mock.patch.object(lm, '_recorded_base_fetch') as fetch, \
                mock.patch.object(lm, 'pr_for_branch', return_value=None) as pr_query, \
                mock.patch.object(lm, 'remote_branch_oid', return_value='stale-cache') as cached:
            result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(pushes, [('push',
            '--force-with-lease=refs/heads/loop-manager/G-047:', 'origin',
            f'{self.final_head}:refs/heads/loop-manager/G-047')])
        fetch.assert_called_once()
        pr_query.assert_called_once_with(self.handler.repo_path, 'loop-manager/G-047')
        cached.assert_not_called()
        self.effects.workspace.remote_branch_oid.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_called_once()
        self.effects.publication.command.assert_called_once()
        self.assertEqual(self.lifecycle, ['manager_continue', 'worker_confirmation',
            'targeted_tests', 'verify', 'review', 'publication'])
        row = self.state.repository.get('repo', 'G-047')
        self.assertEqual(row.status, 'PR_OPEN')
        self.assertEqual(row.publication_remote_oid, self.final_head)

    def test_live_remote_creation_before_publication_is_rejected_despite_absent_cache(self):
        self.recover()
        self.confirm_worker()
        for _ in range(2):
            self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                             ExecutionOutcomeKind.PHASE_ADVANCED)
        self.remote = 'b' * 40
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: None
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()
        self.assertNotIn('publication', self.lifecycle)

    def test_live_remote_change_after_adoption_keeps_publication_closed(self):
        self.existing_pr()
        self.recover()
        self.remote = 'b' * 40
        self.case.observed = replace(self.case.observed,
            remote=replace(self.case.observed.remote, oid=self.remote))
        result = self.handler.recover(self.handler._context())
        self.assertIsInstance(result, ExecutionOutcome)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_owned_pr_conflict_recovery.assert_not_called()
        self.effects.publication.command.assert_not_called()

    def test_existing_remote_head_creates_pr_without_repeated_push(self):
        self.recover()
        self.confirm_worker()
        for _ in range(2):
            self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                             ExecutionOutcomeKind.PHASE_ADVANCED)
        # A push completed before its remote observation was recorded. The
        # actual record adapter must refresh the snapshot for the next guard.
        self.remote = self.final_head
        self.case.expected = replace(self.case.expected,
            remote=replace(self.case.expected.remote, oid=self.remote))
        self.case.observed = replace(self.case.observed, remote=self.case.expected.remote)
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        expected_record = mock.call(Path('/repo'), Path('/state'), 'repo',
                                    'loop-manager/G-047', Path('/state/G-047').resolve(), self.final_head)
        self.assertEqual(self.effects.publication.record_remote_publication_branch.call_args_list,
                         [expected_record, expected_record])
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_called_once()
        row = self.state.repository.get('repo', 'G-047')
        self.assertEqual(row.status, 'PR_OPEN')
        self.assertEqual(row.publication_remote_oid, self.final_head)

    def test_manual_stage_continue_rechecks_worker_verify_review_and_publication_gate(self):
        self.recover()
        self.handler.recover(self.handler._context())
        self.assertEqual(self.lifecycle, ['manager_continue'])
        self.confirm_worker()
        self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                         ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                         ExecutionOutcomeKind.PHASE_ADVANCED)
        self.state.update_project('repo', review_token='old candidate review')
        stale = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(stale.kind, ExecutionOutcomeKind.RETRY)
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                         ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                         ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.lifecycle, ['manager_continue', 'worker_confirmation',
            'targeted_tests', 'verify', 'review', 'review', 'publication'])
        self.assertEqual(self.state.repository.get('repo', 'G-047').status, 'PR_OPEN')
        self.effects.publication.command.assert_called_once()

    def test_old_success_results_cannot_publish_without_current_worker_confirmation(self):
        self.recover()
        self.state.update_project('repo', phase='PUBLISHING', worker_result=self.worker_result,
            verify_result='old verify', review_result='VERDICT: PASS', validated_base_sha='base',
            worker_input_token='old-input', verify_token='old-verify', review_token='old-review')
        # Coordinator runs guard before dispatching the saved PUBLISHING phase.
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()

    def test_unsuccessful_fresh_verify_does_not_open_review_or_publication(self):
        self.recover()
        self.confirm_worker()
        self.effects.validation.verify.side_effect = lambda _: (False, 'current candidate rejected')
        def rejected(_repo, _worktree, _stage, output):
            self.state.update_project('repo', phase='WORKING', validated_base_sha=None,
                                     verify_result=output, last_error=output)
            return SimpleNamespace(blocked=False, fingerprint='current-verify')
        self.effects.validation.transition_validation_failure.side_effect = rejected
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['review_token'])
        self.assertNotIn('review', self.lifecycle)
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()
