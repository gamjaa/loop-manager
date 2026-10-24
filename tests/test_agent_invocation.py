"""Invocation audit, physical retries and session transition contracts."""

import sqlite3
import stat
import tempfile
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.agents import agent_invocation as invocation
from loop_manager_internal.agents import common_guidance
from loop_manager_internal.storage import agent_invocation_state as state
import loop_manager as manager
from tests.support.fixed_samples import PROJECT_FILES


class RunnerTests(TestCase):
    def test_invalid_targeted_evidence_still_finishes_the_physical_invocation_ledger(self):
        class Provider:
            def run(self, request):
                request.on_launch_prepared(self_outer.targeted_launch())
                (self_outer.launch.attempt / 'targeted-corrupt.jsonl').write_text('{}\n', encoding='utf-8')
                return manager.AgentResult(0, 'done')
        self_outer = self
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
             mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'finished_at': 'proof'}):
            result = manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                'worker', 'prompt', read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext('repo', 'G-1'),
                resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertEqual(result.code, 0)
        self.assertEqual(result.message, 'done')
        self.assertTrue(result.targeted_collection_pending)
        row = self.connection.execute('SELECT result_kind, result_code, collection_status FROM agent_invocations').fetchone()
        self.assertEqual(tuple(row), ('success', 0, 'error'))

    def test_targeted_execution_results_are_persisted_even_when_the_cli_is_interrupted(self):
        from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository, ensure_schema
        from loop_manager_internal.verification.timing import EventWriter
        ensure_schema(self.connection)
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                launch = self.targeted_launch('interrupted' if interrupted else 'completed')
                attempt = launch.attempt
                class Provider:
                    def run(self, request):
                        request.on_launch_prepared(launch)
                        run_id = str(interrupted)
                        writer = EventWriter(attempt / f'targeted-{run_id}.jsonl', identity={'run_id': run_id})
                        writer.emit('test_end', id='suite.test_one', result='failure')
                        writer.close()
                        if interrupted:
                            raise KeyboardInterrupt()
                        return manager.AgentResult(0, 'done')
                with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
                     mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'finished_at': 'proof'}):
                    def invoke():
                        return manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                            'worker', 'prompt', read_only=False, connection=self.connection,
                            invocation=invocation.InvocationContext('repo', str(interrupted)),
                            resolved_profile=invocation.ResolvedProfile('model', 'high'))
                    if interrupted:
                        with self.assertRaises(KeyboardInterrupt):
                            invoke()
                    else:
                        self.assertEqual(invoke().code, 0)
                repository = TargetedTestRepository(self.connection)
                self.assertGreater(repository.sequence('repo', str(interrupted)), 0)

    def targeted_launch(self, name='call-one'):
        runtime = self.state_dir / 'agent-runtime' / 'runtime'
        attempt = runtime / name
        attempt.mkdir(parents=True)
        self.launch = SimpleNamespace(attempt=attempt, runtime=runtime)
        return self.launch

    def test_targeted_records_follow_each_physical_retry_and_session_fallback(self):
        from tests.support.workspace import pin_common_guidance
        from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
        from loop_manager_internal.verification.timing import EventWriter
        calls = []
        class Provider:
            def run(_, request):
                calls.append(request.session_id)
                run = str(len(calls))
                launch = self.targeted_launch('call-' + run)
                request.on_launch_prepared(launch)
                writer = EventWriter(launch.attempt / f'targeted-{run}.jsonl', identity={'run_id': run})
                writer.emit('test_end', id='suite.test_one', result='failure')
                writer.close()
                if len(calls) == 1:
                    return manager.AgentResult(1, 'connection reset', request.session_id)
                if len(calls) == 2:
                    return manager.AgentResult(1, 'session not found', request.session_id)
                return manager.AgentResult(0, 'done', 'new-session')
        pin_common_guidance(self.connection, invocation.InvocationContext('repo', 'G-1'),
                            Path('/worktree'), self.state_dir, sessions=('old-session',))
        attempts = invocation.AttemptSequence('repo', 'G-1')
        call = invocation.TrackedAgentCall(lambda session, fallback, context: manager.run_agent(
            'codex', Path('/worktree'), self.state_dir, Path('/config'), 'worker', 'prompt',
            read_only=False, session_id=session, connection=self.connection, invocation=context,
            resolved_profile=invocation.ResolvedProfile('model', 'high')), attempts)
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
             mock.patch('loop_manager_internal.agents.process_receipt.recover', return_value={'finished_at': 'proof'}):
            self.assertEqual(manager.run_agent_with_retries(call, session_id='old-session').code, 0)
        self.assertEqual(calls, ['old-session', 'old-session', None])
        self.assertEqual(self.connection.execute('SELECT count(DISTINCT invocation_id) FROM targeted_test_run_owners').fetchone()[0], 3)
        self.assertEqual(TargetedTestRepository(self.connection).sequence('repo', 'G-1'), 3)
        self.assertTrue(TargetedTestRepository(self.connection).repeated('repo', 'G-1'))

    def test_collection_pending_preserves_independent_provider_failure(self):
        class Provider:
            def run(_, request):
                launch = self.targeted_launch()
                request.on_targeted_prepared(launch.attempt, launch.runtime / (launch.attempt.name + '.process.json'))
                return manager.AgentResult(1, 'sandbox policy denied', kind=manager.AgentResultKind.PERMANENT_CONFIGURATION)
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                'worker', 'prompt', read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext('repo', 'G-1'),
                resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertEqual(result.message, 'sandbox policy denied')
        self.assertFalse(result.targeted_collection_pending)
        row = self.connection.execute('SELECT finished_at, result_kind, collection_status FROM agent_invocations').fetchone()
        self.assertTrue(row[0])
        self.assertEqual(tuple(row)[1:], ('permanent_configuration', 'error'))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        state.ensure_schema(self.connection)
        runtime_root = mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES)
        runtime_root.start()
        self.addCleanup(runtime_root.stop)

    def test_execution_agent_port_supplies_ledger_connection(self):
        with mock.patch.object(manager, "run_agent", return_value=manager.AgentResult(0, "done")) as run:
            result = manager._run_agent_with_connection(self.connection, "codex", Path("/worktree"))
        self.assertEqual(result.code, 0)
        self.assertIs(run.call_args.kwargs["connection"], self.connection)

    def test_session_db_wait_keeps_completed_result_and_recoverable_binding(self):
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        from tests.support.workspace import pin_common_guidance
        context = invocation.InvocationContext('repo', 'G-1')
        pin_common_guidance(self.connection, context, Path('/worktree'), self.state_dir, role='worker')
        class Provider:
            calls = 0
            def run(self, request):
                self.calls += 1
                request.on_process_started()
                request.on_process_finished()
                return manager.AgentResult(0, 'actual result', 'completed-session',
                                           usage=invocation.Usage(input_tokens=7))
        provider = Provider()
        with mock.patch.object(manager, 'agent_provider', return_value=provider), \
             mock.patch.object(GuidanceRepository, 'bind_session', side_effect=DatabaseWaitTimeout('session wait')):
            result = manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                'worker', 'prompt', read_only=False, connection=self.connection,
                invocation=context, resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertEqual((provider.calls, result.code, result.message, result.session_id),
                         (1, 0, 'actual result', 'completed-session'))
        self.assertEqual(result.collection_status, 'error')
        row = self.connection.execute('SELECT * FROM agent_invocations').fetchone()
        self.assertEqual((row['result_kind'], row['result_code'], row['input_tokens']), ('success', 0, 7))
        repository = GuidanceRepository(self.connection)
        self.assertEqual(repository.session_task('repo', 'codex', 'completed-session'), row['guidance_task_id'])
        with self.assertRaises(RuntimeError):
            repository.bind_session('repo', 'codex', 'completed-session', 'another-task')

    def test_receipt_recovers_observed_result_and_exception_after_all_callback_storage_waits(self):
        from loop_manager_internal.storage.db_access import open_connection, DatabaseWaitTimeout
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        from loop_manager_internal.agents import process_receipt
        for mode in ('success', 'failure', 'exception'):
            with self.subTest(mode=mode):
                path = self.state_dir / (mode + '.db')
                connection = open_connection(path)
                self.addCleanup(connection.close)
                connection.row_factory = sqlite3.Row
                with connection:
                    state.ensure_schema(connection)
                launch = self.targeted_launch(mode)
                original = OSError('original provider exception')
                calls = []
                class Provider:
                    def run(_, request):
                        calls.append(True)
                        request.on_launch_prepared(launch)
                        request.on_process_started()
                        request.on_session_started('observed-session')
                        request.on_process_finished()
                        if mode == 'exception':
                            raise original
                        return manager.AgentResult(0 if mode == 'success' else 1, 'private actual text',
                            'observed-session', usage=invocation.Usage(input_tokens=7))
                callback = mock.Mock(side_effect=DatabaseWaitTimeout('caller DB wait'))
                with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
                     mock.patch.object(state.InvocationRepository, 'mark_execution_started', side_effect=DatabaseWaitTimeout('start wait')), \
                     mock.patch.object(GuidanceRepository, 'bind_session', side_effect=DatabaseWaitTimeout('session wait')):
                    def run():
                        return manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                            'planner', 'private prompt', read_only=True, connection=connection,
                            on_session_started=callback, invocation=invocation.InvocationContext('repo', None),
                            resolved_profile=invocation.ResolvedProfile('model', 'high'))
                    if mode == 'exception':
                        with self.assertRaises(OSError) as raised:
                            run()
                        self.assertIs(raised.exception, original)
                    else:
                        result = run()
                        self.assertEqual((result.message, result.usage.input_tokens), ('private actual text', 7))
                callback.assert_called_once_with('observed-session')
                row = connection.execute('SELECT * FROM agent_invocations').fetchone()
                self.assertIsNone(row['finished_at'])
                observed = process_receipt.observation(str(process_receipt.receipt_path(launch)), self.state_dir, row['invocation_id'])
                self.assertNotIn('private', json.dumps(observed))
                connection.close()
                reopened = open_connection(path)
                self.addCleanup(reopened.close)
                reopened.row_factory = sqlite3.Row
                repository = state.InvocationRepository(reopened)
                self.assertEqual(repository.reconcile_interrupted(state_dir=self.state_dir), 0)
                recovered = dict(reopened.execute('SELECT * FROM agent_invocations').fetchone())
                expected_kind = {'success': 'success', 'failure': 'permanent_configuration', 'exception': 'transient_failure'}[mode]
                self.assertEqual((recovered['result_kind'], recovered['session_id']), (expected_kind, 'observed-session'))
                self.assertEqual((recovered['execution_started_at'], recovered['execution_finished_at']),
                                 (observed['started_at'], observed['finished_at']))
                self.assertEqual(recovered['input_tokens'], None if mode == 'exception' else 7)
                self.assertEqual(GuidanceRepository(reopened).session_task('repo', 'codex', 'observed-session'), recovered['guidance_task_id'])
                repository.reconcile_interrupted(state_dir=self.state_dir)
                self.assertEqual(dict(reopened.execute('SELECT * FROM agent_invocations').fetchone()), recovered)
                self.assertEqual(len(calls), 1)

    def test_storage_pending_restart_consumes_existing_message_or_holds_without_provider(self):
        from loop_manager_internal.storage.db_access import open_connection, DatabaseWaitTimeout
        from loop_manager_internal.storage.task_calls import CallDeferred
        from loop_manager_internal.agents import process_receipt
        for mode in ('matching', 'missing', 'changed_request', 'changed_message', 'changed_ledger', 'changed_session_owner', 'exception'):
            with self.subTest(mode=mode):
                path = self.state_dir / (mode + '.db')
                connection = open_connection(path)
                self.addCleanup(connection.close)
                connection.row_factory = sqlite3.Row
                with connection:
                    state.ensure_schema(connection)
                launch = self.targeted_launch(mode)
                source = launch.attempt / 'last-message.txt'
                source.write_text('original role result\n', encoding='utf-8')
                class Provider:
                    def run(_, request):
                        request.on_launch_prepared(launch)
                        request.on_process_started()
                        request.on_process_finished()
                        if mode == 'exception':
                            raise OSError('original provider exception')
                        return manager.AgentResult(0, 'original role result', 'session', usage=invocation.Usage(input_tokens=7))
                arguments = ('codex', Path('/worktree'), self.state_dir, Path('/config'), 'planner')
                kwargs = dict(read_only=True, connection=connection, invocation=invocation.InvocationContext('repo', None),
                              resolved_profile=invocation.ResolvedProfile('model', 'high'))
                with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
                     mock.patch.object(state.InvocationRepository, 'finish', side_effect=DatabaseWaitTimeout('finish wait')):
                    if mode == 'exception':
                        with self.assertRaises(OSError):
                            manager.run_agent(*arguments, 'prompt', **kwargs)
                    else:
                        self.assertEqual(manager.run_agent(*arguments, 'prompt', **kwargs).message, 'original role result')
                identity = connection.execute('SELECT invocation_id FROM agent_invocations').fetchone()[0]
                connection.close()
                reopened = open_connection(path)
                self.addCleanup(reopened.close)
                reopened.row_factory = sqlite3.Row
                state.InvocationRepository(reopened).reconcile_interrupted(state_dir=self.state_dir)
                kwargs['connection'] = reopened
                if mode == 'missing':
                    source.unlink()
                if mode == 'changed_message':
                    source.write_text('different response', encoding='utf-8')
                if mode == 'changed_ledger':
                    with reopened:
                        reopened.execute("UPDATE agent_invocations SET result_kind='interrupted'")
                if mode == 'changed_session_owner':
                    with reopened:
                        reopened.execute("UPDATE common_guidance_sessions SET task_id='another-task'")
                provider = mock.Mock()
                provider.run.side_effect = AssertionError('provider must not be repeated')
                with mock.patch.object(manager, 'agent_provider', return_value=provider):
                    if mode == 'matching':
                        result = manager.run_agent(*arguments, 'prompt', **kwargs)
                        self.assertEqual((result.code, result.message, result.usage.input_tokens, result.isolation_invocation_id),
                                         (0, 'original role result', 7, identity))
                    else:
                        with self.assertRaises(CallDeferred):
                            manager.run_agent(*arguments, 'different prompt' if mode == 'changed_request' else 'prompt', **kwargs)
                provider.run.assert_not_called()
                self.assertEqual(reopened.execute('SELECT COUNT(*) FROM agent_invocations').fetchone()[0], 1)
                saved = process_receipt.observation(str(process_receipt.receipt_path(launch)), self.state_dir, identity)
                self.assertEqual(saved['database_pending'], mode != 'matching')

    def test_callback_database_requests_retry_without_replaying_the_session_callback(self):
        from loop_manager_internal.storage.db_access import open_connection
        connection = open_connection(self.state_dir / 'callbacks.db')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        with connection:
            state.ensure_schema(connection)
        launch = self.targeted_launch()
        callback = mock.Mock()
        calls = []
        class Provider:
            def run(_, request):
                calls.append(True)
                request.on_launch_prepared(launch)
                request.on_process_started()
                request.on_process_started()
                request.on_session_started('session')
                request.on_process_finished()
                request.on_process_finished()
                return manager.AgentResult(0, 'actual result', 'session')
        failures = set()
        execute = connection.execute
        def busy_once(sql, parameters=()):
            result = execute(sql, parameters)
            for marker in ('execution_started_at = COALESCE', 'execution_finished_at = ?',
                           'INSERT OR IGNORE INTO common_guidance_sessions'):
                if marker in sql and marker not in failures:
                    failures.add(marker)
                    error = sqlite3.OperationalError('database is locked')
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    def locked():
                        raise error
                    connection._call(locked)
            return result
        with mock.patch.object(connection, 'execute', side_effect=busy_once), \
             mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
             mock.patch('loop_manager_internal.agents.invocation_service.events.emit') as emitted:
            result = manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                'planner', 'prompt', read_only=True, connection=connection,
                on_session_started=callback, invocation=invocation.InvocationContext('repo', None),
                resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertEqual((result.code, result.message, len(calls), len(failures)), (0, 'actual result', 1, 3))
        callback.assert_called_once_with('session')
        self.assertEqual(sum(call.args[0] == 'Agent 실제 실행 시작' for call in emitted.call_args_list), 1)
        row = connection.execute('SELECT * FROM agent_invocations').fetchone()
        self.assertEqual((row['session_id'], row['result_kind']), ('session', 'success'))
        self.assertTrue(row['execution_finished_at'])

    def test_guidance_database_wait_does_not_start_or_reclassify_provider(self):
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        with mock.patch.object(GuidanceRepository, 'session_task', side_effect=DatabaseWaitTimeout('guidance wait')), \
             mock.patch.object(manager, 'agent_provider') as provider:
            with self.assertRaises(DatabaseWaitTimeout):
                manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                    'worker', 'prompt', read_only=False, connection=self.connection, session_id='saved-session',
                    invocation=invocation.InvocationContext('repo', 'G-1'),
                    resolved_profile=invocation.ResolvedProfile('model', 'high'))
        provider.assert_not_called()
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM agent_invocations').fetchone()[0], 0)

    def test_invocation_preparation_retries_atomically_or_leaves_provider_unstarted(self):
        import threading
        from loop_manager_internal.storage.db_access import open_connection, DatabaseWaitTimeout
        from loop_manager_internal.runtime.handoff_gate import (
            RuntimeHandoffGate, SelfMergeRuntimeHandoff, _runtime_handoff_signal,
        )
        gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
        token = _runtime_handoff_signal.set(gate)
        self.addCleanup(_runtime_handoff_signal.reset, token)
        start = state.InvocationRepository.start
        def start_without_launch_lock(repository, *args, **kwargs):
            self.assertFalse(gate.launch_lock.locked())
            self.assertTrue(repository.connection._request_running)
            return start(repository, *args, **kwargs)

        for stop in (None, DatabaseWaitTimeout('budget exhausted'), SelfMergeRuntimeHandoff()):
            with self.subTest(stop=type(stop).__name__):
                connection = open_connection(self.state_dir / (type(stop).__name__ + '.db'))
                self.addCleanup(connection.close)
                connection.row_factory = sqlite3.Row
                with connection:
                    state.ensure_schema(connection)
                provider = mock.Mock()
                provider.run.return_value = manager.AgentResult(0, 'actual result')
                bind = state.InvocationRepository.bind_guidance
                attempts = []

                def busy_after_binding(repository, *args):
                    bind(repository, *args)
                    attempts.append(args[0])
                    if len(attempts) == 1:
                        error = sqlite3.OperationalError('database is locked')
                        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                        def locked():
                            raise error
                        connection._call(locked)

                with mock.patch.object(state.InvocationRepository, 'bind_guidance', busy_after_binding), \
                     mock.patch.object(state.InvocationRepository, 'start', start_without_launch_lock), \
                     mock.patch.object(state.InvocationRepository, 'invocation_scope_id', return_value=None) as scope, \
                     mock.patch.object(manager, 'agent_provider', return_value=provider), \
                     mock.patch('loop_manager_internal.storage.db_access._pause', side_effect=stop):
                    def run():
                        return manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                            'planner', 'prompt', read_only=True, connection=connection,
                            invocation=invocation.InvocationContext('repo', None),
                            resolved_profile=invocation.ResolvedProfile('model', 'high'))
                    if stop is None:
                        self.assertEqual(run().message, 'actual result')
                        self.assertEqual((len(attempts), scope.call_count, provider.run.call_count), (2, 2, 1))
                        rows = connection.execute('SELECT invocation_id, guidance_task_id FROM agent_invocations').fetchall()
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0][0], attempts[-1])
                        self.assertTrue(rows[0][1])
                    else:
                        with self.assertRaises(type(stop)):
                            run()
                        provider.run.assert_not_called()
                        self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_invocations').fetchone()[0], 0)
                self.assertFalse(connection.in_transaction)
                self.assertFalse(connection._held)

    def test_prelaunch_evidence_db_retry_does_not_replay_provider(self):
        from loop_manager_internal.storage.db_access import open_connection
        from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence
        connection = open_connection(self.state_dir / 'evidence.db')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        with connection:
            state.ensure_schema(connection)
        evidence = IsolationEvidence('codex', 'version', 'environment', 'planner', '/worktree',
                                     'repo', 'a' * 64, 'verified', snapshot_digest='b' * 64,
                                     configuration_digest='c' * 64)
        value = dict(provider='codex', model='model', effort='high', instruction='a' * 64,
                     environment='b' * 64, policy='c' * 64)
        provider_calls = []
        class Provider:
            def run(_, request):
                provider_calls.append(request)
                request.on_isolation_checked(evidence)
                request.on_execution_identity(value)
                request.on_process_started()
                request.on_process_finished()
                return manager.AgentResult(0, 'done')

        execute = connection.execute
        failures = []
        def busy_once(sql, parameters=()):
            result = execute(sql, parameters)
            for marker in ('INSERT INTO agent_isolation_evidence', 'SET execution_identity=?'):
                if marker in sql and marker not in failures:
                    failures.append(marker)
                    error = sqlite3.OperationalError('database is locked')
                    error.sqlite_errorcode = sqlite3.SQLITE_LOCKED
                    def locked():
                        raise error
                    connection._call(locked)
            return result
        with mock.patch.object(connection, 'execute', side_effect=busy_once), \
             mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                'planner', 'prompt', read_only=True, connection=connection,
                invocation=invocation.InvocationContext('repo', None),
                resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertEqual((result.code, len(provider_calls), len(failures)), (0, 1, 2))
        row = connection.execute('SELECT execution_identity, execution_status FROM agent_invocations').fetchone()
        self.assertEqual((json.loads(row[0]), row[1]), (value, 'executed'))
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM agent_isolation_evidence').fetchone()[0], 1)

    def test_launch_binding_wait_retries_db_only_and_preserves_proven_non_execution(self):
        from loop_manager_internal.storage.db_access import open_connection, DatabaseWaitTimeout
        from loop_manager_internal.storage import task_calls
        from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
        from loop_manager_internal.agents import process_receipt
        markers = ('SET receipt_path=?', 'SET process_receipt_path=?', 'INSERT INTO targeted_test_collections')
        for index, marker in enumerate(markers):
            for stop in (None, DatabaseWaitTimeout('launch wait'), SelfMergeRuntimeHandoff()):
                with self.subTest(marker=marker, stop=type(stop).__name__):
                    name = f'launch-{index}-{type(stop).__name__}'
                    connection = open_connection(self.state_dir / (name + '.db'))
                    self.addCleanup(connection.close)
                    connection.row_factory = sqlite3.Row
                    with connection:
                        state.ensure_schema(connection)
                        task_calls.ensure_schema(connection)
                    owner = task_calls.reserve(connection, self.state_dir, 'repo', 'G-1', 'agent', 'run')
                    token = task_calls.current_call.set(owner)
                    launch = self.targeted_launch(name)
                    physical_starts = []
                    class Provider:
                        def run(_, request):
                            request.on_launch_prepared(launch)
                            physical_starts.append(True)
                            request.on_process_started()
                            request.on_process_finished()
                            return manager.AgentResult(0, 'done')
                    execute = connection.execute
                    attempts = []
                    def busy_once(sql, parameters=()):
                        result = execute(sql, parameters)
                        if marker in sql:
                            attempts.append(True)
                            if len(attempts) == 1:
                                error = sqlite3.OperationalError('database is locked')
                                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                                def locked():
                                    raise error
                                connection._call(locked)
                        return result
                    try:
                        with mock.patch.object(connection, 'execute', side_effect=busy_once), \
                             mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
                             mock.patch('loop_manager_internal.storage.db_access._pause', side_effect=stop):
                            def run():
                                return manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                                    'worker', 'prompt', read_only=False, connection=connection,
                                    invocation=invocation.InvocationContext('repo', 'G-1'),
                                    resolved_profile=invocation.ResolvedProfile('model', 'high'))
                            if stop is None:
                                self.assertEqual(run().message, 'done')
                                self.assertEqual(len(physical_starts), 1)
                                self.assertGreaterEqual(len(attempts), 2)
                                self.assertEqual(connection.execute('SELECT COUNT(*) FROM targeted_test_collections').fetchone()[0], 1)
                                row = connection.execute('SELECT * FROM task_call_reservations').fetchone()
                                self.assertEqual(row['receipt_path'], str(process_receipt.receipt_path(launch)))
                                self.assertIsNotNone(row['invocation_id'])
                            else:
                                with self.assertRaises(type(stop)):
                                    run()
                                self.assertEqual(physical_starts, [])
                                proof = process_receipt.recover(str(process_receipt.receipt_path(launch)), self.state_dir)
                                self.assertEqual(proof['evidence'], 'not_started')
                                self.assertEqual(connection.execute('SELECT execution_status FROM agent_invocations').fetchone()[0], 'not_executed')
                                if isinstance(stop, DatabaseWaitTimeout):
                                    self.assertEqual(connection.execute('SELECT result_kind FROM agent_invocations').fetchone()[0], 'database_wait')
                            self.assertTrue(task_calls.confirmed(owner))
                            self.assertEqual(task_calls.recover(connection, self.state_dir), ())
                    finally:
                        task_calls.current_call.reset(token)

    def test_direct_targeted_preparation_binds_the_reservation_and_preserves_unknown_exit(self):
        from loop_manager_internal.storage import task_calls
        from loop_manager_internal.agents import process_receipt
        task_calls.ensure_schema(self.connection)
        owner = task_calls.reserve(self.connection, self.state_dir, 'repo', 'G-1', 'agent', 'run')
        token = task_calls.current_call.set(owner)
        launch = self.targeted_launch()
        receipt = process_receipt.receipt_path(launch)
        class Provider:
            def run(_, request):
                request.on_targeted_prepared(launch.attempt, receipt)
                request.on_process_started()
                raise OSError('exit is not confirmed')
        try:
            with mock.patch.object(manager, 'agent_provider', return_value=Provider()):
                with self.assertRaises(OSError):
                    manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                        'worker', 'prompt', read_only=False, connection=self.connection,
                        invocation=invocation.InvocationContext('repo', 'G-1'),
                        resolved_profile=invocation.ResolvedProfile('model', 'high'))
            row = self.connection.execute('SELECT * FROM task_call_reservations').fetchone()
            self.assertEqual(row['receipt_path'], str(receipt))
            self.assertIsNotNone(row['invocation_id'])
            self.assertFalse(task_calls.confirmed(owner))
            self.assertEqual(len(task_calls.recover(self.connection, self.state_dir)), 1)
            self.assertIsNone(self.connection.execute('SELECT execution_finished_at FROM agent_invocations').fetchone()[0])
        finally:
            task_calls.current_call.reset(token)

    def test_prelaunch_receipt_write_failure_keeps_original_database_wait(self):
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from loop_manager_internal.runtime import operation_events as events
        launch = self.targeted_launch()
        original = DatabaseWaitTimeout('original launch wait')
        class Provider:
            def run(_, request):
                request.on_launch_prepared(launch)
                self.fail('process creation must not be reached')
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
             mock.patch.object(state.InvocationRepository, 'bind_process_receipt', side_effect=original), \
             mock.patch('loop_manager_internal.agents.process_receipt.not_started', side_effect=OSError('disk full')), \
             mock.patch.object(events, 'emit') as emit:
            with self.assertRaises(DatabaseWaitTimeout) as raised:
                manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                    'worker', 'prompt', read_only=False, connection=self.connection,
                    invocation=invocation.InvocationContext('repo', 'G-1'),
                    resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertIs(raised.exception, original)
        self.assertTrue(any(call.args[0] == 'Agent 미시작 근거 보존 실패'
                            and call.kwargs.get('error_type') == 'OSError' for call in emit.call_args_list))

    def test_storage_handoff_does_not_replace_the_original_provider_exception(self):
        from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
        original = OSError('original provider exception')
        provider = mock.Mock()
        provider.run.side_effect = original
        with mock.patch.object(manager, 'agent_provider', return_value=provider), \
             mock.patch.object(state.InvocationRepository, 'apply_observation', side_effect=SelfMergeRuntimeHandoff()):
            with self.assertRaises(OSError) as raised:
                manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                    'planner', 'prompt', read_only=True, connection=self.connection,
                    invocation=invocation.InvocationContext('repo', None),
                    resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertIs(raised.exception, original)

    def test_log_start_follows_process_callback_and_preserves_each_attempt(self):
        from loop_manager_internal.runtime import operation_events as events
        from loop_manager_internal.agents.provider_adapters import _provider_log_path
        recorded = []
        outer = self
        class Provider:
            def run(self, request):
                outer.assertFalse(any(message == "Agent 실제 실행 시작" for message, _ in recorded[-1:]))
                request.on_process_started()
                _provider_log_path(request.worktree, request.state_dir, 'codex', 'worker').write_text(
                    '이번 호출 진단', encoding='utf-8')
                request.on_process_finished()
                return manager.AgentResult(0, '비공개 결과', usage=invocation.Usage(input_tokens=7, output_tokens=2))
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
             mock.patch.object(events, 'emit', side_effect=lambda message, **fields: recorded.append((message, fields))):
            for _ in range(2):
                result = manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                    'worker', '비공개 prompt', read_only=False, level=1, connection=self.connection,
                    invocation=invocation.InvocationContext('repo', 'G-log'),
                    resolved_profile=invocation.ResolvedProfile('model', 'low'))
                self.assertEqual(result.code, 0)
        paths = [Path(fields['log_path']) for message, fields in recorded if message == 'Agent 호출별 상세 기록 보존']
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(path.read_text(encoding='utf-8') == '이번 호출 진단' for path in paths))
        results = [fields for message, fields in recorded if message == 'Agent 호출 결과 관측']
        self.assertTrue(all(fields['execution_status'] == 'executed' and fields['input_tokens'] == 7 for fields in results))
        self.assertNotIn('비공개', str(recorded))

    def test_rejected_preparation_does_not_log_process_start_or_copy_old_log(self):
        from loop_manager_internal.runtime import operation_events as events
        from loop_manager_internal.agents.provider_adapters import _provider_log_path
        previous = _provider_log_path(Path('/worktree'), self.state_dir, 'codex', 'worker')
        previous.write_text('이전 호출 진단', encoding='utf-8')
        provider = mock.Mock()
        provider.run.return_value = manager.AgentResult(1, '실행 전 거부')
        with mock.patch.object(manager, 'agent_provider', return_value=provider), mock.patch.object(events, 'emit') as output:
            manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                'worker', 'prompt', read_only=False, level=1, connection=self.connection,
                invocation=invocation.InvocationContext('repo', 'G-log'),
                resolved_profile=invocation.ResolvedProfile('model', 'low'))
        messages = [call.args[0] for call in output.call_args_list]
        self.assertNotIn('Agent 실제 실행 시작', messages)
        self.assertNotIn('Agent 호출별 상세 기록 보존', messages)
        self.assertEqual(previous.read_text(encoding='utf-8'), '이전 호출 진단')

    def test_pre_resolved_worker_profile_is_shared_by_cli_and_ledger(self):
        selected = invocation.ResolvedProfile('actual-model', 'high')
        class Provider:
            def run(self, request):
                self_outer.assertEqual(request.profile, selected)
                return manager.AgentResult(0, 'done', 'worker-session')
        self_outer = self
        with mock.patch.object(manager, '_agent_profile') as resolve, \
             mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), self.state_dir, Path('/config'),
                'worker', 'prompt', read_only=False, level=5,
                connection=self.connection,
                invocation=invocation.InvocationContext('repo', 'G-041'),
                resolved_profile=selected,
            )
        self.assertEqual(result.code, 0)
        resolve.assert_not_called()
        row = self.connection.execute('SELECT model, effort FROM agent_invocations').fetchone()
        self.assertEqual(tuple(row), ('actual-model', 'high'))

    def test_preflight_accepts_proven_external_base_fetch_for_selected_plan(self):
        self.connection.execute(manager.SCHEMA)
        manager._ensure_execplans_schema(self.connection)
        self.connection.execute("INSERT INTO projects (repo, base_branch, origin) "
                                "VALUES ('repo', 'main', 'owner/repo')")
        self.connection.execute("""INSERT INTO exec_plans
            (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, branch)
            VALUES ('repo', 'G-041', 'docs/plans/[G-041] Plan.md', 'Plan', '[]', '[]',
                    'normal', 'hash', 'IMPLEMENTING', 'loop-manager/G-041')""")
        worktree = str(Path('/worktree').resolve())
        source = str(Path('repo').resolve())
        base_ref, head_ref = 'refs/remotes/origin/main', 'refs/remotes/origin/HEAD'
        branch_ref = 'refs/heads/loop-manager/G-041'
        before = manager.GitSnapshot(
            'candidate', 'loop-manager/G-041', {}, {},
            {base_ref: ('a', ''), head_ref: ('a', base_ref), branch_ref: ('candidate', '')},
            {worktree: ('HEAD=candidate', f'branch={branch_ref}'),
             source: ('HEAD=source-old', 'state=clean'), '/unrelated': ('HEAD=unrelated-old',)},
            {}, {},
        )
        current = replace(
            before,
            refs={**before.refs, base_ref: ('b', ''), head_ref: ('b', base_ref)},
            worktrees={**before.worktrees,
                       source: ('HEAD=source-new', 'branch=refs/heads/user-local', 'state=dirty'),
                       '/unrelated': ('HEAD=unrelated-new',)},
        )
        digests = []

        class Provider:
            def run(self, request):
                digests.extend((request.isolation_snapshot_digest(),
                                request.isolation_snapshot_digest()))
                return manager.AgentResult(0, 'done')

        def git(_repo, *args, **_kwargs):
            if args[0] == 'remote':
                return SimpleNamespace(returncode=0, stdout='https://github.com/owner/repo.git\n')
            if args[0] == 'ls-remote':
                return SimpleNamespace(returncode=0, stdout='b\trefs/heads/main\n')
            if args[0] == 'merge-base':
                return SimpleNamespace(returncode=0, stdout='')
            if args[0] == 'rev-parse':
                return SimpleNamespace(returncode=0, stdout='b\n')
            raise AssertionError(args)

        with mock.patch.object(manager, 'capture_git_snapshot', side_effect=(before, current)), \
             mock.patch.object(manager, 'git', side_effect=git), \
             mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), self.state_dir, Path('/config'),
                'worker', 'prompt', read_only=False, level=5,
                connection=self.connection,
                invocation=invocation.InvocationContext('repo', 'G-041'),
                resolved_profile=invocation.ResolvedProfile('model', 'high'),
            )
        self.assertEqual(result.code, 0)
        self.assertEqual(len(digests), 2)
        self.assertEqual(digests[0], digests[1])

    def test_preflight_uses_full_snapshot_when_project_scope_tables_are_missing(self):
        snapshot = manager.GitSnapshot('a' * 40, None, {}, {}, {}, {}, {}, {})
        observed = []

        class Provider:
            def run(self, request):
                observed.append(request.isolation_snapshot_digest())
                return manager.AgentResult(0, 'done')

        with mock.patch.object(manager, 'capture_git_snapshot', return_value=snapshot), \
             mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), self.state_dir, Path('/config'),
                'planner', 'prompt', read_only=True,
                connection=self.connection,
                invocation=invocation.InvocationContext('repo', None),
                resolved_profile=invocation.ResolvedProfile('model', 'high'),
            )
        self.assertEqual(result.code, 0)
        self.assertEqual(observed, [hashlib.sha256(('planner\0' + str(Path('/worktree').resolve()) + '\0repo\0').encode()).hexdigest()])

    def test_each_fallback_process_has_own_resolved_profile_and_session(self):
        seen = []

        class Provider:
            def run(self, request):
                seen.append(request.profile)
                if request.session_id:
                    return manager.AgentResult(1, "session not found", request.session_id)
                request.on_session_started("new-session")
                return manager.AgentResult(0, "done", "new-session",
                                           usage=invocation.Usage(20, 10, None, 2))

        from tests.support.workspace import pin_common_guidance
        pin_common_guidance(self.connection, invocation.InvocationContext("repo", "G-039"),
                            Path("/worktree"), self.state_dir, sessions=("old-session",))
        attempts = invocation.AttemptSequence("repo", "G-039", "revision")
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "worker", "secret prompt", read_only=False, level=3,
                persist_session=True, session_id=session, connection=self.connection,
                invocation=context), attempts)
        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("real-model", "high")) as resolve_profile, \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent_with_retries(call, session_id="old-session")
        self.assertEqual(result.code, 0)
        self.assertEqual(resolve_profile.call_count, 2)
        self.assertEqual(seen, [invocation.ResolvedProfile("real-model", "high")] * 2)
        rows = self.connection.execute("SELECT * FROM agent_invocations ORDER BY attempt").fetchall()
        self.assertEqual([(row["attempt"], row["transition"], row["transition_cause"],
                           row["session_id"], row["result_kind"])
                          for row in rows],
                         [(1, "resume", None, "old-session", "permanent_configuration"),
                          (2, "fallback", "session_unavailable", "new-session", "success")])
        self.assertEqual(rows[1]["model"], "real-model")
        self.assertEqual(rows[1]["cached_input_tokens"], 10)
        self.assertNotIn("secret prompt", repr(dict(rows[1])))

    def test_planner_worker_reviewer_rows_keep_role_and_plan_identity(self):
        class Provider:
            def run(self, request):
                request.on_session_started(f"{request.role_name}-session")
                return manager.AgentResult(0, "done", f"{request.role_name}-session")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "medium")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            for role, plan in (("planner", None), ("worker", "G-039"),
                               ("reviewer", "G-039")):
                result = manager.run_agent(
                    "codex", Path("/worktree"), self.state_dir, Path("/config"),
                    role, "secret prompt", read_only=role == "reviewer",
                    level=3 if role == "worker" else None,
                    connection=self.connection,
                    invocation=invocation.InvocationContext("repo", plan,
                                                             "revision" if plan else None))
                self.assertEqual(result.code, 0)
        rows = self.connection.execute(
            "SELECT role, plan_id, definition_revision, session_id FROM agent_invocations ORDER BY rowid"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("planner", None, None, "planner-session"),
            ("worker", "G-039", "revision", "worker-session"),
            ("reviewer", "G-039", "revision", "reviewer-session"),
        ])

    def test_planner_check_ledger_keeps_role_profile_session_and_usage(self):
        class Provider:
            def run(self, request):
                self_outer.assertEqual(request.role_name, "planner_check")
                self_outer.assertTrue(request.read_only)
                self_outer.assertEqual((request.profile.model, request.profile.effort), ("check-model", "low"))
                request.on_session_started("check-session")
                return manager.AgentResult(0, "checked", "check-session",
                                           usage=invocation.Usage(input_tokens=12, output_tokens=3))

        self_outer = self
        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("check-model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "planner_check", "secret prompt", read_only=True,
                connection=self.connection,
                invocation=invocation.InvocationContext("repo", None),
            )
        self.assertEqual(result.code, 0)
        row = self.connection.execute(
            "SELECT role, model, effort, session_id, input_tokens, output_tokens FROM agent_invocations"
        ).fetchone()
        self.assertEqual(tuple(row), ("planner_check", "check-model", "low", "check-session", 12, 3))

    def test_ledger_failure_does_not_change_agent_success(self):
        self.connection.execute("DROP TABLE agent_invocations")

        class Provider:
            def run(self, request):
                return manager.AgentResult(0, "done", "session")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual(result.code, 0)
        self.assertEqual(result.collection_status, "error")

    def test_auxiliary_collection_error_keeps_success_and_usage_in_ledger(self):
        class Provider:
            def run(self, request):
                return manager.AgentResult(
                    0, "done", "session", usage=invocation.Usage(10, 4, None, 2),
                    collection_status="error",
                )

        with mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None),
                resolved_profile=manager.AgentProfile("model", "low"),
            )
        row = self.connection.execute("""SELECT result_kind, result_code, session_id,
            collection_status, input_tokens, cached_input_tokens, output_tokens
            FROM agent_invocations""").fetchone()
        self.assertEqual(result.code, 0)
        self.assertEqual(tuple(row), ("success", 0, "session", "error", 10, 4, 2))

    def test_process_callbacks_store_explicit_purpose_target_and_execution_time(self):
        class Provider:
            def run(self, request):
                request.on_process_started()
                request.on_process_finished()
                return manager.AgentResult(0, "done", "session",
                                           usage=invocation.Usage(8, 3, None, 2))

        with mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "worker", "secret prompt", read_only=False,
                connection=self.connection,
                invocation=invocation.InvocationContext(
                    "repo", "G-184", task_id="task-184",
                    purpose="worker_implementation", target="G-184",
                ),
                resolved_profile=invocation.ResolvedProfile("model", "high"),
            )

        row = self.connection.execute("""SELECT task_id, purpose, target, execution_status,
            execution_started_at, execution_finished_at, execution_duration_ms,
            input_tokens, output_tokens FROM agent_invocations""").fetchone()
        self.assertEqual(result.code, 0)
        self.assertEqual(tuple(row[:4]),
                         ("task-184", "worker_implementation", "G-184", "executed"))
        self.assertTrue(row["execution_started_at"])
        self.assertTrue(row["execution_finished_at"])
        self.assertIsNotNone(row["execution_duration_ms"])
        self.assertEqual((row["input_tokens"], row["output_tokens"]), (8, 2))

    def test_process_wait_failure_leaves_execution_finish_unconfirmed(self):
        class Provider:
            def run(self, request):
                request.on_process_started()
                raise OSError("wait interrupted")

        with mock.patch.object(manager, "agent_provider", return_value=Provider()):
            with self.assertRaisesRegex(OSError, "wait interrupted"):
                manager.run_agent(
                    "codex", Path("/worktree"), self.state_dir, Path("/config"),
                    "worker", "secret prompt", read_only=False,
                    connection=self.connection,
                    invocation=invocation.InvocationContext("repo", "G-184"),
                    resolved_profile=invocation.ResolvedProfile("model", "high"),
                )

        row = self.connection.execute("""SELECT execution_status, execution_started_at,
            execution_finished_at, execution_duration_ms FROM agent_invocations""").fetchone()
        self.assertEqual(row["execution_status"], "executed")
        self.assertTrue(row["execution_started_at"])
        self.assertIsNone(row["execution_finished_at"])
        self.assertIsNone(row["execution_duration_ms"])

    def test_process_observation_storage_error_keeps_agent_result(self):
        class Provider:
            def run(self, request):
                request.on_process_started()
                request.on_process_finished()
                return manager.AgentResult(0, "done")

        with mock.patch.object(state.InvocationRepository, "mark_execution_started",
                               side_effect=sqlite3.OperationalError("unavailable")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "planner", "prompt", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None),
                resolved_profile=manager.AgentProfile("model", "low"),
            )

        row = self.connection.execute(
            "SELECT execution_status, collection_status FROM agent_invocations"
        ).fetchone()
        self.assertEqual((result.code, result.collection_status), (0, "error"))
        self.assertEqual(tuple(row), ("executed", "error"))

    def test_profile_observation_failure_does_not_replace_provider_result(self):
        class Provider:
            def run(self, request):
                if request.profile is not None:
                    raise AssertionError("unresolved profile must stay absent")
                return manager.AgentResult(0, "done", "session")

        with mock.patch.object(manager, "_agent_profile", side_effect=RuntimeError("missing config")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual((result.code, result.collection_status), (0, "error"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM agent_invocations").fetchone()[0], 0)

    def test_transient_retry_and_timeout_have_distinct_rows(self):
        count = 0

        class Provider:
            def run(self, request):
                nonlocal count
                count += 1
                if count == 1:
                    raise manager.subprocess.TimeoutExpired("agent", 1)
                return manager.AgentResult(0, "done", "session")

        attempts = invocation.AttemptSequence("repo", None)
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "planner", "secret", read_only=False, session_id=session,
                connection=self.connection, invocation=context), attempts)
        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            result = manager.run_agent_with_retries(call, session_id=None)
        self.assertEqual(result.code, 0)
        rows = self.connection.execute("SELECT result_kind, transition_cause FROM agent_invocations ORDER BY attempt").fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("timeout", None), ("success", "timeout")])

    def test_timeout_spellings_share_timeout_ledger_kind(self):
        class Provider:
            def run(self, request):
                return manager.AgentResult(1, self.message)

        for message in ("Agent CLI timeout", "Agent CLI timed out"):
            with self.subTest(message=message), \
                 mock.patch.object(manager, "_agent_profile",
                                   return_value=manager.AgentProfile("model", "low")), \
                 mock.patch.object(manager, "agent_provider", return_value=Provider()):
                Provider.message = message
                result = manager.run_agent(
                    "codex", Path("/worktree"), self.state_dir, Path("/config"),
                    "planner", "secret", read_only=False, connection=self.connection,
                    invocation=invocation.InvocationContext("repo", None),
                    resolved_profile=manager.AgentProfile("model", "low"),
                )
            self.assertEqual(result.ledger_kind, "timeout")
        rows = self.connection.execute(
            "SELECT result_kind FROM agent_invocations ORDER BY rowid"
        ).fetchall()
        self.assertEqual([row["result_kind"] for row in rows], ["timeout", "timeout"])

    def test_latest_attempt_without_final_session_clears_saved_session(self):
        from tests.support.workspace import pin_common_guidance
        pin_common_guidance(self.connection, invocation.InvocationContext('repo', None),
                            Path('/worktree'), self.state_dir, role='planner',
                            sessions=('saved-session',))
        class Provider:
            def run(self, request):
                self_outer.assertEqual(request.session_id, "saved-session")
                return manager.AgentResult(1, "configuration error")

        self_outer = self
        with mock.patch.object(manager, "agent_provider", return_value=Provider()):
            manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "planner", "secret", read_only=False, session_id="saved-session",
                connection=self.connection,
                invocation=invocation.InvocationContext("repo", None),
                resolved_profile=manager.AgentProfile("model", "low"),
            )
        row = self.connection.execute(
            "SELECT session_id, result_kind FROM agent_invocations"
        ).fetchone()
        self.assertEqual(tuple(row), (None, "permanent_configuration"))

    def test_replacement_context_marks_first_process_and_retry_cause(self):
        attempts = invocation.AttemptSequence(
            "repo", "G-039", "revision",
            initial_transition="replace", initial_cause="profile_changed")
        first = attempts.next(None, False)
        attempts.previous_failure = "usage_limit"
        retry = attempts.next(None, False)
        self.assertEqual(
            [(item.transition, item.transition_cause, item.attempt)
             for item in (first, retry)],
            [("replace", "profile_changed", 1), ("start", "usage_limit", 2)],
        )

    def test_retry_after_session_fallback_records_actual_transition(self):
        attempts = invocation.AttemptSequence("repo", "G-039")
        first = attempts.next("old-session", False)
        attempts.previous_failure = "permanent_configuration"
        fallback = attempts.next(None, True)
        attempts.previous_failure = "transient_failure"
        retry = attempts.next("new-session", True)
        self.assertEqual(
            [(item.transition, item.transition_cause) for item in (first, fallback, retry)],
            [("resume", None), ("fallback", "session_unavailable"),
             ("resume", "transient_failure")],
        )

    def test_session_observation_storage_error_keeps_agent_result(self):
        class Provider:
            def run(self, request):
                request.on_session_started("observed")
                return manager.AgentResult(0, "done", "observed")

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()), \
             mock.patch.object(state.InvocationRepository, "observe_session",
                               side_effect=sqlite3.OperationalError("unavailable")):
            result = manager.run_agent(
                "codex", Path("/worktree"), self.state_dir, Path("/config"),
                "planner", "secret", read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext("repo", None))
        self.assertEqual((result.code, result.collection_status), (0, "error"))
        row = self.connection.execute("SELECT session_id, collection_status FROM agent_invocations").fetchone()
        self.assertEqual(tuple(row), ("observed", "error"))

    def test_interrupted_process_preserves_observed_session(self):
        class Provider:
            def run(self, request):
                request.on_session_started("observed")
                raise KeyboardInterrupt()

        with mock.patch.object(manager, "_agent_profile", return_value=manager.AgentProfile("model", "low")), \
             mock.patch.object(manager, "agent_provider", return_value=Provider()):
            with self.assertRaises(KeyboardInterrupt):
                manager.run_agent(
                    "codex", Path("/worktree"), self.state_dir, Path("/config"),
                    "worker", "secret", read_only=False, level=1,
                    connection=self.connection,
                    invocation=invocation.InvocationContext("repo", "G-039"))
        row = self.connection.execute(
            "SELECT session_id, result_kind, finished_at FROM agent_invocations"
        ).fetchone()
        self.assertEqual((row["session_id"], row["result_kind"]),
                         ("observed", "interrupted"))
        self.assertIsNotNone(row["finished_at"])


