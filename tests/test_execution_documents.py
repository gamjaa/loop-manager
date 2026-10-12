"""Documents execution policy regression tests."""

from tests.support.execution import ExecutionFixture
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from loop_manager_internal.execution import execution_handlers as handlers
import loop_manager as lm
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome, ExecutionOutcomeKind


from tests.support.execution_recovery_scenarios import ExecutionRecoveryScenarios
from tests.support.execution_phase_scenarios import ExecutionPhaseScenarios
from dataclasses import replace


class ExecutionDocumentsTests(ExecutionRecoveryScenarios, ExecutionPhaseScenarios, ExecutionFixture, TestCase):
    def test_document_reports_are_corrected_before_verify_and_review(self):
        patches = {'docs/PRODUCT.md': '-old product\n+new product\n',
                   'docs/ARCHITECTURE.md': '-old design\n+new design\n'}
        original_git = self.effects.workspace.git.side_effect
        def git(worktree, *args, **kwargs):
            if args[:2] == ('remote', 'get-url'):
                return SimpleNamespace(stdout='git@github.com:org/project.git', returncode=0)
            if args[0] == 'diff':
                output = ('M\0docs/PRODUCT.md\0M\0docs/ARCHITECTURE.md\0' if '--name-status' in args
                          else patches[args[-1]])
                return SimpleNamespace(stdout=output, returncode=0)
            return original_git(worktree, *args, **kwargs)
        self.effects.workspace.git.side_effect = git
        self.state.update_project('repo', verify_result='old PASS', review_result='VERDICT: PASS',
                                  verify_token='old-verify', review_token='old-review')
        self.handler.run(self.context())
        self.assertIsNone(self.state.row()['verify_token'])
        self.assertIsNone(self.state.row()['review_token'])
        self.effects.validation.parse_worker_result.return_value.summary = CommonGuidancePrReportTests.explanation(
            'docs/PRODUCT.md', patches['docs/PRODUCT.md'])
        result = self.handler.verify(self.context(), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.RETRY)
        self.assertEqual(self.state.row()['phase'], 'WORKING')
        self.assertIn('docs/ARCHITECTURE.md', self.state.row()['last_error'])
        self.assertIn('docs/PRODUCT.md', self.state.row()['last_error'])
        self.assertNotIn('verify', self.calls)
        self.assertNotIn('reviewer', self.calls)
        self.assertFalse(self.connection.execute('SELECT 1 FROM verify_receipts').fetchone())
        self.effects.validation.parse_worker_result.return_value.summary = '\n'.join(
            CommonGuidancePrReportTests.explanation(path, patch) for path, patch in patches.items())
        self.handler.run(self.context())
        result = self.handler.verify(self.context(), self.handler)
        self.assertEqual(result.kind, ExecutionOutcomeKind.PHASE_ADVANCED)
        self.assertEqual(self.state.row()['phase'], 'REVIEWING')
        self.assertEqual(self.calls.count('verify'), 1)

    def test_worker_correction_counts_ignore_validation_logs_and_sessions(self):
        context = self.handler.guard(self.context())
        definition = self.handler._definition_snapshot
        outcomes = []
        for attempt in range(3):
            output = f'PASS time={attempt}; diagnostics=/tmp/run-{attempt}/test.log'
            self.state.update_project('repo', worker_session_id=f'worker-{attempt}',
                reviewer_session_id=f'reviewer-{attempt}', verify_result=output,
                verify_token=definition.verify_token('base', 'owned-head', output),
                review_token=definition.review_token('base', 'owned-head'))
            outcomes.append(self.handler._recover(handlers.RecoveryReason.WORKER_CORRECTION,
                                                  'missing document explanation', context=context))
        self.assertEqual([item.recovery.occurrence for item in outcomes], [1, 2, 3])
        self.assertEqual(outcomes[-1].kind, ExecutionOutcomeKind.FINAL_BLOCK)
        self.effects.blocking.block_active_execplan.assert_called_once()

    def test_worker_correction_distinguishes_candidate_definition_and_base(self):
        context = self.handler.guard(self.context())
        candidate = self.effects.agent.capture_git_snapshot.return_value
        def occurrence():
            return self.handler._recover(handlers.RecoveryReason.WORKER_CORRECTION,
                'same error', context=context).recovery.occurrence
        self.assertEqual(occurrence(), 1)
        self.assertEqual(occurrence(), 2)
        candidate.head = 'changed-head'
        self.assertEqual(occurrence(), 1)
        candidate.dirty['app.py'] = (('unstaged',), 'new-content')
        self.assertEqual(occurrence(), 1)
        candidate.index = {'app.py': ('new-staged-content',)}
        self.assertEqual(occurrence(), 1)
        self.handler._definition_snapshot = replace(self.handler._definition_snapshot, body_hash='new-spec')
        self.assertEqual(occurrence(), 1)
        self.state.update_project('repo', active_sha='new-base')
        self.assertEqual(occurrence(), 1)

    def test_worker_correction_snapshot_failure_preserves_retry(self):
        self.effects.agent.capture_git_snapshot.side_effect = RuntimeError('cannot observe candidate')
        result = self.handler._recover(handlers.RecoveryReason.WORKER_CORRECTION,
                                      'same error', context=self.context())
        self.assertEqual(result.recovery.observation.reason, handlers.RecoveryReason.STATUS_FAILED)
        self.effects.blocking.block_active_execplan.assert_not_called()

    def test_draft_decision_race_reports_stored_decision(self):
        drafts = object.__new__(handlers.document_drafts.DocumentDraftRepository)
        drafts.connection = mock.Mock()
        drafts.connection.execute.return_value.rowcount = 0
        pending = SimpleNamespace(proposed_hash="hash", state="pending", repo="repo")
        decided = SimpleNamespace(state="rejected", decision_revision="other-revision")
        with (
            mock.patch.object(drafts, "get", side_effect=(pending, decided)),
            mock.patch.object(handlers.document_drafts, "ownership_clear"),
        ):
            with self.assertRaisesRegex(
                handlers.document_drafts.DocumentDraftError,
                "already rejected|이미 rejected",
            ) as raised:
                drafts.decide("draft", "hash", "reject", Path("."), "main")
        self.assertIn("other-revision", str(raised.exception))
        self.assertEqual(drafts.connection.execute.call_count, 1)
        self.assertIn("AND state = 'pending'", drafts.connection.execute.call_args.args[0])

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


