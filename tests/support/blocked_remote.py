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
        self.ports = mock.Mock(spec=loop_manager)
        self.ports.GUIDANCE_BLOCK_REASON = loop_manager.GUIDANCE_BLOCK_REASON
        self.ports.PR_CLOSED_BLOCK_REASON = loop_manager.PR_CLOSED_BLOCK_REASON
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
        self.ports._BlockedCheckpointAdapter.return_value._origin_safety_error.return_value = None
        self.coordinator = self.recovery.ProjectBlockedReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main')


def project_blocked_remote_fixture(test):
    fixture = fixture_cleanup(test, ProjectBlockedRemoteFixture())
    fixture.setUp()
    return fixture
