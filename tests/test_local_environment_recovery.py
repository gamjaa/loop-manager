from contextlib import closing
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.application.contracts import ProjectRunKind, ProjectRunResult
from loop_manager_internal.application.commands import recover_local_environment
from loop_manager_internal.application.project_runner import (
    InternalRunProjectResultEffects,
    _run_project_result,
)
from loop_manager_internal.application.project_service import ProjectIdentity, ProjectService
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.runtime.project_registry import RegisteredProject, add_project
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import runtime_state
from tests.support.database import runtime_connection


class LocalEnvironmentRecoveryCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.repository = self.root / "registered"
        self.repository.mkdir()
        with closing(loop_manager.connect_database(self.state_dir, create=True)) as connection:
            add_project(connection, RegisteredProject(
                repo=str(self.repository.resolve()), base_branch="main", provider="codex",
                agents_hash=None, agents_approved=False,
            ))
        self.config_path = self.root / "config.ini"
        self.arguments = loop_manager.build_parser().parse_args([
            "recover", str(self.repository),
        ])

    def effects(self, run_project_result):
        return SimpleNamespace(
            connect_database=loop_manager.connect_database,
            load_managed_config=loop_manager.load_managed_config,
            operation_print=mock.Mock(),
            run_project_result=run_project_result,
        )

    def test_manual_resume_runs_in_normal_order_and_is_scoped_out_of_recover(self):
        for request_id in (None, "request"):
            with self.subTest(request_id=request_id):
                service = ProjectService(SimpleNamespace(), SimpleNamespace(
                    repo_arg="repo", recovery_request_id=request_id))
                calls = []
                methods = ("_prepare_repository", "_reconcile_catalog_and_cleanup",
                           "_synchronize_pull_requests", "_resume_manual_work", "_recheck_local_plan_blocks",
                           "_resume_existing_execution", "_run_coordinators")
                from contextlib import ExitStack
                with ExitStack() as stack:
                    for method in methods:
                        def step(name=method):
                            calls.append(name)
                            return 0 if name == "_run_coordinators" else None
                        stack.enter_context(mock.patch.object(service, method, side_effect=step))
                    self.assertEqual(service.run().code, 0)
                expected = [method for method in methods if method != "_recheck_local_plan_blocks"] if request_id is None else [
                    method for method in methods
                    if method not in {"_synchronize_pull_requests", "_resume_manual_work"}]
                self.assertEqual(calls, expected)

    def test_paused_zero_exit_is_not_recorded_as_resolved(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        for kind in (ProjectRunKind.TRANSIENT_PAUSE, ProjectRunKind.USAGE_LIMIT_PAUSE):
            with self.subTest(kind=kind):
                effects = self.effects(mock.Mock(return_value=ProjectRunResult(kind, 0, "paused")))
                self.assertEqual(recover_local_environment(
                    effects, self.arguments, self.state_dir, self.config_path), 1)
                with closing(loop_manager.connect_database(self.state_dir)) as connection:
                    row = connection.execute(
                        "SELECT result FROM local_environment_recovery_requests ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                self.assertEqual(row[0], "unresolved")

    def test_pending_recovery_record_prevents_success_without_a_block(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        from loop_manager_internal.recovery.recovery_runtime import RecoveryReason
        def run(connection, *_args, **_kwargs):
            from loop_manager_internal.storage import recovery_state
            repository = recovery_state.RecoveryRepository(connection)
            before = repository.external_run_snapshot(str(self.repository.resolve()))
            loop_manager._record_project_recovery(
                connection, str(self.repository.resolve()), RecoveryReason.GIT_LOCK, "Git lock")
            repository.observe_external_run(str(self.repository.resolve()), before)
            return ProjectRunResult(ProjectRunKind.COMPLETED, 0)
        self.assertEqual(recover_local_environment(
            self.effects(run), self.arguments, self.state_dir, self.config_path), 1)

    def test_recovery_run_reaches_timeout_recheck_without_pr_sync(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        identity = ProjectIdentity(
            connection=connection, state_dir=Path("/state"), repo_arg="repo",
            config_path=Path("/config.ini"), requested_base=None,
            allow_agents_mismatch=False, requested_provider="codex",
            registered_only=True, planner_mutex=None, refactoring_review_interval=20,
            verify_timeout_seconds=600, runtime_mutation=None,
            recovery_request_id="request", recovery_plan_id="G-047")
        effects = SimpleNamespace(resume_plan_blocks=mock.Mock())
        service = ProjectService(effects, identity)
        service.state.repo = "repo"
        service.state.repo_path = Path("/repo")
        service.state.base = "main"
        with mock.patch.object(service, "_prepare_repository", return_value=None), \
             mock.patch.object(service, "_reconcile_catalog_and_cleanup", return_value=None), \
             mock.patch.object(service, "_synchronize_pull_requests") as pr_sync, \
             mock.patch.object(service, "_resume_existing_execution", return_value=None), \
             mock.patch.object(service, "_run_coordinators", return_value=0):
            self.assertEqual(service.run().code, 0)
        pr_sync.assert_not_called()
        self.assertEqual(effects.resume_plan_blocks.call_args.kwargs,
                         {"plan_id": "G-047", "force_timeout_recheck": True, "recovery_request_id": "request"})

    def test_recovery_does_not_loop_for_independent_ready_work(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        effects = InternalRunProjectResultEffects(
            _checkpoint_dispatch_ready=mock.Mock(return_value=True),
            _record_project_recovery=mock.Mock(),
            _run_project_step=mock.Mock(side_effect=[0, AssertionError("repeated recovery")]),
            _stable_work_reason=mock.Mock(return_value=None),
            catalog_schedule_decision=mock.Mock(return_value=SimpleNamespace(
                active=None, runnable_ready=object(), project_blocked=False)),
            dispatch_blocked_checkpoints=mock.Mock(),
            is_loop_manager_repository=mock.Mock(return_value=False),
            operation_log=mock.Mock())
        with mock.patch(
            "loop_manager_internal.application.project_runner.blocked_checkpoint_state.BlockedCheckpointRepository"
        ) as checkpoints:
            checkpoints.return_value.pending.return_value = ()
            _run_project_result(effects, connection, Path("/state"), "repo",
                                Path("/config.ini"), None, False,
                                recovery_request_id="request")
        effects._run_project_step.assert_called_once()

    def test_project_recovery_command_rechecks_saved_planning_without_selecting_ready(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            connection.execute("UPDATE projects SET planning_error = 'saved authentication failure' WHERE repo = ?", (repo,))
        from loop_manager_internal.planning.planning_runtime import PlanningOutcome, PlanningOutcomeKind
        def run(connection, state_dir, repository, config_path, *_args, **kwargs):
            effects = SimpleNamespace(
                _recorded_base_fetch=mock.Mock(), _check_runtime_handoff=mock.Mock(),
                _PlanningLifecyclePorts=mock.Mock(),
                is_loop_manager_repository=mock.Mock(return_value=False),
                _planning_outcome_step_result=mock.Mock(return_value=None))
            identity = ProjectIdentity(
                connection=connection, state_dir=state_dir, repo_arg=repository,
                config_path=config_path, requested_base=None, allow_agents_mismatch=False,
                requested_provider="codex", registered_only=True, planner_mutex=None,
                refactoring_review_interval=20, verify_timeout_seconds=600,
                runtime_mutation=None, recovery_request_id=kwargs["recovery_request_id"])
            service = ProjectService(effects, identity)
            service.state.repo = repository
            service.state.repo_path = Path(repository)
            service.state.base = "main"
            service.state.row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
            def planning(context):
                self.assertTrue(context.local_environment_recheck)
                connection.execute("UPDATE projects SET planning_error = NULL WHERE repo = ?", (repo,))
                return PlanningOutcome(PlanningOutcomeKind.NO_CHANGE)
            with mock.patch.object(service, "_prepare_repository", return_value=None), \
                 mock.patch.object(service, "_reconcile_catalog_and_cleanup", return_value=None), \
                 mock.patch.object(service, "_recheck_local_plan_blocks", return_value=None), \
                 mock.patch.object(service, "_resume_existing_execution", return_value=None), \
                 mock.patch.object(service, "_refresh_catalog", return_value="base"), \
                 mock.patch.object(service, "_dispatch_execution", side_effect=AssertionError("new work")), \
                 mock.patch("loop_manager_internal.application.project_service.PlanningLifecycleCoordinator") as coordinator:
                coordinator.return_value.run.side_effect = planning
                result = service.run()
                coordinator.return_value.run.assert_called_once()
            return ProjectRunResult(ProjectRunKind.COMPLETED, result.code)
        self.assertEqual(recover_local_environment(self.effects(run), self.arguments,
                                                   self.state_dir, self.config_path), 0)

    def test_historical_recovery_does_not_prevent_successful_recheck(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        from loop_manager_internal.recovery.recovery_runtime import RecoveryReason
        from loop_manager_internal.storage import recovery_state
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            before = recovery_state.RecoveryRepository(connection).external_run_snapshot(repo)
            loop_manager._record_project_recovery(connection, repo, RecoveryReason.GIT_LOCK, "old lock")
            recovery_state.RecoveryRepository(connection).observe_external_run(repo, before)
        from loop_manager_internal.application.project_runner import RunProjectResultEffects, run_project_result
        runner_effects = RunProjectResultEffects(
            _blocked_recovery_subjects=mock.Mock(return_value=frozenset()),
            _run_project_result=mock.Mock(return_value=ProjectRunResult(ProjectRunKind.COMPLETED, 0)))
        def successful_recheck(*args, **kwargs):
            return run_project_result(runner_effects, *args, **kwargs)
        result = recover_local_environment(self.effects(successful_recheck),
                                           self.arguments, self.state_dir, self.config_path)
        self.assertEqual(result, 0)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertEqual(connection.execute("SELECT pending_action FROM recovery_records WHERE repo = ?", (repo,)).fetchone()[0], "retry")

    def test_plan_recovery_finalization_keeps_independent_project_failure(self):
        from loop_manager_internal.storage import recovery_state
        from loop_manager_internal.application.project_runner import RunProjectResultEffects, run_project_result
        from loop_manager_internal.recovery.recovery_runtime import RecoveryReason
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repo = str(Path("repo").resolve())
        connection.execute("UPDATE projects SET repo = ?, planning_error = 'saved planning error'", (repo,))
        snapshot = recovery_state.RecoveryRepository(connection).external_run_snapshot(repo)
        loop_manager._record_project_recovery(connection, repo, RecoveryReason.GIT_LOCK, "planning tool failure")
        recovery_state.RecoveryRepository(connection).observe_external_run(repo, snapshot)
        before = dict(connection.execute("SELECT * FROM persistent_execution_failures WHERE repo = ?", (repo,)).fetchone())
        effects = RunProjectResultEffects(
            _blocked_recovery_subjects=mock.Mock(return_value=frozenset()),
            _run_project_result=mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0)))
        run_project_result(effects, connection, Path("/state"), repo, Path("/config"), None, False,
                           recovery_request_id="request", recovery_plan_id="G-047")
        after = dict(connection.execute("SELECT * FROM persistent_execution_failures WHERE repo = ?", (repo,)).fetchone())
        self.assertEqual(after, before)

    def test_parser_accepts_plan_scoped_recovery(self):
        args = loop_manager.build_parser().parse_args([
            "recover", str(self.repository), "--plan", "G-169",
        ])
        self.assertEqual(args.command, "recover")
        self.assertEqual(args.repository, str(self.repository))
        self.assertEqual(args.plan, "G-169")

    def test_missing_config_keeps_one_pending_request_for_same_scope_resume(self):
        run_result = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.COMPLETED, 0))
        effects = self.effects(run_result)
        with self.assertRaisesRegex(
            loop_manager.ManagedConfigError, "같은 `recover` 명령을 다시 실행",
        ):
            recover_local_environment(effects, self.arguments, self.state_dir, self.config_path)

        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            pending = connection.execute(
                "SELECT request_id, status FROM local_environment_recovery_requests "
                "WHERE repo = ?", (str(self.repository.resolve()),),
            ).fetchone()
        self.assertEqual(pending["status"], "pending")
        self.config_path.write_text("[loop]\n", encoding="utf-8")

        self.assertEqual(
            recover_local_environment(effects, self.arguments, self.state_dir, self.config_path),
            0,
        )
        run_result.assert_called_once()
        self.assertEqual(run_result.call_args.kwargs["registered_only"], True)
        self.assertEqual(run_result.call_args.kwargs["recovery_request_id"], pending["request_id"])
        self.assertIsNone(run_result.call_args.kwargs["recovery_plan_id"])
        self.assertEqual(run_result.call_args.kwargs["external_run_id"], pending["request_id"])
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            rows = connection.execute(
                "SELECT request_id, status, result, before_state, after_state "
                "FROM local_environment_recovery_requests WHERE repo = ?",
                (str(self.repository.resolve()),),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], pending["request_id"])
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["result"], "resolved")
        self.assertTrue(rows[0]["before_state"])
        self.assertTrue(rows[0]["after_state"])

    def test_unregistered_repository_and_missing_plan_are_rejected_before_request(self):
        effects = self.effects(mock.Mock())
        missing = self.root / "missing"
        missing.mkdir()
        args = loop_manager.build_parser().parse_args(["recover", str(missing)])
        with self.assertRaisesRegex(ValueError, "등록된 프로젝트"):
            recover_local_environment(effects, args, self.state_dir, self.config_path)
        args = loop_manager.build_parser().parse_args([
            "recover", str(self.repository), "--plan", "G-999",
        ])
        with self.assertRaisesRegex(ValueError, "없는 계획"):
            recover_local_environment(effects, args, self.state_dir, self.config_path)
        effects.run_project_result.assert_not_called()
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='local_environment_recovery_requests'",
            ).fetchone()
        self.assertIsNone(exists)

    def test_missing_state_database_is_not_created(self):
        absent_state = self.root / "absent-state"
        args = loop_manager.build_parser().parse_args(["recover", str(self.repository)])
        with self.assertRaisesRegex(RuntimeError, "상태 DB가 없습니다"):
            recover_local_environment(
                self.effects(mock.Mock()), args, absent_state, self.config_path,
            )
        self.assertFalse((absent_state / "loop-manager.db").exists())

    def test_in_progress_request_keeps_identity_for_a_restart(self):
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
                "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(self.repository.resolve()), "G-047", "docs/plans/[G-047] Runtime.md",
                 "Runtime", '["docs/PRODUCT.md"]', "[]", "normal", "hash", "READY", "plan-sha"),
            )
            runtime_state.RuntimeStateRepository(connection).activate(
                str(self.repository.resolve()), "G-047", activation_sha="base",
                branch="loop-manager/G-047", worktree=str(self.state_dir / "G-047"),
            )
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        args = loop_manager.build_parser().parse_args([
            "recover", str(self.repository), "--plan", "G-047",
        ])
        run = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.COMPLETED, 0))
        effects = self.effects(run)

        self.assertEqual(
            recover_local_environment(effects, args, self.state_dir, self.config_path), 1,
        )
        first_request = run.call_args.kwargs["recovery_request_id"]
        self.assertEqual(
            recover_local_environment(effects, args, self.state_dir, self.config_path), 1,
        )
        self.assertEqual(run.call_args.kwargs["recovery_request_id"], first_request)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            rows = connection.execute(
                "SELECT request_id, status, result FROM local_environment_recovery_requests "
                "WHERE repo = ?", (str(self.repository.resolve()),),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], first_request)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["result"], "in_progress")

    def test_plan_recovery_dispatches_existing_execution_without_planning(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime_state.RuntimeStateRepository(connection).activate(
            "repo", "G-047", activation_sha="base",
            branch="loop-manager/G-047", worktree="/state/G-047",
        )

        class CatalogRefresh:
            def refresh(self, _revision=None):
                return "base"

        class ExecutionCoordinator:
            def __init__(self, _ports):
                pass

            def prepare(self):
                return ExecutionOutcome(ExecutionOutcomeKind.STABLE_WAIT)

        effects = SimpleNamespace(
            git=mock.Mock(),
            _recorded_base_fetch=mock.Mock(),
            _check_runtime_handoff=mock.Mock(),
            _ExecutionLifecyclePorts=mock.Mock(return_value=object()),
            ExecutionPhaseCoordinator=ExecutionCoordinator,
            _execution_outcome_step_result=mock.Mock(return_value=0),
        )
        identity = ProjectIdentity(
            connection=connection, state_dir=Path("/state"), repo_arg="repo",
            config_path=Path("/config.ini"), requested_base=None,
            allow_agents_mismatch=False, requested_provider="codex",
            registered_only=True, planner_mutex=None, refactoring_review_interval=20,
            verify_timeout_seconds=600, runtime_mutation=None,
            recovery_request_id="request", recovery_plan_id="G-047",
        )
        service = ProjectService(effects, identity)
        service.state.repo = "repo"
        service.state.repo_path = Path("/repo")
        service.state.base = "main"
        service.state.base_sha = "base"
        service.state.history_revision = "base"
        service.state.catalog_refresh = CatalogRefresh()
        service.state.row = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'",
        ).fetchone()

        with mock.patch(
            "loop_manager_internal.application.project_service.PlanningLifecycleCoordinator",
            side_effect=AssertionError("recover must not enter general planning"),
        ):
            self.assertEqual(service._run_coordinators(), 0)
        effects._ExecutionLifecyclePorts.assert_called_once()
        effects._execution_outcome_step_result.assert_called_once()

    def test_project_runner_preserves_recovery_scope_when_invoking_project_service(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE projects SET phase = 'WORKING' WHERE repo = 'repo'",
        )
        step = mock.Mock(return_value=0)
        effects = InternalRunProjectResultEffects(
            _checkpoint_dispatch_ready=lambda *_args: False,
            _record_project_recovery=mock.Mock(),
            _run_project_step=step,
            _stable_work_reason=mock.Mock(return_value=None),
            catalog_schedule_decision=mock.Mock(),
            dispatch_blocked_checkpoints=mock.Mock(),
            is_loop_manager_repository=mock.Mock(return_value=False),
            operation_log=mock.Mock(),
        )
        _run_project_result(
            effects, connection, Path("/state"), "repo", Path("/config.ini"),
            None, False, registered_only=True, recovery_request_id="request",
            recovery_plan_id="G-047",
        )
        self.assertEqual(step.call_args.kwargs["recovery_request_id"], "request")
        self.assertEqual(step.call_args.kwargs["recovery_plan_id"], "G-047")

    def test_plan_recovery_scopes_checkpoint_dispatch_and_respects_other_active_plan(self):
        for active_plan, expected_dispatch in (("G-047", True), ("G-048", False)):
            with self.subTest(active_plan=active_plan):
                connection = runtime_connection()
                self.addCleanup(connection.close)
                repo = str(Path("repo").resolve())
                connection.execute("UPDATE projects SET repo = ? WHERE repo = 'repo'", (repo,))
                connection.execute("UPDATE exec_plans SET repo = ? WHERE repo = 'repo'", (repo,))
                connection.execute(
                    "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
                    "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo, "G-048", "docs/plans/[G-048] Other.md", "Other",
                     '["docs/PRODUCT.md"]', "[]", "normal", "other-hash", "READY", "other-plan-sha"),
                )
                runtime_state.RuntimeStateRepository(connection).activate(
                    repo, active_plan, activation_sha="base",
                    branch=f"loop-manager/{active_plan}", worktree=f"/state/{active_plan}",
                )
                effects = InternalRunProjectResultEffects(
                    _checkpoint_dispatch_ready=mock.Mock(return_value=True),
                    _record_project_recovery=mock.Mock(),
                    _run_project_step=mock.Mock(return_value=0),
                    _stable_work_reason=mock.Mock(return_value=None),
                    catalog_schedule_decision=mock.Mock(
                        return_value=SimpleNamespace(active=None, runnable_ready=None),
                    ),
                    dispatch_blocked_checkpoints=mock.Mock(),
                    is_loop_manager_repository=mock.Mock(return_value=False),
                    operation_log=mock.Mock(),
                )
                request = SimpleNamespace(request_identity="checkpoint-request")
                with mock.patch(
                    "loop_manager_internal.application.project_runner.blocked_checkpoint_state."
                    "BlockedCheckpointRepository",
                ) as repository_type, mock.patch(
                    "loop_manager_internal.application.project_runner.notifications.mark_work_available",
                ):
                    repository_type.return_value.pending.return_value = (request,)
                    _run_project_result(
                        effects, connection, Path("/state"), repo, Path("/config.ini"),
                        None, False, registered_only=True,
                        recovery_request_id="request", recovery_plan_id="G-047",
                    )

                self.assertEqual(effects.dispatch_blocked_checkpoints.called, expected_dispatch)
                if expected_dispatch:
                    self.assertEqual(
                        effects.dispatch_blocked_checkpoints.call_args.kwargs["plan_id"],
                        "G-047",
                    )
                    self.assertEqual(
                        repository_type.return_value.pending.call_args.kwargs["plan_id"],
                        "G-047",
                    )

    def test_pending_checkpoints_can_be_filtered_to_one_plan(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("repo", "G-048", "docs/plans/[G-048] Other.md", "Other",
             '["docs/PRODUCT.md"]', "[]", "normal", "other-hash", "READY", "other-plan-sha"),
        )
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        for plan_id in ("G-047", "G-048"):
            repository.request(
                repo="repo", plan_id=plan_id, reason_code="test", evidence="evidence",
                branch=f"loop-manager/{plan_id}", worktree=f"/state/{plan_id}",
                expected_remote_oid="base", rebase_recovery=False,
            )

        pending = repository.pending("repo", plan_id="G-047")

        self.assertEqual([request.plan_id for request in pending], ["G-047"])

    def test_plan_recovery_does_not_touch_another_active_plan(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("repo", "G-048", "docs/plans/[G-048] Other.md", "Other",
             '["docs/PRODUCT.md"]', "[]", "normal", "other-hash", "READY", "other-plan-sha"),
        )
        runtime_state.RuntimeStateRepository(connection).activate(
            "repo", "G-048", activation_sha="base",
            branch="loop-manager/G-048", worktree="/state/G-048",
        )

        class CatalogRefresh:
            def refresh(self, _revision=None):
                return "base"

        effects = SimpleNamespace(
            git=mock.Mock(),
            _recorded_base_fetch=mock.Mock(),
            _check_runtime_handoff=mock.Mock(),
            _ExecutionLifecyclePorts=mock.Mock(),
            ExecutionPhaseCoordinator=mock.Mock(),
            _execution_outcome_step_result=mock.Mock(),
            operation_print=mock.Mock(),
        )
        identity = ProjectIdentity(
            connection=connection, state_dir=Path("/state"), repo_arg="repo",
            config_path=Path("/config.ini"), requested_base=None,
            allow_agents_mismatch=False, requested_provider="codex",
            registered_only=True, planner_mutex=None, refactoring_review_interval=20,
            verify_timeout_seconds=600, runtime_mutation=None,
            recovery_request_id="request", recovery_plan_id="G-047",
        )
        service = ProjectService(effects, identity)
        service.state.repo = "repo"
        service.state.repo_path = Path("/repo")
        service.state.base = "main"
        service.state.base_sha = "base"
        service.state.history_revision = "base"
        service.state.catalog_refresh = CatalogRefresh()
        service.state.row = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'",
        ).fetchone()

        self.assertEqual(service._run_coordinators(), 1)
        effects._ExecutionLifecyclePorts.assert_not_called()
        effects.ExecutionPhaseCoordinator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
