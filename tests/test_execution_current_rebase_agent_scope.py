"""Current Rebase Agent Scope regression tests."""

from tests.support.current_rebase import current_rebase_case
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import loop_manager as lm




class CurrentRebaseAgentScopeTests(TestCase):
    def setUp(self):
        self.case = current_rebase_case(self)
        self.case.handler._complete_rebase_intent(self.case.fixture.context())
        branch, worktree = 'loop-manager/G-047', '/state/G-047'
        self.original = lm.GitSnapshot('before', branch, {}, {},
            {f'refs/heads/{branch}': ('before', ''), 'refs/heads/other': ('other', '')},
            {worktree: ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
        self.current = replace(self.original, head='owned-head',
            refs={**self.original.refs, f'refs/heads/{branch}': ('owned-head', '')},
            worktrees={worktree: ('HEAD=owned-head', f'branch=refs/heads/{branch}')})
        self.case.state.update_project('repo', agent_role='worker',
            agent_snapshot=self.original.serialize())
        for patch in (
            mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **_k: self.case.factory(None)),
            mock.patch.object(lm, 'capture_git_snapshot', side_effect=lambda *_a: self.current),
            mock.patch.object(lm, '_config_entries', return_value={}),
            mock.patch.object(lm, '_proven_merged_cleanup_paths', return_value=frozenset()),
            mock.patch.object(Path, 'is_dir', return_value=True),
            mock.patch.object(lm, 'git', return_value=SimpleNamespace(stdout='', returncode=0)),
            mock.patch.object(lm, 'git_bytes', return_value=b''),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_resumed_scope_keeps_original_and_records_observed_comparison_start(self):
        from loop_manager_internal.git.managed_rebase_adoption import agent_comparison_baseline
        before = lm.begin_agent_scope(self.case.fixture.connection, 'repo', Path('/state/G-047'), 'worker')
        self.assertEqual(before, self.current)
        saved = self.case.checkpoint.get('repo', 'G-047')
        self.assertEqual(agent_comparison_baseline(self.original, saved.snapshot, 'worker'), self.current)
        self.assertEqual(self.case.fixture.connection.execute(
            "SELECT agent_snapshot FROM projects WHERE repo = 'repo'").fetchone()[0], self.original.serialize())
        later = replace(self.original, head='later-start')
        self.assertEqual(agent_comparison_baseline(later, saved.snapshot, 'worker'), later)
        self.assertEqual(agent_comparison_baseline(self.original, saved.snapshot, 'reviewer'), self.original)
        lm.begin_agent_scope(self.case.fixture.connection, 'repo', Path('/state/G-047'), 'worker')
        updated = self.case.checkpoint.get('repo', 'G-047')
        self.assertEqual(updated.snapshot['current_rebase_agent_baseline_history'],
                         [saved.snapshot['current_rebase_agent_baseline']])

    def test_later_head_change_still_requires_independent_manager_git_proof(self):
        connection = self.case.fixture.connection
        lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker')
        accepted_start = self.current
        from loop_manager_internal.git.managed_rebase_adoption import needs_agent_comparison
        self.assertFalse(needs_agent_comparison(
            replace(self.case.checkpoint.get('repo', 'G-047'), operation='commit'),
            self.original, accepted_start))
        self.current = replace(self.current, head='later-head',
            refs={**self.current.refs, 'refs/heads/loop-manager/G-047': ('later-head', '')},
            worktrees={'/state/G-047': ('HEAD=later-head', 'branch=refs/heads/loop-manager/G-047')})
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo='repo'",
                           (accepted_start.serialize(),))
        with self.assertRaises(lm.AgentScopeError):
            lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo')
        self.assertEqual(connection.execute("SELECT agent_snapshot FROM projects WHERE repo='repo'").fetchone()[0],
                         self.original.serialize())

    def test_fresh_scope_preserves_restored_stage_without_readopting_rebase(self):
        connection = self.case.fixture.connection
        self.current = replace(self.current,
            index={'app.py': ('H 100644 original-staged-blob 0',)},
            dirty={'app.py': (('staged',), 'original-file')})
        self.original = self.current
        self.case.state.update_project('repo', agent_snapshot=self.original.serialize(),
                                       agent_result_snapshot=self.current.serialize())
        with mock.patch.object(lm, '_current_rebase_service', side_effect=AssertionError('fresh scope is not rebase completion')):
            self.assertEqual(lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker'), self.original)
            self.assertEqual(lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo'), self.current)
        self.assertEqual(connection.execute("SELECT agent_snapshot FROM projects WHERE repo='repo'").fetchone()[0],
                         self.original.serialize())

    def test_persisted_actual_agent_result_still_rejects_shared_ref_violation(self):
        connection = self.case.fixture.connection
        lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker')
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo = 'repo'",
                           (self.current.serialize(),))
        self.assertEqual(lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo'), self.current)
        violation = replace(self.current, refs={**self.current.refs, 'refs/heads/other': ('foreign', '')})
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo = 'repo'",
                           (violation.serialize(),))
        with self.assertRaises(lm.AgentScopeError):
            lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo')
        self.assertEqual(connection.execute("SELECT agent_snapshot FROM projects WHERE repo = 'repo'").fetchone()[0], self.original.serialize())

    def test_persisted_rebase_keeps_only_revision_eligible_requirement_archive_paths(self):
        connection = self.case.fixture.connection
        lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker')
        source = 'docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md'
        archive = lm.requirement_archive_path(source)
        self.current = replace(self.current, dirty={source: (('staged',), 'deleted'),
                                                    archive: (('staged',), 'unchanged-document')})
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo = 'repo'",
                           (self.current.serialize(),))
        with mock.patch.object(lm, 'requirement_archive_candidates', return_value=(source,)):
            self.assertEqual(lm.validate_persisted_agent_scope(
                connection, Path('/repo'), Path('/state'), 'repo'), self.current)
        with mock.patch.object(lm, 'requirement_archive_candidates', return_value=()):
            with self.assertRaises(lm.AgentScopeError):
                lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo')
