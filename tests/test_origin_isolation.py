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
import manager_git_journal
import agent_invocation_state
from agent_invocation import InvocationContext, ResolvedProfile, Usage
from agent_write_isolation import IsolationEvidence, inspect_worktree


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
    def test_self_runtime_identity_rejects_alias_and_other_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, other_clone = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            runtime_path = state_dir / "runtime"
            selected = git(source, "rev-parse", "HEAD")
            git(source, "worktree", "add", "--detach", str(runtime_path), selected)
            with sqlite3.connect(state_dir / "loop-manager.db") as connection:
                connection.execute(loop_manager.SCHEMA)
                connection.execute(
                    "INSERT INTO projects (repo, base_branch, origin) VALUES (?, ?, ?)",
                    (str(source), "develop", "owner/repo"),
                )
            runtime = loop_manager.RuntimeMutation(
                source, runtime_path, selected, "a" * 32,
            )
            alias = Path(directory) / "source-alias"
            alias.symlink_to(source, target_is_directory=True)
            project = lambda path, origin="owner/repo": loop_manager.project_registry.RegisteredProject(
                str(path), "develop", "codex", None, False, origin,
            )
            with mock.patch.object(loop_manager, "ROOT", runtime_path), mock.patch(
                "loop_manager.github_repository", return_value="owner/repo"
            ):
                self.assertTrue(
                    loop_manager._registered_self_runtime_project(
                        project(source), runtime, state_dir,
                    )
                )
                for path in (other_clone, alias, source / "subdir", runtime_path):
                    self.assertFalse(
                        loop_manager._registered_self_runtime_project(
                            project(path), runtime, state_dir,
                        )
                    )
                with self.assertRaisesRegex(RuntimeError, "origin"):
                    loop_manager._registered_self_runtime_project(
                        project(source, "another/repo"), runtime, state_dir,
                    )
                self.assertEqual(
                    loop_manager.bootstrap_base_branch(
                        source, state_dir, "owner/repo",
                    ),
                    "develop",
                )
                with sqlite3.connect(state_dir / "loop-manager.db") as connection:
                    connection.execute(
                        "UPDATE projects SET repo = ?", (str(alias),)
                    )
                self.assertEqual(
                    loop_manager.bootstrap_base_branch(
                        source, state_dir, "owner/repo",
                    ),
                    "main",
                )

    def test_project_fetch_recovers_owned_ref_update_after_result_write_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            original_oid = git(source, "rev-parse", "origin/main")
            selected = advance_remote(writer)
            git(writer, "checkout", "-b", "z-extra")
            git(writer, "push", "origin", "z-extra")
            git(writer, "checkout", "main")
            with mock.patch.object(manager_git_journal, "_finish",
                                   side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    loop_manager.remote_base(source, "main", state_dir=state_dir)
            self.assertEqual(git(source, "rev-parse", "origin/main"), selected)
            with mock.patch.object(loop_manager, "git", wraps=loop_manager.git) as calls:
                self.assertEqual(
                    loop_manager.remote_base(source, "main", state_dir=state_dir), "main",
                )
            self.assertEqual(git(source, "rev-parse", "origin/z-extra"), selected)
            self.assertFalse(any(call.args[1:3] == ("update-ref", "--create-reflog")
                                 and call.args[5] == "refs/remotes/origin/main"
                                 for call in calls.call_args_list))
            self.assertEqual(
                manager_git_journal.verified_candidates(
                    state_dir, str(source),
                    baseline_refs={"refs/remotes/origin/main": original_oid},
                    baseline_worktrees={},
                    live_refs={"refs/remotes/origin/main": selected},
                    live_worktrees={},
                    marker=lambda intent: (
                        f"loop-manager-base-fetch:{intent['id']}"
                        in git(source, "reflog", "show", str(intent["target"]))
                    ),
                )["refs"][
                    "refs/remotes/origin/main"], selected,
            )
            self.assertEqual(
                manager_git_journal.verified_candidates(
                    state_dir, str(source),
                    baseline_refs={"refs/remotes/origin/z-extra": None},
                    baseline_worktrees={},
                    live_refs={"refs/remotes/origin/z-extra": selected},
                    live_worktrees={},
                    marker=lambda intent: (
                        f"loop-manager-base-fetch:{intent['id']}"
                        in git(source, "reflog", "show", str(intent["target"]))
                    ),
                )["refs"]["refs/remotes/origin/z-extra"], selected,
            )

    def test_guidance_fetch_resumes_sqlite_and_common_journal_after_git_write(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            before = loop_manager.capture_git_snapshot(source)
            selected = advance_remote(writer)
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.MANAGER_BASE_FETCH_SCHEMA)
            intent_id = loop_manager._record_manager_base_fetch_intent(
                connection, source, str(source), "G-001", "main", before,
            )
            with mock.patch.object(manager_git_journal, "_finish",
                                   side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    loop_manager._journaled_manager_base_fetch(
                        connection, source, state_dir, intent_id, "main", "G-001",
                    )
            self.assertEqual(git(source, "rev-parse", "origin/main"), selected)
            self.assertEqual(
                loop_manager._record_manager_base_fetch_intent(
                    connection, source, str(source), "G-001", "main", before,
                ), intent_id,
            )
            loop_manager._journaled_manager_base_fetch(
                connection, source, state_dir, intent_id, "main", "G-001",
            )
            self.assertEqual(connection.execute(
                "SELECT phase FROM manager_base_fetches WHERE id = ?", (intent_id,),
            ).fetchone()[0], "complete")
            connection.close()

    def test_guidance_fetch_rejects_external_round_trip_after_journal_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state = Path(directory) / "state"
            before = loop_manager.capture_git_snapshot(source)
            old = git(source, "rev-parse", "origin/main")
            selected = advance_remote(writer)
            database = Path(directory) / "guidance.sqlite"
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.MANAGER_BASE_FETCH_SCHEMA)
            identity = loop_manager._record_manager_base_fetch_intent(
                connection, source, str(source), "G-001", "main", before)
            with mock.patch.object(manager_git_journal, "_finish", side_effect=RuntimeError("crash")):
                with self.assertRaisesRegex(RuntimeError, "crash"):
                    loop_manager._journaled_manager_base_fetch(
                        connection, source, state, identity, "main", "G-001")
            connection.close()
            git(source, "update-ref", "-m", "external away", "refs/remotes/origin/main", old)
            git(source, "update-ref", "-m", "external back", "refs/remotes/origin/main", selected)
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                with self.assertRaises(manager_git_journal.MutationEvidenceError):
                    loop_manager._journaled_manager_base_fetch(
                        connection, source, state, identity, "main", "G-001")
                self.assertEqual(connection.execute(
                    "SELECT phase FROM manager_base_fetches WHERE id = ?", (identity,),
                ).fetchone()[0], "pending")
                self.assertTrue(list((state / "manager-git-journal").glob("*/active.json")))
                self.assertEqual(git(source, "rev-parse", "origin/main"), selected)
            finally:
                connection.close()

    def test_guidance_fetch_history_requires_uninterrupted_owned_suffix(self):
        for external in (False, True):
            with self.subTest(external=external), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                connection = sqlite3.connect(":memory:")
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.MANAGER_BASE_FETCH_SCHEMA)
                try:
                    before = loop_manager.capture_git_snapshot(source)
                    old = git(source, "rev-parse", "origin/main")
                    first_sha = advance_remote(writer)
                    first = loop_manager._record_manager_base_fetch_intent(
                        connection, source, str(source), "G-001", "main", before)
                    loop_manager._perform_manager_base_fetch(connection, source, first, "main")
                    intermediate = loop_manager.capture_git_snapshot(source)
                    advance_remote(writer, value="third")
                    second = loop_manager._record_manager_base_fetch_intent(
                        connection, source, str(source), "G-001", "main", intermediate)
                    if external:
                        git(source, "update-ref", "-m", "external away", "refs/remotes/origin/main", old)
                        git(source, "update-ref", "-m", "external back", "refs/remotes/origin/main", first_sha)
                    loop_manager._perform_manager_base_fetch(connection, source, second, "main")
                    loop_manager._finish_manager_base_fetch(connection, source, second)
                    rows = connection.execute("SELECT * FROM manager_base_fetches ORDER BY id").fetchall()
                    self.assertEqual(loop_manager._manager_base_fetch_applied(
                        source, rows[0], rows), not external)
                    self.assertFalse(loop_manager._manager_base_fetch_applied(source, rows[0]))
                    self.assertTrue(loop_manager._manager_base_fetch_applied(source, rows[1]))
                finally:
                    connection.close()

    def test_runtime_imports_local_module_without_bytecode_and_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, _writer = remote_fixture(directory)
            (source / ".gitignore").write_text("__pycache__/\n")
            (source / "runtime_helper.py").write_text("VALUE = 7\n")
            (source / "loop_manager.py").write_text(
                "import runtime_helper\nraise SystemExit(runtime_helper.VALUE)\n")
            sha = commit_all(source, "Runtime import fixture")
            state = Path(directory) / "state"
            with loop_manager.process_lock(state):
                runtime = loop_manager.prepare_runtime_worktree(
                    source, state, mutation_id="a" * 32, commit_sha=sha)
                remove = loop_manager.remove_runtime_worktree
                def checked_remove(*args):
                    self.assertFalse((runtime.path / "__pycache__").exists())
                    self.assertFalse(loop_manager._ignored_worktree_paths(runtime.path))
                    remove(*args)
                with mock.patch.object(loop_manager, "remove_runtime_worktree", side_effect=checked_remove) as cleanup:
                    self.assertEqual(loop_manager.run_runtime_child(
                        source, state, source / "config.ini", runtime, []), 7)
                    cleanup.assert_called_once()
                self.assertFalse(runtime.path.exists())
                self.assertEqual(loop_manager._runtime_transition_state(source, runtime.path),
                                 {"exists": False, "registration": None})
                self.assertFalse(list((state / "manager-git-journal").glob("*/active.json")))

    def test_source_fast_forward_recovers_without_touching_disjoint_local_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            (source / "local.txt").write_text("user\n", encoding="utf-8")
            (source / "staged.txt").write_text("staged\n", encoding="utf-8")
            git(source, "add", "staged.txt")
            selected = advance_remote(writer)
            loop_manager._recorded_base_fetch(source, state_dir, "main", owner="project")
            with mock.patch.object(manager_git_journal, "_finish",
                                   side_effect=RuntimeError("interrupted")):
                result = loop_manager.synchronize_base_worktree(
                    source, "main", remote_sha=selected, state_dir=state_dir,
                )
            self.assertEqual(result.status, "failed")
            self.assertEqual(git(source, "rev-parse", "HEAD"), selected)
            self.assertEqual((source / "local.txt").read_text(), "user\n")
            self.assertEqual(git(source, "diff", "--cached", "--name-only"), "staged.txt")
            result = loop_manager.synchronize_base_worktree(
                source, "main", remote_sha=selected, state_dir=state_dir,
            )
            self.assertEqual(result.status, "equal")
            self.assertEqual((source / "local.txt").read_text(), "user\n")
            self.assertEqual(git(source, "diff", "--cached", "--name-only"), "staged.txt")

    def test_runtime_create_recovers_owned_registration_after_result_write_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            selected = advance_remote(writer)
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with loop_manager.process_lock(state_dir):
                    loop_manager._recorded_base_fetch(
                        source, state_dir, "main", owner="bootstrap",
                    )
                    with mock.patch.object(manager_git_journal, "_finish",
                                           side_effect=RuntimeError("interrupted")):
                        with self.assertRaisesRegex(RuntimeError, "interrupted"):
                            loop_manager.prepare_runtime_worktree(
                                source, state_dir, mutation_id="a" * 32,
                                commit_sha=selected,
                            )
                    recovered = loop_manager.prepare_runtime_worktree(
                        source, state_dir, mutation_id="b" * 32,
                        commit_sha=selected,
                    )
                    loop_manager.validate_runtime_worktree(
                        source, state_dir, recovered.path, selected,
                    )
            self.assertTrue(recovered.path.is_dir())

    def test_interrupted_fetch_rejects_external_ref_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state = Path(directory) / "state"
            old = git(source, "rev-parse", "HEAD")
            selected = advance_remote(writer)
            with mock.patch.object(manager_git_journal, "_finish", side_effect=RuntimeError("crash")):
                with self.assertRaises(RuntimeError):
                    loop_manager._recorded_base_fetch(source, state, "main", owner="project")
            git(source, "update-ref", "-m", "external away", "refs/remotes/origin/main", old)
            git(source, "update-ref", "-m", "external back", "refs/remotes/origin/main", selected)
            with self.assertRaises(manager_git_journal.MutationEvidenceError):
                loop_manager._recorded_base_fetch(source, state, "main", owner="project")
            self.assertTrue(list((state / "manager-git-journal").glob("*/active.json")))

    def test_interrupted_source_rejects_ref_round_trip_and_index_changes(self):
        for mutation in ("ref", "head", "blob", "flag"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                state = Path(directory) / "state"
                old = git(source, "rev-parse", "HEAD")
                (source / "staged.txt").write_text("first")
                git(source, "add", "staged.txt")
                (source / "staged.txt").write_text("working")
                selected = advance_remote(writer)
                loop_manager._recorded_base_fetch(source, state, "main", owner="project")
                with mock.patch.object(manager_git_journal, "_finish", side_effect=RuntimeError("crash")):
                    self.assertEqual(loop_manager.synchronize_base_worktree(
                        source, "main", state_dir=state).status, "failed")
                if mutation == "ref":
                    git(source, "update-ref", "-m", "external away", "refs/heads/main", old)
                    git(source, "update-ref", "-m", "external back", "refs/heads/main", selected)
                elif mutation == "head":
                    git(source, "checkout", "--detach", selected)
                    git(source, "checkout", "main")
                elif mutation == "blob":
                    (source / "staged.txt").write_text("second")
                    git(source, "add", "staged.txt")
                    (source / "staged.txt").write_text("working")
                else:
                    git(source, "update-index", "--assume-unchanged", "staged.txt")
                observed = loop_manager._source_transition_state(source)
                self.assertEqual(loop_manager.synchronize_base_worktree(
                    source, "main", state_dir=state).status, "failed")
                self.assertEqual(loop_manager._source_transition_state(source), observed)
                self.assertTrue(list((state / "manager-git-journal").glob("*/active.json")))

    def test_runtime_removal_recovers_receipt_and_blocks_unproven_absence(self):
        for receipt in (True, False):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as directory:
                _remote, source, _writer = remote_fixture(directory)
                state = Path(directory) / "state"
                sha = git(source, "rev-parse", "HEAD")
                runtime = loop_manager.prepare_runtime_worktree(
                    source, state, mutation_id="a" * 32, commit_sha=sha)
                with mock.patch.object(manager_git_journal, "_finish", side_effect=RuntimeError("crash")):
                    with self.assertRaisesRegex(RuntimeError, "crash"):
                        loop_manager.remove_runtime_worktree(source, state, runtime)
                self.assertFalse(runtime.path.exists())
                if not receipt:
                    for path in (state / "runtime-removals").glob("*.json"):
                        path.unlink()
                    with self.assertRaises(manager_git_journal.MutationEvidenceError):
                        loop_manager.prepare_runtime_worktree(
                            source, state, mutation_id="b" * 32, commit_sha=sha)
                else:
                    with mock.patch.object(loop_manager, "git", wraps=loop_manager.git) as calls:
                        loop_manager.remove_runtime_worktree(source, state, runtime)
                    self.assertFalse(any(call.args[1:3] == ("worktree", "remove")
                                         for call in calls.call_args_list))
                    self.assertFalse(list((state / "manager-git-journal").glob("*/active.json")))

    def test_runtime_child_cleanup_preserves_dirty_and_ignored_files(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as directory:
                _remote, source, _writer = remote_fixture(directory)
                state = Path(directory) / "state"
                runtime = loop_manager.prepare_runtime_worktree(
                    source, state, mutation_id="a" * 32,
                    commit_sha=git(source, "rev-parse", "HEAD"))
                if dirty:
                    (runtime.path / "tracked.txt").write_text("user")
                else:
                    (runtime.path / "ignored").mkdir()
                    (runtime.path / "ignored/user.txt").write_text("user")
                with self.assertRaises(loop_manager.WorkspaceOwnershipError):
                    loop_manager.remove_runtime_worktree(source, state, runtime)
                self.assertTrue(runtime.path.exists())

    def test_runtime_child_removes_only_after_exit_and_preserves_exit_code(self):
        runtime = loop_manager.RuntimeWorktree(Path("/runtime"), "a" * 40, "b" * 32)
        events = []
        def child(*args, **kwargs):
            events.append("exit")
            return subprocess.CompletedProcess(args, 7)
        def cleanup(*args):
            events.append("remove")
        with mock.patch.object(loop_manager.subprocess, "run", side_effect=child), mock.patch.object(
            loop_manager, "remove_runtime_worktree", side_effect=cleanup,
        ) as remove:
            result = loop_manager.run_runtime_child(
                Path("/source"), Path("/state"), Path("/config"), runtime, [],
            )
        self.assertEqual(result, 7)
        self.assertEqual(events, ["exit", "remove"])
        remove.assert_called_once_with(Path("/source"), Path("/state"), runtime)

    def test_runtime_removal_interrupted_after_unlock_blocks_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, _writer = remote_fixture(directory)
            state = Path(directory) / "state"
            sha = git(source, "rev-parse", "HEAD")
            runtime = loop_manager.prepare_runtime_worktree(
                source, state, mutation_id="a" * 32, commit_sha=sha)
            original_git = loop_manager.git
            def interrupted(repo, *args, **kwargs):
                result = original_git(repo, *args, **kwargs)
                if args[:2] == ("worktree", "unlock"):
                    raise RuntimeError("crash after unlock")
                return result
            with mock.patch.object(loop_manager, "git", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "crash after unlock"):
                    loop_manager.remove_runtime_worktree(source, state, runtime)
            with self.assertRaises(manager_git_journal.MutationEvidenceError):
                loop_manager.prepare_runtime_worktree(
                    source, state, mutation_id="b" * 32, commit_sha=sha)
            self.assertTrue(runtime.path.exists())
    def test_unverified_agent_snapshot_keeps_strict_shared_git_boundary(self):
        selected = Path("/managed/selected")
        source = Path("/source")
        other = Path("/managed/other")
        before = loop_manager.GitSnapshot(
            head="a" * 40,
            branch="loop-manager/G-088",
            index={}, dirty={},
            refs={"refs/heads/draft": ("a" * 40, "")},
            worktrees={
                str(source): ("branch=refs/heads/main",),
                str(selected): ("branch=refs/heads/loop-manager/G-088",),
                str(other): ("branch=refs/heads/draft",),
            },
            config={}, operations={},
        )
        cases = (
            (replace(before, refs={"refs/heads/draft": ("b" * 40, "")}),
             loop_manager.BlockReasonCode.SHARED_REF_MUTATION),
            (replace(before, worktrees={**before.worktrees,
                                        str(other): ("branch=refs/heads/changed",)}),
             loop_manager.BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION),
            (replace(before, shared_metadata_complete=False),
             loop_manager.BlockReasonCode.REPOSITORY_STATE_UNSAFE),
        )
        for after, expected_reason in cases:
            with self.subTest(reason=expected_reason):
                with self.assertRaises(loop_manager.AgentScopeError) as caught:
                    loop_manager.validate_agent_scope(
                        "reviewer", selected, before, after,
                        external_worktree=source, base_branch="main",
                    )
                self.assertEqual(caught.exception.reason_code, expected_reason)

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
                    runtime = loop_manager.prepare_bootstrap_runtime(
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
                        loop_manager.prepare_bootstrap_runtime(source, state_dir, "a" * 32),
                        loop_manager.BootstrapRefreshHandoff("develop", latest),
                    )
                self.assertEqual(git(source, "rev-parse", "HEAD"), latest)
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "b" * 32) as handle:
                    runtime = loop_manager.prepare_bootstrap_runtime(
                        source, state_dir, "b" * 32
                    )
                    loop_manager._write_process_lock_identity(handle, "runtime:" + "b" * 32)
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        self.assertEqual(
                            loop_manager.validate_runtime_mutation(
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
                loop_manager.bootstrap_base_branch(source, state_dir, "owner/repo"),
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
                        loop_manager.prepare_bootstrap_runtime(source, state_dir, "a" * 32)
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
                        loop_manager.prepare_bootstrap_runtime(source, state_dir, "c" * 32)
            self.assertFalse((state_dir / "runtimes").exists())

            def raced_sync(repo, base, *, remote_sha, state_dir, owner):
                git(source, "checkout", "-b", "topic")
                return loop_manager.BaseWorktreeSyncResult("equal", "raced", remote_sha)

            with mock.patch("loop_manager.github_repository", return_value="owner/repo"), mock.patch(
                "loop_manager.synchronize_base_worktree", side_effect=raced_sync
            ):
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "b" * 32):
                    with self.assertRaisesRegex(RuntimeError, "bootstrap revision 변경"):
                        loop_manager.prepare_bootstrap_runtime(source, state_dir, "b" * 32)
            self.assertFalse((state_dir / "runtimes").exists())

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


class TaskRelevantInvocationScopeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.remote, self.source, self.writer = remote_fixture(directory.name)
        self.state = Path(directory.name) / 'state'
        self.state.mkdir()
        self.worktree = self.state / 'worktrees' / 'planning'
        self.worktree.parent.mkdir()
        git(self.source, 'worktree', 'add', '--detach', str(self.worktree), 'origin/main')
        self.connection = loop_manager.connect_database(self.state, create=True)
        self.addCleanup(self.connection.close)
        self.repo = str(self.source)
        self.connection.execute("INSERT INTO projects (repo, base_branch, phase, worktree) VALUES (?, 'main', 'PLANNING', ?)",
                                (self.repo, str(self.worktree)))
        self.connection.commit()
        self.before = loop_manager.begin_agent_scope(self.connection, self.repo, self.worktree, 'planner')
        self.ledger = agent_invocation_state.InvocationRepository(self.connection)
        self.evidence = IsolationEvidence('codex', 'codex test', 'Linux:test', 'planner',
            str(self.worktree), inspect_worktree(self.worktree, self.state).repository_identity,
            'a' * 64, 'verified', snapshot_digest=self.before.scope_start_digest,
            configuration_digest='b' * 64)

    def prove(self, *, status='success', role='planner', plan_id=None):
        identity = self.ledger.start(InvocationContext(self.repo, plan_id), role, 'codex',
            ResolvedProfile('model', 'high'), 'session', scope_id=self.before.scope_id)
        self.ledger.record_isolation(identity, self.evidence)
        self.ledger.finish(identity, status, 0 if status == 'success' else 1, 10, 'session', Usage())
        session_key = 'planner_session_id' if role == 'planner' else f'{role}_session_id'
        self.connection.execute(f"UPDATE projects SET {session_key} = 'session' WHERE repo = ?", (self.repo,))
        self.connection.commit()
        return identity

    def validate(self, before=None):
        loop_manager.validate_agent_scope('planner', self.worktree, before or self.before,
            loop_manager.capture_git_snapshot(self.worktree), external_worktree=self.source,
            base_branch='main', connection=self.connection)

    def unrelated_ref(self):
        git(self.source, 'update-ref', 'refs/heads/unrelated', self.before.head)

    def test_current_and_saved_scope_accept_independent_changes_with_exact_invocation_proof(self):
        self.prove()
        self.unrelated_ref()
        git(self.source, 'tag', 'independent')
        other = self.state / 'worktrees' / 'other'
        git(self.source, 'worktree', 'add', '--detach', str(other), self.before.head)
        (other / 'tracked.txt').write_text('independent worktree\n')
        git(other, 'add', 'tracked.txt')
        git(self.source, 'checkout', '-b', 'user-local')
        (self.source / 'tracked.txt').write_text('source worktree\n')
        git(self.source, 'add', 'tracked.txt')
        git(self.source, 'config', 'branch.unrelated.remote', 'other')
        self.validate()
        loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state, self.repo)
        self.assertEqual(loop_manager.GitSnapshot.deserialize(self.connection.execute(
            'SELECT agent_snapshot FROM projects WHERE repo = ?', (self.repo,)).fetchone()[0]), self.before)
        git(self.source, 'worktree', 'remove', '--force', str(other))
        self.validate()

    def test_missing_invalidated_pending_or_mismatched_proof_keeps_strict_comparison(self):
        identity = self.prove()
        self.unrelated_ref()
        cases = [
            ('agent_isolation_evidence', 'status', 'prepared'),
            ('agent_isolation_evidence', 'snapshot_digest', 'c' * 64),
            ('agent_isolation_evidence', 'canonical_worktree', str(self.source)),
            ('agent_isolation_evidence', 'repository_identity', 'c' * 64),
            ('agent_isolation_evidence', 'policy_digest', ''),
            ('agent_isolation_evidence', 'configuration_digest', ''),
            ('agent_isolation_evidence', 'provider', 'claude'),
            ('agent_isolation_evidence', 'role', 'reviewer'),
            ('agent_invocations', 'session_id', 'different'),
            ('agent_invocations', 'scope_id', 'different'),
            ('agent_invocations', 'finished_at', None),
        ]
        for table, key, value in cases:
            original = self.connection.execute(f'SELECT {key} FROM {table} WHERE invocation_id = ?', (identity,)).fetchone()[0]
            self.connection.execute(f'UPDATE {table} SET {key} = ? WHERE invocation_id = ?', (value, identity))
            with self.subTest(table=table, key=key), self.assertRaises(loop_manager.AgentScopeError):
                self.validate()
            self.connection.execute(f'UPDATE {table} SET {key} = ? WHERE invocation_id = ?', (original, identity))
        legacy = replace(self.before, scope_id=None, scope_start_digest=None)
        with self.assertRaises(loop_manager.AgentScopeError):
            self.validate(legacy)
        self.prove(status='transient_failure')
        with self.assertRaises(loop_manager.AgentScopeError):
            self.validate()

    def test_used_base_config_operation_and_out_of_scope_files_still_block(self):
        self.prove()
        (self.worktree / 'unowned.txt').write_text('unexpected')
        with self.assertRaises(loop_manager.AgentScopeError):
            self.validate()
        (self.worktree / 'unowned.txt').unlink()
        git(self.worktree, 'update-index', '--assume-unchanged', 'tracked.txt')
        with self.assertRaises(loop_manager.AgentScopeError):
            self.validate()
        git(self.worktree, 'update-index', '--no-assume-unchanged', 'tracked.txt')
        git(self.source, 'config', 'core.hooksPath', 'unexpected')
        with self.assertRaises(loop_manager.AgentScopeError):
            self.validate()
        git(self.source, 'config', '--unset', 'core.hooksPath')
        operation = Path(git(self.worktree, 'rev-parse', '--git-path', 'MERGE_HEAD'))
        operation.write_text(self.before.head)
        with self.assertRaises(loop_manager.AgentScopeError):
            self.validate()
        operation.unlink()
        new_base = advance_remote(self.writer)
        git(self.source, 'fetch', 'origin')
        with self.assertRaises(loop_manager.AgentScopeError):
            self.validate()
        loop_manager.validate_agent_scope('planner', self.worktree, self.before,
            loop_manager.capture_git_snapshot(self.worktree), external_worktree=self.source,
            base_branch='main', connection=self.connection,
            allowed_ref_changes=frozenset({'refs/remotes/origin/main'}))
        self.assertEqual(git(self.source, 'rev-parse', 'origin/main'), new_base)

    def test_run_agent_binds_the_physical_invocation_to_the_saved_scope(self):
        def run(request):
            request.on_isolation_checked(self.evidence)
            request.on_session_started('session')
            return loop_manager.AgentResult(0, 'done', 'session')
        with mock.patch.object(loop_manager, 'agent_provider') as provider:
            provider.return_value.run.side_effect = run
            result = loop_manager.run_agent('codex', self.worktree, self.state,
                self.state / 'config.ini', 'planner', 'prompt', read_only=False,
                connection=self.connection, invocation=InvocationContext(self.repo, None),
                resolved_profile=ResolvedProfile('model', 'high'),
                on_session_started=lambda session: self.connection.execute(
                    'UPDATE projects SET planner_session_id = ? WHERE repo = ?', (session, self.repo)))
        row = self.connection.execute('SELECT scope_id FROM agent_invocations WHERE invocation_id = ?',
                                      (result.isolation_invocation_id,)).fetchone()
        self.assertEqual(row[0], self.before.scope_id)
        self.unrelated_ref()
        self.validate()

    def test_worker_reviewer_and_maintenance_use_their_owned_resources(self):
        plan_path = 'docs/plans/[G-089] Task-Relevant Agent Git Scope.md'
        branch = 'loop-manager/G-089'
        git(self.worktree, 'checkout', '-b', branch)
        for role, phase in (('worker', 'WORKING'), ('reviewer', 'REVIEWING'),
                            ('maintenance_planner', 'PLANNING')):
            with self.subTest(role=role):
                if role == 'maintenance_planner':
                    git(self.worktree, 'checkout', '--detach')
                self.connection.execute('UPDATE projects SET phase = ?, branch = ?, plan_path = ?, agent_snapshot = NULL WHERE repo = ?',
                    (phase, None if role == 'maintenance_planner' else branch, plan_path, self.repo))
                self.connection.commit()
                self.before = loop_manager.begin_agent_scope(self.connection, self.repo, self.worktree, role)
                provider_role = 'planner' if role == 'maintenance_planner' else role
                self.evidence = replace(self.evidence, role=provider_role,
                                        snapshot_digest=self.before.scope_start_digest)
                self.prove(role=provider_role, plan_id=None if role == 'maintenance_planner' else 'G-089')
                git(self.source, 'tag', f'independent-{role}')
                resources = loop_manager._agent_git_resources(self.connection, self.source,
                    self.worktree, self.before, role, selected_plan_path=plan_path)
                self.assertIsNotNone(resources)
                if role != 'maintenance_planner':
                    self.assertIn(f'refs/heads/{branch}', resources.refs)
                loop_manager.validate_agent_scope(role, self.worktree, self.before,
                    loop_manager.capture_git_snapshot(self.worktree), external_worktree=self.source,
                    base_branch='main', connection=self.connection, selected_plan_path=plan_path)


class ManagerBaselineReconciliationUnitTests(unittest.TestCase):
    """Exercise durable fetch evidence and snapshot projection with fake Git."""
    def setUp(self):
        from types import SimpleNamespace
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.source = Path('/source')
        self.ref = 'refs/remotes/origin/main'
        self.before = loop_manager.GitSnapshot('owned', 'loop-manager/G-092', {},
            {'app.py': (('unstaged',), 'worker-file')},
            {self.ref: ('a' * 40, '')}, {'/managed': ('HEAD=owned',)}, {}, {})
        self.current = replace(self.before, refs={self.ref: ('b' * 40, '')})
        self.reflog = ''
        def read_git(_repo, *args, **kwargs):
            if args[:2] == ('rev-parse', '--verify') and args[2] == self.ref + '^{commit}':
                return SimpleNamespace(stdout=self.current.refs[self.ref][0], returncode=0)
            if args[:2] != ('reflog', 'show'):
                raise AssertionError('scope reconciliation must not perform Git mutations')
            return SimpleNamespace(stdout=self.reflog, returncode=0)
        patcher = mock.patch.object(loop_manager, 'git', side_effect=read_git)
        self.git = patcher.start()
        self.addCleanup(patcher.stop)

    def record_fetch(self, *, interrupted=False, before_oid='a' * 40, target_oid='b' * 40):
        current = {'oid': before_oid}
        def apply(token):
            current['oid'] = target_oid
            self.reflog = f"{target_oid}\0loop-manager-base-fetch:{token}\n" + self.reflog
        options = dict(owner='project', kind='project_fetch', repo=str(self.source), target=self.ref,
            before={'oid': before_oid}, expected={'oid': target_oid},
            observe=lambda: dict(current), apply=apply, marker=lambda _intent: True)
        if interrupted:
            with mock.patch.object(manager_git_journal, '_finish', side_effect=RuntimeError('interrupted')):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    manager_git_journal.execute(self.state, **options)
        else:
            manager_git_journal.execute(self.state, **options)

    def reconcile(self):
        return loop_manager._reconciled_manager_scope_metadata(
            self.connection, self.source, self.state, str(self.source), 'G-092', self.before, self.current)

    def test_completed_fetch_advances_only_its_ref_and_keeps_worker_changes(self):
        self.record_fetch()
        self.current = replace(self.current, refs={**self.current.refs, 'refs/heads/other': ('foreign', '')})
        effective = self.reconcile()
        self.assertEqual(effective.refs[self.ref], ('b' * 40, ''))
        self.assertNotIn('refs/heads/other', effective.refs)
        self.assertEqual(effective.dirty, self.before.dirty)
        self.assertEqual(self.before.refs[self.ref], ('a' * 40, ''))
        self.assertEqual(self.reconcile(), effective)

    def test_git_effect_before_receipt_save_is_read_only_and_does_not_fabricate_receipt(self):
        self.record_fetch(interrupted=True)
        slot = manager_git_journal._slot(self.state, str(self.source), 'project_fetch', self.ref)
        audit = {path.name: path.read_bytes() for path in slot.iterdir()}
        self.assertEqual(self.reconcile().refs[self.ref], ('b' * 40, ''))
        self.assertEqual({path.name: path.read_bytes() for path in slot.iterdir()}, audit)
        self.assertFalse(list(slot.glob('*.result.json')))

    def test_mismatched_effect_or_external_reflog_cannot_advance_baseline(self):
        self.record_fetch(interrupted=True)
        self.current = replace(self.current, refs={self.ref: ('c' * 40, '')})
        self.assertEqual(self.reconcile().refs, self.before.refs)
        self.current = replace(self.current, refs={self.ref: ('b' * 40, '')})
        self.reflog = f"{'b' * 40}\0external update\n" + self.reflog
        self.assertEqual(self.reconcile().refs, self.before.refs)

    def test_observed_ref_drift_without_fetch_evidence_does_not_advance(self):
        self.assertEqual(self.reconcile(), self.before)
        self.git.assert_not_called()

    def test_missing_old_rebase_journal_does_not_gate_independent_verified_fetch(self):
        self.record_fetch()
        rebase = manager_git_journal._slot(self.state, str(self.source), 'managed_rebase', '/managed')
        rebase.mkdir()
        (rebase / 'old.result.json').write_text('{"status":"complete","id":"missing-intent"}')
        (rebase / 'active.json').write_text('incomplete old audit')
        self.assertEqual(self.reconcile().refs[self.ref], ('b' * 40, ''))

    def test_multiple_fetches_require_an_uninterrupted_owned_reflog_chain(self):
        self.record_fetch()
        self.record_fetch(before_oid='b' * 40, target_oid='c' * 40)
        self.current = replace(self.current, refs={self.ref: ('c' * 40, '')})
        self.assertEqual(self.reconcile().refs[self.ref], ('c' * 40, ''))
        newest, prior = self.reflog.split('\n', 1)
        self.reflog = newest + f"\n{'b' * 40}\0external revert\n" + prior
        with self.assertRaises(manager_git_journal.MutationEvidenceError):
            self.reconcile()

    def test_completed_sqlite_fetch_without_git_action_is_not_execution_proof(self):
        loop_manager._ensure_cleanup_pending_schema(self.connection)
        self.connection.execute('INSERT INTO manager_base_fetches '
            '(repo, plan_id, refname, before_ref, target_oid, action_token, phase) '
            "VALUES (?, 'G-092', ?, ?, ?, ?, 'complete')",
            (str(self.source), self.ref, '["' + 'a' * 40 + '", ""]', 'b' * 40, 'f' * 32))
        self.reflog = f"{'b' * 40}\0external update"
        self.assertEqual(self.reconcile().refs, self.before.refs)

    def test_sqlite_owned_fetch_is_not_rejected_by_alternate_journal_token(self):
        self.record_fetch()
        loop_manager._ensure_cleanup_pending_schema(self.connection)
        self.connection.execute('INSERT INTO manager_base_fetches '
            '(repo, plan_id, refname, before_ref, target_oid, action_token, phase) '
            "VALUES (?, 'G-092', ?, ?, ?, ?, 'complete')",
            (str(self.source), self.ref, '["' + 'a' * 40 + '", ""]', 'b' * 40, 'f' * 32))
        self.reflog = f"{'b' * 40}\0loop-manager-base-fetch:{'f' * 32}\n"
        self.assertEqual(self.reconcile().refs[self.ref], ('b' * 40, ''))

    def test_begin_scope_records_proven_metadata_start_and_retains_original(self):
        self.record_fetch()
        self.prepare_project(Path('/managed'))
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=self.current), \
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=self.state), \
                mock.patch.object(loop_manager, '_agent_git_resources', return_value=None):
            effective = loop_manager.begin_agent_scope(self.connection, str(self.source), Path('/managed'), 'worker')
        self.assertEqual(effective.refs, self.current.refs)
        self.assertEqual(self.connection.execute('SELECT agent_snapshot FROM projects').fetchone()[0],
                         self.before.serialize())
        checkpoint = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(
            str(self.source), 'G-092')
        self.assertEqual(loop_manager.agent_scope_decision.manager_comparison_baseline(
            self.before, self.before, checkpoint.snapshot, 'worker').refs, self.current.refs)

    def test_deferred_merged_cleanup_records_exact_registration_for_scope_refresh(self):
        worktree = self.state / 'G-099'
        worktree.mkdir()
        self.prepare_project(self.state)
        branch = 'loop-manager/G-099'
        registration = ('HEAD=' + 'a' * 40, 'branch=refs/heads/' + branch)
        self.before = replace(self.before, worktrees={**self.before.worktrees, str(worktree): registration})
        self.current = self.before
        self.connection.execute('INSERT INTO exec_plans '
            '(repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, '
            'branch, worktree, workspace_owned, pr_head_oid) '
            "VALUES (?, 'G-099', 'docs/plans/[G-099] Cleanup.md', 'Cleanup', '[]', '[]', 'normal', 'hash', "
            "'MERGED', ?, ?, 1, ?)", (str(self.source), branch, str(worktree), 'a' * 40))
        loop_manager._ensure_cleanup_pending_schema(self.connection)
        loop_manager._store_cleanup_pending(self.connection, str(self.source), worktree,
                                           'pr', 'a' * 40, 'deferred')
        def remove(*_args, **_kwargs):
            worktree.rmdir()
            self.current = replace(self.current, worktrees=self.before.worktrees.copy())
            self.current.worktrees.pop(str(worktree))
            return loop_manager.CleanupAssessment('safe', '')
        with mock.patch.object(loop_manager, '_recover_managed_worktree_removal'), \
                mock.patch.object(loop_manager, 'validate_pending_cleanup_worktree'), \
                mock.patch.object(loop_manager, 'assess_cleanup', return_value=loop_manager.CleanupAssessment('safe', '')), \
                mock.patch.object(loop_manager, 'remove_cleanup_worktree', side_effect=remove), \
                mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current):
            self.assertEqual(loop_manager.recheck_cleanup_pending(
                self.connection, self.source, str(self.source), self.state), [])
        self.assertEqual(loop_manager._proven_merged_cleanup_paths(
            self.connection, str(self.source), self.before, self.current), frozenset({str(worktree)}))
        self.assertEqual(self.reconcile().worktrees, self.current.worktrees)

    def test_persisted_guidance_checkpoint_allows_proven_publication_ref_only(self):
        owned = 'refs/heads/loop-manager/G-092'
        remote = 'refs/remotes/origin/loop-manager/G-092'
        worktree = self.state / 'managed'
        worktree.mkdir()
        self.before = replace(self.before, refs={**self.before.refs, owned: ('owned', '')},
                              worktrees={str(worktree): ('HEAD=owned', 'branch=' + owned)})
        self.current = replace(self.before, refs={**self.before.refs, remote: ('owned', '')})
        self.prepare_project(worktree)
        self.connection.execute('INSERT INTO exec_plans '
            '(repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, '
            'branch, worktree, workspace_owned, blocked_reason) '
            "VALUES (?, 'G-092', 'docs/plans/[G-092] Work.md', 'Work', '[]', '[]', 'normal', 'hash', "
            "'PLAN_BLOCKED', 'loop-manager/G-092', ?, 1, ?)",
            (str(self.source), str(worktree), loop_manager.GUIDANCE_BLOCK_REASON))
        self.connection.execute("UPDATE projects SET phase = 'PLAN_BLOCKED', agent_result_snapshot = ?, "
            'agent_manager_snapshot = ?', (self.before.serialize(),
            loop_manager.ManagerGitCheckpoint(self.before, frozenset()).serialize()))
        loop_manager.blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).request(
            repo=str(self.source), plan_id='G-092', reason_code=loop_manager.GUIDANCE_BLOCK_REASON,
            evidence='guidance', branch='loop-manager/G-092', worktree=str(worktree),
            expected_remote_oid=None, rebase_recovery=False)
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, '_config_entries', return_value={}), \
                mock.patch.object(loop_manager, '_agent_git_resources', return_value=None), \
                mock.patch.object(loop_manager, '_owned_checkpoint_remote_oid', return_value='owned'), \
                mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False), \
                mock.patch.object(loop_manager, 'requirement_archive_candidates', return_value=()):
            loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state, str(self.source))
            self.current = replace(self.current, refs={**self.current.refs, 'refs/tags/foreign': ('foreign', '')})
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state, str(self.source))

    def prepare_project(self, worktree):
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        self.connection.execute('INSERT INTO projects '
            '(repo, base_branch, phase, branch, plan_path, worktree, agent_role, agent_snapshot, worker_session_id) '
            "VALUES (?, 'main', 'WORKING', 'loop-manager/G-092', 'docs/plans/[G-092] Work.md', ?, 'worker', ?, 'saved-worker')",
            (str(self.source), str(worktree), self.before.serialize()))

    def test_refresh_retains_original_snapshot_and_refuses_unproven_mixed_changes(self):
        self.record_fetch()
        worktree = self.state / 'managed'
        worktree.mkdir()
        self.prepare_project(worktree)
        original = self.before.serialize()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, '_agent_git_resources', return_value=None):
            effective = loop_manager.refresh_persisted_agent_scope_after_manager_git(
                self.connection, str(self.source), self.before, frozenset(), state_dir=self.state)
            self.assertEqual(effective.refs[self.ref], ('b' * 40, ''))
            saved = self.connection.execute('SELECT * FROM projects').fetchone()
            self.assertEqual(saved['agent_snapshot'], original)
            self.assertEqual(saved['worker_session_id'], 'saved-worker')
            checkpoint = saved['agent_manager_snapshot']
            self.current = replace(self.current, refs={**self.current.refs, 'refs/heads/foreign': ('foreign', '')})
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager.refresh_persisted_agent_scope_after_manager_git(
                    self.connection, str(self.source), self.before, frozenset({'/foreign'}), state_dir=self.state)
            self.assertEqual(self.connection.execute('SELECT agent_manager_snapshot FROM projects').fetchone()[0], checkpoint)

    def test_project_block_reuses_previous_phase_only_with_invocation_proof(self):
        from types import SimpleNamespace
        self.before = replace(self.before, scope_id='scope', scope_start_digest='digest')
        self.prepare_project(Path('/managed'))
        loop_manager.scoped_blocking.record_project_block(self.connection, str(self.source),
            loop_manager.BlockReasonCode.DEFAULT_WORKTREE_MUTATION, 'unrelated source', 'same',
            plan_id='G-092', previous_phase='WORKING')
        with mock.patch.object(loop_manager, '_git_common_directory', return_value=Path('/common')), \
                mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(stdout='origin', returncode=0)), \
                mock.patch.object(loop_manager, 'validate_managed_worktree'), \
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch', return_value=False), \
                mock.patch.object(agent_invocation_state.InvocationRepository, 'verified_scope', return_value=True) as proof:
            resources = loop_manager._agent_git_resources(
                self.connection, self.source, Path('/managed'), self.before, 'worker',
                selected_plan_path='docs/plans/[G-092] Work.md')
            self.assertIsNotNone(resources)
            self.assertEqual(resources.worktrees, frozenset({'/managed'}))
            self.assertEqual(proof.call_args.kwargs['session_id'], 'saved-worker')
            proof.return_value = False
            self.assertIsNone(loop_manager._agent_git_resources(
                self.connection, self.source, Path('/managed'), self.before, 'worker'))

    def test_recreated_worktree_uses_only_its_create_receipt_and_keeps_rebase_audit_independent(self):
        worktree = Path('/managed/G-092')
        registration = {'head': 'a' * 40, 'branch': 'refs/heads/loop-manager/G-092',
                        'locked': 'loop-manager-worktree:owned'}
        expected = {'exists': True, 'registration': registration, 'index': 'b' * 40}
        observed = {'exists': False, 'registration': None, 'index': None}
        receipt = None
        def apply(token):
            nonlocal receipt
            slot = manager_git_journal._slot(self.state, str(self.source), 'managed_worktree_create', str(worktree))
            intent = manager_git_journal._read(slot / 'active.json')
            receipt = self.state / 'managed-worktree-intents' / f'{token}.create.json'
            receipt.parent.mkdir(exist_ok=True)
            manager_git_journal._write_new(receipt, {key: intent[key]
                for key in ('id', 'repo', 'target', 'before', 'expected')})
            observed.update(expected)
        manager_git_journal.execute(self.state, owner='plan:G-092', kind='managed_worktree_create',
            repo=str(self.source), target=str(worktree), before=dict(observed), expected=expected,
            observe=lambda: dict(observed), apply=apply, marker=lambda *_: True)
        old = ('HEAD=' + 'a' * 40, 'branch=refs/heads/loop-manager/G-092')
        before = replace(self.before, head='a' * 40, worktrees={str(worktree): old})
        current = replace(before, worktrees={str(worktree): (*old, 'locked=' + registration['locked'])})
        rebase = manager_git_journal._slot(self.state, str(self.source), 'managed_rebase', str(worktree))
        rebase.mkdir()
        (rebase / 'active.json').write_text('missing historical rebase audit')
        result = loop_manager._proven_recovered_worktree_baseline(
            before, current, self.state, self.source, 'G-092', worktree)
        self.assertEqual(result, current)
        self.assertEqual(before.worktrees[str(worktree)], old)
        receipt.unlink()
        with self.assertRaises(manager_git_journal.MutationEvidenceError):
            loop_manager._proven_recovered_worktree_baseline(
                before, current, self.state, self.source, 'G-092', worktree)

    def test_saved_rebase_result_and_start_use_current_proven_fetch_chain(self):
        owned = 'refs/heads/loop-manager/G-092'
        self.before = replace(self.before, refs={**self.before.refs, owned: ('owned', '')})
        self.current = replace(self.before, refs={**self.before.refs, self.ref: ('b' * 40, '')})
        self.record_fetch()
        result = self.current
        self.prepare_project(Path('/managed'))
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=result), \
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=self.state), \
                mock.patch.object(loop_manager, '_agent_git_resources', return_value=None):
            loop_manager.begin_agent_scope(self.connection, str(self.source), Path('/managed'), 'worker')
        self.record_fetch(before_oid='b' * 40, target_oid='c' * 40)
        self.current = replace(self.current, refs={**self.current.refs, self.ref: ('c' * 40, '')})
        worktree = Path('/managed')
        self.connection.execute('INSERT INTO exec_plans '
            '(repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, '
            'branch, worktree, workspace_owned, agent_role, agent_snapshot, agent_result_snapshot) '
            "VALUES (?, 'G-092', 'docs/plans/[G-092] Work.md', 'Work', '[]', '[]', 'normal', 'hash', "
            "'PLAN_BLOCKED', 'loop-manager/G-092', '/managed', 1, 'worker', ?, ?)",
            (str(self.source), self.before.serialize(), result.serialize()))
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=self.state), \
                mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False):
            loop_manager.validate_blocked_agent_scope(self.connection, self.source, str(self.source),
                'main', worktree, plan['path'], saved_plan=plan, accepted_rebase=True)
        self.assertEqual(self.connection.execute('SELECT agent_snapshot FROM exec_plans').fetchone()[0],
                         self.before.serialize())


if __name__ == "__main__":
    unittest.main()
