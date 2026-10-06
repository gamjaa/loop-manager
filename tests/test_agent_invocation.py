"""Invocation audit, physical retries and session transition contracts."""

import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from loop_manager_internal.agents import agent_invocation as invocation
from loop_manager_internal.storage import agent_invocation_state as state
import loop_manager as manager


class RunnerTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
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
                'codex', Path('/worktree'), self.state_dir, Path('/config'),
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

        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        guidance_repo = GuidanceRepository(self.connection)
        snapshot = common_guidance.resolve(guidance_repo, invocation.InvocationContext("repo", "G-039"),
                                          "worker", Path("/worktree"), self.state_dir)
        guidance_repo.bind_session("repo", "codex", "old-session", snapshot.task_id)
        attempts = invocation.AttemptSequence("repo", "G-039", "revision")
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                    "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                    "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        guidance_repo = GuidanceRepository(self.connection)
        snapshot = common_guidance.resolve(guidance_repo, invocation.InvocationContext('repo', None),
                                          'planner', Path('/worktree'), self.state_dir)
        guidance_repo.bind_session('repo', 'codex', 'saved-session', snapshot.task_id)
        class Provider:
            def run(self, request):
                self_outer.assertEqual(request.session_id, "saved-session")
                return manager.AgentResult(1, "configuration error")

        self_outer = self
        with mock.patch.object(manager, "agent_provider", return_value=Provider()):
            manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
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
                    "codex", Path("/worktree"), self.state_dir, Path("/config"),
                    "worker", "secret", read_only=False, level=1,
                    connection=self.connection,
                    invocation=invocation.InvocationContext("repo", "G-039"))
        row = self.connection.execute(
            "SELECT session_id, result_kind, finished_at FROM agent_invocations"
        ).fetchone()
        self.assertEqual((row["session_id"], row["result_kind"]),
                         ("observed", "interrupted"))
        self.assertIsNotNone(row["finished_at"])


