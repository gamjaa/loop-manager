from tests.support.execution import VALID_WORKER_RESULT
from tests.support.lifecycle import fixture_cleanup
from tests.support.execution import execution_fixture
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import loop_manager as lm
from unittest import mock
from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage import blocked_checkpoint_state


class CurrentRebaseFixture:
    """Reusable setup and adapters; no discoverable test methods."""

    def setUp(self):
        from loop_manager_internal.git.managed_rebase_adoption import ManagedRebaseService
        from tests.support.rebase_observations import expectation, observation, scope_for
        self.fixture = execution_fixture(self)
        self.handler, self.state = self.fixture.handler, self.fixture.state
        self.handler._current_rebase_service = handlers.ExecutionHandlers._current_rebase_service.__get__(self.handler)
        self.checkpoint = ExecutionCheckpointRepository(self.fixture.connection)
        self.state.update_project('repo', worktree=str(Path('/state/G-047').resolve()),
                                  worker_result=VALID_WORKER_RESULT,
                                  worker_session_id='saved-worker', verify_result='old verify',
                                  review_result='old review', worker_input_token='old-input')
        self.checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base', observed_remote_oid='remote',
            snapshot={'pending': 'stage', 'stage_evidence': {'old': 'audit'},
                      'failure': 'old failure'})
        self.expected = expectation(repo='repo', plan_id='G-047',
                                    plan_path='docs/plans/[G-047] Runtime.md',
                                    worktree=str(Path('/state/G-047').resolve()), branch='loop-manager/G-047')
        self.expected = replace(self.expected, remote=replace(
            self.expected.remote, branch='loop-manager/G-047'))
        self.observed = observation(self.expected, head='owned-head',
                                    branch_ref=('owned-head', ''),
                                    registration=('HEAD=owned-head', 'branch=refs/heads/loop-manager/G-047'))
        def factory(context, **options):
            current = self.checkpoint.get('repo', 'G-047')
            expected = replace(self.expected,
                               generation=current.generation)
            if current.operation != 'rebase':
                row = self.fixture.connection.execute(
                    "SELECT publication_remote_oid FROM exec_plans "
                    "WHERE repo = 'repo' AND plan_id = 'G-047'").fetchone()
                expected = replace(expected, remote=replace(expected.remote,
                    oid=row['publication_remote_oid'] or current.observed_remote_oid))
            if 'remote_oid' in options:
                expected = replace(expected, remote=replace(expected.remote, oid=options['remote_oid']))
            return ManagedRebaseService(expected, mock.Mock())
        self.factory = factory
        self.handler.effects = replace(self.fixture.effects, checkpoint=self.checkpoint,
            workspace=replace(self.fixture.effects.workspace, current_rebase_service=factory))
        self.observation_patch = mock.patch('loop_manager_internal.git.managed_rebase_adoption.observe_current_rebase',
            side_effect=lambda expected, _: (self.observed, scope_for(expected, self.observed)))
        self.observation_patch.start()
        self.addCleanup(self.observation_patch.stop)
        self.handler._complete_rebase_intent = handlers.ExecutionHandlers._complete_rebase_intent.__get__(self.handler)
        # Isolate current-result adoption from the independently tested remote
        # publication boundary. These observations use synthetic Git OIDs.
        self.handler._publish_rebased_checkpoint = mock.Mock()


    def _prepare_current_checkpoint_recovery(self):
        self.expected = replace(self.expected, remote=replace(self.expected.remote, repository='repo'))
        self.observed = replace(self.observed, remote=self.expected.remote)
        blocked_checkpoint_state.ensure_schema(self.fixture.connection)
        self.fixture.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', 'evidence', "
            "'loop-manager/G-047', ?, 'remote')", (str(Path('/state/G-047').resolve()),))
        self.state.update_project('repo', publication_remote_oid='remote')
        self.fixture.effects.workspace._rebase_in_progress_for_branch.return_value = False
        self.fixture.effects.workspace.git_operation_entries.return_value = {}
        self.fixture.effects.workspace.remote_branch_oid.return_value = 'remote'
        self.fixture.effects.publication.pr_for_branch.return_value = None
        self.fixture.effects.workspace.git.side_effect = self.fixture.observed_git
        self.handler._fetch_latest_base = mock.Mock(return_value='base')
        # These unit observations use symbolic OIDs. Fake the live-ref process
        # boundary as well as the fetched-ref boundary, retaining real policy.
        live_remote = mock.patch.object(handlers.manager_git_journal, 'live_remote_branch_oid',
                                       side_effect=lambda *_: self.observed.remote.oid)
        live_remote.start()
        self.addCleanup(live_remote.stop)
        self.owner_patch = mock.patch.object(handlers.checkpoint_ownership,
                                            'owned_remote_oid', return_value='remote')
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)


def current_rebase_case(test):
    fixture = fixture_cleanup(test, CurrentRebaseFixture())
    fixture.setUp()
    return fixture


class CurrentRebaseAgentScopeFixture:
    """Shared accepted-rebase scope setup, with no discoverable test methods."""
    def setUp(self):
        self.case = current_rebase_case(self)
        self.case.handler._complete_rebase_intent(self.case.fixture.context())
        branch, worktree = 'loop-manager/G-047', str(Path('/state/G-047').resolve())
        self.original = lm.GitSnapshot('before', branch, {}, {},
            {f'refs/heads/{branch}': ('before', ''), 'refs/heads/other': ('other', '')},
            {worktree: ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
        self.current = replace(self.original, head='owned-head',
            refs={**self.original.refs, f'refs/heads/{branch}': ('owned-head', '')},
            worktrees={worktree: ('HEAD=owned-head', f'branch=refs/heads/{branch}')})
        self.case.state.update_project('repo', agent_role='worker',
            agent_snapshot=self.original.serialize())
        for patch in (
            mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **_k: self.case.factory(None)),
            mock.patch.object(lm, 'capture_git_snapshot', side_effect=lambda *_a: self.current),
            mock.patch.object(lm, '_config_entries', return_value={}),
            mock.patch.object(lm, '_proven_merged_cleanup_paths', return_value=frozenset()),
            mock.patch.object(Path, 'is_dir', return_value=True),
            mock.patch.object(lm, 'git', return_value=SimpleNamespace(stdout='', returncode=0)),
            mock.patch.object(lm, 'git_bytes', return_value=b''),
        ):
            patch.start()
            self.addCleanup(patch.stop)
