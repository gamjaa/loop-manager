"""Scope service contracts using real repositories and synthetic Git evidence."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sqlite3
import tempfile
from unittest import TestCase, mock

import loop_manager as manager
from loop_manager_internal.agents import scope_adapter, scope_baseline, scope_lifecycle, scope_observation, scope_recovery, scope_state_service, scope_validation
from loop_manager_internal.agents.scope_contracts import AgentScopeError, GitSnapshot
from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence
from loop_manager_internal.agents import agent_scope_decision, common_guidance
from loop_manager_internal.recovery import blocked_plan_reconciliation, blocked_remote_observation
from loop_manager_internal.storage import agent_invocation_state, blocked_checkpoint_state, execution_checkpoint_state, runtime_state
from tests.support.database import runtime_connection
from tests.support.git import git_command
from tests.support.fixed_samples import PROJECT_FILES


def snapshot(**changes):
    return replace(GitSnapshot('head', 'loop-manager/G-047', {}, {}, {}, {}, {}, {}), **changes)


class ScopeLifecycleTests(TestCase):
    def setUp(self):
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.repository = runtime_state.RuntimeStateRepository(self.connection)
        self.capture = mock.Mock(return_value=snapshot())
        self.baseline = mock.Mock(return_value=None)
        self.effects = replace(manager._scope_lifecycle_effects(),
            capture_git_snapshot=self.capture, _capture_agent_baseline=self.baseline,
            _owned_rebase_state_dir=lambda *_: None)

    def begin(self, role='worker', **kwargs):
        return scope_lifecycle.begin_agent_scope(self.connection, 'repo', Path('/managed'), role,
                                                effects=self.effects, **kwargs)

    def test_new_scope_binds_start_digest_and_preserves_projection(self):
        self.connection.execute("UPDATE projects SET plan_path = 'docs/plans/[G-047] Runtime.md'")
        observed = self.begin(input_fingerprint='remote-input')
        owner = self.repository.scope_owner('repo')
        plan = self.repository.scope_owner('repo', 'G-047')
        self.assertEqual(len(observed.scope_id), 32)
        self.assertEqual(observed.scope_start_digest,
                         hashlib.sha256(snapshot().serialize().encode()).hexdigest())
        self.assertEqual(owner['agent_snapshot'], plan['agent_snapshot'])
        self.assertEqual(owner['agent_input_fingerprint'], 'remote-input')
        self.assertEqual(GitSnapshot.deserialize(owner['agent_snapshot']).scope_id, observed.scope_id)
        self.capture.assert_called_once()

    def test_baseline_ref_is_recaptured_without_accepting_local_race(self):
        self.baseline.return_value = 'saved-local-tree'
        self.capture.side_effect = [snapshot(), snapshot(refs={'refs/loop-manager/baselines/a': ('b', '')})]
        observed = self.begin()
        self.assertIn('refs/loop-manager/baselines/a', observed.refs)
        self.assertEqual(self.repository.scope_owner('repo')['agent_baseline'], 'saved-local-tree')
        scope_lifecycle.clear_agent_scope(self.connection, 'repo', effects=self.effects)
        self.capture.side_effect = [snapshot(), snapshot(index={'app.py': ('H 100644 b 0',)})]
        with self.assertRaisesRegex(RuntimeError, 'worktree가 변경'):
            self.begin()
        self.assertIsNone(self.repository.scope_owner('repo')['agent_snapshot'])

    def test_resume_keeps_original_scope_and_refreshes_worker_input(self):
        original = self.begin(input_fingerprint='old-input')
        self.capture.reset_mock()
        self.baseline.reset_mock()
        resumed = self.begin(input_fingerprint='current-input')
        self.assertEqual(resumed.serialize(), original.serialize())
        self.assertEqual(self.repository.scope_owner('repo')['agent_input_fingerprint'], 'current-input')
        self.capture.assert_not_called()
        self.baseline.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, '역할'):
            self.begin(role='reviewer')

    def test_reviewer_input_plan_is_saved_and_clear_removes_only_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            plan = worktree / 'selected.md'
            plan.write_text('original plan', encoding='utf-8')
            self.connection.execute("UPDATE projects SET plan_path = 'selected.md', reviewer_session_id = 'kept'")
            scope_lifecycle.begin_agent_scope(self.connection, 'repo', worktree, 'reviewer', effects=self.effects)
            self.assertEqual(self.repository.scope_owner('repo')['agent_input_plan'], 'original plan')
            scope_lifecycle.clear_agent_scope(self.connection, 'repo', effects=self.effects)
            row = self.repository.scope_owner('repo')
            self.assertIsNone(row['agent_input_plan'])
            self.assertIsNone(row['agent_snapshot'])
            self.assertEqual(row['reviewer_session_id'], 'kept')

    def test_resume_records_manager_baseline_bound_to_original_digest(self):
        original = self.begin()
        changed = replace(original, refs={'refs/remotes/origin/main': ('new-base', '')})
        effects = replace(self.effects, _reconciled_manager_scope_metadata=lambda *_: changed,
                          _agent_git_resources=lambda *_args, **_kwargs: None)
        resumed = scope_lifecycle.begin_agent_scope(self.connection, 'repo', Path('/managed'), 'worker',
                                                   effects=effects, state_dir=Path('/state'))
        self.assertEqual(resumed.refs, changed.refs)
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', '')
        proof = saved.snapshot['manager_metadata_agent_baseline']
        self.assertEqual(proof['role'], 'worker')
        self.assertEqual(proof['original'], hashlib.sha256(original.serialize().encode()).hexdigest())
        self.assertEqual(self.repository.scope_owner('repo')['agent_snapshot'], original.serialize())


class ScopeObservationTests(TestCase):
    def setUp(self):
        self.git = mock.Mock(return_value=subprocess.CompletedProcess([], 0, ''))
        self.git_bytes = mock.Mock(return_value=b'')
        self.catalog = mock.Mock(return_value=[])
        self.process = mock.Mock()
        self.effects = scope_observation.ObservationEffects(
            self.git, self.git_bytes, self.catalog, self.process, self.process)

    def test_index_parser_preserves_conflict_stages_flags_and_path_bytes(self):
        self.git_bytes.return_value = b'M 100644 aaa 1\tfile name\0M 100644 bbb 3\tfile name\0s 100644 ccc 0\tbad-\xff\0'
        parsed = scope_observation._index_entries(Path('/managed'), effects=self.effects)
        self.assertEqual(parsed['file name'], ('M 100644 aaa 1', 'M 100644 bbb 3'))
        self.assertIn('bad-\udcff', parsed)
        self.git_bytes.return_value = b'missing-tab\0'
        with self.assertRaisesRegex(RuntimeError, 'index snapshot'):
            scope_observation._index_entries(Path('/managed'), effects=self.effects)

    def test_ref_and_registration_parsers_fail_closed_on_duplicate_identity(self):
        self.git.return_value.stdout = 'refs/heads/main\0abc\0\nrefs/heads/main\0def\0\n'
        with self.assertRaisesRegex(RuntimeError, '중복 ref'):
            scope_observation._shared_ref_entries(Path('/managed'), effects=self.effects)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.catalog.return_value = [{'worktree': str(path)}, {'worktree': str(path / '.') }]
            with self.assertRaisesRegex(RuntimeError, '중복 경로'):
                scope_observation._registered_worktree_entries(path, effects=self.effects)

    def test_empty_global_excludes_does_not_hash_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            self.git_bytes.side_effect = lambda _path, *args: (
                b'local\0file:config\0core.excludesfile\n\0' if args[0] == 'config' else b'exclude')
            self.process.return_value = subprocess.CompletedProcess([], 0, b'\n')
            before = scope_observation._config_entries(worktree, effects=self.effects)
            (worktree / 'new.py').write_text('changed', encoding='utf-8')
            after = scope_observation._config_entries(worktree, effects=self.effects)
            self.assertEqual(before, after)
            self.assertEqual(after['ignore-source:global-exclude'], ('', 'disabled'))

    def test_config_parser_keeps_origins_and_exclude_file_content_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            exclude = worktree / 'exclude'
            exclude.write_text('old-rule', encoding='utf-8')
            records = b'local\0file:config\0core.ignorecase\nfalse\0'
            self.git_bytes.side_effect = lambda _path, *args: records if args[0] == 'config' else b'exclude'
            self.process.return_value = subprocess.CompletedProcess([], 1, b'')
            first = scope_observation._config_entries(worktree, effects=self.effects)
            exclude.write_text('new-rule', encoding='utf-8')
            second = scope_observation._config_entries(worktree, effects=self.effects)
            self.assertEqual(first['local:core.ignorecase'], second['local:core.ignorecase'])
            self.assertNotEqual(first['ignore-source:repository-exclude'], second['ignore-source:repository-exclude'])
            self.git_bytes.side_effect = None
            self.git_bytes.return_value = b'local\0missing-field\0'
            with self.assertRaisesRegex(RuntimeError, 'config snapshot'):
                scope_observation._config_entries(worktree, effects=self.effects)


class ScopeValidationTests(TestCase):
    def setUp(self):
        self.effects = replace(manager._scope_validation_effects(),
            _scope_snapshot_with_start_ignore_rules=lambda _path, _before, after: after,
            agent_snapshot_changes=lambda path, before, after: scope_observation.agent_snapshot_changes(
                path, before, after, effects=manager._scope_observation_effects()))

    def validate(self, role, before, after, **kwargs):
        return scope_validation.validate_agent_scope(role, Path('/managed'), before, after,
            selected_plan_path='docs/plans/[G-047] Runtime.md', effects=self.effects, **kwargs)

    def test_blocked_scope_validation_captures_only_for_changed_candidate(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        current = snapshot(config={'local:remote.origin.url': ('origin',)})
        capture = mock.Mock(return_value=current)
        effects = replace(manager._scope_recovery_effects(), capture_git_snapshot=capture)

        validated = scope_state_service.validate_blocked_agent_scope(
            scope_state_service.ValidateBlockedAgentScopeEffects(
                _scope_recovery_effects=lambda: effects,
            ),
            connection, Path('/repo'), 'repo', 'main', Path('/managed'),
            'docs/plans/[G-047] Runtime.md',
        )

        self.assertIsNone(validated)
        capture.assert_not_called()
        validated = scope_state_service.validate_blocked_agent_scope(
            scope_state_service.ValidateBlockedAgentScopeEffects(
                _scope_recovery_effects=lambda: effects,
            ),
            connection, Path('/repo'), 'repo', 'main', Path('/managed'),
            'docs/plans/[G-047] Runtime.md', changed_candidate=True,
        )

        self.assertEqual(validated, current)
        capture.assert_called_once_with(Path('/managed'))

    def test_reviewer_is_read_only_and_worker_protected_paths_stay_protected(self):
        for path in ('src/app.py', 'AGENTS.md', 'docs/ARCHITECTURE.md', 'docs/requirements/archive/change.md'):
            changed = snapshot(dirty={path: (('untracked',), 'content')})
            with self.subTest(path=path):
                with self.assertRaises(AgentScopeError):
                    self.validate('reviewer', snapshot(), changed)
                if path in {'src/app.py', 'AGENTS.md', 'docs/ARCHITECTURE.md'}:
                    self.validate('worker', snapshot(), changed)
                else:
                    with self.assertRaises(AgentScopeError):
                        self.validate('worker', snapshot(), changed)

    def test_shared_metadata_and_index_flags_remain_blocking(self):
        changed_states = (
            {'refs': {'refs/heads/other': ('new', '')}},
            {'worktrees': {'/outside': ('HEAD=new',)}},
            {'config': {'local:core.hooksPath': ('new',)}},
            {'operations': {'MERGE_HEAD': 'new'}},
            {'index': {'src/app.py': ('s 100644 oid 0',)}},
            {'shared_metadata_complete': False},
        )
        for changes in changed_states:
            with self.subTest(changes=changes), self.assertRaises(AgentScopeError):
                self.validate('worker', snapshot(), snapshot(**changes))

    def test_legacy_comparison_excludes_default_checkout_and_local_base_but_keeps_foreign_ref(self):
        before = snapshot(
            refs={'refs/heads/main': ('local-old', ''),
                  'refs/heads/unrelated': ('same', '')},
            worktrees={'/managed': ('HEAD=a',), '/source': ('HEAD=source-old', 'state=clean')},
        )
        source_changed = replace(before,
            refs={'refs/heads/main': ('local-new', ''),
                  'refs/heads/unrelated': ('same', '')},
            worktrees={'/managed': ('HEAD=a',), '/source': ('HEAD=source-new', 'state=dirty')},
        )
        self.validate('worker', before, source_changed,
            external_worktree=Path('/source'), base_branch='main')
        foreign_ref = replace(source_changed,
            refs={**source_changed.refs, 'refs/heads/unrelated': ('foreign', '')},
        )
        with self.assertRaisesRegex(AgentScopeError, '공유 Git refs'):
            self.validate('worker', before, foreign_ref,
                external_worktree=Path('/source'), base_branch='main')
        self.validate('worker', before, replace(before, worktrees={
            '/managed': ('HEAD=a',),
            '/source': ('HEAD=source-new', 'branch=refs/heads/user-local', 'state=dirty'),
        }), external_worktree=Path('/source'), base_branch='main')
        with self.assertRaises(AgentScopeError):
            self.validate('worker', before, replace(before, worktrees={
                '/managed': ('HEAD=a',),
                '/source': ('HEAD=source-old', 'state=clean', 'locked=maintenance'),
            }), external_worktree=Path('/source'), base_branch='main')
        with self.assertRaises(AgentScopeError):
            self.validate('worker', before, replace(before, worktrees={'/managed': ('HEAD=a',)}),
                external_worktree=Path('/source'), base_branch='main')

    def test_verify_scope_uses_current_worktree_resources_without_agent_ledger(self):
        branch = 'loop-manager/G-047'
        own_ref = f'refs/heads/{branch}'
        base_ref = 'refs/remotes/origin/main'
        head_ref = 'refs/remotes/origin/HEAD'
        before = snapshot(
            refs={base_ref: ('base', ''), head_ref: ('base', base_ref),
                  own_ref: ('head', ''), 'refs/heads/unrelated': ('old', '')},
            worktrees={'/managed': (f'HEAD=head', f'branch={own_ref}'),
                       '/source': ('HEAD=source', 'state=clean'),
                       '/other': ('HEAD=other',)},
        )
        current = replace(before,
            refs={**before.refs, 'refs/heads/unrelated': ('new', '')},
            worktrees={**before.worktrees, '/source': ('HEAD=changed', 'state=dirty'),
                       '/other': ('HEAD=changed',)},
        )
        effects = scope_adapter.ValidateWorkerWorkspaceScopeEffects(
            capture_git_snapshot=lambda _worktree: current,
            validate_active_execplan_metadata=mock.Mock(return_value={}),
            validate_agent_scope=lambda *args, **kwargs: scope_validation.validate_agent_scope(
                *args, effects=self.effects, **kwargs),
            validate_managed_worktree=mock.Mock(),
        )
        self.assertEqual(scope_adapter.validate_worker_workspace_scope(
            effects, Path('/source'), Path('/managed'), branch, 'main',
            'docs/plans/[G-047] Work.md', mock.Mock(), before,
            scope_phase='VERIFYING'), {})
        effects.validate_managed_worktree.assert_called_once()

    def test_independent_changes_require_full_invocation_proof(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        worktree = Path('/managed').resolve()
        bound = snapshot(scope_id='scope', scope_start_digest='a' * 64)
        connection.execute("UPDATE projects SET phase='WORKING', worktree=?, branch=?, "
                           "plan_path='docs/plans/[G-047] Runtime.md', "
                           "agent_role='worker', agent_snapshot=?, worker_session_id='session'",
                           (str(worktree), bound.branch, bound.serialize()))
        effects = replace(self.effects, _git_common_directory=lambda _: Path('/common'),
            git=lambda *_: subprocess.CompletedProcess([], 0, 'origin'),
            validate_managed_worktree=mock.Mock(), _rebase_in_progress_for_branch=lambda *_: False)
        self.assertIsNone(scope_validation._agent_git_resources(
            connection, Path('repo'), worktree, bound, 'worker', effects=effects))
        ledger = agent_invocation_state.InvocationRepository(connection)
        identity = ledger.start(InvocationContext('repo', 'G-047'), 'worker', 'codex',
            ResolvedProfile('model', 'high'), 'session', scope_id=bound.scope_id)
        digest = agent_scope_decision.invocation_scope_digest(
            bound, role='worker', phase='IMPLEMENTING', base_branch='main', branch=bound.branch,
            worktree_key=str(worktree), default_worktree=str(Path('repo').resolve()))
        ledger.record_isolation(identity, IsolationEvidence('codex', 'test', 'test', 'worker',
            str(worktree), hashlib.sha256(b'/common\0origin').hexdigest(), 'b' * 64, 'verified',
            snapshot_digest=digest, configuration_digest='c' * 64))
        ledger.finish(identity, 'success', 0, 1, 'session', Usage())
        resources = scope_validation._agent_git_resources(
            connection, Path('repo'), worktree, bound, 'worker', effects=effects)
        self.assertEqual(resources.worktrees, frozenset((str(worktree),)))
        changed = replace(bound, refs={'refs/heads/unrelated': ('new', '')})
        self.validate('worker', bound, changed, resources=resources)
        with self.assertRaises(AgentScopeError):
            self.validate('worker', bound, changed)
        effects.validate_managed_worktree.assert_called_once()


class ScopeInvocationDigestTests(TestCase):
    """Real preflight callback, SQLite ledger and verdicts; Git observations are synthetic."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.worktree = self.state / 'managed'
        self.worktree.mkdir()
        self.repo_path = Path('repo')
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.plan_path = 'docs/plans/[G-047] Runtime.md'
        self.common = Path('/common')
        self.repository_identity = hashlib.sha256(b'/common\0origin').hexdigest()
        patch = mock.patch.object(common_guidance, 'RUNTIME_ROOT', PROJECT_FILES)
        patch.start()
        self.addCleanup(patch.stop)
        self.validation = replace(manager._scope_validation_effects(),
            _git_common_directory=lambda _: self.common,
            git=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, 'origin'),
            validate_managed_worktree=lambda *_args, **_kwargs: None,
            operation_log=lambda *_args, **_kwargs: None,
            _rebase_in_progress_for_branch=lambda *_: False,
            _scope_snapshot_with_start_ignore_rules=lambda _w, _b, a: a)
        self.validation = replace(self.validation, _agent_git_resources=self.resources)

    def resources(self, *args, **kwargs):
        return scope_validation._agent_git_resources(*args, effects=self.validation, **kwargs)

    def validate(self, *args, **kwargs):
        return scope_validation.validate_agent_scope(*args, effects=self.validation, **kwargs)

    def begin(self, role):
        self.role = role
        self.session = role + '-session'
        self.provider_role = 'planner' if role == 'maintenance_planner' else role
        self.plan_id = 'G-047' if role in {'worker', 'reviewer'} else None
        branch = 'loop-manager/G-047' if self.plan_id else None
        phase = 'WORKING' if role == 'worker' else 'REVIEWING' if role == 'reviewer' else 'PLANNING'
        self.raw = snapshot(branch=branch,
            refs={'refs/remotes/origin/main': ('base', ''),
                  **({f'refs/heads/{branch}': ('head', '')} if branch else {})},
            worktrees={str(self.worktree.resolve()): ('HEAD=head',
                       f'branch=refs/heads/{branch}' if branch else 'detached'),
                       str(self.repo_path.resolve()): ('HEAD=base', 'state=clean')})
        self.current = self.raw
        self.connection.execute('UPDATE projects SET phase=?, branch=?, worktree=?, plan_path=?, '
            'agent_snapshot=NULL, agent_result_snapshot=NULL',
            (phase, branch, str(self.worktree), self.plan_path if self.plan_id else None))
        self.connection.execute('UPDATE exec_plans SET status=?, branch=?, worktree=?, '
            'agent_snapshot=NULL, agent_result_snapshot=NULL',
            ('IMPLEMENTING' if role == 'worker' else 'REVIEWING', branch, str(self.worktree)))
        lifecycle = replace(manager._scope_lifecycle_effects(),
            capture_git_snapshot=lambda _: self.raw, _capture_agent_baseline=lambda *_: None)
        self.before = scope_lifecycle.begin_agent_scope(
            self.connection, 'repo', self.worktree, role, effects=lifecycle)
        self.start_serialized = self.before.serialize()

    def invoke(self, *, session=None, kind='success', transition=None):
        outer = self
        class Provider:
            def run(self, request):
                digest = request.isolation_snapshot_digest()
                outer.assertEqual(digest, request.isolation_snapshot_digest())
                request.on_isolation_checked(IsolationEvidence(
                    'codex', 'test', 'test', request.role_name, str(request.worktree.resolve()),
                    outer.repository_identity, 'b' * 64, 'verified', snapshot_digest=digest,
                    configuration_digest='c' * 64))
                request.on_session_started(outer.session)
                return manager.AgentResult(0 if kind == 'success' else 1, 'done', outer.session,
                    manager.AgentResultKind(kind))
        with mock.patch.object(manager, 'capture_git_snapshot', side_effect=lambda _: self.current), \
             mock.patch.object(manager, 'agent_provider', return_value=Provider()):
            result = manager.run_agent('codex', self.worktree, self.state, Path('/config'),
                self.provider_role, 'prompt', read_only=self.role in {'reviewer', 'planner_check'},
                connection=self.connection, session_id=self.session if session else None,
                invocation=InvocationContext('repo', self.plan_id, transition=transition),
                resolved_profile=ResolvedProfile('model', 'high'))
        key = 'planner_session_id' if self.provider_role in {'planner', 'planner_check'} else self.role + '_session_id'
        # planner_check uses the scope ledger session, rather than a project field.
        if self.role != 'planner_check':
            self.connection.execute(f'UPDATE projects SET {key}=?', (self.session,))
            if self.plan_id:
                self.connection.execute(f'UPDATE exec_plans SET {key}=?', (self.session,))
        self.connection.commit()
        return result.isolation_invocation_id

    def independent_changes(self):
        self.current = replace(self.raw,
            refs={**self.raw.refs, 'refs/heads/manual/G-250': ('head', '')},
            worktrees={**self.raw.worktrees, '/manual/G-251': ('HEAD=head',)},
            config={'local:branch.manual/G-250.remote': ('origin',)})

    def check(self, current=None, before=None):
        self.validate(self.role, self.worktree, before or self.before, current or self.current,
            external_worktree=self.repo_path, base_branch='main', connection=self.connection,
            selected_plan_path=self.plan_path if self.plan_id else None)

    def test_preflight_to_verified_scope_all_roles_and_resumed_phase(self):
        for role in ('planner', 'worker', 'reviewer', 'planner_check', 'maintenance_planner'):
            with self.subTest(role=role):
                self.begin(role)
                identity = self.invoke()
                proof = self.connection.execute('SELECT snapshot_digest FROM agent_isolation_evidence '
                    'WHERE invocation_id=?', (identity,)).fetchone()[0]
                self.assertNotEqual(proof, self.before.scope_start_digest)
                self.assertEqual(self.before.scope_start_digest,
                    hashlib.sha256(self.raw.serialize().encode()).hexdigest())
                self.independent_changes()
                self.check()
                self.connection.execute("UPDATE projects SET phase='PLAN_BLOCKED'")
                self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED'")
                self.invoke(session='session', transition='resume')
                self.check()
                # Effective comparison baselines may carry Manager metadata;
                # verification must still hash the saved original start.
                self.check(before=replace(self.before, refs=self.current.refs))
                self.assertEqual(runtime_state.RuntimeStateRepository(self.connection)
                    .scope_owner('repo')['agent_snapshot'], self.start_serialized)
                changes = [replace(self.current, config={'local:core.hooksPath': ('evil',)}),
                           replace(self.current, worktrees={}),
                           replace(self.current, index={'app.py': ('s 100644 oid 0',)})]
                selected_ref = f'refs/heads/{self.before.branch}' if self.plan_id else 'refs/remotes/origin/main'
                changes.append(replace(self.current, refs={**self.current.refs, selected_ref: ('evil', '')}))
                for changed in changes:
                    with self.assertRaises(AgentScopeError):
                        self.check(changed)

    def test_invalid_proof_and_full_snapshot_fallback_remain_strict(self):
        self.begin('worker')
        identity = self.invoke()
        self.independent_changes()
        cases = [('agent_invocations', 'session_id', 'other'),
                 ('agent_invocations', 'repo', 'other'),
                 ('agent_invocations', 'plan_id', 'G-999'),
                 ('agent_isolation_evidence', 'repository_identity', 'd' * 64),
                 ('agent_isolation_evidence', 'canonical_worktree', '/other'),
                 ('agent_isolation_evidence', 'status', 'prepared'),
                 ('agent_isolation_evidence', 'provider', 'claude'),
                 ('agent_isolation_evidence', 'role', 'planner'),
                 ('agent_isolation_evidence', 'policy_digest', ''),
                 ('agent_isolation_evidence', 'configuration_digest', ''),
                 ('agent_isolation_evidence', 'snapshot_digest', self.before.scope_start_digest)]
        for table, field, value in cases:
            with self.subTest(field=field):
                old = self.connection.execute(f'SELECT {field} FROM {table} WHERE invocation_id=?', (identity,)).fetchone()[0]
                self.connection.execute(f'UPDATE {table} SET {field}=? WHERE invocation_id=?', (value, identity))
                with self.assertRaises(AgentScopeError):
                    self.check()
                self.connection.execute(f'UPDATE {table} SET {field}=? WHERE invocation_id=?', (old, identity))
        self.check()
        self.invoke(transition='fallback')
        self.check()
        # A failed physical attempt keeps the whole scope strict even if a
        # subsequent fallback succeeds; never manufacture missing success.
        self.invoke(session='session', kind='transient_failure')
        self.invoke(transition='fallback')
        with self.assertRaises(AgentScopeError):
            self.check()

    def test_canonical_paths_and_existing_digest_contract(self):
        # Equivalent path spellings use the same canonical invocation inputs.
        self.worktree = self.worktree / '..' / 'managed'
        self.repo_path = Path('.') / 'repo'
        self.begin('worker')
        identity = self.invoke()
        stored = self.connection.execute('SELECT snapshot_digest FROM agent_isolation_evidence '
            'WHERE invocation_id=?', (identity,)).fetchone()[0]
        # Pre-hotfix evidence was generated from an unbound live snapshot.
        # Keep accepting that exact serialization contract without rewriting it.
        expected = agent_scope_decision.invocation_scope_digest(
            self.raw, role='worker', phase='IMPLEMENTING', base_branch='main',
            branch=self.before.branch, worktree_key=str(self.worktree.resolve()),
            default_worktree=str(self.repo_path.resolve()))
        self.assertEqual(stored, expected)
        self.independent_changes()
        self.check()
        self.assertEqual(self.connection.execute('SELECT snapshot_digest FROM agent_isolation_evidence '
            'WHERE invocation_id=?', (identity,)).fetchone()[0], stored)

    def test_saved_result_resume_and_block_recovery_keep_real_proof(self):
        for role in ('planner', 'worker', 'reviewer'):
            with self.subTest(role=role):
                self.begin(role)
                identity = self.invoke()
                self.independent_changes()
                result = self.current.serialize()
                self.connection.execute("UPDATE projects SET phase='PLAN_BLOCKED', agent_result_snapshot=?", (result,))
                self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED', agent_result_snapshot=?", (result,))
                # Reload the persisted DB, rather than trusting in-memory proof.
                self.connection.commit()
                path = self.state / (role + '.sqlite')
                with sqlite3.connect(path) as saved:
                    self.connection.backup(saved)
                self.connection.close()
                self.connection = sqlite3.connect(path)
                self.connection.row_factory = sqlite3.Row
                self.addCleanup(self.connection.close)
                recovery = replace(manager._scope_recovery_effects(),
                    _agent_git_resources=self.resources, validate_agent_scope=self.validate,
                    capture_git_snapshot=lambda _: self.current,
                    _config_entries=lambda _: self.current.config,
                    _owned_rebase_state_dir=lambda *_: None,
                    _proven_merged_cleanup_paths=lambda *_: frozenset(),
                    _proven_manager_base_refs=lambda *_: frozenset(),
                    _published_plan_allows_common_plans=lambda *_: False,
                    requirement_archive_candidates=lambda *_: (),
                    _reconciled_manager_scope_metadata=lambda _c, _r, _s, _repo, _p, value, *_: value)
                def resume():
                    return scope_recovery.validate_persisted_agent_scope(
                        self.connection, self.repo_path, self.state, 'repo', effects=recovery)
                def recover():
                    return scope_recovery.validate_blocked_agent_scope(
                        self.connection, self.repo_path, 'repo', 'main', self.worktree,
                        self.plan_path if self.plan_id else '', effects=recovery, require_safe_result=True)
                resume()
                recover()
                # Selected refs and protected configuration remain checked on
                # both recovery paths, including mutations in the live state.
                safe_current = self.current
                ref = f'refs/heads/{self.before.branch}' if self.plan_id else 'refs/remotes/origin/main'
                for unsafe in (replace(self.current, config={'local:core.hooksPath': ('evil',)}),
                               replace(self.current, refs={**self.current.refs, ref: ('evil', '')})):
                    self.current = unsafe
                    self.connection.execute('UPDATE projects SET agent_result_snapshot=?', (unsafe.serialize(),))
                    self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (unsafe.serialize(),))
                    for action in (resume, recover):
                        with self.assertRaises(AgentScopeError):
                            action()
                self.current = safe_current
                self.connection.execute('UPDATE projects SET agent_result_snapshot=?', (result,))
                self.connection.execute('UPDATE exec_plans SET agent_result_snapshot=?', (result,))
                self.connection.execute("UPDATE agent_isolation_evidence SET snapshot_digest=? WHERE invocation_id=?",
                    ('d' * 64, identity))
                for action in (resume, recover):
                    with self.assertRaises(AgentScopeError):
                        action()
                owner = runtime_state.RuntimeStateRepository(self.connection).scope_owner('repo')
                self.assertEqual(owner['agent_snapshot'], self.start_serialized)
                self.assertEqual(owner['agent_result_snapshot'], result)

    def test_checkpoint_uses_preflight_proof_and_rejects_tampering(self):
        from loop_manager_internal.recovery import checkpoint_adapter
        self.begin('worker')
        identity = self.invoke()
        self.independent_changes()
        self.connection.execute("UPDATE projects SET phase='PLAN_BLOCKED', origin='owner/repo'")
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED'")
        request = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).request(
            repo='repo', plan_id='G-047', reason_code=manager.GUIDANCE_BLOCK_REASON,
            evidence='waiting', branch=self.before.branch, worktree=str(self.worktree),
            expected_remote_oid=None, rebase_recovery=False)
        safety = replace(manager._checkpoint_safety_effects(),
            _agent_git_resources=self.resources,
            git=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, 'https://github.com/owner/repo'),
            github_repository=lambda _: 'owner/repo',
            _proven_merged_cleanup_paths=lambda *_: frozenset(),
            _proven_manager_base_refs=lambda *_: frozenset(),
            _owned_checkpoint_remote_oid=lambda *_: None)
        adapter = checkpoint_adapter.BlockedCheckpointAdapter(
            self.connection, self.repo_path, self.state, 'repo', safety_effects=lambda: safety,
            publication_effects=manager._checkpoint_publication_effects)
        plan = runtime_state.RuntimeStateRepository(self.connection).scope_owner('repo', 'G-047')
        self.assertIsNone(adapter._shared_safety_error(request, plan, self.current))
        unsafe = replace(self.current, config={'local:core.hooksPath': ('evil',)})
        self.assertIsNotNone(adapter._shared_safety_error(request, plan, unsafe))
        self.connection.execute("UPDATE agent_isolation_evidence SET status='invalidated' WHERE invocation_id=?", (identity,))
        self.assertIsNotNone(adapter._shared_safety_error(request, plan, self.current))


