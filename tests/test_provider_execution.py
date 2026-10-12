"""Prepared provider inputs and durable physical-call identity boundaries."""
import io
import json
import sqlite3
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.agents import agent_profiles, agent_write_isolation as isolation
from loop_manager_internal.agents import common_guidance, invocation_service, provider_adapters, provider_execution, provider_guidance
from loop_manager_internal.agents.agent_invocation import AgentResult, InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.storage import agent_invocation_state
from tests.support.fixed_samples import PROJECT_FILES
from tests.support.workspace import pin_common_guidance


class ProviderExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.state = self.root / 'state'
        self.worktree = self.state / 'worktrees' / 'review'
        self.worktree.mkdir(parents=True)
        self.source.mkdir()
        self.config = self.root / 'config.ini'
        self.config.write_text('[codex]\nfast_mode=false\n')
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        agent_invocation_state.ensure_schema(self.connection)
        self.repo = str(self.source)
        self.plan_id = getattr(self, 'plan_id', 'G-262')
        pin_common_guidance(self.connection, InvocationContext(self.repo, self.plan_id),
            self.worktree, self.state, role='reviewer')
        self.profile = ResolvedProfile('model', 'high')
        self.boundary = isolation.WorktreeBoundary(self.worktree, self.source, (),
            self.state / 'git', self.source / '.git', 'registered-repository')
        self.child_home = self.state / 'agent-runtime' / 'reviewer' / 'codex'
        self.child_home.mkdir(parents=True)
        self.parent_home = self.root / 'personal'
        self.parent_home.mkdir()
        self.lookups = []
        self.instruction_extra = 'native project instructions'
        self.calls = 0
        patches = (
            mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES),
            mock.patch.object(isolation, 'inspect_worktree', return_value=self.boundary),
            mock.patch.object(isolation, 'prepare_launch', side_effect=self.prepare_launch),
            mock.patch.object(isolation.CodexLaunch, 'probe'),
            mock.patch.object(isolation.CodexLaunch, 'stop'),
            mock.patch.object(isolation.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'codex-cli 0.159.3')),
            mock.patch.object(provider_guidance, '_probe', side_effect=self.native_probe),
            mock.patch.dict(agent_profiles.os.environ, {'CODEX_HOME': str(self.parent_home), 'OPENAI_API_KEY': 'private-auth'}, clear=False),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def prepare_launch(self, boundary, state_dir, role, read_only, arguments, environment):
        self.calls += 1
        runtime = self.child_home.parent
        attempt = runtime / f'call-{self.calls}'
        attempt.mkdir()
        env = dict(environment, CODEX_HOME=str(self.child_home), TMPDIR=str(attempt))
        return isolation.CodexLaunch(arguments, ('codex', 'sandbox'), env, runtime, attempt,
            json.dumps({'worktree': str(self.worktree), 'writable': [], 'protected': [str(self.source)]}))

    def native_probe(self, arguments, **kwargs):
        self.assertEqual(kwargs['env']['CODEX_HOME'], str(self.child_home))
        if arguments == ['codex', '--version']:
            return 'codex-cli 0.159.3'
        text = self.instruction_extra
        for arg in arguments:
            if arg.startswith('developer_instructions='):
                text = json.loads(arg.partition('=')[2])
        return json.dumps([{'type': 'message', 'role': 'developer',
                            'content': [{'type': 'input_text', 'text': text}]}])

    def resolve(self, *_args):
        self.lookups.append(agent_profiles.agent_environment('codex'))
        self.assertEqual(self.lookups[-1]['CODEX_HOME'], str(self.child_home))
        return self.profile

    def validate(self, *_args):
        self.resolve()

    def observe(self, **kwargs):
        return provider_execution.observe(self.connection, repo=self.repo, plan_id=self.plan_id,
            worktree=self.worktree, state_dir=self.state, config_path=self.config, provider='codex',
            resolve_profile=self.resolve, validate_runtime=self.validate,
            snapshot_digest=lambda: 'current-snapshot', **kwargs)

    def invoke(self, *, result_code=0, resolved=None):
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO(json.dumps({'type': 'thread.started', 'thread_id': 'review-session'}) + '\n' +
            json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'VERDICT: PASS'}}) + '\n')
        process.wait.return_value = result_code
        popen = mock.Mock(return_value=process)
        effects = provider_adapters.ProviderEffects(mock.Mock(), mock.Mock(), self.resolve, popen, mock.Mock(), self.validate)
        adapter = provider_adapters.CodexAgentAdapter(lambda *args, **kwargs: provider_adapters.run_codex(*args, effects=effects, **kwargs))
        invoke_effects = invocation_service.InvocationEffects(self.resolve, lambda: None,
            lambda ledger, context, role, provider, profile, session, **kwargs:
                ledger.start(context, role, provider, profile, session, **kwargs),
            lambda _: GitSnapshot('base', 'loop-manager/'+self.plan_id, {}, {}, {}, {}, {}, {}), mock.Mock(), lambda _: adapter)
        result = invocation_service.run_agent('codex', self.worktree, self.state, self.config,
            'reviewer', 'review this candidate', read_only=True, persist_session=True,
            connection=self.connection, invocation=InvocationContext(self.repo, self.plan_id),
            resolved_profile=resolved, effects=invoke_effects)
        if popen.called:
            self.assertEqual(popen.call_args.kwargs['env']['CODEX_HOME'], str(self.child_home))
        return result

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
