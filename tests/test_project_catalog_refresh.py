"""Unit coverage of the catalog boundary and archive transaction adoption."""
import contextlib
import json
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.storage import execplan_archive_state as archive_state
from loop_manager_internal.catalog import execplan_catalog as catalog
from loop_manager_internal.storage import execplan_catalog_state as state
import loop_manager as manager
from loop_manager_internal.catalog import project_catalog_refresh as refresh
from tests.support import catalog as fixtures


class _CatalogRefreshCase(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.catalog_reconciliation_fixture(self)
        self.connection = self.fixture.connection
        self.repo = str(Path('/repo').resolve())
        self.connection.execute('UPDATE projects SET repo = ?', (self.repo,))
        self.connection.execute('UPDATE exec_plans SET repo = ?', (self.repo,))
        self.connection.commit()
        self.sha = 'b' * 40
        self.loads = []
        self.documents = {
            'docs/plans/[G-001] Updated.md': self.fixture._text(
                'G-001', 'Updated', sources=('docs/requirements/change.md',),
                depends_on=('G-002',), complexity='high', risk='critical', priority='critical'),
            'docs/plans/[G-002] Dependency.md': self.fixture._text('G-002', 'Dependency'),
        }
        self.paths = frozenset((*self.documents, 'docs/PRODUCT.md', 'docs/requirements/change.md'))
        self.boundary = refresh.ProjectCatalogRefresh(
            self.connection, self.repo, lambda: self.sha, self.reconcile)

    def pinned(self, revision):
        return state.PinnedCatalog(revision, catalog.snapshot_from_documents(
            self.documents, validate_sources=True, available_regular_paths=self.paths,
            revision_sha=revision), self.paths)

    def reconcile(self, revision, remember):
        self.loads.append(revision)
        pinned = self.pinned(revision)
        result = state.CatalogReconciliationService(
            state.DefinitionStateRepository(self.connection), fixtures.FakeDefinitionPort(pinned)
        ).reconcile(self.repo)
        if result.usable:
            remember(result.catalog or pinned)
        return result

    def row(self, plan='G-001'):
        return self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                       (self.repo, plan)).fetchone()

    def prepare_planning_workspace(self):
        from tests.support.planning_case import FakePorts, planning_snapshot
        from loop_manager_internal.planning.planning_runtime import (
            PlanningContext, PlanningLifecycleCoordinator,
        )
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha=self.sha, worktree="/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result_completed=True,
        ))
        ports.git.remote = self.sha
        ports.state.reconcile_catalog = mock.Mock(
            side_effect=AssertionError("must reuse the project catalog boundary"),
        )
        context = PlanningContext(
            repo=self.repo, repo_path=Path(self.repo), state_dir=Path("/state"),
            base_branch="main", base_sha=self.sha, provider="codex",
            config_path=Path("/config.ini"), registered_only=False,
            refactoring_review_interval=5, refresh_catalog=self.boundary.refresh,
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator._workspace._prepare_workspace(coordinator._new_run(context))
        ports.state.reconcile_catalog.assert_not_called()
        return outcome, ports

    def archive(self, *, remote=True):
        self.boundary.refresh()
        self.archive_repo = archive_state.ExecPlanArchiveRepository(
            self.connection, apply_catalog=self.boundary.apply_archive)
        self.candidates = self.archive_repo.merged_candidates(self.repo)
        self.base_sha = self.sha
        self.sha = 'c' * 40 if remote else self.sha
        return self.archive_repo.record_success(
            self.repo, self.candidates, base_sha=self.base_sha, operation_id=None,
            commit_oid='c'*40, push_result='pushed:'+'c'*40)

    def run_step(self, *, pr=None, phase=None, invalid=False, scope_error=False,
                 expected_priority="critical", expected_sources='["docs/requirements/change.md"]',
                 expected_dependencies='["G-002"]'):
        self.connection.execute("UPDATE projects SET phase=?, agent_snapshot=NULL, branch=NULL, worktree=NULL, plan_path=NULL WHERE repo=?", (phase, self.repo))
        self.connection.execute("UPDATE exec_plans SET status='READY', branch=NULL, worktree=NULL, pr_url=NULL WHERE repo=?", (self.repo,))
        if scope_error:
            self.connection.execute("UPDATE projects SET agent_snapshot='saved' WHERE repo=?", (self.repo,))
        self.connection.commit()
        self.events = []
        def reconcile(revision, remember):
            self.events.append('catalog')
            if invalid:
                return state.CatalogReconciliationService(
                    state.DefinitionStateRepository(self.connection), fixtures.FakeDefinitionPort(
                        replace(self.pinned(revision), snapshot=catalog.ExecPlanCatalogSnapshot({}, {})))
                ).reconcile(self.repo)
            return self.reconcile(revision, remember)
        self.boundary.reconcile = reconcile
        def pull_requests(*args, **kwargs):
            self.events.append('pr')
            self.assertEqual(self.row()['priority'], expected_priority)
            if pr:
                pr()
            kwargs['after_reconciliation']()
            return SimpleNamespace(step_result=0)
        def safe(*args, **kwargs):
            self.events.append('archive-eligibility')
            self.assertEqual(self.row()['sources'], expected_sources)
            return False
        def planning(*args):
            self.assertTrue(callable(args[-1].refresh_catalog))
            self.events.append('planning')
            self.assertEqual(self.row()['depends_on'], expected_dependencies)
            return SimpleNamespace(kind=manager.PlanningOutcomeKind.WAITING)
        with contextlib.ExitStack() as stack:
            for name in ('observe_blocked_plan_remotes', 'migrate_legacy_execplans', 'backfill_plan_shas', 'resume_plan_blocks',
                         '_recorded_base_fetch', 'ensure_safe_base_worktree'):
                stack.enter_context(mock.patch.object(manager, name))
            stack.enter_context(mock.patch.object(manager, 'validate_repository', return_value=Path(self.repo)))
            stack.enter_context(mock.patch.object(manager, 'remote_base', side_effect=lambda *a, **k: self.events.append('fetch') or 'main'))
            stack.enter_context(mock.patch.object(manager, 'git', side_effect=lambda *a, **k: SimpleNamespace(stdout=self.sha)))
            stack.enter_context(mock.patch.object(manager, 'recheck_cleanup_pending', return_value=[]))
            stack.enter_context(mock.patch(
                'loop_manager_internal.catalog.archive_service.ArchiveExecutionResources.safe', side_effect=safe))
            stack.enter_context(mock.patch.object(manager, 'synchronize_waiting_pull_requests', side_effect=pull_requests))
            stack.enter_context(mock.patch.object(manager.project_catalog_refresh, 'ProjectCatalogRefresh', return_value=self.boundary))
            stack.enter_context(mock.patch.object(manager.PlanningLifecycleCoordinator, 'run', side_effect=planning))
            stack.enter_context(mock.patch.object(manager, 'validate_persisted_agent_scope', side_effect=RuntimeError('unsafe')))
            return manager._run_project_step(self.connection, Path('/state'), self.repo, Path('unused.ini'), 'main', False)


class CatalogPlanningConsumerTests(_CatalogRefreshCase):
    def test_planning_reuses_validated_project_catalog(self):
        self.boundary.refresh()
        outcome, ports = self.prepare_planning_workspace()
        self.assertIsNone(outcome)
        self.assertEqual(self.loads, [self.sha])
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")

    def test_planning_reuses_proven_archive_catalog_without_reread(self):
        self.assertEqual(self.archive(), archive_state.ArchiveWriteResult.APPLIED)
        outcome, _ports = self.prepare_planning_workspace()
        self.assertIsNone(outcome)
        self.assertEqual(self.loads, [self.base_sha])
        self.assertEqual(self.row("G-002")["current_revision_sha"], self.sha)
        self.assertEqual(self.row("G-002")["path"], "docs/plans/archive/[G-002] Dependency.md")

    def test_planning_refreshes_catalog_after_base_adoption_before_snapshot(self):
        old = self.boundary.refresh()
        self.sha = "c" * 40
        outcome, _ports = self.prepare_planning_workspace()
        self.assertIsNone(outcome)
        self.assertEqual(self.loads, [old, self.sha])
        self.assertEqual(self.row()["current_revision_sha"], self.sha)

    def test_planning_revalidates_changed_sqlite_declarations(self):
        self.boundary.refresh()
        self.connection.execute("UPDATE exec_plans SET priority='low' WHERE plan_id='G-001'")
        self.connection.commit()
        outcome, _ports = self.prepare_planning_workspace()
        self.assertIsNone(outcome)
        self.assertEqual(self.loads, [self.sha, self.sha])
        self.assertEqual(self.row()["priority"], "critical")

    def test_planning_catalog_failure_preserves_result_without_error_gate(self):
        from loop_manager_internal.planning.planning_runtime import PlanningOutcomeKind
        self.documents.pop("docs/plans/[G-002] Dependency.md")
        outcome, ports = self.prepare_planning_workspace()
        self.assertEqual(outcome.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertTrue(ports.state.snapshot.planning_result_completed)
        self.assertIsNone(ports.state.snapshot.planning_error)

    def test_planning_resume_refreshes_before_pr_consumers(self):
        self.assertEqual(self.run_step(phase='PLANNING'), 0)
        self.assertLess(self.events.index('catalog'), self.events.index('pr'))


class CatalogCacheAndCasTests(_CatalogRefreshCase):
    def test_missing_active_file_rechecks_external_deletion_boundary_at_same_revision(self):
        self.boundary.refresh()
        previous_sha = self.sha
        path = self.row()['path']
        del self.documents[path]
        self.paths = self.paths - {path}
        self.sha = 'c' * 40
        original = self.pinned
        def isolated(revision):
            pinned = original(revision)
            return replace(pinned, snapshot=replace(pinned.snapshot, isolation_enabled=True))
        self.pinned = isolated
        self.boundary.refresh()
        self.assertEqual(self.boundary.pinned.revision_sha, self.sha)
        self.assertNotIn(path, self.boundary.pinned.regular_paths)
        before = dict(self.row())
        self.boundary.refresh()
        self.assertEqual(self.loads, [previous_sha, self.sha, self.sha])
        self.assertEqual(dict(self.row()), before)
        self.assertIsNotNone(self.row('G-002'))

    def test_rowless_cancellation_rechecks_external_cleanup_at_same_revision(self):
        calls = []
        def reconcile(revision, remember):
            calls.append(revision)
            remember(self.pinned(revision))
            if len(calls) == 3:
                self.connection.execute("DELETE FROM execplan_cancellations WHERE repo=? AND plan_id='G-999'", (self.repo,))
                self.connection.commit()
            return state.CatalogReconciliationResult(state.ReconciliationKind.RECONCILED, revision_sha=revision)
        self.boundary.refresh()  # Adopt current paths before isolating cleanup effects.
        self.boundary.revision = None
        self.boundary.reconcile = reconcile
        self.boundary.refresh()
        self.connection.execute("INSERT INTO execplan_cancellations "
            "(repo,plan_id,path,title,sources,depends_on,complexity,risk,priority,content_hash,plan_sha,deletion_commit) "
            "SELECT repo,'G-999','docs/plans/[G-999] Gone.md','Gone',sources,depends_on,'low','low',"
            "priority,content_hash,plan_sha,'deleted' FROM exec_plans WHERE repo=? AND plan_id='G-001'", (self.repo,))
        self.connection.commit()
        self.boundary.refresh()
        # Writer/ref release is external: revision and declarations have not changed.
        self.boundary.refresh()
        self.assertEqual(calls, [self.sha] * 3)
        self.boundary.refresh()
        self.assertEqual(len(calls), 3)

    def test_rowless_deleted_hold_rechecks_external_release_at_same_revision(self):
        self.boundary.refresh()
        calls = []
        def reconcile(revision, remember):
            calls.append(revision)
            remember(self.pinned(revision))
            if len(calls) == 2:
                self.connection.execute("DELETE FROM execplan_catalog_holds WHERE repo=? AND plan_id='G-999'", (self.repo,))
                self.connection.commit()
            return state.CatalogReconciliationResult(state.ReconciliationKind.RECONCILED, revision_sha=revision)
        self.boundary.reconcile = reconcile
        self.connection.execute('INSERT INTO execplan_catalog_holds VALUES (?,?,?,?,NULL)',
            (self.repo, 'G-999', self.sha, json.dumps({'path': 'docs/plans/[G-999] Gone.md'})))
        self.connection.commit()
        self.boundary.refresh()
        self.boundary.refresh()
        self.assertEqual(calls, [self.sha] * 2)
        self.boundary.refresh()
        self.assertEqual(len(calls), 2)

    def test_final_marker_diagnostic_owner_rechecks_cleanup_at_same_revision(self):
        from loop_manager_internal.storage import deleted_plan_state, blocked_checkpoint_state
        deleted_plan_state.ensure_schema(self.connection)
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.boundary.refresh()
        calls = []
        def reconcile(revision, remember):
            calls.append(revision)
            remember(self.pinned(revision))
            if len(calls) == 2:
                self.connection.execute("DELETE FROM diagnostic_preservations WHERE repo=? AND owner='G-999'", (self.repo,))
                self.connection.commit()
            return state.CatalogReconciliationResult(state.ReconciliationKind.RECONCILED, revision_sha=revision)
        self.boundary.reconcile = reconcile
        self.connection.execute("INSERT INTO deleted_plan_ids VALUES (?,'G-999')", (self.repo,))
        self.connection.execute("INSERT INTO diagnostic_preservations (repo,scope,owner,input_identity,payload) VALUES (?,'project','G-999','legacy','{}')", (self.repo,))
        self.connection.commit()
        self.boundary.refresh()
        self.boundary.refresh()
        self.assertEqual(calls, [self.sha] * 2)
        self.boundary.refresh()
        self.assertEqual(len(calls), 2)
        self.assertIsNotNone(self.connection.execute("SELECT * FROM deleted_plan_ids WHERE repo=? AND plan_id='G-999'", (self.repo,)).fetchone())

    def test_pending_file_cleanup_bypasses_cache_until_queue_is_drained(self):
        from loop_manager_internal.storage import deleted_plan_state
        deleted_plan_state.ensure_schema(self.connection)
        calls = []
        def reconcile(revision, remember):
            calls.append(revision)
            remember(self.pinned(revision))
            if len(calls) == 3:
                self.connection.execute('DELETE FROM deleted_plan_files WHERE repo=?', (self.repo,))
                self.connection.commit()
            return state.CatalogReconciliationResult(state.ReconciliationKind.RECONCILED, revision_sha=revision)
        self.boundary.refresh()  # Adopt current paths before isolating cleanup effects.
        self.boundary.revision = None
        self.boundary.reconcile = reconcile
        self.boundary.refresh()
        self.connection.execute("INSERT INTO deleted_plan_files VALUES (?,'G-999','/owned/log')", (self.repo,))
        self.connection.commit()
        self.boundary.refresh()
        self.boundary.refresh()
        self.assertEqual(calls, [self.sha] * 3)
        self.boundary.refresh()
        self.assertEqual(len(calls), 3)
        # Another repository's queue does not invalidate this cache.
        self.connection.execute("INSERT INTO deleted_plan_files VALUES ('/other','G-999','/other/log')")
        self.connection.commit()
        self.boundary.refresh()
        self.assertEqual(len(calls), 3)

    def test_refresh_does_not_update_another_repositorys_plan(self):
        project = dict(self.connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (self.repo,)
        ).fetchone())
        project['repo'] = '/other-repo'
        plan = dict(self.row())
        plan['repo'] = project['repo']
        for values, table in ((project, 'projects'), (plan, 'exec_plans')):
            columns = ', '.join(values)
            placeholders = ', '.join('?' for _ in values)
            self.connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        self.connection.commit()

        self.boundary.refresh()

        other = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (project['repo'],),
        ).fetchone()
        self.assertEqual(dict(other), plan)

    def test_all_declarations_refresh_preserving_execution_audit_and_planning_error(self):
        self.connection.execute("UPDATE projects SET planning_error='retained'")
        original = self.row()
        self.boundary.refresh()
        updated = self.row()
        for field in ('status', 'branch', 'worktree', 'worker_session_id', 'reviewer_session_id',
                      'retry_count', 'publication_path', 'publication_content_hash', 'publication_sha',
                      'verify_failure_history', 'review_failure_history'):
            self.assertEqual(updated[field], original[field], field)
        self.assertEqual((updated['path'], updated['priority'], updated['current_complexity'],
                          updated['current_risk'], updated['current_revision_sha']),
                         ('docs/plans/[G-001] Updated.md', 'critical', 'high', 'critical', self.sha))
        self.assertEqual(updated['sources'], '["docs/requirements/change.md"]')
        self.assertEqual(updated['depends_on'], '["G-002"]')
        self.assertEqual(self.connection.execute('SELECT planning_error FROM projects').fetchone()[0], 'retained')

    def test_pr_close_session_and_cleanup_changes_do_not_reread_same_revision(self):
        self.boundary.refresh()
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED', worker_session_id='new', worktree=NULL")
        self.connection.commit()
        self.boundary.refresh()
        self.assertEqual(self.loads, [self.sha])
        self.assertEqual(self.row()['status'], 'PLAN_BLOCKED')

    def test_push_with_new_plan_and_dependency_is_not_missed(self):
        self.boundary.refresh()
        self.sha = 'c' * 40
        self.documents['docs/plans/[G-003] New.md'] = self.fixture._text('G-003', 'New', depends_on=('G-001',))
        self.paths = frozenset((*self.paths, 'docs/plans/[G-003] New.md'))
        self.boundary.refresh()
        self.assertEqual(self.row('G-003')['depends_on'], '["G-001"]')
        self.assertEqual(self.row()['current_revision_sha'], self.sha)
        self.assertEqual(len(self.loads), 2)

    def test_unrelated_document_push_still_advances_catalog_revision(self):
        self.boundary.refresh()
        self.sha = 'c' * 40
        self.boundary.refresh()
        self.assertEqual(self.row()['current_revision_sha'], self.sha)
        self.assertEqual(len(self.loads), 2)

    def test_db_declaration_change_invalidates_cache(self):
        self.boundary.refresh()
        self.connection.execute("UPDATE exec_plans SET priority='low' WHERE plan_id='G-001'")
        self.connection.commit()
        self.boundary.refresh()
        self.assertEqual(self.row()['priority'], 'critical')
        self.assertEqual(len(self.loads), 2)

    def test_invalid_catalog_has_no_partial_application(self):
        before = state.DefinitionStateRepository(self.connection).capture(self.repo)
        self.documents.pop('docs/plans/[G-002] Dependency.md')
        # Remove dependency so the service itself diagnoses the missing ID.
        self.documents['docs/plans/[G-001] Updated.md'] = self.fixture._text('G-001', 'Updated')
        with self.assertRaises(refresh.CatalogRefreshStopped):
            self.boundary.refresh()
        self.assertEqual(before, state.DefinitionStateRepository(self.connection).capture(self.repo))

    def test_remote_race_rolls_back_all_definition_changes(self):
        pinned = self.pinned(self.sha)
        before = state.DefinitionStateRepository(self.connection).capture(self.repo)
        repo = state.DefinitionStateRepository(self.connection, fault_hook=lambda stage: setattr(self, 'sha', 'c'*40))
        port = SimpleNamespace(load=lambda: pinned, current_revision=lambda: self.sha)
        result = state.CatalogReconciliationService(repo, port).reconcile(self.repo)
        self.assertEqual(result.kind, state.ReconciliationKind.STALE)
        self.assertEqual(before, repo.capture(self.repo))

    def test_ownership_cas_race_does_not_apply_catalog(self):
        pinned = self.pinned(self.sha)
        repo = state.DefinitionStateRepository(self.connection)
        expected = repo.capture(self.repo)
        self.connection.execute("UPDATE exec_plans SET worker_session_id='concurrent'")
        result = repo.apply(self.repo, pinned, expected, revision_is_current=lambda: True)
        self.assertIsNone(result)
        self.assertEqual(self.row()['title'], 'Old')
        self.assertEqual(self.row()['worker_session_id'], 'concurrent')

    def test_catalog_transaction_interruption_restarts_without_partial_delta(self):
        pinned = self.pinned(self.sha)
        repo = state.DefinitionStateRepository(self.connection)
        def interrupt(stage):
            if stage == 'after-plan:G-001':
                raise RuntimeError('interrupted')
        repo._fault_hook = interrupt
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            state.CatalogReconciliationService(repo, fixtures.FakeDefinitionPort(pinned)).reconcile(self.repo)
        self.assertEqual(self.row()['title'], 'Old')
        self.boundary.refresh()
        self.assertEqual(self.row()['title'], 'Updated')
        self.assertEqual(self.row()['worker_session_id'], 'worker')

    def test_adapter_initial_migration_preserves_existing_execution(self):
        pinned = self.pinned(self.sha)
        port = SimpleNamespace(load=lambda: pinned, loaded=pinned, current_revision=lambda: self.sha)
        with mock.patch.object(manager, '_GitCatalogDefinitionPort', return_value=port), \
             mock.patch.object(manager, 'git', return_value=SimpleNamespace(returncode=0, stdout=self.sha)):
            result = manager.reconcile_execplan_catalog(self.connection, Path(self.repo), self.repo, 'main')
        self.assertEqual(result.kind, state.ReconciliationKind.RECONCILED)
        self.assertEqual(self.row()['status'], 'IMPLEMENTING')
        self.assertEqual(self.row()['worker_session_id'], 'worker')
        self.assertEqual(self.row()['publication_content_hash'], 'old-hash-G-001')

    def test_adapter_initial_invalid_catalog_preserves_cache_and_other_safety_cause(self):
        manager.scoped_blocking.record_project_block(
            self.connection, self.repo, manager.BlockReasonCode.REPOSITORY_STATE_UNSAFE,
            'separate safety cause', 'fingerprint', plan_id='G-001', previous_phase='WORKING')
        self.connection.commit()
        before = state.DefinitionStateRepository(self.connection).capture(self.repo)
        snapshot = catalog.snapshot_from_documents({'docs/plans/[G-001] Updated.md': self.fixture._text('G-001', 'Updated')})
        pinned = state.PinnedCatalog(self.sha, snapshot, self.paths)
        port = SimpleNamespace(load=lambda: pinned, loaded=pinned, current_revision=lambda: self.sha)
        with mock.patch.object(manager, '_GitCatalogDefinitionPort', return_value=port):
            result = manager.reconcile_execplan_catalog(self.connection, Path(self.repo), self.repo, 'main')
        self.assertEqual(result.kind, state.ReconciliationKind.INVALID_CATALOG)
        self.assertEqual(state.DefinitionStateRepository(self.connection).capture(self.repo), before)
        self.assertEqual(manager.scoped_blocking.project_block(self.connection, self.repo)['evidence'], 'separate safety cause')

    def test_real_pr_close_audit_does_not_invalidate_catalog_cache(self):
        self.boundary.refresh()
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE plan_id='G-001'")
        self.connection.commit()
        manager._block_closed_pull_request(self.connection, self.repo, self.row(),
                                          'https://example/pr/1', 'c'*40)
        self.boundary.refresh()
        self.assertEqual(self.row()['status'], 'PLAN_BLOCKED')
        self.assertEqual(len(self.loads), 1)
        self.assertIsNotNone(manager.scoped_blocking.plan_block(self.connection, self.repo, 'G-001'))


