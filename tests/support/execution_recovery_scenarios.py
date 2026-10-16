"""Shared scenario setup retained from the phase policy tests."""
from contextlib import closing

from tests.support.current_rebase import current_rebase_case
from tests.support.pr_recovery import observe_live_pull_request
from tests.support.execution import recovered_rebase_workspace, recovered_rebase_publication
from dataclasses import replace
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.git import rebase_block_reconciliation
import loop_manager as lm
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.execution.execution_runtime import ExecutionPhase, ExecutionPhaseCoordinator, ExecutionOutcome, ExecutionOutcomeKind
from tests.support.blocked_git import blocked_git_fixture
from tests.support.recovery_boundary import RecoveryBoundary
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.execution.worker_session import SessionBinding
from tests.support.execution import READY_WORKER_RESULT



class ExecutionRecoveryScenarios:
    def _legacy_rebase_false_block_recovery(self, *, plan_block, rejected=None,
                                          existing_pr=False, published_checkpoint=False,
                                          stale_cache=False, manual=None, scope_stage_block=False,
                                          continue_to_publication=False):
        self.journal_start.stop()
        boundary = self.enterContext(RecoveryBoundary()) if isinstance(rejected, tuple) else None
        fixture = blocked_git_fixture(self, directory=boundary.directory if boundary else None)
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
        with mock.patch.object(lm, 'pr_for_branch', side_effect=lambda *_a, **_k:
                                    observe_live_pull_request(fixture.repo, pull_request)):
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
        if scope_stage_block:
            before_scope = lm.capture_git_snapshot(fixture.worktree)
            self.assertTrue(any(entry.endswith((' 1', ' 2', ' 3'))
                                for entry in before_scope.index['code.py']))
            connection.execute(
                "UPDATE exec_plans SET agent_role = 'worker', agent_snapshot = ?, agent_baseline = ?",
                (before_scope.serialize(), before_scope.head))
        if manual is None:
            with mock.patch.object(lm, 'pr_for_branch', side_effect=lambda *_a, **_k:
                                    observe_live_pull_request(fixture.repo, pull_request)):
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
        if scope_stage_block:
            reason = 'code.py: legacy index flags M -> H false positive'
            resolved_scope = lm.capture_git_snapshot(fixture.worktree)
            self.assertTrue(all(entry.startswith('H ') and entry.endswith(' 0')
                                for entry in resolved_scope.index['code.py']))
            with mock.patch.object(lm, 'pr_for_branch', side_effect=lambda *_a, **_k:
                                    observe_live_pull_request(fixture.repo, pull_request)):
                lm.validate_worker_workspace_scope(
                    fixture.repo, fixture.worktree, branch, 'main', context.plan.path,
                    connection.execute('SELECT * FROM exec_plans').fetchone(), before_scope,
                    allow_rebase=True, connection=connection)
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
            fixture.block('managed_worktree_scope_violation' if scope_stage_block
                          else 'managed_worktree_conflict', reason)
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
        if manual and not scope_stage_block:
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
        with closing(sqlite3.connect(database)) as persisted, persisted:
            connection.backup(persisted)
        connection.close()
        fixture.connection = sqlite3.connect(database)
        fixture.connection.row_factory = sqlite3.Row
        connection = fixture.connection
        if boundary:
            boundary.save(connection)
        def resume():
            if plan_block:
                return lm.resume_plan_blocks(
                    connection, fixture.repo, fixture.state_dir, repo, 'main',
                )
            return lm.resume_project_block_if_safe(
                connection, fixture.repo, fixture.state_dir, repo,
            )

        with mock.patch.object(lm, 'pr_for_branch', side_effect=lambda *_a, **_k:
                                    observe_live_pull_request(fixture.repo, pull_request)):
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
                rejections = rejected if boundary else (rejected,)
                for index, rejected in enumerate(rejections):
                    with self.subTest(rejected=rejected):
                        if index:
                            fixture.connection = connection = boundary.restore(connection)
                        completed = git(fixture.worktree, 'rev-parse', 'HEAD')
                        rejected_file = (fixture.worktree / 'docs/requirements/Protected.md'
                                         if rejected in {'unstaged', 'late-file'} else app)
                        rejected_file.parent.mkdir(parents=True, exist_ok=True)
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
                            return SimpleNamespace(observe=observe, require_owned=observe,
                                                   normalize_frontmatter=lambda: False)
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
                if not published_checkpoint:
                    return
            self.assertTrue(resume())
            if manual in {'waiting', 'staged'}:
                self.assertTrue(lm._rebase_in_progress_for_branch(fixture.worktree, branch))
                if manual == 'waiting':
                    app.write_text(''.join(resolved))
                    git(fixture.worktree, 'add', 'code.py')
                # The user's changed plan is also part of this manual result;
                # Git continue requires all tracked edits to be staged.
                if not scope_stage_block:
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
        # 복구 조합은 위의 파일·journal·SQLite 효과까지, 후속 게시는 대표 경로에서 확인한다.
        if continue_to_publication:
            git(fixture.repo, 'push', 'origin', 'main')
            self._continue_recovered_rebase(fixture, target, pull_request)


    def _continue_recovered_rebase(self, fixture, target, pull_request):
        """Run the phase policies; external Agent and verify services stay fake."""
        # Local remotes exercise rebase/publication ownership; document reporting has unit coverage.
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_publication_handler.common_guidance_report',
            return_value=''))
        self.enterContext(mock.patch(
            'loop_manager_internal.execution.execution_verify_handler.common_guidance_report',
            return_value=''))
        repo = str(fixture.repo.resolve())
        pr_query = mock.patch.object(lm, 'pr_for_branch', side_effect=lambda *_a, **_k:
                                    observe_live_pull_request(fixture.repo, pull_request))
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
        effects.publication.pr_for_branch.side_effect = lambda *_a, **_k: observe_live_pull_request(
            fixture.repo, pull_request)
        def publication_command(arguments, **_kwargs):
            if arguments[:3] == ['gh', 'pr', 'view']:
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    'body': '', 'state': 'OPEN',
                    'headRefOid': observe_live_pull_request(fixture.repo, pull_request)['headRefOid'],
                }))
            return SimpleNamespace(returncode=0, stdout='https://example/pr/1')
        effects.publication.command.side_effect = publication_command
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
                pr_conflict_head_oid=state.row()['pr_head_oid'], pr_conflict_base_oid=target)
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
            effects.publication.command.assert_called_once()
            self.assertEqual(effects.publication.command.call_args.args[0][:3], ['gh', 'pr', 'view'])
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


    def _save_rebase_worker_result(self, message, *, outcome_contract_version=1):
        if self.handler._definition_snapshot is None:
            self.handler.guard(self.context())
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
            input_snapshot=self.handler._definition_snapshot.serialize(),
        )

        return context


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


    def _assert_staged_rebase_resume(self, rebase_metadata, *, changed=False,
                                    unstaged=False, untracked=False,
                                    clean_after_stage=False, replaced_resolution=False):
        self.handler.guard(self.context())
        definition = self.handler._definition_snapshot
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
                      'rebase_worker_input': definition.serialize(),
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
        self.assertEqual(result.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_result'], 'saved correction')
        self.assertEqual(self.state.row()['worker_input_token'], definition.worker_token)
        self.assertIsNone(self.state.row()['verify_token'])
        self.assertIsNone(self.state.row()['review_token'])
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-session')
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.effects.agent.run_agent.assert_not_called()
        continuations = [call for call in self.effects.workspace.git.call_args_list
                         if '--continue' in call.args]
        self.assertEqual(len(continuations), int(rebase_metadata and not clean_after_stage))
        skips = [call for call in self.effects.workspace.git.call_args_list
                 if call.args[1:] == ('rebase', '--skip')]
        self.assertEqual(len(skips), int(rebase_metadata and clean_after_stage))


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
        self.effects.agent.begin_agent_scope.side_effect = (
            lambda _repo, path, *_args, **_kwargs: lm.capture_git_snapshot(path)
        )
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
        self.effects.agent.begin_agent_scope.return_value = lm.GitSnapshot(
            'owned-head', 'loop-manager/G-047', {}, {}, {}, {}, {}, {})
        with mock.patch('loop_manager.capture_git_snapshot', return_value=lm.GitSnapshot(
                'owned-head', 'loop-manager/G-047', {}, {}, {}, {}, {}, {})), mock.patch(
            'loop_manager.validate_agent_scope'
        ), mock.patch('loop_manager.validate_managed_worktree'), mock.patch(
            'loop_manager.validate_execplan', return_value=workspace_plan
        ), mock.patch(
            'loop_manager_internal.execution.plan_validation.restore_worker_frontmatter',
            return_value=False,
        ):
            return ExecutionPhaseCoordinator(self.handler).dispatch(
                self.handler._context()
            )


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
