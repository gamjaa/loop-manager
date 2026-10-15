import json
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import maintenance_state
from tests.support.git import commit_and_push, git_command, isolated_git_test
from tests.support.catalog import plan_text


from tests.support.maintenance_cycle_case import MaintenanceCycleCase

@isolated_git_test
class MaintenanceCycleTest(MaintenanceCycleCase):
    def test_prepared_batch_persists_new_plan_sets_for_restart_recovery(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = self._legacy_cycle(
            connection, "/one", "a" * 40, include_general_planning=True,
        )
        prepared = repository.record_publication_intent(
            "/one",
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            base_sha="a" * 40,
            commit_sha="b" * 40,
            refactoring_plan_ids=("G-100",),
            revalidation_plan_ids=("G-099", "G-101"),
        )
        self.assertEqual(prepared.publication_refactoring_plan_ids, ("G-100",))
        self.assertEqual(
            prepared.publication_revalidation_plan_ids,
            ("G-099", "G-101"),
        )
        self.assertTrue(prepared.include_general_planning)

        restarted = repository.restart_prepared_batch(
            "/one", expected=prepared, new_base_sha="c" * 40
        )
        self.assertIsNone(restarted.publication_stage)
        self.assertEqual(restarted.publication_refactoring_plan_ids, ())
        self.assertEqual(restarted.publication_revalidation_plan_ids, ())
        self.assertTrue(restarted.include_general_planning)


    def test_failed_maintenance_push_resumes_saved_result_without_replanning(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-001", "Ready Feature", "READY", (), "critical"),),
            )
            planner_calls = 0
            selected: list[str | None] = []

            def agent(
                worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                nonlocal planner_calls
                if role_name == "planner":
                    planner_calls += 1
                    self.assertEqual(planner_calls, 1)
                    self._write_refactoring_batch(worktree)
                    return loop_manager.CodexResult(
                        0,
                        'MAINTENANCE_RESULT: {"outcome":"refactoring",'
                        '"plan_ids":["G-002"]}',
                        "assessment-session",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

            real_git = loop_manager.git
            failed_pushes = 0

            def fail_first_maintenance_push(path, *arguments, **kwargs):
                nonlocal failed_pushes
                if arguments[:2] == ("push", "origin") and arguments[2:] == (
                    "HEAD:refs/heads/main",
                ):
                    failed_pushes += 1
                    return subprocess.CompletedProcess(
                        ["git"], 1, "injected maintenance push failure"
                    )
                return real_git(path, *arguments, **kwargs)

            try:
                with mock.patch(
                    "loop_manager.git", side_effect=fail_first_maintenance_push
                ):
                    first = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                prepared = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT phase, last_error, planning_result "
                    "FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                self.assertEqual(prepared.publication_stage, "prepared")
                self.assertEqual(project["phase"], "PLANNING")
                self.assertIn("push 실패", project["last_error"])
                self.assertIn("refactoring", project["planning_result"])

                second = self._run_registered(
                    connection, state_dir, repo_path, agent
                )
                recovered = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
            finally:
                connection.close()

        self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(second.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(failed_pushes, 1)
        self.assertEqual(planner_calls, 1)
        self.assertEqual(selected, ["G-002"])
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )


    def test_post_push_sqlite_failure_recovers_prepared_intent_without_repush(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-001", "Ready Feature", "READY", (), "critical"),),
            )
            planner_calls = 0
            selected: list[str | None] = []

            def agent(
                worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                nonlocal planner_calls
                if role_name == "planner":
                    planner_calls += 1
                    self.assertEqual(planner_calls, 1)
                    self._write_refactoring_batch(worktree)
                    return loop_manager.CodexResult(
                        0,
                        'MAINTENANCE_RESULT: {"outcome":"refactoring",'
                        '"plan_ids":["G-002"]}',
                        "assessment-session",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

            real_transition = maintenance_state.MaintenanceRepository.transition

            def fail_refactoring_transition(
                repository, repo_arg, *arguments, **kwargs
            ):
                if (
                    kwargs.get("next_phase")
                    is maintenance_state.MaintenancePhase.REFACTORING
                ):
                    raise maintenance_state.MaintenanceStateError(
                        "injected post-push SQLite failure"
                    )
                return real_transition(
                    repository, repo_arg, *arguments, **kwargs
                )

            try:
                with mock.patch.object(
                    maintenance_state.MaintenanceRepository,
                    "transition",
                    autospec=True,
                    side_effect=fail_refactoring_transition,
                ):
                    first = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                prepared = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT phase, planning_error FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                missing = connection.execute(
                    "SELECT 1 FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-002'",
                    (repo,),
                ).fetchone()
                self.assertEqual(prepared.publication_stage, "prepared")
                self.assertEqual(project["phase"], "PLANNING")
                self.assertIn("injected post-push", project["planning_error"])
                self.assertIsNone(missing)

                real_git = loop_manager.git
                recovery_pushes: list[tuple[str, ...]] = []

                def record_recovery_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] == "push":
                        recovery_pushes.append(arguments)
                    return real_git(path, *arguments, **kwargs)

                with mock.patch(
                    "loop_manager.git", side_effect=record_recovery_git
                ):
                    second = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                recovered = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT planning_error FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(second.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(recovery_pushes, [])
        self.assertEqual(planner_calls, 1)
        self.assertEqual(selected, ["G-002"])
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertIsNone(project["planning_error"])


    def test_published_combined_batch_recovers_before_changed_fingerprint_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path, state_dir, connection, repo = self._integration_project(
                directory,
                (("G-000", "Merged Foundation", "MERGED", (), "normal"),),
            )
            planner_calls = 0
            selected: list[str | None] = []

            def agent(
                worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                nonlocal planner_calls
                if role_name == "planner":
                    planner_calls += 1
                    if planner_calls == 2:
                        return loop_manager.CodexResult(0, '', 'general-session')
                    self.assertEqual(planner_calls, 1)
                    plans_dir = worktree / "docs/plans"
                    (plans_dir / "[G-001] Extract Boundary.md").write_text(
                        plan_text(
                            "G-001",
                            "Extract Boundary",
                            depends_on=("G-000",),
                        ),
                        encoding="utf-8", newline="\n",
                    )
                    (plans_dir / "[G-002] Pending Capability.md").write_text(
                        plan_text(
                            "G-002",
                            "Pending Capability",
                            depends_on=("G-001",),
                            purpose=(
                                "Deliver a pending product capability after the "
                                "structural boundary is available."
                            ),
                            acceptance_criteria=(
                                "Preserve the public API and expose the queued "
                                "product behavior."
                            ),
                            implementation=(
                                "Implement the product behavior on the extracted "
                                "module boundary."
                            ),
                        ),
                        encoding="utf-8", newline="\n",
                    )
                    return loop_manager.CodexResult(
                        0,
                        'MAINTENANCE_RESULT: {"outcome":"refactoring",'
                        '"plan_ids":["G-001"]}',
                        "combined-session",
                    )
                selected.append(self._active_plan_id(connection, repo))
                return loop_manager.CodexResult(1, "stop worker", "worker-session")

            real_transition = maintenance_state.MaintenanceRepository.transition

            def fail_refactoring_transition(
                repository, repo_arg, *arguments, **kwargs
            ):
                if (
                    kwargs.get("next_phase")
                    is maintenance_state.MaintenancePhase.REFACTORING
                ):
                    raise maintenance_state.MaintenanceStateError(
                        "injected combined SQLite interruption"
                    )
                return real_transition(
                    repository, repo_arg, *arguments, **kwargs
                )

            try:
                with mock.patch.object(
                    maintenance_state.MaintenanceRepository,
                    "transition",
                    autospec=True,
                    side_effect=fail_refactoring_transition,
                ), mock.patch(
                    "loop_manager_internal.planning.tiered_assessment.enabled",
                    return_value=False,
                ):
                    first = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                prepared = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT worktree, planning_attempt_fingerprint "
                    "FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                prepared_worktree = Path(project["worktree"])
                plan_commit = str(prepared.publication_commit_sha)
                self.assertTrue(prepared.include_general_planning)
                self.assertEqual(prepared.publication_stage, "prepared")
                self.assertTrue(prepared_worktree.is_dir())

                git_command(repo_path, "fetch", "origin")
                git_command(repo_path, "merge", "--ff-only", "origin/main")
                product = repo_path / "docs/PRODUCT.md"
                product.write_text(
                    product.read_text(encoding="utf-8")
                    + "\nNew product input after publication.\n",
                    encoding="utf-8",
                )
                commit_and_push(repo_path, "Advance combined planning input")
                latest = git_command(repo_path, "rev-parse", "origin/main")
                self.assertNotEqual(
                    loop_manager._planning_input_fingerprint(repo_path, plan_commit),
                    loop_manager._planning_input_fingerprint(repo_path, latest),
                )

                real_git = loop_manager.git
                recovery_pushes: list[tuple[str, ...]] = []

                def record_recovery_git(path, *arguments, **kwargs):
                    if arguments and arguments[0] == "push":
                        recovery_pushes.append(arguments)
                    return real_git(path, *arguments, **kwargs)

                with mock.patch(
                    "loop_manager.git", side_effect=record_recovery_git
                ), mock.patch(
                    'loop_manager.pr_for_branch', return_value=None
                ), mock.patch(
                    "loop_manager_internal.planning.tiered_assessment.enabled",
                    return_value=False,
                ):
                    second = self._run_registered(
                        connection, state_dir, repo_path, agent
                    )
                recovered = maintenance_state.MaintenanceRepository(connection).get(
                    repo
                )
                project = connection.execute(
                    "SELECT processed_sha, planning_fingerprint "
                    "FROM projects WHERE repo = ?",
                    (repo,),
                ).fetchone()
                general = connection.execute(
                    "SELECT depends_on FROM exec_plans "
                    "WHERE repo = ? AND plan_id = 'G-002'",
                    (repo,),
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual(first.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(second.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertEqual(planner_calls, 2)
        self.assertEqual(recovery_pushes, [])
        self.assertFalse(prepared_worktree.exists())
        self.assertEqual(selected, ["G-001"])
        self.assertEqual(
            recovered.phase, maintenance_state.MaintenancePhase.REFACTORING
        )
        self.assertEqual(recovered.refactoring_plan_ids, ("G-001",))
        self.assertEqual(recovered.revalidation_plan_ids, ("G-002",))
        self.assertEqual(project["processed_sha"], latest)
        self.assertIsNotNone(project["planning_fingerprint"])
        self.assertEqual(json.loads(general["depends_on"]), ["G-001"])
