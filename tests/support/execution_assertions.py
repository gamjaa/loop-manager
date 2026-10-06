"""Shared assertions for checkpoint and publication boundaries."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionOutcomeKind



def _assert_post_push_pr_attachment(test, case, pr_head, accepted, *,
                                   pr_state='OPEN', remote_deleted=False,
                                   reconciled_kind=ExecutionOutcomeKind.SUCCESS):
    case.state.update_project('repo', validated_base_sha='base')
    case.effects.publication.push_managed_publication_branch.side_effect = (
        lambda *_: case.calls.append('push') or 'base'
    )
    case.effects.workspace.remote_branch_oid.side_effect = (
        lambda *_: 'base' if 'push' in case.calls and not remote_deleted else None
    )
    case.effects.publication.pr_for_branch.side_effect = lambda *_: (
        {'url': 'existing', 'state': pr_state, 'headRefOid': pr_head,
         'headRefName': 'loop-manager/G-047'} if 'push' in case.calls else None
    )
    case.effects.publication.reconcile_existing_pull_request.return_value = 0
    case.effects.publication.command.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({'body': 'User summary', 'headRefOid': pr_head, 'state': pr_state}))
    result = case.handler.publish(case.context('PUBLISHING'), case.handler)
    test.assertEqual(result.kind, reconciled_kind if accepted
                     else ExecutionOutcomeKind.FINAL_BLOCK)
    test.assertEqual(case.effects.publication.reconcile_existing_pull_request.call_count,
                     int(accepted))
    case.effects.publication.push_managed_publication_branch.assert_called_once()
    if accepted and pr_state == 'OPEN':
        case.effects.publication.command.assert_called_once()
        test.assertEqual(case.effects.publication.command.call_args.args[0][:3], ['gh', 'pr', 'view'])
    else:
        case.effects.publication.command.assert_not_called()


def _assert_manual_autostash_recovery(test, case, *, interruption=None):
    from loop_manager_internal.git.managed_rebase_validation import FileChange
    case._prepare_current_checkpoint_recovery()
    saved = case.checkpoint.get('repo', 'G-047')
    case.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
        operation='rebase', expected_head='before', target_base_oid='base',
        observed_remote_oid='remote', snapshot={'stash_oid': 'stash'})
    case.state.update_project('repo', worker_result=None, active_sha='old-base')
    case.expected = replace(case.expected, autostash_oid='stash')
    case.observed = replace(case.observed, index_clean=False, staged=(
        FileChange('app.py', '100644', '100644', 'old', 'new', 'M'),))
    calls = []
    class Interrupted(BaseException):
        pass
    armed = [interruption]
    record = case.checkpoint.record
    def interrupted_record(*args, **kwargs):
        saved = record(*args, **kwargs)
        audit = (saved.snapshot or {}).get('rebase_resolution_commit')
        boundary = 'result' if audit and 'after_head' in audit else 'intent'
        if audit and armed[0] == boundary:
            armed[0] = None
            raise Interrupted()
        return saved
    def git(path, *args, **kwargs):
        calls.append(args)
        if args == ('diff', '--cached', '--quiet'):
            return SimpleNamespace(stdout='', returncode=int(not case.observed.index_clean))
        if args[:1] == ('commit',):
            case.observed = replace(case.observed, head='resolved-head', staged=(), index_clean=True,
                branch_ref=('resolved-head', ''), registration=(
                    'HEAD=resolved-head', 'branch=refs/heads/loop-manager/G-047'))
            if armed[0] == 'git':
                armed[0] = None
                raise Interrupted()
            return SimpleNamespace(stdout='', returncode=0)
        if args == ('rev-parse', 'HEAD'):
            return SimpleNamespace(stdout=case.observed.head, returncode=0)
        return case.fixture.observed_git(path, *args, **kwargs)
    case.fixture.effects.workspace.git.side_effect = git
    with mock.patch.object(case.checkpoint, 'record', side_effect=interrupted_record):
        if interruption:
            with test.assertRaises(Interrupted):
                case.handler.recover(case.fixture.context())
            pending = case.checkpoint.get('repo', 'G-047')
            test.assertEqual(pending.operation, 'rebase')
            test.assertEqual(pending.expected_head, 'before')
            test.assertEqual(pending.target_base_oid, 'base')
            test.assertEqual(pending.observed_remote_oid, 'remote')
            test.assertEqual(pending.snapshot['stash_oid'], 'stash')
            test.assertIsNone(pending.current_rebase_acceptance())
        result = case.handler.recover(case.fixture.context())
    test.assertIsInstance(result, handlers.ExecutionContext)
    test.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
    saved = case.checkpoint.get('repo', 'G-047')
    test.assertIsNone(saved.operation)
    test.assertEqual(saved.snapshot['current_rebase_acceptance']['head'], 'resolved-head')
    test.assertIsNone(case.state.row()['worker_result'])
    test.assertIsNone(case.state.row()['verify_result'])
    test.assertEqual(sum(args[0] == 'commit' for args in calls), 1)
    test.assertFalse(any(args[0] == 'add' or 'rebase' in args for args in calls))
    test.assertIsInstance(case.handler.recover(case.fixture.context()), handlers.ExecutionContext)
    test.assertEqual(sum(args[0] == 'commit' for args in calls), 1)
    case.fixture.effects.agent.run_agent.assert_not_called()
    case.fixture.effects.validation.verify.assert_not_called()
