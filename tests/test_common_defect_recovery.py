"""Common-defect observations retain their original, unconfirmed result audit."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from loop_manager_internal.execution.common_defect_report import parse_report
from loop_manager_internal.execution.validation_feedback import Feedback
from loop_manager_internal.storage.validation_feedback_state import (
    ValidationFeedbackRepository, ensure_schema,
)


REPORT = {
    "problem": "shared runner failure",
    "scope": "selected implementation does not own the runner",
    "evidence": "traceback in shared runner; cause not yet confirmed",
    "locations": [{"path": "scripts/runner.py", "line": 12}],
}


def message(report=REPORT):
    return "COMMON_DEFECT_REPORT: " + json.dumps(report)


class CommonDefectReportTests(unittest.TestCase):
    def test_optional_report_is_bounded_and_does_not_accept_resolution_claims(self):
        self.assertIsNone(parse_report("ordinary failure"))
        self.assertEqual(parse_report(message()), REPORT)
        invalid = [
            message() + "\n" + message(),
            'COMMON_DEFECT_REPORT: {"problem":"a","problem":"b"}',
            message({**REPORT, "confirmed": True}),
            message({**REPORT, "evidence": " "}),
            message({**REPORT, "problem": "x" * 2001}),
            message({**REPORT, **{key: "결" * 2000 for key in ("problem", "scope", "evidence")}}),
            message({**REPORT, "scope": "bad\x00scope"}),
            message({**REPORT, "locations": []}),
            message({**REPORT, "locations": REPORT["locations"] * 9}),
        ]
        for path in ("/tmp/runner.py", "../runner.py", "a/../b", "a//b", "a/./b",
                     "C:/runner.py", "a\\b", "a\nb"):
            invalid.append(message({**REPORT, "locations": [{"path": path, "line": 1}]}))
        for line in (True, 0, -1, "12", 2147483648):
            invalid.append(message({**REPORT, "locations": [{"path": "a.py", "line": line}]}))
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_report(raw)

    def test_report_uses_shared_formatting_and_secret_redaction(self):
        raw = message({**REPORT, "evidence": "API_KEY=secret-value"})
        parsed = parse_report(raw.replace("COMMON_DEFECT_REPORT:", "- **common_defect_report:**"))
        self.assertEqual(parsed["evidence"], "API_KEY=<REDACTED>")
        self.assertEqual(parse_report("COMMON_DEFECT_REPORT:\n```json\n"
                                      + json.dumps(REPORT) + "\n```"), REPORT)


class CommonDefectAuditTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "audit.sqlite"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        ensure_schema(self.connection)
        self.repository = ValidationFeedbackRepository(self.connection)

    def test_reports_survive_restart_without_merging_tasks_or_replacing_feedback(self):
        for plan, stage in (("G-001", "verify"), ("G-002", "review")):
            self.repository.observe("repo", plan, "definition-" + plan, stage, False,
                                    message(), (Feedback(stage, "same-symptom", "original failure"),))
        self.repository.observe("repo", "G-003", "independent", "verify", True, "PASS")
        with sqlite3.connect(self.path) as reopened:
            ensure_schema(reopened)
            rows = reopened.execute(
                "SELECT v.repo,v.plan_id,v.definition_revision,v.stage,v.classification,c.report "
                "FROM common_defect_reports c JOIN validation_results v ON v.id=c.result_id "
                "ORDER BY v.id"
            ).fetchall()
            self.assertEqual([row[:5] for row in rows], [
                ("repo", "G-001", "definition-G-001", "verify", "failure"),
                ("repo", "G-002", "definition-G-002", "review", "failure"),
            ])
            self.assertEqual([json.loads(row[5]) for row in rows], [REPORT, REPORT])
            self.assertEqual(reopened.execute(
                "SELECT COUNT(*) FROM validation_feedback_current WHERE active=1"
            ).fetchone()[0], 2)
            self.assertEqual(reopened.execute(
                "SELECT classification FROM validation_results WHERE plan_id='G-003'"
            ).fetchone()[0], "success")
        self.repository.observe("repo", "G-001", "new-definition", "verify", True, "PASS")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM common_defect_reports").fetchone()[0], 2)

    def test_rejected_report_preserves_failure_and_cannot_become_an_approval(self):
        raw = message({**REPORT, "confirmed": True, "evidence": "API_KEY=secret-value"})
        self.repository.record_result("repo", "G-001", "revision", "worker", "invalid", raw)
        result = self.connection.execute("SELECT * FROM validation_results").fetchone()
        report = self.connection.execute("SELECT * FROM common_defect_reports").fetchone()
        self.assertEqual(result["classification"], "invalid")
        self.assertNotIn("secret-value", result["raw_output"])
        self.assertEqual(report["result_id"], result["id"])
        self.assertIsNone(report["report"])
        self.assertEqual(report["error"], "invalid common defect report fields")
        for table in ("validation_results", "common_defect_reports"):
            for command in (f"DELETE FROM {table}", f"UPDATE {table} SET " +
                            ("raw_output='changed'" if table == "validation_results" else "error='changed'")):
                with self.subTest(command=command), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    self.connection.execute(command)
                self.connection.rollback()

    def test_report_and_original_result_rollback_together(self):
        self.connection.execute(
            "CREATE TRIGGER reject_report BEFORE INSERT ON common_defect_reports "
            "BEGIN SELECT RAISE(ABORT, 'interrupted report write'); END"
        )
        self.connection.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "interrupted"):
            self.repository.observe("repo", "G-001", "revision", "review", False, message())
        for table in ("validation_results", "common_defect_reports", "validation_feedback_events"):
            self.assertEqual(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
