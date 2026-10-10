from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace
import loop_manager
from loop_manager_internal.recovery import blocked_agent_service
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from tests.support.git import git_command, advance_remote_file
from tests.support.worker import isolated_scope_resources
from tests.support.worker import dummy_worker_sessions


@isolated_git_test
class ScopeRebaseRecoveryTest(AgentScopeFixture, unittest.TestCase):
    @dummy_worker_sessions
    def test_agent_commit_isolated_automatically_after_blocked_input_change(self):
        isolation = mock.patch.object(loop_manager, '_agent_git_resources', side_effect=isolated_scope_resources)
        isolation.start()
        self.addCleanup(isolation.stop)
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
            self._confirm_general_plan_input(connection, repo, plan_sha)
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            blocked_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = ?, active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1
                    WHERE repo = ? AND plan_id = ?
                    """,
                    (
                        "IMPLEMENTING",
                        plan_sha,
                        branch,
                        str(worktree),
                        str(repo),
                        "G-001",
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = ?,
                        branch = ?, worktree = ?, plan_path = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        "WORKING",
                        branch,
                        str(worktree),
                        plan_path,
                        str(repo),
                    ),
                )
            (worktree / "src/app.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "src/app.py")
            (worktree / "README.md").write_text(
                "# preserved unstaged Worker change\n",
                encoding="utf-8",
            )
            (worktree / "tests/evidence.txt").write_text(
                "targeted tests passed\n",
                encoding="utf-8",
            )
            before = loop_manager.begin_agent_scope(
                connection,
                str(repo),
                worktree,
                "worker",
                input_fingerprint=blocked_fingerprint,
            )
            self.assertTrue(before.dirty)
            self.assertIn("staged", before.dirty["src/app.py"][0])
            (worktree / "docs/requirements/Active.md").write_text(
                "# changed by committing Agent\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "-A")
            git_command(worktree, "commit", "-m", "Agent commit")
            agent_commit = git_command(worktree, "rev-parse", "HEAD")
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'BLOCKED', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        blocked_fingerprint = ?,
                        blocked_worktree_fingerprint = ?
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        blocked_fingerprint,
                        blocked_fingerprint,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'BLOCKED',
                        branch = ?, worktree = ?, plan_path = ?,
                        agent_role = 'worker', agent_snapshot = ?,
                        agent_input_fingerprint = ?,
                        agent_result_snapshot = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        blocked_fingerprint,
                        blocked_result.serialize(),
                        str(repo),
                    ),
                )

            advance_remote_file(directory, repo, 'docs/PRODUCT.md', '# Product changed after blocked Agent commit\n', 'Change source after blocked Agent commit')
            with connection:
                connection.execute(
                    "UPDATE projects SET specification_fingerprint = ? "
                    "WHERE repo = ?",
                    (
                        loop_manager.specification_input_fingerprint(repo, "main"),
                        str(repo),
                    ),
                )

            def stop_after_isolation(
                _worktree, _state, _config, role_name, _prompt, **_kwargs
            ):
                if role_name == "planner":
                    return loop_manager.CodexResult(0, "", "planner-session")
                restored = loop_manager.capture_git_snapshot(worktree)
                self.assertIn("staged", restored.dirty["src/app.py"][0])
                self.assertIn("unstaged", restored.dirty["README.md"][0])
                self.assertIn(
                    "untracked",
                    restored.dirty["tests/evidence.txt"][0],
                )
                return loop_manager.CodexResult(
                    1,
                    "stop after automatic commit isolation",
                    "worker-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex",
                side_effect=stop_after_isolation,
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot, agent_recovery "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, blocked_fingerprint FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            diagnostic_refs = git_command(
                worktree,
                "for-each-ref",
                "--format=%(objectname)",
                "refs/loop-manager/violations/",
            ).splitlines()
            self.assertEqual(result, 1, dict(project))
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(
                project["last_error"],
                "stop after automatic commit isolation",
            )
            self.assertIsNone(project["agent_snapshot"])
            self.assertIsNone(project["agent_recovery"])
            self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
            self.assertIsNone(resumed_plan["blocked_fingerprint"])
            self.assertIn(agent_commit, diagnostic_refs)
            self.assertEqual(
                (worktree / "src/app.py").read_text(encoding="utf-8"),
                "VALUE = 2\n",
            )
            self.assertEqual(
                (worktree / "tests/evidence.txt").read_text(encoding="utf-8"),
                "targeted tests passed\n",
            )
            restored = loop_manager.capture_git_snapshot(worktree)
            self.assertIn("staged", restored.dirty["src/app.py"][0])
            self.assertIn("unstaged", restored.dirty["README.md"][0])
            self.assertIn("untracked", restored.dirty["tests/evidence.txt"][0])
            self.assertEqual(
                (worktree / "README.md").read_text(encoding="utf-8"),
                "# preserved unstaged Worker change\n",
            )
            self.assertEqual(
                (worktree / "docs/requirements/Active.md").read_text(
                    encoding="utf-8"
                ),
                "# Active\n",
            )
            self.assertEqual(
                [call.args[3] for call in resumed_agent.call_args_list],
                ["planner", "worker"],
            )

    @dummy_worker_sessions
    def test_blocked_scope_resume_rebases_non_source_remote_and_direct_execplan_edit(self):
        isolation = mock.patch.object(loop_manager, '_agent_git_resources', side_effect=isolated_scope_resources)
        isolation.start()
        self.addCleanup(isolation.stop)
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
            self._confirm_general_plan_input(connection, repo, plan_sha)
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            input_fingerprint = loop_manager._worktree_execplan_input_fingerprint(
                worktree,
                plan,
            )
            plan_file = worktree / plan_path
            original_plan_text = plan_file.read_text(encoding="utf-8")
            before = loop_manager.capture_git_snapshot(worktree)
            reviewer_plan_text = original_plan_text.replace(
                "Test plan.", "Reviewer mutation."
            )
            plan_file.write_text(reviewer_plan_text, encoding="utf-8")
            blocked_worktree_fingerprint = (
                loop_manager._worktree_execplan_input_fingerprint(worktree, plan)
            )
            (worktree / "reviewer.txt").write_text(
                "preserve violation\n",
                encoding="utf-8",
            )
            blocked_result = loop_manager.capture_git_snapshot(worktree)
            with connection:
                connection.execute(
                    """
                    UPDATE exec_plans
                    SET status = 'BLOCKED', active_sha = ?, branch = ?,
                        worktree = ?, workspace_owned = 1,
                        blocked_fingerprint = ?,
                        blocked_worktree_fingerprint = ?,
                        last_error = 'scope blocked'
                    WHERE repo = ? AND plan_id = 'G-001'
                    """,
                    (
                        plan_sha,
                        branch,
                        str(worktree),
                        input_fingerprint,
                        blocked_worktree_fingerprint,
                        str(repo),
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET active_sha = ?, plan_sha = ?, phase = 'BLOCKED',
                        branch = ?, worktree = ?, plan_path = ?,
                        last_error = 'scope blocked', agent_role = 'reviewer',
                        agent_snapshot = ?, agent_input_fingerprint = ?,
                        agent_result_snapshot = ?, agent_input_plan = ?,
                        agent_result_plan = ?
                    WHERE repo = ?
                    """,
                    (
                        plan_sha,
                        plan_sha,
                        branch,
                        str(worktree),
                        plan_path,
                        before.serialize(),
                        input_fingerprint,
                        blocked_result.serialize(),
                        original_plan_text,
                        reviewer_plan_text,
                        str(repo),
                    ),
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex"
            ) as blocked_agent:
                unchanged_run = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            persisted_block = connection.execute(
                "SELECT phase, agent_manager_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            persisted_plan = connection.execute(
                "SELECT status, agent_snapshot FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(unchanged_run, 0)
            self.assertIsNone(persisted_block["phase"])
            self.assertEqual(persisted_plan["status"], "PLAN_BLOCKED")
            self.assertIsNotNone(persisted_plan["agent_snapshot"])
            blocked_agent.assert_not_called()

            remote_head = advance_remote_file(directory, repo, 'docs/notes.md', '# Notes\n\nRemote non-input change.\n', 'Change non-input documentation')
            self.assertEqual(
                loop_manager._revision_execplan_input_fingerprint(
                    repo,
                    "refs/remotes/origin/main",
                    plan,
                ),
                input_fingerprint,
            )

            user_mixed_plan_text = plan_file.read_text(encoding="utf-8") + (
                "\nUser clarification after scope block.\n"
            )
            plan_file.write_text(user_mixed_plan_text, encoding="utf-8")
            user_plan_text = original_plan_text + "\nUser clarification after scope block.\n"

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.os.replace",
                side_effect=KeyboardInterrupt,
            ), self.assertRaises(KeyboardInterrupt):
                loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            interrupted = connection.execute(
                "SELECT phase, agent_recovery FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(interrupted["phase"], "PLAN_BLOCKED")
            self.assertIsNotNone(interrupted["agent_recovery"])
            recovery = json.loads(interrupted["agent_recovery"])
            recovery_temp = worktree / recovery["plan_temp_path"]
            self.assertEqual(
                plan_file.read_text(encoding="utf-8"),
                original_plan_text,
            )
            self.assertTrue(recovery_temp.is_file())
            recovery_temp.write_bytes(b"partial interrupted contents")
            self.assertFalse((worktree / "reviewer.txt").exists())

            def resumed_worker(agent_worktree, *_args, **_kwargs):
                self.assertEqual(_args[2], "worker")
                self.assertEqual(
                    (agent_worktree / plan_path).read_text(encoding="utf-8"),
                    user_plan_text,
                )
                self.assertNotIn(
                    "Reviewer mutation.",
                    (agent_worktree / plan_path).read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    (agent_worktree / "docs/notes.md").read_text(
                        encoding="utf-8"
                    ),
                    "# Notes\n\nRemote non-input change.\n",
                )
                self.assertEqual(
                    git_command(
                        agent_worktree,
                        "merge-base",
                        "--is-ancestor",
                        remote_head,
                        "HEAD",
                    ),
                    "",
                )
                self.assertFalse((agent_worktree / "reviewer.txt").exists())
                return loop_manager.CodexResult(
                    1,
                    "stop after combined input recovery",
                    "worker-session",
                )

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.pr_for_branch", return_value=None
            ), mock.patch(
                "loop_manager.run_codex", side_effect=resumed_worker
            ) as resumed_agent:
                result = loop_manager.run_project(
                    connection,
                    state_dir,
                    str(repo),
                    Path("unused.ini"),
                    "main",
                    False,
                )

            project = connection.execute(
                "SELECT phase, last_error, agent_snapshot "
                "FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            resumed_plan = connection.execute(
                "SELECT status, blocked_fingerprint, blocked_worktree_fingerprint "
                "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                (str(repo),),
            ).fetchone()
            self.assertEqual(result, 1, dict(project))
            self.assertEqual(project["phase"], "WORKING")
            self.assertEqual(
                project["last_error"],
                "stop after combined input recovery",
            )
            self.assertIsNone(project["agent_snapshot"])
            self.assertEqual(resumed_plan["status"], "IMPLEMENTING")
            self.assertIsNone(resumed_plan["blocked_fingerprint"])
            self.assertIsNone(resumed_plan["blocked_worktree_fingerprint"])
            self.assertEqual(plan_file.read_text(encoding="utf-8"), user_plan_text)
            self.assertFalse((worktree / "reviewer.txt").exists())
            self.assertIn(
                "loop-manager scope violation G-001",
                git_command(worktree, "stash", "list"),
            )
            stashed_plan = git_command(
                worktree, "show", f"stash@{{0}}:{plan_path}"
            )
            self.assertIn("Reviewer mutation.", stashed_plan)
            resumed_agent.assert_called_once()
            self.assertEqual(resumed_agent.call_args.args[3], "worker")


