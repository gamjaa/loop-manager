"""Final approval consumers use actual invocation and full verify provenance."""
import copy
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager as lm
from loop_manager_internal.execution import final_review, stage_results
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from loop_manager_internal.storage import planning_review_state, runtime_state, verify_receipts
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository, CheckpointRace
from loop_manager_internal.verification.inputs import InputObservation
from tests.support.execution import ExecutionFixture, READY_WORKER_RESULT, REVIEW_IDENTITY


class FinalReviewTests(ExecutionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.review_context()
        self.state.update_project(self.repo, definition_snapshot=self.handler._definition_snapshot.serialize())
        self.dirty = False
        self.head = 'base'
        self.raw = ':100644 100644 ' + 'a' * 40 + ' ' + 'b' * 40 + ' M\0app.py\0'
        self.base_delta = ''
        self.contracts = '100644 blob product\tdocs/PRODUCT.md\0'
        self.checkpoints = ExecutionCheckpointRepository(self.connection)
        self.identity = dict(REVIEW_IDENTITY)
        self.actual = dict(REVIEW_IDENTITY)
        self.effects = replace(self.effects, checkpoint=self.checkpoints,
            validation=replace(self.effects.validation, pre_review_identity=self.review_identity))
        self.handler.effects = self.effects
        self.effects.workspace.git.side_effect = self.observe_git
        self.effects.agent.run_agent.side_effect = None
        self.effects.agent.run_agent.return_value = lm.AgentResult(0, 'VERDICT: PASS',
            session_id='reviewer-session', isolation_invocation_id='actual-invocation')
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace()
        guard = mock.patch.object(self.handler, '_post_effect_guard', side_effect=lambda _: self.context())
        guard.start()
        self.addCleanup(guard.stop)
        self.ports = SimpleNamespace(state=self.state, checkpoint=self.checkpoints,
            identity=self.handler._identity, snapshot=self.handler._snapshot,
            _row=self.state.row, git=self.effects.workspace.git,
            observe_verify_inputs=self.effects.validation.observe_verify_inputs,
            pre_review_identity=self.review_identity)

    def set_checkpoint_snapshot(self, snapshot):
        current = self.checkpoints.get(self.repo, 'G-047')
        self.checkpoints.record(self.repo, 'G-047', expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot=snapshot)
        self.connection.commit()

    def review_identity(self, _context, *, invocation_id=None):
        if invocation_id:
            self.assertEqual(invocation_id, 'actual-invocation')
            return self.actual
        return self.identity

    def observe_git(self, path, *args, **kwargs):
        if args == ('rev-parse', 'HEAD'):
            return SimpleNamespace(stdout=self.head, returncode=0)
        if args[:2] == ('diff', '--raw') and args[-3:-1] == ('planning-base', 'new-base'):
            return SimpleNamespace(stdout=self.base_delta, returncode=0)
        if args[:2] == ('diff', '--raw'):
            return SimpleNamespace(stdout=self.raw, returncode=0)
        if args[0] == 'ls-tree':
            self.assertNotIn('AGENTS.md', args)
            return SimpleNamespace(stdout=self.contracts, returncode=0)
        return self.git(path, *args, **kwargs)

    def review(self):
        return self.handler.review(self.context())

    def approved(self):
        self.review()
        saved = final_review.current(self.ports, self.context())
        self.assertEqual(saved['status'], 'pass')
        self.assertEqual(saved['approval']['invocation_id'], 'actual-invocation')
        return saved

    def rebase(self, *, through_verify=False):
        stage_results.preserve(self.ports, self.context(), reason='rebase_start')
        self.head = 'rebased-head'
        self.remote = 'new-base'
        definition = replace(self.handler._definition_snapshot, revision_sha=self.remote)
        self.handler._definition_snapshot = definition
        row = self.state.row()
        observation = self.effects.validation.observe_verify_inputs(None)
        selection = verify_receipts.selected(self.connection, self.repo, 'G-047')
        origin = verify_receipts.success(self.connection, selection, observation)
        if not through_verify:
            verify_receipts.record(self.connection, selection, head=self.head,
                observation=observation, kind='reuse', reason='same code', origin=origin)
        self.state.update_project(self.repo, phase='VERIFYING' if through_verify else 'REVIEWING',
            active_sha=self.remote, definition_snapshot=definition.serialize(),
            validated_base_sha=None if through_verify else self.remote,
            verify_result=None if through_verify else row['verify_result'],
            review_result=None, review_token=None, publication_token=None,
            verify_token=None if through_verify else definition.verify_token(self.remote, self.head, row['verify_result']))
        planning_review_state.begin(self.connection, self.repo, purpose='full', revision=self.remote,
            input_fingerprint='planning', specification_fingerprint='specification',
            work_cycle='project-cycle', comparison_revision='base')
        planning_review_state.validate(self.connection, self.repo, self.remote, 'Unrelated document change', 'unchanged')
        planning_review_state.confirm(self.connection, self.repo, self.remote, self.remote)
        self.connection.commit()

    def test_final_pass_rebase_checkpoint_reload_and_publication_keep_original_approval_without_calls(self):
        original = self.approved()
        self.rebase()
        self.effects.agent.run_agent.reset_mock()
        # Reload the durable checkpoint repository while retaining the prepared definition.
        self.handler.effects = replace(self.effects, checkpoint=ExecutionCheckpointRepository(self.connection))
        self.assertEqual(self.review().kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        saved = final_review.current(self.ports, self.context())
        self.assertEqual(saved['approval'], original['approval'])
        self.assertEqual(saved['binding']['head'], self.head)
        self.assertTrue(final_review.publication_valid(self.ports, self.context()))
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')

    def test_changed_inputs_require_reviewer_without_repeating_verify(self):
        self.approved()
        self.rebase()
        for key in ('instruction', 'provider', 'model', 'effort', 'environment', 'policy'):
            with self.subTest(key=key):
                self.identity = {**REVIEW_IDENTITY, key: 'changed'}
                self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.identity = dict(REVIEW_IDENTITY)
        self.contracts += '100644 blob design\tdocs/ARCHITECTURE.md\0'
        self.review()
        self.assertEqual(self.effects.agent.run_agent.call_count, 2)
        self.effects.validation.verify.assert_not_called()

    def test_current_runtime_roles_and_ponytail_are_independent_of_actual_instruction(self):
        self.approved()
        for role in ('planner', 'worker', 'reviewer', 'ponytail'):
            with self.subTest(role=role), mock.patch.object(final_review.common_guidance,
                    'runtime_content_hash', return_value='changed-' + role):
                self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.assertIsNotNone(final_review.reusable(self.ports, self.context()))

    def test_changes_paths_modes_and_definition_are_never_worker_normalized(self):
        self.approved()
        original = self.raw
        for changed in (original.replace('app.py', 'renamed.py'),
                        original.replace('100644 100644', '100644 100755'),
                        original.replace('b' * 40, 'c' * 40)):
            self.raw = changed
            self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.raw = original
        self.handler._definition_snapshot = replace(self.handler._definition_snapshot, source_fingerprint='new-source')
        self.assertIsNone(final_review.reusable(self.ports, self.context()))

    def test_missing_actual_evidence_claude_prepared_or_unproven_inputs_cannot_reuse(self):
        self.approved()
        self.actual = None
        self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.actual = dict(REVIEW_IDENTITY)
        self.identity = None  # provider_execution.observe(Claude) is deliberately None.
        self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.identity = dict(REVIEW_IDENTITY)
        self.effects.validation.observe_verify_inputs.return_value = InputObservation(None, reason='missing manifest')
        self.assertIsNone(final_review.reusable(self.ports, self.context()))

    def test_new_failed_or_pending_verify_and_new_cycle_revoke_old_approval(self):
        self.approved()
        selection = verify_receipts.selected(self.connection, self.repo, 'G-047')
        identifier, selection = verify_receipts.record(self.connection, selection, head=self.head,
            observation=InputObservation('fixture-review-inputs'), kind='run', reason='new run')
        self.assertIsNone(final_review.reusable(self.ports, self.context()))
        verify_receipts.complete(self.connection, selection, identifier, passed=False, output='FAIL')
        self.assertIsNone(final_review.reusable(self.ports, self.context()))
        verify_receipts.start_cycle(self.connection, self.repo, 'G-047')
        self.assertIsNone(final_review.reusable(self.ports, self.context()))

    def test_incomplete_final_call_does_not_restore_previous_pass(self):
        original = self.approved()
        self.rebase()
        self.identity = {**REVIEW_IDENTITY, 'policy': 'changed'}
        self.effects.agent.run_agent.side_effect = RuntimeError('interrupted')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.review()
        self.identity = dict(REVIEW_IDENTITY)
        self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.assertEqual(final_review.current(self.ports, self.context())['approval'], original['approval'])

    def test_publication_reobserves_before_any_remote_effect(self):
        self.approved()
        self.identity = {**REVIEW_IDENTITY, 'policy': 'changed'}
        self.handler.publish(self.context(), self.handler)
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertIsNone(self.state.row()['review_token'])
        self.effects.publication.push_managed_publication_branch.assert_not_called()

    def test_transition_race_does_not_install_a_stale_binding(self):
        self.approved()
        self.rebase()
        original = final_review.reusable
        count = 0
        def race(*args):
            nonlocal count
            count += 1
            value = original(*args)
            if count == 1:
                final_review.invalidate(self.ports, self.context(), 'running')
            return value
        with mock.patch.object(final_review, 'reusable', side_effect=race):
            with self.assertRaises(CheckpointRace), runtime_state._transaction(self.connection):
                final_review.reconnect(self.ports, self.context())
        self.assertIsNone(self.state.row()['review_token'])

    def test_corrupt_receipt_empty_invocation_and_foreign_cycle_are_not_approvals(self):
        saved = self.approved()
        cases = [None, {}, [], {**saved, 'version': 2},
                 {**saved, 'approval': {**saved['approval'], 'invocation_id': ''}},
                 {**saved, 'approval': {**saved['approval'], 'inputs': {
                     **saved['approval']['inputs'], 'plan_id': 'G-other'}}}]
        for value in cases:
            with self.subTest(value=value), runtime_state._transaction(self.connection):
                final_review.save(self.ports, self.context(), value)
                self.assertIsNone(final_review.reusable(self.ports, self.context()))
        final_review.save(self.ports, self.context(), ['corrupt'])
        self.state.update_project(self.repo, phase='REVIEWING', review_token=None)
        self.review()
        self.assertEqual(self.effects.agent.run_agent.call_count, 2)
        self.assertEqual(final_review.current(self.ports, self.context())['status'], 'pass')

    def test_raw_stage_origin_and_final_gate_are_required_after_rebase(self):
        self.approved()
        self.rebase()
        checkpoint = self.checkpoints.get(self.repo, 'G-047')
        origins = checkpoint.snapshot['stage_result_origins']
        for key in ('review_token', 'observed_head', 'definition_snapshot', 'final_review'):
            changed = copy.deepcopy(origins)
            for item in changed.values():
                item['evidence'][key] = None
            self.set_checkpoint_snapshot({**checkpoint.snapshot, 'stage_result_origins': changed})
            self.assertIsNone(final_review.reusable(self.ports, self.context()), key)
        self.set_checkpoint_snapshot(checkpoint.snapshot)
        self.assertIsNotNone(final_review.reusable(self.ports, self.context()))

    def test_archive_delta_preserves_confirmed_planning_but_new_conditions_do_not(self):
        self.approved()
        self.rebase()
        self.connection.execute("UPDATE planning_reviews SET confirmed_revision='planning-base' WHERE slot='general'")
        self.connection.commit()
        self.base_delta = (':100644 000000 ' + 'c' * 40 + ' ' + '0' * 40
            + ' D\0docs/requirements/[R-999] Other.md\0'
            + ':000000 100644 ' + '0' * 40 + ' ' + 'c' * 40
            + ' A\0docs/requirements/archive/[R-999] Other.md\0')
        self.assertIsNotNone(final_review.reusable(self.ports, self.context()))
        self.base_delta = self.base_delta.replace('c' * 40 + ' A', 'd' * 40 + ' A')
        self.assertIsNone(final_review.reusable(self.ports, self.context()))

    def test_shared_planning_pending_and_dependency_inputs_require_review(self):
        self.approved()
        self.rebase()
        planning_review_state.begin(self.connection, self.repo, purpose='delta', revision=self.remote,
            input_fingerprint='new', specification_fingerprint='new', work_cycle='project-cycle',
            comparison_revision='base')
        self.connection.commit()
        self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.connection.execute("DELETE FROM planning_reviews WHERE slot='attempt'")
        self.connection.commit()
        self.handler._definition_snapshot = replace(self.handler._definition_snapshot, depends_on=('G-999',))
        self.assertIsNone(final_review.observe(self.ports, self.context()))

    def test_policy_change_at_last_publication_workspace_check_is_rejected(self):
        self.approved()
        self.identity = {**REVIEW_IDENTITY, 'environment': 'changed-after-entry'}
        with self.assertRaisesRegex(lm.PublicationSafetyError, '최종 Review'):
            self.handler._publication_handler()._publication_workspace(
                self.handler.repo_path, self.context().worktree, self.context().branch, self.head)

    def test_preliminary_promoted_final_approval_reconnects_without_old_head_gate(self):
        from loop_manager_internal.storage import pre_review
        self.approved()
        row = pre_review.begin(self.connection, self.repo, 'G-047', 'old-early-candidate')
        pre_review.update(self.connection, row, 'finalized_reuse')
        self.connection.commit()
        self.rebase()
        self.effects.agent.run_agent.reset_mock()
        self.review()
        self.assertEqual(pre_review.current(self.connection, self.repo, 'G-047')['status'], 'finalized_reconnected')
        self.assertTrue(final_review.publication_valid(self.ports, self.context()))
        self.effects.agent.run_agent.assert_not_called()

    def test_claude_prepared_runs_reviewer_with_reused_verify_and_preserves_supported_pass(self):
        self.approved()
        self.rebase()
        self.handler._identity = replace(self.handler._identity, provider='claude')
        self.identity = self.actual = None
        self.review()
        self.assertEqual(self.effects.agent.run_agent.call_count, 2)
        self.effects.validation.verify.assert_not_called()
        saved = final_review.current(self.ports, self.context())
        self.assertEqual(saved['status'], 'unproven')
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')
        self.assertTrue(final_review.publication_valid(self.ports, self.context()))

    def test_transitive_dependency_source_archive_and_manager_rules_are_review_inputs(self):
        self.connection.execute('''INSERT INTO exec_plans
            (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status)
            VALUES ('repo', 'G-999', 'docs/plans/[G-999] Dependency.md', 'Dependency',
                    '["docs/requirements/[R-999] Shared.md", "manager:verify-inputs"]',
                    '[]', 'normal', 'hash', 'MERGED')''')
        self.connection.commit()
        definition = replace(self.handler._definition_snapshot, depends_on=('G-999',))
        self.handler._definition_snapshot = definition
        self.state.update_project(self.repo, definition_snapshot=definition.serialize(),
            verify_token=definition.verify_token('base', self.head, self.state.row()['verify_result']))
        self.contracts += ('100644 blob dependency\tdocs/plans/archive/[G-999] Dependency.md\0'
            '100644 blob shared\tdocs/requirements/archive/[R-999] Shared.md\0')
        self.approved()
        self.assertIsNotNone(final_review.reusable(self.ports, self.context()))
        self.contracts = self.contracts.replace('blob shared', 'blob new-shared')
        self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.contracts = self.contracts.replace('blob new-shared', 'blob shared')
        with mock.patch('loop_manager_internal.catalog.manager_sources.source_content', return_value='new-rules'):
            self.assertIsNone(final_review.reusable(self.ports, self.context()))

    def test_dirty_candidate_and_invalid_contract_kind_cannot_form_an_approval(self):
        self.approved()
        self.dirty = True
        self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.dirty = False
        self.contracts = self.contracts.replace('100644', '120000')
        self.assertIsNone(final_review.reusable(self.ports, self.context()))

    def test_verify_receipt_changed_after_reviewer_cannot_be_published_as_unproven_success(self):
        original = self.effects.agent.run_agent
        def run(*args, **kwargs):
            selection = verify_receipts.selected(self.connection, self.repo, 'G-047')
            identifier, selected = verify_receipts.record(self.connection, selection, head=self.head,
                observation=InputObservation('fixture-review-inputs'), kind='run', reason='competing failed run')
            verify_receipts.complete(self.connection, selected, identifier, passed=False, output='FAIL')
            self.connection.commit()
            return lm.AgentResult(0, 'VERDICT: PASS', session_id='reviewer-session',
                isolation_invocation_id='actual-invocation')
        original.side_effect = run
        self.review()
        self.assertEqual(final_review.current(self.ports, self.context())['status'], 'invalidated')
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertIsNone(self.state.row()['review_token'])
        self.handler.publish(self.context(), self.handler)
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.effects.publication.push_managed_publication_branch.assert_not_called()

    def test_actual_invocation_must_match_the_observed_input_not_a_later_identity_read(self):
        before = final_review.observe(self.ports, self.context())
        different = {**REVIEW_IDENTITY, 'policy': 'different-during-call'}
        current = iter([dict(REVIEW_IDENTITY), different])
        def observer(_context, *, invocation_id=None):
            return different if invocation_id else next(current)
        self.ports.pre_review_identity = observer
        approval = final_review.approval(self.ports, self.context(), before,
            'actual-invocation', 'VERDICT: PASS')
        self.assertEqual(approval['status'], 'invalidated')

    def test_verify_reuse_then_final_review_and_publication_keep_all_execution_counts_zero(self):
        self.state.update_project(self.repo, worker_result=READY_WORKER_RESULT)
        original = self.approved()
        targeted = list(self.connection.execute('SELECT * FROM targeted_test_results'))
        self.rebase(through_verify=True)
        self.effects.agent.run_agent.reset_mock()
        latest = SimpleNamespace(reconcile=mock.Mock(return_value=None))
        self.assertEqual(self.handler.verify(self.context(), latest).kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertIsNone(self.state.row()['review_token'])
        self.assertEqual(self.review().kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertTrue(final_review.publication_valid(self.ports, self.context()))
        saved = final_review.current(self.ports, self.context())
        self.assertEqual(saved['approval'], original['approval'])
        self.assertEqual(saved['binding']['verify_origin'], original['approval']['inputs']['verify_origin'])
        self.assertEqual(list(self.connection.execute('SELECT * FROM targeted_test_results')), targeted)
        self.effects.agent.run_agent.assert_not_called()  # Worker and Reviewer use this shared port.
        self.effects.validation.verify.assert_not_called()

    def test_identical_runtime_content_relocation_reuses_but_new_ponytail_requires_review(self):
        guidance = final_review.common_guidance
        original = self.approved()
        with tempfile.TemporaryDirectory() as directory:
            relocated = Path(directory)
            for relative in guidance.GUIDANCE_PATHS:
                target = relocated / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((guidance.RUNTIME_ROOT / relative).read_bytes())
            with mock.patch.object(guidance, 'RUNTIME_ROOT', relocated):
                self.assertIsNotNone(final_review.reusable(self.ports, self.context()))
                ponytail = relocated / guidance.PONYTAIL_PATH
                ponytail.write_bytes(ponytail.read_bytes() + b'\nAdditional runtime instruction.\n')
                self.assertIsNone(final_review.reusable(self.ports, self.context()))
        self.assertEqual(final_review.current(self.ports, self.context())['approval'], original['approval'])

    def test_known_actual_policy_mismatch_returns_to_review_without_approval_token(self):
        self.actual = {**REVIEW_IDENTITY, 'policy': 'actual-call-used-another-policy'}
        self.review()
        self.assertEqual(final_review.current(self.ports, self.context())['status'], 'invalidated')
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertIsNone(self.state.row()['review_token'])
        self.effects.agent.run_agent.assert_called_once()

    def terminal_restart(self, state='MERGED'):
        """Restore persisted approval into a new handler before definition loading."""
        from loop_manager_internal.execution.execution_state import ExecutionStatePort
        saved = self.approved()
        self.state.update_project(self.repo, pr_url='https://example/pr/1',
            pr_head_oid=self.head, publication_remote_oid=self.head)
        identity = self.handler._identity
        effects = replace(self.effects,
            state=ExecutionStatePort(self.connection, self.repository, self.repo),
            checkpoint=ExecutionCheckpointRepository(self.connection))
        fresh = type(self.handler)(effects, **vars(identity))
        fresh.plan = fresh._cached_plan(self.repository.active_row(self.repo))
        self.assertIsNone(fresh._snapshot()._definition_snapshot)
        pr = {'url': 'https://example/pr/1', 'state': state,
            'headRefName': self.context().branch, 'headRefOid': self.head}
        effects.publication.pr_for_branch.side_effect = [{**pr, 'state': 'OPEN'}, pr, pr]
        effects.workspace.live_remote_branch_oid.return_value = None  # Deleted after Merge/Close.
        effects.publication.reconcile_existing_pull_request.return_value = 0 if state == 'MERGED' else 1
        # Model a terminal transition after the initial PUBLISHING PR query.
        self.assertEqual(effects.publication.pr_for_branch(
            identity.repo_path, self.context().branch, pr['url'])['state'], 'OPEN')
        effects.agent.run_agent.reset_mock()
        effects.workspace.validate_managed_worktree.reset_mock()
        effects.validation.observe_verify_inputs.reset_mock()
        return fresh, saved, pr

    def test_terminal_pr_after_real_handler_restart_reconciles_persistent_pass_before_definition(self):
        fresh, saved, pr = self.terminal_restart()
        result = fresh.guard(fresh._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertIsNone(fresh._snapshot()._definition_snapshot)
        reconciled = fresh.effects.publication.reconcile_existing_pull_request.call_args.args[-1]
        self.assertEqual(reconciled, pr)
        self.assertEqual(fresh.effects.workspace.validate_managed_worktree.call_count, 2)
        self.assertEqual(fresh.effects.checkpoint.get(self.repo, 'G-047').snapshot['final_review'], saved)
        fresh.effects.blocking.block_active_execplan.assert_not_called()
        fresh.effects.revision.reconcile_catalog.assert_not_called()
        fresh.effects.validation.observe_verify_inputs.assert_not_called()
        fresh.effects.agent.run_agent.assert_not_called()
        fresh.effects.publication.push_managed_publication_branch.assert_not_called()

    def test_closed_pr_restart_keeps_existing_reconciliation_outcome_without_definition(self):
        fresh, _, pr = self.terminal_restart('CLOSED')
        result = fresh.guard(fresh._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(fresh.effects.publication.reconcile_existing_pull_request.call_args.args[-1], pr)
        fresh.effects.blocking.block_active_execplan.assert_not_called()
        fresh.effects.validation.observe_verify_inputs.assert_not_called()
        self.assertIsNone(fresh._snapshot()._definition_snapshot)

    def test_terminal_pr_restart_rejects_mismatched_pr_head(self):
        fresh, _, pr = self.terminal_restart()
        changed = {**pr, 'headRefOid': 'another-candidate'}
        fresh.effects.publication.pr_for_branch.side_effect = [changed, changed]
        self.assertEqual(fresh.guard(fresh._context()).kind, ExecutionOutcomeKind.FINAL_BLOCK)
        fresh.effects.publication.reconcile_existing_pull_request.assert_not_called()
        fresh.effects.blocking.block_active_execplan.assert_called_once()

    def test_terminal_pr_restart_rejects_head_race_across_pr_query(self):
        fresh, _, pr = self.terminal_restart()
        queries = 0
        def lookup(*_args):
            nonlocal queries
            queries += 1
            if queries == 2:
                self.head = 'changed-during-terminal-query'
            return pr
        fresh.effects.publication.pr_for_branch.side_effect = lookup
        self.assertEqual(fresh.guard(fresh._context()).kind, ExecutionOutcomeKind.FINAL_BLOCK)
        fresh.effects.publication.reconcile_existing_pull_request.assert_not_called()
        fresh.effects.blocking.block_active_execplan.assert_called_once()

    def test_terminal_pr_restart_rechecks_workspace_ownership_after_pr_query(self):
        fresh, _, _ = self.terminal_restart()
        fresh.effects.workspace.validate_managed_worktree.side_effect = [
            None, lm.WorkspaceOwnershipError('ownership changed during terminal query')]
        self.assertEqual(fresh.guard(fresh._context()).kind, ExecutionOutcomeKind.FINAL_BLOCK)
        fresh.effects.publication.reconcile_existing_pull_request.assert_not_called()
        self.assertEqual(fresh.effects.workspace.validate_managed_worktree.call_count, 2)
        fresh.effects.blocking.block_active_execplan.assert_called_once()

    def test_default_publication_observation_keeps_approval_checks_after_restart(self):
        fresh, _, _ = self.terminal_restart()
        fresh._definition_snapshot = self.handler._definition_snapshot
        self.identity = {**REVIEW_IDENTITY, 'policy': 'new-publication-policy'}
        context = fresh._context()
        with self.assertRaisesRegex(lm.PublicationSafetyError, '최종 Review'):
            fresh._publication_resume().observe(context.repo_path, context.worktree,
                context.branch, self.head, context.pr_url)
        fresh.effects.publication.reconcile_existing_pull_request.assert_not_called()
        fresh.effects.publication.push_managed_publication_branch.assert_not_called()
