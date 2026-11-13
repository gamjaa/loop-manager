"""Provider mock과 실제 최소 프로세스 종료를 구분한다."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from loop_manager_internal.agents.runner import Agent,arguments,decode_events,decision,instructions
from loop_manager_internal.storage.state import Store
from loop_manager_internal.execution.processes import run
from loop_manager_internal.execution.validation import verify_command,verify


class AgentContractTests(unittest.TestCase):
    def test_guidance_contains_ponytail_exactly_once_only_for_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'docs').mkdir()
            for role in ('common','planner','worker','reviewer','ponytail'):
                (root/f'docs/AGENTS.{role}.md').write_text(f'지침:{role}',encoding='utf-8')
            worker,digest = instructions(root,'worker')
            self.assertEqual(worker.count('지침:ponytail'),1)
            self.assertEqual(len(digest),64)
            self.assertNotIn('지침:ponytail',instructions(root,'reviewer')[0])

    def test_provider_role_permissions_and_session_arguments(self):
        codex = arguments('codex','reviewer','model','high','원본 지침',session_id='session')
        self.assertIn('read-only',codex)
        self.assertIn('resume',codex)
        claude = arguments('claude','worker','model','high','원본 지침')
        self.assertIn('--append-system-prompt',claude)
        self.assertNotIn('--dangerously-skip-permissions',claude)
        reviewer = arguments('claude','reviewer','model','high','지침')
        self.assertEqual(reviewer[reviewer.index('--tools')+1],'Read,Glob,Grep')

    def test_usage_keeps_provider_cache_meaning_and_missing_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'events.jsonl'
            path.write_text('\n'.join(json.dumps(item) for item in (
                {'type':'thread.started','thread_id':'session'},
                {'type':'item.completed','item':{'type':'agent_message','text':'결과'}},
                {'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':60,'output_tokens':10}})),encoding='utf-8')
            message,session,usage,error = decode_events('codex',path)
            self.assertEqual((message,session,error),('결과','session',False))
            self.assertEqual((usage.input_tokens,usage.cached_input_tokens),(100,60))
            path.write_text(json.dumps({'type':'result','result':'결과','usage':{'input_tokens':40,
                'cache_read_input_tokens':60,'cache_creation_input_tokens':3}}),encoding='utf-8')
            usage = decode_events('claude',path)[2]
            self.assertEqual((usage.input_tokens,usage.cached_input_tokens,usage.cache_write_input_tokens),(40,60,3))
            self.assertIsNone(usage.output_tokens)

    def test_structured_decision_requires_role_and_evidence(self):
        self.assertEqual(decision('{"result":"PASS","evidence":"현재 후보 검토"}','reviewer')['result'],'PASS')
        for message in ('PASS','{"result":"CANDIDATE","evidence":"구현"}','{"result":"PASS","evidence":""}'):
            with self.assertRaises(ValueError):
                decision(message,'reviewer')

    def test_agent_invocation_filters_manager_secrets_and_records_unknown_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'docs').mkdir()
            for role in ('common','reviewer'):
                (root/f'docs/AGENTS.{role}.md').write_text('지침',encoding='utf-8')
            store=Store(root/'state/loop-manager.db')
            store.initialize()
            store.register('owner/repo','/project','remote','main','codex')
            settings=SimpleNamespace(profile=lambda *args:('model','high'),fast=lambda *args:False,agent_timeout=30)
            def execute(argv,cwd,timeout,**kwargs):
                environment=kwargs['environment']
                self.assertNotIn('TELEGRAM_TOKEN',environment)
                self.assertNotIn('LOOP_MANAGER_SECRET',environment)
                self.assertEqual(environment['OPENAI_API_KEY'],'provider-key')
                event={'type':'item.completed','item':{'type':'agent_message',
                       'text':json.dumps({'result':'PASS','evidence':'코드·검증 근거 확인'})}}
                Path(kwargs['output_path']).write_text(json.dumps(event),encoding='utf-8')
                return SimpleNamespace(code=0,timed_out=False,output='')
            with patch.dict(os.environ,{'TELEGRAM_TOKEN':'secret','LOOP_MANAGER_SECRET':'secret',
                             'OPENAI_API_KEY':'provider-key','CODEX_HOME':str(root/'absent-user-home')}), \
                 patch('loop_manager_internal.agents.provider_home._owned_directory'), \
                 patch('loop_manager_internal.agents.runner.run',side_effect=execute):
                result=Agent(store,root/'state',root,settings).call({'repo':'owner/repo','provider':'codex'},
                    'G-001','reviewer',root,'현재 후보를 검토하세요.')
            self.assertEqual(result.kind,'SUCCESS')
            call=store.rows('SELECT * FROM agent_calls')[0]
            self.assertIsNone(call['input_tokens'])
            self.assertEqual(call['result'],'SUCCESS')

    def test_project_command_has_no_language_runner_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'scripts').mkdir()
            (root/'input.txt').write_text('설정',encoding='utf-8')
            (root/'scripts/verify.json').write_text(json.dumps({'version':2,
                'commands':{'windows':['tool','--check'],'posix':['other-tool','test']},
                'inputs':['input.txt'],'environment':{'PROJECT_MODE':'test'}}),encoding='utf-8')
            specification = verify_command(root,30)
            self.assertTrue(specification.reusable)
            self.assertEqual(specification.environment['PROJECT_MODE'],'test')
            old = specification.fingerprint
            (root/'input.txt').write_text('변경',encoding='utf-8')
            self.assertNotEqual(old,verify_command(root,30).fingerprint)
            with patch('loop_manager_internal.execution.validation.run') as execute:
                execute.return_value.code=0
                execute.return_value.timed_out=False
                execute.return_value.output='검증 완료'
                self.assertTrue(verify(specification)[0])
                self.assertEqual(execute.call_args.args[0],specification.argv)


class NativeProcessTests(unittest.TestCase):
    def test_auth_copy_fallback_keeps_existing_auth_and_user_source(self):
        from loop_manager_internal.agents.runner import reuse_auth
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'user-auth.json'
            auth=Path(directory)/'automation-auth.json'
            source.write_text('사용자 인증',encoding='utf-8')
            with patch.object(Path,'symlink_to',side_effect=PermissionError('링크 권한 없음')):
                reuse_auth(source,auth)
            self.assertEqual(auth.read_text(encoding='utf-8'),'사용자 인증')
            auth.write_text('별도 갱신',encoding='utf-8')
            reuse_auth(source,auth)
            self.assertEqual(source.read_text(encoding='utf-8'),'사용자 인증')
            self.assertEqual(auth.read_text(encoding='utf-8'),'별도 갱신')

    def test_private_provider_directory_checks_real_native_permissions(self):
        from loop_manager_internal.agents.provider_home import _owned_directory
        with tempfile.TemporaryDirectory() as directory:
            private=Path(directory)/'provider'
            _owned_directory(private)
            if os.name!='nt':
                private.chmod(0o755)
                with self.assertRaises(RuntimeError):
                    _owned_directory(private)

    def test_stdin_unicode_arguments_and_child_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            normal = run([sys.executable,'-c','import sys; print(sys.stdin.read()+sys.argv[1])',' 인자 공백'],
                         directory,10,input_text='입력')
            self.assertEqual(normal.code,0)
            self.assertFalse(normal.timed_out)
            self.assertIn('입력 인자 공백',normal.output)
            timeout = run([sys.executable,'-c',
                           'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); time.sleep(30)'],
                          directory,0.5)
            self.assertTrue(timeout.timed_out)
            self.assertNotEqual(timeout.code,0)
