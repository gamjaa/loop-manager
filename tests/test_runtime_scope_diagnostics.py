import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from loop_manager_internal.agents import scope_recovery
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.runtime.contracts import RuntimeMutation
from tests.support.scope import snapshot


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
                                 f'path={self.mutation.path}', 'sha=b',
                                 'config=local:remote.origin.url', 'dirty=outside.txt',
                                 'operations=MERGE_HEAD', 'refs=refs/heads/foreign'):
                    self.assertIn(expected, message)
                for secret in ('credential-before', 'credential-after', 'private-file-content',
                               'private-operation-content', 'secret-ref-value'):
                    self.assertNotIn(secret, message)

    def test_current_runtime_failure_preserves_history_without_git_access_diagnosis(self):
        effects = mock.Mock()
        from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
        effects.validate_runtime_worktree.side_effect = WorkspaceOwnershipError('runtime identity mismatch')
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        with mock.patch.object(scope_recovery, '_held_process_lock_identity', return_value='runtime:owned'):
            with self.assertRaisesRegex(WorkspaceOwnershipError, 'runtime identity mismatch'):
                scope_recovery.validate_persisted_agent_scope(
                    connection, Path('/source'), Path('/state'), '/source',
                    runtime_mutation=self.mutation, effects=effects)
        effects.update_project.assert_not_called()
        effects.capture_git_snapshot.assert_not_called()

    def test_current_runtime_validation_does_not_require_an_old_git_snapshot(self):
        effects = mock.Mock()
        effects.repository.return_value.scope_owner.return_value = None
        effects._validated_runtime_registration_marker.return_value = None
        with mock.patch.object(scope_recovery, '_held_process_lock_identity', return_value='runtime:owned'):
            self.assertIsNone(scope_recovery.validate_persisted_agent_scope(
                None, Path('/source'), Path('/state'), '/source',
                runtime_mutation=self.mutation, effects=effects))
        effects.validate_runtime_worktree.assert_called_once()
        effects.capture_git_snapshot.assert_not_called()


class RuntimeFalsePositiveBaseTests(unittest.TestCase):
    def test_false_positive_uses_registered_base_and_keeps_mixed_violation(self):
        from types import SimpleNamespace
        from loop_manager_internal.recovery import project_block_service
        runtime = str(Path('/state/runtime/b').resolve())
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
                        git=lambda _repo, *args, **_kwargs: SimpleNamespace(
                            returncode=0,
                            stdout=(f'b\trefs/heads/{base}\n' if args[0] == 'ls-remote'
                                    else 'b\n' if args[0] == 'rev-parse'
                                    else 'https://github.com/owner/repo.git\n' if args[0] == 'remote' else ''),
                        ),
                        operation_log=mock.Mock(), validate_persisted_agent_scope=mock.Mock())
                    project = {'agent_snapshot': before.serialize(), 'agent_result_snapshot': result.serialize(),
                               'worktree': '/managed', 'base_branch': base, 'origin': 'owner/repo'}
                    original = dict(project)
                    accepted = project_block_service.reconcile_false_positive_runtime_block(
                        effects, None, Path('/source'), Path('/state'), '/source', project,
                        {'reason_code': 'outside_managed_worktree_mutation', 'evidence': 'Agent 범위 선검증 실패: runtime'}, mutation)
                    self.assertTrue(accepted)
                    self.assertEqual(project, original)
                    effects.validate_persisted_agent_scope.assert_called_once()



