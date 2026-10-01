"""Run the real phase policies with in-memory state and fake external effects."""
from dataclasses import fields, replace
from contextlib import ExitStack
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

import execution_handlers as handlers
import rebase_block_reconciliation
from execution_revision import DefinitionSnapshot
import loop_manager as lm
import notifications
import scoped_blocking
import runtime_state
from validation_feedback import Feedback
from validation_feedback_state import ValidationFeedbackRepository
from execution_checkpoint_state import CheckpointRecord, ExecutionCheckpointRepository
from requirement_archive import ArchiveSnapshot
from execution_state import ExecutionStatePort
from execution_runtime import (
    ExecutionPhase,
    ExecutionPhaseCoordinator,
    ExecutionOutcome,
    ExecutionOutcomeKind,
)
from tests import test_runtime_state
from tests.test_guidance_approval import guidance_plan
from tests import test_blocked_checkpoint
import blocked_checkpoint_state
import blocked_checkpoint
from worker_session import SessionBinding, SessionTransition
from tests.test_worker_outcome import decision as worker_decision

READY_WORKER_RESULT = 'WORKER_OUTCOME: {"kind":"ready_for_validation"}\nresult'
VALID_WORKER_RESULT = 'WORKER_OUTCOME: {"kind":"ready_for_validation"}\nvalid result'


def recovered_rebase_workspace(workspace, *, connection, repo_path, state_dir,
                               repo, plan_id, worktree):
    """Share the Manager workspace bindings between real Git and unit fixtures."""
    git_port = mock.Mock(wraps=lm.git)
    return replace(workspace, git=git_port,
        live_remote_branch_oid=mock.Mock(side_effect=lambda repo, branch:
            handlers.manager_git_journal.live_remote_branch_oid(git_port, repo, branch)),
        _rebase_in_progress_for_branch=lm._rebase_in_progress_for_branch,
        git_operation_entries=lm._git_operation_entries,
        current_rebase_service=lambda context, **options: lm._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id,
            worktree, context.branch, **options))


def recovered_rebase_publication(publication, state):
    """Use the Manager's snapshot-bound mutating publication adapters."""
    names = ('record_remote_publication_branch', 'push_managed_publication_branch',
             'push_owned_pr_conflict_recovery')
    return replace(publication, **{name: mock.Mock(side_effect=state.bind(
        getattr(lm, name), connected=True, mutates=True, name=name)) for name in names})


