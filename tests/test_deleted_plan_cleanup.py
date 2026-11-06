"""Task deletion must erase owned history without touching another task."""
import json
from types import SimpleNamespace
import unittest

from loop_manager_internal.catalog.deleted_plan_cleanup import resolved_references
from loop_manager_internal.storage import deleted_plan_state, execplan_catalog_state
from tests.support.catalog import catalog_reconciliation_fixture


class DeletedPlanCleanupTest(unittest.TestCase):
    def test_maintenance_erasure_prunes_exact_membership_and_target_allocations_atomically(self):
        import sqlite3
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        store = MaintenanceRepository(self.connection)
        fields = ('prerequisite_plan_ids', 'refactoring_plan_ids', 'assessment_plan_ids', 'revalidation_plan_ids',
                  'publication_refactoring_plan_ids', 'publication_revalidation_plan_ids')
        allocations = [
            ['docs/plans/[G-009] Deleted.md', 'G-009', 'docs/plans/[G-001] Deleted.md', 'G-001'],
            ['docs/plans/[G-001] Independent.md', 'G-001', 'docs/plans/[G-002] Independent.md', 'G-002'],
        ]
        self.connection.execute('INSERT INTO maintenance_cycles (repo,phase,cycle_id,cycle_request_id,'
            'consumed_request_id,last_success_commit,last_success_merged_count,last_cycle_commit,last_cycle_merged_count,'
            'revision,publication_id_allocations,' + ','.join(fields) + ') '
            "VALUES ('/repo','refactoring','cycle','request','consumed','assessed',4,'previous',2,7,?" + ',?' * len(fields) + ')',
            (json.dumps(allocations), *([json.dumps(['G-001', 'G-002'])] * len(fields))))
        self.connection.execute("INSERT INTO projects (repo,base_branch) VALUES ('/other','main')")
        self.connection.execute("INSERT INTO maintenance_cycles (repo,refactoring_plan_ids) VALUES ('/other','[\"G-001\"]')")
        self.connection.commit()
        original = dict(self.connection.execute("SELECT * FROM maintenance_cycles WHERE repo='/repo'").fetchone())
        foreign = dict(self.connection.execute("SELECT * FROM maintenance_cycles WHERE repo='/other'").fetchone())
        self.connection.execute("CREATE TRIGGER reject_maintenance_erasure BEFORE DELETE ON exec_plans "
                                "BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute('DROP TRIGGER reject_maintenance_erasure')
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        current = dict(self.connection.execute("SELECT * FROM maintenance_cycles WHERE repo='/repo'").fetchone())
        for field in fields:
            self.assertEqual(json.loads(current[field]), ['G-002'])
        self.assertEqual(json.loads(current['publication_id_allocations']), [allocations[1]])
        self.assertEqual(current['revision'], original['revision'] + 1)
        for field in set(original) - {*fields, 'publication_id_allocations', 'revision', 'updated_at'}:
            self.assertEqual(current[field], original[field])
        self.assertEqual(dict(self.connection.execute("SELECT * FROM maintenance_cycles WHERE repo='/other'").fetchone()), foreign)
        self.assertEqual(store.get('/repo').refactoring_plan_ids, ('G-002',))
        before = tuple(self.connection.iterdump())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_maintenance_publication_release_or_unattributed_assessment_keeps_erasure_pending(self):
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        from loop_manager_internal.storage.task_calls import CallDeferred
        MaintenanceRepository(self.connection)
        self.connection.execute("INSERT INTO maintenance_cycles (repo,phase,refactoring_plan_ids) "
                                "VALUES ('/repo','refactoring','[\"G-001\"]')")
        for stage, release, assessment, ids in (
            ('prepared', None, None, '["G-001"]'), ('remote_confirmed', None, None, '["G-001"]'),
            (None, 'release', None, '["G-001"]'), (None, None, 'saved assessment', '["G-001"]'),
            (None, None, None, 'broken JSON'),
        ):
            with self.subTest(stage=stage, release=release, assessment=assessment, ids=ids):
                self.connection.execute('UPDATE maintenance_cycles SET publication_stage=?,legacy_release_identity=?,'
                    'full_assessment_result=?,refactoring_plan_ids=? WHERE repo=?', (stage, release, assessment, ids, '/repo'))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("UPDATE maintenance_cycles SET refactoring_plan_ids='[\"G-001\"]'")
        self.connection.commit()
        before = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        after = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        self.assertEqual(json.loads(after['refactoring_plan_ids']), [])
        self.assertEqual(after['phase'], before['phase'])
        self.assertEqual(after['last_cycle_merged_count'], before['last_cycle_merged_count'])
        self.assertEqual(after['last_success_commit'], before['last_success_commit'])

    def test_live_common_correction_link_requires_same_goal_successor_before_erasure(self):
        from loop_manager_internal.storage import validation_feedback_state
        from loop_manager_internal.storage.task_calls import CallDeferred
        validation_feedback_state.ensure_schema(self.connection)
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED' WHERE plan_id='G-002'")
        self.connection.execute("INSERT INTO validation_results "
            "(id,repo,plan_id,stage,classification,raw_output,identities,created_at) "
            "VALUES (1,'/repo','G-002','verify','failed','independent failure','[]','now')")
        self.connection.execute("INSERT INTO common_defect_reports VALUES (1,'{}',NULL)")
        self.connection.execute("INSERT INTO common_defect_links "
            "(repo,result_id,goal_key,correction_plan_id,reviewed_revision,confirmed_revision,evidence) "
            "VALUES ('/repo',1,'goal','G-001','old','first','{}')")
        self.connection.execute("INSERT INTO common_defect_merges "
            "(repo,plan_id,pr_url,head,merge_oid,verification) VALUES ('/other','G-001','pr','head','merged','proof')")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("INSERT INTO exec_plans "
            "(repo,plan_id,path,title,sources,depends_on,priority,content_hash,status) "
            "VALUES ('/repo','G-003','new.md','Replacement','[]','[]','normal','hash','READY')")
        for goal, revision in (('different-goal', 'second'), ('goal', 'third')):
            self.connection.execute("INSERT INTO common_defect_links "
                "(repo,result_id,goal_key,correction_plan_id,reviewed_revision,confirmed_revision,evidence) "
                "VALUES ('/repo',1,?,'G-003','new',?,'{}')", (goal, revision))
            self.connection.commit()
            if goal == 'different-goal':
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        tables = ('validation_results', 'common_defect_reports', 'common_defect_merges')
        preserved = {table: [dict(row) for row in self.connection.execute(f'SELECT * FROM {table}')] for table in tables}
        successors = [dict(row) for row in self.connection.execute("SELECT * FROM common_defect_links WHERE correction_plan_id='G-003'")]
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        for table in tables:
            self.assertEqual([dict(row) for row in self.connection.execute(f'SELECT * FROM {table}')], preserved[table])
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM common_defect_links')], successors)

    def test_common_correction_merge_receipt_protects_history_without_execution_row(self):
        from loop_manager_internal.storage import common_defect_state
        from loop_manager_internal.storage.task_calls import CallDeferred
        common_defect_state.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO common_defect_merges "
            "(repo,plan_id,pr_url,head,merge_oid,verification) VALUES ('/repo','G-001','pr','head','merged','proof')")
        self.connection.execute("DELETE FROM exec_plans WHERE plan_id='G-001'")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_common_correction_link_with_missing_or_foreign_report_keeps_erasure_pending(self):
        from loop_manager_internal.storage import validation_feedback_state
        from loop_manager_internal.storage.task_calls import CallDeferred
        validation_feedback_state.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO common_defect_links "
            "(repo,result_id,goal_key,correction_plan_id,reviewed_revision,confirmed_revision,evidence) "
            "VALUES ('/repo',1,'goal','G-001','old','first','{}')")
        self.connection.commit()
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                if foreign:
                    self.connection.execute("INSERT INTO validation_results "
                        "(id,repo,plan_id,stage,classification,raw_output,identities,created_at) "
                        "VALUES (1,'/other','G-002','verify','failed','foreign failure','[]','now')")
                    self.connection.commit()
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_notification_erasure_removes_incident_versions_with_foreign_keys_and_preserves_independent_audit(self):
        import sqlite3
        from loop_manager_internal.notifications import notifications
        notifications.ensure_schema(self.connection)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute("INSERT INTO projects (repo,base_branch) VALUES ('/other','main')")
        for repo, key, plan in (('/repo', 'target', 'G-001'), ('/repo', 'independent', 'G-002'),
                                ('/repo', 'project', 'G-002'), ('/other', 'foreign', 'G-001')):
            scope = 'project' if key == 'project' else 'plan'
            payload = json.dumps(dict(execplan_id=plan, scope=scope, cause='saved cause'))
            incident = self.connection.execute('INSERT INTO notified_block_incidents '
                '(repo,subject,block_key,event_key,payload) VALUES (?,?,?,?,?)',
                (repo, 'project' if scope == 'project' else 'plan:' + plan, key, key, payload))
            self.connection.execute('INSERT INTO notification_outbox '
                '(event_key,repo,event_type,payload,rendered_text,confirmed_pieces,last_error) '
                "VALUES (?,?,'blocked',?,'saved body',1,'saved error')", (key, repo, payload))
            self.connection.execute('INSERT INTO block_notice_events VALUES (?,?)', (key, incident.lastrowid))
            if key == 'target':
                self.connection.execute("INSERT INTO notification_outbox (event_key,repo,event_type,payload) "
                    "VALUES ('legacy-version','/repo','blocked','legacy raw payload')")
                self.connection.execute("INSERT INTO block_notice_events VALUES ('legacy-version',?)", (incident.lastrowid,))
        self.connection.commit()
        self.assertEqual(self.connection.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.connection.execute("CREATE TRIGGER reject_notification_erasure BEFORE DELETE ON exec_plans "
                                "BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'interrupted'):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute('DROP TRIGGER reject_notification_erasure')
        tables = ('notification_outbox', 'notified_block_incidents', 'block_notice_events')
        expected = {table: [dict(row) for row in self.connection.execute(f'SELECT * FROM {table}')
                           if row['event_key'] not in ('target', 'legacy-version')] for table in tables}
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        for table in tables:
            self.assertEqual([dict(row) for row in self.connection.execute(f'SELECT * FROM {table}')], expected[table])
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        before = tuple(self.connection.iterdump())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_unattributed_or_mixed_notifications_hold_final_erasure_without_losing_history(self):
        from loop_manager_internal.notifications import notifications
        from loop_manager_internal.storage.task_calls import CallDeferred
        notifications.ensure_schema(self.connection)
        for kind, payload in (
            ('blocked', 'broken JSON'), ('draft_decisions', '{"plans":[null]}'),
            ('draft_decisions', '{"plans":[{"id":"G-001"},{"id":"G-002"}]}'),
            ('plans_published', '{"plans":[{"id":"G-001"},{"id":"G-002"}]}'),
            ('blocked', '{"scope":"project","execplan_id":"G-001","started_execplan_id":"G-002"}'),
        ):
            with self.subTest(kind=kind, payload=payload):
                self.connection.execute('INSERT OR REPLACE INTO notification_outbox '
                    '(event_key,repo,event_type,payload,rendered_text,confirmed_pieces,last_error) '
                    "VALUES ('mixed','/repo',?,?,'preserved body',1,'saved error')", (kind, payload))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("UPDATE notification_outbox SET event_type='blocked',payload=? WHERE event_key='mixed'",
                                (json.dumps(dict(execplan_id='G-001', scope='plan')),))
        incident = self.connection.execute("INSERT INTO notified_block_incidents "
            "(repo,subject,block_key,event_key,payload) VALUES ('/repo','plan:G-002','other','mixed','{}')")
        self.connection.execute("INSERT INTO block_notice_events VALUES ('mixed',?)", (incident.lastrowid,))
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("DELETE FROM block_notice_events WHERE event_key='mixed'")
        # A single-owner publication is removable after current evidence is supplied.
        self.connection.execute("UPDATE notification_outbox SET event_type='plans_published',payload=? WHERE event_key='mixed'",
                                (json.dumps(dict(plans=[dict(id='G-001')])),))
        self.connection.commit()
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertIsNone(self.connection.execute('SELECT * FROM notification_outbox').fetchone())

    def test_archive_erasure_preserves_dispatch_independent_candidates_and_intents(self):
        from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
        import sqlite3
        archive = ExecPlanArchiveRepository(self.connection)
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-002'")
        merged = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone())
        for repo, scope, owner in (('/repo', 'dispatch', None), ('/repo', 'G-001', None),
                ('/repo', 'G-002', None), ('/repo', 'git:owned', 'G-001'),
                ('/repo', 'git:other-G-001', 'G-002'), ('/other', 'git:owned', 'G-001')):
            archive.observe(repo, scope, category='complete' if scope == 'G-002' else 'shared_resource', resource='G-001 appears here',
                reason='saved reason G-001', resume_condition='next_safe_boundary', attempted=True,
                owner_plan_id=owner)
        self.connection.commit()
        before = [dict(row) for row in self.connection.execute('SELECT * FROM execplan_archive_observations')]
        self.connection.execute("UPDATE execplan_archive_observations SET category='complete' WHERE repo='/repo' AND scope='G-001'")
        from loop_manager_internal.storage.task_calls import CallDeferred
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.connection.execute("UPDATE execplan_archive_observations SET category='shared_resource' WHERE repo='/repo' AND scope='G-001'")
        self.connection.execute("CREATE TRIGGER reject_archive_erasure BEFORE DELETE ON exec_plans "
                                "BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM execplan_archive_observations')], before)
        self.connection.execute('DROP TRIGGER reject_archive_erasure')
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        expected = [row for row in before if (row['repo'], row['scope']) not in {
            ('/repo', 'G-001'), ('/repo', 'git:owned')}]
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM execplan_archive_observations')], expected)
        self.assertEqual(dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()), merged)

    def test_unattributed_legacy_archive_resource_holds_final_erasure_until_reobserved(self):
        from loop_manager_internal.storage import execplan_archive_state
        from loop_manager_internal.storage.task_calls import CallDeferred
        self.connection.execute(execplan_archive_state.ARCHIVE_OBSERVATIONS_SCHEMA.replace('    owner_plan_id TEXT,\n', ''))
        self.connection.execute("INSERT INTO execplan_archive_observations "
            "(repo,scope,category,resource,reason,resume_condition,attempts) "
            "VALUES ('/repo','git:old','shared_resource','owned','unconfirmed','reobserve',3)")
        self.connection.execute("UPDATE exec_plans SET branch='owned' WHERE plan_id='G-001'")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)
        archive = execplan_archive_state.ExecPlanArchiveRepository(self.connection)
        self.assertIsNone(self.connection.execute('SELECT owner_plan_id FROM execplan_archive_observations').fetchone()[0])
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        archive.observe('/repo', 'git:old', category='shared_resource', resource='owned',
            reason='allow:separate_rebase', resume_condition='current_stage_gates', owner_plan_id='G-001')
        archive.observe('/repo', 'git:old', category='shared_resource', resource='owned',
            reason='hold:writer_unknown', resume_condition='reobserve')
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        archive.observe('/repo', 'git:old', category='shared_resource', resource='owned',
            reason='allow:separate_rebase', resume_condition='current_stage_gates', owner_plan_id='G-001')
        self.assertEqual(self.connection.execute('SELECT attempts FROM execplan_archive_observations').fetchone()[0], 3)
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertIsNone(self.connection.execute('SELECT * FROM execplan_archive_observations').fetchone())

    def recovery_fixture(self):
        from loop_manager_internal.storage import local_recovery_state
        from loop_manager_internal.notifications import notifications
        local_recovery_state.ensure_schema(self.connection)
        notifications.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO projects (repo,base_branch) VALUES ('/other','main')")
        snapshot = local_recovery_state.snapshot(self.connection, '/repo', None)
        snapshot['blocks'] = [dict(scope='plan', plan_id=key, reason_code='failure') for key in ('G-001', 'G-002')]
        snapshot['recoveries'] = [dict(subject=key, generation=2) for key in ('G-001', 'G-002', '__project__')]
        snapshot['current_failures'] = ['G-001', 'G-002', '__project__']
        raw = json.dumps(snapshot)
        for identity, repo, plan, status in (
            ('target', '/repo', 'G-001', 'completed'), ('pending', '/repo', 'G-001', 'pending'),
            ('project', '/repo', None, 'completed'), ('other', '/repo', 'G-002', 'completed'),
            ('foreign', '/other', 'G-001', 'completed'),
        ):
            saved = raw if identity == 'project' else '{}'
            self.connection.execute('INSERT INTO local_environment_recovery_requests '
                '(request_id,repo,plan_id,status,before_state,after_state,result) VALUES (?,?,?,?,?,?,?)',
                (identity, repo, plan, status, saved, saved, 'unresolved'))
            # Payload aliases are deliberately stale: the request is the owner.
            payload = dict(request_id=identity, execplan_id='G-002' if plan == 'G-001' else 'G-001')
            self.connection.execute('INSERT INTO notification_outbox '
                '(event_key,repo,event_type,payload,rendered_text,confirmed_pieces,last_error) '
                "VALUES (?,?,'recovery_unresolved',?,'saved notice',1,'saved error')",
                (identity, repo, json.dumps(payload)))
        self.connection.executemany('INSERT INTO local_environment_recheck_consumption VALUES (?,?,?)',
            [('target', 'G-001', 'verify'), ('target', '__project__', 'metadata'),
             ('project', 'G-001', 'verify'), ('project', 'G-002', 'verify'),
             ('project', '__project__', 'metadata'), ('other', 'G-002', 'verify'),
             ('foreign', 'G-001', 'verify')])
        self.connection.commit()
        return snapshot

    def test_erasure_removes_exact_recovery_request_scope_and_shared_snapshot_parts(self):
        snapshot = self.recovery_fixture()
        independent = {row['event_key']: dict(row) for row in self.connection.execute(
            "SELECT * FROM notification_outbox WHERE event_key IN ('project','other','foreign')")}
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual({row[0] for row in self.connection.execute(
            'SELECT request_id FROM local_environment_recovery_requests')}, {'project', 'other', 'foreign'})
        self.assertEqual({tuple(row) for row in self.connection.execute(
            'SELECT * FROM local_environment_recheck_consumption')}, {
                ('project', 'G-002', 'verify'), ('project', '__project__', 'metadata'),
                ('other', 'G-002', 'verify'), ('foreign', 'G-001', 'verify')})
        expected = {**snapshot,
            'plans': [item for item in snapshot['plans'] if item['plan_id'] != 'G-001'],
            'blocks': [snapshot['blocks'][1]], 'recoveries': snapshot['recoveries'][1:],
            'current_failures': snapshot['current_failures'][1:]}
        shared = self.connection.execute(
            "SELECT before_state,after_state FROM local_environment_recovery_requests WHERE request_id='project'").fetchone()
        self.assertEqual([json.loads(raw) for raw in shared], [expected, expected])
        self.assertEqual({row['event_key']: dict(row) for row in self.connection.execute(
            'SELECT * FROM notification_outbox')}, independent)
        # Restart/retry without the execution row does not touch independent audit.
        before = tuple(self.connection.iterdump())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)
        from loop_manager_internal.notifications import notifications
        notifications.enqueue_current_project_events(self.connection, '/repo')
        self.assertFalse(any(json.loads(row[0]).get('request_id') in {'target', 'pending'}
            for row in self.connection.execute("SELECT payload FROM notification_outbox WHERE repo='/repo'")))

    def test_recovery_erasure_rolls_back_and_unattributed_snapshots_stay_pending(self):
        import sqlite3
        from loop_manager_internal.storage.local_recovery_state import LocalRecoveryRequestError
        self.recovery_fixture()
        self.connection.execute("CREATE TRIGGER reject_recovery_erasure BEFORE DELETE ON exec_plans "
                                "BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'interrupted'):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute('DROP TRIGGER reject_recovery_erasure')
        for raw in ('broken JSON', '{"plans":{}}', '{"plans":[{}]}', '{"unknown":"G-001"}'):
            with self.subTest(raw=raw):
                self.connection.execute("UPDATE local_environment_recovery_requests SET after_state=? WHERE request_id='project'", (raw,))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                with self.assertRaises(LocalRecoveryRequestError):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)

    def setUp(self):
        self.fixture = catalog_reconciliation_fixture(self)
        self.connection = self.fixture.connection
        deleted_plan_state.ensure_schema(self.connection)
        self.connection.execute("UPDATE exec_plans SET depends_on='[]'")
        self.connection.commit()

    def test_forget_erases_owned_records_and_reserves_only_identity(self):
        self.connection.execute('CREATE TABLE usage_audit(repo,plan_id,tokens)')
        self.connection.executemany('INSERT INTO usage_audit VALUES (?,?,?)',
                                    [('/repo', 'G-001', 100), ('/repo', 'G-002', 200), ('/other', 'G-001', 300)])
        self.connection.commit()
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertIsNone(self.connection.execute("SELECT 1 FROM exec_plans WHERE plan_id='G-001'").fetchone())
        self.assertEqual([tuple(r) for r in self.connection.execute('SELECT * FROM usage_audit')],
                         [('/repo', 'G-002', 200), ('/other', 'G-001', 300)])
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))
        self.assertFalse(execplan_catalog_state.is_cancelled(self.connection, '/other', 'G-001'))
        self.assertIn(('G-001', ''), execplan_catalog_state.DefinitionStateRepository(self.connection).capture('/repo').reserved)
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM deleted_plan_ids').fetchone()[0], 1)

    def test_dependency_writer_and_merge_prevent_erasure(self):
        for assignment in ("status='MERGED'", "agent_role='worker'"):
            with self.subTest(assignment=assignment):
                self.connection.execute(f"UPDATE exec_plans SET {assignment} WHERE plan_id='G-001'")
                self.connection.commit()
                with self.assertRaises(ValueError):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.connection.execute("UPDATE exec_plans SET status='READY',agent_role=NULL WHERE plan_id='G-001'")
        self.connection.execute("UPDATE exec_plans SET depends_on='[\"G-001\"]' WHERE plan_id='G-002'")
        self.connection.commit()
        with self.assertRaises(ValueError):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM deleted_plan_ids').fetchone()[0], 0)

    def test_failed_delete_rolls_back_every_owned_record(self):
        self.connection.execute("CREATE TRIGGER reject_forget BEFORE DELETE ON exec_plans BEGIN SELECT RAISE(ABORT,'failure'); END")
        self.connection.execute("INSERT INTO execplan_drafts VALUES ('/repo','G-001',1,'{}')")
        self.connection.commit()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertIsNotNone(self.connection.execute("SELECT 1 FROM execplan_drafts WHERE plan_id='G-001'").fetchone())
        self.assertEqual(self.connection.execute('SELECT count(*) FROM deleted_plan_ids').fetchone()[0], 0)

    def test_reference_requires_explicit_current_goal_judgment(self):
        references = [SimpleNamespace(plan_id='G-002', source='docs/plans/[G-001] Old.md')]
        text = self.fixture._text('G-002', 'Dependency')
        documents = {'G-002': SimpleNamespace(path='docs/plans/[G-002] Dependency.md', text=text)}
        self.assertIsNone(resolved_references('G-001', references, documents))
        decision = {'deleted_id': 'G-001', 'outcome': 'obsolete', 'replacement_ids': [],
                    'sources': ['docs/PRODUCT.md'], 'reason': 'User withdrew this goal.'}
        documents['G-002'].text += '\n## Decision Log\nDEPENDENCY_RECOVERY: ' + json.dumps(decision) + '\n'
        self.assertEqual(resolved_references('G-001', references, documents), {'G-002': []})
        documents['G-002'].text = documents['G-002'].text.replace('depends_on: []', 'depends_on:\n  - G-001')
        self.assertIsNone(resolved_references('G-001', references, documents))

    def test_cleanup_requires_closed_pr_remote_absence_and_current_revision(self):
        from contextlib import contextmanager
        from unittest import mock
        from loop_manager_internal.catalog.deleted_plan_cleanup import cleanup
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
        self.connection.execute("UPDATE exec_plans SET worktree=NULL,branch='loop-manager/G-001',pr_url='https://example/pr/1',current_revision_sha=? WHERE plan_id='G-001'", ('a'*40,))
        self.connection.commit()
        pinned = SimpleNamespace(regular_paths=set(), revision_sha='b'*40,
            snapshot=SimpleNamespace(observed_ids={'G-002'}, all={}, withheld=()))
        @contextmanager
        def ownership(*args):
            yield SimpleNamespace(check=lambda: None)
        pr = {'state': 'CLOSED', 'mergedAt': None, 'url': 'https://example/pr/1', 'headRefName': 'loop-manager/G-001'}
        def git(_repo, *args, **kwargs):
            return SimpleNamespace(returncode=1 if args[0]=='rev-parse' else 0, stdout='')
        with mock.patch('loop_manager_internal.catalog.deleted_plan_cleanup.reserved_workspace_ownership', ownership), \
             mock.patch('loop_manager_internal.catalog.deleted_plan_cleanup.catalog.plan_content_hash', return_value=row['content_hash']):
            for state, current in [('OPEN', True), ('MERGED', True), ('CLOSED', False)]:
                with self.subTest(state=state, current=current):
                    self.assertEqual(cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                        git_bytes=lambda *a: b'original', pr_for_branch=lambda *a: {**pr, 'state': state},
                        state_dir=self.fixture.state_dir if hasattr(self.fixture,'state_dir') else '/tmp',
                        revision_is_current=lambda: current), ())
                    self.assertIsNotNone(self.connection.execute("SELECT 1 FROM exec_plans WHERE plan_id='G-001'").fetchone())
            from loop_manager_internal.storage import local_recovery_state
            local_recovery_state.ensure_schema(self.connection)
            self.connection.execute("INSERT INTO local_environment_recovery_requests "
                "(request_id,repo,status,before_state) VALUES ('shared','/repo','completed','{\"unknown\":1}')")
            self.connection.commit()
            self.assertEqual(cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                git_bytes=lambda *a: b'original', pr_for_branch=lambda *a: pr, state_dir='/tmp',
                revision_is_current=lambda: True), ())
            self.assertIsNotNone(self.connection.execute("SELECT 1 FROM exec_plans WHERE plan_id='G-001'").fetchone())
            self.connection.execute("UPDATE local_environment_recovery_requests SET before_state='{}'")
            self.connection.commit()
            self.assertEqual(cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                git_bytes=lambda *a: b'original', pr_for_branch=lambda *a: pr, state_dir='/tmp',
                revision_is_current=lambda: True), ('G-001',))

    def test_usage_and_invocation_children_are_erased_together(self):
        from loop_manager_internal.storage.task_calls import CallDeferred
        self.connection.execute('''INSERT INTO agent_invocations
            (invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at)
            VALUES ('call','/repo','G-001','worker','codex','model','high','start',1,'now')''')
        self.connection.execute("INSERT INTO targeted_test_collections(invocation_id,directory,device,inode,receipt) VALUES ('call','directory','1','2','receipt')")
        self.connection.execute("INSERT INTO targeted_test_run_owners VALUES ('run','call')")
        self.connection.commit()
        for assignment in (None, "finished_at='collected'",
                           "execution_status='executed',execution_started_at='started',execution_finished_at='stopped'",
                           "execution_status='not_executed'"):
            with self.subTest(assignment=assignment):
                if assignment:
                    self.connection.execute(f'UPDATE agent_invocations SET {assignment}')
                    self.connection.commit()
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("UPDATE agent_invocations SET execution_status='executed',execution_finish_evidence='provider_process_finished'")
        self.connection.commit()
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        for table in ('agent_invocations', 'targeted_test_collections', 'targeted_test_run_owners'):
            self.assertEqual(self.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_rowless_erasure_waits_for_reservation_return_and_preserves_independent_call(self):
        from pathlib import Path
        from loop_manager_internal.storage import task_calls
        task_calls.ensure_schema(self.connection)
        target = task_calls.reserve(self.connection, Path('/unused'), '/repo', 'G-001', 'verify', 'target-run')
        other = task_calls.reserve(self.connection, Path('/unused'), '/repo', 'G-002', 'verify', 'other-run')
        self.connection.execute("DELETE FROM exec_plans WHERE plan_id='G-001'")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaises(task_calls.CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001', artifacts=('/unused/logs/target',))
        self.assertEqual(tuple(self.connection.iterdump()), before)
        independent = dict(self.connection.execute('SELECT * FROM task_call_reservations WHERE token=?', (other.token,)).fetchone())
        task_calls.release(target)
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM task_call_reservations')], [independent])
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))

    def test_confirmed_non_execution_can_be_erased_without_physical_finish_time(self):
        self.connection.execute('''INSERT INTO agent_invocations
            (invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at,finished_at,execution_status)
            VALUES ('not-started','/repo','G-001','worker','codex','model','high','start',1,'prepared','rejected','not_executed')''')
        self.connection.commit()
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertIsNone(self.connection.execute('SELECT * FROM agent_invocations').fetchone())

    def test_confirmed_erasure_removes_target_from_native_usage_summary_only(self):
        from loop_manager_internal.storage.agent_invocation_state import InvocationRepository
        ledger = InvocationRepository(self.connection)
        for call, repo, plan, tokens in (('target', '/repo', 'G-001', 100),
                                         ('independent', '/repo', 'G-002', 200),
                                         ('foreign', '/other', 'G-001', 300)):
            self.connection.execute('INSERT INTO agent_invocations '
                '(invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at,'
                'finished_at,execution_status,execution_started_at,execution_finished_at,execution_finish_evidence,input_tokens) '
                "VALUES (?,?,?,'worker','codex','model','high','start',1,'prepared','collected','executed',"
                "'started','stopped','provider_process_finished',?)", (call, repo, plan, tokens))
        self.connection.commit()
        independent = [dict(row) for row in ledger.summary('/repo') if row['plan_id'] == 'G-002']
        foreign = [dict(row) for row in ledger.summary('/other')]
        self.assertEqual(sum(row['input_tokens'] for row in ledger.summary('/repo')), 300)
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        current = [dict(row) for row in ledger.summary('/repo')]
        self.assertEqual(current, independent)
        self.assertEqual(sum(row['input_tokens'] for row in current), 200)
        self.assertEqual([dict(row) for row in ledger.summary('/other')], foreign)

    def test_file_queue_survives_failure_and_retries_without_task_row(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from unittest import mock
        from loop_manager_internal.catalog.deleted_plan_cleanup import remove_pending_files
        with TemporaryDirectory() as directory:
            artifact = Path(directory) / 'logs' / 'task' / 'worker.log'
            artifact.parent.mkdir(parents=True)
            artifact.write_text('private task log')
            deleted_plan_state.forget(self.connection, '/repo', 'G-001', artifacts=(artifact,))
            with mock.patch.object(Path, 'unlink', side_effect=OSError('locked')):
                with self.assertRaises(OSError):
                    remove_pending_files(self.connection, '/repo', directory)
            self.assertEqual(self.connection.execute('SELECT count(*) FROM deleted_plan_files').fetchone()[0], 1)
            remove_pending_files(self.connection, '/repo', directory)
            self.assertFalse(artifact.exists())
            self.assertEqual(self.connection.execute('SELECT count(*) FROM deleted_plan_files').fetchone()[0], 0)
            self.connection.execute('INSERT INTO deleted_plan_files VALUES (?,?,?)', ('/repo','G-001',str(Path(directory)/'outside')))
            self.connection.commit()
            with self.assertRaises(ValueError):
                remove_pending_files(self.connection, '/repo', directory)

    def test_closed_pr_takes_precedence_over_deleted_branch_block(self):
        from pathlib import Path
        from unittest import mock
        from loop_manager_internal.recovery.waiting_pr_service import observe_waiting_pull_requests
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED', blocked_reason='remote_branch_deleted',branch='loop-manager/G-001',pr_url='https://example/pr/1' WHERE plan_id='G-001'")
        self.connection.commit()
        block = mock.Mock()
        effects = SimpleNamespace(pr_for_branch=lambda *a: {'state':'CLOSED','headRefName':'loop-manager/G-001',
            'baseRefName':'main','url':'https://example/pr/1'}, _block_closed_pull_request=block,
            git=mock.Mock(side_effect=AssertionError('closed PR must not query or recreate a branch')))
        observe_waiting_pull_requests(effects, self.connection, Path('/repo'), Path('/tmp'), '/repo')
        block.assert_called_once()

    def test_cleanup_journal_reuses_only_held_task_and_workspace_leases(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest import mock
        from loop_manager_internal.runtime import resource_lock
        with TemporaryDirectory() as directory:
            state = Path(directory)
            target = str(state / 'G-001')
            called = mock.Mock(return_value='removed')
            guarded = resource_lock.journal_resource(called)
            with resource_lock.resource_locks(state, '/repo', ['plan:G-001', 'git:' + target],
                                              owner='workspace-reservation-recovery'):
                with mock.patch.object(resource_lock, 'resource_locks', side_effect=AssertionError('double acquisition')):
                    self.assertEqual(guarded(state, repo='/repo', target=target,
                        kind='managed_worktree_remove', owner='plan:G-001'), 'removed')
            with resource_lock.resource_locks(state, '/repo', ['git:' + target], owner='workspace-reservation-recovery'):
                with mock.patch.object(resource_lock, 'resource_locks', side_effect=ValueError('plan lease required')):
                    with self.assertRaisesRegex(ValueError, 'plan lease required'):
                        guarded(state, repo='/repo', target=target, kind='managed_worktree_remove', owner='plan:G-001')

    def test_immutable_validation_audits_allow_only_explicit_task_erasure(self):
        import sqlite3
        from loop_manager_internal.storage import validation_feedback_state
        validation_feedback_state.ensure_schema(self.connection)
        self.connection.execute("UPDATE exec_plans SET status='READY' WHERE plan_id='G-002'")
        for key in ('G-001', 'G-002'):
            self.connection.execute('''INSERT INTO validation_results
                (repo,plan_id,stage,classification,raw_output,identities,created_at)
                VALUES ('/repo',?,'verify','failed','output','[]','now')''', (key,))
        self.connection.execute("INSERT INTO common_defect_reports VALUES (1,'{}',NULL)")
        self.connection.execute("INSERT INTO common_defect_links(repo,result_id,goal_key,correction_plan_id,reviewed_revision,confirmed_revision,evidence) VALUES ('/repo',1,'goal','G-002','old','new','{}')")
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM validation_results WHERE plan_id='G-001'")
        self.connection.rollback()
        from loop_manager_internal.storage.task_calls import CallDeferred
        before = tuple(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)
        # The independent correction has now actually completed; its own audit stays.
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-002'")
        self.connection.commit()
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual([r[0] for r in self.connection.execute('SELECT plan_id FROM validation_results')], ['G-002'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM common_defect_reports').fetchone()[0], 0)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM common_defect_links').fetchone()[0], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM validation_results WHERE plan_id='G-002'")
        self.connection.rollback()

    def test_release_ignores_global_causes_outside_dependents_but_keeps_own_cause(self):
        from loop_manager_internal.catalog.deleted_plan_cleanup import release_reference_holds
        path = 'docs/plans/[G-999] Deleted.md'
        causes = [{'plan_id':'G-002', 'code':'missing-plan-reference', 'source':path},
                  {'plan_id':'G-888', 'code':'missing-plan-reference', 'source':'unrelated'}]
        self.connection.execute('INSERT INTO execplan_catalog_holds(repo,plan_id,revision_sha,evidence) VALUES (?,?,?,?)',
                                ('/repo','G-002','sha',json.dumps({'causes':causes})))
        release_reference_holds(self.connection, '/repo', 'G-999', path, {'G-002':[]})
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_catalog_holds WHERE plan_id='G-002'").fetchone())
        causes.append({'plan_id':'G-002', 'code':'missing-source', 'source':'required.md'})
        self.connection.execute('INSERT INTO execplan_catalog_holds(repo,plan_id,revision_sha,evidence) VALUES (?,?,?,?)',
                                ('/repo','G-002','sha',json.dumps({'causes':causes})))
        release_reference_holds(self.connection, '/repo', 'G-999', path, {'G-002':[]})
        saved=json.loads(self.connection.execute("SELECT evidence FROM execplan_catalog_holds WHERE plan_id='G-002'").fetchone()[0])
        self.assertEqual(saved['causes'], [causes[-1]])
