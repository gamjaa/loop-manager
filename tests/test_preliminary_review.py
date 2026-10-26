"""Early Review and full verify keep separate evidence and existing failure gates."""
from dataclasses import replace
import json
import hashlib
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager as lm
from loop_manager_internal.execution import preliminary_review
from loop_manager_internal.execution.result_validation import evaluate_review_worker_level
from loop_manager_internal.storage import pre_review, review_correction_state, verify_receipts
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace
from tests.support.execution import ExecutionFixture


IDENTITY = {'provider': 'codex', 'model': 'model', 'effort': 'high',
            'instruction': 'actual-instruction', 'environment': 'confirmed-environment', 'policy': 'confirmed-policy'}


class PreliminaryReviewTests(ExecutionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.review_context()
        from loop_manager_internal.verification.inputs import InputObservation
        self.effects.validation.observe_verify_inputs.return_value = InputObservation(None, reason='fixture inputs unproven')
        self.dirty = False
        self.state.update_project(self.repo, phase='VERIFYING', validated_base_sha=None,
            verify_result=None, verify_token=None, review_result=None, review_token=None)
        self.identity = dict(IDENTITY)
        self.partial_status = 'PASS'
        self.partial = mock.Mock(side_effect=self.partial_result)
        self.effects = replace(self.effects, validation=replace(self.effects.validation,
            partial_verify_enabled=lambda _: True, partial_verify=self.partial,
            pre_review_identity=lambda _, **_kwargs: self.identity))
        self.handler.effects = self.effects
        self.effects.agent.run_agent.side_effect = None
        self.effects.agent.run_agent.return_value = lm.AgentResult(0, 'VERDICT: PASS', session_id='review-session',
                                                                 isolation_invocation_id='actual-review-invocation')
        self.effects.validation.verify.side_effect = None
        self.effects.validation.verify.return_value = (True, 'actual full verify output')
        self.effects.validation.transition_validation_failure.side_effect = self.failure
        review_correction_state.begin_worker(self.connection, self.repo, 'G-047', 1, None)
        review_correction_state.complete_worker(self.connection, self.repo, 'G-047', 1)
        latest = mock.patch.object(self.handler, '_fetch_latest_base', return_value='base')
        latest.start()
        self.addCleanup(latest.stop)
        guard = mock.patch.object(self.handler, '_post_effect_guard', side_effect=lambda _: self.handler._context())
        guard.start()
        self.addCleanup(guard.stop)
        self.latest = SimpleNamespace(reconcile=mock.Mock(return_value=None))

    def partial_result(self, _worktree, base):
        output = lm.VerifyOutput('partial execution output', exit_code=0 if self.partial_status != 'FAIL' else 1)
        output.partial_result = {'kind': 'partial_verify', 'version': 1, 'status': self.partial_status,
            'duration': 0.01, 'selection': {'head': self.remote, 'base': base,
                'modules': ['test_related'], 'ids': ['test_related.Case.test_ok'],
                'fallback': ['uncertain'] if self.partial_status == 'FALLBACK' else []}}
        return self.partial_status != 'FAIL', output

    def failure(self, repo, _worktree, _stage, _output, **_kwargs):
        self.state.update_project(repo, phase='WORKING', validated_base_sha=None,
            verify_token=None, review_token=None, publication_token=None)
        return SimpleNamespace(blocked=False, fingerprint='failure')

    def verify(self):
        return self.handler.verify(self.handler._context(), self.latest)

    def review(self):
        return self.handler.review(self.handler._context())

    def saved(self):
        return pre_review.current(self.connection, self.repo, 'G-047')

    def early_pass(self):
        self.verify()
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.review()
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.saved()['status'], 'review_pass')

    def test_early_pass_full_run_and_final_approval_with_one_reviewer(self):
        self.verify()
        self.assertIsNone(self.state.row()['verify_token'])
        self.assertIsNone(self.state.row()['validated_base_sha'])
        correction = review_correction_state.load(self.connection, self.repo, 'G-047')
        self.assertIsNone(correction['candidate']['verified_head'])
        self.assertEqual(correction['candidate']['partial_head'], 'base')
        self.effects.validation.verify.assert_not_called()
        self.review()
        self.assertIsNone(self.state.row()['review_token'])
        self.verify()
        self.effects.validation.verify.assert_called_once()
        self.review()
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')
        self.assertEqual(self.saved()['status'], 'finalized_reuse')
        self.effects.agent.run_agent.assert_called_once()
        self.assertEqual(self.partial.call_count, 1)
        selection = verify_receipts.selected(self.connection, self.repo, 'G-047')
        full = verify_receipts.current_receipt(self.connection, selection)
        self.assertEqual((full['kind'], full['status']), ('run', 'success'))
        self.assertEqual(json.loads(self.saved()['partial'])['full_verify']['passed'], True)

    def test_preliminary_fail_performs_zero_full_verify_calls(self):
        self.verify()
        self.effects.agent.run_agent.return_value = lm.AgentResult(0, 'VERDICT: FAIL\nISSUE_ID: code.py|value|correctness')
        self.effects.validation.parse_reviewer_result.return_value = ('FAIL', ('issue',))
        self.review()
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.effects.validation.verify.assert_not_called()
        self.assertEqual(self.saved()['status'], 'review_fail')
        self.assertEqual(self.effects.validation.transition_validation_failure.call_args.args[2], 'review')
        self.assertIsNone(self.state.row()['review_token'])

    def test_partial_failure_uses_verify_correction_without_ai_or_full_run(self):
        self.partial_status = 'FAIL'
        self.verify()
        self.effects.validation.verify.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.saved()['status'], 'partial_failed')
        self.assertEqual(self.effects.validation.transition_validation_failure.call_args.args[2], 'verify')

    def test_uncertain_selection_falls_back_without_preliminary_reviewer(self):
        self.partial_status = 'FALLBACK'
        self.verify()
        self.assertEqual(self.saved()['status'], 'fallback')
        self.effects.validation.verify.assert_called_once()
        self.effects.agent.run_agent.assert_not_called()
        self.review()
        self.effects.agent.run_agent.assert_called_once()

    def test_unproven_environment_uses_legacy_path_without_double_review(self):
        self.identity = None
        self.verify()
        self.partial.assert_not_called()
        self.assertEqual(self.saved()['status'], 'fallback')
        self.assertIn('review_environment_identity_unproven', self.saved()['partial'])
        self.review()
        self.effects.agent.run_agent.assert_called_once()

    def test_unproven_environment_without_manifest_preserves_legacy_verification(self):
        self.identity = None
        def git_without_declaration(path, *args, **kwargs):
            if args == ('show', 'base:scripts/verify.json'):
                raise RuntimeError('verification declaration does not exist at candidate')
            return self.git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = git_without_declaration
        self.verify()
        self.partial.assert_not_called()
        self.effects.validation.verify.assert_called_once()
        self.assertEqual(self.saved()['status'], 'fallback')
        self.assertIn('verification_declaration_unavailable', self.saved()['partial'])
        self.assertIsNotNone(self.state.row()['verify_token'])
        self.review()
        self.effects.agent.run_agent.assert_called_once()

    def test_production_ports_bind_current_and_invocation_observer_to_owned_context(self):
        from pathlib import Path
        from loop_manager_internal.agents import provider_execution
        production = lm._ExecutionLifecyclePorts(self.connection, self.repository,
            repo=self.repo, repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'), newly_activated=False)
        with mock.patch.object(provider_execution, 'observe', return_value=IDENTITY) as observe, \
             mock.patch.object(lm, 'capture_git_snapshot', return_value=SimpleNamespace(serialize=lambda: 'snapshot')), \
             mock.patch.object(lm, '_agent_profile', return_value=lm.ResolvedProfile('model', 'high')) as resolve, \
             mock.patch.object(lm, 'validate_provider_runtime') as validate:
            context = self.handler._context()
            observer = production.effects.validation.pre_review_identity
            self.assertEqual(observer(context), IDENTITY)
            self.assertEqual(observer(context, invocation_id='physical-invocation'), IDENTITY)
            kwargs = observe.call_args.kwargs
            self.assertEqual(kwargs['repo'], self.repo)
            self.assertEqual(kwargs['plan_id'], context.plan.plan_id)
            self.assertEqual(kwargs['worktree'], context.worktree)
            self.assertEqual(kwargs['state_dir'], Path('/state'))
            self.assertEqual(kwargs['invocation_id'], 'physical-invocation')
            self.assertEqual(kwargs['snapshot_digest'](), hashlib.sha256(b'snapshot').hexdigest())
            kwargs['resolve_profile'](Path('/config'), 'codex', 'reviewer', None, context.worktree)
            kwargs['validate_runtime'](Path('/config'), 'codex', context.worktree)
            resolve.assert_called_once()
            validate.assert_called_once()

    def test_production_observer_completes_cycle_with_actual_adapter_and_durable_evidence(self):
        from pathlib import Path
        from tests.support.provider_execution import ProviderExecutionFixture
        from tests.support.workspace import pin_common_guidance
        from loop_manager_internal.agents.agent_invocation import InvocationContext
        class NativeFixture(ProviderExecutionFixture, unittest.TestCase):
            pass
        native = NativeFixture()
        native.plan_id = 'G-047'
        native.setUp()
        self.addCleanup(native.doCleanups)
        cycle = PreliminaryReviewTests()
        cycle.repo = native.repo
        cycle.setUp()
        self.addCleanup(cycle.doCleanups)
        cycle.handler.repo_path, cycle.handler.state_dir, cycle.handler.config_path = native.source, native.state, native.config
        cycle.state.update_project(cycle.repo, worktree=str(native.worktree))
        native.connection = cycle.connection
        pin_common_guidance(cycle.connection, InvocationContext(cycle.repo, native.plan_id),
            native.worktree, native.state, role='reviewer')
        production = lm._ExecutionLifecyclePorts(cycle.connection, cycle.repository,
            repo=cycle.repo, repo_path=native.source, state_dir=native.state,
            base='main', base_sha='base', provider='codex', config_path=native.config, newly_activated=False)
        cycle.effects = replace(cycle.effects, validation=replace(cycle.effects.validation,
            pre_review_identity=production.effects.validation.pre_review_identity))
        cycle.handler.effects = cycle.effects
        cycle.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: native.invoke()
        snapshot = lm.GitSnapshot('base', 'loop-manager/G-047', {}, {}, {}, {}, {}, {})
        with mock.patch.object(lm, '_agent_profile', side_effect=native.resolve), \
             mock.patch.object(lm, 'validate_provider_runtime', side_effect=native.validate), \
             mock.patch.object(lm, 'capture_git_snapshot', return_value=snapshot):
            cycle.early_pass()
            cycle.verify()
            cycle.review()
        self.assertEqual(cycle.state.row()['phase'], 'PUBLISHING')
        self.assertEqual(cycle.saved()['status'], 'finalized_reuse')
        cycle.effects.validation.verify.assert_called_once()
        cycle.effects.agent.run_agent.assert_called_once()
        self.assertEqual(cycle.partial.call_count, 1)
        completed = cycle.connection.execute('SELECT execution_status, execution_identity '
            'FROM agent_invocations WHERE role=? AND result_kind=?', ('reviewer', 'success')).fetchall()
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]['execution_status'], 'executed')
        self.assertIsNotNone(completed[0]['execution_identity'])

    def test_changed_reviewer_policy_requires_final_reviewer_after_full_verify(self):
        self.early_pass()
        original = self.saved()['review']
        self.identity = {**IDENTITY, 'policy': 'changed'}
        self.effects.agent.run_agent.return_value = lm.AgentResult(0, 'different final Review PASS')
        self.verify()
        self.review()
        self.assertEqual(self.effects.agent.run_agent.call_count, 2)
        self.assertEqual(self.saved()['status'], 'finalized_review')
        self.assertEqual(self.saved()['review'], original)

    def test_new_final_failure_cannot_be_overwritten_by_old_preliminary_pass(self):
        self.early_pass()
        self.identity = {**IDENTITY, 'policy': 'changed'}
        self.verify()
        self.effects.validation.parse_reviewer_result.return_value = ('FAIL', ('issue',))
        self.effects.agent.run_agent.return_value = lm.AgentResult(0, 'VERDICT: FAIL\nISSUE_ID: code.py|value|correctness')
        self.review()
        self.assertEqual(self.saved()['status'], 'final_review_failed')
        self.identity = dict(IDENTITY)
        self.assertFalse(preliminary_review.reusable(self.handler._review_handler().ports,
            self.handler._context(), self.saved()))

    def test_actual_invocation_identity_must_match_preflight_and_current_inputs(self):
        self.verify()
        self.effects = replace(self.effects, validation=replace(self.effects.validation,
            pre_review_identity=lambda _, **kwargs: ({**IDENTITY, 'instruction': 'different-executed-instruction'}
                if kwargs.get('invocation_id') else IDENTITY)))
        self.handler.effects = self.effects
        self.review()
        self.assertEqual(self.saved()['status'], 'review_invalidated')
        self.assertIsNone(self.state.row()['review_token'])

    def test_unattested_preliminary_pass_still_requires_actual_full_run(self):
        from loop_manager_internal.verification.inputs import InputObservation
        self.effects.validation.observe_verify_inputs.return_value = InputObservation('changed-inputs-requiring-preliminary')
        self.verify()
        self.effects.agent.run_agent.return_value = lm.AgentResult(0, 'VERDICT: PASS')
        self.review()
        self.assertEqual(self.saved()['status'], 'review_invalidated')
        self.verify()
        self.effects.validation.verify.assert_called_once()
        self.assertEqual(self.saved()['status'], 'review_invalidated')
        self.review()
        self.assertEqual(self.effects.agent.run_agent.call_count, 2)

    def test_preliminary_pass_requires_new_full_run_even_with_matching_old_success(self):
        from loop_manager_internal.verification.inputs import InputObservation
        observation = InputObservation('changed-inputs-requiring-preliminary')
        self.effects.validation.observe_verify_inputs.return_value = observation
        self.early_pass()
        selection = verify_receipts.selected(self.connection, self.repo, 'G-047')
        original, selection = verify_receipts.record(self.connection, selection,
            head='prior-candidate', observation=observation, kind='run', reason='earlier full run')
        verify_receipts.complete(self.connection, selection, original, passed=True, output='earlier success')
        self.connection.commit()
        self.verify()
        self.effects.validation.verify.assert_called_once()
        self.assertEqual(self.partial.call_count, 1)
        receipt = verify_receipts.current_receipt(self.connection,
            verify_receipts.selected(self.connection, self.repo, 'G-047'))
        self.assertEqual((receipt['kind'], receipt['status']), ('run', 'success'))
        self.assertNotEqual(receipt['id'], original)

    def test_full_failure_invalidates_preliminary_approval(self):
        self.early_pass()
        self.effects.validation.verify.return_value = (False, 'full failure')
        self.verify()
        self.assertEqual(self.saved()['status'], 'full_failed')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['review_token'])
        self.assertFalse(preliminary_review.reusable(self.handler._review_handler().ports,
            self.handler._context(), self.saved()))

    def test_successful_other_full_receipt_cannot_replace_approved_run(self):
        self.early_pass()
        self.verify()
        approved = self.saved()
        ports = self.handler._review_handler().ports
        context = self.handler._context()
        self.assertTrue(preliminary_review.full_evidence(ports, context, approved))
        with self.connection:
            selected = verify_receipts.selected(self.connection, self.repo, 'G-047')
            observation = self.effects.validation.observe_verify_inputs.return_value
            identifier, selected = verify_receipts.record(self.connection, selected, head='base',
                observation=observation, kind='run', reason='different invocation')
            verify_receipts.complete(self.connection, selected, identifier, passed=True,
                                     output='actual full verify output')
        self.assertFalse(preliminary_review.full_evidence(ports, context, approved))
        self.review()
        self.assertEqual(self.effects.agent.run_agent.call_count, 2)

    def test_cancelled_partial_evidence_is_incomplete_on_resume(self):
        self.partial.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.verify()
        self.assertEqual(self.saved()['status'], 'partial_running')
        self.assertIsNone(self.state.row()['verify_token'])
        self.verify()
        self.assertEqual(self.partial.call_count, 1)
        self.effects.validation.verify.assert_called_once()

    def test_partial_result_head_mismatch_is_failure_not_success(self):
        def stale(worktree, base):
            passed, output = self.partial_result(worktree, base)
            output.partial_result['selection']['head'] = 'old-head'
            return passed, output
        self.partial.side_effect = stale
        self.verify()
        self.assertEqual(self.saved()['status'], 'partial_failed')
        self.effects.validation.verify.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()
        self.assertIsNone(self.state.row()['verify_token'])

    def test_candidate_base_definition_and_runtime_changes_reject_old_approval(self):
        self.early_pass()
        original = self.saved()
        ports = self.handler._review_handler().ports
        context = self.handler._context()
        self.assertTrue(preliminary_review.reusable(ports, context, original))
        for change in ('head', 'base', 'definition', 'runtime', 'worker'):
            with self.subTest(change=change):
                patches = []
                if change == 'head':
                    self.remote = 'changed-head'
                elif change == 'base':
                    context = replace(context, active_sha='changed-base')
                elif change == 'definition':
                    old_definition = self.handler._definition_snapshot
                    self.handler._definition_snapshot = replace(old_definition, source_fingerprint='changed-sources')
                elif change == 'runtime':
                    patch = mock.patch.object(preliminary_review, 'runtime_identity', return_value='changed-runtime')
                    patch.start()
                    patches.append(patch)
                else:
                    self.state.update_project(self.repo, worker_result='changed worker result')
                self.assertFalse(preliminary_review.reusable(ports, context, original))
                for patch in patches:
                    patch.stop()
                if change == 'definition':
                    self.handler._definition_snapshot = old_definition
                self.remote = 'base'
                context = self.handler._context()
                self.state.update_project(self.repo, worker_result='worker result')

    def test_interrupted_partial_run_and_new_cycle_cannot_reuse_approval(self):
        ports = self.handler._verify_handler().ports
        context = self.handler._context()
        with self.connection:
            old = pre_review.begin(self.connection, self.repo, 'G-047', preliminary_review.candidate(ports, context))
        self.verify()
        self.partial.assert_not_called()
        self.effects.validation.verify.assert_called_once()
        with self.connection:
            verify_receipts.start_cycle(self.connection, self.repo, 'G-047')
        self.assertIsNone(self.saved())
        with self.assertRaises(CheckpointRace):
            pre_review.update(self.connection, old, 'review_pass')

    def test_publication_rechecks_preliminary_identity_and_head(self):
        self.early_pass()
        self.verify()
        self.review()
        publication = self.handler._publication_handler()
        publication._publication_workspace(self.handler.repo_path, self.handler._context().worktree,
                                           self.handler._context().branch, 'base')
        self.identity = {**IDENTITY, 'instruction': 'changed-instruction'}
        with self.assertRaisesRegex(lm.PublicationSafetyError, '선행 Review'):
            publication._publication_workspace(self.handler.repo_path, self.handler._context().worktree,
                                               self.handler._context().branch, 'base')

    def test_publication_input_change_returns_to_final_review_before_remote_mutation(self):
        self.early_pass()
        self.verify()
        self.review()
        self.identity = {**IDENTITY, 'policy': 'changed'}
        self.handler.publish(self.handler._context(), self.latest)
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertEqual(self.saved()['status'], 'final_review_pending')
        self.assertIsNone(self.state.row()['review_token'])
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.review()
        self.assertEqual(self.effects.agent.run_agent.call_count, 2)
        self.assertEqual(self.saved()['status'], 'finalized_review')

    def test_partial_correction_counts_without_fabricating_verified_head(self):
        state = {'version': 1, 'level': 1, 'closed': False, 'baseline': ['issue'],
            'failures': 1, 'issues': {'issue': 1}, 'audit': [],
            'pending': {'source': 7, 'level': 1, 'completed': True, 'verified_head': None, 'partial_head': 'head'}}
        decision = evaluate_review_worker_level(1, {}, ('issue',), state, review_level=1, candidate_head='head')
        self.assertEqual(decision.effective_level, 2)
        event = decision.correction_state['audit'][-1]
        self.assertIsNone(event['verified_head'])
        self.assertEqual(event['partial_head'], 'head')
        review_correction_state.deserialize(json.dumps(decision.correction_state))


