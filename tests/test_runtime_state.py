from tests.support.runtime import runtime_rebase_fixture
from tests.support.database import runtime_connection
import dataclasses
from pathlib import Path
import sqlite3
import tempfile
import unittest

import loop_manager
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.rebase_observations import decision_for, observation
from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason, decide
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode


class CurrentRebaseRuntimeTests(unittest.TestCase):

    def test_current_acceptance_is_atomic_idempotent_and_never_execution_proof(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                connection, runtime, checkpoint, original, expected, decision = runtime_rebase_fixture(self, legacy=legacy)
                result = runtime.complete_rebase(
                    original, checkpoint, acceptance=decision.acceptance,
                    project_values={'phase': 'WORKING'}, reobserve=lambda: decision,
                )
                saved = checkpoint.get('repo', 'G-047')
                self.assertEqual(saved.current_rebase_acceptance(), decision.acceptance)
                self.assertNotIn('rebase_completion', saved.snapshot)
                self.assertNotIn('completed_rebase', saved.snapshot)
                if not legacy:
                    self.assertEqual(saved.snapshot['old_journal'], 'failed-audit')
                    self.assertEqual(saved.snapshot['stage_evidence'], {'tree': 'old'})
                self.assertEqual(result.status, 'IMPLEMENTING')
                self.assertIsNone(result.verify_result)
                self.assertIsNone(result.review_result)
                self.assertEqual(connection.execute(
                    "SELECT phase, active_sha FROM projects WHERE repo = 'repo'"
                ).fetchone()[:], ('WORKING', 'base'))
                # A new repository instance models restart; the same result is
                # re-observed but does not increment generation or update rows.
                restarted = runtime_state.RuntimeStateRepository(connection)
                result_again = restarted.complete_rebase(
                    result, ExecutionCheckpointRepository(connection), acceptance=decision.acceptance,
                    project_values={'phase': 'WORKING'}, reobserve=lambda: decision,
                )
                self.assertEqual(result_again, result)
                self.assertEqual(checkpoint.get('repo', 'G-047'), saved)
                changed = decision_for(expected, dataclasses.replace(observation(expected),
                                                                      input_fingerprint='new-input'))
                with self.assertRaises(runtime_state.RuntimeStateError):
                    restarted.complete_rebase(
                        result, checkpoint, acceptance=decision.acceptance,
                        project_values={'phase': 'WORKING'}, reobserve=lambda: changed,
                    )

    def test_current_acceptance_rolls_back_checkpoint_plan_project_and_outbox(self):
        for stage in ('after_rebase_acceptance', 'after_execplan_update',
                      'after_project_update', 'after_outbox_enqueue'):
            with self.subTest(stage=stage):
                connection, runtime, checkpoint, original, _expected, decision = runtime_rebase_fixture(self)
                notifications.configure_session(connection, enabled=True)
                connection.commit()
                before_checkpoint = checkpoint.get('repo', 'G-047')
                before_project = tuple(connection.execute("SELECT * FROM projects").fetchone())
                before_events = connection.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0]
                def fail(current):
                    if current == stage:
                        raise RuntimeError(f'interrupted at {stage}')
                failing = runtime_state.RuntimeStateRepository(connection, fault_hook=fail)
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    failing.complete_rebase(
                        original, checkpoint, acceptance=decision.acceptance,
                        project_values={'phase': 'WORKING'}, reobserve=lambda: decision,
                    )
                self.assertEqual(checkpoint.get('repo', 'G-047'), before_checkpoint)
                self.assertEqual(runtime.projected('repo'), original)
                self.assertEqual(tuple(connection.execute("SELECT * FROM projects").fetchone()), before_project)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0],
                                 before_events)
                recovered = runtime.complete_rebase(
                    original, checkpoint, acceptance=decision.acceptance,
                    project_values={'phase': 'WORKING'}, reobserve=lambda: decision,
                )
                self.assertEqual(recovered.status, 'IMPLEMENTING')

    def test_current_acceptance_refuses_stale_generation_or_post_check_drift(self):
        for drift in ('generation', 'remote', 'files', 'inputs', 'scope', 'ownership',
                      'project_projection', 'recheck_checkpoint'):
            with self.subTest(drift=drift):
                connection, runtime, checkpoint, original, expected, decision = runtime_rebase_fixture(self)
                before = checkpoint.get('repo', 'G-047')
                reobserve = lambda: decision
                if drift == 'generation':
                    checkpoint.record('repo', 'G-047', expected_generation=before.generation,
                                      operation='rebase', expected_head='before', target_base_oid='base',
                                      observed_remote_oid='remote', snapshot=before.snapshot)
                elif drift == 'ownership':
                    connection.execute("UPDATE exec_plans SET workspace_owned = 0 WHERE repo = 'repo'")
                elif drift == 'project_projection':
                    connection.execute("UPDATE projects SET branch = 'other' WHERE repo = 'repo'")
                elif drift == 'recheck_checkpoint':
                    def reobserve():
                        checkpoint.record('repo', 'G-047', expected_generation=before.generation,
                                          operation='rebase', expected_head='before', target_base_oid='base',
                                          observed_remote_oid='remote', snapshot=before.snapshot)
                        return decision
                else:
                    actual = observation(expected)
                    changes = {
                        'remote': {'remote': dataclasses.replace(expected.remote, oid='race')},
                        'files': {'repository_state_fingerprint': 'changed-files'},
                        'inputs': {'input_fingerprint': 'changed'},
                        'scope': {'scope_fingerprint': 'changed'},
                    }[drift]
                    changed = decision_for(expected, dataclasses.replace(actual, **changes))
                    reobserve = lambda: changed
                baseline = checkpoint.get('repo', 'G-047')
                with self.assertRaises(runtime_state.RuntimeStateError):
                    runtime.complete_rebase(
                        original, checkpoint, acceptance=decision.acceptance,
                        project_values={'phase': 'WORKING'}, reobserve=reobserve,
                    )
                self.assertEqual(checkpoint.get('repo', 'G-047'), baseline)

    def test_current_acceptance_cannot_publish_clear_blocks_or_fake_journal(self):
        _connection, runtime, checkpoint, original, _expected, decision = runtime_rebase_fixture(self)
        for arguments in (
            {'reobserve': None}, {'completion': {'id': 'made-up'}},
            {'published_proof': {'journal_id': 'made-up'}},
            {'project_values': {'phase': 'PUBLISHING'}},
            {'project_values': {'phase': 'VERIFYING'}},
            {'project_values': {'phase': 'WORKING', 'verify_result': 'PASS'}},
            {'project_values': {'phase': 'WORKING', 'last_error': 'cannot preserve a successful candidate'}},
        ):
            with self.subTest(arguments=arguments):
                kwargs = dict(acceptance=decision.acceptance,
                              project_values={'phase': 'WORKING'}, reobserve=lambda: decision)
                kwargs.update(arguments)
                with self.assertRaises(runtime_state.RuntimeStateError):
                    runtime.complete_rebase(original, checkpoint, **kwargs)

    def test_current_acceptance_preserves_unrelated_block_and_failure_audit(self):
        connection, runtime, checkpoint, original, _expected, decision = runtime_rebase_fixture(self)
        blocked = runtime.transition_from_snapshot(
            original, plan_values={'status': 'PLAN_BLOCKED', 'blocked_reason': 'agent_scope',
                                    'last_error': 'existing failure'},
            project_values={'phase': 'PLAN_BLOCKED', 'last_error': 'existing failure'},
        )
        with self.assertRaises(runtime_state.RuntimeStateError):
            runtime.complete_rebase(blocked, checkpoint, acceptance=decision.acceptance,
                                    project_values={'phase': 'WORKING'}, reobserve=lambda: decision)
        accepted = runtime.complete_rebase(
            blocked, checkpoint, acceptance=decision.acceptance,
            project_values={'phase': 'PLAN_BLOCKED'}, reobserve=lambda: decision,
        )
        self.assertEqual(accepted.status, 'PLAN_BLOCKED')
        self.assertEqual(accepted.last_error, 'existing failure')
        self.assertEqual(connection.execute(
            "SELECT blocked_reason FROM exec_plans WHERE repo = 'repo'"
        ).fetchone()[0], 'agent_scope')