class ExternalBaseAdvanceUnitTests(unittest.TestCase):
    def test_live_forward_and_symbolic_head_accept_without_isolation_and_keep_original(self):
        from types import SimpleNamespace
        from loop_manager_internal.git.base_fetch_service import reconcile_external_base
        from loop_manager_internal.agents.agent_scope_decision import used_git_resources
        for base in ('main', 'develop'):
            ref, head = 'refs/remotes/origin/' + base, 'refs/remotes/origin/HEAD'
            resources = used_git_resources(role='worker', phase='WORKING', base_branch=base,
                branch='loop-manager/G-051', worktree_key='/managed')
            for created in (False, True):
                before = snapshot(refs={ref: ('a', '')} if created else {ref: ('a', ''), head: ('a', ref)})
                current = replace(before, refs={ref: ('c', ''), head: ('c', ref)})
                original = before.serialize()
                def git(_repo, *args, **kwargs):
                    if args[0] == 'ls-remote':
                        output = ('ref: refs/heads/' + base + '\tHEAD\nc\tHEAD\n'
                                  if '--symref' in args else 'c\trefs/heads/' + base + '\n')
                        return SimpleNamespace(returncode=0, stdout=output)
                    if args[0] == 'remote':
                        return SimpleNamespace(returncode=0, stdout='https://github.com/owner/repo.git\n')
                    return SimpleNamespace(returncode=0, stdout='c\n')
                self.assertEqual(reconcile_external_base(git, Path('/source'), base,
                    before, current, resources, registered_origin='owner/repo').refs, current.refs)
                self.assertEqual(before.serialize(), original)
                self.assertEqual(reconcile_external_base(
                    git, Path('/source'), base, before, current, None,
                    registered_origin='owner/repo').refs, current.refs)
                def wrong_origin(_repo, *args, **kwargs):
                    if args[0] == 'remote':
                        return SimpleNamespace(returncode=0, stdout='https://github.com/other/repo.git\n')
                    return git(_repo, *args, **kwargs)
                self.assertIs(reconcile_external_base(
                    wrong_origin, Path('/source'), base, before, current, None,
                    registered_origin='owner/repo'), before)
                for invalid in (({ref: ('c', '')},) if not created else ()) + ({ref: ('c', ''), head: ('c', 'refs/heads/other')},
                                {ref: ('c', ''), head: ('wrong', ref)}, {head: ('c', ref)}):
                    self.assertIs(reconcile_external_base(git, Path('/source'), base,
                        before, replace(current, refs=invalid), resources,
                        registered_origin='owner/repo'), before)
                for failure in ('ls-remote', 'merge-base', 'rev-parse'):
                    def unavailable(repo, *args, **kwargs):
                        return SimpleNamespace(returncode=1, stdout='') if args[0] == failure else git(repo, *args, **kwargs)
                    if failure == 'merge-base':
                        self.assertIs(reconcile_external_base(unavailable, Path('/source'), base,
                            before, current, resources, registered_origin='owner/repo'), before)
                    else:
                        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
                        with self.assertRaises(RecoveryFailure):
                            reconcile_external_base(unavailable, Path('/source'), base,
                                before, current, resources, registered_origin='owner/repo')

    def test_scope_consumer_does_not_reobserve_external_base(self):
        from types import SimpleNamespace
        from loop_manager_internal.agents import scope_validation
        from loop_manager_internal.agents.agent_scope_decision import used_git_resources
        ref, head = 'refs/remotes/origin/main', 'refs/remotes/origin/HEAD'
        before = snapshot(refs={ref: ('a', ''), head: ('a', ref)})
        current = replace(before, refs={ref: ('b', ''), head: ('b', ref)})
        git = mock.Mock(side_effect=AssertionError('scope validation must not re-observe the remote base'))

        validation_effects = scope_validation.ScopeValidationEffects(
            _agent_git_resources=lambda *_args, **_kwargs: None,
            repository=lambda _connection: SimpleNamespace(
                scope_owner=lambda *_args: {'origin': 'owner/repo'},
            ),
            _current_rebase_service=mock.Mock(),
            _git_common_directory=lambda _path: Path('/common'),
            _owned_rebase_state_dir=lambda *_args: None,
            _plan_id_from_path=lambda _path: 'G-199',
            _rebase_in_progress_for_branch=lambda *_args: False,
            _scope_snapshot_with_start_ignore_rules=lambda _worktree, _before, after: after,
            agent_snapshot_changes=lambda *_args: [],
            capture_git_snapshot=lambda _worktree: current,
            git=git,
            operation_log=mock.Mock(),
            validate_managed_worktree=mock.Mock(),
        )
        scope_validation.validate_agent_scope(
            'worker', Path('/managed'), before, current,
            selected_plan_path='docs/plans/[G-199] Work.md',
            external_worktree=Path('/source'), base_branch='main',
            connection=object(),
            resources=used_git_resources(role='worker', phase='WORKING',
                base_branch='main', branch=before.branch, worktree_key='/managed'),
            effects=validation_effects,
        )
        git.assert_not_called()

    def test_checkpoint_accepts_only_proven_derived_head_and_preserves_other_refs(self):
        # Test the same pure normalization used before checkpoint ref checks.
        from loop_manager_internal.agents.agent_scope_decision import reconcile_manager_metadata
        ref, head = 'refs/remotes/origin/develop', 'refs/remotes/origin/HEAD'
        before = snapshot(refs={ref: ('a', ''), head: ('a', ref), 'refs/tags/foreign': ('old', '')})
        current = replace(before, refs={ref: ('b', ''), head: ('b', ref), 'refs/tags/foreign': ('new', '')})
        normalized = reconcile_manager_metadata(before, current, refs=frozenset({ref}), worktrees=frozenset())
        self.assertEqual(normalized.refs[head], ('b', ref))
        self.assertEqual(normalized.refs['refs/tags/foreign'], ('old', ''))
        for target in ('refs/remotes/origin/main', ''):
            changed = replace(current, refs={**current.refs, head: ('b', target)})
            self.assertEqual(reconcile_manager_metadata(before, changed, refs=frozenset({ref}),
                worktrees=frozenset()).refs[head], before.refs[head])


