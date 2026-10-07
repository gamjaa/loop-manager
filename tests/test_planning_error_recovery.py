import hashlib
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager
from tests.support.git import (
    git_command,
    make_remote_repository,
    commit_and_push,
    advance_remote_file,
)
from tests.support.worker import isolated_scope_resources
from tests.support.worker import dummy_worker_sessions


class PlanningErrorRecoveryTest(unittest.TestCase):
    def test_ignored_scope_error_restarts_same_input_without_repeating_planner(self):
        from dataclasses import replace
        from loop_manager_internal.planning.selection_service import PlanningSelectionService
        from loop_manager_internal.storage import runtime_state
        for preserved in (False, True):
            for mutation in ('ignored', 'tracked', 'config', 'current_tracked', 'race', 'ownership', 'other_error'):
                with self.subTest(preserved=preserved, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                    repo = make_remote_repository(directory)
                    (repo / '.gitignore').write_text('cache.bin\n')
                    commit_and_push(repo, 'Ignore cache')
                    git_command(repo, 'switch', '--detach')
                    state_dir = Path(directory) / 'state'
                    state_dir.mkdir()
                    current = loop_manager.capture_git_snapshot(repo)
                    start = replace(current, dirty={'cache.bin': (('untracked',), 'legacy')})
                    result = current
                    if mutation == 'tracked':
                        result = replace(result, dirty={'src/app.py': (('unstaged',), 'actual violation')})
                    if mutation == 'config':
                        result = replace(result, config={**result.config, 'local:core.hooksPath': ('foreign',)})
                    with self.assertRaises(loop_manager.AgentScopeError) as failure:
                        loop_manager.validate_agent_scope('planner', repo, start, result)
                    error = str(failure.exception) if mutation != 'other_error' else 'planner failed'
                    if mutation == 'current_tracked':
                        (repo / 'src/app.py').write_text('VALUE = 99\n')
                        current = loop_manager.capture_git_snapshot(repo)
                    database = state_dir / 'state.db'
                    connection = sqlite3.connect(database)
                    connection.row_factory = sqlite3.Row
                    connection.execute(loop_manager.SCHEMA)
                    loop_manager._ensure_execplans_schema(connection)
                    connection.execute("INSERT INTO projects (repo, base_branch, phase, worktree, "
                        "planning_workspace_owned, planning_attempt_fingerprint, active_sha, "
                        "planner_session_id, planning_result, planning_retry_count, planning_error, "
                        "planning_validation_error, planning_error_fingerprint, agent_role, "
                        "agent_snapshot, agent_result_snapshot) "
                        "VALUES (?, 'main', 'PLANNING', ?, 1, 'same', ?, 'saved-session', '', 3, ?, ?, "
                        "'same', 'planner', ?, ?)",
                        (str(repo), str(repo), current.head, error, error, start.serialize(), result.serialize()))
                    connection.commit()
                    repository = runtime_state.RuntimeStateRepository(connection)
                    if preserved:
                        repository.suspend_planning(repository.planning(str(repo)))
                    connection.close()
                    # Reopen the persisted state, using the real scope validator.
                    connection = sqlite3.connect(database)
                    connection.row_factory = sqlite3.Row
                    self.addCleanup(connection.close)
                    repository = runtime_state.RuntimeStateRepository(connection)
                    context = SimpleNamespace(repo=str(repo), repo_path=repo, state_dir=state_dir,
                                              base_branch='main', recovery_request_id=None,
                                              local_environment_recheck=False)
                    agent = loop_manager._PlanningAgentPorts(connection, context, None)
                    git = SimpleNamespace(validate_workspace_identity=mock.Mock(),
                                          input_fingerprint=lambda *_: 'same')
                    if mutation == 'ownership':
                        git.validate_workspace_identity.side_effect = RuntimeError('wrong owner')
                    state = SimpleNamespace(planning=repository.planning,
                        transaction=lambda: runtime_state._transaction(connection),
                        schedule=lambda _: SimpleNamespace(active=None),
                        planning_release_pending=lambda _: False, preserve_diagnostics=mock.Mock(),
                        retry_diagnostic_preservation=mock.Mock())
                    service = PlanningSelectionService(SimpleNamespace(
                        agent=agent, git=git, state=state, events=SimpleNamespace(error=mock.Mock())))
                    original = repository.planning(str(repo))
                    from loop_manager_internal.storage import maintenance_state
                    run = SimpleNamespace(row=original, context=context, base_sha=current.head,
                        publication_recovery_verified=False, maintenance_planning=False,
                        maintenance_cycle=SimpleNamespace(phase=maintenance_state.MaintenancePhase.IDLE))
                    if mutation == 'race':
                        observations = [current, replace(current, head='foreign')]
                        capture = mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=observations)
                    else:
                        capture = mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=current)
                    with capture, mock.patch.object(loop_manager, 'run_codex') as planner, \
                            mock.patch.object(service, '_suspend_failed_planning', return_value='waiting'), \
                            mock.patch.object(service, '_continue_published_work', return_value='waiting'):
                        self.assertEqual(service._gate_preserved_error(run),
                                         None if mutation == 'ignored' else 'waiting')
                        planner.assert_not_called()
                    if mutation == 'ignored':
                        saved = repository.planning(str(repo))
                        self.assertIsNone(saved.planning_error)
                        self.assertEqual(saved.planner_session_id, 'saved-session')
                        self.assertEqual(saved.planning_retry_count, 3)
                        self.assertEqual(saved.agent_snapshot, start.serialize())
                        self.assertEqual(saved.agent_result_snapshot, result.serialize())
                        self.assertEqual(saved.planning_result_completed, 1)
                        self.assertFalse(service._recover_ignored_error(run))
                    else:
                        self.assertEqual(repository.planning(str(repo)), original)

    @dummy_worker_sessions
    def test_planning_scope_error_preserves_unauthorized_result_after_input_advance(self):
        isolation = mock.patch.object(loop_manager, '_agent_git_resources', side_effect=isolated_scope_resources)
        isolation.start()
        self.addCleanup(isolation.stop)
        planner_sessions = []

        def invalid_planner(worktree, _state, _config, role_name, _prompt, **kwargs):
            self.assertEqual(role_name, "planner")
            planner_sessions.append(kwargs["session_id"])
            (worktree / "unexpected.txt").write_text(
                f"invalid {len(planner_sessions)}\n",
                encoding="utf-8",
            )
            git_command(worktree, "add", "unexpected.txt")
            git_command(worktree, "commit", "-m", "Invalid Planner output")
            session_id = kwargs["session_id"] or "planner-session"
            return loop_manager.CodexResult(0, "", session_id)

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=invalid_planner
            ) as planner:
                for _attempt in range(4):
                    self.assertEqual(
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        ),
                        1,
                    )

                blocked = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                old_worktree = Path(blocked["worktree"])
                old_fingerprint = blocked["planning_error_fingerprint"]
                self.assertEqual(blocked["planning_retry_count"], 3)
                self.assertEqual(
                    blocked["planning_error"],
                    blocked["planning_validation_error"],
                )
                self.assertEqual(
                    blocked["planning_attempt_fingerprint"],
                    old_fingerprint,
                )
                self.assertEqual(blocked["phase"], "PLANNING")
                self.assertIsNone(blocked["branch"])
                self.assertIsNone(blocked["plan_path"])
                self.assertTrue(old_worktree.is_dir())
                self.assertEqual(planner_sessions, [None] + ["planner-session"] * 3)

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                self.assertEqual(planner.call_count, 4)

                advance_remote_file(directory, repo, 'src/app.py', 'VALUE = 2\n', 'Change planning input')
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            restarted = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner.call_count, 4)
            self.assertEqual(planner_sessions[-1], "planner-session")
            self.assertTrue(old_worktree.exists())
            self.assertEqual(Path(restarted["worktree"]), old_worktree)
            self.assertEqual(
                restarted["planning_attempt_fingerprint"],
                old_fingerprint,
            )
            self.assertEqual(restarted["planning_retry_count"], 3)
            self.assertEqual(restarted["planning_error"], blocked["planning_error"])
            self.assertEqual(restarted["planning_error_fingerprint"], old_fingerprint)
            self.assertEqual((old_worktree / "unexpected.txt").read_text(), "invalid 4\n")

    @dummy_worker_sessions
    def test_planning_error_gate_precedes_dirty_base_worktree_check(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            preserved_worktree = state_dir / "preserved-planning-worktree"
            preserved_worktree.mkdir(parents=True)
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            error = "Planner는 docs/plans의 ExecPlan 하나만 변경해야 합니다."
            fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, planning_error, planning_error_fingerprint,
                    phase, worktree
                ) VALUES (?, 'main', ?, ?, 'PLANNING', ?)
                """,
                (str(repo), error, fingerprint, str(preserved_worktree)),
            )
            connection.commit()
            (repo / "local-uncommitted-change.txt").write_text("dirty\n", encoding="utf-8")
            stderr = io.StringIO()

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch("loop_manager.run_codex") as planner, redirect_stderr(stderr):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            self.assertIn(error, stderr.getvalue())
            self.assertIn(str(preserved_worktree), stderr.getvalue())
            planner.assert_not_called()

    @dummy_worker_sessions
    def test_unowned_failed_planning_workspace_survives_restarts_and_base_only_change(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            database = Path(directory) / "state.sqlite"
            fingerprint = loop_manager.planning_input_fingerprint(repo, "main")
            project_id = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
            worktree = state_dir / "worktrees" / project_id / "planning" / fingerprint
            worktree.parent.mkdir(parents=True)
            git_command(repo, "worktree", "add", "--detach", str(worktree), "origin/main")
            marker = worktree / "saved.txt"
            marker.write_text("preserve planning output\n", encoding="utf-8")
            git_command(worktree, "add", "saved.txt")
            saved_head = git_command(worktree, "rev-parse", "HEAD")
            saved_index = git_command(worktree, "ls-files", "--stage")
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                "INSERT INTO projects (repo, base_branch, phase, active_sha, "
                "worktree, planning_attempt_fingerprint, planning_workspace_owned, "
                "planning_error, planning_error_fingerprint, planner_session_id, "
                "planning_validation_error) "
                "VALUES (?, 'main', 'PLANNING', ?, ?, ?, 0, ?, ?, ?, ?)",
                (str(repo), saved_head, str(worktree), fingerprint,
                 "saved ownership failure", fingerprint, "saved-session",
                 "saved diagnostics"),
            )
            connection.commit()
            (repo / "docs/notes.md").write_text("# updated note\n", encoding="utf-8")
            commit_and_push(repo, "Advance base without planning input change")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"),
                             fingerprint)
            saved_registration = git_command(repo, "worktree", "list", "--porcelain")

            with mock.patch("loop_manager.validate_repository", return_value=repo), \
                    mock.patch("loop_manager.remote_base", return_value="main"), \
                    mock.patch("loop_manager.run_codex") as planner:
                for attempt in range(4):
                    if attempt == 2:
                        connection.close()
                        connection = sqlite3.connect(database)
                        connection.row_factory = sqlite3.Row
                    self.assertEqual(loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"),
                        "main", False,
                    ), 1)
                    row = connection.execute(
                        "SELECT planning_error, planning_error_fingerprint, worktree, "
                        "planning_workspace_owned, planner_session_id, "
                        "planning_validation_error FROM projects WHERE repo = ?",
                        (str(repo),),
                    ).fetchone()
                    self.assertEqual(tuple(row), (
                        "saved ownership failure", fingerprint, str(worktree), 0,
                        "saved-session", "saved diagnostics",
                    ))
                planner.assert_not_called()
            self.assertEqual(git_command(worktree, "rev-parse", "HEAD"), saved_head)
            self.assertEqual(git_command(worktree, "ls-files", "--stage"), saved_index)
            self.assertEqual(git_command(repo, "worktree", "list", "--porcelain"),
                             saved_registration)
            self.assertEqual(marker.read_text(encoding="utf-8"),
                             "preserve planning output\n")
            connection.close()

    def test_general_publication_restart_can_roll_back_with_cleanup_failure(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE general_planning_publications (repo TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO general_planning_publications (repo) VALUES ('/repo')"
        )
        connection.commit()
        intent = SimpleNamespace(
            manifest="manifest", snapshot_token="token", commit_sha="commit",
            base_sha="base",
        )
        result = SimpleNamespace(mutable_snapshot=SimpleNamespace(token="token"))
        remote = SimpleNamespace(stdout="remote\n")
        with mock.patch("loop_manager.general_planning_publication", return_value=intent), \
                mock.patch("loop_manager._planning_result_from_manifest",
                           return_value=result), \
                mock.patch("loop_manager._validate_general_planning_commit"), \
                mock.patch("loop_manager.git", return_value=remote), \
                mock.patch("loop_manager._git_commit_is_ancestor",
                           side_effect=(False, True)), \
                mock.patch("loop_manager._clear_active_in_current_transaction"):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                with loop_manager.runtime_state._transaction(connection):
                    self.assertEqual(loop_manager.recover_general_planning_batch(
                        connection, "/repo", Path("/planning"), "main"
                    ), "restart")
                    self.assertIsNone(connection.execute(
                        "SELECT repo FROM general_planning_publications"
                    ).fetchone())
                    raise RuntimeError("cleanup failed")
        self.assertEqual(connection.execute(
            "SELECT repo FROM general_planning_publications"
        ).fetchone()[0], "/repo")
        connection.close()

    @dummy_worker_sessions
    def test_changed_remote_input_rebases_failed_planning_in_preserved_workspace(self):
        seen_source_contents = []

        def fail_planner(worktree, *_args, **_kwargs):
            seen_source_contents.append(
                (worktree / "src/app.py").read_text(encoding="utf-8")
            )
            return 1, "planner failed"

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=fail_planner
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                first = connection.execute(
                    "SELECT active_sha, worktree FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()

                (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                commit_and_push(repo, "Change source while planning is pending")

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            second = connection.execute(
                "SELECT active_sha, worktree, phase, planning_fingerprint FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(seen_source_contents, ["VALUE = 1\n", "VALUE = 2\n"])
            self.assertNotEqual(second["active_sha"], first["active_sha"])
            self.assertEqual(second["worktree"], first["worktree"])
            self.assertTrue(Path(first["worktree"]).exists())
            self.assertEqual(second["active_sha"], git_command(repo, "rev-parse", "origin/main"))
            self.assertEqual(second["phase"], "PLANNING")
            self.assertIsNone(second["planning_fingerprint"])
