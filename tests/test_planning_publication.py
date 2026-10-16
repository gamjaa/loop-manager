from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.maintenance.maintenance_file_inventory import RootPlacement, TrackedSource
from tests.support.maintenance import result_for
from loop_manager_internal.planning.planning_catalog import MutableReadySnapshot, mutable_snapshot_token
from loop_manager_internal.storage.planning_publication_state import (
    GeneralPlanningPublication, general_planning_publication,
    record_general_planning_publication,
)
from loop_manager_internal.storage.database import GENERAL_PLANNING_PUBLICATIONS_SCHEMA
from loop_manager_internal.maintenance.maintenance_orchestration import maintenance_planning_fingerprint
from loop_manager_internal.planning.planning_runtime import (
    PlanningAgentResult,
    PlanningAgentResultKind,
    PlanningContext,
    PlanningLifecycleCoordinator,
    PlanningOutcomeKind,
    PlanningResultInvalid,
    PlanningScopeResult,
    PlanningWorkspaceOwnershipError,
    PlanningDecisionKind,
    choose_planning_decision,
)
from loop_manager_internal.storage.runtime_state import PlanningSnapshot


from tests.support.planning_case import (
    PlanningCase, FakePorts, FakeStatePort, FakeGitPort, FakeAgentPort,
    FakeValidationPort, FakeEventPort, planning_snapshot, maintenance_cycle,
)

