"""Invocation audit, physical retries and session transition contracts."""

import sqlite3
from pathlib import Path
from unittest import TestCase, mock

from loop_manager_internal.agents import agent_invocation as invocation
from loop_manager_internal.storage import agent_invocation_state as state
import loop_manager as manager


class RunnerTests(TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        state.ensure_schema(self.connection)

    def test_execution_agent_port_supplies_ledger_connection(self):
        with mock.patch.object(manager, "run_agent", return_value=manager.AgentResult(0, "done")) as run:
            result = manager._run_agent_with_connection(self.connection, "codex", Path("/worktree"))
        self.assertEqual(result.code, 0)
        self.assertIs(run.call_args.kwargs["connection"], self.connection)

    def test_pre_resolved_worker_profile_is_shared_by_cli_and_ledger(self):
        selected = invocation.ResolvedProfile('actual-model', 'high')
        class Provider:
            def run(self, request):
                self_outer.assertEqual(request.profile, selected)
                return manager.AgentResult(0, 'done', 'worker-session')
        self_outer = self
        with mock.patch.object(manager, '_agent_profile') as resolve, \
             mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), Path('/state'), Path('/config'),
                'worker', 'prompt', read_only=False, level=5,
                connection=self.connection,
                invocation=invocation.InvocationContext('repo', 'G-041'),
                resolved_profile=selected,
            )
        self.assertEqual(result.code, 0)
        resolve.assert_not_called()
        row = self.connection.execute('SELECT model, effort FROM agent_invocations').fetchone()
        self.assertEqual(tuple(row), ('actual-model', 'high'))

    def test_each_fallback_process_has_own_resolved_profile_and_session(self):
        seen = []

        class Provider:
            def run(self, request):
                seen.append(request.profile)
                if request.session_id:
                    return manager.AgentResult(1, "session not found", request.session_id)
                request.on_session_started("new-session")
                return manager.AgentResult(0, "done", "new-session",
                                           usage=invocation.Usage(20, 10, None, 2))

        attempts = invocation.AttemptSequence("repo", "G-039", "revision")
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "worker", "secret prompt", read_only=False, level=3,
                persist_session=True, session_id=session, connection=self.connection,
                invocation=context), attempts)
        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("real-model", "high")) as resolve_profile, \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent_with_retries(call, session_id="old-session")
        self.assertEqual(result.code, 0)
        self.assertEqual(resolve_profile.call_count, 2)
        self.assertEqual(seen, [invocation.ResolvedProfile("real-model", "high")] * 2)
        rows = self.connection.execute("SELECT * FROM agent_invocations ORDER BY attempt").fetchall()
        self.assertEqual([(row["attempt"], row["transition"], row["transition_cause"],
                           row["session_id"], row["result_kind"])
                          for row in rows],
                         [(1, "resume", None, "old-session", "permanent_configuration"),
                          (2, "fallback", "session_unavailable", "new-session", "success")])
        self.assertEqual(rows[1]["model"], "real-model")
        self.assertEqual(rows[1]["cached_input_tokens"], 10)
        self.assertNotIn("secret prompt", repr(dict(rows[1])))

    def test_planner_worker_reviewer_rows_keep_role_and_plan_identity(self):
        class Provider:
            def run(self, request):
                request.on_session_started(f"{request.role_name}-session")
                return manager.AgentResult(0, "done", f"{request.role_name}-session")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "medium")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            for role, plan in (("planner", None), ("worker", "G-039"),
                               ("reviewer", "G-039")):
                result = manager.run_agent(
                    "codex", Path("/worktree"), Path("/state"), Path("/config"),
                    role, "secret prompt", read_only=role == "reviewer",
                    level=3 if role == "worker" else None,
                    connection=self.connection,
                    invocation=invocation.InvocationContext("repo", plan,
                                                             "revision" if plan else None))
                self.assertEqual(result.code, 0)
        rows = self.connection.execute(
            "SELECT role, plan_id, definition_revision, session_id FROM agent_invocations ORDER BY rowid"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("planner", None, None, "planner-session"),
            ("worker", "G-039", "revision", "worker-session"),
            ("reviewer", "G-039", "revision", "reviewer-session"),
        ])

    def test_planner_check_ledger_keeps_role_profile_session_and_usage(self):
        class Provider:
            def run(self, request):
                self_outer.assertEqual(request.role_name, "planner_check")
                self_outer.assertTrue(request.read_only)
                self_outer.assertEqual((request.profile.model, request.profile.effort), ("check-model", "low"))
                request.on_session_started("check-session")
                return manager.AgentResult(0, "checked", "check-session",
                                           usage=invocation.Usage(input_tokens=12, output_tokens=3))

        self_outer = self
        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("check-model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner_check", "secret prompt", read_only=True,
                connection=self.connection,
                invocation=invocation.InvocationContext("repo", None),
            )
        self.assertEqual(result.code, 0)
        row = self.connection.execute(
            "SELECT role, model, effort, session_id, input_tokens, output_tokens FROM agent_invocations"
        ).fetchone()
        self.assertEqual(tuple(row), ("planner_check", "check-model", "low", "check-session", 12, 3))

    def test_ledger_failure_does_not_change_agent_success(self):
        self.connection.execute("DROP TABLE agent_invocations")

        class Provider:
            def run(self, request):
                return manager.AgentResult(0, "done", "session")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual(result.code, 0)
        self.assertEqual(result.collection_status, "error")

    def test_auxiliary_collection_error_keeps_success_and_usage_in_ledger(self):
        class Provider:
            def run(self, request):
                return manager.AgentResult(
                    0, "done", "session", usage=invocation.Usage(10, 4, None, 2),
                    collection_status="error",
                )

        with mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None),
                resolved_profile=manager.AgentProfile("model", "low"),
            )
        row = self.connection.execute("""SELECT result_kind, result_code, session_id,
            collection_status, input_tokens, cached_input_tokens, output_tokens
            FROM agent_invocations""").fetchone()
        self.assertEqual(result.code, 0)
        self.assertEqual(tuple(row), ("success", 0, "session", "error", 10, 4, 2))

    def test_profile_observation_failure_does_not_replace_provider_result(self):
        class Provider:
            def run(self, request):
                if request.profile is not None:
                    raise AssertionError("unresolved profile must stay absent")
                return manager.AgentResult(0, "done", "session")

        with mock.patch.object(manager, "_agent_profile", side_effect=RuntimeError("missing config")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual((result.code, result.collection_status), (0, "error"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM agent_invocations").fetchone()[0], 0)

    def test_transient_retry_and_timeout_have_distinct_rows(self):
        count = 0

        class Provider:
            def run(self, request):
                nonlocal count
                count += 1
                if count == 1:
                    raise manager.subprocess.TimeoutExpired("agent", 1)
                return manager.AgentResult(0, "done", "session")

        attempts = invocation.AttemptSequence("repo", None)
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, session_id=session,
                connection=self.connection, invocation=context), attempts)
        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent_with_retries(call, session_id=None)
        self.assertEqual(result.code, 0)
        rows = self.connection.execute("SELECT result_kind, transition_cause FROM agent_invocations ORDER BY attempt").fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("timeout", None), ("success", "timeout")])

    def test_timeout_spellings_share_timeout_ledger_kind(self):
        class Provider:
            def run(self, request):
                return manager.AgentResult(1, self.message)

        for message in ("Agent CLI timeout", "Agent CLI timed out"):
            with self.subTest(message=message), \
                 mock.patch.object(manager, "_agent_profile",
                                   return_value=manager.AgentProfile("model", "low")), \
                 mock.patch.object(manager, "agent_provider", return_value=Provider()):
                Provider.message = message
                result = manager.run_agent(
                    "codex", Path("/worktree"), Path("/state"), Path("/config"),
                    "planner", "secret", read_only=False, connection=self.connection,
                    invocation=invocation.InvocationContext("repo", None),
                    resolved_profile=manager.AgentProfile("model", "low"),
                )
            self.assertEqual(result.ledger_kind, "timeout")
        rows = self.connection.execute(
            "SELECT result_kind FROM agent_invocations ORDER BY rowid"
        ).fetchall()
        self.assertEqual([row["result_kind"] for row in rows], ["timeout", "timeout"])

    def test_latest_attempt_without_final_session_clears_saved_session(self):
        class Provider:
            def run(self, request):
                self_outer.assertEqual(request.session_id, "saved-session")
                return manager.AgentResult(1, "configuration error")

        self_outer = self
        with mock.patch.object(manager, "agent_provider", return_value=Provider()):
            manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, session_id="saved-session",
                connection=self.connection,
                invocation=invocation.InvocationContext("repo", None),
                resolved_profile=manager.AgentProfile("model", "low"),
            )
        row = self.connection.execute(
            "SELECT session_id, result_kind FROM agent_invocations"
        ).fetchone()
        self.assertEqual(tuple(row), (None, "permanent_configuration"))

    def test_replacement_context_marks_first_process_and_retry_cause(self):
        attempts = invocation.AttemptSequence(
            "repo", "G-039", "revision",
            initial_transition="replace", initial_cause="profile_changed")
        first = attempts.next(None, False)
        attempts.previous_failure = "usage_limit"
        retry = attempts.next(None, False)
        self.assertEqual(
            [(item.transition, item.transition_cause, item.attempt)
             for item in (first, retry)],
            [("replace", "profile_changed", 1), ("start", "usage_limit", 2)],
        )

    def test_retry_after_session_fallback_records_actual_transition(self):
        attempts = invocation.AttemptSequence("repo", "G-039")
        first = attempts.next("old-session", False)
        attempts.previous_failure = "permanent_configuration"
        fallback = attempts.next(None, True)
        attempts.previous_failure = "transient_failure"
        retry = attempts.next("new-session", True)
        self.assertEqual(
            [(item.transition, item.transition_cause) for item in (first, fallback, retry)],
            [("resume", None), ("fallback", "session_unavailable"),
             ("resume", "transient_failure")],
        )

    def test_session_observation_storage_error_keeps_agent_result(self):
        class Provider:
            def run(self, request):
                request.on_session_started("observed")
                return manager.AgentResult(0, "done", "observed")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()), \
             mock.patch.object(state.InvocationRepository, "observe_session",
                               side_effect=sqlite3.OperationalError("unavailable")):
            result = manager.run_agent(
                "codex", Path("/worktree"), Path("/state"), Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual((result.code, result.collection_status), (0, "error"))
        row = self.connection.execute("SELECT session_id, collection_status FROM agent_invocations").fetchone()
        self.assertEqual(tuple(row), ("observed", "error"))

    def test_interrupted_process_preserves_observed_session(self):
        class Provider:
            def run(self, request):
                request.on_session_started("observed")
                raise KeyboardInterrupt()

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            with self.assertRaises(KeyboardInterrupt):
                manager.run_agent(
                    "codex", Path("/worktree"), Path("/state"), Path("/config"),
                    "worker", "secret", read_only=False, level=1,
                    connection=self.connection,
                    invocation=invocation.InvocationContext("repo", "G-039"))
        row = self.connection.execute(
            "SELECT session_id, result_kind, finished_at FROM agent_invocations"
        ).fetchone()
        self.assertEqual((row["session_id"], row["result_kind"]),
                         ("observed", "interrupted"))
        self.assertIsNotNone(row["finished_at"])
