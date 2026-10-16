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
            branch = args[4].removeprefix('refs/remotes/origin/')
            self.assertEqual(self.tracking.get(branch) or '0' * 40, args[6])
            self.tracking[branch] = args[5]
            self.reflogs[branch] = f'{args[5]}\0{args[3]}\n'
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
