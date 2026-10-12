from tests.support.git import isolated_git_test
from contextlib import closing
import io
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.config.managed_config import ManagedConfig, ManagedConfigError, load_managed_config
from loop_manager_internal.runtime import project_registry
from tests.support.fixed_samples import PROJECT_FILES


@isolated_git_test
class ManagedConfigurationAndProjectRegistryTest(unittest.TestCase):
    def test_partial_verify_defaults_enabled_and_config_can_disable_it(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.ini'
            self.assertTrue(ManagedConfig(config, 3, '', '').verify_partial)
            for contents in ('[loop]\n', '[telegram]\n'):
                with self.subTest(contents=contents):
                    config.write_text(contents)
                    self.assertTrue(load_managed_config(config).verify_partial)
            for value, expected in (('true', True), ('false', False), (' OFF ', False)):
                with self.subTest(value=value):
                    config.write_text(f'[loop]\nverify_partial={value}\n')
                    self.assertEqual(load_managed_config(config).verify_partial, expected)
            for value in ('maybe', ''):
                with self.subTest(value=value):
                    config.write_text(f'[loop]\nverify_partial={value}\n')
                    with self.assertRaisesRegex(ManagedConfigError, 'verify_partial'):
                        load_managed_config(config)

    def test_copy_project_files_runs_real_script_with_fixed_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            shutil.copytree(PROJECT_FILES, source)
            tool_relative = '.agents/skills/specification-docs/scripts/next_requirement_id.py'
            tool_source = source / tool_relative
            tool_source.parent.mkdir(parents=True)
            shutil.copy(loop_manager.ROOT / tool_relative, tool_source)
            parser_relative = 'loop_manager_internal/catalog/frontmatter.py'
            parser_source = source / parser_relative
            parser_source.parent.mkdir(parents=True)
            shutil.copy(loop_manager.ROOT / parser_relative, parser_source)
            script_name = 'copy-project-files.ps1' if os.name == 'nt' else 'copy-project-files.sh'
            script = source / 'scripts' / script_name
            script.parent.mkdir()
            shutil.copy(loop_manager.ROOT / 'scripts' / script_name, script)
            target = root / 'project'
            target.mkdir()
            original = b'custom project rules\r\n\xff\n'
            (target / 'AGENTS.md').write_bytes(original)
            def copy_command(destination):
                return (['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                         str(script), '-TargetPath', str(destination)]
                        if os.name == 'nt' else ['bash', str(script), str(destination)])

            command = copy_command(target)
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual((target / 'AGENTS.md').read_bytes(), original)
            copied_files = (
                '.agents/skills/specification-docs/SKILL.md',
                'docs/PLANS.md',
                'docs/FUTURE_IDEAS.md',
                'docs/PRODUCT.template.md',
                'docs/ARCHITECTURE.template.md',
                'docs/REQUIREMENT.template.md',
                'docs/PRODUCT.md',
                'docs/ARCHITECTURE.md',
            )
            for relative in copied_files:
                source_relative = (
                    'docs/PRODUCT.template.md' if relative == 'docs/PRODUCT.md'
                    else 'docs/ARCHITECTURE.template.md' if relative == 'docs/ARCHITECTURE.md'
                    else relative
                )
                with self.subTest(path=relative):
                    self.assertEqual((target / relative).read_bytes(),
                                     (PROJECT_FILES / source_relative).read_bytes())
            self.assertTrue((target / 'docs/plans').is_dir())
            self.assertTrue((target / 'docs/requirements/archive').is_dir())
            self.assertEqual((target / tool_relative).read_bytes(), tool_source.read_bytes())
            copied_parser = target / '.agents/skills/specification-docs/scripts/frontmatter.py'
            self.assertEqual(copied_parser.read_bytes(), parser_source.read_bytes())
            query = subprocess.run([sys.executable, tool_relative], cwd=target,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual((query.returncode, query.stdout, query.stderr), (0, 'R-001\n', ''))
            protected_files = (*copied_files, tool_relative,
                               '.agents/skills/specification-docs/scripts/frontmatter.py')
            before = {relative: (target / relative).read_bytes() for relative in protected_files}

            empty_target = root / 'empty-project'
            empty_target.mkdir()
            subprocess.run(copy_command(empty_target), check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertFalse((empty_target / 'AGENTS.md').exists())

            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((target / 'AGENTS.md').read_bytes(), original)
            self.assertEqual(before, {relative: (target / relative).read_bytes()
                                      for relative in protected_files})

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
            (PROJECT_FILES / "AGENTS.md").read_text(encoding="utf-8")
            if common_agents
            else "# Project-only agent rules\n"
        )
        (root / "AGENTS.md").write_text(agents, encoding="utf-8")
        (root / "docs/PLANS.md").write_bytes(
            (PROJECT_FILES / "docs/PLANS.md").read_bytes()
        )
        (root / "docs/ARCHITECTURE.md").write_text(
            "# Project Architecture\n\nImplemented design.\n", encoding="utf-8"
        )
        (root / "docs/PRODUCT.md").write_text(
            "# Project Product\n\nActive behavior.\n", encoding="utf-8"
        )
        self._commit(root, "Registration files")

    def _inspect_registration_files(self, *args, **kwargs):
        with mock.patch.object(loop_manager, "ROOT", PROJECT_FILES):
            return loop_manager.inspect_registration_files(*args, **kwargs)

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
                20,
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

    def test_verify_timeout_defaults_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            path.write_text("[loop]\n", encoding="utf-8")
            self.assertEqual(load_managed_config(path).verify_timeout_seconds, 600)
            path.write_text("[loop]\nverify_timeout_seconds = 42\n", encoding="utf-8")
            self.assertEqual(load_managed_config(path).verify_timeout_seconds, 42)
            for value in ("0", "-1", "1.5", "abc", "1_0"):
                with self.subTest(value=value):
                    path.write_text(
                        f"[loop]\nverify_timeout_seconds = {value}\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ManagedConfigError, "verify_timeout_seconds"):
                        load_managed_config(path)

    def test_invalid_verify_timeout_stops_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.ini"
            config.write_text(
                "[loop]\nverify_timeout_seconds = 0\n", encoding="utf-8"
            )
            with self.assertRaises(ManagedConfigError):
                loop_manager.execute_runtime_command(
                    loop_manager.build_parser().parse_args(["init"]),
                    root / "state", config,
                )
            self.assertFalse((root / "state/loop-manager.db").exists())

    def test_scheduler_passes_configured_verify_timeout_to_project_worker(self):
        config = ManagedConfig(
            Path('/config.ini'), 1, '', '', verify_timeout_seconds=42, verify_parallelism=2,
        )
        mutation = mock.sentinel.runtime_mutation
        result = loop_manager.ScheduledProjectResult(
            '/repo', loop_manager.ProjectRunResult(loop_manager.ProjectRunKind.WAITING, 0),
        )
        with mock.patch.object(
            project_registry, 'list_projects', return_value=(self._record('/repo'),)
        ), mock.patch.object(
            loop_manager, '_run_registered_project', return_value=result
        ) as worker, mock.patch.object(
            loop_manager, '_preflight_self_merge_handoff'
        ) as preflight, mock.patch(
            'loop_manager_internal.application.scheduler.VerifyCoordinator'
        ):
            self.assertEqual(
                loop_manager.schedule_registered_projects(
                    mock.Mock(), Path('/state'), config, runtime_mutation=mutation,
                ),
                (result,),
            )
        self.assertIs(preflight.call_args.args[3], mutation)
        self.assertEqual(preflight.call_args.kwargs['config_path'], config.path)
        self.assertEqual(worker.call_args.args[-1], 42)
        self.assertEqual(worker.call_args.kwargs['verify_parallelism'], 2)
        self.assertIsNotNone(worker.call_args.kwargs['verify_coordinator'])

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
                "loop_manager.validate_runtime_mutation", return_value=None,
            ), mock.patch(
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
            with closing(loop_manager.connect_database(state_dir, create=True)) as connection, connection:
                project_registry.add_project(connection, self._record())
            args = loop_manager.build_parser().parse_args(
                ["config", "remove", "/repo"]
            )

            with self.assertRaises(ManagedConfigError):
                loop_manager.execute_runtime_command(
                    args, state_dir, invalid
                )

            with closing(loop_manager.connect_database(state_dir)) as connection, connection:
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

    def test_registration_files_require_active_documents_and_accept_project_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._project_files(root, common_agents=False)

            _, approved_without_flag = self._inspect_registration_files(
                root, "HEAD", approve_agents_mismatch=False
            )
            self.assertFalse(approved_without_flag)
            agents_hash, approved = self._inspect_registration_files(
                root, "HEAD", approve_agents_mismatch=True
            )
            self.assertFalse(approved)
            self.assertIsNone(agents_hash)

            (root / "docs/PLANS.md").write_text("different\n", encoding="utf-8")
            self._commit(root, "Change plans")
            with self.assertRaisesRegex(RuntimeError, "PLANS.md"):
                self._inspect_registration_files(
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
                    (PROJECT_FILES / f"docs/{filename}.template.md").read_bytes()
                )
                self._commit(root, f"Use {filename} template")
                with self.assertRaisesRegex(RuntimeError, expected):
                    self._inspect_registration_files(
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
        project_registry.remove_project(connection, added.repo)

        self.assertEqual(project_registry.list_projects(connection), ())
        self.assertEqual(
            connection.execute(
                "SELECT status FROM exec_plans WHERE repo = '/repo'"
            ).fetchone()["status"],
            "MERGED",
        )

    def test_registration_recovers_confirmed_archived_catalog_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            remote = parent / "remote.git"
            root = parent / "repo"
            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=main", str(remote)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            root.mkdir()
            self._project_files(root)
            self._git(root, "remote", "add", "origin", str(remote))
            self._git(root, "push", "-u", "origin", "main")
            plans = root / "docs/plans"
            plans.mkdir()
            active = plans / "[G-001] Historical.md"
            active.write_text(
                """---
id: G-001
title: Historical
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

Registration recovery fixture.
""",
                encoding="utf-8",
            )
            self._commit(root, "Add historical plan")
            publication_sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self._git(root, "push", "origin", "main")
            archive = plans / "archive"
            archive.mkdir()
            active.rename(archive / active.name)
            self._commit(root, "Archive historical plan")
            self._git(root, "push", "origin", "main")
            connection = self._connection()
            inspection = loop_manager.ProjectRegistrationInspection(
                root=root,
                base_branch="main",
                provider="codex",
                agents_hash="agents-hash",
                agents_approved=False,
                origin=str(remote),
            )
            proof = {
                "url": "https://pr/1",
                "state": "MERGED",
                "mergedAt": "2026-01-01T00:00:00Z",
            }

            with mock.patch(
                "loop_manager.inspect_project_registration",
                return_value=inspection,
            ), mock.patch("loop_manager.pr_for_branch", return_value=proof):
                loop_manager.add_registered_project(
                    connection,
                    root,
                    config_path=Path("config.ini"),
                    provider="codex",
                    requested_base=None,
                    approve_agents_mismatch=False,
                )
            row = connection.execute(
                "SELECT path, status, plan_sha FROM exec_plans "
                "WHERE repo = ? AND plan_id = 'G-001'",
                (str(root),),
            ).fetchone()
            baseline = maintenance_state.MaintenanceRepository(connection).get(
                str(root)
            ).file_assessment_start_merged_count
            connection.close()

        self.assertEqual(row["path"], "docs/plans/[G-001] Historical.md")
        self.assertEqual(row["status"], "MERGED")
        self.assertEqual(row["plan_sha"], publication_sha)
        self.assertEqual(baseline, 1)

    def test_registration_revision_drift_rolls_back_catalog_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            remote = parent / "remote.git"
            root = parent / "repo"
            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=main", str(remote)],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            root.mkdir()
            self._project_files(root)
            self._git(root, "remote", "add", "origin", str(remote))
            plans = root / "docs/plans"
            plans.mkdir()
            (plans / "[G-001] Ready.md").write_text(
                """---
id: G-001
title: Ready
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

Registration rollback fixture.
""",
                encoding="utf-8",
            )
            self._commit(root, "Add ready plan")
            self._git(root, "push", "-u", "origin", "main")
            source_sha = loop_manager.git(root, "rev-parse", "HEAD").stdout.strip()
            inspection = loop_manager.ProjectRegistrationInspection(
                root=root, base_branch="main", provider="codex",
                agents_hash="agents-hash", agents_approved=False,
                origin=str(remote), source_revision=source_sha,
            )
            connection = self._connection()
            connection.commit()
            real_git = loop_manager.git
            real_snapshot = loop_manager.revision_execplan_snapshot
            snapshot_calls = 0
            revision_checks = 0

            def observed_snapshot(*arguments, **kwargs):
                nonlocal snapshot_calls
                snapshot = real_snapshot(*arguments, **kwargs)
                snapshot_calls += 1
                return snapshot

            def changed_remote_revision(repo, *arguments, **kwargs):
                nonlocal revision_checks
                result = real_git(repo, *arguments, **kwargs)
                if snapshot_calls == 2 and arguments == (
                    "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"
                ):
                    revision_checks += 1
                    return subprocess.CompletedProcess(
                        result.args, 0, stdout="f" * 40
                    )
                return result

            with mock.patch(
                "loop_manager.inspect_project_registration", return_value=inspection
            ), mock.patch("loop_manager.pr_for_branch", return_value=None):
                with mock.patch(
                    "loop_manager.git", side_effect=changed_remote_revision
                ), mock.patch(
                    "loop_manager.revision_execplan_snapshot",
                    side_effect=observed_snapshot,
                ):
                    with self.assertRaisesRegex(RuntimeError, "catalog revision"):
                        loop_manager.add_registered_project(
                            connection, root, config_path=Path("config.ini"),
                            provider="codex", requested_base=None,
                            approve_agents_mismatch=False,
                        )
                self.assertEqual(snapshot_calls, 2)
                self.assertEqual(revision_checks, 1)
                for table in (
                    "projects", "exec_plans", "execplan_catalog_revisions",
                    "maintenance_cycles",
                ):
                    self.assertEqual(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE repo = ?",
                            (str(root),),
                        ).fetchone()[0], 0, table,
                    )

                loop_manager.add_registered_project(
                    connection, root, config_path=Path("config.ini"),
                    provider="codex", requested_base=None,
                    approve_agents_mismatch=False,
                )
            self.assertEqual(len(project_registry.list_projects(connection)), 1)
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
                    (str(root),),
                ).fetchone()[0], "READY",
            )
            self.assertEqual(
                maintenance_state.MaintenanceRepository(connection).get(
                    str(root)
                ).file_assessment_start_merged_count, 0,
            )
            connection.close()

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

            with closing(loop_manager.connect_database(state_dir)) as connection, connection:
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
