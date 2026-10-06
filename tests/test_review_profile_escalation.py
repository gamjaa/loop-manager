from tests.support.review_escalation import WorkerEscalationFixture
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.storage import review_correction_state
from tests.support.profiles import profile_config
from tests.support.catalog import make_catalog_root, write_execplan
from tests.support.worker import dummy_worker_sessions


class ReviewDrivenWorkerProfileEscalationTest(WorkerEscalationFixture, unittest.TestCase):
    def test_level_fixture_closes_database_when_setup_fails(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError("database setup failed")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            loop_manager, "connect_database", return_value=connection
        ):
            with self.assertRaisesRegex(RuntimeError, "database setup failed"):
                self._level_state(directory, initial_level=1)
        connection.close.assert_called_once_with()

    def test_level_fixture_leaves_returned_file_connection_to_caller(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            with mock.patch.object(self, "addCleanup") as register_cleanup:
                _root, _state_dir, connection = self._level_state(
                    directory, initial_level=1
                )
            cleanup.callback(connection.close)
            register_cleanup.assert_not_called()
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    @dummy_worker_sessions
    def test_new_execplan_stores_capped_initial_and_effective_level(self):
        cases = tuple(
            (complexity, risk, base + (risk == 'critical'))
            for complexity, base in (('low', 1), ('medium', 3), ('high', 5))
            for risk in ('low', 'medium', 'high', 'critical')
        )
        for complexity, risk, expected in cases:
            with self.subTest(complexity=complexity, risk=risk):
                self.assertEqual(
                    loop_manager.worker_level(
                        {"complexity": complexity, "risk": risk}
                    ),
                    expected,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            plan_path = write_execplan(
                root,
                "G-019",
                "Review Escalation",
                complexity="high",
                risk="critical",
            )
            plan = loop_manager.validate_execplan(
                root, plan_path.relative_to(root).as_posix()
            )
            connection = sqlite3.connect(":memory:")
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(root),),
            )
            loop_manager.register_execplans(
                connection,
                str(root),
                root,
                [(plan_path.relative_to(root).as_posix(), plan)],
                "plan-sha",
                "planning-fingerprint",
            )
            stored = connection.execute(
                """
                SELECT initial_worker_level, effective_worker_level,
                       review_level_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(stored["initial_worker_level"], 6)
        self.assertEqual(stored["effective_worker_level"], 6)
        self.assertEqual(json.loads(stored["review_level_history"]), {})

    def _completed_fail(self, previous, fingerprints, *, level=None, head='candidate'):
        state = json.loads(json.dumps(previous.correction_state))
        state['pending'] = {'source': 1, 'level': previous.effective_level,
                            'completed': True, 'verified_head': head}
        return loop_manager.evaluate_review_worker_level(
            previous.effective_level, previous.review_history, fingerprints, state,
            review_level=level or previous.effective_level, candidate_head=head,
        )

    def test_review_state_transition_escalates_one_level_and_caps_at_eight(self):
        history = {}
        effective_level = 1
        for expected_level in range(2, 9):
            issue = (f'same-at-{effective_level}',)
            first = loop_manager.evaluate_review_worker_level(effective_level, history, issue)
            self.assertFalse(first.escalated)
            second = self._completed_fail(first, issue)
            self.assertFalse(second.escalated)
            third = self._completed_fail(second, issue)
            self.assertTrue(third.escalated)
            self.assertEqual(third.effective_level, expected_level)
            history, effective_level = third.review_history, third.effective_level
        capped = loop_manager.evaluate_review_worker_level(8, history, ('a', 'b', 'c'))
        for _ in range(4):
            capped = self._completed_fail(capped, ('a', 'b', 'c'))
        self.assertFalse(capped.escalated)
        self.assertEqual(capped.effective_level, 8)

    def test_multi_issue_baseline_and_changing_corrections_count_results(self):
        first = loop_manager.evaluate_review_worker_level(4, {}, ('a', 'b', 'c'))
        self.assertFalse(first.escalated)
        second = self._completed_fail(first, ('d', 'e', 'f'))
        third = self._completed_fail(second, ('g', 'h', 'i'))
        self.assertFalse(third.escalated)
        fourth = self._completed_fail(third, ('j', 'k', 'l'))
        self.assertEqual(fourth.effective_level, 5)
        self.assertEqual(fourth.correction_state['failures'], 3)

    def test_a_b_a_requires_two_completed_a_corrections(self):
        first = loop_manager.evaluate_review_worker_level(1, {}, ('a',))
        second = self._completed_fail(first, ('b',))
        third = self._completed_fail(second, ('a',))
        self.assertFalse(third.escalated)
        self.assertEqual(self._completed_fail(third, ('a',)).effective_level, 2)

    def test_legacy_history_duplicate_review_and_stale_level_do_not_escalate(self):
        first = loop_manager.evaluate_review_worker_level(4, {'4': {'a': 10}}, ('a',))
        self.assertFalse(first.escalated)
        duplicate = loop_manager.evaluate_review_worker_level(
            4, first.review_history, ('a',), first.correction_state,
        )
        self.assertEqual(duplicate, first)
        stale = self._completed_fail(first, ('a',), level=3)
        self.assertFalse(stale.escalated)
        self.assertEqual(stale.correction_state['failures'], 0)
        state = json.loads(json.dumps(stale.correction_state))
        state['pending']['completed'] = False
        incomplete = loop_manager.evaluate_review_worker_level(
            4, first.review_history, ('a',), state, candidate_head='candidate',
        )
        self.assertEqual(incomplete.correction_state['failures'], 0)
        state['pending']['completed'] = True
        mismatched = loop_manager.evaluate_review_worker_level(
            4, first.review_history, ('a',), state, candidate_head='different-head',
        )
        self.assertEqual(mismatched.correction_state['failures'], 0)

    def _correct_review(self, connection, root, output, source):
        repo = str(root)
        level = connection.execute(
            "SELECT effective_worker_level FROM exec_plans WHERE plan_id = 'G-019'"
        ).fetchone()[0]
        review_correction_state.begin_worker(connection, repo, 'G-019', level, source)
        review_correction_state.complete_worker(connection, repo, 'G-019', level)
        review_correction_state.verified(connection, repo, 'G-019', 'candidate')
        return loop_manager.transition_validation_failure(
            connection, repo, root, 'review', output,
            review_level=level, candidate_head='candidate',
        )

    @dummy_worker_sessions
    def test_review_level_persists_with_global_history_and_blocked_reset(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root, state_dir, connection = self._level_state(
                directory, initial_level=7
            )
            cleanup.callback(connection.close)
            output = (
                "ISSUE_ID: app.py::same-issue::blocking\n"
                "Actionable issue\nVERDICT: FAIL"
            )
            first = loop_manager.transition_validation_failure(
                connection, str(root), root, "review", output
            )
            connection.close()

            reopened = loop_manager.connect_database(state_dir)
            cleanup.callback(reopened.close)
            before = reopened.execute(
                """
                SELECT effective_worker_level, review_level_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            self.assertEqual(before["effective_worker_level"], 7)
            self.assertEqual(
                json.loads(before["review_level_history"])["7"][first.fingerprint],
                1,
            )

            second = self._correct_review(reopened, root, output, 1)
            self.assertEqual(reopened.execute(
                "SELECT effective_worker_level FROM exec_plans WHERE plan_id = 'G-019'"
            ).fetchone()[0], 7)
            second = self._correct_review(reopened, root, output, 2)
            escalated = reopened.execute(
                """
                SELECT effective_worker_level, review_level_history,
                       review_failure_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            self.assertEqual(escalated["effective_worker_level"], 8)
            self.assertEqual(
                json.loads(escalated["review_level_history"])["7"][second.fingerprint],
                3,
            )
            self.assertEqual(
                json.loads(escalated["review_failure_history"])[second.fingerprint][
                    "occurrences"
                ],
                3,
            )

            reopened.execute(
                "UPDATE exec_plans SET status = 'BLOCKED' "
                "WHERE repo = ? AND plan_id = 'G-019'",
                (str(root),),
            )
            reopened.execute(
                "UPDATE projects SET phase = 'BLOCKED' WHERE repo = ?",
                (str(root),),
            )
            reopened.commit()
            loop_manager._requeue_blocked_execplan(
                reopened, str(root), "G-019"
            )
            reset = reopened.execute(
                """
                SELECT initial_worker_level, effective_worker_level,
                       review_level_history, review_failure_history, review_correction_state
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            reopened.close()

        self.assertEqual(reset["initial_worker_level"], 7)
        self.assertEqual(reset["effective_worker_level"], 7)
        self.assertEqual(json.loads(reset["review_level_history"]), {})
        self.assertEqual(json.loads(reset["review_failure_history"]), {})
        self.assertEqual(json.loads(reset["review_correction_state"]), {})

    def test_incomplete_verified_turn_restart_and_duplicate_observation(self):
        issue = 'ISSUE_ID: app.py::persistent::blocking\nFix it\nVERDICT: FAIL'
        with tempfile.TemporaryDirectory() as directory:
            root, state_dir, connection = self._level_state(directory, initial_level=4)
            try:
                loop_manager.transition_validation_failure(connection, str(root), root, 'review', issue)
                review_correction_state.begin_worker(connection, str(root), 'G-019', 4, 1)
                # CLI/protocol failure cannot turn an incomplete Worker into a correction.
                review_correction_state.verified(connection, str(root), 'G-019', 'candidate')
                loop_manager.transition_validation_failure(connection, str(root), root, 'review', issue)
                self.assertEqual(review_correction_state.load(connection, str(root), 'G-019')['failures'], 0)
                review_correction_state.complete_worker(connection, str(root), 'G-019', 4)
                before_verify = review_correction_state.load(connection, str(root), 'G-019')
                loop_manager.transition_validation_failure(connection, str(root), root, 'verify', 'unit failed')
                self.assertEqual(review_correction_state.load(connection, str(root), 'G-019'), before_verify)
            finally:
                connection.close()
            connection = loop_manager.connect_database(state_dir)
            try:
                pending = review_correction_state.load(connection, str(root), 'G-019')['pending']
                self.assertTrue(pending['completed'])
                self.assertIsNone(pending['verified_head'])
                # Verify remediation remains the same Review feedback turn.
                review_correction_state.begin_worker(connection, str(root), 'G-019', 4, None)
                self.assertEqual(review_correction_state.load(connection, str(root), 'G-019')['pending']['source'], 1)
                self._correct_review(connection, root, issue, 1)
                counted = review_correction_state.load(connection, str(root), 'G-019')
                self.assertEqual(counted['failures'], 1)
                loop_manager.transition_validation_failure(connection, str(root), root, 'review', issue)
                self.assertEqual(review_correction_state.load(connection, str(root), 'G-019'), counted)
                self._correct_review(connection, root, issue, 2)
                self.assertEqual(connection.execute(
                    "SELECT effective_worker_level FROM exec_plans WHERE plan_id = 'G-019'"
                ).fetchone()[0], 5)
                # PASS ends the active episode while keeping level/audit.
                audit = connection.execute(
                    "SELECT review_level_history FROM exec_plans WHERE plan_id = 'G-019'"
                ).fetchone()[0]
                loop_manager.clear_validation_failure_history(connection, str(root), 'review')
                closed = review_correction_state.load(connection, str(root), 'G-019')
                self.assertTrue(closed['closed'])
                self.assertEqual(closed['failures'], 0)
                self.assertEqual([entry['source_review_id'] for entry in closed['audit']
                                  if entry['kind'] == 'correction'], [1, 2])
                next_baseline = loop_manager.evaluate_review_worker_level(5, {}, ('new',), closed)
                self.assertFalse(next_baseline.escalated)
                self.assertEqual(next_baseline.correction_state['failures'], 0)
                self.assertEqual(len(next_baseline.correction_state['audit']), 4)
                self.assertEqual(connection.execute(
                    "SELECT review_level_history FROM exec_plans WHERE plan_id = 'G-019'"
                ).fetchone()[0], audit)
            finally:
                connection.close()

    def test_failed_transition_does_not_consume_completed_correction(self):
        from loop_manager_internal.storage import runtime_state
        issue = 'ISSUE_ID: app.py::persistent::blocking\nFix it\nVERDICT: FAIL'
        with tempfile.TemporaryDirectory() as directory:
            root, _state_dir, connection = self._level_state(directory, initial_level=4)
            try:
                loop_manager.transition_validation_failure(connection, str(root), root, 'review', issue)
                self._correct_review(connection, root, issue, 1)
                before = review_correction_state.load(connection, str(root), 'G-019')
                with mock.patch.object(runtime_state.RuntimeStateRepository, 'transition', side_effect=RuntimeError('write failed')):
                    with self.assertRaisesRegex(RuntimeError, 'write failed'):
                        self._correct_review(connection, root, issue, 2)
                after = review_correction_state.load(connection, str(root), 'G-019')
                self.assertEqual(after['failures'], before['failures'])
                self.assertTrue(after['pending']['completed'])
                self.assertEqual(connection.execute(
                    "SELECT effective_worker_level FROM exec_plans WHERE plan_id = 'G-019'"
                ).fetchone()[0], 4)
                # Retry the same verified result; consume it once with the level update.
                loop_manager.transition_validation_failure(
                    connection, str(root), root, 'review', issue,
                    review_level=4, candidate_head='candidate',
                )
                self.assertEqual(connection.execute(
                    "SELECT effective_worker_level FROM exec_plans WHERE plan_id = 'G-019'"
                ).fetchone()[0], 5)
            finally:
                connection.close()

    @dummy_worker_sessions
    def test_verify_and_invalid_review_do_not_change_escalation_history(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            root, _state_dir, connection = self._level_state(
                directory, initial_level=4
            )
            cleanup.callback(connection.close)
            review = (
                "ISSUE_ID: app.py::review-only::blocking\n"
                "Actionable issue\nVERDICT: FAIL"
            )
            loop_manager.transition_validation_failure(
                connection, str(root), root, "review", review
            )
            before = connection.execute(
                """
                SELECT effective_worker_level, review_level_history, review_correction_state
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()

            loop_manager.transition_validation_failure(
                connection, str(root), root, "verify", "unit failure"
            )
            with self.assertRaises(loop_manager.ReviewerProtocolError):
                loop_manager.transition_validation_failure(
                    connection,
                    str(root),
                    root,
                    "review",
                    "VERDICT: FAIL",
                )
            after = connection.execute(
                """
                SELECT effective_worker_level, review_level_history, review_correction_state
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(before["effective_worker_level"], 4)
        self.assertEqual(after["effective_worker_level"], 4)
        self.assertEqual(after["review_level_history"], before["review_level_history"])
        self.assertEqual(after["review_correction_state"], before["review_correction_state"])

    @dummy_worker_sessions
    def test_worker_uses_escalated_level_after_two_completed_matching_corrections(self):
        issue = (
            "ISSUE_ID: app.py::persistent-issue::blocking\n"
            "Actionable issue\nVERDICT: FAIL"
        )
        reviews = (
            loop_manager.CodexResult(0, issue, "reviewer-session"),
            loop_manager.CodexResult(0, issue, "reviewer-session"),
            loop_manager.CodexResult(0, issue, "reviewer-session"),
            loop_manager.CodexResult(0, "VERDICT: PASS", "reviewer-session"),
        )
        with tempfile.TemporaryDirectory() as directory:
            result, worker_levels, plan = self._run_reviews(
                directory, reviews, publish=True
            )

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(worker_levels, [1, 1, 1, 2])
        self.assertEqual(plan["initial_worker_level"], 1)
        self.assertEqual(plan["effective_worker_level"], 2)
        self.assertEqual(json.loads(plan["review_failure_history"]), {})
        level_history = json.loads(plan["review_level_history"])
        self.assertEqual(sorted(level_history["1"].values()), [3])

    @dummy_worker_sessions
    def test_reviewer_cli_failure_does_not_escalate_worker(self):
        reviews = (
            loop_manager.CodexResult(
                1,
                "configuration failed",
                "reviewer-session",
                loop_manager.AgentResultKind.PERMANENT_CONFIGURATION,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            result, worker_levels, plan = self._run_reviews(
                directory, reviews, publish=False
            )

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(worker_levels, [1])
        self.assertEqual(plan["effective_worker_level"], 1)
        self.assertEqual(json.loads(plan["review_level_history"]), {})
        self.assertEqual(json.loads(plan["review_failure_history"]), {})

    @dummy_worker_sessions
    def test_codex_and_claude_level_eight_profiles_share_validation_boundary(self):
        config = profile_config(self)
        for provider, model, effort in (
            ("codex", "test-strong", "xhigh"),
            ("claude", "claude-opus-test", "max"),
        ):
            with self.subTest(provider=provider):
                catalog = {model: frozenset({effort})}
                self.assertEqual(
                    loop_manager.provider_worker_profile(config, provider, 8, catalog),
                    (model, effort),
                )
                with mock.patch(
                    f"loop_manager.{provider}_model_efforts", return_value=catalog
                ):
                    resolve = (loop_manager.worker_profile if provider == "codex"
                               else loop_manager.claude_worker_profile)
                    self.assertEqual(resolve(config, 8), (model, effort))

                with tempfile.TemporaryDirectory() as directory:
                    with mock.patch(
                        f"loop_manager.{provider}_model_efforts",
                        return_value={model: frozenset({"medium"})},
                    ), mock.patch("loop_manager.subprocess.run") as run, mock.patch(
                        "loop_manager.subprocess.Popen"
                    ) as popen:
                        with self.assertRaisesRegex(RuntimeError, "지원하지 않습니다"):
                            getattr(loop_manager, f"run_{provider}")(
                                Path(directory) / "worktree",
                                Path(directory) / "state",
                                config,
                                "worker",
                                "prompt",
                                read_only=False,
                                level=8,
                            )
                    run.assert_not_called()
                    popen.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            for provider, model in (
                ("codex", "supported-model"),
                ("claude", "claude-haiku-custom"),
            ):
                with self.subTest(provider=provider, profile="custom,high"):
                    custom_config = Path(directory) / f"{provider}.ini"
                    custom_config.write_text(
                        f"[{provider}.models]\ncustom = {model}\n"
                        f"[{provider}.worker]\nlevel8 = custom,high\n",
                        encoding="utf-8",
                    )
                    self.assertEqual(
                        loop_manager.provider_worker_profile(
                            custom_config, provider, 8, {model: frozenset({"high"})},
                        ),
                        (model, "high"),
                    )
                    with self.assertRaisesRegex(RuntimeError, "지원하지 않습니다"):
                        loop_manager.provider_worker_profile(custom_config, provider, 8, {})
                    custom_config.write_text(
                        f"[{provider}.models]\ncustom = {model}\n"
                        f"[{provider}.worker]\nlevel7 = custom,high\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(RuntimeError, "level8 설정이 없습니다"):
                        loop_manager.provider_worker_profile(
                            custom_config, provider, 8, {model: frozenset({"high"})},
                        )
