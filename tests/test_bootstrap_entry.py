from tests.support.git import isolated_git_test
from contextlib import closing
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


@isolated_git_test
class BootstrapEntryTests(unittest.TestCase):
    def test_remote_child_handoff_preserves_source_config_interpreter_arguments_and_real_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state = Path(directory) / "state"
            remote = advance_remote(writer, path="loop_manager.py", value="VALUE = 'latest-remote-bootstrap'\n")
            (source / "loop_manager.py").write_text("VALUE = 'local-only'\n")
            config = source / "config/config.ini"
            config.parent.mkdir(); config.write_text("secret-sentinel\n")
            before_head = git(source, "rev-parse", "HEAD")
            real_run = subprocess.run
            def child(command, **options):
                if command[0] == "git":
                    return real_run(command, **options)
                if "-c" in command:
                    return subprocess.CompletedProcess(
                        command, 0,
                        stdout='{"created":1,"reused":0,"replaced":0,"unavailable":0,"missing":0}',
                        stderr="",
                    )
                path = Path(command[command.index("-u") + 1])
                self.assertEqual(command[0], loop_manager.sys.executable)
                self.assertIn("-X", command)
                self.assertTrue(command[command.index("-X") + 1].startswith("pycache_prefix="))
                self.assertEqual(path.read_text(), "VALUE = 'latest-remote-bootstrap'\n")
                self.assertEqual(git(path.parent, "rev-parse", "HEAD"), remote)
                self.assertEqual(options["cwd"], path.parent)
                self.assertFalse(options["check"])
                self.assertIsInstance(options["env"], dict)
                self.assertIn("--runtime-child", command)
                self.assertEqual(command[-2:], ["run", "--skip-if-running"])
                self.assertEqual(command[command.index("--config") + 1], str(config))
                self.assertEqual(command[command.index("--runtime-source") + 1], str(source))
                self.assertNotIn("secret-sentinel", " ".join(command))
                self.assertFalse((path.parent / "config/config.ini").exists())
                token = command[command.index("--runtime-mutation-id") + 1]
                from loop_manager_internal.runtime.process_lock import _held_process_lock_identity
                self.assertEqual(_held_process_lock_identity(state), "runtime:" + token)
                with self.assertRaises(loop_manager.ProcessLockBusyError):
                    with loop_manager.process_lock(state):
                        self.fail("parent lock released before child exit")
                return subprocess.CompletedProcess(command, 7)
            with mock.patch.object(loop_manager, "ROOT", source), mock.patch.object(
                loop_manager.sys, "argv", ["loop_manager.py", "--state-dir", str(state), "--config", "config/config.ini", "run", "--skip-if-running"]
            ), mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                "loop_manager.subprocess.run", side_effect=child
            ):
                self.assertEqual(loop_manager.main(), 7)
            self.assertEqual(git(source, "rev-parse", "HEAD"), before_head)
            self.assertEqual((source / "loop_manager.py").read_text(), "VALUE = 'local-only'\n")
            self.assertEqual(config.read_text(), "secret-sentinel\n")
            with loop_manager.process_lock(state):
                pass

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
            return loop_manager.RuntimeWorktree(runtime.path, runtime.commit_sha, None)

        def child(arguments, **kwargs):
            if arguments[0] == "git":
                return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="")
            self.assertEqual(kwargs["cwd"], runtime.path)
            self.assertFalse(kwargs["check"])
            self.assertIsInstance(kwargs["env"], dict)
            self.assertEqual(arguments[0], loop_manager.sys.executable)
            self.assertIn("-X", arguments)
            self.assertTrue(arguments[arguments.index("-X") + 1].startswith("pycache_prefix="))
            self.assertEqual(
                config_path.read_text(), f"[telegram]\ntoken = {sentinel}\n",
            )
            self.assertIn(str(config_path), " ".join(arguments))
            self.assertEqual(arguments[-2:], ["run", "--skip-if-running"])
            self.assertNotIn(sentinel, " ".join(arguments))
            events.append("child")
            return subprocess.CompletedProcess(arguments, 7)

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.ini"
            sentinel = "bootstrap-config-secret-sentinel"
            config_path.write_text(f"[telegram]\ntoken = {sentinel}\n")
            with mock.patch.object(
                loop_manager.sys,
                "argv",
                [
                    "loop_manager.py",
                    "--state-dir",
                    str(state_dir),
                    "--config",
                    str(config_path),
                    "run",
                    "--skip-if-running",
                ],
            ), mock.patch("loop_manager.process_lock", side_effect=locked), mock.patch(
                "loop_manager.prepare_bootstrap_runtime", side_effect=prepare
            ), mock.patch("loop_manager.subprocess.run", side_effect=child):
                self.assertEqual(loop_manager.main(), 7)
        self.assertEqual(events, ["lock", "prepare", "child", "unlock"])
        secured = loop_manager.runtime_child_arguments(
            ["--runtime-mutation-id", "spoofed", "run"],
            source=source, state_dir=state_dir,
            config_path=source / "custom/config.ini",
            runtime=loop_manager.RuntimeWorktree(runtime.path, runtime.commit_sha, "f" * 32),
        )
        self.assertEqual(secured[secured.index("--runtime-mutation-id") + 1], "f" * 32)
        self.assertNotIn("spoofed", secured)


    def test_bootstrap_refreshes_dirty_source_and_starts_remote_runtime_in_same_run(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
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
                child.assert_called_once()
                self.assertEqual(git(source, "rev-parse", "HEAD"), remote_sha)
                self.assertEqual(git(source, "ls-files", "--stage", "--", "tracked.txt"), before_index)
                self.assertEqual((source / "tracked.txt").read_text(), "unstaged\n")
                self.assertEqual((source / "local.txt").read_text(), "untracked\n")
                self.assertEqual(ignored.read_text(), "ignored\n")

                selected = child.call_args.args[3]
                self.assertEqual(selected.commit_sha, remote_sha)
                self.assertEqual(git(selected.path, "rev-parse", "HEAD"), remote_sha)


    def test_bootstrap_refresh_runs_one_shot_commands_without_retry(self):
        commands = (
            ("init",),
            ("config", "init"),
            ("config", "list"),
            ("add", "/project"),
            ("remove", "/project"),
        )
        for command in commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                state_dir = Path(directory) / "state"
                if command not in {("init",), ("config", "init")}:
                    state_dir.mkdir()
                    with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
                        connection.execute(loop_manager.SCHEMA)
                remote_sha = advance_remote(writer)
                arguments = ["--state-dir", str(state_dir), *command]

                with mock.patch.object(loop_manager, "ROOT", source), mock.patch.object(
                    loop_manager.sys, "argv", ["loop_manager.py", *arguments]
                ), mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                    "loop_manager.run_runtime_child", return_value=7
                ) as child, mock.patch("loop_manager.operation_log") as log:
                    self.assertEqual(loop_manager.main(), 7)
                    child.assert_called_once()
                    self.assertEqual(git(source, "rev-parse", "HEAD"), remote_sha)
                    if command in {("init",), ("config", "init")}:
                        self.assertFalse((state_dir / "loop-manager.db").exists())

                    self.assertEqual(child.call_args.args[-1], arguments)
                    self.assertEqual(child.call_args.args[3].commit_sha, remote_sha)


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
            with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
                connection.execute(loop_manager.SCHEMA)
                connection.execute(
                    "INSERT INTO projects (repo, base_branch, origin) VALUES (?, ?, ?)",
                    (str(source), "develop", "owner/repo"),
                )
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "a" * 32):
                    self.assertEqual(
                        loop_manager._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, "a" * 32).commit_sha,
                        latest,
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
                    ), mock.patch("loop_manager.execute_runtime_command", return_value=0) as execute:
                        self.assertEqual(loop_manager.main(), 0)
                        execute.assert_called_once()
            self.assertEqual(runtime.commit_sha, latest)
            self.assertEqual(git(runtime.path, "rev-parse", "HEAD"), latest)


    def test_bootstrap_reads_legacy_registration_without_migrating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
                connection.execute("CREATE TABLE projects (repo TEXT, base_branch TEXT)")
                connection.execute(
                    "INSERT INTO projects VALUES (?, 'develop')", (str(source),)
                )
            self.assertEqual(
                loop_manager._bootstrap_service().bootstrap_base_branch(source, state_dir, "owner/repo"),
                "develop",
            )
            with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
                self.assertEqual(
                    {row[1] for row in connection.execute("PRAGMA table_info(projects)")},
                    {"repo", "base_branch"},
                )


    def test_bootstrap_preserves_source_and_selects_remote_for_all_local_states(self):
        for scenario in ("overlap", "ahead", "diverged", "other-branch", "detached", "operation", "clean-diverged"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                state_dir = Path(directory) / "state"
                if scenario == "overlap":
                    (source / "tracked.txt").write_text("local\n")
                elif scenario in {"ahead", "diverged", "clean-diverged"}:
                    (source / "local.txt").write_text("local\n")
                    commit_all(source, "Local only")
                    if scenario == "diverged":
                        (source / "tracked.txt").write_text("dirty\n")
                elif scenario == "other-branch":
                    git(source, "checkout", "-b", "topic")
                elif scenario == "detached":
                    git(source, "checkout", "--detach")
                elif scenario == "operation":
                    operation = Path(git(source, "rev-parse", "--git-path", "MERGE_HEAD"))
                    if not operation.is_absolute():
                        operation = source / operation
                    operation.write_text(git(source, "rev-parse", "HEAD") + "\n")
                remote_sha = git(writer, "rev-parse", "HEAD") if scenario == "ahead" else advance_remote(writer)
                before = git(source, "rev-parse", "HEAD")
                before_state = loop_manager._source_transition_state(source)
                with mock.patch.object(loop_manager, "ROOT", source), mock.patch.object(
                    loop_manager.sys, "argv", ["loop_manager.py", "--state-dir", str(state_dir), "run"]
                ), mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                    "loop_manager.run_runtime_child", return_value=0
                ) as child:
                    self.assertEqual(loop_manager.main(), 0)
                    child.assert_called_once()
                    runtime = child.call_args.args[3]
                self.assertEqual(runtime.commit_sha, remote_sha)
                self.assertEqual(git(runtime.path, "rev-parse", "HEAD"), remote_sha)
                self.assertFalse((runtime.path / "local.txt").exists())
                if scenario != "clean-diverged":
                    self.assertEqual(loop_manager._source_transition_state(source), before_state)
                else:
                    self.assertNotEqual(git(source, "rev-parse", "HEAD"), before)
                    self.assertEqual((source / "local.txt").read_text(), "local\n")


    def test_bootstrap_continues_after_sync_failure_and_source_race_but_rejects_fetch_failure(self):
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
                    selected = loop_manager._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, "a" * 32)
            self.assertEqual(selected.commit_sha, git(writer, "rev-parse", "HEAD"))
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

            def raced_sync(repo, base, *, remote_sha, state_dir, owner):
                git(source, "checkout", "-b", "topic")
                return loop_manager.BaseWorktreeSyncResult("equal", "raced", remote_sha)

            with mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                "loop_manager.synchronize_base_worktree", side_effect=raced_sync
            ):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "b" * 32):
                    selected = loop_manager._bootstrap_service().prepare_bootstrap_runtime(source, state_dir, "b" * 32)
            self.assertEqual(selected.commit_sha, git(writer, "rev-parse", "HEAD"))


    def test_launchers_only_start_the_python_bootstrap(self):
        bash = (loop_manager.ROOT / "run.sh").read_text(encoding="utf-8")
        powershell = (loop_manager.ROOT / "run.ps1").read_text(encoding="utf-8")
        self.assertNotIn("git ", bash.casefold())
        self.assertNotIn("git ", powershell.casefold())
        self.assertIn('loop_manager.py" run "$@"', bash)
        self.assertIn("loop_manager.py", powershell)
        self.assertIn("run @args", powershell)

    def test_owned_child_dispatch_uses_validated_selection_when_tracking_base_advances(self):
        identity = loop_manager.RuntimeMutation(loop_manager.ROOT, Path('/state/runtime'), 'a' * 40, 'b' * 32)
        with mock.patch.object(loop_manager.sys, 'argv', ['loop_manager.py', '--runtime-child',
                '--runtime-source', str(loop_manager.ROOT), '--runtime-sha', 'a' * 40, 'run']), \
                mock.patch.object(loop_manager, 'validate_runtime_child'), \
                mock.patch.object(loop_manager, 'validate_runtime_mutation', return_value=identity), \
                mock.patch.object(loop_manager, 'validate_bootstrap_revision', side_effect=RuntimeError('stale equality')), \
                mock.patch.object(loop_manager, 'execute_runtime_command', return_value=0) as execute:
            self.assertEqual(loop_manager.main(), 0)
        self.assertEqual(execute.call_args.args[3], identity)

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
