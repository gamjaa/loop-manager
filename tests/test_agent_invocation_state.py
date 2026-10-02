"""Durability and idempotence of the invocation ledger."""

import sqlite3
from unittest import TestCase

from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.storage.agent_invocation_state import InvocationRepository, ensure_schema
from loop_manager_internal.agents.scope_contracts import GitSnapshot


class InvocationRepositoryTests(TestCase):
    def test_scope_binding_requires_matching_role_workspace_and_saved_snapshot(self):
        for field in ('agent_role', 'worktree', 'agent_snapshot'):
            self.connection.execute(f'ALTER TABLE projects ADD COLUMN {field} TEXT')
        snapshot = GitSnapshot('head', None, {}, {}, {}, {}, {}, {}, scope_id='scope')
        self.connection.execute("UPDATE projects SET agent_role='maintenance_planner', "
                                "worktree='/managed', agent_snapshot=?", (snapshot.serialize(),))
        self.assertEqual(self.repository.invocation_scope_id('repo', 'planner', '/managed'), 'scope')
        self.assertIsNone(self.repository.invocation_scope_id('repo', 'reviewer', '/managed'))
        self.assertIsNone(self.repository.invocation_scope_id('repo', 'planner', '/other'))
        self.assertTrue(self.connection.in_transaction)

    def test_latest_scope_session_is_bound_to_last_physical_attempt(self):
        self.assertIsNone(self.repository.latest_scope_session('scope'))
        for session, scope in (('original', 'scope'), ('unrelated', 'other'), ('resumed', 'scope')):
            self.repository.start(InvocationContext('repo', None), 'planner', 'codex',
                                  ResolvedProfile('model', 'high'), session, scope_id=scope)
        self.assertEqual(self.repository.latest_scope_session('scope'), 'resumed')

    def test_replacement_ledger_links_transition_and_profiles(self):
        context = InvocationContext(
            'repo', 'G-041', transition='replacement', transition_cause='model_changed',
            transition_id='transition-one', previous_provider='codex',
            previous_model='old', previous_effort='low',
            previous_session_id='old-session',
        )
        identity = self.repository.start(context, 'worker', 'codex',
                                         ResolvedProfile('new', 'high'), None)
        self.repository.observe_session(identity, 'new-session')
        row = self.connection.execute(
            'SELECT * FROM agent_invocations WHERE invocation_id = ?', (identity,),
        ).fetchone()
        self.assertEqual((row['transition'], row['transition_cause'],
                          row['transition_id'], row['previous_model'],
                          row['previous_session_id'], row['model'], row['session_id']),
                         ('replacement', 'model_changed', 'transition-one', 'old',
                          'old-session', 'new', 'new-session'))

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE projects (repo TEXT PRIMARY KEY, phase TEXT)")
        self.connection.execute("INSERT INTO projects VALUES ('repo', 'VERIFYING')")
        ensure_schema(self.connection)
        self.repository = InvocationRepository(self.connection)

    def test_repeated_migration_and_interruption_keep_session_identity(self):
        ensure_schema(self.connection)
        identity = self.repository.start(InvocationContext("repo", "G-039"),
                                         "worker", "codex", ResolvedProfile("model", "high"), None)
        self.repository.observe_session(identity, "observed-session")
        self.assertEqual(self.repository.reconcile_interrupted(), 1)
        self.assertEqual(self.repository.reconcile_interrupted(), 0)
        row = self.connection.execute("SELECT * FROM agent_invocations").fetchone()
        self.assertEqual((row["session_id"], row["result_kind"], row["collection_status"]),
                         ("observed-session", "interrupted", "unknown"))
        self.assertEqual(self.connection.execute("SELECT phase FROM projects").fetchone()[0],
                         "VERIFYING")

    def test_summary_uses_nullable_counters_and_provider_input_semantics(self):
        context = InvocationContext("repo", "G-039", attempt=2, transition="resume")
        codex = self.repository.start(context, "worker", "codex",
                                      ResolvedProfile("model", "high"), "session")
        self.repository.finish(codex, "success", 0, 500, "session", Usage(100, 60, None, 10))
        claude = self.repository.start(context, "reviewer", "claude",
                                       ResolvedProfile("other", "medium"), "session-2")
        self.repository.finish(claude, "success", 0, 300, None, Usage(20, 40, 5, 4))
        rows = {row["provider"]: row for row in self.repository.summary("repo")}
        self.assertEqual((rows["codex"]["uncached_input_tokens"], rows["codex"]["cache_ratio"]),
                         (40, 0.6))
        self.assertEqual(rows["claude"]["uncached_input_tokens"], 20)
        self.assertAlmostEqual(rows["claude"]["cache_ratio"], 40 / 65)
        self.assertIsNone(rows["codex"]["cache_write_input_tokens"])
        self.assertEqual((rows["codex"]["calls"], rows["codex"]["retries"],
                          rows["codex"]["duration_ms"]), (1, 1, 500))

    def test_codex_cache_ratio_excludes_incomplete_usage_pairs(self):
        context = InvocationContext("repo", "G-039")
        profile = ResolvedProfile("model", "high")
        for usage in (Usage(100, 40), Usage(None, 1000), Usage(200, None)):
            identity = self.repository.start(context, "worker", "codex", profile, None)
            self.repository.finish(identity, "success", 0, 10, None, usage)

        row = self.repository.summary("repo")[0]
        self.assertEqual((row["calls"], row["input_tokens"],
                          row["cached_input_tokens"]), (3, 300, 1040))
        self.assertAlmostEqual(row["cache_ratio"], 0.4)

        unknown = self.repository.start(
            context, "reviewer", "codex", profile, None)
        self.repository.finish(unknown, "success", 0, 10, None, Usage(None, 5))
        rows = {item["role"]: item for item in self.repository.summary("repo")}
        self.assertIsNone(rows["reviewer"]["cache_ratio"])
