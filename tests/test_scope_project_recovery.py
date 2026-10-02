from tests.support.scope import AgentScopeFixture
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, commit_and_push


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

            self.assertFalse(
                loop_manager.resume_project_block_if_safe(
                    connection,
                    repo,
                    state_dir,
                    str(repo),
                )
            )
            resources = loop_manager.agent_scope_decision.used_git_resources(
                role='worker', phase='WORKING', base_branch='main',
                branch=branch, worktree_key=str(worktree.resolve()))
            with mock.patch.object(loop_manager, '_agent_git_resources', return_value=resources):
                self.assertTrue(loop_manager.resume_project_block_if_safe(
                    connection, repo, state_dir, str(repo)))

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