class CommonGuidancePrReportTests(TestCase):
    def test_all_missing_changed_and_malformed_reports_are_returned_together(self):
        from loop_manager_internal.documents.pr_description import common_guidance_report, DocumentReportError
        patches = {'docs/PRODUCT.md': '-old product\n+new product\n',
                   'docs/ARCHITECTURE.md': '-old design\n+new design\n'}
        for summary in ('', '\n'.join(self.explanation(path, 'old patch') for path in patches),
                        self.explanation('docs/PRODUCT.md', 'old patch') + '\nDOCUMENT_CHANGE: invalid'):
            with self.subTest(summary=summary):
                git = mock.Mock(side_effect=[SimpleNamespace(stdout='M\0docs/PRODUCT.md\0M\0docs/ARCHITECTURE.md\0'),
                    *[SimpleNamespace(stdout=patch) for patch in patches.values()]])
                with self.assertRaises(DocumentReportError) as raised:
                    common_guidance_report(git, Path('/candidate'), 'base', 'head',
                                           'git@github.com:org/project.git', summary)
                for path, patch in patches.items():
                    self.assertIn(path, str(raised.exception))
                    self.assertIn(hashlib.sha256(patch.encode()).hexdigest(), str(raised.exception))
                self.assertIn('변경 문서 전체', str(raised.exception))
                self.assertEqual(git.call_count, 3)

    @staticmethod
    def explanation(path, patch, reason='Align implementation contracts', impact='Document accepted behavior'):
        import json
        return 'DOCUMENT_CHANGE: ' + json.dumps({
            'path': path, 'summary': 'Replace old contract with new contract',
            'reason': reason, 'impact': impact,
            'patch_sha256': hashlib.sha256(patch.encode('utf-8')).hexdigest(),
        })

    def test_existing_pr_push_refreshes_report_for_confirmed_head_before_merge_wait(self):
        from loop_manager_internal.execution import execution_publication_handler as publication
        context = SimpleNamespace(branch='branch', worktree=Path('/candidate'))
        row = {'pr_url': 'https://github.com/o/r/pull/1', 'publication_remote_oid': 'previous',
               'validated_base_sha': 'latest-base', 'pr_conflict_recovery': 0, 'worker_result': 'result'}
        state = mock.Mock()
        ports = SimpleNamespace(
            identity=SimpleNamespace(repo='repo', repo_path=Path('/repo'), state_dir=Path('/state')),
            pr_for_branch=lambda *_: {'state': 'OPEN'}, _row=lambda: row, checkpoint=None,
            state=state, _context=lambda: context, _post_effect_guard=lambda c: c,
            push_owned_pr_conflict_recovery=mock.Mock(return_value='confirmed-head'),
            snapshot=lambda: SimpleNamespace(plan={}), parse_worker_result=lambda *_: SimpleNamespace(summary='reason'),
            git=mock.Mock(return_value=SimpleNamespace(stdout='git@github.com:o/r.git\n')),
            command=mock.Mock(), operation_print=mock.Mock())
        with mock.patch.object(publication, 'common_guidance_report', return_value='final report') as report, mock.patch.object(
            publication, 'refresh_common_guidance_pr') as refresh:
            result = publication.PublicationHandler(ports)._publish_existing_pull_request(context, row, 'confirmed-head')
        self.assertEqual(result.kind, ExecutionOutcomeKind.SUCCESS)
        state.confirm_existing_pr_push.assert_called_once_with(None, 'confirmed-head')
        self.assertEqual(report.call_args.args[2:4], ('latest-base', 'confirmed-head'))
        self.assertEqual(refresh.call_args.args[-2:],
                         ({'url': row['pr_url'], 'state': 'OPEN', 'headRefOid': 'confirmed-head'}, 'final report'))
        state.open_pull_request.assert_called_once()
        ports._recover = mock.Mock(return_value='worker correction')
        state.reset_mock()
        with mock.patch.object(publication, 'common_guidance_report',
                               side_effect=publication.DocumentReportError('missing New.md')), mock.patch.object(
                publication, 'refresh_common_guidance_pr') as refresh:
            self.assertEqual(publication.PublicationHandler(ports)._publish_existing_pull_request(
                context, row, 'confirmed-head'), 'worker correction')
        refresh.assert_not_called()
        state.open_pull_request.assert_not_called()
        state.update_project.assert_called_once_with('repo', phase='WORKING',
            validated_base_sha=None, last_error='missing New.md')
        self.assertEqual(ports._recover.call_args.args[0].value, 'worker_correction')


    def test_open_pr_refresh_preserves_user_body_and_removes_stale_common_report(self):
        import json
        import tempfile
        from loop_manager_internal.documents import pr_description as pr
        old = pr.COMMON_REPORT_START + '\nold diff\n' + pr.COMMON_REPORT_END
        new = pr.COMMON_REPORT_START + '\nfinal diff\n' + pr.COMMON_REPORT_END
        original = 'User summary\n\n' + old + '\n\nUser notes\n'
        updated = []
        def command(args, **_kwargs):
            if args[2] == 'view':
                return SimpleNamespace(returncode=0, stdout=json.dumps(
                    {'body': original, 'headRefOid': 'head', 'state': 'OPEN'}))
            updated.append(Path(args[-1]).read_text(encoding='utf-8'))
            return SimpleNamespace(returncode=0, stdout='')
        pull_request = {'url': 'https://github.com/o/r/pull/1', 'state': 'OPEN', 'headRefOid': 'head'}
        with tempfile.TemporaryDirectory() as directory:
            for report in (new, ''):
                pr.refresh_common_guidance_pr(command, Path('/candidate'), Path(directory), pull_request, report)
                self.assertIn('User summary', updated[-1])
                self.assertIn('User notes', updated[-1])
                self.assertNotIn('old diff', updated[-1])
            self.assertIn('final diff', updated[0])
            self.assertNotIn(pr.COMMON_REPORT_START, updated[1])
            self.assertEqual(list((Path(directory) / 'logs').iterdir()), [])
            for malformed in (pr.COMMON_REPORT_END + pr.COMMON_REPORT_START,
                              pr.COMMON_REPORT_START,
                              old + old):
                original = malformed
                previous = len(updated)
                with self.assertRaises(RuntimeError):
                    pr.refresh_common_guidance_pr(command, Path('/candidate'), Path(directory), pull_request, new)
                self.assertEqual(len(updated), previous)
            for state in ('MERGED', 'CLOSED'):
                never = mock.Mock()
                pr.refresh_common_guidance_pr(never, Path('/candidate'), Path(directory), dict(pull_request, state=state), new)
                never.assert_not_called()

    def test_final_document_report_contains_only_filename_summary_reason_and_impact(self):
        from loop_manager_internal.documents.pr_description import common_guidance_report, COMMON_REPORT_START, COMMON_REPORT_END
        for status in ('A', 'M', 'D', 'R100'):
            with self.subTest(status=status):
                paths = ['docs/AGENTS.common.md', 'docs/rules.md'] if status.startswith('R') else ['docs/AGENTS.common.md']
                names = '\0'.join([status, *paths, ''])
                patch = 'diff --git a/docs/AGENTS.common.md b/docs/AGENTS.common.md\n-old\n+new ``` guidance\n'
                def git(_worktree, *args):
                    return SimpleNamespace(stdout=names if '--name-status' in args else patch)
                report = common_guidance_report(git, Path('/candidate'), 'a' * 40, 'b' * 40,
                                                'git@github.com:org/project.git', self.explanation(paths[-1], patch, '권한 경계를 보존한다.', '현재 작업 규칙 보존'))
                self.assertEqual(report, '\n'.join([
                    COMMON_REPORT_START, '## 최종 문서 변경', '',
                    '### ' + ' → '.join(paths), '',
                    '- 변경 요약: Replace old contract with new contract',
                    '- 이유: 권한 경계를 보존한다.',
                    '- 영향: 현재 작업 규칙 보존', '', COMMON_REPORT_END,
                ]))
                updated = common_guidance_report(git, Path('/candidate'), 'c' * 40, 'd' * 40,
                                                 'git@github.com:org/project.git', self.explanation(paths[-1], patch, 'Updated reason'))
                self.assertNotIn('a' * 40, updated)
                self.assertIn('Updated reason', updated)
                self.assertNotIn('d' * 40, updated)

    def test_role_instruction_changes_are_reported_with_renames_and_validated_diff(self):
        from loop_manager_internal.agents.common_guidance import ROLE_PATHS
        from loop_manager_internal.documents.pr_description import common_guidance_report
        for path in set(ROLE_PATHS.values()):
            for status in ('A', 'M', 'D', 'R100'):
                with self.subTest(path=path, status=status):
                    paths = [path, 'docs/renamed.md'] if status.startswith('R') else [path]
                    names = '\0'.join([status, *paths, ''])
                    patch = f'diff --git a/{path} b/{paths[-1]}\n-old\n+new\n'
                    git = mock.Mock(side_effect=(SimpleNamespace(stdout=names), SimpleNamespace(stdout=patch)))
                    report = common_guidance_report(git, Path('/candidate'), 'a' * 40, 'b' * 40,
                                                    'git@github.com:org/project.git', self.explanation(paths[-1], patch, 'role constraints'))
                    self.assertIn(path, report)
                    self.assertIn('role constraints', report)
                    self.assertNotIn(patch.rstrip(), report)
                    self.assertNotIn('#diff-', report)
                    self.assertEqual(git.call_args.args[-len(paths):], tuple(paths))

    def test_multiple_documents_and_rename_to_code_use_final_per_document_diffs(self):
        from loop_manager_internal.documents.pr_description import common_guidance_report
        changes = [('M', ['docs/PRODUCT.md']), ('D', ['docs/requirements/Old.md']),
                   ('R100', ['docs/plans/Old.md', 'src/new.py']),
                   ('A', ['README.md'])]
        names = ''.join('\0'.join([status, *paths, '']) for status, paths in changes)
        def git(_worktree, *args):
            if '--name-status' in args:
                return SimpleNamespace(stdout=names)
            paths = args[args.index('--') + 1:]
            return SimpleNamespace(stdout='diff for ' + ' '.join(paths) + '\n-old contract\n+new contract\n')
        for base, head in (('a' * 40, 'b' * 40), ('c' * 40, 'd' * 40)):
            report = common_guidance_report(git, Path('/candidate'), base, head,
                                            'git@github.com:org/project.git', '\n'.join(self.explanation(paths[-1],
                        'diff for ' + ' '.join(paths) + '\n-old contract\n+new contract\n')
                        for _, paths in changes))
            for status, paths in changes:
                self.assertIn('### ' + ' → '.join(paths), report)
                for path in paths:
                    self.assertIn(path, report)
                self.assertNotIn('diff for ' + ' '.join(paths), report)
            self.assertEqual(report.count('- 변경 요약:'), len(changes))
            self.assertEqual(report.count('- 이유:'), len(changes))
            self.assertEqual(report.count('- 영향:'), len(changes))
            for excluded in ('/compare/', '#diff-', '<details>', '```', '-old contract', '+new contract', 'patch_sha256'):
                self.assertNotIn(excluded, report)
            self.assertNotIn('AGENTS 승인', report)

    def test_document_specific_worker_explanation_is_selected_without_stale_paths(self):
        from loop_manager_internal.documents.pr_description import common_guidance_report
        git = mock.Mock(side_effect=(SimpleNamespace(stdout='M\0docs/PRODUCT.md\0'),
                                     SimpleNamespace(stdout='-old behavior\n+new behavior\n')))
        report = common_guidance_report(git, Path('/candidate'), 'base', 'head',
                                        'git@github.com:org/project.git',
                                        self.explanation('docs/PRODUCT.md', '-old behavior\n+new behavior\n', 'preserve accepted behavior') +
                                        '\ndocs/Old.md: stale description')
        self.assertIn('preserve accepted behavior', report)
        self.assertNotIn('stale description', report)
        self.assertNotIn('docs/Old.md', report)

    def test_missing_stale_and_changed_document_explanations_require_worker_correction(self):
        from loop_manager_internal.documents.pr_description import common_guidance_report, DocumentReportError
        patch = '-old contract\n+new contract\n'
        for summary in (
            'docs/Old.md: stale reason and impact',
            self.explanation('docs/Old.md', patch),
            self.explanation('docs/New.md', '+previous candidate\n'),
            'DOCUMENT_CHANGE: {"path": "docs/New.md"}',
        ):
            with self.subTest(summary=summary):
                git = mock.Mock(side_effect=(SimpleNamespace(stdout='M\0docs/New.md\0'),
                                             SimpleNamespace(stdout=patch)))
                with self.assertRaises(DocumentReportError):
                    common_guidance_report(git, Path('/candidate'), 'base', 'head',
                                           'git@github.com:org/project.git', summary)

    def test_new_pr_missing_document_report_returns_to_worker_before_publication(self):
        from loop_manager_internal.execution import execution_publication_handler as publication
        from loop_manager_internal.application.contracts import WorkerResultError
        context = SimpleNamespace(worktree=Path('/candidate'))
        row = {'worker_result': 'result', 'validated_base_sha': 'base'}
        state = mock.Mock()
        ports = SimpleNamespace(
            identity=SimpleNamespace(repo='repo'), state=state,
            snapshot=lambda: SimpleNamespace(plan={}, catalog_plan={
                'publication_sha': 'plan-sha', 'publication_path': 'docs/plans/Plan.md'}),
            git=mock.Mock(return_value=SimpleNamespace(stdout='value')),
            published_execplan=mock.Mock(), contract=SimpleNamespace(WorkerResultError=WorkerResultError),
            parse_worker_result=lambda *_: SimpleNamespace(summary='stale description'),
            build_pr_body=mock.Mock(), _recover=mock.Mock(return_value='worker correction'))
        with mock.patch.object(publication, 'common_guidance_report',
                               side_effect=publication.DocumentReportError('missing New.md')):
            self.assertEqual(publication.PublicationHandler(ports)._publish_new_pull_request(
                context, row), 'worker correction')
        ports.build_pr_body.assert_not_called()
        state.update_project.assert_called_once_with('repo', phase='WORKING',
            validated_base_sha=None, last_error='missing New.md')
        self.assertEqual(ports._recover.call_args.args[0].value, 'worker_correction')

    def test_unrelated_change_has_no_guidance_report_and_summary_is_preserved(self):
        from loop_manager_internal.documents.pr_description import common_guidance_report
        git = mock.Mock(return_value=SimpleNamespace(stdout='M\0src/app.py\0'))
        self.assertEqual(common_guidance_report(git, Path('/candidate'), 'a' * 40, 'b' * 40,
                                               'git@github.com:org/project.git', 'summary'), '')
        git.assert_called_once()
        worker = lm.WorkerPublication('Title', 'actual summary\n' + self.explanation(
            'docs/New.md', '+new\n'), 'test: PASS')
        body = lm.build_pr_body(worker, {'id': 'G-147', 'title': 'Guidance', 'sources': ['docs/PRODUCT.md']},
                                'docs/plans/[G-147] Plan.md', '## Purpose\n\nKeep instructions.\n',
                                'git@github.com:org/project.git', 'a' * 40,
                                common_guidance_change='final common change')
        self.assertIn('actual summary', body)
        self.assertIn('final common change', body)
        self.assertNotIn('DOCUMENT_CHANGE:', body)
