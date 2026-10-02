from tests.support.pr_recovery import PrRecoveryFixture
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command


class PrPreflightRecoveryTest(PrRecoveryFixture, unittest.TestCase):
    def _add_ignored_cache(self, worktree: Path) -> Path:
        exclude = Path(
            git_command(worktree, "rev-parse", "--git-path", "info/exclude")
        )
        if not exclude.is_absolute():
            exclude = worktree / exclude
        exclude.write_text(
            exclude.read_text(encoding="utf-8") + "\n__pycache__/\n",
            encoding="utf-8",
        )
        cache = worktree / "__pycache__/verify.cpython-312.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"ignored verify artifact")
        self.assertEqual(git_command(worktree, "status", "--porcelain"), "")
        self.assertNotIn(
            cache.relative_to(worktree).as_posix(),
            loop_manager.capture_git_snapshot(worktree).dirty,
        )
        return cache

    def test_legacy_ignored_only_preflight_block_recovers_to_pr_open(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            cache = self._add_ignored_cache(worktree)
            error = (
                f"{loop_manager.PR_CONFLICT_PREFLIGHT_ERROR_PREFIX} "
                "PR 충돌 복구 시작 전 관리 worktree가 관측한 원격 head의 "
                "clean 상태와 일치하지 않습니다."
            )
            loop_manager.block_active_execplan_from_revision(
                connection,
                repo,
                str(repo),
                "refs/remotes/origin/main",
                error,
                "verify: PASS",
            )
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(
                connection, str(repo), blocked
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            with mock.patch(
                "loop_manager._fetch_pr_conflict_recovery_refs"
            ) as conflict_fetch, mock.patch(
                "loop_manager._validate_pr_conflict_recovery_ownership"
            ) as conflict_ownership:
                result = loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    project,
                    self._pull_request(branch, pr_url, head_oid, "MERGEABLE"),
                )
            conflict_fetch.assert_not_called()
            conflict_ownership.assert_not_called()
            stored = connection.execute(
                """
                SELECT status, blocked_reason, blocked_fingerprint,
                       blocked_worktree_fingerprint, last_error,
                       worker_session_id, reviewer_session_id,
                       verify_result, review_result
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            cache_preserved = cache.is_file()

        self.assertEqual(result, 0)
        self.assertEqual(stored["status"], "PR_OPEN")
        self.assertIsNone(stored["blocked_reason"])
        self.assertIsNone(stored["blocked_fingerprint"])
        self.assertIsNone(stored["blocked_worktree_fingerprint"])
        self.assertIsNone(stored["last_error"])
        self.assertEqual(stored["worker_session_id"], "worker-existing")
        self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
        self.assertEqual(stored["verify_result"], "verify: PASS")
        self.assertEqual(stored["review_result"], "VERDICT: PASS")
        self.assertTrue(cache_preserved)

    def test_unknown_preflight_block_with_ignored_cache_recovers_without_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            cache = self._add_ignored_cache(worktree)
            loop_manager.block_active_execplan_from_revision(
                connection, repo, str(repo), "refs/remotes/origin/main",
                loop_manager.PR_CONFLICT_PREFLIGHT_ERROR_PREFIX + " ignored cache",
                "verify: PASS",
            )
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(connection, str(repo), blocked)
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            with mock.patch("loop_manager._fetch_pr_conflict_recovery_refs") as fetch:
                result = loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project,
                    self._pull_request(branch, url, head, "UNKNOWN"),
                )
            stored = connection.execute(
                "SELECT status, pr_conflict_recovery, verify_result FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(result, 0)
            self.assertEqual(tuple(stored), ("PR_OPEN", 0, "verify: PASS"))
            self.assertTrue(cache.is_file())
            fetch.assert_not_called()

    def test_preflight_block_with_ignored_cache_and_tracked_change_stays_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            cache = self._add_ignored_cache(worktree)
            loop_manager.block_active_execplan_from_revision(
                connection, repo, str(repo), "refs/remotes/origin/main",
                loop_manager.PR_CONFLICT_PREFLIGHT_ERROR_PREFIX + " ignored cache",
                "verify: PASS",
            )
            (worktree / "src/app.py").write_text("VALUE = 99\n", encoding="utf-8")
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(connection, str(repo), blocked)
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            with mock.patch("loop_manager._fetch_pr_conflict_recovery_refs") as fetch:
                result = loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project,
                    self._pull_request(branch, url, head, "MERGEABLE"),
                )
            stored = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertIn("src/app.py", stored["last_error"])
            self.assertTrue(cache.is_file())
            fetch.assert_not_called()

    def test_preflight_block_with_conflicting_pr_starts_owned_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            cache = self._add_ignored_cache(worktree)
            loop_manager.block_active_execplan_from_revision(
                connection, repo, str(repo), "refs/remotes/origin/main",
                loop_manager.PR_CONFLICT_PREFLIGHT_ERROR_PREFIX + " ignored cache",
                "verify: PASS",
            )
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(connection, str(repo), blocked)
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            with mock.patch(
                "loop_manager.notifications.enqueue_current_project_events",
                side_effect=RuntimeError("interrupt before commit"),
            ), self.assertRaisesRegex(RuntimeError, "interrupt before commit"):
                loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project,
                    self._pull_request(branch, url, head, "CONFLICTING"),
                )
            interrupted = connection.execute(
                "SELECT status, pr_conflict_recovery, pr_conflict_head_oid, "
                "pr_conflict_base_oid FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertEqual(tuple(interrupted), ("PLAN_BLOCKED", 0, None, None))
            interrupted_project = connection.execute(
                "SELECT phase, pr_conflict_recovery FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(tuple(interrupted_project), ("PLAN_BLOCKED", 0))
            result = loop_manager.reconcile_existing_pull_request(
                connection, repo, state_dir, str(repo), project,
                self._pull_request(branch, url, head, "CONFLICTING"),
            )
            stored = connection.execute(
                "SELECT status, pr_conflict_recovery, pr_conflict_head_oid, "
                "worker_session_id, reviewer_session_id FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'", (str(repo),),
            ).fetchone()
            self.assertIsNone(result)
            self.assertEqual(
                tuple(stored),
                ("IMPLEMENTING", 1, head, "worker-existing", "reviewer-existing"),
            )
            resumed_project = connection.execute(
                "SELECT phase, pr_conflict_recovery, pr_conflict_head_oid "
                "FROM projects WHERE repo = ?", (str(repo),),
            ).fetchone()
            self.assertEqual(tuple(resumed_project), ("WORKING", 1, head))
            self.assertTrue(cache.is_file())
