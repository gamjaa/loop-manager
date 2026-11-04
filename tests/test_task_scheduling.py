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
from loop_manager_internal.verification.execution_clock import ExecutionClock, BUDGET_DIRECTORY, VerifyBudgetError
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
        self._reset_catalog()

    def _reset_catalog(self):
        self.seed_database.backup(self.connection)
        self.assertEqual((self.directory / 'loop-manager.db').stat().st_ino, self.database_inode)
        for repo in ('A', 'B'):
            self.connection.execute("INSERT INTO projects(repo,base_branch) VALUES (?,'main')", (repo,))
        for plan_id, repo in (('G-001','A'), ('G-002','A'), ('G-003','B')):
            self._insert_plan(self.connection, plan_id, 'READY', repo=repo)

    def scheduler(self, **values):
        return scheduling.TaskScheduler(replace(self.config, **values), database.connect_database,
                                         self.directory, self.connection)

    def test_explicit_release_queues_at_capacity_and_preserves_rebase_then_yields_last(self):
        from loop_manager_internal.storage import management_admission as admission
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE repo='A'")
        self._insert_plan(self.connection,'G-004','PLAN_BLOCKED',repo='A')
        self.connection.execute("INSERT INTO execplan_blocks(repo,plan_id,scope,reason_code,evidence) "
            "VALUES ('A','G-004','plan','user_decision_required','original question')")
        self.connection.execute("INSERT INTO execution_checkpoints(repo,plan_id,generation,operation,snapshot) "
            "VALUES ('A','G-004',7,'rebase','{}')")
        self.connection.commit()
        project_registry.set_task_limit(self.connection,'A',2,3)
        checkpoint = tuple(self.connection.execute("SELECT * FROM execution_checkpoints WHERE plan_id='G-004'").fetchone())
        admission.release_blocks(self.connection,'A',['G-004','G-004'])
        self.assertTrue(admission.parked(self.connection,'A','G-004'))
        self.assertEqual(scheduling.managed_count(self.connection,'A'),2)
        admission.reconcile(self.connection,'A',2,busy={'G-002'})
        self.assertTrue(admission.parked(self.connection,'A','G-004'))
        self.assertTrue(admission.parked(self.connection,'A','G-002'))
        admission.reconcile(self.connection,'A',2)
        self.assertFalse(admission.parked(self.connection,'A','G-004'))
        self.assertFalse(admission.parked(self.connection,'A','G-001'))
        self.assertTrue(admission.parked(self.connection,'A','G-002'))
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM execution_checkpoints WHERE plan_id='G-004'").fetchone()),checkpoint)
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_blocks WHERE plan_id='G-004'").fetchone())
        self.assertEqual(self.connection.execute("SELECT status FROM exec_plans WHERE plan_id='G-002'").fetchone()[0],'IMPLEMENTING')
        sequence = self.connection.execute("SELECT sequence FROM task_management_admissions WHERE plan_id='G-004'").fetchone()[0]
        admission.release_blocks(self.connection,'A',['G-004'])
        self.assertEqual(self.connection.execute("SELECT sequence FROM task_management_admissions WHERE plan_id='G-004'").fetchone()[0],sequence)
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE plan_id='G-004'")
        self.connection.execute("UPDATE execution_checkpoints SET operation=NULL WHERE plan_id='G-004'")
        admission.reconcile(self.connection,'A',2)
        self.assertFalse(admission.parked(self.connection,'A','G-002'))
        self.assertEqual(scheduling.managed_count(self.connection,'A'),2)

    def test_handoff_restores_retry_order_after_task_return_and_database_reconnect(self):
        from loop_manager_internal.storage import management_admission as admission, task_calls
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE repo='A'")
        for pid in ('G-004', 'G-005', 'G-006', 'G-007'):
            self._insert_plan(self.connection, pid, 'REVIEWING', repo='A')
        # A shared manual candidate is one management unit and keeps its current correction phase.
        store = ManualWorkRepository(self.connection)
        group = store.create('A', 'submit', 'manual', [], 'head', 'base', members=('G-004','G-005'))
        store.accept(group,head='head',base='base',input_token='input')
        store.update(group, state='AUTOMATIC', owner_id='G-004')
        self.connection.execute("INSERT INTO maintenance_cycles(repo,phase,refactoring_plan_ids,publication_stage) "
            "VALUES ('A','refactoring','[\"G-001\",\"G-002\",\"G-006\"]','remote_confirmed')")
        self.connection.execute("UPDATE exec_plans SET worker_session_id='preserved' WHERE repo='A'")
        self.connection.execute("INSERT INTO execution_checkpoints(repo,plan_id,generation,operation,snapshot) "
            "VALUES ('A','G-002',7,'rebase','{}')")
        scheduler = self.scheduler()
        with scheduler.task('A','G-001',self.connection):
            scheduler.requeue(scheduling.current_task.get())
        with scheduler.task('A','G-002',self.connection):
            scheduler.requeue(scheduling.current_task.get())
        admission.request(self.connection,'A','G-004')
        admission.request(self.connection,'A','G-005')
        admission.request(self.connection,'A','G-007')
        original = [tuple(row) for row in self.connection.execute("SELECT * FROM exec_plans ORDER BY repo,plan_id")]
        checkpoint = tuple(self.connection.execute("SELECT * FROM execution_checkpoints").fetchone())
        owner = task_calls.reserve(self.connection,self.directory,'A','G-002','verify','run')
        task_calls.mark_uncertain(owner)
        admission.reconcile(self.connection,'A',3,busy={'G-002'},eligible=lambda pid:
            scheduler._eligible(scheduling.Task('A',pid,self.connection),self.connection,check_management=False))
        self.assertTrue(admission.parked(self.connection,'A','G-006'))
        self.assertTrue(admission.parked(self.connection,'A','G-002'))
        self.assertTrue(admission.parked(self.connection,'A','G-007'))
        # Repeating requests on a separate connection does not change acceptance or selection.
        accepted = tuple(self.connection.execute("SELECT priority,sequence FROM task_management_admissions WHERE plan_id='G-007'").fetchone())
        with closing(database.connect_database(self.directory,run_migrations=False)) as other:
            admission.request(other,'A','G-007')
            other.commit()
            replacement = scheduling.TaskScheduler(self.config,database.connect_database,self.directory,other)
            self.assertEqual(replacement.uncertain['verify'],1)
            admission.reconcile(other,'A',3,eligible=lambda pid:
                replacement._eligible(scheduling.Task('A',pid,other),other,check_management=False))
            self.assertTrue(admission.parked(other,'A','G-007'))
            task_calls.release(owner)
            admission.reconcile(other,'A',3,eligible=lambda pid:
                replacement._eligible(scheduling.Task('A',pid,other),other,check_management=False))
            self.assertFalse(admission.parked(other,'A','G-007'))
            self.assertEqual(scheduling.managed_count(other,'A'),3)
            other.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE repo='A' AND plan_id IN ('G-004','G-005','G-007')")
            other.commit()
            admission.reconcile(other,'A',2)
            self.assertFalse(admission.parked(other,'A','G-002'))
            self.assertTrue(admission.parked(other,'A','G-006'))
            with replacement.task('A','G-002',other):
                restored = scheduling.current_task.get()
                self.assertEqual(restored.retry_order,2)
                self.assertEqual(restored.rank(),(1,2,()))
                replacement.requeue(restored)
                self.assertEqual(restored.retry_order,3)
            self.assertEqual(scheduling.Task('A','G-001',other).rank(),(1,1,()))
            self.assertEqual(tuple(other.execute("SELECT priority,sequence FROM task_management_admissions WHERE plan_id='G-007'").fetchone()),accepted)
        # Only the explicit completion above changed execution rows.
        self.connection.execute("UPDATE exec_plans SET status='REVIEWING' WHERE repo='A' AND plan_id IN ('G-004','G-005','G-007')")
        self.assertEqual([tuple(row) for row in self.connection.execute("SELECT * FROM exec_plans ORDER BY repo,plan_id")],original)
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM execution_checkpoints").fetchone()),checkpoint)
        self.assertEqual(self.connection.execute("SELECT publication_stage FROM maintenance_cycles WHERE repo='A'").fetchone()[0],'remote_confirmed')

    def test_old_admission_does_not_evict_on_limit_reduction_but_new_request_does(self):
        from loop_manager_internal.storage import management_admission as admission
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE repo='A'")
        admission.request(self.connection,'A','G-001')
        admission.reconcile(self.connection,'A',2)
        admission.reconcile(self.connection,'A',1)
        self.assertEqual(scheduling.managed_count(self.connection,'A'),2)
        self.assertFalse(admission.parked(self.connection,'A','G-002'))
        self._insert_plan(self.connection,'G-004','IMPLEMENTING',repo='A')
        admission.request(self.connection,'A','G-004')
        admission.reconcile(self.connection,'A',1,eligible=lambda pid:pid!='G-004')
        self.assertEqual(scheduling.managed_count(self.connection,'A'),2)
        admission.reconcile(self.connection,'A',1)
        self.assertEqual(scheduling.managed_count(self.connection,'A'),2)
        self.assertTrue(admission.parked(self.connection,'A','G-004'))
        # Once the earlier request completes, a new eligible request gets its ordered handoff.
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE repo='A' AND plan_id='G-001'")
        admission.reconcile(self.connection,'A',1)
        self.assertFalse(admission.parked(self.connection,'A','G-004'))
        self.assertTrue(admission.parked(self.connection,'A','G-002'))
        self.assertFalse(admission.parked(self.connection,'B','G-003'))

    def test_displaced_request_keeps_its_place_ahead_of_a_new_request(self):
        from loop_manager_internal.storage import management_admission as admission
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE repo='A'")
        admission.request(self.connection,'A','G-002')
        admission.reconcile(self.connection,'A',1)
        self._insert_plan(self.connection,'G-004','VERIFYING',repo='A')
        store = ManualWorkRepository(self.connection)
        group = store.create('A','submit','manual',[],'head','base',members=('G-004',))
        store.accept(group,head='head',base='base',input_token='input')
        store.update(group,state='AUTOMATIC',owner_id='G-004')
        admission.request(self.connection,'A','G-004')
        admission.reconcile(self.connection,'A',1)
        self.assertTrue(admission.parked(self.connection,'A','G-002'))
        self.assertEqual(self.connection.execute("SELECT pending FROM task_management_admissions WHERE plan_id='G-002'").fetchone()[0],0)
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE plan_id='G-004'")
        self._insert_plan(self.connection,'G-005','IMPLEMENTING',repo='A')
        admission.request(self.connection,'A','G-005')
        admission.reconcile(self.connection,'A',1)
        self.assertFalse(admission.parked(self.connection,'A','G-002'))
        self.assertTrue(admission.parked(self.connection,'A','G-005'))
        self.assertEqual(scheduling.managed_count(self.connection,'A'),1)

    def test_concurrent_requests_and_retry_registration_keep_unique_durable_order(self):
        from loop_manager_internal.storage import management_admission as admission
        scheduler = self.scheduler()
        ready = threading.Barrier(2)
        errors = []
        def register(pid):
            try:
                with closing(database.connect_database(self.directory,run_migrations=False)) as connection:
                    ready.wait(timeout=3)
                    admission.request(connection,'A',pid)
                    admission.request(connection,'A',pid)
                    scheduler.requeue(scheduling.Task('A',pid,connection))
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=register,args=(pid,)) for pid in ('G-001','G-002')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        rows = self.connection.execute('SELECT priority,retry_order,pending,parked FROM task_management_admissions').fetchall()
        self.assertEqual(sorted(row['priority'] for row in rows),[1,2])
        self.assertEqual(sorted(row['retry_order'] for row in rows),[1,2])
        self.assertTrue(all(row['pending']==1 and row['parked']==1 for row in rows))

    def test_legacy_admission_upgrade_keeps_only_proven_order_and_is_idempotent(self):
        from loop_manager_internal.storage import management_admission as admission
        with closing(sqlite3.connect(':memory:')) as legacy:
            legacy.row_factory = sqlite3.Row
            legacy.execute('CREATE TABLE task_management_admissions(sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
                'repo TEXT NOT NULL,plan_id TEXT NOT NULL,priority INTEGER NOT NULL DEFAULT 0,'
                'parked INTEGER NOT NULL DEFAULT 1,release_evidence TEXT,UNIQUE(repo,plan_id))')
            legacy.execute("INSERT INTO task_management_admissions(repo,plan_id,priority) VALUES ('A','G-001',7)")
            legacy.execute("INSERT INTO task_management_admissions(repo,plan_id,priority,parked) VALUES ('A','G-002',0,0)")
            admission.ensure_schema(legacy)
            admission.ensure_schema(legacy)
            rows = legacy.execute('SELECT * FROM task_management_admissions ORDER BY sequence').fetchall()
            self.assertEqual([(row['sequence'],row['priority'],row['pending']) for row in rows],[(1,7,1),(2,0,0)])
            self.assertTrue(all(row['retry_order'] is None and row['resumed'] is None for row in rows))
            admission.request(legacy,'A','G-002')
            self.assertTrue(admission.parked(legacy,'A','G-002'))
            self.assertEqual(legacy.execute("SELECT pending FROM task_management_admissions WHERE plan_id='G-002'").fetchone()[0],1)

    def test_handoff_transaction_rollback_and_changed_eligibility_preserve_waiting_request(self):
        from loop_manager_internal.storage import management_admission as admission
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE repo='A'")
        self._insert_plan(self.connection,'G-004','VERIFYING',repo='A')
        admission.request(self.connection,'A','G-004')
        self.connection.commit()
        self.connection.execute("CREATE TEMP TRIGGER interrupt_handoff BEFORE UPDATE ON task_management_admissions "
            "WHEN NEW.plan_id='G-004' BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        before = [tuple(row) for row in self.connection.execute('SELECT * FROM task_management_admissions')]
        with self.assertRaisesRegex(sqlite3.IntegrityError,'interrupted'):
            admission.reconcile(self.connection,'A',1,busy={'G-002'})
        self.assertEqual([tuple(row) for row in self.connection.execute('SELECT * FROM task_management_admissions')],before)
        self.connection.execute('DROP TRIGGER interrupt_handoff')
        admission.reconcile(self.connection,'A',1,busy={'G-002'})
        admission.reconcile(self.connection,'A',1,eligible=lambda pid:pid!='G-004')
        self.assertFalse(admission.parked(self.connection,'A','G-001'))
        self.assertTrue(admission.parked(self.connection,'A','G-004'))
        admission.reconcile(self.connection,'A',1)
        self.assertFalse(admission.parked(self.connection,'A','G-004'))

    def test_explicit_release_is_atomic_and_does_not_override_safety_blocks(self):
        from loop_manager_internal.storage import management_admission as admission
        for pid,reason in [('G-001','user_decision_required'),('G-002','managed_worktree_conflict')]:
            self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo='A' AND plan_id=?",(pid,))
            self.connection.execute("INSERT INTO execplan_blocks(repo,plan_id,scope,reason_code,evidence) VALUES ('A',?,'plan',?,'original')",(pid,reason))
        self.connection.commit()
        for reason in ('managed_worktree_conflict', 'plan_input_invalid'):
            with self.subTest(reason=reason):
                self.connection.execute("UPDATE execplan_blocks SET reason_code=? WHERE repo='A' AND plan_id='G-002'", (reason,))
                self.connection.commit()
                with self.assertRaises(ValueError):
                    admission.release_blocks(self.connection,'A',['G-001','G-002'])
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM execplan_blocks').fetchone()[0],2)
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_management_admissions').fetchone()[0],0)

    def test_unblock_cli_dispatch_and_live_reservation_drain(self):
        from loop_manager_internal.application.project_service import ProjectService
        from loop_manager_internal.application.commands import execute_runtime_command
        from loop_manager_internal.storage import management_admission as admission, task_calls
        repo = str(self.directory.resolve())
        self.connection.execute("UPDATE projects SET repo=?,registered=1 WHERE repo='A'",(repo,))
        self.connection.execute("UPDATE exec_plans SET repo=?,status='IMPLEMENTING' WHERE repo='A'",(repo,))
        self._insert_plan(self.connection,'G-004','PLAN_BLOCKED',repo=repo)
        self.connection.execute("INSERT INTO execplan_blocks(repo,plan_id,scope,reason_code,evidence) VALUES (?, 'G-004','plan','common_defect_pending','cause')",(repo,))
        self.connection.commit()
        args = loop_manager.build_parser().parse_args(['unblock',repo,'G-004'])
        messages=[]
        effects=SimpleNamespace(connect_database=database.connect_database,operation_print=messages.append)
        self.assertEqual(execute_runtime_command(effects,args,self.directory,self.config_path),0)
        self._insert_plan(self.connection,'G-005','MERGED',repo=repo)
        self.connection.commit()
        archive_opportunities = []
        def archive(_connection, _path, _state, _repo, _base, **options):
            self.assertTrue(options['archive_safety']())
            from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
            item = ExecPlanArchiveRepository(self.connection).merged_candidates(repo)[0]
            self.assertEqual(item.plan_id, 'G-005')
            self.assertIsNone(options['candidate_safety'](item))
            archive_opportunities.append(admission.parked(self.connection, repo, 'G-004'))
        service = ProjectService.__new__(ProjectService)
        service.identity = SimpleNamespace(connection=self.connection, state_dir=self.directory)
        service.state = SimpleNamespace(repo=repo, repo_path=Path(repo), base='main', catalog_refresh=mock.Mock())
        service.effects = SimpleNamespace(git=mock.Mock(), operation_print=mock.Mock(),
                                         dispatch_execplan_archive_housekeeping=archive)
        owner = task_calls.reserve(self.connection,self.directory,repo,'G-002','verify','run')
        self.connection.commit()
        admission.reconcile(self.connection,repo,1)
        self.assertTrue(admission.parked(self.connection,repo,'G-004'))
        service._archive_scheduling_boundary()
        self.assertIsNotNone(self.connection.execute('SELECT 1 FROM task_call_reservations WHERE token=?',(owner.token,)).fetchone())
        scheduler=self.scheduler()
        self.assertFalse(scheduler._eligible(scheduling.Task(repo,'G-002',self.connection),self.connection))
        task_calls.release(owner)
        admission.reconcile(self.connection,repo,1)
        self.assertFalse(admission.parked(self.connection,repo,'G-004'))
        service._archive_scheduling_boundary()
        self.assertEqual(archive_opportunities, [True, False])
        self.assertEqual(scheduling.managed_count(self.connection,repo),1)
        self.assertIn('G-004', messages[0])

    def test_manual_management_admission_queues_without_replacing_projection(self):
        from loop_manager_internal.storage import management_admission as admission
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE repo='A'")
        self._insert_plan(self.connection,'G-004','MANUAL_PENDING',repo='A')
        store=ManualWorkRepository(self.connection)
        group=store.create('A','submit','manual',[],'head','base',members=('G-004',))
        store.update(group,state='AUTOMATIC',request_id='accepted',owner_id='G-004')
        project_registry.set_task_limit(self.connection,'A',2,3)
        before=tuple(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone())
        runtime_state.RuntimeStateRepository(self.connection).update_plan('A','G-004',plan_values={'status':'VERIFYING'},enqueue=False)
        self.assertTrue(admission.parked(self.connection,'A','G-004'))
        self.assertEqual(scheduling.managed_count(self.connection,'A'),2)
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone()),before)
        admission.reconcile(self.connection,'A',2,eligible=lambda _pid:False)
        self.assertTrue(admission.parked(self.connection,'A','G-004'))
        self.assertFalse(admission.parked(self.connection,'A','G-002'))
        admission.reconcile(self.connection,'A',2)
        self.assertFalse(admission.parked(self.connection,'A','G-004'))
        self.assertTrue(admission.parked(self.connection,'A','G-002'))

    def test_explicit_release_runs_outside_blocked_maintenance_without_releasing_other_gates(self):
        from loop_manager_internal.storage import management_admission as admission, maintenance_state
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo='A'")
        for pid in ('G-001','G-002'):
            self.connection.execute("INSERT INTO execplan_blocks(repo,plan_id,scope,reason_code,evidence) "
                "VALUES ('A',?,'plan','common_defect_pending','original cause')",(pid,))
        self._insert_plan(self.connection,'G-004','READY',repo='A')
        self.connection.execute("INSERT INTO maintenance_cycles(repo,phase,refactoring_plan_ids,publication_stage) "
            "VALUES ('A','refactoring','[\"G-001\"]','remote_confirmed')")
        self.connection.commit()
        original_cycle = maintenance_state.MaintenanceRepository(self.connection).get('A')
        admission.release_blocks(self.connection,'A',['G-002'])
        scheduler=self.scheduler(max_workers=1,max_tasks_per_project=1)
        scheduler.priority_reader=loop_manager.catalog_schedule_decision
        task=scheduling.Task('A','G-002',self.connection)
        self.assertTrue(scheduler._eligible(task,self.connection,check_management=False))
        admission.reconcile(self.connection,'A',1,eligible=lambda pid:
            scheduler._eligible(scheduling.Task('A',pid,self.connection),self.connection,check_management=False))
        with scheduler.slot('agent',task):
            self.assertEqual(scheduling.managed_count(self.connection,'A'),1)
        self.assertFalse(scheduler._eligible(scheduling.Task('A','G-004',self.connection),self.connection))
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM execplan_blocks WHERE plan_id='G-001'").fetchone())
        self.assertEqual(maintenance_state.MaintenanceRepository(self.connection).get('A'),original_cycle)
        for stage in ('intent','remote_confirmed'):
            self.connection.execute("UPDATE maintenance_cycles SET publication_stage=? WHERE repo='A'",(stage,))
            self.assertEqual(scheduler._eligible(task,self.connection),stage=='remote_confirmed')
        self.connection.execute("UPDATE exec_plans SET depends_on='[\"G-004\"]' WHERE repo='A' AND plan_id='G-002'")
        self.assertFalse(scheduler._eligible(task,self.connection))

    def test_accepted_manual_reentry_respects_maintenance_publication_and_git_safety(self):
        from loop_manager_internal.application.manual_work import ManualWorkService
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        self.connection.execute("UPDATE exec_plans SET status='MANUAL_PENDING' WHERE repo='A'")
        store=ManualWorkRepository(self.connection)
        group=store.create('A','submit','manual',[],'head','base',members=('G-002',))
        store.update(group,state='AUTOMATIC',request_id='accepted',owner_id='G-002')
        group=store.get('A',group['group_id'])
        self.connection.execute("INSERT INTO maintenance_cycles(repo,phase,refactoring_plan_ids,publication_stage) "
            "VALUES ('A','refactoring','[\"G-001\"]','remote_confirmed')")
        service=object.__new__(ManualWorkService)
        service.connection,service.repo,service.store=self.connection,'A',store
        service.state_dir,service.repo_path=self.directory,Path('A')
        service.effects=SimpleNamespace(git=mock.Mock())
        with mock.patch('loop_manager_internal.git.manager_git_journal.has_conflicting_project_mutation',return_value=False) as conflict:
            self.assertTrue(service.slot_available(recovering=group))
            self.assertFalse(service.slot_available())
            self.connection.execute("UPDATE maintenance_cycles SET publication_stage='intent' WHERE repo='A'")
            self.assertFalse(service.slot_available(recovering=group))
            self.connection.execute("UPDATE maintenance_cycles SET publication_stage='remote_confirmed' WHERE repo='A'")
            conflict.return_value=True
            self.assertFalse(service.slot_available(recovering=group))

    def test_queued_manual_resume_preserves_the_other_project_projection(self):
        from loop_manager_internal.application.manual_work import ManualWorkService
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        from loop_manager_internal.storage.management_admission import parked
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE repo='A'")
        self._insert_plan(self.connection,'G-004','MANUAL_PENDING',repo='A')
        store=ManualWorkRepository(self.connection)
        group=store.create('A','submit','manual',[],'head','base',members=('G-004',))
        store.update(group,state='AUTOMATIC',request_id='accepted',owner_id='G-004',worktree=str(self.directory))
        group=store.get('A',group['group_id'])
        project_registry.set_task_limit(self.connection,'A',2,3)
        before=tuple(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone())
        service=object.__new__(ManualWorkService)
        service.connection,service.repo,service.store=self.connection,'A',store
        service.state_dir,service.repo_path,service.base=self.directory,Path('A'),'main'
        service.runtime=runtime_state.RuntimeStateRepository(self.connection)
        service.effects=SimpleNamespace(git=mock.Mock(),git_bytes=mock.Mock(),resolve_profile=mock.Mock())
        service.config_path,service.project=self.config_path,{'provider':'codex'}
        with mock.patch('loop_manager_internal.execution.manual_candidate.ManualCandidate.note_pending'), \
             mock.patch('loop_manager_internal.application.manual_work.confirm_dependencies'), \
             mock.patch.object(service,'slot_available',return_value=True):
            service._resume_group(group)
        self.assertTrue(parked(self.connection,'A','G-004'))
        self.assertEqual(service.runtime.get('A','G-004').status,'VERIFYING')
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone()),before)

    def test_scheduled_call_waits_past_runtime_ttl_without_probe_and_honors_handoff(self):
        from loop_manager_internal.runtime import handoff_gate
        from loop_manager_internal.storage import task_calls
        for stopped in (False, True):
            with self.subTest(handoff=stopped):
                self._reset_catalog()
                scheduler = self.scheduler(verify_concurrency=1)
                scheduler.running['verify']['B'] = 1
                observer = mock.Mock()
                gate = handoff_gate.RuntimeHandoffGate(threading.Event(), threading.Lock(), observer,
                                                      observed_at=0)
                clock, waits, calls, preflights = [31.0], [], [], []
                def waiting(_timeout):
                    observer.assert_not_called()
                    clock[0] += 31
                    waits.append(True)
                    if len(waits) == 2:
                        scheduler.running['verify']['B'] = 0
                        if stopped:
                            gate.stop()
                def preflight():
                    observer.assert_not_called()
                    preflights.append(True)
                @scheduling.scheduled_call('verify')
                def verify():
                    observer.assert_called_once()
                    task_calls.mark_started(self.connection, token=task_calls.current_call.get().token)
                    calls.append(True)
                token = scheduling.current_scheduler.set(scheduler)
                task = scheduling.Task('A', 'G-001', self.connection)
                scheduler.requeue(task)
                selected = scheduling.current_task.set(task)
                handoff = handoff_gate._runtime_handoff_signal.set(gate)
                try:
                    with mock.patch.object(handoff_gate.time, 'monotonic', side_effect=lambda: clock[0]), \
                         mock.patch.object(scheduler.condition, 'wait', side_effect=waiting), \
                         scheduling.verify_preflight(preflight):
                        if stopped:
                            with self.assertRaises(handoff_gate.SelfMergeRuntimeHandoff):
                                verify()
                        else:
                            verify()
                finally:
                    handoff_gate._runtime_handoff_signal.reset(handoff)
                    scheduling.current_task.reset(selected)
                    scheduling.current_scheduler.reset(token)
                self.assertEqual(len(waits), 2)
                self.assertEqual(calls, [] if stopped else [True])
                self.assertEqual(preflights, [] if stopped else [True])
                self.assertEqual(observer.call_count, 0 if stopped else 1)
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)
                self.assertEqual(sum(scheduler.running['verify'].values()), 0)
                self.assertEqual(scheduler.retained, {})
                self.assertEqual(scheduler.cursor.get('verify'), None)
                cursor = self.connection.execute("SELECT repo FROM task_scheduling_cursor WHERE kind='verify'").fetchone()
                self.assertEqual(cursor[0] if cursor else None, None if stopped else 'A')
                self.assertEqual(task.retry_order, 1 if stopped else None)
                saved = self.connection.execute("SELECT retry_order,resumed FROM task_management_admissions "
                    "WHERE repo='A' AND plan_id='G-001'").fetchone()
                self.assertEqual(tuple(saved), (1 if stopped else None, 1))

    def test_full_call_slots_skip_priority_refresh_until_capacity_returns(self):
        self.connection.commit()
        scheduler = self.scheduler(verify_concurrency=1)
        scheduler.running['verify']['A'] = 1
        task = scheduling.Task('A', 'G-001', self.connection)
        reads = mock.Mock(side_effect=[(2, '', (3, 1)), (2, '', (0, 1))])
        waits = []
        def waiting(_timeout):
            waits.append(True)
            self.assertEqual(reads.call_count, 1)
            if len(waits) == 3:
                scheduler.running['verify']['A'] = 0
        with mock.patch.object(task, 'rank', reads), mock.patch.object(scheduler.condition, 'wait', side_effect=waiting):
            with scheduler.slot('verify', task):
                self.assertEqual(reads.call_count, 2)
                self.assertEqual(scheduler.waiters['verify'], [])
        self.assertEqual(len(waits), 3)

    def test_journal_rejects_same_thread_lease_from_another_owner(self):
        from loop_manager_internal.git import manager_git_journal
        from loop_manager_internal.runtime.contracts import ProcessLockBusyError
        from loop_manager_internal.runtime.resource_lock import resource_locks
        target = 'refs/heads/shared'
        apply = mock.Mock()
        with resource_locks(self.directory, 'A', ['git:' + target], owner='automatic-push'):
            with self.assertRaisesRegex(ProcessLockBusyError, 'automatic-push'):
                manager_git_journal.execute(self.directory, repo='A', target=target,
                    owner='manual-push', kind='manual_branch_push',
                    before={'oid': None}, expected={'oid': 'a' * 40},
                    observe=lambda: {'oid': None}, apply=apply, marker=lambda _: True)
        apply.assert_not_called()

    def test_reserved_workspace_reuses_only_open_same_thread_resource_leases(self):
        from contextvars import Context, copy_context
        from loop_manager_internal.recovery.plan_recovery import reserved_workspace_ownership
        from loop_manager_internal.runtime.contracts import ProcessLockBusyError
        from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
        worktree = str((self.directory / 'reserved').resolve())
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET workspace_owned=2,branch='reserved',worktree=? "
                                    "WHERE repo='A' AND plan_id='G-001'", (worktree,))
        resources = ['plan:G-001', 'branch:reserved', 'worktree:' + worktree]
        with resource_locks(self.directory, 'A', resources, owner='task-execution'):
            with reserved_workspace_ownership(self.connection, self.directory, 'A', 'G-001') as proof:
                proof.check()
                from loop_manager_internal.git import manager_git_journal
                self.assertFalse(manager_git_journal.recover_active(self.directory, repo='A', target=worktree,
                    kind='managed_worktree_create', observe=lambda: self.fail('no active mutation'), marker=lambda *_: False))
                apply = mock.Mock()
                with self.assertRaises(ProcessLockBusyError):
                    manager_git_journal.execute(self.directory, repo='A', target=worktree,
                        owner='plan:G-999', kind='managed_worktree_create',
                        before={}, expected={}, observe=lambda: {}, apply=apply, marker=lambda _: True)
                apply.assert_not_called()
            inherited = copy_context()
            observed = []
            thread = threading.Thread(target=lambda: observed.append(
                inherited.run(resource_owned, self.directory, 'A', 'plan:G-001')))
            thread.start()
            thread.join(2)
            self.assertEqual(observed, [False])
            saved = copy_context()
            def other_recovery():
                with self.assertRaises(ProcessLockBusyError):
                    with reserved_workspace_ownership(self.connection, self.directory, 'A', 'G-001'):
                        self.fail('another recovery acquired the held plan')
            Context().run(other_recovery)
        self.assertFalse(saved.run(resource_owned, self.directory, 'A', 'plan:G-001'))

    def test_workspace_identity_repair_locks_checkpoint_resources(self):
        from loop_manager_internal.recovery.plan_recovery import reserved_workspace_ownership
        from loop_manager_internal.runtime.resource_lock import resource_owned
        worktree = str((self.directory / 'restored').resolve())
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET workspace_owned=1,branch=NULL,worktree='planning' "
                                    "WHERE repo='A' AND plan_id='G-001'")
        with reserved_workspace_ownership(self.connection, self.directory, 'A', 'G-001',
                                          workspace=('restored', worktree)) as proof:
            proof.check()
            for resource in ('plan:G-001', 'branch:restored', 'worktree:' + worktree, 'git:' + worktree):
                self.assertTrue(resource_owned(self.directory, 'A', resource), resource)
        self.assertFalse(resource_owned(self.directory, 'A', 'branch:restored'))

    def test_parallel_block_uses_selected_plan_and_preserves_representative(self):
        from loop_manager_internal.recovery import block_service
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason, decide
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for key in ('G-001', 'G-002'):
                runtime.activate('A', key, activation_sha='base', branch=key, worktree=key)
        before = runtime.get('A', 'G-001')
        runtime.hydrate_snapshot('A', before)
        project = dict(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone())
        effects = SimpleNamespace(
            _plan_id_from_path=lambda path: 'G-001' if 'G-001' in path else 'G-002',
            _worktree_execplan_input_fingerprint=lambda *args: 'candidate',
            capture_git_snapshot=lambda *args: SimpleNamespace(serialize=lambda: 'snapshot'),
            project_execplan=lambda connection, repo: runtime.projected_row(repo),
            _final_block_recovery_decision=block_service._final_block_recovery_decision)
        decision = decide(RecoveryObservation(RecoveryReason.PLAN_INPUT_INVALID, 'A', 'G-002',
            'WORKING', 'plan', 'base', 'fingerprint', 'progress', 'cause'))
        with runtime_state.selected_execution('A', 'G-002'):
            block_service.block_active_execplan(effects, self.connection, 'A', Path('G-002'),
                'cause', '', recovery_decision=decision)
        self.assertEqual(runtime.get('A', 'G-001'), before)
        self.assertEqual(dict(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone()), project)
        self.assertEqual(runtime.get('A', 'G-002').status, 'PLAN_BLOCKED')

    def test_parallel_block_rechecks_target_snapshot_after_observation(self):
        from loop_manager_internal.application.task_scheduling import TaskRecheckRequired
        from loop_manager_internal.recovery import block_service
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for key in ('G-001', 'G-002'):
                runtime.activate('A', key, activation_sha='base', branch=key, worktree=key)
        representative = runtime.get('A', 'G-001')
        runtime.hydrate_snapshot('A', representative)
        representative_project = dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo='A'").fetchone())
        concurrent_result = 'concurrent B result'

        def capture():
            self.assertFalse(self.connection.in_transaction)
            with closing(database.connect_database(self.directory, run_migrations=False)) as other:
                with other:
                    other.execute(
                        "UPDATE exec_plans SET worker_result=? WHERE repo='A' AND plan_id='G-002'",
                        (concurrent_result,),
                    )
            return SimpleNamespace(serialize=lambda: 'observed snapshot')

        effects = SimpleNamespace(
            _plan_id_from_path=lambda _path: 'G-002',
            _worktree_execplan_input_fingerprint=lambda *_args: 'candidate',
            capture_git_snapshot=lambda *_args: capture(),
            project_execplan=lambda connection, repo: runtime.projected_row(repo),
            _final_block_recovery_decision=block_service._final_block_recovery_decision,
        )
        with runtime_state.selected_execution('A', 'G-002'), self.assertRaises(TaskRecheckRequired):
            block_service.block_active_execplan(
                effects, self.connection, 'A', Path('G-002'), 'stale cause', '',
            )

        self.assertEqual(runtime.get('A', 'G-001'), representative)
        blocked = runtime.get('A', 'G-002')
        self.assertEqual(blocked.status, 'IMPLEMENTING')
        self.assertEqual(blocked.worker_result, concurrent_result)
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo='A'").fetchone()), representative_project)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM execplan_blocks WHERE repo='A' AND plan_id='G-002'").fetchone())

    def validation_failure_effects(self, observe):
        from loop_manager_internal.recovery import block_service
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        runtime.update_plan('A', 'G-001', plan_values={'verify_failure_history': json.dumps({
            'failure': {'occurrences': block_service.MAX_VALIDATION_FINGERPRINT_REPEATS}})}, enqueue=False)
        def fingerprints(*args):
            observe()
            return 'input', 'worktree'
        def snapshot(*args):
            observe()
            return SimpleNamespace(serialize=lambda: 'snapshot')
        return SimpleNamespace(active_execplan=lambda connection, repo: runtime.active_row(repo),
            validation_failure_fingerprints=lambda *args: (('failure', 'failure'),),
            redact_secrets=str, _deserialize_failure_history=json.loads,
            _blocked_validation_input_fingerprints=fingerprints, capture_git_snapshot=snapshot,
            _final_block_recovery_decision=block_service._final_block_recovery_decision)

    def test_validation_block_git_observation_releases_database_access(self):
        from loop_manager_internal.recovery import block_service
        observed = []
        def observe():
            self.assertFalse(self.connection.in_transaction)
            self.assertFalse(self.connection._held)
            with closing(database.connect_database(self.directory, run_migrations=False)) as other:
                with other:
                    other.execute("UPDATE projects SET last_error='independent' WHERE repo='B'")
                observed.append(other.execute("SELECT last_error FROM projects WHERE repo='B'").fetchone()[0])
        effects = self.validation_failure_effects(observe)
        with runtime_state.selected_execution('A', 'G-001'):
            decision = block_service.transition_validation_failure(
                effects, self.connection, 'A', Path('one'), 'verify', 'failure')
        self.assertTrue(decision.blocked)
        self.assertEqual(observed, ['independent', 'independent'])
        row = self.connection.execute("SELECT status,verify_failure_history FROM exec_plans WHERE plan_id='G-001'").fetchone()
        self.assertEqual(row['status'], 'PLAN_BLOCKED')
        self.assertEqual(json.loads(row['verify_failure_history'])['failure']['occurrences'], decision.occurrences)

    def test_validation_observation_cannot_overwrite_changed_failure_history(self):
        from loop_manager_internal.recovery import block_service
        changed = json.dumps({'concurrent': {'occurrences': 2}})
        def observe():
            with closing(database.connect_database(self.directory, run_migrations=False)) as other, other:
                other.execute("UPDATE exec_plans SET verify_failure_history=? WHERE repo='A' AND plan_id='G-001'", (changed,))
        effects = self.validation_failure_effects(observe)
        with runtime_state.selected_execution('A', 'G-001'), self.assertRaises(scheduling.TaskRecheckRequired):
            block_service.transition_validation_failure(effects, self.connection, 'A', Path('one'), 'verify', 'failure')
        row = self.connection.execute("SELECT status,verify_failure_history FROM exec_plans WHERE plan_id='G-001'").fetchone()
        self.assertEqual((row['status'], row['verify_failure_history']), ('IMPLEMENTING', changed))

    def test_compatibility_updates_cannot_reactivate_above_management_limit(self):
        from loop_manager_internal.storage import task_calls
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        project_registry.set_task_limit(self.connection, 'A', 1, 3)
        with fast_mode_scope(self.config):
            runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE plan_id='G-002'")
        self.connection.commit()
        with runtime_state.selected_execution('A', 'G-002', include_blocked=True):
            with self.assertRaises(runtime_state.TaskLimitReached):
                runtime.update_project('A', {'phase': 'VERIFYING'}, enqueue=False)
        with self.assertRaises(runtime_state.TaskLimitReached):
            runtime.mirror_project_values('A', runtime.get('A', 'G-002').path, {'phase': 'VERIFYING'})
        self.assertEqual(task_calls.managed_count(self.connection, 'A'), 1)
        self.assertEqual(runtime.get('A', 'G-002').status, 'PLAN_BLOCKED')

    def test_direct_rebase_uses_target_pr_head_instead_of_representative(self):
        from loop_manager_internal.git import direct_rebase_service, manager_git_journal
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        self.connection.execute("UPDATE projects SET pr_head_oid='unrelated' WHERE repo='A'")
        self.connection.execute("UPDATE exec_plans SET pr_head_oid='remote' WHERE repo='A' AND plan_id='G-002'")
        self.connection.commit()
        effects = SimpleNamespace(
            _owned_rebase_state_dir=lambda *args: self.directory,
            _owned_rebase_journal_state=lambda *args: {'head': 'before'},
            _canonical_path=lambda path: Path(path).resolve(),
            remote_branch_oid=lambda *args: 'remote',
            git=mock.Mock(side_effect=[SimpleNamespace(returncode=1, stdout='conflict'),
                                       SimpleNamespace(returncode=0, stdout='')]),
            _finish_owned_direct_rebase=mock.Mock())
        with mock.patch.object(manager_git_journal, 'live_remote_branch_oid', return_value='remote'), \
             mock.patch.object(manager_git_journal, 'begin_current_rebase', return_value='intent'):
            result = direct_rebase_service._journaled_direct_rebase(
                effects, self.connection, self.directory, 'A', 'G-002', self.directory / 'worktree',
                'two', 'base', observed_remote=('remote',))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.connection.execute("SELECT pr_head_oid FROM projects WHERE repo='A'").fetchone()[0],
                         'unrelated')
        execute = self.connection.execute
        def pending(sql, *args, **kwargs):
            if sql.startswith('SELECT pr_head_oid, pr_conflict_head_oid'):
                raise DatabaseWaitTimeout('PR head lookup wait')
            return execute(sql, *args, **kwargs)
        with mock.patch.object(self.connection, 'execute', side_effect=pending), \
             mock.patch.object(manager_git_journal, 'live_remote_branch_oid', return_value='remote'), \
             mock.patch.object(manager_git_journal, 'begin_current_rebase') as begin:
            with self.assertRaises(DatabaseWaitTimeout):
                direct_rebase_service._journaled_direct_rebase(
                    effects, self.connection, self.directory, 'A', 'G-001', self.directory / 'worktree-one',
                    'one', 'base', observed_remote=('remote',))
        begin.assert_not_called()

    def test_direct_rebase_rejects_mismatched_selection_before_observation(self):
        from loop_manager_internal.git import direct_rebase_service, manager_git_journal
        before = tuple(tuple(row) for row in self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo='A' ORDER BY plan_id").fetchall())
        state_dir = mock.Mock(return_value=self.directory)
        effects = SimpleNamespace(_owned_rebase_state_dir=state_dir)
        with runtime_state.selected_execution('A', 'G-001'), \
             self.assertRaises(manager_git_journal.MutationEvidenceError):
            direct_rebase_service._journaled_direct_rebase(
                effects, self.connection, self.directory, 'A', 'G-002',
                self.directory / 'worktree-two', 'two', 'base',
            )
        state_dir.assert_not_called()
        self.assertEqual(tuple(tuple(row) for row in self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo='A' ORDER BY plan_id").fetchall()), before)

    def test_reservation_and_cursor_failure_return_unstarted_capacity_atomically(self):
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from loop_manager_internal.storage import task_calls
        scheduler = self.scheduler(max_workers=1)
        execute = self.connection.execute
        def fail_cursor(sql, *args, **kwargs):
            if 'INSERT INTO task_scheduling_cursor VALUES (?,?)' in sql:
                raise DatabaseWaitTimeout('cursor wait')
            return execute(sql, *args, **kwargs)
        with mock.patch.object(self.connection, 'execute', side_effect=fail_cursor):
            with self.assertRaises(DatabaseWaitTimeout), scheduler.slot(
                    'agent', scheduling.Task('A', 'G-001', self.connection)):
                task_calls.mark_started(self.connection, token=task_calls.current_call.get().token)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        self.assertEqual(scheduler.retained, {})

    def test_reservation_db_wait_does_not_hold_scheduler_condition(self):
        from loop_manager_internal.storage import task_calls
        for boundary in ('reserve', 'eligibility'):
            with self.subTest(boundary=boundary):
                scheduler = self.scheduler(max_workers=1)
                entered, release, progressed = threading.Event(), threading.Event(), threading.Event()
                errors = []
                original = task_calls.reserve if boundary == 'reserve' else scheduler._eligible
                def waiting(*args, **kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('다른 작업의 배정기가 DB 대기 중 멈췄습니다.')
                    return original(*args, **kwargs)
                def run():
                    try:
                        with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                            with scheduler.slot('agent', scheduling.Task('A', 'G-001', connection)):
                                pass
                    except BaseException as error:
                        errors.append(error)
                def progress():
                    # RETRY registration now needs the DB writer too. Observe
                    # the independent scheduler condition directly during that wait.
                    with scheduler.condition:
                        scheduler.condition.notify_all()
                        progressed.set()
                target = task_calls if boundary == 'reserve' else scheduler
                attribute = 'reserve' if boundary == 'reserve' else '_eligible'
                with mock.patch.object(target, attribute, side_effect=waiting):
                    thread = threading.Thread(target=run)
                    other = threading.Thread(target=progress)
                    try:
                        thread.start()
                        self.assertTrue(entered.wait(3))
                        other.start()
                        self.assertTrue(progressed.wait(1))
                        self.assertEqual(sum(scheduler.running['agent'].values()), 1 if boundary == 'reserve' else 0)
                    finally:
                        release.set()
                        thread.join(4)
                        if other.ident is not None:
                            other.join(4)
                self.assertEqual(errors, [])

    def test_scheduled_effect_rechecks_input_after_reservation(self):
        from loop_manager_internal.storage import task_calls
        scheduler = self.scheduler(max_workers=1)
        task = scheduling.Task('A', 'G-001', self.connection)
        reserve = task_calls.reserve
        def changed(*args):
            owner = reserve(*args)
            self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo='A' AND plan_id='G-001'")
            return owner
        effect = mock.Mock()
        call = scheduling.scheduled_call('agent')(effect)
        token = scheduling.current_scheduler.set(scheduler)
        selected = scheduling.current_task.set(task)
        try:
            with mock.patch.object(task_calls, 'reserve', side_effect=changed):
                with self.assertRaises(scheduling.TaskRecheckRequired):
                    call(role_name='worker')
            with mock.patch('loop_manager_internal.recovery.scoped_blocking.project_block', return_value={'cause': 'blocked'}):
                with self.assertRaises(task_calls.CallDeferred):
                    call(role_name='worker')
        finally:
            scheduling.current_task.reset(selected)
            scheduling.current_scheduler.reset(token)
        effect.assert_not_called()
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)

    def test_agent_storage_retry_keeps_result_and_frees_confirmed_process_capacity(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, AgentResult, Usage
        from loop_manager_internal.storage import agent_invocation_state
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from tests.support.fixed_samples import PROJECT_FILES
        from uuid import uuid4
        import shutil
        scheduler = self.scheduler(max_workers=1)
        task = scheduling.Task('A', 'G-001', self.connection)
        runtime = self.directory / 'agent-runtime' / uuid4().hex
        attempt = runtime / 'attempt'
        attempt.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, runtime)
        launch = SimpleNamespace(runtime=runtime, attempt=attempt)
        physical = []
        class Provider:
            def run(_, request):
                request.on_launch_prepared(launch)
                physical.append(True)
                request.on_process_started()
                request.on_process_finished()
                return AgentResult(0, 'original role result', usage=Usage(input_tokens=7))
        finish = agent_invocation_state.InvocationRepository.finish
        attempts = []
        def delayed_finish(repository, *args, **kwargs):
            attempts.append(True)
            if len(attempts) <= 2:
                raise DatabaseWaitTimeout('finish wait')
            return finish(repository, *args, **kwargs)
        sibling = []
        def wait(_seconds):
            self.assertEqual(scheduler.running['agent']['A'], 0)
            with scheduler.slot('agent', scheduling.Task('B', 'G-003', self.connection)):
                sibling.append(True)
                self.assertEqual(scheduler.running['agent']['B'], 1)
        token = scheduling.current_scheduler.set(scheduler)
        selected = scheduling.current_task.set(task)
        try:
            with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                 mock.patch.object(loop_manager, 'agent_provider', return_value=Provider()), \
                 mock.patch.object(agent_invocation_state.InvocationRepository, 'finish', delayed_finish), \
                 mock.patch.object(scheduler.condition, 'wait', side_effect=wait):
                result = loop_manager.run_agent('codex', Path('/worktree'), self.directory, self.config_path,
                    'worker', 'prompt', read_only=False, connection=self.connection,
                    invocation=InvocationContext('A', 'G-001'), resolved_profile=ResolvedProfile('model', 'high'))
        finally:
            scheduling.current_task.reset(selected)
            scheduling.current_scheduler.reset(token)
        self.assertEqual((result.code, result.message, result.usage.input_tokens), (0, 'original role result', 7))
        self.assertEqual((len(physical), len(sibling)), (1, 1))
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM agent_invocations').fetchone()[0], 1)

    def test_delivery_marker_retry_keeps_the_managed_request_and_next_call_runs(self):
        from loop_manager_internal.agents import common_guidance, process_receipt
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, AgentResult
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from tests.support.fixed_samples import PROJECT_FILES
        from uuid import uuid4
        import shutil
        scheduler = self.scheduler(max_workers=1)
        repository = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            repository.activate('A', 'G-001', activation_sha='base', branch='one', worktree='/worktree')
        before = repository.get('A', 'G-001')
        service = ProjectService(SimpleNamespace(activate_ready_execplan=mock.Mock(return_value=None), resume_manual_work=None),
            ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                            True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base', provider='codex')
        runtime = self.directory / 'agent-runtime' / uuid4().hex
        runtime.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, runtime)
        launches, dispatched, prompts, waits = [], [], [], []
        class Provider:
            def run(_, request):
                prompts.append(request.prompt)
                attempt = runtime / str(len(launches))
                attempt.mkdir()
                launch = SimpleNamespace(runtime=runtime, attempt=attempt)
                launches.append(launch)
                message = 'result-' + str(len(launches))
                (attempt / 'last-message.txt').write_text(message, encoding='utf-8')
                request.on_launch_prepared(launch)
                request.on_process_started()
                request.on_process_finished()
                return AgentResult(0, message)
        def execute(child, owned_repository, **options):
            dispatched.append(True)
            def invoke(prompt):
                current = owned_repository.get('A', 'G-001')
                return loop_manager.run_agent('codex', Path('/worktree'), self.directory, self.config_path,
                    'worker', prompt + str(current.last_error or ''), read_only=False,
                    connection=child.identity.connection, invocation=InvocationContext('A', 'G-001'),
                    resolved_profile=ResolvedProfile('model', 'high'))
            first = invoke('first prompt')
            self.assertEqual(first.message, 'result-1')
            second = invoke('next prompt')
            self.assertEqual(second.message, 'result-2')
            self.assertNotEqual(first.isolation_invocation_id, second.isolation_invocation_id)
            self.assertEqual(owned_repository.get('A', 'G-001').last_error, before.last_error)
            owned_repository.transition_from_snapshot(owned_repository.projected('A'),
                plan_values={'status': 'PR_OPEN'}, project_values=runtime_state.clear_project_values())
            return 0
        original_write = process_receipt._write
        acknowledgements = []
        def fail_delivery(path, root):
            if root.get('invocation_observation', {}).get('database_pending') is False:
                acknowledgements.append(True)
                if len(acknowledgements) <= 2:
                    raise OSError('delivery marker unavailable')
            return original_write(path, root)
        def wait(_seconds):
            waits.append(True)
            self.assertEqual(sum(scheduler.running['agent'].values()), 0)
            task = scheduling.current_task.get()
            self.assertEqual(runtime_state.RuntimeStateRepository(task.connection).get('A', 'G-001').last_error,
                             before.last_error)
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with fast_mode_scope(self.config), mock.patch.object(ProjectService, '_dispatch_execution', execute), \
                 mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                 mock.patch.object(loop_manager, 'agent_provider', return_value=Provider()), \
                 mock.patch.object(process_receipt, 'recover', return_value={'finished_at': 'proof'}), \
                 mock.patch.object(process_receipt, '_write', side_effect=fail_delivery), \
                 mock.patch.object(scheduler.condition, 'wait', side_effect=wait):
                self.assertEqual(scheduling.run_project_tasks(service, newly_activated=True), 0)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual((len(dispatched), len(launches), len(waits)), (1, 2, 2))
        self.assertEqual(prompts, ['first prompt', 'next prompt'])
        for row in self.connection.execute('SELECT invocation_id,process_receipt_path FROM agent_invocations'):
            self.assertFalse(process_receipt.observation(row[1], self.directory, row[0])['database_pending'])
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)

    def test_restart_uses_the_worker_session_saved_by_the_start_callback(self):
        from loop_manager_internal.agents import common_guidance, process_receipt
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, AgentResult
        from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
        from tests.support.fixed_samples import PROJECT_FILES
        from uuid import uuid4
        import shutil
        repository = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            repository.activate('A', 'G-001', activation_sha='base', branch='one', worktree='/worktree')
        runtime = self.directory / 'agent-runtime' / uuid4().hex
        attempt = runtime / 'attempt'
        attempt.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, runtime)
        launch = SimpleNamespace(runtime=runtime, attempt=attempt)
        (attempt / 'last-message.txt').write_text('original result', encoding='utf-8')
        class Provider:
            def run(_, request):
                self.assertIsNone(request.session_id)
                request.on_launch_prepared(launch)
                request.on_process_started()
                request.on_session_started('saved-session')
                request.on_process_finished()
                return AgentResult(0, 'original result', 'saved-session')
        def save_session(session):
            repository.observe_worker_session(repository.get('A', 'G-001'), None, session, 'binding')
        def invoke(connection, session, callback=None):
            return loop_manager.run_agent('codex', Path('/worktree'), self.directory, self.config_path,
                'worker', 'original prompt', read_only=False, connection=connection, session_id=session,
                on_session_started=callback, invocation=InvocationContext('A', 'G-001'),
                resolved_profile=ResolvedProfile('model', 'high'))
        with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
             mock.patch.object(process_receipt, 'recover', return_value={'finished_at': 'proof'}):
            with mock.patch.object(loop_manager, 'agent_provider', return_value=Provider()), \
                 mock.patch.object(InvocationRepository, 'finish', side_effect=KeyboardInterrupt('restart')):
                with self.assertRaises(KeyboardInterrupt):
                    invoke(self.connection, repository.get('A', 'G-001').worker_session_id, save_session)
            row = self.connection.execute('SELECT invocation_id,process_receipt_path FROM agent_invocations').fetchone()
            saved = process_receipt.observation(row[1], self.directory, row[0])
            self.assertIsNone(saved['request_session_id'])
            self.assertTrue(saved['database_pending'])
            with closing(database.connect_database(self.directory, run_migrations=False)) as reopened:
                restarted = runtime_state.RuntimeStateRepository(reopened).get('A', 'G-001')
                self.assertEqual(restarted.worker_session_id, 'saved-session')
                provider = mock.Mock()
                provider.run.side_effect = AssertionError('completed provider must not run after restart')
                with mock.patch.object(loop_manager, 'agent_provider', return_value=provider):
                    recovered = invoke(reopened, restarted.worker_session_id)
                provider.run.assert_not_called()
                self.assertEqual((recovered.message, recovered.session_id, recovered.isolation_invocation_id),
                                 ('original result', 'saved-session', row[0]))
                self.assertEqual(reopened.execute('SELECT COUNT(*) FROM agent_invocations').fetchone()[0], 1)
                self.assertFalse(process_receipt.observation(row[1], self.directory, row[0])['database_pending'])

    def test_scheduled_agent_preparation_wait_retries_without_a_provider_failure(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, AgentResult
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from tests.support.fixed_samples import PROJECT_FILES
        scheduler = self.scheduler(max_workers=1)
        task = scheduling.Task('A', 'G-001', self.connection)
        context = InvocationContext('A', 'G-001')
        class Provider:
            def run(_, request):
                request.on_process_started()
                request.on_process_finished()
                return AgentResult(0, 'original role result')
        provider = mock.Mock(wraps=Provider())
        phase = ['prepare']
        def prepare(*_args):
            if phase[0] == 'prepare':
                phase[0] = 'waiting'
                raise DatabaseWaitTimeout('prepare wait')
            phase[0] = 'ready'
            return context
        def observe():
            self.assertNotEqual(phase[0], 'waiting', 'runtime remote observation during DB wait')
        token = scheduling.current_scheduler.set(scheduler)
        selected = scheduling.current_task.set(task)
        try:
            with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                 mock.patch.object(common_guidance, 'independent_context', side_effect=prepare), \
                 mock.patch.object(loop_manager, 'agent_provider', return_value=provider), \
                 mock.patch.object(scheduling, 'observe_runtime', side_effect=observe), \
                 mock.patch('loop_manager_internal.runtime.handoff_gate.observe_runtime', side_effect=observe), \
                 mock.patch.object(scheduler.condition, 'wait'):
                result = loop_manager.run_agent('codex', Path('/worktree'), self.directory, self.config_path,
                    'worker', 'prompt', read_only=False, connection=self.connection,
                    invocation=context, resolved_profile=ResolvedProfile('model', 'high'))
        finally:
            scheduling.current_task.reset(selected)
            scheduling.current_scheduler.reset(token)
        self.assertEqual(result.message, 'original role result')
        provider.run.assert_called_once()
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM agent_invocations').fetchone()[0], 1)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM recovery_records').fetchone()[0], 0)
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)

    def test_restart_separates_stopped_storage_wait_from_an_unknown_agent_exit(self):
        from dataclasses import asdict
        from loop_manager_internal.agents import process_receipt
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
        from loop_manager_internal.storage import task_calls, agent_invocation_state
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from uuid import uuid4
        import shutil
        self.connection.commit()
        calls = agent_invocation_state.InvocationRepository(self.connection)
        runtime = self.directory / 'agent-runtime' / uuid4().hex
        runtime.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, runtime)
        timestamp = '2026-10-24T12:00:00+00:00'
        for repo, plan, stopped in (('A', 'G-001', True), ('B', 'G-003', False)):
            identity = calls.start(InvocationContext(repo, plan), 'worker', 'codex', ResolvedProfile('model', 'high'), None)
            calls.mark_execution_started(identity)
            if stopped:
                calls.mark_execution_finished(identity, 1)
            receipt = runtime / (plan + '.process.json')
            calls.bind_process_receipt(identity, str(receipt))
            facts = dict(started_at=timestamp, database_pending=True, outcome=dict(kind='success', code=0,
                    duration_ms=1, session_id=None, usage=asdict(Usage()), collection_status='unknown', observed_at=timestamp))
            if stopped:
                facts.update(finished_at=timestamp, duration_ms=1)
            process_receipt.observe(receipt, identity, facts)
            owner = task_calls.reserve(self.connection, self.directory, repo, plan, 'agent', 'run')
            with self.connection:
                self.connection.execute('UPDATE task_call_reservations SET invocation_id=?, receipt_path=? WHERE token=?',
                                        (identity, str(receipt), owner.token))
        with mock.patch.object(agent_invocation_state.InvocationRepository, 'finish', side_effect=DatabaseWaitTimeout('pending')):
            scheduler = self.scheduler(max_workers=2)
        self.assertEqual((scheduler.running['agent']['A'], scheduler.running['agent']['B'], scheduler.uncertain['agent']), (0, 1, 1))
        self.assertTrue(scheduler.retained[('A', 'G-001', 'agent')]['storage_only'])
        self.assertFalse(scheduler.retained[('B', 'G-003', 'agent')]['storage_only'])
        self.assertTrue(scheduler.wait_existing_call(self.connection, 'A', 'G-001'))
        self.assertEqual(scheduler.running['agent']['A'], 0)
        self.assertIn(('B', 'G-003', 'agent'), scheduler.retained)
        process_receipt.finished(SimpleNamespace(runtime=runtime, attempt=runtime / 'G-003'), SimpleNamespace(pid=1234))
        finish = agent_invocation_state.InvocationRepository.finish
        busy = [True]
        def store(repository, *args, **kwargs):
            if busy[0]:
                raise DatabaseWaitTimeout('still pending')
            return finish(repository, *args, **kwargs)
        def healed(_seconds):
            self.assertEqual((scheduler.running['agent']['B'], scheduler.uncertain['agent']), (0, 0))
            busy[0] = False
        with mock.patch.object(agent_invocation_state.InvocationRepository, 'finish', store), \
             mock.patch.object(scheduler.condition, 'wait', side_effect=healed):
            self.assertTrue(scheduler.wait_existing_call(self.connection, 'B', 'G-003'))
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)

    def test_slot_cleanup_database_wait_does_not_repeat_a_returned_agent_call(self):
        from loop_manager_internal.storage import task_calls
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        scheduler = self.scheduler(max_workers=1)
        task = scheduling.Task('A', 'G-001', self.connection)
        result = object()
        provider = mock.Mock(return_value=result)
        call = scheduling.scheduled_call('agent')(provider)
        token = scheduling.current_scheduler.set(scheduler)
        selected = scheduling.current_task.set(task)
        try:
            with mock.patch.object(task_calls, 'release', side_effect=DatabaseWaitTimeout('release wait')), \
                 mock.patch.object(scheduler.condition, 'wait'):
                self.assertIs(call(role_name='worker'), result)
        finally:
            scheduling.current_task.reset(selected)
            scheduling.current_scheduler.reset(token)
        provider.assert_called_once()
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)

    def test_preparation_db_retry_rechecks_the_original_task_input(self):
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        scheduler = self.scheduler(max_workers=1)
        task = scheduling.Task('A', 'G-001', self.connection)
        prepare = mock.Mock(side_effect=DatabaseWaitTimeout('preparation wait'))
        call = scheduling.scheduled_call('agent')(prepare)
        def changed(_seconds):
            with self.connection:
                self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo='A' AND plan_id='G-001'")
        token = scheduling.current_scheduler.set(scheduler)
        selected = scheduling.current_task.set(task)
        try:
            with mock.patch.object(scheduler.condition, 'wait', side_effect=changed):
                with self.assertRaises(scheduling.TaskRecheckRequired):
                    call(role_name='worker')
        finally:
            scheduling.current_task.reset(selected)
            scheduling.current_scheduler.reset(token)
        prepare.assert_called_once()
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)

    def test_slot_cleanup_sql_error_keeps_the_original_agent_exception(self):
        from loop_manager_internal.storage import task_calls
        scheduler = self.scheduler(max_workers=1)
        task = scheduling.Task('A', 'G-001', self.connection)
        original = OSError('original provider exception')
        provider = mock.Mock(side_effect=original)
        call = scheduling.scheduled_call('agent')(provider)
        token = scheduling.current_scheduler.set(scheduler)
        selected = scheduling.current_task.set(task)
        try:
            with mock.patch.object(task_calls, 'release', side_effect=sqlite3.OperationalError('storage error')):
                with self.assertRaises(OSError) as raised:
                    call(role_name='worker')
            self.assertIs(raised.exception, original)
            self.assertTrue(scheduler.wait_existing_call(self.connection, 'A', 'G-001'))
        finally:
            scheduling.current_task.reset(selected)
            scheduling.current_scheduler.reset(token)
        provider.assert_called_once()
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)

    def test_planner_request_retries_storage_without_repeating_the_provider(self):
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, AgentResult
        from loop_manager_internal.storage import agent_invocation_state
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from tests.support.fixed_samples import PROJECT_FILES
        scheduler = self.scheduler(max_workers=1)
        context = InvocationContext('A', None)
        class Provider:
            def run(_, request):
                request.on_process_started()
                request.on_process_finished()
                return AgentResult(0, 'original planner result')
        provider = mock.Mock(wraps=Provider())
        finish = agent_invocation_state.InvocationRepository.finish
        attempts = []
        def delayed(repository, *args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise DatabaseWaitTimeout('finish wait')
            return finish(repository, *args, **kwargs)
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                 mock.patch.object(common_guidance, 'independent_context', side_effect=[DatabaseWaitTimeout('prepare wait'), context]), \
                 mock.patch.object(loop_manager, 'agent_provider', return_value=provider), \
                 mock.patch.object(agent_invocation_state.InvocationRepository, 'finish', delayed), \
                 mock.patch.object(scheduler.condition, 'wait'):
                result = loop_manager.run_agent('codex', Path('/worktree'), self.directory, self.config_path,
                    'planner', 'prompt', read_only=False, connection=self.connection,
                    invocation=context, resolved_profile=ResolvedProfile('model', 'high'))
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(result.message, 'original planner result')
        provider.run.assert_called_once()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)

    def test_automatic_fetch_waits_and_manual_fetch_still_rejects_contention(self):
        from loop_manager_internal.runtime.resource_lock import resource_locks
        from loop_manager_internal.runtime.contracts import ProcessLockBusyError
        scheduler = self.scheduler()
        scheduler.pause_project('A')
        waiting, fetched = threading.Event(), threading.Event()
        errors = []
        def automatic():
            token = scheduling.current_scheduler.set(scheduler)
            try:
                with resource_locks(self.directory, 'A', ['git:fetch'], owner='automatic'):
                    fetched.set()
            except BaseException as error:
                errors.append(error)
            finally:
                scheduling.current_scheduler.reset(token)
        emit = scheduling.events.emit
        def observe(message, **values):
            if message == '공유 Git 자원 배정 대기':
                waiting.set()
            emit(message, **values)
        thread = threading.Thread(target=automatic)
        try:
            with mock.patch.object(scheduling.events, 'emit', side_effect=observe):
                with resource_locks(self.directory, 'A', ['git:fetch'], owner='existing'):
                    with self.assertRaises(ProcessLockBusyError):
                        with resource_locks(self.directory, 'A', ['git:fetch'], owner='manual'):
                            pass
                    thread.start()
                    self.assertTrue(waiting.wait(2))
                    self.assertFalse(fetched.is_set())
                    scheduler.requeue(scheduling.Task('B', 'G-003', self.connection))
                self.assertTrue(fetched.wait(2))
        finally:
            if thread.ident is not None:
                thread.join(3)
        self.assertEqual(errors, [])

    def test_git_fifo_serializes_archive_with_mutations_but_keeps_other_projects_free(self):
        from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
        scheduler = self.scheduler()
        entered, errors, threads = [], [], []
        other = threading.Event()
        def request(repo, owner, resource):
            token = scheduling.current_scheduler.set(scheduler)
            try:
                with resource_locks(self.directory, repo, [resource], owner=owner, wait=False):
                    self.assertTrue(resource_owned(self.directory, repo, 'git:project'))
                    # A nested Git operation shares the turn, including another owner.
                    with resource_locks(self.directory, repo, ['git:nested'], owner='nested'):
                        entered.append(owner)
                    if repo == 'B':
                        other.set()
            except BaseException as error:
                errors.append(error)
            finally:
                scheduling.current_scheduler.reset(token)
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with resource_locks(self.directory, 'A', ['git:fetch'], owner='active'):
                for number, owner, resource in ((2, 'execplan-archive', 'git:config'),
                                                (3, 'next-fetch', 'git:fetch')):
                    thread = threading.Thread(target=request, args=('A', owner, resource))
                    threads.append(thread)
                    thread.start()
                    with scheduler.condition:
                        self.assertTrue(scheduler.condition.wait_for(
                            lambda: len(scheduler.git_waiters.get('A', ())) == number, 3))
                thread = threading.Thread(target=request, args=('B', 'other-project', 'git:fetch'))
                threads.append(thread)
                thread.start()
                self.assertTrue(other.wait(3))
                self.assertEqual(entered, ['other-project'])
        finally:
            scheduling.current_scheduler.reset(token)
            for thread in threads:
                thread.join(4)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(entered, ['other-project', 'execplan-archive', 'next-fetch'])
        self.assertEqual(scheduler.git_waiters, {})

    def test_direct_git_writes_join_turn_but_read_only_queries_do_not(self):
        from loop_manager_internal.runtime.resource_lock import resource_locks, resource_owned
        from loop_manager_internal.git.command_adapter import GitEffects, git
        scheduler = self.scheduler()
        commands, errors = [], []
        def command(arguments, **_kwargs):
            if any(verb in arguments for verb in ('push', 'update-index', 'branch')):
                self.assertTrue(resource_owned(self.directory, 'A', 'git:project'))
            commands.append(arguments)
            return SimpleNamespace(returncode=0, stdout='')
        effects = GitEffects(mock.Mock(return_value=[]), mock.Mock(return_value=None),
                             command, mock.Mock(return_value=False))
        def write():
            token = scheduling.current_scheduler.set(scheduler)
            task = scheduling.current_task.set(scheduling.Task('A', 'G-001', self.connection))
            try:
                git(effects, Path('/owned-worktree'), 'push', 'origin', 'HEAD')
                git(effects, Path('/owned-worktree'), '--literal-pathspecs', 'update-index', '--refresh')
                git(effects, Path('/owned-worktree'), 'branch', '-r', '-d', 'origin/retired')
            except BaseException as error:
                errors.append(error)
            finally:
                scheduling.current_task.reset(task)
                scheduling.current_scheduler.reset(token)
        token = scheduling.current_scheduler.set(scheduler)
        thread = threading.Thread(target=write)
        try:
            with resource_locks(self.directory, 'A', ['git:fetch'], owner='active'):
                thread.start()
                with scheduler.condition:
                    self.assertTrue(scheduler.condition.wait_for(
                        lambda: len(scheduler.git_waiters.get('A', ())) == 2, 3))
                git(effects, Path('/owned-worktree'), 'config', '--get', 'user.email')
                self.assertEqual(len(commands), 1)
        finally:
            scheduling.current_scheduler.reset(token)
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(commands), 4)
        self.assertEqual(scheduler.git_waiters, {})

    def test_git_fifo_rejects_database_transaction_without_leaking_a_turn(self):
        from loop_manager_internal.runtime.resource_lock import resource_locks
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        scheduler = self.scheduler()
        token = scheduling.current_scheduler.set(scheduler)
        task = scheduling.current_task.set(scheduling.Task('A', 'G-001', self.connection))
        try:
            with self.connection:
                self.connection.execute("UPDATE projects SET processed_sha='held' WHERE repo='A'")
                with self.assertRaises(DatabaseWaitTimeout):
                    with resource_locks(self.directory, 'A', ['git:fetch'], owner='task-fetch'):
                        self.fail('Git ran inside a database transaction')
                self.assertTrue(self.connection.in_transaction)
            self.assertEqual(scheduler.git_waiters, {})
        finally:
            scheduling.current_task.reset(task)
            scheduling.current_scheduler.reset(token)

    def test_git_fifo_cancellation_removes_waiter_and_keeps_project_fetch_eligible(self):
        from loop_manager_internal.runtime.resource_lock import resource_locks
        scheduler = self.scheduler()
        rejected, errors = threading.Event(), []
        def queued():
            scheduler_token = scheduling.current_scheduler.set(scheduler)
            task_token = scheduling.current_task.set(scheduling.Task('A', 'G-001', self.connection))
            try:
                with resource_locks(self.directory, 'A', ['git:config'], owner='task-write'):
                    self.fail('cancelled Git request ran')
            except scheduling.PlanningDrain:
                rejected.set()
            except BaseException as error:
                errors.append(error)
            finally:
                scheduling.current_task.reset(task_token)
                scheduling.current_scheduler.reset(scheduler_token)
        token = scheduling.current_scheduler.set(scheduler)
        thread = threading.Thread(target=queued)
        try:
            with resource_locks(self.directory, 'A', ['git:fetch'], owner='base-fetch'):
                thread.start()
                with scheduler.condition:
                    self.assertTrue(scheduler.condition.wait_for(
                        lambda: len(scheduler.git_waiters.get('A', ())) == 2, 3))
                scheduler.pause_project('A')
                self.assertTrue(rejected.wait(3))
            with resource_locks(self.directory, 'A', ['git:fetch'], owner='base-fetch'):
                pass
        finally:
            scheduling.current_scheduler.reset(token)
            thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(scheduler.git_waiters, {})

    def test_planning_drain_allows_project_fetch_but_keeps_task_and_handoff_gates(self):
        from loop_manager_internal.runtime.resource_lock import resource_locks
        from loop_manager_internal.runtime.handoff_gate import RuntimeHandoffGate, _runtime_handoff_signal, SelfMergeRuntimeHandoff
        scheduler = self.scheduler()
        scheduler.pause_project('A')
        token = scheduling.current_scheduler.set(scheduler)
        task = scheduling.current_task.set(scheduling.Task('A', 'G-001', self.connection))
        gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
        handoff = _runtime_handoff_signal.set(gate)
        try:
            with resource_locks(self.directory, 'A', ['git:fetch'], owner='base-fetch'):
                self.assertIn('A', scheduler.draining_projects)
            waiting, fetched = threading.Event(), threading.Event()
            errors = []
            def queued_fetch():
                task_token = scheduling.current_task.set(
                    scheduling.Task('A', 'G-001', self.connection))
                scheduler_token = scheduling.current_scheduler.set(scheduler)
                try:
                    with resource_locks(self.directory, 'A', ['git:fetch'], owner='base-fetch'):
                        fetched.set()
                except BaseException as error:
                    errors.append(error)
                finally:
                    scheduling.current_scheduler.reset(scheduler_token)
                    scheduling.current_task.reset(task_token)
            emit = scheduling.events.emit
            def observe(message, **values):
                if message == '공유 Git 자원 배정 대기':
                    waiting.set()
                emit(message, **values)
            thread = threading.Thread(target=queued_fetch)
            try:
                with mock.patch.object(scheduling.events, 'emit', side_effect=observe):
                    with resource_locks(self.directory, 'A', ['git:fetch'], owner='base-fetch'):
                        thread.start()
                        self.assertTrue(waiting.wait(2))
                        self.assertFalse(fetched.is_set())
                    self.assertTrue(fetched.wait(2))
            finally:
                if thread.ident is not None:
                    thread.join(3)
            self.assertEqual(errors, [])
            with self.assertRaises(scheduling.PlanningDrain), scheduler.task('A', 'G-001', self.connection):
                self.fail('Planner 대기 중 작업이 재개됐습니다.')
            gate.stop()
            with self.assertRaises(SelfMergeRuntimeHandoff), resource_locks(
                    self.directory, 'A', ['git:fetch'], owner='base-fetch'):
                self.fail('runtime 교체 중 프로젝트 fetch가 시작됐습니다.')
        finally:
            _runtime_handoff_signal.reset(handoff)
            scheduling.current_task.reset(task)
            scheduling.current_scheduler.reset(token)

    def test_project_recovery_admission_preserves_exact_active_owner_and_planning_gate(self):
        from loop_manager_internal.recovery.plan_recovery import recovery_admission
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        with self.connection:
            self.connection.execute("INSERT INTO project_blocks(repo,plan_id,scope,reason_code,evidence) "
                                    "VALUES ('A','G-001','project','repository_state_unsafe','original cause')")
        before = dict(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone())
        scheduler = self.scheduler()
        token = scheduling.current_scheduler.set(scheduler)
        calls = []
        @scheduling.scheduled_call('agent')
        def reviewer(**_options):
            calls.append('reviewer')
        @scheduling.scheduled_call('verify')
        def verify():
            calls.append('verify')
        try:
            with self.assertRaises(scheduling.SchedulingDeferred), scheduler.admission(
                    scheduling.Task('A', 'G-001', self.connection)):
                self.fail('일반 배정이 프로젝트 차단을 우회했습니다.')
            with self.assertRaises(scheduling.SchedulingDeferred), recovery_admission(self.connection, 'A', 'G-002'):
                self.fail('다른 계획이 프로젝트 차단의 복구 자리를 얻었습니다.')
            with recovery_admission(self.connection, 'A', 'G-001'), runtime_state._transaction(self.connection):
                self.assertEqual(runtime.get('A', 'G-001').status, 'IMPLEMENTING')
                self.assertEqual(scheduling.managed_count(self.connection, 'A'), 1)
            with recovery_admission(self.connection, 'A', 'G-001'):
                selected = scheduling.current_task.set(scheduling.Task('A', 'G-001', self.connection, recovery=True))
                try:
                    reviewer(role_name='reviewer')
                    verify()
                finally:
                    scheduling.current_task.reset(selected)
            self.assertEqual(calls, ['reviewer', 'verify'])
            with self.connection:
                self.connection.execute("UPDATE projects SET planning_resume_plan_id='G-001' WHERE repo='A'")
            with self.assertRaises(scheduling.SchedulingDeferred), recovery_admission(self.connection, 'A', 'G-001'):
                self.fail('복구가 planning 소유권을 우회했습니다.')
            with self.connection:
                self.connection.execute("UPDATE projects SET planning_resume_plan_id=NULL WHERE repo='A'")
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(dict(self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone()), before)
        block = self.connection.execute("SELECT plan_id,evidence FROM project_blocks WHERE repo='A'").fetchone()
        self.assertEqual(tuple(block), ('G-001', 'original cause'))
        self.assertFalse(scheduler.admitting)

    def test_blocked_recovery_uses_scheduler_for_review_verify_and_reactivation(self):
        from loop_manager_internal.recovery import plan_recovery, scoped_blocking
        from loop_manager_internal.recovery.blocked_plan_service import (
            RequeueBlockedExecplanEffects, _requeue_blocked_execplan,
        )
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED',pr_conflict_recovery=1 "
                                    "WHERE repo='A' AND plan_id='G-001'")
            self.connection.execute("INSERT INTO execplan_blocks(repo,plan_id,scope,reason_code,evidence) "
                                    "VALUES ('A','G-001','plan','user_decision_required','preserved question')")
        scheduler = self.scheduler(max_workers=1)
        scheduler.priority_reader = lambda connection, repo: SimpleNamespace(priority_ranks=())
        token = scheduling.current_scheduler.set(scheduler)
        calls = []
        @scheduling.scheduled_call('agent')
        def reviewer(**_options):
            calls.append('reviewer')
        @scheduling.scheduled_call('verify')
        def verify():
            calls.append('verify')
        try:
            normal = scheduling.Task('A', 'G-001', self.connection)
            with self.assertRaises(scheduling.SchedulingDeferred), scheduler.slot('agent', normal):
                self.fail('일반 호출이 차단을 우회했습니다.')
            with plan_recovery.recovering_plan(self.connection, self.directory, 'A', 'G-001') as owned:
                self.assertTrue(owned)
                for change, restore in (
                    ("INSERT INTO project_blocks(repo,scope,reason_code,evidence) VALUES ('A','project','repository_state_unsafe','unsafe')",
                     "DELETE FROM project_blocks WHERE repo='A'"),
                    ("UPDATE projects SET phase='PLANNING' WHERE repo='A'",
                     "UPDATE projects SET phase=NULL WHERE repo='A'"),
                    ("UPDATE exec_plans SET depends_on='[\"G-002\"]' WHERE repo='A' AND plan_id='G-001'",
                     "UPDATE exec_plans SET depends_on='[]' WHERE repo='A' AND plan_id='G-001'"),
                ):
                    with self.subTest(gate=change):
                        with self.connection:
                            self.connection.execute(change)
                        with self.assertRaises(scheduling.SchedulingDeferred):
                            verify()
                        self.assertEqual(calls, [])
                        with self.connection:
                            self.connection.execute(restore)
                reviewer(role_name='reviewer')
                verify()
                self.assertIsNotNone(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
                _requeue_blocked_execplan(RequeueBlockedExecplanEffects(loop_manager._clear_active_values),
                    self.connection, 'A', 'G-001')
            self.assertIsNone(scheduling.current_task.get())
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(calls, ['reviewer', 'verify'])
        self.assertEqual(runtime_state.RuntimeStateRepository(self.connection).get('A', 'G-001').status,
                         'IMPLEMENTING')
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        self.assertEqual(sum(scheduler.running['verify'].values()), 0)
        self.assertFalse(scheduler.admitting)

    def test_waiting_calls_recheck_current_gates_and_candidate_before_start(self):
        from loop_manager_internal.storage import execplan_draft_state
        for change in ('project', 'catalog', 'draft', 'dependency', 'manual', 'maintenance', 'phase', 'candidate', 'ownership'):
            with self.subTest(change=change):
                self._reset_catalog()
                scheduler = self.scheduler(max_workers=1)
                scheduler.running['agent']['B'] = 1
                errors, calls = [], []
                def queued():
                    try:
                        with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                            with scheduler.slot('agent', scheduling.Task('A', 'G-001', connection)):
                                calls.append('stale')
                    except BaseException as error:
                        errors.append(error)
                thread = threading.Thread(target=queued)
                thread.start()
                with scheduler.condition:
                    self.assertTrue(scheduler.condition.wait_for(lambda:len(scheduler.waiters['agent']) == 1, 3))
                    if change == 'project':
                        self.connection.execute("INSERT INTO project_blocks(repo,scope,reason_code,evidence) "
                                                "VALUES ('A','project','repository_state_unsafe','unsafe')")
                    elif change == 'catalog':
                        self.connection.execute("INSERT INTO execplan_catalog_holds(repo,plan_id,revision_sha,evidence) "
                                                "VALUES ('A','G-001','base','{}')")
                    elif change == 'maintenance':
                        self.connection.execute("INSERT INTO maintenance_cycles(repo,phase,refactoring_plan_ids) "
                                                "VALUES ('A','refactoring','[\"G-002\"]')")
                    elif change == 'draft':
                        execplan_draft_state.ensure_schema(self.connection)
                        self.connection.execute("INSERT INTO execplan_drafts VALUES ('A','G-001',1,'{}')")
                    else:
                        field, value = {'dependency': ('depends_on', '["G-002"]'),
                                        'manual': ('status', 'MANUAL_WORKING'),
                                        'phase': ('status', 'VERIFYING'),
                                        'candidate': ('active_sha', 'new-head'),
                                        'ownership': ('worktree', 'new-owner')}[change]
                        self.connection.execute(f"UPDATE exec_plans SET {field}=? WHERE repo='A' AND plan_id='G-001'",
                                                (value,))
                    self.connection.commit()
                    scheduler.running['agent']['B'] = 0
                    scheduler.condition.notify_all()
                thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(calls, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], scheduling.SchedulingDeferred if change in {
                    'project', 'catalog', 'draft', 'dependency', 'manual', 'maintenance'} else scheduling.SchedulingChanged)
                with scheduler.slot('agent', scheduling.Task('B', 'G-003', self.connection)):
                    pass
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)

    def test_waiting_queue_refreshes_manual_acceptance_and_skips_ineligible_head(self):
        scheduler = self.scheduler(max_workers=1)
        scheduler.running['agent']['A'] = 1
        order, errors, threads = [], [], []
        def queued(repo, plan_id):
            try:
                with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                    with scheduler.slot('agent', scheduling.Task(repo, plan_id, connection)):
                        order.append(plan_id)
            except BaseException as error:
                errors.append(error)
        for repo, plan_id in (('A', 'G-001'), ('A', 'G-002'), ('B', 'G-003')):
            thread = threading.Thread(target=queued, args=(repo, plan_id))
            threads.append(thread)
            thread.start()
        with scheduler.condition:
            self.assertTrue(scheduler.condition.wait_for(lambda:len(scheduler.waiters['agent']) == 3, 3))
            # 수락은 기존 행의 후보·실행 단계를 바꾸지 않지만 배정 순위는 바꾼다.
            from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
            store = ManualWorkRepository(self.connection)
            group = store.create('B', 'existing', 'manual', [], 'head', 'base', members=('G-003',))
            store.update(group, owner_id='G-003', state='AUTOMATIC', request_id='accepted')
            self.connection.execute("INSERT INTO manual_work_requests(request_id,repo,group_id,source_head,base_sha,"
                "input_token,skip_verify,skip_review,reason,created_at) VALUES ('accepted','B',?,'head','base','input',0,0,'','2026-10-16')",
                (group['group_id'],))
            self.connection.execute("UPDATE exec_plans SET status='MANUAL_WORKING' WHERE plan_id='G-001'")
            self.connection.commit()
            scheduler.running['agent']['A'] = 0
            scheduler.condition.notify_all()
        for thread in threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())
        # 새 수락은 재준비하고, 실행 불가 선두는 적격 G-002를 막지 않는다.
        self.assertEqual(order, ['G-002'])
        self.assertCountEqual([type(error) for error in errors],
                              [scheduling.SchedulingDeferred, scheduling.SchedulingChanged])
        with scheduler.slot('agent', scheduling.Task('B', 'G-003', self.connection)):
            order.append('G-003')
        self.assertEqual(order, ['G-002', 'G-003'])

    def test_activation_reselects_after_management_wait(self):
        for change in ('priority', 'catalog'):
            with self.subTest(change=change):
                self._reset_catalog()
                scheduler = self.scheduler()
                scheduler.admitting = True
                result, errors = [], []
                def activate():
                    token = scheduling.current_scheduler.set(scheduler)
                    try:
                        with closing(database.connect_database(self.directory, run_migrations=False)) as connection, \
                             fast_mode_scope(self.config):
                            result.append(loop_manager.activate_ready_execplan(connection, 'A', self.directory, 'base'))
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        scheduling.current_scheduler.reset(token)
                thread = threading.Thread(target=activate)
                thread.start()
                with scheduler.condition:
                    self.assertTrue(scheduler.condition.wait_for(lambda:bool(scheduler.waiters['management']), 3))
                    if change == 'priority':
                        self.connection.execute("UPDATE exec_plans SET priority='critical' WHERE plan_id='G-002'")
                    else:
                        self.connection.execute("INSERT INTO execplan_catalog_holds(repo,plan_id,revision_sha,evidence) "
                                                "VALUES ('A','G-001','base','{}')")
                    self.connection.commit()
                    scheduler.admitting = False
                    scheduler.condition.notify_all()
                thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(result[0]['plan_id'], 'G-002')
                self.assertEqual(runtime_state.RuntimeStateRepository(self.connection).get('A', 'G-001').status, 'READY')

    def test_prepared_call_input_change_releases_unstarted_slot_and_reprepares(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.execution.execution_state import ExecutionStatePort
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
        scheduler = self.scheduler()
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        calls, outcomes = [], []
        @scheduling.scheduled_call('verify')
        def verify():
            calls.append('verify')
        with runtime_state.selected_execution('A', 'G-001'):
            state = ExecutionStatePort(self.connection, runtime, 'A')
        state.call_inputs = mock.Mock(side_effect=['prepared', 'changed'])
        coordinator = mock.Mock()
        coordinator.prepare.return_value = object()
        coordinator.dispatch.side_effect = lambda _context: state.bind(verify, name='verify')()
        service = ProjectService(SimpleNamespace(_ExecutionLifecyclePorts=mock.Mock(),
            ExecutionPhaseCoordinator=mock.Mock(return_value=coordinator), _check_runtime_handoff=mock.Mock(),
            operation_print=mock.Mock(), _execution_outcome_step_result=lambda result:outcomes.append(result.kind)),
            ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                            True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base')
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with scheduler.task('A', 'G-001', self.connection, resumed=False):
                service._dispatch_execution(runtime, newly_activated=False)
                self.assertFalse(scheduling.current_task.get().resumed)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(outcomes, [ExecutionOutcomeKind.RETRY])
        self.assertEqual(calls, [])
        self.assertNotIn('verify', scheduler.cursor)
        self.assertEqual(sum(scheduler.running['verify'].values()), 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)
        self.assertIsNone(scheduling.call_revalidation.get())

    def test_lifecycle_rechecks_waited_git_inputs_without_rejecting_sibling_refs(self):
        from loop_manager_internal.agents.scope_contracts import GitSnapshot
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree=str(self.directory / 'one'))
        for change in ('candidate', 'remote', 'sibling', 'contention'):
            with self.subTest(change=change):
                scheduler = self.scheduler(verify_concurrency=1)
                snapshot = [GitSnapshot('head', 'one', {}, {}, {}, {}, {}, {})]
                base = ['base']
                calls, errors, commands = [], [], []
                git_waiting = threading.Event()
                emit = scheduling.events.emit
                def observe(message, **values):
                    if message == '공유 Git 자원 배정 대기':
                        git_waiting.set()
                    emit(message, **values)
                def git(_path, *args, **_options):
                    self.assertFalse(self.connection.in_transaction)
                    if args == ('fetch', 'origin'):
                        self.assertEqual(sum(scheduler.running['verify'].values()), 0)
                        self.assertEqual(scheduling.current_task.get().connection.execute(
                            "SELECT COUNT(*) FROM task_call_reservations WHERE repo='A'").fetchone()[0], 0)
                    commands.append(args)
                    return SimpleNamespace(returncode=0, stdout=base[0])
                @scheduling.scheduled_call('verify')
                def verify():
                    calls.append('verify')
                def queued():
                    token = scheduling.current_scheduler.set(scheduler)
                    try:
                        with closing(database.connect_database(self.directory, run_migrations=False)) as connection, \
                             scheduler.task('A', 'G-001', connection):
                            ports = loop_manager._ExecutionLifecyclePorts(connection,
                                runtime_state.RuntimeStateRepository(connection), repo='A', repo_path=Path('A'),
                                state_dir=self.directory, base='main', base_sha='base', provider='codex',
                                config_path=self.config_path, newly_activated=False)
                            ports.effects.state.bind(verify, name='verify')()
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        scheduling.current_scheduler.reset(token)
                from contextlib import ExitStack
                from loop_manager_internal.runtime.resource_lock import resource_locks
                with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda _path:snapshot[0]), \
                     mock.patch.object(loop_manager, 'git', side_effect=git), \
                     mock.patch.object(scheduling.events, 'emit', side_effect=observe):
                    with ExitStack() as leases:
                        if change == 'contention':
                            leases.enter_context(resource_locks(self.directory, str(Path('A').resolve()),
                                ['git:fetch'], owner='existing-fetch'))
                        with scheduler.slot('verify', scheduling.Task('B', 'G-003', self.connection)):
                            thread = threading.Thread(target=queued)
                            thread.start()
                            with scheduler.condition:
                                self.assertTrue(scheduler.condition.wait_for(lambda:bool(scheduler.waiters['verify']), 3))
                                if change == 'candidate':
                                    snapshot[0] = replace(snapshot[0], head='new-head')
                                elif change == 'remote':
                                    base[0] = 'new-base'
                                elif change == 'sibling':
                                    snapshot[0] = replace(snapshot[0], refs={'refs/heads/sibling':('oid', 'commit')},
                                                          worktrees={'sibling':('new-head',)})
                        if change == 'contention':
                            self.assertTrue(git_waiting.wait(3))
                            self.assertEqual(calls, [])
                            self.assertEqual(self.connection.execute(
                                "SELECT COUNT(*) FROM task_call_reservations WHERE repo='A'").fetchone()[0], 0)
                            independent_done = threading.Event()
                            def independent():
                                try:
                                    with closing(database.connect_database(self.directory, run_migrations=False)) as other:
                                        with scheduler.slot('verify', scheduling.Task('B', 'G-003', other)):
                                            self.assertFalse(calls)
                                except BaseException as error:
                                    errors.append(error)
                                finally:
                                    independent_done.set()
                            independent_thread = threading.Thread(target=independent)
                            independent_thread.start()
                            self.assertTrue(independent_done.wait(3))
                            independent_thread.join(3)
                    thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertIn(('fetch', '--no-tags', 'origin',
                               '+refs/heads/main:refs/remotes/origin/main'), commands)
                if change in {'sibling', 'contention'}:
                    self.assertEqual(errors, [])
                    self.assertEqual(calls, ['verify'])
                else:
                    self.assertEqual(calls, [])
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], scheduling.SchedulingChanged)
                self.assertEqual(sum(scheduler.running['verify'].values()), 0)

    def test_uncertain_verify_keeps_management_while_project_children_and_other_project_call_continue(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.runtime.contracts import PROJECT_STEP_RETRY
        from loop_manager_internal.storage import task_calls
        from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        scheduler = self.scheduler(max_workers=2)
        with self.assertRaises(VerifyTerminationUncertain), scheduler.slot(
                'verify', scheduling.Task('A', 'G-001', self.connection)):
            receipt = task_calls.prepare_verify_receipt()
            raise VerifyTerminationUncertain('child stop unknown')
        restarted = self.scheduler(max_workers=2)
        for kind in ('agent', 'verify'):
            with self.assertRaises(task_calls.CallDeferred), restarted.slot(
                    kind, scheduling.Task('A', 'G-001', self.connection)):
                self.fail('종료 불명 verify의 같은 후보에서 호출을 시작했습니다.')
        calls = []
        def execute(child, repository, **_options):
            task = scheduling.current_task.get()
            if task.plan_id == 'G-001':
                self.fail('종료 불명 호출을 재준비 대기 없이 dispatch했습니다.')
            else:
                with restarted.slot('agent', task):
                    calls.append(task.plan_id)
                    self.assertEqual(scheduling.managed_count(task.connection, 'A'), 2)
                    with closing(database.connect_database(self.directory, run_migrations=False)) as other:
                        with restarted.slot('agent', scheduling.Task('B', 'G-003', other)):
                            calls.append('G-003')
                repository.transition_from_snapshot(repository.projected('A'), plan_values={'status':'PR_OPEN'},
                    project_values=runtime_state.clear_project_values())
                restarted.pause_project('A')
                return 0
        service = ProjectService(SimpleNamespace(activate_ready_execplan=loop_manager.activate_ready_execplan,
                                                resume_manual_work=None),
            ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                            True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base')
        token = scheduling.current_scheduler.set(restarted)
        try:
            with fast_mode_scope(self.config), mock.patch.object(ProjectService, '_dispatch_execution', execute):
                self.assertEqual(scheduling.run_project_tasks(service, newly_activated=False), PROJECT_STEP_RETRY)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(calls, ['G-002', 'G-003'])
        self.assertEqual(scheduling.managed_count(self.connection, 'A'), 1)
        self.assertEqual(sum(restarted.running['verify'].values()), 1)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 1)
        task_calls.verify_stopped(receipt)

    def test_two_verify_slots_queue_third_without_blocking_independent_agent(self):
        self._insert_plan(self.connection, 'G-004', 'READY', repo='B')
        scheduler = self.scheduler(max_workers=1, verify_concurrency=2)
        entered, release = threading.Event(), threading.Event()
        errors = []
        def third():
            try:
                with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                    with scheduler.slot('verify', scheduling.Task('A', 'G-002', connection)):
                        entered.set()
                        self.assertTrue(release.wait(3))
            except BaseException as error:
                errors.append(error)
        with scheduler.slot('verify', scheduling.Task('A', 'G-001', self.connection)):
            with scheduler.slot('verify', scheduling.Task('B', 'G-003', self.connection)):
                thread = threading.Thread(target=third)
                thread.start()
                with scheduler.condition:
                    self.assertTrue(scheduler.condition.wait_for(lambda:bool(scheduler.waiters['verify']), 3))
                self.assertFalse(entered.is_set())
                with scheduler.slot('agent', scheduling.Task('B', 'G-004', self.connection)):
                    self.assertEqual(sum(scheduler.running['verify'].values()), 2)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(sum(scheduler.running['verify'].values()), 2)
            finally:
                release.set()
                thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sum(scheduler.running['verify'].values()), 0)

    def test_manual_resume_rechecks_group_phase_and_active_member_after_admission_wait(self):
        from loop_manager_internal.application.manual_work import ManualWorkService
        from loop_manager_internal.execution.manual_candidate import ManualCandidate
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        self.connection.execute("UPDATE projects SET registered=1,provider='codex' WHERE repo='A'")
        self.connection.execute("UPDATE exec_plans SET status='MANUAL_PENDING' WHERE repo='A'")
        store = ManualWorkRepository(self.connection)
        group = store.create('A', 'submit', 'manual', [], 'head', 'base', members=('G-001', 'G-002'))
        store.update(group, state='AUTOMATIC', request_id='accepted', owner_id='G-001',
                     branch='manual', worktree=str(self.directory / 'manual'))
        self.connection.commit()
        for change in ('phase', 'member'):
            with self.subTest(change=change):
                with self.connection:
                    store.update(group, resume_phase='VERIFYING', active_id=None)
                scheduler = self.scheduler()
                scheduler.admitting = True
                errors = []
                def resume():
                    token = scheduling.current_scheduler.set(scheduler)
                    try:
                        with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                            effects = SimpleNamespace(git=mock.Mock(), git_bytes=mock.Mock(),
                                                      pr_for_branch=mock.Mock(), resolve_profile=mock.Mock())
                            ManualWorkService(effects, connection, self.directory, Path('A'), self.config_path).resume()
                    except BaseException as error:
                        errors.append(error)
                    finally:
                        scheduling.current_scheduler.reset(token)
                with mock.patch.object(ManualCandidate, 'note_pending'):
                    thread = threading.Thread(target=resume)
                    thread.start()
                    with scheduler.condition:
                        self.assertTrue(scheduler.condition.wait_for(lambda:bool(scheduler.waiters['management']), 3))
                        with self.connection:
                            store.update(group, **({'resume_phase':'IMPLEMENTING'} if change == 'phase'
                                                   else {'active_id':'G-002'}))
                        scheduler.admitting = False
                        scheduler.condition.notify_all()
                    thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(store.get('A', group['group_id'])['attempts'], 0)
                self.assertEqual([row[0] for row in self.connection.execute(
                    "SELECT status FROM exec_plans WHERE repo='A' ORDER BY plan_id")],
                    ['MANUAL_PENDING', 'MANUAL_PENDING'])

    def test_actual_calls_allocate_basic_turn_before_priority_and_resume_cursor(self):
        from loop_manager_internal.storage import task_calls
        self.connection.execute("INSERT INTO projects(repo,base_branch) VALUES ('C','main')")
        self.connection.commit()
        for repo, plan_id, priority in (('A','G-004','critical'), ('A','G-005','high'),
                                       ('C','G-006','low'), ('C','G-007','low'), ('A','G-008','critical')):
            self._insert_plan(self.connection, plan_id, 'READY', repo=repo, priority=priority)
        scheduler = self.scheduler(max_workers=2)
        order, errors = [], []
        def queued(repo, plan_id):
            try:
                with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                    with scheduler.slot('agent', scheduling.Task(repo, plan_id, connection, resumed=False)):
                        task_calls.mark_started(connection, token=task_calls.current_call.get().token)
                        order.append(plan_id)
                        self.assertLessEqual(sum(scheduler.running['agent'].values()), scheduler.limits['agent'])
            except BaseException as error:
                errors.append(error)
        with scheduler.slot('agent', scheduling.Task('A', 'G-001', self.connection, resumed=False)):
            task_calls.mark_started(self.connection, token=task_calls.current_call.get().token)
            with scheduler.slot('agent', scheduling.Task('B', 'G-003', self.connection, resumed=False)):
                task_calls.mark_started(self.connection, token=task_calls.current_call.get().token)
                threads = [threading.Thread(target=queued, args=(repo, plan_id))
                           for repo, plan_id in (('A','G-004'), ('C','G-006'), ('A','G-005'))]
                for thread in threads:
                    thread.start()
                with scheduler.condition:
                    self.assertTrue(scheduler.condition.wait_for(lambda:len(scheduler.waiters['agent']) == 3, 3))
            for thread in threads:
                thread.join(4)
                self.assertFalse(thread.is_alive())
        self.assertEqual(order, ['G-006', 'G-004', 'G-005'])
        self.assertEqual(errors, [])
        scheduler = self.scheduler(max_workers=1)
        self.assertEqual(scheduler.cursor['agent'], 'A')
        scheduler.running['agent']['B'] = 1  # 두 후보의 등록 뒤 보존한 cursor로 한 자리를 연다.
        threads = [threading.Thread(target=queued, args=(repo, plan_id))
                   for repo, plan_id in (('A','G-008'), ('C','G-007'))]
        for thread in threads:
            thread.start()
        with scheduler.condition:
            self.assertTrue(scheduler.condition.wait_for(lambda:len(scheduler.waiters['agent']) == 2, 3))
            scheduler.running['agent']['B'] = 0
            scheduler.condition.notify_all()
        for thread in threads:
            thread.join(4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(order, ['G-006', 'G-004', 'G-005', 'G-007', 'G-008'])
        self.assertEqual(errors, [])

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

    def test_other_blocked_operation_does_not_keep_stale_project_execution(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        runtime.block_plan('A', 'G-001', reason_code='worker_output_invalid', evidence='failed test',
                           release_fingerprint='blocked', project_values={'phase': 'WORKING'})
        runtime.hydrate_snapshot('A', runtime.get('A', 'G-001'))
        self.connection.execute("UPDATE projects SET phase='WORKING' WHERE repo='A'")
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE repo='A' AND plan_id='G-002'")
        self.connection.execute("INSERT INTO execution_checkpoints(repo,plan_id,operation) VALUES ('A','G-002','rebase')")
        self.connection.commit()
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
        project = self.connection.execute("SELECT * FROM projects WHERE repo='A'").fetchone()
        self.assertIsNone(project['phase'])
        self.assertIsNone(project['branch'])
        self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')
        self.assertEqual(runtime.get('A', 'G-001').worktree, 'one')
        self.assertEqual(self.connection.execute("SELECT operation FROM execution_checkpoints WHERE plan_id='G-002'").fetchone()[0], 'rebase')

    def test_limit_reduction_during_activation_preserves_ready_without_failure(self):
        from loop_manager_internal.storage.task_calls import CallDeferred
        for error in (runtime_state.TaskLimitReached('limit reduced'), CallDeferred('call pending')):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(runtime_state.RuntimeStateRepository, 'activate', side_effect=error):
                    self.assertIsNone(loop_manager.activate_ready_execplan(self.connection, 'A', self.directory, 'base'))
                self.assertEqual(runtime_state.RuntimeStateRepository(self.connection).get('A', 'G-001').status, 'READY')

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
        from loop_manager_internal.storage import task_calls
        scheduler = self.scheduler(max_workers=1, verify_concurrency=1)
        entered_agent, entered_verify, release = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def run(kind, repo, plan_id, entered):
            try:
                with closing(database.connect_database(self.directory,run_migrations=False)) as connection:
                    task=scheduling.Task(repo,plan_id,connection,resumed=False)
                    with scheduler.slot(kind,task):
                        task_calls.mark_started(connection, token=task_calls.current_call.get().token)
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

    def test_archive_runs_between_continuous_admissions_without_candidate_records(self):
        from contextlib import ExitStack
        from concurrent.futures import wait as wait_for_tasks
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.runtime.resource_lock import resource_locks
        from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
        self._insert_plan(self.connection, 'G-004', 'MERGED', repo='A')
        scheduler = self.scheduler(max_tasks_per_project=1)
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        seen = []
        archived_while_running = threading.Event()
        first_future = []
        def archive(connection, _repo_path, _state_dir, repo, _base, **options):
            self.assertTrue(options['archive_safety']())
            self.assertFalse(options['correction_safety']())
            repository = ExecPlanArchiveRepository(connection)
            for item in repository.merged_candidates(repo):
                self.assertIsNone(repository.item(repo, item.plan_id))
                self.assertIsNone(options['candidate_safety'](item))
                seen.append('archive:' + item.plan_id)
                with connection:
                    connection.execute('UPDATE exec_plans SET path=? WHERE repo=? AND plan_id=?',
                                       (item.archive_path, repo, item.plan_id))
                if item.plan_id == 'G-004':
                    archived_while_running.set()
                    first_future[0].result(timeout=3)
        effects = SimpleNamespace(activate_ready_execplan=loop_manager.activate_ready_execplan,
            resume_manual_work=None, dispatch_execplan_archive_housekeeping=archive, operation_print=mock.Mock())
        service = ProjectService(effects, ProjectIdentity(self.connection, self.directory, 'A', self.config_path,
            None, False, None, True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base', catalog_refresh=mock.Mock())
        def execute(child, repository, **_options):
            task = scheduling.current_task.get()
            seen.append('execute:' + task.plan_id)
            if task.plan_id == 'G-001':
                self.assertTrue(archived_while_running.wait(3), 'lease 반환 뒤에도 archive가 Worker 종료를 기다렸습니다.')
            if task.plan_id == 'G-002':
                self.assertIn('archive:G-001', seen)
            repository.transition_from_snapshot(repository.projected('A'),
                plan_values={'status': 'MERGED'}, project_values=runtime_state.clear_project_values())
            return 0
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with ExitStack() as held:
                held.enter_context(resource_locks(self.directory, 'A', ['git:refs/heads/main'], owner='other-publication'))
                def wait(*args, **kwargs):
                    if not first_future:
                        first_future.append(args[0][0])
                    held.close()
                    return wait_for_tasks(*args, **kwargs)
                with fast_mode_scope(self.config), mock.patch.object(ProjectService, '_dispatch_execution', execute), \
                     mock.patch('concurrent.futures.wait', side_effect=wait), \
                     mock.patch.object(scheduler, 'wait_recheck', return_value=False):
                    self.assertEqual(scheduling.run_project_tasks(service, newly_activated=True), 0)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual([item for item in seen if item != 'execute:G-001'],
                         ['archive:G-004', 'archive:G-001', 'execute:G-002', 'archive:G-002'])
        self.assertEqual(service.effects.operation_print.call_count, 1)

    def test_archive_discovery_db_wait_does_not_prevent_independent_admission(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE repo='A' AND plan_id='G-001'")
        self.connection.commit()
        effects = SimpleNamespace(operation_print=mock.Mock(), dispatch_execplan_archive_housekeeping=mock.Mock())
        service = ProjectService(effects, ProjectIdentity(self.connection, self.directory, 'A', self.config_path,
            None, False, None, True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', catalog_refresh=mock.Mock())
        with mock.patch.object(ExecPlanArchiveRepository, 'merged_candidates', side_effect=DatabaseWaitTimeout('busy')):
            service._archive_scheduling_boundary()
        effects.dispatch_execplan_archive_housekeeping.assert_not_called()
        row = self.connection.execute("SELECT * FROM execplan_archive_observations WHERE repo='A'").fetchone()
        self.assertEqual(row['reason'], 'candidate_discovery_failed:busy')
        with fast_mode_scope(self.config):
            activation = loop_manager.activate_ready_execplan(self.connection, 'A', self.directory, 'base')
        self.assertEqual(activation['plan_id'], 'G-002')


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

    def test_started_followup_precedes_new_and_unstarted_reservation_keeps_retry(self):
        from loop_manager_internal.storage import task_calls
        from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
        scheduler = self.scheduler(max_workers=1, verify_concurrency=1)
        with scheduler.task('A', 'G-001', self.connection, resumed=False):
            first = scheduling.current_task.get()
            with scheduler.slot('agent', first):
                pass
            self.assertEqual(first.rank()[0], 3)
            self.assertIsNone(self.connection.execute("SELECT repo FROM task_scheduling_cursor WHERE kind='agent'").fetchone())
            with scheduler.slot('agent', first):
                self.connection.execute("INSERT INTO agent_invocations(invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at) "
                    "VALUES ('first','A','G-001','worker','codex','m','e','start',1,'now')")
                self.connection.commit()
                task_calls.bind_agent('first')
                InvocationRepository(self.connection).mark_execution_started('first')
            self.assertTrue(first.resumed)
            self.connection.execute("UPDATE exec_plans SET status='REVIEWING' WHERE repo='A' AND plan_id='G-001'")
            self.connection.commit()
        with closing(database.connect_database(self.directory, run_migrations=False)) as other:
            replacement = scheduling.TaskScheduler(self.config, database.connect_database, self.directory, other)
            with replacement.task('A', 'G-001', other, resumed=False):
                continued = scheduling.current_task.get()
                new = scheduling.Task('A', 'G-002', other, resumed=False)
                entries = [[new, new.rank()], [continued, continued.rank()]]
                replacement.waiters['agent'].extend(entries)
                self.assertIs(replacement._next('agent', other), entries[1])
                replacement.waiters['agent'].clear()
                order = []
                for candidate in (continued, new):
                    with replacement.slot('agent', candidate):
                        task_calls.mark_started(other, token=task_calls.current_call.get().token)
                        order.append(candidate.plan_id)
                self.assertEqual(order, ['G-001', 'G-002'])
                replacement.requeue(continued)
                before = other.execute("SELECT retry_order,resumed FROM task_management_admissions WHERE repo='A' AND plan_id='G-001'").fetchone()
                with replacement.slot('agent', continued):
                    pass  # reservation alone is not an agent process start
                after = other.execute("SELECT retry_order,resumed FROM task_management_admissions WHERE repo='A' AND plan_id='G-001'").fetchone()
                self.assertEqual(tuple(after), tuple(before))
                self.assertEqual(continued.retry_order, before['retry_order'])

    def test_resumed_waiters_are_served_in_registration_order(self):
        scheduler = self.scheduler()
        first = scheduling.Task('A', 'G-002', self.connection)
        second = scheduling.Task('A', 'G-001', self.connection)
        entries = [[first, first.rank()], [second, second.rank()]]
        scheduler.waiters['agent'].extend(entries)
        self.assertIs(scheduler._next('agent', self.connection), entries[0])
        scheduler.waiters['agent'].remove(entries[0])
        scheduler.waiters['agent'].append([first, first.rank()])
        self.assertIs(scheduler._next('agent', self.connection), entries[1])

    def test_started_receipts_recover_once_without_consuming_later_retry(self):
        from loop_manager_internal.storage import task_calls
        from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
        scheduler = self.scheduler()
        with scheduler.task('A', 'G-001', self.connection, resumed=False):
            task = scheduling.current_task.get()
            owner = task_calls.reserve(self.connection, self.directory, 'A', 'G-001', 'verify', 'run')
            token = task_calls.current_call.set(owner)
            try:
                receipt = task_calls.prepare_verify_receipt()
                with mock.patch.object(task_calls, 'mark_started', side_effect=sqlite3.OperationalError('busy')):
                    with self.assertRaises(sqlite3.OperationalError):
                        task_calls.verify_started(receipt, SimpleNamespace(pid=12345))
                task_calls.verify_stopped(receipt)
                self.assertEqual(task.rank()[0], 3)
                self.assertEqual(task_calls.recover(self.connection, self.directory), ())
                self.assertEqual(task.rank()[0], 2)
            finally:
                task_calls.current_call.reset(token)
            owner = task_calls.reserve(self.connection, self.directory, 'A', 'G-001', 'agent', 'run')
            self.connection.execute("INSERT INTO agent_invocations(invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at) "
                "VALUES ('actual','A','G-001','worker','codex','m','e','start',1,'now')")
            self.connection.execute("UPDATE task_call_reservations SET invocation_id='actual' WHERE token=?", (owner.token,))
            self.connection.commit()
            ledger = InvocationRepository(self.connection)
            ledger.mark_execution_started('actual')
            scheduler.requeue(task)
            retry = task.retry_order
            ledger.mark_execution_started('actual')
            self.assertEqual(self.connection.execute("SELECT retry_order FROM task_management_admissions "
                "WHERE repo='A' AND plan_id='G-001'").fetchone()[0], retry)
            task_calls.release(owner)

        with scheduler.task('A', 'G-002', self.connection, resumed=False):
            task = scheduling.current_task.get()
            owner = task_calls.reserve(self.connection, self.directory, 'A', 'G-002', 'agent', 'run')
            self.connection.execute("UPDATE task_call_reservations SET receipt_path='receipt' WHERE token=?", (owner.token,))
            self.connection.commit()
            row = self.connection.execute("SELECT * FROM task_call_reservations WHERE token=?", (owner.token,)).fetchone()
            with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'evidence':'not_started'}):
                task_calls._recover_agent_start(self.connection, row, self.directory)
                self.assertEqual(task.rank()[0], 3)
            with mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'evidence':'process_group_stopped'}):
                task_calls._recover_agent_start(self.connection, row, self.directory)
                self.assertEqual(task.rank()[0], 2)
            task_calls.release(owner)

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

    def test_observed_specification_change_drains_before_followup_dispatch(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcome
        from loop_manager_internal.planning.planning_runtime import PlanningContext
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for plan in ('G-001', 'G-002'):
                runtime.activate('A', plan, activation_sha='base', branch=plan, worktree=plan)
        runtime.update_plan('A', 'G-001', plan_values={'worker_session_id':'saved-session'}, enqueue=False)
        with self.connection:
            self.connection.execute("UPDATE projects SET processed_sha='base', specification_fingerprint='stable', "
                                    "planning_fingerprint='full-base' WHERE repo='A'")
        scheduler = self.scheduler(max_tasks_per_project=2, verify_concurrency=1)
        agent_running, drained = threading.Event(), threading.Event()
        calls, observed = [], []
        def git(_path, *_args, **_kwargs):
            with scheduler.condition:
                ready = agent_running.is_set() and bool(scheduler.waiters['verify'])
            return SimpleNamespace(stdout='new-specification' if ready or drained.is_set() else 'base')
        def planning_ports(connection, repository, _maintenance, _context, _mutex):
            def specification(_path, revision):
                observed.append(revision)
                return 'changed' if revision == 'new-specification' else 'stable'
            return SimpleNamespace(state=SimpleNamespace(planning=repository.planning),
                git=SimpleNamespace(input_fingerprint=lambda _path, revision:'full-' + revision,
                    specification_fingerprint=specification, requirement_archive_only=lambda *_args, **_kwargs:False))
        def dispatch(context):
            task = scheduling.current_task.get()
            calls.append(task.plan_id)
            if task.plan_id == 'G-002':
                @scheduling.scheduled_call('verify')
                def verify():
                    self.fail('명세 변경 뒤 대기 중 verify가 시작됐습니다.')
                verify()
            else:
                with scheduler.slot('agent', task):
                    agent_running.set()
                    self.assertTrue(drained.wait(4))
                    self.assertEqual(scheduler.running['agent']['A'], 1)
                    with scheduler.slot('agent', scheduling.Task('B', 'G-003', task.connection)):
                        calls.append('independent-B')
                return ExecutionOutcome.advanced(context)
        def coordinator(_ports):
            context = SimpleNamespace(repo='A')
            return SimpleNamespace(prepare=lambda:context, dispatch=dispatch)
        emit = scheduling.events.emit
        def observe(message, **values):
            if message == '프로젝트 배정 중단':
                drained.set()
            emit(message, **values)
        effects = SimpleNamespace(git=git, _PlanningLifecyclePorts=planning_ports,
            _ExecutionLifecyclePorts=mock.Mock(), ExecutionPhaseCoordinator=coordinator,
            _check_runtime_handoff=mock.Mock(), activate_ready_execplan=mock.Mock(), resume_manual_work=None)
        service = ProjectService(effects, ProjectIdentity(self.connection, self.directory, 'A', self.config_path,
            None, False, None, True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base', provider='codex')
        context = PlanningContext(repo='A', repo_path=Path('A'), state_dir=self.directory, base_branch='main',
            base_sha='base', provider='codex', config_path=self.config_path, registered_only=True,
            refactoring_review_interval=20, self_repository=False, refresh_catalog=service._refresh_catalog)
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with scheduler.slot('verify', scheduling.Task('B', 'G-003', self.connection)), \
                 mock.patch.object(scheduling.events, 'emit', side_effect=observe):
                self.assertEqual(service._dispatch_execution(runtime, newly_activated=False,
                    planning_context=context), loop_manager.PROJECT_STEP_RETRY)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(calls.count('G-001'), 1)
        self.assertEqual(calls.count('G-002'), 1)
        self.assertIn('independent-B', calls)
        self.assertIn('new-specification', observed)
        self.assertIn('A', scheduler.draining_projects)
        self.assertNotIn('B', scheduler.draining_projects)
        effects.activate_ready_execplan.assert_not_called()
        self.assertEqual(runtime.get('A', 'G-001').worker_session_id, 'saved-session')
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)

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

    def test_missing_targeted_evidence_waits_and_retries_before_sibling_verify_returns(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        from loop_manager_internal.execution.result_validation import TargetedTestFailure
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind
        from loop_manager_internal.recovery.checkpoint_service import _execution_outcome_step_result
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
        from tests.support.worker import worker_result
        scheduler = self.scheduler(max_workers=1, max_tasks_per_project=2)
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with fast_mode_scope(self.config):
            for plan in ('G-001', 'G-002'):
                runtime.activate('A', plan, activation_sha='base', branch=plan, worktree=plan)
            runtime.update_plan('A', 'G-001', plan_values={'worker_session_id':'saved-worker'}, enqueue=False)
        service = ProjectService(SimpleNamespace(activate_ready_execplan=mock.Mock(return_value=None), resume_manual_work=None),
            ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                            True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base', provider='codex')
        verifying, retried = threading.Event(), threading.Event()
        calls = []
        def execute(child, repository, **options):
            task = scheduling.current_task.get()
            if task.plan_id == 'G-002':
                with scheduler.slot('verify', task):
                    verifying.set()
                    self.assertTrue(retried.wait(3))
            else:
                self.assertTrue(verifying.wait(2))
                with scheduler.slot('agent', task):
                    calls.append(task.plan_id)
                    self.assertEqual(repository.get('A', task.plan_id).worker_session_id, 'saved-worker')
                    if len(calls) == 1:
                        report = worker_result(tests='targeted test: FAIL; tests.Sample.test_one')
                        with self.assertRaises(TargetedTestFailure):
                            loop_manager.parse_worker_result(report, {'id':task.plan_id, 'title':'targeted retry'})
                        decision = RecoveryRepository(task.connection).observe(RecoveryObservation(
                            RecoveryReason.TARGETED_TEST_CORRECTION, 'A', task.plan_id, 'WORKING',
                            'revision', 'base', 'no-new-evidence', 'same-work', report), attempts=1).decision
                        task.connection.commit()
                        return _execution_outcome_step_result(ExecutionOutcome(
                            ExecutionOutcomeKind.TRANSIENT_PAUSE, recovery=decision))
                    self.assertIsNotNone(task.retry_order)
                    self.assertEqual(sum(scheduler.running['verify'].values()), 1)
                    self.assertEqual(TargetedTestRepository(task.connection).sequence('A', task.plan_id), 0)
                    retried.set()
            repository.transition_from_snapshot(repository.projected('A'), plan_values={'status':'PR_OPEN'},
                                                 project_values=runtime_state.clear_project_values())
            return 0
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with fast_mode_scope(replace(self.config, max_tasks_per_project=2)), \
                 mock.patch.object(ProjectService, '_dispatch_execution', execute), \
                 mock.patch('loop_manager_internal.storage.recovery_state.RETRY_DELAY_SECONDS', .05):
                self.assertEqual(scheduling.run_project_tasks(service, newly_activated=False), 0)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(calls, ['G-001', 'G-001'])
        saved = RecoveryRepository(self.connection).row('A', 'G-001')
        self.assertIsNone(saved['retry_after'])
        self.assertEqual(saved['occurrence'], 1)
        self.assertEqual(runtime.get('A', 'G-001').worker_session_id, 'saved-worker')

    def test_review_correction_batches_wait_without_resetting_started_turns_on_new_run(self):
        from loop_manager_internal.storage import recovery_state
        now = time.time()
        with mock.patch.object(recovery_state.time, 'time', return_value=now) as clock:
            recovery_state.begin_external_run(self.connection, 'logical-A')
            self.connection.commit()
            for source in (1, 2, 3):
                allowed, used, run = recovery_state.reserve_review_correction(self.connection, 'A', 'G-001', source)
                self.assertTrue(allowed)
                recovery_state.start_review_correction(self.connection, 'A', 'G-001', run, source)
            self.assertFalse(recovery_state.reserve_review_correction(self.connection, 'A', 'G-001', 4)[0])
            recovery_state.begin_external_run(self.connection, 'logical-B')
            self.connection.commit()
            self.assertFalse(recovery_state.reserve_review_correction(self.connection, 'A', 'G-001', 4)[0])
            self.assertEqual(recovery_state.reserve_review_correction(self.connection, 'A', 'G-001', 3),
                             (True, 3, 'logical-A'))
            clock.return_value += recovery_state.RETRY_DELAY_SECONDS + 1
            allowed, used, run = recovery_state.reserve_review_correction(self.connection, 'A', 'G-001', 4)
            self.assertTrue(allowed)
            self.assertEqual(used, 4)
            recovery_state.start_review_correction(self.connection, 'A', 'G-001', run, 4)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM review_correction_turns WHERE started_at IS NOT NULL').fetchone()[0], 4)

    def test_usage_wait_never_probes_and_requires_same_provider_model_success_evidence(self):
        from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
        from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
        scheduler = self.scheduler()
        ledger = InvocationRepository(self.connection)
        def result(plan, provider, model, kind):
            identity = ledger.start(InvocationContext('A', plan), 'worker', provider, ResolvedProfile(model, 'low'), 'same-session')
            ledger.mark_execution_started(identity)
            ledger.mark_execution_finished(identity, 2)
            ledger.finish(identity, kind, 0 if kind == 'success' else 1, 2, 'same-session', Usage())
            return identity
        failed = result('G-001', 'codex', 'model-A', 'usage_limit')
        checks = []
        def wait(_seconds):
            checks.append('wait')
            result('G-002', 'claude' if len(checks) == 1 else 'codex', 'model-A', 'success')
            self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        with mock.patch.object(scheduler.condition, 'wait', side_effect=wait), \
             mock.patch.object(scheduling, 'observe_runtime') as observe:
            self.assertTrue(scheduler.wait_usage(self.connection, 'A', 'G-001'))
        observe.assert_not_called()
        self.assertEqual(checks, ['wait', 'wait'])
        self.assertEqual(self.connection.execute('SELECT result_kind FROM agent_invocations WHERE invocation_id=?', (failed,)).fetchone()[0], 'usage_limit')

    def test_retry_due_time_survives_runtime_replacement_and_no_early_dispatch(self):
        from loop_manager_internal.storage import recovery_state
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        scheduler = self.scheduler()
        repository = recovery_state.RecoveryRepository(self.connection)
        repository.observe(RecoveryObservation(RecoveryReason.SERVICE_UNAVAILABLE, 'A', 'G-001',
            'WORKING', 'revision', 'base', 'cli', 'same-session', 'original failure'), attempts=3)
        self.connection.commit()
        clock = [100.0]
        with mock.patch.object(recovery_state.time, 'time', side_effect=lambda:clock[0]):
            saved = repository.defer_retry('A', 'G-001')
            self.assertEqual(saved['retry_after'], 130)
            replacement = self.scheduler()
            observed = []
            def wait(seconds):
                observed.append(clock[0])
                clock[0] += 10
            with mock.patch.object(replacement.condition, 'wait', side_effect=wait), \
                 mock.patch.object(scheduling, 'observe_runtime') as observe:
                self.assertTrue(replacement.wait_retry(self.connection, 'A', 'G-001', resume=True))
            observe.assert_not_called()
        self.assertEqual(observed, [100, 110, 120])
        current = repository.row('A', 'G-001')
        self.assertIsNone(current['retry_after'])
        self.assertEqual(current['generation'], saved['generation'])
        self.assertEqual(current['recovery_counts'], saved['recovery_counts'])
        self.assertEqual(current['detail'], 'original failure')

    def test_exhausted_remote_race_does_not_restart_from_a_timer(self):
        from loop_manager_internal.storage import recovery_state
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        scheduler = self.scheduler()
        repository = recovery_state.RecoveryRepository(self.connection)
        repository.observe(RecoveryObservation(RecoveryReason.REMOTE_SNAPSHOT_RACE, 'A', 'G-001',
            'WORKING', 'revision', 'base', 'same-input', 'same-head', 'race'), attempts=3)
        self.connection.commit()
        before = dict(repository.row('A', 'G-001'))
        with mock.patch.object(scheduler.condition, 'wait') as wait:
            self.assertFalse(scheduler.wait_retry(self.connection, 'A', 'G-001'))
        wait.assert_not_called()
        self.assertEqual(dict(repository.row('A', 'G-001')), before)

    def test_unchanged_publication_failure_does_not_restart_from_a_timer(self):
        from loop_manager_internal.storage import recovery_state
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        scheduler = self.scheduler()
        repository = recovery_state.RecoveryRepository(self.connection)
        failure = RecoveryObservation(RecoveryReason.PUBLICATION_RETRY, 'A', 'G-001',
            'WORKING', 'revision', 'base', 'same-input', 'same-head', 'unpublished checkpoint')
        for _ in range(3):
            repository.observe(failure, attempts=3)
        self.connection.commit()
        before = dict(repository.row('A', 'G-001'))
        with mock.patch.object(scheduler.condition, 'wait') as wait:
            self.assertFalse(scheduler.wait_retry(self.connection, 'A', 'G-001'))
        wait.assert_not_called()
        self.assertEqual(dict(repository.row('A', 'G-001')), before)
        repository.observe(replace(failure, fingerprint='new-input'), attempts=3)
        self.connection.commit()
        with mock.patch.object(recovery_state.time, 'time', return_value=100) as clock, \
             mock.patch.object(scheduler.condition, 'wait', side_effect=lambda _: setattr(clock, 'return_value', 131)):
            self.assertTrue(scheduler.wait_retry(self.connection, 'A', 'G-001'))

    def test_unproven_pr_history_releases_projection_and_continues_other_work(self):
        from loop_manager_internal.application.project_service import ProjectService
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE repo='A' AND plan_id='G-001'")
        self.connection.execute("UPDATE projects SET phase='PR_OPEN' WHERE repo='A'")
        self.connection.commit()
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM exec_plans WHERE repo='A'")]
        effects = SimpleNamespace(_record_project_recovery=mock.Mock(), active_execplan=mock.Mock(return_value=None),
            _clear_active_in_current_transaction=lambda connection, repo: runtime_state.RuntimeStateRepository(connection).clear(repo))
        identity = SimpleNamespace(repo_arg='A', connection=self.connection, recovery_request_id=None)
        service = ProjectService(effects, identity)
        service.state.repo = 'A'
        for name in ('_prepare_repository', '_reconcile_catalog_and_cleanup', '_resume_manual_work', '_resume_existing_execution'):
            self.enterContext(mock.patch.object(service, name, return_value=None))
        self.enterContext(mock.patch.object(service, '_synchronize_pull_requests',
            side_effect=RecoveryFailure(RecoveryReason.GIT_INPUT_UNCERTAIN, 'unconnected PR history')))
        dispatch = self.enterContext(mock.patch.object(service, '_run_coordinators', return_value=0))
        self.assertEqual(service.run().code, 0)
        dispatch.assert_called_once()
        effects._record_project_recovery.assert_called_once()
        self.assertIsNone(service.state.row['phase'])
        self.assertEqual([tuple(row) for row in self.connection.execute("SELECT * FROM exec_plans WHERE repo='A'")], before)

    def test_call_condition_wait_reclaims_only_its_confirmed_reservation(self):
        from loop_manager_internal.storage import task_calls
        target = task_calls.reserve(self.connection, self.directory, 'A', 'G-001', 'verify', 'run')
        other = task_calls.reserve(self.connection, self.directory, 'B', 'G-003', 'agent', 'run')
        confirmed = [False]
        with mock.patch.object(task_calls, '_confirmed', side_effect=lambda _c, row, _s:
                row['token'] == target.token and confirmed[0]):
            scheduler = self.scheduler()
            with mock.patch.object(scheduler.condition, 'wait', side_effect=lambda _:confirmed.__setitem__(0, True)) as wait, \
                 mock.patch.object(scheduling, 'observe_runtime') as observe:
                self.assertTrue(scheduler.wait_existing_call(self.connection, 'A', 'G-001'))
            wait.assert_called_once()
            observe.assert_not_called()
        self.assertEqual([row[0] for row in self.connection.execute('SELECT token FROM task_call_reservations')], [other.token])
        self.assertEqual(scheduler.running['verify']['A'], 0)
        self.assertEqual(scheduler.running['agent']['B'], 1)
        self.assertIn(('B', 'G-003', 'agent'), scheduler.retained)

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
            if index == 2:
                from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
                with mock.patch.object(RecoveryRepository, 'mark_paused',
                        side_effect=DatabaseWaitTimeout('pause write queue deadline')), \
                     self.assertRaises(DatabaseWaitTimeout):
                    retry_pause_detail(self.connection, 'A', before, attempts, plan_id='G-001')
                self.assertEqual(attempts, {'G-001|transient_retry': 2})
            detail = retry_pause_detail(self.connection, 'A', before, attempts, plan_id='G-001')
            self.assertEqual(detail, 'temporary' if index == 2 else None)
            before = {str(row['subject']): int(row['generation']) for row in self.connection.execute(
                "SELECT subject,generation FROM recovery_records WHERE repo='A'")}
        self.assertEqual(attempts, {'G-001|transient_retry': 3})
        self.assertEqual(repository.row('A', 'G-001')['pending_action'], 'pause')
        self.assertEqual(repository.row('A', 'G-002')['disposition'], 'transient_retry')

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
        with mock.patch.object(scheduling, 'observe_runtime') as observe:
            self._assert_exception_isolation('retry')
        observe.assert_not_called()

    def test_repeated_task_exceptions_pause_only_that_task_and_keep_admitting(self):
        self._assert_exception_isolation('pause')

    def test_task_setup_exception_does_not_abort_project_result_collection(self):
        self._assert_exception_isolation('setup')

    def test_database_retry_wait_reacquires_real_sqlite_writer_without_call_capacity(self):
        from contextlib import contextmanager
        from loop_manager_internal.storage import db_access
        scheduler = self.scheduler()
        waiting, completed = threading.Event(), threading.Event()
        errors = []
        original_waiting = db_access._waiting
        @contextmanager
        def observe_wait():
            waiting.set()
            with original_waiting():
                yield
        def retry():
            try:
                with closing(database.connect_database(self.directory, run_migrations=False)) as other:
                    scheduler.wait_database(scheduling.Task('A', 'G-001', other))
                    self.assertFalse(other.in_transaction)
                    completed.set()
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=retry)
        with closing(sqlite3.connect(self.directory / 'loop-manager.db', timeout=0)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            try:
                with mock.patch.object(db_access, '_waiting', observe_wait):
                    thread.start()
                    self.assertTrue(waiting.wait(3))
                    self.assertFalse(completed.is_set())
                    self.assertEqual(sum(scheduler.running['agent'].values()), 0)
                    self.assertEqual(sum(scheduler.running['verify'].values()), 0)
                    writer.rollback()
                    self.assertTrue(completed.wait(3))
            finally:
                writer.rollback()
                thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)

    def test_paused_task_reenters_only_after_recovery_inputs_change(self):
        self._assert_exception_isolation('changed_recovery')

    def test_retry_condition_wait_ignores_timers_and_diagnostics_and_honors_drain(self):
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        from loop_manager_internal.storage.recovery_state import RecoveryRepository
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.activate('A', 'G-001', activation_sha='base', branch='one', worktree='one')
        repository = RecoveryRepository(self.connection)
        repository.observe(RecoveryObservation(RecoveryReason.STATUS_FAILED, 'A', 'G-001', 'WORKING',
            'revision', 'base', 'same-input', 'same-head', 'temporary'), attempts=3)
        self.connection.commit()
        for stop in (False, True):
            with self.subTest(drain=stop):
                scheduler = self.scheduler()
                task = scheduling.Task('A', 'G-001', self.connection)
                observed = scheduler.retry_inputs(task)
                waits = []
                def waiting(_timeout):
                    waits.append(True)
                    with self.connection:
                        self.connection.execute("UPDATE exec_plans SET last_error=?,worker_session_id=?, "
                            "catalog_generation=catalog_generation+1 WHERE repo='A' AND plan_id='G-001'",
                            (str(len(waits)), str(len(waits))))
                        self.connection.execute("UPDATE recovery_records SET generation=generation+1,attempts=attempts+1, "
                            "retry_after=COALESCE(retry_after,0)+30 WHERE repo='A' AND subject='G-001'")
                    self.assertEqual(scheduler.retry_inputs(task), observed)
                    if len(waits) == 3:
                        if stop:
                            scheduler.pause_project('A')
                        else:
                            runtime.update_plan('A', 'G-001', plan_values={'active_sha':'new-input'}, enqueue=False)
                with mock.patch.object(scheduler.condition, 'wait', side_effect=waiting), \
                     mock.patch.object(scheduling, 'observe_runtime') as observe:
                    if stop:
                        with self.assertRaises(scheduling.PlanningDrain):
                            scheduler.wait_recheck(task, observed)
                    else:
                        self.assertTrue(scheduler.wait_recheck(task, observed))
                self.assertEqual(len(waits), 3)
                observe.assert_not_called()
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)
                self.assertEqual(sum(scheduler.running['agent'].values()), 0)

    def test_task_setup_exception_retries_in_the_same_execution(self):
        self._assert_exception_isolation('setup_retry')

    def test_completed_dispatch_collection_wait_does_not_repeat_the_effect(self):
        self._assert_exception_isolation('collection')

    def test_completed_dispatch_collection_error_retries_only_collection(self):
        self._assert_exception_isolation('collection_error')

    def test_error_record_db_wait_preserves_original_error_and_independent_work(self):
        self._assert_exception_isolation('error_record')

    def test_setup_db_wait_is_a_transient_pause(self):
        self._assert_exception_isolation('db_setup')

    def test_db_wait_preserves_task_and_keeps_independent_admission(self):
        self._assert_exception_isolation('db_wait')

    def test_db_wait_retries_the_same_task_after_the_queue_recovers(self):
        self._assert_exception_isolation('db_retry')

    def test_no_progress_retry_stops_only_that_task_and_keeps_admitting(self):
        self._assert_exception_isolation('no_progress')

    def test_pause_record_db_wait_is_inside_the_task_error_boundary(self):
        self._assert_exception_isolation('pause_record')

    def test_repeated_exception_returns_without_waiting_for_external_input(self):
        self._assert_exception_isolation('persistent')

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
        calls, storage_attempts, setup_attempts, dispatches = [], [], [], []
        resumed_after_condition, paused_input = threading.Event(), threading.Event()
        configure_session = notifications.configure_session
        def configure(connection, **options):
            if failure == 'setup_retry' and threading.current_thread().name == 'loop-task-G-001':
                setup_attempts.append(True)
                if len(setup_attempts) == 1:
                    failed.set()
                    raise RuntimeError('task setup failed once')
            if failure in {'setup', 'db_setup'} and threading.current_thread().name == 'loop-task-G-001':
                setup_attempts.append(True)
                if len(setup_attempts) <= 3:
                    if len(setup_attempts) == 3:
                        failed.set()
                    if failure == 'db_setup':
                        raise DatabaseWaitTimeout('setup DB queue deadline')
                    raise RuntimeError('task setup failed')
            return configure_session(connection, **options)
        def execute(child, repository, **options):
            task = scheduling.current_task.get()
            dispatches.append(task.plan_id)
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
                        if ((failure in {'pause', 'db_wait', 'changed_recovery'} and len(calls) == 4)
                                or failure in {'setup', 'db_setup'} and len(calls) == 1):
                            self.assertTrue(verifying.is_set())
                            resumed_after_condition.set()
                        if failure == 'no_progress' or failure == 'changed_recovery' and len(calls) <= 3:
                            from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
                            from loop_manager_internal.storage.recovery_state import RecoveryRepository
                            RecoveryRepository(task.connection).observe(RecoveryObservation(
                                RecoveryReason.PUBLICATION_RETRY, 'A', 'G-001', 'WORKING',
                                'revision', 'base', 'same-input', 'same-head', 'unpublished checkpoint'))
                            task.connection.commit()
                            if failure != 'changed_recovery' or len(calls) == 3:
                                failed.set()
                            return project_runner.PROJECT_STEP_RETRY
                        if failure in {'db_wait', 'db_retry', 'pause_record'}:
                            if failure != 'db_wait' or len(calls) == 3:
                                failed.set()
                            if (failure == 'db_wait' and len(calls) <= 3
                                    or failure == 'db_retry' and len(calls) == 1):
                                raise DatabaseWaitTimeout('DB queue deadline')
                            if failure == 'pause_record' and len(calls) == 1:
                                return project_runner.PROJECT_STEP_RETRY
                        if failure == 'persistent':
                            failed.set()
                            raise KeyError('duration')
                        if failure == 'pause' and len(calls) <= 3:
                            if len(calls) == 3:
                                failed.set()
                            raise sqlite3.OperationalError('database is locked')
                        if len(calls) == 1:
                            failed.set()
                            if failure not in {'collection', 'collection_error'}:
                                raise MutationEvidenceError('stored rebase result differs')
                    else:
                        self.assertTrue(failed.wait(3))
                        self.assertTrue(verifying.wait(3))
                        self.assertEqual(sum(scheduler.running['verify'].values()), 1)
                        if failure == 'changed_recovery':
                            self.assertTrue(paused_input.wait(4))
                            from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
                            from loop_manager_internal.storage.recovery_state import RecoveryRepository
                            RecoveryRepository(task.connection).observe(RecoveryObservation(
                                RecoveryReason.PUBLICATION_RETRY, 'A', 'G-001', 'WORKING',
                                'revision', 'base', 'new-input', 'new-head', 'corrected checkpoint'))
                            task.connection.commit()
                        if failure in {'pause', 'setup'}:
                            repository.update_plan('A', 'G-001',
                                plan_values={'active_sha':'condition-resolved'}, enqueue=False)
                        if failure in {'pause', 'setup', 'db_wait', 'db_setup', 'changed_recovery'}:
                            self.assertTrue(resumed_after_condition.wait(4))
                        replacement.set()
            repository.transition_from_snapshot(repository.projected('A'), plan_values={'status': 'PR_OPEN'},
                                                 project_values=runtime_state.clear_project_values())
            return 0
        pause_detail = project_runner.retry_pause_detail
        flush_events = notifications.flush_project_events
        update_plan = runtime_state.RuntimeStateRepository.update_plan
        def storage_wait():
            storage_attempts.append(True)
            if len(storage_attempts) <= 3:
                self.assertEqual(len(calls), 1)
                from loop_manager_internal.storage import task_calls
                self.assertIsNone(task_calls.current_call.get())
                if failure == 'collection_error':
                    if len(storage_attempts) <= 2:
                        raise RuntimeError('result collection transient error')
                else:
                    raise DatabaseWaitTimeout('result storage queue deadline')
        def pause(*args, **kwargs):
            if failure == 'pause_record' and kwargs.get('plan_id') == 'G-001':
                storage_wait()
            return pause_detail(*args, **kwargs)
        def flush(*args, **kwargs):
            task = scheduling.current_task.get()
            if failure in {'collection', 'collection_error'} and task is not None and task.plan_id == 'G-001':
                storage_wait()
            return flush_events(*args, **kwargs)
        def save(repository, repo, plan_id, **kwargs):
            if failure == 'error_record' and plan_id == 'G-001' and kwargs.get('plan_values', {}).get('last_error'):
                storage_wait()
            return update_plan(repository, repo, plan_id, **kwargs)
        retry_inputs = scheduler.retry_inputs
        def inputs(task):
            key = retry_inputs(task)
            if (failure == 'changed_recovery' and task.plan_id == 'G-001' and key[1] is not None
                    and key[1][-1] == 'pause' and threading.current_thread().name == 'loop-task-G-001'):
                paused_input.set()
            return key
        token = scheduling.current_scheduler.set(scheduler)
        try:
            with mock.patch.object(scheduler, 'retry_inputs', side_effect=inputs), \
                 fast_mode_scope(self.config), mock.patch.object(ProjectService, '_dispatch_execution', execute), \
                 mock.patch.object(notifications, 'configure_session', configure), \
                 mock.patch.object(project_runner, 'retry_pause_detail', pause), \
                 mock.patch.object(notifications, 'flush_project_events', flush), \
                 mock.patch.object(runtime_state.RuntimeStateRepository, 'update_plan', save):
                result = scheduling.run_project_tasks(service, newly_activated=True)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(len(calls), {'persistent': 3, 'retry': 2, 'pause': 4, 'setup': 2, 'db_wait': 4, 'db_retry': 2,
                                    'no_progress': 3, 'changed_recovery': 4, 'pause_record': 2, 'db_setup': 2,
                                    'setup_retry': 2, 'collection': 1, 'collection_error': 1, 'error_record': 2}[failure])
        self.assertEqual(dispatches.count('G-001'), len(calls))
        self.assertEqual(result, {'persistent': PROJECT_STEP_TRANSIENT_PAUSE, 'retry': 0, 'pause': 0, 'setup': 0,
                                 'db_wait': 0, 'db_retry': 0,
                                 'no_progress': PROJECT_STEP_TRANSIENT_PAUSE, 'changed_recovery': 0,
                                 'pause_record': 0, 'db_setup': 0,
                                 'setup_retry': 0, 'collection': 0, 'collection_error': 0, 'error_record': 0}[failure])
        self.assertEqual(runtime.get('A', 'G-004').status, 'PR_OPEN')
        self.assertEqual(runtime.get('A', 'G-002').status, 'PR_OPEN')
        if failure in {'pause_record', 'collection', 'collection_error', 'error_record'}:
            self.assertEqual(len(storage_attempts), 3 if failure == 'collection_error' else 4)
            self.assertEqual(runtime.get('A', 'G-001').status, 'PR_OPEN')
        if failure == 'setup_retry':
            self.assertEqual(len(setup_attempts), 2)
        if failure == 'pause':
            self.assertEqual(runtime.get('A', 'G-001').status, 'PR_OPEN')
            self.assertIn('OperationalError: database is locked', runtime.get('A', 'G-001').last_error)
            self.assertIn('Traceback (most recent call last)', runtime.get('A', 'G-001').last_error)



    def test_completed_block_recheck_is_not_repeated_when_other_work_fills_capacity(self):
        from loop_manager_internal.application.project_service import ProjectIdentity, ProjectObservations, ProjectService
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='Keep question',
                           release_fingerprint='same-input', clear_projection=False)
        calls, full_observations = [], []
        def recheck(*_args, plan_id, **_kwargs):
            calls.append(plan_id)
            return False
        def count(*_args, **_kwargs):
            # Other work can fill the management slots after this check ends.
            if calls and not full_observations:
                full_observations.append(True)
                return self.config.max_tasks_per_project
            return 0
        service = ProjectService(SimpleNamespace(activate_ready_execplan=mock.Mock(return_value=None),
            resume_manual_work=None, resume_plan_blocks=recheck),
            ProjectIdentity(self.connection, self.directory, 'A', self.config_path, None, False, None,
                            True, threading.Lock(), 20, 600, None))
        service.state = ProjectObservations(Path('A'), repo='A', base='main', base_sha='base')
        token = scheduling.current_scheduler.set(self.scheduler())
        try:
            with fast_mode_scope(self.config), mock.patch.object(scheduling, 'managed_count', side_effect=count):
                self.assertEqual(scheduling.run_project_tasks(service, newly_activated=False), 0)
        finally:
            scheduling.current_scheduler.reset(token)
        self.assertEqual(calls, ['G-001'])
        self.assertEqual(full_observations, [True])
        self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')

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
        observed = SimpleNamespace(fingerprint='owned-original-head', files={})
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
        requirement = 'docs/requirements/[R-001] Shared.md'
        self.connection.execute("UPDATE exec_plans SET sources=? WHERE repo='A' AND plan_id='G-001'",
                                (json.dumps([requirement]),))
        self.connection.commit()
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
                    if args[0] == 'rev-parse':
                        return SimpleNamespace(returncode=0, stdout='latest')
                    if args[0] == 'show':
                        revision, path = args[1].split(':', 1)
                        if path == requirement:
                            return SimpleNamespace(returncode=0, stdout=latest if revision == 'latest' else old)
                        if path == 'docs/ARCHITECTURE.md':
                            return SimpleNamespace(returncode=0, stdout='Architecture: retain the safety boundary.')
                        if path == plan['path']:
                            return SimpleNamespace(returncode=0, stdout='Plan body remains unchanged.')
                        return SimpleNamespace(returncode=1, stdout='')
                    if args[0] == 'ls-tree':
                        return SimpleNamespace(returncode=0, stdout='')
                    raise AssertionError(args)
                result = dict(resolved=case not in {'remaining'}, detail='cause assessment',
                    evidence=[dict(source=plan['path'] if case == 'only_plan_evidence' else requirement,
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
                        payload = json.loads(agent.call_args.args[5].splitlines()[-1])
                        self.assertFalse(payload['source_presence']['docs/PRODUCT.md'])
                        stored = checkpoint.get('A', 'G-001').snapshot['user_decision_assessment']
                        self.assertEqual(stored['question'], 'question ' + case)
                self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001')), block)
                self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')

    def test_user_question_recheck_tracks_optional_product_presence_in_identity(self):
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        checkpoint = ExecutionCheckpointRepository(self.connection)
        requirement = 'docs/requirements/[R-001] Shared.md'
        self.connection.execute("UPDATE exec_plans SET sources=? WHERE repo='A' AND plan_id='G-001'",
                                (json.dumps([requirement]),))
        self.connection.commit()
        runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='existing question',
            release_fingerprint='old', clear_projection=False, plan_values={'worktree': 'one', 'active_sha': 'before'})
        plan = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
        product = 'Product policy: defer the optional integration.'
        requirement_text = 'Requirement: ordinary calls are sufficient.'
        architecture_text = 'Architecture: preserve read-only assessment.'
        def git(_repo, *args, **_options):
            if args[0] == 'rev-parse':
                return SimpleNamespace(returncode=0, stdout='current')
            if args[0] == 'show':
                revision, path = args[1].split(':', 1)
                if path == 'docs/PRODUCT.md' and revision == 'before':
                    return SimpleNamespace(returncode=1, stdout='')
                if path == 'docs/PRODUCT.md':
                    return SimpleNamespace(returncode=0, stdout=product)
                if path == requirement:
                    return SimpleNamespace(returncode=0, stdout=requirement_text)
                if path == 'docs/ARCHITECTURE.md':
                    return SimpleNamespace(returncode=0, stdout=architecture_text)
                if path == plan['path']:
                    return SimpleNamespace(returncode=0, stdout='Plan body unchanged.')
                return SimpleNamespace(returncode=1, stdout='')
            if args[0] == 'ls-tree':
                return SimpleNamespace(returncode=0, stdout='')
            raise AssertionError(args)
        decision = dict(resolved=True, detail='current sources resolve the question', evidence=[
            {'source': 'docs/PRODUCT.md', 'quote': product},
            {'source': 'docs/ARCHITECTURE.md', 'quote': architecture_text},
        ])
        agent = mock.Mock(return_value=(0, 'USER_DECISION_ASSESSMENT: ' + json.dumps(decision)))
        effects = SimpleNamespace(git=git, capture_git_snapshot=mock.Mock(), run_agent=agent,
                                  validate_agent_scope=mock.Mock())
        first = _assess_user_decision_cause(effects, self.connection, Path('A'), self.directory,
                                            'A', plan, self.config_path)
        self.assertTrue(first[0])
        prompt = agent.call_args.args[5]
        payload = json.loads(prompt.splitlines()[-1])
        self.assertTrue(payload['source_presence']['docs/PRODUCT.md'])
        self.assertTrue(payload['source_presence'][requirement])
        self.assertIn('+Product policy: defer the optional integration.', payload['changes']['docs/PRODUCT.md'])
        first_assessment = checkpoint.get('A', 'G-001').snapshot['user_decision_assessment']
        self.assertEqual(_assess_user_decision_cause(effects, self.connection, Path('A'), self.directory,
                         'A', plan, self.config_path), first)
        agent.assert_called_once()

        self.connection.execute("UPDATE exec_plans SET active_sha='prior-with-product' WHERE repo='A' AND plan_id='G-001'")
        self.connection.commit()
        prior_product = 'Old product policy.'
        original_git = git
        def changed_prior_git(_repo, *args, **_options):
            if args[0] == 'show' and args[1] == 'prior-with-product:docs/PRODUCT.md':
                return SimpleNamespace(returncode=0, stdout=prior_product)
            return original_git(_repo, *args, **_options)
        effects.git = changed_prior_git
        changed_plan = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
        self.assertTrue(_assess_user_decision_cause(effects, self.connection, Path('A'), self.directory,
                         'A', changed_plan, self.config_path)[0])
        self.assertEqual(agent.call_count, 2)
        second_assessment = checkpoint.get('A', 'G-001').snapshot['user_decision_assessment']
        self.assertNotEqual(first_assessment['identity'], second_assessment['identity'])

    def test_user_question_recheck_keeps_deleted_product_from_becoming_evidence(self):
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        checkpoint = ExecutionCheckpointRepository(self.connection)
        requirement = 'docs/requirements/[R-001] Shared.md'
        self.connection.execute("UPDATE exec_plans SET sources=? WHERE repo='A' AND plan_id='G-001'",
                                (json.dumps([requirement]),))
        self.connection.commit()
        runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='existing question',
            release_fingerprint='old', clear_projection=False, plan_values={'worktree': 'one', 'active_sha': 'before'})
        plan = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
        saved = checkpoint.get('A', 'G-001')
        product = 'Prior product wording.'
        requirement_text = 'Same requirement content.'
        architecture_text = 'Same architecture content.'
        def git(_repo, *args, **_options):
            if args[0] == 'rev-parse':
                return SimpleNamespace(returncode=0, stdout='current')
            if args[0] == 'show':
                revision, path = args[1].split(':', 1)
                if path == 'docs/PRODUCT.md' and revision == 'current':
                    return SimpleNamespace(returncode=1, stdout='')
                if path == 'docs/PRODUCT.md':
                    return SimpleNamespace(returncode=0, stdout=product)
                if path == requirement:
                    return SimpleNamespace(returncode=0, stdout=requirement_text)
                if path == 'docs/ARCHITECTURE.md':
                    return SimpleNamespace(returncode=0, stdout=architecture_text)
                if path == plan['path']:
                    return SimpleNamespace(returncode=0, stdout='Plan body unchanged.')
                return SimpleNamespace(returncode=1, stdout='')
            if args[0] == 'ls-tree':
                return SimpleNamespace(returncode=0, stdout='')
            raise AssertionError(args)
        resolved = dict(resolved=True, detail='deletion is not an approval', evidence=[
            {'source': 'docs/PRODUCT.md', 'quote': product},
        ])
        agent = mock.Mock(return_value=(0, 'USER_DECISION_ASSESSMENT: ' + json.dumps(resolved)))
        effects = SimpleNamespace(git=git, capture_git_snapshot=mock.Mock(), run_agent=agent,
                                  validate_agent_scope=mock.Mock())
        with self.assertRaises(RuntimeError):
            _assess_user_decision_cause(effects, self.connection, Path('A'), self.directory,
                                        'A', plan, self.config_path)
        payload = json.loads(agent.call_args.args[5].splitlines()[-1])
        self.assertFalse(payload['source_presence']['docs/PRODUCT.md'])
        self.assertIn('-Prior product wording.', payload['changes']['docs/PRODUCT.md'])
        self.assertEqual(checkpoint.get('A', 'G-001'), saved)
        self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))['evidence'],
                         'existing question')

    def test_user_question_recheck_does_not_hide_required_sources_or_git_tree_failures(self):
        from loop_manager_internal.git import command_adapter
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        checkpoint = ExecutionCheckpointRepository(self.connection)
        requirement = 'docs/requirements/[R-001] Shared.md'
        cases = ('declared_product_missing', 'architecture_missing', 'requirement_missing',
                 'ambiguous_requirement', 'product_tree_failure')
        for case in cases:
            with self.subTest(case=case):
                sources = ['docs/PRODUCT.md'] if case == 'declared_product_missing' else [requirement]
                self.connection.execute("UPDATE exec_plans SET sources=? WHERE repo='A' AND plan_id='G-001'",
                                        (json.dumps(sources),))
                self.connection.commit()
                runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='question ' + case,
                    release_fingerprint='old', clear_projection=False,
                    plan_values={'worktree': 'one', 'active_sha': 'before'})
                plan = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
                saved = checkpoint.get('A', 'G-001')
                def git(_repo, *args, **_options):
                    if args[0] == 'rev-parse':
                        return SimpleNamespace(returncode=0, stdout='latest')
                    if args[0] == 'show':
                        revision, path = args[1].split(':', 1)
                        missing = (
                            (case == 'declared_product_missing' and path == 'docs/PRODUCT.md')
                            or (case == 'architecture_missing' and path == 'docs/ARCHITECTURE.md')
                            or (case in {'requirement_missing', 'ambiguous_requirement'} and path == requirement)
                            or (case == 'product_tree_failure' and path == 'docs/PRODUCT.md')
                        )
                        if missing:
                            return SimpleNamespace(returncode=1, stdout='')
                        if path == requirement:
                            return SimpleNamespace(returncode=0, stdout='Requirement: source is present.')
                        if path == 'docs/ARCHITECTURE.md':
                            return SimpleNamespace(returncode=0, stdout='Architecture: source is present.')
                        if path == plan['path']:
                            return SimpleNamespace(returncode=0, stdout='Plan body unchanged.')
                        return SimpleNamespace(returncode=1, stdout='')
                    if args[0] == 'ls-tree':
                        failed_product_probe = case == 'product_tree_failure' and args[-1] == 'docs/PRODUCT.md'
                        return SimpleNamespace(returncode=1 if failed_product_probe else 0, stdout='')
                    raise AssertionError(args)
                agent = mock.Mock()
                effects = SimpleNamespace(git=git, capture_git_snapshot=mock.Mock(), run_agent=agent,
                                          validate_agent_scope=mock.Mock())
                lookup_documents = ({
                    'docs/requirements/[R-001] First.md': '# [R-001] First\nFirst candidate.\n',
                    'docs/requirements/[R-001] Second.md': '# [R-001] Second\nSecond candidate.\n',
                } if case == 'ambiguous_requirement' else {})
                with mock.patch.object(command_adapter, 'revision_requirement_documents',
                                       return_value=lookup_documents):
                    with self.assertRaises(RuntimeError):
                        _assess_user_decision_cause(effects, self.connection, Path('A'), self.directory,
                                                    'A', plan, self.config_path)
                self.assertEqual(checkpoint.get('A', 'G-001'), saved)
                self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')
                agent.assert_not_called()

    def test_user_question_recheck_uses_confirmed_indirect_requirement_and_rejects_races(self):
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage import planning_review_state as reviews
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='original choice',
            release_fingerprint='unchanged', clear_projection=False,
            plan_values={'worktree': 'one', 'active_sha': 'before', 'worker_session_id': 'kept-session'})
        plan = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
        source = 'docs/requirements/[R-002] New policy.md'
        text = '# [R-002] New policy\n\nOrdinary calls and mock checks are sufficient.\n'
        additional_source = 'docs/requirements/[R-003] Related validation.md'
        additional_text = '# [R-003] Related validation\n\nRetain failure diagnostics.\n'
        requirements = [dict(source=source, reason='defines sufficient validation for this task',
                             quote='Ordinary calls and mock checks are sufficient.')]
        inputs = 'BLOCKED_PLAN_INPUTS: ' + json.dumps(dict(revision='latest', plans=[
            dict(plan_id='G-001', requirements=requirements)]))
        reviews.begin(self.connection, 'A', purpose='delta', revision='latest', input_fingerprint='input',
                      specification_fingerprint='spec', work_cycle='cycle', comparison_revision='before')
        reviews.validate(self.connection, 'A', 'latest', inputs, 'reviewed')
        self.connection.commit()
        observed_revision, race = 'latest', None
        store = ExecutionCheckpointRepository(self.connection)
        def git(_repo, *args, **_options):
            self.assertFalse(self.connection.in_transaction)
            if args[0] == 'rev-parse':
                return SimpleNamespace(returncode=0, stdout=observed_revision)
            if args[0] in {'merge-base', 'diff', 'ls-tree'}:
                return SimpleNamespace(returncode=0, stdout='')
            if args[0] == 'show':
                revision, path = args[1].split(':', 1)
                if path in {source, additional_source}:
                    return SimpleNamespace(returncode=1 if revision == 'before' else 0,
                                           stdout='' if revision == 'before' else text if path == source else additional_text)
                return SimpleNamespace(returncode=0, stdout='Existing plan and architecture.')
            raise AssertionError(args)
        def run_agent(*_args, **_kwargs):
            nonlocal observed_revision
            self.assertFalse(self.connection.in_transaction)
            if race == 'remote':
                observed_revision = 'advanced'
            elif race == 'checkpoint':
                current = store.get('A', 'G-001')
                with self.connection:
                    store.record('A', 'G-001', expected_generation=current.generation, operation=current.operation,
                        expected_head='concurrent-head', target_base_oid=current.target_base_oid,
                        observed_remote_oid=current.observed_remote_oid, snapshot=current.snapshot)
            elif race == 'question':
                runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='concurrent question',
                    release_fingerprint='unchanged', clear_projection=False)
            return 0, 'USER_DECISION_ASSESSMENT: ' + json.dumps(dict(resolved=True, detail='policy resolves choice',
                evidence=[dict(source=source, quote=requirements[0]['quote'])]))
        effects = SimpleNamespace(git=git, capture_git_snapshot=mock.Mock(), run_agent=mock.Mock(side_effect=run_agent),
                                  validate_agent_scope=mock.Mock())
        def assess():
            return _assess_user_decision_cause(effects, self.connection, Path('A'), self.directory,
                                             'A', plan, self.config_path)
        self.assertFalse(assess()[0])  # Push/DB application is not confirmed yet.
        effects.run_agent.assert_not_called()
        self.assertEqual(reviews.reassessment(self.connection, 'A', 'G-001'), {})
        with self.connection:
            reviews.confirm(self.connection, 'A', 'latest', 'latest')
        self.assertEqual(reviews.reassessment(self.connection, 'A', 'G-002'), {})
        first = store.get('A', 'G-001')
        for race in ('remote', 'checkpoint', 'question'):
            with self.subTest(race=race):
                before = dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
                with self.assertRaises(RuntimeError):
                    assess()
                self.assertNotIn('user_decision_assessment', store.get('A', 'G-001').snapshot)
                after = dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
                if race == 'question':
                    self.assertEqual(after['evidence'], 'concurrent question')
                    self.assertEqual(after['created_at'], before['created_at'])
                else:
                    self.assertEqual(after, before)
                observed_revision = 'latest'
                runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='original choice',
                    release_fingerprint='unchanged', clear_projection=False)
        race = None
        # Fixture writers above legitimately change updated_at. From this
        # restored state, read-only assessments must preserve the entire block.
        block = dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
        self.assertEqual(block['evidence'], 'original choice')
        self.assertTrue(assess()[0])
        self.assertTrue(assess()[0])
        self.assertEqual(effects.run_agent.call_count, 4)
        previous_identity = store.get('A', 'G-001').snapshot['user_decision_assessment']['identity']
        with self.connection:
            reviews.begin(self.connection, 'A', purpose='delta', revision=observed_revision,
                input_fingerprint='related-input', specification_fingerprint='other-spec',
                work_cycle='cycle', comparison_revision='latest')
            reviews.validate(self.connection, 'A', observed_revision,
                'BLOCKED_PLAN_INPUTS: ' + json.dumps(dict(revision=observed_revision, plans=[dict(
                    plan_id='G-001', requirements=[dict(source=additional_source,
                        reason='adds an applicable validation constraint', quote='Retain failure diagnostics.')])])), 'reviewed')
            reviews.confirm(self.connection, 'A', observed_revision, observed_revision)
        self.assertTrue(assess()[0])
        self.assertTrue(assess()[0])
        self.assertEqual(effects.run_agent.call_count, 5)

        self.assertNotEqual(store.get('A', 'G-001').snapshot['user_decision_assessment']['identity'], previous_identity)
        prompt = effects.run_agent.call_args.args[5]
        payload = json.loads(prompt.splitlines()[-1])
        self.assertEqual(payload['related_source_texts'][source], text)
        self.assertEqual(payload['related_requirements'][0]['reason'], requirements[0]['reason'])
        self.assertEqual(payload['planning_confirmation']['confirmed_revision'], 'latest')
        self.assertFalse(payload['prior_source_presence'][source])
        self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001')), block)
        self.assertEqual(runtime.get('A', 'G-001').worker_session_id, 'kept-session')
        self.assertGreater(store.get('A', 'G-001').generation, first.generation)
        # An unrelated, confirmed Requirement change does not repeat the model.
        observed_revision = 'after-unrelated-change'
        with self.connection:
            reviews.begin(self.connection, 'A', purpose='delta', revision=observed_revision,
                input_fingerprint='other-input', specification_fingerprint='other-spec',
                work_cycle='cycle', comparison_revision='latest')
            reviews.validate(self.connection, 'A', observed_revision,
                'BLOCKED_PLAN_INPUTS: ' + json.dumps(dict(revision=observed_revision, plans=[])), 'reviewed')
            reviews.confirm(self.connection, 'A', observed_revision, observed_revision)
        self.assertTrue(assess()[0])
        self.assertEqual(effects.run_agent.call_count, 5)

    def test_confirmed_question_resume_preserves_sessions_failure_audit_and_previous_results(self):
        from loop_manager_internal.recovery.blocked_plan_service import _requeue_blocked_execplan
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='original question',
            release_fingerprint='old', clear_projection=False, plan_values={
                'worker_session_id': 'worker-session', 'reviewer_session_id': 'reviewer-session',
                'worker_result': 'previous work', 'verify_result': 'old verify', 'verify_token': 'old-token',
                'verify_failure_history': '{"failure":3}', 'effective_worker_level': 6})
        independent = runtime.get('A', 'G-002')
        checkpoints = ExecutionCheckpointRepository(self.connection)
        confirmation = dict(revision='reviewed', confirmed_revision='published', requirements=[])
        with self.connection:
            checkpoints.record('A', 'G-001', expected_generation=0, operation=None, expected_head='candidate',
                target_base_oid='published', observed_remote_oid='candidate', snapshot={
                    'planning_reassessment': confirmation, 'user_decision_assessment': dict(
                        resolved=True, question='original question', planning_confirmation=confirmation)})
        _requeue_blocked_execplan(SimpleNamespace(), self.connection, 'A', 'G-001',
                                 preserve_execution=True, target_base_oid='published')
        resumed = runtime.get('A', 'G-001')
        self.assertEqual(resumed.status, 'IMPLEMENTING')
        self.assertEqual(resumed.worker_session_id, 'worker-session')
        self.assertEqual(resumed.reviewer_session_id, 'reviewer-session')
        audit = self.connection.execute("SELECT effective_worker_level, verify_failure_history FROM exec_plans "
                                        "WHERE repo='A' AND plan_id='G-001'").fetchone()
        self.assertEqual(audit['effective_worker_level'], 6)
        self.assertEqual(audit['verify_failure_history'], '{"failure":3}')
        self.assertIsNone(resumed.verify_token)
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
        self.assertEqual(checkpoints.get('A', 'G-001').snapshot['planning_resume_artifacts']['worker_result'], 'previous work')
        self.assertEqual(runtime.get('A', 'G-002'), independent)
        runtime.block_plan('A', 'G-001', reason_code='user_decision_required', evidence='new question',
                           release_fingerprint='new', clear_projection=False)
        with self.assertRaises(RuntimeError):
            _requeue_blocked_execplan(SimpleNamespace(), self.connection, 'A', 'G-001',
                                     preserve_execution=True, target_base_oid='published')
        self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')

    def test_user_question_recheck_distinguishes_new_requirement_from_unreadable_source(self):
        from loop_manager_internal.git import command_adapter
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        checkpoints = ExecutionCheckpointRepository(self.connection)
        requirement = 'docs/requirements/[R-002] New policy.md'
        text = '# [R-002] New policy\n\nOrdinary calls and mock checks are sufficient.\n'
        self.connection.execute("UPDATE exec_plans SET sources=? WHERE repo='A' AND plan_id='G-001'",
                                (json.dumps([requirement]),))
        self.connection.commit()
        for case in ('new', 'current_missing', 'prior_ambiguous', 'prior_tree_failure'):
            with self.subTest(case=case):
                runtime.block_plan('A', 'G-001', reason_code='user_decision_required',
                    evidence='original question ' + case, release_fingerprint='old', clear_projection=False,
                    plan_values={'worktree': 'one', 'active_sha': 'before'})
                plan = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
                saved = checkpoints.get('A', 'G-001')
                block = dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001'))
                def git(_repo, *args, **_options):
                    if args[0] == 'rev-parse':
                        return SimpleNamespace(returncode=0, stdout='latest')
                    if args[0] == 'show':
                        revision, path = args[1].split(':', 1)
                        if path == requirement:
                            return SimpleNamespace(returncode=0 if revision == 'latest' and case != 'current_missing' else 1,
                                                   stdout=text if revision == 'latest' and case != 'current_missing' else '')
                        return SimpleNamespace(returncode=0, stdout='Unchanged plan and architecture.')
                    if args[0] == 'ls-tree':
                        if case == 'prior_tree_failure' and 'before' in args:
                            raise RuntimeError('historical tree lookup failed')
                        return SimpleNamespace(returncode=0, stdout='')
                    raise AssertionError(args)
                result = dict(resolved=True, detail='new current policy resolves the question',
                              evidence=[dict(source=requirement, quote='Ordinary calls and mock checks are sufficient.')])
                agent = mock.Mock(return_value=(0, 'USER_DECISION_ASSESSMENT: ' + json.dumps(result)))
                effects = SimpleNamespace(git=git, capture_git_snapshot=mock.Mock(), run_agent=agent,
                                          validate_agent_scope=mock.Mock())
                def documents(_git, _repo, revision):
                    if revision == 'before' and case == 'prior_ambiguous':
                        return {f'docs/requirements/[R-002] {name}.md': text for name in ('First', 'Second')}
                    return {}
                def assess():
                    return _assess_user_decision_cause(effects, self.connection, Path('A'), self.directory,
                                                     'A', plan, self.config_path)
                with mock.patch.object(command_adapter, 'revision_requirement_documents', side_effect=documents):
                    if case == 'new':
                        self.assertTrue(assess()[0])
                        self.assertTrue(assess()[0])
                        agent.assert_called_once()
                        payload = json.loads(agent.call_args.args[5].splitlines()[-1])
                        self.assertFalse(payload['prior_source_presence'][requirement])
                        self.assertTrue(payload['source_presence'][requirement])
                        self.assertIn('--- /dev/null', payload['changes'][requirement])
                        stored = checkpoints.get('A', 'G-001').snapshot['user_decision_assessment']
                        self.assertEqual(stored['prior_revision'], 'before')
                        self.assertFalse(stored['prior_source_presence'][requirement])
                    else:
                        with self.assertRaises(RuntimeError):
                            assess()
                        agent.assert_not_called()
                        self.assertEqual(checkpoints.get('A', 'G-001'), saved)
                self.assertEqual(dict(scoped_blocking.plan_block(self.connection, 'A', 'G-001')), block)
                self.assertEqual(runtime.get('A', 'G-001').status, 'PLAN_BLOCKED')

    def test_user_question_recheck_resolves_renamed_requirement_by_existing_r_id(self):
        from loop_manager_internal.git import command_adapter
        from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause
        requirement = 'docs/requirements/[R-001] Shared.md'
        renamed = 'docs/requirements/[R-001] Renamed.md'
        requirement_text = '# [R-001] Renamed\n\nThe current policy explicitly keeps ordinary calls available.\n'
        self.connection.execute("UPDATE exec_plans SET sources=? WHERE repo='A' AND plan_id='G-001'",
                                (json.dumps([requirement]),))
        self.connection.commit()
        runtime_state.RuntimeStateRepository(self.connection).block_plan(
            'A', 'G-001', reason_code='user_decision_required', evidence='existing question',
            release_fingerprint='old', clear_projection=False,
            plan_values={'worktree': 'one', 'active_sha': 'before'})
        plan = self.connection.execute("SELECT * FROM exec_plans WHERE repo='A' AND plan_id='G-001'").fetchone()
        architecture_text = 'Architecture: preserve the existing assessment gate.'
        def git(_repo, *args, **_options):
            if args[0] == 'rev-parse':
                return SimpleNamespace(returncode=0, stdout='latest')
            if args[0] == 'show':
                revision, path = args[1].split(':', 1)
                if path == requirement and revision == 'before':
                    return SimpleNamespace(returncode=0, stdout='Old requirement wording.')
                if path == requirement or path == 'docs/PRODUCT.md':
                    return SimpleNamespace(returncode=1, stdout='')
                if path == 'docs/ARCHITECTURE.md':
                    return SimpleNamespace(returncode=0, stdout=architecture_text)
                if path == plan['path']:
                    return SimpleNamespace(returncode=0, stdout='Plan body unchanged.')
                return SimpleNamespace(returncode=1, stdout='')
            if args[0] == 'ls-tree':
                return SimpleNamespace(returncode=0, stdout='')
            raise AssertionError(args)
        result = dict(resolved=True, detail='the renamed active Requirement resolves the question', evidence=[
            {'source': requirement, 'quote': 'The current policy explicitly keeps ordinary calls available.'},
            {'source': 'docs/ARCHITECTURE.md', 'quote': architecture_text},
        ])
        effects = SimpleNamespace(git=git, capture_git_snapshot=mock.Mock(),
            run_agent=mock.Mock(return_value=(0, 'USER_DECISION_ASSESSMENT: ' + json.dumps(result))),
            validate_agent_scope=mock.Mock())
        with mock.patch.object(command_adapter, 'revision_requirement_documents',
                               return_value={renamed: requirement_text}) as documents:
            resolved, _detail, _stage = _assess_user_decision_cause(
                effects, self.connection, Path('A'), self.directory, 'A', plan, self.config_path)
        self.assertTrue(resolved)
        documents.assert_called_once()
        self.assertEqual(documents.call_args.args[2], 'latest')



