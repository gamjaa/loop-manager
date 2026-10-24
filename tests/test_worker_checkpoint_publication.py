import json
import sqlite3
import subprocess
import tempfile
import unittest
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.git import github_observation, publication_service
from loop_manager_internal.storage import database
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.git import git_command, git_test_environment, local_github_origin, make_remote_repository


class RemoteWorkAdoptionDatabaseTests(unittest.TestCase):
    def _database(self, *, fresh=True):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state_dir = Path(directory.name)
        connection = database.connect_database(
            state_dir, create=True, mark_created_from_absence=fresh)
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO projects(repo, base_branch, origin) VALUES ('repo', 'main', 'owner/repo')")
        connection.commit()
        return connection, state_dir

    def _catalog(self, connection, *, repo='repo', ids=('G-001',), revision='a' * 40):
        from tests.support.catalog import active_snapshot, execplan_header, FakeDefinitionPort
        from loop_manager_internal.storage import execplan_catalog_state as catalog
        pinned = active_snapshot(revision, {
            f'docs/plans/[{plan_id}] Resume.md': execplan_header(plan_id, 'Resume')
            for plan_id in ids})
        result = catalog.CatalogReconciliationService(
            catalog.DefinitionStateRepository(connection), FakeDefinitionPort(pinned)).reconcile(repo)
        self.assertTrue(result.usable, repr(result))
        return pinned

    def test_first_catalog_grant_survives_restart_and_is_consumed_by_workspace(self):
        connection, state_dir = self._database()
        self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))
        self._catalog(connection)
        self.assertTrue(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))
        reopened = database.connect_database(state_dir, create=True, mark_created_from_absence=True)
        self.addCleanup(reopened.close)
        self.assertTrue(database.remote_work_adoption_allowed(reopened, 'repo', 'G-001'))
        database.record_execplan_workspace_seen(reopened, 'repo', 'G-001')
        self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))
        self._catalog(connection)
        self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))

    def test_later_plan_project_and_lost_row_do_not_inherit_grants(self):
        connection, _ = self._database()
        self._catalog(connection)
        connection.execute("UPDATE exec_plans SET worker_session_id='preserved', verify_result='evidence'")
        connection.commit()
        self._catalog(connection, ids=('G-001', 'G-002'))
        self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-002'))
        self.assertEqual(tuple(connection.execute(
            "SELECT worker_session_id, verify_result FROM exec_plans WHERE plan_id='G-001'").fetchone()),
            ('preserved', 'evidence'))
        connection.execute("INSERT INTO projects(repo, base_branch, origin) VALUES ('later', 'main', 'owner/later')")
        connection.commit()
        self._catalog(connection, repo='later')
        self.assertFalse(database.remote_work_adoption_allowed(connection, 'later', 'G-001'))
        connection.execute("DELETE FROM exec_plans WHERE repo='repo' AND plan_id='G-001'")
        connection.commit()
        self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))
        self._catalog(connection, ids=('G-001', 'G-002'))
        self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))

    def test_existing_and_legacy_absence_marker_do_not_authorize_adoption(self):
        for legacy_marker in (False, True):
            with self.subTest(legacy_marker=legacy_marker):
                connection, state_dir = self._database(fresh=False)
                if legacy_marker:
                    connection.execute("INSERT INTO manager_metadata VALUES ('created_from_absence', '1')")
                    connection.commit()
                reinitialized = database.connect_database(state_dir, create=True, mark_created_from_absence=True)
                reinitialized.close()
                self._catalog(connection)
                self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))

    def test_changed_registration_or_declaration_does_not_match_grant(self):
        connection, _ = self._database()
        self._catalog(connection)
        for table, field, value in (('projects', 'origin', 'other/repo'),
                                    ('projects', 'base_branch', 'other'),
                                    ('exec_plans', 'content_hash', 'changed')):
            with self.subTest(field=field):
                connection.execute('SAVEPOINT changed_identity')
                connection.execute(f'UPDATE {table} SET {field}=?', (value,))
                self.assertFalse(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))
                connection.execute('ROLLBACK TO changed_identity')
                connection.execute('RELEASE changed_identity')
        self.assertTrue(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))

    def test_stale_catalog_does_not_consume_bootstrap_scope(self):
        from loop_manager_internal.storage import execplan_catalog_state as catalog
        from tests.support.catalog import active_snapshot, execplan_header
        connection, _ = self._database()
        repository = catalog.DefinitionStateRepository(connection)
        pinned = active_snapshot('a' * 40, {'docs/plans/[G-001] Resume.md': execplan_header('G-001', 'Resume')})
        changed = repository.apply('repo', pinned, repository.capture('repo'),
                                   revision_is_current=lambda: False, new_plan_ids=('G-001',))
        self.assertIsNone(changed)
        self.assertEqual(connection.execute("SELECT value FROM manager_metadata WHERE key='remote_adoption_bootstrap'").fetchone()[0], 'null')
        self._catalog(connection)
        self.assertTrue(database.remote_work_adoption_allowed(connection, 'repo', 'G-001'))

    def test_fresh_registration_defers_active_pr_observation_to_recovery_catalog(self):
        from loop_manager_internal.runtime import registration_service
        from tests.support.catalog import active_snapshot, execplan_header
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                connection, state_dir = self._database(fresh=fresh)
                root = state_dir / 'project'
                root.mkdir()
                pinned = active_snapshot('a' * 40, {
                    'docs/plans/[G-001] Resume.md': execplan_header('G-001', 'Resume')})
                migrate = mock.Mock()
                effects = registration_service.AddRegisteredProjectEffects(
                    _ensure_execplans_schema=database._ensure_execplans_schema,
                    _merged_execplan_ids=lambda *_: frozenset(),
                    git=mock.Mock(return_value=SimpleNamespace(stdout=pinned.revision_sha)),
                    inspect_project_registration=mock.Mock(return_value=SimpleNamespace(
                        root=root, base_branch='main', provider='codex', agents_hash=None,
                        agents_approved=False, origin='owner/repo', source_revision=pinned.revision_sha)),
                    migrate_legacy_execplans=migrate,
                    revision_execplan_snapshot=mock.Mock(return_value=pinned.snapshot))
                registration_service.add_registered_project(effects, connection, root,
                    config_path=state_dir / 'config.ini', provider='codex',
                    requested_base='main', approve_agents_mismatch=False)
                if fresh:
                    migrate.assert_not_called()
                    self._catalog(connection, repo=str(root))
                    self.assertTrue(database.remote_work_adoption_allowed(connection, str(root), 'G-001'))
                else:
                    self.assertEqual(migrate.call_count, 2)

    def test_corrupt_database_is_preserved_by_explicit_init(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            path = state_dir / 'loop-manager.db'
            path.write_bytes(b'corrupt database')
            with self.assertRaises(sqlite3.DatabaseError):
                database.connect_database(state_dir, create=True, mark_created_from_absence=True)
            self.assertEqual(path.read_bytes(), b'corrupt database')


class PullRequestHistoryTests(unittest.TestCase):
    def test_checkpoint_database_timeout_pauses_without_recovery_budget(self):
        import loop_manager
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
        from loop_manager_internal.storage.db_access import DatabaseWaitTimeout
        from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
        from tests.support.execution import execution_fixture

        fixture = execution_fixture(self)
        ports = loop_manager._ExecutionLifecyclePorts(
            fixture.connection, fixture.repository,
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex',
            config_path=Path('/config'), newly_activated=False)
        context = SimpleNamespace(resolving_rebase=False)
        before = dict(ports.effects.state.row())
        for error in (DatabaseWaitTimeout('budget'), sqlite3.OperationalError('SQL error'),
                      SelfMergeRuntimeHandoff()):
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(ports.effects.state, 'active_execplan', side_effect=error):
                if isinstance(error, DatabaseWaitTimeout):
                    outcome = ports.effects.publication.publish_worker_checkpoints(context)
                    self.assertEqual(outcome.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
                else:
                    with self.assertRaises(type(error)):
                        ports.effects.publication.publish_worker_checkpoints(context)
                self.assertEqual(dict(ports.effects.state.row()), before)
    def _pr(self, **changes):
        return {'url': 'https://github.com/owner/repo/pull/1', 'state': 'OPEN',
                'mergedAt': None, 'headRefOid': 'a' * 40,
                'headRefName': 'loop-manager/G-047', 'baseRefName': 'main',
                'mergeable': 'MERGEABLE', 'isCrossRepository': False, **changes}

    def _lookup(self, payload):
        effects = github_observation.PrForBranchEffects(command=mock.Mock(return_value=
            subprocess.CompletedProcess([], 0, json.dumps(payload), '')))
        return github_observation.pr_for_branch(
            effects, Path('/repo'), 'loop-manager/G-047', history=True,
            expected_repository='owner/repo', expected_base='main')

    def test_production_ports_forward_history_and_preserve_default_lookup(self):
        import loop_manager
        from tests.support.execution import execution_fixture

        fixture = execution_fixture(self)
        fixture.connection.execute("UPDATE projects SET origin='owner/repo'")
        fixture.connection.commit()
        repo_path = Path('/repo')
        branch = 'loop-manager/G-047'
        pull_request = self._pr()
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value=pull_request) as lookup:
            ports = loop_manager._ExecutionLifecyclePorts(
                fixture.connection, fixture.repository,
                repo='repo', repo_path=repo_path, state_dir=Path('/state'),
                base='main', base_sha='base', provider='codex',
                config_path=Path('/config'), newly_activated=False,
            )
            for url in (None, pull_request['url']):
                for history in (False, True):
                    with self.subTest(url=url, history=history):
                        args = (repo_path, branch, url) if url else (repo_path, branch)
                        options = ({'history': True, 'expected_repository': 'owner/repo',
                                    'expected_base': 'main'} if history else {})
                        result = ports.effects.publication.pr_for_branch(*args, history=history)
                        self.assertEqual(result, pull_request)
                        lookup.assert_called_once_with(*args, **options)
                        lookup.reset_mock()
            fixture.connection.execute("UPDATE projects SET base_branch='other'")
            with self.assertRaisesRegex(RuntimeError, '등록 repository/base'):
                ports.effects.publication.pr_for_branch(repo_path, branch, history=True)
            lookup.assert_not_called()

    def test_merged_history_wins_only_after_all_identities_match(self):
        merged = self._pr(url='https://github.com/owner/repo/pull/2', state='MERGED',
                          mergedAt='2026-01-01T00:00:00Z', headRefOid='b' * 40,
                          mergeCommit={'oid': 'c' * 40})
        self.assertEqual(self._lookup([self._pr(), merged]), merged)
        # No ancestry or live branch observation is needed for a real Merge.
        for changes in ({'baseRefName': 'develop'}, {'headRefName': 'other'},
                        {'url': 'https://github.com/other/repo/pull/2'},
                        {'isCrossRepository': True}, {'isCrossRepository': None},
                        {'headRefOid': 'short'}, {'headRefOid': None}):
            for invalid_index in (0, 1):
                with self.subTest(changes=changes, invalid_index=invalid_index):
                    records = [self._pr(), merged]
                    records[invalid_index] = {**records[invalid_index], **changes}
                    with self.assertRaisesRegex(RuntimeError, 'identity'):
                        self._lookup(records)

    def _prepare_fixture(self):
        from dataclasses import replace
        import loop_manager
        from tests.support.execution import execution_fixture

        fixture = execution_fixture(self)
        fixture.connection.executescript(
            "CREATE TABLE manager_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO manager_metadata VALUES ('remote_adoption_bootstrap', 'null');"
            "UPDATE projects SET origin='owner/repo';"
            "UPDATE exec_plans SET workspace_owned=0;")
        database.record_remote_work_adoption_catalog(fixture.connection, 'repo', 'a' * 40, ('G-047',))
        fixture.connection.commit()
        fixture.state.expected = fixture.repository.projected('repo')
        ports = loop_manager._ExecutionLifecyclePorts(
            fixture.connection, fixture.repository,
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex',
            config_path=Path('/config'), newly_activated=True)
        fixture.handler.effects = replace(fixture.effects,
            checkpoint=ExecutionCheckpointRepository(fixture.connection),
            publication=replace(fixture.effects.publication,
                pr_for_branch=ports.effects.publication.pr_for_branch,
                reconcile_existing_pull_request=ports.effects.publication.reconcile_existing_pull_request,
                github_repository=loop_manager.github_repository))
        fixture.effects.workspace._plan_id_from_path.return_value = 'G-047'
        fixture.effects.workspace.git.side_effect = lambda *_a, **_k: SimpleNamespace(
            stdout='https://github.com/owner/repo.git', returncode=0)
        return fixture

    def test_prepare_rejects_mismatched_merged_history_before_terminal_or_workspace_effects(self):
        import loop_manager
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
        fixture = self._prepare_fixture()
        for changes in ({'baseRefName': 'other'}, {'headRefName': 'other'},
                        {'url': 'https://github.com/other/repo/pull/1'}):
            with self.subTest(changes=changes), \
                    mock.patch.object(fixture.handler, '_resolving_rebase', return_value=False), \
                    mock.patch.object(fixture.handler, '_fetch_latest_base', return_value='b' * 40), \
                    mock.patch.object(fixture.handler, '_checkpoint_remote_oid', return_value=None), \
                    mock.patch.object(loop_manager, 'command', return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps([self._pr(state='MERGED', **changes)]), '')) as command:
                result = fixture.handler.prepare()
                self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                command.assert_called_once()
                fixture.effects.workspace.make_worktree.assert_not_called()
                fixture.effects.agent.run_agent.assert_not_called()
                self.assertEqual(fixture.state.active_execplan('repo')['status'], 'IMPLEMENTING')

    def test_prepare_merged_uses_real_terminal_binding_before_fetch(self):
        import loop_manager
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
        for failed_fetch in (False, True):
            with self.subTest(failed_fetch=failed_fetch):
                fixture = self._prepare_fixture()
                fixture.connection.execute('UPDATE projects SET planning_attempt_fingerprint=NULL')
                fixture.connection.commit()
                pr = self._pr(state='MERGED', headRefOid='b' * 40,
                              mergeCommit={'oid': 'c' * 40})
                with mock.patch.object(fixture.handler, '_resolving_rebase', return_value=False), \
                     mock.patch.object(fixture.handler, '_fetch_latest_base') as fetch, \
                     mock.patch.object(fixture.handler, '_checkpoint_remote_oid') as branch, \
                     mock.patch.object(loop_manager, 'command', return_value=subprocess.CompletedProcess(
                         [], 0, json.dumps([pr]), '')), \
                     mock.patch.object(loop_manager, '_assess_missing_managed_cleanup',
                                       return_value=loop_manager.CleanupAssessment('missing')), \
                     mock.patch.object(loop_manager, 'remote_base',
                                       side_effect=RuntimeError('fetch unavailable') if failed_fetch else None):
                    result = fixture.handler.prepare()
                self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY if failed_fetch
                                 else ExecutionOutcomeKind.PLANNING_PENDING)
                fetch.assert_not_called()
                branch.assert_not_called()
                fixture.effects.workspace.make_worktree.assert_not_called()
                fixture.effects.agent.run_agent.assert_not_called()
                saved = fixture.connection.execute('SELECT * FROM exec_plans').fetchone()
                self.assertEqual((saved['status'], saved['pr_url'], saved['pr_head_oid']),
                                 ('MERGED', pr['url'], 'b' * 40))

    def test_prepare_preserves_competing_owner_during_terminal_observation(self):
        import loop_manager
        from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
        fixture = self._prepare_fixture()
        def competing(*_a, **_k):
            fixture.connection.execute('UPDATE exec_plans SET workspace_owned=1')
            fixture.connection.commit()
            return subprocess.CompletedProcess([], 0, json.dumps([self._pr(state='MERGED')]), '')
        with mock.patch.object(fixture.handler, '_resolving_rebase', return_value=False), \
             mock.patch.object(loop_manager, 'command', side_effect=competing), \
             mock.patch.object(loop_manager, 'complete_merged_execplan_worktree') as complete:
            result = fixture.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        complete.assert_not_called()
        fixture.effects.workspace.make_worktree.assert_not_called()
        saved = fixture.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual((saved['status'], saved['workspace_owned']), ('IMPLEMENTING', 1))

    def test_absent_open_closed_and_incomplete_history(self):
        self.assertIsNone(self._lookup([]))
        self.assertEqual(self._lookup([self._pr()]), self._pr())
        closed = self._pr(state='CLOSED')
        self.assertEqual(self._lookup([closed]), closed)
        with self.assertRaisesRegex(RuntimeError, '목록 응답'):
            self._lookup([{}] * 1000)
        with self.assertRaisesRegex(RuntimeError, '열린 PR이 여러 개'):
            self._lookup([self._pr(), self._pr(url='https://github.com/owner/repo/pull/2')])
        with self.assertRaisesRegex(RuntimeError, '필드'):
            self._lookup([self._pr(state='UNKNOWN')])
        with self.assertRaisesRegex(RuntimeError, '등록 repository'):
            github_observation.pr_for_branch(
                github_observation.PrForBranchEffects(command=mock.Mock()),
                Path('/repo'), 'loop-manager/G-047', history=True)