class ScopeBaselineTests(TestCase):
    def test_capture_and_restore_keep_tree_identity_index_flags_and_local_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / 'app.py').write_text('dirty content', encoding='utf-8')
            before = snapshot(index={'app.py': ('s 100644 old 0',)},
                              dirty={'app.py': (('staged', 'unstaged'), 'signature')})
            refs = {}
            changes = []

            def git(_path, *args, **kwargs):
                value, code = '', 0
                if args == ('write-tree',):
                    value = 'index-tree'
                elif args[:2] == ('rev-parse', '--verify'):
                    value, code = refs.get(args[2], ''), int(args[2] not in refs)
                elif args[0] == 'rev-parse':
                    value = 'index-tree' if '^1' in args[1] else 'worktree-tree'
                elif args[0] == 'update-ref':
                    refs[args[1]] = args[2]
                else:
                    changes.append(args)
                return subprocess.CompletedProcess([], code, value)

            def command(arguments, **kwargs):
                if arguments[3] == 'commit-tree':
                    self.assertEqual(kwargs['env']['GIT_AUTHOR_NAME'], 'Loop Manager')
                    self.assertEqual(kwargs['env']['GIT_AUTHOR_DATE'], '2000-01-01T00:00:00 +0000')
                    value = arguments[4] + '-commit'
                else:
                    self.assertIn('GIT_INDEX_FILE', kwargs['env'])
                    value = 'worktree-tree' if arguments[3] == 'write-tree' else ''
                return subprocess.CompletedProcess(arguments, 0, value)

            effects = scope_baseline.ScopeBaselineEffects(
                mock.Mock(return_value=subprocess.CompletedProcess([], 0, b'blob-id\n')),
                mock.Mock(return_value=before), command, git)
            serialized = scope_baseline._capture_agent_baseline('repo', worktree, before, effects=effects)
            scope_baseline._restore_agent_baseline(worktree, before, serialized, effects=effects)
            self.assertIn(('read-tree', '--reset', '-u', 'worktree-tree'), changes)
            self.assertIn(('read-tree', 'index-tree'), changes)
            self.assertIn(('update-index', '--skip-worktree', '--', 'app.py'), changes)
            self.assertIn(('update-index', '--assume-unchanged', '--', 'app.py'), changes)
            self.assertEqual((worktree / 'app.py').read_text(), 'dirty content')
            effects.capture_git_snapshot.return_value = replace(before, dirty={})
            with self.assertRaisesRegex(AgentScopeError, '정확히 복원'):
                scope_baseline._restore_agent_baseline(worktree, before, serialized, effects=effects)
            refs.clear()
            changes.clear()
            with self.assertRaisesRegex(AgentScopeError, '보존 ref'):
                scope_baseline._restore_agent_baseline(worktree, before, serialized, effects=effects)
            self.assertEqual(changes, [])

    def test_unmerged_scope_skips_ordinary_baseline_and_staged_restore_keeps_flags(self):
        before = snapshot(index={'app.py': ('s 100644 old 0',)}, dirty={'app.py': (('staged',), 'value')})
        git = mock.Mock(return_value=subprocess.CompletedProcess([], 0, 'unmerged'))
        effects = scope_baseline.ScopeBaselineEffects(mock.Mock(), mock.Mock(), mock.Mock(), git)
        self.assertIsNone(scope_baseline._capture_agent_baseline('repo', Path('/managed'), before, effects=effects))
        effects.command.assert_not_called()
        git.reset_mock()
        scope_baseline._restore_agent_staged_entries(Path('/managed'), before, effects=effects)
        git.assert_any_call(Path('/managed'), 'update-index', '--add', '--cacheinfo', '100644', 'old', 'app.py')
        git.assert_any_call(Path('/managed'), 'update-index', '--skip-worktree', '--', 'app.py')


