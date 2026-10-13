from tests.support.fixed_samples import use_fixed_guidance
from tests.support.database import AutomationCatalogFixture
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.storage import planning_input_state
from tests.support.catalog import write_execplan
from tests.support.maintenance import request_maintenance
from tests.support.git import git_command, make_remote_repository, commit_and_push, isolated_git_test
from tests.support.worker import dummy_worker_context, dummy_worker_sessions, legacy_planning_ports


@isolated_git_test
class CatalogSchedulingTest(AutomationCatalogFixture, unittest.TestCase):
    def test_dependency_gate_then_priority_and_active_plan_preemption(self):
        connection = self._connection()
        self._insert_plan(connection, "G-001", "MERGED")
        self._insert_plan(connection, "G-002", "READY", priority="low")
        self._insert_plan(
            connection,
            "G-003",
            "READY",
            priority="critical",
            depends_on=("G-004",),
        )
        self._insert_plan(connection, "G-004", "READY", priority="high")
        self._insert_plan(connection, "G-005", "READY", priority="high")

        decision = loop_manager.catalog_schedule_decision(connection, "repo")
        self.assertIsNone(decision.active)
        self.assertTrue(decision.has_ready)
        self.assertEqual(decision.runnable_ready["plan_id"], "G-004")
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' WHERE repo = 'repo' AND plan_id = 'G-004'"
        )
        connection.commit()
        self.assertEqual(loop_manager.select_ready_execplan(connection, "repo")["plan_id"], "G-003")

        connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' WHERE repo = 'repo' AND plan_id = 'G-005'"
        )
        connection.commit()
        decision = loop_manager.catalog_schedule_decision(connection, "repo")
        self.assertEqual(decision.active["plan_id"], "G-005")
        self.assertTrue(decision.has_ready)
        self.assertIsNone(decision.runnable_ready)

    @dummy_worker_sessions
    def test_dependency_blocked_ready_is_not_an_empty_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked_parent = root / "blocked"
            runnable_parent = root / "runnable"
            blocked_parent.mkdir()
            runnable_parent.mkdir()
            blocked_repo = make_remote_repository(str(blocked_parent))
            runnable_repo = make_remote_repository(str(runnable_parent))
            (blocked_repo / "docs/plans").mkdir()
            (runnable_repo / "docs/plans").mkdir()

            historical = write_execplan(
                blocked_repo, "G-009", "Unverified Historical"
            )

            blocked_plan = write_execplan(
                blocked_repo,
                "G-010",
                "Dependency Blocked",
                depends_on=("G-009",),
                priority="critical",
            )
            runnable_plan = write_execplan(
                runnable_repo,
                "G-001",
                "Runnable",
                priority="high",
            )
            commit_and_push(blocked_repo, "Add dependency-blocked plan")
            commit_and_push(runnable_repo, "Add runnable plan")
            blocked_plan_sha = git_command(blocked_repo, "rev-parse", "origin/main")
            runnable_plan_sha = git_command(runnable_repo, "rev-parse", "origin/main")
            preserved_fingerprint = loop_manager.planning_input_fingerprint(
                blocked_repo, "main"
            )
            preserved_specification = (
                loop_manager.specification_input_fingerprint(
                    blocked_repo, "main"
                )
            )

            (blocked_repo / "docs/PRODUCT.md").write_text(
                "# Changed product input\n",
                encoding="utf-8",
            )
            commit_and_push(blocked_repo, "Change blocked project input")

            state_dir = root / "state"
            state_dir.mkdir()
            connection = loop_manager.connect_database(state_dir, create=True)
            connection.executemany(
                "INSERT INTO projects (repo, base_branch, "
                "specification_fingerprint, planning_fingerprint) "
                "VALUES (?, 'main', ?, ?)",
                (
                    (
                        str(blocked_repo),
                        preserved_specification,
                        preserved_fingerprint,
                    ),
                    (
                        str(runnable_repo),
                        loop_manager.specification_input_fingerprint(
                            runnable_repo, "main"
                        ),
                        loop_manager.planning_input_fingerprint(
                            runnable_repo, "main"
                        ),
                    ),
                ),
            )
            planning_input_state.record_general(
                connection, str(blocked_repo), blocked_plan_sha,
                loop_manager.plan_declarations(blocked_repo, blocked_plan_sha),
            )
            planning_input_state.record_general(
                connection, str(runnable_repo), runnable_plan_sha,
                loop_manager.plan_declarations(runnable_repo, runnable_plan_sha),
            )
            for repo, plan_id, plan_path, title, dependencies, priority, plan_sha, status in (
                (
                    blocked_repo,
                    "G-009",
                    historical,
                    "Unverified Historical",
                    (),
                    "normal",
                    blocked_plan_sha,
                    "PLAN_BLOCKED",
                ),
                (
                    blocked_repo,
                    "G-010",
                    blocked_plan,
                    "Dependency Blocked",
                    ("G-009",),
                    "critical",
                    blocked_plan_sha,
                    "READY",
                ),
                (
                    runnable_repo,
                    "G-001",
                    runnable_plan,
                    "Runnable",
                    (),
                    "high",
                    runnable_plan_sha,
                    "READY",
                ),
            ):
                connection.execute(
                    """
                    INSERT INTO exec_plans (
                        repo, plan_id, path, title, sources, depends_on, priority,
                        content_hash, status, plan_sha
                    ) VALUES (?, ?, ?, ?, '["docs/PRODUCT.md"]', ?, ?,
                              ?, ?, ?)
                    """,
                    (
                        str(repo),
                        plan_id,
                        plan_path.relative_to(repo).as_posix(),
                        title,
                        json.dumps(dependencies),
                        priority,
                        loop_manager.file_hash(plan_path),
                        status,
                        plan_sha,
                    ),
                )
            maintenance_repository = (
                loop_manager.maintenance_state.MaintenanceRepository(connection)
            )
            for current_repo in (blocked_repo, runnable_repo):
                current_sha = git_command(
                    current_repo, "rev-parse", "origin/main"
                )
                request_maintenance(connection, str(current_repo))
                cycle, _ = maintenance_repository.begin_assessment_if_due(
                    str(current_repo), 5, current_sha, self_repository=True,
                )
                maintenance_repository.advance_success_baseline(
                    str(current_repo),
                    expected_phase=cycle.phase,
                    expected_revision=cycle.revision,
                    remote_commit=current_sha,
                )
            connection.commit()
            config = loop_manager.ManagedConfig(
                path=root / "unused.ini",
                max_workers=2,
                telegram_token="",
                telegram_chat_id="",
            )
            roles: list[str] = []

            def agent(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                return loop_manager.CodexResult(
                    1, "stop runnable worker", "worker-session"
                )

            try:
                with mock.patch(
                    "loop_manager.validate_repository",
                    side_effect=lambda path, **_kwargs: Path(path).resolve(),
                ), mock.patch(
                    "loop_manager.run_codex",
                    side_effect=agent,
                ), legacy_planning_ports(), dummy_worker_context(
                    roles=("worker", "planner", "maintenance_planner")
                ):
                    outcomes = loop_manager.schedule_registered_projects(
                        connection,
                        state_dir,
                        config,
                    )
                blocked_project = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (str(blocked_repo),),
                ).fetchone()
                blocked_catalog = connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = ?",
                    (str(blocked_repo),),
                ).fetchall()
                runnable_status = connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(runnable_repo),),
                ).fetchone()["status"]
            finally:
                connection.close()

        results = {item.repo: item.result.kind for item in outcomes}
        self.assertEqual(
            results[str(blocked_repo)],
            loop_manager.ProjectRunKind.OPERATIONAL_ERROR,
        )
        self.assertEqual(
            results[str(runnable_repo)],
            loop_manager.ProjectRunKind.OPERATIONAL_ERROR,
        )
        self.assertCountEqual(
            roles, ["planner", "worker"],
            msg=[(item.repo, item.result.detail) for item in outcomes],
        )
        self.assertEqual(
            tuple(blocked_project), ("PLANNING", preserved_fingerprint)
        )
        self.assertEqual(
            [tuple(row) for row in blocked_catalog],
            [("G-009", "PLAN_BLOCKED"), ("G-010", "READY")],
        )
        self.assertEqual(runnable_status, "IMPLEMENTING")

    def test_merge_marks_only_current_plan_and_unlocks_next_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = self._connection()
            self._insert_plan(connection, "G-001", "PR_OPEN", priority="high")
            self._insert_plan(
                connection,
                "G-002",
                "READY",
                priority="critical",
                depends_on=("G-001",),
            )
            state_dir = Path(directory) / "state"
            _branch, missing_worktree = loop_manager.expected_execplan_workspace(
                state_dir, "repo", "G-001"
            )
            connection.execute(
                """
                UPDATE projects
                SET phase = 'PR_OPEN', branch = 'loop-manager/G-001',
                    worktree = ?, plan_path = 'docs/plans/[G-001] Plan G-001.md',
                    pr_url = 'https://pr/1'
                WHERE repo = 'repo'
                """,
                (str(missing_worktree),),
            )
            connection.commit()

            assessment = loop_manager.complete_merged_worktree(
                connection,
                Path(directory),
                "repo",
                state_dir,
                missing_worktree,
                "https://pr/1",
                None,
            )

            statuses = dict(
                connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = 'repo'"
                ).fetchall()
            )
            selected = loop_manager.select_ready_execplan(connection, "repo")

        self.assertEqual(assessment.status, "missing")
        self.assertEqual(statuses, {"G-001": "MERGED", "G-002": "READY"})
        self.assertEqual(selected["plan_id"], "G-002")

    def test_last_merge_preserves_successful_planning_baseline(self):
        for completion in ("projected", "catalog"):
            with self.subTest(completion=completion), tempfile.TemporaryDirectory() as directory:
                connection = self._connection()
                self.addCleanup(connection.close)
                self._insert_plan(connection, "G-001", "PR_OPEN")
                state_dir = Path(directory) / "state"
                branch, worktree = loop_manager.expected_execplan_workspace(
                    state_dir, "repo", "G-001"
                )
                plan_path = "docs/plans/[G-001] Plan G-001.md"
                connection.execute(
                    "UPDATE exec_plans SET branch = ?, worktree = ? "
                    "WHERE repo = 'repo' AND plan_id = 'G-001'",
                    (branch, str(worktree)),
                )
                connection.execute(
                    "UPDATE projects SET phase = 'PR_OPEN', branch = ?, "
                    "worktree = ?, plan_path = ?, processed_sha = 'baseline-sha', "
                    "specification_fingerprint = 'baseline-spec', "
                    "planning_fingerprint = 'baseline-full' WHERE repo = 'repo'",
                    (branch, str(worktree), plan_path),
                )
                connection.commit()

                if completion == "projected":
                    assessment = loop_manager.complete_merged_worktree(
                        connection, Path(directory), "repo", state_dir,
                        worktree, "https://pr/1", None,
                    )
                else:
                    plan = connection.execute(
                        "SELECT * FROM exec_plans WHERE repo = 'repo' "
                        "AND plan_id = 'G-001'"
                    ).fetchone()
                    assessment = loop_manager.complete_merged_execplan_worktree(
                        connection, Path(directory), "repo", state_dir,
                        plan, "https://pr/1", None,
                    )

                row = connection.execute(
                    "SELECT phase, processed_sha, specification_fingerprint, "
                    "planning_fingerprint FROM projects WHERE repo = 'repo'"
                ).fetchone()
                self.assertEqual(assessment.status, "missing")
                self.assertEqual(
                    tuple(row),
                    (None, "baseline-sha", "baseline-spec", "baseline-full"),
                )

    @dummy_worker_sessions
    def test_planner_error_with_ready_does_not_repeat_project_step(self):
        for phase, planning_error in (("PLANNING", None), (None, "planner error")):
            with self.subTest(phase=phase, planning_error=planning_error):
                connection = self._connection()
                self.addCleanup(connection.close)
                self._insert_plan(connection, "G-001", "READY")

                def failed_planner_step(*_args, **_kwargs):
                    connection.execute(
                        "UPDATE projects SET phase = ?, planning_error = ?, "
                        "last_error = 'planner stopped' WHERE repo = 'repo'",
                        (phase, planning_error),
                    )
                    connection.commit()
                    return 1

                with mock.patch(
                    "loop_manager._checkpoint_dispatch_ready", return_value=False
                ), mock.patch(
                    "loop_manager._run_project_step", side_effect=failed_planner_step
                ) as step:
                    result = loop_manager._run_project_result(
                        connection, Path("/unused"), "repo",
                        Path("/unused.ini"), "main", False,
                    )

                self.assertEqual(result.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
                step.assert_called_once()

    @dummy_worker_sessions
    def test_remote_input_change_after_planner_defers_published_ready_worker(self):
        for publishes_plan in (False, True):
            with self.subTest(publishes_plan=publishes_plan), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                (repo / "docs/plans").mkdir()
                write_execplan(repo, "G-001", "Queued", priority="high")
                commit_and_push(repo, "Add queued plan")
                external_repo = Path(directory) / "external"
                git_command(
                    repo,
                    "clone",
                    str(Path(directory) / "remote.git"),
                    str(external_repo),
                )
                git_command(external_repo, "config", "user.email", "user@example.com")
                git_command(external_repo, "config", "user.name", "External User")
                state_dir = Path(directory) / "state"
                connection = self._connection(str(repo))
                baseline_sha = git_command(repo, "rev-parse", "origin/main")
                self._insert_plan(
                    connection,
                    "G-001",
                    "MERGED",
                    priority="high",
                    repo=str(repo),
                    title="Queued",
                    plan_sha=baseline_sha,
                )
                started_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
                roles = []
                remote_advanced = False
                published_fingerprint = None

                def advance_remote():
                    nonlocal remote_advanced
                    if remote_advanced:
                        return
                    git_command(external_repo, "fetch", "origin")
                    git_command(external_repo, "merge", "--ff-only", "origin/main")
                    (external_repo / "src/app.py").write_text(
                        "VALUE = 2\n",
                        encoding="utf-8",
                    )
                    commit_and_push(external_repo, "Change input during planning")
                    remote_advanced = True

                def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
                    roles.append(role_name)
                    if role_name == "planner_check":
                        self.assertTrue(_kwargs["read_only"])
                        return loop_manager.CodexResult(
                            0,
                            "PLANNER_CHECK_RESULT: " + json.dumps({
                                "outcome": "no_change", "evidence": "source diff reviewed",
                                "reason": "queued plan remains valid", "plan_ids": [],
                            }),
                            "check-session",
                        )
                    if role_name == "planner":
                        if not publishes_plan and roles.count("planner") == 2:
                            self.assertEqual(_kwargs.get("session_id"), "planner-session")
                            self.assertEqual((worktree / "src/app.py").read_text(encoding="utf-8"),
                                             "VALUE = 2\n")
                        if publishes_plan and roles.count("planner") == 1:
                            write_execplan(
                                worktree,
                                "G-002",
                                "Deferred",
                                priority="low",
                            )
                        elif not publishes_plan:
                            advance_remote()
                        return loop_manager.CodexResult(0, "", "planner-session")
                    return loop_manager.CodexResult(1, "worker must not start", "worker-session")

                original_publish = loop_manager.publish_general_planning_batch

                def publish(*args, **kwargs):
                    nonlocal published_fingerprint
                    original_publish(*args, **kwargs)
                    if publishes_plan and not remote_advanced:
                        published_fingerprint = connection.execute(
                            "SELECT planning_fingerprint FROM projects WHERE repo = ?",
                            (str(repo),),
                        ).fetchone()["planning_fingerprint"]
                        git_command(repo, "merge", "--ff-only", "origin/main")
                        advance_remote()

                with mock.patch(
                    "loop_manager.validate_repository",
                    return_value=repo,
                ), mock.patch(
                    "loop_manager.remote_base",
                    return_value="main",
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree",
                ), mock.patch(
                    "loop_manager.run_codex",
                    side_effect=agent,
                ), mock.patch(
                    "loop_manager.publish_general_planning_batch",
                    side_effect=publish,
                ), dummy_worker_context(roles=("worker", "planner_check")):
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                    if not publishes_plan:
                        preserved = connection.execute(
                            "SELECT phase, worktree, planner_session_id, planning_result_completed "
                            "FROM projects WHERE repo = ?", (str(repo),),
                        ).fetchone()
                        self.assertEqual(result, 0)
                        self.assertEqual(roles, ["planner"])
                        self.assertEqual(preserved["phase"], "PLANNING")
                        self.assertEqual(preserved["planner_session_id"], "planner-session")
                        self.assertTrue(preserved["planning_result_completed"])
                        self.assertTrue(Path(preserved["worktree"]).is_dir())
                        result = loop_manager.run_project(
                            connection, state_dir, str(repo), Path("unused.ini"), "main", False,
                        )

                project = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                plans = connection.execute(
                    "SELECT plan_id, status FROM exec_plans WHERE repo = ? ORDER BY plan_id",
                    (str(repo),),
                ).fetchall()

                self.assertEqual(result, 1 if publishes_plan else 0)
                self.assertEqual(
                    roles,
                    ["planner", "planner_check", "worker"] if publishes_plan
                    else ["planner", "planner"],
                )
                self.assertEqual(
                    project["phase"], "WORKING" if publishes_plan else None
                )
                current_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
                if publishes_plan:
                    self.assertIsNotNone(published_fingerprint)
                    self.assertEqual(project["planning_fingerprint"], published_fingerprint)
                    self.assertNotEqual(project["planning_fingerprint"], current_fingerprint)
                else:
                    self.assertEqual(project["planning_fingerprint"], current_fingerprint)
                    self.assertNotEqual(project["planning_fingerprint"], started_fingerprint)
                self.assertEqual(
                    [tuple(plan) for plan in plans],
                    [("G-001", "MERGED")]
                    + ([('G-002', 'IMPLEMENTING')] if publishes_plan else []),
                )

    @dummy_worker_sessions
    def test_changed_input_plans_before_ready_and_unchanged_input_runs_worker(self):
        for changed in (True, False):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                repo = make_remote_repository(directory)
                (repo / "docs/plans").mkdir()
                plan = write_execplan(repo, "G-001", "Queued", priority="high")
                commit_and_push(repo, "Add queued plan")
                state_dir = Path(directory) / "state"
                connection = self._connection(str(repo))
                baseline_sha = git_command(repo, "rev-parse", "origin/main")
                self._insert_plan(
                    connection,
                    "G-001",
                    "READY",
                    priority="high",
                    repo=str(repo),
                    title="Queued",
                    plan_sha=baseline_sha,
                )
                fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
                specification = loop_manager.specification_input_fingerprint(
                    repo, "main"
                )
                if changed:
                    (repo / "src/app.py").write_text(
                        "VALUE = 2\n", encoding="utf-8"
                    )
                    commit_and_push(repo, "Change source before READY selection")
                connection.execute(
                    """
                    UPDATE projects
                    SET specification_fingerprint = ?, planning_fingerprint = ?,
                        processed_sha = ?
                    WHERE repo = ?
                    """,
                    (
                        specification,
                        fingerprint,
                        baseline_sha,
                        str(repo),
                    ),
                )
                planning_input_state.record_general(
                    connection, str(repo), baseline_sha,
                    loop_manager.plan_declarations(repo, baseline_sha),
                )
                connection.commit()
                roles = []

                def agent(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                    roles.append(role_name)
                    if role_name == "planner_check":
                        self.assertTrue(_kwargs["read_only"])
                        return loop_manager.CodexResult(
                            0,
                            "PLANNER_CHECK_RESULT: " + json.dumps({
                                "outcome": "no_change", "evidence": "source diff reviewed",
                                "reason": "queued plan remains valid", "plan_ids": [],
                            }),
                            "check-session",
                        )
                    if role_name == "planner":
                        return loop_manager.CodexResult(0, "", "planner-session")
                    return loop_manager.CodexResult(1, "stop", f"{role_name}-session")

                with mock.patch(
                    "loop_manager.validate_repository",
                    return_value=repo,
                ), mock.patch(
                    "loop_manager.remote_base",
                    return_value="main",
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree",
                ), mock.patch(
                    "loop_manager.run_codex",
                    side_effect=agent,
                ), dummy_worker_context(roles=("worker", "planner_check")):
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                self.assertEqual(result, 1)
                self.assertEqual(
                    roles, ["planner_check", "worker"] if changed else ["worker"]
                )

    @dummy_worker_sessions
    def test_planning_and_ready_selection_pin_fingerprint_to_resolved_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / "docs/plans").mkdir()
            write_execplan(repo, "G-001", "Queued", priority="high")
            commit_and_push(repo, "Add queued plan")
            selected_sha = git_command(repo, "rev-parse", "origin/main")
            selected_fingerprint = loop_manager._planning_input_fingerprint(
                repo,
                selected_sha,
            )

            state_dir = Path(directory) / "state"
            connection = self._connection(str(repo))
            self._insert_plan(
                connection,
                "G-001",
                "READY",
                priority="high",
                repo=str(repo),
                title="Queued",
                plan_sha=selected_sha,
            )
            connection.execute(
                """
                UPDATE projects
                SET planning_fingerprint = ?, processed_sha = ?
                WHERE repo = ?
                """,
                (selected_fingerprint, selected_sha, str(repo)),
            )
            planning_input_state.record_general(
                connection, str(repo), selected_sha,
                loop_manager.plan_declarations(repo, selected_sha),
            )
            connection.commit()

            (repo / "docs/PRODUCT.md").write_text(
                "# Product moved after SHA resolve\n",
                encoding="utf-8",
            )
            git_command(repo, "add", "docs/PRODUCT.md")
            git_command(repo, "commit", "-m", "Move planning input locally")
            moved_sha = git_command(repo, "rev-parse", "HEAD")

            real_git = loop_manager.git
            real_fingerprint = loop_manager._planning_input_fingerprint
            exact_reads = 0
            fingerprint_revisions = []

            def move_ref_after_resolve(path, *arguments, **kwargs):
                nonlocal exact_reads
                result = real_git(path, *arguments, **kwargs)
                if arguments in {
                    (
                        "rev-parse",
                        "refs/remotes/origin/main^{commit}",
                    ),
                    (
                        "rev-parse",
                        "--verify",
                        "refs/remotes/origin/main^{commit}",
                    ),
                }:
                    exact_reads += 1
                    if exact_reads == 1:
                        git_command(
                            repo,
                            "update-ref",
                            "refs/remotes/origin/main",
                            moved_sha,
                        )
                return result

            def record_fingerprint(path, revision):
                fingerprint_revisions.append(revision)
                return real_fingerprint(path, revision)

            roles = []

            def agent(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                roles.append(role_name)
                return loop_manager.CodexResult(1, "stop", "worker-session")

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.git", side_effect=move_ref_after_resolve
            ), mock.patch(
                "loop_manager._planning_input_fingerprint",
                side_effect=record_fingerprint,
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch("loop_manager.run_codex", side_effect=agent):
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT active_sha, worktree FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1)
            self.assertEqual(roles, ["worker"])
            self.assertGreaterEqual(exact_reads, 2)
            self.assertEqual(fingerprint_revisions, [selected_sha])
            self.assertEqual(project["active_sha"], selected_sha)
            self.assertEqual(
                git_command(Path(project["worktree"]), "rev-parse", "HEAD"),
                selected_sha,
            )
            self.assertEqual(
                git_command(repo, "rev-parse", "refs/remotes/origin/main"),
                selected_sha,
            )


class PreservedPlanningSchedulingTest(AutomationCatalogFixture, unittest.TestCase):
    def setUp(self):
        use_fixed_guidance(self)
        super().setUp()

    def test_failed_general_handoff_holds_targets_and_transitive_dependents(self):
        from loop_manager_internal.storage import runtime_state, planner_check_state
        for stage in ('handoff', 'superseded'):
            with self.subTest(stage=stage):
                connection = self._connection()
                self._insert_plan(connection, 'G-001', 'READY', priority='critical')
                self._insert_plan(connection, 'G-002', 'READY', depends_on=('G-001',))
                self._insert_plan(connection, 'G-003', 'READY', priority='low')
                self._insert_plan(connection, 'G-004', 'MERGED')
                self._insert_plan(connection, 'G-005', 'MERGED', depends_on=('G-004',))
                self._insert_plan(connection, 'G-006', 'READY', depends_on=('G-005',), priority='critical')
                planner_check_state.begin(
                    connection, 'repo', input_fingerprint='input',
                    specification_fingerprint='spec', target_fingerprint='targets',
                    base_sha='base', comparison_sha='old-base',
                )
                result = 'PLANNER_CHECK_RESULT: ' + json.dumps({
                    'outcome': 'needs_revision', 'evidence': 'changed API',
                    'reason': 'validation needs correction', 'plan_ids': ['G-001', 'G-004'],
                })
                planner_check_state.advance(connection, 'repo', 'pending', 'result', result=result)
                planner_check_state.advance(connection, 'repo', 'result', 'handoff')
                if stage == 'superseded':
                    planner_check_state.advance(connection, 'repo', 'handoff', 'superseded')
                runtime = runtime_state.RuntimeStateRepository(connection)
                runtime.update_project('repo', {
                    'phase': 'PLANNING', 'active_sha': 'base', 'worktree': '/planning',
                    'planning_workspace_owned': 1,
                    'planning_attempt_fingerprint': 'input', 'planning_error_fingerprint': 'input',
                    'planner_session_id': 'general-session', 'planning_error': 'correction failed',
                    'planning_result': 'unpublished correction',
                }, enqueue=False)
                runtime.suspend_planning(runtime.planning('repo'))
                preserved = runtime.planning('repo')
                check = dict(planner_check_state.get(connection, 'repo'))
                self.assertEqual(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready['plan_id'], 'G-003')
                connection.execute("UPDATE exec_plans SET status = 'PR_OPEN' WHERE plan_id = 'G-003'")
                for _ in range(2):
                    decision = loop_manager.catalog_schedule_decision(connection, 'repo')
                    self.assertTrue(decision.has_ready)
                    self.assertIsNone(decision.runnable_ready)
                    self.assertEqual(runtime.planning('repo'), preserved)
                    self.assertEqual(dict(planner_check_state.get(connection, 'repo')), check)
                # A changed input alone cannot certify the unresolved targets.
                runtime.resume_planning(preserved)
                runtime.update_project('repo', {'planning_attempt_fingerprint': 'changed-input'}, enqueue=False)
                runtime.suspend_planning(runtime.planning('repo'))
                self.assertIsNone(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready)
                # Only a successful full planning baseline resolves the handoff.
                runtime.resume_planning(runtime.planning('repo'))
                with connection:
                    runtime.clear('repo', planning_fingerprint='published-correction')
                    planning_input_state.record_general(connection, 'repo', 'corrected-commit', {})
                resolved = planner_check_state.get(connection, 'repo')
                self.assertEqual(resolved['stage'], 'accepted')
                self.assertEqual(resolved['base_sha'], 'corrected-commit')
                self.assertEqual(resolved['result'], check['result'])
                runtime.update_project('repo', {
                    'planning_attempt_fingerprint': 'later-input',
                    'planning_error': 'later failure', 'planning_error_fingerprint': 'later-input',
                }, enqueue=False)
                runtime.suspend_planning(runtime.planning('repo'))
                self.assertIsNone(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready)
                runtime.resume_planning(runtime.planning('repo'))
                runtime.suspend_planning(runtime.planning('repo'), unconfirmed_plan_ids=())
                self.assertEqual(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready['plan_id'], 'G-001')

    def test_uncertain_or_invalid_general_check_does_not_release_unknown_targets(self):
        from loop_manager_internal.storage import runtime_state, planner_check_state
        for result in ('invalid', 'PLANNER_CHECK_RESULT: ' + json.dumps({
                'outcome': 'uncertain', 'evidence': 'missing evidence',
                'reason': 'cannot establish validity', 'plan_ids': []})):
            with self.subTest(result=result):
                connection = self._connection()
                self._insert_plan(connection, 'G-001', 'READY')
                planner_check_state.begin(
                    connection, 'repo', input_fingerprint='input',
                    specification_fingerprint='spec', target_fingerprint='targets',
                    base_sha='base', comparison_sha='old-base',
                )
                planner_check_state.advance(connection, 'repo', 'pending', 'handoff', result=result)
                runtime = runtime_state.RuntimeStateRepository(connection)
                runtime.update_project('repo', {'planning_error': 'correction failed',
                                               'planning_attempt_fingerprint': 'input'}, enqueue=False)
                runtime.suspend_planning(runtime.planning('repo'))
                decision = loop_manager.catalog_schedule_decision(connection, 'repo')
                self.assertTrue(decision.has_ready)
                self.assertIsNone(decision.runnable_ready)

    def test_interrupted_general_acceptance_rolls_back_baseline_and_target_release(self):
        from loop_manager_internal.storage import runtime_state, planner_check_state
        connection = self._connection()
        self._insert_plan(connection, 'G-001', 'READY')
        planner_check_state.begin(
            connection, 'repo', input_fingerprint='input',
            specification_fingerprint='spec', target_fingerprint='targets',
            base_sha='base', comparison_sha='old-base',
        )
        planner_check_state.advance(connection, 'repo', 'pending', 'handoff',
                                    result='PLANNER_CHECK_RESULT: ' + json.dumps({
                                        'outcome': 'needs_revision', 'evidence': 'changed API',
                                        'reason': 'needs correction', 'plan_ids': ['G-001'],
                                    }), session_id='check-session')
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.update_project('repo', {'planning_error': 'failed correction',
                                       'planning_attempt_fingerprint': 'input'}, enqueue=False)
        runtime.suspend_planning(runtime.planning('repo'))
        connection.commit()
        check = dict(planner_check_state.get(connection, 'repo'))
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with connection:
                planning_input_state.record_general(connection, 'repo', 'corrected-commit', {})
                raise RuntimeError('interrupted')
        self.assertFalse(planning_input_state.has_baseline(connection, 'repo'))
        self.assertEqual(dict(planner_check_state.get(connection, 'repo')), check)
        self.assertIsNone(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready)

    def test_unvalidated_targets_and_dependents_wait_while_independent_ready_runs(self):
        from loop_manager_internal.storage import runtime_state, maintenance_state
        connection = self._connection()
        self._insert_plan(connection, 'G-001', 'READY', priority='critical')
        self._insert_plan(connection, 'G-002', 'READY', depends_on=('G-001',))
        self._insert_plan(connection, 'G-003', 'READY', title='Independent Refactoring', priority='low')
        self._insert_plan(connection, 'G-004', 'MERGED')
        self._insert_plan(connection, 'G-005', 'READY', depends_on=('G-004',), priority='critical')
        self._insert_plan(connection, 'G-006', 'MERGED', depends_on=('G-004',))
        self._insert_plan(connection, 'G-007', 'READY', depends_on=('G-006',), priority='critical')
        maintenance_state.ensure_schema(connection)
        connection.execute(
            'INSERT INTO maintenance_cycles (repo, phase, revalidation_plan_ids) VALUES (?, ?, ?)',
            ('repo', maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION.value,
             json.dumps(['G-001', 'G-004'])),
        )
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.update_project('repo', {'planning_error': 'target goals remain uncertain',
                                       'planning_error_origin': 'unsafe'}, enqueue=False)
        runtime.suspend_planning(runtime.planning('repo'))
        preserved = runtime.planning('repo')
        decision = loop_manager.catalog_schedule_decision(connection, 'repo')
        self.assertEqual(decision.runnable_ready['plan_id'], 'G-003')
        connection.execute("UPDATE exec_plans SET status = 'PR_OPEN' WHERE plan_id = 'G-003'")
        decision = loop_manager.catalog_schedule_decision(connection, 'repo')
        self.assertTrue(decision.has_ready)
        self.assertIsNone(decision.runnable_ready)
        self.assertEqual(runtime.planning('repo'), preserved)
        # The existing successful recovery boundary releases the target gate.
        runtime.resume_planning(preserved)
        runtime.clear('repo')
        connection.execute('UPDATE maintenance_cycles SET phase = ? WHERE repo = ?',
                           (maintenance_state.MaintenancePhase.IDLE.value, 'repo'))
        decision = loop_manager.catalog_schedule_decision(connection, 'repo')
        self.assertEqual(decision.runnable_ready['plan_id'], 'G-001')

    def test_failed_maintenance_allows_only_dependency_valid_independent_ready(self):
        from loop_manager_internal.storage import runtime_state, maintenance_state
        for phase in (maintenance_state.MaintenancePhase.IDLE,
                      maintenance_state.MaintenancePhase.ASSESSMENT,
                      maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION):
            with self.subTest(phase=phase):
                connection = self._connection()
                self._insert_plan(connection, 'G-001', 'PLAN_BLOCKED')
                self._insert_plan(connection, 'G-002', 'READY', depends_on=('G-001',), priority='critical')
                self._insert_plan(connection, 'G-003', 'READY', priority='normal')
                self._insert_plan(connection, 'G-004', 'READY', priority='high')
                maintenance_state.ensure_schema(connection)
                connection.execute('INSERT INTO maintenance_cycles (repo, phase) VALUES (?, ?)',
                                   ('repo', phase.value))
                self.assertEqual(maintenance_state.MaintenanceRepository(connection).get('repo').phase, phase)
                runtime = runtime_state.RuntimeStateRepository(connection)
                runtime.update_project('repo', {'planning_error': 'failed', 'planning_error_fingerprint': 'input'}, enqueue=False)
                runtime.suspend_planning(runtime.planning('repo'), unconfirmed_plan_ids=())
                decision = loop_manager.catalog_schedule_decision(connection, 'repo')
                self.assertEqual(decision.runnable_ready['plan_id'], 'G-004')
                runtime.activate('repo', 'G-004', activation_sha='base', branch='branch', worktree='/worker')
                decision = loop_manager.catalog_schedule_decision(connection, 'repo')
                self.assertEqual(decision.active['plan_id'], 'G-004')
                self.assertIsNone(decision.runnable_ready)


class PreservedPlanningProjectResultTest(AutomationCatalogFixture, unittest.TestCase):
    def test_stable_work_reports_preserved_planning_waiting_and_actual_worker_error(self):
        from loop_manager_internal.application import project_runner
        from loop_manager_internal.storage import runtime_state
        for step_result in (0, 1):
            with self.subTest(step_result=step_result):
                repo = str(Path('/registered-project').resolve())
                connection = self._connection(repo=repo)
                runtime = runtime_state.RuntimeStateRepository(connection)
                runtime.update_project(repo, {'planning_error': 'preserved failure'}, enqueue=False)
                runtime.suspend_planning(runtime.planning(repo))
                runtime.update_project(repo, {'last_error': 'worker CLI failed'}, enqueue=False)
                effects = project_runner.InternalRunProjectResultEffects(
                    _checkpoint_dispatch_ready=lambda *_: False,
                    _record_project_recovery=mock.Mock(),
                    _run_project_step=mock.Mock(return_value=step_result),
                    _stable_work_reason=mock.Mock(return_value=None),
                    catalog_schedule_decision=mock.Mock(),
                    dispatch_blocked_checkpoints=mock.Mock(),
                    is_loop_manager_repository=lambda _: False,
                    operation_log=mock.Mock(),
                )
                result = project_runner._run_project_result(
                    effects, connection, Path('/state'), repo, Path('/config'), 'main', False)
                self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING
                                 if step_result == 0 else loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
                self.assertEqual(result.exit_code, step_result)
                self.assertIn('preserved failure' if step_result == 0 else 'worker CLI failed', result.detail)
                if step_result == 0:
                    effects._stable_work_reason.assert_not_called()

    def test_independent_ready_steps_continue_sequentially_with_preserved_error(self):
        from loop_manager_internal.application import project_runner
        from loop_manager_internal.storage import runtime_state, blocked_checkpoint_state
        from loop_manager_internal.notifications import notifications
        repo = str(Path('/registered-project').resolve())
        connection = self._connection(repo=repo)
        notifications.ensure_schema(connection)
        blocked_checkpoint_state.ensure_schema(connection)
        self._insert_plan(connection, 'G-001', 'READY', repo=repo)
        self._insert_plan(connection, 'G-002', 'READY', repo=repo)
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.update_project(repo, {'planning_error': 'preserved failure'}, enqueue=False)
        # The runner consumes the planning boundary's verified independent scope.
        runtime.suspend_planning(runtime.planning(repo), unconfirmed_plan_ids=())
        saved = runtime.planning(repo).planning_preserved_state
        completed = []
        def step(*_args):
            selected = loop_manager.catalog_schedule_decision(connection, repo).runnable_ready
            self.assertIsNotNone(selected)
            runtime.activate(repo, selected['plan_id'], activation_sha='base', branch='branch', worktree='/worker')
            runtime.open_pull_request(repo, pr_url='https://example.test/pull/' + selected['plan_id'])
            completed.append(selected['plan_id'])
            return 0
        effects = project_runner.InternalRunProjectResultEffects(
            _checkpoint_dispatch_ready=lambda *_: True,
            _record_project_recovery=mock.Mock(), _run_project_step=step,
            _stable_work_reason=mock.Mock(), catalog_schedule_decision=loop_manager.catalog_schedule_decision,
            dispatch_blocked_checkpoints=mock.Mock(), is_loop_manager_repository=lambda _: False,
            operation_log=mock.Mock(),
        )
        result = project_runner._run_project_result(
            effects, connection, Path('/state'), repo, Path('/config'), 'main', False)
        self.assertEqual(completed, ['G-001', 'G-002'])
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(runtime.planning(repo).planning_preserved_state, saved)
        effects._stable_work_reason.assert_not_called()


class EffectivePrioritySchedulingTest(AutomationCatalogFixture, unittest.TestCase):
    def test_multilevel_merge_gate_and_selection_log_preserve_declarations(self):
        connection = self._connection()
        for plan_id, priority, deps in (
            ('G-177', 'critical', ('G-170', 'G-176')),
            ('G-170', 'high', ('G-169',)),
            ('G-169', 'low', ()), ('G-176', 'normal', ()),
            ('G-168', 'high', ()),
        ):
            self._insert_plan(connection, plan_id, 'READY', priority=priority, depends_on=deps)
        with mock.patch.object(loop_manager, 'operation_log') as log:
            self.assertEqual(loop_manager.select_ready_execplan(connection, 'repo')['plan_id'], 'G-169')
        self.assertIn('declared_priority=low', log.call_args.args[0])
        self.assertIn('effective_priority=critical', log.call_args.args[0])
        self.assertIn('G-177 -> G-170 -> G-169', log.call_args.args[0])
        connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-169'")
        self.assertEqual(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready['plan_id'], 'G-170')
        self.assertEqual(connection.execute("SELECT priority FROM exec_plans WHERE plan_id='G-170'").fetchone()[0], 'high')
        connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-170'")
        self.assertEqual(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready['plan_id'], 'G-176')
        connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-176'")
        self.assertEqual(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready['plan_id'], 'G-177')

    def test_shared_goals_recompute_after_priority_dependency_and_catalog_changes(self):
        from loop_manager_internal.catalog.effective_priority import effective_priorities
        connection = self._connection()
        self._insert_plan(connection, 'G-001', 'READY', priority='low')
        self._insert_plan(connection, 'G-002', 'READY', priority='high')
        self._insert_plan(connection, 'G-003', 'READY', priority='critical', depends_on=('G-001',))
        self._insert_plan(connection, 'G-004', 'READY', priority='high', depends_on=('G-001',))
        def values():
            return effective_priorities(connection.execute('SELECT * FROM exec_plans').fetchall())
        self.assertEqual(values()['G-001'].effective, 'critical')
        connection.execute("UPDATE exec_plans SET priority='normal' WHERE plan_id='G-003'")
        self.assertEqual(values()['G-001'].effective, 'high')
        self.assertEqual(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready['plan_id'], 'G-001')
        connection.execute("UPDATE exec_plans SET depends_on='[]' WHERE plan_id='G-004'")
        self.assertEqual(values()['G-001'].effective, 'normal')
        connection.execute("DELETE FROM exec_plans WHERE plan_id='G-003'")
        self.assertEqual(values()['G-001'].effective, 'low')
        self._insert_plan(connection, 'G-005', 'READY', priority='critical', depends_on=('G-001',))
        self.assertEqual(values()['G-001'].effective, 'critical')
        connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-005'")
        self.assertEqual(values()['G-001'].effective, 'low')
        self.assertEqual(values(), values())

    def test_invalid_and_held_relations_do_not_raise_independent_priority(self):
        connection = self._connection()
        self._insert_plan(connection, 'G-001', 'READY', priority='low')
        self._insert_plan(connection, 'G-002', 'READY', priority='high')
        self._insert_plan(connection, 'G-003', 'READY', priority='critical', depends_on=('G-001', 'G-099'))
        self._insert_plan(connection, 'G-004', 'READY', priority='critical', depends_on=('G-001', 'G-004'))
        self._insert_plan(connection, 'G-005', 'READY', priority='critical', depends_on=('G-001', 'G-006'))
        self._insert_plan(connection, 'G-006', 'READY', depends_on=('G-005',))
        self._insert_plan(connection, 'G-007', 'READY', priority='critical', depends_on=('G-001',))
        connection.execute("INSERT INTO execplan_catalog_holds VALUES ('repo', 'G-007', 'sha', '{}', NULL)")
        self.assertEqual(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready['plan_id'], 'G-002')

    def test_waiting_paths_do_not_prevent_independent_selection(self):
        for status in ('PLAN_BLOCKED', 'PR_OPEN'):
            with self.subTest(status=status):
                connection = self._connection()
                self._insert_plan(connection, 'G-001', status, priority='low')
                self._insert_plan(connection, 'G-002', 'READY', priority='critical', depends_on=('G-001',))
                self._insert_plan(connection, 'G-003', 'READY', priority='high')
                self.assertEqual(loop_manager.catalog_schedule_decision(connection, 'repo').runnable_ready['plan_id'], 'G-003')
                self.assertEqual(connection.execute("SELECT status FROM exec_plans WHERE plan_id='G-001'").fetchone()[0], status)

    def test_resumable_and_preserved_planning_use_inheritance_within_candidate_gate(self):
        from loop_manager_internal.storage import runtime_state, maintenance_state
        for preserved in (False, True):
            with self.subTest(preserved=preserved):
                connection = self._connection()
                self._insert_plan(connection, 'G-001', 'READY', priority='low')
                self._insert_plan(connection, 'G-002', 'READY', priority='high')
                self._insert_plan(connection, 'G-003', 'READY', priority='critical', depends_on=('G-001',))
                connection.execute("UPDATE exec_plans SET workspace_owned=1 WHERE plan_id IN ('G-001', 'G-002')")
                if preserved:
                    runtime = runtime_state.RuntimeStateRepository(connection)
                    runtime.update_project('repo', {'planning_error': 'failed'}, enqueue=False)
                    runtime.suspend_planning(runtime.planning('repo'), unconfirmed_plan_ids=())
                else:
                    maintenance_state.ensure_schema(connection)
                    connection.execute("INSERT INTO maintenance_cycles (repo, phase) VALUES ('repo', 'assessment')")
                decision = loop_manager.catalog_schedule_decision(connection, 'repo')
                self.assertEqual(decision.runnable_ready['plan_id'], 'G-001')

    def test_merged_boundary_default_priority_and_restart(self):
        import sqlite3
        from loop_manager_internal.catalog.effective_priority import effective_priorities
        rows = [
            {'plan_id': 'G-001', 'status': 'READY', 'depends_on': '[]'},
            {'plan_id': 'G-002', 'status': 'MERGED', 'priority': 'low', 'depends_on': '["G-001"]'},
            {'plan_id': 'G-003', 'status': 'READY', 'priority': 'critical', 'depends_on': '["G-002"]'},
            {'plan_id': 'G-004', 'status': 'READY', 'priority': 'low', 'depends_on': '["G-003"]'},
        ]
        priorities = effective_priorities(rows)
        self.assertEqual(priorities['G-001'].effective, 'normal')
        self.assertEqual(priorities['G-003'].effective, 'critical')
        self.assertEqual(priorities['G-003'].paths, ())
        connection = self._connection()
        self._insert_plan(connection, 'G-001', 'READY', priority='low')
        self._insert_plan(connection, 'G-002', 'READY', priority='critical', depends_on=('G-001',))
        connection.commit()
        restarted = sqlite3.connect(':memory:')
        self.addCleanup(restarted.close)
        restarted.row_factory = sqlite3.Row
        connection.backup(restarted)
        before = loop_manager.catalog_schedule_decision(connection, 'repo')
        after = loop_manager.catalog_schedule_decision(restarted, 'repo')
        self.assertEqual(before.runnable_ready['plan_id'], after.runnable_ready['plan_id'])
        self.assertEqual(before.selection_reason, after.selection_reason)
