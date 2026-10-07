"""Role boundaries accept formatting while preserving required decisions."""
import json
import unittest

import loop_manager
from loop_manager_internal.execution.worker_outcome import parse_worker_outcome, WorkerOutcomeError
from loop_manager_internal.maintenance.maintenance_orchestration import (
    parse_maintenance_planner_decision, MaintenanceOrchestrationError,
)
from loop_manager_internal.planning.planner_check import parse_planner_check_result
from loop_manager_internal.planning.planning_catalog import PlanningResultInvalid
from loop_manager_internal.storage.maintenance_state import MaintenancePhase


class ResultNormalizationTest(unittest.TestCase):
    def test_worker_marker_order_spacing_and_multiline_disposition(self):
        message = ('  **WORKER_OUTCOME** =\n```json\n'
                   '{"kind": "ready_for_validation"}\n```\n'
                   '- TARGETED_TESTS： parser: PASS\n\n'
                   '**PR_SUMMARY:** Preserves actual parser evidence.\n\n'
                   ' COMMIT_TITLE = Normalize role result declarations')
        self.assertEqual(parse_worker_outcome(message, plan_path='plan', sources=(), documents={}).kind,
                         'ready_for_validation')
        publication = loop_manager.parse_worker_result(message, {'id': 'G-180', 'title': 'Goal'})
        self.assertEqual(publication.commit_title, 'Normalize role result declarations')
        self.assertEqual(publication.test_evidence, 'parser: PASS')
        moved = ('COMMIT_TITLE: Normalize role result declarations\n'
                 'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
                 'PR_SUMMARY: Preserves actual parser evidence.\nTARGETED_TESTS: parser: PASS')
        self.assertEqual(loop_manager.parse_worker_result(moved, {'id': 'G-180', 'title': 'Goal'}), publication)
        for bad in (message + '\nCOMMIT_TITLE: Another title',
                    message.replace('parser: PASS', 'parser: FAIL'),
                    message.replace('parser: PASS', 'none')):
            with self.subTest(bad=bad), self.assertRaises(loop_manager.WorkerResultError):
                loop_manager.parse_worker_result(bad, {'id': 'G-180', 'title': 'Goal'})
        with self.assertRaises(WorkerOutcomeError):
            parse_worker_outcome(message + '\nWORKER_OUTCOME: {"kind":"needs_user_decision"}',
                                 plan_path='plan', sources=(), documents={})

    def test_plain_marker_preserves_bold_evidence_body(self):
        message = ('COMMIT_TITLE: Preserve role result evidence\n'
                   'PR_SUMMARY:**Source citations** remain unchanged.\n'
                   'TARGETED_TESTS: parser: PASS')
        publication = loop_manager.parse_worker_result(message, {'id': 'G-180', 'title': 'Goal'})
        self.assertEqual(publication.summary, '**Source citations** remain unchanged.')

    def test_specification_blocked_accepts_multiline_without_filling_judgment(self):
        value = {'kind': 'missing', 'locations': [{'path': 'docs/PRODUCT.md', 'section': 'Policy'}],
                 'problem': 'The required behavior is unspecified',
                 'implementation_choice': 'Choose the source retention rule',
                 'question': 'Which retention rule should apply?'}
        result = '**VERDICT** = SPEC_BLOCKED\n**SPEC_DEADLOCK** =\n' + json.dumps(value, indent=2)
        verdict, details = loop_manager.parse_reviewer_result(result)
        self.assertEqual(verdict, 'SPEC_BLOCKED')
        self.assertEqual(details.question, value['question'])
        with self.assertRaises(loop_manager.ReviewerProtocolError):
            loop_manager.parse_reviewer_result(result.replace('"kind": "missing"', '"kind": "missing", "kind": "conflict"'))
        value.pop('question')
        with self.assertRaises(loop_manager.ReviewerProtocolError):
            loop_manager.parse_reviewer_result('SPEC_DEADLOCK: ' + json.dumps(value) + '\nVERDICT: SPEC_BLOCKED')

    def test_reviewer_verdict_placement_and_issue_separator_spacing(self):
        output = '**VERDICT** = FAIL\n- ISSUE_ID： src/app.py :: value :: incorrect\nActual blocking issue.'
        self.assertEqual(loop_manager.parse_reviewer_result(output),
                         ('FAIL', ('ISSUE_ID:src/app.py::value::incorrect',)))
        self.assertEqual(loop_manager._review_issue_paths(output), ['src/app.py'])
        self.assertEqual(loop_manager.parse_reviewer_result(' VERDICT： PASS\nReview explanation.'), ('PASS', ()))
        for bad in ('VERDICT: PASS\nVERDICT: FAIL', 'VERDICT: FAIL\nNo issue ID.',
                    'VERDICT: PASS\nISSUE_ID: src/app.py::value::incorrect'):
            with self.subTest(bad=bad), self.assertRaises(loop_manager.ReviewerProtocolError):
                loop_manager.parse_reviewer_result(bad)

    def test_planner_multiline_results_keep_evidence_and_scope_validation(self):
        value = {'outcome': 'needs_revision', 'evidence': 'src/app.py diff',
                 'reason': 'Source contract changed', 'plan_ids': [' G-180 ']}
        output = '**PLANNER_CHECK_RESULT**：\n' + json.dumps(value, indent=2)
        self.assertEqual(parse_planner_check_result(output, frozenset({'G-180'})).plan_ids, ('G-180',))
        for change in ({'plan_ids': ['G-181']}, {'evidence': ''}, {'reason': ''}):
            with self.subTest(change=change), self.assertRaises(PlanningResultInvalid):
                parse_planner_check_result('PLANNER_CHECK_RESULT =\n' + json.dumps(value | change), frozenset({'G-180'}))
        decision = parse_maintenance_planner_decision(
            '- MAINTENANCE_RESULT =\n' + json.dumps({'outcome': 'refactoring', 'plan_ids': [' G-180 ']}),
            MaintenancePhase.ASSESSMENT)
        self.assertEqual(decision.plan_ids, ('G-180',))
        combined = output + ' \t\nMAINTENANCE_RESULT: {"outcome":"no_change"}'
        self.assertEqual(parse_planner_check_result(combined, frozenset({'G-180'})).plan_ids, ('G-180',))
        self.assertEqual(parse_maintenance_planner_decision(combined, MaintenancePhase.ASSESSMENT).outcome.value,
                         'no_change')
        for bad in ('{"outcome":"no_change","outcome":"refactoring"}',
                    '{"outcome":"refactoring"}', '{"outcome":"no_change","reason":NaN}'):
            with self.subTest(bad=bad), self.assertRaises(MaintenanceOrchestrationError):
                parse_maintenance_planner_decision('MAINTENANCE_RESULT: ' + bad, MaintenancePhase.ASSESSMENT)
