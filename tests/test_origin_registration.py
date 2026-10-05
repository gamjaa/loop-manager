from contextlib import closing
from tests.support.workspace import symlink_or_skip
import contextlib
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.storage import agent_invocation_state
from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence, inspect_worktree


from tests.support.origin import (
    git, configure_identity, commit_all, write_registration_files,
    remote_fixture, advance_remote, OriginScopeFixture,
)
from loop_manager_internal.runtime import bootstrap


class OriginRegistrationTests(unittest.TestCase):
    def test_registry_schema_does_not_commit_caller_transaction(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT, provider TEXT)"
        )
        connection.execute("INSERT INTO projects VALUES ('repo', 'main', 'codex')")
        loop_manager.project_registry.ensure_registry_schema(connection)
        self.assertTrue(connection.in_transaction)
        self.assertIn("origin", {
            row[1] for row in connection.execute("PRAGMA table_info(projects)")
        })
        connection.rollback()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
        self.assertNotIn("origin", {
            row[1] for row in connection.execute("PRAGMA table_info(projects)")
        })

    def test_self_runtime_identity_rejects_alias_and_other_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, other_clone = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            runtime_path = state_dir / "runtime"
            selected = git(source, "rev-parse", "HEAD")
            git(source, "worktree", "add", "--detach", str(runtime_path), selected)
            with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
                connection.execute(loop_manager.SCHEMA)
                connection.execute(
                    "INSERT INTO projects (repo, base_branch, origin) VALUES (?, ?, ?)",
                    (str(source), "develop", "owner/repo"),
                )
            runtime = loop_manager.RuntimeMutation(
                source, runtime_path, selected, "a" * 32,
            )
            alias = Path(directory) / "source-alias"
            symlink_or_skip(alias, source, target_is_directory=True)
            project = lambda path, origin="owner/repo": loop_manager.project_registry.RegisteredProject(
                str(path), "develop", "codex", None, False, origin,
            )
            with mock.patch.object(loop_manager, "ROOT", runtime_path), mock.patch(
                "loop_manager.github_repository", return_value="owner/repo"
            ):
                self.assertTrue(
                    loop_manager._registered_self_runtime_project(
                        project(source), runtime, state_dir,
                    )
                )
                for path in (other_clone, alias, source / "subdir", runtime_path):
                    self.assertFalse(
                        loop_manager._registered_self_runtime_project(
                            project(path), runtime, state_dir,
                        )
                    )
                with self.assertRaisesRegex(RuntimeError, "origin"):
                    loop_manager._registered_self_runtime_project(
                        project(source, "another/repo"), runtime, state_dir,
                    )
                self.assertEqual(
                    loop_manager._bootstrap_service().bootstrap_base_branch(
                        source, state_dir, "owner/repo",
                    ),
                    "develop",
                )
                with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
                    connection.execute(
                        "UPDATE projects SET repo = ?", (str(alias),)
                    )
                self.assertEqual(
                    loop_manager._bootstrap_service().bootstrap_base_branch(
                        source, state_dir, "owner/repo",
                    ),
                    "main",
                )


    def test_registration_reads_only_the_selected_revision_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-b", "main")
            configure_identity(repo)
            write_registration_files(repo)
            committed_agents = commit_all(repo, "Valid registration")

            (repo / "AGENTS.md").write_text("# local-only mismatch\n", encoding="utf-8")
            (repo / "docs/PLANS.md").unlink()
            agents_hash, approved = loop_manager.inspect_registration_files(
                repo,
                committed_agents,
                approve_agents_mismatch=False,
            )
            self.assertFalse(approved)
            self.assertEqual(
                agents_hash,
                loop_manager.file_hash(loop_manager.ROOT / "AGENTS.md"),
            )

            def validate(_path, **kwargs):
                kwargs["origin_result"].append("example/project")
                return repo

            with mock.patch(
                "loop_manager.validate_repository", side_effect=validate
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.git",
                return_value=subprocess.CompletedProcess(
                    (), 0, stdout=committed_agents + "\n"
                ),
            ) as revision_git, mock.patch(
                "loop_manager.inspect_registration_files",
                return_value=("agents-hash", False),
            ) as inspect_files:
                inspection = loop_manager.inspect_project_registration(
                    repo,
                    config_path=Path("config.ini"),
                    provider="codex",
                    requested_base=None,
                    approve_agents_mismatch=False,
                )
            revision_git.assert_called_once_with(
                repo,
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main^{commit}",
            )
            inspect_files.assert_called_once_with(
                repo,
                committed_agents,
                approve_agents_mismatch=False,
            )
            self.assertEqual(inspection.origin, "example/project")

            (repo / "AGENTS.md").write_bytes(
                (loop_manager.ROOT / "AGENTS.md").read_bytes()
            )
            (repo / "docs/PLANS.md").write_text("remote-invalid\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-m", "Invalid remote plans")
            (repo / "docs/PLANS.md").write_bytes(
                (loop_manager.ROOT / "docs/PLANS.md").read_bytes()
            )
            with self.assertRaisesRegex(RuntimeError, "PLANS.md"):
                loop_manager.inspect_registration_files(
                    repo,
                    "HEAD",
                    approve_agents_mismatch=True,
                )