class ExecutionHandlerTests(TestCase):
    def test_manual_detached_block_recovers_changed_plan_without_rebase_journal(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, manual='waiting')

    def test_manual_stage_block_recovers_changed_plan_without_worker_stage_evidence(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, manual='staged')

    def test_manual_stage_owned_pr_requires_fresh_validation_before_publication(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=True, existing_pr=True, manual='staged',
        )

    def test_manual_completed_owned_pr_recovers_changed_plan_without_rebase_journal(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, existing_pr=True, manual='complete')

    def test_legacy_rebase_false_block_recovers_after_restart_with_later_file_changes(self):
        self._legacy_rebase_false_block_recovery(plan_block=True)

    def test_legacy_rebase_project_block_recovers_after_restart_with_later_file_changes(self):
        self._legacy_rebase_false_block_recovery(plan_block=False)

    def test_legacy_rebase_rejects_unstaged_files_after_git_completion(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, rejected='unstaged')

    def test_legacy_rebase_rejects_staged_index_after_git_completion(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, rejected='staged')

    def test_legacy_rebase_rejects_index_flags_after_git_completion(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, rejected='index-flags')

    def test_legacy_rebase_rejects_remote_branch_created_without_fetch(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, rejected='remote-created')

    def test_legacy_rebase_rejects_remote_branch_changed_without_fetch(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, rejected='remote-changed')

    def test_legacy_rebase_rejects_file_change_after_journal_sealing(self):
        self._legacy_rebase_false_block_recovery(plan_block=True, rejected='late-file')

    def test_legacy_rebase_rechecks_live_remote_after_journal_sealing(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, rejected='late-remote')

    def test_legacy_rebase_existing_pr_recovers_after_restart(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, existing_pr=True)

    def test_published_legacy_dirty_rejection_preserves_journal_for_restored_files(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=True, rejected='unstaged', published_checkpoint=True,
        )

    def test_published_legacy_staged_rejection_preserves_journal_for_restored_index(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=False, rejected='staged', published_checkpoint=True,
        )

    def test_legacy_project_recovery_ignores_stale_remote_cache(self):
        self._legacy_rebase_false_block_recovery(plan_block=False, stale_cache=True)

    def test_published_legacy_recovery_ignores_stale_remote_cache(self):
        self._legacy_rebase_false_block_recovery(
            plan_block=False, published_checkpoint=True, stale_cache=True,
        )

    def _legacy_rebase_false_block_recovery(self, *, plan_block, rejected=None,
                                          existing_pr=False, published_checkpoint=False,
                                          stale_cache=False, manual=None):
        self.journal_start.stop()
        fixture = test_blocked_checkpoint.BlockedCheckpointGitAdapterTest(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        repo = str(fixture.repo.resolve())
        branch = 'loop-manager/G-034'
        git = fixture.run_git
        app = fixture.worktree / 'code.py'
        lines = ['value=initial\n'] + [f'line-{n}\n' for n in range(30)]
        app.write_text(''.join(lines))
        git(fixture.worktree, 'add', 'code.py')
        git(fixture.worktree, 'commit', '-m', 'Worker value')
        for index in (20, 12):
            lines[index] = f'later-{index}\n'
            app.write_text(''.join(lines))
            git(fixture.worktree, 'add', 'code.py')
            git(fixture.worktree, 'commit', '-m', f'Later change {index}')
        original = git(fixture.worktree, 'rev-parse', 'HEAD')
        expected_remote = original if (existing_pr or published_checkpoint
                                       or rejected == 'remote-changed') else None
        if expected_remote:
            git(fixture.worktree, 'push', 'origin', f'HEAD:refs/heads/{branch}')
        (fixture.repo / 'code.py').write_text('value=base\n')
        git(fixture.repo, 'add', 'code.py')
        git(fixture.repo, 'commit', '-m', 'Conflicting base')
        target = git(fixture.repo, 'rev-parse', 'HEAD')
        connection = fixture.connection
        connection.execute(
            "UPDATE exec_plans SET worker_session_id = 'saved-worker', "
            "worker_result = 'accepted correction', verify_result = 'old pass', "
            "review_result = 'old pass'",
        )
        pull_request = None
        if existing_pr:
            pull_request = dict(url='https://example.test/pr/34', state='OPEN',
                                headRefName=branch, baseRefName='main',
                                headRefOid=original)
            connection.execute(
                'UPDATE exec_plans SET pr_url = ?, pr_head_oid = ?',
                (pull_request['url'], original),
            )
        lm.hydrate_execplan_project(connection, repo, connection.execute(
            "SELECT * FROM exec_plans",
        ).fetchone())
        connection.execute("UPDATE projects SET phase = 'WORKING'")
        checkpoint = ExecutionCheckpointRepository(connection)
        checkpoint.record(
            repo, 'G-034', expected_generation=0, operation='rebase',
            expected_head=original, target_base_oid=target,
            observed_remote_oid=expected_remote, snapshot={},
        )
        handler = lm._ExecutionLifecyclePorts(
            connection, runtime_state.RuntimeStateRepository(connection),
            repo=repo, repo_path=fixture.repo, state_dir=fixture.state_dir,
            base='main', base_sha=target, provider='codex',
            config_path=Path('/config'), newly_activated=False,
        )
        handler.plan = handler._cached_plan(handler.effects.state.active_execplan(repo))
        with mock.patch.object(lm, 'pr_for_branch', return_value=pull_request):
            context = handler._context()
        journal = handlers.manager_git_journal
        expected = dict(
            branch=branch, before_head=original, target_base_oid=target,
            remote_oid=expected_remote, agent_session_id='saved-worker',
            affected_paths=['code.py'],
        )
        identity = journal.begin_rebase(
            fixture.state_dir, owner='plan:G-034', repo=repo,
            target=str(fixture.worktree.resolve()),
            before=lm._owned_rebase_journal_state(fixture.worktree, branch),
            expected=expected,
        ) if manual is None else None
        environment = dict(os.environ, GIT_EDITOR='true')
        if identity is not None:
            environment['GIT_REFLOG_ACTION'] = f'loop-manager-rebase:{identity}'
        self.assertNotEqual(lm.git(
            fixture.worktree, 'rebase', target, check=False, env=environment,
        ).returncode, 0)
        if manual is None:
            with mock.patch.object(lm, 'pr_for_branch', return_value=pull_request):
                handler._finish_rebase_journal(context)
        resolved = ['value=resolved\n'] + [f'line-{n}\n' for n in range(30)]
        continuation = None
        if manual != 'waiting':
            app.write_text(''.join(resolved))
            git(fixture.worktree, 'add', 'code.py')
            staged = lm._owned_rebase_journal_state(fixture.worktree, branch)
            tree = git(fixture.worktree, 'write-tree')
            if manual is None:
                continuation = journal.begin_rebase(
                    fixture.state_dir, owner='plan:G-034', repo=repo,
                    target=str(fixture.worktree.resolve()), before=staged,
                    expected={**expected, 'action': 'continue', 'staged_tree_oid': tree,
                              'accepted_files': {'code.py': staged['dirty']['code.py'][1]}},
                )
                environment['GIT_REFLOG_ACTION'] = f'loop-manager-rebase:{continuation}'
            if manual != 'staged':
                self.assertEqual(lm.git(
                    fixture.worktree, 'rebase', '--continue', check=False, env=environment,
                ).returncode, 0)
                self.assertNotEqual(git(fixture.worktree, 'rev-parse', 'HEAD^{tree}'), tree)
        reason = ('manual G-088 conflict; inputs changed' if manual else
                  rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC)
        handlers.recovery_state.begin_external_run(connection)
        if manual is None:
            handlers.recovery_state.RecoveryRepository(connection).observe(
                handlers.RecoveryObservation(
                    handlers.RecoveryReason.STATUS_FAILED, repo, 'G-034', 'WORKING',
                    fixture.base_oid, fixture.base_oid, 'old-failure',
                    f'{fixture.base_oid}|saved-worker|', reason,
                ),
            )
        if plan_block:
            fixture.block('managed_worktree_conflict', reason)
        else:
            with connection:
                connection.execute(
                    "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, "
                    "evidence, previous_phase) VALUES (?, 'project', "
                    "'repository_state_unsafe', 'G-034', ?, 'WORKING')", (repo, reason),
                )
                connection.execute("UPDATE projects SET phase = 'PROJECT_BLOCKED'")
            if manual:
                handlers.recovery_state.RecoveryRepository(connection).observe(
                    handlers.RecoveryObservation(handlers.RecoveryReason.STATE_UNSAFE, repo, 'G-034',
                        'WORKING', fixture.base_oid, fixture.base_oid, 'saved-rebase', 'old-progress', reason),
                    final_block_reason=scoped_blocking.BlockReasonCode.REPOSITORY_STATE_UNSAFE)
        if manual:
            plan_path = connection.execute('SELECT path FROM exec_plans').fetchone()[0]
            document = fixture.worktree / plan_path
            document.write_text(document.read_text() + '\nUser input changed during blocked rebase.\n')
        published_before = None
        if published_checkpoint:
            # Legacy published checkpoints may adopt an existing clean commit.
            # Its durable identity binds that actual commit/tree, with no mocked owner proof.
            checkpoint_repository = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            request = checkpoint_repository.request(
                repo=repo, plan_id='G-034', reason_code='managed_worktree_conflict',
                evidence=reason, branch=branch, worktree=str(fixture.worktree),
                expected_remote_oid=original, rebase_recovery=True,
                release_fingerprint='fingerprint',
            ) if not plan_block else checkpoint_repository.get(repo, 'G-034')
            published_tree = git(fixture.repo, 'rev-parse', f'{original}^{{tree}}')
            identity = blocked_checkpoint.checkpoint_identity(
                plan_id='G-034', reason_code=request.reason_code, evidence=request.evidence,
                parent_oid=original, tree_oid=published_tree, included_paths=(),
            )
            connection.execute(
                "UPDATE blocked_checkpoints SET phase = 'published', result = 'published', "
                "published_oid = ?, local_oid = ?, remote_oid = ?, accepted_remote_oid = ?, "
                "commit_identity = ?, tree_oid = ? WHERE repo = ? AND plan_id = 'G-034'",
                (original, original, original, original, identity, published_tree, repo),
            )
            connection.execute('UPDATE exec_plans SET publication_remote_oid = ?', (original,))
            connection.execute('UPDATE projects SET publication_remote_oid = ?', (original,))
            published_before = dict(connection.execute('SELECT * FROM blocked_checkpoints').fetchone())
        subject = 'plan:G-034' if plan_block else 'project'
        connection.execute(
            "INSERT INTO notified_block_incidents "
            "(repo, subject, block_key, event_key, payload) "
            "VALUES (?, ?, 'legacy-failure', 'old-block-event', ?)",
            (repo, subject, json.dumps({'cause': reason})),
        )
        database = fixture.state_dir / 'restart.db'
        connection.commit()
        with sqlite3.connect(database) as persisted:
            connection.backup(persisted)
        connection.close()
        fixture.connection = sqlite3.connect(database)
        fixture.connection.row_factory = sqlite3.Row
        connection = fixture.connection
        def resume():
            if plan_block:
                return lm.resume_plan_blocks(
                    connection, fixture.repo, fixture.state_dir, repo, 'main',
                )
            return lm.resume_project_block_if_safe(
                connection, fixture.repo, fixture.state_dir, repo,
            )

        with mock.patch.object(lm, 'pr_for_branch', return_value=pull_request):
            # Planning still owns the slot; historical sessions are not gates.
            connection.execute("UPDATE projects SET planning_error = 'independent error'")
            self.assertFalse(resume())
            self.assertEqual(connection.execute(
                'SELECT planning_error FROM projects',
            ).fetchone()[0], 'independent error')
            connection.execute('UPDATE projects SET planning_error = NULL')
            if stale_cache:
                git(fixture.repo, 'update-ref', f'refs/remotes/origin/{branch}', target)
                self.assertNotEqual(lm.remote_branch_oid(fixture.repo, branch), expected_remote)
                self.assertEqual(journal.live_remote_branch_oid(lm.git, fixture.repo, branch),
                                 expected_remote)
            if rejected:
                completed = git(fixture.worktree, 'rev-parse', 'HEAD')
                rejected_file = (fixture.worktree / 'docs/ARCHITECTURE.md'
                                 if rejected in {'unstaged', 'late-file'} else app)
                if rejected in {'unstaged', 'staged'}:
                    rejected_file.write_text(rejected_file.read_text() + 'unapproved change\n'
                                             if rejected_file.exists() else 'unapproved change\n')
                    if rejected == 'staged':
                        git(fixture.worktree, 'add', 'code.py')
                elif rejected == 'index-flags':
                    git(fixture.worktree, 'update-index', '--skip-worktree', 'code.py')
                elif rejected == 'remote-created':
                    git(fixture.worktree, 'push', 'origin', f'{original}:refs/heads/{branch}')
                    git(fixture.repo, 'update-ref', '-d', f'refs/remotes/origin/{branch}')
                elif rejected == 'remote-changed':
                    git(fixture.repo, 'push', 'origin', 'main')
                    git(fixture.remote, 'update-ref', f'refs/heads/{branch}', target)
                self.assertEqual(lm.remote_branch_oid(fixture.repo, branch), expected_remote)
                # Catalog compatibility adoption is independent of block recovery.
                lm.active_execplan(connection, repo)
                before = dict(connection.execute('SELECT * FROM exec_plans').fetchone())
                saved_recovery = dict(handlers.recovery_state.RecoveryRepository(
                    connection,
                ).row(repo, 'G-034'))
                current_service = lm._current_rebase_service
                def service_with_race(*args, **kwargs):
                    service = current_service(*args, **kwargs)
                    require_owned = service.require_owned
                    calls = []
                    def observe(**options):
                        calls.append(None)
                        if len(calls) == 2:
                            if rejected == 'late-file':
                                rejected_file.write_text('late unapproved change\n')
                            elif rejected == 'late-remote':
                                git(fixture.worktree, 'push', 'origin', f'{original}:refs/heads/{branch}')
                                git(fixture.repo, 'update-ref', '-d', f'refs/remotes/origin/{branch}')
                        return require_owned(**options)
                    return SimpleNamespace(require_owned=observe)
                with mock.patch.object(lm, '_current_rebase_service', side_effect=service_with_race):
                    for _ in range(2):
                        self.assertFalse(resume())
                self.assertEqual(dict(connection.execute('SELECT * FROM exec_plans').fetchone()), before)
                self.assertEqual(dict(handlers.recovery_state.RecoveryRepository(
                    connection,
                ).row(repo, 'G-034')), saved_recovery)
                self.assertIsNone(connection.execute(
                    'SELECT released_at FROM notified_block_incidents',
                ).fetchone()[0])
                slot = journal._slot(fixture.state_dir, repo, 'managed_rebase',
                                     str(fixture.worktree.resolve()))
                self.assertTrue((slot / 'active.json').exists())
                self.assertFalse((slot / f'{continuation}.result.json').exists())
                self.assertEqual(git(fixture.worktree, 'rev-parse', 'HEAD'), completed)
                if rejected in {'unstaged', 'staged', 'late-file'}:
                    self.assertIn('unapproved change', rejected_file.read_text())
                if published_checkpoint:
                    self.assertEqual(dict(connection.execute(
                        'SELECT * FROM blocked_checkpoints',
                    ).fetchone()), published_before)
                    # Restoring the unexpected change must leave the same original
                    # completion intent usable; a poisoned result would prevent this.
                    if rejected == 'unstaged':
                        rejected_file.unlink()
                    else:
                        git(fixture.worktree, 'restore', '--source=HEAD', '--staged', '--worktree',
                            '--', 'code.py')
                else:
                    return
            self.assertTrue(resume())
            if manual in {'waiting', 'staged'}:
                self.assertTrue(lm._rebase_in_progress_for_branch(fixture.worktree, branch))
                if manual == 'waiting':
                    app.write_text(''.join(resolved))
                    git(fixture.worktree, 'add', 'code.py')
                # The user's changed plan is also part of this manual result;
                # Git continue requires all tracked edits to be staged.
                git(fixture.worktree, 'add', '--', plan_path)
                # Leave continuation and candidate adoption to Manager recover().
                self.assertEqual(ExecutionCheckpointRepository(connection).get(repo, 'G-034').operation,
                                 'rebase')
            self.assertFalse(resume())
        row = connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['status'], 'IMPLEMENTING')
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        if manual not in {'waiting', 'staged'}:
            self.assertIsNone(row['worker_result'])
            self.assertIsNone(row['verify_result'])
            self.assertIsNone(row['review_result'])
        self.assertIsNone(scoped_blocking.plan_block(connection, repo, 'G-034'))
        self.assertIsNone(scoped_blocking.project_block(connection, repo))
        incidents = connection.execute('SELECT * FROM notified_block_incidents').fetchall()
        self.assertEqual(len(incidents), 1)
        self.assertIsNotNone(incidents[0]['released_at'])
        self.assertEqual(json.loads(incidents[0]['payload'])['cause'], reason)
        failure = connection.execute('SELECT * FROM persistent_execution_failures').fetchone()
        if manual in {'waiting', 'staged'}:
            self.assertIsNone(failure)
        else:
            self.assertEqual(failure['active'], 0)
            self.assertEqual(failure['detail'], reason)
            self.assertIsNotNone(failure['resolved_at'])
        self.assertEqual(ExecutionCheckpointRepository(connection).get(repo, 'G-034').operation,
                         'rebase' if manual in {'waiting', 'staged'} else None)
        if published_checkpoint:
            self.assertEqual(dict(connection.execute(
                'SELECT * FROM blocked_checkpoints',
            ).fetchone()), published_before)
            saved = ExecutionCheckpointRepository(connection).get(repo, 'G-034')
            expected_published = (saved.expected_head if saved.snapshot.get('rebase_publication_result') else original)
            self.assertEqual(row['publication_remote_oid'], expected_published)
            proof = saved.snapshot['current_rebase_acceptance']
            self.assertEqual(proof['remote']['oid'], original)
            audit_slot = journal._slot(fixture.state_dir, repo, 'managed_rebase', str(fixture.worktree.resolve()))
            self.assertEqual(journal._read(audit_slot / f'{continuation}.intent.json')['expected']['before_head'], original)
            self.assertEqual(proof['head'], git(fixture.worktree, 'rev-parse', 'HEAD'))
        recovery = handlers.recovery_state.RecoveryRepository(connection).row(repo, 'G-034')
        if manual in {'waiting', 'staged'}:
            self.assertIsNone(recovery)
        else:
            self.assertEqual(recovery['pending_action'], 'reconcile')
        if manual not in {'waiting', 'staged'}:
            self.assertEqual(app.read_text().splitlines()[20], 'later-20')
            self.assertEqual(app.read_text().splitlines()[12], 'later-12')
        fixture.verify_probe.assert_not_called()
        fixture.reviewer_probe.assert_not_called()
        git(fixture.repo, 'push', 'origin', 'main')
        self._continue_recovered_rebase(fixture, target, pull_request)

    def _continue_recovered_rebase(self, fixture, target, pull_request):
        """Run the phase policies; external Agent and verify services stay fake."""
        repo = str(fixture.repo.resolve())
        pr_query = mock.patch.object(lm, 'pr_for_branch', return_value=pull_request)
        pr_query.start()
        self.addCleanup(pr_query.stop)
        state = ExecutionStatePort(
            fixture.connection, runtime_state.RuntimeStateRepository(fixture.connection), repo,
        )
        effects = replace(
            self.effects, state=state,
            checkpoint=ExecutionCheckpointRepository(fixture.connection),
            publication=recovered_rebase_publication(self.effects.publication, state),
            workspace=recovered_rebase_workspace(self.effects.workspace,
                connection=fixture.connection, repo_path=fixture.repo,
                state_dir=fixture.state_dir, repo=repo, plan_id='G-034',
                worktree=fixture.worktree),
        )
        handler = handlers.ExecutionHandlers(
            effects, repo=repo, repo_path=fixture.repo, state_dir=fixture.state_dir,
            base='main', base_sha=target, provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        plan = handler._cached_plan(state.active_execplan(repo))
        handler.plan = plan
        handler.catalog_plan = state.active_execplan(repo)
        effects.workspace.remote_branch_oid.side_effect = lm.remote_branch_oid
        effects.publication.pr_for_branch.return_value = pull_request
        effects.agent.validate_worker_workspace_scope.return_value = plan
        effects.validation.validate_active_execplan_metadata.return_value = plan
        effects.validation.parse_worker_result.side_effect = lm.parse_worker_result
        effects.agent.capture_git_snapshot.side_effect = lm.capture_git_snapshot
        effects.revision.reconcile_catalog.side_effect = lambda *_: SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha=target,
        )
        worker_result = (
            READY_WORKER_RESULT + '\nCOMMIT_TITLE: Preserve resolved replay\n'
            'PR_SUMMARY: Verify recovered rebase work.\n'
            'TARGETED_TESTS:\nrebase correction: PASS\n'
        )
        roles = []
        lifecycle = []
        def agent(*args, **kwargs):
            roles.append(args[4])
            if args[4] == 'worker':
                self.assertIn('현재 HEAD의 base 대비 최종 diff 전체', args[5])
                self.assertIn('targeted unit test', args[5])
                lifecycle.extend(('worker_confirmation', 'targeted_tests'))
                kwargs['on_session_started']('saved-worker')
                return lm.AgentResult(0, worker_result, session_id='saved-worker')
            lifecycle.append('review')
            return lm.AgentResult(0, 'VERDICT: PASS', session_id='review-session')
        effects.agent.run_agent.side_effect = agent
        effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile('model', 'high')
        coordinator = ExecutionPhaseCoordinator(handler)
        # Saved profiles are durable input; resuming should keep the Worker session.
        binding = SessionBinding('codex', 'model', 'high').serialize()
        state.update_project(repo, worker_session_binding=binding)
        pending_before = effects.checkpoint.get(repo, 'G-034').operation == 'rebase'
        continued = handler.recover(handler._context())
        self.assertNotIsInstance(continued, ExecutionOutcome)
        self.assertFalse(lm._rebase_in_progress_for_branch(fixture.worktree, continued.branch))
        self.assertIsNone(effects.checkpoint.get(repo, 'G-034').operation)
        for field in ('worker_result', 'verify_result', 'review_result',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(state.row()[field], field)
        self.assertEqual((fixture.worktree / 'code.py').read_text().splitlines()[20], 'later-20')
        self.assertEqual((fixture.worktree / 'code.py').read_text().splitlines()[12], 'later-12')
        effects.agent.run_agent.assert_not_called()
        effects.validation.verify.assert_not_called()
        handler.recover(handler._context())
        continues = [call for call in effects.workspace.git.call_args_list
                     if 'rebase' in call.args[1:] and '--continue' in call.args[1:]]
        self.assertEqual(len(continues), int(pending_before))
        context = handler.guard(handler._context())
        self.assertNotIsInstance(context, ExecutionOutcome)
        # The recovered old Worker publication must be checked before verify.
        if context.phase is ExecutionPhase.VERIFYING:
            invalid = handler.verify(context, handler)
            self.assertEqual(invalid.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(state.row()['phase'], 'WORKING')
        worker = coordinator.dispatch(handler._context())
        self.assertEqual(worker.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(state.row()['worker_session_id'], 'saved-worker')
        effects.validation.verify.reset_mock()
        effects.validation.verify.side_effect = lambda _: (
            lifecycle.append('verify') or True, 'fresh recovered candidate: OK',
        )
        verified = coordinator.dispatch(handler._context())
        self.assertEqual(verified.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(state.row()['phase'], 'REVIEWING')
        effects.validation.verify.assert_called_once_with(fixture.worktree)
        reviewed = coordinator.dispatch(handler._context())
        self.assertEqual(reviewed.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(roles, ['worker', 'reviewer'])
        self.assertEqual(state.row()['phase'], 'PUBLISHING')
        self.assertIsNotNone(state.row()['verify_token'])
        self.assertIsNotNone(state.row()['review_token'])
        self.assertEqual(state.row()['pr_url'], (pull_request or {}).get('url'))
        effects.publication.push_managed_publication_branch.assert_not_called()
        effects.publication.command.assert_not_called()
        if pull_request:
            state.update_project(repo, pr_conflict_recovery=1,
                pr_conflict_head_oid=pull_request['headRefOid'], pr_conflict_base_oid=target)
        # The real publication policy must reject a stale successful token.
        fresh_review = state.row()['review_token']
        state.update_project(repo, review_token='old candidate review')
        stale = coordinator.dispatch(handler._context())
        self.assertEqual(stale.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(state.row()['phase'], 'REVIEWING')
        effects.publication.push_managed_publication_branch.assert_not_called()
        effects.publication.command.assert_not_called()
        # Re-run Review through Manager, rather than restoring its old result.
        reviewed = coordinator.dispatch(handler._context())
        self.assertEqual(reviewed.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(state.row()['review_token'], fresh_review)
        published = coordinator.dispatch(handler._context())
        self.assertEqual(published.kind, ExecutionOutcomeKind.SUCCESS)
        lifecycle.append('publication')
        self.assertEqual(lifecycle, ['worker_confirmation', 'targeted_tests',
                                     'verify', 'review', 'review', 'publication'])
        self.assertEqual(state.repository.get(repo, 'G-034').status, 'PR_OPEN')
        if pull_request:
            effects.publication.push_owned_pr_conflict_recovery.assert_called_once()
            effects.publication.command.assert_not_called()
        else:
            saved = effects.checkpoint.get(repo, 'G-034')
            if saved.snapshot.get('rebase_publication_result'):
                # An owned checkpoint was already pushed during recovery.
                # Final publication creates its PR without a second push.
                effects.publication.push_managed_publication_branch.assert_not_called()
                self.assertEqual(lm.remote_branch_oid(fixture.repo, 'loop-manager/G-034'),
                                 lm.git(fixture.worktree, 'rev-parse', 'HEAD').stdout.strip())
            else:
                effects.publication.push_managed_publication_branch.assert_called_once()
            effects.publication.command.assert_called_once()

    def test_real_git_published_review_release_restart_and_correction(self):
        # Exercise the ownership boundary with real commits, refs, registration,
        # snapshots and SQLite persistence; only external PR/agent services are fake.
        fixture = test_blocked_checkpoint.BlockedCheckpointGitAdapterTest(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        repo = str(fixture.repo.resolve())
        branch = 'loop-manager/G-034'
        connection = fixture.connection
        connection.execute(
            "UPDATE exec_plans SET review_result = 'code.py: repair\nVERDICT: FAIL', "
            "worker_session_id = 'saved-worker'",
        )
        (fixture.worktree / 'code.py').write_text('checkpoint work\n')
        fixture.block('review_retry_exhausted', 'code.py: repair')
        with mock.patch.object(lm, 'pr_for_branch', return_value=None):
            self.assertEqual(lm.dispatch_blocked_checkpoints(
                connection, fixture.repo, fixture.state_dir, repo,
            ), 1)
        published = fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD')
        original = dict(connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE plan_id = 'G-034'",
        ).fetchone())
        self.assertEqual(original['published_oid'], published)
        self.assertEqual(original['accepted_remote_oid'], published)
        self.assertEqual(original['tree_oid'], fixture.run_git(
            fixture.worktree, 'rev-parse', f'{published}^{{tree}}',
        ))
        self.assertIn(f"Checkpoint-Identity: {original['commit_identity']}",
                      fixture.run_git(fixture.worktree, 'show', '-s', '--format=%B', published))

        def plan_row():
            return fixture.connection.execute(
                "SELECT * FROM exec_plans WHERE plan_id = 'G-034'",
            ).fetchone()

        with mock.patch.object(lm, 'pr_for_branch', return_value=None):
            # A real published commit must not excuse corrupted durable evidence.
            for column in ('request_identity', 'commit_identity', 'tree_oid'):
                with self.subTest(column=column):
                    connection.execute(
                        f"UPDATE blocked_checkpoints SET {column} = 'invalid'",
                    )
                    self.assertFalse(lm._resume_legacy_review_count_block(
                        connection, fixture.repo, fixture.state_dir, repo, plan_row(),
                    ))
                    connection.execute(
                        f"UPDATE blocked_checkpoints SET {column} = ?", (original[column],),
                    )
            self.assertTrue(lm._resume_legacy_review_count_block(
                connection, fixture.repo, fixture.state_dir, repo, plan_row(),
            ))
        self.assertEqual(plan_row()['status'], 'IMPLEMENTING')
        self.assertEqual(plan_row()['worker_session_id'], 'saved-worker')
        self.assertIn('VERDICT: FAIL', plan_row()['review_result'])
        self.assertIsNone(scoped_blocking.plan_block(connection, repo, 'G-034'))

        # Close the original connection and reconstruct both repositories and
        # handler, so ownership cannot depend on in-process state from release.
        database = fixture.state_dir / 'restart.db'
        connection.commit()
        with sqlite3.connect(database) as persisted:
            connection.backup(persisted)
        connection.close()
        fixture.connection = sqlite3.connect(database)
        fixture.connection.row_factory = sqlite3.Row
        connection = fixture.connection
        lm.hydrate_execplan_project(connection, repo, plan_row())
        checkpoint = ExecutionCheckpointRepository(connection)
        effects = replace(
            self.effects,
            state=ExecutionStatePort(connection, runtime_state.RuntimeStateRepository(connection), repo),
            checkpoint=checkpoint,
            workspace=replace(
                self.effects.workspace, git=lm.git,
                validate_execplan_workspace_identity=lm.validate_execplan_workspace_identity,
                validate_managed_worktree=lm.validate_managed_worktree,
            ),
        )
        handler = handlers.ExecutionHandlers(
            effects, repo=repo, repo_path=fixture.repo, state_dir=fixture.state_dir,
            base='main', base_sha=fixture.base_oid, provider='codex',
            config_path=Path('/config'), newly_activated=False,
        )
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-034'), branch=branch,
            worktree=fixture.worktree, active_sha=fixture.base_oid,
        )

        def assert_owned(expected):
            saved = checkpoint.get(repo, 'G-034')
            self.assertNotIn('blocked_resume', saved.snapshot)
            self.assertEqual(saved.snapshot['published_review_resume']['owner_oid'], published)
            self.assertEqual(handler._owned_checkpoint_remote(
                context, plan_row(), published, saved,
            ), expected)

        assert_owned(True)
        before = lm.capture_git_snapshot(fixture.worktree)
        (fixture.worktree / 'code.py').write_text('review correction\n')
        lm.validate_agent_scope(
            'worker', fixture.worktree, before, lm.capture_git_snapshot(fixture.worktree),
            selected_plan_path=plan_row()['path'],
        )
        fixture.run_git(fixture.worktree, 'add', '--', 'code.py')
        handler._commit_owned(context, 'Repair review issue')
        corrected = fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(corrected, published)
        self.assertEqual(fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD^'), published)
        self.assertEqual(checkpoint.get(repo, 'G-034').snapshot['commit_tree_oid'],
                         fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD^{tree}'))
        assert_owned(True)
        self.assertEqual(lm.git(fixture.repo, 'ls-remote', '--heads', 'origin',
                               f'refs/heads/{branch}').stdout.split()[0], published)
        self.assertEqual(dict(connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE plan_id = 'G-034'",
        ).fetchone()), original)
        effects.validation.verify.assert_not_called()
        effects.agent.run_agent.assert_not_called()
        effects.publication.push_managed_publication_branch.assert_not_called()
        effects.publication.command.assert_not_called()
        fixture.verify_probe.assert_not_called()
        fixture.reviewer_probe.assert_not_called()
        # A successor pushed by somebody else invalidates the same durable owner.
        fixture.user_commit(contents='external correction\n')
        assert_owned(False)

    def test_published_review_commit_correction_keeps_owner_on_restart(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', "
            "'review_retry_exhausted', 'review', 'loop-manager/G-047', "
            "'/state/G-047', 'old-head')"
        )
        self.state.update_project('repo', phase='VERIFYING',
                                  publication_remote_oid='old-head')
        self.dirty = False
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='commit',
            expected_head='old-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={'published_review_resume': {
                'request_identity': 'request', 'owner_oid': 'old-head',
                'head': 'old-head',
            }, 'before_head': 'old-head',
                'commit_message': 'Worker correction',
                'commit_tree_oid': 'c' * 40},
        )

        def observed_git(path, *args, **kwargs):
            if args in {('rev-parse', 'HEAD'),
                        ('rev-parse', 'refs/heads/loop-manager/G-047')}:
                return SimpleNamespace(stdout='corrected-head\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout='old-head\trefs/heads/loop-manager/G-047\n',
                    returncode=0,
                )
            if args == ('show', '-s', '--format=%P', 'corrected-head'):
                return SimpleNamespace(stdout='old-head\n', returncode=0)
            if args == ('show', '-s', '--format=%B', 'corrected-head'):
                return SimpleNamespace(stdout='Worker correction\n', returncode=0)
            if args == ('rev-parse', 'corrected-head^{tree}'):
                return SimpleNamespace(stdout='c' * 40 + '\n', returncode=0)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'old-head'
        self.effects.publication.pr_for_branch.return_value = None
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='old-head'):
            first = self.handler.recover(self.context())
            saved = checkpoint.get('repo', 'G-047')
            self.assertEqual(first.phase, ExecutionPhase.VERIFYING)
            self.assertIsNone(saved.operation)
            self.assertEqual(saved.expected_head, 'corrected-head')
            self.assertEqual(saved.snapshot['published_review_resume'], {
                'request_identity': 'request', 'owner_oid': 'old-head',
                'head': 'old-head',
            })
            self.assertEqual(self.handler.recover(self.context()).phase,
                             ExecutionPhase.VERIFYING)
        self.effects.publication.command.assert_not_called()

    def test_published_review_push_restarts_before_completion_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', "
            "'review_retry_exhausted', 'review', 'loop-manager/G-047', "
            "'/state/G-047', 'old-head')"
        )
        self.state.update_project('repo', publication_remote_oid='corrected-head')
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='push',
            expected_head='corrected-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={
                'published_review_resume': {
                    'request_identity': 'request', 'owner_oid': 'old-head',
                    'head': 'old-head',
                },
                'remote_before': 'old-head',
            },
        )

        def observed_git(path, *args, **kwargs):
            if args in {('rev-parse', 'HEAD'),
                        ('rev-parse', 'refs/heads/loop-manager/G-047')}:
                return SimpleNamespace(stdout='corrected-head\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout='corrected-head\trefs/heads/loop-manager/G-047\n',
                    returncode=0,
                )
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'corrected-head'
        self.effects.publication.pr_for_branch.return_value = None
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='old-head'):
            pending = checkpoint.get('repo', 'G-047')
            self.assertNotIn('pushed_checkpoint_head', pending.snapshot)
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), self.state.row(), 'corrected-head', pending,
            ))
            self.handler.recover(self.context())
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'corrected-head')
        self.assertEqual(self.state.row()['publication_remote_oid'], 'corrected-head')

    def test_published_review_resume_push_completion_survives_restart(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', "
            "'review_retry_exhausted', 'review', 'loop-manager/G-047', "
            "'/state/G-047', 'owned-head')"
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='push',
            expected_head='corrected-head', target_base_oid='base',
            observed_remote_oid='owned-head',
            snapshot={
                'published_review_resume': {
                    'request_identity': 'request', 'owner_oid': 'owned-head',
                    'head': 'owned-head',
                },
                'remote_before': 'owned-head',
            },
        )
        self.handler._complete_publication_intent(self.context())
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'corrected-head')
        self.assertEqual(saved.observed_remote_oid, 'corrected-head')

        def git(_path, *args, **_kwargs):
            if args[0] == 'rev-parse':
                return SimpleNamespace(stdout='corrected-head\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout='corrected-head\trefs/heads/loop-manager/G-047\n',
                    returncode=0,
                )
            raise AssertionError(args)

        self.effects.workspace.git.side_effect = git
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='owned-head'):
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(),
                {'pr_url': None, 'publication_remote_oid': 'corrected-head'},
                'corrected-head', checkpoint.get('repo', 'G-047'),
            ))

    def test_published_review_resume_rechecks_identity_and_live_owner(self):
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', "
            "'review_retry_exhausted', 'review', 'loop-manager/G-047', "
            "'/state/G-047', 'owned-head')"
        )
        context = self.context()
        marker = {'request_identity': 'request', 'owner_oid': 'owned-head',
                  'head': 'owned-head'}
        current = SimpleNamespace(
            operation=None, expected_head='owned-head',
            target_base_oid='new-base', observed_remote_oid='prior-head',
            snapshot={'published_review_resume': marker},
            completed_rebase=lambda: None,
        )
        row = {'pr_url': None, 'publication_remote_oid': 'owned-head'}
        live = {'oid': 'owned-head'}

        def git(_path, *args, **_kwargs):
            if args[0] == 'rev-parse':
                return SimpleNamespace(stdout='owned-head\n', returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout=f"{live['oid']}\trefs/heads/loop-manager/G-047\n",
                    returncode=0,
                )
            raise AssertionError(args)

        self.effects.workspace.git.side_effect = git
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='owned-head'):
            self.assertTrue(self.handler._owned_checkpoint_remote(
                context, row, live['oid'], current,
            ))
            live['oid'] = 'external'
            self.assertFalse(self.handler._owned_checkpoint_remote(
                context, row, live['oid'], current,
            ))
            live['oid'] = 'owned-head'
            marker['request_identity'] = 'other-request'
            self.assertFalse(self.handler._owned_checkpoint_remote(
                context, row, live['oid'], current,
            ))
            marker['request_identity'] = 'request'
            row['publication_remote_oid'] = 'other-head'
            self.assertFalse(self.handler._owned_checkpoint_remote(
                context, row, live['oid'], current,
            ))

    def test_rebase_completion_session_handles_optional_snapshot(self):
        context = SimpleNamespace(worker_session_id='current-session')
        cases = (
            ({}, 'current-session'),
            ({'snapshot': None}, 'current-session'),
            ({'snapshot': {}}, 'current-session'),
            ({'snapshot': {'rebase_start_session_id': 'start-session'}},
             'current-session'),
            ({'snapshot': {'stash_oid': 'stash', 'pending': 'stage',
                           'rebase_start_session_id': 'start-session'}},
             'current-session'),
            ({'snapshot': {'stash_oid': 'stash', 'pending': 'continue',
                           'rebase_start_session_id': 'start-session'}},
             'start-session'),
            ({'snapshot': {'stash_oid': 'stash', 'pending': 'continue',
                           'rebase_start_session_id': None}}, None),
        )
        for fields, expected in cases:
            with self.subTest(fields=fields):
                self.assertEqual(self.handler._rebase_completion_session(
                    context, SimpleNamespace(**fields),
                ), expected)

    def test_rebase_journal_match_requires_run_identity_only_for_v2(self):
        context = SimpleNamespace(
            plan=SimpleNamespace(plan_id='G-047'),
            branch='loop-manager/G-047', worktree=Path('/state/G-047'),
            worker_session_id='worker-session',
        )
        observed = {'head': 'after', 'branch': context.branch}
        transition = {
            'intent': {
                'kind': 'managed_rebase',
                'target': str(context.worktree.resolve()),
                'expected': {
                    'branch': context.branch, 'run_id': 'other-run',
                    'checkpoint_generation': 2, 'before_head': 'before',
                    'target_base_oid': 'base', 'remote_oid': 'remote',
                },
            },
            'result': {'observed': observed},
        }
        current = SimpleNamespace(
            expected_head='before', target_base_oid='base', generation=2,
            snapshot={'rebase_evidence_version': 2, 'rebase_run_id': 'run'},
        )
        with mock.patch.object(
            handlers.manager_git_journal, 'verified_managed_transitions',
            return_value=[transition],
        ):
            self.assertFalse(self.handler._matching_rebase_journal_result(
                context, current, 'remote', observed,
            ))
            transition['intent']['expected']['run_id'] = 'run'
            self.assertTrue(self.handler._matching_rebase_journal_result(
                context, current, 'remote', observed,
            ))
            transition['intent']['expected']['checkpoint_generation'] = 3
            self.assertFalse(self.handler._matching_rebase_journal_result(
                context, current, 'remote', observed,
            ))

    def test_sealed_rebase_completion_updates_runtime_without_repeating_git(self):
        case = current_rebase_case(self)
        handlers.recovery_state.begin_external_run(case.fixture.connection)
        base = case.state.row()['active_sha']
        handlers.recovery_state.RecoveryRepository(case.fixture.connection).observe(
            handlers.RecoveryObservation(handlers.RecoveryReason.STATUS_FAILED, 'repo', 'G-047',
                'WORKING', 'plan-revision', base, 'old-failure', f'{base}|saved-worker|',
                rebase_block_reconciliation.LEGACY_COMPLETION_DIAGNOSTIC))
        with mock.patch.object(handlers.manager_git_journal, 'verified_rebase_completion',
                               side_effect=AssertionError('history is not a current gate')):
            case.handler._complete_rebase_intent(case.fixture.context())
        saved = case.checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['current_rebase_acceptance']['head'], 'owned-head')
        self.assertEqual(case.state.row()['phase'], 'WORKING')
        self.assertIsNone(case.state.row()['worker_result'])
        self.assertEqual(case.state.row()['worker_session_id'], 'saved-worker')
        failure = case.fixture.connection.execute(
            "SELECT active, resolved_at FROM persistent_execution_failures WHERE repo = 'repo'").fetchone()
        self.assertEqual(failure['active'], 0)
        self.assertIsNotNone(failure['resolved_at'])
        case.handler._complete_rebase_intent(case.handler._context())
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), saved)

    def test_autostash_completion_requires_approved_stage_and_matching_blob(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project('repo', worker_session_id='corrector')
        context = self.context()
        stage = {'head': 'head', 'branch': context.branch,
                 'files': {'app.py': 'accepted'},
                 'modes': {'app.py': '100644'},
                 'index': {'app.py': ['M 100644 before 2']}}
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None,
            snapshot={'rebase_evidence_version': 2, 'stash_oid': 'stash',
                      'rebase_start_session_id': 'original',
                      'pending': 'continue', 'worker_result': 'result',
                      'stage_evidence': stage},
        )
        blob_oid = 'a' * 40
        common = {'head': 'head', 'branch': context.branch,
                  'ref': ['head', ''], 'registration': ['owned'],
                  'operations': {}, 'metadata': None}
        sealed = {**common, 'unmerged': True,
                  'dirty': {'app.py': [['unmerged'], 'before']},
                  'index': {'app.py': ['M 100644 before 2']}}
        live = {**common, 'unmerged': False,
                'dirty': {'app.py': [['staged'], 'accepted']},
                'index': {'app.py': [f'H 100644 {blob_oid} 0']}}
        self.handler._stage_evidence_preserved = (
            handlers.ExecutionHandlers._stage_evidence_preserved.__get__(self.handler)
        )
        self.handler._rebase_stage_evidence = mock.Mock(return_value={
            **stage, 'index': live['index'],
        })

        def evidence_git(_worktree, *args, **_kwargs):
            if args[:2] == ('ls-files', '--cached'):
                return SimpleNamespace(returncode=0, stdout='app.py\0')
            if args[:1] == ('hash-object',):
                return SimpleNamespace(returncode=0, stdout=blob_oid + '\n')
            raise AssertionError(args)

        self.effects.workspace.git.side_effect = evidence_git
        intent = {'expected': {'evidence_version': 2,
                               'agent_session_id': 'original'}}
        self.assertTrue(self.handler._completed_autostash_correction(
            context, intent, sealed, live,
        ))
        self.assertTrue(self.handler._completed_autostash_correction(
            context, intent,
            {**sealed, 'operations': {'MERGE_MSG': 'sealed-marker'}}, live,
        ))
        self.assertTrue(self.handler._completed_autostash_correction(
            context, intent,
            {**sealed, 'operations': {'REBASE_HEAD': 'sealed-marker'}}, live,
        ))
        self.assertFalse(self.handler._completed_autostash_correction(
            context, intent,
            {**sealed, 'operations': {'CHERRY_PICK_HEAD': 'foreign-marker'}}, live,
        ))
        rejection = []
        self.assertFalse(self.handler._completed_autostash_correction(
            context, intent, sealed,
            {**live, 'dirty': {**live['dirty'], 'extra.py': [['staged'], 'extra']}},
            rejection=rejection,
        ))
        self.assertEqual(rejection, ['unapproved dirty path'])
        self.assertFalse(self.handler._completed_autostash_correction(
            context, intent, sealed,
            {**live, 'file_evidence': {'extra.py': {'mode': '100755'}}},
        ))
        self.assertFalse(self.handler._completed_autostash_correction(
            context, {'expected': {**intent['expected'],
                                   'agent_session_id': 'foreign'}}, sealed, live,
        ))
        self.effects.workspace.git.side_effect = lambda *_args, **_kwargs: (
            SimpleNamespace(returncode=0, stdout='app.py\0')
            if _args[1:3] == ('ls-files', '--cached') else
            SimpleNamespace(returncode=0, stdout='b' * 40 + '\n')
        )
        self.assertFalse(self.handler._completed_autostash_correction(
            context, intent, sealed, live,
        ))

    def test_autostash_completion_reports_rejected_operation_marker(self):
        case = current_rebase_case(self)
        case.expected = replace(case.expected, autostash_oid='stash')
        case.observed = replace(case.observed, operations=(('CHERRY_PICK_HEAD', 'foreign'),))
        original = case.checkpoint.get('repo', 'G-047')
        with self.assertRaisesRegex(handlers.manager_git_journal.MutationEvidenceError,
                                   'unresolved_operation_or_index'):
            case.handler._complete_rebase_intent(case.fixture.context())
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
        self.assertEqual(case.state.row()['verify_result'], 'old verify')

    def test_review_budget_pauses_before_worker_and_preserves_projection(self):
        run_id = handlers.recovery_state.begin_external_run(self.connection)
        feedback = ValidationFeedbackRepository(self.connection)
        for index in range(4):
            review = f"ISSUE_ID: app.py::issue-{index}::blocking\nVERDICT: FAIL"
            feedback.observe('repo', 'G-047', 'base', 'review', False, review,
                             (Feedback('review', f'review:app.py::issue-{index}::blocking', review),))
            result_id = self.connection.execute(
                "SELECT MAX(id) FROM validation_results"
            ).fetchone()[0]
            if index < 3:
                self.assertEqual(
                    handlers.recovery_state.reserve_review_correction(
                        self.connection, 'repo', 'G-047', result_id,
                    ), (True, index + 1, run_id),
                )
        self.state.update_project(
            'repo', phase='WORKING', review_result=review,
            last_error='Review 실패:\n' + review,
            worker_session_id='worker-session',
        )
        before = self.state.check()
        outcome = self.handler.run(self.context())
        after = self.state.check()

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual((after.status, after.worker_session_id, after.review_result),
                         (before.status, before.worker_session_id, before.review_result))
        self.assertIn('used=3/3', outcome.detail)
        self.effects.agent.run_agent.assert_not_called()
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM blocked_checkpoints WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone())
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox "
            "WHERE repo = 'repo' AND event_type IN ('blocked', 'resumed')"
        ).fetchone()[0], 0)

    def test_review_pass_after_third_correction_advances_to_publication(self):
        run_id = handlers.recovery_state.begin_external_run(self.connection)
        for result_id in range(1, 4):
            self.assertEqual(
                handlers.recovery_state.reserve_review_correction(
                    self.connection, 'repo', 'G-047', result_id,
                ), (True, result_id, run_id),
            )
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        self.effects.agent.run_agent.side_effect = [lm.AgentResult(
            0, 'VERDICT: PASS', session_id='review-session',
        )]
        outcome = self.handler.review(self.review_context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-047'))

    def test_worker_result_correction_does_not_consume_review_budget(self):
        run_id = handlers.recovery_state.begin_external_run(self.connection)
        feedback = ValidationFeedbackRepository(self.connection)
        for result_id in range(1, 5):
            review = f'ISSUE_ID: app.py::issue-{result_id}::blocking\nVERDICT: FAIL'
            feedback.observe('repo', 'G-047', 'base', 'review', False, review)
            if result_id <= 3:
                self.assertEqual(
                    handlers.recovery_state.reserve_review_correction(
                        self.connection, 'repo', 'G-047', result_id,
                    ), (True, result_id, run_id),
                )
        self.state.update_project(
            'repo', phase='WORKING', review_result=review,
            last_error='Worker 결과 검증 실패: format',
        )
        handlers.recovery_state.RecoveryRepository(self.connection).observe(
            handlers.RecoveryObservation(
                handlers.RecoveryReason.WORKER_CORRECTION,
                'repo', 'G-047', 'IMPLEMENTING', 'base', 'base',
                'worker-format', 'worker-session', 'invalid Worker result',
            ),
        )
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'worker failed', session_id='worker-session',
        )
        outcome = self.handler.run(self.context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.OPERATIONAL_ERROR)
        self.effects.agent.run_agent.assert_called_once()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM review_correction_turns WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0], 3)

    def test_document_execution_guard_preserves_crlf_approved_diff(self):
        original, proposed = b"# Product\r\nOld.\r\n", b"# Product\r\nNew.\r\n"
        draft = SimpleNamespace(
            draft_id="a" * 32, state="approved", target_path="docs/PRODUCT.md",
            decision_revision="b" * 32, original_revision="c" * 40,
            original_hash=hashlib.sha256(original).hexdigest(),
            proposed_hash=hashlib.sha256(proposed).hexdigest(),
            complete_diff=handlers.document_drafts._complete_diff(
                "docs/PRODUCT.md", original, proposed,
            ),
        )
        self.handler.plan = {
            **self.plan,
            "sources": [handlers.document_publication.R027_SOURCE],
        }
        plan_text = handlers.document_publication.planning_instruction(draft)
        repository = mock.Mock()
        repository.assert_current = mock.Mock()
        with (
            mock.patch.object(handlers.document_drafts, "DocumentDraftRepository",
                              return_value=repository),
            mock.patch.object(Path, "read_bytes", return_value=plan_text.encode("utf-8")),
            mock.patch.object(handlers.document_publication, "validate_exact_worktree"),
        ):
            self.handler._document_guard(self.context(), draft)
        repository.assert_current.assert_called_once()

    def test_document_plan_applies_program_bytes_without_worker_agent(self):
        draft = SimpleNamespace(
            draft_id="a" * 32, target_path="docs/PRODUCT.md",
        )
        repository = mock.Mock()
        repository.assert_current = mock.Mock()
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(handlers.document_drafts, 'DocumentDraftRepository',
                              return_value=repository),
            mock.patch.object(handlers.document_publication, 'read_plan_text',
                              return_value='approved plan'),
            mock.patch.object(handlers.document_publication, 'validate_document_plan'),
            mock.patch.object(handlers.document_publication, 'apply_approved_bytes') as apply,
        ):
            outcome = self.handler.run(self.context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        apply.assert_called_once()
        self.effects.agent.run_agent.assert_not_called()
        self.assertIn('실행 가능한 관련 unit test가 없습니다',
                      self.state.row()['worker_result'])

    def test_document_review_requires_explicit_meaning_and_diff_verdicts(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        self.effects.agent.reviewer_prompt.return_value = 'review prompt'
        self.effects.agent.run_agent.side_effect = lambda *args, **kwargs: lm.AgentResult(
            0, 'VERDICT: PASS', session_id=None,
        )
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(self.handler, '_document_guard'),
            mock.patch.object(self.handler, '_document_block',
                              return_value=ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK)) as block,
            mock.patch.object(handlers.document_drafts, 'review_artifact', return_value='artifact'),
        ):
            outcome = self.handler.review(self.review_context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIn('명시적으로 확정', block.call_args.args[2])
        self.effects.publication.command.assert_not_called()

    def test_document_review_passes_with_explicit_meaning_and_diff_verdicts(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        self.effects.agent.reviewer_prompt.return_value = 'review prompt'
        self.effects.agent.run_agent.side_effect = lambda *args, **kwargs: lm.AgentResult(
            0, 'DOCUMENT_MEANING: PASS\nDOCUMENT_DIFF: PASS\nVERDICT: PASS',
            session_id=None,
        )
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(self.handler, '_document_guard'),
            mock.patch.object(handlers.document_drafts, 'review_artifact', return_value='artifact'),
        ):
            outcome = self.handler.review(self.review_context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')

    def test_document_publication_stops_before_pr_when_original_is_stale(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(self.handler, '_document_guard', side_effect=
                              handlers.document_drafts.DocumentDraftError('original changed')),
            mock.patch.object(self.handler, '_document_block',
                              return_value=ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK)) as block,
            mock.patch.object(self.handler, '_fetch_latest_base', return_value='base'),
        ):
            outcome = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIn('original changed', str(block.call_args.args[2]))
        self.effects.publication.command.assert_not_called()

    def test_document_uncertainty_persists_audit_and_blocks_plan(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        repository = mock.Mock()
        repository.record_publication_error = mock.Mock()
        with mock.patch.object(
            handlers.document_drafts, 'DocumentDraftRepository',
            return_value=repository,
        ):
            outcome = self.handler._document_block(
                self.context('REVIEWING'), draft, 'meaning uncertain',
            )
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.assertIn('meaning uncertain', repository.record_publication_error.call_args.args[1])

    def test_document_pr_resume_reuses_existing_pr_after_interruption(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.command.side_effect = RuntimeError('interrupted response')
        with (
            mock.patch.object(self.handler, '_document_draft', return_value=draft),
            mock.patch.object(self.handler, '_document_guard') as guard,
        ):
            paused = self.handler.publish(self.context('PUBLISHING'), self.handler)
            self.assertEqual(paused.kind, ExecutionOutcomeKind.RETRY)
            self.effects.publication.pr_for_branch.return_value = {
                'url': 'created', 'headRefOid': 'base',
            }
            self.effects.publication.reconcile_existing_pull_request.return_value = 0
            resumed = self.handler.publish(self.context(), self.handler)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertGreaterEqual(guard.call_count, 2)
        self.assertEqual(self.calls.count('push'), 1)
        self.assertEqual(self.effects.publication.command.call_count, 1)

    def test_document_plan_without_durable_approval_mapping_blocks_before_agent(self):
        self.effects.agent.is_loop_manager_repository.return_value = True
        with mock.patch.object(
            handlers.document_publication, 'read_plan_text',
            return_value='DOCUMENT_DRAFT_ID: ' + 'a' * 32,
        ):
            outcome = self.handler.guard(self.context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.effects.agent.run_agent.assert_not_called()

    def test_document_plan_keeps_approved_pr_free_of_requirement_archive(self):
        draft = SimpleNamespace(draft_id="a" * 32, target_path="docs/PRODUCT.md")
        self.effects.archive.requirement_archive_sources.side_effect = AssertionError(
            'document-only PR must not add an archive move',
        )
        with mock.patch.object(self.handler, '_document_draft', return_value=draft):
            outcome = self.handler.guard(self.context())
        self.assertNotIsInstance(outcome, ExecutionOutcome)
        self.assertEqual(self.handler.archive_sources, ())
        self.effects.archive.requirement_archive_sources.assert_not_called()

    def test_project_resume_is_recorded_when_execution_phase_starts(self):
        notifications.configure_session(self.connection, enabled=True)
        with self.connection:
            self.connection.execute(
                "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence) "
                "VALUES ('repo', 'project', 'repository_state_unsafe', 'G-047', 'unsafe')"
            )
            notifications.enqueue_current_project_events(self.connection, 'repo')
        notifications.dispatch_pending(
            self.connection, 'token', 'chat', adapter=mock.Mock(),
        )
        with self.connection:
            scoped_blocking.clear_project_block(self.connection, 'repo')
        self.handler.work_started(self.context())
        resumed = self.connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_type = 'resumed'"
        ).fetchall()
        self.assertEqual(len(resumed), 1)
        self.assertEqual(json.loads(resumed[0][0])['operation'],
                         'ExecPlan IMPLEMENTING')

    def setUp(self):
        self.connection = test_runtime_state.RuntimeStateRepositoryTest()._connection()
        self.addCleanup(self.connection.close)
        self.repository = runtime_state.RuntimeStateRepository(self.connection)
        self.repository.activate('repo', 'G-047', activation_sha='base',
                                 branch='loop-manager/G-047', worktree='/state/G-047')
        self.state = ExecutionStatePort(self.connection, self.repository, 'repo')
        # Existing saved-session fixtures represent sessions started by this
        # configured provider; explicit None still exercises legacy recovery.
        original_update = self.state.update_project
        def update_with_binding(repo, **values):
            if values.get('worker_session_id') and 'worker_session_binding' not in values:
                values['worker_session_binding'] = SessionBinding(
                    'codex', 'model', 'high',
                ).serialize()
            return original_update(repo, **values)
        self.state.update_project = update_with_binding
        self.plan = {'id': 'G-047', 'title': 'Runtime', 'sources': ['docs/PRODUCT.md']}
        self.calls = []
        self.remote = 'base'
        self.dirty = True

        def bundle(cls):
            return cls(**{f.name: mock.Mock(name=f.name, return_value=None)
                          for f in fields(cls)})

        self.effects = handlers.ExecutionEffects(
            state=self.state, contract=lm,
            workspace=bundle(handlers.WorkspaceEffects),
            agent=bundle(handlers.AgentEffects),
            validation=bundle(handlers.ValidationEffects),
            archive=bundle(handlers.ArchiveEffects),
            blocking=bundle(handlers.BlockingEffects),
            publication=bundle(handlers.PublicationEffects),
            events=bundle(handlers.EventsEffects),
            revision=bundle(handlers.RevisionEffects),
        )
        self.effects = replace(self.effects, workspace=replace(self.effects.workspace, current_rebase_service=None))
        self.effects = replace(self.effects, workspace=replace(self.effects.workspace,
            live_remote_branch_oid=self.effects.workspace.remote_branch_oid))
        self.effects.workspace.is_directory.return_value = False
        self.effects.workspace.branch_has_changes.return_value = True
        self.effects.workspace.git.side_effect = self.git
        self.effects.agent.validate_worker_workspace_scope.return_value = self.plan
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('staged',), 'accepted-content')},
        )
        self.effects.validation.validate_active_execplan_metadata.return_value = self.plan
        self.effects.validation.parse_worker_result.return_value = SimpleNamespace(
            commit_title='Concrete change', test_evidence='targeted test: PASS',
        )
        self.effects.validation.verify.side_effect = lambda _: (self.calls.append('verify') or True, 'OK')
        self.effects.validation.parse_reviewer_result.return_value = ('PASS', ())
        self.effects.agent.run_agent_with_retries.side_effect = lambda call, session_id: call(session_id, False)
        self.effects.agent.run_agent.side_effect = self.agent
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'model', 'high',
        )
        self.effects.publication.published_execplan.return_value = self.plan
        def push(*args):
            self.calls.append('push')
            self.effects.workspace.remote_branch_oid.return_value = self.remote
            return self.remote
        self.effects.publication.push_managed_publication_branch.side_effect = push
        self.effects.publication.command.side_effect = lambda *a, **k: self.calls.append('pr') or SimpleNamespace(stdout='https://example/pr/1')
        self.effects.revision.reconcile_catalog.side_effect = lambda *_: SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha=self.remote
        )
        self.effects.revision.body_hash.return_value = 'body'
        self.effects.revision.source_fingerprint.return_value = 'source'
        self.effects.revision.changed_paths.return_value = ()
        self.effects.revision.unmet_dependencies.return_value = ()
        self.effects.archive.requirement_archive_sources.return_value = ()
        self.handler = handlers.ExecutionHandlers(
            self.effects, repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')
        # This class exercises phase policy with fake Git effects. Journal
        # persistence and ownership are covered by test_managed_mutation_evidence;
        # keep these legacy policy tests at the Git operation boundary.
        journal_start = mock.patch.object(
            handlers.manager_git_journal, 'begin_rebase',
            return_value='test-rebase-token',
        )
        self.journal_start = journal_start
        journal_start.start()
        self.addCleanup(journal_start.stop)
        self.handler._rebase_journal_state = mock.Mock(return_value={})
        self.handler._begin_rebase_continuation = mock.Mock(
            return_value='test-rebase-token',
        )
        self.handler._finish_rebase_journal = mock.Mock(return_value=True)
        def fake_complete_rebase(context, *, project_values=None):
            checkpoint = self.handler.effects.checkpoint
            if checkpoint is not None:
                current = checkpoint.get('repo', context.plan.plan_id)
                if current.operation == 'rebase':
                    checkpoint.record(
                        'repo', context.plan.plan_id,
                        expected_generation=current.generation, operation=None,
                        expected_head=self.remote, target_base_oid=current.target_base_oid,
                        observed_remote_oid=current.observed_remote_oid,
                        snapshot=current.snapshot,
                    )
            from managed_rebase_adoption import revalidation_values
            target = (project_values or {}).get('active_sha') or self.remote
            self.state.update_project('repo', **revalidation_values(target))
        self.handler._complete_rebase_intent = mock.Mock(side_effect=fake_complete_rebase)
        def fake_current_service(context, **options):
            from managed_rebase_validation import RebaseDecision, RebaseStatus, Remote
            current = self.handler.effects.checkpoint.get('repo', context.plan.plan_id)
            in_progress = bool(self.effects.workspace._rebase_in_progress_for_branch(
                context.worktree, context.branch))
            stages = self.handler._unmerged_stages(context.worktree)
            remote = Remote('repo', context.branch, options.get('remote_oid', current.observed_remote_oid))
            observed = SimpleNamespace(remote=remote, head=self.remote,
                metadata=(('onto', current.target_base_oid),) if in_progress else None,
                index=(SimpleNamespace(stage=1),) if stages else ())
            decision = RebaseDecision(RebaseStatus.WAITING if in_progress or stages else RebaseStatus.COMPLETE)
            return SimpleNamespace(expected=SimpleNamespace(remote=remote),
                require_owned=lambda **_options: (observed, decision),
                adopt=lambda *_args: fake_complete_rebase(context, project_values={'active_sha': current.target_base_oid}))
        self.handler._current_rebase_service = fake_current_service


        def fake_rebase(context, target_oid, *, autostash=False):
            arguments = ('--autostash', target_oid) if autostash else (target_oid,)
            return self.effects.workspace.git(
                context.worktree, 'rebase', *arguments, check=False,
            )

        self.handler._run_managed_rebase = fake_rebase

    def git(self, path, *args, **kwargs):
        self.calls.append(args[0])
        if args[0] == 'rebase' and '--skip' not in args:
            self.assertEqual(self.state.row()['last_error'], lm.REBASE_IN_PROGRESS_ERROR)
        if 'rebase' in args and ('--continue' in args or '--skip' in args):
            # The fake command succeeds, so its operation metadata must be
            # cleared just as a completed real rebase would be.
            self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        if args[0] == 'commit':
            self.dirty = False
        output = self.remote if args[0] == 'rev-parse' else ''
        if args[:2] == ('ls-files', '--cached'):
            output = 'app.py\0'
        if args[0] == 'status' and self.dirty:
            output = ' M app.py'
        return SimpleNamespace(stdout=output, returncode=0)

    def agent(self, *args, **kwargs):
        self.calls.append(args[4])
        kwargs['on_session_started']('session')
        return lm.AgentResult(0, READY_WORKER_RESULT, session_id='session')

    def context(self, phase=None):
        if phase is not None:
            self.state.update_project('repo', phase=phase)
        return self.handler._context()

    def review_context(self):
        snapshot = DefinitionSnapshot(
            revision_sha='base', path='docs/plans/[G-047] Runtime.md',
            title='Runtime', sources=('docs/PRODUCT.md',), depends_on=(),
            complexity='medium', risk='medium', priority='normal',
            body_hash='body', source_fingerprint='source', archive_candidates=(),
        )
        self.handler._definition_snapshot = snapshot
        self.state.update_project(
            'repo', phase='REVIEWING', active_sha='base',
            validated_base_sha='base', worker_result='worker result',
            verify_result='successful verify output',
            worker_input_token=snapshot.worker_token,
            archive_token=snapshot.archive_token,
            verify_token=snapshot.verify_token('base', 'base'),
        )
        return self.handler._context()

    def observed_git(self, path, *args, **kwargs):
        if args[0] == 'symbolic-ref':
            return SimpleNamespace(stdout='loop-manager/G-047\n', returncode=0)
        if args[0] == 'ls-files':
            return SimpleNamespace(
                stdout='app.py\0' if args[1:2] == ('--cached',) else '',
                returncode=0,
            )
        if args[:2] == ('diff', '--cached'):
            return SimpleNamespace(stdout='', returncode=0)
        if args[0] == 'merge-base':
            return SimpleNamespace(stdout='', returncode=0)
        if args == ('rev-parse', 'HEAD'):
            return SimpleNamespace(stdout='owned-head\n', returncode=0)
        return self.git(path, *args, **kwargs)

    def _use_runtime_contract(self):
        runtime = lm._ExecutionLifecyclePorts(
            self.connection, self.repository,
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        self.effects = replace(self.effects, contract=runtime.effects.contract)
        self.handler.effects = self.effects

    def _with_feedback(self, feedback):
        self.effects = replace(
            self.effects,
            feedback=handlers.FeedbackEffects(
                feedback, lm.validation_failure_fingerprints,
            ),
        )
        self.handler.effects = self.effects

    def test_worker_prompt_delivers_validation_once_and_keeps_other_errors(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        feedback.observe(
            'repo', 'G-047', 'revision', 'verify', False,
            'private raw prelude\nfirst failure',
            (Feedback('verify', 'verify:fingerprint', 'first failure'),),
        )
        self.state.update_project(
            'repo', last_error='전체 검증 실패:\nprivate raw prelude\nfirst failure',
            verify_result='private raw prelude\nfirst failure',
            worker_result='previous result', worker_session_id='same-session',
        )
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, VALID_WORKER_RESULT, session_id='same-session',
        )
        with mock.patch.object(self.handler, '_finish_worker', return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
            first_prompt = self.effects.agent.run_agent.call_args.args[5]
            self.handler.run(self.context())
            second_prompt = self.effects.agent.run_agent.call_args.args[5]
        self.assertIn('verify:fingerprint occurrence=1 revision=1', first_prompt)
        self.assertIn('WORKER_OUTCOME:', first_prompt)
        self.assertIn('needs_user_decision', second_prompt)
        self.assertEqual(first_prompt.count('first failure'), 1)
        self.assertNotIn('private raw prelude', first_prompt)
        self.assertNotIn('first failure', second_prompt)
        self.assertNotIn('verify:fingerprint', second_prompt)
        self.state.update_project('repo', last_error='Worker 결과 검증 실패:\nbad format')
        with mock.patch.object(self.handler, '_finish_worker', return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertIn('bad format', self.effects.agent.run_agent.call_args.args[5])

    def _prepare_pending_guidance(self, *, implementation_changes):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        worktree = Path(directory.name)
        plan_path = self.handler.catalog_plan['path']
        document_path = worktree / plan_path
        document_path.parent.mkdir(parents=True)
        document_path.write_text(
            guidance_plan().replace('G-020', 'G-047'), encoding='utf-8',
        )
        (worktree / 'AGENTS.md').write_text('# Agents\n', encoding='utf-8')
        before = lm.GitSnapshot(
            head='owned-head', branch='loop-manager/G-047', index={}, dirty={},
            refs={}, worktrees={}, config={}, operations={},
        )
        dirty = {plan_path: (('unstaged',), 'proposal-content')}
        if implementation_changes:
            dirty.update({
                'app.py': (('unstaged',), 'implementation-content'),
                'tests/test_app.py': (('untracked',), 'test-content'),
            })
        after = replace(before, dirty=dirty)
        self.connection.execute(lm.GUIDANCE_APPROVALS_SCHEMA)
        self.state.update_project(
            'repo', worktree=str(worktree), agent_role='worker',
            agent_snapshot=before.serialize(), worker_session_id='saved-session',
        )
        self.effects.agent.inspect_worker_guidance_result.side_effect = self.state.bind(
            lm.inspect_worker_guidance_result, connected=True, mutates=True,
        )
        # Scope clearing must really mutate state so an ordering regression is
        # visible after the real guidance transaction persists the block.
        self.effects.agent.clear_agent_scope.side_effect = self.state.bind(
            lm.clear_agent_scope, connected=True, mutates=True,
        )
        self.effects.agent.capture_git_snapshot.return_value = after
        self.handler.catalog_plan = self.repository.active_row('repo')
        return worktree, before, after

    def _assert_pending_guidance(self, result, worktree, before, after, session):
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        projected = self.repository.projected('repo')
        self.assertEqual(projected.status, 'PLAN_BLOCKED')
        self.assertEqual(projected.agent_role, 'worker')
        self.assertEqual(projected.agent_snapshot, before.serialize())
        self.assertEqual(projected.agent_result_snapshot, after.serialize())
        blocked = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'",
        ).fetchone()
        self.assertEqual(blocked['status'], 'PLAN_BLOCKED')
        self.assertEqual(blocked['blocked_reason'], lm.GUIDANCE_BLOCK_REASON)
        self.assertEqual(blocked['agent_role'], 'worker')
        self.assertEqual(blocked['agent_snapshot'], before.serialize())
        self.assertEqual(blocked['agent_result_snapshot'], after.serialize())
        self.assertEqual(blocked['worker_session_id'], session)
        self.assertEqual(blocked['branch'], before.branch)
        self.assertEqual(blocked['worktree'], str(worktree))
        approval = self.connection.execute(
            "SELECT * FROM guidance_approvals WHERE repo = 'repo' AND plan_id = 'G-047'",
        ).fetchone()
        self.assertEqual(approval['proposal_id'], 'worker-rule')
        self.assertFalse(approval['applied'])
        self.assertFalse(approval['decision_hash'])
        self.assertEqual((worktree / 'AGENTS.md').read_text(), '# Agents\n')
        self.assertIn('Decision: `pending`', (worktree / blocked['path']).read_text())
        self.effects.agent.clear_agent_scope.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()

    def test_guidance_result_keeps_worker_scope_until_block_is_recorded(self):
        worktree, before, after = self._prepare_pending_guidance(
            implementation_changes=True,
        )
        with (
            mock.patch.object(lm, 'capture_git_snapshot', return_value=after),
            mock.patch.object(lm, 'is_loop_manager_repository', return_value=False),
        ):
            result = self.handler.run(self.context())
        self._assert_pending_guidance(result, worktree, before, after, 'session')
        self.assertEqual(self.repository.get('repo', 'G-047').worker_result,
                         READY_WORKER_RESULT)
        self.effects.agent.validate_worker_workspace_scope.assert_called_once()

    def test_saved_rebase_guidance_result_keeps_worker_scope(self):
        worktree, before, after = self._prepare_pending_guidance(
            implementation_changes=False,
        )
        saved = SimpleNamespace(
            scope_marker='accepted', worker_code=0, worker_result='proposal',
        )
        with (
            mock.patch.object(lm, 'capture_git_snapshot', return_value=after),
            mock.patch.object(lm, 'is_loop_manager_repository', return_value=False),
        ):
            result = self.handler._resume_saved_rebase_worker(
                self.context(), saved, SimpleNamespace(progress='accepted'),
            )
        self._assert_pending_guidance(result, worktree, before, after, 'saved-session')
        self.assertEqual(self.repository.get('repo', 'G-047').worker_result, 'proposal')
        self.effects.agent.run_agent.assert_not_called()

    def _run_real_worker_guidance_checkpoint(self, *, implementation_changes):
        fixture = test_blocked_checkpoint.BlockedCheckpointGitAdapterTest(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        repo = str(fixture.repo.resolve())
        plan_path = 'docs/plans/[G-034] Checkpoint.md'
        (fixture.worktree / 'AGENTS.md').write_text('# Agents\n')
        fixture.run_git(fixture.worktree, 'add', 'AGENTS.md')
        fixture.run_git(fixture.worktree, 'commit', '-m', 'guidance baseline')
        plan_row = fixture.connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()
        lm.hydrate_execplan_project(fixture.connection, repo, plan_row)
        state = ExecutionStatePort(
            fixture.connection, runtime_state.RuntimeStateRepository(fixture.connection), repo,
        )
        effects = replace(self.effects, state=state)
        effects.agent.begin_agent_scope.side_effect = state.bind(
            lm.begin_agent_scope, connected=True, mutates=True,
        )
        effects.agent.inspect_worker_guidance_result.side_effect = state.bind(
            lm.inspect_worker_guidance_result, connected=True, mutates=True,
        )
        effects.agent.clear_agent_scope.side_effect = state.bind(
            lm.clear_agent_scope, connected=True, mutates=True,
        )
        effects.agent.capture_git_snapshot.side_effect = lm.capture_git_snapshot
        effects.agent._guidance_worker_targets.return_value = ()
        effects.agent._published_plan_allows_common_plans.return_value = False
        effects.agent.worker_prompt.return_value = 'worker guidance prompt'
        plan = {
            'id': 'G-034', 'title': 'Checkpoint',
            'sources': ['docs/PRODUCT.md'], 'depends_on': [],
            'complexity': 'high', 'risk': 'critical', 'priority': 'critical',
        }

        def validate_scope(_repo_path, worktree, _branch, _base, selected,
                           _catalog, before, **_kwargs):
            lm.validate_agent_scope(
                'worker', worktree, before, lm.capture_git_snapshot(worktree),
                selected_plan_path=selected,
            )
            return plan

        effects.agent.validate_worker_workspace_scope.side_effect = validate_scope

        def worker(_provider, worktree, *_args, **kwargs):
            (worktree / plan_path).write_text(
                guidance_plan().replace('G-020', 'G-034')
                .replace('title: Guidance', 'title: Checkpoint'),
            )
            if implementation_changes:
                (worktree / 'code.py').write_text('safe implementation\n')
                test_path = worktree / 'tests/test_app.py'
                test_path.parent.mkdir(parents=True, exist_ok=True)
                test_path.write_text('def test_safe():\n    assert True\n')
            kwargs['on_session_started']('real-worker-session')
            return lm.AgentResult(0, READY_WORKER_RESULT,
                                  session_id='real-worker-session')

        effects.agent.run_agent.side_effect = worker
        handler = handlers.ExecutionHandlers(
            effects, repo=repo, repo_path=fixture.repo,
            state_dir=fixture.state_dir, base='main', base_sha=fixture.base_oid,
            provider='codex', config_path=Path('/config'), newly_activated=False,
        )
        handler.plan = plan
        handler.catalog_plan = plan_row
        with mock.patch.object(lm, 'is_loop_manager_repository', return_value=False):
            outcome = handler.run(handler._context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(fixture.connection.execute(
            "SELECT blocked_reason FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()[0], lm.GUIDANCE_BLOCK_REASON)
        blocked = fixture.connection.execute(
            "SELECT agent_role, agent_snapshot, agent_result_snapshot, "
            "worker_session_id FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()
        self.assertEqual(blocked['agent_role'], 'worker')
        self.assertTrue(blocked['agent_snapshot'])
        self.assertTrue(blocked['agent_result_snapshot'])
        self.assertEqual(blocked['worker_session_id'], 'real-worker-session')
        effects.validation.verify.assert_not_called()
        effects.publication.push_managed_publication_branch.assert_not_called()
        with mock.patch.object(lm, 'pr_for_branch', return_value=None):
            self.assertEqual(lm.dispatch_blocked_checkpoints(
                fixture.connection, fixture.repo, fixture.state_dir, repo,
            ), 1)
        oid = fixture.run_git(fixture.worktree, 'rev-parse', 'HEAD')
        changed = set(fixture.run_git(
            fixture.worktree, 'diff-tree', '--no-commit-id', '--name-only', '-r', oid,
        ).splitlines())
        expected = {plan_path}
        if implementation_changes:
            expected.update(('code.py', 'tests/test_app.py'))
        self.assertEqual(changed, expected)
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(
            fixture.connection,
        ).get(repo, 'G-034')
        self.assertEqual(checkpoint.remote_oid, oid)
        self.assertEqual(lm.validate_persisted_agent_scope(
            fixture.connection, fixture.repo, fixture.state_dir, repo,
        ).head, oid)

    def test_real_worker_guidance_plan_only_checkpoint_tree(self):
        self._run_real_worker_guidance_checkpoint(implementation_changes=False)

    def test_real_worker_guidance_with_code_and_test_checkpoint_tree(self):
        self._run_real_worker_guidance_checkpoint(implementation_changes=True)

    def test_timeout_prompt_requests_cause_check_and_progress_evidence(self):
        prompt = lm.worker_prompt(
            'docs/plans/[G-047] Runtime.md', None,
            validation_feedback='전체 verify 시간 초과: verify_timeout_seconds=2',
        )
        self.assertIn('무한 반복·교착·중복 검증', prompt)
        self.assertIn('PR_SUMMARY 안에 VERIFY_TIMEOUT_LIMIT_INSUFFICIENT:', prompt)

    def test_session_unavailable_rebuilds_compact_unresolved_feedback(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        feedback.observe(
            'repo', 'G-047', 'revision', 'review', False,
            'raw Review detail omitted from prompt',
            (Feedback('review', 'review:a.py::f::bug', 'Fix the assertion'),),
        )
        self.state.update_project(
            'repo', worker_session_id='missing-session',
            worker_result='previous result', review_result='raw Review detail omitted from prompt',
            last_error='Review 실패:\nraw Review detail omitted from prompt',
        )
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        prompts = []

        def invoke(*args, **kwargs):
            prompts.append(args[5])
            if kwargs['session_id']:
                return lm.AgentResult(1, 'unknown session', session_id='missing-session')
            kwargs['on_session_started']('replacement')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='replacement')

        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker', return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(len(prompts), 2)
        self.assertIn('AGENTS.md, 선택된 최신 ExecPlan과 현재 worktree', prompts[1])
        self.assertIn('previous result', prompts[1])
        self.assertIn('review:a.py::f::bug occurrence=1 revision=1', prompts[1])
        self.assertIn('Fix the assertion', prompts[1])
        self.assertNotIn('raw Review detail omitted from prompt', prompts[1])
        self.assertEqual(feedback.prepare('repo', 'G-047', 'replacement').rows, ())

    def test_model_replacement_records_intent_before_agent_and_binds_event(self):
        old = SessionBinding('codex', 'old-model', 'low')
        self.state.update_project(
            'repo', worker_session_id='old-session',
            worker_session_binding=old.serialize(), worker_result='saved result',
            verify_result='raw verify', publication_token='publication',
        )
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'new-model', 'high',
        )
        observed = []
        def invoke(*args, **kwargs):
            row = self.state.row()
            observed.append((kwargs['session_id'], row['worker_session_id'],
                             SessionTransition.deserialize(row['worker_session_transition']),
                             kwargs['resolved_profile'], kwargs['invocation']))
            kwargs['on_session_started']('new-session')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='new-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        session, stored, intent, profile, invocation = observed[0]
        self.assertIsNone(session)
        self.assertIsNone(stored)
        self.assertEqual((intent.cause, intent.previous_session),
                         ('model_changed', 'old-session'))
        self.assertEqual((profile.model, profile.effort), ('new-model', 'high'))
        self.assertEqual((invocation.transition, invocation.transition_id,
                          invocation.previous_model),
                         ('replacement', intent.identity, 'old-model'))
        row = self.state.row()
        self.assertEqual((row['worker_session_id'], row['worker_session_binding']),
                         ('new-session', intent.target.serialize()))
        self.assertIsNone(row['worker_session_transition'])
        self.assertEqual((row['worker_result'], row['verify_result'],
                          row['publication_token']),
                         ('saved result', 'raw verify', 'publication'))

    def test_legacy_unbound_session_starts_recovery_without_resuming(self):
        self.state.update_project('repo', worker_session_id='legacy-session',
                                  worker_session_binding=None)
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        def invoke(*args, **kwargs):
            self.assertIsNone(kwargs['session_id'])
            self.assertEqual(kwargs['invocation'].transition_cause,
                             'unbound_session_recovery')
            self.assertIn('AGENTS.md, 선택된 최신 ExecPlan과 현재 worktree', args[5])
            kwargs['on_session_started']('bound-session')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='bound-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(self.state.row()['worker_session_id'], 'bound-session')

    def test_provider_change_launches_target_provider_without_old_session(self):
        self.state.update_project('repo', worker_session_id='codex-session')
        self.handler.provider = 'claude'
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'model', 'high',
        )
        def invoke(*args, **kwargs):
            self.assertEqual(args[0], 'claude')
            self.assertIsNone(kwargs['session_id'])
            self.assertEqual(kwargs['invocation'].transition_cause,
                             'provider_changed')
            kwargs['on_session_started']('claude-session')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='claude-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(self.state.row()['worker_session_binding'],
                         SessionBinding('claude', 'model', 'high').serialize())

    def test_replacement_start_failure_reuses_pending_target_after_restart(self):
        old = SessionBinding('codex', 'old-model', 'low')
        self.state.update_project('repo', worker_session_id='old-session',
                                  worker_session_binding=old.serialize())
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'new-model', 'high',
        )
        self.effects.agent.run_agent.side_effect = RuntimeError('start interrupted')
        with self.assertRaisesRegex(RuntimeError, 'start interrupted'):
            self.handler.run(self.context())
        pending = SessionTransition.deserialize(
            self.state.row()['worker_session_transition'],
        )
        self.assertIsNone(self.state.row()['worker_session_id'])
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'old-model', 'low',
        )
        def invoke(*args, **kwargs):
            self.assertIsNone(kwargs['session_id'])
            self.assertEqual(kwargs['resolved_profile'].model, 'new-model')
            kwargs['on_session_started']('new-session')
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='new-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(self.state.row()['worker_session_id'], 'new-session')
        self.assertIsNone(self.state.row()['worker_session_transition'])
        self.assertEqual(pending.identity,
                         self.effects.agent.run_agent.call_args.kwargs['invocation'].transition_id)

    def test_level_and_alias_change_with_same_resolved_model_resumes_session(self):
        self.state.update_project('repo', worker_session_id='same-session')
        self.handler.level = 7
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'model', 'low',
        )
        def invoke(*args, **kwargs):
            self.assertEqual(kwargs['session_id'], 'same-session')
            self.assertEqual(kwargs['resolved_profile'].effort, 'low')
            self.assertEqual(kwargs['invocation'].transition, 'effort_resume')
            self.assertIsNone(self.state.row()['worker_session_transition'])
            return lm.AgentResult(0, VALID_WORKER_RESULT, session_id='same-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(self.state.row()['worker_session_binding'],
                         SessionBinding('codex', 'model', 'low').serialize())

    def test_unavailable_fallback_intent_survives_failed_new_start(self):
        self.state.update_project('repo', worker_session_id='old-session',
                                  worker_result='saved result',
                                  verify_result='raw verify')
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        calls = []
        def interrupted(*args, **kwargs):
            calls.append(kwargs['session_id'])
            if kwargs['session_id']:
                return lm.AgentResult(1, 'unknown session', session_id='old-session')
            pending = SessionTransition.deserialize(
                self.state.row()['worker_session_transition'],
            )
            self.assertEqual((pending.kind, pending.previous_session),
                             ('fallback', 'old-session'))
            self.assertIn('saved result', args[5])
            raise RuntimeError('new session interrupted')
        self.effects.agent.run_agent.side_effect = interrupted
        with self.assertRaisesRegex(RuntimeError, 'new session interrupted'):
            self.handler.run(self.context())
        self.assertEqual(calls, ['old-session', None])
        pending = SessionTransition.deserialize(
            self.state.row()['worker_session_transition'],
        )
        self.assertIsNone(self.state.row()['worker_session_id'])
        self.assertEqual(self.state.row()['verify_result'], 'raw verify')
        def recovered(*args, **kwargs):
            self.assertIsNone(kwargs['session_id'])
            self.assertIn('saved result', args[5])
            self.assertEqual(kwargs['invocation'].transition_id, pending.identity)
            kwargs['on_session_started']('replacement-session')
            return lm.AgentResult(0, VALID_WORKER_RESULT,
                                  session_id='replacement-session')
        self.effects.agent.run_agent.side_effect = recovered
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(self.state.row()['worker_session_id'], 'replacement-session')
        self.assertIsNone(self.state.row()['worker_session_transition'])

    def test_timeout_after_session_event_retries_observed_session(self):
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        attempts = []
        def invoke(*args, **kwargs):
            attempts.append(kwargs['session_id'])
            if len(attempts) == 1:
                kwargs['on_session_started']('observed-session')
                raise lm.subprocess.TimeoutExpired('agent', 1)
            return lm.AgentResult(0, VALID_WORKER_RESULT,
                                  session_id='observed-session')
        self.effects.agent.run_agent.side_effect = invoke
        with mock.patch.object(self.handler, '_finish_worker',
                               return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertEqual(attempts, [None, 'observed-session'])
        self.assertEqual(self.state.row()['worker_session_id'], 'observed-session')

    def test_verify_audit_keeps_full_raw_output_and_success_clears_feedback(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        raw = 'beginning of raw output\n' + 'x' * 9000 + '\nFAIL'
        self.effects.validation.verify.side_effect = [
            (False, lm.VerifyOutput(raw)), (True, lm.VerifyOutput('SUCCESS output')),
        ]
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='fingerprint',
        )

        self.handler._verify_once(self.context('VERIFYING'))
        self.assertEqual(feedback.prepare('repo', 'G-047', 'session').rows[0]['occurrence'], 1)
        self.handler._verify_once(self.context('VERIFYING'))
        audit = self.connection.execute(
            "SELECT classification,raw_output FROM validation_results ORDER BY id"
        ).fetchall()
        self.assertEqual([(item['classification'], item['raw_output']) for item in audit],
                         [('failure', raw), ('success', 'SUCCESS output')])
        self.assertEqual(feedback.prepare('repo', 'G-047', 'session').rows, ())

    def test_verify_feedback_extracts_full_failure_source_not_persisted_tail(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        raw = (
            'FAILED tests/test_focus.py::test_message - AssertionError: expected message\n'
            'E       assert actual == expected\n'
            '================= 1 failed, 99 passed =================\n'
            + 'PASS unrelated completion\n' * 800
        )
        self.effects.validation.verify.side_effect = None
        self.effects.validation.verify.return_value = (
            False, lm.VerifyOutput(raw, exit_code=1),
        )
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='fingerprint',
        )

        self.handler._verify_once(self.context('VERIFYING'))

        row, = feedback.prepare('repo', 'G-047', 'session').rows
        self.assertIn('tests/test_focus.py::test_message', row['diagnostic'])
        self.assertIn('assert actual == expected', row['diagnostic'])
        self.assertIn('verify exit code: 1', row['diagnostic'])
        self.assertNotIn('unrelated completion', row['diagnostic'])
        audit = self.connection.execute(
            'SELECT raw_output FROM validation_results'
        ).fetchone()
        self.assertEqual(audit['raw_output'], raw)

    def test_timeout_verify_uses_stable_fingerprint_and_returns_to_worker(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        self.effects.validation.verify.side_effect = None
        self.effects.validation.verify.return_value = (
            False, lm.VerifyOutput(
                'progress\n전체 verify 시간 초과: verify_timeout_seconds=2',
                timed_out=True,
            ),
        )
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='timeout',
        )
        outcome, output = self.handler._verify_once(self.context('VERIFYING'))
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertIn('전체 verify 시간 초과:', output)
        first = lm.validation_failure_fingerprints('verify', output)[0][0]
        second = lm.validation_failure_fingerprints(
            'verify', lm.VerifyOutput('other progress', timed_out=True),
        )[0][0]
        self.assertEqual(first, second)
        self.effects.validation.transition_validation_failure.assert_called_once()

    def test_unconfirmed_verify_termination_blocks_project(self):
        self.effects.validation.verify.side_effect = lm.VerifyTerminationUncertain(
            'verify process group 잔존 (PGID 123)'
        )
        with mock.patch.object(self.handler, '_block_final') as blocked:
            outcome, _ = self.handler._verify_once(self.context('VERIFYING'))
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            blocked.call_args.kwargs['reason'],
            lm.BlockReasonCode.REPOSITORY_STATE_UNSAFE,
        )

    def test_worker_evidence_blocks_insufficient_verify_limit(self):
        self.state.update_project(
            'repo', phase='WORKING',
            verify_result='progress\n전체 verify 시간 초과: verify_timeout_seconds=2',
        )
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0,
            'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
            'COMMIT_TITLE: Fix verify\nPR_SUMMARY: progress\n'
            'TARGETED_TESTS: none\n'
            'VERIFY_TIMEOUT_LIMIT_INSUFFICIENT: 9 of 10 required tests completed',
            session_id='session',
        )
        self.effects.validation.parse_worker_result.return_value = SimpleNamespace(
            commit_title='Fix verify'
        )
        self.effects.validation.block_verify_timeout_limit.return_value = 'limit too short'
        with mock.patch.object(self.handler, '_finish_worker') as finish:
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.validation.block_verify_timeout_limit.assert_called_once()
        finish.assert_not_called()

    def test_review_observation_tracks_each_issue_and_pass_resolves_them(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(
            blocked=False, fingerprint='fingerprint',
        )
        fail = ('ISSUE_ID: z.py::f::bug\nFix Z\n'
                'ISSUE_ID: a.py::g::bug\nFix A\nVERDICT: FAIL')
        self.effects.agent.run_agent.side_effect = [
            lm.AgentResult(0, fail, session_id='review-session'),
            lm.AgentResult(0, 'VERDICT: PASS', session_id='review-session'),
        ]

        self.handler.review(self.review_context())
        self.assertEqual([row['identity'] for row in feedback.prepare(
            'repo', 'G-047', 'worker-session',
        ).rows], ['review:a.py::g::bug', 'review:z.py::f::bug'])
        self.handler.review(self.review_context())
        self.assertEqual(feedback.prepare('repo', 'G-047', 'worker-session').rows, ())
        self.assertEqual(self.connection.execute(
            "SELECT classification FROM validation_results ORDER BY id"
        ).fetchall()[0][0], 'failure')

    def test_review_retry_audits_each_returned_attempt(self):
        self._with_feedback(ValidationFeedbackRepository(self.connection))
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        self.effects.validation.parse_reviewer_result.side_effect = lm.parse_reviewer_result
        raw = 'service unavailable: original reviewer output'
        self.effects.agent.run_agent.side_effect = [
            lm.AgentResult(1, raw, kind=lm.AgentResultKind.TRANSIENT_FAILURE),
            lm.AgentResult(0, 'VERDICT: PASS'),
        ]
        self.handler.review(self.review_context())
        audit = self.connection.execute(
            "SELECT classification,raw_output FROM validation_results ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in audit],
                         [('transient_failure', raw), ('success', 'VERDICT: PASS')])

    def test_review_post_call_rejections_audit_each_execution_once(self):
        self._with_feedback(ValidationFeedbackRepository(self.connection))
        for code in (0, 1):
            for rejection in ('scope', 'ownership', 'snapshot', 'stale'):
                with self.subTest(code=code, rejection=rejection):
                    raw = f'reviewer output {code} {rejection}'
                    result = lm.AgentResult(code, raw)
                    self.effects.agent.run_agent.side_effect = lambda *a, **k: result
                    self.effects.agent.validate_agent_scope.side_effect = (
                        lm.AgentScopeError('scope') if rejection == 'scope' else None)
                    self.effects.workspace.validate_managed_worktree.side_effect = (
                        lm.WorkspaceOwnershipError('ownership')
                        if rejection == 'ownership' else None)
                    self.effects.agent.capture_git_snapshot.side_effect = (
                        RuntimeError('snapshot') if rejection == 'snapshot' else None)
                    context = self.review_context()
                    stale = ExecutionOutcome(ExecutionOutcomeKind.RETRY, detail='stale')
                    with mock.patch.object(self.handler, '_block_final'), mock.patch.object(
                        self.handler, '_post_effect_guard', return_value=stale,
                    ):
                        outcome = self.handler.review(context)
                    if rejection == 'stale':
                        self.assertIs(outcome, stale)
                    audit = self.connection.execute(
                        "SELECT classification FROM validation_results WHERE raw_output=?",
                        (raw,),
                    ).fetchall()
                    expected = (result.ledger_kind if code else
                                'stale_result' if rejection == 'stale' else 'scope_failure')
                    self.assertEqual([row[0] for row in audit], [expected])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_feedback_events"
        ).fetchone()[0], 0)

    def test_review_retry_interruption_preserves_previous_raw_result(self):
        self._with_feedback(ValidationFeedbackRepository(self.connection))
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        raw = 'service unavailable: retained before retry'
        self.effects.agent.run_agent.side_effect = [
            lm.AgentResult(1, raw, kind=lm.AgentResultKind.TRANSIENT_FAILURE),
            KeyboardInterrupt(),
        ]
        with self.assertRaises(KeyboardInterrupt):
            self.handler.review(self.review_context())
        audit = self.connection.execute(
            "SELECT classification,raw_output FROM validation_results ORDER BY id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in audit], [('transient_failure', raw)])

    def test_invalid_worker_result_keeps_feedback_pending(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        feedback.observe(
            'repo', 'G-047', 'revision', 'verify', False, 'raw',
            (Feedback('verify', 'verify:fingerprint', 'fix this'),),
        )
        self.state.update_project('repo', worker_session_id='same-session')
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, 'invalid format', session_id='same-session',
        )
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError('format')

        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(feedback.prepare('repo', 'G-047', 'same-session').rows[0]['revision'], 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM validation_delivery_attempts WHERE status='delivered'"
        ).fetchone()[0], 0)

    def _decision_workspace(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        worktree = Path(directory.name)
        plan_path = worktree / 'docs/plans/[G-047] Runtime.md'
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text('# Runtime\n## Decision Log\n', encoding='utf-8')
        product = worktree / 'docs/PRODUCT.md'
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text('# Product\n## User approval\n', encoding='utf-8')
        architecture = worktree / 'docs/ARCHITECTURE.md'
        architecture.write_text('# Architecture\n## Architecture Change Policy\n',
                                encoding='utf-8')
        self.state.update_project('repo', worktree=str(worktree))
        self.effects.agent.begin_agent_scope.side_effect = lambda *_args, **_kwargs: (
            self.state.update_project(
                'repo', agent_role='worker', agent_snapshot='before-scope',
                agent_input_fingerprint='remote-input',
            ) or 'before-scope'
        )
        self.effects.agent._revision_execplan_input_fingerprint.return_value = 'remote-input'
        self.effects.blocking.block_active_execplan.side_effect = (
            lambda *args, **kwargs: lm.block_active_execplan(
                self.connection, *args, **kwargs,
            )
        )
        return worktree

    def test_worker_decision_blocks_before_publication_and_keeps_failure_history(self):
        self._decision_workspace()
        message = worker_decision()
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, message, session_id='session')
        )
        self.connection.execute(
            "UPDATE exec_plans SET verify_failure_history = ?, "
            "review_failure_history = ?, effective_worker_level = 7 "
            "WHERE repo = 'repo' AND plan_id = 'G-047'",
            ('{"verify":2}', '{"review":3}'),
        )
        self.connection.commit()
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        blocked = self.repository.get('repo', 'G-047')
        self.assertEqual(blocked.status, 'PLAN_BLOCKED')
        row = self.connection.execute(
            "SELECT blocked_reason, blocked_fingerprint, worker_result, "
            "worker_session_id, agent_snapshot, agent_result_snapshot, "
            "verify_failure_history, review_failure_history, effective_worker_level "
            "FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(row['blocked_reason'], 'user_decision_required')
        self.assertEqual(row['blocked_fingerprint'], 'remote-input')
        self.assertEqual(row['worker_result'], message)
        self.assertEqual(row['worker_session_id'], 'session')
        self.assertEqual(row['agent_snapshot'], 'before-scope')
        self.assertEqual(row['agent_result_snapshot'], 'after-scope')
        self.assertEqual(row['verify_failure_history'], '{"verify":2}')
        self.assertEqual(row['review_failure_history'], '{"review":3}')
        self.assertEqual(row['effective_worker_level'], 7)
        recovery = self.connection.execute(
            "SELECT final_evidence FROM recovery_records WHERE repo = 'repo' "
            "AND subject = 'G-047'"
        ).fetchone()
        self.assertEqual(json.loads(recovery[0])['worker_decision']['question'],
                         '이 변경을 승인합니까?')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.agent.clear_agent_scope.assert_not_called()
        self.assertNotIn('commit', self.calls)

    def test_worker_decision_without_file_change_bypasses_no_change_gate(self):
        self._decision_workspace()
        self.dirty = False
        self.effects.workspace.branch_has_changes.return_value = False
        message = worker_decision()
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, message, session_id='session')
        )
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'unchanged-scope'
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.connection.execute(
            "SELECT blocked_reason FROM exec_plans WHERE repo = 'repo' "
            "AND plan_id = 'G-047'"
        ).fetchone()[0], 'user_decision_required')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()

    def test_worker_decision_checkpoint_wait_survives_three_restarted_runs(self):
        worktree = self._decision_workspace()
        notifications.configure_session(self.connection, enabled=True)
        self.connection.execute(
            "UPDATE exec_plans SET workspace_owned = 1, "
            "verify_failure_history = '{\"verify\":2}', "
            "review_failure_history = '{\"review\":3}' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        plan_file = worktree / 'docs/plans/[G-047] Runtime.md'
        plan_file.write_text(
            plan_file.read_text(encoding='utf-8') + '- Worker decision pending\n',
            encoding='utf-8',
        )
        message = worker_decision()
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, message, session_id='session')
        )
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection).get('repo', 'G-047').phase,
            blocked_checkpoint.CheckpointPhase.REQUESTED)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / 'state.sqlite'
        with sqlite3.connect(database) as saved:
            self.connection.backup(saved)

        self.effects.agent.run_agent.reset_mock()
        self.effects.validation.verify.reset_mock()
        self.effects.validation.parse_reviewer_result.reset_mock()
        local_only = blocked_checkpoint.SafetyVerdict(
            False, blocked_checkpoint.SafetyScope.PLAN,
            'git_operation_in_progress', 'unresolved rebase', None, (),
        )
        for iteration in range(3):
            with sqlite3.connect(database) as connection:
                connection.row_factory = sqlite3.Row
                notifications.configure_session(connection, enabled=True)
                with (
                    mock.patch.object(lm._BlockedCheckpointAdapter, 'assess',
                                      return_value=local_only) as assess,
                    mock.patch.object(lm, '_revision_execplan_input_fingerprint',
                                      return_value='remote-input'),
                    mock.patch.object(lm, '_revision_review_issue_fingerprint') as review,
                    mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                    mock.patch.object(lm, 'make_worktree'),
                    mock.patch.object(lm, '_resume_interrupted_blocked_rebase',
                                      return_value=None),
                    mock.patch.object(lm, 'validate_blocked_agent_scope'),
                ):
                    self.assertEqual(lm.dispatch_blocked_checkpoints(
                        connection, Path('/repo'), Path('/state'), 'repo',
                    ), 1 if iteration == 0 else 0)
                    self.assertFalse(lm.resume_plan_blocks(
                        connection, Path('/repo'), Path('/state'), 'repo', 'main',
                    ))
                    self.assertEqual(assess.call_count, 1 if iteration == 0 else 0)
                    review.assert_not_called()
                row = connection.execute(
                    "SELECT status, blocked_reason, worker_result, worker_session_id, "
                    "verify_failure_history, review_failure_history "
                    "FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                self.assertEqual(tuple(row), (
                    'PLAN_BLOCKED', 'user_decision_required', message, 'session',
                    '{"verify":2}', '{"review":3}',
                ))
                checkpoint = connection.execute(
                    "SELECT phase, result, unpublished_reason "
                    "FROM blocked_checkpoints WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                self.assertEqual(tuple(checkpoint),
                                 ('unpublished', 'local_only', 'unresolved rebase'))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox "
                    "WHERE repo = 'repo' AND event_type = 'blocked'"
                ).fetchone()[0], 1)
        self.assertIn('Worker decision pending', plan_file.read_text(encoding='utf-8'))
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.validation.parse_reviewer_result.assert_not_called()

    def test_malformed_worker_outcome_enters_protocol_correction(self):
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, 'WORKER_OUTCOME: {bad}', session_id='session')
        )
        result = self.handler.run(self.context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_new_worker_wait_without_outcome_declaration_enters_protocol_correction(self):
        message = (
            '사용자 승인을 기다려 구현을 중단했습니다.\n'
            'COMMIT_TITLE: Premature candidate\n'
            'PR_SUMMARY: Waiting for approval.\n'
            'TARGETED_TESTS:\ncase: PASS'
        )
        self.effects.agent.run_agent.side_effect = lambda *_args, **kwargs: (
            kwargs['on_session_started']('session') or
            lm.AgentResult(0, message, session_id='session')
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')
        self.assertIn('WORKER_OUTCOME', self.state.row()['last_error'])
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_saved_rebase_decision_blocks_before_stage(self):
        self._decision_workspace()
        message = worker_decision()
        self.state.update_project(
            'repo', agent_role='worker', agent_snapshot='before-scope',
            agent_input_fingerprint='remote-input',
        )
        saved = SimpleNamespace(
            scope_marker='same', worker_code=0, worker_result=message,
            generation=1, outcome_contract_version=1,
        )
        context = replace(self.context(), resolving_rebase=True)
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler._resume_saved_rebase_worker(
                context, saved, SimpleNamespace(progress='same'),
            )
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PLAN_BLOCKED')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.agent.clear_agent_scope.assert_not_called()
        self.assertNotIn('add', self.calls)

    def test_saved_verifying_decision_blocks_before_commit_and_verify(self):
        self._decision_workspace()
        message = worker_decision()
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result=message,
            agent_role=None, agent_snapshot=None, agent_input_fingerprint=None,
        )
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler.verify(self.context(), None)
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PLAN_BLOCKED')
        self.assertEqual(self.connection.execute(
            "SELECT blocked_fingerprint FROM exec_plans WHERE repo = 'repo' "
            "AND plan_id = 'G-047'"
        ).fetchone()[0], 'remote-input')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.assertNotIn('add', self.calls)

    def test_saved_decision_blocks_revision_refresh_before_checkpoint_commit(self):
        self._decision_workspace()
        message = worker_decision()
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result=message,
            agent_role=None, agent_snapshot=None, agent_input_fingerprint=None,
        )
        with mock.patch.object(lm, 'capture_git_snapshot') as snapshot:
            snapshot.return_value.serialize.return_value = 'after-scope'
            result = self.handler._refresh_revision_base(
                self.context(), None, None, 'new-base',
            )
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PLAN_BLOCKED')
        self.effects.validation.parse_worker_result.assert_not_called()
        self.assertNotIn('add', self.calls)
        self.assertNotIn('commit', self.calls)

    def test_no_change_rebase_blocks_before_sealed_completion(self):
        self.effects.workspace.git.side_effect = None
        self.effects.workspace.git.return_value = SimpleNamespace(
            stdout='', returncode=0,
        )
        self.effects.workspace.branch_has_changes.return_value = False
        self.handler._record_rebase_intent = mock.Mock()
        self.handler._run_managed_rebase = mock.Mock(return_value=SimpleNamespace(
            returncode=0, stdout='',
        ))
        self.handler._post_rebase_observation = mock.Mock(return_value=None)
        self.handler._complete_rebase_intent = mock.Mock()

        outcome = self.handler._refresh_revision_base(
            self.context('VERIFYING'), None,
            SimpleNamespace(phase='VERIFYING'), 'new-base',
        )

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.handler._complete_rebase_intent.assert_not_called()

    def test_failed_fallback_start_reuses_recovery_prompt_on_next_turn(self):
        feedback = ValidationFeedbackRepository(self.connection)
        self._with_feedback(feedback)
        feedback.observe(
            'repo', 'G-047', 'revision', 'verify', False, 'raw full failure',
            (Feedback('verify', 'verify:fingerprint', 'fix this'),),
        )
        self.state.update_project(
            'repo', worker_session_id='missing-session', worker_result='previous result',
            verify_result='raw full failure', last_error='전체 검증 실패:\nraw full failure',
        )
        self.effects.agent.worker_prompt.side_effect = lm.worker_prompt
        self.effects.agent.run_agent_with_retries.side_effect = lm.run_agent_with_retries
        prompts = []

        def failed_start(*args, **kwargs):
            prompts.append(args[5])
            if kwargs['session_id']:
                return lm.AgentResult(1, 'unknown session', session_id='missing-session')
            kwargs['on_session_started']('replacement')
            return lm.AgentResult(1, 'provider rejected', session_id='replacement')

        self.effects.agent.run_agent.side_effect = failed_start
        self.assertEqual(self.handler.run(self.context()).kind,
                         ExecutionOutcomeKind.OPERATIONAL_ERROR)
        self.effects.agent.run_agent.side_effect = lambda *args, **kwargs: (
            prompts.append(args[5]) or lm.AgentResult(
                0, VALID_WORKER_RESULT, session_id='replacement',
            )
        )
        with mock.patch.object(self.handler, '_finish_worker', return_value=SimpleNamespace(kind='done')):
            self.handler.run(self.context())
        self.assertIn('AGENTS.md, 선택된 최신 ExecPlan과 현재 worktree', prompts[-1])
        self.assertIn('previous result', prompts[-1])
        self.assertIn('verify:fingerprint occurrence=1 revision=1', prompts[-1])
        self.assertNotIn('raw full failure', prompts[-1])

    def test_single_abnormal_rebase_worker_result_preserves_session_without_block(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='same-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'abnormal exit', session_id='same-session',
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['worker_session_id'], 'same-session')
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')
        self.effects.blocking.block_active_execplan.assert_not_called()
        correction = self.handler.rebase_corrections.get('repo', 'G-047')
        self.assertEqual(correction.occurrence, 1)
        self.assertIsNone(correction.worker_result)
        self.assertEqual(correction.audit[-1]['worker_result'], 'abnormal exit')

    def test_rebase_format_failure_starts_fresh_turn_until_no_progress_limit(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='same-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, 'invalid format', session_id='same-session',
        )
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError(
            'invalid format'
        )

        outcomes = [self.handler.run(self.context()) for _ in range(3)]

        self.assertEqual([outcome.kind for outcome in outcomes], [
            ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.RETRY,
            ExecutionOutcomeKind.FINAL_BLOCK,
        ])
        self.assertEqual(self.effects.agent.run_agent.call_count, 3)
        self.assertEqual(self.handler.rebase_corrections.get('repo', 'G-047').occurrence, 3)

    def test_owned_pr_conflict_uses_same_correction_history_without_publication(self):
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='pr-worker-session',
            pr_conflict_recovery=1, pr_conflict_base_oid='base',
            pr_conflict_head_oid='before',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'PR conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'abnormal exit', session_id='pr-worker-session',
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        saved = self.handler.rebase_corrections.get('repo', 'G-047')
        self.assertEqual((saved.target_base, saved.before_head), ('base', 'before'))
        self.assertEqual(saved.worker_session, 'pr-worker-session')
        self.effects.publication.push_owned_pr_conflict_recovery.assert_not_called()

    def test_rebase_stage_omits_ignored_cache_from_accepted_snapshot(self):
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={
                'app.py': (('unstaged',), 'accepted-content'),
                'cache/ignored.bin': (('untracked',), 'preserved-cache'),
            },
        )

        stage = self.handler._stage_rebase_correction(self.context('WORKING'))

        self.assertEqual(stage.returncode, 0)
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1] == 'add']
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].args[1:], ('add', '-A', '--', 'app.py'))

    def test_rebase_stage_evidence_records_index_entries(self):
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('unstaged',), 'accepted-content')},
            index={'app.py': ('H 100644 ' + 'a' * 40,)},
        )

        self.assertEqual(self.handler._rebase_stage_evidence(self.context('WORKING')), {
            'head': 'owned-head', 'branch': 'loop-manager/G-047',
            'files': {'app.py': 'accepted-content'},
            'index': {'app.py': ['H 100644 ' + 'a' * 40]},
        })

    def test_partial_stage_resume_only_stages_remaining_dirty_path(self):
        self._assert_partial_stage_resume(changed=False)

    def test_partial_stage_resume_rejects_replaced_clean_resolution(self):
        self._assert_partial_stage_resume(changed=True)

    def _assert_partial_stage_resume(self, *, changed):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None,
            snapshot={'stage_evidence': {
                'head': 'owned-head', 'branch': 'loop-manager/G-047',
                'files': {'app.py': 'clean-content', 'other.py': 'remaining-content'},
            }},
        )
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'other.py': (('unstaged',), 'remaining-content')},
        )

        def partially_staged_git(path, *args, **kwargs):
            if args == ('ls-files', '--cached', '-z'):
                return SimpleNamespace(stdout='app.py\0other.py\0', returncode=0)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = partially_staged_git
        self.effects.agent.path_signature.return_value = (
            'head-content' if changed else 'clean-content'
        )
        if changed:
            with self.assertRaises(handlers.RecoveryFailure):
                self.handler._stage_rebase_correction(self.context('WORKING'))
            self.assertFalse(any(call.args[1] == 'add'
                                 for call in self.effects.workspace.git.call_args_list))
            return
        stage = self.handler._stage_rebase_correction(self.context('WORKING'))

        self.assertEqual(stage.returncode, 0)
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1] == 'add']
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].args[1:], ('add', '-A', '--', 'other.py'))

    def test_owned_pr_correction_stages_only_publishable_resolution(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='pr-worker-session',
            pr_conflict_recovery=1, pr_conflict_base_oid='base',
            pr_conflict_head_oid='before',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'PR conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, READY_WORKER_RESULT, session_id='pr-worker-session',
        )
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={
                'app.py': (('unstaged',), 'resolved-content'),
                'cache/ignored.bin': (('untracked',), 'preserved-cache'),
            },
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.context.phase, ExecutionPhase.IMPLEMENTING)
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1] == 'add']
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].args[1:], ('add', '-A', '--', 'app.py'))
        self.assertEqual(self.state.row()['worker_session_id'], 'pr-worker-session')
        self.effects.publication.push_owned_pr_conflict_recovery.assert_not_called()

    def test_rebase_worker_result_format_error_stops_run_for_same_session_retry(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='worker-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError(
            'missing final fields'
        )

        result = self.handler.run(self.context())

        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.WORKER_CORRECTION)
        self.assertEqual(lm._execution_outcome_step_result(result), 1)
        self.assertEqual(self.state.row()['worker_session_id'], 'session')
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')

    def test_rebase_correction_blocks_only_after_three_identical_worker_results(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='same-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        context = self.context()

        first = self.handler._rebase_conflict(context, 'conflict')
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        for _ in range(2):
            result = self.handler._rebase_conflict(
                context, 'conflict', corrected=True, worker_result='same output',
            )
            self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
            self.assertEqual(self.repository.get('repo', 'G-047').status, 'IMPLEMENTING')
            self.effects.blocking.block_active_execplan.assert_not_called()
        final = self.handler._rebase_conflict(
            context, 'conflict', corrected=True, worker_result='same output',
        )
        self.assertEqual(final.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIn('same output', final.detail)
        self.assertEqual(self.handler.rebase_corrections.get('repo', 'G-047').occurrence, 3)

    def test_format_error_then_three_abnormal_results_reach_safety_gate(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='same-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        context = self.context()

        self.handler._rebase_conflict(context, 'conflict')
        invalid = self.handler._rebase_conflict(
            context, 'invalid format', corrected=True,
            worker_result='invalid worker result',
            recovery_reason=handlers.RecoveryReason.WORKER_CORRECTION,
        )
        self.assertEqual(invalid.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.handler.rebase_corrections.get('repo', 'G-047').occurrence, 1)

        for _ in range(2):
            retry = self.handler._rebase_conflict(
                context, 'abnormal exit', corrected=True,
                worker_result='same abnormal result', abnormal_result=True,
            )
            self.assertEqual(retry.kind, ExecutionOutcomeKind.RETRY)
            self.effects.blocking.block_active_execplan.assert_not_called()
        final = self.handler._rebase_conflict(
            context, 'abnormal exit', corrected=True,
            worker_result='same abnormal result', abnormal_result=True,
        )
        self.assertEqual(final.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.handler.rebase_corrections.get('repo', 'G-047').occurrence, 4)

    def test_saved_worker_result_finishes_after_restart_without_second_agent_turn(self):
        legacy_result = (
            'COMMIT_TITLE: Preserve resolved rebase changes\n'
            'PR_SUMMARY: Resolves the saved rebase conflict.\n'
            'TARGETED_TESTS:\ntargeted test: PASS'
        )
        context = self._save_rebase_worker_result(legacy_result, outcome_contract_version=0)

        resumed = self.handler.run(context)

        self.assertEqual(resumed.context.phase, ExecutionPhase.IMPLEMENTING)
        self.effects.agent.run_agent.assert_not_called()
        self.assertIsNone(self.state.row()['worker_result'])

        self.effects.validation.parse_worker_result.assert_any_call(legacy_result, self.plan)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_saved_new_rebase_result_requires_outcome_before_finishing(self):
        message = (
            '사용자 승인을 기다려 구현을 중단했습니다.\n'
            'COMMIT_TITLE: Preserve resolved rebase changes\n'
            'PR_SUMMARY: Waiting for approval.\n'
            'TARGETED_TESTS:\ntargeted test: PASS'
        )
        context = self._save_rebase_worker_result(message)
        # Reconstruct the repository as after a Manager restart.
        self.handler.rebase_corrections = type(self.handler.rebase_corrections)(self.connection)
        with mock.patch.object(self.handler, '_finish_worker') as finish:
            resumed = self.handler.run(context)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.RETRY)
        self.assertIn('WORKER_OUTCOME', self.state.row()['last_error'])
        finish.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.parse_worker_result.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.assertNotIn('add', self.calls)

    def _save_rebase_worker_result(self, message, *, outcome_contract_version=1):
        self.effects.validation.parse_worker_result.side_effect = lm.parse_worker_result
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='same-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        context = self.context()
        observation = self.handler._rebase_observation(context)
        stored = self.handler.rebase_corrections.observe('repo', 'G-047', observation)
        turn = self.handler.rebase_corrections.begin_turn(
            'repo', 'G-047', expected_generation=stored.generation,
            session='same-session',
        )
        self.handler.rebase_corrections.finish_turn(
            'repo', 'G-047', expected_generation=turn.generation,
            turn=turn.pending_turn, session='same-session',
            result=message,
            scope_marker=observation.progress,
            outcome_contract_version=outcome_contract_version,
        )

        return context

    def test_changed_pending_turn_without_scope_baseline_blocks_before_agent(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='same-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git
        context = self.context()
        observation = self.handler._rebase_observation(context)
        stored = self.handler.rebase_corrections.observe('repo', 'G-047', observation)
        self.handler.rebase_corrections.begin_turn(
            'repo', 'G-047', expected_generation=stored.generation,
            session='same-session',
        )
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('unstaged',), 'changed-content')},
        )

        result = self.handler.run(context)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.agent.run_agent.assert_not_called()

    def test_zero_exit_rebase_with_unmerged_index_keeps_conflict_correction(self):
        checkpoint = mock.Mock()
        checkpoint.get.return_value = CheckpointRecord(
            generation=1, operation='rebase', expected_head='before',
            target_base_oid='new-base', snapshot={'staged_paths': ['app.py']},
        )
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        original_git = self.git

        def conflicted_git(path, *args, **kwargs):
            if args[:2] == ('ls-files', '-u'):
                return SimpleNamespace(
                    stdout='100644 ' + 'a' * 40 + ' 1\tapp.py\0',
                    returncode=0,
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = conflicted_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        context = self.context('VERIFYING')
        before = self.state.row()['active_sha']

        result = self.handler._post_rebase_observation(
            context, 'new-base', 'Created autostash: deadbee\nconflicts',
        )

        self.assertEqual(result.recovery.observation.reason.value, 'rebase_conflict')
        self.assertEqual(self.state.row()['active_sha'], before)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        saved = checkpoint.record.call_args.kwargs['snapshot']
        self.assertIn('100644 ' + 'a' * 40, saved['unmerged_index'])

    def test_publication_intent_records_expected_head_and_completes(self):
        checkpoint = mock.Mock()
        checkpoint.get.side_effect = [
            CheckpointRecord(generation=1),
            CheckpointRecord(generation=2, operation='push',
                             expected_head='base', target_base_oid='base',
                             observed_remote_oid='old'),
        ]
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        context = self.context('PUBLISHING')

        self.handler._publication_intent(context, 'push', 'old')
        self.assertEqual(checkpoint.record.call_args_list[0].kwargs['operation'], 'push')
        self.assertEqual(checkpoint.record.call_args_list[0].kwargs['expected_head'], 'base')
        self.handler._complete_publication_intent(context)
        self.assertIsNone(checkpoint.record.call_args_list[1].kwargs['operation'])

    def test_preparation_recovery_corrects_owned_stale_markers_before_verify(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='PUBLISHING', active_sha='old-base',
            validated_base_sha='old-base', worker_session_id='worker-session',
            verify_result='PASS', review_result='PASS',
        )
        self.remote = 'new-base'

        def observed_git(path, *args, **kwargs):
            if args[0] == 'symbolic-ref':
                return SimpleNamespace(stdout='loop-manager/G-047\n', returncode=0)
            if args[0] == 'ls-files':
                return SimpleNamespace(stdout='', returncode=0)
            if args[:2] == ('diff', '--cached'):
                return SimpleNamespace(stdout='', returncode=0)
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='', returncode=0)
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='owned-head\n', returncode=0)
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        context = self.context()
        recovered = self.handler.recover(context)

        self.assertEqual(recovered.phase, ExecutionPhase.VERIFYING)
        row = self.state.row()
        self.assertEqual(row['active_sha'], 'new-base')
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.effects.validation.verify.assert_not_called()

    def test_preparation_leaves_new_rebase_to_verify_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result='saved worker output',
            worker_session_id='worker-session',
        )
        self.remote = 'new-base'

        def stale_base_git(path, *args, **kwargs):
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='', returncode=1)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = stale_base_git
        recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_result'], 'saved worker output')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertEqual(self.state.row()['active_sha'], 'base')
        self.assertNotIn('rebase', [call.args[1] for call in self.effects.workspace.git.call_args_list])
        self.effects.publication.pr_for_branch.assert_not_called()

    def test_preparation_preserves_existing_rebase_correction_diagnostic(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        message = lm.REBASE_CONFLICT_PREFIX + 'Worker 결과 검증 실패: actionable output'
        self.state.update_project(
            'repo', phase='WORKING', last_error=message,
            worker_session_id='worker-session',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertTrue(recovered.resolving_rebase)
        self.assertEqual(self.state.row()['last_error'], message)
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')

    def test_review_failure_checkpoint_resumes_worker_without_pr_lookup(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', active_sha='base',
            validated_base_sha=None, worker_session_id='worker-session',
            worker_result='previous worker result',
            review_result='VERDICT: FAIL', last_error='Review 실패: fix issue',
        )
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(self.state.row()['last_error'], 'Review 실패: fix issue')
        self.assertEqual(self.state.row()['worker_result'], 'previous worker result')
        self.effects.publication.pr_for_branch.assert_not_called()

    def test_new_activation_drops_old_execution_head_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.handler.newly_activated = True
        checkpoint.record(
            'repo', 'G-047', expected_generation=0,
            operation=None, expected_head='old-execution-head',
            target_base_oid='old-base', observed_remote_oid=None,
            snapshot={'old_phase': 'PUBLISHING'},
        )
        self.effects.workspace.git.side_effect = self.observed_git

        recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.expected_head, 'owned-head')
        self.assertEqual(saved.snapshot['reactivated_from_head'],
                         'old-execution-head')

    def test_new_activation_keeps_published_resume_lineage(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.handler.newly_activated = True
        marker = {
            'owner_oid': 'remote-head', 'before_head': 'old-execution-head',
            'after_head': 'owned-head', 'target_base_oid': 'base',
        }
        checkpoint.record(
            'repo', 'G-047', expected_generation=0,
            operation=None, expected_head='owned-head',
            target_base_oid='base', observed_remote_oid='remote-head',
            snapshot={'blocked_resume': marker},
        )
        self.effects.workspace.git.side_effect = self.observed_git
        with mock.patch.object(self.handler, '_owned_checkpoint_remote',
                               return_value=True):
            recovered = self.handler.recover(self.context())

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['blocked_resume'], marker)
        self.assertEqual(saved.snapshot['reactivated_from_head'], 'owned-head')

    def test_confirmed_checkpoint_push_remains_owned_during_pr_creation(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.handler.effects = replace(self.effects, checkpoint=checkpoint)
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', 'evidence', "
            "'loop-manager/G-047', '/state/G-047', ?)",
            ('a' * 40,),
        )
        marker = {'owner_oid': 'a' * 40, 'before_head': 'before',
                  'after_head': 'after', 'target_base_oid': 'base'}
        row = {'pr_url': None, 'publication_remote_oid': 'b' * 40}
        current = SimpleNamespace(
            expected_head='b' * 40, operation='push',
            snapshot={'blocked_resume': marker, 'remote_before': 'a' * 40},
        )
        from managed_rebase_adoption import ManagedRebaseService
        from tests.test_managed_rebase_validation import expectation, observation, scope_for
        def current_service(context, **options):
            expected = expectation(repo='repo', worktree='/state/G-047',
                                   branch=context.branch, plan_id='G-047',
                                   plan_path=context.plan.path)
            expected = replace(expected, remote=replace(expected.remote,
                               repository='repo', branch=context.branch, oid=options['remote_oid']))
            service = ManagedRebaseService(expected, mock.Mock())
            return service
        self.handler._current_rebase_service = current_service
        def current_observation(expected, _):
            actual = observation(expected, head='b' * 40, branch_ref=('b' * 40, ''),
                registration=('HEAD=' + 'b' * 40, 'branch=refs/heads/' + expected.branch))
            return actual, scope_for(expected, actual)
        def git(_repo, *args, **_kwargs):
            self.assertEqual(args[0], 'ls-remote')
            return SimpleNamespace(
                returncode=0,
                stdout='b' * 40 + '\trefs/heads/loop-manager/G-047\n',
            )
        self.effects.workspace.git.side_effect = git
        with (
            mock.patch('managed_rebase_adoption.observe_current_rebase',
                       side_effect=current_observation),
            mock.patch.object(handlers.checkpoint_ownership,
                              'rebase_preserves_commits', return_value=True),
            mock.patch.object(handlers.checkpoint_ownership,
                              'owned_remote_oid', return_value='a' * 40),
        ):
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
            ))
            row['publication_remote_oid'] = 'a' * 40
            with mock.patch.object(self.effects.workspace.git, 'side_effect', new=lambda _repo, *args, **kw: (
                SimpleNamespace(stdout='b' * 40 + '\n', returncode=0)
                if args[0] == 'rev-parse' else git(_repo, *args, **kw)
            )):
                self.assertTrue(self.handler._owned_checkpoint_remote(
                    self.context(), row, 'b' * 40, current,
                ))
                current.snapshot['remote_before'] = 'external'
                self.assertFalse(self.handler._owned_checkpoint_remote(
                    self.context(), row, 'b' * 40, current,
                ))
                current.snapshot['remote_before'] = 'a' * 40
                current.expected_head = 'external'
                self.assertFalse(self.handler._owned_checkpoint_remote(
                    self.context(), row, 'b' * 40, current,
                ))
                current.expected_head = 'b' * 40
            row['publication_remote_oid'] = 'b' * 40
            current.operation = None
            current.snapshot['pushed_checkpoint_head'] = 'b' * 40
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
            ))
            self.assertFalse(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
                {'headRefOid': 'b' * 40, 'url': 'new-pr'},
            ))
            current.operation = 'pr_create'
            current.snapshot['remote_before'] = 'b' * 40
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
                {'headRefOid': 'b' * 40, 'url': 'new-pr'},
            ))

    def test_interrupted_owned_push_persists_oid_before_clearing_intent(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', 'evidence', "
            "'loop-manager/G-047', '/state/G-047', 'old-head')"
        )
        marker = {'owner_oid': 'old-head', 'before_head': 'old-head',
                  'after_head': 'owned-head', 'target_base_oid': 'base'}
        self.state.update_project('repo', publication_remote_oid='old-head')
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='push',
            expected_head='owned-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={'blocked_resume': marker, 'remote_before': 'old-head'},
        )
        def observed_git(path, *args, **kwargs):
            if args[0] == 'ls-remote':
                return SimpleNamespace(
                    stdout='owned-head\trefs/heads/loop-manager/G-047\n',
                    returncode=0,
                )
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'owned-head'
        self.effects.publication.pr_for_branch.return_value = None
        with (
            mock.patch.object(handlers.checkpoint_ownership,
                              'rebase_preserves_commits', return_value=True),
            mock.patch.object(handlers.checkpoint_ownership,
                              'owned_remote_oid', return_value='old-head'),
        ):
            self.handler.recover(self.context())
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'owned-head')

    def test_interrupted_rebased_checkpoint_push_updates_projection_before_intent(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', 'evidence', "
            "'loop-manager/G-047', '/state/G-047', 'old-head')"
        )
        self.state.update_project('repo', publication_remote_oid='old-head',
                                  worker_session_id='worker-session')
        proof = {'generation': 1, 'remote_oid': 'old-head',
                 'after_head': 'rebased-head'}
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='old-head', target_base_oid='base',
            observed_remote_oid='old-head', snapshot={},
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=1, operation=None,
            expected_head='rebased-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={'completed_rebase': proof},
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=2, operation='push',
            expected_head='owned-head', target_base_oid='base',
            observed_remote_oid='old-head',
            snapshot={'completed_rebase': proof, 'after_head': 'owned-head',
                      'remote_before': 'old-head'},
        )
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'owned-head'
        self.effects.publication.pr_for_branch.return_value = None
        original_record = checkpoint.record
        def record_after_projection(*args, **kwargs):
            if (kwargs.get('operation') is None
                    and kwargs.get('observed_remote_oid') == 'owned-head'):
                self.assertEqual(
                    self.state.row()['publication_remote_oid'], 'owned-head',
                )
            return original_record(*args, **kwargs)
        with (mock.patch.object(self.handler, '_owned_checkpoint_remote',
                                return_value=True),
              mock.patch.object(checkpoint, 'record',
                                side_effect=record_after_projection)):
            self.handler.recover(self.context())

        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.observed_remote_oid, 'owned-head')
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'owned-head')

    def test_completed_owned_push_persists_new_oid_in_execution_checkpoint(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        marker = {'owner_oid': 'a' * 40, 'before_head': 'before',
                  'after_head': 'after', 'target_base_oid': 'base'}
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='push',
            expected_head='b' * 40, target_base_oid='base',
            observed_remote_oid='a' * 40,
            snapshot={'blocked_resume': marker, 'remote_before': 'a' * 40},
        )

        self.handler._complete_publication_intent(self.context('PUBLISHING'))

        saved = checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.observed_remote_oid, 'b' * 40)
        self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'b' * 40)

    def test_accepted_user_correction_requires_checkpoint_remote_ownership(self):
        blocked_checkpoint_state.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', "
            "'evidence', 'user-head')"
        )
        self.effects.workspace.git.side_effect = lambda *_args, **_kwargs: SimpleNamespace(
            stdout='user-head\n', returncode=0,
        )
        current = SimpleNamespace(operation=None, snapshot=None)
        row = {'pr_url': None, 'publication_remote_oid': None}

        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid',
                               return_value='user-head') as owner:
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'user-head', current,
            ))
            owner.assert_called_once()
            owner.return_value = None
            self.assertFalse(self.handler._owned_checkpoint_remote(
                self.context(), row, 'user-head', current,
            ))
        self.connection.execute(
            "UPDATE blocked_checkpoints SET accepted_remote_oid = 'old-head' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.assertFalse(self.handler._owned_checkpoint_remote(
            self.context(), row, 'user-head', current,
        ))

    def test_completed_published_rebase_keeps_remote_lease_only_with_sealed_owner(self):
        case = current_rebase_case(self)
        context = case.fixture.context()
        checkpoint = {'accepted_remote_oid': 'remote'}
        row = dict(case.state.row(), publication_remote_oid='remote')
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid', return_value='remote'), \
             mock.patch.object(handlers.manager_git_journal, 'verified_managed_transitions',
                               side_effect=AssertionError('no replay gate')):
            self.assertTrue(case.handler._owned_persisted_rebase(context, row, 'remote',
                            case.checkpoint.get('repo', 'G-047'), checkpoint, None))
            case.state.update_project('repo', worker_session_id='replacement-session')
            self.assertTrue(case.handler._owned_persisted_rebase(case.handler._context(), row, 'remote',
                            case.checkpoint.get('repo', 'G-047'), checkpoint, None))
            original = case.observed
            for change in ({'remote': replace(original.remote, oid='foreign')},
                           {'base_is_ancestor': False}, {'branch_ref': ('foreign', '')},
                           {'operations': (('MERGE_HEAD', 'foreign'),)}):
                with self.subTest(change=change):
                    case.observed = replace(original, **change)
                    self.assertFalse(case.handler._owned_persisted_rebase(context, row, 'remote',
                                     case.checkpoint.get('repo', 'G-047'), checkpoint, None))
            case.observed = original
            candidate = case.checkpoint.record('repo', 'G-047', expected_generation=1, operation='push',
                expected_head='candidate', target_base_oid='base', observed_remote_oid='remote',
                snapshot={'remote_before': 'remote'})
            case.observed = replace(original, head='candidate', branch_ref=('candidate', ''),
                registration=('HEAD=candidate', 'branch=refs/heads/loop-manager/G-047'),
                remote=replace(original.remote, oid='candidate'))
            self.assertTrue(case.handler._owned_persisted_rebase(context, row, 'candidate', candidate, checkpoint, None))
            self.assertFalse(case.handler._owned_persisted_rebase(context, row, 'foreign', candidate, checkpoint, None))
            self.assertFalse(case.handler._owned_persisted_rebase(context, row, 'candidate', candidate,
                                                                checkpoint, {'headRefOid': 'foreign'}))

    def test_published_rebase_completion_accepts_current_ref_without_remote_ancestry(self):
        self._published_rebase_completion(legacy=False)

    def test_legacy_published_rebase_completion_accepts_current_ref_without_remote_ancestry(self):
        self._published_rebase_completion(legacy=True)

    def _published_rebase_completion(self, *, legacy):
        case = current_rebase_case(self)
        old = case.checkpoint.get('repo', 'G-047')
        snapshot = dict(old.snapshot)
        if not legacy:
            snapshot.update(rebase_evidence_version=2, rebase_run_id='old-run', rebase_source_commits=[])
        case.checkpoint.record('repo', 'G-047', expected_generation=old.generation,
            operation='rebase', expected_head=old.expected_head, target_base_oid=old.target_base_oid,
            observed_remote_oid=old.observed_remote_oid, snapshot=snapshot)
        case.fixture.connection.execute(
            "INSERT INTO blocked_checkpoints (repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) VALUES ('repo', 'G-047', 'request', 'published', "
            "'review', 'audit', 'loop-manager/G-047', '/state/G-047', 'remote')")
        case.state.update_project('repo', publication_remote_oid='remote')
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid', return_value='remote'), \
             mock.patch.object(handlers.manager_git_journal, 'verified_managed_transitions',
                               side_effect=AssertionError('legacy audit is not a gate')):
            case.handler._complete_rebase_intent(case.fixture.context())
            saved = case.checkpoint.get('repo', 'G-047')
            self.assertIsNone(saved.operation)
            self.assertIsNotNone(saved.current_rebase_acceptance())
            self.assertIsNone(saved.completed_rebase())
            self.assertEqual(saved.snapshot['stage_evidence'], old.snapshot['stage_evidence'])
            self.assertEqual(case.state.row()['phase'], 'WORKING')
            self.assertIsNone(case.state.row()['worker_result'])
            self.assertTrue(case.handler._owned_checkpoint_remote(case.handler._context(), case.state.row(), 'remote', saved))
            case.observed = replace(case.observed, remote=replace(case.expected.remote, oid='external'))
            self.assertFalse(case.handler._owned_checkpoint_remote(case.handler._context(), case.state.row(), 'remote', saved))

    def test_autostash_published_completion_keeps_start_session_after_worker_replacement(self):
        from managed_rebase_validation import FileChange
        case = current_rebase_case(self)
        case.expected = replace(case.expected, autostash_oid='stash')
        case.state.update_project('repo', worker_session_id='replacement-session')
        case.observed = replace(case.observed, index_clean=False,
            staged=(FileChange('app.py', '100644', '100644', 'old', 'approved', 'M'),))
        original = case.checkpoint.get('repo', 'G-047')
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            case.handler._complete_rebase_intent(case.fixture.context())
        self.assertEqual(case.checkpoint.get('repo', 'G-047'), original)
        case.observed = replace(case.observed, index_clean=True, staged=())
        case.handler._complete_rebase_intent(case.fixture.context())
        self.assertIsNone(case.checkpoint.get('repo', 'G-047').operation)
        self.assertEqual(case.state.row()['worker_session_id'], 'replacement-session')
        self.assertIsNone(case.state.row()['worker_result'])

    def test_external_remote_checkpoint_uses_publication_ownership_cause(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.workspace.remote_branch_oid.return_value = 'external-head'

        result = self.handler.recover(self.context('PUBLISHING'))

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.REMOTE_OID_UNCERTAIN)
        self.assertIn('갱신을 거부', result.recovery.observation.detail)

    def test_preparation_resumes_durable_rebase_continue_without_worker(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', active_sha='old-base',
            validated_base_sha=None, worker_result='saved correction',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='old-head', target_base_oid='new-base',
            observed_remote_oid=None,
            snapshot={'pending': 'continue', 'worker_result': 'saved correction'},
        )
        self.remote = 'new-base'
        operation = {'rebase': True}

        def observed_git(path, *args, **kwargs):
            if args[0] == 'symbolic-ref':
                return SimpleNamespace(stdout='loop-manager/G-047\n', returncode=0)
            if args[0] == 'ls-files':
                return SimpleNamespace(stdout='', returncode=0)
            if args[:2] == ('diff', '--cached'):
                return SimpleNamespace(stdout='', returncode=0)
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='', returncode=0)
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='owned-head\n', returncode=0)
            if args[:2] == ('rebase', '--skip'):
                operation['rebase'] = False
                return SimpleNamespace(stdout='completed', returncode=0)
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace._rebase_in_progress_for_branch.side_effect = (
            lambda *_: operation['rebase']
        )
        context = self.context()

        recovered = self.handler.recover(context)

        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.effects.agent.run_agent.assert_not_called()

    def test_resolved_rebase_then_verify_failure_returns_to_same_worker(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', active_sha='base',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session', worker_result='old result',
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before-head', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.workspace.git.side_effect = self.observed_git

        first = self.handler.run(self.context())
        self.assertEqual(first.context.phase, ExecutionPhase.IMPLEMENTING)
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['stage_evidence'], {
            'head': 'owned-head', 'branch': 'loop-manager/G-047',
            'files': {'app.py': 'accepted-content'},
        })

        first = self.handler.run(first.context)
        self.assertEqual(first.context.phase, ExecutionPhase.VERIFYING)
        self.effects.validation.verify.side_effect = lambda _: (False, 'verify failure')

        def fail_verify(_repo, _worktree, _stage, output):
            self.state.update_project(
                'repo', phase='WORKING', validated_base_sha=None,
                verify_result=output, last_error='전체 검증 실패: ' + output,
            )
            return SimpleNamespace(blocked=False, fingerprint='verify-failure')

        self.effects.validation.transition_validation_failure.side_effect = fail_verify
        verified = self.handler.verify(first.context, self.handler)
        self.assertEqual(verified.kind, ExecutionOutcomeKind.RETRY)
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'stop after same-run verify recovery', session_id='worker-session',
        )

        recovered = self.handler.recover(self.handler._context())
        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        second = self.handler.run(recovered)

        self.assertEqual(second.kind, ExecutionOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(self.state.row()['last_error'],
                         'stop after same-run verify recovery')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')

    def test_staged_rebase_resume_reuses_saved_worker_result(self):
        self._assert_staged_rebase_resume(True)

    def test_staged_autostash_resume_reuses_saved_worker_result(self):
        self._assert_staged_rebase_resume(False)

    def test_completed_stage_matching_head_resumes_without_restaging(self):
        self._assert_staged_rebase_resume(True, clean_after_stage=True)

    def test_completed_autostash_stage_matching_head_resumes_without_rebase(self):
        self._assert_staged_rebase_resume(False, clean_after_stage=True)

    def test_completed_stage_accepts_replaced_clean_resolution_before_skip(self):
        self._assert_staged_rebase_resume(
            True, clean_after_stage=True, replaced_resolution=True,
        )

    def test_completed_autostash_stage_accepts_replaced_clean_resolution(self):
        self._assert_staged_rebase_resume(
            False, clean_after_stage=True, replaced_resolution=True,
        )

    def test_staged_rebase_resume_accepts_allowed_added_files(self):
        self._assert_staged_rebase_resume(True, changed=True)

    def test_pending_stage_resumes_accepted_unstaged_changes(self):
        self._assert_staged_rebase_resume(True, unstaged=True)

    def test_pending_stage_resumes_accepted_untracked_files(self):
        self._assert_staged_rebase_resume(True, untracked=True)

    def test_pending_stage_with_unmerged_index_retries_only_accepted_paths(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='saved-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None,
            snapshot={
                'pending': 'stage', 'worker_result': 'accepted correction',
                'stage_evidence': {
                    'head': 'owned-head', 'branch': 'loop-manager/G-047',
                    'files': {'app.py': 'accepted-content'},
                },
            },
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('unstaged',), 'accepted-content')},
        )
        unresolved = {'value': True}

        def observed_git(path, *args, **kwargs):
            if args[:2] == ('ls-files', '-u'):
                return SimpleNamespace(
                    stdout=('100644 ' + 'a' * 40 + ' 1\tapp.py\0'
                            if unresolved['value'] else ''), returncode=0,
                )
            if args[0] == 'add':
                unresolved['value'] = False
            if args == ('diff', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = observed_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        result = self.handler.recover(self.context())

        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        self.assertFalse(unresolved['value'])
        self.effects.agent.run_agent.assert_not_called()
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1] == 'add']
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0].args[1:], ('add', '-A', '--', 'app.py'))

    def test_stage_lock_resume_uses_checkpoint_without_new_worker_turn(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='saved-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        attempts = {'add': 0, 'unmerged': True}

        def locked_git(path, *args, **kwargs):
            if args[:2] == ('ls-files', '-u'):
                return SimpleNamespace(
                    stdout=('100644 ' + 'a' * 40 + ' 1\tapp.py\0'
                            if attempts['unmerged'] else ''), returncode=0,
                )
            if args[0] == 'add':
                attempts['add'] += 1
                if attempts['add'] == 1:
                    return SimpleNamespace(stdout='index.lock', returncode=1)
                attempts['unmerged'] = False
                return SimpleNamespace(stdout='', returncode=0)
            if args == ('diff', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = locked_git
        context = self.context()
        first = self.handler._finish_worker(context, self.state.row(), 'accepted correction')
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['pending'], 'stage')

        resumed = self.handler.recover(self.context())
        self.assertEqual(resumed.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(attempts['add'], 2)
        self.effects.agent.run_agent.assert_not_called()

    def test_continue_lock_resume_does_not_repeat_stage(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='saved-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        attempts = {'add': 0, 'continue': 0}

        def locked_git(path, *args, **kwargs):
            if args[0] == 'add':
                attempts['add'] += 1
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=1)
            if args[-2:] == ('rebase', '--continue'):
                attempts['continue'] += 1
                if attempts['continue'] == 1:
                    return SimpleNamespace(stdout='index.lock', returncode=1)
            return self.observed_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = locked_git
        context = self.context()
        first = self.handler._finish_worker(context, self.state.row(), 'accepted correction')
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['pending'], 'continue')

        def finished_git(path, *args, **kwargs):
            result = locked_git(path, *args, **kwargs)
            if args[-2:] == ('rebase', '--continue') and attempts['continue'] == 2:
                self.effects.workspace._rebase_in_progress_for_branch.return_value = False
            return result

        self.effects.workspace.git.side_effect = finished_git
        resumed = self.handler.recover(self.context())
        self.assertEqual(resumed.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(attempts, {'add': 1, 'continue': 2})
        self.effects.agent.run_agent.assert_not_called()

    def test_pending_stage_accepts_current_allowed_changes_before_staging(self):
        self._assert_staged_rebase_resume(True, changed=True, unstaged=True)

    def _assert_staged_rebase_resume(self, rebase_metadata, *, changed=False,
                                    unstaged=False, untracked=False,
                                    clean_after_stage=False, replaced_resolution=False):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint,
            workspace=replace(self.effects.workspace,
                              current_rebase_service=self.handler._current_rebase_service))
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='WORKING', worker_session_id='saved-session',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
        )
        accepted_files = {'app.py': 'accepted-content'}
        dirty = {'app.py': (('staged',), 'accepted-content')}
        if clean_after_stage:
            dirty = {}
            self.effects.agent.path_signature.return_value = (
                'head-content' if replaced_resolution else 'accepted-content'
            )
        if unstaged or untracked:
            accepted_files['allowed.py'] = 'accepted-other'
            dirty['allowed.py'] = (
                ('untracked',) if untracked else ('unstaged',), 'accepted-other',
            )
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047', dirty=dirty,
        )
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before-head', target_base_oid='base',
            observed_remote_oid=None,
            snapshot={'pending': 'stage', 'worker_result': 'saved correction',
                      **({'stash_oid': 'saved-stash-oid'} if not rebase_metadata else {}),
                      'stage_evidence': {'head': 'owned-head', 'branch': 'loop-manager/G-047',
                                         'files': accepted_files}},
        )
        if changed:
            self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
                head='owned-head', branch='loop-manager/G-047',
                dirty={**dirty,
                       'extra.py': (('untracked',), 'unexpected-content')},
            )
        def staged_git(path, *args, **kwargs):
            if args == ('diff', '--quiet'):
                return SimpleNamespace(stdout='', returncode=int(unstaged))
            if args == ('ls-files', '--cached', '-z'):
                return SimpleNamespace(
                    stdout='app.py\0allowed.py\0' if unstaged else 'app.py\0',
                    returncode=0,
                )
            if args == ('ls-files', '--others', '--exclude-standard', '-z'):
                return SimpleNamespace(
                    stdout=('allowed.py\0' if untracked else '') + ('extra.py\0' if changed else ''),
                    returncode=0)
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=int(not clean_after_stage))
            return self.observed_git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = staged_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = rebase_metadata
        result = self.handler.recover(self.context())
        self.effects.agent.run_agent.assert_not_called()
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1:3] == ('add', '-A')]
        self.assertEqual(len(additions), int(unstaged or untracked or changed))
        for call in additions:
            self.assertEqual(call.args[3], '--')
            self.assertEqual(set(call.args[4:]),
                             ({'allowed.py'} if unstaged or untracked else set())
                             | ({'extra.py'} if changed else set()))
        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.effects.agent.run_agent.assert_not_called()
        continuations = [call for call in self.effects.workspace.git.call_args_list
                         if '--continue' in call.args]
        self.assertEqual(len(continuations), int(rebase_metadata and not clean_after_stage))
        skips = [call for call in self.effects.workspace.git.call_args_list
                 if call.args[1:] == ('rebase', '--skip')]
        self.assertEqual(len(skips), int(rebase_metadata and clean_after_stage))

    def test_autostash_without_identity_blocks_after_command_and_on_resume(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before-head', target_base_oid='base',
            observed_remote_oid=None, snapshot={},
        )
        def conflicted_git(path, *args, **kwargs):
            if args[0] == 'ls-files':
                return SimpleNamespace(stdout='100644 blob 1\tapp.py\0', returncode=0)
            return self.observed_git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = conflicted_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        context = self.context('VERIFYING')
        for result in (
            self.handler._post_rebase_observation(context, 'base', 'completed'),
            self.handler.recover(context),
        ):
            self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
            self.assertEqual(result.detail, 'autostash_identity_unavailable')
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.assertEqual(checkpoint.get('repo', 'G-047').operation, 'rebase')
        self.assertEqual(self.effects.blocking.block_active_execplan.call_count, 2)
        for call in self.effects.blocking.block_active_execplan.call_args_list:
            self.assertEqual(call.kwargs['reason'], lm.BlockReasonCode.REPOSITORY_STATE_UNSAFE)

    def test_old_pr_head_after_rebase_cannot_attach_or_push(self):
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'existing', 'headRefOid': 'old',
            'headRefName': 'loop-manager/G-047', 'state': 'OPEN',
        }
        self.effects.workspace.remote_branch_oid.return_value = 'old'
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.reconcile_existing_pull_request.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()

    def test_post_push_pr_with_different_head_is_not_attached(self):
        self._assert_post_push_pr_attachment('other-head', False)

    def test_post_push_pr_with_verified_head_is_attached(self):
        self._assert_post_push_pr_attachment('base', True)

    def _assert_post_push_pr_attachment(self, pr_head, accepted, *,
                                       pr_state='OPEN', remote_deleted=False,
                                       reconciled_kind=ExecutionOutcomeKind.SUCCESS):
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.push_managed_publication_branch.side_effect = (
            lambda *_: self.calls.append('push') or 'base'
        )
        self.effects.workspace.remote_branch_oid.side_effect = (
            lambda *_: 'base' if 'push' in self.calls and not remote_deleted else None
        )
        self.effects.publication.pr_for_branch.side_effect = lambda *_: (
            {'url': 'existing', 'state': pr_state, 'headRefOid': pr_head,
             'headRefName': 'loop-manager/G-047'} if 'push' in self.calls else None
        )
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, reconciled_kind if accepted
                         else ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(self.effects.publication.reconcile_existing_pull_request.call_count,
                         int(accepted))
        self.effects.publication.push_managed_publication_branch.assert_called_once()
        self.effects.publication.command.assert_not_called()

    def test_checkpoint_fetch_lock_preserves_intent_and_worker_session(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project('repo', worker_session_id='worker-session')
        self.effects.workspace.git.side_effect = lm.GitLockError('index.lock')

        result = self.handler.recover(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'git_lock')
        self.assertTrue(checkpoint.get('repo', 'G-047').snapshot['fetch_in_progress'])
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_worktree_recovery_failure_keeps_durable_pending_boundary(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = RuntimeError('temporary worktree failure')
        self.state.update_project('repo', worker_session_id='worker-session')

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        saved = checkpoint.get('repo', 'G-047').snapshot
        self.assertTrue(saved['worktree_recovery_pending'])
        self.assertEqual(saved['branch'], 'loop-manager/G-047')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_commit_command_failure_preserves_expected_head_intent(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects

        def failing_git(path, *args, **kwargs):
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='before-head\n', returncode=0)
            if args == ('write-tree',):
                return SimpleNamespace(stdout='c' * 40 + '\n', returncode=0)
            if args[0] == 'commit':
                raise lm.GitLockError('index.lock')
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = failing_git
        with self.assertRaises(lm.GitLockError):
            self.handler._commit_owned(self.context('VERIFYING'), 'change')
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.operation, 'commit')
        self.assertEqual(saved.expected_head, 'before-head')
        self.assertEqual(saved.snapshot['commit_tree_oid'], 'c' * 40)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')

    def test_final_block_uses_current_verify_output_before_project_checkpoint(self):
        result = self.handler._block_final(
            'repo', Path('/state/G-047'), 'no publishable change',
            'verify: PASS', reason=lm.BlockReasonCode.PLAN_INPUT_INVALID,
            recovery_reason=handlers.RecoveryReason.NO_CHANGE,
        )
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.args[3],
            'verify: PASS',
        )

    def test_real_handlers_complete_each_phase_and_reject_replay(self):
        context = self.handler.guard(self.context())
        for method in ('run', 'verify', 'review', 'publish'):
            old = context
            call = getattr(self.handler, method)
            args = (old, self.handler) if method in {'verify', 'publish'} else (old,)
            result = call(*args)
            context = result.context
            before = list(self.calls)
            with self.assertRaises(runtime_state.RuntimeStateError):
                call(*args)
            self.assertEqual(self.calls, before)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.repository.get('repo', 'G-047').status, 'PR_OPEN')
        order = [c for c in self.calls if c in {'worker', 'verify', 'commit', 'reviewer', 'push', 'pr'}]
        self.assertEqual(order, ['worker', 'commit', 'verify', 'reviewer', 'push', 'pr'])
        self.assertEqual(self.calls.count('verify'), 1)

    def test_worker_and_reviewer_keep_phase_outcome_after_telemetry_error(self):
        def reported_agent(*args, **kwargs):
            self.calls.append(args[4])
            kwargs['on_session_started']('session')
            return lm.AgentResult(0, READY_WORKER_RESULT, session_id='session',
                                  collection_status='error')

        self.effects.agent.run_agent.side_effect = reported_agent
        worker = self.handler.run(self.handler.guard(self.context()))
        verified = self.handler.verify(worker.context, self.handler)
        reviewed = self.handler.review(verified.context)

        self.assertEqual(reviewed.context.phase, ExecutionPhase.PUBLISHING)
        calls = self.effects.agent.run_agent.call_args_list
        self.assertEqual([call.args[4] for call in calls], ['worker', 'reviewer'])
        for call in calls:
            request = call.kwargs['invocation']
            self.assertEqual((request.repo, request.plan_id, request.attempt),
                             ('repo', 'G-047', 1))
            self.assertNotIn('connection', call.kwargs)

    def test_revision_guard_tokens_follow_complete_phase_lifecycle(self):
        context = self.handler.guard(self.context())
        self.assertNotIsInstance(context, handlers.ExecutionOutcome)

        for method in ('run', 'verify', 'review', 'publish'):
            call = getattr(self.handler, method)
            args = (context, self.handler) if method in {'verify', 'publish'} else (context,)
            result = call(*args)
            context = result.context

        completed = self.repository.get('repo', 'G-047')
        self.assertEqual(completed.status, 'PR_OPEN')
        self.assertIsNotNone(completed.definition_snapshot)
        self.assertIsNotNone(completed.worker_input_token)
        self.assertIsNotNone(completed.archive_token)
        self.assertIsNotNone(completed.verify_token)
        self.assertIsNotNone(completed.review_token)
        self.assertIsNotNone(completed.publication_token)
        snapshot = DefinitionSnapshot.deserialize(completed.definition_snapshot)
        assert snapshot is not None
        self.assertEqual(completed.verify_token, snapshot.verify_token('base', 'base'))
        self.assertEqual(completed.review_token, snapshot.review_token('base', 'base'))

    def test_latest_base_uses_same_intent_and_rebase_for_both_checkpoints(self):
        for phase in ('VERIFYING', 'PUBLISHING'):
            with self.subTest(phase=phase):
                self.state.update_project('repo', phase=phase, active_sha='base', validated_base_sha='base',
                                          worker_result=VALID_WORKER_RESULT)
                self.remote = 'new-base'
                self.calls.clear()
                context = self.context()
                result = (self.handler.verify(context, self.handler) if phase == 'VERIFYING'
                          else self.handler.publish(context, self.handler))
                self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                self.assertEqual(self.state.row()['phase'], 'WORKING')
                self.assertIsNone(self.state.row()['worker_result'])
                self.assertIsNone(self.state.row()['review_result'])
                self.assertEqual(self.calls.count('rebase'), 1)
                self.assertEqual(self.calls.count('verify'), 0)
                self.assertEqual(self.state.row()['active_sha'], 'new-base')
                confirmed = self.handler.run(self.handler._context())
                self.assertEqual(confirmed.context.phase, ExecutionPhase.VERIFYING)
                verified = self.handler.verify(confirmed.context, self.handler)
                self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING)
                self.assertEqual(self.calls.count('verify'), 1)
                self.assertLess(self.calls.index('rebase'), self.calls.index('worker'))
                self.assertLess(self.calls.index('worker'), self.calls.index('verify'))

    def test_stale_callback_cannot_update_same_phase_new_snapshot(self):
        context = self.context()
        self.repository.update_project('repo', {'worker_result': 'newer observation'})
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.state.update_project('repo', worker_session_id='stale')
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.handler.run(context)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.repository.get('repo', 'G-047').worker_session_id)

    def test_source_revision_invalidates_artifacts_but_preserves_sessions_and_workspace(self):
        guarded = self.handler.guard(self.context())
        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        snapshot = DefinitionSnapshot.deserialize(
            self.state.row()['definition_snapshot']
        )
        assert snapshot is not None
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_session_id='worker-session',
            reviewer_session_id='reviewer-session',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            worker_input_token=snapshot.worker_token,
            archive_token=snapshot.archive_token,
            verify_token=snapshot.verify_token('base'),
            review_token=snapshot.review_token('base'),
            publication_token=snapshot.publication_token('base'),
        )
        self.effects.revision.source_fingerprint.return_value = 'changed-source'

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['reviewer_session_id'], 'reviewer-session')
        self.assertEqual(row['branch'], 'loop-manager/G-047')
        self.assertEqual(row['worktree'], '/state/G-047')

    def test_working_body_change_clears_retry_artifacts_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            worker_session_id='worker-session',
            worker_result='previous worker result',
            verify_result='failed verify result',
            review_result='previous review result',
            validated_base_sha='base',
        )
        self.effects.revision.body_hash.return_value = 'revised-body'

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, ExecutionOutcome)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertIsNone(row['validated_base_sha'])
        self.assertEqual(row['worker_session_id'], 'worker-session')

    def test_working_source_change_clears_retry_artifacts_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            worker_result='previous worker result',
            verify_result='failed verify result',
            worker_input_token='stale-worker-token',
        )
        self.effects.revision.source_fingerprint.return_value = 'revised-source'

        guarded = self.handler.guard(self.handler._context())

        self.assertNotIsInstance(guarded, ExecutionOutcome)
        row = self.state.row()
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['worker_input_token'])

    def _conflict_revision_setup(self):
        self.handler.guard(self.context())
        workspace_plan = self.handler._cached_plan(self.handler.catalog_plan)
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.validate_worker_workspace_scope
        )
        def resume_agent(*args, **kwargs):
            self.calls.append(args[4])
            session = kwargs['session_id'] or 'session'
            kwargs['on_session_started'](session)
            return lm.AgentResult(0, READY_WORKER_RESULT, session_id=session)
        self.effects.agent.run_agent.side_effect = resume_agent
        return workspace_plan

    def _dispatch_conflict_revision(self, workspace_plan):
        # Exercise the real workspace metadata comparison, faking only Git and
        # file parsing. A constant validator result would hide stale metadata.
        with mock.patch('loop_manager.capture_git_snapshot'), mock.patch(
            'loop_manager.validate_agent_scope'
        ), mock.patch('loop_manager.validate_managed_worktree'), mock.patch(
            'loop_manager.validate_execplan', return_value=workspace_plan
        ):
            return ExecutionPhaseCoordinator(self.handler).dispatch(
                self.handler._context()
            )

    def test_conflict_revision_dependency_merge_resumes_worker(self):
        workspace_plan = self._conflict_revision_setup()
        applied = self.state.row()['definition_snapshot']
        for plan_id, dependencies in (('G-048', '["G-050"]'), ('G-050', '[]')):
            self.connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "('repo', ?, ?, 'Dependency', '[\"docs/PRODUCT.md\"]', ?, "
                "'normal', 'hash', 'READY')",
                (plan_id, f'docs/plans/[{plan_id}] Dependency.md', dependencies),
            )
        self.connection.execute(
            "UPDATE exec_plans SET depends_on = '[\"G-048\"]' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        self.remote = 'new-base'
        self.effects.revision.unmet_dependencies.side_effect = (
            lambda repo, plan_id: lm.unmet_execplan_dependencies(
                self.connection, repo, plan_id
            )
        )

        outcome = ExecutionPhaseCoordinator(self.handler).dispatch(
            self.handler._context()
        )

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.STABLE_WAIT)
        self.assertIn('G-048', outcome.detail)
        self.assertIn('G-050', outcome.detail)
        self.effects.agent.run_agent.assert_not_called()
        self.assertNotIn('rebase', self.calls)
        row = self.state.row()
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['worker_result'], 'previous worker result')
        self.assertTrue(row['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
        self.connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = 'repo' AND plan_id IN ('G-048', 'G-050')"
        )
        self.connection.commit()
        resumed = self._dispatch_conflict_revision(workspace_plan)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertEqual(
            self.effects.agent.run_agent.call_args.kwargs['session_id'],
            'worker-session',
        )
        self.assertIsNone(self.effects.agent.worker_prompt.call_args.args[2])
        self.assertEqual(self.state.row()['definition_snapshot'], applied)
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertEqual(self.state.row()['branch'], 'loop-manager/G-047')
        self.assertEqual(self.state.row()['worktree'], '/state/G-047')
        self.effects.validation.verify.assert_not_called()
        latest_plan = self.handler._cached_plan(self.handler.catalog_plan)
        rerun = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(rerun.context.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        verified = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING)
        reviewed = self._dispatch_conflict_revision(latest_plan)
        self.assertEqual(reviewed.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(
            [call for call in self.calls if call in {'worker', 'verify', 'reviewer'}],
            ['worker', 'worker', 'verify', 'reviewer'],
        )

    def test_conflict_revision_priority_change_allows_old_workspace_metadata(self):
        workspace_plan = self._conflict_revision_setup()
        self.connection.execute(
            "UPDATE exec_plans SET priority = 'high' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        self.remote = 'new-base'

        outcome = self._dispatch_conflict_revision(workspace_plan)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertEqual(self.handler.plan['priority'], 'high')
        self.assertEqual(
            self.effects.agent.worker_prompt.call_args.args[2],
            'previous worker result',
        )
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['worker_result'])

    def _assert_conflict_profile_resume(self, old_profile, new_profile, effective):
        self.connection.execute(
            "UPDATE exec_plans SET current_complexity = ?, current_risk = ? "
            "WHERE repo = 'repo' AND plan_id = 'G-047'", old_profile,
        )
        self.connection.commit()
        workspace_plan = self._conflict_revision_setup()
        current_plan = dict(workspace_plan, complexity=new_profile[0], risk=new_profile[1])
        initial = lm.worker_level(current_plan)
        selected = max(initial, effective)
        history = '{"1":{"prior-review":2}}'
        # G-036 publishes the new profile and monotonic level in one update.
        self.connection.execute(
            "UPDATE exec_plans SET current_complexity = ?, current_risk = ?, "
            "initial_worker_level = ?, effective_worker_level = ?, "
            "review_level_history = ? WHERE repo = 'repo' AND plan_id = 'G-047'",
            (*new_profile, initial, selected, history),
        )
        self.connection.commit()
        self.remote = 'new-base'
        self.effects.validation.ensure_worker_level_state.side_effect = (
            lambda repo, plan: lm.ensure_worker_level_state(self.connection, repo, plan)
        )
        with mock.patch('loop_manager.validate_execplan', return_value=workspace_plan):
            with self.assertRaisesRegex(RuntimeError, 'metadata'):
                lm.validate_active_execplan_metadata(
                    Path('/repo'), Path('/state/G-047'),
                    self.state.row()['plan_path'], self.repository.active_row('repo'),
                )

        outcome = self._dispatch_conflict_revision(workspace_plan)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.blocking.block_active_execplan.assert_not_called()
        call = self.effects.agent.run_agent.call_args
        self.assertEqual(call.kwargs['session_id'], 'worker-session')
        self.assertEqual(call.kwargs['level'], selected)
        self.assertEqual(self.handler.plan['complexity'], new_profile[0])
        self.assertEqual(self.handler.plan['risk'], new_profile[1])
        self.assertEqual(self.effects.agent.worker_prompt.call_args.args[2],
                         'previous worker result')
        row = self.repository.active_row('repo')
        self.assertEqual(row['initial_worker_level'], initial)
        self.assertEqual(row['effective_worker_level'], selected)
        self.assertEqual(row['review_level_history'], history)
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        self.handler.guard(self.handler._context())
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['worker_result'])

    def test_conflict_profile_complexity_change_resumes_same_worker(self):
        self._assert_conflict_profile_resume(('low', 'low'), ('high', 'low'), 1)

    def test_conflict_profile_risk_change_resumes_same_worker(self):
        self._assert_conflict_profile_resume(('low', 'low'), ('low', 'critical'), 1)

    def test_conflict_profile_downgrade_preserves_escalated_level(self):
        self._assert_conflict_profile_resume(('high', 'critical'), ('low', 'low'), 8)

    def test_conflict_revision_scope_rejects_unapproved_metadata(self):
        workspace_plan = self._conflict_revision_setup()
        baseline = DefinitionSnapshot.deserialize(self.state.row()['definition_snapshot'])
        self.connection.execute(
            "UPDATE exec_plans SET priority = 'high', depends_on = '[\"G-048\"]', "
            "current_complexity = 'medium', current_risk = 'medium' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()
        catalog = self.repository.active_row('repo')
        for field, value in (
            ('priority', 'critical'), ('depends_on', ['G-099']),
            ('complexity', 'high'), ('risk', 'high'),
            ('title', 'Unauthorized'), ('sources', ['docs/OTHER.md']),
        ):
            with self.subTest(field=field), mock.patch(
                'loop_manager.capture_git_snapshot'
            ), mock.patch('loop_manager.validate_agent_scope'), mock.patch(
                'loop_manager.validate_managed_worktree'
            ), mock.patch('loop_manager.validate_execplan', return_value={
                **workspace_plan, field: value,
            }):
                with self.assertRaisesRegex(lm.ExecPlanMetadataError, 'metadata'):
                    lm.validate_worker_workspace_scope(
                        Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                        'main', catalog['path'], catalog, mock.Mock(),
                        allow_rebase=True, rebase_definition=baseline,
                    )
        with mock.patch('loop_manager.capture_git_snapshot'), mock.patch(
            'loop_manager.validate_agent_scope'
        ), mock.patch('loop_manager.validate_managed_worktree'), mock.patch(
            'loop_manager.validate_execplan', return_value=workspace_plan
        ) as parsed:
            with self.assertRaisesRegex(lm.ExecPlanMetadataError, 'metadata'):
                lm.validate_worker_workspace_scope(
                    Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                    'main', catalog['path'], catalog, mock.Mock(),
                )
            parsed.return_value = {
                **workspace_plan, 'priority': 'high', 'depends_on': ['G-048'],
                'complexity': 'medium', 'risk': 'medium',
            }
            current = lm.validate_worker_workspace_scope(
                Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                'main', catalog['path'], catalog, mock.Mock(),
                allow_rebase=True, rebase_definition=baseline,
            )
            self.assertEqual(current['priority'], 'high')
            self.assertEqual(current['depends_on'], ['G-048'])
            self.assertEqual(current['complexity'], 'medium')
            self.assertEqual(current['risk'], 'medium')
        with self.assertRaisesRegex(RuntimeError, '충돌 복구'):
            lm.validate_worker_workspace_scope(
                Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047',
                'main', catalog['path'], catalog, mock.Mock(),
                rebase_definition=baseline,
            )

    def test_conflicted_rebase_blocks_new_worker_input_before_worker(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.revision.body_hash.return_value = 'revised-body'

        outcome = ExecutionPhaseCoordinator(self.handler).dispatch(
            self.handler._context()
        )

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs['reason'],
            lm.BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
        )

    def test_conflict_resolution_rechecks_new_remote_base_before_verify(self):
        self.handler.guard(self.context())
        self.state.update_project(
            'repo',
            last_error=lm.REBASE_CONFLICT_PREFIX + 'conflict',
            worker_session_id='worker-session',
            worker_result='previous worker result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.remote = 'new-base'
        guarded = self.handler.guard(self.handler._context())
        self.assertNotIsInstance(guarded, ExecutionOutcome)

        resolved = self.handler._finish_worker(
            guarded, self.state.row(), 'resolved worker result'
        )

        self.assertEqual(resolved.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['active_sha'], 'base')
        self.dirty = False
        self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        refreshed = self.handler.guard(self.handler._context())
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertEqual(self.calls.count('rebase'), 2)

    def test_revision_guard_adopts_legacy_phase_artifacts_without_worker_replay(self):
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            definition_snapshot=None,
            worker_input_token=None,
            archive_token=None,
            verify_token=None,
            review_token=None,
            publication_token=None,
        )

        guarded = self.handler.guard(self.handler._context())

        self.assertEqual(guarded.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        snapshot = DefinitionSnapshot.deserialize(row['definition_snapshot'])
        assert snapshot is not None
        self.assertEqual(row['phase'], 'VERIFYING')
        self.assertEqual(row['worker_result'], 'worker-result')
        self.assertEqual(row['worker_input_token'], snapshot.worker_token)
        self.assertEqual(row['archive_token'], snapshot.archive_token)
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertIsNone(row['verify_token'])
        self.assertIsNone(row['review_token'])
        self.assertEqual(row['phase'], 'VERIFYING')

    def test_revision_guard_retries_when_base_and_catalog_shas_disagree(self):
        self.effects.revision.reconcile_catalog.side_effect = None
        self.effects.revision.reconcile_catalog.return_value = SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha='other-base'
        )

        outcome = self.handler.guard(self.context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertIsNone(self.state.row()['definition_snapshot'])
        self.effects.revision.body_hash.assert_not_called()
        self.effects.revision.source_fingerprint.assert_not_called()

    def test_revision_guard_rejects_legacy_artifacts_after_source_change(self):
        self.state.update_project(
            'repo',
            phase='PUBLISHING',
            validated_base_sha='base',
            worker_result='worker-result',
            verify_result='verify-result',
            review_result='review-result',
            definition_snapshot=None,
            worker_input_token=None,
            archive_token=None,
            verify_token=None,
            review_token=None,
            publication_token=None,
        )
        self.remote = 'new-base'
        self.effects.revision.changed_paths.return_value = (
            'docs/PRODUCT.md',
        )

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertIsNone(row['worker_result'])
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])

    def test_successful_revision_refresh_clears_rebase_recovery_marker(self):
        guarded = self.handler.guard(self.context())
        self.assertNotIsInstance(guarded, handlers.ExecutionOutcome)
        self.remote = 'new-base'

        outcome = self.handler.guard(guarded)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertEqual(row['active_sha'], 'new-base')
        self.assertIsNone(row['revision_resume_status'])
        self.assertIsNone(row['last_error'])

    def test_revision_refresh_checkpoints_dirty_verified_worker_patch_before_rebase(self):
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result='worker-result'
        )
        self.remote = 'new-base'

        outcome = self.handler.guard(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertLess(self.calls.index('commit'), self.calls.index('rebase'))
        self.assertEqual(self.state.row()['active_sha'], 'new-base')
        self.assertIsNone(self.state.row()['worker_result'])

    def test_dependency_gate_reports_unmerged_direct_and_indirect_plans(self):
        for plan_id, dependencies in (
            ('G-035', '[]'),
            ('G-036', '["G-035"]'),
        ):
            self.connection.execute(
                "INSERT INTO exec_plans (repo, plan_id, path, title, sources, "
                "depends_on, priority, content_hash, status) VALUES "
                "('repo', ?, ?, ?, '[\"docs/PRODUCT.md\"]', ?, 'normal', "
                "'hash', 'READY')",
                (plan_id, f'docs/plans/[{plan_id}] Plan.md', 'Plan', dependencies),
            )
        self.connection.execute(
            "UPDATE exec_plans SET depends_on = '[\"G-036\"]' "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        self.connection.commit()

        self.assertEqual(
            lm.unmet_execplan_dependencies(
                self.connection, 'repo', 'G-047'
            ),
            ('G-035', 'G-036'),
        )
        self.connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' "
            "WHERE repo = 'repo' AND plan_id IN ('G-035', 'G-036')"
        )
        self.assertEqual(
            lm.unmet_execplan_dependencies(
                self.connection, 'repo', 'G-047'
            ),
            (),
        )

    def test_verify_interruption_resumes_without_duplicate_commit(self):
        context = self.context('VERIFYING')
        original = self.git
        def interrupted(path, *args, **kwargs):
            if args[0] == 'fetch':
                raise RuntimeError('interruption')
            return original(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = interrupted
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            self.handler.verify(context, self.handler)
        self.effects.validation.verify.assert_not_called()
        self.effects.workspace.git.side_effect = original
        self.handler.verify(self.context(), self.handler)
        self.assertEqual(self.calls.count('commit'), 1)
        self.assertEqual(self.calls.count('verify'), 1)

    def test_candidate_head_change_after_verify_requires_new_validation(self):
        self.handler.guard(self.context())
        worker = self.handler.run(self.handler._context())
        verified = self.handler.verify(worker.context, self.handler)
        self.assertEqual(verified.context.phase, ExecutionPhase.REVIEWING)
        previous_token = self.state.row()['verify_token']
        self.assertIsNotNone(previous_token)
        original = self.git

        def replaced_head(path, *args, **kwargs):
            if path == Path('/state/G-047') and args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='replacement-head\n', returncode=0)
            return original(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = replaced_head
        outcome = self.handler.guard(self.handler._context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertIsNone(self.state.row()['verify_token'])
        self.effects.agent.run_agent.assert_called_once()

    def test_review_entry_rechecks_verified_candidate_before_agent(self):
        self.handler.guard(self.context())
        worker = self.handler.run(self.handler._context())
        verified = self.handler.verify(worker.context, self.handler)
        original = self.git

        def replaced_head(path, *args, **kwargs):
            if path == Path('/state/G-047') and args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout='replacement-head\n', returncode=0)
            return original(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = replaced_head
        outcome = self.handler.review(verified.context)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.effects.agent.run_agent.assert_called_once()

    def test_review_entry_returns_invalid_worker_result_to_correction(self):
        self.handler.guard(self.context())
        worker = self.handler.run(self.handler._context())
        verified = self.handler.verify(worker.context, self.handler)
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError(
            'TARGETED_TESTS 형식이 유효하지 않습니다.',
        )

        outcome = self.handler.review(verified.context)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.effects.agent.run_agent.assert_called_once()

    def test_review_entry_accepts_empty_successful_verify_output(self):
        context = self.review_context()
        self.state.update_project('repo', verify_result='')

        outcome = self.handler.review(context)

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.agent.run_agent.assert_called_once()

    def test_head_change_during_verify_does_not_save_success(self):
        original = self.git
        changed = False

        def changing_git(path, *args, **kwargs):
            if path == Path('/state/G-047') and args == ('rev-parse', 'HEAD') and changed:
                return SimpleNamespace(stdout='new-head\n', returncode=0)
            return original(path, *args, **kwargs)

        def verify(_worktree):
            nonlocal changed
            changed = True
            return True, 'OK'

        self.effects.workspace.git.side_effect = changing_git
        self.effects.validation.verify.side_effect = verify
        outcome = self.handler.verify(self.context('VERIFYING'), self.handler)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertIsNone(self.state.row()['verify_result'])
        self.assertIsNone(self.state.row()['verify_token'])
        self.effects.validation.verify.assert_called_once()

    def test_verify_generated_allowed_change_is_committed_and_reverified(self):
        attempts = 0

        def verify(_worktree):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.dirty = True
            return True, 'OK'

        self.effects.validation.verify.side_effect = verify
        first = self.handler.verify(self.context('VERIFYING'), self.handler)
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.calls.count('commit'), 2)
        self.assertEqual(attempts, 1)
        self.assertIsNone(self.state.row()['verify_result'])
        self.effects.validation.clear_validation_failure_history.assert_not_called()
        second = self.handler.verify(self.handler._context(), self.handler)
        self.assertEqual(second.context.phase, ExecutionPhase.REVIEWING)
        self.assertEqual(attempts, 2)
        self.assertEqual(self.calls.count('commit'), 2)
        self.effects.validation.clear_validation_failure_history.assert_called_once_with(
            'repo', 'verify',
        )

    def test_unchanged_worker_result_blocks_before_full_verify(self):
        self.dirty = False
        self.effects.workspace.branch_has_changes.return_value = False
        outcome = self.handler.verify(self.context('VERIFYING'), self.handler)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.validation.verify.assert_not_called()
        self.assertNotIn('commit', self.calls)

    def test_rebase_discards_prior_candidate_evidence_before_verify(self):
        self.remote = 'new-base'
        self.state.update_project(
            'repo', phase='VERIFYING', worker_result='worker result',
            verify_result='old verify', review_result='old review',
            validated_base_sha='base', verify_token='old token',
            review_token='old review token',
        )
        outcome = self.handler.verify(self.handler._context(), self.handler)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        row = self.state.row()
        self.assertEqual(row['active_sha'], 'new-base')
        self.assertIsNone(row['verify_result'])
        self.assertIsNone(row['review_result'])
        self.assertIsNone(row['verify_token'])
        self.assertIsNone(row['review_token'])
        self.effects.validation.verify.assert_not_called()

    def test_protocol_correction_reuses_session_before_publication(self):
        self.effects.validation.parse_reviewer_result.side_effect = [
            lm.ReviewerProtocolError('malformed'), ('PASS', ())]
        result = self.handler.review(self.review_context())
        self.assertEqual(result.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(
            [call for call in self.calls if call == 'reviewer'],
            ['reviewer', 'reviewer'],
        )
        self.assertEqual(self.effects.agent.run_agent.call_args.kwargs['session_id'], 'session')

    def test_review_specification_deadlock_blocks_plan_without_worker_correction(self):
        from reviewer_specification import SpecificationDeadlock

        self.plan['sources'] = ['docs/requirements/source.md']
        details = SpecificationDeadlock(
            'missing', (('docs/PRODUCT.md', 'Review policy'),),
            'A retention decision is absent', 'Whether to retain the result',
            'How long should the result be retained?',
        )
        self.effects.validation.parse_reviewer_result.return_value = (
            'SPEC_BLOCKED', details,
        )
        result = self.handler.review(self.review_context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual([call for call in self.calls if call == 'reviewer'], ['reviewer'])
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs['reason'],
            lm.BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
        )
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs[
                'recovery_decision'
            ].observation.user_targets,
            ('docs/PRODUCT.md',),
        )
        self.effects.validation.transition_validation_failure.assert_not_called()

    def test_review_deadlock_accepts_architecture_outside_sources(self):
        from reviewer_specification import SpecificationDeadlock

        self.plan['sources'] = ['docs/requirements/source.md']
        details = SpecificationDeadlock(
            'missing', (('docs/ARCHITECTURE.md', 'Architecture Change Policy'),),
            'The migration choice is unspecified', 'Which migration to use',
            'Which migration policy should apply?',
        )
        self.effects.validation.parse_reviewer_result.return_value = (
            'SPEC_BLOCKED', details,
        )
        result = self.handler.review(self.review_context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs[
                'recovery_decision'
            ].observation.user_targets,
            ('docs/ARCHITECTURE.md',),
        )

    def test_review_deadlock_with_unrelated_path_uses_protocol_correction(self):
        from reviewer_specification import SpecificationDeadlock

        details = SpecificationDeadlock(
            'missing', (('docs/other.md', 'Policy'),),
            'Missing decision', 'Whether to retain', 'What is the rule?',
        )
        self.effects.validation.parse_reviewer_result.side_effect = [
            ('SPEC_BLOCKED', details), ('PASS', ()),
        ]
        result = self.handler.review(self.review_context())
        self.assertEqual(result.context.phase, ExecutionPhase.PUBLISHING)
        self.assertEqual(
            [call for call in self.calls if call == 'reviewer'],
            ['reviewer', 'reviewer'],
        )
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_archive_change_returns_to_worker_before_push(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.archive.requirement_archive_reconciliation_error.return_value = 'archive changed'
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertNotIn('push', self.calls)

    def test_guard_pins_archive_target_without_resetting_worker_session(self):
        source = 'docs/requirements/source.md'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        snapshot = ArchiveSnapshot('base', 'catalog', 'states', 'G-047', (), ())
        self.effects.archive.requirement_archive_snapshot.return_value = snapshot
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='base', returncode=0)
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git
        self.state.update_project('repo', worker_session_id='saved-session')

        result = self.handler.guard(self.context())

        self.assertEqual(result.archive_snapshot_token, snapshot.token)
        target = self.connection.execute(
            'SELECT snapshot_token, stage FROM requirement_archive_targets '
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(target['snapshot_token'], snapshot.token)
        self.assertEqual(target['stage'], 'WORKTREE_UPDATED')
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_guard_remote_race_preserves_worker_session_without_block(self):
        source = 'docs/requirements/source.md'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git
        fetches = 0

        def git(path, *args, **kwargs):
            nonlocal fetches
            if args[0] == 'fetch':
                fetches += 1
                if fetches == 2:
                    self.remote = 'base-2'
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='base', returncode=0)
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git
        self.state.update_project('repo', worker_session_id='saved-session')

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.BASE_BEHIND)
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_guard_persists_archive_target_before_latest_base_refresh(self):
        source = 'docs/requirements/source.md'
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        snapshot = ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        self.effects.archive.requirement_archive_snapshot.return_value = snapshot
        self.handler._refresh_revision_base = mock.Mock(
            return_value=ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.handler._refresh_revision_base.assert_called_once()
        target = self.connection.execute(
            'SELECT base_oid, snapshot_token, stage '
            "FROM requirement_archive_targets WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(tuple(target), ('base-2', snapshot.token, 'OBSERVED'))
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_guard_invalid_remote_catalog_uses_final_recovery_gate(self):
        path = 'docs/plans/[G-002] Invalid.md'
        self.effects.revision.reconcile_catalog.side_effect = None
        self.effects.revision.reconcile_catalog.return_value = SimpleNamespace(
            kind=SimpleNamespace(value='invalid-catalog'),
            revision_sha='base',
            diagnostic=SimpleNamespace(message='invalid source', path=path,
                                       source='docs/requirements/missing.md'),
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.CATALOG_INVALID)
        self.assertEqual(result.recovery.observation.user_targets,
                         (path, 'docs/requirements/missing.md'))
        self.effects.blocking.block_active_execplan.assert_called_once()
        self.effects.archive.requirement_archive_snapshot.assert_not_called()

    def test_guard_refuses_archive_rebase_when_head_lacks_plan_ancestry(self):
        source = 'docs/requirements/source.md'
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (source,)
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=1 if path == Path('/state/G-047') else 0
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.OWNERSHIP_UNCERTAIN)
        self.assertNotIn('rebase', self.calls)
        self.assertIsNone(self.connection.execute(
            "SELECT snapshot_token FROM requirement_archive_targets WHERE repo = 'repo'"
        ).fetchone())

    def test_guard_archive_rebase_checks_managed_head_not_base_checkout(self):
        self.remote = 'base-2'
        self.effects.archive.requirement_archive_sources.return_value = (
            'docs/requirements/source.md',
        )
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base-2', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=0 if path == Path('/state/G-047') else 1
                )
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git
        self.handler._refresh_revision_base = mock.Mock(
            return_value=ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        )

        result = self.handler.guard(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.handler._refresh_revision_base.assert_called_once()
        ancestry_paths = [
            call.args[0] for call in self.effects.workspace.git.call_args_list
            if call.args[1:3] == ('merge-base', '--is-ancestor')
        ]
        self.assertEqual(ancestry_paths, [Path('/state/G-047')])
        self.effects.workspace.validate_managed_worktree.assert_called_with(
            Path('/repo'), Path('/state/G-047'), 'loop-manager/G-047'
        )

    def test_guard_archive_snapshot_ignores_base_checkout_head(self):
        self.effects.archive.requirement_archive_sources.return_value = (
            'docs/requirements/source.md',
        )
        self.effects.archive.requirement_archive_touched_sources.return_value = ()
        self.effects.archive.requirement_archive_snapshot.return_value = (
            ArchiveSnapshot('base', 'catalog', 'states', 'G-047', (), ())
        )
        original_git = self.git

        def git(path, *args, **kwargs):
            if args[:2] == ('merge-base', '--is-ancestor'):
                return SimpleNamespace(
                    stdout='', returncode=0 if path == Path('/state/G-047') else 1
                )
            if args[0] == 'merge-base':
                return SimpleNamespace(stdout='base', returncode=0)
            return original_git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = git

        result = self.handler.guard(self.context())

        self.assertNotIsInstance(result, ExecutionOutcome)
        ancestry_paths = [
            call.args[0] for call in self.effects.workspace.git.call_args_list
            if call.args[1:3] == ('merge-base', '--is-ancestor')
        ]
        self.assertEqual(ancestry_paths, [Path('/state/G-047')])
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_existing_pr_recovery_does_not_push_or_create(self):
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'existing', 'headRefOid': 'base',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertTrue(self.calls)
        self.assertTrue(all(call == 'rev-parse' for call in self.calls))

    def test_worker_format_scope_and_guidance_stop_before_verify(self):
        scenarios = ('format', 'scope', 'guidance')
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.effects.validation.parse_worker_result.side_effect = None
                self.effects.agent.validate_worker_workspace_scope.side_effect = None
                self.effects.agent.inspect_worker_guidance_result.return_value = False
                self.state.update_project('repo', phase='WORKING')
                if scenario == 'format':
                    self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError('format')
                elif scenario == 'scope':
                    self.effects.agent.validate_worker_workspace_scope.side_effect = lm.AgentScopeError('scope')
                else:
                    self.effects.agent.inspect_worker_guidance_result.return_value = True
                result = self.handler.run(self.context())
                self.assertEqual(
                    result.kind,
                    ExecutionOutcomeKind.RETRY if scenario == 'format'
                    else ExecutionOutcomeKind.FINAL_BLOCK,
                )
                self.assertNotIn('verify', self.calls)

    def test_worker_metadata_mismatch_corrects_then_final_blocks(self):
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )

        outcomes = [self.handler.run(self.handler._context()) for _ in range(3)]

        self.assertEqual([outcome.kind for outcome in outcomes], [
            ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.RETRY,
            ExecutionOutcomeKind.FINAL_BLOCK,
        ])
        self.assertEqual([outcome.recovery.occurrence for outcome in outcomes], [1, 2, 3])
        self.assertEqual(outcomes[-1].recovery.block_reason, lm.BlockReasonCode.PLAN_INPUT_INVALID)
        self.assertEqual(self.state.row()['worker_session_id'], 'session')
        self.assertNotIn('verify', self.calls)
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_rebase_metadata_correction_preserves_resume_context(self):
        self.state.update_project(
            'repo', last_error=lm.REBASE_IN_PROGRESS_ERROR,
            worker_session_id='worker-session', worker_result='saved result',
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            0, READY_WORKER_RESULT, session_id='worker-session',
        )
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )

        for occurrence in (1, 2):
            context = self.handler._context()
            self.assertTrue(context.resolving_rebase)
            outcome = self.handler.run(context)
            self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
            self.assertEqual(outcome.recovery.occurrence, occurrence)
            self.assertEqual(outcome.recovery.observation.reason.value, 'worker_correction')
            self.assertTrue(self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
            self.assertIn('catalog metadata mismatch', self.state.row()['last_error'])
            self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
            self.assertEqual(self.state.row()['worker_result'], 'saved result')
            self.assertTrue(
                self.effects.agent.validate_worker_workspace_scope.call_args.kwargs['allow_rebase']
            )
            self.assertEqual(
                self.effects.agent.run_agent.call_args.kwargs['session_id'], 'worker-session',
            )

        self.assertTrue(self.handler._context().resolving_rebase)
        self.assertIn(
            'catalog metadata mismatch', self.effects.agent.worker_prompt.call_args.args[1],
        )
        self.effects.blocking.block_active_execplan.assert_not_called()
        self.assertNotIn('verify', self.calls)

    def test_verify_metadata_mismatch_returns_to_worker_with_output(self):
        self.effects.agent.validate_worker_workspace_scope.side_effect = (
            lm.ExecPlanMetadataError('catalog metadata mismatch')
        )
        self.state.update_project('repo', phase='VERIFYING', worker_result='saved worker result')

        outcome, output = self.handler._verify_once(self.handler._context())

        self.assertEqual(outcome.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(outcome.recovery.observation.reason.value, 'worker_correction')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['worker_result'], 'saved worker result')
        self.assertEqual(self.state.row()['verify_result'], output)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_repeated_worker_result_format_uses_same_session_before_final_gate(self):
        self.effects.validation.parse_worker_result.side_effect = lm.WorkerResultError('bad format')
        outcomes = [self.handler.run(self.context()) for _ in range(3)]

        self.assertEqual([item.kind for item in outcomes], [
            ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.RETRY,
            ExecutionOutcomeKind.FINAL_BLOCK,
        ])
        self.assertEqual([item.recovery.occurrence for item in outcomes], [1, 2, 3])
        self.assertEqual(self.state.row()['worker_session_id'], 'session')
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_rebase_worker_retry_prompt_uses_latest_failure(self):
        prior_error = lm.REBASE_CONFLICT_PREFIX + 'Worker 결과 검증 실패:\ninvalid result'
        self.state.update_project('repo', last_error=prior_error,
                                  pr_conflict_recovery=1, worker_session_id='worker-session')
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'still conflicted', session_id='worker-session')

        outcomes = [self.handler.run(self.handler._context()) for _ in range(2)]

        self.assertEqual([outcome.kind for outcome in outcomes], [
            ExecutionOutcomeKind.RETRY, ExecutionOutcomeKind.RETRY,
        ])
        prompts = [call.args[1] for call in self.effects.agent.worker_prompt.call_args_list]
        self.assertEqual(prompts, [
            prior_error, lm.REBASE_CONFLICT_PREFIX + 'still conflicted',
        ])
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_validation_retry_and_final_block_remain_distinct(self):
        for stage in ('verify', 'review'):
            for blocked in (False, True):
                with self.subTest(stage=stage, blocked=blocked):
                    self.effects.validation.transition_validation_failure.return_value = SimpleNamespace(blocked=blocked, fingerprint='diagnostics')
                    self.effects.validation.verify.side_effect = lambda _: (False, 'diagnostics')
                    self.effects.validation.parse_reviewer_result.return_value = ('FAIL', ('issue',))
                    phase = 'VERIFYING' if stage == 'verify' else 'REVIEWING'
                    context = (
                        self.context(phase) if stage == 'verify'
                        else self.review_context()
                    )
                    result = (self.handler.verify(context, self.handler) if stage == 'verify'
                              else self.handler.review(context))
                    self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK if blocked else ExecutionOutcomeKind.RETRY)

    def test_rebase_conflict_preserves_intent_for_both_checkpoints(self):
        self.remote = 'changed'
        original = self.git
        def conflict(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if args[0] == 'rebase':
                result.returncode = 1
                result.stdout = 'conflict details'
            return result
        self.effects.workspace.git.side_effect = conflict
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        for phase in ('VERIFYING', 'PUBLISHING'):
            with self.subTest(phase=phase):
                self.state.update_project('repo', phase=phase, active_sha='base', validated_base_sha='base')
                context = self.context()
                result = (self.handler.verify(context, self.handler) if phase == 'VERIFYING'
                          else self.handler.publish(context, self.handler))
                self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                self.assertEqual(result.recovery.disposition.value, 'agent_correction')
                self.effects.blocking.block_active_execplan.assert_not_called()
                self.assertEqual(self.state.row()['phase'], 'WORKING')
                self.assertTrue(self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX))
                self.assertIsNone(self.state.row()['validated_base_sha'])
                self.effects.validation.verify.assert_not_called()

    def test_workspace_runtime_failure_retries_without_blocking_or_losing_context(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.make_worktree.side_effect = RuntimeError('temporary git failure')
        self.state.update_project('repo', worker_session_id='worker-session',
                                  worker_result='saved result')

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'worktree_recovery_failed')
        row = self.state.row()
        self.assertEqual(row['worker_session_id'], 'worker-session')
        self.assertEqual(row['worker_result'], 'saved result')
        self.assertEqual(row['worktree'], '/state/G-047')
        self.effects.blocking.block_active_execplan_from_revision.assert_not_called()
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM blocked_checkpoints WHERE repo = 'repo'"
        ).fetchone()[0], 0)

    def test_exhausted_transient_agent_retries_pause_with_saved_session(self):
        def transient_agent(*_args, **kwargs):
            kwargs['on_session_started']('worker-session')
            return lm.AgentResult(
                1, 'service unavailable', session_id='worker-session',
                kind=lm.AgentResultKind.TRANSIENT_FAILURE,
            )

        self.effects.agent.run_agent.side_effect = transient_agent
        result = self.handler.run(self.context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertEqual(result.recovery.observation.reason.value, 'service_unavailable')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_transient_worker_pause_preserves_rebase_resume_marker(self):
        self.state.update_project(
            'repo', pr_conflict_recovery=1,
            last_error=lm.REBASE_IN_PROGRESS_ERROR,
        )
        self.effects.workspace.is_directory.return_value = True
        self.effects.workspace._rebase_in_progress_for_branch.return_value = True
        self.effects.agent.run_agent.side_effect = lambda *_args, **_kwargs: lm.AgentResult(
            1, 'temporary provider failure', 'worker-session',
            lm.AgentResultKind.TRANSIENT_FAILURE,
        )

        result = self.handler.run(self.handler._context())

        self.assertEqual(result.kind, ExecutionOutcomeKind.TRANSIENT_PAUSE)
        self.assertTrue(
            self.state.row()['last_error'].startswith(lm.REBASE_CONFLICT_PREFIX)
        )
        self.assertTrue(self.handler._context().resolving_rebase)

    def test_pr_rebase_resume_operation_failure_retries_without_checkpoint(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.resume_pr_conflict_rebase.return_value = lm.PROJECT_STEP_RETRY
        self.state.update_project(
            'repo', pr_conflict_recovery=1,
            last_error=lm.REBASE_IN_PROGRESS_ERROR,
            worker_session_id='worker-session',
        )

        result = self.handler.prepare()

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'rebase_continue_failed')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_publication_checkpoint_sha_lag_reconciles_from_owned_head(self):
        self.state.update_project(
            'repo', phase='PUBLISHING', active_sha='stale',
            validated_base_sha='base', worker_session_id='worker-session',
            verify_result='saved verify', review_result='saved review',
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'checkpoint_stale')
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['active_sha'], 'base')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertIsNone(self.state.row()['verify_result'])
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_repeated_archive_mismatch_reaches_final_gate_only_at_limit(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.return_value = ('source.md',)
        self.effects.archive.requirement_archive_move_set.return_value = ()
        self.effects.archive.requirement_archive_reconciliation_error.return_value = 'archive changed'

        first = self.handler.publish(self.context(), self.handler)
        self.assertEqual(first.recovery.occurrence, 1)
        self.assertEqual(first.kind, ExecutionOutcomeKind.RETRY)
        self.effects.blocking.block_active_execplan.assert_not_called()
        second = self.handler._recover(first.recovery.observation.reason, 'archive changed',
                                       fingerprint=first.recovery.observation.fingerprint)
        self.assertEqual(second.recovery.occurrence, 2)
        self.effects.blocking.block_active_execplan.assert_not_called()
        third = self.handler._recover(first.recovery.observation.reason, 'archive changed',
                                      fingerprint=first.recovery.observation.fingerprint)
        self.assertEqual(third.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_archive_move_validation_error_returns_to_worker_without_block(self):
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project(
            'repo', phase='PUBLISHING', validated_base_sha='base',
            worker_session_id='worker-session', verify_result='saved verify',
            review_result='saved review',
        )
        self.effects.archive.requirement_archive_candidates.return_value = ('source.md',)
        self.effects.archive.requirement_archive_move_set.side_effect = lm.RequirementArchiveError('move invalid')

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason.value, 'archive_move_mismatch')
        self.assertEqual(self.state.row()['worker_session_id'], 'worker-session')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['verify_result'], 'saved verify')
        self.assertEqual(self.state.row()['review_result'], 'saved review')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_archive_destination_collision_is_final_with_actual_path(self):
        self._use_runtime_contract()
        path = 'docs/requirements/archive/source.md'
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.side_effect = (
            lm.RequirementArchiveCollisionError(path)
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(result.recovery.observation.user_targets, (path,))
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_archive_git_lock_retries_without_blocking(self):
        self._use_runtime_contract()
        self.handler.archive_sources = ('docs/requirements/source.md',)
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')
        self.effects.archive.requirement_archive_candidates.side_effect = (
            lm.GitLockError('locked')
        )

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(result.recovery.observation.reason,
                         handlers.RecoveryReason.GIT_LOCK)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_publication_interruption_recovers_existing_pr_without_repush(self):
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.command.side_effect = RuntimeError('interrupted response')
        paused = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(paused.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(paused.recovery.observation.reason.value, 'publication_retry')
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'created', 'headRefOid': 'base',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        resumed = self.handler.publish(self.context(), self.handler)
        self.assertEqual(resumed.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.calls.count('push'), 1)
        self.assertEqual(self.effects.publication.command.call_count, 1)

    def test_pr_conflict_lease_rejection_blocks_without_opening_pr(self):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
        self.handler.effects = self.effects
        self.state.update_project(
            'repo', phase='PUBLISHING', active_sha='base',
            validated_base_sha='base', publication_remote_oid='old-head',
            pr_conflict_recovery=1, pr_conflict_head_oid='old-head',
            pr_conflict_base_oid='base', pr_head_oid='old-head',
            pr_url='https://example/pr/1',
        )
        self.effects.workspace.remote_branch_oid.return_value = 'old-head'
        self.effects.workspace.git.side_effect = self.observed_git
        self.effects.publication.pr_for_branch.return_value = {
            'url': 'https://example/pr/1', 'state': 'OPEN',
            'headRefName': 'loop-manager/G-047', 'headRefOid': 'old-head',
        }
        self.effects.publication.reconcile_existing_pull_request.return_value = None
        self.effects.publication.push_owned_pr_conflict_recovery.side_effect = (
            lm.PublicationSafetyError('lease rejected')
        )

        prepared = self.handler.recover(self.context())
        self.assertEqual(prepared.phase, ExecutionPhase.PUBLISHING)
        result = self.handler.publish(prepared, self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(
            self.effects.blocking.block_active_execplan.call_args.kwargs['reason'],
            lm.BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
        )
        self.effects.publication.push_owned_pr_conflict_recovery.assert_called_once()
        self.effects.state.check()
        self.assertEqual(self.state.row()['phase'], 'PUBLISHING')

    def test_publication_uses_immutable_plan_link_with_reconciled_metadata(self):
        self.connection.execute(
            "UPDATE exec_plans SET title = 'Current Runtime', "
            "publication_path = 'docs/plans/[G-047] Original Runtime.md', "
            "publication_content_hash = 'original-hash', "
            "publication_sha = ? WHERE repo = 'repo' AND plan_id = 'G-047'",
            ("a" * 40,),
        )
        self.connection.commit()
        self.plan = {
            'id': 'G-047', 'title': 'Current Runtime',
            'sources': ['docs/PRODUCT.md'], 'depends_on': [],
            'complexity': 'low', 'risk': 'low', 'priority': 'normal',
        }
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')
        self.effects.publication.published_execplan.return_value = {
            **self.plan, 'title': 'Original Runtime',
        }
        self.state.update_project('repo', phase='PUBLISHING', validated_base_sha='base')

        result = self.handler.publish(self.context(), self.handler)

        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        show = [
            call for call in self.effects.workspace.git.call_args_list
            if len(call.args) >= 3 and call.args[1] == 'show'
        ]
        self.assertEqual(
            show[-1].args[2],
            f"{'a' * 40}:docs/plans/[G-047] Original Runtime.md",
        )
        build = self.effects.publication.build_pr_body.call_args.args
        self.assertEqual(build[1]['title'], 'Current Runtime')
        self.assertEqual(
            build[2], 'docs/plans/[G-047] Original Runtime.md'
        )
        self.assertEqual(build[5], 'a' * 40)

    def test_workspace_violation_blocks_before_agent(self):
        self.effects.workspace._plan_id_from_path.return_value = 'G-047'
        self.effects.workspace.validate_execplan_workspace_identity.side_effect = lm.WorkspaceOwnershipError('ownership')
        result = self.handler.prepare()
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan_from_revision.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_guidance_mismatch_reads_blocked_plan_after_projection_release(self):
        self.connection.execute(lm.GUIDANCE_APPROVALS_SCHEMA)
        proposal = SimpleNamespace(
            proposal_id='guidance-change', line=10,
            decision=SimpleNamespace(line=20),
        )
        document = SimpleNamespace(proposals=(proposal,), structure_hash='structure')
        approval = {'applied': 0, 'proposal_id': proposal.proposal_id,
                    'target_path': 'AGENTS.md'}
        self.effects.agent.inspect_worker_guidance_result.side_effect = self.state.bind(
            lm.inspect_worker_guidance_result, connected=True, mutates=True,
        )
        context = self.context()
        with (
            mock.patch.object(lm, '_guidance_document', return_value=document),
            mock.patch.object(lm, '_guidance_rows', return_value=(approval,)),
            mock.patch.object(lm, '_validate_guidance_proposal_scope'),
            mock.patch.object(lm, 'is_loop_manager_repository', return_value=True),
            mock.patch.object(lm, '_guidance_document_matches_rows',
                              return_value=({proposal.proposal_id: proposal}, ())),
            mock.patch.object(lm, '_guidance_final_expected',
                              return_value=({('batch', 'AGENTS.md'): (b'approved', 0o100644)}, {})),
            mock.patch.object(lm, '_guidance_target_state',
                              return_value=(b'mismatched', 0o100644)),
            mock.patch.object(lm, 'capture_git_snapshot') as snapshot,
        ):
            snapshot.return_value.serialize.return_value = '{}'
            result = self.handler.run(context)

        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertIsNone(self.repository.projected('repo'))
        self.assertIsNone(self.state.expected)
        blocked = self.repository.get('repo', 'G-047')
        self.assertEqual(blocked.status, 'PLAN_BLOCKED')
        self.assertIn('실제 AGENTS.md 결과가 승인된 patch 결과와 다릅니다.', blocked.last_error)
        self.assertEqual(self.state.blocked()['last_error'], blocked.last_error)
        self.assertEqual(
            self.effects.events.operation_print.call_args.args[0], blocked.last_error,
        )
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()
        with self.assertRaises(runtime_state.RuntimeStateError):
            self.handler.run(context)

class CurrentRebaseAdoptionTests(TestCase):
    """Real handler/CAS policies with immutable fake Git observations only."""
    def setUp(self):
        from managed_rebase_adoption import ManagedRebaseService
        from tests.test_managed_rebase_validation import expectation, observation, scope_for
        self.fixture = ExecutionHandlerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.handler, self.state = self.fixture.handler, self.fixture.state
        self.handler._current_rebase_service = handlers.ExecutionHandlers._current_rebase_service.__get__(self.handler)
        self.checkpoint = ExecutionCheckpointRepository(self.fixture.connection)
        self.state.update_project('repo', worker_result=VALID_WORKER_RESULT,
                                  worker_session_id='saved-worker', verify_result='old verify',
                                  review_result='old review', worker_input_token='old-input')
        self.checkpoint.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='before', target_base_oid='base', observed_remote_oid='remote',
            snapshot={'pending': 'stage', 'stage_evidence': {'old': 'audit'},
                      'failure': 'old failure'})
        self.expected = expectation(repo='repo', plan_id='G-047',
                                    plan_path='docs/plans/[G-047] Runtime.md',
                                    worktree='/state/G-047', branch='loop-manager/G-047')
        self.expected = replace(self.expected, remote=replace(
            self.expected.remote, branch='loop-manager/G-047'))
        self.observed = observation(self.expected, head='owned-head',
                                    branch_ref=('owned-head', ''),
                                    registration=('HEAD=owned-head', 'branch=refs/heads/loop-manager/G-047'))
        def factory(context, **options):
            current = self.checkpoint.get('repo', 'G-047')
            expected = replace(self.expected,
                               generation=current.generation)
            if current.operation != 'rebase':
                row = self.fixture.connection.execute(
                    "SELECT publication_remote_oid FROM exec_plans "
                    "WHERE repo = 'repo' AND plan_id = 'G-047'").fetchone()
                expected = replace(expected, remote=replace(expected.remote,
                    oid=row['publication_remote_oid'] or current.observed_remote_oid))
            if 'remote_oid' in options:
                expected = replace(expected, remote=replace(expected.remote, oid=options['remote_oid']))
            return ManagedRebaseService(expected, mock.Mock())
        self.factory = factory
        self.handler.effects = replace(self.fixture.effects, checkpoint=self.checkpoint,
            workspace=replace(self.fixture.effects.workspace, current_rebase_service=factory))
        self.observation_patch = mock.patch('managed_rebase_adoption.observe_current_rebase',
            side_effect=lambda expected, _: (self.observed, scope_for(expected, self.observed)))
        self.observation_patch.start()
        self.addCleanup(self.observation_patch.stop)
        self.handler._complete_rebase_intent = handlers.ExecutionHandlers._complete_rebase_intent.__get__(self.handler)
        # Isolate current-result adoption from the independently tested remote
        # publication boundary. These observations use synthetic Git OIDs.
        self.handler._publish_rebased_checkpoint = mock.Mock()

    def test_ordinary_checkpoint_remote_observation_keeps_fetched_reader(self):
        from managed_rebase_adoption import checkpoint_remote_oid
        current = self.checkpoint.get('repo', 'G-047')
        for selected in (None, replace(current, operation=None, snapshot={}),
                         replace(current, operation=None, snapshot={
                             'current_rebase_acceptance': {'kind': 'unverified'}})):
            with self.subTest(selected=selected):
                git = mock.Mock(side_effect=AssertionError('ordinary observation must use its existing reader'))
                fetched = mock.Mock(return_value='fetched-oid')
                self.assertEqual(checkpoint_remote_oid(selected, Path('/repo'), 'loop-manager/G-047',
                    git=git, read_fetched=fetched), 'fetched-oid')
                fetched.assert_called_once_with(Path('/repo'), 'loop-manager/G-047')
                git.assert_not_called()

    def _prepare_current_checkpoint_recovery(self):
        self.expected = replace(self.expected, remote=replace(self.expected.remote, repository='repo'))
        self.observed = replace(self.observed, remote=self.expected.remote)
        blocked_checkpoint_state.ensure_schema(self.fixture.connection)
        self.fixture.connection.execute(
            "INSERT INTO blocked_checkpoints "
            "(repo, plan_id, request_identity, phase, reason_code, evidence, "
            "branch, worktree, accepted_remote_oid) "
            "VALUES ('repo', 'G-047', 'request', 'published', 'review', 'evidence', "
            "'loop-manager/G-047', '/state/G-047', 'remote')")
        self.state.update_project('repo', publication_remote_oid='remote')
        self.fixture.effects.workspace._rebase_in_progress_for_branch.return_value = False
        self.fixture.effects.workspace.git_operation_entries.return_value = {}
        self.fixture.effects.workspace.remote_branch_oid.return_value = 'remote'
        self.fixture.effects.publication.pr_for_branch.return_value = None
        self.fixture.effects.workspace.git.side_effect = self.fixture.observed_git
        self.handler._fetch_latest_base = mock.Mock(return_value='base')
        # These unit observations use symbolic OIDs. Fake the live-ref process
        # boundary as well as the fetched-ref boundary, retaining real policy.
        live_remote = mock.patch.object(handlers.manager_git_journal, 'live_remote_branch_oid',
                                       side_effect=lambda *_: self.observed.remote.oid)
        live_remote.start()
        self.addCleanup(live_remote.stop)
        self.owner_patch = mock.patch.object(handlers.checkpoint_ownership,
                                            'owned_remote_oid', return_value='remote')
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)

    def test_current_acceptance_push_completion_retains_ownership_on_restart(self):
        self._prepare_current_checkpoint_recovery()
        self.handler._complete_rebase_intent(self.fixture.context())
        accepted = self.checkpoint.get('repo', 'G-047').snapshot['current_rebase_acceptance']
        for entry in ('normal', 'recover'):
            with self.subTest(entry=entry):
                saved = self.checkpoint.get('repo', 'G-047')
                self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
                    operation='push', expected_head='owned-head', target_base_oid='base',
                    observed_remote_oid='remote', snapshot={
                        'current_rebase_acceptance': accepted, 'remote_before': 'remote'})
                self.state.update_project('repo', publication_remote_oid='remote')
                self.fixture.effects.workspace.remote_branch_oid.return_value = 'owned-head'
                self.observed = replace(self.observed, remote=replace(self.expected.remote, oid='owned-head'))
                self.fixture.dirty = False
                if entry == 'normal':
                    # Publication has already confirmed the exact live push.
                    self.state.update_project('repo', publication_remote_oid='owned-head')
                    self.handler._complete_publication_intent(self.fixture.context())
                else:
                    self.assertIsInstance(self.handler.recover(self.fixture.context()), handlers.ExecutionContext)
                saved = self.checkpoint.get('repo', 'G-047')
                self.assertEqual(saved.snapshot['pushed_checkpoint_head'], 'owned-head')
                self.assertIsNone(saved.operation)
                self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
                self.assertIsInstance(self.handler.recover(self.fixture.context()), handlers.ExecutionContext)
                saved = self.checkpoint.get('repo', 'G-047')
                self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
                    operation='pr_create', expected_head='owned-head', target_base_oid='base',
                    observed_remote_oid='owned-head', snapshot=saved.snapshot)
                pr = dict(url='new-pr', headRefOid='owned-head')
                from managed_rebase_validation import PullRequest
                self.observed = replace(self.observed, remote=replace(self.observed.remote,
                    pull_request=PullRequest('new-pr', 'repo', self.expected.branch, 'owned-head', 'OPEN')))
                self.assertTrue(self.handler._owned_checkpoint_remote(self.fixture.context(),
                    self.state.row(), 'owned-head', self.checkpoint.get('repo', 'G-047'), pr))
                self.assertFalse(self.handler._owned_checkpoint_remote(self.fixture.context(),
                    self.state.row(), 'foreign', self.checkpoint.get('repo', 'G-047'), pr))
                self.observed = replace(self.observed, remote=self.expected.remote)

    def test_adopted_checkpoint_restored_stage_reaches_worker_revalidation(self):
        self._assert_restored_stage_recovery()

    def test_new_activation_preserves_adopted_stage_and_revalidates_worker(self):
        self._assert_restored_stage_recovery(newly_activated=True)

    def test_new_activation_rejects_changed_adopted_head_or_remote(self):
        for changed in ('head', 'remote'):
            with self.subTest(changed=changed):
                case = current_rebase_case(self)
                case._prepare_current_checkpoint_recovery()
                case.handler._complete_rebase_intent(case.fixture.context())
                accepted = case.checkpoint.get('repo', 'G-047').snapshot['current_rebase_acceptance']
                case.handler.newly_activated = True
                if changed == 'head':
                    case.observed = replace(case.observed, head='foreign', branch_ref=('foreign', ''),
                        registration=('HEAD=foreign', 'branch=refs/heads/loop-manager/G-047'))
                    def git(path, *args, **kwargs):
                        if args == ('rev-parse', 'HEAD'):
                            return SimpleNamespace(stdout='foreign', returncode=0)
                        return case.fixture.observed_git(path, *args, **kwargs)
                    case.fixture.effects.workspace.git.side_effect = git
                else:
                    case.fixture.effects.workspace.remote_branch_oid.return_value = 'foreign'
                    case.observed = replace(case.observed, remote=replace(case.observed.remote, oid='foreign'))
                result = case.handler.recover(case.fixture.context())
                self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
                saved = case.checkpoint.get('repo', 'G-047')
                self.assertEqual(saved.expected_head, 'owned-head')
                self.assertEqual(saved.snapshot['current_rebase_acceptance'], accepted)
                case.fixture.effects.agent.run_agent.assert_not_called()

    def _assert_restored_stage_recovery(self, *, newly_activated=False):
        from managed_rebase_validation import FileChange, IndexEntry
        self._prepare_current_checkpoint_recovery()
        self.handler._complete_rebase_intent(self.fixture.context())
        accepted = self.checkpoint.get('repo', 'G-047').snapshot['current_rebase_acceptance']
        self.handler.newly_activated = newly_activated
        if newly_activated:
            current = self.checkpoint.get('repo', 'G-047')
            self.checkpoint.record('repo', 'G-047', expected_generation=current.generation,
                operation=None, expected_head=current.expected_head, target_base_oid='base',
                observed_remote_oid='remote', snapshot={**current.snapshot, 'blocked_resume': {
                    'before_head': 'before', 'after_head': 'owned-head',
                    'target_base_oid': 'base', 'owner_oid': 'remote'}})
        clean = self.observed
        staged = FileChange('app.py', '100644', '100644', 'old', 'new', 'M')
        self.observed = replace(clean, staged=(staged,), index_clean=False)
        result = self.handler.recover(self.fixture.context())
        self.assertIsInstance(result, handlers.ExecutionContext)
        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot['current_rebase_acceptance'], accepted)
        self.assertIsNone(self.state.row()['worker_result'])
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            self.factory(None).require_owned()  # Staging is not a new completion.
        saved = self.checkpoint.get('repo', 'G-047')
        preserved = self.observed
        for invalid in (
            replace(self.observed, operations=(('MERGE_HEAD', 'foreign'),)),
            replace(self.observed, head='foreign', branch_ref=('foreign', '')),
            replace(self.observed, staged=(replace(staged, path='docs/ARCHITECTURE.md'),)),
            replace(self.observed, index=(IndexEntry('app.py', '100644', 'new', 1, 'M', 0),)),
            replace(self.observed, index=(IndexEntry('app.py', '100644', 'new', 0, 'H', 32768),)),
            replace(self.observed, remote=replace(self.observed.remote, oid='foreign')),
        ):
            with self.subTest(observed=invalid):
                self.observed = invalid
                self.assertFalse(self.handler._owned_checkpoint_remote(
                    self.fixture.context(), self.state.row(), 'remote', saved))
        self.observed = preserved
        for acceptance in (None, {**accepted, 'head': 'tampered'}):
            with self.subTest(acceptance=acceptance):
                changed = replace(saved, snapshot={**saved.snapshot, 'current_rebase_acceptance': acceptance})
                self.assertFalse(self.handler._owned_checkpoint_remote(
                    self.fixture.context(), self.state.row(), 'remote', changed))
        from tests.test_execution_revision import definition
        self.handler._definition_snapshot = definition(path='docs/plans/[G-047] Runtime.md')
        finished = self.handler._finish_worker(result, self.state.row(), VALID_WORKER_RESULT)
        self.assertEqual(finished.context.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.checkpoint.get('repo', 'G-047').snapshot['worker_candidate_token'],
                         self.handler._definition_snapshot.worker_candidate_token('owned-head'))

    def test_manual_autostash_stage_without_worker_result_is_committed_and_readopted(self):
        self._assert_manual_autostash_recovery()

    def test_autostash_commit_interruptions_recover_original_rebase_intent(self):
        for boundary in ('intent', 'git', 'result'):
            with self.subTest(boundary=boundary):
                case = current_rebase_case(self)
                case._assert_manual_autostash_recovery(interruption=boundary)

    def _assert_manual_autostash_recovery(self, *, interruption=None):
        from managed_rebase_validation import FileChange
        self._prepare_current_checkpoint_recovery()
        saved = self.checkpoint.get('repo', 'G-047')
        self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
            operation='rebase', expected_head='before', target_base_oid='base',
            observed_remote_oid='remote', snapshot={'stash_oid': 'stash'})
        self.state.update_project('repo', worker_result=None, active_sha='old-base')
        self.expected = replace(self.expected, autostash_oid='stash')
        self.observed = replace(self.observed, index_clean=False, staged=(
            FileChange('app.py', '100644', '100644', 'old', 'new', 'M'),))
        calls = []
        class Interrupted(BaseException):
            pass
        armed = [interruption]
        record = self.checkpoint.record
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
                return SimpleNamespace(stdout='', returncode=int(not self.observed.index_clean))
            if args[:1] == ('commit',):
                self.observed = replace(self.observed, head='resolved-head', staged=(), index_clean=True,
                    branch_ref=('resolved-head', ''), registration=(
                        'HEAD=resolved-head', 'branch=refs/heads/loop-manager/G-047'))
                if armed[0] == 'git':
                    armed[0] = None
                    raise Interrupted()
                return SimpleNamespace(stdout='', returncode=0)
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(stdout=self.observed.head, returncode=0)
            return self.fixture.observed_git(path, *args, **kwargs)
        self.fixture.effects.workspace.git.side_effect = git
        with mock.patch.object(self.checkpoint, 'record', side_effect=interrupted_record):
            if interruption:
                with self.assertRaises(Interrupted):
                    self.handler.recover(self.fixture.context())
                pending = self.checkpoint.get('repo', 'G-047')
                self.assertEqual(pending.operation, 'rebase')
                self.assertEqual(pending.expected_head, 'before')
                self.assertEqual(pending.target_base_oid, 'base')
                self.assertEqual(pending.observed_remote_oid, 'remote')
                self.assertEqual(pending.snapshot['stash_oid'], 'stash')
                self.assertIsNone(pending.current_rebase_acceptance())
            result = self.handler.recover(self.fixture.context())
        self.assertIsInstance(result, handlers.ExecutionContext)
        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        saved = self.checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.snapshot['current_rebase_acceptance']['head'], 'resolved-head')
        self.assertIsNone(self.state.row()['worker_result'])
        self.assertIsNone(self.state.row()['verify_result'])
        self.assertEqual(sum(args[0] == 'commit' for args in calls), 1)
        self.assertFalse(any(args[0] == 'add' or 'rebase' in args for args in calls))
        self.assertIsInstance(self.handler.recover(self.fixture.context()), handlers.ExecutionContext)
        self.assertEqual(sum(args[0] == 'commit' for args in calls), 1)
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()

    def test_manual_completion_without_journal_invalidates_worker_and_validation_once(self):
        with mock.patch.object(handlers.manager_git_journal, 'verified_rebase_completion',
                               side_effect=AssertionError('history must not gate acceptance')):
            self.handler._complete_rebase_intent(self.fixture.context())
        saved = self.checkpoint.get('repo', 'G-047')
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.expected_head, 'owned-head')
        self.assertEqual(saved.snapshot['failure'], 'old failure')
        self.assertEqual(saved.snapshot['stage_evidence'], {'old': 'audit'})
        self.assertEqual(saved.snapshot['rebase_revalidation_audit'][0]['verify_result'], 'old verify')
        row = self.state.row()
        self.assertEqual(row['phase'], 'WORKING')
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        for field in ('worker_result', 'worker_input_token', 'verify_result', 'review_result',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(row[field], field)
        self.handler._complete_rebase_intent(self.handler._context())
        self.assertEqual(self.checkpoint.get('repo', 'G-047'), saved)
        self.fixture.effects.validation.verify.assert_not_called()
        self.fixture.effects.agent.run_agent.assert_not_called()

    def test_stale_or_out_of_scope_current_result_preserves_checkpoint_and_audit(self):
        from managed_rebase_validation import FileChange
        original = self.checkpoint.get('repo', 'G-047')
        for observed in (replace(self.observed, remote=replace(self.expected.remote, oid='foreign')),
                         replace(self.observed, input_fingerprint='changed'),
                         replace(self.observed, committed=(FileChange(
                             'docs/ARCHITECTURE.md', '100644', '100644', 'old', 'new', 'M'),))):
            with self.subTest(observed=observed):
                self.observed = observed
                with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
                    self.handler._complete_rebase_intent(self.fixture.context())
                self.assertEqual(self.checkpoint.get('repo', 'G-047'), original)
                self.assertEqual(self.state.row()['verify_result'], 'old verify')

    def test_current_remote_owner_survives_rewritten_commits_without_replay_gate(self):
        checkpoint = {'accepted_remote_oid': 'remote'}
        row = dict(self.state.row(), publication_remote_oid='remote')
        with mock.patch.object(handlers.checkpoint_ownership, 'owned_remote_oid', return_value='remote') as owner, \
             mock.patch.object(handlers.manager_git_journal, 'verified_managed_transitions',
                               side_effect=AssertionError('no replay gate')):
            self.assertTrue(self.handler._owned_current_checkpoint_rebase(
                self.fixture.context(), row, 'remote', self.checkpoint.get('repo', 'G-047'), checkpoint, None))
            self.assertNotIn('before_head', owner.call_args.kwargs)
            self.assertFalse(self.handler._owned_current_checkpoint_rebase(
                self.fixture.context(), row, 'external', self.checkpoint.get('repo', 'G-047'), checkpoint, None))

    def test_direct_and_handler_paths_share_current_result_without_session_receipts(self):
        with mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')), \
             mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **_k: self.factory(None)), \
             mock.patch.object(lm, '_publish_direct_rebase_checkpoint'), \
             mock.patch.object(handlers.manager_git_journal, 'verified_managed_transitions',
                               side_effect=AssertionError('no history')):
            self.assertTrue(lm._direct_rebase_history_proven(
                self.fixture.connection, Path('/repo'), 'repo', 'G-047', Path('/state/G-047'),
                'loop-manager/G-047', 'before', 'owned-head', 'base', 'replacement-session'))
            lm._complete_direct_rebase_checkpoint(
                self.fixture.connection, 'repo', 'G-047', Path('/state/G-047'), 'base',
                repo_path=Path('/repo'), branch='loop-manager/G-047', agent_session_id=None)
        self.assertEqual(self.state.repository.projected('repo').status, 'IMPLEMENTING')
        self.assertIsNone(self.checkpoint.get('repo', 'G-047').operation)

    def test_owned_detached_resolution_waits_without_worker_turn_or_stage_approval(self):
        from managed_rebase_validation import IndexEntry
        self.observed = replace(self.observed, head='partial', branch=None,
            branch_ref=('before', ''), registration=('HEAD=partial', 'detached=True'),
            branch_worktrees=(), operations=(('rebase-merge', 'operation'),),
            metadata=(('head-name', 'refs/heads/loop-manager/G-047'),
                      ('orig-head', 'before'), ('onto', 'base')),
            index=(IndexEntry('app.py', '100644', 'conflict', 2, 'M', 0x2000),),
            index_clean=False)
        from managed_rebase_validation import RebaseStatus
        service = self.factory(None)
        before = self.checkpoint.get('repo', 'G-047')
        self.assertEqual(service.require_owned()[1].status, RebaseStatus.WAITING)
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            service.adopt(self.state, self.checkpoint)
        self.assertEqual(self.checkpoint.get('repo', 'G-047'), before)

    def test_selected_baseline_keeps_original_and_unrelated_metadata(self):
        from managed_rebase_adoption import selected_baseline
        before = lm.GitSnapshot('before', 'loop-manager/G-047', {}, {},
            {'refs/heads/loop-manager/G-047': ('before', ''), 'refs/heads/other': ('old', '')},
            {'/state/G-047': ('HEAD=before',), '/other': ('HEAD=other',)}, {}, {})
        current = replace(before, head='owned-head',
            refs={**before.refs, 'refs/heads/loop-manager/G-047': ('owned-head', ''),
                  'refs/heads/other': ('unauthorized', '')},
            worktrees={**before.worktrees, '/state/G-047': ('HEAD=owned-head',)})
        accepted = selected_baseline(before, current, 'loop-manager/G-047', '/state/G-047')
        self.assertEqual(accepted.head, 'owned-head')
        self.assertEqual(accepted.refs['refs/heads/other'], ('old', ''))
        self.assertEqual(before.head, 'before')
        self.assertEqual(before.worktrees['/state/G-047'], ('HEAD=before',))

    def test_staging_leaves_already_staged_paths_intact(self):
        self.fixture.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='partial', branch=None,
            dirty={'app.py': (('staged',), 'manual'), 'other.py': (('unstaged',), 'resolved')})
        def git(_path, *args, **kwargs):
            if args[:2] == ('ls-files', '--cached'):
                return SimpleNamespace(stdout='app.py\0other.py\0', returncode=0)
            return SimpleNamespace(stdout='', returncode=0)
        self.fixture.effects.workspace.git.side_effect = git
        result = self.handler._stage_rebase_correction(self.fixture.context())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.fixture.effects.workspace.git.call_args.args[1:],
                         ('add', '-A', '--', 'other.py'))

    def test_worker_confirmation_is_bound_to_current_candidate_and_inputs(self):
        from tests.test_execution_revision import definition
        self.handler._complete_rebase_intent(self.fixture.context())
        self.handler._definition_snapshot = definition(path='docs/plans/[G-047] Runtime.md')
        self.fixture.remote = 'owned-head'
        result = self.handler._finish_worker(self.handler._context(), self.state.row(), VALID_WORKER_RESULT)
        self.assertEqual(result.context.phase, ExecutionPhase.VERIFYING)
        confirmed = self.checkpoint.get('repo', 'G-047').snapshot['worker_candidate_token']
        self.assertEqual(confirmed, self.handler._definition_snapshot.worker_candidate_token('owned-head'))
        self.assertNotEqual(confirmed, self.handler._definition_snapshot.worker_candidate_token('another-head'))

    def test_legacy_completed_checkpoint_revalidates_once_in_both_entry_points(self):
        for marker in ('rebase_completion', 'completed_rebase'):
            for entry in ('handler', 'direct'):
                with self.subTest(marker=marker, entry=entry):
                    case = current_rebase_case(self)
                    prior = case.checkpoint.get('repo', 'G-047')
                    legacy = {'head': 'historical-head', 'target_base_oid': 'base'}
                    case.checkpoint.record('repo', 'G-047', expected_generation=prior.generation,
                        operation=None, expected_head='owned-head', target_base_oid='base',
                        observed_remote_oid='remote', snapshot={**prior.snapshot, marker: legacy})
                    with mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')), \
                         mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **_k: case.factory(None)), \
                         mock.patch.object(lm, '_publish_direct_rebase_checkpoint'):
                        def complete():
                            if entry == 'handler':
                                case.handler._complete_rebase_intent(case.handler._context())
                            else:
                                lm._complete_direct_rebase_checkpoint(case.fixture.connection, 'repo', 'G-047',
                                    Path('/state/G-047'), 'base', repo_path=Path('/repo'),
                                    branch='loop-manager/G-047', agent_session_id=None)
                        complete()
                        saved = case.checkpoint.get('repo', 'G-047')
                        complete()
                    self.assertEqual(case.checkpoint.get('repo', 'G-047'), saved)
                    self.assertEqual(saved.snapshot[marker], legacy)
                    self.assertIsNotNone(saved.current_rebase_acceptance())
                    self.assertIsNone(case.fixture.repository.projected('repo').worker_result)
                    self.assertEqual(case.fixture.repository.projected('repo').status, 'IMPLEMENTING')

    def test_start_observes_manual_completion_without_repeating_git(self):
        self.handler._run_managed_rebase = handlers.ExecutionHandlers._run_managed_rebase.__get__(self.handler)
        result = self.handler._run_managed_rebase(self.fixture.context(), 'base')
        self.assertEqual(result.returncode, 0)
        self.fixture.effects.workspace.git.assert_not_called()
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            self.handler._run_managed_rebase(self.fixture.context(), 'wrong-target')
        self.fixture.effects.workspace.git.assert_not_called()

    def test_new_rebase_preserves_nested_prior_operation_audit_without_cycles(self):
        self.handler._complete_rebase_intent(self.fixture.context())
        saved = self.checkpoint.get('repo', 'G-047')
        prior = {**saved.snapshot, 'rebase_operation_history': [{'before_head': 'older'}]}
        self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
            operation=None, expected_head='owned-head', observed_remote_oid='remote',
            target_base_oid='base', snapshot=prior)
        self.fixture.effects.workspace.remote_branch_oid.return_value = 'remote'
        self.fixture.effects.workspace.git.side_effect = lambda *_a, **_k: SimpleNamespace(stdout='owned-head', returncode=0)
        with mock.patch.object(handlers.manager_git_journal, 'live_remote_branch_oid', return_value='remote'), \
             mock.patch.object(handlers.manager_git_journal, 'begin_current_rebase', return_value='new-audit'):
            self.handler._record_rebase_intent(self.handler._context(), 'advanced-base')
        current = self.checkpoint.get('repo', 'G-047')
        self.assertEqual(current.target_base_oid, 'advanced-base')
        self.assertEqual(current.snapshot['rebase_operation_history'], [{'before_head': 'older'}, prior])
        self.assertNotIn('current_rebase_acceptance', current.snapshot)
        self.assertEqual(current.snapshot['current_rebase_acceptance_history'][-1], prior['current_rebase_acceptance'])

    def test_only_confirmed_worker_patch_carries_candidate_token_to_manager_commit(self):
        from tests.test_execution_revision import definition
        self.handler._complete_rebase_intent(self.fixture.context())
        current = definition(path='docs/plans/[G-047] Runtime.md')
        self.handler._definition_snapshot = current
        for confirmed in (True, False):
            with self.subTest(worker_confirmed=confirmed):
                saved = self.checkpoint.get('repo', 'G-047')
                self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
                    operation=None, expected_head='owned-head', target_base_oid='base', observed_remote_oid='remote',
                    snapshot={**saved.snapshot, 'worker_candidate_token': current.worker_candidate_token('owned-head')})
                head = ['owned-head']
                def git(_path, *args, **kwargs):
                    if args[0] == 'commit':
                        head[0] = 'committed'
                    output = head[0] if args == ('rev-parse', 'HEAD') else 'tree'
                    return SimpleNamespace(stdout=output, returncode=0)
                self.fixture.effects.workspace.git.side_effect = git
                self.handler._commit_owned(self.handler._context(), 'Current patch', worker_confirmed=confirmed)
                token = self.checkpoint.get('repo', 'G-047').snapshot['worker_candidate_token']
                self.assertEqual(token, current.worker_candidate_token('committed' if confirmed else 'owned-head'))

    def test_publication_rebase_always_returns_to_worker_confirmation(self):
        for validated in ('old-base', 'new-base'):
            with self.subTest(validated_base=validated):
                saved = self.checkpoint.get('repo', 'G-047')
                self.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
                    operation='rebase', expected_head='before', target_base_oid='new-base',
                    observed_remote_oid='remote', snapshot=saved.snapshot)
                self.expected = replace(self.expected, target_base_oid='new-base')
                self.state.update_project('repo', phase='PUBLISHING', active_sha='base',
                    validated_base_sha=validated, worker_result=VALID_WORKER_RESULT)
                def git(_path, *args, **kwargs):
                    return SimpleNamespace(stdout='', returncode=int(args[:2] == ('merge-base', '--is-ancestor')))
                self.fixture.effects.workspace.git.side_effect = git
                def rebase(context, *_a, **_k):
                    self.handler._complete_rebase_intent(context)
                with mock.patch.object(self.handler, '_fetch_latest_base', return_value='new-base'), \
                     mock.patch.object(self.handler, '_rebase_latest', side_effect=rebase):
                    result = self.handler._latest_before_publication(self.handler._context())
                self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                self.assertEqual(self.state.row()['phase'], 'WORKING')
                self.assertIsNone(self.state.row()['worker_result'])
                self.fixture.effects.validation.verify.assert_not_called()


