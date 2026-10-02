import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import dummy_worker_sessions


class PlanningFingerprintTest(unittest.TestCase):
    def test_planning_input_path_boundary(self):
        cases = {
            b"AGENTS.md": True,
            b"docs/PRODUCT.md": True,
            b"docs/ARCHITECTURE.md": True,
            b"docs/requirements/Active.md": True,
            b"README.md": True,
            b"docs/requirements/.md": True,
            b".github/workflows/test.yml": True,
            b"src/app.py": True,
            b"docs/PLANS.md": False,
            b"docs/FUTURE_IDEAS.md": False,
            b"docs/plans/[G-999] Ignored.md": False,
            b"docs/requirements/archive/Old.md": False,
            b"docs/requirements/nested/Requirement.md": False,
            b"docs/requirements/NotMarkdown.txt": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(loop_manager._is_planning_input(path), expected)

    def test_planning_input_digest_is_ordered_and_unambiguous(self):
        entries = [
            (b"src/b.py", b"blob", b"two"),
            (b"src/a.py", b"blob", b"one"),
        ]

        fingerprint = loop_manager._planning_input_digest(entries)

        self.assertEqual(fingerprint, loop_manager._planning_input_digest(list(reversed(entries))))
        self.assertEqual(len(fingerprint), 64)
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"a", b"blob", b"bc")]),
            loop_manager._planning_input_digest([(b"ab", b"blob", b"c")]),
        )
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"src/a.py", b"blob", b"one")]),
            loop_manager._planning_input_digest(
                [(b"src/renamed.py", b"blob", b"one")]
            ),
        )
        self.assertNotEqual(
            loop_manager._planning_input_digest([(b"vendor/lib", b"blob", b"abc")]),
            loop_manager._planning_input_digest([(b"vendor/lib", b"commit", b"abc")]),
        )

    def test_planning_input_fingerprint_uses_origin_tree_and_input_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            initial = loop_manager.planning_input_fingerprint(repo, "main")

            (repo / "src/app.py").write_text("VALUE = 99\n", encoding="utf-8")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)
            (repo / "src/app.py").write_text("VALUE = 1\n", encoding="utf-8")

            (repo / "docs/notes.md").write_text("# Changed notes\n", encoding="utf-8")
            commit_and_push(repo, "Change excluded documentation")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)

            (repo / "docs/requirements/archive/Old.md").write_text(
                "# Changed archived requirement\n", encoding="utf-8"
            )
            commit_and_push(repo, "Change archived requirement")
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), initial)

            (repo / "docs/PRODUCT.md").write_text("# Changed product\n", encoding="utf-8")
            commit_and_push(repo, "Change product")
            changed = loop_manager.planning_input_fingerprint(repo, "main")
            self.assertNotEqual(changed, initial)

            git_command(repo, "checkout", "--detach", "HEAD^")
            self.assertNotEqual(
                git_command(repo, "rev-parse", "HEAD"),
                git_command(repo, "rev-parse", "origin/main"),
            )
            self.assertEqual(loop_manager.planning_input_fingerprint(repo, "main"), changed)

            git_command(repo, "checkout", "main")
            (repo / "src/app.py").rename(repo / "src/renamed.py")
            commit_and_push(repo, "Rename source")
            self.assertNotEqual(loop_manager.planning_input_fingerprint(repo, "main"), changed)

    def test_planning_input_fingerprint_includes_gitlink_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            first_target = git_command(repo, "rev-parse", "HEAD")
            git_command(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{first_target},vendor/library",
            )
            git_command(repo, "commit", "-m", "Add tracked submodule commit")
            git_command(repo, "push", "origin", "HEAD:main")
            first = loop_manager.planning_input_fingerprint(repo, "main")

            second_target = git_command(repo, "rev-parse", "HEAD")
            git_command(
                repo,
                "update-index",
                "--cacheinfo",
                f"160000,{second_target},vendor/library",
            )
            git_command(repo, "commit", "-m", "Update tracked submodule commit")
            git_command(repo, "push", "origin", "HEAD:main")

            self.assertNotEqual(
                loop_manager.planning_input_fingerprint(repo, "main"), first
            )

    @dummy_worker_sessions
    def test_planner_no_change_saves_fingerprint_and_skip_gate_uses_it(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            logs = []

            connection.execute(loop_manager.SCHEMA)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.run_codex", return_value=(0, "")
            ) as planner, mock.patch("loop_manager.operation_log", side_effect=lambda message, **kwargs: logs.append((message, kwargs))):
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                first = connection.execute(
                    "SELECT planning_fingerprint FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()["planning_fingerprint"]
                self.assertIsNotNone(first)
                self.assertEqual(planner.call_count, 1)
                self.assertEqual(logs[0], ("Planner 시작: 다음 ExecPlan 계획을 생성합니다.", {}))

                planner.reset_mock()
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                planner.assert_not_called()

                (repo / "docs/notes.md").write_text("# Other notes\n", encoding="utf-8")
                commit_and_push(repo, "Change excluded docs again")
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                planner.assert_not_called()

                (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                commit_and_push(repo, "Change source")
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                self.assertEqual(planner.call_count, 1)

            second = connection.execute(
                "SELECT planning_fingerprint FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()["planning_fingerprint"]
            self.assertNotEqual(second, first)

    @dummy_worker_sessions
    def test_requirement_archive_move_does_not_run_planner(self):
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
                "loop_manager.run_codex", return_value=(0, "")
            ) as planner:
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                completed_fingerprint = connection.execute(
                    "SELECT planning_fingerprint FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()["planning_fingerprint"]
                planner.reset_mock()

                active = repo / "docs/requirements/Active.md"
                archived = repo / "docs/requirements/archive/Active.md"
                active.rename(archived)
                commit_and_push(repo, "Archive completed requirement")

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    0,
                )
                planner.assert_not_called()

            row = connection.execute(
                "SELECT processed_sha, planning_fingerprint FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(row["processed_sha"], git_command(repo, "rev-parse", "origin/main"))
            self.assertEqual(row["planning_fingerprint"], completed_fingerprint)
            self.assertNotEqual(
                row["planning_fingerprint"],
                loop_manager.planning_input_fingerprint(repo, "main"),
            )

    @dummy_worker_sessions
    def test_planner_failure_and_invalid_result_do_not_save_fingerprint(self):
        def fail_planner(_worktree, *_args, **_kwargs):
            return 1, "planner failed"

        def write_invalid_file(worktree, *_args, **_kwargs):
            (worktree / "unexpected.txt").write_text("invalid\n", encoding="utf-8")
            return 0, ""

        for name, planner_effect in (
            ("failure", fail_planner),
            ("invalid-result", write_invalid_file),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
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
                    "loop_manager.run_codex", side_effect=planner_effect
                ) as planner:
                    for _attempt in range(2):
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

                row = connection.execute(
                    "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                    (str(repo),),
                ).fetchone()
                self.assertEqual(planner.call_count, 2)
                self.assertEqual(row["phase"], "PLANNING")
                self.assertIsNone(row["planning_fingerprint"])

    @dummy_worker_sessions
    def test_new_execplan_saves_cataloged_input_fingerprint(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return 0, ""
            return 1, "stop after planning"

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            expected_fingerprint = loop_manager.planning_input_fingerprint(repo, "main")

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

            row = connection.execute(
                "SELECT phase, planning_fingerprint FROM projects WHERE repo = ?",
                (str(repo),),
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(row["phase"], "WORKING")
            self.assertEqual(row["planning_fingerprint"], expected_fingerprint)
