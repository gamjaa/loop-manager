from tests.support.lifecycle import fixture_cleanup
import sqlite3
from types import SimpleNamespace
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import blocked_remote_observation


class BlockedRemoteFixture:
    """Reusable setup and adapters; no discoverable test methods."""

    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        maintenance_state.ensure_schema(self.connection)
        self.repo, self.base, self.head = 'repo', 'a' * 40, 'b' * 40
        self.connection.execute("INSERT INTO projects (repo, base_branch, origin) VALUES ('repo', 'main', 'example/repo')")
        for plan_id in ('G-106', 'G-107'):
            self.connection.execute(
                'INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, '
                'content_hash, status, branch, worktree, workspace_owned, worker_session_id, '
                'verify_failure_history, effective_worker_level, priority) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (self.repo, plan_id, f'docs/plans/[{plan_id}] Work.md', 'Work', '[]', '[]', 'hash',
                 'IMPLEMENTING', f'loop-manager/{plan_id}', f'/state/{plan_id}', 1, 'session', '{"failure":3}', 6, 'critical'))
            runtime_state = loop_manager.runtime_state.RuntimeStateRepository(self.connection)
            runtime_state.block_plan(self.repo, plan_id, reason_code='verify_retry_exhausted',
                                     evidence='original verify failure', release_fingerprint='input',
                                     clear_projection=True)
            self.connection.execute('UPDATE exec_plans SET blocked_fingerprint = ? WHERE plan_id = ?',
                                    ('input', plan_id))
            self.record_observation(plan_id)
        self.signature = 'before'
        self.ports = mock.Mock(spec=list(blocked_plan_reconciliation.PlanRecoveryEffects.__dataclass_fields__))
        self.ports.unmet_execplan_dependencies.return_value = []
        self.ports.github_repository.return_value = 'example/repo'
        self.ports.pr_for_branch.return_value = None
        self.ports._blocked_execplan_metadata_error.return_value = None
        self.ports._worktree_execplan_input_fingerprint.return_value = 'input'
        self.ports._revision_execplan_input_fingerprint.return_value = 'input'
        self.ports._worktree_state_signature.side_effect = lambda _: self.signature
        self.ports.capture_git_snapshot.return_value = SimpleNamespace(
            head=self.head, dirty={}, index={}, operations={})
        self.ports.git.side_effect = self.git
        self.ports._recorded_base_fetch.side_effect = self.recorded_fetch
        self.ports._update_blocked_error.side_effect = loop_manager._update_blocked_error
        self.ports._evaluate_remote_checkpoint_cause.return_value = (False, 'same failure', 'verify')
        self.ports._current_rebase_service.return_value.require_owned.return_value = (
            SimpleNamespace(head=self.head), SimpleNamespace(status=SimpleNamespace(value='complete')))
        self.ports.observe_blocked_plan_remotes.side_effect = self.observe
        self.ref_probe = mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(
            blocked_remote_observation.RefObservation('present', self.base),
            blocked_remote_observation.RefObservation('absent')))
        self.ref_probe.start()
        self.addCleanup(self.ref_probe.stop)
        self.coordinator = blocked_plan_reconciliation.BlockedPlanReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main',
            timeout_seconds=600)

    def git(self, _, *args, **kwargs):
        output = ''
        if args[:2] == ('remote', 'get-url'):
            output = 'https://example.test/example/repo'
        elif args[:1] == ('rev-parse',):
            output = self.base if args[1] == 'FETCH_HEAD^{commit}' else self.head
        return SimpleNamespace(returncode=0, stdout=output)

    def recorded_fetch(self, repo_path, state_dir, base, *, owner):
        # The Git and journal adapter is a separate port. Preserve network
        # failures and FETCH_HEAD pinning in the coordinator's fake fetch.
        fetched = self.ports.git(repo_path, 'fetch', '--no-tags', '--refmap=', 'origin',
                                 f'refs/heads/{base}', check=False)
        if fetched.returncode:
            raise RuntimeError(fetched.stdout)
        return self.ports.git(repo_path, 'rev-parse', 'FETCH_HEAD^{commit}').stdout.strip()

    def record_observation(self, plan_id='G-106'):
        blocked_remote_observation.record(
            self.connection, repo=self.repo, plan_id=plan_id,
            registered_origin='example/repo', origin_url='https://example.test/example/repo',
            base_ref='refs/heads/main', branch_ref=f'refs/heads/loop-manager/{plan_id}',
            base=blocked_remote_observation.RefObservation('present', self.base),
            branch=blocked_remote_observation.RefObservation('absent'),
            blocked_reason='verify_retry_exhausted', blocked_fingerprint='input',
            project_blocked_reason=None, project_blocked_fingerprint=None,
            worktree_fingerprint=getattr(self, 'signature', 'before'))
        self.connection.commit()

    def observe(self, connection, repo_path, repo, project, *, plan_id):
        self.record_observation(plan_id)

    def plan(self, plan_id='G-106'):
        return self.connection.execute('SELECT * FROM exec_plans WHERE plan_id = ?', (plan_id,)).fetchone()

    def row(self):
        return self.connection.execute("SELECT * FROM blocked_remote_observations WHERE plan_id = 'G-106'").fetchone()


