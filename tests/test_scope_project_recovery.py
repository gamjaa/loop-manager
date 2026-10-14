from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture
from tests.support.blocked_remote import ProjectBlockedRemoteFixture
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import scope_reconciliation
from loop_manager_internal.recovery import project_block_service
from loop_manager_internal.storage import execution_checkpoint_state
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
import json
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, commit_and_push


@isolated_git_test
class ScopeProjectRecoveryTest(AgentScopeFixture, unittest.TestCase):
    def test_default_worktree_project_block_releases_without_source_revert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, plan_path = self._repository_with_plan(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            branch, worktree = loop_manager.expected_execplan_workspace(
                state_dir,
                str(repo),
                "G-001",
            )
            loop_manager.make_worktree(repo, worktree, branch, "main")
            plan_sha = git_command(repo, "rev-parse", "origin/main")

            connection = sqlite3.connect(":memory:")
            self.addCleanup(connection.close)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                (str(repo),),
            )
            loop_manager.register_execplans(
                connection,
                str(repo),
                repo,
                [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
                plan_sha,
                loop_manager.planning_input_fingerprint(repo, "main"),
            )
            loop_manager.scoped_blocking.ensure_schema(connection)

            before = loop_manager.capture_git_snapshot(worktree)
            (worktree / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            source_note = repo / "source-note.txt"
            source_note.write_text("preserve me\n", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(worktree)
            self.assertNotEqual(
                before.worktrees[str(repo.resolve())],
                after.worktrees[str(repo.resolve())],
            )

            session_id = "preserved-worker-session"
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'IMPLEMENTING', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        worker_session_id = ?, agent_role = 'worker',
                        agent_snapshot = ?, agent_result_snapshot = ?
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        session_id,
                        before.serialize(),
                        after.serialize(),
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'WORKING',
                        branch = ?, worktree = ?, plan_path = ?,
                        worker_session_id = ?, agent_role = 'worker',
                        agent_snapshot = ?, agent_result_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        session_id,
                        before.serialize(),
                        after.serialize(),
                        str(repo),
                    ),
                )
                loop_manager.scoped_blocking.record_project_block(
                    connection,
                    str(repo),
                    loop_manager.BlockReasonCode.DEFAULT_WORKTREE_MUTATION,
                    "worker Git 범위 위반: primary checkout changed",
                    "blocked-fingerprint",
                    plan_id="G-001",
                    previous_phase="WORKING",
                )

            self.assertTrue(
                loop_manager.resume_project_block_if_safe(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                )
            )

            project = connection.execute(
                "SELECT phase, worker_session_id, last_error FROM projects "
                "WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            plan = connection.execute(
                "SELECT status, worker_session_id FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertIsNone(
                loop_manager.scoped_blocking.project_block(connection, str(repo))
            )
            self.assertEqual(tuple(project), ("WORKING", session_id, None))
            self.assertEqual(tuple(plan), ("IMPLEMENTING", session_id))
            self.assertEqual(source_note.read_text(encoding="utf-8"), "preserve me\n")

    def test_active_execplan_uses_reconciled_identity_not_publication_equality(self):
        replacements = (
            (
                "sources",
                "sources:\n  - docs/PRODUCT.md",
                "sources:\n  - docs/requirements/Active.md",
            ),
            (
                "depends_on",
                "depends_on: []",
                "depends_on:\n  - G-002",
            ),
            ("complexity", "complexity: low", "complexity: medium"),
            ("risk", "risk: low", "risk: high"),
            ("priority", "priority: normal", "priority: critical"),
            ("body", "Test plan.", "Updated implementation progress."),
        )
        for name, original, replacement in replacements:
            with self.subTest(field=name):
                with tempfile.TemporaryDirectory() as directory:
                    repo, plan_path = self._repository_with_plan(directory)
                    write_execplan(repo, "G-002", "Dependency")
                    commit_and_push(repo, "Add dependency plan")
                    plan_sha = git_command(repo, "rev-parse", "origin/main")
                    connection = sqlite3.connect(":memory:")
                    self.addCleanup(connection.close)
                    connection.row_factory = sqlite3.Row
                    connection.execute(loop_manager.SCHEMA)
                    connection.execute(
                        "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                        (str(repo),),
                    )
                    parsed = loop_manager._parse_execplan(
                        repo,
                        repo / plan_path,
                    )
                    loop_manager.register_execplans(
                        connection,
                        str(repo),
                        repo,
                        [(plan_path, parsed)],
                        plan_sha,
                        loop_manager.planning_input_fingerprint(repo, "main"),
                    )
                    connection.execute(
                        "UPDATE exec_plans SET status = 'IMPLEMENTING' "
                        "WHERE repo = ? AND plan_id = 'G-001'",
                        (str(repo),),
                    )
                    connection.commit()
                    stored = loop_manager.active_execplan(
                        connection,
                        str(repo),
                    )
                    self.assertIsNotNone(stored)
                    assert stored is not None

                    plan_file = repo / plan_path
                    if name == "sources":
                        requirement = repo / "docs/requirements/Active.md"
                        requirement.parent.mkdir(parents=True, exist_ok=True)
                        requirement.write_text("# Active\n", encoding="utf-8")
                    plan_file.write_text(
                        plan_file.read_text(encoding="utf-8").replace(
                            original,
                            replacement,
                        ),
                        encoding="utf-8",
                    )
                    if name != "body":
                        revised = loop_manager.validate_execplan(repo, plan_path)
                        sources, dependencies, priority = (
                            loop_manager._execplan_metadata(revised)
                        )
                        connection.execute(
                            "UPDATE exec_plans SET sources = ?, depends_on = ?, "
                            "priority = ?, content_hash = ?, "
                            "current_complexity = ?, current_risk = ? "
                            "WHERE repo = ? AND plan_id = 'G-001'",
                            (
                                sources,
                                dependencies,
                                priority,
                                loop_manager.file_hash(plan_file),
                                revised["complexity"],
                                revised["risk"],
                                str(repo),
                            ),
                        )
                        connection.commit()
                        stored = loop_manager.active_execplan(
                            connection,
                            str(repo),
                        )
                        self.assertIsNotNone(stored)
                        assert stored is not None

                    current = loop_manager.validate_active_execplan_metadata(
                        repo,
                        repo,
                        plan_path,
                        stored,
                    )
                    self.assertEqual(current["id"], "G-001")


@isolated_git_test
class ProjectRecoveryTransitionUnitTests(ProjectBlockedRemoteFixture, unittest.TestCase):
    def test_rebase_scope_project_release_advances_checkpoint_generation(self):
        self.connection.execute(
            "UPDATE project_blocks SET reason_code = 'shared_ref_mutation' WHERE repo = 'repo'")
        self.connection.execute(
            "UPDATE exec_plans SET agent_snapshot = 'saved-snapshot' WHERE plan_id = 'G-106'")
        self.connection.commit()
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
        before = checkpoints.get(self.repo, 'G-106')
        effects = mock.Mock()
        effects.capture_git_snapshot.return_value = SimpleNamespace(head='current', dirty={}, branch='loop-manager/G-106', index={}, operations={}, serialize=lambda: 'candidate')
        effects._clear_project_block_without_losing_planning_error.side_effect = (
            project_block_service._clear_project_block_without_losing_planning_error)
        self.assertTrue(scope_reconciliation.resume_project_block_if_safe(
            effects, self.connection, Path('/repo'), Path('/state'), self.repo))
        after = checkpoints.get(self.repo, 'G-106')
        self.assertEqual(after.generation, before.generation + 1)
        self.assertEqual(after.snapshot['scope_block_release_audit'][-1]['candidate'], 'candidate')
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))

    def test_transient_pr_lookup_keeps_project_diagnostic_pending(self):
        self.ports.pr_for_branch.side_effect = RuntimeError('temporary API failure')
        self.assertEqual(self.coordinator.reconcile('G-106'), 'blocked')
        self.assertEqual(self.connection.execute(
            "SELECT stage FROM project_blocked_reconciliations WHERE plan_id = 'G-106'").fetchone()[0], 'observed')
        self.assertIsNone(self.connection.execute(
            "SELECT * FROM project_checkpoint_results WHERE plan_id = 'G-106'").fetchone())
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))

    def test_gate_release_and_candidate_revalidation_roll_back_together(self):
        self.connection.execute("DELETE FROM execplan_blocks WHERE plan_id = 'G-106'")
        self.connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING', publication_remote_oid = ?, "
            "worker_result = 'old candidate' "
            "WHERE plan_id = 'G-106'", (self.head,))
        self.connection.execute(
            "UPDATE project_blocks SET previous_phase = 'WORKING' WHERE repo = 'repo'")
        block = scoped_blocking.project_block(self.connection, self.repo)
        cause = scoped_blocking.project_cause_identity(block)
        self.connection.execute(
            "INSERT INTO project_checkpoint_results VALUES (?, ?, ?, ?)",
            (self.repo, 'G-106', cause,
             json.dumps({'result': 'published', 'commit_oid': self.head})))
        self.connection.commit()
        plan = self.plan()
        context = {
            'plan_id': 'G-106', 'project_block': dict(block),
            'cause_identity': cause, 'diagnostic': False,
            'base_oid': self.base, 'input_identity': 'candidate',
        }
        with (mock.patch.object(self.coordinator, '_check_owner', return_value=plan),
              mock.patch.object(self.coordinator, '_candidate_unchanged'),
              mock.patch.object(self.coordinator, '_lineage_known', return_value=True),
              mock.patch.object(self.coordinator, '_save', side_effect=KeyboardInterrupt)):
            with self.assertRaises(KeyboardInterrupt):
                self.coordinator._revalidate_project_cause(plan, context)
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertEqual(self.plan()['status'], 'IMPLEMENTING')
        self.assertEqual(self.plan()['worker_result'], 'old candidate')
        self.assertIsNone(self.connection.execute(
            "SELECT * FROM project_blocked_reconciliations WHERE plan_id = 'G-106'").fetchone())

        restarted = self.recovery.ProjectBlockedReconciler(
            self.ports, self.connection, Path('/repo'), Path('/state'), self.repo, 'main')
        with (mock.patch.object(restarted, '_check_owner', return_value=plan),
              mock.patch.object(restarted, '_candidate_unchanged'),
              mock.patch.object(restarted, '_lineage_known', return_value=True)):
            self.assertTrue(restarted._revalidate_project_cause(plan, context))
        self.assertIsNone(scoped_blocking.project_block(self.connection, self.repo))
        self.assertIsNone(self.plan()['worker_result'])
        self.assertEqual(self.connection.execute(
            "SELECT stage FROM project_blocked_reconciliations WHERE plan_id = 'G-106'").fetchone()[0], 'complete')
