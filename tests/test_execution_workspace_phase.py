"""Workspace Phase execution policy regression tests."""

from tests.support.execution import ExecutionFixture
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest import mock

import loop_manager as lm
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind


class ExecutionWorkspacePhaseTests(ExecutionFixture, TestCase):
    def test_reserved_user_rebase_does_not_reconstruct_manager_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        factory = mock.Mock(side_effect=AssertionError('reservation is not owned'))
        self.effects = replace(self.effects, checkpoint=checkpoint,
            workspace=replace(self.effects.workspace, current_rebase_service=factory))
        self.handler.effects = self.effects
        self.effects.workspace.is_directory.return_value = True
        self.connection.execute('UPDATE exec_plans SET workspace_owned=2')
        self.connection.commit()
        self.state.expected = self.repository.projected('repo')
        self.handler._update_snapshot(catalog_plan=self.state.active_execplan('repo'))
        before = checkpoint.get('repo', 'G-047')
        self.assertFalse(self.handler._resolving_rebase(self.state.row()))
        factory.assert_not_called()
        self.assertEqual(checkpoint.get('repo', 'G-047'), before)

    def test_reserved_live_writer_stops_before_worktree_preparation(self):
        from loop_manager_internal.storage import task_calls
        self.connection.execute('UPDATE exec_plans SET workspace_owned=2')
        self.connection.commit()
        self.state.expected = self.repository.projected('repo')
        owner = task_calls.reserve(self.connection, Path('/state'), 'repo', 'G-047', 'agent', 'run')
        task_calls.mark_uncertain(owner)
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.effects.workspace.make_worktree.assert_not_called()
        self.effects.agent.capture_git_snapshot.assert_not_called()
        self.effects.workspace.mark_execplan_workspace_owned.assert_not_called()

    def test_reserved_new_writer_before_confirmation_keeps_ownership_pending(self):
        from loop_manager_internal.storage import task_calls
        self.connection.execute('UPDATE exec_plans SET workspace_owned=2')
        self.connection.commit()
        self.state.expected = self.repository.projected('repo')
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = lambda *_a, **_k: task_calls.reserve(
            self.connection, Path('/state'), 'repo', 'G-047', 'agent', 'new-run')
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.effects.workspace.mark_execplan_workspace_owned.assert_not_called()
        self.assertEqual(self.state.active_execplan('repo')['workspace_owned'], lm.WORKSPACE_RESERVED)
        self.assertIsNotNone(self.connection.execute('SELECT * FROM task_call_reservations').fetchone())

    def test_reserved_resource_race_does_not_confirm_ownership(self):
        worktree = str(Path('/state/G-047').resolve())
        initial = lm.GitSnapshot('owned-head', 'loop-manager/G-047', {}, {}, {},
                                {worktree: ('HEAD=owned-head', 'branch=refs/heads/loop-manager/G-047')}, {}, {})
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.connection.execute('UPDATE exec_plans SET workspace_owned=2')
        self.connection.commit()
        self.state.expected = self.repository.projected('repo')
        for changed in (
            replace(initial, head='foreign-head'),
            replace(initial, index={'app.py': ('foreign-stage',)}),
            replace(initial, dirty={'user.txt': (('untracked',), 'preserved-data')}),
            replace(initial, operations={'merge': 'user-operation'}),
            replace(initial, worktrees={}),
            replace(initial, worktrees={worktree: ('HEAD=owned-head', 'locked=foreign')}),
            replace(initial, shared_metadata_complete=False),
        ):
            with self.subTest(changed=changed), mock.patch.object(Path, 'is_dir', return_value=True):
                self.effects.agent.capture_git_snapshot.return_value = initial
                self.effects.workspace.make_worktree.side_effect = lambda *_a, **_k: setattr(
                    self.effects.agent.capture_git_snapshot, 'return_value', changed)
                result = self.handler.prepare()
                self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
                self.assertEqual(self.state.active_execplan('repo')['workspace_owned'], lm.WORKSPACE_RESERVED)
                self.effects.workspace.mark_execplan_workspace_owned.assert_not_called()

    def test_reserved_confirmation_ignores_other_worktree_and_ref_changes(self):
        worktree = str(Path('/state/G-047').resolve())
        initial = lm.GitSnapshot('owned-head', 'loop-manager/G-047', {}, {}, {},
                                {worktree: ('HEAD=owned-head',)}, {}, {})
        changed = replace(initial, refs={'refs/heads/other': ('new-head', '')},
                          worktrees={**initial.worktrees, '/state/other': ('HEAD=new-head',)})
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.connection.execute('UPDATE exec_plans SET workspace_owned=2')
        self.connection.commit()
        self.state.expected = self.repository.projected('repo')
        self.effects.agent.capture_git_snapshot.return_value = initial
        self.effects.workspace.make_worktree.side_effect = lambda *_a, **_k: setattr(
            self.effects.agent.capture_git_snapshot, 'return_value', changed)
        with mock.patch.object(Path, 'is_dir', return_value=True):
            self.handler.prepare()
        self.effects.workspace.mark_execplan_workspace_owned.assert_called_once()

    def test_reserved_checkpoint_race_does_not_confirm_ownership(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.connection.execute("UPDATE exec_plans SET workspace_owned=2")
        self.connection.commit()
        self.state.expected = self.repository.projected('repo')
        def competing(*_args, **_kwargs):
            current = checkpoint.get('repo', 'G-047')
            checkpoint.record('repo', 'G-047', expected_generation=current.generation,
                              operation=current.operation, expected_head=current.expected_head,
                              target_base_oid=current.target_base_oid,
                              observed_remote_oid=current.observed_remote_oid,
                              snapshot={**current.snapshot, 'competing': True})
        self.effects.workspace.make_worktree.side_effect = competing

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(self.state.active_execplan('repo')['workspace_owned'], lm.WORKSPACE_RESERVED)
        self.effects.workspace.mark_execplan_workspace_owned.assert_not_called()
        self.assertTrue(checkpoint.get('repo', 'G-047').snapshot['competing'])

    def test_worktree_recovery_failure_keeps_durable_pending_boundary(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = RuntimeError('temporary worktree failure')
        self.state.update_project('repo', worker_session_id='worker-session')

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        saved = checkpoint.get('repo', 'G-047').snapshot
        self.assertTrue(saved['worktree_recovery_pending'])
        self.assertEqual(saved['branch'], 'loop-manager/G-047')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()


    def test_workspace_runtime_failure_retries_without_blocking_or_losing_context(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = RuntimeError('temporary git failure')
        self.state.update_project('repo', worker_session_id='worker-session',
                                  worker_result='saved result')

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'worktree_recovery_failed')
        row = self.state.row()
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['worker_result'], 'saved result')
        self.assertEqual(row['worktree'], str(Path('/state/G-047')))
        self.effects.blocking.block_active_execplan_from_revision.assert_not_called()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM blocked_checkpoints WHERE repo = 'repo'"
        ).fetchone()[0], 0)


    def test_workspace_violation_blocks_before_agent(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.validate_execplan_workspace_identity.side_effect = lm.WorkspaceOwnershipError('ownership')
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan_from_revision.assert_called_once()
        self.assertEqual(self.calls, [])


class RemoteAdoptionPhaseTests(ExecutionFixture, TestCase):
    def setUp(self):
        import tempfile
        from types import SimpleNamespace
        from unittest import mock
        from loop_manager_internal.storage import database
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.worktree = root / 'G-047'
        self.handler._identity = replace(self.handler._identity, state_dir=root)
        self.state.update_project('repo', worktree=str(root / 'G-047'))
        self.connection.executescript(
            "CREATE TABLE manager_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO manager_metadata VALUES ('remote_adoption_bootstrap', 'null');"
            "UPDATE projects SET origin='owner/repo', planning_attempt_fingerprint=NULL;"
            "UPDATE exec_plans SET workspace_owned=0;")
        database.record_remote_work_adoption_catalog(self.connection, 'repo', 'a' * 40, ('G-047',))
        self.connection.commit()
        self.state.expected = self.repository.projected('repo')
        self.head = 'a' * 40
        self.live = self.head
        self.local = None
        self.pr = None
        self.checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects.agent.capture_git_snapshot.side_effect = lambda *_a, **_k: lm.GitSnapshot(
            self.local or self.head, 'loop-manager/G-047', {}, {}, {},
            {str(self.worktree.resolve()): (f'HEAD={self.local or self.head}',
                                          'branch=refs/heads/loop-manager/G-047')}, {}, {})
        workspace = replace(self.effects.workspace,
            reserve_execplan_workspace=self.state.bind(lm.reserve_execplan_workspace, connected=True, mutates=True),
            mark_execplan_workspace_owned=self.state.bind(lm.mark_execplan_workspace_owned, connected=True, mutates=True))
        publication = replace(self.effects.publication, github_repository=lm.github_repository,
            reconcile_existing_pull_request=self.state.bind(lm.reconcile_existing_pull_request,
                                                            connected=True, mutates=True))
        self.effects = replace(self.effects, workspace=workspace, publication=publication, checkpoint=self.checkpoint)
        self.handler.effects = self.effects
        workspace._plan_id_from_path.return_value = 'G-047'
        publication.pr_for_branch.side_effect = lambda *_a, **_k: self.pr
        def git(_path, *args, **_kwargs):
            if args[0] == 'remote':
                return SimpleNamespace(stdout='https://github.com/owner/repo.git', returncode=0)
            if args[0] == 'show-ref':
                return SimpleNamespace(stdout='', returncode=0 if self.local else 1)
            if args[0] == 'ls-remote':
                return SimpleNamespace(stdout=f'{self.live}\trefs/heads/loop-manager/G-047' if self.live else '', returncode=0)
            if args[0] == 'rev-parse':
                if 'origin/main' in args[-1]:
                    return SimpleNamespace(stdout='base', returncode=0)
                return SimpleNamespace(stdout=self.local or self.head, returncode=0)
            return self.git(_path, *args, **_kwargs)
        workspace.git.side_effect = git
        self.handler._resolving_rebase = mock.Mock(return_value=False)
        self.handler._fetch_latest_base = mock.Mock(return_value='base')
        self.handler._checkpoint_remote_oid = mock.Mock(side_effect=lambda *_: self.live)
        def create(_repo, worktree, branch, base, **options):
            if not worktree.exists():
                options['before_create']()
                self.local = options['start_point']
                worktree.mkdir()
            return 'created'
        workspace.make_worktree.side_effect = create

    def test_absent_pr_adopts_exact_head_without_inherited_evidence(self):
        from loop_manager_internal.execution.execution_runtime import ExecutionContext
        context = self.handler.prepare()
        self.assertIsInstance(context, ExecutionContext)
        self.assertEqual(self.effects.workspace.make_worktree.call_args.kwargs['start_point'], self.head)
        self.assertEqual(self.local, self.head)
        row = self.state.active_execplan('repo')
        self.assertEqual(row['workspace_owned'], lm.WORKSPACE_OWNED)
        for key in ('worker_session_id', 'worker_result', 'verify_result', 'review_result',
                    'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(row[key], key)
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot['accepted_work_branch'], self.head)

    def test_open_pr_keeps_identity_and_runs_new_worker(self):
        self.pr = {'url': 'https://github.com/owner/repo/pull/1', 'state': 'OPEN',
                   'headRefName': 'loop-manager/G-047', 'baseRefName': 'main', 'headRefOid': self.head}
        context = self.handler.prepare()
        self.assertEqual(context.pr_url, self.pr['url'])
        self.assertIsNone(context.worker_session_id)
        result = self.handler.run(self.handler.guard(context))
        self.effects.agent.run_agent.assert_called_once()
        self.assertEqual(self.effects.agent.run_agent.call_args.kwargs['session_id'], None)
        self.assertEqual(self.state.row()['pr_url'], self.pr['url'])
        self.assertEqual(result.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.effects.publication.command.assert_not_called()
        self.dirty = False
        from loop_manager_internal.verification.inputs import InputObservation
        self.effects.validation.observe_verify_inputs.return_value = InputObservation('adopted-inputs')
        verified = self.handler.verify(result.context, self.handler)
        self.assertEqual(verified.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        reviewed = self.handler.review(verified.context)
        self.assertEqual(reviewed.kind, ExecutionOutcomeKind.PHASE_ADVANCED, reviewed.detail)
        self.effects.validation.verify.assert_called_once()
        self.assertEqual([call.args[4] for call in self.effects.agent.run_agent.call_args_list],
                         ['worker', 'reviewer'])
        self.assertEqual(self.state.row()['pr_url'], self.pr['url'])

    def test_closed_pr_is_durably_blocked_without_workspace_or_worker(self):
        self.pr = {'url': 'https://github.com/owner/repo/pull/1', 'state': 'CLOSED',
                   'headRefName': 'loop-manager/G-047', 'baseRefName': 'main', 'headRefOid': self.head}
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        row = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual((row['status'], row['pr_url']), ('PLAN_BLOCKED', self.pr['url']))
        self.effects.workspace.make_worktree.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()

    def test_local_branch_with_same_oid_has_no_adoption_intent(self):
        self.local = self.head
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.workspace.make_worktree.assert_not_called()
        self.assertNotIn('remote_adoption', self.checkpoint.get('repo', 'G-047').snapshot)

    def test_created_workspace_resumes_same_intent_after_interruption(self):
        original = self.effects.workspace.make_worktree.side_effect
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('stopped after Git effect')
        self.effects.workspace.make_worktree.side_effect = interrupted
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        saved = self.checkpoint.get('repo', 'G-047').snapshot['remote_adoption']
        self.effects.workspace.make_worktree.side_effect = original
        context = self.handler.prepare()
        self.assertEqual(context.worktree, Path(self.state.row()['worktree']))
        options = self.effects.workspace.make_worktree.call_args.kwargs
        self.assertTrue(options['require_creation_evidence'])
        self.assertEqual(options['expected_head'], self.head)
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot['remote_adoption'], saved)

    def test_branch_deleted_after_intent_without_local_artifacts_is_preserved(self):
        current = self.checkpoint.get('repo', 'G-047')
        intent = {'version': 1, 'repo': 'repo', 'plan_id': 'G-047',
                  'branch': 'loop-manager/G-047', 'base': 'main', 'head': self.head}
        self.checkpoint.record('repo', 'G-047', expected_generation=current.generation,
            operation=None, expected_head=None, target_base_oid=None, observed_remote_oid=None,
            snapshot={'remote_adoption': intent})
        self.live = None
        self.handler.prepare()
        self.effects.workspace.make_worktree.assert_not_called()
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot['remote_adoption'], intent)

    def test_checkpoint_race_during_creation_keeps_competing_record_and_local_work(self):
        original = self.effects.workspace.make_worktree.side_effect
        def competing(*args, **kwargs):
            original(*args, **kwargs)
            current = self.checkpoint.get('repo', 'G-047')
            self.checkpoint.record('repo', 'G-047', expected_generation=current.generation,
                operation=None, expected_head=None, target_base_oid=None, observed_remote_oid=None,
                snapshot={**current.snapshot, 'competing': True})
        self.effects.workspace.make_worktree.side_effect = competing
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertTrue(self.checkpoint.get('repo', 'G-047').snapshot['competing'])
        self.assertEqual(self.local, self.head)
        self.assertEqual(self.state.active_execplan('repo')['workspace_owned'], lm.WORKSPACE_RESERVED)
        self.effects.agent.run_agent.assert_not_called()

    def test_remote_advance_during_creation_preserves_intent_for_normal_recovery(self):
        original = self.effects.workspace.make_worktree.side_effect
        def advancing(*args, **kwargs):
            original(*args, **kwargs)
            self.live = 'b' * 40
        self.effects.workspace.make_worktree.side_effect = advancing
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        saved = self.checkpoint.get('repo', 'G-047').snapshot['remote_adoption']
        self.effects.workspace.make_worktree.side_effect = original
        context = self.handler.prepare()
        self.assertEqual(self.local, saved['head'])
        self.assertEqual(context.worker_session_id, None)
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot['accepted_work_branch'], saved['head'])
        self.effects.agent.run_agent.assert_not_called()
        # The phase coordinator always runs normal remote integration before dispatch.
        from loop_manager_internal.execution.execution_runtime import ExecutionPhaseCoordinator
        from types import SimpleNamespace
        from unittest import mock
        recovery = mock.Mock(return_value=context)
        ports = SimpleNamespace(prepare=lambda: context, recover=recovery)
        self.assertEqual(ExecutionPhaseCoordinator(ports).prepare(), context)
        recovery.assert_called_once_with(context)

    def test_catalog_change_during_fetch_does_not_create_workspace(self):
        def changed():
            self.connection.execute("UPDATE exec_plans SET content_hash='changed', catalog_generation=catalog_generation+1")
            self.connection.commit()
            return 'base'
        self.handler._fetch_latest_base.side_effect = changed
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.effects.workspace.make_worktree.assert_not_called()
        self.assertEqual(self.connection.execute('SELECT content_hash FROM exec_plans').fetchone()[0], 'changed')

    def test_checkpoint_change_during_fetch_is_not_adopted_as_current_intent(self):
        def changed():
            current = self.checkpoint.get('repo', 'G-047')
            self.checkpoint.record('repo', 'G-047', expected_generation=current.generation,
                operation=None, expected_head=None, target_base_oid=None, observed_remote_oid=None,
                snapshot={'competing': True})
            return 'base'
        self.handler._fetch_latest_base.side_effect = changed
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.effects.workspace.make_worktree.assert_not_called()
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot, {'competing': True})
