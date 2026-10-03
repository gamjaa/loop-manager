"""Current Rebase Adoption Phase regression tests."""

from tests.support.current_rebase import CurrentRebaseFixture, current_rebase_case
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionOutcomeKind
from tests.support.execution import VALID_WORKER_RESULT


from tests.support.execution_assertions import _assert_manual_autostash_recovery


class CurrentRebaseAdoptionTests(CurrentRebaseFixture, TestCase):
    """Real handler/CAS policies with immutable fake Git observations only."""

    def test_ordinary_checkpoint_remote_observation_keeps_fetched_reader(self):
        from loop_manager_internal.git.managed_rebase_adoption import checkpoint_remote_oid
        current = self.checkpoint.get('repo', 'G-047')
        for selected in (None, replace(current, operation=None, snapshot={}),
                         replace(current, operation=None, snapshot={
                             'current_rebase_acceptance': {'kind': 'unverified'}})):
            with self.subTest(selected=selected):
                git = mock.Mock(side_effect=AssertionError('ordinary observation must use its existing reader'))
                fetched = mock.Mock(return_value='fetched-oid')
                self.assertEqual(checkpoint_remote_oid(selected, Path('/repo'), 'loop-manager/G-047',
                    git=git, read_fetched=fetched), 'fetched-oid')
                fetched.assert_called_once_with(Path('/repo'), 'loop-manager/G-047')
                git.assert_not_called()


    def test_current_acceptance_push_completion_retains_ownership_on_restart(self):
        self._prepare_current_checkpoint_recovery()
        self.handler._complete_rebase_intent(self.fixture.context())
        accepted = self.checkpoint.get('repo', 'G-047').snapshot['current_rebase_acceptance']
        for entry in ('normal', 'recover'):
            with self.subTest(entry=entry):
                saved = self.checkpoint.get('repo', 'G-047')
                self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
                    operation='push', expected_head='owned-head', target_base_oid='base',
                    observed_remote_oid='remote', snapshot={
                        'current_rebase_acceptance': accepted, 'remote_before': 'remote'})
                self.state.update_project('repo', publication_remote_oid='remote')
                self.fixture.effects.workspace.remote_branch_oid.return_value = 'owned-head'
                self.observed = replace(self.observed, remote=replace(self.expected.remote, oid='owned-head'))
                self.fixture.dirty = False
                if entry == 'normal':
                    # Publication has already confirmed the exact live push.
                    self.state.update_project('repo', publication_remote_oid='owned-head')
                    self.handler._complete_publication_intent(self.fixture.context())
                else:
                    self.assertIsInstance(self.handler.recover(self.fixture.context()), handlers.ExecutionContext)
                saved = self.checkpoint.get('repo', 'G-047')
                self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'owned-head')
                self.assertIsNone(saved.operation)
                self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
                self.assertIsInstance(self.handler.recover(self.fixture.context()), handlers.ExecutionContext)
                saved = self.checkpoint.get('repo', 'G-047')
                self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
                    operation='pr_create', expected_head='owned-head', target_base_oid='base',
                    observed_remote_oid='owned-head', snapshot=saved.snapshot)
                pr = dict(url='new-pr', headRefOid='owned-head')
                from loop_manager_internal.git.managed_rebase_validation import PullRequest
                self.observed = replace(self.observed, remote=replace(self.observed.remote,
                    pull_request=PullRequest('new-pr', 'repo', self.expected.branch, 'owned-head', 'OPEN')))
                self.assertTrue(self.handler._owned_checkpoint_remote(self.fixture.context(),
                    self.state.row(), 'owned-head', self.checkpoint.get('repo', 'G-047'), pr))
                self.assertFalse(self.handler._owned_checkpoint_remote(self.fixture.context(),
                    self.state.row(), 'foreign', self.checkpoint.get('repo', 'G-047'), pr))
                self.observed = replace(self.observed, remote=self.expected.remote)

    def test_adopted_checkpoint_restored_stage_reaches_worker_revalidation(self):
        self._assert_restored_stage_recovery()

    def test_new_activation_preserves_adopted_stage_and_revalidates_worker(self):
        self._assert_restored_stage_recovery(newly_activated=True)

    def test_new_activation_rejects_changed_adopted_head_or_remote(self):
        for changed in ('head', 'remote'):
            with self.subTest(changed=changed):
                case = current_rebase_case(self)
                case._prepare_current_checkpoint_recovery()
                case.handler._complete_rebase_intent(case.fixture.context())
                accepted = case.checkpoint.get('repo', 'G-047').snapshot['current_rebase_acceptance']
                case.handler.newly_activated = True
                if changed == 'head':
                    case.observed = replace(case.observed, head='foreign', branch_ref=('foreign', ''),
                        registration=('HEAD=foreign', 'branch=refs/heads/loop-manager/G-047'))
                    def git(path, *args, **kwargs):
                        if args == ('rev-parse', 'HEAD'):
                            return SimpleNamespace(stdout='foreign', returncode=0)
                        return case.fixture.observed_git(path, *args, **kwargs)
                    case.fixture.effects.workspace.git.side_effect = git
                else:
                    case.fixture.effects.workspace.remote_branch_oid.return_value = 'foreign'
                    case.observed = replace(case.observed, remote=replace(case.observed.remote, oid='foreign'))
                result = case.handler.recover(case.fixture.context())
                self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
                saved = case.checkpoint.get('repo', 'G-047')
                self.assertEqual(saved.expected_head, 'owned-head')
                self.assertEqual(saved.snapshot['current_rebase_acceptance'], accepted)
                case.fixture.effects.agent.run_agent.assert_not_called()

    def _assert_restored_stage_recovery(self, *, newly_activated=False):
        from loop_manager_internal.git.managed_rebase_validation import FileChange, IndexEntry
        self._prepare_current_checkpoint_recovery()
        self.handler._complete_rebase_intent(self.fixture.context())
        accepted = self.checkpoint.get('repo', 'G-047').snapshot['current_rebase_acceptance']
        self.handler.newly_activated = newly_activated
        if newly_activated:
            current = self.checkpoint.get('repo', 'G-047')
            self.checkpoint.record('repo', 'G-047', expected_generation=current.generation,
                operation=None, expected_head=current.expected_head, target_base_oid='base',
                observed_remote_oid='remote', snapshot={**current.snapshot, 'blocked_resume': {
                    'before_head': 'before', 'after_head': 'owned-head',
                    'target_base_oid': 'base', 'owner_oid': 'remote'}})
        clean = self.observed
        staged = FileChange('app.py', '100644', '100644', 'old', 'new', 'M')
        self.observed = replace(clean, staged=(staged,), index_clean=False)
        result = self.handler.recover(self.fixture.context())
        self.assertIsInstance(result, handlers.ExecutionContext)
        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot['current_rebase_acceptance'], accepted)
        self.assertIsNone(self.state.row()['worker_result'])
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            self.factory(None).require_owned()  # Staging is not a new completion.
        saved = self.checkpoint.get('repo', 'G-047')
        preserved = self.observed
        for invalid in (
            replace(self.observed, operations=(('MERGE_HEAD', 'foreign'),)),
            replace(self.observed, head='foreign', branch_ref=('foreign', '')),
            replace(self.observed, staged=(replace(staged, path='docs/ARCHITECTURE.md'),)),
            replace(self.observed, index=(IndexEntry('app.py', '100644', 'new', 1, 'M', 0),)),
            replace(self.observed, index=(IndexEntry('app.py', '100644', 'new', 0, 'H', 32768),)),
            replace(self.observed, remote=replace(self.observed.remote, oid='foreign')),
        ):
            with self.subTest(observed=invalid):
                self.observed = invalid
                self.assertFalse(self.handler._owned_checkpoint_remote(
                    self.fixture.context(), self.state.row(), 'remote', saved))
        self.observed = preserved
        for acceptance in (None, {**accepted, 'head': 'tampered'}):
            with self.subTest(acceptance=acceptance):
                changed = replace(saved, snapshot={**saved.snapshot, 'current_rebase_acceptance': acceptance})
                self.assertFalse(self.handler._owned_checkpoint_remote(
                    self.fixture.context(), self.state.row(), 'remote', changed))
        from tests.support.revision import definition
        self.handler._definition_snapshot = definition(path='docs/plans/[G-047] Runtime.md')
        finished = self.handler._finish_worker(result, self.state.row(), VALID_WORKER_RESULT)
        self.assertEqual(finished.context.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot['worker_candidate_token'],
                         self.handler._definition_snapshot.worker_candidate_token('owned-head'))

    def test_manual_autostash_stage_without_worker_result_is_committed_and_readopted(self):
        _assert_manual_autostash_recovery(self, self, )

    def test_autostash_commit_interruptions_recover_original_rebase_intent(self):
        for boundary in ('intent', 'git', 'result'):
            with self.subTest(boundary=boundary):
                case = current_rebase_case(self)
                _assert_manual_autostash_recovery(self, case, interruption=boundary)


    def test_manual_completion_without_journal_invalidates_worker_and_validation_once(self):
        with mock.patch.object(handlers.manager_git_journal, 'verified_rebase_completion',
                               side_effect=AssertionError('history must not gate acceptance')):
            self.handler._complete_rebase_intent(self.fixture.context())
        saved = self.checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.expected_head, 'owned-head')
        self.assertEqual(saved.snapshot['failure'], 'old failure')
        self.assertEqual(saved.snapshot['stage_evidence'], {'old': 'audit'})
        self.assertEqual(saved.snapshot['rebase_revalidation_audit'][0]['verify_result'], 'old verify')
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        for field in ('worker_result', 'worker_input_token', 'verify_result', 'review_result',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(row[field], field)
        self.handler._complete_rebase_intent(self.handler._context())
        self.assertEqual(self.checkpoint.get('repo', 'G-047'), saved)
        self.fixture.effects.validation.verify.assert_not_called()
        self.fixture.effects.agent.run_agent.assert_not_called()

    def test_worker_completed_rebase_is_adopted_without_repeating_git_and_requires_revalidation(self):
        context = replace(self.fixture.context(), resolving_rebase=True)
        with mock.patch.object(self.handler, '_stage_rebase_correction',
                               side_effect=AssertionError('completed rebase must not be staged')), \
             mock.patch.object(self.handler, '_execute_rebase_step',
                               side_effect=AssertionError('completed rebase must not continue')):
            result = self.handler._finish_worker(context, self.state.row(), VALID_WORKER_RESULT)
        self.assertEqual(result.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        saved = self.checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.expected_head, 'owned-head')
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        for field in ('worker_result', 'worker_input_token', 'verify_result', 'review_result',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(row[field], field)
        self.fixture.effects.validation.verify.assert_not_called()

    def test_stale_or_out_of_scope_current_result_preserves_checkpoint_and_audit(self):
        from loop_manager_internal.git.managed_rebase_validation import FileChange
        original = self.checkpoint.get('repo', 'G-047')
        for observed in (replace(self.observed, remote=replace(self.expected.remote, oid='foreign')),
                         replace(self.observed, input_fingerprint='changed'),
                         replace(self.observed, committed=(FileChange(
                             'docs/ARCHITECTURE.md', '100644', '100644', 'old', 'new', 'M'),))):
            with self.subTest(observed=observed):
                self.observed = observed
                with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
                    self.handler._complete_rebase_intent(self.fixture.context())
                self.assertEqual(self.checkpoint.get('repo', 'G-047'), original)
                self.assertEqual(self.state.row()['verify_result'], 'old verify')

    def test_current_remote_owner_survives_rewritten_commits_without_replay_gate(self):
        checkpoint = {'accepted_remote_oid': 'remote'}
        row = dict(self.state.row(), publication_remote_oid='remote')
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid', return_value='remote') as owner, \
             mock.patch.object(handlers.manager_git_journal, 'verified_managed_transitions',
                               side_effect=AssertionError('no replay gate')):
            self.assertTrue(self.handler._owned_current_checkpoint_rebase(
                self.fixture.context(), row, 'remote', self.checkpoint.get('repo', 'G-047'), checkpoint, None))
            self.assertNotIn('before_head', owner.call_args.kwargs)
            self.assertFalse(self.handler._owned_current_checkpoint_rebase(
                self.fixture.context(), row, 'external', self.checkpoint.get('repo', 'G-047'), checkpoint, None))

    def test_direct_and_handler_paths_share_current_result_without_session_receipts(self):
        with mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')), \
             mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **_k: self.factory(None)), \
             mock.patch.object(lm, '_publish_direct_rebase_checkpoint'), \
             mock.patch.object(handlers.manager_git_journal, 'verified_managed_transitions',
                               side_effect=AssertionError('no history')):
            self.assertTrue(lm._direct_rebase_history_proven(
                self.fixture.connection, Path('/repo'), 'repo', 'G-047', Path('/state/G-047'),
                'loop-manager/G-047', 'before', 'owned-head', 'base', 'replacement-session'))
            lm._complete_direct_rebase_checkpoint(
                self.fixture.connection, 'repo', 'G-047', Path('/state/G-047'), 'base',
                repo_path=Path('/repo'), branch='loop-manager/G-047', agent_session_id=None)
        self.assertEqual(self.state.repository.projected('repo').status, 'IMPLEMENTING')
        self.assertIsNone(self.checkpoint.get('repo', 'G-047').operation)

    def test_owned_detached_resolution_waits_without_worker_turn_or_stage_approval(self):
        from loop_manager_internal.git.managed_rebase_validation import IndexEntry
        self.observed = replace(self.observed, head='partial', branch=None,
            branch_ref=('before', ''), registration=('HEAD=partial', 'detached=True'),
            branch_worktrees=(), operations=(('rebase-merge', 'operation'),),
            metadata=(('head-name', 'refs/heads/loop-manager/G-047'),
                      ('orig-head', 'before'), ('onto', 'base')),
            index=(IndexEntry('app.py', '100644', 'conflict', 2, 'M', 0x2000),),
            index_clean=False)
        from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
        service = self.factory(None)
        before = self.checkpoint.get('repo', 'G-047')
        self.assertEqual(service.require_owned()[1].status, RebaseStatus.WAITING)
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            service.adopt(self.state, self.checkpoint)
        self.assertEqual(self.checkpoint.get('repo', 'G-047'), before)

    def test_selected_baseline_keeps_original_and_unrelated_metadata(self):
        from loop_manager_internal.git.managed_rebase_adoption import selected_baseline
        before = lm.GitSnapshot('before', 'loop-manager/G-047', {}, {},
            {'refs/heads/loop-manager/G-047': ('before', ''), 'refs/heads/other': ('old', '')},
            {'/state/G-047': ('HEAD=before',), '/other': ('HEAD=other',)}, {}, {})
        current = replace(before, head='owned-head',
            refs={**before.refs, 'refs/heads/loop-manager/G-047': ('owned-head', ''),
                  'refs/heads/other': ('unauthorized', '')},
            worktrees={**before.worktrees, '/state/G-047': ('HEAD=owned-head',)})
        accepted = selected_baseline(before, current, 'loop-manager/G-047', '/state/G-047')
        self.assertEqual(accepted.head, 'owned-head')
        self.assertEqual(accepted.refs['refs/heads/other'], ('old', ''))
        self.assertEqual(before.head, 'before')
        self.assertEqual(before.worktrees['/state/G-047'], ('HEAD=before',))

    def test_staging_leaves_already_staged_paths_intact(self):
        self.fixture.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='partial', branch=None,
            dirty={'app.py': (('staged',), 'manual'), 'other.py': (('unstaged',), 'resolved')})
        def git(_path, *args, **kwargs):
            if args[:2] == ('ls-files', '--cached'):
                return SimpleNamespace(stdout='app.py\0other.py\0', returncode=0)
            return SimpleNamespace(stdout='', returncode=0)
        self.fixture.effects.workspace.git.side_effect = git
        result = self.handler._stage_rebase_correction(self.fixture.context())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.fixture.effects.workspace.git.call_args.args[1:],
                         ('add', '-A', '--', 'other.py'))

    def test_worker_confirmation_is_bound_to_current_candidate_and_inputs(self):
        from tests.support.revision import definition
        self.handler._complete_rebase_intent(self.fixture.context())
        self.handler._definition_snapshot = definition(path='docs/plans/[G-047] Runtime.md')
        self.fixture.remote = 'owned-head'
        result = self.handler._finish_worker(self.handler._context(), self.state.row(), VALID_WORKER_RESULT)
        self.assertEqual(result.context.phase, ExecutionPhase.VERIFYING)
        confirmed = self.checkpoint.get('repo', 'G-047').snapshot['worker_candidate_token']
        self.assertEqual(confirmed, self.handler._definition_snapshot.worker_candidate_token('owned-head'))
        self.assertNotEqual(confirmed, self.handler._definition_snapshot.worker_candidate_token('another-head'))

    def test_legacy_completed_checkpoint_revalidates_once_in_both_entry_points(self):
        for marker in ('rebase_completion', 'completed_rebase'):
            for entry in ('handler', 'direct'):
                with self.subTest(marker=marker, entry=entry):
                    case = current_rebase_case(self)
                    prior = case.checkpoint.get('repo', 'G-047')
                    legacy = {'head': 'historical-head', 'target_base_oid': 'base'}
                    case.checkpoint.record('repo', 'G-047', expected_generation=prior.generation,
                        operation=None, expected_head='owned-head', target_base_oid='base',
                        observed_remote_oid='remote', snapshot={**prior.snapshot, marker: legacy})
                    with mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')), \
                         mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **_k: case.factory(None)), \
                         mock.patch.object(lm, '_publish_direct_rebase_checkpoint'):
                        def complete():
                            if entry == 'handler':
                                case.handler._complete_rebase_intent(case.handler._context())
                            else:
                                lm._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                                    Path('/state/G-047'), 'base', repo_path=Path('/repo'),
                                    branch='loop-manager/G-047', agent_session_id=None)
                        complete()
                        saved = case.checkpoint.get('repo', 'G-047')
                        complete()
                    self.assertEqual(case.checkpoint.get('repo', 'G-047'), saved)
                    self.assertEqual(saved.snapshot[marker], legacy)
                    self.assertIsNotNone(saved.current_rebase_acceptance())
                    self.assertIsNone(case.fixture.repository.projected('repo').worker_result)
                    self.assertEqual(case.fixture.repository.projected('repo').status, 'IMPLEMENTING')

    def test_start_observes_manual_completion_without_repeating_git(self):
        self.handler._run_managed_rebase = handlers.ExecutionHandlers._run_managed_rebase.__get__(self.handler)
        result = self.handler._run_managed_rebase(self.fixture.context(), 'base')
        self.assertEqual(result.returncode, 0)
        self.fixture.effects.workspace.git.assert_not_called()
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            self.handler._run_managed_rebase(self.fixture.context(), 'wrong-target')
        self.fixture.effects.workspace.git.assert_not_called()

    def test_new_rebase_preserves_nested_prior_operation_audit_without_cycles(self):
        self.handler._complete_rebase_intent(self.fixture.context())
        saved = self.checkpoint.get('repo', 'G-047')
        prior = {**saved.snapshot, 'rebase_operation_history': [{'before_head': 'older'}]}
        self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
            operation=None, expected_head='owned-head', observed_remote_oid='remote',
            target_base_oid='base', snapshot=prior)
        self.fixture.effects.workspace.remote_branch_oid.return_value = 'remote'
        self.fixture.effects.workspace.git.side_effect = lambda *_a, **_k: SimpleNamespace(stdout='owned-head', returncode=0)
        with mock.patch.object(handlers.manager_git_journal, 'live_remote_branch_oid', return_value='remote'), \
             mock.patch.object(handlers.manager_git_journal, 'begin_current_rebase', return_value='new-audit'):
            self.handler._record_rebase_intent(self.handler._context(), 'advanced-base')
        current = self.checkpoint.get('repo', 'G-047')
        self.assertEqual(current.target_base_oid, 'advanced-base')
        self.assertEqual(current.snapshot['rebase_operation_history'], [{'before_head': 'older'}, prior])
        self.assertNotIn('current_rebase_acceptance', current.snapshot)
        self.assertEqual(current.snapshot['current_rebase_acceptance_history'][-1], prior['current_rebase_acceptance'])

    def test_only_confirmed_worker_patch_carries_candidate_token_to_manager_commit(self):
        from tests.support.revision import definition
        self.handler._complete_rebase_intent(self.fixture.context())
        current = definition(path='docs/plans/[G-047] Runtime.md')
        self.handler._definition_snapshot = current
        for confirmed in (True, False):
            with self.subTest(worker_confirmed=confirmed):
                saved = self.checkpoint.get('repo', 'G-047')
                self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
                    operation=None, expected_head='owned-head', target_base_oid='base', observed_remote_oid='remote',
                    snapshot={**saved.snapshot, 'worker_candidate_token': current.worker_candidate_token('owned-head')})
                head = ['owned-head']
                def git(_path, *args, **kwargs):
                    if args[0] == 'commit':
                        head[0] = 'committed'
                    output = head[0] if args == ('rev-parse', 'HEAD') else 'tree'
                    return SimpleNamespace(stdout=output, returncode=0)
                self.fixture.effects.workspace.git.side_effect = git
                self.handler._commit_owned(self.handler._context(), 'Current patch', worker_confirmed=confirmed)
                token = self.checkpoint.get('repo', 'G-047').snapshot['worker_candidate_token']
                self.assertEqual(token, current.worker_candidate_token('committed' if confirmed else 'owned-head'))

    def test_publication_rebase_always_returns_to_worker_confirmation(self):
        for validated in ('old-base', 'new-base'):
            with self.subTest(validated_base=validated):
                saved = self.checkpoint.get('repo', 'G-047')
                self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
                    operation='rebase', expected_head='before', target_base_oid='new-base',
                    observed_remote_oid='remote', snapshot=saved.snapshot)
                self.expected = replace(self.expected, target_base_oid='new-base')
                self.state.update_project('repo', phase='PUBLISHING', active_sha='base',
                    validated_base_sha=validated, worker_result=VALID_WORKER_RESULT)
                def git(_path, *args, **kwargs):
                    return SimpleNamespace(stdout='', returncode=int(args[:2] == ('merge-base', '--is-ancestor')))
                self.fixture.effects.workspace.git.side_effect = git
                def rebase(context, *_a, **_k):
                    self.handler._complete_rebase_intent(context)
                with mock.patch.object(self.handler, '_fetch_latest_base', return_value='new-base'), \
                     mock.patch.object(self.handler, '_rebase_latest', side_effect=rebase):
                    result = self.handler._latest_before_publication(self.handler._context())
                self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                self.assertEqual(self.state.row()['phase'], 'WORKING')
                self.assertIsNone(self.state.row()['worker_result'])
                self.fixture.effects.validation.verify.assert_not_called()
