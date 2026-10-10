"""Scope service contracts using real repositories and synthetic Git evidence."""

from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
import sqlite3
import tempfile
from unittest import TestCase, mock

import loop_manager as manager
from loop_manager_internal.agents import scope_adapter, scope_baseline, scope_lifecycle, scope_observation, scope_recovery, scope_validation
from loop_manager_internal.agents.scope_contracts import AgentScopeError, GitSnapshot
from loop_manager_internal.recovery import blocked_plan_reconciliation, blocked_remote_observation
from loop_manager_internal.storage import agent_invocation_state, blocked_checkpoint_state, execution_checkpoint_state, runtime_state
from tests.support.database import runtime_connection


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

    def test_reviewer_is_read_only_and_worker_protected_paths_stay_protected(self):
        for path in ('src/app.py', 'AGENTS.md', 'docs/ARCHITECTURE.md', 'docs/requirements/change.md'):
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
                           "agent_role='worker', agent_snapshot=?, worker_session_id='session'",
                           (str(worktree), bound.branch, bound.serialize()))
        effects = replace(self.effects, _git_common_directory=lambda _: Path('/common'),
            git=lambda *_: subprocess.CompletedProcess([], 0, 'origin'),
            validate_managed_worktree=mock.Mock(), _rebase_in_progress_for_branch=lambda *_: False)
        with mock.patch.object(agent_invocation_state.InvocationRepository, 'verified_scope', return_value=False):
            self.assertIsNone(scope_validation._agent_git_resources(
                connection, Path('repo'), worktree, bound, 'worker', effects=effects))
        with mock.patch.object(agent_invocation_state.InvocationRepository, 'verified_scope', return_value=True):
            resources = scope_validation._agent_git_resources(
                connection, Path('repo'), worktree, bound, 'worker', effects=effects)
        self.assertEqual(resources.worktrees, frozenset((str(worktree),)))
        changed = replace(bound, refs={'refs/heads/unrelated': ('new', '')})
        self.validate('worker', bound, changed, resources=resources)
        with self.assertRaises(AgentScopeError):
            self.validate('worker', bound, changed)
        effects.validate_managed_worktree.assert_called_once()


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

    def test_dirty_blocked_baseline_and_security_flags_are_not_absorbed(self):
        before = snapshot(worktrees={str(Path('/managed').resolve()): ('HEAD=head', 'branch=refs/heads/loop-manager/G-047')})
        result = replace(before, dirty={'src/app.py': (('unstaged',), 'agent')})
        accepted = scope_recovery._blocked_agent_local_baseline(before, result, result, Path('/managed'))
        self.assertEqual(accepted.dirty, result.dirty)
        for changed in (replace(result, dirty={'src/app.py': (('unstaged',), 'later-user-change')}),
                        replace(result, index={'src/app.py': ('s 100644 oid 0',)})):
            with self.subTest(changed=changed), self.assertRaises(AgentScopeError):
                scope_recovery._blocked_agent_local_baseline(before, result, changed, Path('/managed'))
