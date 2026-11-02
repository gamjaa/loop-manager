"""Managed Claude calls with native startup and process doubles only."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest
from unittest import mock

import loop_manager as manager
from loop_manager_internal.agents import agent_claude_preflight as preflight
from loop_manager_internal.agents import agent_write_isolation as isolation
from loop_manager_internal.agents import common_guidance, process_receipt, provider_adapters, provider_guidance
from loop_manager_internal.agents.agent_invocation import AgentRequest, InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.storage import agent_invocation_state as state
from tests.support.profiles import profile_config, use_empty_provider_home
from tests.support.workspace import pin_common_guidance, PROJECT_FILES


@unittest.skipIf(os.name == 'nt', 'Managed Claude requires a POSIX native sandbox')
class ClaudeManagedRuntimeTests(unittest.TestCase):
    def setUp(self):
        use_empty_provider_home(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.short_temp_requests = []
        make_directory = tempfile.mkdtemp
        def owned_temp(*args, **kwargs):
            if kwargs.get('prefix') == 'lm-':
                self.short_temp_requests.append(dict(kwargs))
                kwargs = dict(kwargs, dir=self.root)
            directory = make_directory(*args, **kwargs)
            if kwargs.get('prefix') == 'lm-':
                self.addCleanup(shutil.rmtree, directory)
            return directory
        self.enterContext(mock.patch.object(tempfile, 'mkdtemp', side_effect=owned_temp))
        self.workspace, self.state, self.system = (self.root / name for name in ('workspace', 'state', 'system'))
        for path in (self.workspace, self.state, self.system):
            path.mkdir()
        self.config = profile_config(self)
        self.boundary = isolation.WorktreeBoundary(self.workspace, self.root / 'source',
            (self.root / 'other',), self.root / 'source/.git/worktrees/G-191',
            self.root / 'source/.git', 'repo')
        self.profile = ResolvedProfile('claude-sonnet-test', 'high')
        self.calls, self.records, self.sessions, self.launches, self.order = [], [], [], [], []
        self.mutation, self.extra_event, self.missing_session = {}, None, False
        self.zero_exit_error = False
        self.native = self.enterContext(mock.patch.object(preflight, '_command', side_effect=self.probe))
        self.enterContext(mock.patch.object(preflight, '_system_directory', return_value=self.system))
        self.enterContext(mock.patch.object(preflight.platform, 'system', return_value='Linux'))
        self.enterContext(mock.patch.object(preflight.platform, 'release', return_value='linux-test'))
        self.enterContext(mock.patch.object(isolation, 'inspect_worktree', return_value=self.boundary))
        self.stop = self.enterContext(mock.patch.object(isolation.CodexLaunch, 'stop',
                                                       side_effect=lambda process: self.order.append('stop')))
        self.real_receipt_started, self.real_receipt_finished = process_receipt.started, process_receipt.finished
        self.enterContext(mock.patch.object(process_receipt, 'started', side_effect=lambda *_: self.order.append('receipt-start')))
        self.enterContext(mock.patch.object(process_receipt, 'finished', side_effect=lambda *_: self.order.append('receipt-finish')))
        self.enterContext(mock.patch.object(provider_adapters, '_start_timeout', return_value=mock.Mock()))
        self.effects = provider_adapters.ProviderEffects(
            role=mock.Mock(), worker_profile=mock.Mock(), _agent_profile=mock.Mock(),
            _handoff_popen=mock.Mock(side_effect=self.spawn), _handoff_run=mock.Mock())

    def probe(self, arguments, environment, worktree):
        if arguments[1:] == ['--version']:
            return '2.1.307 (Claude Code)'
        if arguments[-1:] == ['--help']:
            return '--restricted --setting-sources --settings --permission-mode --permission-prompts --tools --disallowedTools --strict-mcp-config'
        if arguments[-2:] == ['auth', 'status']:
            return json.dumps({'authMethod': 'api_key', 'configDirectory': environment['CLAUDE_CONFIG_DIR']})
        if arguments[-1] == 'doctor':
            return 'No settings errors'
        raise AssertionError(arguments)

    def spawn(self, arguments, **context):
        self.order.append('spawn')
        self.calls.append((arguments, context))
        session = arguments[arguments.index('--resume') + 1] if '--resume' in arguments else 'new-session'
        init = {'type': 'system', 'subtype': 'init', 'session_id': session,
            'permissionMode': 'auto', 'model': self.profile.model, 'cwd': str(self.workspace),
            'claude_code_version': '2.1.307', 'tools': arguments[arguments.index('--tools') + 1].split(','),
            'mcp_servers': [], 'plugins': []}
        init.update(self.mutation)
        result = {'type': 'result', 'session_id': session, 'is_error': False, 'result': 'done'}
        if self.zero_exit_error:
            events = [{'type': 'error', 'message': 'authentication expired'}]
            code = 0
        elif self.missing_session and '--resume' in arguments:
            events = [dict(result, is_error=True, result=f'session {session} not found')]
            code = 1
        else:
            events = [init, *([self.extra_event] if self.extra_event else []), result]
            code = 0
        return mock.Mock(stdin=mock.Mock(), stdout=io.StringIO('\n'.join(map(json.dumps, events)) + '\n'),
                         wait=mock.Mock(return_value=code), returncode=code, pid=12345)

    def run_claude(self, role='worker', session=None, **kwargs):
        def observe(evidence):
            self.order.append(evidence.status)
            self.records.append(evidence)
        def bind(launch):
            self.order.append('bound')
            self.launches.append(launch)
        options = dict(read_only=role in {'reviewer', 'planner_check'}, persist_session=True,
            session_id=session, resolved_profile=self.profile, on_isolation_checked=observe,
            isolation_snapshot_digest=lambda: 'invocation-context', expected_repository=str(self.boundary.source),
            on_session_started=self.sessions.append, on_launch_prepared=bind,
            on_process_started=lambda: self.order.append('started'),
            on_process_finished=lambda: self.order.append('finished'))
        options.update(kwargs)
        return provider_adapters.run_claude(self.workspace, self.state, self.config, role,
                                            'task prompt', effects=self.effects, **options)

    def test_all_roles_new_and_resume_use_fresh_prepared_native_policy(self):
        for role in ('planner', 'planner_check', 'maintenance_planner', 'worker', 'reviewer'):
            for session in (None, 'saved-session'):
                with self.subTest(role=role, session=session):
                    self.assertEqual(self.run_claude(role, session).code, 0)
                    arguments, context = self.calls[-1]
                    self.assertTrue(context['start_new_session'])
                    self.assertNotIn('--restricted', arguments)
                    self.assertEqual(context['env']['TMPDIR'], str(self.launches[-1].bash_temporary))
                    settings = json.loads(arguments[arguments.index('--settings') + 1])
                    self.assertIn('Edit', settings['permissions']['deny'])
                    self.assertFalse({'Edit', 'Write', 'NotebookEdit'} &
                                     set(arguments[arguments.index('--tools') + 1].split(',')))
                    package = str(Path(preflight.__file__).resolve().parents[1])
                    filesystem = settings['sandbox']['filesystem']
                    self.assertEqual(filesystem['allowRead'], [package] if role == 'worker' else [])
                    if role == 'worker':
                        self.assertIn(package, filesystem['denyWrite'])
                        self.assertNotIn(package, settings['permissions']['additionalDirectories'])
                    self.assertEqual(str(self.workspace) in settings['sandbox']['filesystem']['denyWrite'],
                                     role in {'reviewer', 'planner_check'})
                    self.assertEqual(self.records[-1].status, 'prepared')
                    self.assertEqual(json.loads(self.records[-1].reason)['runtime_enforcement'], 'not_observed')
        self.assertEqual(len({launch.attempt for launch in self.launches}), 10)
        self.effects._handoff_run.assert_not_called()
        self.assertLess(self.order.index('prepared'), self.order.index('spawn'))
        self.assertLess(self.order.index('bound'), self.order.index('spawn'))
        self.assertLess(self.order.index('receipt-finish'), self.order.index('finished'))

    def test_missing_saved_session_fallback_reprepares_the_same_boundary(self):
        self.missing_session = True
        def spawn(arguments, **context):
            process = self.spawn(arguments, **context)
            if '--resume' in arguments:
                # A failed CLI call may leave new settings for the next call.
                home = Path(context['env']['CLAUDE_CONFIG_DIR'])
                (home / 'settings.json').write_text('{"model":"next-call"}')
            return process
        self.effects._handoff_popen.side_effect = spawn
        result = manager.run_agent_with_retries(lambda session, recovery: self.run_claude(session=session),
                                                session_id='saved-session')
        self.assertEqual((result.code, result.session_id), (0, 'new-session'))
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.launches[0].attempt, self.launches[1].attempt)
        self.assertNotEqual(json.loads(self.launches[0].reason)['effective_settings_digest'],
                            json.loads(self.launches[1].reason)['effective_settings_digest'])
        home = Path(self.calls[-1][1]['env']['CLAUDE_CONFIG_DIR'])
        self.assertEqual((home / 'settings.json').read_text(), '{"model":"next-call"}')
        self.assertTrue(all(arguments[arguments.index('--setting-sources') + 1] == 'user,project,local'
                            for arguments, _ in self.calls))

    def test_invalid_native_init_is_stopped_and_observed_session_is_preserved(self):
        for mutation in ({'permissionMode': 'acceptEdits'}, {'model': 'other-model'},
                {'cwd': '/other'}, {'claude_code_version': 'old'}, {'tools': ['Bash', 'Read', 'Agent']},
                {'tools': ['Bash', 'Read', 'Write']},
                {'mcp_servers': [{'name': 'external'}]}, {'plugins': [{'name': 'external'}]}):
            with self.subTest(mutation=mutation):
                self.mutation = mutation
                with self.assertRaisesRegex(isolation.IsolationError, '시작 관측'):
                    self.run_claude()
                self.assertEqual(self.sessions[-1], 'new-session')
                self.assertEqual(self.order[-1], 'finished')
        self.stop.assert_called()
        self.assertFalse(any(evidence.status == 'verified' for evidence in self.records))

    def test_observed_provider_security_errors_stop_but_unit_command_failures_can_be_corrected(self):
        for event in ({'type': 'system', 'subtype': 'status', 'permissionMode': 'default'},
                {'type': 'system', 'subtype': 'permission_denied'},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True,
                                                       'content': 'EACCES permission denied'}]}}):
            with self.subTest(event=event):
                self.extra_event = event
                with self.assertRaises(isolation.IsolationError):
                    self.run_claude()
        self.extra_event = {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'is_error': True, 'content': 'unit test assertion failed'}]}}
        self.assertEqual(self.run_claude().code, 0)

    def test_evidence_or_launch_binding_failure_prevents_process_creation(self):
        for callback in ('on_isolation_checked', 'on_launch_prepared'):
            with self.subTest(callback=callback), self.assertRaisesRegex(RuntimeError, 'storage failed'):
                self.run_claude(**{callback: mock.Mock(side_effect=RuntimeError('storage failed'))})
        self.effects._handoff_popen.assert_not_called()

    def test_configuration_drift_after_guidance_is_rejected_before_process_start(self):
        def change(*args, **kwargs):
            (self.state / 'automation-claude/remote-settings.json').write_text('{"disableAutoMode":"disable"}')
            return []
        with mock.patch.object(provider_guidance, 'claude_arguments', side_effect=change), \
                self.assertRaisesRegex(isolation.IsolationError, 'source가 변경'):
            self.run_claude(common_guidance=mock.sentinel.guidance)
        self.effects._handoff_popen.assert_not_called()
        self.assertEqual([evidence.status for evidence in self.records], ['prepared', 'rejected'])

    def test_preparation_is_rechecked_after_profile_lookup_and_old_receipts_are_rejected(self):
        first = self.run_claude()
        self.assertEqual(first.code, 0)
        launch = self.launches[-1]
        (launch.runtime / (launch.attempt.name + '.process.json')).write_text('{}')
        with self.assertRaisesRegex(isolation.IsolationError, '재사용'):
            self.run_claude(prepared_launch=launch)
        self.assertEqual(len(self.calls), 1)

    def test_prepared_environment_is_used_for_profile_resolution_and_adapter_dispatch(self):
        adapter = provider_adapters.ClaudeAgentAdapter(mock.Mock())
        request = AgentRequest(self.workspace, self.state, self.config, 'worker', 'prompt', False,
                               expected_repository=str(self.boundary.source))
        launch = adapter.prepare_environment(request)
        def profile(*args):
            from loop_manager_internal.agents.agent_profiles import agent_environment
            self.assertEqual(agent_environment('claude')['TMPDIR'], str(launch.bash_temporary))
            return self.profile
        self.effects._agent_profile.side_effect = profile
        self.assertEqual(self.run_claude(prepared_launch=launch, resolved_profile=None).code, 0)
        self.assertIs(self.launches[-1], launch)
        adapter.run(replace(request, prepared_launch=launch, on_launch_prepared=self.launches.append))
        self.assertIs(adapter._run.call_args.kwargs['prepared_launch'], launch)
        self.assertEqual(adapter._run.call_args.kwargs['on_launch_prepared'], self.launches.append)

    def test_drift_during_launch_binding_records_nonexecution_and_preserves_prior_message(self):
        def bind(launch):
            self.launches.append(launch)
            (self.state / 'automation-claude/remote-settings.json').write_text('{"disableAutoMode":"disable"}')
        project = provider_adapters._provider_log_directory(self.workspace, self.state)
        previous = project / 'claude-worker-last.txt'
        previous.write_text('previous result')
        with self.assertRaisesRegex(isolation.IsolationError, 'source가 변경'):
            self.run_claude(on_launch_prepared=bind)
        self.effects._handoff_popen.assert_not_called()
        receipt = json.loads(process_receipt.receipt_path(self.launches[-1]).read_text())
        self.assertEqual(receipt['evidence'], 'not_started')
        self.assertEqual(previous.read_text(), 'previous result')
        self.assertEqual(self.records[-1].status, 'rejected')

    def test_unconfirmed_child_shutdown_does_not_record_a_finished_receipt(self):
        self.stop.side_effect = RuntimeError('group shutdown unconfirmed')
        with self.assertRaisesRegex(RuntimeError, 'shutdown unconfirmed'):
            self.run_claude()
        self.assertNotIn('receipt-finish', self.order)
        self.assertNotIn('finished', self.order)

    def test_zero_exit_error_before_init_preserves_the_actual_provider_failure(self):
        self.zero_exit_error = True
        result = self.run_claude()
        self.assertEqual(result.code, 1)
        self.assertIn('authentication expired', result.message)
        self.stop.assert_called()

    def test_runtime_initialization_failure_is_recorded_as_rejected_before_model_execution(self):
        with mock.patch.object(preflight, '_owned_directory', side_effect=RuntimeError('private directory failed')), \
                self.assertRaisesRegex(isolation.IsolationError, 'private directory failed'):
            self.run_claude()
        self.effects._handoff_popen.assert_not_called()
        self.assertEqual(self.records[-1].status, 'rejected')
        self.assertIn('private directory failed', self.records[-1].reason)

    def test_managed_service_persists_call_session_policy_and_targeted_binding(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        context = InvocationContext(str(self.boundary.source), 'G-191')
        pin_common_guidance(connection, context, self.workspace, self.state, provider='claude')
        with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                mock.patch.object(provider_guidance, '_probe', return_value='--append-system-prompt-file --system-prompt-snapshot'), \
                mock.patch.object(manager, '_handoff_popen', side_effect=self.spawn), \
                mock.patch.object(process_receipt, 'started', side_effect=self.real_receipt_started), \
                mock.patch.object(process_receipt, 'finished', side_effect=self.real_receipt_finished):
            result = manager.run_agent('claude', self.workspace, self.state, self.config, 'worker',
                'prompt', read_only=False, connection=connection, invocation=context,
                resolved_profile=self.profile, persist_session=True)
        self.assertEqual((result.code, result.session_id), (0, 'new-session'))
        self.assertFalse(result.targeted_collection_pending)
        row = connection.execute('''SELECT a.session_id,p.status,a.execution_status,t.directory,t.status
            FROM agent_invocations a JOIN agent_isolation_evidence p USING(invocation_id)
            JOIN targeted_test_collections t USING(invocation_id) WHERE invocation_id=?''',
            (result.isolation_invocation_id,)).fetchone()
        self.assertEqual(row, ('new-session', 'prepared', 'executed', self.calls[-1][1]['env']['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'], 'collected'))
        self.assertEqual(connection.execute('SELECT count(*) FROM targeted_test_results').fetchone()[0], 0)

    def test_real_records_follow_claude_retry_and_session_fallback(self):
        from loop_manager_internal.agents.agent_invocation import AttemptSequence, TrackedAgentCall
        from loop_manager_internal.verification.timing import EventWriter
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        context = InvocationContext(str(self.boundary.source), 'G-191')
        pin_common_guidance(connection, context, self.workspace, self.state,
                            sessions=('old-session',), provider='claude')
        def spawn(arguments, **kwargs):
            process = self.spawn(arguments, **kwargs)
            number = len(self.calls)
            directory = Path(kwargs['env']['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'])
            writer = EventWriter(directory / f'targeted-run-{number}.jsonl', identity={'run_id': f'run-{number}'})
            writer.emit('test_end', id='suite.test_one', result='failure')
            writer.close()
            if number < 3:
                message = 'connection reset' if number == 1 else 'session old-session not found'
                process.stdout = io.StringIO(json.dumps({'type': 'result', 'session_id': 'old-session',
                    'is_error': True, 'result': message}) + '\n')
                process.wait.return_value = process.returncode = 1
            return process
        def invoke(session, fallback, invocation):
            return manager.run_agent('claude', self.workspace, self.state, self.config, 'worker',
                'prompt', read_only=False, session_id=session, connection=connection,
                invocation=invocation, resolved_profile=self.profile, persist_session=True)
        call = TrackedAgentCall(invoke, AttemptSequence(context.repo, context.plan_id))
        with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                mock.patch.object(provider_guidance, '_probe', return_value='--append-system-prompt-file --system-prompt-snapshot'), \
                mock.patch.object(manager, '_handoff_popen', side_effect=spawn), \
                mock.patch.object(process_receipt, 'started', side_effect=self.real_receipt_started), \
                mock.patch.object(process_receipt, 'finished', side_effect=self.real_receipt_finished):
            result = manager.run_agent_with_retries(call, session_id='old-session')
        self.assertEqual((result.code, result.session_id), (0, 'new-session'))
        self.assertEqual([args[args.index('--resume') + 1] if '--resume' in args else None
                          for args, _ in self.calls], ['old-session', 'old-session', None])
        rows = connection.execute("""SELECT r.run_id, r.test_id, r.result, o.invocation_id,
            c.directory, c.receipt, a.process_receipt_path FROM targeted_test_results r
            JOIN targeted_test_run_owners o USING(run_id) JOIN targeted_test_collections c USING(invocation_id)
            JOIN agent_invocations a USING(invocation_id) ORDER BY r.sequence""").fetchall()
        self.assertEqual([(r[1], r[2]) for r in rows], [('suite.test_one', 'failure')] * 3)
        self.assertEqual(len({r[0] for r in rows}), 3)
        self.assertEqual(len({r[3] for r in rows}), 3)
        self.assertEqual({r[4] for r in rows}, {c['env']['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'] for _, c in self.calls})
        for row in rows:
            self.assertEqual(row[5], row[6])
            self.assertEqual(Path(row[5]), Path(row[4]).parent / (Path(row[4]).name + '.process.json'))
            self.assertTrue(process_receipt.observation(row[5], self.state, row[3])['finished_at'])
        self.assertEqual(connection.execute('SELECT failures, repeated FROM targeted_test_streaks').fetchone(), (3, 0))

    def test_targeted_binding_database_wait_prevents_spawn_until_recovered(self):
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        context = InvocationContext(str(self.boundary.source), 'G-191')
        pin_common_guidance(connection, context, self.workspace, self.state, provider='claude')
        def invoke():
            return manager.run_agent('claude', self.workspace, self.state, self.config, 'worker',
                'prompt', read_only=False, connection=connection, invocation=context,
                resolved_profile=self.profile, persist_session=True)
        with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                mock.patch.object(provider_guidance, '_probe', return_value='--append-system-prompt-file --system-prompt-snapshot'), \
                mock.patch.object(manager, '_handoff_popen', side_effect=self.spawn), \
                mock.patch.object(process_receipt, 'started', side_effect=self.real_receipt_started), \
                mock.patch.object(process_receipt, 'finished', side_effect=self.real_receipt_finished):
            with mock.patch.object(TargetedTestRepository, 'bind', side_effect=DatabaseWaitTimeout('binding wait')):
                with self.assertRaises(DatabaseWaitTimeout):
                    invoke()
            self.assertEqual(len(self.calls), 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM targeted_test_results').fetchone()[0], 0)
            self.assertEqual(invoke().code, 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(connection.execute('SELECT count(*) FROM targeted_test_streaks').fetchone()[0], 0)

    def test_record_collection_wait_recovers_without_relaunch(self):
        from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
        from loop_manager_internal.verification.timing import EventWriter
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        context = InvocationContext(str(self.boundary.source), 'G-191')
        pin_common_guidance(connection, context, self.workspace, self.state, provider='claude')
        def spawn(arguments, **kwargs):
            process = self.spawn(arguments, **kwargs)
            directory = Path(kwargs['env']['LOOP_MANAGER_TARGETED_TEST_DIRECTORY'])
            writer = EventWriter(directory / 'targeted-run-wait.jsonl', identity={'run_id': 'run-wait'})
            writer.emit('test_end', id='suite.test_one', result='failure')
            writer.close()
            return process
        def invoke():
            return manager.run_agent('claude', self.workspace, self.state, self.config, 'worker',
                'prompt', read_only=False, connection=connection, invocation=context,
                resolved_profile=self.profile, persist_session=True)
        with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                mock.patch.object(provider_guidance, '_probe', return_value='--append-system-prompt-file --system-prompt-snapshot'), \
                mock.patch.object(manager, '_handoff_popen', side_effect=spawn), \
                mock.patch.object(process_receipt, 'started', side_effect=self.real_receipt_started), \
                mock.patch.object(process_receipt, 'finished', side_effect=self.real_receipt_finished):
            with mock.patch.object(TargetedTestRepository, 'reconcile', side_effect=sqlite3.OperationalError('busy')):
                first = invoke()
            self.assertTrue(first.targeted_collection_pending)
            directory, receipt = connection.execute('SELECT directory, receipt FROM targeted_test_collections').fetchone()
            self.assertTrue((Path(directory) / 'targeted-run-wait.jsonl').is_file())
            self.assertTrue(Path(receipt).is_file())
            TargetedTestRepository(connection).reconcile(state_dir=self.state, repo=context.repo, plan_id=context.plan_id)
            TargetedTestRepository(connection).reconcile(state_dir=self.state, repo=context.repo, plan_id=context.plan_id)
        self.assertEqual((first.code, first.message, first.session_id), (0, 'done', 'new-session'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(connection.execute('SELECT count(*) FROM targeted_test_results').fetchone()[0], 1)
        self.assertEqual(connection.execute('SELECT failures, repeated FROM targeted_test_streaks').fetchone(), (1, 0))

    def test_database_wait_recovers_the_original_attempt_message_without_another_process(self):
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        state.ensure_schema(connection)
        context = InvocationContext(str(self.boundary.source), 'G-191')
        pin_common_guidance(connection, context, self.workspace, self.state, provider='claude')
        original_apply = state.InvocationRepository.apply_observation
        def unavailable(repository, identity, value):
            if 'outcome' in value:
                raise DatabaseWaitTimeout('result database unavailable')
            return original_apply(repository, identity, value)
        def invoke():
            return manager.run_agent('claude', self.workspace, self.state, self.config, 'worker',
                'prompt', read_only=False, connection=connection, invocation=context,
                resolved_profile=self.profile, persist_session=True)
        with mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES), \
                mock.patch.object(provider_guidance, '_probe', return_value='--append-system-prompt-file --system-prompt-snapshot'), \
                mock.patch.object(manager, '_handoff_popen', side_effect=self.spawn), \
                mock.patch.object(process_receipt, 'started', side_effect=self.real_receipt_started), \
                mock.patch.object(process_receipt, 'finished', side_effect=self.real_receipt_finished):
            with mock.patch.object(state.InvocationRepository, 'apply_observation', unavailable):
                first = invoke()
            row = connection.execute('SELECT invocation_id,process_receipt_path FROM agent_invocations').fetchone()
            self.assertTrue(process_receipt.observation(row[1], self.state, row[0])['database_pending'])
            # The shared latest message is auxiliary and cannot be recovery input.
            project = provider_adapters._provider_log_directory(self.workspace, self.state)
            (project / 'claude-worker-last.txt').write_text('another invocation result')
            recovered = invoke()
        self.assertEqual((recovered.code, recovered.message, recovered.session_id), (0, 'done', 'new-session'))
        self.assertEqual(first.isolation_invocation_id, recovered.isolation_invocation_id)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(process_receipt.observation(row[1], self.state, row[0])['database_pending'])


class ClaudePreparedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        state.ensure_schema(self.connection)
        self.ledger = state.InvocationRepository(self.connection)
        self.identity = self.ledger.start(InvocationContext('repo', 'G-191'), 'worker', 'claude',
                                          ResolvedProfile('model', 'high'), 'saved-session')
        self.evidence = isolation.IsolationEvidence('claude', 'version', 'Linux', 'worker', '/workspace',
            'repo', 'policy', 'prepared', '{"basis":"documented-native-configuration"}', 'context', 'config')

    def test_prepared_can_be_rejected_before_start_and_is_never_verified_proof(self):
        self.ledger.record_isolation(self.identity, self.evidence)
        self.assertFalse(self.ledger.verified_invocation(self.identity, session_id='saved-session', expected=self.evidence))
        self.ledger.record_isolation(self.identity, replace(self.evidence, status='rejected', reason='settings changed'))
        self.assertEqual(self.connection.execute('SELECT status,reason FROM agent_isolation_evidence').fetchone(),
                         ('rejected', 'settings changed'))

    def test_failure_and_interrupt_keep_original_configuration_basis(self):
        self.ledger.record_isolation(self.identity, self.evidence)
        self.ledger.finish(self.identity, 'exception', None, 1, 'saved-session', Usage())
        self.assertEqual(self.connection.execute('SELECT status,reason FROM agent_isolation_evidence').fetchone(),
                         ('invalidated', self.evidence.reason))
        second = self.ledger.start(InvocationContext('repo', 'G-191'), 'worker', 'claude',
                                  ResolvedProfile('model', 'high'), 'saved-session')
        self.ledger.record_isolation(second, self.evidence)
        self.ledger.reconcile_interrupted()
        self.assertEqual(self.connection.execute('SELECT reason FROM agent_isolation_evidence WHERE invocation_id=?',
                                                (second,)).fetchone()[0], self.evidence.reason)

    def test_preflight_cannot_be_rewritten_after_process_start(self):
        self.ledger.record_isolation(self.identity, self.evidence)
        self.ledger.mark_execution_started(self.identity)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.record_isolation(self.identity, replace(self.evidence, status='rejected'))
