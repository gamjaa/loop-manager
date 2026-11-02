import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from loop_manager_internal.notifications import notifications
from loop_manager_internal.notifications import work_exhaustion

from support.notification import NotificationFixture


class CompletionCycleTest(NotificationFixture, unittest.TestCase):
    @staticmethod
    def _snapshot(cycle: str) -> work_exhaustion.WorkReasonSnapshot:
        return work_exhaustion.WorkReasonSnapshot(cycle, (), (), None, None)

    def _complete_project(
        self,
        connection: sqlite3.Connection,
        fingerprint: str,
    ) -> None:
        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.execute(
            "UPDATE projects SET phase = NULL, planning_error = NULL, "
            "planning_fingerprint = ? WHERE repo = ?",
            (fingerprint, "/projects/alpha"),
        )
        notifications.record_merged_plan(
            connection, "/projects/alpha", "G-018", "Reliable notices"
        )
        self.confirm_final_review(connection, "/projects/alpha", fingerprint)
        connection.commit()

    def test_completion_requires_gates_and_sends_silent_notice(self):
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE projects SET planning_fingerprint = 'cycle-one'")
        self.assertFalse(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.assertIsNone(connection.execute('SELECT completed_at FROM project_completion_cycles').fetchone())
        self._complete_project(connection, 'cycle-one')
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        row = connection.execute('SELECT * FROM project_completion_cycles').fetchone()
        self.assertEqual(row['cycle_key'], 'cycle-one')
        self.assertEqual(row['last_completed_plan_id'], 'G-018')
        self.assertEqual(row['last_completed_title'], 'Reliable notices')
        self.assertIsNotNone(row['completed_at'])
        event = self.events(connection)[0]
        self.assertEqual(event['event_type'], 'project_completed')
        self.assertTrue(json.loads(event['payload'])['silent'])
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock()), 1)

    def test_completion_while_disabled_cancels_legacy_wait_and_survives_restart(self):
        for cursor in (0, 1):
            with self.subTest(cursor=cursor), tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
                database = Path(directory) / 'state.db'
                connection = self.connection(database=database)
                cleanup.callback(connection.close)
                repo = '/projects/alpha'
                notifications._insert(connection, 'legacy-wait', repo, 'work_waiting', {'project': 'alpha'})
                connection.execute("UPDATE notification_outbox SET rendered_text='frozen text', confirmed_pieces=?, attempts=2, last_error='prior failure'", (cursor,))
                before = dict(self.events(connection)[0])
                notifications.configure_session(connection, enabled=False)
                self._complete_project(connection, 'cycle-one')
                self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
                self.assertIsNotNone(connection.execute('SELECT completed_at FROM project_completion_cycles').fetchone()[0])
                connection.close()
                restarted = sqlite3.connect(database)
                cleanup.callback(restarted.close)
                restarted.row_factory = sqlite3.Row
                notifications.configure_session(restarted, enabled=True)
                adapter = mock.Mock()
                for _ in range(2):
                    self.assertTrue(notifications.enqueue_completion_if_ready(restarted, repo, self._snapshot('cycle-one')))
                    self.assertEqual(notifications.dispatch_pending(restarted, 'token', 'chat', adapter=adapter), 0)
                adapter.assert_not_called()
                after = dict(self.events(restarted)[0])
                self.assertIsNotNone(after['canceled_at'])
                for field in before.keys() - {'canceled_at'}:
                    self.assertEqual(after[field], before[field], field)
                self.assertEqual(len(self.events(restarted)), 1)

    def test_same_and_archive_only_cycle_preserve_completion_and_new_input_updates_it(self):
        connection = self.connection()
        repo = '/projects/alpha'
        self._complete_project(connection, 'cycle-one')
        for _ in range(2):
            self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        connection.execute('UPDATE project_completion_cycles SET completed_at=NULL')
        connection.commit()
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.assertIsNotNone(connection.execute('SELECT completed_at FROM project_completion_cycles').fetchone()[0])
        connection.execute("UPDATE project_completion_cycles SET completed_at='preserved completion time'")
        connection.execute("UPDATE projects SET processed_sha = 'archive-only-sha'")
        connection.commit()
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.assertEqual(tuple(connection.execute('SELECT cycle_key, completed_at FROM project_completion_cycles').fetchone()), ('cycle-one', 'preserved completion time'))
        connection.execute("UPDATE projects SET planning_fingerprint = 'cycle-two'")
        connection.commit()
        self.assertFalse(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.confirm_final_review(connection, repo, 'cycle-two')
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-two')))
        completion = connection.execute('SELECT cycle_key, completed_at FROM project_completion_cycles').fetchone()
        self.assertEqual(completion[0], 'cycle-two')
        self.assertNotEqual(completion[1], 'preserved completion time')
        events = self.events(connection)
        self.assertEqual(len(events), 2)
        self.assertIsNotNone(events[0]['canceled_at'])
        self.assertIsNone(events[1]['canceled_at'])

    def test_completion_redacts_saved_title_before_delivery(self):
        connection = self.connection()
        repo = '/projects/alpha'
        self._complete_project(connection, 'cycle-one')
        connection.execute("UPDATE project_completion_cycles SET last_completed_title='Bearer private-token' WHERE repo=?", (repo,))
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.assertNotIn('private-token', self.events(connection)[0]['payload'])
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
        self.assertNotIn('private-token', adapter.call_args.args[2])
        self.assertEqual(connection.execute('SELECT last_completed_title FROM project_completion_cycles').fetchone()[0], 'Bearer private-token')

    def test_completion_generation_migration_preserves_old_cycle(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE projects(repo TEXT PRIMARY KEY);
            INSERT INTO projects VALUES ('repo');
            CREATE TABLE project_completion_cycles (
                repo TEXT PRIMARY KEY, cycle_key TEXT, last_completed_plan_id TEXT,
                last_completed_title TEXT, completed_at TEXT);
            INSERT INTO project_completion_cycles VALUES ('repo', 'cycle', 'G-001', 'Title', 'old time');
        """)
        notifications.ensure_schema(connection)
        self.assertEqual(connection.execute('SELECT cycle_key, last_completed_title, completed_at, generation '
                                            'FROM project_completion_cycles').fetchone(),
                         ('cycle', 'Title', 'old time', 0))

    def test_actual_progress_allows_new_completion_with_same_inputs(self):
        connection = self.connection()
        repo = '/projects/alpha'
        self._complete_project(connection, 'cycle-one')
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock()), 1)
        notifications.mark_work_available(connection, repo)
        notifications.mark_work_available(connection, repo)
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        events = self.events(connection)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(json.loads(events[0]['payload'])['generation'], json.loads(events[1]['payload'])['generation'])
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock()), 1)
        self.assertTrue(all(event['sent_at'] for event in self.events(connection)))

    def test_completion_cancels_remaining_pieces_when_work_changes(self):
        connection = self.connection()
        repo = '/projects/alpha'
        self._complete_project(connection, 'cycle-one')
        connection.execute("UPDATE project_completion_cycles SET last_completed_title=?", ('x' * 5000,))
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        calls = []
        def send(_token, _chat, piece):
            calls.append(piece)
            connection.execute("UPDATE exec_plans SET status='READY' WHERE repo=?", (repo,))
            connection.commit()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=send), 0)
        self.assertEqual(len(calls), 1)
        row = self.events(connection)[0]
        self.assertEqual(row['confirmed_pieces'], 1)
        self.assertIsNotNone(row['canceled_at'])
        self.assertIsNotNone(row['rendered_text'])
        self.assertIsNone(row['sent_at'])

    def test_late_completion_confirmation_does_not_advance_new_cycle(self):
        connection = self.connection()
        repo = '/projects/alpha'
        self._complete_project(connection, 'cycle-one')
        connection.execute("UPDATE project_completion_cycles SET last_completed_title=?", ('x' * 5000,))
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        calls = []
        def change_cycle(_token, _chat, piece):
            calls.append(piece)
            connection.execute("UPDATE projects SET planning_fingerprint='cycle-two' WHERE repo=?", (repo,))
            self.confirm_final_review(connection, repo, 'cycle-two')
            self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-two')))
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=change_cycle), 0)
        first, second = self.events(connection)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(first['canceled_at'])
        self.assertEqual(first['confirmed_pieces'], 1)
        self.assertEqual(second['confirmed_pieces'], 0)
        self.assertIsNone(second['canceled_at'])
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock()), 1)

    def test_completion_rejects_uncertain_call_before_creation_and_claim(self):
        from loop_manager_internal.notifications.policy import is_current
        connection = self.connection()
        repo = '/projects/alpha'
        self._complete_project(connection, 'cycle-one')
        connection.execute("INSERT INTO task_call_reservations (token,repo,plan_id,kind,uncertain) "
                           "VALUES ('reserved', ?, 'G-999', 'verify', 1)", (repo,))
        self.assertFalse(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.assertEqual(self.events(connection), [])
        connection.execute("DELETE FROM task_call_reservations WHERE token='reserved'")
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        payload = json.loads(self.events(connection)[0]['payload'])
        connection.execute("INSERT INTO task_call_reservations (token,repo,plan_id,kind,uncertain) "
                           "VALUES ('reserved', ?, 'G-999', 'verify', 1)", (repo,))
        self.assertFalse(is_current(connection, repo, 'project_completed', payload))

    def test_completion_claim_rechecks_maintenance_and_recovery(self):
        from loop_manager_internal.notifications.policy import is_current
        connection = self.connection()
        repo = '/projects/alpha'
        self._complete_project(connection, 'cycle-one')
        self.assertTrue(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        payload = json.loads(self.events(connection)[0]['payload'])
        self.assertTrue(is_current(connection, repo, 'project_completed', payload))
        connection.execute("INSERT INTO maintenance_cycles (repo, phase) VALUES (?, 'assessment')", (repo,))
        self.assertFalse(is_current(connection, repo, 'project_completed', payload))
        connection.execute("UPDATE maintenance_cycles SET phase='idle', publication_stage='pending' WHERE repo=?", (repo,))
        self.assertFalse(is_current(connection, repo, 'project_completed', payload))
        connection.execute("UPDATE maintenance_cycles SET publication_stage=NULL WHERE repo=?", (repo,))
        connection.execute("INSERT INTO recovery_records (repo, subject, identity, reason_code, disposition, "
            "pending_action, phase, fingerprint, progress_marker, occurrence, attempts, detail) "
            "VALUES (?, '__project__', 'id', 'git_lock', 'transient_retry', 'retry', 'PLANNING', 'fp', 'm', 1, 1, 'lock')", (repo,))
        self.assertFalse(is_current(connection, repo, 'project_completed', payload))
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock()), 0)
        self.assertIsNotNone(self.events(connection)[0]['canceled_at'])

    def test_completion_rechecks_final_receipt_and_work_cycle(self):
        from loop_manager_internal.storage import planning_review_state
        connection = self.connection()
        repo = '/projects/alpha'
        self._complete_project(connection, 'cycle-one')
        connection.execute("DELETE FROM planning_reviews WHERE repo=?", (repo,))
        self.assertFalse(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.confirm_final_review(connection, repo, 'cycle-one')
        connection.execute("UPDATE exec_plans SET verify_token='changed' WHERE repo=?", (repo,))
        self.assertFalse(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.confirm_final_review(connection, repo, 'cycle-one')
        planning_review_state.begin(connection, repo, purpose='delta', revision='new',
            input_fingerprint='new', specification_fingerprint='spec',
            work_cycle=planning_review_state.work_cycle(connection, repo), comparison_revision='base')
        self.assertFalse(notifications.enqueue_completion_if_ready(connection, repo, self._snapshot('cycle-one')))
        self.assertIsNone(connection.execute('SELECT completed_at FROM project_completion_cycles').fetchone()[0])

    def test_planning_error_blocks_completion(self):
        connection = self.connection()
        self._complete_project(connection, "cycle-one")
        connection.execute(
            "UPDATE projects SET planning_error = 'still blocked' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.commit()
        self.assertFalse(
            notifications.enqueue_completion_if_ready(
                connection, "/projects/alpha", self._snapshot("cycle-one")
            )
        )
        self.assertEqual(self.events(connection), [])

    def test_active_requirement_and_project_block_reject_completion(self):
        connection = self.connection()
        self._complete_project(connection, "cycle-one")
        requirement = work_exhaustion.WorkReasonSnapshot(
            "cycle-one", (), ({"id": "[R-031]", "path": "docs/requirements/[R-031] x.md",
                               "reason": "archive 확인 대기"},), None, None,
        )
        self.assertFalse(notifications.enqueue_completion_if_ready(
            connection, "/projects/alpha", requirement,
        ))
        connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, evidence) "
            "VALUES (?, 'project', 'repository_state_unsafe', 'unsafe')",
            ("/projects/alpha",),
        )
        self.assertFalse(notifications.enqueue_completion_if_ready(
            connection, "/projects/alpha", self._snapshot("cycle-one"),
        ))
        self.assertEqual(self.events(connection), [])


class WorkReasonOutboxTest(NotificationFixture, unittest.TestCase):
    def test_waiting_snapshot_and_episode_survive_restart_without_notice(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Path(directory) / 'state.db'
            connection = self.connection(database=database)
            cleanup.callback(connection.close)
            repo = '/projects/alpha'
            connection.execute(
                "UPDATE exec_plans SET depends_on = '[\"G-017\"]' "
                "WHERE repo = ? AND plan_id = 'G-018'", (repo,),
            )
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "(?, 'G-017', 'docs/plans/[G-017] Base.md', 'Base', '[]', "
                "'[]', 'normal', 'hash', 'PR_OPEN')", (repo,),
            )
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "(?, 'G-019', 'docs/plans/[G-019] Choice.md', 'Choice', '[]', "
                "'[]', 'normal', 'hash', 'PLAN_BLOCKED')", (repo,),
            )
            connection.execute(
                "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                "VALUES (?, 'G-019', 'plan', 'user_decision_required', 'Choose A or B')",
                (repo,),
            )
            paths = ("docs/requirements/[R-031] Waiting.md",)
            snapshot = work_exhaustion.collect(
                connection, repo, cycle="cycle-one", active_requirements=paths,
            )
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertFalse(snapshot.completed)
            self.assertIn("G-017 (PR_OPEN)", snapshot.describe())
            self.assertIn("user_decision_required", snapshot.describe())
            self.assertIn("[R-031]", snapshot.describe())
            self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
            first = tuple(connection.execute('SELECT signature, generation FROM work_reason_episodes').fetchone())
            connection.close()
            restarted = sqlite3.connect(database)
            cleanup.callback(restarted.close)
            restarted.row_factory = sqlite3.Row
            notifications.configure_session(restarted, enabled=True)
            self.assertFalse(notifications.enqueue_waiting_reason(restarted, repo, snapshot))
            self.assertEqual(tuple(restarted.execute('SELECT signature, generation FROM work_reason_episodes').fetchone()), first)
            changed = work_exhaustion.WorkReasonSnapshot('cycle-one', snapshot.plans,
                ({'id': '[R-031]', 'path': 'docs/requirements/[R-031] Waiting.md', 'reason': '새 archive 원인'},), None, None)
            self.assertFalse(notifications.enqueue_waiting_reason(restarted, repo, changed))
            self.assertEqual(restarted.execute('SELECT generation FROM work_reason_episodes').fetchone()[0], first[1] + 1)
            notifications.mark_work_available(restarted, repo)
            self.assertEqual(restarted.execute('SELECT signature FROM work_reason_episodes').fetchone()[0], '__work_available__')
            self.assertFalse(notifications.enqueue_waiting_reason(restarted, repo, snapshot))
            self.assertEqual(restarted.execute('SELECT generation FROM work_reason_episodes').fetchone()[0], first[1] + 2)
            self.assertEqual(self.events(restarted), [])

    def test_disabled_wait_transition_cancels_legacy_notice_and_preserves_audit(self):
        connection = self.connection(enabled=False)
        repo = '/projects/alpha'
        notifications._insert(connection, 'legacy-wait', repo, 'work_waiting', {'project': 'alpha'})
        connection.execute("UPDATE notification_outbox SET attempts=2, confirmed_pieces=1, rendered_text='frozen text', last_error='network failure'")
        before = dict(self.events(connection)[0])
        for requirements in ((), ('docs/requirements/[R-031] x.md',)):
            snapshot = work_exhaustion.WorkReasonSnapshot('cycle-one', (),
                tuple({'id': path, 'path': path, 'reason': 'archive 대기'} for path in requirements), None, None)
            self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        notifications.configure_session(connection, enabled=True)
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
        adapter.assert_not_called()
        after = dict(self.events(connection)[0])
        self.assertIsNotNone(after['canceled_at'])
        for field in before.keys() - {'canceled_at'}:
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(connection.execute('SELECT generation FROM work_reason_episodes').fetchone()[0], 1)

    def test_episode_ignores_presentation_changes_but_tracks_wait_causes(self):
        connection = self.connection()
        repo = '/projects/alpha'
        plan = {'id': 'G-018', 'title': 'Choice', 'status': 'DRAFT',
                'reason': 'Decision pending', 'question': 'Choose A or B',
                'path': 'docs/plans/old.md', 'url': 'https://example/old',
                'revision': 'old'}
        requirement = {'id': '[R-031]', 'path': 'docs/requirements/old.md',
                       'reason': 'Related plan pending', 'url': 'https://example/old-requirement'}
        def observe(cycle, plan, requirement):
            snapshot = work_exhaustion.WorkReasonSnapshot(cycle, (plan,), (requirement,), None, None)
            notifications.enqueue_waiting_reason(connection, repo, snapshot)
            return connection.execute('SELECT generation FROM work_reason_episodes').fetchone()[0]
        self.assertEqual(observe('old', plan, requirement), 1)
        moved = {**plan, 'path': 'docs/plans/new.md', 'url': 'https://example/new', 'revision': 'new'}
        self.assertEqual(observe('new', moved, {**requirement, 'path': 'docs/requirements/archive/new.md', 'url': 'https://example/new-requirement'}), 1)
        self.assertEqual(observe('new', {**moved, 'question': 'Choose C'}, requirement), 2)
        self.assertEqual(observe('new', {**moved, 'reason': 'Different cause'}, requirement), 3)

    def test_wait_episode_reobservation_and_new_progress_preserve_audit(self):
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE projects SET planning_fingerprint='cycle-one' WHERE repo=?", (repo,))
        connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE repo=?", (repo,))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        self.assertEqual(len(self.events(connection)), 1)
        notifications.mark_work_available(connection, repo)
        self.assertIsNotNone(self.events(connection)[0]['canceled_at'])
        self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        self.assertEqual(len(self.events(connection)), 2)
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock()), 1)
        self.assertIsNotNone(self.events(connection)[1]['sent_at'])
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        self.assertEqual(len(self.events(connection)), 2)

    def test_wait_retry_preserves_frozen_body_and_confirmed_piece(self):
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE projects SET planning_fingerprint='cycle-one' WHERE repo=?", (repo,))
        connection.execute("UPDATE exec_plans SET status='PR_OPEN', title=? WHERE repo=?", ('x' * 5000, repo))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        calls = []
        def fail_second(_token, _chat, piece):
            calls.append(piece)
            if len(calls) == 2:
                raise RuntimeError('temporary')
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=fail_second), 0)
        before = dict(self.events(connection)[0])
        self.assertEqual(before['confirmed_pieces'], 1)
        self.assertIsNotNone(before['last_error'])
        retry = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=retry), 1)
        retry.assert_called_once_with('token', 'chat', calls[1])
        after = self.events(connection)[0]
        self.assertEqual(after['rendered_text'], before['rendered_text'])
        self.assertEqual(after['confirmed_pieces'], 2)
        self.assertIsNotNone(after['sent_at'])

    def test_late_wait_confirmation_stays_with_original_episode(self):
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE projects SET planning_fingerprint='cycle-one' WHERE repo=?", (repo,))
        connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED', title=? WHERE repo=?", ('x' * 5000, repo))
        connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                           "VALUES (?, 'G-018', 'plan', 'common_defect_pending', 'First cause')", (repo,))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        calls = []
        def update_during_send(_token, _chat, piece):
            calls.append(piece)
            connection.execute("UPDATE execplan_blocks SET evidence='New cause' WHERE repo=?", (repo,))
            changed = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
            self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, changed))
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=update_during_send), 0)
        first, second = self.events(connection)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first['confirmed_pieces'], 1)
        self.assertIsNotNone(first['canceled_at'])
        self.assertEqual(second['confirmed_pieces'], 0)
        self.assertIsNone(second['canceled_at'])
        self.assertIn('First cause', first['payload'])
        self.assertIn('New cause', second['payload'])
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock()), 1)

    def test_wait_claim_rejects_uncertain_other_task_and_unfinished_invocation(self):
        from loop_manager_internal.notifications.policy import is_current
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE projects SET planning_fingerprint='cycle-one' WHERE repo=?", (repo,))
        connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE repo=?", (repo,))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        payload = json.loads(self.events(connection)[0]['payload'])
        connection.execute("INSERT INTO task_call_reservations (token,repo,plan_id,kind,uncertain) "
                           "VALUES ('reserved', ?, 'G-999', 'verify', 1)", (repo,))
        self.assertFalse(is_current(connection, repo, 'work_waiting', payload))
        connection.execute("DELETE FROM task_call_reservations WHERE token='reserved'")
        connection.execute("INSERT INTO agent_invocations "
            "(invocation_id,repo,role,provider,model,effort,transition,attempt,started_at) "
            "VALUES ('call', ?, 'worker', 'mock', 'mock', 'low', 'new', 1, 'now')", (repo,))
        self.assertFalse(is_current(connection, repo, 'work_waiting', payload))
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=mock.Mock()), 0)
        self.assertIsNotNone(self.events(connection)[0]['canceled_at'])

    def test_wait_notice_redacts_diagnostics_without_changing_current_identity(self):
        connection = self.connection()
        repo = '/projects/alpha'
        secret = 'Bearer super-secret-token'
        connection.execute("UPDATE projects SET planning_fingerprint='cycle-one' WHERE repo=?", (repo,))
        connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo=?", (repo,))
        connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                           "VALUES (?, 'G-018', 'plan', 'common_defect_pending', ?)", (repo, secret))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        saved = self.events(connection)[0]
        self.assertNotIn('super-secret-token', saved['payload'])
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
        self.assertNotIn('super-secret-token', adapter.call_args.args[2])
        self.assertIn('<REDACTED>', adapter.call_args.args[2])
        self.assertEqual(connection.execute("SELECT evidence FROM execplan_blocks WHERE repo=?", (repo,)).fetchone()[0], secret)

    def test_wait_notice_is_independent_and_cancels_stale_remaining_pieces(self):
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE projects SET planning_fingerprint='cycle-one' WHERE repo=?", (repo,))
        connection.execute("UPDATE exec_plans SET status='PR_OPEN', title=? WHERE repo=?", ('x' * 5000, repo))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        event = self.events(connection)[0]
        self.assertFalse(json.loads(event['payload'])['silent'])
        pieces = []
        def send(_token, _chat, piece):
            pieces.append(piece)
            connection.execute("UPDATE exec_plans SET status='READY' WHERE repo=?", (repo,))
            connection.commit()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=send), 0)
        self.assertEqual(len(pieces), 1)
        saved = self.events(connection)[0]
        self.assertEqual(saved['confirmed_pieces'], 1)
        self.assertIsNotNone(saved['canceled_at'])
        self.assertIsNotNone(saved['rendered_text'])
        self.assertIsNone(saved['sent_at'])

    def test_archive_of_unrelated_merged_plan_does_not_change_episode(self):
        plan = {'id': 'G-018', 'title': 'Wait', 'status': 'PR_OPEN', 'reason': 'PR Merge 대기'}
        sources = {'G-018': {'sources': ['docs/PRODUCT.md'], 'requirements': []}}
        before = work_exhaustion.WorkReasonSnapshot('old', (plan,), (), None, None,
            {**sources, 'G-001': {'sources': ['docs/ARCHITECTURE.md'], 'requirements': []}})
        after = work_exhaustion.WorkReasonSnapshot('new', (plan,), (), None, None, sources)
        self.assertEqual(work_exhaustion.episode_signature(before), work_exhaustion.episode_signature(after))

    def test_removing_unrelated_merged_catalog_row_keeps_wait_current(self):
        from loop_manager_internal.notifications.policy import is_current
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE projects SET planning_fingerprint='cycle-one' WHERE repo=?", (repo,))
        connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE repo=?", (repo,))
        connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status) "
            "VALUES (?, 'G-001', 'docs/plans/[G-001] Done.md', 'Done', '[]', '[]', 'normal', 'hash', 'MERGED')", (repo,))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        payload = json.loads(self.events(connection)[0]['payload'])
        connection.execute("DELETE FROM exec_plans WHERE repo=? AND plan_id='G-001'", (repo,))
        self.assertTrue(is_current(connection, repo, 'work_waiting', payload))
        fresh = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, fresh))
        self.assertEqual(len(self.events(connection)), 1)

    def test_confirmed_worker_decision_exposes_its_validated_location(self):
        connection = self.connection()
        repo = '/projects/alpha'
        self.decision_block(connection)
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertEqual(snapshot.plans[0]['action_targets'], ['docs/plans/[G-018] Reliable notices.md'])

    def test_unverified_recovery_targets_do_not_request_user_action(self):
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo=?", (repo,))
        connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                           "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'Cause')", (repo,))
        connection.execute("INSERT INTO recovery_records (repo, subject, identity, reason_code, disposition, "
            "pending_action, phase, fingerprint, progress_marker, occurrence, attempts, detail, final_evidence) "
            "VALUES (?, 'G-018', 'id', 'user_decision', 'user_action_required', 'final_block', 'WORKING', "
            "'fp', 'marker', 1, 1, 'Cause', ?)", (repo, json.dumps({'user_targets': ['unverified']})))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertNotIn('action_targets', snapshot.plans[0])
        self.assertNotIn('unverified', snapshot.describe())

    def test_plan_block_without_confirmed_cause_is_not_stable(self):
        connection = self.connection()
        repo = '/projects/alpha'
        connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo=?", (repo,))
        self.assertIsNone(work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=()))

    def test_blocked_requirement_does_not_infer_user_action(self):
        connection = self.connection()
        repo = '/projects/alpha'
        path = 'docs/requirements/[R-031] Waiting.md'
        connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED', sources=?", (json.dumps([path]),))
        connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                           "VALUES (?, 'G-018', 'plan', 'common_defect_pending', 'Correction pending')", (repo,))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=(path,))
        self.assertIn('차단 해소와 재평가', snapshot.requirements[0]['reason'])
        self.assertNotIn('사용자 조치', snapshot.requirements[0]['reason'])

    def test_plan_and_project_waits_keep_causes_without_wait_notice(self):
        from dataclasses import replace
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED'")
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', 'Choose A')", (repo,))
        snapshot = work_exhaustion.collect(connection, repo, cycle='cycle-one', active_requirements=())
        self.assertIn('Choose A', snapshot.describe())
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        for _ in range(2):
            self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        self.assertEqual(connection.execute('SELECT generation FROM work_reason_episodes').fetchone()[0], 1)
        blocked = replace(snapshot, project_block={'code': 'unsafe_repository', 'reason': 'unsafe'})
        self.assertIn('unsafe', blocked.describe())
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, blocked))
        self.assertEqual(connection.execute('SELECT generation FROM work_reason_episodes').fetchone()[0], 2)
        self.assertEqual(self.events(connection), [])
