"""Bulk fetch policy uses fake Git observations and the real durable journal."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import subprocess
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.runtime.resource_lock import resource_locks


class ProjectFetchBranchDeletionTest(unittest.TestCase):
    repo = Path('/repo')
    oid = 'a' * 40
    old = 'b' * 40
    branch = 'loop-manager/G-115'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name) / 'state'
        self.fetch_head = Path(temporary.name) / 'FETCH_HEAD'
        self.remote = {'main': self.oid}
        self.tracking = {'main': self.old}
        self.reflogs = {}
        self.symbolic = {}
        self.on_prepare = None
        self.on_fetch = None
        self.fetches = []
        self.updates = []
        self.git = self.enterContext(mock.patch.object(loop_manager, 'git', side_effect=self.command))
        self.enterContext(mock.patch.object(loop_manager, '_tracking_ref_oid',
            side_effect=lambda repo, ref: self.tracking.get(ref.removeprefix('refs/remotes/origin/'))))

    def result(self, stdout='', returncode=0):
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    def command(self, repo, *args, check=True, input=None, **kwargs):
        if args[:3] == ('ls-remote', '--heads', 'origin'):
            branches = self.remote if len(args) == 3 else {
                name: oid for name, oid in self.remote.items() if args[3] == 'refs/heads/' + name}
            return self.result(''.join(f'{oid}\trefs/heads/{branch}\n' for branch, oid in branches.items()))
        if args[:1] == ('symbolic-ref',):
            target = self.symbolic.get(args[-1])
            return self.result(target or '', 0 if target else 1)
        if args[0] == 'fetch':
            names = ([ref.removeprefix('refs/heads/') for ref in input.splitlines()]
                     if input is not None else [args[-1].removeprefix('refs/heads/')])
            self.fetches.append(names)
            if self.on_fetch:
                result = self.on_fetch()
                if result is not None:
                    return result
            if any(name not in self.remote for name in names):
                return self.result('missing remote ref', 128)
            self.fetch_head.write_text(''.join(
                f"{self.remote[name]}\t\tbranch '{name}' of fixture\n" for name in names), encoding='utf-8')
            return self.result()
        if args == ('rev-parse', '--git-path', 'FETCH_HEAD'):
            return self.result(str(self.fetch_head))
        if args[:2] == ('rev-parse', '--verify'):
            if args[2] == 'FETCH_HEAD^{commit}':
                return self.result(self.fetch_head.read_text().split('\t')[0])
            return self.result(self.tracking[args[2].removeprefix('refs/remotes/origin/').removesuffix('^{commit}')])
        if args[:2] == ('cat-file', '-e'):
            return self.result()
        if args[0] == 'update-ref':
            self.assertEqual(args[-2:], ('--no-deref', '--stdin'))
            start, update, prepare = input.splitlines()
            self.assertEqual((start, prepare), ('start', 'prepare'))
            _, ref, selected, old = update.split()
            branch = ref.removeprefix('refs/remotes/origin/')
            if self.on_prepare:
                self.on_prepare()
            self.assertEqual(self.tracking.get(branch) or '0' * 40, old)
            kwargs['before_commit']()
            self.tracking[branch] = selected
            self.reflogs[branch] = f'{selected}\0{args[3]}\n'
            self.updates.append(branch)
            return self.result()
        if args[:2] == ('reflog', 'show'):
            return self.result(self.reflogs.get(args[-1].removeprefix('refs/remotes/origin/'), ''))
        self.fail(f'Unexpected Git command: {args}')

    def run_fetch(self):
        return loop_manager.remote_base(self.repo, 'main', state_dir=self.state_dir)

    def queries(self):
        return [call for call in self.git.call_args_list if call.args[1] == 'ls-remote']

    def test_multiple_branches_use_one_query_and_one_receive(self):
        self.remote[self.branch] = 'c' * 40
        self.remote["feature/한글's"] = 'd' * 40
        self.assertEqual(self.run_fetch(), 'main')
        self.assertEqual(self.tracking, self.remote)
        self.assertEqual(len(self.queries()), 1)
        self.assertEqual(len(self.fetches), 1)
        call = next(call for call in self.git.call_args_list if call.args[1] == 'fetch')
        for option in ('--stdin', '--refmap=', '--no-tags', '--no-prune', '--no-prune-tags', '--recurse-submodules=no'):
            self.assertIn(option, call.args)
        self.assertTrue(all(':' not in ref for ref in call.kwargs['input'].splitlines()))

    def test_equal_branches_skip_receive_but_enter_journal(self):
        self.tracking = dict(self.remote)
        self.assertEqual(self.run_fetch(), 'main')
        self.assertEqual(len(self.queries()), 1)
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.updates, [])
        self.assertTrue(list((self.state_dir / 'manager-git-journal').glob('*/*.result.json')))

    def test_deleted_listed_pr_branch_does_not_abort_base_fetch(self):
        self.remote[self.branch] = self.oid
        self.tracking[self.branch] = self.old
        self.on_fetch = lambda: (self.remote.pop(self.branch, None), None)[1]
        self.assertEqual(self.run_fetch(), 'main')
        self.assertEqual(self.tracking, {'main': self.oid, self.branch: self.old})
        self.assertEqual(len(self.queries()), 2)
        self.assertEqual(len(self.fetches), 2)

    def test_deleted_base_still_fails(self):
        self.on_fetch = lambda: (self.remote.clear(), None)[1]
        with self.assertRaises(loop_manager.RemoteBranchMissingError):
            self.run_fetch()
        self.assertEqual(self.tracking, {'main': self.old})
        self.assertEqual(self.updates, [])

    def test_missing_base_in_list_still_fails(self):
        self.remote.clear()
        with self.assertRaises(loop_manager.RemoteBranchMissingError):
            self.run_fetch()
        self.assertEqual(self.fetches, [])

    def test_invalid_and_duplicate_remote_responses_are_not_skipped(self):
        for response in ('invalid-response', f'not-an-oid\trefs/heads/main\n',
                         f'{self.oid}\trefs/tags/other\n',
                         f'{self.oid}\trefs/heads/main:refs/heads/unowned\n',
                         f'{self.oid}\trefs/heads/../main\n',
                         f'{self.oid}\trefs/heads/main\n' * 2):
            with self.subTest(response=response), mock.patch.object(loop_manager, 'git', return_value=self.result(response)):
                with self.assertRaises(RuntimeError):
                    self.run_fetch()
        self.assertEqual(self.updates, [])

    def test_direct_required_branch_fetch_still_fails_when_deleted(self):
        self.remote.clear()
        with self.assertRaises(loop_manager.RemoteBranchMissingError):
            loop_manager._recorded_base_fetch(self.repo, self.state_dir, self.branch, owner='project')
        self.assertEqual(self.fetches, [])

    def test_branch_list_failure_is_not_skipped(self):
        with mock.patch.object(loop_manager, 'git', side_effect=RuntimeError('network')):
            with self.assertRaisesRegex(RuntimeError, 'network'):
                self.run_fetch()
        self.assertEqual(self.updates, [])

    def test_failed_receive_does_not_adopt_old_fetch_head(self):
        self.fetch_head.write_text(f"{self.oid}\t\tbranch 'main' of fixture\n")
        self.on_fetch = lambda: self.result('authentication failed', 128)
        with self.assertRaises(RecoveryFailure) as error:
            self.run_fetch()
        self.assertEqual(error.exception.reason, RecoveryReason.FETCH_FAILED)
        self.assertIn('authentication failed', str(error.exception))
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.updates, [])
        self.assertFalse(any(call.args[1:] == ('rev-parse', '--git-path', 'FETCH_HEAD')
                             for call in self.git.call_args_list))

    def test_remote_oid_race_is_reobserved_before_ref_publication(self):
        self.on_fetch = lambda: self.remote.update(main='c' * 40)
        self.run_fetch()
        self.assertEqual(self.tracking['main'], 'c' * 40)
        self.assertEqual(self.updates, ['main'])
        self.assertEqual(len(self.queries()), 2)
        self.assertEqual(len(self.fetches), 2)

    def test_continuous_remote_oid_race_is_bounded_and_preserves_refs(self):
        self.on_fetch = lambda: self.remote.update(main=('c' if len(self.fetches) == 1 else 'd') * 40)
        with self.assertRaises(RecoveryFailure) as error:
            self.run_fetch()
        self.assertEqual(error.exception.reason, RecoveryReason.REMOTE_SNAPSHOT_RACE)
        self.assertEqual(len(self.queries()), 2)
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(self.updates, [])

    def test_advancing_other_task_does_not_hold_selected_base(self):
        self.remote[self.branch] = self.old
        self.on_fetch = lambda: self.remote.update({self.branch: 'c' * 40})
        self.run_fetch()
        self.assertEqual(self.tracking, {'main': self.oid, self.branch: 'c' * 40})
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(len(self.queries()), 1)

    def test_invalid_duplicate_and_missing_receive_entries_preserve_refs(self):
        for text in ('invalid', f"{self.oid}\t\tbranch 'main' of fixture\n" * 2, ''):
            def receive():
                self.fetch_head.write_text(text)
                return self.result()
            with self.subTest(text=text):
                self.on_fetch = receive
                with self.assertRaises((RuntimeError, RecoveryFailure)):
                    self.run_fetch()
        self.assertEqual(self.updates, [])

    def test_old_single_branch_pending_receipt_recovers_when_remote_is_equal(self):
        with mock.patch.object(loop_manager.manager_git_journal, '_finish', side_effect=RuntimeError('crash')):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                loop_manager._recorded_base_fetch(self.repo, self.state_dir, 'main', owner='project')
        self.assertEqual(self.updates, ['main'])
        self.run_fetch()
        self.assertEqual(self.updates, ['main'])
        self.assertEqual(len(self.fetches), 1)
        self.assertFalse(list((self.state_dir / 'manager-git-journal').glob('*/active.json')))

    def test_bulk_fetch_respects_existing_fetch_lease(self):
        with resource_locks(self.state_dir, str(self.repo.resolve()), ['git:fetch'], owner='other-fetch'):
            with self.assertRaises(loop_manager.ProcessLockBusyError):
                self.run_fetch()
        self.git.assert_not_called()

    def test_external_ref_update_during_receive_is_not_overwritten(self):
        self.on_fetch = lambda: self.tracking.update(main='c' * 40)
        with self.assertRaises(loop_manager.manager_git_journal.MutationEvidenceError):
            self.run_fetch()
        self.assertEqual(self.tracking['main'], 'c' * 40)
        self.assertEqual(self.updates, [])

    def test_long_branch_list_uses_standard_input(self):
        self.remote.update({f'feature/{index:04d}/' + 'x' * 80: self.oid for index in range(500)})
        with mock.patch.object(loop_manager.manager_git_journal, 'execute') as journal:
            self.run_fetch()
        call = next(call for call in self.git.call_args_list if call.args[1] == 'fetch')
        self.assertGreater(len(call.kwargs['input']), 32767)
        self.assertLess(sum(len(str(arg)) for arg in call.args), 1000)
        self.assertEqual(journal.call_count, len(self.remote))

    def test_command_transports_utf8_lf_input_without_windows_text_translation(self):
        contents = "refs/heads/main\nrefs/heads/한글\n"
        with mock.patch.object(loop_manager, '_handoff_run',
                return_value=subprocess.CompletedProcess([], 0, b'received\r\n')) as run:
            result = loop_manager.command(['git', 'fetch', '--stdin'], input=contents)
        self.assertEqual(run.call_args.kwargs['input'], contents.encode('utf-8'))
        self.assertNotIn('text', run.call_args.kwargs)
        self.assertNotIn('encoding', run.call_args.kwargs)
        self.assertEqual(result.stdout, 'received\n')

    def test_symbolic_targets_and_dangling_aliases_are_preserved(self):
        ref = 'refs/remotes/origin/main'
        for target in ('refs/heads/user-work', 'refs/remotes/origin/other',
                       'refs/shared/user', 'refs/heads/missing'):
            with self.subTest(target=target):
                self.symbolic[ref] = target
                with self.assertRaises(loop_manager.manager_git_journal.MutationEvidenceError):
                    self.run_fetch()
                self.assertEqual(self.symbolic[ref], target)
                self.assertEqual(self.updates, [])
                self.assertEqual(self.fetches, [])
        with self.assertRaises(loop_manager.manager_git_journal.MutationEvidenceError):
            loop_manager._recorded_base_fetch(self.repo, self.state_dir, 'main', owner='project')

    def test_same_oid_alias_race_keeps_intent_and_completed_ref(self):
        self.remote = {'a-direct': self.oid, 'main': self.oid}
        self.tracking['a-direct'] = self.old
        ref = 'refs/remotes/origin/main'
        def race():
            if self.updates:
                self.symbolic[ref] = 'refs/heads/user-work'
        self.on_prepare = race
        with self.assertRaises(loop_manager.manager_git_journal.MutationEvidenceError):
            self.run_fetch()
        self.assertEqual(self.updates, ['a-direct'])
        self.assertEqual(self.tracking, {'a-direct': self.oid, 'main': self.old})
        active = next((self.state_dir / 'manager-git-journal').glob('*/active.json'))
        saved = active.read_bytes()
        with self.assertRaises(loop_manager.manager_git_journal.MutationEvidenceError):
            self.run_fetch()
        self.assertEqual(active.read_bytes(), saved)
        self.assertEqual(self.symbolic[ref], 'refs/heads/user-work')

    def test_pending_legacy_receipt_does_not_accept_same_oid_alias(self):
        with mock.patch.object(loop_manager.manager_git_journal, '_finish', side_effect=RuntimeError('crash')):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                loop_manager._recorded_base_fetch(self.repo, self.state_dir, 'main', owner='project')
        active = next((self.state_dir / 'manager-git-journal').glob('*/active.json'))
        saved = active.read_bytes()
        self.symbolic['refs/remotes/origin/main'] = 'refs/heads/user-work'
        with self.assertRaises(loop_manager.manager_git_journal.MutationEvidenceError):
            self.run_fetch()
        self.assertEqual(active.read_bytes(), saved)
        self.assertEqual(self.updates, ['main'])

    def test_transaction_protocol_commits_only_after_the_locked_check(self):
        import io
        from loop_manager_internal.git import command_adapter
        for response, failure in ((b'start: ok\nprepare: ok\n', None),
                                  (b'start: ok\r\nprepare: ok\r\n', None),
                                  (b'start: ok\nfatal: cannot lock ref\n', None),
                                  (b'start: ok\nprepare: ok\n', RuntimeError('raw alias'))):
            with self.subTest(response=response, failure=failure):
                process = mock.MagicMock()
                process.__enter__.return_value = process
                process.stdin = io.BytesIO()
                process.stdout = io.BytesIO(response)
                process.communicate.return_value = (b'commit: ok\n', None)
                process.returncode = 0
                checked = mock.Mock(side_effect=failure)
                with mock.patch.object(command_adapter, '_handoff_popen', return_value=process):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, 'raw alias'):
                            loop_manager.command(['git', 'update-ref', '--stdin'],
                                input='start\nprepare\n', before_commit=checked)
                        process.communicate.assert_called_once_with()
                    elif b'fatal' in response:
                        with self.assertRaisesRegex(RuntimeError, 'cannot lock ref'):
                            loop_manager.command(['git', 'update-ref', '--stdin'],
                                input='start\nprepare\n', before_commit=checked)
                        checked.assert_not_called()
                        process.communicate.assert_called_once_with()
                    else:
                        loop_manager.command(['git', 'update-ref', '--stdin'],
                            input='start\nprepare\n', before_commit=checked)
                        checked.assert_called_once_with()
                        process.communicate.assert_called_once_with(b'commit\n')
                self.assertEqual(process.stdin.getvalue(), b'start\nprepare\n')

    def test_raw_ref_query_failure_is_not_treated_as_absence(self):
        original = self.command
        def broken(repo, *args, **kwargs):
            if args[:1] == ('symbolic-ref',):
                return self.result('fatal: invalid reference', 128)
            return original(repo, *args, **kwargs)
        self.git.side_effect = broken
        with self.assertRaisesRegex(loop_manager.manager_git_journal.MutationEvidenceError, 'invalid reference'):
            self.run_fetch()
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.updates, [])

    def test_prepared_ref_check_stays_inside_project_git_turn(self):
        from contextlib import contextmanager
        from loop_manager_internal.git import command_adapter
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                events = []
                @contextmanager
                def turn(repo, command, arguments):
                    self.assertEqual((repo, command, arguments),
                        (self.repo, 'update-ref', ('--no-deref', '--stdin')))
                    events.append('enter')
                    try:
                        yield
                    finally:
                        events.append('exit')
                def check():
                    self.assertEqual(events, ['enter', 'command'])
                    events.append('check')
                    if rejected:
                        raise RuntimeError('raw alias')
                def command(arguments, **kwargs):
                    self.assertEqual(arguments,
                        ['git', '-C', str(self.repo), 'update-ref', '--no-deref', '--stdin'])
                    self.assertEqual(kwargs['input'], 'start\nprepare\n')
                    events.append('command')
                    kwargs['before_commit']()
                    events.append('commit')
                    return self.result()
                effects = SimpleNamespace(command=command)
                with mock.patch('loop_manager_internal.runtime.resource_lock.git_command_turn', turn):
                    if rejected:
                        with self.assertRaisesRegex(RuntimeError, 'raw alias'):
                            command_adapter.git(effects, self.repo, 'update-ref', '--no-deref', '--stdin',
                                input='start\nprepare\n', before_commit=check)
                    else:
                        command_adapter.git(effects, self.repo, 'update-ref', '--no-deref', '--stdin',
                            input='start\nprepare\n', before_commit=check)
                self.assertEqual(events,
                    ['enter', 'command', 'check'] + ([] if rejected else ['commit']) + ['exit'])

    def test_hook_diagnostics_are_separate_from_transaction_responses(self):
        import io
        from loop_manager_internal.git import command_adapter
        diagnostic = b'hook prepared diagnostic\n' * 8192
        for prepared, rejected in ((True, False), (True, True), (False, False)):
            with self.subTest(prepared=prepared, rejected=rejected):
                process = mock.MagicMock()
                process.__enter__.return_value = process
                process.stdin = io.BytesIO()
                process.stdout = io.BytesIO(b'start: ok\n' + (b'prepare: ok\n' if prepared else b''))
                process.communicate.return_value = (b'commit: ok\n' if prepared else b'', None)
                process.returncode = 0 if prepared else 128
                def spawn(*args, **kwargs):
                    self.assertEqual(kwargs['stdout'], subprocess.PIPE)
                    kwargs['stderr'].write(diagnostic)
                    return process
                checked = mock.Mock(side_effect=RuntimeError('raw alias') if rejected else None)
                with mock.patch.object(command_adapter, '_handoff_popen', side_effect=spawn):
                    if rejected:
                        with self.assertRaisesRegex(RuntimeError, 'raw alias') as caught:
                            loop_manager.command(['git', 'update-ref', '--stdin'],
                                input='start\nprepare\n', before_commit=checked)
                        self.assertIn(diagnostic.decode(), caught.exception.__notes__)
                        process.communicate.assert_called_once_with()
                    else:
                        result = loop_manager.command(['git', 'update-ref', '--stdin'], check=False,
                            input='start\nprepare\n', before_commit=checked)
                        self.assertTrue(result.stdout.endswith(diagnostic.decode()))
                        self.assertEqual(result.returncode, 0 if prepared else 128)
                        if prepared:
                            checked.assert_called_once_with()
                            process.communicate.assert_called_once_with(b'commit\n')
                        else:
                            checked.assert_not_called()
                            process.communicate.assert_called_once_with()
