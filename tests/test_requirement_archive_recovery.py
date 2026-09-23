"""Latest-base archive recovery ordering and bounded race policy."""

from types import SimpleNamespace
from dataclasses import replace
import unittest
from unittest import mock

import requirement_archive as archive
import requirement_archive_state
import runtime_state
from requirement_archive_recovery import (
    ArchiveRecoveryPorts, ArchiveRecoveryService,
)
from recovery_runtime import RecoveryAction, RecoveryDisposition
from tests import test_runtime_state


class ArchiveRecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.base = "base-2"
        self.applied = "base-1"
        self.snapshot = archive.ArchiveSnapshot(
            "base-2", "catalog", "states", "G-001", (), (),
        )
        self.execution = SimpleNamespace(
            repo="repo", plan_id="G-001", phase="IMPLEMENTING",
            active_sha="base-1", worker_session_id="saved-worker",
        )
        self.repository = mock.Mock()
        self.repository.get.return_value = None
        self.repository.put.return_value = SimpleNamespace(generation=1)
        self.owned = True
        self.catalog_kind = "reconciled"

    def ports(self):
        def fetch():
            self.calls.append("fetch")
            return self.base

        def observe():
            self.calls.append("pr")

        def reconcile(base):
            self.calls.append("catalog")
            return SimpleNamespace(
                kind=SimpleNamespace(value=self.catalog_kind),
                revision_sha=base,
            )

        def owned():
            self.calls.append("ownership")
            return self.owned

        def applied():
            self.calls.append("head")
            return self.applied

        def refresh(base):
            self.calls.append("rebase")
            self.applied = base

        def calculate(base):
            self.calls.append("snapshot")
            return self.snapshot

        def current():
            self.calls.append("current")
            return self.base

        return ArchiveRecoveryPorts(
            fetch, observe, reconcile, owned, applied, refresh, calculate,
            current, lambda: self.execution,
        )

    def test_owned_lag_refreshes_before_pinning_target(self):
        result = ArchiveRecoveryService(self.repository, self.ports()).run()
        self.assertIsNone(result.decision)
        self.assertEqual(result.target_generation, 1)
        self.assertEqual(self.calls[:6],
                         ["fetch", "pr", "catalog", "ownership", "head", "rebase"])
        self.repository.put.assert_called_once()
        self.assertEqual(self.repository.put.call_args.kwargs["stage"],
                         "WORKTREE_UPDATED")

    def test_unowned_worktree_is_final_without_rebase(self):
        self.owned = False
        result = ArchiveRecoveryService(
            self.repository,
            replace(self.ports(), plan_revision=lambda: 'plan-revision'),
        ).run()
        self.assertEqual(result.decision.disposition,
                         RecoveryDisposition.USER_ACTION_REQUIRED)
        self.assertEqual(result.decision.action, RecoveryAction.FINAL_BLOCK)
        self.assertEqual(result.decision.observation.plan_revision,
                         'plan-revision')
        self.assertNotIn("rebase", self.calls)
        self.repository.put.assert_not_called()

    def test_resume_keeps_pending_worker_correction_and_move_set(self):
        self.applied = 'base-2'
        source = 'docs/requirements/source.md'
        self.repository.get.return_value = SimpleNamespace(
            generation=4, snapshot_token=self.snapshot.token,
            stage='CORRECTION_PENDING',
            snapshot=self.snapshot.with_moves((source,)),
        )

        ArchiveRecoveryService(self.repository, self.ports()).run()

        kwargs = self.repository.put.call_args.kwargs
        self.assertEqual(kwargs['expected_generation'], 4)
        self.assertEqual(kwargs['stage'], 'CORRECTION_PENDING')
        self.assertEqual(self.repository.put.call_args.args[1].move_set,
                         (source,))
        self.assertNotIn('rebase', self.calls)

    def test_continuous_remote_race_pauses_without_target(self):
        self.snapshot = archive.ArchiveSnapshot(
            "old-base", "catalog", "states", "G-001", (), (),
        )
        result = ArchiveRecoveryService(
            self.repository, self.ports(), max_attempts=2,
        ).run()
        self.assertEqual(result.decision.action, RecoveryAction.PAUSE)
        self.assertEqual(result.decision.disposition,
                         RecoveryDisposition.TRANSIENT_RETRY)
        self.assertEqual(self.calls.count("fetch"), 2)
        self.repository.put.assert_not_called()
        self.repository.pause.assert_called_once()

    def test_same_runtime_error_uses_the_failing_port_reason(self):
        base_ports = self.ports()
        fetch_error = ArchiveRecoveryService(
            self.repository,
            replace(base_ports, fetch_base=mock.Mock(side_effect=RuntimeError('busy'))),
        ).run()
        pr_error = ArchiveRecoveryService(
            self.repository,
            replace(base_ports, observe_pull_request=mock.Mock(side_effect=RuntimeError('busy'))),
        ).run()
        self.assertEqual(fetch_error.decision.observation.reason.value,
                         'fetch_failed')
        self.assertEqual(pr_error.decision.observation.reason.value,
                         'service_unavailable')
        self.repository.put.assert_not_called()

    def test_real_repository_pauses_race_without_touching_execution(self):
        connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(connection.close)
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.activate(
            'repo', 'G-047', activation_sha='base-1',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        expected = runtime.get('repo', 'G-047')
        stale = archive.ArchiveSnapshot(
            'base-1', 'catalog', 'states', 'G-047', (), (),
        )
        ports = ArchiveRecoveryPorts(
            fetch_base=lambda: 'base-2',
            observe_pull_request=lambda: None,
            reconcile_catalog=lambda base: SimpleNamespace(
                kind=SimpleNamespace(value='reconciled'), revision_sha=base,
            ),
            workspace_owned=lambda: True,
            applied_base=lambda: 'base-2',
            refresh_worktree=lambda base: self.fail('unexpected rebase'),
            calculate_snapshot=lambda base: stale,
            current_base=lambda: 'base-2',
            execution_snapshot=lambda: runtime.get('repo', 'G-047'),
        )
        repository = requirement_archive_state.ArchiveTargetRepository(connection)

        result = ArchiveRecoveryService(repository, ports, max_attempts=2).run()

        self.assertEqual(result.decision.action, RecoveryAction.PAUSE)
        self.assertEqual(repository.get('repo', 'G-047').stage, 'PAUSED')
        self.assertEqual(runtime.get('repo', 'G-047'), expected)
        self.assertEqual(connection.execute(
            'SELECT COUNT(*) FROM notification_outbox'
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