class RuntimeSelectionEvidenceUnitTests(unittest.TestCase):
    def test_selected_fetch_remains_valid_after_live_forward_but_not_rewrite(self):
        import tempfile
        from types import SimpleNamespace
        from loop_manager_internal.runtime.bootstrap import BootstrapService
        from loop_manager_internal.git import manager_git_journal as journal
        with tempfile.TemporaryDirectory() as directory:
            state, source = Path(directory), Path('/source').resolve()
            ref, a, b, c = 'refs/remotes/origin/develop', 'a' * 40, 'b' * 40, 'c' * 40
            observed = {'oid': a}
            journal.execute(state, owner='bootstrap', kind='bootstrap_fetch', repo=str(source), target=ref,
                before={'oid': a}, expected={'oid': b}, observe=lambda: observed,
                apply=lambda _token: observed.update(oid=b), marker=lambda _intent: True)
            path = next(journal._slot(state, str(source), 'bootstrap_fetch', ref).glob('*.intent.json'))
            token = journal._read(path)['id']
            def git(_repo, *args, **kwargs):
                stdout = (c + '\trefs/heads/develop\n' if args[0] == 'ls-remote' else
                          c if args[0] == 'rev-parse' else b + '\0loop-manager-base-fetch:' + token)
                return SimpleNamespace(returncode=0, stdout=stdout)
            effects = mock.Mock(git=git)
            service = BootstrapService(effects, runtime_root=Path('/runtime'))
            self.assertTrue(service._selected_runtime_revision(source, state, 'develop', b, c))
            original = path.read_bytes()
            effects.git = lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout='')
            self.assertFalse(service._selected_runtime_revision(source, state, 'develop', b, c))
            self.assertEqual(path.read_bytes(), original)
            effects.git = git
            self.assertFalse(service._selected_runtime_revision(source, state, 'develop', a, c))

    def test_removed_comparison_runtime_requires_exact_receipt_and_absence(self):
        import tempfile
        import json
        from loop_manager_internal.runtime.runtime_scope import proven_runtime_removal
        from loop_manager_internal.runtime.runtime_identity import expected_runtime_worktree
        from loop_manager_internal.git import manager_git_journal as journal
        with tempfile.TemporaryDirectory() as directory:
            state, source, sha = Path(directory), Path('/source'), 'b' * 40
            runtime = expected_runtime_worktree(state, source, sha)
            registration = ('HEAD=' + sha, 'detached=True', 'locked=loop-manager-runtime:owned')
            before = {'exists': True, 'registration': {'head': sha, 'detached': True, 'locked': 'loop-manager-runtime:owned'}}
            live = dict(before)
            def remove(token):
                live.update(exists=False, registration=None)
                receipts = state / 'runtime-removals'
                receipts.mkdir()
                (receipts / (token + '.json')).write_text(json.dumps(
                    {'id': token, 'repo': str(source), 'target': str(runtime), 'before': before}))
            journal.execute(state, owner='bootstrap', kind='runtime_remove', repo=str(source), target=str(runtime),
                before=before, expected={'exists': False, 'registration': None},
                observe=lambda: live, apply=remove, marker=lambda _intent: True)
            self.assertTrue(proven_runtime_removal(state, source, runtime, registration))
            self.assertFalse(proven_runtime_removal(state, source, runtime, (*registration, 'unknown=True')))
            self.assertFalse(proven_runtime_removal(state, source, runtime.with_name('other'), registration))
            runtime.mkdir(parents=True)
            self.assertFalse(proven_runtime_removal(state, source, runtime, registration))
            runtime.rmdir()
            receipt = next((state / 'runtime-removals').glob('*.json'))
            receipt.write_text('{}')
            self.assertFalse(proven_runtime_removal(state, source, runtime, registration))