class CommonGuidanceInvocationTests(TestCase):
    """All roles share the same durable instruction resolution boundary."""
    def setUp(self):
        import tempfile
        from loop_manager_internal.agents import common_guidance
        self.guidance_module = common_guidance
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'runtime'
        (self.runtime / 'docs').mkdir(parents=True)
        self.source = self.runtime / common_guidance.COMMON_PATH
        self.source.write_text('자동 지침 "quoted" \\path\n두 번째 줄\n', encoding='utf-8')
        self.state_dir = self.root / 'state'
        self.worktree = self.root / 'candidate'
        self.worktree.mkdir()
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        state.ensure_schema(self.connection)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(common_guidance, 'RUNTIME_ROOT', self.runtime).start()
        mock.patch.object(common_guidance.subprocess, 'run', return_value=
                          manager.subprocess.CompletedProcess(['git'], 0, 'a' * 40)).start()
        self.requests = []
        outer = self
        class Provider:
            def run(self, request):
                outer.requests.append(request)
                request.on_session_started('session-' + str(len(outer.requests)))
                return manager.AgentResult(0, 'done', 'session-' + str(len(outer.requests)))
        self.provider = Provider()
        self.provider_mock = mock.patch.object(manager, 'agent_provider', return_value=self.provider).start()

    def invoke(self, role='worker', plan='G-147', session=None, task=None, provider='codex'):
        return manager.run_agent(
            provider, self.worktree, self.state_dir, self.root / 'config.ini', role,
            'delta only', read_only=role in {'reviewer', 'planner_check'},
            persist_session=True, session_id=session, connection=self.connection,
            invocation=invocation.InvocationContext('repo', plan, task_id=task),
            resolved_profile=invocation.ResolvedProfile('model', 'high'),
        )

    def test_worker_reviewer_retry_and_new_tasks_preserve_runtime_version(self):
        self.assertEqual(self.invoke().code, 0)
        original = self.requests[-1].common_guidance
        rows = self.connection.execute('SELECT guidance_task_id, guidance_content_hash FROM agent_invocations').fetchall()
        self.assertEqual(tuple(rows[0]), (original.task_id, original.content_hash))
        (self.worktree / 'docs').mkdir()
        (self.worktree / self.guidance_module.COMMON_PATH).write_text('candidate changes', encoding='utf-8')
        self.source.write_text('새 runtime 지침\n', encoding='utf-8')
        for role, session in (('worker', 'session-1'), ('reviewer', None), ('worker', None)):
            self.assertEqual(self.invoke(role=role, session=session).code, 0)
            self.assertEqual(self.requests[-1].common_guidance, original)
            self.assertEqual(self.requests[-1].prompt, 'delta only')
        self.assertEqual(self.invoke(plan='G-148').code, 0)
        self.assertNotEqual(self.requests[-1].common_guidance.content_hash, original.content_hash)
        for role in ('planner', 'planner_check', 'reviewer'):
            self.assertEqual(self.invoke(role=role, plan=None, task='independent-' + role).code, 0)
            self.assertEqual(self.requests[-1].common_guidance.content, '새 runtime 지침\n')

    def test_one_shot_rejects_state_inside_candidate_before_any_database_write(self):
        state_dir = self.worktree / 'state'
        result = manager.run_agent('codex', self.worktree, state_dir, self.root / 'config',
            'worker', 'delta only', read_only=False, resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertEqual(result.kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        self.assertFalse(state_dir.exists())
        self.provider_mock.assert_not_called()

    def test_legacy_session_uses_existing_minimal_context_fallback_and_keeps_audit(self):
        attempts = invocation.AttemptSequence('repo', 'G-147')
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                'codex', self.worktree, self.state_dir, self.root / 'config', 'worker',
                'new role/task context' if fallback else 'delta only', read_only=False,
                connection=self.connection, invocation=context, session_id=session, persist_session=True,
                resolved_profile=invocation.ResolvedProfile('model', 'high')),
            attempts,
        )
        result = manager.run_agent_with_retries(call, session_id='legacy')
        self.assertEqual(result.code, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.requests[0].session_id)
        self.assertEqual(self.requests[0].prompt, 'new role/task context')
        rows = self.connection.execute('SELECT session_id, transition, result_code FROM agent_invocations ORDER BY rowid').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('legacy', 'resume', 1), ('session-1', 'fallback', 0)])

    def test_corrupt_or_missing_snapshot_never_reloads_latest_or_starts_provider(self):
        for damage in ('hash', 'missing', 'encoding', 'directory', 'reference'):
            with self.subTest(damage=damage):
                self.assertEqual(self.invoke(plan='G-' + damage).code, 0)
                saved = self.requests[-1].common_guidance
                before = len(self.requests)
                if damage == 'missing':
                    saved.snapshot_path.unlink()
                elif damage == 'directory':
                    saved.snapshot_path.unlink()
                    saved.snapshot_path.mkdir()
                elif damage == 'reference':
                    self.connection.execute('UPDATE common_guidance_versions SET snapshot_path = ? WHERE task_id = ?',
                                            (str(self.source), saved.task_id))
                else:
                    saved.snapshot_path.write_bytes(b'changed' if damage == 'hash' else b'\xff')
                self.source.write_text('latest ' + damage, encoding='utf-8')
                result = self.invoke(role='reviewer', plan='G-' + damage)
                self.assertEqual(result.result_kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
                self.assertEqual(len(self.requests), before)
                self.assertIn('공통 지침', result.message)

    def test_invalid_runtime_and_failed_storage_reject_before_process(self):
        for value in (b'', b' \n', b'\xff', b'a\0b'):
            with self.subTest(value=value):
                self.source.write_bytes(value)
                self.assertEqual(self.invoke().code, 1)
                self.assertEqual(self.requests, [])
        self.source.unlink()
        self.assertEqual(self.invoke().code, 1)
        self.source.mkdir()
        self.assertEqual(self.invoke().code, 1)
        self.source.rmdir()
        self.source.write_text('valid', encoding='utf-8')
        with mock.patch.object(self.guidance_module.GuidanceRepository, 'save', side_effect=sqlite3.OperationalError('disk full')):
            result = self.invoke()
        self.assertEqual(result.code, 1)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_guidance_versions').fetchone()[0], 0)

    def test_same_content_new_revision_and_restart_keep_stored_origin(self):
        self.assertEqual(self.invoke().code, 0)
        saved = self.requests[-1].common_guidance
        with mock.patch.object(self.guidance_module.subprocess, 'run', return_value=
                               manager.subprocess.CompletedProcess(['git'], 0, 'b' * 40)):
            self.assertEqual(self.invoke(role='reviewer').code, 0)
            self.assertEqual(self.requests[-1].common_guidance.source_revision, 'a' * 40)
            self.assertEqual(self.invoke(plan='G-148').code, 0)
        latest = self.requests[-1].common_guidance
        self.assertEqual(latest.content_hash, saved.content_hash)
        self.assertEqual(latest.snapshot_path, saved.snapshot_path)
        self.assertEqual(latest.source_revision, 'b' * 40)

    def test_session_cannot_cross_tasks_or_provider_preflight_failures(self):
        self.invoke()
        before = len(self.requests)
        result = self.invoke(plan='G-148', session='session-1')
        self.assertEqual(result.code, 1)
        self.assertEqual(len(self.requests), before)
        with mock.patch.object(self.provider, 'run', side_effect=self.guidance_module.GuidanceError('unsupported')):
            result = self.invoke()
        self.assertEqual(result.code, 1)
        row = self.connection.execute('SELECT result_kind, result_code FROM agent_invocations ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(row), ('permanent_configuration', 1))
