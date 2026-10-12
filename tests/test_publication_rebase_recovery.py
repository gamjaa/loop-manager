from tests.support.recovery_boundary import RecoveryBoundary
from tests.support.publication import PublicationFixture
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command, commit_and_push, isolated_git_test
from tests.support.worker import worker_result
from tests.support.worker import dummy_worker_sessions


class PublicationRebaseRecoveryTestFixture(PublicationFixture):
    pass

@isolated_git_test
class PublicationConflictRecoveryTests(PublicationRebaseRecoveryTestFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_publication_rebase_conflict_blocks_when_worker_cannot_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n", encoding="utf-8"
            )
            commit_and_push(repo, "Create publication conflict")

            common_patches = (
                mock.patch("loop_manager.validate_repository", return_value=repo),
                mock.patch("loop_manager.remote_base", return_value="main"),
                mock.patch("loop_manager.ensure_safe_base_worktree"),
                mock.patch("loop_manager.pr_for_branch", return_value=None),
            )
            with common_patches[0], common_patches[1], common_patches[2], common_patches[3], mock.patch(
                "loop_manager.run_codex"
            ) as agent, mock.patch("loop_manager.verify") as verify:
                first = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
            conflicted = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(first, 1)
            self.assertEqual(conflicted["phase"], "WORKING")
            self.assertTrue(
                conflicted["last_error"].startswith(
                    loop_manager.REBASE_CONFLICT_PREFIX
                )
            )
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            agent.assert_not_called()
            verify.assert_not_called()

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
                return_value=loop_manager.CodexResult(
                    1, "cannot resolve conflict", "worker-session"
                ),
            ):
                second = loop_manager.run_project(
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
            plan = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            rebase_preserved = loop_manager._rebase_in_progress_for_branch(worktree, branch)
            unmerged = git_command(worktree, 'ls-files', '-u')

        self.assertEqual(second, 0)
        self.assertIsNone(blocked["phase"])
        self.assertIn("cannot resolve conflict", plan["last_error"])
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        # Paired with the PR retry-policy unit test: blocking must preserve the
        # actual conflict, index and operation, not just a mocked marker value.
        self.assertTrue(rebase_preserved)
        self.assertIn('src/app.py', unmerged)


    @dummy_worker_sessions
    def test_real_autostash_conflict_resumes_to_verified_pr_without_reapplying_stash(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo, state_dir, connection, _plan, branch, worktree,
            ) = self._publication_state(directory)
            original_base = git_command(repo, "rev-parse", "origin/main")
            git_command(worktree, "reset", "--hard", original_base)
            (worktree / "src/feature.py").write_text(
                "FEATURE = True\n", encoding="utf-8"
            )
            git_command(worktree, "add", "src/feature.py")
            git_command(worktree, "commit", "-m", "Add independent feature")
            (worktree / "src/app.py").write_text(
                "VALUE = 'worker'\n", encoding="utf-8"
            )
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n", encoding="utf-8"
            )
            commit_and_push(repo, "Change app on latest base")
            original_git = loop_manager.git
            rebase_results = []

            def record_initial_git(path, *arguments, **kwargs):
                result = original_git(path, *arguments, **kwargs)
                if path == worktree and arguments[:2] == ("rebase", "--autostash"):
                    rebase_results.append(result.returncode)
                return result

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch("loop_manager.run_codex") as first_agent, mock.patch(
                "loop_manager.git", side_effect=record_initial_git
            ):
                first = loop_manager.run_project(
                    connection, state_dir, str(repo), Path("unused.ini"),
                    "main", False,
                )

            self.assertEqual(first, 1)
            self.assertEqual(rebase_results, [0])
            first_agent.assert_not_called()
            self.assertFalse(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertTrue(git_command(worktree, "ls-files", "-u"))
            saved = connection.execute(
                "SELECT operation, snapshot FROM execution_checkpoints "
                "WHERE repo = ? AND plan_id = 'G-014'", (str(repo),),
            ).fetchone()
            self.assertEqual(saved["operation"], "rebase")
            self.assertTrue(json.loads(saved["snapshot"])["stash_oid"])

            roles = []
            git_steps = []
            created = []

            def record_git(path, *arguments, **kwargs):
                if path == worktree and arguments and arguments[0] in {"rebase", "stash"}:
                    git_steps.append(arguments)
                return original_git(path, *arguments, **kwargs)

            def agent(agent_worktree, _state, _config, role, _prompt, **_kwargs):
                roles.append(role)
                if role == "worker":
                    self.assertEqual(bool(git_command(worktree, "ls-files", "-u")), roles.count("worker") == 1)
                    self.assertFalse(
                        loop_manager._rebase_in_progress_for_branch(worktree, branch)
                    )
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 'resolved'\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(
                            "Resolve autostash conflict",
                            "Keeps the feature and resolves the saved app change.",
                            "LatestBasePublicationTest: PASS",
                        ),
                        "worker-session",
                    )
                self.assertEqual(role, "reviewer")
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
                "loop_manager.run_codex", side_effect=agent
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify, mock.patch(
                "loop_manager.build_pr_body", return_value="body"
            ), mock.patch(
                "loop_manager.command", side_effect=self._command_stub(created)
            ), mock.patch("loop_manager.git", side_effect=record_git):
                resumed = loop_manager.run_project_result(
                    connection, state_dir, str(repo), Path("unused.ini"),
                    "main", False,
                )

            plan = connection.execute(
                "SELECT status, active_sha, validated_base_sha "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            latest_base = git_command(repo, "rev-parse", "origin/main")
            self.assertEqual(resumed.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(plan["status"], "PR_OPEN")
            self.assertEqual(plan["active_sha"], latest_base)
            self.assertEqual(plan["validated_base_sha"], latest_base)
            self.assertFalse(git_command(worktree, "ls-files", "-u"))
            self.assertFalse(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertEqual(
                (worktree / "src/feature.py").read_text(encoding="utf-8"),
                "FEATURE = True\n",
            )
            self.assertEqual(
                (worktree / "src/app.py").read_text(encoding="utf-8"),
                "VALUE = 'resolved'\n",
            )
            self.assertEqual(roles, ["worker", "worker", "reviewer"])
            verify.assert_called_once()
            self.assertEqual(git_steps, [])
            self.assertEqual(len(created), 1)


    @dummy_worker_sessions
    def test_empty_conflict_resolution_blocks_instead_of_reinvoking_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n", encoding="utf-8"
            )
            commit_and_push(repo, "Create publication conflict")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent:
                first = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            self.assertEqual(first, 1)
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            agent.assert_not_called()

            def choose_latest_base(
                agent_worktree,
                _state,
                _config,
                role_name,
                _prompt,
                **_kwargs,
            ):
                self.assertEqual(role_name, "worker")
                (agent_worktree / "src/app.py").write_text(
                    "VALUE = 'base'\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(
                    0,
                    worker_result(
                        "Resolve publication conflict",
                        "Keeps the latest base version while resolving "
                        "the conflict.",
                        "LatestBasePublicationTest: PASS",
                    ),
                    "worker-session",
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
                "loop_manager.run_codex", side_effect=choose_latest_base
            ) as resolving_agent, mock.patch(
                "loop_manager.verify"
            ) as verify:
                second = loop_manager.run_project(
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
            plan = connection.execute(
                "SELECT status, last_error FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()
            rebase_in_progress = loop_manager._rebase_in_progress_for_branch(
                worktree, branch
            )

        self.assertEqual(second, 0)
        self.assertIsNone(blocked["phase"])
        self.assertIn(
            "rebase 충돌 해결 후 Worker 변경이 남지 않아",
            plan["last_error"],
        )
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertFalse(
            loop_manager._rebase_in_progress_for_branch(worktree, branch)
        )
        resolving_agent.assert_called_once()
        verify.assert_not_called()


@isolated_git_test
class PublicationRebaseInterruptionTests(PublicationRebaseRecoveryTestFixture, unittest.TestCase):
    # Real rebase metadata, conflict files and autostash/index preservation must
    # survive Agent pause and validation failure before the same recovery resumes.
    @dummy_worker_sessions
    def test_rebase_recovery_marker_survives_agent_pause(self):
        cases = (
            (
                loop_manager.AgentResultKind.TRANSIENT_FAILURE,
                loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                loop_manager.TRANSIENT_AGENT_ERROR_PREFIX,
                loop_manager.MAX_TRANSIENT_AGENT_RETRIES + 1,
            ),
            (
                loop_manager.AgentResultKind.USAGE_LIMIT,
                loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE,
                loop_manager.USAGE_LIMIT_ERROR_PREFIX,
                1,
            ),
        )
        with RecoveryBoundary() as boundary:
            directory = boundary.directory
            for result_kind, pause_kind, error_prefix, expected_calls in cases:
                with self.subTest(result_kind=result_kind):
                    if not boundary.saved:
                        (
                            repo,
                            state_dir,
                            connection,
                            _plan,
                            branch,
                            worktree,
                        ) = self._publication_state(directory)
                        (repo / "src/app.py").write_text(
                            "VALUE = 'base'\n", encoding="utf-8"
                        )
                        commit_and_push(repo, "Create publication conflict")

                        with mock.patch(
                            "loop_manager.validate_repository", return_value=repo
                        ), mock.patch(
                            "loop_manager.remote_base", return_value="main"
                        ), mock.patch(
                            "loop_manager.ensure_safe_base_worktree"
                        ), mock.patch(
                            "loop_manager.pr_for_branch", return_value=None
                        ), mock.patch(
                            "loop_manager.run_codex"
                        ) as agent:
                            first = loop_manager.run_project(
                                connection,
                                state_dir,
                                str(repo),
                                Path("unused.ini"),
                                "main",
                                False,
                            )

                        self.assertEqual(first, 1)
                        self.assertTrue(
                            loop_manager._rebase_in_progress_for_branch(worktree, branch)
                        )
                        agent.assert_not_called()

                        boundary.save(connection)
                    else:
                        boundary.restore(connection)
                    paused_result = loop_manager.CodexResult(
                        1,
                        "external agent unavailable",
                        "worker-session",
                        result_kind,
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
                        "loop_manager.run_codex", return_value=paused_result
                    ) as paused_agent:
                        paused = loop_manager.run_project_result(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                    stored = connection.execute(
                        "SELECT phase, last_error, worker_session_id "
                        "FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(paused.kind, pause_kind)
                    self.assertEqual(stored["phase"], "WORKING")
                    self.assertTrue(
                        stored["last_error"].startswith(
                            loop_manager.REBASE_CONFLICT_PREFIX
                        )
                    )
                    self.assertIn(error_prefix, stored["last_error"])
                    self.assertEqual(stored["worker_session_id"], "worker-session")
                    self.assertEqual(paused_agent.call_count, expected_calls)
                    self.assertTrue(
                        loop_manager._rebase_in_progress_for_branch(worktree, branch)
                    )

                    def resumed_worker(
                        _worktree,
                        _state,
                        _config,
                        role_name,
                        _prompt,
                        **kwargs,
                    ):
                        self.assertEqual(role_name, "worker")
                        self.assertEqual(kwargs["session_id"], "worker-session")
                        self.assertTrue(
                            loop_manager._rebase_in_progress_for_branch(
                                worktree, branch
                            )
                        )
                        return loop_manager.CodexResult(
                            1,
                            "stop after recovery assertion",
                            "worker-session",
                            loop_manager.AgentResultKind.USAGE_LIMIT,
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
                        "loop_manager.run_codex", side_effect=resumed_worker
                    ) as resumed_agent:
                        resumed = loop_manager.run_project_result(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                    preserved = connection.execute(
                        "SELECT phase, worker_session_id FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    plan_status = connection.execute(
                        "SELECT status FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-014'",
                        (str(repo),),
                    ).fetchone()["status"]
                    # Retry exhaustion and the actual blocked conflict/index
                    # are covered by test_publication_rebase_conflict_blocks_when_worker_cannot_resolve.
                    # This regression ends once the original recovery resumes.
                    self.assertEqual(resumed.kind, loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE)
                    self.assertEqual(preserved["phase"], "WORKING")
                    self.assertEqual(preserved["worker_session_id"], "worker-session")
                    self.assertEqual(plan_status, "IMPLEMENTING")
                    self.assertEqual(resumed_agent.call_count, 1)
                    self.assertTrue(loop_manager._rebase_in_progress_for_branch(worktree, branch))


    @dummy_worker_sessions
    def test_rebase_recovery_marker_survives_worker_result_validation_failure(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            (
                repo,
                state_dir,
                connection,
                _plan,
                branch,
                worktree,
            ) = self._publication_state(directory)
            (repo / "src/app.py").write_text(
                "VALUE = 'base'\n", encoding="utf-8"
            )
            commit_and_push(repo, "Create publication conflict")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as agent:
                first = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            self.assertEqual(first, 1)
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            agent.assert_not_called()

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
                return_value=loop_manager.CodexResult(
                    0,
                    "invalid worker result",
                    "worker-session",
                ),
            ):
                invalid = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            stored = connection.execute(
                "SELECT phase, last_error, worker_session_id "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(invalid, 1)
            self.assertEqual(stored["phase"], "WORKING")
            self.assertTrue(
                stored["last_error"].startswith(
                    loop_manager.REBASE_CONFLICT_PREFIX
                )
            )
            self.assertIn("Worker 결과 검증 실패", stored["last_error"])
            self.assertEqual(stored["worker_session_id"], "worker-session")
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )

            resumed_prompts = []

            def resumed_worker(
                _worktree,
                _state,
                _config,
                role_name,
                prompt,
                **kwargs,
            ):
                self.assertEqual(role_name, "worker")
                resumed_prompts.append(prompt)
                if len(resumed_prompts) == 1:
                    self.assertIn("Worker 결과 검증 실패", prompt)
                else:
                    self.assertIn("rebase 충돌 해결 필요", prompt)
                    self.assertIn("stop after recovery assertion", prompt)
                self.assertEqual(kwargs["session_id"], "worker-session")
                self.assertTrue(
                    loop_manager._rebase_in_progress_for_branch(
                        worktree, branch
                    )
                )
                return loop_manager.CodexResult(
                    1,
                    "stop after recovery assertion",
                    "worker-session",
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
                "loop_manager.run_codex", side_effect=resumed_worker
            ) as resumed_agent:
                resumed = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            blocked = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan_status = connection.execute(
                "SELECT status FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-014'",
                (str(repo),),
            ).fetchone()["status"]

        self.assertEqual(resumed, 0)
        self.assertIsNone(blocked["phase"])
        self.assertEqual(plan_status, "PLAN_BLOCKED")
        # The invalid result was already the first correction occurrence;
        # three further abnormal results reach the no-progress safety gate.
        self.assertEqual(resumed_agent.call_count, 3)
        self.assertEqual(len(resumed_prompts), 3)


@isolated_git_test
class PublicationRebaseLeaseTests(PublicationRebaseRecoveryTestFixture, unittest.TestCase):
    def test_pr_conflict_push_preserves_config_and_exact_lease(self):
        branch, old, new, base = 'loop-manager/G-081', 'a' * 40, 'b' * 40, 'c' * 40
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {'base_branch': 'main'}
        plan = {
            'plan_id': 'G-081', 'status': 'PUBLISHING', 'workspace_owned': 1,
            'pr_conflict_recovery': 1, 'branch': branch, 'worktree': '/managed',
            'pr_url': 'https://example.test/pr/81', 'pr_conflict_head_oid': old,
            'pr_conflict_base_oid': base, 'active_sha': base, 'validated_base_sha': base,
        }
        pr = {'url': plan['pr_url'], 'state': 'OPEN', 'headRefName': branch, 'headRefOid': old}
        calls = []

        def git(_path, *arguments, **_kwargs):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0,
                new + '\n' if arguments[0] == 'rev-parse' else '')

        with (
            mock.patch('loop_manager.active_execplan', return_value=plan),
            mock.patch('loop_manager.validate_execplan_workspace_identity'),
            mock.patch('loop_manager.validate_managed_worktree'),
            mock.patch('loop_manager._fetch_pr_conflict_recovery_refs'),
            mock.patch('loop_manager._recorded_base_fetch'),
            mock.patch('loop_manager.remote_branch_oid', side_effect=[old, new]),
            mock.patch('loop_manager.pr_for_branch', return_value=pr),
            mock.patch('loop_manager.git', side_effect=git),
        ):
            self.assertEqual(loop_manager.push_owned_pr_conflict_recovery(
                connection, Path('/repo'), Path('/state'), 'repo', branch,
                Path('/managed'), pr, new), new)
        self.assertEqual([args for args in calls if args[0] == 'push'], [(
            'push', f'--force-with-lease=refs/heads/{branch}:{old}',
            'origin', f'{new}:refs/heads/{branch}')])
        self.assertFalse(any(args[0] == 'config' for args in calls))
