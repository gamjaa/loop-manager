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


    def _prepare_pending_guidance(self, *, implementation_changes):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        worktree = Path(directory.name)
        plan_path = self.handler.catalog_plan['path']
        document_path = worktree / plan_path
        document_path.parent.mkdir(parents=True)
        document_path.write_text(
            guidance_plan().replace('G-020', 'G-047'), encoding='utf-8',
        )
        (worktree / 'AGENTS.md').write_text('# Agents\n', encoding='utf-8')
        before = lm.GitSnapshot(
            head='owned-head', branch='loop-manager/G-047', index={}, dirty={},
            refs={}, worktrees={}, config={}, operations={},
        )
        dirty = {plan_path: (('unstaged',), 'proposal-content')}
        if implementation_changes:
            dirty.update({
                'app.py': (('unstaged',), 'implementation-content'),
                'tests/test_app.py': (('untracked',), 'test-content'),
            })
        after = replace(before, dirty=dirty)
        self.connection.execute(lm.GUIDANCE_APPROVALS_SCHEMA)
        self.state.update_project(
            'repo', worktree=str(worktree), agent_role='worker',
            agent_snapshot=before.serialize(), worker_session_id='saved-session',
        )
        self.effects.agent.inspect_worker_guidance_result.side_effect = self.state.bind(
            lm.inspect_worker_guidance_result, connected=True, mutates=True,
        )
        # Scope clearing must really mutate state so an ordering regression is
        # visible after the real guidance transaction persists the block.
        self.effects.agent.clear_agent_scope.side_effect = self.state.bind(
            lm.clear_agent_scope, connected=True, mutates=True,
        )
        self.effects.agent.capture_git_snapshot.return_value = after
        self.handler.catalog_plan = self.repository.active_row('repo')
        return worktree, before, after


    def _assert_pending_guidance(self, result, worktree, before, after, session):
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        projected = self.repository.projected('repo')
        self.assertEqual(projected.status, 'PLAN_BLOCKED')
        self.assertEqual(projected.agent_role, 'worker')
        self.assertEqual(projected.agent_snapshot, before.serialize())
        self.assertEqual(projected.agent_result_snapshot, after.serialize())
        blocked = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'",
        ).fetchone()
        self.assertEqual(blocked['status'], 'PLAN_BLOCKED')
        self.assertEqual(blocked['blocked_reason'], lm.GUIDANCE_BLOCK_REASON)
        self.assertEqual(blocked['agent_role'], 'worker')
        self.assertEqual(blocked['agent_snapshot'], before.serialize())
        self.assertEqual(blocked['agent_result_snapshot'], after.serialize())
        self.assertEqual(blocked['worker_session_id'], session)
        self.assertEqual(blocked['branch'], before.branch)
        self.assertEqual(blocked['worktree'], str(worktree))
        approval = self.connection.execute(
            "SELECT * FROM guidance_approvals WHERE repo = 'repo' AND plan_id = 'G-047'",
        ).fetchone()
        self.assertEqual(approval['proposal_id'], 'worker-rule')
        self.assertFalse(approval['applied'])
        self.assertFalse(approval['decision_hash'])
        self.assertEqual((worktree / 'AGENTS.md').read_text(), '# Agents\n')
        self.assertIn('Decision: `pending`', (worktree / blocked['path']).read_text())
        self.effects.agent.clear_agent_scope.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()


    def _run_real_worker_guidance_checkpoint(self, *, implementation_changes):
        fixture = blocked_git_fixture(self)
        repo = str(fixture.repo.resolve())
        plan_path = 'docs/plans/[G-034] Checkpoint.md'
        (fixture.worktree / 'AGENTS.md').write_text('# Agents\n')
        fixture.run_git(fixture.worktree, 'add', 'AGENTS.md')
        fixture.run_git(fixture.worktree, 'commit', '-m', 'guidance baseline')
        plan_row = fixture.connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()
        lm.hydrate_execplan_project(fixture.connection, repo, plan_row)
        state = ExecutionStatePort(
            fixture.connection, runtime_state.RuntimeStateRepository(fixture.connection), repo,
        )
        effects = replace(self.effects, state=state)
        effects.agent.begin_agent_scope.side_effect = state.bind(
            lm.begin_agent_scope, connected=True, mutates=True,
        )
        effects.agent.inspect_worker_guidance_result.side_effect = state.bind(
            lm.inspect_worker_guidance_result, connected=True, mutates=True,
        )
        effects.agent.clear_agent_scope.side_effect = state.bind(
            lm.clear_agent_scope, connected=True, mutates=True,
        )
        effects.agent.capture_git_snapshot.side_effect = lm.capture_git_snapshot
        effects.agent._guidance_worker_targets.return_value = ()
        effects.agent._published_plan_allows_common_plans.return_value = False
        effects.agent.worker_prompt.return_value = 'worker guidance prompt'
        plan = {
            'id': 'G-034', 'title': 'Checkpoint',
            'sources': ['docs/PRODUCT.md'], 'depends_on': [],
            'complexity': 'high', 'risk': 'critical', 'priority': 'critical',
        }

        def validate_scope(_repo_path, worktree, _branch, _base, selected,
                           _catalog, before, **_kwargs):
            lm.validate_agent_scope(
                'worker', worktree, before, lm.capture_git_snapshot(worktree),
                selected_plan_path=selected,
            )
            return plan

        effects.agent.validate_worker_workspace_scope.side_effect = validate_scope

        def worker(_provider, worktree, *_args, **kwargs):
            (worktree / plan_path).write_text(
                guidance_plan().replace('G-020', 'G-034')
                .replace('title: Guidance', 'title: Checkpoint'),
            )
            if implementation_changes:
                (worktree / 'code.py').write_text('safe implementation\n')
                test_path = worktree / 'tests/test_app.py'
                test_path.parent.mkdir(parents=True, exist_ok=True)
                test_path.write_text('def test_safe():\n    assert True\n')
            kwargs['on_session_started']('real-worker-session')
            return lm.AgentResult(0, READY_WORKER_RESULT,
                                  session_id='real-worker-session')

        effects.agent.run_agent.side_effect = worker
        handler = handlers.ExecutionHandlers(
            effects, repo=repo, repo_path=fixture.repo,
            state_dir=fixture.state_dir, base='main', base_sha=fixture.base_oid,
            provider='codex', config_path=Path('/config'), newly_activated=False,
        )
        handler.plan = plan
        handler.catalog_plan = plan_row
        with mock.patch.object(lm, 'is_loop_manager_repository', return_value=False):
            outcome = handler.run(handler._context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(fixture.connection.execute(
            "SELECT blocked_reason FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()[0], lm.GUIDANCE_BLOCK_REASON)
        blocked = fixture.connection.execute(
            "SELECT agent_role, agent_snapshot, agent_result_snapshot, "
            "worker_session_id FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()
        self.assertEqual(blocked['agent_role'], 'worker')
        self.assertTrue(blocked['agent_snapshot'])
        self.assertTrue(blocked['agent_result_snapshot'])
        self.assertEqual(blocked['worker_session_id'], 'real-worker-session')
        effects.validation.verify.assert_not_called()
        effects.publication.push_managed_publication_branch.assert_not_called()
        with mock.patch.object(lm, 'pr_for_branch', return_value=None):
            self.assertEqual(lm.dispatch_blocked_checkpoints(
                fixture.connection, fixture.repo, fixture.state_dir, repo,
            ), 1)
        oid = fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD')
        changed = set(fixture.run_git(
            fixture.worktree, 'diff-tree', '--no-commit-id', '--name-only', '-r', oid,
        ).splitlines())
        expected = {plan_path}
        if implementation_changes:
            expected.update(('code.py', 'tests/test_app.py'))
        self.assertEqual(changed, expected)
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(
            fixture.connection,
        ).get(repo, 'G-034')
        self.assertEqual(checkpoint.remote_oid, oid)
        self.assertEqual(lm.validate_persisted_agent_scope(
            fixture.connection, fixture.repo, fixture.state_dir, repo,
        ).head, oid)


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
