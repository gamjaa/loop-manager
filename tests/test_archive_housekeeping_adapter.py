import os
import sqlite3
import threading
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from contextlib import ExitStack
from loop_manager_internal.catalog.archive_service import ArchiveExecutionResources
from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
from tests.support.database import runtime_connection


class ExecPlanArchiveHousekeepingAdapterTest(unittest.TestCase):
    def test_entry_deferral_is_durable_and_logs_only_reason_changes(self):
        from types import SimpleNamespace
        from loop_manager_internal.application.project_service import ProjectService
        from tests.support.database import runtime_connection
        connection = runtime_connection("/repo")
        self.addCleanup(connection.close)
        connection.execute("UPDATE projects SET base_branch='other'")
        connection.commit()
        service = ProjectService.__new__(ProjectService)
        service.identity = SimpleNamespace(connection=connection, state_dir=Path("/unused"))
        service.state = SimpleNamespace(repo="/repo", repo_path=Path("/repo"), base="main")
        service.effects = SimpleNamespace(operation_print=mock.Mock())

        service._archive_housekeeping()
        service._archive_housekeeping()

        row = connection.execute("SELECT * FROM execplan_archive_observations").fetchone()
        self.assertEqual(row["category"], "shared_resource")
        self.assertEqual(row["reason"], "base_owner_changed")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM execplan_archive_items").fetchone()[0], 0)
        service.effects.operation_print.assert_called_once()
        service._archive_deferred("shared_resource", "refs/heads/main", "writer_unknown", "confirm_writer_stopped")
        self.assertEqual(service.effects.operation_print.call_count, 2)

    def test_candidate_log_suppression_keeps_attempt_count_and_completion_transition(self):
        from loop_manager_internal.catalog import archive_service, execplan_archive
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        effects = archive_service.DispatchExecplanArchiveHousekeepingEffects(
            mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        pending = execplan_archive.ArchiveDispatchOutcome(
            execplan_archive.ArchiveDispatchKind.PENDING,
            pending=(("G-001", "source_hash_mismatch"),),
        )
        completed = execplan_archive.ArchiveDispatchOutcome(
            execplan_archive.ArchiveDispatchKind.CONVERGED, archived_plan_ids=("G-001",),
        )
        outcomes = (pending, pending, completed, pending)
        with mock.patch.object(execplan_archive.ExecPlanArchiveDispatcher, "dispatch", side_effect=outcomes):
            for _ in outcomes:
                archive_service.dispatch_execplan_archive_housekeeping(
                    effects, connection, Path("/repo"), Path("/state"), "/repo", "main")
        self.assertEqual(effects.operation_print.call_count, 3)
        row = connection.execute("SELECT * FROM execplan_archive_observations WHERE scope='G-001'").fetchone()
        self.assertEqual(row["attempts"], 4)
        self.assertEqual(row["category"], "candidate")
        self.assertEqual(row["reason"], "source_hash_mismatch")

    def test_deferral_storage_failure_keeps_archive_non_gating(self):
        from types import SimpleNamespace
        from loop_manager_internal.application.project_service import ProjectService
        from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
        service = ProjectService.__new__(ProjectService)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        service.identity = SimpleNamespace(connection=connection)
        service.state = SimpleNamespace(repo="/repo")
        service.effects = SimpleNamespace(operation_print=mock.Mock())
        with mock.patch.object(ExecPlanArchiveRepository, "observe", side_effect=sqlite3.OperationalError("database busy")):
            service._archive_deferred("shared_resource", "base", "busy", "release_writer")
        self.assertIn("기록 미확정", service.effects.operation_print.call_args.args[0])

    def test_archive_housekeeping_waits_for_preserved_planning_attempt(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, "
            "planning_attempt_fingerprint) VALUES ('/repo', 'main', 'attempt')"
        )
        self.assertFalse(loop_manager._execplan_archive_housekeeping_safe(
            connection, '/repo', None,
            maintenance_assessment_enabled=False,
            refactoring_review_interval=5,
        ))

    def test_archive_commit_ignores_ambient_identity_signing_and_hooks(self):
        workspace = Path("/archive-worktree")
        ambient = {
            "GIT_AUTHOR_NAME": "Ambient Author",
            "GIT_AUTHOR_EMAIL": "ambient-author@example.test",
            "GIT_COMMITTER_NAME": "Ambient Committer",
            "GIT_COMMITTER_EMAIL": "ambient-committer@example.test",
        }
        completed = subprocess.CompletedProcess(["git"], 0, "")

        with mock.patch.dict(os.environ, ambient), mock.patch(
            "loop_manager.command", return_value=completed
        ) as command_mock:
            loop_manager._ExecPlanArchiveGitPort(Path('/repo'), Path('/state'), 'repo')._commit_archive(
                workspace, "Archive merged ExecPlans: G-000"
            )

        arguments = command_mock.call_args.args[0]
        environment = command_mock.call_args.kwargs["env"]
        hooks_config = arguments[2]
        self.assertEqual(arguments[0:2], ["git", "-c"])
        self.assertTrue(hooks_config.startswith("core.hooksPath="))
        self.assertEqual(
            arguments[3:],
            [
                "-c",
                "commit.gpgSign=false",
                "-C",
                str(workspace),
                "commit",
                "-m",
                "Archive merged ExecPlans: G-000",
            ],
        )
        self.assertFalse(Path(hooks_config.partition("=")[2]).exists())
        self.assertFalse(command_mock.call_args.kwargs["check"])
        self.assertEqual(environment["GIT_AUTHOR_NAME"], "Loop Manager")
        self.assertEqual(
            environment["GIT_AUTHOR_EMAIL"], "loop-manager@localhost"
        )
        self.assertEqual(environment["GIT_COMMITTER_NAME"], "Loop Manager")
        self.assertEqual(
            environment["GIT_COMMITTER_EMAIL"], "loop-manager@localhost"
        )

    def test_archive_housekeeping_waits_for_file_due_or_active_legacy_cycle(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')"
        )
        connection.commit()

        self.assertTrue(
            loop_manager._execplan_archive_housekeeping_safe(
                connection,
                "/repo",
                None,
                maintenance_assessment_enabled=False,
                refactoring_review_interval=5,
            )
        )
        self.assertTrue(
            loop_manager._execplan_archive_housekeeping_safe(
                connection,
                "/repo",
                None,
                maintenance_assessment_enabled=True,
                refactoring_review_interval=5,
            )
        )
        repository = loop_manager.maintenance_state.MaintenanceRepository(
            connection
        )
        from tests.support.maintenance import request_maintenance
        request_maintenance(connection, "/repo")
        cycle, started = repository.begin_assessment_if_due(
            "/repo", 5, "a" * 40, self_repository=True,
        )

        self.assertTrue(started)
        self.assertEqual(
            cycle.phase,
            loop_manager.maintenance_state.MaintenancePhase.ASSESSMENT,
        )
        self.assertFalse(
            loop_manager._execplan_archive_housekeeping_safe(
                connection,
                "/repo",
                None,
                maintenance_assessment_enabled=True,
                refactoring_review_interval=5,
            )
        )
        idle = repository.advance_success_baseline(
            "/repo",
            expected_phase=cycle.phase,
            expected_revision=cycle.revision,
            remote_commit="b" * 40,
        )
        self.assertEqual(
            idle.phase,
            loop_manager.maintenance_state.MaintenancePhase.IDLE,
        )
        self.assertTrue(
            loop_manager._execplan_archive_housekeeping_safe(
                connection,
                "/repo",
                None,
                maintenance_assessment_enabled=True,
                refactoring_review_interval=5,
            )
        )
        connection.execute(
            "INSERT INTO execplan_catalog_revisions "
            "(repo, revision_sha, catalog_fingerprint) "
            "VALUES ('/repo', ?, 'catalog')",
            ("b" * 40,),
        )
        repository.initialize_file_assessment_start(
            "/repo", source_sha="b" * 40, expected_merged_count=0,
        )
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) VALUES "
            "('/repo', 'G-001', 'docs/plans/[G-001] Done.md', 'Done', "
            "'[]', '[]', 'normal', 'hash', 'MERGED')"
        )
        self.assertFalse(
            loop_manager._execplan_archive_housekeeping_safe(
                connection, "/repo", None,
                maintenance_assessment_enabled=True,
                refactoring_review_interval=1,
            )
        )
        connection.close()


    def test_archive_staging_does_not_add_the_deleted_source_pathspec(self):
        workspace = Path("/archive-worktree")
        move = loop_manager.execplan_archive.ArchiveMove(
            "G-000",
            "docs/plans/[G-000] Merged Foundation.md",
            "docs/plans/archive/[G-000] Merged Foundation.md",
            "content-hash",
        )

        with mock.patch("loop_manager.git") as git_mock:
            loop_manager._ExecPlanArchiveGitPort(Path('/repo'), Path('/state'), 'repo')._stage_archive_moves(
                workspace, (move,)
            )

        self.assertEqual(
            git_mock.call_args_list,
            [
                mock.call(
                    workspace,
                    "update-index",
                    "--force-remove",
                    "--",
                    move.source,
                ),
                mock.call(
                    workspace,
                    "add",
                    "--",
                    f":(literal){move.destination}",
                ),
            ],
        )

    def test_planning_archive_classifier_delegates_exact_requirement_move(self):
        source = b"docs/requirements/[R-001] Change.md"
        destination = b"docs/requirements/archive/[R-001] Change.md"
        value = (b"blob", b"object")
        before = {source: value, b"src/app.py": value}
        after = {destination: value, b"src/app.py": value}

        with mock.patch(
            "loop_manager._git_tree_entries", side_effect=(before, after)
        ):
            self.assertTrue(
                loop_manager._is_requirement_archive_only_change(
                    Path("/repo"), "old", "new"
                )
            )

        changed = dict(after)
        changed[b"src/app.py"] = (b"blob", b"changed")
        with mock.patch(
            "loop_manager._git_tree_entries", side_effect=(before, changed)
        ):
            self.assertFalse(
                loop_manager._is_requirement_archive_only_change(
                    Path("/repo"), "old", "new"
                )
            )

    def test_pull_request_reconciliation_runs_housekeeping_before_selection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('/repo', 'main')"
        )
        connection.commit()
        calls = []

        result = loop_manager.synchronize_waiting_pull_requests(
            connection,
            Path("/repo"),
            Path("/state"),
            "/repo",
            after_reconciliation=lambda: calls.append("housekeeping"),
        )

        self.assertEqual(calls, ["housekeeping"])
        self.assertIsNone(result.step_result)
        self.assertFalse(result.conflict_started)
        connection.close()


class ArchiveExecutionResourcesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = str(self.root / "checkout")
        self.connection = runtime_connection(self.repo)
        self.addCleanup(self.connection.close)
        self.defer = mock.Mock()

    def guard(self, leases):
        return ArchiveExecutionResources(
            self.connection, self.root, Path(self.repo), self.repo, "main", mock.Mock(), leases, self.defer,
        )

    def test_active_execution_planner_and_maintenance_are_not_archive_resources(self):
        repository = loop_manager.maintenance_state.MaintenanceRepository(self.connection)
        repository.get(self.repo)
        self.connection.execute("INSERT INTO maintenance_cycles(repo,phase) VALUES (?,'assessment')", (self.repo,))
        with ExitStack() as leases:
            guard = self.guard(leases)
            for phase, status in (("WORKING", "IMPLEMENTING"), ("VERIFYING", "VERIFYING"),
                                  ("REVIEWING", "REVIEWING"), ("PLANNING", "PLAN_BLOCKED")):
                with self.subTest(phase=phase):
                    self.connection.execute("UPDATE projects SET phase=?", (phase,))
                    self.connection.execute("UPDATE exec_plans SET status=?", (status,))
                    self.connection.commit()
                    self.assertTrue(guard.safe())
                    self.assertTrue(resource_owned(self.root, self.repo, "git:fetch", owner="execplan-archive"))
        self.defer.assert_not_called()
        self.assertFalse(resource_owned(self.root, self.repo, "git:fetch"))

    def test_base_lease_conflict_is_nonblocking_and_retries_after_release(self):
        from loop_manager_internal.application.task_scheduling import current_scheduler
        scheduler = mock.Mock()
        scheduler.draining_projects = set()
        scheduler.condition = threading.Condition()
        scheduler.git_waiters = {}
        token = current_scheduler.set(scheduler)
        try:
            with resource_locks(self.root, self.repo, ["git:refs/heads/main"], owner="other-publication"):
                with ExitStack() as leases:
                    self.assertFalse(self.guard(leases).safe())
                self.assertEqual(len(scheduler.git_waiters[self.repo]), 1)
            with ExitStack() as leases:
                self.assertTrue(self.guard(leases).safe())
        finally:
            current_scheduler.reset(token)
        self.assertIn("other-publication", self.defer.call_args.args[2])

    def test_prepared_base_publication_blocks_but_assessment_does_not(self):
        loop_manager.maintenance_state.MaintenanceRepository(self.connection).get(self.repo)
        self.connection.execute("INSERT INTO maintenance_cycles(repo,phase,publication_stage) VALUES (?,'assessment','prepared')", (self.repo,))
        self.connection.commit()
        with ExitStack() as leases:
            self.assertFalse(self.guard(leases).safe())
        self.assertEqual(self.defer.call_args.args[2], "base_publication_reserved:maintenance_cycles")
        self.connection.execute("UPDATE maintenance_cycles SET publication_stage=NULL")
        self.connection.commit()
        with ExitStack() as leases:
            self.assertTrue(self.guard(leases).safe())

    def test_planning_drain_defers_before_acquiring_archive_resources(self):
        from types import SimpleNamespace
        from loop_manager_internal.application.task_scheduling import current_scheduler
        token = current_scheduler.set(SimpleNamespace(draining_projects={self.repo}))
        try:
            with ExitStack() as leases:
                self.assertFalse(self.guard(leases).safe())
                self.assertFalse(resource_owned(self.root, self.repo, "git:fetch"))
        finally:
            current_scheduler.reset(token)
        self.assertEqual(self.defer.call_args.args[2], "planning_drain")

    def test_archive_fetch_reuses_only_its_own_open_lease(self):
        from loop_manager_internal.runtime.resource_lock import fetch_resource
        from loop_manager_internal.runtime.contracts import ProcessLockBusyError
        calls = []
        @fetch_resource
        def fetch(_effects, repo, state_dir):
            calls.append(repo)
            return "base"
        with resource_locks(self.root, self.repo, ["git:fetch"], owner="execplan-archive"):
            self.assertEqual(fetch(None, Path(self.repo), self.root), "base")
        with resource_locks(self.root, self.repo, ["git:fetch"], owner="another-owner"):
            with self.assertRaises(ProcessLockBusyError):
                fetch(None, Path(self.repo), self.root)
        self.assertEqual(calls, [Path(self.repo)])

    def test_target_writer_lease_is_candidate_specific_and_does_not_change_execution(self):
        from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
        self.connection.execute("UPDATE exec_plans SET status='MERGED',current_revision_sha='base'")
        self.connection.commit()
        item = ExecPlanArchiveRepository(self.connection).merged_candidates(self.repo)[0]
        before = tuple(self.connection.execute("SELECT * FROM exec_plans").fetchone())
        with resource_locks(self.root, self.repo, ["plan:G-047"], owner="manual-start"):
            with ExitStack() as leases:
                guard = self.guard(leases)
                self.assertTrue(guard.safe())
                self.assertEqual(guard.candidate_reason(item), "candidate_resource_writer_or_lease_busy")
        with ExitStack() as leases:
            guard = self.guard(leases)
            self.assertIsNone(guard.candidate_reason(item))
            self.assertTrue(guard.safe())
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM exec_plans").fetchone()), before)

    def test_target_unknown_writer_is_preserved_until_physical_nonlaunch_is_known(self):
        from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
        from loop_manager_internal.storage.agent_invocation_state import ensure_schema
        ensure_schema(self.connection)
        self.connection.execute("UPDATE exec_plans SET status='MERGED',current_revision_sha='base'")
        self.connection.execute("INSERT INTO agent_invocations "
            "(invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at) "
            "VALUES ('writer',?,'G-047','worker','codex','mock','low','start',1,'now')", (self.repo,))
        self.connection.commit()
        item = ExecPlanArchiveRepository(self.connection).merged_candidates(self.repo)[0]
        with ExitStack() as leases:
            guard = self.guard(leases)
            self.assertEqual(guard.candidate_reason(item), "candidate_resource_writer_or_lease_busy")
        self.assertEqual(self.connection.execute("SELECT execution_status FROM agent_invocations").fetchone()[0], "unknown")
        self.connection.execute("UPDATE agent_invocations SET execution_status='not_executed',finished_at='stopped'")
        self.connection.commit()
        with ExitStack() as leases:
            self.assertIsNone(self.guard(leases).candidate_reason(item))

    def test_parked_target_keeps_physical_reservation_while_other_merged_candidate_is_safe(self):
        import json
        import sys
        from loop_manager_internal.storage import management_admission, task_calls
        from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
        self.connection.execute("UPDATE exec_plans SET status='MERGED',current_revision_sha='base'")
        self.connection.execute("INSERT INTO exec_plans "
            "(repo,plan_id,path,title,sources,depends_on,priority,content_hash,current_revision_sha,status) "
            "VALUES (?,'G-048','docs/plans/[G-048] Other.md','Other','[]','[]','normal','other','base','MERGED')", (self.repo,))
        management_admission.request(self.connection, self.repo, 'G-047')
        self.connection.commit()
        owner = task_calls.reserve(self.connection, self.root, self.repo, 'G-047', 'verify', 'test')
        receipt = self.root / 'logs/task-calls' / (owner.token + '.json')
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({'token': owner.token, 'platform': sys.platform, 'state': 'running'}))
        self.connection.execute('UPDATE task_call_reservations SET receipt_path=?,uncertain=1 WHERE token=?',
                                (str(receipt), owner.token))
        self.connection.commit()
        before = tuple(self.connection.execute('SELECT * FROM task_call_reservations').fetchone())
        admission = tuple(self.connection.execute('SELECT * FROM task_management_admissions').fetchone())
        items = ExecPlanArchiveRepository(self.connection).merged_candidates(self.repo)
        with ExitStack() as leases:
            guard = self.guard(leases)
            self.assertTrue(guard.safe())
            self.assertEqual(guard.candidate_reason(items[0]), 'candidate_resource_writer_or_lease_busy')
            self.assertIsNone(guard.candidate_reason(items[1]))
        self.assertTrue(management_admission.parked(self.connection, self.repo, 'G-047'))
        self.assertEqual(task_calls.managed_count(self.connection, self.repo), 0)
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM task_call_reservations').fetchone()), before)
        task_calls.verify_stopped(receipt)
        with ExitStack() as leases:
            self.assertIsNone(self.guard(leases).candidate_reason(items[0]))
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM task_management_admissions').fetchone()), admission)

    def test_closed_and_deleted_tasks_are_not_archive_candidates_or_global_holds(self):
        from loop_manager_internal.storage import deleted_plan_state
        from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED',blocked_reason='pull_request_closed',pr_url='closed-pr'")
        self.connection.execute("INSERT INTO exec_plans "
            "(repo,plan_id,path,title,sources,depends_on,priority,content_hash,current_revision_sha,status) "
            "VALUES (?,'G-048','docs/plans/[G-048] Merged.md','Merged','[]','[]','normal','hash','base','MERGED')", (self.repo,))
        deleted_plan_state.ensure_schema(self.connection)
        artifact = self.root / 'logs' / 'deleted-owner.txt'
        artifact.parent.mkdir(parents=True)
        artifact.write_text('preserve cleanup evidence')
        self.connection.execute('INSERT INTO deleted_plan_ids VALUES (?,?)', (self.repo, 'G-049'))
        self.connection.execute('INSERT INTO deleted_plan_files VALUES (?,?,?)', (self.repo, 'G-049', str(artifact)))
        self.connection.commit()
        closed = tuple(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-047'").fetchone())
        candidates = ExecPlanArchiveRepository(self.connection).merged_candidates(self.repo)
        self.assertEqual([item.plan_id for item in candidates], ['G-048'])
        with ExitStack() as leases:
            guard = self.guard(leases)
            self.assertTrue(guard.safe())
            self.assertIsNone(guard.candidate_reason(candidates[0]))
        with resource_locks(self.root, self.repo, ['git:refs/heads/main'], owner='deleted-plan-publication'):
            with ExitStack() as leases:
                self.assertFalse(self.guard(leases).safe())
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-047'").fetchone()), closed)
        self.assertEqual(self.connection.execute('SELECT plan_id FROM deleted_plan_ids').fetchone()[0], 'G-049')
        self.assertEqual(self.connection.execute('SELECT path FROM deleted_plan_files').fetchone()[0], str(artifact))
        self.assertEqual(artifact.read_text(), 'preserve cleanup evidence')
