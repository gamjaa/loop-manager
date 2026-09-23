"""Durability and idempotence of the invocation ledger."""

import sqlite3
from unittest import TestCase

from agent_invocation import InvocationContext, ResolvedProfile, Usage
from agent_invocation_state import InvocationRepository, ensure_schema


class InvocationRepositoryTests(TestCase):
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
