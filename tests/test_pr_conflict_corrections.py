from tests.support.recovery_boundary import RecoveryBoundary
from tests.support.pr_recovery import PrRecoveryFixture, observe_live_pull_request
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.git import git_command
from tests.support.worker import worker_result
from tests.support.worker import dummy_worker_sessions


class LivePullRequestObservationTests(unittest.TestCase):
    def test_live_observer_reads_remote_and_preserves_saved_pr_identity(self):
        saved = {'url': 'saved-pr', 'state': 'OPEN', 'headRefName': 'branch',
                 'headRefOid': 'old', 'baseRefName': 'main'}
        with mock.patch('tests.support.pr_recovery.git_command',
                        return_value='new\trefs/heads/branch\n') as git:
            observed = observe_live_pull_request(Path('/repo'), saved)
        git.assert_called_once_with(Path('/repo'), 'ls-remote', 'origin', 'refs/heads/branch')
        self.assertEqual(observed, {**saved, 'headRefOid': 'new'})
        self.assertEqual(saved['headRefOid'], 'old')

    def test_live_observer_keeps_absent_pr_and_rejects_absent_or_ambiguous_ref(self):
        with mock.patch('tests.support.pr_recovery.git_command') as git:
            self.assertIsNone(observe_live_pull_request(Path('/repo'), None))
            git.assert_not_called()
            for output in ('', 'malformed', 'new\trefs/heads/other',
                           'new\trefs/heads/branch\nold\trefs/heads/branch'):
                with self.subTest(output=output):
                    git.return_value = output
                    with self.assertRaises(AssertionError):
                        observe_live_pull_request(Path('/repo'), {'headRefName': 'branch'})


