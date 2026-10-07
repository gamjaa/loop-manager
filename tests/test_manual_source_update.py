"""Unit recovery cases with modeled Git ancestry and the durable journal."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.git import manager_git_journal as journal
from loop_manager_internal.git.manual_source_update import update_source


BASE, INCOMING, CANDIDATE, LATER, NEWER, REWRITTEN = [c * 40 for c in 'abcdef']


class ManualSourceUpdateRecoveryTests(unittest.TestCase):
    def fixture(self, directory):
        group = dict(pending_source_head=INCOMING, prepared_head=BASE,
                     source_update_candidate=CANDIDATE, source_update_remote_oid=BASE,
                     source_update_conflict=0, branch='loop-manager/G-201',
                     owner_id='G-201', members='["G-201", "G-202"]', request_id='same-request')
        remote = {'oid': CANDIDATE}
        ancestors = {BASE: {BASE}, INCOMING: {INCOMING},
                     CANDIDATE: {BASE, INCOMING, CANDIDATE},
                     LATER: {BASE, INCOMING, CANDIDATE, LATER},
                     NEWER: {BASE, INCOMING, CANDIDATE, LATER, NEWER},
                     REWRITTEN: {BASE, REWRITTEN}}

        def git(_path, *args, **_kwargs):
            if args[0] == 'merge-base':
                return SimpleNamespace(returncode=int(args[2] not in ancestors[args[3]]))
            if args[0] == 'rev-list':
                return SimpleNamespace(stdout=f'{CANDIDATE} {BASE} {INCOMING}')
            if args[0] == 'ls-remote':
                return SimpleNamespace(returncode=0, stdout=f"{remote['oid']}\trefs/heads/{group['branch']}\n")
            self.fail(f'Unexpected Git command: {args}')

        connection = mock.MagicMock()
        connection.execute.return_value.fetchone.return_value = dict(branch=group['branch'], workspace_owned=1)
        store = mock.Mock()
        store.update.side_effect = lambda saved, **values: dict(saved, **values)
        runtime = mock.Mock()
        runtime.get.return_value.status = 'MANUAL_PENDING'
        service = SimpleNamespace(effects=SimpleNamespace(git=mock.Mock(side_effect=git)), store=store,
                                  connection=connection, repo='/repo', repo_path=Path('/repo'),
                                  state_dir=Path(directory), runtime=runtime,
                                  fetch=lambda _branch: remote['oid'])
        # The exact push completed but its result was interrupted.
        with self.assertRaises(KeyboardInterrupt):
            self.seed_push(service, group, remote)
        remote['oid'] = LATER
        return service, group, remote

    def seed_push(self, service, group, remote):
        remote['oid'] = BASE
        def push(_token):
            remote['oid'] = CANDIDATE
            raise KeyboardInterrupt('after push')
        journal.execute(service.state_dir, owner='plan:G-201', kind='manual_branch_push',
                        repo=service.repo, target=f"refs/heads/{group['branch']}",
                        before={'oid': BASE}, expected={'oid': CANDIDATE},
                        observe=lambda: remote.copy(), apply=push, marker=lambda _: True)

    def test_equal_recovery_survives_second_interruption(self):
        for boundary in ('intent', 'before_result', 'after_result'):
            for advances in (False, True):
                with self.subTest(boundary=boundary, advances=advances), tempfile.TemporaryDirectory() as directory:
                    service, group, remote = self.fixture(directory)
                    write = journal._write_new
                    def interrupt(path, value):
                        equal = (value.get('before') == value.get('expected') == {'oid': LATER})
                        result = value.get('status') == 'equal'
                        if boundary == 'before_result' and result:
                            raise KeyboardInterrupt('before equal receipt')
                        write(path, value)
                        if (boundary == 'intent' and path.name == 'active.json' and equal
                                or boundary == 'after_result' and result):
                            raise KeyboardInterrupt('equal interrupted')
                    with mock.patch.object(journal, '_write_new', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                        update_source(service, group)
                    slot = journal._slot(service.state_dir, service.repo, 'manual_branch_push', f"refs/heads/{group['branch']}")
                    receipts = {p: p.read_bytes() for p in slot.glob('*.result.json')}
                    remote['oid'] = NEWER if advances else LATER
                    current = update_source(service, group)
                    self.assertEqual(current['prepared_head'], remote['oid'])
                    self.assertEqual(current['request_id'], group['request_id'])
                    self.assertEqual(current['members'], group['members'])
                    self.assertIsNone(current['pending_source_head'])
                    self.assertEqual(service.runtime.update_plan.call_count, 2)
                    self.assertFalse(journal.has_pending_project_mutation(service.state_dir, service.repo))
                    for path, content in receipts.items():
                        self.assertEqual(path.read_bytes(), content)

    def test_published_descendant_evidence_blocks_rewrite(self):
        for boundary in ('before_result', 'sqlite'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                service, group, remote = self.fixture(directory)
                write = journal._write_new
                def interrupt(path, value):
                    if boundary == 'before_result' and value.get('status') == 'equal':
                        raise KeyboardInterrupt('before equal receipt')
                    write(path, value)
                service.runtime.update_plan.side_effect = KeyboardInterrupt('before SQLite confirmation')
                with mock.patch.object(journal, '_write_new', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                    update_source(service, group)
                remote['oid'] = REWRITTEN
                service.effects.git.reset_mock()
                with self.assertRaisesRegex(RuntimeError, '게시 완료한 source 후보'):
                    update_source(service, group)
                service.store.update.assert_not_called()
                self.assertFalse(any(call.args[1] == 'push' for call in service.effects.git.call_args_list))
                self.assertEqual(remote['oid'], REWRITTEN)

    def test_equal_receipt_with_different_owner_is_preserved_and_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            service, group, remote = self.fixture(directory)
            write = journal._write_new
            def interrupt(path, value):
                write(path, value)
                if value.get('status') == 'equal':
                    raise KeyboardInterrupt('after equal receipt')
            with mock.patch.object(journal, '_write_new', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                update_source(service, group)
            slot = journal._slot(service.state_dir, service.repo, 'manual_branch_push', f"refs/heads/{group['branch']}")
            active = journal._read(slot / 'active.json')
            active['owner'] = 'plan:G-999'
            # Model a journal owned by another request, with matching OIDs.
            for path in (slot / 'active.json', slot / f"{active['id']}.intent.json"):
                path.write_text(json.dumps(active))
            service.effects.git.reset_mock()
            with self.assertRaisesRegex(journal.MutationEvidenceError, '게시 완료 근거가 다릅니다'):
                update_source(service, group)
            self.assertEqual(journal._read(slot / 'active.json'), active)
            service.store.update.assert_not_called()
            self.assertEqual(remote['oid'], LATER)
            self.assertFalse(any(call.args[1] == 'push' for call in service.effects.git.call_args_list))