def current_rebase_case(test):
    """Production current-state/CAS fixture for updating legacy gate cases."""
    case = CurrentRebaseAdoptionTests()
    case.setUp()
    test.addCleanup(case.doCleanups)
    return case


class CurrentRebaseAgentScopeTests(TestCase):
    def setUp(self):
        self.case = current_rebase_case(self)
        self.case.handler._complete_rebase_intent(self.case.fixture.context())
        branch, worktree = 'loop-manager/G-047', '/state/G-047'
        self.original = lm.GitSnapshot('before', branch, {}, {},
            {f'refs/heads/{branch}': ('before', ''), 'refs/heads/other': ('other', '')},
            {worktree: ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
        self.current = replace(self.original, head='owned-head',
            refs={**self.original.refs, f'refs/heads/{branch}': ('owned-head', '')},
            worktrees={worktree: ('HEAD=owned-head', f'branch=refs/heads/{branch}')})
        self.case.state.update_project('repo', agent_role='worker',
            agent_snapshot=self.original.serialize())
        for patch in (
            mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **_k: self.case.factory(None)),
            mock.patch.object(lm, 'capture_git_snapshot', side_effect=lambda *_a: self.current),
            mock.patch.object(lm, '_config_entries', return_value={}),
            mock.patch.object(lm, '_proven_merged_cleanup_paths', return_value=frozenset()),
            mock.patch.object(Path, 'is_dir', return_value=True),
            mock.patch.object(lm, 'git', return_value=SimpleNamespace(stdout='', returncode=0)),
            mock.patch.object(lm, 'git_bytes', return_value=b''),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_resumed_scope_keeps_original_and_records_observed_comparison_start(self):
        from managed_rebase_adoption import agent_comparison_baseline
        before = lm.begin_agent_scope(self.case.fixture.connection, 'repo', Path('/state/G-047'), 'worker')
        self.assertEqual(before, self.current)
        saved = self.case.checkpoint.get('repo', 'G-047')
        self.assertEqual(agent_comparison_baseline(self.original, saved.snapshot, 'worker'), self.current)
        self.assertEqual(self.case.fixture.connection.execute(
            "SELECT agent_snapshot FROM projects WHERE repo = 'repo'").fetchone()[0], self.original.serialize())
        later = replace(self.original, head='later-start')
        self.assertEqual(agent_comparison_baseline(later, saved.snapshot, 'worker'), later)
        self.assertEqual(agent_comparison_baseline(self.original, saved.snapshot, 'reviewer'), self.original)
        lm.begin_agent_scope(self.case.fixture.connection, 'repo', Path('/state/G-047'), 'worker')
        updated = self.case.checkpoint.get('repo', 'G-047')
        self.assertEqual(updated.snapshot['current_rebase_agent_baseline_history'],
                         [saved.snapshot['current_rebase_agent_baseline']])

    def test_later_head_change_still_requires_independent_manager_git_proof(self):
        connection = self.case.fixture.connection
        lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker')
        accepted_start = self.current
        from managed_rebase_adoption import needs_agent_comparison
        self.assertFalse(needs_agent_comparison(
            replace(self.case.checkpoint.get('repo', 'G-047'), operation='commit'),
            self.original, accepted_start))
        self.current = replace(self.current, head='later-head',
            refs={**self.current.refs, 'refs/heads/loop-manager/G-047': ('later-head', '')},
            worktrees={'/state/G-047': ('HEAD=later-head', 'branch=refs/heads/loop-manager/G-047')})
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo='repo'",
                           (accepted_start.serialize(),))
        with self.assertRaises(lm.AgentScopeError):
            lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo')
        self.assertEqual(connection.execute("SELECT agent_snapshot FROM projects WHERE repo='repo'").fetchone()[0],
                         self.original.serialize())

    def test_fresh_scope_preserves_restored_stage_without_readopting_rebase(self):
        connection = self.case.fixture.connection
        self.current = replace(self.current,
            index={'app.py': ('H 100644 original-staged-blob 0',)},
            dirty={'app.py': (('staged',), 'original-file')})
        self.original = self.current
        self.case.state.update_project('repo', agent_snapshot=self.original.serialize(),
                                       agent_result_snapshot=self.current.serialize())
        with mock.patch.object(lm, '_current_rebase_service', side_effect=AssertionError('fresh scope is not rebase completion')):
            self.assertEqual(lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker'), self.original)
            self.assertEqual(lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo'), self.current)
        self.assertEqual(connection.execute("SELECT agent_snapshot FROM projects WHERE repo='repo'").fetchone()[0],
                         self.original.serialize())

    def test_persisted_actual_agent_result_still_rejects_shared_ref_violation(self):
        connection = self.case.fixture.connection
        lm.begin_agent_scope(connection, 'repo', Path('/state/G-047'), 'worker')
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo = 'repo'",
                           (self.current.serialize(),))
        self.assertEqual(lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo'), self.current)
        violation = replace(self.current, refs={**self.current.refs, 'refs/heads/other': ('foreign', '')})
        connection.execute("UPDATE projects SET agent_result_snapshot = ? WHERE repo = 'repo'",
                           (violation.serialize(),))
        with self.assertRaises(lm.AgentScopeError):
            lm.validate_persisted_agent_scope(connection, Path('/repo'), Path('/state'), 'repo')
        self.assertEqual(connection.execute("SELECT agent_snapshot FROM projects WHERE repo = 'repo'").fetchone()[0], self.original.serialize())


class ImmediateRebasePublicationTests(TestCase):
    """Only fake external effects; test real acceptance, intents and runtime CAS."""
    def setUp(self):
        self.seed = CurrentRebaseAdoptionTests()
        self.seed.setUp()
        self.addCleanup(self.seed.doCleanups)
        self.seed._prepare_current_checkpoint_recovery()
        self.handler = self.seed.handler
        self.handler._publish_rebased_checkpoint = handlers.ExecutionHandlers._publish_rebased_checkpoint.__get__(self.handler)
        self.state, self.checkpoints = self.seed.state, self.seed.checkpoint
        self.fixture = self.seed.fixture
        self.remote = 'remote'
        live = mock.patch.object(handlers.manager_git_journal, 'live_remote_branch_oid',
                                 side_effect=lambda *_: self.remote)
        live.start()
        self.addCleanup(live.stop)
        self.pushes = []
        self.events = []
        self.push_failure = False
        self.before_push = None
        self.handler.effects.workspace.remote_branch_oid.side_effect = lambda *_: self.remote
        self.handler.effects.workspace.git.side_effect = self.git
        factory = self.seed.factory
        self.handler.effects = replace(self.handler.effects, workspace=replace(
            self.handler.effects.workspace, current_rebase_service=lambda context, **options:
            factory(context, **{'remote_oid': self.state.row()['publication_remote_oid'] or self.seed.expected.remote.oid, **options})))

    def git(self, path, *args, **kwargs):
        if args[0] == 'ls-remote':
            return SimpleNamespace(returncode=0, stdout=(
                f'{self.remote}\trefs/heads/loop-manager/G-047\n' if self.remote else ''))
        if args[0] == 'push':
            self.events.append('push')
            self.pushes.append(args)
            if self.before_push:
                self.before_push()
            if self.push_failure:
                return SimpleNamespace(returncode=1, stdout='permission denied')
            self.remote = 'owned-head'
            self.seed.observed = replace(self.seed.observed,
                                         remote=replace(self.seed.expected.remote, oid=self.remote))
            return SimpleNamespace(returncode=0, stdout='pushed')
        return self.fixture.observed_git(path, *args, **kwargs)

    def complete(self):
        self.handler._complete_rebase_intent(self.handler._context())

    def restart(self):
        self.state = ExecutionStatePort(self.fixture.connection,
            runtime_state.RuntimeStateRepository(self.fixture.connection), 'repo')
        self.handler.effects = replace(self.handler.effects, state=self.state)
        replacement = handlers.ExecutionHandlers(self.handler.effects,
            repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'), base='main',
            base_sha='later-base', provider='codex', config_path=Path('/config'), newly_activated=False)
        replacement.plan = self.handler.plan
        replacement.catalog_plan = self.handler.catalog_plan
        self.handler = replacement

    def direct_ports(self):
        stack = ExitStack()
        for patch in (
            mock.patch.object(lm, '_owned_rebase_state_dir', return_value=Path('/state')),
            mock.patch.object(lm, '_current_rebase_service', side_effect=lambda *_a, **options:
                self.seed.factory(None, remote_oid=options.get('remote_oid', self.remote))),
            mock.patch.object(lm, 'git', side_effect=self.git),
            mock.patch.object(lm, 'remote_branch_oid', side_effect=lambda *_: self.remote),
            mock.patch.object(lm, 'pr_for_branch', return_value=None),
        ):
            stack.enter_context(patch)
        return stack

    def test_pending_publication_passes_persisted_agent_scope_before_handler_recovery(self):
        class Exit(BaseException):
            pass
        for stop in ('before-push', 'after-push'):
            with self.subTest(stop=stop):
                self.setUp()
                self.before_push = (lambda: (_ for _ in ()).throw(Exit())) if stop == 'before-push' else None
                with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
                    with self.assertRaises(Exit):
                        self.complete()
                pending = self.checkpoints.get('repo', 'G-047')
                branch, worktree = 'loop-manager/G-047', '/state/G-047'
                ref, remote_ref = f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'
                original = lm.GitSnapshot('before', branch, {}, {},
                    {ref: ('before', ''), remote_ref: ('remote', ''), 'refs/heads/other': ('other', '')},
                    {worktree: ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
                current = replace(original, head='owned-head', refs={**original.refs,
                    ref: ('owned-head', ''), remote_ref: (self.remote, '')},
                    worktrees={worktree: ('HEAD=owned-head', f'branch=refs/heads/{branch}')})
                self.state.update_project('repo', agent_role='worker',
                    agent_snapshot=original.serialize(), agent_result_snapshot=original.serialize())
                with self.direct_ports(), ExitStack() as ports:
                    capture = ports.enter_context(mock.patch.object(lm, 'capture_git_snapshot', return_value=current))
                    for patch in (
                        mock.patch.object(lm, '_config_entries', return_value={}),
                        mock.patch.object(lm, '_proven_merged_cleanup_paths', return_value=frozenset()),
                        mock.patch.object(Path, 'is_dir', return_value=True),
                        mock.patch.object(lm, 'git_bytes', return_value=b''),
                    ):
                        ports.enter_context(patch)
                    self.assertEqual(lm.validate_persisted_agent_scope(
                        self.fixture.connection, Path('/repo'), Path('/state'), 'repo'), current)
                    self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                    # Normalization never absorbs a different shared ref.
                    capture.return_value = replace(current, refs={**current.refs,
                        'refs/heads/other': ('foreign', '')})
                    with self.assertRaises(lm.AgentScopeError):
                        lm.validate_persisted_agent_scope(
                            self.fixture.connection, Path('/repo'), Path('/state'), 'repo')
                self.assertEqual(self.fixture.connection.execute(
                    "SELECT agent_snapshot FROM projects WHERE repo='repo'").fetchone()[0], original.serialize())
                self.before_push = None
                self.restart()
                self.handler._publish_rebased_checkpoint(self.handler._context())
                self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                                 pending.snapshot['rebase_publication_intent'])
                self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
                self.assertEqual(len(self.pushes), 2 if stop == 'before-push' else 1)

    def test_pending_publication_service_accepts_only_saved_lease_or_pushed_head(self):
        class Exit(BaseException):
            pass
        self.before_push = lambda: (_ for _ in ()).throw(Exit())
        with self.assertRaises(Exit):
            self.complete()
        from tests.test_execution_revision import definition
        self.state.update_project('repo', definition_snapshot=definition(
            path='docs/plans/[G-047] Runtime.md').serialize())
        with ExitStack() as ports:
            for patch in (
                mock.patch.object(lm, '_revision_execplan_input_fingerprint', return_value='inputs'),
                mock.patch.object(lm, '_worktree_execplan_input_fingerprint', return_value='inputs'),
                mock.patch.object(lm, 'requirement_archive_candidates', return_value=()),
                mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False),
                mock.patch.object(lm, 'validate_active_execplan_metadata'),
                mock.patch.object(lm, '_owned_rebase_metadata', return_value=None),
                mock.patch.object(lm, 'git', return_value=SimpleNamespace(returncode=0, stdout='')),
            ):
                ports.enter_context(patch)
            for oid in ('remote', 'owned-head'):
                self.remote = oid
                service = lm._current_rebase_service(self.fixture.connection, Path('/repo'),
                    Path('/state'), 'repo', 'G-047', Path('/state/G-047'), 'loop-manager/G-047')
                self.assertEqual(service.expected.remote.oid, oid)
            for oid in (None, 'external'):
                self.remote = oid
                with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
                    lm._current_rebase_service(self.fixture.connection, Path('/repo'),
                        Path('/state'), 'repo', 'G-047', Path('/state/G-047'), 'loop-manager/G-047')

    def test_blocked_release_publishes_clean_head_before_restoring_staged_work(self):
        self.assert_blocked_release()

    def test_blocked_publication_interruption_restores_stage_once_before_requeue(self):
        self.assert_blocked_release(interrupted=True)

    def test_blocked_publication_rejection_keeps_stage_snapshot_and_retries_same_intent(self):
        self.assert_blocked_release(rejected=True)

    def test_blocked_publication_retry_finishes_saved_target_after_base_advances(self):
        self.assert_blocked_release(rejected=True, advanced_base=True)

    def test_plan_blocks_recovers_pushed_checkpoint_before_old_remote_check(self):
        self.assert_blocked_release(interrupted=True, entrypoint=True)

    def test_plan_blocks_retries_saved_publication_before_old_remote_check(self):
        self.assert_blocked_release(rejected=True, advanced_base=True, entrypoint=True)

    def assert_blocked_release(self, *, interrupted=False, rejected=False,
                               advanced_base=False, entrypoint=False):
        from managed_rebase_validation import FileChange
        connection, runtime = self.fixture.connection, self.fixture.repository
        runtime.transition_and_clear('repo', 'G-047', plan_values={
            'status': 'PLAN_BLOCKED', 'blocked_fingerprint': 'stored'}, enqueue=False)
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo' AND plan_id='G-047'").fetchone()
        branch, worktree = 'loop-manager/G-047', '/state/G-047'
        before = lm.GitSnapshot('before', branch,
            {'app.py': ('H 100644 staged-blob 0',)}, {'app.py': (('staged',), 'saved-content')},
            {f'refs/heads/{branch}': ('before', '')},
            {worktree: ('HEAD=before', f'branch=refs/heads/{branch}')}, {}, {})
        local = {'head': 'before', 'staged': True, 'base': 'base'}
        def capture(_):
            return replace(before, head=local['head'],
                index=before.index if local['staged'] else {'app.py': ('H 100644 rebased-blob 0',)},
                dirty={'app.py': (('staged',) if local['staged'] else ('unstaged',), 'saved-content')},
                refs={f'refs/heads/{branch}': (local['head'], '')},
                worktrees={worktree: ('HEAD=' + local['head'], f'branch=refs/heads/{branch}')})
        original_git = self.git
        def git(path, *args, **kwargs):
            if args == ('rev-parse', 'HEAD'):
                return SimpleNamespace(returncode=0, stdout=local['head'])
            if args == ('rev-parse', 'refs/remotes/origin/main'):
                return SimpleNamespace(returncode=0, stdout=local['base'])
            if args == ('merge-base', '--is-ancestor', 'refs/remotes/origin/main', 'HEAD'):
                return SimpleNamespace(returncode=1, stdout='')
            return original_git(path, *args, **kwargs)
        def rebase(*_a, **_k):
            local['head'] = 'owned-head'
            local['staged'] = False
            return SimpleNamespace(returncode=0, stdout='')
        def restore(_worktree, snapshot):
            self.assertEqual(snapshot, before)
            self.assertEqual(self.remote, 'owned-head')
            self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
            self.events.append('restore-staging')
            local['staged'] = True
            self.seed.observed = replace(self.seed.observed, index_clean=False,
                staged=(FileChange('app.py', '100644', '100644', 'old', 'staged-blob', 'M'),))
        self.git = git
        with self.direct_ports(), ExitStack() as ports:
            for patch in (
                mock.patch.object(lm, '_plan_block_selected_for_release', return_value=plan),
                mock.patch.object(lm, '_revision_execplan_input_fingerprint', return_value='changed'),
                mock.patch.object(lm, '_revision_review_issue_fingerprint', return_value=None),
                mock.patch.object(lm, '_worktree_execplan_input_fingerprint', return_value='stored'),
                mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                mock.patch.object(lm, 'make_worktree'),
                mock.patch.object(lm, 'validate_blocked_agent_scope'),
                mock.patch.object(lm, 'preserve_blocked_agent_changes', return_value=before),
                mock.patch.object(lm, '_journaled_direct_rebase', side_effect=rebase),
                mock.patch.object(lm, '_blocked_execplan_metadata_error', return_value=None),
                mock.patch.object(lm, 'capture_git_snapshot', side_effect=capture),
                mock.patch.object(lm, '_owned_rebase_journal_state', return_value={'clean': 'rebase result'}),
                mock.patch.object(lm, '_restore_agent_staged_entries', side_effect=restore),
                mock.patch.object(Path, 'is_dir', return_value=True),
            ):
                ports.enter_context(patch)
            if interrupted:
                class Exit(BaseException):
                    pass
                with mock.patch.object(ExecutionStatePort, 'complete_rebase_publication', side_effect=Exit()):
                    with self.assertRaises(Exit):
                        lm.resume_blocked_execplan_if_inputs_changed(
                            connection, Path('/repo'), 'repo', 'main', Path('/state'))
                pending = self.checkpoints.get('repo', 'G-047')
                self.assertEqual(pending.operation, 'rebase_publish')
                self.assertEqual(self.events, ['push'])
                self.assertFalse(local['staged'])
                self.assertEqual(runtime.get('repo', 'G-047').publication_remote_oid, 'remote')
            if rejected:
                self.push_failure = True
                self.assertFalse(lm.resume_blocked_execplan_if_inputs_changed(
                    connection, Path('/repo'), 'repo', 'main', Path('/state')))
                pending = self.checkpoints.get('repo', 'G-047')
                self.assertEqual(pending.operation, 'rebase_publish')
                self.assertEqual(pending.snapshot['blocked_resume']['before_snapshot'], before.serialize())
                self.assertEqual(self.remote, 'remote')
                self.assertEqual(self.events, ['push'])
                self.assertFalse(local['staged'])
                self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
                self.push_failure = False
            if advanced_base:
                local['base'] = 'later-base'
            if entrypoint:
                with mock.patch.object(lm, '_reconcile_published_blocked_checkpoint',
                                       side_effect=AssertionError('old remote checked before recovery')) as reconcile:
                    # A retry refusal must preserve the pending release instead
                    # of falling through to the stale checkpoint remote gate.
                    with mock.patch.object(lm, '_complete_direct_rebase_checkpoint',
                                           side_effect=RuntimeError('temporary publication failure')):
                        self.assertFalse(lm.resume_plan_blocks(
                            connection, Path('/repo'), Path('/state'), 'repo', 'main'))
                    self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                    self.assertEqual(runtime.get('repo', 'G-047').status, 'PLAN_BLOCKED')
                    self.assertFalse(local['staged'])
                    self.assertTrue(lm.resume_plan_blocks(
                        connection, Path('/repo'), Path('/state'), 'repo', 'main'))
                    reconcile.assert_not_called()
            else:
                self.assertTrue(lm.resume_blocked_execplan_if_inputs_changed(
                    connection, Path('/repo'), 'repo', 'main', Path('/state')))
            self.assertEqual(self.checkpoints.get('repo', 'G-047').target_base_oid, 'base')
            self.assertEqual(runtime.get('repo', 'G-047').status, 'READY')
            self.assertIsNone(runtime.get('repo', 'G-047').verify_result)
            if interrupted or rejected:
                self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                                 pending.snapshot['rebase_publication_intent'])
        self.assertEqual(self.events, ['push', 'push', 'restore-staging'] if rejected else ['push', 'restore-staging'])
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.observed_remote_oid, 'owned-head')
        self.assertEqual(saved.snapshot['blocked_resume']['owner_oid'], 'remote')
        self.assertEqual(runtime.get('repo', 'G-047').publication_remote_oid, 'owned-head')
        self.assertEqual(capture(None).index, before.index)
        self.assertEqual(capture(None).dirty, before.dirty)
        runtime.activate('repo', 'G-047', activation_sha='base', branch=branch, worktree=worktree)
        self.restart()
        self.handler._fetch_latest_base = mock.Mock(return_value='base')
        recovered = self.handler.recover(self.handler._context())
        self.assertIsInstance(recovered, handlers.ExecutionContext)
        self.assertEqual(recovered.phase, ExecutionPhase.IMPLEMENTING)
        self.assertEqual(self.seed.observed.staged[0].new_oid, 'staged-blob')
        self.assertEqual(len(self.pushes), 2 if rejected else 1)
        if rejected:
            self.assertEqual(self.pushes[0], self.pushes[1])
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()

    def test_completion_publishes_before_candidate_work_without_historical_receipts(self):
        self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(self.pushes, [('push',
            '--force-with-lease=refs/heads/loop-manager/G-047:remote', 'origin',
            'owned-head:refs/heads/loop-manager/G-047')])
        self.assertIsNone(saved.operation)
        self.assertEqual(saved.observed_remote_oid, 'owned-head')
        self.assertEqual(saved.snapshot['last_adopted_remote_oid'], 'owned-head')
        self.assertEqual(saved.snapshot['failure'], 'old failure')
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-worker')
        for key in ('worker_result', 'verify_result', 'review_result', 'publication_token'):
            self.assertIsNone(self.state.row()[key])
        self.fixture.effects.agent.run_agent.assert_not_called()
        self.fixture.effects.validation.verify.assert_not_called()
        self.fixture.effects.publication.push_managed_publication_branch.assert_not_called()
        self.fixture.effects.publication.command.assert_not_called()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), saved)
        self.assertEqual(len(self.pushes), 1)

    def test_interruption_before_push_reuses_intent_before_later_base_fetch(self):
        class Exit(BaseException):
            pass
        self.before_push = lambda: (_ for _ in ()).throw(Exit())
        with self.assertRaises(Exit):
            self.complete()
        intent = self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent']
        self.before_push = None
        self.restart()
        def fetch():
            self.events.append('fetch-later-base')
            raise Exit()
        self.handler._fetch_latest_base = fetch
        with self.assertRaises(Exit):
            self.handler.guard(self.handler._context())
        self.assertEqual(self.events[-2:], ['push', 'fetch-later-base'])
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'], intent)
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')

    def test_interruption_after_push_confirms_once_without_repeating_push(self):
        class Exit(BaseException):
            pass
        with mock.patch.object(self.state, 'complete_rebase_publication', side_effect=Exit()):
            with self.assertRaises(Exit):
                self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(pending.operation, 'rebase_publish')
        self.assertEqual(self.state.row()['publication_remote_oid'], 'remote')
        self.restart()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['rebase_publication_intent'], pending.snapshot['rebase_publication_intent'])
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.checkpoints.get('repo', 'G-047'), saved)
        self.assertEqual(len(self.pushes), 1)

    def test_transient_push_failure_retains_same_lease_and_intent(self):
        self.push_failure = True
        with self.assertRaises(handlers.RecoveryFailure):
            self.complete()
        pending = self.checkpoints.get('repo', 'G-047')
        self.push_failure = False
        self.restart()
        self.handler._publish_rebased_checkpoint(self.handler._context())
        self.assertEqual(self.pushes[0], self.pushes[1])
        self.assertEqual(self.checkpoints.get('repo', 'G-047').snapshot['rebase_publication_intent'],
                         pending.snapshot['rebase_publication_intent'])

    def test_remote_race_deletion_or_new_pr_never_replaces_intent_lease(self):
        for raced in ('external-commit', None, 'pr'):
            with self.subTest(raced=raced):
                self.setUp()
                self.push_failure = True
                with self.assertRaises(handlers.RecoveryFailure):
                    self.complete()
                pending = self.checkpoints.get('repo', 'G-047')
                if raced == 'pr':
                    self.fixture.effects.publication.pr_for_branch.return_value = {'url': 'new-pr'}
                else:
                    self.remote = raced
                self.push_failure = False
                with self.assertRaises(handlers.RecoveryFailure):
                    self.handler._publish_rebased_checkpoint(self.handler._context())
                self.assertEqual(len(self.pushes), 1)
                self.assertEqual(self.checkpoints.get('repo', 'G-047'), pending)
                self.assertEqual(self.state.row()['publication_remote_oid'], 'remote')

    def test_new_branch_existing_pr_or_different_owner_never_gets_early_push(self):
        for excluded in ('new-branch', 'existing-pr', 'other-owner'):
            with self.subTest(excluded=excluded):
                self.setUp()
                if excluded == 'new-branch':
                    self.seed.expected = replace(self.seed.expected,
                        remote=replace(self.seed.expected.remote, oid=None))
                    self.seed.observed = replace(self.seed.observed, remote=self.seed.expected.remote)
                    current = self.checkpoints.get('repo', 'G-047')
                    self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
                        operation='rebase', expected_head='before', target_base_oid='base',
                        observed_remote_oid=None, snapshot=current.snapshot)
                    self.state.update_project('repo', publication_remote_oid=None)
                elif excluded == 'existing-pr':
                    self.state.update_project('repo', pr_url='existing-pr')
                else:
                    self.seed.observed = replace(self.seed.observed, branch='another-plan')
                with mock.patch.object(self.handler, '_owned_checkpoint_remote', return_value=True):
                    if excluded == 'other-owner':
                        with self.assertRaises(RuntimeError):
                            self.complete()
                    elif excluded == 'existing-pr':
                        # Existing PR rebase still uses the ordinary path; there
                        # is no intermediate publication entry for its branch.
                        saved = self.checkpoints.get('repo', 'G-047')
                        from tests.test_managed_rebase_validation import decision_for
                        accepted = decision_for(self.seed.expected, self.seed.observed).acceptance
                        self.checkpoints.accept_current_rebase('repo', 'G-047', acceptance=accepted)
                        self.handler._publish_rebased_checkpoint(self.handler._context())
                    else:
                        self.complete()
                self.assertEqual(self.pushes, [])

    def test_same_oid_completes_without_push_and_next_rebase_uses_adopted_oid(self):
        self.remote = 'owned-head'
        self.seed.expected = replace(self.seed.expected, remote=replace(self.seed.expected.remote, oid=self.remote))
        self.seed.observed = replace(self.seed.observed, remote=self.seed.expected.remote)
        self.state.update_project('repo', publication_remote_oid=self.remote)
        current = self.checkpoints.get('repo', 'G-047')
        self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head='before', target_base_oid='base',
            observed_remote_oid=self.remote, snapshot=current.snapshot)
        with mock.patch.object(self.handler, '_owned_checkpoint_remote', return_value=True):
            self.complete()
        self.assertEqual(self.pushes, [])
        with mock.patch.object(handlers.manager_git_journal, 'begin_current_rebase'):
            self.handler._record_rebase_intent(self.handler._context(), 'later-base')
        current = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(current.operation, 'rebase')
        self.assertEqual(current.observed_remote_oid, 'owned-head')
        self.assertEqual(current.target_base_oid, 'later-base')

    def test_final_push_resume_uses_latest_adopted_oid_instead_of_original_checkpoint(self):
        self.complete()
        current = self.checkpoints.get('repo', 'G-047')
        pending = self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation='push', expected_head='worker-head', target_base_oid='base',
            observed_remote_oid='owned-head', snapshot={**current.snapshot, 'remote_before': 'owned-head'})
        self.seed.observed = replace(self.seed.observed, head='worker-head',
            branch_ref=('worker-head', ''),
            registration=('HEAD=worker-head', 'branch=refs/heads/loop-manager/G-047'),
            remote=replace(self.seed.expected.remote, oid='worker-head'))
        self.assertTrue(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'worker-head', pending))
        self.assertFalse(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'unrelated-head', pending))
        # A restart may see the confirmed runtime head while the final push
        # checkpoint still holds the intermediate checkpoint's fixed lease.
        self.state.update_project('repo', publication_remote_oid='worker-head')
        self.assertTrue(self.handler._owned_checkpoint_remote(self.handler._context(),
            self.state.row(), 'worker-head', pending))
        for unowned_before in ('remote', 'unrelated-head'):
            with self.subTest(unowned_before=unowned_before):
                changed = replace(pending, snapshot={**pending.snapshot,
                    'remote_before': unowned_before})
                self.assertFalse(self.handler._owned_checkpoint_remote(self.handler._context(),
                    self.state.row(), 'worker-head', changed))

    def test_publication_comparison_updates_only_selected_remote_tracking_ref(self):
        from managed_rebase_adoption import selected_baseline
        self.complete()
        branch, worktree = 'loop-manager/G-047', '/state/G-047'
        ref, remote_ref = f'refs/heads/{branch}', f'refs/remotes/origin/{branch}'
        before = lm.GitSnapshot('before', branch, {}, {},
            {ref: ('before', ''), remote_ref: ('remote', ''), 'refs/heads/other': ('original', '')},
            {worktree: ('HEAD=before',)}, {}, {})
        current = replace(before, head='owned-head', refs={ref: ('owned-head', ''),
            remote_ref: ('owned-head', ''), 'refs/heads/other': ('unauthorized', '')},
            worktrees={worktree: ('HEAD=owned-head',)})
        published = self.checkpoints.get('repo', 'G-047')
        accepted = selected_baseline(before, current, branch, worktree, publication=published)
        self.assertEqual(accepted.refs[remote_ref], ('owned-head', ''))
        self.assertEqual(accepted.refs['refs/heads/other'], ('original', ''))
        self.assertEqual(before.refs[remote_ref], ('remote', ''))
        tampered = replace(current, refs={**current.refs, remote_ref: ('foreign', '')})
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            selected_baseline(before, tampered, branch, worktree, publication=published)

    def test_existing_remote_without_durable_publication_identity_is_preserved(self):
        self.state.update_project('repo', publication_remote_oid=None)
        self.fixture.connection.execute("DELETE FROM blocked_checkpoints WHERE repo = 'repo'")
        with self.assertRaises(handlers.RecoveryFailure):
            self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(saved.expected_head, 'owned-head')
        self.assertNotIn('rebase_publication_intent', saved.snapshot)
        self.assertEqual(self.remote, 'remote')
        self.assertEqual(self.pushes, [])

    def test_legacy_completed_result_without_journal_or_completion_markers_publishes(self):
        current = self.checkpoints.get('repo', 'G-047')
        self.checkpoints.record('repo', 'G-047', expected_generation=current.generation,
            operation=None, expected_head='owned-head', target_base_oid='base',
            observed_remote_oid='remote', snapshot={'failure': 'preserved legacy audit'})
        original_git = self.git
        def legacy_git(path, *args, **kwargs):
            if args == ('merge-base', '--is-ancestor', 'base', 'remote'):
                return SimpleNamespace(returncode=1, stdout='')
            return original_git(path, *args, **kwargs)
        self.handler.effects.workspace.git.side_effect = legacy_git
        self.complete()
        saved = self.checkpoints.get('repo', 'G-047')
        self.assertEqual(self.state.row()['publication_remote_oid'], 'owned-head')
        self.assertEqual(saved.snapshot['failure'], 'preserved legacy audit')
        self.assertEqual(len(self.pushes), 1)
        self.assertNotIn('completed_rebase', saved.snapshot)
        self.assertNotIn('rebase_completion', saved.snapshot)


