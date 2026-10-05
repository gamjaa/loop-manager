"""Unit coverage of the catalog boundary and archive transaction adoption."""
import contextlib
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


class ProjectCatalogRefreshTest(unittest.TestCase):
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
        if result.kind is state.ReconciliationKind.RECONCILED:
            remember(pinned)
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

    def test_verified_archive_delta_and_audit_are_atomic_without_reread(self):
        self.assertEqual(self.archive(), archive_state.ArchiveWriteResult.APPLIED)
        item = self.archive_repo.item(self.repo, 'G-002')
        self.assertEqual(item['archived'], 1)
        self.assertEqual(self.row('G-002')['path'], 'docs/plans/archive/[G-002] Dependency.md')
        self.assertEqual(self.row('G-002')['current_revision_sha'], self.sha)
        self.assertEqual(self.row('G-002')['status'], 'MERGED')
        self.boundary.refresh()
        self.assertEqual(len(self.loads), 1)

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

    def run_step(self, *, pr=None, phase=None, invalid=False, scope_error=False):
        self.connection.execute("UPDATE projects SET phase=?, agent_snapshot=NULL, branch=NULL, worktree=NULL, plan_path=NULL", (phase,))
        self.connection.execute("UPDATE exec_plans SET status='READY', branch=NULL, worktree=NULL, pr_url=NULL")
        if scope_error:
            self.connection.execute("UPDATE projects SET agent_snapshot='saved'")
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
            self.assertEqual(self.row()['priority'], 'critical')
            if pr:
                pr()
            kwargs['after_reconciliation']()
            return SimpleNamespace(step_result=0)
        def safe(*args, **kwargs):
            self.events.append('archive-eligibility')
            self.assertEqual(self.row()['sources'], '["docs/requirements/change.md"]')
            return False
        def planning(*args):
            self.assertTrue(callable(args[-1].refresh_catalog))
            self.events.append('planning')
            self.assertEqual(self.row()['depends_on'], '["G-002"]')
            return SimpleNamespace(kind=manager.PlanningOutcomeKind.WAITING)
        with contextlib.ExitStack() as stack:
            for name in ('observe_blocked_plan_remotes', 'migrate_legacy_execplans', 'backfill_plan_shas', 'resume_plan_blocks',
                         '_recorded_base_fetch', 'ensure_safe_base_worktree'):
                stack.enter_context(mock.patch.object(manager, name))
            stack.enter_context(mock.patch.object(manager, 'validate_repository', return_value=Path(self.repo)))
            stack.enter_context(mock.patch.object(manager, 'remote_base', side_effect=lambda *a, **k: self.events.append('fetch') or 'main'))
            stack.enter_context(mock.patch.object(manager, 'git', side_effect=lambda *a, **k: SimpleNamespace(stdout=self.sha)))
            stack.enter_context(mock.patch.object(manager, 'recheck_cleanup_pending', return_value=[]))
            stack.enter_context(mock.patch.object(manager, '_execplan_archive_housekeeping_safe', side_effect=safe))
            stack.enter_context(mock.patch.object(manager, 'synchronize_waiting_pull_requests', side_effect=pull_requests))
            stack.enter_context(mock.patch.object(manager.project_catalog_refresh, 'ProjectCatalogRefresh', return_value=self.boundary))
            stack.enter_context(mock.patch.object(manager.PlanningLifecycleCoordinator, 'run', side_effect=planning))
            stack.enter_context(mock.patch.object(manager, 'validate_persisted_agent_scope', side_effect=RuntimeError('unsafe')))
            return manager._run_project_step(self.connection, Path('/state'), self.repo, Path('unused.ini'), 'main', False)

    def test_step_fetch_catalog_pr_archive_and_selection_order(self):
        self.assertEqual(self.run_step(), 0)
        self.assertEqual(self.events, ['fetch', 'catalog', 'pr', 'archive-eligibility', 'planning'])

    def test_planning_resume_refreshes_before_pr_consumers(self):
        self.assertEqual(self.run_step(phase='PLANNING'), 0)
        self.assertLess(self.events.index('catalog'), self.events.index('pr'))

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

    def test_restart_after_committed_pr_result_keeps_merge_and_session(self):
        self.boundary.refresh()
        self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-001'")
        self.connection.commit()
        restarted = refresh.ProjectCatalogRefresh(self.connection, self.repo, lambda: self.sha, self.reconcile)
        restarted.refresh()
        self.assertEqual(self.row()['status'], 'MERGED')
        self.assertEqual(self.row()['worker_session_id'], 'worker')
        self.assertEqual(self.row()['publication_content_hash'], 'old-hash-G-001')

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
        self.assertEqual(fixture.ports.reconcile_execplan_catalog.call_args.kwargs, {'record_invalid': False})
        self.assertEqual(manager.scoped_blocking.project_block(fixture.connection, fixture.repo)['evidence'], 'original project cause')
        events.clear()
        with mock.patch.object(coordinator, '_validate', side_effect=RuntimeError('unsafe')):
            self.assertFalse(coordinator.refresh_catalog('G-106'))
        self.assertEqual(events, [])

    def test_archive_adapter_validates_publication_then_adopts_delta_and_audit(self):
        from loop_manager_internal.catalog import execplan_archive as archive
        self.boundary.refresh()
        port = mock.Mock()
        port.fetch_remote_base.side_effect = lambda base: self.sha
        entry = archive.ArchiveTreeEntry('100644', 'blob', 'object', self.row('G-002')['content_hash'])
        port.observe.side_effect = lambda sha, candidates: tuple(
            archive.ArchiveCandidateObservation(item, entry, None) for item in candidates)
        port.prepare_commit.side_effect = lambda sha, base, operation, moves: archive.PreparedArchive(
            operation, sha, 'tree-hash', 'c'*40)
        port.commit_matches.return_value = True
        def push(prepared, base):
            self.sha = prepared.commit_oid
            return archive.ArchivePushResult(True, self.sha)
        port.push_fast_forward.side_effect = push
        with mock.patch.object(manager, '_ExecPlanArchiveGitPort', return_value=port):
            result = manager.dispatch_execplan_archive_housekeeping(
                self.connection, Path(self.repo), Path('/state'), self.repo, 'main',
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

    def test_new_pr_head_safety_block_prevents_followup_git_and_consumers(self):
        def observe_reopened_pr():
            self.connection.execute(
                "UPDATE exec_plans SET status='PLAN_BLOCKED', ever_started=1, blocked_reason=?, "
                "branch='branch', pr_url='https://example/pr/1', pr_head_oid=? WHERE plan_id='G-001'",
                (manager.PR_CLOSED_BLOCK_REASON, 'd'*40))
            self.connection.commit()
            # Capture the step's mocks before invoking the real PR observer.
            self.followup_fetch = manager._recorded_base_fetch
            self.source_sync = manager.ensure_safe_base_worktree
            manager.observe_waiting_pull_requests(
                self.connection, Path(self.repo), Path('/state'), self.repo)
            block = manager.scoped_blocking.project_block(self.connection, self.repo)
            self.assertEqual(block['reason_code'],
                             manager.BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value)
            self.block_evidence = block['evidence']
        with mock.patch.object(manager, 'pr_for_branch', return_value={
                'state': 'OPEN', 'headRefOid': 'c'*40, 'headRefName': 'branch',
                'baseRefName': 'main', 'url': 'https://example/pr/1'}), \
             mock.patch.object(manager, 'resume_project_block_if_safe', return_value=False), \
             mock.patch.object(manager, 'dispatch_execplan_archive_housekeeping') as archive, \
             mock.patch.object(manager, 'run_agent') as agent:
            self.assertEqual(self.run_step(pr=observe_reopened_pr), 1)
        self.assertEqual(self.events, ['fetch', 'catalog', 'pr'])
        self.followup_fetch.assert_not_called()
        self.source_sync.assert_not_called()
        archive.assert_not_called()
        agent.assert_not_called()
        self.assertEqual(self.row()['status'], 'PLAN_BLOCKED')
        self.assertEqual(self.row()['blocked_reason'], manager.PR_CLOSED_BLOCK_REASON)
        self.assertEqual(self.row()['pr_head_oid'], 'd'*40)
        block = manager.scoped_blocking.project_block(self.connection, self.repo)
        self.assertEqual(block['evidence'], self.block_evidence)

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
            stack.enter_context(mock.patch.object(manager, '_execplan_archive_housekeeping_safe', return_value=False))
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
