import sqlite3
import unittest

from validation_feedback import Feedback, normalize_feedback
from validation_feedback_state import ValidationFeedbackRepository, ensure_schema


class ValidationFeedbackStateTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        ensure_schema(self.connection)
        self.repo = ValidationFeedbackRepository(self.connection)

    def observe(self, stage, raw, feedback=(), passed=False):
        self.repo.observe("repo", "G-040", "revision", stage, passed, raw, feedback)

    def pending(self, session="session", recovery=False):
        return self.repo.prepare("repo", "G-040", session, recovery=recovery)

    def test_repetition_changed_diagnostic_resolution_and_recurrence(self):
        first = Feedback("verify", "verify:A", "failure A")
        changed = Feedback("verify", "verify:A", "failure A at new line")
        other = Feedback("verify", "verify:B", "failure B")
        self.observe("verify", "raw A", (first,))
        initial = self.pending()
        self.assertEqual((initial.rows[0]["occurrence"], initial.rows[0]["revision"]), (1, 1))
        self.repo.delivered(initial.attempt_id, "session")
        self.observe("verify", "raw A again", (first,))
        repeat = self.pending()
        self.assertEqual((repeat.rows[0]["occurrence"], repeat.rows[0]["revision"]), (2, 2))
        self.assertEqual(repeat.rows[0]["diagnostic"], "")
        self.repo.delivered(repeat.attempt_id, "session")
        self.observe("verify", "raw A changed", (changed,))
        self.assertEqual(self.pending().rows[0]["diagnostic"], changed.diagnostic)
        self.observe("verify", "raw B", (other,))
        self.assertEqual([row["identity"] for row in self.pending().rows], ["verify:B"])
        self.observe("verify", "raw A returns", (first,))
        self.assertEqual(self.pending().rows[0]["occurrence"], 4)
        self.observe("verify", "all clear", passed=True)
        self.assertEqual(self.pending().rows, ())
        self.assertEqual(self.connection.execute(
            "SELECT raw_output FROM validation_results ORDER BY id"
        ).fetchall()[-1][0], "all clear")
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results"
        ).fetchone()[0], 6)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE validation_results SET raw_output='changed'")

    def test_unacknowledged_attempt_replays_same_revision_and_new_session_recovers(self):
        self.observe("review", "ISSUE_ID: b.py::f::bug\nFix B\nVERDICT: FAIL",
                     normalize_feedback("review", "ISSUE_ID: b.py::f::bug\nFix B\nVERDICT: FAIL"))
        interrupted = self.pending()
        replayed = self.pending()
        self.assertEqual(interrupted.rows, replayed.rows)
        self.repo.delivered(replayed.attempt_id, "session")
        self.assertEqual(self.pending().rows, ())
        recovered = self.pending(None, recovery=True)
        self.assertEqual(recovered.rows[0]["diagnostic"], "Fix B")
        self.repo.delivered(recovered.attempt_id, "replacement")
        self.assertEqual(self.pending("replacement").rows, ())

    def test_failed_fallback_start_retains_recovery_contract_for_started_session(self):
        self.observe("verify", "raw", (Feedback("verify", "verify:A", "fix A"),))
        fallback = self.pending(None, recovery=True)
        self.repo.bind_session(fallback.attempt_id, "new-session")
        resumed = self.pending("new-session")
        self.assertTrue(resumed.recovery)
        self.assertEqual(resumed.rows[0]["diagnostic"], "fix A")
        self.assertEqual(resumed.rows[0]["revision"], 1)
        self.repo.delivered(resumed.attempt_id, "new-session")
        self.assertFalse(self.pending("new-session").recovery)

    def test_later_observation_is_not_acked_with_earlier_invocation(self):
        self.observe("verify", "raw A", (Feedback("verify", "verify:A", "A"),))
        first = self.pending()
        self.observe("verify", "raw B", (Feedback("verify", "verify:B", "B"),))
        self.repo.delivered(first.attempt_id, "session")
        self.assertEqual([row["identity"] for row in self.pending().rows], ["verify:B"])

    def test_resolved_undelivered_issue_keeps_diagnostic_on_recurrence(self):
        issue = Feedback("verify", "verify:A", "fix A")
        self.observe("verify", "A", (issue,))
        self.pending()  # An interrupted invocation does not establish delivery.
        self.observe("verify", "clear", passed=True)
        empty = self.pending()
        self.assertEqual(empty.rows, ())
        self.repo.delivered(empty.attempt_id, "session")
        self.observe("verify", "A returns", (issue,))
        recurrence = self.pending()
        self.assertEqual(recurrence.rows[0]["diagnostic"], "fix A")
        self.assertEqual((recurrence.rows[0]["occurrence"],
                          recurrence.rows[0]["revision"]), (2, 3))
        self.repo.delivered(recurrence.attempt_id, "session")
        self.observe("verify", "A repeats", (issue,))
        self.assertEqual(self.pending().rows[0]["diagnostic"], "")

    def test_ambiguous_legacy_verify_history_is_not_guessed(self):
        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,current_revision_sha TEXT,"
            "verify_result TEXT,review_result TEXT,verify_failure_history TEXT,"
            "review_failure_history TEXT,worker_session_id TEXT)"
        )
        history = '{"A":{"occurrences":2},"B":{"occurrences":1}}'
        legacy = ("repo", "G-040", "revision", "truncated B diagnostic", None,
                  history, "{}", "session")
        self.connection.execute("INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?,?)", legacy)
        self.repo.migrate_legacy()
        self.repo.migrate_legacy()
        self.assertEqual(self.pending().rows, ())
        self.assertEqual(tuple(self.connection.execute(
            "SELECT * FROM exec_plans").fetchone()), legacy)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results").fetchone()[0], 0)
        self.observe("verify", "new B", (Feedback("verify", "verify:B", "fix B"),))
        self.assertEqual(self.pending().rows[0]["identity"], "verify:B")

    def test_legacy_migration_keeps_old_columns_and_seeds_parseable_failure(self):
        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,current_revision_sha TEXT,"
            "verify_result TEXT,review_result TEXT,verify_failure_history TEXT,"
            "review_failure_history TEXT,worker_session_id TEXT)"
        )
        history = '{"old-fingerprint":{"occurrences":2,"sample":"failure"}}'
        self.connection.execute(
            "INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?,?)",
            ("repo", "G-040", "old-revision", "legacy raw", None,
             history, "{}", "old-session"),
        )
        self.repo.migrate_legacy()
        self.repo.migrate_legacy()
        self.assertEqual(self.pending("old-session").rows[0]["identity"],
                         "verify:old-fingerprint")
        self.assertEqual(self.pending("old-session").rows[0]["occurrence"], 2)
        row = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        self.assertEqual((row["verify_result"], row["verify_failure_history"],
                          row["worker_session_id"]),
                         ("legacy raw", history, "old-session"))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results"
        ).fetchone()[0], 1)

    def test_new_observation_is_not_reimported_as_legacy_on_restart(self):
        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,current_revision_sha TEXT,"
            "verify_result TEXT,review_result TEXT,verify_failure_history TEXT,"
            "review_failure_history TEXT)"
        )
        self.connection.execute(
            "INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?)",
            ("repo", "G-040", "revision", "raw", None,
             '{"digest":{"occurrences":1,"sample":"raw"}}', "{}"),
        )
        self.observe("verify", "raw", (Feedback("verify", "verify:digest", "raw"),))
        self.repo.migrate_legacy()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results"
        ).fetchone()[0], 1)

    def test_sparse_legacy_catalog_skips_unknown_validation_columns(self):
        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,status TEXT)"
        )
        self.connection.execute(
            "INSERT INTO exec_plans VALUES ('repo','G-040','BLOCKED')"
        )

        self.repo.migrate_legacy()

        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results"
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans"
        ).fetchone()[0], 'BLOCKED')
