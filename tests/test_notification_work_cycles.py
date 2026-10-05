import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.config.managed_config import ManagedConfigError, load_managed_config
from loop_manager_internal.notifications import notifications
from loop_manager_internal.notifications import work_exhaustion
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.recovery_state import RecoveryRepository

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
        connection.commit()

    def test_completion_payload_requires_all_gates_and_has_last_work(self):
        connection = self.connection()
        connection.execute(
            "UPDATE projects SET planning_fingerprint = 'cycle-one' WHERE repo = ?",
            ("/projects/alpha",),
        )
        self.assertFalse(
            notifications.enqueue_completion_if_ready(
                connection, "/projects/alpha", self._snapshot("cycle-one")
            )
        )
        self._complete_project(connection, "cycle-one")
        self.assertTrue(
            notifications.enqueue_completion_if_ready(
                connection, "/projects/alpha", self._snapshot("cycle-one")
            )
        )
        event = self.events(connection)[0]
        payload = json.loads(event["payload"])
        self.assertEqual(event["event_type"], "project_completed")
        self.assertEqual(payload["project"], "alpha")
        self.assertEqual(payload["last_completed_plan_id"], "G-018")
        self.assertEqual(payload["last_completed_title"], "Reliable notices")
        self.assertIn("활성 Requirement", payload["reason"])

    def test_completion_while_disabled_cancels_wait_before_reenabled_dispatch(self):
        for fail_delivery in (False, True):
            with self.subTest(fail_delivery=fail_delivery):
                directory = self.enterContext(tempfile.TemporaryDirectory())
                database = Path(directory) / "state.db"
                connection = self.connection(database=database)
                repo = "/projects/alpha"
                waiting = work_exhaustion.WorkReasonSnapshot(
                    "cycle-one", (),
                    ({"id": "[R-031]", "path": "docs/requirements/[R-031] x.md",
                      "reason": "archive 확인 대기"},), None, None,
                )
                self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, waiting))
                if fail_delivery:
                    self.assertEqual(notifications.dispatch_pending(
                        connection, "token", "chat",
                        adapter=mock.Mock(side_effect=RuntimeError("network unavailable")),
                    ), 0)
                notifications.configure_session(connection, enabled=False)
                self._complete_project(connection, "cycle-one")
                self.assertFalse(notifications.enqueue_completion_if_ready(
                    connection, repo, self._snapshot("cycle-one"),
                ))
                events = self.events(connection)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["event_type"], "work_waiting")
                self.assertIsNotNone(events[0]["canceled_at"])
                self.assertIsNone(events[0]["sent_at"])
                self.assertEqual(events[0]["attempts"], int(fail_delivery))
                connection.close()

                restarted = sqlite3.connect(database)
                self.addCleanup(restarted.close)
                restarted.row_factory = sqlite3.Row
                notifications.configure_session(restarted, enabled=True)
                delivered = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(
                    restarted, "token", "chat", adapter=delivered,
                ), 0)
                delivered.assert_not_called()
                for _ in range(2):
                    self.assertTrue(notifications.enqueue_completion_if_ready(
                        restarted, repo, self._snapshot("cycle-one"),
                    ))
                self.assertEqual(notifications.dispatch_pending(
                    restarted, "token", "chat", adapter=delivered,
                ), 1)
                delivered.assert_called_once()
                self.assertEqual(
                    [row["event_type"] for row in self.events(restarted)
                     if row["sent_at"] is not None],
                    ["project_completed"],
                )

    def test_same_and_archive_only_cycle_deduplicate_but_new_input_reopens(self):
        connection = self.connection()
        self._complete_project(connection, "cycle-one")
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha", self._snapshot("cycle-one"))
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha", self._snapshot("cycle-one"))

        # Archive-only handling retains the prior planning fingerprint.
        connection.execute(
            "UPDATE projects SET processed_sha = 'archive-only-sha' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.commit()
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha", self._snapshot("cycle-one"))
        self.assertEqual(len(self.events(connection)), 1)

        connection.execute(
            "UPDATE projects SET planning_fingerprint = 'cycle-two' WHERE repo = ?",
            ("/projects/alpha",),
        )
        connection.commit()
        notifications.enqueue_completion_if_ready(connection, "/projects/alpha", self._snapshot("cycle-two"))
        self.assertEqual(len(self.events(connection)), 2)

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
    def test_waiting_snapshot_lists_confirmed_causes_and_deduplicates_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            repo = "/projects/alpha"
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
            self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, snapshot))
            first = self.events(connection)[0]
            message = notifications._message("work_waiting", json.loads(first["payload"]))
            self.assertIn("G-017 Base: PR Merge 대기", message)
            self.assertIn("G-019 Choice", message)
            connection.close()

            restarted = sqlite3.connect(database)
            restarted.row_factory = sqlite3.Row
            notifications.configure_session(restarted, enabled=True)
            notifications.enqueue_waiting_reason(restarted, repo, snapshot)
            self.assertEqual(len(self.events(restarted)), 1)
            changed = work_exhaustion.WorkReasonSnapshot(
                "cycle-one", snapshot.plans,
                ({"id": "[R-031]", "path": paths[0], "reason": "새 archive 원인"},),
                None, None,
            )
            notifications.enqueue_waiting_reason(restarted, repo, changed)
            self.assertEqual(len(self.events(restarted)), 2)
            self.assertIsNotNone(self.events(restarted)[0]["canceled_at"])
            notifications.mark_work_available(restarted, repo)
            notifications.enqueue_waiting_reason(restarted, repo, snapshot)
            self.assertEqual(len(self.events(restarted)), 3)
            restarted.close()

    def test_disabled_wait_transition_cancels_stale_notice_before_restart(self):
        for remaining_requirement in (False, True):
            for failed_delivery in (False, True):
                with self.subTest(requirement=remaining_requirement, failed=failed_delivery):
                    directory = self.enterContext(tempfile.TemporaryDirectory())
                    database = Path(directory) / "state.db"
                    connection = self.connection(database=database)
                    repo = "/projects/alpha"
                    connection.execute(
                        "UPDATE exec_plans SET status = 'PR_OPEN' WHERE repo = ?", (repo,),
                    )
                    waiting = work_exhaustion.collect(
                        connection, repo, cycle="cycle-one", active_requirements=(),
                    )
                    assert waiting is not None
                    self.assertIn("PR Merge 대기", waiting.describe())
                    self.assertTrue(notifications.enqueue_waiting_reason(connection, repo, waiting))
                    if failed_delivery:
                        self.assertEqual(notifications.dispatch_pending(
                            connection, "token", "chat",
                            adapter=mock.Mock(side_effect=RuntimeError("network unavailable")),
                        ), 0)
                    notifications.configure_session(connection, enabled=False)
                    # An unchanged wait still owns its pending delivery.
                    self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, waiting))
                    self.assertIsNone(self.events(connection)[0]["canceled_at"])
                    connection.execute(
                        "UPDATE exec_plans SET status = 'PLAN_BLOCKED' WHERE repo = ?", (repo,),
                    )
                    connection.execute(
                        "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                        "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'PR closed without merge')",
                        (repo,),
                    )
                    changed = work_exhaustion.collect(
                        connection, repo, cycle="cycle-one",
                        active_requirements=("docs/requirements/[R-031] x.md",)
                        if remaining_requirement else (),
                    )
                    assert changed is not None
                    self.assertEqual(changed.has_distinct_wait, remaining_requirement)
                    self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, changed))
                    events = self.events(connection)
                    self.assertEqual(len(events), 1)
                    self.assertIsNotNone(events[0]["canceled_at"])
                    self.assertIsNone(events[0]["sent_at"])
                    self.assertEqual(events[0]["attempts"], int(failed_delivery))
                    connection.close()

                    restarted = sqlite3.connect(database)
                    self.addCleanup(restarted.close)
                    restarted.row_factory = sqlite3.Row
                    notifications.configure_session(restarted, enabled=True)
                    delivered = mock.Mock()
                    self.assertEqual(notifications.dispatch_pending(
                        restarted, "token", "chat", adapter=delivered,
                    ), 0)
                    delivered.assert_not_called()
                    for _ in range(2):
                        self.assertEqual(notifications.enqueue_waiting_reason(
                            restarted, repo, changed,
                        ), remaining_requirement)
                    self.assertEqual(notifications.dispatch_pending(
                        restarted, "token", "chat", adapter=delivered,
                    ), int(remaining_requirement))
                    self.assertEqual(len(self.events(restarted)), 1 + int(remaining_requirement))

    def test_block_only_suppresses_wait_and_disabled_delivery_keeps_reason(self):
        connection = self.connection(enabled=False)
        repo = "/projects/alpha"
        connection.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED' WHERE repo = ?", (repo,),
        )
        connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES (?, 'G-018', 'plan', 'user_decision_required', 'Choose A')",
            (repo,),
        )
        snapshot = work_exhaustion.collect(
            connection, repo, cycle="cycle-one", active_requirements=(),
        )
        assert snapshot is not None
        self.assertIn("Choose A", snapshot.describe())
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        notifications.configure_session(connection, enabled=True)
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, snapshot))
        self.assertEqual(self.events(connection), [])

        requirement_path = "docs/requirements/[R-031] x.md"
        connection.execute(
            "UPDATE exec_plans SET sources = ? WHERE repo = ? AND plan_id = 'G-018'",
            (json.dumps([requirement_path]), repo),
        )
        related = work_exhaustion.collect(
            connection, repo, cycle="cycle-one",
            active_requirements=(requirement_path,),
        )
        assert related is not None
        self.assertIn("[R-031]", related.describe())
        self.assertFalse(related.has_distinct_wait)
        self.assertFalse(notifications.enqueue_waiting_reason(connection, repo, related))
        self.assertEqual(self.events(connection), [])

        connection.execute(
            "UPDATE exec_plans SET sources = '[]' "
            "WHERE repo = ? AND plan_id = 'G-018'", (repo,),
        )
        with_requirement = work_exhaustion.collect(
            connection, repo, cycle="cycle-one",
            active_requirements=(requirement_path,),
        )
        assert with_requirement is not None
        self.assertTrue(with_requirement.has_distinct_wait)
        notifications.enqueue_waiting_reason(connection, repo, with_requirement)
        failed = mock.Mock(side_effect=RuntimeError("network unavailable"))
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=failed,
        ), 0)
        event = self.events(connection)[0]
        self.assertEqual(event["attempts"], 1)
        self.assertIsNone(event["sent_at"])
        delivered = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=delivered,
        ), 1)
        self.assertIsNotNone(self.events(connection)[0]["sent_at"])


