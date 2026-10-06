"""Preservation unit tests: filesystem bytes and SQLite, all Git effects mocked."""
from dataclasses import replace
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.git import rebase_file_evidence as originals
from loop_manager_internal.notifications.incidents import _preservation_checkpoint, _current_block_key
from loop_manager_internal.planning import diagnostics
from loop_manager_internal.recovery import blocked_checkpoint, checkpoint_service
from loop_manager_internal.recovery import diagnostic_preservation as preservation
from loop_manager_internal.storage import blocked_checkpoint_state, runtime_state
from tests.support.database import runtime_connection


def result(output='', code=0):
    return SimpleNamespace(stdout=output, returncode=code)


class OriginalArtifactTests(unittest.TestCase):
    def test_ignore_inspection_retains_conflict_stages_and_rejects_ignored_or_unavailable_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manual = b'<<<<<<< HEAD\nupstream\n=======\nmanual edit\n>>>>>>> local\n'
            (root / 'code.py').write_bytes(manual)
            content = {'base': b'before\n', 'ours': b'upstream\n', 'theirs': b'local\n'}
            stages = b'100644 base 1\tcode.py\0' + b'100644 ours 2\tcode.py\0' + b'100644 theirs 3\tcode.py\0'
            snapshot = GitSnapshot('head', None, {'code.py': tuple(stages.decode().strip('\0').split('\0'))},
                {'code.py': (('unstaged',), 'conflict')}, {}, {}, {}, {'rebase-merge': 'owned'})
            source = preservation.PreservationSource('repo', 'project', 'G-047', 'cause', 'branch',
                root, {'base': 'base', 'head': 'head'}, {}, lambda _: True, lambda: snapshot)
            def raw(_root, *args):
                if 'ls-tree' in args:
                    return b'100644 blob base\tcode.py\0'
                if 'ls-files' in args:
                    return stages
                if 'cat-file' in args:
                    return content[args[-1]]
                if '--name-only' in args:
                    return b'code.py\0'
                if 'diff' in args:
                    return b'conflict diff\n'
                raise AssertionError(args)
            for ignore_code in (1, 0, 128):
                def check(_root, *args, **kwargs):
                    # Git rejects the old option, including on tracked conflicts.
                    if '--literal-pathspecs' in args:
                        return result('pathspec magic not supported: literal', 128)
                    self.assertEqual(args, ('--no-literal-pathspecs', 'check-ignore',
                        '--no-index', '--quiet', '--', 'code.py'))
                    self.assertEqual(kwargs, {'check': False})
                    return result('', ignore_code)
                with self.subTest(ignore_code=ignore_code):
                    read = mock.Mock(side_effect=raw)
                    before = snapshot.serialize()
                    assets, manifest = preservation.collect(
                        preservation.PreservationEffects(check, read, mock.Mock()), source, snapshot)
                    self.assertEqual(snapshot.serialize(), before)
                    self.assertEqual((root / 'code.py').read_bytes(), manual)
                    if ignore_code == 1:
                        self.assertTrue(manifest['complete'])
                        self.assertEqual(manifest['omitted'], {})
                        item = manifest['files']['code.py']
                        self.assertEqual(set(item['index']), {'1', '2', '3'})
                        for stage, oid in (('1', 'base'), ('2', 'ours'), ('3', 'theirs')):
                            self.assertEqual(assets['blobs/' + item['index'][stage]['blob']], content[oid])
                        self.assertEqual(assets['blobs/' + item['worktree']['blob']], manual)
                        self.assertIn('diff/worktree.patch', assets)
                    else:
                        self.assertFalse(manifest['complete'])
                        self.assertEqual(manifest['files'], {})
                        self.assertEqual(manifest['omitted'],
                            {'code.py': 'ignored path or ignore inspection unavailable'})
                        self.assertEqual(set(assets), {'manifest.json'})
                        self.assertFalse(any('cat-file' in call.args for call in read.call_args_list))

    def test_closed_pr_artifacts_have_ownership_proof_without_rebase_acceptance(self):
        from tests.support.rebase_observations import expectation, observation, scope_for
        from loop_manager_internal.git.managed_rebase_validation import PullRequest, RebaseStatus, assess_current_rebase
        expected = expectation()
        expected = replace(expected, remote=replace(expected.remote,
            pull_request=PullRequest('saved-pr', expected.remote.repository, expected.branch, expected.remote.oid, 'OPEN')))
        actual = replace(expected.remote, pull_request=replace(expected.remote.pull_request, state='CLOSED'))
        current = observation(expected, remote=actual)
        scope = scope_for(expected, current)
        self.assertEqual(assess_current_rebase(expected, current, scope).status, RebaseStatus.REJECTED)
        service = SimpleNamespace(expected=expected, adapters=SimpleNamespace(
            path_allowed=lambda name: name == 'app.py', contracts_allowed=lambda _: True))
        with mock.patch('loop_manager_internal.git.managed_rebase_observation.observe_current_rebase',
                        return_value=(current, scope)):
            self.assertEqual(preservation.artifact_observation(service), current)
        changed = replace(current, remote=replace(actual, pull_request=replace(actual.pull_request, identity='different-pr')))
        with (mock.patch('loop_manager_internal.git.managed_rebase_observation.observe_current_rebase', return_value=(changed, scope)),
              self.assertRaises(RuntimeError)):
            preservation.artifact_observation(service)

    def test_conflict_binary_manual_edit_delete_rename_and_modes_remain_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'new.bin').write_bytes(b'\0manual\xff\r\n')
            (root / 'new.bin').chmod(0o755)
            blobs = {'base': b'\0base', 'ours': b'\0ours', 'theirs': b'\0theirs'}
            def git(_root, *args):
                if 'ls-tree' in args:
                    revision, name = args[-3], args[-1]
                    return (b'100644 blob base\told.bin\0' if revision == 'base' and name == 'old.bin'
                            else b'100755 blob ours\tnew.bin\0' if revision == 'head' and name == 'new.bin' else b'')
                if 'ls-files' in args:
                    return (b'100644 base 1\tnew.bin\0'
                            b'100755 ours 2\tnew.bin\0'
                            b'100644 theirs 3\tnew.bin\0') if args[-1] == 'new.bin' else b''
                return blobs[args[-1]]
            read = mock.Mock(side_effect=git)
            files, contents = originals.capture_originals(root, ('old.bin', 'new.bin'),
                                                          {'base': 'base', 'head': 'head'}, read)
            self.assertTrue(files['old.bin']['worktree']['absent'])
            self.assertTrue(files['old.bin']['head']['absent'])
            self.assertTrue(files['new.bin']['base']['absent'])
            self.assertEqual(set(files['new.bin']['index']), {'1', '2', '3'})
            self.assertEqual(files['new.bin']['worktree']['mode'],
                             '100644' if os.name == 'nt' else '100755')
            self.assertEqual(contents[files['new.bin']['worktree']['blob']], b'\0manual\xff\r\n')
            self.assertEqual((root / 'new.bin').read_bytes(), b'\0manual\xff\r\n')
            self.assertEqual({call.args[1] for call in read.call_args_list}, {'--literal-pathspecs', 'cat-file'})

    def test_unsafe_and_symlink_parent_paths_are_never_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ['../outside', '/outside', '.git/config', 'a\\b']
            try:
                (root / 'link').symlink_to(root, target_is_directory=True)
                paths.append('link/file')
            except OSError:
                pass  # Windows may deny symlink creation; traversal cases still run.
            for name in paths:
                read = mock.Mock()
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    originals.capture_originals(root, (name,), {'base': 'base'}, read)
                read.assert_not_called()


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.snapshot = GitSnapshot('head', None, {'app.bin': ('M 100644 base 1',)},
            {'app.bin': (('unstaged',), 'signature')}, {}, {}, {}, {'rebase-merge': 'owned'})
        self.source = preservation.PreservationSource('repo', 'plan', 'G-047', 'cause',
            'loop-manager/G-047', self.root, {'base': 'base', 'head': 'head'},
            {'phase': 'PLAN_BLOCKED', 'reason': 'conflict'}, lambda _: True, lambda: self.snapshot)
        self.request = blocked_checkpoint.CheckpointRequest('repo', 'G-047', 'request',
            blocked_checkpoint.CheckpointPhase.UNPUBLISHED, 'conflict', '', self.source.branch, str(self.root),
            pr_url='https://github.com/owner/repo/pull/1')
        self.adapter = mock.Mock()
        self.remote = None
        self.adapter.observe_remote.side_effect = lambda _: self.remote
        self.push = mock.Mock(side_effect=self._push)
        self.effects = preservation.PreservationEffects(mock.Mock(return_value=result('tree', 0)),
            mock.Mock(return_value=b''), self.push)
        self.assets = {'manifest.json': b'{"version":1}', 'blobs/hash': b'\0original'}
        self.manifest = {'files': {'app.bin': {}}, 'omitted': {}, 'complete': True}
        self.collection = mock.patch.object(preservation, 'collect', return_value=(self.assets, self.manifest))
        self.collection.start()
        self.addCleanup(self.collection.stop)
        self.commit_patch = mock.patch.object(preservation, '_commit', return_value=('tree', 'commit'))
        self.commit = self.commit_patch.start()
        self.addCleanup(self.commit_patch.stop)

    def _push(self, adapter, request, head):
        self.assertIsNone(request.pr_url)
        self.assertTrue(request.branch.startswith('loop-manager/diagnostics/G-047/'))
        self.remote = head

    def preserve(self):
        return preservation.preserve(self.effects, self.connection, self.root, self.source,
                                     self.adapter, self.request, 'original branch unavailable')

    def test_published_artifact_reuses_intent_without_commit_push_or_pr(self):
        first = self.preserve()
        self.assertEqual(first['result'], 'published')
        self.assertEqual(first['original_branch'], self.request.branch)
        self.assertEqual(self.preserve(), first)
        self.commit.assert_called_once()
        self.push.assert_called_once()
        self.adapter.existing_pull_request.assert_not_called()
        self.assertEqual(self.connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'READY')

    def test_push_then_interruption_before_receipt_is_confirmed_without_repush(self):
        self.adapter.observe_remote.side_effect = [None, RuntimeError('network offline')]
        first = self.preserve()
        self.assertEqual(first['result'], 'local_only')
        self.assertEqual(self.remote, 'commit')
        self.adapter.observe_remote.side_effect = lambda _: self.remote
        self.assertEqual(self.preserve()['result'], 'published')
        self.push.assert_called_once()
        self.commit.assert_called_once()

    def test_sqlite_failure_after_push_recovers_from_saved_intent(self):
        real = blocked_checkpoint_state.DiagnosticPublicationRepository.preserve
        def record(store, *args, **kwargs):
            if args[4]['result'] == 'published':
                raise sqlite3.OperationalError('receipt interrupted')
            return real(store, *args, **kwargs)
        with mock.patch.object(blocked_checkpoint_state.DiagnosticPublicationRepository, 'preserve', record):
            self.assertEqual(self.preserve()['result'], 'local_only')
        self.assertEqual(self.preserve()['result'], 'published')
        self.push.assert_called_once()

    def test_competing_diagnostic_ref_is_never_overwritten_or_retried(self):
        self.remote = 'user-commit'
        first = self.preserve()
        self.assertEqual(first['result'], 'local_only')
        self.assertIn('다른 OID', first['reason'])
        self.push.assert_not_called()
        count = self.adapter.observe_remote.call_count
        self.assertEqual(self.preserve(), first)
        self.assertEqual(self.adapter.observe_remote.call_count, count)

    def test_changed_manual_file_prevents_commit_and_push(self):
        self.source = replace(self.source, validate=mock.Mock(side_effect=[self.snapshot, replace(self.snapshot, head='new')]))
        payload = self.preserve()
        self.assertEqual(payload['result'], 'local_only')
        self.commit.assert_not_called()
        self.push.assert_not_called()

    def test_origin_failure_prevents_even_local_diagnostic_commit(self):
        self.adapter._validated_push_url.side_effect = RuntimeError('origin mismatch')
        self.assertEqual(self.preserve()['reason'], 'origin mismatch')
        self.commit.assert_not_called()
        self.push.assert_not_called()

    def test_unconfirmed_commit_pruned_by_gc_is_reconstructed_with_same_oid(self):
        self.push.side_effect = blocked_checkpoint.CheckpointPushError('temporarily offline', retryable=True)
        self.assertEqual(self.preserve()['result'], 'local_only')
        self.effects.git.return_value = result('', 1)
        self.push.side_effect = self._push
        self.assertEqual(self.preserve()['result'], 'published')
        self.assertEqual(self.commit.call_count, 2)
        bundles = [path for path in (self.root / 'diagnostic-artifacts').iterdir() if path.is_dir()]
        self.assertEqual(len(bundles), 1)
        self.assertEqual((bundles[0] / 'blobs/hash').read_bytes(), b'\0original')

    def test_revisited_shape_becomes_latest_receipt_without_republishing(self):
        original_snapshot = self.snapshot
        first = self.preserve()
        self.snapshot = replace(self.snapshot, head='manual-change')
        self.preserve()
        self.snapshot = original_snapshot
        self.assertEqual(self.preserve(), first)
        store = blocked_checkpoint_state.DiagnosticPublicationRepository(self.connection)
        self.assertEqual(store.latest_preservation('repo', 'plan', 'G-047'), first)
        self.push.assert_called_once()

    def test_partial_artifacts_never_claim_complete_and_new_shape_gets_new_intent(self):
        self.manifest['omitted'] = {'secret.bin': 'credential content'}
        self.manifest['complete'] = False
        self.assertFalse(self.preserve()['complete'])
        self.snapshot = replace(self.snapshot, dirty={'app.bin': (('unstaged',), 'manual-change')})
        self.assets['blobs/new'] = b'new'
        self.preserve()
        self.assertEqual(self.connection.execute('SELECT count(*) FROM diagnostic_preservations').fetchone()[0], 2)

    def test_private_index_orphan_commit_is_deterministic_and_does_not_update_refs(self):
        # Call the original implementation directly; every Git write is a mock.
        self.commit_patch.stop()
        calls = []
        def git(path, *args, **kwargs):
            calls.append((args, kwargs))
            return result('tree' if 'write-tree' in args else 'commit' if 'commit-tree' in args else 'blob')
        effects = replace(self.effects, git=git)
        self.assertEqual(preservation._commit(effects, self.root, self.source, self.assets), ('tree', 'commit'))
        commands = [args[4] for args, kwargs in calls]
        self.assertEqual(commands[-2:], ['write-tree', 'commit-tree'])
        self.assertNotIn('update-ref', commands)
        self.assertNotIn('commit', commands)
        self.assertNotIn('-p', calls[-1][0])
        self.assertEqual(calls[-1][1]['env']['GIT_AUTHOR_DATE'], '2000-01-01T00:00:00+00:00')
        self.assertNotEqual(calls[-1][1]['env']['GIT_INDEX_FILE'], str(self.root / '.git/index'))


