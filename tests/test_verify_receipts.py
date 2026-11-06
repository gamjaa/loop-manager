"""Narrow verify policy checks; all process, Agent and Git effects are fakes."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tests.support.execution import ExecutionFixture, READY_WORKER_RESULT, execution_fixture
from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.execution.execution_revision import ExecPlanBodyHash
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from loop_manager_internal.storage import runtime_state, verify_receipts
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace
from loop_manager_internal.verification import inputs


class StageResultProvenanceTest(ExecutionFixture, unittest.TestCase):
    def assert_erasure_during_git_observation(self, *, reconnect):
        from loop_manager_internal.execution import stage_results
        from loop_manager_internal.storage import deleted_plan_state
        definition = self.adopted_origin()[0] if reconnect else None
        before_git = self.git_port.side_effect
        erased = []
        def observe(*args, **kwargs):
            result = before_git(*args, **kwargs)
            if not erased:
                deleted_plan_state.ensure_schema(self.connection)
                self.connection.execute("INSERT INTO deleted_plan_ids VALUES ('repo','G-047')")
                self.connection.execute("DELETE FROM exec_plans WHERE repo='repo' AND plan_id='G-047'")
                self.connection.commit()
                erased.extend(self.connection.iterdump())
            return result
        self.git_port.side_effect = observe
        if reconnect:
            self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        else:
            self.assertIsNone(stage_results.preserve(self.ports, self.context(), reason='rebase_start'))
        self.assertTrue(erased)
        self.assertEqual(list(self.connection.iterdump()), erased)

    def test_erasure_during_git_observation_prevents_new_stage_origin(self):
        self.assert_erasure_during_git_observation(reconnect=False)

    def test_erasure_during_git_observation_prevents_worker_reconnection(self):
        self.assert_erasure_during_git_observation(reconnect=True)

    def setUp(self):
        super().setUp()
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        self.checkpoints = ExecutionCheckpointRepository(self.connection)
        self.state.update_project('repo', phase='PUBLISHING',
            worker_result=READY_WORKER_RESULT, verify_result='original full PASS',
            review_result='original final Review', review_token='original-review-token',
            validated_base_sha='original-base', worker_session_id='original-session')
        self.raw = ':100644 100755 ' + 'a' * 40 + ' ' + 'b' * 40 + ' M\0src/app.py\0'
        self.observation = {'head': 'original-head', 'dirty': '', 'raw': self.raw}
        self.blobs = {}
        self.git_port = mock.Mock(side_effect=self.observe)
        self.ports = SimpleNamespace(state=self.state, checkpoint=self.checkpoints,
            identity=SimpleNamespace(repo='repo'), git=self.git_port)

    def observe(self, _root, *args):
        self.assertFalse(self.connection.in_transaction)
        if args == ('rev-parse', 'HEAD'):
            value = self.observation['head']
        elif args == ('status', '--porcelain'):
            value = self.observation['dirty']
        elif args[:2] == ('cat-file', 'blob'):
            value = self.blobs[args[2]]
        else:
            self.assertEqual(args[0], 'diff')
            self.assertEqual(args[-3:], ('base', 'original-head', '--'))
            value = self.observation['raw']
        return SimpleNamespace(stdout=value, returncode=0)

    def test_preserves_candidate_base_modes_and_results_once_without_success_or_invocation(self):
        from loop_manager_internal.execution import stage_results
        before = dict(self.state.row())
        origin = stage_results.preserve(self.ports, self.context(), reason='rebase_start')
        saved = self.checkpoints.get('repo', 'G-047')
        evidence = saved.snapshot['stage_result_origins'][origin]['evidence']
        self.assertEqual((evidence['active_sha'], evidence['observed_head']), ('base', 'original-head'))
        self.assertEqual((evidence['changes'][0]['old_mode'], evidence['changes'][0]['new_mode']),
                         ('100644', '100755'))
        self.assertEqual(evidence['review_result'], 'original final Review')
        self.assertEqual(evidence['worker_session_id'], 'original-session')
        self.assertEqual(evidence['verify_selection'],
                         verify_receipts.selected(self.connection, 'repo', 'G-047'))
        self.assertIsNone(evidence['verify_receipt'])
        self.assertEqual(stage_results.preserve(self.ports, self.context(), reason='revision_invalidation'), origin)
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), saved)
        self.assertEqual(dict(self.state.row()), before)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM verify_receipts').fetchone()[0], 0)
        self.effects.agent.run_agent.assert_not_called()

    def test_unknown_or_dirty_changes_preserve_results_without_fabricating_comparison(self):
        from loop_manager_internal.execution import stage_results
        for invalid in ('dirty', 'raw'):
            with self.subTest(invalid=invalid):
                self.observation[invalid] = 'unconfirmed'
                origin = stage_results.preserve(self.ports, self.context(), reason='rebase_start')
                evidence = self.checkpoints.get('repo', 'G-047').snapshot['stage_result_origins'][origin]['evidence']
                self.assertIsNone(evidence['changes'])
                self.assertIsNone(evidence['observed_head'])
                self.assertEqual(evidence['review_result'], 'original final Review')
                self.assertIn('observation_error', evidence)
                self.observation[invalid] = '' if invalid == 'dirty' else self.raw

    def test_checkpoint_and_receipt_completion_races_do_not_store_stale_provenance(self):
        from loop_manager_internal.execution import stage_results
        for race in ('checkpoint', 'receipt'):
            with self.subTest(race=race):
                verify_receipts.start_cycle(self.connection, 'repo', 'G-047')
                selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
                identifier, selection = verify_receipts.record(self.connection, selection,
                    head='original-head', observation=inputs.InputObservation('original-input'),
                    kind='run', reason='pending actual run')
                self.connection.commit()
                def observe(root, *args):
                    result = self.observe(root, *args)
                    if args[0] == 'diff':
                        if race == 'checkpoint':
                            current = self.checkpoints.get('repo', 'G-047')
                            self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
                                operation=current.operation, expected_head=current.expected_head,
                                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                                snapshot={'concurrent': True})
                        else:
                            verify_receipts.complete(self.connection, selection, identifier,
                                passed=False, output='actual failure')
                        self.connection.commit()
                    return result
                self.git_port.side_effect = observe
                with self.assertRaisesRegex(runtime_state.RuntimeStateError, 'provenance changed'):
                    stage_results.preserve(self.ports, self.context(), reason='rebase_start')
                self.assertNotIn('stage_result_origins', self.checkpoints.get('repo', 'G-047').snapshot or {})
                self.assertEqual(self.state.row()['review_result'], 'original final Review')

    def adopted_origin(self, *, full_success=False, plan_records=False):
        from loop_manager_internal.execution import stage_results
        from loop_manager_internal.storage import planning_review_state
        from tests.support.revision import definition
        from tests.support.rebase_observations import expectation, observation, decision_for
        from loop_manager_internal.git.managed_rebase_validation import Remote
        original = definition(path=self.state.row()['plan_path'])
        if plan_records:
            text = '---\nid: G-047\n---\n## Purpose\nImplement\n## Progress\n- [ ] Completed unit check\n'
            body = ExecPlanBodyHash(text)
            original = replace(original, body_hash=str(body), body_text=text)
            self.blobs.update({'d' * 40: text, 'e' * 40: text.replace('[ ]', '[x]'),
                               'f' * 40: text.replace('[ ]', '[X]')})
            plan_change = ':100644 100644 ' + 'd' * 40 + ' ' + 'e' * 40 + ' M\0' + original.path + '\0'
            self.observation['raw'] = self.raw + plan_change
        self.state.update_project('repo', definition_snapshot=original.serialize(),
            worker_input_token=original.worker_token, archive_token=original.archive_token)
        if full_success:
            selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
            identifier, selection = verify_receipts.record(self.connection, selection,
                head='original-head', observation=inputs.InputObservation('original-code'),
                kind='run', reason='completed full verify fixture')
            verify_receipts.complete(self.connection, selection, identifier,
                                     passed=True, output='original full PASS')
            self.connection.commit()
        self.checkpoints.record('repo', 'G-047', expected_generation=0,
            expected_head='original-head', operation=None, target_base_oid=None,
            observed_remote_oid=None, snapshot={
                'worker_candidate_token': original.worker_candidate_token('original-head')})
        origin = stage_results.preserve(self.ports, self.context(), reason='rebase_start')
        current = self.checkpoints.get('repo', 'G-047')
        current = self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head='original-head', target_base_oid='new-base',
            observed_remote_oid=None, snapshot={**current.snapshot, 'rebase_stage_origin': origin})
        expected = expectation(repo='repo', plan_id='G-047', plan_path=original.path,
            branch=self.state.row()['branch'], worktree=self.state.row()['worktree'],
            before_head='original-head', target_base_oid='new-base', generation=current.generation,
            remote=Remote('owner/repo', self.state.row()['branch'], 'remote'))
        accepted = decision_for(expected, observation(expected, head='new-head',
            branch_ref=('new-head', ''),
            registration=('HEAD=new-head', 'branch=refs/heads/' + expected.branch))).acceptance
        self.checkpoints.accept_current_rebase('repo', 'G-047', acceptance=accepted)
        self.state.update_project('repo', phase='WORKING', active_sha='new-base',
            worker_result=None, worker_input_token=None, archive_token=None,
            verify_result=None, verify_token=None, review_result=None, review_token=None)
        planning_review_state.begin(self.connection, 'repo', purpose='full', revision='new-base',
            input_fingerprint='planning-inputs', specification_fingerprint='specifications',
            work_cycle='project-cycle', comparison_revision='base')
        planning_review_state.validate(self.connection, 'repo', 'new-base', 'general assessment', 'unchanged')
        planning_review_state.confirm(self.connection, 'repo', 'new-base', 'new-base')
        self.connection.commit()
        self.rebased = self.raw
        self.base_delta = ':000000 100644 ' + '0' * 40 + ' ' + 'c' * 40 + ' A\0docs/plans/[G-999] Other.md\0'
        def observe(_root, *args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            if args == ('rev-parse', 'HEAD'):
                value = 'new-head'
            elif args == ('status', '--porcelain') or args[0] == 'merge-base':
                value = ''
            elif args[-3:] == ('base', 'new-base', '--'):
                value = self.base_delta
            elif args[-3:] == ('new-base', 'new-head', '--'):
                value = self.rebased
            elif args[:2] == ('cat-file', 'blob'):
                value = self.blobs[args[2]]
            else:
                raise AssertionError(args)
            return SimpleNamespace(stdout=value, returncode=0)
        self.git_port.side_effect = observe
        if plan_records:
            self.base_delta += ':100644 100644 ' + 'd' * 40 + ' ' + 'f' * 40 + ' M\0' + original.path + '\0'
            text = self.blobs['f' * 40]
            original = replace(original, body_hash=str(ExecPlanBodyHash(text)), body_text=text)
        return replace(original, revision_sha='new-base'), origin

    def test_selected_plan_record_adoption_preserves_raw_review_scope_and_reconnects_worker(self):
        from loop_manager_internal.execution import stage_results
        definition, origin = self.adopted_origin(plan_records=True)
        evidence = self.checkpoints.get('repo', 'G-047').snapshot['stage_result_origins'][origin]['evidence']
        selected = next(change for change in evidence['changes'] if change['path'] == definition.path)
        self.assertEqual(selected['new_oid'], 'e' * 40)
        self.assertEqual(len(evidence['implementation_changes']), 1)
        self.assertTrue(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.assertIsNone(self.state.row()['review_token'])
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['stage_result_origins'][origin]['evidence'], evidence)

    def test_confirmed_planning_survives_only_exact_archive_delta(self):
        from loop_manager_internal.execution import stage_results
        definition, _origin = self.adopted_origin()
        self.connection.execute("UPDATE planning_reviews SET revision='base', confirmed_revision='base' "
                                "WHERE repo='repo' AND slot='general'")
        self.connection.commit()
        # A different ID alone does not justify reusing stale planning.
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        archive = ''
        for directory, name in (('requirements', '[R-999] Other.md'), ('plans', '[G-999] Other.md')):
            archive += ':100644 000000 ' + 'c' * 40 + ' ' + '0' * 40 + ' D\0docs/' + directory + '/' + name + '\0'
            archive += ':000000 100644 ' + '0' * 40 + ' ' + 'c' * 40 + ' A\0docs/' + directory + '/archive/' + name + '\0'
        self.base_delta = archive.replace('000000 100644', '000000 100755')
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.base_delta = archive.replace('c' * 40 + ' A', 'e' * 40 + ' A')
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.base_delta = archive
        self.assertTrue(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.assertEqual(len(self.checkpoints.get('repo', 'G-047').snapshot['worker_reconnection']['planning_delta']), 4)

    def test_plan_record_comparison_keeps_new_constraints_header_and_mode(self):
        from loop_manager_internal.execution import stage_results
        from loop_manager_internal.git.managed_rebase_observation import parse_raw_changes
        path = self.context().plan.path
        text = '---\nid: G-047\n---\n## Progress\n- [ ] Completed unit check\n'
        self.blobs = {'d' * 40: text, 'e' * 40: text.replace('[ ]', '[x]')}
        raw = ':100644 100644 ' + 'd' * 40 + ' ' + 'e' * 40 + ' M\0' + path + '\0'
        changes = parse_raw_changes(raw)
        self.assertEqual(stage_results.implementation_changes(self.ports, self.context(), changes, path), [])
        for changed in (text + '- New validation requirement\n', text.replace('G-047', 'G-048')):
            self.blobs['e' * 40] = changed
            self.assertEqual(len(stage_results.implementation_changes(self.ports, self.context(), changes, path)), 1)
        self.blobs['e' * 40] = text.replace('[ ]', '[x]')
        self.assertEqual(len(stage_results.implementation_changes(self.ports, self.context(),
            parse_raw_changes(raw.replace('100644 100644', '100644 100755')), path)), 1)

    def test_restart_reconnects_only_original_worker_with_same_changes_and_confirmed_planning(self):
        from loop_manager_internal.execution import stage_results
        definition, origin = self.adopted_origin()
        selection = verify_receipts.selected(self.connection, 'repo', 'G-047')
        self.assertTrue(stage_results.reconnect_worker(self.ports, self.context(), definition))
        row = self.state.row()
        self.assertEqual((row['phase'], row['worker_result'], row['worker_session_id']),
                         ('VERIFYING', READY_WORKER_RESULT, 'original-session'))
        self.assertEqual(row['worker_input_token'], definition.worker_token)
        self.assertIsNone(row['verify_token'])
        self.assertIsNone(row['review_token'])
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['worker_reconnection']['origin'], origin)
        self.assertEqual(saved.snapshot['worker_candidate_token'], definition.worker_candidate_token('new-head'))
        self.assertEqual(verify_receipts.selected(self.connection, 'repo', 'G-047'), selection)
        self.effects.agent.run_agent.assert_not_called()
        # A later correction cannot replay this rebase's prior Worker result.
        self.state.update_project('repo', phase='WORKING', worker_result=None)
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))

    def test_worker_reconnection_rejects_changed_patch_path_mode_and_unproven_inputs(self):
        from loop_manager_internal.execution import stage_results
        definition, _origin = self.adopted_origin()
        baseline = self.rebased
        for altered in (baseline.replace('src/app.py', 'src/renamed.py'),
                        baseline.replace('100755', '100644'),
                        baseline.replace('b' * 40, 'd' * 40), ''):
            with self.subTest(raw=altered):
                self.rebased = altered
                self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.rebased = baseline
        self.base_delta = self.base_delta.replace('docs/plans/[G-999] Other.md', 'src/shared.py')
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.base_delta = ''
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(),
                         replace(definition, depends_on=('G-998',))))
        self.state.update_project('repo', worker_session_id='new-correction-session')
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.state.update_project('repo', worker_session_id='original-session')
        with mock.patch('loop_manager_internal.storage.planning_review_state.general_pending', return_value=True):
            self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        with mock.patch('loop_manager_internal.storage.planning_review_state.get', return_value=None):
            self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        verify_receipts.start_cycle(self.connection, 'repo', 'G-047')
        self.connection.commit()
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertNotIn('worker_reconnection', self.checkpoints.get('repo', 'G-047').snapshot)

    def test_worker_reconnection_rechecks_checkpoint_after_git_observation(self):
        from loop_manager_internal.execution import stage_results
        definition, _origin = self.adopted_origin()
        original_git = self.git_port.side_effect
        statuses = 0
        def drift(root, *args, **kwargs):
            nonlocal statuses
            result = original_git(root, *args, **kwargs)
            if args == ('status', '--porcelain'):
                statuses += 1
                if statuses == 2:
                    current = self.checkpoints.get('repo', 'G-047')
                    self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
                        operation=current.operation, expected_head=current.expected_head,
                        target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                        snapshot={**current.snapshot, 'concurrent_owner_observation': True})
                    self.connection.commit()
            return result
        self.git_port.side_effect = drift
        self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertTrue(saved.snapshot['concurrent_owner_observation'])
        self.assertNotIn('worker_reconnection', saved.snapshot)
        self.assertIsNone(self.state.row()['worker_result'])

    def test_missing_or_incompatible_origins_use_existing_revalidation(self):
        from loop_manager_internal.execution import stage_results
        definition, origin_id = self.adopted_origin()
        snapshot = self.checkpoints.get('repo', 'G-047').snapshot
        original = snapshot['stage_result_origins'][origin_id]['evidence']
        variants = (None, [], {}, {origin_id: None},
            {origin_id: {'evidence': {**original, 'version': 2}}},
            {origin_id: {'evidence': {key: value for key, value in original.items() if key != 'active_sha'}}})
        for origins in variants:
            with self.subTest(origins=origins):
                current = self.checkpoints.get('repo', 'G-047')
                saved = self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
                    operation=current.operation, expected_head=current.expected_head,
                    target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                    snapshot={**snapshot, 'stage_result_origins': origins})
                self.assertFalse(stage_results.reconnect_worker(self.ports, self.context(), definition))
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), saved)
                self.assertIsNone(self.state.row()['worker_result'])

    def test_reconnected_worker_uses_original_full_run_without_worker_or_verify_call(self):
        from loop_manager_internal.execution import stage_results
        from tests.support.execution import REVIEW_IDENTITY
        definition, _origin = self.adopted_origin(full_success=True)
        self.assertTrue(stage_results.reconnect_worker(self.ports, self.context(), definition))
        self.dirty = False
        self.remote = 'new-base'
        self.handler.catalog_plan = self.repository.active_row('repo')
        self.handler._definition_snapshot = definition
        original_git = self.effects.workspace.git.side_effect
        self.effects.workspace.git.side_effect = lambda root, *args, **kwargs: (
            SimpleNamespace(stdout='new-head', returncode=0) if args == ('rev-parse', 'HEAD')
            else original_git(root, *args, **kwargs))
        partial = mock.Mock(side_effect=AssertionError('a matching full success must precede new partial work'))
        self.handler.effects = replace(self.handler.effects, checkpoint=self.checkpoints,
            validation=replace(self.handler.effects.validation,
                partial_verify_enabled=lambda _: True, partial_verify=partial,
                pre_review_identity=lambda _, **kwargs: dict(REVIEW_IDENTITY),
                observe_verify_inputs=lambda _: inputs.InputObservation('original-code')))
        with mock.patch.object(self.handler, '_post_effect_guard', side_effect=lambda context: context):
            outcome = self.handler.verify(self.context(), SimpleNamespace(reconcile=lambda *args: None))
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        receipts = [dict(row) for row in self.connection.execute('SELECT * FROM verify_receipts ORDER BY rowid')]
        self.assertEqual([receipt['kind'] for receipt in receipts], ['run', 'reuse'])
        self.assertEqual(receipts[1]['origin_id'], receipts[0]['id'])
        self.assertEqual(receipts[1]['head'], 'new-head')
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertIsNone(self.state.row()['review_token'])
        self.assertIsNone(self.state.row()['publication_token'])
        self.effects.validation.verify.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()
        partial.assert_not_called()
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM preliminary_reviews WHERE repo='repo' AND plan_id='G-047'").fetchone())


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

    def test_verify_reconnection_preserves_original_review_without_reapproving_it(self):
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        self.verify()
        self.ready()
        checkpoints = ExecutionCheckpointRepository(self.connection)
        self.handler.effects = replace(self.handler.effects, checkpoint=checkpoints)
        self.state.update_project('repo', review_result='original approved review with invocation identity',
                                  review_token='original-review-token')
        self.verify()
        saved = checkpoints.get('repo', 'G-047')
        origins = list(saved.snapshot['stage_result_origins'].values())
        origin = next(item['evidence'] for item in origins if item['reason'] == 'verify_reconnection')
        self.assertEqual(origin['review_result'], 'original approved review with invocation identity')
        self.assertEqual(origin['review_token'], 'original-review-token')
        self.assertEqual(origin['verify_receipt']['id'], self.receipts()[0]['id'])
        self.assertEqual(self.calls.count('verify'), 1)
        self.assertIsNone(self.state.row()['review_token'])
        self.assertIsNone(self.state.row()['review_result'])
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')

    def test_revision_invalidation_preserves_original_results_and_definition(self):
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        self.verify()
        checkpoints = ExecutionCheckpointRepository(self.connection)
        self.handler.effects = replace(self.handler.effects, checkpoint=checkpoints)
        self.state.update_project('repo', review_result='original review', review_token='original-review-token')
        original = self.state.row()['definition_snapshot']
        self.effects.revision.body_hash.return_value = 'new implementation conditions'
        self.handler.guard(self.context())
        saved = checkpoints.get('repo', 'G-047')
        origin = next(item['evidence'] for item in saved.snapshot['stage_result_origins'].values()
                      if item['reason'] == 'revision_invalidation')
        self.assertEqual(origin['definition_snapshot'], original)
        self.assertEqual(origin['worker_result'], READY_WORKER_RESULT)
        self.assertEqual(origin['verify_result'], 'OK')
        self.assertEqual(origin['review_result'], 'original review')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertIsNone(self.state.row()['review_token'])

    def test_publication_revalidation_preserves_review_for_both_stale_base_paths(self):
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        self.verify()
        checkpoints = ExecutionCheckpointRepository(self.connection)
        self.handler.effects = replace(self.handler.effects, checkpoint=checkpoints)
        for active, validated, reason in (('base', 'older-base', 'publication_base_revalidation'),
                                          ('older-base', 'base', 'publication_candidate_revalidation')):
            with self.subTest(reason=reason):
                self.state.update_project('repo', phase='PUBLISHING', active_sha=active,
                    validated_base_sha=validated, review_result='original review', review_token='old-approval')
                self.handler._latest_before_publication(self.context())
                origins = checkpoints.get('repo', 'G-047').snapshot['stage_result_origins'].values()
                evidence = next(item['evidence'] for item in origins if item['reason'] == reason)
                self.assertEqual((evidence['active_sha'], evidence['validated_base_sha']), (active, validated))
                self.assertEqual(evidence['review_result'], 'original review')
                self.assertIsNone(self.state.row()['review_token'])
                self.assertIsNone(self.state.row()['publication_token'])

    def test_environment_and_revision_changes_reuse_original_run(self):
        self.verify()
        self.environment.update(MODE='changed')
        self.git_inputs.update(HEAD='new-revision')
        self.ready()
        self.verify()
        self.assertEqual(self.calls.count('verify'), 1)
        run, reused = self.receipts()
        self.assertEqual(reused['origin_id'], run['id'])
        self.assertEqual(reused['kind'], 'reuse')

    def test_changed_code_or_uncovered_input_runs_again(self):
        self.verify()
        for change in (
            lambda: self.source.write_text('changed'),
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
        self.assertIn('entrypoint outside manifest', observed.reason)

    def test_production_ports_reuse_general_scripts_and_reject_changed_code(self):
        import loop_manager
        from loop_manager_internal.execution import result_validation
        script = self.root / 'scripts/verify.sh'
        script.write_text('#!/bin/sh\npython -m unittest\n')
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
        with mock.patch.object(result_validation, 'os', type('OS', (), {'name': 'posix'})()):
            self.verify()
            self.ready()
            self.verify()
            self.assertEqual(self.calls.count('verify'), 1)
            self.assertEqual(self.receipts()[-1]['kind'], 'reuse')
            script.write_text('#!/bin/sh\npython -m unittest changed\n')
            self.ready()
            self.verify()
            self.assertEqual(self.calls.count('verify'), 2)
            (self.root / 'uncovered.py').write_text('new code')
            self.ready()
            self.verify()
            self.assertEqual(self.calls.count('verify'), 3)

    def test_production_document_reconfirmation_reuses_original_run_for_new_head(self):
        from types import SimpleNamespace
        from loop_manager_internal.execution import result_validation
        self.enterContext(mock.patch.object(result_validation, 'os', SimpleNamespace(name='posix')))
        from loop_manager_internal.execution.result_validation import observe_verify_inputs
        repository = Path(__file__).resolve().parents[1]
        (self.root / 'scripts/verify.sh').write_bytes((repository / 'scripts/verify.sh').read_bytes())
        (self.root / inputs.MANIFEST).write_text(json.dumps(
            {'version': 1, 'paths': ['src/**', 'scripts/**', 'tests/**']}))
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_app.py').write_text('import unittest\n')
        (self.root / 'scripts/verify.json').write_text(json.dumps({
            'version': 1, 'adapter': 'unittest', 'environment': {'python': 'launcher'},
            'stages': [{'id': 'tests', 'kind': 'tests', 'cwd': '.',
                        'collection': {'directory': 'tests', 'pattern': 'test*.py'},
                        'execution': 'independent'}]}))
        (self.root / 'docs').mkdir()
        document = self.root / 'docs/PRODUCT.md'
        document.write_text('# Original')
        self.observer.side_effect = lambda _: observe_verify_inputs(self.root)
        self.verify()
        document.write_text('# Updated after rebase')
        original_git = self.effects.workspace.git.side_effect
        self.effects.workspace.git.side_effect = lambda path, *args, **kwargs: (
            type('Result', (), {'stdout': 'new-head', 'returncode': 0})()
            if args == ('rev-parse', 'HEAD') else original_git(path, *args, **kwargs))
        self.ready()
        self.verify()
        self.assertEqual(self.calls.count('verify'), 1)
        run, reused = self.receipts()
        self.assertEqual(reused['origin_id'], run['id'])
        self.assertEqual(reused['head'], 'new-head')
        self.assertNotEqual(json.loads(run['observation'])['completeness_token'],
                            json.loads(reused['observation'])['completeness_token'])

    def test_worker_review_through_production_ports_reuses_original_run_after_document_rebase(self):
        import loop_manager
        from loop_manager_internal.execution import result_validation
        from types import SimpleNamespace
        self.enterContext(mock.patch.object(result_validation, 'os', SimpleNamespace(name='posix')))
        (self.root / 'scripts/verify.sh').write_text('custom native verify')
        (self.root / inputs.MANIFEST).write_text(json.dumps(
            {'version': 1, 'paths': ['src/**', 'scripts/**']}))
        document = self.root / 'docs/AGENTS.worker.md'
        document.parent.mkdir(exist_ok=True)
        document.write_text('not consumed')
        review = inputs.exclusion_request(self.root, ('scripts/verify.sh',))
        review['basis'] = 'Custom script consumes src only, never runtime guidance contents.'
        message = READY_WORKER_RESULT + '\n' + inputs.EXCLUSION_MARKER + json.dumps(review)
        self.state.update_project('repo', worktree=str(self.root), worker_result=message)
        production = loop_manager._ExecutionLifecyclePorts(
            self.connection, self.repository, repo='repo', repo_path=Path('/repo'),
            state_dir=Path('/state'), base='main', base_sha='base', provider='codex',
            config_path=Path('/config'), newly_activated=False)
        self.state = production.effects.state
        self.handler.effects = replace(self.handler.effects, state=self.state,
            validation=replace(self.handler.effects.validation,
                observe_verify_inputs=production.effects.validation.observe_verify_inputs))
        self.assertEqual(production.effects.agent.worker_prompt('plan', None, session_resume=True),
                         loop_manager.worker_prompt('plan', None, session_resume=True))
        prompt = production.effects.agent.reviewer_prompt('main', verify_evidence='Manager receipt')
        self.assertIn(review['basis'], prompt)
        self.assertIn(inputs.EXCLUSION_MARKER, prompt)
        self.assertIn('Manager receipt', prompt)
        self.assertEqual(self.state.row()['worker_result'], message)
        self.verify()
        document.write_text('changed after rebase')
        original_git = self.effects.workspace.git.side_effect
        self.effects.workspace.git.side_effect = lambda path, *args, **kwargs: (
            type('Result', (), {'stdout': 'new-head', 'returncode': 0})()
            if args == ('rev-parse', 'HEAD') else original_git(path, *args, **kwargs))
        self.ready()
        self.state.update_project('repo', worker_result=message)
        self.verify()
        run, reused = self.receipts()
        self.assertEqual(self.calls.count('verify'), 1)
        self.assertEqual((reused['kind'], reused['origin_id'], reused['head']),
                         ('reuse', run['id'], 'new-head'))
        (self.root / 'scripts/verify.sh').write_text('now consumes docs/AGENTS.worker.md')
        self.ready()
        self.state.update_project('repo', worker_result=message)
        self.verify()
        self.assertEqual(self.calls.count('verify'), 2)

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

    def test_review_gate_reobserves_reused_code_without_invalidating_worker(self):
        self.verify()
        self.ready()
        self.verify()
        self.source.write_text('changed-after-reuse')
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
        # Unconfirmed repository code requires verify again while a clean
        # rebase retains the completed Worker.
        self.observer.side_effect = lambda _: inputs.InputObservation(
            None, reason='unconfirmed repository code inputs')
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
