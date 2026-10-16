"""실제 SQLite 상태와 제어된 호출로 R-091의 배정 경계를 확인한다."""
from contextlib import closing
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import loop_manager
from loop_manager_internal.application import task_scheduling as scheduling
from loop_manager_internal.config.managed_config import ManagedConfig, ManagedConfigError, load_managed_config, fast_mode_scope
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.storage import database, runtime_state
from loop_manager_internal.verification.execution_clock import ExecutionClock, BUDGET_DIRECTORY
from loop_manager_internal.verification.coordination import VerifyCoordinator
from loop_manager_internal.verification.resources import Snapshot, MIB
from tests.support.database import AutomationCatalogFixture


class TaskSchedulingTests(AutomationCatalogFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.state_directory = Path(temporary.name)
        cls.seed_database = sqlite3.connect(':memory:')
        cls.addClassCleanup(cls.seed_database.close)
        with closing(database.connect_database(cls.state_directory, create=True)) as connection:
            connection.backup(cls.seed_database)
        cls.database_inode = (cls.state_directory / 'loop-manager.db').stat().st_ino

    def setUp(self):
        self.directory = self.state_directory
        self.config_path = self.directory / 'config.ini'
        self.config_path.write_text('[loop]\nmax_tasks_per_project=3\n', encoding='utf-8')
        self.config = load_managed_config(self.config_path)
        self.connection = database.connect_database(self.directory, run_migrations=False)
        self.addCleanup(self.connection.close)
        self.seed_database.backup(self.connection)
        self.assertEqual((self.directory / 'loop-manager.db').stat().st_ino, self.database_inode)
        for repo in ('A', 'B'):
            self.connection.execute("INSERT INTO projects(repo,base_branch) VALUES (?,'main')", (repo,))
        for plan_id, repo in (('G-001','A'), ('G-002','A'), ('G-003','B')):
            self._insert_plan(self.connection, plan_id, 'READY', repo=repo)

    def scheduler(self, **values):
        return scheduling.TaskScheduler(replace(self.config, **values), database.connect_database,
                                         self.directory, self.connection)

    def test_configuration_defaults_validation_and_project_override_inheritance(self):
        self.assertEqual((self.config.max_tasks_per_project, self.config.verify_concurrency), (3,1))
        self.assertEqual(project_registry.task_limit(self.connection,'A',3), (0,3,'config'))
        project_registry.set_task_limit(self.connection,'A',5,3)
        project_registry.ensure_registry_schema(self.connection)
        self.assertEqual(project_registry.task_limit(self.connection,'A',4), (5,5,'project'))
        project_registry.set_task_limit(self.connection,'A',0,4)
        self.assertEqual(project_registry.task_limit(self.connection,'A',4), (0,4,'config'))
        for value in (-1, '3', True, 1.5):
            with self.subTest(value=value), self.assertRaises(project_registry.ProjectRegistryError):
                project_registry.set_task_limit(self.connection,'A',value,4)
        for option in ('max_tasks_per_project','verify_concurrency'):
            for value in ('0','-1','1.5','１','true'):
                self.config_path.write_text(f'[loop]\n{option}={value}\n')
                with self.subTest(option=option,value=value), self.assertRaises(ManagedConfigError):
                    load_managed_config(self.config_path)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE projects SET max_tasks_per_project=-1 WHERE repo='A'")
        self.connection.rollback()

    def test_new_project_turns_precede_additional_priority_and_survive_restart(self):
        self.connection.execute("INSERT INTO projects(repo,base_branch) VALUES ('C','main')")
        self.connection.commit()
        for repo,plan_id,priority in (('A','G-004','critical'),('A','G-005','high'),('C','G-006','low')):
            self._insert_plan(self.connection,plan_id,'READY',repo=repo,priority=priority)
        self.connection.execute("UPDATE exec_plans SET priority='low' WHERE plan_id='G-003'")
        scheduler=self.scheduler()
        self.connection.execute("INSERT INTO task_scheduling_cursor VALUES ('agent','A')")
        self.connection.commit()
        scheduler=self.scheduler()  # 재시작에서도 마지막 배정 프로젝트를 유지한다.
        for repo,plan_id in (('A','G-004'),('A','G-005'),('B','G-003'),('C','G-006')):
            task=scheduling.Task(repo,plan_id,self.connection,resumed=False)
            scheduler.waiters['agent'].append((task,task.rank()))
        chosen=[]
        for _ in range(4):
            entry=scheduler._next('agent')
            scheduler.waiters['agent'].remove(entry)
            scheduler.running['agent'][entry[0].repo]+=1
            scheduler.cursor['agent']=entry[0].repo
            chosen.append(entry[0].plan_id)
        self.assertEqual(chosen,['G-003','G-006','G-004','G-005'])

    def test_uncertain_verify_reservation_survives_restart_without_stopping_agent_work(self):
        from loop_manager_internal.storage import task_calls
        from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
        scheduler=self.scheduler()
        with self.assertRaises(VerifyTerminationUncertain):
            with scheduler.slot('verify',scheduling.Task('A','G-001',self.connection)):
                receipt=task_calls.prepare_verify_receipt()
                raise VerifyTerminationUncertain('fake child ownership unavailable')
        restarted=self.scheduler()
        self.assertEqual(sum(restarted.running['verify'].values()),1)
        self.assertEqual(scheduling.managed_count(self.connection,'A'),1)
        with restarted.slot('agent',scheduling.Task('B','G-003',self.connection)):
            self.assertEqual(sum(restarted.running['agent'].values()),1)
        with self.assertRaises(task_calls.CallDeferred):
            with restarted.slot('verify',scheduling.Task('B','G-003',self.connection)):
                self.fail('종료 불명 verify의 자리를 다시 배정했습니다.')
        task_calls.verify_stopped(receipt)
        confirmed=self.scheduler()
        self.assertEqual(sum(confirmed.running['verify'].values()),0)
        self.assertEqual(scheduling.managed_count(self.connection,'A'),0)

    def test_json_lookup_errors_keep_common_envelope_without_creating_database(self):
        target=self.directory/'missing'
        args=['loop_manager.py','--state-dir',str(target),'--config',str(self.config_path),
              'status',str(self.directory/'repo'),'--json']
        output=io.StringIO()
        with mock.patch('sys.argv',args), mock.patch('sys.stdout',output), \
             mock.patch.object(loop_manager,'prepare_bootstrap_runtime',side_effect=AssertionError('bootstrap')):
            self.assertEqual(loop_manager.main(),1)
        result=json.loads(output.getvalue())
        self.assertEqual((result['schema_version'],result['command'],result['error']['code']),
                         (1,'status','db_not_found'))
        self.assertIsNone(result['data'])
        self.assertFalse(target.exists())

    def test_unconfirmed_normal_return_and_git_operation_keep_owned_slots(self):
        from loop_manager_internal.storage import task_calls
        scheduler=self.scheduler()
        with self.assertRaises(task_calls.CallDeferred):
            with scheduler.slot('agent',scheduling.Task('A','G-001',self.connection)):
                task_calls.bind_agent_receipt(self.directory/'missing-process-receipt.json')
        self.assertEqual(sum(scheduler.running['agent'].values()),1)
        self.assertEqual(scheduling.managed_count(self.connection,'A'),1)
        self.connection.execute("INSERT INTO execution_checkpoints(repo,plan_id,operation) VALUES ('A','G-002','push')")
        self.connection.commit()
        self.assertEqual(scheduling.managed_count(self.connection,'A'),2)
        project_registry.set_task_limit(self.connection,'A',1,3)
        runtime=runtime_state.RuntimeStateRepository(self.connection)
        self._insert_plan(self.connection,'G-004','PLAN_BLOCKED',repo='A')
        with self.assertRaises(runtime_state.RuntimeStateError):
            runtime.update_plan('A','G-004',plan_values={'status':'VERIFYING'},enqueue=False)
        self.assertEqual(runtime.get('A','G-004').status,'PLAN_BLOCKED')

    def test_failed_finish_observation_keeps_capacity_and_unblocks_other_kind(self):
        from loop_manager_internal.storage import task_calls
        scheduler=self.scheduler(max_workers=1)
        with mock.patch.object(task_calls,'confirmed',side_effect=sqlite3.OperationalError('busy')):
            with self.assertRaises(sqlite3.OperationalError):
                with scheduler.slot('agent',scheduling.Task('A','G-001',self.connection)):
                    pass
        self.assertEqual(scheduler.uncertain['agent'],1)
        with self.assertRaises(task_calls.CallDeferred):
            with scheduler.slot('agent',scheduling.Task('B','G-003',self.connection)):
                self.fail('종료 확인 실패 자리를 재배정했습니다.')
        with scheduler.slot('verify',scheduling.Task('B','G-003',self.connection)):
            pass

    def test_blocked_work_releases_management_without_discarding_call_or_git_ownership(self):
        from loop_manager_internal.storage import task_calls
        scheduler = self.scheduler(max_workers=1)
        with self.assertRaises(task_calls.CallDeferred):
            with scheduler.slot('agent', scheduling.Task('A', 'G-001', self.connection)):
                task_calls.bind_agent_receipt(self.directory / 'missing-process-receipt.json')
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo='A'")
        self.connection.execute("INSERT INTO execution_checkpoints(repo,plan_id,operation) VALUES ('A','G-002','rebase')")
        self.connection.commit()
        self.assertEqual(scheduling.managed_count(self.connection, 'A'), 0)
        self.assertEqual(scheduling.managed_count(self.connection, 'A', 'G-002'), 0)
        self.assertEqual(sum(scheduler.running['agent'].values()), 1)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 1)
        self.assertEqual(self.connection.execute('SELECT operation FROM execution_checkpoints').fetchone()[0], 'rebase')
        self._insert_plan(self.connection, 'G-004', 'READY', repo='A')
        project_registry.set_task_limit(self.connection, 'A', 1, 3)
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            runtime.activate('A', 'G-004', activation_sha='base', branch='four', worktree='four')
            with self.assertRaises(runtime_state.TaskLimitReached):
                runtime.update_plan('A', 'G-002', plan_values={'status': 'VERIFYING'}, enqueue=False)
        self.assertEqual(runtime.get('A', 'G-002').status, 'PLAN_BLOCKED')

    def test_empty_management_does_not_clear_projection_with_pending_blocked_ownership(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        runtime.block_plan('A', 'G-001', reason_code='managed_worktree_conflict', evidence='pending rebase',
                           release_fingerprint='blocked', project_values={'phase': 'PLAN_BLOCKED'})
        self.connection.execute("INSERT INTO execution_checkpoints(repo,plan_id,operation) VALUES ('A','G-001','rebase')")
        self.connection.commit()
        before = tuple(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone())
        scheduler = self.scheduler()
        effects = SimpleNamespace(activate_ready_execplan=mock.Mock(return_value=None), resume_manual_work=None)
        service = ProjectService(effects, ProjectIdentity(self.connection, self.directory, 'A', self.config_path,
                                 None, False, None, True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base')
        token = scheduling.current_scheduler.set(scheduler)
        try:
            self.assertEqual(scheduling.run_project_tasks(service, newly_activated=False), 0)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(scheduling.managed_count(self.connection, 'A'), 0)
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone()), before)
        self.assertEqual(runtime.get('A', 'G-001').worktree, 'one')

    def test_limit_reduction_during_activation_preserves_ready_without_failure(self):
        with mock.patch.object(runtime_state.RuntimeStateRepository,'activate',
                               side_effect=runtime_state.TaskLimitReached('limit reduced')):
            self.assertIsNone(loop_manager.activate_ready_execplan(self.connection,'A',self.directory,'base'))
        self.assertEqual(runtime_state.RuntimeStateRepository(self.connection).get('A','G-001').status,'READY')

    def test_shared_manual_group_counts_once_and_stable_waits_release_management(self):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        from loop_manager_internal.storage import task_calls
        ManualWorkRepository(self.connection).create('A','submit','manual',[],'head','base',
                                                   members=('G-001','G-002'))
        self.connection.execute("UPDATE exec_plans SET status=CASE plan_id WHEN 'G-002' THEN 'VERIFYING' ELSE 'PR_OPEN' END")
        owner=task_calls.reserve(self.connection,self.directory,'A','G-001','verify','run')
        self.assertEqual(scheduling.managed_count(self.connection,'A'),1)
        self.assertEqual(scheduling.managed_count(self.connection,'B'),0)
        task_calls.release(owner)
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE plan_id='G-002'")
        self.assertEqual(scheduling.managed_count(self.connection,'A'),0)

    def test_public_verify_starts_its_timeout_after_call_queue_wait(self):
        from loop_manager_internal.execution.verify_process import VerifyProcessOutput
        scheduler=self.scheduler(verify_concurrency=1)
        now=[0.0]
        worktree=self.directory/'candidate'
        (worktree/'scripts').mkdir(parents=True)
        for name in ('verify.sh','verify.ps1'):
            (worktree/'scripts'/name).write_text('exit 0\n')
        observed=[]
        def physical(*_args,**_kwargs):
            now[0]+=2
            return True,False,VerifyProcessOutput('PASS',exit_code=0)
        def queued():
            token=scheduling.current_scheduler.set(scheduler)
            try:
                with closing(database.connect_database(self.directory,run_migrations=False)) as connection, \
                     scheduler.task('B','G-003',connection):
                    observed.append(loop_manager.verify(worktree,10))
            finally:
                scheduling.current_scheduler.reset(token)
        with mock.patch.object(time,'monotonic',side_effect=lambda:now[0]), \
             mock.patch.object(loop_manager,'run_verify_process',side_effect=physical):
            with scheduler.slot('verify',scheduling.Task('A','G-001',self.connection)):
                thread=threading.Thread(target=queued)
                thread.start()
                with scheduler.condition:
                    self.assertTrue(scheduler.condition.wait_for(lambda:len(scheduler.waiters['verify'])==1,3))
                now[0]=30
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(observed),1)
        self.assertTrue(observed[0][0])
        self.assertFalse(observed[0][1].timed_out)

    def test_parallel_snapshot_updates_do_not_replace_other_sessions_or_projection(self):
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for plan_id in ('G-001','G-002'):
                runtime.activate('A', plan_id, activation_sha='base', branch=plan_id, worktree=plan_id)
        projected = dict(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone())
        before_other = runtime.get('A','G-002')
        with runtime_state.selected_execution('A','G-001'):
            own = runtime.projected('A')
            runtime.update_project('A', {'worker_session_id':'session-1','phase':'VERIFYING'})
            self.assertEqual(runtime.projected('A').plan_id,'G-001')
            self.assertEqual(runtime_state.project_row(self.connection,'A')['worker_session_id'],'session-1')
            self.assertEqual(runtime.scope_owner('A')['plan_path'], own.path)
            self.assertEqual(runtime.scope_owner('A')['worker_session_id'],'session-1')
            with self.assertRaises(runtime_state.RuntimeStateError):
                runtime.transition_from_snapshot(before_other, plan_values={'status':'REVIEWING'},
                                                 project_values={'phase':'REVIEWING'})
        self.assertEqual(runtime.get('A','G-002'),before_other)
        self.assertEqual(dict(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone()),projected)
        with runtime_state.selected_execution('A','G-002'):
            runtime.update_project('A', {'worker_session_id':'session-2','phase':'REVIEWING'})
        self.assertEqual(runtime.get('A','G-001').worker_session_id,'session-1')
        self.assertEqual(scheduling.managed_count(self.connection,'A'),2)
        project_registry.set_task_limit(self.connection,'A',1,3)
        with runtime_state.selected_execution('A','G-001'):
            runtime.update_project('A', {'phase':'REVIEWING'})
        self._insert_plan(self.connection,'G-004','READY',repo='A')
        with fast_mode_scope(self.config), self.assertRaises(runtime_state.RuntimeStateError):
            runtime.activate('A','G-004',activation_sha='base',branch='four',worktree='four')

    def test_verify_has_its_own_capacity_while_agent_call_is_running(self):
        scheduler = self.scheduler(max_workers=1, verify_concurrency=1)
        entered_agent, entered_verify, release = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def run(kind, repo, plan_id, entered):
            try:
                with closing(database.connect_database(self.directory,run_migrations=False)) as connection:
                    task=scheduling.Task(repo,plan_id,connection,resumed=False)
                    with scheduler.slot(kind,task):
                        entered.set()
                        if not release.wait(3):
                            raise AssertionError('다른 종류의 호출이 배정되지 않았습니다.')
                    self.assertTrue(task.resumed)
            except BaseException as error:
                errors.append(error)
        threads=[threading.Thread(target=run,args=('agent','A','G-001',entered_agent)),
                 threading.Thread(target=run,args=('verify','B','G-003',entered_verify))]
        try:
            for thread in threads: thread.start()
            self.assertTrue(entered_agent.wait(3))
            self.assertTrue(entered_verify.wait(3))
            self.assertEqual(sum(scheduler.running['agent'].values()),1)
            self.assertEqual(sum(scheduler.running['verify'].values()),1)
        finally:
            release.set()
            for thread in threads: thread.join(4)
        self.assertEqual(errors,[])

    def test_completed_task_keeps_independent_planning_error_and_other_execution(self):
        runtime=runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for plan_id in ('G-001','G-002'):
                runtime.activate('A',plan_id,activation_sha='base',branch=plan_id,worktree=plan_id)
        self.connection.execute("UPDATE projects SET planning_error='independent',planner_session_id='planner' WHERE repo='A'")
        self.connection.commit()
        other=runtime.get('A','G-002')
        with runtime_state.selected_execution('A','G-001'):
            runtime.transition_from_snapshot(runtime.projected('A'),plan_values={'status':'PR_OPEN'},
                                             project_values=runtime_state.clear_project_values())
        self.assertEqual(runtime.get('A','G-002'),other)
        self.assertEqual(self.connection.execute("SELECT planning_error,planner_session_id FROM projects WHERE repo='A'").fetchone()[:],
                         ('independent','planner'))

    def test_config_set_and_status_skip_bootstrap_and_preserve_database_on_read(self):
        repo=str((self.directory/'한글 경로').resolve())
        self.connection.execute('UPDATE projects SET repo=? WHERE repo=?',(repo,'A'))
        self.connection.commit()
        base=['loop_manager.py','--state-dir',str(self.directory),'--config',str(self.config_path)]
        with mock.patch.object(loop_manager,'prepare_bootstrap_runtime',side_effect=AssertionError('bootstrap')):
            with mock.patch('sys.argv',base+['config','set',repo,'--max-tasks-per-project','4']):
                self.assertEqual(loop_manager.main(),0)
            before=(self.directory/'loop-manager.db').read_bytes()
            output=io.StringIO()
            with mock.patch('sys.argv',base+['status',repo,'--json']), mock.patch('sys.stdout',output):
                self.assertEqual(loop_manager.main(),0)
            data=json.loads(output.getvalue())
            self.assertEqual((data['data']['max_tasks_per_project'],data['data']['effective_max_tasks_per_project'],data['data']['source']),
                             (4,4,'project'))
            self.assertEqual((self.directory/'loop-manager.db').read_bytes(),before)

    def test_manual_intake_order_precedes_existing_and_critical_automatic_calls(self):
        self._insert_plan(self.connection,'G-004','READY',repo='A')
        for repo,plan_id,accepted in (('B','G-003','2026-10-16T01:00:00'),
                                      ('A','G-004','2026-10-16T02:00:00'),
                                      ('A','G-001','2026-10-16T00:00:00')):
            group='manual-'+plan_id
            self.connection.execute("INSERT INTO manual_work_groups(repo,group_id,kind,source_branch,requirements,"
                "source_head,base_sha,state,members,documents,owner_id,request_id) "
                "VALUES (?,?,'existing',?,'[]','head','base','AUTOMATIC',?,'[]',?,?)",
                (repo,group,plan_id,json.dumps([plan_id]),plan_id,group))
            self.connection.execute('INSERT INTO manual_work_members VALUES (?,?,?)',(repo,plan_id,group))
            self.connection.execute("INSERT INTO manual_work_requests(request_id,repo,group_id,source_head,base_sha,"
                "input_token,skip_verify,skip_review,reason,created_at) VALUES (?,?,?,'head','base','input',0,0,'',?)",
                (group,repo,group,accepted))
        self.connection.execute("UPDATE exec_plans SET priority='critical' WHERE plan_id='G-002'")
        self.connection.commit()
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        self.assertEqual([group['owner_id'] for group in ManualWorkRepository(self.connection).pending('A')],
                         ['G-001','G-004'])
        scheduler=self.scheduler(max_workers=1)
        order, errors, threads = [], [], []
        def queued(repo, plan_id):
            try:
                with closing(database.connect_database(self.directory,run_migrations=False)) as connection:
                    with scheduler.slot('agent',scheduling.Task(repo,plan_id,connection)):
                        order.append(plan_id)
            except BaseException as error:
                errors.append(error)
        with scheduler.slot('agent',scheduling.Task('A','G-001',self.connection)):
            for repo,plan_id in (('A','G-002'),('A','G-004'),('B','G-003')):
                thread=threading.Thread(target=queued,args=(repo,plan_id))
                threads.append(thread)
                thread.start()
            with scheduler.condition:
                self.assertTrue(scheduler.condition.wait_for(lambda:len(scheduler.waiters['agent'])==3,3))
            self.assertEqual(order,[])
        for thread in threads: thread.join(4)
        self.assertEqual(errors,[])
        self.assertEqual(order,['G-003','G-004','G-002'])

    def test_project_dispatch_overlaps_independent_workers_and_keeps_verify_serial(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        self._insert_plan(self.connection,'G-004','READY',repo='A')
        scheduler=self.scheduler(max_workers=2)
        runtime=runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            runtime.activate('A','G-001',activation_sha='base',branch='one',worktree='one')
        identity=ProjectIdentity(self.connection,self.directory,'A',self.config_path,None,False,None,
                                 True,threading.Lock(),20,600,None)
        effects=SimpleNamespace(activate_ready_execplan=loop_manager.activate_ready_execplan,resume_manual_work=None)
        service=ProjectService(effects,identity)
        service.state=ProjectObservations(Path('A'),repo='A',base='main',base_sha='base',provider='codex')
        first_workers=threading.Barrier(2)
        third_worker=threading.Event()
        observations=[]
        lock=threading.Lock()
        def execute(child, repository, **_options):
            task=scheduling.current_task.get()
            with scheduler.slot('agent',task):
                if task.plan_id=='G-004': third_worker.set()
                else: first_workers.wait(3)
                snapshot=repository.projected('A')
                repository.transition_from_snapshot(snapshot,plan_values={'status':'VERIFYING'},
                                                      project_values={'phase':'VERIFYING'})
            with scheduler.slot('verify',task):
                if not third_worker.wait(3): raise AssertionError('verify 중 추가 Worker가 실행되지 않았습니다.')
                with lock:
                    observations.append((task.plan_id,sum(scheduler.running['verify'].values()),
                                         scheduling.managed_count(child.identity.connection,'A')))
                repository.transition_from_snapshot(repository.projected('A'),plan_values={'status':'PR_OPEN'},
                    project_values=runtime_state.clear_project_values())
            return 0
        token=scheduling.current_scheduler.set(scheduler)
        try:
            self.connection.execute('CREATE TEMP TABLE parent_session(value)')
            self.connection.execute('INSERT INTO parent_session VALUES (1)')
            with fast_mode_scope(self.config), mock.patch.object(ProjectService,'_dispatch_execution',execute):
                self.assertEqual(scheduling.run_project_tasks(service,newly_activated=True),0)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual({item[0] for item in observations},{'G-001','G-002','G-004'})
        self.assertTrue(all(item[1]==1 and item[2]<=3 for item in observations))
        self.assertEqual(scheduling.managed_count(self.connection,'A'),0)


    def test_manual_precedes_retry_then_resumed_and_new_calls_without_preemption(self):
        scheduler = self.scheduler()
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        manual = ManualWorkRepository(self.connection)
        group = manual.create('A', 'submit', 'manual', [], 'head', 'base', members=('G-001',))
        manual.accept(group, head='head', base='base', input_token='input')
        self._insert_plan(self.connection, 'G-004', 'READY', repo='B')
        tasks = [scheduling.Task('A', 'G-001', self.connection),
                 scheduling.Task('A', 'G-002', self.connection),
                 scheduling.Task('B', 'G-003', self.connection),
                 scheduling.Task('B', 'G-004', self.connection, resumed=False)]
        scheduler.requeue(tasks[1])
        scheduler.requeue(tasks[0])
        self.assertEqual(tasks[0].rank()[0], 0)
        for task in tasks:
            scheduler.waiters['agent'].append((task, task.rank()))
        chosen = []
        while scheduler.waiters['agent']:
            entry = scheduler._next('agent')
            scheduler.waiters['agent'].remove(entry)
            chosen.append(entry[0].plan_id)
        self.assertEqual(chosen, ['G-001', 'G-002', 'G-003', 'G-004'])
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        for task in (tasks[1], tasks[2]):
            scheduler.requeue(task)
            scheduler.waiters['agent'].append((task, task.rank()))
        self.assertIs(scheduler._next('agent')[0], tasks[1])

    def test_planning_drain_releases_queued_calls_but_keeps_running_and_other_projects(self):
        scheduler = self.scheduler(max_workers=1)
        errors = []
        def queued():
            try:
                with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                    with scheduler.slot('agent', scheduling.Task('A', 'G-002', connection)):
                        self.fail('Planner 대기 프로젝트의 새 호출이 실행됐습니다.')
            except BaseException as error:
                errors.append(error)
        with scheduler.slot('agent', scheduling.Task('A', 'G-001', self.connection)):
            thread = threading.Thread(target=queued)
            thread.start()
            with scheduler.condition:
                self.assertTrue(scheduler.condition.wait_for(lambda: len(scheduler.waiters['agent']) == 1, 3))
            scheduler.pause_project('A')
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(sum(scheduler.running['agent'].values()), 1)
            with scheduler.slot('verify', scheduling.Task('B', 'G-003', self.connection)):
                pass
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], scheduling.PlanningDrain)
        with self.assertRaises(scheduling.PlanningDrain), scheduler.admission(
                scheduling.Task('A', 'G-002', self.connection)):
            self.fail('Planner 대기 중 새 관리 대상이 편입됐습니다.')
        self.assertEqual(scheduler.waiters['agent'], [])

    def test_project_reentry_keeps_manual_submissions_pending_until_planner_finishes(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        scheduler = self.scheduler()
        effects = SimpleNamespace(resume_manual_work=mock.Mock(), pr_for_branch=mock.Mock())
        identity = ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                                   True, threading.Lock(), 20, 600, None)
        service = ProjectService(effects, identity)
        service.state = ProjectObservations(Path('A'), repo='A')
        token = scheduling.current_scheduler.set(scheduler)
        try:
            scheduler.pause_project('A')
            service._resume_manual_work()
            service.state.row = {'branch': 'owned'}
            self.assertIsNone(service._resume_existing_execution())
            effects.pr_for_branch.assert_not_called()
            effects.resume_manual_work.assert_not_called()
            scheduler.draining_projects.clear()
            service._resume_manual_work()
            effects.resume_manual_work.assert_called_once()
        finally:
            scheduling.current_scheduler.reset(token)

    def test_unconfirmed_call_defers_planner_without_reclaiming_capacity(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.application.contracts import PROJECT_STEP_TRANSIENT_PAUSE
        scheduler = self.scheduler()
        scheduler.running['verify']['A'] = 1
        scheduler.pause_project('A')
        identity = ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                                   True, threading.Lock(), 20, 600, None)
        service = ProjectService(SimpleNamespace(operation_print=mock.Mock()), identity)
        service.state = ProjectObservations(Path('A'), repo='A', row={})
        token = scheduling.current_scheduler.set(scheduler)
        try:
            self.assertEqual(service._run_coordinators(), PROJECT_STEP_TRANSIENT_PAUSE)
            self.assertEqual(scheduler.running['verify']['A'], 1)
        finally:
            scheduling.current_scheduler.reset(token)

    def test_reserved_call_only_blocks_actual_planning_release_for_its_project(self):
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        from loop_manager_internal.storage import task_calls
        port = _PlanningStatePorts(None, self.connection, None, None, self.directory)
        owner = task_calls.reserve(self.connection, self.directory, 'A', 'G-001', 'verify', 'run')
        self.assertTrue(port.planning_release_pending('A'))
        self.assertFalse(port.planning_release_pending('B'))
        task_calls.release(owner)
        self.assertFalse(port.planning_release_pending('A'))

    def test_reserved_verify_does_not_stop_project_selection_without_planning_drain(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        scheduler = self.scheduler()
        scheduler.running['verify']['A'] = 1
        effects = SimpleNamespace(_recorded_base_fetch=mock.Mock())
        identity = ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                                   True, threading.Lock(), 20, 600, None)
        service = ProjectService(effects, identity)
        service.state = ProjectObservations(Path('A'), repo='A', row={})
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with mock.patch.object(service, '_refresh_catalog', side_effect=LookupError('selection reached')), \
                 self.assertRaisesRegex(LookupError, 'selection reached'):
                service._run_coordinators()
            effects._recorded_base_fetch.assert_called_once()
        finally:
            scheduling.current_scheduler.reset(token)

    def test_merge_planning_pending_drains_project_and_returns_to_coordinator(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        scheduler = self.scheduler(max_tasks_per_project=1)
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        identity = ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                                   True, threading.Lock(), 20, 600, None)
        service = ProjectService(SimpleNamespace(activate_ready_execplan=mock.Mock(), resume_manual_work=None), identity)
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base', provider='codex')
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with mock.patch.object(ProjectService, '_dispatch_execution', return_value=loop_manager.PROJECT_STEP_PLANNING_PENDING):
                self.assertEqual(scheduling.run_project_tasks(service, newly_activated=False), loop_manager.PROJECT_STEP_RETRY)
            self.assertIn('A', scheduler.draining_projects)
            service.effects.activate_ready_execplan.assert_not_called()
        finally:
            scheduling.current_scheduler.reset(token)

    def test_local_transient_retry_preserves_three_attempt_pause_and_other_plan_records(self):
        from loop_manager_internal.application.project_runner import retry_pause_detail
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        repository = RecoveryRepository(self.connection)
        attempts = {}
        before = {}
        for index in range(3):
            for plan_id in ('G-001', 'G-002'):
                repository.observe(RecoveryObservation(RecoveryReason.STATUS_FAILED, 'A', plan_id, 'WORKING',
                    'revision', 'base', str(index), 'progress', 'temporary'))
            self.connection.commit()
            detail = retry_pause_detail(self.connection, 'A', before, attempts, plan_id='G-001')
            self.assertEqual(detail, 'temporary' if index == 2 else None)
            before = {str(row['subject']): int(row['generation']) for row in self.connection.execute(
                "SELECT subject,generation FROM recovery_records WHERE repo='A'")}
        self.assertEqual(attempts, {'G-001|transient_retry': 3})
        self.assertEqual(repository.row('A', 'G-001')['pending_action'], 'pause')
        self.assertEqual(repository.row('A', 'G-002')['disposition'], 'transient_retry')

    def test_requirement_revision_guard_requests_project_drain_before_next_phase(self):
        from loop_manager_internal.execution.execution_revision_handler import RevisionHandler
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind, ExecutionPhase
        scheduler = self.scheduler()
        ports = SimpleNamespace(
            state=mock.Mock(), identity=SimpleNamespace(repo='A'),
            _publish_rebased_checkpoint=mock.Mock(), _fetch_latest_base=mock.Mock(return_value='new-base'),
            planning_required=mock.Mock(return_value=True),
            _retry=lambda detail: ExecutionOutcome(ExecutionOutcomeKind.RETRY, detail=detail))
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with mock.patch('loop_manager_internal.execution.execution_revision.advance_owned_record'), \
                 mock.patch('loop_manager_internal.execution.manual_candidate.get_manual', return_value=None):
                outcome = RevisionHandler(ports).guard(SimpleNamespace(repo='A', phase=ExecutionPhase.IMPLEMENTING, resolving_rebase=False))
            self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
            self.assertEqual(scheduler.draining_projects, {'A'})
            ports.planning_required.assert_called_once_with('new-base')
        finally:
            scheduling.current_scheduler.reset(token)

    def test_phase_completion_yields_to_planner_before_starting_next_phase(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionPhase
        scheduler = self.scheduler()
        context = SimpleNamespace(repo='A', phase=ExecutionPhase.IMPLEMENTING, resolving_rebase=False)
        coordinator = mock.Mock()
        coordinator.prepare.return_value = context
        def finish(_context):
            scheduler.pause_project('A')
            return ExecutionOutcome.advanced(context)
        coordinator.dispatch.side_effect = finish
        effects = SimpleNamespace(_ExecutionLifecyclePorts=mock.Mock(),
                                  ExecutionPhaseCoordinator=mock.Mock(return_value=coordinator),
                                  _check_runtime_handoff=mock.Mock())
        identity = ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                                   True, threading.Lock(), 20, 600, None)
        service = ProjectService(effects, identity)
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base', provider='codex')
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with scheduler.task('A', 'G-001', self.connection), \
                 mock.patch('loop_manager_internal.application.project_service.notifications.flush_project_events'), \
                 self.assertRaises(scheduling.PlanningDrain):
                service._dispatch_execution(runtime_state.RuntimeStateRepository(self.connection), newly_activated=False)
            self.assertEqual(coordinator.dispatch.call_count, 1)
        finally:
            scheduling.current_scheduler.reset(token)

    def test_runtime_handoff_unwinds_waiters_without_reclaiming_running_calls(self):
        from loop_manager_internal.runtime.handoff_gate import RuntimeHandoffGate, _runtime_handoff_signal, SelfMergeRuntimeHandoff
        scheduler = self.scheduler()
        gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
        token = _runtime_handoff_signal.set(gate)
        try:
            with scheduler.slot('agent', scheduling.Task('A', 'G-001', self.connection)):
                gate.stop()
                with self.assertRaises(SelfMergeRuntimeHandoff), scheduler.slot(
                        'verify', scheduling.Task('B', 'G-003', self.connection)):
                    self.fail('runtime 교체 중 verify가 시작됐습니다.')
                self.assertEqual(sum(scheduler.running['agent'].values()), 1)
            with self.assertRaises(SelfMergeRuntimeHandoff), scheduler.admission(
                    scheduling.Task('A', 'G-002', self.connection)):
                self.fail('runtime 교체 중 관리 대상이 편입됐습니다.')
        finally:
            _runtime_handoff_signal.reset(token)
        self.assertEqual(scheduler.waiters['verify'], [])

    def test_retry_and_plan_block_progress_while_sibling_verify_is_running(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        self._insert_plan(self.connection, 'G-004', 'READY', repo='A')
        scheduler = self.scheduler(max_workers=1, max_tasks_per_project=2)
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for plan_id in ('G-001', 'G-002'):
                runtime.activate('A', plan_id, activation_sha='base', branch=plan_id, worktree=plan_id)
        identity = ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                                   True, threading.Lock(), 20, 600, None)
        service = ProjectService(SimpleNamespace(activate_ready_execplan=loop_manager.activate_ready_execplan,
                                                resume_manual_work=None), identity)
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base', provider='codex')
        verifying, replacement = threading.Event(), threading.Event()
        calls = []
        def execute(child, repository, **options):
            task = scheduling.current_task.get()
            if task.plan_id == 'G-002':
                with scheduler.slot('verify', task):
                    verifying.set()
                    self.assertTrue(replacement.wait(4), '형제 verify가 RETRY 또는 다른 작업 시작을 막았습니다.')
                repository.transition_from_snapshot(repository.projected('A'), plan_values={'status': 'PR_OPEN'},
                                                     project_values=runtime_state.clear_project_values())
                return 0
            if task.plan_id == 'G-001' and not calls:
                self.assertTrue(verifying.wait(3))
                calls.append('retry')
                return loop_manager.PROJECT_STEP_RETRY
            with scheduler.slot('agent', task):
                if task.plan_id == 'G-001':
                    self.assertIsNotNone(task.retry_order)
                    self.assertFalse(options['newly_activated'])
                    calls.append('correct')
                    repository.transition_from_snapshot(repository.projected('A'), plan_values={'status': 'PLAN_BLOCKED'},
                                                         project_values=runtime_state.clear_project_values())
                    return 1
                calls.append('replacement')
                replacement.set()
                repository.transition_from_snapshot(repository.projected('A'), plan_values={'status': 'PR_OPEN'},
                                                     project_values=runtime_state.clear_project_values())
                return 0
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with fast_mode_scope(replace(self.config, max_tasks_per_project=2)), \
                 mock.patch.object(ProjectService, '_dispatch_execution', execute):
                self.assertEqual(scheduling.run_project_tasks(service, newly_activated=False), 1)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(calls, ['retry', 'correct', 'replacement'])
        self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')
        self.assertEqual(scheduling.managed_count(self.connection, 'A'), 0)


    def test_unexpected_task_exception_retries_before_sibling_verify_finishes(self):
        self._assert_exception_isolation('retry')

    def test_repeated_task_exceptions_pause_only_that_task_and_keep_admitting(self):
        self._assert_exception_isolation('pause')

    def test_task_setup_exception_does_not_abort_project_result_collection(self):
        self._assert_exception_isolation('setup')

    def test_setup_db_wait_is_a_transient_pause(self):
        self._assert_exception_isolation('db_setup')

    def test_db_wait_preserves_task_and_keeps_independent_admission(self):
        self._assert_exception_isolation('db_wait')

    def test_pause_record_db_wait_is_inside_the_task_error_boundary(self):
        self._assert_exception_isolation('pause_record')

    def _assert_exception_isolation(self, failure):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.application.contracts import PROJECT_STEP_TRANSIENT_PAUSE
        from loop_manager_internal.git.manager_git_journal import MutationEvidenceError
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from loop_manager_internal.application import project_runner
        from loop_manager_internal.notifications import notifications
        self._insert_plan(self.connection, 'G-004', 'READY', repo='A')
        scheduler = self.scheduler(max_workers=2)
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for plan_id in ('G-001', 'G-002'):
                runtime.activate('A', plan_id, activation_sha='base', branch=plan_id, worktree=plan_id)
        service = ProjectService(SimpleNamespace(activate_ready_execplan=loop_manager.activate_ready_execplan,
                                                resume_manual_work=None),
            ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                            True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base', provider='codex')
        verifying, failed, replacement = threading.Event(), threading.Event(), threading.Event()
        calls = []
        configure_session = notifications.configure_session
        def configure(connection, **options):
            if failure in {'setup', 'db_setup'} and threading.current_thread().name == 'loop-task-G-001':
                failed.set()
                if failure == 'db_setup':
                    raise DatabaseWaitTimeout('setup DB queue deadline')
                raise RuntimeError('task setup failed')
            return configure_session(connection, **options)
        def execute(child, repository, **options):
            task = scheduling.current_task.get()
            if task.plan_id == 'G-002':
                with scheduler.slot('verify', task):
                    verifying.set()
                    self.assertTrue(replacement.wait(5), '작업 예외 이후 독립 작업 배정이 멈췄습니다.')
            else:
                with scheduler.slot('agent', task):
                    if task.plan_id == 'G-001':
                        self.assertTrue(verifying.wait(3))
                        if calls:
                            self.assertIsNotNone(task.retry_order)
                            self.assertFalse(options['newly_activated'])
                        calls.append(task.plan_id)
                        if failure in {'db_wait', 'pause_record'}:
                            failed.set()
                            if failure == 'db_wait':
                                raise DatabaseWaitTimeout('DB queue deadline')
                            return project_runner.PROJECT_STEP_RETRY
                        if failure == 'pause':
                            if len(calls) == 3:
                                failed.set()
                            raise sqlite3.OperationalError('database is locked')
                        if len(calls) == 1:
                            failed.set()
                            raise MutationEvidenceError('stored rebase result differs')
                    else:
                        self.assertTrue(failed.wait(3))
                        self.assertTrue(verifying.wait(3))
                        self.assertEqual(sum(scheduler.running['verify'].values()), 1)
                        replacement.set()
            repository.transition_from_snapshot(repository.projected('A'), plan_values={'status': 'PR_OPEN'},
                                                 project_values=runtime_state.clear_project_values())
            return 0
        pause_detail = project_runner.retry_pause_detail
        def pause(*args, **kwargs):
            if failure == 'pause_record' and kwargs.get('plan_id') == 'G-001':
                raise DatabaseWaitTimeout('pause record queue deadline')
            return pause_detail(*args, **kwargs)
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with fast_mode_scope(self.config), mock.patch.object(ProjectService, '_dispatch_execution', execute), \
                 mock.patch.object(notifications, 'configure_session', configure), \
                 mock.patch.object(project_runner, 'retry_pause_detail', pause):
                result = scheduling.run_project_tasks(service, newly_activated=True)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(len(calls), {'retry': 2, 'pause': 3, 'setup': 0, 'db_wait': 1, 'pause_record': 1, 'db_setup': 0}[failure])
        self.assertEqual(result, {'retry': 0, 'pause': PROJECT_STEP_TRANSIENT_PAUSE, 'setup': 1, 'db_wait': PROJECT_STEP_TRANSIENT_PAUSE, 'pause_record': PROJECT_STEP_TRANSIENT_PAUSE, 'db_setup': PROJECT_STEP_TRANSIENT_PAUSE}[failure])
        self.assertEqual(runtime.get('A', 'G-004').status, 'PR_OPEN')
        self.assertEqual(runtime.get('A', 'G-002').status, 'PR_OPEN')
        if failure == 'pause':
            self.assertEqual(runtime.get('A', 'G-001').status, 'IMPLEMENTING')
            self.assertIn('OperationalError: database is locked', runtime.get('A', 'G-001').last_error)
            self.assertIn('Traceback (most recent call last)', runtime.get('A', 'G-001').last_error)



    def test_independent_block_rechecks_use_target_ownership_and_one_recovery_lease(self):
        from loop_manager_internal.recovery.plan_recovery import recovering_plan
        from loop_manager_internal.storage import task_calls
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for plan_id in ('G-001', 'G-002'):
                runtime.activate('A', plan_id, activation_sha='base', branch=plan_id, worktree=plan_id)
        runtime.block_plan('A', 'G-001', reason_code='plan_input_invalid', evidence='declaration',
                           release_fingerprint='old', clear_projection=False)
        before = tuple(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone())
        outcomes = []
        def compete():
            with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                with recovering_plan(connection, self.directory, 'A', 'G-001') as available:
                    outcomes.append(available)
        with recovering_plan(self.connection, self.directory, 'A', 'G-001') as available:
            self.assertTrue(available)
            self.assertEqual(runtime_state.project_row(self.connection, 'A')['plan_path'], runtime.get('A', 'G-001').path)
            thread = threading.Thread(target=compete)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [False])
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone()), before)
        owner = task_calls.reserve(self.connection, self.directory, 'A', 'G-001', 'verify', 'pending')
        with recovering_plan(self.connection, self.directory, 'A', 'G-001') as available:
            self.assertFalse(available)
        task_calls.release(owner)
        runtime.update_plan('A', 'G-001', plan_values={'worktree': 'G-002'}, enqueue=False)
        with recovering_plan(self.connection, self.directory, 'A', 'G-001') as available:
            self.assertFalse(available)
        self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')

    def test_rebase_start_block_is_rechecked_without_document_change_or_idle_siblings(self):
        from loop_manager_internal.recovery.plan_recovery import recovering_plan
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from loop_manager_internal.git.managed_rebase_validation import RebaseDecision, RebaseStatus
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for plan_id in ('G-001', 'G-002'):
                runtime.activate('A', plan_id, activation_sha='base', branch=plan_id, worktree=plan_id)
        runtime.block_plan('A', 'G-001', reason_code='plan_input_invalid', evidence='old declaration check',
                           release_fingerprint='unchanged', clear_projection=False)
        checkpoint = ExecutionCheckpointRepository(self.connection)
        expected = checkpoint.record('A', 'G-001', expected_generation=0, operation='rebase',
            expected_head='original', target_base_oid='new-base', observed_remote_oid='remote', snapshot={})
        original = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
        sibling = runtime.get('A', 'G-002')
        observed = SimpleNamespace(fingerprint='owned-original-head')
        decision = RebaseDecision(RebaseStatus.START, 'owned_original_head_before_rebase')
        block = scoped_blocking.plan_block(self.connection, 'A', 'G-001')
        with fast_mode_scope(self.config), recovering_plan(self.connection, self.directory, 'A', 'G-001') as available:
            self.assertTrue(available)
            runtime.resume_rebase_block(original, runtime_state.project_row(self.connection, 'A'), block, checkpoint,
                (observed, decision), expected_checkpoint=expected, is_plan_block=True,
                reobserve=lambda: (observed, decision), dependencies_ready=lambda: True, pending_error='rebase pending')
        self.assertEqual(runtime.get('A', 'G-001').status, 'IMPLEMENTING')
        self.assertEqual(runtime.get('A', 'G-001').current_revision_sha, original['current_revision_sha'])
        self.assertEqual(checkpoint.get('A', 'G-001').operation, 'rebase')
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
        self.assertEqual(runtime.get('A', 'G-002'), sibling)

    def test_block_recheck_runs_before_sibling_verify_finishes(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.recovery.plan_recovery import recovering_plan
        self._insert_plan(self.connection, 'G-004', 'READY', repo='A')
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            runtime.activate('A', 'G-002', activation_sha='base', branch='two', worktree='two')
        runtime.block_plan('A', 'G-001', reason_code='plan_input_invalid', evidence='recheck', release_fingerprint='old',
            plan_values={'branch': 'one', 'worktree': 'one'}, clear_projection=False)
        verifying, resumed, additional = threading.Event(), threading.Event(), threading.Event()
        def recheck(connection, _repo_path, state_dir, repo, *_args, plan_id):
            self.assertEqual(scheduling.current_task.get().plan_id, plan_id)
            self.assertIs(scheduling.current_task.get().connection, connection)
            self.assertTrue(verifying.wait(3))
            self.assertTrue(additional.wait(4), '재평가가 독립 신규 작업의 배정을 막았습니다.')
            with recovering_plan(connection, state_dir, repo, plan_id) as available:
                self.assertTrue(available)
                runtime_state.RuntimeStateRepository(connection).transition(repo, plan_id,
                    plan_values={'status': 'IMPLEMENTING', 'blocked_reason': None}, project_values={'phase': 'WORKING'})
                with connection:
                    scoped_blocking.clear_plan_block(connection, repo, plan_id)
            return True
        service = ProjectService(SimpleNamespace(activate_ready_execplan=loop_manager.activate_ready_execplan,
            resume_manual_work=None, resume_plan_blocks=recheck),
            ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                            True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base')
        scheduler = self.scheduler()
        def execute(child, repository, **_options):
            task = scheduling.current_task.get()
            if task.plan_id == 'G-002':
                with scheduler.slot('verify', task):
                    verifying.set()
                    self.assertTrue(resumed.wait(4), '독립 차단 재평가가 형제 작업 완료를 기다렸습니다.')
            else:
                with scheduler.slot('agent', task):
                    if task.plan_id == 'G-004':
                        additional.set()
                    else:
                        resumed.set()
            repository.transition_from_snapshot(repository.projected('A'), plan_values={'status': 'PR_OPEN'},
                                                 project_values=runtime_state.clear_project_values())
            return 0
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with fast_mode_scope(self.config), mock.patch.object(ProjectService, '_dispatch_execution', execute):
                self.assertEqual(scheduling.run_project_tasks(service, newly_activated=False), 0)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(runtime.get('A', 'G-001').status, 'PR_OPEN')
        self.assertEqual(runtime.get('A', 'G-002').status, 'PR_OPEN')
        self.assertEqual(runtime.get('A', 'G-004').status, 'PR_OPEN')

    def test_user_question_recheck_requires_current_cause_evidence_and_preserves_failed_assessments(self):
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        checkpoint = ExecutionCheckpointRepository(self.connection)
        for case in ('resolved', 'remaining', 'links', 'frontmatter', 'bad_evidence', 'only_plan_evidence', 'probe_failure'):
            with self.subTest(case=case):
                runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='question ' + case,
                    release_fingerprint='old', clear_projection=False, plan_values={'worktree': 'one', 'active_sha': 'before'})
                plan = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
                block = dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
                saved = checkpoint.get('A', 'G-001')
                old = 'Support policy refresh and choose an implementation.'
                latest = 'Policy refresh is deferred; implement ordinary calls and mock checks.'
                if case == 'links':
                    old, latest = '[policy](old.md)', '[policy](new.md)'
                if case == 'frontmatter':
                    old, latest = '---\nsources: [old]\n---\nExisting goal.', '---\nsources: [new]\n---\nExisting goal.'
                def git(_repo, *args, **_options):
                    text = 'latest' if args[0] == 'rev-parse' else (latest if args[1].startswith('latest:') else old)
                    return SimpleNamespace(returncode=0, stdout=text)
                result = dict(resolved=case not in {'remaining'}, detail='cause assessment',
                    evidence=[dict(source=plan['path'] if case == 'only_plan_evidence' else 'docs/PRODUCT.md',
                                   quote='invented' if case == 'bad_evidence' else latest)])
                agent = mock.Mock(return_value=(1 if case == 'probe_failure' else 0,
                    'USER_DECISION_ASSESSMENT: ' + json.dumps(result)))
                effects = SimpleNamespace(git=git, capture_git_snapshot=mock.Mock(), run_agent=agent,
                                          validate_agent_scope=mock.Mock())
                def assess():
                    return _assess_user_decision_cause(effects, self.connection, Path('A'), self.directory,
                                                     'A', plan, self.config_path)
                if case in {'bad_evidence', 'only_plan_evidence', 'probe_failure'}:
                    with self.assertRaises(RuntimeError):
                        assess()
                    self.assertEqual(checkpoint.get('A', 'G-001'), saved)
                else:
                    resolved, _detail, _stage = assess()
                    self.assertEqual(resolved, case == 'resolved')
                    if case in {'links', 'frontmatter'}:
                        agent.assert_not_called()
                        self.assertEqual(checkpoint.get('A', 'G-001'), saved)
                    else:
                        self.assertEqual(assess()[0], resolved)
                        agent.assert_called_once()
                        stored = checkpoint.get('A', 'G-001').snapshot['user_decision_assessment']
                        self.assertEqual(stored['question'], 'question ' + case)
                self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001')), block)
                self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')



class ExecutionClockTests(unittest.TestCase):
    def test_only_confirmed_wait_is_excluded_and_nested_clocks_keep_prior_execution(self):
        now=[0.0]
        with tempfile.TemporaryDirectory() as directory:
            environment={BUDGET_DIRECTORY:directory}
            with ExecutionClock(environment,clock=lambda:now[0]) as outer:
                now[0]=2
                self.assertEqual(outer.remaining(10),8)
                with ExecutionClock(environment,clock=lambda:now[0]) as inner:
                    Path(directory,'session.json').write_text(json.dumps({'waiting_seconds':30}))
                    now[0]=32
                    self.assertEqual(inner.remaining(8),8)
                    self.assertEqual(outer.remaining(10),8)
                    now[0]=34
                    self.assertEqual(inner.remaining(8),6)
                    self.assertEqual(outer.remaining(10),6)

    def test_resource_wait_with_no_running_child_is_recorded_but_stale_wait_is_not(self):
        now=[0.0]
        sampler=mock.Mock()
        sampler.sample.side_effect=lambda: Snapshot(now[0],1,1,4096*MIB,'fake')
        with tempfile.TemporaryDirectory() as directory:
            coordinator=VerifyCoordinator(sampler=sampler,clock=lambda:now[0])
            session,secret=coordinator.open(1)
            coordinator.track_waiting(session,directory)
            with ExecutionClock({BUDGET_DIRECTORY:directory},clock=lambda:now[0]) as budget:
                for step in range(21):
                    now[0]=step*.1
                    self.assertFalse(coordinator.request(session,secret,'acquire')['granted'])
                self.assertAlmostEqual(budget.elapsed(),0)
                now[0]=20
                coordinator.request(session,secret,'acquire')
                self.assertAlmostEqual(budget.elapsed(),18)
                coordinator.finish(session,stopped=True)
