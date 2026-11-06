"""Task deletion must erase owned history without touching another task."""
import json
from types import SimpleNamespace
import unittest

from loop_manager_internal.catalog.deleted_plan_cleanup import resolved_references
from loop_manager_internal.storage import deleted_plan_state, execplan_catalog_state
from tests.support.catalog import catalog_reconciliation_fixture
from tests.support.diagnostic import ArtifactRecoveryFixture


class DeletedPlanCleanupTest(unittest.TestCase):
    def manual_history(self, repo='/repo', plan_id='G-001', *, members=None, owner=None):
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        store = ManualWorkRepository(self.connection)
        members = members or (plan_id,)
        group = store.create(repo, 'start', 'manual-' + plan_id, [], 'head', 'base', members=members,
            documents=tuple({'path': key + '.md', 'text': 'original ' + key, 'plan': {'id': key}} for key in members))
        group = store.update(group, owner_id=owner or plan_id)
        request = store.accept(group, head='head', base='base', input_token='token')
        group = store.get(repo, group['group_id'])
        store.record(group, 'verify', 'head', 'base', 'token', 'FAIL', 'saved failure')
        group = store.update(group, state='CLOSED')
        self.connection.commit()
        return store, group, request

    def test_erasure_removes_released_exclusive_manual_history_only(self):
        store, target, _request = self.manual_history()
        self.manual_history('/repo', 'G-002')
        self.manual_history('/other', 'G-001')
        tables = ('manual_work_groups', 'manual_work_members', 'manual_work_requests', 'manual_work_results')
        retained = {table: [dict(row) for row in self.connection.execute(
            f'SELECT * FROM {table} WHERE repo<>? OR group_id<>? ORDER BY rowid', ('/repo', target['group_id']))]
            for table in tables}
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        for table in tables:
            self.assertEqual([dict(row) for row in self.connection.execute(f'SELECT * FROM {table} ORDER BY rowid')],
                             retained[table])
        self.assertIsNone(store.for_plan('/repo', 'G-001'))
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')

    def test_erasure_prunes_released_shared_manual_history_and_preserves_request(self):
        store, group, request = self.manual_history(members=('G-001', 'G-002'), owner='G-002')
        documents = json.loads(group['documents'])
        retained_results = [dict(row) for row in self.connection.execute(
            "SELECT * FROM manual_work_results WHERE plan_id='G-002'")]
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        current = dict(store.get('/repo', group['group_id']))
        self.assertEqual(current, {**dict(group), 'members': json.dumps(['G-002']),
            'documents': json.dumps(documents[1:], ensure_ascii=False)})
        self.assertEqual(dict(store.request(current)), dict(request))
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM manual_work_results')], retained_results)
        self.assertIsNone(store.for_plan('/repo', 'G-001'))
        self.assertEqual(dict(store.for_plan('/repo', 'G-002')), current)
        self.assertEqual(self.connection.execute("SELECT status FROM exec_plans WHERE plan_id='G-002'").fetchone()[0], 'MERGED')
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(dict(store.get('/repo', group['group_id'])), current)

    def test_shared_manual_erasure_preserves_dependencies_owner_and_atomic_rollback(self):
        import sqlite3
        from loop_manager_internal.storage.task_calls import CallDeferred
        _store, group, _request = self.manual_history(members=('G-001', 'G-002'), owner='G-002')
        for assignment, value in (('owner_id', 'G-001'), ('documents', json.dumps([
                {'plan': {'id': 'G-001'}}, {'plan': {'id': 'G-002', 'depends_on': ['G-001']}}]))):
            with self.subTest(field=assignment):
                self.connection.execute(f'UPDATE manual_work_groups SET {assignment}=? WHERE group_id=?',
                                        (value, group['group_id']))
                before = list(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(list(self.connection.iterdump()), before)
                self.connection.rollback()
        # A stale target snapshot cannot be forgotten just because membership
        # was already removed by an older runtime.
        self.connection.execute('UPDATE manual_work_groups SET members=? WHERE group_id=?',
                                (json.dumps(['G-002']), group['group_id']))
        self.connection.execute("DELETE FROM manual_work_members WHERE plan_id='G-001'")
        before = list(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)
        self.connection.rollback()
        self.connection.execute("CREATE TRIGGER interrupt_shared_manual_erasure BEFORE DELETE ON exec_plans "
            "BEGIN SELECT RAISE(ABORT,'later shared deletion failed'); END")
        self.connection.commit()
        before = list(self.connection.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'later shared deletion failed'):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_unconfirmed_or_shared_manual_history_keeps_final_erasure_pending(self):
        from loop_manager_internal.storage.task_calls import CallDeferred
        _store, group, request = self.manual_history()
        scenarios = (
            ("UPDATE manual_work_groups SET state='AUTOMATIC' WHERE group_id=?", (group['group_id'],)),
            ("UPDATE manual_work_groups SET state='MERGED' WHERE group_id=?", (group['group_id'],)),
            ("UPDATE manual_work_groups SET branch='owned' WHERE group_id=?", (group['group_id'],)),
            ("UPDATE manual_work_groups SET documents='{}' WHERE group_id=?", (group['group_id'],)),
            ("UPDATE manual_work_groups SET members='[]',owner_id=NULL WHERE group_id=?", (group['group_id'],)),
            ("UPDATE manual_work_results SET plan_id='G-002' WHERE group_id=?", (group['group_id'],)),
            ("UPDATE manual_work_requests SET repo='/other' WHERE request_id=?", (request['request_id'],)),
            ("INSERT INTO manual_work_publications(repo,group_id,pr_url,head,base_sha,evidence) VALUES ('/repo',?,'pr','head','base','proof')",
             (group['group_id'],)),
        )
        for sql, parameters in scenarios:
            with self.subTest(sql=sql):
                self.connection.execute(sql, parameters)
                before = list(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(list(self.connection.iterdump()), before)
                self.connection.rollback()

    def test_manual_erasure_rolls_back_when_later_task_deletion_fails(self):
        import sqlite3
        self.manual_history()
        self.connection.execute("CREATE TRIGGER interrupt_manual_erasure BEFORE DELETE ON exec_plans "
            "BEGIN SELECT RAISE(ABORT,'later deletion failed'); END")
        self.connection.commit()
        before = list(self.connection.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'later deletion failed'):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_shared_manual_membership_and_cross_group_request_are_preserved(self):
        from loop_manager_internal.storage.task_calls import CallDeferred
        _store, target, request = self.manual_history()
        _store, other, _other_request = self.manual_history('/repo', 'G-002')
        self.connection.execute("DELETE FROM manual_work_members WHERE repo='/repo' AND plan_id='G-002'")
        self.connection.execute("INSERT INTO manual_work_members VALUES ('/repo','G-002',?)", (target['group_id'],))
        self.connection.execute('UPDATE manual_work_groups SET members=? WHERE group_id=?',
                                (json.dumps(['G-001', 'G-002']), target['group_id']))
        before = list(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)
        self.connection.rollback()
        self.connection.execute('UPDATE manual_work_groups SET request_id=? WHERE group_id=?',
                                (request['request_id'], other['group_id']))
        before = list(self.connection.iterdump())
        with self.assertRaisesRegex(CallDeferred, '독립 묶음'):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)

    def rowless_cancellation(self, *, started=False, empty_checkpoint=False, blocked=False,
                             empty_blocked_checkpoint=False, resolved_blocked_checkpoint=False,
                             requested_blocked_checkpoint=False, native_block=False, checkpoint_snapshot=None, owned_workspace=None, published_blocked=False, checkpoint_builder=None, withdrawn=True, publication_oid=None):
        from loop_manager_internal.catalog.execplan_catalog import plan_content_hash
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        from loop_manager_internal.planning.planning_catalog import PlanDeletion
        from loop_manager_internal.storage.planning_publication_state import apply_mutable_catalog_changes, _MUTABLE_READY_EVIDENCE_FIELDS
        from loop_manager_internal.storage.runtime_state import _transaction
        text = self.fixture._text('G-001', 'Gone')
        self.connection.execute('UPDATE exec_plans SET ' + ','.join(field + '=NULL' for field in _MUTABLE_READY_EVIDENCE_FIELDS)
            + ",status='READY',ever_started=0,workspace_owned=0,retry_count=0,pr_conflict_recovery=0 WHERE plan_id='G-001'")
        self.connection.execute('UPDATE projects SET phase=NULL,plan_path=NULL,branch=NULL,worktree=NULL,worker_session_id=NULL,pr_url=NULL')
        self.connection.execute("UPDATE exec_plans SET path='docs/plans/[G-001] Gone.md',title='Gone',"
            "priority='normal',content_hash=?,plan_sha=?,current_revision_sha=? WHERE plan_id='G-001'",
            (plan_content_hash(text), 'a' * 40, 'a' * 40))
        if started:
            self.connection.execute("UPDATE exec_plans SET ever_started=1,retry_count=2,worker_session_id='old-session',"
                "worker_result='saved result',verify_result='saved failure',last_error='historical failure',"
                "verify_failure_history='{}' WHERE plan_id='G-001'")
        if blocked:
            self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED',blocked_reason='saved block' WHERE plan_id='G-001'")
        self.connection.commit()
        original_block = None
        if owned_workspace is not None:
            self.connection.execute("UPDATE exec_plans SET branch='loop-manager/G-001',worktree=?,workspace_owned=1 WHERE plan_id='G-001'",
                                    (str(owned_workspace),))
            self.connection.commit()
        if native_block:
            from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
            RuntimeStateRepository(self.connection).block_plan('/repo', 'G-001', reason_code='plan_input_invalid',
                evidence='saved native block', release_fingerprint='c' * 64, enqueue=False)
            original_block = dict(self.connection.execute("SELECT * FROM execplan_blocks WHERE plan_id='G-001'").fetchone())
        if published_blocked:
            from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
            from loop_manager_internal.recovery.blocked_checkpoint import PreparedCheckpoint, checkpoint_identity
            checkpoints = BlockedCheckpointRepository(self.connection)
            request = checkpoints.get('/repo', 'G-001')
            identity = checkpoint_identity(plan_id='G-001', reason_code=request.reason_code, evidence=request.evidence,
                parent_oid='a' * 40, tree_oid='c' * 40, included_paths=())
            checkpoints.record_prepared(request, PreparedCheckpoint('a' * 40, 'c' * 40, identity, 'checkpoint', ()))
            checkpoints.record_remote_observation(request, None)
            checkpoints.record_local(request, 'b' * 40)
            checkpoints.finish_published(request, 'b' * 40)
            checkpoints.record_rebased_publication(request, 'e' * 40, 'f' * 40)
        if publication_oid is not None:
            self.connection.execute("UPDATE exec_plans SET publication_remote_oid=? WHERE plan_id='G-001'", (publication_oid,))
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
        plan = ReadyPlanSnapshot('G-001', row['path'], row['title'], tuple(json.loads(row['sources'])), (),
            'low', 'low', row['priority'], row['content_hash'], 'a' * 40, text,
            current_complexity=row['current_complexity'], current_risk=row['current_risk'], current_metadata_recorded=True)
        if started:
            from dataclasses import replace
            from loop_manager_internal.planning.planner_validation import planning_execution_state
            plan = replace(plan, execution_state=planning_execution_state(row))
        judgment = {'decision': {'id': 'G-001', 'source': 'docs/PRODUCT.md', 'outcome': 'withdrawn',
            'reason': 'The original goal was withdrawn.'}} if withdrawn else None
        if empty_checkpoint:
            from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
            ExecutionCheckpointRepository(self.connection).record('/repo', 'G-001', expected_generation=0,
                operation=None, expected_head=None, target_base_oid=None, observed_remote_oid=None,
                snapshot=checkpoint_snapshot if checkpoint_snapshot is not None else {})
        if checkpoint_builder is not None:
            checkpoint_builder()
        if empty_blocked_checkpoint or resolved_blocked_checkpoint or requested_blocked_checkpoint:
            from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
            checkpoints = BlockedCheckpointRepository(self.connection)
            request = checkpoints.request(repo='/repo', plan_id='G-001', reason_code='no-resource',
                evidence='No owned workspace.', branch=None, worktree=None,
                expected_remote_oid=None, rebase_recovery=False)
            if resolved_blocked_checkpoint:
                self.assertIsNone(checkpoints.reconcile_cause(request))
            elif not requested_blocked_checkpoint:
                checkpoints.finish_unpublished(request, 'No workspace was created.', retryable=False)
            else:
                self.connection.commit()
        with _transaction(self.connection):
            apply_mutable_catalog_changes(self.connection, '/repo', deletions=(PlanDeletion(plan, judgment),), revisions=(),
                snapshot_token='saved', plan_commit='b' * 40, content_hash=lambda _: '', agent=False)
        audit = json.loads(self.connection.execute("SELECT recovery_evidence FROM execplan_cancellations WHERE plan_id='G-001'").fetchone()[0])
        self.assertEqual(audit['row'], dict(row))
        self.assertEqual(audit['text'], text)
        if native_block:
            self.assertEqual(audit['block'], original_block)
        return text

    def cleanup_rowless(self, text, *, git=None, pr=None, current=lambda: True, documents=None):
        from tempfile import TemporaryDirectory
        from unittest import mock
        from loop_manager_internal.catalog.deleted_plan_cleanup import cleanup
        pinned = SimpleNamespace(regular_paths=set(), revision_sha='c' * 40,
            snapshot=SimpleNamespace(observed_ids={'G-002'}, all=documents or {}, withheld=()))
        def observe(_repo, *args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            return SimpleNamespace(returncode=1 if args[0] == 'rev-parse' else 0, stdout='')
        with TemporaryDirectory() as state:
            return cleanup(self.connection, '/repo', '/repo', pinned, (), git=git or observe,
                git_bytes=lambda *_: text.encode(), pr_for_branch=pr or mock.Mock(return_value=None),
                state_dir=state, revision_is_current=current)

    def test_plain_unstarted_remote_deletion_keeps_general_cancellation_audit(self):
        from unittest import mock
        text = self.rowless_cancellation(withdrawn=False)
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.assertIsNotNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())
        self.assertIsNone(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone())

    def test_unstarted_final_erasure_requires_published_recovery_for_live_dependents(self):
        text = self.rowless_cancellation(withdrawn=False)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        audit['affected'] = ['G-002']
        self.connection.execute("UPDATE exec_plans SET status='READY' WHERE plan_id='G-002'")
        self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.assertEqual(self.cleanup_rowless(text, documents=self.published_withdrawal()), ('G-001',))
        self.assertIsNotNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone())
        self.assertIsNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())

    def published_withdrawal(self):
        path = 'docs/plans/[G-002] Dependency.md'
        decision = dict(deleted_id='G-001', outcome='obsolete', replacement_ids=[], sources=['docs/PRODUCT.md'],
                        reason='The withdrawn goal is no longer needed.')
        text = self.fixture._text('G-002', 'Dependency') + '\n## Decision Log\nDEPENDENCY_RECOVERY: ' + json.dumps(decision) + '\n'
        return {'G-002': SimpleNamespace(path=path, text=text)}

    def test_started_remote_cancellation_erases_from_published_recovery_without_judgment(self):
        text = self.rowless_cancellation(started=True, blocked=True, withdrawn=False)
        self.artifact_call('finished-call', '/repo', 'G-001')
        self.connection.execute("UPDATE agent_invocations SET input_tokens=123 WHERE invocation_id='finished-call'")
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        self.assertIsNone(audit.pop('judgment'))
        audit['affected'] = ['G-002']
        self.connection.execute("UPDATE exec_plans SET status='READY' WHERE plan_id='G-002'")
        self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
        self.connection.execute("CREATE TRIGGER reject_started_recovery_resurrection BEFORE INSERT ON exec_plans "
            "WHEN NEW.plan_id='G-001' BEGIN SELECT RAISE(ABORT,'execution resurrection'); END")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        documents = self.published_withdrawal()
        entry = documents['G-002']
        invalid = {'G-002': SimpleNamespace(path=entry.path, text=entry.text.replace('"obsolete"', '"unknown"'))}
        self.assertEqual(self.cleanup_rowless(text, documents=invalid), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("UPDATE agent_invocations SET execution_finish_evidence=NULL WHERE invocation_id='finished-call'")
        self.connection.commit()
        unfinished = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text, documents=documents), ())
        self.assertEqual(tuple(self.connection.iterdump()), unfinished)
        self.connection.execute("UPDATE agent_invocations SET execution_finish_evidence='provider_process_finished' WHERE invocation_id='finished-call'")
        self.connection.commit()
        independent = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone())
        self.assertEqual(self.cleanup_rowless(text, documents=documents), ('G-001',))
        for table in ('exec_plans', 'execplan_cancellations', 'agent_invocations'):
            self.assertIsNone(self.connection.execute(f"SELECT 1 FROM {table} WHERE plan_id='G-001'").fetchone())
        self.assertEqual(dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()), independent)
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
        self.assertEqual(self.cleanup_rowless(text, documents=documents), ())

    def test_complete_unstarted_cancellation_can_finish_without_recreating_execution_row(self):
        text = self.rowless_cancellation(empty_checkpoint=True, checkpoint_snapshot=dict(
            worktree_recovery_pending=False, branch='', worktree='', stored_active_sha=''))
        self.connection.execute("CREATE TRIGGER reject_rowless_resurrection BEFORE INSERT ON exec_plans "
            "WHEN NEW.plan_id='G-001' BEGIN SELECT RAISE(ABORT,'execution resurrection'); END")
        self.connection.commit()
        independent = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone())
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        self.assertIsNone(self.connection.execute("SELECT * FROM execplan_cancellations WHERE plan_id='G-001'").fetchone())
        self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
        self.assertEqual(dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()), independent)
        self.assertEqual(self.cleanup_rowless(text), ())

    def test_pre_registration_hold_finishes_from_published_current_reference_recovery(self):
        from loop_manager_internal.catalog import execplan_catalog as catalog, deleted_plan_cleanup, remote_deletions
        from tempfile import TemporaryDirectory
        text = self.rowless_cancellation()
        canceled = self.connection.execute('SELECT * FROM execplan_cancellations').fetchone()
        original = json.loads(canceled['recovery_evidence'])['row']
        held_evidence = dict(path=original['path'], content_hash=original['content_hash'],
            deleted_goal=dict(path=original['path'], original_revision='a' * 40, observed_revision='b' * 40,
                last_observed_revision='c' * 40, text=text, row=original, affected=['G-002'], resolved={},
                unknown_reason='The pre-registration goal needs semantic confirmation.'))
        self.connection.execute('DELETE FROM execplan_cancellations')
        self.connection.execute('INSERT INTO execplan_catalog_holds VALUES (?,?,?,?,NULL)',
            ('/repo', 'G-001', 'c' * 40, json.dumps(held_evidence)))
        recovery_text = self.fixture._text('G-002', 'Dependency') + '\n## Decision Log\nDEPENDENCY_RECOVERY: ' + json.dumps(
            dict(deleted_id='G-001', outcome='obsolete', replacement_ids=[], sources=['docs/PRODUCT.md'],
                 reason='The obsolete prerequisite goal was withdrawn.')) + '\n'
        self.connection.execute("UPDATE exec_plans SET status='READY',content_hash=?,path=?,sources=? WHERE plan_id='G-002'",
            (catalog.plan_content_hash(recovery_text), 'docs/plans/[G-002] Dependency.md', '["docs/PRODUCT.md"]'))
        self.connection.commit()
        pinned = SimpleNamespace(regular_paths=set(), revision_sha='c' * 40,
            snapshot=SimpleNamespace(observed_ids={'G-002'}, all={
                'G-002': SimpleNamespace(path='docs/plans/[G-002] Dependency.md', text=recovery_text)}, withheld=()))
        def observe(_repo, *args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            return SimpleNamespace(returncode=int(args[0] == 'rev-parse'), stdout='')
        with TemporaryDirectory() as state:
            def run(pr=None):
                return deleted_plan_cleanup.cleanup(self.connection, '/repo', '/repo', pinned, (), git=observe,
                    git_bytes=lambda *_: text.encode(), pr_for_branch=pr or (lambda *_: None),
                    state_dir=state, revision_is_current=lambda: True)
            before = list(self.connection.iterdump())
            self.assertEqual(run(), ())  # No semantic decision has been published yet.
            self.assertEqual(list(self.connection.iterdump()), before)
            goal = dict(id='G-001', path=original['path'], kind='missing-plan', affected_ids=['G-002'])
            remote_deletions.resolve_recovery(self.connection,
                SimpleNamespace(repo='/repo', recovery_goals=(json.dumps(goal),), by_id={'G-002': None}), 'c' * 40, {'G-002'})
            self.connection.commit()
            published = list(self.connection.iterdump())
            self.assertEqual(run(pr=lambda *_: {'state': 'OPEN'}), ())
            self.assertEqual(list(self.connection.iterdump()), published)
            def change_dependent(*_args):
                self.connection.execute("UPDATE exec_plans SET title='concurrent declaration' WHERE plan_id='G-002'")
                self.connection.commit()
            self.assertEqual(run(pr=change_dependent), ())
            self.assertEqual(self.connection.execute("SELECT title FROM exec_plans WHERE plan_id='G-002'").fetchone()[0],
                             'concurrent declaration')
            self.assertIsNone(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone())
            self.connection.execute("UPDATE exec_plans SET title='Dependency' WHERE plan_id='G-002'")
            self.connection.commit()
            # A post-publication declaration race invalidates the exact approval.
            self.connection.execute("UPDATE exec_plans SET content_hash='changed' WHERE plan_id='G-002'")
            self.connection.commit()
            changed = list(self.connection.iterdump())
            self.assertEqual(run(), ())
            self.assertEqual(list(self.connection.iterdump()), changed)
            self.connection.execute("UPDATE exec_plans SET content_hash=? WHERE plan_id='G-002'",
                                    (catalog.plan_content_hash(recovery_text),))
            self.connection.commit()
            import hashlib
            from pathlib import Path
            copies = [Path(state) / 'worktrees' / hashlib.sha256(b'/repo').hexdigest()[:16] / folder / 'G-001'
                      for folder in ('document-revisions', 'document-assessments')]
            for root in copies:
                leaf = root / ('a' * 40)
                leaf.mkdir(parents=True)
                (leaf / 'saved-document.md').write_text('dedicated document copy')
            before_copies = list(self.connection.iterdump())
            self.assertEqual(run(), ())
            self.assertEqual(list(self.connection.iterdump()), before_copies)
            for root in copies:
                leaf = root / ('a' * 40)
                self.assertEqual((leaf / 'saved-document.md').read_text(), 'dedicated document copy')
                (leaf / 'saved-document.md').unlink()
                leaf.rmdir()
            self.assertEqual(run(), ('G-001',))
            self.assertTrue(all(not root.exists() for root in copies))
            self.assertIsNone(self.connection.execute("SELECT * FROM execplan_catalog_holds WHERE plan_id='G-001'").fetchone())
            self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())
            self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
            self.assertEqual(run(), ())

    def test_rowless_cancellation_keeps_legacy_resource_and_checkpoint_uncertainty(self):
        from unittest import mock
        text = self.rowless_cancellation()
        original = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        for audit in ({'text': text, 'row': {}},
                      {**original, 'row': {**original['row'], 'branch': 'owned'}},
                      {**original, 'checkpoints': {'execution_checkpoints': {'snapshot': 'saved'}}},
                      {**original, 'row': {**original['row'], 'retry_count': 1}},
                      {**original, 'row': {**original['row'], 'ever_started': 1}}):
            with self.subTest(audit=audit):
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                git = mock.Mock(side_effect=AssertionError('uncertain audit must not authorize external cleanup'))
                self.assertEqual(self.cleanup_rowless(text, git=git), ())
                self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(original),))
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        ExecutionCheckpointRepository(self.connection)
        self.connection.execute("INSERT INTO execution_checkpoints (repo,plan_id,generation,operation) VALUES ('/repo','G-001',1,'rebase')")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_rowless_remote_and_audit_races_preserve_cancellation(self):
        from unittest import mock
        text = self.rowless_cancellation()
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text, current=lambda: False), ())
        self.assertEqual(self.cleanup_rowless(text + '\nchanged'), ())
        self.assertEqual(self.cleanup_rowless(text, pr=mock.Mock(return_value={'state': 'OPEN'})), ())
        self.assertEqual(self.cleanup_rowless(text, pr=mock.Mock(side_effect=RuntimeError('query unavailable'))), ())
        def changed_history(_repo, *args, **kwargs):
            return SimpleNamespace(returncode=int(args[0] == 'merge-base' and args[2] == 'b' * 40), stdout='')
        self.assertEqual(self.cleanup_rowless(text, git=changed_history), ())
        def remote_present(_repo, *args, **kwargs):
            return SimpleNamespace(returncode=1 if args[0] == 'rev-parse' else 0,
                                   stdout='owned-ref' if args[0] == 'ls-remote' else '')
        self.assertEqual(self.cleanup_rowless(text, git=remote_present), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        for name, value in (('manual_plan_ownership', ('manual', True)),
                            ('publication_claims', (frozenset({'G-001'}), frozenset()))):
            with mock.patch('loop_manager_internal.storage.work_claims.' + name, return_value=value):
                self.assertEqual(self.cleanup_rowless(text), ())
                self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("UPDATE projects SET plan_path='docs/plans/[G-001] Old.md'")
        self.connection.commit()
        projected = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), projected)
        self.connection.execute('UPDATE projects SET plan_path=NULL')
        self.connection.commit()
        def replace_audit(*_args):
            self.connection.execute("UPDATE execplan_cancellations SET recovery_evidence='{}'")
            self.connection.commit()
        self.assertEqual(self.cleanup_rowless(text, pr=replace_audit), ())
        self.assertEqual(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0], '{}')
        self.assertIsNone(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone())

    def artifact_call(self, identity, repo, plan_id, receipt=None):
        self.connection.execute('INSERT INTO agent_invocations '
            '(invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at,finished_at,'
            'execution_status,execution_started_at,execution_finished_at,execution_finish_evidence,process_receipt_path,session_id) '
            "VALUES (?,?,?,'worker','codex','model','high','start',1,'prepared','collected','executed',"
            "'started','stopped','provider_process_finished',?,'shared-session')",
            (identity, repo, plan_id, str(receipt) if receipt else None))

    def test_artifact_selection_removes_only_owned_attempt_in_shared_nested_runtime(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths, remove_pending_files
        with TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = state / 'agent-runtime' / 'runtime'
            attempt = runtime / 'call-target'
            attempt.mkdir(parents=True)
            own = runtime / 'call-target.process.json'
            own.write_text('target process proof')
            (attempt / 'last-message.txt').write_text('target response')
            independent = runtime / 'nested' / 'call-other.process.json'
            independent.parent.mkdir()
            independent.write_text('independent process proof')
            self.artifact_call('target', '/repo', 'G-001', own)
            self.artifact_call('other', '/other', 'G-002', independent)
            self.connection.commit()
            preserved = dict(self.connection.execute("SELECT * FROM agent_invocations WHERE invocation_id='other'").fetchone())
            paths = artifact_paths(self.connection, '/repo', dict(plan_id='G-001', worktree=None), state)
            self.assertEqual(set(paths), {own, attempt})
            deleted_plan_state.forget(self.connection, '/repo', 'G-001', artifacts=paths)
            self.assertEqual(remove_pending_files(self.connection, '/repo', state), set())
            self.assertFalse(own.exists())
            self.assertFalse(attempt.exists())
            self.assertEqual(independent.read_text(), 'independent process proof')
            self.assertEqual(dict(self.connection.execute("SELECT * FROM agent_invocations WHERE invocation_id='other'").fetchone()), preserved)

    def test_same_receipt_alias_cannot_erase_another_repositorys_process_proof(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths
        from loop_manager_internal.storage.task_calls import CallDeferred
        with TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = state / 'agent-runtime' / 'runtime'
            (runtime / 'nested').mkdir(parents=True)
            receipt = runtime / 'call.process.json'
            receipt.write_text('shared proof')
            self.artifact_call('target', '/repo', 'G-001', receipt)
            self.artifact_call('other', '/other', 'G-001', runtime / 'nested' / '..' / receipt.name)
            self.connection.commit()
            before = tuple(self.connection.iterdump())
            with self.assertRaises(CallDeferred):
                deleted_plan_state.forget(self.connection, '/repo', 'G-001',
                    artifacts=artifact_paths(self.connection, '/repo', dict(plan_id='G-001', worktree=None), state))
            self.assertEqual(tuple(self.connection.iterdump()), before)
            self.assertEqual(receipt.read_text(), 'shared proof')

    def test_file_queue_rechecks_new_collection_owner_without_stalling_independent_cleanup(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths, remove_pending_files
        with TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = state / 'agent-runtime' / 'runtime'
            runtime.mkdir(parents=True)
            receipt = runtime / 'call.process.json'
            receipt.write_text('target proof')
            self.artifact_call('target', '/repo', 'G-001', receipt)
            self.connection.commit()
            paths = artifact_paths(self.connection, '/repo', dict(plan_id='G-001', worktree=None), state)
            self.assertEqual(paths, (runtime,))
            deleted_plan_state.forget(self.connection, '/repo', 'G-001', artifacts=paths)
            collection = runtime / 'independent-tests'
            collection.mkdir()
            (collection / 'receipt.json').write_text('independent collection proof')
            self.artifact_call('other', '/repo', 'G-002')
            self.connection.execute('INSERT INTO targeted_test_collections '
                "(invocation_id,directory,device,inode,receipt) VALUES ('other',?,'1','2','receipt')", (str(collection),))
            self.connection.commit()
            free = state / 'logs' / 'free.log'
            free.parent.mkdir()
            free.write_text('another deleted task')
            deleted_plan_state.forget(self.connection, '/repo', 'G-999', artifacts=(free,))
            queued = [tuple(row) for row in self.connection.execute("SELECT * FROM deleted_plan_files WHERE plan_id='G-001'")]
            self.assertEqual(remove_pending_files(self.connection, '/repo', state), {'G-001'})
            self.assertFalse(free.exists())
            self.assertEqual([tuple(row) for row in self.connection.execute('SELECT * FROM deleted_plan_files')], queued)
            self.assertEqual((collection / 'receipt.json').read_text(), 'independent collection proof')
            # A later ownership relocation supplies a fresh, disjoint path.
            relocated = state / 'logs' / 'independent-tests'
            collection.rename(relocated)
            self.connection.execute("UPDATE targeted_test_collections SET directory=? WHERE invocation_id='other'", (str(relocated),))
            self.connection.commit()
            self.assertEqual(remove_pending_files(self.connection, '/repo', state), set())
            self.assertFalse(runtime.exists())
            self.assertEqual((relocated / 'receipt.json').read_text(), 'independent collection proof')
            self.assertEqual(self.connection.execute('SELECT count(*) FROM deleted_plan_files').fetchone()[0], 0)

    def test_resource_free_started_ready_audit_finishes_after_call_termination(self):
        text = self.rowless_cancellation(started=True)
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.artifact_call('old-call', '/repo', 'G-001')
        self.connection.execute("UPDATE agent_invocations SET execution_finish_evidence=NULL WHERE invocation_id='old-call'")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("UPDATE agent_invocations SET execution_finish_evidence='provider_process_finished' WHERE invocation_id='old-call'")
        self.connection.execute("CREATE TRIGGER reject_started_rowless_resurrection BEFORE INSERT ON exec_plans "
            "WHEN NEW.plan_id='G-001' BEGIN SELECT RAISE(ABORT,'execution resurrection'); END")
        self.connection.commit()
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_cancellations WHERE plan_id='G-001'").fetchone())
        self.assertIsNone(self.connection.execute("SELECT 1 FROM agent_invocations WHERE invocation_id='old-call'").fetchone())
        self.assertIsNone(self.connection.execute("SELECT 1 FROM exec_plans WHERE plan_id='G-001'").fetchone())
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))

    def test_resource_free_blocked_audit_erases_only_after_call_handoff_and_termination(self):
        text = self.rowless_cancellation(started=True, blocked=True)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        self.assertEqual(audit['row']['status'], 'PLAN_BLOCKED')
        self.assertEqual(audit['row']['blocked_reason'], 'saved block')
        self.artifact_call('blocked-call', '/repo', 'G-001')
        self.connection.execute("UPDATE agent_invocations SET finished_at=NULL WHERE invocation_id='blocked-call'")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("UPDATE agent_invocations SET finished_at='collected',execution_finish_evidence=NULL WHERE invocation_id='blocked-call'")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("UPDATE agent_invocations SET execution_finish_evidence='provider_process_finished' WHERE invocation_id='blocked-call'")
        self.connection.execute("CREATE TRIGGER reject_blocked_rowless_resurrection BEFORE INSERT ON exec_plans "
            "WHEN NEW.plan_id='G-001' BEGIN SELECT RAISE(ABORT,'execution resurrection'); END")
        self.connection.commit()
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        for table in ('exec_plans', 'execplan_cancellations', 'agent_invocations'):
            self.assertIsNone(self.connection.execute(f"SELECT 1 FROM {table} WHERE plan_id='G-001'").fetchone())
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))

    def test_blocked_rowless_audit_does_not_authorize_merged_or_active_stage_cleanup(self):
        from unittest import mock
        text = self.rowless_cancellation(started=True, blocked=True)
        self.artifact_call('blocked-call', '/repo', 'G-001')
        self.connection.commit()
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        for status in ('IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING', 'MERGED'):
            with self.subTest(status=status):
                audit['row']['status'] = status
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                git = mock.Mock(side_effect=AssertionError('unconfirmed historical stage'))
                self.assertEqual(self.cleanup_rowless(text, git=git), ())
                self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_empty_checkpoint_audit_does_not_hold_resource_free_rowless_erasure(self):
        text = self.rowless_cancellation(empty_checkpoint=True)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        self.assertEqual(audit['checkpoints']['execution_checkpoints']['generation'], 1)
        self.assertEqual(audit['checkpoints']['execution_checkpoints']['snapshot'], '{}')
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execution_checkpoints WHERE plan_id='G-001'").fetchone())
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_cancellations WHERE plan_id='G-001'").fetchone())
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))

    def test_cancelled_document_copy_cleanup_reuses_native_assessment_and_resumes_after_physical_removal(self):
        self.document_copy_cleanup(active=False)

    def test_active_document_copy_cleanup_reuses_native_assessment_and_resumes_after_physical_removal(self):
        self.document_copy_cleanup(active=True)

    def test_nested_rebase_history_document_copy_uses_same_native_cleanup_and_recovery(self):
        self.document_copy_cleanup(active=False, historical=True)

    def document_copy_cleanup(self, *, active, historical=False):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import hashlib
        from loop_manager_internal.catalog.deleted_plan_cleanup import cleanup
        from loop_manager_internal.git.cleanup_service import assess_cleanup, AssessCleanupEffects
        with TemporaryDirectory() as directory:
            state = Path(directory)
            root = state / 'worktrees' / hashlib.sha256(b'/repo').hexdigest()[:16] / 'document-revisions' / 'G-001'
            workspace = root / ('a' * 40)
            workspace.mkdir(parents=True)
            (workspace / 'proposal.md').write_text('only this task document proposal')
            snapshot = dict(document_revision=dict(version=1, base='a' * 40, stage='prepared', phase='IMPLEMENTING',
                scope='execplan', document_only=True, workspace=str(workspace), commit='b' * 40))
            if historical:
                snapshot = {'rebase_operation_history': [snapshot]}
            text = self.rowless_cancellation(started=True, empty_checkpoint=True, checkpoint_snapshot=snapshot)
            if active:
                audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
                self.connection.execute('DELETE FROM execplan_cancellations')
                for table, values in (('exec_plans', audit['row']),
                                      ('execution_checkpoints', audit['checkpoints']['execution_checkpoints'])):
                    self.connection.execute('INSERT INTO ' + table + ' (' + ','.join(values) + ') VALUES ('
                        + ','.join('?' for _ in values) + ')', tuple(values.values()))
            self.artifact_call('doc-call', '/repo', 'G-001')
            self.connection.commit()
            original = list(self.connection.iterdump())
            own_log = state / 'logs' / hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
            own_log.mkdir(parents=True)
            (own_log / 'call.log').write_text('owned historical document log')
            independent_log = state / 'logs' / 'independent'
            independent_log.mkdir()
            (independent_log / 'call.log').write_text('independent log')
            dirty = True
            race = False
            removals, recoveries = [], []
            receipt = None
            def git(_repo, *args, **kwargs):
                self.assertFalse(self.connection.in_transaction)
                if args[0] == 'worktree':
                    output = ('worktree ' + str(workspace) + '\nHEAD ' + 'b' * 40 + '\ndetached\n') if workspace.exists() else ''
                elif args[0] == 'status':
                    output = ' M proposal.md' if dirty else ''
                elif args[0] == 'rev-parse' and '--git-common-dir' in args:
                    output = '/common'
                elif args[0] == 'rev-parse' and args[-1] == 'HEAD^{commit}':
                    output = 'b' * 40
                else:
                    output = ''
                return SimpleNamespace(returncode=int(args[0] == 'rev-parse' and args[-1].startswith('refs/heads/')), stdout=output)
            effects = AssessCleanupEffects(lambda _workspace: ('b' * 40, None, {}, {}), lambda _workspace: {}, git)
            def assess(path, head):
                nonlocal race
                result = assess_cleanup(effects, path, head)
                if race:
                    race = False
                    if active:
                        self.connection.execute("UPDATE execution_checkpoints SET snapshot=? WHERE plan_id='G-001'",
                            (json.dumps({**snapshot, 'concurrent': True}),))
                    else:
                        updated = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
                        self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?',
                            (json.dumps({**updated, 'concurrent': True}),))
                    self.connection.commit()
                return result
            from loop_manager_internal.git import manager_git_journal as journal
            before_removal = dict(exists=True, registration=dict(head='b' * 40, branch=None, locked=None), index='c' * 40)
            after_removal = dict(exists=False, registration=None, index=None)
            observe_removal = lambda: before_removal if workspace.exists() else after_removal
            def remove(repository, path, assessment, *, state_dir, owner):
                nonlocal receipt
                self.assertEqual((repository, path, assessment.pr_head_oid, state_dir, owner),
                    ('/repo', workspace, 'b' * 40, state, 'plan:G-001'))
                removals.append(path)
                def apply(token):
                    nonlocal receipt
                    (path / 'proposal.md').unlink()
                    path.rmdir()
                    receipt = token  # The native removal marker exists before result sealing.
                    raise RuntimeError('interrupted after physical removal and marker')
                with self.assertRaisesRegex(RuntimeError, 'interrupted after physical removal'):
                    journal.execute(state, owner=owner, repo='/repo', kind='managed_worktree_remove', target=str(path),
                        before=before_removal, expected=after_removal, observe=observe_removal,
                        apply=apply, marker=lambda intent: receipt == intent['id'])
                return SimpleNamespace(status='unverifiable')
            def recover(state_dir, repository, path):
                self.assertEqual((state_dir, repository, path), (state, '/repo', workspace))
                recoveries.append(path)
                return journal.recover_active(state, repo='/repo', kind='managed_worktree_remove', target=str(path),
                    observe=observe_removal, marker=lambda intent: receipt == intent['id'])
            pinned = SimpleNamespace(regular_paths=set(), revision_sha='c' * 40,
                snapshot=SimpleNamespace(observed_ids={'G-002'}, all=self.published_withdrawal() if active else {}, withheld=()))
            references = (SimpleNamespace(plan_id='G-002', source='docs/plans/[G-001] Gone.md'),) if active else ()
            def run():
                return cleanup(self.connection, '/repo', '/repo', pinned, references, git=git, git_bytes=lambda *_: text.encode(),
                    pr_for_branch=lambda *_: None, state_dir=state, revision_is_current=lambda: True,
                    assess_cleanup=assess, remove_cleanup_worktree=remove, recover_cleanup_worktree=recover)
            self.assertEqual(run(), ())
            self.assertEqual(removals, [])
            self.assertEqual(list(self.connection.iterdump()), original)
            self.assertEqual((workspace / 'proposal.md').read_text(), 'only this task document proposal')
            dirty = False
            if not active:
                self.connection.execute('UPDATE projects SET worktree=?', (str(workspace / '..' / workspace.name),))
                self.connection.commit()
                claimed = list(self.connection.iterdump())
                self.assertEqual(run(), ())
                self.assertEqual(removals, [])
                self.assertEqual(list(self.connection.iterdump()), claimed)
                self.connection.execute('UPDATE projects SET worktree=NULL')
                self.connection.commit()
            source_table, source_column = ('execution_checkpoints', 'snapshot') if active else ('execplan_cancellations', 'recovery_evidence')
            source = self.connection.execute('SELECT ' + source_column + ' FROM ' + source_table).fetchone()[0]
            race = True
            self.assertEqual(run(), ())
            self.assertEqual(removals, [])
            self.assertTrue(json.loads(self.connection.execute('SELECT ' + source_column + ' FROM ' + source_table).fetchone()[0])['concurrent'])
            self.assertTrue(workspace.exists())
            self.connection.execute('UPDATE ' + source_table + ' SET ' + source_column + '=?', (source,))
            self.connection.commit()
            self.connection.execute("UPDATE agent_invocations SET finished_at=NULL WHERE invocation_id='doc-call'")
            self.connection.commit()
            pending = list(self.connection.iterdump())
            self.assertEqual(run(), ())
            self.assertEqual(list(self.connection.iterdump()), pending)
            self.assertEqual(removals, [])
            self.connection.execute("UPDATE agent_invocations SET finished_at='collected' WHERE invocation_id='doc-call'")
            self.connection.commit()
            def interrupt_before_effect(_token):
                raise RuntimeError('interrupted before physical removal')
            with self.assertRaisesRegex(RuntimeError, 'interrupted before physical removal'):
                journal.execute(state, owner='plan:G-001', repo='/repo', kind='managed_worktree_remove',
                    target=str(workspace), before=before_removal, expected=after_removal, observe=observe_removal,
                    apply=interrupt_before_effect, marker=lambda _intent: False)
            self.assertTrue(journal.has_pending_project_mutation(state, '/repo'))
            self.assertEqual(run(), ())
            self.assertEqual(removals, [workspace])
            self.assertEqual(list(self.connection.iterdump()), original)
            self.assertFalse(workspace.exists())
            self.assertTrue(own_log.exists())
            self.assertEqual(run(), ('G-001',))
            self.assertEqual(removals, [workspace])
            self.assertEqual(recoveries, [workspace, workspace])
            self.assertFalse(journal.has_pending_project_mutation(state, '/repo'))
            self.assertFalse(root.exists())
            self.assertFalse(own_log.exists())
            self.assertEqual((independent_log / 'call.log').read_text(), 'independent log')
            self.assertIsNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())
            self.assertIsNone(self.connection.execute('SELECT * FROM agent_invocations').fetchone())
            self.assertEqual(run(), ())

    def test_document_assessment_and_superseded_revision_resources_require_native_identity(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import hashlib
        from loop_manager_internal.agents.scope_contracts import GitSnapshot
        from loop_manager_internal.catalog.deleted_document_cleanup import document_resources
        with TemporaryDirectory() as directory:
            managed = Path(directory).resolve() / 'worktrees' / hashlib.sha256(b'/repo').hexdigest()[:16]
            generation = hashlib.sha256(('a' * 40 + ':' + 'b' * 40 + ':2').encode()).hexdigest()
            workspace = managed / 'document-assessments' / 'G-001' / generation
            original = GitSnapshot('c' * 40, None, {}, {}, {}, {}, {}, {})
            item = dict(version=1, base='a' * 40, candidate='b' * 40, planner_generation=2,
                planner_workspace=str(workspace), planner_scope=dict(role='planner', worktree=str(workspace), before=original.serialize()),
                superseded=[dict(base='d' * 40, workspace=str(managed / 'document-revisions' / 'G-001' / ('d' * 40)), commit='e' * 40)])
            roots, paths = document_resources({'retired_document_assessment': item}, directory, '/repo', 'G-001')
            self.assertEqual(paths, {str(workspace): 'c' * 40, str(roots[0] / ('d' * 40)): 'e' * 40})
            self.assertEqual(document_resources({'rebase_operation_history': [{'retired_document_assessment': item}]}, directory, '/repo', 'G-001'), (roots, paths))
            for invalid in ({**item, 'version': 2}, {**item, 'planner_generation': 3},
                    {**item, 'planner_scope': {**item['planner_scope'], 'role': 'worker'}},
                    {**item, 'planner_scope': {**item['planner_scope'], 'before': '[]'}},
                    {**item, 'planner_workspace': str(workspace).replace('G-001', 'G-002')},
                    {**item, 'superseded': 'unknown'}):
                with self.subTest(invalid=invalid), self.assertRaises((ValueError, RuntimeError)):
                    document_resources({'document_revision': invalid}, directory, '/repo', 'G-001')

    def test_checkpoint_audit_with_ownership_or_unknown_fields_keeps_rowless_erasure_pending(self):
        from unittest import mock
        text = self.rowless_cancellation(empty_checkpoint=True)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        original = audit['checkpoints']['execution_checkpoints']
        for field, value in (('operation', 'rebase'), ('expected_head', 'old-head'),
                ('target_base_oid', 'old-base'), ('observed_remote_oid', 'old-remote'),
                ('workspace_ownership_held', 1), ('snapshot', '{"prepared_documents":{}}'),
                ('snapshot', 'null'), ('snapshot', 'broken'), ('repo', '/other'),
                ('plan_id', 'G-002'), ('generation', -1), ('future_ownership', 'unknown')):
            with self.subTest(field=field, value=value):
                audit['checkpoints'] = {'execution_checkpoints': {**original, field: value}}
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                git = mock.Mock(side_effect=AssertionError('checkpoint ownership must be preserved'))
                self.assertEqual(self.cleanup_rowless(text, git=git), ())
                self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_unpublished_checkpoint_without_resources_allows_rowless_erasure(self):
        text = self.rowless_cancellation(empty_checkpoint=True, empty_blocked_checkpoint=True)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        self.assertEqual(set(audit['checkpoints']), {'execution_checkpoints', 'blocked_checkpoints'})
        checkpoint = audit['checkpoints']['blocked_checkpoints']
        self.assertEqual((checkpoint['phase'], checkpoint['result']), ('unpublished', 'local_only'))
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM blocked_checkpoints WHERE plan_id='G-001'").fetchone())
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_cancellations WHERE plan_id='G-001'").fetchone())
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))
        self.assertEqual(self.cleanup_rowless(text), ())

    def test_unpublished_checkpoint_preserves_progress_identity_and_unknown_evidence(self):
        from unittest import mock
        text = self.rowless_cancellation(empty_blocked_checkpoint=True)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        original = audit['checkpoints']['blocked_checkpoints']
        variants = [{**original, field: value} for field, value in (
            *(('phase', phase) for phase in ('requested', 'prepared', 'local_committed', 'published', 'retry_pending')),
            *((field, 1) for field in ('retryable', 'rebase_recovery', 'remote_observed', 'inspection_retryable')),
            *((field, 'owned') for field in ('branch', 'worktree', 'pr_url', 'expected_remote_oid',
                'commit_identity', 'identity_reason_code', 'identity_evidence', 'tree_oid', 'local_oid',
                'remote_oid', 'published_oid', 'accepted_remote_oid', 'pending_remote_oid',
                'inspected_remote_oid', 'inspected_input', 'inspection_reason')),
            ('publication_history', '[{"published_oid":"old-head"}]'), ('publication_history', 'broken'),
            ('publication_history', '{}'), ('request_identity', 'other-request'), ('repo', '/other'),
            ('plan_id', 'G-002'), ('unpublished_reason', ''), ('result', 'published'),
            ('future_ownership', 'unknown'))]
        variants.append({key: value for key, value in original.items() if key != 'branch'})
        for checkpoint in variants:
            with self.subTest(checkpoint=checkpoint):
                audit['checkpoints'] = {'blocked_checkpoints': checkpoint}
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                git = mock.Mock(side_effect=AssertionError('checkpoint evidence must be preserved'))
                self.assertEqual(self.cleanup_rowless(text, git=git), ())
                self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_native_block_and_fingerprint_bound_checkpoint_move_to_audit_before_erasure(self):
        from unittest import mock
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.artifact_call('stopped-worker', '/repo', 'G-001')
        self.connection.commit()
        text = self.rowless_cancellation(started=True, native_block=True)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        self.assertEqual(audit['block']['release_fingerprint'], 'c' * 64)
        self.assertEqual(audit['checkpoints']['blocked_checkpoints']['phase'], 'requested')
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_blocks WHERE plan_id='G-001'").fetchone())
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        original = audit['block']
        for block in (None, {**original, 'release_fingerprint': 'other'}, {**original, 'repo': '/other'},
                      {**original, 'reason_code': 'other'}, {**original, 'evidence': 'other'},
                      {**original, 'future_ownership': 'unknown'}):
            with self.subTest(block=block):
                audit['block'] = block
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                self.assertEqual(self.cleanup_rowless(text, git=mock.Mock(side_effect=AssertionError('unknown block binding'))), ())
                self.assertEqual(tuple(self.connection.iterdump()), before)
        audit['block'] = original
        self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
        self.connection.commit()
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM agent_invocations WHERE plan_id='G-001'").fetchone())
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_block_and_checkpoint_audit_transfer_rolls_back_with_failed_cancellation(self):
        import sqlite3
        from unittest import mock
        from loop_manager_internal.storage import planning_publication_state
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.artifact_call('stopped-worker', '/repo', 'G-001')
        self.connection.execute("CREATE TRIGGER reject_block_cancellation BEFORE DELETE ON exec_plans "
            "BEGIN SELECT RAISE(ABORT,'interrupted block cancellation'); END")
        self.connection.commit()
        before = []
        apply = planning_publication_state.apply_mutable_catalog_changes
        def capture(*args, **kwargs):
            before.extend(self.connection.iterdump())
            return apply(*args, **kwargs)
        with mock.patch.object(planning_publication_state, 'apply_mutable_catalog_changes', side_effect=capture):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'interrupted block cancellation'):
                self.rowless_cancellation(started=True, native_block=True)
        self.assertTrue(before)
        self.assertEqual(list(self.connection.iterdump()), before)
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_cancelled_resource_free_requested_checkpoint_does_not_require_synthetic_completion(self):
        from unittest import mock
        from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
        text = self.rowless_cancellation(requested_blocked_checkpoint=True)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        checkpoint = audit['checkpoints']['blocked_checkpoints']
        self.assertEqual(checkpoint['phase'], 'requested')
        self.assertIsNone(checkpoint['result'])
        self.assertIsNone(checkpoint['unpublished_reason'])
        self.assertEqual(BlockedCheckpointRepository(self.connection).pending('/repo', 'G-001'), ())
        self.assertIsNone(self.connection.execute("SELECT 1 FROM blocked_checkpoints WHERE plan_id='G-001'").fetchone())
        for field, value in (('phase', 'prepared'), ('result', 'local_only'), ('unpublished_reason', 'synthetic completion'),
                ('branch', 'owned'), ('local_oid', 'old-head'), ('retryable', 1),
                ('request_identity', 'other-request'), ('publication_history', '[{"local_oid":"old-head"}]')):
            with self.subTest(field=field):
                audit['checkpoints']['blocked_checkpoints'] = {**checkpoint, field: value}
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                self.assertEqual(self.cleanup_rowless(text, git=mock.Mock(side_effect=AssertionError('owned checkpoint'))), ())
                self.assertEqual(tuple(self.connection.iterdump()), before)
        audit['checkpoints']['blocked_checkpoints'] = checkpoint
        self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
        self.connection.commit()
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_cancellations WHERE plan_id='G-001'").fetchone())
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))

    def test_resolved_checkpoint_without_publication_result_allows_rowless_erasure(self):
        from unittest import mock
        text = self.rowless_cancellation(resolved_blocked_checkpoint=True)
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        checkpoint = audit['checkpoints']['blocked_checkpoints']
        self.assertEqual(checkpoint['phase'], 'unpublished')
        self.assertIsNone(checkpoint['result'])
        self.assertTrue(checkpoint['unpublished_reason'])
        for field, value in (('phase', 'requested'), ('unpublished_reason', None),
                ('retryable', 1), ('publication_history', '[{"local_oid":"old-head"}]')):
            with self.subTest(field=field):
                changed = {**checkpoint, field: value}
                audit['checkpoints']['blocked_checkpoints'] = changed
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                self.assertEqual(self.cleanup_rowless(text, git=mock.Mock(side_effect=AssertionError('unresolved checkpoint'))), ())
                self.assertEqual(tuple(self.connection.iterdump()), before)
        audit['checkpoints']['blocked_checkpoints'] = checkpoint
        self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
        self.connection.commit()
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_cancellations WHERE plan_id='G-001'").fetchone())
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))

    def test_rowless_owned_workspace_audit_finishes_only_after_current_resources_are_absent(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from loop_manager_internal.catalog.deleted_plan_cleanup import cleanup
        text = self.rowless_cancellation(started=True, empty_checkpoint=True)
        self.artifact_call('old-call', '/repo', 'G-001')
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        with TemporaryDirectory() as directory:
            state = Path(directory)
            workspace = state / 'worktrees' / 'repo-key' / 'G-001'
            audit['row'].update(status='IMPLEMENTING', workspace_owned=1,
                branch='loop-manager/G-001', worktree=str(workspace), publication_remote_oid='d' * 40,
                pr_url='https://example/pr/old', pr_head_oid='d' * 40)
            audit['cleanup_pending'] = True
            from loop_manager_internal.agents.scope_contracts import GitSnapshot
            native_scope = GitSnapshot('d' * 40, 'refs/heads/loop-manager/G-001', {}, {}, {}, {}, {}, {}).serialize()
            audit['row'].update(agent_snapshot=native_scope, agent_result_snapshot=native_scope,
                agent_manager_snapshot=native_scope, agent_input_fingerprint='f' * 64,
                agent_input_plan=text, agent_result_plan=text)
            retired_scope = dict(agent_role='worker', agent_snapshot=native_scope,
                                 agent_result_snapshot=native_scope, agent_manager_snapshot=native_scope)
            import hashlib
            baseline_id = hashlib.sha256(('d' * 40 + '\0' + 'a' * 40 + '\0' + 'b' * 40).encode()).hexdigest()
            baseline_ref = 'refs/loop-manager/baselines/' + hashlib.sha256(b'/repo').hexdigest()[:16] + '/' + baseline_id
            audit['row']['agent_baseline'] = json.dumps(dict(version=1, ref=baseline_ref, ref_target='e' * 40,
                index_tree='a' * 40, worktree_tree='b' * 40))
            origin = dict(version=1, repo='/repo', plan_id='G-001', branch=audit['row']['branch'],
                worktree=str(workspace), pr_url=audit['row']['pr_url'], pr_head_oid='d' * 40, worker_result='owned historical result')
            origin_id = hashlib.sha256(json.dumps(origin, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            checkpoint = audit['checkpoints']['execution_checkpoints']
            from dataclasses import asdict
            from loop_manager_internal.execution.execution_checkpoint import observe
            def native_git(_workspace, *args, **kwargs):
                output = ('d' * 40 if args == ('rev-parse', 'HEAD') else
                          'loop-manager/G-001' if args[0] == 'symbolic-ref' else '')
                return SimpleNamespace(returncode=0, stdout=output)
            observation = asdict(observe(plan_id='G-001', row={**audit['row'], 'phase': 'IMPLEMENTING',
                **{key: None for key in ('verify_token', 'review_token', 'archive_token', 'publication_token')}},
                definition_revision='current-definition', base_oid='a' * 40, branch=audit['row']['branch'],
                worktree=workspace, owned=True, git=native_git, rebase_in_progress=lambda *_: False,
                remote_oid=None, pull_request=None, rebase_intent=False, rebase_before_head=None,
                rebase_target_oid=None, stash_oid=None))
            checkpoint.update(expected_head='d' * 40, target_base_oid='a' * 40, observed_remote_oid='d' * 40,
                snapshot=json.dumps(dict(**{**observation, 'pending_operation': 'rebase', 'stash_oid': 'c' * 40}, worktree=str(workspace), worktree_recovery_pending=False,
                                         stored_active_sha='a' * 40, worker_checkpoint_result=dict(head='d' * 40, title='preserved'),
                                         worker_checkpoint_requests=[], fetch_in_progress=False, fetched_base_oid='a' * 40, stage_result_origins={origin_id: dict(evidence=origin, reason='rebase')},
                                         worker_candidate_token='e' * 64, retired_agent_scopes=[retired_scope])))
            self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
            self.connection.commit()
            before = list(self.connection.iterdump())
            pinned = SimpleNamespace(regular_paths=set(), revision_sha='c' * 40,
                snapshot=SimpleNamespace(observed_ids={'G-002'}, all={}, withheld=()))
            observations = []
            def run(*, registration='', remote='', pr='closed-history', local=False, race=False, baseline=False, baseline_race=False):
                observations.clear()
                baseline_present = baseline
                def git(_repo, *args, **kwargs):
                    observations.append(args)
                    self.assertFalse(self.connection.in_transaction)
                    output = registration if args[0] == 'worktree' else remote if args[0] == 'ls-remote' else ''
                    return SimpleNamespace(returncode=int(args[0] == 'rev-parse' and not (local or baseline_present and args[-1] == baseline_ref)), stdout=output)
                def observe_pr(*_args):
                    nonlocal baseline_present
                    if baseline_race:
                        baseline_present = True
                    if race:
                        workspace.mkdir(parents=True, exist_ok=True)
                    return (dict(state='CLOSED', mergedAt=None, headRefName='loop-manager/G-001',
                                 url='https://example/pr/old') if pr == 'closed-history' else pr)
                return cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                    git_bytes=lambda *_: text.encode(), pr_for_branch=observe_pr,
                    state_dir=state, revision_is_current=lambda: True)
            for bad_snapshot in (
                    {**observation, 'pr_state': 'MERGED'},
                    {**observation, 'branch': 'foreign'},
                    {**observation, 'worktree': '/independent'},
                    {**observation, 'worktree_recovery_pending': 'false'},
                    {**observation, 'worker_checkpoint_result': dict(worktree='/independent')},
                    {'phase': 'IMPLEMENTING'},
                    {'stage_result_origins': {origin_id: dict(evidence={**origin, 'repo': '/other'}, reason='rebase')}},
                    {'stage_result_origins': {'bad-checksum': dict(evidence=origin, reason='rebase')}},
                    {'retired_agent_scopes': [{**retired_scope, 'agent_snapshot': GitSnapshot(
                        'd' * 40, 'refs/heads/other-task', {}, {}, {}, {}, {}, {}).serialize()}]},
                    {'stage_result_origins': {origin_id: dict(evidence={**origin, 'worktree': '/unowned'}, reason='rebase')}}):
                invalid = json.loads(json.dumps(audit))
                invalid['checkpoints']['execution_checkpoints']['snapshot'] = json.dumps(bad_snapshot)
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(invalid),))
                self.connection.commit()
                retained = list(self.connection.iterdump())
                self.assertEqual(run(), ())
                self.assertEqual(observations, [])
                self.assertEqual(list(self.connection.iterdump()), retained)
            self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
            self.connection.commit()
            for kwargs in ({'remote': 'new-remote-head'}, {'local': True}, {'baseline': True}, {'baseline_race': True}, {'pr': None}, {'pr': {'state': 'OPEN'}},
                    {'pr': dict(state='CLOSED', mergedAt='merged', headRefName='loop-manager/G-001', url='https://example/pr/old')},
                    {'pr': dict(state='CLOSED', mergedAt=None, headRefName='loop-manager/G-001', url='https://example/pr/other')},
                    {'registration': 'worktree ' + str(workspace)},
                    {'registration': 'branch refs/heads/loop-manager/G-001'}, {'race': True}):
                with self.subTest(kwargs=kwargs):
                    self.assertEqual(run(**kwargs), ())
                    self.assertTrue(observations)
                    self.assertEqual(list(self.connection.iterdump()), before)
                    if workspace.exists():
                        workspace.rmdir()
            self.connection.execute("UPDATE projects SET worktree=? WHERE repo='/repo'", (str(workspace),))
            self.connection.commit()
            self.assertEqual(run(), ())
            self.connection.execute("UPDATE projects SET worktree=NULL WHERE repo='/repo'")
            from loop_manager_internal.storage.execplan_archive_state import ExecPlanArchiveRepository
            archive = ExecPlanArchiveRepository(self.connection)
            archive.observe('/repo', 'git:old', category='shared_resource', resource=str(workspace),
                reason='unknown historical owner', resume_condition='reobserve', attempted=True)
            self.connection.commit()
            before_archive = list(self.connection.iterdump())
            self.assertEqual(run(), ())
            self.assertEqual(list(self.connection.iterdump()), before_archive)
            audit['cleanup_pending'] = False
            self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
            archive.observe('/repo', 'git:old', category='shared_resource', resource=str(workspace),
                reason='confirmed historical owner', resume_condition='cleanup', attempted=False, owner_plan_id='G-001')
            self.connection.commit()
            self.assertEqual(run(), ('G-001',))
            self.assertIsNone(self.connection.execute("SELECT * FROM execplan_archive_observations WHERE scope='git:old'").fetchone())
            self.assertIsNone(self.connection.execute("SELECT * FROM execplan_cancellations WHERE plan_id='G-001'").fetchone())
            self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
            self.assertEqual(run(), ())

    def test_native_deleted_branch_restart_waits_for_retired_copy_and_refs_then_erases(self):
        import shutil
        import hashlib
        from dataclasses import replace
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest import mock
        from loop_manager_internal.agents.scope_contracts import GitSnapshot
        from loop_manager_internal.recovery import deleted_branch_restart, scoped_blocking
        from loop_manager_internal.storage import runtime_state
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from loop_manager_internal.storage.planning_publication_state import apply_mutable_catalog_changes
        from loop_manager_internal.planning.planning_catalog import PlanDeletion
        from loop_manager_internal.planning.planner_validation import planning_execution_state
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        from loop_manager_internal.catalog.deleted_plan_cleanup import cleanup
        with TemporaryDirectory() as directory:
            state = Path(directory)
            workspace = state / 'worktrees' / 'repo-key' / 'G-001'
            text = self.rowless_cancellation(started=True, native_block=True, empty_checkpoint=True, owned_workspace=workspace)
            audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
            self.connection.execute('DELETE FROM execplan_cancellations')
            for table, row in [('exec_plans', audit['row']), ('execplan_blocks', audit['block']),
                               *audit['checkpoints'].items()]:
                self.connection.execute('INSERT INTO ' + table + ' (' + ','.join(row) + ') VALUES ('
                    + ','.join('?' for _ in row) + ')', tuple(row.values()))
            self.connection.execute("UPDATE projects SET origin='owner/repo',registered=1 WHERE repo='/repo'")
            scoped_blocking.record_plan_block(self.connection, '/repo', 'G-001',
                scoped_blocking.BlockReasonCode.REMOTE_BRANCH_DELETED, 'native deleted branch', None)
            self.connection.commit()
            workspace.mkdir(parents=True)
            (workspace / 'original.txt').write_text('original user input')
            branches, heads = {str(workspace): 'loop-manager/G-001'}, {str(workspace): 'a' * 40}
            def capture(path):
                content = path / 'original.txt'
                dirty = {'original.txt': (('untracked',), hashlib.sha256(content.read_bytes()).hexdigest())} if content.exists() else {}
                return GitSnapshot(heads[str(path)], branches[str(path)], {}, dirty, {}, {}, {}, {})
            def reset_git(path, *args, **kwargs):
                output = ''
                if args[:2] == ('remote', 'get-url'):
                    output = '/mock-origin'
                elif args[:2] == ('ls-remote', '--heads'):
                    pass
                elif args[:2] == ('rev-parse', 'FETCH_HEAD^{commit}'):
                    output = 'b' * 40
                elif args[:2] == ('worktree', 'move'):
                    shutil.move(args[2], args[3])
                    branches[args[3]], heads[args[3]] = branches.pop(args[2]), heads.pop(args[2])
                elif args[:2] == ('branch', '-m'):
                    branches[str(path)] = args[3]
                elif args[:2] == ('worktree', 'add'):
                    Path(args[4]).mkdir()
                    branches[args[4]], heads[args[4]] = args[3], args[5]
                elif args[0] != 'fetch':
                    raise AssertionError(args)
                return SimpleNamespace(returncode=0, stdout=output)
            effects = SimpleNamespace(git=reset_git, github_repository=lambda _: 'owner/repo',
                validate_execplan_workspace_identity=lambda *_: None, validate_managed_worktree=lambda *_: None,
                capture_git_snapshot=capture)
            deleted_branch_restart.restart(effects, self.connection, Path('/repo'), state, '/repo', 'G-001',
                complete_merged=mock.Mock(side_effect=AssertionError('not merged')), unmet_dependencies=lambda *_: (),
                lookup_pr=lambda *_: None)
            checkpoints = ExecutionCheckpointRepository(self.connection)
            restart = checkpoints.get('/repo', 'G-001').snapshot['deleted_branch_restart']
            archive = Path(restart['archive'])
            self.assertEqual((archive / 'original.txt').read_text(), 'original user input')
            self.assertEqual(restart['state'], 'complete')
            row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
            original = ReadyPlanSnapshot('G-001', row['path'], row['title'], tuple(json.loads(row['sources'])), (),
                'low', 'low', row['priority'], row['content_hash'], 'a' * 40, text,
                current_complexity=row['current_complexity'], current_risk=row['current_risk'], current_metadata_recorded=True,
                execution_state=planning_execution_state(row))
            with runtime_state._transaction(self.connection):
                apply_mutable_catalog_changes(self.connection, '/repo', deletions=(PlanDeletion(original,
                    {'decision': {'id': 'G-001', 'source': 'docs/PRODUCT.md', 'outcome': 'withdrawn', 'reason': 'goal withdrawn'}}),),
                    revisions=(), snapshot_token='withdrawn', plan_commit='c' * 40, content_hash=lambda _: '', agent=False)
            self.artifact_call('stopped-before-restart', '/repo', 'G-001')
            self.connection.commit()
            workspace.rmdir()  # Existing native resource owner retired the fresh workspace.
            pinned = SimpleNamespace(regular_paths=set(), revision_sha='d' * 40,
                snapshot=SimpleNamespace(observed_ids={'G-002'}, all={}, withheld=()))
            live, registration, race, local, unreadable, pull_request = False, '', False, False, False, None
            observations = []
            def git(_repo, *args, **kwargs):
                observations.append(args)
                self.assertFalse(self.connection.in_transaction)
                if unreadable and 'ls-remote' in args and args[-1].endswith(restart['archive_branch']):
                    raise RuntimeError('mock remote unavailable')
                output = registration if args[0] == 'worktree' else ('head' if live and 'ls-remote' in args and args[-1].endswith(restart['archive_branch']) else '')
                return SimpleNamespace(returncode=int(args[0] == 'rev-parse' and not (local and args[-1].endswith(restart['archive_branch']))), stdout=output)
            def pr(_repo, branch, *_):
                nonlocal race
                if race and branch == restart['archive_branch']:
                    race = False
                    archive.mkdir(parents=True)
                return pull_request if branch == restart['archive_branch'] else None
            def run():
                return cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                    git_bytes=lambda *_: text.encode(), pr_for_branch=pr, state_dir=state, revision_is_current=lambda: True)
            before = tuple(self.connection.iterdump())
            self.assertEqual(run(), ())
            self.assertEqual((archive / 'original.txt').read_text(), 'original user input')
            self.assertEqual(tuple(self.connection.iterdump()), before)
            (archive / 'original.txt').unlink()
            archive.rmdir()  # This observation follows physical retirement, without redoing reset.
            live = True
            self.assertEqual(run(), ())
            self.assertEqual(tuple(self.connection.iterdump()), before)
            live, local = False, True
            self.assertEqual(run(), ())
            local, unreadable = False, True
            self.assertEqual(run(), ())
            unreadable, pull_request = False, dict(state='OPEN')
            self.assertEqual(run(), ())
            pull_request, registration = None, 'worktree ' + str(archive)
            self.assertEqual(run(), ())
            self.assertEqual(tuple(self.connection.iterdump()), before)
            registration = ''
            self.connection.execute("UPDATE projects SET worktree=? WHERE repo='/repo'",
                                    (str(archive.parent / '..' / 'retired' / archive.name),))
            self.connection.commit()
            shared = tuple(self.connection.iterdump())
            self.assertEqual(run(), ())
            self.assertEqual(tuple(self.connection.iterdump()), shared)
            self.connection.execute("UPDATE projects SET worktree=NULL WHERE repo='/repo'")
            self.connection.commit()
            registration, race = '', True
            self.assertEqual(run(), ())
            self.assertTrue(archive.exists())
            archive.rmdir()
            self.assertEqual(tuple(self.connection.iterdump()), before)
            self.assertEqual(run(), ('G-001',))
            self.assertIsNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())
            self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
            self.assertEqual(run(), ())

    def test_retired_native_rebase_publications_and_history_erase_without_replaying_tokens(self):
        import copy
        import sqlite3
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from dataclasses import replace
        from loop_manager_internal.catalog.deleted_plan_cleanup import cleanup
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from tests.support.rebase_observations import expectation, observation, decision_for
        with TemporaryDirectory() as directory:
            state = Path(directory)
            workspace = state / 'worktrees' / 'repo-key' / 'G-001'
            def build():
                checkpoints = ExecutionCheckpointRepository(self.connection)
                for head in ('c' * 40, 'd' * 40):
                    current = checkpoints.get('/repo', 'G-001')
                    expected = expectation(repo='/repo', plan_id='G-001', plan_path='docs/plans/[G-001] Before Rename.md',
                        branch='loop-manager/G-001', worktree=str(workspace), generation=current.generation,
                        target_base_oid='a' * 40)
                    expected = replace(expected, remote=replace(expected.remote, branch=expected.branch,
                        oid=current.observed_remote_oid or 'b' * 40))
                    accepted = decision_for(expected, observation(expected, head=head, branch_ref=(head, ''),
                        registration=('HEAD=' + head, 'branch=refs/heads/' + expected.branch))).acceptance
                    saved = checkpoints.accept_current_rebase('/repo', 'G-001', acceptance=accepted,
                        prior_artifacts=dict(version=1, repo='/repo', plan_id='G-001', branch=expected.branch,
                                             worktree=str(workspace), worker_result='old result'))
                    pending = checkpoints.begin_rebase_publication('/repo', 'G-001', expected_generation=saved.generation,
                                                                  acceptance=accepted, remote_before=expected.remote.oid)
                    after = replace(expected, generation=pending.generation, remote=replace(expected.remote, oid=head))
                    result = decision_for(after, observation(after, head=head, branch_ref=(head, ''),
                        registration=('HEAD=' + head, 'branch=refs/heads/' + after.branch))).acceptance
                    saved = checkpoints.finish_rebase_publication('/repo', 'G-001',
                        intent=pending.snapshot['rebase_publication_intent'], acceptance=result)
                # The engine retains the previous snapshot separately when a
                # new rebase begins. No execution success is inferred from it.
                from loop_manager_internal.agents.scope_contracts import GitSnapshot
                import hashlib
                scope = GitSnapshot('f' * 40, 'loop-manager/G-001', {}, {}, {}, {}, {}, {}).serialize()
                baseline_id = hashlib.sha256(('f' * 40 + '\0' + 'a' * 40 + '\0' + 'b' * 40).encode()).hexdigest()
                baseline_ref = 'refs/loop-manager/baselines/' + hashlib.sha256(b'/repo').hexdigest()[:16] + '/' + baseline_id
                baseline = json.dumps(dict(version=1, ref=baseline_ref, ref_target='e' * 40,
                    index_tree='a' * 40, worktree_tree='b' * 40))
                preserved = dict(remote='d' * 40, original_head='f' * 40, snapshot=scope,
                                 baseline=baseline, phase='complete')
                origin = dict(version=1, repo='/repo', plan_id='G-001', branch='loop-manager/G-001',
                    worktree=str(workspace), pr_url=None, pr_head_oid=None, worker_result='historical source')
                origin_id = hashlib.sha256(json.dumps(origin, sort_keys=True, ensure_ascii=False,
                                                       separators=(',', ':')).encode()).hexdigest()
                snapshot = {**saved.snapshot,
                    'stage_result_origins': {origin_id: dict(evidence=origin, reason='rebase_start')},
                    'rebase_operation_history': [{**copy.deepcopy(saved.snapshot), 'rebase_stage_origin': origin_id}],
                    'integration_repo': '/repo', 'pending_work_branch_input': 'a' * 40, 'base_rebase_restart': preserved,
                    'base_rebase_restart_history': [{**preserved, 'phase': 'preserving'}],
                    'retired_rebase_publications': [dict(intent=copy.deepcopy(saved.snapshot['rebase_publication_intent']),
                                                        observed_remote='e' * 40, confirmed=False)],
                    'rebase_publication_intent': None,
                    'superseded_rebases': [dict(operation='rebase', expected_head='a' * 40,
                        target_base_oid='b' * 40, observed_remote_oid='c' * 40, snapshot=copy.deepcopy(saved.snapshot))]}
                checkpoints.record('/repo', 'G-001', expected_generation=saved.generation, operation=None,
                    expected_head=saved.expected_head, target_base_oid=saved.target_base_oid,
                    observed_remote_oid=saved.observed_remote_oid, snapshot=snapshot)
                self.connection.commit()
            text = self.rowless_cancellation(started=True, empty_checkpoint=True, owned_workspace=workspace,
                                             checkpoint_builder=build)
            self.artifact_call('retired-rebase', '/repo', 'G-001')
            self.connection.commit()
            original = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
            snapshot = json.loads(original['checkpoints']['execution_checkpoints']['snapshot'])
            self.assertEqual(len(snapshot['rebase_publication_history']), 1)
            pinned = SimpleNamespace(regular_paths=set(), revision_sha='e' * 40,
                snapshot=SimpleNamespace(observed_ids={'G-002'}, all={}, withheld=()))
            observations = []
            baseline_present = False
            def git(_repo, *args, **kwargs):
                observations.append(args)
                self.assertFalse(self.connection.in_transaction)
                return SimpleNamespace(returncode=int(args[0] == 'rev-parse' and not baseline_present), stdout='')
            def run():
                return cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                    git_bytes=lambda *_: text.encode(), pr_for_branch=lambda *_: None,
                    state_dir=state, revision_is_current=lambda: True)
            before = tuple(self.connection.iterdump())
            baseline_present = True
            self.assertEqual(run(), ())
            self.assertTrue(any(args[0] == 'rev-parse' and args[-1].startswith('refs/loop-manager/baselines/') for args in observations))
            self.assertEqual(tuple(self.connection.iterdump()), before)
            baseline_present = False
            for change in ('foreign', 'intent', 'result', 'nested', 'baseline', 'origin'):
                invalid = copy.deepcopy(snapshot)
                if change == 'foreign':
                    acceptance = invalid['current_rebase_acceptance']
                    acceptance['worktree'] = '/independent'
                    from loop_manager_internal.git.managed_rebase_validation import fingerprint
                    acceptance['id'] = fingerprint({key: value for key, value in acceptance.items() if key != 'id'})
                elif change == 'intent':
                    invalid['retired_rebase_publications'][0]['intent']['id'] = 'forged'
                elif change == 'result':
                    invalid['rebase_publication_result']['intent_id'] = 'other-request'
                elif change == 'baseline':
                    invalid['base_rebase_restart']['baseline'] = '{}'
                elif change == 'origin':
                    invalid['stage_result_origins'] = {}
                else:
                    invalid['rebase_operation_history'][0]['unknown_resource'] = '/independent'
                audit = copy.deepcopy(original)
                audit['checkpoints']['execution_checkpoints']['snapshot'] = json.dumps(invalid)
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                observations.clear()
                self.assertEqual(run(), ())
                self.assertEqual(observations, [])
                self.assertEqual(tuple(self.connection.iterdump()), before)
            self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(original),))
            self.connection.execute("CREATE TRIGGER interrupt_retired_rebase BEFORE DELETE ON agent_invocations "
                                    "BEGIN SELECT RAISE(ABORT,'retain rebase history'); END")
            self.connection.commit()
            before = tuple(self.connection.iterdump())
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'retain rebase history'):
                run()
            self.assertEqual(tuple(self.connection.iterdump()), before)
            self.connection.execute('DROP TRIGGER interrupt_retired_rebase')
            self.connection.commit()
            self.assertEqual(run(), ('G-001',))
            self.assertIsNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())
            self.assertIsNone(self.connection.execute('SELECT * FROM agent_invocations').fetchone())
            self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
            self.assertEqual(run(), ())

    def test_published_cancelled_checkpoint_retires_after_actual_resource_absence(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from loop_manager_internal.catalog.deleted_plan_cleanup import cleanup
        with TemporaryDirectory() as directory:
            state = Path(directory)
            workspace = state / 'worktrees' / 'repo-key' / 'G-001'
            text = self.rowless_cancellation(started=True, native_block=True, owned_workspace=workspace, published_blocked=True)
            self.artifact_call('old-publisher', '/repo', 'G-001')
            self.connection.commit()
            audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
            checkpoint = audit['checkpoints']['blocked_checkpoints']
            self.assertEqual((checkpoint['phase'], checkpoint['result']), ('published', 'published'))
            self.assertEqual(len(json.loads(checkpoint['publication_history'])), 1)
            before = list(self.connection.iterdump())
            pinned = SimpleNamespace(regular_paths=set(), revision_sha='c' * 40,
                snapshot=SimpleNamespace(observed_ids={'G-002'}, all={}, withheld=()))
            remote, local = False, False
            observations = []
            def git(_repo, *args, **kwargs):
                observations.append(args)
                self.assertFalse(self.connection.in_transaction)
                return SimpleNamespace(returncode=int(args[0] == 'rev-parse' and not local),
                                       stdout='owned-head' if args[0] == 'ls-remote' and remote else '')
            def run():
                return cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                    git_bytes=lambda *_: text.encode(), pr_for_branch=lambda *_: None,
                    state_dir=state, revision_is_current=lambda: True)
            workspace.mkdir(parents=True)
            (workspace / 'unpublished.txt').write_text('preserved work')
            self.assertEqual(run(), ())
            self.assertTrue(all(args[0] in {'merge-base', 'worktree', 'rev-parse', 'ls-remote'}
                                and (args[0] != 'worktree' or args[1] == 'list') for args in observations))
            self.assertEqual(list(self.connection.iterdump()), before)
            self.assertEqual((workspace / 'unpublished.txt').read_text(), 'preserved work')
            (workspace / 'unpublished.txt').unlink()
            workspace.rmdir()
            remote = True
            self.assertEqual(run(), ())
            self.assertEqual(list(self.connection.iterdump()), before)
            remote, local = False, True
            self.assertEqual(run(), ())
            self.assertEqual(list(self.connection.iterdump()), before)
            local = False
            original = checkpoint['publication_history']
            checkpoint['publication_history'] = '[{"unknown_owner":"G-999"}]'
            self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
            self.connection.commit()
            malformed = list(self.connection.iterdump())
            self.assertEqual(run(), ())
            self.assertEqual(list(self.connection.iterdump()), malformed)
            checkpoint['publication_history'] = original
            self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
            self.connection.commit()
            self.assertEqual(run(), ('G-001',))
            self.assertIsNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())
            self.assertIsNone(self.connection.execute('SELECT * FROM agent_invocations').fetchone())
            self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
            self.assertEqual(run(), ())

    def test_started_rowless_audit_preserves_uncertain_judgment_resources_and_agent_ownership(self):
        from unittest import mock
        text = self.rowless_cancellation(started=True)
        self.artifact_call('old-call', '/repo', 'G-001')
        self.connection.commit()
        original = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        for audit in ({**original, 'judgment': None}, {**original, 'judgment': {'decision': 'unknown'}},
                {**original, 'cleanup_pending': True}, {**original, 'checkpoints': {'saved': 'operation'}},
                *({**original, 'row': {**original['row'], field: value}} for field, value in (
                    ('branch', 'loop-manager/G-001'), ('worktree', '/old-worktree'), ('agent_role', 'worker'),
                    ('agent_snapshot', '{}'), ('publication_remote_oid', 'old-oid'), ('workspace_owned', 1)))):
            with self.subTest(audit=audit):
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                if audit['judgment'] != original['judgment']:
                    # A missing judgment can now use published recovery, but
                    # without that proof only read-only ancestry checks occur.
                    git = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=''))
                else:
                    git = mock.Mock(side_effect=AssertionError('uncertain audit must be preserved'))
                self.assertEqual(self.cleanup_rowless(text, git=git), ())
                self.assertTrue(all(call.args[1:3] == ('merge-base', '--is-ancestor')
                                    for call in git.call_args_list))
                self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_orphan_or_relative_collection_reference_keeps_file_queue_pending(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from loop_manager_internal.catalog.deleted_plan_cleanup import remove_pending_files
        with TemporaryDirectory() as directory:
            state = Path(directory)
            retained = state / 'logs' / 'retained'
            retained.mkdir(parents=True)
            proof = retained / 'proof.json'
            proof.write_text('unattributed verification evidence')
            deleted_plan_state.forget(self.connection, '/repo', 'G-001', artifacts=(retained,))
            self.connection.execute('INSERT INTO targeted_test_collections '
                "(invocation_id,directory,device,inode,receipt) VALUES ('legacy-orphan',?,'1','2','receipt')", (str(retained),))
            self.connection.commit()
            for path in (str(retained), 'relative-collection'):
                self.connection.execute("UPDATE targeted_test_collections SET directory=? WHERE invocation_id='legacy-orphan'", (path,))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                self.assertEqual(remove_pending_files(self.connection, '/repo', state), {'G-001'})
                self.assertEqual(tuple(self.connection.iterdump()), before)
                self.assertEqual(proof.read_text(), 'unattributed verification evidence')

    def test_shared_worktree_log_is_protected_by_independent_execution_history(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths
        from loop_manager_internal.storage.task_calls import CallDeferred
        self.connection.execute("UPDATE exec_plans SET worktree='/shared/worktree'")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with TemporaryDirectory() as directory, self.assertRaises(CallDeferred):
            artifact_paths(self.connection, '/repo', dict(plan_id='G-001', worktree='/shared/worktree'), Path(directory))
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_cancelled_worktree_log_owner_is_rechecked_after_queue_persistence(self):
        import hashlib
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths, remove_pending_files
        from loop_manager_internal.storage.task_calls import CallDeferred
        self.rowless_cancellation()
        audit = json.loads(self.connection.execute('SELECT recovery_evidence FROM execplan_cancellations').fetchone()[0])
        self.connection.execute("INSERT INTO exec_plans(repo,plan_id,path,title,sources,depends_on,priority,content_hash,status) "
            "VALUES ('/repo','G-999','target.md','Target','[]','[]','normal','target','READY')")
        self.connection.commit()
        with TemporaryDirectory() as directory:
            state = Path(directory)
            log = state / 'logs' / hashlib.sha256(b'/shared/worktree').hexdigest()[:16]
            log.mkdir(parents=True)
            (log / 'proof.txt').write_text('independent cancellation log')
            row = dict(plan_id='G-999', worktree='/shared/worktree')
            paths = artifact_paths(self.connection, '/repo', row, state)
            deleted_plan_state.forget(self.connection, '/repo', 'G-999', artifacts=paths)
            audit['row']['worktree'] = '/shared/worktree'
            self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
            self.connection.commit()
            before = tuple(self.connection.iterdump())
            with self.assertRaises(CallDeferred):
                artifact_paths(self.connection, '/repo', row, state)
            self.assertEqual(remove_pending_files(self.connection, '/repo', state), {'G-999'})
            self.assertEqual(tuple(self.connection.iterdump()), before)
            self.assertEqual((log / 'proof.txt').read_text(), 'independent cancellation log')
            audit['row']['worktree'] = '/independent/worktree'
            self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (json.dumps(audit),))
            self.connection.commit()
            self.assertEqual(remove_pending_files(self.connection, '/repo', state), set())
            self.assertFalse(log.exists())

    def test_file_queue_preserves_project_manual_and_suspended_planning_log_references(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from loop_manager_internal.agents.provider_adapters import _provider_log_directory
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths, remove_pending_files
        from loop_manager_internal.storage import runtime_state
        from loop_manager_internal.storage.task_calls import CallDeferred
        _store, group, _request = self.manual_history('/repo', 'G-002')
        workspace = '/shared/log-workspace'
        self.connection.execute("UPDATE projects SET worktree=NULL,plan_path=NULL,phase=NULL")
        self.connection.execute("UPDATE exec_plans SET worktree=NULL WHERE plan_id='G-002'")
        self.connection.execute("UPDATE exec_plans SET worktree=? WHERE plan_id='G-001'", (workspace,))
        self.connection.commit()
        row = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())
        suspended = {**runtime_state.EXECUTION_CLEAR_VALUES, **runtime_state.PLANNING_CLEAR_VALUES,
                     'phase': 'PLANNING', 'planning_error': 'saved planning failure', 'worktree': workspace}
        with TemporaryDirectory() as directory:
            state = Path(directory)
            log = _provider_log_directory(Path(workspace), state)
            proof = log / 'independent.log'
            proof.write_text('retained provider output')
            self.connection.execute("UPDATE projects SET phase='IMPLEMENTING',plan_path=?,worktree=?", (row['path'], workspace))
            self.connection.commit()
            self.assertEqual(artifact_paths(self.connection, '/repo', row, state), (log,))
            deleted_plan_state.forget(self.connection, '/repo', 'G-001', artifacts=(log,))
            for sql, parameters in (
                ("UPDATE projects SET worktree=?,plan_path='docs/plans/[G-002] Independent.md'", (workspace,)),
                ("UPDATE projects SET worktree=?,phase='PLANNING',plan_path='docs/plans/[G-001] Gone.md'", (workspace,)),
                ('UPDATE projects SET planning_preserved_state=?', (json.dumps(suspended),)),
                ("UPDATE projects SET planning_preserved_state='broken snapshot'", ()),
                ('UPDATE projects SET worktree=?,planning_preserved_state=?', (workspace, '[' * 1100)),
                ('UPDATE manual_work_groups SET worktree=? WHERE group_id=?', (workspace, group['group_id'])),
                ("UPDATE manual_work_groups SET worktree='relative-workspace' WHERE group_id=?", (group['group_id'],)),
                ("INSERT INTO projects(repo,base_branch,worktree,plan_path) VALUES "
                 "('/other','main',?,'docs/plans/[G-001] Other.md')", (workspace,)),
            ):
                with self.subTest(sql=sql):
                    self.connection.execute(sql, parameters)
                    self.connection.commit()
                    before = tuple(self.connection.iterdump())
                    with self.assertRaises(CallDeferred):
                        artifact_paths(self.connection, '/repo', row, state)
                    self.assertEqual(remove_pending_files(self.connection, '/repo', state), {'G-001'})
                    self.assertEqual(tuple(self.connection.iterdump()), before)
                    self.assertEqual(proof.read_text(), 'retained provider output')
                    self.connection.execute('UPDATE projects SET worktree=NULL,plan_path=NULL,phase=NULL,planning_preserved_state=NULL')
                    self.connection.execute('UPDATE manual_work_groups SET worktree=NULL')
                    self.connection.execute("DELETE FROM projects WHERE repo='/other'")
                    self.connection.commit()
            self.assertEqual(remove_pending_files(self.connection, '/repo', state), set())
            self.assertFalse(log.exists())

    def test_unmanaged_receipt_preserves_call_and_cancellation_before_final_erasure(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        text = self.rowless_cancellation()
        with TemporaryDirectory() as directory:
            receipt = Path(directory) / 'external.process.json'
            receipt.write_text('external process evidence')
            self.artifact_call('external-call', '/repo', 'G-001', receipt)
            self.connection.commit()
            before = tuple(self.connection.iterdump())
            self.assertEqual(self.cleanup_rowless(text), ())
            self.assertEqual(tuple(self.connection.iterdump()), before)
            self.assertEqual(receipt.read_text(), 'external process evidence')
            self.assertIsNone(self.connection.execute('SELECT 1 FROM deleted_plan_ids').fetchone())
            self.assertIsNone(self.connection.execute('SELECT 1 FROM deleted_plan_files').fetchone())

    def test_receipt_at_managed_root_queues_only_owned_pair_and_preserves_siblings(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths, remove_pending_files
        with TemporaryDirectory() as directory:
            state = Path(directory)
            root = state / 'agent-runtime'
            root.mkdir()
            receipt, attempt, independent = (root / name for name in ('target.process.json', 'target', 'independent'))
            for path in (receipt, attempt, independent):
                path.write_text(path.name)
            self.artifact_call('root-call', '/repo', 'G-001', receipt)
            self.connection.commit()
            paths = artifact_paths(self.connection, '/repo', dict(plan_id='G-001', worktree=None), state)
            self.assertEqual(paths, (receipt, attempt))
            deleted_plan_state.forget(self.connection, '/repo', 'G-001', artifacts=paths)
            self.assertEqual(remove_pending_files(self.connection, '/repo', state), set())
            self.assertFalse(receipt.exists())
            self.assertFalse(attempt.exists())
            self.assertEqual(independent.read_text(), 'independent')
            self.assertTrue(root.is_dir())
            self.assertIsNone(self.connection.execute('SELECT 1 FROM deleted_plan_files').fetchone())

    def test_unattributed_cancellation_preserves_logs_but_allows_independent_runtime_cleanup(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from loop_manager_internal.catalog.deleted_plan_cleanup import remove_pending_files
        self.rowless_cancellation()
        with TemporaryDirectory() as directory:
            state = Path(directory)
            log = state / 'logs' / 'retained'
            runtime = state / 'agent-runtime' / 'independent'
            log.mkdir(parents=True)
            runtime.mkdir(parents=True)
            (log / 'proof.txt').write_text('unattributed historical log')
            self.connection.executemany('INSERT INTO deleted_plan_files VALUES (?,?,?)',
                [('/repo', 'G-999', str(path)) for path in (log, runtime)])
            for evidence in ('{}', 'broken', json.dumps({'row': {'repo': '/other', 'plan_id': 'G-001', 'worktree': '/worktree'}})):
                self.connection.execute('UPDATE execplan_cancellations SET recovery_evidence=?', (evidence,))
                self.connection.commit()
                self.assertEqual(remove_pending_files(self.connection, '/repo', state), {'G-999'})
                self.assertEqual((log / 'proof.txt').read_text(), 'unattributed historical log')
                self.assertFalse(runtime.exists())
                self.assertEqual([row['path'] for row in self.connection.execute('SELECT * FROM deleted_plan_files')], [str(log)])

    def test_dedicated_collection_without_receipt_is_queued_and_removed(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths, remove_pending_files
        with TemporaryDirectory() as directory:
            state = Path(directory)
            collection = state / 'agent-runtime' / 'target-tests'
            collection.mkdir(parents=True)
            (collection / 'result.json').write_text('target evidence')
            self.artifact_call('target', '/repo', 'G-001')
            self.connection.execute('INSERT INTO targeted_test_collections '
                "(invocation_id,directory,device,inode,receipt) VALUES ('target',?,'1','2','receipt')", (str(collection),))
            self.connection.commit()
            paths = artifact_paths(self.connection, '/repo', dict(plan_id='G-001', worktree=None), state)
            self.assertEqual(paths, (collection,))
            deleted_plan_state.forget(self.connection, '/repo', 'G-001', artifacts=paths)
            self.assertEqual(remove_pending_files(self.connection, '/repo', state), set())
            self.assertFalse(collection.exists())

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

    def test_tiered_root_plan_links_preserve_target_and_unknown_ownership_before_erasure(self):
        from loop_manager_internal.maintenance import tiered_review
        from loop_manager_internal.storage.task_calls import CallDeferred
        tiered_review.ensure_schema(self.connection)
        target = dict(path='legacy', destination='src/legacy', root_review_hash='a' * 64,
            status='new_plan', plan_id='G-001', plan_path='docs/plans/[G-001] Old.md',
            plan_hash='b' * 64, reason='Preserve the investigated root tree.')
        self.connection.execute('INSERT INTO tiered_maintenance_root_plan_links VALUES (?,?,?,?,?)',
            ('/repo', 1, 'a' * 40, 'b' * 64, json.dumps([target])))
        self.connection.commit()
        for links in (json.dumps([target]), 'broken', '{}', '[null]',
                json.dumps([{**target, 'future_owner': 'unknown'}]),
                json.dumps([{**target, 'plan_id': 'G-002'}])):
            with self.subTest(links=links):
                self.connection.execute('UPDATE tiered_maintenance_root_plan_links SET links_json=?', (links,))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
        independent = {**target, 'plan_id': 'G-002', 'plan_path': 'docs/plans/[G-002] Dependency.md'}
        self.connection.execute('UPDATE tiered_maintenance_root_plan_links SET links_json=?', (json.dumps([independent]),))
        self.connection.execute('INSERT INTO tiered_maintenance_root_plan_links VALUES (?,?,?,?,?)',
            ('/other', 1, 'a' * 40, 'b' * 64, json.dumps([target])))
        self.connection.commit()
        retained = [dict(row) for row in self.connection.execute('SELECT * FROM tiered_maintenance_root_plan_links')]
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM tiered_maintenance_root_plan_links')], retained)
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))

    def test_completed_cycle_prunes_only_historical_target_root_links(self):
        import sqlite3
        from loop_manager_internal.maintenance import tiered_review
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        from loop_manager_internal.storage.task_calls import CallDeferred
        tiered_review.ensure_schema(self.connection)
        MaintenanceRepository(self.connection)
        target = dict(path='legacy', destination='src/legacy', root_review_hash='a' * 64,
            status='new_plan', plan_id='G-001', plan_path='docs/plans/[G-001] Old.md',
            plan_hash='b' * 64, reason='Preserve the investigated root tree.')
        independent = {**target, 'path': 'other', 'destination': 'src/other',
            'plan_id': 'G-002', 'plan_path': 'docs/plans/[G-002] Dependency.md'}
        self.connection.execute('INSERT INTO maintenance_cycles '
            '(repo,phase,revision,last_cycle_commit,last_cycle_merged_count) VALUES (?,?,?,?,?)',
            ('/repo', 'idle', 4, 'c' * 40, 1))
        for repo, cycle, links in (('/repo', 1, [target, independent]), ('/repo', 2, [target]),
                                   ('/other', 1, [target])):
            self.connection.execute('INSERT INTO tiered_maintenance_root_plan_links VALUES (?,?,?,?,?)',
                (repo, cycle, 'a' * 40, 'b' * 64, json.dumps(links)))
        self.connection.commit()
        for field, value in (('phase', 'refactoring'), ('last_cycle_commit', None),
                ('revision', 2), ('publication_stage', 'prepared'), ('legacy_release_identity', 'release')):
            with self.subTest(field=field):
                original = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
                self.connection.execute('UPDATE maintenance_cycles SET ' + field + '=?', (value,))
                self.connection.commit()
                before = list(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(list(self.connection.iterdump()), before)
                self.connection.execute('UPDATE maintenance_cycles SET ' + field + '=?', (original[field],))
                self.connection.commit()
        original = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        foreign = dict(self.connection.execute("SELECT * FROM tiered_maintenance_root_plan_links WHERE repo='/other'").fetchone())
        self.connection.execute("CREATE TRIGGER interrupt_root_erasure BEFORE DELETE ON exec_plans "
                                "BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        self.connection.commit()
        before = list(self.connection.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)
        self.connection.execute('DROP TRIGGER interrupt_root_erasure')
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        links = self.connection.execute("SELECT * FROM tiered_maintenance_root_plan_links WHERE repo='/repo'").fetchall()
        self.assertEqual(len(links), 1)
        self.assertEqual((links[0]['cycle'], links[0]['assessment_digest'], json.loads(links[0]['links_json'])),
                         (1, 'b' * 64, [independent]))
        self.assertEqual(dict(self.connection.execute("SELECT * FROM tiered_maintenance_root_plan_links WHERE repo='/other'").fetchone()), foreign)
        current = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        self.assertEqual(current, {**original, 'revision': 5, 'updated_at': current['updated_at']})
        before = list(self.connection.iterdump())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)

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
        with self.assertRaisesRegex(CallDeferred, '마지막 대상 연결'):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone()), before)

    def test_last_cancelled_cycle_link_survives_until_verified_completion_before_erasure(self):
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        from loop_manager_internal.maintenance import cycle_service, maintenance_orchestration
        store = MaintenanceRepository(self.connection)
        self.connection.execute('INSERT INTO maintenance_cycles '
            '(repo,phase,refactoring_plan_ids,last_success_commit,last_success_merged_count) '
            "VALUES ('/repo','refactoring','[\"G-001\"]','original-assessment',0)")
        report = self.assessment_report(('G-001',))
        self.connection.execute('UPDATE maintenance_cycles SET full_assessment_source_sha=?,full_assessment_result=?',
                                ('a' * 40, report))
        self.connection.commit()
        text = self.rowless_cancellation()
        before = tuple(self.connection.iterdump())
        self.assertEqual(self.cleanup_rowless(text), ())
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.assertEqual(store.get('/repo').cancelled_scope_plan_ids, ('G-001',))
        source = 'c' * 40
        self.connection.execute('INSERT INTO execplan_catalog_views(repo,revision_sha,fingerprint,validation_kind) '
            "VALUES (?,?,?,'full') ON CONFLICT(repo) DO UPDATE SET revision_sha=excluded.revision_sha",
            ('/repo', source, 'confirmed-catalog'))
        self.connection.commit()
        count = store._merged_count('/repo')
        effects = cycle_service.AdvanceMaintenanceCycleAfterMergesEffects(
            maintenance_orchestration.all_cycle_plans_merged, maintenance_orchestration.cycle_plan_status_error)
        completed = cycle_service._advance_maintenance_cycle_after_merges(effects, self.connection, '/repo', source)
        self.assertEqual(completed.phase.value, 'idle')
        self.assertEqual((completed.last_cycle_commit, completed.last_cycle_merged_count), (source, count))
        self.assertEqual((completed.last_success_commit, completed.last_success_merged_count), ('original-assessment', 0))
        self.assertEqual(self.cleanup_rowless(text), ('G-001',))
        after = store.get('/repo')
        self.assertEqual(json.loads(after.full_assessment_result), {**json.loads(report), 'findings': []})
        self.assertEqual(after.phase.value, 'idle')
        self.assertEqual(after.refactoring_plan_ids, ())
        self.assertEqual(after.cancelled_scope_plan_ids, ())
        self.assertEqual((after.last_cycle_commit, after.last_cycle_merged_count), (source, count))
        self.assertIsNone(self.connection.execute("SELECT 1 FROM execplan_cancellations WHERE plan_id='G-001'").fetchone())
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))
        self.assertEqual(self.cleanup_rowless(text), ())

    def assessment_report(self, plan_ids):
        from loop_manager_internal.maintenance.maintenance_validation import parse_assessment_result
        report = {'source_revision': 'a' * 40,
            'coverage': {key: 'Preserved historical coverage evidence.' for key in ('code', 'tests', 'documents')},
            'findings': [{'id': 'historical-issue', 'kind': 'code', 'path': 'module.py',
                'evidence': 'Historical issue is linked to the recorded work.', 'status': 'existing_plan',
                'plan_ids': list(plan_ids), 'source_hash': 'b' * 64}]}
        return parse_assessment_result('FULL_ASSESSMENT_RESULT: ' + json.dumps(report),
            {'module.py': 'b' * 64}, 'a' * 40, (), set(plan_ids))

    def test_unconfirmed_assessment_without_cycle_membership_keeps_erasure_pending(self):
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        from loop_manager_internal.storage.task_calls import CallDeferred
        MaintenanceRepository(self.connection)
        self.connection.execute('INSERT INTO maintenance_cycles '
            '(repo,full_assessment_source_sha,full_assessment_result) VALUES (?,?,?)',
            ('/repo', 'a' * 40, self.assessment_report(('G-001',))))
        self.connection.commit()
        for result, source in (('broken', 'a' * 40), ('{}', 'a' * 40),
                (self.assessment_report(('G-002',)), 'c' * 40),
                (self.assessment_report(('G-002',)), None)):
            with self.subTest(result=result, source=source):
                self.connection.execute('UPDATE maintenance_cycles SET full_assessment_result=?,full_assessment_source_sha=?',
                                        (result, source))
                self.connection.commit()
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_exclusive_assessment_finding_is_erased_without_resolving_independent_findings(self):
        import sqlite3
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        MaintenanceRepository(self.connection)
        report = json.loads(self.assessment_report(('G-001',)))
        independent = {**report['findings'][0], 'id': 'independent-issue', 'plan_ids': ['G-002']}
        report['findings'].append(independent)
        self.connection.execute('INSERT INTO maintenance_cycles '
            '(repo,phase,revision,last_success_commit,last_success_merged_count,full_assessment_source_sha,full_assessment_result) '
            'VALUES (?,?,?,?,?,?,?)', ('/repo', 'idle', 7, 'old-success', 2, 'a' * 40, json.dumps(report)))
        self.connection.commit()
        original = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        self.connection.execute("CREATE TRIGGER interrupt_assessment_erasure BEFORE DELETE ON exec_plans "
                                "BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        self.connection.commit()
        before = list(self.connection.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)
        self.connection.execute('DROP TRIGGER interrupt_assessment_erasure')
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        current = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        self.assertEqual(json.loads(current['full_assessment_result']), {**report, 'findings': [independent]})
        self.assertEqual(current, {**original, 'full_assessment_result': current['full_assessment_result'],
            'revision': 8, 'updated_at': current['updated_at']})
        before = list(self.connection.iterdump())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_unrelated_valid_assessment_is_preserved_during_membership_erasure(self):
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        MaintenanceRepository(self.connection)
        report = self.assessment_report(('G-002',))
        self.connection.execute('INSERT INTO maintenance_cycles '
            '(repo,phase,revision,refactoring_plan_ids,full_assessment_source_sha,full_assessment_result) '
            'VALUES (?,?,?,?,?,?)', ('/repo', 'refactoring', 7, json.dumps(['G-001', 'G-002']), 'a' * 40, report))
        self.connection.commit()
        original = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        current = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        self.assertEqual(current, {**original, 'refactoring_plan_ids': '["G-002"]',
            'revision': 8, 'updated_at': current['updated_at']})
        before = tuple(self.connection.iterdump())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_shared_assessment_erasure_removes_only_target_link_and_preserves_finding(self):
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        MaintenanceRepository(self.connection)
        report = self.assessment_report(('G-001', 'G-002'))
        self.connection.execute('INSERT INTO maintenance_cycles '
            '(repo,phase,revision,full_assessment_completed,full_assessment_source_sha,full_assessment_result) '
            'VALUES (?,?,?,?,?,?)', ('/repo', 'refactoring', 7, 1, 'a' * 40, report))
        self.connection.commit()
        original = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        independent = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        current = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        self.assertEqual(json.loads(current['full_assessment_result']),
            {**json.loads(report), 'findings': [{**json.loads(report)['findings'][0], 'plan_ids': ['G-002']}]})
        self.assertEqual(current, {**original, 'full_assessment_result': current['full_assessment_result'],
            'revision': 8, 'updated_at': current['updated_at']})
        self.assertEqual(dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()), independent)
        before = tuple(self.connection.iterdump())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_shared_assessment_erasure_preserves_unknown_or_conflicting_links_and_rolls_back(self):
        import sqlite3
        from loop_manager_internal.storage.maintenance_state import MaintenanceRepository
        from loop_manager_internal.storage.task_calls import CallDeferred
        MaintenanceRepository(self.connection)
        report = self.assessment_report(('G-001', 'G-002'))
        self.connection.execute('INSERT INTO maintenance_cycles '
            '(repo,full_assessment_source_sha,full_assessment_result) VALUES (?,?,?)', ('/repo', 'a' * 40, report))
        self.connection.commit()
        for sql in ("DELETE FROM exec_plans WHERE plan_id='G-002'",
                    "INSERT INTO deleted_plan_ids VALUES ('/repo','G-002')",
                    "UPDATE maintenance_cycles SET publication_stage='prepared'"):
            with self.subTest(sql=sql):
                self.connection.execute(sql)
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
                self.connection.rollback()
        self.connection.execute("CREATE TRIGGER reject_assessment_erasure BEFORE DELETE ON exec_plans "
            "BEGIN SELECT RAISE(ABORT,'interrupted assessment erasure'); END")
        self.connection.commit()
        before = tuple(self.connection.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'interrupted assessment erasure'):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_shared_assessment_all_cancelled_tasks_erase_sequentially_after_cycle_finish(self):
        from loop_manager_internal.storage import maintenance_state, planning_publication_state, runtime_state
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        from loop_manager_internal.planning.planning_catalog import PlanDeletion
        maintenance_state.MaintenanceRepository(self.connection)
        report = self.assessment_report(('G-001', 'G-002'))
        self.connection.execute('INSERT INTO maintenance_cycles '
            '(repo,phase,revision,last_cycle_commit,last_cycle_merged_count,full_assessment_source_sha,full_assessment_result) '
            "VALUES ('/repo','idle',9,?,0,?,?)", ('c' * 40, 'a' * 40, report))
        self.connection.execute('UPDATE projects SET phase=NULL,plan_path=NULL,branch=NULL,worktree=NULL,worker_session_id=NULL,pr_url=NULL')
        fields = planning_publication_state._MUTABLE_READY_EVIDENCE_FIELDS
        self.connection.execute('UPDATE exec_plans SET ' + ','.join(field + '=NULL' for field in fields)
            + ",status='READY',ever_started=0,workspace_owned=0,retry_count=0,pr_conflict_recovery=0")
        deletions = []
        for key in ('G-001', 'G-002'):
            row = self.connection.execute('SELECT * FROM exec_plans WHERE plan_id=?', (key,)).fetchone()
            snapshot = ReadyPlanSnapshot(key, row['path'], row['title'], tuple(json.loads(row['sources'])), (),
                'low', 'low', row['priority'], row['content_hash'], row['plan_sha'], self.fixture._text(key, row['title']),
                current_complexity=row['current_complexity'], current_risk=row['current_risk'], current_metadata_recorded=True)
            deletions.append(PlanDeletion(snapshot, {'decision': {'id': key, 'source': snapshot.sources[0],
                'outcome': 'withdrawn', 'reason': 'Both linked goals were withdrawn.'}}))
        self.connection.commit()
        with runtime_state._transaction(self.connection):
            planning_publication_state.apply_mutable_catalog_changes(self.connection, '/repo', deletions=tuple(deletions),
                revisions=(), snapshot_token='withdrawn', plan_commit='b' * 40, content_hash=lambda _: '', agent=False)
        for key, tokens in (('G-001', 100), ('G-002', 200)):
            self.artifact_call('call-' + key, '/repo', key)
            self.connection.execute('UPDATE agent_invocations SET input_tokens=? WHERE plan_id=?', (tokens, key))
        self.artifact_call('independent-call', '/other', 'G-001')
        self.connection.commit()
        cycle = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        surviving_audit = dict(self.connection.execute("SELECT * FROM execplan_cancellations WHERE plan_id='G-002'").fetchone())
        surviving_call = dict(self.connection.execute("SELECT * FROM agent_invocations WHERE plan_id='G-002'").fetchone())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        current = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        self.assertEqual(json.loads(current['full_assessment_result']),
            {**json.loads(report), 'findings': [{**json.loads(report)['findings'][0], 'plan_ids': ['G-002']}]})
        self.assertEqual(current, {**cycle, 'full_assessment_result': current['full_assessment_result'],
            'revision': 10, 'updated_at': current['updated_at']})
        self.assertEqual(dict(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone()), surviving_audit)
        self.assertEqual(dict(self.connection.execute("SELECT * FROM agent_invocations WHERE repo='/repo'").fetchone()), surviving_call)
        deleted_plan_state.forget(self.connection, '/repo', 'G-002')
        current = dict(self.connection.execute('SELECT * FROM maintenance_cycles').fetchone())
        self.assertEqual(json.loads(current['full_assessment_result']), {**json.loads(report), 'findings': []})
        self.assertEqual(current, {**cycle, 'full_assessment_result': current['full_assessment_result'],
            'revision': 11, 'updated_at': current['updated_at']})
        for table in ('execplan_cancellations', 'agent_invocations'):
            self.assertIsNone(self.connection.execute(f"SELECT 1 FROM {table} WHERE repo='/repo'").fetchone())
        self.assertEqual({row[0] for row in self.connection.execute(
            "SELECT plan_id FROM deleted_plan_ids WHERE repo='/repo'")}, {'G-001', 'G-002'})
        self.assertEqual(self.connection.execute('SELECT invocation_id FROM agent_invocations').fetchone()[0], 'independent-call')
        before = tuple(self.connection.iterdump())
        for key in ('G-001', 'G-002'):
            deleted_plan_state.forget(self.connection, '/repo', key)
        self.assertEqual(tuple(self.connection.iterdump()), before)

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

    def test_legacy_planning_notice_erases_only_exclusive_target_history(self):
        from loop_manager_internal.notifications import notifications
        from loop_manager_internal.notifications.rendering import _message
        notifications.ensure_schema(self.connection)
        for key, repo, plan_id in (('target', '/repo', 'G-001'), ('independent', '/repo', 'G-002'),
                                   ('foreign', '/other', 'G-001')):
            payload = dict(project=repo, plans=[dict(id=plan_id, title='historical plan')])
            self.connection.execute('INSERT INTO notification_outbox '
                '(event_key,repo,event_type,payload,rendered_text,confirmed_pieces,last_error) '
                "VALUES (?,?,'planning_result',?,?,1,'saved error')", (key, repo, json.dumps(payload), _message('planning_result', payload)))
        self.connection.commit()
        retained = [dict(row) for row in self.connection.execute("SELECT * FROM notification_outbox WHERE event_key<>'target'")]
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM notification_outbox')], retained)
        self.assertTrue(execplan_catalog_state.is_cancelled(self.connection, '/repo', 'G-001'))

    def test_partial_legacy_planning_notice_erases_only_unconfirmed_target_suffix(self):
        from loop_manager_internal.notifications import notifications
        from loop_manager_internal.notifications.rendering import _message, _pieces
        from loop_manager_internal.storage.task_calls import CallDeferred
        notifications.ensure_schema(self.connection)
        retained_plan = dict(id='G-002', title='independent title ' * 400)
        target_plan = dict(id='G-001', title='withdrawn title')
        payload = dict(project='repo', plans=[retained_plan, target_plan], status='작성 완료 · 미게시')
        original = _message('planning_result', payload)
        retained = {**payload, 'plans': [retained_plan]}
        revised = _message('planning_result', retained)
        self.assertGreater(len(_pieces(original)), 1)
        self.assertEqual(_pieces(original)[0], _pieces(revised)[0])
        self.connection.execute('INSERT INTO notification_outbox '
            '(event_key,repo,event_type,payload,rendered_text,confirmed_pieces,attempts,last_error) '
            "VALUES ('legacy-list','/repo','planning_result',?,?,1,2,'saved error')", (json.dumps(payload), original))
        self.connection.commit()
        baseline = dict(self.connection.execute("SELECT * FROM notification_outbox WHERE event_key='legacy-list'").fetchone())
        for changes in ({'plans': [target_plan, retained_plan]}, {'execplan_id': 'G-001'}, {'started_execplan_id': 'G-001'}):
            with self.subTest(changes=changes):
                changed = {**payload, **changes}
                self.connection.execute("UPDATE notification_outbox SET payload=?,rendered_text=? WHERE event_key='legacy-list'",
                    (json.dumps(changed), _message('planning_result', changed)))
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
                self.connection.rollback()
        for assignment in ("delivery_started_at='in-flight'", "canceled_at='canceled'", 'last_error=NULL'):
            with self.subTest(assignment=assignment):
                self.connection.execute('UPDATE notification_outbox SET ' + assignment + " WHERE event_key='legacy-list'")
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
                self.connection.rollback()
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        current = dict(self.connection.execute("SELECT * FROM notification_outbox WHERE event_key='legacy-list'").fetchone())
        self.assertEqual(current, {**baseline, 'payload': json.dumps(retained, ensure_ascii=False), 'rendered_text': revised})
        self.assertNotIn('G-001', current['payload'] + current['rendered_text'])
        self.assertEqual(_pieces(current['rendered_text'])[:current['confirmed_pieces']], _pieces(original)[:1])
        self.assertIsNone(current['sent_at'])
        before = tuple(self.connection.iterdump())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_failed_unconfirmed_publication_notice_removes_target_after_claim_returns(self):
        from unittest import mock
        from loop_manager_internal.notifications import notifications
        from loop_manager_internal.notifications.rendering import _message
        from loop_manager_internal.storage.task_calls import CallDeferred
        notifications.ensure_schema(self.connection)
        plans = []
        for row in self.connection.execute('SELECT * FROM exec_plans ORDER BY plan_id').fetchall():
            self.connection.execute('UPDATE exec_plans SET publication_path=path,publication_sha=? WHERE plan_id=?',
                                    ('a' * 40, row['plan_id']))
            plans.append(dict(id=row['plan_id'], title=row['title'], source_path=row['path'], waiting_for=[]))
        payload = dict(project='repo', publication_revision='a' * 40, plans=plans)
        self.connection.execute('INSERT INTO notification_outbox (event_key,repo,event_type,payload) '
            "VALUES ('failed-list','/repo','plans_published',?)", (json.dumps(payload),))
        self.connection.commit()
        def fail_during_send(_token, _chat, text):
            self.assertEqual(text, _message('plans_published', payload))
            before = tuple(self.connection.iterdump())
            with self.assertRaises(CallDeferred):
                deleted_plan_state.forget(self.connection, '/repo', 'G-001')
            self.assertEqual(tuple(self.connection.iterdump()), before)
            raise RuntimeError('mock transport failure')
        self.assertEqual(notifications.dispatch_pending(self.connection, 'token', 'chat', adapter=fail_during_send), 0)
        original = dict(self.connection.execute("SELECT * FROM notification_outbox WHERE event_key='failed-list'").fetchone())
        self.assertEqual((original['attempts'], original['confirmed_pieces']), (1, 0))
        self.assertIsNone(original['delivery_started_at'])
        self.assertTrue(original['last_error'])
        # Cancellation can clear a claim before HTTP returns; it is not a handoff.
        for assignment in ("canceled_at='canceled'", 'confirmed_pieces=1',
                           "rendered_text='legacy body'", 'attempts=0', 'last_error=NULL', "last_error=''"):
            with self.subTest(assignment=assignment):
                self.connection.execute('UPDATE notification_outbox SET ' + assignment + " WHERE event_key='failed-list'")
                before = tuple(self.connection.iterdump())
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
                self.connection.rollback()
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        retained = {**payload, 'plans': [plans[1]]}
        current = dict(self.connection.execute("SELECT * FROM notification_outbox WHERE event_key='failed-list'").fetchone())
        self.assertEqual(current, {**original, 'payload': json.dumps(retained, ensure_ascii=False),
                                   'rendered_text': _message('plans_published', retained)})
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(self.connection, 'token', 'chat', adapter=sender), 1)
        sender.assert_called_once_with('token', 'chat', _message('plans_published', retained))
        self.assertEqual(notifications.dispatch_pending(self.connection, 'token', 'chat', adapter=sender), 0)

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

    def test_active_project_cause_is_preserved_before_final_erasure(self):
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.storage.task_calls import CallDeferred
        scoped_blocking.record_project_block(self.connection, '/repo',
            scoped_blocking.BlockReasonCode.REPOSITORY_STATE_UNSAFE, 'independent safety decision', 'cause',
            plan_id='G-001', previous_phase=None)
        before = list(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)
        from loop_manager_internal.storage.blocked_checkpoint_state import DiagnosticPublicationRepository
        diagnostics = DiagnosticPublicationRepository(self.connection)
        self.connection.execute("UPDATE project_blocks SET plan_id=NULL WHERE repo='/repo'")
        self.connection.commit()
        block = self.connection.execute("SELECT * FROM project_blocks WHERE repo='/repo'").fetchone()
        identity = scoped_blocking.project_cause_identity(block)
        diagnostics.preserve('/repo', 'project', 'G-001', 'saved-project-input', dict(cause_identity=identity))
        before = list(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(list(self.connection.iterdump()), before)
        scoped_blocking.clear_project_block(self.connection, '/repo')
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM diagnostic_preservations').fetchone()[0], 0)
        self.assertIsNone(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())

    def test_confirmed_catalog_merge_survives_missing_execution_row_and_archive_file(self):
        from loop_manager_internal.storage.task_calls import CallDeferred
        self.rowless_cancellation()
        columns = ('repo,plan_id,original_path,observed_path,original_content_hash,final_content_hash,'
                   'plan_sha,merge_confirmed,identity_verified,currently_archived,proof_kind,proof_revision')
        for repo in ('/repo', '/other'):
            self.connection.execute('INSERT INTO execplan_catalog_history (' + columns + ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (repo, 'G-001', 'docs/plans/[G-001] Gone.md', 'docs/plans/archive/[G-001] Gone.md',
                 'original-hash', 'final-hash', 'a' * 40, 1, 1, 1, 'github-pr', 'b' * 40))
        self.connection.commit()
        for archived in (1, 0):
            self.connection.execute("UPDATE execplan_catalog_history SET currently_archived=? WHERE repo='/repo'", (archived,))
            self.connection.commit()
            before = tuple(self.connection.iterdump())
            with self.assertRaises(CallDeferred):
                deleted_plan_state.forget(self.connection, '/repo', 'G-001')
            self.assertEqual(tuple(self.connection.iterdump()), before)
        self.connection.execute("DELETE FROM execplan_catalog_history WHERE repo='/repo'")
        self.connection.commit()
        foreign = dict(self.connection.execute("SELECT * FROM execplan_catalog_history WHERE repo='/other'").fetchone())
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual(dict(self.connection.execute('SELECT * FROM execplan_catalog_history').fetchone()), foreign)

    def test_verified_archive_dependency_is_historical_before_backfill_and_preserved(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from loop_manager_internal.catalog.deleted_plan_cleanup import cleanup
        from loop_manager_internal.catalog.execplan_catalog import plan_content_hash
        text = self.rowless_cancellation()
        archived = self.fixture._text('G-002', 'Dependency', depends_on=('G-001',))
        archive_path = 'docs/plans/archive/[G-002] Dependency.md'
        self.connection.execute("UPDATE exec_plans SET depends_on='[\"G-001\"]',plan_sha=?,pr_url='https://example/pr/2',"
            "pr_head_oid=?,content_hash=? WHERE plan_id='G-002'", ('a' * 40, 'b' * 40, plan_content_hash(archived)))
        self.connection.commit()
        original = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone())
        self.assertIsNone(self.connection.execute("SELECT * FROM execplan_catalog_history WHERE plan_id='G-002'").fetchone())
        entry = SimpleNamespace(path=archive_path, text=archived)
        pinned = SimpleNamespace(regular_paths={archive_path}, revision_sha='c' * 40,
            snapshot=SimpleNamespace(observed_ids={'G-002'}, all={'G-002': entry}, archived={'G-002': entry}, withheld=()))
        from loop_manager_internal.catalog.remote_deletions import historical_reference_ids
        self.assertEqual(historical_reference_ids(self.connection, '/repo', pinned), {'G-002'})
        race = False
        def git(_repo, *args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            return SimpleNamespace(returncode=int(args[0] == 'rev-parse'), stdout='')
        def pr(*_):
            if race:
                self.connection.execute("UPDATE exec_plans SET content_hash='new-observation' WHERE plan_id='G-002'")
                self.connection.commit()
            return None
        with TemporaryDirectory() as directory:
            def run():
                return cleanup(self.connection, '/repo', '/repo', pinned, (), git=git,
                    git_bytes=lambda *_: text.encode(), pr_for_branch=pr, state_dir=Path(directory), revision_is_current=lambda: True)
            before = tuple(self.connection.iterdump())
            entry.text = archived + 'mismatch'
            self.assertEqual(run(), ())
            self.assertEqual(tuple(self.connection.iterdump()), before)
            entry.text = archived
            self.connection.execute("UPDATE exec_plans SET pr_head_oid=NULL WHERE plan_id='G-002'")
            self.connection.commit()
            self.assertEqual(run(), ())
            self.connection.execute("UPDATE exec_plans SET pr_head_oid=? WHERE plan_id='G-002'", ('b' * 40,))
            self.connection.commit()
            race = True
            self.assertEqual(run(), ())
            self.assertIsNotNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())
            self.assertEqual(self.connection.execute("SELECT content_hash FROM exec_plans WHERE plan_id='G-002'").fetchone()[0], 'new-observation')
            self.connection.execute("UPDATE exec_plans SET content_hash=? WHERE plan_id='G-002'", (original['content_hash'],))
            self.connection.commit()
            race = False
            self.connection.execute('INSERT INTO execplan_catalog_history '
                '(repo,plan_id,original_path,observed_path,original_content_hash,final_content_hash,plan_sha,'
                'merge_confirmed,identity_verified,currently_archived,proof_kind,proof_revision) '
                "VALUES ('/repo','G-002',?,?,?,?,?,1,1,0,'github-pr',?)",
                (original['path'], archive_path, original['content_hash'], original['content_hash'], 'a' * 40, 'b' * 40))
            self.connection.execute("UPDATE exec_plans SET path=? WHERE plan_id='G-002'", (archive_path,))
            self.connection.commit()
            original['path'] = archive_path
            history = dict(self.connection.execute("SELECT * FROM execplan_catalog_history WHERE plan_id='G-002'").fetchone())
            pinned.snapshot.observed_ids, pinned.snapshot.all, pinned.snapshot.archived, pinned.regular_paths = set(), {}, {}, set()
            self.assertEqual(historical_reference_ids(self.connection, '/repo', pinned), {'G-002'})
            self.assertEqual(run(), ('G-001',))
            self.assertEqual(dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-002'").fetchone()), original)
            self.assertEqual(dict(self.connection.execute("SELECT * FROM execplan_catalog_history WHERE plan_id='G-002'").fetchone()), history)

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
            snapshot=SimpleNamespace(observed_ids={'G-002'}, all=self.published_withdrawal(), withheld=()))
        references = (SimpleNamespace(plan_id='G-002', source='docs/plans/[G-001] Old.md'),)
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
                    self.assertEqual(cleanup(self.connection, '/repo', '/repo', pinned, references, git=git,
                        git_bytes=lambda *a: b'original', pr_for_branch=lambda *a: {**pr, 'state': state},
                        state_dir=self.fixture.state_dir if hasattr(self.fixture,'state_dir') else '/tmp',
                        revision_is_current=lambda: current), ())
                    self.assertIsNotNone(self.connection.execute("SELECT 1 FROM exec_plans WHERE plan_id='G-001'").fetchone())
            from loop_manager_internal.storage import local_recovery_state
            local_recovery_state.ensure_schema(self.connection)
            self.connection.execute("INSERT INTO local_environment_recovery_requests "
                "(request_id,repo,status,before_state) VALUES ('shared','/repo','completed','{\"unknown\":1}')")
            self.connection.commit()
            self.assertEqual(cleanup(self.connection, '/repo', '/repo', pinned, references, git=git,
                git_bytes=lambda *a: b'original', pr_for_branch=lambda *a: pr, state_dir='/tmp',
                revision_is_current=lambda: True), ())
            self.assertIsNotNone(self.connection.execute("SELECT 1 FROM exec_plans WHERE plan_id='G-001'").fetchone())
            self.connection.execute("UPDATE local_environment_recovery_requests SET before_state='{}'")
            self.connection.commit()
            self.assertEqual(cleanup(self.connection, '/repo', '/repo', pinned, references, git=git,
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

    def test_erasure_requires_known_execution_status_and_nonempty_start_and_handoff(self):
        from loop_manager_internal.storage.task_calls import CallDeferred, pending_for_plan
        self.artifact_call('target-call', '/repo', 'G-001')
        self.artifact_call('independent-call', '/other', 'G-001')
        self.connection.commit()
        independent = dict(self.connection.execute("SELECT * FROM agent_invocations WHERE invocation_id='independent-call'").fetchone())
        for field, value in (('execution_status', 'unknown'), ('execution_status', 'prepared'),
                ('execution_status', ''), ('execution_status', 'legacy-finished'),
                ('execution_started_at', None), ('execution_started_at', ''), ('finished_at', '')):
            with self.subTest(field=field, value=value):
                self.connection.execute(f'UPDATE agent_invocations SET {field}=? WHERE invocation_id=?', (value, 'target-call'))
                before = tuple(self.connection.iterdump())
                self.assertTrue(pending_for_plan(self.connection, '/repo', 'G-001'))
                self.assertFalse(pending_for_plan(self.connection, '/other', 'G-001'))
                with self.assertRaises(CallDeferred):
                    deleted_plan_state.forget(self.connection, '/repo', 'G-001')
                self.assertEqual(tuple(self.connection.iterdump()), before)
                self.connection.rollback()
        self.assertFalse(pending_for_plan(self.connection, '/repo', 'G-001'))
        deleted_plan_state.forget(self.connection, '/repo', 'G-001')
        self.assertEqual([dict(row) for row in self.connection.execute('SELECT * FROM agent_invocations')], [independent])

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
                self.assertEqual(remove_pending_files(self.connection, '/repo', directory), {'G-001'})
            self.assertTrue(artifact.exists())
            self.assertEqual(self.connection.execute('SELECT count(*) FROM deleted_plan_files').fetchone()[0], 1)
            remove_pending_files(self.connection, '/repo', directory)
            self.assertFalse(artifact.exists())
            self.assertEqual(self.connection.execute('SELECT count(*) FROM deleted_plan_files').fetchone()[0], 0)
            outside = Path(directory) / 'outside'
            outside.write_text('unowned bytes')
            independent = Path(directory) / 'logs' / 'independent-file'
            independent.write_text('finished independent task')
            self.connection.executemany('INSERT INTO deleted_plan_files VALUES (?,?,?)', (
                ('/repo', 'G-001', str(outside)), ('/repo', 'G-999', str(independent))))
            self.connection.commit()
            self.assertEqual(remove_pending_files(self.connection, '/repo', directory), {'G-001'})
            self.assertEqual(outside.read_text(), 'unowned bytes')
            self.assertFalse(independent.exists())
            self.assertEqual([tuple(row) for row in self.connection.execute('SELECT * FROM deleted_plan_files')],
                             [('/repo', 'G-001', str(outside))])

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


class DeletedDiagnosticArtifactTests(ArtifactRecoveryFixture, unittest.TestCase):
    def test_verified_bundle_erasure_preserves_shared_and_late_ref_owners(self):
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths, remove_pending_files
        from loop_manager_internal.storage import deleted_diagnostic_state
        from loop_manager_internal.storage.blocked_checkpoint_state import DiagnosticPublicationRepository
        from loop_manager_internal.storage.task_calls import CallDeferred
        deleted_plan_state.ensure_schema(self.connection)
        self.connection.commit()
        store = DiagnosticPublicationRepository(self.connection)
        target = dict(plan_id='G-047', worktree=None)
        paths = artifact_paths(self.connection, 'repo', target, self.state)
        self.assertEqual(paths, (self.bundle,))
        payload = json.loads(self.saved['payload'])
        store.preserve('other', 'plan', 'G-999', self.identity, payload,
                       ref=self.saved['ref'], tree=self.saved['tree_oid'], commit=self.saved['commit_oid'])
        before = list(self.connection.iterdump())
        with self.assertRaises(CallDeferred):
            artifact_paths(self.connection, 'repo', target, self.state)
        self.assertEqual(list(self.connection.iterdump()), before)
        # Releasing the independent pointer makes only this owned bundle eligible.
        self.connection.execute("DELETE FROM diagnostic_preservations WHERE repo='other'")
        self.connection.execute("DELETE FROM diagnostic_ref_uses WHERE repo='other'")
        self.connection.commit()
        proof = deleted_diagnostic_state.capture(self.connection, 'repo', 'G-047')
        deleted_plan_state.forget(self.connection, 'repo', 'G-047', artifacts=paths, diagnostic_snapshot=proof)
        self.assertTrue(self.bundle.exists())
        # A late typed use protects the file queue even without a preservation row.
        use = dict(proof['owners']['diagnostic_ref_uses'][0])
        use.update(repo='other', owner='G-999')
        self.connection.execute('INSERT INTO diagnostic_ref_uses (' + ','.join(use) + ') VALUES ('
            + ','.join('?' for _ in use) + ')', tuple(use.values()))
        self.connection.commit()
        self.assertEqual(remove_pending_files(self.connection, 'repo', self.state), {'G-047'})
        self.assertTrue(self.bundle.exists())
        self.connection.execute("DELETE FROM diagnostic_ref_uses WHERE repo='other'")
        self.connection.commit()
        self.assertEqual(remove_pending_files(self.connection, 'repo', self.state), set())
        self.assertFalse(self.bundle.exists())
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('repo', 'G-047'))
        before = list(self.connection.iterdump())
        with self.assertRaises(RuntimeError):
            store.preserve('repo', 'plan', 'G-047', self.identity, payload)
        self.assertEqual(list(self.connection.iterdump()), before)

    def test_diagnostic_scope_records_and_bundles_retire_only_after_current_ref_absence(self):
        from unittest import mock
        from loop_manager_internal.catalog import deleted_plan_cleanup, execplan_catalog as catalog
        from loop_manager_internal.storage import runtime_state, planning_publication_state, deleted_diagnostic_state, task_calls
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        from loop_manager_internal.storage.blocked_checkpoint_state import DiagnosticPublicationRepository
        from loop_manager_internal.planning.planning_catalog import PlanDeletion
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        from loop_manager_internal.recovery import diagnostic_cleanup
        task_calls.ensure_schema(self.connection)
        ManualWorkRepository(self.connection)
        deleted_plan_state.ensure_schema(self.connection)
        store = DiagnosticPublicationRepository(self.connection)
        payload = json.loads(self.saved['payload'])
        store.preserve('repo', 'plan', 'G-047', self.identity, {**payload, 'result': 'local_only'},
            ref=self.saved['ref'], tree=self.saved['tree_oid'], commit=self.saved['commit_oid'], retryable=True)
        store.preserve('repo', 'plan', 'G-047', self.identity, payload,
            ref=self.saved['ref'], tree=self.saved['tree_oid'], commit=self.saved['commit_oid'])
        former = self.state / 'worktrees' / 'previous' / 'G-047'
        self.connection.execute('INSERT INTO diagnostic_cause_resolutions VALUES (?,?,?,?,?,?,?,?)',
            ('repo', 'plan', 'G-047', 'cause', '', 'branch', str(former),
             json.dumps({'cause': self.manifest['detail'], 'acceptance': None, 'pending_work': False})))
        text = '---\nid: G-047\ntitle: Retired\nsources:\n  - docs/PRODUCT.md\ndepends_on: []\ncomplexity: low\nrisk: low\npriority: normal\n---\n\n## Purpose\nRetired goal.\n'
        path = 'docs/plans/[G-047] Retired.md'
        from loop_manager_internal.storage.planning_publication_state import _MUTABLE_READY_EVIDENCE_FIELDS
        self.connection.execute('UPDATE exec_plans SET ' + ','.join(field + '=NULL' for field in _MUTABLE_READY_EVIDENCE_FIELDS)
            + ",status='READY',ever_started=0,workspace_owned=0,retry_count=0,pr_conflict_recovery=0,depends_on='[]',"
            "path=?,title='Retired',sources='[\"docs/PRODUCT.md\"]',priority='normal',content_hash=?,plan_sha=?,current_revision_sha=?,"
            "current_complexity='low',current_risk='low' WHERE plan_id='G-047'", (path, catalog.plan_content_hash(text), 'a' * 40, 'a' * 40))
        self.connection.execute('UPDATE projects SET plan_path=NULL,worktree=NULL,branch=NULL,phase=NULL')
        self.connection.commit()
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-047'").fetchone()
        plan = ReadyPlanSnapshot('G-047', path, 'Retired', ('docs/PRODUCT.md',), (), 'low', 'low', 'normal',
            row['content_hash'], 'a' * 40, text, current_complexity='low', current_risk='low', current_metadata_recorded=True)
        with runtime_state._transaction(self.connection):
            planning_publication_state.apply_mutable_catalog_changes(self.connection, 'repo',
                deletions=(PlanDeletion(plan, {'decision': dict(id='G-047', source='docs/PRODUCT.md',
                    outcome='withdrawn', reason='The original goal was withdrawn.')}),),
                revisions=(), snapshot_token='withdrawn', plan_commit='b' * 40,
                content_hash=lambda _: '', agent=False)
        # Preserve the real native failed preparation/attempt; no rebase success is invented.
        effects = diagnostic_cleanup.CleanupEffects(*[mock.Mock(side_effect=AssertionError('no actual Git/Agent')) for _ in range(6)])
        diagnostic_cleanup.prepare_recorded(effects, self.connection, self.state, 'repo')
        state = diagnostic_cleanup._snapshot(self.connection, 'repo', self.saved['ref'])
        diagnostic_cleanup._record_attempt(self.connection, state, {}, 'held', reason='source owner is now withdrawn')
        self.connection.commit()
        pinned = SimpleNamespace(regular_paths=set(), revision_sha='c' * 40,
            snapshot=SimpleNamespace(observed_ids=set(), all={}, withheld=()))
        remote, local, failed, raced = True, False, False, False
        def git(_repo, *args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            if 'ls-remote' in args and args[-1] == 'refs/heads/' + self.saved['ref']:
                return SimpleNamespace(returncode=int(failed), stdout=self.saved['commit_oid'] if remote else '')
            return SimpleNamespace(returncode=int(args[0] == 'rev-parse' and not (local and args[-1] == 'refs/heads/' + self.saved['ref'])), stdout='')
        def pull_request(_repo, branch, *_args):
            nonlocal raced
            if raced and branch == self.saved['ref']:
                raced = False
                self.connection.execute("UPDATE diagnostic_ref_uses SET generation=generation+1 WHERE repo='repo'")
                self.connection.commit()
            return None
        def run():
            return deleted_plan_cleanup.cleanup(self.connection, '/repo', 'repo', pinned, (), git=git,
                git_bytes=lambda *_: text.encode(), pr_for_branch=pull_request, state_dir=self.state,
                revision_is_current=lambda: True)
        before = list(self.connection.iterdump())
        self.assertEqual(run(), ())
        self.assertEqual(list(self.connection.iterdump()), before)
        remote, local = False, True
        self.assertEqual(run(), ())
        self.assertEqual(list(self.connection.iterdump()), before)
        local, failed = False, True
        self.assertEqual(run(), ())
        self.assertEqual(list(self.connection.iterdump()), before)
        failed, raced = False, True
        self.assertEqual(run(), ())
        self.assertTrue(self.bundle.exists())
        self.assertIsNone(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone())
        # Mixed native history cannot be erased as if it belonged to this owner alone.
        attempt = self.connection.execute('SELECT * FROM diagnostic_cleanup_attempts').fetchone()
        mixed = json.loads(attempt['payload'])
        mixed['state']['uses'].append({**mixed['state']['uses'][0], 'owner': 'G-999'})
        self.connection.execute('UPDATE diagnostic_cleanup_attempts SET payload=?', (json.dumps(mixed),))
        self.connection.commit()
        shared = list(self.connection.iterdump())
        self.assertEqual(run(), ())
        self.assertEqual(list(self.connection.iterdump()), shared)
        self.connection.execute('UPDATE diagnostic_cleanup_attempts SET payload=?', (attempt['payload'],))
        self.connection.commit()
        self.assertEqual(run(), ('G-047',))
        self.assertFalse(self.bundle.exists())
        for table in (*deleted_diagnostic_state.OWNER_TABLES, *deleted_diagnostic_state.REF_TABLES, 'execplan_cancellations'):
            self.assertIsNone(self.connection.execute('SELECT 1 FROM ' + table).fetchone(), table)
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('repo', 'G-047'))
        self.assertEqual(run(), ())

    def test_final_marker_resumes_legacy_diagnostic_owner_cleanup_without_recreating_declaration(self):
        from unittest import mock
        from loop_manager_internal.catalog import deleted_plan_cleanup
        from loop_manager_internal.storage import runtime_state
        deleted_plan_state.ensure_schema(self.connection)
        with runtime_state._transaction(self.connection):
            self.connection.execute("DELETE FROM exec_plans WHERE repo='repo' AND plan_id='G-047'")
            self.connection.execute("INSERT INTO deleted_plan_ids VALUES ('repo','G-047')")
        pinned = SimpleNamespace(regular_paths=set(), revision_sha='c' * 40,
            snapshot=SimpleNamespace(observed_ids=set(), all={}, withheld=()))
        read_text = mock.Mock(side_effect=AssertionError('erased declaration must not be recreated'))
        present = True
        def git(_repo, *args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            return SimpleNamespace(returncode=int(args[0] == 'rev-parse'),
                stdout=self.saved['commit_oid'] if args[0] == 'ls-remote' and present else '')
        def run():
            return deleted_plan_cleanup.cleanup(self.connection, '/repo', 'repo', pinned, (), git=git,
                git_bytes=read_text, pr_for_branch=lambda *_: None, state_dir=self.state,
                revision_is_current=lambda: True)
        before = list(self.connection.iterdump())
        self.assertEqual(run(), ())
        self.assertEqual(list(self.connection.iterdump()), before)
        self.assertTrue(self.bundle.exists())
        present = False
        self.assertEqual(run(), ('G-047',))
        read_text.assert_not_called()
        self.assertFalse(self.bundle.exists())
        self.assertIsNone(self.connection.execute('SELECT * FROM diagnostic_ref_uses').fetchone())
        self.assertIsNone(self.connection.execute('SELECT * FROM diagnostic_preservations').fetchone())
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('repo', 'G-047'))
        self.assertEqual(run(), ())

    def test_unverified_bundle_preserves_database_before_erasure(self):
        from loop_manager_internal.catalog.deleted_plan_cleanup import artifact_paths
        from loop_manager_internal.storage.task_calls import CallDeferred
        before = list(self.connection.iterdump())
        (self.bundle / 'manifest.json').write_text('{}')
        with self.assertRaises(CallDeferred):
            artifact_paths(self.connection, 'repo', dict(plan_id='G-047', worktree=None), self.state)
        self.assertEqual(list(self.connection.iterdump()), before)
        self.assertTrue(self.bundle.exists())