class EvidenceFreeBlockedScopeTests(TestCase):
    setUp = CurrentRebaseAgentScopeTests.setUp

    def validate(self):
        connection = self.case.fixture.connection
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo'").fetchone()
        with mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False), \
             mock.patch.object(lm, 'requirement_archive_candidates', return_value=()), \
             mock.patch.object(lm, '_proven_manager_base_refs', return_value=frozenset()):
            lm.validate_blocked_agent_scope(connection, Path('/repo'), 'repo', 'main',
                Path('/state/G-047'), plan['path'], accepted_rebase=True, saved_plan=plan)

    def test_manual_result_can_replace_selected_rebase_state_without_changing_saved_agent_start(self):
        self.validate()
        row = self.case.fixture.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(row['agent_snapshot'], self.original.serialize())

    def test_saved_actual_agent_ref_violation_is_checked_before_rebase_normalization(self):
        violation = replace(self.original, refs={**self.original.refs, 'refs/heads/other': ('foreign', '')})
        self.case.state.update_project('repo', agent_result_snapshot=violation.serialize())
        with self.assertRaises(lm.AgentScopeError):
            self.validate()

    def test_current_external_ref_and_default_worktree_mutation_remain_rejected(self):
        original = self.current
        for current in (replace(original, refs={**original.refs, 'refs/heads/other': ('foreign', '')}),
                        replace(original, worktrees={**original.worktrees, '/repo': ('HEAD=foreign',)})):
            with self.subTest(current=current):
                self.current = current
                with self.assertRaises(lm.AgentScopeError):
                    self.validate()

    def test_guidance_decision_resumes_after_accepted_rebase_with_original_agent_snapshot(self):
        connection = self.case.fixture.connection
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo'").fetchone()
        pending = lm.parse_guidance_document(guidance_plan().replace('G-020', 'G-047'))
        approved = lm.parse_guidance_document(
            guidance_plan(decision='approve', revision='user-1').replace('G-020', 'G-047'))
        with (mock.patch.object(lm, 'is_loop_manager_repository', return_value=False),
              mock.patch.object(lm, '_guidance_target_state', return_value=(b'# Agents\n', 0o100644)),
              mock.patch.object(lm, 'capture_git_snapshot', return_value=self.original)):
            lm._block_for_new_guidance(connection, Path('/repo'), 'repo', Path('/state/G-047'),
                                      plan, pending, pending.proposals, 'proposal')
        plan = connection.execute("SELECT * FROM exec_plans WHERE repo='repo'").fetchone()
        lm.hydrate_execplan_project(connection, 'repo', plan)
        self.assertEqual(plan['agent_snapshot'], self.original.serialize())
        with (mock.patch.object(lm, 'validate_execplan_workspace_identity'),
              mock.patch.object(lm, 'make_worktree'),
              mock.patch.object(lm, '_rebase_in_progress_for_branch', return_value=False),
              mock.patch.object(lm, 'is_loop_manager_repository', return_value=False),
              mock.patch.object(lm, '_guidance_document', return_value=pending) as decision_document,
              mock.patch.object(lm, '_guidance_target_state', return_value=(b'# Agents\n', 0o100644)),
              mock.patch.object(lm, '_published_plan_allows_common_plans', return_value=False),
              mock.patch.object(lm, 'requirement_archive_candidates', return_value=()),
              mock.patch.object(lm, '_proven_manager_base_refs', return_value=frozenset())):
            self.assertFalse(lm.resume_guidance_approval_if_decided(
                connection, Path('/repo'), 'repo', 'main', Path('/state'), plan_id='G-047'))
            self.assertEqual(connection.execute('SELECT agent_snapshot FROM exec_plans').fetchone()[0],
                             self.original.serialize())
            decision_document.return_value = approved
            self.assertTrue(lm.resume_guidance_approval_if_decided(
                connection, Path('/repo'), 'repo', 'main', Path('/state'), plan_id='G-047'),
                connection.execute("SELECT last_error FROM exec_plans WHERE repo='repo'").fetchone()[0])
        self.assertEqual(connection.execute('SELECT decision FROM guidance_approvals').fetchone()[0], 'approve')
        self.assertIsNone(scoped_blocking.plan_block(connection, 'repo', 'G-047'))

    def _published_tracking_ref(self):
        remote_ref = 'refs/remotes/origin/loop-manager/G-047'
        self.original = replace(self.original, refs={**self.original.refs, remote_ref: ('remote', '')})
        self.current = replace(self.current, refs={**self.current.refs, remote_ref: ('owned-head', '')})
        self.case.state.update_project('repo', agent_snapshot=self.original.serialize(), publication_remote_oid='owned-head')
        lm.blocked_checkpoint_state.BlockedCheckpointRepository(self.case.fixture.connection).request(
            repo='repo', plan_id='G-047', reason_code='verify_retry_exhausted', evidence='original cause',
            branch='loop-manager/G-047', worktree='/state/G-047', expected_remote_oid='remote', rebase_recovery=False)

    def test_owned_checkpoint_tracking_update_does_not_rewrite_original_agent_snapshot(self):
        self._published_tracking_ref()
        with mock.patch.object(lm, '_owned_checkpoint_remote_oid', return_value='owned-head') as ownership:
            self.validate()
        ownership.assert_called_once()
        self.assertEqual(self.case.fixture.connection.execute('SELECT agent_snapshot FROM exec_plans').fetchone()[0],
                         self.original.serialize())

    def test_unproven_checkpoint_tracking_update_remains_a_scope_violation(self):
        self._published_tracking_ref()
        with mock.patch.object(lm, '_owned_checkpoint_remote_oid', return_value=None):
            with self.assertRaises(lm.AgentScopeError):
                self.validate()


