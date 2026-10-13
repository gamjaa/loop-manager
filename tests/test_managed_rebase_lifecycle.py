"""Real Git/SQLite rebase lifecycle coverage; run by Manager full verify only.

Git, journal, stage validation, correction turns, checkpoint recovery and phase
handlers are real. Agent CLI and catalog services use deterministic fixtures.
"""
from tests.support.execution import execution_fixture
from tests.support.pr_recovery import observe_live_pull_request
from tests.support.recovery_boundary import RecoveryBoundary

from contextlib import ExitStack, nullcontext
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind, ExecutionPhase
from loop_manager_internal.execution.execution_state import ExecutionStatePort
import loop_manager as lm
from loop_manager_internal.git import manager_git_journal as journal
from loop_manager_internal.git import direct_rebase_service, managed_rebase_publication
from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.execution.worker_session import SessionBinding
from tests.support import execution as policy_fixture
from tests.support.git import isolated_git_test


class SimulatedProcessExit(BaseException):
    pass


def _restart_execution_effects(effects, database, repo):
    effects.state.connection.commit()
    effects.state.connection.close()
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return replace(effects, state=ExecutionStatePort(
        connection, runtime_state.RuntimeStateRepository(connection), repo),
        checkpoint=ExecutionCheckpointRepository(connection))


class RebaseObservationFailureTests(unittest.TestCase):
    def test_restarted_fixture_updates_audit_on_current_connection_and_keeps_checkpoint(self):
        fixture = execution_fixture(self)
        with TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Path(directory) / 'execution.db'
            connection = sqlite3.connect(database)
            cleanup.callback(connection.close)
            fixture.connection.commit()
            fixture.connection.backup(connection)
            connection.row_factory = sqlite3.Row
            effects = replace(fixture.effects, state=ExecutionStatePort(
                connection, runtime_state.RuntimeStateRepository(connection), fixture.repo),
                checkpoint=ExecutionCheckpointRepository(connection))
            effects.state.update_project(fixture.repo, worker_session_id='saved-worker')
            effects.checkpoint.record(fixture.repo, 'G-047', expected_generation=0,
                operation='rebase', expected_head='saved-head', target_base_oid='base',
                observed_remote_oid=None, snapshot={'failure': 'prior failure'})
            checkpoint = effects.checkpoint.get(fixture.repo, 'G-047')
            for count in (1, 2):
                previous = effects.state.repository
                effects = _restart_execution_effects(effects, database, fixture.repo)
                cleanup.callback(effects.state.connection.close)
                with self.assertRaises(sqlite3.ProgrammingError):
                    previous.projected(fixture.repo)
                effects.state.repository.update_plan(fixture.repo, 'G-047', plan_values={
                    'retry_count': count, 'verify_failure_history': '["prior failure"]'}, enqueue=False)
                effects.state.expected = effects.state.repository.projected(fixture.repo)
                self.assertEqual(effects.state.active_execplan(fixture.repo)['retry_count'], count)
                self.assertEqual(effects.state.active_execplan(fixture.repo)['verify_failure_history'],
                                 '["prior failure"]')
                self.assertEqual(effects.state.row()['worker_session_id'], 'saved-worker')
                self.assertEqual(effects.checkpoint.get(fixture.repo, 'G-047'), checkpoint)
                self.assertIs(effects.checkpoint.connection, effects.state.connection)
            fixture.effects.workspace.git.assert_not_called()

    def test_preflight_database_failure_preserves_publication_intent(self):
        intent = {'remote_before': 'old', 'head': 'new'}
        current = SimpleNamespace(
            operation='rebase_publish', snapshot={'rebase_publication_intent': intent},
            rebase_publication_intent=lambda: intent,
        )
        for edge in ('checkpoint', 'state', 'owner'):
            with self.subTest(edge=edge):
                checkpoints = mock.Mock()
                checkpoints.get.return_value = current
                state = mock.Mock()
                state.row.return_value = {}
                git = mock.Mock()
                lookup_pr = mock.Mock()
                service = mock.Mock()
                if edge == 'checkpoint':
                    checkpoints.get.side_effect = sqlite3.OperationalError('database is locked')
                elif edge == 'state':
                    state.row.side_effect = sqlite3.OperationalError('database is locked')
                else:
                    checkpoints.connection.execute.side_effect = sqlite3.OperationalError('database is locked')

                with self.assertRaises(managed_rebase_publication.RebaseObservationRetry) as caught:
                    managed_rebase_publication.publish_checkpoint(
                        state=state, checkpoints=checkpoints, repo='/repo',
                        plan_id='G-001', repo_path=Path('/repo'), worktree=Path('/worktree'),
                        branch='loop-manager/G-001', service=service,
                        owns_remote=mock.Mock(), git=git, lookup_pr=lookup_pr)

                self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
                self.assertEqual(current.snapshot['rebase_publication_intent'], intent)
                checkpoints.begin_rebase_publication.assert_not_called()
                state.complete_rebase_publication.assert_not_called()
                git.assert_not_called()
                lookup_pr.assert_not_called()
                service.assert_not_called()

    def test_pr_lookup_failure_retries_saved_rebase_intent(self):
        intent = {'remote_before': 'old', 'head': 'new',
                  'acceptance': {'remote': {'pull_request': None}}}
        current = SimpleNamespace(
            operation='rebase_publish', snapshot={'rebase_publication_intent': intent},
            rebase_publication_intent=lambda: intent,
        )
        checkpoints = mock.Mock()
        checkpoints.get.return_value = current
        checkpoints.connection.execute.return_value.fetchone.return_value = None
        state = mock.Mock()
        state.row.return_value = {'pr_url': None, 'pr_conflict_recovery': None}
        git = mock.Mock()
        with mock.patch.object(managed_rebase_publication.manager_git_journal,
                               'live_remote_branch_oid', return_value='old'):
            with self.assertRaises(managed_rebase_publication.RebaseObservationRetry):
                managed_rebase_publication.publish_checkpoint(
                    state=state, checkpoints=checkpoints, repo='/repo',
                    plan_id='G-001', repo_path=Path('/repo'), worktree=Path('/worktree'),
                    branch='loop-manager/G-001', service=mock.Mock(),
                    owns_remote=mock.Mock(), git=git,
                    lookup_pr=mock.Mock(side_effect=RuntimeError('changed diagnostic')))
        git.assert_not_called()

    def test_direct_rebase_observation_failure_retries_but_rejection_remains_false(self):
        service = mock.Mock()
        effects = direct_rebase_service.DirectRebaseHistoryProvenEffects(
            _current_rebase_service=lambda *args, **kwargs: service,
            _owned_rebase_state_dir=lambda *args: Path('/state'),
        )
        args = (None, Path('/repo'), '/repo', 'G-001', Path('/worktree'),
                'loop-manager/G-001', 'before', 'after', 'base', None)
        service.observe.side_effect = RuntimeError('different diagnostic')
        with self.assertRaises(RecoveryFailure) as caught:
            direct_rebase_service._direct_rebase_history_proven(effects, *args)
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        service.observe.side_effect = None
        service.observe.return_value = (
            SimpleNamespace(head='after'), SimpleNamespace(status=RebaseStatus.REJECTED))
        self.assertFalse(direct_rebase_service._direct_rebase_history_proven(effects, *args))
        service.observe.return_value = (
            SimpleNamespace(detail='unavailable'), SimpleNamespace(status=RebaseStatus.UNAVAILABLE))
        with self.assertRaises(RecoveryFailure) as caught:
            direct_rebase_service._direct_rebase_history_proven(effects, *args)
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)

    def test_remote_query_failure_retries_without_rebase_publication(self):
        current = SimpleNamespace(
            operation='rebase_publish',
            snapshot={'rebase_publication_intent': {'remote_before': 'old', 'head': 'new'}},
            rebase_publication_intent=lambda: {'remote_before': 'old', 'head': 'new'},
        )
        checkpoints = mock.Mock()
        checkpoints.get.return_value = current
        checkpoints.connection.execute.return_value.fetchone.return_value = None
        state = mock.Mock()
        state.row.return_value = {}
        git = mock.Mock()
        with mock.patch.object(managed_rebase_publication.manager_git_journal,
                               'live_remote_branch_oid',
                               side_effect=journal.MutationEvidenceError('changed text')):
            with self.assertRaises(RecoveryFailure) as caught:
                managed_rebase_publication.publish_checkpoint(
                    state=state, checkpoints=checkpoints, repo='/repo',
                    plan_id='G-001', repo_path=Path('/repo'), worktree=Path('/worktree'),
                    branch='loop-manager/G-001', service=mock.Mock(),
                    owns_remote=mock.Mock(), git=git, lookup_pr=mock.Mock())
        self.assertIs(caught.exception.reason, RecoveryReason.PUBLICATION_RETRY)
        self.assertIsInstance(caught.exception, managed_rebase_publication.RebaseObservationRetry)
        git.assert_not_called()
        with mock.patch.object(managed_rebase_publication.manager_git_journal,
                               'live_remote_branch_oid', return_value='unexpected'):
            with self.assertRaises(RecoveryFailure) as caught:
                managed_rebase_publication.publish_checkpoint(
                    state=state, checkpoints=checkpoints, repo='/repo',
                    plan_id='G-001', repo_path=Path('/repo'), worktree=Path('/worktree'),
                    branch='loop-manager/G-001', service=mock.Mock(),
                    owns_remote=mock.Mock(), git=git, lookup_pr=mock.Mock())
        self.assertIs(caught.exception.reason, RecoveryReason.REMOTE_OID_UNCERTAIN)


