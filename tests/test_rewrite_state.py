"""신규 DB 계약: 운영 데이터 없이 짧은 트랜잭션·경합·재시작을 확인한다."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from loop_manager_internal.storage.state import Store, StateError, SCHEMA_VERSION


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'loop-manager.db'
        self.store = Store(self.path, busy_ms=60)
        self.store.initialize()
        self.store.register('owner/repo', '/project', 'git@github.com:owner/repo.git', 'develop', 'codex')
        self.add_plan()

    def add_plan(self, plan_id='G-001', dependencies=(), **updates):
        with self.store.transaction() as db:
            db.execute('INSERT INTO exec_plans(repo,plan_id,source_path,source_oid,title,sources,depends_on,'
                       'complexity,risk,priority,draft,branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('owner/repo',plan_id,f'docs/plans/{plan_id}.md','blob','계획','[]',json.dumps(dependencies),
                        'low','low','normal',0,f'loop-manager/{plan_id}'))
            db.execute('INSERT INTO exec_plan_runs(repo,plan_id) VALUES(?,?)', ('owner/repo',plan_id))
            for key, value in updates.items():
                self.assertIn(key, ('status','draft'))
                db.execute(f'UPDATE exec_plans SET {key}=? WHERE repo=? AND plan_id=?',
                           (value,'owner/repo',plan_id))

    def test_initialization_restart_and_exact_responsibility_tables(self):
        self.store.initialize()
        restarted = Store(self.path)
        self.assertEqual(restarted.plan('owner/repo','G-001')['status'], 'READY')
        tables = {row['name'] for row in restarted.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'projects','pause_scopes','exec_plans','requirement_states','exec_plan_runs',
                                 'agent_calls','validation_receipts','managed_prs','external_jobs',
                                 'notification_outbox','exec_plan_tombstones'})
        self.assertNotIn('phase', {row['name'] for row in restarted.rows('PRAGMA table_info(exec_plan_runs)')})
        self.assertEqual(restarted.rows('PRAGMA user_version')[0]['user_version'], SCHEMA_VERSION)
        self.assertEqual(restarted.rows('PRAGMA journal_mode')[0]['journal_mode'], 'wal')

    def test_missing_and_legacy_database_are_not_created_or_migrated(self):
        missing = Store(self.path.with_name('missing.db'))
        with self.assertRaises(StateError):
            missing.rows('SELECT 1')
        self.assertFalse(missing.path.exists())
        legacy_path = self.path.with_name('legacy.db')
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.execute('CREATE TABLE original(value TEXT)')
            db.execute("INSERT INTO original VALUES('보존')")
        legacy = Store(legacy_path)
        with self.assertRaises(StateError):
            legacy.initialize()
        with closing(sqlite3.connect(legacy_path)) as db:
            self.assertEqual(db.execute('SELECT * FROM original').fetchone()[0], '보존')

    def test_wal_reader_during_writer_and_bounded_busy_failure(self):
        with self.store.transaction() as writer:
            writer.execute("UPDATE exec_plans SET title='미확정' WHERE plan_id='G-001'")
            self.assertEqual(Store(self.path).plan('owner/repo','G-001')['title'], '계획')
            started = time.monotonic()
            with self.assertRaisesRegex(StateError, 'locked'):
                Store(self.path,busy_ms=60).pause('*',True)
            self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(self.store.plan('owner/repo','G-001')['title'], '미확정')
        self.store.pause('*',True)

    def test_exception_rolls_back_and_releases_writer(self):
        with self.assertRaisesRegex(RuntimeError, '중단'):
            with self.store.transaction() as db:
                db.execute("UPDATE exec_plans SET status='WORKING'")
                raise RuntimeError('중단')
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'], 'READY')
        self.store.pause('*',True)

    def test_competing_claims_have_one_winner_and_cancel_rejects_late_result(self):
        barrier = threading.Barrier(2)
        results = []
        def claim():
            barrier.wait()
            results.append(Store(self.path).claim('owner/repo','G-001','READY','WORKING','input'))
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(value for value in results if value is not None), [1])
        self.assertEqual(results.count(None), 1)
        self.store.cancel('owner/repo','G-001','revision','삭제 확인')
        self.assertFalse(self.store.apply('owner/repo','G-001',1,'input','WORKING','VERIFYING'))
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'], 'CANCELLED')
        self.assertEqual(len(self.store.rows('SELECT * FROM exec_plan_tombstones')), 1)

    def test_pause_resume_and_manual_reservation_are_seen_before_claim(self):
        self.store.pause('*',True)
        self.assertIsNone(self.store.claim('owner/repo','G-001','READY','WORKING','input'))
        self.store.pause('*',False)
        request = self.store.request('owner/repo','G-001','manual_start')
        self.assertEqual(request,self.store.request('owner/repo','G-001','manual_start'))
        self.assertIsNone(self.store.claim('owner/repo','G-001','READY','WORKING','input'))
        with self.assertRaises(StateError):
            self.store.request('owner/repo','G-001','manual_complete')
        self.store.finish_request('owner/repo','G-001',request,'수동 준비 완료',cli_final=True,
                                  manual_mode='MANUAL_WORKING',worktree='/isolated')
        self.assertEqual(self.store.plan('owner/repo','G-001')['worktree'], '/isolated')
        self.assertFalse(self.store.rows('SELECT * FROM notification_outbox'))

    def test_manual_dependency_must_be_merged_and_draft_cannot_start(self):
        self.add_plan('G-002', ['G-001'])
        with self.assertRaisesRegex(StateError,'선행'):
            self.store.request('owner/repo','G-002','manual_start')
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-001'")
        self.assertTrue(self.store.request('owner/repo','G-002','manual_start'))
        self.add_plan('G-003',draft=1)
        with self.assertRaises(StateError):
            self.store.request('owner/repo','G-003','manual_start')

    def test_async_result_and_outbox_are_atomic_and_request_scoped(self):
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='BLOCKED'")
        request = self.store.request('owner/repo','G-001','unblock')
        self.assertEqual(request,self.store.request('owner/repo','G-001','unblock'))
        with self.assertRaises(StateError):
            self.store.request('owner/repo','G-001','manual_start')
        original_notify = self.store.notify
        self.store.notify = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('전송 의도 중단'))
        with self.assertRaises(RuntimeError):
            self.store.finish_request('owner/repo','G-001',request,'재개 불가: 판단 필요')
        self.assertIsNone(self.store.plan('owner/repo','G-001')['request_result'])
        self.store.notify = original_notify
        self.assertTrue(self.store.finish_request('owner/repo','G-001',request,'재개 불가: 판단 필요'))
        self.assertFalse(self.store.finish_request('owner/repo','G-001',request,'늦은 성공'))
        self.assertEqual(len(self.store.rows('SELECT * FROM notification_outbox')), 1)
        next_request = self.store.request('owner/repo','G-001','unblock')
        self.assertNotEqual(request,next_request)
        self.assertFalse(self.store.finish_request('owner/repo','G-001',request,'이전 결과'))
        self.assertTrue(self.store.finish_request('owner/repo','G-001',next_request,'새 결과'))
        self.assertEqual(len(self.store.rows('SELECT * FROM notification_outbox')), 2)

    def test_validation_receipt_only_written_with_current_result(self):
        generation = self.store.claim('owner/repo','G-001','READY','VERIFYING','candidate')
        self.assertFalse(self.store.apply('owner/repo','G-001',generation,'old','VERIFYING','REVIEWING',
                                         stage='verify',evidence='PASS'))
        self.assertFalse(self.store.rows('SELECT * FROM validation_receipts'))
        self.assertTrue(self.store.apply('owner/repo','G-001',generation,'candidate','VERIFYING','REVIEWING',
                                        stage='verify',evidence='정상 종료 확인'))
        self.assertEqual(self.store.rows('SELECT * FROM validation_receipts')[0]['success'], 1)


if __name__ == '__main__':
    unittest.main()
