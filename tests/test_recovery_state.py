import json
from dataclasses import replace
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.recovery.recovery_runtime import RecoveryAction, RecoveryObservation, RecoveryReason
from loop_manager_internal.storage.recovery_state import (
    RecoveryRepository, RecoveryStateConflict, begin_external_run,
    pending_review_correction, reserve_review_correction, start_review_correction,
)
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.recovery.recovery_dispatcher import RecoveryDispatcher
from tests.support.database import runtime_connection


class RecoveryRepositoryTests(unittest.TestCase):
    def test_cancelled_and_erased_subjects_cannot_replay_recovery_or_persistent_failure(self):
        from loop_manager_internal.storage import deleted_plan_state
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = RecoveryRepository(connection)
        deleted_plan_state.ensure_schema(connection)
        target = replace(self.observation(), plan_id='G-047')
        begin_external_run(connection, 'original-run')
        for observation in (target, replace(target, plan_id='G-048'),
                            replace(target, repo='other'), replace(target, plan_id=None)):
            repository.observe(observation)
        connection.execute("INSERT INTO execplan_cancellations "
            "(repo,plan_id,path,title,sources,depends_on,complexity,risk,priority,content_hash,plan_sha,deletion_commit) "
            "SELECT repo,plan_id,path,title,sources,depends_on,'low','low',priority,content_hash,plan_sha,'deleted' "
            "FROM exec_plans WHERE plan_id='G-047'")
        connection.commit()
        for erased in (False, True):
            with self.subTest(erased=erased):
                if erased:
                    deleted_plan_state.forget(connection, 'repo', 'G-047')
                before = tuple(connection.iterdump())
                with self.assertRaisesRegex(RecoveryStateConflict, '취소·최종 삭제'):
                    repository.observe(replace(target, detail='late failure', progress_marker='late-input'))
                for subject in ('G-047', 'plan:G-047'):
                    repository._record_external_failure('repo', subject, phase='IMPLEMENTING',
                        reason_code=target.reason.value, fingerprint='late', progress_marker='late',
                        disposition='state_reconcile', detail='late failure', recovery_attempts='{}', run_id='late-run')
                self.assertEqual(tuple(connection.iterdump()), before)
                self.assertFalse(connection.in_transaction)
                connection.execute("INSERT INTO manager_metadata VALUES ('caller-pending','retained')")
                with self.assertRaises(RecoveryStateConflict):
                    repository.observe(target)
                self.assertTrue(connection.in_transaction)
                self.assertEqual(connection.execute("SELECT value FROM manager_metadata WHERE key='caller-pending'").fetchone()[0], 'retained')
                connection.rollback()
                self.assertEqual(tuple(connection.iterdump()), before)

    def test_erasure_between_recovery_observation_and_writer_reservation_is_rechecked(self):
        from loop_manager_internal.storage import deleted_plan_state, execplan_catalog_state
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recovery.db'
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            repository = RecoveryRepository(connection)
            deleted_plan_state.ensure_schema(connection)
            connection.commit()
            other = sqlite3.connect(path)
            self.addCleanup(other.close)
            check = execplan_catalog_state.is_cancelled
            observed = []
            def mark_after_read(current, repo, plan_id):
                canceled = check(current, repo, plan_id)
                if not observed:
                    self.assertFalse(current.in_transaction)
                    with other:
                        other.execute('INSERT INTO deleted_plan_ids VALUES (?,?)', (repo, plan_id))
                    observed.append(True)
                return canceled
            with mock.patch.object(execplan_catalog_state, 'is_cancelled', side_effect=mark_after_read):
                with self.assertRaisesRegex(RecoveryStateConflict, '취소·최종 삭제'):
                    repository.observe(self.observation())
            self.assertEqual(observed, [True])
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute('SELECT count(*) FROM recovery_records').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM persistent_execution_failures').fetchone()[0], 0)
            self.assertEqual(tuple(connection.execute('SELECT * FROM deleted_plan_ids').fetchone()), ('repo', 'G-042'))

    def test_conflict_dispatch_observes_outside_write_and_rolls_back_failed_owner_commit(self):
        from tests.support.pr_recovery import prepared_conflict_assessment
        connection = runtime_connection()
        self.addCleanup(connection.close)
        observed = prepared_conflict_assessment(connection, 'repo', 'G-047')
        connection.commit()
        repository = RecoveryRepository(connection)
        def interrupted(*_args, **options):
            self.assertFalse(connection.in_transaction)
            with runtime_state._transaction(connection):
                repository.observe(options['recovery_decision'].observation)
                raise RuntimeError('owner commit interrupted')
        dispatcher = RecoveryDispatcher(connection, interrupted, mock.Mock())
        with self.assertRaisesRegex(RuntimeError, 'owner commit interrupted'):
            dispatcher.apply(observed, worktree=Path('/state/G-047'), repo_path=Path('/repo'),
                             revision='base', verify_result='')
        self.assertIsNone(repository.row('repo', 'G-047'))
        self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'IMPLEMENTING')
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM execplan_blocks').fetchone()[0], 0)

    def test_conflict_dispatch_defers_inside_a_callers_transaction_without_git_or_commit(self):
        from tests.support.pr_recovery import prepared_conflict_assessment
        connection = runtime_connection()
        self.addCleanup(connection.close)
        observed = prepared_conflict_assessment(connection, 'repo', 'G-047')
        connection.commit()
        blocked = mock.Mock(side_effect=AssertionError('Git observation under write transaction'))
        dispatcher = RecoveryDispatcher(connection, blocked, blocked)
        connection.execute('BEGIN')
        result = dispatcher.apply(observed, worktree=Path('/state/G-047'), repo_path=Path('/repo'),
                                  revision='base', verify_result='')
        self.assertEqual(result.action, RecoveryAction.PAUSE)
        self.assertTrue(connection.in_transaction)
        blocked.assert_not_called()
        connection.rollback()
        self.assertIsNone(RecoveryRepository(connection).row('repo', 'G-047'))

    def test_final_conflict_evidence_is_preserved_only_for_current_pr_and_owned_checkpoint(self):
        from tests.support.pr_recovery import prepared_conflict_assessment
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        connection = runtime_connection()
        self.addCleanup(connection.close)
        observed = prepared_conflict_assessment(connection, 'repo', 'G-047')
        repository = RecoveryRepository(connection)
        final = repository.observe(observed)
        self.assertEqual(final.decision.action, RecoveryAction.FINAL_BLOCK)
        self.assertEqual(final.final_evidence['conflict_evidence'], observed.conflict_evidence)
        self.assertEqual(final.final_evidence['unavailable_recovery_routes'], observed.unavailable_recovery_routes)
        from loop_manager_internal.recovery.block_service import _final_block_recovery_decision
        from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
        plan = connection.execute('SELECT * FROM exec_plans').fetchone()
        self.assertIs(_final_block_recovery_decision(connection, 'repo', plan, 'WORKING',
            BlockReasonCode.MANAGED_WORKTREE_CONFLICT, observed.detail, observed.fingerprint, final.decision), final.decision)
        for field in ('pr_head_oid', 'pr_conflict_base_oid', 'worktree', 'worker_session_id', 'current_revision_sha'):
            with self.subTest(field=field):
                original = connection.execute(f'SELECT {field} FROM exec_plans').fetchone()[0]
                connection.execute(f'UPDATE exec_plans SET {field}=?', ('changed',))
                paused = repository.observe(observed)
                self.assertEqual(paused.decision.action, RecoveryAction.PAUSE)
                self.assertIsNone(paused.final_evidence)
                connection.execute(f'UPDATE exec_plans SET {field}=?', (original,))
        checkpoints = ExecutionCheckpointRepository(connection)
        saved = checkpoints.get('repo', 'G-047')
        checkpoints.record('repo', 'G-047', expected_generation=saved.generation,
            operation=saved.operation, expected_head=saved.expected_head, target_base_oid=saved.target_base_oid,
            observed_remote_oid=saved.observed_remote_oid, snapshot={'new': 'resource input'})
        self.assertEqual(repository.observe(observed).decision.action, RecoveryAction.PAUSE)
        with self.assertRaises(RecoveryStateConflict):
            _final_block_recovery_decision(connection, 'repo', plan, 'WORKING',
                BlockReasonCode.MANAGED_WORKTREE_CONFLICT, observed.detail, observed.fingerprint, final.decision)
        self.assertEqual(connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'IMPLEMENTING')
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM execplan_blocks').fetchone()[0], 0)

    def test_retired_git_failure_resolves_without_resetting_audit_or_other_causes(self):
        observation = RecoveryObservation(
            RecoveryReason.AGENT_SCOPE_VIOLATION, 'repo', 'G-042', 'WORKING',
            'plan', 'base', 'same-input', 'head', 'worker Git 범위 위반: 공유 Git refs 변경',
        )
        self.repository.observe(observation, attempts=3)
        before = dict(self.repository.row('repo', 'G-042'))
        self.repository.resolve_git_scope_failure('repo', 'G-042')
        after = dict(self.repository.row('repo', 'G-042'))
        self.assertEqual(after['pending_action'], 'reconcile')
        for key in ('generation', 'identity', 'attempts', 'recovery_counts', 'detail', 'final_evidence'):
            self.assertEqual(after[key], before[key])
        self.repository.resolve_git_scope_failure('repo', 'G-042')
        self.assertEqual(dict(self.repository.row('repo', 'G-042')), after)
        self.repository.observe(replace(observation, reason=RecoveryReason.USER_DECISION))
        protected = dict(self.repository.row('repo', 'G-042'))
        self.repository.resolve_git_scope_failure('repo', 'G-042')
        self.assertEqual(dict(self.repository.row('repo', 'G-042')), protected)

    def test_retired_pending_notice_closes_even_after_an_independent_record_replaces_it(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        loop_manager.notifications.configure_session(connection, enabled=True)
        repository = RecoveryRepository(connection)
        observation = RecoveryObservation(
            RecoveryReason.REMOTE_SNAPSHOT_RACE, 'repo', None, 'WORKING', None,
            'base', 'same', 'progress', 'Agent 범위 선검증 실패: old Git observation')
        for _ in range(3):
            begin_external_run(connection)
            repository.observe(observation, attempts=3)
        from loop_manager_internal.notifications.events import _key
        loop_manager.notifications.ensure_schema(connection)
        legacy = connection.execute('SELECT * FROM persistent_execution_failures').fetchone()
        identity = "\0".join((legacy['subject'], str(legacy['episode']), legacy['phase'],
                              legacy['reason_code'], legacy['fingerprint']))
        loop_manager.notifications._insert(connection, _key('persistent_failure', 'repo', identity),
                                           'repo', 'persistent_execution_failure', {'cause': observation.detail})
        notice = connection.execute("SELECT * FROM notification_outbox WHERE "
                                    "event_type='persistent_execution_failure'").fetchone()
        self.assertIsNotNone(notice)
        self.assertIsNone(notice['canceled_at'])
        connection.execute("UPDATE recovery_records SET reason_code=?", (RecoveryReason.USER_DECISION.value,))
        protected = dict(repository.row('repo', None))
        repository.resolve_git_scope_failure('repo', None)
        self.assertEqual(dict(repository.row('repo', None)), protected)
        failure = connection.execute('SELECT * FROM persistent_execution_failures').fetchone()
        self.assertEqual(failure['active'], 0)
        self.assertEqual(failure['detail'], observation.detail)
        canceled = connection.execute('SELECT * FROM notification_outbox WHERE event_key=?',
                                      (notice['event_key'],)).fetchone()
        self.assertIsNotNone(canceled['canceled_at'])
        self.assertEqual(canceled['payload'], notice['payload'])
        repository.resolve_git_scope_failure('repo', None)
        self.assertEqual(dict(connection.execute('SELECT * FROM persistent_execution_failures').fetchone()),
                         dict(failure))

    def test_registered_project_reentry_shares_budget_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'state.db'
            for result_id, expected in ((1, 1), (2, 2)):
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                RecoveryRepository(connection)
                begin_external_run(connection, 'same-scheduler-run')
                self.assertEqual(
                    reserve_review_correction(
                        connection, 'repo', 'G-042', result_id,
                    ), (True, expected, 'same-scheduler-run'),
                )
                start_review_correction(connection, 'repo', 'G-042', 'same-scheduler-run', result_id)
                connection.close()
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            begin_external_run(connection, 'next-scheduler-run')
            self.assertEqual(
                reserve_review_correction(connection, 'repo', 'G-042', 2),
                (True, 2, 'same-scheduler-run'),
            )
            connection.close()

    def test_review_budget_counts_started_turns_and_keeps_wait_across_external_runs(self):
        self.connection.execute(
            "CREATE TABLE validation_results (id INTEGER PRIMARY KEY, repo TEXT, "
            "plan_id TEXT, stage TEXT, classification TEXT, raw_output TEXT)"
        )
        run = begin_external_run(self.connection)
        for result_id in range(1, 5):
            diagnostic = f"ISSUE_ID: a.py::issue-{result_id}::blocking"
            self.connection.execute(
                "INSERT INTO validation_results VALUES (?, 'repo', 'G-042', "
                "'review', 'failure', ?)", (result_id, diagnostic),
            )
            self.assertEqual(
                pending_review_correction(self.connection, 'repo', 'G-042', diagnostic),
                result_id,
            )
            allowed, used, identity = reserve_review_correction(
                self.connection, 'repo', 'G-042', result_id,
            )
            self.assertEqual((allowed, used, identity), (result_id <= 3, min(result_id, 3), run))
            if result_id <= 3:
                start_review_correction(
                    self.connection, 'repo', 'G-042', run, result_id,
                )
                start_review_correction(
                    self.connection, 'repo', 'G-042', run, result_id,
                )
                self.assertEqual(
                    reserve_review_correction(self.connection, 'repo', 'G-042', result_id),
                    (True, result_id, run),
                )
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM review_correction_turns WHERE started_at IS NOT NULL"
        ).fetchone()[0], 3)
        self.connection.execute(
            "INSERT INTO validation_results VALUES (5, 'repo', 'G-042', "
            "'verify', 'failure', 'verify failure')"
        )
        self.assertIsNone(pending_review_correction(
            self.connection, 'repo', 'G-042', diagnostic,
        ))
        next_run = begin_external_run(self.connection)
        self.assertNotEqual(next_run, run)
        self.assertEqual(
            reserve_review_correction(self.connection, 'repo', 'G-042', 4),
            (False, 3, next_run),
        )
        due = self.connection.execute('SELECT MAX(retry_after) FROM review_correction_turns').fetchone()[0]
        with mock.patch('loop_manager_internal.storage.recovery_state.time.time', return_value=due + 1):
            self.assertEqual(reserve_review_correction(self.connection, 'repo', 'G-042', 4), (True, 4, next_run))
            start_review_correction(self.connection, 'repo', 'G-042', next_run, 4)
        self.assertEqual(self.connection.execute(
            'SELECT COUNT(*) FROM review_correction_turns WHERE started_at IS NOT NULL').fetchone()[0], 4)

    def test_review_correction_matches_the_redacted_audit_result(self):
        self.connection.execute(
            "CREATE TABLE validation_results (id INTEGER PRIMARY KEY, repo TEXT, "
            "plan_id TEXT, stage TEXT, classification TEXT, raw_output TEXT)"
        )
        raw = 'ISSUE_ID: app.py::secret::blocking\npassword: review-secret\nVERDICT: FAIL'
        self.connection.execute(
            "INSERT INTO validation_results VALUES (1, 'repo', 'G-042', "
            "'review', 'failure', ?)", (redact_secrets(raw),),
        )

        self.assertEqual(
            pending_review_correction(self.connection, 'repo', 'G-042', raw), 1,
        )

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.repository = RecoveryRepository(self.connection)

    def observation(self, progress="worker:head", fingerprint="conflict"):
        return RecoveryObservation(
            RecoveryReason.REBASE_CONFLICT, "repo", "G-042", "IMPLEMENTING",
            "plan-sha", "base-sha", fingerprint, progress, "conflict remains",
        )

    def test_occurrence_progress_cas_and_budget_pause_survive_repository_restart(self):
        first = self.repository.observe(self.observation())
        self.assertEqual(first.decision.action, RecoveryAction.CORRECT)
        with self.assertRaises(RecoveryStateConflict):
            self.repository.observe(self.observation(), expected_generation=0)
        second = RecoveryRepository(self.connection).observe(
            self.observation(), expected_generation=first.generation)
        self.assertEqual(second.decision.occurrence, 2)
        self.repository.observe(RecoveryObservation(
            RecoveryReason.GIT_LOCK, "repo", "G-042", "IMPLEMENTING",
            "plan-sha", "base-sha", "lock", "worker:head", "lock busy",
        ))
        third = self.repository.observe(
            self.observation(), expected_generation=second.generation + 1)
        self.assertEqual(third.decision.occurrence, 3)
        self.assertEqual(third.decision.action, RecoveryAction.CORRECT)
        paused = self.repository.observe(
            self.observation(), expected_generation=third.generation)
        self.assertEqual(paused.decision.action, RecoveryAction.PAUSE)
        self.assertEqual(paused.decision.occurrence, 4)
        self.assertIsNone(paused.final_evidence)
        restarted = RecoveryRepository(self.connection)
        saved = restarted.row('repo', 'G-042')
        self.assertEqual(saved['pending_action'], 'pause')
        self.assertEqual(saved['detail'], 'conflict remains')
        self.assertEqual(json.loads(saved['recovery_counts'])['rebase_conflict'], 4)
        self.assertEqual(json.loads(saved['recovery_counts'])['git_lock'], 1)

        # An independent ownership safety failure still retains final evidence;
        # the correction counter cannot establish this cause on its own.
        final = restarted.observe(replace(
            self.observation(), reason=RecoveryReason.OWNERSHIP_UNCERTAIN))
        self.assertEqual(final.decision.action, RecoveryAction.FINAL_BLOCK)
        self.assertEqual(final.final_evidence["occurrence"], 1)
        self.assertEqual(final.final_evidence["last_stable_revision"], "base-sha")
        self.assertEqual(final.final_evidence["recovery_attempts"]["git_lock"], 1)
        self.assertEqual(final.final_evidence["recovery_attempts"]["rebase_conflict"], 4)
        progressed = self.repository.observe(self.observation(progress="worker:new-head"))
        self.assertEqual(progressed.decision.occurrence, 1)
        self.assertEqual(progressed.decision.action, RecoveryAction.CORRECT)
        self.assertIsNone(progressed.final_evidence)

    def test_transient_pause_preserves_only_recovery_record(self):
        observation = RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", "G-042", "PUBLISHING",
            "plan-sha", "base-sha", "fetch", "worker:head", "service unavailable",
        )
        record = self.repository.observe(observation, attempts=3)
        self.assertEqual(record.decision.action, RecoveryAction.PAUSE)
        self.assertEqual(self.repository.row("repo", "G-042")["pending_action"], "pause")
        self.assertIsNone(record.final_evidence)

    def test_external_runs_count_once_and_reset_on_progress_or_success(self):
        def observe_run(observation, repeats=1):
            before = self.repository.external_run_snapshot("repo")
            for _ in range(repeats):
                self.repository.observe(observation)
            self.repository.observe_external_run("repo", before)

        failure = RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", "G-042", "PUBLISHING",
            "plan-sha", "base-sha", "fetch-failure", "head-a", "fetch failed",
        )
        observe_run(failure, repeats=3)
        first = self.connection.execute(
            "SELECT consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(first), (1, 0))

        observe_run(failure)
        observe_run(failure)
        third = self.connection.execute(
            "SELECT consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(third), (3, 1))

        observe_run(RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", "G-042", "PUBLISHING",
            "plan-sha", "base-sha", "fetch-failure", "head-b", "fetch failed",
        ))
        progressed = self.connection.execute(
            "SELECT consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(progressed), (1, 0))

        before_success = self.repository.external_run_snapshot("repo")
        self.repository.observe_external_run("repo", before_success)
        resolved = self.connection.execute(
            "SELECT active, consecutive_runs FROM persistent_execution_failures "
            "WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(resolved), (0, 0))

    def test_blocked_subject_without_matching_saved_block_keeps_failure_diagnostic(self):
        before = self.repository.external_run_snapshot("repo")
        self.repository.observe(RecoveryObservation(
            RecoveryReason.GUIDANCE_APPROVAL_REQUIRED, "repo", "G-042",
            "IMPLEMENTING", "plan-sha", "base-sha", "guidance", "head-a",
            "decision pending",
        ))
        self.repository.observe_external_run(
            "repo", before, blocked_subjects=frozenset({"G-042"}),
        )
        row = self.connection.execute(
            "SELECT active, consecutive_runs, notification_eligible, "
            "suppressed_by_block FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
        ).fetchone()
        self.assertEqual(tuple(row), (1, 1, 1, 0))

    def test_external_failure_streak_survives_interrupted_database_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            observation = RecoveryObservation(
                RecoveryReason.FETCH_FAILED, "repo", "G-042", "PUBLISHING",
                "plan-sha", "base-sha", "fetch-failure", "head-a", "fetch failed",
            )
            for _ in range(3):
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                repository = RecoveryRepository(connection)
                begin_external_run(connection)
                repository.observe(observation)
                connection.commit()
                connection.close()

            connection = sqlite3.connect(database)
            row = connection.execute(
                "SELECT consecutive_runs, notification_eligible FROM "
                "persistent_execution_failures WHERE repo = 'repo' AND subject = 'G-042'"
            ).fetchone()
            connection.close()
        self.assertEqual(tuple(row), (3, 1))

    def test_third_interrupted_observation_keeps_diagnostics_without_failure_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            repo = "repo"
            failure = RecoveryObservation(
                RecoveryReason.FETCH_FAILED, repo, None, "WORKING", None,
                "base-sha", "fetch-failure", "head-a", "fetch failed",
            )
            for _ in range(3):
                connection = sqlite3.connect(database)
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.SCHEMA)
                loop_manager._ensure_execplans_schema(connection)
                loop_manager.notifications.ensure_schema(connection)
                connection.execute(
                    "INSERT OR IGNORE INTO projects (repo, base_branch) "
                    "VALUES (?, 'main')", (repo,),
                )
                loop_manager.notifications.configure_session(connection, enabled=True)
                begin_external_run(connection)
                connection.commit()
                with runtime_state._transaction(connection):
                    RecoveryRepository(connection).observe(failure)
                # Closing here models a process that never reaches finish_external_run.
                connection.close()

            connection = sqlite3.connect(database)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE repo = ? AND event_type = 'persistent_execution_failure'",
                (repo,),
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT consecutive_runs, notification_eligible, notification_queued "
                "FROM persistent_execution_failures WHERE repo = ?", (repo,),
            ).fetchone(), (3, 1, 0))
            connection.close()

    def test_block_suppression_without_matching_live_cause_keeps_legacy_audit(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        loop_manager.notifications.ensure_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )
        loop_manager.notifications.configure_session(connection, enabled=True)
        run_id = begin_external_run(connection)
        repository = RecoveryRepository(connection)
        repository.observe(RecoveryObservation(
            RecoveryReason.GUIDANCE_APPROVAL_REQUIRED, "repo", "G-042",
            "IMPLEMENTING", "plan-sha", "base-sha", "guidance", "head-a",
            "decision pending",
        ))
        failure = connection.execute(
            "SELECT episode, phase, reason_code, fingerprint, consecutive_runs "
            "FROM persistent_execution_failures WHERE repo = ? AND subject = ?",
            ("repo", "G-042"),
        ).fetchone()
        identity = "\0".join((
            "G-042", str(failure[0]), str(failure[1]), str(failure[2]),
            str(failure[3]),
        ))
        event_key = loop_manager.notifications._key(
            "persistent_failure", "repo", identity,
        )
        loop_manager.notifications._insert(
            connection, event_key, "repo", "persistent_execution_failure",
            {"execplan_id": "G-042", "stage": str(failure[1]),
             "reason_code": str(failure[2]), "cause": "decision pending"},
        )
        connection.execute(
            "UPDATE persistent_execution_failures SET notification_queued = 1 "
            "WHERE repo = ? AND subject = ?", ("repo", "G-042"),
        )
        connection.commit()
        old_payload = connection.execute(
            "SELECT payload FROM notification_outbox WHERE event_key = ?",
            (event_key,),
        ).fetchone()[0]

        # A claimed subject alone is not proof that this failure became a block.
        loop_manager.notifications.suppress_persistent_failure_for_block(
            connection, "repo", "G-042",
        )
        connection.commit()
        repository.finish_external_run(
            "repo", run_id, resolved=False,
            blocked_subjects=frozenset({"G-042"}),
        )
        retained = connection.execute(
            "SELECT attempts, canceled_at, payload FROM notification_outbox "
            "WHERE event_key = ?", (event_key,),
        ).fetchone()
        self.assertEqual(retained[0], 0)
        self.assertIsNone(retained[1])
        self.assertEqual(retained[2], old_payload)
        failure = connection.execute(
            "SELECT active, consecutive_runs, notification_eligible, "
            "notification_queued, suppressed_by_block FROM "
            "persistent_execution_failures WHERE repo = ? AND subject = ?",
            ("repo", "G-042"),
        ).fetchone()
        self.assertEqual(tuple(failure), (1, 1, 1, 1, 0))

    def test_external_failure_projection_supports_tuple_rows(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        repository = RecoveryRepository(connection)
        before = repository.external_run_snapshot("repo")
        repository.observe(RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", None, "WORKING", None,
            "base-sha", "fetch-failure", "head-a", "fetch failed",
        ))

        repository.observe_external_run("repo", before)

        row = connection.execute(
            "SELECT consecutive_runs, notification_eligible FROM "
            "persistent_execution_failures WHERE repo = 'repo' AND subject = '__project__'"
        ).fetchone()
        self.assertEqual(row, (1, 0))

    def test_validation_success_resets_only_its_correction_streak(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime_state.RuntimeStateRepository(connection).activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        repository = RecoveryRepository(connection)

        def correction(fingerprint):
            return RecoveryObservation(
                RecoveryReason.WORKER_CORRECTION, 'repo', 'G-047',
                'VERIFYING', 'plan-sha', 'base', fingerprint,
                'worker-session:head', 'same validation failure',
            )

        verify = correction('verify-fingerprint')
        review = correction('review-fingerprint')
        rebase = RecoveryObservation(
            RecoveryReason.REBASE_CONFLICT, 'repo', 'G-047', 'IMPLEMENTING',
            'plan-sha', 'base', 'verify-fingerprint',
            'worker-session:head', 'conflict remains',
        )
        repository.observe(verify)
        repository.observe(verify)
        repository.observe(review)
        repository.observe(rebase)
        connection.execute(
            "UPDATE exec_plans SET verify_failure_history = ?, "
            "review_failure_history = ? WHERE repo = 'repo' AND plan_id = 'G-047'",
            (json.dumps({'verify-fingerprint': {'occurrences': 2, 'sample': 'failure'}}),
             json.dumps({'review-fingerprint': {'occurrences': 1, 'sample': 'failure'}})),
        )
        connection.commit()

        loop_manager.clear_validation_failure_history(connection, 'repo', 'verify')

        plan = connection.execute(
            "SELECT verify_failure_history, review_failure_history FROM exec_plans "
            "WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual(json.loads(plan['verify_failure_history']), {})
        self.assertIn('review-fingerprint', json.loads(plan['review_failure_history']))
        self.assertEqual(repository.observe(verify).decision.occurrence, 1)
        self.assertEqual(repository.observe(review).decision.occurrence, 2)
        self.assertEqual(repository.observe(rebase).decision.occurrence, 2)

        loop_manager.clear_validation_failure_history(connection, 'repo', 'review')

        self.assertEqual(repository.observe(review).decision.occurrence, 1)
        self.assertEqual(repository.observe(verify).decision.occurrence, 2)

    def test_project_recovery_before_execplan_schema_is_durable(self):
        self.connection.execute(loop_manager.SCHEMA)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )

        decision = loop_manager._record_project_recovery(
            self.connection, "repo", RecoveryReason.GIT_LOCK,
            "index.lock exists", attempts=3,
        )

        self.assertEqual(decision.action, RecoveryAction.PAUSE)
        row = self.repository.row("repo", None)
        self.assertEqual((row["reason_code"], row["pending_action"]),
                         ("git_lock", "pause"))

    def test_process_run_pauses_after_three_identical_remote_races(self):
        repo = str(Path("repo").resolve())
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, phase) "
            "VALUES (?, 'main', 'WORKING')", (repo,)
        )
        self.connection.commit()
        observation = RecoveryObservation(
            RecoveryReason.REMOTE_SNAPSHOT_RACE, repo, "G-042", "VERIFYING",
            "plan-sha", "base-sha", "remote-race", "worker:head", "base moved",
        )
        calls = []

        def step(*_args):
            calls.append(1)
            RecoveryRepository(self.connection).observe(observation)
            return loop_manager.PROJECT_STEP_RETRY

        with mock.patch("loop_manager._run_project_step", side_effect=step):
            result = loop_manager.run_project_result(
                self.connection, Path("/unused-state"), repo, Path("/unused.ini"),
                "main", False,
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
        self.assertEqual(result.exit_code, 0)
        row = self.repository.row(repo, "G-042")
        self.assertEqual((row["pending_action"], row["attempts"]), ("pause", 3))

    def test_process_run_bounds_remote_race_even_when_revision_changes(self):
        repo = str(Path("repo").resolve())
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, phase) VALUES (?, 'main', 'WORKING')",
            (repo,),
        )
        self.connection.commit()
        attempts = []

        def step(*_args):
            attempts.append(len(attempts) + 1)
            RecoveryRepository(self.connection).observe(RecoveryObservation(
                RecoveryReason.REMOTE_SNAPSHOT_RACE, repo, "G-042",
                "VERIFYING", "plan-sha", f"base-{attempts[-1]}",
                f"race-{attempts[-1]}", "worker:head", "base moved",
            ))
            return loop_manager.PROJECT_STEP_RETRY

        with mock.patch("loop_manager._run_project_step", side_effect=step):
            result = loop_manager.run_project_result(
                self.connection, Path("/unused-state"), repo, Path("/unused.ini"),
                "main", False,
            )
        self.assertEqual(len(attempts), 3)
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)

    def test_final_block_effect_failure_rolls_back_recovery_record(self):
        observation = RecoveryObservation(
            RecoveryReason.GUIDANCE_APPROVAL_REQUIRED, "repo", "G-042",
            "IMPLEMENTING", "plan-sha", "base-sha", "guidance", "worker:head",
            "decision pending",
        )
        calls = []

        def failing_block(*_args, **_kwargs):
            calls.append(1)
            raise RuntimeError("block transaction interrupted")

        dispatcher = RecoveryDispatcher(
            self.connection, failing_block, failing_block,
        )
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            dispatcher.apply(
                observation, worktree=Path("/worktree"), repo_path=Path("/repo"),
                revision="base-sha", verify_result="",
            )
        self.assertEqual(calls, [1])
        self.assertIsNone(self.repository.row("repo", "G-042"))

    def test_nonfinal_dispatch_never_calls_checkpoint_or_block_effect(self):
        observation = RecoveryObservation(
            RecoveryReason.FETCH_FAILED, "repo", "G-042",
            "VERIFYING", "plan-sha", "base-sha", "fetch", "worker:head",
            "temporary fetch error",
        )
        blocked = mock.Mock(side_effect=AssertionError("block called"))
        dispatcher = RecoveryDispatcher(self.connection, blocked, blocked)
        decision = dispatcher.apply(
            observation, worktree=Path("/worktree"), repo_path=Path("/repo"),
            revision="base-sha", verify_result="saved verify",
        )
        self.assertEqual(decision.action, RecoveryAction.RETRY)
        blocked.assert_not_called()

    def test_exhausted_conflict_preserves_execution_checkpoint_and_has_no_outbox_event(self):
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository

        connection = runtime_connection()
        self.addCleanup(connection.close)
        runtime = runtime_state.RuntimeStateRepository(connection)
        runtime.activate('repo', 'G-047', activation_sha='base',
                         branch='loop-manager/G-047', worktree='/state/G-047')
        connection.execute(
            "UPDATE exec_plans SET pr_url='https://example.test/pr/1', "
            "pr_head_oid='remote-head', pr_conflict_recovery=1, "
            "pr_conflict_head_oid='remote-head', pr_conflict_base_oid='base', "
            "worker_session_id='saved-worker' WHERE repo='repo' AND plan_id='G-047'"
        )
        checkpoints = ExecutionCheckpointRepository(connection)
        checkpoint = checkpoints.record(
            'repo', 'G-047', expected_generation=0, operation='rebase',
            expected_head='original-head', target_base_oid='base',
            observed_remote_oid='remote-head', snapshot={'stash_oid': 'saved-stash'},
        )
        loop_manager.notifications.configure_session(connection, enabled=True)
        connection.commit()
        before = dict(connection.execute('SELECT * FROM exec_plans').fetchone())
        blocked = mock.Mock(side_effect=AssertionError('unexpected final block'))
        dispatcher = RecoveryDispatcher(connection, blocked, blocked)
        observation = replace(self.observation(), plan_id='G-047')
        for _ in range(5):
            decision = dispatcher.apply(
                observation, worktree=Path('/state/G-047'), repo_path=Path('/repo'),
                revision='base', verify_result='', correction_limit=1,
            )
            self.assertEqual(decision.action, RecoveryAction.PAUSE)
        blocked.assert_not_called()
        self.assertEqual(dict(connection.execute('SELECT * FROM exec_plans').fetchone()), before)
        self.assertEqual(checkpoints.get('repo', 'G-047'), checkpoint)
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM execplan_blocks').fetchone()[0], 0)
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM project_blocks').fetchone()[0], 0)
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)
        saved = RecoveryRepository(connection).row('repo', 'G-047')
        self.assertEqual((saved['pending_action'], saved['occurrence']), ('pause', 5))
        self.assertIsNone(saved['final_evidence'])


