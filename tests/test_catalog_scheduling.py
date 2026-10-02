from tests.support.database import AutomationCatalogFixture
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import dummy_worker_sessions


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
            archive = blocked_repo / "docs/plans/archive"
            archive.mkdir()
            historical.rename(archive / historical.name)

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
            for repo, plan_id, plan_path, title, dependencies, priority, plan_sha in (
                (
                    blocked_repo,
                    "G-010",
                    blocked_plan,
                    "Dependency Blocked",
                    ("G-009",),
                    "critical",
                    blocked_plan_sha,
                ),
                (
                    runnable_repo,
                    "G-001",
                    runnable_plan,
                    "Runnable",
                    (),
                    "high",
                    runnable_plan_sha,
                ),
            ):
                connection.execute(
                    """
                    INSERT INTO exec_plans (
                        repo, plan_id, path, title, sources, depends_on, priority,
                        content_hash, status, plan_sha
                    ) VALUES (?, ?, ?, ?, '["docs/PRODUCT.md"]', ?, ?,
                              ?, 'READY', ?)
                    """,
                    (
                        str(repo),
                        plan_id,
                        plan_path.relative_to(repo).as_posix(),
                        title,
                        json.dumps(dependencies),
                        priority,
                        loop_manager.file_hash(plan_path),
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
            [("G-010", "READY")],
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
                    if role_name == "planner":
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
                    original_publish(*args, **kwargs)
                    if publishes_plan and not remote_advanced:
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
                ):
                    result = loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
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
                    ["planner", "planner", "worker"] if publishes_plan
                    else ["planner", "planner"],
                )
                self.assertEqual(
                    project["phase"], "WORKING" if publishes_plan else None
                )
                self.assertEqual(
                    project["planning_fingerprint"],
                    loop_manager.planning_input_fingerprint(repo, "main"),
                )
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
                connection.commit()
                roles = []

                def agent(_worktree, _state, _config, role_name, _prompt, **_kwargs):
                    roles.append(role_name)
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
                ):
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
                    roles, ["planner", "worker"] if changed else ["worker"]
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