class PrConflictCorrectionsTestFixture(PrRecoveryFixture):
class PrConflictLeaseTests(PrConflictCorrectionsTestFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_conflicting_pr_reuses_sessions_revalidates_new_base_and_leases_push(self):
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
            self._advance_base(
                directory,
                repo,
                "src/app.py",
                "VALUE = 3\n",
                "Create PR conflict",
            )
            source_before = (
                git_command(repo, "rev-parse", "HEAD"),
                git_command(repo, "symbolic-ref", "--short", "HEAD"),
                git_command(repo, "rev-parse", "refs/heads/main"),
                git_command(repo, "status", "--porcelain"),
            )
            pull_request = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            started = loop_manager.reconcile_existing_pull_request(
                connection,
                repo,
                state_dir,
                str(repo),
                project,
                pull_request,
            )
            durable = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                       pr_conflict_base_oid, worker_session_id,
                       reviewer_session_id
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            self.assertIsNone(started)
            self.assertEqual(durable["status"], "IMPLEMENTING")
            self.assertEqual(durable["pr_conflict_recovery"], 1)
            self.assertEqual(durable["pr_conflict_head_oid"], head_oid)
            self.assertEqual(
                durable["pr_conflict_base_oid"],
                git_command(repo, "rev-parse", "origin/main"),
            )
            self.assertEqual(durable["worker_session_id"], "worker-existing")
            self.assertEqual(
                durable["reviewer_session_id"], "reviewer-existing"
            )
            self.assertFalse(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertTrue(cache.is_file())

            roles = []
            session_ids = []
            review_count = 0
            pushes = []
            created = []
            original_git = loop_manager.git
            original_command = loop_manager.command

            def agent_runner(
                agent_worktree,
                _state,
                _config,
                role_name,
                _prompt,
                **kwargs,
            ):
                nonlocal review_count
                roles.append(role_name)
                session_ids.append(kwargs["session_id"])
                if role_name == "worker":
                    (agent_worktree / "src/app.py").write_text(
                        "VALUE = 4\n", encoding="utf-8"
                    )
                    return loop_manager.CodexResult(
                        0,
                        worker_result(
                            "Resolve owned PR conflict",
                            "Rebases and resolves the existing owned PR.",
                            "AutomaticOwnedPrConflictRecoveryTest: PASS",
                        ),
                        "worker-existing",
                    )
                review_count += 1
                if review_count == 1:
                    self._advance_base(
                        directory,
                        repo,
                        "docs/notes.md",
                        "# Base moved during review\n",
                        "Advance base during recovery review",
                    )
                return loop_manager.CodexResult(
                    0, "VERDICT: PASS", "reviewer-existing"
                )

            def recording_git(path, *arguments, **kwargs):
                if arguments and arguments[0] == "push":
                    pushes.append(arguments)
                return original_git(path, *arguments, **kwargs)

            def recording_command(arguments, **kwargs):
                if arguments[:3] == ["gh", "pr", "create"]:
                    created.append(arguments)
                    raise AssertionError("conflict recovery must not create a PR")
                return original_command(arguments, **kwargs)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ) as synchronize, mock.patch(
                "loop_manager.pr_for_branch", side_effect=lambda *_a, **_k:
                    observe_live_pull_request(repo, pull_request)
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify, mock.patch(
                "loop_manager.run_codex", side_effect=agent_runner
            ), mock.patch(
                "loop_manager.git", side_effect=recording_git
            ), mock.patch(
                "loop_manager.command", side_effect=recording_command
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
                """
                SELECT status, validated_base_sha, publication_remote_oid,
                       pr_url, pr_head_oid, pr_conflict_recovery,
                       pr_conflict_head_oid, pr_conflict_base_oid,
                       worker_session_id, reviewer_session_id
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            final_head = git_command(worktree, "rev-parse", "HEAD")
            source_after = (
                git_command(repo, "rev-parse", "HEAD"),
                git_command(repo, "symbolic-ref", "--short", "HEAD"),
                git_command(repo, "rev-parse", "refs/heads/main"),
                git_command(repo, "status", "--porcelain"),
            )
            lease = f"--force-with-lease=refs/heads/{branch}:{head_oid}"

            self.assertEqual(outcome.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(stored["status"], "PR_OPEN")
            self.assertEqual(stored["publication_remote_oid"], final_head)
            self.assertEqual(stored["pr_head_oid"], final_head)
            self.assertEqual(stored["pr_url"], pr_url)
            self.assertEqual(stored["pr_conflict_recovery"], 0)
            self.assertIsNone(stored["pr_conflict_head_oid"])
            self.assertIsNone(stored["pr_conflict_base_oid"])
            self.assertEqual(stored["worker_session_id"], "worker-existing")
            self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
            self.assertEqual(roles, ["worker", "worker", "reviewer", "worker", "reviewer"])
            self.assertEqual(
                session_ids,
                ["worker-existing", "worker-existing", "reviewer-existing",
                 "worker-existing", "reviewer-existing"],
            )
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(len(pushes), 2)
            self.assertIn(lease, pushes[0])
            first_checkpoint = pushes[0][-1].split(":", 1)[0]
            self.assertIn(f"--force-with-lease=refs/heads/{branch}:{first_checkpoint}", pushes[1])
            for pushed in pushes:
                self.assertNotIn("--force", pushed)
            self.assertEqual(created, [])
            self.assertEqual(source_after, source_before)
            synchronize.assert_not_called()


class PrConflictCauseRevalidationTests(PrConflictCorrectionsTestFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_worker_failure_blocks_after_three_corrections_and_stays_blocked(self):
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
            self._advance_base(
                directory,
                repo,
                "src/app.py",
                "VALUE = 3\n",
                "Conflict for failed Worker",
            )
            pull_request = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )
            failed_worker = loop_manager.CodexResult(
                1,
                "Worker cannot resolve this conflict",
                "worker-existing",
                loop_manager.AgentResultKind.PERMANENT_CONFIGURATION,
            )
            roles = []

            def failed_agent(
                _worktree,
                _state,
                _config,
                role_name,
                _prompt,
                **_kwargs,
            ):
                roles.append(role_name)
                if role_name == 'worker':
                    return failed_worker
                return loop_manager.CodexResult(failed_worker.code, failed_worker.message, 'failed-planner')

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=pull_request
            ), mock.patch(
                "loop_manager.run_codex", side_effect=failed_agent
            ) as agent:
                first = loop_manager.run_project_result(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                second = loop_manager.run_project_result(
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
            self.assertEqual(first.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(second.kind, loop_manager.ProjectRunKind.WAITING)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertEqual(stored["pr_conflict_recovery"], 1)
            self.assertEqual(stored["pr_conflict_head_oid"], head_oid)
            self.assertEqual(stored["worker_session_id"], "worker-existing")
            self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
            self.assertTrue(
                loop_manager._rebase_in_progress_for_branch(worktree, branch)
            )
            self.assertEqual(roles, ["worker", "worker", "worker", "worker", "planner"])
            self.assertEqual(agent.call_count, 5)


    def test_repeated_verify_fingerprint_blocks_recovery_without_auto_resume(self):
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
            pull_request = self._pull_request(
                branch, pr_url, head_oid, "CONFLICTING"
            )
            project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertIsNone(
                loop_manager.reconcile_existing_pull_request(
                    connection, repo, state_dir, str(repo), project, pull_request
                )
            )
            output = (
                "FAILED tests/test_conflict.py::test_owned_pr_recovery\n"
                "AssertionError: same recovery failure"
            )
            decisions = []
            for _attempt in range(
                loop_manager.MAX_VALIDATION_FINGERPRINT_REPEATS + 1
            ):
                loop_manager.update_project(
                    connection, str(repo), phase="VERIFYING"
                )
                decisions.append(
                    loop_manager.transition_validation_failure(
                        connection,
                        str(repo),
                        worktree,
                        "verify",
                        output,
                    )
                )

            blocked_plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                (str(repo),),
            ).fetchone()
            loop_manager.hydrate_execplan_project(
                connection, str(repo), blocked_plan
            )
            blocked_project = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(
                loop_manager.reconcile_existing_pull_request(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                    blocked_project,
                    pull_request,
                ),
                1,
            )
            stored = connection.execute(
                """
                SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                       verify_failure_history
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                """,
                (str(repo),),
            ).fetchone()
            history = json.loads(stored["verify_failure_history"])
            self.assertFalse(any(item.blocked for item in decisions[:-1]))
            self.assertTrue(decisions[-1].blocked)
            self.assertEqual(stored["status"], "PLAN_BLOCKED")
            self.assertEqual(stored["pr_conflict_recovery"], 1)
            self.assertEqual(stored["pr_conflict_head_oid"], head_oid)
            self.assertEqual(
                next(iter(history.values()))["occurrences"],
                loop_manager.MAX_VALIDATION_FINGERPRINT_REPEATS + 1,
            )


class PrConflictInterruptionTests(PrConflictCorrectionsTestFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_transient_and_usage_limit_pauses_resume_same_recovery(self):
        pauses = (
            (
                loop_manager.AgentResultKind.TRANSIENT_FAILURE,
                loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                "temporary provider failure",
            ),
            (
                loop_manager.AgentResultKind.USAGE_LIMIT,
                loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE,
                "usage limit reached",
            ),
        )
        with RecoveryBoundary() as boundary:
            directory = boundary.directory
            for result_kind, expected_kind, message in pauses:
                with self.subTest(result_kind=result_kind):
                    if not boundary.saved:
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
                            "src/app.py",
                            "VALUE = 3\n",
                            "Conflict before pause variants",
                        )
                        git_command(repo, "fetch", "origin")
                        source_before = self._source_checkout_snapshot(repo)
                        pull_request = self._pull_request(
                            branch, pr_url, head_oid, "CONFLICTING"
                        )
                        boundary.save(connection)
                    else:
                        boundary.restore(connection)
                    paused_worker = loop_manager.CodexResult(
                        1,
                        message,
                        "worker-existing",
                        result_kind,
                    )

                    with mock.patch(
                        "loop_manager.validate_repository", return_value=repo
                    ), mock.patch(
                        "loop_manager.pr_for_branch", side_effect=lambda *_a, **_k:
                            observe_live_pull_request(repo, pull_request)
                    ), mock.patch(
                        "loop_manager.run_codex", return_value=paused_worker
                    ):
                        paused = loop_manager.run_project_result(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                    preserved = connection.execute(
                        """
                        SELECT status, pr_conflict_recovery, pr_conflict_head_oid,
                               worker_session_id, reviewer_session_id
                        FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'
                        """,
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(paused.kind, expected_kind)
                    self.assertEqual(preserved["status"], "IMPLEMENTING")
                    self.assertEqual(preserved["pr_conflict_recovery"], 1)
                    self.assertEqual(preserved["pr_conflict_head_oid"], head_oid)
                    self.assertEqual(preserved["worker_session_id"], "worker-existing")
                    self.assertEqual(preserved["reviewer_session_id"], "reviewer-existing")
                    self.assertEqual(
                        self._source_checkout_snapshot(repo), source_before
                    )

                    roles = []

                    def successful_agent(
                        agent_worktree,
                        _state,
                        _config,
                        role_name,
                        _prompt,
                        **_kwargs,
                    ):
                        roles.append(role_name)
                        if role_name == "worker":
                            (agent_worktree / "src/app.py").write_text(
                                "VALUE = 4\n", encoding="utf-8"
                            )
                            return loop_manager.CodexResult(
                                0,
                                worker_result(
                                    "Resume paused PR recovery",
                                    "Resolves the preserved conflict.",
                                    "AutomaticOwnedPrConflictRecoveryTest: PASS",
                                ),
                                "worker-existing",
                            )
                        return loop_manager.CodexResult(
                            0, "VERDICT: PASS", "reviewer-existing"
                        )

                    with mock.patch(
                        "loop_manager.validate_repository", return_value=repo
                    ), mock.patch(
                        "loop_manager.pr_for_branch", side_effect=lambda *_a, **_k:
                            observe_live_pull_request(repo, pull_request)
                    ), mock.patch(
                        "loop_manager.verify", return_value=(True, "verify: PASS")
                    ), mock.patch(
                        "loop_manager.run_codex", side_effect=successful_agent
                    ):
                        resumed = loop_manager.run_project_result(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                    final = connection.execute(
                        "SELECT status, pr_conflict_recovery FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-026'",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(resumed.kind, loop_manager.ProjectRunKind.WAITING)
                    self.assertEqual(tuple(final), ("PR_OPEN", 0))
                    self.assertEqual(roles, ["worker", "worker", "reviewer"])
                    self.assertEqual(
                        self._source_checkout_snapshot(repo), source_before
                    )


    @dummy_worker_sessions
    def test_external_command_interruptions_preserve_recovery_and_resume(self):
        with RecoveryBoundary() as boundary:
            directory = boundary.directory
            for stage in ("github", "fetch", "rebase", "push"):
                with self.subTest(stage=stage):
                    if not boundary.saved:
                        repo, state_dir, connection, branch, worktree, url, head = (
                            self._pr_open_state(directory)
                        )
                        self._make_source_checkout_dirty_and_ahead(repo)
                        base = self._advance_base(
                            directory, repo, "docs/notes.md", "# New remote base\n",
                            "Advance before interruption",
                        )
                        pull_request = self._pull_request(branch, url, head, "CONFLICTING")
                        project = connection.execute(
                            "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                        ).fetchone()
                        self.assertIsNone(loop_manager.reconcile_existing_pull_request(
                            connection, repo, state_dir, str(repo), project, pull_request
                        ))
                        source_before = self._source_checkout_snapshot(repo)
                        boundary.save(connection)
                    else:
                        boundary.restore(connection)
                    original_command = loop_manager.command
                    original_fetch = loop_manager._recorded_base_fetch
                    injected = False
                    rebases = []
                    pushes = []

                    def interrupted_command(arguments, **kwargs):
                        nonlocal injected
                        operation = arguments[3] if arguments[:2] == ["git", "-C"] else None
                        is_query = arguments[:3] == ["gh", "pr", "view"]
                        matches = {
                            "github": is_query,
                            "rebase": operation == "rebase",
                            "push": operation == "push",
                        }
                        if not injected and matches.get(stage, False):
                            injected = True
                            if stage == "github":
                                raise RuntimeError("GitHub HTTP 503 Service Unavailable")
                            output = (
                                "fatal: Unable to create index.lock: File exists"
                                if stage == "rebase"
                                else "fatal: unable to access origin: Connection timed out"
                            )
                            return subprocess.CompletedProcess(arguments, 128, stdout=output)
                        if is_query:
                            return subprocess.CompletedProcess(
                                arguments, 0, stdout=json.dumps(observe_live_pull_request(repo, pull_request))
                            )
                        result = original_command(arguments, **kwargs)
                        if operation == "rebase" and result.returncode == 0:
                            rebases.append(arguments)
                        if operation == "push":
                            pushes.append(arguments)
                        return result

                    def interrupted_fetch(*args, **kwargs):
                        nonlocal injected
                        if stage == "fetch" and not injected:
                            injected = True
                            raise RuntimeError("fatal: unable to access origin: Connection timed out")
                        return original_fetch(*args, **kwargs)

                    with mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                        "loop_manager.command", side_effect=interrupted_command
                    ), mock.patch(
                        "loop_manager._recorded_base_fetch", side_effect=interrupted_fetch
                    ), mock.patch("loop_manager.verify", return_value=(True, "verify: PASS")), mock.patch(
                        "loop_manager.run_codex",
                        side_effect=lambda _worktree, _state, _config, role, _prompt, **_kwargs:
                            loop_manager.CodexResult(0,
                                worker_result("Confirm rebased owned PR", "Checks the current final candidate.",
                                              "owned PR candidate: PASS") if role == "worker" else "VERDICT: PASS",
                                "worker-existing" if role == "worker" else "reviewer-existing"),
                    ):
                        if stage in ("github", "fetch"):
                            with self.assertRaises(RuntimeError):
                                loop_manager.run_project_result(
                                    connection, state_dir, str(repo), Path("unused.ini"), "main", False
                                )
                        else:
                            paused = loop_manager.run_project_result(
                                connection, state_dir, str(repo), Path("unused.ini"), "main", False
                            )
                            self.assertEqual(
                                paused.kind,
                                loop_manager.ProjectRunKind.WAITING if stage == "push"
                                else loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                            )
                        if stage == "push":
                            self.assertTrue(injected)
                            final = connection.execute(
                                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-026'",
                                (str(repo),),
                            ).fetchone()
                            self.assertEqual(final["status"], "PR_OPEN")
                            self.assertEqual(final["pr_conflict_recovery"], 0)
                            self.assertEqual(final["pr_url"], url)
                            self.assertEqual(len(rebases), 1)
                            self.assertEqual(len(pushes), 1)
                            self.assertEqual(self._source_checkout_snapshot(repo), source_before)
                            continue
                        self.assertTrue(injected)
                        stored = loop_manager.active_execplan(connection, str(repo))
                        self.assertEqual(
                            stored["status"], "PUBLISHING" if stage == "push" else "IMPLEMENTING"
                        )
                        self.assertEqual(stored["pr_conflict_recovery"], 1)
                        self.assertEqual(stored["pr_conflict_head_oid"], head)
                        self.assertEqual(stored["pr_conflict_base_oid"], base)
                        self.assertEqual(stored["worker_session_id"], "worker-existing")
                        self.assertEqual(stored["reviewer_session_id"], "reviewer-existing")
                        self.assertEqual(self._source_checkout_snapshot(repo), source_before)
                        self.assertTrue(worktree.is_dir())
                        self.assertEqual(pushes, [])

                        resumed = loop_manager.run_project_result(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False
                        )
                    self.assertEqual(resumed.kind, loop_manager.ProjectRunKind.WAITING)
                    final = connection.execute(
                        "SELECT * FROM exec_plans "
                        "WHERE repo = ? AND plan_id = 'G-026'",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(final["status"], "PR_OPEN")
                    self.assertEqual(final["pr_conflict_recovery"], 0)
                    self.assertEqual(final["pr_url"], url)
                    self.assertEqual(len(rebases), 1)
                    self.assertEqual(len(pushes), 1)
                    self.assertIn(f"--force-with-lease=refs/heads/{branch}:{head}", pushes[0])
                    self.assertEqual(self._source_checkout_snapshot(repo), source_before)
