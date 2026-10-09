"""Unit observations only: no Git lifecycle or external service is executed."""
from tests.support.rebase_observations import expectation, observation, scope_for, decision_for

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from loop_manager import _worker_path_allowed, capture_git_snapshot
from loop_manager_internal.agents import scope_observation
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.git.managed_rebase_observation import (
    ObservationAdapters, observe_current_rebase, parse_index, parse_raw_changes,
)
from loop_manager_internal.git.managed_rebase_validation import FileChange, IndexEntry, ObservationFailure, PullRequest, RebaseObservation, RebaseStatus, Remote, WorktreeFile, assess_current_rebase, valid_acceptance


class CurrentRebasePolicyTests(unittest.TestCase):
    def test_final_manual_result_needs_no_execution_evidence(self):
        current = observation()
        expected = expectation(before_head=None)
        result = decision_for(expected, current)
        self.assertEqual(result.status, RebaseStatus.COMPLETE)
        self.assertTrue(valid_acceptance(result.acceptance))
        self.assertEqual(result, decision_for(expected, current))
        auxiliary = replace(current, operations=(('REBASE_HEAD', 'last-commit'),
                                                  ('MERGE_MSG', 'message')))
        self.assertEqual(decision_for(expected, auxiliary).status, RebaseStatus.COMPLETE)
        # These historical records are deliberately outside the API. Removing
        # each cannot affect acceptance of identical current Git facts.
        history = dict(turn='turn', stage_tree='tree', receipt='receipt',
                       reflog='marker', replay=['original'], journal='incomplete')
        for key in history:
            with self.subTest(missing=key):
                remaining = {name: value for name, value in history.items() if name != key}
                self.assertNotIn(key, remaining)
                self.assertEqual(decision_for(expected, current), result)
        # Squash/reorder/content changes are not compared to old commit lists.
        rewritten = replace(current, head='rewritten', branch_ref=('rewritten', ''),
                            registration=('HEAD=rewritten',
                                          f'branch=refs/heads/{expected.branch}'),
                            committed=(FileChange('app.py', '100644', '100755',
                                                  'old', 'new-content', 'M'),))
        self.assertEqual(decision_for(expected, rewritten).status, RebaseStatus.COMPLETE)

    def test_new_rebase_start_retains_auxiliary_files_but_rejects_active_operations(self):
        expected = expectation(before_head='rebased', target_base_oid='advanced-base')
        current = observation(expected, head='rebased', branch_ref=('rebased', ''),
            registration=('HEAD=rebased', f'branch=refs/heads/{expected.branch}'),
            base_is_ancestor=False)
        auxiliary = (('REBASE_HEAD', 'last-commit'), ('MERGE_MSG', 'message'),
                     ('MERGE_MODE', 'mode'))
        for operations in ((), *(tuple([item]) for item in auxiliary), auxiliary):
            with self.subTest(operations=operations):
                self.assertEqual(decision_for(expected, replace(current, operations=operations)).status,
                                 RebaseStatus.START)
        for marker in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'sequencer',
                       'rebase-merge', 'rebase-apply', 'BISECT_START'):
            with self.subTest(marker=marker):
                pending = replace(current, operations=(*auxiliary, (marker, 'active')))
                self.assertEqual(decision_for(expected, pending).status, RebaseStatus.REJECTED)
        for changes in (
            {'head': 'unexpected'}, {'index_clean': False},
            {'staged': current.committed},
            {'index': (IndexEntry('app.py', '100644', 'unmerged', 2, 'M', 0x2000),)},
            {'remote': replace(expected.remote, oid='changed')},
            {'committed': (replace(current.committed[0], path='docs/requirements/Protected.md'),)},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(decision_for(expected, replace(current, operations=auxiliary,
                                                               **changes)).status,
                                 RebaseStatus.REJECTED)

    def test_owned_detached_conflict_waits_without_mutating_index(self):
        expected = expectation()
        pending = observation(
            expected, head='partial', branch=None, branch_ref=('before', ''),
            registration=('HEAD=partial', 'detached=True'),
            branch_worktrees=(),
            operations=(('rebase-merge', 'metadata-signature'),),
            metadata=(('head-name', f'refs/heads/{expected.branch}'),
                      ('orig-head', 'before'), ('onto', 'base')),
            index_clean=False, base_is_ancestor=True,
            index=tuple(IndexEntry('app.py', '100644', f'blob-{stage}',
                                   stage, 'M', stage << 12) for stage in (1, 2, 3)),
            staged=(FileChange('app.py', '100644', '000000', 'old', '0', 'U'),),
            files=(WorktreeFile('app.py', ('staged', 'unstaged'), '100644', 'conflict'),),
        )
        for remote in (expected.remote, replace(expected.remote, oid=None)):
            with self.subTest(remote=remote):
                owned = replace(expected, remote=remote)
                actual = replace(pending, remote=remote)
                before = actual.index
                result = decision_for(owned, actual)
                self.assertEqual(result.status, RebaseStatus.WAITING)
                self.assertIsNone(result.acceptance)
                self.assertEqual(actual.index, before)
        for changes in (
            {'metadata': (('head-name', 'refs/heads/other'), ('orig-head', 'before'), ('onto', 'base'))},
            {'metadata': (('head-name', f'refs/heads/{expected.branch}'), ('orig-head', 'other'), ('onto', 'base'))},
            {'metadata': (('head-name', f'refs/heads/{expected.branch}'), ('orig-head', 'before'), ('onto', 'other'))},
            {'operations': (*pending.operations, ('MERGE_HEAD', 'merge'))},
            {'operations': (*pending.operations, ('rebase-apply', 'other'))},
            {'registration': ('HEAD=partial', f'branch=refs/heads/{expected.branch}')},
            {'registration': ('HEAD=partial', 'detached=')},
            {'registration': ('HEAD=partial', 'detached=False')},
            {'branch_ref': ('other', '')}, {'metadata': None},
            {'branch_worktrees': ('/other',)}, {'base_is_ancestor': False},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(decision_for(expected, replace(pending, **changes)).status,
                                 RebaseStatus.REJECTED)

    def test_rejects_identity_target_operation_and_lease_mismatches(self):
        expected, current = expectation(), observation()
        pr = PullRequest('pr/1', 'owner/repo', expected.branch, 'remote', 'OPEN')
        pr_expected = replace(expected, remote=replace(expected.remote, pull_request=pr))
        pr_current = replace(current, remote=pr_expected.remote)
        self.assertEqual(decision_for(pr_expected, pr_current).status, RebaseStatus.COMPLETE)
        for changes in (
            {'repo': '/other'}, {'repository_identity': '/other/.git'},
            {'worktree': '/other'}, {'identity_verified': False}, {'branch': 'other'},
            {'branch_ref': ('other', '')}, {'branch_ref': ('after', 'symbolic')},
            {'registration': None}, {'registration': ('HEAD=other',)},
            {'registration': ('HEAD=after', f'branch=refs/heads/{expected.branch}',
                              'detached=True')},
            {'branch_worktrees': ('/owned', '/other')},
            {'base_is_ancestor': False}, {'index_clean': False},
            {'operations': (('CHERRY_PICK_HEAD', 'commit'),)},
            {'input_fingerprint': 'new-inputs'}, {'scope_fingerprint': 'new-approval'},
            {'remote': replace(expected.remote, oid='racing-remote')},
            {'remote': replace(expected.remote, repository='other/repo')},
            {'remote': replace(expected.remote, branch='other')},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(decision_for(expected, replace(current, **changes)).status,
                                 RebaseStatus.REJECTED)
        for changes in ({'identity': 'pr/2'}, {'head': 'other'}, {'state': 'CLOSED'},
                        {'repository': 'other/repo'}, {'branch': 'other'}):
            actual = replace(pr_current, remote=replace(pr_expected.remote,
                                                       pull_request=replace(pr, **changes)))
            self.assertEqual(decision_for(pr_expected, actual).status, RebaseStatus.REJECTED)

    def test_missing_expectations_and_observations_are_not_clean_success(self):
        for changes in (
            {'remote': None}, {'workspace_owned': False}, {'target_base_oid': ''},
            {'repository_identity': ''}, {'scope_fingerprint': ''}, {'input_fingerprint': ''},
            {'plan_id': 'G-109'}, {'generation': -1}, {'role': 'reviewer'},
            {'plan_id': None}, {'remote': Remote('owner/repo', 'loop-manager/G-108', 123)},
        ):
            self.assertEqual(decision_for(expectation(**changes)).status, RebaseStatus.REJECTED)
        expected = expectation()
        for actual in (None, ObservationFailure('remote_query', 'offline')):
            self.assertEqual(assess_current_rebase(expected, actual, None).status,
                             RebaseStatus.UNAVAILABLE)
        for actual in (observation(remote=None), observation(base_is_ancestor=None),
                       observation(index_clean=None)):
            self.assertEqual(decision_for(expected, actual).status, RebaseStatus.UNAVAILABLE)
        confirmed_absence = expectation(remote=Remote('owner/repo', expected.branch, None))
        self.assertEqual(decision_for(confirmed_absence).status, RebaseStatus.COMPLETE)

    def test_scope_covers_final_dirty_staged_ignored_modes_and_real_flags(self):
        expected, current = expectation(), observation()
        forbidden = 'docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md'
        for changes in (
            {'committed': (replace(current.committed[0], path=forbidden),)},
            {'committed': (replace(current.committed[0], new_mode='040000'),)},
            {'staged': (replace(current.committed[0], path=forbidden),)},
            {'files': (WorktreeFile(forbidden, ('unstaged',), '100644', 'changed'),)},
            {'files': (WorktreeFile('docs/requirements/Protected.md', ('untracked',), '100644', 'new'),)},
            {'files': (WorktreeFile('docs/requirements/Protected.md', ('ignored',), '100644', 'hidden'),)},
            {'files': (WorktreeFile('../escape', ('untracked',), '100644', 'outside'),)},
            {'files': (WorktreeFile('.GIT/config', ('ignored',), '100644', 'outside'),)},
            {'index': (replace(current.index[0], mode='040000'),)},
            {'index': (replace(current.index[0], stage=1, tag='M', flags=0x1000),)},
            *({'index': (replace(current.index[0], flags=flag),)}
              for flag in (0x8000, 0x40000000, 0x20000000)),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(decision_for(expected, replace(current, **changes)).status,
                                 RebaseStatus.REJECTED)
        dirty_allowed = replace(current, files=(
            WorktreeFile('app.py', ('unstaged',), '100644', 'local'),
            WorktreeFile('cache.pyc', ('ignored',), '100644', 'artifact'),
        ))
        self.assertEqual(decision_for(expected, dirty_allowed).status, RebaseStatus.COMPLETE)
        stale_scope = scope_for(expected, current)
        self.assertEqual(assess_current_rebase(expected, dirty_allowed, stale_scope).status,
                         RebaseStatus.REJECTED)

    def test_guidance_and_archive_need_exact_current_contract(self):
        expected = expectation()
        guidance = observation(committed=(FileChange('AGENTS.md', '100644', '100644',
                                                     'old', 'approved', 'M'),))
        def contracts(actual):
            return actual.committed == guidance.committed and not actual.files
        path_allowed = lambda path: _worker_path_allowed(
            path, expected.plan_path, allowed_guidance_targets=('AGENTS.md',),
        )
        for actual, status in ((guidance, RebaseStatus.COMPLETE),
                               (replace(guidance, committed=(replace(guidance.committed[0],
                                                                    new_oid='unapproved'),)),
                                RebaseStatus.REJECTED)):
            scope = scope_for(expected, actual, path_allowed=path_allowed,
                              contracts_allowed=contracts)
            self.assertEqual(assess_current_rebase(expected, actual, scope).status, status)
        archives = observation(committed=(
            FileChange('docs/requirements/a.md', '100644', '000000', 'content', '0', 'D'),
            FileChange('docs/requirements/archive/a.md', '000000', '100644', '0', 'content', 'A'),
        ))
        path_allowed = lambda path: _worker_path_allowed(
            path, expected.plan_path, allowed_requirement_archives=('docs/requirements/a.md',),
        )
        archive_contract = lambda actual: actual.committed == archives.committed
        accepted = scope_for(expected, archives, path_allowed=path_allowed,
                             contracts_allowed=archive_contract)
        self.assertEqual(assess_current_rebase(expected, archives, accepted).status,
                         RebaseStatus.COMPLETE)
        self.assertEqual(assess_current_rebase(expected, archives,
                                              scope_for(expected, archives)).status,
                         RebaseStatus.REJECTED)


class CurrentRebaseObservationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        repo, worktree = root / 'repo', root / 'owned'
        common_dir = repo / '.git'
        common_dir.mkdir(parents=True)
        worktree.mkdir()
        self.expected = expectation(
            repo=str(repo.resolve()), repository_identity=str(common_dir.resolve()),
            worktree=str(worktree.resolve()),
        )

    def adapters(self):
        expected = self.expected
        snapshot = SimpleNamespace(
            head='after', branch=expected.branch, index={'app.py': ('H 100644 blob 0',)},
            dirty={}, refs={f'refs/heads/{expected.branch}': ('after', '')},
            worktrees={expected.worktree: ('HEAD=after', f'branch=refs/heads/{expected.branch}')},
            config={}, operations={}, shared_metadata_complete=True,
        )
        def git(_context, *args, **_kwargs):
            if args[0] == 'rev-parse':
                output = expected.repository_identity + '\n' if '--git-common-dir' in args else 'base\n'
            elif args[:2] == ('ls-files', '--debug'):
                output = ('app.py\0  ctime: 1:1\n  mtime: 1:1\n  dev: 1\tino: 1\n'
                          '  uid: 1\tgid: 1\n  size: 1\tflags: 0\n')
            elif args[:2] == ('ls-files', '--others'):
                output = 'ignored.pyc\0'
            elif args[0] == 'diff' and '--raw' in args and '--cached' not in args:
                output = ':100644 100644 old blob M\0app.py\0'
            elif args[0] in {'diff', 'merge-base'}:
                output = ''
            else:
                self.fail(f'unexpected Git query {args}')
            return SimpleNamespace(returncode=0, stdout=output)
        return ObservationAdapters(
            git=mock.Mock(side_effect=git), capture_snapshot=mock.Mock(return_value=snapshot),
            validate_identity=mock.Mock(), read_rebase_metadata=mock.Mock(return_value=None),
            read_remote=mock.Mock(return_value=expected.remote),
            read_inputs=mock.Mock(return_value=('inputs', 'worker-contract')),
            read_file=mock.Mock(return_value=('100644', 'artifact')),
            path_allowed=lambda path: _worker_path_allowed(path, expected.plan_path),
            contracts_allowed=lambda _actual: True,
        )

    def test_adapter_reads_final_modes_ignored_files_and_flags_without_history(self):
        expected, adapters = self.expected, self.adapters()
        actual, scope = observe_current_rebase(expected, adapters)
        self.assertIsInstance(actual, RebaseObservation)
        for path in (actual.repo, actual.worktree, actual.repository_identity):
            self.assertTrue(Path(path).is_absolute())
            self.assertEqual(path, str(Path(path).resolve()))
        self.assertEqual(actual.repository_identity, expected.repository_identity)
        self.assertEqual(actual.registration,
                         ('HEAD=after', f'branch=refs/heads/{expected.branch}'))
        self.assertEqual(actual.branch_worktrees, (expected.worktree,))
        for context in (expected.repo, expected.worktree):
            adapters.git.assert_any_call(
                Path(context), 'rev-parse', '--path-format=absolute',
                '--git-common-dir', check=False,
            )
        self.assertEqual(actual.files[0].kinds, ('ignored',))
        self.assertEqual(actual.index[0].flags, 0)
        self.assertEqual(actual.committed[0].new_oid, 'blob')
        self.assertEqual(assess_current_rebase(expected, actual, scope).status,
                         RebaseStatus.COMPLETE)
        forbidden_queries = {'reflog', 'rev-list', 'range-diff', 'rebase', 'add', 'commit'}
        self.assertFalse(any(call.args[1] in forbidden_queries for call in adapters.git.call_args_list))

    def test_proven_ignored_creation_modification_and_deletion_do_not_change_rebase_acceptance(self):
        artifact = 'docs/requirements/arbitrary.asset'
        for before_paths, after_paths in (((), (artifact,)), ((artifact,), (artifact,)), ((artifact,), ())):
            for mutation in (None, 'rule', 'config', 'missing-evidence'):
                with self.subTest(paths=(before_paths, after_paths), mutation=mutation):
                    adapters = self.adapters()
                    snapshot = GitSnapshot(**vars(adapters.capture_snapshot.return_value))
                    snapshot.index['.gitignore'] = ('H 100644 rule-blob 0',)
                    second = replace(snapshot, index=dict(snapshot.index), config=dict(snapshot.config))
                    if mutation == 'rule':
                        second.index['.gitignore'] = ('H 100644 new-rule 0',)
                    elif mutation == 'config':
                        second.config['ignore-source:repository-exclude'] = ('exclude', 'new-rule')
                    elif mutation == 'missing-evidence':
                        snapshot.index.pop('.gitignore')
                        second.index.pop('.gitignore')
                    calls = []
                    def capture(_worktree, *, details):
                        current = snapshot if not calls else second
                        paths = before_paths if not calls else after_paths
                        calls.append(current)
                        details.update(index_debug=''.join(
                            path + '\0  ctime: 1:1\n  mtime: 1:1\n  dev: 1\tino: 1\n'
                            '  uid: 1\tgid: 1\n  size: 1\tflags: 0\n' for path in current.index),
                            staged_raw='', ignored_paths=paths)
                        return current
                    def check_ignore(_command, *, input, **kwargs):
                        raw = b''.join(b'.gitignore\0001\000*\000' + path + b'\0'
                                       for path in input.rstrip(b'\0').split(b'\0'))
                        return SimpleNamespace(returncode=0, stdout=raw)
                    effects = SimpleNamespace(git=adapters.git, _handoff_run=check_ignore)
                    adapters = replace(adapters, capture_snapshot=capture,
                        read_file=lambda *_args: ('100644', f'contents-{len(calls)}' if _args[1] == artifact else 'stable-tracked'),
                        normalize_ignored_snapshot=lambda *args, **kwargs:
                        scope_observation._normalize_legacy_ignored_snapshot(*args, effects=effects, **kwargs))
                    actual, scope = observe_current_rebase(self.expected, adapters)
                    if mutation is None:
                        self.assertIsInstance(actual, RebaseObservation)
                        self.assertEqual(actual.files, ())
                        self.assertTrue(scope.allowed)
                    else:
                        self.assertIsInstance(actual, ObservationFailure)
                        self.assertEqual(actual.area, 'state_changed')
                        self.assertIsNone(scope)

    def test_file_reader_receives_each_fresh_observations_index_mode(self):
        adapters = self.adapters()
        snapshot = adapters.capture_snapshot.return_value
        snapshots = iter((snapshot, SimpleNamespace(**{**vars(snapshot),
            'index': {'app.py': ('H 100755 blob 0',)}})))
        adapters.capture_snapshot.side_effect = lambda *_args, **_kwargs: next(snapshots)
        actual, scope = observe_current_rebase(self.expected, adapters)
        self.assertIsInstance(actual, ObservationFailure)
        self.assertEqual(actual.area, 'state_changed')
        self.assertIsNone(scope)
        root = Path(self.expected.worktree)
        self.assertEqual(adapters.read_file.call_args_list, [
            mock.call(root, 'app.py', '100644'), mock.call(root, 'ignored.pyc', None),
            mock.call(root, 'app.py', '100755'), mock.call(root, 'ignored.pyc', None)])

    def test_snapshot_details_remove_duplicate_queries_but_recapture_both_observations(self):
        adapters = self.adapters()
        legacy, scope = observe_current_rebase(self.expected, adapters)
        debug = adapters.git(Path(self.expected.worktree), 'ls-files', '--debug', '-z').stdout
        buffers = []
        def capture(worktree, *, details):
            buffers.append(details)
            details.update(index_debug=debug, staged_raw='', ignored_paths=('ignored.pyc',))
            return adapters.capture_snapshot.return_value
        optimized = replace(adapters, capture_snapshot=capture)
        adapters.git.reset_mock()
        actual, actual_scope = observe_current_rebase(self.expected, optimized)
        self.assertEqual((actual, actual_scope), (legacy, scope))
        self.assertEqual(len(buffers), 2)
        self.assertIsNot(buffers[0], buffers[1])
        self.assertFalse(any(call.args[1] == 'ls-files' or '--cached' in call.args
                             for call in adapters.git.call_args_list))
        buffers.clear()
        def changing_capture(worktree, *, details):
            snapshot = capture(worktree, details=details)
            if len(buffers) == 2:
                details['staged_raw'] = ':100644 100644 old blob M\0app.py\0'
            return snapshot
        failed, scope = observe_current_rebase(self.expected,
            replace(adapters, capture_snapshot=changing_capture))
        self.assertIsInstance(failed, ObservationFailure)
        self.assertEqual(failed.area, 'state_changed')
        self.assertIsNone(scope)

    def test_new_base_observation_uses_common_ancestor_with_retained_auxiliary_files(self):
        expected, adapters = self.expected, self.adapters()
        snapshot = adapters.capture_snapshot.return_value
        snapshot.head = expected.before_head
        snapshot.refs[f'refs/heads/{expected.branch}'] = (expected.before_head, '')
        snapshot.worktrees[expected.worktree] = (f'HEAD={expected.before_head}',
                                               f'branch=refs/heads/{expected.branch}')
        snapshot.operations = {'REBASE_HEAD': 'last-commit', 'MERGE_MSG': 'retained-message'}
        original_git = adapters.git.side_effect
        def git(context, *args, **kwargs):
            result = original_git(context, *args, **kwargs)
            if args[:2] == ('merge-base', '--is-ancestor'):
                result.returncode = 1
            elif args == ('merge-base', expected.target_base_oid, expected.before_head):
                result.stdout = 'common-base'
            elif args[0] == 'diff' and '--raw' in args and '--cached' not in args:
                if args[-3] != 'common-base':
                    # The new base's protected change is not part of this branch.
                    result.stdout = ':100644 000000 base-blob 0 D\0docs/ARCHITECTURE.md\0'
            return result
        adapters.git.side_effect = git
        actual, scope = observe_current_rebase(expected, adapters)
        self.assertIsInstance(actual, RebaseObservation)
        self.assertEqual([change.path for change in actual.committed], ['app.py'])
        self.assertEqual(assess_current_rebase(expected, actual, scope).status, RebaseStatus.START)
        adapters.git.assert_any_call(Path(expected.worktree), 'merge-base',
                                    expected.target_base_oid, expected.before_head, check=False)
        self.assertEqual(snapshot.operations,
                         {'REBASE_HEAD': 'last-commit', 'MERGE_MSG': 'retained-message'})

    def test_adapter_preserves_actual_detached_snapshot_and_waits_for_resolution(self):
        expected = self.expected
        for operation in ('rebase-merge', 'rebase-apply'):
            for conflicted in (False, True):
                with self.subTest(operation=operation, conflicted=conflicted):
                    adapters = self.adapters()
                    stages = (1, 2, 3) if conflicted else (0,)
                    index = {'app.py': tuple(
                        f"{'M' if stage else 'H'} 100644 blob-{stage} {stage}"
                        for stage in stages
                    )}
                    dirty = {'app.py': (('staged',), 'artifact')} if conflicted else {}
                    original_git = adapters.git.side_effect
                    def git(context, *args, **kwargs):
                        result = original_git(context, *args, **kwargs)
                        if args[:2] == ('ls-files', '--debug'):
                            result.stdout = ''.join(
                                'app.py\0  ctime: 1:1\n  mtime: 1:1\n  dev: 1\tino: 1\n'
                                f'  uid: 1\tgid: 1\n  size: 1\tflags: {stage << 12:x}\n'
                                for stage in stages
                            )
                        elif args[0] == 'diff' and '--cached' in args:
                            if '--quiet' in args:
                                result.returncode = int(conflicted)
                            elif '--raw' in args and conflicted:
                                result.stdout = ':100644 000000 old 0 U\0app.py\0'
                        return result
                    adapters.git.side_effect = git
                    adapters.read_rebase_metadata.return_value = {
                        'head-name': f'refs/heads/{expected.branch}',
                        'orig-head': expected.before_head, 'onto': expected.target_base_oid,
                    }
                    adapters = replace(adapters, capture_snapshot=capture_git_snapshot)
                    # Run the real catalog parser and snapshot serializer. Only
                    # Git reads and unrelated snapshot readers are replaced.
                    porcelain = f'worktree {expected.worktree}\nHEAD partial\ndetached\n\n'
                    with (
                        mock.patch('loop_manager.git', return_value=SimpleNamespace(stdout=porcelain)),
                        mock.patch('loop_manager_internal.agents.scope_observation._capture_local_worktree_state',
                                   return_value=('partial', None, index, dirty)),
                        mock.patch('loop_manager_internal.agents.scope_observation._shared_ref_entries', return_value={
                            f'refs/heads/{expected.branch}': (expected.before_head, ''),
                        }),
                        mock.patch('loop_manager_internal.agents.scope_observation._config_entries', return_value={}),
                        mock.patch('loop_manager_internal.agents.scope_observation._git_operation_entries',
                                   return_value={operation: 'metadata-signature'}),
                    ):
                        actual, scope = observe_current_rebase(expected, adapters)
                    self.assertIsInstance(actual, RebaseObservation)
                    self.assertEqual(actual.registration, ('HEAD=partial', 'detached=True'))
                    self.assertIsNone(actual.branch)
                    self.assertEqual(tuple(entry.stage for entry in actual.index), stages)
                    result = assess_current_rebase(expected, actual, scope)
                    self.assertEqual(result.status, RebaseStatus.WAITING)
                    self.assertIsNone(result.acceptance)
                    self.assertEqual(index['app.py'], tuple(
                        f"{'M' if stage else 'H'} 100644 blob-{stage} {stage}"
                        for stage in stages
                    ))

    def test_adapter_reports_query_failures_missing_flags_and_remote_races(self):
        expected = self.expected
        for configure in (
            lambda adapters: setattr(adapters.read_remote, 'side_effect', RuntimeError('offline')),
            lambda adapters: setattr(adapters.validate_identity, 'side_effect', RuntimeError('other worktree')),
            lambda adapters: setattr(adapters.read_inputs, 'side_effect', [('inputs', 'worker-contract'),
                                                                        ('changed', 'worker-contract')]),
            lambda adapters: setattr(adapters.read_remote, 'side_effect', [expected.remote,
                                                                           replace(expected.remote, oid='race')]),
            lambda adapters: setattr(adapters.git, 'side_effect', lambda *_args, **_kwargs:
                                     SimpleNamespace(returncode=128, stdout='')),
        ):
            with self.subTest(configure=configure):
                adapters = self.adapters()
                configure(adapters)
                actual, scope = observe_current_rebase(expected, adapters)
                self.assertIsInstance(actual, ObservationFailure)
                self.assertIsNone(scope)
        with self.assertRaises(ValueError):
            parse_index({'app.py': ('H 100644 blob 0',)}, '')
        with self.assertRaises(ValueError):
            parse_raw_changes(':100644 100644 old new M\0truncated')

    def test_adapter_rejects_noncanonical_repository_and_worktree_paths(self):
        expected = self.expected
        for field in ('repo', 'worktree'):
            with self.subTest(field=field):
                path = Path(getattr(expected, field))
                noncanonical = str(path / '..' / path.name)
                self.assertNotEqual(noncanonical, str(Path(noncanonical).resolve()))
                adapters = self.adapters()
                actual, scope = observe_current_rebase(
                    replace(expected, **{field: noncanonical}), adapters,
                )
                self.assertIsInstance(actual, ObservationFailure)
                self.assertIsNone(scope)
                adapters.validate_identity.assert_not_called()
                adapters.git.assert_not_called()

    def test_debug_flags_and_unusual_paths_cannot_be_hidden_by_v_tag(self):
        debug = ('weird\nname.py\0  ctime: 1:1\n  mtime: 1:1\n  dev: 1\tino: 1\n'
                 '  uid: 1\tgid: 1\n  size: 1\tflags: 20000000\n')
        entries = parse_index({'weird\nname.py': ('H 100644 blob 0',)}, debug)
        self.assertEqual(entries[0].flags, 0x20000000)
        actual = observation(index=entries)
        self.assertEqual(decision_for(observed=actual).status, RebaseStatus.REJECTED)

    def test_adapter_detects_hidden_tracked_mode_change(self):
        expected = self.expected
        adapters = self.adapters()
        snapshot = adapters.capture_snapshot.return_value
        protected = 'docs/requirements/Protected.md'
        snapshot.index = {protected: ('H 100644 blob 0',)}
        original_git = adapters.git.side_effect
        def git(context, *args, **kwargs):
            result = original_git(context, *args, **kwargs)
            if args[:2] == ('ls-files', '--debug'):
                result.stdout = result.stdout.replace('app.py\0', protected + '\0')
            return result
        adapters.git.side_effect = git
        adapters.read_file.side_effect = lambda _worktree, path, index_mode: (
            '100755' if path == protected else '100644', 'current-mode',
        )
        actual, scope = observe_current_rebase(expected, adapters)
        self.assertEqual(actual.files[0].path, protected)
        self.assertEqual(actual.files[0].kinds, ('unstaged',))
        self.assertEqual(assess_current_rebase(expected, actual, scope).status,
                         RebaseStatus.REJECTED)
