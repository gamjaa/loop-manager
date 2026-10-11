"""Fast 선택·기본 off와 실행별 설정 고정을 검증한다."""
from contextlib import nullcontext
from functools import partial
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.agents import agent_invocation, agent_profiles, provider_adapters
from loop_manager_internal.application import commands, scheduler
from loop_manager_internal.application.contracts import ProjectRunKind, ProjectRunResult
from loop_manager_internal.config.managed_config import (
    ManagedConfigError, fast_mode_scope, fast_mode_settings, load_managed_config, provider_fast_mode,
)
from loop_manager_internal.runtime.project_registry import RegisteredProject
from tests.support.profiles import profile_config


class FastModeTests(TestCase):
    def setUp(self):
        self.config = profile_config(self)
        self.original = self.config.read_text(encoding="utf-8")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.calls = []
        self.profile = agent_invocation.ResolvedProfile("configured-model", "high")

    def set_fast(self, codex="", claude=""):
        self.config.write_text(self.original + f"\n[codex]\nfast_mode = {codex}\n"
                               f"[claude]\nfast_mode = {claude}\n", encoding="utf-8")

    def invoke(self, provider, role="worker", session=None, code=0, message="done"):
        events = ([{"type": "thread.started", "thread_id": "new-session"},
                   {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
                   {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
                  if provider == "codex" else
                  [{"type": "system", "session_id": "new-session"},
                   {"type": "result", "result": message, "session_id": "new-session", "is_error": bool(code)}])
        raw = "\n".join(json.dumps(event) for event in events) + "\n"

        def start(arguments, **kwargs):
            self.calls.append(arguments)
            if provider == "codex" and "-o" in arguments:
                Path(arguments[arguments.index("-o") + 1]).write_text(message, encoding="utf-8")
            return SimpleNamespace(stdin=mock.Mock(), stdout=io.StringIO(raw),
                                   wait=lambda: code, kill=mock.Mock())

        def run(arguments, **kwargs):
            self.calls.append(arguments)
            if provider == "codex":
                Path(arguments[arguments.index("-o") + 1]).write_text(message, encoding="utf-8")
                kwargs["stdout"].write(raw)
                return subprocess.CompletedProcess(arguments, code)
            return subprocess.CompletedProcess(arguments, code, json.dumps(events[-1]))

        effects = provider_adapters.ProviderEffects(mock.Mock(), mock.Mock(), mock.Mock(), start, run)
        function = provider_adapters.run_codex if provider == "codex" else provider_adapters.run_claude
        with mock.patch.object(provider_adapters, "_start_timeout", return_value=mock.Mock()):
            result = function(self.worktree, self.root / "state", self.config, role, "작업 내용",
                              read_only=role in {"reviewer", "planner_check"},
                              level=3 if role == "worker" else None, persist_session=True,
                              session_id=session, resolved_profile=self.profile, effects=effects)
        effects.role.assert_not_called()
        effects.worker_profile.assert_not_called()
        effects._agent_profile.assert_not_called()
        return agent_invocation._coerce_agent_result(result, session)

    def assert_fast(self, provider, requested):
        arguments = self.calls[-1]
        model_flag = "-m" if provider == "codex" else "--model"
        self.assertEqual(arguments[arguments.index(model_flag) + 1], self.profile.model)
        if provider == "codex":
            self.assertIn('model_reasoning_effort="high"', arguments)
            tiers = [arg for arg in arguments if arg.startswith("service_tier=")]
            features = [arg for arg in arguments if arg.startswith("features.fast_mode=")]
            self.assertEqual(tiers, ['service_tier="fast"' if requested else 'service_tier="default"'])
            self.assertEqual(features, [f"features.fast_mode={str(requested).lower()}"])
            self.assertIn('approvals_reviewer="auto_review"', arguments)
        else:
            self.assertEqual(arguments[arguments.index("--effort") + 1], "high")
            self.assertEqual(arguments.count("--settings"), 1)
            settings = json.loads(arguments[arguments.index("--settings") + 1])
            self.assertIs(settings["fastMode"], requested)
            self.assertEqual(settings["permissions"], {"defaultMode": "auto"})
            self.assertTrue(settings["sandbox"]["enabled"])
            self.assertTrue(settings["sandbox"]["failIfUnavailable"])
            self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        name = "worker.log" if provider == "codex" else "claude-worker.log"
        logs = list((self.root / "state").rglob(name))
        if logs:
            header = json.loads(logs[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(header, {"type": "loop_manager.fast_mode", "requested": requested})

    def test_boolean_values_and_default_off_do_not_use_ini_defaults(self):
        for provider in ("codex", "claude"):
            for content in ("", f"[{provider}]\n", "[DEFAULT]\nfast_mode=true\n",
                            f"[DEFAULT]\nfast_mode=true\n[{provider}]\n"):
                self.config.write_text(content, encoding="utf-8")
                for frozen in (False, True):
                    with self.subTest(provider=provider, content=content, frozen=frozen):
                        scope = fast_mode_scope(load_managed_config(self.config)) if frozen else nullcontext()
                        with scope:
                            self.assertIs(provider_fast_mode(self.config, provider), False)
            for value, expected in (("", False), ("   ", False), ("TrUe", True), ("ON", True),
                                    ("yes", True), ("1", True), ("False", False),
                                    ("off", False), ("no", False), ("0", False)):
                with self.subTest(provider=provider, value=value):
                    self.config.write_text(f"[{provider}]\nfast_mode={value}\n", encoding="utf-8")
                    self.assertIs(provider_fast_mode(self.config, provider), expected)

    def test_invalid_selected_value_stops_before_cli_and_does_not_leak(self):
        for provider in ("codex", "claude"):
            for value in ("maybe", "2", "%secret%"):
                self.set_fast(value if provider == "codex" else "", value if provider == "claude" else "")
                with self.subTest(provider=provider, value=value):
                    with self.assertRaisesRegex(ManagedConfigError, rf"\[{provider}\] fast_mode") as raised:
                        self.invoke(provider)
                    self.assertNotIn(value, str(raised.exception))
                    self.assertEqual(self.calls, [])
                    effects = mock.Mock()
                    with mock.patch.object(agent_profiles.shutil, "which") as executable:
                        with self.assertRaises(ManagedConfigError):
                            agent_profiles.validate_provider_runtime(self.config, provider, effects=effects)
                    executable.assert_not_called()
                    effects.command.assert_not_called()

    def test_invalid_unselected_provider_does_not_prevent_selected_profiles(self):
        self.set_fast("on", "invalid")
        snapshot = load_managed_config(self.config)
        catalog = {"test-fast": frozenset({"medium", "high", "max"}),
                   "test-balanced": frozenset({"high", "xhigh"}),
                   "test-strong": frozenset({"high", "xhigh"})}
        with fast_mode_scope(snapshot):
            agent_profiles.validate_provider_profiles(self.config, "codex", catalog)
            self.assertIs(provider_fast_mode(self.config, "codex"), True)
            with self.assertRaises(ManagedConfigError):
                provider_fast_mode(self.config, "claude")

    def test_on_off_and_blank_are_passed_in_new_and_resumed_calls(self):
        for provider in ("codex", "claude"):
            for value, expected in (("true", True), ("false", False), ("", False), ("   ", False)):
                self.set_fast(value, value)
                for session in (None, "saved-session"):
                    with self.subTest(provider=provider, value=value, session=session):
                        self.assertEqual(self.invoke(provider, session=session).code, 0)
                        self.assert_fast(provider, expected)
                        self.assertEqual(len(self.calls), 1)
                        self.calls.clear()

    def test_missing_fast_setting_or_config_passes_off_in_new_and_resumed_calls(self):
        for content in (self.original, self.original + "\n[codex]\n[claude]\n", None):
            if content is None:
                self.config.unlink()
            else:
                self.config.write_text(content, encoding="utf-8")
            for provider in ("codex", "claude"):
                for session in (None, "saved-session"):
                    with self.subTest(provider=provider, content=content, session=session):
                        self.assertEqual(self.invoke(provider, session=session).code, 0)
                        self.assert_fast(provider, False)
                        self.assertEqual(len(self.calls), 1)
                        self.calls.clear()

    def test_default_off_is_frozen_until_next_execution(self):
        snapshot = load_managed_config(self.config)
        self.set_fast("true", "true")
        with fast_mode_scope(snapshot):
            for provider in ("codex", "claude"):
                for session in (None, "saved-session"):
                    with self.subTest(provider=provider, session=session):
                        self.assertEqual(self.invoke(provider, session=session).code, 0)
                        self.assert_fast(provider, False)
                        self.calls.clear()
        for provider in ("codex", "claude"):
            self.assertIs(provider_fast_mode(self.config, provider), True)

    def test_every_role_uses_common_fast_setting_without_model_checks(self):
        self.set_fast("true", "true")
        for provider in ("codex", "claude"):
            for role in ("planner", "planner_check", "maintenance_planner", "reviewer", "worker"):
                with self.subTest(provider=provider, role=role):
                    self.assertEqual(self.invoke(provider, role=role).code, 0)
                    self.assert_fast(provider, True)
                    self.assertEqual(len(self.calls), 1)
                    self.calls.clear()

    def test_cli_standard_fallback_and_failure_keep_existing_results(self):
        self.set_fast("true", "true")
        for provider in ("codex", "claude"):
            for code, message in ((0, "Fast unavailable; standard speed"), (1, "Fast unavailable")):
                with self.subTest(provider=provider, code=code):
                    result = self.invoke(provider, code=code, message=message)
                    self.assertEqual((result.code, result.message), (code, message))
                    self.assertEqual(len(self.calls), 1)
                    self.assert_fast(provider, True)
                    self.calls.clear()

    def test_retry_and_session_fallback_keep_start_snapshot(self):
        for provider in ("codex", "claude"):
            self.set_fast("true", "true")
            snapshot = load_managed_config(self.config)
            responses = iter(((1, "session not found"), (1, "network error"), (0, "done")))
            def invoke(session, fallback):
                code, message = next(responses)
                self.set_fast("false", "false")
                result = self.invoke(provider, session=session, code=code, message=message)
                self.assert_fast(provider, True)
                return result
            with fast_mode_scope(snapshot):
                result = agent_invocation.run_agent_with_retries(invoke, session_id="saved-session")
            self.assertEqual((result.code, result.message), (0, "done"))
            self.assertEqual(len(self.calls), 3)
            self.calls.clear()
            self.assertIs(provider_fast_mode(self.config, provider), False)
            self.assertIsNone(fast_mode_settings.get())

    def test_parallel_registered_workers_use_the_same_start_snapshot(self):
        self.set_fast("true", "false")
        snapshot = load_managed_config(self.config)
        self.set_fast("false", "true")
        projects = tuple(RegisteredProject(provider, "main", provider, None, False)
                         for provider in ("codex", "claude"))
        seen = []
        def run_project(connection, state_dir, repo, config_path, *args, **kwargs):
            seen.append((repo, provider_fast_mode(config_path, repo)))
            return ProjectRunResult(ProjectRunKind.WAITING, 0)
        worker = scheduler.RunRegisteredProjectEffects(mock.Mock(),
            lambda *args, **kwargs: sqlite3.connect(":memory:"), mock.Mock(), run_project)
        effects = scheduler.ScheduleRegisteredProjectsEffects(mock.Mock(),
            partial(scheduler._run_registered_project, worker))
        with mock.patch.object(scheduler.project_registry, "list_projects", return_value=projects), \
             mock.patch.object(scheduler, "next_local_recovery_request", return_value=None), \
             mock.patch.object(scheduler.notifications, "configure_session"), \
             mock.patch.object(scheduler.notifications, "immediate_delivery", return_value=nullcontext()), \
             mock.patch.object(scheduler, "VerifyCoordinator", return_value=mock.MagicMock()):
            results = scheduler.schedule_registered_projects(effects, mock.Mock(), self.root, snapshot)
        self.assertEqual(sorted(seen), [("claude", False), ("codex", True)])
        self.assertTrue(all(result.result.exit_code == 0 for result in results))
        self.assertIsNone(fast_mode_settings.get())

    def test_manual_command_freezes_settings_and_restores_context_on_failure(self):
        self.set_fast("true", "true")
        def manual(*args):
            self.set_fast("false", "false")
            with fast_mode_scope(load_managed_config(self.config)):
                self.assertIs(provider_fast_mode(self.config, "codex"), True)
            raise RuntimeError("작업 실패")
        effects = commands.ExecuteRuntimeCommandEffects(mock.Mock(), mock.Mock(), mock.Mock(),
            load_managed_config, mock.Mock(), mock.Mock(), mock.Mock(), manual)
        with self.assertRaisesRegex(RuntimeError, "작업 실패"):
            commands.execute_runtime_command(effects, SimpleNamespace(command="manual"), self.root, self.config)
        self.assertIsNone(fast_mode_settings.get())
        self.assertIs(provider_fast_mode(self.config, "codex"), False)
