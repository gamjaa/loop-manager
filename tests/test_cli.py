"""공개 진입부의 직접 DB 접근과 원격 runtime 인계를 분리해 검증한다."""
from contextlib import redirect_stdout,redirect_stderr
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch

from loop_manager_internal.application.entrypoint import main
from loop_manager_internal.runtime import launcher,revision_runtime
from loop_manager_internal.runtime.process_lock import process_lock,_held_process_lock_identity
from loop_manager_internal.storage.state import Store


class EntryTests(unittest.TestCase):
    def test_init_and_queries_need_no_config_runtime_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for command in ('init','list','status','logs','usage','pause','resume'):
                with self.subTest(command=command),redirect_stdout(io.StringIO()), \
                     patch.object(launcher,'launch',side_effect=AssertionError('runtime 실행 금지')):
                    self.assertEqual(main(['--state-dir',str(root/'state'),command],root=root),0)
            self.assertFalse((root/'config').exists())

    def test_missing_database_is_an_error_without_implicit_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with redirect_stderr(io.StringIO()) as output:
                self.assertEqual(main(['--state-dir',str(root),'list'],root=root),1)
            self.assertIn('init',output.getvalue())
            self.assertFalse((root/'loop-manager.db').exists())


class LauncherTests(unittest.TestCase):
    def test_parent_uses_exact_remote_revision_and_consumes_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root/'loop-manager.db')
            store.initialize()
            repository=Mock(remote='git@github.com:owner/repo.git',base='develop')
            repository.refresh.return_value='a'*40
            calls=[]
            def child(argv,**kwargs):
                calls.append(argv)
                token=argv[argv.index('--runtime-token')+1]
                self.assertEqual(_held_process_lock_identity(root,name='bootstrap.lock'),token)
                if len(calls)==1:
                    revision_runtime.write_handoff(kwargs['env']['LOOP_MANAGER_HANDOFF_PATH'],token,'a'*40,'b'*40)
                    return SimpleNamespace(returncode=75)
                return SimpleNamespace(returncode=0)
            args=SimpleNamespace(command='run',skip_if_running=False,repositories=[])
            with patch.object(launcher,'own_project',return_value={}), \
                 patch.object(launcher,'Repository',return_value=repository), \
                 patch.object(revision_runtime,'prepare',side_effect=lambda repo,state,revision:root/'runtimes'/revision), \
                 patch.object(launcher.subprocess,'run',side_effect=child):
                self.assertEqual(launcher.launch(args,root,store,root,root/'config.ini'),0)
            self.assertEqual([argv[argv.index('--runtime-sha')+1] for argv in calls],['a'*40,'b'*40])
            self.assertFalse(list(root.glob('handoff-*.json')))

    def test_child_holds_runtime_lock_while_cli_can_write_and_handoff_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)
            root=state/'runtimes'/('a'*40)
            root.mkdir(parents=True)
            store=Store(state/'loop-manager.db')
            store.initialize()
            token='test-parent'
            receipt=state/f'handoff-{token}.json'
            args=SimpleNamespace(command='run',runtime_origin='git@github.com:owner/repo.git',runtime_base='develop',
                                 runtime_sha='a'*40,runtime_token=token,runtime_lock='bootstrap.lock',
                                 repositories=[],skip_if_running=False)
            repository=Mock()
            repository.head.return_value='a'*40
            repository.git.return_value=args.runtime_origin
            coordinator=Mock(handoff_target='b'*40)
            def execute(selected):
                self.assertEqual(_held_process_lock_identity(state),'runtime:'+token)
                Store(store.path).pause('*',True)
                return []
            coordinator.run.side_effect=execute
            with process_lock(state,identity=token,name='bootstrap.lock'), \
                 patch.object(launcher,'Repository',return_value=repository), \
                 patch.object(launcher,'Settings',return_value=Mock()), \
                 patch.object(launcher,'Coordinator',return_value=coordinator), \
                 patch.object(revision_runtime,'Refresh'), \
                 patch.dict('os.environ',{'LOOP_MANAGER_HANDOFF_PATH':str(receipt)}):
                self.assertEqual(launcher.child(args,root,store,state,state/'config.ini'),75)
            self.assertEqual(revision_runtime.consume_handoff(receipt,token,'a'*40),'b'*40)
