import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import loop_manager
from loop_manager_internal.git.command_adapter import ExternalObservationError, revision_document_or_none
from loop_manager_internal.planning.planner_validation import (
    FutureIdeaItemsEffects, _future_idea_items, validate_planner_result,
    _ready_plan_snapshot_at_revision,
)
from loop_manager_internal.maintenance.maintenance_planning import MaintenancePlanningError
from loop_manager_internal.runtime.contracts import GitLockError
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository
from tests.support.worker import dummy_worker_sessions


class RevisionDocumentObservationTests(unittest.TestCase):
    def test_git_lock_keeps_its_retry_category(self):
        git = mock.Mock(side_effect=GitLockError('different lock text'))
        with self.assertRaises(GitLockError):
            revision_document_or_none(git, Path('/repo'), 'base', 'docs/PRODUCT.md')
        self.assertEqual(git.call_count, 1)

    def test_failed_show_requires_confirmed_tree_absence(self):
        calls = []
        def git(_repo, operation, *args, **kwargs):
            calls.append(operation)
            if operation == "show":
                return SimpleNamespace(returncode=128, stdout="changed diagnostic")
            return SimpleNamespace(returncode=0, stdout="100644 blob deadbeef\tdocs/FUTURE_IDEAS.md\0")

        with self.assertRaises(ExternalObservationError):
            revision_document_or_none(git, Path("/repo"), "base", "docs/FUTURE_IDEAS.md")
        self.assertEqual(calls, ["show", "ls-tree"])

    def test_confirmed_absence_does_not_invent_previous_future_ideas(self):
        def git(_repo, operation, *args, **kwargs):
            return SimpleNamespace(returncode=128 if operation == "show" else 0, stdout="")
        effects = FutureIdeaItemsEffects(
            _future_idea_items_from_text=lambda value: ((value,),) if value else (),
            git=git,
        )
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(_future_idea_items(effects, Path(directory), "base"), ())

    def test_future_idea_file_read_error_remains_operational(self):
        effects = FutureIdeaItemsEffects(
            _future_idea_items_from_text=lambda value: (), git=mock.Mock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'docs/FUTURE_IDEAS.md'
            path.parent.mkdir()
            path.write_text('idea', encoding='utf-8')
            with mock.patch.object(Path, 'read_text', side_effect=OSError('unreadable')):
                with self.assertRaises(ExternalObservationError):
                    _future_idea_items(effects, Path(directory), 'base')
        effects.git.assert_not_called()

    def test_planner_tree_failure_does_not_become_agent_correction(self):
        effects = SimpleNamespace(
            changed_files=lambda *args: [],
            mutable_snapshot_token=lambda *args: 'snapshot',
            git=mock.Mock(side_effect=RuntimeError('different failure text')),
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ExternalObservationError):
                validate_planner_result(
                    effects, Path(directory), 'base',
                    ['docs/plans/[G-001] Proposed.md'],
                )

    def test_ready_revision_read_failure_remains_operational(self):
        effects = SimpleNamespace(git_bytes=mock.Mock(side_effect=RuntimeError('Git lock')))
        with self.assertRaises(ExternalObservationError):
            _ready_plan_snapshot_at_revision(
                effects, Path('/repo'), 'base', {'path': 'docs/plans/plan.md'}
            )

    def test_confirmed_missing_ready_revision_keeps_safety_decision(self):
        git_bytes = mock.Mock(side_effect=[RuntimeError('missing'), b''])
        effects = SimpleNamespace(git_bytes=git_bytes)
        with self.assertRaises(MaintenancePlanningError):
            _ready_plan_snapshot_at_revision(
                effects, Path('/repo'), 'base', {'path': 'docs/plans/plan.md'}
            )


class PlannerValidationRecoveryTest(unittest.TestCase):
    @dummy_worker_sessions
    def test_first_planner_validation_retry_resumes_session_and_clears_state(self):
        planner_calls = []

        def agent(worktree, _state, _config, role_name, prompt, **kwargs):
            if role_name == "planner":
                planner_calls.append((kwargs["session_id"], prompt))
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                if len(planner_calls) == 1:
                    write_execplan(
                        worktree,
                        "G-001",
                        "Generated",
                        depends_on=("G-999",),
                    )
                else:
                    write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

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
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                first = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(first["planning_retry_count"], 0)
                self.assertEqual(first["planner_session_id"], "planner-session")
                self.assertIn("존재하지 않는 depends_on ID", first["planning_validation_error"])
                self.assertIsNone(first["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            final = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls[0][0], None)
            self.assertEqual(planner_calls[1][0], "planner-session")
            self.assertIn(first["planning_validation_error"], planner_calls[1][1])
            self.assertEqual(final["phase"], "WORKING")
            self.assertEqual(final["planning_retry_count"], 0)
            self.assertIsNone(final["planner_session_id"])
            self.assertIsNone(final["planning_validation_error"])
            self.assertIsNone(final["planning_error"])

    @dummy_worker_sessions
    def test_interrupted_first_planner_with_result_resumes_before_validation(self):
        planner_sessions = []
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            nonlocal planner_calls
            if role_name != "planner":
                return loop_manager.CodexResult(1, "stop after planning")
            planner_calls += 1
            planner_sessions.append(kwargs["session_id"])
            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
            write_execplan(worktree, "G-001", "Generated")
            if planner_calls == 1:
                # The CLI starts a session and changes the worktree, then the
                # process dies before it can report a completed result.
                loop_manager.update_project(
                    connection,
                    str(repo),
                    planner_session_id="planner-session",
                )
                raise RuntimeError("process interrupted after Planner edit")
            return loop_manager.CodexResult(0, "", "planner-session")

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
            ), mock.patch("loop_manager.run_codex", side_effect=agent):
                with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    )

                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertEqual(interrupted["planning_result_completed"], 0)
                self.assertEqual(interrupted["planner_session_id"], "planner-session")
                self.assertTrue(
                    (Path(interrupted["worktree"]) / "docs/plans/[G-001] Generated.md").is_file()
                )

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

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_sessions, [None, "planner-session"])
            self.assertEqual(resumed["phase"], "WORKING")

    @dummy_worker_sessions
    def test_interrupted_planner_correction_before_edit_resumes_with_invalid_diff(self):
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            nonlocal planner_calls
            self.assertEqual(role_name, "planner")
            planner_calls += 1
            if planner_calls == 1:
                (worktree / "unexpected.txt").write_text(
                    "invalid\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(0, "", "planner-session")
            if planner_calls == 2:
                saved = connection.execute(
                    "SELECT planning_result_pending_validation FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(saved["planning_result_pending_validation"], 1)
                raise RuntimeError("process interrupted before Planner correction")
            self.assertEqual(_kwargs["session_id"], "planner-session")
            return loop_manager.CodexResult(1, "planner timeout", "planner-session")

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", side_effect=agent
            ):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                with self.assertRaisesRegex(RuntimeError, "process interrupted"):
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    )

                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertEqual(interrupted["planning_retry_count"], 0)
                self.assertTrue((Path(interrupted["worktree"]) / "unexpected.txt").is_file())
                plan_sha = git_command(repo, "rev-parse", "origin/main")
                git_command(
                    Path(directory) / "remote.git",
                    "update-ref",
                    "refs/heads/planner-correction-update",
                    plan_sha,
                )

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls, 6)
            self.assertEqual(resumed["planning_retry_count"], 0)
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_error"])
            self.assertIsNotNone(resumed["planning_validation_error"])
            self.assertIn(
                loop_manager.TRANSIENT_AGENT_ERROR_PREFIX,
                resumed["last_error"],
            )
            self.assertEqual(
                git_command(
                    repo,
                    "rev-parse",
                    "refs/remotes/origin/planner-correction-update",
                ),
                plan_sha,
            )

    @dummy_worker_sessions
    def test_planner_cli_failure_does_not_consume_validation_retry(self):
        planner_calls = 0

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            nonlocal planner_calls
            if role_name != "planner":
                return loop_manager.CodexResult(1, "stop after planning")
            planner_calls += 1
            invalid = worktree / "unexpected.txt"
            if planner_calls == 1:
                invalid.write_text("invalid\n", encoding="utf-8")
                return loop_manager.CodexResult(0, "", "planner-session")
            if 2 <= planner_calls <= 5:
                saved_retry_count = connection.execute(
                    "SELECT planning_retry_count FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()["planning_retry_count"]
                self.assertEqual(saved_retry_count, 0)
                return loop_manager.CodexResult(1, "planner timeout", "planner-session")
            invalid.unlink()
            (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
            write_execplan(worktree, "G-001", "Generated")
            return loop_manager.CodexResult(0, "", "planner-session")

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
                "loop_manager.run_codex", side_effect=agent
            ):
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
                self.assertEqual(
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        Path("unused.ini"),
                        "main",
                        False,
                    ),
                    0,
                )
                failed = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(failed["planning_retry_count"], 0)
                self.assertIsNotNone(failed["planning_validation_error"])
                self.assertIsNone(failed["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            final = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls, 6)
            self.assertEqual(final["phase"], "WORKING")
            self.assertEqual(final["planning_retry_count"], 0)
            self.assertIsNone(final["planning_validation_error"])

    @dummy_worker_sessions
    def test_saved_third_retry_is_validated_without_a_fourth_planner_call(self):
        def invalid_planner(worktree, *_args, **_kwargs):
            (worktree / "unexpected.txt").write_text("invalid\n", encoding="utf-8")
            return loop_manager.CodexResult(0, "", "planner-session")

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
                "loop_manager.run_codex", side_effect=invalid_planner
            ) as planner:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )
                loop_manager.update_project(
                    connection,
                    str(repo),
                    planning_retry_count=loop_manager.MAX_PLANNER_VALIDATION_RETRIES,
                )
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            row = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner.call_count, 1)
            self.assertEqual(row["planning_retry_count"], 3)
            self.assertIsNotNone(row["planning_error"])
