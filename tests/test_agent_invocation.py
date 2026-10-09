"""Invocation audit, physical retries and session transition contracts."""

import sqlite3
import stat
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

        from tests.support.workspace import pin_common_guidance
        pin_common_guidance(self.connection, invocation.InvocationContext("repo", "G-039"),
                            Path("/worktree"), self.state_dir, sessions=("old-session",))
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
        from tests.support.workspace import pin_common_guidance
        pin_common_guidance(self.connection, invocation.InvocationContext('repo', None),
                            Path('/worktree'), self.state_dir, role='planner',
                            sessions=('saved-session',))
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
        self.source.write_text('자동 지침 "quoted" \\path\n두 번째 줄\n', encoding='utf-8', newline='\n')
        self.role_texts = {}
        (self.runtime / common_guidance.PONYTAIL_PATH).write_text('Ponytail fixture\n', encoding='utf-8', newline='\n')
        for path in set(common_guidance.ROLE_PATHS.values()):
            self.role_texts[path] = '정적 역할: ' + path + '\n'
            (self.runtime / path).write_text(self.role_texts[path], encoding='utf-8', newline='\n')
        self.state_dir = self.root / 'state'
        self.worktree = self.root / 'candidate'
        self.worktree.mkdir()
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        state.ensure_schema(self.connection)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(common_guidance, 'RUNTIME_ROOT', self.runtime).start()
        self.runtime_probe = mock.patch.object(common_guidance.subprocess, 'run', return_value=
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

    def test_always_adds_ponytail_only_to_worker_without_plugin_queries_and_records_hash(self):
        self.assertEqual(self.invoke().code, 0)
        selected = self.requests[-1].common_guidance
        self.assertEqual(selected.content.count('Ponytail fixture'), 1)
        self.assertEqual(selected.snapshot_path.read_text(encoding='utf-8'), selected.content)
        row = self.connection.execute('SELECT guidance_content_hash FROM agent_invocations').fetchone()
        self.assertEqual(row[0], selected.content_hash)
        self.assertEqual(self.invoke(session='session-1').code, 0)
        self.assertEqual(self.requests[-1].common_guidance, selected)
        for role in ('planner', 'maintenance_planner', 'planner_check', 'reviewer'):
            self.assertEqual(self.invoke(role=role, plan=None, task=role).code, 0)
            self.assertNotIn('Ponytail fixture', self.requests[-1].common_guidance.content)
            self.assertEqual(self.requests[-1].prompt, 'delta only')
        self.assertTrue(all('plugin' not in call.args[0] for call in self.runtime_probe.call_args_list))

    def test_provider_switch_keeps_the_same_pinned_ponytail_source_and_delivery_hash(self):
        self.assertEqual(self.invoke(provider='codex').code, 0)
        original = self.requests[-1].common_guidance
        fingerprint = self.guidance_module.worker_content_hash(self.connection, 'repo', 'G-147')
        source_fingerprint = self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-147')
        (self.runtime / self.guidance_module.PONYTAIL_PATH).write_text('updated Ponytail')
        self.assertEqual(self.invoke(provider='claude').code, 0)
        added = self.requests[-1].common_guidance
        self.assertEqual(added.task_id, original.task_id)
        self.assertEqual(added.source_revision, original.source_revision)
        self.assertIn('Ponytail fixture', original.content)
        self.assertIn('Ponytail fixture', added.content)
        self.assertNotIn('updated Ponytail', added.content)
        self.assertEqual(added.content_hash, original.content_hash)
        self.assertEqual(fingerprint,
                         self.guidance_module.worker_content_hash(self.connection, 'repo', 'G-147'))
        self.assertEqual(source_fingerprint,
                         self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-147'))
        self.assertEqual(self.invoke(provider='codex').code, 0)
        self.assertEqual(self.requests[-1].common_guidance, original)
        self.assertEqual(fingerprint,
                         self.guidance_module.worker_content_hash(self.connection, 'repo', 'G-147'))

    def test_ponytail_only_change_affects_new_tasks_without_changing_pinned_workers(self):
        before = self.guidance_module.runtime_content_hash()
        self.assertEqual(self.invoke().code, 0)
        original = self.requests[-1].common_guidance
        (self.runtime / self.guidance_module.PONYTAIL_PATH).write_text('updated Ponytail')
        self.assertNotEqual(self.guidance_module.runtime_content_hash(), before)
        self.assertEqual(self.invoke().code, 0)
        self.assertEqual(self.requests[-1].common_guidance, original)
        self.assertEqual(self.invoke(plan='G-148').code, 0)
        self.assertIn('updated Ponytail', self.requests[-1].common_guidance.content)

    def test_old_json_bundle_is_resumed_without_retrospective_ponytail(self):
        import hashlib
        import json
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        files = {path: (self.runtime / path).read_text(encoding='utf-8')
                 for path in self.guidance_module.LEGACY_GUIDANCE_PATHS}
        contents = json.dumps({'version': 1, 'files': files}).encode()
        digest = hashlib.sha256(contents).hexdigest()
        directory = self.state_dir / 'agent-guidance'
        directory.mkdir(parents=True, mode=0o700)
        snapshot = directory / (digest + '.json')
        snapshot.write_bytes(contents)
        identity = self.guidance_module.task_identity(
            invocation.InvocationContext('repo', 'G-147'), 'worker', self.worktree)
        GuidanceRepository(self.connection).save(identity, 'repo', digest, 'a' * 40, str(snapshot))
        for path in self.guidance_module.GUIDANCE_PATHS:
            (self.runtime / path).unlink()
        self.assertEqual(self.invoke().code, 0)
        self.assertNotIn('Ponytail fixture', self.requests[-1].common_guidance.content)
        self.assertEqual(self.requests[-1].common_guidance.source_revision, 'a' * 40)

    def test_invalid_ponytail_source_is_a_guidance_failure(self):
        path = self.runtime / self.guidance_module.PONYTAIL_PATH
        for value in (b'', b'\xff', b'instructions\0'):
            path.write_bytes(value)
            self.assertEqual(self.invoke().result_kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        path.unlink()
        self.assertEqual(self.invoke().result_kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        self.assertEqual(self.requests, [])

    def test_modern_session_fixture_binds_selected_plan_and_models_only_pr_body_reads(self):
        from types import SimpleNamespace
        from tests.support.worker import dummy_worker_context
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        repository = mock.Mock()
        repository.projected.return_value = SimpleNamespace(
            plan_id='G-147', worktree=str(self.worktree), worker_session_id='saved-worker',
            reviewer_session_id='saved-reviewer', worker_session_binding=None)
        pr = {'body': '', 'state': 'OPEN', 'headRefOid': 'head'}
        with mock.patch.object(manager, '_ExecutionLifecyclePorts', return_value='ports') as ports, \
             mock.patch.object(manager, 'command', return_value='delegated') as command, \
             mock.patch.object(manager, 'git', return_value=SimpleNamespace(stdout='branch')), \
             mock.patch.object(manager, 'pr_for_branch', return_value=pr):
            with dummy_worker_context():
                for provider in ('codex', 'claude'):
                    result = manager._ExecutionLifecyclePorts(self.connection, repository,
                        repo='repo', state_dir=self.state_dir, config_path=Path('unused.ini'), provider=provider)
                    self.assertEqual(result, 'ports')
                observation = manager.command(['gh', 'pr', 'view', 'url', '--json',
                    'body,headRefOid,state'], cwd=self.worktree)
                import json
                self.assertEqual(json.loads(observation.stdout), pr)
                self.assertEqual(manager.command(['gh', 'pr', 'create']), 'delegated')
        self.assertEqual(ports.call_count, 2)
        command.assert_called_once_with(['gh', 'pr', 'create'])
        saved = GuidanceRepository(self.connection)
        context = invocation.InvocationContext('repo', 'G-147')
        identity = self.guidance_module.task_identity(context, 'worker', self.worktree)
        self.assertEqual(saved.session_task('repo', 'codex', 'saved-worker'), identity)
        self.assertEqual(saved.session_task('repo', 'codex', 'saved-reviewer'), identity)
        self.assertEqual(saved.session_task('repo', 'claude', 'saved-worker'), identity)
        self.assertEqual(saved.session_task('repo', 'claude', 'saved-reviewer'), identity)
        self.assertEqual(len(self.requests), 0)

    def test_independent_planner_and_worker_keep_separate_session_bindings(self):
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        planner = self.invoke(role='planner', plan=None, task='planning-input')
        worker = self.invoke()
        self.assertEqual((planner.code, worker.code), (0, 0))
        repository = GuidanceRepository(self.connection)
        planner_task = repository.session_task('repo', 'codex', planner.session_id)
        worker_task = repository.session_task('repo', 'codex', worker.session_id)
        self.assertNotEqual(planner.session_id, worker.session_id)
        self.assertNotEqual(planner_task, worker_task)
        self.assertEqual(planner_task, self.requests[0].common_guidance.task_id)
        self.assertEqual(worker_task, self.requests[1].common_guidance.task_id)
        with self.assertRaisesRegex(RuntimeError, '다른 공통 지침 작업'):
            repository.bind_session('repo', 'codex', planner.session_id, worker_task)
        self.assertEqual(repository.session_task('repo', 'codex', planner.session_id), planner_task)

    def test_started_independent_session_keeps_guidance_after_interruption_and_runtime_change(self):
        outer = self
        class InterruptedProvider:
            def run(self, request):
                outer.requests.append(request)
                request.on_session_started('interrupted-planner')
                raise KeyboardInterrupt()
        self.provider_mock.return_value = InterruptedProvider()
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(role='planner', plan=None, task='planning-input')
        original = self.requests[-1].common_guidance
        self.source.write_text('new runtime instructions')
        self.provider_mock.return_value = self.provider
        result = self.invoke(role='planner', plan=None, task='planning-input', session='interrupted-planner')
        self.assertEqual(result.code, 0)
        self.assertEqual(self.requests[-1].common_guidance, original)
        self.assertEqual(self.requests[-1].session_id, 'interrupted-planner')
        audit = self.connection.execute(
            "SELECT result_kind, session_id FROM agent_invocations WHERE result_kind = 'interrupted'").fetchall()
        self.assertEqual([tuple(row) for row in audit], [('interrupted', 'interrupted-planner')])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM agent_invocations').fetchone()[0], 2)

    def test_worker_reviewer_retry_and_new_tasks_preserve_runtime_version(self):
        self.assertEqual(self.invoke().code, 0)
        original = self.requests[-1].common_guidance
        rows = self.connection.execute('SELECT guidance_task_id, guidance_content_hash FROM agent_invocations').fetchall()
        self.assertEqual(tuple(rows[0]), (original.task_id, original.content_hash))
        (self.worktree / 'docs').mkdir()
        (self.worktree / self.guidance_module.COMMON_PATH).write_text('candidate changes', encoding='utf-8', newline='\n')
        self.source.write_text('새 runtime 지침\n', encoding='utf-8', newline='\n')
        for role, session in (('worker', 'session-1'), ('reviewer', None), ('worker', None)):
            self.assertEqual(self.invoke(role=role, session=session).code, 0)
            selected = self.requests[-1].common_guidance
            self.assertEqual(selected.task_id, original.task_id)
            self.assertEqual(selected.source_revision, original.source_revision)
            if role == 'worker':
                self.assertEqual(selected, original)
            else:
                self.assertIn(self.role_texts[self.guidance_module.ROLE_PATHS[role]], selected.content)
                self.assertNotIn('새 runtime 지침', selected.content)
            self.assertEqual(self.requests[-1].prompt, 'delta only')
        self.assertEqual(self.invoke(plan='G-148').code, 0)
        self.assertNotEqual(self.requests[-1].common_guidance.content_hash, original.content_hash)
        for role in ('planner', 'planner_check', 'reviewer'):
            self.assertEqual(self.invoke(role=role, plan=None, task='independent-' + role).code, 0)
            selected = self.requests[-1].common_guidance.content
            self.assertTrue(selected.startswith('새 runtime 지침\n\n'))
            self.assertTrue(selected.endswith(self.role_texts[self.guidance_module.ROLE_PATHS[role]]))

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
                reference = self.connection.execute(
                    'SELECT snapshot_path FROM common_guidance_versions WHERE task_id = ?',
                    (saved.task_id,)).fetchone()[0]
                bundle = Path(reference)
                before = len(self.requests)
                if damage == 'missing':
                    bundle.unlink()
                elif damage == 'directory':
                    bundle.unlink()
                    bundle.mkdir()
                elif damage == 'reference':
                    self.connection.execute('UPDATE common_guidance_versions SET snapshot_path = ? WHERE task_id = ?',
                                            (str(self.source), saved.task_id))
                else:
                    bundle.write_bytes(b'changed' if damage == 'hash' else b'\xff')
                self.source.write_text('latest ' + damage, encoding='utf-8', newline='\n')
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
        self.source.write_text('valid', encoding='utf-8', newline='\n')
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

    def test_each_role_receives_only_common_and_its_role_outside_git(self):
        common = self.source.read_text(encoding='utf-8')
        for role, path in self.guidance_module.ROLE_PATHS.items():
            with self.subTest(role=role):
                self.assertEqual(self.invoke(role=role, plan=None, task=role).code, 0)
                request = self.requests[-1]
                guidance = request.common_guidance
                expected = common.rstrip('\n') + '\n\n' + self.role_texts[path]
                if role == 'worker':
                    expected = expected.rstrip('\n') + '\n\nPonytail fixture\n'
                self.assertEqual(guidance.content, expected)
                self.assertEqual(guidance.snapshot_path.read_text(encoding='utf-8'), guidance.content)
                self.assertTrue(guidance.snapshot_path.is_relative_to(self.state_dir))
                self.assertFalse(guidance.snapshot_path.is_relative_to(self.worktree))
                self.assertFalse(guidance.snapshot_path.is_relative_to(self.runtime))
                self.assertEqual(request.prompt, 'delta only')
                self.assertEqual(request.role_name, role)

    def test_reviewer_uses_worker_pinned_role_sources_after_runtime_is_removed(self):
        self.assertEqual(self.invoke().code, 0)
        original = self.requests[-1].common_guidance
        common = self.source.read_text(encoding='utf-8')
        for path in self.guidance_module.GUIDANCE_PATHS:
            (self.runtime / path).unlink()
        self.assertEqual(self.invoke(role='reviewer', provider='claude').code, 0)
        reviewer = self.requests[-1].common_guidance
        self.assertEqual(reviewer.task_id, original.task_id)
        self.assertEqual(reviewer.source_revision, original.source_revision)
        self.assertEqual(reviewer.content, common.rstrip('\n') + '\n\n' +
                         self.role_texts[self.guidance_module.ROLE_PATHS['reviewer']])
        original.snapshot_path.unlink()
        self.assertEqual(self.invoke(session='session-1').code, 0)
        self.assertEqual(self.requests[-1].common_guidance, original)

    def test_role_only_change_invalidates_new_task_bundle_and_preserves_old_task(self):
        original_hash = self.guidance_module.runtime_content_hash()
        self.assertEqual(self.invoke().code, 0)
        reviewer_path = self.runtime / self.guidance_module.ROLE_PATHS['reviewer']
        reviewer_path.write_text('new reviewer constraints\n', encoding='utf-8', newline='\n')
        changed_hash = self.guidance_module.runtime_content_hash()
        self.assertNotEqual(changed_hash, original_hash)
        self.assertEqual(self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-147'), original_hash)
        self.assertEqual(self.invoke(role='reviewer').code, 0)
        self.assertNotIn('new reviewer constraints', self.requests[-1].common_guidance.content)
        self.assertEqual(self.invoke(role='reviewer', plan='G-148').code, 0)
        self.assertIn('new reviewer constraints', self.requests[-1].common_guidance.content)
        self.assertEqual(self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-148'), changed_hash)

    def test_missing_invalid_role_sources_and_unknown_roles_never_start_provider(self):
        path = self.runtime / self.guidance_module.ROLE_PATHS['reviewer']
        for value in (b'', b' \n', b'\xff', b'a\0b'):
            with self.subTest(value=value):
                path.write_bytes(value)
                self.assertEqual(self.invoke().result_kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        path.unlink()
        self.assertEqual(self.invoke().code, 1)
        path.mkdir()
        self.assertEqual(self.invoke().code, 1)
        path.rmdir()
        original_lstat = Path.lstat
        # 파일 형식 거부는 OS 관측값으로 검사해 Windows symlink 권한에 의존하지 않는다.
        with mock.patch.object(Path, 'lstat', autospec=True, side_effect=lambda candidate, *a, **kw:
                               mock.Mock(st_mode=stat.S_IFLNK) if candidate == path
                               else original_lstat(candidate, *a, **kw)):
            self.assertEqual(self.invoke().code, 1)
        path.write_text('valid role', encoding='utf-8', newline='\n')
        self.assertEqual(self.invoke(role='../worker').code, 1)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_guidance_versions').fetchone()[0], 0)

    def test_changed_combined_file_is_rejected_instead_of_overwritten(self):
        self.assertEqual(self.invoke().code, 0)
        saved = self.requests[-1].common_guidance
        saved.snapshot_path.write_text('changed derived instructions', encoding='utf-8', newline='\n')
        self.assertEqual(self.invoke().code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(saved.snapshot_path.read_text(encoding='utf-8'), 'changed derived instructions')

    def test_concurrent_snapshot_publication_never_exposes_partial_bytes(self):
        from concurrent.futures import ThreadPoolExecutor
        directory = self.state_dir / 'agent-guidance'
        directory.mkdir(parents=True, mode=0o700)
        contents = ('shared instructions\n' * 10000).encode()
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(self.guidance_module._store_snapshot, directory, contents, '.md')
                       for _ in range(8)]
            paths = [future.result() for future in futures]
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(paths[0].read_bytes(), contents)
        self.assertEqual(list(directory.iterdir()), [paths[0]])

    def test_invalid_stored_bundle_schema_never_starts_provider(self):
        import hashlib
        import json
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        directory = self.state_dir / 'agent-guidance'
        directory.mkdir(parents=True, mode=0o700)
        files = {path: 'valid instructions' for path in self.guidance_module.GUIDANCE_PATHS}
        variants = (
            {'version': 3, 'files': files},
            {'version': 1, 'files': {}},
            {'version': 1, 'files': dict(files, **{'docs/foreign.md': 'expanded role'})},
            {'version': 1, 'files': {**files, self.guidance_module.COMMON_PATH: '\ud800'}},
        )
        for index, bundle in enumerate(variants):
            with self.subTest(bundle=index):
                plan = 'G-schema-' + str(index)
                identity = self.guidance_module.task_identity(
                    invocation.InvocationContext('repo', plan), 'worker', self.worktree)
                contents = json.dumps(bundle).encode('utf-8')
                digest = hashlib.sha256(contents).hexdigest()
                path = directory / (digest + '.json')
                path.write_bytes(contents)
                GuidanceRepository(self.connection).save(identity, 'repo', digest, 'a' * 40, str(path))
                self.assertEqual(self.invoke(plan=plan).result_kind,
                                 invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        self.assertEqual(self.requests, [])

    def test_legacy_markdown_snapshot_resumes_without_adding_current_role_rules(self):
        import hashlib
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        context = invocation.InvocationContext('repo', 'G-legacy')
        identity = self.guidance_module.task_identity(context, 'worker', self.worktree)
        text = 'legacy common and all role instructions\n'
        digest = hashlib.sha256(text.encode()).hexdigest()
        directory = self.state_dir / 'agent-guidance'
        directory.mkdir(parents=True, mode=0o700)
        path = directory / (digest + '.md')
        path.write_text(text, encoding='utf-8', newline='\n')
        GuidanceRepository(self.connection).save(identity, 'repo', digest, 'a' * 40, str(path))
        for filename in self.guidance_module.GUIDANCE_PATHS:
            (self.runtime / filename).unlink()
        for role in ('worker', 'reviewer'):
            self.assertEqual(self.invoke(role=role, plan='G-legacy').code, 0)
            self.assertEqual(self.requests[-1].common_guidance.content, text)
            self.assertEqual(self.requests[-1].common_guidance.snapshot_path, path)


class ProcessStartNotificationTests(TestCase):
    setUp = RunnerTests.setUp
    def test_process_callback_precedes_work_and_internal_retry_does_not_notify(self):
        from loop_manager_internal.notifications import notifications
        self.connection.execute(manager.SCHEMA)
        manager._ensure_execplans_schema(self.connection)
        notifications.ensure_schema(self.connection)
        notifications.configure_session(self.connection, enabled=True)
        self.connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status) "
                                "VALUES ('repo','G-041','plan.md','Named plan','[]','[]','normal','hash','IMPLEMENTING')")
        self.connection.commit()
        outer = self
        class Provider:
            def run(self, request):
                outer.assertEqual(outer.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0 if request.prompt == 'first' else 1)
                request.on_process_started()
                outer.assertEqual(outer.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
                return manager.AgentResult(0, 'done')
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            for attempt in (1, 2):
                result = manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                    'worker', 'first' if attempt == 1 else 'retry', read_only=False, level=5,
                    connection=self.connection, invocation=invocation.InvocationContext('repo','G-041',attempt=attempt,notification_id='retry-sequence'),
                    resolved_profile=invocation.ResolvedProfile('model','high'))
                self.assertEqual(result.code, 0)

    def test_prelaunch_failure_keeps_notified_block_unresumed(self):
        from loop_manager_internal.notifications import notifications
        # Start callback is never reached when provider launch fails.
        calls = []
        class Provider:
            def run(self, request):
                calls.append(request.on_process_started)
                raise OSError('launch failed')
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
             mock.patch.object(notifications, 'record_process_start') as start:
            with self.assertRaises(OSError):
                manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                    'worker', 'first', read_only=False, level=5, connection=self.connection,
                    invocation=invocation.InvocationContext('repo','G-041'),
                    resolved_profile=invocation.ResolvedProfile('model','high'))
        self.assertTrue(calls)
        start.assert_not_called()

    def test_unconfigured_notifications_preserve_provider_invocation_shape(self):
        outer = self
        class Provider:
            def run(self, request):
                outer.assertIsNone(request.on_process_started)
                return manager.AgentResult(0, 'done')
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), self.state_dir, Path('/config'),
                'planner', 'prompt', read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext('repo', None),
                resolved_profile=invocation.ResolvedProfile('model', 'high'),
            )
        self.assertEqual(result.code, 0)
