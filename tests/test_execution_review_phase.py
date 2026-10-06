"""Review Phase execution policy regression tests."""
from contextlib import closing

from tests.support.execution import ExecutionFixture
from dataclasses import replace
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.execution.validation_feedback import Feedback
from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionOutcome, ExecutionOutcomeKind
from tests.support.blocked_git import blocked_git_fixture
from loop_manager_internal.storage import blocked_checkpoint_state


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionReviewPhaseTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_real_git_published_review_release_restart_and_correction(self):
        # Exercise the ownership boundary with real commits, refs, registration,
        # snapshots and SQLite persistence; only external PR/agent services are fake.
        fixture = blocked_git_fixture(self)
        repo = str(fixture.repo.resolve())
        branch = 'loop-manager/G-034'
        connection = fixture.connection
        connection.execute(
            "UPDATE exec_plans SET review_result = 'code.py: repair\nVERDICT: FAIL', "
            "worker_session_id = 'saved-worker'",
        )
        (fixture.worktree / 'code.py').write_text('checkpoint work\n')
        fixture.block('review_retry_exhausted', 'code.py: repair')
        with mock.patch.object(lm, 'pr_for_branch', return_value=None):
            self.assertEqual(lm.dispatch_blocked_checkpoints(
                connection, fixture.repo, fixture.state_dir, repo,
            ), 1)
        published = fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD')
        original = dict(connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE plan_id = 'G-034'",
        ).fetchone())
        self.assertEqual(original['published_oid'], published)
        self.assertEqual(original['accepted_remote_oid'], published)
        self.assertEqual(original['tree_oid'], fixture.run_git(
            fixture.worktree, 'rev-parse', f'{published}^{{tree}}',
        ))
        self.assertIn(f"Checkpoint-Identity: {original['commit_identity']}",
                      fixture.run_git(fixture.worktree, 'show', '-s', '--format=%B', published))

        def plan_row():
            return fixture.connection.execute(
                "SELECT * FROM exec_plans WHERE plan_id = 'G-034'",
            ).fetchone()

        with mock.patch.object(lm, 'pr_for_branch', return_value=None):
            # A real published commit must not excuse corrupted durable evidence.
            for column in ('request_identity', 'commit_identity', 'tree_oid'):
                with self.subTest(column=column):
                    connection.execute(
                        f"UPDATE blocked_checkpoints SET {column} = 'invalid'",
                    )
                    self.assertFalse(lm._resume_legacy_review_count_block(
                        connection, fixture.repo, fixture.state_dir, repo, plan_row(),
                    ))
                    connection.execute(
                        f"UPDATE blocked_checkpoints SET {column} = ?", (original[column],),
                    )
            self.assertTrue(lm._resume_legacy_review_count_block(
                connection, fixture.repo, fixture.state_dir, repo, plan_row(),
            ))
        self.assertEqual(plan_row()['status'], 'IMPLEMENTING')
        self.assertEqual(plan_row()['worker_session_id'], 'saved-worker')
        self.assertIn('VERDICT: FAIL', plan_row()['review_result'])
        self.assertIsNone(scoped_blocking.plan_block(connection, repo, 'G-034'))

        # Close the original connection and reconstruct both repositories and
        # handler, so ownership cannot depend on in-process state from release.
        database = fixture.state_dir / 'restart.db'
        connection.commit()
        with closing(sqlite3.connect(database)) as persisted, persisted:
            connection.backup(persisted)
        connection.close()
        fixture.connection = sqlite3.connect(database)
        fixture.connection.row_factory = sqlite3.Row
        connection = fixture.connection
        lm.hydrate_execplan_project(connection, repo, plan_row())
        checkpoint = ExecutionCheckpointRepository(connection)
        effects = replace(
            self.effects,
            state=ExecutionStatePort(connection, runtime_state.RuntimeStateRepository(connection), repo),
            checkpoint=checkpoint,
            workspace=replace(
                self.effects.workspace, git=lm.git,
                validate_execplan_workspace_identity=lm.validate_execplan_workspace_identity,
                validate_managed_worktree=lm.validate_managed_worktree,
            ),
        )
        handler = handlers.ExecutionHandlers(
            effects, repo=repo, repo_path=fixture.repo, state_dir=fixture.state_dir,
            base='main', base_sha=fixture.base_oid, provider='codex',
            config_path=Path('/config'), newly_activated=False,
        )
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-034'), branch=branch,
            worktree=fixture.worktree, active_sha=fixture.base_oid,
        )

        def assert_owned(expected):
            saved = checkpoint.get(repo, 'G-034')
            self.assertNotIn('blocked_resume', saved.snapshot)
            self.assertEqual(saved.snapshot['published_review_resume']['owner_oid'], published)
            self.assertEqual(handler._owned_checkpoint_remote(
                context, plan_row(), published, saved,
            ), expected)

        assert_owned(True)
        before = lm.capture_git_snapshot(fixture.worktree)
        (fixture.worktree / 'code.py').write_text('review correction\n')
        lm.validate_agent_scope(
            'worker', fixture.worktree, before, lm.capture_git_snapshot(fixture.worktree),
            selected_plan_path=plan_row()['path'],
        )
        fixture.run_git(fixture.worktree, 'add', '--', 'code.py')
        handler._commit_owned(context, 'Repair review issue')
        corrected = fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(corrected, published)
        self.assertEqual(fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD^'), published)
        self.assertEqual(checkpoint.get(repo, 'G-034').snapshot['commit_tree_oid'],
                         fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD^{tree}'))
        assert_owned(True)
        self.assertEqual(lm.git(fixture.repo, 'ls-remote', '--heads', 'origin',
                               f'refs/heads/{branch}').stdout.split()[0], published)
        self.assertEqual(dict(connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE plan_id = 'G-034'",
        ).fetchone()), original)
        effects.validation.verify.assert_not_called()
        effects.agent.run_agent.assert_not_called()
        effects.publication.push_managed_publication_branch.assert_not_called()
        effects.publication.command.assert_not_called()
        fixture.verify_probe.assert_not_called()
        fixture.reviewer_probe.assert_not_called()
        # A successor pushed by somebody else invalidates the same durable owner.
        fixture.user_commit(contents='external correction\n')
        assert_owned(False)


    def test_published_review_commit_correction_keeps_owner_on_restart(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', "
            "'review_retry_exhausted', 'review', 'loop-manager/G-047', "
            "?, 'old-head')", (str(Path('/state/G-047')),)
        )
        self.state.update_project('repo', phase='VERIFYING',
                                  publication_remote_oid='old-head')
        self.dirty = False
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='commit',
            expected_head='old-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={'published_review_resume': {
                'request_identity': 'request', 'owner_oid': 'old-head',
                'head': 'old-head',
            }, 'before_head': 'old-head',
                'commit_message': 'Worker correction',
                'commit_tree_oid': 'c' * 40},
        )

        def observed_git(path, *args, **kwargs):
            if args in {('rev-parse', 'HEAD'),
                        ('rev-parse', 'refs/heads/loop-manager/G-047')}:
                return SimpleNamespace(stdout='corrected-head\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout='old-head\trefs/heads/loop-manager/G-047\n',
                    returncode=0,
                )
            if args == ('show', '-s', '--format=%P', 'corrected-head'):
                return SimpleNamespace(stdout='old-head\n', returncode=0)
            if args == ('show', '-s', '--format=%B', 'corrected-head'):
                return SimpleNamespace(stdout='Worker correction\n', returncode=0)
            if args == ('rev-parse', 'corrected-head^{tree}'):
                return SimpleNamespace(stdout='c' * 40 + '\n', returncode=0)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'old-head'
        self.effects.publication.pr_for_branch.return_value = None
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='old-head'):
            first = self.handler.recover(self.context())
            saved = checkpoint.get('repo', 'G-047')
            self.assertEqual(first.phase, ExecutionPhase.VERIFYING)
            self.assertIsNone(saved.operation)
            self.assertEqual(saved.expected_head, 'corrected-head')
            self.assertEqual(saved.snapshot['published_review_resume'], {
                'request_identity': 'request', 'owner_oid': 'old-head',
                'head': 'old-head',
            })
            self.assertEqual(self.handler.recover(self.context()).phase,
                             ExecutionPhase.VERIFYING)
        self.effects.publication.command.assert_not_called()


    def test_published_review_push_restarts_before_completion_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', "
            "'review_retry_exhausted', 'review', 'loop-manager/G-047', "
            "?, 'old-head')", (str(Path('/state/G-047')),)
        )
        self.state.update_project('repo', publication_remote_oid='corrected-head')
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='push',
            expected_head='corrected-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={
                'published_review_resume': {
                    'request_identity': 'request', 'owner_oid': 'old-head',
                    'head': 'old-head',
                },
                'remote_before': 'old-head',
            },
        )

        def observed_git(path, *args, **kwargs):
            if args in {('rev-parse', 'HEAD'),
                        ('rev-parse', 'refs/heads/loop-manager/G-047')}:
                return SimpleNamespace(stdout='corrected-head\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout='corrected-head\trefs/heads/loop-manager/G-047\n',
                    returncode=0,
                )
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'corrected-head'
        self.effects.publication.pr_for_branch.return_value = None
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='old-head'):
            pending = checkpoint.get('repo', 'G-047')
            self.assertNotIn('pushed_checkpoint_head', pending.snapshot)
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), self.state.row(), 'corrected-head', pending,
            ))
            self.handler.recover(self.context())
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'corrected-head')
        self.assertEqual(self.state.row()['publication_remote_oid'], 'corrected-head')


    def test_published_review_resume_push_completion_survives_restart(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', "
            "'review_retry_exhausted', 'review', 'loop-manager/G-047', "
            "?, 'owned-head')", (str(Path('/state/G-047')),)
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='push',
            expected_head='corrected-head', target_base_oid='base',
            observed_remote_oid='owned-head',
            snapshot={
                'published_review_resume': {
                    'request_identity': 'request', 'owner_oid': 'owned-head',
                    'head': 'owned-head',
                },
                'remote_before': 'owned-head',
            },
        )
        self.handler._complete_publication_intent(self.context())
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'corrected-head')
        self.assertEqual(saved.observed_remote_oid, 'corrected-head')

        def git(_path, *args, **_kwargs):
            if args[0] == 'rev-parse':
                return SimpleNamespace(stdout='corrected-head\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout='corrected-head\trefs/heads/loop-manager/G-047\n',
                    returncode=0,
                )
            raise AssertionError(args)

        self.effects.workspace.git.side_effect = git
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='owned-head'):
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(),
                {'pr_url': None, 'publication_remote_oid': 'corrected-head'},
                'corrected-head', checkpoint.get('repo', 'G-047'),
            ))


    def test_published_review_resume_rechecks_identity_and_live_owner(self):
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', "
            "'review_retry_exhausted', 'review', 'loop-manager/G-047', "
            "?, 'owned-head')", (str(Path('/state/G-047')),)
        )
        context = self.context()
        marker = {'request_identity': 'request', 'owner_oid': 'owned-head',
                  'head': 'owned-head'}
        current = SimpleNamespace(
            operation=None, expected_head='owned-head',
            target_base_oid='new-base', observed_remote_oid='prior-head',
            snapshot={'published_review_resume': marker},
            completed_rebase=lambda: None,
        )
        row = {'pr_url': None, 'publication_remote_oid': 'owned-head'}
        live = {'oid': 'owned-head'}

        def git(_path, *args, **_kwargs):
            if args[0] == 'rev-parse':
                return SimpleNamespace(stdout='owned-head\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout=f"{live['oid']}\trefs/heads/loop-manager/G-047\n",
                    returncode=0,
                )
            raise AssertionError(args)

        self.effects.workspace.git.side_effect = git
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='owned-head'):
            self.assertTrue(self.handler._owned_checkpoint_remote(
                context, row, live['oid'], current,
            ))
            live['oid'] = 'external'
            self.assertFalse(self.handler._owned_checkpoint_remote(
                context, row, live['oid'], current,
            ))
            live['oid'] = 'owned-head'
            marker['request_identity'] = 'other-request'
            self.assertFalse(self.handler._owned_checkpoint_remote(
                context, row, live['oid'], current,
            ))
            marker['request_identity'] = 'request'
            row['publication_remote_oid'] = 'other-head'
            self.assertFalse(self.handler._owned_checkpoint_remote(
                context, row, live['oid'], current,
            ))


    def test_review_budget_pauses_before_worker_and_preserves_projection(self):
        run_id = handlers.recovery_state.begin_external_run(self.connection)
        feedback = ValidationFeedbackRepository(self.connection)
        for index in range(4):
            review = f"ISSUE_ID: app.py::issue-{index}::blocking\nVERDICT: FAIL"
            feedback.observe('repo', 'G-047', 'base', 'review', False, review,
                             (Feedback('review', f'review:app.py::issue-{index}::blocking', review),))
            result_id = self.connection.execute(
                "SELECT MAX(id) FROM validation_results"
            ).fetchone()[0]
            if index < 3:
                self.assertEqual(
                    handlers.recovery_state.reserve_review_correction(
                        self.connection, 'repo', 'G-047', result_id,
                    ), (True, index + 1, run_id),
                )
        self.state.update_project(
            'repo', phase='WORKING', review_result=review,
            last_error='Review 실패:\n' + review,
            worker_session_id='worker-session',
        )
        before = self.state.check()
        outcome = self.handler.run(self.context())
        after = self.state.check()

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual((after.status, after.worker_session_id, after.review_result),
                         (before.status, before.worker_session_id, before.review_result))
        self.assertIn('used=3/3', outcome.detail)
        self.effects.agent.run_agent.assert_not_called()
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM blocked_checkpoints WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone())
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE repo = 'repo' AND event_type IN ('blocked', 'resumed')"
        ).fetchone()[0], 0)


    def test_review_pass_after_third_correction_advances_to_publication(self):
        run_id = handlers.recovery_state.begin_external_run(self.connection)
        for result_id in range(1, 4):
            self.assertEqual(
                handlers.recovery_state.reserve_review_correction(
                    self.connection, 'repo', 'G-047', result_id,
                ), (True, result_id, run_id),
            )
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        self.effects.agent.run_agent.side_effect = [lm.AgentResult(
            0, 'VERDICT: PASS', session_id='review-session',
        )]
        outcome = self.handler.review(self.review_context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))


    def test_worker_result_correction_does_not_consume_review_budget(self):
        run_id = handlers.recovery_state.begin_external_run(self.connection)
        feedback = ValidationFeedbackRepository(self.connection)
        for result_id in range(1, 5):
            review = f'ISSUE_ID: app.py::issue-{result_id}::blocking\nVERDICT: FAIL'
            feedback.observe('repo', 'G-047', 'base', 'review', False, review)
            if result_id <= 3:
                self.assertEqual(
                    handlers.recovery_state.reserve_review_correction(
                        self.connection, 'repo', 'G-047', result_id,
                    ), (True, result_id, run_id),
                )
        self.state.update_project(
            'repo', phase='WORKING', review_result=review,
            last_error='Worker 결과 검증 실패: format',
        )
        handlers.recovery_state.RecoveryRepository(self.connection).observe(
            handlers.RecoveryObservation(
                handlers.RecoveryReason.WORKER_CORRECTION,
                'repo', 'G-047', 'IMPLEMENTING', 'base', 'base',
                'worker-format', 'worker-session', 'invalid Worker result',
            ),
        )
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'worker failed', session_id='worker-session',
        )
        outcome = self.handler.run(self.context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.OPERATIONAL_ERROR)
        self.effects.agent.run_agent.assert_called_once()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM review_correction_turns WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0], 3)


    def test_verified_correction_and_execution_projection_roll_back_together(self):
        from loop_manager_internal.storage import review_correction_state
        state = {'version': 1, 'level': 1, 'baseline': ['issue'],
                 'failures': 0, 'issues': {}, 'pending': {
                     'source': 1, 'level': 1, 'completed': True, 'verified_head': None,
                 }}
        review_correction_state.save(self.connection, 'repo', 'G-047', state)
        old_phase = self.connection.execute(
            "SELECT phase FROM projects WHERE repo = 'repo'"
        ).fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with runtime_state._transaction(self.connection):
                review_correction_state.verified(self.connection, 'repo', 'G-047', 'verified-head')
                lm.clear_validation_failure_history(self.connection, 'repo', 'verify')
                self.state.update_project('repo', phase='REVIEWING', verify_result='PASS')
                raise RuntimeError('interrupted')
        self.assertEqual(review_correction_state.load(self.connection, 'repo', 'G-047'), state)
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM projects WHERE repo = 'repo'"
        ).fetchone()[0], old_phase)

    def test_review_of_old_level_candidate_does_not_establish_new_baseline(self):
        from loop_manager_internal.storage import review_correction_state
        state = {'version': 1, 'level': 1, 'baseline': ['issue'],
                 'failures': 2, 'issues': {'issue': 2}, 'pending': None}
        review_correction_state.save(self.connection, 'repo', 'G-047', state)
        self.connection.execute(
            "UPDATE exec_plans SET effective_worker_level = 2 WHERE plan_id = 'G-047'"
        )
        self.connection.commit()
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        self.effects.agent.run_agent.side_effect = [lm.AgentResult(
            0, 'ISSUE_ID: app.py::persistent::blocking\nFix it\nVERDICT: FAIL',
            session_id='review-session',
        )]
        self.effects.validation.transition_validation_failure.side_effect = self.state.bind(
            lm.transition_validation_failure, connected=True, mutates=True,
            name='transition_validation_failure',
        )
        self.handler.review(self.review_context())
        stored = review_correction_state.load(self.connection, 'repo', 'G-047')
        self.assertEqual(stored, state)
        self.assertEqual(self.connection.execute(
            "SELECT review_level_history FROM exec_plans WHERE plan_id = 'G-047'"
        ).fetchone()[0], '{}')

    def test_review_observation_tracks_each_issue_and_pass_resolves_them(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='fingerprint',
        )
        fail = ('ISSUE_ID: z.py::f::bug\nFix Z\n'
                'ISSUE_ID: a.py::g::bug\nFix A\nVERDICT: FAIL')
        self.effects.agent.run_agent.side_effect = [
            lm.AgentResult(0, fail, session_id='review-session'),
            lm.AgentResult(0, 'VERDICT: PASS', session_id='review-session'),
        ]

        self.handler.review(self.review_context())
        self.assertEqual([row['identity'] for row in feedback.prepare(
            'repo', 'G-047', 'worker-session',
        ).rows], ['review:a.py::g::bug', 'review:z.py::f::bug'])
        self.handler.review(self.review_context())
        self.assertEqual(feedback.prepare('repo', 'G-047', 'worker-session').rows, ())
        self.assertEqual(self.connection.execute(
            "SELECT classification FROM validation_results ORDER BY id"
        ).fetchall()[0][0], 'failure')

    def test_review_failure_redacts_transition_and_operation_output(self):
        secret = 'review-handler-secret'
        review = (
            'ISSUE_ID: app.py::config::secret\n'
            f'password: {secret}\nVERDICT: FAIL'
        )
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='failure',
        )
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, review, session_id='review-session',
        )

        self.handler.review(self.review_context())

        self.assertNotIn(
            secret,
            self.effects.validation.transition_validation_failure.call_args.args[3],
        )
        self.assertTrue(all(
            secret not in str(call.args)
            for call in self.effects.events.operation_print.call_args_list
        ))


    def test_review_retry_audits_each_returned_attempt(self):
        self._with_feedback(ValidationFeedbackRepository(self.connection))
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        raw = 'service unavailable: original reviewer output'
        self.effects.agent.run_agent.side_effect = [
            lm.AgentResult(1, raw, kind=lm.AgentResultKind.TRANSIENT_FAILURE),
            lm.AgentResult(0, 'VERDICT: PASS'),
        ]
        self.handler.review(self.review_context())
        audit = self.connection.execute(
            "SELECT classification,raw_output FROM validation_results ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in audit],
                         [('transient_failure', raw), ('success', 'VERDICT: PASS')])


    def test_review_post_call_rejections_audit_each_execution_once(self):
        self._with_feedback(ValidationFeedbackRepository(self.connection))
        for code in (0, 1):
            for rejection in ('scope', 'ownership', 'snapshot', 'stale'):
                with self.subTest(code=code, rejection=rejection):
                    raw = f'reviewer output {code} {rejection}'
                    result = lm.AgentResult(code, raw)
                    self.effects.agent.run_agent.side_effect = lambda *a, **k: result
                    self.effects.agent.validate_agent_scope.side_effect = (
                        lm.AgentScopeError('scope') if rejection == 'scope' else None)
                    self.effects.workspace.validate_managed_worktree.side_effect = (
                        lm.WorkspaceOwnershipError('ownership')
                        if rejection == 'ownership' else None)
                    self.effects.agent.capture_git_snapshot.side_effect = (
                        RuntimeError('snapshot') if rejection == 'snapshot' else None)
                    context = self.review_context()
                    stale = ExecutionOutcome(ExecutionOutcomeKind.RETRY, detail='stale')
                    with mock.patch.object(self.handler, '_block_final'), mock.patch.object(
                        self.handler, '_post_effect_guard', return_value=stale,
                    ):
                        outcome = self.handler.review(context)
                    if rejection == 'stale':
                        self.assertIs(outcome, stale)
                    audit = self.connection.execute(
                        "SELECT classification FROM validation_results WHERE raw_output=?",
                        (raw,),
                    ).fetchall()
                    expected = (result.ledger_kind if code else
                                'stale_result' if rejection == 'stale' else 'scope_failure')
                    self.assertEqual([row[0] for row in audit], [expected])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_events"
        ).fetchone()[0], 0)


    def test_review_retry_interruption_preserves_previous_raw_result(self):
        self._with_feedback(ValidationFeedbackRepository(self.connection))
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        raw = 'service unavailable: retained before retry'
        self.effects.agent.run_agent.side_effect = [
            lm.AgentResult(1, raw, kind=lm.AgentResultKind.TRANSIENT_FAILURE),
            KeyboardInterrupt(),
        ]
        with self.assertRaises(KeyboardInterrupt):
            self.handler.review(self.review_context())
        audit = self.connection.execute(
            "SELECT classification,raw_output FROM validation_results ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in audit], [('transient_failure', raw)])


    def test_review_failure_checkpoint_resumes_worker_without_pr_lookup(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', active_sha='base',
            validated_base_sha=None, worker_session_id='worker-session',
            worker_result='previous worker result',
            review_result='VERDICT: FAIL', last_error='Review 실패: fix issue',
        )
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(self.state.row()['last_error'], 'Review 실패: fix issue')
        self.assertEqual(self.state.row()['worker_result'], 'previous worker result')
        self.effects.publication.pr_for_branch.assert_not_called()


    def test_review_specification_deadlock_blocks_plan_without_worker_correction(self):
        from loop_manager_internal.documents.reviewer_specification import SpecificationDeadlock

        self.plan['sources'] = ['docs/requirements/source.md']
        self.handler.plan = self.plan
        details = SpecificationDeadlock(
            'missing', (('docs/PRODUCT.md', 'Review policy'),),
            'A retention decision is absent', 'Whether to retain the result',
            'How long should the result be retained?',
        )
        self.effects.validation.parse_reviewer_result.return_value = (
            'SPEC_BLOCKED', details,
        )
        result = self.handler.review(self.review_context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual([call for call in self.calls if call == 'reviewer'], ['reviewer'])
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs['reason'],
            lm.BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
        )
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs[
                'recovery_decision'
            ].observation.user_targets,
            ('docs/PRODUCT.md',),
        )
        self.effects.validation.transition_validation_failure.assert_not_called()


    def test_review_deadlock_accepts_architecture_outside_sources(self):
        from loop_manager_internal.documents.reviewer_specification import SpecificationDeadlock

        self.plan['sources'] = ['docs/requirements/source.md']
        self.handler.plan = self.plan
        details = SpecificationDeadlock(
            'missing', (('docs/ARCHITECTURE.md', 'Architecture Change Policy'),),
            'The migration choice is unspecified', 'Which migration to use',
            'Which migration policy should apply?',
        )
        self.effects.validation.parse_reviewer_result.return_value = (
            'SPEC_BLOCKED', details,
        )
        result = self.handler.review(self.review_context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs[
                'recovery_decision'
            ].observation.user_targets,
            ('docs/ARCHITECTURE.md',),
        )


    def test_review_deadlock_with_unrelated_path_uses_protocol_correction(self):
        from loop_manager_internal.documents.reviewer_specification import SpecificationDeadlock

        details = SpecificationDeadlock(
            'missing', (('docs/other.md', 'Policy'),),
            'Missing decision', 'Whether to retain', 'What is the rule?',
        )
        self.effects.validation.parse_reviewer_result.side_effect = [
            ('SPEC_BLOCKED', details), ('PASS', ()),
        ]
        result = self.handler.review(self.review_context())
        self.assertEqual(result.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(
            [call for call in self.calls if call == 'reviewer'],
            ['reviewer', 'reviewer'],
        )
        self.effects.blocking.block_active_execplan.assert_not_called()
