"""Current Rebase Agent Scope regression tests."""

from tests.support.current_rebase import CurrentRebaseAgentScopeFixture
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, mock

import loop_manager as lm




class CurrentRebaseAgentScopeTests(CurrentRebaseAgentScopeFixture, TestCase):
    def test_obsolete_comparison_records_are_preserved_without_becoming_a_resume_gate(self):
        repository = self.case.checkpoint
        current = repository.get('repo', 'G-047')
        legacy = {'kind': 'current_rebase_comparison_baseline', 'snapshot': 'incomplete legacy audit'}
        repository.record('repo', 'G-047', expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot={**current.snapshot, 'current_rebase_agent_baseline': legacy})
        self.assertEqual(lm.begin_agent_scope(
            self.case.fixture.connection, 'repo', Path('/state/G-047'), 'worker'), self.current)
        self.assertEqual(repository.get('repo', 'G-047').snapshot['current_rebase_agent_baseline'], legacy)

    def test_resumed_scope_returns_current_candidate_and_preserves_original_audit(self):
        connection = self.case.fixture.connection
        for _ in range(2):
            self.assertEqual(lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker'), self.current)
            self.assertEqual(connection.execute(
                "SELECT agent_snapshot FROM projects WHERE repo = 'repo'").fetchone()[0],
                self.original.serialize())
        self.assertNotIn('manager_metadata_agent_baseline', self.case.checkpoint.get('repo', 'G-047').snapshot or {})

    def test_later_head_change_still_requires_independent_manager_git_proof(self):
        connection = self.case.fixture.connection
        lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker')
        accepted_start = self.current
        self.current = replace(self.current, head='later-head',
            refs={**self.current.refs, 'refs/heads/loop-manager/G-047': ('later-head', '')},
            worktrees={str(Path('/state/G-047').resolve()): ('HEAD=later-head', 'branch=refs/heads/loop-manager/G-047')})
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo='repo'",
                           (accepted_start.serialize(),))
        with self.subTest():
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

    def test_saved_result_shared_ref_change_returns_current_snapshot_and_preserves_audit(self):
        connection = self.case.fixture.connection
        lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker')
        saved_result = replace(self.current,
            refs={**self.current.refs, 'refs/heads/other': ('foreign', '')})
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo = 'repo'",
                           (saved_result.serialize(),))

        self.assertEqual(
            lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo'),
            self.current,
        )
        row = connection.execute(
            "SELECT agent_snapshot, agent_result_snapshot FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(row['agent_snapshot'], self.original.serialize())
        self.assertEqual(row['agent_result_snapshot'], saved_result.serialize())

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
            with self.subTest():
                lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo')
