from tests.support.pr_recovery import PrRecoveryFixture
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command
from tests.support.publication import seed_validated_candidate
from tests.support.worker import dummy_worker_sessions


class PrConflictPublicationTest(PrRecoveryFixture, unittest.TestCase):
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

    @dummy_worker_sessions
    def test_external_remote_head_change_blocks_without_push(self):
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
            upstream = Path(directory) / "external-head"
            git_command(Path(directory), "clone", str(repo.parent / "remote.git"), str(upstream))
            external_head = git_command(upstream, "rev-parse", "HEAD")
            git_command(
                upstream,
                "push",
                "--force",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            pushes = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            pull_request = self._pull_request(
                branch, pr_url, external_head, "CONFLICTING"
            )
            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                "SELECT phase, pr_conflict_recovery, last_error "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertNotEqual(external_head, head_oid)
            self.assertEqual(result, 1)
            self.assertEqual(stored["phase"], "PROJECT_BLOCKED")
            self.assertEqual(stored["pr_conflict_recovery"], 0)
            self.assertIn("head OID", stored["last_error"])
            self.assertEqual(pushes, [])
            agent.assert_not_called()

    @dummy_worker_sessions
    def test_lease_competition_blocks_without_overwriting_remote_head(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                observed_head,
            ) = self._pr_open_state(directory)
            (worktree / "docs/notes.md").write_text(
                "# Locally recovered head\n", encoding="utf-8"
            )
            git_command(worktree, "add", "docs/notes.md")
            git_command(worktree, "commit", "-m", "Prepare lease race head")
            local_head = git_command(worktree, "rev-parse", "HEAD")
            base_oid = git_command(repo, "rev-parse", "origin/main")
            loop_manager.update_project(
                connection,
                str(repo),
                phase="PUBLISHING",
                active_sha=base_oid,
                validated_base_sha=base_oid,
                pr_conflict_recovery=1,
                pr_conflict_head_oid=observed_head,
                pr_conflict_base_oid=base_oid,
            )
            seed_validated_candidate(connection, repo, worktree, "G-026")

            competitor = Path(directory) / "lease-competitor"
            git_command(
                Path(directory),
                "clone",
                str(repo.parent / "remote.git"),
                str(competitor),
            )
            git_command(competitor, "config", "user.email", "race@example.com")
            git_command(competitor, "config", "user.name", "Lease Race")
            (competitor / "lease-race.txt").write_text(
                "external update\n", encoding="utf-8"
            )
            git_command(competitor, "add", "lease-race.txt")
            git_command(competitor, "commit", "-m", "Win lease race")
            external_head = git_command(competitor, "rev-parse", "HEAD")
            pull_request = self._pull_request(
                branch, pr_url, observed_head, "CONFLICTING"
            )
            original_git = loop_manager.git
            pushes = []
            raced = False

            def racing_git(path, *arguments, **kwargs):
                nonlocal raced
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                    if not raced:
                        raced = True
                        git_command(
                            competitor,
                            "push",
                            "--force",
                            "origin",
                            f"HEAD:refs/heads/{branch}",
                        )
                return original_git(path, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.git", side_effect=racing_git
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            git_command(repo, "fetch", "origin", branch)
            stored = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                outcome.kind, loop_manager.ProjectRunKind.BLOCKED,
                (outcome.detail, stored["status"],
                 stored["pr_conflict_recovery"], pushes),
            )
            self.assertEqual(stored["status"], "PUBLISHING")
            self.assertEqual(stored["pr_conflict_recovery"], 1)
            self.assertEqual(stored["pr_conflict_head_oid"], observed_head)
            self.assertEqual(
                git_command(repo, "rev-parse", f"origin/{branch}"),
                external_head,
            )
            self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), local_head)
            self.assertEqual(len(pushes), 1)
            self.assertIn(
                f"--force-with-lease=refs/heads/{branch}:{observed_head}",
                pushes[0],
            )

    def test_post_push_interruption_confirms_local_head_without_repush(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                branch,
                worktree,
                pr_url,
                observed_head,
            ) = self._pr_open_state(directory)
            (worktree / "docs/notes.md").write_text(
                "# Recovered push\n", encoding="utf-8"
            )
            git_command(worktree, "add", "docs/notes.md")
            git_command(worktree, "commit", "-m", "Prepare recovered PR head")
            local_head = git_command(worktree, "rev-parse", "HEAD")
            git_command(
                worktree,
                "push",
                "--force-with-lease",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            base_oid = git_command(repo, "rev-parse", "origin/main")
            loop_manager.update_project(
                connection,
                str(repo),
                phase="PUBLISHING",
                active_sha=base_oid,
                validated_base_sha=base_oid,
                pr_conflict_recovery=1,
                pr_conflict_head_oid=observed_head,
                pr_conflict_base_oid=base_oid,
            )
            approval = seed_validated_candidate(connection, repo, worktree, 'G-026')
            loop_manager.update_project(connection, str(repo),
                publication_token=approval.publication_token(base_oid, local_head))
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            pull_request = self._pull_request(
                branch, pr_url, local_head, "MERGEABLE"
            )
            pushes = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            with mock.patch("loop_manager.git", side_effect=recording_git):
                result = loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    project,
                    pull_request,
                )

            stored = connection.execute(
                """
                SELECT status, publication_remote_oid, pr_head_oid,
                       pr_conflict_recovery, pr_conflict_head_oid,
                       pr_conflict_base_oid
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 0)
            self.assertEqual(stored["status"], "PR_OPEN")
            self.assertEqual(stored["publication_remote_oid"], local_head)
            self.assertEqual(stored["pr_head_oid"], local_head)
            self.assertEqual(stored["pr_conflict_recovery"], 0)
            self.assertIsNone(stored["pr_conflict_head_oid"])
            self.assertIsNone(stored["pr_conflict_base_oid"])
            self.assertEqual(pushes, [])

    @dummy_worker_sessions
    def test_post_push_ownership_failure_blocks_and_preserves_recovery(self):
        for damage in ("missing-worktree", "wrong-branch"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                repo, state_dir, connection, branch, worktree, url, observed_head = (
                    self._pr_open_state(directory)
                )
                (worktree / "docs/notes.md").write_text("# Recovered head\n", encoding="utf-8")
                git_command(worktree, "add", "docs/notes.md")
                git_command(worktree, "commit", "-m", "Prepare interrupted push")
                local_head = git_command(worktree, "rev-parse", "HEAD")
                git_command(
                    worktree, "push",
                    f"--force-with-lease=refs/heads/{branch}:{observed_head}",
                    "origin", f"HEAD:refs/heads/{branch}",
                )
                base = git_command(repo, "rev-parse", "origin/main")
                loop_manager.update_project(
                    connection, str(repo), phase="PUBLISHING",
                    active_sha=base, validated_base_sha=base,
                    pr_conflict_recovery=1, pr_conflict_head_oid=observed_head,
                    pr_conflict_base_oid=base,
                )
                approval = seed_validated_candidate(connection, repo, worktree, 'G-026')
                loop_manager.update_project(connection, str(repo),
                    publication_token=approval.publication_token(base, local_head))
                self._make_source_checkout_dirty_and_ahead(repo)
                source_before = self._source_checkout_snapshot(repo)
                if damage == "missing-worktree":
                    preserved = worktree.with_name("preserved-worktree")
                    worktree.rename(preserved)
                else:
                    preserved = worktree
                    git_command(worktree, "checkout", "-b", "user-owned-branch")
                evidence = preserved / "keep-untracked.txt"
                evidence.write_text("preserve this evidence\n", encoding="utf-8")
                original_git = loop_manager.git
                writes = []

                def recording_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] in {"push", "rebase", "reset", "clean"}:
                        writes.append(arguments)
                    return original_git(path, *arguments, **kwargs)

                pull_request = self._pull_request(branch, url, local_head, "MERGEABLE")
                with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                    "loop_manager.pr_for_branch", return_value=pull_request
                ), mock.patch("loop_manager.git", side_effect=recording_git), mock.patch(
                    "loop_manager.run_codex"
                ) as agent:
                    for _attempt in range(2):
                        result = loop_manager.run_project_result(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        )
                        self.assertEqual(result.kind, loop_manager.ProjectRunKind.BLOCKED)
                        plan = connection.execute(
                            "SELECT * FROM exec_plans "
                            "WHERE repo = ? AND plan_id = 'G-026'",
                            (str(repo),),
                        ).fetchone()
                        self.assertEqual(plan["status"], "PUBLISHING")
                        self.assertIn("안전 검증 실패", plan["last_error"])
                        self.assertEqual(plan["pr_conflict_recovery"], 1)
                        self.assertEqual(plan["pr_conflict_head_oid"], observed_head)
                        self.assertEqual(plan["pr_conflict_base_oid"], base)
                        self.assertEqual(plan["pr_head_oid"], observed_head)
                        self.assertEqual(plan["worker_session_id"], "worker-existing")
                        self.assertEqual(plan["reviewer_session_id"], "reviewer-existing")
                        project = connection.execute(
                            "SELECT phase FROM projects WHERE repo = ?", (str(repo),)
                        ).fetchone()
                        self.assertEqual(project["phase"], "PROJECT_BLOCKED")
                    agent.assert_not_called()
                self.assertEqual(writes, [])
                self.assertEqual(self._source_checkout_snapshot(repo), source_before)
                self.assertEqual(evidence.read_text(encoding="utf-8"), "preserve this evidence\n")
                self.assertEqual((preserved / "docs/notes.md").read_text(
                    encoding="utf-8"), "# Recovered head\n")
                self.assertEqual(git_command(repo, "rev-parse", f"refs/heads/{branch}"), local_head)
                self.assertEqual(git_command(
                    repo.parent / "remote.git", "rev-parse", f"refs/heads/{branch}"
                ), local_head)

    def test_only_closed_pr_block_resumes_when_pr_reopens(self):
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
            base_oid = git_command(repo, "rev-parse", "origin/main")
            loop_manager.update_project(
                connection,
                str(repo),
                phase="PUBLISHING",
                active_sha=base_oid,
                validated_base_sha=base_oid,
                pr_conflict_recovery=1,
                pr_conflict_head_oid=head_oid,
                pr_conflict_base_oid=base_oid,
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            closed = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING", state="CLOSED"
            )
            self.assertEqual(
                loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project, closed
                ),
                1,
            )
            blocked = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(blocked["status"], "PLAN_BLOCKED")
            self.assertEqual(
                blocked["blocked_reason"],
                loop_manager.PR_CLOSED_BLOCK_REASON,
            )

            loop_manager.hydrate_execplan_project(
                connection, str(repo), blocked
            )
            blocked_project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            reopened = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )
            self.assertIsNone(
                loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    blocked_project,
                    reopened,
                )
            )
            resumed = connection.execute(
                """
                SELECT status, blocked_reason, pr_conflict_recovery,
                       pr_conflict_head_oid
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertEqual(resumed["status"], "IMPLEMENTING")
            self.assertIsNone(resumed["blocked_reason"])
            self.assertEqual(resumed["pr_conflict_recovery"], 1)
            self.assertEqual(resumed["pr_conflict_head_oid"], head_oid)

    def test_recovery_merge_and_close_races_never_push(self):
        for state in ("CLOSED", "MERGED"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                (
                    repo,
                    state_dir,
                    connection,
                    branch,
                    worktree,
                    pr_url,
                    head_oid,
                ) = self._pr_open_state(directory)
                base_oid = git_command(repo, "rev-parse", "origin/main")
                loop_manager.update_project(
                    connection,
                    str(repo),
                    phase="PUBLISHING",
                    active_sha=base_oid,
                    validated_base_sha=base_oid,
                    pr_conflict_recovery=1,
                    pr_conflict_head_oid=head_oid,
                    pr_conflict_base_oid=base_oid,
                )
                project = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                pull_request = self._pull_request(
                    branch, pr_url, head_oid, "CONFLICTING", state=state
                )
                if state == "MERGED":
                    pull_request["mergedAt"] = "2026-09-21T00:00:00Z"
                pushes = []
                original_git = loop_manager.git

                def recording_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] == "push":
                        pushes.append(arguments)
                    return original_git(path, *arguments, **kwargs)

                with mock.patch(
                    "loop_manager.git", side_effect=recording_git
                ):
                    result = loop_manager.reconcile_existing_pull_request(
                        connection,
                        repo,
                        state_dir,
                        str(repo),
                        project,
                        pull_request,
                    )

                stored = connection.execute(
                    "SELECT status, pr_conflict_recovery FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-026'",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(pushes, [])
                if state == "CLOSED":
                    self.assertEqual(result, 1)
                    self.assertEqual(tuple(stored), ("PLAN_BLOCKED", 1))
                    self.assertTrue(worktree.is_dir())
                else:
                    self.assertEqual(
                        result, loop_manager.PROJECT_STEP_PLANNING_PENDING
                    )
                    self.assertEqual(tuple(stored), ("MERGED", 0))
                    self.assertFalse(worktree.exists())

    @dummy_worker_sessions
    def test_reopened_head_safety_failure_cannot_resume_when_head_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection, branch, worktree, url, head = (
                self._pr_open_state(directory)
            )
            base = git_command(repo, "rev-parse", "origin/main")
            loop_manager.update_project(
                connection, str(repo), phase="PUBLISHING",
                pr_conflict_recovery=1, pr_conflict_head_oid=head,
                pr_conflict_base_oid=base,
            )
            closed = self._pull_request(branch, url, head, "CONFLICTING", state="CLOSED")
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(loop_manager.reconcile_existing_pull_request(
                connection, repo, state_dir, str(repo), project, closed
            ), 1)

            with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch("loop_manager.push_owned_pr_conflict_recovery") as push:
                # A separate project safety cause permits PR queries, but no
                # close/reopen transition before a validated catalog boundary.
                scenarios = (
                    (base, "OPEN", "PLAN_BLOCKED"),
                    (head, "CLOSED", "PLAN_BLOCKED"),
                    (head, "OPEN", "PLAN_BLOCKED"),
                    (head, "OPEN", "PLAN_BLOCKED"),
                )
                for observed, state, expected_status in scenarios:
                    with mock.patch("loop_manager.pr_for_branch", return_value=
                                    self._pull_request(
                                        branch, url, observed, "CONFLICTING", state=state
                                    )):
                        result = loop_manager.run_project_result(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        )
                    self.assertEqual(result.kind, loop_manager.ProjectRunKind.BLOCKED)
                    stored = connection.execute(
                        "SELECT * FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-026'",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(stored["status"], expected_status)
                    self.assertEqual(
                        stored["blocked_reason"],
                        loop_manager.PR_CLOSED_BLOCK_REASON
                        if expected_status == "PLAN_BLOCKED"
                        else None,
                    )
                    self.assertEqual(stored["pr_conflict_head_oid"], head)
                    self.assertEqual(stored["pr_conflict_recovery"], 1)
                    blocked_project = connection.execute(
                        "SELECT phase FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(blocked_project["phase"], "PROJECT_BLOCKED")
                    self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), head)
                agent.assert_not_called()
                push.assert_not_called()
