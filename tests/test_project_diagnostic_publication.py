import hashlib
import json
import os
import sqlite3
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager


class ProjectDiagnosticPublicationUnitTests(unittest.TestCase):
    def test_guidance_checkpoint_archive_validates_both_rename_paths(self):
        parent, head, tree = 'a' * 40, 'b' * 40, 'c' * 40
        evidence = 'Guidance decision pending'
        paths = ('docs/requirements/Archive.md', 'docs/requirements/archive/Archive.md')
        identity = loop_manager.blocked_checkpoint.checkpoint_identity(
            plan_id='G-058', reason_code=loop_manager.GUIDANCE_BLOCK_REASON,
            evidence=evidence, parent_oid=parent, tree_oid=tree, included_paths=paths)
        message = '\n'.join([
            '[G-058] BLOCKED checkpoint', 'Scope: plan',
            'Reason: ' + loop_manager.GUIDANCE_BLOCK_REASON,
            'Evidence: ' + json.dumps(evidence),
            'Evidence-SHA256: ' + hashlib.sha256(evidence.encode()).hexdigest(),
            'Checkpoint-Identity: ' + identity, 'Paths:', *('- ' + p for p in paths)])

        def git(_path, *args, **_kwargs):
            if args[0] == 'diff':
                changed = paths if '--no-renames' in args else paths[1:]
                output = '\0'.join(changed) + '\0'
            elif args[:2] == ('rev-parse', head + '^{tree}'):
                output = tree
            else:
                output = {'--format=%B': message, '--format=%P': parent,
                    '--format=%an <%ae>%n%cn <%ce>':
                    'Loop Manager <loop-manager@localhost>\nLoop Manager <loop-manager@localhost>'}[args[2]]
            return subprocess.CompletedProcess(args, 0, output)

        with (mock.patch.object(loop_manager, 'git', side_effect=git),
              mock.patch.object(loop_manager, 'git_bytes',
                  side_effect=lambda path, *args: git(path, *args).stdout.encode())):
            self.assertEqual(loop_manager._guidance_checkpoint_commit_evidence(
                Path('/repo'), 'G-058', head), evidence)
            self.assertIsNone(loop_manager._guidance_checkpoint_commit_evidence(
                Path('/repo'), 'G-097', head))

    def test_missing_origin_keeps_local_only_scope_and_mismatched_push_url_is_rejected(self):
        connection = mock.Mock()
        adapter = loop_manager._BlockedCheckpointAdapter(connection, Path('/repo'), Path('/state'), 'repo')
        request = SimpleNamespace(repo='repo', worktree='/state/G-107')
        current = SimpleNamespace(head='a' * 40)
        connection.execute.return_value.fetchone.return_value = {'origin': None}
        verdict = adapter._origin_safety_error(request, current)
        self.assertEqual(verdict.scope, loop_manager.blocked_checkpoint.SafetyScope.PLAN)
        self.assertEqual(verdict.reason_code, 'registered_origin_missing')
        connection.execute.return_value.fetchone.return_value = {'origin': 'example/repo'}
        with mock.patch.object(loop_manager, 'git', side_effect=[
                SimpleNamespace(stdout='https://github.com/example/repo.git\n'),
                SimpleNamespace(stdout='https://github.com/other/repo.git\n')]):
            verdict = adapter._origin_safety_error(request, current)
        self.assertEqual(verdict.reason_code, 'publication_ownership_uncertain')

    def test_remote_observation_rejects_an_unexpected_ref_name(self):
        adapter = loop_manager._BlockedCheckpointAdapter(mock.Mock(), Path('/repo'), Path('/state'), 'repo')
        with mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(
                returncode=0, stdout='a' * 40 + '\trefs/heads/unexpected\n')):
            with self.assertRaises(RuntimeError):
                adapter.observe_remote(SimpleNamespace(branch='loop-manager/diagnostics/G-107/content'))

    def test_diagnostic_create_uses_absence_lease_and_verified_url(self):
        request = SimpleNamespace(branch='loop-manager/diagnostics/G-107/artifact', worktree='/state/G-107')
        adapter = mock.Mock()
        adapter._validated_push_url.return_value = 'https://github.com/example/repo.git'
        with mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(returncode=0, stdout='')) as git:
            loop_manager.push_diagnostic_checkpoint(adapter, request, 'a' * 40)
        adapter._validated_push_url.assert_called_once_with(request)
        args = git.call_args.args
        operand = args[-2]
        self.assertRegex(operand, r'^x[0-9a-f]{32}://target$')
        self.assertEqual(args, (
            Path('/state/G-107'), '-c', f'core.hooksPath={os.devnull}',
            '-c', f'url.https://github.com/example/repo.git.insteadOf={operand}',
            '-c', f'url.https://github.com/example/repo.git.pushInsteadOf={operand}', 'push',
            '--force-with-lease=refs/heads/loop-manager/diagnostics/G-107/artifact:',
            operand, 'a' * 40 + ':refs/heads/loop-manager/diagnostics/G-107/artifact'))
        self.assertNotIn('origin', git.call_args.args)

    def test_diagnostic_delete_uses_exact_lease_frozen_url_and_disables_hooks(self):
        from loop_manager_internal.recovery.checkpoint_adapter import delete_diagnostic_checkpoint
        request = SimpleNamespace(branch='loop-manager/diagnostics/G-107/artifact', worktree='/state/G-107')
        adapter = mock.Mock()
        url = 'https://github.com/example/repo.git'
        adapter._validated_push_url.return_value = url
        git, revalidate = mock.Mock(return_value=SimpleNamespace(returncode=0)), mock.Mock()
        delete_diagnostic_checkpoint(git, adapter, request, 'a' * 40,
            target_digest=hashlib.sha256(url.encode()).hexdigest(), revalidate=revalidate)
        revalidate.assert_called_once_with()
        args = git.call_args.args
        operand = args[-2]
        self.assertEqual(args, (Path(request.worktree), '-c', f'core.hooksPath={os.devnull}',
            '-c', f'url.{url}.insteadOf={operand}', '-c', f'url.{url}.pushInsteadOf={operand}',
            'push', '--force-with-lease=refs/heads/' + request.branch + ':' + 'a' * 40,
            operand, ':refs/heads/' + request.branch))
        git.reset_mock()
        with self.assertRaises(RuntimeError):
            delete_diagnostic_checkpoint(git, adapter, request, 'a' * 40,
                target_digest='changed destination', revalidate=revalidate)
        with self.assertRaises(RuntimeError):
            delete_diagnostic_checkpoint(git, adapter, SimpleNamespace(branch='main'), 'a' * 40,
                target_digest=hashlib.sha256(url.encode()).hexdigest(), revalidate=revalidate)
        git.assert_not_called()

    def test_diagnostic_never_pushes_an_original_branch_or_unverified_origin(self):
        adapter = mock.Mock()
        with mock.patch.object(loop_manager, 'git') as git:
            with self.assertRaises(RuntimeError):
                loop_manager.push_diagnostic_checkpoint(adapter, SimpleNamespace(branch='loop-manager/G-107'), 'a' * 40)
            adapter._validated_push_url.side_effect = RuntimeError('origin changed')
            with self.assertRaisesRegex(RuntimeError, 'origin changed'):
                loop_manager.push_diagnostic_checkpoint(adapter, SimpleNamespace(
                    branch='loop-manager/diagnostics/G-107/artifact', worktree='/state/G-107'), 'a' * 40)
        git.assert_not_called()

    def test_original_checkpoint_fast_forward_and_create_use_exact_leases(self):
        adapter = loop_manager._BlockedCheckpointAdapter(mock.Mock(), Path('/repo'), Path('/state'), 'repo')
        for mode, expected in ((loop_manager.blocked_checkpoint.PushMode.CREATE, None),
                               (loop_manager.blocked_checkpoint.PushMode.FAST_FORWARD, 'b' * 40)):
            request = loop_manager.blocked_checkpoint.CheckpointRequest(
                'repo', 'G-107', 'request', loop_manager.blocked_checkpoint.CheckpointPhase.LOCAL_COMMITTED,
                'verify_retry_exhausted', 'failure', 'loop-manager/G-107', '/state/G-107', expected)
            with (mock.patch.object(adapter, '_validated_push_url',
                                    return_value='https://github.com/example/repo.git'),
                  mock.patch.object(loop_manager, 'git', side_effect=[
                      SimpleNamespace(returncode=1, stdout=''),
                      SimpleNamespace(returncode=0, stdout=''),
                      SimpleNamespace(returncode=0, stdout=''),
                  ]) as git):
                adapter.push(request, 'a' * 40, loop_manager.blocked_checkpoint.PushDecision(mode))
            self.assertIn('--force-with-lease=refs/heads/loop-manager/G-107:' + (expected or ''),
                          git.call_args_list[1].args)
            self.assertEqual(git.call_args_list[2].args[1:3],
                             ('update-ref', 'refs/remotes/origin/loop-manager/G-107'))
            self.assertEqual(git.call_args_list[2].args[-1], '0' * 40)

    def test_tracking_ref_race_after_push_keeps_checkpoint_recoverable(self):
        adapter = loop_manager._BlockedCheckpointAdapter(mock.Mock(), Path('/repo'), Path('/state'), 'repo')
        request = SimpleNamespace(branch='loop-manager/G-107', worktree='/state/G-107',
                                  expected_remote_oid=None)
        with (mock.patch.object(adapter, '_validated_push_url',
                                return_value='https://github.com/example/repo.git'),
              mock.patch.object(loop_manager, 'git', side_effect=[
                  SimpleNamespace(returncode=1, stdout=''),
                  SimpleNamespace(returncode=0, stdout=''),
                  SimpleNamespace(returncode=1, stdout='tracking ref changed'),
              ]) as git):
            with self.assertRaises(RuntimeError) as raised:
                adapter.push(request, 'a' * 40, loop_manager.blocked_checkpoint.PushDecision(
                    loop_manager.blocked_checkpoint.PushMode.CREATE))
        self.assertIn('tracking ref changed', str(raised.exception))
        self.assertEqual(git.call_args_list[2].args[-1], '0' * 40)

    def test_project_original_branch_push_disables_hooks(self):
        adapter = loop_manager._BlockedCheckpointAdapter(mock.Mock(), Path('/repo'), Path('/state'), 'repo',
                                                         project_recovery_scope=True)
        request = SimpleNamespace(branch='loop-manager/G-107', worktree='/state/G-107', expected_remote_oid='b' * 40)
        with (mock.patch.object(adapter, '_validated_push_url',
                                return_value='https://github.com/example/repo.git'),
              mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(returncode=0, stdout='')) as git):
            adapter.push(request, 'a' * 40, loop_manager.blocked_checkpoint.PushDecision(
                loop_manager.blocked_checkpoint.PushMode.FAST_FORWARD))
        self.assertEqual(git.call_args_list[1].args[1:3], ('-c', f'core.hooksPath={os.devnull}'))

    def test_project_rebase_disables_hooks_and_other_branch_updates(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE exec_plans(repo, plan_id, pr_head_oid, pr_conflict_head_oid)')
        connection.execute("INSERT INTO exec_plans VALUES ('/repo','G-107',NULL,NULL)")
        with (mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=Path('/state')),
              mock.patch.object(loop_manager, '_owned_rebase_journal_state', return_value={'head': 'a' * 40}),
              mock.patch.object(loop_manager, 'remote_branch_oid') as tracking,
              mock.patch.object(loop_manager.manager_git_journal, 'live_remote_branch_oid', return_value=None),
              mock.patch.object(loop_manager.manager_git_journal, 'begin_current_rebase', return_value='operation'),
              mock.patch.object(loop_manager.manager_git_journal, 'record_rebase_invocation'),
              mock.patch.object(loop_manager, '_finish_owned_direct_rebase'),
              mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(returncode=0, stdout='')) as git):
            loop_manager._journaled_direct_rebase(connection, Path('/repo'), '/repo', 'G-107',
                                                  Path('/state/G-107'), 'loop-manager/G-107', 'b' * 40,
                                                  observed_remote=(None,))
        tracking.assert_not_called()
        self.assertEqual(git.call_args_list[0].args, (
            Path('/state/G-107'), '-c', f'core.hooksPath={os.devnull}', '-c', 'rebase.updateRefs=false',
            'rebase', 'b' * 40))

    def test_intermediate_project_checkpoint_keeps_the_slot_gate_and_disables_hooks(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")
        connection.execute("INSERT INTO exec_plans "
            "(repo,plan_id,path,title,sources,depends_on,priority,content_hash,status,branch,worktree) "
            "VALUES ('repo','G-107','docs/plans/[G-107] Work.md','Work','[]','[]','normal','hash',"
            "'IMPLEMENTING','loop-manager/G-107','/state/G-107')")
        connection.execute("INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence) "
                           "VALUES ('repo', 'project', 'publication_ownership_uncertain', 'G-107', 'cause')")
        connection.commit()
        with (mock.patch.object(loop_manager.managed_rebase_publication, 'publish_checkpoint') as publish,
              mock.patch.object(loop_manager, 'git') as git):
            loop_manager._publish_direct_rebase_checkpoint(connection, Path('/repo'), Path('/state'),
                                                          'repo', 'G-107', Path('/state/G-107'), 'loop-manager/G-107')
            state = publish.call_args.kwargs['state']
            state.check()
            publish.call_args.kwargs['git'](Path('/state/G-107'), 'push', 'origin', 'head:ref', check=False)
            self.assertEqual(git.call_args.args[1:3], ('-c', f'core.hooksPath={os.devnull}'))
            publish.reset_mock()
            with self.assertRaises(loop_manager.manager_git_journal.MutationEvidenceError):
                loop_manager._publish_direct_rebase_checkpoint(connection, Path('/repo'), Path('/state'),
                                                              'repo', 'G-108', Path('/state/G-108'), 'loop-manager/G-108')
            publish.assert_not_called()
            connection.execute('DELETE FROM project_blocks')
            from loop_manager_internal.storage.runtime_state import RuntimeStateError
            with self.assertRaises(RuntimeStateError):
                state.check()
