from tests.support.recovery import BoundedRecoveryFixture
from tests.support.recovery_boundary import RecoveryBoundary
import tempfile
import unittest
import loop_manager
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from tests.support.notification import NotificationFixture
from loop_manager_internal.agents.agent_invocation import AgentResult
from loop_manager_internal.execution.execution_review_handler import ReviewHandler
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from tests.support.worker import dummy_worker_sessions
from tests.support.git import isolated_git_test


class ReviewProtocolNotificationTest(NotificationFixture, unittest.TestCase):
    def test_corrected_protocol_failure_retries_without_stage_notice(self):
        connection = self.connection()
        repo = '/projects/alpha'
        context = SimpleNamespace(plan=SimpleNamespace(plan_id='G-018', definition_revision_sha='revision'),
                                  worktree=Path(repo), branch='branch')
        snapshot = SimpleNamespace(plan={'id': 'G-018', 'title': 'Reliable notices'}, level=1,
                                   catalog_plan={'path': 'plan.md', 'current_revision_sha': 'revision'},
                                   _definition_snapshot=mock.Mock())
        snapshot._definition_snapshot.review_token.return_value = 'review-token'
        ports = mock.Mock()
        ports.identity = SimpleNamespace(repo=repo, repo_path=Path(repo), base='main')
        ports.contract = loop_manager
        ports.feedback = None
        ports.checkpoint = None
        ports.state = ExecutionStatePort(
            connection, RuntimeStateRepository(connection), repo,
        )
        ports.state.check_context = mock.Mock()
        ports.snapshot.return_value = snapshot
        ports._row.side_effect = lambda: connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()
        ports._document_draft.return_value = None
        ports.git.return_value = SimpleNamespace(stdout='candidate')
        ports._post_effect_guard.return_value = context
        ports.parse_reviewer_result.side_effect = [loop_manager.ReviewerProtocolError('missing ISSUE_ID'), ('PASS', ())]
        sent = mock.Mock()
        calls = []
        def reviewer(*args, **kwargs):
            calls.append('reviewer')
            if len(calls) == 2:
                sent.assert_not_called()
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM notification_outbox WHERE event_type = 'stage_failed'"
                ).fetchone())
            return AgentResult(0, 'invalid' if len(calls) == 1 else 'VERDICT: PASS')
        ports.run_agent_with_retries.side_effect = reviewer
        handler = ReviewHandler(ports)
        with mock.patch.object(handler, '_review_entry_evidence', return_value=(
                SimpleNamespace(test_evidence='PASS'), 'Manager verify PASS receipt fixture')), \
             mock.patch('loop_manager_internal.storage.review_correction_state.load', return_value={}), \
             mock.patch('loop_manager_internal.notifications.delivery.send_telegram', sent), \
             notifications.immediate_delivery(connection, 'token', 'chat', repo):
            handler.review(context)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(connection.execute('SELECT last_error FROM projects').fetchone()[0])
        sent.assert_not_called()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type = 'stage_failed'").fetchone()[0], 0)


@isolated_git_test
class ReviewProtocolRecoveryTest(BoundedRecoveryFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_format_normalization_needs_no_retry_or_failure_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory,
                (" **VERDICT** = PASS\nVerified source and scope.",),
                publish=True,
            )
        self.assertEqual(data["roles"], ["worker", "reviewer"])
        self.assertEqual(data["verify_calls"], 1)
        self.assertEqual(data["transitions"], [])
        self.assertEqual(data["plan"]["status"], "PR_OPEN")
        self.assertEqual(data["plan"]["effective_worker_level"], 1)

    @dummy_worker_sessions
    def test_review_protocol_correction_preserves_pass_and_fail_transitions(self):
        invalid = "blocking issue without an identifier\nVERDICT: FAIL"
        valid_fail = (
            "ISSUE_ID: src/app.py::value::incorrect\n"
            "value remains incorrect\nVERDICT: FAIL"
        )
        # The first variant keeps the actual Worker -> verify -> Review flow.
        # The second restores its Review entry, then exercises its own failure,
        # corrective Worker and verify. No tested correction is prepared ahead.
        with RecoveryBoundary() as boundary:
            for corrected_to_fail in (False, True):
                with self.subTest(corrected_to_fail=corrected_to_fail):
                    messages = ((invalid, valid_fail, "VERDICT: PASS")
                                if corrected_to_fail else (invalid, "VERDICT: PASS"))
                    data = self._run_review_sequence(
                        boundary.directory, messages, publish=True, boundary=boundary,
                    )
                    self.assertEqual(data["result"].kind, loop_manager.ProjectRunKind.WAITING)
                    self.assertEqual(data["result"].exit_code, 0)
                    self.assertEqual(
                        data["roles"],
                        ["reviewer", "reviewer", "worker", "reviewer"]
                        if corrected_to_fail else ["worker", "reviewer", "reviewer"],
                    )
                    self.assertEqual(
                        data["reviewer_sessions"],
                        [None, "reviewer-session", "reviewer-session"]
                        if corrected_to_fail else [None, "reviewer-session"],
                    )
                    self.assertEqual(data["verify_calls"], 1)
                    self.assertEqual(
                        [(stage, occurrences, blocked)
                         for stage, _fingerprint, occurrences, blocked in data["transitions"]],
                        [("review", 1, False)] if corrected_to_fail else [],
                    )
                    self.assertIsNone(data["project"]["phase"])
                    self.assertEqual(data["plan"]["status"], "PR_OPEN")
                    self.assertIsNone(data["plan"]["last_error"])
                    self.assertIn(
                        "Reviewer FAIL 결과에 유효한 ISSUE_ID가 없습니다",
                        data["reviewer_prompts"][1],
                    )
                    self.assertIn(invalid, data["reviewer_prompts"][1])
                    if not corrected_to_fail:
                        # Reject foreign identity/inputs before touching the
                        # owned state. The next variant must still restore it.
                        with mock.patch.object(boundary, 'restore', wraps=boundary.restore) as restore:
                            with self.assertRaisesRegex(RuntimeError, '준비 경로'):
                                self._run_review_sequence(
                                    str(boundary.archive), (invalid,), publish=True, boundary=boundary)
                            with self.assertRaisesRegex(RuntimeError, '준비 입력'):
                                self._run_review_sequence(
                                    boundary.directory, (invalid,), publish=False, boundary=boundary)
                            restore.assert_not_called()
                    if corrected_to_fail:
                        self.assertNotIn(
                            "직전 Reviewer 결과가 아래 프로토콜 검증에 실패",
                            data["reviewer_prompts"][2],
                        )
