"""Shared scenario setup retained from the phase policy tests."""

from dataclasses import replace
from pathlib import Path
import tempfile
from unittest import mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from tests.support.guidance import guidance_plan
from tests.support.blocked_git import blocked_git_fixture
from loop_manager_internal.storage import blocked_checkpoint_state
from tests.support.execution import READY_WORKER_RESULT



class ExecutionPhaseScenarios:
    def _use_runtime_contract(self):
        runtime = lm._ExecutionLifecyclePorts(
            self.connection, self.repository,
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        self.effects = replace(self.effects, contract=runtime.effects.contract)
        self.handler.effects = self.effects


    def _with_feedback(self, feedback):
        self.effects = replace(
            self.effects,
            feedback=handlers.FeedbackEffects(
                feedback, lm.validation_failure_fingerprints,
            ),
        )
        self.handler.effects = self.effects








    def _decision_workspace(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        worktree = Path(directory.name)
        plan_path = worktree / 'docs/plans/[G-047] Runtime.md'
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text('# Runtime\n## Decision Log\n', encoding='utf-8')
        product = worktree / 'docs/PRODUCT.md'
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text('# Product\n## User approval\n', encoding='utf-8')
        architecture = worktree / 'docs/ARCHITECTURE.md'
        architecture.write_text('# Architecture\n## Architecture Change Policy\n',
                                encoding='utf-8')
        self.state.update_project('repo', worktree=str(worktree))
        self.effects.agent.begin_agent_scope.side_effect = lambda *_args, **_kwargs: (
            self.state.update_project(
                'repo', agent_role='worker', agent_snapshot='before-scope',
                agent_input_fingerprint='remote-input',
            ) or 'before-scope'
        )
        self.effects.agent._revision_execplan_input_fingerprint.return_value = 'remote-input'
        self.effects.blocking.block_active_execplan.side_effect = (
            lambda *args, **kwargs: lm.block_active_execplan(
                self.connection, *args, **kwargs,
            )
        )
        return worktree
