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

    def test_current_external_ref_is_rejected_but_default_checkout_change_is_ignored(self):
        original = self.current
        self.current = replace(original, refs={**original.refs, 'refs/heads/other': ('foreign', '')})
        with self.assertRaises(lm.AgentScopeError):
            self.validate()
        self.current = replace(original,
            worktrees={**original.worktrees, '/repo': ('HEAD=foreign',)})
        self.validate()

    def test_historical_wait_resumes_after_owned_rebase_without_file_approval(self):
        connection = self.case.fixture.connection
        lm.runtime_state.RuntimeStateRepository(connection).block_plan(
            'repo', 'G-047', reason_code=lm.GUIDANCE_BLOCK_REASON,
            evidence='historical wait', release_fingerprint='old-document',
        )
        with (mock.patch.object(lm, 'validate_execplan_workspace_identity'),
              mock.patch.object(lm, 'make_worktree'),
              mock.patch.object(lm, '_rebase_in_progress_for_branch', return_value=False),
              mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False),
              mock.patch.object(lm, 'requirement_archive_candidates', return_value=()),
              mock.patch.object(lm, '_proven_manager_base_refs', return_value=frozenset())):
            self.assertTrue(lm.resume_retired_guidance_block(
                connection, Path('/repo'), 'repo', 'main', Path('/state'), plan_id='G-047'))
        self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'IMPLEMENTING')

    def test_historical_wait_cannot_bypass_a_foreign_ref_scope_violation(self):
        connection = self.case.fixture.connection
        lm.runtime_state.RuntimeStateRepository(connection).block_plan(
            'repo', 'G-047', reason_code=lm.GUIDANCE_BLOCK_REASON,
            evidence='historical wait', release_fingerprint='old-document',
        )
        self.current = replace(self.original, refs={**self.original.refs, 'refs/heads/B': ('foreign', '')})
        with (mock.patch.object(lm, 'validate_execplan_workspace_identity'),
              mock.patch.object(lm, 'make_worktree'),
              mock.patch.object(lm, '_rebase_in_progress_for_branch', return_value=False),
              mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False),
              mock.patch.object(lm, 'requirement_archive_candidates', return_value=()),
              mock.patch.object(lm, '_proven_manager_base_refs', return_value=frozenset())):
            self.assertFalse(lm.resume_retired_guidance_block(
                connection, Path('/repo'), 'repo', 'main', Path('/state'), plan_id='G-047'))
        row = connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'PLAN_BLOCKED')
        self.assertEqual(row['blocked_reason'], lm.GUIDANCE_BLOCK_REASON)

    def _published_tracking_ref(self):
        remote_ref = 'refs/remotes/origin/loop-manager/G-047'
        self.original = replace(self.original, refs={**self.original.refs, remote_ref: ('remote', '')})
        self.current = replace(self.current, refs={**self.current.refs, remote_ref: ('owned-head', '')})
        self.case.state.update_project('repo', agent_snapshot=self.original.serialize(), publication_remote_oid='owned-head')
        lm.blocked_checkpoint_state.BlockedCheckpointRepository(self.case.fixture.connection).request(
            repo='repo', plan_id='G-047', reason_code='verify_retry_exhausted', evidence='original cause',
            branch='loop-manager/G-047', worktree=str(Path('/state/G-047').resolve()), expected_remote_oid='remote', rebase_recovery=False)


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
