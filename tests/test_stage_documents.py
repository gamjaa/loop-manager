"""단계별 문서 영향 판단과 검증 근거 경계의 회귀 시나리오."""
from dataclasses import replace
import json
from unittest import TestCase
from unittest import mock
from types import SimpleNamespace
from pathlib import Path
import tempfile

from loop_manager_internal.execution import stage_documents as documents
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
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

    def test_relevance_resolves_renamed_requirement_source(self):
        source = 'docs/requirements/[R-001] Original.md'
        renamed = 'docs/requirements/[R-001] Renamed.md'
        relevant = documents.related_paths(
            'docs/plans/[G-001] Plan.md', (source,), (renamed, 'src/app.py'),
            requirement_documents={renamed: b'# [R-001] Renamed\n'},
        )
        self.assertEqual(relevant, (renamed,))

    def test_assessment_requires_complete_unambiguous_evidence(self):
        value = dict(relevant=True, implementation='satisfied', plan_text=None,
                     evidence='현재 구현이 변경된 조건을 충족하며 검증은 별도 확인한다.')
        output = 'DOCUMENT_RESULT: ' + json.dumps(value)
        self.assertEqual(documents.parse_assessment(output), value)
        formatted = ('- **DOCUMENT_RESULT** =\n```json\n'
                     + json.dumps(value, indent=2) + '\n```')
        self.assertEqual(documents.parse_assessment(formatted), value)
        duplicate_key = ('DOCUMENT_RESULT: {"relevant":false,"relevant":true,'
                         '"implementation":"satisfied","plan_text":null,"evidence":"근거"}')
        for invalid in (output + '\n' + output, 'DOCUMENT_RESULT: {}', duplicate_key,
                        'DOCUMENT_RESULT: ' + json.dumps({**value, 'evidence': ''}),
                        'DOCUMENT_RESULT: ' + json.dumps({**value, 'relevant': False, 'implementation': 'needed'})):
            with self.subTest(invalid=invalid), self.assertRaises(PlannerValidationError):
                documents.parse_assessment(invalid)


