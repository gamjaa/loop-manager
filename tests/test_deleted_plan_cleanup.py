"""Task deletion must erase owned history without touching another task."""
import json
from types import SimpleNamespace
import unittest

from loop_manager_internal.catalog.deleted_plan_cleanup import resolved_references
from loop_manager_internal.storage import deleted_plan_state, execplan_catalog_state
from tests.support.catalog import catalog_reconciliation_fixture


class DeletedPlanCleanupTest(unittest.TestCase):
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
            self.assertEqual(cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                git_bytes=lambda *a: b'original', pr_for_branch=lambda *a: pr, state_dir='/tmp',
                revision_is_current=lambda: True), ('G-001',))

    def test_usage_and_invocation_children_are_erased_together(self):
        self.connection.execute('''INSERT INTO agent_invocations
            (invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at)
            VALUES ('call','/repo','G-001','worker','codex','model','high','start',1,'now')''')
        self.connection.execute("INSERT INTO targeted_test_collections(invocation_id,directory,device,inode,receipt) VALUES ('call','directory','1','2','receipt')")
        self.connection.execute("INSERT INTO targeted_test_run_owners VALUES ('run','call')")
        self.connection.commit()
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        for table in ('agent_invocations', 'targeted_test_collections', 'targeted_test_run_owners'):
            self.assertEqual(self.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

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
