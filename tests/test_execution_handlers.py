"""Run the real phase policies with in-memory state and fake external effects."""
from dataclasses import fields, replace
import hashlib
import json
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

import execution_handlers as handlers
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
from tests.test_blocked_checkpoint import BlockedCheckpointGitAdapterTest
import blocked_checkpoint_state
import blocked_checkpoint
from worker_session import SessionBinding, SessionTransition
from tests.test_worker_outcome import decision as worker_decision

READY_WORKER_RESULT = 'WORKER_OUTCOME: {"kind":"ready_for_validation"}\nresult'
VALID_WORKER_RESULT = 'WORKER_OUTCOME: {"kind":"ready_for_validation"}\nvalid result'


class ExecutionHandlerTests(TestCase):
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
        self.effects.publication.push_managed_publication_branch.side_effect = lambda *a: self.calls.append('push') or 'head'
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
        journal_start.start()
        self.addCleanup(journal_start.stop)
        self.handler._rebase_journal_state = mock.Mock(return_value={})
        self.handler._begin_rebase_continuation = mock.Mock(
            return_value='test-rebase-token',
        )
        self.handler._finish_rebase_journal = mock.Mock(return_value=True)

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
        fixture = BlockedCheckpointGitAdapterTest(methodName='runTest')
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

        self.assertEqual(result.context.phase, ExecutionPhase.VERIFYING)
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

        self.assertEqual(resumed.context.phase, ExecutionPhase.VERIFYING)
        self.effects.agent.run_agent.assert_not_called()
        self.assertEqual(self.state.row()['worker_result'], legacy_result)

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
        def git(_repo, *args, **_kwargs):
            self.assertEqual(args[0], 'ls-remote')
            return SimpleNamespace(
                returncode=0,
                stdout='b' * 40 + '\trefs/heads/loop-manager/G-047\n',
            )
        self.effects.workspace.git.side_effect = git
        with (
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
                {'headRefOid': 'b' * 40},
            ))
            current.operation = 'pr_create'
            current.snapshot['remote_before'] = 'b' * 40
            self.assertTrue(self.handler._owned_checkpoint_remote(
                self.context(), row, 'b' * 40, current,
                {'headRefOid': 'b' * 40},
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

        self.assertEqual(recovered.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_result'], 'saved correction')
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
        self.assertEqual(first.context.phase, ExecutionPhase.VERIFYING)
        self.assertIsNone(checkpoint.get('repo', 'G-047').operation)
        self.assertEqual(checkpoint.get('repo', 'G-047').snapshot['stage_evidence'], {
            'head': 'owned-head', 'branch': 'loop-manager/G-047',
            'files': {'app.py': 'accepted-content'},
        })

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

    def test_completed_stage_rejects_replaced_clean_resolution_before_skip(self):
        self._assert_staged_rebase_resume(
            True, clean_after_stage=True, replaced_resolution=True,
        )

    def test_completed_autostash_stage_rejects_replaced_clean_resolution(self):
        self._assert_staged_rebase_resume(
            False, clean_after_stage=True, replaced_resolution=True,
        )

    def test_staged_rebase_resume_rejects_added_files(self):
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

        self.assertEqual(result.phase, ExecutionPhase.VERIFYING)
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
        self.assertEqual(resumed.phase, ExecutionPhase.VERIFYING)
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
        self.assertEqual(resumed.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(attempts, {'add': 1, 'continue': 2})
        self.effects.agent.run_agent.assert_not_called()

    def test_pending_stage_rejects_changed_worktree_before_staging(self):
        self._assert_staged_rebase_resume(True, changed=True, unstaged=True)

    def _assert_staged_rebase_resume(self, rebase_metadata, *, changed=False,
                                    unstaged=False, untracked=False,
                                    clean_after_stage=False, replaced_resolution=False):
        checkpoint = ExecutionCheckpointRepository(self.connection)
        self.effects = replace(self.effects, checkpoint=checkpoint)
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
                return SimpleNamespace(stdout='allowed.py\0' if untracked else '', returncode=0)
            if args == ('diff', '--cached', '--quiet'):
                return SimpleNamespace(stdout='', returncode=int(not clean_after_stage))
            return self.observed_git(path, *args, **kwargs)
        self.effects.workspace.git.side_effect = staged_git
        self.effects.workspace._rebase_in_progress_for_branch.return_value = rebase_metadata
        result = self.handler.recover(self.context())
        self.effects.agent.run_agent.assert_not_called()
        additions = [call for call in self.effects.workspace.git.call_args_list
                     if call.args[1:3] == ('add', '-A')]
        self.assertEqual(len(additions), int(not changed and (unstaged or untracked)))
        for call in additions:
            self.assertEqual(call.args[3], '--')
            self.assertEqual(set(call.args[4:]),
                             {'app.py', 'allowed.py'} if unstaged or untracked
                             else {'app.py'})
        if changed or replaced_resolution:
            self.assertEqual(result.kind, ExecutionOutcomeKind.FINAL_BLOCK)
            self.assertEqual(checkpoint.get('repo', 'G-047').operation, 'rebase')
            self.assertFalse(any('--continue' in call.args or '--skip' in call.args
                                 for call in self.effects.workspace.git.call_args_list))
            self.effects.validation.verify.assert_not_called()
            return
        self.assertEqual(result.phase, ExecutionPhase.VERIFYING)
        self.assertEqual(self.state.row()['worker_result'], 'saved correction')
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

    def _assert_post_push_pr_attachment(self, pr_head, accepted):
        self.state.update_project('repo', validated_base_sha='base')
        self.effects.publication.push_managed_publication_branch.side_effect = (
            lambda *_: self.calls.append('push') or 'base'
        )
        self.effects.workspace.remote_branch_oid.side_effect = (
            lambda *_: 'base' if 'push' in self.calls else None
        )
        self.effects.publication.pr_for_branch.side_effect = lambda *_: (
            {'url': 'existing', 'state': 'OPEN', 'headRefOid': pr_head,
             'headRefName': 'loop-manager/G-047'} if 'push' in self.calls else None
        )
        self.effects.publication.reconcile_existing_pull_request.return_value = 0
        result = self.handler.publish(self.context('PUBLISHING'), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS if accepted
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
            if args[0] == 'commit':
                raise lm.GitLockError('index.lock')
            return self.git(path, *args, **kwargs)

        self.effects.workspace.git.side_effect = failing_git
        with self.assertRaises(lm.GitLockError):
            self.handler._commit_owned(self.context('VERIFYING'), 'change')
        saved = checkpoint.get('repo', 'G-047')
        self.assertEqual(saved.operation, 'commit')
        self.assertEqual(saved.expected_head, 'before-head')
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
                self.state.update_project('repo', phase=phase, active_sha='base', validated_base_sha='base')
                self.remote = 'new-base'
                self.calls.clear()
                context = self.context()
                if phase == 'VERIFYING':
                    result = self.handler.verify(context, self.handler)
                    self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                    self.assertEqual(self.calls.count('verify'), 0)
                    result = self.handler.verify(self.handler._context(), self.handler)
                    self.assertEqual(result.context.phase, ExecutionPhase.REVIEWING)
                    self.assertEqual(self.calls.count('verify'), 1)
                else:
                    result = self.handler.publish(context, self.handler)
                    self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
                    self.assertEqual(self.calls.count('verify'), 0)
                    self.assertIsNone(self.state.row()['review_result'])
                self.assertEqual(self.calls.count('rebase'), 1)
                self.assertEqual(self.state.row()['active_sha'], 'new-base')
                if phase == 'VERIFYING':
                    self.assertLess(self.calls.index('fetch'), self.calls.index('rebase'))
                    self.assertLess(self.calls.index('fetch'), self.calls.index('commit'))
                    self.assertLess(self.calls.index('commit'), self.calls.index('rebase'))
                    self.assertLess(self.calls.index('rebase'), self.calls.index('verify'))

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
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], READY_WORKER_RESULT)

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
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(self.state.row()['worker_result'], READY_WORKER_RESULT)

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
        self.assertEqual(self.state.row()['worker_result'], 'worker-result')

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
        self.assertEqual(self.calls, ['rev-parse'])

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
