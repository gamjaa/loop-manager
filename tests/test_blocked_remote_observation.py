"""Narrow tests for blocked-plan remote observation and durable input identity."""

from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import blocked_remote_observation as observation
import loop_manager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


class BlockedRemoteObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.remote = root / "remote.git"
        self.repo = root / "repo"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.remote)],
            check=True, capture_output=True,
        )
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "file.txt").write_text("initial\n")
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "push", "origin", "main")
        self.database = root / "state.sqlite"
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(lambda: self.connection.close())
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, origin, registered, phase) "
            "VALUES (?, 'main', ?, 1, 'WORKING')",
            (str(self.repo), str(self.remote)),
        )
        self.connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "content_hash, priority, status, branch, blocked_reason, blocked_fingerprint) "
            "VALUES (?, 'G-001', 'docs/plans/one.md', 'One', '[]', '[]', 'hash', 'normal', "
            "'PLAN_BLOCKED', 'work/one', 'user_decision_required', 'same-input')",
            (str(self.repo),),
        )
        self.connection.commit()

    def observe(self) -> sqlite3.Row:
        project = self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (str(self.repo),)
        ).fetchone()
        loop_manager.observe_blocked_plan_remotes(
            self.connection, self.repo, str(self.repo), project,
        )
        return self.connection.execute(
            "SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()

    def reconnect(self) -> None:
        self.connection.commit()
        self.connection.close()
        self.connection = sqlite3.connect(self.database)
        self.connection.row_factory = sqlite3.Row

    def advance(self, branch: str, value: str) -> str:
        (self.repo / "file.txt").write_text(value)
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-m", value)
        git(self.repo, "push", "origin", f"HEAD:{branch}")
        return git(self.repo, "rev-parse", "HEAD")

    def test_base_branch_and_both_changes_remain_pending_across_restarts(self):
        initial = self.observe()
        self.assertEqual((initial["base_result"], initial["branch_result"]), ("present", "absent"))
        self.assertTrue(observation.pending_input(initial))
        self.assertTrue(observation.mark_processed(
            self.connection, str(self.repo), "G-001", observation.input_identity(initial),
        ))
        self.reconnect()
        restarted = self.observe()
        self.assertEqual(restarted["confirmed_base_oid"], initial["base_oid"])
        self.assertEqual(restarted["processed_input"], observation.input_identity(initial))
        self.assertFalse(observation.pending_input(restarted))

        base_oid = self.advance("main", "base\n")
        base_only = self.observe()
        self.assertEqual(base_only["base_oid"], base_oid)
        self.assertTrue(observation.pending_input(base_only))
        self.assertEqual(base_only["blocked_fingerprint"], "same-input")
        self.reconnect()
        saved_base = self.connection.execute(
            "SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = 'G-001'",
            (str(self.repo),),
        ).fetchone()
        self.assertEqual(saved_base["base_oid"], base_oid)
        self.assertEqual(saved_base["processed_input"], observation.input_identity(initial))
        self.assertTrue(observation.pending_input(saved_base))
        git(self.repo, "switch", "-c", "work/one")
        branch_oid = self.advance("work/one", "branch\n")
        both = self.observe()
        self.assertEqual((both["base_oid"], both["branch_oid"]), (base_oid, branch_oid))
        self.assertTrue(observation.pending_input(both))
        self.assertFalse(observation.mark_processed(
            self.connection, str(self.repo), "G-001", observation.input_identity(base_only),
        ))
        self.assertEqual(self.observe()["processed_input"], observation.input_identity(initial))

    def test_failure_and_deletion_preserve_confirmed_oids_and_execution_slot(self):
        self.advance("work/one", "branch\n")
        confirmed = self.observe()
        self.connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "content_hash, priority, status) VALUES (?, 'G-002', 'two.md', 'Two', '[]', '[]', "
            "'hash', 'normal', 'IMPLEMENTING')", (str(self.repo),),
        )
        git(self.repo, "push", "origin", "--delete", "work/one")
        deleted = self.observe()
        self.assertEqual(deleted["branch_result"], "absent")
        self.assertIsNone(deleted["branch_oid"])
        self.assertEqual(deleted["confirmed_branch_oid"], confirmed["branch_oid"])
        real_run = subprocess.run
        def offline_remote(command, **kwargs):
            if "ls-remote" in command:
                raise OSError("offline")
            return real_run(command, **kwargs)
        with mock.patch.object(observation.subprocess, "run", side_effect=offline_remote):
            failed = self.observe()
        self.assertEqual((failed["base_result"], failed["branch_result"]), ("error", "error"))
        self.assertEqual(failed["base_error"], "os-error-unknown")
        self.assertEqual(failed["branch_error"], "os-error-unknown")
        self.assertEqual(failed["confirmed_base_oid"], confirmed["base_oid"])
        self.assertEqual(failed["confirmed_branch_oid"], confirmed["branch_oid"])
        statuses = self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ? ORDER BY plan_id", (str(self.repo),)
        ).fetchall()
        self.assertEqual([row[0] for row in statuses], ["PLAN_BLOCKED", "IMPLEMENTING"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_missing_or_mismatched_registered_origin_preserves_url_without_confirming_oid(self):
        confirmed = self.observe()
        self.connection.execute(
            "UPDATE projects SET origin = NULL WHERE repo = ?", (str(self.repo),)
        )
        missing = self.observe()
        self.assertIsNone(missing["registered_origin"])
        self.assertEqual(missing["origin_url"], str(self.remote))
        self.assertEqual(missing["base_error"], "registered-origin-missing")
        self.assertIsNone(missing["base_oid"])
        self.assertEqual(missing["confirmed_base_oid"], confirmed["base_oid"])

        self.connection.execute(
            "UPDATE projects SET origin = 'another/repo' WHERE repo = ?",
            (str(self.repo),),
        )
        mismatch = self.observe()
        self.assertEqual(mismatch["registered_origin"], "another/repo")
        self.assertEqual(mismatch["origin_url"], str(self.remote))
        self.assertEqual(mismatch["base_error"], "registered-origin-mismatch")
        self.assertIsNone(mismatch["base_oid"])
        self.assertEqual(mismatch["confirmed_base_oid"], confirmed["base_oid"])
        self.assertNotEqual(observation.input_identity(missing), observation.input_identity(mismatch))

    def test_work_branch_only_change_and_project_linked_plan(self):
        initial = self.observe()
        self.assertTrue(observation.mark_processed(
            self.connection, str(self.repo), "G-001", observation.input_identity(initial),
        ))
        base_oid = initial["base_oid"]
        branch_oid = self.advance("work/one", "branch only\n")
        changed = self.observe()
        self.assertEqual(changed["base_oid"], base_oid)
        self.assertEqual(changed["branch_oid"], branch_oid)
        self.assertTrue(observation.pending_input(changed))
        self.connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' WHERE repo = ?",
            (str(self.repo),),
        )
        self.connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence, release_fingerprint) "
            "VALUES (?, 'project', 'repository_state_unsafe', 'G-001', 'unsafe', 'first')",
            (str(self.repo),),
        )
        linked = self.observe()
        self.assertEqual(linked["branch_oid"], branch_oid)
        self.assertIsNone(linked["blocked_reason"])
        self.assertEqual(linked["project_blocked_reason"], "repository_state_unsafe")
        self.assertEqual(linked["project_blocked_fingerprint"], "first")
        self.assertTrue(observation.mark_processed(
            self.connection, str(self.repo), "G-001", observation.input_identity(linked),
        ))
        self.connection.execute(
            "UPDATE project_blocks SET release_fingerprint = 'second' WHERE repo = ?",
            (str(self.repo),),
        )
        changed_project_input = self.observe()
        self.assertEqual(changed_project_input["base_oid"], linked["base_oid"])
        self.assertEqual(changed_project_input["branch_oid"], linked["branch_oid"])
        self.assertTrue(observation.pending_input(changed_project_input))
        next_oid = self.advance("work/one", "next branch commit\n")
        with (
            mock.patch.object(loop_manager, "_self_runtime_project", return_value=None),
            mock.patch.object(loop_manager, "synchronize_waiting_pull_requests"),
            mock.patch.object(loop_manager, "resume_project_block_if_safe"),
        ):
            result = loop_manager._run_project_step(
                self.connection, self.repo.parent, str(self.repo),
                self.repo.parent / "unused.ini", None, False, registered_only=True,
            )
        self.assertEqual(result, 1)
        observed = self.connection.execute(
            "SELECT branch_oid FROM blocked_remote_observations "
            "WHERE repo = ? AND plan_id = 'G-001'", (str(self.repo),)
        ).fetchone()
        self.assertEqual(observed[0], next_oid)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE repo = ?", (str(self.repo),)
        ).fetchone()[0], "IMPLEMENTING")

    def test_local_work_change_updates_input_without_touching_default_worktree(self):
        worktree = self.repo.parent / "plan-worktree"
        git(self.repo, "worktree", "add", "-b", "work/one", str(worktree))
        self.connection.execute(
            "UPDATE exec_plans SET worktree = ? WHERE repo = ?",
            (str(worktree), str(self.repo)),
        )
        before = self.observe()
        self.assertTrue(observation.mark_processed(
            self.connection, str(self.repo), "G-001", observation.input_identity(before),
        ))
        (worktree / "file.txt").write_text("local edit\n")
        after = self.observe()
        self.assertNotEqual(after["worktree_fingerprint"], before["worktree_fingerprint"])
        self.assertTrue(observation.pending_input(after))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_remote_failure_cause_changes_are_persisted_as_new_input(self):
        for diagnostic, cause in (
            ("fatal: Authentication failed", "authentication"),
            ("fatal: Could not resolve host", "network"),
        ):
            with self.subTest(cause=cause), mock.patch.object(
                observation.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 128, "", diagnostic),
            ):
                base, branch = observation.observe_refs(
                    self.repo, str(self.remote),
                    ("refs/heads/main", "refs/heads/work/one"),
                )
            observation.record(
                self.connection, repo=str(self.repo), plan_id="G-001",
                registered_origin=str(self.remote), origin_url=str(self.remote),
                base_ref="refs/heads/main",
                branch_ref="refs/heads/work/one", base=base, branch=branch,
                blocked_reason="user_decision_required", blocked_fingerprint="same-input",
                project_blocked_reason=None, project_blocked_fingerprint=None,
                worktree_fingerprint="",
            )
            row = self.connection.execute(
                "SELECT * FROM blocked_remote_observations WHERE repo = ?",
                (str(self.repo),),
            ).fetchone()
            self.assertEqual((row["base_error"], row["branch_error"]), (cause, cause))
            self.assertTrue(observation.pending_input(row))
            self.assertTrue(observation.mark_processed(
                self.connection, str(self.repo), "G-001", observation.input_identity(row),
            ))

    def test_plan_and_project_block_inputs_remain_distinct(self):
        self.connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence, release_fingerprint) "
            "VALUES (?, 'G-001', 'plan', 'user_decision_required', 'decision', 'plan-input')",
            (str(self.repo),),
        )
        self.connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence, release_fingerprint) "
            "VALUES (?, 'project', 'repository_state_unsafe', 'G-001', 'unsafe', 'project-input')",
            (str(self.repo),),
        )
        row = self.observe()
        self.assertEqual(row["blocked_reason"], "user_decision_required")
        self.assertEqual(row["blocked_fingerprint"], "plan-input")
        self.assertEqual(row["project_blocked_reason"], "repository_state_unsafe")
        self.assertEqual(row["project_blocked_fingerprint"], "project-input")

    def test_existing_observation_table_adds_failure_and_project_columns(self):
        self.connection.execute("DROP TABLE blocked_remote_observations")
        self.connection.execute(
            "CREATE TABLE blocked_remote_observations "
            "(repo TEXT, plan_id TEXT, base_result TEXT, branch_result TEXT)"
        )
        observation.ensure_schema(self.connection)
        columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(blocked_remote_observations)"
            )
        }
        self.assertTrue({
            "registered_origin", "base_error", "branch_error", "project_blocked_reason",
            "project_blocked_fingerprint",
        } <= columns)


if __name__ == "__main__":
    unittest.main()