class PreliminarySummaryTests(unittest.TestCase):
    def test_review_input_bounds_diagnostics_without_claiming_full_coverage(self):
        selection = {'head': 'candidate', 'base': 'base', 'modules': ['test_selected'],
            'ids': [f'test_selected.Case.test_{i}' for i in range(1000)],
            'total_modules': 218, 'total_tests': 4000,
            'reasons': {'test_selected': ['direct_reference', 'common_regression_precheck']},
            'analysis': {'remaining_uncertainties': {f'head:pkg/module{i}.py': ['dynamic_import:1'] for i in range(300)},
                         'not_selected_related_modules': [f'test_other{i}' for i in range(200)],
                         'scope_limits': ['document_consumption_unproven:docs/PRODUCT.md']}}
        report = {'kind': 'partial_verify', 'status': 'PASS', 'selection': selection,
                  'diagnostic': '/logs/partial/summary.json'}
        serialized = json.dumps(report)
        summary = json.loads(preliminary_review.review_summary(serialized))
        self.assertEqual(summary['coverage'], 'selected_modules_only')
        self.assertTrue(summary['full_verify_required'])
        self.assertTrue(summary['samples_are_incomplete'])
        self.assertEqual(summary['selected_tests'], 1000)
        self.assertEqual(summary['uncertain_sources'], 300)
        self.assertEqual(summary['uncertainty_categories'], {'dynamic_import': 300})
        self.assertEqual(summary['not_selected_related_count'], 200)
        self.assertLess(len(summary['uncertainty_sample']), summary['uncertain_sources'])
        self.assertEqual(summary['selection_identity'], preliminary_review.digest(selection))
        self.assertEqual(summary['diagnostic'], report['diagnostic'])
        self.assertEqual(json.dumps(report), serialized)