class CommonGuidanceInvocationTests(TestCase):
    """All roles share the same durable instruction resolution boundary."""
    def setUp(self):
        import tempfile
        from loop_manager_internal.agents import common_guidance
        self.guidance_module = common_guidance
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'runtime'
        (self.runtime / 'docs').mkdir(parents=True)
        self.source = self.runtime / common_guidance.COMMON_PATH
        self.source.write_text('자동 지침 "quoted" \\path\n두 번째 줄\n', encoding='utf-8', newline='\n')
        self.role_texts = {}
        (self.runtime / common_guidance.PONYTAIL_PATH).write_text('Ponytail fixture\n', encoding='utf-8', newline='\n')
        for path in set(common_guidance.ROLE_PATHS.values()):
            self.role_texts[path] = '정적 역할: ' + path + '\n'
            (self.runtime / path).write_text(self.role_texts[path], encoding='utf-8', newline='\n')
        self.state_dir = self.root / 'state'
        self.worktree = self.root / 'candidate'
        self.worktree.mkdir()
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        state.ensure_schema(self.connection)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(common_guidance, 'RUNTIME_ROOT', self.runtime).start()
        self.requests = []
        outer = self
        class Provider:
            def run(self, request):
                outer.requests.append(request)
                request.on_session_started('session-' + str(len(outer.requests)))
                return manager.AgentResult(0, 'done', 'session-' + str(len(outer.requests)))
        self.provider = Provider()
        self.provider_mock = mock.patch.object(manager, 'agent_provider', return_value=self.provider).start()

    def invoke(self, role='worker', plan='G-147', session=None, task=None, provider='codex', prompt='delta only'):
        return manager.run_agent(
            provider, self.worktree, self.state_dir, self.root / 'config.ini', role,
            prompt, read_only=role in {'reviewer', 'planner_check'},
            persist_session=True, session_id=session, connection=self.connection,
            invocation=invocation.InvocationContext('repo', plan, task_id=task),
            resolved_profile=invocation.ResolvedProfile('model', 'high'),
        )

    def test_real_worker_prompts_deliver_static_rules_only_in_pinned_instruction(self):
        root = Path(__file__).resolve().parents[1]
        for filename in self.guidance_module.GUIDANCE_PATHS:
            (self.runtime / filename).write_bytes((root / filename).read_bytes())
        path = 'docs/plans/[G-147] Instruction.md'
        prompts = (
            manager.worker_prompt(path, None),
            manager.worker_prompt(path, 'new correction', session_resume=True),
            manager.worker_prompt(path, None, 'PRIVATE OLD WORKER RESULT', session_recovery=True),
        )
        for prompt in prompts:
            result = self.invoke(prompt=prompt)
            self.assertEqual(result.code, 0)
            request = self.requests[-1]
            instruction = request.common_guidance.content
            self.assertEqual(request.prompt, prompt)
            for rule in ('최소 범위의 targeted unit test만', '개별 test case·class·module',
                         '공백을 정규화한 뒤 정확히 같아야', 'Worker는 Git add·commit·fetch·rebase·push·PR',
                         'frontmatter는 직접 변경하지', 'sources 문자열은 보존',
                         '모든 Requirement', 'Ponytail', 'User Decision',
                         'VERIFY_INPUT_EXCLUSIONS:', '--review-request .',
                         '어떤 내용이어도 소비되지 않음',
                         '스크립트와 같은 디렉터리의 `inputs.py`'):
                self.assertIn(rule, instruction)
                self.assertNotIn(rule, prompt)
            self.assertNotIn('PRIVATE OLD WORKER RESULT', prompt)
            self.assertNotIn('AGENTS.md', prompt)
        self.assertNotIn(path, prompts[1])
        self.assertEqual(len({r.common_guidance.content_hash for r in self.requests}), 1)

    def test_always_adds_ponytail_only_to_worker_without_plugin_queries_and_records_hash(self):
        self.assertEqual(self.invoke().code, 0)
        selected = self.requests[-1].common_guidance
        self.assertEqual(selected.content.count('Ponytail fixture'), 1)
        self.assertEqual(selected.instruction_path.read_text(encoding='utf-8'), selected.content)
        row = self.connection.execute('SELECT guidance_content_hash FROM agent_invocations').fetchone()
        self.assertEqual(row[0], selected.content_hash)
        self.assertEqual(self.invoke(session='session-1').code, 0)
        self.assertEqual(self.requests[-1].common_guidance, selected)
        for role in ('planner', 'maintenance_planner', 'planner_check', 'reviewer'):
            self.assertEqual(self.invoke(role=role, plan=None, task=role).code, 0)
            self.assertNotIn('Ponytail fixture', self.requests[-1].common_guidance.content)
            self.assertEqual(self.requests[-1].prompt, 'delta only')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_guidance_versions').fetchone()[0], 0)
        self.assertFalse((self.state_dir / 'agent-guidance').exists())

    def test_provider_switch_reads_current_ponytail_and_records_each_delivery(self):
        self.assertEqual(self.invoke(provider='codex').code, 0)
        original = self.requests[-1].common_guidance
        fingerprint = self.guidance_module.worker_content_hash(self.connection, 'repo', 'G-147')
        source_fingerprint = self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-147')
        (self.runtime / self.guidance_module.PONYTAIL_PATH).write_text('updated Ponytail')
        self.assertEqual(self.invoke(provider='claude').code, 0)
        added = self.requests[-1].common_guidance
        self.assertEqual(added.task_id, original.task_id)
        self.assertIn('updated Ponytail', added.content)
        self.assertNotEqual(added.content_hash, fingerprint)
        self.assertEqual(added.content_hash,
                         self.guidance_module.worker_content_hash(self.connection, 'repo', 'G-147'))
        self.assertNotEqual(source_fingerprint,
                            self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-147'))
        self.assertEqual(self.invoke(provider='codex', session='session-1').code, 0)
        self.assertEqual(self.requests[-1].common_guidance, added)
        rows = self.connection.execute('SELECT guidance_content_hash FROM agent_invocations ORDER BY rowid').fetchall()
        self.assertEqual([r[0] for r in rows], [original.content_hash, added.content_hash, added.content_hash])

    def test_ponytail_change_reaches_existing_and_new_tasks(self):
        self.assertEqual(self.invoke().code, 0)
        original = self.requests[-1].common_guidance
        (self.runtime / self.guidance_module.PONYTAIL_PATH).write_text('updated Ponytail')
        for plan in ('G-147', 'G-148'):
            self.assertEqual(self.invoke(plan=plan).code, 0)
            self.assertIn('updated Ponytail', self.requests[-1].common_guidance.content)
            self.assertNotEqual(self.requests[-1].common_guidance.content_hash, original.content_hash)

    def test_legacy_version_and_missing_snapshot_do_not_override_current_runtime(self):
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        identity = self.guidance_module.task_identity(
            invocation.InvocationContext('repo', 'G-147'), 'worker', self.worktree)
        repository = GuidanceRepository(self.connection)
        repository.save(identity, 'repo', 'a' * 64, 'b' * 40, '/missing-old-snapshot.json')
        repository.bind_session('repo', 'codex', 'legacy-worker', identity)
        before = tuple(repository.get(identity))
        self.assertEqual(self.invoke(session='legacy-worker').code, 0)
        self.assertIn('Ponytail fixture', self.requests[-1].common_guidance.content)
        self.assertEqual(tuple(repository.get(identity)), before)
        self.assertEqual(self.requests[-1].session_id, 'legacy-worker')

    def test_invalid_ponytail_source_is_a_guidance_failure(self):
        path = self.runtime / self.guidance_module.PONYTAIL_PATH
        for value in (b'', b'\xff', b'instructions\0'):
            path.write_bytes(value)
            self.assertEqual(self.invoke().result_kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        path.unlink()
        self.assertEqual(self.invoke().result_kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        self.assertEqual(self.requests, [])

    def test_modern_session_fixture_binds_selected_plan_and_models_only_pr_body_reads(self):
        from types import SimpleNamespace
        from tests.support.worker import dummy_worker_context
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        repository = mock.Mock()
        repository.projected.return_value = SimpleNamespace(
            plan_id='G-147', worktree=str(self.worktree), worker_session_id='saved-worker',
            reviewer_session_id='saved-reviewer', worker_session_binding=None)
        pr = {'body': '', 'state': 'OPEN', 'headRefOid': 'head'}
        with mock.patch.object(manager, '_ExecutionLifecyclePorts', return_value='ports') as ports, \
             mock.patch.object(manager, 'command', return_value='delegated') as command, \
             mock.patch.object(manager, 'git', return_value=SimpleNamespace(stdout='branch')), \
             mock.patch.object(manager, 'pr_for_branch', return_value=pr):
            with dummy_worker_context():
                for provider in ('codex', 'claude'):
                    result = manager._ExecutionLifecyclePorts(self.connection, repository,
                        repo='repo', state_dir=self.state_dir, config_path=Path('unused.ini'), provider=provider)
                    self.assertEqual(result, 'ports')
                observation = manager.command(['gh', 'pr', 'view', 'url', '--json',
                    'body,headRefOid,state'], cwd=self.worktree)
                import json
                self.assertEqual(json.loads(observation.stdout), pr)
                self.assertEqual(manager.command(['gh', 'pr', 'create']), 'delegated')
        self.assertEqual(ports.call_count, 2)
        command.assert_called_once_with(['gh', 'pr', 'create'])
        saved = GuidanceRepository(self.connection)
        context = invocation.InvocationContext('repo', 'G-147')
        identity = self.guidance_module.task_identity(context, 'worker', self.worktree)
        self.assertEqual(saved.session_task('repo', 'codex', 'saved-worker'), identity)
        self.assertEqual(saved.session_task('repo', 'codex', 'saved-reviewer'), identity)
        self.assertEqual(saved.session_task('repo', 'claude', 'saved-worker'), identity)
        self.assertEqual(saved.session_task('repo', 'claude', 'saved-reviewer'), identity)
        self.assertEqual(len(self.requests), 0)

    def test_independent_planner_and_worker_keep_separate_session_bindings(self):
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        planner = self.invoke(role='planner', plan=None, task='planning-input')
        worker = self.invoke()
        self.assertEqual((planner.code, worker.code), (0, 0))
        repository = GuidanceRepository(self.connection)
        planner_task = repository.session_task('repo', 'codex', planner.session_id)
        worker_task = repository.session_task('repo', 'codex', worker.session_id)
        self.assertNotEqual(planner.session_id, worker.session_id)
        self.assertNotEqual(planner_task, worker_task)
        self.assertEqual(planner_task, self.requests[0].common_guidance.task_id)
        self.assertEqual(worker_task, self.requests[1].common_guidance.task_id)
        with self.assertRaisesRegex(RuntimeError, '다른 공통 지침 작업'):
            repository.bind_session('repo', 'codex', planner.session_id, worker_task)
        self.assertEqual(repository.session_task('repo', 'codex', planner.session_id), planner_task)

    def test_interrupted_independent_session_uses_current_runtime_and_preserves_audit(self):
        outer = self
        class InterruptedProvider:
            def run(self, request):
                outer.requests.append(request)
                request.on_session_started('interrupted-planner')
                raise KeyboardInterrupt()
        self.provider_mock.return_value = InterruptedProvider()
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(role='planner', plan=None, task='planning-input')
        original = self.requests[-1].common_guidance
        self.source.write_text('new runtime instructions')
        self.provider_mock.return_value = self.provider
        result = self.invoke(role='planner', plan=None, task='planning-input', session='interrupted-planner')
        self.assertEqual(result.code, 0)
        self.assertNotEqual(self.requests[-1].common_guidance.content_hash, original.content_hash)
        self.assertIn('new runtime instructions', self.requests[-1].common_guidance.content)
        self.assertEqual(self.requests[-1].session_id, 'interrupted-planner')
        audit = self.connection.execute(
            "SELECT result_kind, session_id FROM agent_invocations WHERE result_kind = 'interrupted'").fetchall()
        self.assertEqual([tuple(row) for row in audit], [('interrupted', 'interrupted-planner')])

    def test_all_roles_retries_and_new_tasks_read_current_runtime_not_candidate(self):
        self.assertEqual(self.invoke().code, 0)
        original = self.requests[-1].common_guidance
        (self.worktree / 'docs').mkdir()
        (self.worktree / self.guidance_module.COMMON_PATH).write_text('candidate changes', encoding='utf-8')
        self.source.write_text('new runtime instructions\n', encoding='utf-8')
        for role, session in (('worker', 'session-1'), ('reviewer', None), ('worker', None)):
            self.assertEqual(self.invoke(role=role, session=session).code, 0)
            selected = self.requests[-1].common_guidance
            self.assertEqual(selected.task_id, original.task_id)
            self.assertTrue(selected.content.startswith('new runtime instructions\n\n'))
            self.assertNotIn('candidate changes', selected.content)
            self.assertEqual(self.requests[-1].prompt, 'delta only')
        for role in ('planner', 'maintenance_planner', 'planner_check', 'worker', 'reviewer'):
            self.assertEqual(self.invoke(role=role, plan=None, task=role).code, 0)
            self.assertTrue(self.requests[-1].common_guidance.content.startswith('new runtime instructions\n\n'))

    def test_one_shot_rejects_state_inside_candidate_before_any_database_write(self):
        state_dir = self.worktree / 'state'
        result = manager.run_agent('codex', self.worktree, state_dir, self.root / 'config',
            'worker', 'delta only', read_only=False, resolved_profile=invocation.ResolvedProfile('model', 'high'))
        self.assertEqual(result.kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        self.assertFalse(state_dir.exists())
        self.provider_mock.assert_not_called()

    def test_legacy_session_uses_existing_minimal_context_fallback_and_keeps_audit(self):
        attempts = invocation.AttemptSequence('repo', 'G-147')
        call = invocation.TrackedAgentCall(
            lambda session, fallback, context: manager.run_agent(
                'codex', self.worktree, self.state_dir, self.root / 'config', 'worker',
                'new role/task context' if fallback else 'delta only', read_only=False,
                connection=self.connection, invocation=context, session_id=session, persist_session=True,
                resolved_profile=invocation.ResolvedProfile('model', 'high')),
            attempts,
        )
        result = manager.run_agent_with_retries(call, session_id='legacy')
        self.assertEqual(result.code, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.requests[0].session_id)
        self.assertEqual(self.requests[0].prompt, 'new role/task context')
        rows = self.connection.execute('SELECT session_id, transition, result_code FROM agent_invocations ORDER BY rowid').fetchall()
        self.assertEqual([tuple(row) for row in rows], [('legacy', 'resume', 1), ('session-1', 'fallback', 0)])

    def test_legacy_snapshots_are_not_read_or_replaced(self):
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        directory = self.state_dir / 'agent-guidance'
        directory.mkdir(parents=True, mode=0o700)
        for index, contents in enumerate((b'old instructions', b'\xff', b'')):
            path = directory / (str(index) + '.json')
            path.write_bytes(contents)
            plan = 'G-legacy-' + str(index)
            identity = self.guidance_module.task_identity(invocation.InvocationContext('repo', plan), 'worker', self.worktree)
            GuidanceRepository(self.connection).save(identity, 'repo', 'a' * 64, 'b' * 40, str(path))
            self.assertEqual(self.invoke(plan=plan).code, 0)
            self.assertEqual(path.read_bytes(), contents)

    def test_invalid_runtime_and_failed_execution_file_reject_before_process(self):
        for value in (b'', b' \n', b'\xff', b'a\0b'):
            with self.subTest(value=value):
                self.source.write_bytes(value)
                self.assertEqual(self.invoke().code, 1)
                self.assertEqual(self.requests, [])
        self.source.unlink()
        self.assertEqual(self.invoke().code, 1)
        self.source.mkdir()
        self.assertEqual(self.invoke().code, 1)
        self.source.rmdir()
        self.source.write_text('valid', encoding='utf-8')
        with mock.patch.object(self.guidance_module, '_store_instructions', side_effect=OSError('disk full')):
            self.assertEqual(self.invoke().code, 1)
        self.assertEqual(self.requests, [])

    def test_same_contents_from_another_runtime_keep_reuse_hashes(self):
        import shutil
        self.assertEqual(self.invoke().code, 0)
        saved = self.requests[-1].common_guidance
        source_hash = self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-147')
        new_runtime = self.root / 'new-runtime'
        shutil.copytree(self.runtime, new_runtime)
        with mock.patch.object(self.guidance_module, 'RUNTIME_ROOT', new_runtime):
            self.assertEqual(self.invoke(session='session-1').code, 0)
            self.assertEqual(self.requests[-1].common_guidance, saved)
            self.assertEqual(self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-147'), source_hash)

    def test_session_cannot_cross_tasks_or_provider_preflight_failures(self):
        self.invoke()
        before = len(self.requests)
        result = self.invoke(plan='G-148', session='session-1')
        self.assertEqual(result.code, 1)
        self.assertEqual(len(self.requests), before)
        with mock.patch.object(self.provider, 'run', side_effect=self.guidance_module.GuidanceError('unsupported')):
            result = self.invoke()
        self.assertEqual(result.code, 1)
        row = self.connection.execute('SELECT result_kind, result_code FROM agent_invocations ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(row), ('permanent_configuration', 1))

    def test_each_role_receives_only_common_and_its_role_outside_git(self):
        common = self.source.read_text(encoding='utf-8')
        for role, path in self.guidance_module.ROLE_PATHS.items():
            with self.subTest(role=role):
                self.assertEqual(self.invoke(role=role, plan=None, task=role).code, 0)
                request = self.requests[-1]
                guidance = request.common_guidance
                expected = common.rstrip('\n') + '\n\n' + self.role_texts[path]
                if role == 'worker':
                    expected = expected.rstrip('\n') + '\n\nPonytail fixture\n'
                self.assertEqual(guidance.content, expected)
                self.assertEqual(guidance.instruction_path.read_text(encoding='utf-8'), guidance.content)
                self.assertTrue(guidance.instruction_path.is_relative_to(self.state_dir))
                self.assertFalse(guidance.instruction_path.is_relative_to(self.worktree))
                self.assertFalse(guidance.instruction_path.is_relative_to(self.runtime))
                self.assertEqual(request.prompt, 'delta only')
                self.assertEqual(request.role_name, role)

    def test_missing_current_runtime_is_rejected_even_with_an_old_success(self):
        self.assertEqual(self.invoke().code, 0)
        for path in self.guidance_module.GUIDANCE_PATHS:
            (self.runtime / path).unlink()
        for role in ('worker', 'reviewer'):
            self.assertEqual(self.invoke(role=role).result_kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        self.assertEqual(len(self.requests), 1)

    def test_role_only_change_updates_current_source_hash_and_reviewer(self):
        original_hash = self.guidance_module.runtime_content_hash()
        self.assertEqual(self.invoke().code, 0)
        worker_hash = self.guidance_module.worker_content_hash(self.connection, 'repo', 'G-147')
        reviewer_path = self.runtime / self.guidance_module.ROLE_PATHS['reviewer']
        reviewer_path.write_text('new reviewer constraints\n', encoding='utf-8')
        changed_hash = self.guidance_module.runtime_content_hash()
        self.assertNotEqual(changed_hash, original_hash)
        self.assertEqual(self.guidance_module.plan_content_hash(self.connection, 'repo', 'G-147'), changed_hash)
        self.assertEqual(self.guidance_module.planning_content_hash(self.connection, 'repo'), changed_hash)
        self.assertEqual(self.invoke(role='reviewer').code, 0)
        self.assertIn('new reviewer constraints', self.requests[-1].common_guidance.content)
        self.assertEqual(self.guidance_module.worker_content_hash(self.connection, 'repo', 'G-147'), worker_hash)

    def test_missing_invalid_role_sources_and_unknown_roles_never_start_provider(self):
        path = self.runtime / self.guidance_module.ROLE_PATHS['reviewer']
        for value in (b'', b' \n', b'\xff', b'a\0b'):
            with self.subTest(value=value):
                path.write_bytes(value)
                self.assertEqual(self.invoke().result_kind, invocation.AgentResultKind.PERMANENT_CONFIGURATION)
        path.unlink()
        self.assertEqual(self.invoke().code, 1)
        path.mkdir()
        self.assertEqual(self.invoke().code, 1)
        path.rmdir()
        original_lstat = Path.lstat
        # 파일 형식 거부는 OS 관측값으로 검사해 Windows symlink 권한에 의존하지 않는다.
        with mock.patch.object(Path, 'lstat', autospec=True, side_effect=lambda candidate, *a, **kw:
                               mock.Mock(st_mode=stat.S_IFLNK) if candidate == path
                               else original_lstat(candidate, *a, **kw)):
            self.assertEqual(self.invoke().code, 1)
        path.write_text('valid role', encoding='utf-8', newline='\n')
        self.assertEqual(self.invoke(role='../worker').code, 1)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_guidance_versions').fetchone()[0], 0)

    def test_changed_combined_file_is_rejected_instead_of_overwritten(self):
        self.assertEqual(self.invoke().code, 0)
        saved = self.requests[-1].common_guidance
        saved.instruction_path.write_text('changed derived instructions', encoding='utf-8', newline='\n')
        self.assertEqual(self.invoke().code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(saved.instruction_path.read_text(encoding='utf-8'), 'changed derived instructions')

    def test_concurrent_snapshot_publication_never_exposes_partial_bytes(self):
        from concurrent.futures import ThreadPoolExecutor
        directory = self.state_dir / 'agent-guidance'
        directory.mkdir(parents=True, mode=0o700)
        contents = ('shared instructions\n' * 10000).encode()
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [workers.submit(self.guidance_module._store_instructions, directory, contents, '.md')
                       for _ in range(8)]
            paths = [future.result() for future in futures]
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(paths[0].read_bytes(), contents)
        self.assertEqual(list(directory.iterdir()), [paths[0]])

    def test_execution_file_is_recreated_from_current_runtime(self):
        self.assertEqual(self.invoke().code, 0)
        current = self.requests[-1].common_guidance
        current.instruction_path.unlink()
        self.assertEqual(self.invoke(session='session-1').code, 0)
        self.assertEqual(current.instruction_path.read_text(encoding='utf-8'), current.content)

    def test_legacy_markdown_does_not_pin_current_role_rules(self):
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        identity = self.guidance_module.task_identity(invocation.InvocationContext('repo', 'G-legacy'), 'worker', self.worktree)
        path = self.root / 'old.md'
        path.write_text('old rules')
        repository = GuidanceRepository(self.connection)
        repository.save(identity, 'repo', 'a' * 64, 'b' * 40, str(path))
        for role in ('worker', 'reviewer'):
            self.assertEqual(self.invoke(role=role, plan='G-legacy').code, 0)
            self.assertIn(self.role_texts[self.guidance_module.ROLE_PATHS[role]], self.requests[-1].common_guidance.content)
            self.assertNotIn('old rules', self.requests[-1].common_guidance.content)
        self.assertEqual(path.read_text(), 'old rules')


class ProcessStartNotificationTests(TestCase):
    setUp = RunnerTests.setUp

    def test_planner_check_physical_start_consumes_confirmed_planning_block(self):
        from loop_manager_internal.notifications import notifications
        self.connection.execute(manager.SCHEMA)
        manager._ensure_execplans_schema(self.connection)
        notifications.ensure_schema(self.connection)
        notifications.configure_session(self.connection, enabled=True)
        self.connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")
        self.connection.execute(
            "UPDATE projects SET agent_role = 'planner_check', agent_snapshot = ?, worktree = ? WHERE repo = 'repo'",
            (manager.GitSnapshot('head', None, {}, {}, {}, {}, {}, {}, scope_id='planning-scope').serialize(),
             str(Path('/worktree'))),
        )
        self.connection.commit()
        manager.update_project(self.connection, 'repo', planning_error='original planning cause')
        from tests.support.notification import NotificationFixture
        NotificationFixture.legacy_planning_notice(self.connection, 'original planning cause', repo='repo', confirmed=1, sent=True)
        self.connection.execute(
            "UPDATE notification_outbox SET sent_at = CURRENT_TIMESTAMP, "
            "confirmed_pieces = 1 WHERE event_type = 'blocked'"
        )
        manager.update_project(self.connection, 'repo', planning_error=None)

        outer = self
        class Provider:
            def run(self, request):
                outer.assertEqual(request.role_name, 'planner_check')
                outer.assertTrue(request.read_only)
                outer.assertEqual(outer.connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'resumed'"
                ).fetchone()[0], 0)
                request.on_process_started()
                outer.assertEqual(outer.connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'resumed'"
                ).fetchone()[0], 0)
                outer.assertIsNotNone(outer.connection.execute(
                    "SELECT started_at FROM notified_block_incidents WHERE subject='planning'"
                ).fetchone()[0])
                return manager.AgentResult(0, 'checked')

        with mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), self.state_dir, Path('/config'),
                'planner_check', 'read only check', read_only=True,
                connection=self.connection,
                invocation=invocation.InvocationContext('repo', None),
                resolved_profile=invocation.ResolvedProfile('check-model', 'low'),
            )
        self.assertEqual(result.code, 0)
        incident = self.connection.execute(
            "SELECT payload, started_operation, resumed_at FROM notified_block_incidents WHERE subject='planning'"
        ).fetchone()
        self.assertEqual(incident['started_operation'], 'Planner')
        self.assertIsNotNone(incident['resumed_at'])
        self.assertEqual(json.loads(incident['payload'])['cause'], 'original planning cause')
        ledger_row = self.connection.execute(
            "SELECT role, model, effort, scope_id FROM agent_invocations"
        ).fetchone()
        self.assertEqual(tuple(ledger_row),
                         ('planner_check', 'check-model', 'low', 'planning-scope'))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'work_started'"
        ).fetchone()[0], 0)

    def test_process_callback_precedes_work_and_internal_retry_does_not_notify(self):
        from loop_manager_internal.notifications import notifications
        self.connection.execute(manager.SCHEMA)
        manager._ensure_execplans_schema(self.connection)
        notifications.ensure_schema(self.connection)
        notifications.configure_session(self.connection, enabled=True)
        self.connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")
        self.connection.execute("INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status) "
                                "VALUES ('repo','G-041','plan.md','Named plan','[]','[]','normal','hash','IMPLEMENTING')")
        self.connection.commit()
        outer = self
        class Provider:
            def run(self, request):
                outer.assertEqual(outer.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
                request.on_process_started()
                outer.assertEqual(outer.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 0)
                outer.assertIsNotNone(outer.connection.execute(
                    "SELECT execution_started_at FROM agent_invocations ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0])
                return manager.AgentResult(0, 'done')
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            for attempt in (1, 2):
                result = manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                    'worker', 'first' if attempt == 1 else 'retry', read_only=False, level=5,
                    connection=self.connection, invocation=invocation.InvocationContext('repo','G-041',attempt=attempt,notification_id='retry-sequence'),
                    resolved_profile=invocation.ResolvedProfile('model','high'))
                self.assertEqual(result.code, 0)

    def test_prelaunch_failure_keeps_notified_block_unresumed(self):
        from loop_manager_internal.notifications import notifications
        # Start callback is never reached when provider launch fails.
        calls = []
        class Provider:
            def run(self, request):
                calls.append(request.on_process_started)
                raise OSError('launch failed')
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()), \
             mock.patch.object(notifications, 'record_process_start') as start:
            with self.assertRaises(OSError):
                manager.run_agent('codex', Path('/worktree'), self.state_dir, Path('/config'),
                    'worker', 'first', read_only=False, level=5, connection=self.connection,
                    invocation=invocation.InvocationContext('repo','G-041'),
                    resolved_profile=invocation.ResolvedProfile('model','high'))
        self.assertTrue(calls)
        start.assert_not_called()
        row = self.connection.execute(
            "SELECT execution_status, result_kind FROM agent_invocations"
        ).fetchone()
        self.assertEqual(tuple(row), ("not_executed", "transient_failure"))

    def test_unconfigured_notifications_track_process_without_forcing_streaming(self):
        outer = self
        class Provider:
            def run(self, request):
                outer.assertIsNotNone(request.on_process_started)
                outer.assertIsNotNone(request.on_process_finished)
                outer.assertFalse(request.stream_output)
                return manager.AgentResult(0, 'done')
        with mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent(
                'codex', Path('/worktree'), self.state_dir, Path('/config'),
                'planner', 'prompt', read_only=False, connection=self.connection,
                invocation=invocation.InvocationContext('repo', None),
                resolved_profile=invocation.ResolvedProfile('model', 'high'),
            )
        self.assertEqual(result.code, 0)