class EvidenceFreeRecoveryLifecycleTests(TestCase):
    """Manager phase policies with SQLite and fake Git/Agent/process adapters."""

    def setUp(self):
        from tests.test_scoped_blocking import EvidenceFreeRebaseBlockTests
        self.blocked = EvidenceFreeRebaseBlockTests()
        self.blocked.setUp()
        self.addCleanup(self.blocked.doCleanups)
        self.case = self.blocked.case
        self.fixture = self.case.fixture
        self.handler, self.state = self.case.handler, self.case.state
        workspace = recovered_rebase_workspace(self.handler.effects.workspace,
            connection=self.fixture.connection, repo_path=self.handler.repo_path,
            state_dir=self.handler.state_dir, repo='repo', plan_id='G-047',
            worktree=Path('/state/G-047'))
        # Substitute external Git effects after constructing the same bindings
        # as the real lifecycle fixture; no Git process runs in these cases.
        self.effects = replace(self.handler.effects, workspace=replace(workspace,
            _rebase_in_progress_for_branch=mock.Mock(side_effect=lambda *_: self.pending),
            git_operation_entries=mock.Mock(side_effect=lambda *_:
                {'rebase-merge': 'operation'} if self.pending else {})))
        self.handler.effects = self.effects
        self.case.expected = replace(self.case.expected,
            input_fingerprint='changed-plan-and-source',
            remote=replace(self.case.expected.remote, oid=None, repository='repo'))
        self.case.observed = replace(self.case.observed,
            input_fingerprint=self.case.expected.input_fingerprint,
            remote=self.case.expected.remote)
        self.final_head = 'c' * 40
        self.case.observed = replace(self.case.observed, head=self.final_head,
            branch_ref=(self.final_head, ''), registration=(
                f'HEAD={self.final_head}', 'branch=refs/heads/loop-manager/G-047'))
        self.completed = self.case.observed
        self.pending = True
        self.remote = None
        self.lifecycle = []
        self.worker_result = (
            'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
            'COMMIT_TITLE: Preserve current manual conflict resolution\n'
            'PR_SUMMARY: Recheck the changed plan and current diff.\n'
            'TARGETED_TESTS:\ncurrent candidate unit case: OK\n'
        )
        self.effects.validation.parse_worker_result.side_effect = lm.parse_worker_result
        self.effects.revision.body_hash.return_value = 'changed-plan-body'
        self.effects.revision.source_fingerprint.return_value = 'changed-source'
        self.effects.workspace.git.side_effect = self.git
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: self.remote
        self.effects.agent.capture_git_snapshot.side_effect = lambda *_: lm.GitSnapshot(
            'partial' if self.pending else self.final_head,
            None if self.pending else 'loop-manager/G-047', {},
            {'app.py': (('staged',), 'manual')} if self.pending else {}, {}, {}, {}, {})
        self.effects.agent.run_agent.side_effect = self.agent
        self.effects.validation.verify.side_effect = (
            lambda _: (self.lifecycle.append('verify') or True, 'fresh candidate: OK'))
        self.effects.publication.command.side_effect = (
            lambda *_args, **_kwargs: SimpleNamespace(stdout='https://example.test/pr/47'))
        saved = self.case.checkpoint.get('repo', 'G-047')
        # There is no saved Worker correction, stage approval or journal proof.
        self.case.checkpoint.record('repo', 'G-047', expected_generation=saved.generation,
            operation='rebase', expected_head='before', target_base_oid='base',
            observed_remote_oid=None, snapshot={'failure': 'old failure'})
        self.blocked.block()
        self.blocked.pending(staged=True)
        self.assertTrue(self.blocked.resume())
        self.assertIsNone(scoped_blocking.plan_block(self.fixture.connection, 'repo', 'G-047'))
        self.assertTrue(self.pending)
        self.effects.workspace.git.assert_not_called()
        # A resumed Manager opens a new port bound to the released execution.
        self.state = ExecutionStatePort(self.fixture.connection, self.fixture.repository, 'repo')
        self.case.state = self.fixture.state = self.state
        with mock.patch.object(lm, 'push_managed_publication_branch', side_effect=self.push), \
                mock.patch.object(lm, 'push_owned_pr_conflict_recovery', side_effect=self.push):
            publication = recovered_rebase_publication(self.effects.publication, self.state)
        self.effects = replace(self.effects, state=self.state, publication=publication)
        self.handler.effects = self.effects
        self.handler.catalog_plan = self.state.active_execplan('repo')
        self.coordinator = ExecutionPhaseCoordinator(self.handler)
        for patch in (
                mock.patch.object(lm, 'git', side_effect=self.git),
                mock.patch.object(lm, '_owned_publication_plan',
                                  side_effect=lambda *_: self.state.active_execplan('repo'))):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, _path, *args, **_kwargs):
        self.assertNotIn(args[0], ('add', 'commit', 'checkout', 'reset'),
                         'the manually staged candidate must be preserved')
        if 'rebase' in args:
            self.assertIn('--continue', args)
            self.assertTrue(self.pending)
            self.pending = False
            self.case.observed = self.completed
            self.lifecycle.append('manager_continue')
        output, code = '', 0
        if args == ('rev-parse', 'HEAD'):
            output = 'partial' if self.pending else self.final_head
        elif args == ('rev-parse', '--verify', 'refs/heads/loop-manager/G-047'):
            output = self.final_head
        elif args[:2] == ('rev-parse', '--git-path'):
            code = 1
        elif args[0] == 'rev-parse':
            output = 'base'
        elif args[0] == 'symbolic-ref':
            output, code = ('', 1) if self.pending else ('loop-manager/G-047', 0)
        elif args == ('diff', '--cached', '--quiet'):
            code = int(self.pending)
        elif args[:2] == ('ls-files', '--cached'):
            output = 'app.py\0'
        elif args[0] == 'ls-remote' and self.remote:
            output = f'{self.remote}\trefs/heads/loop-manager/G-047\n'
        return SimpleNamespace(stdout=output, returncode=code)

    def agent(self, *args, **kwargs):
        role = args[4]
        if role == 'worker':
            self.assertEqual(kwargs['session_id'], 'saved-worker')
            self.assertIn('현재 HEAD의 base 대비 최종 diff 전체', args[5])
            self.assertIn('targeted unit test', args[5])
            self.assertEqual(self.handler._definition_snapshot.body_hash, 'changed-plan-body')
            self.assertEqual(self.handler._definition_snapshot.source_fingerprint, 'changed-source')
            self.lifecycle.extend(('worker_confirmation', 'targeted_tests'))
            kwargs['on_session_started']('saved-worker')
            return lm.AgentResult(0, self.worker_result, session_id='saved-worker')
        self.assertEqual(role, 'reviewer')
        self.lifecycle.append('review')
        return lm.AgentResult(0, 'VERDICT: PASS', session_id='review-session')

    def push(self, connection, *_args):
        self.assertEqual(self.state.row()['verify_result'], 'fresh candidate: OK')
        self.assertEqual(self.state.row()['review_result'], 'VERDICT: PASS')
        current = self.handler._definition_snapshot
        self.assertEqual(self.state.row()['verify_token'], current.verify_token('base', self.final_head))
        self.assertEqual(self.state.row()['review_token'], current.review_token('base', self.final_head))
        self.remote = self.final_head
        self.case.expected = replace(self.case.expected,
            remote=replace(self.case.expected.remote, oid=self.remote))
        self.case.observed = replace(self.case.observed, remote=self.case.expected.remote)
        self.lifecycle.append('publication')
        lm.update_project(connection, 'repo', publication_remote_oid=self.remote)
        return self.remote

    def recover(self):
        result = self.handler.recover(self.handler._context())
        self.assertIsInstance(result, handlers.ExecutionContext)
        self.assertEqual(result.phase, ExecutionPhase.IMPLEMENTING)
        self.assertFalse(self.pending)
        self.assertIsNone(self.case.checkpoint.get('repo', 'G-047').operation)
        for field in ('worker_result', 'verify_result', 'review_result',
                      'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(self.state.row()[field], field)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()

    def confirm_worker(self):
        context = self.handler.guard(self.handler._context())
        self.assertIsInstance(context, handlers.ExecutionContext)
        outcome = self.coordinator.dispatch(context)
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        saved = self.case.checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.snapshot['worker_candidate_token'],
                         self.handler._definition_snapshot.worker_candidate_token(self.final_head))

    def existing_pr(self):
        from managed_rebase_validation import PullRequest
        self.remote = self.prior_remote = 'a' * 40
        pr = PullRequest('https://example.test/pr/47', 'repo', 'loop-manager/G-047',
                         self.remote, 'OPEN')
        self.case.expected = replace(self.case.expected,
            remote=replace(self.case.expected.remote, oid=self.remote, pull_request=pr))
        self.case.observed = replace(self.case.observed, remote=self.case.expected.remote)
        self.completed = replace(self.completed, remote=self.case.expected.remote)
        current = self.case.checkpoint.get('repo', 'G-047')
        self.case.checkpoint.record('repo', 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=self.remote,
            snapshot=current.snapshot)
        self.state.update_project('repo', pr_url=pr.identity, pr_head_oid=pr.head)
        self.effects.publication.pr_for_branch.return_value = dict(
            url=pr.identity, state=pr.state, headRefName=pr.branch,
            headRefOid=pr.head, baseRefName='main')

    def test_current_pr_lease_survives_recovery_and_fresh_publication(self):
        self.existing_pr()
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: 'stale-cache'
        self.recover()
        self.handler.recover(self.handler._context())
        self.effects.workspace.remote_branch_oid.assert_not_called()
        self.confirm_worker()
        for _ in range(2):
            self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                             ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects.publication.push_owned_pr_conflict_recovery.assert_not_called()
        self.state.update_project('repo', pr_conflict_recovery=1,
            pr_conflict_head_oid=self.prior_remote, pr_conflict_base_oid='base')
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: self.remote
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.lifecycle, ['manager_continue', 'worker_confirmation',
            'targeted_tests', 'verify', 'review', 'publication'])
        self.effects.publication.push_owned_pr_conflict_recovery.assert_called_once()
        self.effects.publication.command.assert_not_called()
        self.assertEqual(self.state.repository.get('repo', 'G-047').status, 'PR_OPEN')

    def test_absent_live_remote_ignores_stale_tracking_ref_after_adoption(self):
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: 'stale-cache'
        self.recover()
        result = self.handler.recover(self.handler._context())
        self.assertIsInstance(result, handlers.ExecutionContext)
        self.effects.workspace.remote_branch_oid.assert_not_called()
        self.effects.agent.run_agent.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()

    def test_stale_cache_recovery_reaches_actual_push_adapter_after_fresh_validation(self):
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: 'stale-cache'
        self.recover()
        self.confirm_worker()
        for _ in range(2):
            self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                             ExecutionOutcomeKind.PHASE_ADVANCED)
        self.effects = replace(self.effects, publication=recovered_rebase_publication(
            self.effects.publication, self.state))
        self.handler.effects = self.effects
        pushes = []
        def git(path, *args, **kwargs):
            if args[0] == 'push':
                self.assertEqual(self.state.row()['verify_result'], 'fresh candidate: OK')
                self.assertEqual(self.state.row()['review_result'], 'VERDICT: PASS')
                pushes.append(args)
                self.remote = self.final_head
                self.case.expected = replace(self.case.expected,
                    remote=replace(self.case.expected.remote, oid=self.remote))
                self.case.observed = replace(self.case.observed, remote=self.case.expected.remote)
                self.lifecycle.append('publication')
            return self.git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = git
        with mock.patch.object(lm, 'git', side_effect=git), \
                mock.patch.object(lm, '_recorded_base_fetch') as fetch, \
                mock.patch.object(lm, 'pr_for_branch', return_value=None) as pr_query, \
                mock.patch.object(lm, 'remote_branch_oid', return_value='stale-cache') as cached:
            result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(pushes, [('push',
            '--force-with-lease=refs/heads/loop-manager/G-047:', 'origin',
            f'{self.final_head}:refs/heads/loop-manager/G-047')])
        fetch.assert_called_once()
        pr_query.assert_called_once_with(self.handler.repo_path, 'loop-manager/G-047')
        cached.assert_not_called()
        self.effects.workspace.remote_branch_oid.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_called_once()
        self.effects.publication.command.assert_called_once()
        self.assertEqual(self.lifecycle, ['manager_continue', 'worker_confirmation',
            'targeted_tests', 'verify', 'review', 'publication'])
        row = self.state.repository.get('repo', 'G-047')
        self.assertEqual(row.status, 'PR_OPEN')
        self.assertEqual(row.publication_remote_oid, self.final_head)

    def test_live_remote_creation_before_publication_is_rejected_despite_absent_cache(self):
        self.recover()
        self.confirm_worker()
        for _ in range(2):
            self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                             ExecutionOutcomeKind.PHASE_ADVANCED)
        self.remote = 'b' * 40
        self.effects.workspace.remote_branch_oid.side_effect = lambda *_: None
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()
        self.assertNotIn('publication', self.lifecycle)

    def test_live_remote_change_after_adoption_keeps_publication_closed(self):
        self.existing_pr()
        self.recover()
        self.remote = 'b' * 40
        self.case.observed = replace(self.case.observed,
            remote=replace(self.case.observed.remote, oid=self.remote))
        result = self.handler.recover(self.handler._context())
        self.assertIsInstance(result, ExecutionOutcome)
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_owned_pr_conflict_recovery.assert_not_called()
        self.effects.publication.command.assert_not_called()

    def test_existing_remote_head_creates_pr_without_repeated_push(self):
        self.recover()
        self.confirm_worker()
        for _ in range(2):
            self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                             ExecutionOutcomeKind.PHASE_ADVANCED)
        # A push completed before its remote observation was recorded. The
        # actual record adapter must refresh the snapshot for the next guard.
        self.remote = self.final_head
        self.case.expected = replace(self.case.expected,
            remote=replace(self.case.expected.remote, oid=self.remote))
        self.case.observed = replace(self.case.observed, remote=self.case.expected.remote)
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        expected_record = mock.call(Path('/repo'), Path('/state'), 'repo',
                                    'loop-manager/G-047', Path('/state/G-047'), self.final_head)
        self.assertEqual(self.effects.publication.record_remote_publication_branch.call_args_list,
                         [expected_record, expected_record])
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_called_once()
        row = self.state.repository.get('repo', 'G-047')
        self.assertEqual(row.status, 'PR_OPEN')
        self.assertEqual(row.publication_remote_oid, self.final_head)

    def test_manual_stage_continue_rechecks_worker_verify_review_and_publication_gate(self):
        self.recover()
        self.handler.recover(self.handler._context())
        self.assertEqual(self.lifecycle, ['manager_continue'])
        self.confirm_worker()
        self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                         ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                         ExecutionOutcomeKind.PHASE_ADVANCED)
        self.state.update_project('repo', review_token='old candidate review')
        stale = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(stale.kind, ExecutionOutcomeKind.RETRY)
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                         ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.coordinator.dispatch(self.handler._context()).kind,
                         ExecutionOutcomeKind.SUCCESS)
        self.assertEqual(self.lifecycle, ['manager_continue', 'worker_confirmation',
            'targeted_tests', 'verify', 'review', 'review', 'publication'])
        self.assertEqual(self.state.repository.get('repo', 'G-047').status, 'PR_OPEN')
        self.effects.publication.command.assert_called_once()

    def test_old_success_results_cannot_publish_without_current_worker_confirmation(self):
        self.recover()
        self.state.update_project('repo', phase='PUBLISHING', worker_result=self.worker_result,
            verify_result='old verify', review_result='VERDICT: PASS', validated_base_sha='base',
            worker_input_token='old-input', verify_token='old-verify', review_token='old-review')
        # Coordinator runs guard before dispatching the saved PUBLISHING phase.
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.effects.agent.run_agent.assert_not_called()
        self.effects.validation.verify.assert_not_called()
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()

    def test_unsuccessful_fresh_verify_does_not_open_review_or_publication(self):
        self.recover()
        self.confirm_worker()
        self.effects.validation.verify.side_effect = lambda _: (False, 'current candidate rejected')
        def rejected(_repo, _worktree, _stage, output):
            self.state.update_project('repo', phase='WORKING', validated_base_sha=None,
                                     verify_result=output, last_error=output)
            return SimpleNamespace(blocked=False, fingerprint='current-verify')
        self.effects.validation.transition_validation_failure.side_effect = rejected
        result = self.coordinator.dispatch(self.handler._context())
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIsNone(self.state.row()['review_token'])
        self.assertNotIn('review', self.lifecycle)
        self.effects.publication.push_managed_publication_branch.assert_not_called()
        self.effects.publication.command.assert_not_called()