class CatalogArchiveConsumerTests(_CatalogRefreshCase):
    def test_archive_does_not_replay_already_applied_deletion_receipt(self):
        self.boundary.refresh()
        recheck = mock.Mock(side_effect=AssertionError('applied deletion replayed'))
        self.boundary._remember(replace(self.boundary.pinned,
            deletions=(SimpleNamespace(recheck=recheck),)))
        self.assertEqual(self.archive(), archive_state.ArchiveWriteResult.APPLIED)
        recheck.assert_not_called()
        self.assertTrue(self.row('G-002')['path'].startswith('docs/plans/archive/'))

    def test_archive_revalidation_preserves_renamed_requirement_contents(self):
        source = 'docs/requirements/[R-066] AGENTS 변경 사전 승인.md'
        renamed = 'docs/requirements/[R-066] Agent CLI 지침의 독립성.md'
        requirements = {renamed: '# [R-066] Agent CLI 지침의 독립성\n\n변경된 요구사항.\n'}
        plan_path = 'docs/plans/[G-001] Updated.md'
        self.documents[plan_path] = self.documents[plan_path].replace(
            'docs/requirements/change.md', source)
        self.paths = self.paths - {'docs/requirements/change.md'} | {renamed}

        def pinned(revision):
            return state.PinnedCatalog(revision, catalog.snapshot_from_documents(
                self.documents, validate_sources=True, available_regular_paths=self.paths,
                requirement_documents=requirements, revision_sha=revision), self.paths,
                requirement_documents=requirements)

        with mock.patch.object(self, 'pinned', side_effect=pinned):
            self.assertEqual(self.archive(), archive_state.ArchiveWriteResult.APPLIED)
            self.boundary.refresh()
        self.assertEqual(self.row('G-002')['path'], 'docs/plans/archive/[G-002] Dependency.md')
        self.assertEqual(self.row()['sources'], json.dumps([source], ensure_ascii=False))
        self.assertEqual(self.boundary.pinned.snapshot.active['G-001'].sources, (source,))
        self.assertIs(self.boundary.pinned.requirement_documents, requirements)
        self.assertEqual(len(self.loads), 1)

    def test_verified_archive_delta_and_audit_are_atomic_without_reread(self):
        self.connection.execute(
            "UPDATE exec_plans SET publication_sha=? WHERE repo=? AND plan_id='G-002'",
            ('d' * 40, self.repo),
        )
        self.assertEqual(self.archive(), archive_state.ArchiveWriteResult.APPLIED)
        item = self.archive_repo.item(self.repo, 'G-002')
        self.assertEqual(item['archived'], 1)
        self.assertEqual(self.row('G-002')['path'], 'docs/plans/archive/[G-002] Dependency.md')
        self.assertEqual(self.row('G-002')['current_revision_sha'], self.sha)
        self.assertEqual(self.row('G-002')['status'], 'MERGED')
        history = state.CatalogStateRepository(self.connection).historical_row(self.repo, 'G-002')
        self.assertTrue(history['identity_verified'])
        self.assertTrue(history['merge_confirmed'])
        self.assertEqual(history['proof_revision'], self.sha)
        self.assertEqual(history['plan_sha'], self.row('G-002')['publication_sha'])
        self.assertEqual({row['plan_id'] for row in manager.prerequisite_validation_rows(
            self.connection, self.repo)}, {'G-001', 'G-002'})
        self.boundary.refresh()
        self.assertEqual(len(self.loads), 1)

    def test_reconciliation_repairs_missing_archive_history_from_matching_merged_definition(self):
        self.assertEqual(self.archive(), archive_state.ArchiveWriteResult.APPLIED)
        self.connection.execute('DELETE FROM execplan_catalog_history WHERE repo=?', (self.repo,))
        self.connection.commit()
        repository = state.DefinitionStateRepository(self.connection)
        self.assertIsNotNone(repository.apply(
            self.repo, self.boundary.pinned, repository.capture(self.repo), revision_is_current=lambda: True))
        self.assertEqual({row['plan_id'] for row in manager.prerequisite_validation_rows(
            self.connection, self.repo)}, {'G-001', 'G-002'})

    def test_planning_migrates_legacy_archive_before_dependency_validation(self):
        self.connection.execute(
            "UPDATE exec_plans SET publication_sha=? WHERE repo=? AND plan_id='G-002'",
            ('d' * 40, self.repo),
        )
        self.assertEqual(self.archive(), archive_state.ArchiveWriteResult.APPLIED)
        self.connection.execute(
            "UPDATE execplan_catalog_history SET plan_sha=?, diagnostic_code=? WHERE repo=?",
            ('a' * 40, 'stable-identity-mismatch', self.repo),
        )
        self.connection.execute(
            "UPDATE projects SET phase='PLANNING', planner_session_id='saved-planner', "
            "worktree='/owned/planning', planning_workspace_owned=1, "
            "planning_error='saved-planning-error', planning_result_completed=1 WHERE repo=?",
            (self.repo,),
        )
        manager._ensure_execplans_schema(self.connection)
        self.connection.commit()
        project = dict(self.connection.execute('SELECT * FROM projects WHERE repo=?', (self.repo,)).fetchone())
        independent = dict(self.row('G-001'))
        history = state.CatalogStateRepository(self.connection)
        with mock.patch.object(manager, '_LegacyCatalogMergeProof') as external:
            for _restart in range(2):
                pinned = self.boundary.pinned if _restart == 0 else replace(
                    self.boundary.pinned, snapshot=replace(self.boundary.pinned.snapshot, active={}),
                )
                prepared = manager.migrate_legacy_execplans(
                    self.connection, reconcile_history=True, target_repo=self.repo,
                    ensure_schema=False, pinned_catalog=pinned, prepare_only=True,
                )
                manager.migrate_legacy_execplans(
                    self.connection, reconcile_history=True, target_repo=self.repo,
                    ensure_schema=False, pinned_catalog=pinned, prepared=prepared,
                )
                self.assertEqual({row['plan_id'] for row in manager.prerequisite_validation_rows(
                    self.connection, self.repo)}, {'G-001', 'G-002'})
                self.assertEqual(dict(self.connection.execute(
                    'SELECT * FROM projects WHERE repo=?', (self.repo,),
                ).fetchone()), project)
                self.assertEqual(dict(self.row('G-001')), independent)
            external.return_value.proof_for.assert_not_called()
            external.return_value.verify_stored.assert_not_called()
        self.assertEqual(history.historical_row(self.repo, 'G-002')['plan_sha'], 'a' * 40)

    def test_archive_unconfirmed_remote_does_not_record_success(self):
        self.assertEqual(self.archive(remote=False), archive_state.ArchiveWriteResult.SUPERSEDED)
        self.assertIsNone(self.archive_repo.item(self.repo, 'G-002'))
        self.assertEqual(self.row('G-002')['path'], 'docs/plans/[G-002] Dependency.md')

    def test_archive_remote_race_rolls_back_success_and_catalog(self):
        self.boundary.refresh()
        repo = archive_state.ExecPlanArchiveRepository(self.connection)
        candidates = repo.merged_candidates(self.repo)
        self.sha = 'c'*40
        original = self.boundary.repository
        original._fault_hook = lambda stage: setattr(self, 'sha', 'd'*40)
        repo.apply_catalog = self.boundary.apply_archive
        result = repo.record_success(self.repo, candidates, base_sha='b'*40,
                                    operation_id=None, commit_oid='c'*40, push_result='pushed')
        self.assertEqual(result, archive_state.ArchiveWriteResult.SUPERSEDED)
        self.assertIsNone(repo.item(self.repo, 'G-002'))
        self.assertEqual(self.row('G-002')['path'], 'docs/plans/[G-002] Dependency.md')

    def test_archive_sqlite_definition_race_rolls_back_success(self):
        self.boundary.refresh()
        repo = archive_state.ExecPlanArchiveRepository(self.connection, apply_catalog=self.boundary.apply_archive)
        candidates = repo.merged_candidates(self.repo)
        self.connection.execute("UPDATE exec_plans SET content_hash='changed' WHERE plan_id='G-001'")
        self.connection.commit()
        self.sha = 'c'*40
        result = repo.record_success(self.repo, candidates, base_sha='b'*40,
                                    operation_id=None, commit_oid=self.sha, push_result='pushed')
        self.assertEqual(result, archive_state.ArchiveWriteResult.SUPERSEDED)
        self.assertIsNone(repo.item(self.repo, 'G-002'))
        self.assertEqual(self.row('G-002')['path'], 'docs/plans/[G-002] Dependency.md')

    def test_archive_adapter_validates_publication_then_adopts_delta_and_audit(self):
        from loop_manager_internal.catalog import execplan_archive as archive
        from loop_manager_internal.runtime.resource_lock import resource_locks
        self.boundary.refresh()
        current_revision = self.boundary.current_revision
        def observe_revision():
            self.assertFalse(self.connection.in_transaction)
            return current_revision()
        self.boundary.current_revision = observe_revision
        port = mock.Mock()
        port.fetch_remote_base.side_effect = lambda base: self.sha
        entry = archive.ArchiveTreeEntry('100644', 'blob', 'object', self.row('G-002')['content_hash'])
        port.observe.side_effect = lambda sha, candidates: tuple(
            archive.ArchiveCandidateObservation(item, entry, None) for item in candidates)
        port.prepare_commit.side_effect = lambda sha, base, operation, moves: archive.PreparedArchive(
            operation, sha, 'tree-hash', 'c'*40)
        def commit_matches(*_args):
            self.assertFalse(self.connection.in_transaction)
            return True
        port.commit_matches.side_effect = commit_matches
        def push(prepared, base):
            self.sha = prepared.commit_oid
            return archive.ArchivePushResult(True, self.sha)
        port.push_fast_forward.side_effect = push
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with resource_locks(state_dir, self.repo, ['git:fetch'], owner='execplan-archive'), \
                 mock.patch.object(manager, '_ExecPlanArchiveGitPort', return_value=port):
                result = manager.dispatch_execplan_archive_housekeeping(
                    self.connection, Path(self.repo), state_dir, self.repo, 'main',
                    catalog_refresh=self.boundary)
        self.assertEqual(result.commit_oid, self.sha)
        self.assertEqual(result.archived_plan_ids, ('G-002',))
        self.assertEqual(self.row('G-002')['path'], 'docs/plans/archive/[G-002] Dependency.md')
        self.assertEqual(archive_state.ExecPlanArchiveRepository(self.connection).item(self.repo, 'G-002')['archived'], 1)
        self.boundary.refresh()
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(port.commit_matches.call_count, 2)

    def test_archive_ownership_change_after_git_proof_cannot_be_adopted(self):
        self.boundary.refresh()
        repo = archive_state.ExecPlanArchiveRepository(self.connection)
        candidates = repo.merged_candidates(self.repo)
        expected = self.boundary.repository.capture(self.repo)
        self.connection.execute("UPDATE exec_plans SET worker_session_id='raced' WHERE plan_id='G-001'")
        self.connection.commit()
        self.sha = 'c'*40
        self.assertFalse(self.boundary.apply_archive('b'*40, self.sha, candidates, expected=expected))
        self.assertEqual(self.row('G-002')['path'], 'docs/plans/[G-002] Dependency.md')
        self.assertEqual(self.row()['worker_session_id'], 'raced')

    def test_archive_refresh_rechecks_safety_before_preparing_or_publishing(self):
        self.boundary.refresh()
        port = mock.Mock()
        port.fetch_remote_base.side_effect = lambda base: self.sha
        seen = []
        def safety():
            seen.append(self.row()['current_revision_sha'])
            return False
        with mock.patch.object(manager, '_ExecPlanArchiveGitPort', return_value=port):
            result = manager.dispatch_execplan_archive_housekeeping(
                self.connection, Path(self.repo), Path('/state'), self.repo, 'main',
                catalog_refresh=self.boundary, archive_safety=safety)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {self.sha})
        port.prepare_commit.assert_not_called()
        port.push_fast_forward.assert_not_called()
        self.assertIsNone(result.commit_oid)
        self.assertEqual(self.row('G-002')['status'], 'MERGED')


