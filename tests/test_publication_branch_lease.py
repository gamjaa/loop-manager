from tests.support.publication import PublicationFixture
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, commit_and_push
from tests.support.worker import worker_result
from tests.support.publication import seed_validated_candidate
from tests.support.worker import dummy_worker_sessions


class PublicationBranchLeaseTestFixture(PublicationFixture):
    pass

class PublicationLeaseRaceTests(PublicationBranchLeaseTestFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_remote_branch_push_interruption_recovers_without_another_push(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            git_command(
                worktree,
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            seed_validated_candidate(connection, repo, worktree, "G-014")
            created = []
            git_calls = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    git_calls.append(arguments)
                return original_git(path, *arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.build_pr_body", return_value="body"
            ), mock.patch(
                "loop_manager.command",
                side_effect=self._command_stub(created),
            ), mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                outcome = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, publication_remote_oid, pr_head_oid "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, publication_remote_oid, pr_head_oid "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            local_head = git_command(worktree, "rev-parse", "HEAD")

        self.assertEqual(outcome, 0)
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(plan["publication_remote_oid"], local_head)
        self.assertEqual(plan["pr_head_oid"], local_head)
        self.assertEqual(git_calls, [])
        self.assertEqual(len(created), 1)


    @dummy_worker_sessions
    def test_owned_remote_branch_rebase_uses_explicit_force_with_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            old_head = git_command(worktree, "rev-parse", "HEAD")
            git_command(
                worktree,
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
            )
            (repo / "docs/notes.md").write_text(
                "# New base\n", encoding="utf-8"
            )
            commit_and_push(repo, "Advance base after interrupted push")
            pushes = []
            created = []
            original_git = loop_manager.git

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            def reviewer(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                if role_name == "worker":
                    return loop_manager.CodexResult(0,
                        worker_result("Confirm current rebase candidate", "Checks the latest candidate and inputs.",
                                      "current rebase candidate: PASS"), "worker-session")
                self.assertEqual(role_name, "reviewer")
                return loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-session"
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
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ), mock.patch(
                "loop_manager.run_codex", side_effect=reviewer
            ), mock.patch(
                "loop_manager.build_pr_body", return_value="body"
            ), mock.patch(
                "loop_manager.command",
                side_effect=self._command_stub(created),
            ), mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                outcome = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

        lease = f"--force-with-lease=refs/heads/{branch}:{old_head}"
        self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(len(pushes), 1)
        self.assertIn(lease, pushes[0])
        self.assertNotIn("--force", pushes[0])
        self.assertEqual(len(created), 1)


    def test_owned_branch_push_rechecks_live_ref_before_lease(self):
        branch = 'loop-manager/G-081'
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        plan = {'plan_id': 'G-081', 'publication_remote_oid': 'a' * 40, 'pr_url': None}
        calls = []

        def git(_path, *arguments, **_kwargs):
            calls.append(arguments)
            if arguments[:2] == ('rev-parse', 'HEAD'):
                return subprocess.CompletedProcess(arguments, 0, 'b' * 40 + '\n')
            if arguments[0] == 'ls-remote':
                return subprocess.CompletedProcess(
                    arguments, 0, 'c' * 40 + f'\trefs/heads/{branch}\n',
                )
            raise AssertionError(f'unexpected Git command: {arguments}')

        with (
            mock.patch('loop_manager._owned_publication_plan', return_value=plan),
            mock.patch('loop_manager.remote_branch_oid', return_value='a' * 40) as remote,
            mock.patch('loop_manager.record_remote_publication_branch'),
            mock.patch('loop_manager.git', side_effect=git),
        ):
            with self.assertRaises(loop_manager.PublicationSafetyError):
                loop_manager.push_managed_publication_branch(
                    connection, Path('/repo'), Path('/state'), 'repo',
                    branch, Path('/managed'), 'b' * 40,
                )
            remote.return_value = None
            with self.assertRaises(loop_manager.PublicationSafetyError):
                loop_manager.push_managed_publication_branch(
                    connection, Path('/repo'), Path('/state'), 'repo',
                    branch, Path('/managed'), 'b' * 40,
                )
        self.assertFalse(any(args[0] == 'push' for args in calls))


    def test_owned_branch_push_uses_exact_lease_and_saves_confirmed_head(self):
        branch = 'loop-manager/G-081'
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        old, new = 'a' * 40, 'b' * 40
        pushes = []
        remote_observations = iter((old, new))

        def git(_path, *arguments, **_kwargs):
            if arguments[0] == 'rev-parse':
                return subprocess.CompletedProcess(arguments, 0, new + '\n')
            if arguments[0] == 'ls-remote':
                return subprocess.CompletedProcess(
                    arguments, 0,
                    next(remote_observations) + f'\trefs/heads/{branch}\n',
                )
            if arguments[0] == 'push':
                pushes.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, '')

        with (
            mock.patch('loop_manager._owned_publication_plan', return_value={
                'plan_id': 'G-081', 'publication_remote_oid': old, 'pr_url': None,
            }),
            mock.patch('loop_manager.remote_branch_oid', side_effect=[old, new]),
            mock.patch('loop_manager.record_remote_publication_branch'),
            mock.patch('loop_manager.pr_for_branch', return_value=None),
            mock.patch('loop_manager.update_project') as update,
            mock.patch('loop_manager.git', side_effect=git),
            mock.patch('loop_manager._recorded_base_fetch', return_value=new) as fetched,
        ):
            result = loop_manager.push_managed_publication_branch(
                connection, Path('/repo'), Path('/state'), 'repo',
                branch, Path('/managed'), new,
            )

        self.assertEqual(result, new)
        fetched.assert_called_once_with(
            Path('/repo'), Path('/state'), branch, owner='project',
        )
        self.assertEqual(len(pushes), 1)
        self.assertIn(f'--force-with-lease=refs/heads/{branch}:{old}', pushes[0])
        self.assertNotIn('-u', pushes[0])
        self.assertNotIn('--set-upstream', pushes[0])
        self.assertEqual(pushes[0][-2:], ('origin', f'{new}:refs/heads/{branch}'))
        update.assert_called_once_with(connection, 'repo', publication_remote_oid=new)


    @dummy_worker_sessions
    def test_unowned_remote_branch_is_blocked_without_force_push(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                _worktree,
            ) = self._publication_state(directory)
            git_command(
                repo,
                "push",
                "origin",
                f"HEAD:refs/heads/{branch}",
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
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.git", side_effect=recording_git
            ):
                outcome = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()

        self.assertEqual(outcome, 1)
        self.assertEqual(blocked["phase"], "PROJECT_BLOCKED")
        self.assertIn("갱신을 거부", blocked["last_error"])
        self.assertEqual(pushes, [])


class PublicationPrObservationTests(PublicationBranchLeaseTestFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_existing_open_closed_reopened_and_merged_prs_are_reused(self):
        for initial_state in ("OPEN", "CLOSED", "MERGED"):
            with self.subTest(state=initial_state), tempfile.TemporaryDirectory() as directory:
                (
                    repo,
                    state_dir,
                    connection,
                    _plan,
                    branch,
                    worktree,
                ) = self._publication_state(directory)
                head = git_command(worktree, "rev-parse", "HEAD")
                approval = seed_validated_candidate(connection, repo, worktree, 'G-014')
                base = git_command(repo, 'rev-parse', 'origin/main')
                loop_manager.update_project(connection, str(repo),
                    publication_token=approval.publication_token(base, head))
                pull_request = {
                    "url": "https://example.test/pr/14",
                    "state": initial_state,
                    "mergedAt": (
                        "2026-09-19T00:00:00Z"
                        if initial_state == "MERGED"
                        else None
                    ),
                    "headRefOid": head,
                    "headRefName": branch,
                }
                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.pr_for_branch", return_value=pull_request
                ), mock.patch("loop_manager.run_codex") as agent:
                    outcome = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )
                agent.assert_not_called()

                plan = connection.execute(
                    "SELECT status, pr_url, pr_head_oid FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-014'",
                    (str(repo),),
                ).fetchone()
                if initial_state == "OPEN":
                    self.assertEqual(outcome, 0)
                    self.assertEqual(plan["status"], "PR_OPEN")
                    self.assertEqual(plan["pr_head_oid"], head)
                elif initial_state == "CLOSED":
                    self.assertEqual(outcome, 0)
                    self.assertEqual(plan["status"], "PLAN_BLOCKED")
                    reopened = {**pull_request, "state": "OPEN"}
                    with mock.patch(
                        "loop_manager.validate_repository", return_value=repo
                    ), mock.patch(
                        "loop_manager.pr_for_branch", return_value=reopened
                    ):
                        self.assertEqual(
                            loop_manager.run_project(
                                connection,
                                state_dir,
                                str(repo),
                                Path("unused.ini"),
                                "main",
                                False,
                            ),
                            0,
                        )
                    resumed = connection.execute(
                        "SELECT status, pr_url FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-014'",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(resumed["status"], "PR_OPEN")
                    self.assertEqual(resumed["pr_url"], pull_request["url"])
                else:
                    self.assertEqual(outcome, 0)
                    self.assertEqual(plan["status"], "MERGED")
                    self.assertFalse(worktree.exists())