class EvidenceFreeRebasePreparationTests(TestCase):
    def setUp(self):
        self.case = current_rebase_case(self)
        self.case.fixture.effects.workspace.is_directory.return_value = True
        self.case.handler.catalog_plan = self.case.state.active_execplan('repo')

    def pending(self):
        from managed_rebase_validation import IndexEntry
        self.case.observed = replace(self.case.observed, head='partial', branch=None,
            branch_ref=('before', ''), registration=('HEAD=partial', 'detached=True'),
            branch_worktrees=(), operations=(('rebase-merge', 'operation'),),
            metadata=(('head-name', 'refs/heads/loop-manager/G-047'),
                      ('orig-head', 'before'), ('onto', 'base')),
            index=(IndexEntry('app.py', '100644', 'manual', 0, 'H', 0),), index_clean=False)

    def test_owned_detached_rebase_is_recognized_without_old_error_marker(self):
        self.pending()
        self.case.state.update_project('repo', last_error='different historical failure')
        self.assertTrue(self.case.handler._resolving_rebase(self.case.state.row()))

    def test_detached_metadata_alone_cannot_bypass_current_remote_or_target_check(self):
        self.pending()
        original = self.case.observed
        for observed in (replace(original, remote=replace(original.remote, oid='foreign')),
                         replace(original, metadata=(('head-name', 'refs/heads/loop-manager/G-047'),
                                                     ('orig-head', 'before'), ('onto', 'foreign')))):
            with self.subTest(observed=observed):
                self.case.observed = observed
                with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
                    self.case.handler._resolving_rebase(self.case.state.row())

    def test_initial_and_detached_context_validate_current_existing_pr_lease(self):
        from managed_rebase_validation import PullRequest
        remote = replace(self.case.expected.remote, repository='repo', oid='remote', pull_request=PullRequest(
            'https://example.test/pr/47', 'repo', 'loop-manager/G-047',
            'remote', 'OPEN'))
        self.case.expected = replace(self.case.expected, remote=remote)
        self.case.observed = replace(self.case.observed, remote=remote)
        self.case.state.update_project('repo', pr_url=remote.pull_request.identity, pr_head_oid=remote.oid)
        self.assertFalse(self.case.handler._context().resolving_rebase)
        self.pending()
        self.assertTrue(self.case.handler._context().resolving_rebase)
        self.case.observed = replace(self.case.observed, remote=replace(remote, pull_request=None))
        with self.assertRaises(handlers.manager_git_journal.MutationEvidenceError):
            self.case.handler._context()


