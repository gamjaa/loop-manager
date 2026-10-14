from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture, snapshot
from dataclasses import replace
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class ScopeConfigurationTest(AgentScopeFixture, unittest.TestCase):

    def test_git_config_changes_are_audit_only_without_leaking_values(self):
        for name in ('remote-url', 'hooks-path'):
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
                loop_manager.validate_agent_scope(
                    "worker", repo, before, after, selected_plan_path=plan_path,
                )

    def test_current_config_drift_does_not_restore_or_compare_saved_agent_state(self):
        from tests.support.database import runtime_connection
        from loop_manager_internal.agents import scope_recovery
        current = snapshot(config={'local:core.hooksPath': ('changed-digest',)})
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE projects SET worktree='/managed', agent_snapshot=?",
                           (snapshot().serialize(),))
        effects = replace(loop_manager._scope_recovery_effects(),
            capture_git_snapshot=mock.Mock(return_value=current),
            git=mock.Mock(side_effect=AssertionError('Git access comparison')))
        with mock.patch.object(Path, 'is_dir', return_value=True):
            self.assertEqual(scope_recovery.validate_persisted_agent_scope(
                connection, Path('/source'), Path('/state'), 'repo', effects=effects), current)
        effects.git.assert_not_called()
        self.assertEqual(connection.execute('SELECT agent_snapshot FROM projects').fetchone()[0],
                         snapshot().serialize())
