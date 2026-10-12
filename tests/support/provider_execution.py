"""Shared provider CLI doubles with real request, proof and ledger boundaries."""
import io
import json
import sqlite3
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.agents import agent_profiles, agent_write_isolation as isolation
from loop_manager_internal.agents import common_guidance, invocation_service, provider_adapters, provider_execution, provider_guidance
from loop_manager_internal.agents.agent_invocation import AgentResult, InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.storage import agent_invocation_state
from tests.support.fixed_samples import PROJECT_FILES
from tests.support.workspace import pin_common_guidance


class ProviderExecutionFixture:
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

