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
        scheduler = self.scheduler(max_workers=1)
        execute = self.connection.execute
        def fail_cursor(sql, *args, **kwargs):
            if 'INSERT INTO task_scheduling_cursor VALUES (?,?)' in sql:
                raise DatabaseWaitTimeout('cursor wait')
            return execute(sql, *args, **kwargs)
        with mock.patch.object(self.connection, 'execute', side_effect=fail_cursor):
            with self.assertRaises(DatabaseWaitTimeout), scheduler.slot(
                    'agent', scheduling.Task('A', 'G-001', self.connection)):
                self.fail('미확정 예약은 실행하면 안 됩니다.')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM task_call_reservations').fetchone()[0], 0)
        self.assertEqual(sum(scheduler.running['agent'].values()), 0)
        self.assertEqual(scheduler.retained, {})

    def test_reservation_db_wait_does_not_hold_scheduler_condition(self):
        from loop_manager_internal.storage import task_calls
        scheduler = self.scheduler(max_workers=1)
        entered, release, progressed = threading.Event(), threading.Event(), threading.Event()
        errors = []
        reserve = task_calls.reserve
        def waiting_reserve(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError('다른 작업의 배정기가 DB 대기 중 멈췄습니다.')
            return reserve(*args)
        def run():
            try:
                with closing(database.connect_database(self.directory, run_migrations=False)) as connection:
                    with scheduler.slot('agent', scheduling.Task('A', 'G-001', connection)):
                        pass
            except BaseException as error:
                errors.append(error)
        def progress():
            scheduler.requeue(scheduling.Task('B', 'G-003', None))
            progressed.set()
        with mock.patch.object(task_calls, 'reserve', side_effect=waiting_reserve):
            thread = threading.Thread(target=run)
            other = threading.Thread(target=progress)
            try:
                thread.start()
                self.assertTrue(entered.wait(3))
                other.start()
                self.assertTrue(progressed.wait(1))
                self.assertEqual(sum(scheduler.running['agent'].values()), 1)
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
                    scheduler.requeue(scheduling.Task('B', 'G-003', None))
                self.assertTrue(fetched.wait(2))
        finally:
            if thread.ident is not None:
                thread.join(3)
        self.assertEqual(errors, [])

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
