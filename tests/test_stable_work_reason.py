from tests.support.fixed_samples import use_fixed_guidance
import sqlite3
import json
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.storage import planning_input_state, planning_review_state
from tests.support.git import git_command
from tests.support.worker import dummy_worker_sessions


class StableWorkReasonBoundaryTest(unittest.TestCase):
    def setUp(self):
        use_fixed_guidance(self)
        super().setUp()

    def _connection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        loop_manager.notifications.ensure_schema(connection)
        loop_manager.notifications.configure_session(connection, enabled=True)
        repo = str(Path("/projects/alpha").resolve())
        connection.execute(
            "INSERT INTO projects (repo, base_branch, planning_fingerprint, "
            "specification_fingerprint) VALUES (?, 'main', 'cycle-one', 'spec-one')",
            (repo,),
        )
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) VALUES "
            "(?, 'G-018', 'docs/plans/[G-018] Work.md', 'Work', '[]', "
            "'[]', 'normal', 'hash', 'PR_OPEN')", (repo,),
        )
        loop_manager.maintenance_state.ensure_schema(connection)
        connection.execute(
            "INSERT INTO maintenance_cycles "
            "(repo, last_cycle_commit, last_cycle_merged_count, file_assessment_start_merged_count) "
            "VALUES (?, 'completed-cycle', 0, 0)", (repo,),
        )
        planning_input_state.record_general(connection, repo, "base-sha", {})
        connection.commit()
        return connection, repo

    def test_remote_requirement_and_planner_confirmation_gate_completion(self):
        connection, repo = self._connection()
        old_source = "docs/requirements/[R-031] Original.md"
        requirement_path = "docs/requirements/[R-031] Waiting.md"
        requirement = requirement_path.encode()
        connection.execute("UPDATE projects SET origin='org/repo' WHERE repo=?", (repo,))
        connection.execute(
            "UPDATE exec_plans SET sources = ? WHERE repo = ?",
            (json.dumps([old_source]), repo),
        )
        oid = "1" * 40

        def git_bytes(_repo, operation, *args):
            if operation == "ls-tree":
                return f"100644 blob {oid}\t{requirement_path}\0".encode()
            if operation == "cat-file":
                return b"# [R-031] Waiting\n"
            raise AssertionError((operation, args))

        with mock.patch("loop_manager.git") as git_command, mock.patch(
            "loop_manager._planning_input_fingerprint", return_value="cycle-one"
        ), mock.patch(
            "loop_manager._specification_input_fingerprint", return_value="spec-one"
        ), mock.patch(
            "loop_manager._git_tree_entries", return_value={requirement: (b"blob", b"oid")}
        ), mock.patch(
            "loop_manager.git_bytes", side_effect=git_bytes
        ):
            git_command.return_value.stdout = "base-sha\n"
            snapshot = loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            )
            self.assertIsNotNone(snapshot)
            self.assertIn("[R-031]", snapshot.describe())
            self.assertEqual(snapshot.plans[0]["url"],
                "https://github.com/org/repo/blob/base-sha/docs/plans/%5BG-018%5D%20Work.md")
            self.assertEqual(snapshot.requirements[0]["url"],
                "https://github.com/org/repo/blob/base-sha/docs/requirements/%5BR-031%5D%20Waiting.md")
            connection.execute(
                "UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,),
            )
            no_plan = loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            )
            self.assertFalse(no_plan.completed)
            connection.execute(
                "UPDATE projects SET planning_fingerprint = 'older' WHERE repo = ?", (repo,),
            )
            self.assertIsNone(loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            ))
        connection.close()

    def final_receipt(self, connection, repo, revision, input_fingerprint="cycle-one", specification_fingerprint="spec-one"):
        cycle = planning_review_state.work_cycle(connection, repo)
        planning_review_state.begin(connection, repo, purpose="final", revision=revision,
            input_fingerprint=input_fingerprint, specification_fingerprint=specification_fingerprint,
            work_cycle=cycle, comparison_revision=None)
        result = "FINAL_SPECIFICATION_REVIEW: " + json.dumps({
            "revision": revision, "input_fingerprint": input_fingerprint,
            "specification_fingerprint": specification_fingerprint, "work_cycle": cycle,
            "outcome": "fulfilled", "evidence": ["docs/PRODUCT.md", "src/app.py"],
            "reason": "Full specifications and implementation reviewed",
        })
        planning_review_state.validate(connection, repo, revision, result, "fulfilled")
        planning_review_state.confirm(connection, repo, revision, revision)

    def test_real_state_port_only_accepts_idle_unowned_completion_candidate(self):
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        from loop_manager_internal.storage import runtime_state
        from types import SimpleNamespace
        connection, repo = self._connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,))
        effects = SimpleNamespace(
            catalog_schedule_decision=loop_manager.catalog_schedule_decision,
            general_planning_publication=lambda *_args: None,
        )
        port = _PlanningStatePorts(effects, connection, runtime_state.RuntimeStateRepository(connection),
            loop_manager.maintenance_state.MaintenanceRepository(connection), Path('/unused'))
        self.assertTrue(port.final_review_candidate(repo, 5))
        self.assertEqual(json.loads(port.review_validation_context(repo))[0]['plan_id'], 'G-018')
        self.assertIsNone(json.loads(port.review_validation_context(repo))[0]['verify_result_sha256'])
        connection.execute("UPDATE exec_plans SET status = 'READY' WHERE repo = ?", (repo,))
        self.assertFalse(port.final_review_candidate(repo, 5))
        connection.execute("UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,))
        connection.execute("UPDATE projects SET planning_error = 'unresolved' WHERE repo = ?", (repo,))
        self.assertFalse(port.final_review_candidate(repo, 5))
        connection.execute("UPDATE projects SET planning_error = NULL WHERE repo = ?", (repo,))
        self.assertFalse(port.final_review_candidate(repo, 1))  # Maintenance is due.
        connection.execute("UPDATE maintenance_cycles SET phase = 'refactoring' WHERE repo = ?", (repo,))
        self.assertFalse(port.final_review_candidate(repo, 5))

    def test_final_receipt_is_required_and_bound_to_input_and_work_cycle(self):
        connection, repo = self._connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,))
        with (mock.patch("loop_manager.git") as command,
              mock.patch("loop_manager._planning_input_fingerprint", return_value="cycle-one"),
              mock.patch("loop_manager._specification_input_fingerprint", return_value="spec-one"),
              mock.patch("loop_manager.plan_declarations", return_value={}),
              mock.patch("loop_manager._git_tree_entries", return_value={})):
            command.return_value.stdout = "base-sha\n"
            check = lambda: loop_manager._stable_work_reason(connection, repo, refactoring_review_interval=5)
            self.assertIsNone(check())  # Ordinary planning no-change is insufficient.
            self.final_receipt(connection, repo, "base-sha")
            self.assertTrue(check().completed)
            for _ in range(2):
                self.assertTrue(check().completed)
            connection.execute("UPDATE planning_reviews SET result = NULL WHERE slot = 'final'")
            self.assertIsNone(check())
            self.final_receipt(connection, repo, "base-sha")
            connection.execute("UPDATE planning_reviews SET input_fingerprint = 'stale' WHERE slot = 'final'")
            self.assertIsNone(check())
            self.final_receipt(connection, repo, "base-sha")
            connection.execute("UPDATE exec_plans SET verify_token = 'different-candidate' WHERE repo = ?", (repo,))
            self.assertIsNone(check())
            self.final_receipt(connection, repo, "base-sha")
            connection.execute("UPDATE maintenance_cycles SET revision = revision + 1 WHERE repo = ?", (repo,))
            self.assertIsNone(check())

    def test_dual_archive_completion_requires_verified_plan_baseline(self):
        self._assert_archive_completion(include_requirement=True)

    def test_execplan_archive_completion_requires_verified_plan_baseline(self):
        self._assert_archive_completion(include_requirement=False)

    def _assert_archive_completion(self, *, include_requirement):
        connection, repo = self._connection()
        self.addCleanup(connection.close)
        plan = "docs/plans/[G-018] Work.md"
        previous = {
            plan.encode(): (b"blob", b"plan"),
        }
        current = {
            b"docs/plans/archive/[G-018] Work.md": (b"blob", b"plan"),
        }
        if include_requirement:
            previous[b"docs/requirements/R.md"] = (b"blob", b"requirement")
            current[b"docs/requirements/archive/R.md"] = (b"blob", b"requirement")
        connection.execute("UPDATE projects SET processed_sha = 'old-base' WHERE repo = ?", (repo,))
        connection.execute("UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,))
        planning_input_state.record_general(connection, repo, "old-base", {plan: ("blob", "plan")})
        self.final_receipt(connection, repo, "old-base")
        with mock.patch("loop_manager.git") as command, mock.patch(
            "loop_manager._planning_input_fingerprint", return_value="archived-full"
        ), mock.patch(
            "loop_manager._specification_input_fingerprint",
            return_value="archived-spec" if include_requirement else "spec-one",
        ), mock.patch(
            "loop_manager._nonplan_input_fingerprint", return_value="unchanged-code"
        ), mock.patch(
            "loop_manager._git_tree_entries",
            side_effect=lambda _repo, sha: previous if sha == "old-base" else current,
        ):
            command.return_value.stdout = "archived-base\n"
            self.assertIsNone(loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            ))
            planning_input_state.advance_verified_delta(
                connection, repo, "archived-base", {plan: (("blob", "plan"), None)},
            )
            for _ in range(2):
                result = loop_manager._stable_work_reason(
                    connection, repo, refactoring_review_interval=5,
                )
                self.assertIsNotNone(result)
                self.assertTrue(result.completed)
                self.assertEqual(result.cycle, "cycle-one")
            current[b"docs/plans/archive/[G-018] Work.md"] = (b"blob", b"edited")
            self.assertIsNone(loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            ))

    def test_legacy_processed_sha_cannot_confirm_unreviewed_plan(self):
        connection, repo = self._connection()
        connection.execute(
            "DELETE FROM planning_input_baselines WHERE repo = ?", (repo,),
        )
        connection.execute(
            "UPDATE projects SET processed_sha = 'base-sha', "
            "planning_fingerprint = 'legacy-v2' WHERE repo = ?", (repo,),
        )
        with mock.patch("loop_manager.git") as git_command, mock.patch(
            "loop_manager._planning_input_fingerprint", return_value="current-v3"
        ), mock.patch(
            "loop_manager._nonplan_input_fingerprint", return_value="legacy-v2"
        ), mock.patch(
            "loop_manager._specification_input_fingerprint", return_value="spec-one"
        ), mock.patch(
            "loop_manager.plan_declarations", return_value={}
        ), mock.patch(
            "loop_manager._git_tree_entries", return_value={}
        ):
            git_command.return_value.stdout = "base-sha\n"
            self.assertIsNone(loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=5,
            ))
        connection.close()

    def test_runnable_and_due_maintenance_do_not_emit_exhaustion(self):
        connection, repo = self._connection()
        connection.execute(
            "UPDATE exec_plans SET status = 'READY' WHERE repo = ?", (repo,),
        )
        self.assertIsNone(loop_manager._stable_work_reason(
            connection, repo, refactoring_review_interval=5,
        ))
        connection.execute(
            "UPDATE exec_plans SET status = 'PR_OPEN' WHERE repo = ?", (repo,),
        )
        connection.execute(
            "UPDATE maintenance_cycles SET last_success_merged_count = NULL "
            "WHERE repo = ?", (repo,),
        )
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
            "depends_on, priority, content_hash, status) VALUES "
            "(?, 'G-019', 'docs/plans/[G-019] Done.md', 'Done', "
            "'[]', '[]', 'normal', 'hash-19', 'MERGED')",
            (repo,),
        )
        with mock.patch("loop_manager.is_loop_manager_repository", return_value=False), \
                mock.patch("loop_manager.git") as git_command:
            self.assertIsNone(loop_manager._stable_work_reason(
                connection, repo, refactoring_review_interval=1,
            ))
            git_command.assert_not_called()
        connection.close()

    @dummy_worker_sessions
    def test_manual_run_with_due_maintenance_does_not_complete(self):
        connection, repo = self._connection()
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?", (repo,),
        )
        connection.execute(
            "UPDATE maintenance_cycles SET last_success_merged_count = NULL "
            "WHERE repo = ?", (repo,),
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=0), \
                mock.patch.object(loop_manager.notifications, "enqueue_completion_if_ready") as completed:
            result = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False, registered_only=False,
                refactoring_review_interval=1,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        completed.assert_not_called()
        connection.close()

    @dummy_worker_sessions
    def test_planner_no_change_with_active_requirement_waits_without_completion(self):
        connection, repo = self._connection()
        from loop_manager_internal.notifications import work_exhaustion
        snapshot = work_exhaustion.WorkReasonSnapshot(
            "cycle-one", (),
            ({"id": "[R-031]", "path": "docs/requirements/[R-031] Waiting.md",
              "reason": "Planner는 추가 계획이 없다고 확인했지만 활성 Requirement가 남아 있습니다"},),
            None, None,
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=0), \
                mock.patch("loop_manager._stable_work_reason", return_value=snapshot), \
                mock.patch.object(loop_manager.notifications, "enqueue_completion_if_ready") as completed:
            result = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertIn("[R-031]", result.detail)
        completed.assert_not_called()
        connection.close()

    @dummy_worker_sessions
    def test_stable_result_reports_reason_and_pause_skips_notification(self):
        connection, repo = self._connection()
        from loop_manager_internal.notifications import work_exhaustion
        snapshot = work_exhaustion.WorkReasonSnapshot(
            "cycle-one", ({"id": "G-018", "title": "Work", "status": "PR_OPEN",
                            "reason": "PR Merge 대기"},), (), None, None,
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=0), \
                mock.patch("loop_manager._stable_work_reason", return_value=snapshot):
            result = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertIn("PR Merge 대기", result.detail)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'work_waiting'"
        ).fetchone()[0], 0)
        episode = tuple(connection.execute(
            "SELECT signature, generation FROM work_reason_episodes WHERE repo=?", (repo,)
        ).fetchone())
        self.assertTrue(episode[0])
        self.assertEqual(episode[1], 1)
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=loop_manager.PROJECT_STEP_TRANSIENT_PAUSE), \
                mock.patch("loop_manager._stable_work_reason") as reason:
            paused = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(paused.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
        reason.assert_not_called()
        self.assertEqual(tuple(connection.execute(
            "SELECT signature, generation FROM work_reason_episodes WHERE repo=?", (repo,)
        ).fetchone()), episode)
        connection.close()

    @dummy_worker_sessions
    def test_suspended_validation_error_waits_only_for_unchanged_input_and_idle_targets(self):
        from loop_manager_internal.storage import runtime_state
        connection, repo = self._connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE projects SET phase='PLANNING', planning_error='Invalid result', "
            "planning_validation_error='Invalid result', planning_error_origin='validation', "
            "planning_error_fingerprint='cycle-one' WHERE repo=?", (repo,))
        state = runtime_state.RuntimeStateRepository(connection)
        state.suspend_planning(state.planning(repo))
        with (mock.patch('loop_manager._checkpoint_dispatch_ready', return_value=False),
              mock.patch('loop_manager._run_project_step', return_value=0),
              mock.patch('loop_manager.git') as command,
              mock.patch('loop_manager._planning_input_fingerprint', return_value='cycle-one') as fingerprint,
              mock.patch('loop_manager._specification_input_fingerprint', return_value='spec-one'),
              mock.patch('loop_manager.plan_declarations', return_value={}),
              mock.patch('loop_manager._git_tree_entries', return_value={})):
            command.return_value.stdout = 'base-sha\n'
            result = loop_manager._run_project_result(connection, Path('/unused'), repo, Path('/unused.ini'), None, False)
            self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
            event = connection.execute("SELECT payload FROM notification_outbox WHERE event_type='work_waiting'").fetchone()
            self.assertIsNotNone(event)
            payload = json.loads(event[0])
            self.assertEqual(payload['planning_error'], 'Invalid result')
            from loop_manager_internal.notifications.policy import is_current
            self.assertTrue(is_current(connection, repo, 'work_waiting', payload))
            connection.execute("UPDATE projects SET planning_error_origin='unsafe' WHERE repo=?", (repo,))
            self.assertFalse(is_current(connection, repo, 'work_waiting', payload))
            connection.execute("UPDATE projects SET planning_error_origin='validation' WHERE repo=?", (repo,))
            fingerprint.return_value = 'changed'
            self.assertIsNone(loop_manager._stable_work_reason(connection, repo, refactoring_review_interval=5))
            fingerprint.return_value = 'cycle-one'
            connection.execute("UPDATE exec_plans SET status='VERIFYING' WHERE repo=?", (repo,))
            self.assertIsNone(loop_manager._stable_work_reason(connection, repo, refactoring_review_interval=5))

    @dummy_worker_sessions
    def test_project_block_waits_only_after_all_targets_and_recovery_are_stable(self):
        connection, repo = self._connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE exec_plans SET status='READY' WHERE repo=?", (repo,))
        connection.execute("INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
                           "VALUES (?, 'project', 'repository_state_unsafe', 'Unsafe worktree')", (repo,))
        connection.execute("UPDATE projects SET phase='PROJECT_BLOCKED' WHERE repo=?", (repo,))
        with (mock.patch('loop_manager._checkpoint_dispatch_ready', return_value=False),
              mock.patch('loop_manager._run_project_step', return_value=1),
              mock.patch('loop_manager.git') as command,
              mock.patch('loop_manager._planning_input_fingerprint', return_value='cycle-one'),
              mock.patch('loop_manager._specification_input_fingerprint', return_value='spec-one'),
              mock.patch('loop_manager.plan_declarations', return_value={}),
              mock.patch('loop_manager._git_tree_entries', return_value={})):
            command.return_value.stdout = 'base-sha\n'
            result = loop_manager._run_project_result(connection, Path('/unused'), repo, Path('/unused.ini'), None, False)
            self.assertEqual(result.kind, loop_manager.ProjectRunKind.BLOCKED)
            event = connection.execute("SELECT payload FROM notification_outbox WHERE event_type='work_waiting'").fetchone()
            self.assertIsNotNone(event)
            payload = json.loads(event[0])
            self.assertEqual(payload['project_block']['reason'], 'Unsafe worktree')
            self.assertEqual(payload['plans'][0]['status'], 'READY')
            connection.execute("UPDATE exec_plans SET status='VERIFYING' WHERE repo=?", (repo,))
            self.assertIsNone(loop_manager._stable_work_reason(connection, repo, refactoring_review_interval=5))
            connection.execute("UPDATE exec_plans SET status='READY' WHERE repo=?", (repo,))
            connection.execute("INSERT INTO recovery_records (repo, subject, identity, reason_code, disposition, "
                "pending_action, phase, fingerprint, progress_marker, occurrence, attempts, detail) "
                "VALUES (?, 'G-018', 'id', 'git_lock', 'transient_retry', 'retry', 'WORKING', 'fp', 'm', 1, 1, 'lock')", (repo,))
            self.assertIsNone(loop_manager._stable_work_reason(connection, repo, refactoring_review_interval=5))

    @dummy_worker_sessions
    def test_project_block_and_planning_error_report_actual_causes(self):
        connection, repo = self._connection()
        connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
            "VALUES (?, 'project', 'repository_state_unsafe', 'Unsafe worktree')",
            (repo,),
        )
        connection.execute(
            "UPDATE projects SET phase = 'PROJECT_BLOCKED' WHERE repo = ?", (repo,),
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=1):
            blocked = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(blocked.kind, loop_manager.ProjectRunKind.BLOCKED)
        self.assertIn("Unsafe worktree", blocked.detail)
        connection.execute("DELETE FROM project_blocks WHERE repo = ?", (repo,))
        connection.execute(
            "UPDATE projects SET phase = 'PLANNING', planning_error = 'Invalid plans' "
            "WHERE repo = ?", (repo,),
        )
        with mock.patch("loop_manager._checkpoint_dispatch_ready", return_value=False), \
                mock.patch("loop_manager._run_project_step", return_value=1):
            planning = loop_manager._run_project_result(
                connection, Path("/unused"), repo, Path("/unused.ini"),
                None, False,
            )
        self.assertEqual(planning.kind, loop_manager.ProjectRunKind.OPERATIONAL_ERROR)
        self.assertIn("planning_error: Invalid plans", planning.detail)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'work_waiting'"
        ).fetchone()[0], 0)
        connection.close()