@isolated_git_test
class BlockedAgentBaselineResumeUnitTests(unittest.TestCase):
    def test_interrupted_dirty_baseline_restore_reuses_verified_tree(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        loop_manager.scoped_blocking.ensure_schema(connection)
        loop_manager.blocked_checkpoint_state.ensure_schema(connection)
        before = loop_manager.GitSnapshot(
            'a' * 40, 'loop-manager/G-001', {},
            {'notes.txt': (('unstaged',), 'saved-content')}, {}, {}, {}, {})
        clean = loop_manager.GitSnapshot(
            before.head, before.branch, {}, {}, {}, {}, {}, {})
        baseline = json.dumps({'worktree_tree': 'b' * 40, 'index_tree': 'c' * 40})
        recovery = dict(version=1, state='stashed', source_snapshot=before.serialize(),
                        original_head=before.head, violation_head=before.head,
                        branch=before.branch, stash_marker='owned-stash', stash_oid='stash',
                        violation_ref='refs/loop-manager/diagnostic/test')
        connection.execute(
            'INSERT INTO projects (repo, base_branch, phase, agent_snapshot, agent_baseline, agent_recovery) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            ('repo', 'main', 'PLAN_BLOCKED', before.serialize(), baseline, json.dumps(recovery)))
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, "
            "content_hash, status, branch, worktree, workspace_owned) "
            "VALUES ('repo', 'G-001', 'docs/plans/[G-001] Work.md', 'Work', '[]', '[]', "
            "'high', 'hash', 'PLAN_BLOCKED', 'loop-manager/G-001', ?, 1)", (str(Path('/state/G-001')),)
        )
        connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES ('repo', 'G-001', 'plan', 'default_worktree_mutation', 'saved cause')"
        )
        connection.execute(
            "INSERT INTO blocked_checkpoints (repo, plan_id, request_identity, phase, reason_code, evidence) "
            "VALUES ('repo', 'G-001', 'owned-request', 'requested', 'default_worktree_mutation', 'saved cause')"
        )
        block = dict(connection.execute(
            "SELECT * FROM execplan_blocks WHERE plan_id = 'G-001'").fetchone())
        recovery.update(owner_plan_id='G-001', owner_block=block,
                        owner_request_identity='owned-request', checkpoint_generation=0)
        connection.execute(
            "UPDATE projects SET agent_recovery = ? WHERE repo = 'repo'",
            (json.dumps(recovery),))
        connection.commit()

        def update_project(db, repo, **values):
            db.execute('UPDATE projects SET agent_recovery = ? WHERE repo = ?',
                       (values['agent_recovery'], repo))
            db.commit()

        def effects(*, interrupted, observed=clean):
            port = mock.Mock()
            port._deserialize_blocked_agent_recovery.side_effect = blocked_agent_service._deserialize_blocked_agent_recovery
            port._serialize_blocked_agent_recovery.side_effect = blocked_agent_service._serialize_blocked_agent_recovery
            port._blocked_recovery_stash_oid.return_value = 'stash'
            port._config_entries.return_value = before.config
            port.capture_git_snapshot.side_effect = [observed, before]
            port._same_local_snapshot.return_value = False
            port._recovered_agent_local_state_matches.side_effect = [False, True]
            port.update_project.side_effect = update_project
            port.git.side_effect = lambda _worktree, command, *args: SimpleNamespace(
                stdout=('b' * 40 if command == 'write-tree' else 'd' * 40))
            if interrupted:
                port._restore_agent_baseline.side_effect = KeyboardInterrupt()
            return port

        first = effects(interrupted=True)
        with self.assertRaises(KeyboardInterrupt):
            blocked_agent_service._complete_blocked_agent_recovery(
                first, connection, 'repo', Path('/state/G-001'))
        saved = json.loads(connection.execute(
            "SELECT agent_recovery FROM projects WHERE repo = 'repo'").fetchone()[0])
        self.assertEqual(saved['state'], 'restoring_baseline')

        changed = loop_manager.GitSnapshot(
            before.head, before.branch, {},
            {'notes.txt': (('unstaged',), 'later-user-edit')}, {}, {}, {}, {})
        unsafe = effects(interrupted=False, observed=changed)
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service._complete_blocked_agent_recovery(
                unsafe, connection, 'repo', Path('/state/G-001'))
        unsafe._restore_agent_baseline.assert_not_called()

        connection.execute('SAVEPOINT changed_cause')
        connection.execute(
            "UPDATE blocked_checkpoints SET request_identity = 'new-cause' WHERE plan_id = 'G-001'")
        stale_cause = effects(interrupted=False)
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service._complete_blocked_agent_recovery(
                stale_cause, connection, 'repo', Path('/state/G-001'))
        stale_cause._restore_agent_baseline.assert_not_called()
        connection.execute('ROLLBACK TO changed_cause')
        connection.execute('RELEASE changed_cause')

        connection.execute('SAVEPOINT stale_generation')
        ExecutionCheckpointRepository(connection).record(
            'repo', 'G-001', expected_generation=0, operation=None,
            expected_head=None, target_base_oid=None, observed_remote_oid=None, snapshot=None)
        stale = effects(interrupted=False)
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service._complete_blocked_agent_recovery(
                stale, connection, 'repo', Path('/state/G-001'))
        stale._restore_agent_baseline.assert_not_called()
        connection.execute('ROLLBACK TO stale_generation')
        connection.execute('RELEASE stale_generation')

        first_read_tree = loop_manager.GitSnapshot(
            before.head, before.branch,
            {'notes.txt': ('H 100644 blob ' + 'e' * 40 + '\tnotes.txt',)},
            {'notes.txt': (('staged',), 'saved-content')}, {}, {}, {}, {})
        interrupted_after_read_tree = effects(interrupted=False, observed=first_read_tree)
        blocked_agent_service._complete_blocked_agent_recovery(
            interrupted_after_read_tree, connection, 'repo', Path('/state/G-001'))
        interrupted_after_read_tree._restore_agent_baseline.assert_called_once()
        interrupted_after_read_tree._mark_blocked_agent_isolated.assert_called_once()

        changed_after_read_tree = loop_manager.GitSnapshot(
            before.head, before.branch, first_read_tree.index,
            {'notes.txt': (('staged', 'unstaged'), 'later-user-edit')}, {}, {}, {}, {})
        unsafe_after_read_tree = effects(interrupted=False, observed=changed_after_read_tree)
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service._complete_blocked_agent_recovery(
                unsafe_after_read_tree, connection, 'repo', Path('/state/G-001'))
        unsafe_after_read_tree._restore_agent_baseline.assert_not_called()

        restarted = effects(interrupted=False)
        blocked_agent_service._complete_blocked_agent_recovery(
            restarted, connection, 'repo', Path('/state/G-001'))
        restarted._restore_agent_baseline.assert_called_once()
        restarted._mark_blocked_agent_isolated.assert_called_once()

    def test_isolated_recovery_accepts_advanced_checkpoint_generation(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        loop_manager.scoped_blocking.ensure_schema(connection)
        loop_manager.blocked_checkpoint_state.ensure_schema(connection)
        before = loop_manager.GitSnapshot(
            'a' * 40, 'loop-manager/G-001', {}, {}, {}, {}, {}, {})
        connection.execute(
            'INSERT INTO projects (repo, base_branch, phase, branch, worktree) '
            "VALUES ('repo', 'main', 'PLAN_BLOCKED', 'loop-manager/G-001', ?)", (str(Path('/state/G-001')),))
        connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, priority, "
            "content_hash, status, branch, worktree, workspace_owned) "
            "VALUES ('repo', 'G-001', 'docs/plans/[G-001] Work.md', 'Work', '[]', '[]', "
            "'high', 'hash', 'PLAN_BLOCKED', 'loop-manager/G-001', ?, 1)", (str(Path('/state/G-001')),))
        connection.execute(
            "INSERT INTO execplan_blocks (repo, plan_id, scope, reason_code, evidence) "
            "VALUES ('repo', 'G-001', 'plan', 'default_worktree_mutation', 'saved cause')")
        connection.execute(
            "INSERT INTO blocked_checkpoints (repo, plan_id, request_identity, phase, reason_code, evidence) "
            "VALUES ('repo', 'G-001', 'owned-request', 'requested', 'default_worktree_mutation', 'saved cause')")
        block = dict(loop_manager.scoped_blocking.plan_block(connection, 'repo', 'G-001'))
        recovery = dict(version=1, state='isolated', source_snapshot=before.serialize(),
                        baseline_snapshot=before.serialize(), original_head=before.head,
                        violation_head=before.head, branch=before.branch,
                        stash_marker='owned-stash', violation_ref='refs/loop-manager/diagnostic/test',
                        owner_plan_id='G-001', owner_block=block,
                        owner_request_identity='owned-request', checkpoint_generation=0)
        connection.execute("UPDATE projects SET agent_recovery = ? WHERE repo = 'repo'",
                           (json.dumps(recovery),))
        ExecutionCheckpointRepository(connection).record(
            'repo', 'G-001', expected_generation=0, operation='rebase',
            expected_head=before.head, target_base_oid='b' * 40,
            observed_remote_oid=None, snapshot=None)
        connection.commit()

        effects = mock.Mock()
        effects._deserialize_blocked_agent_recovery.side_effect = blocked_agent_service._deserialize_blocked_agent_recovery
        effects._config_entries.return_value = before.config
        effects.project_execplan.return_value = connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-001'").fetchone()
        effects._rebase_in_progress_for_branch.return_value = True
        self.assertTrue(blocked_agent_service.resume_interrupted_blocked_agent_recovery(
            effects, connection, Path('/repo'), Path('/state'), 'repo'))
        self.assertTrue(effects.make_worktree.call_args.kwargs['allow_rebase'])
        effects._complete_blocked_agent_recovery.assert_not_called()

        recovery['checkpoint_generation'] = 2
        connection.execute("UPDATE projects SET agent_recovery = ? WHERE repo = 'repo'",
                           (json.dumps(recovery),))
        effects.make_worktree.reset_mock()
        with self.assertRaises(loop_manager.AgentScopeError):
            blocked_agent_service.resume_interrupted_blocked_agent_recovery(
                effects, connection, Path('/repo'), Path('/state'), 'repo')
        effects.make_worktree.assert_not_called()
