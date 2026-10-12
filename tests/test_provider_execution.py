"""Prepared provider inputs and durable physical-call identity boundaries."""
import unittest
from unittest import mock
from loop_manager_internal.agents import agent_profiles, agent_write_isolation as isolation
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.storage import agent_invocation_state
from tests.support.provider_execution import ProviderExecutionFixture


class ProviderExecutionTests(ProviderExecutionFixture, unittest.TestCase):
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
