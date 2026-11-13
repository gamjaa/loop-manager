import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from loop_manager_internal.catalog.observer import catalog,synchronize
from loop_manager_internal.git.repository import GitError
from loop_manager_internal.planning.proposals import metadata,observe_pulls,proposal,record_pull
from loop_manager_internal.storage.state import Store
from tests.support.rewrite_fixtures import requirement,plan


def pull(number=1,*,draft=True,head='candidate',merged=False,primary='R-001',plan_ids=None):
    return {'number':number,'html_url':f'https://github.com/owner/repo/pull/{number}',
            'node_id':'node','state':'closed' if merged else 'open','merged_at':'time' if merged else None,
            'draft':draft,'mergeable':True,'head':{'ref':'loop-manager/plans/r-001-r1',
            'sha':head,'repo':{'full_name':'owner/repo'}},'base':{'sha':'base'},
            'body':metadata('planning',primary,plan_ids or ['G-001'])}


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        self.store=Store(self.root/'state/loop-manager.db')
        self.store.initialize()
        self.store.register('owner/repo',self.root/'source','remote','main','codex')
        self.snapshot=catalog('base',[requirement()])
        synchronize(self.store,'owner/repo',self.snapshot)
        self.workspace=self.root/'work'
        for doc in (requirement(),plan(draft=True)):
            path=self.workspace/doc.path
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(doc.text,encoding='utf-8')
        self.repository=Mock()
        self.repository.repo='owner/repo'
        self.repository.base='main'
        self.repository.project={'repo':'owner/repo','provider':'codex'}
        self.repository.prepare.return_value=self.workspace
        self.repository.head.return_value='initial'
        self.repository.git.return_value=''
        self.repository.commit.return_value='candidate'
        self.repository.refresh.return_value='base'
        self.repository.snapshot.return_value=self.snapshot
        self.repository.plan_branches.return_value=[]
        self.github=Mock()
        self.github.ensure_pull.return_value=pull()
        self.agent=Mock()
        self.agent.call.return_value=Mock(kind='SUCCESS',decision={'result':'PLANNED','evidence':'전체 미충족 범위 평가'})

    def test_one_draft_pr_contains_questions_and_commits_planning_revision_after_observation(self):
        self.assertTrue(proposal(self.store,self.repository,self.github,self.agent,self.snapshot,'R-001','requirement_changed'))
        self.assertEqual(self.github.ensure_pull.call_count,1)
        args=self.github.ensure_pull.call_args
        self.assertTrue(args.kwargs['draft'])
        self.assertIn('질문',args.args[3])
        self.assertEqual(self.store.rows('SELECT last_planned_source_oid FROM requirement_states')[0]['last_planned_source_oid'],'r1')
        self.assertEqual(self.store.rows('SELECT kind,primary_requirement_id FROM managed_prs')[0],
                         {'kind':'planning','primary_requirement_id':'R-001'})
        self.assertEqual(len(self.store.rows('SELECT * FROM notification_outbox')),1)

    def test_user_closed_same_input_is_not_recreated(self):
        item=pull()
        item['state']='closed'
        record_pull(self.store,'owner/repo',item,{'kind':'planning','primary_requirement_id':'R-001','plan_ids':['G-001']},input_revision='r1')
        self.assertFalse(proposal(self.store,self.repository,self.github,self.agent,self.snapshot,'R-001','requirement_changed'))
        self.agent.call.assert_not_called()

    def test_existing_shared_g_is_linked_without_empty_planning_pr(self):
        self.snapshot=catalog('base',[requirement(),plan()])
        synchronize(self.store,'owner/repo',self.snapshot)
        self.repository.snapshot.return_value=self.snapshot
        (self.workspace/plan().path).write_text(plan().text,encoding='utf-8')
        self.agent.call.return_value.decision={'result':'PLANNED','evidence':'기존 G가 목표를 모두 담당','reuses':['G-001']}
        self.assertTrue(proposal(self.store,self.repository,self.github,self.agent,self.snapshot,'R-001','requirement_changed'))
        self.github.ensure_pull.assert_not_called()
        row=self.store.rows('SELECT * FROM requirement_states')[0]
        self.assertEqual(json.loads(row['evidence'])['reuses'],['G-001'])
        self.assertEqual(row['last_planned_source_oid'],'r1')

    def test_remote_merged_implementation_releases_renamed_and_reused_requirements(self):
        snapshot=catalog('base',[requirement(),requirement('R-002'),plan()])
        synchronize(self.store,'owner/repo',snapshot)
        with self.store.transaction() as db:
            db.execute("UPDATE requirement_states SET judgment='UNRESOLVED'")
            db.execute('UPDATE requirement_states SET source_path=? WHERE requirement_id=?',('renamed','R-001'))
            db.execute('UPDATE requirement_states SET evidence=? WHERE requirement_id=?',('{"reuses":["G-001"]}','R-002'))
        item=pull(merged=True)
        item['head']['ref']='loop-manager/G-001'
        record_pull(self.store,'owner/repo',item,{'kind':'implementation','primary_requirement_id':'R-001','plan_ids':['G-001']})
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'MERGED')
        self.assertTrue(all(row['judgment'] is None for row in self.store.rows('SELECT judgment FROM requirement_states')))

    def test_changed_requirement_does_not_confirm_old_planning_revision(self):
        self.repository.snapshot.return_value=catalog('newbase',[requirement(oid='new')])
        with self.assertRaises(GitError):
            proposal(self.store,self.repository,self.github,self.agent,self.snapshot,'R-001','requirement_changed')
        self.assertIsNone(self.store.rows('SELECT last_planned_source_oid FROM requirement_states')[0]['last_planned_source_oid'])

    def test_latest_user_pr_edit_prevents_close_and_requirement_archive_judgment(self):
        item=pull(head='initial')
        record_pull(self.store,'owner/repo',item,{'kind':'planning','primary_requirement_id':'R-001','plan_ids':['G-001']},input_revision='r1')
        self.agent.call.return_value.decision={'result':'SATISFIED','evidence':'최신 AC 충족 확인'}
        self.github.close.side_effect=GitError('사용자 head 변경')
        with self.assertRaisesRegex(GitError,'사용자'):
            proposal(self.store,self.repository,self.github,self.agent,self.snapshot,'R-001','final_acceptance')
        self.assertNotEqual(self.store.rows('SELECT judgment FROM requirement_states')[0]['judgment'],'SATISFIED')

    def test_close_response_loss_resumes_archive_judgment_without_another_agent(self):
        item=pull(head='initial')
        info={'kind':'planning','primary_requirement_id':'R-001','plan_ids':['G-001']}
        record_pull(self.store,'owner/repo',item,info,input_revision='r1')
        self.agent.call.return_value.decision={'result':'SATISFIED','evidence':'最新 base AC와 테스트 충족'}
        def lost(*args):
            item['state']='closed'
            record_pull(self.store,'owner/repo',item,info,input_revision='r1')
            raise OSError('Close 응답 전에 연결 중단')
        self.github.close.side_effect=lost
        with self.assertRaises(OSError):
            proposal(self.store,self.repository,self.github,self.agent,self.snapshot,'R-001','final_acceptance')
        self.assertEqual(self.store.rows('SELECT judgment FROM requirement_states')[0]['judgment'],'CLOSING')
        self.github.close.side_effect=None
        self.assertTrue(proposal(self.store,self.repository,self.github,self.agent,self.snapshot,'R-001','final_acceptance'))
        self.assertEqual(self.agent.call.call_count,1)
        self.assertEqual(self.store.rows('SELECT judgment FROM requirement_states')[0]['judgment'],'SATISFIED')

    def test_remote_branch_without_pr_reserves_previous_id_and_active_checkpoint_publication(self):
        synchronize(self.store,'owner/repo',catalog('base',[requirement(),plan()]))
        self.repository.plan_branches.return_value=['G-001','G-099']
        self.github.pulls.return_value=[]
        observe_pulls(self.store,self.repository,self.github)
        self.assertTrue(self.store.plan('owner/repo','G-001')['published'])
        self.assertEqual(self.store.rows('SELECT plan_id FROM exec_plan_tombstones'),[{'plan_id':'G-099'}])

    def test_followup_uses_new_branch_and_omits_completed_g_from_proposal(self):
        self.snapshot=catalog('base',[requirement(),plan()])
        synchronize(self.store,'owner/repo',self.snapshot)
        first=pull(merged=True)
        record_pull(self.store,'owner/repo',first,{'kind':'planning','primary_requirement_id':'R-001','plan_ids':['G-001']},input_revision='r1')
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='MERGED'")
        (self.workspace/plan().path).write_text(plan().text,encoding='utf-8')
        added=plan('G-002')
        (self.workspace/added.path).write_text(added.text,encoding='utf-8')
        self.repository.snapshot.return_value=self.snapshot
        self.github.ensure_pull.return_value=pull(number=2,plan_ids=['G-002'])
        self.assertTrue(proposal(self.store,self.repository,self.github,self.agent,self.snapshot,'R-001','final_acceptance'))
        self.assertIn('-after-1',self.repository.prepare.call_args.args[0])
        self.assertEqual(json.loads(self.store.rows('SELECT plan_ids FROM managed_prs WHERE pr_number=2')[0]['plan_ids']),['G-002'])

    def test_planning_conflict_is_a_trigger_once_per_observed_pair(self):
        item=pull()
        info={'kind':'planning','primary_requirement_id':'R-001','plan_ids':['G-001']}
        record_pull(self.store,'owner/repo',item,info)
        item['base']['sha']='advanced-base'
        item['mergeable']=False
        record_pull(self.store,'owner/repo',item,info)
        self.assertEqual(self.store.rows('SELECT judgment FROM requirement_states')[0]['judgment'],'REPLAN')
        with self.store.transaction() as db:
            db.execute("UPDATE requirement_states SET judgment='UNRESOLVED'")
        record_pull(self.store,'owner/repo',item,info)
        self.assertEqual(self.store.rows('SELECT judgment FROM requirement_states')[0]['judgment'],'UNRESOLVED')

    def test_other_base_merge_reserves_history_without_completing_current_g(self):
        synchronize(self.store,'owner/repo',catalog('base',[requirement(),plan()]))
        item=pull(merged=True)
        item['body']=metadata('implementation','R-001',['G-001'])
        item['head']['ref']='loop-manager/G-001'
        item['base']['ref']='another-base'
        self.github.pulls.return_value=[item]
        self.assertEqual(observe_pulls(self.store,self.repository,self.github),[])
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'READY')
        self.assertTrue(self.store.rows('SELECT * FROM managed_prs'))

    def test_conflict_cache_reuses_pair_but_open_closed_state_keeps_observing(self):
        info={'kind':'planning','primary_requirement_id':'R-001','plan_ids':['G-001']}
        item=pull()
        record_pull(self.store,'owner/repo',item,info)
        listing=dict(item)
        listing.pop('mergeable')
        self.github.pulls.return_value=[listing]
        observe_pulls(self.store,self.repository,self.github)
        self.github.pull.assert_not_called()
        listing['state']='closed'
        observe_pulls(self.store,self.repository,self.github)
        self.assertEqual(self.store.rows('SELECT state FROM managed_prs')[0]['state'],'CLOSED')

    def test_planning_pr_head_edit_triggers_same_requirement_reassessment(self):
        info={'kind':'planning','primary_requirement_id':'R-001','plan_ids':['G-001']}
        record_pull(self.store,'owner/repo',pull(),info)
        record_pull(self.store,'owner/repo',pull(head='user-edit'),info)
        self.assertEqual(self.store.rows('SELECT judgment FROM requirement_states')[0]['judgment'],'REPLAN')
