"""게시된 사전 승인과 Worker가 만든 신규 결정의 경계를 검증한다."""

import sqlite3
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager as lm
from loop_manager_internal.documents import guidance_approval as ga
from tests.support.guidance import PATCH, active_connection, git, guidance_plan, repository


class CommonGuidanceApprovalCompatibilityTests(unittest.TestCase):
    def test_published_legacy_common_keeps_original_target_baseline_and_patch(self):
        from types import SimpleNamespace
        from tests.support.database import runtime_connection
        from loop_manager_internal.documents import guidance_preapproval as pre, guidance_service as service
        baseline = 'a' * 40
        old = guidance_plan().replace('Scope: `project`', 'Scope: `common`')
        approved = guidance_plan(decision='approve', revision='user-1').replace(
            'Scope: `project`', 'Scope: `common`').replace('Revision: `user-1`\n',
                f'Revision: `user-1`\nBaseline: `{baseline}`\n')
        source = ga.parse_guidance_document(approved)
        def git_port(_repo, *args, **_kwargs):
            if args[0] == 'log':
                return SimpleNamespace(returncode=0, stdout='c' * 40 + '\n')
            if args[0] == 'ls-tree':
                return SimpleNamespace(returncode=0, stdout='100644 blob oid\tAGENTS.md\n')
            return SimpleNamespace(returncode=0, stdout=old if args[-1].startswith('c' * 40 + '^:') else approved)
        connection = runtime_connection()
        self.addCleanup(connection.close)
        effects = pre.PrepareGuidancePreapprovalsEffects(
            _guidance_document=mock.Mock(return_value=source), _guidance_rows=mock.Mock(return_value=()),
            _invalidate_guidance_approval=mock.Mock(), _validate_guidance_proposal_scope=service._validate_guidance_proposal_scope,
            git=git_port, is_loop_manager_repository=mock.Mock(return_value=True))
        plan = {'plan_id': 'G-047', 'path': 'docs/plans/old.md', 'plan_sha': 'b' * 40,
                'current_revision_sha': 'b' * 40, 'agent_snapshot': None}
        native_run = pre.subprocess.run
        def run(arguments, **kwargs):
            if arguments[:2] == ['git', 'show']:
                return SimpleNamespace(returncode=0, stdout=b'# Agents\n')
            return native_run(arguments, **kwargs)
        with mock.patch.object(pre, '_guidance_target_state', return_value=(b'# Agents\n', 0o644)), mock.patch.object(
            pre.subprocess, 'run', side_effect=run):
            self.assertFalse(pre.prepare_guidance_preapprovals(effects, connection, Path('/repo'), 'repo', Path('/worktree'), plan))
        row = connection.execute('SELECT * FROM guidance_approvals').fetchone()
        self.assertEqual((row['target_path'], row['scope'], row['baseline_content'], row['approved_patch']),
                         ('AGENTS.md', 'common', b'# Agents\n', source.proposals[0].patch))
        self.assertEqual(row['decision_hash'], source.proposals[0].decision.identity)
        effects._invalidate_guidance_approval.assert_not_called()

    def test_revisionless_preapproval_uses_shared_parser_and_keeps_registered_identity(self):
        from types import SimpleNamespace
        from tests.support.database import runtime_connection
        from loop_manager_internal.documents import guidance_preapproval as pre, guidance_service as service
        baseline = 'a' * 40
        published = guidance_plan(decision='approve', patch=PATCH).replace(
            'Revision: ``\n', f'Baseline: `{baseline}` <!-- 실제 기준 commit 전체 SHA -->\nComment: ``\n'
        ) + '<!-- END USER DECISION -->\n## 메모\nDecision: `reject`\n'
        source = ga.parse_guidance_document(published)
        effects = pre.PrepareGuidancePreapprovalsEffects(
            _guidance_document=mock.Mock(return_value=source),
            _guidance_rows=mock.Mock(return_value=()),
            _invalidate_guidance_approval=mock.Mock(),
            _validate_guidance_proposal_scope=service._validate_guidance_proposal_scope,
            git=mock.Mock(side_effect=lambda _repo, *args, **kwargs: SimpleNamespace(
                returncode=0, stdout='100644 blob oid\tAGENTS.md\n' if args[0] == 'ls-tree' else published)),
            is_loop_manager_repository=mock.Mock(return_value=False),
        )
        connection = runtime_connection()
        self.addCleanup(connection.close)
        plan = {'plan_id': 'G-020', 'path': 'docs/plans/test.md', 'plan_sha': 'b' * 40,
                'current_revision_sha': 'b' * 40, 'agent_snapshot': None}
        with mock.patch.object(pre, '_guidance_target_state', return_value=(b'# Agents\n', 0o644)), mock.patch.object(
            pre, 'subprocess', mock.Mock(run=mock.Mock(return_value=SimpleNamespace(stdout=b'# Agents\n')))
        ):
            self.assertFalse(pre.prepare_guidance_preapprovals(effects, connection, Path('/repo'), 'repo', Path('/work'), plan))
            row = connection.execute('SELECT * FROM guidance_approvals').fetchone()
            self.assertEqual((row['decision'], row['decision_revision'], row['approved_patch']), ('approve', '', PATCH))
            self.assertEqual(row['decision_hash'], source.proposals[0].decision.identity)
            effects._guidance_rows.return_value = (row,)
            # Display-only changes on restart do not register another approval.
            edited = published.replace('Comment: ``', 'Comment: `user explanation`').replace('## 메모', '## changed memo')
            edited = edited.replace(f'```diff\n{PATCH}```\n<!-- END', '```diff\n```\n<!-- END')
            effects._guidance_document.return_value = ga.parse_guidance_document(edited)
            self.assertFalse(pre.prepare_guidance_preapprovals(effects, connection, Path('/repo'), 'repo', Path('/work'), plan))
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM guidance_approvals').fetchone()[0], 1)
        effects._invalidate_guidance_approval.assert_not_called()

    def test_new_common_proposal_is_rejected_before_worker_scope_or_storage(self):
        from types import SimpleNamespace
        from loop_manager_internal.documents import guidance_service as service
        old = guidance_plan().replace('Scope: `project`', 'Scope: `common`')
        source = ga.parse_guidance_document(old)
        git_port = mock.Mock(side_effect=lambda _repo, *args, **_kwargs: SimpleNamespace(
            returncode=0 if args[0] != 'show' else 1,
            stdout='c' * 40 + '\n' if args[0] == 'log' else ''))
        effects = SimpleNamespace(is_loop_manager_repository=lambda _: True, git=git_port,
            _validate_guidance_proposal_scope=service._validate_guidance_proposal_scope)
        connection = mock.Mock()
        with self.assertRaisesRegex(lm.AgentScopeError, 'Scope project'):
            service._block_for_new_guidance(effects, connection, Path('/repo'), 'repo', Path('/worktree'),
                {'current_revision_sha': 'b' * 40, 'plan_sha': 'b' * 40, 'path': 'docs/plans/new.md'},
                source, source.proposals, '')
        connection.execute.assert_not_called()


class GuidancePreapprovalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo, self.plan_path = repository(Path(self.directory.name))
        self.plan_file = self.repo / self.plan_path
        self.scope = mock.patch.object(lm, 'is_loop_manager_repository', return_value=False)
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def publish(self, text, *, database=':memory:'):
        if 'Baseline:' not in text:
            baseline = git(self.repo, 'rev-parse', 'HEAD')
            text = re.sub(r'(Revision: `[^`\n]+`\n)', rf'\1Baseline: `{baseline}`\n', text)
        self.plan_file.write_text(text, encoding='utf-8')
        git(self.repo, 'add', self.plan_path)
        git(self.repo, 'commit', '-m', '사용자 결정 게시')
        git(self.repo, 'update-ref', 'refs/remotes/origin/main', git(self.repo, 'rev-parse', 'HEAD'))
        self.connection = active_connection(self.repo, self.plan_path, database)
        self.addCleanup(self.connection.close)
        return lm.active_execplan(self.connection, str(self.repo))

    def prepare(self, plan):
        return lm.prepare_guidance_preapprovals(
            self.connection, self.repo, str(self.repo), self.repo, plan,
        )

    def rows(self):
        return lm._guidance_rows(self.connection, str(self.repo), 'G-020')

    def test_published_decisions_use_existing_scope_result_and_publication_checks(self):
        for decision in ('approve', 'replace', 'reject'):
            with self.subTest(decision=decision):
                patch = PATCH.replace('Approved rule', 'Replacement rule') if decision == 'replace' else ''
                plan = self.publish(guidance_plan(decision=decision, revision=decision, patch=patch))
                baseline = (self.repo / 'AGENTS.md').read_bytes()
                self.assertFalse(self.prepare(plan))
                self.assertEqual((self.repo / 'AGENTS.md').read_bytes(), baseline)
                row, = self.rows()
                self.assertEqual(row['decision'], decision)
                self.assertEqual(row['decision_revision'], decision)
                self.assertIn('AGENTS.md', lm._guidance_worker_targets(self.connection, str(self.repo), 'G-020'))
                document = ga.parse_guidance_document(self.plan_file.read_text(encoding='utf-8'))
                expected = lm._guidance_final_expected(self.rows(), {p.proposal_id: p for p in document.proposals})[0]
                contents, _ = next(iter(expected.values()))
                (self.repo / 'AGENTS.md').write_bytes(contents)
                self.assertFalse(lm.inspect_worker_guidance_result(
                    self.connection, self.repo, str(self.repo), self.repo, plan, '변경 적용',
                ))
                self.assertFalse(lm.validate_guidance_publication(
                    self.connection, str(self.repo), self.repo, plan, '검증',
                ))
                # 다음 subtest의 공개 기준을 원래 내용으로 복원한다.
                (self.repo / 'AGENTS.md').write_bytes(b'# Agents\n')

    def test_local_approval_is_not_imported_and_new_worker_approval_is_rejected(self):
        self.connection = active_connection(self.repo, self.plan_path)
        self.addCleanup(self.connection.close)
        plan = lm.active_execplan(self.connection, str(self.repo))
        self.plan_file.write_text(guidance_plan(decision='approve', revision='local'), encoding='utf-8')
        self.assertFalse(self.prepare(plan))
        self.assertEqual(self.rows(), ())
        with self.assertRaisesRegex(lm.AgentScopeError, '미리 확정'):
            lm.inspect_worker_guidance_result(
                self.connection, self.repo, str(self.repo), self.repo, plan, '위조된 승인',
            )

    def test_changed_local_proposal_or_decision_is_not_imported(self):
        plan = self.publish(guidance_plan(decision='approve', revision='published'))
        original = self.plan_file.read_text(encoding='utf-8')
        for changed in (original.replace('published', 'worker'), original.replace('Approved rule', 'Extra rule')):
            self.plan_file.write_text(changed, encoding='utf-8')
            with self.assertRaisesRegex(lm.AgentScopeError, '게시된 사전 승인과 다릅니다'):
                self.prepare(plan)
            self.assertEqual(self.rows(), ())

    def test_stale_baseline_blocks_before_worker_and_existing_user_decision_resumes(self):
        plan = self.publish(guidance_plan(decision='approve', revision='published'))
        baseline = b'# Agents\nOther rule\n'
        (self.repo / 'AGENTS.md').write_bytes(baseline)
        git(self.repo, 'add', 'AGENTS.md')
        git(self.repo, 'commit', '-m', '승인 뒤 기준 변경')
        current = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'update-ref', 'refs/remotes/origin/main', current)
        self.connection.execute('UPDATE exec_plans SET current_revision_sha = ?', (current,))
        self.connection.commit()
        plan = lm.active_execplan(self.connection, str(self.repo))
        self.assertTrue(self.prepare(plan))
        row, = self.rows()
        self.assertEqual(bytes(row['baseline_content']), baseline)
        self.assertEqual(row['invalidated_decision_hash'], row['decision_hash'])
        blocked = lm.plan_blocked_execplans(self.connection, str(self.repo))[0]
        self.assertEqual(blocked['blocked_reason'], 'guidance_approval_required')
        self.assertEqual((self.repo / 'AGENTS.md').read_bytes(), baseline)
        with (
            mock.patch.object(lm, 'validate_execplan_workspace_identity'),
            mock.patch.object(lm, 'make_worktree'),
            mock.patch.object(lm, 'validate_blocked_agent_scope'),
        ):
            self.assertFalse(lm.resume_guidance_approval_if_decided(
                self.connection, self.repo, str(self.repo), 'main', self.repo,
            ))
            replacement = PATCH.replace('@@ -1 +1,2 @@', '@@ -1,2 +1,3 @@') + ' Other rule\n'
            approved = self.plan_file.read_text(encoding='utf-8').replace(
                'Decision: `approve`', 'Decision: `replace`',
            ).replace('Revision: `published`', 'Revision: `current-baseline`')
            approved = approved.replace('Patch:\n```diff\n```', f'Patch:\n```diff\n{replacement}```')
            self.plan_file.write_text(approved, encoding='utf-8')
            self.assertTrue(lm.resume_guidance_approval_if_decided(
                self.connection, self.repo, str(self.repo), 'main', self.repo,
            ))
        self.assertFalse(self.prepare(plan))
        document = ga.parse_guidance_document(self.plan_file.read_text(encoding='utf-8'))
        expected = lm._guidance_final_expected(self.rows(), {p.proposal_id: p for p in document.proposals})[0]
        self.assertEqual(next(iter(expected.values()))[0], b'# Agents\nApproved rule\nOther rule\n')

    def test_multiple_preapprovals_apply_in_published_order(self):
        first = guidance_plan(decision='approve', revision='batch')
        second = guidance_plan(decision='approve', revision='batch')
        second = second[second.index('### Agent Guidance Proposal:'):]
        next_patch = PATCH.replace('@@ -1 +1,2 @@', '@@ -1,2 +1,3 @@').replace('+Approved rule', ' Approved rule\n+Second rule')
        second = second.replace('worker-rule', 'second-rule').replace(PATCH, next_patch)
        plan = self.publish(first + '\n' + second)
        (self.repo / 'AGENTS.md').write_bytes(b'# Agents\n')
        self.assertFalse(self.prepare(plan))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['expected_content_hash'], rows[1]['expected_content_hash'])
        self.assertEqual(rows[0]['expected_content_hash'], ga.content_hash(b'# Agents\nApproved rule\nSecond rule\n'))

    def test_mixed_pending_batch_and_common_scope_are_rejected_before_import(self):
        approved = guidance_plan(decision='approve', revision='published')
        pending = guidance_plan().split('### Agent Guidance Proposal:', 1)[1]
        mixed = approved + '\n### Agent Guidance Proposal:' + pending.replace('worker-rule', 'pending-rule')
        plan = self.publish(mixed)
        with self.assertRaisesRegex(lm.AgentScopeError, 'pending'):
            self.prepare(plan)
        self.assertEqual(self.rows(), ())
        plan = self.publish(approved.replace('Scope: `project`', 'Scope: `common`'))
        with self.assertRaisesRegex(lm.AgentScopeError, '자체 저장소'):
            self.prepare(plan)
        self.assertEqual(self.rows(), ())

    def test_unknown_baseline_cannot_be_imported(self):
        text = guidance_plan(decision='approve', revision='published').replace(
            'Revision: `published`\n', 'Revision: `published`\nBaseline: `' + '0' * 40 + '`\n',
        )
        plan = self.publish(text)
        with self.assertRaisesRegex(lm.AgentScopeError, '선행 commit'):
            self.prepare(plan)
        self.assertEqual(self.rows(), ())

    def test_reopen_does_not_duplicate_or_restore_a_superseded_decision(self):
        database = Path(self.directory.name) / 'state.sqlite3'
        plan = self.publish(guidance_plan(decision='approve', revision='published'), database=database)
        self.assertFalse(self.prepare(plan))
        original = dict(self.rows()[0])
        self.connection.close()
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.assertFalse(self.prepare(plan))
        self.assertEqual(dict(self.rows()[0]), original)

    def test_registered_preapproval_rechecks_baseline_before_first_worker_after_restart(self):
        plan = self.publish(guidance_plan(decision='approve', revision='published'))
        self.assertFalse(self.prepare(plan))
        (self.repo / 'AGENTS.md').write_bytes(b'# Agents\nChanged after registration\n')
        self.assertTrue(self.prepare(plan))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['invalidated_decision_hash'], self.rows()[0]['decision_hash'])

    def test_registered_preapproval_accepts_checkout_eol_change_before_worker(self):
        plan = self.publish(guidance_plan(decision='approve', revision='published'))
        (self.repo / 'AGENTS.md').write_bytes(b'# Agents\n')
        self.assertFalse(self.prepare(plan))
        (self.repo / 'AGENTS.md').write_bytes(b'# Agents\r\n')
        self.assertFalse(self.prepare(plan))
        self.assertEqual(self.rows()[0]['expected_content_hash'], ga.content_hash(b'# Agents\r\nApproved rule\r\n'))

    def test_checkout_crlf_does_not_invalidate_or_change_line_ending_style(self):
        plan = self.publish(guidance_plan(decision='approve', revision='published'))
        (self.repo / 'AGENTS.md').write_bytes(b'# Agents\r\n')
        self.assertFalse(self.prepare(plan))
        row, = self.rows()
        self.assertEqual(bytes(row['baseline_content']), b'# Agents\r\n')
        self.assertEqual(row['expected_content_hash'], ga.content_hash(b'# Agents\r\nApproved rule\r\n'))

    def test_failed_stale_snapshot_rolls_back_approval_registration(self):
        plan = self.publish(guidance_plan(decision='approve', revision='published'))
        (self.repo / 'AGENTS.md').write_bytes(b'# Agents\nChanged\n')
        with mock.patch.object(lm, 'capture_git_snapshot', side_effect=OSError('snapshot failed')):
            with self.assertRaises(OSError):
                self.prepare(plan)
        self.assertEqual(self.rows(), ())
