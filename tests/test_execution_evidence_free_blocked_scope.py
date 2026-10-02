"""Evidence Free Blocked Scope regression tests."""

from dataclasses import replace
from pathlib import Path
from unittest import TestCase, mock

import loop_manager as lm
from loop_manager_internal.recovery import scoped_blocking
from tests.support.guidance import guidance_plan
from tests.support.current_rebase import CurrentRebaseAgentScopeFixture




class EvidenceFreeBlockedScopeTests(CurrentRebaseAgentScopeFixture, TestCase):

    def validate(self):
        connection = self.case.fixture.connection
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo'").fetchone()
        with mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False), \
             mock.patch.object(lm, 'requirement_archive_candidates', return_value=()), \
             mock.patch.object(lm, '_proven_manager_base_refs', return_value=frozenset()):
            lm.validate_blocked_agent_scope(connection, Path('/repo'), 'repo', 'main',
                Path('/state/G-047'), plan['path'], accepted_rebase=True, saved_plan=plan)

    def test_manual_result_can_replace_selected_rebase_state_without_changing_saved_agent_start(self):
        self.validate()
        row = self.case.fixture.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['agent_snapshot'], self.original.serialize())

    def test_saved_actual_agent_ref_violation_is_checked_before_rebase_normalization(self):
        violation = replace(self.original, refs={**self.original.refs, 'refs/heads/other': ('foreign', '')})
        self.case.state.update_project('repo', agent_result_snapshot=violation.serialize())
        with self.assertRaises(lm.AgentScopeError):
            self.validate()

    def test_current_external_ref_and_default_worktree_mutation_remain_rejected(self):
        original = self.current
        for current in (replace(original, refs={**original.refs, 'refs/heads/other': ('foreign', '')}),
                        replace(original, worktrees={**original.worktrees, '/repo': ('HEAD=foreign',)})):
            with self.subTest(current=current):
                self.current = current
                with self.assertRaises(lm.AgentScopeError):
                    self.validate()

    def test_guidance_decision_resumes_after_accepted_rebase_with_original_agent_snapshot(self):
        connection = self.case.fixture.connection
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo'").fetchone()
        pending = lm.parse_guidance_document(guidance_plan().replace('G-020', 'G-047'))
        approved = lm.parse_guidance_document(
            guidance_plan(decision='approve', revision='user-1').replace('G-020', 'G-047'))
        with (mock.patch.object(lm, 'is_loop_manager_repository', return_value=False),
              mock.patch.object(lm, '_guidance_target_state', return_value=(b'# Agents\n', 0o100644)),
              mock.patch.object(lm, 'capture_git_snapshot', return_value=self.original)):
            lm._block_for_new_guidance(connection, Path('/repo'), 'repo', Path('/state/G-047'),
                                      plan, pending, pending.proposals, 'proposal')
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo'").fetchone()
        lm.hydrate_execplan_project(connection, 'repo', plan)
        self.assertEqual(plan['agent_snapshot'], self.original.serialize())
        with (mock.patch.object(lm, 'validate_execplan_workspace_identity'),
              mock.patch.object(lm, 'make_worktree'),
              mock.patch.object(lm, '_rebase_in_progress_for_branch', return_value=False),
              mock.patch.object(lm, 'is_loop_manager_repository', return_value=False),
              mock.patch.object(lm, '_guidance_document', return_value=pending) as decision_document,
              mock.patch.object(lm, '_guidance_target_state', return_value=(b'# Agents\n', 0o100644)),
              mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False),
              mock.patch.object(lm, 'requirement_archive_candidates', return_value=()),
              mock.patch.object(lm, '_proven_manager_base_refs', return_value=frozenset())):
            self.assertFalse(lm.resume_guidance_approval_if_decided(
                connection, Path('/repo'), 'repo', 'main', Path('/state'), plan_id='G-047'))
            self.assertEqual(connection.execute('SELECT agent_snapshot FROM exec_plans').fetchone()[0],
                             self.original.serialize())
            decision_document.return_value = approved
            self.assertTrue(lm.resume_guidance_approval_if_decided(
                connection, Path('/repo'), 'repo', 'main', Path('/state'), plan_id='G-047'),
                connection.execute("SELECT last_error FROM exec_plans WHERE repo='repo'").fetchone()[0])
        self.assertEqual(connection.execute('SELECT decision FROM guidance_approvals').fetchone()[0], 'approve')
        self.assertIsNone(scoped_blocking.plan_block(connection, 'repo', 'G-047'))

    def _published_tracking_ref(self):
        remote_ref = 'refs/remotes/origin/loop-manager/G-047'
        self.original = replace(self.original, refs={**self.original.refs, remote_ref: ('remote', '')})
        self.current = replace(self.current, refs={**self.current.refs, remote_ref: ('owned-head', '')})
        self.case.state.update_project('repo', agent_snapshot=self.original.serialize(), publication_remote_oid='owned-head')
        lm.blocked_checkpoint_state.BlockedCheckpointRepository(self.case.fixture.connection).request(
            repo='repo', plan_id='G-047', reason_code='verify_retry_exhausted', evidence='original cause',
            branch='loop-manager/G-047', worktree='/state/G-047', expected_remote_oid='remote', rebase_recovery=False)

    def test_owned_checkpoint_tracking_update_does_not_rewrite_original_agent_snapshot(self):
        self._published_tracking_ref()
        with mock.patch.object(lm, '_owned_checkpoint_remote_oid', return_value='owned-head') as ownership:
            self.validate()
        ownership.assert_called_once()
        self.assertEqual(self.case.fixture.connection.execute('SELECT agent_snapshot FROM exec_plans').fetchone()[0],
                         self.original.serialize())

    def test_unproven_checkpoint_tracking_update_remains_a_scope_violation(self):
        self._published_tracking_ref()
        with mock.patch.object(lm, '_owned_checkpoint_remote_oid', return_value=None):
            with self.assertRaises(lm.AgentScopeError):
                self.validate()
