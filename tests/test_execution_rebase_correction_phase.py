"""Rebase Correction Phase execution policy regression tests."""

from tests.support.current_rebase import current_rebase_case
from tests.support.runtime import runtime_rebase_fixture
from tests.support.execution import ExecutionFixture
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRecord, ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionOutcomeKind
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from tests.support.worker import decision as worker_decision


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionRebaseCorrectionPhaseTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_rebase_commit_failure_keeps_expected_snapshot_for_retry(self):
        connection, runtime, checkpoint, original, _expected, decision = (
            runtime_rebase_fixture(self)
        )
        state = ExecutionStatePort(connection, runtime, "repo")

        class FailingCommit:
            def __init__(self, actual):
                self.actual = actual

            def __getattr__(self, name):
                return getattr(self.actual, name)

            def commit(self):
                raise RuntimeError("commit failure")

        state.connection = FailingCommit(connection)
        with self.assertRaisesRegex(RuntimeError, "commit failure"):
            state.complete_rebase(
                checkpoint, project_values={"phase": "WORKING"},
                acceptance=decision.acceptance, reobserve=lambda: decision,
            )
        self.assertEqual(state.expected, original)
        self.assertEqual(runtime.projected("repo"), original)
        state.connection = connection
        state.complete_rebase(
            checkpoint, project_values={"phase": "WORKING"},
            acceptance=decision.acceptance, reobserve=lambda: decision,
        )
        self.assertEqual(state.expected, runtime.projected("repo"))

    def test_scope_stage_false_block_recovers_checkpoint_through_fresh_validation(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=True, manual='staged', scope_stage_block=True)


    def test_manual_stage_block_recovers_changed_plan_without_worker_stage_evidence(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, manual='staged')


    def test_legacy_rebase_rejects_unstaged_files_after_git_completion(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, rejected='unstaged')


    def test_legacy_rebase_rejects_staged_index_after_git_completion(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, rejected='staged')


    def test_published_legacy_staged_rejection_preserves_journal_for_restored_index(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=False, rejected='staged', published_checkpoint=True,
        )


    def test_autostash_completion_requires_approved_stage_and_matching_blob(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project('repo', worker_session_id='corrector')
        context = self.context()
        stage = {'head': 'head', 'branch': context.branch,
                 'files': {'app.py': 'accepted'},
                 'modes': {'app.py': '100644'},
                 'index': {'app.py': ['M 100644 before 2']}}
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None,
            snapshot={'rebase_evidence_version': 2, 'stash_oid': 'stash',
                      'rebase_start_session_id': 'original',
                      'pending': 'continue', 'worker_result': 'result',
                      'stage_evidence': stage},
        )
        blob_oid = 'a' * 40
        common = {'head': 'head', 'branch': context.branch,
                  'ref': ['head', ''], 'registration': ['owned'],
                  'operations': {}, 'metadata': None}
        sealed = {**common, 'unmerged': True,
                  'dirty': {'app.py': [['unmerged'], 'before']},
                  'index': {'app.py': ['M 100644 before 2']}}
        live = {**common, 'unmerged': False,
                'dirty': {'app.py': [['staged'], 'accepted']},
                'index': {'app.py': [f'H 100644 {blob_oid} 0']}}
        self.handler._stage_evidence_preserved = (
            handlers.ExecutionHandlers._stage_evidence_preserved.__get__(self.handler)
        )
        self.handler._rebase_stage_evidence = mock.Mock(return_value={
            **stage, 'index': live['index'],
        })

        def evidence_git(_worktree, *args, **_kwargs):
            if args[:2] == ('ls-files', '--cached'):
                return SimpleNamespace(returncode=0, stdout='app.py\0')
            if args[:1] == ('hash-object',):
                return SimpleNamespace(returncode=0, stdout=blob_oid + '\n')
            raise AssertionError(args)

        self.effects.workspace.git.side_effect = evidence_git
        intent = {'expected': {'evidence_version': 2,
                               'agent_session_id': 'original'}}
        self.assertTrue(self.handler._completed_autostash_correction(
            context, intent, sealed, live,
        ))
        self.assertTrue(self.handler._completed_autostash_correction(
            context, intent,
            {**sealed, 'operations': {'MERGE_MSG': 'sealed-marker'}}, live,
        ))
        self.assertTrue(self.handler._completed_autostash_correction(
            context, intent,
            {**sealed, 'operations': {'REBASE_HEAD': 'sealed-marker'}}, live,
        ))
        self.assertFalse(self.handler._completed_autostash_correction(
            context, intent,
            {**sealed, 'operations': {'CHERRY_PICK_HEAD': 'foreign-marker'}}, live,
        ))
        rejection = []
        self.assertFalse(self.handler._completed_autostash_correction(
            context, intent, sealed,
            {**live, 'dirty': {**live['dirty'], 'extra.py': [['staged'], 'extra']}},
            rejection=rejection,
        ))
        self.assertEqual(rejection, ['unapproved dirty path'])
        self.assertFalse(self.handler._completed_autostash_correction(
            context, intent, sealed,
            {**live, 'file_evidence': {'extra.py': {'mode': '100755'}}},
        ))
        self.assertFalse(self.handler._completed_autostash_correction(
            context, {'expected': {**intent['expected'],
                                   'agent_session_id': 'foreign'}}, sealed, live,
        ))
        self.effects.workspace.git.side_effect = lambda *_args, **_kwargs: (
            SimpleNamespace(returncode=0, stdout='app.py\0')
            if _args[1:3] == ('ls-files', '--cached') else
            SimpleNamespace(returncode=0, stdout='b' * 40 + '\n')
        )
        self.assertFalse(self.handler._completed_autostash_correction(
            context, intent, sealed, live,
        ))


    def test_autostash_completion_reports_rejected_operation_marker(self):
        case = current_rebase_case(self)
        case.expected = replace(case.expected, autostash_oid='stash')
        case.observed = replace(case.observed, operations=(('CHERRY_PICK_HEAD', 'foreign'),))
        original = case.checkpoint.get('repo', 'G-047')
        with self.assertRaisesRegex(handlers.manager_git_journal.MutationEvidenceError,
                                   'unresolved_operation_or_index'):
            case.handler._complete_rebase_intent(case.fixture.context())
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
        self.assertEqual(case.state.row()['verify_result'], 'old verify')


    def test_malformed_worker_outcome_enters_protocol_correction(self):
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, 'WORKER_OUTCOME: {bad}', session_id='session')
        )
        result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_new_worker_wait_without_outcome_declaration_enters_protocol_correction(self):
        message = (
            '사용자 승인을 기다려 구현을 중단했습니다.\n'
            'COMMIT_TITLE: Premature candidate\n'
            'PR_SUMMARY: Waiting for approval.\n'
            'TARGETED_TESTS:\ncase: PASS'
        )
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, message, session_id='session')
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')
        self.assertIn('WORKER_OUTCOME', self.state.row()['last_error'])
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_saved_rebase_decision_blocks_before_stage(self):
        self._decision_workspace()
        message = worker_decision()
        self.state.update_project(
            'repo', agent_role='worker', agent_snapshot='before-scope',
            agent_input_fingerprint='remote-input',
        )
        saved = SimpleNamespace(
            scope_marker='same', worker_code=0, worker_result=message,
            generation=1, outcome_contract_version=1,
        )
        context = replace(self.context(), resolving_rebase=True)
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler._resume_saved_rebase_worker(
                context, saved, SimpleNamespace(progress='same'),
            )
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PLAN_BLOCKED')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.agent.clear_agent_scope.assert_not_called()
        self.assertNotIn('add', self.calls)


    def test_rebase_stage_omits_ignored_cache_from_accepted_snapshot(self):
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={
                'app.py': (('unstaged',), 'accepted-content'),
                'cache/ignored.bin': (('untracked',), 'preserved-cache'),
            },
        )

        stage = self.handler._stage_rebase_correction(self.context('WORKING'))

        self.assertEqual(stage.returncode, 0)
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1] == 'add']
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].args[1:], ('add', '-A', '--', 'app.py'))


    def test_rebase_stage_evidence_records_index_entries(self):
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('unstaged',), 'accepted-content')},
            index={'app.py': ('H 100644 ' + 'a' * 40,)},
        )

        self.assertEqual(self.handler._rebase_stage_evidence(self.context('WORKING')), {
            'head': 'owned-head', 'branch': 'loop-manager/G-047',
            'files': {'app.py': 'accepted-content'},
            'index': {'app.py': ['H 100644 ' + 'a' * 40]},
        })


    def test_partial_stage_resume_only_stages_remaining_dirty_path(self):
        self._assert_partial_stage_resume(changed=False)


    def test_partial_stage_resume_rejects_replaced_clean_resolution(self):
        self._assert_partial_stage_resume(changed=True)


    def test_rebase_correction_blocks_only_after_three_identical_worker_results(self):
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

        first = self.handler._rebase_conflict(context, 'conflict')
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        for _ in range(2):
            result = self.handler._rebase_conflict(
                context, 'conflict', corrected=True, worker_result='same output',
            )
            self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
            self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')
            self.effects.blocking.block_active_execplan.assert_not_called()
        final = self.handler._rebase_conflict(
            context, 'conflict', corrected=True, worker_result='same output',
        )
        self.assertEqual(final.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIn('same output', final.detail)
        self.assertEqual(self.handler.rebase_corrections.get('repo', 'G-047').occurrence, 3)


    def test_zero_exit_rebase_with_unmerged_index_keeps_conflict_correction(self):
        checkpoint = mock.Mock()
        checkpoint.get.return_value = CheckpointRecord(
            generation=1, operation='rebase', expected_head='before',
            target_base_oid='new-base', snapshot={'staged_paths': ['app.py']},
        )
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        original_git = self.git

        def conflicted_git(path, *args, **kwargs):
            if args[:2] == ('ls-files', '-u'):
                return SimpleNamespace(
                    stdout='100644 ' + 'a' * 40 + ' 1\tapp.py\0',
                    returncode=0,
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = conflicted_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        context = self.context('VERIFYING')
        before = self.state.row()['active_sha']

        result = self.handler._post_rebase_observation(
            context, 'new-base', 'Created autostash: deadbee\nconflicts',
        )

        self.assertEqual(result.recovery.observation.reason.value, 'rebase_conflict')
        self.assertEqual(self.state.row()['active_sha'], before)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        saved = checkpoint.record.call_args.kwargs['snapshot']
        self.assertIn('100644 ' + 'a' * 40, saved['unmerged_index'])


    def test_preparation_preserves_existing_rebase_correction_diagnostic(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        message = lm.REBASE_CONFLICT_PREFIX + 'Worker 결과 검증 실패: actionable output'
        self.state.update_project(
            'repo', phase='WORKING', last_error=message,
            worker_session_id='worker-session',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertTrue(recovered.resolving_rebase)
        self.assertEqual(self.state.row()['last_error'], message)
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')


    def test_accepted_user_correction_requires_checkpoint_remote_ownership(self):
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', "
            "'evidence', 'user-head')"
        )
        self.effects.workspace.git.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
            stdout='user-head\n', returncode=0,
        )
        current = SimpleNamespace(operation=None, snapshot=None)
        row = {'pr_url': None, 'publication_remote_oid': None}

        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='user-head') as owner:
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'user-head', current,
            ))
            owner.assert_called_once()
            owner.return_value = None
            self.assertFalse(self.handler._owned_checkpoint_remote(
                self.context(), row, 'user-head', current,
            ))
        self.connection.execute(
            "UPDATE blocked_checkpoints SET accepted_remote_oid = 'old-head' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.assertFalse(self.handler._owned_checkpoint_remote(
            self.context(), row, 'user-head', current,
        ))


    def test_staged_rebase_resume_reuses_saved_worker_result(self):
        self._assert_staged_rebase_resume(True)


    def test_staged_autostash_resume_reuses_saved_worker_result(self):
        self._assert_staged_rebase_resume(False)


    def test_completed_stage_matching_head_resumes_without_restaging(self):
        self._assert_staged_rebase_resume(True, clean_after_stage=True)


    def test_completed_autostash_stage_matching_head_resumes_without_rebase(self):
        self._assert_staged_rebase_resume(False, clean_after_stage=True)


    def test_completed_stage_accepts_replaced_clean_resolution_before_skip(self):
        self._assert_staged_rebase_resume(
            True, clean_after_stage=True, replaced_resolution=True,
        )


    def test_completed_autostash_stage_accepts_replaced_clean_resolution(self):
        self._assert_staged_rebase_resume(
            False, clean_after_stage=True, replaced_resolution=True,
        )


    def test_staged_rebase_resume_accepts_allowed_added_files(self):
        self._assert_staged_rebase_resume(True, changed=True)


    def test_pending_stage_resumes_accepted_unstaged_changes(self):
        self._assert_staged_rebase_resume(True, unstaged=True)


    def test_pending_stage_resumes_accepted_untracked_files(self):
        self._assert_staged_rebase_resume(True, untracked=True)


    def test_pending_stage_with_unmerged_index_retries_only_accepted_paths(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='saved-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None,
            snapshot={
                'pending': 'stage', 'worker_result': 'accepted correction',
                'stage_evidence': {
                    'head': 'owned-head', 'branch': 'loop-manager/G-047',
                    'files': {'app.py': 'accepted-content'},
                },
            },
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('unstaged',), 'accepted-content')},
        )
        unresolved = {'value': True}

        def observed_git(path, *args, **kwargs):
            if args[:2] == ('ls-files', '-u'):
                return SimpleNamespace(
                    stdout=('100644 ' + 'a' * 40 + ' 1\tapp.py\0'
                            if unresolved['value'] else ''), returncode=0,
                )
            if args[0] == 'add':
                unresolved['value'] = False
            if args == ('diff', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        result = self.handler.recover(self.context())

        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        self.assertFalse(unresolved['value'])
        self.effects.agent.run_agent.assert_not_called()
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1] == 'add']
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].args[1:], ('add', '-A', '--', 'app.py'))


    def test_stage_lock_resume_uses_checkpoint_without_new_worker_turn(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='saved-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        attempts = {'add': 0, 'unmerged': True}

        def locked_git(path, *args, **kwargs):
            if args[:2] == ('ls-files', '-u'):
                return SimpleNamespace(
                    stdout=('100644 ' + 'a' * 40 + ' 1\tapp.py\0'
                            if attempts['unmerged'] else ''), returncode=0,
                )
            if args[0] == 'add':
                attempts['add'] += 1
                if attempts['add'] == 1:
                    return SimpleNamespace(stdout='index.lock', returncode=1)
                attempts['unmerged'] = False
                return SimpleNamespace(stdout='', returncode=0)
            if args == ('diff', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = locked_git
        context = self.context()
        first = self.handler._finish_worker(context, self.state.row(), 'accepted correction')
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['pending'], 'stage')

        resumed = self.handler.recover(self.context())
        self.assertEqual(resumed.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(attempts['add'], 2)
        self.effects.agent.run_agent.assert_not_called()


    def test_continue_lock_resume_does_not_repeat_stage(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='saved-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        attempts = {'add': 0, 'continue': 0}

        def locked_git(path, *args, **kwargs):
            if args[0] == 'add':
                attempts['add'] += 1
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            if args[-2:] == ('rebase', '--continue'):
                attempts['continue'] += 1
                if attempts['continue'] == 1:
                    return SimpleNamespace(stdout='index.lock', returncode=1)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = locked_git
        context = self.context()
        first = self.handler._finish_worker(context, self.state.row(), 'accepted correction')
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['pending'], 'continue')

        def finished_git(path, *args, **kwargs):
            result = locked_git(path, *args, **kwargs)
            if args[-2:] == ('rebase', '--continue') and attempts['continue'] == 2:
                self.effects.workspace._rebase_in_progress_for_branch.return_value = False
            return result

        self.effects.workspace.git.side_effect = finished_git
        resumed = self.handler.recover(self.context())
        self.assertEqual(resumed.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(attempts, {'add': 1, 'continue': 2})
        self.effects.agent.run_agent.assert_not_called()


    def test_pending_stage_accepts_current_allowed_changes_before_staging(self):
        self._assert_staged_rebase_resume(True, changed=True, unstaged=True)


    def test_autostash_without_identity_blocks_after_command_and_on_resume(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before-head', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        def conflicted_git(path, *args, **kwargs):
            if args[0] == 'ls-files':
                return SimpleNamespace(stdout='100644 blob 1\tapp.py\0', returncode=0)
            return self.observed_git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = conflicted_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        context = self.context('VERIFYING')
        for result in (
            self.handler._post_rebase_observation(context, 'base', 'completed'),
            self.handler.recover(context),
        ):
            self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
            self.assertEqual(result.detail, 'autostash_identity_unavailable')
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.assertEqual(checkpoint.get('repo', 'G-047').operation, 'rebase')
        self.assertEqual(self.effects.blocking.block_active_execplan.call_count, 2)
        for call in self.effects.blocking.block_active_execplan.call_args_list:
            self.assertEqual(call.kwargs['reason'], lm.BlockReasonCode.REPOSITORY_STATE_UNSAFE)
