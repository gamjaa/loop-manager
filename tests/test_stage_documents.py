"""단계별 문서 영향 판단과 검증 근거 경계의 회귀 시나리오."""
from dataclasses import replace
import json
from unittest import TestCase
from unittest import mock
from types import SimpleNamespace

from loop_manager_internal.execution import stage_documents as documents
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.execution import ExecutionFixture, READY_WORKER_RESULT


class DocumentProtocolTests(TestCase):
    def test_verification_only_review_cannot_contain_implementation_issues(self):
        import loop_manager as lm
        self.assertEqual(lm.parse_reviewer_result('VERDICT: VERIFY_REQUIRED'), ('VERIFY_REQUIRED', ()))
        with self.assertRaises(lm.ReviewerProtocolError):
            lm.parse_reviewer_result('VERDICT: VERIFY_REQUIRED\nISSUE_ID: app.py::missing-condition')

    def test_relevance_includes_sources_and_unlisted_upper_specification(self):
        changed = ('docs/ARCHITECTURE.md', 'docs/requirements/R.md', 'docs/requirements/Other.md', 'app.py')
        self.assertEqual(documents.related_paths('docs/plans/G.md', ('docs/requirements/R.md',), changed),
                         ('docs/ARCHITECTURE.md', 'docs/requirements/R.md'))
        self.assertFalse(documents.documentary(changed))
        self.assertTrue(documents.documentary(changed[:3]))

    def test_assessment_requires_complete_unambiguous_evidence(self):
        value = dict(relevant=True, implementation='satisfied', plan_text=None,
                     evidence='현재 구현이 변경된 조건을 충족하며 검증은 별도 확인한다.')
        output = 'DOCUMENT_RESULT: ' + json.dumps(value)
        self.assertEqual(documents.parse_assessment(output), value)
        for invalid in (output + '\n' + output, 'DOCUMENT_RESULT: {}',
                        'DOCUMENT_RESULT: ' + json.dumps({**value, 'evidence': ''}),
                        'DOCUMENT_RESULT: ' + json.dumps({**value, 'relevant': False, 'implementation': 'needed'})):
            with self.subTest(invalid=invalid), self.assertRaises(PlannerValidationError):
                documents.parse_assessment(invalid)