class WorkerCheckpointPublicationTests(unittest.TestCase):
    def test_accepted_rebase_and_later_commit_wait_for_final_publication(self):
        from loop_manager_internal.git.managed_rebase_validation import Remote
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
        from tests.support.rebase_observations import decision_for, expectation, observation

        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            remote = git_command(repo, 'rev-parse', 'HEAD')
            git_command(repo, 'push', 'origin', f'HEAD:refs/heads/{branch}')
            git_command(repo, 'switch', 'main')
            (repo / 'base.txt').write_text('new base\n', encoding='utf-8')
            git_command(repo, 'add', 'base.txt')
            git_command(repo, 'commit', '-m', 'Advance base')
            latest = git_command(repo, 'rev-parse', 'HEAD')
            git_command(repo, 'switch', branch)
            git_command(repo, 'rebase', 'main')
            rebased = git_command(repo, 'rev-parse', 'HEAD')
            path = 'docs/plans/[G-001] Resume.md'
            expected = expectation(repo=str(repo), repository_identity=str(repo / '.git'),
                plan_id='G-001', plan_path=path, worktree=str(repo), branch=branch,
                target_base_oid=latest, remote=Remote(str(repo), branch, remote))
            acceptance = decision_for(expected, observation(expected,
                head=rebased, branch_ref=(rebased, ''),
                registration=(f'HEAD={rebased}', f'branch=refs/heads/{branch}'))).acceptance
            connection.execute('UPDATE exec_plans SET active_sha=?, publication_remote_oid=?',
                               (latest, remote))
            connection.commit()
            checkpoint = ExecutionCheckpointRepository(connection)
            checkpoint.record(str(repo), 'G-001', expected_generation=0, operation=None,
                expected_head=rebased, target_base_oid=latest, observed_remote_oid=remote,
                snapshot={'accepted_work_branch': remote, 'current_rebase_acceptance': acceptance})
            connection.commit()
            effects = self._effects(repo, origin, connection)
            for followup in (False, True):
                if followup:
                    (repo / 'src/app.py').write_text('VALUE = 99\n', encoding='utf-8')
                    git_command(repo, 'add', 'src/app.py')
                    git_command(repo, 'commit', '-m', 'Follow up after rebase')
                head = git_command(repo, 'rev-parse', 'HEAD')
                with self.subTest(followup=followup):
                    self.assertEqual(publication_service.push_worker_checkpoint_commits(
                        effects, connection, repo, Path(directory), str(repo), 'G-001',
                        branch, repo, 'main', remote, None), (head, 0))
                    self.assertEqual(git_command(repo.parent / 'remote.git', 'rev-parse',
                                                f'refs/heads/{branch}'), remote)
                    self.assertEqual(checkpoint.get(str(repo), 'G-001').expected_head, rebased)
            connection.execute('UPDATE exec_plans SET active_sha=?', (base,))
            connection.commit()
            with self.assertRaises(RecoveryFailure):
                publication_service.push_worker_checkpoint_commits(effects, connection,
                    repo, Path(directory), str(repo), 'G-001', branch, repo, 'main', remote, None)

    def test_confirmed_push_and_later_remote_input_are_separate_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            target = git_command(repo, 'rev-parse', 'HEAD~1')
            successor = git_command(repo, 'rev-parse', 'HEAD')
            git_command(repo, 'push', 'origin', f'{successor}:refs/heads/{branch}')
            checkpoints = ExecutionCheckpointRepository(connection)
            checkpoints.record(str(repo), 'G-001', expected_generation=0, operation=None,
                expected_head=target, target_base_oid=base, observed_remote_oid=None,
                snapshot={'worker_checkpoint_pending': {'branch': branch, 'from': None, 'target': target}})
            effects = self._effects(repo, origin, connection)
            observed = mock.Mock(wraps=effects.git)
            effects = publication_service.WorkerCheckpointPublicationEffects(**{**effects.__dict__, 'git': observed})
            _, count = publication_service.push_worker_checkpoint_commits(effects, connection,
                repo, Path(directory), str(repo), 'G-001', branch, repo, 'main', successor, None)
            self.assertEqual(count, 0)
            self.assertFalse(any(call.args[1] == 'push' for call in observed.call_args_list))
            self.assertEqual(git_command(repo.parent / 'remote.git', 'rev-parse', f'refs/heads/{branch}'), successor)
            # Only our target is confirmed. The caller still has to accept the
            # successor as a new remote input before continuing implementation.
            self.assertEqual(connection.execute('SELECT publication_remote_oid FROM exec_plans').fetchone()[0], target)
            saved = checkpoints.get(str(repo), 'G-001').snapshot
            self.assertEqual(saved['accepted_work_branch'], target)
            self.assertNotIn('worker_checkpoint_pending', saved)

    def _effects(self, repo, origin, connection, *, git=None, update=None):
        return publication_service.WorkerCheckpointPublicationEffects(
            active_execplan=lambda _connection, selected_repo: connection.execute(
                'SELECT * FROM exec_plans WHERE repo=?', (selected_repo,)).fetchone(),
            git=git or self._git,
            remote_branch_oid=lambda path, branch: publication_service.remote_branch_oid(
                publication_service.RemoteBranchOidEffects(git=self._git), path, branch),
            pr_for_branch=lambda *_: None, github_repository=lambda _: origin,
            validate_execplan_workspace_identity=mock.Mock(), validate_managed_worktree=mock.Mock(),
            update_project=update or (lambda _connection, selected_repo, **values: connection.execute(
                'UPDATE exec_plans SET ' + ', '.join(f'{key}=?' for key in values) + ' WHERE repo=?',
                (*values.values(), selected_repo))),
        )

    def test_intermediate_forbidden_document_is_never_pushed_after_its_revert(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            safe = git_command(repo, 'rev-parse', 'HEAD')
            target = repo / 'docs/requirements/Active.md'
            original = target.read_bytes()
            target.write_text('# [R-001] Forbidden temporary change\n', encoding='utf-8')
            git_command(repo, 'add', str(target))
            git_command(repo, 'commit', '-m', 'Forbidden intermediate requirement')
            forbidden = git_command(repo, 'rev-parse', 'HEAD')
            target.write_bytes(original)
            git_command(repo, 'add', str(target))
            git_command(repo, 'commit', '-m', 'Restore final requirement')
            final = git_command(repo, 'rev-parse', 'HEAD')
            effects = self._effects(repo, origin, connection)
            with self.assertRaisesRegex(RuntimeError, '금지 문서'):
                publication_service.push_worker_checkpoint_commits(effects, connection, repo, Path(directory),
                    str(repo), 'G-001', branch, repo, 'main', None, None)
            self.assertEqual(git_command(repo.parent / 'remote.git', 'rev-parse', f'refs/heads/{branch}'), safe)
            self.assertEqual(git_command(repo, 'rev-parse', 'HEAD'), final)
            self.assertNotEqual(safe, forbidden)
            self.assertEqual(ExecutionCheckpointRepository(connection).get(str(repo), 'G-001')
                             .snapshot['accepted_work_branch'], safe)
            # The restart entrance must apply the same immutable policy.
            with self.assertRaisesRegex(RuntimeError, '금지 문서'):
                publication_service.push_worker_checkpoint_commits(effects, connection, repo, Path(directory),
                    str(repo), 'G-001', branch, repo, 'main', safe, None)

    def test_publication_completion_rolls_back_both_rows_and_does_not_repeat_push(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            first = git_command(repo, 'rev-parse', 'HEAD~1')
            real = self._effects(repo, origin, connection)
            pushes = []
            def git(path, *args, **kwargs):
                self.assertFalse(connection.in_transaction, args)
                if args[0] == 'push':
                    pushes.append(args[-1].split(':')[0])
                return self._git(path, *args, **kwargs)
            def interrupted(*args, **kwargs):
                real.update_project(*args, **kwargs)
                raise sqlite3.OperationalError('completion interrupted')
            effects = publication_service.WorkerCheckpointPublicationEffects(
                **{**real.__dict__, 'git': git, 'update_project': interrupted})
            with self.assertRaisesRegex(sqlite3.OperationalError, 'completion interrupted'):
                publication_service.push_worker_checkpoint_commits(effects, connection, repo, Path(directory),
                    str(repo), 'G-001', branch, repo, 'main', None, None)
            saved = ExecutionCheckpointRepository(connection).get(str(repo), 'G-001')
            self.assertEqual(saved.snapshot['worker_checkpoint_pending']['target'], first)
            self.assertNotIn('accepted_work_branch', saved.snapshot)
            self.assertIsNone(connection.execute('SELECT publication_remote_oid FROM exec_plans').fetchone()[0])
            effects = publication_service.WorkerCheckpointPublicationEffects(**{**real.__dict__, 'git': git})
            head, count = publication_service.push_worker_checkpoint_commits(effects, connection, repo, Path(directory),
                str(repo), 'G-001', branch, repo, 'main', first, None)
            self.assertEqual(pushes, [first, head])
            self.assertEqual(count, 1)
            self.assertEqual(connection.execute('SELECT publication_remote_oid FROM exec_plans').fetchone()[0], head)

    def test_manager_request_survives_commit_completion_interruption_without_new_commit(self):
        import loop_manager as manager
        from loop_manager_internal.execution import worker_checkpoint
        from loop_manager_internal.execution.execution_publication_handler import PublicationHandler
        from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionPhase, ExecutionPlanMetadata
        from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot
        from loop_manager_internal.execution.execution_state import ExecutionStatePort
        from loop_manager_internal.execution.worker_outcome import WorkerOutcome
        from loop_manager_internal.storage import runtime_state
        from loop_manager_internal.notifications import notifications

        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            notifications.ensure_schema(connection)
            connection.commit()
            state = ExecutionStatePort(connection, runtime_state.RuntimeStateRepository(connection), str(repo))
            state.update_project(str(repo), worker_session_id='same-session',
                                 verify_result='existing verify failure', last_error='existing diagnostic')
            identity = ExecutionIdentity(repo=str(repo), repo_path=repo, state_dir=Path(directory), base='main')
            checkpoint = ExecutionCheckpointRepository(connection)
            context = ExecutionContext(str(repo), repo, Path(directory), 'main', base, 'codex', repo / 'config.ini',
                ExecutionPlanMetadata('G-001', 'Resume', 'docs/plans/[G-001] Resume.md', base),
                ExecutionPhase.IMPLEMENTING, branch, repo, base, None, worker_session_id='same-session')
            ports = SimpleNamespace(identity=identity, checkpoint=checkpoint, state=state, git=self._git,
                capture_git_snapshot=manager.capture_git_snapshot, snapshot=lambda: PreparationSnapshot(),
                _published_plan_allows_common_plans=lambda *_: False, _row=state.row, feedback=None,
                validate_execplan_workspace_identity=mock.Mock(), validate_managed_worktree=mock.Mock(),
                _retry=lambda detail: detail, _fetch_latest_base=lambda: base)
            handler = PublicationHandler(ports)
            ports._commit_owned = handler._commit_owned
            publication = self._effects(repo, origin, connection,
                update=lambda _c, r, **values: state.update_project(r, preserve_transaction=True, **values))
            def publish(ctx, **kwargs):
                remote = publication.remote_branch_oid(repo, branch)
                _, count = publication_service.push_worker_checkpoint_commits(publication, connection,
                    repo, Path(directory), str(repo), 'G-001', branch, repo, 'main', remote, None)
                return {'pushed': count, 'candidate_changed': False}
            ports.publish_worker_checkpoints = publish
            before = manager.capture_git_snapshot(repo)
            (repo / 'src/app.py').write_text('VALUE = 123\n', encoding='utf-8')
            request = WorkerOutcome('checkpoint', plan_id='G-001', start_head=before.head,
                                    commit_title='Preserve the new implementation')
            original_record = checkpoint.record
            def interrupt_completion(*args, **kwargs):
                if kwargs['operation'] is None and kwargs['snapshot'].get('after_head'):
                    raise sqlite3.OperationalError('commit completion interrupted')
                return original_record(*args, **kwargs)
            with mock.patch.object(checkpoint, 'record', side_effect=interrupt_completion):
                with self.assertRaisesRegex(sqlite3.OperationalError, 'commit completion interrupted'):
                    worker_checkpoint.save_request(ports, context, request, 'checkpoint only', before,
                                                   session='same-session')
            head = git_command(repo, 'rev-parse', 'HEAD')
            self.assertEqual(git_command(repo, 'show', '-s', '--format=%P', head), before.head)
            worker_checkpoint.resume_request(ports, context)
            self.assertEqual(git_command(repo, 'rev-parse', 'HEAD'), head)
            self.assertEqual(git_command(repo.parent / 'remote.git', 'rev-parse', f'refs/heads/{branch}'), head)
            self.assertIsNone(worker_checkpoint.resume_request(ports, context))
            row = state.row()
            self.assertEqual(row['worker_session_id'], 'same-session')
            self.assertEqual(row['verify_result'], 'existing verify failure')
            self.assertEqual(row['last_error'], 'existing diagnostic')
            for field in ('worker_result', 'worker_input_token', 'verify_token', 'review_token', 'publication_token'):
                self.assertIsNone(row[field])

    def _repository(self, directory):
        repo = make_remote_repository(directory)
        origin = local_github_origin(self, repo)
        branch = 'loop-manager/G-001'
        git_command(repo, 'switch', '-c', branch)
        git_command(repo, 'config', 'user.email', 'worker@example.com')
        git_command(repo, 'config', 'user.name', 'Worker')
        base = git_command(repo, 'rev-parse', 'HEAD')
        for index in range(2):
            (repo / 'src/app.py').write_text(f'VALUE = {index + 2}\n', encoding='utf-8')
            git_command(repo, 'add', 'src/app.py')
            git_command(repo, 'commit', '-m', f'Checkpoint {index + 1}')

        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.execute(database.SCHEMA)
        database._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects(repo, base_branch, origin, active_sha, phase, branch, worktree, plan_path) "
            "VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)",
            (str(repo), origin, base, branch, str(repo), 'docs/plans/[G-001] Resume.md'),
        )
        connection.execute(
            "INSERT INTO exec_plans(repo, plan_id, path, title, sources, depends_on, priority, content_hash, "
            "status, plan_sha, active_sha, branch, worktree, workspace_owned) "
            "VALUES (?, 'G-001', 'docs/plans/[G-001] Resume.md', 'Resume', '[]', '[]', 'normal', 'hash', "
            "'IMPLEMENTING', ?, ?, ?, ?, 1)",
            (str(repo), base, base, branch, str(repo)),
        )
        connection.commit()
        return repo, origin, branch, base, connection

    @staticmethod
    def _git(path, *arguments, check=True):
        result = subprocess.run(
            ['git', '-C', str(path), *arguments], capture_output=True, text=True,
            encoding='utf-8', errors='replace', env=git_test_environment(), check=False,
        )
        if check and result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        return result

    def test_pushes_each_linear_worker_commit_with_successive_exact_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            state_dir = Path(directory) / 'state'
            state_dir.mkdir()
            effects = publication_service.WorkerCheckpointPublicationEffects(
                active_execplan=lambda _connection, selected_repo: connection.execute(
                    'SELECT * FROM exec_plans WHERE repo=?', (selected_repo,)).fetchone(),
                git=self._git,
                remote_branch_oid=lambda *_: None,
                pr_for_branch=lambda *_: None,
                github_repository=lambda _url: origin,
                validate_execplan_workspace_identity=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
                update_project=lambda _connection, selected_repo, **values: connection.execute(
                    'UPDATE exec_plans SET ' + ', '.join(f'{key}=?' for key in values) + ' WHERE repo=?',
                    (*values.values(), selected_repo),
                ),
            )
            pushes = []
            original_git = effects.git

            def record_push(path, *arguments, **options):
                if arguments and arguments[0] == 'push':
                    pushes.append(arguments)
                return original_git(path, *arguments, **options)

            effects = publication_service.WorkerCheckpointPublicationEffects(
                **{**effects.__dict__, 'git': record_push},
            )

            head, pushed = publication_service.push_worker_checkpoint_commits(
                effects, connection, repo, state_dir, str(repo), 'G-001', branch,
                repo, 'main', None, None,
            )

            self.assertEqual(pushed, 2)
            self.assertEqual(git_command(repo.parent / 'remote.git', 'rev-parse', f'refs/heads/{branch}'), head)
            self.assertEqual(len(pushes), 2)
            self.assertEqual(pushes[0][1], f'--force-with-lease=refs/heads/{branch}:')
            self.assertEqual(pushes[1][1], f'--force-with-lease=refs/heads/{branch}:{pushes[0][3].split(":")[0]}')
            self.assertEqual(connection.execute(
                'SELECT publication_remote_oid FROM exec_plans WHERE repo=?', (str(repo),)
            ).fetchone()[0], head)
            checkpoint = ExecutionCheckpointRepository(connection).get(str(repo), 'G-001')
            self.assertEqual(checkpoint.snapshot['accepted_work_branch'], head)
            self.assertNotIn('worker_checkpoint_pending', checkpoint.snapshot)

    def test_a_push_confirmed_before_sqlite_interruption_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            state_dir = Path(directory) / 'state'
            state_dir.mkdir()
            first = git_command(repo, 'rev-parse', 'HEAD~1')
            git_command(repo, 'push', 'origin', f'{first}:refs/heads/{branch}')
            checkpoint = ExecutionCheckpointRepository(connection)
            checkpoint.record(
                str(repo), 'G-001', expected_generation=0, operation=None,
                expected_head=first, target_base_oid=base, observed_remote_oid=base,
                snapshot={'worker_checkpoint_pending': {
                    'branch': branch, 'from': base, 'target': first,
                }},
            )
            effects = publication_service.WorkerCheckpointPublicationEffects(
                active_execplan=lambda _connection, selected_repo: connection.execute(
                    'SELECT * FROM exec_plans WHERE repo=?', (selected_repo,)).fetchone(),
                git=self._git,
                remote_branch_oid=lambda *_: first,
                pr_for_branch=lambda *_: None,
                github_repository=lambda _url: origin,
                validate_execplan_workspace_identity=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
                update_project=lambda _connection, selected_repo, **values: connection.execute(
                    'UPDATE exec_plans SET ' + ', '.join(f'{key}=?' for key in values) + ' WHERE repo=?',
                    (*values.values(), selected_repo),
                ),
            )

            head, pushed = publication_service.push_worker_checkpoint_commits(
                effects, connection, repo, state_dir, str(repo), 'G-001', branch,
                repo, 'main', first, None,
            )

            self.assertEqual(pushed, 1)
            self.assertEqual(git_command(repo.parent / 'remote.git', 'rev-parse', f'refs/heads/{branch}'), head)
            self.assertNotIn('worker_checkpoint_pending', checkpoint.get(str(repo), 'G-001').snapshot)

    def test_remote_advance_is_never_overwritten_by_a_stale_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, origin, branch, _base, connection = self._repository(directory)
            self.addCleanup(connection.close)
            state_dir = Path(directory) / 'state'
            state_dir.mkdir()
            upstream = Path(directory) / 'upstream'
            subprocess.run(['git', 'clone', str(repo.parent / 'remote.git'), str(upstream)], check=True,
                           capture_output=True, env=git_test_environment())
            git_command(upstream, 'switch', '-c', branch)
            git_command(upstream, 'config', 'user.email', 'other@example.com')
            git_command(upstream, 'config', 'user.name', 'Other')
            (upstream / 'src/app.py').write_text('VALUE = 99\n', encoding='utf-8')
            git_command(upstream, 'add', 'src/app.py')
            git_command(upstream, 'commit', '-m', 'Concurrent remote change')
            git_command(upstream, 'push', 'origin', f'HEAD:refs/heads/{branch}')
            remote_head = git_command(upstream, 'rev-parse', 'HEAD')
            local_head = git_command(repo, 'rev-parse', 'HEAD')
            effects = publication_service.WorkerCheckpointPublicationEffects(
                active_execplan=lambda _connection, selected_repo: connection.execute(
                    'SELECT * FROM exec_plans WHERE repo=?', (selected_repo,)).fetchone(),
                git=self._git,
                remote_branch_oid=lambda *_: None,
                pr_for_branch=lambda *_: None,
                github_repository=lambda _url: origin,
                validate_execplan_workspace_identity=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
                update_project=mock.Mock(),
            )
            with self.assertRaisesRegex(RuntimeError, '게시 전 원격 branch가 전진'):
                publication_service.push_worker_checkpoint_commits(
                    effects, connection, repo, state_dir, str(repo), 'G-001', branch,
                    repo, 'main', None, None,
                )
            self.assertEqual(git_command(upstream, 'rev-parse', 'HEAD'), remote_head)
            self.assertEqual(git_command(repo, 'rev-parse', 'HEAD'), local_head)


class WorkerCheckpointPolicyTests(unittest.TestCase):
    def test_database_requests_retry_or_preserve_the_same_publication(self):
        from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
        from loop_manager_internal.storage import db_access, runtime_state
        from loop_manager_internal.execution.execution_state import ExecutionStatePort
        from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRace
        from tests.support.database import runtime_connection

        for phase in ('intent', 'completion'):
            for mode in ('retry', 'generation', 'owner', 'timeout', 'cancel', 'sql_error'):
                with self.subTest(phase=phase, mode=mode), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'state.db'
                    source = runtime_connection()
                    base, head, tree = 'a' * 40, 'b' * 40, 'c' * 40
                    branch = 'loop-manager/G-047'
                    source.execute("UPDATE projects SET origin='owner/repo', "
                                   "plan_path='docs/plans/[G-047] Runtime.md'")
                    source.execute("UPDATE exec_plans SET status='IMPLEMENTING', active_sha=?, "
                                   "branch=?, worktree='/managed', worker_session_id='same-session', "
                                   "publication_remote_oid=?, pr_head_oid=?, pr_url='existing-pr'",
                                   (base, branch, base, base))
                    source.commit()
                    checkpoints = ExecutionCheckpointRepository(source)
                    request = {'session': 'same-session'}
                    initial = checkpoints.record('repo', 'G-047', expected_generation=0,
                        operation=None, expected_head=base, target_base_oid=base,
                        observed_remote_oid=base, snapshot={'worker_checkpoint_request': request,
                                                           'accepted_work_branch': base})
                    with sqlite3.connect(path) as destination:
                        source.backup(destination)
                    source.close()
                    connection = db_access.open_connection(path, timeout=.08)
                    connection.row_factory = sqlite3.Row
                    reader = sqlite3.connect(path)
                    self.addCleanup(connection.close)
                    self.addCleanup(reader.close)
                    checkpoints = ExecutionCheckpointRepository(connection)
                    state = ExecutionStatePort(connection, runtime_state.RuntimeStateRepository(connection), 'repo')
                    live, pushes, attempts = base, [], []
                    def git(_path, *args, **kwargs):
                        nonlocal live
                        self.assertFalse(connection.in_transaction, args)
                        if args[0] == 'remote': value = 'https://github.com/owner/repo.git'
                        elif args[0] == 'symbolic-ref': value = branch
                        elif args[0] == 'rev-parse': value = tree if args[-1].endswith('^{tree}') else head
                        elif args[0] == 'ls-remote': value = f'{live}\trefs/heads/{branch}' if live else ''
                        elif args[0] == 'rev-list': value = f'{head} {base}'
                        elif args[0] == 'show': value = base
                        elif args[0] == 'diff': value = 'app.py\0'
                        elif args[0] == 'merge-base':
                            return SimpleNamespace(returncode=1 if args[-1] == 'refs/remotes/origin/main' else 0)
                        elif args[0] == 'push':
                            pushes.append(args)
                            live, value = head, ''
                        else: raise AssertionError(args)
                        return SimpleNamespace(stdout=value, returncode=0)
                    def pr(*_):
                        self.assertFalse(connection.in_transaction)
                        return {'url': 'existing-pr', 'state': 'OPEN', 'headRefName': branch,
                                'baseRefName': 'main', 'headRefOid': live}
                    observe_pr = mock.Mock(side_effect=pr)
                    effects = publication_service.WorkerCheckpointPublicationEffects(
                        active_execplan=lambda c, r: state.active_execplan(r),
                        git=git, remote_branch_oid=lambda *_: live, pr_for_branch=observe_pr,
                        github_repository=lambda _: 'owner/repo',
                        validate_execplan_workspace_identity=lambda *_: None,
                        validate_managed_worktree=lambda *_: None,
                        update_project=lambda c, r, **values: state.update_project(r, preserve_transaction=True, **values),
                        run_request=state.run_checkpoint_publication_request)
                    record = ExecutionCheckpointRepository.record
                    def contend(repository, *args, **kwargs):
                        selected = bool(kwargs['snapshot'].get('worker_checkpoint_history')) == (phase == 'completion')
                        if selected:
                            attempts.append(repository.get('repo', 'G-047').generation)
                        result = record(repository, *args, **kwargs)
                        if selected and len(attempts) == 1:
                            if mode == 'sql_error':
                                connection.execute('UPDATE missing_publication_table SET value=1')
                            # A real SQLite reader prevents COMMIT after both writes.
                            reader.execute('BEGIN')
                            reader.execute('SELECT * FROM exec_plans').fetchall()
                        return result
                    pause = db_access._pause
                    def after_rollback(deadline, interval=.01):
                        self.assertFalse(connection.in_transaction)
                        self.assertFalse(connection._held)
                        self.assertEqual(state.check().publication_remote_oid, base)
                        saved = checkpoints.get('repo', 'G-047')
                        self.assertEqual(saved.generation, initial.generation + (phase == 'completion'))
                        self.assertEqual(tuple(connection.execute(
                            'SELECT publication_remote_oid, pr_head_oid FROM exec_plans').fetchone()), (base, base))
                        if mode == 'timeout':
                            return pause(deadline, interval)
                        reader.rollback()
                        if mode == 'cancel':
                            raise SelfMergeRuntimeHandoff()
                        if mode in ('generation', 'owner'):
                            with sqlite3.connect(path) as competitor:
                                competitor.row_factory = sqlite3.Row
                                if mode == 'owner':
                                    competitor.execute("UPDATE exec_plans SET worker_session_id='competing-session'")
                                else:
                                    record(ExecutionCheckpointRepository(competitor), 'repo', 'G-047',
                                        expected_generation=saved.generation, operation=None,
                                        expected_head=saved.expected_head, target_base_oid=base,
                                        observed_remote_oid=None,
                                        snapshot={**saved.snapshot, 'competing': True})
                    def publish():
                        return publication_service.push_worker_checkpoint_commits(effects, connection,
                            Path('/source'), Path(directory), 'repo', 'G-047', branch,
                            Path('/managed'), 'main', live, pr())
                    publish = state.bind(publish, mutates=True)
                    errors = {'generation': CheckpointRace, 'owner': runtime_state.RuntimeStateError,
                              'timeout': db_access.DatabaseWaitTimeout, 'cancel': SelfMergeRuntimeHandoff,
                              'sql_error': sqlite3.OperationalError}
                    with mock.patch.object(ExecutionCheckpointRepository, 'record', new=contend), \
                         mock.patch.object(db_access, '_pause', side_effect=after_rollback):
                        if mode == 'retry':
                            self.assertEqual(publish(), (head, 1))
                            self.assertEqual(len(attempts), 2)
                            self.assertEqual(attempts[0], attempts[1])
                        else:
                            with self.assertRaises(errors[mode]):
                                publish()
                    reader.rollback()
                    self.assertEqual(state.expected, state.repository.projected('repo'))
                    saved = checkpoints.get('repo', 'G-047')
                    self.assertEqual(saved.snapshot['worker_checkpoint_request'], request)
                    self.assertEqual(connection.execute('SELECT worker_session_id FROM exec_plans').fetchone()[0],
                                     'competing-session' if mode == 'owner' else 'same-session')
                    self.assertEqual(len(pushes), int(phase == 'completion' or mode == 'retry'))
                    observe_pr.assert_called_once()
                    if mode in ('timeout', 'cancel', 'sql_error'):
                        if phase == 'completion':
                            self.assertEqual(saved.snapshot['worker_checkpoint_pending']['target'], head)
                        # Resume observes the remote result without another push.
                        publish()
                        self.assertEqual(len(pushes), 1)
                    if mode not in ('generation', 'owner'):
                        saved = checkpoints.get('repo', 'G-047')
                        self.assertNotIn('worker_checkpoint_pending', saved.snapshot)
                        self.assertEqual(saved.snapshot['accepted_work_branch'], head)
                        self.assertEqual(len(saved.snapshot['worker_checkpoint_history']), 1)
                        self.assertEqual(tuple(connection.execute(
                            'SELECT publication_remote_oid, pr_head_oid FROM exec_plans').fetchone()), (head, head))
                    elif mode == 'generation':
                        self.assertTrue(saved.snapshot['competing'])
                    if mode in ('generation', 'owner'):
                        self.assertEqual(tuple(connection.execute(
                            'SELECT publication_remote_oid, pr_head_oid FROM exec_plans').fetchone()), (base, base))
                        self.assertEqual(saved.snapshot.get('worker_checkpoint_pending') is not None,
                                         phase == 'completion')
                    connection.close()
                    reader.close()

    def test_final_candidate_uses_literal_owned_paths_and_preserves_unsafe_index(self):
        from dataclasses import replace
        from loop_manager_internal.agents.scope_contracts import GitSnapshot
        from loop_manager_internal.execution.worker_checkpoint import stage_candidate
        selected = 'docs/plans/[G-001] Resume.md'
        snapshot = GitSnapshot('head', 'loop-manager/G-001', {}, {}, {}, {}, {}, {})
        good = replace(snapshot, dirty={'src/app.py': (('unstaged',), 'content')})
        git = mock.Mock()
        ports = SimpleNamespace(capture_git_snapshot=lambda _: good, git=git,
            _published_plan_allows_common_plans=lambda *_: False,
            snapshot=lambda: SimpleNamespace(catalog_plan=None),
            identity=SimpleNamespace(repo_path=Path('/source')))
        context = SimpleNamespace(worktree=Path('/managed'), branch=snapshot.branch,
                                  plan=SimpleNamespace(path=selected))
        stage_candidate(ports, context)
        git.assert_called_once_with(context.worktree, '--literal-pathspecs', 'add', '--', 'src/app.py')
        git.reset_mock()
        for unsafe in (replace(good, branch='other'),
                       replace(good, operations={'MERGE_HEAD': 'operation'}),
                       replace(good, index={'src/app.py': ('h 100644 blob 0',)}),
                       replace(good, index={'src/app.py': ('M 100644 blob 2',)}),
                       replace(good, dirty={'docs/requirements/Active.md': (('staged',), 'content')})):
            with self.subTest(unsafe=unsafe), self.assertRaises(RuntimeError):
                ports.capture_git_snapshot = lambda _, value=unsafe: value
                stage_candidate(ports, context)
        git.assert_not_called()

    def test_immutable_scope_preserves_base_changes_and_rejects_protected_intermediate_trees(self):
        from loop_manager_internal.agents.scope_validation import validate_worker_commit
        from tests.support.catalog import execplan_header
        path = 'docs/plans/[G-001] Resume.md'
        previous = execplan_header('G-001', 'Resume') + '\n## Progress\n- pending\n'
        current = previous + '\n- implementation preserved\n'
        changed = [path, 'src/app.py', 'docs/PRODUCT.md']
        inherited = False
        mode = '100644'
        def git(_path, *args, **kwargs):
            if args[:3] == ('show', '-s', '--format=%P'):
                return SimpleNamespace(stdout='parent', returncode=0)
            if args[0] == 'merge-base':
                return SimpleNamespace(returncode=0 if inherited else 1)
            if args[0] == 'diff':
                return SimpleNamespace(stdout='\0'.join(changed) + '\0', returncode=0)
            if args[0] == 'ls-tree':
                return SimpleNamespace(stdout=f'{mode if args[1] == "commit" else "100644"} blob oid\t{path}', returncode=0)
            if args[0] == 'show':
                return SimpleNamespace(stdout=previous if args[1].startswith('parent:') else current, returncode=0)
            raise AssertionError(args)
        def validate():
            validate_worker_commit(git, Path('/managed'), 'parent', 'commit', path, base_branch='main')
        validate()
        for forbidden in ('docs/requirements/Active.md', 'docs/requirements/archive/Old.md',
                          'docs/plans/[G-002] Other.md'):
            changed[:] = [forbidden]
            with self.subTest(path=forbidden), self.assertRaisesRegex(RuntimeError, '금지 문서'):
                validate()
        inherited = True
        validate()  # An immutable commit from the registered base retains its author.
        inherited = False
        changed[:] = [path]
        for candidate in (current.replace('priority: normal', 'priority: high'),
                          current.replace('sources:', '# Changed declaration\nsources:'),
                          current + '\n## User Decision\nDecision: Approved\n'):
            with self.subTest(candidate=candidate), self.assertRaises(RuntimeError):
                current = candidate
                validate()
        current = previous
        mode = '100755'
        with self.assertRaisesRegex(RuntimeError, 'mode'):
            validate()

    def test_pr_wait_releases_database_and_checkpoint_race_prevents_push(self):
        from loop_manager_internal.storage.db_access import open_connection
        from tests.support.database import runtime_connection
        for race in (False, True):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'state.db'
                source = runtime_connection()
                base, head, tree = 'a' * 40, 'b' * 40, 'c' * 40
                branch = 'loop-manager/G-047'
                source.execute("UPDATE projects SET origin='owner/repo'")
                source.execute("UPDATE exec_plans SET status='IMPLEMENTING', active_sha=?, "
                               "branch=?, worktree='/managed'", (base, branch))
                source.commit()
                with sqlite3.connect(path) as destination:
                    source.backup(destination)
                source.close()
                waiting, released = threading.Event(), threading.Event()
                failures, pushes = [], []
                live = None
                def git(_path, *args, **kwargs):
                    nonlocal live
                    if args[0] == 'remote': value = 'https://github.com/owner/repo.git'
                    elif args[0] == 'symbolic-ref': value = branch
                    elif args[0] == 'rev-parse': value = tree if args[-1].endswith('^{tree}') else head
                    elif args[0] == 'ls-remote': value = f'{live}\trefs/heads/{branch}' if live else ''
                    elif args[0] == 'rev-list': value = f'{head} {base}'
                    elif args[0] == 'show': value = base
                    elif args[0] == 'diff': value = 'app.py\0'
                    elif args[0] == 'merge-base':
                        return SimpleNamespace(returncode=1 if args[-1] == 'refs/remotes/origin/main' else 0)
                    elif args[0] == 'push':
                        pushes.append(args)
                        live, value = head, ''
                    else: raise AssertionError(args)
                    return SimpleNamespace(stdout=value, returncode=0)
                def run():
                    connection = open_connection(path, timeout=2)
                    connection.row_factory = sqlite3.Row
                    try:
                        def pr(*args):
                            waiting.set()
                            if not released.wait(3):
                                raise AssertionError('other DB connection was blocked by PR wait')
                            return None
                        effects = publication_service.WorkerCheckpointPublicationEffects(
                            active_execplan=lambda c, r: c.execute('SELECT * FROM exec_plans').fetchone(),
                            git=git, remote_branch_oid=lambda *_: None, pr_for_branch=pr,
                            github_repository=lambda _: 'owner/repo',
                            validate_execplan_workspace_identity=lambda *_: None,
                            validate_managed_worktree=lambda *_: None,
                            update_project=lambda c, r, **values: c.execute(
                                'UPDATE exec_plans SET publication_remote_oid=?', (values['publication_remote_oid'],)))
                        publication_service.push_worker_checkpoint_commits(effects, connection,
                            Path('/source'), Path(directory), 'repo', 'G-047', branch,
                            Path('/managed'), 'main', None, None)
                    except BaseException as error:
                        failures.append(error)
                    finally:
                        connection.close()
                worker = threading.Thread(target=run)
                worker.start()
                try:
                    self.assertTrue(waiting.wait(3), repr(failures))
                    other = open_connection(path, timeout=1)
                    other.row_factory = sqlite3.Row
                    try:
                        self.assertEqual(other.execute('SELECT count(*) FROM exec_plans').fetchone()[0], 1)
                        with other:
                            other.execute("UPDATE projects SET planning_fingerprint='other writer'")
                            if race:
                                checkpoints = ExecutionCheckpointRepository(other)
                                checkpoints.record('repo', 'G-047', expected_generation=0, operation=None,
                                    expected_head=base, target_base_oid=base, observed_remote_oid=None,
                                    snapshot={'competing': True})
                    finally:
                        other.close()
                finally:
                    released.set()
                    worker.join(4)
                self.assertFalse(worker.is_alive())
                if race:
                    self.assertEqual(len(failures), 1)
                    self.assertIsInstance(failures[0], RuntimeError)
                    self.assertEqual(pushes, [])
                    with sqlite3.connect(path) as observed:
                        self.assertIn('competing', observed.execute('SELECT snapshot FROM execution_checkpoints').fetchone()[0])
                else:
                    self.assertEqual(failures, [])
                    self.assertEqual(len(pushes), 1)
