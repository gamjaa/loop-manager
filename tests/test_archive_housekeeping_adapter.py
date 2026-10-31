import os
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock
import loop_manager


class ExecPlanArchiveHousekeepingAdapterTest(unittest.TestCase):
    def test_entry_deferral_is_durable_and_logs_only_reason_changes(self):
        from types import SimpleNamespace
        from loop_manager_internal.application.project_service import ProjectService
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        service = ProjectService.__new__(ProjectService)
        service.identity = SimpleNamespace(connection=connection)
        service.state = SimpleNamespace(repo="/repo")
        service.effects = SimpleNamespace(operation_print=mock.Mock())
        service._archive_housekeeping_safe = mock.Mock(return_value=False)

        service._archive_housekeeping()
        service._archive_housekeeping()

        row = connection.execute("SELECT * FROM execplan_archive_observations").fetchone()
        self.assertEqual(row["category"], "execution_opportunity")
        self.assertEqual(row["reason"], "lifecycle_gate_closed")
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
        service._archive_housekeeping_safe = mock.Mock(return_value=False)
        with mock.patch.object(ExecPlanArchiveRepository, "observe", side_effect=sqlite3.OperationalError("database busy")):
            service._archive_housekeeping()
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