class PersistedScopeServiceTests(TestCase):
    def test_remote_scope_observation_requires_identity_and_owned_publication(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        blocked_remote_observation.ensure_schema(connection)
        branch = 'loop-manager/G-047'
        base_ref, branch_ref = 'refs/remotes/origin/main', f'refs/remotes/origin/{branch}'
        connection.execute("UPDATE projects SET origin='owner/repo'")
        connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED', branch=?, worktree='/managed', "
                           "publication_remote_oid='published'", (branch,))
        connection.execute("INSERT INTO blocked_remote_observations "
            "(repo, plan_id, registered_origin, origin_url, base_ref, branch_ref, base_result, branch_result, "
            "base_oid, branch_oid, observed_at) VALUES "
            "('repo', 'G-047', 'owner/repo', 'origin-url', 'refs/heads/main', ?, 'present', 'present', "
            "'new-base', 'published', 'now')", (f'refs/heads/{branch}',))
        before = snapshot(refs={base_ref: ('old-base', ''), branch_ref: ('old-branch', '')})
        current = replace(before, refs={base_ref: ('new-base', ''), branch_ref: ('published', ''),
                                       f'refs/heads/{branch}': ('head', '')})
        ownership = mock.Mock(return_value=None)
        refs = (blocked_remote_observation.RefObservation('present', 'new-base'),
                blocked_remote_observation.RefObservation('present', 'newer-remote-successor'))
        with mock.patch.object(blocked_remote_observation, 'observe_refs', return_value=refs):
            def observe():
                return blocked_plan_reconciliation.validated_scope_refs(
                    connection, Path('repo'), 'repo', 'main', 'G-047', before, None, current,
                    git=lambda *_: subprocess.CompletedProcess([], 0, 'origin-url'),
                    github_repository=lambda _: 'owner/repo', owned_remote_oid=ownership)
            self.assertEqual(observe(), frozenset((base_ref,)))
            ownership.return_value = 'published'
            self.assertEqual(observe(), frozenset((base_ref, branch_ref)))
            connection.execute("UPDATE blocked_remote_observations SET registered_origin='different' ")
            self.assertEqual(observe(), frozenset())
        self.assertFalse(ownership.call_args.kwargs['require_live'])

    def test_legacy_owner_and_publication_probes_do_not_initialize_schema_or_commit(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE projects (repo TEXT PRIMARY KEY, agent_role TEXT)')
        connection.execute('CREATE TABLE blocked_checkpoints (repo TEXT, plan_id TEXT, request_identity TEXT)')
        connection.execute("INSERT INTO projects VALUES ('repo', 'worker')")
        connection.execute("INSERT INTO blocked_checkpoints VALUES ('repo', 'G-047', 'identity')")
        repository = runtime_state.RuntimeStateRepository(connection)
        self.assertFalse(repository.scope_catalog_available())
        self.assertEqual(repository.scope_owner('repo')['agent_role'], 'worker')
        row = blocked_checkpoint_state.scope_request_row(connection, 'repo', 'G-047')
        self.assertEqual(row['request_identity'], 'identity')
        self.assertIsNone(blocked_checkpoint_state.scope_request_row(connection, 'repo', 'G-999'))
        self.assertTrue(connection.in_transaction)
        self.assertFalse(repository.scope_catalog_available())

    def test_missing_snapshot_evidence_stops_before_recovery_or_git(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        capture = mock.Mock()
        recover = mock.Mock()
        effects = replace(manager._scope_recovery_effects(), capture_git_snapshot=capture,
                          recover_persisted_agent_worktree=recover)
        for raw in ('invalid-json', snapshot(shared_metadata_complete=False).serialize()):
            connection.execute("UPDATE projects SET agent_snapshot = ?", (raw,))
            with self.subTest(raw=raw), self.assertRaises(AgentScopeError):
                scope_recovery.validate_persisted_agent_scope(connection, Path('repo'), Path('/state'),
                                                             'repo', effects=effects)
        capture.assert_not_called()
        recover.assert_not_called()

    def test_saved_result_validation_uses_registered_origin_for_normal_fetch(self):
        from loop_manager_internal.agents import scope_validation
        from loop_manager_internal.agents.agent_scope_decision import used_git_resources
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo_path = root / 'source'
            repo_path.mkdir()
            worktree = (root / 'worktree').resolve()
            worktree.mkdir()
            repo = str(repo_path.resolve())
            connection = runtime_connection(repo)
            self.addCleanup(connection.close)
            branch = 'loop-manager/G-047'
            ref, alias, own = ('refs/remotes/origin/main', 'refs/remotes/origin/HEAD',
                               f'refs/heads/{branch}')
            before = GitSnapshot(
                'head', branch, {}, {}, {ref: ('a', ''), alias: ('a', ref), own: ('head', '')},
                {str(worktree): (f'HEAD=head', f'branch={own}')}, {}, {},
            )
            current = replace(before, refs={ref: ('b', ''), alias: ('b', ref), own: ('head', '')})
            plan_path = 'docs/plans/[G-047] Runtime.md'
            connection.execute(
                "UPDATE projects SET origin='owner/repo', phase='WORKING', agent_role='worker', "
                "branch=?, worktree=?, plan_path=?, agent_snapshot=?, agent_result_snapshot=? "
                "WHERE repo=?",
                (branch, str(worktree), plan_path, before.serialize(), current.serialize(), repo),
            )
            connection.execute(
                "UPDATE exec_plans SET status='IMPLEMENTING', active_sha='base', branch=?, "
                "worktree=?, workspace_owned=1, agent_role='worker', agent_snapshot=?, "
                "agent_result_snapshot=? WHERE repo=? AND plan_id='G-047'",
                (branch, str(worktree), before.serialize(), current.serialize(), repo),
            )
            connection.commit()

            def git(_path, *args, **_kwargs):
                if args[0] == 'remote':
                    return subprocess.CompletedProcess([], 0, 'https://github.com/owner/repo.git\n')
                if args[0] == 'ls-remote':
                    return subprocess.CompletedProcess([], 0, 'b\trefs/heads/main\n')
                if args[0] == 'rev-parse':
                    return subprocess.CompletedProcess([], 0, 'b\n')
                return subprocess.CompletedProcess([], 0, '')

            validation_effects = scope_validation.ScopeValidationEffects(
                _agent_git_resources=lambda *_args, **_kwargs: None,
                repository=lambda db: runtime_state.RuntimeStateRepository(db),
                _current_rebase_service=mock.Mock(),
                _git_common_directory=lambda _path: root,
                _owned_rebase_state_dir=lambda *_args: None,
                _plan_id_from_path=manager._plan_id_from_path,
                _rebase_in_progress_for_branch=lambda *_args: False,
                _scope_snapshot_with_start_ignore_rules=lambda _worktree, _before, after: after,
                agent_snapshot_changes=lambda *_args: [],
                capture_git_snapshot=lambda _worktree: current,
                git=git,
                operation_log=mock.Mock(),
                validate_managed_worktree=mock.Mock(),
            )
            scope_effects = replace(
                manager._scope_recovery_effects(),
                _agent_git_resources=lambda *_args, **_kwargs: None,
                _config_entries=lambda _path: {},
                _with_proven_legacy_ignore_config=lambda value, *_args: value,
                _proven_merged_cleanup_paths=lambda *_args: frozenset(),
                _proven_manager_base_refs=lambda *_args: frozenset(),
                _published_plan_allows_common_plans=lambda *_args: False,
                requirement_archive_candidates=lambda *_args: (),
                _reconciled_manager_scope_metadata=lambda *_args: _args[5],
                capture_git_snapshot=lambda _worktree: current,
                git=git,
                validate_agent_scope=lambda *args, **kwargs: scope_validation.validate_agent_scope(
                    *args, **kwargs, effects=validation_effects,
                ),
            )
            scope_recovery._validate_persisted_agent_scope(
                connection, repo_path, root, repo, effects=scope_effects,
            )

    def test_blocked_agent_preservation_keeps_validated_commit_and_all_local_input(self):
        from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory).resolve()
            branch = 'loop-manager/G-047'
            own = f'refs/heads/{branch}'
            before = GitSnapshot(
                'old-head', branch, {}, {}, {own: ('old-head', '')},
                {str(worktree): ('HEAD=old-head', f'branch={own}', 'locked=owner')}, {}, {},
            )
            result = replace(before, dirty={'agent.py': (('unstaged',), 'saved Agent output')})
            current = replace(
                before, head='user-head',
                index={'user.py': ('H 100644 user-blob 0',)},
                dirty={'user.py': (('staged',), 'staged user input'),
                       'extra.py': (('untracked',), 'new user file')},
                refs={own: ('user-head', '')},
                worktrees={str(worktree): ('HEAD=user-head', f'branch={own}', 'locked=owner')},
            )
            connection = runtime_connection()
            self.addCleanup(connection.close)
            connection.execute(
                "UPDATE projects SET agent_role='worker', branch=?, worktree=?, "
                "agent_snapshot=?, agent_result_snapshot=? WHERE repo='repo'",
                (branch, str(worktree), before.serialize(), result.serialize()),
            )
            connection.execute(
                "UPDATE exec_plans SET branch=?, worktree=?, workspace_owned=1, "
                "agent_role='worker', agent_snapshot=?, agent_result_snapshot=? "
                "WHERE repo='repo' AND plan_id='G-047'",
                (branch, str(worktree), before.serialize(), result.serialize()),
            )
            connection.commit()
            RuntimeStateRepository(connection).block_plan(
                'repo', 'G-047', reason_code=BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION.value,
                evidence='saved scope violation', release_fingerprint='changed-input',
            )
            with mock.patch.object(manager, 'capture_git_snapshot', return_value=current):
                preserved = manager.preserve_blocked_agent_changes(
                    connection, 'repo', worktree, 'G-047', accept_current_input=True,
                    validated_snapshot=current,
                )
                self.assertEqual(preserved, current)
                with self.assertRaises(AgentScopeError):
                    manager.preserve_blocked_agent_changes(
                        connection, 'repo', worktree, 'G-047',
                    )
            self.assertEqual(tuple(connection.execute(
                "SELECT agent_snapshot, agent_result_snapshot FROM projects WHERE repo='repo'",
            ).fetchone()), (before.serialize(), result.serialize()))

    def test_blocked_agent_preservation_rejects_git_snapshot_change_after_validation(self):
        before = snapshot()
        result = replace(before, dirty={'agent.py': (('unstaged',), 'saved Agent output')})
        validated = replace(before, head='user-head')
        changed = replace(validated, config={'local:remote.origin.url': ('foreign',)})
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE projects SET agent_role='worker', agent_snapshot=?, "
            "agent_result_snapshot=? WHERE repo='repo'",
            (before.serialize(), result.serialize()),
        )
        connection.commit()

        with mock.patch.object(manager, 'capture_git_snapshot', return_value=changed), \
             mock.patch.object(manager, '_blocked_agent_local_baseline') as baseline:
            with self.assertRaisesRegex(AgentScopeError, '검증 이후 변경'):
                manager.preserve_blocked_agent_changes(
                    connection, 'repo', Path('/managed'), 'G-047',
                    accept_current_input=True, validated_snapshot=validated,
                )

        baseline.assert_not_called()
        self.assertEqual(tuple(connection.execute(
            "SELECT agent_snapshot, agent_result_snapshot FROM projects WHERE repo='repo'",
        ).fetchone()), (before.serialize(), result.serialize()))

    def test_blocked_agent_preservation_resumes_pending_recovery_for_current_input(self):
        before = snapshot()
        recovery = {
            "version": 1,
            "state": "prepared",
            "source_snapshot": before.serialize(),
            "original_head": before.head,
            "violation_head": before.head,
            "branch": before.branch,
            "stash_marker": "loop-manager scope violation G-047 recovery",
            "violation_ref": "refs/loop-manager/violations/G-047",
        }
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE projects SET agent_snapshot=?, agent_result_snapshot=?, "
            "agent_recovery=? WHERE repo='repo'",
            (before.serialize(), before.serialize(), json.dumps(recovery)),
        )
        connection.commit()

        with mock.patch.object(manager, 'capture_git_snapshot', return_value=before), \
             mock.patch.object(manager, "_complete_blocked_agent_recovery") as complete:
            preserved = manager.preserve_blocked_agent_changes(
                connection, "repo", Path("/managed"), "G-047",
                accept_current_input=True,
                validated_snapshot=before,
            )

        self.assertEqual(preserved, before)
        complete.assert_called_once_with(connection, "repo", Path("/managed"))

    def test_blocked_plan_resume_detects_code_only_committed_candidate(self):
        from types import SimpleNamespace
        from loop_manager_internal.recovery import blocked_plan_service
        from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory).resolve()
            branch = 'loop-manager/G-047'
            git_command(worktree, 'init', f'--initial-branch={branch}')
            git_command(worktree, 'config', 'user.email', 'loop-manager@example.com')
            git_command(worktree, 'config', 'user.name', 'Loop Manager Test')
            source = worktree / 'src/app.py'
            source.parent.mkdir()
            source.write_text('VALUE = 1\n', encoding='utf-8')
            git_command(worktree, 'add', 'src/app.py')
            git_command(worktree, 'commit', '-m', 'blocked Agent result')
            blocked_result = manager.capture_git_snapshot(worktree)
            source.write_text('VALUE = 2\n', encoding='utf-8')
            git_command(worktree, 'add', 'src/app.py')
            git_command(worktree, 'commit', '-m', 'user code correction')
            current = manager.capture_git_snapshot(worktree)
            connection = runtime_connection()
            self.addCleanup(connection.close)
            connection.execute(
                "UPDATE exec_plans SET status='IMPLEMENTING', active_sha='base', branch=?, "
                "worktree=?, workspace_owned=1, blocked_fingerprint='old-input', "
                "blocked_worktree_fingerprint='old-worktree', agent_result_snapshot=? "
                "WHERE repo='repo' AND plan_id='G-047'",
                (branch, str(worktree), blocked_result.serialize()),
            )
            connection.execute(
                "UPDATE projects SET planning_attempt_fingerprint=NULL WHERE repo='repo'",
            )
            connection.commit()
            RuntimeStateRepository(connection).block_plan(
                'repo', 'G-047', reason_code=BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION.value,
                evidence='saved scope violation', release_fingerprint='old-input',
            )
            current = GitSnapshot(
                'user-head', branch, {'user.py': ('H 100644 staged-blob 0',)},
                {'user.py': (('staged',), 'user content'),
                 'extra.py': (('untracked',), 'additional user file')},
                {f'refs/heads/{branch}': ('user-head', '')},
                {str(worktree): ('HEAD=user-head', f'branch=refs/heads/{branch}')}, {}, {},
            )
            preserve = mock.Mock(return_value=current)
            validate = mock.Mock(return_value=current)
            effects = SimpleNamespace(
                _blocked_execplan_metadata_error=mock.Mock(return_value=None),
                _blocked_execplan_revision_metadata_error=mock.Mock(return_value=None),
                _blocked_resume_worktree_matches=mock.Mock(return_value=True),
                _complete_direct_rebase_checkpoint=mock.Mock(),
                _direct_rebase_history_proven=mock.Mock(return_value=True),
                _journaled_direct_rebase=mock.Mock(return_value=subprocess.CompletedProcess([], 0, '')),
                _owned_rebase_journal_state=mock.Mock(return_value={}),
                _plan_block_selected_for_release=lambda db, repo, **_kwargs: db.execute(
                    'SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, 'G-047'),
                ).fetchone(),
                _reconcile_rebase_evidence_block=mock.Mock(return_value=False),
                _reconcile_scope_baseline_plan_block=mock.Mock(return_value=False),
                _record_blocked_resume_head=mock.Mock(),
                _requeue_blocked_execplan=mock.Mock(),
                _restore_agent_staged_entries=mock.Mock(),
                _resume_interrupted_blocked_rebase=mock.Mock(return_value=None),
                _revision_execplan_input_fingerprint=mock.Mock(return_value='old-input'),
                _revision_review_issue_fingerprint=mock.Mock(return_value=None),
                _update_blocked_error=mock.Mock(),
                _worktree_execplan_input_fingerprint=mock.Mock(return_value='old-worktree'),
                capture_git_snapshot=mock.Mock(return_value=current),
                clear_agent_scope=mock.Mock(),
                git=mock.Mock(side_effect=lambda _path, *args, **_kwargs:
                    subprocess.CompletedProcess([], 0, 'base\n') if args[0] == 'rev-parse'
                    else subprocess.CompletedProcess([], 0, '')),
                hydrate_execplan_project=mock.Mock(),
                make_worktree=mock.Mock(),
                preserve_blocked_agent_changes=preserve,
                project_execplan=mock.Mock(),
                remote_branch_oid=mock.Mock(return_value=None),
                validate_blocked_agent_scope=validate,
                validate_execplan_workspace_identity=mock.Mock(),
            )

            self.assertTrue(blocked_plan_service.resume_blocked_execplan_if_inputs_changed(
                effects, connection, Path('/repo'), 'repo', 'main', Path('/state'),
                plan_id='G-047',
            ))
            validate.assert_called_once()
            self.assertTrue(validate.call_args.kwargs['changed_candidate'])
            preserve.assert_called_once()
            self.assertTrue(preserve.call_args.kwargs['accept_current_input'])
            self.assertEqual(preserve.call_args.kwargs['validated_snapshot'], current)
            self.assertIsNone(preserve.call_args.kwargs['preserved_input_path'])

    def test_dirty_blocked_baseline_and_security_flags_are_not_absorbed(self):
        before = snapshot(worktrees={str(Path('/managed').resolve()): ('HEAD=head', 'branch=refs/heads/loop-manager/G-047')})
        result = replace(before, dirty={'src/app.py': (('unstaged',), 'agent')})
        accepted = scope_recovery._blocked_agent_local_baseline(before, result, result, Path('/managed'))
        self.assertEqual(accepted.dirty, result.dirty)
        for changed in (replace(result, dirty={'src/app.py': (('unstaged',), 'later-user-change')}),
                        replace(result, index={'src/app.py': ('s 100644 oid 0',)})):
            with self.subTest(changed=changed), self.assertRaises(AgentScopeError):
                scope_recovery._blocked_agent_local_baseline(before, result, changed, Path('/managed'))
