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

class PlanningRecoveryTest(PlanningCase):
    def test_recorded_workspace_fetch_preserves_saved_planner_scope_during_remote_race(self):
        from tests.support.origin import remote_fixture, advance_remote, git
        from loop_manager_internal.storage import database
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            connection = database.connect_database(state_dir, create=True)
            self.addCleanup(connection.close)
            tree = Path(directory) / "planning"
            old = git(source, "rev-parse", "HEAD")
            git(source, "worktree", "add", "--detach", str(tree), old)
            repo = str(source)
            connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,))
            connection.commit()
            context = replace(self.context, repo=repo, repo_path=source, state_dir=state_dir)
            before = loop_manager.capture_git_snapshot(tree)
            snapshot = before.serialize()
            connection.execute(
                "UPDATE projects SET agent_snapshot=?, agent_role='planner', worktree=? WHERE repo=?",
                (snapshot, str(tree), repo),
            )
            connection.commit()
            loop_manager._recorded_base_fetch(source, state_dir, "main", owner="project")
            # Origin advances after the outer fetch, while a saved scope exists.
            latest = advance_remote(writer)
            ports = FakePorts(planning_snapshot(
                repo=repo, phase="PLANNING", active_sha=old, worktree=str(tree),
                planning_workspace_owned=1, planner_session_id="saved-session",
                agent_snapshot=snapshot, agent_role="planner",
            ))
            real_git = loop_manager._PlanningGitPorts(connection, source, state_dir)
            ports.git.fetch_base = real_git.fetch_base
            ports.git.remote = latest
            real_agent = loop_manager._PlanningAgentPorts(connection, context, None)
            ports.agent.validate_saved_scope = mock.Mock(wraps=real_agent.validate_saved_scope)
            coordinator = PlanningLifecycleCoordinator(ports)
            self.assertIsNone(coordinator._workspace._prepare_workspace(coordinator._new_run(context)))
            self.assertIsNone(real_agent.validate_saved_scope("planner", tree, snapshot).error)
            self.assertEqual(ports.agent.validate_saved_scope.call_count, 1)
            self.assertEqual(git(source, "rev-parse", "origin/main"), latest)
            self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
            self.assertEqual(git(source, "rev-parse", "HEAD"), old)

    def test_rebased_workspace_refreshes_sqlite_catalog_before_ready_snapshot(self):
        from tests.support.catalog import make_catalog_root, write_execplan
        from tests.support.maintenance_planning_case import git
        from loop_manager_internal.storage import database, runtime_state

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = make_catalog_root(str(root / "repo"))
            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "Test")
            git(repository, "config", "user.email", "test@example.com")
            plan = write_execplan(repository, "G-001", "Unrelated")
            git(repository, "add", ".")
            git(repository, "commit", "-m", "base")
            old = git(repository, "rev-parse", "HEAD").strip()
            git(repository, "update-ref", "refs/remotes/origin/main", old)
            connection = database.connect_database(root, create=True)
            self.addCleanup(connection.close)
            repo = str(repository)
            connection.execute("INSERT INTO projects (repo, base_branch) VALUES (?, 'main')", (repo,))
            connection.commit()
            with mock.patch.object(loop_manager, "pr_for_branch", return_value=None):
                loop_manager.reconcile_execplan_catalog(connection, repository, repo, "main")
            state = loop_manager._PlanningStatePorts(
                connection, runtime_state.RuntimeStateRepository(connection),
                maintenance_state.MaintenanceRepository(connection), root,
            )
            original = plan.read_text()
            plan.write_text(original.replace("Test plan.", "Remote acceptance change."))
            git(repository, "add", ".")
            git(repository, "commit", "-m", "remote")
            latest = git(repository, "rev-parse", "HEAD").strip()
            git(repository, "update-ref", "refs/remotes/origin/main", latest)
            worktree = root / "planning"
            git(repository, "worktree", "add", "--detach", str(worktree), latest)
            candidate = write_execplan(worktree, "G-002", "Saved")
            saved = candidate.read_bytes()
            # An unpublished intent prevents outer catalog reconciliation;
            # workspace adoption must supersede it before refreshing SQLite.
            connection.execute(
                "INSERT INTO general_planning_publications "
                "(repo, stage, base_sha, commit_sha, snapshot_token, "
                "specification_fingerprint, planning_fingerprint, manifest, manifest_hash) "
                "VALUES (?, 'prepared', ?, ?, 'snapshot', 'spec', 'input', '[]', ?)",
                (repo, old, old, hashlib.sha256(b"[]").hexdigest()),
            )
            connection.commit()
            with self.assertRaises(PlanningResultInvalid):
                loop_manager.mutable_ready_snapshot(connection, repo, worktree, latest)
            for active in (old, latest):
                with self.subTest(active=active):
                    ports = FakePorts(planning_snapshot(
                        repo=repo, phase="PLANNING", active_sha=active, worktree=str(worktree),
                        planning_attempt_fingerprint="original-input", planning_workspace_owned=1,
                        planner_session_id="saved-session", planning_result="saved result",
                        planning_result_completed=True,
                    ))
                    ports.git.remote = latest
                    ports.state.reconcile_catalog = state.reconcile_catalog
                    ports.state.general_publication = state.general_publication
                    ports.state.supersede_unpublished_general_intent = state.supersede_unpublished_general_intent
                    ports.validation.prepare_general_context = (
                        lambda repo, worktree, revision: loop_manager.mutable_ready_snapshot(
                            connection, repo, worktree, revision,
                        )
                    )
                    coordinator = PlanningLifecycleCoordinator(ports)
                    context = replace(self.context, repo=repo, repo_path=repository, base_sha=latest)
                    run = coordinator._new_run(context)
                    self.assertIsNone(coordinator._workspace._prepare_workspace(run))
                    self.assertEqual(run.mutable_ready_snapshot.active_sha, latest)
                    self.assertEqual(run.mutable_ready_snapshot.plans[0].content_hash,
                                     hashlib.sha256(plan.read_bytes()).hexdigest())
                    self.assertIsNone(ports.state.snapshot.planning_error)
                    self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
                    self.assertTrue(ports.state.snapshot.planning_result_completed)
                    self.assertEqual(candidate.read_bytes(), saved)
                    self.assertIsNone(state.general_publication(repo))

    def test_unchanged_prepared_intent_retains_its_catalog_snapshot(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result_completed=True,
        ))
        ports.state.general_intent = object()
        ports.state.reconcile_catalog = mock.Mock(side_effect=AssertionError("intent owns catalog"))
        coordinator = PlanningLifecycleCoordinator(ports)
        self.assertIsNone(coordinator._workspace._prepare_workspace(
            coordinator._new_run(self.context),
        ))
        ports.state.reconcile_catalog.assert_not_called()

    def test_catalog_race_pauses_without_poisoning_planning_error(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result_completed=True,
        ))
        ports.state.reconcile_catalog = mock.Mock(side_effect=RuntimeError("catalog changed"))
        ports.validation.prepare_general_context = mock.Mock()
        coordinator = PlanningLifecycleCoordinator(ports)
        outcome = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))
        self.assertEqual(outcome.kind, PlanningOutcomeKind.TRANSIENT_PAUSE)
        self.assertIsNone(ports.state.snapshot.planning_error)
        ports.validation.prepare_general_context.assert_not_called()

    def test_saved_scope_violation_blocks_rebase_without_discarding_result(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            agent_snapshot="saved-scope", agent_role="planner",
            planning_error="scope violation", planning_result="saved result",
        ))
        ports.git.remote = "new-base"
        ports.agent.validate_saved_scope = mock.Mock(return_value=PlanningScopeResult(
            error="unauthorized source change",
        ))
        ports.git.rebase_planning_workspace = mock.Mock()
        coordinator = PlanningLifecycleCoordinator(ports)

        outcome = coordinator._workspace._prepare_workspace(coordinator._new_run(self.context))

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        ports.git.rebase_planning_workspace.assert_not_called()
        self.assertEqual(ports.state.snapshot.active_sha, "base")
        self.assertEqual(ports.state.snapshot.planning_error, "scope violation")
        self.assertEqual(ports.state.snapshot.planning_result, "saved result")
        self.assertEqual(ports.state.clear_calls, [])

    def test_completed_no_change_turn_resumes_validation_without_agent_replay(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result="no further plans", planning_result_completed=True,
        ))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)
        run.planning_worktree = Path("/planning")

        self.assertIsNone(coordinator._assessment._run_planner_turn(run))

        self.assertEqual(ports.agent.run_count, 0)
        self.assertEqual(ports.state.snapshot.planning_result, "no further plans")
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")

    def test_recorded_base_finishes_interrupted_rebase_cleanup(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
        ))
        coordinator = PlanningLifecycleCoordinator(ports)

        self.assertIsNone(coordinator._workspace._prepare_workspace(
            coordinator._new_run(self.context),
        ))
        self.assertIn(
            (None, ("complete_planning_rebase", "/repo", Path("/planning"), "base"), True),
            ports.git.calls,
        )

    def test_remote_advance_preserves_owned_planner_session_and_result(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result="saved result", planning_result_completed=True,
        ))
        ports.git.remote = "new-base"
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        outcome = coordinator._workspace._prepare_workspace(run)

        self.assertIsNone(outcome)
        self.assertEqual(ports.state.snapshot.active_sha, "new-base")
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
        self.assertEqual(ports.state.snapshot.planning_result, "saved result")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertIn(("/repo", "original-input", Path("/planning")),
                      ports.git.ownership_checks)

    def test_related_document_change_requests_saved_planner_recheck(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING", active_sha="base", worktree="/planning",
            planning_attempt_fingerprint="original-input",
            planning_workspace_owned=1, planner_session_id="saved-session",
            planning_result="saved result", planning_result_completed=True,
            planning_error="old validation failure",
            planning_validation_error="old validation failure",
        ))
        ports.git.remote = "new-base"
        ports.git.relevant_document_changes = mock.Mock(return_value=(
            "docs/requirements/[R-051] Relevant.md",
        ))
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(self.context)

        self.assertIsNone(coordinator._workspace._prepare_workspace(run))

        self.assertFalse(ports.state.snapshot.planning_result_completed)
        self.assertIn("[R-051] Relevant.md",
                      ports.state.snapshot.planning_validation_error)
        self.assertEqual(ports.state.snapshot.planning_error,
                         ports.state.snapshot.planning_validation_error)
        self.assertEqual(ports.state.snapshot.planner_session_id, "saved-session")
        self.assertEqual(ports.state.snapshot.planning_result, "saved result")

    def test_planner_resolves_owned_plan_file_rebase_conflict_in_saved_session(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            plan = worktree / "docs/plans/[G-001] Plan.md"
            plan.parent.mkdir(parents=True)
            plan.write_text("resolved plan\n", encoding="utf-8")
            (worktree / ".git/rebase-merge").mkdir(parents=True)
            ports = FakePorts(planning_snapshot(
                phase="PLANNING", active_sha="base", worktree=str(worktree),
                planning_attempt_fingerprint="original-input",
                planning_workspace_owned=1, planner_session_id="saved-session",
            ))
            ports.git.remote = "new-base"
            ports.git.rebase_planning_workspace = mock.Mock(side_effect=(
                SimpleNamespace(target="new-base", changed=True, conflict=True,
                                detail="plan conflict"),
                SimpleNamespace(target="new-base", changed=True, conflict=False,
                                detail=""),
            ))
            original_run = ports.git.run

            def run(repo, *args, check=True):
                if args == ("diff", "--name-only", "--diff-filter=U", "-z"):
                    return SimpleNamespace(stdout="docs/plans/[G-001] Plan.md\0",
                                           returncode=0)
                if args == ("rev-parse", "--git-path", "rebase-merge"):
                    return SimpleNamespace(
                        stdout=str(worktree / ".git/rebase-merge"), returncode=0,
                    )
                return original_run(repo, *args, check=check)

            ports.git.run = run
            coordinator = PlanningLifecycleCoordinator(ports)
            outcome = coordinator._workspace._prepare_workspace(
                coordinator._new_run(self.context)
            )

            self.assertIsNone(outcome)
            self.assertEqual(ports.agent.run_count, 1)
            self.assertEqual(ports.agent.resumed_session, "saved-session")
            self.assertEqual(ports.state.snapshot.active_sha, "new-base")
            calls = [args for _repo, args, _check in ports.git.calls]
            self.assertIn(("add", "--", "docs/plans/[G-001] Plan.md"), calls)
            self.assertIn(("-c", "core.editor=true", "rebase", "--continue"), calls)

    def test_untracked_plan_id_collision_uses_saved_planner_session(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            remote = worktree / "docs/plans/[G-120] Candidate.md"
            remote.parent.mkdir(parents=True)
            remote.write_text("remote plan\n", encoding="utf-8")
            corrected = worktree / "docs/plans/[G-121] Candidate.md"
            corrected.write_text("corrected local plan\n", encoding="utf-8")
            ports = FakePorts(planning_snapshot(
                phase="PLANNING", active_sha="base", worktree=str(worktree),
                planning_attempt_fingerprint="original-input",
                planning_workspace_owned=1, planner_session_id="saved-session",
            ))
            ports.git.remote = "new-base"
            collision = "docs/plans/[G-120] Candidate.md"
            ports.git.rebase_planning_workspace = mock.Mock(side_effect=(
                SimpleNamespace(target="new-base", changed=True, conflict=True,
                                detail="untracked collision", conflict_paths=(collision,),
                                stash_sha="saved-stash"),
                SimpleNamespace(target="new-base", changed=True, conflict=False,
                                detail=""),
            ))
            ports.git.accept_resolved_stash_conflict = mock.Mock()
            original_run = ports.git.run

            def run(repo, *args, check=True):
                if args == ("diff", "--name-only", "--diff-filter=U", "-z"):
                    return SimpleNamespace(stdout="", returncode=0)
                if args[:4] == ("ls-tree", "-r", "--name-only", "saved-stash^3"):
                    return SimpleNamespace(stdout=collision + "\n", returncode=0)
                if args == ("ls-files", "--others", "--exclude-standard"):
                    return SimpleNamespace(stdout="docs/plans/[G-121] Candidate.md\n",
                                           returncode=0)
                return original_run(repo, *args, check=check)

            ports.git.run = run
            coordinator = PlanningLifecycleCoordinator(ports)
            outcome = coordinator._workspace._prepare_workspace(
                coordinator._new_run(self.context),
            )

            self.assertIsNone(outcome)
            self.assertEqual(ports.agent.resumed_session, "saved-session")
            ports.git.accept_resolved_stash_conflict.assert_called_once_with(
                "/repo", worktree, "new-base",
            )
            self.assertEqual(ports.state.snapshot.active_sha, "new-base")

    def test_validation_retry_resumes_session_and_persists_retry_transition(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
                planner_session_id="session-1",
                planning_validation_error="first invalid result",
                planning_retry_count=1,
            )
        )
        ports.validation.general_error = "second invalid result"

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.VALIDATION_RETRY)
        self.assertEqual(ports.agent.resumed_session, "session-1")
        self.assertEqual(ports.state.snapshot.planning_retry_count, 2)
        self.assertEqual(
            ports.state.snapshot.planning_validation_error,
            "second invalid result",
        )
        self.assertFalse(
            ports.state.snapshot.planning_result_pending_validation
        )

    def test_transient_git_error_escapes_without_persisting_planning_error(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
            )
        )
        ports.git.workspace_error = RuntimeError("index.lock exists")

        with self.assertRaisesRegex(RuntimeError, "index.lock exists"):
            PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertIsNone(ports.state.snapshot.planning_error)
        self.assertFalse(
            any("planning_error" in update for update in ports.state.updates)
        )

    def test_workspace_ownership_error_is_persisted_for_same_fingerprint(self):
        ports = FakePorts(
            planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree="/planning",
                planning_attempt_fingerprint="fingerprint",
                planning_workspace_owned=1,
            )
        )
        ports.git.workspace_error = PlanningWorkspaceOwnershipError(
            "workspace belongs to another task"
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(
            ports.state.snapshot.planning_error,
            "planning workspace 소유권 검증 실패: workspace belongs to another task",
        )

    def test_changed_failed_input_without_workspace_starts_new_planning(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.NO_CHANGE)
        self.assertEqual(ports.agent.run_count, 1)
        self.assertEqual(ports.state.clear_calls[0], (None, None))
        self.assertIsNone(ports.state.snapshot.planning_error)

    def test_changed_input_without_saved_workspace_keeps_session_evidence(self):
        ports = FakePorts(planning_snapshot(
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
            planner_session_id="saved-session",
            planning_validation_error="saved diagnostics",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                         "old-input")
        self.assertEqual(ports.state.snapshot.planner_session_id,
                         "saved-session")
        self.assertEqual(ports.state.snapshot.planning_validation_error,
                         "saved diagnostics")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)

    def test_changed_input_without_workspace_preserves_phase_or_active_sha(self):
        for phase, active_sha in (("PLANNING", None), (None, "base")):
            with self.subTest(phase=phase, active_sha=active_sha):
                ports = FakePorts(planning_snapshot(
                    phase=phase,
                    active_sha=active_sha,
                    planning_error="saved failure",
                    planning_error_fingerprint="old-input",
                ))

                outcome = PlanningLifecycleCoordinator(ports).run(self.context)

                self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
                self.assertEqual(ports.state.snapshot.phase, phase)
                self.assertEqual(ports.state.snapshot.active_sha, active_sha)
                self.assertEqual(ports.state.snapshot.planning_error,
                                 "saved failure")
                self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                                 "old-input")
                self.assertEqual(ports.state.clear_calls, [])
                self.assertEqual(ports.agent.run_count, 0)

    def test_changed_failed_input_keeps_owned_workspace_and_independent_error(self):
        ports = FakePorts(planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="old-input",
            planning_workspace_owned=1,
            planning_error="saved failure",
            planning_error_fingerprint="old-input",
        ))

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 1)

    def test_changed_failed_input_keeps_error_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "planning"
            worktree.mkdir()
            ports = FakePorts(planning_snapshot(
                phase="PLANNING",
                active_sha="base",
                worktree=str(worktree),
                planning_attempt_fingerprint="old-input",
                planning_workspace_owned=1,
                planning_error="saved failure",
                planning_error_fingerprint="old-input",
                planning_validation_error="saved diagnostics",
            ))
            ports.git.remove_error = RuntimeError("cleanup failed")

            outcome = PlanningLifecycleCoordinator(ports).run(self.context)

            self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
            self.assertEqual(ports.state.snapshot.planning_error, "saved failure")
            self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                             "old-input")
            self.assertEqual(ports.state.snapshot.planning_validation_error,
                             "saved diagnostics")
            self.assertEqual(ports.state.snapshot.worktree, str(worktree))
            self.assertTrue(worktree.exists())
            self.assertEqual(ports.agent.run_count, 1)
            self.assertNotIn(("worktree", "remove", str(worktree)),
                             [args for _repo, args, _check in ports.git.calls])

    def test_changed_failed_input_keeps_unowned_workspace_and_diagnostics(self):
        before = planning_snapshot(
            phase="PLANNING",
            active_sha="base",
            worktree="/planning",
            planning_attempt_fingerprint="old-input",
            planning_workspace_owned=0,
            planning_error="saved ownership failure",
            planning_error_fingerprint="old-input",
            planning_validation_error="saved diagnostics",
            planner_session_id="saved-session",
        )
        ports = FakePorts(before)
        ports.git.ownership_error = PlanningWorkspaceOwnershipError(
            "workspace owner is unknown"
        )

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertEqual(ports.state.snapshot.planning_error,
                         "saved ownership failure")
        self.assertEqual(ports.state.snapshot.planning_error_fingerprint,
                         "old-input")
        self.assertEqual(ports.state.snapshot.planning_validation_error,
                         "saved diagnostics")
        self.assertEqual(ports.state.snapshot.planner_session_id,
                         "saved-session")
        self.assertEqual(ports.state.clear_calls, [])
        self.assertEqual(ports.agent.run_count, 0)

    def test_failed_planner_does_not_activate_stale_ready(self):
        ports = FakePorts(planning_snapshot(
            processed_sha="old-base",
            specification_fingerprint="specification",
            planning_fingerprint="old-full",
        ))
        ports.git.fingerprint = "new-full"
        ports.git.fingerprints["old-base"] = "old-full"
        ports.agent.result = PlanningAgentResult(
            1, "planner failed", None,
            PlanningAgentResultKind.PERMANENT_CONFIGURATION,
        )
        ports.state.schedule_result = SimpleNamespace(
            active=None, has_ready=True, has_waiting=False,
            runnable_ready=object(), project_blocked=False,
        )
        ports.state.activate_result = object()

        outcome = PlanningLifecycleCoordinator(ports).run(self.context)

        self.assertEqual(outcome.kind, PlanningOutcomeKind.OPERATIONAL_ERROR)
        self.assertNotEqual(ports.state.snapshot.phase, "WORKING")
        self.assertEqual(ports.state.snapshot.planning_fingerprint, "old-full")
