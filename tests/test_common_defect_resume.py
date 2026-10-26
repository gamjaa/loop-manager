"""Native correction evidence gates existing cause assessment, never task success."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support.database import runtime_connection
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.storage import common_defect_state, verify_receipts, runtime_state, execution_checkpoint_state
from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository
from loop_manager_internal.verification.inputs import InputObservation
from loop_manager_internal.recovery.checkpoint_recovery import _assess_user_decision_cause


class CommonDefectResumeTests(unittest.TestCase):
    def setUp(self):
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.head, self.base, self.merge, self.remote = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        self.definition = DefinitionSnapshot(revision_sha=self.base, path='docs/plans/[G-047] Runtime.md',
            title='Runtime', sources=('docs/PRODUCT.md',), depends_on=(), complexity='medium', risk='medium',
            priority='normal', body_hash='body', source_fingerprint='source', archive_candidates=())
        report = dict(problem='common cause', scope='outside selected work', evidence='actual traceback',
                      locations=[dict(path='shared.py', line=1)])
        self.raw = 'COMMON_DEFECT_REPORT: ' + json.dumps(report) + '\nOriginal failure and question'
        self.feedback = ValidationFeedbackRepository(self.connection)
        for plan_id in ('G-048', 'G-049'):
            self.connection.execute("INSERT INTO exec_plans "
                "(repo,plan_id,path,title,sources,depends_on,priority,content_hash,status,plan_sha,active_sha,branch,worktree,workspace_owned,worker_result) "
                "VALUES ('repo',?,?,'Affected','[\"docs/PRODUCT.md\"]','[]','normal','hash','READY',?,?,'owned',?,1,?)",
                (plan_id, 'docs/plans/[' + plan_id + '] Affected.md', 'e' * 40, 'e' * 40, '/state/' + plan_id, self.raw))
            self.feedback.record_result('repo', plan_id, 'definition', 'worker', 'returned', self.raw)
            runtime_state.RuntimeStateRepository(self.connection).block_plan('repo', plan_id,
                reason_code='common_defect_pending', evidence='Original cause', release_fingerprint='input', clear_projection=False)
        with self.connection:
            common_defect_state.confirm(self.connection, 'repo', self.base, self.base, [dict(
                reports=[1, 2], plan_id='G-047', target_path=self.definition.path, target_revision='plan-sha',
                source='docs/PRODUCT.md', quote='Required contract.', cause='missing guard', completion='shared callers fixed',
                code=[dict(path='shared.py', quote='return broken')])])
            verify_receipts.start_cycle(self.connection, 'repo', 'G-047')
            identifier, selection = verify_receipts.record(self.connection, verify_receipts.selected(self.connection, 'repo', 'G-047'),
                head=self.head, observation=InputObservation('code-input'), kind='run', reason='actual full run')
            verify_receipts.complete(self.connection, selection, identifier, passed=True, output='full verify passed')
            self.connection.execute("UPDATE exec_plans SET status='PR_OPEN',definition_snapshot=?,validated_base_sha=?,"
                "verify_result='full verify passed',verify_token=?,review_result='VERDICT: PASS',review_token=?,pr_url='https://example/pr/1',pr_head_oid=? "
                "WHERE plan_id='G-047'", (self.definition.serialize(), self.base,
                    self.definition.verify_token(self.base, self.head, 'full verify passed'),
                    self.definition.review_token(self.base, self.head), self.head))
        self.effects = SimpleNamespace(git=mock.Mock(side_effect=self.git),
            capture_git_snapshot=mock.Mock(return_value=object()), validate_agent_scope=mock.Mock(),
            run_agent=mock.Mock(side_effect=self.assess))
        self.resolved = True
        self.quotes = [dict(source='docs/PRODUCT.md', quote='Required contract.'),
                       dict(source='shared.py', quote='return fixed')]

    def git(self, root, *args, **kwargs):
        self.assertFalse(self.connection.in_transaction, 'External observations must not hold a DB write transaction')
        if args[0] == 'rev-parse':
            return SimpleNamespace(returncode=0, stdout=self.remote)
        if args[0] == 'merge-base':
            return SimpleNamespace(returncode=0, stdout='')
        if args[0] == 'show':
            revision, path = args[1].split(':', 1)
            text = ('Required contract.' if path == 'docs/PRODUCT.md' else
                    ('return fixed' if revision == self.remote else 'return broken') if path == 'shared.py'
                    else 'Unchanged plan or design.')
            return SimpleNamespace(returncode=0, stdout=text)
        raise AssertionError(args)

    def assess(self, *args, **kwargs):
        self.assertFalse(self.connection.in_transaction)
        return 0, 'USER_DECISION_ASSESSMENT: ' + json.dumps(dict(
            resolved=self.resolved, detail='cause assessment', evidence=self.quotes))

    def observe_merge(self, *, merge=None):
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-047'")
            common_defect_state.record_merge(self.connection, 'repo', 'G-047', 'https://example/pr/1', self.head, merge or self.merge)

    def evaluate(self, plan_id='G-048'):
        plan = self.connection.execute('SELECT * FROM exec_plans WHERE plan_id=?', (plan_id,)).fetchone()
        return _assess_user_decision_cause(self.effects, self.connection, Path('/repo'), Path('/state'), 'repo', plan, Path('/config'))

    def test_plan_publication_and_merge_status_alone_cannot_resume(self):
        self.assertFalse(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['ready'])
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-047'")
        self.assertFalse(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['ready'])
        self.assertFalse(self.evaluate()[0])
        self.effects.run_agent.assert_not_called()

    def test_confirmed_self_revision_allows_owner_execution_without_claiming_merge(self):
        self.feedback.record_result('repo', 'G-048', 'original-definition', 'worker', 'returned', self.raw)
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET current_revision_sha='revised' WHERE plan_id='G-048'")
            common_defect_state.confirm(self.connection, 'repo', 'revised', 'revised', [dict(
                reports=[3], plan_id='G-048', target_path='docs/plans/[G-048] Affected.md', target_revision=None,
                source='docs/PRODUCT.md', quote='Required contract.', cause='owner guard', completion='guard fixed',
                code=[dict(path='shared.py', quote='return broken')])])
        state = common_defect_state.reassessment(self.connection, 'repo', 'G-048')
        self.assertTrue(state['owner_execution'])
        self.assertFalse(state['ready'])
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET current_revision_sha='unconfirmed' WHERE plan_id='G-048'")
        self.assertFalse(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['owner_execution'])

        with self.connection:
            self.connection.execute("UPDATE exec_plans SET current_revision_sha='revised' WHERE plan_id='G-048'")
            common_defect_state.confirm(self.connection, 'repo', self.base, self.base, [dict(
                reports=[3], plan_id='G-047', target_path=self.definition.path, target_revision='plan-sha',
                source='docs/PRODUCT.md', quote='Required contract.', cause='another guard', completion='other guard fixed',
                code=[dict(path='shared.py', quote='return broken')])])
        self.assertFalse(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['owner_execution'])

    def test_manual_member_merge_uses_shared_run_and_its_published_review(self):
        from loop_manager_internal.application.contracts import VerifyOutput
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        store = ManualWorkRepository(self.connection)
        raw_verify = 'full verify log\n' * 1000 + 'full verify passed\n'
        bounded_verify = str(VerifyOutput(raw_verify))
        with self.connection:
            self.connection.execute("INSERT INTO exec_plans (repo,plan_id,path,title,sources,depends_on,priority,content_hash,status,plan_sha) "
                "VALUES ('repo','G-050','docs/plans/[G-050] Member.md','Member','[\"docs/PRODUCT.md\"]','[]','normal','hash','MANUAL_PENDING','member-revision')")
            group = store.create('repo', 'submit', 'shared', [], self.head, self.base, members=('G-047', 'G-050'))
            group = store.update(group, owner_id='G-047', branch='shared', state='AUTOMATIC')
            store.accept(group, head=self.head, base=self.base, input_token='candidate')
            group = store.get('repo', group['group_id'])
            review = 'MANUAL_PLAN_RESULT: G-047 PASS\nMANUAL_PLAN_RESULT: G-050 PASS\nVERDICT: PASS'
            self.connection.execute("UPDATE exec_plans SET review_result=?,verify_result=?,verify_token=? WHERE plan_id='G-047'",
                (review, bounded_verify, self.definition.verify_token(self.base, self.head, bounded_verify)))
            self.connection.execute('UPDATE verify_receipts SET result=?', (bounded_verify,))
        store.record(group, 'verify', self.head, self.base, 'verify-input', 'PASS', raw_verify)
        store.record(group, 'review', self.head, self.base, 'review-input', 'PASS', review)
        with self.connection:
            store.publish_group('repo', 'G-047', dict(pr_url='https://example/pr/1', pr_head_oid=self.head,
                publication_remote_oid=self.head), stage_tokens=dict(verify='verify-input', review='review-input'))
            link = json.loads(self.connection.execute('SELECT evidence FROM common_defect_links LIMIT 1').fetchone()[0])
            link.update(reports=[2], plan_id='G-050', target_path='docs/plans/[G-050] Member.md', target_revision='member-revision',
                        cause='manual member guard')
            common_defect_state.confirm(self.connection, 'repo', self.base, self.base, [link])
            self.connection.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id IN ('G-047','G-050')")
            common_defect_state.record_merge(self.connection, 'repo', 'G-050', 'https://example/pr/1', self.head, self.merge)
        member = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-050'").fetchone()
        self.assertIsNone(member['verify_token'])
        proof = common_defect_state._verification(self.connection, member, self.head)
        self.assertEqual(proof['member_id'], 'G-050')
        self.assertEqual(proof['execution_plan_id'], 'G-047')
        # Both linked corrections must actually be merged and verified.
        self.observe_merge()
        self.assertTrue(common_defect_state.reassessment(self.connection, 'repo', 'G-049')['ready'])
        publication = self.connection.execute('SELECT * FROM manual_work_publications').fetchone()
        original = publication['evidence']
        for mutation in ('waiver', 'missing', 'foreign-token', 'wrong-request'):
            evidence = json.loads(original)
            if mutation == 'waiver':
                evidence[-1]['result'] = 'SKIPPED_BY_USER'
            elif mutation == 'missing':
                evidence.pop()
            elif mutation == 'foreign-token':
                evidence[-1]['input_token'] = 'foreign'
            else:
                evidence[-1]['request_id'] = 'foreign'
            with self.subTest(mutation=mutation), self.connection:
                self.connection.execute('UPDATE manual_work_publications SET evidence=?', (json.dumps(evidence),))
                self.assertIsNone(common_defect_state._verification(self.connection, member, self.head))
        with self.connection:
            self.connection.execute('UPDATE manual_work_publications SET evidence=?', (original,))
            self.connection.execute("UPDATE manual_work_results SET detail='VERDICT: PASS' WHERE plan_id='G-050' AND stage='review'")
        self.assertIsNone(common_defect_state._verification(self.connection, member, self.head))
        with self.connection:
            self.connection.execute("UPDATE manual_work_results SET detail=? WHERE plan_id='G-050' AND stage='review'", (review,))
            self.connection.execute("UPDATE verify_receipts SET status='failure'")
        self.assertIsNone(common_defect_state._verification(self.connection, member, self.head))

    def test_native_merge_and_own_full_verification_feed_assessment_without_resetting_tasks(self):
        before = [dict(row) for row in self.connection.execute("SELECT * FROM exec_plans WHERE plan_id IN ('G-048','G-049')")]
        self.observe_merge()
        self.assertTrue(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['ready'])
        self.assertTrue(self.evaluate()[0])
        self.assertTrue(self.evaluate()[0])
        self.assertEqual(self.effects.run_agent.call_count, 1)
        snapshot = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-048').snapshot
        self.assertTrue(snapshot['common_defect_assessment']['common_defect_input']['ready'])
        self.assertEqual(len(snapshot['common_defect_assessment_history']), 1)
        self.assertEqual([dict(row) for row in self.connection.execute("SELECT * FROM exec_plans WHERE plan_id IN ('G-048','G-049')")], before)

    def test_missing_review_wrong_head_and_failed_or_foreign_verify_do_not_prove_correction(self):
        original = dict(self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-047'").fetchone())
        for index, changes in enumerate((dict(review_token=None), dict(review_result='VERDICT: FAIL'),
                                        dict(verify_token=None), dict(pr_head_oid='f' * 40)), 1):
            with self.subTest(changes=changes):
                with self.connection:
                    fields = {**{key: original[key] for key in ('review_token', 'review_result', 'verify_token', 'pr_head_oid')}, **changes}
                    self.connection.execute('UPDATE exec_plans SET ' + ','.join(key + '=?' for key in fields) + " WHERE plan_id='G-047'", tuple(fields.values()))
                self.observe_merge(merge=f'{index:040x}')
                self.assertFalse(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['ready'])
        with self.connection:
            for key in ('review_token', 'review_result', 'verify_token', 'pr_head_oid'):
                self.connection.execute(f"UPDATE exec_plans SET {key}=? WHERE plan_id='G-047'", (original[key],))
            self.connection.execute("UPDATE verify_receipts SET status='failure'")
        self.observe_merge()
        self.assertFalse(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['ready'])
        with self.connection:
            self.connection.execute("UPDATE verify_receipts SET status='success',plan_id='G-049'")
        self.observe_merge()
        self.assertFalse(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['ready'])

    def test_remote_without_merge_or_unresolved_cause_does_not_resume(self):
        self.observe_merge()
        self.effects.git.side_effect = lambda root, *args, **kw: (SimpleNamespace(returncode=1, stdout='')
            if args[0] == 'merge-base' else self.git(root, *args, **kw))
        self.assertFalse(self.evaluate()[0])

        self.effects.run_agent.assert_not_called()
        self.effects.git.side_effect = self.git
        self.resolved = False
        self.assertFalse(self.evaluate()[0])

    def test_proven_verify_reuse_keeps_its_own_actual_run_origin(self):
        origin = verify_receipts.current_receipt(self.connection, verify_receipts.selected(self.connection, 'repo', 'G-047'))
        self.head = 'f' * 40
        with self.connection:
            verify_receipts.record(self.connection, verify_receipts.selected(self.connection, 'repo', 'G-047'),
                head=self.head, observation=InputObservation('code-input'), kind='reuse', reason='same code inputs', origin=origin)
            self.connection.execute("UPDATE exec_plans SET pr_head_oid=?,verify_token=?,review_token=? WHERE plan_id='G-047'",
                (self.head, self.definition.verify_token(self.base, self.head, 'full verify passed'),
                 self.definition.review_token(self.base, self.head)))
        self.observe_merge()
        evidence = common_defect_state.reassessment(self.connection, 'repo', 'G-048')
        self.assertTrue(evidence['ready'])
        proof = json.loads(evidence['corrections'][0]['merge']['verification'])
        self.assertEqual(proof['kind'], 'reuse')
        self.assertEqual(proof['origin_id'], origin['id'])

    def test_waived_or_unfinished_verify_never_supplies_native_success(self):
        for status, kind in (('pending', 'run'), ('waived', 'waiver')):
            with self.subTest(status=status):
                with self.connection:
                    self.connection.execute('UPDATE verify_receipts SET status=?,kind=?', (status, kind))
                self.observe_merge()
                self.assertFalse(common_defect_state.reassessment(self.connection, 'repo', 'G-048')['ready'])

    def test_existing_question_still_requires_an_explicit_upper_specification_quote(self):
        self.observe_merge()
        with self.connection:
            runtime_state.RuntimeStateRepository(self.connection).block_plan('repo', 'G-048',
                reason_code='user_decision_required', evidence='Original question', release_fingerprint='input', clear_projection=False)
        self.quotes = [dict(source='shared.py', quote='return fixed')]
        with self.assertRaisesRegex(RuntimeError, '명세 근거'):
            self.evaluate()
        self.quotes.append(dict(source='docs/PRODUCT.md', quote='Required contract.'))
        self.assertTrue(self.evaluate()[0])
        snapshot = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-048').snapshot
        self.assertEqual(snapshot['user_decision_assessment']['question'], 'Original question')
        self.assertEqual(self.connection.execute("SELECT blocked_reason FROM exec_plans WHERE plan_id='G-048'").fetchone()[0], 'user_decision_required')

    def test_cause_change_during_assessment_rejects_result_and_keeps_original_audit(self):
        self.observe_merge()
        def changed(*args, **kwargs):
            result = self.assess(*args, **kwargs)
            with self.connection:
                self.connection.execute("UPDATE exec_plans SET worker_result='new cause' WHERE plan_id='G-048'")
            return result
        self.effects.run_agent.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, '변경'):
            self.evaluate()
        self.assertIsNone(execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-048').snapshot)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_defect_links').fetchone()[0], 2)

    def test_removed_cause_code_uses_observed_absence_and_current_upper_specification(self):
        self.observe_merge()
        def removed(root, *args, **kwargs):
            if args == ('show', self.remote + ':shared.py') or args[0] == 'ls-tree':
                return SimpleNamespace(returncode=1 if args[0] == 'show' else 0, stdout='')
            return self.git(root, *args, **kwargs)
        self.effects.git.side_effect = removed
        self.quotes = [dict(source='docs/PRODUCT.md', quote='Required contract.')]
        self.assertTrue(self.evaluate()[0])
        snapshot = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get('repo', 'G-048').snapshot
        self.assertFalse(snapshot['common_defect_assessment']['source_presence']['shared.py'])

    def test_code_whitespace_changes_do_not_reuse_a_semantic_document_assessment(self):
        self.observe_merge()
        self.assertTrue(self.evaluate()[0])
        original_git = self.effects.git.side_effect
        def indented(root, *args, **kwargs):
            if args == ('show', self.remote + ':shared.py'):
                return SimpleNamespace(returncode=0, stdout='    return fixed')
            return original_git(root, *args, **kwargs)
        self.effects.git.side_effect = indented
        self.assertTrue(self.evaluate()[0])
        self.assertEqual(self.effects.run_agent.call_count, 2)

    def test_empty_cause_file_is_not_required_to_supply_an_impossible_nonempty_quote(self):
        self.observe_merge()
        def emptied(root, *args, **kwargs):
            if args == ('show', self.remote + ':shared.py'):
                return SimpleNamespace(returncode=0, stdout='\n')
            return self.git(root, *args, **kwargs)
        self.effects.git.side_effect = emptied
        self.quotes = [dict(source='docs/PRODUCT.md', quote='Required contract.')]
        self.assertTrue(self.evaluate()[0])

    def test_rejected_review_protocol_report_matches_only_the_latest_original_result(self):
        self.feedback.record_result('repo', 'G-048', 'definition', 'review', 'protocol_failure', self.raw)
        with self.connection:
            self.connection.execute("UPDATE exec_plans SET worker_result=NULL,blocked_reason='review_protocol_exhausted' WHERE plan_id='G-048'")
            link = json.loads(self.connection.execute('SELECT evidence FROM common_defect_links LIMIT 1').fetchone()[0])
            link['reports'] = [3]
            common_defect_state.confirm(self.connection, 'repo', self.base, self.base, [link])
        self.observe_merge()
        current = common_defect_state.reassessment(self.connection, 'repo', 'G-048')
        self.assertTrue(current['ready'])
        self.assertEqual([item['result_id'] for item in current['corrections']], [3])
        self.feedback.record_result('repo', 'G-048', 'definition', 'review', 'protocol_failure', 'different original failure')
        self.assertIsNone(common_defect_state.reassessment(self.connection, 'repo', 'G-048'))
