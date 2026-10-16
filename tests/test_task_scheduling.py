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
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config_path = self.directory / 'config.ini'
        self.config_path.write_text('[loop]\nmax_tasks_per_project=3\n', encoding='utf-8')
        self.config = load_managed_config(self.config_path)
        self.connection = database.connect_database(self.directory, create=True)
        self.addCleanup(self.connection.close)
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