class CatalogPullRequestConsumerTests(_CatalogRefreshCase):
    def test_restart_after_committed_pr_result_keeps_merge_and_session(self):
        self.boundary.refresh()
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-001'")
        self.connection.commit()
        restarted = refresh.ProjectCatalogRefresh(self.connection, self.repo, lambda: self.sha, self.reconcile)
        restarted.refresh()
        self.assertEqual(self.row()['status'], 'MERGED')
        self.assertEqual(self.row()['worker_session_id'], 'worker')
        self.assertEqual(self.row()['publication_content_hash'], 'old-hash-G-001')

    def test_blocked_pr_query_cannot_close_reopen_or_cleanup_before_catalog(self):
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE plan_id='G-001'")
        self.connection.commit()
        before = dict(self.row())
        for pr_state in ('MERGED', 'CLOSED', 'OPEN'):
            with mock.patch.object(manager, 'pr_for_branch', return_value={
                    'state': pr_state, 'url': before['pr_url'], 'headRefName': before['branch'],
                    'baseRefName': 'main', 'headRefOid': 'd'*40}), \
                 mock.patch.object(manager, 'complete_merged_execplan_worktree') as cleanup, \
                 mock.patch.object(manager, '_recorded_base_fetch') as fetch:
                manager.observe_waiting_pull_requests(self.connection, Path(self.repo), Path('/state'),
                                                      self.repo, read_only=True)
                cleanup.assert_not_called()
                fetch.assert_not_called()
                self.assertEqual(dict(self.row()), before)

    def test_new_merge_proof_allows_archive_conflict_without_pr_state_transition(self):
        active = 'docs/plans/[G-001] Old.md'
        archived = 'docs/plans/archive/[G-001] Old.md'
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN', pr_head_oid=? WHERE plan_id='G-001'", ('d'*40,))
        self.connection.commit()
        before = dict(self.row())
        proof = {'state': 'MERGED', 'baseRefName': 'main', 'headRefName': 'branch',
                 'headRefOid': 'd'*40, 'url': 'https://example/pr/1'}
        with mock.patch.object(manager, '_git_tree_entries', return_value={active.encode(): (), archived.encode(): ()}), \
             mock.patch.object(manager, 'pr_for_branch', return_value=proof):
            confirmed = manager._catalog_archive_conflict_ids(self.connection, Path(self.repo), self.repo, self.sha)
            self.assertIn('G-001', confirmed)
            self.assertEqual(dict(self.row()), before)
            for field, value in (('baseRefName', 'other'), ('headRefName', 'other'),
                                 ('headRefOid', 'other'), ('url', 'other')):
                with mock.patch.object(manager, 'pr_for_branch', return_value={**proof, field: value}):
                    self.assertNotIn('G-001', manager._catalog_archive_conflict_ids(
                        self.connection, Path(self.repo), self.repo, self.sha))
        self.assertEqual(dict(self.row()), before)

    def test_closed_pr_merge_conflict_reconciles_before_merge_observation(self):
        active = 'docs/plans/[G-001] Updated.md'
        archived = 'docs/plans/archive/[G-001] Old.md'
        documents = {**self.documents, archived: self.fixture._text('G-001', 'Old')}
        self.connection.execute(
            "UPDATE exec_plans SET status='PLAN_BLOCKED', blocked_reason=?, pr_head_oid=? WHERE plan_id='G-001'",
            (manager.PR_CLOSED_BLOCK_REASON, 'd'*40))
        self.connection.commit()
        proof = {'state': 'MERGED', 'baseRefName': 'main', 'headRefName': 'branch',
                 'headRefOid': 'd'*40, 'url': 'https://example/pr/1'}
        with mock.patch.object(manager, '_git_tree_entries', return_value={
                path.encode(): () for path in documents}), \
             mock.patch.object(manager, 'pr_for_branch', return_value=proof), \
             mock.patch.object(manager, 'complete_merged_execplan_worktree',
                               return_value=SimpleNamespace(status='missing')) as cleanup, \
             mock.patch.object(manager, '_recorded_base_fetch'):
            for field, value in (('baseRefName', 'other'), ('headRefName', 'other'),
                                 ('headRefOid', 'other'), ('url', 'other')):
                with mock.patch.object(manager, 'pr_for_branch', return_value={**proof, field: value}):
                    self.assertNotIn('G-001', manager._catalog_archive_conflict_ids(
                        self.connection, Path(self.repo), self.repo, self.sha))
            confirmed = manager._catalog_archive_conflict_ids(
                self.connection, Path(self.repo), self.repo, self.sha)
            self.assertIn('G-001', confirmed)
            snapshot = catalog.snapshot_from_documents(
                documents, revision_sha=self.sha, archive_conflict_plan_ids=confirmed,
                validate_sources=True, available_regular_paths=self.paths | {archived})
            self.assertEqual(snapshot.active['G-001'].path, active)
            self.assertNotIn('G-001', snapshot.archived)
            result = state.CatalogReconciliationService(
                state.DefinitionStateRepository(self.connection), fixtures.FakeDefinitionPort(
                    state.PinnedCatalog(self.sha, snapshot, self.paths | {archived}))
            ).reconcile(self.repo)
            self.assertEqual(result.kind, state.ReconciliationKind.RECONCILED)
            self.assertEqual(self.row()['status'], 'PLAN_BLOCKED')
            self.assertEqual(self.row()['blocked_reason'], manager.PR_CLOSED_BLOCK_REASON)
            observation = manager.observe_waiting_pull_requests(
                self.connection, Path(self.repo), Path('/state'), self.repo)
            self.assertTrue(observation.merged)
            cleanup.assert_called_once()
            self.assertEqual(cleanup.call_args.args[4]['plan_id'], 'G-001')
            self.connection.execute(
                "UPDATE exec_plans SET blocked_reason='unrelated safety block' WHERE plan_id='G-001'")
            self.connection.commit()
            self.assertNotIn('G-001', manager._catalog_archive_conflict_ids(
                self.connection, Path(self.repo), self.repo, self.sha))


    def test_project_safety_block_keeps_closed_pr_block_when_head_is_restored(self):
        self.connection.execute(
            "UPDATE exec_plans SET status='PLAN_BLOCKED', ever_started=1, blocked_reason=?, pr_head_oid=? WHERE plan_id='G-001'",
            (manager.PR_CLOSED_BLOCK_REASON, 'd'*40))
        manager.scoped_blocking.record_project_block(
            self.connection, self.repo, manager.BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            'independent head safety cause', 'head-safety', plan_id='G-001', previous_phase='PLAN_BLOCKED')
        self.connection.commit()
        original = dict(self.row())
        for head, pr_state in (('c'*40, 'OPEN'), ('d'*40, 'CLOSED'), ('d'*40, 'OPEN'), ('d'*40, 'OPEN')):
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(manager, 'observe_blocked_plan_remotes'))
                stack.enter_context(mock.patch.object(manager, 'resume_project_block_if_safe', return_value=False))
                stack.enter_context(mock.patch.object(manager, 'pr_for_branch', return_value={
                    'state': pr_state, 'headRefOid': head, 'headRefName': 'branch',
                    'baseRefName': 'main', 'url': 'https://example/pr/1'}))
                reconciler = stack.enter_context(mock.patch.object(manager.project_blocked_recovery, 'ProjectBlockedReconciler'))
                reconciler.return_value.refresh_catalog.return_value = False
                fetch = stack.enter_context(mock.patch.object(manager, 'remote_base'))
                mutation = stack.enter_context(mock.patch.object(manager, '_recorded_base_fetch'))
                cleanup = stack.enter_context(mock.patch.object(manager, 'complete_merged_execplan_worktree'))
                agent = stack.enter_context(mock.patch.object(manager, 'run_agent'))
                result = manager._run_project_step(self.connection, Path('/state'), self.repo, Path('unused.ini'), 'main', False)
            self.assertEqual(result, 1)
            self.assertEqual(dict(self.row()), original)
            self.assertEqual(manager.scoped_blocking.project_block(self.connection, self.repo)['evidence'],
                             'independent head safety cause')
            reconciler.return_value.reconcile.assert_not_called()
            fetch.assert_not_called()
            mutation.assert_not_called()
            cleanup.assert_not_called()
            agent.assert_not_called()


