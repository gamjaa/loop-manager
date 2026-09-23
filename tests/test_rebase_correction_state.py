"""Durable no-progress and turn CAS tests."""

import sqlite3
from unittest import TestCase

from rebase_correction import ConflictObservation, CorrectionSource
from rebase_correction_state import CorrectionRace, RebaseCorrectionRepository


class RebaseCorrectionStateTests(TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.repository = RebaseCorrectionRepository(self.connection)

    @staticmethod
    def observation(blob='a', content='a'):
        return ConflictObservation(
            'definition', 'base', 'before', (('app.py', 1, blob),),
            (('app.py', content),), '', 'position', CorrectionSource.REBASE,
        )

    def test_three_worker_corrections_with_same_progress_and_restart(self):
        original = self.observation()
        current = self.repository.observe('repo', 'G-045', original)
        self.assertEqual(current.occurrence, 0)
        self.assertEqual((current.plan_revision, current.target_base, current.before_head),
                         ('definition', 'base', 'before'))
        for count in range(1, 4):
            reopened = RebaseCorrectionRepository(self.connection)
            current = reopened.observe('repo', 'G-045', original, corrected=True,
                                       expected_generation=current.generation,
                                       worker_session='same-session',
                                       worker_result=f'result-{count}')
            self.assertEqual(current.occurrence, count)
            self.assertEqual(current.worker_session, 'same-session')
        self.assertEqual(len(current.audit), 4)
        self.assertEqual([event['occurrence'] for event in current.audit], [0, 1, 2, 3])

    def test_failed_worker_result_is_audited_without_becoming_replayable(self):
        current = self.repository.observe('repo', 'G-045', self.observation())
        saved = self.repository.observe(
            'repo', 'G-045', self.observation(), corrected=True,
            expected_generation=current.generation, worker_result='abnormal exit',
            persist_worker_result=False,
        )
        reopened = RebaseCorrectionRepository(self.connection).get('repo', 'G-045')
        self.assertEqual(reopened.occurrence, 1)
        self.assertEqual(reopened.audit[-1]['worker_result'], 'abnormal exit')
        self.assertIsNone(reopened.worker_result)
        self.assertIsNone(reopened.pending_turn)

    def test_progress_and_conflict_cycle_preserve_old_history(self):
        a = self.observation()
        changed = self.observation(content='resolved')
        b = self.observation(blob='b')
        current = self.repository.observe('repo', 'G-045', a)
        current = self.repository.observe('repo', 'G-045', a, corrected=True,
                                          expected_generation=current.generation)
        self.assertEqual(current.occurrence, 1)
        current = self.repository.observe('repo', 'G-045', changed, corrected=True,
                                          expected_generation=current.generation)
        self.assertEqual(current.occurrence, 0)
        current = self.repository.observe('repo', 'G-045', b, corrected=True,
                                          expected_generation=current.generation)
        self.assertEqual(current.occurrence, 0)
        current = self.repository.observe('repo', 'G-045', a, corrected=True,
                                          expected_generation=current.generation)
        self.assertEqual(current.occurrence, 2)

    def test_pending_turn_and_result_survive_restart_and_cas(self):
        current = self.repository.observe('repo', 'G-045', self.observation())
        turn = self.repository.begin_turn('repo', 'G-045',
                                          expected_generation=current.generation,
                                          session='session')
        reopened = RebaseCorrectionRepository(self.connection)
        self.assertEqual(reopened.begin_turn('repo', 'G-045',
                                             expected_generation=turn.generation,
                                             session='session').pending_turn,
                         turn.pending_turn)
        finished = reopened.finish_turn('repo', 'G-045',
                                        expected_generation=turn.generation,
                                        turn=turn.pending_turn, session='session',
                                        result='worker output', scope_marker='accepted')
        self.assertEqual(finished.worker_result, 'worker output')
        with self.assertRaises(CorrectionRace):
            reopened.finish_turn('repo', 'G-045',
                                 expected_generation=turn.generation,
                                 turn=turn.pending_turn, session='session',
                                 result='duplicate', scope_marker='accepted')
