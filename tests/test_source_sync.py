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
from loop_manager_internal.git import source_rebase


class SourceSynchronizationTests(unittest.TestCase):
    def test_multiple_conflicts_preserve_partial_staging_before_first_observation(self):
        for scenario in ("untouched", "staged-markers", "staged-modify-delete"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                (source / "tracked.txt").write_text("local conflict\n")
                (source / "loop_manager.py").write_text("local conflict\n")
                (source / "local.txt").write_text("local addition\n")
                original = commit_all(source, "Two local changes")
                (writer / "tracked.txt").write_text("remote conflict\n")
                if scenario == "staged-modify-delete":
                    (writer / "loop_manager.py").unlink()
                else:
                    (writer / "loop_manager.py").write_text("remote conflict\n")
                (writer / "remote.txt").write_text("remote addition\n")
                remote = commit_all(writer, "Two remote changes")
                git(writer, "push", "origin", "main")
                state = Path(directory) / "state"
                loop_manager._recorded_base_fetch(source, state, "main", owner="project")
                effects = loop_manager._source_sync_effects_synchronize_base_worktree()
                start = source_rebase._observe(effects, source, "main")
                observed = []
                real_git = loop_manager.git
                def stage_before_observation(repo, *args, **kwargs):
                    result = real_git(repo, *args, **kwargs)
                    if "rebase" in args and "--abort" not in args and result.returncode:
                        conflicts = git(repo, "ls-files", "--unmerged")
                        self.assertIn("tracked.txt", conflicts)
                        self.assertIn("loop_manager.py", conflicts)
                        if scenario != "untouched":
                            path = "tracked.txt" if scenario == "staged-markers" else "loop_manager.py"
                            git(repo, "add", path)
                            self.assertEqual(git(repo, "rev-parse", ":" + path),
                                             git(repo, "rev-parse", "AUTO_MERGE:" + path))
                            self.assertTrue(git(repo, "ls-files", "--unmerged"))
                        observed.append(source_rebase._observe(effects, repo, "main"))
                    return result
                with mock.patch("loop_manager.git", side_effect=stage_before_observation) as commands:
                    result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "failed", result.reason)
                self.assertEqual(len(observed), 1)
                self.assertEqual(git(source, "rev-parse", "refs/heads/main"), original)
                current = source_rebase._observe(effects, source, "main")
                aborts = [call for call in commands.call_args_list if "--abort" in call.args]
                if scenario == "untouched":
                    self.assertEqual(len(aborts), 1)
                    self.assertEqual({key: value for key, value in current.items() if key != "head_reflog"},
                                     {key: value for key, value in start.items() if key != "head_reflog"})
                else:
                    self.assertEqual(len(aborts), 0)
                    self.assertEqual(current, observed[0])
                    with mock.patch("loop_manager.git", wraps=loop_manager.git) as retry_commands:
                        retry = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                    self.assertEqual(retry.status, "failed", retry.reason)
                    self.assertEqual(source_rebase._observe(effects, source, "main"), observed[0])
                    self.assertFalse(any("rebase" in call.args[1:] for call in retry_commands.call_args_list))

    def test_conflict_edits_before_first_observation_are_never_aborted(self):
        for scenario in ("conflict-file", "staged-resolution", "replayed-file"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                _remote, source, writer = remote_fixture(directory)
                (source / "local.txt").write_text("local commit\n")
                commit_all(source, "First local commit")
                (source / "tracked.txt").write_text("local conflict\n")
                original = commit_all(source, "Conflicting local commit")
                state = Path(directory) / "state"
                remote = advance_remote(writer)
                loop_manager._recorded_base_fetch(source, state, "main", owner="project")
                real_git = loop_manager.git
                edited_path = source / ("local.txt" if scenario == "replayed-file" else "tracked.txt")
                def edit_before_observation(repo, *args, **kwargs):
                    result = real_git(repo, *args, **kwargs)
                    if "rebase" in args and "--abort" not in args and result.returncode:
                        edited_path.write_text("user edit before receipt\n")
                        if scenario == "staged-resolution":
                            git(repo, "add", edited_path.name)
                    return result
                with mock.patch("loop_manager.git", side_effect=edit_before_observation) as commands:
                    result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "failed")
                self.assertEqual(edited_path.read_text(), "user edit before receipt\n")
                self.assertIn("rebase-merge", loop_manager._git_operation_entries(source))
                self.assertEqual(git(source, "rev-parse", "refs/heads/main"), original)
                self.assertFalse(any("--abort" in call.args for call in commands.call_args_list))
                effects = loop_manager._source_sync_effects_synchronize_base_worktree()
                before = source_rebase._observe(effects, source, "main")
                result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "failed")
                self.assertEqual(source_rebase._observe(effects, source, "main"), before)

    def test_unstarted_source_intents_resume_without_changing_user_work(self):
        for kind, checkpoint in (("source_fast_forward", "active"), ("source_rebase", "active"),
                                 ("source_fast_forward", "started"), ("source_rebase", "started")):
            with self.subTest(kind=kind, checkpoint=checkpoint), tempfile.TemporaryDirectory() as directory:
                if kind == "source_rebase":
                    source, _writer, state, original, remote = self._diverged(directory)
                else:
                    _remote, source, writer = remote_fixture(directory)
                    state = Path(directory) / "state"
                    original = git(source, "rev-parse", "HEAD")
                    remote = advance_remote(writer)
                    loop_manager._recorded_base_fetch(source, state, "main", owner="project")
                    (source / "staged.txt").write_text("staged user work\n")
                    git(source, "add", "staged.txt")
                index = git(source, "ls-files", "--stage", "--", "staged.txt")
                slot = manager_git_journal._slot(state, str(source), kind, str(source))
                write = manager_git_journal._write_new
                def crash_after_intent(path, value):
                    write(path, value)
                    if path.parent == slot and (
                        path.name == "active.json" if checkpoint == "active" else path.name.endswith(".started.json")
                    ):
                        raise RuntimeError("interrupted before Git invocation")
                with mock.patch.object(manager_git_journal, "_write_new", side_effect=crash_after_intent):
                    self.assertEqual(loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state).status, "failed")
                self.assertEqual(git(source, "rev-parse", "HEAD"), original)
                self.assertFalse(loop_manager._git_operation_entries(source))
                result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "updated", result.reason)
                self.assertEqual(git(source, "ls-files", "--stage", "--", "staged.txt"), index)
                if kind == "source_rebase":
                    self.assertEqual((source / "local.txt").read_text(), "local\n")
                else:
                    self.assertEqual(git(source, "rev-parse", "HEAD"), remote)
                    self.assertEqual((source / "staged.txt").read_text(), "staged user work\n")

    def test_conflict_abort_requires_unmodified_git_tree_and_index_evidence(self):
        for scenario in ("missing-tree", "staged-markers", "raw-edit", "converted-checkout"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                source, _writer, state, original, remote = self._diverged(directory, conflict=True)
                if scenario == "converted-checkout":
                    git(source, "config", "core.autocrlf", "true")
                real_git = loop_manager.git
                preserved = []
                def mutate_before_observation(repo, *args, **kwargs):
                    result = real_git(repo, *args, **kwargs)
                    if "rebase" in args and "--abort" not in args and result.returncode:
                        if scenario == "missing-tree":
                            git(repo, "update-ref", "-d", "AUTO_MERGE")
                        elif scenario == "staged-markers":
                            git(repo, "add", "tracked.txt")
                        elif scenario == "raw-edit":
                            content = (repo / "tracked.txt").read_bytes()
                            (repo / "tracked.txt").write_bytes(content.replace(b"\n", b"\r\n"))
                        else:
                            # Canonical bytes now match AUTO_MERGE, but differ
                            # from the CRLF checkout Git originally produced.
                            (repo / "tracked.txt").write_bytes((repo / "tracked.txt").read_bytes().replace(b"\r\n", b"\n"))
                        preserved.append((repo / "tracked.txt").read_bytes())
                    return result
                with mock.patch("loop_manager.git", side_effect=mutate_before_observation) as commands:
                    result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "failed", result.reason)
                self.assertEqual((source / "tracked.txt").read_bytes(), preserved[0])
                self.assertIn("rebase-merge", loop_manager._git_operation_entries(source))
                self.assertEqual(git(source, "rev-parse", "refs/heads/main"), original)
                self.assertFalse(any("--abort" in call.args for call in commands.call_args_list))

    def test_source_sync_retries_after_user_removes_git_lock(self):
        for kind in ("source_fast_forward", "source_rebase"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                if kind == "source_rebase":
                    source, _writer, state, original, remote = self._diverged(directory)
                else:
                    _remote, source, writer = remote_fixture(directory)
                    state = Path(directory) / "state"
                    original = git(source, "rev-parse", "HEAD")
                    remote = advance_remote(writer)
                    loop_manager._recorded_base_fetch(source, state, "main", owner="project")
                lock = Path(git(source, "rev-parse", "--git-path", "index.lock"))
                if not lock.is_absolute(): lock = source / lock
                lock.write_text("user process lock\n")
                self.assertEqual(loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state).status, "failed")
                self.assertEqual(git(source, "rev-parse", "HEAD"), original)
                with mock.patch("loop_manager.git", wraps=loop_manager.git) as commands:
                    repeated = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertIn(repeated.status, {"failed", "skipped"})
                self.assertEqual(lock.read_text(), "user process lock\n")
                self.assertFalse(any("rebase" in call.args[1:] or "merge" in call.args[1:] for call in commands.call_args_list))
                lock.unlink()  # The user releases their lock; the Manager never removes it.
                result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "updated", result.reason)
                self.assertFalse(loop_manager._git_operation_entries(source))
                self.assertEqual((source / "tracked.txt").read_text(), "two\n")

    def test_source_sync_retries_when_lock_is_released_before_failure_observation(self):
        for kind in ("source_fast_forward", "source_rebase"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                if kind == "source_rebase":
                    source, _writer, state, _original, remote = self._diverged(directory)
                else:
                    _remote, source, writer = remote_fixture(directory)
                    state = Path(directory) / "state"
                    remote = advance_remote(writer)
                    loop_manager._recorded_base_fetch(source, state, "main", owner="project")
                lock = Path(git(source, "rev-parse", "--git-path", "index.lock"))
                if not lock.is_absolute(): lock = source / lock
                lock.write_text("user lock\n")
                real_git = loop_manager.git
                def release_after_command(repo, *args, **kwargs):
                    result = real_git(repo, *args, **kwargs)
                    if result.returncode and ("rebase" in args or "merge" in args):
                        lock.unlink()
                    return result
                with mock.patch("loop_manager.git", side_effect=release_after_command):
                    result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "failed", result.reason)
                self.assertFalse(lock.exists())
                result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "updated", result.reason)

    def test_sync_log_distinguishes_remote_revision_and_local_failure(self):
        result = loop_manager.BaseWorktreeSyncResult("failed", "diverged conflict preserved", "a" * 40)
        with mock.patch("loop_manager.operation_log") as log:
            loop_manager.log_base_worktree_sync(Path("/source"), result)
        message = log.call_args.args[0]
        self.assertIn("remote=" + "a" * 40, message)
        self.assertIn("diverged conflict preserved", message)
        self.assertTrue(log.call_args.kwargs["error"])

    def _diverged(self, directory, *, conflict=False):
        _remote, source, writer = remote_fixture(directory)
        path = "tracked.txt" if conflict else "local.txt"
        (source / path).write_text("local\n")
        original = commit_all(source, "Local only")
        state = Path(directory) / "state"
        remote = advance_remote(writer)
        loop_manager._recorded_base_fetch(source, state, "main", owner="project")
        return source, writer, state, original, remote

    def test_clean_diverged_replays_local_commits_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            source, writer, state, original, remote = self._diverged(directory)
            ignored = source / "ignored/config.ini"
            ignored.parent.mkdir(); ignored.write_text("secret\n")
            (source / "second.txt").write_text("second\n")
            original = commit_all(source, "Second local commit")
            git(source, "branch", "user-topic")
            git(source, "config", "rebase.updateRefs", "true")
            result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
            self.assertEqual(result.status, "updated", result.reason)
            self.assertEqual(result.remote_sha, remote)
            self.assertEqual(git(source, "rev-list", "--count", f"{remote}..HEAD"), "2")
            self.assertTrue(loop_manager.checkpoint_ownership.rebase_preserves_commits(
                source, original, git(source, "rev-parse", "HEAD"), remote, loop_manager.git))
            self.assertEqual(git(source, "rev-parse", "user-topic"), original)
            self.assertEqual(git(writer, "ls-remote", "--heads", "origin", "main").split()[0], remote)
            self.assertEqual(ignored.read_text(), "secret\n")
            self.assertEqual((source / "local.txt").read_text(), "local\n")
            self.assertEqual((source / "tracked.txt").read_text(), "two\n")
            self.assertEqual(git(source, "stash", "list"), "")
            self.assertFalse(list((state / "manager-git-journal").glob("*/active.json")))

    def test_diverged_dirty_index_and_intermediate_ignored_overlap_are_preserved(self):
        for scenario in ("staged", "unstaged", "untracked", "index-flag", "ignored-intermediate", "operation"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                source, _writer, state, _original, remote = self._diverged(directory)
                if scenario in {"staged", "unstaged", "untracked"}:
                    path = source / ("tracked.txt" if scenario != "untracked" else "user.txt")
                    path.write_text("dirty\n")
                    if scenario == "staged":
                        git(source, "add", path.name)
                elif scenario == "index-flag":
                    git(source, "update-index", "--skip-worktree", "tracked.txt")
                elif scenario == "ignored-intermediate":
                    ignored = source / "ignored/temporary.txt"
                    ignored.parent.mkdir(); ignored.write_text("committed\n")
                    git(source, "add", "-f", "ignored/temporary.txt")
                    git(source, "commit", "-m", "Intermediate file")
                    git(source, "rm", "ignored/temporary.txt")
                    git(source, "commit", "-m", "Remove intermediate file")
                    ignored.parent.mkdir(exist_ok=True); ignored.write_text("preserve\n")
                else:
                    operation = Path(git(source, "rev-parse", "--git-path", "MERGE_HEAD"))
                    if not operation.is_absolute(): operation = source / operation
                    operation.write_text(remote + "\n")
                before = source_rebase._observe(loop_manager._source_sync_effects_synchronize_base_worktree(), source, "main")
                result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "skipped", result.reason)
                self.assertEqual(source_rebase._observe(loop_manager._source_sync_effects_synchronize_base_worktree(), source, "main"), before)

    def test_owned_conflict_restores_start_and_does_not_repeat_until_inputs_change(self):
        with tempfile.TemporaryDirectory() as directory:
            source, writer, state, original, remote = self._diverged(directory, conflict=True)
            before_index = git(source, "ls-files", "--stage", "-v")
            result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
            self.assertEqual(result.status, "failed", result.reason)
            self.assertEqual(git(source, "rev-parse", "HEAD"), original)
            self.assertEqual(git(source, "ls-files", "--stage", "-v"), before_index)
            self.assertEqual((source / "tracked.txt").read_text(), "local\n")
            self.assertFalse(loop_manager._git_operation_entries(source))
            real_git = loop_manager.git
            with mock.patch("loop_manager.git", wraps=real_git) as commands:
                repeated = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
            self.assertEqual(repeated.status, "skipped", repeated.reason)
            self.assertFalse(any("rebase" in call.args[1:] for call in commands.call_args_list))
            remote = advance_remote(writer, path="another.txt")
            loop_manager._recorded_base_fetch(source, state, "main", owner="project")
            with mock.patch("loop_manager.git", wraps=real_git) as commands:
                loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
            self.assertTrue(any("rebase" in call.args[1:] for call in commands.call_args_list))

    def test_interrupted_success_reuses_completion_and_rejects_later_user_changes(self):
        for mutation in (None, "ref", "index", "file"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                source, _writer, state, original, remote = self._diverged(directory)
                with mock.patch.object(manager_git_journal, "_finish", side_effect=RuntimeError("interrupted")):
                    self.assertEqual(loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state).status, "failed")
                head = git(source, "rev-parse", "HEAD")
                self.assertNotEqual(head, original)
                if mutation == "ref":
                    git(source, "update-ref", "-m", "external away", "refs/heads/main", original)
                    git(source, "update-ref", "-m", "external back", "refs/heads/main", head)
                elif mutation == "index":
                    git(source, "update-index", "--assume-unchanged", "local.txt")
                elif mutation == "file":
                    (source / "local.txt").write_text("later user edit\n")
                effects = loop_manager._source_sync_effects_synchronize_base_worktree()
                before = source_rebase._observe(effects, source, "main")
                with mock.patch("loop_manager.git", wraps=loop_manager.git) as commands:
                    result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "updated" if mutation is None else "failed", result.reason)
                self.assertEqual(source_rebase._observe(effects, source, "main"), before)
                self.assertFalse(any("rebase" in call.args[1:] for call in commands.call_args_list))

    def test_interrupted_conflict_only_aborts_unchanged_owned_receipt(self):
        for mutation in (None, "file", "operation", "no-receipt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                source, _writer, state, _original, remote = self._diverged(directory, conflict=True)
                with mock.patch.object(source_rebase, "_restore_failure", side_effect=RuntimeError("interrupted")):
                    self.assertEqual(loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state).status, "failed")
                if mutation == "file":
                    (source / "tracked.txt").write_text("user resolution\n")
                elif mutation == "operation":
                    path = Path(git(source, "rev-parse", "--git-path", "rebase-merge/onto"))
                    if not path.is_absolute(): path = source / path
                    path.write_text(_original + "\n")
                elif mutation == "no-receipt":
                    slot = manager_git_journal._slot(state, str(source), "source_rebase", str(source))
                    next(slot.glob("*.invocation.json")).unlink()
                effects = loop_manager._source_sync_effects_synchronize_base_worktree()
                before = source_rebase._observe(effects, source, "main")
                result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                self.assertEqual(result.status, "failed", result.reason)
                self.assertFalse(manager_git_journal.has_pending_project_mutation(state, str(source)))
                if mutation is None:
                    self.assertFalse(loop_manager._git_operation_entries(source))
                    self.assertEqual((source / "tracked.txt").read_text(), "local\n")
                else:
                    self.assertEqual(source_rebase._observe(effects, source, "main"), before)

    def test_interrupted_abort_finalization_reuses_restored_start_without_another_git_write(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _writer, state, original, remote = self._diverged(directory, conflict=True)
            with mock.patch.object(manager_git_journal, "_finish", side_effect=RuntimeError("interrupted after abort")):
                result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
            self.assertEqual(result.status, "failed")
            self.assertEqual(git(source, "rev-parse", "HEAD"), original)
            self.assertFalse(loop_manager._git_operation_entries(source))
            with mock.patch("loop_manager.git", wraps=loop_manager.git) as commands:
                recovered = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
            self.assertEqual(recovered.status, "failed", recovered.reason)
            self.assertFalse(any("rebase" in call.args[1:] for call in commands.call_args_list))
            self.assertEqual((source / "tracked.txt").read_text(), "local\n")
            self.assertFalse(list((state / "manager-git-journal").glob("*/active.json")))

    def test_failed_fast_forward_is_not_repeated_at_identical_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, writer = remote_fixture(directory)
            state = Path(directory) / "state"
            remote = advance_remote(writer)
            loop_manager._recorded_base_fetch(source, state, "main", owner="project")
            real_git = loop_manager.git
            attempts = []
            def locked(repo, *args, **kwargs):
                if args[:2] == ("merge", "--ff-only"):
                    attempts.append(args)
                    return subprocess.CompletedProcess(args, 128, "index.lock exists")
                return real_git(repo, *args, **kwargs)
            with mock.patch("loop_manager.git", side_effect=locked):
                for _ in range(2):
                    result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                    self.assertEqual(result.status, "failed")
                self.assertEqual(len(attempts), 1)
                remote = advance_remote(writer, path="next.txt")
                loop_manager._recorded_base_fetch(source, state, "main", owner="project")
                self.assertEqual(loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state).status, "failed")
                self.assertEqual(len(attempts), 2)
            self.assertFalse(manager_git_journal.has_pending_project_mutation(state, str(source)))

    def test_source_rebase_lock_and_concurrent_eligibility_changes_are_preserved(self):
        for scenario in ("lock", "concurrent-file", "concurrent-remote"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                source, _writer, state, original, remote = self._diverged(directory)
                if scenario == "lock":
                    lock = Path(git(source, "rev-parse", "--git-path", "index.lock"))
                    if not lock.is_absolute(): lock = source / lock
                    lock.write_text("user-lock\n")
                    result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                    self.assertEqual(result.status, "failed", result.reason)
                    self.assertEqual(lock.read_text(), "user-lock\n")
                else:
                    real_snapshot = loop_manager._base_worktree_snapshot
                    calls = 0
                    def raced(repo):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            if scenario == "concurrent-file":
                                (repo / "local.txt").write_text("user edit\n")
                            else:
                                git(repo, "update-ref", "refs/remotes/origin/main", original)
                        return real_snapshot(repo)
                    with mock.patch("loop_manager._base_worktree_snapshot", side_effect=raced):
                        result = loop_manager.synchronize_base_worktree(source, "main", remote_sha=remote, state_dir=state)
                    self.assertEqual(result.status, "skipped", result.reason)
                    if scenario == "concurrent-file":
                        self.assertEqual((source / "local.txt").read_text(), "user edit\n")
                self.assertEqual(git(source, "rev-parse", "HEAD"), original)
                self.assertFalse(loop_manager._git_operation_entries(source))

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
