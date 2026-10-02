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


class BootstrapRuntimeTests(unittest.TestCase):
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
                runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(
                    source, state, mutation_id="a" * 32, commit_sha=sha)
                remove = loop_manager.remove_runtime_worktree
                def checked_remove(*args):
                    self.assertFalse((runtime.path / "__pycache__").exists())
                    self.assertFalse(loop_manager._ignored_worktree_paths(runtime.path))
                    remove(*args)
                with mock.patch.object(bootstrap.BootstrapService, "remove_runtime_worktree", side_effect=checked_remove) as cleanup:
                    self.assertEqual(loop_manager._bootstrap_service().run_runtime_child(
                        source, state, source / "config.ini", runtime, []), 7)
                    cleanup.assert_called_once()
                self.assertFalse(runtime.path.exists())
                self.assertEqual(loop_manager._bootstrap_service()._runtime_transition_state(source, runtime.path),
                                 {"exists": False, "registration": None})
                self.assertFalse(list((state / "manager-git-journal").glob("*/active.json")))


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
                            loop_manager._bootstrap_service().prepare_runtime_worktree(
                                source, state_dir, mutation_id="a" * 32,
                                commit_sha=selected,
                            )
                    recovered = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id="b" * 32,
                        commit_sha=selected,
                    )
                    loop_manager._bootstrap_service().validate_runtime_worktree(
                        source, state_dir, recovered.path, selected,
                    )
            self.assertTrue(recovered.path.is_dir())


    def test_runtime_removal_recovers_receipt_and_blocks_unproven_absence(self):
        for receipt in (True, False):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as directory:
                _remote, source, _writer = remote_fixture(directory)
                state = Path(directory) / "state"
                sha = git(source, "rev-parse", "HEAD")
                runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(
                    source, state, mutation_id="a" * 32, commit_sha=sha)
                with mock.patch.object(manager_git_journal, "_finish", side_effect=RuntimeError("crash")):
                    with self.assertRaisesRegex(RuntimeError, "crash"):
                        loop_manager._bootstrap_service().remove_runtime_worktree(source, state, runtime)
                self.assertFalse(runtime.path.exists())
                if not receipt:
                    for path in (state / "runtime-removals").glob("*.json"):
                        path.unlink()
                    with self.assertRaises(manager_git_journal.MutationEvidenceError):
                        loop_manager._bootstrap_service().prepare_runtime_worktree(
                            source, state, mutation_id="b" * 32, commit_sha=sha)
                else:
                    with mock.patch.object(loop_manager, "git", wraps=loop_manager.git) as calls:
                        loop_manager._bootstrap_service().remove_runtime_worktree(source, state, runtime)
                    self.assertFalse(any(call.args[1:3] == ("worktree", "remove")
                                         for call in calls.call_args_list))
                    self.assertFalse(list((state / "manager-git-journal").glob("*/active.json")))


    def test_runtime_child_cleanup_preserves_dirty_and_ignored_files(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as directory:
                _remote, source, _writer = remote_fixture(directory)
                state = Path(directory) / "state"
                runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(
                    source, state, mutation_id="a" * 32,
                    commit_sha=git(source, "rev-parse", "HEAD"))
                if dirty:
                    (runtime.path / "tracked.txt").write_text("user")
                else:
                    (runtime.path / "ignored").mkdir()
                    (runtime.path / "ignored/user.txt").write_text("user")
                with self.assertRaises(loop_manager.WorkspaceOwnershipError):
                    loop_manager._bootstrap_service().remove_runtime_worktree(source, state, runtime)
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
            bootstrap.BootstrapService, "remove_runtime_worktree", side_effect=cleanup,
        ) as remove:
            result = loop_manager._bootstrap_service().run_runtime_child(
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
            runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(
                source, state, mutation_id="a" * 32, commit_sha=sha)
            original_git = loop_manager.git
            def interrupted(repo, *args, **kwargs):
                result = original_git(repo, *args, **kwargs)
                if args[:2] == ("worktree", "unlock"):
                    raise RuntimeError("crash after unlock")
                return result
            with mock.patch.object(loop_manager, "git", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "crash after unlock"):
                    loop_manager._bootstrap_service().remove_runtime_worktree(source, state, runtime)
            with self.assertRaises(manager_git_journal.MutationEvidenceError):
                loop_manager._bootstrap_service().prepare_runtime_worktree(
                    source, state, mutation_id="b" * 32, commit_sha=sha)
            self.assertTrue(runtime.path.exists())


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
                    first = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id="a" * 32,
                    )
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "b" * 32) as handle:
                    reused = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id="b" * 32,
                    )
                    self.assertEqual(reused.path, first.path)
                    loop_manager._write_process_lock_identity(handle, "runtime:" + "b" * 32)
                    with mock.patch.object(loop_manager, "ROOT", reused.path):
                        identity = loop_manager._bootstrap_service().validate_runtime_mutation(
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
                    runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(source, state_dir)
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "e" * 32):
                    with self.assertRaisesRegex(
                        loop_manager.WorkspaceOwnershipError, "구버전 전환 소유 증거",
                    ):
                        loop_manager._bootstrap_service().prepare_runtime_worktree(
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
                    runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id=mutation_id,
                    )
                    git(source, "worktree", "unlock", str(runtime.path))
                    git(source, "worktree", "lock", "--reason=outside", str(runtime.path))
                    loop_manager._write_process_lock_identity(handle, f"runtime:{mutation_id}")
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        with self.assertRaisesRegex(RuntimeError, "durable mutation 표식"):
                            loop_manager._bootstrap_service().validate_runtime_mutation(
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
                    reused_legacy = loop_manager._bootstrap_service().prepare_runtime_worktree(source, state_dir)
                    self.assertEqual(reused_legacy.path, runtime.path)
                    with mock.patch.object(loop_manager, "ROOT", reused_legacy.path):
                        self.assertTrue(loop_manager._bootstrap_service().validate_runtime_mutation(
                            source, state_dir, selected_sha, None,
                        ).legacy)

                with loop_manager.process_lock(state_dir) as handle:
                    loop_manager._write_process_lock_identity(handle, "runtime:" + "c" * 32)
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        with self.assertRaisesRegex(RuntimeError, "mutation ID가 없는 새 형식"):
                            loop_manager._bootstrap_service().validate_runtime_mutation(
                                source, state_dir, selected_sha, None,
                            )
                with loop_manager.process_lock(state_dir, identity="bootstrap:" + "d" * 32) as handle:
                    adopted = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id="d" * 32,
                    )
                    self.assertEqual(adopted.path, runtime.path)
                    loop_manager._write_process_lock_identity(handle, "runtime:" + "d" * 32)
                    with mock.patch.object(loop_manager, "ROOT", adopted.path):
                        self.assertFalse(loop_manager._bootstrap_service().validate_runtime_mutation(
                            source, state_dir, selected_sha, "d" * 32,
                        ).legacy)
            connection.close()


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
                first = loop_manager._bootstrap_service().prepare_runtime_worktree(source, state_dir)
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
                second = loop_manager._bootstrap_service().prepare_runtime_worktree(source, state_dir)
            self.assertEqual(second.commit_sha, next_sha)
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(git(first.path, "rev-parse", "HEAD"), first.commit_sha)
            self.assertEqual((second.path / "loop_manager.py").read_text(), "VALUE = 'origin-two'\n")

            (second.path / "loop_manager.py").write_text("dirty\n", encoding="utf-8")
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with self.assertRaisesRegex(loop_manager.WorkspaceOwnershipError, "clean"):
                    loop_manager._bootstrap_service().prepare_runtime_worktree(source, state_dir)
            self.assertEqual((second.path / "loop_manager.py").read_text(), "dirty\n")

            git(second.path, "checkout", "--", "loop_manager.py")
            git(second.path, "checkout", "-b", "runtime-topic")
            with mock.patch("loop_manager.github_repository", return_value="owner/repo"):
                with self.assertRaisesRegex(
                    loop_manager.WorkspaceOwnershipError,
                    "detached HEAD",
                ):
                    loop_manager._bootstrap_service().prepare_runtime_worktree(source, state_dir)
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
                    loop_manager._bootstrap_service().prepare_runtime_worktree(source, state_dir)

            self.assertEqual(tuple(outside.iterdir()), ())
            self.assertNotIn(
                str(outside),
                git(source, "worktree", "list", "--porcelain"),
            )
