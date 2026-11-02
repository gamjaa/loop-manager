import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote
from unittest import mock

import loop_manager
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.config.managed_config import ManagedConfigError, load_managed_config
from loop_manager_internal.notifications import delivery
from loop_manager_internal.notifications import notifications
from loop_manager_internal.notifications import transport
from loop_manager_internal.notifications import work_exhaustion
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.recovery_state import RecoveryRepository

from tests.support.notification import NotificationFixture


from tests.support.scoped_recovery import ScopedGateFixture


class WorkspaceOwnershipDeliveryTest(ScopedGateFixture, unittest.TestCase):
    def test_scope_transition_cancels_only_legacy_project_pieces_and_keeps_question_audit(self):
        from tests.support.notification import NotificationFixture
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        self.plan('G-001', 'IMPLEMENTING', branch='loop-manager/G-001')
        self.plan('G-002')
        worktree = Path('/state/G-001').resolve()
        head = 'a' * 40
        snapshot = loop_manager.GitSnapshot(head, 'loop-manager/G-001', {}, {},
            {'refs/heads/loop-manager/G-001': (head, '')},
            {self.repo: ('HEAD=base', 'branch=refs/heads/main'),
             str(worktree): ('HEAD=' + head, 'branch=refs/heads/loop-manager/G-001')}, {}, {})
        with self.connection:
            notifications.configure_session(self.connection, enabled=True)
            self.connection.execute("UPDATE exec_plans SET worktree=?,workspace_owned=2,agent_snapshot=? WHERE plan_id='G-001'",
                                    (str(worktree), snapshot.serialize()))
            self.connection.execute("UPDATE projects SET phase='WORKING',plan_path='docs/plans/[G-001] Work.md'")
            NotificationFixture.decision_block(self.connection, repo=self.repo, plan_id='G-002')
        question_sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(self.connection, 'token', 'chat', adapter=question_sender), 1)
        question = dict(self.connection.execute('SELECT * FROM notification_outbox').fetchone())
        with self.connection:
            scoped_blocking.record_project_block(self.connection, self.repo,
                scoped_blocking.BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
                'missing creation evidence', 'original', plan_id='G-001', previous_phase='WORKING')
            NotificationFixture.legacy_project_notice(self.connection, repo=self.repo)
            self.connection.execute("UPDATE notification_outbox SET sent_at=NULL,attempts=2,last_error='old send failure',rendered_text='preserved fragments' WHERE event_key<>?", (question['event_key'],))
        legacy = dict(self.connection.execute('SELECT * FROM notification_outbox WHERE event_key<>?', (question['event_key'],)).fetchone())
        live = {'exists': True, 'registration': {'head': head, 'branch': 'refs/heads/loop-manager/G-001'}, 'index': 'tree'}
        with ExitStack() as stack:
            for patch in (
                mock.patch.object(Path, 'is_dir', return_value=True),
                mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'),
                mock.patch.object(loop_manager, 'validate_managed_worktree'),
                mock.patch.object(loop_manager, 'git', return_value=mock.Mock(returncode=0, stdout='')),
                mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=snapshot),
                mock.patch.object(loop_manager, '_managed_worktree_transition_state', return_value=live),
                mock.patch('loop_manager_internal.git.manager_git_journal.recover_active'),
                mock.patch('loop_manager_internal.git.manager_git_journal.verified_managed_transitions', return_value=[]),
            ):
                stack.enter_context(patch)
            self.assertTrue(loop_manager.resume_project_block_if_safe(self.connection, Path(self.repo), Path('/state'), self.repo))
            self.assertFalse(loop_manager.resume_plan_blocks(self.connection, Path(self.repo), Path('/state'), self.repo, 'main', plan_id='G-001'))
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(self.connection, 'token', 'chat', adapter=sender), 0)
        sender.assert_not_called()
        after = dict(self.connection.execute('SELECT * FROM notification_outbox WHERE event_key=?', (legacy['event_key'],)).fetchone())
        self.assertIsNotNone(after['canceled_at'])
        for key in ('attempts', 'confirmed_pieces', 'last_error', 'rendered_text'):
            self.assertEqual(after[key], legacy[key], key)
        self.assertEqual(dict(self.connection.execute('SELECT * FROM notification_outbox WHERE event_key=?', (question['event_key'],)).fetchone()), question)
        self.assertEqual(scoped_blocking.plan_block(self.connection, self.repo, 'G-002')['reason_code'], 'user_decision_required')
        self.assertTrue(ExecutionCheckpointRepository(self.connection).get(self.repo, 'G-001').snapshot['workspace_ownership_hold'])


