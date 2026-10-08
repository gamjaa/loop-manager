from tests.support.recovery import BoundedRecoveryFixture
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager
from tests.support.worker import worker_result


class ReviewSpecificationRecoveryTest(BoundedRecoveryFixture, unittest.TestCase):

    def test_reviewer_specification_deadlock_requires_structured_evidence(self):
        import json

        evidence = {
            "kind": "conflict",
            "locations": [
                {"path": "docs/PRODUCT.md", "section": "Review policy"},
                {"path": "docs/requirements/[R-025] Review.md", "section": "Acceptance Criteria"},
            ],
            "problem": "The two retention rules conflict",
            "implementation_choice": "Whether to retain review history",
            "question": "Which retention rule should apply?",
        }
        response = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"
        verdict, details = loop_manager.parse_reviewer_result(response)
        self.assertEqual(verdict, "SPEC_BLOCKED")
        self.assertEqual(details.paths, (
            "docs/PRODUCT.md", "docs/requirements/[R-025] Review.md",
        ))
        self.assertEqual(details.question, evidence["question"])
        missing = {
            **evidence,
            "kind": "missing",
            "locations": evidence["locations"][:1],
        }
        missing_verdict, missing_details = loop_manager.parse_reviewer_result(
            f"SPEC_DEADLOCK: {json.dumps(missing)}\nVERDICT: SPEC_BLOCKED"
        )
        self.assertEqual(missing_verdict, "SPEC_BLOCKED")
        self.assertEqual(missing_details.paths, ("docs/PRODUCT.md",))
        for invalid in (
            {**evidence, "locations": evidence["locations"][:1]},
            {**evidence, "question": ""},
            {**evidence, "kind": ["conflict"]},
            {**evidence, "kind": {"value": "conflict"}},
            {**evidence, "locations": [
                {"path": "docs/../PRODUCT.md", "section": "Review policy"},
                evidence["locations"][1],
            ]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                loop_manager.ReviewerProtocolError
            ):
                loop_manager.parse_reviewer_result(
                    f"SPEC_DEADLOCK: {json.dumps(invalid)}\nVERDICT: SPEC_BLOCKED"
                )
        with self.assertRaises(loop_manager.ReviewerProtocolError):
            loop_manager.parse_reviewer_result(
                f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: FAIL"
            )

    def test_review_specification_block_requeues_only_after_input_change(self):
        from tests.support.database import runtime_connection

        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
            "blocked_reason = 'review_specification_deadlock', "
            "blocked_fingerprint = 'old-input', "
            "blocked_review_fingerprint = 'old-document', "
            "worktree = '/state/G-047', "
            "workspace_owned = 0 WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        connection.commit()
        with (
            mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                              return_value='old-input') as inputs,
            mock.patch.object(loop_manager, '_revision_review_issue_fingerprint',
                              return_value='old-document') as document_input,
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'git', return_value=mock.Mock(returncode=1)),
            mock.patch.object(loop_manager, '_blocked_execplan_revision_metadata_error',
                              return_value=None),
            mock.patch.object(loop_manager, '_requeue_blocked_execplan') as requeue,
        ):
            self.assertFalse(loop_manager.resume_blocked_execplan_if_inputs_changed(
                connection, Path('/repo'), 'repo', 'main', Path('/state'),
            ))
            requeue.assert_not_called()
            from loop_manager_internal.execution import plan_validation
            from loop_manager_internal.git.command_adapter import ExternalObservationError

            path = 'docs/ARCHITECTURE.md'
            observation = plan_validation.RevisionReviewIssueFingerprintEffects(
                _execplan_input_fingerprint=plan_validation._execplan_input_fingerprint,
                _review_issue_paths=lambda result: [path],
                git=mock.Mock(side_effect=[
                    RuntimeError('temporary read failure'),
                    SimpleNamespace(returncode=0, stdout=f'100644 blob abc\t{path}\0'),
                ]),
            )
            document_input.side_effect = lambda *args: (
                plan_validation._revision_review_issue_fingerprint(observation, *args)
            )
            with self.assertRaises(ExternalObservationError):
                loop_manager.resume_blocked_execplan_if_inputs_changed(
                    connection, Path('/repo'), 'repo', 'main', Path('/state'),
                )
            requeue.assert_not_called()
            row = connection.execute(
                "SELECT status, blocked_review_fingerprint FROM exec_plans "
                "WHERE repo = 'repo' AND plan_id = 'G-047'"
            ).fetchone()
            self.assertEqual(tuple(row), ('PLAN_BLOCKED', 'old-document'))
            document_input.side_effect = None
            document_input.return_value = 'changed-architecture'
            self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                connection, Path('/repo'), 'repo', 'main', Path('/state'),
            ))
            requeue.assert_called_once_with(connection, 'repo', 'G-047')
            requeue.reset_mock()
            document_input.return_value = 'old-document'
            inputs.return_value = 'changed-source'
            self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                connection, Path('/repo'), 'repo', 'main', Path('/state'),
            ))
            requeue.assert_called_once_with(connection, 'repo', 'G-047')

    def test_remote_checkpoint_new_review_failure_uses_existing_run_budget(self):
        from loop_manager_internal.storage import blocked_checkpoint_state
        from loop_manager_internal.storage import runtime_state
        from tests.support.database import runtime_connection
        from loop_manager_internal.execution.validation_feedback import normalize_feedback
        from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository

        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        old_review = 'ISSUE_ID: app.py::old::blocking\nOld issue\nVERDICT: FAIL'
        runtime.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence=old_review, release_fingerprint='prior-input',
            plan_values={
                'review_result': old_review,
                'worker_result': worker_result(),
                'publication_remote_oid': 'candidate-head',
            },
            clear_projection=True,
        )
        checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        request = checkpoints.get('repo', 'G-047')
        checkpoints.finish_published(request, 'candidate-head')
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        run_id = loop_manager.recovery_state.begin_external_run(connection, 'same-run')
        feedback = ValidationFeedbackRepository(connection)
        for index in range(3):
            prior = f'ISSUE_ID: app.py::prior-{index}::blocking\nVERDICT: FAIL'
            feedback.observe(
                'repo', 'G-047', 'base', 'review', False, prior,
                normalize_feedback('review', prior),
            )
            result_id = loop_manager.recovery_state.pending_review_correction(
                connection, 'repo', 'G-047', prior,
            )
            self.assertEqual(
                loop_manager.recovery_state.reserve_review_correction(
                    connection, 'repo', 'G-047', result_id,
                ), (True, index + 1, run_id),
            )

        new_review = 'ISSUE_ID: app.py::new::blocking\nNew issue\nVERDICT: FAIL'
        with (
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=mock.Mock()),
            mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')),
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=('base', 'candidate-head')),
            mock.patch.object(loop_manager, 'validate_agent_scope'),
            mock.patch.object(loop_manager, 'run_agent', return_value=(0, new_review)),
            mock.patch.object(loop_manager, '_remote_checkpoint_has_new_failure',
                              return_value=True),
        ):
            resolved = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertEqual(resolved, (True, new_review, 'review'))
        loop_manager._resume_remote_corrected_execplan(
            connection, 'repo', blocked, checkpoints, request,
            'candidate-head', 'candidate-input', 'new Review issue',
            new_failure_stage='review', assessment=new_review,
        )
        plan = connection.execute(
            "SELECT status, review_result FROM exec_plans "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(tuple(plan), ('IMPLEMENTING', new_review))
        latest = connection.execute(
            "SELECT id, stage, classification, raw_output FROM validation_results "
            "WHERE repo = 'repo' AND plan_id = 'G-047' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(latest)[1:], ('review', 'failure', new_review))
        self.assertEqual(
            loop_manager.recovery_state.pending_review_correction(
                connection, 'repo', 'G-047', plan['review_result'],
            ), latest['id'],
        )
        self.assertEqual(
            loop_manager.recovery_state.reserve_review_correction(
                connection, 'repo', 'G-047', latest['id'],
            ), (False, 3, run_id),
        )

    def test_remote_checkpoint_requires_valid_worker_evidence_before_reviewer(self):
        from tests.support.database import runtime_connection
        from loop_manager_internal.storage import runtime_state

        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        for evidence in (None, worker_result(tests='none')):
            with self.subTest(evidence=evidence):
                repository.block_plan(
                    'repo', 'G-047', reason_code='review_retry_exhausted',
                    evidence='prior review failure', release_fingerprint='prior-input',
                    plan_values={'worker_result': evidence}, clear_projection=True,
                )
                blocked = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                with (
                    mock.patch.object(loop_manager, 'capture_git_snapshot',
                                      return_value=mock.Mock()),
                    mock.patch.object(loop_manager, 'verify', return_value=(True, '')),
                    mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                                      return_value=('base', 'head')),
                    mock.patch.object(loop_manager, 'validate_agent_scope'),
                    mock.patch.object(loop_manager, 'run_agent') as reviewer,
                ):
                    resolved, message, stage = loop_manager._evaluate_remote_checkpoint_cause(
                        connection, Path('/repo'), Path('/state'), 'repo', blocked,
                        Path('/config'), 30,
                    )
                self.assertFalse(resolved)
                self.assertEqual(stage, 'worker')
                self.assertIn('Worker 결과 검증 실패', message)
                reviewer.assert_not_called()

    def test_remote_checkpoint_base_change_after_verify_skips_reviewer(self):
        from tests.support.database import runtime_connection
        from loop_manager_internal.storage import runtime_state

        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence='prior review failure', release_fingerprint='prior-input',
            plan_values={
                'worker_result': worker_result(),
                'publication_remote_oid': 'head',
            }, clear_projection=True,
        )
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        with (
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=None),
            mock.patch.object(loop_manager, 'verify') as verify,
            mock.patch.object(loop_manager, 'run_agent') as reviewer,
        ):
            resolved, _, stage = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertFalse(resolved)
        self.assertEqual(stage, 'stale_base')
        verify.assert_not_called()
        reviewer.assert_not_called()
        for revisions, expected_calls in (
            ([('base', 'head'), ('new-base', 'head')], 0),
            ([('base', 'head'), ('base', 'head'), ('new-base', 'head')], 1),
        ):
            with self.subTest(expected_calls=expected_calls):
                with (
                    mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                                      side_effect=revisions),
                    mock.patch.object(loop_manager, 'capture_git_snapshot',
                                      return_value=mock.Mock()),
                    mock.patch.object(loop_manager, 'verify', return_value=(True, '')),
                    mock.patch.object(loop_manager, 'validate_agent_scope'),
                    mock.patch.object(loop_manager, 'run_agent',
                                      return_value=(0, 'invalid review')) as reviewer,
                ):
                    resolved, message, stage = loop_manager._evaluate_remote_checkpoint_cause(
                        connection, Path('/repo'), Path('/state'), 'repo', blocked,
                        Path('/config'), 30,
                    )
                self.assertFalse(resolved)
                self.assertEqual(stage, 'revision_drift')
                self.assertIn('base 또는 HEAD', message)
                self.assertEqual(reviewer.call_count, expected_calls)
                if expected_calls:
                    prompt = reviewer.call_args.args[5]
                    self.assertIn('Worker 보고:\ntargeted test: PASS', prompt)
                    self.assertIn('Manager 확인: 전체 verify PASS', prompt)
                    self.assertIn('candidate HEAD=head', prompt)
                    self.assertIn('기록된 출력 없음', prompt)
        audit = connection.execute(
            "SELECT stage, classification, raw_output FROM validation_results "
            "WHERE repo = 'repo' AND plan_id = 'G-047' ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in audit],
            [('verify', 'success', ''), ('verify', 'success', '')],
        )

    def test_remote_checkpoint_revision_requires_latest_base_ancestry(self):
        responses = [
            SimpleNamespace(stdout='head\n'),
            SimpleNamespace(returncode=1),
        ]
        with mock.patch.object(loop_manager, '_recorded_base_fetch', return_value='base') as fetched, \
             mock.patch.object(loop_manager, 'git', side_effect=responses) as git:
            revision = loop_manager._remote_checkpoint_review_revision(
                Path('/repo'), Path('/worktree'), 'main', Path('/state'),
            )
        self.assertIsNone(revision)
        fetched.assert_called_once_with(
            Path('/repo'), Path('/state'), 'main', owner='project',
        )
        self.assertEqual(
            git.call_args_list[-1].args,
            (Path('/repo'), 'merge-base', '--is-ancestor', 'base', 'head'),
        )

    def test_remote_checkpoint_corrects_invalid_specification_to_pass(self):
        from tests.support.database import runtime_connection
        from loop_manager_internal.storage import runtime_state

        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence='prior review failure', release_fingerprint='prior-input',
            plan_values={
                'worker_result': worker_result(),
                'publication_remote_oid': 'head',
            },
            clear_projection=True,
        )
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        invalid = (
            'SPEC_DEADLOCK: {"kind":"missing","locations":'
            '[{"path":"docs/other.md","section":"Policy"}],'
            '"problem":"Missing rule","implementation_choice":"Choice",'
            '"question":"Which rule?"}\nVERDICT: SPEC_BLOCKED'
        )
        raw_verify = 'PRIVATE_SUCCESS_LOG\n' + 'x' * 9000
        snapshot = mock.Mock()
        with (
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
            mock.patch.object(loop_manager, 'verify', return_value=(
                True, loop_manager.VerifyOutput(raw_verify),
            )),
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=('base', 'head')),
            mock.patch.object(loop_manager, 'validate_agent_scope'),
            mock.patch.object(loop_manager, 'run_agent', side_effect=[
                (0, invalid), (0, 'VERDICT: PASS'),
            ]) as reviewer,
        ):
            result = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertEqual(result, (True, 'VERDICT: PASS', None))
        self.assertEqual(reviewer.call_count, 2)
        self.assertIn('직전 Reviewer 결과가 아래 프로토콜 검증에 실패',
                      reviewer.call_args_list[1].args[5])
        self.assertIn('docs/other.md', reviewer.call_args_list[1].args[5])
        audit = connection.execute(
            "SELECT definition_revision, stage, classification, raw_output "
            "FROM validation_results WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in audit],
            [('base', 'verify', 'success', raw_verify),
             ('base', 'review', 'success', 'VERDICT: PASS')],
        )
        for call in reviewer.call_args_list:
            self.assertNotIn('PRIVATE_SUCCESS_LOG', call.args[5])

    def test_remote_checkpoint_protocol_exhaustion_stops_at_three_attempts(self):
        from tests.support.database import runtime_connection
        from loop_manager_internal.storage import runtime_state
        from loop_manager_internal.storage import blocked_checkpoint_state

        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence='prior review failure', release_fingerprint='prior-input',
            plan_values={
                'worker_result': worker_result(),
                'publication_remote_oid': 'head',
            },
            clear_projection=True,
        )
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        checkpoint.finish_published(checkpoint.get('repo', 'G-047'), 'head')
        request = checkpoint.get('repo', 'G-047')
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        invalid = (
            'SPEC_DEADLOCK: {"kind":"missing","locations":'
            '[{"path":"docs/other.md","section":"Policy"}],'
            '"problem":"Missing rule","implementation_choice":"Choice",'
            '"question":"Which rule?"}\nVERDICT: SPEC_BLOCKED'
        )
        snapshot = mock.Mock()
        with (
            mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
            mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')),
            mock.patch.object(loop_manager, '_remote_checkpoint_review_revision',
                              return_value=('base', 'head')),
            mock.patch.object(loop_manager, 'validate_agent_scope'),
            mock.patch.object(loop_manager, 'run_agent', return_value=(0, invalid)) as reviewer,
            mock.patch.object(loop_manager, '_remote_checkpoint_has_new_failure') as fingerprint,
        ):
            resolved, evidence, stage = loop_manager._evaluate_remote_checkpoint_cause(
                connection, Path('/repo'), Path('/state'), 'repo', blocked,
                Path('/config'), 30,
            )
        self.assertFalse(resolved)
        self.assertEqual(stage, 'protocol_exhausted')
        self.assertEqual(reviewer.call_count, loop_manager.MAX_REVIEWER_PROTOCOL_ATTEMPTS)
        self.assertIn('프로토콜 교정 3회 실패', evidence)
        self.assertIn(invalid, evidence)
        fingerprint.assert_not_called()

        with (
            mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                              return_value='source-base'),
            mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint',
                              return_value='accepted-worktree'),
        ):
            loop_manager._reclassify_remote_checkpoint_block(
                connection, Path('/repo'), 'repo', blocked, checkpoint, request,
                'accepted-oid', Path('/state/G-047'),
                reason=loop_manager.BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
                evidence=evidence, review_fingerprint_source=None,
                user_targets=('docs/plans/[G-047] Runtime.md', 'docs/PRODUCT.md'),
            )
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        saved = connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(plan['blocked_reason'], 'review_protocol_exhausted')
        self.assertEqual(plan['review_result'], evidence)
        self.assertEqual(saved['phase'], 'published')
        self.assertEqual(saved['accepted_remote_oid'], 'accepted-oid')
        self.assertIsNone(saved['inspected_remote_oid'])
        next_snapshot = mock.Mock()
        next_snapshot.serialize.return_value = 'saved-worktree'
        with (
            mock.patch.object(loop_manager.checkpoint_adapter.BlockedCheckpointAdapter, 'observe_remote',
                              return_value='accepted-oid'),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot',
                              return_value=next_snapshot),
            mock.patch.object(loop_manager, '_nul_git_paths', return_value=()),
            mock.patch.object(loop_manager, 'run_agent') as repeated_reviewer,
        ):
            next_result = loop_manager._reconcile_published_blocked_checkpoint(
                connection, Path('/repo'), Path('/state'), 'repo', plan,
                saved, checkpoint.get('repo', 'G-047'), checkpoint,
            )
        self.assertEqual(next_result, 'unchanged')
        repeated_reviewer.assert_not_called()

    def test_remote_checkpoint_specification_block_preserves_published_identity(self):
        from tests.support.database import runtime_connection
        from loop_manager_internal.storage import runtime_state
        from loop_manager_internal.storage import blocked_checkpoint_state

        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        prior = 'ISSUE_ID: app.py::review::blocking\nReview failure\nVERDICT: FAIL'
        repository.block_plan(
            'repo', 'G-047', reason_code='review_retry_exhausted',
            evidence=prior, release_fingerprint='prior-input',
            plan_values={'review_result': prior, 'review_failure_history':
                         '{"prior": {"occurrences": 4}}'},
            clear_projection=True,
        )
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        initial_request = checkpoint.get('repo', 'G-047')
        checkpoint.finish_published(initial_request, 'published-oid')
        request = checkpoint.get('repo', 'G-047')
        blocked = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        evidence = {
            'kind': 'missing',
            'locations': [
                {'path': 'docs/ARCHITECTURE.md', 'section': 'Migration policy'},
            ],
            'problem': 'The decision is absent',
            'implementation_choice': 'Which migration to use',
            'question': 'Which migration should be used?',
        }
        review = f"SPEC_DEADLOCK: {json.dumps(evidence)}\nVERDICT: SPEC_BLOCKED"
        with (
            mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                              return_value='source-base'),
            mock.patch.object(loop_manager, '_revision_review_issue_fingerprint',
                              return_value='architecture-base'),
            mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint',
                              return_value='accepted-worktree'),
        ):
            loop_manager._reclassify_remote_specification_deadlock(
                connection, Path('/repo'), 'repo', blocked, checkpoint, request,
                'accepted-oid', Path('/state/G-047'), review,
            )
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        block = connection.execute(
            "SELECT * FROM execplan_blocks WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        saved = connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        recovery = connection.execute(
            "SELECT * FROM recovery_records WHERE repo = 'repo' AND subject = 'G-047'"
        ).fetchone()
        self.assertEqual(plan['status'], 'PLAN_BLOCKED')
        self.assertEqual(plan['blocked_reason'], 'review_specification_deadlock')
        self.assertEqual(plan['review_result'], review)
        self.assertEqual(plan['blocked_fingerprint'], 'source-base')
        self.assertEqual(plan['blocked_review_fingerprint'], 'architecture-base')
        self.assertEqual(json.loads(plan['review_failure_history']), {})
        self.assertEqual((block['reason_code'], block['evidence']),
                         ('review_specification_deadlock', review))
        self.assertEqual(saved['phase'], 'published')
        self.assertEqual(saved['published_oid'], 'published-oid')
        self.assertEqual(saved['accepted_remote_oid'], 'accepted-oid')
        self.assertEqual(saved['reason_code'], 'review_specification_deadlock')
        self.assertNotEqual(saved['request_identity'], request.request_identity)
        self.assertIsNone(saved['inspected_remote_oid'])
        self.assertIsNone(saved['inspected_input'])
        self.assertIsNone(saved['inspection_reason'])
        self.assertEqual(json.loads(recovery['final_evidence'])['user_targets'],
                         ['docs/ARCHITECTURE.md'])

        snapshot = mock.Mock()
        snapshot.serialize.return_value = 'saved-worktree'
        with (
            mock.patch.object(loop_manager.checkpoint_adapter.BlockedCheckpointAdapter, 'observe_remote',
                              return_value='accepted-oid'),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'capture_git_snapshot',
                              return_value=snapshot),
            mock.patch.object(loop_manager, '_nul_git_paths', return_value=()),
            mock.patch.object(loop_manager, 'run_agent') as reviewer,
        ):
            next_result = loop_manager._reconcile_published_blocked_checkpoint(
                connection, Path('/repo'), Path('/state'), 'repo', plan,
                saved, checkpoint.get('repo', 'G-047'), checkpoint,
            )
        self.assertEqual(next_result, 'unchanged')
        reviewer.assert_not_called()

    def test_review_specification_block_uses_source_revision_not_worker_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root, connection = self._failure_state(directory)
            self.addCleanup(connection.close)
            connection.execute(
                "UPDATE exec_plans SET status = 'REVIEWING', active_sha = 'base' "
                "WHERE repo = ? AND plan_id = 'G-013'", (str(root),)
            )
            connection.execute(
                "UPDATE projects SET phase = 'REVIEWING', "
                "agent_input_fingerprint = 'worktree-progress' WHERE repo = ?",
                (str(root),),
            )
            connection.commit()
            snapshot = mock.Mock()
            snapshot.serialize.return_value = 'saved-snapshot'
            with (
                mock.patch.object(loop_manager, '_worktree_execplan_input_fingerprint',
                                  return_value='worktree-progress'),
                mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                                  return_value='source-base') as source_input,
                mock.patch.object(loop_manager, '_revision_review_issue_fingerprint',
                                  return_value='spec-document-base'),
                mock.patch.object(loop_manager, 'capture_git_snapshot',
                                  return_value=snapshot),
                mock.patch.object(loop_manager, '_final_block_recovery_decision',
                                  return_value=mock.Mock()),
                mock.patch.object(loop_manager.runtime_state.RuntimeStateRepository,
                                  'block_plan') as block_plan,
            ):
                loop_manager.block_active_execplan(
                    connection, str(root), root, 'Specification missing', 'verify passed',
                    reason=loop_manager.BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
                )
            self.assertEqual(source_input.call_args.args[1], 'base')
            self.assertEqual(block_plan.call_args.kwargs['release_fingerprint'],
                             'source-base')
            self.assertEqual(
                block_plan.call_args.kwargs['plan_values']['blocked_review_fingerprint'],
                'spec-document-base',
            )
