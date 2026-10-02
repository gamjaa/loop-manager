import contextlib
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.storage import agent_invocation_state
from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence, inspect_worktree


from tests.support.origin import (
    git, configure_identity, commit_all, write_registration_files,
    remote_fixture, advance_remote, OriginScopeFixture,
)
from loop_manager_internal.runtime import bootstrap


class BootstrapEntryTests(unittest.TestCase):
    def test_bootstrap_holds_lock_around_runtime_and_normalizes_external_config(self):
        events: list[str] = []
        state_dir = Path("/state").resolve()
        source = loop_manager.ROOT
        runtime = loop_manager.RuntimeWorktree(Path("/state/runtime"), "a" * 40)

        @contextlib.contextmanager
        def locked(_state_dir: Path, *, identity: str):
            self.assertRegex(identity, r"^bootstrap:[0-9a-f]{32}$")
            events.append("lock")
            with tempfile.TemporaryFile() as handle:
                try:
                    yield handle
                finally:
                    events.append("unlock")

        def prepare(actual_source: Path, actual_state: Path, mutation_id: str):
            self.assertEqual(actual_source, source)
            self.assertEqual(actual_state, state_dir)
            events.append("prepare")
            return loop_manager.RuntimeWorktree(runtime.path, runtime.commit_sha, mutation_id)

        def child(actual_source, actual_state, config_path, selected, arguments):
            self.assertEqual(actual_source, source)
            self.assertEqual(actual_state, state_dir)
            self.assertEqual(config_path, source / "custom/config.ini")
            self.assertEqual(selected.path, runtime.path)
            self.assertEqual(selected.commit_sha, runtime.commit_sha)
            self.assertRegex(selected.mutation_id, r"^[0-9a-f]{32}$")
            self.assertEqual(arguments[-2:], ["run", "--skip-if-running"])
            events.append("child")
            return 7

        with mock.patch.object(
            loop_manager.sys,
            "argv",
            [
                "loop_manager.py",
                "--state-dir",
                str(state_dir),
                "--config",
                "custom/config.ini",
                "run",
                "--skip-if-running",
            ],
        ), mock.patch("loop_manager.process_lock", side_effect=locked), mock.patch(
            "loop_manager.prepare_bootstrap_runtime", side_effect=prepare
        ), mock.patch("loop_manager.run_runtime_child", side_effect=child):
            self.assertEqual(loop_manager.main(), 7)
        self.assertEqual(events, ["lock", "prepare", "child", "unlock"])

        arguments = loop_manager.runtime_child_arguments(
            ["--config", "custom/config.ini", "run"],
            source=source,
            state_dir=state_dir,
            config_path=source / "custom/config.ini",
            runtime=runtime,
        )
        self.assertIn(str(source / "custom/config.ini"), arguments)
        self.assertNotIn("secret-value", " ".join(arguments))
        secured = loop_manager.runtime_child_arguments(
            ["--runtime-mutation-id", "spoofed", "run"],
            source=source, state_dir=state_dir,
            config_path=source / "custom/config.ini",
            runtime=loop_manager.RuntimeWorktree(runtime.path, runtime.commit_sha, "f" * 32),
        )
        self.assertEqual(secured[secured.index("--runtime-mutation-id") + 1], "f" * 32)
        self.assertNotIn("spoofed", secured)


    def test_bootstrap_refreshes_dirty_source_then_starts_matching_runtime_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            with sqlite3.connect(state_dir / "loop-manager.db") as connection:
                connection.execute(loop_manager.SCHEMA)
                connection.execute(
                    "INSERT INTO projects (repo, base_branch, origin) VALUES (?, ?, ?)",
                    (str(source), "main", "owner/repo"),
                )
            (source / "tracked.txt").write_text("staged\n")
            git(source, "add", "tracked.txt")
            (source / "tracked.txt").write_text("unstaged\n")
            (source / "local.txt").write_text("untracked\n")
            ignored = source / "ignored/local.txt"
            ignored.parent.mkdir()
            ignored.write_text("ignored\n")
            before_index = git(source, "ls-files", "--stage", "--", "tracked.txt")
            remote_sha = advance_remote(writer, path="remote.txt")

            with mock.patch.object(loop_manager, "ROOT", source), mock.patch.object(
                loop_manager.sys, "argv", ["loop_manager.py", "--state-dir", str(state_dir), "run"]
            ), mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                "loop_manager.run_runtime_child", return_value=0
            ) as child:
                self.assertEqual(loop_manager.main(), 0)
                child.assert_not_called()
                self.assertEqual(git(source, "rev-parse", "HEAD"), remote_sha)
                self.assertFalse((state_dir / "runtimes").exists())
                self.assertEqual(git(source, "ls-files", "--stage", "--", "tracked.txt"), before_index)
                self.assertEqual((source / "tracked.txt").read_text(), "unstaged\n")
                self.assertEqual((source / "local.txt").read_text(), "untracked\n")
                self.assertEqual(ignored.read_text(), "ignored\n")

                self.assertEqual(loop_manager.main(), 0)
                child.assert_called_once()
                selected = child.call_args.args[3]
                self.assertEqual(selected.commit_sha, remote_sha)
                self.assertEqual(git(selected.path, "rev-parse", "HEAD"), remote_sha)


    def test_bootstrap_refresh_requires_retry_for_one_shot_commands(self):
        commands = (
            ("init",),
            ("config", "init"),
            ("config", "list"),
            ("config", "add", "/project"),
            ("config", "remove", "/project"),
        )
        for command in commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                state_dir = Path(directory) / "state"
                if command not in {("init",), ("config", "init")}:
                    state_dir.mkdir()
                    with sqlite3.connect(state_dir / "loop-manager.db") as connection:
                        connection.execute(loop_manager.SCHEMA)
                remote_sha = advance_remote(writer)
                arguments = ["--state-dir", str(state_dir), *command]

                with mock.patch.object(loop_manager, "ROOT", source), mock.patch.object(
                    loop_manager.sys, "argv", ["loop_manager.py", *arguments]
                ), mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                    "loop_manager.run_runtime_child", return_value=7
                ) as child, mock.patch("loop_manager.operation_log") as log:
                    self.assertEqual(loop_manager.main(), 1)
                    child.assert_not_called()
                    self.assertTrue(any(
                        "같은 명령을 다시 실행하세요" in call.args[0]
                        and call.kwargs.get("error") is True
                        for call in log.call_args_list
                    ))
                    self.assertEqual(git(source, "rev-parse", "HEAD"), remote_sha)
                    self.assertFalse((state_dir / "runtimes").exists())
                    if command in {("init",), ("config", "init")}:
                        self.assertFalse((state_dir / "loop-manager.db").exists())

                    self.assertEqual(loop_manager.main(), 7)
                    child.assert_called_once()
                    self.assertEqual(child.call_args.args[-1], arguments)


    def test_bootstrap_equal_sha_allows_dirty_source(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, _writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            (source / "tracked.txt").write_text("local\n")
            git(source, "add", "tracked.txt")
            (source / "untracked.txt").write_text("local\n")
            ignored = source / "ignored/local.txt"
            ignored.parent.mkdir()
            ignored.write_text("local\n")
            expected = git(source, "rev-parse", "HEAD")
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "a" * 32):
                    runtime = loop_manager._bootstrap_service().prepare_bootstrap_runtime(
                        source, state_dir, "a" * 32
                    )
            self.assertIsNotNone(runtime)
            self.assertEqual(runtime.commit_sha, expected)
            self.assertEqual((source / "tracked.txt").read_text(), "local\n")
            self.assertEqual((source / "untracked.txt").read_text(), "local\n")
            self.assertEqual(ignored.read_text(), "local\n")


    def test_bootstrap_uses_registered_develop_base(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            git(writer, "checkout", "-b", "develop")
            develop_base = git(writer, "rev-parse", "HEAD")
            git(writer, "push", "-u", "origin", "develop")
            git(source, "fetch", "origin")
            git(source, "checkout", "-b", "develop", "origin/develop")
            self.assertEqual(git(source, "rev-parse", "HEAD"), develop_base)
            (writer / "develop.txt").write_text("new\n")
            latest = commit_all(writer, "Advance develop")
            git(writer, "push", "origin", "develop")
            with sqlite3.connect(state_dir / "loop-manager.db") as connection:
                connection.execute(loop_manager.SCHEMA)
                connection.execute(
                    "INSERT INTO projects (repo, base_branch, origin) VALUES (?, ?, ?)",
                    (str(source), "develop", "owner/repo"),
                )
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "a" * 32):
                    self.assertEqual(
                        loop_manager._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, "a" * 32),
                        loop_manager.BootstrapRefreshHandoff("develop", latest),
                    )
                self.assertEqual(git(source, "rev-parse", "HEAD"), latest)
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "b" * 32) as handle:
                    runtime = loop_manager._bootstrap_service().prepare_bootstrap_runtime(
                        source, state_dir, "b" * 32
                    )
                    loop_manager._write_process_lock_identity(handle, "runtime:" + "b" * 32)
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        self.assertEqual(
                            loop_manager._bootstrap_service().validate_runtime_mutation(
                                source, state_dir, latest, "b" * 32
                            ).commit_sha,
                            latest,
                        )
                    git(source, "checkout", "-b", "topic")
                    with mock.patch.object(loop_manager, "ROOT", runtime.path), mock.patch.object(
                        loop_manager.sys, "argv", [
                            "loop_manager.py", "--runtime-child",
                            "--runtime-source", str(source),
                            "--runtime-sha", latest,
                            "--runtime-mutation-id", "b" * 32,
                            "--state-dir", str(state_dir), "run",
                        ],
                    ), mock.patch("loop_manager.execute_runtime_command") as execute:
                        self.assertEqual(loop_manager.main(), 1)
                        execute.assert_not_called()
            self.assertEqual(runtime.commit_sha, latest)
            self.assertEqual(git(runtime.path, "rev-parse", "HEAD"), latest)


    def test_bootstrap_reads_legacy_registration_without_migrating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            with sqlite3.connect(state_dir / "loop-manager.db") as connection:
                connection.execute("CREATE TABLE projects (repo TEXT, base_branch TEXT)")
                connection.execute(
                    "INSERT INTO projects VALUES (?, 'develop')", (str(source),)
                )
            self.assertEqual(
                loop_manager._bootstrap_service().bootstrap_base_branch(source, state_dir, "owner/repo"),
                "develop",
            )
            with sqlite3.connect(state_dir / "loop-manager.db") as connection:
                self.assertEqual(
                    {row[1] for row in connection.execute("PRAGMA table_info(projects)")},
                    {"repo", "base_branch"},
                )


    def test_bootstrap_rejects_unsafe_refresh_and_does_not_start_runtime(self):
        for scenario in ("overlap", "ahead", "diverged", "other-branch", "operation"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                state_dir = Path(directory) / "state"
                if scenario == "overlap":
                    (source / "tracked.txt").write_text("local\n")
                elif scenario in {"ahead", "diverged"}:
                    (source / "local.txt").write_text("local\n")
                    commit_all(source, "Local only")
                elif scenario == "other-branch":
                    git(source, "checkout", "-b", "topic")
                elif scenario == "operation":
                    operation = Path(git(source, "rev-parse", "--git-path", "MERGE_HEAD"))
                    if not operation.is_absolute():
                        operation = source / operation
                    operation.write_text(git(source, "rev-parse", "HEAD") + "\n")
                if scenario != "ahead":
                    advance_remote(writer)
                before = git(source, "rev-parse", "HEAD")
                with mock.patch.object(loop_manager, "ROOT", source), mock.patch.object(
                    loop_manager.sys, "argv", ["loop_manager.py", "--state-dir", str(state_dir), "run"]
                ), mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                    "loop_manager.run_runtime_child"
                ) as child:
                    self.assertEqual(loop_manager.main(), 1)
                    child.assert_not_called()
                self.assertEqual(git(source, "rev-parse", "HEAD"), before)
                self.assertFalse((state_dir / "runtimes").exists())


    def test_bootstrap_rejects_git_failures_and_source_race(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            advance_remote(writer)
            real_git = loop_manager.git

            def failing_git(repo, *arguments, **kwargs):
                if arguments[:2] == ("merge", "--ff-only"):
                    return subprocess.CompletedProcess(arguments, 128, "fatal: merge refused")
                return real_git(repo, *arguments, **kwargs)

            with mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                "loop_manager.git", side_effect=failing_git
            ):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "a" * 32):
                    with self.assertRaisesRegex(RuntimeError, "fast-forward 실패"):
                        loop_manager._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, "a" * 32)
            self.assertFalse((state_dir / "runtimes").exists())
            advance_remote(writer, value="three\n")

            def failing_fetch(repo, *arguments, **kwargs):
                if arguments[:2] == ("fetch", "--no-tags"):
                    raise RuntimeError("fetch refused")
                return real_git(repo, *arguments, **kwargs)

            with mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                "loop_manager.git", side_effect=failing_fetch
            ):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "c" * 32):
                    with self.assertRaisesRegex(RuntimeError, "fetch refused"):
                        loop_manager._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, "c" * 32)
            self.assertFalse((state_dir / "runtimes").exists())

            def raced_sync(repo, base, *, remote_sha, state_dir, owner):
                git(source, "checkout", "-b", "topic")
                return loop_manager.BaseWorktreeSyncResult("equal", "raced", remote_sha)

            with mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                "loop_manager.synchronize_base_worktree", side_effect=raced_sync
            ):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "b" * 32):
                    with self.assertRaisesRegex(RuntimeError, "bootstrap revision 변경"):
                        loop_manager._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, "b" * 32)
            self.assertFalse((state_dir / "runtimes").exists())


    def test_launchers_only_start_the_python_bootstrap(self):
        bash = (loop_manager.ROOT / "run.sh").read_text(encoding="utf-8")
        powershell = (loop_manager.ROOT / "run.ps1").read_text(encoding="utf-8")
        self.assertNotIn("git ", bash.casefold())
        self.assertNotIn("git ", powershell.casefold())
        self.assertIn('loop_manager.py" run "$@"', bash)
        self.assertIn("loop_manager.py", powershell)
        self.assertIn("run @args", powershell)

    def test_runtime_child_does_not_reacquire_process_lock(self):
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
                "run",
            ],
        ), mock.patch("loop_manager.validate_runtime_child"), mock.patch(
            "loop_manager.validate_runtime_mutation",
            return_value=loop_manager.RuntimeMutation(loop_manager.ROOT, Path("/state/runtime"), "a" * 40, None, True),
        ), mock.patch(
            "loop_manager.execute_runtime_command", return_value=0
        ), mock.patch("loop_manager.process_lock") as process_lock:
            self.assertEqual(loop_manager.main(), 0)
        process_lock.assert_not_called()
