from tests.support.scope import AgentScopeFixture
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command
from tests.support.worker import dummy_worker_sessions


class ScopeConfigurationTest(AgentScopeFixture, unittest.TestCase):

    def test_git_config_changes_are_rejected_without_leaking_values(self):
        scenarios = (
            (
                "remote-url",
                r"Git config 변경 .*local:remote\.origin\.url",
            ),
            (
                "hooks-path",
                r"Git config 변경 .*local:core\.hookspath",
            ),
        )
        for name, expected in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                if name == "remote-url":
                    sensitive_value = "https://secret@attacker.invalid/repo.git"
                    git_command(
                        repo,
                        "remote",
                        "set-url",
                        "origin",
                        sensitive_value,
                    )
                else:
                    sensitive_value = str(root / "agent-hooks")
                    git_command(
                        repo,
                        "config",
                        "core.hooksPath",
                        sensitive_value,
                    )
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotEqual(before.config, after.config)
                self.assertNotIn(sensitive_value, after.serialize())
                with self.assertRaisesRegex(
                    loop_manager.AgentScopeError,
                    expected,
                ) as violation:
                    loop_manager.validate_agent_scope(
                        "worker",
                        repo,
                        before,
                        after,
                        selected_plan_path=plan_path,
                    )
                self.assertNotIn(sensitive_value, str(violation.exception))

    @dummy_worker_sessions
    def test_blocked_config_change_stops_before_git_reentry(self):
        scenarios = (
            (
                "remote-url",
                ("remote", "set-url", "origin"),
                "https://secret@attacker.invalid/repo.git",
                "local:remote.origin.url",
            ),
            (
                "hooks-path",
                ("config", "core.hooksPath"),
                "agent-hooks",
                "local:core.hookspath",
            ),
        )
        for name, arguments, configured_value, expected_key in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, _plan_path = self._repository_with_plan(directory)
                before = loop_manager.capture_git_snapshot(repo)
                sensitive_value = (
                    str(root / configured_value)
                    if name == "hooks-path"
                    else configured_value
                )
                git_command(repo, *arguments, sensitive_value)
                blocked_result = loop_manager.capture_git_snapshot(repo)
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.SCHEMA)
                connection.execute(
                    """
                    INSERT INTO projects (
                        repo, base_branch, phase, worktree, agent_role,
                        agent_snapshot, agent_result_snapshot
                    ) VALUES (?, 'main', 'BLOCKED', ?, 'worker', ?, ?)
                    """,
                    (
                        str(repo),
                        str(repo),
                        before.serialize(),
                        blocked_result.serialize(),
                    ),
                )
                connection.commit()

                with mock.patch(
                    "loop_manager.validate_repository",
                    return_value=repo,
                ), mock.patch(
                    "loop_manager.migrate_legacy_execplans"
                ) as migration, mock.patch(
                    "loop_manager.git",
                    side_effect=AssertionError("unsafe Git command before config validation"),
                ) as unsafe_git, mock.patch(
                    "loop_manager.run_codex"
                ) as agent:
                    result = loop_manager.run_project(
                        connection,
                        root / "state",
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                project = connection.execute(
                    "SELECT phase, last_error, agent_result_snapshot "
                    "FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(result, 1)
                self.assertEqual(project["phase"], "PROJECT_BLOCKED")
                self.assertIn("범위 선검증 실패", project["last_error"])
                self.assertIn(expected_key, project["last_error"])
                self.assertNotIn(sensitive_value, project["last_error"])
                self.assertEqual(
                    project["agent_result_snapshot"],
                    blocked_result.serialize(),
                )
                migration.assert_not_called()
                unsafe_git.assert_not_called()
                agent.assert_not_called()