class DocumentPhaseTests(ExecutionFixture, TestCase):
    def test_new_execplan_conditions_do_not_create_document_assessment(self):
        context = self.review_context()
        current = self.handler._definition_snapshot
        checkpoints = ExecutionCheckpointRepository(self.connection)
        effects = self.document_effects()
        self.state.update_project(self.repo, worker_session_id='saved-worker')
        self.assertIsNone(documents.refresh(effects, self.state, checkpoints, self.handler._identity,
            context, replace(current, body_hash='previous-condition'), current, [current.path]))
        effects.run_agent.assert_not_called()
        self.assertIsNone(documents.pending(checkpoints, self.repo, context.plan.plan_id))
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-worker')

    def test_preserved_judgment_checks_candidate_without_recalling_planner(self):
        context = self.review_context()
        current = self.handler._definition_snapshot
        checkpoints = ExecutionCheckpointRepository(self.connection)
        effects = self.document_effects()
        scope = effects.capture_scope.return_value
        item = dict(version=1, stage='proposed', base='base', phase='REVIEWING',
            paths=[current.path], document_only=True, previous=current.serialize(),
            candidate='base', worker_snapshot=scope.serialize(), worker_result='saved',
            assessment=dict(relevant=True, implementation='satisfied', plan_text=None, evidence='근거'))
        documents.save(checkpoints, self.repo, context.plan.plan_id, item)
        result = documents.refresh(effects, self.state, checkpoints, self.handler._identity,
            context, current, current, [])
        self.assertEqual(result['stage'], 'done')
        effects.run_agent.assert_not_called()
        effects.capture_scope.return_value = replace(scope, dirty={'app.py': (('worktree',), 'changed')})
        with self.assertRaises(RecoveryFailure):
            documents.refresh(effects, self.state, checkpoints, self.handler._identity,
                context, current, current, [])

    def test_unfinished_or_uncertain_assessment_keeps_audit_and_returns_to_revision_gate(self):
        context = self.review_context()
        current = self.handler._definition_snapshot
        checkpoints = ExecutionCheckpointRepository(self.connection)
        effects = self.document_effects()
        for stage in ('assess', 'proposed'):
            item = dict(version=1, stage=stage, base='base', phase='REVIEWING',
                paths=[current.path], document_only=True, previous=current.serialize(),
                candidate='base', worker_snapshot=effects.capture_scope.return_value.serialize(),
                assessment=(dict(relevant=True, implementation='uncertain', plan_text=None,
                                 evidence='기존 미확정 판단') if stage == 'proposed' else None))
            documents.save(checkpoints, self.repo, context.plan.plan_id, item)
            self.assertIsNone(documents.refresh(effects, self.state, checkpoints, self.handler._identity,
                context, current, current, []))
            self.assertIsNone(documents.pending(checkpoints, self.repo, context.plan.plan_id))
            self.assertEqual(checkpoints.get(self.repo, context.plan.plan_id).snapshot['retired_document_assessment'], item)
        effects.run_agent.assert_not_called()

    def test_owned_progress_commit_recovers_atomically_without_consuming_new_conditions(self):
        from loop_manager_internal.execution.execution_revision import advance_owned_record, ExecPlanBodyHash
        for mixed in (False, True):
            with self.subTest(mixed=mixed):
                context = self.review_context()
                text = '---\nid: G-047\n---\n## Purpose\nImplement safely\n## Progress\n- [ ] Run test\n'
                previous = replace(self.handler._definition_snapshot, body_text=text, body_hash=str(ExecPlanBodyHash(text)))
                self.handler._definition_snapshot = previous
                self.state.update_project(self.repo, definition_snapshot=previous.serialize(),
                    worker_input_token=previous.worker_token, verify_token='saved-verify', review_token='saved-review')
                recorded = text.replace('[ ]', '[x]') + ('- New condition: send data\n' if mixed else '')
                checkpoints = ExecutionCheckpointRepository(self.connection)
                current = checkpoints.get(self.repo, context.plan.plan_id)
                checkpoints.record(self.repo, context.plan.plan_id, expected_generation=current.generation,
                    operation=None, expected_head='after', target_base_oid='base', observed_remote_oid=None,
                    snapshot={'after_head': 'after', 'document_record': {
                        'previous': previous.serialize(), 'tree': 'approved'}})
                def git(_repo, *args, **kwargs):
                    return SimpleNamespace(returncode=0, stdout=(recorded if args[0] == 'show' else 'approved'))
                ports = replace(self.handler._publication_handler().ports, checkpoint=checkpoints, git=mock.Mock(side_effect=git))
                if not mixed:
                    with mock.patch.object(checkpoints, 'record', side_effect=RuntimeError('interrupted')):
                        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                            advance_owned_record(ports, context)
                    self.assertEqual(self.state.row()['definition_snapshot'], previous.serialize())
                    self.assertNotIn('applied', checkpoints.get(self.repo, context.plan.plan_id).snapshot['document_record'])
                advance_owned_record(ports, context)
                actual = self.state.row()
                self.assertEqual(actual['verify_token'], 'saved-verify')
                self.assertEqual(actual['review_token'], 'saved-review')
                if mixed:
                    self.assertEqual(actual['definition_snapshot'], previous.serialize())
                else:
                    self.assertEqual(json.loads(actual['definition_snapshot'])['body_text'], recorded)
                    self.assertEqual(actual['worker_input_token'], previous.worker_token)
                    count = ports.git.call_count
                    advance_owned_record(ports, context)
                    self.assertEqual(ports.git.call_count, count)

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.handler._identity = replace(self.handler._identity, state_dir=Path(directory.name))

    def document_effects(self, base='base', outputs=None):
        scope = GitSnapshot(head='base', branch='work', index={}, dirty={}, operations={},
                            refs={}, worktrees={}, config={})
        output = 'DOCUMENT_RESULT: ' + json.dumps(dict(relevant=True, implementation='satisfied',
            plan_text=None, evidence='현재 구현은 조건을 충족하며 검증은 새 조건에서 진행한다.'))
        effects = mock.Mock()
        effects.pr_for_branch.return_value = None
        effects.capture_scope.return_value = scope
        effects.git.side_effect = lambda root, *args, **kw: SimpleNamespace(returncode=0,
            stdout='' if args[0] in {'status', 'worktree'} else base if args[-1] == 'origin/main' else 'base')
        import loop_manager as lm
        effects.run_agent.side_effect = [lm.AgentResult(0, text) for text in (outputs or [output])]
        effects.run_with_retries.side_effect = lambda call, session_id: call(session_id, False)
        return effects



    def test_upper_specification_does_not_repeat_task_planning(self):
        context = self.review_context()
        current = self.handler._definition_snapshot
        checkpoints = ExecutionCheckpointRepository(self.connection)
        effects = self.document_effects()
        first = documents.refresh(effects, self.state, checkpoints, self.handler._identity,
            context, current, current, ['docs/ARCHITECTURE.md'])
        self.assertIsNone(first)
        effects.run_agent.assert_not_called()
        next_definition = replace(current, revision_sha='new-base')
        self.state.update_project(self.repo, active_sha='new-base')
        second_effects = self.document_effects('new-base')
        second = documents.refresh(second_effects, self.state, checkpoints, self.handler._identity,
            context, current, next_definition, ['docs/ARCHITECTURE.md'])
        self.assertIsNone(second)
        second_effects.run_agent.assert_not_called()

    def test_unrelated_document_never_calls_planner(self):
        context = self.review_context()
        effects = self.document_effects()
        current = self.handler._definition_snapshot
        self.assertIsNone(documents.refresh(effects, self.state, ExecutionCheckpointRepository(self.connection),
            self.handler._identity, context, current, current, ['docs/requirements/Other.md']))
        effects.run_agent.assert_not_called()

    def test_requirement_rename_does_not_repeat_task_planning(self):
        context = self.review_context()
        previous = replace(
            self.handler._definition_snapshot,
            sources=('docs/requirements/[R-001] Original.md',),
        )
        renamed = 'docs/requirements/[R-001] Renamed.md'
        oid = '1' * 40
        effects = self.document_effects()

        def git_bytes(_repo, operation, *args):
            if operation == 'ls-tree':
                return f'100644 blob {oid}\t{renamed}\0'.encode()
            if operation == 'cat-file':
                return b'# [R-001] Renamed\n'
            raise AssertionError((operation, args))

        effects.git_bytes.side_effect = git_bytes
        item = documents.refresh(
            effects, self.state, ExecutionCheckpointRepository(self.connection),
            self.handler._identity, context, previous, previous, [renamed],
        )
        self.assertIsNone(item)
        effects.run_agent.assert_not_called()


    def test_actual_pr_discards_pre_pr_assessment_authority(self):
        checkpoints = self.changed_document('REVIEWING', 'satisfied')
        effects = self.document_effects()
        effects.pr_for_branch.return_value = {'url': 'https://example/pr/1'}
        current = self.handler._definition_snapshot
        self.assertIsNone(documents.refresh(effects, self.state, checkpoints, self.handler._identity,
            self.context(), current, current, []))
        self.assertIsNone(documents.pending(checkpoints, self.repo, 'G-047'))
        effects.run_agent.assert_not_called()

    def test_prepared_document_pr_race_preserves_candidate_and_execution_history(self):
        import hashlib
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        from loop_manager_internal.planning.planning_catalog import snapshot_to_dict

        context = self.review_context()
        current = self.handler._definition_snapshot
        checkpoints = ExecutionCheckpointRepository(self.connection)
        identity = self.handler._identity
        plan_text = ('---\nid: G-047\ntitle: Existing plan\nsources:\n  - docs/PRODUCT.md\n'
                     'depends_on: []\ncomplexity: low\nrisk: low\npriority: normal\n---\n')
        original = ReadyPlanSnapshot(
            plan_id='G-047', path=current.path, title='Existing plan',
            sources=('docs/PRODUCT.md',), depends_on=(), complexity='low', risk='low',
            priority='normal', content_hash='saved-content', plan_sha=current.revision_sha,
            text=plan_text,
        )
        output = 'DOCUMENT_RESULT: ' + json.dumps(dict(
            relevant=True, implementation='satisfied', plan_text=plan_text, evidence='saved proposal',
        ))
        worker_snapshot = GitSnapshot(
            head=current.revision_sha, branch='work', index={}, dirty={}, operations={},
            refs={}, worktrees={}, config={},
        )
        workspace = (identity.state_dir / 'worktrees' / hashlib.sha256(self.repo.encode()).hexdigest()[:16]
                     / 'document-revisions' / 'G-047' / current.revision_sha)
        saved = {
            'version': 1, 'stage': 'prepared', 'base': current.revision_sha,
            'phase': 'REVIEWING', 'paths': ['docs/PRODUCT.md'], 'document_only': True,
            'previous': current.serialize(), 'assessment': documents.parse_assessment(output),
            'assessment_raw_result': output, 'candidate': current.revision_sha,
            'worker_snapshot': worker_snapshot.serialize(), 'workspace': str(workspace),
            'commit': 'b' * 40, 'original': snapshot_to_dict(original),
            'worker_result': 'saved worker result', 'verify_result': 'saved verify result',
        }
        documents.save(checkpoints, self.repo, 'G-047', saved)
        effects = self.document_effects(current.revision_sha)
        effects.assert_snapshot = mock.Mock()
        effects.pr_for_branch.side_effect = [None, {'url': 'https://example/pr/1'}]
        effects.git.side_effect = lambda _root, *args, **_kwargs: SimpleNamespace(
            returncode=1 if args[0] == 'merge-base' else 0,
            stdout=(current.revision_sha if args[0] == 'rev-parse' and args[-1] == 'origin/main' else ''),
        )

        result = documents.refresh(
            effects, self.state, checkpoints, identity, context, current, current, [],
        )

        self.assertIsNone(result)
        self.assertEqual(documents.pending(checkpoints, self.repo, 'G-047'), saved)
        self.assertEqual(self.state.row()['worker_result'], 'worker result')
        self.assertEqual(self.state.row()['verify_result'], 'successful verify output')
        self.assertEqual(effects.pr_for_branch.call_count, 2)
        self.assertFalse(any(call.args[1] == 'push' for call in effects.git.call_args_list))
        effects.run_agent.assert_not_called()

    def test_proposed_pr_race_from_snapshot_is_handed_off_by_revision_handler(self):
        from loop_manager_internal.planning.planner_validation import (
            MutableReadySnapshotEffects, mutable_ready_snapshot,
        )
        from loop_manager_internal.planning.planning_catalog import mutable_snapshot_token

        context = self.review_context()
        current = self.handler._definition_snapshot
        checkpoints = ExecutionCheckpointRepository(self.connection)
        plan_text = '---\nid: G-047\ntitle: Runtime\n---\n## Purpose\nRevised conditions\n'
        assessment = dict(
            relevant=True, implementation='satisfied', plan_text=plan_text, evidence='저장된 개정 후보',
        )
        output = 'DOCUMENT_RESULT: ' + json.dumps(assessment)
        document_effects = self.document_effects()
        saved = {
            'version': 1, 'stage': 'proposed', 'base': current.revision_sha,
            'phase': 'REVIEWING', 'paths': [current.path], 'document_only': True,
            'previous': current.serialize(), 'assessment': assessment,
            'assessment_result': assessment, 'assessment_raw_result': output,
            'candidate': current.revision_sha,
            'worker_snapshot': document_effects.capture_scope.return_value.serialize(),
            'worker_result': 'saved worker result', 'verify_result': 'saved verify result',
        }
        documents.save(checkpoints, self.repo, 'G-047', saved)
        self.connection.execute(
            "UPDATE exec_plans SET status='REVIEWING', branch=?, ever_started=1 "
            "WHERE repo=? AND plan_id='G-047'",
            (context.branch, self.repo),
        )
        self.connection.commit()

        pull_request = {'url': 'https://example/pr/1'}
        pr_for_branch = mock.Mock(side_effect=[None, pull_request, pull_request])
        snapshot_effects = MutableReadySnapshotEffects(
            _ensure_execplans_schema=mock.Mock(),
            _ready_plan_snapshot_at_revision=mock.Mock(
                side_effect=AssertionError('PR-backed plan must be excluded before Git inspection')
            ),
            _row_is_mutable_ready=mock.Mock(return_value=True),
            mutable_snapshot_token=mutable_snapshot_token,
            pr_for_branch=pr_for_branch,
        )
        snapshots = []

        def prepare_snapshot(connection, repo, worktree, active_sha):
            snapshot = mutable_ready_snapshot(
                snapshot_effects, connection, repo, worktree, active_sha,
            )
            snapshots.append(snapshot)
            return snapshot

        document_effects.prepare_snapshot.side_effect = prepare_snapshot
        document_effects.pr_for_branch = pr_for_branch
        self.effects = replace(
            self.effects,
            checkpoint=checkpoints,
            revision=replace(
                self.effects.revision,
                refresh_documents=lambda active, previous, definition, changed: documents.refresh(
                    document_effects, self.state, checkpoints, self.handler._identity,
                    active, previous, definition, changed,
                ),
            ),
        )
        self.handler.effects = self.effects

        result = self.handler.guard(context)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].by_id, {})
        self.assertEqual(pr_for_branch.call_count, 3)
        self.assertFalse(
            isinstance(result, ExecutionOutcome) and result.kind is ExecutionOutcomeKind.FINAL_BLOCK
        )
        self.assertEqual(documents.pending(checkpoints, self.repo, 'G-047'), saved)
        self.assertEqual(self.state.row()['worker_result'], 'worker result')
        self.assertEqual(self.state.row()['verify_result'], 'successful verify output')
        self.assertFalse(any(call.args[1] == 'push' for call in document_effects.git.call_args_list))

    def test_unchanged_legacy_plan_keeps_raw_and_normalized_assessment(self):
        context = self.review_context()
        current = self.handler._definition_snapshot
        checkpoints = ExecutionCheckpointRepository(self.connection)
        effects = self.document_effects(current.revision_sha)
        plan_text = '---\nid: G-047\ntitle: Existing plan\n---\n## Purpose\nKeep this plan\n'
        assessment = dict(
            relevant=True, implementation='satisfied', plan_text=plan_text, evidence='조건이 이미 반영됨',
        )
        raw_result = '- **DOCUMENT_RESULT** =\n```json\n' + json.dumps(assessment, indent=2) + '\n```'
        worker_snapshot = effects.capture_scope.return_value.serialize()
        saved = {
            'version': 1, 'stage': 'proposed', 'base': current.revision_sha,
            'phase': 'REVIEWING', 'paths': [current.path], 'document_only': True,
            'previous': current.serialize(), 'assessment': assessment,
            'assessment_result': assessment, 'assessment_raw_result': raw_result,
            'candidate': current.revision_sha, 'worker_snapshot': worker_snapshot,
        }
        documents.save(checkpoints, self.repo, 'G-047', saved)
        effects.prepare_snapshot.return_value = SimpleNamespace(
            by_id={'G-047': SimpleNamespace(text=plan_text)},
        )

        result = documents.refresh(
            effects, self.state, checkpoints, self.handler._identity,
            context, current, current, [],
        )

        self.assertEqual(result['stage'], 'done')
        self.assertIsNone(result['assessment']['plan_text'])
        self.assertEqual(result['assessment_result'], assessment)
        self.assertEqual(result['assessment_raw_result'], raw_result)
        effects.run_agent.assert_not_called()

    def test_remote_race_keeps_proposal_and_code_invalidation(self):
        from tests.support.revision import definition
        old = dict(previous=definition().serialize(), paths=['docs/PRODUCT.md'], document_only=True,
            base='old', assessment={'evidence': 'old'}, assessment_result={'evidence': 'old'},
            assessment_raw_result='raw DOCUMENT_RESULT', last_result='last output',
            validation_error='old validation error', workspace='owned', commit='candidate')
        item = documents.restart_assessment(old, 'new', ['app.py', 'docs/ARCHITECTURE.md'])
        self.assertEqual(item['stage'], 'rebase')
        self.assertFalse(item['document_only'])
        self.assertIsNone(item['assessment'])
        self.assertNotIn('workspace', item)
        self.assertEqual(item['superseded'][0]['commit'], 'candidate')
        self.assertEqual(item['superseded'][0]['assessment_raw_result'], 'raw DOCUMENT_RESULT')
        for key in ('assessment_result', 'assessment_raw_result', 'last_result', 'validation_error'):
            self.assertNotIn(key, item)

    def test_work_branch_document_observation_does_not_grant_adoption(self):
        context = self.review_context()
        self.state.update_project(self.repo, definition_snapshot=self.handler._definition_snapshot.serialize())
        checkpoints = ExecutionCheckpointRepository(self.connection)
        checkpoints.record(self.repo, 'G-047', expected_generation=0, operation=None,
            expected_head='base', target_base_oid='base', observed_remote_oid='old-remote', snapshot={})
        def git(_root, *args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=context.plan.path + '\0' if args[0] == 'diff' else '')
        documents.remember_work_branch(git, self.state, checkpoints, self.handler._identity,
            context, 'new-remote', 'base', 'base', None)
        item = documents.pending(checkpoints, self.repo, 'G-047')
        self.assertEqual(item['phase'], 'REVIEWING')
        self.assertTrue(item['work_branch'])
        self.assertEqual(item['paths'], [context.plan.path])
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
