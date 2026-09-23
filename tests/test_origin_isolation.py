import contextlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"git {' '.join(arguments)} failed:\n{result.stdout}")
    return result.stdout.strip()


def configure_identity(repo: Path) -> None:
    git(repo, "config", "user.email", "loop@example.com")
    git(repo, "config", "user.name", "Loop Test")


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def write_registration_files(repo: Path) -> None:
    (repo / "docs/requirements").mkdir(parents=True)
    (repo / "AGENTS.md").write_bytes((loop_manager.ROOT / "AGENTS.md").read_bytes())
    (repo / "docs/PLANS.md").write_bytes(
        (loop_manager.ROOT / "docs/PLANS.md").read_bytes()
    )
    (repo / "docs/ARCHITECTURE.md").write_text(
        "# Test Architecture\n\nCommitted design.\n",
        encoding="utf-8",
    )
    (repo / "docs/PRODUCT.md").write_text(
        "# Test Product\n\nCommitted behavior.\n",
        encoding="utf-8",
    )


def remote_fixture(directory: str) -> tuple[Path, Path, Path]:
    root = Path(directory)
    remote = root / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    seed = root / "seed"
    subprocess.run(
        ["git", "clone", str(remote), str(seed)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    configure_identity(seed)
    git(seed, "checkout", "-b", "main")
    (seed / ".gitignore").write_text(
        "config/config.ini\n.venv/\n.cache/\nignored/\n",
        encoding="utf-8",
    )
    (seed / "tracked.txt").write_text("one\n", encoding="utf-8")
    (seed / "loop_manager.py").write_text("VALUE = 'origin-one'\n", encoding="utf-8")
    commit_all(seed, "Initial")
    git(seed, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    primary = root / "primary"
    writer = root / "writer"
    for checkout in (primary, writer):
        subprocess.run(
            ["git", "clone", str(remote), str(checkout)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        configure_identity(checkout)
    return remote, primary, writer


def advance_remote(writer: Path, *, path: str = "tracked.txt", value: str = "two\n") -> str:
    target = writer / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    git(writer, "add", "-f", "--", path)
    git(writer, "commit", "-m", f"Advance {path}")
    git(writer, "push", "origin", "main")
    return git(writer, "rev-parse", "HEAD")


class OriginIsolatedRegistrationAndExecutionTest(unittest.TestCase):
    def test_registration_reads_only_the_selected_revision_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-b", "main")
            configure_identity(repo)
            write_registration_files(repo)
            committed_agents = commit_all(repo, "Valid registration")

            (repo / "AGENTS.md").write_text("# local-only mismatch\n", encoding="utf-8")
            (repo / "docs/PLANS.md").unlink()
            agents_hash, approved = loop_manager.inspect_registration_files(
                repo,
                committed_agents,
                approve_agents_mismatch=False,
            )
            self.assertFalse(approved)
            self.assertEqual(
                agents_hash,
                loop_manager.file_hash(loop_manager.ROOT / "AGENTS.md"),
            )

            def validate(_path, **kwargs):
                kwargs["origin_result"].append("example/project")
                return repo

            with mock.patch(
                "loop_manager.validate_repository", side_effect=validate
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.git",
                return_value=subprocess.CompletedProcess(
                    (), 0, stdout=committed_agents + "\n"
                ),
            ) as revision_git, mock.patch(
                "loop_manager.inspect_registration_files",
                return_value=("agents-hash", False),
            ) as inspect_files:
                inspection = loop_manager.inspect_project_registration(
                    repo,
                    config_path=Path("config.ini"),
                    provider="codex",
                    requested_base=None,
                    approve_agents_mismatch=False,
                )
            revision_git.assert_called_once_with(
                repo,
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main^{commit}",
            )
            inspect_files.assert_called_once_with(
                repo,
                committed_agents,
                approve_agents_mismatch=False,
            )
            self.assertEqual(inspection.origin, "example/project")

            (repo / "AGENTS.md").write_bytes(
                (loop_manager.ROOT / "AGENTS.md").read_bytes()
            )
            (repo / "docs/PLANS.md").write_text("remote-invalid\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-m", "Invalid remote plans")
            (repo / "docs/PLANS.md").write_bytes(
                (loop_manager.ROOT / "docs/PLANS.md").read_bytes()
            )
            with self.assertRaisesRegex(RuntimeError, "PLANS.md"):
                loop_manager.inspect_registration_files(
                    repo,
                    "HEAD",
                    approve_agents_mismatch=True,
                )

    def test_safe_fast_forward_updates_only_eligible_checkout_and_preserves_ignored_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            equal = loop_manager.synchronize_base_worktree(primary, "main")
            self.assertEqual(equal.status, "equal")

            config = primary / "config/config.ini"
            environment = primary / ".venv/bin/python"
            cache = primary / ".cache/tool/state"
            for path, value in (
                (config, "secret-value\n"),
                (environment, "python\n"),
                (cache, "cache\n"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
            expected = advance_remote(writer)
            git(primary, "fetch", "origin")

            updated = loop_manager.synchronize_base_worktree(
                primary,
                "main",
                remote_sha=expected,
            )
            self.assertEqual(updated.status, "updated")
            self.assertEqual(git(primary, "rev-parse", "HEAD"), expected)
            self.assertEqual((primary / "tracked.txt").read_text(), "two\n")
            self.assertEqual(config.read_text(), "secret-value\n")
            self.assertEqual(environment.read_text(), "python\n")
            self.assertEqual(cache.read_text(), "cache\n")

    def test_safe_fast_forward_skips_local_state_matrix_without_gating_remote_sha(self):
        scenarios = (
            "tracked",
            "staged",
            "untracked",
            "ahead",
            "diverged",
            "detached",
            "other-branch",
            "operation",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                _remote, primary, writer = remote_fixture(directory)
                initial = git(primary, "rev-parse", "HEAD")
                if scenario == "tracked":
                    (primary / "tracked.txt").write_text("local\n", encoding="utf-8")
                elif scenario == "staged":
                    (primary / "staged.txt").write_text("local\n", encoding="utf-8")
                    git(primary, "add", "staged.txt")
                elif scenario == "untracked":
                    (primary / "untracked.txt").write_text("local\n", encoding="utf-8")
                elif scenario in {"ahead", "diverged"}:
                    (primary / "local.txt").write_text("local\n", encoding="utf-8")
                    commit_all(primary, "Local only")
                elif scenario == "detached":
                    git(primary, "checkout", "--detach", initial)
                elif scenario == "other-branch":
                    git(primary, "checkout", "-b", "topic")
                elif scenario == "operation":
                    merge_head = Path(git(primary, "rev-parse", "--git-path", "MERGE_HEAD"))
                    if not merge_head.is_absolute():
                        merge_head = primary / merge_head
                    merge_head.write_text(initial + "\n", encoding="ascii")

                remote_sha = (
                    git(writer, "rev-parse", "HEAD")
                    if scenario == "ahead"
                    else advance_remote(writer)
                )
                git(primary, "fetch", "origin")
                before_head = git(primary, "rev-parse", "HEAD")
                result = loop_manager.synchronize_base_worktree(
                    primary,
                    "main",
                    remote_sha=remote_sha,
                )
                self.assertEqual(result.status, "skipped")
                self.assertEqual(result.remote_sha, remote_sha)
                self.assertEqual(git(primary, "rev-parse", "HEAD"), before_head)

    def test_ignored_path_overlap_and_fast_forward_failures_preserve_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            ignored = primary / "config/config.ini"
            ignored.parent.mkdir(parents=True)
            ignored.write_text("keep-secret\n", encoding="utf-8")
            remote_sha = advance_remote(
                writer,
                path="config/config.ini",
                value="tracked-default\n",
            )
            git(primary, "fetch", "origin")
            before = git(primary, "rev-parse", "HEAD")

            overlap = loop_manager.synchronize_base_worktree(
                primary,
                "main",
                remote_sha=remote_sha,
            )
            self.assertEqual(overlap.status, "skipped")
            self.assertIn("ignored", overlap.reason)
            self.assertEqual(ignored.read_text(), "keep-secret\n")
            self.assertEqual(git(primary, "rev-parse", "HEAD"), before)

        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            remote_sha = advance_remote(writer)
            git(primary, "fetch", "origin")
            before = git(primary, "rev-parse", "HEAD")
            real_git = loop_manager.git

            def locked(repo: Path, *arguments: str, **kwargs):
                if arguments[:2] == ("merge", "--ff-only"):
                    return subprocess.CompletedProcess(
                        arguments,
                        128,
                        stdout="fatal: Unable to create '.git/index.lock': File exists",
                    )
                return real_git(repo, *arguments, **kwargs)

            with mock.patch("loop_manager.git", side_effect=locked):
                failed = loop_manager.synchronize_base_worktree(
                    primary,
                    "main",
                    remote_sha=remote_sha,
                )
            self.assertEqual(failed.status, "failed")
            self.assertIn("lock", failed.reason.casefold())
            self.assertEqual(git(primary, "rev-parse", "HEAD"), before)

    def test_concurrent_user_change_after_eligibility_skips_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            remote_sha = advance_remote(writer)
            git(primary, "fetch", "origin")
            before = git(primary, "rev-parse", "HEAD")
            real_snapshot = loop_manager._base_worktree_snapshot
            calls = 0

            def changing_snapshot(repo: Path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    (repo / "concurrent.txt").write_text(
                        "user-change\n",
                        encoding="utf-8",
                    )
                return real_snapshot(repo)

            with mock.patch(
                "loop_manager._base_worktree_snapshot",
                side_effect=changing_snapshot,
            ):
                result = loop_manager.synchronize_base_worktree(
                    primary,
                    "main",
                    remote_sha=remote_sha,
                )
            self.assertEqual(result.status, "skipped")
            self.assertIn("상태가 변경", result.reason)
            self.assertEqual(git(primary, "rev-parse", "HEAD"), before)
            self.assertEqual(
                (primary / "concurrent.txt").read_text(),
                "user-change\n",
            )

    def test_exact_remote_sha_seeds_planning_and_implementation_worktrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _remote, primary, writer = remote_fixture(directory)
            remote_sha = advance_remote(writer)
            git(primary, "fetch", "origin")
            (primary / "local.txt").write_text("local-only\n", encoding="utf-8")
            commit_all(primary, "Ahead local commit")
            (primary / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

            planning = root / "state/worktrees/project/planning/fingerprint"
            implementation = root / "state/worktrees/project/G-022"
            loop_manager.make_planning_worktree(
                primary,
                planning,
                "main",
                expected_head=remote_sha,
            )
            loop_manager.make_worktree(
                primary,
                implementation,
                "loop-manager/G-022",
                "main",
                expected_head=remote_sha,
            )
            for managed in (planning, implementation):
                self.assertEqual(git(managed, "rev-parse", "HEAD"), remote_sha)
                self.assertFalse((managed / "local.txt").exists())
                self.assertFalse((managed / "uncommitted.txt").exists())

    def test_runtime_is_pinned_to_origin_sha_and_rejects_dirty_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _remote, source, writer = remote_fixture(directory)
            state_dir = root / "state"
            (source / "config/config.ini").parent.mkdir(parents=True)
            (source / "config/config.ini").write_text("secret\n", encoding="utf-8")
            (source / ".venv/bin/python").parent.mkdir(parents=True)
            (source / ".venv/bin/python").write_text("python\n", encoding="utf-8")
            (source / ".cache/tool").mkdir(parents=True)
            (source / ".cache/tool/value").write_text("cache\n", encoding="utf-8")
            (source / "loop_manager.py").write_text("VALUE = 'local-only'\n", encoding="utf-8")
            commit_all(source, "Local-only runtime change")

            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                first = loop_manager.prepare_runtime_worktree(source, state_dir)
            self.assertEqual(
                (first.path / "loop_manager.py").read_text(),
                "VALUE = 'origin-one'\n",
            )
            self.assertEqual(git(first.path, "rev-parse", "HEAD"), first.commit_sha)
            self.assertEqual(git(first.path, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
            self.assertFalse((first.path / "config/config.ini").exists())
            self.assertFalse((first.path / ".venv").exists())
            self.assertFalse((first.path / ".cache").exists())

            next_sha = advance_remote(writer, path="loop_manager.py", value="VALUE = 'origin-two'\n")
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                second = loop_manager.prepare_runtime_worktree(source, state_dir)
            self.assertEqual(second.commit_sha, next_sha)
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(git(first.path, "rev-parse", "HEAD"), first.commit_sha)
            self.assertEqual((second.path / "loop_manager.py").read_text(), "VALUE = 'origin-two'\n")

            (second.path / "loop_manager.py").write_text("dirty\n", encoding="utf-8")
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with self.assertRaisesRegex(loop_manager.WorkspaceOwnershipError, "clean"):
                    loop_manager.prepare_runtime_worktree(source, state_dir)
            self.assertEqual((second.path / "loop_manager.py").read_text(), "dirty\n")

            git(second.path, "checkout", "--", "loop_manager.py")
            git(second.path, "checkout", "-b", "runtime-topic")
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with self.assertRaisesRegex(
                    loop_manager.WorkspaceOwnershipError,
                    "detached HEAD",
                ):
                    loop_manager.prepare_runtime_worktree(source, state_dir)
            self.assertEqual(git(second.path, "branch", "--show-current"), "runtime-topic")

    def test_runtime_rejects_symlink_escape_before_worktree_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _remote, source, _writer = remote_fixture(directory)
            state_dir = root / "state"
            outside = root / "outside"
            state_dir.mkdir()
            outside.mkdir()
            try:
                (state_dir / "runtimes").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlink creation is unavailable: {error}")

            with mock.patch(
                "loop_manager.github_repository", return_value="owner/repo"
            ):
                with self.assertRaisesRegex(
                    loop_manager.WorkspaceOwnershipError,
                    "상태 디렉터리 밖",
                ):
                    loop_manager.prepare_runtime_worktree(source, state_dir)

            self.assertEqual(tuple(outside.iterdir()), ())
            self.assertNotIn(
                str(outside),
                git(source, "worktree", "list", "--porcelain"),
            )

    def test_bootstrap_holds_lock_around_runtime_and_normalizes_external_config(self):
        events: list[str] = []
        state_dir = Path("/state").resolve()
        source = loop_manager.ROOT
        runtime = loop_manager.RuntimeWorktree(Path("/state/runtime"), "a" * 40)

        @contextlib.contextmanager
        def locked(_state_dir: Path):
            events.append("lock")
            try:
                yield
            finally:
                events.append("unlock")

        def prepare(actual_source: Path, actual_state: Path):
            self.assertEqual(actual_source, source)
            self.assertEqual(actual_state, state_dir)
            events.append("prepare")
            return runtime

        def child(actual_source, actual_state, config_path, selected, arguments):
            self.assertEqual(actual_source, source)
            self.assertEqual(actual_state, state_dir)
            self.assertEqual(config_path, source / "custom/config.ini")
            self.assertEqual(selected, runtime)
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
            "loop_manager.prepare_runtime_worktree", side_effect=prepare
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
            "loop_manager.execute_runtime_command", return_value=0
        ), mock.patch("loop_manager.process_lock") as process_lock:
            self.assertEqual(loop_manager.main(), 0)
        process_lock.assert_not_called()

    def test_git_lock_outside_synchronizer_is_transient_and_preserves_state(self):
        connection = sqlite3.connect(":memory:")
        with mock.patch(
            "loop_manager._run_project_step",
            side_effect=loop_manager.GitLockError("index.lock exists"),
        ):
            result = loop_manager.run_project_result(
                connection,
                Path("/state"),
                "/repo",
                Path("/config.ini"),
                None,
                False,
            )
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            connection.execute(
                "SELECT reason_code, pending_action FROM recovery_records "
                "WHERE repo = ? AND subject = '__project__'",
                ("/repo",),
            ).fetchone(),
            ("git_lock", "pause"),
        )

    def test_launchers_only_start_the_python_bootstrap(self):
        bash = (loop_manager.ROOT / "run.sh").read_text(encoding="utf-8")
        powershell = (loop_manager.ROOT / "run.ps1").read_text(encoding="utf-8")
        self.assertNotIn("git ", bash.casefold())
        self.assertNotIn("git ", powershell.casefold())
        self.assertIn('loop_manager.py" run "$@"', bash)
        self.assertIn("loop_manager.py", powershell)
        self.assertIn("run @args", powershell)


if __name__ == "__main__":
    unittest.main()