class TaskRelevantPublicationBoundaryTests(TestCase):
    def setUp(self):
        self.case = ExecutionHandlerTests(methodName='runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.handler = self.case.handler
        self.effects = self.case.effects

    def observation(self):
        context = self.case.context('PUBLISHING')
        return self.handler._publication_resume().observe(
            context.repo_path, context.worktree, context.branch, None)

    def test_publication_reads_live_remote_instead_of_unused_tracking_cache(self):
        live = mock.Mock(return_value='base')
        self.handler.effects = replace(self.effects, workspace=replace(
            self.effects.workspace, live_remote_branch_oid=live))
        self.effects.workspace.remote_branch_oid.return_value = 'stale'
        self.assertEqual(self.observation().remote_oid, 'base')
        self.effects.workspace.remote_branch_oid.assert_not_called()
        live.assert_called_once()
        self.assertEqual(self.effects.workspace.validate_managed_worktree.call_count, 2)

    def test_observation_rejects_head_change_during_pr_query(self):
        self.effects.publication.pr_for_branch.side_effect = lambda *args: setattr(self.case, 'remote', 'changed')
        with self.assertRaises(lm.PublicationSafetyError):
            self.observation()

    def test_observation_rechecks_selected_worktree_ownership(self):
        self.effects.workspace.validate_managed_worktree.side_effect = [None, lm.WorkspaceOwnershipError('registration changed')]
        with self.assertRaises(lm.WorkspaceOwnershipError):
            self.observation()
        self.effects.publication.command.assert_not_called()

    def test_candidate_changed_since_review_cannot_be_observed_for_publication(self):
        self.case.review_context()
        self.case.state.update_project('repo', phase='PUBLISHING')
        self.case.remote = 'changed'
        with self.assertRaises(lm.PublicationSafetyError):
            self.observation()

    def test_remote_change_between_push_and_pr_creation_stops_creation(self):
        self.case.state.update_project('repo', validated_base_sha='base')
        live = mock.Mock(side_effect=[None, 'competitor'])
        self.handler.effects = replace(self.effects, workspace=replace(
            self.effects.workspace, live_remote_branch_oid=live))
        result = self.handler._publish_new_pull_request(self.case.context('PUBLISHING'), self.case.state.row())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.command.assert_not_called()

    def test_merged_pr_with_deleted_head_after_push_is_reconciled(self):
        self.handler._owned_checkpoint_remote = mock.Mock(side_effect=lambda *args:
            'push' not in self.case.calls or args[2] is not None)
        self.case._assert_post_push_pr_attachment(
            'base', True, pr_state='MERGED', remote_deleted=True)
        self.assertEqual(self.handler._owned_checkpoint_remote.call_count, 2)
        pr = self.effects.publication.reconcile_existing_pull_request.call_args.args[-1]
        self.assertEqual(pr['state'], 'MERGED')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_merged_pr_with_advanced_base_and_deleted_head_is_reconciled(self):
        fetched_bases = []
        def fetch_base():
            base = 'merged-base' if 'push' in self.case.calls else 'base'
            fetched_bases.append(base)
            return base
        self.handler._fetch_latest_base = mock.Mock(side_effect=fetch_base)
        self.handler._latest_before_publication = mock.Mock(side_effect=lambda *_:
            self.assertNotIn('push', self.case.calls))
        self.handler._owned_checkpoint_remote = mock.Mock(side_effect=lambda *args:
            'push' not in self.case.calls or args[2] is not None)

        self.case._assert_post_push_pr_attachment(
            'base', True, pr_state='MERGED', remote_deleted=True)

        self.assertIn('merged-base', fetched_bases)
        self.handler._latest_before_publication.assert_called_once()
        pr = self.effects.publication.reconcile_existing_pull_request.call_args.args[-1]
        self.assertEqual(pr['state'], 'MERGED')
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_deleted_head_after_push_does_not_authorize_mismatched_or_open_pr(self):
        for state, head in (('MERGED', 'other-head'), ('OPEN', 'base')):
            with self.subTest(state=state):
                case = ExecutionHandlerTests(methodName='runTest')
                case.setUp()
                self.addCleanup(case.doCleanups)
                case._assert_post_push_pr_attachment(
                    head, False, pr_state=state, remote_deleted=True)

    def test_closed_pr_with_deleted_head_after_push_keeps_closed_pr_outcome(self):
        self.effects.publication.reconcile_existing_pull_request.side_effect = lambda *_: 1
        self.case._assert_post_push_pr_attachment(
            'base', True, pr_state='CLOSED', remote_deleted=True,
            reconciled_kind=ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_base_advance_before_pr_creation_returns_to_existing_revalidation_flow(self):
        self.case.state.update_project('repo', validated_base_sha='base')
        self.handler._fetch_latest_base = mock.Mock(return_value='advanced')
        expected = ExecutionOutcome(ExecutionOutcomeKind.RETRY)
        self.handler._latest_before_publication = mock.Mock(return_value=expected)
        result = self.handler._publish_new_pull_request(self.case.context('PUBLISHING'), self.case.state.row())
        self.assertEqual(result, expected)
        self.effects.publication.command.assert_not_called()
        self.handler._latest_before_publication.assert_called_once()

    def push_fixture(self, *, branch_changed=False, remote_race=False, existing_pr=False,
                     candidate_changed=False):
        branch = 'loop-manager/G-047'
        head = 'a' * 40
        remote = None
        def git(path, *args, **kwargs):
            nonlocal remote
            if args[0] == 'rev-parse':
                value = ('b' * 40 if candidate_changed or branch_changed and '--verify' in args
                         else head)
                return SimpleNamespace(stdout=value, returncode=0)
            if args[0] == 'ls-remote':
                return SimpleNamespace(stdout=f'{remote}\trefs/heads/{branch}\n' if remote else '', returncode=0)
            if args[0] == 'push':
                if remote_race:
                    remote = 'c' * 40
                    return SimpleNamespace(stdout='lease rejected', returncode=1)
                remote = head
            return SimpleNamespace(stdout='', returncode=0)
        stack = ExitStack()
        self.addCleanup(stack.close)
        owned = stack.enter_context(mock.patch.object(lm, '_owned_publication_plan', return_value={
            'plan_id': 'G-047', 'publication_remote_oid': None, 'pr_url': None}))
        command = stack.enter_context(mock.patch.object(lm, 'git', side_effect=git))
        stack.enter_context(mock.patch.object(lm, 'remote_branch_oid', side_effect=lambda *args: remote))
        stack.enter_context(mock.patch.object(lm, '_recorded_base_fetch'))
        stack.enter_context(mock.patch.object(lm, 'pr_for_branch', return_value={'headRefOid': head} if existing_pr else None))
        return branch, head, owned, command

    def push(self, branch, expected_oid='a' * 40):
        return lm.push_managed_publication_branch(self.case.connection, Path('/repo'),
            Path('/state'), 'repo', branch, Path('/state/G-047'), expected_oid)

    def test_candidate_changed_between_observation_and_push_helper_cannot_be_pushed(self):
        branch, head, _, command = self.push_fixture(candidate_changed=True)
        self.case.remote = head
        self.case.review_context()
        snapshot = self.handler._definition_snapshot
        self.case.state.update_project('repo',
            verify_token=snapshot.verify_token('base', head),
            review_token=snapshot.review_token('base', head))
        self.effects.publication.push_managed_publication_branch.side_effect = (
            lambda *args: lm.push_managed_publication_branch(self.case.connection, *args))
        result = self.handler._publish_new_pull_request(
            self.case.context('PUBLISHING'), self.case.state.row())
        self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.publication.push_managed_publication_branch.assert_called_once_with(
            Path('/repo'), Path('/state'), 'repo', branch, Path('/state/G-047'), head)
        self.assertFalse(any(call.args[1] == 'push' for call in command.call_args_list))
        self.effects.publication.command.assert_not_called()

    def test_push_uses_confirmed_object_oid_and_create_only_lease(self):
        branch, head, owned, command = self.push_fixture()
        self.assertEqual(self.push(branch), head)
        pushed = next(call for call in command.call_args_list if call.args[1] == 'push')
        self.assertIn(f'{head}:refs/heads/{branch}', pushed.args)
        self.assertIn(f'--force-with-lease=refs/heads/{branch}:', pushed.args)
        self.assertEqual(owned.call_count, 2)

    def test_push_rejects_changed_owned_branch_or_new_pr_before_mutation(self):
        for options in ({'branch_changed': True}, {'existing_pr': True}):
            with self.subTest(options=options):
                branch, _, _, command = self.push_fixture(**options)
                with self.assertRaises(lm.PublicationSafetyError):
                    self.push(branch)
                self.assertFalse(any(call.args[1] == 'push' for call in command.call_args_list))

    def test_remote_creation_race_cannot_be_overwritten_by_first_push(self):
        branch, _, _, command = self.push_fixture(remote_race=True)
        with self.assertRaisesRegex(RuntimeError, 'lease rejected'):
            self.push(branch)
        pushed = next(call for call in command.call_args_list if call.args[1] == 'push')
        self.assertIn(f'--force-with-lease=refs/heads/{branch}:', pushed.args)

    def conflict_push_fixture(self):
        branch = 'loop-manager/G-047'
        head, base, old = 'a' * 40, 'b' * 40, 'c' * 40
        plan = dict(self.case.repository.active_row('repo'))
        plan.update(status='PUBLISHING', pr_conflict_recovery=1, workspace_owned=lm.WORKSPACE_OWNED,
            branch=branch, worktree='/state/G-047', pr_url='https://example/pr/1',
            pr_conflict_head_oid=old, pr_conflict_base_oid=base, active_sha=base, validated_base_sha=base)
        pr = {'url': plan['pr_url'], 'state': 'OPEN', 'headRefName': branch, 'headRefOid': old}
        stack = ExitStack()
        self.addCleanup(stack.close)
        for patch in (
                mock.patch.object(lm, 'active_execplan', return_value=plan),
                mock.patch.object(lm, 'validate_execplan_workspace_identity'),
                mock.patch.object(lm, 'validate_managed_worktree'),
                mock.patch.object(lm, '_fetch_pr_conflict_recovery_refs'),
                mock.patch.object(lm, 'remote_branch_oid', return_value=old)):
            stack.enter_context(patch)
        command = stack.enter_context(mock.patch.object(lm, 'git',
            return_value=SimpleNamespace(stdout=head, returncode=0)))
        lookup = stack.enter_context(mock.patch.object(lm, 'pr_for_branch', return_value=pr))
        return branch, head, pr, command, lookup

    def test_conflict_push_requeries_actual_pr_head_after_fetch(self):
        branch, head, pr, command, lookup = self.conflict_push_fixture()
        lookup.return_value = {**pr, 'headRefOid': 'd' * 40}
        with self.assertRaises(lm.PublicationSafetyError):
            lm.push_owned_pr_conflict_recovery(self.case.connection, Path('/repo'),
                Path('/state'), 'repo', branch, Path('/state/G-047'), pr, head)
        self.assertFalse(any(call.args[1] == 'push' for call in command.call_args_list))

    def test_conflict_push_rejects_candidate_changed_before_helper_entry(self):
        branch, head, pr, command, lookup = self.conflict_push_fixture()
        command.return_value.stdout = 'd' * 40
        with self.assertRaises(lm.PublicationSafetyError):
            lm.push_owned_pr_conflict_recovery(self.case.connection, Path('/repo'),
                Path('/state'), 'repo', branch, Path('/state/G-047'), pr, head)
        self.assertFalse(any(call.args[1] == 'push' for call in command.call_args_list))
        lookup.assert_not_called()