class PlanningPublicationTest(PlanningCase):
    def test_new_manager_source_publication_requires_critical_before_db_confirmation(self):
        from loop_manager_internal.catalog import execplan_catalog, manager_sources
        from loop_manager_internal.planning import planner_publication
        from loop_manager_internal.storage import database, verify_input_requests

        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        database._ensure_execplans_schema(connection)
        repo = "/repo"
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,))
        request = verify_input_requests.register(connection, repo, "a" * 40)
        connection.commit()
        path = "docs/plans/[G-900] Verify Inputs.md"
        worktree = Path("/publication")

        def make_text(priority):
            return (
                "---\nid: G-900\ntitle: Verify Inputs\nsources:\n  - "
                + manager_sources.SOURCE
                + "\ndepends_on: []\ncomplexity: low\nrisk: medium\npriority: "
                + priority
                + "\n---\n\n## Purpose\n\n"
                + manager_sources.RULE
                + "\n\n## Acceptance Criteria\n\n- Keep scripts/verify-inputs.json current.\n\n"
                + "## Decision Log\n\n"
                + manager_sources.PREFIX
                + json.dumps(request, sort_keys=True)
                + "\n"
            )

        def publish(text):
            plan = execplan_catalog.parse_execplan_text(path, text)
            verify_input_requests.validate(connection, repo, text, plan["sources"])
            row_effects = planner_publication.RegisterExecplanRowsEffects(
                _ensure_execplans_schema=database._ensure_execplans_schema,
                file_hash=lambda _path: execplan_catalog.plan_content_hash(text),
                git=mock.Mock(),
                git_bytes=mock.Mock(return_value=text.encode("utf-8")),
            )
            effects = planner_publication.RegisterExecplansEffects(
                _register_execplan_rows=lambda db, target_repo, root, plans, commit:
                    planner_publication._register_execplan_rows(
                        row_effects, db, target_repo, root, plans, commit,
                    ),
            )
            planner_publication.register_execplans(
                effects, connection, repo, worktree, [(path, plan)], "published", "fingerprint",
            )

        for priority in ("high", "normal", "low"):
            with self.subTest(priority=priority):
                with self.assertRaisesRegex(RuntimeError, "priority: critical"):
                    publish(make_text(priority))
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM exec_plans WHERE repo=? AND plan_id='G-900'", (repo,),
                ).fetchone())
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM execplan_drafts WHERE repo=? AND plan_id='G-900'", (repo,),
                ).fetchone())
                project = connection.execute(
                    "SELECT processed_sha FROM projects WHERE repo=?", (repo,),
                ).fetchone()
                self.assertIsNotNone(project)
                self.assertIsNone(project[0])

        publish(make_text("critical"))
        row = connection.execute(
            "SELECT priority,status FROM exec_plans WHERE repo=? AND plan_id='G-900'", (repo,),
        ).fetchone()
        self.assertEqual(tuple(row), ("critical", "READY"))
        self.assertEqual(connection.execute(
            "SELECT processed_sha FROM projects WHERE repo=?", (repo,),
        ).fetchone()[0], "published")

    def test_publication_uses_current_changes_after_manager_validation(self):
        path = "docs/plans/[G-001] Plan.md"
        for manager_changed in (True, False):
            with self.subTest(manager_changed=manager_changed), tempfile.TemporaryDirectory() as directory:
                worktree = Path(directory)
                target = worktree / path
                target.parent.mkdir(parents=True)
                target.write_text("validated decision log", encoding="utf-8")
                ports = FakePorts(planning_snapshot(phase="PLANNING", active_sha="base",
                    worktree=directory, planning_workspace_owned=1))
                ports.git.dirty_files = [path] if manager_changed else []
                ports.git.head = "base" if manager_changed else "plan-commit"
                ports.git.remote = "concurrent-base"
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                run.planning_worktree = worktree
                run.validated_result = ports.validation.result
                run.validated_result.plans = ()
                run.validated_result.revisions = (object(),)
                run.dirty_files = [] if manager_changed else [path]
                run.current_fingerprint = run.general_fingerprint = "fingerprint"
                run.specification_fingerprint = "specification"
                original_intent = ports.state.record_general_intent
                def record_intent(*args):
                    self.assertEqual(args[3], "plan-commit")
                    original_intent(*args)
                ports.state.record_general_intent = record_intent

                outcome = coordinator._publication._publish_planner_result(run)

                self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
                commands = [args for _repo, args, _check in ports.git.calls]
                self.assertEqual(sum(args[0] == "commit" for args in commands), int(manager_changed))
                if manager_changed:
                    self.assertIn(("add", "--", f":(literal){path}"), commands)
                self.assertEqual(ports.state.general_intent_record_count, 1)

    def _reviewed_publication_connection(self):
        from loop_manager_internal.storage import planning_review_state as reviews
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
        reviews.begin(connection, '/repo', purpose='general', revision='base',
            input_fingerprint='input', specification_fingerprint='spec',
            work_cycle='cycle', comparison_revision=None)
        reviews.validate(connection, '/repo', 'base', 'validated review', 'complete')
        connection.commit()
        return connection

    def test_requirement_publication_preserves_general_baseline_and_recovers_without_model(self):
        from loop_manager_internal.planning import planner_publication
        from loop_manager_internal.storage import planning_review_state as reviews, planning_input_state
        connection = self._reviewed_publication_connection()
        connection.execute("INSERT INTO projects (repo, base_branch, processed_sha, specification_fingerprint, planning_fingerprint) VALUES ('/repo', 'main', 'old', 'old-spec', 'old-input')")
        old = {'docs/plans/Decision.md': ('blob', 'old-answer')}
        planning_input_state.record_general(connection, '/repo', 'old', old)
        connection.execute("UPDATE planning_reviews SET purpose='requirement' WHERE slot='attempt'")
        connection.commit()
        snapshot = MutableReadySnapshot('/repo', 'base', (), 'token')
        result = loop_manager.ValidatedPlanningResult(plans=(), publication_paths=(), mutable_snapshot=snapshot)
        intent = SimpleNamespace(stage='prepared', base_sha='base', commit_sha='commit',
            snapshot_token='token', specification_fingerprint='spec', planning_fingerprint='input')
        clear = mock.Mock()
        effects = planner_publication.ApplyGeneralPlanningBatchEffects(
            mock.Mock(), clear, mock.Mock(return_value=False), mock.Mock(), mock.Mock(),
            mock.Mock(return_value=intent), mock.Mock(return_value=False), mock.Mock())
        with mock.patch.object(planner_publication.planning_publication_state, 'apply_mutable_catalog_changes'), \
             mock.patch('loop_manager_internal.catalog.remote_deletions.resolve_recovery'):
            planner_publication._apply_general_planning_batch(effects, connection, '/repo',
                Path('/worktree'), result, 'commit', 'spec', 'input')
        self.assertEqual(clear.call_args.kwargs['processed_sha'], 'old')
        self.assertEqual(clear.call_args.kwargs['planning_fingerprint'], 'old-input')
        self.assertTrue(planning_input_state.matches(connection, '/repo', old))
        self.assertEqual(reviews.get(connection, '/repo', 'requirement')['confirmed_revision'], 'commit')
        self.assertIsNone(reviews.get(connection, '/repo', 'general'))
        applied = planner_publication.GeneralPlanningBatchAlreadyAppliedEffects(mock.Mock(), mock.Mock(), mock.Mock())
        self.assertTrue(planner_publication._general_planning_batch_already_applied(applied,
            connection, '/repo', Path('/worktree'), result, 'commit', 'spec', 'input'))

    def test_general_batch_confirms_review_and_pinned_guidance_before_clear_atomically(self):
        from loop_manager_internal.planning import planner_publication
        from loop_manager_internal.storage import planning_input_state, planning_review_state as reviews
        from loop_manager_internal.agents import common_guidance
        for fail_clear in (False, True):
            with self.subTest(fail_clear=fail_clear):
                connection = self._reviewed_publication_connection()
                snapshot = MutableReadySnapshot('/repo', 'base', (), 'token')
                result = loop_manager.ValidatedPlanningResult(plans=(), publication_paths=(), mutable_snapshot=snapshot)
                intent = SimpleNamespace(stage='prepared', base_sha='base', commit_sha='commit',
                    snapshot_token='token', specification_fingerprint='spec', planning_fingerprint='input')
                entries = {'docs/plans/G-001.md': ('G-001', 'plan-hash')}
                def clear(*_args, **_kwargs):
                    self.assertEqual(reviews.get(connection, '/repo', 'general')['confirmed_revision'], 'commit')
                    baseline = connection.execute('SELECT guidance_hash FROM planning_input_baselines').fetchone()
                    self.assertEqual(baseline['guidance_hash'], 'pinned-guidance')
                    self.assertTrue(planning_input_state.matches(connection, '/repo', entries))
                    if fail_clear:
                        raise RuntimeError('clear interrupted')
                effects = planner_publication.ApplyGeneralPlanningBatchEffects(
                    mock.Mock(), clear, mock.Mock(return_value=False), mock.Mock(), mock.Mock(),
                    mock.Mock(return_value=intent), mock.Mock(return_value=False), mock.Mock(return_value=entries))
                with mock.patch.object(planner_publication.planning_publication_state, 'apply_mutable_catalog_changes'), \
                     mock.patch('loop_manager_internal.catalog.remote_deletions.resolve_recovery'), \
                     mock.patch.object(common_guidance, 'planning_content_hash', return_value='pinned-guidance'), \
                     mock.patch.object(common_guidance, 'runtime_content_hash', side_effect=AssertionError('reload')):
                    if fail_clear:
                        with self.assertRaisesRegex(RuntimeError, 'clear interrupted'):
                            planner_publication._apply_general_planning_batch(effects, connection, '/repo',
                                Path('/worktree'), result, 'commit', 'spec', 'input')
                    else:
                        planner_publication._apply_general_planning_batch(effects, connection, '/repo',
                            Path('/worktree'), result, 'commit', 'spec', 'input')
                self.assertEqual(reviews.get(connection, '/repo', 'general') is None, fail_clear)
                self.assertEqual(reviews.get(connection, '/repo', 'attempt') is not None, fail_clear)
                self.assertEqual(planning_input_state.has_baseline(connection, '/repo'), not fail_clear)

    def test_no_change_confirms_review_and_guidance_with_cleanup_transaction(self):
        from loop_manager_internal.storage import planning_input_state, planning_review_state as reviews
        from loop_manager_internal.agents import common_guidance
        for fail_cleanup in (False, True):
            with self.subTest(fail_cleanup=fail_cleanup), tempfile.TemporaryDirectory() as directory:
                connection = self._reviewed_publication_connection()
                worktree = Path(directory)
                ports = FakePorts(planning_snapshot(phase='PLANNING', active_sha='base',
                    worktree=directory, planning_attempt_fingerprint='input', planning_workspace_owned=1))
                ports.state.confirm_planning_review = lambda *args: reviews.confirm(connection, *args)
                ports.state.record_general_plan_declarations = lambda *args: planning_input_state.record_general(connection, *args)
                original_transaction = ports.state.transaction
                @contextmanager
                def transaction():
                    with original_transaction(), connection:
                        yield
                ports.state.transaction = transaction
                original_clear = ports.state.clear_active_in_transaction
                def clear(*args, **kwargs):
                    self.assertEqual(reviews.get(connection, '/repo', 'general')['confirmed_revision'], 'base')
                    self.assertEqual(connection.execute('SELECT guidance_hash FROM planning_input_baselines')
                                     .fetchone()[0], 'pinned-guidance')
                    original_clear(*args, **kwargs)
                ports.state.clear_active_in_transaction = clear
                if fail_cleanup:
                    ports.git.remove_error = RuntimeError('cleanup interrupted')
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                run.planning_worktree = worktree
                run.current_fingerprint = 'input'
                with mock.patch.object(common_guidance, 'planning_content_hash', return_value='pinned-guidance'), \
                     mock.patch.object(common_guidance, 'runtime_content_hash', side_effect=AssertionError('reload')):
                    outcome = coordinator._publication._publish_planner_result(run)
                if fail_cleanup:
                    self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                else:
                    self.assertIsNone(outcome)
                self.assertEqual(reviews.get(connection, '/repo', 'general') is None, fail_cleanup)
                self.assertEqual(reviews.get(connection, '/repo', 'attempt') is not None, fail_cleanup)
                self.assertEqual(planning_input_state.has_baseline(connection, '/repo'), not fail_cleanup)
                if fail_cleanup:
                    self.assertEqual(ports.state.snapshot.worktree, directory)
                    self.assertEqual(ports.state.snapshot.phase, 'PLANNING')

    def test_publication_intent_rejects_bad_hash_before_insert_and_recovers(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "state.db"
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, planner_session_id) "
            "VALUES ('/repo', 'main', 'planner-session')"
        )
        connection.commit()
        snapshot = MutableReadySnapshot(
            "/repo", "base", (), mutable_snapshot_token("/repo", "base", ())
        )
        manifest = loop_manager._planning_result_manifest(
            loop_manager.ValidatedPlanningResult(
                plans=(), publication_paths=(), mutable_snapshot=snapshot,
            )
        )
        valid = GeneralPlanningPublication(
            "/repo", "prepared", "base", "commit", snapshot.token, "spec", "input",
            manifest, hashlib.sha256(manifest.encode()).hexdigest(),
        )
        with self.assertRaises(PlanningResultInvalid):
            record_general_planning_publication(
                connection, replace(valid, manifest_hash="invalid")
            )
        for invalid_manifest in ("", "[]", "invalid", "{}"):
            with self.subTest(manifest=invalid_manifest):
                with self.assertRaises(PlanningResultInvalid):
                    record_general_planning_publication(
                        connection, replace(
                            valid, manifest=invalid_manifest,
                            manifest_hash=hashlib.sha256(invalid_manifest.encode()).hexdigest(),
                        ),
                    )
        for field, value in (("base_sha", "wrong-base"), ("snapshot_token", "wrong-token")):
            with self.subTest(field=field):
                with self.assertRaises(PlanningResultInvalid):
                    record_general_planning_publication(
                        connection, replace(valid, **{field: value})
                    )
        self.assertIsNone(general_planning_publication(connection, "/repo"))
        self.assertEqual(record_general_planning_publication(connection, valid), valid)
        self.assertEqual(record_general_planning_publication(connection, valid), valid)
        self.assertEqual(general_planning_publication(connection, "/repo"), valid)
        connection.close()
        resumed = sqlite3.connect(database)
        self.addCleanup(resumed.close)
        resumed.row_factory = sqlite3.Row
        restored = general_planning_publication(resumed, "/repo")
        self.assertEqual(restored, valid)
        self.assertEqual(
            loop_manager._planning_result_from_manifest(restored.manifest).mutable_snapshot,
            snapshot,
        )
        self.assertEqual(
            resumed.execute(
                "SELECT planner_session_id FROM projects WHERE repo = '/repo'"
            ).fetchone()["planner_session_id"],
            "planner-session",
        )

    def test_unpublished_assessment_keeps_owned_worktree_for_every_ready_guard(self):
        for guard in ("clean", "dirty", "committed", "ownership", "publication", "project_blocked", "active"):
            with self.subTest(guard=guard), tempfile.TemporaryDirectory() as directory:
                snapshot = planning_snapshot(
                    phase="PLANNING", active_sha="base", worktree=directory,
                    planning_attempt_fingerprint="attempt", planning_workspace_owned=1,
                    processed_sha="old", planning_fingerprint="successful-input",
                    specification_fingerprint="successful-specification",
                )
                cycle = maintenance_cycle(
                    phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                    cycle_base_sha="base", last_success_merged_count=5,
                    last_success_file_assessment_merged_count=5,
                )
                ports = FakePorts(snapshot, cycle)
                ports.state.schedule_result.runnable_ready = {"plan_id": "G-058"}
                if guard == "dirty":
                    original = ports.git.run
                    ports.git.run = lambda path, *args: (
                        SimpleNamespace(stdout="?? user.txt", returncode=0)
                        if args == ("status", "--porcelain") else original(path, *args)
                    )
                elif guard == "committed":
                    ports.git.head = "new-commit"
                elif guard == "ownership":
                    ports.git.ownership_error = PlanningWorkspaceOwnershipError("foreign workspace")
                elif guard == "publication":
                    ports.state.general_intent = object()
                elif guard == "project_blocked":
                    ports.state.schedule_result.project_blocked = True
                elif guard == "active":
                    ports.state.schedule_result.active = {"plan_id": "G-059"}
                coordinator = PlanningLifecycleCoordinator(ports)
                run = coordinator._new_run(self.context)
                outcome = coordinator._selection._select_planning_work(run)
                self.assertIsNone(outcome)
                self.assertEqual(ports.state.snapshot, snapshot)
                self.assertEqual(ports.state.diagnostics, [])
                self.assertEqual(ports.state.cycle, cycle)

    def test_remote_base_race_preserves_commit_before_push_or_registration(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.git.remote = "concurrent-base"
        ports.git.ancestor = False

        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")
        run.validated_result = ports.validation.result
        run.dirty_files = list(ports.git.dirty_files)
        run.current_fingerprint = "fingerprint"
        run.general_fingerprint = "fingerprint"
        run.specification_fingerprint = "specification"
        outcome = coordinator._publication._publish_planner_result(run)

        self.assertEqual(
            outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED
        )
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertIn(("commit", "-m", "Add ExecPlan G-001: Plan"), commands)
        self.assertNotIn(
            ("push", "origin", "HEAD:refs/heads/main"), commands
        )
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.state.snapshot.planner_session_id, None)
        self.assertIsNotNone(ports.state.general_intent)

    def test_mutable_ready_race_after_fetch_stops_before_push(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.state.general_intent_error_call = 2

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(
            ports.state.snapshot.planning_error,
            "mutable READY changed before push",
        )
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertNotIn(
            ("push", "origin", "HEAD:refs/heads/main"), commands
        )

    def test_live_pr_race_defers_before_push_and_preserves_planning_candidate(self):
        from loop_manager_internal.planning.planner_validation import MutableReadyPRConflict

        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.state.record_general_intent = mock.Mock(
            side_effect=MutableReadyPRConflict("mutable READY snapshot 이후 PR이 등록됐습니다: G-001")
        )
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")
        run.validated_result = ports.validation.result
        run.dirty_files = list(ports.git.dirty_files)
        run.current_fingerprint = "fingerprint"
        run.general_fingerprint = "fingerprint"
        run.specification_fingerprint = "specification"

        outcome = coordinator._publication._publish_planner_result(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
        self.assertEqual(ports.state.snapshot.phase, "PLANNING")
        self.assertEqual(ports.state.snapshot.worktree, "/planning")
        self.assertEqual(ports.state.snapshot.planning_result, "completed")
        self.assertIsNone(ports.state.snapshot.planning_error)
        ports.state.record_general_intent.assert_called_once()
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertNotIn(("push", "origin", "HEAD:refs/heads/main"), commands)
        self.assertEqual(ports.state.clear_calls, [])

    def test_live_pr_race_after_fetch_supersedes_only_unpublished_intent(self):
        from loop_manager_internal.planning.planner_validation import MutableReadyPRConflict

        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result_pending_validation=True,
                planning_result_completed=True,
                planning_result="completed",
            )
        )
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        record_intent = ports.state.record_general_intent
        calls = 0

        def record_or_race(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise MutableReadyPRConflict("mutable READY snapshot 이후 PR이 등록됐습니다: G-001")
            return record_intent(*args)

        ports.state.record_general_intent = mock.Mock(side_effect=record_or_race)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")
        run.validated_result = ports.validation.result
        run.dirty_files = list(ports.git.dirty_files)
        run.current_fingerprint = "fingerprint"
        run.general_fingerprint = "fingerprint"
        run.specification_fingerprint = "specification"

        outcome = coordinator._publication._publish_planner_result(run)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.REMOTE_BASE_RESTART_DEFERRED)
        self.assertIsNone(ports.state.general_intent)
        self.assertEqual(ports.state.snapshot.worktree, "/planning")
        self.assertEqual(ports.state.snapshot.planning_result, "completed")
        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertEqual(ports.state.record_general_intent.call_count, 2)
        commands = [arguments for _repo, arguments, _check in ports.git.calls]
        self.assertNotIn(("push", "origin", "HEAD:refs/heads/main"), commands)
        self.assertEqual(ports.state.clear_calls, [])

    def test_prepared_publication_recovers_before_agent_or_validation(self):
        cycle = maintenance_cycle(
            phase=maintenance_state.MaintenancePhase.ASSESSMENT,
            cycle_base_sha="base",
            publication_stage="prepared",
            publication_base_sha="old-base",
            publication_commit_sha="plan-commit",
            revision=3,
        )
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="maintenance-fingerprint",
                planning_workspace_owned=1,
            ),
            cycle,
        )
        ports.git.remote = "published-base"
        ports.state.recovered_cycle = replace(
            cycle,
            publication_stage="remote_confirmed",
            publication_remote_oid="published-base",
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(
            outcome.kind, PlanningOutcomeKind.PUBLICATION_RECOVERY
        )
        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(
            ports.state.recover_calls,
            [("/repo", Path("/planning"), "main")],
        )

    def test_general_publication_cleanup_failure_keeps_saved_planning_state(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved publication failure",
                planning_error_fingerprint="fingerprint",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            )
            ports = FakePorts(before)
            ports.state.general_intent = SimpleNamespace(
                base_sha="base", commit_sha="plan-commit", stage="prepared",
            )
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved publication failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)

    def test_verified_pending_general_publication_resumes_saved_result(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="fingerprint",
            planning_workspace_owned=1,
            planning_error="saved push failure",
            planning_error_fingerprint="fingerprint",
            planning_result="saved result",
            planning_result_completed=True,
        ))
        ports.state.general_intent = SimpleNamespace(
            base_sha="base", commit_sha="plan-commit", stage="prepared",
        )
        ports.state.general_recovery_result = "pending"
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.git.advance_remote_on_push = True

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertNotEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR,
                            ports.events.messages)
        self.assertEqual(ports.agent.run_count, 0)
        self.assertTrue(any(
            arguments[:1] == ("push",)
            for _repo, arguments, _check in ports.git.calls
        ))

    def test_pending_general_publication_cleanup_failure_restores_saved_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved push failure",
                planning_error_fingerprint="fingerprint",
                planning_validation_error="saved diagnostics",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_pending_validation=True,
                planning_result_completed=True,
            )
            ports = FakePorts(before)
            ports.state.general_intent = SimpleNamespace(
                base_sha="base", commit_sha="plan-commit", stage="prepared",
            )
            ports.state.general_recovery_result = "pending"
            def apply_batch(*_args):
                ports.state.general_intent = None
                ports.state.clear_active("/repo")
                ports.state.update(
                    "/repo",
                    planning_validation_error=None,
                    planner_session_id=None,
                    planning_result=None,
                    planning_result_pending_validation=False,
                    planning_result_completed=False,
                )
            ports.state.publish_general_batch = mock.Mock(side_effect=apply_batch)
            ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
            ports.git.advance_remote_on_push = True
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.active_sha, "base")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_attempt_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planning_workspace_owned, 1)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved push failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_pending_validation)
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertIsNotNone(ports.state.general_intent)
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)
            self.assertFalse(ports.state.validation_cleared)
            ports.state.publish_general_batch.assert_called_once()

    def test_remote_publication_retry_keeps_saved_result_and_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_result="saved result",
                planning_result_completed=True,
            ))
            ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
            ports.git.remote = "concurrent-base"
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertIsNone(ports.state.snapshot.planning_error_fingerprint)
            self.assertIsNotNone(ports.state.general_intent)
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 0)
            self.assertNotIn(("worktree", "remove", str(worktree)),
                             [args for _repo, args, _check in ports.git.calls])

    def test_pending_maintenance_publication_cleanup_failure_restores_state(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            cycle = maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                cycle_base_sha="base",
                publication_stage="prepared",
                publication_base_sha="base",
                revision=4,
            )
            fingerprint = maintenance_planning_fingerprint(cycle, "fingerprint")
            before = planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint=fingerprint,
                planning_workspace_owned=1,
                planning_error="saved publication failure",
                planning_error_fingerprint=fingerprint,
                planning_validation_error="saved diagnostics",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            )
            ports = FakePorts(before, cycle)
            ports.git.head = "plan-commit"
            ports.git.advance_remote_on_push = True
            ports.git.remove_error = RuntimeError("cleanup failed")
            def apply_batch(*_args):
                ports.state.clear_active("/repo")
                ports.state.update(
                    "/repo",
                    planning_validation_error=None,
                    planner_session_id=None,
                    planning_result=None,
                    planning_result_completed=False,
                )
            ports.state.publish_maintenance_batch = mock.Mock(side_effect=apply_batch)
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.current_fingerprint = fingerprint
            run.planning_worktree = worktree
            run.maintenance_context = object()
            run.validated_result = ports.validation.result

            outcome = coordinator._publication._publish_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.active_sha, "base")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_attempt_fingerprint,
                             fingerprint)
            self.assertEqual(ports.state.snapshot.planning_workspace_owned, 1)
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved publication failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             fingerprint)
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertTrue(worktree.exists())
            ports.state.publish_maintenance_batch.assert_called_once()

    def test_no_change_cleanup_failure_keeps_saved_planning_result(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planning_error="saved validation failure",
                planning_error_fingerprint="fingerprint",
                planner_session_id="saved-session",
                planning_result="saved result",
                planning_result_completed=True,
            ))
            ports.git.remove_error = RuntimeError("cleanup failed")
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.planning_worktree = worktree
            run.current_fingerprint = "fingerprint"

            outcome = coordinator._publication._publish_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_error,
                             "saved validation failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertEqual(ports.state.snapshot.planning_result,
                             "saved result")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertTrue(worktree.exists())

    def test_prerequisite_publication_cleanup_failure_preserves_planning_and_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            cycle = maintenance_cycle(
                phase=maintenance_state.MaintenancePhase.ASSESSMENT,
                cycle_base_sha="base",
                revision=4,
            )
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planner_session_id="saved-session",
                planning_result=(
                    'MAINTENANCE_RESULT: '
                    '{"outcome":"prerequisites","plan_ids":["G-001"]}'
                ),
                planning_result_completed=True,
            ), cycle)
            ports.git.remove_error = RuntimeError("cleanup failed")
            ports.state.transition_maintenance = mock.Mock(
                side_effect=lambda *_args, **_kwargs: setattr(
                    ports.state, "cycle", replace(
                        ports.state.cycle,
                        phase=maintenance_state.MaintenancePhase.PREREQUISITE_WAIT,
                    )
                )
            )
            coordinator = PlanningLifecycleCoordinator(ports)
            run = coordinator._new_run(self.context)
            run.planning_worktree = worktree
            run.current_fingerprint = "fingerprint"
            run.maintenance_context = object()

            with mock.patch(
                "loop_manager_internal.planning.assessment_service.validate_prerequisite_plan_ids",
                return_value=("G-001",),
            ):
                self.assertIsNone(coordinator._assessment._validate_planner_result(run))

            ports.state.transition_maintenance.assert_not_called()
            cycle = ports.state.cycle
            self.assertEqual(cycle.prerequisite_plan_ids, ("G-001",))
            run.row = ports.state.snapshot
            run.maintenance_cycle = cycle
            outcome = coordinator._publication._publish_planner_result(run)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.phase, "PLANNING")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertEqual(ports.state.snapshot.planning_error,
                             "cleanup failed")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "fingerprint")
            self.assertEqual(ports.state.snapshot.planner_session_id,
                             "saved-session")
            self.assertTrue(ports.state.snapshot.planning_result_completed)
            self.assertEqual(ports.state.cycle, cycle)
            self.assertTrue(worktree.exists())
            ports.state.transition_maintenance.assert_called_once()


    def test_unpublished_planner_does_not_activate_stale_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="old-spec",
            planning_fingerprint="old-full",
        ))
        ports.git.specification_fingerprints["old-base"] = "old-spec"
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.git.dirty_files = ["docs/plans/[G-001] Plan.md"]
        ports.state.general_intent_error_call = 1
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertNotEqual(ports.state.snapshot.phase, "WORKING")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")
