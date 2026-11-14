from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from loop_manager_internal.application.completion import archive,complete,sessions
from loop_manager_internal.git.repository import GitError
from loop_manager_internal.catalog.observer import catalog,synchronize
from loop_manager_internal.runtime.revision_runtime import (Refresh,consume_handoff,execution_input,
                                                           revision_fingerprint,write_handoff)
from loop_manager_internal.storage.state import Store
from tests.support.rewrite_fixtures import requirement,plan


class CompletionTests(unittest.TestCase):
    def test_final_usage_precedes_cleanup_and_keeps_cli_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root/'loop-manager.db')
            store.initialize()
            store.register('owner/repo','/project','remote','main','codex')
            snapshot=catalog('base',[requirement(),plan()])
            synchronize(store,'owner/repo',snapshot)
            request=store.request('owner/repo','G-001','manual_start')
            store.finish_request('owner/repo','G-001',request,'수동 준비 완료',cli_final=True,manual_mode='MANUAL_WORKING')
            store.cancel('owner/repo','G-001','new-base','명시 철회')
            work=root/'work'
            work.mkdir()
            with store.transaction() as db:
                db.execute('UPDATE exec_plan_runs SET worktree=?',(str(work),))
                db.execute('INSERT INTO agent_calls(invocation_id,repo,scope,role,provider,model,instruction_hash,created_at) '
                           'VALUES(?,?,?,?,?,?,?,?)',('call','owner/repo','G-001','worker','codex','model','hash',0))
                db.execute("INSERT INTO managed_prs(repo,pr_number,kind,primary_requirement_id,plan_ids,branch,url,state,draft) "
                           "VALUES(?,1,'implementation',?,'[\"G-001\"]','loop-manager/G-001','url','CLOSED',1)",('owner/repo','R-001'))
            repository=Mock(repo='owner/repo',state_dir=root)
            github=Mock()
            events=[]
            github.update_usage.side_effect=lambda *args:events.append('usage')
            repository.cleanup.side_effect=lambda *args:events.append('cleanup') or True
            self.assertEqual(complete(store,repository,github,snapshot),[])
            self.assertEqual(events,['usage','cleanup'])
            self.assertFalse(store.rows('SELECT * FROM agent_calls'))
            self.assertEqual(store.plan('owner/repo','G-001')['request_result'],'수동 준비 완료')

    def test_archive_reobserves_lost_push_and_rejects_extra_committed_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root/'loop-manager.db')
            store.initialize()
            store.register('owner/repo','/project','remote','main','codex')
            source=plan()
            snapshot=catalog('base',[requirement(),source])
            path=root/'archive-work'
            file=path/source.path
            file.parent.mkdir(parents=True)
            file.write_text(source.text,encoding='utf-8')
            repository=Mock(repo='owner/repo',base='main')
            repository.refresh.return_value='base'
            repository.snapshot.return_value=snapshot
            def prepare(*args,**kwargs):
                kwargs['on_reserved'](path)
                return path
            repository.prepare.side_effect=prepare
            repository.git.side_effect=lambda *args,**kwargs:'user-code.py' if args[0]=='diff' else ''
            with self.assertRaisesRegex(GitError,'사용자 변경'):
                archive(store,repository,'plan','G-001',source)
            repository.publish.assert_not_called()
            self.assertTrue(file.exists())
            repository.git.side_effect=lambda *args,**kwargs:source.oid if args[0]=='hash-object' else ''
            def push(*args,**kwargs):
                archived=plan(path='docs/plans/archive/'+Path(source.path).name)
                repository.snapshot.return_value=catalog('after-push',[requirement(),archived])
                raise GitError('Push 응답 유실')
            repository.publish.side_effect=push
            with self.assertRaisesRegex(GitError,'유실'):
                archive(store,repository,'plan','G-001',source)
            archive(store,repository,'plan','G-001',source)
            self.assertEqual(repository.publish.call_count,1)
            self.assertEqual(repository.commit.call_count,1)
            self.assertFalse(store.rows('SELECT * FROM external_jobs'))

    def test_cleanup_failure_preserves_user_work_and_other_terminal_plan_can_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root/'loop-manager.db')
            store.initialize()
            store.register('owner/repo','/project','remote','main','codex')
            snapshot=catalog('base',[requirement(),plan(),plan('G-002')])
            synchronize(store,'owner/repo',snapshot)
            for identity in ('G-001','G-002'):
                work=root/identity
                work.mkdir()
                (work/'user.txt').write_text('사용자 작업',encoding='utf-8')
                store.cancel('owner/repo',identity,'revision','철회 확인')
                with store.transaction() as db:
                    db.execute('UPDATE exec_plan_runs SET worktree=? WHERE plan_id=?',(str(work),identity))
            repository=Mock(repo='owner/repo',state_dir=root)
            repository.cleanup.side_effect=[False,True]
            github=Mock()
            complete(store,repository,github,snapshot)
            self.assertEqual(store.plan('owner/repo','G-001')['worktree'],str(root/'G-001'))
            self.assertIsNone(store.plan('owner/repo','G-002')['worktree'])
            self.assertEqual((root/'G-001/user.txt').read_text(encoding='utf-8'),'사용자 작업')

    def test_session_cleanup_keeps_auth_and_sessions_shared_with_other_g(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root/'loop-manager.db')
            store.initialize()
            store.register('owner/repo','/project','remote','main','codex')
            home=root/'automation-codex'
            (home/'sessions').mkdir(parents=True)
            private=home/'sessions/only-one.jsonl'
            shared=home/'sessions/shared-session.jsonl'
            private.write_text('비공개 세션',encoding='utf-8')
            shared.write_text('공유 세션',encoding='utf-8')
            auth=home/'auth.json'
            auth.write_text('인증',encoding='utf-8')
            with store.transaction() as db:
                for invocation,scope,session in (('1','G-001','only-one'),('2','G-001','shared-session'),('3','G-002','shared-session')):
                    db.execute('INSERT INTO agent_calls(invocation_id,repo,scope,role,provider,model,session_id,instruction_hash,created_at) '
                               'VALUES(?,?,?,?,?,?,?,?,?)',(invocation,'owner/repo',scope,'worker','codex','model',session,'hash',0))
            sessions(store,root,'owner/repo','G-001')
            self.assertFalse(private.exists())
            self.assertTrue(shared.exists())
            self.assertTrue(auth.exists())
            self.assertEqual(len(store.rows('SELECT * FROM agent_calls')),1)


class RuntimeRevisionTests(unittest.TestCase):
    def test_design_and_sha_only_change_do_not_refresh_execution_inputs(self):
        repository=Mock()
        repository.git.side_effect=[
            '100644 blob code\tloop_manager.py\0'+'100644 blob design1\tdocs/PRODUCT.md\0',
            '100644 blob code\tloop_manager.py\0'+'100644 blob design2\tdocs/PRODUCT.md\0',
            '100644 blob changed\tloop_manager.py\0']
        refresh=Refresh(repository,'first')
        repository.refresh.side_effect=['second','third']
        self.assertIsNone(refresh.boundary())
        self.assertEqual(refresh.boundary(),'third')
        self.assertFalse(execution_input('docs/requirements/[R-001] 목표.md'))
        self.assertTrue(execution_input('docs/AGENTS.worker.md'))

    def test_handoff_receipt_is_bound_to_parent_and_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'handoff.json'
            write_handoff(path,'token','a'*40,'b'*40)
            with self.assertRaises(RuntimeError):
                consume_handoff(path,'other','a'*40)
            self.assertTrue(path.exists())
            self.assertEqual(consume_handoff(path,'token','a'*40),'b'*40)
            self.assertFalse(path.exists())
