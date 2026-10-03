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


class ScopeCheckpointRecoveryTest(AgentScopeFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_blocked_checkpoint_allows_manager_cleanup_and_later_resume(self):
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
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
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
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'BLOCKED', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        blocked_fingerprint = ?,
                        blocked_worktree_fingerprint = ?,
                        last_error = 'scope blocked'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        input_fingerprint,
                        input_fingerprint,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'BLOCKED',
                        branch = ?, worktree = ?, plan_path = ?,
                        last_error = 'scope blocked', agent_role = 'reviewer',
                        agent_snapshot = ?, agent_input_fingerprint = ?,
                        agent_result_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        input_fingerprint,
                        blocked_result.serialize(),
                        str(repo),
                    ),
                )

            common_patches = (
                mock.patch("loop_manager.validate_repository", return_value=repo),
                mock.patch("loop_manager.remote_base", return_value="main"),
                mock.patch("loop_manager.ensure_safe_base_worktree"),
                mock.patch("loop_manager.pr_for_branch", return_value=None),
            )
            with common_patches[0], common_patches[1], common_patches[2], common_patches[3], mock.patch(
                "loop_manager.run_codex"
            ) as blocked_agent:
                blocked_run = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked_project = connection.execute(
                "SELECT phase, last_error, agent_manager_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            blocked_plan = connection.execute(
                "SELECT status, last_error, agent_snapshot "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(blocked_run, 0)
            self.assertIsNone(blocked_project["phase"])
            self.assertEqual(blocked_plan["status"], "PLAN_BLOCKED")
            self.assertEqual(blocked_plan["last_error"], "scope blocked")
            self.assertIsNotNone(blocked_plan["agent_snapshot"])
            self.assertFalse(cleanup_worktree.exists())
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM cleanup_pending WHERE repo = ?",
                    (str(repo),),
                ).fetchone()[0],
                0,
            )
            blocked_agent.assert_not_called()

            isolation = mock.patch.object(loop_manager, '_agent_git_resources', side_effect=isolated_scope_resources)
            isolation.start()
            self.addCleanup(isolation.stop)
            advance_remote_file(directory, repo, 'docs/PRODUCT.md', '# Product changed after cleanup\n', 'Change source after blocked cleanup')
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=[
                    loop_manager.CodexResult(0, "", "planner-session"),
                    loop_manager.CodexResult(
                        1, "stop after cleanup recovery", "worker-session"
                    ),
                ],
            ) as resumed_agent:
                resumed_run = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed_project = connection.execute(
                "SELECT phase, last_error, agent_snapshot, agent_manager_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, blocked_fingerprint, blocked_worktree_fingerprint "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed_run, 1)
            self.assertEqual(resumed_project["phase"], "WORKING")
            self.assertEqual(
                resumed_project["last_error"],
                "stop after cleanup recovery",
            )
            self.assertIsNone(resumed_project["agent_snapshot"])
            self.assertIsNone(resumed_project["agent_manager_snapshot"])
            self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
            self.assertIsNone(resumed_plan["blocked_fingerprint"])
            self.assertIsNone(resumed_plan["blocked_worktree_fingerprint"])
            self.assertEqual(
                [call.args[3] for call in resumed_agent.call_args_list],
                ["planner", "worker"],
            )

    def test_blocked_resume_does_not_excuse_agent_owned_ref_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            git_command(repo, "branch", "loop-manager/G-099", "HEAD")
            worktree = root / "managed"
            loop_manager.make_worktree(
                repo,
                worktree,
                "loop-manager/G-001",
                "main",
            )
            before = loop_manager.capture_git_snapshot(worktree)
            git_command(
                worktree,
                "update-ref",
                "refs/heads/loop-manager/G-099",
                "HEAD^",
            )
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, agent_role, agent_snapshot,
                    agent_result_snapshot
                ) VALUES (?, 'main', 'reviewer', ?, ?)
                """,
                (
                    str(repo),
                    before.serialize(),
                    blocked_result.serialize(),
                ),
            )
            connection.commit()

            (repo / "docs/PRODUCT.md").write_text(
                "# Product changed after blocked review\n",
                encoding="utf-8",
            )
            commit_and_push(repo, "Change source after base ref mutation")

            with self.assertRaisesRegex(
                loop_manager.AgentScopeError,
                r"refs/heads/loop-manager/G-099",
            ):
                loop_manager.validate_blocked_agent_scope(
                    connection,
                    repo,
                    str(repo),
                    "main",
                    worktree,
                    plan_path,
                )