if __name__ == "__main__":
    unittest.main()


class RebaseCauseResolutionTests(unittest.TestCase):
    def setUp(self):
        from loop_manager_internal.notifications import notifications
        RecoveryRepositoryTests.setUp(self)
        notifications.ensure_schema(self.connection)

    def block(self, detail='manual rebase conflict'):
        from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
        begin_external_run(self.connection)
        observation = RecoveryObservation(RecoveryReason.MANAGED_CONFLICT_UNRECOVERABLE, 'repo', 'G-042',
            'WORKING', 'old-input', 'old-base', 'failure', 'old-session-progress', detail)
        self.repository.observe(observation, final_block_reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT)
        # Saved legacy blocks predate the assessment gate. Keep their release
        # contract without producing them from the current policy.
        self.connection.execute("UPDATE recovery_records SET pending_action='final_block', "
            "disposition='user_action_required', final_evidence=?",
            (json.dumps({'block_reason_code': BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value}),))
        return {'reason_code': 'managed_worktree_conflict', 'evidence': detail}

    def test_current_resolution_closes_only_matching_cause_and_keeps_failure_audit(self):
        block = self.block()
        # Missing occurrence history and old progress/session are not gates.
        self.connection.execute("UPDATE recovery_records SET occurrence_index = '{}'")
        before = dict(self.repository.row('repo', 'G-042'))
        self.repository.resolve_rebase_block('repo', 'G-042', block)
        after = dict(self.repository.row('repo', 'G-042'))
        self.assertEqual(after['pending_action'], 'reconcile')
        for key in ('detail', 'reason_code', 'final_evidence', 'progress_marker', 'occurrence_index'):
            self.assertEqual(after[key], before[key])
        failure = self.connection.execute('SELECT * FROM persistent_execution_failures').fetchone()
        self.assertEqual(failure['active'], 0)
        self.assertEqual(failure['detail'], block['evidence'])
        self.assertIsNotNone(failure['resolved_at'])
        self.repository.resolve_rebase_block('repo', 'G-042', block)
        self.assertEqual(dict(self.repository.row('repo', 'G-042')), after)

    def test_independent_recovery_record_is_not_resolved_by_rebase_release(self):
        block = self.block()
        self.repository.observe(RecoveryObservation(RecoveryReason.USER_DECISION, 'repo', 'G-042',
            'WORKING', 'new-input', 'new-base', 'decision', 'current', 'choose a policy'))
        before = dict(self.repository.row('repo', 'G-042'))
        self.repository.resolve_rebase_block('repo', 'G-042', block)
        self.assertEqual(dict(self.repository.row('repo', 'G-042')), before)

    def test_final_block_reason_must_match_before_closing_its_record(self):
        block = self.block()
        block['reason_code'] = 'repository_state_unsafe'
        before = dict(self.repository.row('repo', 'G-042'))
        self.repository.resolve_rebase_block('repo', 'G-042', block)
        self.assertEqual(dict(self.repository.row('repo', 'G-042')), before)
