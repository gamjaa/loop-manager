"""Narrow verify policy checks; all process, Agent and Git effects are fakes."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.support.execution import ExecutionFixture, READY_WORKER_RESULT, execution_fixture
from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.execution.execution_revision import ExecPlanBodyHash
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from loop_manager_internal.storage import verify_receipts
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace
from loop_manager_internal.verification import inputs


class CleanRebaseFixtureTest(unittest.TestCase):
    def test_publication_fixture_seeds_original_body_and_current_stage_tokens(self):
        import loop_manager
        from tests.support.publication import seed_validated_candidate
        from loop_manager_internal.execution.execution_revision import DefinitionSnapshot

        fixture = execution_fixture(self)
        body = ExecPlanBodyHash('---\nid: G-047\n---\n## Purpose\nImplement\n## Progress\n- [x] Unit check\n')
        with mock.patch('tests.support.publication.git_command', return_value='base'), \
                mock.patch.object(loop_manager, '_revision_execplan_body_hash', return_value=body), \
                mock.patch.object(loop_manager, '_revision_execplan_source_fingerprint', return_value='source'), \
                mock.patch('tests.support.publication.plan_content_hash', return_value='guidance'):
            snapshot = seed_validated_candidate(fixture.connection, Path(fixture.repo),
                                               Path('worktree'), 'G-047')
        row = fixture.connection.execute('SELECT * FROM projects WHERE repo=?',
                                         (fixture.repo,)).fetchone()
        stored = DefinitionSnapshot.deserialize(row['definition_snapshot'])
        self.assertEqual(stored.body_text, body.body_text)
        self.assertEqual(stored.worker_token, row['worker_input_token'])
        self.assertEqual(stored.verify_token('base', 'base', row['verify_result']), row['verify_token'])
        self.assertEqual(stored.review_token('base', 'base'), row['review_token'])
        self.assertNotEqual(replace(snapshot, body_text=None).verify_token(
            'base', 'base', row['verify_result']), row['verify_token'])

    def test_rebase_facade_forwards_worker_definition_and_legacy_completion(self):
        handler = object.__new__(handlers.ExecutionHandlers)
        rebase = mock.Mock()
        handler._rebase_handler = mock.Mock(return_value=rebase)
        context, definition = object(), object()
        values = {'active_sha': 'new-base'}
        result = handler._complete_rebase_intent(context, project_values=values,
                                               worker_definition=definition)
        rebase._complete_rebase_intent.assert_called_once_with(
            context, project_values=values, worker_definition=definition)
        self.assertIs(result, rebase._complete_rebase_intent.return_value)
        handler._complete_rebase_intent(context)
        rebase._complete_rebase_intent.assert_called_with(
            context, project_values=None, worker_definition=None)

    def test_clean_rebase_publication_evidence_uses_explicit_case_results(self):
        import loop_manager
        from tests.support.worker import worker_result

        plan = {'id': 'G-182', 'title': '동일 입력의 verify 성공 재사용과 rebase 후 선택'}
        title = 'verify receipt CAS와 rebase Worker 보존 연결 및 fixture repository 충돌 해소'
        summary = '입력 관측과 receipt CAS를 연결하고 선택 repository의 Worker 결과를 보존한다.'
        previous = '기존 clean rebase의 Worker·session·실패 감사 보존: 통과.'
        with self.assertRaisesRegex(loop_manager.WorkerResultError, '해결되지 않은 targeted'):
            loop_manager.parse_worker_result(worker_result(title, summary, previous), plan)

        evidence = (
            'tests.test_verify_receipts.CleanRebaseFixtureTest.'
            'test_clean_rebase_retains_worker_for_selected_repository: PASS\n'
            'tests.test_verify_receipts.VerifyReceiptPolicyTest.'
            'test_clean_base_change_preserves_worker_and_session_but_invalidates_review: PASS\n'
            'tests.test_verify_receipts.VerifyReceiptPolicyTest.'
            'test_pending_worker_correction_is_not_skipped_by_clean_rebase: PASS'
        )
        publication = loop_manager.parse_worker_result(worker_result(title, summary, evidence), plan)
        self.assertEqual(publication.test_evidence, evidence)
        # Command pairs alone are incomplete: the result contract requires an
        # explicit initial outcome between FAILED_COMMAND and its RERUN.
        commands = ('python3 -m unittest tests.test_verify_inputs',
                    'python3 -m unittest tests.test_verify_receipts')
        incomplete = '\n'.join(
            f'FAILED_COMMAND: {command}\nRERUN: {command}\n2 tests, PASS.'
            for command in commands)
        with self.assertRaisesRegex(loop_manager.WorkerResultError, '해결되지 않은 targeted'):
            loop_manager.parse_worker_result(worker_result(title, summary, incomplete), plan)
        recovered = '\n'.join(
            f'FAILED_COMMAND: {command}\nInitial result: FAIL.\nRERUN: {command}\n2 tests, PASS.'
            for command in commands)
        publication = loop_manager.parse_worker_result(worker_result(title, summary, recovered), plan)
        self.assertEqual(publication.test_evidence, recovered)

    def test_clean_rebase_retains_worker_for_selected_repository(self):
        fixture = execution_fixture(self, repo='selected-repo')
        fixture.handler.guard(fixture.context())
        definition = fixture.handler._definition_snapshot
        fixture.dirty = False
        fixture.state.update_project(fixture.repo, phase='VERIFYING',
            worker_result=READY_WORKER_RESULT, worker_session_id='saved-session',
            worker_input_token=definition.worker_token, archive_token=definition.archive_token)
        fixture.remote = 'new-base'

        result = fixture.handler.guard(fixture.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        row = fixture.state.row()
        self.assertEqual((row['active_sha'], row['phase']), ('new-base', 'VERIFYING'))
        self.assertEqual(row['worker_result'], READY_WORKER_RESULT)
        self.assertEqual(row['worker_session_id'], 'saved-session')
        self.assertEqual(fixture.calls.count('rebase'), 1)
        self.assertNotIn('worker', fixture.calls)
        self.assertIsNone(fixture.connection.execute(
            "SELECT repo FROM projects WHERE repo = 'repo'",
        ).fetchone())


class VerifyReceiptPolicyTest(ExecutionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts/verify-inputs.json').write_text(json.dumps({'version': 1, 'paths': ['src/**']}))
        (self.root / 'src').mkdir()
        self.source = self.root / 'src/app.py'
        self.source.write_text('initial')
        self.environment = {'MODE': 'test'}
        self.git_inputs = {}
        self.observer = mock.Mock(side_effect=lambda _: inputs.observe(self.root,
            command=('measured-verify',), environment=self.environment,
            runtime='measured-runtime', git_inputs=self.git_inputs, external_inputs={}))
        self.handler.effects = replace(self.effects, validation=replace(self.effects.validation,
            observe_verify_inputs=self.observer))
        self.handler.guard(self.context())
        self.definition = self.handler._definition_snapshot
        self.dirty = False
        self.ready()

    def ready(self):
        self.state.update_project('repo', phase='VERIFYING', worker_result=READY_WORKER_RESULT,
            worker_input_token=self.definition.worker_token, archive_token=self.definition.archive_token,
            validated_base_sha=None, verify_token=None, review_result=None, review_token=None)

    def audit(self):
        self.repository.update_plan('repo', 'G-047',
            plan_values={'retry_count': 2, 'verify_failure_history': '{"past": 1}'}, enqueue=False)
        self.state.expected = self.repository.projected('repo')

    def verify(self):
        return self.handler.verify(self.context(), self.handler)

    def receipts(self):
        return [dict(row) for row in self.connection.execute('SELECT * FROM verify_receipts ORDER BY rowid')]

    def test_reviewer_receipt_proof_distinguishes_run_reuse_and_waiver(self):
        observation = self.observer(self.root)
        verify_receipts.start_cycle(self.connection, 'repo', 'G-047')
        selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
        run_id, selection = verify_receipts.record(
            self.connection, selection, head='original-head', observation=observation,
            kind='run', reason='no prior success',
        )
        verify_receipts.complete(
            self.connection, selection, run_id, passed=True, output='',
        )
        self.assertEqual(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'original-head', '', observation,
        )['kind'], 'run')
        ignored_artifacts = inputs.InputObservation(
            observation.fingerprint,
            observation.files,
            (('logs/verify.log', 'file', 0o644, 'generated'),),
            'changed-completeness',
            'unconfirmed inputs outside manifest',
        )
        self.assertEqual(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'original-head', '', ignored_artifacts,
        )['kind'], 'run')
        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'original-head', '',
        ))
        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'original-head', '',
            inputs.InputObservation(None, reason='unconfirmed native loader inputs'),
        ))
        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'original-head', '',
            inputs.InputObservation('changed-inputs'),
        ))
        unproven_changed_inputs = inputs.InputObservation(
            'changed-inputs',
            observation.files,
            observation.uncovered,
            observation.completeness_token,
            'verify command or runtime inputs are unconfirmed',
        )
        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'original-head', '',
            unproven_changed_inputs,
        ))
        origin = verify_receipts.success(
            self.connection, selection, observation,
        )
        reused_id, _selection = verify_receipts.record(
            self.connection, selection, head='current-head', observation=observation,
            kind='reuse', origin=origin, reason='same proven inputs',
        )

        reused = verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'current-head', '', observation,
        )
        self.assertEqual(reused['kind'], 'reuse')
        self.assertEqual(reused['receipt']['id'], reused_id)
        self.assertEqual(reused['origin']['id'], run_id)
        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'current-head', '',
        ))
        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'original-head', '', observation,
        ))
        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'other-head', '', observation,
        ))
        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'current-head', '',
            inputs.InputObservation('different-inputs'),
        ))

        unobservable = inputs.InputObservation(
            None, reason='verify input fingerprint unavailable',
        )
        verify_receipts.start_cycle(self.connection, 'repo', 'G-047')
        selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
        unobservable_id, selection = verify_receipts.record(
            self.connection, selection, head='unobserved-head',
            observation=unobservable, kind='run', reason='inputs unavailable',
        )
        verify_receipts.complete(
            self.connection, selection, unobservable_id, passed=True, output='',
        )
        self.assertEqual(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'unobserved-head', '', unobservable,
        )['kind'], 'run')

        self.assertIsNone(verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'unobserved-head', '',
            inputs.InputObservation(None, reason='unconfirmed native loader inputs'),
        ))

        verify_receipts.start_cycle(self.connection, 'repo', 'G-047')
        selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
        waiver_id, selection = verify_receipts.record(
            self.connection, selection, head='current-head', observation=observation,
            kind='run', reason='user waiver candidate',
        )
        verify_receipts.complete(
            self.connection, selection, waiver_id, passed=True,
            output='SKIPPED_BY_USER @current-head; user choice', executed=False,
        )
        waiver = verify_receipts.candidate_evidence(
            self.connection, 'repo', 'G-047', 'current-head',
            'SKIPPED_BY_USER @current-head; user choice', observation,
        )
        self.assertEqual(waiver['kind'], 'waiver')
        self.assertNotEqual(waiver['receipt']['status'], 'success')

    def test_proven_success_reuses_original_run_without_new_invocation_or_audit(self):
        self.verify()
        self.ready()
        self.audit()
        # Reuse is selected from SQLite against a new candidate SHA.
        original_git = self.effects.workspace.git.side_effect
        self.effects.workspace.git.side_effect = lambda path, *args, **kwargs: (
            type('Result', (), {'stdout': 'new-head', 'returncode': 0})()
            if args == ('rev-parse', 'HEAD') else original_git(path, *args, **kwargs))
        self.verify()
        self.assertEqual(self.calls.count('verify'), 1)
        run, reused = self.receipts()
        self.assertEqual((run['kind'], reused['kind']), ('run', 'reuse'))
        self.assertEqual(reused['origin_id'], run['id'])
        self.assertEqual(reused['head'], 'new-head')
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertEqual(self.repository.active_row('repo')['verify_failure_history'], '{"past": 1}')
        self.assertIsNone(self.state.row()['review_token'])
        self.assertEqual(self.state.row()['verify_token'],
                         self.definition.verify_token('base', 'new-head', 'OK'))

    def test_changed_file_environment_git_or_uncovered_input_runs_again(self):
        self.verify()
        for change in (
            lambda: self.source.write_text('changed'),
            lambda: self.environment.update(MODE='new'),
            lambda: self.git_inputs.update(HEAD='revision-dependent'),
            lambda: (self.root / 'new-config.json').write_text('uncovered'),
            lambda: (self.root / inputs.MANIFEST).write_text('invalid'),
        ):
            change()
            self.ready()
            before = self.calls.count('verify')
            self.verify()
            self.assertEqual(self.calls.count('verify'), before + 1)
        self.assertTrue(all(row['kind'] == 'run' for row in self.receipts()))

    def test_default_script_adapter_does_not_guess_dynamic_input_identities(self):
        from loop_manager_internal.execution.result_validation import observe_verify_inputs
        observed = observe_verify_inputs(self.root)
        self.assertFalse(observed.proven)
        self.assertIn('unconfirmed', observed.reason)

    def test_production_ports_reuse_measured_contract_and_run_unknown_scripts(self):
        import loop_manager
        from types import SimpleNamespace
        from loop_manager_internal.execution import result_validation
        from loop_manager_internal.verification import manager_adapter
        script = self.root / 'scripts/verify.sh'
        script.write_bytes(manager_adapter.ISOLATED_COMPILE_SCRIPT)
        (self.root / inputs.MANIFEST).write_text(json.dumps(
            {'version': 1, 'paths': ['src/**', 'scripts/**']}))
        self.state.update_project('repo', worktree=str(self.root))
        production = loop_manager._ExecutionLifecyclePorts(
            self.connection, self.repository, repo='repo', repo_path=Path('/repo'),
            state_dir=Path('/state'), base='main', base_sha='base', provider='codex',
            config_path=Path('/config'), newly_activated=False)
        self.state = production.effects.state
        self.handler.effects = replace(self.handler.effects, state=self.state,
            validation=replace(self.handler.effects.validation,
                observe_verify_inputs=production.effects.validation.observe_verify_inputs))
        with mock.patch.object(result_validation, 'os', SimpleNamespace(name='posix', environ={})), \
                mock.patch.object(manager_adapter, 'os', SimpleNamespace(name='posix')), \
                mock.patch.object(manager_adapter.shutil, 'which', return_value=str(self.root / 'python')), \
                mock.patch.object(manager_adapter.subprocess, 'run',
                    return_value=type('Result', (), {'stdout': 'a' * 64})()) as probe:
            self.verify()
            self.ready()
            self.verify()
            self.assertEqual(self.calls.count('verify'), 1)
            self.assertEqual(self.receipts()[-1]['kind'], 'reuse')
            self.assertTrue(probe.called)
            self.assertEqual(probe.call_args.args[0][1:5], ['-I', '-S', '-B', '-c'])
            probe.return_value.stdout = 'b' * 64
            self.ready()
            self.verify()
            self.assertEqual(self.calls.count('verify'), 2)
            script.write_bytes(manager_adapter.ISOLATED_COMPILE_SCRIPT + b'# custom command\n')
            self.ready()
            before = probe.call_count
            self.verify()
            self.assertEqual(self.calls.count('verify'), 3)
            self.assertEqual(probe.call_count, before)
        script.write_bytes(manager_adapter.ISOLATED_COMPILE_SCRIPT)
        with mock.patch.object(result_validation, 'os', SimpleNamespace(name='nt', environ={})), \
                mock.patch.object(manager_adapter, 'os', SimpleNamespace(name='nt')), \
                mock.patch.object(result_validation.shutil, 'which', return_value=None), \
                mock.patch.object(manager_adapter.subprocess, 'run') as probe:
            for _ in range(2):
                self.ready()
                self.verify()
            self.assertEqual(self.calls.count('verify'), 5)
            self.assertTrue(all(row['kind'] == 'run' for row in self.receipts()[-2:]))
            probe.assert_not_called()

    def test_later_completeness_proof_does_not_promote_an_unproven_original_run(self):
        (self.root / 'excluded.json').write_text('outside')
        self.verify()
        original = self.observer(None)
        self.assertFalse(original.proven)
        self.observer.side_effect = lambda _: inputs.observe(self.root,
            command=('measured-verify',), environment=self.environment, runtime='measured-runtime',
            git_inputs=self.git_inputs, external_inputs={}, completeness=original.completeness_token)
        self.assertTrue(self.observer(None).proven)
        self.ready()
        self.verify()
        self.assertEqual(self.calls.count('verify'), 2)
        self.ready()
        self.verify()
        self.assertEqual(self.calls.count('verify'), 2)

    def test_inputs_changed_during_success_are_not_adopted(self):
        self.effects.validation.verify.side_effect = lambda _: (self.source.write_text('mutated') or True, 'OK')
        result = self.verify()
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertIsNone(self.state.row()['verify_token'])
        self.assertEqual(self.receipts()[0]['status'], 'failure')

    def test_new_failure_revokes_old_success_and_requires_another_run(self):
        self.verify()
        self.source.write_text('changed')
        self.ready()
        self.effects.validation.verify.side_effect = lambda _: (self.calls.append('verify') or False, 'failed')
        self.effects.validation.transition_validation_failure.return_value = type('Decision', (), {
            'blocked': False, 'fingerprint': 'failure'})()
        self.verify()
        self.assertEqual(self.receipts()[-1]['status'], 'failure')
        self.source.write_text('initial')
        self.ready()
        self.effects.validation.verify.side_effect = lambda _: (self.calls.append('verify') or True, 'OK')
        self.verify()
        self.assertEqual(self.calls.count('verify'), 3)
        self.assertTrue(all(row['kind'] == 'run' for row in self.receipts()))

    def test_interrupted_run_and_new_cycle_cannot_reuse_old_success(self):
        self.verify()
        selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
        with self.connection:
            _, pending = verify_receipts.record(self.connection, selection,
                head='base', observation=self.observer(None), kind='run', reason='started')
        self.assertIsNone(verify_receipts.success(self.connection, pending, self.observer(None)))
        self.ready()
        self.verify()
        self.assertEqual(self.calls.count('verify'), 2)
        with self.connection:
            verify_receipts.start_cycle(self.connection, 'repo', 'G-047')
        self.ready()
        self.verify()
        self.assertEqual(self.calls.count('verify'), 3)

    def test_receipt_survives_database_reopen_and_rejects_other_scopes(self):
        self.verify()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite'
            with closing(sqlite3.connect(path)) as target, target:
                self.connection.backup(target)
            reopened = sqlite3.connect(path)
            try:
                reopened.row_factory = sqlite3.Row
                selected = verify_receipts.selected(reopened, 'repo', 'G-047')
                original = verify_receipts.success(reopened, selected, self.observer(None))
                self.assertIsNotNone(original)
                for changed in ({'repo': 'other'}, {'plan_id': 'G-999'}, {'cycle': 'other-cycle'}):
                    with self.subTest(changed=changed):
                        self.assertIsNone(verify_receipts.success(reopened, {**selected, **changed}, self.observer(None)))
            finally:
                reopened.close()

    def test_receipt_cas_and_save_failure_do_not_install_stale_success(self):
        self.verify()
        selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
        with self.connection:
            identifier, current = verify_receipts.record(self.connection, selection,
                head='base', observation=self.observer(None), kind='run', reason='first')
        with self.connection:
            verify_receipts.record(self.connection, current, head='other',
                observation=self.observer(None), kind='run', reason='competitor')
        with self.assertRaises(CheckpointRace), self.connection:
            verify_receipts.complete(self.connection, current, identifier, passed=True, output='old')
        self.ready()
        with mock.patch.object(verify_receipts, 'complete', side_effect=RuntimeError('storage failure')):
            with self.assertRaisesRegex(RuntimeError, 'storage failure'):
                self.verify()
        self.assertEqual(self.receipts()[-1]['status'], 'pending')
        self.assertIsNone(verify_receipts.success(self.connection,
            verify_receipts.selected(self.connection, 'repo', 'G-047'), self.observer(None)))

    def test_reuse_selection_race_and_input_race_are_not_accepted(self):
        self.verify()
        self.ready()
        original = self.observer.side_effect
        count = 0
        def change(_):
            nonlocal count
            count += 1
            if count == 2:
                self.source.write_text('race')
            return original(None)
        self.observer.side_effect = change
        result = self.verify()
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertIsNone(self.state.row()['verify_token'])
        self.assertEqual(len(self.receipts()), 1)

    def test_review_gate_reobserves_reused_environment_without_invalidating_worker(self):
        self.verify()
        self.ready()
        self.verify()
        self.environment['MODE'] = 'changed-after-reuse'
        result = self.handler.guard(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], READY_WORKER_RESULT)
        self.assertIsNone(self.state.row()['verify_token'])

    def test_pending_worker_correction_is_not_skipped_by_clean_rebase(self):
        self.state.update_project('repo', phase='WORKING', last_error='verify correction required')
        self.remote = 'new-base'
        self.handler.guard(self.context())
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['worker_result'])

    def test_clean_rebase_candidate_requires_verification_without_repeating_worker(self):
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.handler.effects = replace(self.handler.effects, checkpoint=checkpoint)
        current = checkpoint.get('repo', 'G-047')
        checkpoint.record('repo', 'G-047', expected_generation=current.generation,
            operation=None, expected_head='old-head', target_base_oid='base',
            observed_remote_oid=None, snapshot={
                'current_rebase_acceptance': {'head': 'old-head'},
                'worker_candidate_token': self.definition.worker_candidate_token('old-head')})
        self.remote = 'new-base'
        self.handler.guard(self.context())
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertIsNotNone(self.state.row()['worker_result'])
        self.handler._complete_rebase_intent.assert_called_once()
        self.assertEqual(self.handler._complete_rebase_intent.call_args.kwargs['worker_definition'].worker_token, self.definition.worker_token)

    def test_clean_rebase_with_changed_source_preserves_worker_and_rechecks_verify(self):
        self.effects.revision.source_fingerprint.return_value = 'new-requirement-content'
        self.remote = 'new-base'
        self.handler.guard(self.context())
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertIsNotNone(self.state.row()['worker_result'])
        self.assertIsNotNone(self.handler._complete_rebase_intent.call_args.kwargs['worker_definition'])

    def test_waiver_is_not_recorded_as_an_actual_verify_success(self):
        manual = mock.Mock()
        manual.enabled.return_value = False
        manual.user_submission.return_value = True
        manual.waive.return_value = 'source verify waiver'
        with mock.patch('loop_manager_internal.execution.manual_candidate.get_manual', return_value=manual):
            self.verify()
        self.assertNotIn('verify', self.calls)
        row = self.receipts()[0]
        self.assertEqual((row['kind'], row['status']), ('waiver', 'waived'))
        self.assertIsNone(verify_receipts.success(self.connection,
            verify_receipts.selected(self.connection, 'repo', 'G-047'), self.observer(None)))

    def test_clean_worker_binding_uses_the_accepted_head_and_checkpoint_cas(self):
        from tests.support.rebase_observations import expectation, observation, decision_for
        from loop_manager_internal.git.managed_rebase_validation import Remote
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        checkpoint = ExecutionCheckpointRepository(self.connection)
        expected = expectation(repo='repo', plan_id='G-047',
            plan_path=self.state.row()['plan_path'], worktree=self.state.row()['worktree'],
            branch=self.state.row()['branch'], generation=0,
            remote=Remote('owner/repo', self.state.row()['branch'], 'remote'))
        acceptance = decision_for(expected, observation(expected)).acceptance
        checkpoint.accept_current_rebase('repo', 'G-047', acceptance=acceptance)
        prior = self.state.row()
        self.state.update_project('repo', phase='WORKING', worker_result=None,
            worker_input_token=None, archive_token=None, active_sha='base')
        self.state.retain_clean_rebase_worker(prior, self.definition, checkpoint)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], READY_WORKER_RESULT)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['worker_candidate_token'],
                         self.definition.worker_candidate_token(acceptance['head']))

    def test_clean_base_change_preserves_worker_and_session_but_invalidates_review(self):
        # Match the production script adapter: unmeasured inputs require verify
        # again even though a clean rebase retains the completed Worker.
        self.observer.side_effect = lambda _: inputs.InputObservation(
            None, reason='unconfirmed runtime/Git/external inputs')
        self.verify()
        self.state.update_project('repo', worker_session_id='saved',
                                  review_result='VERDICT: PASS', review_token='old-review')
        self.audit()
        self.remote = 'new-base'
        result = self.handler.guard(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'VERIFYING')
        self.assertEqual(row['worker_result'], READY_WORKER_RESULT)
        self.assertEqual(row['worker_session_id'], 'saved')
        self.assertEqual(self.repository.active_row('repo')['retry_count'], 2)
        self.assertEqual(self.repository.active_row('repo')['verify_failure_history'], '{"past": 1}')
        self.assertIsNone(row['verify_token'])
        self.assertIsNone(row['review_token'])
        self.assertNotIn('worker', self.calls)
        self.verify()
        self.assertEqual(self.calls.count('verify'), 2)
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertNotIn('worker', self.calls)

    def test_conflict_progress_only_preserves_rebase_state_and_real_conditions_invalidate(self):
        text = '---\nid: G-047\n---\n## Purpose\nImplement\n## Progress\n- [ ] targeted test\n'
        original = ExecPlanBodyHash(text)
        previous = replace(self.definition, body_hash=str(original), body_text=text)
        self.state.update_project('repo', phase='WORKING', definition_snapshot=previous.serialize(),
            worker_input_token=previous.worker_token, worker_session_id='saved',
            last_error=self.handler.effects.contract.REBASE_CONFLICT_PREFIX + 'conflict')
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.is_directory.return_value = True
        self.audit()
        self.effects.revision.body_hash.return_value = ExecPlanBodyHash(text.replace('[ ]', '[x]'))
        before = dict(self.state.row())
        result = self.handler.guard(self.context())
        self.assertNotIsInstance(result, handlers.ExecutionOutcome)
        after = self.state.row()
        for field in ('worker_session_id', 'worker_result', 'last_error', 'definition_snapshot'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.repository.active_row('repo')['retry_count'], 2)
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertNotIn('rebase', self.calls)
        self.effects.revision.body_hash.return_value = ExecPlanBodyHash(text.replace('targeted test', 'skip required validation'))
        result = self.handler.guard(self.context())
        self.assertNotIsInstance(result, handlers.ExecutionOutcome)
        self.assertTrue(result.resolving_rebase)
        self.assertEqual(self.state.row()['worker_session_id'], 'saved')
        for field in ('worker_result', 'verify_result', 'review_result', 'verify_token', 'review_token'):
            self.assertIsNone(self.state.row()[field], field)
        self.assertEqual(self.state.row()['definition_snapshot'], previous.serialize())
        self.effects.blocking.block_active_execplan.assert_not_called()
