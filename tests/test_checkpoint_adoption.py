import json
import sqlite3
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import runtime_state
import loop_manager
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery import work_branch_integration
from loop_manager_internal.git import managed_rebase_validation as rebase_policy


class CheckpointRefreshFixture:
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")
        self.connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, content_hash, status) "
            "VALUES ('repo', 'G-106', 'docs/plans/[G-106] Work.md', 'Work', '[]', '[]', 'critical', 'hash', 'PLAN_BLOCKED')")
        self.store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        self.request = self.store.request(repo='repo', plan_id='G-106', reason_code='verify_retry_exhausted',
                                          evidence='original failure', branch='loop-manager/G-106', worktree=str(Path('/state/G-106').resolve()),
                                          expected_remote_oid='a' * 40, rebase_recovery=False)
        self.store.record_prepared(self.request, blocked_checkpoint.PreparedCheckpoint(
            'a' * 40, 'c' * 40, 'identity', 'message', ()))
        self.store.finish_published(self.request, 'a' * 40)


    def row(self):
        return self.connection.execute('SELECT * FROM blocked_checkpoints').fetchone()


    def _adoption(self, *, fetched_oid=None, observed_oid=None, diverged=False,
                  dirty=False, changed_paths=('code.py',), rewritten=False, candidate_race=False,
                  managed_rebased=False, local_rewritten=False, manager_waiting=False, base_conflict=False,
                  pending_restart=None, pending_input_rewritten=False, foreign_pending=False,
                  completed_base_restart=False, base_completed=None,
                  existing_pr=False, projected=False, pr_conflict=False):
        branch, worktree = 'loop-manager/G-106', str(Path('/state/G-106').resolve())
        original, successor = 'a' * 40, 'b' * 40
        self.connection.execute("UPDATE projects SET origin = 'example/repo'")
        self.connection.execute(
            "UPDATE exec_plans SET branch = ?, worktree = ?, workspace_owned = 1, blocked_reason = 'verify_retry_exhausted'",
            (branch, worktree))
        self.connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES ('repo', 'G-106', 'plan', 'verify_retry_exhausted', 'original failure')")
        if existing_pr:
            self.connection.execute(
                'UPDATE exec_plans SET pr_url = ?, pr_head_oid = ?, publication_remote_oid = ?, '
                'pr_conflict_recovery = ?, pr_conflict_head_oid = ?',
                ('saved-pr', original, original, int(pr_conflict), original if pr_conflict else None))
            self.request = self.store.request(
                repo='repo', plan_id='G-106', reason_code='verify_retry_exhausted',
                evidence='original failure', branch=branch, worktree=worktree,
                expected_remote_oid=original, rebase_recovery=False)
            self.store.record_prepared(self.request, blocked_checkpoint.PreparedCheckpoint(
                original, 'c' * 40, 'identity', 'message', ()))
            self.store.finish_published(self.request, original, reobserve=lambda: original)
        if projected:
            runtime_state.RuntimeStateRepository(self.connection).hydrate('repo', 'G-106')
        self.connection.commit()
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        local = 'c' * 40 if diverged or managed_rebased or local_rewritten or pending_restart or base_completed else original
        if base_completed == 'aborted':
            local = original
        snapshot = loop_manager.GitSnapshot(local, branch, {}, {},
                                            {f'refs/heads/{branch}': (local, '')},
                                            {str(Path(worktree).resolve()): (f'branch=refs/heads/{branch}',)}, {}, {})
        if managed_rebased or base_completed in {'after_save', 'work_pending', 'unproven'}:
            acceptance = dict(kind=rebase_policy.ACCEPTANCE_KIND, policy_version=rebase_policy.POLICY_VERSION,
                repo='repo', repository_identity='identity', plan_id='G-106', plan_path=plan['path'],
                worktree=worktree, branch=branch, target_base_oid='e' * 40,
                remote=dict(repository='repo', branch=branch,
                            oid='f' * 40 if base_completed else original, pull_request=None),
                head=local, generation=0, input_fingerprint='input', scope_fingerprint='scope',
                state_fingerprint='state', role='worker')
            acceptance['id'] = rebase_policy.fingerprint(acceptance)
            saved = {'current_rebase_acceptance': acceptance}
            if base_completed:
                saved.update(base_rebase_restart={
                    'phase': 'complete', 'remote': 'f' * 40, 'remote_before': original, 'head': local,
                    'original_head': '7' * 40 if local_rewritten else original,
                    'acceptance_id': 'invalid' if base_completed == 'unproven' else acceptance['id']},
                    remote_adoption_intent={
                        'head': 'f' * 40, 'branch': branch, 'worktree': worktree,
                        'request_identity': self.request.request_identity, 'snapshot': snapshot.serialize(),
                        'agent_snapshot': None, 'agent_result_snapshot': None})
                if base_completed == 'work_pending':
                    saved['work_branch_integration'] = {
                        'phase': 'rebasing', 'remote': 'f' * 40, 'before': snapshot.serialize(),
                        'branch': branch, 'worktree': worktree}
            execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).record(
                'repo', 'G-106', expected_generation=0,
                operation='rebase' if base_completed == 'work_pending' else None, expected_head=local,
                target_base_oid='f' * 40 if base_completed == 'work_pending' else 'e' * 40,
                observed_remote_oid='f' * 40 if base_completed else original, snapshot=saved)
        if dirty:
            snapshot = replace(snapshot, dirty={'local.txt': (('untracked',), 'bytes')})
        restored = snapshot
        if base_completed in {'manual', 'with_work'}:
            execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).record(
                'repo', 'G-106', expected_generation=0, operation='rebase', expected_head=original,
                target_base_oid='e' * 40, observed_remote_oid=original,
                snapshot={'work_branch_integration': {
                    'phase': 'complete', 'remote': original, 'before': replace(snapshot, head=original).serialize(),
                    'branch': branch, 'worktree': worktree}}
                    if base_completed == 'with_work' else {})
        elif base_completed == 'work_pending':
            snapshot = replace(snapshot, head='detached', branch=None,
                               operations={'rebase-merge': 'owned'})
        elif base_completed == 'aborted':
            execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).record(
                'repo', 'G-106', expected_generation=0, operation=None, expected_head=original,
                target_base_oid='f' * 40, observed_remote_oid=original,
                snapshot={'base_rebase_restart': {
                    'phase': 'complete', 'remote': 'f' * 40, 'original_head': original,
                    'baseline': 'preserved-conflict'},
                    'remote_adoption_intent': {
                        'head': 'f' * 40, 'branch': branch, 'worktree': worktree,
                        'request_identity': self.request.request_identity, 'snapshot': snapshot.serialize(),
                        'agent_snapshot': None, 'agent_result_snapshot': None}})
        if pending_restart:
            pending_head, integration_remote = 'f' * 40, 'e' * 40
            if pending_restart == 'before_abort':
                snapshot = replace(snapshot, head='detached', branch=None,
                    operations={'rebase-merge': 'owned'},
                    index={'code.py': ('M 100644 ours 2', 'M 100644 theirs 3')},
                    dirty={'code.py': (('staged', 'unstaged'), 'manual conflict bytes')})
            execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).record(
                'repo', 'G-106', expected_generation=0,
                operation=None if pending_restart == 'completed' else 'rebase', expected_head=local,
                target_base_oid=integration_remote, observed_remote_oid=pending_head,
                snapshot={'work_branch_integration': {
                    'phase': 'complete' if pending_restart == 'completed' else 'rebasing',
                    'remote': integration_remote, 'before': restored.serialize(),
                    'branch': branch, 'worktree': worktree,
                    'restart': {'remote': pending_head, 'baseline': 'pinned-conflict'}},
                    'remote_adoption_intent': {
                        'head': pending_head, 'branch': 'foreign' if foreign_pending else branch,
                        'worktree': worktree, 'request_identity': self.request.request_identity,
                        'snapshot': restored.serialize(), 'agent_snapshot': None, 'agent_result_snapshot': None},
                    **({'base_rebase_restart': {'phase': 'complete', 'remote': '1' * 40}}
                       if completed_base_restart else {})})
            self.store.start_remote_adoption(self.request, pending_head)
        if base_conflict:
            execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).record(
                'repo', 'G-106', expected_generation=0, operation='rebase', expected_head=original,
                target_base_oid='e' * 40, observed_remote_oid=original, snapshot={})
            snapshot = replace(snapshot, head='detached', branch=None, operations={'rebase-merge': 'owned'},
                               dirty={'code.py': (('unstaged',), 'manual conflict bytes')})
        combined = 'd' * 40 if diverged or dirty or managed_rebased or pending_restart or base_completed else successor
        adopted = replace(restored, head=combined, dirty={},
                          refs={f'refs/heads/{branch}': (combined, '')})
        confirmed = replace(adopted, dirty={'new.txt': (('untracked',), 'raced')}) if candidate_race else adopted
        def git(_, *args, **kwargs):
            output = ''
            code = 0
            if args[:2] == ('remote', 'get-url'):
                output = 'https://github.com/example/repo'
            elif args == ('rev-parse', 'FETCH_HEAD^{commit}'):
                output = fetched_oid or successor
            elif args == ('rev-parse', 'HEAD^{commit}'):
                output = combined
            elif args == ('merge-base', '--is-ancestor', original, successor):
                code = int(rewritten)
            elif args == ('merge-base', '--is-ancestor', original, local):
                code = int(local != original and (managed_rebased or local_rewritten or bool(base_completed)))
            elif args == ('merge-base', '--is-ancestor', original, '7' * 40):
                code = int(local_rewritten)
            elif args == ('merge-base', '--is-ancestor', local, successor):
                code = int(diverged or managed_rebased)
            elif pending_restart and args == ('merge-base', '--is-ancestor', pending_head, successor):
                code = int(pending_input_rewritten)
            elif base_completed and args == ('merge-base', '--is-ancestor', 'f' * 40, successor):
                code = int(pending_input_rewritten)
            elif completed_base_restart and args == ('merge-base', '--is-ancestor', '1' * 40, successor):
                code = 1  # A completed historical input may predate a published Manager rebase.
            elif args[:1] == ('ls-tree',):
                output = '100644 blob ' + successor + '\tcode.py'
            return subprocess.CompletedProcess([], code, output)
        real_restart = work_branch_integration.restart_base_rebase
        with (mock.patch.object(loop_manager, '_BlockedCheckpointAdapter') as adapter,
              mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
              mock.patch.object(loop_manager, 'validate_managed_worktree'),
              mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=(
                  [snapshot, restored, adopted, confirmed] if base_conflict or base_completed in {'manual', 'with_work'}
                  else [snapshot, adopted, confirmed])),
              mock.patch.object(loop_manager, '_nul_git_paths', side_effect=lambda _, *args:
                                changed_paths if args[0] == 'diff' else ()),
              mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False),
              mock.patch.object(loop_manager, '_blocked_execplan_revision_metadata_error', return_value=None),
              mock.patch.object(loop_manager, 'git', side_effect=git) as commands,
              mock.patch.object(loop_manager, 'validate_blocked_agent_scope'),
              mock.patch.object(loop_manager, '_current_rebase_service') as service,
              mock.patch.object(work_branch_integration, 'integrate', return_value=adopted) as integrate,
              mock.patch.object(work_branch_integration, 'restart_base_rebase') as restart,
              mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause') as evaluate):
            adapter.return_value.observe_remote.return_value = observed_oid or successor
            adapter.return_value._approved_guidance_targets.return_value = ()
            service.return_value.require_owned.return_value = (None, mock.Mock(status=mock.Mock(
                value='waiting_for_resolution' if manager_waiting else 'complete')))
            if base_completed:
                def completed_service(*args, **kwargs):
                    current = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
                    acceptance = dict(kind=rebase_policy.ACCEPTANCE_KIND, policy_version=rebase_policy.POLICY_VERSION,
                        repo='repo', repository_identity='identity', plan_id='G-106', plan_path=plan['path'],
                        worktree=worktree, branch=branch, target_base_oid=kwargs.get('target') or current.target_base_oid,
                        remote=dict(repository='repo', branch=branch, oid=kwargs['remote_oid'], pull_request=None),
                        head=local, generation=current.generation, input_fingerprint='input',
                        scope_fingerprint='scope', state_fingerprint='state', role='worker')
                    acceptance['id'] = rebase_policy.fingerprint(acceptance)
                    decision = rebase_policy.RebaseDecision(
                        rebase_policy.RebaseStatus.WAITING if manager_waiting or base_completed == 'work_pending'
                        else rebase_policy.RebaseStatus.COMPLETE, acceptance=acceptance)
                    service.return_value.require_owned.return_value = (SimpleNamespace(head=local), decision)
                    service.return_value.observe.return_value = (None, decision)
                    return service.return_value
                service.side_effect = completed_service
            def finish_restart(*args):
                checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
                current = checkpoints.get('repo', 'G-106')
                return checkpoints.record('repo', 'G-106', expected_generation=current.generation,
                    operation=None, expected_head=original, target_base_oid=successor,
                    observed_remote_oid=original, snapshot=current.snapshot)
            restart.side_effect = finish_restart
            if base_completed in {'manual', 'with_work'}:
                restart.side_effect = real_restart
            if pending_restart or base_completed:
                def finish_integration(*args):
                    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
                    current = checkpoints.get('repo', 'G-106')
                    checkpoints.record('repo', 'G-106', expected_generation=current.generation,
                        operation=None, expected_head=combined, target_base_oid=successor,
                        observed_remote_oid=successor, snapshot=current.snapshot)
                    return adopted
                integrate.side_effect = finish_integration
            result = loop_manager._reconcile_published_blocked_checkpoint(
                self.connection, Path('/repo'), Path('/state'), 'repo', plan, self.row(),
                self.store.get('repo', 'G-106'), self.store, adopt_only=True, expected_remote_oid=successor)
        evaluate.assert_not_called()
        if base_conflict:
            restart.assert_called_once()
            self.assertEqual(restart.call_args.args[-1], snapshot)
        if base_completed in {'manual', 'with_work'}:
            restart.assert_called_once()
        if ((diverged or dirty or managed_rebased or pending_restart or base_completed)
                and not rewritten and not manager_waiting and not pending_input_rewritten and not foreign_pending
                and not local_rewritten and base_completed != 'unproven'):
            integrate.assert_called_once()
        if pending_restart and foreign_pending:
            integrate.assert_not_called()
        self.assertEqual(scoped_blocking.plan_block(self.connection, 'repo', 'G-106')['evidence'], 'original failure')
        return result, commands


