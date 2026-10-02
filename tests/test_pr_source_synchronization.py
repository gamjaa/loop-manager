from tests.support.pr_recovery import PrRecoveryFixture
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, commit_and_push
from tests.support.worker import worker_result
from tests.support.worker import dummy_worker_sessions


class PrSourceSynchronizationTest(PrRecoveryFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_merged_pr_fast_forwards_clean_source_after_durable_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            original_sync = loop_manager.ensure_safe_base_worktree
            original_catalog = loop_manager.reconcile_execplan_catalog
            observed = []
            catalog_shas = []

            def synchronize(path, base, *, remote_sha=None, state_dir=None):
                plan = connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()
                completion = connection.execute(
                    "SELECT last_completed_plan_id FROM project_completion_cycles "
                    "WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                observed.append(
                    (plan["status"], remote_sha, worktree.exists(),
                     completion["last_completed_plan_id"], connection.in_transaction)
                )
                return original_sync(
                    path, base, remote_sha=remote_sha, state_dir=state_dir,
                )

            def reconcile_catalog(*args, **kwargs):
                catalog_shas.append(kwargs.get("remote_sha"))
                return original_catalog(*args, **kwargs)

            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree", side_effect=synchronize
            ), mock.patch(
                "loop_manager.reconcile_execplan_catalog", side_effect=reconcile_catalog
            ), mock.patch("loop_manager.dispatch_execplan_archive_housekeeping"), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            self.assertEqual(result, 0)
            self.assertEqual(observed, [("MERGED", head, False, "G-026", False)])
            self.assertEqual(catalog_shas, [head])
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)
            planner.assert_called_once()

    @dummy_worker_sessions
    def test_archive_publication_syncs_source_and_catalog_to_final_remote_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            original_sync = loop_manager.ensure_safe_base_worktree
            original_catalog = loop_manager.reconcile_execplan_catalog
            source_shas = []
            catalog_shas = []

            def synchronize(path, base, *, remote_sha=None, state_dir=None):
                source_shas.append(remote_sha)
                return original_sync(
                    path, base, remote_sha=remote_sha, state_dir=state_dir,
                )

            def reconcile_catalog(*args, **kwargs):
                if kwargs.get("remote_sha") is not None:
                    catalog_shas.append(kwargs["remote_sha"])
                return original_catalog(*args, **kwargs)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree", side_effect=synchronize
            ), mock.patch(
                "loop_manager.reconcile_execplan_catalog", side_effect=reconcile_catalog
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            remote_sha = git_command(repo, "rev-parse", "origin/main")
            self.assertEqual(result, 0)
            self.assertNotEqual(remote_sha, head)
            self.assertEqual(source_shas, [remote_sha])
            # The fetched source is validated once; the proven archive rename
            # advances catalog paths/revision atomically without a full reread.
            self.assertEqual(catalog_shas, [head])
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), remote_sha)
            planner.assert_called_once()

    @dummy_worker_sessions
    def test_closed_pr_is_blocked_before_source_fast_forward(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, _worktree, url, head = (
                self._pr_open_state(directory)
            )
            remote_sha = self._advance_base(
                directory, repo, "docs/notes.md", "# After close\n", "Advance after close"
            )
            original_sync = loop_manager.ensure_safe_base_worktree
            observed = []

            def synchronize(path, base, *, remote_sha=None, state_dir=None):
                status = connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()["status"]
                observed.append((status, remote_sha))
                return original_sync(
                    path, base, remote_sha=remote_sha, state_dir=state_dir,
                )

            closed = self._pull_request(branch, url, head, "UNKNOWN", state="CLOSED")
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=closed
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree", side_effect=synchronize
            ), mock.patch("loop_manager.dispatch_execplan_archive_housekeeping"):
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            self.assertEqual(result, 0)
            self.assertEqual(observed, [("PLAN_BLOCKED", remote_sha)])
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), remote_sha)

    @dummy_worker_sessions
    def test_one_merged_pr_syncs_source_while_other_pr_stays_open(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            second_plan = write_execplan(repo, "G-027", "Other Open PR")
            commit_and_push(repo, "Add second open plan")
            second_base = git_command(repo, "rev-parse", "origin/main")
            loop_manager._register_execplan_rows(
                connection,
                str(repo),
                repo,
                [(second_plan.relative_to(repo).as_posix(), loop_manager._parse_execplan(repo, second_plan))],
                second_base,
            )
            second_branch, second_worktree = loop_manager.expected_execplan_workspace(
                state_dir, str(repo), "G-027"
            )
            loop_manager.make_worktree(repo, second_worktree, second_branch, "main")
            (second_worktree / "docs/other.md").write_text("Other PR\n", encoding="utf-8")
            git_command(second_worktree, "add", "docs/other.md")
            git_command(second_worktree, "commit", "-m", "Other PR")
            second_head = git_command(second_worktree, "rev-parse", "HEAD")
            second_url = "https://example.test/pr/27"
            with connection:
                connection.execute(
                    "UPDATE exec_plans SET status = 'PR_OPEN', active_sha = ?, "
                    "branch = ?, worktree = ?, workspace_owned = 1, pr_url = ?, "
                    "pr_head_oid = ? WHERE repo = ? AND plan_id = 'G-027'",
                    (second_base, second_branch, str(second_worktree), second_url,
                     second_head, str(repo)),
                )
                connection.execute(
                    "UPDATE projects SET phase = 'PR_OPEN', active_sha = ?, "
                    "branch = ?, worktree = ?, plan_path = ?, pr_url = ?, "
                    "pr_head_oid = ? WHERE repo = ?",
                    (second_base, second_branch, str(second_worktree),
                     second_plan.relative_to(repo).as_posix(), second_url,
                     second_head, str(repo)),
                )
            upstream = Path(directory) / "merge-writer"
            git_command(Path(directory), "clone", str(repo.parent / "remote.git"), str(upstream))
            git_command(upstream, "config", "user.email", "upstream@example.com")
            git_command(upstream, "config", "user.name", "Upstream Test")
            git_command(upstream, "merge", "--no-ff", f"origin/{branch}", "-m", "Merge first PR")
            git_command(upstream, "push", "origin", "HEAD:main")
            merged_head = git_command(upstream, "rev-parse", "HEAD")
            first_pr = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            first_pr["mergedAt"] = "2026-09-24T00:00:00Z"
            second_pr = self._pull_request(second_branch, second_url, second_head, "MERGEABLE")
            responses = {branch: first_pr, second_branch: second_pr}
            original_sync = loop_manager.ensure_safe_base_worktree
            observed = []

            def synchronize(path, base, *, remote_sha=None, state_dir=None):
                statuses = connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = ? ORDER BY plan_id",
                    (str(repo),),
                ).fetchall()
                project = connection.execute(
                    "SELECT phase FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                observed.append((tuple(tuple(item) for item in statuses),
                                 project["phase"], remote_sha, second_worktree.exists()))
                return original_sync(
                    path, base, remote_sha=remote_sha, state_dir=state_dir,
                )

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", side_effect=lambda _repo, name, _url: responses[name]
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree", side_effect=synchronize
            ), mock.patch("loop_manager.dispatch_execplan_archive_housekeeping"), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            self.assertEqual(result, 0)
            self.assertEqual(
                observed,
                [((('G-026', 'MERGED'), ('G-027', 'PR_OPEN')),
                  None, merged_head, True)],
            )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), merged_head)
            self.assertTrue(second_worktree.exists())
            planner.assert_called_once()

    @dummy_worker_sessions
    def test_fetch_failure_after_merge_keeps_transition_for_source_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            original_fetch = loop_manager._recorded_base_fetch
            failed = False

            def fail_after_transition(path, *arguments, **kwargs):
                nonlocal failed
                if not failed:
                    status = connection.execute(
                        "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                        (str(repo),),
                    ).fetchone()["status"]
                    if status == "MERGED":
                        failed = True
                        raise RuntimeError("fetch unavailable")
                return original_fetch(path, *arguments, **kwargs)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch("loop_manager._recorded_base_fetch", side_effect=fail_after_transition), mock.patch(
                "loop_manager.dispatch_execplan_archive_housekeeping"
            ):
                with self.assertRaisesRegex(RuntimeError, "fetch unavailable"):
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )
            self.assertTrue(failed)
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()["status"], "MERGED"
            )
            self.assertNotEqual(git_command(repo, "rev-parse", "HEAD"), head)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.dispatch_execplan_archive_housekeeping"
            ), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ), 0,
                )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)

    @dummy_worker_sessions
    def test_github_failure_defers_source_sync_until_pr_state_is_known(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", side_effect=RuntimeError("GitHub unavailable")
            ), mock.patch("loop_manager.ensure_safe_base_worktree") as synchronize:
                with self.assertRaisesRegex(RuntimeError, "GitHub unavailable"):
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )
            synchronize.assert_not_called()
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()["status"], "PR_OPEN"
            )
            self.assertNotEqual(git_command(repo, "rev-parse", "HEAD"), head)

            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch("loop_manager.dispatch_execplan_archive_housekeeping"), mock.patch(
                "loop_manager.run_codex",
                return_value=loop_manager.CodexResult(0, "", "planner-session"),
            ) as planner:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ), 0,
                )
            self.assertEqual(git_command(repo, "rev-parse", "HEAD"), head)
            planner.assert_called_once()

    @dummy_worker_sessions
    def test_unverifiable_merge_cleanup_blocks_source_sync_without_losing_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            git_command(worktree, "push", "origin", "HEAD:main")
            with connection:
                connection.execute(
                    "UPDATE exec_plans SET workspace_owned = 0 "
                    "WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                )
            merged = self._pull_request(branch, url, head, "UNKNOWN", state="MERGED")
            merged["mergedAt"] = "2026-09-24T00:00:00Z"
            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.pr_for_branch", return_value=merged
            ), mock.patch("loop_manager.ensure_safe_base_worktree") as synchronize:
                result = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                )

            self.assertEqual(result, 1)
            synchronize.assert_not_called()
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()["status"], "MERGED"
            )
            self.assertIsNotNone(loop_manager.scoped_blocking.project_block(connection, str(repo)))
            self.assertTrue(worktree.exists())
            self.assertNotEqual(git_command(repo, "rev-parse", "HEAD"), head)

    def test_catalog_rejects_remote_ref_change_after_source_sync(self):
        connection = sqlite3.connect(":memory:")
        try:
            newer = subprocess.CompletedProcess(
                ["git"], 0, stdout="newer-sha\n"
            )
            with mock.patch("loop_manager.git", return_value=newer), mock.patch(
                "loop_manager._GitCatalogDefinitionPort"
            ) as catalog:
                result = loop_manager.reconcile_execplan_catalog(
                    connection, Path("/repo"), "/repo", "main", remote_sha="pinned-sha"
                )

            self.assertEqual(
                result.kind, loop_manager.execplan_catalog_state.ReconciliationKind.STALE
            )
            self.assertEqual(result.revision_sha, "pinned-sha")
            catalog.assert_not_called()
        finally:
            connection.close()

    @dummy_worker_sessions
    def test_success_preserves_dirty_diverged_source_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                _worktree,
                pr_url,
                head_oid,
            ) = self._pr_open_state(directory)
            self._make_source_checkout_dirty_and_ahead(repo)
            self._advance_base(
                directory,
                repo,
                "docs/notes.md",
                "# Remote base advance\n",
                "Diverge source main",
            )
            git_command(repo, "fetch", "origin")
            local_main = git_command(repo, "rev-parse", "refs/heads/main")
            remote_main = git_command(repo, "rev-parse", "origin/main")
            self.assertNotEqual(local_main, remote_main)
            for ancestor, descendant in (
                (local_main, remote_main),
                (remote_main, local_main),
            ):
                relation = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "merge-base",
                        "--is-ancestor",
                        ancestor,
                        descendant,
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                self.assertEqual(relation.returncode, 1)
            source_before = self._source_checkout_snapshot(repo)
            pull_request = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ) as synchronize, mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=lambda _worktree, _state, _config, role, _prompt, **_kwargs:
                    loop_manager.CodexResult(0,
                        worker_result("Confirm rebased owned PR", "Checks the current final candidate.",
                                      "owned PR candidate: PASS") if role == "worker" else "VERDICT: PASS",
                        "worker-existing" if role == "worker" else "reviewer-existing"),
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                "SELECT status, pr_conflict_recovery FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(tuple(stored), ("PR_OPEN", 0))
            self.assertEqual(self._source_checkout_snapshot(repo), source_before)
            synchronize.assert_not_called()

    @dummy_worker_sessions
    def test_external_head_block_persists_and_preserves_dirty_ahead_source(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                _worktree,
                pr_url,
                observed_head,
            ) = self._pr_open_state(directory)
            self._make_source_checkout_dirty_and_ahead(repo)
            source_before = self._source_checkout_snapshot(repo)
            original_pull_request = self._pull_request(
                branch, pr_url, observed_head, "CONFLICTING"
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertIsNone(
                loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    project,
                    original_pull_request,
                )
            )

            upstream = Path(directory) / "external-recovery-head"
            git_command(
                Path(directory),
                "clone",
                str(repo.parent / "remote.git"),
                str(upstream),
            )
            external_head = git_command(upstream, "rev-parse", "HEAD")
            git_command(
                upstream,
                "push",
                "--force",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            pull_request = self._pull_request(
                branch, pr_url, external_head, "CONFLICTING"
            )
            pushes = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                first = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                second = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                       worker_session_id, reviewer_session_id
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertNotEqual(external_head, observed_head)
            self.assertEqual((first, second), (1, 1))
            self.assertEqual(stored["status"], "IMPLEMENTING")
            self.assertEqual(stored["pr_conflict_recovery"], 1)
            self.assertEqual(stored["pr_conflict_head_oid"], observed_head)
            project = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(project["phase"], "PROJECT_BLOCKED")
            self.assertEqual(stored["worker_session_id"], "worker-existing")
            self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
            self.assertEqual(self._source_checkout_snapshot(repo), source_before)
            self.assertEqual(pushes, [])
            agent.assert_not_called()