class ExecutionClockTests(unittest.TestCase):
    def test_invalid_wait_records_reject_even_an_unlimited_budget(self):
        records = ('{', '[]', '{}', '{"waiting_seconds": true}',
                   '{"waiting_seconds": -1}', '{"waiting_seconds": NaN}',
                   '{"waiting_seconds": Infinity}', '{"waiting_seconds": "30"}',
                   '{"waiting_seconds": ' + '9' * 400 + '}')
        for content in records:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                budget = ExecutionClock({BUDGET_DIRECTORY: directory}, clock=lambda: 0)
                path = Path(directory) / 'session.json'
                path.write_text(content)
                with self.assertRaisesRegex(VerifyBudgetError, 'session.json'):
                    budget.remaining(None)
                self.assertEqual(path.read_text(), content)
                evidence, = Path(directory).glob('error-*')
                self.assertEqual((evidence / 'session.json').read_text(), content)
                # A later coordinator write cannot repair this clock's failed
                # observation or replace the retained failure evidence.
                path.write_text('{"waiting_seconds": 0}')
                with self.assertRaises(VerifyBudgetError):
                    budget.remaining(10)
                self.assertEqual((evidence / 'session.json').read_text(), content)

    def test_wait_record_regression_disappearance_and_overflow_reject_success(self):
        for change in ('regress', 'remove', 'overflow'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                now = [0.0]
                budget = ExecutionClock({BUDGET_DIRECTORY: directory}, clock=lambda: now[0])
                path = Path(directory) / 'session.json'
                path.write_text(json.dumps({'waiting_seconds': 30}))
                now[0] = 32
                self.assertEqual(budget.remaining(10), 8)
                if change == 'remove':
                    path.unlink()
                elif change == 'regress':
                    path.write_text(json.dumps({'waiting_seconds': 29}))
                else:
                    path.write_text(json.dumps({'waiting_seconds': 1e308}))
                    (Path(directory) / 'second.json').write_text(json.dumps({'waiting_seconds': 1e308}))
                with self.assertRaises(VerifyBudgetError):
                    budget.remaining(10)
                evidence, = Path(directory).glob('error-*')
                self.assertEqual(json.loads((evidence / 'observed-records').read_text()), {'session.json': 30})

    def test_budget_error_preserves_owned_records_and_restores_clock_context(self):
        from loop_manager_internal.verification.execution_clock import current_execution_clock
        previous = current_execution_clock.get()
        budget = ExecutionClock({})
        path = budget.directory / 'session.json'
        try:
            with self.assertRaises(VerifyBudgetError):
                with budget:
                    path.write_text('{')
                    budget.remaining(10)
            self.assertEqual(path.read_text(), '{')
            self.assertIs(current_execution_clock.get(), previous)
        finally:
            budget.close()

    def test_wsl_lifecycle_is_not_a_wait_record(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'wsl-lifecycle.json').write_text('{"state": "running"}')
            budget = ExecutionClock({BUDGET_DIRECTORY: directory}, clock=lambda: 0)
            self.assertEqual(budget.remaining(10), 10)

    def test_missing_budget_directory_rejects_success_even_without_prior_records(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = ExecutionClock({BUDGET_DIRECTORY: str(Path(directory) / 'budget')})
            budget.directory.rmdir()
            with self.assertRaisesRegex(VerifyBudgetError, '디렉터리를 읽지 못했습니다'):
                budget.remaining(None)

    def test_shared_budget_counts_overlapping_session_wait_once_and_keeps_execution(self):
        now = [0.0]
        sampler = mock.Mock()
        sampler.sample.side_effect = lambda: Snapshot(now[0], 1, 1, 4096*MIB, 'fake')
        coordinator = VerifyCoordinator(sampler=sampler, clock=lambda: now[0])
        with tempfile.TemporaryDirectory() as directory:
            sessions = [coordinator.open(1) for _ in range(2)]
            for session, _ in sessions:
                coordinator.track_waiting(session, directory)
            with ExecutionClock({BUDGET_DIRECTORY: directory}, clock=lambda: now[0]) as budget:
                for step in range(301):
                    now[0] = step / 10
                    for session, secret in sessions:
                        self.assertFalse(coordinator.request(session, secret, 'acquire')['granted'])
                self.assertAlmostEqual(budget.remaining(10), 10)
                # Stopping sessions keeps their common cumulative record for
                # the next stage; it must not reset already consumed time.
                for session, _ in sessions:
                    coordinator.finish(session, stopped=True)
                now[0] = 32
                self.assertAlmostEqual(budget.remaining(10), 8)
                # A later session/stage in this coordinator keeps the prior
                # cumulative wait, while a nested clock starts at its own budget.
                session, secret = coordinator.open(1)
                coordinator.track_waiting(session, directory)
                with ExecutionClock({BUDGET_DIRECTORY: directory}, clock=lambda: now[0]) as nested:
                    for step in range(301):
                        now[0] = 32 + step / 10
                        self.assertFalse(coordinator.request(session, secret, 'acquire')['granted'])
                    self.assertAlmostEqual(budget.remaining(10), 8)
                    self.assertAlmostEqual(nested.remaining(8), 8)
                coordinator.finish(session, stopped=True)
                now[0] = 72
                self.assertEqual(budget.remaining(10), 0)
                self.assertAlmostEqual(budget.elapsed(), 12)
                self.assertEqual(len(list(Path(directory).glob('*.json'))), 1)

    def test_common_wait_requires_all_sessions_and_fresh_heartbeats(self):
        for state in ('preparing', 'running', 'uncertain', 'stale', 'other-budget'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                now = [0.0]
                busy = [0]
                sampler = mock.Mock()
                sampler.sample.side_effect = lambda: Snapshot(now[0], 1, busy[0], 4096*MIB, 'fake')
                coordinator = VerifyCoordinator(sampler=sampler, clock=lambda: now[0])
                sibling, sibling_secret = coordinator.open(1)
                session, secret = coordinator.open(1)
                coordinator.track_waiting(sibling, Path(directory) / 'other' if state == 'other-budget' else directory)
                coordinator.track_waiting(session, directory)
                if state in ('running', 'uncertain', 'other-budget'):
                    reservation = coordinator.request(sibling, sibling_secret, 'acquire')
                    self.assertTrue(reservation['granted'])
                    if state == 'uncertain':
                        coordinator.finish(sibling, stopped=False)
                busy[0] = 1
                with ExecutionClock({BUDGET_DIRECTORY: directory}, clock=lambda: now[0]) as budget:
                    if state == 'stale':
                        self.assertFalse(coordinator.request(sibling, sibling_secret, 'acquire')['granted'])
                    self.assertFalse(coordinator.request(session, secret, 'acquire')['granted'])
                    for step in range(1, 21):
                        now[0] = step / 10
                        self.assertFalse(coordinator.request(session, secret, 'acquire')['granted'])
                    if state == 'other-budget':
                        self.assertAlmostEqual(budget.elapsed(), 0)
                    elif state == 'stale':
                        # Only the first second has both live heartbeats.
                        self.assertAlmostEqual(budget.elapsed(), 1)
                    else:
                        self.assertAlmostEqual(budget.elapsed(), 2)
                for identity, _ in ((sibling, sibling_secret), (session, secret)):
                    coordinator.finish(identity, stopped=True)

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
