from tests.support.review_escalation import WorkerEscalationFixture
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import make_catalog_root, write_execplan
from tests.support.worker import dummy_worker_sessions


class ReviewDrivenWorkerProfileEscalationTest(WorkerEscalationFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_new_execplan_stores_capped_initial_and_effective_level(self):
        cases = (
            ("low", "low", 1),
            ("low", "critical", 4),
            ("medium", "critical", 6),
            ("high", "high", 7),
            ("high", "critical", 7),
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

        self.assertEqual(stored["initial_worker_level"], 7)
        self.assertEqual(stored["effective_worker_level"], 7)
        self.assertEqual(json.loads(stored["review_level_history"]), {})

    @dummy_worker_sessions
    def test_review_state_transition_escalates_one_level_and_caps_at_eight(self):
        history: dict[str, dict[str, int]] = {}
        effective_level = 1

        for expected_level in range(2, 9):
            fingerprint = f"same-at-{effective_level}"
            first = loop_manager.evaluate_review_worker_level(
                effective_level, history, (fingerprint,)
            )
            self.assertFalse(first.escalated)
            second = loop_manager.evaluate_review_worker_level(
                effective_level, first.review_history, (fingerprint,)
            )
            self.assertTrue(second.escalated)
            self.assertEqual(second.effective_level, expected_level)
            history = second.review_history
            effective_level = second.effective_level

        capped = loop_manager.evaluate_review_worker_level(
            8, history, ("distinct-a", "distinct-b", "distinct-c")
        )
        self.assertFalse(capped.escalated)
        self.assertEqual(capped.effective_level, 8)

        distinct = loop_manager.evaluate_review_worker_level(
            4, {}, ("issue-a", "issue-b", "issue-c")
        )
        self.assertTrue(distinct.escalated)
        self.assertEqual(distinct.effective_level, 5)

    @dummy_worker_sessions
    def test_review_level_persists_with_global_history_and_blocked_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root, state_dir, connection = self._level_state(
                directory, initial_level=7
            )
            output = (
                "ISSUE_ID: app.py::same-issue::blocking\n"
                "Actionable issue\nVERDICT: FAIL"
            )
            first = loop_manager.transition_validation_failure(
                connection, str(root), root, "review", output
            )
            connection.close()

            reopened = loop_manager.connect_database(state_dir)
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

            second = loop_manager.transition_validation_failure(
                reopened, str(root), root, "review", output
            )
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
                2,
            )
            self.assertEqual(
                json.loads(escalated["review_failure_history"])[second.fingerprint][
                    "occurrences"
                ],
                2,
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
                       review_level_history, review_failure_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            reopened.close()

        self.assertEqual(reset["initial_worker_level"], 7)
        self.assertEqual(reset["effective_worker_level"], 7)
        self.assertEqual(json.loads(reset["review_level_history"]), {})
        self.assertEqual(json.loads(reset["review_failure_history"]), {})

    @dummy_worker_sessions
    def test_verify_and_invalid_review_do_not_change_escalation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _state_dir, connection = self._level_state(
                directory, initial_level=4
            )
            review = (
                "ISSUE_ID: app.py::review-only::blocking\n"
                "Actionable issue\nVERDICT: FAIL"
            )
            loop_manager.transition_validation_failure(
                connection, str(root), root, "review", review
            )
            before = connection.execute(
                """
                SELECT effective_worker_level, review_level_history
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
                SELECT effective_worker_level, review_level_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-019'
                """,
                (str(root),),
            ).fetchone()
            connection.close()

        self.assertEqual(before["effective_worker_level"], 4)
        self.assertEqual(after["effective_worker_level"], 4)
        self.assertEqual(after["review_level_history"], before["review_level_history"])

    @dummy_worker_sessions
    def test_worker_uses_escalated_level_only_after_second_matching_review(self):
        issue = (
            "ISSUE_ID: app.py::persistent-issue::blocking\n"
            "Actionable issue\nVERDICT: FAIL"
        )
        reviews = (
            loop_manager.CodexResult(0, issue, "reviewer-session"),
            loop_manager.CodexResult(0, issue, "reviewer-session"),
            loop_manager.CodexResult(0, "VERDICT: PASS", "reviewer-session"),
        )
        with tempfile.TemporaryDirectory() as directory:
            result, worker_levels, plan = self._run_reviews(
                directory, reviews, publish=True
            )

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(worker_levels, [1, 1, 2])
        self.assertEqual(plan["initial_worker_level"], 1)
        self.assertEqual(plan["effective_worker_level"], 2)
        self.assertEqual(json.loads(plan["review_failure_history"]), {})
        level_history = json.loads(plan["review_level_history"])
        self.assertEqual(sorted(level_history["1"].values()), [2])

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
        config = loop_manager.ROOT / "config/config.ini.example"
        with mock.patch(
            "loop_manager.codex_model_efforts",
            return_value={"gpt-6.1-sol": frozenset({"xhigh"})},
        ):
            self.assertEqual(
                loop_manager.worker_profile(config, 8),
                ("gpt-6.1-sol", "xhigh"),
            )
        self.assertEqual(
            loop_manager.provider_worker_profile(
                config,
                "claude",
                8,
                {"claude-opus-5": frozenset({"max"})},
            ),
            ("claude-opus-5", "max"),
        )

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-6.1-sol": frozenset({"medium"})},
            ), mock.patch("loop_manager.subprocess.run") as subprocess_run:
                with self.assertRaisesRegex(RuntimeError, "지원하지 않습니다"):
                    loop_manager.run_codex(
                        Path(directory) / "worktree",
                        Path(directory) / "state",
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=8,
                    )
            subprocess_run.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            custom_config = Path(directory) / "custom.ini"
            custom_config.write_text(
                "[codex.models]\ncustom = supported-model\n"
                "[codex.worker]\nlevel8 = custom,high\n",
                encoding="utf-8",
            )
            self.assertEqual(
                loop_manager.provider_worker_profile(
                    custom_config, "codex", 8,
                    {"supported-model": frozenset({"high"})},
                ),
                ("supported-model", "high"),
            )
            with self.assertRaisesRegex(RuntimeError, "지원하지 않습니다"):
                loop_manager.provider_worker_profile(custom_config, "codex", 8, {})

        invalid_profiles = (
            (
                "claude-alias",
                "[claude.models]\nstrong = claude-opus-5\nfast = claude-haiku\n"
                "[claude.worker]\nlevel8 = fast,max\n",
                "claude",
                {"claude-haiku": frozenset({"max"})},
                "strong,max",
            ),
            (
                "claude-family",
                "[claude.models]\nstrong = claude-fable-5\n"
                "[claude.worker]\nlevel8 = strong,max\n",
                "claude",
                {"claude-fable-5": frozenset({"max"})},
                "claude-opus-5",
            ),
            (
                "unsupported-effort",
                "[codex.models]\nfrontier = gpt-6-astra\n"
                "[codex.worker]\nlevel8 = frontier,low\n",
                "codex",
                {"gpt-6-astra": frozenset({"medium"})},
                "지원하지 않습니다",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, contents, provider, catalog, message in invalid_profiles:
                with self.subTest(name=name):
                    profile_config = Path(directory) / f"{name}.ini"
                    profile_config.write_text(contents, encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, message):
                        loop_manager.provider_worker_profile(
                            profile_config, provider, 8, catalog
                        )
