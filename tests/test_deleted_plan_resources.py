"""Withdrawn resource policy uses real SQLite/files and observed Git facts."""
import hashlib
import shutil
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.catalog import deleted_plan_resources as resources
from loop_manager_internal.git import manager_git_journal as journal
from loop_manager_internal.git.cleanup_service import AssessCleanupEffects, assess_cleanup
from loop_manager_internal.storage.task_calls import CallDeferred
from tests.support.catalog import catalog_reconciliation_fixture, DeletedPlanCancellationFixture
from tests.support.git import isolated_git_test, make_remote_repository, git_command


class DeletedPlanResourceTests(DeletedPlanCancellationFixture, unittest.TestCase):
    def setUp(self):
        self.fixture = catalog_reconciliation_fixture(self)
        self.connection = self.fixture.connection
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.workspace = self.state / 'worktrees' / hashlib.sha256(b'/repo').hexdigest()[:16] / 'G-001'
        self.workspace.mkdir(parents=True)
        self.branch = 'loop-manager/G-001'
        self.ref = 'refs/heads/' + self.branch
        self.head = 'd' * 40
        self.remote = self.local = self.head
        self.registered = True
        self.lock = ''
        self.captured_branch = self.ref
        self.dirty = ''
        self.operations = {}
        self.writes = []
        self.fail_push = self.fail_remove = self.fail_bundle = self.alias = False
        self.connection.execute("UPDATE projects SET origin='owner/repo',base_branch='main',branch=NULL,worktree=NULL,plan_path=NULL")
        self.connection.execute("UPDATE exec_plans SET branch=?,worktree=?,workspace_owned=1,publication_remote_oid=?,pr_url='https://example/pr/1' WHERE plan_id='G-001'",
                                (self.branch, str(self.workspace), self.head))
        self.connection.execute("UPDATE exec_plans SET depends_on='[]'")
        self.connection.commit()
        self.row = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())
        self.pr = dict(state='CLOSED', mergedAt=None, url=self.row['pr_url'], headRefName=self.branch,
                       baseRefName='main', headRefOid=self.head, isCrossRepository=False)
        self.effects = AssessCleanupEffects(lambda _: (self.local, self.captured_branch, {}, []),
                                           lambda _: self.operations, self.git)

    def git(self, repo, *args, **kwargs):
        self.assertFalse(self.connection.in_transaction)
        code, output = 0, ''
        if 'ls-remote' in args:
            output = (self.remote + '\t' + self.ref + '\n') if self.remote else ''
        elif args[0] == 'rev-parse':
            if '--git-common-dir' in args:
                output = '/repo/.git'
            else:
                code, output = (0, self.local) if self.local else (1, '')
        elif args[0] == 'symbolic-ref':
            code = 0 if self.alias else 1
        elif args[0] == 'status':
            output = self.dirty
        elif args[0] == 'worktree':
            output = ('worktree ' + str(self.workspace) + '\nHEAD ' + self.head + '\nbranch ' + self.ref
                      + ('\nlocked ' + self.lock if self.lock else '') + '\n\n') if self.registered else ''
        elif args[0] == 'remote':
            output = 'https://github.com/owner/repo.git\n'
        elif args[0] == 'bundle':
            if args[1] == 'create':
                Path(args[2]).write_bytes(b'full bundle bytes')
            elif args[1] == 'verify':
                code = int(self.fail_bundle)
            else:
                output = self.head + ' ' + self.ref
        elif 'push' in args:
            self.writes.append(args)
            self.assertIn(journal.exact_branch_lease(self.branch, self.head), args)
            code = int(self.fail_push)
            if not code:
                self.remote = None
        elif args[0] == 'update-ref':
            self.writes.append(args)
            self.assertEqual(args, ('update-ref', '--no-deref', '-d', self.ref, self.local))
            self.local = None
        elif args[0] == 'merge-base':
            code = int(self.local not in (None, self.head))
        elif args[0] != 'cat-file':
            raise AssertionError(args)
        return SimpleNamespace(returncode=code, stdout=output)

    def remove(self, repo, worktree, assessment, **kwargs):
        if self.fail_remove:
            raise CallDeferred('temporary removal failure')
        self.assertEqual(assessment.status, 'safe')
        self.workspace.rmdir()
        self.registered = False
        return assessment

    def run_cleanup(self, owner=lambda: None):
        return resources.cleanup(self.connection, self.state, '/repo', '/repo', self.row, 'b' * 40,
            git=self.git, pr_for_branch=lambda *_, **__: self.pr, check_owner=owner,
            assess_cleanup=lambda path, head: assess_cleanup(self.effects, path, head),
            remove_cleanup_worktree=self.remove, recover_cleanup_worktree=None)

    def test_policy_refusals_preserve_files_refs_and_database(self):
        before = list(self.connection.iterdump())
        for field, value in [('remote', 'e' * 40), ('local', 'e' * 40), ('dirty', '?? user.txt'),
                             ('dirty', 'M  staged.txt'), ('operations', {'rebase-merge': 'running'}),
                             ('lock', 'user inspection'), ('captured_branch', 'user-branch'),
                             ('alias', True), ('fail_bundle', True)]:
            with self.subTest(field=field, value=value):
                old = getattr(self, field)
                setattr(self, field, value)
                with self.assertRaises(CallDeferred):
                    self.run_cleanup()
                setattr(self, field, old)
                self.assertEqual(self.writes, [])
                self.assertTrue(self.workspace.exists())
                self.assertEqual(list(self.connection.iterdump()), before)
        for pr in [dict(self.pr, state='OPEN'), dict(self.pr, mergedAt='merged'),
                   dict(self.pr, url='other'), dict(self.pr, headRefOid='e' * 40), dict(self.pr, baseRefName='other'),
                   dict(self.pr, isCrossRepository=True)]:
            original, self.pr = self.pr, pr
            with self.assertRaises(CallDeferred):
                self.run_cleanup()
            self.pr = original
        for field, value in [('branch', 'main'), ('workspace_owned', 0), ('worktree', '/user/G-001')]:
            original = self.row[field]
            self.row[field] = value
            with self.assertRaises(CallDeferred):
                self.run_cleanup()
            self.row[field] = original
        self.connection.execute("UPDATE exec_plans SET worktree=? WHERE plan_id='G-002'", (str(self.workspace),))
        self.connection.commit()
        with self.assertRaises(CallDeferred):
            self.run_cleanup()
        self.assertEqual(self.writes, [])
        self.assertTrue(self.workspace.exists())

    def test_missing_branch_and_changed_pr_or_ref_before_effect_preserve_resources(self):
        self.row['branch'] = None
        with self.assertRaises(CallDeferred):
            self.run_cleanup()
        self.row['branch'] = self.branch
        observed = self.git
        def changed(repo, *args, **kwargs):
            result = observed(repo, *args, **kwargs)
            if args[:2] == ('bundle', 'verify'):
                self.pr['state'] = 'OPEN'
            return result
        with mock.patch.object(self, 'git', side_effect=changed):
            with self.assertRaises(CallDeferred):
                self.run_cleanup()
        self.assertEqual(self.writes, [])
        self.assertTrue(self.workspace.exists())

    def test_partial_cleanup_and_recreated_ref_resume_without_second_push(self):
        self.fail_remove = True
        with self.assertRaises(CallDeferred):
            self.run_cleanup()
        self.assertIsNone(self.remote)
        self.assertEqual(self.local, self.head)
        self.assertTrue(self.workspace.exists())
        bundles = list(self.state.rglob('*.bundle'))
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].read_bytes(), b'full bundle bytes')
        # A completed deletion cannot be used against a same-name recreation,
        # even when the recreated ref has the original OID.
        self.remote = self.head
        with self.assertRaises(CallDeferred):
            self.run_cleanup()
        self.assertEqual(len(self.writes), 1)
        self.remote, self.fail_remove = None, False
        artifacts = self.run_cleanup()
        self.assertEqual(len(self.writes), 2)
        self.assertFalse(self.workspace.exists())
        self.assertIsNone(self.local)
        self.assertEqual(self.run_cleanup(), artifacts)
        self.assertTrue(all(path.exists() for path in artifacts))
        self.assertTrue(all(resources.journal_artifact(path, '/repo', 'G-001') for path in artifacts[1:]))

    def test_unrecorded_remote_and_local_effects_recover_from_fsynced_intents(self):
        real_finish = journal._finish
        failures = set(resources.KINDS)
        def finish(slot, active, intent, *args, **kwargs):
            if intent['kind'] in failures:
                failures.remove(intent['kind'])
                raise RuntimeError('interrupted before result')
            return real_finish(slot, active, intent, *args, **kwargs)
        with mock.patch.object(journal, '_finish', side_effect=finish):
            for _ in resources.KINDS:
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    self.run_cleanup()
        self.assertIsNone(self.remote)
        self.assertIsNone(self.local)
        self.assertEqual(len(self.writes), 2)
        self.run_cleanup()
        self.assertEqual(len(self.writes), 2)
        self.assertFalse(list(self.state.rglob('active.json')))

    def test_protection_failure_and_owner_race_leave_pending_evidence(self):
        self.fail_push = True
        with self.assertRaises(CallDeferred):
            self.run_cleanup()
        self.assertEqual(self.remote, self.head)
        self.assertTrue(self.workspace.exists())
        self.assertTrue(list(self.state.rglob('active.json')))
        self.fail_push = False
        def owner():
            raise CallDeferred('writer or declaration changed')
        with self.assertRaises(CallDeferred):
            self.run_cleanup(owner)
        self.assertEqual(len(self.writes), 1)
        self.run_cleanup()
        self.assertIsNone(self.remote)
        self.assertFalse(self.workspace.exists())

    def test_live_rowless_cancellation_hands_partial_cleanup_to_existing_erasure_queue(self):
        from loop_manager_internal.catalog import deleted_plan_cleanup
        self.pr = None
        text = self.rowless_cancellation(started=True,
            owned_workspace=self.workspace, publication_oid=self.head)
        self.artifact_call('terminated-writer', '/repo', 'G-001')
        self.connection.commit()
        pinned = SimpleNamespace(regular_paths=set(), revision_sha='c' * 40,
            snapshot=SimpleNamespace(observed_ids={'G-002'}, all={}, withheld=()))
        def run():
            return deleted_plan_cleanup.cleanup(self.connection, '/repo', '/repo', pinned, (), git=self.git,
                git_bytes=lambda *_: text.encode(), pr_for_branch=lambda *_, **__: self.pr,
                state_dir=self.state, revision_is_current=lambda: True,
                assess_cleanup=lambda path, head: assess_cleanup(self.effects, path, head),
                remove_cleanup_worktree=self.remove)
        self.fail_remove = True
        self.assertEqual(run(), ())
        self.assertIsNotNone(self.connection.execute('SELECT * FROM execplan_cancellations').fetchone())
        self.assertIsNone(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone())
        self.assertIsNone(self.remote)
        self.assertTrue(list(self.state.rglob('*.bundle')))
        self.fail_remove = False
        self.assertEqual(run(), ('G-001',))
        self.assertEqual(run(), ())
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(tuple(self.connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('/repo', 'G-001'))
        for table in ('execplan_cancellations', 'deleted_plan_files', 'agent_invocations'):
            self.assertIsNone(self.connection.execute('SELECT * FROM ' + table).fetchone())
        self.assertFalse(list(self.state.rglob('*.bundle')))
        self.assertFalse(any(slot.exists() for slot in (
            journal._slot(self.state, '/repo', kind, self.ref) for kind in resources.KINDS)))


@isolated_git_test
class DeletedPlanNativeRefTest(unittest.TestCase):
    def setUp(self):
        pass

    def test_lease_race_and_retry_preserve_bundle_and_remove_exact_native_resources(self):
        from loop_manager_internal.runtime.resource_lock import resource_locks
        fixture = catalog_reconciliation_fixture(self)
        connection = fixture.connection
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_remote_repository(directory)
            state = root / 'state'
            branch, workspace = loop_manager.expected_execplan_workspace(state, str(repo), 'G-001')
            loop_manager.make_worktree(repo, workspace, branch, 'main',
                journal_state_dir=state, journal_owner='plan:G-001')
            (workspace / 'src/app.py').write_text('VALUE = 42\n')
            git_command(workspace, 'add', 'src/app.py')
            git_command(workspace, 'commit', '-m', 'withdrawn work')
            head = git_command(workspace, 'rev-parse', 'HEAD')
            base = git_command(repo, 'rev-parse', 'main')
            git_command(workspace, 'push', 'origin', branch)
            origin = git_command(repo, 'remote', 'get-url', 'origin')
            connection.execute("UPDATE projects SET repo=?,origin='owner/repo',base_branch='main',branch=NULL,worktree=NULL,plan_path=NULL", (str(repo),))
            connection.execute("UPDATE exec_plans SET repo=?", (str(repo),))
            connection.execute("UPDATE exec_plans SET depends_on='[]'")
            connection.execute("UPDATE exec_plans SET branch=?,worktree=?,workspace_owned=1,publication_remote_oid=?,pr_url=NULL WHERE plan_id='G-001'",
                               (branch, str(workspace), head))
            connection.commit()
            saved = dict(connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone())
            ref = 'refs/heads/' + branch
            raced = git_command(repo, 'commit-tree', head + '^{tree}', '-p', head, '-m', 'competitor')
            pushes = []
            def git(path, *args, **kwargs):
                if 'push' in args:
                    pushes.append(args)
                    if len(pushes) == 1:
                        git_command(repo, 'push', journal.exact_branch_lease(branch, head), origin, raced + ':' + ref)
                return loop_manager.git(path, *args, **kwargs)
            def check_owner():
                self.assertEqual(dict(connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()), saved)
            def parse_url(url):
                self.assertEqual(url, origin)
                return 'owner/repo'
            def run():
                with resource_locks(state, str(repo), ['plan:G-001', 'branch:' + branch,
                        'worktree:' + str(workspace), 'git:' + str(workspace)], owner='workspace-reservation-recovery'):
                    return resources.cleanup(connection, state, repo, str(repo), saved, head,
                        git=git, pr_for_branch=lambda *_, **__: None, check_owner=check_owner,
                        assess_cleanup=loop_manager.assess_cleanup, remove_cleanup_worktree=loop_manager.remove_cleanup_worktree,
                        recover_cleanup_worktree=loop_manager._recover_managed_worktree_removal)
            with mock.patch.object(resources, 'github_repository', side_effect=parse_url):
                with self.assertRaises(CallDeferred):
                    run()
                self.assertEqual(git_command(Path(origin), 'rev-parse', ref), raced)
                self.assertEqual(git_command(repo, 'rev-parse', ref), head)
                self.assertEqual((workspace / 'src/app.py').read_text(), 'VALUE = 42\n')
                self.assertTrue(list(state.rglob('active.json')))
                # A later observation still cannot replace the saved lease.
                with self.assertRaises(CallDeferred):
                    run()
                self.assertEqual(len(pushes), 1)
                # Restore this isolated fixture explicitly; cleanup itself never
                # swaps its expectation to the racing OID.
                git_command(repo, 'push', journal.exact_branch_lease(branch, raced), origin, head + ':' + ref)
                artifacts = run()
                self.assertEqual(run(), artifacts)
                self.assertEqual(len(pushes), 2)
                git_command(repo, 'worktree', 'add', '-b', branch, str(workspace), head)
                with self.assertRaises(CallDeferred):
                    run()
                self.assertEqual((workspace / 'src/app.py').read_text(), 'VALUE = 42\n')
                self.assertEqual(git_command(repo, 'rev-parse', ref), head)
                git_command(repo, 'worktree', 'remove', str(workspace))
                git_command(repo, 'update-ref', '-d', ref, head)
            self.assertFalse(workspace.exists())
            self.assertNotIn(str(workspace), git_command(repo, 'worktree', 'list', '--porcelain'))
            self.assertEqual(git_command(repo, 'for-each-ref', '--format=%(refname)', ref), '')
            self.assertEqual(git_command(repo, 'ls-remote', '--heads', origin, ref), '')
            bundle = next(artifacts[0].glob('*.bundle'))
            restored = root / 'restored'
            restored.mkdir()
            git_command(restored, 'init')
            git_command(restored, 'fetch', str(bundle), ref)
            self.assertEqual(git_command(restored, 'show', 'FETCH_HEAD:src/app.py'), 'VALUE = 42')
            self.assertEqual(git_command(repo, 'rev-parse', 'main'), base)
            self.assertEqual(git_command(Path(origin), 'rev-parse', 'main'), base)
            from loop_manager_internal.catalog.deleted_plan_cleanup import remove_pending_files
            from loop_manager_internal.storage import deleted_plan_state
            deleted_plan_state.ensure_schema(connection)
            connection.commit()
            deleted_plan_state.forget(connection, str(repo), 'G-001', artifacts=artifacts)
            lost_result = journal._slot(state, str(repo), resources.KINDS[0], ref)
            original_remove = shutil.rmtree
            def interrupted(path, *args, **kwargs):
                original_remove(path, *args, **kwargs)
                if Path(path) == lost_result:
                    raise OSError('interrupted after file removal')
            with mock.patch.object(shutil, 'rmtree', side_effect=interrupted):
                self.assertEqual(remove_pending_files(connection, str(repo), state), {'G-001'})
            self.assertEqual(remove_pending_files(connection, str(repo), state), set())
            self.assertTrue(all(not path.exists() for path in artifacts))
            self.assertIsNone(connection.execute('SELECT * FROM deleted_plan_files').fetchone())
            self.assertEqual(tuple(connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), (str(repo), 'G-001'))
