"""Immediate Rebase Publication Phase regression tests."""

from tests.support.current_rebase import current_rebase_case
from dataclasses import replace
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.execution.execution_runtime import ExecutionPhase




class ImmediateRebasePublicationTests(TestCase):
    """Only fake external effects; test real acceptance, intents and runtime CAS."""
    def setUp(self):
        self.seed = current_rebase_case(self)
        self.seed._prepare_current_checkpoint_recovery()
        self.handler = self.seed.handler
        self.handler._publish_rebased_checkpoint = handlers.ExecutionHandlers._publish_rebased_checkpoint.__get__(self.handler)
        self.state, self.checkpoints = self.seed.state, self.seed.checkpoint
        self.fixture = self.seed.fixture
        self.remote = 'remote'
        live = mock.patch.object(handlers.manager_git_journal, 'live_remote_branch_oid',
                                 side_effect=lambda *_: self.remote)
        live.start()
        self.addCleanup(live.stop)
        self.pushes = []
        self.events = []
        self.push_failure = False
        self.before_push = None
        self.handler.effects.workspace.remote_branch_oid.side_effect = lambda *_: self.remote
        self.handler.effects.workspace.git.side_effect = self.git
        factory = self.seed.factory
        self.handler.effects = replace(self.handler.effects, workspace=replace(
            self.handler.effects.workspace, current_rebase_service=lambda context, **options:
            factory(context, **{'remote_oid': self.state.row()['publication_remote_oid'] or self.seed.expected.remote.oid, **options})))

    def git(self, path, *args, **kwargs):
        if args[0] == 'ls-remote':
            return SimpleNamespace(returncode=0, stdout=(
                f'{self.remote}\trefs/heads/loop-manager/G-047\n' if self.remote else ''))
        if args[0] == 'push':
            self.events.append('push')
            self.pushes.append(args)
            if self.before_push:
                self.before_push()
            if self.push_failure:
                return SimpleNamespace(returncode=1, stdout='permission denied')
            self.remote = 'owned-head'
            self.seed.observed = replace(self.seed.observed,
                                         remote=replace(self.seed.expected.remote, oid=self.remote))
            return SimpleNamespace(returncode=0, stdout='pushed')
        return self.fixture.observed_git(path, *args, **kwargs)

    def complete(self):
        self.handler._complete_rebase_intent(self.handler._context())

    def restart(self):
        self.state = ExecutionStatePort(self.fixture.connection,
            runtime_state.RuntimeStateRepository(self.fixture.connection), 'repo')
        self.handler.effects = replace(self.handler.effects, state=self.state)
        replacement = handlers.ExecutionHandlers(self.handler.effects,
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'), base='main',
            base_sha='later-base', provider='codex', config_path=Path('/config'), newly_activated=False)
        replacement.plan = self.handler.plan
        replacement.catalog_plan = self.handler.catalog_plan
        self.handler = replacement

    def direct_ports(self):
        stack = ExitStack()
        for patch in (
            mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **options:
                self.seed.factory(None, remote_oid=options.get('remote_oid', self.remote))),
            mock.patch.object(lm, 'git', side_effect=self.git),
            mock.patch.object(lm, 'remote_branch_oid', side_effect=lambda *_: self.remote),
            mock.patch.object(lm, 'pr_for_branch', return_value=None),
        ):
            stack.enter_context(patch)
        return stack

    def test_pending_publication_passes_persisted_agent_scope_before_handler_recovery(self):
        class Exit(BaseException):
            pass
        for stop in ('before-push', 'after-push'):
            with self.subTest(stop=stop):
                self.setUp()
                self.before_push = (lambda: (_ for _ in ()).throw(Exit())) if stop == 'before-push' else None
                with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
                    with self.assertRaises(Exit):
                        self.complete()
                pending = self.checkpoints.get('repo', 'G-047')
                branch, worktree = 'loop-manager/G-047', '/state/G-047'
                ref, remote_ref = f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'
                original = lm.GitSnapshot('before', branch, {}, {},
                    {ref: ('before', ''), remote_ref: ('remote', ''), 'refs/heads/other': ('other', '')},
                    {worktree: ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
                current = replace(original, head='owned-head', refs={**original.refs,
                    ref: ('owned-head', ''), remote_ref: (self.remote, '')},
                    worktrees={worktree: ('HEAD=owned-head', f'branch=refs/heads/{branch}')})
                self.state.update_project('repo', agent_role='worker',
                    agent_snapshot=original.serialize(), agent_result_snapshot=original.serialize())
                with self.direct_ports(), ExitStack() as ports:
                    capture = ports.enter_context(mock.patch.object(lm, 'capture_git_snapshot', return_value=current))
                    for patch in (
                        mock.patch.object(lm, '_config_entries', return_value={}),
                        mock.patch.object(lm, '_proven_merged_cleanup_paths', return_value=frozenset()),
                        mock.patch.object(Path, 'is_dir', return_value=True),
                        mock.patch.object(lm, 'git_bytes', return_value=b''),
                    ):
                        ports.enter_context(patch)
                    self.assertEqual(lm.validate_persisted_agent_scope(
                        self.fixture.connection, Path('/repo'), Path('/state'), 'repo'), current)
                    self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                    # Normalization never absorbs a different shared ref.
                    capture.return_value = replace(current, refs={**current.refs,
                        'refs/heads/other': ('foreign', '')})
                    with self.assertRaises(lm.AgentScopeError):
                        lm.validate_persisted_agent_scope(
                            self.fixture.connection, Path('/repo'), Path('/state'), 'repo')
                self.assertEqual(self.fixture.connection.execute(
                    "SELECT agent_snapshot FROM projects WHERE repo='repo'").fetchone()[0], original.serialize())
                self.before_push = None
                self.restart()
                self.handler._publish_rebased_checkpoint(self.handler._context())
                self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                                 pending.snapshot['rebase_publication_intent'])
                self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
                self.assertEqual(len(self.pushes), 2 if stop == 'before-push' else 1)

    def test_pending_publication_service_accepts_only_saved_lease_or_pushed_head(self):
        class Exit(BaseException):
            pass
        self.before_push = lambda: (_ for _ in ()).throw(Exit())
        with self.assertRaises(Exit):
            self.complete()
        from tests.support.revision import definition
        self.state.update_project('repo', definition_snapshot=definition(
            path='docs/plans/[G-047] Runtime.md').serialize())
        with ExitStack() as ports:
            for patch in (
                mock.patch.object(lm, '_revision_execplan_input_fingerprint', return_value='inputs'),
                mock.patch.object(lm, '_worktree_execplan_input_fingerprint', return_value='inputs'),
                mock.patch.object(lm, 'requirement_archive_candidates', return_value=()),
                mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False),
                mock.patch.object(lm, 'validate_active_execplan_metadata'),
                mock.patch.object(lm, '_owned_rebase_metadata', return_value=None),
                mock.patch.object(lm, 'git', return_value=SimpleNamespace(returncode=0, stdout='')),
            ):
                ports.enter_context(patch)
            for oid in ('remote', 'owned-head'):
                self.remote = oid
                service = lm._current_rebase_service(self.fixture.connection, Path('/repo'),
                    Path('/state'), 'repo', 'G-047', Path('/state/G-047'), 'loop-manager/G-047')
                self.assertEqual(service.expected.remote.oid, oid)
            for oid in (None, 'external'):
                self.remote = oid
                with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
                    lm._current_rebase_service(self.fixture.connection, Path('/repo'),
                        Path('/state'), 'repo', 'G-047', Path('/state/G-047'), 'loop-manager/G-047')

    def test_blocked_release_publishes_clean_head_before_restoring_staged_work(self):
        self.assert_blocked_release()

    def test_blocked_publication_interruption_restores_stage_once_before_requeue(self):
        self.assert_blocked_release(interrupted=True)

    def test_blocked_publication_rejection_keeps_stage_snapshot_and_retries_same_intent(self):
        self.assert_blocked_release(rejected=True)

    def test_blocked_publication_retry_finishes_saved_target_after_base_advances(self):
        self.assert_blocked_release(rejected=True, advanced_base=True)

    def test_plan_blocks_recovers_pushed_checkpoint_before_old_remote_check(self):
        self.assert_blocked_release(interrupted=True, entrypoint=True)

    def test_plan_blocks_retries_saved_publication_before_old_remote_check(self):
        self.assert_blocked_release(rejected=True, advanced_base=True, entrypoint=True)

    def assert_blocked_release(self, *, interrupted=False, rejected=False,
                               advanced_base=False, entrypoint=False):
        from loop_manager_internal.git.managed_rebase_validation import FileChange
        connection, runtime = self.fixture.connection, self.fixture.repository
        runtime.transition_and_clear('repo', 'G-047', plan_values={
            'status': 'PLAN_BLOCKED', 'blocked_fingerprint': 'stored'}, enqueue=False)
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo' AND plan_id='G-047'").fetchone()
        branch, worktree = 'loop-manager/G-047', '/state/G-047'
        before = lm.GitSnapshot('before', branch,
            {'app.py': ('H 100644 staged-blob 0',)}, {'app.py': (('staged',), 'saved-content')},
            {f'refs/heads/{branch}': ('before', '')},
            {worktree: ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
        local = {'head': 'before', 'staged': True, 'base': 'base'}
        def capture(_):
            return replace(before, head=local['head'],
                index=before.index if local['staged'] else {'app.py': ('H 100644 rebased-blob 0',)},
                dirty={'app.py': (('staged',) if local['staged'] else ('unstaged',), 'saved-content')},
                refs={f'refs/heads/{branch}': (local['head'], '')},
                worktrees={worktree: ('HEAD=' + local['head'], f'branch=refs/heads/{branch}')})
        original_git = self.git
        def git(path, *args, **kwargs):
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(returncode=0, stdout=local['head'])
            if args == ('rev-parse', 'refs/remotes/origin/main'):
                return SimpleNamespace(returncode=0, stdout=local['base'])
            if args == ('merge-base', '--is-ancestor', 'refs/remotes/origin/main', 'HEAD'):
                return SimpleNamespace(returncode=1, stdout='')
            return original_git(path, *args, **kwargs)
        def rebase(*_a, **_k):
            local['head'] = 'owned-head'
            local['staged'] = False
            return SimpleNamespace(returncode=0, stdout='')
        def restore(_worktree, snapshot):
            self.assertEqual(snapshot, before)
            self.assertEqual(self.remote, 'owned-head')
            self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
            self.events.append('restore-staging')
            local['staged'] = True
            self.seed.observed = replace(self.seed.observed, index_clean=False,
                staged=(FileChange('app.py', '100644', '100644', 'old', 'staged-blob', 'M'),))
        self.git = git
        with self.direct_ports(), ExitStack() as ports:
            for patch in (
                mock.patch.object(lm, '_plan_block_selected_for_release', return_value=plan),
                mock.patch.object(lm, '_revision_execplan_input_fingerprint', return_value='changed'),
                mock.patch.object(lm, '_revision_review_issue_fingerprint', return_value=None),
                mock.patch.object(lm, '_worktree_execplan_input_fingerprint', return_value='stored'),
                mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                mock.patch.object(lm, 'make_worktree'),
                mock.patch.object(lm, 'validate_blocked_agent_scope'),
                mock.patch.object(lm, 'preserve_blocked_agent_changes', return_value=before),
                mock.patch.object(lm, '_journaled_direct_rebase', side_effect=rebase),
                mock.patch.object(lm, '_blocked_execplan_metadata_error', return_value=None),
                mock.patch.object(lm, 'capture_git_snapshot', side_effect=capture),
                mock.patch.object(lm, '_owned_rebase_journal_state', return_value={'clean': 'rebase result'}),
                mock.patch.object(lm, '_restore_agent_staged_entries', side_effect=restore),
                mock.patch.object(Path, 'is_dir', return_value=True),
            ):
                ports.enter_context(patch)
            if interrupted:
                class Exit(BaseException):
                    pass
                with mock.patch.object(ExecutionStatePort, 'complete_rebase_publication', side_effect=Exit()):
                    with self.assertRaises(Exit):
                        lm.resume_blocked_execplan_if_inputs_changed(
                            connection, Path('/repo'), 'repo', 'main', Path('/state'))
                pending = self.checkpoints.get('repo', 'G-047')
                self.assertEqual(pending.operation, 'rebase_publish')
                self.assertEqual(self.events, ['push'])
                self.assertFalse(local['staged'])
                self.assertEqual(runtime.get('repo', 'G-047').publication_remote_oid, 'remote')
            if rejected:
                self.push_failure = True
                self.assertFalse(lm.resume_blocked_execplan_if_inputs_changed(
                    connection, Path('/repo'), 'repo', 'main', Path('/state')))
                pending = self.checkpoints.get('repo', 'G-047')
                self.assertEqual(pending.operation, 'rebase_publish')
                self.assertEqual(pending.snapshot['blocked_resume']['before_snapshot'], before.serialize())
                self.assertEqual(self.remote, 'remote')
                self.assertEqual(self.events, ['push'])
                self.assertFalse(local['staged'])
                self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
                self.push_failure = False
            if advanced_base:
                local['base'] = 'later-base'
            if entrypoint:
                with mock.patch.object(lm, '_reconcile_published_blocked_checkpoint',
                                       side_effect=AssertionError('old remote checked before recovery')) as reconcile:
                    # A retry refusal must preserve the pending release instead
                    # of falling through to the stale checkpoint remote gate.
                    with mock.patch.object(lm, '_complete_direct_rebase_checkpoint',
                                           side_effect=RuntimeError('temporary publication failure')):
                        self.assertFalse(lm.resume_plan_blocks(
                            connection, Path('/repo'), Path('/state'), 'repo', 'main'))
                    self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                    self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
                    self.assertFalse(local['staged'])
                    self.assertTrue(lm.resume_plan_blocks(
                        connection, Path('/repo'), Path('/state'), 'repo', 'main'))
                    reconcile.assert_not_called()
            else:
                self.assertTrue(lm.resume_blocked_execplan_if_inputs_changed(
                    connection, Path('/repo'), 'repo', 'main', Path('/state')))
            self.assertEqual(self.checkpoints.get('repo', 'G-047').target_base_oid, 'base')
            self.assertEqual(runtime.get('repo', 'G-047').status, 'READY')
            self.assertIsNone(runtime.get('repo', 'G-047').verify_result)
            if interrupted or rejected:
                self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                                 pending.snapshot['rebase_publication_intent'])
        self.assertEqual(self.events, ['push', 'push', 'restore-staging'] if rejected else ['push', 'restore-staging'])
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.observed_remote_oid, 'owned-head')
        self.assertEqual(saved.snapshot['blocked_resume']['owner_oid'], 'remote')
        self.assertEqual(runtime.get('repo', 'G-047').publication_remote_oid, 'owned-head')
        self.assertEqual(capture(None).index, before.index)
        self.assertEqual(capture(None).dirty, before.dirty)
        runtime.activate('repo', 'G-047', activation_sha='base', branch=branch, worktree=worktree)
        self.restart()
        self.handler._fetch_latest_base = mock.Mock(return_value='base')
        recovered = self.handler.recover(self.handler._context())
        self.assertIsInstance(recovered, handlers.ExecutionContext)
        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(self.seed.observed.staged[0].new_oid, 'staged-blob')
        self.assertEqual(len(self.pushes), 2 if rejected else 1)
        if rejected:
            self.assertEqual(self.pushes[0], self.pushes[1])
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()

    def test_completion_publishes_before_candidate_work_without_historical_receipts(self):
        self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(self.pushes, [('push',
            '--force-with-lease=refs/heads/loop-manager/G-047:remote', 'origin',
            'owned-head:refs/heads/loop-manager/G-047')])
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.observed_remote_oid, 'owned-head')
        self.assertEqual(saved.snapshot['last_adopted_remote_oid'], 'owned-head')
        self.assertEqual(saved.snapshot['failure'], 'old failure')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-worker')
        for key in ('worker_result', 'verify_result', 'review_result', 'publication_token'):
            self.assertIsNone(self.state.row()[key])
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()
        self.fixture.effects.publication.push_managed_publication_branch.assert_not_called()
        self.fixture.effects.publication.command.assert_not_called()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), saved)
        self.assertEqual(len(self.pushes), 1)

    def test_interruption_before_push_reuses_intent_before_later_base_fetch(self):
        class Exit(BaseException):
            pass
        self.before_push = lambda: (_ for _ in ()).throw(Exit())
        with self.assertRaises(Exit):
            self.complete()
        intent = self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent']
        self.before_push = None
        self.restart()
        def fetch():
            self.events.append('fetch-later-base')
            raise Exit()
        self.handler._fetch_latest_base = fetch
        with self.assertRaises(Exit):
            self.handler.guard(self.handler._context())
        self.assertEqual(self.events[-2:], ['push', 'fetch-later-base'])
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'], intent)
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')

    def test_interruption_after_push_confirms_once_without_repeating_push(self):
        class Exit(BaseException):
            pass
        with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
            with self.assertRaises(Exit):
                self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(pending.operation, 'rebase_publish')
        self.assertEqual(self.state.row()['publication_remote_oid'], 'remote')
        self.restart()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['rebase_publication_intent'], pending.snapshot['rebase_publication_intent'])
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), saved)
        self.assertEqual(len(self.pushes), 1)

    def test_transient_push_failure_retains_same_lease_and_intent(self):
        self.push_failure = True
        with self.assertRaises(handlers.RecoveryFailure):
            self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        self.push_failure = False
        self.restart()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.pushes[0], self.pushes[1])
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                         pending.snapshot['rebase_publication_intent'])

    def test_remote_race_deletion_or_new_pr_never_replaces_intent_lease(self):
        for raced in ('external-commit', None, 'pr'):
            with self.subTest(raced=raced):
                self.setUp()
                self.push_failure = True
                with self.assertRaises(handlers.RecoveryFailure):
                    self.complete()
                pending = self.checkpoints.get('repo', 'G-047')
                if raced == 'pr':
                    self.fixture.effects.publication.pr_for_branch.return_value = {'url': 'new-pr'}
                else:
                    self.remote = raced
                self.push_failure = False
                with self.assertRaises(handlers.RecoveryFailure):
                    self.handler._publish_rebased_checkpoint(self.handler._context())
                self.assertEqual(len(self.pushes), 1)
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                self.assertEqual(self.state.row()['publication_remote_oid'], 'remote')

    def test_new_branch_existing_pr_or_different_owner_never_gets_early_push(self):
        for excluded in ('new-branch', 'existing-pr', 'other-owner'):
            with self.subTest(excluded=excluded):
                self.setUp()
                if excluded == 'new-branch':
                    self.seed.expected = replace(self.seed.expected,
                        remote=replace(self.seed.expected.remote, oid=None))
                    self.seed.observed = replace(self.seed.observed, remote=self.seed.expected.remote)
                    current = self.checkpoints.get('repo', 'G-047')
                    self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
                        operation='rebase', expected_head='before', target_base_oid='base',
                        observed_remote_oid=None, snapshot=current.snapshot)
                    self.state.update_project('repo', publication_remote_oid=None)
                elif excluded == 'existing-pr':
                    self.state.update_project('repo', pr_url='existing-pr')
                else:
                    self.seed.observed = replace(self.seed.observed, branch='another-plan')
                with mock.patch.object(self.handler, '_owned_checkpoint_remote', return_value=True):
                    if excluded == 'other-owner':
                        with self.assertRaises(RuntimeError):
                            self.complete()
                    elif excluded == 'existing-pr':
                        # Existing PR rebase still uses the ordinary path; there
                        # is no intermediate publication entry for its branch.
                        saved = self.checkpoints.get('repo', 'G-047')
                        from tests.support.rebase_observations import decision_for
                        accepted = decision_for(self.seed.expected, self.seed.observed).acceptance
                        self.checkpoints.accept_current_rebase('repo', 'G-047', acceptance=accepted)
                        self.handler._publish_rebased_checkpoint(self.handler._context())
                    else:
                        self.complete()
                self.assertEqual(self.pushes, [])

    def test_same_oid_completes_without_push_and_next_rebase_uses_adopted_oid(self):
        self.remote = 'owned-head'
        self.seed.expected = replace(self.seed.expected, remote=replace(self.seed.expected.remote, oid=self.remote))
        self.seed.observed = replace(self.seed.observed, remote=self.seed.expected.remote)
        self.state.update_project('repo', publication_remote_oid=self.remote)
        current = self.checkpoints.get('repo', 'G-047')
        self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head='before', target_base_oid='base',
            observed_remote_oid=self.remote, snapshot=current.snapshot)
        with mock.patch.object(self.handler, '_owned_checkpoint_remote', return_value=True):
            self.complete()
        self.assertEqual(self.pushes, [])
        with mock.patch.object(handlers.manager_git_journal, 'begin_current_rebase'):
            self.handler._record_rebase_intent(self.handler._context(), 'later-base')
        current = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(current.operation, 'rebase')
        self.assertEqual(current.observed_remote_oid, 'owned-head')
        self.assertEqual(current.target_base_oid, 'later-base')

    def test_final_push_resume_uses_latest_adopted_oid_instead_of_original_checkpoint(self):
        self.complete()
        current = self.checkpoints.get('repo', 'G-047')
        pending = self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='push', expected_head='worker-head', target_base_oid='base',
            observed_remote_oid='owned-head', snapshot={**current.snapshot, 'remote_before': 'owned-head'})
        self.seed.observed = replace(self.seed.observed, head='worker-head',
            branch_ref=('worker-head', ''),
            registration=('HEAD=worker-head', 'branch=refs/heads/loop-manager/G-047'),
            remote=replace(self.seed.expected.remote, oid='worker-head'))
        self.assertTrue(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'worker-head', pending))
        self.assertFalse(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'unrelated-head', pending))
        # A restart may see the confirmed runtime head while the final push
        # checkpoint still holds the intermediate checkpoint's fixed lease.
        self.state.update_project('repo', publication_remote_oid='worker-head')
        self.assertTrue(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'worker-head', pending))
        for unowned_before in ('remote', 'unrelated-head'):
            with self.subTest(unowned_before=unowned_before):
                changed = replace(pending, snapshot={**pending.snapshot,
                    'remote_before': unowned_before})
                self.assertFalse(self.handler._owned_checkpoint_remote(self.handler._context(),
                    self.state.row(), 'worker-head', changed))

    def test_publication_comparison_updates_only_selected_remote_tracking_ref(self):
        from loop_manager_internal.git.managed_rebase_adoption import selected_baseline
        self.complete()
        branch, worktree = 'loop-manager/G-047', '/state/G-047'
        ref, remote_ref = f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'
        before = lm.GitSnapshot('before', branch, {}, {},
            {ref: ('before', ''), remote_ref: ('remote', ''), 'refs/heads/other': ('original', '')},
            {worktree: ('HEAD=before',)}, {}, {})
        current = replace(before, head='owned-head', refs={ref: ('owned-head', ''),
            remote_ref: ('owned-head', ''), 'refs/heads/other': ('unauthorized', '')},
            worktrees={worktree: ('HEAD=owned-head',)})
        published = self.checkpoints.get('repo', 'G-047')
        accepted = selected_baseline(before, current, branch, worktree, publication=published)
        self.assertEqual(accepted.refs[remote_ref], ('owned-head', ''))
        self.assertEqual(accepted.refs['refs/heads/other'], ('original', ''))
        self.assertEqual(before.refs[remote_ref], ('remote', ''))
        tampered = replace(current, refs={**current.refs, remote_ref: ('foreign', '')})
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            selected_baseline(before, tampered, branch, worktree, publication=published)

    def test_existing_remote_without_durable_publication_identity_is_preserved(self):
        self.state.update_project('repo', publication_remote_oid=None)
        # Seed the durable missing-identity state before rebase adoption owns
        # its transaction, rather than leaving a fixture transaction open.
        with self.fixture.connection:
            self.fixture.connection.execute("DELETE FROM blocked_checkpoints WHERE repo = 'repo'")
        self.assertFalse(self.fixture.connection.in_transaction)
        with self.assertRaises(handlers.RecoveryFailure):
            self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.expected_head, 'owned-head')
        self.assertNotIn('rebase_publication_intent', saved.snapshot)
        self.assertEqual(self.remote, 'remote')
        self.assertEqual(self.pushes, [])

    def test_legacy_completed_result_without_journal_or_completion_markers_publishes(self):
        current = self.checkpoints.get('repo', 'G-047')
        self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation=None, expected_head='owned-head', target_base_oid='base',
            observed_remote_oid='remote', snapshot={'failure': 'preserved legacy audit'})
        original_git = self.git
        def legacy_git(path, *args, **kwargs):
            if args == ('merge-base', '--is-ancestor', 'base', 'remote'):
                return SimpleNamespace(returncode=1, stdout='')
            return original_git(path, *args, **kwargs)
        self.handler.effects.workspace.git.side_effect = legacy_git
        self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        self.assertEqual(saved.snapshot['failure'], 'preserved legacy audit')
        self.assertEqual(len(self.pushes), 1)
        self.assertNotIn('completed_rebase', saved.snapshot)
        self.assertNotIn('rebase_completion', saved.snapshot)
