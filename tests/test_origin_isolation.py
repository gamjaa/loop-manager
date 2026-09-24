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
    def _persist_planner_scope(self, connection, repo: Path):
        before = loop_manager.capture_git_snapshot(repo)
        connection.execute(loop_manager.SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, phase, worktree, agent_role, agent_snapshot) "
            "VALUES (?, 'main', 'PLANNING', ?, 'planner', ?)",
            (str(repo), str(repo), before.serialize()),
        )
        connection.commit()
        return before

    def test_owned_runtime_reconciles_saved_planner_scope_and_reuses_same_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            before = self._persist_planner_scope(connection, source)
            selected_sha = advance_remote(writer)
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir) as handle:
                    mutation_id = "a" * 32
                    runtime = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id=mutation_id,
                    )
                    loop_manager._write_process_lock_identity(handle, f"runtime:{mutation_id}")
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        identity = loop_manager.validate_runtime_mutation(
                            source, state_dir, selected_sha, mutation_id,
                        )
                        loop_manager.validate_persisted_agent_scope(
                            connection, source, state_dir, str(source),
                            runtime_mutation=identity,
                        )
                    saved = loop_manager.GitSnapshot.deserialize(connection.execute(
                        "SELECT agent_snapshot FROM projects WHERE repo = ?", (str(source),)
                    ).fetchone()[0])
                    self.assertNotEqual(saved, before)
                    self.assertIn(str(runtime.path), saved.worktrees)
                    self.assertEqual(saved.refs["refs/remotes/origin/main"][0], selected_sha)

                with mock.patch.object(loop_manager, "ROOT", runtime.path):
                    with self.assertRaisesRegex(RuntimeError, "process lock"):
                        loop_manager.validate_runtime_mutation(
                            source, state_dir, selected_sha, mutation_id,
                        )
                with self.assertRaisesRegex(loop_manager.AgentScopeError, "process lock"):
                    loop_manager.validate_persisted_agent_scope(
                        connection, source, state_dir, str(source),
                        runtime_mutation=identity,
                    )

                with loop_manager.process_lock(state_dir) as handle:
                    next_id = "b" * 32
                    reused = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id=next_id,
                    )
                    self.assertEqual(reused.path, runtime.path)
                    loop_manager._write_process_lock_identity(handle, f"runtime:{next_id}")
                    with mock.patch.object(loop_manager, "ROOT", reused.path):
                        identity = loop_manager.validate_runtime_mutation(
                            source, state_dir, selected_sha, next_id,
                        )
                        loop_manager.validate_persisted_agent_scope(
                            connection, source, state_dir, str(source),
                            runtime_mutation=identity,
                        )
                self.assertEqual(
                    git(source, "worktree", "list", "--porcelain").count(str(runtime.path)), 1,
                )
            connection.close()

    def test_scope_rechecks_runtime_registration_marker_and_intent(self):
        for change in ("unlock", "change_reason", "alter_intent"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                state_dir = Path(directory) / "state"
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                before = self._persist_planner_scope(connection, source)
                selected_sha = advance_remote(writer)
                mutation_id = "a" * 32
                with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                    with loop_manager.process_lock(state_dir, identity=f"bootstrap:{mutation_id}") as handle:
                        runtime = loop_manager.prepare_runtime_worktree(
                            source, state_dir, mutation_id=mutation_id,
                        )
                        loop_manager._write_process_lock_identity(handle, f"runtime:{mutation_id}")
                        with mock.patch.object(loop_manager, "ROOT", runtime.path):
                            identity = loop_manager.validate_runtime_mutation(
                                source, state_dir, selected_sha, mutation_id,
                            )
                        if change in {"unlock", "change_reason"}:
                            git(source, "worktree", "unlock", str(runtime.path))
                        if change == "change_reason":
                            git(source, "worktree", "lock", "--reason=outside", str(runtime.path))
                        if change == "alter_intent":
                            loop_manager._runtime_intent_path(
                                state_dir, mutation_id,
                            ).write_text("{}", encoding="utf-8")
                        with self.assertRaisesRegex(
                            loop_manager.AgentScopeError, "runtime mutation 검증 실패",
                        ):
                            loop_manager.validate_persisted_agent_scope(
                                connection, source, state_dir, str(source),
                                runtime_mutation=identity,
                            )
                        saved = connection.execute(
                            "SELECT agent_snapshot FROM projects WHERE repo = ?", (str(source),)
                        ).fetchone()[0]
                        self.assertEqual(saved, before.serialize())
                connection.close()

    def test_scope_rechecks_reused_runtime_origin_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            before = self._persist_planner_scope(connection, source)
            selected_sha = advance_remote(writer)
            first_id, second_id = "a" * 32, "b" * 32
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir, identity=f"bootstrap:{first_id}"):
                    first = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id=first_id,
                    )
                with loop_manager.process_lock(state_dir, identity=f"bootstrap:{second_id}") as handle:
                    reused = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id=second_id,
                    )
                    self.assertEqual(reused.path, first.path)
                    loop_manager._write_process_lock_identity(handle, f"runtime:{second_id}")
                    with mock.patch.object(loop_manager, "ROOT", reused.path):
                        identity = loop_manager.validate_runtime_mutation(
                            source, state_dir, selected_sha, second_id,
                        )
                    loop_manager._runtime_intent_path(
                        state_dir, first_id,
                    ).write_text("{}", encoding="utf-8")
                    with self.assertRaisesRegex(
                        loop_manager.AgentScopeError, "runtime mutation 검증 실패",
                    ):
                        loop_manager.validate_persisted_agent_scope(
                            connection, source, state_dir, str(source),
                            runtime_mutation=identity,
                        )
                    saved = connection.execute(
                        "SELECT agent_snapshot FROM projects WHERE repo = ?", (str(source),)
                    ).fetchone()[0]
                    self.assertEqual(saved, before.serialize())
            connection.close()

    def test_saved_scope_block_reopens_only_for_exact_runtime_delta(self):
        for mixed in (False, True):
            with self.subTest(mixed=mixed), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                state_dir = Path(directory) / "state"
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                before = self._persist_planner_scope(connection, source)
                selected_sha = advance_remote(writer)
                with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                    with loop_manager.process_lock(state_dir):
                        runtime = loop_manager.prepare_runtime_worktree(source, state_dir)
                        if mixed:
                            git(source, "worktree", "add", "--detach",
                                str(state_dir / "external"), selected_sha)
                        current = loop_manager.capture_git_snapshot(source)
                        connection.execute(
                            "UPDATE projects SET phase = 'PROJECT_BLOCKED', "
                            "agent_result_snapshot = ? WHERE repo = ?",
                            (current.serialize(), str(source)),
                        )
                        loop_manager.scoped_blocking.ensure_schema(connection)
                        connection.execute(
                            "INSERT INTO project_blocks "
                            "(repo, scope, reason_code, evidence, previous_phase) "
                            "VALUES (?, 'project', 'outside_managed_worktree_mutation', ?, 'PLANNING')",
                            (str(source), "Agent 범위 선검증 실패:\nruntime"),
                        )
                        connection.commit()
                        project = connection.execute(
                            "SELECT * FROM projects WHERE repo = ?", (str(source),)
                        ).fetchone()
                        with mock.patch.object(loop_manager, "ROOT", runtime.path):
                            identity = loop_manager.validate_runtime_mutation(
                                source, state_dir, selected_sha, None,
                            )
                        reopened = loop_manager.reconcile_false_positive_runtime_block(
                            connection, source, state_dir, str(source), project,
                            {"evidence": "Agent 범위 선검증 실패:\nruntime"}, identity,
                        )
                        self.assertEqual(reopened, not mixed)
                        self.assertEqual(loop_manager.resume_project_block_if_safe(
                            connection, source, state_dir, str(source),
                        ), not mixed)
                        saved = connection.execute(
                            "SELECT agent_snapshot, agent_result_snapshot FROM projects WHERE repo = ?",
                            (str(source),),
                        ).fetchone()
                        self.assertEqual(saved["agent_result_snapshot"], current.serialize())
                        self.assertEqual(saved["agent_snapshot"] == before.serialize(), mixed)
                connection.close()

    def test_registration_intent_survives_interruption_before_child(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            self._persist_planner_scope(connection, source)
            selected_sha = advance_remote(writer)
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "a" * 32):
                    first = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id="a" * 32,
                    )
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "b" * 32) as handle:
                    reused = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id="b" * 32,
                    )
                    self.assertEqual(reused.path, first.path)
                    loop_manager._write_process_lock_identity(handle, "runtime:" + "b" * 32)
                    with mock.patch.object(loop_manager, "ROOT", reused.path):
                        identity = loop_manager.validate_runtime_mutation(
                            source, state_dir, selected_sha, "b" * 32,
                        )
                    loop_manager.validate_persisted_agent_scope(
                        connection, source, state_dir, str(source),
                        runtime_mutation=identity,
                    )
            connection.close()

    def test_new_parent_rejects_unmarked_existing_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            advance_remote(writer)
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir):
                    runtime = loop_manager.prepare_runtime_worktree(source, state_dir)
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "e" * 32):
                    with self.assertRaisesRegex(
                        loop_manager.WorkspaceOwnershipError, "구버전 전환 소유 증거",
                    ):
                        loop_manager.prepare_runtime_worktree(
                            source, state_dir, mutation_id="e" * 32,
                        )
                self.assertTrue(runtime.path.is_dir())

    def test_child_rejects_changed_runtime_catalog_lock_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            selected_sha = advance_remote(writer)
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                mutation_id = "f" * 32
                with loop_manager.process_lock(
                    state_dir, identity=f"bootstrap:{mutation_id}",
                ) as handle:
                    runtime = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id=mutation_id,
                    )
                    git(source, "worktree", "unlock", str(runtime.path))
                    git(source, "worktree", "lock", "--reason=outside", str(runtime.path))
                    loop_manager._write_process_lock_identity(handle, f"runtime:{mutation_id}")
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        with self.assertRaisesRegex(RuntimeError, "durable mutation 표식"):
                            loop_manager.validate_runtime_mutation(
                                source, state_dir, selected_sha, mutation_id,
                            )
                    self.assertTrue(runtime.path.is_dir())

    def test_legacy_parent_lock_allows_exact_runtime_but_new_lock_requires_id(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            before = self._persist_planner_scope(connection, source)
            selected_sha = advance_remote(writer)
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir):
                    # Reproduce the pre-mutation-ID parent without calling the new bootstrap.
                    git(source, "fetch", "origin")
                    runtime_path = loop_manager.expected_runtime_worktree(
                        state_dir, source, selected_sha,
                    )
                    runtime_path.parent.mkdir(parents=True, exist_ok=True)
                    git(source, "worktree", "add", "--detach", str(runtime_path), selected_sha)
                    runtime = loop_manager.RuntimeWorktree(runtime_path, selected_sha)
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        def resume(_args, _state, _config, identity):
                            self.assertTrue(identity.legacy)
                            loop_manager.validate_persisted_agent_scope(
                                connection, source, state_dir, str(source),
                                runtime_mutation=identity,
                            )
                            return 0

                        with mock.patch.object(loop_manager.sys, "argv", [
                            "loop_manager.py", "--runtime-child",
                            "--runtime-source", str(source),
                            "--runtime-sha", selected_sha,
                            "--state-dir", str(state_dir), "run",
                        ]), mock.patch(
                            "loop_manager.execute_runtime_command", side_effect=resume,
                        ) as execute:
                            self.assertEqual(loop_manager.main(), 0)
                            self.assertTrue(execute.call_args.args[3].legacy)
                self.assertNotEqual(connection.execute(
                    "SELECT agent_snapshot FROM projects WHERE repo = ?", (str(source),)
                ).fetchone()[0], before.serialize())

                with loop_manager.process_lock(state_dir):
                    reused_legacy = loop_manager.prepare_runtime_worktree(source, state_dir)
                    self.assertEqual(reused_legacy.path, runtime.path)
                    with mock.patch.object(loop_manager, "ROOT", reused_legacy.path):
                        self.assertTrue(loop_manager.validate_runtime_mutation(
                            source, state_dir, selected_sha, None,
                        ).legacy)

                with loop_manager.process_lock(state_dir) as handle:
                    loop_manager._write_process_lock_identity(handle, "runtime:" + "c" * 32)
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        with self.assertRaisesRegex(RuntimeError, "mutation ID가 없는 새 형식"):
                            loop_manager.validate_runtime_mutation(
                                source, state_dir, selected_sha, None,
                            )
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "d" * 32) as handle:
                    adopted = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id="d" * 32,
                    )
                    self.assertEqual(adopted.path, runtime.path)
                    loop_manager._write_process_lock_identity(handle, "runtime:" + "d" * 32)
                    with mock.patch.object(loop_manager, "ROOT", adopted.path):
                        self.assertFalse(loop_manager.validate_runtime_mutation(
                            source, state_dir, selected_sha, "d" * 32,
                        ).legacy)
            connection.close()

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

    def test_safe_fast_forward_preserves_disjoint_dirty_files_and_index(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            git(primary, "config", "merge.autostash", "true")
            tracked = primary / "tracked.txt"
            tracked.write_text("staged\n", encoding="utf-8")
            git(primary, "add", "tracked.txt")
            tracked.write_text("unstaged\n", encoding="utf-8")
            (primary / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            ignored = primary / "ignored/local.txt"
            ignored.parent.mkdir()
            ignored.write_text("ignored\n", encoding="utf-8")
            local_index = git(primary, "ls-files", "--stage", "--", "tracked.txt")
            local_staged = git(primary, "diff", "--cached", "--", "tracked.txt")
            local_unstaged = git(primary, "diff", "--", "tracked.txt")
            remote_sha = advance_remote(writer, path="remote.txt", value="remote\n")
            git(primary, "fetch", "origin")

            result = loop_manager.synchronize_base_worktree(
                primary, "main", remote_sha=remote_sha
            )

            self.assertEqual(result.status, "updated", result.reason)
            self.assertEqual(git(primary, "rev-parse", "HEAD"), remote_sha)
            self.assertEqual((primary / "remote.txt").read_text(), "remote\n")
            self.assertEqual(tracked.read_text(), "unstaged\n")
            self.assertEqual(git(primary, "ls-files", "--stage", "--", "tracked.txt"), local_index)
            self.assertEqual(git(primary, "diff", "--cached", "--", "tracked.txt"), local_staged)
            self.assertEqual(git(primary, "diff", "--", "tracked.txt"), local_unstaged)
            self.assertEqual((primary / "untracked.txt").read_text(), "untracked\n")
            self.assertEqual(ignored.read_text(), "ignored\n")
            self.assertEqual(git(primary, "stash", "list"), "")

    def test_equal_remote_sha_is_noop_with_dirty_index(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, _writer = remote_fixture(directory)
            (primary / "tracked.txt").write_text("local\n")
            git(primary, "add", "tracked.txt")
            (primary / "untracked.txt").write_text("untracked\n")
            head = git(primary, "rev-parse", "HEAD")
            index = git(primary, "ls-files", "--stage", "--", "tracked.txt")

            with mock.patch("loop_manager._base_worktree_snapshot") as snapshot:
                result = loop_manager.synchronize_base_worktree(primary, "main")

            self.assertEqual(result.status, "equal")
            snapshot.assert_not_called()
            self.assertEqual(git(primary, "rev-parse", "HEAD"), head)
            self.assertEqual(git(primary, "ls-files", "--stage", "--", "tracked.txt"), index)
            self.assertEqual((primary / "tracked.txt").read_text(), "local\n")
            self.assertEqual((primary / "untracked.txt").read_text(), "untracked\n")

    def test_safe_fast_forward_rejects_nonstandard_index_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            git(primary, "update-index", "--assume-unchanged", "tracked.txt")
            remote_sha = advance_remote(writer, path="remote.txt")
            git(primary, "fetch", "origin")
            before_index = git(primary, "ls-files", "--stage", "-v")
            before_head = git(primary, "rev-parse", "HEAD")

            result = loop_manager.synchronize_base_worktree(
                primary, "main", remote_sha=remote_sha
            )

            self.assertEqual(result.status, "skipped")
            self.assertIn("index", result.reason)
            self.assertEqual(git(primary, "rev-parse", "HEAD"), before_head)
            self.assertEqual(git(primary, "ls-files", "--stage", "-v"), before_index)

    def test_safe_fast_forward_rejects_remote_path_collisions(self):
        scenarios = (
            "staged", "unstaged", "untracked", "ignored", "parent",
            "rename-old", "rename-new", "local-rename", "deletion",
            "local-deletion", "directory-transition", "case",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                _remote, primary, writer = remote_fixture(directory)
                if scenario == "staged":
                    (primary / "tracked.txt").write_text("local\n")
                    git(primary, "add", "tracked.txt")
                    remote_sha = advance_remote(writer)
                elif scenario == "unstaged":
                    (primary / "tracked.txt").write_text("local\n")
                    remote_sha = advance_remote(writer)
                elif scenario == "untracked":
                    (primary / "new.txt").write_text("local\n")
                    remote_sha = advance_remote(writer, path="new.txt")
                elif scenario == "ignored":
                    local = primary / "ignored/local.txt"
                    local.parent.mkdir()
                    local.write_text("local\n")
                    remote_sha = advance_remote(writer, path="ignored/local.txt")
                elif scenario == "parent":
                    (primary / "folder").write_text("local\n")
                    remote_sha = advance_remote(writer, path="folder/child.txt")
                elif scenario in {"rename-old", "rename-new"}:
                    if scenario == "rename-old":
                        (primary / "tracked.txt").write_text("local\n")
                    else:
                        (primary / "renamed.txt").write_text("local\n")
                    git(writer, "mv", "tracked.txt", "renamed.txt")
                    remote_sha = commit_all(writer, "Rename tracked file")
                    git(writer, "push", "origin", "main")
                elif scenario == "local-rename":
                    git(primary, "mv", "tracked.txt", "local-renamed.txt")
                    remote_sha = advance_remote(writer)
                elif scenario == "deletion":
                    (primary / "tracked.txt").write_text("local\n")
                    git(writer, "rm", "tracked.txt")
                    remote_sha = commit_all(writer, "Delete tracked file")
                    git(writer, "push", "origin", "main")
                elif scenario == "local-deletion":
                    git(primary, "rm", "tracked.txt")
                    remote_sha = advance_remote(writer)
                elif scenario == "directory-transition":
                    (primary / "tracked.txt").write_text("local\n")
                    git(writer, "rm", "tracked.txt")
                    nested = writer / "tracked.txt/child.txt"
                    nested.parent.mkdir()
                    nested.write_text("remote\n")
                    remote_sha = commit_all(writer, "Turn tracked file into directory")
                    git(writer, "push", "origin", "main")
                else:
                    (primary / "case.txt").write_text("local\n")
                    remote_sha = advance_remote(writer, path="CASE.txt")
                git(primary, "fetch", "origin")
                before_head = git(primary, "rev-parse", "HEAD")
                before_index = git(primary, "ls-files", "--stage", "-v")
                before_files = {
                    path: path.read_bytes() for path in primary.rglob("*")
                    if path.is_file() and ".git" not in path.relative_to(primary).parts
                }

                result = loop_manager.synchronize_base_worktree(
                    primary, "main", remote_sha=remote_sha
                )

                self.assertEqual(result.status, "skipped", result.reason)
                self.assertIn("겹치는", result.reason)
                self.assertEqual(git(primary, "rev-parse", "HEAD"), before_head)
                self.assertEqual(git(primary, "ls-files", "--stage", "-v"), before_index)
                self.assertEqual({
                    path: path.read_bytes() for path in primary.rglob("*")
                    if path.is_file() and ".git" not in path.relative_to(primary).parts
                }, before_files)

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
                    else advance_remote(
                        writer,
                        path=("staged.txt" if scenario == "staged" else
                              "untracked.txt" if scenario == "untracked" else
                              "tracked.txt"),
                    )
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
            before_index = git(primary, "ls-files", "--stage", "-v")
            before_file = (primary / "tracked.txt").read_text()
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
            self.assertEqual(git(primary, "ls-files", "--stage", "-v"), before_index)
            self.assertEqual((primary / "tracked.txt").read_text(), before_file)

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

    def test_concurrent_index_change_after_eligibility_skips_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            remote_sha = advance_remote(writer, path="remote.txt")
            git(primary, "fetch", "origin")
            before_head = git(primary, "rev-parse", "HEAD")
            real_snapshot = loop_manager._base_worktree_snapshot
            calls = 0

            def changing_snapshot(repo: Path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    (repo / "staged.txt").write_text("local\n")
                    git(repo, "add", "staged.txt")
                return real_snapshot(repo)

            with mock.patch("loop_manager._base_worktree_snapshot", side_effect=changing_snapshot):
                result = loop_manager.synchronize_base_worktree(
                    primary, "main", remote_sha=remote_sha
                )

            self.assertEqual(result.status, "skipped")
            self.assertIn("상태가 변경", result.reason)
            self.assertEqual(git(primary, "rev-parse", "HEAD"), before_head)
            self.assertEqual(git(primary, "ls-files", "--stage", "--", "staged.txt").count("staged.txt"), 1)

    def test_concurrent_ignored_content_change_skips_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            ignored = primary / "ignored/local.txt"
            ignored.parent.mkdir()
            ignored.write_text("original\n")
            remote_sha = advance_remote(writer, path="remote.txt")
            git(primary, "fetch", "origin")
            before_head = git(primary, "rev-parse", "HEAD")
            real_snapshot = loop_manager._base_worktree_snapshot
            calls = 0

            def changing_snapshot(repo: Path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    ignored.write_text("changed\n")
                return real_snapshot(repo)

            with mock.patch("loop_manager._base_worktree_snapshot", side_effect=changing_snapshot):
                result = loop_manager.synchronize_base_worktree(
                    primary, "main", remote_sha=remote_sha
                )

            self.assertEqual(result.status, "skipped")
            self.assertIn("상태가 변경", result.reason)
            self.assertEqual(git(primary, "rev-parse", "HEAD"), before_head)
            self.assertEqual(ignored.read_text(), "changed\n")

    def test_remote_ref_change_after_eligibility_skips_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, primary, writer = remote_fixture(directory)
            before_head = git(primary, "rev-parse", "HEAD")
            remote_sha = advance_remote(writer, path="remote.txt")
            git(primary, "fetch", "origin")
            real_snapshot = loop_manager._base_worktree_snapshot
            calls = 0

            def changing_ref(repo: Path):
                nonlocal calls
                calls += 1
                result = real_snapshot(repo)
                if calls == 2:
                    git(repo, "update-ref", "refs/remotes/origin/main", before_head)
                return result

            with mock.patch("loop_manager._base_worktree_snapshot", side_effect=changing_ref):
                result = loop_manager.synchronize_base_worktree(
                    primary, "main", remote_sha=remote_sha
                )

            self.assertEqual(result.status, "skipped")
            self.assertIn("origin ref", result.reason)
            self.assertEqual(git(primary, "rev-parse", "HEAD"), before_head)
            self.assertFalse((primary / "remote.txt").exists())

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
        def locked(_state_dir: Path, *, identity: str):
            self.assertRegex(identity, r"^bootstrap:[0-9a-f]{32}$")
            events.append("lock")
            with tempfile.TemporaryFile() as handle:
                try:
                    yield handle
                finally:
                    events.append("unlock")

        def prepare(actual_source: Path, actual_state: Path, *, mutation_id: str):
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
        secured = loop_manager.runtime_child_arguments(
            ["--runtime-mutation-id", "spoofed", "run"],
            source=source, state_dir=state_dir,
            config_path=source / "custom/config.ini",
            runtime=loop_manager.RuntimeWorktree(runtime.path, runtime.commit_sha, "f" * 32),
        )
        self.assertEqual(secured[secured.index("--runtime-mutation-id") + 1], "f" * 32)
        self.assertNotIn("spoofed", secured)

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
