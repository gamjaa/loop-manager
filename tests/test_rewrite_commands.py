"""CLI는 실행 잠금·Agent·원격 게시 없이 DB에 직접 접근한다."""
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loop_manager_internal.application.direct_commands import build_parser, execute, manual_start, query
from loop_manager_internal.storage.state import Store, StateError
from tests.support.rewrite_fixtures import requirement, plan
from loop_manager_internal.catalog.observer import catalog,synchronize


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root/'loop-manager.db')
        self.store.initialize()
        self.store.register('owner/repo',self.root/'project','remote','main','codex')
        synchronize(self.store,'owner/repo',catalog('base',[requirement(),plan()]))

    def test_read_commands_do_not_mutate_or_call_external_tools(self):
        with self.store.connect() as db:
            before = '\n'.join(db.iterdump())
        with patch('loop_manager_internal.application.direct_commands.Repository',side_effect=AssertionError), \
                patch('loop_manager_internal.application.direct_commands.command',side_effect=AssertionError):
            for kind in ('list','status','usage','logs'):
                self.assertIsNotNone(query(self.store,kind,'owner/repo'))
        with self.store.connect() as db:
            self.assertEqual(before,'\n'.join(db.iterdump()))

    def test_no_validation_bypass_or_retired_commands(self):
        parser = build_parser()
        with patch('sys.stderr'):
            for argv in (['manual','submit','owner/repo'],['maintenance-review','owner/repo'],
                         ['recover','owner/repo'],['manual','complete','owner/repo','G-001','--skip-verify']):
                with self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_manual_git_io_is_outside_db_transaction_and_after_reservation(self):
        with patch('loop_manager_internal.application.direct_commands.Repository') as factory:
            repository = factory.return_value
            path = self.root/'isolated'
            def prepare(branch,**kwargs):
                self.assertEqual(self.store.plan('owner/repo','G-001')['manual_mode'],'PREPARING')
                Store(self.store.path).pause('owner/repo',False)
                kwargs['on_reserved'](path)
                self.assertEqual(self.store.plan('owner/repo','G-001')['worktree'],str(path))
                return path
            repository.prepare.side_effect = prepare
            result = manual_start(self.store,self.root,{'repo':'owner/repo','plan_id':'G-001'})
            self.assertEqual(result['result'],'수동 준비 완료')
            self.assertEqual(self.store.plan('owner/repo','G-001')['manual_mode'],'MANUAL_WORKING')
            self.assertFalse(self.store.rows('SELECT * FROM notification_outbox'))

    def test_failed_manual_preparation_retains_request_and_path_for_restart(self):
        with patch('loop_manager_internal.application.direct_commands.Repository') as factory:
            def prepare(branch,**kwargs):
                kwargs['on_reserved'](self.root/'preserved')
                raise RuntimeError('Git 준비 실패')
            factory.return_value.prepare.side_effect = prepare
            with self.assertRaisesRegex(RuntimeError,'Git 준비 실패'):
                manual_start(self.store,self.root,{'repo':'owner/repo','plan_id':'G-001'})
        row = self.store.plan('owner/repo','G-001')
        self.assertEqual(row['manual_mode'],'PREPARING')
        self.assertEqual(row['worktree'],str(self.root/'preserved'))
        self.assertEqual(row['request_kind'],'manual_start')
        self.assertIsNone(self.store.claim('owner/repo','G-001','READY','WORKING','input'))

    def test_pause_precedes_manual_git_and_resume_does_not_unblock(self):
        self.store.pause('*',True)
        with patch('loop_manager_internal.application.direct_commands.Repository') as factory:
            with self.assertRaises(StateError):
                manual_start(self.store,self.root,{'repo':'owner/repo','plan_id':'G-001'})
            factory.assert_not_called()
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='BLOCKED'")
        execute(build_parser().parse_args(['resume']),self.store,self.root)
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'BLOCKED')

    def test_unknown_usage_is_not_zero(self):
        with self.store.transaction() as db:
            db.execute('INSERT INTO agent_calls(invocation_id,repo,scope,role,provider,model,instruction_hash,created_at) '
                       'VALUES(?,?,?,?,?,?,?,?)',('call','owner/repo','G-001','worker','codex','model','hash',1))
        self.assertIsNone(query(self.store,'usage')[0]['input_tokens'])
