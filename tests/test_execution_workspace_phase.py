"""Workspace Phase execution policy regression tests."""

from tests.support.execution import ExecutionFixture
from dataclasses import replace
from unittest import TestCase

import loop_manager as lm
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios


class ExecutionWorkspacePhaseTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_worktree_recovery_failure_keeps_durable_pending_boundary(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = RuntimeError('temporary worktree failure')
        self.state.update_project('repo', worker_session_id='worker-session')

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        saved = checkpoint.get('repo', 'G-047').snapshot
        self.assertTrue(saved['worktree_recovery_pending'])
        self.assertEqual(saved['branch'], 'loop-manager/G-047')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_workspace_runtime_failure_retries_without_blocking_or_losing_context(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = RuntimeError('temporary git failure')
        self.state.update_project('repo', worker_session_id='worker-session',
                                  worker_result='saved result')

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'worktree_recovery_failed')
        row = self.state.row()
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['worker_result'], 'saved result')
        self.assertEqual(row['worktree'], '/state/G-047')
        self.effects.blocking.block_active_execplan_from_revision.assert_not_called()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM blocked_checkpoints WHERE repo = 'repo'"
        ).fetchone()[0], 0)


    def test_workspace_violation_blocks_before_agent(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.validate_execplan_workspace_identity.side_effect = lm.WorkspaceOwnershipError('ownership')
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan_from_revision.assert_called_once()
        self.assertEqual(self.calls, [])
