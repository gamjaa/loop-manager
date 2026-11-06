import json
import sqlite3
import unittest

from loop_manager_internal.execution.validation_feedback import Feedback, normalize_feedback
from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository, ensure_schema


class ValidationFeedbackStateTests(unittest.TestCase):
    def test_delivered_failures_remain_reminders_until_new_stage_observation(self):
        failure = Feedback('verify', 'verify:one', 'FAILED: test_first\nTraceback: detail')
        self.observe('verify', 'failed', (failure,))
        first = self.pending()
        self.assertEqual(first.reminders, ())
        self.repo.delivered(first.attempt_id, 'session')
        before = tuple(self.connection.execute(
            'SELECT occurrence,revision,sequence,active FROM validation_feedback_current').fetchone())
        resumed = self.pending()
        self.assertEqual(resumed.rows, ())
        self.assertEqual([row['identity'] for row in resumed.reminders], ['verify:one'])
        self.repo.delivered(resumed.attempt_id, 'session')
        self.assertEqual(tuple(self.connection.execute(
            'SELECT occurrence,revision,sequence,active FROM validation_feedback_current').fetchone()), before)
        self.assertEqual(self.pending(recovery=True).reminders, ())
        self.assertEqual(self.pending('other-session').reminders, ())
        self.observe('verify', 'passed', passed=True)
        self.assertEqual(self.pending().reminders, ())
        self.assertEqual(self.pending().rows, ())

    def test_delivery_cannot_be_acknowledged_by_another_session(self):
        attempt = self.repo.prepare('repo', 'G-040', 'first', context={'task': 'plan'})
        for action in (self.repo.bind_session, self.repo.delivered):
            with self.assertRaisesRegex(ValueError, 'session identity'):
                action(attempt.attempt_id, 'other')
        self.assertIsNone(self.connection.execute('SELECT * FROM validation_delivery_cursors').fetchone())
        self.repo.delivered(attempt.attempt_id, 'first')

    def test_context_acknowledges_only_the_frozen_attempt_and_actual_session(self):
        context = {'task': 'selected plan', 'archives': '[R-001]', 'error': 'first error'}
        first = self.repo.prepare('repo', 'G-040', None, context=context)
        self.assertTrue(first.initial)
        self.assertEqual(first.context, context)
        self.repo.bind_session(first.attempt_id, 'session')
        # Starting a session alone cannot acknowledge its prompt.
        interrupted = self.repo.prepare('repo', 'G-040', 'session', context=context)
        self.assertEqual(interrupted.context, context)
        latest = {**context, 'archives': '[R-002]', 'error': 'new error'}
        during_call = self.repo.prepare('repo', 'G-040', 'session', context=latest)
        self.repo.delivered(first.attempt_id, 'session')
        pending = self.repo.prepare('repo', 'G-040', 'session', context=latest)
        self.assertFalse(pending.initial)
        self.assertEqual(pending.context, {'archives': '[R-002]', 'error': 'new error'})
        self.repo.delivered(during_call.attempt_id, 'session')
        self.repo.delivered(interrupted.attempt_id, 'session')
        self.assertEqual(self.repo.prepare('repo', 'G-040', 'session', context=latest).context, {})
        recovered = self.repo.prepare('repo', 'G-040', None, recovery=True, context=latest)
        self.repo.bind_session(recovered.attempt_id, 'replacement')
        self.assertEqual(recovered.context, latest)
        self.assertTrue(recovered.initial)
        self.repo.delivered(recovered.attempt_id, 'replacement')
        self.assertEqual(self.repo.prepare('repo', 'G-040', 'replacement', context=latest).context, {})

    def test_context_delivery_write_failure_keeps_both_channels_pending(self):
        self.observe('verify', 'raw', (Feedback('verify', 'verify:one', 'failure'),))
        attempt = self.repo.prepare('repo', 'G-040', 'session', context={'task': 'plan'})
        self.connection.execute("CREATE TRIGGER fail_delivery BEFORE UPDATE OF status ON validation_delivery_attempts "
                                "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        self.connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'disk full'):
            self.repo.delivered(attempt.attempt_id, 'session')
        self.assertIsNone(self.connection.execute('SELECT * FROM validation_delivery_cursors').fetchone())
        pending = self.repo.prepare('repo', 'G-040', 'session', context={'task': 'plan'})
        self.assertEqual(pending.context, {'task': 'plan'})
        self.assertEqual(len(pending.rows), 1)

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

    def test_duplicate_review_ids_increment_once_per_observation(self):
        raw = ("ISSUE_ID: File.py::Func::Bug\nFix A\n"
               "ISSUE_ID: file.py::func::bug\nFix B\nVERDICT: FAIL")
        for occurrence in (1, 2):
            self.observe("review", raw, normalize_feedback("review", raw))
            delivery = self.pending()
            self.assertEqual(len(delivery.rows), 1)
            self.assertEqual((delivery.rows[0]["occurrence"],
                              delivery.rows[0]["revision"]), (occurrence, occurrence))
            self.repo.delivered(delivery.attempt_id, "session")
        audit = self.connection.execute(
            "SELECT raw_output,identities FROM validation_results ORDER BY id"
        ).fetchall()
        self.assertEqual([row["raw_output"] for row in audit], [raw, raw])
        self.assertEqual([json.loads(row["identities"]) for row in audit],
                         [["review:file.py::func::bug"]] * 2)

    def test_legacy_review_casefold_preserves_occurrence(self):
        from loop_manager import validation_failure_fingerprints

        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,current_revision_sha TEXT,"
            "verify_result TEXT,review_result TEXT,verify_failure_history TEXT,"
            "review_failure_history TEXT,worker_session_id TEXT)"
        )
        raw = ("ISSUE_ID: File.py::Func::Bug\nFix A\n"
               "ISSUE_ID: file.py::func::bug\nFix A\nVERDICT: FAIL")
        fingerprint, _ = validation_failure_fingerprints("review", raw)[0]
        history = json.dumps({fingerprint: {"occurrences": 4}})
        legacy = ("repo", "G-040", "revision", None, raw, "{}", history, "session")
        self.connection.execute("INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?,?)", legacy)
        self.repo.migrate_legacy()
        self.repo.migrate_legacy()
        rows = self.pending().rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["identity"], "review:file.py::func::bug")
        self.assertEqual(rows[0]["occurrence"], 4)
        self.assertEqual(tuple(self.connection.execute(
            "SELECT * FROM exec_plans").fetchone()), legacy)
        self.observe("review", raw, normalize_feedback("review", raw))
        self.assertEqual(self.pending().rows[0]["occurrence"], 5)

    def test_malformed_later_legacy_stage_rolls_back_audit_and_can_retry(self):
        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,current_revision_sha TEXT,"
            "verify_result TEXT,review_result TEXT,verify_failure_history TEXT,"
            "review_failure_history TEXT)"
        )
        self.connection.execute(
            "INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?)",
            ("repo", "G-040", "revision", "verify failure", "review failure",
             '{"fingerprint":{"occurrences":2}}', "{malformed"),
        )
        self.repo.migrate_legacy()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results"
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_migrations"
        ).fetchone()[0], 0)
        self.connection.execute(
            "UPDATE exec_plans SET review_failure_history = '{}'"
        )
        self.repo.migrate_legacy()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results"
        ).fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_migrations"
        ).fetchone()[0], 1)

        self.connection.execute(
            "INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?)",
            ("repo", "G-041", "revision", "verify failure", "review failure",
             '{"fingerprint":{"occurrences":2}}',
             '{"historical":{"occurrences":"invalid"}}'),
        )
        self.repo.migrate_legacy()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results WHERE plan_id = 'G-041'"
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_migrations WHERE plan_id = 'G-041'"
        ).fetchone()[0], 0)
        self.connection.execute(
            "UPDATE exec_plans SET review_failure_history = '{}' WHERE plan_id = 'G-041'"
        )
        self.repo.migrate_legacy()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results WHERE plan_id = 'G-041'"
        ).fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_migrations WHERE plan_id = 'G-041'"
        ).fetchone()[0], 1)

    def test_invalid_legacy_occurrences_remain_retryable(self):
        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,current_revision_sha TEXT,"
            "verify_result TEXT,review_result TEXT,verify_failure_history TEXT,"
            "review_failure_history TEXT)"
        )
        for index, value in enumerate((1.5, float("inf"), float("nan"), True,
                                       0, -1, 2**63, "2", None)):
            with self.subTest(value=value):
                plan = f"G-{index}"
                self.connection.execute(
                    "INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?)",
                    ("repo", plan, "revision", "failure", None,
                     json.dumps({"fingerprint": {"occurrences": value}}), "{}"),
                )
                self.repo.migrate_legacy()
                for table in ("validation_results", "validation_feedback_events",
                              "validation_feedback_current", "validation_feedback_migrations"):
                    self.assertEqual(self.connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE plan_id=?", (plan,)
                    ).fetchone()[0], 0)
                self.connection.execute(
                    "UPDATE exec_plans SET verify_failure_history=? WHERE plan_id=?",
                    ('{"fingerprint":{"occurrences":2}}', plan),
                )
                self.repo.migrate_legacy()
                self.assertEqual(self.connection.execute(
                    "SELECT occurrence FROM validation_feedback_current WHERE plan_id=?", (plan,)
                ).fetchone()[0], 2)

    def test_prior_partial_legacy_migration_recovers_without_duplicate_audit(self):
        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,current_revision_sha TEXT,"
            "verify_result TEXT,review_result TEXT,verify_failure_history TEXT,"
            "review_failure_history TEXT)"
        )
        raw = "ISSUE_ID: file::function::bug\nFix it\nVERDICT: FAIL"
        self.connection.execute(
            "INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?)",
            ("repo", "G-040", "revision", "verify failure", raw,
             '{"fingerprint":{"occurrences":2}}', "{malformed"),
        )
        feedback = (Feedback("verify", "verify:fingerprint", "verify failure"),)
        result_id = self.repo._result("repo", "G-040", "revision", "verify",
                                      "legacy_failure", "verify failure", feedback)
        self.repo._event(result_id, "repo", "G-040", "verify", feedback[0].identity,
                         2, 1, feedback[0].diagnostic, True)
        self.repo._mark_migrated("repo", "G-040")
        audit = tuple(self.connection.execute("SELECT * FROM validation_results").fetchone())
        self.repo.migrate_legacy()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_migrations").fetchone()[0], 0)
        self.assertEqual(tuple(self.connection.execute(
            "SELECT * FROM validation_results").fetchone()), audit)
        self.connection.execute(
            "UPDATE exec_plans SET review_failure_history=?", ('{"review":{"occurrences":1}}',))
        self.repo.migrate_legacy()
        self.repo.migrate_legacy()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_results").fetchone()[0], 2)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_events").fetchone()[0], 2)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_migrations").fetchone()[0], 1)
        self.assertEqual(tuple(self.connection.execute(
            "SELECT * FROM validation_results WHERE id=?", (result_id,)).fetchone()), audit)
        self.assertEqual({row["stage"] for row in self.pending().rows}, {"verify", "review"})

    def test_prior_legacy_result_with_missing_events_recovers_on_retry(self):
        from loop_manager import validation_failure_fingerprints

        self.connection.execute(
            "CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,current_revision_sha TEXT,"
            "verify_result TEXT,review_result TEXT,verify_failure_history TEXT,"
            "review_failure_history TEXT)"
        )
        raw = "ISSUE_ID: file::one::bug\nFix one\nISSUE_ID: file::two::bug\nFix two\nVERDICT: FAIL"
        feedback = normalize_feedback("review", raw)
        fingerprints = validation_failure_fingerprints("review", raw)
        for saved_events in (0, 1):
            with self.subTest(saved_events=saved_events):
                plan = f"G-{saved_events}"
                history = {key: {"occurrences": 3} for key, _ in fingerprints}
                history[fingerprints[-1][0]]["occurrences"] = "invalid"
                self.connection.execute(
                    "INSERT INTO exec_plans VALUES (?,?,?,?,?,?,?)",
                    ("repo", plan, "revision", None, raw, "{}", json.dumps(history)),
                )
                result_id = self.repo._result("repo", plan, "revision", "review",
                                              "legacy_failure", raw, feedback)
                for item in feedback[:saved_events]:
                    self.repo._event(result_id, "repo", plan, "review", item.identity,
                                     3, 1, item.diagnostic, True)
                self.repo._mark_migrated("repo", plan)
                audit = tuple(self.connection.execute(
                    "SELECT * FROM validation_results WHERE id=?", (result_id,)).fetchone())
                old_events = [tuple(row) for row in self.connection.execute(
                    "SELECT * FROM validation_feedback_events WHERE result_id=?", (result_id,))]
                self.repo.migrate_legacy()
                self.assertIsNone(self.connection.execute(
                    "SELECT 1 FROM validation_feedback_migrations WHERE plan_id=?", (plan,)).fetchone())
                self.assertEqual(self.connection.execute(
                    "SELECT COUNT(*) FROM validation_feedback_events WHERE result_id=?",
                    (result_id,)).fetchone()[0], saved_events)
                history[fingerprints[-1][0]]["occurrences"] = 3
                self.connection.execute(
                    "UPDATE exec_plans SET review_failure_history=? WHERE plan_id=?",
                    (json.dumps(history), plan))
                self.repo.migrate_legacy()
                self.repo.migrate_legacy()
                self.assertEqual(tuple(self.connection.execute(
                    "SELECT * FROM validation_results WHERE id=?", (result_id,)).fetchone()), audit)
                self.assertEqual(self.connection.execute(
                    "SELECT COUNT(*) FROM validation_results WHERE plan_id=?", (plan,)).fetchone()[0], 1)
                events = [tuple(row) for row in self.connection.execute(
                    "SELECT * FROM validation_feedback_events WHERE result_id=? ORDER BY sequence",
                    (result_id,))]
                self.assertEqual(len(events), 2)
                self.assertEqual(events[:saved_events], old_events)
                self.assertIsNotNone(self.connection.execute(
                    "SELECT 1 FROM validation_feedback_migrations WHERE plan_id=?", (plan,)).fetchone())
                delivery = self.repo.prepare("repo", plan, "session")
                self.assertEqual({row["identity"] for row in delivery.rows},
                                 {item.identity for item in feedback})
                self.assertEqual([row["occurrence"] for row in delivery.rows], [3, 3])

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
        # A Worker observation does not supersede an older verify failure.
        self.repo.record_result("repo", "G-040", "new-revision", "worker", "returned", "Worker report")
        self.repo.migrate_legacy()
        self.repo.migrate_legacy()
        self.assertEqual(self.pending("old-session").rows[0]["identity"],
                         "verify:old-fingerprint")
        self.assertEqual(self.pending("old-session").rows[0]["occurrence"], 2)
        row = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        self.assertEqual((row["verify_result"], row["verify_failure_history"],
                          row["worker_session_id"]),
                         ("legacy raw", history, "old-session"))
        self.assertEqual([tuple(row) for row in self.connection.execute(
            "SELECT stage,classification,raw_output FROM validation_results ORDER BY id"
        )], [('worker', 'returned', 'Worker report'), ('verify', 'legacy_failure', 'legacy raw')])

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
