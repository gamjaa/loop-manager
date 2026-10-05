import contextlib
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage import planning_input_state
from tests.support.worker import dummy_worker_sessions


class PersistedMergeProjectQueryTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, phase, plan_path, branch, pr_url) "
            "VALUES ('/repo', 'main', 'PUBLISHING', 'docs/plans/[G-001] Original.md', "
            "'loop-manager/G-001', 'https://example.test/pr/1')"
        )
        self.insert_plan("G-001", "MERGED")

    def insert_plan(self, plan_id, status, *, repo="/repo"):
        self.connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "priority, content_hash, status, branch, pr_url) "
            "VALUES (?, ?, ?, 'Original', '[\"docs/PRODUCT.md\"]', '[]', "
            "'normal', 'hash', ?, ?, ?)",
            (repo, plan_id, f"docs/plans/[{plan_id}] Original.md", status,
             f"loop-manager/{plan_id}", f"https://example.test/pr/{plan_id}"),
        )
        self.connection.commit()

    def run_step(self, *, synchronize=None, real_lifecycle=False):
        reconciled = SimpleNamespace(
            kind=loop_manager.execplan_catalog_state.ReconciliationKind.RECONCILED,
            revision_sha="a" * 40,
        )
        with contextlib.ExitStack() as stack:
            for name in (
                "observe_blocked_plan_remotes", "backfill_plan_shas",
                "migrate_legacy_execplans", "resume_plan_blocks", "_recorded_base_fetch",
            ):
                stack.enter_context(mock.patch.object(loop_manager, name))
            stack.enter_context(mock.patch.object(loop_manager, "validate_repository", return_value=Path("/repo")))
            stack.enter_context(mock.patch.object(loop_manager, "remote_base", return_value="main"))
            stack.enter_context(mock.patch.object(loop_manager, "git", return_value=SimpleNamespace(stdout="a" * 40)))
            stack.enter_context(mock.patch.object(loop_manager, "recheck_cleanup_pending", return_value=[]))
            stack.enter_context(mock.patch.object(loop_manager, "synchronize_waiting_pull_requests",
                side_effect=synchronize, return_value=SimpleNamespace(step_result=0)))
            stack.enter_context(mock.patch.object(loop_manager, "reconcile_execplan_catalog", return_value=reconciled))
            planning = None
            if real_lifecycle:
                stack.enter_context(mock.patch.object(loop_manager, "git_bytes", return_value=b""))
                stack.enter_context(mock.patch.object(loop_manager, "_planning_input_fingerprint", return_value="stable"))
                stack.enter_context(mock.patch.object(loop_manager, "_specification_input_fingerprint", return_value="spec"))
                stack.enter_context(mock.patch.object(loop_manager, "plan_declarations", return_value={}))
                stack.enter_context(mock.patch.object(loop_manager, "is_loop_manager_repository", return_value=False))
            else:
                planning = stack.enter_context(mock.patch.object(
                    loop_manager.PlanningLifecycleCoordinator, "run",
                    return_value=SimpleNamespace(kind=loop_manager.PlanningOutcomeKind.WAITING),
                ))
            result = loop_manager._run_project_step(
                self.connection, Path("/state"), "/repo", Path("unused.ini"), "main", False,
            )
        self.assertEqual(result, 0)
        if planning is not None:
            planning.assert_called_once()

    @dummy_worker_sessions
    def test_stale_merged_projection_skips_pr_query_after_plan_rename(self):
        self.connection.execute(
            "UPDATE exec_plans SET path = 'docs/plans/archive/[G-001] Renamed.md' "
            "WHERE repo = '/repo' AND plan_id = 'G-001'"
        )
        with mock.patch.object(loop_manager, "pr_for_branch", side_effect=AssertionError("PR query")) as pr:
            for phase in ("PUBLISHING", "WORKING", "VERIFYING", "REVIEWING"):
                with self.subTest(phase=phase):
                    self.connection.execute(
                        "UPDATE projects SET phase = ?, plan_path = 'docs/plans/[G-001] Original.md', "
                        "branch = 'loop-manager/G-001', pr_url = 'https://example.test/pr/1'", (phase,),
                    )
                    self.run_step()
                    projection = self.connection.execute("SELECT phase, plan_path, branch FROM projects").fetchone()
                    self.assertEqual(tuple(projection), (None, None, None))
                    self.assertEqual(self.connection.execute(
                        "SELECT status FROM exec_plans WHERE repo = '/repo' AND plan_id = 'G-001'"
                    ).fetchone()[0], "MERGED")
            pr.assert_not_called()

    @dummy_worker_sessions
    def test_same_step_merge_with_open_plan_queries_only_unconfirmed_pr(self):
        self.connection.execute("UPDATE exec_plans SET status = 'PR_OPEN' WHERE plan_id = 'G-001'")
        self.insert_plan("G-002", "PR_OPEN")

        def synchronize(connection, _repo_path, _state_dir, repo, **_kwargs):
            connection.execute(
                "UPDATE exec_plans SET status = 'MERGED' WHERE repo = ? AND plan_id = 'G-001'", (repo,),
            )
            connection.commit()
            # The first observation has committed G-001's merge. The remaining
            # unconfirmed plan still requires its normal observation query.
            loop_manager.pr_for_branch(Path(repo), "loop-manager/G-002")
            return SimpleNamespace(step_result=0)

        with mock.patch.object(loop_manager, "pr_for_branch", return_value={"state": "OPEN"}) as pr:
            self.run_step(synchronize=synchronize)
            pr.assert_called_once_with(Path("/repo"), "loop-manager/G-002")
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-002'"
        ).fetchone()[0], "PR_OPEN")

    @dummy_worker_sessions
    def test_unconfirmed_project_keeps_query_despite_other_repository_merge(self):
        self.connection.execute("UPDATE exec_plans SET status = 'PR_OPEN' WHERE repo = '/repo'")
        self.insert_plan("G-001", "MERGED", repo="/other")
        with mock.patch.object(loop_manager, "pr_for_branch", return_value=None) as pr:
            self.run_step()
            pr.assert_called_once_with(Path("/repo"), "loop-manager/G-001", "https://example.test/pr/1")

    def stable_planning_input(self):
        self.connection.execute(
            "INSERT INTO execplan_catalog_revisions (repo, revision_sha, catalog_fingerprint) "
            "VALUES ('/repo', ?, 'catalog')", ("a" * 40,),
        )
        self.connection.execute(
            "UPDATE projects SET planning_fingerprint = 'stable', "
            "specification_fingerprint = 'spec', processed_sha = ?", ("a" * 40,),
        )
        planning_input_state.record_general(self.connection, "/repo", "a" * 40, {})
        self.connection.commit()

    @dummy_worker_sessions
    def test_merged_projection_allows_actual_planning_to_complete(self):
        self.stable_planning_input()
        self.connection.execute(
            "UPDATE exec_plans SET worktree = '/preserved/worktree', "
            "worker_session_id = 'saved-worker', reviewer_session_id = 'saved-reviewer'"
        )
        loop_manager._ensure_execplans_schema(self.connection)
        before = tuple(self.connection.execute("SELECT * FROM exec_plans").fetchone())
        with (
            mock.patch.object(loop_manager, "pr_for_branch", side_effect=AssertionError("PR query")) as pr,
            mock.patch.object(loop_manager.ExecutionPhaseCoordinator, "prepare", side_effect=AssertionError("unexpected execution")) as prepare,
        ):
            self.run_step(real_lifecycle=True)
            # A subsequent step observes the settled state as well.
            self.run_step(real_lifecycle=True)
        pr.assert_not_called()
        prepare.assert_not_called()
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM exec_plans").fetchone()), before)
        project = self.connection.execute("SELECT phase, branch, plan_path, last_error FROM projects").fetchone()
        self.assertEqual(tuple(project), (None, None, None, None))
        self.assertIsNone(scoped_blocking.project_block(self.connection, "/repo"))

    @dummy_worker_sessions
    def test_merged_projection_allows_ready_activation_and_actual_execution_prepare(self):
        self.stable_planning_input()
        self.insert_plan("G-002", "READY")
        self.connection.execute("UPDATE exec_plans SET depends_on = '[\"G-001\"]' WHERE plan_id = 'G-002'")
        with (
            mock.patch.object(loop_manager, "pr_for_branch", side_effect=AssertionError("PR query")) as pr,
            mock.patch.object(loop_manager, "make_worktree") as workspace,
            mock.patch.object(loop_manager.execution_handlers.ExecutionHandlers, "recover", autospec=True,
                side_effect=lambda _handler, context: context),
            mock.patch.object(loop_manager.ExecutionPhaseCoordinator, "dispatch", autospec=True,
                return_value=loop_manager.ExecutionOutcome(loop_manager.ExecutionOutcomeKind.SUCCESS)) as dispatch,
        ):
            self.run_step(real_lifecycle=True)
        pr.assert_not_called()
        workspace.assert_called_once()
        dispatch.assert_called_once()
        context = dispatch.call_args.args[1]
        self.assertEqual(context.plan.plan_id, "G-002")
        self.assertEqual(context.phase.value, "IMPLEMENTING")
        project = self.connection.execute("SELECT phase, plan_path, last_error FROM projects").fetchone()
        self.assertEqual(tuple(project), ("WORKING", "docs/plans/[G-002] Original.md", None))
        self.assertEqual(self.connection.execute("SELECT status FROM exec_plans WHERE plan_id = 'G-001'").fetchone()[0], "MERGED")
        self.assertIsNone(scoped_blocking.project_block(self.connection, "/repo"))

    @dummy_worker_sessions
    def test_preserved_planning_attempt_keeps_merged_projection_and_sessions(self):
        self.connection.execute(
            "UPDATE projects SET planning_error = 'saved planning error', "
            "planning_error_fingerprint = 'saved-input', planner_session_id = 'saved-planner', "
            "planning_workspace_owned = 1, worktree = '/preserved/planning'"
        )
        before = tuple(self.connection.execute("SELECT * FROM projects").fetchone())
        with mock.patch.object(loop_manager, "pr_for_branch", side_effect=AssertionError("PR query")) as pr:
            self.run_step()
        pr.assert_not_called()
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM projects").fetchone()), before)
