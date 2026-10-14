"""Current Rebase Agent Scope regression tests."""

from tests.support.current_rebase import CurrentRebaseAgentScopeFixture
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, mock

import loop_manager as lm




class CurrentRebaseAgentScopeTests(CurrentRebaseAgentScopeFixture, TestCase):
    def test_damaged_saved_comparison_snapshot_uses_evidence_error(self):
        from loop_manager_internal.git.managed_rebase_adoption import agent_comparison_baseline
        from loop_manager_internal.git.manager_git_journal import MutationEvidenceError
        import hashlib
        saved = {'current_rebase_agent_baseline': {
            'kind': 'current_rebase_comparison_baseline', 'role': 'worker',
            'original': hashlib.sha256(self.original.serialize().encode()).hexdigest(),
            'snapshot': '{"version":4,"head":"valid","index":[]}',
        }}
        with self.assertRaises(MutationEvidenceError):
            agent_comparison_baseline(self.original, saved, 'worker')

    def test_legacy_saved_comparison_snapshot_remains_readable(self):
        from loop_manager_internal.git.managed_rebase_adoption import agent_comparison_baseline
        import hashlib
        import json
        parsed = json.loads(self.original.serialize())
        parsed['version'] = 3
        for key in ('refs', 'worktrees', 'config', 'operations'):
            parsed.pop(key)
        saved = {'current_rebase_agent_baseline': {
            'kind': 'current_rebase_comparison_baseline', 'role': 'worker',
            'original': hashlib.sha256(self.original.serialize().encode()).hexdigest(),
            'snapshot': json.dumps(parsed),
        }}
        restored = agent_comparison_baseline(self.original, saved, 'worker')
        self.assertEqual(restored.head, self.original.head)
        self.assertFalse(restored.shared_metadata_complete)

    def test_version_five_comparison_requires_scope_identity(self):
        from loop_manager_internal.git.managed_rebase_adoption import agent_comparison_baseline
        from loop_manager_internal.git.manager_git_journal import MutationEvidenceError
        import hashlib
        import json
        original = replace(self.original, scope_id='a' * 32,
                           scope_start_digest='b' * 64)
        valid = json.loads(original.serialize())
        saved = {'current_rebase_agent_baseline': {
            'kind': 'current_rebase_comparison_baseline', 'role': 'worker',
            'original': hashlib.sha256(original.serialize().encode()).hexdigest(),
            'snapshot': original.serialize(),
        }}
        self.assertEqual(agent_comparison_baseline(original, saved, 'worker'), original)
        for field, invalid in (('scope_id', None), ('scope_id', 'not-a-token'),
                               ('scope_id', 'c' * 32),
                               ('scope_start_digest', None), ('scope_start_digest', 'bad'),
                               ('scope_start_digest', 'd' * 64)):
            with self.subTest(field=field, invalid=invalid):
                damaged = {**valid, field: invalid}
                saved['current_rebase_agent_baseline']['snapshot'] = json.dumps(damaged)
                with self.assertRaises(MutationEvidenceError):
                    agent_comparison_baseline(original, saved, 'worker')
        saved['current_rebase_agent_baseline']['snapshot'] = json.dumps({
            key: value for key, value in valid.items()
            if key not in ('scope_id', 'scope_start_digest')
        } | {'version': 4})
        with self.assertRaises(MutationEvidenceError):
            agent_comparison_baseline(original, saved, 'worker')


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
        from loop_manager_internal.git.managed_rebase_adoption import needs_agent_comparison
        self.assertFalse(needs_agent_comparison(
            replace(self.case.checkpoint.get('repo', 'G-047'), operation='commit'),
            self.original, accepted_start))
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

    def test_persisted_actual_agent_result_still_rejects_shared_ref_violation(self):
        connection = self.case.fixture.connection
        lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker')
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo = 'repo'",
                           (self.current.serialize(),))
        self.assertEqual(lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo'), self.current)
        violation = replace(self.current, refs={**self.current.refs, 'refs/heads/other': ('foreign', '')})
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo = 'repo'",
                           (violation.serialize(),))
        with self.subTest():
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
            with self.subTest():
                lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo')
