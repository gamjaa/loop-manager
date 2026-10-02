from tests.support.scope import AgentScopeFixture
from tests.support.git import prepare_manager_import_fixture
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, commit_and_push


class ScopeIgnoredArtifactsTest(AgentScopeFixture, unittest.TestCase):
    def test_ignored_untracked_creation_change_and_deletion_are_allowed(self):
        scenarios = (
            ("planner", None, "created by planner\n"),
            ("worker", "before work\n", "changed by worker\n"),
            ("reviewer", "before review\n", "changed by reviewer\n"),
            ("reviewer", "before review\n", None),
        )
        for role_name, before_contents, after_contents in scenarios:
            with self.subTest(role=role_name), tempfile.TemporaryDirectory() as directory:
                repo, _plan_path = self._repository_with_plan(directory)
                (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
                commit_and_push(repo, "Ignore diagnostic fixture")
                ignored = repo / "ignored.txt"
                if before_contents is not None:
                    ignored.write_text(before_contents, encoding="utf-8")
                before = loop_manager.capture_git_snapshot(repo)

                if after_contents is None:
                    ignored.unlink()
                else:
                    ignored.write_text(after_contents, encoding="utf-8")
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotIn("ignored.txt", before.dirty)
                self.assertNotIn("ignored.txt", after.dirty)
                loop_manager.validate_agent_scope(
                    role_name, repo, before, after,
                    selected_plan_path="docs/plans/[G-001] Scope Safety.md"
                    if role_name == "worker" else None,
                )

    def test_legacy_ignored_snapshot_requires_unchanged_tracked_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            (repo / "cache.bin").write_bytes(b"old")
            current = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                current,
                dirty={"cache.bin": (("untracked",), "old-signature")},
                config={
                    key: value for key, value in current.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            normalized = loop_manager._normalize_legacy_ignored_snapshot(
                repo, legacy, current
            )
            self.assertEqual(normalized.dirty, {})

            (repo / ".gitignore").write_text("cache.bin\nnew.bin\n", encoding="utf-8")
            changed = loop_manager.capture_git_snapshot(repo)
            self.assertEqual(
                loop_manager._normalize_legacy_ignored_snapshot(repo, legacy, changed),
                legacy,
            )

    def test_persisted_worker_snapshot_resumes_with_legacy_ignored_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            (repo / "cache.bin").write_bytes(b"old")
            current = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                current,
                dirty={"cache.bin": (("untracked",), "old-signature")},
                config={
                    key: value for key, value in current.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            state_dir = Path(directory) / "state"
            self._persist_interrupted_worker(state_dir, repo, plan_path, legacy)
            connection = sqlite3.connect(state_dir / "loop-manager.db")
            connection.row_factory = sqlite3.Row
            try:
                self.assertEqual(
                    loop_manager.validate_persisted_agent_scope(
                        connection, repo, state_dir, str(repo)
                    ),
                    current,
                )
            finally:
                connection.close()

    def test_legacy_snapshot_does_not_trust_unrecorded_exclude_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                before,
                config={
                    key: value for key, value in before.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            state_dir = Path(directory) / "state"
            self._persist_interrupted_worker(state_dir, repo, plan_path, legacy)
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nnew-cache.bin\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(state_dir / "loop-manager.db")
            connection.row_factory = sqlite3.Row
            try:
                with self.assertRaisesRegex(loop_manager.AgentScopeError, "ignore-source"):
                    loop_manager.validate_persisted_agent_scope(
                        connection, repo, state_dir, str(repo)
                    )
            finally:
                connection.close()

    def test_forced_staged_ignored_file_stays_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / "cache.bin").write_bytes(b"staged")
            git_command(repo, "add", "-f", "cache.bin")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("cache.bin", after.index)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "cache.bin"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_tracked_file_matching_ignore_rule_stays_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("src/app.py\n", encoding="utf-8")
            commit_and_push(repo, "Ignore tracked path fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("src/app.py", after.dirty)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "src/app.py"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_new_ignore_rule_cannot_hide_untracked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("*.cache\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / ".gitignore").write_text("*.cache\nhidden.txt\n", encoding="utf-8")
            (repo / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("hidden.txt", after.dirty)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "hidden.txt"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_colon_in_ignore_rule_path_cannot_hide_untracked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            rule_directory = repo / "rules:old"
            rule_directory.mkdir()
            rule = rule_directory / ".gitignore"
            rule.write_text("*.cache\n", encoding="utf-8")
            git_command(repo, "add", "rules:old/.gitignore")
            commit_and_push(repo, "Add ignore rule with colon in path")
            before = loop_manager.capture_git_snapshot(repo)
            rule.write_text("*.cache\nhidden.txt\n", encoding="utf-8")
            (rule_directory / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("rules:old/hidden.txt", after.dirty)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "hidden.txt"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_repository_exclude_change_cannot_hide_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nhidden.txt\n",
                encoding="utf-8",
            )
            (repo / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertNotIn("hidden.txt", after.dirty)
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "Git config"):
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_planner_import_bytecode_is_ignored_without_hiding_scope_violations(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            prepare_manager_import_fixture(repo)

            planner_environment = loop_manager.agent_environment("codex", "planner")
            import_result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import loop_manager; "
                    "from loop_manager_internal.config import managed_config; "
                    "from loop_manager_internal.maintenance import maintenance_git, maintenance_planning; "
                    "from loop_manager_internal.storage import maintenance_state; "
                    "from loop_manager_internal.runtime import project_registry",
                ],
                cwd=repo,
                env=planner_environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            self.assertTrue(any(repo.rglob("*.pyc")))

            existing_cache = repo / "__pycache__" / "preserved.pyc"
            existing_cache.parent.mkdir(exist_ok=True)
            existing_cache.write_bytes(b"existing cache")
            before = loop_manager.capture_git_snapshot(repo)
            import_result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import loop_manager; "
                    "from loop_manager_internal.config import managed_config; "
                    "from loop_manager_internal.maintenance import maintenance_git, maintenance_planning; "
                    "from loop_manager_internal.storage import maintenance_state; "
                    "from loop_manager_internal.runtime import project_registry",
                ],
                cwd=repo,
                env=planner_environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            self.assertEqual(existing_cache.read_bytes(), b"existing cache")
            write_execplan(repo, "G-002", "Imported Without Bytecode")
            after = loop_manager.capture_git_snapshot(repo)
            loop_manager.validate_agent_scope("planner", repo, before, after)

            explicit_cache = repo / "__pycache__" / "explicit.pyc"
            explicit_cache.write_bytes(b"explicit cache")
            explicit_after = loop_manager.capture_git_snapshot(repo)
            loop_manager.validate_agent_scope("planner", repo, after, explicit_after)
            (repo / "unexpected.txt").write_text("publishable\n", encoding="utf-8")
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "unexpected.txt"):
                loop_manager.validate_agent_scope(
                    "planner", repo, after, loop_manager.capture_git_snapshot(repo)
                )

    def test_codex_turn_diff_refs_are_ignored_for_persisted_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            head = git_command(repo, "rev-parse", "HEAD")
            old_ref = "refs/codex/turn-diffs/captures/old/session/base"
            new_ref = "refs/codex/turn-diffs/captures/new/session/base"
            git_command(repo, "update-ref", old_ref, head)
            before = loop_manager.capture_git_snapshot(repo)

            git_command(repo, "update-ref", "-d", old_ref)
            git_command(repo, "update-ref", new_ref, head)
            after = loop_manager.capture_git_snapshot(repo)

            self.assertIn(old_ref, before.refs)
            self.assertNotIn(old_ref, after.refs)
            self.assertNotIn(new_ref, before.refs)
            self.assertIn(new_ref, after.refs)
            loop_manager.validate_agent_scope(
                "worker",
                repo,
                before,
                after,
                selected_plan_path=plan_path,
            )
