import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from loop_manager_internal.agents import scope_recovery
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.runtime.contracts import RuntimeMutation
from tests.test_agent_scope_decision import snapshot


class RuntimeScopeDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.before = snapshot(config={'local:remote.origin.url': ('credential-before',)})
        self.current = replace(self.before,
            config={'local:remote.origin.url': ('credential-after',)},
            dirty={'outside.txt': (('unstaged',), 'private-file-content')},
            operations={'MERGE_HEAD': ('private-operation-content',)},
            refs={**self.before.refs, 'refs/heads/foreign': ('secret-ref-value', '')})
        self.mutation = RuntimeMutation(Path('/source'), Path('/state/runtime/b'), 'b', 'owned')

    def test_acceptance_and_rejection_report_identities_and_keys_without_values(self):
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                message = scope_recovery.runtime_scope_diagnostic(
                    self.before, self.current, self.mutation, accepted=accepted,
                    result=self.current, checkpoint=self.current)
                for expected in ('before=', 'current=', 'result=', 'checkpoint=', 'checkpoint_delta=',
                                 'path=/state/runtime/b', 'sha=b',
                                 'config=local:remote.origin.url', 'dirty=outside.txt',
                                 'operations=MERGE_HEAD', 'refs=refs/heads/foreign'):
                    self.assertIn(expected, message)
                for secret in ('credential-before', 'credential-after', 'private-file-content',
                               'private-operation-content', 'secret-ref-value'):
                    self.assertNotIn(secret, message)

    def test_early_rejection_logs_original_evidence_and_never_updates_it(self):
        effects = mock.Mock()
        effects.repository.return_value.scope_owner.return_value = {
            'agent_snapshot': self.before.serialize(), 'worktree': str(Path.cwd()),
            'agent_result_snapshot': self.current.serialize()}
        effects.capture_git_snapshot.return_value = self.current
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        for error in (AgentScopeError('config rejected'), AgentScopeError('runtime rejected')):
            with self.subTest(error=str(error)), mock.patch.object(
                    scope_recovery, '_validate_persisted_agent_scope', side_effect=error):
                with self.assertRaises(AgentScopeError) as caught:
                    scope_recovery.validate_persisted_agent_scope(
                        connection, Path('/source'), Path('/state'), '/source',
                        runtime_mutation=self.mutation, effects=effects)
                self.assertIs(caught.exception, error)
                message = effects.operation_log.call_args.args[0]
                self.assertIn('runtime scope 거부', message)
                self.assertIn('dirty=outside.txt', message)
                self.assertIn('result_delta=refs=refs/heads/foreign', message)
                self.assertNotIn('credential-after', message)
                effects.update_project.assert_not_called()

    def test_success_and_unavailable_snapshot_have_explicit_diagnostics(self):
        effects = mock.Mock()
        effects.repository.return_value.scope_owner.return_value = None
        with mock.patch.object(scope_recovery, '_validate_persisted_agent_scope', return_value=None):
            scope_recovery.validate_persisted_agent_scope(
                None, Path('/source'), Path('/state'), '/source',
                runtime_mutation=self.mutation, effects=effects)
        self.assertIn('runtime scope 수락', effects.operation_log.call_args.args[0])
        self.assertIn('before=unavailable current=unavailable', effects.operation_log.call_args.args[0])


class RuntimeFalsePositiveBaseTests(unittest.TestCase):
    def test_false_positive_uses_registered_base_and_keeps_mixed_violation(self):
        from loop_manager_internal.recovery import project_block_service
        runtime = '/state/runtime/b'
        mutation = RuntimeMutation(Path('/source'), Path(runtime), 'b', 'owned')
        for base in ('main', 'develop'):
            ref = 'refs/remotes/origin/' + base
            before = snapshot(refs={ref: ('a', ''), 'refs/remotes/origin/HEAD': ('a', ref)})
            current = replace(before, refs={ref: ('b', ''), 'refs/remotes/origin/HEAD': ('b', ref)},
                              worktrees={**before.worktrees, runtime: ('HEAD=b', 'detached=True')})
            for violation in (False, True):
                with self.subTest(base=base, violation=violation):
                    result = replace(current, refs={**current.refs, 'refs/heads/foreign': ('c', '')}) if violation else current
                    effects = project_block_service.ReconcileFalsePositiveRuntimeBlockEffects(
                        _canonical_path=lambda path: path,
                        capture_git_snapshot=mock.Mock(return_value=result),
                        operation_log=mock.Mock(), validate_persisted_agent_scope=mock.Mock())
                    project = {'agent_snapshot': before.serialize(), 'agent_result_snapshot': result.serialize(),
                               'worktree': '/managed', 'base_branch': base}
                    original = dict(project)
                    accepted = project_block_service.reconcile_false_positive_runtime_block(
                        effects, None, Path('/source'), Path('/state'), '/source', project,
                        {'evidence': 'Agent 범위 선검증 실패: runtime'}, mutation)
                    self.assertEqual(accepted, not violation)
                    self.assertEqual(project, original)
                    if violation:
                        effects.validate_persisted_agent_scope.assert_not_called()
                        self.assertIn('refs/heads/foreign', effects.operation_log.call_args.args[0])
                    else:
                        effects.validate_persisted_agent_scope.assert_called_once()
