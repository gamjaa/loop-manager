from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, commit_and_push, advance_remote_file
from tests.support.worker import isolated_scope_resources
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class ScopeCheckpointRecoveryTest(AgentScopeFixture, unittest.TestCase):
    def test_blocked_checkpoint_allows_manager_cleanup_and_later_resume(self):
        origin_parser = mock.patch(
            "loop_manager_internal.git.git_adapter.github_repository",
            return_value="owner/repo",
        )
        origin_parser.start()
        self.addCleanup(origin_parser.stop)
        manager_origin_parser = mock.patch.object(
            loop_manager, "github_repository", return_value="owner/repo")
        manager_origin_parser.start()
        self.addCleanup(manager_origin_parser.stop)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            cleanup_plan = write_execplan(repo, 'G-099', 'Cleanup')
            cleanup_plan_path = cleanup_plan.relative_to(repo).as_posix()
            commit_and_push(repo, 'Add merged cleanup plan fixture')
            state_dir = root / "state"
            state_dir.mkdir()
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-001"
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            cleanup_branch, cleanup_worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-099"
            )
            loop_manager.make_worktree(
                repo,
                cleanup_worktree,
                cleanup_branch,
                "main",
            )

            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch, origin) VALUES (?, 'main', 'owner/repo')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path)),
                 (cleanup_plan_path, loop_manager._parse_execplan(repo, cleanup_plan))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            self._confirm_general_plan_input(connection, repo, plan_sha)
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            input_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            before = loop_manager.capture_git_snapshot(worktree)
            (worktree / "reviewer.txt").write_text(
                "preserve violation\n",
                encoding="utf-8",
            )
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            # Deferred cleanup belongs to an already merged, owned plan.
            connection.execute(
                "UPDATE exec_plans SET status = 'MERGED', branch = ?, worktree = ?, "
                "workspace_owned = 1, pr_head_oid = ? WHERE repo = ? AND plan_id = 'G-099'",
                (cleanup_branch, str(cleanup_worktree), plan_sha, str(repo)))
            loop_manager._ensure_cleanup_pending_schema(connection)
            loop_manager._store_cleanup_pending(
                connection,
                str(repo),
                cleanup_worktree,
                "https://example.test/pr/99",
                plan_sha,
                "pending cleanup",
            )
            connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', active_sha=?, branch=?, "
                "worktree=?, workspace_owned=1, agent_role='reviewer', agent_snapshot=?, agent_result_snapshot=? "
                "WHERE plan_id='G-001'", (plan_sha, branch, str(worktree), before.serialize(), blocked_result.serialize()))
            connection.commit()
            runtime = loop_manager.runtime_state.RuntimeStateRepository(connection)
            runtime.block_plan(str(repo), 'G-001', reason_code='managed_worktree_scope_violation',
                               evidence='legacy Git comparison', release_fingerprint='same input')
            self.assertEqual(loop_manager.recheck_cleanup_pending(connection, repo, str(repo), state_dir), [])
            self.assertFalse(cleanup_worktree.exists())
            block = loop_manager.scoped_blocking.plan_block(connection, str(repo), 'G-001')
            self.assertTrue(loop_manager._reconcile_scope_baseline_plan_block(
                connection, repo, state_dir, str(repo), block))
            self.assertTrue((worktree / 'reviewer.txt').is_file())
            self.assertIsNone(loop_manager.scoped_blocking.plan_block(connection, str(repo), 'G-001'))
            self.assertEqual(connection.execute("SELECT agent_snapshot FROM exec_plans WHERE plan_id='G-001'").fetchone()[0],
                             before.serialize())
