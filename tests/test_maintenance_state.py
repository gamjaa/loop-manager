import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
import maintenance_git
import maintenance_state


class MaintenanceStateRepositoryTest(unittest.TestCase):
    def test_self_full_assessment_is_once_and_survives_restart(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        self.assertEqual(repository.due("one", 5, self_repository=True).reason,
                         "one_time_full_assessment")
        cycle, created = repository.begin_assessment_if_due(
            "one", 5, "a" * 40, self_repository=True)
        self.assertTrue(created)
        saved = repository.record_full_assessment(
            "one", expected_revision=cycle.revision, source_sha="a" * 40,
            result='{"findings":[]}',
        )
        self.assertEqual(maintenance_state.MaintenanceRepository(connection).get("one").full_assessment_result,
                         saved.full_assessment_result)
        completed = repository.advance_success_baseline(
            "one", expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision, remote_commit="a" * 40,
        )
        self.assertTrue(completed.full_assessment_completed)
        self.assertFalse(repository.due("one", 5, self_repository=True).required)
        self.assertEqual(repository.due("two", 5).reason, "no_successful_assessment")
        for number in range(1, 6):
            self._insert_plan(connection, "one", f"G-{number:03d}", "MERGED")
        periodic, started = repository.begin_assessment_if_due(
            "one", 5, "b" * 40, self_repository=True)
        self.assertTrue(started)
        restarted = repository.restart_stale_planning_context(
            "one", expected=periodic, new_base_sha="c" * 40)
        self.assertEqual(restarted.full_assessment_source_sha, "a" * 40)
        self.assertEqual(restarted.full_assessment_result, saved.full_assessment_result)

    def test_full_assessment_source_drift_discards_unpublished_result(self):
        connection = self._connection()
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due(
            "one", 5, "a" * 40, self_repository=True)
        saved = repository.record_full_assessment(
            "one", expected_revision=cycle.revision, source_sha="a" * 40,
            result='{"findings":[]}',
        )
        restarted = repository.restart_stale_planning_context(
            "one", expected=saved, new_base_sha="b" * 40,
        )
        self.assertIsNone(restarted.full_assessment_result)
        self.assertEqual(restarted.full_assessment_source_sha, "b" * 40)
        self.assertFalse(restarted.full_assessment_completed)

    def test_refactoring_publication_marks_full_assessment_without_resetting_periodic_due(self):
        connection = self._connection()
        self._insert_plan(connection, "one", "G-100", "READY")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due(
            "one", 5, "a" * 40, self_repository=True)
        repository.record_full_assessment(
            "one", expected_revision=cycle.revision, source_sha="a" * 40,
            result='{"findings":[]}',
        )
        published = repository.transition(
            "one", expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-100",),
        )
        self.assertTrue(published.full_assessment_completed)
        self.assertEqual(repository.due("one", 5, self_repository=True).reason,
                         "active_cycle")

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.executemany(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (("one",), ("two",)),
        )
        connection.commit()
        return connection

    @staticmethod
    def _insert_plan(
        connection: sqlite3.Connection,
        repo: str,
        plan_id: str,
        status: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on,
                priority, content_hash, status, worker_session_id,
                verify_failure_history
            ) VALUES (?, ?, ?, ?, '[]', '[]', 'normal', ?, ?, ?, ?)
            """,
            (
                repo,
                plan_id,
                f"docs/plans/{plan_id}.md",
                plan_id,
                f"hash-{plan_id}",
                status,
                f"session-{plan_id}",
                '{"fingerprint":2}',
            ),
        )

    def test_due_is_project_scoped_and_success_baseline_advances_only_on_success(self):
        connection = self._connection()
        self._insert_plan(connection, "one", "G-001", "MERGED")
        self._insert_plan(connection, "two", "G-001", "READY")
        repository = maintenance_state.MaintenanceRepository(connection)

        self.assertEqual(repository.due("one", 5).reason, "no_successful_assessment")
        self.assertEqual(repository.due("two", 5).reason, "no_successful_assessment")
        cycle, created = repository.begin_assessment_if_due("one", 5, "a" * 40)
        self.assertTrue(created)
        self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.ASSESSMENT)
        self.assertEqual(cycle.cycle_start_merged_count, 1)

        same_cycle, created = repository.begin_assessment_if_due("one", 1, "b" * 40)
        self.assertFalse(created)
        self.assertEqual(same_cycle, cycle)
        self._insert_plan(connection, "one", "G-002", "MERGED")
        self.assertEqual(repository.due("one", 1).reason, "active_cycle")

        completed = repository.advance_success_baseline(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            remote_commit="b" * 40,
        )
        self.assertEqual(completed.phase, maintenance_state.MaintenancePhase.IDLE)
        self.assertEqual(completed.last_success_merged_count, 2)
        self.assertFalse(repository.due("one", 2).required)
        self._insert_plan(connection, "one", "G-003", "MERGED")
        self.assertFalse(repository.due("one", 2).required)
        self._insert_plan(connection, "one", "G-004", "MERGED")
        self.assertTrue(repository.due("one", 2).required)
        self.assertEqual(repository.due("two", 2).reason, "no_successful_assessment")

    def test_plan_sets_round_trip_deterministically_and_reject_invalid_ids(self):
        connection = self._connection()
        for plan_id in ("G-010", "G-002", "G-003"):
            self._insert_plan(connection, "one", plan_id, "READY")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due("one", 5, "a" * 40)
        waiting = repository.transition(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
            prerequisite_plan_ids=("G-010", "G-002"),
            revalidation_plan_ids=("G-003",),
            publication_stage="remote_confirmed",
            publication_base_sha="a" * 40,
            publication_commit_sha="b" * 40,
            publication_remote_oid="b" * 40,
        )
        self.assertEqual(waiting.prerequisite_plan_ids, ("G-002", "G-010"))
        raw = connection.execute(
            "SELECT prerequisite_plan_ids FROM maintenance_cycles WHERE repo = 'one'"
        ).fetchone()[0]
        self.assertEqual(raw, '["G-002","G-010"]')
        self.assertEqual(waiting.publication_stage, "remote_confirmed")

        for invalid in (("G-002", "G-002"), ("G-999",), ("G-002", 3)):
            with self.subTest(invalid=invalid), self.assertRaises(
                maintenance_state.MaintenanceStateError
            ):
                repository.transition(
                    "one",
                    expected_phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                    expected_revision=waiting.revision,
                    next_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                    prerequisite_plan_ids=invalid,
                )
        self.assertEqual(repository.get("one"), waiting)
        connection.execute(
            "UPDATE maintenance_cycles SET refactoring_plan_ids = '[\"G-999\"]' "
            "WHERE repo = 'one'"
        )
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.get("one")

    def test_refactoring_baseline_waits_for_post_refactor_revalidation(self):
        connection = self._connection()
        self._insert_plan(connection, "one", "G-100", "READY")
        repository = maintenance_state.MaintenanceRepository(connection)
        cycle, _ = repository.begin_assessment_if_due("one", 5, "a" * 40)
        refactoring = repository.transition(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            expected_revision=cycle.revision,
            next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            refactoring_plan_ids=("G-100",),
        )
        with self.assertRaises(maintenance_state.MaintenanceStateError):
            repository.advance_success_baseline(
                "one",
                expected_phase=maintenance_state.MaintenancePhase.REFACTORING,
                expected_revision=refactoring.revision,
                remote_commit="b" * 40,
            )
        revalidation = repository.transition(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.REFACTORING,
            expected_revision=refactoring.revision,
            next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
        )
        completed = repository.advance_success_baseline(
            "one",
            expected_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
            expected_revision=revalidation.revision,
            remote_commit="c" * 40,
        )
        self.assertEqual(completed.last_success_commit, "c" * 40)

    def test_additive_schema_preserves_projects_execplans_sessions_and_failures(self):
        connection = self._connection()
        self._insert_plan(connection, "one", "G-001", "MERGED")
        before = tuple(
            connection.execute(
                """
                SELECT status, worker_session_id, verify_failure_history
                FROM exec_plans WHERE repo = 'one' AND plan_id = 'G-001'
                """
            ).fetchone()
        )
        maintenance_state.ensure_schema(connection)
        maintenance_state.ensure_schema(connection)
        after = tuple(
            connection.execute(
                """
                SELECT status, worker_session_id, verify_failure_history
                FROM exec_plans WHERE repo = 'one' AND plan_id = 'G-001'
                """
            ).fetchone()
        )
        self.assertEqual(after, before)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 2
        )

    def test_existing_database_connection_adds_maintenance_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.row_factory = sqlite3.Row
            database.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(database)
            database.execute(
                "INSERT INTO projects (repo, base_branch) VALUES ('one', 'main')"
            )
            self._insert_plan(database, "one", "G-001", "MERGED")
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as migrated:
                tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                plan = migrated.execute(
                    "SELECT status, worker_session_id FROM exec_plans "
                    "WHERE repo = 'one' AND plan_id = 'G-001'"
                ).fetchone()
            self.assertIn("maintenance_cycles", tables)
            self.assertEqual(tuple(plan), ("MERGED", "session-G-001"))


class BoundedGitChangeContextTest(unittest.TestCase):
    def _git(self, repo: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return completed.stdout.strip()

    def _commit(self, repo: Path, path: str, contents: str, message: str) -> str:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        self._git(repo, "add", path)
        self._git(repo, "commit", "-m", message)
        return self._git(repo, "rev-parse", "HEAD")

    def test_normal_empty_and_bounded_remote_ranges_do_not_change_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test")
            baseline = self._commit(repo, "z.txt", "z\n", "baseline")
            self._commit(repo, "b.txt", "b\n", "second")
            current = self._commit(repo, "a.txt", "a\n", "third")
            self._git(repo, "update-ref", "refs/remotes/origin/main", current)
            before_head = self._git(repo, "symbolic-ref", "HEAD")
            before_status = self._git(repo, "status", "--porcelain=v1")

            context = maintenance_git.bounded_remote_change_context(
                repo, "main", baseline, current
            )
            self.assertTrue(context.ok)
            self.assertEqual(
                tuple(commit.subject for commit in context.commits),
                ("second", "third"),
            )
            self.assertEqual(context.commits[0].paths, ("b.txt",))
            empty = maintenance_git.bounded_remote_change_context(
                repo, "main", current, current
            )
            self.assertEqual(empty.commits, ())

            with mock.patch.object(maintenance_git, "MAX_CHANGE_COMMITS", 1):
                bounded = maintenance_git.bounded_remote_change_context(
                    repo, "main", baseline, current
                )
            self.assertTrue(bounded.truncated)
            self.assertEqual(len(bounded.commits), 1)
            with mock.patch.object(
                maintenance_git, "MAX_CHANGE_CONTEXT_BYTES", 46
            ):
                byte_bounded = maintenance_git.bounded_remote_change_context(
                    repo, "main", baseline, current
                )
            self.assertTrue(byte_bounded.truncated)
            used = sum(
                len(commit.sha.encode("utf-8"))
                + len(commit.subject.encode("utf-8"))
                + sum(len(path.encode("utf-8")) for path in commit.paths)
                for commit in byte_bounded.commits
            )
            self.assertLessEqual(used, 46)
            self.assertEqual(self._git(repo, "symbolic-ref", "HEAD"), before_head)
            self.assertEqual(self._git(repo, "status", "--porcelain=v1"), before_status)

    def test_nonancestor_and_missing_baselines_return_diagnostic_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test")
            current = self._commit(repo, "tracked.txt", "value\n", "current")
            self._git(repo, "update-ref", "refs/remotes/origin/main", current)
            tree = self._git(repo, "write-tree")
            sibling = self._git(repo, "commit-tree", tree, "-m", "sibling")
            before = self._git(repo, "status", "--porcelain=v1")

            nonancestor = maintenance_git.bounded_remote_change_context(
                repo, "main", sibling, current
            )
            self.assertFalse(nonancestor.ok)
            self.assertIn("조상", nonancestor.error or "")
            missing = maintenance_git.bounded_remote_change_context(
                repo, "main", "f" * 40, current
            )
            self.assertFalse(missing.ok)
            self.assertIn("이력", missing.error or "")
            self.assertEqual(self._git(repo, "status", "--porcelain=v1"), before)

    def test_oversized_git_failure_is_not_misreported_as_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test")
            baseline = self._commit(repo, "one.txt", "one\n", "baseline")
            current = self._commit(repo, "two.txt", "two\n", "current")
            self._git(repo, "update-ref", "refs/remotes/origin/main", current)
            failed = subprocess.CompletedProcess(
                ["git"],
                1,
                stdout="x" * 100,
                stderr="fatal: simulated Git failure",
            )

            with mock.patch(
                "maintenance_git._bounded_git_output",
                return_value=(failed, True),
            ):
                context = maintenance_git.bounded_remote_change_context(
                    repo, "main", baseline, current
                )

            self.assertFalse(context.ok)
            self.assertFalse(context.truncated)
            self.assertIn("안전하게", context.error or "")

    def test_git_capture_bounds_actual_stdout_and_stderr_without_temp_spooling(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test")
            baseline = self._commit(repo, "tracked.txt", "value\n", "baseline")
            tree = self._git(repo, "write-tree")
            huge_subject = "s" * (maintenance_git.MAX_COMMIT_SUBJECT_BYTES * 8)
            created = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", tree, "-p", baseline],
                input=huge_subject,
                check=True,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            current = created.stdout.strip()
            self._git(repo, "update-ref", "refs/remotes/origin/main", current)

            context = maintenance_git.bounded_remote_change_context(
                repo, "main", baseline, current
            )
            self.assertTrue(context.ok)
            self.assertTrue(context.truncated)
            self.assertLessEqual(
                len(context.commits[0].subject.encode("utf-8")),
                maintenance_git.MAX_COMMIT_SUBJECT_BYTES,
            )

            invalid_object = "missing-" + (
                "x" * (maintenance_git.MAX_GIT_ERROR_BYTES * 2)
            )
            failed, output_overflow = maintenance_git._bounded_git_output(
                repo, 16, "show", invalid_object
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(output_overflow)
            self.assertLessEqual(
                len(failed.stdout.encode("utf-8")), 16
            )
            self.assertLessEqual(
                len(failed.stderr.encode("utf-8")),
                maintenance_git.MAX_GIT_ERROR_BYTES,
            )


if __name__ == "__main__":
    unittest.main()
