"""Prepared provider inputs and durable physical-call identity boundaries."""
import json
import unittest
from unittest import mock
from loop_manager_internal.agents import agent_profiles, agent_write_isolation as isolation
from loop_manager_internal.agents import provider_adapters
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.storage import agent_invocation_state
from tests.support.provider_execution import ProviderExecutionFixture


class ProviderExecutionTests(ProviderExecutionFixture, unittest.TestCase):
    def replace_instruction_during_sentinel_lookup(self):
        config = self.child_home / 'config.toml'
        config.write_text('developer_instructions="A"\n')
        self.instruction_extra = 'A'
        lookups = []
        replaced = False

        def replace(arguments):
            nonlocal replaced
            if 'debug' not in arguments:
                return
            override = next((json.loads(argument.partition('=')[2])
                             for argument in arguments
                             if argument.startswith('developer_instructions=')), None)
            if override is not None and override.startswith('loop-manager-guidance-') and not replaced:
                replacement = config.with_suffix('.replacement')
                replacement.write_text('developer_instructions="B"\n')
                replacement.replace(config)
                self.instruction_extra = 'B'
                replaced = True
            lookups.append((override, self.instruction_extra))

        self.native_probe_hook = replace
        return lookups

    def test_current_and_actual_request_share_environment_instruction_and_policy(self):
        current = self.observe()
        self.assertIsNotNone(current)
        result = self.invoke()
        self.assertEqual(result.code, 0)
        actual = self.observe(invocation_id=result.isolation_invocation_id)
        self.assertEqual(actual, current)
        self.assertEqual(self.observe(), actual)
        # Native process is a double; the request, ledger and comparison are real.
        row = self.connection.execute('SELECT execution_identity FROM agent_invocations').fetchone()
        self.assertNotIn('private-auth', row[0])
        self.assertNotIn('review this candidate', row[0])

    def test_unused_personal_config_does_not_change_selected_identity(self):
        original = self.observe()
        (self.parent_home / 'config.toml').write_text('hooks=["personal"]\n')
        self.assertEqual(self.observe(), original)
        (self.child_home / 'config.toml').write_text('model="changed"\n')
        self.assertNotEqual(self.observe(), original)

    def test_native_instruction_lookup_config_generation_is_bound_to_actual_call(self):
        config = self.child_home / 'config.toml'

        def create_config(arguments):
            if 'debug' in arguments and not config.exists():
                replacement = config.with_suffix('.replacement')
                replacement.write_text('model="generated during instruction lookup"\n')
                replacement.replace(config)

        self.native_probe_hook = create_config
        result = self.invoke()
        self.assertEqual(result.code, 0)
        proof = self.connection.execute(
            'SELECT status, configuration_digest FROM agent_isolation_evidence').fetchone()
        self.assertEqual(proof['status'], 'verified')
        self.assertEqual(proof['configuration_digest'], isolation._configuration_digest(
            'codex', self.worktree, self.config,
            {'CODEX_HOME': str(self.child_home)}))
        self.assertEqual(self.observe(invocation_id=result.isolation_invocation_id), self.observe())

    def test_config_change_after_instruction_resolution_rejects_execution_and_retries(self):
        first = self.invoke()
        old_identity = self.observe(invocation_id=first.isolation_invocation_id)
        old_call = self.connection.execute(
            'SELECT session_id, execution_identity FROM agent_invocations WHERE invocation_id=?',
            (first.isolation_invocation_id,)).fetchone()
        old_proof = self.connection.execute(
            'SELECT status, configuration_digest FROM agent_isolation_evidence WHERE invocation_id=?',
            (first.isolation_invocation_id,)).fetchone()
        old_result = provider_adapters._provider_log_directory(
            self.worktree, self.state) / 'reviewer-last.txt'
        old_result.write_text('preserve previous result')

        config = self.child_home / 'config.toml'
        config.write_text('developer_instructions="A"\n')
        self.instruction_extra = 'A'
        refresh = isolation.refresh_configuration_evidence

        def replace_before_refresh(*args, **kwargs):
            replacement = config.with_suffix('.replacement')
            replacement.write_text('developer_instructions="B"\n')
            replacement.replace(config)
            self.instruction_extra = 'B'
            return refresh(*args, **kwargs)

        with mock.patch.object(isolation, 'refresh_configuration_evidence',
                               side_effect=replace_before_refresh):
            with self.assertRaisesRegex(isolation.IsolationError, '지침 해석 뒤 변경'):
                self.invoke()

        self.popen.assert_not_called()
        self.assertEqual(old_result.read_text(), 'preserve previous result')
        old_call_after = self.connection.execute(
            'SELECT session_id, execution_identity FROM agent_invocations WHERE invocation_id=?',
            (first.isolation_invocation_id,)).fetchone()
        self.assertEqual(tuple(old_call_after), tuple(old_call))
        old_proof_after = self.connection.execute(
            'SELECT status, configuration_digest FROM agent_isolation_evidence WHERE invocation_id=?',
            (first.isolation_invocation_id,)).fetchone()
        self.assertEqual(tuple(old_proof_after), tuple(old_proof))
        self.assertEqual(self.observe(invocation_id=first.isolation_invocation_id), old_identity)
        rejected = self.connection.execute('''SELECT proof.status, call.execution_identity,
            call.execution_started_at FROM agent_isolation_evidence proof
            JOIN agent_invocations call USING(invocation_id)
            ORDER BY call.rowid DESC LIMIT 1''').fetchone()
        self.assertEqual(rejected['status'], 'rejected')
        self.assertIsNone(rejected['execution_identity'])
        self.assertIsNone(rejected['execution_started_at'])

        retried = self.invoke()
        self.assertEqual(retried.code, 0)
        arguments = self.popen.call_args.args[0]
        override = next(argument for argument in arguments
                        if argument.startswith('developer_instructions='))
        self.assertEqual(json.loads(override.partition('=')[2]), 'B\n\n' + self.guidance.content)
        actual = self.observe(invocation_id=retried.isolation_invocation_id)
        self.assertEqual(actual, self.observe())

    def test_current_observation_rejects_config_change_after_instruction_resolution(self):
        config = self.child_home / 'config.toml'
        config.write_text('developer_instructions="A"\n')
        self.instruction_extra = 'A'
        refresh = isolation.refresh_configuration_evidence

        def replace_before_refresh(*args, **kwargs):
            replacement = config.with_suffix('.replacement')
            replacement.write_text('developer_instructions="B"\n')
            replacement.replace(config)
            self.instruction_extra = 'B'
            return refresh(*args, **kwargs)

        with mock.patch.object(isolation, 'refresh_configuration_evidence',
                               side_effect=replace_before_refresh):
            self.assertIsNone(self.observe())

        self.assertEqual(config.read_text(), 'developer_instructions="B"\n')
        self.assertIsNotNone(self.observe())

    def test_instruction_change_during_native_lookup_restarts_observation_baseline(self):
        lookups = self.replace_instruction_during_sentinel_lookup()

        observed = self.observe()

        self.assertIsNotNone(observed)
        self.assertEqual([instructions for _, instructions in lookups], ['A', 'B', 'B', 'B', 'B'])
        self.assertIsNone(lookups[0][0])
        self.assertTrue(lookups[1][0].startswith('loop-manager-guidance-'))
        self.assertIsNone(lookups[2][0])
        self.assertTrue(lookups[3][0].startswith('loop-manager-guidance-'))
        self.assertEqual(lookups[4][0], 'B\n\n' + self.guidance.content)
        self.assertEqual(self.observe(), observed)

    def test_instruction_change_during_native_lookup_restarts_execution_baseline(self):
        lookups = self.replace_instruction_during_sentinel_lookup()

        result = self.invoke()

        self.assertEqual(result.code, 0)
        self.assertEqual([instructions for _, instructions in lookups], ['A', 'B', 'B', 'B', 'B'])
        self.assertIsNone(lookups[0][0])
        self.assertTrue(lookups[1][0].startswith('loop-manager-guidance-'))
        self.assertIsNone(lookups[2][0])
        self.assertTrue(lookups[3][0].startswith('loop-manager-guidance-'))
        self.assertEqual(lookups[4][0], 'B\n\n' + self.guidance.content)
        arguments = self.popen.call_args.args[0]
        override = next(argument for argument in arguments
                        if argument.startswith('developer_instructions='))
        self.assertEqual(json.loads(override.partition('=')[2]), 'B\n\n' + self.guidance.content)
        self.assertEqual(self.observe(invocation_id=result.isolation_invocation_id), self.observe())

    def test_current_observation_accepts_safe_config_created_during_instruction_lookup(self):
        config = self.child_home / 'config.toml'

        def create_config(arguments):
            if 'debug' in arguments and not config.exists():
                config.write_text('model="created while observing"\n')

        self.native_probe_hook = create_config
        observed = self.observe()
        self.assertIsNotNone(observed)
        self.assertTrue(config.is_file())
        self.assertEqual(self.observe(), observed)

    def test_instruction_config_conflict_is_rejected_before_process_and_preserves_result(self):
        config = self.child_home / 'config.toml'
        log_dir = provider_adapters._provider_log_directory(self.worktree, self.state)
        previous = log_dir / 'reviewer-last.txt'
        previous.write_text('previous result')
        native_lookups = []

        def create_conflict(arguments):
            native_lookups.append(tuple(arguments))
            if 'debug' in arguments and not config.exists():
                config.write_text('hooks=["unsafe"]\n')

        self.native_probe_hook = create_conflict
        with self.assertRaisesRegex(isolation.IsolationError, 'hook'):
            self.invoke()
        self.popen.assert_not_called()
        self.assertEqual(previous.read_text(), 'previous result')
        proof = self.connection.execute(
            'SELECT status, reason FROM agent_isolation_evidence').fetchone()
        self.assertEqual(proof['status'], 'rejected')
        self.assertIn('hook', proof['reason'])
        self.assertEqual(sum('debug' in arguments for arguments in native_lookups), 1)

    def test_manager_config_change_during_native_lookup_is_rejected_before_next_lookup(self):
        native_lookups = []

        def change_fast_mode(arguments):
            native_lookups.append(tuple(arguments))
            if 'debug' in arguments and self.config.read_text() == '[codex]\nfast_mode=false\n':
                self.config.write_text('[codex]\nfast_mode=true\n')

        self.native_probe_hook = change_fast_mode
        with self.assertRaisesRegex(isolation.IsolationError, '설정이 준비 중 변경됐습니다'):
            self.invoke()

        self.popen.assert_not_called()
        self.assertEqual(sum('debug' in arguments for arguments in native_lookups), 1)
        proof = self.connection.execute('''SELECT proof.status, call.execution_identity,
            call.execution_started_at FROM agent_isolation_evidence proof
            JOIN agent_invocations call USING (invocation_id)''').fetchone()
        self.assertEqual(proof['status'], 'rejected')
        self.assertIsNone(proof['execution_identity'])
        self.assertIsNone(proof['execution_started_at'])

    def test_config_changed_after_final_check_is_rejected_at_process_handoff(self):
        config = self.child_home / 'config.toml'
        previous = provider_adapters._provider_log_directory(
            self.worktree, self.state) / 'reviewer-last.txt'
        previous.write_text('previous result')

        def race_after_check():
            replacement = config.with_suffix('.replacement')
            replacement.write_text('model="changed after final check"\n')
            replacement.replace(config)

        self.before_handoff = race_after_check
        with self.assertRaisesRegex(isolation.IsolationError, '변경됐습니다'):
            self.invoke()
        self.popen.assert_not_called()
        self.assertEqual(previous.read_text(), 'previous result')
        proof = self.connection.execute('''SELECT proof.status, call.execution_identity,
            call.execution_started_at FROM agent_isolation_evidence proof
            JOIN agent_invocations call USING(invocation_id)''').fetchone()
        self.assertEqual(proof['status'], 'rejected')
        self.assertIsNone(proof['execution_identity'])
        self.assertIsNone(proof['execution_started_at'])

    def test_effective_native_instruction_change_does_not_rewrite_old_invocation(self):
        result = self.invoke()
        actual = self.observe(invocation_id=result.isolation_invocation_id)
        self.instruction_extra = 'changed native project instructions'
        self.assertNotEqual(self.observe()['instruction'], actual['instruction'])
        self.assertEqual(self.observe(invocation_id=result.isolation_invocation_id), actual)

    def test_failed_interrupted_missing_and_cross_owned_invocations_are_not_proof(self):
        result = self.invoke()
        identity = result.isolation_invocation_id
        self.assertIsNotNone(self.observe(invocation_id=identity))
        for assignment in ("result_kind='interrupted'", "result_code=1", "execution_status='not_executed'",
                           "execution_finished_at=NULL", "collection_status='error'", "role='worker'", "repo='other'"):
            with self.subTest(assignment=assignment):
                self.connection.execute('SAVEPOINT invalid')
                self.connection.execute('UPDATE agent_invocations SET '+assignment+' WHERE invocation_id=?', (identity,))
                self.assertIsNone(self.observe(invocation_id=identity))
                self.connection.execute('ROLLBACK TO invalid')
                self.connection.execute('RELEASE invalid')
        self.assertIsNone(self.observe(invocation_id='missing'))

    def test_environment_scope_is_nested_and_restores_caller_mapping(self):
        parent = agent_profiles.agent_environment('codex')
        selected = {'CODEX_HOME': str(self.child_home), 'TELEGRAM_TOKEN': 'manager-private'}
        with agent_profiles.prepared_environment('codex', selected):
            self.assertEqual(agent_profiles.agent_environment('codex'), {'CODEX_HOME': str(self.child_home)})
            self.assertNotIn('CODEX_HOME', agent_profiles.agent_environment('claude'))
            with agent_profiles.prepared_environment('codex', {'CODEX_HOME': 'nested'}):
                self.assertEqual(agent_profiles.agent_environment()['CODEX_HOME'], 'nested')
            self.assertEqual(agent_profiles.agent_environment()['CODEX_HOME'], str(self.child_home))
        self.assertEqual(selected['TELEGRAM_TOKEN'], 'manager-private')
        self.assertEqual(agent_profiles.agent_environment('codex'), parent)

    def test_changed_pre_resolved_profile_refuses_launch(self):
        with self.assertRaisesRegex(isolation.IsolationError, 'profile'):
            self.invoke(resolved=ResolvedProfile('old-model', 'high'))
        self.assertIsNone(self.connection.execute('SELECT execution_started_at FROM agent_invocations').fetchone()[0])

    def test_current_probe_failure_and_missing_validator_cannot_supply_identity(self):
        with mock.patch.object(isolation.CodexLaunch, 'probe', side_effect=RuntimeError('sandbox unavailable')):
            self.assertIsNone(self.observe())
        with mock.patch.object(self, 'validate', side_effect=RuntimeError('authentication unavailable')):
            self.assertIsNone(self.observe())

    def test_preparation_failure_precedes_profile_lookup_and_provider_process(self):
        with mock.patch.object(isolation, 'inspect_worktree', side_effect=isolation.IsolationError('ownership unproven')):
            result = self.invoke()
        self.assertEqual(result.code, 1)
        self.assertEqual(self.lookups, [])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM agent_invocations').fetchone()[0], 0)

    def test_failed_native_result_and_legacy_missing_identity_cannot_be_reused(self):
        result = self.invoke(result_code=1)
        self.assertIsNone(self.observe(invocation_id=result.isolation_invocation_id))
        successful = self.invoke()
        self.connection.execute('UPDATE agent_invocations SET execution_identity=NULL WHERE invocation_id=?',
                                (successful.isolation_invocation_id,))
        self.assertIsNone(self.observe(invocation_id=successful.isolation_invocation_id))

    def test_execution_identity_is_immutable_and_rejects_raw_input_values(self):
        result = self.invoke()
        value = self.observe(invocation_id=result.isolation_invocation_id)
        ledger = agent_invocation_state.InvocationRepository(self.connection)
        with self.assertRaises(ValueError):
            ledger.record_execution_identity(result.isolation_invocation_id, value)
        with self.assertRaisesRegex(ValueError, '지문'):
            ledger.record_execution_identity(result.isolation_invocation_id, {**value, 'environment': 'private-auth'})
        self.assertEqual(self.observe(invocation_id=result.isolation_invocation_id), value)


if __name__ == '__main__':
    unittest.main()
