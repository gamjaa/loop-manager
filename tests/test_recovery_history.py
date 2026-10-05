from contextlib import closing
from tests.support.recovery_boundary import RecoveryBoundary
from tests.support.recovery import BoundedRecoveryFixture
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.recovery import scoped_blocking
from tests.support.worker import worker_result
from tests.support.publication import publishing_branch_pr_lookup
from tests.support.worker import dummy_worker_sessions


class RecoveryHistoryTest(BoundedRecoveryFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_review_occurrence_history_does_not_create_final_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root, connection = self._failure_state(directory)
            self.addCleanup(connection.close)
            reviews = [
                ('ISSUE_ID: app.py::same-issue::blocking\n'
                 f'Changed diagnostic {index}\n'
                 + ('ISSUE_ID: app.py::extra-issue::blocking\n' if index == 3 else '')
                 + 'VERDICT: FAIL')
                for index in range(5)
            ]
            decisions = [
                loop_manager.transition_validation_failure(
                    connection, str(root), root, 'review', review,
                ) for review in reviews
            ]
            row = connection.execute(
                "SELECT status, review_failure_history FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-013'", (str(root),),
            ).fetchone()
            self.assertEqual([item.occurrences for item in decisions], [1, 2, 3, 4, 5])
            self.assertTrue(all(not item.blocked for item in decisions))
            self.assertEqual(row['status'], 'IMPLEMENTING')
            self.assertEqual(json.loads(row['review_failure_history'])[
                decisions[-1].fingerprint]['occurrences'], 5)
            self.assertIsNone(scoped_blocking.plan_block(
                connection, str(root), 'G-013',
            ))


    @dummy_worker_sessions
    def test_stage_histories_survive_cycles_and_block_only_fourth_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            root, connection = self._failure_state(directory)
            outputs = ["failure A", "failure B", "failure C", "failure D"]
            decisions = [
                loop_manager.transition_validation_failure(
                    connection, str(root), root, "verify", output
                )
                for output in outputs
            ]
            for _ in range(2):
                decisions.append(
                    loop_manager.transition_validation_failure(
                        connection, str(root), root, "verify", "failure A"
                    )
                )
            review = loop_manager.transition_validation_failure(
                connection, str(root), root, "review", "ISSUE_ID: app.py::failure-a::blocking\nfailure A\nVERDICT: FAIL"
            )
            final = loop_manager.transition_validation_failure(
                connection, str(root), root, "verify", "failure A"
            )
            row = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            ).fetchone()
            verify_history = json.loads(row["verify_failure_history"])
            review_history = json.loads(row["review_failure_history"])

        self.assertTrue(all(not decision.blocked for decision in decisions))
        self.assertFalse(review.blocked)
        self.assertTrue(final.blocked)
        self.assertEqual(final.occurrences, 4)
        self.assertEqual(row["status"], "PLAN_BLOCKED")
        self.assertIn(final.fingerprint, row["last_error"])
        self.assertEqual(verify_history[final.fingerprint]["occurrences"], 4)
        self.assertEqual(review_history[review.fingerprint]["occurrences"], 1)
        self.assertEqual(
            sorted(record["occurrences"] for record in verify_history.values()),
            [1, 1, 1, 4],
        )

    @dummy_worker_sessions
    def test_history_persists_and_stage_success_or_blocked_requeue_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.row_factory = sqlite3.Row
            root, connection = self._failure_state(
                str(Path(directory) / "catalog"), connection=database
            )
            verify = loop_manager.transition_validation_failure(
                connection, str(root), root, "verify", "failure A"
            )
            review = loop_manager.transition_validation_failure(
                connection, str(root), root, "review", "ISSUE_ID: app.py::issue-r::blocking\nissue R\nVERDICT: FAIL"
            )
            connection.close()

            reopened = sqlite3.connect(state_dir / "loop-manager.db")
            reopened.row_factory = sqlite3.Row
            persisted = reopened.execute(
                "SELECT verify_failure_history, review_failure_history "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            ).fetchone()
            self.assertEqual(
                json.loads(persisted["verify_failure_history"])[verify.fingerprint][
                    "occurrences"
                ],
                1,
            )
            self.assertEqual(
                json.loads(persisted["review_failure_history"])[review.fingerprint][
                    "occurrences"
                ],
                1,
            )

            loop_manager.clear_validation_failure_history(
                reopened, str(root), "verify"
            )
            stage_cleared = reopened.execute(
                "SELECT verify_failure_history, review_failure_history "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            ).fetchone()
            self.assertEqual(json.loads(stage_cleared["verify_failure_history"]), {})
            self.assertNotEqual(json.loads(stage_cleared["review_failure_history"]), {})

            reopened.execute(
                "UPDATE exec_plans SET status = 'BLOCKED' "
                "WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            )
            reopened.execute(
                "UPDATE projects SET phase = 'BLOCKED' WHERE repo = ?",
                (str(root),),
            )
            reopened.commit()
            loop_manager._requeue_blocked_execplan(
                reopened, str(root), "G-013"
            )
            requeued = reopened.execute(
                "SELECT verify_failure_history, review_failure_history, "
                "worker_session_id, reviewer_session_id "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-013'",
                (str(root),),
            ).fetchone()
            reopened.close()

        self.assertEqual(json.loads(requeued["verify_failure_history"]), {})
        self.assertEqual(json.loads(requeued["review_failure_history"]), {})
        self.assertIsNone(requeued["worker_session_id"])
        self.assertIsNone(requeued["reviewer_session_id"])

    @dummy_worker_sessions
    def test_review_protocol_errors_do_not_retry_worker_or_consume_history(self):
        invalid_reviews = (
            "blocking issue without an identifier\nVERDICT: FAIL",
            "ISSUE_ID: app.py:42 prose\nblocking issue\nVERDICT: FAIL",
            "ISSUE_ID: app.py::pass-issue::blocking\nVERDICT: PASS",
            "ISSUE_ID: app.py::missing-verdict::blocking\nblocking issue",
            "VERDICT: PASS\nVERDICT: FAIL",
            "ISSUE_ID: app.py::unknown-verdict::blocking\nVERDICT: MAYBE",
            "VERDICT: PASS\ntrailing explanation",
        )
        with RecoveryBoundary() as boundary:
            for variant, review in enumerate(invalid_reviews):
                with self.subTest(review=review):
                    data = self._run_review_sequence(
                        boundary.directory,
                        (review,) * loop_manager.MAX_REVIEWER_PROTOCOL_ATTEMPTS,
                        publish=False, boundary=boundary,
                    )

                result = data["result"]
                project = data["project"]
                plan = data["plan"]
                self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(
                    data["roles"],
                    (["worker"] if variant == 0 else [])
                    + ["reviewer"] * loop_manager.MAX_REVIEWER_PROTOCOL_ATTEMPTS,
                )
                self.assertEqual(
                    data["reviewer_sessions"],
                    [None]
                    + ["reviewer-session"]
                    * (loop_manager.MAX_REVIEWER_PROTOCOL_ATTEMPTS - 1),
                )
                self.assertEqual(data["verify_calls"], 1 if variant == 0 else 0)
                self.assertEqual(data["transitions"], [])
                self.assertIsNone(project["phase"])
                self.assertIn("Reviewer 결과 프로토콜 오류", plan["last_error"])
                self.assertIn("프로토콜 교정 3회 실패", plan["last_error"])
                self.assertEqual(plan["review_result"], review)
                self.assertEqual(plan["status"], "PLAN_BLOCKED")
                self.assertEqual(json.loads(plan["review_failure_history"]), {})
                self.assertEqual(json.loads(plan["review_level_history"]), {})
                self.assertEqual(plan["effective_worker_level"], 1)
                for prompt in data["reviewer_prompts"][1:]:
                    self.assertIn("직전 Reviewer 결과가 아래 프로토콜 검증에 실패", prompt)
                    self.assertIn(review, prompt)

    @dummy_worker_sessions
    def test_review_failure_then_verify_failure_keeps_stage_counts_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._active_worker_project(directory)
            roles = []
            worker_sessions = []
            reviewer_sessions = []
            review_messages = iter(
                ("ISSUE_ID: app.py::issue-a::blocking\nblocking issue A\nVERDICT: FAIL", "VERDICT: PASS")
            )
            worker_version = 1
            transitions = []
            original_transition = loop_manager.transition_validation_failure

            def record_transition(*args, **kwargs):
                decision = original_transition(*args, **kwargs)
                transitions.append(
                    (decision.stage, decision.fingerprint, decision.occurrences)
                )
                return decision

            def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
                nonlocal worker_version
                roles.append(role_name)
                if role_name == "worker":
                    worker_sessions.append(kwargs["session_id"])
                    worker_version += 1
                    (worktree / "src/app.py").write_text(
                        f"VALUE = {worker_version}\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(f"Record review recovery {worker_version}"),
                        "worker-session",
                    )
                reviewer_sessions.append(kwargs["session_id"])
                return loop_manager.CodexResult(
                    0, next(review_messages), "reviewer-session"
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch",
                side_effect=publishing_branch_pr_lookup(
                    connection, "https://example.test/pr/13"
                ),
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ), mock.patch(
                "loop_manager.verify",
                side_effect=[
                    (True, "verify: PASS"),
                    (False, "verify issue V"),
                    (True, "verify: PASS"),
                ],
            ), mock.patch(
                "loop_manager.transition_validation_failure",
                side_effect=record_transition,
            ):
                result = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            row = connection.execute(
                "SELECT phase, reviewer_session_id FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, reviewer_session_id, verify_failure_history, "
                "review_failure_history "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(
            roles,
            ["worker", "reviewer", "worker", "worker", "reviewer"],
        )
        self.assertEqual(
            worker_sessions, [None, "worker-session", "worker-session"]
        )
        self.assertEqual(
            reviewer_sessions, [None, "reviewer-session"]
        )
        self.assertEqual(
            [(stage, occurrences) for stage, _fingerprint, occurrences in transitions],
            [("review", 1), ("verify", 1)],
        )
        self.assertIsNone(row["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(plan["reviewer_session_id"], "reviewer-session")
        self.assertEqual(json.loads(plan["verify_failure_history"]), {})
        self.assertEqual(json.loads(plan["review_failure_history"]), {})

    @dummy_worker_sessions
    def test_existing_database_migrates_recovery_fields_without_losing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            legacy_projects = loop_manager.SCHEMA.replace(
                "    reviewer_session_id TEXT,\n", ""
            ).replace("    review_result TEXT,\n", "")
            for line in (
                "    pr_conflict_recovery INTEGER NOT NULL DEFAULT 0,\n",
                "    pr_conflict_head_oid TEXT,\n",
                "    pr_conflict_base_oid TEXT,\n",
            ):
                legacy_projects = legacy_projects.replace(line, "")
                "    pr_conflict_recovery INTEGER NOT NULL DEFAULT 0,\n",
                "    pr_conflict_head_oid TEXT,\n",
                "    pr_conflict_base_oid TEXT,\n",
            legacy_execplans = loop_manager.EXEC_PLANS_SCHEMA
            for line in (
                "    reviewer_session_id TEXT,\n",
                "    review_result TEXT,\n",
                "    verify_failure_history TEXT NOT NULL DEFAULT '{}',\n",
                "    review_failure_history TEXT NOT NULL DEFAULT '{}',\n",
                "    initial_worker_level INTEGER,\n",
                "    effective_worker_level INTEGER,\n",
                "    review_level_history TEXT NOT NULL DEFAULT '{}',\n",
                "    blocked_review_fingerprint TEXT,\n",
            ):
                legacy_execplans = legacy_execplans.replace(line, "")
            database.execute(legacy_projects)
            database.execute(legacy_execplans)
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) "
                "VALUES ('repo', 'main', 'VERIFYING')"
            )
            database.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, retry_count
                ) VALUES ('repo', 'G-013', 'plan.md', 'Plan', '[]', '[]',
                          'critical', 'hash', 'VERIFYING', 2)
                """
            )
            database.commit()
            database.close()

            with closing(loop_manager.connect_database(state_dir)) as migrated, migrated:
                project_columns = {
                    row[1] for row in migrated.execute("PRAGMA table_info(projects)")
                }
                plan_columns = {
                    row[1] for row in migrated.execute("PRAGMA table_info(exec_plans)")
                }
                row = migrated.execute(
                    "SELECT retry_count, verify_failure_history, "
                    "review_failure_history, initial_worker_level, "
                    "effective_worker_level, review_level_history FROM exec_plans"
                ).fetchone()

        self.assertTrue(
            {
                "reviewer_session_id",
                "review_result",
                "pr_conflict_recovery",
                "pr_conflict_head_oid",
                "pr_conflict_base_oid",
            } <= project_columns
        )
        self.assertTrue(
            {
                "reviewer_session_id",
                "review_result",
                "verify_failure_history",
                "review_failure_history",
                "pr_conflict_recovery",
                "pr_conflict_head_oid",
                "pr_conflict_base_oid",
                "initial_worker_level",
                "effective_worker_level",
                "review_level_history",
                "blocked_review_fingerprint",
            }
            <= plan_columns
        )
        self.assertEqual(row["retry_count"], 2)
        self.assertEqual(json.loads(row["verify_failure_history"]), {})
        self.assertEqual(json.loads(row["review_failure_history"]), {})
        self.assertIsNone(row["initial_worker_level"])
        self.assertIsNone(row["effective_worker_level"])
        self.assertEqual(json.loads(row["review_level_history"]), {})
