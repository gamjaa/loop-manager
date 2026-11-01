from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.application.contracts import ProjectRunKind, ProjectRunResult
from loop_manager_internal.application.commands import recover_local_environment
from loop_manager_internal.application.project_runner import (
    InternalRunProjectResultEffects,
    _run_project_result,
)
from loop_manager_internal.application.project_service import ProjectIdentity, ProjectService
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind
from loop_manager_internal.runtime.project_registry import RegisteredProject, add_project
from loop_manager_internal.runtime.process_lock import process_lock, _held_process_lock_identity
from loop_manager_internal.storage import blocked_checkpoint_state, local_recovery_state
from loop_manager_internal.storage import runtime_state
from tests.support.database import runtime_connection


class LocalEnvironmentRecoveryCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.repository = self.root / "registered"
        self.repository.mkdir()
        with closing(loop_manager.connect_database(self.state_dir, create=True)) as connection:
            add_project(connection, RegisteredProject(
                repo=str(self.repository.resolve()), base_branch="main", provider="codex",
                agents_hash=None, agents_approved=False,
            ))
        self.config_path = self.root / "config.ini"
        self.arguments = loop_manager.build_parser().parse_args([
            "recover", str(self.repository),
        ])

    def effects(self, run_project_result):
        return SimpleNamespace(
            connect_database=loop_manager.connect_database,
            load_managed_config=loop_manager.load_managed_config,
            operation_print=mock.Mock(),
            run_project_result=run_project_result,
        )

    def test_recheck_consumption_rejects_other_repo_and_plan(self):
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.begin_request(connection, repo, 'G-001')
            for other_repo, subject in (('other', 'G-001'), (repo, 'G-002'), (repo, 'planning')):
                with self.subTest(repo=other_repo, subject=subject):
                    with self.assertRaises(local_recovery_state.LocalRecoveryRequestError):
                        local_recovery_state.consume_recheck(connection, request.request_id, subject, 'verify', repo=other_repo)
                    self.assertFalse(local_recovery_state.recheck_consumed(connection, request.request_id, subject, 'verify'))
            with connection:
                self.assertTrue(local_recovery_state.consume_recheck(connection, request.request_id, 'G-001', 'verify', repo=repo))
                self.assertFalse(local_recovery_state.consume_recheck(connection, request.request_id, 'G-001', 'verify', repo=repo))

    def test_recover_releases_database_access_during_external_execution(self):
        from loop_manager_internal.storage.db_access import open_connection
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        errors = []
        def run(connection, *args, **kwargs):
            self.assertFalse(connection.in_transaction)
            self.assertFalse(connection._held)
            def write():
                try:
                    with closing(open_connection(self.state_dir / 'loop-manager.db', timeout=.5)) as other, other:
                        other.execute('UPDATE projects SET last_error=?', ('independent',))
                except BaseException as error:
                    errors.append(error)
            thread = threading.Thread(target=write)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            return ProjectRunResult(ProjectRunKind.WAITING, 0)
        recover_local_environment(self.effects(run), self.arguments, self.state_dir, self.config_path)
        self.assertEqual(errors, [])

    def test_complete_request_retries_database_only_and_rejects_stale_observation(self):
        from loop_manager_internal.storage.db_access import (
            CoordinatedConnection, DatabaseWaitTimeout, open_connection,
        )
        from loop_manager_internal.storage.local_recovery_state import LocalRecoveryRequestError
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.begin_request(connection, repo, None)
            observed = local_recovery_state.snapshot(connection, repo, None)

        busy = threading.Event()

        class SignalBusyConnection(CoordinatedConnection):
            def execute(self, sql, parameters=()):
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError as error:
                    if (sql.strip().upper() == 'BEGIN IMMEDIATE'
                            and isinstance(error, DatabaseWaitTimeout)):
                        busy.set()
                    raise

        database = self.state_dir / 'loop-manager.db'
        blocker = sqlite3.connect(database, timeout=.5)
        blocker.execute('BEGIN IMMEDIATE')
        blocker.execute(
            "UPDATE projects SET planning_error='competing planning state' WHERE repo=?", (repo,),
        )
        outcome = []

        def finalize():
            with closing(open_connection(database, timeout=2, factory=SignalBusyConnection)) as owner:
                owner.row_factory = sqlite3.Row
                try:
                    outcome.append(local_recovery_state.complete_request(
                        owner, request, 'resolved', observed,
                    ))
                except BaseException as error:
                    outcome.append(error)

        worker = threading.Thread(target=finalize)
        worker.start()
        try:
            self.assertTrue(busy.wait(2), 'finalization did not encounter the held SQLite writer')
            blocker.commit()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()
            worker.join(3)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], LocalRecoveryRequestError)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            saved = connection.execute(
                'SELECT status,result,after_state FROM local_environment_recovery_requests WHERE request_id=?',
                (request.request_id,),
            ).fetchone()
            self.assertEqual(tuple(saved), ('pending', None, None))
            self.assertEqual(connection.execute(
                'SELECT planning_error FROM projects WHERE repo=?', (repo,),
            ).fetchone()[0], 'competing planning state')

    def test_complete_rejects_change_between_result_snapshot_and_finalization(self):
        from loop_manager_internal.application import commands as commands_app
        from loop_manager_internal.storage.db_access import open_connection
        from loop_manager_internal.storage.local_recovery_state import LocalRecoveryRequestError
        repo = str(self.repository.resolve())
        database = self.state_dir / 'loop-manager.db'
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.begin_request(connection, repo, None)
            run = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.COMPLETED, 0))
            effects = commands_app.LocalRecoveryExecutionEffects(
                operation_print=mock.Mock(), run_project_result=run,
            )
            complete = local_recovery_state.complete_request

            def compete_then_complete(db, current_request, result, after):
                self.assertEqual(result, 'resolved')
                self.assertIsNone(after['planning_error_sha256'])
                with closing(open_connection(database, timeout=2)) as other, other:
                    other.execute(
                        "UPDATE projects SET planning_error='new unresolved cause' WHERE repo=?",
                        (repo,),
                    )
                return complete(db, current_request, result, after)

            with mock.patch.object(
                    local_recovery_state, 'complete_request', side_effect=compete_then_complete):
                with self.assertRaisesRegex(LocalRecoveryRequestError, '결과 판단 뒤'):
                    commands_app.execute_local_recovery_request(
                        effects, connection, self.state_dir, request, self.config_path,
                        planner_mutex=threading.Lock(), refactoring_review_interval=20,
                        verify_timeout_seconds=600,
                    )

            run.assert_called_once()
            effects.operation_print.assert_not_called()
            saved = connection.execute(
                'SELECT status,result,after_state FROM local_environment_recovery_requests '
                'WHERE request_id=?', (request.request_id,),
            ).fetchone()
            self.assertEqual(tuple(saved), ('pending', None, None))
            self.assertEqual(connection.execute(
                'SELECT planning_error FROM projects WHERE repo=?', (repo,),
            ).fetchone()[0], 'new unresolved cause')

    def test_record_progress_preserves_pending_request_when_target_changed(self):
        from dataclasses import replace
        from loop_manager_internal.storage.local_recovery_state import LocalRecoveryRequestError
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.begin_request(connection, repo, None)
            observed = local_recovery_state.snapshot(connection, repo, None)
            with self.assertRaisesRegex(LocalRecoveryRequestError, '요청 대상 또는 상태'):
                local_recovery_state.record_progress(
                    connection, replace(request, plan_id='G-999'), observed,
                )
            connection.execute(
                "UPDATE projects SET planning_error='changed after observation' WHERE repo=?", (repo,),
            )
            connection.commit()
            with self.assertRaisesRegex(LocalRecoveryRequestError, '진행 관측 뒤 대상 상태'):
                local_recovery_state.record_progress(connection, request, observed)
            saved = connection.execute(
                'SELECT status,result,after_state FROM local_environment_recovery_requests WHERE request_id=?',
                (request.request_id,),
            ).fetchone()
            self.assertEqual(tuple(saved), ('pending', None, None))

    def add_recovery_plans(self, *plan_ids):
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            for plan_id in plan_ids:
                connection.execute(
                    "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
                    "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo, plan_id, f'docs/plans/{plan_id}.md', plan_id, '[]', '[]',
                     'normal', 'hash', 'READY', 'base'))
        return loop_manager.build_parser().parse_args(['recover', repo, '--plan', plan_ids[0]])

    def scheduled_recovery_run(self, run):
        config = loop_manager.load_managed_config(self.config_path)
        with process_lock(self.state_dir, identity='automatic-next-run'), \
             closing(loop_manager.connect_database(self.state_dir)) as connection, \
             mock.patch.object(loop_manager, 'run_project_result', side_effect=run), \
             mock.patch.object(loop_manager, 'operation_log'):
            return loop_manager.run_registered_projects(connection, self.state_dir, config)

    def test_busy_command_saves_and_reuses_request_without_project_mutation(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        run = mock.Mock()
        effects = self.effects(run)
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            before = dict(connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone())
        with process_lock(self.state_dir, identity='automatic-running'):
            for _ in range(2):
                self.assertEqual(recover_local_environment(effects, self.arguments, self.state_dir, self.config_path), 1)
            self.assertEqual(_held_process_lock_identity(self.state_dir), 'automatic-running')
        run.assert_not_called()
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.pending_request(connection, repo)
            self.assertIsNotNone(request)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM local_environment_recovery_requests').fetchone()[0], 1)
            self.assertEqual(dict(connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()), before)
        for call in effects.operation_print.call_args_list:
            self.assertIn(request.request_id, call.args[0])
            self.assertIn('다음 run', call.args[0])

    def test_next_run_consumes_saved_plan_request_once(self):
        self.config_path.write_text('[loop]\nverify_timeout_seconds = 937\n', encoding='utf-8')
        args = self.add_recovery_plans('G-001')
        with process_lock(self.state_dir, identity='automatic-running'):
            self.assertEqual(recover_local_environment(self.effects(mock.Mock()), args, self.state_dir, self.config_path), 1)
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.pending_request(connection, repo)
        def recheck(connection, state_dir, repository, config_path, *_args, **kwargs):
            self.assertEqual((state_dir, repository, config_path), (self.state_dir, repo, self.config_path))
            self.assertEqual(_held_process_lock_identity(self.state_dir), 'automatic-next-run')
            self.assertEqual(kwargs['recovery_request_id'], request.request_id)
            self.assertEqual(kwargs['external_run_id'], request.request_id)
            self.assertEqual(kwargs['recovery_plan_id'], 'G-001')
            self.assertEqual(kwargs['verify_timeout_seconds'], 937)
            self.assertIsInstance(kwargs['planner_mutex'], type(threading.Lock()))
            self.assertTrue(callable(kwargs['on_step_completed']))
            with connection:
                self.assertTrue(local_recovery_state.consume_recheck(
                    connection, request.request_id, 'G-001', 'verify',
                ))
            return ProjectRunResult(ProjectRunKind.WAITING, 0)
        # This case verifies scheduler request handoff; the injected run does not use verify transport.
        with mock.patch('loop_manager_internal.application.scheduler.VerifyCoordinator'):
            self.assertEqual(self.scheduled_recovery_run(recheck), 0)
            normal = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0))
            self.assertEqual(self.scheduled_recovery_run(normal), 0)
        self.assertNotIn('recovery_request_id', normal.call_args.kwargs)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertIsNone(local_recovery_state.pending_request(connection, repo))
            self.assertTrue(local_recovery_state.recheck_consumed(connection, request.request_id, 'G-001', 'verify'))
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM local_environment_recovery_requests').fetchone()[0], 1)

    def test_other_active_plan_progresses_and_keeps_recovery_pending(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        args = self.add_recovery_plans('G-001', 'G-002')
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            runtime_state.RuntimeStateRepository(connection).activate(
                repo, 'G-002', activation_sha='base', branch='branch', worktree='/worktree')
        immediate = mock.Mock()
        self.assertEqual(recover_local_environment(self.effects(immediate), args, self.state_dir, self.config_path), 1)
        immediate.assert_not_called()
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.pending_request(connection, repo)
        def finish_other(connection, *_args, **kwargs):
            self.assertNotIn('recovery_request_id', kwargs)
            runtime = runtime_state.RuntimeStateRepository(connection)
            self.assertEqual(runtime.active_row(repo)['plan_id'], 'G-002')
            runtime.transition_and_clear(repo, 'G-002', plan_values={'status': 'PR_OPEN'}, enqueue=False)
            return ProjectRunResult(ProjectRunKind.WAITING, 0)
        self.assertEqual(self.scheduled_recovery_run(finish_other), 0)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertEqual(local_recovery_state.pending_request(connection, repo).request_id, request.request_id)
            self.assertFalse(local_recovery_state.recheck_consumed(connection, request.request_id, 'G-001', 'verify'))
        resume = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0))
        self.assertEqual(self.scheduled_recovery_run(resume), 0)
        self.assertEqual(resume.call_args.kwargs['recovery_request_id'], request.request_id)
        self.assertEqual(resume.call_args.kwargs['recovery_plan_id'], 'G-001')

    def test_different_scopes_queue_and_invalid_config_does_not_execute(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        args = self.add_recovery_plans('G-001')
        run = mock.Mock()
        effects = self.effects(run)
        with process_lock(self.state_dir, identity='automatic-running'):
            self.assertEqual(recover_local_environment(effects, args, self.state_dir, self.config_path), 1)
            self.assertEqual(recover_local_environment(effects, self.arguments, self.state_dir, self.config_path), 1)
            self.config_path.write_text('[loop]\nverify_timeout_seconds = bad\n', encoding='utf-8')
            with self.assertRaises(loop_manager.ManagedConfigError):
                recover_local_environment(effects, args, self.state_dir, self.config_path)
        run.assert_not_called()
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            requests = local_recovery_state.pending_requests(connection, str(self.repository.resolve()))
            self.assertEqual([request.plan_id for request in requests], ['G-001', None])

    def test_all_scopes_queue_in_order_and_each_run_processes_one_request(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        first = self.add_recovery_plans('G-001', 'G-002')
        second = loop_manager.build_parser().parse_args(['recover', str(self.repository), '--plan', 'G-002'])
        immediate = mock.Mock()
        with process_lock(self.state_dir, identity='automatic-running'):
            for args in (first, second, self.arguments, second, self.arguments, first):
                self.assertEqual(recover_local_environment(self.effects(immediate), args, self.state_dir, self.config_path), 1)
        immediate.assert_not_called()
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            # UUID ordering must not reorder intake when timestamps are equal.
            connection.execute("UPDATE local_environment_recovery_requests SET created_at = '2026-10-07 00:00:00'")
            queued = local_recovery_state.pending_requests(connection, repo)
        self.assertEqual([request.plan_id for request in queued], ['G-001', 'G-002', None])
        calls = []
        def recheck(_connection, *_args, **kwargs):
            calls.append((kwargs['recovery_request_id'], kwargs['recovery_plan_id']))
            return ProjectRunResult(ProjectRunKind.WAITING, 0)
        for index, request in enumerate(queued):
            self.assertEqual(self.scheduled_recovery_run(recheck), 0)
            self.assertEqual(len(calls), index + 1)
            self.assertEqual(calls[-1], (request.request_id, request.plan_id))
            with closing(loop_manager.connect_database(self.state_dir)) as connection:
                self.assertEqual(len(local_recovery_state.pending_requests(connection, repo)), len(queued) - index - 1)
        normal = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0))
        self.assertEqual(self.scheduled_recovery_run(normal), 0)
        self.assertNotIn('recovery_request_id', normal.call_args.kwargs)

    def test_occupied_queue_head_does_not_block_recovery_of_active_plan(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        first = self.add_recovery_plans('G-001', 'G-002')
        second = loop_manager.build_parser().parse_args(['recover', str(self.repository), '--plan', 'G-002'])
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            runtime_state.RuntimeStateRepository(connection).activate(
                repo, 'G-002', activation_sha='base', branch='branch', worktree='/worktree')
        with process_lock(self.state_dir, identity='automatic-running'):
            for args in (first, second):
                recover_local_environment(self.effects(mock.Mock()), args, self.state_dir, self.config_path)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            queued = local_recovery_state.pending_requests(connection, repo)
        def recover_active(connection, *_args, **kwargs):
            self.assertEqual(kwargs['recovery_request_id'], queued[1].request_id)
            self.assertEqual(kwargs['recovery_plan_id'], 'G-002')
            runtime_state.RuntimeStateRepository(connection).transition_and_clear(
                repo, 'G-002', plan_values={'status': 'PR_OPEN'}, enqueue=False)
            return ProjectRunResult(ProjectRunKind.WAITING, 0)
        self.assertEqual(self.scheduled_recovery_run(recover_active), 0)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertEqual(local_recovery_state.pending_requests(connection, repo), (queued[0],))
            self.assertFalse(local_recovery_state.recheck_consumed(connection, queued[0].request_id, 'G-001', 'verify'))
        resume = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0))
        self.assertEqual(self.scheduled_recovery_run(resume), 0)
        self.assertEqual(resume.call_args.kwargs['recovery_request_id'], queued[0].request_id)

    def test_immediate_command_does_not_overtake_earlier_executable_request(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        first = self.add_recovery_plans('G-001', 'G-002')
        second = loop_manager.build_parser().parse_args(['recover', str(self.repository), '--plan', 'G-002'])
        with process_lock(self.state_dir, identity='automatic-running'):
            recover_local_environment(self.effects(mock.Mock()), first, self.state_dir, self.config_path)
        run = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0))
        self.assertEqual(recover_local_environment(self.effects(run), second, self.state_dir, self.config_path), 1)
        run.assert_not_called()
        self.assertEqual(recover_local_environment(self.effects(run), first, self.state_dir, self.config_path), 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs['recovery_plan_id'], 'G-001')
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertEqual([r.plan_id for r in local_recovery_state.pending_requests(connection, str(self.repository.resolve()))], ['G-002'])

    def test_simultaneous_intake_accepts_every_distinct_scope_and_deduplicates(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        first = self.add_recovery_plans('G-001', 'G-002')
        second = loop_manager.build_parser().parse_args(['recover', str(self.repository), '--plan', 'G-002'])
        run = mock.Mock()
        effects = self.effects(run)
        with process_lock(self.state_dir, identity='automatic-running'), ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(recover_local_environment, effects, args, self.state_dir, self.config_path)
                       for args in (first, second, self.arguments, first, second, self.arguments)]
            self.assertEqual([future.result(timeout=30) for future in futures], [1] * 6)
        run.assert_not_called()
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            queued = local_recovery_state.pending_requests(connection, str(self.repository.resolve()))
            self.assertEqual({request.plan_id for request in queued}, {'G-001', 'G-002', None})
            self.assertEqual(len(queued), 3)

    def test_legacy_single_request_migration_preserves_audit_and_enforces_scope_uniqueness(self):
        self.add_recovery_plans('G-001', 'G-002')
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            legacy = local_recovery_state.begin_request(connection, repo, 'G-001')
            local_recovery_state.consume_recheck(connection, legacy.request_id, 'G-001', 'verify')
            before = dict(connection.execute('SELECT * FROM local_environment_recovery_requests').fetchone())
            connection.execute('DROP INDEX one_pending_local_recovery_per_plan')
            connection.execute('DROP INDEX one_pending_project_local_recovery')
            connection.execute("CREATE UNIQUE INDEX one_pending_local_recovery_per_repo "
                               "ON local_environment_recovery_requests(repo) WHERE status = 'pending'")
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            second = local_recovery_state.begin_request(connection, repo, 'G-002')
            project = local_recovery_state.begin_request(connection, repo, None)
            self.assertEqual(local_recovery_state.begin_request(connection, repo, 'G-001').request_id, legacy.request_id)
            self.assertEqual(dict(connection.execute('SELECT * FROM local_environment_recovery_requests '
                                                    'WHERE request_id = ?', (legacy.request_id,)).fetchone()), before)
            self.assertTrue(local_recovery_state.recheck_consumed(connection, legacy.request_id, 'G-001', 'verify'))
            self.assertEqual([r.request_id for r in local_recovery_state.pending_requests(connection, repo)],
                             [legacy.request_id, second.request_id, project.request_id])
            for scope in ('G-002', None):
                with self.subTest(scope=scope), self.assertRaises(sqlite3.IntegrityError), connection:
                    connection.execute("INSERT INTO local_environment_recovery_requests "
                                       "(request_id, repo, plan_id, status, before_state) VALUES (?, ?, ?, 'pending', '{}')",
                                       ('duplicate', repo, scope))
            local_recovery_state.complete_request(
                connection, legacy, 'unresolved', local_recovery_state.snapshot(connection, repo, 'G-001'),
            )
            renewed = local_recovery_state.begin_request(connection, repo, 'G-001')
            self.assertNotEqual(renewed.request_id, legacy.request_id)
            self.assertFalse(local_recovery_state.recheck_consumed(connection, renewed.request_id, 'G-001', 'verify'))
            self.assertTrue(local_recovery_state.recheck_consumed(connection, legacy.request_id, 'G-001', 'verify'))

    def test_interrupted_scheduled_recheck_preserves_request_and_consumption(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        args = self.add_recovery_plans('G-001')
        repo = str(self.repository.resolve())
        with process_lock(self.state_dir, identity='automatic-running'):
            recover_local_environment(self.effects(mock.Mock()), args, self.state_dir, self.config_path)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.pending_request(connection, repo)
        def interrupted(connection, *_args, **kwargs):
            self.assertEqual(kwargs['recovery_request_id'], request.request_id)
            with connection:
                local_recovery_state.consume_recheck(connection, request.request_id, 'G-001', 'verify')
            raise RuntimeError('interrupted after durable check')
        def resumed(connection, *_args, **kwargs):
            self.assertEqual(kwargs['recovery_request_id'], request.request_id)
            self.assertTrue(local_recovery_state.recheck_consumed(connection, request.request_id, 'G-001', 'verify'))
            return ProjectRunResult(ProjectRunKind.WAITING, 0)
        # This case verifies recovery request persistence; verify transport is not exercised.
        with mock.patch('loop_manager_internal.application.scheduler.VerifyCoordinator'):
            self.assertEqual(self.scheduled_recovery_run(interrupted), 1)
            self.assertEqual(self.scheduled_recovery_run(resumed), 0)

    def test_selected_plan_waits_for_planning_and_maintenance_slots(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        args = self.add_recovery_plans('G-001')
        repo = str(self.repository.resolve())
        for owner in ('planning', 'maintenance'):
            with self.subTest(owner=owner):
                with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
                    connection.execute('UPDATE projects SET phase = ? WHERE repo = ?',
                                       ('PLANNING' if owner == 'planning' else None, repo))
                    connection.execute('INSERT INTO maintenance_cycles (repo, phase) VALUES (?, ?) '
                                       'ON CONFLICT(repo) DO UPDATE SET phase = excluded.phase',
                                       (repo, 'assessment' if owner == 'maintenance' else 'idle'))
                immediate = mock.Mock()
                self.assertEqual(recover_local_environment(self.effects(immediate), args, self.state_dir, self.config_path), 1)
                immediate.assert_not_called()
                normal = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0))
                self.assertEqual(self.scheduled_recovery_run(normal), 0)
                self.assertNotIn('recovery_request_id', normal.call_args.kwargs)
                with closing(loop_manager.connect_database(self.state_dir)) as connection:
                    self.assertIsNotNone(local_recovery_state.pending_request(connection, repo))

    def test_recover_runtime_child_uses_concurrent_ownership_validation(self):
        token, sha = 'a' * 32, 'b' * 40
        argv = ['loop_manager.py', '--state-dir', str(self.state_dir), '--runtime-child',
                '--runtime-source', str(self.repository), '--runtime-sha', sha,
                '--runtime-mutation-id', token, 'recover', str(self.repository)]
        with mock.patch.object(loop_manager.sys, 'argv', argv), \
             mock.patch.object(loop_manager, 'validate_runtime_child') as child, \
             mock.patch.object(loop_manager, 'validate_runtime_mutation', return_value=None) as mutation, \
             mock.patch.object(loop_manager, 'execute_runtime_command', return_value=1):
            self.assertEqual(loop_manager.main(), 1)
        child.assert_called_once_with(self.repository.resolve(), self.state_dir, sha, command_id=token)
        mutation.assert_called_once_with(self.repository.resolve(), self.state_dir, sha, token, concurrent=True)

    def test_plan_command_resolves_with_independent_planning_error(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            connection.execute("UPDATE projects SET planning_error = 'independent planning error' WHERE repo = ?", (repo,))
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (repo, 'G-001', 'docs/plans/G-001.md', 'one', '[]', '[]', 'normal', 'hash', 'READY', 'base'))
        args = loop_manager.build_parser().parse_args(['recover', repo, '--plan', 'G-001'])
        run = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0))
        self.assertEqual(recover_local_environment(self.effects(run), args, self.state_dir, self.config_path), 0)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertEqual(connection.execute("SELECT planning_error FROM projects WHERE repo = ?", (repo,)).fetchone()[0], 'independent planning error')
            self.assertEqual(connection.execute("SELECT result FROM local_environment_recovery_requests").fetchone()[0], 'resolved')
        self.assertEqual(recover_local_environment(self.effects(run), self.arguments, self.state_dir, self.config_path), 1)

    def test_project_command_checks_second_block_after_first_remains_blocked(self):
        from loop_manager_internal.storage import local_recovery_state
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            for plan in ('G-001', 'G-002'):
                connection.execute(
                    "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha, blocked_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo, plan, f'docs/plans/{plan}.md', plan, '[]', '[]', 'normal', 'hash', 'PLAN_BLOCKED', 'base', 'verify_retry_exhausted'))
        checked = []
        def run(connection, *_args, **kwargs):
            target = kwargs['recovery_plan_id'] or 'G-001'
            checked.append(target)
            with connection:
                self.assertTrue(local_recovery_state.consume_recheck(connection, kwargs['recovery_request_id'], target, 'verify'))
                if target == 'G-002':
                    connection.execute("UPDATE exec_plans SET status = 'READY', blocked_reason = NULL WHERE repo = ? AND plan_id = ?", (repo, target))
            return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1) if target == 'G-001' else ProjectRunResult(ProjectRunKind.WAITING, 0)
        self.assertEqual(recover_local_environment(self.effects(run), self.arguments, self.state_dir, self.config_path), 1)
        self.assertEqual(checked, ['G-001', 'G-002'])
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertEqual([row[0] for row in connection.execute("SELECT status FROM exec_plans ORDER BY plan_id")], ['PLAN_BLOCKED', 'READY'])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM local_environment_recheck_consumption").fetchone()[0], 2)

    def test_project_command_enumerates_metadata_recheck_target(self):
        self.config_path.write_text('[loop]\n', encoding='utf-8')
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha, blocked_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (repo, 'G-001', 'docs/plans/G-001.md', 'one', '[]', '[]', 'normal', 'hash', 'PLAN_BLOCKED', 'base', 'plan_input_invalid'))
        targets = []
        def run(connection, *_args, **kwargs):
            targets.append(kwargs['recovery_plan_id'])
            return ProjectRunResult(ProjectRunKind.WAITING, 0)
        self.assertEqual(recover_local_environment(self.effects(run), self.arguments, self.state_dir, self.config_path), 1)
        self.assertEqual(targets, [None, 'G-001'])

    def test_project_reentry_does_not_dispatch_retained_paused_execution(self):
        from loop_manager_internal.storage import local_recovery_state
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            connection.execute("UPDATE projects SET planning_error = 'saved planning error' WHERE repo = ?", (repo,))
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha, blocked_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (repo, 'G-001', 'docs/plans/G-001.md', 'one', '[]', '[]', 'normal', 'hash', 'PLAN_BLOCKED', 'base', 'verify_retry_exhausted'))
        for kind in (ProjectRunKind.TRANSIENT_PAUSE, ProjectRunKind.USAGE_LIMIT_PAUSE):
            for consumed in (False, True):
                with self.subTest(kind=kind, consumed=consumed):
                    with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
                        connection.execute("UPDATE exec_plans SET status = 'PLAN_BLOCKED' WHERE repo = ?", (repo,))
                        runtime_state.RuntimeStateRepository(connection).clear(repo)
                        connection.execute("UPDATE projects SET planning_error = 'saved planning error' WHERE repo = ?", (repo,))
                    calls = []
                    def run(connection, *_args, **kwargs):
                        calls.append(kwargs['recovery_plan_id'])
                        self.assertEqual(len(calls), 1, 'paused execution dispatched twice')
                        with connection:
                            connection.execute("UPDATE exec_plans SET status = 'READY' WHERE repo = ?", (repo,))
                            runtime_state.RuntimeStateRepository(connection).activate(
                                repo, 'G-001', activation_sha='base', branch='branch', worktree='/worktree')
                            connection.execute("UPDATE projects SET planning_error = 'saved planning error' WHERE repo = ?", (repo,))
                            if consumed:
                                local_recovery_state.consume_recheck(connection, kwargs['recovery_request_id'], 'G-001', 'verify')
                        return ProjectRunResult(kind, 0, 'paused')
                    self.assertEqual(recover_local_environment(self.effects(run), self.arguments, self.state_dir, self.config_path), 1)
                    self.assertEqual(calls, [None])
                    with closing(loop_manager.connect_database(self.state_dir)) as connection:
                        self.assertEqual(runtime_state.RuntimeStateRepository(connection).active_row(repo)['plan_id'], 'G-001')
                        self.assertEqual(connection.execute("SELECT planning_error FROM projects WHERE repo = ?", (repo,)).fetchone()[0], 'saved planning error')
                        self.assertEqual(connection.execute("SELECT result FROM local_environment_recovery_requests ORDER BY rowid DESC LIMIT 1").fetchone()[0], 'unresolved')

    def test_recover_dispatch_preserves_runtime_mutation(self):
        from loop_manager_internal.application.commands import execute_runtime_command
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        mutation = object()
        run = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0))
        self.assertEqual(execute_runtime_command(self.effects(run), self.arguments,
            self.state_dir, self.config_path, runtime_mutation=mutation), 0)
        self.assertIs(run.call_args.kwargs['runtime_mutation'], mutation)

    def test_project_command_rechecks_planning_after_failed_verify(self):
        from loop_manager_internal.storage import local_recovery_state
        from loop_manager_internal.planning.planning_runtime import PlanningOutcome, PlanningOutcomeKind
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            connection.execute("UPDATE projects SET planning_error = 'saved authentication error' WHERE repo = ?", (repo,))
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha, blocked_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (repo, 'G-001', 'docs/plans/G-001.md', 'one', '[]', '[]', 'normal', 'hash', 'PLAN_BLOCKED', 'base', 'verify_retry_exhausted'))
        checked = []
        def run(connection, state_dir, repository, config_path, *_args, **kwargs):
            request_id = kwargs['recovery_request_id']
            if not local_recovery_state.recheck_consumed(connection, request_id, 'G-001', 'verify'):
                with connection:
                    local_recovery_state.consume_recheck(connection, request_id, 'G-001', 'verify')
                checked.append('verify')
                return ProjectRunResult(ProjectRunKind.OPERATIONAL_ERROR, 1)
            effects = SimpleNamespace(_recorded_base_fetch=mock.Mock(), _check_runtime_handoff=mock.Mock(),
                _PlanningLifecyclePorts=mock.Mock(), is_loop_manager_repository=mock.Mock(return_value=False),
                _planning_outcome_step_result=mock.Mock(return_value=None))
            identity = ProjectIdentity(connection=connection, state_dir=state_dir, repo_arg=repository,
                config_path=config_path, requested_base=None, allow_agents_mismatch=False,
                requested_provider='codex', registered_only=True, planner_mutex=None,
                refactoring_review_interval=20, verify_timeout_seconds=600,
                runtime_mutation=kwargs['runtime_mutation'], recovery_request_id=request_id)
            service = ProjectService(effects, identity)
            service.state.repo = repository
            service.state.repo_path = Path(repository)
            service.state.base = 'main'
            service.state.row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
            def planning(context):
                checked.append('planning')
                self.assertEqual(context.recovery_request_id, request_id)
                self.assertTrue(context.local_environment_recheck)
                with connection:
                    connection.execute("UPDATE projects SET planning_error = NULL WHERE repo = ?", (repo,))
                return PlanningOutcome(PlanningOutcomeKind.NO_CHANGE)
            with mock.patch.object(service, '_refresh_catalog', return_value='base'), \
                 mock.patch.object(service, '_dispatch_execution', side_effect=AssertionError('unexpected execution')), \
                 mock.patch('loop_manager_internal.application.project_service.PlanningLifecycleCoordinator') as coordinator:
                coordinator.return_value.run.side_effect = planning
                code = service._run_coordinators()
            return ProjectRunResult(ProjectRunKind.COMPLETED, code)
        self.assertEqual(recover_local_environment(self.effects(run), self.arguments, self.state_dir, self.config_path), 1)
        self.assertEqual(checked, ['verify', 'planning'])
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertIsNone(connection.execute("SELECT planning_error FROM projects WHERE repo = ?", (repo,)).fetchone()[0])
            self.assertEqual(connection.execute("SELECT status FROM exec_plans").fetchone()[0], 'PLAN_BLOCKED')

    def test_manual_resume_runs_in_normal_order_and_is_scoped_out_of_recover(self):
        for request_id in (None, "request"):
            with self.subTest(request_id=request_id):
                service = ProjectService(SimpleNamespace(), SimpleNamespace(
                    repo_arg="repo", recovery_request_id=request_id))
                calls = []
                methods = ("_prepare_repository", "_reconcile_catalog_and_cleanup",
                           "_synchronize_pull_requests", "_resume_manual_work", "_recheck_local_plan_blocks",
                           "_resume_existing_execution", "_run_coordinators")
                from contextlib import ExitStack
                with ExitStack() as stack:
                    for method in methods:
                        def step(name=method):
                            calls.append(name)
                            return 0 if name == "_run_coordinators" else None
                        stack.enter_context(mock.patch.object(service, method, side_effect=step))
                    self.assertEqual(service.run().code, 0)
                expected = [method for method in methods if method != "_recheck_local_plan_blocks"] if request_id is None else [
                    method for method in methods
                    if method not in {"_synchronize_pull_requests", "_resume_manual_work"}]
                self.assertEqual(calls, expected)

    def test_paused_zero_exit_is_not_recorded_as_resolved(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        for kind in (ProjectRunKind.TRANSIENT_PAUSE, ProjectRunKind.USAGE_LIMIT_PAUSE):
            with self.subTest(kind=kind):
                effects = self.effects(mock.Mock(return_value=ProjectRunResult(kind, 0, "paused")))
                self.assertEqual(recover_local_environment(
                    effects, self.arguments, self.state_dir, self.config_path), 1)
                with closing(loop_manager.connect_database(self.state_dir)) as connection:
                    row = connection.execute(
                        "SELECT result FROM local_environment_recovery_requests ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                self.assertEqual(row[0], "unresolved")

    def test_pending_recovery_record_prevents_success_without_a_block(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        from loop_manager_internal.recovery.recovery_runtime import RecoveryReason
        def run(connection, *_args, **_kwargs):
            from loop_manager_internal.storage import recovery_state
            with connection:
                repository = recovery_state.RecoveryRepository(connection)
                before = repository.external_run_snapshot(str(self.repository.resolve()))
                loop_manager._record_project_recovery(
                    connection, str(self.repository.resolve()), RecoveryReason.GIT_LOCK, "Git lock")
                repository.observe_external_run(str(self.repository.resolve()), before)
            return ProjectRunResult(ProjectRunKind.COMPLETED, 0)
        self.assertEqual(recover_local_environment(
            self.effects(run), self.arguments, self.state_dir, self.config_path), 1)

    def test_recovery_run_reaches_timeout_recheck_without_pr_sync(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        identity = ProjectIdentity(
            connection=connection, state_dir=Path("/state"), repo_arg="repo",
            config_path=Path("/config.ini"), requested_base=None,
            allow_agents_mismatch=False, requested_provider="codex",
            registered_only=True, planner_mutex=None, refactoring_review_interval=20,
            verify_timeout_seconds=600, runtime_mutation=None,
            recovery_request_id="request", recovery_plan_id="G-047")
        effects = SimpleNamespace(resume_plan_blocks=mock.Mock())
        service = ProjectService(effects, identity)
        service.state.repo = "repo"
        service.state.repo_path = Path("/repo")
        service.state.base = "main"
        with mock.patch.object(service, "_prepare_repository", return_value=None), \
             mock.patch.object(service, "_reconcile_catalog_and_cleanup", return_value=None), \
             mock.patch.object(service, "_synchronize_pull_requests") as pr_sync, \
             mock.patch.object(service, "_resume_existing_execution", return_value=None), \
             mock.patch.object(service, "_run_coordinators", return_value=0):
            self.assertEqual(service.run().code, 0)
        pr_sync.assert_not_called()
        self.assertEqual(effects.resume_plan_blocks.call_args.kwargs,
                         {"plan_id": "G-047", "force_timeout_recheck": True, "recovery_request_id": "request"})

    def test_recovery_does_not_loop_for_independent_ready_work(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        effects = InternalRunProjectResultEffects(
            _checkpoint_dispatch_ready=mock.Mock(return_value=True),
            _record_project_recovery=mock.Mock(),
            _run_project_step=mock.Mock(side_effect=[0, AssertionError("repeated recovery")]),
            _stable_work_reason=mock.Mock(return_value=None),
            catalog_schedule_decision=mock.Mock(return_value=SimpleNamespace(
                active=None, runnable_ready=object(), project_blocked=False)),
            dispatch_blocked_checkpoints=mock.Mock(),
            is_loop_manager_repository=mock.Mock(return_value=False),
            operation_log=mock.Mock())
        with mock.patch(
            "loop_manager_internal.application.project_runner.blocked_checkpoint_state.BlockedCheckpointRepository"
        ) as checkpoints:
            checkpoints.return_value.pending.return_value = ()
            _run_project_result(effects, connection, Path("/state"), "repo",
                                Path("/config.ini"), None, False,
                                recovery_request_id="request")
        effects._run_project_step.assert_called_once()

    def test_project_recovery_command_rechecks_saved_planning_without_selecting_ready(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            connection.execute("UPDATE projects SET planning_error = 'saved authentication failure' WHERE repo = ?", (repo,))
        from loop_manager_internal.planning.planning_runtime import PlanningOutcome, PlanningOutcomeKind
        def run(connection, state_dir, repository, config_path, *_args, **kwargs):
            effects = SimpleNamespace(
                _recorded_base_fetch=mock.Mock(), _check_runtime_handoff=mock.Mock(),
                _PlanningLifecyclePorts=mock.Mock(),
                is_loop_manager_repository=mock.Mock(return_value=False),
                _planning_outcome_step_result=mock.Mock(return_value=None))
            identity = ProjectIdentity(
                connection=connection, state_dir=state_dir, repo_arg=repository,
                config_path=config_path, requested_base=None, allow_agents_mismatch=False,
                requested_provider="codex", registered_only=True, planner_mutex=None,
                refactoring_review_interval=20, verify_timeout_seconds=600,
                runtime_mutation=None, recovery_request_id=kwargs["recovery_request_id"])
            service = ProjectService(effects, identity)
            service.state.repo = repository
            service.state.repo_path = Path(repository)
            service.state.base = "main"
            service.state.row = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
            def planning(context):
                self.assertTrue(context.local_environment_recheck)
                with connection:
                    connection.execute("UPDATE projects SET planning_error = NULL WHERE repo = ?", (repo,))
                return PlanningOutcome(PlanningOutcomeKind.NO_CHANGE)
            with mock.patch.object(service, "_prepare_repository", return_value=None), \
                 mock.patch.object(service, "_reconcile_catalog_and_cleanup", return_value=None), \
                 mock.patch.object(service, "_recheck_local_plan_blocks", return_value=None), \
                 mock.patch.object(service, "_resume_existing_execution", return_value=None), \
                 mock.patch.object(service, "_refresh_catalog", return_value="base"), \
                 mock.patch.object(service, "_dispatch_execution", side_effect=AssertionError("new work")), \
                 mock.patch("loop_manager_internal.application.project_service.PlanningLifecycleCoordinator") as coordinator:
                coordinator.return_value.run.side_effect = planning
                result = service.run()
                coordinator.return_value.run.assert_called_once()
            return ProjectRunResult(ProjectRunKind.COMPLETED, result.code)
        self.assertEqual(recover_local_environment(self.effects(run), self.arguments,
                                                   self.state_dir, self.config_path), 0)

    def test_historical_recovery_does_not_prevent_successful_recheck(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        from loop_manager_internal.recovery.recovery_runtime import RecoveryReason
        from loop_manager_internal.storage import recovery_state
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            before = recovery_state.RecoveryRepository(connection).external_run_snapshot(repo)
            loop_manager._record_project_recovery(connection, repo, RecoveryReason.GIT_LOCK, "old lock")
            recovery_state.RecoveryRepository(connection).observe_external_run(repo, before)
        from loop_manager_internal.application.project_runner import RunProjectResultEffects, run_project_result
        runner_effects = RunProjectResultEffects(
            _blocked_recovery_subjects=mock.Mock(return_value=frozenset()),
            _run_project_result=mock.Mock(return_value=ProjectRunResult(ProjectRunKind.COMPLETED, 0)))
        def successful_recheck(*args, **kwargs):
            return run_project_result(runner_effects, *args, **kwargs)
        result = recover_local_environment(self.effects(successful_recheck),
                                           self.arguments, self.state_dir, self.config_path)
        self.assertEqual(result, 0)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertEqual(connection.execute("SELECT pending_action FROM recovery_records WHERE repo = ?", (repo,)).fetchone()[0], "retry")

    def test_plan_recovery_finalization_keeps_independent_project_failure(self):
        from loop_manager_internal.storage import recovery_state
        from loop_manager_internal.application.project_runner import RunProjectResultEffects, run_project_result
        from loop_manager_internal.recovery.recovery_runtime import RecoveryReason
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repo = str(Path("repo").resolve())
        connection.execute("UPDATE projects SET repo = ?, planning_error = 'saved planning error'", (repo,))
        snapshot = recovery_state.RecoveryRepository(connection).external_run_snapshot(repo)
        loop_manager._record_project_recovery(connection, repo, RecoveryReason.GIT_LOCK, "planning tool failure")
        recovery_state.RecoveryRepository(connection).observe_external_run(repo, snapshot)
        before = dict(connection.execute("SELECT * FROM persistent_execution_failures WHERE repo = ?", (repo,)).fetchone())
        effects = RunProjectResultEffects(
            _blocked_recovery_subjects=mock.Mock(return_value=frozenset()),
            _run_project_result=mock.Mock(return_value=ProjectRunResult(ProjectRunKind.WAITING, 0)))
        run_project_result(effects, connection, Path("/state"), repo, Path("/config"), None, False,
                           recovery_request_id="request", recovery_plan_id="G-047")
        after = dict(connection.execute("SELECT * FROM persistent_execution_failures WHERE repo = ?", (repo,)).fetchone())
        self.assertEqual(after, before)

    def test_plan_command_preserves_slot_wait_with_independent_block(self):
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            for plan_id in ("G-001", "G-002", "G-003"):
                connection.execute(
                    "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo, plan_id, f"docs/plans/{plan_id}.md", plan_id, '[]', '[]', 'normal', 'hash', 'READY', 'base'))
            runtime_state.RuntimeStateRepository(connection).activate(
                repo, "G-002", activation_sha="base", branch="branch", worktree="/worktree")
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo, "G-003", reason_code="user_decision_required", evidence="independent", release_fingerprint=None, clear_projection=False)
        args = loop_manager.build_parser().parse_args(["recover", repo, "--plan", "G-001"])
        def run(connection, state_dir, repository, config_path, *_args, **kwargs):
            def step(*_args, **_kwargs):
                identity = ProjectIdentity(
                    connection=connection, state_dir=state_dir, repo_arg=repository,
                    config_path=config_path, requested_base=None, allow_agents_mismatch=False,
                    requested_provider="codex", registered_only=True, planner_mutex=None,
                    refactoring_review_interval=20, verify_timeout_seconds=600, runtime_mutation=None,
                    recovery_request_id=kwargs["recovery_request_id"], recovery_plan_id="G-001")
                return ProjectService(SimpleNamespace(_check_runtime_handoff=mock.Mock(), operation_print=mock.Mock()), identity)._prepare_repository()
            effects = InternalRunProjectResultEffects(
                _checkpoint_dispatch_ready=mock.Mock(return_value=False), _record_project_recovery=mock.Mock(),
                _run_project_step=step, _stable_work_reason=mock.Mock(return_value=SimpleNamespace(completed=False, describe=lambda: "waiting")),
                catalog_schedule_decision=mock.Mock(), dispatch_blocked_checkpoints=mock.Mock(),
                is_loop_manager_repository=mock.Mock(return_value=False), operation_log=mock.Mock())
            return _run_project_result(effects, connection, state_dir, repository, config_path,
                                       None, False, recovery_request_id=kwargs["recovery_request_id"], recovery_plan_id="G-001")
        self.assertEqual(recover_local_environment(self.effects(run), args, self.state_dir, self.config_path), 1)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.pending_request(connection, repo)
            self.assertIsNotNone(request)
            self.assertEqual(request.plan_id, "G-001")
            self.assertFalse(local_recovery_state.recheck_consumed(connection, request.request_id, "G-001", "verify"))

    def test_planning_result_and_consumption_survive_database_reopen(self):
        from loop_manager_internal.storage import local_recovery_state, maintenance_state
        from loop_manager_internal.planning.state_ports import _PlanningStatePorts
        repo = str(self.repository.resolve())
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            request = local_recovery_state.begin_request(connection, repo, None)
            state = _PlanningStatePorts(
                SimpleNamespace(update_project=loop_manager.update_project), connection,
                runtime_state.RuntimeStateRepository(connection),
                maintenance_state.MaintenanceRepository(connection), self.state_dir)
            state.update_local_recheck_result(repo, request.request_id,
                last_error="tool unavailable", planning_result_completed=0,
                planning_result_pending_validation=0)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            self.assertTrue(local_recovery_state.recheck_consumed(connection, request.request_id, "planning", "planner"))
            self.assertEqual(connection.execute("SELECT last_error FROM projects WHERE repo = ?", (repo,)).fetchone()[0], "tool unavailable")
            self.assertEqual(local_recovery_state.begin_request(connection, repo, None).request_id, request.request_id)

    def test_parser_accepts_plan_scoped_recovery(self):
        args = loop_manager.build_parser().parse_args([
            "recover", str(self.repository), "--plan", "G-169",
        ])
        self.assertEqual(args.command, "recover")
        self.assertEqual(args.repository, str(self.repository))
        self.assertEqual(args.plan, "G-169")

    def test_missing_config_keeps_one_pending_request_for_same_scope_resume(self):
        run_result = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.COMPLETED, 0))
        effects = self.effects(run_result)
        with self.assertRaisesRegex(
            loop_manager.ManagedConfigError, "같은 `recover` 명령을 다시 실행",
        ):
            recover_local_environment(effects, self.arguments, self.state_dir, self.config_path)

        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            pending = connection.execute(
                "SELECT request_id, status FROM local_environment_recovery_requests "
                "WHERE repo = ?", (str(self.repository.resolve()),),
            ).fetchone()
        self.assertEqual(pending["status"], "pending")
        self.config_path.write_text("[loop]\n", encoding="utf-8")

        self.assertEqual(
            recover_local_environment(effects, self.arguments, self.state_dir, self.config_path),
            0,
        )
        run_result.assert_called_once()
        self.assertEqual(run_result.call_args.kwargs["registered_only"], True)
        self.assertEqual(run_result.call_args.kwargs["recovery_request_id"], pending["request_id"])
        self.assertIsNone(run_result.call_args.kwargs["recovery_plan_id"])
        self.assertEqual(run_result.call_args.kwargs["external_run_id"], pending["request_id"])
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            rows = connection.execute(
                "SELECT request_id, status, result, before_state, after_state "
                "FROM local_environment_recovery_requests WHERE repo = ?",
                (str(self.repository.resolve()),),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], pending["request_id"])
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["result"], "resolved")
        self.assertTrue(rows[0]["before_state"])
        self.assertTrue(rows[0]["after_state"])

    def test_unregistered_repository_and_missing_plan_are_rejected_before_request(self):
        effects = self.effects(mock.Mock())
        missing = self.root / "missing"
        missing.mkdir()
        args = loop_manager.build_parser().parse_args(["recover", str(missing)])
        with self.assertRaisesRegex(ValueError, "등록된 프로젝트"):
            recover_local_environment(effects, args, self.state_dir, self.config_path)
        args = loop_manager.build_parser().parse_args([
            "recover", str(self.repository), "--plan", "G-999",
        ])
        with self.assertRaisesRegex(ValueError, "없는 계획"):
            recover_local_environment(effects, args, self.state_dir, self.config_path)
        effects.run_project_result.assert_not_called()
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='local_environment_recovery_requests'",
            ).fetchone()
        self.assertIsNone(exists)

    def test_missing_state_database_is_not_created(self):
        absent_state = self.root / "absent-state"
        args = loop_manager.build_parser().parse_args(["recover", str(self.repository)])
        with self.assertRaisesRegex(RuntimeError, "상태 DB가 없습니다"):
            recover_local_environment(
                self.effects(mock.Mock()), args, absent_state, self.config_path,
            )
        self.assertFalse((absent_state / "loop-manager.db").exists())

    def test_in_progress_request_keeps_identity_for_a_restart(self):
        with closing(loop_manager.connect_database(self.state_dir)) as connection, connection:
            connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
                "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(self.repository.resolve()), "G-047", "docs/plans/[G-047] Runtime.md",
                 "Runtime", '["docs/PRODUCT.md"]', "[]", "normal", "hash", "READY", "plan-sha"),
            )
            runtime_state.RuntimeStateRepository(connection).activate(
                str(self.repository.resolve()), "G-047", activation_sha="base",
                branch="loop-manager/G-047", worktree=str(self.state_dir / "G-047"),
            )
        self.config_path.write_text("[loop]\n", encoding="utf-8")
        args = loop_manager.build_parser().parse_args([
            "recover", str(self.repository), "--plan", "G-047",
        ])
        run = mock.Mock(return_value=ProjectRunResult(ProjectRunKind.COMPLETED, 0))
        effects = self.effects(run)

        self.assertEqual(
            recover_local_environment(effects, args, self.state_dir, self.config_path), 1,
        )
        first_request = run.call_args.kwargs["recovery_request_id"]
        self.assertEqual(
            recover_local_environment(effects, args, self.state_dir, self.config_path), 1,
        )
        self.assertEqual(run.call_args.kwargs["recovery_request_id"], first_request)
        with closing(loop_manager.connect_database(self.state_dir)) as connection:
            rows = connection.execute(
                "SELECT request_id, status, result FROM local_environment_recovery_requests "
                "WHERE repo = ?", (str(self.repository.resolve()),),
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_id"], first_request)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["result"], "in_progress")

    def test_plan_recovery_dispatches_existing_execution_without_planning(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime_state.RuntimeStateRepository(connection).activate(
            "repo", "G-047", activation_sha="base",
            branch="loop-manager/G-047", worktree="/state/G-047",
        )

        class CatalogRefresh:
            def refresh(self, _revision=None):
                return "base"

        class ExecutionCoordinator:
            def __init__(self, _ports):
                pass

            def prepare(self):
                return ExecutionOutcome(ExecutionOutcomeKind.STABLE_WAIT)

        effects = SimpleNamespace(
            git=mock.Mock(),
            _recorded_base_fetch=mock.Mock(),
            _check_runtime_handoff=mock.Mock(),
            _ExecutionLifecyclePorts=mock.Mock(return_value=object()),
            ExecutionPhaseCoordinator=ExecutionCoordinator,
            _execution_outcome_step_result=mock.Mock(return_value=0),
        )
        identity = ProjectIdentity(
            connection=connection, state_dir=Path("/state"), repo_arg="repo",
            config_path=Path("/config.ini"), requested_base=None,
            allow_agents_mismatch=False, requested_provider="codex",
            registered_only=True, planner_mutex=None, refactoring_review_interval=20,
            verify_timeout_seconds=600, runtime_mutation=None,
            recovery_request_id="request", recovery_plan_id="G-047",
        )
        service = ProjectService(effects, identity)
        service.state.repo = "repo"
        service.state.repo_path = Path("/repo")
        service.state.base = "main"
        service.state.base_sha = "base"
        service.state.history_revision = "base"
        service.state.catalog_refresh = CatalogRefresh()
        service.state.row = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'",
        ).fetchone()

        with mock.patch(
            "loop_manager_internal.application.project_service.PlanningLifecycleCoordinator",
            side_effect=AssertionError("recover must not enter general planning"),
        ):
            self.assertEqual(service._run_coordinators(), 0)
        effects._ExecutionLifecyclePorts.assert_called_once()
        effects._execution_outcome_step_result.assert_called_once()

    def test_project_runner_preserves_recovery_scope_when_invoking_project_service(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE projects SET phase = 'WORKING' WHERE repo = 'repo'",
        )
        step = mock.Mock(return_value=0)
        effects = InternalRunProjectResultEffects(
            _checkpoint_dispatch_ready=lambda *_args: False,
            _record_project_recovery=mock.Mock(),
            _run_project_step=step,
            _stable_work_reason=mock.Mock(return_value=None),
            catalog_schedule_decision=mock.Mock(),
            dispatch_blocked_checkpoints=mock.Mock(),
            is_loop_manager_repository=mock.Mock(return_value=False),
            operation_log=mock.Mock(),
        )
        _run_project_result(
            effects, connection, Path("/state"), "repo", Path("/config.ini"),
            None, False, registered_only=True, recovery_request_id="request",
            recovery_plan_id="G-047",
        )
        self.assertEqual(step.call_args.kwargs["recovery_request_id"], "request")
        self.assertEqual(step.call_args.kwargs["recovery_plan_id"], "G-047")

    def test_plan_recovery_scopes_checkpoint_dispatch_and_respects_other_active_plan(self):
        for active_plan, expected_dispatch in (("G-047", True), ("G-048", False)):
            with self.subTest(active_plan=active_plan):
                connection = runtime_connection()
                self.addCleanup(connection.close)
                repo = str(Path("repo").resolve())
                connection.execute("UPDATE projects SET repo = ? WHERE repo = 'repo'", (repo,))
                connection.execute("UPDATE exec_plans SET repo = ? WHERE repo = 'repo'", (repo,))
                connection.execute(
                    "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
                    "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo, "G-048", "docs/plans/[G-048] Other.md", "Other",
                     '["docs/PRODUCT.md"]', "[]", "normal", "other-hash", "READY", "other-plan-sha"),
                )
                runtime_state.RuntimeStateRepository(connection).activate(
                    repo, active_plan, activation_sha="base",
                    branch=f"loop-manager/{active_plan}", worktree=f"/state/{active_plan}",
                )
                effects = InternalRunProjectResultEffects(
                    _checkpoint_dispatch_ready=mock.Mock(return_value=True),
                    _record_project_recovery=mock.Mock(),
                    _run_project_step=mock.Mock(return_value=0),
                    _stable_work_reason=mock.Mock(return_value=None),
                    catalog_schedule_decision=mock.Mock(
                        return_value=SimpleNamespace(active=None, runnable_ready=None),
                    ),
                    dispatch_blocked_checkpoints=mock.Mock(),
                    is_loop_manager_repository=mock.Mock(return_value=False),
                    operation_log=mock.Mock(),
                )
                request = SimpleNamespace(request_identity="checkpoint-request")
                with mock.patch(
                    "loop_manager_internal.application.project_runner.blocked_checkpoint_state."
                    "BlockedCheckpointRepository",
                ) as repository_type, mock.patch(
                    "loop_manager_internal.application.project_runner.notifications.mark_work_available",
                ):
                    repository_type.return_value.pending.return_value = (request,)
                    _run_project_result(
                        effects, connection, Path("/state"), repo, Path("/config.ini"),
                        None, False, registered_only=True,
                        recovery_request_id="request", recovery_plan_id="G-047",
                    )

                self.assertEqual(effects.dispatch_blocked_checkpoints.called, expected_dispatch)
                if expected_dispatch:
                    self.assertEqual(
                        effects.dispatch_blocked_checkpoints.call_args.kwargs["plan_id"],
                        "G-047",
                    )
                    self.assertEqual(
                        repository_type.return_value.pending.call_args.kwargs["plan_id"],
                        "G-047",
                    )

    def test_pending_checkpoints_can_be_filtered_to_one_plan(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("repo", "G-048", "docs/plans/[G-048] Other.md", "Other",
             '["docs/PRODUCT.md"]', "[]", "normal", "other-hash", "READY", "other-plan-sha"),
        )
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        for plan_id in ("G-047", "G-048"):
            repository.request(
                repo="repo", plan_id=plan_id, reason_code="test", evidence="evidence",
                branch=f"loop-manager/{plan_id}", worktree=f"/state/{plan_id}",
                expected_remote_oid="base", rebase_recovery=False,
            )

        pending = repository.pending("repo", plan_id="G-047")

        self.assertEqual([request.plan_id for request in pending], ["G-047"])

    def test_plan_recovery_does_not_touch_another_active_plan(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "priority, content_hash, status, plan_sha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("repo", "G-048", "docs/plans/[G-048] Other.md", "Other",
             '["docs/PRODUCT.md"]', "[]", "normal", "other-hash", "READY", "other-plan-sha"),
        )
        runtime_state.RuntimeStateRepository(connection).activate(
            "repo", "G-048", activation_sha="base",
            branch="loop-manager/G-048", worktree="/state/G-048",
        )

        class CatalogRefresh:
            def refresh(self, _revision=None):
                return "base"

        effects = SimpleNamespace(
            git=mock.Mock(),
            _recorded_base_fetch=mock.Mock(),
            _check_runtime_handoff=mock.Mock(),
            _ExecutionLifecyclePorts=mock.Mock(),
            ExecutionPhaseCoordinator=mock.Mock(),
            _execution_outcome_step_result=mock.Mock(),
            operation_print=mock.Mock(),
        )
        identity = ProjectIdentity(
            connection=connection, state_dir=Path("/state"), repo_arg="repo",
            config_path=Path("/config.ini"), requested_base=None,
            allow_agents_mismatch=False, requested_provider="codex",
            registered_only=True, planner_mutex=None, refactoring_review_interval=20,
            verify_timeout_seconds=600, runtime_mutation=None,
            recovery_request_id="request", recovery_plan_id="G-047",
        )
        service = ProjectService(effects, identity)
        service.state.repo = "repo"
        service.state.repo_path = Path("/repo")
        service.state.base = "main"
        service.state.base_sha = "base"
        service.state.history_revision = "base"
        service.state.catalog_refresh = CatalogRefresh()
        service.state.row = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'",
        ).fetchone()

        self.assertEqual(service._run_coordinators(), 1)
        effects._ExecutionLifecyclePorts.assert_not_called()
        effects.ExecutionPhaseCoordinator.assert_not_called()


if __name__ == "__main__":
    unittest.main()