class ProjectBlockedRemoteFixture(BlockedRemoteFixture):
    """Reusable setup and adapters; no discoverable test methods."""

    def setUp(self):
        from loop_manager_internal.recovery import project_blocked_recovery
        self.recovery = project_blocked_recovery
        BlockedRemoteFixture.setUp(self)
        self.connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence, previous_phase) "
            "VALUES ('repo', 'project', 'publication_ownership_uncertain', 'G-106', 'original project cause', 'PLAN_BLOCKED')")
        self.connection.execute("UPDATE projects SET phase = 'PROJECT_BLOCKED', last_error = 'original project cause'")
        self.connection.commit()
        self.ports.mock_add_spec(list(set(blocked_plan_reconciliation.PlanRecoveryEffects.__dataclass_fields__) | set(project_blocked_recovery.ProjectRecoveryEffects.__dataclass_fields__)))
        self.ports._BlockedCheckpointAdapter.return_value._origin_safety_error.return_value = None
        self.coordinator = self.recovery.ProjectBlockedReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main')


def project_blocked_remote_fixture(test):
    fixture = fixture_cleanup(test, ProjectBlockedRemoteFixture())
    fixture.setUp()
    return fixture


from dataclasses import replace

from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.git import managed_rebase_validation

class PlanRemoteRecoveryFixture(BlockedRemoteFixture):
    def published_successor(self):
        branch, worktree = self.plan()['branch'], self.plan()['worktree']
        remote = 'c' * 40
        original = loop_manager.GitSnapshot(self.head, branch, {}, {},
            {f'refs/heads/{branch}': (self.head, '')},
            {worktree: (f'HEAD={self.head}', f'branch=refs/heads/{branch}')}, {}, {})
        self.current_snapshot = original
        self.connection.execute(
            "UPDATE exec_plans SET agent_role = 'worker', agent_snapshot = ?, agent_result_snapshot = ? "
            "WHERE plan_id = 'G-106'", (original.serialize(), original.serialize()))
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        repository.finish_published(repository.get(self.repo, 'G-106'), self.head)
        self.connection.execute("UPDATE blocked_remote_observations SET branch_result = 'present', branch_oid = ? WHERE plan_id = 'G-106'", (remote,))
        self.connection.commit()
        fetched = [self.base]
        self.fail_branch_fetch = False
        def git(path, *args, **kwargs):
            if args[:1] == ('fetch',):
                fetched[0] = self.base if args[-1] == 'refs/heads/main' else remote
                if fetched[0] == remote and self.fail_branch_fetch:
                    self.fail_branch_fetch = False
                    return SimpleNamespace(returncode=1, stdout='temporary fetch failure')
            if args[:1] == ('rev-parse',) and args[1] == 'FETCH_HEAD^{commit}':
                return SimpleNamespace(returncode=0, stdout=fetched[0])
            if 'merge' in args:
                self.head = remote
                self.current_snapshot = replace(original, head=remote,
                    index={self.plan()['path']: ('H 100644 blob 0',)},
                    refs={**self.current_snapshot.refs, f'refs/heads/{branch}': (remote, ''),
                          f'refs/remotes/origin/{branch}': (remote, '')},
                    worktrees={worktree: (f'HEAD={remote}', f'branch=refs/heads/{branch}')})
            return self.git(path, *args, **kwargs)
        self.ports.git.side_effect = git
        self.ports._non_default_index_flags.side_effect = loop_manager._non_default_index_flags
        self.ports.capture_git_snapshot.side_effect = lambda *_: self.current_snapshot
        self.ports.validate_blocked_agent_scope.side_effect = loop_manager.validate_blocked_agent_scope
        self.ports._reconcile_published_blocked_checkpoint.side_effect = loop_manager._reconcile_published_blocked_checkpoint
        self.ports.owned_remote_oid.return_value = remote
        adapter = mock.Mock()
        adapter.observe_remote.return_value = remote
        adapter._approved_guidance_targets.return_value = ()
        for patch in (
            mock.patch.object(loop_manager, 'git', side_effect=git),
            mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current_snapshot),
            mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
            mock.patch.object(loop_manager, 'validate_managed_worktree'),
            mock.patch.object(loop_manager, 'github_repository', return_value='example/repo'),
            mock.patch.object(loop_manager, '_BlockedCheckpointAdapter', return_value=adapter),
            # Both facade callers and the extracted observation service use this Git port.
            mock.patch.object(loop_manager, 'git_bytes', return_value=b''),
            mock.patch.object(loop_manager, '_blocked_execplan_revision_metadata_error', return_value=None),
            mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False),
            mock.patch.object(loop_manager, '_proven_manager_base_refs', return_value=frozenset()),
            mock.patch.object(loop_manager, '_proven_merged_cleanup_paths', return_value=frozenset()),
            mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=(
                blocked_remote_observation.RefObservation('present', self.base),
                blocked_remote_observation.RefObservation('present', remote))),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return original


    def guidance_tracking_updates(self):
        original = self.published_successor()
        branch = self.plan()['branch']
        original = replace(original, refs={**original.refs, 'refs/remotes/origin/main': ('old-base', '')})
        self.current_snapshot = replace(original, refs={**original.refs,
            'refs/remotes/origin/main': (self.base, ''),
            f'refs/remotes/origin/{branch}': (original.head, '')})
        self.connection.execute(
            "UPDATE exec_plans SET agent_snapshot = ?, agent_result_snapshot = ?, "
            "blocked_reason = ?, publication_remote_oid = ? WHERE plan_id = 'G-106'",
            (original.serialize(), original.serialize(), loop_manager.GUIDANCE_BLOCK_REASON, original.head))
        self.connection.commit()
        patch = mock.patch.object(loop_manager.checkpoint_ownership, 'owned_remote_oid',
                                  return_value=original.head)
        proof = patch.start()
        self.addCleanup(patch.stop)
        fetch = mock.patch.object(loop_manager, '_proven_manager_base_refs',
            side_effect=lambda _connection, _path, _repo, _plan, before, current:
                frozenset({'refs/remotes/origin/main'})
                if (before.refs.get('refs/remotes/origin/main') == ('old-base', '')
                    and current.refs.get('refs/remotes/origin/main') == (self.base, '')) else frozenset())
        fetch.start()
        self.addCleanup(fetch.stop)
        return original, proof


    def interrupt_after_fast_forward(self):
        original = self.published_successor()
        with mock.patch.object(blocked_checkpoint_state.BlockedCheckpointRepository,
                               'record_remote_inspection', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator.reconcile('G-106')
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-106')
        self.assertNotIn('remote_adoption_scope', checkpoint.snapshot)
        self.assertEqual(checkpoint.snapshot['remote_adoption_intent']['head'], self.head)
        self.assertEqual(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints WHERE plan_id = 'G-106'").fetchone()[0], self.head)
        return original


    def with_previous_rebase_acceptance(self, scenario):
        successor = self.published_successor
        saved = {}
        def prepare():
            original = successor()
            repository = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
            current = repository.get(self.repo, 'G-106')
            acceptance = dict(kind=managed_rebase_validation.ACCEPTANCE_KIND,
                policy_version=managed_rebase_validation.POLICY_VERSION, repo=self.repo,
                repository_identity='example/repo', plan_id='G-106', plan_path=self.plan()['path'],
                worktree=self.plan()['worktree'], branch=self.plan()['branch'], target_base_oid=self.base,
                remote=dict(repository='example/repo', branch=self.plan()['branch'],
                            oid=original.head, pull_request=None), head=original.head,
                generation=current.generation, role='worker', input_fingerprint='input',
                scope_fingerprint='scope', state_fingerprint='state')
            acceptance['id'] = managed_rebase_validation.fingerprint(acceptance)
            self.assertTrue(managed_rebase_validation.valid_acceptance(acceptance))
            repository.record(self.repo, 'G-106', expected_generation=current.generation,
                operation=None, expected_head=original.head, target_base_oid=self.base,
                observed_remote_oid=original.head,
                snapshot={**(current.snapshot or {}), 'current_rebase_acceptance': acceptance})
            loop_manager.managed_rebase_adoption.remember_agent_baseline(
                repository, self.repo, 'G-106', original, original, 'worker')
            saved.update(repository.get(self.repo, 'G-106').snapshot)
            return original
        with mock.patch.object(self, 'published_successor', side_effect=prepare):
            scenario()
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-106')
        for key in ('current_rebase_acceptance', 'current_rebase_agent_baseline'):
            self.assertEqual(checkpoint.snapshot[key], saved[key])


    def persisted_adoption_scope(self):
        # Exercise the exact pre-reconciler gate used by _run_project_step.
        with mock.patch.object(Path, 'is_dir', return_value=True), \
             mock.patch.object(loop_manager, '_config_entries', return_value={}), \
             mock.patch.object(loop_manager, '_normalize_legacy_ignored_snapshot', side_effect=lambda path, before, current: before):
            return loop_manager.validate_persisted_agent_scope(
                self.connection, Path('/repo'), Path('/state'), self.repo)


    def _assert_occupied_project_restart_validates_pending_adoption_before_reconcile(self):
        original = self.interrupt_after_fast_forward()
        loop_manager.hydrate_execplan_project(self.connection, self.repo, self.plan())
        self.assertEqual(self.persisted_adoption_scope().head, self.head)
        self.connection.execute("UPDATE projects SET registered = 1 WHERE repo = ?", (self.repo,))
        # Stop immediately after the real project-entry persisted scope gate.
        # All repository and process effects remain fake unit ports.
        class RepositoryGateReached(Exception):
            pass
        with (mock.patch.object(Path, 'expanduser', autospec=True, side_effect=lambda path:
                  SimpleNamespace(resolve=lambda **kwargs: Path(self.repo)) if str(path) == self.repo else path),
              mock.patch.object(Path, 'is_dir', return_value=True),
              mock.patch.object(loop_manager, '_check_runtime_handoff'),
              mock.patch.object(loop_manager, '_self_runtime_project', return_value=None),
              mock.patch.object(loop_manager, 'observe_blocked_plan_remotes'),
              mock.patch.object(loop_manager, '_config_entries', return_value={}),
              mock.patch.object(loop_manager, '_normalize_legacy_ignored_snapshot', side_effect=lambda path, before, current: before),
              mock.patch.object(loop_manager, 'validate_repository', side_effect=RepositoryGateReached),
              mock.patch.object(loop_manager, 'block_persisted_agent_scope_violation') as block):
            with self.assertRaises(RepositoryGateReached):
                loop_manager._run_project_step(self.connection, Path('/state'), self.repo,
                    Path('/config'), None, False, registered_only=True)
            block.assert_not_called()
        with mock.patch.object(self.coordinator, '_publish'):
            self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.plan()['agent_snapshot'], original.serialize())


    def _assert_occupied_restart_pending_adoption_rejects_unowned_ref(self):
        self.interrupt_after_fast_forward()
        loop_manager.hydrate_execplan_project(self.connection, self.repo, self.plan())
        self.current_snapshot = replace(self.current_snapshot,
            refs={**self.current_snapshot.refs, 'refs/heads/unowned': ('foreign', '')})
        with self.assertRaises(loop_manager.AgentScopeError):
            self.persisted_adoption_scope()


class ProjectRemoteRecoveryFixture(ProjectBlockedRemoteFixture):
    def published_project_successor(self, *, plan_block=False):
        original = PlanRemoteRecoveryFixture.published_successor(self)
        if not plan_block:
            scoped_blocking.clear_plan_block(self.connection, self.repo, 'G-106')
            self.connection.execute(
                "UPDATE exec_plans SET status = 'IMPLEMENTING', blocked_reason = NULL, "
                "blocked_fingerprint = NULL WHERE plan_id = 'G-106'")
        self.connection.execute("UPDATE project_blocks SET reason_code = 'repository_state_unsafe'")
        self.previous_base = 'd' * 40
        self.connection.execute("UPDATE exec_plans SET active_sha = ? WHERE plan_id = 'G-106'",
                                (self.previous_base,))
        self.connection.commit()
        self.ports.owned_remote_oid.side_effect = lambda *args, **kwargs: args[6]
        self.ports._current_rebase_service.side_effect = self.current_service
        self.ports.observe_blocked_plan_remotes.side_effect = None
        self.adoption_scope_targets = []
        for patch in (
            mock.patch.object(loop_manager, '_current_rebase_service', side_effect=self.current_service),
            mock.patch.object(self.recovery, 'observe_current_rebase', side_effect=self.adoption_observation),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        return original


    def adoption_observation(self, expected, adapters):
        snapshot = self.current_snapshot
        self.adoption_scope_targets.append((expected.target_base_oid, snapshot.head))
        observed = SimpleNamespace(
            remote=expected.remote, operations=snapshot.operations, metadata=None,
            branch=snapshot.branch, head=snapshot.head,
            branch_ref=snapshot.refs[f'refs/heads/{snapshot.branch}'],
            branch_worktrees=(self.plan()['worktree'],),
            registration=snapshot.worktrees[self.plan()['worktree']],
            index=(), files=(), staged=())
        return observed, SimpleNamespace(allowed=True)


    def diagnostic_context(self):
        plan = self.plan()
        block = scoped_blocking.project_block(self.connection, self.repo)
        return dict(plan_id='G-106', diagnostic=True, base_oid=self.base, branch_oid=None,
                    block=dict(scoped_blocking.plan_block(self.connection, self.repo, 'G-106') or block),
                    project_block=dict(block), cause_identity=scoped_blocking.project_cause_identity(block))


    def complete_context(self):
        context = self.diagnostic_context()
        cause = scoped_blocking.plan_block(self.connection, self.repo, 'G-106')
        context.update(status=self.plan()['status'], plan_block=dict(cause) if cause else None,
                       identity={key: self.plan()[key] for key in blocked_plan_reconciliation.IDENTITY_FIELDS},
                       artifacts={key: self.plan()[key] for key in blocked_plan_reconciliation.ARTIFACT_FIELDS},
                       registered_origin='example/repo', origin_url='https://example.test/example/repo',
                       base_ref='refs/heads/main', branch_ref='refs/heads/loop-manager/G-106', input_identity='input')
        return context


    def current_service(self, *args, **kwargs):
        checkpoints = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        current = checkpoints.get(self.repo, 'G-106')
        acceptance = dict(
            kind=managed_rebase_validation.ACCEPTANCE_KIND, policy_version=managed_rebase_validation.POLICY_VERSION,
            repo=self.repo, repository_identity='/repo/.git', plan_id='G-106', plan_path=self.plan()['path'],
            worktree=self.plan()['worktree'], branch=self.plan()['branch'], target_base_oid=kwargs.get('target', self.base),
            remote=dict(repository=self.repo, branch=self.plan()['branch'], oid=kwargs.get('remote_oid'), pull_request=None),
            head=self.head, generation=current.generation, role='worker', input_fingerprint='input',
            scope_fingerprint='scope', state_fingerprint='state')
        acceptance['id'] = managed_rebase_validation.fingerprint(acceptance)
        decision = SimpleNamespace(status=SimpleNamespace(value='complete'), acceptance=acceptance)
        observed = SimpleNamespace(head=self.head, files=(), staged=())
        service = mock.Mock()
        service.expected.remote = SimpleNamespace(oid=kwargs.get('remote_oid'), pull_request=None)
        service.expected.target_base_oid = acceptance['target_base_oid']
        service.require_owned.return_value = observed, decision
        service.observe.return_value = observed, decision
        return service


    def confirmed_original_checkpoint(self):
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        original = 'c' * 40
        acceptance = self.current_service().require_owned()[1].acceptance
        acceptance['remote']['oid'] = original
        acceptance['id'] = managed_rebase_validation.fingerprint(
            {key: value for key, value in acceptance.items() if key != 'id'})
        current = checkpoints.accept_current_rebase(self.repo, 'G-106', acceptance=acceptance)
        current = checkpoints.begin_rebase_publication(
            self.repo, 'G-106', expected_generation=current.generation,
            acceptance=acceptance, remote_before=original)
        published = self.current_service().require_owned()[1].acceptance
        published['remote']['oid'] = self.head
        published['id'] = managed_rebase_validation.fingerprint(
            {key: value for key, value in published.items() if key != 'id'})
        checkpoints.finish_rebase_publication(
            self.repo, 'G-106', intent=current.rebase_publication_intent(), acceptance=published)
        self.connection.execute("UPDATE exec_plans SET publication_remote_oid = ? WHERE plan_id = 'G-106'", (self.head,))
        self.connection.execute("UPDATE blocked_remote_observations SET branch_oid = ?, branch_result = 'present' WHERE plan_id = 'G-106'", (original,))
        self.connection.commit()
        return checkpoints, original
