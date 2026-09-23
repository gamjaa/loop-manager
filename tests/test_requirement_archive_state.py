"""Archive target CAS must preserve execution and reject remote races."""

import unittest

import requirement_archive as archive
import requirement_archive_state as state
import runtime_state
from tests import test_runtime_state


class ArchiveTargetRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(self.connection.close)
        self.runtime = runtime_state.RuntimeStateRepository(self.connection)
        self.runtime.activate('repo', 'G-047', activation_sha='base',
                              branch='loop-manager/G-047', worktree='/state/G-047')
        self.repository = state.ArchiveTargetRepository(self.connection)
        self.snapshot = archive.ArchiveSnapshot(
            'base', 'catalog', 'states', 'G-047',
            ('docs/requirements/Feature.md',), (),
        )

    def test_target_resume_and_cas_keep_worker_session(self):
        self.runtime.update_plan(
            'repo', 'G-047', plan_values={'worker_session_id': 'worker-session'},
            enqueue=False,
        )
        expected = self.runtime.get('repo', 'G-047')
        first = self.repository.put(
            'repo', self.snapshot, expected_execution=expected,
            base_is_current=lambda oid: oid == 'base',
            snapshot_is_current=lambda: True,
        )
        self.assertEqual(first.snapshot_token, self.snapshot.token)
        self.assertEqual(self.repository.get('repo', 'G-047').generation, 1)
        second = self.repository.put(
            'repo', self.snapshot.with_moves(()),
            expected_execution=expected, expected_generation=first.generation,
            base_is_current=lambda oid: oid == 'base', stage='VALIDATED',
            snapshot_is_current=lambda: True,
        )
        self.assertEqual(second.generation, 2)
        self.assertEqual(self.runtime.get('repo', 'G-047').worker_session_id,
                         'worker-session')

    def test_remote_race_rolls_back_target(self):
        calls = 0

        def base_is_current(_oid):
            nonlocal calls
            calls += 1
            return calls < 2

        with self.assertRaises(state.ArchiveTargetConflict):
            self.repository.put(
                'repo', self.snapshot,
                expected_execution=self.runtime.get('repo', 'G-047'),
                base_is_current=base_is_current,
                snapshot_is_current=lambda: True,
            )
        self.assertIsNone(self.repository.get('repo', 'G-047'))

    def test_execution_change_rejects_stale_target(self):
        expected = self.runtime.get('repo', 'G-047')
        self.runtime.update_plan(
            'repo', 'G-047', plan_values={'worker_session_id': 'new-session'},
            enqueue=False,
        )
        with self.assertRaises(state.ArchiveTargetConflict):
            self.repository.put(
                'repo', self.snapshot, expected_execution=expected,
                base_is_current=lambda oid: True,
                snapshot_is_current=lambda: True,
            )

    def test_bounded_race_pause_has_no_accepted_candidate(self):
        expected = self.runtime.get('repo', 'G-047')
        self.repository.pause(
            'repo', 'G-047', 'moving-base', expected_execution=expected,
            attempts=3, expected_generation=None,
        )
        paused = self.repository.get('repo', 'G-047')
        self.assertEqual(paused.stage, 'PAUSED')
        self.assertEqual(paused.attempts, 3)
        self.assertEqual(paused.snapshot.candidates, ())
        self.assertEqual(self.runtime.get('repo', 'G-047'), expected)

    def test_expected_absence_rejects_concurrently_created_target(self):
        expected = self.runtime.get('repo', 'G-047')
        self.repository.put(
            'repo', self.snapshot, expected_execution=expected,
            base_is_current=lambda oid: True,
            snapshot_is_current=lambda: True,
        )
        with self.assertRaises(state.ArchiveTargetConflict):
            self.repository.put(
                'repo', self.snapshot, expected_execution=expected,
                base_is_current=lambda oid: True,
                snapshot_is_current=lambda: True,
                expected_generation=None,
            )


if __name__ == "__main__":
    unittest.main()