class DocumentPhaseTests(ExecutionFixture, TestCase):
    def document_effects(self, base='base', outputs=None):
        scope = SimpleNamespace(head='base', branch='work', index={}, dirty={}, operations={})
        output = 'DOCUMENT_RESULT: ' + json.dumps(dict(relevant=True, implementation='satisfied',
            plan_text=None, evidence='현재 구현은 조건을 충족하며 검증은 새 조건에서 진행한다.'))
        effects = mock.Mock()
        effects.pr_for_branch.return_value = None
        effects.begin_scope.return_value = scope
        effects.capture_scope.return_value = scope
        effects.git.side_effect = lambda root, *args, **kw: SimpleNamespace(returncode=0,
            stdout=base if args[-1] == 'origin/main' else 'base')
        import loop_manager as lm
        effects.run_agent.side_effect = [lm.AgentResult(0, text) for text in (outputs or [output])]
        effects.run_with_retries.side_effect = lambda call, session_id: call(session_id, False)
        return effects

    def test_unlisted_upper_specification_is_reassessed_at_each_changed_base(self):
        context = self.review_context()
        current = self.handler._definition_snapshot
        checkpoints = ExecutionCheckpointRepository(self.connection)
        effects = self.document_effects()
        first = documents.refresh(effects, self.state, checkpoints, self.handler._identity,
            context, current, current, ['docs/ARCHITECTURE.md'])
        self.assertEqual(first['stage'], 'done')
        self.assertTrue(first['verification_pending'])
        next_definition = replace(current, revision_sha='new-base')
        self.state.update_project(self.repo, active_sha='new-base')
        second_effects = self.document_effects('new-base')
        second = documents.refresh(second_effects, self.state, checkpoints, self.handler._identity,
            context, current, next_definition, ['docs/ARCHITECTURE.md'])
        self.assertEqual(second['base'], 'new-base')
        second_effects.run_agent.assert_called_once()

    def test_unrelated_document_never_calls_planner(self):
        context = self.review_context()
        effects = self.document_effects()
        current = self.handler._definition_snapshot
        self.assertIsNone(documents.refresh(effects, self.state, ExecutionCheckpointRepository(self.connection),
            self.handler._identity, context, current, current, ['docs/requirements/Other.md']))
        effects.run_agent.assert_not_called()

    def test_identical_proposal_finishes_without_publication(self):
        context = self.review_context()
        current = self.handler._definition_snapshot
        output = 'DOCUMENT_RESULT: ' + json.dumps(dict(relevant=True, implementation='satisfied',
            plan_text='same', evidence='현재 계획 문서는 최신 조건과 동일하다.'))
        effects = self.document_effects(outputs=[output])
        effects.prepare_snapshot.return_value = SimpleNamespace(by_id={'G-047': SimpleNamespace(text='same')})
        item = documents.refresh(effects, self.state, ExecutionCheckpointRepository(self.connection),
            self.handler._identity, context, current, current, ['docs/ARCHITECTURE.md'])
        self.assertEqual(item['stage'], 'done')
        self.assertIsNone(item['assessment']['plan_text'])
        effects.validate_worktree.assert_not_called()
        effects.run_agent.assert_called_once()

    def test_actual_pr_discards_pre_pr_assessment_authority(self):
        checkpoints = self.changed_document('REVIEWING', 'satisfied')
        effects = self.document_effects()
        effects.pr_for_branch.return_value = {'url': 'https://example/pr/1'}
        current = self.handler._definition_snapshot
        self.assertIsNone(documents.refresh(effects, self.state, checkpoints, self.handler._identity,
            self.context(), current, current, []))
        self.assertIsNone(documents.pending(checkpoints, self.repo, 'G-047'))
        effects.run_agent.assert_not_called()

    def test_invalid_protocol_retries_without_mutating_work_or_approving_verify(self):
        context = self.review_context()
        current = self.handler._definition_snapshot
        effects = self.document_effects(outputs=['DOCUMENT_RESULT: {}'] * 3)
        checkpoints = ExecutionCheckpointRepository(self.connection)
        with self.assertRaises(PlannerValidationError):
            documents.refresh(effects, self.state, checkpoints, self.handler._identity,
                context, current, current, ['docs/ARCHITECTURE.md'])
        self.assertEqual(effects.run_agent.call_count, 3)
        self.assertEqual(documents.pending(checkpoints, self.repo, 'G-047')['stage'], 'assess')
        self.assertEqual(self.state.row()['worker_result'], 'worker result')
        self.assertEqual(self.state.row()['verify_result'], 'successful verify output')

    def test_remote_race_keeps_proposal_and_code_invalidation(self):
        from tests.support.revision import definition
        old = dict(previous=definition().serialize(), paths=['docs/PRODUCT.md'], document_only=True,
            base='old', assessment={'evidence': 'old'}, workspace='owned', commit='candidate')
        item = documents.restart_assessment(old, 'new', ['app.py', 'docs/ARCHITECTURE.md'])
        self.assertEqual(item['stage'], 'rebase')
        self.assertFalse(item['document_only'])
        self.assertIsNone(item['assessment'])
        self.assertNotIn('workspace', item)
        self.assertEqual(item['superseded'][0]['commit'], 'candidate')

    def test_work_branch_document_observation_does_not_grant_adoption(self):
        context = self.review_context()
        self.state.update_project(self.repo, definition_snapshot=self.handler._definition_snapshot.serialize())
        checkpoints = ExecutionCheckpointRepository(self.connection)
        checkpoints.record(self.repo, 'G-047', expected_generation=0, operation=None,
            expected_head='base', target_base_oid='base', observed_remote_oid='old-remote', snapshot={})
        def git(_root, *args, **kwargs):
            return SimpleNamespace(returncode=0, stdout='docs/ARCHITECTURE.md\0' if args[0] == 'diff' else '')
        documents.remember_work_branch(git, self.state, checkpoints, self.handler._identity,
            context, 'new-remote', 'base', 'base', None)
        item = documents.pending(checkpoints, self.repo, 'G-047')
        self.assertEqual(item['phase'], 'REVIEWING')
        self.assertTrue(item['work_branch'])
        self.assertEqual(item['paths'], ['docs/ARCHITECTURE.md'])
        effects = mock.Mock()
        self.assertIsNone(documents.refresh(effects, self.state, checkpoints, self.handler._identity,
            context, self.handler._definition_snapshot, self.handler._definition_snapshot, []))
        effects.run_agent.assert_not_called()

    def changed_document(self, phase, implementation):
        self.handler.guard(self.context())
        definition = self.handler._definition_snapshot
        updated = replace(definition, source_fingerprint='changed-source')
        self.state.update_project(self.repo, phase=phase, worker_result=READY_WORKER_RESULT,
            worker_session_id='saved-worker', reviewer_session_id='saved-reviewer',
            worker_input_token=definition.worker_token, archive_token=definition.archive_token,
            verify_result='old verify', validated_base_sha='base')
        self.effects.revision.source_fingerprint.return_value = 'changed-source'
        checkpoints = ExecutionCheckpointRepository(self.connection)
        documents.save(checkpoints, self.repo, 'G-047', {
            'version': 1, 'stage': 'done', 'base': 'base', 'phase': phase,
            'document_only': True, 'definition': updated.definition_token,
            'worker_result': READY_WORKER_RESULT, 'verify_result': 'old verify', 'verify_base': 'base',
            'assessment': dict(relevant=True, implementation=implementation, plan_text=None, evidence='근거'),
            'verification_pending': True,
        })
        self.effects = replace(self.effects, checkpoint=checkpoints,
            revision=replace(self.effects.revision, refresh_documents=lambda *_:
                             documents.pending(checkpoints, self.repo, 'G-047')))
        self.handler.effects = self.effects
        return checkpoints

    def test_verify_requires_worker_only_for_missing_implementation(self):
        self.changed_document('VERIFYING', 'needed')
        result = self.handler.guard(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-worker')
        self.effects.validation.verify.assert_not_called()

    def test_verify_keeps_satisfied_implementation_and_invalidates_old_success(self):
        self.changed_document('VERIFYING', 'satisfied')
        self.assertEqual(self.handler.guard(self.context()).kind, ExecutionOutcomeKind.RETRY)
        current = self.handler.guard(self.context())
        self.assertNotIsInstance(current, ExecutionOutcome)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], READY_WORKER_RESULT)
        self.assertIsNone(self.state.row()['verify_token'])
        self.effects.agent.run_agent.assert_not_called()

    def test_review_precedes_verification_and_cannot_authorize_publication(self):
        checkpoints = self.changed_document('REVIEWING', 'satisfied')
        self.effects.validation.parse_reviewer_result.return_value = ('VERIFY_REQUIRED', ())
        self.handler.guard(self.context())
        context = self.handler.guard(self.context())
        self.assertNotIsInstance(context, ExecutionOutcome)
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertEqual(self.handler.review(context).kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.calls.count('reviewer'), 1)
        self.assertNotIn('worker', self.calls)
        self.effects.validation.verify.assert_not_called()
        self.assertIsNone(self.state.row()['review_token'])
        self.assertFalse(documents.pending(checkpoints, self.repo, 'G-047')['verification_pending'])

    def test_checkpoint_rebase_transitions_preserve_pending_document_work(self):
        checkpoints = self.changed_document('REVIEWING', 'satisfied')
        before = documents.pending(checkpoints, self.repo, 'G-047')
        current = checkpoints.get(self.repo, 'G-047')
        checkpoints.record(self.repo, 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head='head', target_base_oid='new-base',
            observed_remote_oid=None, snapshot={'rebase': 'owned'})
        self.assertEqual(documents.pending(checkpoints, self.repo, 'G-047'), before)