@unittest.skipIf(os.name == "nt", "Linux mode-bit and bash verification fixture")
@isolated_git_test
class ManagedRebaseLifecycleTests(unittest.TestCase):
    def test_conflict_record_change_survives_database_restart_and_resumes_same_worker(self):
        self._lifecycle(with_pr=False, definition_change='record')

    def test_conflict_condition_change_after_database_restart_resumes_without_touching_index(self):
        self._lifecycle(with_pr=False, definition_change='condition')

    def test_checkpoint_multiconflict_skip_restart_verify_review(self):
        self._lifecycle(with_pr=False, sealed_restart=(False, True))

    def test_pr_multiconflict_skip_restart_verify_review(self):
        self._lifecycle(with_pr=True, sealed_restart=(False, True))

    def test_legacy_same_file_replay_restart_before_journal_seal(self):
        self._lifecycle(with_pr=True, legacy=True, sealed_restart=(False, True))

    def test_legacy_checkpoint_restart_before_journal_seal(self):
        self._lifecycle(with_pr=False, legacy=True, sealed_restart=(False, True))

    def test_manual_checkpoint_completion_without_journal_reconfirms_candidate(self):
        self._lifecycle(with_pr=False, manual=True, missing_journal=True)

    def test_manual_pr_completion_after_seal_reconfirms_candidate(self):
        self._lifecycle(with_pr=True, manual=True, sealed_restart=True)

    def test_manual_completion_with_new_base_and_changed_inputs_reconfirms_latest_candidate(self):
        self._lifecycle(with_pr=False, manual=True, latest_base_advanced=True)

    def test_checkpoint_restart_before_intermediate_push(self):
        self._lifecycle(with_pr=False, publication_interruption='before-push')

    def test_checkpoint_restart_after_intermediate_push(self):
        self._lifecycle(with_pr=False, publication_interruption='after-push')

    def test_checkpoint_restart_before_intermediate_sqlite_completion(self):
        self._lifecycle(with_pr=False, publication_interruption='sqlite')

    def test_checkpoint_permission_rejection_retries_original_intent(self):
        self._lifecycle(with_pr=False, publication_interruption='permission')

    def test_checkpoint_remote_race_rejects_original_lease_without_overwrite(self):
        self._lifecycle(with_pr=False, publication_interruption='remote-race')

    def test_checkpoint_remote_deletion_after_interrupted_push_is_preserved(self):
        self._lifecycle(with_pr=False, publication_interruption='remote-deleted')

    def test_checkpoint_pr_appears_after_interrupted_push_is_preserved(self):
        self._lifecycle(with_pr=False, publication_interruption='new-pr')

    def test_legacy_multiple_unpublished_rebases_without_journal_publish_final_head(self):
        self._lifecycle(with_pr=False, manual=True, missing_journal=True, cumulative=True)

    def _lifecycle(self, *, with_pr, legacy=False, sealed_restart=False,
                   manual=False, missing_journal=False, latest_base_advanced=False,
                   publication_interruption=None, cumulative=False, definition_change=None):
        # Reuse only the deterministic service bundle, not the policy fixture's
        # substituted Manager rebase methods or its substituted journal writer.
        real_begin = journal.begin_rebase
        fixture = execution_fixture(self)
        with RecoveryBoundary() as boundary, mock.patch.object(journal, 'begin_rebase', real_begin):
            root = Path(boundary.directory)
            repo, remote, state_dir = root / 'repo', root / 'remote.git', root / 'state'
            state_dir.mkdir()
            repo_key = str(repo.resolve())
            for args in (('init', '--bare', str(remote)), ('init', '-b', 'main', str(repo))):
                subprocess.run(['git', *args], check=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
            git = lm.git
            git(repo, 'config', 'user.name', 'Lifecycle Test')
            git(repo, 'config', 'user.email', 'lifecycle@example.test')
            git(repo, 'config', 'core.fileMode', 'true')
            git(repo, 'remote', 'add', 'origin', str(remote))
            lines = ['value=initial\n'] + [f'line-{n}\n' for n in range(1, 25)]
            (repo / 'docs/plans').mkdir(parents=True)
            (repo / 'docs/PRODUCT.md').write_text('# Product\n\nFixture behavior.\n')
            (repo / 'docs/plans/[G-047] Runtime.md').write_text(
                '---\nid: G-047\ntitle: Runtime\nsources:\n  - docs/PRODUCT.md\n'
                'depends_on: []\ncomplexity: low\nrisk: low\npriority: normal\n---\n'
                '\n## Purpose\nResolve current changes.\n' +
                ('\n## Progress\n- [ ] Required targeted unit test\n## Decision Log\n'
                 if definition_change else ''))
            (repo / 'app.txt').write_text(''.join(lines))
            (repo / 'skip.txt').write_text('initial\n')
            (repo / 'scripts').mkdir()
            # This fixture verification runs only when Manager executes this
            # integration test, never in the Worker's targeted unit command.
            (repo / 'scripts/verify.sh').write_text(
                "#!/usr/bin/env bash\nset -eu\npython3 - <<'CHECK'\n"
                "from pathlib import Path\n"
                "p = Path('app.txt')\n"
                "assert p.read_text().splitlines()[0] == 'value=resolved'\n"
                "assert p.read_text().splitlines()[20] == 'later-content'\n"
                "assert p.stat().st_mode & 0o111\n"
                "assert Path('skip.txt').read_text() == 'base\\n'\n"
                "print('phase-bound candidate verified')\nCHECK\n")
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'Initial fixture')
            base = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'push', '-u', 'origin', 'main')
            branch, worktree = lm.expected_execplan_workspace(state_dir, repo_key, 'G-047')
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git(repo, 'worktree', 'add', '-b', branch, str(worktree), base)

            def commit(path, content, title, *, executable=False):
                target = worktree / path
                target.write_text(content)
                if executable:
                    target.chmod(0o755)
                git(worktree, 'add', '--', path)
                if executable:
                    git(worktree, 'update-index', '--chmod=+x', '--', path)
                git(worktree, 'commit', '-m', title)

            commit('notes.txt', 'independent\n', 'Independent first commit')
            if not publication_interruption:
                changed = lines.copy()
                changed[0] = 'value=worker\n'
                commit('app.txt', ''.join(changed), 'Middle conflicting commit')
                if not legacy:
                    commit('skip.txt', 'worker\n', 'Second conflict to resolve as empty')
                changed[20] = 'later-content\n'
                commit('app.txt', ''.join(changed), 'Later content and mode', executable=True)
                changed[12] = 'another-later-change\n'
                commit('app.txt', ''.join(changed), 'Another later same-file change')
            original = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
            git(worktree, 'push', 'origin', branch)
            upstream = lines.copy()
            upstream[0] = 'value=base\n'
            (repo / 'app.txt').write_text(''.join(upstream))
            (repo / 'skip.txt').write_text('base\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'Conflicting latest base')
            target = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'push', 'origin', 'main')
            git(repo, 'fetch', 'origin')
            fixture.remote = target

            database = state_dir / 'execution.db'
            connection = sqlite3.connect(database)
            fixture.connection.commit()
            fixture.connection.backup(connection)
            connection.row_factory = sqlite3.Row
            self.addCleanup(lambda: connection.close())
            connection.execute('UPDATE projects SET repo = ?', (repo_key,))
            connection.execute('UPDATE exec_plans SET repo = ?, plan_sha = ?, current_revision_sha = ?, '
                               "current_complexity = 'low', current_risk = 'low'", (repo_key, base, target))
            repository = runtime_state.RuntimeStateRepository(connection)
            state = ExecutionStatePort(connection, repository, repo_key)
            state.update_project(
                repo_key, worktree=str(worktree), branch=branch, active_sha=base,
                phase='VERIFYING', publication_remote_oid=original,
                worker_session_id='worker-session',
                worker_session_binding=SessionBinding('codex', 'model', 'high').serialize(),
                worker_result=policy_fixture.READY_WORKER_RESULT,
                pr_url='https://example.test/pr/47' if with_pr else None,
                pr_head_oid=original if with_pr else None,
                pr_conflict_recovery=int(with_pr),
                pr_conflict_head_oid=original if with_pr else None,
                pr_conflict_base_oid=target if with_pr else None,
            )
            if not with_pr:
                # A clean checkpoint can reuse its existing local commit. Bind
                # its legacy identity exactly as the checkpoint publisher does.
                tree = git(worktree, 'rev-parse', 'HEAD^{tree}').stdout.strip()
                identity = blocked_checkpoint.checkpoint_identity(
                    plan_id='G-047', reason_code='verify_retry_exhausted',
                    evidence='fixture checkpoint', parent_oid=original,
                    tree_oid=tree, included_paths=())
                connection.execute(
                    "INSERT INTO blocked_checkpoints "
                    "(repo, plan_id, request_identity, phase, reason_code, evidence, "
                    "branch, worktree, commit_identity, tree_oid, local_oid, "
                    "published_oid, accepted_remote_oid, result) "
                    "VALUES (?, 'G-047', 'checkpoint', 'published', "
                    "'verify_retry_exhausted', 'fixture checkpoint', ?, ?, ?, ?, ?, ?, ?, 'published')",
                    (repo_key, branch, str(worktree), identity, tree, original, original, original))

            effects = replace(fixture.effects, state=state,
                              checkpoint=ExecutionCheckpointRepository(connection))
            effects = replace(effects, workspace=replace(effects.workspace,
                current_rebase_service=lambda context, **options: lm._current_rebase_service(
                    connection, repo, state_dir, repo_key, 'G-047', worktree, branch, **options)))

            def parse_lifecycle_worker_result(_message, _plan):
                active_base = effects.state.row()['active_sha']
                head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
                paths = git(worktree, 'diff', '--name-only', active_base, head).stdout.splitlines()
                changes = []
                for path in paths:
                    if not (path.startswith('docs/') or path.lower().endswith(('.md', '.rst', '.txt'))):
                        continue
                    patch = git(worktree, 'diff', '--no-ext-diff', '--no-textconv',
                                '--find-renames', active_base, head, '--', path).stdout
                    changes.append('DOCUMENT_CHANGE: ' + json.dumps({
                        'path': path,
                        'summary': 'Preserve the lifecycle fixture change.',
                        'reason': 'The fixture models an explained Worker result.',
                        'impact': 'Lifecycle assertions retain the real document-report gate.',
                        'patch_sha256': hashlib.sha256(patch.encode('utf-8')).hexdigest(),
                    }))
                return SimpleNamespace(
                    commit_title='Resolve managed rebase fixture conflicts',
                    test_evidence='targeted unit test: PASS',
                    summary='\n'.join(['Rebase lifecycle fixture result.', *changes]),
                )

            effects.validation.parse_worker_result.side_effect = parse_lifecycle_worker_result
            pr_patch = mock.patch.object(lm, 'pr_for_branch', side_effect=lambda *_a, **_k:
                                        effects.publication.pr_for_branch(*_a, **_k))
            pr_patch.start()
            self.addCleanup(pr_patch.stop)
            effects.workspace.git.side_effect = git
            effects.workspace.is_directory.side_effect = lambda path: path.is_dir()
            effects.workspace.remote_branch_oid.side_effect = lm.remote_branch_oid
            effects.workspace._rebase_in_progress_for_branch.side_effect = lm._rebase_in_progress_for_branch
            effects.workspace.git_operation_entries.side_effect = lm._git_operation_entries
            effects.workspace.validate_managed_worktree.side_effect = lm.validate_managed_worktree
            effects.workspace.validate_execplan_workspace_identity.side_effect = lm.validate_execplan_workspace_identity
            effects.workspace.branch_has_changes.side_effect = lm.branch_has_changes
            effects.agent.capture_git_snapshot.side_effect = lm.capture_git_snapshot
            effects.agent.path_signature.side_effect = lm._path_signature
            effects.agent.begin_agent_scope.side_effect = lambda _repo, path, *_a, **_k: lm.capture_git_snapshot(path)
            effects.agent.validate_agent_scope.side_effect = lm.validate_agent_scope
            effects.publication.pr_for_branch.return_value = (
                {'state': 'OPEN', 'headRefOid': original, 'url': 'https://example.test/pr/47',
                 'headRefName': branch, 'baseRefName': 'main'}
                if with_pr else None)
            if with_pr:
                saved_pr = effects.publication.pr_for_branch.return_value
                effects.publication.pr_for_branch.side_effect = lambda *_a, **_k: observe_live_pull_request(repo, saved_pr)
            events = []
            resolutions = []

            def validate_worker(_repo, path, _branch, _base, plan_path, _catalog, before, **kwargs):
                lm.validate_agent_scope('worker', path, before, lm.capture_git_snapshot(path),
                                        selected_plan_path=plan_path)
                return fixture.plan

            effects.agent.validate_worker_workspace_scope.side_effect = validate_worker
            if definition_change:
                effects.revision.body_hash.side_effect = lm._revision_execplan_body_hash
                effects.revision.source_fingerprint.side_effect = lm._revision_execplan_source_fingerprint
                effects.revision.changed_paths.side_effect = lm._revision_changed_paths
                from tests.support.publication import seed_validated_candidate
                seed_validated_candidate(connection, repo, worktree, 'G-047')
                effects.state.expected = effects.state.repository.projected(repo_key)

            def agent(_provider, path, _state, _config, role, *_args, **kwargs):
                events.append(role)
                kwargs['on_session_started'](role + '-session')
                if role == 'worker' and lm._rebase_in_progress_for_branch(path, branch):
                    source = git(path, 'rev-parse', 'REBASE_HEAD').stdout.strip()
                    self.assertNotIn(source, resolutions, 'completed correction repeated')
                    resolutions.append(source)
                    conflicts = git(path, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
                    if conflicts == ['app.txt']:
                        resolved = upstream.copy()
                        resolved[0] = 'value=resolved\n'
                        (path / 'app.txt').write_text(''.join(resolved))
                    else:
                        self.assertEqual(conflicts, ['skip.txt'])
                        (path / 'skip.txt').write_text('base\n')
                return lm.AgentResult(0, 'VERDICT: PASS' if role == 'reviewer'
                                      else policy_fixture.READY_WORKER_RESULT,
                                      session_id=role + '-session')

            effects.agent.run_agent.side_effect = agent

            def verify(path):
                events.append('verify')
                return lm.verify(path, timeout_seconds=30)

            effects.validation.verify.side_effect = verify

            def new_handler():
                instance = handlers.ExecutionHandlers(
                    effects, repo=repo_key, repo_path=repo, state_dir=state_dir,
                    base='main', base_sha=target, provider='codex',
                    config_path=root / 'config', newly_activated=False)
                instance.plan = fixture.plan
                instance.catalog_plan = effects.state.active_execplan(repo_key)
                return instance

            def restart():
                nonlocal connection, effects
                effects = _restart_execution_effects(effects, database, repo_key)
                connection = effects.state.connection
                return new_handler()

            if legacy:
                # Emit the pre-v2 journal format at creation time. Keep actual
                # Git observations, approved tree and reflog invocation IDs;
                # do not downgrade or rewrite any sealed modern receipt.
                def begin_legacy(state_path, **kwargs):
                    expected = kwargs['expected']
                    kwargs['expected'] = {
                        key: expected[key] for key in (
                            'branch', 'before_head', 'target_base_oid',
                            'remote_oid', 'affected_paths', 'agent_session_id',
                            'action', 'accepted_files', 'staged_tree_oid',
                        ) if key in expected
                    }
                    current = effects.checkpoint.get(repo_key, 'G-047')
                    if (current.snapshot or {}).get('rebase_evidence_version') == 2:
                        effects.checkpoint.record(
                            repo_key, 'G-047', expected_generation=current.generation,
                            operation=current.operation, expected_head=current.expected_head,
                            target_base_oid=current.target_base_oid,
                            observed_remote_oid=current.observed_remote_oid,
                            snapshot={key: value for key, value in current.snapshot.items()
                                      if not key.startswith('rebase_')},
                        )
                    return real_begin(state_path, **kwargs)

                journal.begin_rebase = begin_legacy

            handler = new_handler()
            if publication_interruption:
                # 게시 경계만 검사할 때는 실제 Git으로 후보와 pending SQLite를
                # 준비한다. Manager의 충돌 교정·journal 경계는 다른 12개 흐름이 검사한다.
                current = effects.checkpoint.get(repo_key, 'G-047')
                effects.checkpoint.record(repo_key, 'G-047',
                    expected_generation=current.generation, operation='rebase',
                    expected_head=original, target_base_oid=target,
                    observed_remote_oid=original, snapshot={'rebase_remote_oid': original})
                # 독립된 임시 worktree를 target에 놓고 최종 허용 tree를 만든다.
                # 원격 branch는 original을 유지하므로 실제 lease 경쟁도 검사한다.
                git(worktree, 'reset', '--hard', target)
                resolved = upstream.copy()
                resolved[0] = 'value=resolved\n'
                resolved[20] = 'later-content\n'
                resolved[12] = 'another-later-change\n'
                (worktree / 'app.txt').write_text(''.join(resolved))
                (worktree / 'app.txt').chmod(0o755)
                (worktree / 'notes.txt').write_text('independent\n')
                git(worktree, 'add', '--', 'app.txt', 'notes.txt')
                git(worktree, 'update-index', '--chmod=+x', '--', 'app.txt')
                git(worktree, 'commit', '-m', 'Publication boundary candidate')
            else:
                handler._rebase_latest(handler._context(), handler._row(), target, publishing=with_pr)
                self.assertTrue(lm._rebase_in_progress_for_branch(worktree, branch))
            handler = restart()
            if not publication_interruption:
                conflict_context = handler.recover(handler._context())
                self.assertNotIsInstance(conflict_context, ExecutionOutcome)
                self.assertTrue(conflict_context.resolving_rebase)
            self.assertEqual(events, [])
            if definition_change:
                # Manager-only integration boundary: real conflict/index and
                # durable SQLite are retained; no verify or publication follows.
                plan_path = 'docs/plans/[G-047] Runtime.md'
                text = (repo / plan_path).read_text()
                changed_text = (text.replace('[ ]', '[x]') +
                    '- 2026-10-08 실행 기록: `python3 -m unittest tests.test_code` → PASS (2 tests).\n')
                if definition_change == 'condition':
                    changed_text += '- New condition: skip required validation.\n'
                (repo / plan_path).write_text(changed_text)
                git(repo, 'add', '--', plan_path)
                git(repo, 'commit', '-m', 'Change records or implementation condition')
                git(repo, 'push', 'origin', 'main')
                git(repo, 'fetch', 'origin')
                fixture.remote = git(repo, 'rev-parse', 'HEAD').stdout.strip()
                connection.execute('UPDATE exec_plans SET current_revision_sha=?', (fixture.remote,))
                effects.state.repository.update_plan(repo_key, 'G-047', plan_values={
                    'retry_count': 2, 'verify_failure_history': '["prior failure"]'}, enqueue=False)
                effects.state.expected = effects.state.repository.projected(repo_key)
                def git_state():
                    return (git(worktree, 'rev-parse', 'HEAD').stdout,
                            git(worktree, 'rev-parse', 'REBASE_HEAD').stdout,
                            git(worktree, 'ls-files', '--stage').stdout,
                            git(worktree, 'diff', '--binary', 'HEAD').stdout,
                            lm._git_operation_entries(worktree))
                before_git = git_state()
                before_row = dict(effects.state.row())
                handler = restart()
                context = handler.recover(handler._context())
                self.assertNotIsInstance(context, ExecutionOutcome)
                self.assertTrue(context.resolving_rebase)
                result = handler.guard(context)
                self.assertEqual(git_state(), before_git)
                self.assertEqual(effects.state.row()['worker_session_id'], 'worker-session')
                plan = effects.state.active_execplan(repo_key)
                self.assertEqual(plan['retry_count'], 2)
                self.assertEqual(plan['verify_failure_history'], '["prior failure"]')
                self.assertEqual(events, [])
                if definition_change == 'condition':
                    self.assertNotIsInstance(result, ExecutionOutcome)
                    self.assertIsNone(effects.state.row()['worker_result'])
                    self.assertIsNone(effects.state.row()['verify_token'])
                    self.assertEqual(effects.state.row()['definition_snapshot'], before_row['definition_snapshot'])
                else:
                    self.assertNotIsInstance(result, ExecutionOutcome)
                    for field in ('definition_snapshot', 'worker_result', 'last_error'):
                        self.assertEqual(effects.state.row()[field], before_row[field])
                handler.run(result)
                self.assertEqual(events, ['worker'])
                self.assertEqual(effects.agent.run_agent.call_args.kwargs['session_id'], 'worker-session')
                self.assertEqual(len(resolutions), 1)
                self.assertEqual(git(worktree, 'diff', '--name-only', '--diff-filter=U').stdout.strip(), 'skip.txt')
                effects.validation.verify.assert_not_called()
                effects.publication.push_managed_publication_branch.assert_not_called()
                return
            if not manual and not publication_interruption and not legacy:
                handler.run(conflict_context)
                self.assertEqual(len(resolutions), 1)
                self.assertEqual(git(worktree, 'diff', '--name-only', '--diff-filter=U').stdout.strip(), 'skip.txt')
            variants = sealed_restart if isinstance(sealed_restart, tuple) else (sealed_restart,)
            if len(variants) > 1:
                boundary.save(connection)
            prepared_events, prepared_resolutions = events.copy(), resolutions.copy()
            for variant, sealed_restart in enumerate(variants):
                with self.subTest(sealed_restart=sealed_restart):
                    if variant:
                        connection = boundary.restore(connection)
                        effects = replace(effects, state=ExecutionStatePort(
                            connection, runtime_state.RuntimeStateRepository(connection), repo_key),
                            checkpoint=ExecutionCheckpointRepository(connection))
                        effects.workspace.git.side_effect = git
                        events[:], resolutions[:] = prepared_events, prepared_resolutions
                        handler = new_handler()
                    if manual:
                        while lm._rebase_in_progress_for_branch(worktree, branch):
                            source = git(worktree, 'rev-parse', 'REBASE_HEAD').stdout.strip()
                            resolutions.append(source)
                            conflicts = git(worktree, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
                            if 'app.txt' in conflicts:
                                resolved = upstream.copy()
                                resolved[0] = 'value=resolved\n'
                                (worktree / 'app.txt').write_text(''.join(resolved))
                                git(worktree, 'add', '--', 'app.txt')
                                git(worktree, '-c', 'core.editor=true', 'rebase', '--continue', check=False)
                            elif 'skip.txt' in conflicts:
                                (worktree / 'skip.txt').write_text('base\n')
                                git(worktree, 'add', '--', 'skip.txt')
                                git(worktree, 'rebase', '--skip', check=False)
                            else:
                                self.fail('unexpected manual conflict')
                        if sealed_restart:
                            handler._finish_rebase_journal(handler._context())
                        if missing_journal:
                            (state_dir / 'manager-git-journal').rename(state_dir / 'preserved-old-audit')
                    elif not publication_interruption:
                        real_post = handler._post_rebase_observation

                        def crash_after_git(context, target_oid, output, **kwargs):
                            if not lm._rebase_in_progress_for_branch(worktree, branch):
                                raise SimulatedProcessExit()
                            return real_post(context, target_oid, output, **kwargs)

                        crash = (mock.patch.object(effects.state, 'complete_rebase', side_effect=SimulatedProcessExit)
                                 if sealed_restart else mock.patch.object(handler, '_post_rebase_observation',
                                                                           side_effect=crash_after_git))
                        with crash:
                            with self.assertRaises(SimulatedProcessExit):
                                handler.run(handler._context())
                    resolution_count = 0 if publication_interruption else (1 if legacy else 2)
                    self.assertEqual(len(resolutions), resolution_count)
                    final_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
                    if cumulative:
                        # Recover the final currently owned result, without replaying
                        # two historical rebases or reconstructing absent receipts.
                        (repo / 'base-only.txt').write_text('second target base\n')
                        git(repo, 'add', '--', 'base-only.txt')
                        git(repo, 'commit', '-m', 'Second target before legacy recovery')
                        fixture.remote = git(repo, 'rev-parse', 'HEAD').stdout.strip()
                        git(repo, 'push', 'origin', 'main')
                        git(repo, 'fetch', 'origin')
                        git(worktree, 'rebase', fixture.remote)
                        final_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
                        pending = effects.checkpoint.get(repo_key, 'G-047')
                        effects.checkpoint.record(repo_key, 'G-047', expected_generation=pending.generation,
                            operation='rebase', expected_head=original, target_base_oid=fixture.remote,
                            observed_remote_oid=original, snapshot={'rebase_remote_oid': original})
                        connection.execute('UPDATE exec_plans SET current_revision_sha = ?', (fixture.remote,))
                    if latest_base_advanced:
                        (repo / 'docs/PRODUCT.md').write_text('# Product\n\nUpdated current input.\n')
                        git(repo, 'add', '--', 'docs/PRODUCT.md')
                        git(repo, 'commit', '-m', 'Advance inputs after completed Git rebase')
                        git(repo, 'push', 'origin', 'main')
                        fixture.remote = git(repo, 'rev-parse', 'HEAD').stdout.strip()
                        connection.execute('UPDATE exec_plans SET current_revision_sha = ?', (fixture.remote,))
                    slot = journal._slot(state_dir, str(repo.resolve()), 'managed_rebase', str(worktree.resolve()))
                    self.assertEqual((slot / 'active.json').exists(),
                                     not sealed_restart and not missing_journal and not manual
                                     and not publication_interruption)
                    sealed_before = {path.name: path.read_bytes()
                                     for path in slot.glob('*.result.json')}
                    intents_before = {path.name: path.read_bytes()
                                      for path in slot.glob('*.intent.json')}
                    if sealed_restart:
                        final_results = [journal._read(path) for path in slot.glob('*.result.json')
                                         if journal._read(path)['observed']['metadata'] is None]
                        self.assertEqual(len(final_results), 0 if manual else 1)
                        self.assertIsNone(effects.checkpoint.get(repo_key, 'G-047').completed_rebase())
                    self.assertEqual(effects.checkpoint.get(repo_key, 'G-047').operation, 'rebase')
                    audit_row = handler.rebase_corrections.get(repo_key, 'G-047')
                    audit_before = audit_row.audit if audit_row else ()
                    handler = restart()

                    def no_repeated_rebase(path, *args, **kwargs):
                        if 'rebase' in args and (not latest_base_advanced or args[-1] != fixture.remote):
                            self.fail('restart repeated completed Git rebase')
                        if args[0] == 'push':
                            events.append('checkpoint-push')
                        elif 'rebase' in args:
                            events.append('next-base-rebase')
                        return git(path, *args, **kwargs)

                    effects.workspace.git.side_effect = no_repeated_rebase
                    if publication_interruption:
                        # 준비한 후보의 게시는 추가 후보 작업보다 먼저 확정해야 한다.
                        publication_start = len(events)
                        attempted = []
                        def interrupted_push(path, *args, **kwargs):
                            if args[0] == 'push':
                                attempted.append(args)
                                if publication_interruption == 'permission':
                                    # A real receive hook models server policy rejection.
                                    hook = remote / 'hooks/pre-receive'
                                    hook.write_text('#!/bin/sh\necho permission-denied >&2\nexit 1\n')
                                    hook.chmod(0o755)
                                elif publication_interruption == 'remote-race':
                                    git(remote, 'update-ref', f'refs/heads/{branch}', target)
                                elif publication_interruption in ('before-push', 'remote-deleted', 'new-pr'):
                                    raise SimulatedProcessExit()
                                result = no_repeated_rebase(path, *args, **kwargs)
                                if publication_interruption == 'after-push':
                                    raise SimulatedProcessExit()
                                return result
                            return no_repeated_rebase(path, *args, **kwargs)
                        effects.workspace.git.side_effect = interrupted_push
                        completion = (mock.patch.object(effects.state, 'complete_rebase_publication',
                                                        side_effect=SimulatedProcessExit())
                                      if publication_interruption == 'sqlite' else nullcontext())
                        with completion:
                            try:
                                first = handler.recover(handler._context())
                            except SimulatedProcessExit:
                                first = None
                            else:
                                self.assertIsInstance(first, ExecutionOutcome)
                        pending = effects.checkpoint.get(repo_key, 'G-047')
                        self.assertEqual(pending.operation, 'rebase_publish')
                        intent = pending.snapshot['rebase_publication_intent']
                        self.assertEqual(intent['remote_before'], original)
                        self.assertEqual(attempted, [('push',
                            f'--force-with-lease=refs/heads/{branch}:{original}', 'origin',
                            f'{final_head}:refs/heads/{branch}')])
                        self.assertEqual(effects.state.row()['publication_remote_oid'], original)
                        publication_events = events[publication_start:]
                        self.assertNotIn('verify', publication_events)
                        self.assertNotIn('reviewer', publication_events)
                        self.assertNotIn('worker', publication_events)
                        effects.workspace.git.side_effect = no_repeated_rebase
                        if publication_interruption == 'permission':
                            (remote / 'hooks/pre-receive').unlink()
                        elif publication_interruption == 'remote-deleted':
                            git(remote, 'update-ref', '-d', f'refs/heads/{branch}')
                        elif publication_interruption == 'new-pr':
                            effects.publication.pr_for_branch.return_value = {
                                'url': 'https://example.test/pr/new', 'state': 'OPEN',
                                'headRefName': branch, 'headRefOid': original, 'baseRefName': 'main'}
                        handler = restart()
                        if publication_interruption in ('remote-race', 'remote-deleted', 'new-pr'):
                            with self.assertRaises(handlers.RecoveryFailure):
                                handler._publish_rebased_checkpoint(handler._context())
                            self.assertEqual(effects.checkpoint.get(repo_key, 'G-047'), pending)
                            expected_remote = None if publication_interruption == 'remote-deleted' else (
                                target if publication_interruption == 'remote-race' else original)
                            self.assertEqual(journal.live_remote_branch_oid(git, repo, branch), expected_remote)
                            self.assertEqual(git(worktree, 'rev-parse', 'HEAD').stdout.strip(), final_head)
                            return
                        handler._publish_rebased_checkpoint(handler._context())
                        self.assertEqual(effects.checkpoint.get(repo_key, 'G-047').snapshot['rebase_publication_intent'], intent)
                        if publication_interruption in ('after-push', 'sqlite'):
                            self.assertEqual(events.count('checkpoint-push'), 1)
                    recovered = handler.recover(handler._context())
                    self.assertNotIsInstance(recovered, ExecutionOutcome)
                    completed_checkpoint = effects.checkpoint.get(repo_key, 'G-047')
                    self.assertIsNone(completed_checkpoint.operation)
                    for name, content in {**sealed_before, **intents_before}.items():
                        self.assertEqual((slot / name).read_bytes(), content)
                    if sealed_restart:
                        self.assertEqual(completed_checkpoint.snapshot['current_rebase_acceptance']['head'], final_head)
                    handler._complete_rebase_intent(handler._context())
                    self.assertEqual(effects.checkpoint.get(repo_key, 'G-047'), completed_checkpoint)
                    if not with_pr:
                        self.assertTrue(handler._owned_checkpoint_remote(
                            handler._context(), handler._row(), final_head,
                            effects.checkpoint.get(repo_key, 'G-047'),
                        ))
                    self.assertEqual(effects.state.row()['worker_session_id'], 'worker-session')
                    audit_row = handler.rebase_corrections.get(repo_key, 'G-047')
                    self.assertEqual(audit_row.audit if audit_row else (), audit_before)
                    self.assertEqual(len(resolutions), resolution_count)
                    transitions = journal.verified_managed_transitions(
                        state_dir, str(repo.resolve()), 'plan:G-047',
                        agent_session_id='worker-session', git=git)
                    self.assertEqual(transitions, [], 'manual/current acceptance is not Manager replay evidence')
                    self.assertEqual(journal.live_remote_branch_oid(git, repo, branch), final_head)
                    self.assertEqual(effects.state.row()['publication_remote_oid'], final_head)
                    self.assertIn('checkpoint-push', events)
                    if with_pr:
                        self.assertEqual(effects.state.row()['pr_head_oid'], final_head)
                    self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
                    self.assertIsNone(effects.state.row()['worker_result'])
                    self.assertIsNone(effects.state.row()['verify_result'])
                    self.assertIsNone(effects.state.row()['review_result'])
                    if publication_interruption:
                        # 7개 게시 실패/중단 case 모두 위의 실제 원격 lease·intent·CAS·
                        # restart·멱등성·session·검증 무효화 assertion을 유지한다.
                        # 후속 Worker→verify→Review는 정상/수동/legacy 12개 case가 검사한다.
                        self.assertNotIn('worker', events)
                        self.assertNotIn('verify', events)
                        self.assertNotIn('reviewer', events)
                        effects.publication.push_managed_publication_branch.assert_not_called()
                        return
                    context = handler.guard(recovered)
                    if latest_base_advanced:
                        self.assertIsInstance(context, ExecutionOutcome)
                        context = handler.guard(handler._context())
                        final_head = git(worktree, 'rev-parse', 'HEAD').stdout.strip()
                        self.assertEqual(effects.state.row()['active_sha'], fixture.remote)
                        self.assertLess(events.index('checkpoint-push'), events.index('next-base-rebase'))
                        self.assertEqual(journal.live_remote_branch_oid(git, repo, branch), final_head)
                        self.assertEqual(events.count('checkpoint-push'), 2)
                    self.assertNotIsInstance(context, ExecutionOutcome)
                    self.assertEqual(context.phase, ExecutionPhase.IMPLEMENTING)
                    before_worker_count = events.count('worker')
                    context = handler.run(context).context
                    self.assertEqual(events.count('worker'), before_worker_count + 1)
                    self.assertEqual(context.phase, ExecutionPhase.VERIFYING)
                    verified = handler.verify(context, handler)
                    self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING, verified.detail)
                    reviewed = handler.review(verified.context)
                    self.assertEqual(reviewed.context.phase, ExecutionPhase.PUBLISHING, reviewed.detail)
                    self.assertEqual(events.count('verify'), 1)
                    self.assertEqual(events[-2:], ['verify', 'reviewer'])
                    self.assertEqual(len(resolutions), 1 if legacy else 2)
                    self.assertEqual(git(worktree, 'rev-parse', 'HEAD').stdout.strip(), final_head)
                    self.assertIn('phase-bound candidate verified', effects.state.row()['verify_result'])
                    effects.publication.push_managed_publication_branch.assert_not_called()
