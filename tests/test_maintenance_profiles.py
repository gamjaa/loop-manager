"""Maintenance profile selection without changing the Planner execution scope."""
import configparser
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.agents import agent_profiles
from loop_manager_internal.planning.agent_ports import run_scoped_planner_turn
from tests.support.planning_case import PlanningCase


class MaintenanceProfileTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "config.ini"
        self.catalog = {"model-strong": frozenset({"high", "xhigh"}),
                        "model-fast": frozenset({"medium"})}

    def write(self, provider, maintenance=None):
        self.config.write_text(
            f"[{provider}.models]\nstrong=model-strong\nfast=model-fast\n"
            f"[{provider}.roles]\nplanner=strong,xhigh\nplanner_check=fast,medium\nreviewer=strong,high\n"
            + (f"maintenance_planner={maintenance}\n" if maintenance is not None else ""),
            encoding="utf-8",
        )

    def profile(self, provider, name):
        return agent_profiles.provider_profile(self.config, provider, f"{provider}.roles", name,
                                               "Maintenance Planner", self.catalog)

    def test_provider_specific_maintenance_alias_and_effort_are_independent(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                self.write(provider, "fast,medium")
                self.assertEqual(self.profile(provider, "maintenance_planner"), ("model-fast", "medium"))
                self.assertEqual(self.profile(provider, "planner"), ("model-strong", "xhigh"))

    def test_old_configuration_inherits_planner_only_when_key_absent(self):
        for provider in ("codex", "claude"):
            self.write(provider)
            self.assertEqual(self.profile(provider, "maintenance_planner"), ("model-strong", "xhigh"))
            for value in ("", "fast,high", "absent,medium", "strong", "strong,%", "strong,unknown"):
                with self.subTest(provider=provider, value=value):
                    self.write(provider, value)
                    with self.assertRaises(RuntimeError):
                        self.profile(provider, "maintenance_planner")

    def test_startup_validation_includes_configured_maintenance_profile(self):
        for provider in ("codex", "claude"):
            self.write(provider, "fast,high")
            with self.subTest(provider=provider), self.assertRaisesRegex(RuntimeError, "maintenance_planner"):
                agent_profiles.validate_provider_profiles(self.config, provider, self.catalog)

    def test_example_includes_both_profiles_with_existing_model_aliases(self):
        parser = configparser.ConfigParser()
        parser.read(loop_manager.ROOT / "config/config.ini.example")
        for provider in ("codex", "claude"):
            section = f"{provider}.roles"
            value = parser.get(section, "maintenance_planner")
            alias, effort = value.split(",")
            self.assertTrue(parser.get(f"{provider}.models", alias))
            self.assertTrue(effort)


class MaintenanceProfileRoutingTest(PlanningCase):
    def setUp(self):
        super().setUp()
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.port = loop_manager._PlanningAgentPorts(self.connection, self.context, None)

    def test_scoped_maintenance_uses_resolved_profile_and_existing_execution_role(self):
        profile = loop_manager.ResolvedProfile("model-maintenance", "medium")
        result = loop_manager.AgentResult(0, "reviewed", "session")
        with mock.patch.object(self.port, "begin_scope"), mock.patch.object(self.port, "finish_scope") as finish, \
                mock.patch.object(self.port, "clear_scope"), \
                mock.patch.object(loop_manager, "_agent_profile", return_value=profile) as resolve, \
                mock.patch.object(loop_manager, "run_agent", return_value=result) as invoke, \
                mock.patch.object(loop_manager, "operation_log") as log:
            finish.return_value.error = None
            outcome, _scope = run_scoped_planner_turn(
                self.port, self.context.repo, "maintenance_planner", Path("/tree"),
                "review", "saved-session", lambda _: None,
            )
        self.assertEqual(outcome.message, "reviewed")
        resolve.assert_called_once_with(self.context.config_path, "codex", "maintenance_planner", None, Path("/tree"))
        self.assertEqual(invoke.call_args.args[4], "planner")
        self.assertIs(invoke.call_args.kwargs["resolved_profile"], profile)
        self.assertEqual(invoke.call_args.kwargs["session_id"], "saved-session")
        log.assert_called_once_with("maintenance_planner 설정: codex, model-maintenance, medium")

    def test_invalid_profile_stops_before_provider_or_retry(self):
        with mock.patch.object(loop_manager, "_agent_profile", side_effect=RuntimeError("invalid profile")), \
                mock.patch.object(loop_manager, "run_agent") as invoke:
            with self.assertRaisesRegex(RuntimeError, "invalid profile"):
                self.port.run_planner(Path("/tree"), "review", None, lambda _: None,
                                      profile_role="maintenance_planner")
        invoke.assert_not_called()

    def test_normal_planner_keeps_existing_configuration_path(self):
        with mock.patch.object(loop_manager, "_agent_profile") as resolve, \
                mock.patch.object(loop_manager, "run_agent", return_value=loop_manager.AgentResult(0, "planned")) as invoke:
            self.port.run_planner(Path("/tree"), "plan", None, lambda _: None)
        resolve.assert_not_called()
        self.assertEqual(invoke.call_args.args[4], "planner")
        self.assertNotIn("resolved_profile", invoke.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
