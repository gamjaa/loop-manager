from tests.support.rebase_block import rebase_block_fixture
from dataclasses import replace
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.execution.execution_runtime import (
    ExecutionOutcome,
    ExecutionOutcomeKind,
    ExecutionPhase,
    ExecutionPhaseCoordinator,
)

import loop_manager
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode


from tests.support.scoped_recovery import ScopeBaselineFixture
from tests.support.scoped_recovery import CurrentRebaseBlockFixture


class ScopeBaselineRecoveryUnitTests(ScopeBaselineFixture, unittest.TestCase):
    def test_retired_restore_intent_releases_without_changing_user_candidate(self):
        self.connection.execute("UPDATE exec_plans SET agent_recovery='opaque legacy restore intent', "
                                "worker_session_id='saved session'")
        self.connection.commit()
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation',
                           evidence='old scope error', release_fingerprint='unchanged')
        block = scoped_blocking.plan_block(self.connection, 'repo', 'G-047')
        original = self.current
        self.assertTrue(loop_manager._reconcile_scope_baseline_plan_block(
            self.connection, self.repo_path, Path('/state'), 'repo', block))
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertIsNone(row['agent_recovery'])
        self.assertEqual(row['worker_session_id'], 'saved session')
        audit = self.checkpoints.get('repo', 'G-047').snapshot['scope_block_release_audit'][-1]
        self.assertEqual(audit['prior_agent_recovery'], 'opaque legacy restore intent')
        self.assertEqual(self.current, original)

    def test_project_retired_scope_release_invalidates_changed_candidate_only(self):
        self.connection.execute("UPDATE exec_plans SET worker_result='old output', verify_result='old verify', "
                                "review_result='old review', status='VERIFYING'")
        self.connection.commit()
        self.current = replace(self.current, head='user-new-head')
        self.project_block()
        self.assertTrue(self.resume_project())
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        audit = self.checkpoints.get('repo', 'G-047').snapshot['scope_block_release_audit'][-1]
        self.assertEqual(audit['prior_artifacts']['review_result'], 'old review')

    def test_ignored_plan_release_continues_after_saved_verify_without_rerunning(self):
        self._assert_saved_verify_resume(legacy=False)

    def test_legacy_ignored_plan_release_migrates_audited_verify_without_rerunning(self):
        self._assert_saved_verify_resume(legacy=True)

    def test_ignored_plan_release_keeps_dirty_verified_candidate_in_verifying(self):
        self._assert_saved_verify_resume(legacy=False, dirty=True)

    def test_legacy_verify_resumes_with_definition_commit_different_from_base(self):
        self._assert_saved_verify_resume(legacy=True, definition_revision='submitted-definition')

    def _assert_saved_verify_resume(self, *, legacy, dirty=False, definition_revision='base'):
        if not dirty:
            self.current = replace(self.current, dirty={})
        start = replace(self.before, dirty={'cache.bin': (('untracked',), 'old')})
        result = replace(self.current, dirty={**self.current.dirty,
                                             'cache.bin': (('untracked',), 'result')},
                         worktrees=self.before.worktrees)
        definition = DefinitionSnapshot(
            revision_sha='base', path='docs/plans/[G-047] Runtime.md',
            title='Runtime', sources=('docs/PRODUCT.md',), depends_on=(),
            complexity='medium', risk='medium', priority='normal',
            body_hash='body', source_fingerprint='source', archive_candidates=(),
        )
        loop_manager.runtime_state.RuntimeStateRepository(self.connection).update_project(
            'repo', {
                'phase': 'VERIFYING', 'active_sha': 'base',
                'validated_base_sha': 'base', 'worker_result': 'saved worker',
                'verify_result': 'saved verify', 'definition_snapshot': definition.serialize(),
                'verify_token': definition.verify_token(
                    'base', self.current.head, None if legacy else 'saved verify',
                ),
            }, enqueue=False,
        )
        if legacy:
            from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository
            ValidationFeedbackRepository(self.connection).record_result(
                'repo', 'G-047', definition_revision, 'verify', 'success', 'saved verify')
        self.connection.execute(
            "UPDATE exec_plans SET agent_snapshot = ?, agent_result_snapshot = ?, current_revision_sha = ?",
            (start.serialize(), result.serialize(), definition_revision),
        )
        self.connection.commit()
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation',
                           evidence='legacy ignored cache', release_fingerprint='same')
        block = scoped_blocking.plan_block(self.connection, 'repo', 'G-047')

        def normalize(_worktree, saved, _current, **_options):
            return replace(saved, dirty={key: value for key, value in saved.dirty.items()
                                         if key != 'cache.bin'})

        with mock.patch.object(loop_manager, '_normalize_legacy_ignored_snapshot', side_effect=normalize):
            self.assertTrue(loop_manager._reconcile_scope_baseline_plan_block(
                self.connection, self.repo_path, Path('/state'), 'repo', block))

        resumed = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(resumed['status'], 'VERIFYING' if dirty else 'REVIEWING')
        self.assertEqual((resumed['verify_result'], resumed['verify_token']),
                         ('saved verify', definition.verify_token(
                             'base', self.current.head, 'saved verify',
                         )))
        self.assertEqual(resumed['agent_result_snapshot'], result.serialize())

        if dirty:
            return
        if legacy:
            from loop_manager_internal.execution.execution_revision import (
                StageArtifacts, decide_revision_gate)
            decision = decide_revision_gate(
                phase='REVIEWING', previous=definition, current=definition,
                active_base_token='base', current_base_token='base',
                candidate_head=self.current.head,
                artifacts=StageArtifacts(
                    manual_submission=True, verify_result=True,
                    verify_output=resumed['verify_result'], verify_token=resumed['verify_token']))
            self.assertEqual(decision.phase, 'REVIEWING')
        context = SimpleNamespace(phase=ExecutionPhase(resumed['status']))
        phase_outcome = ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)
        ports = SimpleNamespace(
            guard=mock.Mock(return_value=context),
            work_started=mock.Mock(),
            run=mock.Mock(return_value=phase_outcome),
            verify=mock.Mock(return_value=phase_outcome),
            review=mock.Mock(return_value=phase_outcome),
            publish=mock.Mock(return_value=phase_outcome),
        )
        ExecutionPhaseCoordinator(ports).dispatch(context)
        ports.verify.assert_not_called()
        ports.review.assert_called_once_with(context)

    def test_legacy_verify_requires_clean_snapshots_and_latest_matching_success(self):
        from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository
        definition = DefinitionSnapshot(
            revision_sha='base', path='docs/plans/[G-047] Runtime.md', title='Runtime',
            sources=('docs/PRODUCT.md',), depends_on=(), complexity='medium',
            risk='medium', priority='normal', body_hash='body', source_fingerprint='source',
            archive_candidates=())
        plan = dict(repo='repo', plan_id='G-047', active_sha='base', validated_base_sha='base',
                    current_revision_sha='submitted-definition',
                    definition_snapshot=definition.serialize(), verify_result='saved verify',
                    verify_token=definition.verify_token('base', self.before.head))
        proof = loop_manager.project_block_service._legacy_verify_matches_candidate
        clean = replace(self.current, dirty={})
        self.assertFalse(proof(self.connection, plan, self.before, clean, clean))
        audit = ValidationFeedbackRepository(self.connection)
        audit.record_result('repo', 'G-047', 'submitted-definition', 'verify', 'success', 'saved verify')
        self.assertTrue(proof(self.connection, plan, self.before, clean, clean))
        audit.record_result('repo', 'G-047', 'base', 'verify', 'success', 'saved verify')
        self.assertFalse(proof(self.connection, plan, self.before, clean, clean))
        audit.record_result('repo', 'G-047', 'submitted-definition', 'verify', 'success', 'saved verify')
        self.assertFalse(proof(self.connection, plan, self.before, self.current, self.current))
        self.assertFalse(proof(self.connection, plan, self.before, self.current, clean))
        self.assertFalse(proof(self.connection, plan, self.before, clean, self.current))
        self.assertFalse(proof(self.connection, plan, self.before, clean, replace(clean, head='other')))
        audit.record_result('repo', 'G-047', 'other', 'verify', 'success', 'saved verify')
        self.assertFalse(proof(self.connection, plan, self.before, clean, clean))
        audit.record_result('repo', 'G-047', 'submitted-definition', 'verify', 'success', 'replaced output')
        self.assertFalse(proof(self.connection, plan, self.before, clean, clean))
        audit.record_result('repo', 'G-047', 'submitted-definition', 'verify', 'failure', 'saved verify')
        self.assertFalse(proof(self.connection, plan, self.before, clean, clean))

    def test_saved_verify_success_token_does_not_cover_replaced_failure_output(self):
        definition = DefinitionSnapshot(
            revision_sha='base', path='docs/plans/[G-047] Runtime.md',
            title='Runtime', sources=('docs/PRODUCT.md',), depends_on=(),
            complexity='medium', risk='medium', priority='normal',
            body_hash='body', source_fingerprint='source', archive_candidates=(),
        )
        plan = {
            'active_sha': 'base',
            'validated_base_sha': 'base',
            'verify_result': 'failed output from resumed verify',
            'verify_token': definition.verify_token(
                'base', self.current.head, 'prior successful output',
            ),
            'definition_snapshot': definition.serialize(),
        }

        self.assertFalse(loop_manager.project_block_service._saved_verify_matches_candidate(
            plan, self.current.head,
        ))

    def test_ignored_plan_release_preserves_stage_with_proven_invocation_isolation(self):
        start = replace(self.before, dirty={'cache.bin': (('untracked',), 'old')})
        result = replace(self.current, dirty={**self.current.dirty,
                                             'cache.bin': (('untracked',), 'result')},
                         worktrees=self.before.worktrees)
        self.connection.execute("UPDATE exec_plans SET status = 'REVIEWING', "
                                "agent_snapshot = ?, agent_result_snapshot = ?, "
                                "verify_result = 'saved verify', verify_token = 'saved token'",
                                (start.serialize(), result.serialize()))
        self.connection.commit()
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation',
                           evidence='legacy ignored cache', release_fingerprint='same')
        block = scoped_blocking.plan_block(self.connection, 'repo', 'G-047')
        # This test isolates classification from the separately tested Git rule proof.
        def normalize(_worktree, saved, _current, **_options):
            return replace(saved, dirty={key: value for key, value in saved.dirty.items()
                                         if key != 'cache.bin'})
        with mock.patch.object(loop_manager, '_normalize_legacy_ignored_snapshot', side_effect=normalize):
            self.current = replace(self.current, refs={**self.current.refs, 'refs/heads/other': ('foreign', '')})
            self.assertTrue(loop_manager._reconcile_scope_baseline_plan_block(
                self.connection, self.repo_path, Path('/state'), 'repo', block))
        resumed = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual((resumed['status'], resumed['verify_result'], resumed['verify_token']),
                         ('VERIFYING', None, None))
        self.assertEqual(resumed['agent_result_snapshot'], result.serialize())

    def test_related_plan_document_scope_block_resumes_without_discarding_worker(self):
        related_plan = 'docs/plans/[G-060] Project Root Placement Policy and Inventory.md'
        self.current = replace(self.current, dirty={
            **self.current.dirty,
            related_plan: (('unstaged', 'worktree'), 'inventory-update'),
        })
        self.connection.execute('UPDATE exec_plans SET agent_result_snapshot = ?',
                                (self.current.serialize(),))
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation',
                           evidence='worker Git 범위 위반: ' + related_plan,
                           release_fingerprint='input',
                           plan_values={'blocked_fingerprint': 'input'})
        block = scoped_blocking.plan_block(self.connection, 'repo', 'G-047')

        self.assertTrue(loop_manager._reconcile_scope_baseline_plan_block(
            self.connection, self.repo_path, Path('/state'), 'repo', block))

        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertEqual(row['agent_snapshot'], self.before.serialize())
        self.assertEqual(row['agent_result_snapshot'], self.current.serialize())
        self.assertEqual((row['worker_session_id'], row['retry_count'], row['review_failure_history']),
                         ('saved-worker', 3, '{"failure":2}'))
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        audit = self.checkpoints.get('repo', 'G-047').snapshot['scope_block_release_audit']
        self.assertEqual(audit[-1]['block']['evidence'], 'worker Git 범위 위반: ' + related_plan)

    def test_isolated_default_worktree_false_positive_keeps_work_session_and_retry_history(self):
        self.project_block()
        self.assertTrue(self.resume_project())
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['agent_snapshot'], self.before.serialize())
        self.assertEqual(row['agent_result_snapshot'], self.current.serialize())
        self.assertEqual((row['worker_session_id'], row['retry_count'], row['review_failure_history']),
                         ('saved-worker', 3, '{"failure":2}'))
        self.assertIsNone(scoped_blocking.project_block(self.connection, 'repo'))


    def test_legacy_execution_without_isolation_ignores_default_worktree_change(self):
        self.isolated = False
        self.project_block()
        self.assertTrue(self.resume_project())
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['agent_snapshot'], self.before.serialize())
        self.assertEqual(row['agent_result_snapshot'], self.current.serialize())
        self.assertEqual((row['worker_session_id'], row['retry_count']), ('saved-worker', 3))
        self.assertIsNone(scoped_blocking.project_block(self.connection, 'repo'))


    def test_legacy_blocked_handoff_accepts_current_worktree_input_and_preserves_snapshots(self):
        self.isolated = False
        saved_result = self.current.serialize()
        self.project_block()
        branch_ref = f"refs/heads/{self.before.branch}"
        registration = self.before.worktrees[str(self.worktree)]
        self.current = replace(
            self.current, head='user-head',
            index={'user.py': ('H 100644 user-blob 0',)},
            dirty={'user.py': (('staged',), 'user input')},
            refs={**self.current.refs, branch_ref: ('user-head', '')},
            worktrees={**self.current.worktrees, str(self.worktree): (
                'HEAD=user-head', *tuple(value for value in registration if not value.startswith('HEAD=')))},
        )

        self.assertTrue(self.resume_project())

        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['agent_snapshot'], self.before.serialize())
        self.assertEqual(row['agent_result_snapshot'], saved_result)
        self.assertEqual(self.current.head, 'user-head')
        self.assertIsNone(scoped_blocking.project_block(self.connection, 'repo'))


    def test_old_ref_and_config_diagnoses_release_without_git_access_attribution(self):
        original = self.current
        for current in (replace(original, refs={**original.refs, 'refs/remotes/origin/main': ('new', '')}),
                        replace(original, config={'local:core.hooksPath': ('changed',)})):
            self.current = current
            self.project_block()
            with self.subTest(current=current):
                self.assertTrue(self.resume_project())
                self.assertIsNone(scoped_blocking.project_block(self.connection, 'repo'))


    def test_releasing_project_scope_does_not_release_separate_review_cause(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='review_specification_deadlock',
                           evidence='review still needs a decision', release_fingerprint='review')
        review = dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.project_block()
        self.assertTrue(self.resume_project())
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-047')), review)
        self.assertEqual(self.connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'PLAN_BLOCKED')


    def test_current_git_race_keeps_original_project_scope_block(self):
        self.project_block()
        original = self.current
        count = 0
        def capture(*_args):
            nonlocal count
            count += 1
            return original if count < 3 else replace(original, head='foreign')
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=capture):
            self.assertFalse(self.resume_project())
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, 'repo'))


    def test_restored_current_input_releases_old_index_diagnosis_without_erasing_audit(self):
        self.project_block()
        flagged = replace(self.current, index={'app.py': ('S 100644 resolved 0',)})
        self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (flagged.serialize(),))
        self.assertTrue(self.resume_project())
        self.assertIsNone(scoped_blocking.project_block(self.connection, 'repo'))
        self.assertEqual(self.connection.execute('SELECT agent_result_snapshot FROM exec_plans').fetchone()[0], flagged.serialize())


    def test_saved_index_diagnosis_does_not_replay_permissions_against_current_candidate(self):
        flagged = replace(self.current, index={'app.py': ('S 100644 resolved 0',)})
        self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (flagged.serialize(),))
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(loop_manager.validate_blocked_agent_scope(self.connection, self.repo_path, 'repo', 'main', self.worktree, plan['path'], saved_plan=plan), self.current)
        self.assertEqual(self.connection.execute('SELECT agent_result_snapshot FROM exec_plans').fetchone()[0], flagged.serialize())


    def test_failed_false_positive_release_keeps_existing_changed_input_correction_route(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation',
                           evidence='real violation', release_fingerprint='input',
                           plan_values={'blocked_fingerprint': 'input'})
        with mock.patch.object(loop_manager, '_reconcile_scope_baseline_plan_block', return_value=False), \
                mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                                  side_effect=RuntimeError('existing correction selected')) as inputs:
            with self.assertRaisesRegex(RuntimeError, 'existing correction selected'):
                loop_manager.resume_blocked_execplan_if_inputs_changed(self.connection, self.repo_path,
                    'repo', 'main', Path('/state'), plan_id='G-047')
        inputs.assert_called_once()


    def test_failed_scope_resume_releases_projection_without_changing_saved_plan(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation',
                           evidence='real violation', release_fingerprint='unchanged')
        loop_manager._ensure_execplans_schema(self.connection)
        with self.connection:
            loop_manager._clear_active_in_current_transaction(self.connection, 'repo')
        original = dict(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        block = dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        def failed_resume(*_args, **_kwargs):
            loop_manager.hydrate_execplan_project(self.connection, 'repo', original)
            return False
        with mock.patch.object(loop_manager, 'resume_blocked_execplan_if_inputs_changed',
                               side_effect=failed_resume):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo_path, Path('/state'), 'repo', 'main'))
        self.assertIsNone(loop_manager.project_execplan(self.connection, 'repo'))
        self.assertIsNone(self.connection.execute('SELECT phase FROM projects').fetchone()[0])
        self.assertEqual(dict(self.connection.execute('SELECT * FROM exec_plans').fetchone()), original)
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-047')), block)


    def test_failed_scope_resume_keeps_new_project_safety_block(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation',
                           evidence='real violation', release_fingerprint='unchanged')
        plan = dict(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        def failed_resume(*_args, **_kwargs):
            loop_manager.hydrate_execplan_project(self.connection, 'repo', plan)
            self.project_block()
            return False
        with mock.patch.object(loop_manager, 'resume_blocked_execplan_if_inputs_changed',
                               side_effect=failed_resume):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo_path, Path('/state'), 'repo', 'main'))
        self.assertEqual(self.connection.execute('SELECT phase FROM projects').fetchone()[0], 'PROJECT_BLOCKED')
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, 'repo'))
        self.assertIsNotNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))


    def test_failed_scope_resume_restores_previously_selected_other_block(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        self.connection.execute('INSERT INTO exec_plans '
            '(repo, plan_id, path, title, sources, depends_on, priority, content_hash, status) '
            "VALUES ('repo', 'G-048', 'docs/plans/[G-048] Other.md', 'Other', '[]', '[]', "
            "'normal', 'hash', 'PLAN_BLOCKED')")
        for plan_id in ('G-047', 'G-048'):
            runtime.block_plan('repo', plan_id, reason_code='managed_worktree_scope_violation',
                               evidence='real violation', release_fingerprint='unchanged')
        loop_manager._ensure_execplans_schema(self.connection)
        previous = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id = 'G-048'").fetchone()
        loop_manager.hydrate_execplan_project(self.connection, 'repo', previous)
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        plans = [dict(row) for row in self.connection.execute('SELECT * FROM exec_plans ORDER BY plan_id')]
        def failed_resume(*_args, plan_id, **_kwargs):
            plan = self.connection.execute('SELECT * FROM exec_plans WHERE plan_id = ?', (plan_id,)).fetchone()
            loop_manager.hydrate_execplan_project(self.connection, 'repo', plan)
            return False
        with mock.patch.object(loop_manager, 'resume_blocked_execplan_if_inputs_changed',
                               side_effect=failed_resume):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo_path, Path('/state'), 'repo', 'main'))
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM exec_plans ORDER BY plan_id')],
                         plans)


    def test_planner_scope_release_rechecks_snapshot_and_preserves_planning_error(self):
        from loop_manager_internal.agents.agent_scope_decision import used_git_resources
        self.before = replace(self.before, branch=None, dirty={})
        self.current = replace(self.before, worktrees=self.current.worktrees)
        self.resources = used_git_resources(role='planner', phase='PLANNING', base_branch='main',
            branch=None, worktree_key=str(self.worktree))
        self.connection.execute("UPDATE projects SET phase = 'PLANNING', plan_path = NULL, "
            "agent_role = 'planner', agent_snapshot = ?, agent_result_snapshot = ?, "
            "worktree = ?, planning_error = 'independent planning error'",
            (self.before.serialize(), self.current.serialize(), str(self.worktree)))
        scoped_blocking.record_project_block(self.connection, 'repo',
            BlockReasonCode.DEFAULT_WORKTREE_MUTATION, 'source changed', 'same',
            plan_id=None, previous_phase='PLANNING')
        original = self.current
        count = 0
        def capture(*_args):
            nonlocal count
            count += 1
            return original if count == 1 else replace(original, head='late mutation')
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=capture):
            self.assertFalse(self.resume_project())
        self.connection.execute("UPDATE projects SET planning_attempt_fingerprint='current-attempt', planning_workspace_owned=1")
        with mock.patch.object(loop_manager, 'validate_planning_worktree_ownership'):
            self.assertTrue(self.resume_project())
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        self.assertEqual(project['planning_error'], 'independent planning error')
        self.assertEqual(project['last_error'], 'independent planning error')


    def test_local_scope_release_is_independent_of_old_publication_oid(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation', evidence='old scope', release_fingerprint='same')
        self.connection.execute("UPDATE blocked_checkpoints SET phase='published', published_oid='old-remote'")
        self.connection.commit()
        with mock.patch.object(loop_manager, '_owned_checkpoint_remote_oid', return_value=None) as ownership:
            self.assertTrue(loop_manager.resume_plan_blocks(self.connection, self.repo_path, Path('/state'), 'repo', 'main'))
        ownership.assert_not_called()
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))


    def test_plan_scope_false_positive_requeues_same_worker_and_invalidates_validation(self):
        runtime = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('repo', 'G-047', reason_code='managed_worktree_scope_violation',
                           evidence='legacy unrelated source scope', release_fingerprint='unchanged')
        self.connection.commit()
        self.current = replace(self.current, head='new-candidate')
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo_path, Path('/state'), 'repo', 'main'))
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual((row['status'], row['worker_session_id'], row['retry_count']),
                         ('IMPLEMENTING', 'saved-worker', 3))
        for field in ('worker_result', 'verify_result', 'review_result', 'worker_input_token',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(row[field], field)
        self.assertEqual(row['agent_snapshot'], self.before.serialize())
        self.assertTrue(self.checkpoints.get('repo', 'G-047').snapshot['scope_block_release_audit'])


class EvidenceFreeRebaseBlockTests(CurrentRebaseBlockFixture, unittest.TestCase):
    def test_plan_input_block_uses_owned_rebase_before_ordinary_branch_recovery(self):
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        self.block(reason='plan_input_invalid')
        self.pending()
        recovery = RecoveryRepository(self.connection)
        recovery.observe(RecoveryObservation(RecoveryReason.WORKER_CORRECTION,
            'repo', 'G-047', 'WORKING', 'revision', 'base', 'fingerprint', 'progress',
            'original sources metadata failure'), correction_limit=1)
        self.connection.execute("UPDATE recovery_records SET final_evidence=? WHERE repo='repo' AND subject='G-047'",
            ('{"block_reason_code":"plan_input_invalid"}',))
        self.connection.commit()
        original = self.case.observed
        with mock.patch.object(loop_manager, 'make_worktree',
                               side_effect=AssertionError('detached rebase is not an ordinary worktree')):
            self.assertTrue(self.resume())
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.assertEqual(self.case.observed, original)
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        self.assertEqual(recovery.row('repo', 'G-047')['pending_action'], 'reconcile')
        self.assertEqual(recovery.row('repo', 'G-047')['detail'], 'original sources metadata failure')

    def test_plan_input_rebase_block_keeps_original_cause_if_remote_changes(self):
        self.block(reason='plan_input_invalid')
        self.pending()
        self.case.observed = replace(self.case.observed,
            remote=replace(self.case.observed.remote, oid='external'))
        block = dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        with mock.patch.object(loop_manager, 'make_worktree',
                               side_effect=AssertionError('unsafe operation must remain preserved')):
            self.assertFalse(self.resume())
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-047')), block)

    def test_changed_inputs_and_session_resume_manual_completion_without_recovery_history(self):
        self.block()
        self.connection.execute("UPDATE exec_plans SET worker_session_id = 'replacement-session', "
                                "worker_input_token = 'obsolete', review_failure_history = '{\"issue\":2}'")
        self.connection.commit()
        before = self.checkpoints.get('repo', 'G-047')
        self.assertTrue(self.resume())
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertEqual(row['worker_session_id'], 'replacement-session')
        self.assertEqual(row['review_failure_history'], '{"issue":2}')
        for field in ('worker_result', 'worker_input_token', 'verify_result', 'review_result',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(row[field], field)
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        adopted = self.checkpoints.get('repo', 'G-047')
        self.assertIsNone(adopted.operation)
        self.assertEqual(adopted.snapshot['failure'], before.snapshot['failure'])
        self.assertTrue(adopted.current_rebase_acceptance())
        self.assertEqual(adopted.snapshot['rebase_revalidation_audit'][-1]['released_rebase_block']['evidence'],
                         'G-088 repeated conflict with changed plan inputs')
        self.assertFalse(self.resume())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), adopted)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM notified_block_incidents').fetchone()[0], 1)
        self.git_write.assert_not_called()


    def test_owned_detached_conflict_resumes_same_operation(self):
        self.block()
        self.pending()
        original = self.case.observed
        self.assertTrue(self.resume())
        self.assertEqual(self.case.observed, original)
        self.assertEqual(self.checkpoints.get('repo', 'G-047').operation, 'rebase')
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertEqual(row['last_error'], loop_manager.REBASE_IN_PROGRESS_ERROR)
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        self.git_write.assert_not_called()


    def test_manual_stage_resumes_without_stage_approval_or_extra_git_step(self):
        self.block()
        self.pending(staged=True)
        self.assertTrue(self.resume())
        self.assertEqual(self.case.observed.staged[0].new_oid, 'manual')
        self.assertEqual(self.checkpoints.get('repo', 'G-047').operation, 'rebase')
        self.git_write.assert_not_called()


    def test_saved_scope_stage_false_positive_resumes_without_input_change_or_retry_reset(self):
        self.block(reason='managed_worktree_scope_violation')
        before, result = self.scope_stage_result()
        self.assertTrue(self.resume())
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertEqual(row['agent_snapshot'], before.serialize())
        self.assertEqual(row['agent_result_snapshot'], result.serialize())
        self.assertEqual(row['retry_count'], 3)
        self.assertEqual(row['review_failure_history'], '{"issue":2}')
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        self.assertEqual(self.checkpoints.get('repo', 'G-047').operation, 'rebase')
        self.git_write.assert_not_called()


    def test_saved_scope_stage_false_positive_uses_same_owned_existing_pr_service(self):
        from loop_manager_internal.git.managed_rebase_validation import PullRequest
        remote = replace(self.case.expected.remote, pull_request=PullRequest(
            'https://example/pr/1', self.case.expected.remote.repository,
            'loop-manager/G-047', 'remote', 'OPEN'))
        self.case.expected = replace(self.case.expected, remote=remote)
        self.case.observed = replace(self.case.observed, remote=remote)
        self.connection.execute("UPDATE exec_plans SET pr_url = 'https://example/pr/1', pr_head_oid = 'remote', "
                                "pr_conflict_recovery = 1, pr_conflict_head_oid = 'remote'")
        self.block(reason='managed_worktree_scope_violation')
        self.scope_stage_result()
        self.assertTrue(self.resume())
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual((row['pr_url'], row['pr_head_oid'], row['pr_conflict_recovery']),
                         ('https://example/pr/1', 'remote', 1))
        self.git_write.assert_not_called()


    def test_completed_scope_stage_result_adopts_after_restart_and_keeps_other_block(self):
        self.block(reason='managed_worktree_scope_violation')
        self.scope_stage_result()
        self.case.observed = replace(self.case.observed, head='owned-head', branch='loop-manager/G-047',
            branch_ref=('owned-head', ''), branch_worktrees=(str(Path('/state/G-047').resolve()),),
            registration=('HEAD=owned-head', 'branch=refs/heads/loop-manager/G-047'),
            operations=(), metadata=None, index_clean=True, staged=())
        self.current_scope = replace(self.current_scope, head='owned-head', branch='loop-manager/G-047',
            refs={'refs/heads/loop-manager/G-047': ('owned-head', '')},
            worktrees={str(Path('/state/G-047').resolve()): self.case.observed.registration}, operations={}, dirty={})
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status, blocked_reason) VALUES ('repo', 'G-048', "
            "'docs/plans/[G-048] Other.md', 'Other', '[]', '[]', 'normal', 'hash', 'PLAN_BLOCKED', 'user_decision_required')")
        self.connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES ('repo', 'G-048', 'plan', 'user_decision_required', 'keep decision')")
        self.connection.commit()
        other = dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-048'))
        self.assertTrue(self.resume())
        adopted = self.checkpoints.get('repo', 'G-047')
        self.assertIsNone(adopted.operation)
        self.assertEqual(adopted.current_rebase_acceptance()['head'], 'owned-head')
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'repo', 'G-048')), other)
        self.assertIsNone(self.connection.execute("SELECT verify_token FROM exec_plans WHERE plan_id = 'G-047'").fetchone()[0])
        self.git_write.assert_not_called()


    def test_historical_git_flags_and_refs_do_not_block_owned_current_rebase(self):
        self.block(reason='managed_worktree_scope_violation')
        _, result = self.scope_stage_result()
        changed = replace(result, index={'app.py': ('S 100644 manual 0',)}, refs={**result.refs, 'refs/heads/other': ('foreign', '')})
        self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (changed.serialize(),))
        self.connection.commit()
        self.assertTrue(self.resume())
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.assertEqual(self.connection.execute('SELECT agent_result_snapshot FROM exec_plans').fetchone()[0], changed.serialize())
        self.git_write.assert_not_called()


    def test_scope_rebase_release_rolls_back_with_transaction_interruption(self):
        self.block(reason='managed_worktree_scope_violation')
        self.scope_stage_result()
        before = self.snapshot()
        def fault(_runtime, stage):
            if stage == 'after_rebase_block_release':
                raise RuntimeError('interrupted scope release')
        with mock.patch.object(loop_manager.runtime_state.RuntimeStateRepository, '_fault', fault):
            self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.resume())
        self.git_write.assert_not_called()


    def test_previously_adopted_result_rechecks_current_state_and_releases_once(self):
        self.case.handler._complete_rebase_intent(self.case.fixture.context())
        self.block()
        self.assertTrue(self.resume())
        adopted = self.checkpoints.get('repo', 'G-047')
        self.assertFalse(self.resume())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), adopted)
        self.git_write.assert_not_called()


    def test_same_pending_result_cannot_repeat_a_released_correction(self):
        detail = self.block()
        self.pending()
        self.assertTrue(self.resume())
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
                                    "blocked_reason = 'managed_worktree_conflict'")
            self.connection.execute("UPDATE projects SET phase = 'PLAN_BLOCKED'")
            self.connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES ('repo', 'G-047', 'plan', 'managed_worktree_conflict', ?)", (detail,))
        before = self.snapshot()
        self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        from dataclasses import replace
        self.case.expected = replace(self.case.expected, input_fingerprint='new-user-input')
        self.case.observed = replace(self.case.observed, input_fingerprint='new-user-input')
        self.assertTrue(self.resume())


    def test_first_exhausted_conflict_stays_blocked_until_selected_git_state_changes(self):
        from dataclasses import replace
        snapshot = self.exhausted_pending()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot) as capture, \
             mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint', return_value='same-input'), \
             mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint', return_value='same-input'):
            before = self.snapshot()
            self.assertFalse(self.resume())
            self.assertEqual(self.snapshot(), before)
            self.assertFalse(self.resume())
            self.assertEqual(self.snapshot(), before)
            capture.return_value = replace(snapshot, dirty={'app.py': (('unstaged',), 'user-resolution')})
            self.assertTrue(self.resume())
            self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.git_write.assert_not_called()


    def test_changed_current_inputs_resume_exhausted_conflict_without_git_progress(self):
        snapshot = self.exhausted_pending()
        for changed in ('revision', 'worktree'):
            with self.subTest(changed=changed), \
                 mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot), \
                 mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                     return_value='new-input' if changed == 'revision' else 'same-input'), \
                 mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint',
                     return_value='new-input' if changed == 'worktree' else 'same-input'), \
                 mock.patch.object(loop_manager.runtime_state.RuntimeStateRepository,
                     'resume_rebase_block') as release:
                self.assertTrue(self.resume())
                release.assert_called_once()
        self.git_write.assert_not_called()


    def test_missing_recovery_history_does_not_make_saved_snapshot_a_resume_gate(self):
        snapshot = self.exhausted_pending()
        self.connection.execute('DELETE FROM recovery_records')
        self.connection.commit()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot), \
             mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint', return_value='same-input'), \
             mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint', return_value='same-input'):
            self.assertTrue(self.resume())
        self.git_write.assert_not_called()


    def test_project_rebase_release_preserves_each_independent_plan_cause(self):
        for cause in ('review_retry_exhausted', 'guidance_approval_required', 'user_decision_required',
                      'pull_request_closed', 'managed_worktree_scope_violation'):
            with self.subTest(cause=cause):
                # One isolated fixture per cause, without inheriting unrelated tests.
                case = rebase_block_fixture(self)
                try:
                    case.block(project=True, separate=cause)
                    original = dict(scoped_blocking.plan_block(case.connection, 'repo', 'G-047'))
                    self.assertTrue(case.resume())
                    self.assertEqual(dict(scoped_blocking.plan_block(case.connection, 'repo', 'G-047')), original)
                    row = case.connection.execute('SELECT * FROM exec_plans').fetchone()
                    self.assertEqual(row['status'], 'PLAN_BLOCKED')
                    self.assertEqual(row['blocked_reason'], cause)
                    self.assertEqual(row['last_error'], 'original block text')
                    self.assertEqual(row['review_result'], 'old review')
                    self.assertEqual(row['worker_session_id'], 'saved-worker')
                    self.assertIsNone(row['review_token'])
                    self.assertIsNone(scoped_blocking.project_block(case.connection, 'repo'))
                finally:
                    case.doCleanups()


    def test_unrelated_project_cause_is_not_released_by_rebase(self):
        self.block(project=True, reason='user_decision_required')
        before = self.snapshot()
        with mock.patch.object(loop_manager, 'validate_execplan_workspace_identity',
                               side_effect=loop_manager.WorkspaceOwnershipError('foreign owner')):
            self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.factory.assert_not_called()


    def test_planning_defers_recheck_and_other_execution_preserves_management_limit(self):
        self.block()
        for values in ("planning_error = 'separate error'", "planning_attempt_fingerprint = 'pending'",
                       "planner_session_id = 'planner'"):
            with self.subTest(values=values), self.connection:
                self.connection.execute('UPDATE projects SET ' + values)
                before = self.snapshot()
                self.assertFalse(self.resume())
                self.assertEqual(self.snapshot(), before)
                self.connection.execute("UPDATE projects SET planning_error = NULL, "
                    "planning_attempt_fingerprint = NULL, planner_session_id = NULL")
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) VALUES ('repo', 'G-048', "
            "'docs/plans/[G-048] Other.md', 'Other', '[]', '[]', 'normal', 'hash', 'IMPLEMENTING')")
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.commit()
        before = self.snapshot()
        self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.factory.assert_called_once()


    def test_dependency_gate_blocks_release_without_observation(self):
        self.block()
        self.connection.execute("UPDATE exec_plans SET depends_on = '[\"G-109\"]'")
        self.connection.commit()
        before = self.snapshot()
        self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.factory.assert_not_called()


    def test_new_project_rebase_diagnosis_needs_no_legacy_text_or_occurrence_history(self):
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        self.block(project=True, reason='publication_ownership_uncertain')
        detail = 'current owned rebase completion was misclassified'
        self.connection.execute('UPDATE project_blocks SET evidence = ?', (detail,))
        RecoveryRepository(self.connection).observe(RecoveryObservation(
            RecoveryReason.REMOTE_OID_UNCERTAIN, 'repo', 'G-047', 'WORKING',
            'old-input', 'old-base', 'old-fingerprint', 'another-session', detail),
            final_block_reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN)
        self.connection.execute("UPDATE recovery_records SET occurrence_index = '{}'")
        self.connection.commit()
        checkpoint = self.connection.execute("SELECT observed_remote_oid FROM execution_checkpoints WHERE repo='repo' AND plan_id='G-047'").fetchone()
        with mock.patch('loop_manager_internal.git.manager_git_journal.live_remote_branch_oid', return_value=checkpoint[0]):
            self.assertTrue(self.resume())
        self.assertIsNone(scoped_blocking.project_block(self.connection, 'repo'))
        self.assertEqual(RecoveryRepository(self.connection).row('repo', 'G-047')['pending_action'], 'reconcile')


    def test_unlinked_pending_rebase_project_cause_cannot_take_generic_release(self):
        self.block(project=True)
        self.connection.execute("UPDATE project_blocks SET evidence = 'unknown unrelated safety cause'")
        self.connection.commit()
        before = self.snapshot()
        with mock.patch.object(loop_manager, 'validate_managed_worktree') as generic:
            self.assertFalse(self.resume())
        generic.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(self.snapshot(), before)


    def test_remote_scope_index_and_operation_mismatches_preserve_block(self):
        from dataclasses import replace
        from loop_manager_internal.git.managed_rebase_validation import FileChange, IndexEntry
        self.block()
        original = self.case.observed
        for observed in (replace(original, remote=replace(original.remote, oid='foreign')),
                         replace(original, base_is_ancestor=False),
                         replace(original, operations=(('MERGE_HEAD', 'foreign'),)),
                         replace(original, index=(IndexEntry('app.py', '100644', 'blob', 0, 'S', 0x40000000),)),
                         replace(original, committed=(FileChange('docs/requirements/archive/change.md',
                             '100644', '100644', 'old', 'new', 'M'),))):
            with self.subTest(observed=observed):
                self.case.observed = observed
                before = self.snapshot()
                self.assertFalse(self.resume())
                self.assertEqual(self.snapshot(), before)


    def test_target_and_original_head_mismatch_reject_detached_recovery(self):
        from dataclasses import replace
        self.block()
        self.pending()
        original = self.case.observed
        for key in ('onto', 'orig-head', 'head-name'):
            self.case.observed = replace(original, metadata=tuple(
                (name, 'foreign' if name == key else value) for name, value in original.metadata))
            before = self.snapshot()
            self.assertFalse(self.resume())
            self.assertEqual(self.snapshot(), before)


    def test_reobservation_race_and_transaction_faults_roll_back_then_resume_once(self):
        from dataclasses import replace
        from loop_manager_internal.storage import runtime_state
        self.block()
        before = self.snapshot()
        original = self.case.observed
        observe = self.case.factory(None).require_owned
        calls = []
        def raced(**kwargs):
            calls.append(None)
            if len(calls) == 2:
                self.case.observed = replace(original, remote=replace(original.remote, oid='raced'))
            return observe(**kwargs)
        with mock.patch.object(loop_manager, '_current_rebase_service',
                               return_value=SimpleNamespace(require_owned=raced,
                                                            normalize_frontmatter=lambda: False)):
            self.assertFalse(self.resume())
        self.assertEqual(self.snapshot(), before)
        self.case.observed = original
        for stage in ('after_rebase_acceptance', 'after_rebase_block_release'):
            def fault(actual):
                if actual == stage:
                    raise runtime_state.RuntimeStateError('interrupted ' + stage)
            with mock.patch.object(runtime_state.RuntimeStateRepository, '_fault', side_effect=fault):
                self.assertFalse(self.resume())
            self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.resume())
        adopted = self.checkpoints.get('repo', 'G-047')
        self.assertFalse(self.resume())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), adopted)


    def test_direct_input_and_interrupted_entry_use_current_recovery_before_generic_branch_gate(self):
        self.block()
        self.pending(staged=True)
        with mock.patch.object(loop_manager, 'make_worktree', side_effect=AssertionError('generic branch gate')):
            self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                self.connection, Path('/repo'), 'repo', 'main', Path('/state')))
        self.git_write.assert_not_called()


    def test_interrupted_entry_routes_detached_state_to_same_service(self):
        self.block()
        self.pending()
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        with mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')):
            self.assertTrue(loop_manager._resume_interrupted_blocked_rebase(
                self.connection, Path('/repo'), 'repo', 'main', plan,
                Path('/state/G-047'), 'loop-manager/G-047'))
        self.git_write.assert_not_called()


    def test_direct_input_recovery_preserves_a_different_selected_block(self):
        self.block()
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status, blocked_reason, blocked_fingerprint) "
            "VALUES ('repo', 'G-048', 'docs/plans/[G-048] Other.md', 'Other', '[]', '[]', "
            "'normal', 'hash', 'PLAN_BLOCKED', 'verify_retry_exhausted', 'other-input')")
        self.connection.execute("UPDATE projects SET plan_path = 'docs/plans/[G-048] Other.md'")
        self.connection.commit()
        other = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id = 'G-048'").fetchone()
        with mock.patch.object(loop_manager, '_plan_block_selected_for_release', return_value=None) as selected:
            self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                self.connection, Path('/repo'), 'repo', 'main', Path('/state')))
        selected.assert_not_called()
        self.factory.assert_called_once()
        self.assertEqual(dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id = 'G-048'").fetchone()), dict(other))
        self.assertEqual(self.connection.execute("SELECT status FROM exec_plans WHERE plan_id = 'G-047'").fetchone()[0], 'IMPLEMENTING')
        self.assertEqual(self.connection.execute("SELECT plan_path FROM projects").fetchone()[0], other['path'])
