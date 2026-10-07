"""Durability and idempotence of the invocation ledger."""

import sqlite3
from unittest import TestCase

from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence
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

    def test_conflicting_finish_cannot_invalidate_successful_evidence(self):
        identity = self.repository.start(
            InvocationContext("repo", "G-134"), "worker", "codex",
            ResolvedProfile("model", "high"), "session",
        )
        evidence = IsolationEvidence(
            "codex", "version", "Linux:test", "worker", "/worktree", "repo",
            "a" * 64, "verified", snapshot_digest="b" * 64,
            configuration_digest="c" * 64,
        )
        self.repository.record_isolation(identity, evidence)
        self.repository.finish(identity, "success", 0, 1, "session", Usage())
        self.repository.finish(identity, "transient_failure", 1, 2, None, Usage())

        row = self.connection.execute("""SELECT call.result_kind, proof.status
            FROM agent_invocations AS call JOIN agent_isolation_evidence AS proof
            USING (invocation_id) WHERE call.invocation_id = ?""", (identity,)).fetchone()
        self.assertEqual(tuple(row), ("success", "verified"))
        self.assertTrue(self.repository.verified_invocation(
            identity, session_id="session", expected=evidence,
        ))

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


class CommonGuidanceStateTests(TestCase):
    def test_additive_migration_preserves_legacy_invocation_and_session_audit(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        ensure_schema(connection)
        repository = InvocationRepository(connection)
        identity = repository.start(InvocationContext('repo', 'G-147'), 'worker', 'codex',
                                    ResolvedProfile('old', 'high'), 'old-session')
        repository.finish(identity, 'transient_failure', 1, 17, 'old-session', Usage(5, 2))
        before = dict(connection.execute('SELECT * FROM agent_invocations').fetchone())
        ensure_schema(connection)
        self.assertEqual(dict(connection.execute('SELECT * FROM agent_invocations').fetchone()), before)
        self.assertIsNone(before['guidance_content_hash'])
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM common_guidance_versions').fetchone()[0], 0)

    def test_snapshot_and_session_binding_survive_database_reopen_without_reloading_runtime(self):
        from pathlib import Path
        import tempfile
        from unittest import mock
        from loop_manager_internal.agents import common_guidance
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'runtime'
            (runtime / 'docs').mkdir(parents=True)
            (runtime / common_guidance.COMMON_PATH).write_text('original', encoding='utf-8')
            for path in set(common_guidance.ROLE_PATHS.values()):
                (runtime / path).write_text('role: ' + path, encoding='utf-8')
            database = root / 'state.db'
            context = InvocationContext('repo', 'G-147')
            with sqlite3.connect(database) as connection, mock.patch.object(common_guidance, 'RUNTIME_ROOT', runtime), \
                 mock.patch.object(common_guidance.subprocess, 'run', return_value=type('Revision', (), {'returncode': 0, 'stdout': 'a' * 40})()):
                repository = GuidanceRepository(connection)
                snapshot = common_guidance.resolve(repository, context, 'worker', root / 'project', root / 'state')
                repository.bind_session('repo', 'codex', 'session', snapshot.task_id)
            for path in common_guidance.GUIDANCE_PATHS:
                (runtime / path).unlink()
            with sqlite3.connect(database) as restarted, mock.patch.object(common_guidance, 'RUNTIME_ROOT', runtime):
                self.addCleanup(restarted.close)
                repository = GuidanceRepository(restarted)
                resumed = common_guidance.resolve(repository, context, 'reviewer', root / 'project', root / 'state')
                self.assertEqual(resumed.task_id, snapshot.task_id)
                self.assertEqual(resumed.source_revision, snapshot.source_revision)
                self.assertEqual(resumed.content, 'original\n\nrole: docs/AGENTS.reviewer.md')
                self.assertEqual(snapshot.content, 'original\n\nrole: docs/AGENTS.worker.md')
                self.assertEqual(repository.session_task('repo', 'codex', 'session'), snapshot.task_id)

    def test_changed_plan_guidance_invalidates_all_candidate_result_tokens(self):
        from dataclasses import replace
        from loop_manager_internal.execution.execution_revision import DefinitionSnapshot, classify_definition_delta, DefinitionDelta
        original = DefinitionSnapshot('a' * 40, 'docs/plans/[G-147] Test.md', 'Test', ('docs/PRODUCT.md',), (),
                                      'high', 'high', 'high', 'body', 'source', (), 'old')
        changed = replace(original, guidance_hash='new')
        self.assertEqual(classify_definition_delta(original, changed), {DefinitionDelta.GUIDANCE})
        for method in ('worker_candidate_token', 'verify_token', 'review_token', 'publication_token'):
            self.assertNotEqual(getattr(original, method)('head'), getattr(changed, method)('head'))
        self.assertEqual(DefinitionSnapshot.deserialize(original.serialize()), original)


    def test_publication_fixture_tokens_use_saved_plan_guidance_without_git_execution(self):
        from pathlib import Path
        from unittest import mock
        from tests.support.database import runtime_connection
        from tests.support.publication import seed_validated_candidate
        from loop_manager_internal.storage import runtime_state
        from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
        from loop_manager_internal.agents import common_guidance
        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime_state.RuntimeStateRepository(connection).activate(
            'repo', 'G-047', activation_sha='base', branch='branch', worktree='/candidate')
        identity = common_guidance.task_identity(InvocationContext('repo', 'G-047'), 'worker', Path('/candidate'))
        GuidanceRepository(connection).save(identity, 'repo', 'a' * 64, 'a' * 40, '/snapshot')
        def revision(_path, *_args):
            return 'base' if _path == Path('repo') else 'candidate'
        with mock.patch('tests.support.publication.git_command', side_effect=revision), \
             mock.patch('loop_manager._revision_execplan_body_hash', return_value='body'), \
             mock.patch('loop_manager._revision_execplan_source_fingerprint', return_value='source'), \
             mock.patch.object(common_guidance, 'runtime_content_hash', return_value='b' * 64):
            snapshot = seed_validated_candidate(connection, Path('repo'), Path('/candidate'), 'G-047')
        self.assertEqual(snapshot.guidance_hash, 'a' * 64)
        row = connection.execute('SELECT verify_token, review_token FROM exec_plans').fetchone()
        self.assertEqual(row['verify_token'], snapshot.verify_token('base', 'candidate'))
        self.assertEqual(row['review_token'], snapshot.review_token('base', 'candidate'))
