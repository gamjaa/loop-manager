"""실제 Git 경로·재관측과 snapshot 계측의 비용·실패 계약을 확인한다."""
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager as manager
from loop_manager_internal.agents import scope_observation
from loop_manager_internal.verification import git_timing
from loop_manager_internal.verification.git_timing import snapshot_timing
from loop_manager_internal.verification.timing import EventWriter, summarize
from tests.support.git import git_command, make_remote_repository


class GitSnapshotCostTests(unittest.TestCase):
    def test_operation_batch_matches_single_queries_and_recaptures_each_worktree(self):
        with tempfile.TemporaryDirectory(prefix="snapshot cost ") as directory:
            repo = make_remote_repository(directory)
            other = Path(directory) / "other worktree"
            git_command(repo, "worktree", "add", "-b", "other", str(other))
            effects = manager._scope_observation_effects()
            for worktree in (repo, other):
                for name in ("MERGE_MSG", "rebase-merge/head-name"):
                    path = Path(git_command(worktree, "rev-parse", "--git-path", name))
                    if not path.is_absolute():
                        path = worktree / path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(str(worktree), encoding="utf-8")
                expected = {}
                for name in scope_observation._GIT_OPERATION_PATHS:
                    path = Path(git_command(worktree, "rev-parse", "--git-path", name))
                    if not path.is_absolute():
                        path = worktree / path
                    if path.exists() or path.is_symlink():
                        expected[name] = scope_observation._git_operation_path_signature(path)
                observed_git = mock.Mock(wraps=effects.git)
                observed = replace(effects, git=observed_git)
                self.assertEqual(scope_observation._git_operation_entries(worktree, effects=observed), expected)
                self.assertEqual(observed_git.call_count, 1)
            before = scope_observation._git_operation_entries(other, effects=effects)
            message = Path(git_command(other, "rev-parse", "--git-path", "MERGE_MSG"))
            if not message.is_absolute():
                message = other / message
            message.write_text("changed", encoding="utf-8")
            after = scope_observation._git_operation_entries(other, effects=effects)
            self.assertNotEqual(before["MERGE_MSG"], after["MERGE_MSG"])
            self.assertNotEqual(after["MERGE_MSG"], scope_observation._git_operation_entries(repo, effects=effects)["MERGE_MSG"])

    def test_ambiguous_batch_output_uses_single_path_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            message = repo / "MERGE_MSG"
            message.write_text("evidence", encoding="utf-8")
            names = scope_observation._GIT_OPERATION_PATHS
            def git(_path, *arguments):
                output = "ambiguous\npath\n" if len(arguments) > 3 else str(repo / arguments[-1]) + "\n"
                return subprocess.CompletedProcess([], 0, output)
            effects = replace(manager._scope_observation_effects(), git=mock.Mock(side_effect=git))
            entries = scope_observation._git_operation_entries(repo, effects=effects)
            self.assertEqual(entries, {"MERGE_MSG": scope_observation._git_operation_path_signature(message)})
            self.assertEqual(effects.git.call_count, 1 + len(names))

    def test_real_snapshot_costs_group_by_caller_without_logging_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            git_command(repo, "config", "private.token", "do-not-log-this")
            logs = Path(directory) / "logs"
            logs.mkdir()
            writer = EventWriter(logs / "worker.jsonl")
            try:
                def capture():
                    return manager.capture_git_snapshot(repo)
                with writer.activate():
                    before = capture()
                    (repo / "src/app.py").write_text("changed", encoding="utf-8")
                    after = capture()
            finally:
                writer.close()
            summary = summarize(logs)
            self.assertNotEqual(before.dirty, after.dirty)
            self.assertEqual(len(summary["git_snapshots"]), 1)
            name, cost = next(iter(summary["git_snapshots"].items()))
            self.assertIn(":capture:", name)
            self.assertEqual(cost["count"], 2)
            self.assertEqual(cost["commands"]["rev-parse"]["count"], 6)
            self.assertEqual(cost["commands"]["diff"]["count"], 4)
            self.assertGreater(cost["seconds"], 0)
            text = (logs / "worker.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("do-not-log-this", text)
            self.assertNotIn(str(repo), text)
            self.assertFalse(summary["active"])

    def test_inactive_timing_is_identity_and_errors_keep_costs_and_propagate(self):
        effects = manager._scope_observation_effects()
        with mock.patch.object(git_timing, "_current_writer") as current:
            # 전체 runner의 바깥 writer와 무관하게 비활성 경계를 확인한다.
            current.get.return_value = None
            with snapshot_timing(effects) as observed:
                self.assertIs(observed, effects)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = EventWriter(root / "worker.jsonl")
            observed = replace(effects, git=mock.Mock(side_effect=RuntimeError("Git failed")))
            try:
                with writer.activate(), self.assertRaisesRegex(RuntimeError, "Git failed"):
                    scope_observation.capture_git_snapshot(root, effects=observed)
            finally:
                writer.close()
            summary = summarize(root)
            event = summary["phases"][0]
            self.assertEqual(event["result"], "error")
            self.assertEqual(event["commands"]["rev-parse"]["count"], 1)
            self.assertFalse(summary["active"])
