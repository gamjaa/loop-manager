"""Common-defect observations retain their original, unconfirmed result audit."""
import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest import mock

from loop_manager_internal.execution.common_defect_report import parse_report
from loop_manager_internal.execution.validation_feedback import Feedback
from loop_manager_internal.storage.validation_feedback_state import (
    ValidationFeedbackRepository, ensure_schema,
)
from loop_manager_internal.planning.common_defect_context import planning_context
from loop_manager_internal.planning.state_ports import _PlanningStatePorts
from tests.support.planning_case import PlanningCase, FakePorts, planning_snapshot
from loop_manager_internal.planning.planning_runtime import PlanningLifecycleCoordinator


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

    def test_planning_port_reads_only_its_project_with_original_question_and_identity(self):
        question = '\nWORKER_OUTCOME: {"kind":"needs_user_decision","question":"existing question"}'
        observation = {"invocation_id": "actual-call", "candidate_head": "actual-head",
                       "candidate_snapshot": '{"dirty":{"a.py":"digest"}}',
                       "plan_path": "docs/plans/A.md", "sources": "docs/PRODUCT.md",
                       "remote_revision": "remote", "session_id": "saved-session"}
        self.repository.record_result("repo", "G-001", "definition", "worker", "returned",
                                      message() + question, observation=observation)
        self.repository.record_result("other", "G-001", "other-definition", "worker", "returned", message())
        state = _PlanningStatePorts(None, self.connection, None, None, Path(self.directory.name))
        rows = state.common_defect_reports("repo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observation"], observation)
        self.assertIn("existing question", rows[0]["raw_output"])
        self.assertEqual(rows[0]["definition_revision"], "definition")
        prompt = planning_context(rows, Path(self.directory.name))
        exported, = (Path(self.directory.name) / "common-defect-evidence").glob("*.json")
        self.assertIn(str(exported), prompt)
        self.assertEqual(exported.stem, hashlib.sha256(exported.read_bytes()).hexdigest())
        self.assertEqual(json.loads(exported.read_text())["reports"], rows)
        self.assertEqual(planning_context(rows, Path(self.directory.name)), prompt)
        self.assertEqual(state.common_defect_reports("repo"), rows)

    def test_interrupted_export_keeps_reports_and_removes_partial_file(self):
        self.repository.record_result("repo", "G-001", "definition", "worker", "returned", message())
        reports = self.repository.common_defect_reports("repo")
        with mock.patch("loop_manager_internal.planning.common_defect_context.os.replace",
                        side_effect=OSError("interrupted snapshot publication")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                planning_context(reports, Path(self.directory.name))
        self.assertEqual(self.repository.common_defect_reports("repo"), reports)
        self.assertEqual(list((Path(self.directory.name) / "common-defect-evidence").iterdir()), [])

    def test_old_audit_adds_unknown_observation_without_changing_original_rows(self):
        with sqlite3.connect(":memory:") as legacy:
            legacy.execute("CREATE TABLE validation_results (id INTEGER PRIMARY KEY, repo TEXT, "
                           "plan_id TEXT, definition_revision TEXT, stage TEXT, classification TEXT, "
                           "raw_output TEXT, identities TEXT, created_at TEXT)")
            legacy.execute("INSERT INTO validation_results VALUES "
                           "(1,'repo','G-001','old-definition','verify','failure','old failure','[]','then')")
            before = legacy.execute("SELECT * FROM validation_results").fetchone()
            ensure_schema(legacy)
            row = legacy.execute("SELECT * FROM validation_results").fetchone()
            self.assertEqual(row[:-1], before)
            self.assertEqual(json.loads(row[-1]), {})


class CommonDefectPlanningTests(PlanningCase):
    def test_reports_are_delivered_only_with_an_existing_general_planning_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            ports = FakePorts(planning_snapshot(
                phase="PLANNING", active_sha="base", worktree="/planning",
                planning_attempt_fingerprint="input", planning_workspace_owned=1,
            ))
            rows = [{"id": 1, "plan_id": "G-001", "report": REPORT}]
            ports.state.common_defect_reports = mock.Mock(return_value=rows)
            ports.state.bind_common_defect_snapshot = mock.Mock()
            coordinator = PlanningLifecycleCoordinator(ports)
            context = replace(self.context, state_dir=Path(directory))
            run = coordinator._new_run(context)
            self.assertIsNone(coordinator._workspace._prepare_workspace(run))
            self.assertIsNone(coordinator._assessment._run_planner_turn(run))
            self.assertEqual(ports.agent.run_count, 1)
            ports.state.common_defect_reports.assert_called_once_with("/repo")
            ports.state.bind_common_defect_snapshot.assert_called_once_with("/repo", "base", [1])
            self.assertIn("공통 결함의 미확정 보고 snapshot:", ports.agent.prompts[0])
            self.assertIn("catalog", ports.agent.prompts[0])
            self.assertIsNone(coordinator._assessment._run_planner_turn(run))
            self.assertEqual(ports.agent.run_count, 1)
            ports.state.common_defect_reports.assert_called_once()
            ports.state.bind_common_defect_snapshot.assert_called_once()
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 1)

    def test_report_presence_does_not_start_planning_or_write_empty_exports(self):
        ports = FakePorts(planning_snapshot(phase="WORKING", active_sha="base"))
        ports.state.common_defect_reports = mock.Mock(return_value=[{"report": REPORT}])
        coordinator = PlanningLifecycleCoordinator(ports)
        self.assertIsNone(coordinator._assessment._run_planner_turn(coordinator._new_run(self.context)))
        ports.state.common_defect_reports.assert_not_called()
        self.assertEqual(ports.agent.run_count, 0)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(planning_context([], Path(directory)), "")
            self.assertEqual(list(Path(directory).iterdir()), [])
