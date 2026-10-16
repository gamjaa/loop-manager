"""Unit tests for connection, schema, catalog migration and publication storage."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.storage import (
    database, execplan_catalog_state as catalog, catalog_migration,
    planning_publication_state as publication,
)
from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot, PlanRevision
from loop_manager_internal.planning.planning_catalog import (
    PlanDeletion, PlannerValidationError, mutable_snapshot_token,
)
from tests.support.database import AutomationCatalogFixture
from tests.support.catalog import active_snapshot, plan_text


class StateFoundationTests(AutomationCatalogFixture, unittest.TestCase):
    def setUp(self):
        self.connection = self._connection()
        self.addCleanup(self.connection.close)

    def test_connection_requires_explicit_init_and_preserves_stored_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with self.assertRaisesRegex(RuntimeError, '상태 DB가 없습니다'):
                database.connect_database(state)
            self.assertFalse((state / 'loop-manager.db').exists())
            connection = database.connect_database(state, create=True)
            connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('saved', 'develop')")
            connection.commit(); connection.close()
            resumed = database.connect_database(state)
            try:
                self.assertEqual(resumed.execute('SELECT repo, base_branch FROM projects').fetchone()[:],
                                 ('saved', 'develop'))
                self.assertTrue(resumed.execplans_schema_ready)
                self.assertEqual(resumed._timeout, 30)
                self.assertEqual(resumed.execute('PRAGMA busy_timeout').fetchone()[0], 0)
            finally:
                resumed.close()
            child = database.connect_database(state, run_migrations=False)
            try:
                self.assertTrue(child.execplans_schema_ready)
            finally:
                child.close()

    def test_corrupt_database_is_rejected_without_reinitializing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'loop-manager.db'; path.write_bytes(b'corrupt')
            with self.assertRaisesRegex(RuntimeError, '상태 DB를 읽을 수 없습니다'):
                database.connect_database(Path(directory))
            self.assertEqual(path.read_bytes(), b'corrupt')

    def test_external_lock_during_open_is_a_wait_timeout_without_corruption_diagnosis(self):
        from loop_manager_internal.storage.db_access import open_connection, DatabaseWaitTimeout
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            database.connect_database(state, create=True).close()
            external = sqlite3.connect(state / 'loop-manager.db')
            self.addCleanup(external.close)
            external.execute('BEGIN EXCLUSIVE')
            with mock.patch.object(database, 'open_connection',
                    side_effect=lambda path, **kw: open_connection(path, timeout=.05, factory=kw['factory'])):
                with self.assertRaises(DatabaseWaitTimeout):
                    database.connect_database(state, run_migrations=False)
            external.rollback()
            external.close()
            resumed = database.connect_database(state, run_migrations=False)
            resumed.close()

    def test_schema_backfill_is_idempotent_and_does_not_commit_caller_changes(self):
        self._insert_plan(self.connection, 'G-001', 'READY')
        database._ensure_execplans_schema(self.connection)
        first = tuple(self.connection.execute('SELECT * FROM exec_plans').fetchone())
        database._ensure_execplans_schema(self.connection)
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM exec_plans').fetchone()), first)
        self.connection.execute("UPDATE projects SET processed_sha = 'pending'")
        database._ensure_execplans_schema(self.connection)
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertIsNone(self.connection.execute('SELECT processed_sha FROM projects').fetchone()[0])

    def test_schema_backfill_failure_rolls_back_only_its_savepoint(self):
        self._insert_plan(self.connection, 'G-001', 'READY')
        self.connection.execute("UPDATE exec_plans SET publication_path = NULL")
        self.connection.execute('''CREATE TRIGGER stop_backfill BEFORE UPDATE OF publication_path ON exec_plans
                                   BEGIN SELECT RAISE(ABORT, 'backfill interrupted'); END''')
        self.connection.commit()
        self.connection.execute("UPDATE projects SET processed_sha = 'caller-change'")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'backfill interrupted'):
            database._ensure_execplans_schema(self.connection)
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(self.connection.execute('SELECT processed_sha FROM projects').fetchone()[0], 'caller-change')
        self.assertIsNone(self.connection.execute('SELECT publication_path FROM exec_plans').fetchone()[0])
        self.connection.rollback()
        self.connection.execute('DROP TRIGGER stop_backfill')
        database._ensure_execplans_schema(self.connection)
        self.assertEqual(self.connection.execute('SELECT publication_path FROM exec_plans').fetchone()[0],
                         'docs/plans/[G-001] Plan G-001.md')

    def test_worker_level_keeps_metadata_error_and_upper_bound(self):
        self.assertEqual(catalog.worker_level({'complexity': 'high', 'risk': 'critical'}), 6)
        for metadata in ({}, {'complexity': [], 'risk': 'low'}, {'complexity': 'other', 'risk': 'low'}):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, '검증된 ExecPlan'):
                catalog.worker_level(metadata)

    def test_registration_reuses_json_contract_and_does_not_commit(self):
        metadata = dict(id='G-001', title='저장 계약', sources=['docs/PRODUCT.md', 'docs/requirements/한글.md'],
                        depends_on=[], priority='normal', complexity='low', risk='low')
        path = 'docs/plans/[G-001] 저장 계약.md'
        catalog.register_published_plan_row(self.connection, 'repo', path, metadata, 'hash', 'revision')
        self.assertTrue(self.connection.in_transaction)
        catalog.register_published_plan_row(self.connection, 'repo', path, metadata, 'hash', 'revision')
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['sources'], '["docs/PRODUCT.md","docs/requirements/한글.md"]')
        self.assertEqual(json.loads(row['sources']), metadata['sources'])
        self.assertEqual(catalog.publication_identity(row).path, path)
        self.assertEqual(catalog.DefinitionStateRepository(self.connection).publication_audit('repo', 'G-001').revision_sha,
                         'revision')
        with self.assertRaisesRegex(RuntimeError, '충돌'):
            catalog.register_published_plan_row(self.connection, 'repo', path, metadata, 'other-hash', 'revision')
        self.connection.rollback()
        self.assertEqual(self.connection.execute('SELECT count(*) FROM exec_plans').fetchone()[0], 0)

    def test_migration_rollback_and_explicit_caller_transaction(self):
        pinned = active_snapshot('a' * 40, {f'docs/plans/[{plan_id}] {title}.md': plan_text(plan_id, title)
                                          for plan_id, title in [('G-001', 'One'), ('G-002', 'Two')]})
        def fail_second(root, revision, entry):
            if entry.plan_id == 'G-002':
                raise RuntimeError('proof interrupted')
            return 'a' * 40
        effects = catalog_migration.CatalogMigrationEffects(
            revision_execplan_snapshot=mock.Mock(side_effect=AssertionError('pinned catalog was ignored')),
            pr_for_branch=mock.Mock(return_value=None), _current_execplan_sha=fail_second,
            _LegacyCatalogMergeProof=mock.Mock())
        for managed in (True, False):
            with self.subTest(manage_transaction=managed):
                with self.assertRaisesRegex(RuntimeError, 'proof interrupted'):
                    catalog_migration._migrate_execplan_catalog(self.connection, effects=effects,
                        reconcile_history=True, target_repo='repo', manage_transaction=managed, pinned_catalog=pinned)
                self.assertEqual(self.connection.execute('SELECT count(*) FROM exec_plans').fetchone()[0],
                                 0 if managed else 1)
                self.connection.rollback()
        effects.revision_execplan_snapshot.assert_not_called()

    def test_publication_intent_json_round_trip_conflict_and_hash_tampering(self):
        manifest = json.dumps({
            'version': 1, 'publication_paths': ['한글.md'],
            'publication_hashes': {}, 'plans': [], 'revisions': [], 'deletions': [],
            'mutable_snapshot': {
                'repo': 'repo', 'active_sha': 'base',
                'token': mutable_snapshot_token('repo', 'base', ()), 'plans': [],
            },
        }, ensure_ascii=True, separators=(',', ':'))
        intent = publication.GeneralPlanningPublication('repo', 'prepared', 'base', 'commit',
            mutable_snapshot_token('repo', 'base', ()),
            'specification', 'planning', manifest, hashlib.sha256(manifest.encode()).hexdigest())
        self.assertEqual(publication.record_general_planning_publication(self.connection, intent), intent)
        self.assertEqual(publication.record_general_planning_publication(self.connection, intent), intent)
        self.assertEqual(publication.general_planning_publication(self.connection, 'repo').manifest,
                         manifest)
        with self.assertRaisesRegex(PlannerValidationError, '다른 general'):
            publication.record_general_planning_publication(self.connection, replace(intent, commit_sha='other'))
        self.assertEqual(publication.general_planning_publication(self.connection, 'repo'), intent)
        self.connection.execute("UPDATE general_planning_publications SET manifest = 'changed'")
        with self.assertRaisesRegex(PlannerValidationError, 'hash'):
            publication.general_planning_publication(self.connection, 'repo')
        self.connection.rollback()
        publication.delete_general_planning_publication(self.connection, 'repo')
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(publication.general_planning_publication(self.connection, 'repo'), intent)

    def test_mutable_publication_preserves_audit_and_rolls_back_partial_rename(self):
        self._insert_plan(self.connection, 'G-001', 'READY', title='One')
        self._insert_plan(self.connection, 'G-002', 'READY', title='Two')
        database._ensure_execplans_schema(self.connection)
        def snapshot(plan_id, title):
            return ReadyPlanSnapshot(plan_id, f'docs/plans/[{plan_id}] {title}.md', title,
                ('docs/PRODUCT.md',), (), 'low', 'low', 'normal', 'hash-' + plan_id, 'plan-sha', '')
        original = snapshot('G-001', 'One'); second = snapshot('G-002', 'Two')
        plan = dict(id='G-001', title='Renamed', sources=['docs/PRODUCT.md'], depends_on=[],
                    priority='normal', complexity='low', risk='low')
        revision = PlanRevision('docs/plans/[G-001] Renamed.md', plan, original)
        with self.assertRaisesRegex(PlannerValidationError, 'deletion'):
            with self.connection:
                publication.apply_mutable_catalog_changes(self.connection, 'repo',
                    deletions=(PlanDeletion(second), PlanDeletion(replace(original, content_hash='stale'))),
                    revisions=(revision,), snapshot_token='token', plan_commit='new', content_hash=lambda path: 'new-hash')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM exec_plans').fetchone()[0], 2)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM execplan_cancellations').fetchone()[0], 0)
        with self.assertRaisesRegex(RuntimeError, 'hash interrupted'):
            with self.connection:
                publication.apply_mutable_catalog_changes(self.connection, 'repo', deletions=(), revisions=(revision,),
                    snapshot_token='token', plan_commit='new', content_hash=mock.Mock(side_effect=RuntimeError('hash interrupted')))
        self.assertEqual(self.connection.execute("SELECT path FROM exec_plans WHERE plan_id = 'G-001'").fetchone()[0], original.path)
        with self.connection:
            publication.apply_mutable_catalog_changes(self.connection, 'repo', deletions=(PlanDeletion(second),),
                revisions=(revision,), snapshot_token='token', plan_commit='new', content_hash=lambda path: 'new-hash')
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual((row['path'], row['content_hash'], row['current_revision_sha']), (revision.path, 'new-hash', 'new'))
        self.assertEqual((row['publication_path'], row['publication_content_hash'], row['publication_sha']),
                         (original.path, original.content_hash, original.plan_sha))
        self.assertEqual(self.connection.execute('SELECT deletion_commit FROM execplan_cancellations').fetchone()[0], 'new')