class RuntimeStateRepositoryTest(unittest.TestCase):
    def test_planning_correction_releases_only_bound_incident_atomically(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        notifications.configure_session(connection, enabled=True)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.update_project("repo", {
            "planning_error": "old unsafe", "planning_error_fingerprint": "old-input",
            "planning_error_origin": "unsafe",
            "active_sha": "new-base", "planning_result_completed": 1,
        })
        incident_identity = connection.execute(
            "SELECT planning_error_incident_id FROM projects WHERE repo = 'repo'",
        ).fetchone()[0]
        repository.update_project("repo", {
            "planning_correction_error": "old unsafe",
            "planning_correction_incident_id": incident_identity,
            "planning_correction_sha": "new-base",
            "planning_correction_context": "post_refactor_revalidation:2",
        })
        incident = connection.execute(
            "SELECT id FROM notified_block_incidents WHERE repo = 'repo' "
            "AND subject = 'planning'",
        ).fetchone()[0]
        arguments = dict(error="old unsafe", active_sha="new-base",
                         context="post_refactor_revalidation:2")
        self.assertFalse(repository.release_planning_correction(
            "repo", **{**arguments, "active_sha": "stale"},
        ))
        self.assertIsNone(connection.execute(
            "SELECT released_at FROM notified_block_incidents WHERE id = ?",
            (incident,),
        ).fetchone()[0])
        repository.update_project("repo", {"planning_error": "old unsafe"})
        self.assertFalse(repository.release_planning_correction("repo", **arguments))
        repository.update_project("repo", {
            "planning_correction_error": "old unsafe",
            "planning_correction_incident_id": incident_identity,
            "planning_correction_sha": "new-base",
            "planning_correction_context": "post_refactor_revalidation:2",
        })
        connection.execute(
            "UPDATE projects SET planning_error_incident_id = 'other-incident' "
            "WHERE repo = 'repo'",
        )
        self.assertFalse(repository.release_planning_correction("repo", **arguments))
        connection.execute(
            "UPDATE projects SET planning_error_incident_id = ? WHERE repo = 'repo'",
            (incident_identity,),
        )

        def interrupt(stage):
            if stage == "after_planning_block_release":
                raise RuntimeError("interrupted")

        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            runtime_state.RuntimeStateRepository(
                connection, fault_hook=interrupt,
            ).release_planning_correction("repo", **arguments)
        self.assertEqual(connection.execute(
            "SELECT planning_error FROM projects WHERE repo = 'repo'",
        ).fetchone()[0], "old unsafe")
        self.assertIsNone(connection.execute(
            "SELECT released_at FROM notified_block_incidents WHERE id = ?",
            (incident,),
        ).fetchone()[0])
        self.assertTrue(repository.release_planning_correction("repo", **arguments))
        self.assertIsNone(connection.execute(
            "SELECT planning_error FROM projects WHERE repo = 'repo'",
        ).fetchone()[0])
        self.assertIsNone(connection.execute(
            "SELECT planning_error_origin FROM projects WHERE repo = 'repo'",
        ).fetchone()[0])
        self.assertFalse(repository.release_planning_correction("repo", **arguments))

    def test_planning_error_release_rolls_back_with_incident_on_interruption(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        notifications.configure_session(connection, enabled=True)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.update_project("repo", {"planning_error": "owner mismatch"})
        incident = connection.execute(
            "SELECT id, event_key FROM notified_block_incidents "
            "WHERE repo = 'repo' AND subject = 'planning'",
        ).fetchone()
        connection.commit()

        for stage in ("after_planning_block_release", "after_project_update"):
            with self.subTest(stage=stage):
                def interrupt(current: str) -> None:
                    if current == stage:
                        raise RuntimeError(stage)

                with self.assertRaisesRegex(RuntimeError, stage):
                    runtime_state.RuntimeStateRepository(
                        connection, fault_hook=interrupt,
                    ).update_project("repo", {"planning_error": None})
                self.assertEqual(connection.execute(
                    "SELECT planning_error FROM projects WHERE repo = 'repo'",
                ).fetchone()[0], "owner mismatch")
                self.assertIsNone(connection.execute(
                    "SELECT released_at FROM notified_block_incidents WHERE id = ?",
                    (incident["id"],),
                ).fetchone()[0])
                self.assertIsNone(connection.execute(
                    "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
                    (incident["event_key"],),
                ).fetchone()[0])

        repository.update_project("repo", {"planning_error": None})
        self.assertIsNone(connection.execute(
            "SELECT planning_error FROM projects WHERE repo = 'repo'",
        ).fetchone()[0])
        self.assertIsNotNone(connection.execute(
            "SELECT released_at FROM notified_block_incidents WHERE id = ?",
            (incident["id"],),
        ).fetchone()[0])
        self.assertIsNotNone(connection.execute(
            "SELECT canceled_at FROM notification_outbox WHERE event_key = ?",
            (incident["event_key"],),
        ).fetchone()[0])

    def test_recovery_slot_waits_for_preserved_planning_attempt(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        for values in (
            {"phase": "PLANNING"},
            {"phase": None, "planning_error": "planner failed"},
            {"phase": None, "planning_error_fingerprint": "failed-input"},
            {"phase": None, "planning_workspace_owned": 1},
            {"phase": None, "planning_retry_count": 2},
            {"phase": None, "planning_result_completed": 1},
        ):
            with self.subTest(values=values):
                connection.execute(
                    "UPDATE projects SET phase = NULL, planning_error = NULL, "
                    "planning_error_fingerprint = NULL, planning_workspace_owned = 0, "
                    "planning_retry_count = 0, planning_result_completed = 0 "
                    "WHERE repo = 'repo'"
                )
                connection.execute(
                    "UPDATE projects SET planning_attempt_fingerprint = NULL "
                    "WHERE repo = 'repo'"
                )
                repository.update_project('repo', values, enqueue=False)
                self.assertFalse(repository.recovery_slot_available('repo'))
        connection.execute(
            "UPDATE projects SET planning_result_completed = 0 WHERE repo = 'repo'"
        )
        self.assertTrue(repository.recovery_slot_available('repo'))

    def test_activation_projects_owned_checkpoint_remote_oid(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE exec_plans SET publication_remote_oid = ? "
            "WHERE repo = 'repo' AND plan_id = 'G-047'", ('a' * 40,),
        )
        connection.commit()

        activated = runtime_state.RuntimeStateRepository(connection).activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )

        self.assertEqual(activated.publication_remote_oid, 'a' * 40)
        project = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()
        self.assertEqual(project['publication_remote_oid'], 'a' * 40)

    def test_blocked_requeue_discards_stale_worker_profile_target(self):
        from loop_manager_internal.execution.worker_session import SessionBinding, SessionTransition

        connection = runtime_connection()
        self.addCleanup(connection.close)
        old = SessionBinding('codex', 'old', 'low')
        target = SessionBinding('codex', 'escalated', 'high')
        pending = SessionTransition('switch', 'replacement', 'model_changed',
                                    'old-session', old, target).serialize()
        connection.execute("""UPDATE exec_plans SET status = 'PLAN_BLOCKED',
            initial_worker_level = 1, effective_worker_level = 4,
            worker_session_binding = ?, worker_session_transition = ?
            WHERE repo = 'repo' AND plan_id = 'G-047'""", (old.serialize(), pending))
        connection.execute("""UPDATE projects SET phase = 'BLOCKED',
            plan_path = 'docs/plans/[G-047] Runtime.md',
            worker_session_binding = ?, worker_session_transition = ?
            WHERE repo = 'repo'""", (old.serialize(), pending))
        connection.commit()

        loop_manager._requeue_blocked_execplan(connection, 'repo', 'G-047')

        plan = connection.execute("SELECT * FROM exec_plans WHERE repo = 'repo'").fetchone()
        project = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()
        self.assertEqual((plan['status'], plan['effective_worker_level']), ('READY', 1))
        for row in (plan, project):
            self.assertIsNone(row['worker_session_id'])
            self.assertIsNone(row['worker_session_binding'])
            self.assertIsNone(row['worker_session_transition'])

    def test_activation_clears_stale_ready_session_transition(self):
        from loop_manager_internal.execution.worker_session import SessionBinding, SessionTransition

        connection = runtime_connection()
        self.addCleanup(connection.close)
        old = SessionBinding('codex', 'old', 'low')
        target = SessionBinding('codex', 'other', 'high')
        pending = SessionTransition('switch', 'replacement', 'model_changed',
                                    'old-session', old, target).serialize()
        connection.execute("""UPDATE exec_plans SET worker_session_id = 'old-session',
            worker_session_binding = ?, worker_session_transition = ?
            WHERE repo = 'repo' AND plan_id = 'G-047'""", (old.serialize(), pending))
        connection.commit()

        activated = runtime_state.RuntimeStateRepository(connection).activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )

        self.assertEqual(activated.status, 'IMPLEMENTING')
        self.assertIsNone(activated.worker_session_id)
        self.assertIsNone(activated.worker_session_binding)
        self.assertIsNone(activated.worker_session_transition)
        project = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()
        self.assertIsNone(project['worker_session_id'])
        self.assertIsNone(project['worker_session_binding'])
        self.assertIsNone(project['worker_session_transition'])

    def test_conflict_requeue_keeps_bound_session_but_drops_pending_target(self):
        from loop_manager_internal.execution.worker_session import SessionBinding, SessionTransition

        connection = runtime_connection()
        self.addCleanup(connection.close)
        old = SessionBinding('codex', 'old', 'low')
        target = SessionBinding('codex', 'escalated', 'high')
        pending = SessionTransition('switch', 'replacement', 'model_changed',
                                    'old-session', old, target).serialize()
        connection.execute("""UPDATE exec_plans SET status = 'PLAN_BLOCKED',
            pr_conflict_recovery = 1, initial_worker_level = 1,
            effective_worker_level = 4, worker_session_id = 'old-session',
            worker_session_binding = ?, worker_session_transition = ?
            WHERE repo = 'repo' AND plan_id = 'G-047'""", (old.serialize(), pending))
        connection.execute("""UPDATE projects SET phase = 'BLOCKED',
            plan_path = 'docs/plans/[G-047] Runtime.md',
            worker_session_id = 'old-session', worker_session_binding = ?,
            worker_session_transition = ? WHERE repo = 'repo'""",
            (old.serialize(), pending))
        connection.commit()

        loop_manager._requeue_blocked_execplan(connection, 'repo', 'G-047')

        plan = connection.execute("SELECT * FROM exec_plans WHERE repo = 'repo'").fetchone()
        project = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()
        self.assertEqual((plan['status'], plan['effective_worker_level'],
                          plan['worker_session_id']), ('IMPLEMENTING', 1, 'old-session'))
        for row in (plan, project):
            self.assertEqual(row['worker_session_id'], 'old-session')
            self.assertEqual(row['worker_session_binding'], old.serialize())
            self.assertIsNone(row['worker_session_transition'])

    def test_worker_transition_cas_preserves_execution_evidence(self):
        from loop_manager_internal.execution.worker_session import SessionBinding, decide_session
        from loop_manager_internal.agents.agent_invocation import ResolvedProfile

        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate('repo', 'G-047', activation_sha='base',
                            branch='loop-manager/G-047', worktree='/state/G-047')
        old = SessionBinding('codex', 'old', 'low')
        repository.update_project('repo', {
            'worker_session_id': 'old-session',
            'worker_session_binding': old.serialize(),
            'worker_result': 'saved work', 'verify_result': 'raw verify',
            'review_result': 'raw review', 'publication_token': 'published',
        })
        before = repository.projected('repo')
        pending = decide_session('old-session', old, None, 'codex',
                                 ResolvedProfile('new', 'high'), 'change-one').transition
        intent = repository.transition_from_snapshot(before,
            plan_values={'worker_session_id': None,
                         'worker_session_transition': pending.serialize()},
            project_values={'worker_session_id': None,
                            'worker_session_transition': pending.serialize()})
        self.assertIsNone(intent.worker_session_id)
        self.assertEqual(intent.worker_session_transition, pending.serialize())
        self.assertEqual((intent.worker_result, intent.verify_result,
                          intent.review_result, intent.publication_token),
                         ('saved work', 'raw verify', 'raw review', 'published'))
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.observe_worker_session(before, pending.identity,
                                              'new-session', pending.target.serialize())
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.observe_worker_session(intent, 'wrong',
                                              'new-session', pending.target.serialize())
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.observe_worker_session(intent, pending.identity,
                                              'new-session', old.serialize())
        observed = repository.observe_worker_session(intent, pending.identity,
            'new-session', pending.target.serialize())
        self.assertEqual(observed.worker_session_id, 'new-session')
        self.assertIsNone(observed.worker_session_transition)
        self.assertEqual(observed.worker_session_binding, pending.target.serialize())

    def test_nonfinal_recovery_decision_cannot_enter_block_transaction(self):
        connection = runtime_connection()
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        connection.execute(
            "UPDATE projects SET agent_input_fingerprint = 'saved-input' "
            "WHERE repo = 'repo'"
        )
        connection.commit()
        retry = decide(RecoveryObservation(
            RecoveryReason.GIT_LOCK, 'repo', 'G-047', 'IMPLEMENTING',
            'plan-sha', 'base', 'lock', 'worker-session', 'git busy',
        ))

        with self.assertRaises(ValueError):
            loop_manager.block_active_execplan_from_revision(
                connection, Path('/repo'), 'repo', 'base', 'git busy',
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
                recovery_decision=retry,
            )
        self.assertEqual(repository.get('repo', 'G-047').status, 'IMPLEMENTING')
        self.assertIsNone(connection.execute(
            "SELECT * FROM recovery_records WHERE repo = 'repo'"
        ).fetchone())
        self.assertIsNone(connection.execute(
            "SELECT * FROM execplan_blocks WHERE repo = 'repo'"
        ).fetchone())

    def test_transaction_reserves_writer_before_read_and_preserves_nesting(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE state (value INTEGER)")
        statements = []
        connection.set_trace_callback(statements.append)
        with runtime_state._transaction(connection):
            connection.execute("SELECT * FROM state").fetchall()
            connection.execute("INSERT INTO state VALUES (1)")
        self.assertEqual(statements[0], "BEGIN IMMEDIATE")
        self.assertFalse(connection.in_transaction)
        with self.assertRaises(KeyboardInterrupt):
            with runtime_state._transaction(connection):
                connection.execute("INSERT INTO state VALUES (2)")
                raise KeyboardInterrupt()
        self.assertFalse(connection.in_transaction)
        connection.execute("BEGIN")
        connection.execute("INSERT INTO state VALUES (3)")
        with self.assertRaises(ValueError):
            with runtime_state._transaction(connection):
                connection.execute("INSERT INTO state VALUES (4)")
                raise ValueError("rollback nested write")
        with runtime_state._transaction(connection):
            connection.execute("INSERT INTO state VALUES (5)")
        self.assertTrue(connection.in_transaction)
        self.assertEqual(connection.execute("SELECT value FROM state").fetchall(), [(1,), (3,), (5,)])
        connection.rollback()
        self.assertEqual(connection.execute("SELECT value FROM state").fetchall(), [(1,)])


    def test_active_hydration_preserves_planner_scope_and_interrupted_attempt(self):
        owners = (
            {"phase": "PLANNING"},
            {"planning_attempt_fingerprint": "saved-attempt"},
            {"planning_error": "planner failed"},
            {"planning_error_fingerprint": "failed-input"},
            {"planning_workspace_owned": 1},
            {"planning_retry_count": 2},
            {"planner_session_id": "planner-session"},
            {"planning_validation_error": "invalid-result"},
            {"planning_result_pending_validation": 1},
            {"planning_result_completed": 1},
            {"planning_result": "saved-result"},
        )
        for owner in owners:
            with self.subTest(owner=owner):
                connection = runtime_connection()
                self.addCleanup(connection.close)
                connection.execute(
                    "UPDATE exec_plans SET status='IMPLEMENTING', "
                    "branch='loop-manager/G-047', worktree='/worker', "
                    "worker_session_id='worker-session', agent_snapshot='worker-scope', "
                    "guidance_plan_hash='user-decision' WHERE repo='repo'"
                )
                connection.execute(
                    "UPDATE projects SET phase=NULL, planning_attempt_fingerprint=NULL, "
                    "worktree='/planner', agent_role='planner', "
                    "agent_snapshot='planner-scope' WHERE repo='repo'"
                )
                runtime_state.RuntimeStateRepository(connection).update_project(
                    'repo', owner, enqueue=False,
                )
                connection.commit()
                before_project = dict(connection.execute(
                    "SELECT * FROM projects WHERE repo='repo'"
                ).fetchone())
                before_plan = dict(connection.execute(
                    "SELECT * FROM exec_plans WHERE repo='repo'"
                ).fetchone())
                loop_manager.hydrate_active_project(connection, 'repo')
                self.assertEqual(dict(connection.execute(
                    "SELECT * FROM projects WHERE repo='repo'"
                ).fetchone()), before_project)
                self.assertEqual(dict(connection.execute(
                    "SELECT * FROM exec_plans WHERE repo='repo'"
                ).fetchone()), before_plan)

    def test_active_hydration_resumes_worker_when_planner_does_not_own_slot(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        connection.execute("UPDATE projects SET planning_attempt_fingerprint=NULL WHERE repo='repo'")
        connection.execute(
            "UPDATE exec_plans SET status='IMPLEMENTING', branch='loop-manager/G-047', "
            "worktree='/worker', worker_session_id='worker-session', "
            "agent_role='worker', agent_snapshot='worker-scope', "
            "guidance_plan_hash='user-decision' WHERE repo='repo'"
        )
        connection.commit()
        preserved_fields = ("status", "branch", "worktree", "workspace_owned",
                            "worker_session_id", "agent_role", "agent_snapshot",
                            "guidance_plan_hash")
        before_plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo='repo'"
        ).fetchone()
        loop_manager.hydrate_active_project(connection, 'repo')
        row = connection.execute(
            "SELECT phase,branch,worktree,worker_session_id,agent_role,agent_snapshot, "
            "planning_fingerprint FROM projects WHERE repo='repo'"
        ).fetchone()
        self.assertEqual(tuple(row), ('WORKING', 'loop-manager/G-047', '/worker',
                                     'worker-session', 'worker', 'worker-scope',
                                     'planning-kept'))
        after_plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo='repo'"
        ).fetchone()
        self.assertEqual({field: after_plan[field] for field in preserved_fields},
                         {field: before_plan[field] for field in preserved_fields})

    def test_phase_mapping_and_field_ownership_are_declared_once(self):
        self.assertEqual(
            runtime_state.project_phase_for_status("IMPLEMENTING"), "WORKING"
        )
        for value in (
            "VERIFYING",
            "REVIEWING",
            "PUBLISHING",
            "PR_OPEN",
            "PLAN_BLOCKED",
        ):
            with self.subTest(value=value):
                self.assertEqual(runtime_state.project_phase_for_status(value), value)
                self.assertEqual(runtime_state.execplan_status_for_phase(value), value)
        self.assertEqual(
            runtime_state.execplan_status_for_phase("WORKING"), "IMPLEMENTING"
        )
        self.assertEqual(
            runtime_state.COMPATIBILITY_PHASE_BY_STATUS,
            {"IMPLEMENTING": "WORKING"},
        )
        self.assertIsNone(runtime_state.execplan_status_for_phase("PLANNING"))
        self.assertIsNone(runtime_state.execplan_status_for_phase(None))
        self.assertTrue(
            set(runtime_state.MIRRORED_FIELDS).isdisjoint(
                runtime_state.PROJECT_ONLY_FIELDS
            )
        )
        self.assertEqual(loop_manager.FIELDS, runtime_state.PROJECT_UPDATE_FIELDS)

    def test_planning_snapshot_is_typed_and_keeps_project_only_state(self):
        connection = runtime_connection()
        repository = runtime_state.RuntimeStateRepository(connection)

        snapshot = repository.planning("repo")

        self.assertEqual(snapshot.repo, "repo")
        self.assertEqual(snapshot.planning_fingerprint, "planning-kept")
        self.assertIsNone(snapshot.specification_fingerprint)
        self.assertEqual(
            snapshot.planning_attempt_fingerprint, "planning-cleared"
        )
        self.assertIsNone(snapshot.phase)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.phase = "PLANNING"

    def test_activation_update_hydrate_and_clear_round_trip_runtime_fields(self):
        connection = runtime_connection()
        repository = runtime_state.RuntimeStateRepository(connection)

        activated = repository.activate(
            "repo",
            "G-047",
            activation_sha="active-sha",
            branch="loop-manager/G-047",
            worktree="/state/G-047",
        )
        self.assertEqual(activated.status, "IMPLEMENTING")
        self.assertEqual(activated.phase, "WORKING")
        self.assertTrue(activated.ever_started)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            activated.status = "VERIFYING"

        project = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(
            (
                project["phase"],
                project["plan_path"],
                project["branch"],
                project["worktree"],
            ),
            (
                "WORKING",
                "docs/plans/[G-047] Runtime.md",
                "loop-manager/G-047",
                "/state/G-047",
            ),
        )

        repository.update_project(
            "repo",
            {
                "phase": "REVIEWING",
                "worker_session_id": "worker-session",
                "reviewer_session_id": "reviewer-session",
                "worker_result": "worker-result",
                "verify_result": "verify-result",
                "review_result": "review-result",
                "validated_base_sha": "base-sha",
                "publication_remote_oid": "remote-sha",
                "pr_url": "https://example.test/pr/47",
                "pr_head_oid": "head-sha",
                "agent_role": "reviewer",
                "agent_snapshot": "snapshot",
                "agent_input_fingerprint": "fingerprint",
                "planning_fingerprint": "project-only",
            },
        )
        snapshot = repository.get("repo", "G-047")
        self.assertEqual(snapshot.status, "REVIEWING")
        self.assertEqual(snapshot.worker_session_id, "worker-session")
        self.assertEqual(snapshot.reviewer_session_id, "reviewer-session")
        self.assertEqual(snapshot.verify_result, "verify-result")
        self.assertEqual(snapshot.publication_remote_oid, "remote-sha")
        self.assertEqual(snapshot.agent_role, "reviewer")
        self.assertNotIn(
            "planning_fingerprint",
            {row[1] for row in connection.execute("PRAGMA table_info(exec_plans)")},
        )

        connection.execute(
            "UPDATE projects SET phase = NULL, worker_session_id = NULL, "
            "reviewer_session_id = NULL, plan_path = NULL WHERE repo = 'repo'"
        )
        connection.commit()
        repository.hydrate("repo", "G-047")
        hydrated = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(hydrated["phase"], "REVIEWING")
        self.assertEqual(hydrated["worker_session_id"], "worker-session")
        self.assertEqual(hydrated["reviewer_session_id"], "reviewer-session")
        self.assertEqual(hydrated["planning_fingerprint"], "project-only")

        repository.clear("repo")
        cleared = connection.execute(
            "SELECT * FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertIsNone(cleared["phase"])
        self.assertIsNone(cleared["plan_path"])
        self.assertIsNone(cleared["worker_session_id"])
        self.assertIsNone(cleared["planning_attempt_fingerprint"])
        self.assertEqual(cleared["planning_fingerprint"], "project-only")
        self.assertEqual(repository.get("repo", "G-047").status, "REVIEWING")

    def test_compatibility_wrappers_preserve_public_update_and_hydrate_contract(self):
        connection = runtime_connection()
        loop_manager.activate_ready_execplan(
            connection,
            "repo",
            loop_manager.Path("/state"),
            "active-sha",
        )
        loop_manager.update_project(
            connection,
            "repo",
            phase="VERIFYING",
            worker_session_id="worker-session",
        )
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = 'repo' AND plan_id = 'G-047'"
        ).fetchone()
        self.assertEqual((plan["status"], plan["worker_session_id"]), (
            "VERIFYING", "worker-session"
        ))
        connection.execute(
            "UPDATE projects SET phase = NULL, plan_path = NULL WHERE repo = 'repo'"
        )
        connection.commit()
        loop_manager.hydrate_execplan_project(connection, "repo", plan)
        project = connection.execute(
            "SELECT phase, plan_path FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(tuple(project), (
            "VERIFYING", "docs/plans/[G-047] Runtime.md"
        ))
        loop_manager.clear_active(connection, "repo")
        self.assertIsNone(
            connection.execute(
                "SELECT phase FROM projects WHERE repo = 'repo'"
            ).fetchone()[0]
        )
        with self.assertRaisesRegex(ValueError, "유효하지 않은 프로젝트 상태 필드"):
            loop_manager.update_project(connection, "repo", unknown="value")
        loop_manager._mirror_project_update_to_execplan(
            connection,
            "repo",
            connection.execute(
                "SELECT plan_path FROM projects WHERE repo = 'repo'"
            ).fetchone(),
            {"unknown": "ignored"},
        )
        loop_manager.update_project(connection, "missing", phase="WORKING")
        loop_manager.clear_active(connection, "missing")
        loop_manager.hydrate_execplan_project(connection, "missing", plan)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('other', 'main')"
        )
        loop_manager.hydrate_execplan_project(connection, "other", plan)
        self.assertEqual(
            connection.execute(
                "SELECT plan_path FROM projects WHERE repo = 'other'"
            ).fetchone()[0],
            "docs/plans/[G-047] Runtime.md",
        )

        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        legacy.row_factory = sqlite3.Row
        legacy.execute(loop_manager.SCHEMA)
        legacy.execute(
            "INSERT INTO projects (repo, base_branch, plan_path, phase) "
            "VALUES ('legacy', 'main', "
            "'docs/plans/[G-047] Runtime.md', 'WORKING')"
        )
        loop_manager.update_project(
            legacy, "legacy", worker_session_id="legacy-session"
        )
        self.assertIsNotNone(
            legacy.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'exec_plans'"
            ).fetchone()
        )

    def test_workspace_reservation_and_merge_are_repository_operations(self):
        connection = runtime_connection()
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.update_plan(
            "repo",
            "G-047",
            plan_values={"workspace_owned": 0},
            enqueue=False,
        )

        def fail_after_update(stage: str) -> None:
            if stage == "after_execplan_update":
                raise RuntimeError(stage)

        with self.assertRaisesRegex(RuntimeError, "after_execplan_update"):
            runtime_state.RuntimeStateRepository(
                connection,
                fault_hook=fail_after_update,
            ).reserve_workspace(
                "repo",
                "G-047",
                reserved_value=-1,
                allowed_values=(0, -1),
            )
        self.assertEqual(repository.get("repo", "G-047").workspace_owned, 0)

        reserved = repository.reserve_workspace(
            "repo",
            "G-047",
            reserved_value=-1,
            allowed_values=(0, -1),
        )
        self.assertEqual(reserved.workspace_owned, -1)
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.reserve_workspace(
                "repo",
                "G-047",
                reserved_value=-1,
                allowed_values=(0,),
            )
        self.assertEqual(repository.get("repo", "G-047").workspace_owned, -1)

        repository.activate(
            "repo",
            "G-047",
            activation_sha="active-sha",
            branch="loop-manager/G-047",
            worktree="/state/G-047",
        )
        merged = repository.mark_merged(
            "repo",
            "G-047",
            project_values=runtime_state.clear_project_values(
                processed_sha="merged-sha",
                planning_fingerprint="merged-cycle",
            ),
        )
        project = connection.execute(
            "SELECT phase, plan_path, processed_sha, planning_fingerprint "
            "FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(merged.status, "MERGED")
        self.assertEqual(
            tuple(project),
            (None, None, "merged-sha", "merged-cycle"),
        )

    def test_activation_rollback_restores_both_source_and_projection(self):
        for stage in ("after_execplan_update", "after_project_update"):
            with self.subTest(stage=stage):
                connection = runtime_connection()

                def fail(current: str) -> None:
                    if current == stage:
                        raise RuntimeError(stage)

                repository = runtime_state.RuntimeStateRepository(
                    connection, fault_hook=fail
                )
                with self.assertRaisesRegex(RuntimeError, stage):
                    repository.activate(
                        "repo",
                        "G-047",
                        activation_sha="active-sha",
                        branch="loop-manager/G-047",
                        worktree="/state/G-047",
                    )
                plan = connection.execute(
                    "SELECT status, active_sha, ever_started FROM exec_plans "
                    "WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                project = connection.execute(
                    "SELECT phase, active_sha, plan_path FROM projects "
                    "WHERE repo = 'repo'"
                ).fetchone()
                self.assertEqual(tuple(plan), ("READY", None, 0))
                self.assertEqual(tuple(project), (None, None, None))

                activated = runtime_state.RuntimeStateRepository(
                    connection
                ).activate(
                    "repo",
                    "G-047",
                    activation_sha="active-sha",
                    branch="loop-manager/G-047",
                    worktree="/state/G-047",
                )
                self.assertEqual(activated.status, "IMPLEMENTING")

    def test_expected_status_mismatch_rejects_second_activation(self):
        connection = runtime_connection()
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            "repo",
            "G-047",
            activation_sha="active-sha",
            branch="loop-manager/G-047",
            worktree="/state/G-047",
        )
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.activate(
                "repo",
                "G-047",
                activation_sha="different",
                branch="different",
                worktree="different",
            )
        self.assertEqual(repository.get("repo", "G-047").active_sha, "active-sha")

    def test_stage_revision_tokens_are_snapshot_bound_and_projected_atomically(self):
        connection = runtime_connection()
        repository = runtime_state.RuntimeStateRepository(connection)
        original = repository.activate(
            "repo",
            "G-047",
            activation_sha="base",
            branch="loop-manager/G-047",
            worktree="/state/G-047",
        )

        revised = repository.transition_from_snapshot(
            original,
            plan_values={
                "definition_snapshot": '{"revision":"new"}',
                "worker_input_token": "worker-token",
                "archive_token": "archive-token",
            },
            project_values={
                "definition_snapshot": '{"revision":"new"}',
                "worker_input_token": "worker-token",
                "archive_token": "archive-token",
            },
        )

        project = connection.execute(
            "SELECT definition_snapshot, worker_input_token, archive_token "
            "FROM projects WHERE repo = 'repo'"
        ).fetchone()
        self.assertEqual(
            tuple(project),
            ('{"revision":"new"}', "worker-token", "archive-token"),
        )
        self.assertEqual(revised.worker_input_token, "worker-token")
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.transition_from_snapshot(
                original,
                plan_values={"verify_token": "stale"},
                project_values={"verify_token": "stale"},
            )
        self.assertIsNone(repository.get("repo", "G-047").verify_token)

    def test_outbox_failures_roll_back_source_projection_and_event(self):
        for stage in ("before_outbox_enqueue", "after_outbox_enqueue"):
            with self.subTest(stage=stage):
                connection = runtime_connection()
                repository = runtime_state.RuntimeStateRepository(connection)
                repository.activate(
                    "repo",
                    "G-047",
                    activation_sha="active-sha",
                    branch="loop-manager/G-047",
                    worktree="/state/G-047",
                )
                repository.update_project("repo", {"phase": "PUBLISHING"})
                notifications.configure_session(connection, enabled=True)
                connection.commit()

                def fail(current: str) -> None:
                    if current == stage:
                        raise RuntimeError(stage)

                failing = runtime_state.RuntimeStateRepository(
                    connection, fault_hook=fail
                )
                with self.assertRaisesRegex(RuntimeError, stage):
                    failing.open_pull_request(
                        "repo",
                        pr_url="https://example.test/pr/47",
                        pr_head_oid="head-sha",
                    )
                plan = connection.execute(
                    "SELECT status, pr_url FROM exec_plans "
                    "WHERE repo = 'repo' AND plan_id = 'G-047'"
                ).fetchone()
                project = connection.execute(
                    "SELECT phase, plan_path, pr_url FROM projects WHERE repo = 'repo'"
                ).fetchone()
                outbox_count = connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox"
                ).fetchone()[0]
                self.assertEqual(tuple(plan), ("PUBLISHING", None))
                self.assertEqual(
                    tuple(project),
                    ("PUBLISHING", "docs/plans/[G-047] Runtime.md", None),
                )
                self.assertEqual(outbox_count, 0)

    def test_existing_database_schema_and_runtime_values_survive_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            connection = loop_manager.connect_database(state_dir, create=True)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, provider, active_sha, validated_base_sha,
                    publication_remote_oid, plan_sha, phase, branch, worktree,
                    plan_path, pr_url, pr_head_oid, worker_session_id,
                    reviewer_session_id, worker_result, verify_result,
                    review_result, agent_role, agent_snapshot,
                    agent_input_fingerprint
                ) VALUES (
                    'repo', 'main', 'codex', 'active-sha', 'base-sha',
                    'remote-sha', 'plan-sha', 'VERIFYING',
                    'loop-manager/G-047', '/state/G-047',
                    'docs/plans/[G-047] Runtime.md',
                    'https://example.test/pr/47', 'head-sha',
                    'worker-session', 'reviewer-session', 'worker-result',
                    'verify-result', 'review-result', 'worker', 'snapshot',
                    'fingerprint'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, plan_sha, active_sha,
                    validated_base_sha, publication_remote_oid, branch,
                    worktree, pr_url, pr_head_oid, worker_session_id,
                    reviewer_session_id, worker_result, verify_result,
                    review_result, workspace_owned, agent_role, agent_snapshot,
                    agent_input_fingerprint
                ) VALUES (
                    'repo', 'G-047', 'docs/plans/[G-047] Runtime.md',
                    'Runtime', '["docs/PRODUCT.md"]', '[]', 'normal',
                    'hash', 'VERIFYING', 'plan-sha', 'active-sha', 'base-sha',
                    'remote-sha', 'loop-manager/G-047', '/state/G-047',
                    'https://example.test/pr/47', 'head-sha',
                    'worker-session', 'reviewer-session', 'worker-result',
                    'verify-result', 'review-result', 1, 'worker', 'snapshot',
                    'fingerprint'
                )
                """
            )
            connection.commit()
            schema_before = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            )
            connection.close()

            resumed = loop_manager.connect_database(state_dir)
            self.addCleanup(resumed.close)
            schema_after = tuple(
                tuple(row)
                for row in resumed.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            )
            snapshot = runtime_state.RuntimeStateRepository(resumed).get(
                "repo", "G-047"
            )
            loop_manager.hydrate_active_project(resumed, "repo")
            project = resumed.execute(
                "SELECT phase, branch, worktree, pr_url, pr_head_oid, "
                "worker_session_id, reviewer_session_id, verify_result, "
                "publication_remote_oid, agent_role, agent_snapshot, "
                "agent_input_fingerprint FROM projects WHERE repo = 'repo'"
            ).fetchone()
            resumed.close()

        self.assertEqual(schema_after, schema_before)
        self.assertEqual(snapshot.status, "VERIFYING")
        self.assertEqual(snapshot.phase, "VERIFYING")
        self.assertEqual(snapshot.workspace_owned, 1)
        self.assertEqual(
            tuple(project),
            (
                "VERIFYING",
                "loop-manager/G-047",
                "/state/G-047",
                "https://example.test/pr/47",
                "head-sha",
                "worker-session",
                "reviewer-session",
                "verify-result",
                "remote-sha",
                "worker",
                "snapshot",
                "fingerprint",
            ),
        )


class PreservedPlanningRuntimeTest(unittest.TestCase):
    def failed_repository(self):
        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.update_project('repo', {
            'phase': 'PLANNING', 'active_sha': 'failed-base', 'worktree': '/planning',
            'planning_workspace_owned': 1, 'planning_error': 'validation failed',
            'planning_error_fingerprint': 'failed-input', 'planner_session_id': 'planner-session',
            'planning_result': 'unpublished-result', 'planning_validation_error': 'feedback',
            'agent_role': 'planner', 'agent_snapshot': 'saved-scope',
            'agent_result_snapshot': 'failed-result-scope',
        }, enqueue=False)
        return connection, repository

    def test_worker_pr_and_merge_clear_preserve_attempt_and_error_incident(self):
        connection, repository = self.failed_repository()
        before = repository.planning('repo')
        repository.suspend_planning(before)
        suspended = repository.planning('repo')
        self.assertTrue(repository.recovery_slot_available('repo'))
        worker = repository.activate('repo', 'G-047', activation_sha='base',
                                     branch='loop-manager/G-047', worktree='/worker')
        repository.transition('repo', worker.plan_id, plan_values={
            'worker_session_id': 'worker-session', 'agent_role': 'worker',
            'agent_snapshot': 'worker-scope'}, project_values={
            'worker_session_id': 'worker-session', 'agent_role': 'worker',
            'agent_snapshot': 'worker-scope'}, enqueue=False)
        restarted = runtime_state.RuntimeStateRepository(connection)
        restarted.hydrate('repo', worker.plan_id)
        with self.assertRaises(runtime_state.RuntimeStateError):
            restarted.resume_planning(restarted.planning('repo'))
        restarted.open_pull_request('repo', pr_url='https://example.test/pull/1')
        restarted.mark_merged('repo', worker.plan_id)
        restarted.clear('repo')
        after = restarted.planning('repo')
        self.assertEqual(after.planning_preserved_state, suspended.planning_preserved_state)
        self.assertEqual(after.planning_error_incident_id, before.planning_error_incident_id)
        self.assertEqual(after.planning_error, before.planning_error)
        self.assertEqual(after.planning_fingerprint, before.planning_fingerprint)
        restarted.resume_planning(after)
        self.assertEqual(restarted.planning('repo'), before)
        self.assertEqual(restarted.get('repo', worker.plan_id).worker_session_id, 'worker-session')
        self.assertFalse(restarted.recovery_slot_available('repo'))

    def test_suspension_and_resume_rollback_without_losing_projection(self):
        for stage in ('after_planning_suspend', 'after_planning_resume'):
            with self.subTest(stage=stage):
                connection, repository = self.failed_repository()
                if stage.endswith('resume'):
                    repository.suspend_planning(repository.planning('repo'))
                before = dict(connection.execute('SELECT * FROM projects').fetchone())
                def fail(current):
                    if current == stage:
                        raise RuntimeError('interrupted')
                failing = runtime_state.RuntimeStateRepository(connection, fault_hook=fail)
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    if stage.endswith('resume'):
                        failing.resume_planning(repository.planning('repo'))
                    else:
                        failing.suspend_planning(repository.planning('repo'))
                self.assertEqual(dict(connection.execute('SELECT * FROM projects').fetchone()), before)

    def test_unverified_or_damaged_preservation_never_releases_slot(self):
        connection, repository = self.failed_repository()
        self.assertFalse(repository.recovery_slot_available('repo'))
        repository.suspend_planning(repository.planning('repo'))
        connection.execute("UPDATE projects SET planning_preserved_state = '{}' WHERE repo = 'repo'")
        self.assertFalse(repository.recovery_slot_available('repo'))
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.planning('repo')

    def test_stale_snapshot_cannot_suspend_or_resume(self):
        _connection, repository = self.failed_repository()
        original = repository.planning('repo')
        repository.update_project('repo', {'planning_result': 'new-result'}, enqueue=False)
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.suspend_planning(original)
        repository.suspend_planning(repository.planning('repo'))
        suspended = repository.planning('repo')
        repository.update_project('repo', {'phase': 'WORKING'}, enqueue=False)
        with self.assertRaises(runtime_state.RuntimeStateError):
            repository.resume_planning(suspended)

    def test_pr_conflict_hydration_and_scope_failure_preserve_planner(self):
        _connection, repository = self.failed_repository()
        repository.suspend_planning(repository.planning('repo'))
        saved = repository.planning('repo').planning_preserved_state
        repository.update_plan('repo', 'G-047', plan_values={
            'status': 'PR_OPEN', 'pr_url': 'https://example.test/pull/1',
            'worker_session_id': 'worker-session'}, enqueue=False)
        repository.transition('repo', 'G-047', expected_status='PR_OPEN',
                              plan_values={'status': 'IMPLEMENTING', 'pr_conflict_recovery': 1},
                              project_values={'phase': 'WORKING', 'plan_path': 'docs/plans/[G-047] Runtime.md'},
                              enqueue=False)
        repository.hydrate('repo', 'G-047')
        repository.transition_and_clear('repo', 'G-047', plan_values={'status': 'PLAN_BLOCKED'},
                                        enqueue=False)
        self.assertEqual(repository.planning('repo').planning_preserved_state, saved)
        self.assertEqual(repository.get('repo', 'G-047').worker_session_id, 'worker-session')
