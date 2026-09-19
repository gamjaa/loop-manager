import json
import io
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import loop_manager


def make_catalog_root(directory: str) -> Path:
    root = Path(directory)
    (root / "docs/plans").mkdir(parents=True)
    (root / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")
    return root


def write_execplan(
    root: Path,
    plan_id: str,
    title: str,
    *,
    sources: tuple[str, ...] = ("docs/PRODUCT.md",),
    depends_on: tuple[str, ...] = (),
    complexity: str = "low",
    risk: str = "low",
) -> Path:
    source_lines = "sources: []" if not sources else "sources:\n" + "".join(
        f"  - {source}\n" for source in sources
    ).rstrip("\n")
    dependency_lines = "depends_on: []" if not depends_on else "depends_on:\n" + "".join(
        f"  - {dependency}\n" for dependency in depends_on
    ).rstrip("\n")
    plan = root / "docs/plans" / f"[{plan_id}] {title}.md"
    plan.write_text(
        f"""---
id: {plan_id}
title: {title}
{source_lines}
{dependency_lines}
complexity: {complexity}
risk: {risk}
---

## Purpose

Test plan.
""",
        encoding="utf-8",
    )
    return plan


def git_command(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()


def make_remote_repository(directory: str) -> Path:
    parent = Path(directory)
    remote = parent / "remote.git"
    repo = parent / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    repo.mkdir()
    git_command(repo, "init", "--initial-branch=main")
    git_command(repo, "config", "user.email", "loop-manager@example.com")
    git_command(repo, "config", "user.name", "Loop Manager Test")
    git_command(repo, "remote", "add", "origin", str(remote))

    files = {
        "AGENTS.md": "# Agents\n",
        "README.md": "# Readme\n",
        "config/settings.ini": "setting=true\n",
        "docs/ARCHITECTURE.md": "# Architecture\n",
        "docs/PLANS.md": "# Plans\n",
        "docs/PRODUCT.md": "# Product\n",
        "docs/notes.md": "# Notes\n",
        "docs/requirements/Active.md": "# Active\n",
        "docs/requirements/archive/Old.md": "# Old\n",
        "src/app.py": "VALUE = 1\n",
        "tests/test_app.py": "def test_app(): pass\n",
    }
    for relative_path, contents in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    git_command(repo, "add", "-A")
    git_command(repo, "commit", "-m", "Initial")
    git_command(repo, "push", "-u", "origin", "main")
    return repo


def commit_and_push(repo: Path, message: str) -> None:
    git_command(repo, "add", "-A")
    git_command(repo, "commit", "-m", message)
    git_command(repo, "push", "origin", "HEAD:main")


class LoopManagerTest(unittest.TestCase):
    def test_codex_uses_mutually_exclusive_sandbox_options(self):
        self.assertEqual(loop_manager.codex_sandbox_arguments(False), ["--approve-for-me"])
        self.assertEqual(loop_manager.codex_sandbox_arguments(True), ["--sandbox", "read-only"])

    def test_schema_and_project_update(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute("INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')")

        loop_manager.update_project(
            connection,
            "repo",
            phase="WORKING",
            active_sha="abc",
            planning_fingerprint="fingerprint",
            planning_retry_count=2,
            planner_session_id="session-1",
        )

        row = connection.execute("SELECT * FROM projects WHERE repo = 'repo'").fetchone()
        self.assertEqual(
            (
                row["phase"],
                row["active_sha"],
                row["planning_fingerprint"],
                row["planning_retry_count"],
                row["planner_session_id"],
            ),
            ("WORKING", "abc", "fingerprint", 2, "session-1"),
        )

    def test_existing_database_adds_planning_fingerprint_without_losing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                """
                CREATE TABLE projects (
                    repo TEXT PRIMARY KEY,
                    base_branch TEXT NOT NULL,
                    processed_sha TEXT,
                    active_sha TEXT,
                    phase TEXT,
                    branch TEXT,
                    worktree TEXT,
                    pr_url TEXT,
                    last_error TEXT
                )
                """
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch, phase) VALUES ('repo', 'main', 'WORKING')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(projects)")
                }
                row = connection.execute(
                    "SELECT * FROM projects WHERE repo = 'repo'"
                ).fetchone()

        self.assertIn("plan_path", columns)
        self.assertIn("planning_fingerprint", columns)
        self.assertIn("planning_attempt_fingerprint", columns)
        self.assertIn("planning_retry_count", columns)
        self.assertIn("planner_session_id", columns)
        self.assertIn("planning_validation_error", columns)
        self.assertIn("planning_error", columns)
        self.assertIn("planning_result_pending_validation", columns)
        self.assertIn("planning_result_completed", columns)
        self.assertIn("planning_error_fingerprint", columns)
        self.assertEqual((row["base_branch"], row["phase"]), ("main", "WORKING"))
        self.assertIsNone(row["planning_fingerprint"])
        self.assertEqual(row["planning_retry_count"], 0)
        self.assertEqual(row["planning_result_pending_validation"], 0)
        self.assertIsNone(row["planning_error"])

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

    def test_planner_no_change_saves_fingerprint_and_skip_gate_uses_it(self):
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
                first = connection.execute(
                    "SELECT planning_fingerprint FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()["planning_fingerprint"]
                self.assertIsNotNone(first)
                self.assertEqual(planner.call_count, 1)

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
                    git_command(worktree, "add", "-A")
                    git_command(worktree, "commit", "-m", "Invalid Planner output")
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
    def test_planner_commit_failure_preserves_valid_result_without_retry_penalty(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

        original_git = loop_manager.git

        def reject_planning_commit(worktree, *arguments, **kwargs):
            if arguments[:1] == ("commit",) and arguments[-1].startswith("Add ExecPlan"):
                raise RuntimeError("commit hook rejected")
            return original_git(worktree, *arguments, **kwargs)

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
                with mock.patch("loop_manager.git", side_effect=reject_planning_commit):
                    with self.assertRaisesRegex(RuntimeError, "commit hook rejected"):
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            Path("unused.ini"),
                            "main",
                            False,
                        )

                failed_commit = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(failed_commit["planning_retry_count"], 0)
                self.assertEqual(failed_commit["planning_result_pending_validation"], 1)
                self.assertIsNone(failed_commit["planning_validation_error"])
                self.assertIsNone(failed_commit["planning_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_validation_error"])
            self.assertIsNone(resumed["planning_error"])

    def test_interrupted_successful_planner_result_is_validated_before_resume(self):
        roles = []

        def agent(worktree, _state, _config, role_name, _prompt, **_kwargs):
            roles.append(role_name)
            if role_name == "planner":
                (worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
                write_execplan(worktree, "G-001", "Generated")
                return loop_manager.CodexResult(0, "", "planner-session")
            return loop_manager.CodexResult(1, "stop after planning")

        original_changed_files = loop_manager.changed_files
        changed_files_calls = 0

        def interrupt_after_result_is_persisted(*args, **kwargs):
            nonlocal changed_files_calls
            changed_files_calls += 1
            if changed_files_calls == 2:
                raise RuntimeError("process interrupted after Planner result")
            return original_changed_files(*args, **kwargs)

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
                with mock.patch(
                    "loop_manager.changed_files",
                    side_effect=interrupt_after_result_is_persisted,
                ):
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
                self.assertEqual(interrupted["planner_session_id"], "planner-session")
                self.assertEqual(interrupted["planning_result_pending_validation"], 1)
                self.assertIsNone(interrupted["planning_validation_error"])

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(roles, ["planner", "worker"])
            self.assertEqual(resumed["phase"], "WORKING")
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_validation_error"])
            self.assertIsNone(resumed["planning_error"])


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

                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            resumed = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            self.assertEqual(planner_calls, 3)
            self.assertEqual(resumed["planning_retry_count"], 0)
            self.assertEqual(resumed["planning_result_pending_validation"], 0)
            self.assertIsNone(resumed["planning_error"])

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
            if planner_calls == 2:
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
            self.assertEqual(planner_calls, 3)
            self.assertEqual(final["phase"], "WORKING")
            self.assertEqual(final["planning_retry_count"], 0)
            self.assertIsNone(final["planning_validation_error"])

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

    def test_planning_error_blocks_same_input_and_changed_input_starts_fresh(self):
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

                (repo / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
                commit_and_push(repo, "Change planning input")
                self.assertEqual(
                    loop_manager.run_project(
                        connection, state_dir, str(repo), Path("unused.ini"), "main", False
                    ),
                    1,
                )

            restarted = connection.execute(
                "SELECT * FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()
            diagnostic = loop_manager.planning_diagnostics_path(
                state_dir, str(repo), old_fingerprint
            )
            self.assertEqual(planner.call_count, 5)
            self.assertEqual(planner_sessions[-1], None)
            self.assertFalse(old_worktree.exists())
            self.assertNotEqual(Path(restarted["worktree"]), old_worktree)
            self.assertNotEqual(
                restarted["planning_attempt_fingerprint"],
                old_fingerprint,
            )
            self.assertEqual(restarted["planning_retry_count"], 0)
            self.assertIsNone(restarted["planning_error"])
            self.assertIsNone(restarted["planning_error_fingerprint"])
            self.assertTrue(diagnostic.is_file())
            diagnostic_text = diagnostic.read_text(encoding="utf-8")
            self.assertIn(blocked["planning_error"], diagnostic_text)
            self.assertIn("unexpected.txt", diagnostic_text)
            self.assertIn("+invalid 4", diagnostic_text)

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

    def test_changed_remote_input_restarts_failed_planning_from_latest_origin(self):
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
            self.assertNotEqual(second["worktree"], first["worktree"])
            self.assertFalse(Path(first["worktree"]).exists())
            self.assertEqual(second["active_sha"], git_command(repo, "rev-parse", "origin/main"))
            self.assertEqual(second["phase"], "PLANNING")
            self.assertIsNone(second["planning_fingerprint"])

    def test_new_execplan_does_not_save_completed_input_fingerprint(self):
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
            self.assertIsNone(row["planning_fingerprint"])

    def test_execplan_frontmatter_parser(self):
        values = loop_manager.parse_frontmatter(
            """---
id: G-002
title: Small Task
sources:
  - docs/PRODUCT.md
depends_on:
  - G-001
complexity: low
risk: medium
---
"""
        )

        self.assertEqual(values["id"], "G-002")
        self.assertEqual(values["sources"], ["docs/PRODUCT.md"])
        self.assertEqual(values["depends_on"], ["G-001"])

    def test_current_execplan_is_valid(self):
        values = loop_manager.validate_execplan(
            loop_manager.ROOT,
            "docs/plans/[G-001] Minimal Bootstrap Runner.md",
        )

        self.assertEqual(values["id"], "G-001")

    def test_execplan_catalog_is_deterministic_and_indexed_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            requirement = root / "docs/requirements/[R-001] Active.md"
            requirement.parent.mkdir()
            requirement.write_text("# Requirement\n", encoding="utf-8")
            write_execplan(root, "G-003", "Third", depends_on=("G-002",))
            write_execplan(root, "G-001", "First")
            write_execplan(
                root,
                "G-002",
                "Second",
                sources=("docs/requirements/[R-001] Active.md",),
                depends_on=("G-001",),
            )
            nested = root / "docs/plans/archive/[G-999] Ignored.md"
            nested.parent.mkdir()
            nested.write_text("not an active plan", encoding="utf-8")

            catalog = loop_manager.load_execplan_catalog(root)

        self.assertEqual(list(catalog), ["G-001", "G-002", "G-003"])
        self.assertEqual(catalog["G-002"]["title"], "Second")

    def test_execplan_catalog_rejects_duplicate_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "First")
            write_execplan(root, "G-001", "Duplicate")

            with self.assertRaisesRegex(RuntimeError, "중복 ExecPlan id.*G-001"):
                loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_restricts_sources_to_active_product_inputs(self):
        invalid_sources = (
            (),
            ("/docs/PRODUCT.md",),
            ("../PRODUCT.md",),
            ("docs/requirements/archive/Old.md",),
            ("README.md",),
            ("docs/requirements/Missing.md",),
        )
        for sources in invalid_sources:
            with self.subTest(sources=sources), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                (root / "README.md").write_text("# Readme\n", encoding="utf-8")
                archived = root / "docs/requirements/archive/Old.md"
                archived.parent.mkdir(parents=True)
                archived.write_text("# Old\n", encoding="utf-8")
                write_execplan(root, "G-001", "Invalid Source", sources=sources)

                with self.assertRaisesRegex(RuntimeError, "ExecPlan source"):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_validates_fixed_metadata(self):
        cases = (
            ("id", "BAD", "Plan", "low", "low", "id는 G-000"),
            ("filename", "G-001", "Plan", "low", "low", "파일명"),
            ("complexity", "G-001", "Plan", "tiny", "low", "complexity"),
            ("risk", "G-001", "Plan", "low", "extreme", "risk"),
            ("keys", "G-001", "Plan", "low", "low", "frontmatter key"),
        )
        for name, plan_id, title, complexity, risk, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                plan = write_execplan(
                    root, plan_id, title, complexity=complexity, risk=risk
                )
                if name == "filename":
                    plan.rename(plan.with_name("[G-001] Wrong.md"))
                elif name == "keys":
                    contents = plan.read_text(encoding="utf-8")
                    plan.write_text(
                        contents.replace("risk: low\n", "risk: low\nstatus: READY\n"),
                        encoding="utf-8",
                    )

                with self.assertRaisesRegex(RuntimeError, message):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_invalid_and_missing_dependencies(self):
        cases = (("G-1", "depends_on ID 형식"), ("G-999", "존재하지 않는 depends_on"))
        for dependency, message in cases:
            with self.subTest(dependency=dependency), tempfile.TemporaryDirectory() as directory:
                root = make_catalog_root(directory)
                write_execplan(root, "G-001", "Plan", depends_on=(dependency,))

                with self.assertRaisesRegex(RuntimeError, message):
                    loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_self_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "Plan", depends_on=("G-001",))

            with self.assertRaisesRegex(RuntimeError, r"G-001 -> G-001"):
                loop_manager.load_execplan_catalog(root)

    def test_execplan_catalog_rejects_multistep_cycle_with_all_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            write_execplan(root, "G-001", "First", depends_on=("G-003",))
            write_execplan(root, "G-002", "Second", depends_on=("G-001",))
            write_execplan(root, "G-003", "Third", depends_on=("G-002",))

            with self.assertRaises(RuntimeError) as raised:
                loop_manager.load_execplan_catalog(root)

        message = str(raised.exception)
        self.assertIn("순환 ExecPlan 의존성", message)
        for plan_id in ("G-001", "G-002", "G-003"):
            self.assertIn(plan_id, message)

    def test_selected_execplan_requires_valid_whole_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_catalog_root(directory)
            selected = write_execplan(root, "G-001", "Selected")
            write_execplan(root, "G-002", "Broken", depends_on=("G-999",))
            relative_path = selected.relative_to(root).as_posix()

            with self.assertRaisesRegex(RuntimeError, "G-002.*G-999"):
                loop_manager.validate_execplan(root, relative_path)

    def test_role_resolves_model_alias(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        with mock.patch(
            "loop_manager.codex_model_efforts",
            return_value={"gpt-5.6-sol": frozenset({"xhigh"})},
        ):
            self.assertEqual(loop_manager.role(config, "planner"), ("gpt-5.6-sol", "xhigh"))

    def test_worker_level_boundaries_and_cap(self):
        cases = [
            ("low", "low", 1),
            ("low", "medium", 2),
            ("low", "high", 3),
            ("low", "critical", 4),
            ("medium", "low", 3),
            ("medium", "medium", 4),
            ("medium", "high", 5),
            ("medium", "critical", 6),
            ("high", "low", 5),
            ("high", "medium", 6),
            ("high", "high", 7),
            ("high", "critical", 7),
        ]

        for complexity, risk, expected in cases:
            with self.subTest(complexity=complexity, risk=risk):
                plan = {"complexity": complexity, "risk": risk}
                self.assertEqual(loop_manager.worker_level(plan), expected)

    def test_worker_profile_resolves_level_alias(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        model_efforts = {
            "gpt-5.6-luna": frozenset({"medium"}),
            "gpt-5.6-terra": frozenset({"high"}),
            "gpt-5.6-sol": frozenset({"xhigh"}),
        }
        with mock.patch("loop_manager.codex_model_efforts", return_value=model_efforts):
            self.assertEqual(loop_manager.worker_profile(config, 1), ("gpt-5.6-luna", "medium"))
            self.assertEqual(loop_manager.worker_profile(config, 4), ("gpt-5.6-terra", "high"))
            self.assertEqual(loop_manager.worker_profile(config, 7), ("gpt-5.6-sol", "xhigh"))

    def test_worker_profile_rejects_invalid_level(self):
        config = loop_manager.ROOT / "config/config.ini.example"

        for level in (0, 8, True):
            with self.subTest(level=level):
                with self.assertRaisesRegex(RuntimeError, "Worker level"):
                    loop_manager.worker_profile(config, level)

    def test_worker_profile_rejects_invalid_configuration(self):
        cases = [
            (
                "missing-level",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel2 = fast,medium\n",
                "level1 설정이 없습니다",
            ),
            (
                "empty-level",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 =\n",
                "Model alias/Effort 설정이 비어",
            ),
            (
                "malformed-profile",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast\n",
                "Model alias,Effort",
            ),
            (
                "empty-alias",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = ,medium\n",
                "Model alias 설정이 비어",
            ),
            (
                "empty-effort",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,\n",
                "Effort 설정이 비어",
            ),
            (
                "unsupported-effort",
                "[codex.models]\nfast = gpt-5.6-sol\n[codex.worker]\nlevel1 = fast,minimal\n",
                "Model/Effort 조합을 Codex CLI가 지원하지 않습니다: gpt-5.6-sol,minimal",
            ),
            (
                "unsupported-model",
                "[codex.models]\nfast = unavailable\n[codex.worker]\nlevel1 = fast,medium\n",
                "Model을 Codex CLI가 지원하지 않습니다: unavailable",
            ),
            (
                "missing-alias",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = strong,high\n",
                "Model alias가 .* 없습니다",
            ),
            (
                "empty-model",
                "[codex.models]\nfast =\n[codex.worker]\nlevel1 = fast,medium\n",
                "Model 설정이 비어",
            ),
            (
                "malformed-ini",
                "codex.models]\nfast = model-fast\n",
                "설정 파일 형식이 올바르지 않습니다",
            ),
            (
                "malformed-profile-interpolation",
                "[codex.models]\nfast = model-fast\n[codex.worker]\nlevel1 = fast,%\n",
                r"설정 값 형식이 올바르지 않습니다: \[codex.worker\] level1",
            ),
            (
                "malformed-model-interpolation",
                "[codex.models]\nfast = model-%\n[codex.worker]\nlevel1 = fast,medium\n",
                r"설정 값 형식이 올바르지 않습니다: \[codex.models\] fast",
            ),
        ]

        with tempfile.TemporaryDirectory() as directory:
            for name, contents, message in cases:
                with self.subTest(name=name):
                    config = Path(directory) / f"{name}.ini"
                    config.write_text(contents, encoding="utf-8")
                    with mock.patch(
                        "loop_manager.codex_model_efforts",
                        return_value={
                            "gpt-5.6-sol": frozenset(
                                {"low", "medium", "high", "xhigh", "max", "ultra"}
                            )
                        },
                    ):
                        with self.assertRaisesRegex(RuntimeError, message):
                            loop_manager.worker_profile(config, 1)

    def test_codex_model_efforts_reads_effective_catalog_for_worktree(self):
        catalog = {
            "models": [
                {
                    "slug": "gpt-reserve",
                    "supported_reasoning_levels": [
                        {"effort": "max"}, {"effort": "ultra"}
                    ],
                }
            ]
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(catalog))
        worktree = Path("/project/worktree")
        with mock.patch("loop_manager.command", return_value=completed) as command:
            efforts = loop_manager.codex_model_efforts(worktree)

        self.assertEqual(efforts, {"gpt-reserve": frozenset({"max", "ultra"})})
        self.assertEqual(command.call_args.args[0], ["codex", "debug", "models"])
        self.assertEqual(command.call_args.kwargs["cwd"], worktree)

    def test_run_codex_validates_profile_in_worker_catalog(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-terra": frozenset({"high"})},
            ) as model_efforts, mock.patch(
                "loop_manager.subprocess.run", return_value=completed
            ):
                loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "prompt",
                    read_only=False,
                    level=4,
                )

        model_efforts.assert_called_once_with(worktree)

    def test_profiles_validate_model_specific_efforts(self):
        model_efforts = {
            "gpt-5.6-sol": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"}),
            "gpt-5.6-luna": frozenset({"low", "medium", "high", "xhigh", "max"}),
        }
        contents = (
            "[codex.models]\n"
            "strong = gpt-5.6-sol\n"
            "fast = gpt-5.6-luna\n"
            "[codex.roles]\n"
            "planner = strong,max\n"
            "reviewer = strong,ultra\n"
            "[codex.worker]\n"
            "level1 = fast,max\n"
            "level2 = strong,minimal\n"
            "level3 = fast,ultra\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.ini"
            config.write_text(contents, encoding="utf-8")
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value=model_efforts,
            ):
                self.assertEqual(loop_manager.role(config, "planner"), ("gpt-5.6-sol", "max"))
                self.assertEqual(loop_manager.role(config, "reviewer"), ("gpt-5.6-sol", "ultra"))
                self.assertEqual(loop_manager.worker_profile(config, 1), ("gpt-5.6-luna", "max"))
                for level in (2, 3):
                    with self.subTest(level=level):
                        with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                            loop_manager.worker_profile(config, level)

    def test_worker_codex_uses_level_model_and_effort(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            completed = subprocess.CompletedProcess([], 0)
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-terra": frozenset({"high"})},
            ), mock.patch("loop_manager.subprocess.run", return_value=completed) as run:
                code, message = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "worker",
                    "prompt",
                    read_only=False,
                    level=4,
                )

        self.assertEqual((code, message), (0, ""))
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[arguments.index("-m") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="high"', arguments)

    def test_planner_codex_persists_thread_id_and_resumes_exact_session(self):
        config = loop_manager.ROOT / "config/config.ini.example"
        calls = []

        thread_event = (
            json.dumps(
                {
                    "type": "thread.started",
                    "thread_id": "0199a213-81c0-7800-8aa1-bbab2a035a53",
                }
            )
            + "\n"
        )

        class StartedOutput:
            def __iter__(self):
                return iter((thread_event,))

            def close(self):
                pass

        class StartedProcess:
            def __init__(self, arguments, **_kwargs):
                calls.append(arguments)
                self.stdin = mock.Mock()
                self.stdout = StartedOutput()

            def wait(self):
                return 0

            def kill(self):
                raise AssertionError("completed Planner process must not be killed")

        def codex_run(arguments, **_kwargs):
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0)

        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            state_dir = Path(directory) / "state"
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-sol": frozenset({"xhigh"})},
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=StartedProcess
            ), mock.patch("loop_manager.subprocess.run", side_effect=codex_run):
                started = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "planner",
                    "plan",
                    read_only=False,
                    persist_session=True,
                )
                resumed = loop_manager.run_codex(
                    worktree,
                    state_dir,
                    config,
                    "planner",
                    "repair",
                    read_only=False,
                    persist_session=True,
                    session_id=started.session_id,
                )

        self.assertEqual(started.code, 0)
        self.assertEqual(
            started.session_id,
            "0199a213-81c0-7800-8aa1-bbab2a035a53",
        )
        self.assertEqual(resumed.session_id, started.session_id)
        self.assertEqual(calls[0][:3], ["codex", "exec", "--json"])
        self.assertNotIn("--ephemeral", calls[0])
        self.assertEqual(calls[1][:4], ["codex", "exec", "resume", "--json"])
        self.assertIn(started.session_id, calls[1])

    def test_interrupted_planner_persists_started_session_and_resumes(self):
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        thread_event = json.dumps(
            {"type": "thread.started", "thread_id": session_id}
        ) + "\n"

        class InterruptedOutput:
            def __iter__(self):
                yield thread_event
                raise RuntimeError("Planner process interrupted")

            def close(self):
                pass

        class InterruptedProcess:
            def __init__(self):
                self.stdin = mock.Mock()
                self.stdout = InterruptedOutput()
                self.killed = False

            def wait(self):
                return -9

            def kill(self):
                self.killed = True

        process = InterruptedProcess()
        real_popen = subprocess.Popen

        def start_process(arguments, *args, **kwargs):
            if arguments[0] == "codex":
                return process
            return real_popen(arguments, *args, **kwargs)

        resumed_sessions = []

        def stopped_resume(_worktree, _state, _config, role_name, _prompt, **kwargs):
            self.assertEqual(role_name, "planner")
            resumed_sessions.append(kwargs["session_id"])
            return loop_manager.CodexResult(
                1, "stop after persisted session check", kwargs["session_id"]
            )

        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            config = loop_manager.ROOT / "config/config.ini.example"
            connection = loop_manager.connect_database(state_dir, create=True)

            with mock.patch(
                "loop_manager.validate_repository", return_value=repo
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.ensure_safe_base_worktree"
            ), mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={"gpt-5.6-sol": frozenset({"xhigh"})},
            ), mock.patch(
                "loop_manager.subprocess.Popen", side_effect=start_process
            ):
                with self.assertRaisesRegex(RuntimeError, "Planner process interrupted"):
                    loop_manager.run_project(
                        connection,
                        state_dir,
                        str(repo),
                        config,
                        "main",
                        False,
                    )
            connection.close()

            with loop_manager.connect_database(state_dir) as connection:
                interrupted = connection.execute(
                    "SELECT * FROM projects WHERE repo = ?", (str(repo),)
                ).fetchone()
                self.assertEqual(interrupted["phase"], "PLANNING")
                self.assertEqual(interrupted["planner_session_id"], session_id)
                self.assertTrue(Path(interrupted["worktree"]).is_dir())
                self.assertTrue(process.killed)

                with mock.patch(
                    "loop_manager.validate_repository", return_value=repo
                ), mock.patch(
                    "loop_manager.remote_base", return_value="main"
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree"
                ), mock.patch(
                    "loop_manager.run_codex", side_effect=stopped_resume
                ):
                    self.assertEqual(
                        loop_manager.run_project(
                            connection,
                            state_dir,
                            str(repo),
                            config,
                            "main",
                            False,
                        ),
                        1,
                    )

        self.assertEqual(resumed_sessions, [session_id])

    def test_unsupported_worker_effort_fails_before_codex_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[codex.models]\nstrong = gpt-5.6-sol\n[codex.worker]\nlevel1 = strong,minimal\n",
                encoding="utf-8",
            )
            with mock.patch(
                "loop_manager.codex_model_efforts",
                return_value={
                    "gpt-5.6-sol": frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})
                },
            ), mock.patch("loop_manager.subprocess.run") as run:
                with self.assertRaisesRegex(RuntimeError, "Model/Effort 조합"):
                    loop_manager.run_codex(
                        root / "worktree",
                        root / "state",
                        config,
                        "worker",
                        "prompt",
                        read_only=False,
                        level=1,
                    )
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