class ProjectCatalogConsumerTests(_CatalogRefreshCase):
    def test_step_fetch_catalog_pr_archive_and_selection_order(self):
        self.assertEqual(self.run_step(), 0)
        self.assertEqual(self.events, ['fetch', 'catalog', 'pr', 'archive-eligibility', 'planning'])

    def test_invalid_input_stops_all_consumers(self):
        self.assertEqual(self.run_step(invalid=True), 1)
        self.assertEqual(self.events, ['fetch', 'catalog'])
        self.assertEqual(self.row()['title'], 'Old')

    def test_merge_push_refreshes_before_archive_eligibility(self):
        self.assertEqual(self.run_step(pr=lambda: setattr(self, 'sha', 'c'*40)), 0)
        self.assertEqual(self.events[:5], ['fetch', 'catalog', 'pr', 'catalog', 'archive-eligibility'])
        self.assertEqual(self.row()['current_revision_sha'], self.sha)

    def test_unsafe_agent_scope_prevents_fetch_and_consumers(self):
        self.assertEqual(self.run_step(scope_error=True), 1)
        self.assertEqual(self.events, [])

    def test_pending_publication_recovers_before_any_catalog_consumers(self):
        self.connection.execute("UPDATE projects SET phase='PLANNING', agent_snapshot=NULL")
        self.connection.commit()
        calls = []
        with contextlib.ExitStack() as stack:
            for name in ('observe_blocked_plan_remotes', 'validate_repository', 'remote_base'):
                stack.enter_context(mock.patch.object(manager, name, return_value='main' if name == 'remote_base' else Path(self.repo)))
            stack.enter_context(mock.patch.object(manager, 'git', return_value=SimpleNamespace(stdout=self.sha)))
            stack.enter_context(mock.patch.object(state.DefinitionStateRepository, 'has_publication_intent', return_value=True))
            pr = stack.enter_context(mock.patch.object(manager, 'synchronize_waiting_pull_requests'))
            archive = stack.enter_context(mock.patch.object(manager, '_execplan_archive_housekeeping_safe'))
            catalog_call = stack.enter_context(mock.patch.object(manager, 'reconcile_execplan_catalog'))
            stack.enter_context(mock.patch.object(manager.PlanningLifecycleCoordinator, 'run',
                side_effect=lambda context: calls.append(context.base_sha) or SimpleNamespace(kind=manager.PlanningOutcomeKind.PUBLICATION_RECOVERY)))
            result = manager._run_project_step(self.connection, Path('/state'), self.repo, Path('unused.ini'), 'main', False)
        self.assertEqual(calls, [self.sha])
        self.assertEqual(result, manager.PROJECT_STEP_RETRY)
        pr.assert_not_called()
        archive.assert_not_called()
        catalog_call.assert_not_called()

    def test_blocked_recovery_validates_scope_before_fetch_and_catalog(self):
        from tests.support.blocked_remote import project_blocked_remote_fixture
        fixture = project_blocked_remote_fixture(self)
        coordinator = fixture.coordinator
        events = []
        fixture.ports._recorded_base_fetch.side_effect = lambda *a, **k: events.append('fetch')
        fixture.ports.reconcile_execplan_catalog.side_effect = lambda *a, **k: events.append('catalog') or SimpleNamespace(kind=state.ReconciliationKind.RECONCILED)
        with mock.patch.object(coordinator, '_validate', side_effect=lambda *a, **k: events.append('validate')), \
             mock.patch.object(fixture.recovery, 'independent_scope', side_effect=lambda *a, **k: events.append('scope')):
            self.assertTrue(coordinator.refresh_catalog('G-106'))
        self.assertEqual(events, ['validate', 'scope', 'fetch', 'catalog'])
        self.assertEqual(fixture.ports.reconcile_execplan_catalog.call_args.kwargs,
                         {'record_invalid': False, 'state_dir': coordinator.state_dir})
        self.assertEqual(manager.scoped_blocking.project_block(fixture.connection, fixture.repo)['evidence'], 'original project cause')
        events.clear()
        with mock.patch.object(coordinator, '_validate', side_effect=RuntimeError('unsafe')):
            self.assertFalse(coordinator.refresh_catalog('G-106'))
        self.assertEqual(events, [])

    def test_publication_recovery_runs_selected_worker_in_same_step_after_catalog(self):
        self.connection.execute(
            "UPDATE projects SET phase='PLANNING', agent_snapshot='planner-before', "
            "agent_manager_snapshot=NULL, branch='planning', worktree='/planning', plan_path=NULL")
        self.connection.execute("UPDATE exec_plans SET status='READY' WHERE plan_id='G-001'")
        self.connection.commit()
        pending = True
        events = []
        def complete_publication(context):
            nonlocal pending
            pending = False
            self.sha = 'c' * 40
            self.connection.execute(
                "UPDATE exec_plans SET status='IMPLEMENTING' WHERE plan_id='G-001'")
            self.connection.execute(
                "UPDATE projects SET agent_snapshot=NULL, agent_manager_snapshot=NULL WHERE repo=?", (self.repo,))
            manager.hydrate_execplan_project(self.connection, self.repo, self.row())
            events.append('publication-and-activation')
            return manager.PlanningOutcome(manager.PlanningOutcomeKind.EXECUTION_READY,
                                           base_sha=self.sha, newly_activated=True)
        def observe_pr(*args, **kwargs):
            self.assertEqual(self.row()['current_revision_sha'], self.sha)
            self.assertEqual(self.row()['priority'], 'critical')
            events.append('pr-after-catalog')
            kwargs['after_reconciliation']()
            return SimpleNamespace(step_result=0)
        def prepare():
            self.assertEqual(self.row()['current_revision_sha'], self.sha)
            events.append('worker-prepare')
            return SimpleNamespace()
        def dispatch(context):
            events.append('worker-dispatch')
            return manager.ExecutionOutcome(manager.ExecutionOutcomeKind.OPERATIONAL_ERROR)
        with contextlib.ExitStack() as stack:
            for name in ('observe_blocked_plan_remotes', 'migrate_legacy_execplans', 'backfill_plan_shas',
                         'resume_plan_blocks', '_recorded_base_fetch', 'ensure_safe_base_worktree'):
                stack.enter_context(mock.patch.object(manager, name))
            stack.enter_context(mock.patch.object(manager, 'validate_repository', return_value=Path(self.repo)))
            stack.enter_context(mock.patch.object(manager, 'validate_persisted_agent_scope', return_value=object()))
            stack.enter_context(mock.patch.object(manager, 'ManagerGitCheckpoint',
                                                  return_value=SimpleNamespace(serialize=lambda: 'checkpoint')))
            refresh_scope = stack.enter_context(mock.patch.object(manager, 'refresh_persisted_agent_scope_after_manager_git'))
            stack.enter_context(mock.patch.object(manager, 'remote_base', return_value='main'))
            stack.enter_context(mock.patch.object(manager, 'git', side_effect=lambda *a, **k: SimpleNamespace(stdout=self.sha)))
            stack.enter_context(mock.patch.object(state.DefinitionStateRepository, 'has_publication_intent', side_effect=lambda *a: pending))
            planner = stack.enter_context(mock.patch.object(manager.PlanningLifecycleCoordinator, 'run', side_effect=complete_publication))
            stack.enter_context(mock.patch.object(manager.project_catalog_refresh, 'ProjectCatalogRefresh', return_value=self.boundary))
            stack.enter_context(mock.patch.object(manager, 'synchronize_waiting_pull_requests', side_effect=observe_pr))
            stack.enter_context(mock.patch.object(manager, 'recheck_cleanup_pending', return_value=[]))
            stack.enter_context(mock.patch(
                'loop_manager_internal.catalog.archive_service.ArchiveExecutionResources.safe', return_value=False))
            pr = stack.enter_context(mock.patch.object(manager, 'pr_for_branch', side_effect=AssertionError('new Worker PR lookup')))
            execution = stack.enter_context(mock.patch.object(manager, 'ExecutionPhaseCoordinator'))
            execution.return_value.prepare.side_effect = prepare
            execution.return_value.dispatch.side_effect = dispatch
            result = manager._run_project_step(self.connection, Path('/state'), self.repo, Path('unused.ini'), 'main', False)
        self.assertEqual(result, 1)
        self.assertEqual(events, ['publication-and-activation', 'pr-after-catalog', 'worker-prepare', 'worker-dispatch'])
        self.assertEqual(self.row()['status'], 'IMPLEMENTING')
        self.assertEqual(self.row()['worker_session_id'], 'worker')
        planner.assert_called_once()
        pr.assert_not_called()
        refresh_scope.assert_not_called()

    def test_planner_import_fixture_loads_catalog_adapter_without_git_or_provider(self):
        import subprocess
        import sys
        import tempfile
        from tests.support.git import prepare_manager_import_fixture
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (mock.patch('tests.support.git.commit_and_push',
                             side_effect=StopIteration('fixture copied')),
                  self.assertRaisesRegex(StopIteration, '^fixture copied$')):
                prepare_manager_import_fixture(root)
            result = subprocess.run(
                [sys.executable, '-I', '-B', '-c',
                 'import pathlib, sys; sys.path.insert(0, sys.argv[1]); '
                 'import loop_manager; from loop_manager_internal.catalog import project_catalog_refresh; '
                 'root = pathlib.Path(sys.argv[1]); assert loop_manager.ROOT == root; '
                 'assert pathlib.Path(project_catalog_refresh.__file__).is_relative_to(root)', str(root)],
                cwd=root, capture_output=True, text=True, check=False,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


class ScopedCatalogRefreshTests(_CatalogRefreshCase):
    def setUp(self):
        super().setUp()
        self.connection.execute(
            "UPDATE exec_plans SET status='READY', branch=NULL, worktree=NULL, pr_url=NULL")
        for plan_id, title, status in (('G-003', 'Direct', 'READY'),
                                     ('G-004', 'Indirect', 'READY'),
                                     ('G-005', 'Waiting', 'PR_OPEN')):
            self.fixture._insert(plan_id, title, status=status,
                                 branch='waiting' if status == 'PR_OPEN' else None,
                                 pr_url='https://example/pr/5' if status == 'PR_OPEN' else None)
        self.connection.execute('UPDATE exec_plans SET repo = ?', (self.repo,))
        self.connection.commit()
        self.documents = {
            'docs/plans/[G-001] Updated.md': self.fixture._text('G-001', 'Updated'),
            'docs/plans/[G-002] Dependency.md': self.fixture._text('G-002', 'Dependency', risk='invalid'),
            'docs/plans/[G-003] Direct.md': self.fixture._text('G-003', 'Direct', depends_on=('G-002',)),
            'docs/plans/[G-004] Indirect.md': self.fixture._text('G-004', 'Indirect', depends_on=('G-003',)),
            'docs/plans/[G-005] Waiting.md': self.fixture._text('G-005', 'Waiting'),
        }
        self.paths = frozenset((*self.documents, 'docs/PRODUCT.md'))

    def pinned(self, revision):
        return state.PinnedCatalog(revision, catalog.snapshot_from_documents(
            self.documents, validate_sources=True, available_regular_paths=self.paths,
            revision_sha=revision, isolate_errors=True), self.paths)

    def reconcile(self, revision, remember):
        self.loads.append(revision)
        pinned = self.pinned(revision)
        result = state.CatalogReconciliationService(
            state.DefinitionStateRepository(self.connection), fixtures.FakeDefinitionPort(pinned)
        ).reconcile(self.repo)
        if result.usable:
            remember(result.catalog or pinned)
        return result

    def held(self):
        return manager.scoped_blocking.catalog_held_plan_ids(self.connection, self.repo)

    def test_independent_selection_and_pr_observation_continue_without_partial_catalog_success(self):
        original = dict(self.row('G-002'))
        self.boundary.refresh()
        self.assertEqual(self.held(), {'G-002', 'G-003', 'G-004'})
        self.assertEqual(dict(self.row('G-002')), original)
        self.assertEqual(manager.select_ready_execplan(self.connection, self.repo)['plan_id'], 'G-001')
        self.assertEqual(manager.unmet_execplan_dependencies(self.connection, self.repo, 'G-004'), ('G-004',))
        self.assertIsNone(self.connection.execute(
            'SELECT * FROM execplan_catalog_revisions WHERE repo=?', (self.repo,)).fetchone())
        view = self.connection.execute(
            'SELECT * FROM execplan_catalog_views WHERE repo=?', (self.repo,)).fetchone()
        self.assertEqual((view['revision_sha'], view['validation_kind']), (self.sha, 'execution-view'))
        with mock.patch.object(manager, 'pr_for_branch', return_value={
                'state': 'OPEN', 'headRefName': 'waiting', 'baseRefName': 'main',
                'url': 'https://example/pr/5', 'mergeStateStatus': 'CLEAN'}) as observe:
            manager.observe_waiting_pull_requests(self.connection, Path(self.repo), Path('/state'), self.repo)
        observe.assert_called_once()
        self.assertEqual(self.row('G-005')['status'], 'PR_OPEN')
        restarted = refresh.ProjectCatalogRefresh(self.connection, self.repo, lambda: self.sha, self.reconcile)
        restarted.refresh()
        self.assertEqual(self.held(), {'G-002', 'G-003', 'G-004'})
        self.assertEqual(restarted.pinned.snapshot.withheld_ids, self.held())

    def test_remote_correction_releases_only_catalog_causes_and_preserves_history(self):
        with self.connection:
            manager.scoped_blocking.record_plan_block(
                self.connection, self.repo, 'G-003', manager.BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
                'pending approval', 'approval-fingerprint')
        approval = dict(manager.scoped_blocking.plan_block(self.connection, self.repo, 'G-003'))
        original = dict(self.row('G-003'))
        self.boundary.refresh()
        self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
        self.sha = 'c' * 40
        self.boundary.refresh()
        self.assertEqual(self.held(), set())
        self.assertEqual(dict(manager.scoped_blocking.plan_block(self.connection, self.repo, 'G-003')), approval)
        for field in ('status', 'blocked_reason', 'worker_session_id', 'retry_count',
                      'verify_failure_history', 'publication_sha'):
            self.assertEqual(self.row('G-003')[field], original[field])
        self.assertEqual(self.row('G-002')['current_revision_sha'], self.sha)

    def test_remote_and_sqlite_races_do_not_split_view_and_pending_causes(self):
        before = self.fixture.repository.capture(self.repo)
        for fault in ('before-revision-recheck', 'after-plan:G-001'):
            with self.subTest(fault=fault):
                def interrupt(stage):
                    if stage == fault:
                        raise RuntimeError('interrupted')
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    self.fixture._service(self.pinned(self.sha), fault_hook=interrupt).reconcile(self.repo)
                self.assertEqual(self.fixture.repository.capture(self.repo), before)
                self.assertIsNone(self.connection.execute('SELECT * FROM execplan_catalog_views').fetchone())
        result = self.fixture._service(self.pinned(self.sha), revisions=[self.sha, self.sha, 'c' * 40]).reconcile(self.repo)
        self.assertEqual(result.kind, state.ReconciliationKind.STALE)
        self.assertEqual(self.fixture.repository.capture(self.repo), before)
        self.assertEqual(self.held(), set())

    def test_shared_worktree_or_branch_rejects_execution_view(self):
        for field in ('worktree', 'branch'):
            with self.subTest(field=field):
                self.connection.execute(f'UPDATE exec_plans SET {field}=? WHERE plan_id IN (?, ?)',
                                        ('shared', 'G-001', 'G-002'))
                self.connection.commit()
                result = self.fixture._service(self.pinned(self.sha)).reconcile(self.repo)
                self.assertEqual(result.kind, state.ReconciliationKind.INVALID_CATALOG)
                self.assertEqual(result.diagnostic.code, 'shared-ownership')
                self.assertEqual(self.held(), set())
                self.connection.execute(f'UPDATE exec_plans SET {field}=NULL')
                self.connection.commit()

    def test_active_hold_preserves_session_and_waits_for_independent_execution_on_correction(self):
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', ever_started=1 WHERE plan_id='G-002'")
        self.connection.execute("UPDATE projects SET plan_path=?", ('docs/plans/[G-002] Dependency.md',))
        self.connection.commit()
        original = dict(self.row('G-002'))
        self.boundary.refresh()
        self.assertIsNone(manager.active_execplan(self.connection, self.repo))
        paused = dict(self.row('G-002'))
        self.assertEqual(paused.pop('status'), 'PLAN_BLOCKED')
        self.assertEqual(paused, {key: value for key, value in original.items() if key != 'status'})
        self.assertIsNone(self.connection.execute('SELECT plan_path FROM projects').fetchone()[0])
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING' WHERE plan_id='G-001'")
        self.connection.commit()
        self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
        self.sha = 'c' * 40
        self.boundary.refresh()
        self.assertEqual(self.held(), {'G-002'})
        self.assertEqual(manager.active_execplan(self.connection, self.repo)['plan_id'], 'G-001')
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-001'")
        self.connection.commit()
        self.boundary.refresh()
        self.assertEqual(self.held(), set())
        self.assertEqual(manager.active_execplan(self.connection, self.repo)['plan_id'], 'G-002')

    def test_missing_active_declaration_preserves_conflicting_project_owner(self):
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', ever_started=1 WHERE plan_id='G-002'")
        self.connection.execute("UPDATE projects SET plan_path=?", ('docs/plans/[G-002] Dependency.md',))
        self.connection.commit()
        original = dict(self.row('G-002'))
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        del self.documents['docs/plans/[G-002] Dependency.md']
        result = self.fixture._service(self.pinned(self.sha)).reconcile(self.repo)
        self.assertEqual(result.kind, state.ReconciliationKind.INVALID_CATALOG)
        self.assertEqual(result.diagnostic.code, 'shared-ownership')
        self.assertEqual(dict(self.row('G-002')), original)
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), project)
        self.assertFalse(self.held())

    def test_planning_reuses_execution_view(self):
        self.boundary.refresh()
        outcome, _ports = self.prepare_planning_workspace()
        self.assertIsNone(outcome)
        self.assertEqual(self.loads, [self.sha])

    def test_archive_adoption_does_not_discard_pending_errors(self):
        self.boundary.refresh()
        self.assertFalse(self.boundary.apply_archive(self.sha, 'c' * 40, ()))
        self.assertEqual(self.held(), {'G-002', 'G-003', 'G-004'})

    def test_project_step_reaches_pr_archive_gate_and_planning_with_scoped_errors(self):
        result = self.run_step(expected_priority='normal', expected_sources='["docs/PRODUCT.md"]',
                               expected_dependencies='[]')
        self.assertEqual(result, 0)
        self.assertEqual(self.events, ['fetch', 'catalog', 'pr', 'archive-eligibility', 'planning'])
        self.assertEqual(self.held(), {'G-002', 'G-003', 'G-004'})

    def test_scoped_adoption_does_not_touch_another_registered_project(self):
        project = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        project['repo'] = '/other-repo'
        plan = dict(self.row('G-001'))
        plan['repo'] = project['repo']
        for table, values in (('projects', project), ('exec_plans', plan)):
            self.connection.execute(f"INSERT INTO {table} ({', '.join(values)}) "
                                    f"VALUES ({', '.join('?' for _ in values)})", tuple(values.values()))
        self.connection.commit()
        self.boundary.refresh()
        self.assertEqual(dict(self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo='/other-repo'").fetchone()), plan)
        self.assertEqual(manager.scoped_blocking.catalog_held_plan_ids(self.connection, '/other-repo'), set())
        self.assertEqual(manager.select_ready_execplan(self.connection, '/other-repo')['plan_id'], 'G-001')

    def test_public_reconciliation_returns_execution_view_and_releases_only_old_catalog_project_block(self):
        port = fixtures.FakeDefinitionPort(self.pinned(self.sha))
        port.loaded = port.pinned
        with self.connection:
            manager.scoped_blocking.record_project_block(
                self.connection, self.repo, manager.BlockReasonCode.EXECPLAN_CATALOG_INVALID,
                'old global catalog failure', 'old', plan_id='G-002', previous_phase=None)
        with mock.patch.object(manager, '_GitCatalogDefinitionPort', return_value=port), \
             mock.patch.object(manager, 'backfill_plan_shas'), \
             mock.patch.object(manager, 'migrate_legacy_execplans'):
            result = manager.reconcile_execplan_catalog(self.connection, Path(self.repo), self.repo, 'main')
        self.assertEqual(result.kind, state.ReconciliationKind.EXECUTION_VIEW)
        self.assertIsNone(manager.scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.held(), {'G-002', 'G-003', 'G-004'})

    def test_related_sqlite_ownership_change_is_not_overwritten(self):
        pinned = self.pinned(self.sha)
        repository = state.DefinitionStateRepository(self.connection)
        original_apply = repository.apply
        def competing_owner(*args, **kwargs):
            self.connection.execute("UPDATE exec_plans SET worker_session_id='competing' WHERE plan_id='G-002'")
            self.connection.commit()
            return original_apply(*args, **kwargs)
        with mock.patch.object(repository, 'apply', side_effect=competing_owner):
            result = state.CatalogReconciliationService(repository, fixtures.FakeDefinitionPort(pinned)).reconcile(self.repo)
        self.assertEqual(result.kind, state.ReconciliationKind.STALE)
        self.assertEqual(self.held(), set())
        self.assertEqual(self.row('G-002')['worker_session_id'], 'competing')

    def test_pr_conflict_selection_skips_held_plan_even_if_cached_dependency_is_merged(self):
        from loop_manager_internal.recovery import waiting_pr_service
        from loop_manager_internal.application.contracts import WaitingPullRequestObservation
        self.connection.execute("UPDATE exec_plans SET status='PR_OPEN', branch='broken', pr_url='https://example/pr/2' WHERE plan_id='G-002'")
        self.connection.commit()
        self.boundary.refresh()
        effects = SimpleNamespace(active_execplan=manager.active_execplan,
            hydrate_execplan_project=mock.Mock(), reconcile_existing_pull_request=mock.Mock(return_value=None))
        observation = WaitingPullRequestObservation(
            merged=False, conflicts=((self.row('G-002'), {}), (self.row('G-005'), {})))
        waiting_pr_service.select_waiting_pull_request_conflict(
            effects, self.connection, Path(self.repo), Path('/state'), self.repo, observation)
        self.assertEqual(effects.hydrate_execplan_project.call_args.args[2]['plan_id'], 'G-005')

    def test_independent_planner_batch_retains_only_unchanged_proven_errors(self):
        import tempfile
        from loop_manager_internal.planning import planner_validation
        self.boundary.refresh()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, contents in {**self.documents, 'docs/PRODUCT.md': 'Product.',
                                   'docs/FUTURE_IDEAS.md': 'Deferred.'}.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding='utf-8', newline='\n')
            def git(_root, command, *arguments, **kwargs):
                path = arguments[-1]
                return SimpleNamespace(returncode=0, stdout=f'100644 blob abc\t{path}\0' if command == 'ls-tree' else '')
            effects = SimpleNamespace(changed_files=lambda *args: ['docs/FUTURE_IDEAS.md'],
                mutable_snapshot_token=lambda *args: 'token', git=git,
                load_execplan_catalog=manager.load_execplan_catalog,
                _validate_future_idea_separation=mock.Mock(),
                file_hash=lambda path: catalog.plan_content_hash(path.read_bytes()),
                _stored_string_list=planner_validation._stored_string_list)
            result = planner_validation.validate_planner_result(effects, root, self.sha, [],
                connection=self.connection, repo=self.repo)
            self.assertEqual(result.publication_paths, ('docs/FUTURE_IDEAS.md',))
            for mutation in ('cause', 'bytes', 'symlink'):
                with self.subTest(mutation=mutation):
                    target = root / 'docs/plans/[G-002] Dependency.md'
                    if mutation == 'cause':
                        target.write_text(self.fixture._text('G-002', 'Dependency', risk='other-invalid'), encoding='utf-8', newline='\n')
                    elif mutation == 'bytes':
                        target.write_text(self.documents['docs/plans/[G-002] Dependency.md'], encoding='utf-8', newline='\n')
                        effects.git = lambda root, command, *args, **kwargs: (SimpleNamespace(returncode=1, stdout="")
                            if command == "diff" else git(root, command, *args, **kwargs))
                    else:
                        effects.git = git
                    original_lstat = Path.lstat
                    observed = (mock.patch.object(Path, 'lstat', autospec=True, side_effect=lambda candidate, *a, **kw:
                                mock.Mock(st_mode=stat.S_IFLNK) if candidate == target
                                else original_lstat(candidate, *a, **kw))
                                if mutation == 'symlink' else contextlib.nullcontext())
                    with observed, self.assertRaises(planner_validation.PlannerValidationError):
                        planner_validation.validate_planner_result(effects, root, self.sha, [],
                            connection=self.connection, repo=self.repo)

    def test_invalid_new_declaration_is_reserved_and_can_be_corrected_after_independent_registration(self):
        broken_path = 'docs/plans/[G-006] New invalid.md'
        valid_path = 'docs/plans/[G-007] New independent.md'
        self.documents[broken_path] = self.fixture._text('G-006', 'New invalid', risk='invalid')
        self.documents[valid_path] = self.fixture._text('G-007', 'New independent')
        self.paths |= {broken_path, valid_path}
        self.boundary.refresh()
        self.assertIn('G-006', self.held())
        self.assertIsNone(self.row('G-006'))
        self.assertEqual(self.row('G-007')['status'], 'READY')
        self.documents[broken_path] = self.fixture._text('G-006', 'New invalid')
        self.sha = 'c' * 40
        self.boundary.refresh()
        self.assertNotIn('G-006', self.held())
        self.assertEqual(self.row('G-006')['status'], 'READY')

    def test_missing_priority_of_new_plan_is_withheld_with_its_dependents(self):
        new_path = 'docs/plans/[G-006] Legacy metadata.md'
        self.documents[new_path] = self.fixture._text('G-006', 'Legacy metadata').replace('priority: normal\n', '')
        dependent_path = 'docs/plans/[G-007] New dependent.md'
        self.documents[dependent_path] = self.fixture._text('G-007', 'New dependent', depends_on=('G-006',))
        self.paths |= {new_path, dependent_path}
        self.boundary.refresh()
        self.assertEqual(self.held(), {'G-002', 'G-003', 'G-004', 'G-006', 'G-007'})
        self.assertEqual(self.boundary.pinned.snapshot.withheld_ids, self.held())
        self.assertIsNone(self.row('G-006'))
        self.assertIsNone(self.row('G-007'))
        self.assertEqual(manager.select_ready_execplan(self.connection, self.repo)['plan_id'], 'G-001')

    def test_active_correction_preserves_a_separate_approval_added_while_held(self):
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', ever_started=1 WHERE plan_id='G-002'")
        self.connection.commit()
        self.boundary.refresh()
        with self.connection:
            manager.scoped_blocking.record_plan_block(self.connection, self.repo, 'G-002',
                manager.BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED, 'approval while held', 'approval')
        approval = dict(manager.scoped_blocking.plan_block(self.connection, self.repo, 'G-002'))
        self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
        self.sha = 'c' * 40
        self.boundary.refresh()
        self.assertEqual(self.row('G-002')['status'], 'PLAN_BLOCKED')
        self.assertEqual(dict(manager.scoped_blocking.plan_block(self.connection, self.repo, 'G-002')), approval)
        self.assertEqual(self.held(), set())

    def test_runtime_activation_and_projection_cannot_bypass_catalog_hold(self):
        from loop_manager_internal.storage import runtime_state
        self.boundary.refresh()
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with self.assertRaisesRegex(runtime_state.RuntimeStateError, 'catalog'):
            runtime.activate(self.repo, 'G-002', activation_sha=self.sha, branch='broken', worktree='/broken')
        with self.assertRaisesRegex(runtime_state.RuntimeStateError, 'catalog'):
            runtime.hydrate_snapshot(self.repo, runtime.get(self.repo, 'G-002'))

    def test_all_execution_phases_resume_without_losing_validation_or_sessions(self):
        for index, phase in enumerate(('IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING'), start=1):
            with self.subTest(phase=phase):
                self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency', risk='invalid')
                self.connection.execute(
                    "UPDATE exec_plans SET status=?, ever_started=1, worker_session_id='saved-worker', "
                    "reviewer_session_id='saved-reviewer', verify_result='saved-verify', review_result='saved-review' "
                    "WHERE plan_id='G-002'", (phase,))
                self.connection.commit()
                self.sha = f'{index:x}' * 40
                self.boundary.refresh()
                self.assertEqual(self.row('G-002')['status'], 'PLAN_BLOCKED')
                self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
                self.sha = f'{index + 4:x}' * 40
                self.boundary.refresh()
                row = self.row('G-002')
                self.assertEqual(row['status'], phase)
                self.assertEqual((row['worker_session_id'], row['reviewer_session_id'],
                                  row['verify_result'], row['review_result']),
                                 ('saved-worker', 'saved-reviewer', 'saved-verify', 'saved-review'))

    def test_legacy_backfill_receives_withheld_new_metadata_before_it_can_create_rows(self):
        self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
        path = 'docs/plans/[G-006] Missing priority.md'
        self.documents[path] = self.fixture._text('G-006', 'Missing priority').replace('priority: normal\n', '')
        self.paths |= {path}
        port = fixtures.FakeDefinitionPort(self.pinned(self.sha))
        port.loaded = port.pinned
        port.load = lambda: port.loaded
        def backfill(*args, **kwargs):
            pinned = kwargs['pinned_catalog']
            self.assertEqual(pinned.snapshot.withheld_ids, {'G-006'})
            self.assertNotIn('G-006', pinned.snapshot.active)
        with mock.patch.object(manager, '_GitCatalogDefinitionPort', return_value=port), \
             mock.patch.object(manager, 'backfill_plan_shas'), \
             mock.patch.object(manager, 'migrate_legacy_execplans', side_effect=backfill):
            result = manager.reconcile_execplan_catalog(self.connection, Path(self.repo), self.repo, 'main')
        self.assertEqual(result.kind, state.ReconciliationKind.EXECUTION_VIEW)
        self.assertEqual(self.held(), {'G-006'})
        self.assertIsNone(self.row('G-006'))

    def test_ambiguous_error_from_an_outdated_revision_does_not_create_a_project_block(self):
        diagnostic = state.ReconciliationDiagnostic(self.sha, 'duplicate-id', 'ambiguous IDs',
            'docs/plans/[G-002] Dependency.md', None, 'fingerprint')
        port = SimpleNamespace(loaded=None, load=mock.Mock(side_effect=state.InvalidPinnedCatalog(diagnostic)),
                               current_revision=lambda: 'c' * 40)
        with mock.patch.object(manager, '_GitCatalogDefinitionPort', return_value=port), \
             mock.patch.object(manager, 'git', return_value=SimpleNamespace(stdout=self.sha)):
            result = manager.reconcile_execplan_catalog(self.connection, Path(self.repo), self.repo, 'main',
                                                        remote_sha=self.sha)
        self.assertEqual(result.kind, state.ReconciliationKind.STALE)
        self.assertIsNone(manager.scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.held(), set())

    def test_pending_catalog_errors_prevent_false_completion_after_all_prs_merge(self):
        from loop_manager_internal.notifications import work_exhaustion
        self.boundary.refresh()
        self.connection.execute("UPDATE exec_plans SET status='MERGED'")
        self.connection.commit()
        snapshot = work_exhaustion.collect(self.connection, self.repo, cycle=self.sha, active_requirements=())
        self.assertFalse(snapshot.completed)
        self.assertTrue(snapshot.has_distinct_wait)
        self.assertEqual({item['id'] for item in snapshot.plans}, self.held())
        self.assertTrue(all(item['evidence'] for item in snapshot.plans))
        previously_complete = work_exhaustion.WorkReasonSnapshot(self.sha, (), (), None, None)
        self.assertFalse(manager.notifications.enqueue_completion_if_ready(self.connection, self.repo,
                                                                         previously_complete))

    def test_activation_rechecks_new_catalog_hold_inside_its_transaction(self):
        from loop_manager_internal.storage import runtime_state
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with mock.patch.object(manager.scoped_blocking, 'catalog_held_plan_ids',
                               side_effect=(frozenset(), frozenset({'G-002'}))), \
             self.assertRaisesRegex(runtime_state.RuntimeStateError, 'catalog'):
            runtime.activate(self.repo, 'G-002', activation_sha=self.sha, branch='broken', worktree='/broken')
        self.assertEqual(self.row('G-002')['status'], 'READY')

    def test_planner_cannot_add_dependency_on_pending_new_plan_with_missing_priority(self):
        import tempfile
        from loop_manager_internal.planning import planner_validation
        self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
        broken = 'docs/plans/[G-006] Missing priority.md'
        self.documents[broken] = self.fixture._text('G-006', 'Missing priority').replace('priority: normal\n', '')
        self.paths |= {broken}
        self.boundary.refresh()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = 'docs/plans/[G-007] Dependent.md'
            documents = {**self.documents, 'docs/PRODUCT.md': 'Product.',
                         path: self.fixture._text('G-007', 'Dependent', depends_on=('G-006',))}
            for relative, text in documents.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
            effects = SimpleNamespace(changed_files=lambda *args: [path],
                mutable_snapshot_token=lambda *args: 'token',
                git=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=''),
                _parse_execplan=catalog.parse_execplan_file,
                load_execplan_catalog=manager.load_execplan_catalog,
                _stored_string_list=planner_validation._stored_string_list)
            with self.assertRaisesRegex(planner_validation.PlannerValidationError, '보류 계획에 의존'):
                planner_validation.validate_planner_result(effects, root, self.sha, [],
                    connection=self.connection, repo=self.repo)

    def test_planner_can_publish_new_dependencies_on_held_plans_without_enabling_execution(self):
        import tempfile
        from loop_manager_internal.planning import planner_validation
        self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
        broken = 'docs/plans/[G-006] Held predecessor.md'
        self.fixture._insert('G-006', 'Held predecessor', status='READY')
        self.connection.execute('UPDATE exec_plans SET repo = ?', (self.repo,))
        self.connection.commit()
        self.documents[broken] = self.fixture._text('G-006', 'Held predecessor', risk='invalid')
        self.paths |= {broken}
        self.boundary.refresh()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = 'docs/plans/[G-007] Dependent.md'
            indirect = 'docs/plans/[G-008] Indirect.md'
            documents = {**self.documents, 'docs/PRODUCT.md': 'Product.',
                         path: self.fixture._text('G-007', 'Dependent', depends_on=('G-006',)),
                         indirect: self.fixture._text('G-008', 'Indirect', depends_on=('G-007',))}
            for relative, text in documents.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
            def git(_root, command, *args, **kwargs):
                item = args[-1]
                tree = f'100644 blob abc\t{item}\0' if item in self.documents else ''
                return SimpleNamespace(returncode=0, stdout=tree if command == 'ls-tree' else '')

            effects = SimpleNamespace(changed_files=lambda *args: [path, indirect],
                mutable_snapshot_token=lambda *args: 'token',
                git=git,
                _parse_execplan=catalog.parse_execplan_file,
                load_execplan_catalog=manager.load_execplan_catalog,
                _validate_future_idea_separation=mock.Mock(),
                file_hash=lambda target: catalog.plan_content_hash(target.read_bytes()),
                _stored_string_list=planner_validation._stored_string_list)
            result = planner_validation.validate_planner_result(effects, root, self.sha, [],
                connection=self.connection, repo=self.repo)
            self.assertEqual([plan['id'] for _, plan in result.plans], ['G-007', 'G-008'])
            self.assertEqual(set(result.publication_paths), {path, indirect})
            self.assertNotIn('G-007', self.held())
            for invalid in (
                self.fixture._text('G-008', 'Indirect', depends_on=('G-007', 'G-999')),
                self.fixture._text('G-008', 'Indirect', depends_on=('G-007', 'G-008')),
                documents[indirect].replace('docs/PRODUCT.md', 'docs/missing.md'),
            ):
                with self.subTest(invalid=invalid):
                    (root / indirect).write_text(invalid, encoding='utf-8', newline='\n')
                    with self.assertRaises(planner_validation.PlannerValidationError):
                        planner_validation.validate_planner_result(effects, root, self.sha, [],
                            connection=self.connection, repo=self.repo)
            (root / indirect).write_text(documents[indirect], encoding='utf-8', newline='\n')
            with mock.patch.object(manager, 'git_bytes', side_effect=lambda _root, _cmd, spec:
                                   documents[spec.split(':', 1)[1]].encode('utf-8')):
                manager._register_execplan_rows(self.connection, self.repo, root,
                                               list(result.plans), 'c' * 40)
            self.connection.commit()
            self.assertEqual(json.loads(self.row('G-007')['depends_on']), ['G-006'])
            self.assertEqual(json.loads(self.row('G-008')['depends_on']), ['G-007'])

        self.documents.update({key: documents[key] for key in (path, indirect)})
        self.paths |= {path, indirect}
        self.sha = 'c' * 40
        self.boundary.refresh()
        self.assertTrue({'G-006', 'G-007', 'G-008'} <= self.held())
        self.assertEqual(self.row('G-007')['status'], 'READY')
        self.assertEqual(manager.select_ready_execplan(self.connection, self.repo)['plan_id'], 'G-001')
        self.documents[broken] = self.fixture._text('G-006', 'Held predecessor')
        self.sha = 'd' * 40
        self.boundary.refresh()
        self.assertFalse(self.held())
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id < 'G-006'")
        self.assertEqual(manager.select_ready_execplan(self.connection, self.repo)['plan_id'], 'G-006')
        for merged, selected in (('G-006', 'G-007'), ('G-007', 'G-008')):
            self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id=?", (merged,))
            self.assertEqual(manager.select_ready_execplan(self.connection, self.repo)['plan_id'], selected)

    def test_planner_rejects_cycle_joining_registered_held_dependency_and_preserves_rows(self):
        import tempfile
        from loop_manager_internal.planning import planner_validation

        held_path = 'docs/plans/[G-006] Held predecessor.md'
        self.fixture._insert('G-006', 'Held predecessor', status='READY', retry_count=3)
        self.connection.execute(
            "UPDATE exec_plans SET depends_on='[\"G-001\"]', repo=? WHERE plan_id='G-006'",
            (self.repo,),
        )
        self.connection.commit()
        self.documents[held_path] = self.fixture._text(
            'G-006', 'Held predecessor', depends_on=('G-001',), risk='invalid',
        )
        self.paths |= {held_path}
        self.boundary.refresh()
        self.assertIn('G-006', self.held())
        before_rows = {
            plan_id: dict(self.row(plan_id)) for plan_id in ('G-001', 'G-006')
        }
        before_holds = [tuple(row) for row in self.connection.execute(
            'SELECT * FROM execplan_catalog_holds WHERE repo=? ORDER BY plan_id',
            (self.repo,),
        )]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = 'docs/plans/[G-001] Updated.md'
            from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
            from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot
            original = catalog.parse_execplan_text(path, self.documents[path], validate_sources=False)
            mutable = MutableReadySnapshot(self.repo, self.sha, (ReadyPlanSnapshot(
                plan_id='G-001', path=path, title='Updated',
                sources=tuple(original['sources']), depends_on=tuple(original['depends_on']),
                complexity=original['complexity'], risk=original['risk'],
                priority=original['priority'],
                content_hash=catalog.plan_content_hash(self.documents[path]),
                plan_sha=self.sha, text=self.documents[path], current_metadata_recorded=True,
            ),), 'token')
            documents = {**self.documents, 'docs/PRODUCT.md': 'Product.'}
            for relative, text in documents.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
            (root / path).write_text(
                self.fixture._text('G-001', 'Updated', depends_on=('G-006',)),
                encoding='utf-8', newline='\n',
            )

            def git(_root, command, *args, **kwargs):
                item = args[-1]
                tree = f'100644 blob abc\t{item}\0' if item in self.documents else ''
                return SimpleNamespace(returncode=0, stdout=tree if command == 'ls-tree' else '')

            effects = SimpleNamespace(
                changed_files=lambda *args: [path],
                mutable_snapshot_token=lambda *args: 'token',
                git=git,
                _parse_execplan=catalog.parse_execplan_file,
                load_execplan_catalog=manager.load_execplan_catalog,
                _validate_future_idea_separation=mock.Mock(),
                file_hash=lambda target: catalog.plan_content_hash(target.read_bytes()),
                _stored_string_list=planner_validation._stored_string_list,
                _assert_mutable_ready_snapshot_current=None,
            )
            with self.assertRaisesRegex(planner_validation.PlannerValidationError, '순환'):
                planner_validation.validate_planner_result(
                    effects, root, self.sha, [], connection=self.connection, repo=self.repo,
                    mutable_snapshot=mutable,
                )

        self.assertEqual({key: dict(self.row(key)) for key in before_rows}, before_rows)
        self.assertEqual([tuple(row) for row in self.connection.execute(
            'SELECT * FROM execplan_catalog_holds WHERE repo=? ORDER BY plan_id',
            (self.repo,),
        )], before_holds)

    def test_dependency_cycle_check_uses_batch_overlay_and_ignores_existing_held_cycle(self):
        held_ids = {'G-006', 'G-010', 'G-011'}
        original = {
            'G-006': ('G-007',),
            'G-010': ('G-011',),
            'G-011': ('G-010',),
        }

        with self.assertRaisesRegex(
            catalog.ExecPlanCatalogError,
            'G-007 -> G-008 -> G-001 -> G-006 -> G-007',
        ):
            refresh.assert_planning_dependency_references(
                {
                    'G-007': {'depends_on': []},
                    'G-008': {'depends_on': []},
                    'G-001': {'depends_on': []},
                },
                [
                    ('u.md', {'id': 'G-007', 'depends_on': ['G-008']}),
                    ('t.md', {'id': 'G-008', 'depends_on': ['G-001']}),
                    ('revision.md', {'id': 'G-001', 'depends_on': ['G-006']}),
                ],
                held_ids,
                original_dependencies=original,
            )

        refresh.assert_planning_dependency_references(
            {}, [('new.md', {'id': 'G-012', 'depends_on': ['G-010']})],
            {'G-010', 'G-011'}, original_dependencies=original,
        )

        with self.assertRaisesRegex(
            catalog.ExecPlanCatalogError,
            'G-004 -> G-003 -> G-002 -> G-004',
        ):
            refresh.assert_planning_dependency_references(
                {},
                [
                    ('changed.md', {'id': 'G-001', 'depends_on': ['G-002']}),
                    ('also-changed.md', {'id': 'G-004', 'depends_on': ['G-003']}),
                ],
                {'G-002', 'G-003'},
                original_dependencies={
                    'G-002': ('G-003', 'G-004'),
                    'G-003': ('G-002',),
                },
            )

    def test_independent_planning_reuses_missing_manager_priority_hold_cause(self):
        import tempfile
        from loop_manager_internal.catalog import manager_sources
        from loop_manager_internal.planning import planner_validation
        from loop_manager_internal.storage import verify_input_requests

        request = verify_input_requests.register(self.connection, self.repo, self.sha)
        broken_path = 'docs/plans/[G-006] Missing verify priority.md'
        self.documents[broken_path] = (
            '---\nid: G-006\ntitle: Missing verify priority\nsources:\n  - '
            + manager_sources.SOURCE
            + '\ndepends_on: []\ncomplexity: low\nrisk: low\n---\n\n'
            + '## Purpose\n\n' + manager_sources.RULE + '\n\n'
            + '## Acceptance Criteria\n\n- Keep the declared verify inputs complete.\n\n'
            + '## Implementation\n\n- Record the complete input list.\n\n'
            + '## Validation\n\n- Run the targeted unit tests.\n\n'
            + '## Decision Log\n\n' + manager_sources.PREFIX
            + json.dumps(request, sort_keys=True) + '\n'
        )
        self.paths |= {broken_path}
        self.boundary.refresh()
        self.assertIn('G-006', self.held())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            independent_path = 'docs/plans/[G-007] Independent.md'
            documents = {
                **self.documents,
                'docs/PRODUCT.md': 'Product.',
                independent_path: self.fixture._text('G-007', 'Independent'),
            }
            for relative, contents in documents.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding='utf-8', newline='\n')

            def git(_root, command, *args, **kwargs):
                item = args[-1]
                tree = f'100644 blob abc\t{item}\0' if item in self.documents else ''
                return SimpleNamespace(returncode=0, stdout=tree if command == 'ls-tree' else '')

            effects = SimpleNamespace(
                changed_files=lambda *args: [independent_path],
                mutable_snapshot_token=lambda *args: 'token',
                git=git,
                _parse_execplan=catalog.parse_execplan_file,
                load_execplan_catalog=manager.load_execplan_catalog,
                _validate_future_idea_separation=mock.Mock(),
                file_hash=lambda target: catalog.plan_content_hash(target.read_bytes()),
                _stored_string_list=planner_validation._stored_string_list,
            )
            result = planner_validation.validate_planner_result(
                effects, root, self.sha, [], connection=self.connection, repo=self.repo,
            )

        self.assertEqual([plan['id'] for _path, plan in result.plans], ['G-007'])
        held_evidence = json.loads(self.connection.execute(
            'SELECT evidence FROM execplan_catalog_holds WHERE repo=? AND plan_id=?',
            (self.repo, 'G-006'),
        ).fetchone()[0])
        self.assertEqual({cause['code'] for cause in held_evidence['causes']}, {'invalid-plan'})

    def test_new_planner_error_preserves_catalog_diagnostic_without_existing_holds(self):
        import tempfile
        from loop_manager_internal.planning import planner_validation
        self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
        self.boundary.refresh()
        self.assertFalse(self.held())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = 'docs/plans/[G-006] New invalid.md'
            documents = {**self.documents, 'docs/PRODUCT.md': 'Product.',
                         path: self.fixture._text('G-006', 'New invalid', depends_on=('G-999',))}
            for relative, text in documents.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
            effects = SimpleNamespace(changed_files=lambda *args: [path],
                mutable_snapshot_token=lambda *args: 'token',
                git=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=''),
                _parse_execplan=catalog.parse_execplan_file,
                load_execplan_catalog=manager.load_execplan_catalog)
            with self.assertRaisesRegex(planner_validation.PlannerValidationError,
                                        '존재하지 않는 depends_on ID.*G-999'):
                planner_validation.validate_planner_result(effects, root, self.sha, [],
                    connection=self.connection, repo=self.repo)

    def test_independent_planner_accepts_combined_risk_and_new_declaration_errors(self):
        import tempfile
        from loop_manager_internal.planning import planner_validation
        for reason in ('priority', 'active-source'):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                broken = 'docs/plans/[G-006] New invalid.md'
                text = self.fixture._text('G-006', 'New invalid')
                if reason == 'priority':
                    text = text.replace('priority: normal\n', '')
                else:
                    text = text.replace('docs/PRODUCT.md', 'docs/requirements/Moved.md')
                    self.paths |= {'docs/requirements/archive/Moved.md'}
                self.documents[broken] = text
                self.paths |= {broken}
                self.sha = ('c' if reason == 'priority' else 'd') * 40
                self.boundary.refresh()
                path = 'docs/plans/[G-007] Independent.md'
                documents = {**self.documents, 'docs/PRODUCT.md': 'Product.',
                    'docs/requirements/archive/Moved.md': 'Moved.',
                    path: self.fixture._text('G-007', 'Independent')}
                for relative, contents in documents.items():
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(contents, encoding='utf-8', newline='\n')
                def git(_root, command, *args, **kwargs):
                    item = args[-1]
                    tree = f'100644 blob abc\t{item}\0' if item in self.documents else ''
                    return SimpleNamespace(returncode=0, stdout=tree if command == 'ls-tree' else '')
                effects = SimpleNamespace(changed_files=lambda *args: [path],
                    mutable_snapshot_token=lambda *args: 'token', git=git,
                    _parse_execplan=catalog.parse_execplan_file,
                    load_execplan_catalog=manager.load_execplan_catalog,
                    _validate_future_idea_separation=mock.Mock(),
                    file_hash=lambda target: catalog.plan_content_hash(target.read_bytes()),
                    _stored_string_list=planner_validation._stored_string_list)
                result = planner_validation.validate_planner_result(effects, root, self.sha, [],
                    connection=self.connection, repo=self.repo)
                self.assertEqual([plan['id'] for _, plan in result.plans], ['G-007'])
                self.assertEqual(self.held(), {'G-002', 'G-003', 'G-004', 'G-006'})
                (root / broken).write_text(text + '\nChanged withheld body.\n', encoding='utf-8', newline='\n')
                with self.assertRaisesRegex(planner_validation.PlannerValidationError, '원인이나 범위'):
                    planner_validation.validate_planner_result(effects, root, self.sha, [],
                        connection=self.connection, repo=self.repo)
                (root / broken).write_text(text, encoding='utf-8', newline='\n')
                if reason == 'priority':
                    (root / broken).write_text(text.replace('risk: low\n', 'risk: low\npriority: normal\n'), encoding='utf-8', newline='\n')
                else:
                    (root / 'docs/requirements/Moved.md').write_text('Moved.', encoding='utf-8', newline='\n')
                with self.assertRaisesRegex(planner_validation.PlannerValidationError, '원인이나 범위'):
                    planner_validation.validate_planner_result(effects, root, self.sha, [],
                        connection=self.connection, repo=self.repo)

    def test_corrected_execution_waits_for_planner_slot_and_resumes_after_release(self):
        self.connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', ever_started=1 WHERE plan_id='G-002'")
        self.connection.execute("UPDATE projects SET plan_path=?", ('docs/plans/[G-002] Dependency.md',))
        self.connection.commit()
        original = dict(self.row('G-002'))
        self.boundary.refresh()
        self.connection.execute("UPDATE projects SET phase='PLANNING', planner_session_id='independent-planner', "
                                "planning_workspace_owned=1, branch='planning', worktree='/planning', agent_snapshot='saved'")
        self.connection.commit()
        planning = dict(self.connection.execute('SELECT * FROM projects').fetchone())
        self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text('G-002', 'Dependency')
        self.sha = 'c' * 40
        self.boundary.refresh()
        self.assertEqual(self.row('G-002')['status'], 'PLAN_BLOCKED')
        self.assertEqual(self.row('G-002')['worker_session_id'], original['worker_session_id'])
        self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), planning)
        self.assertEqual(self.held(), {'G-002'})
        hold = self.connection.execute('SELECT resume_status, evidence FROM execplan_catalog_holds').fetchone()
        self.assertEqual(hold['resume_status'], 'IMPLEMENTING')
        self.assertIn('execution-slot-busy', hold['evidence'])
        # Preserved Planner evidence also owns the slot outside the live phase.
        self.connection.execute("UPDATE projects SET phase='IDLE', planning_workspace_owned=0")
        self.connection.commit()
        self.boundary.refresh()
        self.assertEqual(self.row('G-002')['status'], 'PLAN_BLOCKED')
        self.connection.execute("UPDATE projects SET planner_session_id=NULL, agent_snapshot=NULL, branch=NULL, worktree=NULL")
        self.connection.commit()
        self.boundary.refresh()
        self.assertEqual(self.row('G-002')['status'], 'IMPLEMENTING')
        self.assertEqual(self.row('G-002')['worker_session_id'], original['worker_session_id'])
        self.assertFalse(self.held())
        # A missing held path needs a fresh deletion check even at the same
        # revision while independent Planner evidence still owns the slot.
        self.assertEqual(self.loads, ['b' * 40, 'c' * 40, 'c' * 40, 'c' * 40])

    def test_planner_slot_ownership_change_invalidates_catalog_cas(self):
        repository = state.DefinitionStateRepository(self.connection)
        expected = repository.capture(self.repo)
        self.connection.execute("UPDATE projects SET planning_attempt_fingerprint='concurrent-planner'")
        self.connection.commit()
        self.assertIsNone(repository.apply(self.repo, self.pinned(self.sha), expected,
            revision_is_current=lambda: True))
        self.assertFalse(self.held())

    def test_maintenance_barrier_excludes_holds_and_validates_independent_batch(self):
        import tempfile
        from loop_manager_internal.storage import maintenance_state
        self.connection.execute("UPDATE exec_plans SET workspace_owned=0, worker_session_id=NULL, "
                                "reviewer_session_id=NULL, retry_count=0, ever_started=0 WHERE plan_id='G-001'")
        self.documents['docs/plans/[G-001] Updated.md'] = fixtures.plan_text('G-001', 'Updated')
        broken = 'docs/plans/[G-006] New invalid.md'
        self.documents[broken] = self.fixture._text('G-006', 'New invalid').replace('priority: normal\n', '')
        self.paths |= {broken}
        self.connection.commit()
        self.boundary.refresh()
        maintenance_state.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO maintenance_cycles (repo, phase, cycle_base_sha) VALUES (?, 'assessment', ?)",
                                (self.repo, self.sha))
        self.connection.commit()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, contents in {**self.documents, 'docs/PRODUCT.md': 'Product.'}.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents, encoding='utf-8', newline='\n')
            def snapshot_at(worktree, sha, row):
                return manager._ready_plan_snapshot_from_contents(row, (worktree / row['path']).read_bytes())
            with mock.patch.object(manager, '_ready_plan_snapshot', side_effect=snapshot_at) as snapshots:
                context = manager.prepare_maintenance_planning_context(self.connection, self.repo, root, self.sha)
            self.assertEqual([plan.plan_id for plan in context.eligible_plans], ['G-001'])
            snapshots.assert_called_once()
            cycle = maintenance_state.MaintenanceRepository(self.connection).get(self.repo)
            with mock.patch.object(manager, '_ready_plan_snapshot_at_revision', side_effect=snapshot_at) as snapshots:
                restored_context = manager._maintenance_context_at_revision(
                    self.connection, self.repo, root, self.sha, cycle)
            self.assertEqual(restored_context, context)
            snapshots.assert_called_once()
            self.assertNotEqual(self.row('G-002')['content_hash'], catalog.plan_content_hash(
                (root / self.row('G-002')['path']).read_bytes()))
            path = 'docs/plans/[G-007] Refactor.md'
            (root / path).write_text(fixtures.plan_text('G-007', 'Refactor').replace(
                'depends_on: []', 'depends_on:\n  - G-002'), encoding='utf-8', newline='\n')
            ready = context.eligible_plans[0]
            (root / ready.path).write_text(ready.text.replace('depends_on: []', 'depends_on:\n  - G-007'), encoding='utf-8', newline='\n')
            def git(_root, command, *args, **kwargs):
                item = args[-1]
                tree = f'100644 blob abc\t{item}\0' if item in self.documents else ''
                return SimpleNamespace(returncode=0, stdout=tree if command == 'ls-tree' else '')
            with mock.patch.object(manager, 'git', side_effect=git), \
                 mock.patch.object(manager, '_git_tree_entries', return_value={
                     relative.encode(): (b'blob', b'abc') for relative in self.documents}), \
                 mock.patch.object(manager, 'changed_files', return_value=[]):
                result = manager.validate_maintenance_planner_result(
                    self.connection, context, root, [ready.path, path])
                self.assertEqual([plan['id'] for _, plan in result.plans], ['G-007'])
                self.assertEqual([revision.snapshot.plan_id for revision in result.revisions], ['G-001'])
                for mutation in ('held-body', 'unregistered-dependency', 'new-priority'):
                    with self.subTest(mutation=mutation):
                        (root / broken).write_text(self.documents[broken], encoding='utf-8', newline='\n')
                        text = fixtures.plan_text('G-007', 'Refactor')
                        if mutation == 'held-body':
                            (root / broken).write_text(self.documents[broken] + '\nChanged.\n', encoding='utf-8', newline='\n')
                        elif mutation == 'unregistered-dependency':
                            text = text.replace('depends_on: []', 'depends_on:\n  - G-006')
                        else:
                            text = text.replace('priority: normal\n', '')
                        (root / path).write_text(text, encoding='utf-8', newline='\n')
                        with self.assertRaises(manager.PlannerValidationError):
                            manager.validate_maintenance_planner_result(
                                self.connection, context, root, [ready.path, path])
                (root / path).write_text(fixtures.plan_text('G-007', 'Refactor').replace(
                'depends_on: []', 'depends_on:\n  - G-002'), encoding='utf-8', newline='\n')
                (root / broken).write_text(self.documents[broken], encoding='utf-8', newline='\n')
                self.connection.execute("INSERT INTO execplan_catalog_holds "
                    "SELECT repo, 'G-001', revision_sha, evidence, NULL FROM execplan_catalog_holds LIMIT 1")
                with self.assertRaisesRegex(manager.PlannerValidationError, 'eligible READY snapshot'):
                    manager.validate_maintenance_planner_result(
                        self.connection, context, root, [ready.path, path])

    def test_maintenance_context_refuses_held_required_revalidation(self):
        from loop_manager_internal.maintenance import maintenance_validation
        from loop_manager_internal.storage import maintenance_state
        self.boundary.refresh()
        cycle = SimpleNamespace(phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
                                revalidation_plan_ids=('G-002',))
        effects = maintenance_validation.MaintenanceModeAndRowsEffects(is_unowned_ready=manager.is_unowned_ready)
        with self.assertRaisesRegex(manager.MaintenancePlanningError, 'catalog 보류'):
            maintenance_validation._maintenance_mode_and_rows(effects, self.connection, self.repo, cycle, self.sha)

    @contextlib.contextmanager
    def _scoped_maintenance_publication(self, *, registered_hold=False, revalidation=False):
        import hashlib
        import tempfile
        from loop_manager_internal.storage import maintenance_state
        self.connection.execute("UPDATE exec_plans SET workspace_owned=0, worker_session_id=NULL, "
                                "reviewer_session_id=NULL, retry_count=0, ever_started=0 WHERE plan_id='G-001'")
        self.documents['docs/plans/[G-001] Updated.md'] = fixtures.plan_text('G-001', 'Updated')
        broken = 'docs/plans/[G-006] New invalid.md'
        if registered_hold:
            self.fixture._insert('G-006', 'New invalid', status='READY', retry_count=2)
            held_dependency = 'G-002' if revalidation else 'G-001'
            self.connection.execute(
                "UPDATE exec_plans SET depends_on=?, repo=? WHERE plan_id='G-006'",
                (json.dumps([held_dependency]), self.repo),
            )
            self.documents[broken] = self.fixture._text(
                'G-006', 'New invalid', depends_on=(held_dependency,), risk='invalid',
            )
            if revalidation:
                self.connection.execute(
                    "UPDATE exec_plans SET depends_on='[\"G-001\"]' "
                    "WHERE repo=? AND plan_id='G-002'",
                    (self.repo,),
                )
                self.documents['docs/plans/[G-002] Dependency.md'] = self.fixture._text(
                    'G-002', 'Dependency', depends_on=('G-001',),
                )
        else:
            self.documents[broken] = self.fixture._text('G-006', 'New invalid').replace(
                'priority: normal\n', '',
            )
        self.paths |= {broken}
        self.connection.commit()
        self.boundary.refresh()
        maintenance_state.ensure_schema(self.connection)
        self.connection.execute("INSERT INTO maintenance_cycles (repo, phase, cycle_base_sha) VALUES (?, 'assessment', ?)",
                                (self.repo, self.sha))
        self.connection.commit()
        if revalidation:
            cycle_store = maintenance_state.MaintenanceRepository(self.connection)
            cycle = cycle_store.get(self.repo)
            cycle = cycle_store.transition(
                self.repo, expected_phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                expected_revision=cycle.revision,
                next_phase=maintenance_state.MaintenancePhase.REFACTORING,
            )
            cycle_store.transition(
                self.repo, expected_phase=maintenance_state.MaintenancePhase.REFACTORING,
                expected_revision=cycle.revision,
                next_phase=maintenance_state.MaintenancePhase.POST_REFACTOR_REVALIDATION,
                revalidation_plan_ids=('G-001',),
            )
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as ports:
            root = Path(directory)
            baseline = {**self.documents, 'docs/PRODUCT.md': 'Product.'}
            for relative, text in baseline.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8', newline='\n')
            def snapshot_at(worktree, sha, row):
                return manager._ready_plan_snapshot_from_contents(row, baseline[row['path']].encode())
            ports.enter_context(mock.patch.object(manager, '_ready_plan_snapshot', side_effect=snapshot_at))
            ports.enter_context(mock.patch.object(manager, '_ready_plan_snapshot_at_revision', side_effect=snapshot_at))
            context = manager.prepare_maintenance_planning_context(self.connection, self.repo, root, self.sha)
            ready = context.eligible_plans[0]
            if revalidation:
                path = None
                (root / ready.path).write_text(
                    ready.text.replace('Keep the boundary explicit.',
                                       'Keep the boundary explicit and verifiable.'),
                    encoding='utf-8', newline='\n',
                )
                publication_paths = (ready.path,)
            else:
                path = 'docs/plans/[G-007] Refactor.md'
                (root / path).write_text(fixtures.plan_text('G-007', 'Refactor').replace(
                    'depends_on: []', 'depends_on:\n  - G-002'), encoding='utf-8', newline='\n')
                (root / ready.path).write_text(ready.text.replace(
                    'depends_on: []', 'depends_on:\n  - G-007'),
                    encoding='utf-8', newline='\n',
                )
                publication_paths = (ready.path, path)
            batch = SimpleNamespace(root=root, context=context, commit='e' * 40, remote=self.sha,
                                    publication_paths=publication_paths, broken=broken)
            committed = {relative: (root / relative).read_bytes() for relative in batch.publication_paths}
            base_tree = {relative.encode(): (b'blob', hashlib.sha1(text.encode()).hexdigest().encode())
                         for relative, text in baseline.items()}
            plan_tree = {**base_tree, **{relative.encode(): (b'blob', hashlib.sha1(data).hexdigest().encode())
                                      for relative, data in committed.items()}}
            remote_tree = {**plan_tree, b'docs/notes.md': (b'blob', b'f' * 40)}
            def git(_root, command, *args, **kwargs):
                stdout, returncode = '', 0
                if command == 'rev-parse':
                    stdout = batch.commit if args == ('HEAD',) else batch.remote
                elif command == 'rev-list':
                    stdout = f'{batch.commit} {self.sha}'
                elif command == 'ls-tree':
                    stdout = f'100644 blob abc\t{args[-1]}\0' if args[-1] in baseline else ''
                elif command == 'diff' and '--name-only' in args:
                    stdout = '\n'.join(batch.publication_paths)
                elif command == 'diff' and '--quiet' in args:
                    returncode = int((root / args[-1]).read_bytes() != baseline[args[-1]].encode())
                return SimpleNamespace(stdout=stdout, returncode=returncode)
            ports.enter_context(mock.patch.object(manager, 'git', side_effect=git))
            ports.enter_context(mock.patch.object(manager, 'git_bytes', side_effect=lambda root, cmd, spec: committed[spec.split(':', 1)[1]]))
            ports.enter_context(mock.patch.object(manager, '_git_tree_entries', side_effect=lambda root, sha:
                base_tree if sha == self.sha else plan_tree if sha == batch.commit else remote_tree))
            ports.enter_context(mock.patch.object(manager, 'changed_files', return_value=list(batch.publication_paths)))
            ports.enter_context(mock.patch.object(manager, 'is_loop_manager_repository', return_value=False))
            ports.enter_context(mock.patch.object(manager, 'plan_declarations', return_value={}))
            batch.result = manager.validate_maintenance_planner_result(
                self.connection, context, root, list(batch.publication_paths))
            yield batch

    def _assert_scoped_maintenance_recovery(self, *, remote_advances):
        import sqlite3
        from loop_manager_internal.storage import maintenance_state
        with self._scoped_maintenance_publication() as batch:
            before = {key: dict(self.row(key)) for key in self.held() if self.row(key) is not None}
            holds = [tuple(row) for row in self.connection.execute('SELECT * FROM execplan_catalog_holds ORDER BY plan_id')]
            intent = manager.record_maintenance_publication_intent(
                self.connection, self.repo, batch.root, 'main', batch.result, batch.commit)
            self.assertEqual(intent.publication_stage, 'prepared')
            self.assertEqual(intent.publication_refactoring_plan_ids, ('G-007',))
            self.assertIsNone(self.row('G-007'))
            # Only durable SQLite state and committed blobs survive this restart.
            restarted = sqlite3.connect(':memory:')
            restarted.row_factory = sqlite3.Row
            self.addCleanup(restarted.close)
            self.connection.backup(restarted)
            batch.remote = 'f' * 40 if remote_advances else batch.commit
            cycle = manager.recover_maintenance_planning_batch(restarted, self.repo, batch.root, 'main')
            self.assertEqual(cycle.phase, maintenance_state.MaintenancePhase.REFACTORING)
            self.assertEqual(cycle.publication_remote_oid, batch.commit)
            self.assertEqual(cycle.refactoring_plan_ids, ('G-007',))
            self.assertEqual(restarted.execute("SELECT status FROM exec_plans WHERE plan_id='G-007'").fetchone()[0], 'READY')
            self.assertEqual(restarted.execute("SELECT depends_on FROM exec_plans WHERE plan_id='G-001'").fetchone()[0], '["G-007"]')
            for key, row in before.items():
                self.assertEqual(dict(restarted.execute('SELECT * FROM exec_plans WHERE plan_id=?', (key,)).fetchone()), row)
            self.assertEqual([tuple(row) for row in restarted.execute('SELECT * FROM execplan_catalog_holds ORDER BY plan_id')], holds)
            again = manager.recover_maintenance_planning_batch(restarted, self.repo, batch.root, 'main')
            self.assertEqual(again, cycle)
            self.assertEqual(restarted.execute("SELECT COUNT(*) FROM exec_plans WHERE plan_id='G-007'").fetchone()[0], 1)

    def test_maintenance_intent_recovers_scoped_batch_after_restart(self):
        self._assert_scoped_maintenance_recovery(remote_advances=False)

    def test_maintenance_intent_recovers_scoped_batch_after_remote_advances(self):
        self._assert_scoped_maintenance_recovery(remote_advances=True)

    def test_maintenance_validation_and_publication_recheck_held_cycle(self):
        import hashlib
        from loop_manager_internal.maintenance import maintenance_publication
        from loop_manager_internal.storage import maintenance_state

        with self._scoped_maintenance_publication(
            registered_hold=True, revalidation=True,
        ) as batch:
            self.assertIn('G-006', self.held())
            held_before = dict(self.row('G-006'))
            path_plan_before = dict(self.row('G-002'))
            ready_before = dict(self.row('G-001'))
            holds_before = [tuple(row) for row in self.connection.execute(
                'SELECT * FROM execplan_catalog_holds WHERE repo=? ORDER BY plan_id',
                (self.repo,),
            )]
            ready = batch.context.eligible_plans[0]
            (batch.root / ready.path).write_text(
                ready.text.replace('depends_on: []', 'depends_on:\n  - G-006'),
                encoding='utf-8', newline='\n',
            )
            with self.assertRaisesRegex(manager.PlannerValidationError, '순환'):
                manager.validate_maintenance_planner_result(
                    self.connection, batch.context, batch.root, [ready.path],
                )
            self.assertEqual(dict(self.row('G-006')), held_before)
            self.assertEqual(dict(self.row('G-002')), path_plan_before)
            self.assertEqual(dict(self.row('G-001')), ready_before)
            self.assertEqual([tuple(row) for row in self.connection.execute(
                'SELECT * FROM execplan_catalog_holds WHERE repo=? ORDER BY plan_id',
                (self.repo,),
            )], holds_before)
            cycle_state = maintenance_state.MaintenanceRepository(self.connection).get(self.repo)
            self.assertIsNone(cycle_state.publication_stage)
            self.assertIsNone(self.row('G-007'))

            cycle_plan = catalog.parse_execplan_file(
                batch.root, batch.root / ready.path, validate_sources=False,
            )
            revised = replace(batch.result.revisions[0], plan=cycle_plan)
            hashes = tuple(
                (path, hashlib.sha256((batch.root / path).read_bytes()).hexdigest())
                for path in batch.result.publication_paths
            )
            cycle_result = replace(
                batch.result, revisions=(revised,), publication_hashes=hashes,
            )
            committed = {
                path: (batch.root / path).read_bytes()
                for path in cycle_result.publication_paths
            }
            batch.remote = batch.commit
            planning_effects = maintenance_publication.ValidateMaintenancePlanningCommitEffects(
                _parse_execplan=catalog.parse_execplan_file,
                git=manager.git,
                git_bytes=lambda _root, _command, spec: committed[spec.split(':', 1)[1]],
                load_execplan_catalog=manager.load_execplan_catalog,
                load_execplan_snapshot=manager.load_execplan_snapshot,
            )
            publication_effects = maintenance_publication.ValidateMaintenancePublicationCommitEffects(
                _validate_maintenance_planning_commit=lambda root, result, commit, **kwargs:
                    maintenance_publication._validate_maintenance_planning_commit(
                        planning_effects, root, result, commit, **kwargs,
                    ),
                git=manager.git,
            )
            with self.assertRaisesRegex(manager.MaintenancePlanningError, '순환'):
                maintenance_publication._validate_maintenance_publication_commit(
                    publication_effects, batch.root, 'main', cycle_result, batch.commit,
                    connection=self.connection,
                )
            self.assertEqual(dict(self.row('G-006')), held_before)
            self.assertEqual(dict(self.row('G-002')), path_plan_before)
            self.assertEqual(dict(self.row('G-001')), ready_before)
            self.assertEqual([tuple(row) for row in self.connection.execute(
                'SELECT * FROM execplan_catalog_holds WHERE repo=? ORDER BY plan_id',
                (self.repo,),
            )], holds_before)
            cycle_state = maintenance_state.MaintenanceRepository(self.connection).get(self.repo)
            self.assertIsNone(cycle_state.publication_stage)
            self.assertIsNone(self.row('G-007'))

    def test_maintenance_new_plan_batch_cycle_is_rejected_before_intent(self):
        import hashlib
        from loop_manager_internal.maintenance import maintenance_publication
        from loop_manager_internal.storage import maintenance_state

        with self._scoped_maintenance_publication(registered_hold=True) as batch:
            ready = batch.context.eligible_plans[0]
            first_path, _first_plan = batch.result.plans[0]
            second_path = 'docs/plans/[G-008] Second.md'
            (batch.root / ready.path).write_text(
                ready.text, encoding='utf-8', newline='\n',
            )
            first_text = (batch.root / first_path).read_text(encoding='utf-8')
            first_text = first_text.replace(
                '  - G-002', '  - G-008\n  - G-006',
            )
            (batch.root / first_path).write_text(
                first_text, encoding='utf-8', newline='\n',
            )
            (batch.root / second_path).write_text(
                fixtures.plan_text('G-008', 'Second').replace(
                    'depends_on: []', 'depends_on:\n  - G-007',
                ), encoding='utf-8', newline='\n',
            )
            paths = tuple(sorted((first_path, second_path)))
            with mock.patch.object(manager, 'changed_files', return_value=list(paths)):
                with self.assertRaises(manager.PlannerValidationError):
                    manager.validate_maintenance_planner_result(
                        self.connection, batch.context, batch.root, list(paths),
                    )

            held_before = dict(self.row('G-006'))
            ready_before = dict(self.row('G-001'))
            holds_before = [tuple(row) for row in self.connection.execute(
                'SELECT * FROM execplan_catalog_holds WHERE repo=? ORDER BY plan_id',
                (self.repo,),
            )]
            cycle_plans = tuple(
                (path, catalog.parse_execplan_file(
                    batch.root, batch.root / path, validate_sources=False,
                ))
                for path in paths
            )
            hashes = tuple(
                (path, hashlib.sha256((batch.root / path).read_bytes()).hexdigest())
                for path in paths
            )
            cycle_result = replace(
                batch.result,
                plans=cycle_plans,
                publication_paths=paths,
                publication_hashes=hashes,
                revisions=(),
                refactoring_plan_ids=('G-007', 'G-008'),
            )
            committed = {path: (batch.root / path).read_bytes() for path in paths}
            batch.publication_paths = paths
            batch.remote = batch.commit
            planning_effects = maintenance_publication.ValidateMaintenancePlanningCommitEffects(
                _parse_execplan=catalog.parse_execplan_file,
                git=manager.git,
                git_bytes=lambda _root, _command, spec: committed[spec.split(':', 1)[1]],
                load_execplan_catalog=manager.load_execplan_catalog,
                load_execplan_snapshot=manager.load_execplan_snapshot,
            )
            publication_effects = maintenance_publication.ValidateMaintenancePublicationCommitEffects(
                _validate_maintenance_planning_commit=lambda root, result, commit, **kwargs:
                    maintenance_publication._validate_maintenance_planning_commit(
                        planning_effects, root, result, commit, **kwargs,
                    ),
                git=manager.git,
            )
            with self.assertRaises(manager.MaintenancePlanningError):
                maintenance_publication._validate_maintenance_publication_commit(
                    publication_effects, batch.root, 'main', cycle_result, batch.commit,
                    connection=self.connection,
                )
            self.assertIsNone(self.row('G-007'))
            self.assertIsNone(self.row('G-008'))
            self.assertEqual(dict(self.row('G-006')), held_before)
            self.assertEqual(dict(self.row('G-001')), ready_before)
            self.assertEqual([tuple(row) for row in self.connection.execute(
                'SELECT * FROM execplan_catalog_holds WHERE repo=? ORDER BY plan_id',
                (self.repo,),
            )], holds_before)
            cycle_state = maintenance_state.MaintenanceRepository(self.connection).get(self.repo)
            self.assertIsNone(cycle_state.publication_stage)

    def test_maintenance_publication_rejects_changed_hold_and_preserves_intent(self):
        from loop_manager_internal.storage import maintenance_state
        with self._scoped_maintenance_publication() as batch:
            target = batch.root / batch.broken
            baseline = target.read_bytes()
            holds = [tuple(row) for row in self.connection.execute('SELECT * FROM execplan_catalog_holds ORDER BY plan_id')]
            target.write_bytes(baseline + b'\nChanged withheld file.\n')
            with self.assertRaisesRegex(manager.MaintenancePlanningError, '원인이나 범위'):
                manager.record_maintenance_publication_intent(
                    self.connection, self.repo, batch.root, 'main', batch.result, batch.commit)
            self.assertIsNone(maintenance_state.MaintenanceRepository(self.connection).get(self.repo).publication_stage)
            target.write_bytes(baseline)
            intent = manager.record_maintenance_publication_intent(
                self.connection, self.repo, batch.root, 'main', batch.result, batch.commit)
            batch.remote = batch.commit
            for mutation in ('held-file', 'batch-file'):
                with self.subTest(mutation=mutation):
                    target = batch.root / (batch.broken if mutation == 'held-file' else batch.publication_paths[-1])
                    data = target.read_bytes()
                    target.write_bytes(data + b'\nChanged after intent.\n')
                    with self.assertRaises(manager.MaintenancePlanningError):
                        manager.recover_maintenance_planning_batch(self.connection, self.repo, batch.root, 'main')
                    self.assertEqual(maintenance_state.MaintenanceRepository(self.connection).get(self.repo), intent)
                    self.assertIsNone(self.row('G-007'))
                    self.assertEqual([tuple(row) for row in self.connection.execute('SELECT * FROM execplan_catalog_holds ORDER BY plan_id')], holds)
                    target.write_bytes(data)