class BlockedCheckpointRefreshUnitTests(CheckpointRefreshFixture, unittest.TestCase):
    def test_pr_successor_adoption_completes_base_rebase_and_resumes_unverified(self):
        from tests.support.rebase_observations import expectation, observation, decision_for
        from loop_manager_internal.git.managed_rebase_validation import PullRequest, Remote
        correction = {'version': 1, 'level': 1, 'baseline': ['old-issue'],
                      'failures': 2, 'issues': {'old-issue': 2}, 'pending': {
                          'source': 1, 'level': 1, 'completed': True,
                          'verified_head': 'rebased-head',
                      }}
        self.connection.execute(
            "UPDATE exec_plans SET worker_session_id = 'saved-worker', "
            "initial_worker_level = 1, effective_worker_level = 2, "
            "review_level_history = ?, review_correction_state = ?",
            (json.dumps({'1': {'old-issue': 3}}), json.dumps(correction)),
        )
        result, _ = self._adoption(existing_pr=True, projected=True, pr_conflict=True)
        self.assertEqual(result, 'adopted')
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        adopted = runtime.get('repo', 'G-106')
        successor = 'b' * 40
        self.assertEqual((adopted.publication_remote_oid, adopted.pr_head_oid,
                          adopted.pr_conflict_head_oid), (successor,) * 3)
        self.assertEqual(runtime.projected('repo'), adopted)
        self.assertEqual(adopted.status, 'PLAN_BLOCKED')
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        current = checkpoints.get('repo', 'G-106')
        current = checkpoints.record(
            'repo', 'G-106', expected_generation=current.generation, operation='rebase',
            expected_head=successor, target_base_oid='latest-base', observed_remote_oid=successor,
            snapshot=current.snapshot)
        expected = expectation(
            repo='repo', plan_id='G-106', plan_path=adopted.path, worktree=adopted.worktree,
            branch=adopted.branch, generation=current.generation, target_base_oid='latest-base',
            remote=Remote('repo', adopted.branch, successor,
                          PullRequest('saved-pr', 'repo', adopted.branch, successor, 'OPEN')))
        decision = decision_for(expected, observation(expected, head='rebased-head',
            branch_ref=('rebased-head', ''), registration=('HEAD=rebased-head', f'branch=refs/heads/{adopted.branch}')))
        completed = runtime.complete_rebase(
            adopted, checkpoints, acceptance=decision.acceptance,
            project_values={'phase': 'PLAN_BLOCKED'}, reobserve=lambda: decision)
        self.assertEqual(completed.pr_head_oid, successor)
        self.assertEqual(completed.active_sha, 'latest-base')
        self.assertEqual(completed.status, 'PLAN_BLOCKED')
        self.assertEqual(scoped_blocking.plan_block(self.connection, 'repo', 'G-106')['evidence'], 'original failure')
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        loop_manager._resume_remote_corrected_execplan(
            self.connection, 'repo', plan, self.store, self.store.get('repo', 'G-106'),
            'rebased-head', 'current-input', 'cause resolved')
        resumed = runtime.projected('repo')
        self.assertEqual(resumed.status, 'IMPLEMENTING')
        self.assertEqual(resumed.pr_head_oid, successor)
        self.assertEqual(resumed.worker_session_id, 'saved-worker')
        self.assertIsNone(resumed.verify_token)
        self.assertIsNone(resumed.review_token)
        self.assertIsNone(resumed.worker_result)
        self.assertIsNone(scoped_blocking.plan_block(self.connection, 'repo', 'G-106'))
        from loop_manager_internal.storage import review_correction_state
        reset = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id = 'G-106'").fetchone()
        self.assertEqual(reset['effective_worker_level'], 1)
        self.assertEqual(json.loads(reset['review_level_history']), {})
        self.assertEqual(review_correction_state.load(self.connection, 'repo', 'G-106'), {})
        review_correction_state.begin_worker(self.connection, 'repo', 'G-106', 1, None)
        review_correction_state.complete_worker(self.connection, 'repo', 'G-106', 1)
        review_correction_state.verified(self.connection, 'repo', 'G-106', 'rebased-head')
        loop_manager.transition_validation_failure(
            self.connection, 'repo', Path('/state/G-106').resolve(), 'review',
            'ISSUE_ID: app.py::persistent::blocking\nFix it\nVERDICT: FAIL',
            review_level=1, candidate_head='rebased-head',
        )
        baseline = review_correction_state.load(self.connection, 'repo', 'G-106')
        self.assertEqual(baseline['failures'], 0)
        self.assertEqual(self.connection.execute(
            "SELECT effective_worker_level FROM exec_plans WHERE plan_id = 'G-106'"
        ).fetchone()[0], 1)

    def test_unprojected_pr_adoption_preserves_independent_project_heads(self):
        self.connection.execute("UPDATE projects SET pr_head_oid = 'other-pr-head', "
                                "publication_remote_oid = 'other-lease', verify_token = 'other-verify'")
        result, _ = self._adoption(existing_pr=True)
        self.assertEqual(result, 'adopted')
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertEqual(plan['pr_head_oid'], 'b' * 40)
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        self.assertEqual(project['pr_head_oid'], 'other-pr-head')
        self.assertEqual(project['publication_remote_oid'], 'other-lease')
        self.assertEqual(project['verify_token'], 'other-verify')

    def test_pr_head_and_receipt_adoption_roll_back_together(self):
        transition = runtime_state.RuntimeStateRepository.transition
        def interrupted(runtime, *args, **kwargs):
            result = transition(runtime, *args, **kwargs)
            if kwargs['plan_values'].get('pr_head_oid') == 'b' * 40:
                raise RuntimeError('receipt interrupted')
            return result
        with mock.patch.object(runtime_state.RuntimeStateRepository, 'transition', autospec=True,
                               side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'receipt interrupted'):
                self._adoption(existing_pr=True, projected=True, pr_conflict=True)
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        for row in (plan, project):
            self.assertEqual(row['publication_remote_oid'], 'a' * 40)
            self.assertEqual(row['pr_head_oid'], 'a' * 40)
            self.assertEqual(row['pr_conflict_head_oid'], 'a' * 40)
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)

    def test_manual_base_completion_then_successor_preserves_rewritten_result(self):
        result, commands = self._adoption(base_completed='manual')
        self.assertEqual(result, 'adopted')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(saved.expected_head, 'd' * 40)
        self.assertEqual(saved.observed_remote_oid, 'b' * 40)
        self.assertEqual(saved.snapshot['base_rebase_restart']['head'], 'c' * 40)
        self.assertEqual(saved.snapshot['base_rebase_restart']['original_head'], 'a' * 40)
        self.assertEqual(saved.snapshot['current_rebase_acceptance']['target_base_oid'], 'e' * 40)
        self.assertIsNone(saved.snapshot['remote_adoption_intent'])
        self.assertFalse(any(call.args[-2:] == ('rebase', '--abort') for call in commands.call_args_list))

    def test_manual_base_completion_after_work_integration_uses_completed_head(self):
        result, _ = self._adoption(base_completed='with_work')
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)

    def test_successor_after_base_completion_save_interruption_uses_original_lineage(self):
        result, commands = self._adoption(base_completed='after_save')
        self.assertEqual(result, 'adopted')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(saved.snapshot['base_rebase_restart']['original_head'], 'a' * 40)
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)

    def test_pending_work_rebase_after_base_completion_keeps_original_lineage(self):
        result, _ = self._adoption(base_completed='work_pending')
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)

    def test_successor_after_base_abort_preserves_pending_identity_and_conflict(self):
        result, _ = self._adoption(base_completed='aborted')
        self.assertEqual(result, 'adopted')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(saved.snapshot['base_rebase_restart']['baseline'], 'preserved-conflict')
        self.assertIsNone(saved.snapshot['remote_adoption_intent'])
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)




    def test_base_completion_saved_result_still_requires_current_complete_state(self):
        result, _ = self._adoption(base_completed='after_save', manager_waiting=True)
        self.assertEqual(result, 'blocked')
        self.assertTrue(self.row()['inspection_retryable'])
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)

    def test_successor_after_restart_save_retargets_pending_adoption(self):
        result, _ = self._adoption(pending_restart='before_abort')
        self.assertEqual(result, 'adopted')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertIsNone(saved.snapshot['remote_adoption_intent'])
        self.assertEqual(saved.observed_remote_oid, 'b' * 40)
        self.assertEqual(saved.snapshot['work_branch_integration']['restart']['baseline'], 'pinned-conflict')

    def test_successor_after_abort_retargets_pending_adoption(self):
        result, _ = self._adoption(pending_restart='after_abort')
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)

    def test_successor_after_completion_save_retargets_pending_adoption(self):
        result, _ = self._adoption(pending_restart='completed')
        self.assertEqual(result, 'adopted')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertIsNone(saved.snapshot['remote_adoption_intent'])
        self.assertEqual(saved.snapshot['remote_adoption_scope']['head'], 'd' * 40)
        self.assertEqual(saved.observed_remote_oid, 'b' * 40)
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)

    def test_related_rewrite_can_retarget_pending_restart_input(self):
        result, commands = self._adoption(pending_restart='before_abort', pending_input_rewritten=True)
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertIsNone(saved.snapshot['remote_adoption_intent'])
        self.assertEqual(saved.snapshot['work_branch_integration']['restart']['baseline'], 'pinned-conflict')

    def test_successor_does_not_retarget_foreign_pending_identity(self):
        result, _ = self._adoption(pending_restart='after_abort', foreign_pending=True)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)

    def test_completed_historical_base_restart_does_not_block_work_restart_successor(self):
        result, commands = self._adoption(pending_restart='after_abort', completed_base_restart=True)
        self.assertEqual(result, 'adopted')
        self.assertFalse(any(call.args[1:] == ('merge-base', '--is-ancestor', '1' * 40, 'b' * 40)
                             for call in commands.call_args_list))

    def test_new_successor_restarts_owned_base_conflict_before_normal_adoption(self):
        result, commands = self._adoption(base_conflict=True)
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)
        self.assertTrue(any('merge' in call.args for call in commands.call_args_list))

    def test_refresh_keeps_published_ownership_and_same_request(self):
        request = self.store.get('repo', 'G-106')
        self.store.refresh(request, expected_remote_oid='a' * 40)
        row = self.row()
        self.assertEqual(row['phase'], 'requested')
        self.assertEqual(row['published_oid'], 'a' * 40)
        self.assertEqual(row['commit_identity'], 'identity')
        self.assertEqual(row['request_identity'], request.request_identity)
        self.assertEqual(row['evidence'], 'original failure')


    def test_refresh_does_not_overwrite_pending_push_lease(self):
        request = self.store.get('repo', 'G-106')
        self.store.refresh(request, expected_remote_oid='a' * 40)
        self.store.record_local(request, 'b' * 40)
        self.store.finish_unpublished(request, 'network error', retryable=True)
        self.store.refresh(self.store.get('repo', 'G-106'), expected_remote_oid='different')
        row = self.row()
        self.assertEqual(row['phase'], 'retry_pending')
        self.assertEqual(row['expected_remote_oid'], 'a' * 40)
        self.assertEqual(row['local_oid'], 'b' * 40)


    def test_rebased_result_preserves_prior_identity_and_accepts_next_successor(self):
        request = self.store.get('repo', 'G-106')
        with self.connection:
            self.store.record_rebased_publication(request, 'b' * 40, 'd' * 40)
        first = dict(self.row())
        with self.connection:
            self.store.record_rebased_publication(request, 'b' * 40, 'd' * 40)
        self.assertEqual(dict(self.row()), first)
        history = json.loads(self.row()['publication_history'])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['published_oid'], 'a' * 40)
        self.assertEqual(history[0]['commit_identity'], 'identity')
        self.assertEqual(self.row()['reason_code'], 'verify_retry_exhausted')
        def git(_, *args, **kwargs):
            output = ''
            if args[:1] == ('rev-parse',):
                output = 'd' * 40
            elif args[:1] == ('show',):
                output = 'Rebased result with no checkpoint trailer'
            elif args[:1] == ('ls-remote',):
                output = 'b' * 40 + '\trefs/heads/loop-manager/G-106\n'
            return mock.Mock(returncode=0, stdout=output)
        self.assertEqual(checkpoint_ownership.owned_remote_oid(
            self.connection, Path('/repo'), 'repo', 'G-106', 'loop-manager/G-106',
            Path('/state/G-106').resolve(), 'b' * 40, git, before_head='b' * 40, recorded_oid='b' * 40), 'b' * 40)


    def test_refresh_preparation_retains_old_publication_audit(self):
        request = self.store.get('repo', 'G-106')
        self.store.refresh(request, expected_remote_oid='a' * 40)
        prepared = blocked_checkpoint.PreparedCheckpoint('a' * 40, 'new-tree', 'new-identity', 'new-message', ())
        self.store.record_prepared(request, prepared)
        history = json.loads(self.row()['publication_history'])
        self.assertEqual(history[0]['superseded_request']['commit_identity'], 'identity')
        self.assertEqual(history[0]['superseded_request']['accepted_remote_oid'], 'a' * 40)
        self.assertEqual(history[-1]['commit_identity'], 'identity')
        self.assertEqual(self.row()['commit_identity'], 'new-identity')


    def test_adoption_defers_cause_check_and_records_exact_successor_lease(self):
        self.connection.execute("UPDATE exec_plans SET verify_token = 'old', review_token = 'old', "
                                "publication_token = 'old', worker_result = 'preserved audit'")
        result, _ = self._adoption()
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)
        self.assertEqual(self.connection.execute('SELECT publication_remote_oid FROM exec_plans').fetchone()[0], 'b' * 40)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(checkpoint.observed_remote_oid, 'b' * 40)
        self.assertIsNone(checkpoint.operation)
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertIsNone(plan['verify_token'])
        self.assertIsNone(plan['review_token'])
        self.assertIsNone(plan['publication_token'])
        self.assertEqual(plan['worker_result'], 'preserved audit')


    def test_adoption_fetch_race_does_not_fast_forward_or_change_accepted_oid(self):
        result, commands = self._adoption(fetched_oid='c' * 40)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)
        self.assertFalse(any('merge' in call.args for call in commands.call_args_list))


    def test_adoption_observation_race_is_refused_before_fetch(self):
        result, commands = self._adoption(observed_oid='c' * 40)
        self.assertEqual(result, 'blocked')
        commands.assert_not_called()

    def test_diverged_branch_preserves_local_candidate_and_pins_remote_lease(self):
        result, commands = self._adoption(diverged=True)
        self.assertEqual(result, 'adopted')
        saved = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(saved.expected_head, 'd' * 40)
        self.assertEqual(saved.observed_remote_oid, 'b' * 40)
        self.assertEqual(saved.snapshot['remote_adoption_scope']['remote'], 'b' * 40)
        self.assertTrue(loop_manager.blocked_plan_reconciliation.remote_adoption_is_current(saved))
        self.assertFalse(any('merge' in call.args for call in commands.call_args_list))

    def test_dirty_branch_uses_preserving_integration(self):
        result, _ = self._adoption(dirty=True)
        self.assertEqual(result, 'adopted')

    def test_user_guidance_successor_is_input_instead_of_worker_scope_violation(self):
        result, _ = self._adoption(changed_paths=('AGENTS.md', 'docs/ARCHITECTURE.md'))
        self.assertEqual(result, 'adopted')

    def test_related_remote_rewrite_is_adopted_as_current_input(self):
        result, _ = self._adoption(rewritten=True)
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)

    def test_candidate_race_before_sqlite_confirmation_retains_pending_input(self):
        result, _ = self._adoption(candidate_race=True)
        self.assertEqual(result, 'blocked')
        self.assertEqual(self.row()['accepted_remote_oid'], 'a' * 40)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-106')
        self.assertEqual(checkpoint.snapshot['remote_adoption_intent']['head'], 'b' * 40)
        self.assertTrue(self.row()['inspection_retryable'])


    def test_local_rewrite_is_preserved_as_current_input(self):
        result, _ = self._adoption(local_rewritten=True)
        self.assertEqual(result, 'adopted')
        self.assertEqual(self.row()['accepted_remote_oid'], 'b' * 40)