class PreservationEngineTests(unittest.TestCase):
    def test_secret_originals_private_ignored_and_unrelated_files_never_enter_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, data in {'ok.bin': b'\0safe', 'secret.bin': b'password=do-not-publish',
                               'unrelated.bin': b'unrelated', '.env': b'SAFE_NAME=value'}.items():
                (root / name).write_bytes(data)
            snapshot = GitSnapshot('head', None, {}, {name: (('unstaged',), name) for name in
                ('ok.bin', 'secret.bin', 'unrelated.bin', '.env', 'ignored')}, {}, {}, {}, {})
            source = preservation.PreservationSource('repo', 'plan', 'G-047', 'cause', 'branch',
                root, {'base': 'base', 'head': 'head'}, {}, lambda name: name != 'unrelated.bin', lambda: snapshot)
            def raw(path, *args):
                if 'diff' in args:
                    if '--name-only' in args:
                        return b''
                    self.assertEqual(args[-1], 'ok.bin')
                    return b'binary diff'
                if 'ls-tree' in args or 'ls-files' in args:
                    return b''
                raise AssertionError(args)
            effects = preservation.PreservationEffects(
                lambda path, *args, **kwargs: result('', 0 if args[-1] == 'ignored' else 1), raw, mock.Mock())
            assets, manifest = preservation.collect(effects, source, snapshot)
            self.assertEqual(set(manifest['files']), {'ok.bin'})
            self.assertEqual(set(manifest['omitted']), {'secret.bin', 'unrelated.bin', '.env', 'ignored'})
            self.assertFalse(manifest['complete'])
            self.assertNotIn(b'do-not-publish', b''.join(assets.values()))
            self.assertIn(b'\0safe', assets.values())

    def test_credentials_in_utf16_binary_tokens_and_private_keys_are_detected(self):
        for content in (b'\0password=hidden\0', 'api_key="hidden"'.encode('utf-16-le'),
                        b'-----BEGIN OPENSSH PRIVATE KEY-----', b'ghp_' + b'A' * 30,
                        b'https://user:password@github.com/owner/repo'):
            with self.subTest(content=content):
                self.assertTrue(preservation.sensitive(content))
        self.assertFalse(preservation.sensitive(b'\0image\xff\n'))
        detail = preservation.redact_detail('https://user:password@github.com/repo\n'
            '-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-body\n-----END OPENSSH PRIVATE KEY-----')
        self.assertNotIn('password', detail)
        self.assertNotIn('private-body', detail)


class PreservationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.store = blocked_checkpoint_state.DiagnosticPublicationRepository(self.connection)

    def test_plan_notification_is_bound_to_current_cause_and_does_not_claim_branch_ownership(self):
        self.connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                                "VALUES ('repo', 'G-047', 'plan', 'managed_worktree_conflict', 'conflict')")
        block = self.connection.execute('SELECT * FROM execplan_blocks').fetchone()
        cause = hashlib.sha256(json.dumps(dict(block), sort_keys=True).encode()).hexdigest()
        payload = dict(result='published', diagnostic=True, cause_identity=cause,
                       branch='loop-manager/diagnostics/G-047/hash', commit_oid='commit', original_branch='branch')
        self.store.preserve('repo', 'plan', 'G-047', 'input', payload, ref=payload['branch'], tree='tree', commit='commit')
        self.assertEqual(_preservation_checkpoint(self.connection, 'repo', 'plan', 'G-047')['branch'], payload['branch'])
        notice = _preservation_checkpoint(self.connection, 'repo', 'plan', 'G-047')
        from loop_manager_internal.notifications.incidents import _plan_block_key, _project_checkpoint_suffix
        self.assertEqual(_current_block_key(self.connection, 'repo',
            dict(scope='plan', stage='ExecPlan', execplan_id='G-047', reason_code='managed_worktree_conflict')),
            _plan_block_key('repo', 'managed_worktree_conflict', 'G-047', None, _project_checkpoint_suffix(notice)))
        self.assertIsNone(self.connection.execute('SELECT * FROM blocked_checkpoints').fetchone())
        self.connection.execute("UPDATE execplan_blocks SET evidence='different cause'")
        self.assertIsNone(_preservation_checkpoint(self.connection, 'repo', 'plan', 'G-047'))

    def test_original_branch_refusal_dispatches_diagnostics_without_new_block(self):
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        request = repository.request(repo='repo', plan_id='G-047', reason_code='managed_worktree_conflict',
            evidence='conflict', branch='branch', worktree='/managed', expected_remote_oid=None, rebase_recovery=False)
        adapter = mock.Mock()
        adapter.assess.return_value = blocked_checkpoint.SafetyVerdict(
            False, blocked_checkpoint.SafetyScope.PLAN, 'git_operation_in_progress', 'conflict', 'head', ())
        preserve = mock.Mock()
        effects = checkpoint_service.DispatchBlockedCheckpointsEffects(lambda *args: adapter, preserve)
        self.assertEqual(checkpoint_service.dispatch_blocked_checkpoints(effects, self.connection,
            Path('/repo'), Path('/state'), 'repo'), 1)
        self.assertEqual(preserve.call_args.args[-1].plan_id, request.plan_id)
        self.assertIsNone(self.connection.execute('SELECT * FROM project_blocks').fetchone())

    def test_completed_project_publication_does_not_start_a_second_diagnostic(self):
        from loop_manager_internal.recovery import project_blocked_recovery as project
        coordinator = project.ProjectBlockedReconciler.__new__(project.ProjectBlockedReconciler)
        coordinator.connection = mock.Mock()
        coordinator.connection.execute.return_value.fetchone.side_effect = [{}, {'stage': 'complete'}]
        coordinator.repo, coordinator.repo_path, coordinator.state_dir = 'repo', Path('/repo'), Path('/state')
        coordinator.ports = mock.Mock()
        coordinator.ports.unmet_execplan_dependencies.return_value = False
        coordinator._plan = mock.Mock(return_value={'plan_id': 'G-047'})
        with (mock.patch.object(project, 'project_slot_available', return_value=True),
              mock.patch.object(project.observations, 'pending_input', return_value=False)):
            self.assertEqual(coordinator.reconcile('G-047'), 'blocked')
        coordinator.ports.preserve_blocked_diagnostics.assert_not_called()

    def test_unavailable_artifact_receipt_replaces_notice_once_across_retries(self):
        from loop_manager_internal.notifications import notifications
        notifications.configure_session(self.connection, enabled=True)
        self.connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                                "VALUES ('repo', 'G-047', 'plan', 'managed_worktree_conflict', 'conflict')")
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        request = repository.request(repo='repo', plan_id='G-047', reason_code='managed_worktree_conflict',
            evidence='conflict', branch='branch', worktree='/managed', expected_remote_oid=None, rebase_recovery=False)
        repository.finish_unpublished(request, 'unresolved rebase', retryable=False)
        adapter = mock.Mock()
        adapter.diagnostic_source.side_effect = RuntimeError('ownership unavailable')
        effects = checkpoint_service.PreserveBlockedDiagnosticsEffects(mock.Mock(), mock.Mock(return_value=adapter))
        for _ in range(3):
            payload = checkpoint_service.preserve_blocked_diagnostics(effects, self.connection,
                Path('/repo'), Path('/state'), request)
            self.assertEqual(payload['result'], 'local_only')
            self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM notification_outbox "
                "WHERE event_type='blocked' AND canceled_at IS NULL").fetchone()[0], 1)
            self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM notification_outbox "
                "WHERE event_type='blocked'").fetchone()[0], 2)
        effects.preservation_effects.assert_not_called()
        self.assertEqual(self.connection.execute(
            "SELECT unpublished_reason FROM blocked_checkpoints WHERE repo='repo' AND plan_id='G-047'"
        ).fetchone()[0], 'unresolved rebase')

    def test_unowned_planning_is_local_only_and_cannot_clear_error_or_other_plan(self):
        self.connection.execute("UPDATE projects SET planning_error='failure', planning_error_incident_id='incident', "
                                "planning_workspace_owned=0, worktree='/unowned', active_sha='base'")
        self.connection.commit()
        snapshot = runtime_state.RuntimeStateRepository(self.connection).planning('repo')
        effects = diagnostics.RemotePlanningDiagnosticsEffects(*[mock.Mock() for _ in range(6)])
        payload = diagnostics.preserve_remote_planning_diagnostics(effects, self.connection, Path('/state'), 'repo', snapshot)
        self.assertEqual(payload['result'], 'local_only')
        self.assertFalse(payload['complete'])
        effects.preservation_effects.assert_not_called()
        self.assertEqual(runtime_state.RuntimeStateRepository(self.connection).planning('repo'), snapshot)
        self.assertEqual(_preservation_checkpoint(self.connection, 'repo', 'planning', 'planning')['result'], 'local_only')

    def test_owned_planning_uses_common_preservation_without_publishing_catalog_or_pr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.connection.execute("UPDATE projects SET phase='PLANNING', planning_error='failure', "
                                    "planning_error_incident_id='incident', planning_workspace_owned=1, worktree=?, active_sha='base'", (str(root),))
            self.connection.commit()
            snapshot = runtime_state.RuntimeStateRepository(self.connection).planning('repo')
            current = GitSnapshot('head', None, {}, {}, {}, {}, {}, {})
            adapter = mock.Mock()
            effects = diagnostics.RemotePlanningDiagnosticsEffects(lambda: 'effects', lambda *args: adapter,
                lambda *args: current, mock.Mock(), mock.Mock(), mock.Mock())
            with mock.patch.object(preservation, 'preserve', return_value={'result': 'published'}) as publish:
                diagnostics.preserve_remote_planning_diagnostics(effects, self.connection, root, 'repo', snapshot)
            source, selected_adapter, request = publish.call_args.args[3:6]
            self.assertEqual(source.scope, 'planning')
            self.assertEqual(source.validate(), current)
            self.assertTrue(source.allowed('docs/plans/[G-001] New.md'))
            self.assertFalse(source.allowed('AGENTS.md'))
            self.assertFalse(source.allowed('unrelated.py'))
            self.assertIsNone(request.pr_url)
            self.assertEqual(runtime_state.RuntimeStateRepository(self.connection).planning('repo'), snapshot)

    def test_suspended_planning_retries_same_owner_without_hydrating_other_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.connection.execute("UPDATE projects SET phase='PLANNING', planning_error='failure', "
                                    "planning_error_incident_id='incident', planning_workspace_owned=1, worktree=?, active_sha='base'", (str(root),))
            self.connection.commit()
            runtime = runtime_state.RuntimeStateRepository(self.connection)
            runtime.suspend_planning(runtime.planning('repo'))
            expected = runtime.planning('repo')
            current = GitSnapshot('head', None, {}, {}, {}, {}, {}, {})
            effects = diagnostics.RemotePlanningDiagnosticsEffects(lambda: 'effects', mock.Mock(),
                lambda *args: current, mock.Mock(), mock.Mock(), mock.Mock())
            with mock.patch.object(preservation, 'preserve', return_value={'result': 'published'}) as publish:
                diagnostics.preserve_remote_planning_diagnostics(effects, self.connection, root, 'repo', expected)
            self.assertEqual(publish.call_args.args[3].worktree, root)
            self.assertEqual(publish.call_args.args[3].validate(), current)
            self.assertEqual(runtime.planning('repo'), expected)

    def test_same_failed_input_retries_artifacts_before_continuing_independent_work(self):
        from loop_manager_internal.planning.selection_service import PlanningSelectionService
        from loop_manager_internal.storage import maintenance_state
        self.connection.execute("UPDATE projects SET phase='PLANNING', planning_error='failure', "
                                "planning_error_fingerprint='same-input', active_sha='base'")
        self.connection.commit()
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        runtime.suspend_planning(runtime.planning('repo'))
        expected = runtime.planning('repo')
        ports = SimpleNamespace(state=mock.Mock(), git=mock.Mock(), events=mock.Mock())
        ports.state.schedule.return_value = SimpleNamespace(active=None, project_blocked=False)
        ports.git.input_fingerprint.return_value = 'same-input'
        service = PlanningSelectionService(ports)
        order = []
        ports.state.retry_diagnostic_preservation.side_effect = lambda *args: order.append('preservation')
        service._continue_published_work = lambda run: order.append('independent work') or 'continue'
        run = SimpleNamespace(row=expected, context=SimpleNamespace(repo='repo', repo_path=Path('/repo')),
            base_sha='base', publication_recovery_verified=False, maintenance_planning=False,
            maintenance_cycle=SimpleNamespace(phase=maintenance_state.MaintenancePhase.IDLE))
        self.assertEqual(service._gate_preserved_error(run), 'continue')
        self.assertEqual(order, ['preservation', 'independent work'])
        ports.state.resume_planning.assert_not_called()
        self.assertEqual(runtime.planning('repo'), expected)

    def test_notification_rendering_reports_partial_artifacts_and_original_failure(self):
        from loop_manager_internal.notifications.rendering import _message
        text = _message('blocked', dict(project='repo', stage='ExecPlan', scope='plan',
            execplan_id='G-047', reason_code='conflict', cause='conflict', paths=['app.bin'],
            checkpoint=dict(result='published', diagnostic=True, complete=False,
                            branch='diagnostic', commit_oid='commit', original_branch='branch',
                            omitted={'secret.bin': 'credential content'},
                            original_unpublished_reason='original branch unavailable')))
        self.assertIn('Incomplete artifacts', text)
        self.assertIn('original branch unavailable', text)
        self.assertIn('Original branch: branch', text)

    def test_conflict_source_uses_owned_current_rebase_and_excludes_unapproved_untracked(self):
        from loop_manager_internal.recovery import checkpoint_adapter
        self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED', branch='branch', "
                                "worktree='/managed', active_sha='base'")
        self.connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                                "VALUES ('repo', 'G-047', 'plan', 'managed_worktree_conflict', 'conflict')")
        self.connection.execute('CREATE TABLE IF NOT EXISTS blocked_remote_observations (repo TEXT, plan_id TEXT, branch_oid TEXT)')
        store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        request = store.request(repo='repo', plan_id='G-047', reason_code='managed_worktree_conflict', evidence='conflict',
            branch='branch', worktree='/managed', expected_remote_oid=None, rebase_recovery=False)
        snapshot = GitSnapshot('head', None, {}, {'unrelated.txt': (('untracked',), 'unrelated')}, {}, {}, {}, {'rebase-merge': 'owned'})
        observed = SimpleNamespace(metadata=(('orig-head', 'original'), ('onto', 'base')))
        service = mock.Mock()
        service.require_owned.return_value = (observed, mock.Mock())
        adapter = checkpoint_adapter.BlockedCheckpointAdapter(self.connection, Path('/repo'), Path('/state'), 'repo',
            safety_effects=lambda: replace(loop_manager._checkpoint_safety_effects(),
                current_scope_effects=lambda: SimpleNamespace(_current_rebase_service=lambda *args, **kwargs: service),
                capture_git_snapshot=lambda *args: snapshot,
                git=lambda path, *args, **kwargs: result('replay' if args[0] == 'rev-parse' else 'ancestor' if args[0] == 'merge-base' else 'original'),
                _published_plan_allows_common_plans=lambda *args: False),
            publication_effects=mock.Mock())
        with (mock.patch('loop_manager_internal.recovery.blocked_plan_reconciliation.slot_available', return_value=True),
              mock.patch.object(adapter, '_origin_safety_error', return_value=None)):
            source = adapter.diagnostic_source(request)
            self.assertEqual(source.revisions['start'], 'original')
            self.assertEqual(source.revisions['replay'], 'replay')
            self.assertFalse(source.allowed('unrelated.txt'))
            self.assertEqual(source.validate(), snapshot)
            self.connection.execute("UPDATE execplan_blocks SET evidence='new cause'")
            with self.assertRaises(RuntimeError):
                source.validate()

    def test_composition_connects_plan_project_and_planning_preservation(self):
        with mock.patch.object(loop_manager, 'preserve_blocked_diagnostics') as preserve:
            loop_manager._project_blocked_recovery_effects().preserve_blocked_diagnostics('selected')
            preserve.assert_called_once_with('selected')
        with (mock.patch.object(loop_manager, 'preserve_remote_planning_diagnostics') as preserve,
              mock.patch.object(loop_manager, 'preserve_planning_diagnostics')):
            ports = loop_manager._PlanningStatePorts(self.connection,
                runtime_state.RuntimeStateRepository(self.connection), mock.Mock(), Path('/state'))
            snapshot = runtime_state.RuntimeStateRepository(self.connection).planning('repo')
            ports.preserve_diagnostics('repo', snapshot)
            preserve.assert_called_once_with(self.connection, Path('/state'), 'repo', snapshot)
