"""Publication Phase execution policy regression tests."""

from tests.support.execution import ExecutionFixture
from dataclasses import replace
import json
from types import SimpleNamespace
import sqlite3
from pathlib import Path
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
from loop_manager_internal.git.github_observation import PrForBranchEffects, pr_for_branch
from loop_manager_internal.execution.execution_ownership_handler import OwnershipHandler
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery import blocked_plan_reconciliation, project_blocked_recovery
from loop_manager_internal.execution.plan_validation import (
    RevisionExecplanSourceFingerprintEffects, _revision_execplan_source_fingerprint,
    validate_active_execplan_metadata,
)
from loop_manager_internal.git.command_adapter import ExternalObservationError
from loop_manager_internal.runtime.contracts import GitLockError


class ExternalOwnershipObservationTests(TestCase):
    def test_blocked_checkpoint_adoption_retries_failed_owner_observation(self):
        checkpoint = SimpleNamespace(operation=None)
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {'phase': 'published'}
        owner = mock.Mock(side_effect=RecoveryFailure(
            RecoveryReason.PUBLICATION_RETRY, 'different diagnostic'))
        reconciler = SimpleNamespace(
            connection=connection, repo='/repo', repo_path=Path('/repo'),
            state_dir=Path('/state'),
            ports=SimpleNamespace(
                _reconcile_published_blocked_checkpoint=lambda *args, **kwargs: 'unchanged',
                git=lambda *args, **kwargs: SimpleNamespace(stdout='head\n'),
                owned_remote_oid=owner,
            ),
            _plan=lambda plan_id: {'publication_remote_oid': 'head'},
        )
        plan = {'plan_id': 'G-001', 'branch': 'branch', 'worktree': '/worktree'}
        context = {'branch_oid': 'head'}
        with mock.patch.object(blocked_plan_reconciliation, 'ExecutionCheckpointRepository') as checkpoints, \
             mock.patch.object(blocked_plan_reconciliation, 'BlockedCheckpointRepository'):
            checkpoints.return_value.get.return_value = checkpoint
            with self.assertRaises(RecoveryFailure) as caught:
                blocked_plan_reconciliation.BlockedPlanReconciler._adopt(
                    reconciler, plan, context)
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        self.assertTrue(owner.call_args.kwargs['strict_observation'])

    def test_project_checkpoint_lineage_retries_failed_owner_observation(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {
            'accepted_remote_oid': 'head', 'published_oid': None,
        }
        owner = mock.Mock(side_effect=RecoveryFailure(
            RecoveryReason.PUBLICATION_RETRY, 'different diagnostic'))
        reconciler = SimpleNamespace(
            connection=connection, repo='/repo', repo_path=Path('/repo'),
            ports=SimpleNamespace(owned_remote_oid=owner, git=mock.Mock()),
        )
        plan = {'plan_id': 'G-001', 'branch': 'branch', 'worktree': '/worktree',
                'publication_remote_oid': 'head'}
        with self.assertRaises(RecoveryFailure) as caught:
            project_blocked_recovery.ProjectBlockedReconciler._lineage_known(
                reconciler, plan, {})
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        self.assertTrue(owner.call_args.kwargs['strict_observation'])

    def test_blocked_scope_ref_observation_failure_remains_retryable(self):
        branch = 'loop-manager/G-001'
        branch_ref = f'refs/remotes/origin/{branch}'
        before = SimpleNamespace(refs={branch_ref: ('old', '')})
        current = SimpleNamespace(
            refs={branch_ref: ('head', ''), f'refs/heads/{branch}': ('head', '')},
            branch=branch, head='head',
        )
        connection = mock.Mock()
        connection.execute.side_effect = [
            SimpleNamespace(fetchone=lambda: {'present': 1}),
            SimpleNamespace(fetchone=lambda: {
                'registered_origin': 'example/repo', 'base_ref': 'refs/heads/main',
                'branch_ref': f'refs/heads/{branch}', 'base_result': 'present',
                'base_oid': 'base', 'origin_url': 'https://github.com/example/repo',
            }),
            SimpleNamespace(fetchone=lambda: {
                'status': 'PLAN_BLOCKED', 'workspace_owned': 1,
                'branch': branch, 'worktree': '/worktree',
                'publication_remote_oid': 'head',
            }),
            SimpleNamespace(fetchone=lambda: {
                'origin': 'example/repo', 'base_branch': 'main',
            }),
        ]
        owner = mock.Mock(side_effect=RecoveryFailure(
            RecoveryReason.PUBLICATION_RETRY, 'different diagnostic'))
        observed = SimpleNamespace(result='present', oid='head')
        with mock.patch.object(blocked_plan_reconciliation.observations, 'observe_refs',
                               return_value=(observed, observed)):
            with self.assertRaises(RecoveryFailure) as caught:
                blocked_plan_reconciliation.validated_scope_refs(
                    connection, Path('/repo'), '/repo', 'main', 'G-001',
                    before, None, current,
                    git=lambda *args, **kwargs: SimpleNamespace(stdout='https://github.com/example/repo\n'),
                    github_repository=lambda origin: 'example/repo',
                    owned_remote_oid=owner,
                )
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        self.assertTrue(owner.call_args.kwargs['strict_observation'])

    def test_workspace_metadata_git_lock_does_not_become_agent_correction(self):
        effects = SimpleNamespace(validate_execplan=mock.Mock(
            side_effect=GitLockError('changed lock text')))
        with self.assertRaises(GitLockError):
            validate_active_execplan_metadata(
                effects, Path('/repo'), Path('/worktree'),
                'docs/plans/plan.md', {},
            )

    def test_failed_source_read_does_not_fall_back_to_archive(self):
        source = 'docs/requirements/[R-001] Source.md'
        calls = []
        def git(_repo, operation, *args, **kwargs):
            calls.append((operation, args))
            if operation == 'show':
                return SimpleNamespace(returncode=128, stdout='different diagnostic')
            return SimpleNamespace(
                returncode=0, stdout=f'100644 blob deadbeef\t{source}\0')
        effects = RevisionExecplanSourceFingerprintEffects(
            _execplan_input_fingerprint=mock.Mock(), git=git,
        )
        with self.assertRaises(ExternalObservationError):
            _revision_execplan_source_fingerprint(
                effects, Path('/repo'), 'base', {'sources': json.dumps([source])}
            )
        self.assertEqual([operation for operation, _args in calls], ['show', 'ls-tree'])
        effects._execplan_input_fingerprint.assert_not_called()

    def test_pr_response_must_be_valid_before_absence_or_identity_is_accepted(self):
        malformed_fields = json.dumps([{
            'url': 'https://example.test/pr/1', 'state': [], 'mergedAt': None,
            'headRefOid': 'head', 'headRefName': 'loop-manager/G-001',
            'baseRefName': 'main', 'mergeable': 'UNKNOWN',
        }])
        for response in ('not JSON', '{}', '[null]', '[{}, {}]', malformed_fields):
            with self.subTest(response=response):
                effects = PrForBranchEffects(command=lambda *args, **kwargs:
                    SimpleNamespace(returncode=0, stdout=response))
                with self.assertRaises(RuntimeError):
                    pr_for_branch(effects, Path('/repo'), 'loop-manager/G-001')
        effects = PrForBranchEffects(command=lambda *args, **kwargs:
            SimpleNamespace(returncode=0, stdout='[]'))
        self.assertIsNone(pr_for_branch(effects, Path('/repo'), 'loop-manager/G-001'))
        failed = PrForBranchEffects(command=lambda *args, **kwargs:
            SimpleNamespace(returncode=1, stdout='[]'))
        with self.assertRaises(RuntimeError):
            pr_for_branch(failed, Path('/repo'), 'loop-manager/G-001')

    def test_checkpoint_sqlite_failure_is_retryable_and_does_not_check_git(self):
        connection = mock.Mock()
        connection.execute.side_effect = sqlite3.OperationalError('database is locked')
        git = mock.Mock()
        handler = OwnershipHandler(SimpleNamespace(
            state=SimpleNamespace(connection=connection),
            identity=SimpleNamespace(repo='/repo'), git=git,
        ))
        context = SimpleNamespace(plan=SimpleNamespace(plan_id='G-001'))
        with self.assertRaises(RecoveryFailure) as caught:
            handler._owned_checkpoint_remote(context, {}, None, None)
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        git.assert_not_called()

    def test_failed_live_ref_query_does_not_become_ownership_refusal(self):
        row = {
            'phase': 'published', 'result': 'published', 'branch': 'branch',
            'worktree': '/worktree', 'accepted_remote_oid': 'old',
            'pending_remote_oid': None, 'published_oid': 'published',
            'commit_identity': 'identity', 'tree_oid': 'tree',
        }
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = row
        def git(_repo, operation, *args, **kwargs):
            if operation == 'show':
                return SimpleNamespace(stdout='[G-001] BLOCKED checkpoint\nScope: plan\nCheckpoint-Identity: identity\n')
            if operation == 'rev-parse':
                return SimpleNamespace(stdout='tree\n')
            return SimpleNamespace(returncode=1 if operation == 'ls-remote' else 0, stdout='')
        with self.assertRaises(RecoveryFailure) as caught:
            checkpoint_ownership.owned_remote_oid(
                connection, Path('/repo'), '/repo', 'G-001', 'branch',
                Path('/worktree'), 'old', git, strict_observation=True,
            )
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        def malformed_git(repo, operation, *args, **kwargs):
            if operation == 'ls-remote':
                return SimpleNamespace(returncode=0, stdout='malformed ref response')
            return git(repo, operation, *args, **kwargs)
        with self.assertRaises(RecoveryFailure) as caught:
            checkpoint_ownership.owned_remote_oid(
                connection, Path('/repo'), '/repo', 'G-001', 'branch',
                Path('/worktree'), 'old', malformed_git, strict_observation=True,
            )
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)


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
