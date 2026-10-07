"""Narrow tests for the Worker disposition contract."""
from contextlib import closing
from tests.support.worker import decision

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.notifications import notifications
from tests.support.database import runtime_connection
from loop_manager_internal.execution.worker_outcome import (
    WorkerOutcomeError, parse_worker_outcome, worker_outcome_instruction,
)


PLAN = "docs/plans/[G-096] Decision.md"
DOCUMENTS = {
    PLAN: "# Decision\n## Decision Log\n",
    "docs/PRODUCT.md": "# Product\n## 사용자 승인 및 완료\n",
    "docs/ARCHITECTURE.md": "# Architecture\n## Architecture Change Policy\n",
}


def parse(message):
    return parse_worker_outcome(
        message, plan_path=PLAN, sources=("docs/PRODUCT.md",),
        documents=DOCUMENTS,
    )


class WorkerOutcomeTests(unittest.TestCase):
    def test_ready_and_legacy_completion_remain_distinct_from_decision_words(self):
        with self.assertRaisesRegex(WorkerOutcomeError, 'WORKER_OUTCOME'):
            parse('승인 대기 BLOCKED 중단')
        self.assertEqual(parse_worker_outcome(
            'COMMIT_TITLE: Saved completion\nPR_SUMMARY: Saved summary\n'
            'TARGETED_TESTS:\ncase: PASS',
            plan_path=PLAN, sources=('docs/PRODUCT.md',),
            documents=DOCUMENTS, allow_legacy_ready=True,
        ).kind, 'ready_for_validation')
        self.assertEqual(parse('WORKER_OUTCOME: {"kind":"ready_for_validation"}')
                         .kind, 'ready_for_validation')
        self.assertIn('needs_user_decision', worker_outcome_instruction())

    def test_decision_requires_only_structured_evidence(self):
        outcome = parse(decision())
        self.assertEqual(outcome.kind, 'needs_user_decision')
        self.assertEqual(outcome.question, '이 변경을 승인합니까?')
        self.assertEqual(outcome.locations[0].section,
                         'Architecture Change Policy')

    def test_explicit_ready_uses_existing_publication_contract(self):
        message = (
            'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
            'COMMIT_TITLE: Preserve worker decision evidence\n'
            'PR_SUMMARY: Records the decision before validation.\n'
            'TARGETED_TESTS:\nparser case: PASS'
        )
        self.assertEqual(parse(message).kind, 'ready_for_validation')
        publication = loop_manager.parse_worker_result(
            message, {'id': 'G-096', 'title': 'Structured Worker Decision Blocking'},
        )
        self.assertEqual(publication.commit_title,
                         'Preserve worker decision evidence')

    def test_invalid_or_conflicting_declarations_are_rejected(self):
        invalid = [
            'WORKER_OUTCOME: {bad}',
            'WORKER_OUTCOME: {"kind":"unknown"}',
            'WORKER_OUTCOME: {"kind":"ready_for_validation","question":"x"}',
            'WORKER_OUTCOME: {"kind":"ready_for_validation","kind":"needs_user_decision"}',
            decision() + '\n' + decision(),
            decision(question=' '),
            decision() + '\nCOMMIT_TITLE: premature',
            decision() + '\n  TARGETED_TESTS: premature',
            decision(locations=[{"path": '../secret', 'section': 'x'}]),
            decision(locations=[{"path": 'docs/ARCHITECTURE.md',
                                 'section': 'Missing heading'}]),
            decision(locations=[{"path": 'docs/ARCHITECTURE.md'}]),
        ]
        for message in invalid:
            with self.subTest(message=message), self.assertRaises(WorkerOutcomeError):
                parse(message)

    def test_user_decision_release_ignores_unrelated_review_changes(self):
        source = runtime_connection()
        self.addCleanup(source.close)
        source.execute(
            "UPDATE exec_plans SET status = 'PLAN_BLOCKED', "
            "blocked_reason = 'user_decision_required', "
            "blocked_fingerprint = 'remote-input', "
            "blocked_worktree_fingerprint = 'worker-result-input', "
            "worker_session_id = 'saved-session', "
            "review_failure_history = '{\"issue\":3}', "
            "worktree = '/state/G-047', workspace_owned = 0 "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        )
        source.commit()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / 'state.sqlite'
        with closing(sqlite3.connect(database)) as saved, saved:
            source.backup(saved)

        for _ in range(3):
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.row_factory = sqlite3.Row
                with (
                    mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                                      return_value='remote-input'),
                    mock.patch.object(loop_manager, '_revision_review_issue_fingerprint',
                                      return_value='changed-review-context') as review,
                    mock.patch.object(loop_manager, '_requeue_blocked_execplan') as requeue,
                ):
                    self.assertFalse(loop_manager.resume_blocked_execplan_if_inputs_changed(
                        connection, Path('/repo'), 'repo', 'main', Path('/state'),
                    ))
                    requeue.assert_not_called()
                    review.assert_not_called()
                row = connection.execute(
                    "SELECT status, worker_session_id, review_failure_history "
                    "FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                self.assertEqual(tuple(row),
                                 ('PLAN_BLOCKED', 'saved-session', '{"issue":3}'))

        with closing(sqlite3.connect(database)) as connection, connection:
            connection.row_factory = sqlite3.Row
            with (
                mock.patch.object(loop_manager, '_revision_execplan_input_fingerprint',
                                  return_value='changed-input'),
                mock.patch.object(loop_manager, '_revision_review_issue_fingerprint',
                                  return_value='changed-review-context'),
                mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
                mock.patch.object(loop_manager, 'git',
                                  return_value=mock.Mock(returncode=1)),
                mock.patch.object(loop_manager, '_blocked_execplan_revision_metadata_error',
                                  return_value=None),
                mock.patch.object(loop_manager, '_requeue_blocked_execplan') as requeue,
            ):
                self.assertTrue(loop_manager.resume_blocked_execplan_if_inputs_changed(
                    connection, Path('/repo'), 'repo', 'main', Path('/state'),
                ))
                requeue.assert_called_once_with(connection, 'repo', 'G-047')

    def test_block_notice_renders_structured_question_and_location(self):
        payload = {
            'project': 'sample', 'scope': 'plan', 'execplan_id': 'G-096',
            'reason_code': 'user_decision_required',
            'cause': 'Worker 사용자 결정 대기',
            'paths': ['docs/ARCHITECTURE.md#Architecture Change Policy'],
            'worker_decision': {'question': '설계 변경을 허용합니까?'},
            'checkpoint': {'result': 'local_only', 'reason': 'rebase unresolved'},
        }
        message = notifications._message('blocked', payload)
        self.assertIn('Question: 설계 변경을 허용합니까?', message)
        self.assertIn('docs/ARCHITECTURE.md#Architecture Change Policy', message)
        self.assertIn('local only (rebase unresolved)', message)

    def test_resumed_worker_scope_rebinds_remote_input_without_losing_snapshot(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE projects SET agent_role = 'worker', agent_snapshot = 'saved', "
            "agent_input_fingerprint = 'legacy-local' WHERE repo = 'repo'"
        )
        connection.commit()
        with mock.patch.object(loop_manager.GitSnapshot, 'deserialize',
                               return_value='saved-snapshot'):
            result = loop_manager.begin_agent_scope(
                connection, 'repo', Path('/state/G-047'), 'worker',
                input_fingerprint='remote-input',
            )
        self.assertEqual(result, 'saved-snapshot')
        row = connection.execute(
            "SELECT agent_snapshot, agent_input_fingerprint FROM projects "
            "WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(tuple(row), ('saved', 'remote-input'))


if __name__ == '__main__':
    unittest.main()