class PrCreationPublicationTest(unittest.TestCase):
    def prepare(self, *, proof=False):
        from tests.support.execution import execution_fixture
        case = execution_fixture(self)
        case.review_context()
        from dataclasses import replace
        definition = replace(case.handler._definition_snapshot, complexity='low', risk='low')
        case.handler._definition_snapshot = definition
        notifications.configure_session(case.connection, enabled=True)
        case.state.update_project(case.repo, phase='PUBLISHING',
            definition_snapshot=definition.serialize(), review_result='PASS',
            review_token=definition.review_token('base', 'base'),
            publication_token=definition.publication_token('base', 'base'))
        if proof:
            notifications.record_pr_review_candidate(case.connection, case.repo, 'G-047',
                publication_token=definition.publication_token('base', 'base'),
                review_input_identity='initial')
            case.connection.commit()
        return case, definition

    def publish(self, case):
        with notifications.immediate_delivery(case.connection, 'token', 'chat', case.repo), \
                mock.patch.object(delivery, 'send_telegram') as sent:
            result = case.handler._publish_new_pull_request(case.context(), case.state.row())
        self.assertEqual(result.kind.value, 'success')
        self.assertTrue(case.connection.execute("SELECT 1 FROM notification_outbox WHERE event_type='pr_created'").fetchone(),
            (dict(case.connection.execute('SELECT * FROM exec_plans').fetchone()),
             [dict(row) for row in case.connection.execute('SELECT * FROM pr_creation_publications')]))
        return sent

    def test_creation_without_diff_proof_runs_publication_outbox_and_claim(self):
        case, _ = self.prepare()
        sent = self.publish(case)
        sent.assert_called_once()
        self.assertEqual(case.connection.execute('SELECT COUNT(*) FROM pr_review_candidates').fetchone()[0], 0)
        original = dict(case.connection.execute("SELECT * FROM notification_outbox WHERE event_type='pr_created'").fetchone())
        self.assertIsNotNone(original['sent_at'])
        self.assertEqual(json.loads(original['payload'])['notice_kind'], 'creation')
        # 재시작 연결에서도 생성 이력과 완료된 Outbox를 그대로 재사용한다.
        restarted = sqlite3.connect(':memory:')
        self.addCleanup(restarted.close)
        case.connection.backup(restarted)
        restarted.row_factory = sqlite3.Row
        notifications.configure_session(restarted, enabled=True)
        with restarted:
            notifications.enqueue_current_project_events(restarted, case.repo)
            notifications.enqueue_current_project_events(restarted, case.repo)
        self.assertEqual(restarted.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type='pr_created'").fetchone()[0], 1)
        self.assertEqual(notifications.dispatch_new_pr_notifications(restarted, 'token', 'chat', case.repo, adapter=sent), 0)
        sent.assert_called_once()

    def test_creation_retry_and_terminal_cancel_preserve_audit_and_cursor(self):
        for terminal in ('MERGED', 'PLAN_BLOCKED', None):
            with self.subTest(terminal=terminal):
                case, _ = self.prepare()
                with notifications.immediate_delivery(case.connection, 'token', 'chat', case.repo), \
                        mock.patch.object(delivery, 'send_telegram', side_effect=RuntimeError('offline')):
                    case.handler._publish_new_pull_request(case.context(), case.state.row())
                original = dict(case.connection.execute("SELECT * FROM notification_outbox WHERE event_type='pr_created'").fetchone())
                self.assertEqual(original['attempts'], 1)
                with case.connection:
                    case.connection.execute("UPDATE notification_outbox SET rendered_text='confirmed\npending', confirmed_pieces=1")
                    if terminal:
                        case.connection.execute('UPDATE exec_plans SET status=?', (terminal,))
                with notifications.immediate_delivery(case.connection, 'token', 'chat', case.repo), \
                        mock.patch.object(delivery, '_pieces', return_value=['confirmed', 'pending']), \
                        mock.patch.object(delivery, 'send_telegram') as sent:
                    notifications.flush_observed_pr_events(case.connection, case.repo)
                row = case.connection.execute('SELECT * FROM notification_outbox').fetchone()
                self.assertEqual(row['payload'], original['payload'])
                if terminal:
                    sent.assert_not_called()
                    self.assertIsNotNone(row['canceled_at'])
                    self.assertEqual((row['attempts'], row['last_error'], row['confirmed_pieces']),
                                     (1, original['last_error'], 1))
                else:
                    sent.assert_called_once()
                    self.assertIsNotNone(row['sent_at'])
                    self.assertEqual(row['confirmed_pieces'], 2)

    def test_existing_publication_distinguishes_new_candidate_and_rebase(self):
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
        case, definition = self.prepare(proof=True)
        sent = self.publish(case)
        repository = RuntimeStateRepository(case.connection)
        for head, identity, expected_count in (
                ('rebased', 'initial', 1), ('changed', 'new-input', 2), ('rebased-again', 'new-input', 2)):
            snapshot = repository.get(case.repo, 'G-047')
            repository.transition(case.repo, 'G-047', plan_values={
                'status': 'PUBLISHING', 'validated_base_sha': 'base',
                'review_token': definition.review_token('base', head),
                'publication_token': definition.publication_token('base', head)},
                project_values={**snapshot.project_values(), 'phase': 'PUBLISHING',
                    'review_token': definition.review_token('base', head),
                    'publication_token': definition.publication_token('base', head)}, enqueue=False)
            notifications.record_pr_review_candidate(case.connection, case.repo, 'G-047',
                publication_token=definition.publication_token('base', head), review_input_identity=identity)
            case.connection.commit()
            repository.open_pull_request(case.repo, pr_head_oid=head)
            notifications.dispatch_new_pr_notifications(case.connection, 'token', 'chat', case.repo, adapter=sent)
            self.assertEqual(sent.call_count, expected_count)

    def test_existing_pr_without_proof_is_never_promoted_to_creation(self):
        case, _ = self.prepare()
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
        RuntimeStateRepository(case.connection).open_pull_request(case.repo,
            pr_url='https://example/pr/existing', pr_head_oid='base')
        self.assertEqual(case.connection.execute('SELECT COUNT(*) FROM pr_creation_publications').fetchone()[0], 0)
        self.assertEqual(case.connection.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)

    def test_recovered_creation_requires_owned_creation_intent_and_sends_once(self):
        from dataclasses import fields
        from loop_manager_internal.recovery.pull_request_recovery import (
            ReconcileExistingPullRequestEffects, reconcile_existing_pull_request)
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        for operation, saved_url, head, expected in (
                ('pr_create', None, 'base', 1),
                ('pr_create', 'https://example/pr/1', 'base', 1),
                ('push', None, 'base', 0),
                ('pr_create', None, 'other-head', 0)):
            with self.subTest(operation=operation, saved_url=saved_url, head=head):
                case, _ = self.prepare()
                if saved_url:
                    case.state.update_project(case.repo, pr_url=saved_url)
                checkpoints = ExecutionCheckpointRepository(case.connection)
                checkpoints.record(case.repo, 'G-047', expected_generation=0,
                    operation=operation, expected_head='base', target_base_oid='base',
                    observed_remote_oid='base', snapshot={})
                effects = ReconcileExistingPullRequestEffects(**{
                    field.name: mock.Mock(return_value=None) for field in fields(ReconcileExistingPullRequestEffects)})
                effects._plan_id_from_path.return_value = 'G-047'
                effects.project_execplan.side_effect = lambda *_: case.connection.execute('SELECT * FROM exec_plans').fetchone()
                effects.update_project.side_effect = lambda _conn, repo, **values: loop_manager.update_project(_conn, repo, **values)
                pr = {'url': 'https://example/pr/1', 'state': 'OPEN', 'headRefOid': head,
                      'headRefName': 'loop-manager/G-047', 'baseRefName': 'main'}
                with notifications.immediate_delivery(case.connection, 'token', 'chat', case.repo), \
                        mock.patch.object(delivery, 'send_telegram') as sent:
                    reconcile_existing_pull_request(effects, case.connection, Path('/repo'), Path('/state'),
                        case.repo, case.state.row(), pr)
                    with case.connection:
                        notifications.enqueue_current_project_events(case.connection, case.repo)
                    notifications.flush_observed_pr_events(case.connection, case.repo)
                self.assertEqual(sent.call_count, expected)
                self.assertEqual(case.connection.execute('SELECT COUNT(*) FROM pr_creation_publications').fetchone()[0], expected)


class TelegramTransportTest(unittest.TestCase):
    def test_send_encodes_payload_and_checks_response(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(transport, "urlopen", return_value=response) as open_url:
            transport.send_telegram("token:part", "chat", "한글 \U0001f642")
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bottoken:part/sendMessage")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "chat_id": "chat", "text": "한글 \U0001f642", "disable_notification": False,
        })
        self.assertEqual(open_url.call_args.kwargs["timeout"], 15)

        response.__enter__.return_value.status = 503
        with mock.patch.object(transport, "urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "non-success status") as raised:
                transport.send_telegram("secret-token", "secret-chat", "message")
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertNotIn("secret-chat", str(raised.exception))


class NotificationDeliveryTest(NotificationFixture, unittest.TestCase):
    def test_legacy_progress_pieces_are_canceled_with_original_audit_after_restart(self):
        kinds = (
            'plans_published', 'planning_result', 'work_started', 'resumed',
            'stage_failed', 'maintenance_started', 'maintenance_completed',
            'project_completed', 'work_waiting',
        )
        for kind in kinds:
            for cursor in (0, 1):
                with self.subTest(kind=kind, cursor=cursor), tempfile.TemporaryDirectory() as directory:
                    database = Path(directory) / 'state.db'
                    connection = self.connection(database=database)
                    try:
                        notifications._insert(connection, 'legacy-progress', '/projects/alpha', kind,
                                              {'project': 'alpha', 'silent': True, 'cause': 'original cause'})
                        text = 'frozen progress ' * 700
                        connection.execute(
                            'UPDATE notification_outbox SET rendered_text=?, confirmed_pieces=?, '
                            'attempts=3, last_error=?, delivery_started_at=CURRENT_TIMESTAMP',
                            (text, cursor, 'original delivery error'))
                        connection.commit()
                        before = dict(self.events(connection)[0])
                    finally:
                        connection.close()
                    connection = sqlite3.connect(database)
                    connection.row_factory = sqlite3.Row
                    try:
                        adapter = mock.Mock()
                        for _ in range(2):
                            self.assertEqual(notifications.dispatch_pending(
                                connection, 'token', 'chat', adapter=adapter), 0)
                        adapter.assert_not_called()
                        after = dict(self.events(connection)[0])
                        self.assertIsNotNone(after['canceled_at'])
                        self.assertIsNone(after['delivery_started_at'])
                        for field in before.keys() - {'canceled_at', 'delivery_started_at'}:
                            self.assertEqual(after[field], before[field], field)
                        plan = connection.execute('SELECT status FROM exec_plans').fetchone()
                        self.assertEqual(plan[0], 'READY')
                    finally:
                        connection.close()

    def test_immediate_progress_delivery_uses_same_cancellation_policy(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with notifications.immediate_delivery(connection, 'token', 'chat', repo):
            with connection:
                notifications._insert(connection, 'progress', repo, 'work_started',
                                      {'project': 'alpha', 'silent': True})
            with mock.patch.object(delivery, 'send_telegram') as adapter:
                self.assertEqual(notifications.flush_project_events(connection, repo), 0)
            adapter.assert_not_called()
        row = self.events(connection)[0]
        self.assertIsNotNone(row['canceled_at'])
        self.assertEqual(row['attempts'], 0)
        self.assertIsNone(row['rendered_text'])

    def test_stale_selection_cannot_claim_canceled_progress(self):
        connection = self.connection()
        with connection:
            notifications._insert(connection, 'progress', '/projects/alpha', 'work_waiting',
                                  {'project': 'alpha'})
        rows = connection.execute(
            'SELECT event_key, event_type, payload, repo FROM notification_outbox').fetchall()
        adapter = mock.Mock()
        self.assertEqual(delivery._deliver_rows(connection, 'token', 'chat', rows, adapter), 0)
        self.assertEqual(delivery._deliver_rows(connection, 'token', 'chat', rows, adapter), 0)
        adapter.assert_not_called()
        self.assertEqual(self.events(connection)[0]['attempts'], 0)

    def test_pending_targeted_failure_notices_are_canceled_without_losing_sent_piece_audit(self):
        for kind in ('persistent_execution_failure', 'stage_failed'):
            for attempts, pieces in ((0, 0), (1, 0), (1, 1)):
                with self.subTest(kind=kind, attempts=attempts, pieces=pieces):
                    connection = self.connection()
                    payload = {'project': 'alpha', 'execplan_id': 'G-018', 'reason_code': 'worker_correction',
                        'operation': 'Worker', 'cause': 'Worker가 해결되지 않은 targeted unit test 실패를 보고했습니다.',
                        'status': '다음 실행에서 교정', 'stage': 'WORKING', 'scope': 'execution',
                        'consecutive_external_runs': 3}
                    notifications._insert(connection, 'legacy-targeted', '/projects/alpha', kind, payload)
                    connection.execute('UPDATE notification_outbox SET attempts=?, confirmed_pieces=?, rendered_text=?',
                        (attempts, pieces, 'already confirmed piece\n' + 'pending content\n' * 700))
                    connection.commit()
                    transport = mock.Mock()
                    notifications.dispatch_pending(connection, 'token', 'chat', adapter=transport)
                    transport.assert_not_called()
                    row = connection.execute('SELECT canceled_at, attempts, confirmed_pieces, payload FROM notification_outbox').fetchone()
                    self.assertIsNotNone(row[0])
                    self.assertEqual((row[1], row[2]), (attempts, pieces))
                    self.assertEqual(json.loads(row[3])['cause'], payload['cause'])

    def test_failed_delivery_stays_pending_retries_once_and_hides_secrets(self):
        connection = self.connection()
        self.prepare_pr_candidate(connection)
        loop_manager.update_project(
            connection,
            "/projects/alpha",
            phase="PR_OPEN",
            plan_path="docs/plans/[G-018] Reliable notices.md",
            pr_url="https://example.test/pr/18",
        )
        token = "telegram-secret-token"
        chat_id = "secret-chat"
        failed = mock.Mock(
            side_effect=RuntimeError(f"https://api.telegram.org/bot{token}/failed")
        )
        from loop_manager_internal.notifications import delivery
        with mock.patch.object(delivery.events, "emit") as output:
            self.assertEqual(notifications.dispatch_pending(connection, token, chat_id, adapter=failed), 0)
        self.assertEqual(output.call_args.kwargs["level"], "WARN")
        self.assertEqual(output.call_args.kwargs["project"], "/projects/alpha")
        self.assertIn("다음 실행에서 재시도", output.call_args.args[0])
        self.assertNotIn(token, str(output.call_args))
        self.assertNotIn(chat_id, str(output.call_args))
        pending = self.events(connection)[0]
        self.assertIsNone(pending["sent_at"])
        self.assertIsNone(pending["delivery_started_at"])
        self.assertEqual(pending["attempts"], 1)
        self.assertNotIn(token, pending["last_error"])
        self.assertNotIn(chat_id, pending["last_error"])

        delivered = mock.Mock()
        self.assertEqual(
            notifications.dispatch_pending(
                connection, token, chat_id, adapter=delivered
            ),
            1,
        )
        self.assertEqual(
            notifications.dispatch_pending(
                connection, token, chat_id, adapter=delivered
            ),
            0,
        )
        delivered.assert_called_once()
        self.assertNotIn(token, delivered.call_args.args[2])
        self.assertNotIn(chat_id, delivered.call_args.args[2])

    def test_new_pr_delivery_is_scoped_and_fallback_keeps_pending_failure(self):
        connection = self.connection()
        repo = "/projects/alpha"
        other_repo = "/projects/beta"
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (other_repo,),
        )
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, pr_url
            ) VALUES (?, 'G-019', 'docs/plans/other.md', 'Other PR',
                      '[]', '[]', 'normal', 'hash', 'PR_OPEN', ?)
            """,
            (other_repo, "https://example.test/pr/19"),
        )
        with connection:
            self.prepare_pr_candidate(connection)
            loop_manager.update_project(
                connection,
                repo,
                phase="PR_OPEN",
                plan_path="docs/plans/[G-018] Reliable notices.md",
                pr_url="https://example.test/pr/18",
            )
            self.prepare_pr_candidate(
                connection, repo=other_repo, plan_id='G-019',
                pr_url='https://example.test/pr/19',
                path='docs/plans/other.md', title='Other PR',
            )
            notifications.enqueue_current_project_events(connection, other_repo)

        failed = mock.Mock(side_effect=RuntimeError("network unavailable"))
        self.assertEqual(
            notifications.dispatch_new_pr_notifications(
                connection, "token", "chat", repo, adapter=failed
            ),
            0,
        )
        failed.assert_called_once()
        # A later project step does not immediately retry the failed event.
        self.assertEqual(
            notifications.dispatch_new_pr_notifications(
                connection, "token", "chat", repo, adapter=mock.Mock()
            ),
            0,
        )
        pending = connection.execute(
            "SELECT attempts, sent_at FROM notification_outbox WHERE repo = ?",
            (repo,),
        ).fetchone()
        self.assertEqual(tuple(pending), (1, None))
        other = connection.execute(
            "SELECT attempts FROM notification_outbox WHERE repo = ?",
            (other_repo,),
        ).fetchone()
        self.assertEqual(other[0], 0)

        delivered = mock.Mock()
        self.assertEqual(
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=delivered
            ),
            2,
        )
        self.assertEqual(delivered.call_count, 2)
        self.assertEqual(
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=delivered
            ),
            0,
        )

    def test_pr_delivery_precedes_next_project_step(self):
        connection = self.connection()
        repo = "/projects/alpha"
        delivered = mock.Mock()
        steps = []

        def run_step(*_args, **_kwargs):
            steps.append(len(steps) + 1)
            if len(steps) == 1:
                self.prepare_pr_candidate(connection)
                loop_manager.update_project(
                    connection,
                    repo,
                    phase="PR_OPEN",
                    plan_path="docs/plans/[G-018] Reliable notices.md",
                    pr_url="https://example.test/pr/18",
                )
                self.assertIsNone(self.events(connection)[0]["sent_at"])
                return loop_manager.PROJECT_STEP_RETRY
            self.assertIsNotNone(self.events(connection)[0]["sent_at"])
            delivered.assert_called_once()
            return 0

        def deliver():
            notifications.dispatch_new_pr_notifications(
                connection, "token", "chat", repo, adapter=delivered
            )

        with mock.patch(
            "loop_manager._checkpoint_dispatch_ready", return_value=False
        ), mock.patch("loop_manager._run_project_step", side_effect=run_step):
            result = loop_manager.run_project_result(
                connection,
                Path("/state"),
                repo,
                Path("unused.ini"),
                None,
                False,
                on_step_completed=deliver,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(steps, [1, 2])
        self.assertEqual(self.events(connection)[0]["attempts"], 1)
        self.assertEqual(
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=delivered
            ),
            0,
        )
        delivered.assert_called_once()

    def test_registered_project_wires_immediate_delivery_after_commit(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            state_dir = Path(directory)
            connection = self.connection(database=state_dir / "loop-manager.db")
            cleanup.callback(connection.close)
            repo = "/projects/alpha"
            project = project_registry.RegisteredProject(
                repo, "main", "codex", None, False
            )
            delivered = mock.Mock()
            def run_one(worker_connection, *_args, on_step_completed, **_kwargs):
                with worker_connection:
                    self.prepare_pr_candidate(worker_connection)
                    notifications.enqueue_current_project_events(
                        worker_connection, repo
                    )
                self.assertIsNone(
                    worker_connection.execute(
                        "SELECT sent_at FROM notification_outbox"
                    ).fetchone()[0]
                )
                on_step_completed()
                self.assertIsNone(
                    worker_connection.execute(
                        "SELECT sent_at FROM notification_outbox"
                    ).fetchone()[0]
                )
                notifications.flush_new_pr_events(worker_connection, repo)
                self.assertIsNotNone(
                    worker_connection.execute(
                        "SELECT sent_at FROM notification_outbox"
                    ).fetchone()[0]
                )
                return loop_manager.ProjectRunResult(
                    loop_manager.ProjectRunKind.WAITING, 0
                )

            with mock.patch(
                "loop_manager.run_project_result", side_effect=run_one
            ), mock.patch(
                "loop_manager_internal.notifications.delivery.send_telegram",
                side_effect=delivered,
            ):
                result = loop_manager._run_registered_project(
                    project,
                    state_dir,
                    Path("unused.ini"),
                    "token",
                    "chat",
                    mock.Mock(),
                    5,
                )
            self.assertEqual(result.result.kind, loop_manager.ProjectRunKind.WAITING)
            delivered.assert_called_once()
            self.assertEqual(
                notifications.dispatch_pending(
                    connection, "token", "chat", adapter=delivered
                ),
                0,
            )
            connection.close()

    def test_ambiguous_interruption_stays_pending_and_retries_after_restart(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            cleanup.callback(connection.close)
            self.prepare_pr_candidate(connection)
            loop_manager.update_project(
                connection,
                "/projects/alpha",
                phase="PR_OPEN",
                plan_path="docs/plans/[G-018] Reliable notices.md",
                pr_url="https://example.test/pr/18",
            )
            delivered = []

            def deliver_then_interrupt(_token, _chat_id, text):
                delivered.append(text)
                raise KeyboardInterrupt("process interrupted after delivery")

            with self.assertRaisesRegex(KeyboardInterrupt, "process interrupted"):
                notifications.dispatch_pending(
                    connection,
                    "token",
                    "chat",
                    adapter=deliver_then_interrupt,
                )
            connection.close()

            restarted = sqlite3.connect(database)
            cleanup.callback(restarted.close)
            restarted.row_factory = sqlite3.Row
            notifications.ensure_schema(restarted)
            claimed = self.events(restarted)[0]
            self.assertEqual(claimed["attempts"], 1)
            self.assertIsNotNone(claimed["delivery_started_at"])
            self.assertIsNone(claimed["sent_at"])

            restarted_adapter = mock.Mock()
            self.assertEqual(
                notifications.dispatch_pending(
                    restarted,
                    "token",
                    "chat",
                    adapter=restarted_adapter,
                ),
                1,
            )
            restarted_adapter.assert_called_once()
            retried = self.events(restarted)[0]
            self.assertEqual(retried["attempts"], 2)
            self.assertIsNotNone(retried["sent_at"])
            restarted.close()
        self.assertEqual(len(delivered), 1)

    def test_scheduler_retries_pending_when_project_processing_is_skipped(self):
        connection = self.connection()
        self.prepare_pr_candidate(connection)
        loop_manager.update_project(
            connection,
            "/projects/alpha",
            phase="PR_OPEN",
            plan_path="docs/plans/[G-018] Reliable notices.md",
            pr_url="https://example.test/pr/18",
        )
        config = mock.Mock(
            telegram_token="token",
            telegram_chat_id="chat",
            max_workers=1,
        )
        delivered = mock.Mock()
        original_dispatch = notifications.dispatch_pending

        def dispatch_pending(connection, token, chat_id, **options):
            return original_dispatch(
                connection, token, chat_id, adapter=delivered, **options
            )

        with mock.patch(
            "loop_manager.notifications.dispatch_pending",
            side_effect=dispatch_pending,
        ) as dispatch, mock.patch(
            "loop_manager.schedule_registered_projects", return_value=()
        ):
            self.assertEqual(
                loop_manager.run_registered_projects(
                    connection, Path("/state"), config
                ),
                0,
            )
        self.assertEqual(dispatch.call_count, 2)
        self.assertTrue(all(call.kwargs.get('exclude_pr') for call in dispatch.call_args_list))
        delivered.assert_not_called()
        pending = self.events(connection)[0]
        self.assertIsNone(pending["sent_at"])
        self.assertEqual(pending['attempts'], 0)


class SegmentedDeliveryTest(NotificationFixture, unittest.TestCase):
    def add_event(self, connection, kind="pr_created", body=""):
        candidate = self.prepare_pr_candidate(connection)
        payload = {
            "project": "alpha", "execplan_id": "G-018",
            "execplan_title": body, "pr_url": "https://example.test/pr/18",
            **candidate,
            "occurred_at": "2000-01-01T00:00:00Z",
            "status": "PR 검토·Merge 대기", "silent": False,
        }
        notifications._insert(connection, "test-event", "/projects/alpha", kind, payload)
        connection.commit()
        return notifications._message(kind, payload)

    def test_exact_boundaries_and_unicode_round_trip(self):
        for length in (4096, 4097, 8193):
            with self.subTest(length=length):
                connection = self.connection()
                header = "[alpha] G-018 PR 검토 후보 게시\n"
                suffix = "\nhttps://example.test/pr/18\n상태: PR 검토·Merge 대기\n발생 시각: 2000-01-01T00:00:00Z\n처리 상태: PR 검토·Merge 대기"
                title_length = length - len(header) - len(suffix)
                title = ("한글🙂\n" * (title_length // 4 + 1))[:title_length]
                body = self.add_event(connection, body=title)
                self.assertEqual(len(body), length)
                calls = []
                self.assertEqual(notifications.dispatch_pending(
                    connection, "token", "chat",
                    adapter=lambda _token, _chat, text: calls.append(text),
                ), 1)
                self.assertEqual([len(piece) for piece in calls],
                                 [4096] * (length // 4096) +
                                 ([length % 4096] if length % 4096 else []))
                self.assertEqual("".join(calls), body)
                row = self.events(connection)[0]
                self.assertEqual(row["confirmed_pieces"], len(calls))
                self.assertIsNotNone(row["sent_at"])
                connection.close()

    def test_later_failure_resumes_frozen_text_after_restart(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            cleanup.callback(connection.close)
            original = self.add_event(connection, body="가🙂\n" * 3000)
            delivered = []

            def fail_second(_token, _chat, text):
                if delivered:
                    raise RuntimeError("body-secret token-secret chat-secret")
                delivered.append(text)

            self.assertEqual(notifications.dispatch_pending(
                connection, "token-secret", "chat-secret", adapter=fail_second,
            ), 0)
            row = self.events(connection)[0]
            self.assertEqual(row["confirmed_pieces"], 1)
            self.assertEqual(row["attempts"], 2)
            self.assertIsNone(row["sent_at"])
            self.assertEqual(row["rendered_text"], original)
            for secret in ("body-secret", "token-secret", "chat-secret", original):
                self.assertNotIn(secret, row["last_error"])
            connection.execute(
                "UPDATE notification_outbox SET payload = ? WHERE event_key = ?",
                (json.dumps({**json.loads(row['payload']), 'project': 'changed'}), "test-event"),
            )
            connection.commit()
            connection.close()

            restarted = sqlite3.connect(database)
            cleanup.callback(restarted.close)
            restarted.row_factory = sqlite3.Row
            notifications.ensure_schema(restarted)
            remaining = []
            self.assertEqual(notifications.dispatch_pending(
                restarted, "token", "chat",
                adapter=lambda _token, _chat, text: remaining.append(text),
            ), 1)
            self.assertEqual("".join(delivered + remaining), original)
            self.assertEqual(self.events(restarted)[0]["confirmed_pieces"], 3)
            self.assertEqual(notifications.dispatch_pending(
                restarted, "token", "chat", adapter=mock.Mock(),
            ), 0)
            restarted.close()

    def test_interrupted_later_piece_retries_only_uncertain_piece(self):
        connection = self.connection()
        original = self.add_event(connection, body="x" * 8200)
        delivered = []

        def interrupt(_token, _chat, text):
            delivered.append(text)
            if len(delivered) == 2:
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            notifications.dispatch_pending(
                connection, "token", "chat", adapter=interrupt,
            )
        row = self.events(connection)[0]
        self.assertEqual(row["confirmed_pieces"], 1)
        self.assertEqual(row["attempts"], 2)
        self.assertIsNotNone(row["delivery_started_at"])
        retried = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _token, _chat, text: retried.append(text),
        ), 1)
        self.assertEqual(retried[0], delivered[1])
        self.assertEqual("".join([delivered[0], *retried]), original)
        self.assertEqual(self.events(connection)[0]["attempts"], 4)

    def test_legacy_persistent_failure_pieces_are_canceled_without_losing_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = self.connection(database=database)
            payload = {
                "project": "alpha", "stage": "WORKING", "scope": "execution",
                "execplan_id": "G-018", "reason_code": "fetch_failed",
                "cause": "fetch failed", "consecutive_external_runs": 3,
            }
            notifications._insert(
                connection, "legacy-persistent-failure", "/projects/alpha",
                "persistent_execution_failure", payload,
            )
            unsent_key = "legacy-persistent-failure-unsent"
            notifications._insert(
                connection, unsent_key, "/projects/alpha",
                "persistent_execution_failure", payload,
            )
            payload_before = connection.execute(
                "SELECT payload FROM notification_outbox WHERE event_key = ?",
                ("legacy-persistent-failure",),
            ).fetchone()[0]
            saved_text = "frozen legacy text" * 300
            connection.execute(
                "UPDATE notification_outbox SET attempts = 2, last_error = ?, "
                "rendered_text = ?, confirmed_pieces = 1, "
                "delivery_started_at = CURRENT_TIMESTAMP WHERE event_key = ?",
                ("previous send failed", saved_text, "legacy-persistent-failure"),
            )
            connection.commit()

            sender = mock.Mock()
            self.assertEqual(notifications.dispatch_pending(
                connection, "token", "chat", adapter=sender,
            ), 0)
            sender.assert_not_called()
            row = connection.execute(
                "SELECT attempts, last_error, rendered_text, confirmed_pieces, "
                "canceled_at, delivery_started_at, sent_at, payload "
                "FROM notification_outbox WHERE event_key = ?",
                ("legacy-persistent-failure",),
            ).fetchone()
            self.assertEqual(row[0:4], (
                2, "previous send failed", saved_text, 1,
            ))
            self.assertIsNotNone(row[4])
            self.assertIsNone(row[5])
            self.assertIsNone(row[6])
            self.assertEqual(row[7], payload_before)
            unsent = connection.execute(
                "SELECT attempts, confirmed_pieces, canceled_at, sent_at "
                "FROM notification_outbox WHERE event_key = ?", (unsent_key,),
            ).fetchone()
            self.assertEqual(unsent[0:2], (0, 0))
            self.assertIsNotNone(unsent[2])
            self.assertIsNone(unsent[3])
            connection.close()

            restarted = sqlite3.connect(database)
            restarted.row_factory = sqlite3.Row
            notifications.ensure_schema(restarted)
            retry = mock.Mock()
            self.assertEqual(notifications.dispatch_pending(
                restarted, "token", "chat", adapter=retry,
            ), 0)
            retry.assert_not_called()
            self.assertIsNotNone(restarted.execute(
                "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
                ("legacy-persistent-failure",),
            ).fetchone()[0])
            self.assertIsNotNone(restarted.execute(
                "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
                (unsent_key,),
            ).fetchone()[0])
            restarted.close()

    def test_canceled_inflight_legacy_piece_keeps_error_and_does_not_resume(self):
        for failure_in_flight in (False, True):
            with self.subTest(failure_in_flight=failure_in_flight):
                with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
                    database = Path(directory) / "state.db"
                    connection = self.connection(database=database)
                    cleanup.callback(connection.close)
                    repo = "/projects/alpha"
                    subject, episode, phase = "G-018", 2, "WORKING"
                    reason_code, fingerprint = "fetch_failed", "failure-fingerprint"
                    identity = "\0".join((subject, str(episode), phase, reason_code, fingerprint))
                    event_key = notifications._key("persistent_failure", repo, identity)
                    payload = {
                        "project": "alpha", "stage": phase, "scope": "execution",
                        "execplan_id": subject, "reason_code": reason_code,
                        "cause": "fetch failed", "consecutive_external_runs": 3,
                    }
                    notifications._insert(
                        connection, event_key, repo, "persistent_execution_failure", payload,
                    )
                    saved_text = "frozen legacy text" * 300
                    connection.execute(
                        "UPDATE notification_outbox SET attempts = 1, last_error = ?, "
                        "rendered_text = ?, delivery_started_at = CURRENT_TIMESTAMP "
                        "WHERE event_key = ?",
                        ("previous send failed", saved_text, event_key),
                    )
                    connection.commit()

                    first_piece = saved_text[:4096]
                    original_claim = delivery._claim_piece
                    claimed = False

                    def claim_piece(current, key, *, first_attempt_only):
                        nonlocal claimed
                        if not claimed:
                            claimed = True
                            # An older process had claimed this piece before policy changed.
                            return first_piece, 0, 2, []
                        return original_claim(
                            current, key, first_attempt_only=first_attempt_only,
                        )

                    attempted = []

                    def cancel_during_send(_token, _chat, text):
                        concurrent = sqlite3.connect(database)
                        try:
                            with concurrent:
                                notifications.cancel_pending_persistent_execution_failure_event(
                                    concurrent, repo, subject, episode, phase,
                                    reason_code, fingerprint,
                                )
                        finally:
                            concurrent.close()
                        attempted.append(text)
                        if failure_in_flight:
                            raise RuntimeError("network unavailable")

                    with mock.patch.object(delivery, "_claim_piece", side_effect=claim_piece):
                        self.assertEqual(notifications.dispatch_pending(
                            connection, "token", "chat", adapter=cancel_during_send,
                        ), 0)
                    self.assertEqual(attempted, [first_piece])
                    row = connection.execute(
                        "SELECT attempts, last_error, rendered_text, confirmed_pieces, "
                        "canceled_at, delivery_started_at, sent_at FROM notification_outbox "
                        "WHERE event_key = ?", (event_key,),
                    ).fetchone()
                    confirmed_pieces = 0 if failure_in_flight else 1
                    self.assertEqual(row[:4], (
                        1, "previous send failed", saved_text, confirmed_pieces,
                    ))
                    self.assertIsNotNone(row[4])
                    self.assertIsNone(row[5])
                    self.assertIsNone(row[6])
                    restarted = sqlite3.connect(database)
                    cleanup.callback(restarted.close)
                    restarted.row_factory = sqlite3.Row
                    notifications.ensure_schema(restarted)
                    retry = mock.Mock()
                    self.assertEqual(notifications.dispatch_pending(
                        restarted, "token", "chat", adapter=retry,
                    ), 0)
                    retry.assert_not_called()
                    persisted = restarted.execute(
                        "SELECT attempts, last_error, rendered_text, confirmed_pieces, "
                        "canceled_at FROM notification_outbox WHERE event_key = ?",
                        (event_key,),
                    ).fetchone()
                    self.assertEqual(tuple(persisted[:4]), (
                        1, "previous send failed", saved_text, confirmed_pieces,
                    ))
                    self.assertIsNotNone(persisted[4])

    def test_immediate_pr_dispatch_uses_same_piece_cursor(self):
        connection = self.connection()
        original = self.add_event(connection, body="x" * 5000)
        calls = []
        self.assertEqual(notifications.dispatch_new_pr_notifications(
            connection, "token", "chat", "/projects/alpha",
            adapter=lambda _token, _chat, text: calls.append(text),
        ), 1)
        self.assertEqual("".join(calls), original)
        self.assertEqual(self.events(connection)[0]["attempts"], 2)
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=mock.Mock(),
        ), 0)

    def test_cleared_block_cancels_remaining_pieces_and_keeps_progress(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            scoped_blocking.record_plan_block(
                connection, repo, "G-018",
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                "x" * 9000, "fingerprint",
            )
            checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(
                connection
            )
            checkpoint.finish_unpublished(
                checkpoint.get(repo, "G-018"), "local only", retryable=False,
            )
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        calls = []

        def clear_after_first(_token, _chat, text):
            calls.append(text)
            scoped_blocking.clear_plan_block(connection, repo, "G-018")
            connection.commit()

        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=clear_after_first,
        ), 0)
        row = self.events(connection)[0]
        self.assertEqual(len(calls), 1)
        self.assertEqual(row["confirmed_pieces"], 1)
        self.assertIsNotNone(row["canceled_at"])
        self.assertIsNone(row["sent_at"])
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=mock.Mock(),
        ), 0)

    def test_published_block_with_remote_inspection_is_delivered(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            scoped_blocking.record_plan_block(
                connection, repo, "G-018",
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                "Decision still required", "fingerprint",
            )
            self.confirm_worker_question(connection)
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
        request = checkpoint.get(repo, "G-018")
        checkpoint.finish_published(request, "a" * 40)
        original_key = self.events(connection)[0]["event_key"]
        checkpoint.record_remote_inspection(
            request, "b" * 40, "input", "still blocked",
        )
        self.assertEqual(len(self.events(connection)), 2)
        current_key = next(
            row["event_key"] for row in self.events(connection)
            if row["event_key"] != original_key
        )

        delivered = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat", adapter=delivered,
        ), 1)
        delivered.assert_called_once()
        rows = {row["event_key"]: row for row in self.events(connection)}
        self.assertIsNotNone(rows[original_key]["canceled_at"])
        self.assertIsNone(rows[current_key]["canceled_at"])
        self.assertIsNotNone(rows[current_key]["sent_at"])

    def test_cleared_planning_error_cancels_only_its_unsent_notice(self):
        connection = self.connection()
        repo = "/projects/alpha"
        with connection:
            self.legacy_planning_notice(connection, 'Old failure')
        pr_message = self.add_event(connection, body="Unrelated PR")
        connection.execute(
            "UPDATE projects SET planning_error = NULL WHERE repo = ?", (repo,),
        )
        connection.commit()
        calls = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _token, _chat, text: calls.append(text),
        ), 1)
        self.assertEqual(calls, [pr_message])
        rows = {row["event_type"]: row for row in self.events(connection)}
        self.assertIsNotNone(rows["blocked"]["canceled_at"])
        self.assertEqual(rows["blocked"]["confirmed_pieces"], 0)
        self.assertIsNone(rows["blocked"]["sent_at"])
        self.assertIsNotNone(rows["pr_created"]["sent_at"])

    def test_legacy_pending_row_gains_piece_progress(self):
        connection = self.connection()
        connection.execute("DROP TABLE notification_outbox")
        connection.execute(
            "CREATE TABLE notification_outbox (event_key TEXT PRIMARY KEY, "
            "repo TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL, "
            "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "delivery_started_at TEXT, sent_at TEXT)"
        )
        original = self.add_event(connection, body="legacy")
        calls = []
        self.assertEqual(notifications.dispatch_pending(
            connection, "token", "chat",
            adapter=lambda _token, _chat, text: calls.append(text),
        ), 1)
        self.assertEqual(calls, [original])
        self.assertEqual(self.events(connection)[0]["confirmed_pieces"], 1)
        self.assertIsNotNone(self.events(connection)[0]["delivery_started_at"])


class TimelyProgressDeliveryTest(NotificationFixture, unittest.TestCase):
    def test_published_batch_silent_flag_survives_split_and_http_retry(self):
        connection = self.connection()
        repo = '/projects/alpha'
        path = 'docs/plans/[G-018] Reliable notices.md'
        with connection:
            connection.execute("UPDATE exec_plans SET title=?, publication_path=path, publication_sha='abc' WHERE repo=?", ('Long ' * 1000, repo))
            notifications.record_planning_publication(connection, repo, [(path, {'id': 'G-018'})], 'abc')
        requests = []
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        def send(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 2:
                raise OSError('offline')
            return response
        with mock.patch.object(transport, 'urlopen', side_effect=send):
            self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat'), 0)
            self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat'), 1)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(item['disable_notification'] is True for item in requests))
        self.assertEqual(requests[1], requests[2])

    def test_late_publication_confirmation_removes_item_from_continuation(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute('DELETE FROM exec_plans WHERE repo=?', (repo,))
            plans = []
            for plan_id, title in (('G-018', 'Long ' * 1000), ('G-019', 'Removed'), ('G-020', 'Remaining')):
                path = f'docs/plans/[{plan_id}] {title}.md'
                connection.execute("INSERT INTO exec_plans (repo,plan_id,path,title,sources,depends_on,priority,content_hash,status,publication_path,publication_sha) VALUES (?,?,?,?,'[]','[]','normal','hash','READY',?,'abc')", (repo, plan_id, path, title, path))
                plans.append((path, {'id': plan_id}))
            notifications.record_planning_publication(connection, repo, plans, 'abc')
        def send(_token, _chat, _text):
            with connection:
                connection.execute("DELETE FROM exec_plans WHERE repo=? AND plan_id='G-019'", (repo,))
                notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=send), 0)
        rows = self.events(connection)
        self.assertEqual(rows[0]['confirmed_pieces'], 1)
        self.assertEqual([item['id'] for item in json.loads(rows[1]['payload'])['plans']], ['G-018', 'G-020'])
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 0)
        self.assertEqual([item['id'] for item in json.loads(self.events(connection)[2]['payload'])['plans']], ['G-020'])
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertNotIn('G-018', sender.call_args.args[2])

    def test_publication_remains_current_after_catalog_rename(self):
        connection = self.connection()
        repo = '/projects/alpha'
        path = 'docs/plans/[G-018] Reliable notices.md'
        with connection:
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='abc' WHERE repo=?", (repo,))
            notifications.record_planning_publication(connection, repo,
                [(path, {'id': 'G-018'})], 'abc')
            connection.execute("UPDATE exec_plans SET path='docs/plans/[G-018] Renamed.md', title='Renamed' WHERE repo=?", (repo,))
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertIn('G-018 · Reliable notices', sender.call_args.args[2])
        self.assertEqual(len(self.events(connection)), 1)

    def test_partial_publication_withdrawal_retains_only_unconfirmed_valid_items(self):
        connection = self.connection()
        repo = '/projects/alpha'
        long_title = 'Long ' * 1000
        plans = []
        with connection:
            connection.execute("DELETE FROM exec_plans WHERE repo=?", (repo,))
            for plan_id, title in (('G-018', long_title), ('G-019', 'Withdrawn'), ('G-020', 'Retained')):
                path = f'docs/plans/[{plan_id}] {title}.md'
                connection.execute("INSERT INTO exec_plans (repo,plan_id,path,title,sources,depends_on,priority,content_hash,status,publication_path,publication_sha) VALUES (?,?,?,?,'[]','[]','normal','hash','READY',?,'abc')", (repo, plan_id, path, title, path))
                plans.append((path, {'id': plan_id, 'title': title}))
            notifications.record_planning_publication(connection, repo, plans, 'abc')
        def send(_token, _chat, _text):
            with connection:
                connection.execute("DELETE FROM exec_plans WHERE repo=? AND plan_id='G-019'", (repo,))
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=send), 0)
        old, continuation = self.events(connection)
        self.assertEqual(old['confirmed_pieces'], 1)
        self.assertIsNotNone(old['canceled_at'])
        payload = json.loads(continuation['payload'])
        self.assertEqual([item['id'] for item in payload['plans']], ['G-020'])
        self.assertTrue(payload['silent'])
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertIn('G-020 · Retained', sender.call_args.args[2])
        self.assertNotIn('G-019', sender.call_args.args[2])

    def test_publication_reobservation_retains_other_unsent_items(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='abc' WHERE repo=?", (repo,))
            path = 'docs/plans/[G-019] Removed.md'
            connection.execute("INSERT INTO exec_plans (repo,plan_id,path,title,sources,depends_on,priority,content_hash,status,publication_path,publication_sha) VALUES (?,'G-019',?,'Removed','[]','[]','normal','hash','READY',?,'abc')", (repo, path, path))
            notifications.record_planning_publication(connection, repo, [
                ('docs/plans/[G-018] Reliable notices.md', {'id': 'G-018'}),
                (path, {'id': 'G-019'})], 'abc')
            connection.execute("DELETE FROM exec_plans WHERE repo=? AND plan_id='G-019'", (repo,))
            notifications.enqueue_current_project_events(connection, repo)
        old, continuation = self.events(connection)
        self.assertIsNotNone(old['canceled_at'])
        self.assertEqual([item['id'] for item in json.loads(continuation['payload'])['plans']], ['G-018'])
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)

    def test_registered_publication_is_deduplicated_and_sent_silently(self):
        connection = self.connection()
        repo = '/projects/alpha'
        path = 'docs/plans/[G-018] Reliable notices.md'
        with connection:
            connection.execute("UPDATE projects SET origin='https://github.com/org/repo' WHERE repo=?", (repo,))
            connection.execute("UPDATE exec_plans SET publication_path=?, publication_sha='abc', depends_on='[]' WHERE repo=? AND plan_id='G-018'", (path, repo))
            plans = [(path, {'id': 'G-018', 'title': 'Reliable notices'})]
            notifications.record_planning_publication(connection, repo, plans, 'abc')
            notifications.record_planning_publication(connection, repo, plans, 'abc')
        rows = self.events(connection)
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]['payload'])
        self.assertTrue(payload['silent'])
        self.assertEqual(payload['plans'][0]['url'],
            'https://github.com/org/repo/blob/abc/docs/plans/%5BG-018%5D%20Reliable%20notices.md')
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(transport, 'urlopen', return_value=response) as send:
            self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat'), 1)
        request = json.loads(send.call_args.args[0].data)
        self.assertTrue(request['disable_notification'])
        self.assertIn('G-018 · Reliable notices · 실행 대기', request['text'])

    def test_published_plan_rendering_shows_links_and_actual_waiting_dependencies(self):
        from loop_manager_internal.notifications.rendering import _message
        text = _message('plans_published', {'project': 'alpha', 'plans': [
            {'id': 'G-018', 'title': 'Ready\nwork', 'waiting_for': [],
             'url': 'https://github.com/org/repo/blob/abc/docs/plans/one.md'},
            {'id': 'G-019', 'title': 'Dependent', 'waiting_for': ['G-018'], 'url': None},
        ]})
        self.assertIn('새 작업 게시 2건', text)
        self.assertIn('G-018 · Ready work · 실행 대기', text)
        self.assertIn('https://github.com/org/repo/blob/abc/docs/plans/one.md', text)
        self.assertIn('G-019 · Dependent · 선행 작업 대기: G-018', text)

    def test_action_delivery_waits_for_commit_and_preserves_legacy_silent_pieces(self):
        connection = self.connection()
        repo = '/projects/alpha'
        sent = []
        with notifications.immediate_delivery(connection, 'token', 'chat', repo), \
             mock.patch.object(delivery, 'send_telegram', side_effect=lambda *args, **kw: sent.append((args, kw))):
            with connection:
                candidate = self.prepare_pr_candidate(connection)
                notifications._insert(connection, 'legacy-pr', repo, 'pr_created',
                    {'project': 'alpha', 'execplan_id': 'G-018', 'execplan_title': 'x' * 6000,
                     **candidate, 'silent': True})
                self.assertEqual(notifications.flush_project_events(connection, repo), 0)
            self.assertEqual(sent, [])
            self.assertEqual(notifications.flush_new_pr_events(connection, repo), 1)
            self.assertEqual(len(sent), 2)
            self.assertTrue(all(kw['disable_notification'] for _args, kw in sent))
            self.assertEqual(notifications.flush_new_pr_events(connection, repo), 0)

    def test_transport_preserves_explicit_silent_flag(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(transport, 'urlopen', return_value=response) as open_url:
            transport.send_telegram('token', 'chat', 'notice', disable_notification=True)
        self.assertTrue(json.loads(open_url.call_args.args[0].data)['disable_notification'])

    def test_silent_pieces_and_retry_reach_http_with_notification_disabled(self):
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            candidate = self.prepare_pr_candidate(connection)
            notifications._insert(connection, 'silent-http', repo, 'pr_created',
                {'project': 'alpha', 'execplan_id': 'G-018', 'execplan_title': 'x' * 6000,
                 **candidate, 'silent': True})
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        requests = []

        def open_url(request, timeout):
            requests.append(json.loads(request.data))
            if len(requests) == 2:
                raise OSError('offline')
            return response

        with mock.patch.object(transport, 'urlopen', side_effect=open_url):
            self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat'), 0)
            self.assertEqual(self.events(connection)[0]['confirmed_pieces'], 1)
            self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat'), 1)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request['disable_notification'] is True for request in requests))
        self.assertEqual(requests[1], requests[2])


class InterruptedBlockStartupTest(NotificationFixture, unittest.TestCase):
    def test_restart_recovers_canceled_inflight_start_before_worker_and_deduplicates(self):
        from types import SimpleNamespace
        from loop_manager_internal.notifications import delivery
        for confirmed in (0, 1):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / 'restart.db'
                connection = self.connection(database=database)
                repo = '/projects/alpha'
                with connection:
                    connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', ?)", (repo, 'x' * 6000))
                    self.confirm_worker_question(connection)
                    notifications.enqueue_current_project_events(connection, repo)
                    if confirmed:
                        connection.execute("UPDATE notification_outbox SET confirmed_pieces=1")
                key = self.events(connection)[0]['event_key']
                # Claim the next piece, then stop before confirmation or the
                # exception callback can observe its result.
                self.assertIsNotNone(delivery._claim_piece(connection, key, first_attempt_only=False))
                with connection:
                    scoped_blocking.clear_plan_block(connection, repo, 'G-018')
                    notifications.record_process_start(connection, repo, 'worker', 'saved-start', plan_id='G-018')
                started_at = connection.execute('SELECT started_at FROM notified_block_incidents').fetchone()[0]
                connection.close()
                restarted = sqlite3.connect(database)
                self.addCleanup(restarted.close)
                restarted.row_factory = sqlite3.Row
                config = SimpleNamespace(telegram_token='token', telegram_chat_id='chat')
                before = dict(self.events(restarted)[0])
                sent = mock.Mock()
                dispatch = notifications.dispatch_pending
                def worker(*a, **kw):
                    self.assertEqual(restarted.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)
                    self.assertEqual(restarted.execute('SELECT started_at FROM notified_block_incidents').fetchone()[0], started_at)
                    return ()
                with mock.patch('loop_manager.schedule_registered_projects', side_effect=worker), \
                        mock.patch.object(delivery, 'send_telegram', sent), \
                        mock.patch.object(notifications, 'dispatch_pending', side_effect=lambda conn, token, chat, **options: dispatch(conn, token, chat, adapter=sent, **options)):
                    self.assertEqual(loop_manager.run_registered_projects(restarted, Path(directory), config), 0)
                    self.assertEqual(loop_manager.run_registered_projects(restarted, Path(directory), config), 0)
                sent.assert_not_called()
                self.assertIsNotNone(restarted.execute('SELECT canceled_at FROM notification_outbox WHERE event_key=?', (key,)).fetchone()[0])
                with restarted:
                    notifications.record_process_start(restarted, repo, 'worker', 'saved-start', plan_id='G-018')
                    notifications.record_process_start(restarted, repo, 'worker', 'new-start', plan_id='G-018')
                self.assertEqual(restarted.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_type IN ('resumed', 'work_started')").fetchone()[0], 0)
                incident = restarted.execute('SELECT start_identity, started_at FROM notified_block_incidents').fetchone()
                self.assertEqual(tuple(incident), ('saved-start', started_at))
                after = dict(self.events(restarted)[0])
                self.assertEqual(after['confirmed_pieces'], int(confirmed))
                for field in before.keys() - {'delivery_started_at'}:
                    self.assertEqual(after[field], before[field], field)
                restarted.close()

    def test_disabled_startup_preserves_abandoned_record_without_notice(self):
        from types import SimpleNamespace
        from loop_manager_internal.notifications import delivery
        connection = self.connection()
        repo = '/projects/alpha'
        with connection:
            connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'plan', 'user_decision_required', 'cause')", (repo,))
            self.confirm_worker_question(connection)
            notifications.enqueue_current_project_events(connection, repo)
        key = self.events(connection)[0]['event_key']
        delivery._claim_piece(connection, key, first_attempt_only=False)
        with connection:
            scoped_blocking.clear_plan_block(connection, repo, 'G-018')
            notifications.record_process_start(connection, repo, 'worker', 'saved-start', plan_id='G-018')
        with mock.patch('loop_manager.schedule_registered_projects', return_value=()):
            loop_manager.run_registered_projects(connection, Path('/unused'), SimpleNamespace(telegram_token='', telegram_chat_id=''))
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(connection.execute('SELECT start_identity FROM notified_block_incidents').fetchone()[0], 'saved-start')


class DraftListNotificationTest(NotificationFixture, unittest.TestCase):
    repo = "/projects/alpha"

    def draft(self, connection, plan_id="G-018", title="보존 정책", *, revision="revision",
              remote_revision="remote-sha", url=True):
        from loop_manager_internal.storage import execplan_draft_state
        execplan_draft_state.ensure_schema(connection)
        path = f"docs/plans/[{plan_id}] {title}.md"
        connection.execute("UPDATE projects SET origin='https://github.com/org/repo' WHERE repo=?",
                           (self.repo,))
        connection.execute(
            "INSERT OR REPLACE INTO exec_plans "
            "(repo, plan_id, path, title, sources, depends_on, priority, content_hash, "
            "current_revision_sha, status) "
            "VALUES (?, ?, ?, ?, '[]', '[]', 'normal', 'hash', ?, 'READY')",
            (self.repo, plan_id, path, title, remote_revision if url else None),
        )
        saved = {"pending": True, "identity": "question-" + plan_id, "revision": revision,
                 "question": "Question: Which storage policy?", "path": path,
                 "remote_revision": remote_revision, "remote_url": "https://github.com/org/repo"}
        if url:
            saved["url"] = ("https://github.com/org/repo/blob/" + remote_revision + "/"
                            + quote(path, safe="/"))
        connection.execute("INSERT OR REPLACE INTO execplan_drafts VALUES (?, ?, 1, ?)",
                           (self.repo, plan_id, json.dumps(saved)))

    def test_new_drafts_share_one_notice_with_current_questions(self):
        connection = self.connection()
        with connection:
            self.draft(connection, 'G-018', 'Question')
            self.draft(connection, 'G-019', 'No question')
            connection.execute("UPDATE execplan_drafts SET evidence=json_remove(evidence, '$.question') WHERE plan_id='G-019'")
            for plan_id in ('G-018', 'G-019'):
                connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='remote-sha' WHERE plan_id=?", (plan_id,))
            plans = [(f'docs/plans/[{plan_id}] {title}.md', {'id': plan_id, 'draft': True})
                     for plan_id, title in (('G-018', 'Question'), ('G-019', 'No question'))]
            notifications.record_planning_publication(connection, self.repo, plans, 'remote-sha')
            notifications.record_planning_publication(connection, self.repo, plans, 'remote-sha')
        rows = self.events(connection)
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]['payload'])
        self.assertFalse(payload['silent'])
        self.assertEqual([item['id'] for item in payload['plans']], ['G-018', 'G-019'])
        self.assertIn('decision_identity', payload['plans'][0])
        self.assertNotIn('decision_identity', payload['plans'][1])
        self.assertTrue(all(item['publication_revision'] == 'remote-sha' for item in payload['plans']))
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertIn('G-019 · No question · 신규 게시', sender.call_args.args[2])
        with connection:
            notifications.enqueue_draft_decisions(connection, self.repo)
        self.assertEqual(len(self.events(connection)), 1)

    def test_mixed_publication_keeps_http_notification_levels_separate(self):
        connection = self.connection()
        with connection:
            self.draft(connection, 'G-018', 'Decision')
            path = 'docs/plans/[G-019] General.md'
            connection.execute("INSERT INTO exec_plans (repo,plan_id,path,title,sources,depends_on,priority,content_hash,status,publication_path,publication_sha) VALUES (?,'G-019',?,'General','[]','[]','normal','hash','READY',?,'remote-sha')", (self.repo, path, path))
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='remote-sha' WHERE plan_id='G-018'")
            plans = [('docs/plans/[G-018] Decision.md', {'id': 'G-018', 'draft': True}),
                     (path, {'id': 'G-019'})]
            notifications.record_planning_publication(connection, self.repo, plans, 'remote-sha')
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(transport, 'urlopen', return_value=response) as send:
            self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat'), 2)
        requests = [json.loads(call.args[0].data) for call in send.call_args_list]
        general = next(item for item in requests if 'G-019' in item['text'])
        draft = next(item for item in requests if 'G-018' in item['text'])
        self.assertTrue(general['disable_notification'])
        self.assertFalse(draft['disable_notification'])
        self.assertNotIn('G-018', general['text'])
        self.assertNotIn('G-019', draft['text'])

    def test_notification_failure_does_not_change_published_catalog(self):
        connection = self.connection()
        with connection:
            self.draft(connection, 'G-018', 'Decision')
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='remote-sha'")
            notifications.record_planning_publication(connection, self.repo,
                [('docs/plans/[G-018] Decision.md', {'id': 'G-018', 'draft': True})], 'remote-sha')
        before = tuple(connection.execute('SELECT * FROM exec_plans').fetchone())
        draft_before = tuple(connection.execute('SELECT * FROM execplan_drafts').fetchone())
        with mock.patch.object(transport, 'urlopen', side_effect=OSError('offline')):
            self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat'), 0)
        self.assertEqual(tuple(connection.execute('SELECT * FROM exec_plans').fetchone()), before)
        self.assertEqual(tuple(connection.execute('SELECT * FROM execplan_drafts').fetchone()), draft_before)
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(transport, 'urlopen', return_value=response):
            self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat'), 1)
        self.assertEqual(tuple(connection.execute('SELECT * FROM exec_plans').fetchone()), before)
        self.assertEqual(tuple(connection.execute('SELECT * FROM execplan_drafts').fetchone()), draft_before)

    def test_new_draft_without_question_is_still_notified(self):
        connection = self.connection()
        with connection:
            self.draft(connection, 'G-018', 'Draft')
            connection.execute("UPDATE execplan_drafts SET evidence=json_remove(evidence, '$.question')")
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='remote-sha'")
            notifications.record_planning_publication(connection, self.repo,
                [('docs/plans/[G-018] Draft.md', {'id': 'G-018', 'draft': True})], 'remote-sha')
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertIn('새 draft 게시', sender.call_args.args[2])
        self.assertNotIn('사용자 판단 대기', sender.call_args.args[2])

    def test_resolved_question_keeps_unsent_draft_creation(self):
        connection = self.connection()
        with connection:
            self.draft(connection, 'G-018', 'Draft')
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='remote-sha'")
            notifications.record_planning_publication(connection, self.repo,
                [('docs/plans/[G-018] Draft.md', {'id': 'G-018', 'draft': True})], 'remote-sha')
            connection.execute("UPDATE execplan_drafts SET pending=0")
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 0)
        rows = self.events(connection)
        self.assertIsNotNone(rows[0]['canceled_at'])
        self.assertEqual(len(rows), 2)
        item = json.loads(rows[1]['payload'])['plans'][0]
        self.assertEqual(item['publication_revision'], 'remote-sha')
        self.assertNotIn('decision_identity', item)
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertIn('G-018 · Draft · 신규 게시', sender.call_args.args[2])

    def test_draft_question_rename_keeps_original_publication_identity(self):
        connection = self.connection()
        with connection:
            self.draft(connection, 'G-018', 'Original', remote_revision='remote-sha')
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='remote-sha'")
            notifications.record_planning_publication(connection, self.repo,
                [('docs/plans/[G-018] Original.md', {'id': 'G-018', 'draft': True})], 'remote-sha')
            new_path = 'docs/plans/[G-018] Renamed.md'
            connection.execute("UPDATE exec_plans SET path=?, title='Renamed', current_revision_sha='new-sha'", (new_path,))
            connection.execute("UPDATE execplan_drafts SET evidence=json_set(evidence, '$.path', ?)", (new_path,))
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 0)
        old, continuation = self.events(connection)
        self.assertIsNotNone(old['canceled_at'])
        item = json.loads(continuation['payload'])['plans'][0]
        self.assertEqual(item['source_path'], new_path)
        self.assertEqual(item['publication_path'], 'docs/plans/[G-018] Original.md')
        self.assertIn('/blob/new-sha/', item['url'])
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertIn('G-018 · Renamed · 신규 게시', sender.call_args.args[2])

    def test_partial_draft_notice_keeps_only_unconfirmed_creation(self):
        connection = self.connection()
        title = 'Long ' * 1000
        with connection:
            self.draft(connection, 'G-018', title)
            self.draft(connection, 'G-019', 'Later')
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='remote-sha'")
            plans = [(f'docs/plans/[{plan_id}] {name}.md', {'id': plan_id, 'draft': True})
                     for plan_id, name in (('G-018', title), ('G-019', 'Later'))]
            notifications.record_planning_publication(connection, self.repo, plans, 'remote-sha')
        def send(_token, _chat, _text):
            with connection:
                connection.execute("UPDATE execplan_drafts SET pending=0 WHERE plan_id='G-019'")
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=send), 0)
        rows = self.events(connection)
        self.assertEqual(rows[0]['confirmed_pieces'], 1)
        self.assertIsNotNone(rows[0]['canceled_at'])
        self.assertEqual([item['id'] for item in json.loads(rows[1]['payload'])['plans']], ['G-019'])
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertIn('G-019 · Later · 신규 게시', sender.call_args.args[2])

    def test_late_draft_confirmation_removes_creation_from_continuation(self):
        connection = self.connection()
        title = 'Long ' * 1000
        with connection:
            self.draft(connection, 'G-018', title)
            self.draft(connection, 'G-019', 'Later')
            connection.execute("UPDATE exec_plans SET publication_path=path, publication_sha='remote-sha'")
            plans = [(f'docs/plans/[{plan_id}] {name}.md', {'id': plan_id, 'draft': True})
                     for plan_id, name in (('G-018', title), ('G-019', 'Later'))]
            notifications.record_planning_publication(connection, self.repo, plans, 'remote-sha')
        def send(_token, _chat, _text):
            with connection:
                connection.execute("UPDATE execplan_drafts SET pending=0 WHERE plan_id='G-019'")
                notifications.enqueue_current_project_events(connection, self.repo)
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=send), 0)
        rows = self.events(connection)
        self.assertEqual(rows[0]['confirmed_pieces'], 1)
        self.assertEqual([item['id'] for item in json.loads(rows[1]['payload'])['plans']], ['G-018', 'G-019'])
        sender = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 0)
        self.assertEqual([item['id'] for item in json.loads(self.events(connection)[2]['payload'])['plans']], ['G-019'])
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=sender), 1)
        self.assertNotIn('G-018', sender.call_args.args[2])

    def test_publication_current_and_waiting_share_one_sorted_list(self):
        connection = self.connection()
        with connection:
            self.draft(connection, "G-019", "검증 🙂 정책")
            self.draft(connection)
            notifications.record_planning_publication(connection, self.repo, [
                ("second", {"id": "G-019", "title": "검증 🙂 정책"}),
                ("first", {"id": "G-018", "title": "보존 정책"}),
                ("ordinary", {"id": "G-020", "title": "구현"}),
            ], "publication-sha")
            notifications.enqueue_current_project_events(connection, self.repo)
            notifications.enqueue_waiting_reason(connection, self.repo, work_exhaustion.WorkReasonSnapshot(
                "cycle", ({"id": "G-018", "title": "보존 정책", "status": "DRAFT",
                           "reason": "사용자 판단 대기", "question": "Question: Choose?"},), (), None, None))
        rows = self.events(connection)
        self.assertEqual([row["event_type"] for row in rows],
                         ["draft_decisions"])
        payload = json.loads(rows[0]["payload"])
        self.assertEqual([plan["id"] for plan in payload["plans"]], ["G-018", "G-019"])
        self.assertFalse(payload["silent"])
        message = notifications._message("draft_decisions", payload)
        self.assertIn("[alpha] draft 작업 2건 · 사용자 판단 대기\n\n• G-018 · 보존 정책\n• G-019 · 검증 🙂 정책", message)
        self.assertIn("각 작업 문서에서 질문을 확인", message)
        self.assertNotIn("Which storage policy", message)
        self.assertNotIn("https://", message)
        self.assertEqual(connection.execute("SELECT generation FROM work_reason_episodes").fetchone()[0], 1)

    def test_disabled_and_empty_lists_do_not_create_notices(self):
        connection = self.connection()
        with connection:
            notifications.enqueue_draft_decisions(connection, self.repo)
        self.assertEqual(self.events(connection), [])
        notifications.configure_session(connection, enabled=False)
        with connection:
            self.draft(connection)
            notifications.enqueue_draft_decisions(connection, self.repo)
        self.assertEqual(self.events(connection), [])

    def test_restart_and_reobservation_do_not_duplicate_list(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "drafts.db"
            connection = self.connection(database=database)
            with connection:
                self.draft(connection)
                notifications.enqueue_draft_decisions(connection, self.repo)
            sent = mock.Mock()
            self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)
            connection.close()
            restarted = sqlite3.connect(database)
            self.addCleanup(restarted.close)
            restarted.row_factory = sqlite3.Row
            notifications.configure_session(restarted, enabled=True)
            with restarted:
                notifications.enqueue_current_project_events(restarted, self.repo)
            self.assertEqual(notifications.dispatch_pending(restarted, "token", "chat", adapter=sent), 0)
            self.assertEqual(sent.call_count, 1)
            restarted.close()

    def test_changes_cancel_old_lists_and_returning_subset_is_delivered_once(self):
        connection = self.connection()
        with connection:
            self.draft(connection)
            notifications.enqueue_draft_decisions(connection, self.repo)
            self.draft(connection, "G-019", "검증 정책")
            notifications.enqueue_draft_decisions(connection, self.repo)
            connection.execute("UPDATE execplan_drafts SET pending=0 WHERE plan_id='G-019'")
            notifications.enqueue_draft_decisions(connection, self.repo)
        sent = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)
        self.assertIn("draft 작업 1건", sent.call_args.args[2])
        self.assertEqual(sum(row["canceled_at"] is not None for row in self.events(connection)), 2)
        with connection:
            self.draft(connection, revision="new-revision")
            notifications.enqueue_draft_decisions(connection, self.repo)
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)

    def test_link_change_refreshes_unsent_snapshot_without_reidentifying_question(self):
        connection = self.connection()
        with connection:
            self.draft(connection, remote_revision="old-sha")
            notifications.enqueue_draft_decisions(connection, self.repo)
            old = self.events(connection)[0]
            self.draft(connection, remote_revision="current-sha")
        sent = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 0)
        sent.assert_not_called()
        self.assertIsNotNone(self.events(connection)[0]['canceled_at'])
        with connection:
            notifications.enqueue_draft_decisions(connection, self.repo)
        rows = self.events(connection)
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]['canceled_at'])
        self.assertEqual(json.loads(rows[1]['payload'])['plans'][0]['decision_identity'], 'question-G-018')
        self.assertEqual(json.loads(rows[1]['payload'])['plans'][0]['decision_revision'], 'revision')
        self.assertIn('/blob/current-sha/', json.loads(rows[1]['payload'])['plans'][0]['url'])
        self.assertEqual(old['confirmed_pieces'], 0)
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)
        self.assertEqual(len(self.events(connection)), 2)

    def test_partial_list_link_change_keeps_confirmed_question_and_refreshes_remaining_one(self):
        connection = self.connection()
        with connection:
            self.draft(connection, title="Large title " * 500, remote_revision="old-sha")
            self.draft(connection, "G-019", "Remaining question", remote_revision="old-sha")
            notifications.enqueue_draft_decisions(connection, self.repo)
        old = self.events(connection)[0]
        calls = []

        def send(*args):
            calls.append(args[2])
            if len(calls) == 1:
                with connection:
                    connection.execute(
                        "UPDATE exec_plans SET current_revision_sha='current-sha' WHERE repo=?",
                        (self.repo,),
                    )

        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=send), 0)
        canceled = self.events(connection)[0]
        self.assertEqual(canceled['confirmed_pieces'], 1)
        self.assertIsNotNone(canceled['canceled_at'])
        self.assertEqual(canceled['rendered_text'], notifications._message(
            "draft_decisions", json.loads(old['payload']),
        ))
        with connection:
            notifications.enqueue_draft_decisions(connection, self.repo)
        rows = self.events(connection)
        current = json.loads(rows[-1]['payload'])
        self.assertEqual([item['id'] for item in current['plans']], ['G-019'])
        self.assertIn('/blob/current-sha/', current['plans'][0]['url'])
        sent = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)
        self.assertEqual(len(sent.call_args_list), 1)
        self.assertIn('G-019', sent.call_args.args[2])
        self.assertNotIn('G-018', sent.call_args.args[2])

    def test_id_at_confirmed_piece_end_is_not_resent_after_link_change(self):
        connection = self.connection()
        probe = notifications._message("draft_decisions", {
            "project": "alpha",
            "plans": [{"id": "G-018", "title": "x"},
                      {"id": "G-019", "title": "Remaining question"}],
            "status": "사용자 판단 대기",
        })
        target_id_end = probe.index("\n• G-019 · ") + len("\n• G-019")
        first_title = "x" * (1 + notifications.MESSAGE_LIMIT - target_id_end)
        with connection:
            self.draft(connection, title=first_title, remote_revision="old-sha")
            self.draft(connection, "G-019", "Remaining question", remote_revision="old-sha")
            notifications.enqueue_draft_decisions(connection, self.repo)
        old = self.events(connection)[0]
        frozen = notifications._message("draft_decisions", json.loads(old["payload"]))
        self.assertTrue(notifications._pieces(frozen)[0].endswith("G-019"))
        self.assertTrue(frozen[notifications.MESSAGE_LIMIT:].startswith(" · "))

        calls = []
        def send(*args):
            calls.append(args[2])
            if len(calls) == 1:
                with connection:
                    connection.execute(
                        "UPDATE exec_plans SET current_revision_sha='current-sha' WHERE repo=?",
                        (self.repo,),
                    )

        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=send), 0)
        canceled = self.events(connection)[0]
        self.assertEqual(canceled["confirmed_pieces"], 1)
        self.assertIsNotNone(canceled["canceled_at"])
        self.assertEqual(len(calls), 1)
        with connection:
            notifications.enqueue_draft_decisions(connection, self.repo)
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=send), 0)
        self.assertEqual(len(calls), 1)

    def test_fully_confirmed_question_is_not_resent_when_link_changes(self):
        connection = self.connection()
        with connection:
            self.draft(connection, remote_revision="old-sha")
            notifications.enqueue_draft_decisions(connection, self.repo)
        sent = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)
        with connection:
            self.draft(connection, remote_revision="current-sha")
            notifications.enqueue_draft_decisions(connection, self.repo)
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 0)
        self.assertEqual(sent.call_count, 1)

    def test_resolved_or_changed_evidence_cancels_unsent_list(self):
        for change in ("resolved", "revision", "identity", "added"):
            with self.subTest(change=change):
                connection = self.connection()
                with connection:
                    self.draft(connection)
                    notifications.enqueue_draft_decisions(connection, self.repo)
                    if change == "resolved":
                        connection.execute("UPDATE execplan_drafts SET pending=0")
                    elif change == "added":
                        self.draft(connection, "G-019", "검증 정책")
                    else:
                        connection.execute("UPDATE execplan_drafts SET evidence=json_set(evidence, ?, 'changed')",
                                           ("$." + change,))
                sent = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 0)
                sent.assert_not_called()
                self.assertIsNotNone(self.events(connection)[0]["canceled_at"])

    def test_split_retry_keeps_text_and_utf16_id_links(self):
        from loop_manager_internal.notifications import delivery
        connection = self.connection()
        with connection:
            self.draft(connection, title="한글 🙂 " * 900)
            self.draft(connection, "G-019", "연결 없는 작업", url=False)
            self.draft(connection, "G-020", "다른 문서")
            notifications.enqueue_draft_decisions(connection, self.repo)
        calls = []
        def send(*args, **kwargs):
            calls.append((args[2], kwargs))
            if len(calls) == 2:
                raise RuntimeError("network failure")
        with mock.patch.object(delivery, "send_telegram", side_effect=send):
            notifications.dispatch_pending(connection, "token", "chat", adapter=delivery.send_telegram)
        self.assertEqual(self.events(connection)[0]["confirmed_pieces"], 1)
        original = self.events(connection)[0]["rendered_text"]
        with mock.patch.object(delivery, "send_telegram", side_effect=send):
            self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=delivery.send_telegram), 1)
        self.assertEqual(calls[1], calls[2])
        successful = [calls[0], *calls[2:]]
        self.assertEqual("".join(text for text, _ in successful), original)
        links = []
        for text, kwargs in successful:
            self.assertFalse(kwargs["disable_notification"])
            for entity in kwargs.get("entities", []):
                raw = text.encode("utf-16-le")
                linked_id = raw[entity["offset"] * 2:(entity["offset"] + entity["length"]) * 2].decode("utf-16-le")
                links.append((linked_id, entity["url"]))
        expected_links = []
        for plan_id in ("G-018", "G-020"):
            saved = json.loads(connection.execute(
                "SELECT evidence FROM execplan_drafts WHERE repo=? AND plan_id=?",
                (self.repo, plan_id),
            ).fetchone()[0])
            expected_links.append((plan_id, saved["url"]))
        self.assertEqual(links, expected_links)

    def test_link_crossing_piece_boundary_keeps_both_id_fragments(self):
        from loop_manager_internal.notifications import rendering
        prefix = "프로젝트 🙂\n• G-018 · "
        text = prefix + "가" * (4091 - len(prefix)) + "\n• G-019 · 두 번째"
        payload = {"plans": [{"id": "G-019", "url": "https://example.test/second"}]}
        fragments = []
        for start in (0, 4096):
            piece = text[start:start + 4096]
            for entity in rendering._draft_entities(payload, text, start, start + 4096):
                raw = piece.encode("utf-16-le")
                fragments.append(raw[entity["offset"] * 2:
                                     (entity["offset"] + entity["length"]) * 2].decode("utf-16-le"))
                self.assertEqual(entity["url"], "https://example.test/second")
        self.assertEqual(fragments, ["G-", "019"])

    def test_resolution_during_split_list_cancels_only_remaining_pieces(self):
        from loop_manager_internal.storage import execplan_draft_state
        connection = self.connection()
        with connection:
            self.draft(connection, title="Large title " * 650)
            self.draft(connection, "G-019", "Remaining question")
            notifications.enqueue_draft_decisions(connection, self.repo)
        original = self.events(connection)[0]
        calls = []
        def send(*args):
            calls.append(args[2])
            with connection:
                execplan_draft_state.cancel(connection, self.repo, "G-018", "resolved")
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=send), 0)
        self.assertEqual(len(calls), 1)
        canceled = self.events(connection)[0]
        self.assertEqual(canceled['confirmed_pieces'], 1)
        self.assertIsNotNone(canceled['canceled_at'])
        self.assertEqual(canceled['payload'], original['payload'])
        with connection:
            notifications.enqueue_draft_decisions(connection, self.repo)
        sent = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)
        self.assertIn('G-019', sent.call_args.args[2])
        self.assertEqual(self.events(connection)[0]['confirmed_pieces'], 1)

    def test_legacy_split_retry_preserves_payload_text_and_cursor(self):
        connection = self.connection()
        question = 'Question: ' + 'Large legacy question ' * 500
        with connection:
            self.draft(connection)
            saved = json.loads(connection.execute(
                "SELECT evidence FROM execplan_drafts WHERE repo=? AND plan_id='G-018'", (self.repo,),
            ).fetchone()[0])
            saved['question'] = question
            connection.execute("UPDATE execplan_drafts SET evidence=? WHERE repo=? AND plan_id='G-018'",
                               (json.dumps(saved), self.repo))
            notifications.enqueue_progress(connection, self.repo, 'draft_decision', 'legacy-split',
                execplan_id='G-018', question=question, url=saved['url'],
                decision_identity=saved['identity'], decision_revision=saved['revision'],
                source_path=saved['path'], silent=False)
        original = self.events(connection)[0]
        calls = []
        def send(*args):
            calls.append(args[2])
            if len(calls) == 2:
                raise RuntimeError('Fixture retry boundary')
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=send), 0)
        paused = self.events(connection)[0]
        self.assertEqual(paused['confirmed_pieces'], 1)
        with connection:
            notifications.enqueue_draft_decisions(connection, self.repo)
        sent = mock.Mock()
        notifications.dispatch_pending(connection, 'token', 'chat', adapter=sent)
        self.assertEqual(sent.call_args_list[0].args[2], calls[1])
        completed = self.events(connection)[0]
        self.assertEqual(completed['payload'], original['payload'])
        self.assertEqual(completed['rendered_text'], paused['rendered_text'])
        self.assertIsNotNone(completed['sent_at'])
        self.assertIsNone(completed['canceled_at'])

    def test_legacy_individual_notice_keeps_original_payload_and_text(self):
        connection = self.connection()
        with connection:
            self.draft(connection)
            saved = json.loads(connection.execute(
                "SELECT evidence FROM execplan_drafts WHERE repo=? AND plan_id='G-018'", (self.repo,),
            ).fetchone()[0])
            notifications.enqueue_progress(connection, self.repo, "draft_decision", "legacy",
                execplan_id="G-018", question=saved['question'], url=saved['url'],
                decision_identity=saved['identity'], decision_revision=saved['revision'],
                source_path=saved['path'], status="사용자 판단 대기", silent=False)
        payload = self.events(connection)[0]["payload"]
        original = notifications._message("draft_decision", json.loads(payload))
        sent = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)
        self.assertEqual(sent.call_args.args[2], original)
        self.assertEqual(self.events(connection)[0]["payload"], payload)

    def test_legacy_notice_without_current_identity_or_source_is_canceled(self):
        connection = self.connection()
        with connection:
            self.draft(connection)
            base = {
                "project": "alpha", "execplan_id": "G-018",
                "question": "Question: Which storage policy?", "decision_revision": "revision",
                "silent": False,
            }
            notifications._insert(connection, "legacy-unlinked", self.repo, "draft_decision", {
                **base, "url": "https://example.test/old",
            })
            notifications._insert(connection, "legacy-wrong-source", self.repo, "draft_decision", {
                **base, "url": "https://github.com/org/repo/blob/remote-sha/docs/plans/%5BG-018%5D%20보존%20정책.md",
                "decision_identity": "question-G-018", "source_path": "docs/plans/[G-019] 다른 계획.md",
            })
            notifications.enqueue_draft_decisions(connection, self.repo)
        legacy = {row['event_key']: row for row in self.events(connection)}
        sent = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, "token", "chat", adapter=sent), 1)
        for key in ('legacy-unlinked', 'legacy-wrong-source'):
            row = next(row for row in self.events(connection) if row['event_key'] == key)
            self.assertIsNotNone(row['canceled_at'])
            self.assertEqual(row['payload'], legacy[key]['payload'])
        self.assertIn("draft 작업 1건", sent.call_args.args[2])


class TelegramEntityTransportTest(unittest.TestCase):
    def test_text_link_entities_are_sent_without_markup(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        entities = [{"type": "text_link", "offset": 0, "length": 5, "url": "https://example.test/plan"}]
        with mock.patch.object(transport, "urlopen", return_value=response) as open_url:
            transport.send_telegram("token", "chat", "G-018 · 작업", entities=entities)
        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(payload["entities"], entities)
        self.assertEqual(payload["text"], "G-018 · 작업")


class CurrentActionPolicyTest(NotificationFixture, unittest.TestCase):
    repo = '/projects/alpha'

    def test_same_status_needs_concrete_current_question_before_one_notice(self):
        connection = self.connection()
        with connection:
            scoped_blocking.record_plan_block(connection, self.repo, 'G-018',
                scoped_blocking.BlockReasonCode.USER_DECISION_REQUIRED,
                'Permission denied; retry budget exhausted', 'unconfirmed')
            checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            checkpoints.finish_unpublished(checkpoints.get(self.repo, 'G-018'), 'local-only', retryable=False)
            notifications.enqueue_current_project_events(connection, self.repo)
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
        adapter.assert_not_called()
        self.assertEqual(self.events(connection), [])
        with connection:
            detail = self.decision_block(connection)
            notifications.enqueue_current_project_events(connection, self.repo)
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
        adapter.assert_called_once()
        self.assertIn('Which permission should be granted?', adapter.call_args.args[2])
        self.assertIn(detail, adapter.call_args.args[2])
        self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'PLAN_BLOCKED')

    def test_final_block_and_user_targets_do_not_prove_user_action(self):
        from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
        from loop_manager_internal.recovery.recovery_runtime import RecoveryAction
        for reason, block_reason in (
            (RecoveryReason.VERIFY_EXHAUSTED, 'verify_retry_exhausted'),
            (RecoveryReason.REVIEW_EXHAUSTED, 'review_retry_exhausted'),
            (RecoveryReason.WORKER_OUTPUT_EXHAUSTED, 'worker_output_invalid'),
            (RecoveryReason.PLAN_INPUT_INVALID, 'plan_input_invalid'),
            (RecoveryReason.STATE_UNSAFE, 'repository_state_unsafe'),
        ):
            with self.subTest(reason=reason):
                connection = self.connection()
                with connection:
                    record = RecoveryRepository(connection).observe(RecoveryObservation(
                        reason, self.repo, 'G-018', 'WORKING', '', '', 'failure', 'head',
                        'CPU/DB/Git/resource/usage wait or next-run correction remains possible',
                        user_targets=('docs/PRODUCT.md',)), attempts=3)
                    self.assertEqual(record.decision.action, RecoveryAction.FINAL_BLOCK)
                    if block_reason == 'repository_state_unsafe':
                        connection.execute("INSERT INTO project_blocks (repo, plan_id, scope, reason_code, evidence) VALUES (?, 'G-018', 'project', ?, ?)",
                            (self.repo, block_reason, record.decision.observation.detail))
                    else:
                        RuntimeStateRepository(connection).block_plan(self.repo, 'G-018',
                            reason_code=block_reason, evidence=record.decision.observation.detail,
                            release_fingerprint='failure', enqueue=False)
                        checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
                        checkpoints.finish_unpublished(checkpoints.get(self.repo, 'G-018'), 'local-only', retryable=False)
                    notifications.enqueue_current_project_events(connection, self.repo)
                adapter = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
                adapter.assert_not_called()
                self.assertEqual(self.events(connection), [])
                self.assertEqual(connection.execute('SELECT attempts FROM recovery_records').fetchone()[0], 3)

    def test_pending_recovery_cancels_partial_question_without_rewriting_audit(self):
        connection = self.connection()
        with connection:
            self.decision_block(connection)
            connection.execute("UPDATE notification_outbox SET rendered_text=?, confirmed_pieces=1, attempts=2, last_error='original failure'", ('frozen ' * 1000,))
            before = dict(self.events(connection)[0])
            RecoveryRepository(connection).observe(RecoveryObservation(
                RecoveryReason.GIT_LOCK, self.repo, 'G-018', 'WORKING', '', '', 'lock', 'head', 'lock busy'), attempts=3)
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
        adapter.assert_not_called()
        after = dict(self.events(connection)[0])
        self.assertIsNotNone(after['canceled_at'])
        for key in before.keys() - {'canceled_at', 'delivery_started_at'}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(connection.execute('SELECT pending_action FROM recovery_records').fetchone()[0], 'pause')
        self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'PLAN_BLOCKED')

    def test_reconfirmed_canceled_action_gets_new_event_without_rewriting_old_delivery(self):
        from loop_manager_internal.notifications.rendering import _message, _pieces
        for cursor, sent in ((0, False), (1, False), (0, True)):
            with self.subTest(cursor=cursor, sent=sent):
                directory = self.enterContext(tempfile.TemporaryDirectory())
                database = Path(directory) / 'queue.sqlite'
                connection = self.connection(database=database)
                self.addCleanup(connection.close)
                with connection:
                    self.decision_block(connection, question='q' * 1900, problem='p' * 1900, proposal='r' * 1900)
                    first = dict(self.events(connection)[0])
                    frozen = _message('blocked', json.loads(first['payload']))
                    pieces = _pieces(frozen)
                    self.assertGreater(len(pieces), 1)
                    confirmed = len(pieces) if sent else cursor
                    connection.execute("UPDATE notification_outbox SET rendered_text=?, confirmed_pieces=?, "
                        "attempts=2, last_error='original transport error', "
                        "sent_at=CASE WHEN ? THEN CURRENT_TIMESTAMP END", (frozen, confirmed, sent))
                    recovery = dict(connection.execute('SELECT * FROM recovery_records').fetchone())
                    RecoveryRepository(connection).observe(RecoveryObservation(
                        RecoveryReason.GIT_LOCK, self.repo, 'G-018', recovery['phase'],
                        recovery['plan_revision'], recovery['base_revision'], 'lock',
                        recovery['progress_marker'], 'lock busy'), attempts=3)
                adapter = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
                old = dict(self.events(connection)[0])
                adapter.assert_not_called()
                if not sent:
                    self.assertIsNotNone(old['canceled_at'])
                connection.close()
                connection = sqlite3.connect(database)
                self.addCleanup(connection.close)
                connection.row_factory = sqlite3.Row
                with connection:
                    notifications.configure_session(connection, enabled=True)
                    self.decision_block(connection, question='q' * 1900, problem='p' * 1900, proposal='r' * 1900)
                    notifications.enqueue_current_project_events(connection, self.repo)
                    notifications.enqueue_current_project_events(connection, self.repo)
                self.assertEqual(connection.execute('SELECT identity FROM recovery_records').fetchone()[0], recovery['identity'])
                self.assertEqual(len(self.events(connection)), 1 if sent else 2)
                self.assertEqual(dict(connection.execute('SELECT * FROM notification_outbox WHERE event_key=?',
                                                        (first['event_key'],)).fetchone()), old)
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0 if sent else 1)
                self.assertEqual(adapter.call_count, 0 if sent else len(pieces) - cursor)
                if not sent:
                    self.assertEqual([call.args[2] for call in adapter.call_args_list], pieces[cursor:])
                with connection:
                    notifications.enqueue_current_project_events(connection, self.repo)
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
                self.assertEqual(len(self.events(connection)), 1 if sent else 2)

    def test_guidance_action_survives_other_scope_recovery_and_project_release(self):
        for previously_sent in (False, True, None):
            with self.subTest(previously_sent=previously_sent):
                connection = self.connection(enabled=previously_sent is not None)
                proposal_id = 'legacy-rule'
                with connection:
                    connection.execute('INSERT INTO guidance_approvals '
                        '(repo, plan_id, proposal_id, proposal_hash, target_path, scope, proposal_patch, '
                        'proposal_patch_hash, proposal_line, decision_line, batch_id, sequence, baseline_content, baseline_mode) '
                        "VALUES (?, 'G-018', ?, ?, ?, ?, ?, ?, ?, ?, 'batch', 0, ?, 33188)",
                        (self.repo, proposal_id, 'legacy-proposal-hash', 'AGENTS.md', 'project',
                         'legacy proposal patch', 'legacy-patch-hash', 1, 2, b'# Agents\n'))
                    scoped_blocking.record_plan_block(connection, self.repo, 'G-018',
                        scoped_blocking.BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED, 'approval is pending', 'guidance-input')
                    checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
                    checkpoints.finish_unpublished(checkpoints.get(self.repo, 'G-018'), 'local-only', retryable=False)
                adapter = mock.Mock()
                if previously_sent:
                    self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
                original = dict(self.events(connection)[0]) if self.events(connection) else None
                approval = dict(connection.execute('SELECT * FROM guidance_approvals').fetchone())
                with connection:
                    scoped_blocking.record_project_block(connection, self.repo,
                        scoped_blocking.BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                        'independent project safety observation', 'safety-input', plan_id='G-018', previous_phase=None)
                    notifications.enqueue_current_project_events(connection, self.repo)
                self.assertEqual([dict(row) for row in self.events(connection)], [original] if original else [])
                with connection:
                    scoped_blocking.clear_project_block(connection, self.repo)
                    notifications.configure_session(connection, enabled=True)
                    notifications.enqueue_current_project_events(connection, self.repo)
                    notifications.enqueue_current_project_events(connection, self.repo)
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0 if previously_sent else 1)
                adapter.assert_called_once()
                self.assertEqual(len(self.events(connection)), 1)
                self.assertEqual(dict(connection.execute('SELECT * FROM guidance_approvals').fetchone()), approval)
                self.assertEqual(connection.execute('SELECT reason_code FROM execplan_blocks').fetchone()[0], 'guidance_approval_required')
                self.assertEqual(connection.execute('SELECT reason_code FROM recovery_records').fetchone()[0], 'state_unsafe')
                self.assertIsNone(connection.execute('SELECT * FROM project_blocks').fetchone())

    def test_late_confirmation_updates_only_continued_versions_of_same_action(self):
        from loop_manager_internal.notifications.rendering import _message, _pieces
        for long_question, changed in ((False, False), (True, False), (True, True)):
            with self.subTest(long_question=long_question, changed=changed):
                connection = self.connection()
                question = 'q' * 1900 if long_question else 'Which permission?'
                with connection:
                    self.decision_block(connection, question=question)
                first = dict(self.events(connection)[0])
                frozen = _message('blocked', json.loads(first['payload']))
                original_pieces = _pieces(frozen)
                recovery = dict(connection.execute('SELECT * FROM recovery_records').fetchone())
                def reconfirm_while_http_is_inflight(_token, _chat, piece):
                    self.assertEqual(piece, original_pieces[0])
                    for _ in range(2):
                        with connection:
                            RecoveryRepository(connection).observe(RecoveryObservation(
                                RecoveryReason.GIT_LOCK, self.repo, 'G-018', recovery['phase'],
                                recovery['plan_revision'], recovery['base_revision'], 'lock',
                                recovery['progress_marker'], 'lock busy'), attempts=3)
                            notifications.enqueue_current_project_events(connection, self.repo)
                            self.decision_block(connection, question='A different question?' if changed else question)
                    current = connection.execute('SELECT o.* FROM notified_block_incidents i '
                        'JOIN notification_outbox o ON o.event_key=i.event_key').fetchone()
                    self.assertEqual(current['confirmed_pieces'], 0)
                notifications.dispatch_pending(connection, 'token', 'chat', adapter=reconfirm_while_http_is_inflight)
                old = connection.execute('SELECT * FROM notification_outbox WHERE event_key=?', (first['event_key'],)).fetchone()
                self.assertEqual(old['confirmed_pieces'], 1)
                self.assertIsNotNone(old['canceled_at'])
                self.assertEqual(old['payload'], first['payload'])
                current = connection.execute('SELECT o.* FROM notified_block_incidents i '
                    'JOIN notification_outbox o ON o.event_key=i.event_key').fetchone()
                self.assertEqual(current['confirmed_pieces'], 0 if changed else 1)
                adapter = mock.Mock()
                notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter)
                if changed:
                    adapter.assert_called_once()
                    self.assertIn('A different question?', adapter.call_args.args[2])
                else:
                    self.assertEqual([call.args[2] for call in adapter.call_args_list], original_pieces[1:])
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)

    def test_worker_question_survives_project_recovery_without_restoring_shared_record(self):
        for initially_enabled, already_sent, changed_revision in ((True, False, False), (True, True, False),
                                                                (False, False, False), (True, False, True)):
            with self.subTest(enabled=initially_enabled, sent=already_sent, revision=changed_revision):
                directory = self.enterContext(tempfile.TemporaryDirectory())
                database = Path(directory) / 'scope.sqlite'
                connection = self.connection(enabled=initially_enabled, database=database)
                self.addCleanup(connection.close)
                with connection:
                    connection.execute("UPDATE exec_plans SET current_revision_sha='question-revision'")
                    self.decision_block(connection)
                original_count = len(self.events(connection))
                adapter = mock.Mock()
                if already_sent:
                    self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
                with connection:
                    scoped_blocking.record_project_block(connection, self.repo,
                        scoped_blocking.BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                        'independent project safety observation', 'safety-input', plan_id='G-018', previous_phase=None)
                    notifications.enqueue_current_project_events(connection, self.repo)
                    project_block = scoped_blocking.project_block(connection, self.repo)
                    RecoveryRepository(connection).resolve_rebase_block(self.repo, 'G-018', project_block)
                    self.assertEqual(RecoveryRepository(connection).row(self.repo, 'G-018')['pending_action'],
                                     'reconcile')
                    scoped_blocking.clear_project_block(connection, self.repo)
                connection.close()
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                self.addCleanup(connection.close)
                with connection:
                    notifications.configure_session(connection, enabled=True)
                    if changed_revision:
                        connection.execute("UPDATE exec_plans SET current_revision_sha='replacement-revision'")
                    notifications.enqueue_current_project_events(connection, self.repo)
                    notifications.enqueue_current_project_events(connection, self.repo)
                expected = not already_sent and not changed_revision
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), int(expected))
                self.assertEqual(adapter.call_count, 1 if already_sent or expected else 0)
                if expected:
                    self.assertIn('Which permission should be granted?', adapter.call_args.args[2])
                self.assertEqual(len(self.events(connection)), original_count or 1)
                self.assertEqual(connection.execute('SELECT reason_code FROM recovery_records').fetchone()[0], 'state_unsafe')
                self.assertEqual(RecoveryRepository(connection).row(self.repo, 'G-018')['pending_action'], 'reconcile')
                block = connection.execute('SELECT * FROM execplan_blocks').fetchone()
                self.assertEqual(block['reason_code'], 'user_decision_required')
                self.assertEqual(json.loads(block['recovery_snapshot'])['reason_code'], 'user_decision')
                self.assertIsNone(connection.execute('SELECT * FROM project_blocks').fetchone())

    def test_old_block_schema_adds_nullable_proof_without_changing_existing_rows(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(scoped_blocking.PLAN_BLOCKS_SCHEMA.replace('    recovery_snapshot TEXT,\n', ''))
        connection.execute("INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
                           "VALUES ('repo', 'G-001', 'plan', 'user_decision_required', 'saved cause')")
        original = dict(connection.execute('SELECT * FROM execplan_blocks').fetchone())
        for _ in range(2):
            scoped_blocking.ensure_schema(connection)
        migrated = dict(connection.execute('SELECT * FROM execplan_blocks').fetchone())
        self.assertIsNone(migrated.pop('recovery_snapshot'))
        self.assertEqual(migrated, original)

    def test_next_piece_rechecks_resolved_action_and_confirmation_never_recreates_notice(self):
        connection = self.connection()
        with connection:
            self.decision_block(connection, question='q' * 1900, problem='p' * 1900, proposal='r' * 1900)
        original = dict(self.events(connection)[0])
        calls = []
        def clear_during_send(_token, _chat, text):
            self.assertFalse(connection.in_transaction)
            calls.append(text)
            with connection:
                scoped_blocking.clear_plan_block(connection, self.repo, 'G-018')
                notifications.record_process_start(connection, self.repo, 'worker', 'process', plan_id='G-018')
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=clear_during_send), 0)
        self.assertEqual(len(calls), 1)
        row = self.events(connection)[0]
        self.assertEqual(row['confirmed_pieces'], 1)
        self.assertEqual(row['attempts'], 1)
        self.assertIsNotNone(row['canceled_at'])
        self.assertEqual(row['payload'], original['payload'])
        self.assertIsNotNone(row['rendered_text'])
        with connection:
            notifications.recover_interrupted_starts(connection)
        self.assertEqual(len(self.events(connection)), 1)
        self.assertEqual(connection.execute('SELECT start_identity FROM notified_block_incidents').fetchone()[0], 'process')

    def test_latest_question_replaces_pending_action_in_same_incident(self):
        connection = self.connection()
        with connection:
            self.decision_block(connection, question='Choose permission A?')
        first = dict(self.events(connection)[0])
        with connection:
            self.decision_block(connection, question='Choose permission B?')
        rows = self.events(connection)
        self.assertEqual(len(rows), 2)
        old = next(row for row in rows if row['event_key'] == first['event_key'])
        self.assertIsNotNone(old['canceled_at'])
        self.assertEqual(old['payload'], first['payload'])
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
        adapter.assert_called_once()
        self.assertIn('Choose permission B?', adapter.call_args.args[2])
        self.assertNotIn('Choose permission A?', adapter.call_args.args[2])
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM notified_block_incidents').fetchone()[0], 1)

    def test_changed_question_after_confirmation_is_not_regenerated_from_first_incident_payload(self):
        connection = self.connection()
        with connection:
            self.decision_block(connection, question='Choose permission A?',
                                detail='permission choice', fingerprint='permission')
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
        original = connection.execute('SELECT payload FROM notified_block_incidents').fetchone()[0]
        with connection:
            self.decision_block(connection, question='Choose permission B?',
                                detail='permission choice', fingerprint='permission')
            notifications.enqueue_current_project_events(connection, self.repo)
            notifications.enqueue_current_project_events(connection, self.repo)
        self.assertEqual(len(self.events(connection)), 2)
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
        with connection:
            notifications.enqueue_current_project_events(connection, self.repo)
        self.assertEqual(len(self.events(connection)), 2)
        self.assertEqual(adapter.call_count, 2)
        self.assertIn('Choose permission B?', adapter.call_args.args[2])
        self.assertEqual(connection.execute('SELECT payload FROM notified_block_incidents').fetchone()[0], original)

    def test_old_planning_partial_queue_is_canceled_without_incident_reactivation(self):
        connection = self.connection()
        payload = {'project': 'alpha', 'stage': 'planning', 'scope': 'project',
                   'reason_code': 'planning_error', 'cause': 'old planning failure', 'execplan_id': None}
        key = notifications._key('blocked', self.repo, 'project\0planning_error\0')
        with connection:
            connection.execute("UPDATE projects SET planning_error='old planning failure'")
            notifications._insert(connection, key, self.repo, 'blocked', payload)
            notifications.adopt_legacy_block_notices(connection)
            connection.execute("UPDATE notification_outbox SET rendered_text='frozen', confirmed_pieces=1, attempts=2, last_error='old error', canceled_at=CURRENT_TIMESTAMP")
            connection.execute('UPDATE notified_block_incidents SET released_at=CURRENT_TIMESTAMP')
            before = dict(self.events(connection)[0])
            incident = dict(connection.execute('SELECT * FROM notified_block_incidents').fetchone())
            for _ in range(2):
                notifications.enqueue_current_project_events(connection, self.repo)
                notifications.recover_interrupted_starts(connection)
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
        adapter.assert_not_called()
        self.assertEqual(dict(self.events(connection)[0]), before)
        self.assertEqual(dict(connection.execute('SELECT * FROM notified_block_incidents').fetchone()), incident)
        self.assertEqual(connection.execute('SELECT planning_error FROM projects').fetchone()[0], 'old planning failure')

    def test_pr_claim_requires_current_merge_wait_for_same_pr(self):
        connection = self.connection()
        self.prepare_pr_candidate(connection)
        with connection:
            notifications.enqueue_current_project_events(connection, self.repo)
        before = dict(next(row for row in self.events(connection) if row['event_type'] == 'pr_created'))
        with connection:
            connection.execute("UPDATE exec_plans SET status='MERGED'")
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_new_pr_notifications(connection, 'token', 'chat', self.repo, adapter=adapter), 0)
        adapter.assert_not_called()
        row = next(row for row in self.events(connection) if row['event_key'] == before['event_key'])
        self.assertIsNotNone(row['canceled_at'])
        self.assertEqual(row['payload'], before['payload'])
        self.assertEqual(row['attempts'], 0)

    def test_replaced_pr_candidate_cancels_remaining_piece_and_preserves_audit(self):
        connection = self.connection()
        repo = self.repo
        self.prepare_pr_candidate(connection)
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        original = next(row for row in self.events(connection) if row['event_type'] == 'pr_created')
        connection.execute(
            "UPDATE notification_outbox SET rendered_text=?, confirmed_pieces=1, "
            "attempts=2, last_error=? WHERE event_key=?",
            ('confirmed first piece\npending second piece', 'previous send failed', original['event_key']),
        )
        connection.commit()

        replacement = self.prepare_pr_candidate(
            connection, head='c' * 40, reviewed_change_identity='replacement-change',
        )
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
            notifications.enqueue_current_project_events(connection, repo)

        rows = {row['event_key']: row for row in self.events(connection)}
        stale = rows[original['event_key']]
        self.assertIsNotNone(stale['canceled_at'])
        self.assertEqual(stale['payload'], original['payload'])
        self.assertEqual(
            (stale['rendered_text'], stale['confirmed_pieces'], stale['attempts'], stale['last_error']),
            ('confirmed first piece\npending second piece', 1, 2, 'previous send failed'),
        )
        current = next(row for row in rows.values() if row['canceled_at'] is None)
        self.assertNotEqual(current['event_key'], original['event_key'])
        self.assertEqual(json.loads(current['payload'])['candidate_identity'], replacement['candidate_identity'])

        sent = []
        self.assertEqual(notifications.dispatch_pending(
            connection, 'token', 'chat', adapter=lambda _token, _chat, text: sent.append(text),
        ), 1)
        self.assertEqual(len(sent), 1)
        self.assertNotIn('pending second piece', ''.join(sent))

    def test_rebased_pr_candidate_preserves_canceled_payload_and_resumes_remaining_piece(self):
        connection = self.connection()
        repo = self.repo
        candidate = self.prepare_pr_candidate(connection)
        with connection:
            connection.execute(
                "UPDATE exec_plans SET title=? WHERE repo=? AND plan_id='G-018'",
                ('long review title ' * 400, repo),
            )
            notifications.enqueue_current_project_events(connection, repo)
        original = dict(next(row for row in self.events(connection)
                             if row['event_type'] == 'pr_created'))
        original_payload = json.loads(original['payload'])
        frozen = notifications._message('pr_created', original_payload)
        pieces = notifications._pieces(frozen)
        self.assertGreater(len(pieces), 1)
        with connection:
            connection.execute(
                "UPDATE notification_outbox SET rendered_text=?, confirmed_pieces=1, "
                "attempts=2, last_error='previous send uncertainty' WHERE event_key=?",
                (frozen, original['event_key']),
            )
            connection.execute(
                "UPDATE exec_plans SET publication_token='rebase-in-progress' "
                "WHERE repo=? AND plan_id='G-018'", (repo,),
            )
            notifications.enqueue_current_project_events(connection, repo)
        canceled = dict(next(row for row in self.events(connection)
                             if row['event_key'] == original['event_key']))
        self.assertIsNotNone(canceled['canceled_at'])

        rebased = self.prepare_pr_candidate(connection, head='c' * 40)
        self.assertEqual(candidate['candidate_identity'], rebased['candidate_identity'])
        self.assertNotEqual(candidate['candidate_publication_token'],
                            rebased['candidate_publication_token'])
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
            notifications.enqueue_current_project_events(connection, repo)

        rows = {row['event_key']: dict(row) for row in self.events(connection)
                if row['event_type'] == 'pr_created'}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[original['event_key']], canceled)
        continued = next(row for key, row in rows.items() if key != original['event_key'])
        self.assertEqual(continued['event_key'], f"{original['event_key']}:v1")
        self.assertIsNone(continued['canceled_at'])
        self.assertEqual(continued['rendered_text'], frozen)
        self.assertEqual(continued['confirmed_pieces'], 1)
        self.assertEqual(continued['attempts'], 2)
        self.assertEqual(continued['last_error'], 'previous send uncertainty')
        payload = json.loads(continued['payload'])
        self.assertEqual(payload['continued_from'], original['event_key'])
        self.assertEqual(payload['candidate_identity'], candidate['candidate_identity'])
        self.assertEqual(payload['candidate_head_oid'], rebased['candidate_head_oid'])
        self.assertEqual(payload['candidate_publication_token'],
                         rebased['candidate_publication_token'])
        self.assertEqual(payload['occurred_at'], original_payload['occurred_at'])

        sent = []
        self.assertEqual(notifications.dispatch_pending(
            connection, 'token', 'chat', adapter=lambda _token, _chat, text: sent.append(text),
        ), 1)
        self.assertEqual(sent, pieces[1:])
        self.assertEqual(dict(next(row for row in self.events(connection)
                                   if row['event_key'] == original['event_key'])), canceled)
        self.assertIsNotNone(connection.execute(
            'SELECT sent_at FROM notification_outbox WHERE event_key=?',
            (continued['event_key'],),
        ).fetchone()[0])
        with connection:
            notifications.enqueue_current_project_events(connection, repo)
        self.assertEqual(len([row for row in self.events(connection)
                             if row['event_type'] == 'pr_created']), 2)

    def test_late_pr_confirmation_advances_only_same_candidate_continuation(self):
        connection = self.connection()
        repo = self.repo
        self.prepare_pr_candidate(connection)
        with connection:
            connection.execute(
                "UPDATE exec_plans SET title=? WHERE repo=? AND plan_id='G-018'",
                ('long review title ' * 400, repo),
            )
            notifications.enqueue_current_project_events(connection, repo)
        original = dict(next(row for row in self.events(connection)
                             if row['event_type'] == 'pr_created'))
        original_payload = original['payload']
        frozen = notifications._message('pr_created', json.loads(original_payload))
        pieces = notifications._pieces(frozen)
        self.assertGreater(len(pieces), 1)

        def rebase_while_send_is_in_flight(_token, _chat, piece):
            self.assertEqual(piece, pieces[0])
            with connection:
                connection.execute(
                    "UPDATE exec_plans SET publication_token='rebase-in-progress' "
                    "WHERE repo=? AND plan_id='G-018'", (repo,),
                )
                notifications.enqueue_current_project_events(connection, repo)
            canceled = connection.execute(
                'SELECT * FROM notification_outbox WHERE event_key=?',
                (original['event_key'],),
            ).fetchone()
            self.assertIsNotNone(canceled['canceled_at'])
            self.assertEqual(canceled['payload'], original_payload)

            self.prepare_pr_candidate(connection, head='c' * 40)
            with connection:
                notifications.enqueue_current_project_events(connection, repo)
            continued = connection.execute(
                'SELECT * FROM notification_outbox WHERE event_key=?',
                (f"{original['event_key']}:v1",),
            ).fetchone()
            self.assertEqual(continued['rendered_text'], frozen)
            self.assertEqual(continued['confirmed_pieces'], 0)

        self.assertEqual(notifications.dispatch_pending(
            connection, 'token', 'chat', adapter=rebase_while_send_is_in_flight,
        ), 0)
        old = connection.execute(
            'SELECT * FROM notification_outbox WHERE event_key=?',
            (original['event_key'],),
        ).fetchone()
        continued = connection.execute(
            'SELECT * FROM notification_outbox WHERE event_key=?',
            (f"{original['event_key']}:v1",),
        ).fetchone()
        self.assertEqual(old['payload'], original_payload)
        self.assertIsNotNone(old['canceled_at'])
        self.assertEqual(old['confirmed_pieces'], 1)
        self.assertEqual(continued['confirmed_pieces'], 1)

        sent = []
        self.assertEqual(notifications.dispatch_pending(
            connection, 'token', 'chat', adapter=lambda _token, _chat, text: sent.append(text),
        ), 1)
        self.assertEqual(sent, pieces[1:])
        self.assertEqual(connection.execute(
            'SELECT confirmed_pieces FROM notification_outbox WHERE event_key=?',
            (continued['event_key'],),
        ).fetchone()[0], len(pieces))

    def test_pr_candidate_lookup_failure_holds_event_without_claiming_piece(self):
        connection = self.connection()
        self.prepare_pr_candidate(connection)
        with connection:
            notifications.enqueue_current_project_events(connection, self.repo)
        original = next(row for row in self.events(connection) if row['event_type'] == 'pr_created')
        adapter = mock.Mock()

        def deny_pr_state(action, table, _column, _database, _trigger):
            if action == sqlite3.SQLITE_READ and table == 'exec_plans':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_pr_state)
        with self.assertRaises(sqlite3.DatabaseError):
            notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter)
        connection.set_authorizer(None)

        adapter.assert_not_called()
        row = next(row for row in self.events(connection) if row['event_key'] == original['event_key'])
        self.assertIsNone(row['canceled_at'])
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['payload'], original['payload'])

    def test_pr_retry_waits_for_successful_observation_and_rechecks_closed_or_replaced(self):
        from types import SimpleNamespace
        from loop_manager_internal.application.project_service import _stale_conflicting_pr_plan_ids
        from loop_manager_internal.recovery import waiting_pr_service

        for outcome in ('lookup_failure', 'closed', 'replaced', 'replaced_conflict', 'still_open'):
            with self.subTest(outcome=outcome):
                connection = self.connection()
                repo = self.repo
                candidate = self.prepare_pr_candidate(connection)
                branch = 'loop-manager/G-018'
                connection.execute(
                    "UPDATE exec_plans SET branch=? WHERE repo=? AND plan_id='G-018'",
                    (branch, repo),
                )
                with connection:
                    notifications.enqueue_current_project_events(connection, repo)
                original = next(row for row in self.events(connection)
                                if row['event_type'] == 'pr_created')
                self.assertEqual(notifications.dispatch_pending(
                    connection, 'token', 'chat', adapter=mock.Mock(
                        side_effect=RuntimeError('temporary Telegram outage'),
                    ),
                ), 0)
                before = next(row for row in self.events(connection)
                              if row['event_key'] == original['event_key'])
                self.assertEqual(before['attempts'], 1)

                sent = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(
                    connection, 'token', 'chat', adapter=sent, exclude_pr=True,
                ), 0)
                with notifications.immediate_delivery(connection, 'token', 'chat', repo), \
                        mock.patch.object(delivery, 'send_telegram', sent):
                    notifications.flush_project_events(connection, repo)
                    sent.assert_not_called()

                    def pr_for_branch(_repo_path, _branch, _url):
                        if outcome == 'lookup_failure':
                            raise RuntimeError('GitHub lookup unavailable')
                        state = 'CLOSED' if outcome == 'closed' else 'OPEN'
                        observed_head = ('c' * 40 if outcome in {'replaced', 'replaced_conflict'}
                                         else candidate['candidate_head_oid'])
                        return {
                            'url': candidate['pr_url'], 'state': state,
                            'headRefName': branch, 'headRefOid': observed_head,
                            'baseRefName': 'main',
                        }

                    closed_effects = waiting_pr_service.BlockClosedPullRequestEffects(
                        _final_block_recovery_decision=lambda *args, **kwargs: None,
                    )
                    observe_effects = waiting_pr_service.ObserveWaitingPullRequestsEffects(
                        _block_closed_pull_request=lambda conn, observed_repo, plan, url, head: \
                            waiting_pr_service._block_closed_pull_request(
                                closed_effects, conn, observed_repo, plan, url, head,
                            ),
                        _recorded_base_fetch=lambda *args, **kwargs: None,
                        _resume_reopened_pull_request=lambda *args, **kwargs: None,
                        classify_pull_request_mergeability=lambda _pr: (
                            'conflicting' if outcome == 'replaced_conflict' else 'mergeable'
                        ),
                        complete_merged_execplan_worktree=lambda *args, **kwargs: None,
                        operation_log=lambda *args, **kwargs: None,
                        operation_print=lambda *args, **kwargs: None,
                        pr_for_branch=pr_for_branch,
                    )
                    synchronize_effects = waiting_pr_service.SynchronizeWaitingPullRequestsEffects(
                        observe_waiting_pull_requests=lambda conn, repo_path, state_dir, observed_repo, **kwargs:
                            waiting_pr_service.observe_waiting_pull_requests(
                                observe_effects, conn, repo_path, state_dir, observed_repo, **kwargs,
                            ),
                        select_waiting_pull_request_conflict=lambda *args, **kwargs:
                            SimpleNamespace(step_result=None),
                    )
                    def after_observation(observation):
                        stale_plan_ids = _stale_conflicting_pr_plan_ids(observation)
                        if stale_plan_ids:
                            with connection:
                                notifications.cancel_pr_review_events_for_plans(
                                    connection, repo, stale_plan_ids,
                                )
                        notifications.flush_observed_pr_events(connection, repo)

                    observe_options = {'after_observation': after_observation}
                    if outcome == 'lookup_failure':
                        with self.assertRaisesRegex(RuntimeError, 'lookup unavailable'):
                            waiting_pr_service.synchronize_waiting_pull_requests(
                                synchronize_effects, connection, Path('/unused'), Path('/state'), repo,
                                **observe_options,
                            )
                    else:
                        waiting_pr_service.synchronize_waiting_pull_requests(
                            synchronize_effects, connection, Path('/unused'), Path('/state'), repo,
                            **observe_options,
                        )

                rows = {row['event_key']: row for row in self.events(connection)}
                current = rows[original['event_key']]
                if outcome == 'still_open':
                    sent.assert_called_once()
                    self.assertIsNotNone(current['sent_at'])
                    self.assertIsNone(current['canceled_at'])
                else:
                    sent.assert_not_called()
                    self.assertIsNone(current['sent_at'])
                    if outcome in {'closed', 'replaced', 'replaced_conflict'}:
                        self.assertIsNotNone(current['canceled_at'])
                    else:
                        self.assertIsNone(current['canceled_at'])
                    if outcome == 'replaced_conflict':
                        stored_head = connection.execute(
                            "SELECT pr_head_oid FROM exec_plans WHERE repo=? AND plan_id='G-018'",
                            (repo,),
                        ).fetchone()[0]
                        self.assertEqual(stored_head, candidate['candidate_head_oid'])
                    self.assertEqual(current['attempts'], 1)
                connection.close()

    def test_deleted_published_branch_requires_restart_until_request_is_recorded(self):
        from loop_manager_internal.recovery.work_branch_integration import require_remote_present
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure
        connection = self.connection()
        with connection:
            connection.execute("UPDATE exec_plans SET branch='loop-manager/G-018', publication_remote_oid=?", ('a' * 40,))
            plan = connection.execute('SELECT * FROM exec_plans').fetchone()
            with self.assertRaises(RecoveryFailure) as failure:
                require_remote_present(connection, self.repo, plan, None, None)
            self.assertEqual(failure.exception.reason, RecoveryReason.REMOTE_BRANCH_DELETED)
            scoped_blocking.record_plan_block(connection, self.repo, 'G-018',
                scoped_blocking.BlockReasonCode.REMOTE_BRANCH_DELETED, str(failure.exception), 'deleted-published-branch')
            checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
            checkpoints.finish_unpublished(checkpoints.get(self.repo, 'G-018'), 'preserved locally', retryable=False)
            notifications.enqueue_current_project_events(connection, self.repo)
        row = self.events(connection)[0]
        self.assertEqual(json.loads(row['payload'])['action']['kind'], 'restart')
        self.assertIn('명시적 재시작', notifications._message('blocked', json.loads(row['payload'])))
        with connection:
            current = dict(connection.execute('SELECT * FROM execplan_blocks').fetchone())
            from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
            store = ExecutionCheckpointRepository(connection)
            saved = store.get(self.repo, 'G-018')
            store.record(self.repo, 'G-018', expected_generation=saved.generation,
                operation=saved.operation, expected_head=saved.expected_head,
                target_base_oid=saved.target_base_oid, observed_remote_oid=saved.observed_remote_oid,
                snapshot={'deleted_branch_restart': {'state': 'complete', 'block': current}})
        adapter = mock.Mock()
        self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
        adapter.assert_not_called()
        self.assertIsNotNone(self.events(connection)[0]['canceled_at'])
        self.assertEqual(self.events(connection)[0]['attempts'], 0)

    def test_invalid_recovery_evidence_and_changed_revision_preserve_unsent_audit(self):
        for mutation in ('not-json', 'null', '[]', 'changed-revision', 'changed-fingerprint'):
            with self.subTest(mutation=mutation):
                connection = self.connection()
                with connection:
                    self.decision_block(connection)
                    before = dict(self.events(connection)[0])
                    if mutation == 'changed-fingerprint':
                        connection.execute("UPDATE recovery_records SET fingerprint='unbound'")
                    elif mutation == 'changed-revision':
                        connection.execute("UPDATE recovery_records SET plan_revision='previous'")
                        connection.execute("UPDATE exec_plans SET current_revision_sha='latest'")
                    else:
                        connection.execute('UPDATE recovery_records SET final_evidence=?', (mutation,))
                adapter = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
                adapter.assert_not_called()
                after = dict(self.events(connection)[0])
                self.assertIsNotNone(after['canceled_at'])
                for key in before.keys() - {'canceled_at', 'delivery_started_at'}:
                    self.assertEqual(after[key], before[key], key)

    def test_closed_pr_and_recorded_verify_limit_expose_only_their_current_action(self):
        for reason, fingerprint, expected_kind in (
            (scoped_blocking.BlockReasonCode.PULL_REQUEST_CLOSED, 'closed-head', 'reopen_pr'),
            (scoped_blocking.BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT, '90', 'verify_timeout'),
        ):
            with self.subTest(reason=reason):
                connection = self.connection()
                with connection:
                    notifications.configure_session(connection, enabled=True, verify_timeout_seconds=90)
                    connection.execute("UPDATE exec_plans SET pr_url='https://example.test/pr/18', verify_result='전체 verify 시간 초과: verify_timeout_seconds=90'")
                    scoped_blocking.record_plan_block(connection, self.repo, 'G-018', reason,
                        'Observed native result requires this specific user action.', fingerprint)
                    checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
                    checkpoints.finish_unpublished(checkpoints.get(self.repo, 'G-018'), 'local-only', retryable=False)
                    notifications.enqueue_current_project_events(connection, self.repo)
                row = self.events(connection)[0]
                payload = json.loads(row['payload'])
                self.assertEqual(payload['action']['kind'], expected_kind)
                adapter = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 1)
                adapter.assert_called_once()
                if expected_kind == 'verify_timeout':
                    self.assertIn('verify_timeout_seconds', adapter.call_args.args[2])
                    self.assertIn('90초보다', adapter.call_args.args[2])
                else:
                    self.assertIn('다시 열어', adapter.call_args.args[2])
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)

    def test_explicit_route_assessment_allows_unrecoverable_stop_without_fake_user_action(self):
        routes = {
            'reobservation': 'The immutable source inventory has been rechecked; no source copy exists.',
            'retry': 'The required original bytes are absent rather than temporarily unavailable.',
            'correction': 'No valid input remains for a correction task.',
            'condition_wait': 'There is no pending resource or condition that can restore those bytes.',
            'next_run': 'The saved source inventory is permanent and unchanged by restarting.',
            'safe_recovery': 'No owned checkpoint, backup or alternate source contains the original bytes.',
        }
        for missing in (None, 'retry', 'next_run'):
            with self.subTest(missing=missing):
                connection = self.connection()
                facts = {key: value for key, value in routes.items() if key != missing}
                with connection:
                    record = RecoveryRepository(connection).observe(RecoveryObservation(
                        RecoveryReason.STATE_UNSAFE, self.repo, None, 'PROJECT_BLOCKED', None, None,
                        'confirmed-source-inventory', 'inventory-v1', 'Required original artifacts are unavailable.',
                        unavailable_recovery_routes=facts), attempts=0 if missing is None else 50)
                    scoped_blocking.record_project_block(connection, self.repo,
                        scoped_blocking.BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                        record.decision.observation.detail, 'confirmed-source-inventory',
                        plan_id=None, previous_phase=None, recovery_decision=record.decision)
                    notifications.enqueue_current_project_events(connection, self.repo)
                    notifications.enqueue_current_project_events(connection, self.repo)
                adapter = mock.Mock()
                expected = 1 if missing is None else 0
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), expected)
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
                if missing is None:
                    adapter.assert_called_once()
                    text = adapter.call_args.args[2]
                    self.assertIn('자동 복구 불가', text)
                    self.assertIn(routes['safe_recovery'], text)
                    self.assertNotIn('사용자 조치', text)
                    self.assertNotIn('Question:', text)
                    self.assertEqual(len(self.events(connection)), 1)
                else:
                    adapter.assert_not_called()
                    self.assertEqual(self.events(connection), [])
                saved = connection.execute('SELECT final_evidence FROM recovery_records').fetchone()[0]
                self.assertEqual(json.loads(saved)['unavailable_recovery_routes'], facts)
                self.assertEqual(connection.execute('SELECT evidence FROM project_blocks').fetchone()[0], record.decision.observation.detail)

    def test_current_limit_change_or_missing_settings_cancel_old_configuration_action(self):
        for current_limit in (None, 120):
            with self.subTest(current_limit=current_limit):
                connection = self.connection()
                with connection:
                    notifications.configure_session(connection, enabled=True, verify_timeout_seconds=90)
                    connection.execute("UPDATE exec_plans SET verify_result='전체 verify 시간 초과: verify_timeout_seconds=90'")
                    scoped_blocking.record_plan_block(connection, self.repo, 'G-018',
                        scoped_blocking.BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT, 'native timeout limit insufficient', '90')
                    checkpoints = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
                    checkpoints.finish_unpublished(checkpoints.get(self.repo, 'G-018'), 'local-only', retryable=False)
                original = dict(self.events(connection)[0])
                with connection:
                    notifications.configure_session(connection, enabled=True, verify_timeout_seconds=current_limit)
                adapter = mock.Mock()
                self.assertEqual(notifications.dispatch_pending(connection, 'token', 'chat', adapter=adapter), 0)
                adapter.assert_not_called()
                after = dict(self.events(connection)[0])
                self.assertIsNotNone(after['canceled_at'])
                for field in original.keys() - {'canceled_at', 'delivery_started_at'}:
                    self.assertEqual(after[field], original[field], field)
                self.assertEqual(connection.execute('SELECT release_fingerprint FROM execplan_blocks').fetchone()[0], '90')
