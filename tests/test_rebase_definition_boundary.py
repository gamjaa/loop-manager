"""Checkpoint ownership precedes latest-definition adoption, with real Git/SQLite."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, mock

import loop_manager as lm
from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.execution.worker_session import SessionBinding
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot, execplan_body_hash
from loop_manager_internal.execution.execution_runtime import ExecutionContext, ExecutionOutcomeKind, ExecutionPhase
from loop_manager_internal.git import manager_git_journal as journal
from loop_manager_internal.git.managed_rebase_runtime import build_service
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.execution import execution_fixture, recovered_rebase_workspace, READY_WORKER_RESULT
from tests.support.git import make_remote_repository, git_command, isolated_git_test


@isolated_git_test
class RebaseDefinitionBoundaryTests(TestCase):
    plan_path = 'docs/plans/[G-047] Runtime.md'
    source = 'docs/requirements/Added.md'

    def setUp(self):
        real_begin = journal.begin_rebase
        self.fixture = execution_fixture(self)
        restore_journal = mock.patch.object(journal, 'begin_rebase', real_begin)
        restore_journal.start()
        self.addCleanup(restore_journal.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = make_remote_repository(directory.name)
        self.repo_key = str(self.repo.resolve())
        self.state_dir = self.root / 'state'
        self.branch, self.worktree = lm.expected_execplan_workspace(self.state_dir, self.repo_key, 'G-047')
        self.worktree.parent.mkdir(parents=True)
        self.old_text = (
            '---\nid: G-047\ntitle: Runtime\nsources:\n  - docs/PRODUCT.md\n'
            'depends_on: []\ncomplexity: medium\nrisk: medium\npriority: normal\n'
            '---\n\n## Purpose\nImplement current inputs.\n')
        (self.repo / self.plan_path).parent.mkdir(parents=True)
        (self.repo / self.plan_path).write_text(self.old_text, encoding='utf-8')
        git_command(self.repo, 'add', '.')
        git_command(self.repo, 'commit', '-m', 'Add plan')
        git_command(self.repo, 'push', 'origin', 'main')
        self.base = git_command(self.repo, 'rev-parse', 'HEAD')
        git_command(self.repo, 'worktree', 'add', '-b', self.branch, str(self.worktree), self.base)
        (self.worktree / 'app.py').write_text('saved_worker_change = True\n')
        git_command(self.worktree, 'add', 'app.py')
        git_command(self.worktree, 'commit', '-m', 'Preserved Worker change')
        self.head = git_command(self.worktree, 'rev-parse', 'HEAD')
        self.connection = self.fixture.connection
        self.connection.execute('UPDATE projects SET repo=?', (self.repo_key,))
        self.connection.execute('UPDATE exec_plans SET repo=?', (self.repo_key,))
        self.state = ExecutionStatePort(self.connection, self.fixture.repository, self.repo_key)
        self.definition = DefinitionSnapshot(
            revision_sha=self.base, path=self.plan_path, title='Runtime',
            sources=('docs/PRODUCT.md',), depends_on=(), complexity='medium',
            risk='medium', priority='normal', body_hash=execplan_body_hash(self.old_text),
            source_fingerprint='sources-before', archive_candidates=())
        self.connection.execute(
            "UPDATE exec_plans SET current_revision_sha=?, "
            "current_complexity='medium', current_risk='medium'", (self.base,))
        self.state.update_project(self.repo_key, active_sha=self.base, plan_sha=self.base, worktree=str(self.worktree),
                                  branch=self.branch, definition_snapshot=self.definition.serialize())
        self.checkpoints = ExecutionCheckpointRepository(self.connection)
        self.checkpoints.record(self.repo_key, 'G-047', expected_generation=0, operation='rebase',
            expected_head=self.base, target_base_oid=self.base, observed_remote_oid=None,
            snapshot={'failure': 'prior failure', 'stage_evidence': {'audit': 'prior stage'}})
        pr = mock.patch.object(lm, 'pr_for_branch', return_value=None)
        pr.start()
        self.addCleanup(pr.stop)
        self.service().adopt(self.state, self.checkpoints)
        self.acceptance = self.checkpoints.get(self.repo_key, 'G-047').snapshot['current_rebase_acceptance']
        self.state.update_project(self.repo_key, worker_session_id='saved-worker',
            worker_session_binding=SessionBinding('codex', 'model', 'high').serialize(),
            worker_result=READY_WORKER_RESULT, verify_result='old verify', review_result='old Review',
            worker_input_token=self.definition.worker_token, archive_token=self.definition.archive_token,
            verify_token='old-verify-token', review_token='old-review-token',
            publication_token='old-publication-token')
        self.connection.execute('UPDATE exec_plans SET verify_failure_history=?, review_failure_history=?',
            ('["prior verify failure"]', '["prior Review failure"]'))
        self.new_text = self.old_text.replace(
            '  - docs/PRODUCT.md\n', f'  - docs/PRODUCT.md\n  - {self.source}\n').replace(
                'risk: medium', 'risk: high')
        (self.repo / self.plan_path).write_text(self.new_text, encoding='utf-8')
        (self.repo / self.source).write_text('# Added source\n', encoding='utf-8')
        git_command(self.repo, 'add', '.')
        git_command(self.repo, 'commit', '-m', 'Change sources and risk on remote base')
        git_command(self.repo, 'push', 'origin', 'main')
        git_command(self.repo, 'fetch', 'origin')
        self.latest = git_command(self.repo, 'rev-parse', 'HEAD')
        # Only catalog is reconciled; runtime definition and worktree remain old.
        self.connection.execute(
            "UPDATE exec_plans SET sources=?, current_risk='high', current_revision_sha=?, "
            "catalog_generation=catalog_generation+1",
            (json.dumps(['docs/PRODUCT.md', self.source]), self.latest))
        effects = replace(self.fixture.effects, state=self.state, checkpoint=self.checkpoints,
            workspace=recovered_rebase_workspace(self.fixture.effects.workspace,
                connection=self.connection, repo_path=self.repo, state_dir=self.state_dir,
                repo=self.repo_key, plan_id='G-047', worktree=self.worktree))
        effects = replace(effects, workspace=replace(effects.workspace,
            branch_has_changes=lm.branch_has_changes))
        effects.agent.capture_git_snapshot.side_effect = lm.capture_git_snapshot
        effects.agent.path_signature.side_effect = lm._path_signature
        effects.agent.begin_agent_scope.side_effect = lambda _repo, path, *_a, **_k: lm.capture_git_snapshot(path)
        effects.validation.validate_active_execplan_metadata.side_effect = lm.validate_active_execplan_metadata
        effects.revision.reconcile_catalog.return_value = SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha=self.latest)
        effects.revision.reconcile_catalog.side_effect = None
        effects.revision.body_hash.side_effect = lm._revision_execplan_body_hash
        effects.revision.source_fingerprint.side_effect = lm._revision_execplan_source_fingerprint
        effects.revision.changed_paths.return_value = (self.plan_path, self.source)
        self.effects = effects
        self.handler = self.new_handler()

    def new_handler(self):
        handler = handlers.ExecutionHandlers(self.effects, repo=self.repo_key, repo_path=self.repo,
            state_dir=self.state_dir, base='main', base_sha=self.latest, provider='codex',
            config_path=self.root / 'config', newly_activated=False)
        handler.catalog_plan = self.state.active_execplan(self.repo_key)
        handler.plan = handler._cached_plan(handler.catalog_plan)
        return handler

    def service(self, **options):
        return lm._current_rebase_service(self.connection, self.repo, self.state_dir,
            self.repo_key, 'G-047', self.worktree, self.branch, **options)

    def test_completed_checkpoint_reaches_revision_gate_and_resumes_worker(self):
        self._assert_refresh_and_resume(dirty=False)

    def test_unstaged_work_is_preserved_across_definition_refresh(self):
        self._assert_refresh_and_resume(dirty=True)

    def _assert_refresh_and_resume(self, *, dirty):
        contents = 'saved_worker_change = True\n' + ('local_work = True\n' if dirty else '')
        (self.worktree / 'app.py').write_text(contents)
        old_snapshot = self.state.row()['definition_snapshot']
        context = self.handler.recover(self.handler._context())
        self.assertIsInstance(context, ExecutionContext)
        self.assertEqual(self.state.row()['definition_snapshot'], old_snapshot)
        self.assertEqual(self.checkpoints.get(self.repo_key, 'G-047').snapshot['current_rebase_acceptance'],
                         self.acceptance)
        refreshed = self.handler.guard(context)
        self.assertEqual(refreshed.kind, ExecutionOutcomeKind.RETRY, refreshed.detail)
        row = self.state.row()
        self.assertEqual(row['active_sha'], self.latest)
        self.assertEqual(row['phase'], 'WORKING')
        self.assertEqual(row['worker_session_id'], 'saved-worker')
        audit = self.state.active_execplan(self.repo_key)
        self.assertEqual(audit['verify_failure_history'], '["prior verify failure"]')
        self.assertEqual(audit['review_failure_history'], '["prior Review failure"]')
        for field in ('worker_result', 'verify_result', 'review_result', 'worker_input_token',
                      'archive_token', 'verify_token', 'review_token', 'publication_token'):
            self.assertIsNone(row[field], field)
        definition = DefinitionSnapshot.deserialize(row['definition_snapshot'])
        self.assertEqual(definition.sources, ('docs/PRODUCT.md', self.source))
        self.assertEqual(definition.risk, 'high')
        self.assertEqual((self.worktree / self.plan_path).read_text(), self.new_text)
        self.assertEqual((self.worktree / 'app.py').read_text(), contents)
        self.assertEqual(git_command(self.worktree, 'merge-base', self.latest, 'HEAD'), self.latest)
        saved = self.checkpoints.get(self.repo_key, 'G-047')
        self.assertEqual(saved.snapshot['failure'], 'prior failure')
        self.assertIn(self.acceptance, saved.snapshot['current_rebase_acceptance_history'])
        self.assertTrue(any(item.get('stage_evidence') == {'audit': 'prior stage'}
                            for item in saved.snapshot['rebase_operation_history']))
        # Restart the handler so no transient definition or context is reused.
        self.handler = self.new_handler()
        context = self.handler.recover(self.handler._context())
        self.assertIsInstance(context, ExecutionContext)
        context = self.handler.guard(context)
        self.assertIsInstance(context, ExecutionContext)
        self.assertEqual(context.phase, ExecutionPhase.IMPLEMENTING)
        self.handler.run(context)
        call = self.effects.agent.run_agent.call_args
        self.assertIsNotNone(call, 'Worker must resume after revision refresh')
        self.assertEqual(call.kwargs['session_id'], 'saved-worker')

    def test_staged_work_remains_owned_without_becoming_completed_candidate(self):
        (self.worktree / 'app.py').write_text('saved_worker_change = True\nlocal_work = True\n')
        git_command(self.worktree, 'add', 'app.py')
        index = git_command(self.worktree, 'write-tree')
        context = self.handler.recover(self.handler._context())
        self.assertIsInstance(context, ExecutionContext)
        self.assertEqual(git_command(self.worktree, 'write-tree'), index)
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-worker')
        with self.assertRaises(journal.MutationEvidenceError):
            self.service().require_owned()

    def test_normal_and_final_metadata_validation_remain_current(self):
        for options in ({}, {'rebase_definition': self.definition}):
            with self.subTest(options=options), self.assertRaises(lm.ExecPlanMetadataError):
                lm.validate_active_execplan_metadata(self.repo, self.worktree, self.plan_path,
                    self.state.active_execplan(self.repo_key), **options)
        self.service().require_owned(checkpoint=self.checkpoints.get(self.repo_key, 'G-047'))

    def test_replayed_header_is_restored_after_git_and_commit_restart_preserves_body(self):
        # Real Git verifies the extra commit, index contents and post-commit crash recovery.
        body = '\n## Progress\nWorker implementation and 검증 기록 preserved.\n'.encode()
        header = self.old_text.split('---', 2)[1].replace('priority: normal', 'priority: high')
        (self.worktree / self.plan_path).write_bytes(b'---' + header.encode() + b'---\n' + body)
        git_command(self.worktree, 'add', self.plan_path)
        git_command(self.worktree, 'commit', '-m', 'Replayed historical declaration')
        replayed = git_command(self.worktree, 'rev-parse', 'HEAD')
        self.state.update_project(self.repo_key, definition_snapshot=None)
        prior = self.checkpoints.get(self.repo_key, 'G-047')
        self.checkpoints.record(self.repo_key, 'G-047', expected_generation=prior.generation,
            operation='rebase', expected_head=self.head, target_base_oid=self.base,
            observed_remote_oid=None, snapshot={})
        ports = lm._managed_rebase_runtime_effects()
        def interrupted(root, *args, **kwargs):
            result = ports.git(root, *args, **kwargs)
            if 'commit' in args:
                raise RuntimeError('after declaration commit')
            return result
        crashed = build_service(replace(ports, git=interrupted), self.connection, self.repo,
            self.state_dir, self.repo_key, 'G-047', self.worktree, self.branch)
        with self.assertRaisesRegex(RuntimeError, 'after declaration commit'):
            crashed.normalize_frontmatter()
        restored_head = git_command(self.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(restored_head, replayed)
        self.assertEqual(git_command(self.worktree, 'rev-parse', 'HEAD^'), replayed)

        self.assertTrue(self.service().normalize_frontmatter())
        self.assertFalse(self.service().normalize_frontmatter())

        self.assertEqual(git_command(self.worktree, 'rev-parse', 'HEAD'), restored_head)
        raw = (self.worktree / self.plan_path).read_bytes()
        self.assertEqual(raw.split(b'---', 2)[1], self.old_text.split('---', 2)[1].encode())
        self.assertEqual(raw.split(b'---', 2)[2], b'\n' + body)
        self.assertEqual((self.worktree / 'app.py').read_text(), 'saved_worker_change = True\n')
        self.service().require_owned(checkpoint=self.checkpoints.get(self.repo_key, 'G-047'))
        self.assertEqual(git_command(self.worktree, 'status', '--porcelain'), '')

    def test_uncommitted_worker_body_is_not_committed_by_declaration_restore(self):
        raw = self.old_text.replace('priority: normal', 'priority: high')
        (self.worktree / self.plan_path).write_text(raw)
        self.state.update_project(self.repo_key, definition_snapshot=None)
        current = self.checkpoints.get(self.repo_key, 'G-047')
        self.checkpoints.record(self.repo_key, 'G-047', expected_generation=current.generation,
            operation='rebase', expected_head=self.head, target_base_oid=self.base,
            observed_remote_oid=None, snapshot={})
        with self.assertRaises((RecoveryFailure, journal.MutationEvidenceError)):
            self.service().normalize_frontmatter()
        self.assertEqual((self.worktree / self.plan_path).read_text(), raw)
        self.assertEqual(git_command(self.worktree, 'rev-parse', 'HEAD'), self.head)

    def test_metadata_mismatch_is_visible_input_error_without_polling_retry(self):
        # Neither the applied nor exact target declaration permits this mixture.
        (self.worktree / self.plan_path).write_text(self.old_text.replace('risk: medium', 'risk: high'))
        outcome = self.handler.recover(self.handler._context())
        self.assertEqual(outcome.kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.assertEqual(outcome.recovery.observation.reason, RecoveryReason.PLAN_INPUT_INVALID)
        self.assertIn('target base', outcome.detail)
        self.effects.agent.run_agent.assert_not_called()
        self.assertEqual(self.state.row()['worker_session_id'], 'saved-worker')

    def test_latest_declaration_cannot_masquerade_as_old_target(self):
        (self.worktree / self.plan_path).write_text(self.new_text)
        (self.worktree / self.source).write_text('# Added source\n')
        with self.assertRaises(RecoveryFailure) as caught:
            self.service().require_owned()
        self.assertEqual(caught.exception.reason, RecoveryReason.PLAN_INPUT_INVALID)

    def test_unbound_target_cannot_use_historical_definition(self):
        with self.assertRaises(RecoveryFailure) as caught:
            self.service(target=self.latest).require_owned()
        self.assertEqual(caught.exception.reason, RecoveryReason.PLAN_INPUT_INVALID)

    def test_ownership_checks_still_reject_foreign_live_lease(self):
        git_command(self.worktree, 'push', 'origin', self.branch)
        with self.assertRaises(journal.MutationEvidenceError):
            self.service().require_owned(checkpoint=self.checkpoints.get(self.repo_key, 'G-047'))

    def test_historical_archive_inputs_are_pinned_and_catalog_races_still_reject(self):
        ports = lm._managed_rebase_runtime_effects()
        archives = mock.Mock(wraps=ports.requirement_archive_candidates)
        ports = replace(ports, requirement_archive_candidates=archives)
        service = build_service(ports, self.connection, self.repo, self.state_dir,
                                self.repo_key, 'G-047', self.worktree, self.branch)
        archives.assert_called_once_with(self.connection, self.repo, self.repo_key, self.base, 'G-047')
        first = service.adapters.read_inputs(service.expected)
        self.connection.execute("UPDATE exec_plans SET current_revision_sha='racing-catalog'")
        self.assertNotEqual(service.adapters.read_inputs(service.expected), first)
