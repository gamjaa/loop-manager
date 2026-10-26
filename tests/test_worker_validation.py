from tests.support.worker import ActiveWorkerFixture
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, commit_and_push, isolated_git_test
from tests.support.worker import worker_result
from tests.support.publication import matching_branch_pr_lookup, publishing_branch_pr_lookup
from tests.support.worker import dummy_worker_sessions
from tests.support.execution import execution_policy_fixture, run_execution_policy


@isolated_git_test
class WorkerValidationTest(ActiveWorkerFixture, unittest.TestCase):

    @dummy_worker_sessions
    def test_worker_without_diff_requeues_after_source_change(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            managed = Path(
                connection.execute(
                    "SELECT worktree FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["worktree"]
            )
            planning_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, worktree = ?,
                    planning_fingerprint = ?, processed_sha = ?
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    str(managed),
                    planning_fingerprint,
                    plan_sha,
                    str(repo),
                ),
            )
            connection.commit()
            roles = []

            def worker(worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                if role_name == "planner":
                    write_execplan(
                        worktree,
                        "G-002",
                        "Deferred",
                        priority="low",
                    )
                    return loop_manager.CodexResult(0, "", "planner-session")
                if roles.count("worker") == 1:
                    return loop_manager.CodexResult(0, worker_result(), "worker-session")
                return loop_manager.CodexResult(1, "stop after source change", "worker-session")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=worker
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS")
            ) as verify:
                blocked_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

                blocked_project = connection.execute(
                    """
                    SELECT phase, worktree, pr_url, last_error, verify_result
                    FROM projects WHERE repo = ?
                    """,
                    (str(repo),),
                ).fetchone()
                blocked_plan = connection.execute(
                    """
                    SELECT status, worktree, pr_url, last_error, verify_result,
                           blocked_fingerprint
                    FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (str(repo),),
                ).fetchone()
                worktree_preserved = Path(blocked_plan["worktree"]).exists()
                connection.execute(
                    "UPDATE exec_plans SET retry_count = 2 WHERE repo = ? AND plan_id = 'G-001'",
                    (str(repo),),
                )
                connection.commit()
                git_command(repo, "worktree", "remove", "--force", "--force",
                            str(managed))
                self.assertFalse(managed.exists())

                unchanged_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                unchanged_plan = connection.execute(
                    "SELECT status, retry_count FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(repo),),
                ).fetchone()
                worktree_restored = managed.is_dir()

                blocked_plan_path = managed / "docs/plans/[G-001] Worker Boundary.md"
                original_plan_text = blocked_plan_path.read_text(encoding="utf-8")
                blocked_plan_path.write_text(
                    original_plan_text.replace("priority: normal", "priority: critical"),
                    encoding="utf-8",
                )
                metadata_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )
                rejected_metadata = connection.execute(
                    """
                    SELECT status, priority, depends_on, last_error, retry_count
                    FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (str(repo),),
                ).fetchone()
                blocked_plan_path.write_text(original_plan_text, encoding="utf-8")

                (repo / "docs/PRODUCT.md").write_text(
                    "# Product changed to resolve block\n",
                    encoding="utf-8",
                )
                commit_and_push(repo, "Change blocked plan source")
                resumed_result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            resumed_project = connection.execute(
                """
                SELECT active_sha, phase, worktree, pr_url, last_error
                FROM projects WHERE repo = ?
                """,
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                """
                SELECT status, pr_url, last_error, blocked_fingerprint, retry_count
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'
                """,
                (str(repo),),
            ).fetchone()
            rebased_source = (managed / "docs/PRODUCT.md").read_text(encoding="utf-8")
            managed_head = git_command(managed, "rev-parse", "HEAD")
            origin_head = git_command(repo, "rev-parse", "origin/main")

        self.assertEqual(
            (blocked_result, unchanged_result, metadata_result, resumed_result),
            (0, 0, 0, 1),
        )
        self.assertEqual(roles, ["worker", "planner", "worker"])
        verify.assert_not_called()
        self.assertIsNone(blocked_project["phase"])
        self.assertEqual(blocked_plan["verify_result"], "")
        self.assertIsNone(blocked_project["pr_url"])
        self.assertTrue(worktree_preserved)
        self.assertIn("PR을 생성할 수 없습니다", blocked_plan["last_error"])
        self.assertEqual(blocked_plan["status"], "PLAN_BLOCKED")
        self.assertIsNotNone(blocked_plan["blocked_fingerprint"])
        self.assertEqual(tuple(unchanged_plan), ("PLAN_BLOCKED", 2))
        self.assertTrue(worktree_restored)
        self.assertEqual(rejected_metadata["status"], "PLAN_BLOCKED")
        self.assertEqual(rejected_metadata["priority"], "normal")
        self.assertEqual(rejected_metadata["depends_on"], "[]")
        self.assertIn(
            "reconciled catalog와 다릅니다",
            rejected_metadata["last_error"],
        )
        self.assertEqual(rejected_metadata["retry_count"], 2)
        self.assertEqual(resumed_project["phase"], "WORKING")
        self.assertEqual(resumed_project["last_error"], "stop after source change")
        self.assertEqual(resumed_project["active_sha"], managed_head)
        self.assertEqual(resumed_project["active_sha"], origin_head)
        self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
        self.assertIsNone(resumed_plan["blocked_fingerprint"])
        self.assertEqual(resumed_plan["retry_count"], 0)
        self.assertEqual(rebased_source, "# Product changed to resolve block\n")

    @dummy_worker_sessions
    def test_rebase_that_drops_worker_patch_blocks_before_review(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            plan_sha = git_command(repo, "rev-parse", "origin/main")
            managed = Path(
                connection.execute(
                    "SELECT worktree FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["worktree"]
            )
            connection.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, worktree = ?
                WHERE repo = ?
                """,
                (plan_sha, plan_sha, str(managed), str(repo)),
            )
            connection.commit()
            (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
            commit_and_push(repo, "Apply worker change upstream")
            roles = []

            def worker(worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                return loop_manager.CodexResult(
                    0,
                    worker_result("Apply worker change"),
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
                "loop_manager.run_codex", side_effect=worker
            ), mock.patch(
                "loop_manager.verify", return_value=(True, "verify: PASS"),
            ) as verify:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                """
                SELECT status, last_error, blocked_fingerprint
                FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'
                """,
                (str(repo),),
            ).fetchone()
            ahead = git_command(managed, "rev-list", "--count", "origin/main..HEAD")

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["worker"])
        # The revision gate now refreshes the base before Worker dispatch, so
        # it never validates the already-absorbed patch on the stale base.
        verify.assert_not_called()
        self.assertEqual(ahead, "0")
        self.assertIsNone(project["phase"])
        self.assertIn("Worker가 변경을 만들지 않아", plan["last_error"])
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIsNotNone(plan["blocked_fingerprint"])

    def test_repeated_verify_failure_blocks_in_one_run_without_reviewer(self):
        fixture = execution_policy_fixture(self)
        roles, sessions, prompts = [], [], []

        def agent(*args, **kwargs):
            role_name = args[4]
            roles.append(role_name)
            self.assertEqual(role_name, "worker")
            sessions.append(kwargs["session_id"])
            prompts.append(args[5])
            kwargs["on_session_started"]("worker-session")
            fixture.dirty = True
            return loop_manager.AgentResult(
                0, worker_result(), session_id="worker-session",
            )

        fixture.effects.agent.run_agent.side_effect = agent
        verify = fixture.effects.validation.verify
        verify.side_effect = None
        verify.return_value = (False, "verify failed")

        def observed_git(_path, *args, **_kwargs):
            if args == ("rev-parse", "--verify", "base^{commit}"):
                return mock.Mock(returncode=0, stdout="base\n")
            if len(args) == 2 and args[0] == "show" and args[1].startswith("base:"):
                return mock.Mock(returncode=0, stdout="fixture revision content\n")
            raise AssertionError(f"unexpected policy Git: {args}")

        with mock.patch.object(loop_manager, "git", side_effect=observed_git), mock.patch.object(
            loop_manager, "capture_git_snapshot",
            side_effect=fixture.effects.agent.capture_git_snapshot,
        ):
            result = run_execution_policy(fixture)

        project = fixture.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (fixture.repo,),
        ).fetchone()
        plan = fixture.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-047'",
            (fixture.repo,),
        ).fetchone()
        history = json.loads(plan["verify_failure_history"])

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(plan["worker_session_id"], "worker-session")
        self.assertEqual(plan["worker_result"], worker_result())
        self.assertEqual(plan["verify_result"], "verify failed")
        self.assertIn("실패 지문:", plan["last_error"])
        self.assertEqual(sessions, [None] + ["worker-session"] * 3)
        self.assertEqual(roles, ["worker"] * 4)
        fingerprint = next(iter(history))
        self.assertIn(
            f"verify:{fingerprint} occurrence=1 revision=1\n"
            "verify exit code: unavailable\nverify failed",
            prompts[1],
        )
        self.assertNotIn("전체 검증 실패:\nverify failed", prompts[1])
        self.assertIn(
            f"verify:{fingerprint} occurrence=2 revision=2", prompts[2]
        )
        self.assertNotIn("verify failed", prompts[2])
        self.assertEqual(verify.call_count, 4)
        self.assertEqual(
            [record["occurrences"] for record in history.values()], [4]
        )

    @dummy_worker_sessions
    def test_rebase_verify_failure_recovers_after_initial_success(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            sessions = []
            reviewed = False
            loop_manager.migrate_legacy_execplans(
                connection, target_repo=str(repo)
            )
            origin_sha = git_command(repo, "rev-parse", "origin/main")
            origin_tree = git_command(repo, "rev-parse", "origin/main^{tree}")
            advanced_sha = git_command(
                repo,
                "commit-tree",
                origin_tree,
                "-p",
                origin_sha,
                "-m",
                "Advance base for rebase verification",
            )
            git_command(repo, "push", "origin", f"{advanced_sha}:main")
            managed = Path(
                connection.execute(
                    "SELECT worktree FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()["worktree"]
            )
            for _ in range(3):
                loop_manager.transition_validation_failure(
                    connection, str(repo), managed, "verify", "rebased verify failed"
                )

            def worker(worktree, _state, _config, role_name, _prompt, **kwargs):
                nonlocal reviewed
                if role_name == "reviewer":
                    reviewed = True
                    return loop_manager.CodexResult(
                        0, "VERDICT: PASS", "reviewer-session"
                    )
                self.assertEqual(role_name, "worker")
                sessions.append(kwargs["session_id"])
                (worktree / "src/app.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(
                    0,
                    worker_result("Rebase failure context"),
                    "worker-session",
                )

            verify_results = [(True, "first verify")]
            clear_history = loop_manager.clear_validation_failure_history
            cleared_histories = []

            def track_clear_history(connection, project_repo, stage):
                clear_history(connection, project_repo, stage)
                if stage == "verify":
                    cleared_histories.append(
                        json.loads(
                            connection.execute(
                                "SELECT verify_failure_history FROM exec_plans "
                                "WHERE repo = ? AND plan_id = 'G-001'",
                                (project_repo,),
                            ).fetchone()["verify_failure_history"]
                        )
                    )

            with mock.patch('loop_manager_internal.git.publication_service.push_worker_checkpoint_commits',
                            return_value=(None, 0)), mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch",
                side_effect=matching_branch_pr_lookup(lambda *_args, **_kwargs: (
                    {"url": "https://example.test/pr/1"}
                    if reviewed else None
                )),
            ), mock.patch(
                "loop_manager.run_codex", side_effect=worker
            ), mock.patch(
                "loop_manager.verify", side_effect=verify_results
            ) as verify, mock.patch(
                "loop_manager.clear_validation_failure_history",
                side_effect=track_clear_history,
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

            row = connection.execute(
                "SELECT phase, last_error FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, last_error, verify_result, verify_failure_history FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'", (str(repo),)
            ).fetchone()

        self.assertIsNone(row["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertIsNone(plan["last_error"])
        self.assertEqual(sessions, [None])
        verify.assert_called_once()
        self.assertEqual(plan["verify_result"], "first verify")
        self.assertEqual(cleared_histories, [{}])
        self.assertEqual(json.loads(plan["verify_failure_history"]), {})

    @dummy_worker_sessions
    def test_verify_success_keeps_worker_and_verify_logs_out_of_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, state_dir, connection = self._make_active_worker_project(directory)
            managed = Path(connection.execute('SELECT worktree FROM projects').fetchone()[0])
            exclude = Path(git_command(managed, 'rev-parse', '--git-path', 'info/exclude'))
            if not exclude.is_absolute():
                exclude = managed / exclude
            exclude.write_text(exclude.read_text() + '\n__pycache__/\n')
            artifact = managed / '__pycache__' / 'targeted.pyc'
            artifact.parent.mkdir(exist_ok=True)
            calls = []
            sequence = []
            logs = []

            def agent(worktree, _state, _config, role_name, prompt, **kwargs):
                self.assertEqual(worktree, managed)
                calls.append((role_name, prompt, kwargs))
                if role_name == "worker":
                    sequence.append('targeted')
                    artifact.write_bytes(b'targeted test artifact')
                    (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                    return loop_manager.CodexResult(0, worker_result("Persist worker change"), "worker-session")
                self.assertEqual(role_name, "reviewer")
                sequence.append('reviewer')
                self.assertEqual(artifact.read_bytes(), b'verify artifact')
                artifact.unlink()
                return 0, "VERDICT: PASS"

            def full_verify(worktree, *_args, **_kwargs):
                self.assertEqual(worktree, managed)
                sequence.append('verify')
                self.assertEqual(artifact.read_bytes(), b'targeted test artifact')
                artifact.write_bytes(b'verify artifact')
                return True, 'verify: PASS'

            with mock.patch('loop_manager_internal.git.publication_service.push_worker_checkpoint_commits',
                            return_value=(None, 0)), mock.patch("loop_manager.validate_repository", return_value=repo), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.ensure_safe_base_worktree"), mock.patch(
                "loop_manager.pr_for_branch", side_effect=publishing_branch_pr_lookup(
                    connection, "https://example.test/pr/1"
                )
            ), mock.patch("loop_manager.run_codex", side_effect=agent), mock.patch(
                "loop_manager.verify", side_effect=full_verify
            ) as verify, mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                verify.assert_called_once()

            self.assertEqual(sequence, ['targeted', 'verify', 'reviewer'])
            self.assertFalse(artifact.exists())
            self.assertEqual(logs[:3], [("Worker 준비: G-001 Worker Boundary", {}), ("전체 verify 시작: G-001 Worker Boundary", {}), ("Reviewer 준비: G-001 Worker Boundary (최종 Review)", {})])
            self.assertEqual([role for role, _prompt, _kwargs in calls], ["worker", "reviewer"])
            reviewer_prompt = calls[1][1]
            self.assertIn("Worker 보고:\ntargeted test: PASS", reviewer_prompt)
            self.assertIn("Manager 확인: 전체 verify PASS", reviewer_prompt)
            self.assertIn("candidate HEAD=", reviewer_prompt)
            self.assertNotIn("verify: PASS", reviewer_prompt)
            self.assertNotIn("테스트 command와 scripts/verify를 실행하지 말고", reviewer_prompt)

            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(
                git_command(Path(plan["worktree"]), "log", "-1", "--pretty=%s"),
                "Persist worker change",
            )
            self.assertEqual(
                git_command(repo, "log", "-1", "--pretty=%s"),
                "Add worker plan",
            )
            self.assertIsNone(row["phase"])
            self.assertEqual(plan["status"], "PR_OPEN")
            self.assertEqual(plan["worker_session_id"], "worker-session")
            self.assertEqual(plan["verify_result"], "verify: PASS")
