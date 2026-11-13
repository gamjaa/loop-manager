"""실제 Agent/Git 없이 단계 겹침·중단 결과·요청 최종 확정을 검증한다."""
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from loop_manager_internal.application.coordinator import Coordinator
from loop_manager_internal.catalog.observer import catalog,synchronize
from loop_manager_internal.storage.state import Store
from tests.support.rewrite_fixtures import requirement,plan


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.store=Store(self.root/'loop-manager.db')
        self.store.initialize()
        self.store.register('owner/repo',self.root/'project','remote','main','codex')
        self.snapshot=catalog('base',[requirement(),plan(),plan('G-002')])
        synchronize(self.store,'owner/repo',self.snapshot)
        with self.store.transaction() as db:
            db.execute("UPDATE requirement_states SET last_planned_source_oid=source_oid,last_planned_base_sha='base'")
        self.settings=SimpleNamespace(max_ai=2,verify_timeout=10,telegram_token='',telegram_chat='')
        self.repository=Mock()
        self.repository.repo='owner/repo'
        self.repository.state_dir=self.root
        self.repository.project=self.store.rows('SELECT * FROM projects')[0]
        self.repository.refresh.return_value='base'
        self.repository.snapshot.return_value=self.snapshot
        self.github=Mock()
        self.github.pulls.return_value=[]
        self.coordinator=Coordinator(self.store,self.root,self.root,self.settings,
            repository_factory=lambda *args:self.repository,github_factory=lambda *args:self.github,agent=Mock())
        self.coordinator.candidate=lambda repo,item:(SimpleNamespace(reusable=False),'candidate:'+item['plan_id'])
        self.coordinator.prepare_worker=lambda repo,item:self.root/item['plan_id']
        self.coordinator.reviewer=lambda *args:SimpleNamespace(kind='SUCCESS',decision={'result':'PASS','evidence':'검토 완료'})
        self.published=[]
        def publish(repo,item,value):
            self.published.append(item['plan_id'])
            with self.store.transaction() as db:
                db.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE repo=? AND plan_id=?",(repo,item['plan_id']))
            return True
        self.coordinator.publish=publish

    def test_worker_verify_reviewer_overlap_with_independent_limits(self):
        verified=threading.Event()
        next_worker=threading.Event()
        events=[]
        def worker(repo,item,*args):
            events.append('worker:'+item['plan_id'])
            if item['plan_id']=='G-002':
                next_worker.set()
            return ('CANDIDATE','관련 테스트 통과','sha:'+item['plan_id'])
        def verify(repo,item,*args):
            events.append('verify:'+item['plan_id'])
            if item['plan_id']=='G-001':
                self.assertTrue(next_worker.wait(5),'Verify 중 독립 Worker가 시작되지 않았습니다.')
                verified.set()
            else:
                self.assertTrue(verified.is_set())
            return True,'검증 완료'
        self.coordinator.worker=worker
        self.coordinator.verify=verify
        self.assertEqual(self.coordinator.run(),[])
        self.assertEqual(set(self.published),{'G-001','G-002'})
        self.assertIn('verify:G-001',events)
        self.assertLess(events.index('worker:G-001'),events.index('verify:G-001'))
        self.assertEqual(self.store.rows("SELECT COUNT(*) AS n FROM exec_plans WHERE status='PR_OPEN'")[0]['n'],2)

    def test_cancelled_generation_cannot_publish_late_worker_result(self):
        def worker(repo,item,*args):
            self.store.cancel(repo,item['plan_id'],'withdrawal','사용자 철회')
            return 'CANDIDATE','늦은 반환','sha'
        self.coordinator.worker=worker
        self.coordinator.verify=Mock(side_effect=AssertionError('취소 후 Verify 금지'))
        self.coordinator.run()
        self.assertFalse(self.published)
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'CANCELLED')

    def test_unblock_keeps_feedback_pending_until_real_worker_result(self):
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='BLOCKED' WHERE plan_id='G-001'")
            db.execute("UPDATE exec_plans SET draft=1 WHERE plan_id='G-002'")
        request=self.store.request('owner/repo','G-001','unblock')
        def worker(repo,item,*args):
            self.assertIsNone(self.store.plan(repo,item['plan_id'])['request_result'])
            return 'BLOCKED','정책 질문이 아직 미해결',None
        self.coordinator.worker=worker
        self.coordinator.run()
        current=self.store.plan('owner/repo','G-001')
        self.assertEqual(current['status'],'BLOCKED')
        self.assertIn('재개 불가',current['request_result'])
        self.assertEqual(len(self.store.rows('SELECT * FROM notification_outbox WHERE event_key=?',
            (f'cli:owner/repo:G-001:{request}',))),1)

    def test_live_interrupted_process_prevents_duplicate_execution(self):
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plan_runs SET process_pid=123,process_identity='identity'")
        self.coordinator.worker=Mock(side_effect=AssertionError('기존 프로세스가 실행 중입니다.'))
        with patch('loop_manager_internal.application.coordinator.still_running',return_value=True):
            self.coordinator.run()
        self.coordinator.worker.assert_not_called()

    def test_pause_prevents_dispatch_without_touching_active_work(self):
        self.store.pause('*',True)
        self.coordinator.worker=Mock(side_effect=AssertionError)
        self.coordinator.run()
        self.coordinator.worker.assert_not_called()
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'READY')

    def test_runtime_change_drains_worker_and_defers_new_verify(self):
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET draft=1 WHERE plan_id='G-002'")
        returned=threading.Event()
        def worker(*args):
            returned.set()
            return 'CANDIDATE','작업물 보존 완료','candidate'
        self.coordinator.worker=worker
        self.coordinator.verify=Mock(side_effect=AssertionError('인계 대기 중 새 Verify를 시작했습니다.'))
        self.coordinator.runtime_boundary=lambda:'new-runtime' if returned.is_set() else None
        self.coordinator.run()
        self.assertEqual(self.coordinator.handoff_target,'new-runtime')
        self.coordinator.verify.assert_not_called()
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'VERIFYING')

    def test_restart_after_worker_commit_resumes_publication_without_agent(self):
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='WORKING' WHERE plan_id='G-001'")
            db.execute("UPDATE exec_plans SET draft=1 WHERE plan_id='G-002'")
            db.execute('UPDATE exec_plan_runs SET generation=1,input_fingerprint=?,candidate_sha=?,worktree=?,worker_result=? '
                       'WHERE plan_id=?',('worker-input','candidate',str(self.root/'G-001'),
                       '{"result":"CANDIDATE","evidence":"구현 완료","generation":1}','G-001'))
        self.coordinator.repositories['owner/repo']=self.repository
        self.repository.head.return_value='candidate'
        row=self.store.rows('SELECT p.*,r.* FROM exec_plans p JOIN exec_plan_runs r USING(repo,plan_id) WHERE plan_id=?',('G-001',))[0]
        self.assertTrue(self.coordinator.resume_worker_publication('owner/repo',row))
        self.coordinator.agent.call.assert_not_called()
        self.repository.publish.assert_called_once()
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'VERIFYING')
        self.assertIsNone(self.store.rows('SELECT worker_result FROM exec_plan_runs WHERE plan_id=?',('G-001',))[0]['worker_result'])
