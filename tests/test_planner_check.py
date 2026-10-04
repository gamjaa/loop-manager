"""Direct read-only Planner check contracts."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.agents import agent_profiles
from loop_manager_internal.agents.agent_prompts import planner_check_prompt
from loop_manager_internal.storage import planner_check_state
from loop_manager_internal.planning.agent_ports import run_scoped_planner_turn
from loop_manager_internal.planning.planner_check import parse_planner_check_result
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, PlanningResultInvalid
from tests.support.planning_case import PlanningCase


class PlannerCheckContractTest(unittest.TestCase):
    def test_database_schema_registers_check_ledger(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)

        loop_manager._ensure_execplans_schema(connection)

        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'",
            )
        }
        self.assertIn("planner_checks", tables)
        self.assertIn("planner_check_history", tables)

    def test_durable_check_stage_and_revision_independent_target(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        target = planner_check_state.target_fingerprint(
            MutableReadySnapshot("repo", "first-sha", (), "first-token")
        )
        self.assertEqual(target, planner_check_state.target_fingerprint(
            MutableReadySnapshot("repo", "second-sha", (), "second-token")
        ))
        planner_check_state.begin(
            connection, "repo", input_fingerprint="input-a",
            specification_fingerprint="spec", target_fingerprint=target,
            base_sha="first-sha", comparison_sha="baseline",
        )
        self.assertTrue(planner_check_state.advance(
            connection, "repo", "pending", "result", result="decision",
            session_id="check-session",
        ))
        self.assertFalse(planner_check_state.advance(
            connection, "repo", "pending", "accepted",
        ))
        self.assertFalse(planner_check_state.advance(
            connection, "repo", "result", "accepted", expected_input="other-input",
        ))
        self.assertEqual(planner_check_state.get(connection, "repo")["stage"], "result")
        planner_check_state.begin(
            connection, "repo", input_fingerprint="input-b",
            specification_fingerprint="spec", target_fingerprint=target,
            base_sha="second-sha", comparison_sha="first-sha",
        )
        old = connection.execute(
            "SELECT stage, result, session_id FROM planner_check_history WHERE repo = ?",
            ("repo",),
        ).fetchone()
        self.assertEqual(tuple(old), ("result", "decision", "check-session"))
        self.assertEqual(planner_check_state.get(connection, "repo")["stage"], "pending")

    def test_profiles_are_independent_and_only_absent_values_inherit(self):
        catalog = {"model-a": frozenset({"high"}), "model-b": frozenset({"low"})}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            for provider in ("codex", "claude"):
                for value, expected in ((None, ("model-a", "high")), ("fast,low", ("model-b", "low"))):
                    path.write_text(f"[{provider}.models]\nstrong=model-a\nfast=model-b\n"
                                    f"[{provider}.roles]\nplanner=strong,high\nreviewer=strong,high\n"
                                    + (f"planner_check={value}\n" if value is not None else ""), encoding="utf-8")
                    self.assertEqual(agent_profiles.provider_profile(path, provider, f"{provider}.roles",
                                     "planner_check", "check", catalog), expected)
                for value in ("", "missing,low", "fast,high", "fast,unsupported", "strong"):
                    path.write_text(f"[{provider}.models]\nstrong=model-a\nfast=model-b\n"
                                    f"[{provider}.roles]\nplanner=strong,high\nreviewer=strong,high\n"
                                    f"planner_check={value}\n", encoding="utf-8")
                    with self.subTest(provider=provider, value=value), self.assertRaises(RuntimeError):
                        agent_profiles.validate_provider_profiles(path, provider, catalog)

    def test_prompt_and_three_results_are_bounded(self):
        snapshot = MutableReadySnapshot("repo", "sha", (), "token")
        prompt = planner_check_prompt("src/app.py changed", "[]", snapshot)
        for expected in ("src/app.py changed", "compact catalog", "identity snapshot", "모든 파일 변경은 금지", "테스트·verify 실행"):
            self.assertIn(expected, prompt)
        self.assertIn("전체 미충족 작업이나 저장소 전체 개선점을 탐색하지 마라", prompt)
        allowed = frozenset({"G-123"})
        for outcome, ids in (("no_change", []), ("needs_revision", ["G-123"]), ("uncertain", [])):
            line = "PLANNER_CHECK_RESULT: " + json.dumps({"outcome": outcome, "evidence": "diff", "reason": "review", "plan_ids": ids})
            decision = parse_planner_check_result(line, allowed)
            self.assertEqual((decision.outcome, decision.plan_ids), (outcome, tuple(ids)))
        for message in ("", "PLANNER_CHECK_RESULT: {}", "PLANNER_CHECK_RESULT: bad",
                        'PLANNER_CHECK_RESULT: {"outcome":"no_change","outcome":"uncertain","evidence":"x","reason":"x","plan_ids":[]}',
                        "PLANNER_CHECK_RESULT: " + json.dumps({"outcome": "other", "evidence": "x", "reason": "x", "plan_ids": []}),
                        "PLANNER_CHECK_RESULT: " + json.dumps({"outcome": [], "evidence": "x", "reason": "x", "plan_ids": []}),
                        "PLANNER_CHECK_RESULT: " + json.dumps({"outcome": "needs_revision", "evidence": "x", "reason": "x", "plan_ids": ["G-999"]})):
            with self.subTest(message=message), self.assertRaises(PlanningResultInvalid):
                parse_planner_check_result(message, allowed)


class PlannerCheckRoutingTest(PlanningCase):
    def test_scoped_call_uses_distinct_role_profile_and_read_only(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        port = loop_manager._PlanningAgentPorts(connection, self.context, None)
        result = loop_manager.AgentResult(0, "checked", "check-session")
        with mock.patch.object(port, "begin_scope") as begin, mock.patch.object(port, "finish_scope") as finish, \
                mock.patch.object(port, "clear_scope") as clear, \
                mock.patch.object(loop_manager, "_agent_profile", return_value=loop_manager.AgentProfile("check-model", "low")) as profile, \
                mock.patch.object(loop_manager, "run_agent", return_value=result) as invoke:
            finish.return_value.error = None
            run_scoped_planner_turn(port, self.context.repo, "planner_check", Path("/tree"), "check", None, lambda _: None)
        begin.assert_called_once_with("planner_check", Path("/tree"))
        profile.assert_called_once_with(self.context.config_path, "codex", "planner_check", None, Path("/tree"))
        self.assertEqual(invoke.call_args.args[4], "planner_check")
        self.assertTrue(invoke.call_args.kwargs["read_only"])
        self.assertEqual(invoke.call_args.kwargs["resolved_profile"].model, "check-model")
        clear.assert_not_called()

    def test_scoped_check_failure_preserves_session_and_scope(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        port = loop_manager._PlanningAgentPorts(connection, self.context, None)
        result = loop_manager.AgentResult(1, "sandbox policy failed", "check-session")
        saved = []
        with mock.patch.object(port, "begin_scope") as begin, mock.patch.object(port, "finish_scope") as finish, \
                mock.patch.object(port, "clear_scope") as clear, \
                mock.patch.object(loop_manager, "_agent_profile", return_value=loop_manager.AgentProfile("check-model", "low")), \
                mock.patch.object(loop_manager, "run_agent", return_value=result):
            finish.return_value.error = None
            outcome, scope = run_scoped_planner_turn(
                port, self.context.repo, "planner_check", Path("/tree"),
                "check", None, saved.append,
            )
        self.assertEqual(outcome.code, 1)
        self.assertIs(scope, finish.return_value)
        self.assertEqual(saved, ["check-session"])
        finish.assert_called_once_with("planner_check", Path("/tree"), begin.return_value)
        clear.assert_not_called()

    def test_planner_session_cannot_resume_as_check(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        from loop_manager_internal.storage import agent_invocation_state
        agent_invocation_state.ensure_schema(connection)
        connection.execute("INSERT INTO agent_invocations (invocation_id, repo, role, provider, model, effort, session_id, transition, attempt, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           ("id", self.context.repo, "planner", "codex", "model", "low", "planner-session", "start", 1, "now"))
        port = loop_manager._PlanningAgentPorts(connection, self.context, None)
        with self.assertRaisesRegex(RuntimeError, "session 역할"), mock.patch.object(loop_manager, "run_agent") as invoke:
            port.run_planner(Path("/tree"), "check", "planner-session", lambda _: None, profile_role="planner_check")
        invoke.assert_not_called()

    def test_check_session_cannot_resume_as_general_planner(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        from loop_manager_internal.storage import agent_invocation_state
        agent_invocation_state.ensure_schema(connection)
        connection.execute("INSERT INTO agent_invocations (invocation_id, repo, role, provider, model, effort, session_id, transition, attempt, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           ("id", self.context.repo, "planner_check", "codex", "model", "low", "check-session", "start", 1, "now"))
        port = loop_manager._PlanningAgentPorts(connection, self.context, None)
        with self.assertRaisesRegex(RuntimeError, "session 역할"), mock.patch.object(loop_manager, "run_agent") as invoke:
            port.run_planner(Path("/tree"), "plan", "check-session", lambda _: None)
        invoke.assert_not_called()

    def test_check_resume_and_session_fallback_keep_role_and_policy(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        from loop_manager_internal.storage import agent_invocation_state
        agent_invocation_state.ensure_schema(connection)
        connection.execute("INSERT INTO agent_invocations (invocation_id, repo, role, provider, model, effort, session_id, transition, attempt, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           ("id", self.context.repo, "planner_check", "codex", "model", "low", "old-session", "start", 1, "now"))
        port = loop_manager._PlanningAgentPorts(connection, self.context, None)
        results = [loop_manager.AgentResult(1, "session not found", "old-session"),
                   loop_manager.AgentResult(0, "checked", "new-session")]
        with mock.patch.object(loop_manager, "_agent_profile", return_value=loop_manager.AgentProfile("check-model", "low")), \
                mock.patch.object(loop_manager, "run_agent", side_effect=results) as invoke:
            outcome = port.run_planner(Path("/tree"), "check", "old-session", lambda _: None,
                                       profile_role="planner_check")
        self.assertEqual(outcome.session_id, "new-session")
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual([call.args[4] for call in invoke.call_args_list], ["planner_check"] * 2)
        self.assertEqual([call.kwargs["read_only"] for call in invoke.call_args_list], [True] * 2)
        self.assertEqual([call.kwargs["session_id"] for call in invoke.call_args_list], ["old-session", None])

    def test_policy_failure_is_not_retried_as_check_success(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        port = loop_manager._PlanningAgentPorts(connection, self.context, None)
        with mock.patch.object(loop_manager, "_agent_profile", return_value=loop_manager.AgentProfile("check-model", "low")), \
                mock.patch.object(loop_manager, "run_agent", return_value=loop_manager.AgentResult(1, "sandbox policy failed")) as invoke:
            outcome = port.run_planner(Path("/tree"), "check", None, lambda _: None,
                                       profile_role="planner_check")
        self.assertEqual(outcome.kind.value, "permanent_configuration")
        invoke.assert_called_once()
