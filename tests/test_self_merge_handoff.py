from tests.support.git import isolated_git_test
from tests.support.pr_recovery import PrRecoveryFixture
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.application import self_runtime
from loop_manager_internal.application.contracts import WaitingPullRequestObservation
from loop_manager_internal.runtime.project_registry import RegisteredProject
from types import SimpleNamespace
from tests.support.git import git_command
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class SelfMergeHandoffTest(PrRecoveryFixture, unittest.TestCase):
    def test_local_sync_status_never_gates_verified_self_merge_handoff(self):
        project = RegisteredProject('/source', 'develop', 'codex', None, False)
        old, remote = 'a' * 40, 'b' * 40
        runtime = loop_manager.RuntimeMutation(Path('/source'), Path('/runtime'), old, 'c' * 32)
        observation = WaitingPullRequestObservation((), True, ('https://example.test/pr/1',))
        for status in ('updated', 'skipped', 'failed'):
            with self.subTest(status=status):
                sync = loop_manager.BaseWorktreeSyncResult(status, 'local checkout preserved', remote)
                effects = self_runtime.FinishSelfMergeHandoffEffects(
                    _registered_self_runtime_project=mock.Mock(return_value=True),
                    ensure_safe_base_worktree=mock.Mock(return_value=sync),
                    git=mock.Mock(side_effect=[SimpleNamespace(stdout=remote), SimpleNamespace(stdout=remote), SimpleNamespace(returncode=0)]),
                    log_base_worktree_sync=mock.Mock(), operation_log=mock.Mock(),
                )
                with mock.patch.object(self_runtime.scoped_blocking, 'project_block', return_value=None):
                    with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                        self_runtime._finish_self_merge_handoff(effects, None, project, Path('/state'), runtime, observation)
                effects.ensure_safe_base_worktree.assert_called_once_with(
                    Path('/source'), 'develop', remote_sha=remote, state_dir=Path('/state'))
                effects.git.assert_called_with(Path('/source'), 'merge-base', '--is-ancestor', old, remote, check=False)
                self.assertIn('source_sync=' + status, effects.operation_log.call_args.args[0])

    def test_self_merge_handoff_persists_merge_before_parallel_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            old_sha = git_command(repo, "rev-parse", "HEAD")
            runtime_path = state_dir / "runtime"
            git_command(repo, "worktree", "add", "--detach", str(runtime_path), old_sha)
            git_command(worktree, "push", "origin", "HEAD:main")
            other = str((Path(directory) / "other").resolve())
            with connection:
                connection.execute(
                    "INSERT INTO projects (repo, base_branch, worker_session_id, "
                    "worktree, planning_retry_count) "
                    "VALUES (?, 'main', 'peer-session', '/peer-worktree', 2)",
                    (other,),
                )
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            runtime = loop_manager.RuntimeMutation(
                repo, runtime_path, old_sha, "a" * 32,
            )
            config = loop_manager.ManagedConfig(
                path=Path(directory) / "config.ini", max_workers=2,
                telegram_token="test-token", telegram_chat_id="test-chat",
            )
            try:
                with mock.patch.object(loop_manager, "ROOT", runtime_path), mock.patch(
                    "loop_manager.github_repository", return_value="owner/repo"
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=merged
                ), mock.patch("loop_manager.run_project_result") as run_project:
                    with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                        loop_manager.schedule_registered_projects(
                            connection, state_dir, config, runtime_mutation=runtime,
                        )
                run_project.assert_not_called()
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                        (str(repo),),
                    ).fetchone()["status"],
                    "MERGED",
                )
                self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)
                self.assertEqual(
                    connection.execute(
                        "SELECT enabled FROM notification_session"
                    ).fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT last_error FROM projects WHERE repo = ?", (other,)
                    ).fetchone()["last_error"]
                )
                self.assertEqual(
                    tuple(connection.execute(
                        "SELECT worker_session_id, worktree, planning_retry_count "
                        "FROM projects WHERE repo = ?", (other,),
                    ).fetchone()),
                    ("peer-session", "/peer-worktree", 2),
                )
            finally:
                connection.close()

    def test_self_merge_requires_pr_targeting_registered_base(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            old_sha = git_command(repo, "rev-parse", "HEAD")
            runtime_path = state_dir / "runtime"
            git_command(repo, "worktree", "add", "--detach", str(runtime_path), old_sha)
            git_command(worktree, "push", "origin", "HEAD:main")
            runtime = loop_manager.RuntimeMutation(repo, runtime_path, old_sha, "a" * 32)
            config = loop_manager.ManagedConfig(
                path=Path(directory) / "config.ini", max_workers=1,
                telegram_token="", telegram_chat_id="",
            )
            try:
                for target in ("release", None):
                    with self.subTest(target=target):
                        merged = self._pull_request(
                            branch, url, head, "UNKNOWN", state="MERGED",
                        )
                        merged["mergedAt"] = "2026-09-24T00:00:00Z"
                        merged["baseRefName"] = target
                        with mock.patch.object(loop_manager, "ROOT", runtime_path), mock.patch(
                            "loop_manager.github_repository", return_value="owner/repo"
                        ), mock.patch(
                            "loop_manager.pr_for_branch", return_value=merged
                        ), mock.patch("loop_manager.run_project_result") as run_project:
                            with self.assertRaises(loop_manager.PublicationSafetyError):
                                loop_manager.schedule_registered_projects(
                                    connection, state_dir, config,
                                    runtime_mutation=runtime,
                                )
                        run_project.assert_not_called()
                        self.assertEqual(
                            connection.execute(
                                "SELECT status FROM exec_plans "
                                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),)
                            ).fetchone()["status"],
                            "PR_OPEN",
                        )
                        self.assertEqual(git_command(repo, "rev-parse", "HEAD"), old_sha)
            finally:
                connection.close()

    def test_self_merge_at_current_runtime_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            git_command(repo, "fetch", "origin")
            runtime_path = state_dir / "runtime"
            git_command(repo, "worktree", "add", "--detach", str(runtime_path), head)
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            runtime = loop_manager.RuntimeMutation(repo, runtime_path, head, "a" * 32)
            config = loop_manager.ManagedConfig(
                path=Path(directory) / "config.ini", max_workers=1,
                telegram_token="", telegram_chat_id="",
            )
            try:
                with mock.patch.object(loop_manager, "ROOT", runtime_path), mock.patch(
                    "loop_manager.github_repository", return_value="owner/repo"
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=merged
                ), mock.patch(
                    "loop_manager_internal.application.scheduler.VerifyCoordinator"
                ), mock.patch(
                    "loop_manager._run_registered_project",
                    return_value=loop_manager.ScheduledProjectResult(
                        str(repo), loop_manager.ProjectRunResult(
                            loop_manager.ProjectRunKind.WAITING, 0,
                        ),
                    ),
                ) as run_project:
                    outcomes = loop_manager.schedule_registered_projects(
                        connection, state_dir, config, runtime_mutation=runtime,
                    )
                self.assertEqual(len(outcomes), 1)
                run_project.assert_called_once()
            finally:
                connection.close()

    @dummy_worker_sessions
    def test_merge_observed_after_preflight_stops_before_archive_or_planner(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            old_sha = git_command(repo, "rev-parse", "HEAD")
            runtime_path = state_dir / "runtime"
            git_command(repo, "worktree", "add", "--detach", str(runtime_path), old_sha)
            git_command(worktree, "push", "origin", "HEAD:main")
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            runtime = loop_manager.RuntimeMutation(repo, runtime_path, old_sha, "a" * 32)
            gate = loop_manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
            token = loop_manager._runtime_handoff_signal.set(gate)
            try:
                with mock.patch.object(loop_manager, "ROOT", runtime_path), mock.patch(
                    "loop_manager.github_repository", return_value="owner/repo"
                ), mock.patch(
                    "loop_manager.validate_repository", return_value=repo,
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=merged,
                ), mock.patch(
                    "loop_manager.dispatch_execplan_archive_housekeeping",
                ) as archive, mock.patch("loop_manager.run_codex") as planner:
                    with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                        loop_manager.run_project_result(
                            connection, state_dir, str(repo), Path("unused.ini"),
                            "main", False, runtime_mutation=runtime,
                        )
                archive.assert_not_called()
                planner.assert_not_called()
                self.assertTrue(gate.event.is_set())
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                        (str(repo),),
                    ).fetchone()["status"],
                    "MERGED",
                )
            finally:
                loop_manager._runtime_handoff_signal.reset(token)
                connection.close()
