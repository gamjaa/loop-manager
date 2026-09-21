import io
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import loop_manager
from managed_config import ManagedConfigError, load_managed_config
import project_registry


class ManagedConfigurationAndProjectRegistryTest(unittest.TestCase):
    def _git(self, root: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def _commit(self, root: Path, message: str) -> None:
        self._git(root, "add", "-A")
        self._git(root, "commit", "-m", message)

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        return connection

    def _project_files(self, root: Path, *, common_agents: bool = True) -> None:
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.email", "registry@example.com")
        self._git(root, "config", "user.name", "Registry Test")
        (root / "docs/requirements").mkdir(parents=True)
        agents = (
            (loop_manager.ROOT / "AGENTS.md").read_text(encoding="utf-8")
            if common_agents
            else "# Project-only agent rules\n"
        )
        (root / "AGENTS.md").write_text(agents, encoding="utf-8")
        (root / "docs/PLANS.md").write_bytes(
            (loop_manager.ROOT / "docs/PLANS.md").read_bytes()
        )
        (root / "docs/ARCHITECTURE.md").write_text(
            "# Project Architecture\n\nImplemented design.\n", encoding="utf-8"
        )
        (root / "docs/PRODUCT.md").write_text(
            "# Project Product\n\nActive behavior.\n", encoding="utf-8"
        )
        self._commit(root, "Registration files")

    def _record(
        self,
        repo: str = "/repo",
        *,
        approved: bool = False,
    ) -> project_registry.RegisteredProject:
        return project_registry.RegisteredProject(
            repo=repo,
            base_branch="main",
            provider="codex",
            agents_hash="agents-sha",
            agents_approved=approved,
        )

    def test_global_config_validates_worker_limit_and_paired_telegram_without_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.ini"
            valid.write_text("[telegram]\ntoken =\nchat_id =\n", encoding="utf-8")
            self.assertEqual(load_managed_config(valid).max_workers, 3)

            invalid_workers = root / "workers.ini"
            invalid_workers.write_text(
                "[loop]\nmax_workers = 0\n[telegram]\ntoken =\nchat_id =\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManagedConfigError, "양의 정수"):
                load_managed_config(invalid_workers)

            secret = "telegram-super-secret"
            partial = root / "partial.ini"
            partial.write_text(
                f"[telegram]\ntoken = {secret}\nchat_id =\n",
                encoding="utf-8",
            )
            with self.assertRaises(ManagedConfigError) as raised:
                load_managed_config(partial)
            self.assertNotIn(secret, str(raised.exception))

    def test_refactoring_review_interval_defaults_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default_config = root / "default.ini"
            default_config.write_text("[loop]\nmax_workers = 2\n", encoding="utf-8")
            self.assertEqual(
                load_managed_config(default_config).refactoring_review_interval,
                5,
            )

            configured = root / "configured.ini"
            configured.write_text(
                "[loop]\nrefactoring_review_interval = 9\n", encoding="utf-8"
            )
            self.assertEqual(
                load_managed_config(configured).refactoring_review_interval,
                9,
            )

            for value in ("0", "-1", "1.5"):
                with self.subTest(value=value):
                    invalid = root / f"invalid-{value}.ini"
                    invalid.write_text(
                        "[loop]\n"
                        f"refactoring_review_interval = {value}\n"
                        "[telegram]\n"
                        "token = do-not-leak\n"
                        "chat_id = paired\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ManagedConfigError) as raised:
                        load_managed_config(invalid)
                    self.assertIn("refactoring_review_interval", str(raised.exception))
                    self.assertNotIn("do-not-leak", str(raised.exception))

    def test_invalid_refactoring_interval_stops_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            invalid = root / "invalid.ini"
            invalid.write_text(
                "[loop]\nrefactoring_review_interval = nope\n", encoding="utf-8"
            )
            with mock.patch.object(
                loop_manager.sys,
                "argv",
                [
                    "loop_manager.py",
                    "--runtime-child",
                    "--runtime-source",
                    str(loop_manager.ROOT),
                    "--runtime-sha",
                    "a" * 40,
                    "--state-dir",
                    str(state_dir),
                    "--config",
                    str(invalid),
                    "run",
                ],
            ), mock.patch("loop_manager.validate_runtime_child"), mock.patch(
                "builtins.print"
            ):
                self.assertEqual(loop_manager.main(), 1)
            self.assertFalse((state_dir / "loop-manager.db").exists())

    def test_invalid_refactoring_interval_stops_top_level_init_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            invalid = root / "invalid.ini"
            invalid.write_text(
                "[loop]\nrefactoring_review_interval = 0\n", encoding="utf-8"
            )
            args = loop_manager.build_parser().parse_args(["init"])

            with self.assertRaises(ManagedConfigError):
                loop_manager.execute_runtime_command(args, state_dir, invalid)

            self.assertFalse((state_dir / "loop-manager.db").exists())

    def test_invalid_refactoring_interval_prevents_registry_state_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            invalid = root / "invalid.ini"
            invalid.write_text(
                "[loop]\nrefactoring_review_interval = 0\n", encoding="utf-8"
            )
            with loop_manager.connect_database(state_dir, create=True) as connection:
                project_registry.add_project(connection, self._record())
            args = loop_manager.build_parser().parse_args(
                ["config", "remove", "/repo"]
            )

            with self.assertRaises(ManagedConfigError):
                loop_manager.execute_runtime_command(
                    args, state_dir, invalid
                )

            with loop_manager.connect_database(state_dir) as connection:
                self.assertEqual(
                    tuple(project.repo for project in project_registry.list_projects(connection)),
                    ("/repo",),
                )

    def test_missing_config_stops_run_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            missing = root / "missing.ini"
            with mock.patch.object(
                loop_manager.sys,
                "argv",
                [
                    "loop_manager.py",
                    "--runtime-child",
                    "--runtime-source",
                    str(loop_manager.ROOT),
                    "--runtime-sha",
                    "a" * 40,
                    "--state-dir",
                    str(state_dir),
                    "--config",
                    str(missing),
                    "run",
                ],
            ), mock.patch("loop_manager.validate_runtime_child"), mock.patch("builtins.print"):
                self.assertEqual(loop_manager.main(), 1)
            self.assertFalse((state_dir / "loop-manager.db").exists())

    def test_registration_files_require_active_documents_and_hash_bound_agents_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project_files(root, common_agents=False)

            with self.assertRaisesRegex(RuntimeError, "승인"):
                loop_manager.inspect_registration_files(
                    root, "HEAD", approve_agents_mismatch=False
                )
            agents_hash, approved = loop_manager.inspect_registration_files(
                root, "HEAD", approve_agents_mismatch=True
            )
            self.assertTrue(approved)
            self.assertEqual(agents_hash, loop_manager.file_hash(root / "AGENTS.md"))

            (root / "docs/PLANS.md").write_text("different\n", encoding="utf-8")
            self._commit(root, "Change plans")
            with self.assertRaisesRegex(RuntimeError, "PLANS.md"):
                loop_manager.inspect_registration_files(
                    root, "HEAD", approve_agents_mismatch=True
                )

    def test_registration_rejects_unchanged_product_and_architecture_templates(self):
        for filename, expected in (
            ("PRODUCT", "PRODUCT.md"),
            ("ARCHITECTURE", "ARCHITECTURE.md"),
        ):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._project_files(root)
                (root / f"docs/{filename}.md").write_bytes(
                    (loop_manager.ROOT / f"docs/{filename}.template.md").read_bytes()
                )
                self._commit(root, f"Use {filename} template")
                with self.assertRaisesRegex(RuntimeError, expected):
                    loop_manager.inspect_registration_files(
                        root, "HEAD", approve_agents_mismatch=False
                    )

    def test_add_list_and_remove_preserve_merged_audit_history(self):
        connection = self._connection()
        inspection = loop_manager.ProjectRegistrationInspection(
            root=Path("/repo"),
            base_branch="stable",
            provider="claude",
            agents_hash="approved-hash",
            agents_approved=True,
        )
        with mock.patch(
            "loop_manager.inspect_project_registration", return_value=inspection
        ):
            added = loop_manager.add_registered_project(
                connection,
                Path("/repo"),
                config_path=Path("config.ini"),
                provider="claude",
                requested_base="stable",
                approve_agents_mismatch=True,
            )
        self.assertTrue(added.agents_approved)
        self.assertEqual(project_registry.list_projects(connection), (added,))

        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status
            ) VALUES ('/repo', 'G-001', 'docs/plans/G-001.md', 'Done',
                      '[]', '[]', 'normal', 'hash', 'MERGED')
            """
        )
        connection.commit()
        project_registry.remove_project(connection, "/repo")

        self.assertEqual(project_registry.list_projects(connection), ())
        self.assertEqual(
            connection.execute(
                "SELECT status FROM exec_plans WHERE repo = '/repo'"
            ).fetchone()["status"],
            "MERGED",
        )

    def test_remove_refuses_active_execplans_and_planning_errors(self):
        connection = self._connection()
        project_registry.add_project(connection, self._record())
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status
            ) VALUES ('/repo', 'G-016', 'docs/plans/G-016.md', 'Active',
                      '[]', '[]', 'high', 'hash', 'READY')
            """
        )
        connection.commit()
        with self.assertRaisesRegex(project_registry.ProjectRegistryError, "READY"):
            project_registry.remove_project(connection, "/repo")

        connection.execute(
            "UPDATE exec_plans SET status = 'MERGED' WHERE repo = '/repo'"
        )
        connection.execute(
            "UPDATE projects SET planning_error = 'invalid plan' WHERE repo = '/repo'"
        )
        connection.commit()
        with self.assertRaisesRegex(
            project_registry.ProjectRegistryError, "planning_error"
        ):
            project_registry.remove_project(connection, "/repo")

    def test_provider_auth_failure_does_not_create_registry_row(self):
        connection = self._connection()
        root = Path("/repo").resolve()

        def git_result(_repo, *arguments, **_kwargs):
            if arguments[:2] == ("rev-parse", "--show-toplevel"):
                output = str(root)
            elif arguments[:3] == ("remote", "get-url", "origin"):
                output = "https://github.com/example/project.git"
            else:
                raise AssertionError(arguments)
            return subprocess.CompletedProcess(arguments, 0, stdout=output)

        with mock.patch("loop_manager.shutil.which", return_value="/bin/tool"), mock.patch(
            "loop_manager.git", side_effect=git_result
        ), mock.patch(
            "loop_manager.inspect_registration_files",
            return_value=("agents-hash", False),
        ), mock.patch(
            "loop_manager.remote_base", return_value="main"
        ), mock.patch(
            "loop_manager.validate_provider_runtime",
            side_effect=RuntimeError("Codex CLI가 인증되어 있지 않습니다."),
        ):
            with self.assertRaisesRegex(RuntimeError, "인증"):
                loop_manager.add_registered_project(
                    connection,
                    root,
                    config_path=Path("config.ini"),
                    provider="codex",
                    requested_base=None,
                    approve_agents_mismatch=False,
                )

        self.assertEqual(project_registry.list_projects(connection), ())

    def test_registered_runtime_preflight_fails_before_project_state_transition(self):
        connection = self._connection()
        root = Path("/repo").resolve()
        project_registry.add_project(connection, self._record(repo=str(root)))

        with mock.patch(
            "loop_manager.shutil.which", return_value="/bin/tool"
        ), mock.patch(
            "loop_manager.validate_provider_runtime",
            side_effect=RuntimeError("Codex CLI가 인증되어 있지 않습니다."),
        ) as provider_runtime, mock.patch(
            "loop_manager.inspect_registration_files"
        ) as registration_files, mock.patch(
            "loop_manager.git"
        ) as git:
            with self.assertRaisesRegex(RuntimeError, "인증"):
                loop_manager.run_project(
                    connection,
                    Path("/state"),
                    str(root),
                    Path("config.ini"),
                    None,
                    False,
                    registered_only=True,
                )

        project = connection.execute(
            "SELECT phase, worktree FROM projects WHERE repo = ?", (str(root),)
        ).fetchone()
        plan_count = connection.execute(
            "SELECT COUNT(*) FROM exec_plans WHERE repo = ?", (str(root),)
        ).fetchone()[0]
        self.assertEqual(tuple(project), (None, None))
        self.assertEqual(plan_count, 0)
        provider_runtime.assert_called_once_with(
            Path("config.ini"), "codex", root
        )
        registration_files.assert_not_called()
        git.assert_not_called()

    def test_legacy_registry_migration_preserves_codex_project(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            database = sqlite3.connect(state_dir / "loop-manager.db")
            database.execute(
                "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT NOT NULL, phase TEXT)"
            )
            database.execute(
                "INSERT INTO projects (repo, base_branch) VALUES ('/legacy', 'main')"
            )
            database.commit()
            database.close()

            with loop_manager.connect_database(state_dir) as connection:
                projects = project_registry.list_projects(connection)

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].provider, "codex")
        self.assertEqual(projects[0].repo, "/legacy")

    def test_run_parser_has_no_implicit_registration_arguments(self):
        args = loop_manager.build_parser().parse_args(["run"])
        self.assertFalse(hasattr(args, "repository"))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            loop_manager.build_parser().parse_args(["run", "/repo"])

        state_dir = Path("/state")
        with mock.patch.object(
            loop_manager.sys,
            "argv",
            ["loop_manager.py", "run", "/repo", "--allow-agents-mismatch"],
        ), mock.patch(
            "loop_manager.state_directory", return_value=state_dir
        ) as state_directory, mock.patch(
            "loop_manager.process_lock"
        ) as process_lock, redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as raised:
                loop_manager.main()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments", stderr.getvalue())
        state_directory.assert_not_called()
        process_lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
