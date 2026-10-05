"""실제 Git 경로·재관측과 snapshot 계측의 비용·실패 계약을 확인한다."""
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager as manager
from loop_manager_internal.agents import scope_observation
from loop_manager_internal.git.managed_rebase_observation import (
    ObservationAdapters, observe_current_rebase, parse_index, parse_raw_changes,
)
from loop_manager_internal.git.managed_rebase_validation import RebaseObservation
from loop_manager_internal.verification import git_timing
from loop_manager_internal.verification.git_timing import snapshot_timing, git_command_timing
from loop_manager_internal.verification.timing import EventWriter, summarize, phase
from tests.support.git import git_command, make_remote_repository
from tests.support.rebase_observations import expectation


class GitSnapshotCostTests(unittest.TestCase):
    def test_head_branch_batch_matches_legacy_and_saves_one_command_per_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            other = Path(directory) / 'other'
            git_command(repo, 'worktree', 'add', '--detach', str(other))
            effects = manager._scope_observation_effects()
            indexes = [repo / '.git/index', repo / '.git/worktrees/other/index']
            original_indexes = [path.read_bytes() for path in indexes]
            count = []
            def legacy(path, *args, **kwargs):
                if args == ('rev-parse', 'HEAD', '--abbrev-ref', 'HEAD'):
                    head = effects.git(path, 'rev-parse', '--verify', 'HEAD').stdout.strip()
                    branch = effects.git(path, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False)
                    count.extend(('rev-parse', 'symbolic-ref'))
                    return subprocess.CompletedProcess([], 0, head + '\n' + (branch.stdout.strip() if branch.returncode == 0 else 'HEAD') + '\n')
                count.append(args[0])
                return effects.git(path, *args, **kwargs)
            expected = scope_observation.capture_git_snapshot(repo, effects=replace(effects, git=legacy))
            calls = mock.Mock(wraps=effects.git)
            actual = scope_observation.capture_git_snapshot(repo, effects=replace(effects, git=calls))
            self.assertEqual(actual, expected)
            self.assertEqual([path.read_bytes() for path in indexes], original_indexes)
            self.assertEqual(len(count) - calls.call_count, 2)
            for output in ('', 'bad\nmain\n', 'a' * 40 + '\n', 'a' * 40 + '\nmain\nextra\n'):
                with self.subTest(output=output), self.assertRaises(RuntimeError):
                    scope_observation._capture_local_worktree_state(repo, effects=replace(effects,
                        git=mock.Mock(return_value=subprocess.CompletedProcess([], 0, output))))

    def test_total_git_cost_includes_preparation_and_snapshot_with_parent_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = EventWriter(root / 'worker.jsonl')
            try:
                with writer.activate(), git_command_timing():
                    writer.current_test = self.id()
                    with phase('fixture:prepare'):
                        repo = make_remote_repository(str(root / 'case'))
                    def shared():
                        return manager.capture_git_snapshot(repo)
                    def first_decision():
                        return shared()
                    def ownership():
                        return shared()
                    with phase('inspect'):
                        self.assertEqual(first_decision(), ownership())
            finally:
                writer.close()
            summary = summarize(root)
            snapshots = summary['git_snapshots']
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(any('first_decision' in name for name in snapshots))
            self.assertTrue(any('ownership' in name for name in snapshots))
            snapshot_commands = sum(command['count'] for cost in snapshots.values() for command in cost['commands'].values())
            self.assertGreater(sum(cost['count'] for cost in summary['git_commands']), snapshot_commands)
            self.assertTrue(any('fixture:prepare' in cost['phases'] for cost in summary['git_commands']))
            self.assertTrue(all(cost['test'] == self.id() for cost in summary['git_commands']))
            self.assertFalse(summary['git_commands_incomplete'])

    def test_real_rebase_observation_shares_reads_and_still_queries_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            head = git_command(repo, 'rev-parse', 'HEAD')
            expected = expectation(repo=str(repo.resolve()), worktree=str(repo.resolve()),
                repository_identity=str((repo / '.git').resolve()), branch='main',
                target_base_oid=head, before_head=head)
            git = mock.Mock(wraps=manager.git)
            adapters = ObservationAdapters(git=git, capture_snapshot=manager.capture_git_snapshot,
                validate_identity=mock.Mock(), read_rebase_metadata=manager._owned_rebase_metadata,
                read_remote=mock.Mock(return_value=expected.remote),
                read_inputs=lambda _: ('inputs', 'worker-contract'),
                read_file=lambda root, path: ('100644', manager._path_signature(root, path)),
                path_allowed=lambda _: True, contracts_allowed=lambda _: True)
            legacy = observe_current_rebase(expected, replace(adapters,
                capture_snapshot=lambda root, **_: manager.capture_git_snapshot(root)))
            legacy_calls = git.call_count
            git.reset_mock()
            actual = observe_current_rebase(expected, adapters)
            self.assertIsInstance(actual[0], RebaseObservation)
            self.assertIsNotNone(actual[1])
            self.assertEqual(actual, legacy)
            self.assertEqual(legacy_calls - git.call_count, 6)
            self.assertEqual(adapters.read_remote.call_count, 4)
            self.assertFalse(any(call.args[1] == 'ls-files' or '--cached' in call.args
                                 for call in git.call_args_list))

    def test_details_share_index_diff_ignored_and_paths_without_changing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_remote_repository(directory)
            (repo / '.gitignore').write_text('*.cache\n', encoding='utf-8')
            git_command(repo, 'add', '.gitignore')
            git_command(repo, 'commit', '-m', 'Ignore artifacts')
            for name in ('one.cache', 'two space.cache'):
                (repo / name).write_text('ignored', encoding='utf-8')
            (repo / 'src/app.py').write_text('changed', encoding='utf-8')
            git_command(repo, 'add', 'src/app.py')
            git_command(repo, 'update-index', '--skip-worktree', 'README.md')
            expected = manager.capture_git_snapshot(repo)
            effects = manager._scope_observation_effects()
            git = mock.Mock(wraps=effects.git)
            git_bytes = mock.Mock(wraps=effects.git_bytes)
            process = mock.Mock(wraps=effects._handoff_run)
            details = {'stale': 'must disappear'}
            actual = scope_observation.capture_git_snapshot(repo, details=details,
                effects=replace(effects, git=git, git_bytes=git_bytes, _handoff_run=process))
            self.assertEqual(actual.serialize(), expected.serialize())
            self.assertNotIn('stale', details)
            entries = parse_index(actual.index, details['index_debug'])
            self.assertTrue(next(entry for entry in entries if entry.path == 'README.md').flags)
            self.assertEqual([change.path for change in parse_raw_changes(details['staged_raw'])], ['src/app.py'])
            self.assertEqual(set(details['ignored_paths']), {'one.cache', 'two space.cache'})
            self.assertEqual(process.call_count, 1, 'ignored 파일 수와 subprocess 수를 분리한다')
            self.assertEqual(sum(call.args[1] == 'diff' for call in git_bytes.call_args_list), 2)
            self.assertEqual(sum(call.args[1] == 'ls-files' and '--stage' in call.args
                                 for call in git_bytes.call_args_list), 1)
            metadata_dir = details['git_paths']['rebase-merge']
            metadata_dir.mkdir()
            for name, value in (('orig-head', 'before'), ('head-name', 'refs/heads/main'), ('onto', 'target')):
                (metadata_dir / name).write_text(value, encoding='utf-8')
            with mock.patch.object(manager, 'git', wraps=manager.git) as read:
                self.assertEqual(manager._owned_rebase_metadata(repo, git_paths=details['git_paths'])['onto'], 'target')
                read.assert_not_called()
                self.assertEqual(manager._owned_rebase_metadata(repo)['onto'], 'target')
                self.assertEqual(read.call_count, 1)
            (metadata_dir / 'onto').write_text('advanced', encoding='utf-8')
            self.assertEqual(manager._owned_rebase_metadata(repo, git_paths=details['git_paths'])['onto'], 'advanced')
            (repo / '.gitignore').write_text('*.cache\nchanged-ignore\n', encoding='utf-8')
            changed = manager.capture_git_snapshot(repo, details=details)
            self.assertIn('one.cache', changed.dirty)
            self.assertIn('two space.cache', changed.dirty)

    def test_batched_ignore_sources_reject_malformed_results_and_missing_rules(self):
        effects = manager._scope_observation_effects()
        with self.assertRaisesRegex(RuntimeError, '목록이 잘렸습니다'):
            scope_observation._nul_git_paths(Path('/repo'), 'ls-files', '--others',
                effects=replace(effects, git_bytes=mock.Mock(return_value=b'ignored.cache')))
        valid = b'.gitignore\0' + b'1\0*.cache\0one.cache\0'
        for output, code in ((valid + valid, 0), (valid[:-1], 0),
                             (valid.replace(b'one.cache', b'other.cache'), 0),
                             (valid, 1), (b'', 128)):
            with self.subTest(output=output, code=code):
                process = mock.Mock(return_value=subprocess.CompletedProcess([], code, output))
                with self.assertRaises(RuntimeError):
                    scope_observation._ignore_rule_sources(Path('/repo'), ['one.cache'],
                        effects=replace(effects, _handoff_run=process))
        process = mock.Mock(return_value=subprocess.CompletedProcess([], 0, valid))
        sources = scope_observation._ignore_rule_sources(Path('/repo'), ['one.cache', 'missing'],
            effects=replace(effects, _handoff_run=process))
        self.assertEqual(sources, {'one.cache': '.gitignore'})
        self.assertEqual(process.call_args.kwargs['input'], b'one.cache\0missing\0')
        with self.assertRaisesRegex(RuntimeError, '규칙을 확인할 수 없습니다: missing'):
            scope_observation._capture_local_worktree_state(Path('/repo'), effects=replace(
                effects, git=mock.Mock(return_value=subprocess.CompletedProcess([], 0, 'a' * 40 + '\nmain\n')),
                git_bytes=mock.Mock(side_effect=[b'', b'', b'', b'', b'one.cache\0missing\0']),
                _handoff_run=process))
        process.return_value = subprocess.CompletedProcess([], 1, b'')
        self.assertIsNone(scope_observation._ignore_rule_source(Path('/repo'), 'missing',
            effects=replace(effects, _handoff_run=process)))

    def test_combined_index_debug_preserves_stages_and_unusual_path_and_fails_closed(self):
        effects = manager._scope_observation_effects()
        path = 'tab\tline\nname'
        raw = b''.join(
            f'M 100644 blob-{stage} {stage}\t{path}\0'.encode() +
            f'  ctime: 1:1\n  mtime: 1:1\n  dev: 1\tino: 1\n  uid: 1\tgid: 1\n  size: 1\tflags: {stage << 12:x}\n'.encode()
            for stage in (1, 2, 3))
        details = {}
        fake = replace(effects, git_bytes=mock.Mock(return_value=raw))
        index = scope_observation._index_entries(Path('/repo'), effects=fake, details=details)
        self.assertEqual([entry.stage for entry in parse_index(index, details['index_debug'])], [1, 2, 3])
        self.assertEqual(set(index), {path})
        for malformed in (raw[:-1], raw.replace(b'flags: 1000', b'flags: invalid'), raw + b'truncated'):
            with self.subTest(malformed=malformed), self.assertRaises(RuntimeError):
                scope_observation._index_entries(Path('/repo'), details={},
                    effects=replace(effects, git_bytes=mock.Mock(return_value=malformed)))

    def test_metadata_query_failure_and_empty_operation_paths_are_not_absence(self):
        with mock.patch.object(manager, 'git', return_value=subprocess.CompletedProcess([], 128, '')):
            with self.assertRaisesRegex(RuntimeError, 'metadata 경로'):
                manager._owned_rebase_metadata(Path('/repo'))
        with mock.patch.object(manager, 'git', return_value=subprocess.CompletedProcess([], 0, '')):
            with self.assertRaisesRegex(RuntimeError, '경로가 비어 있습니다'):
                manager._owned_rebase_metadata(Path('/repo'))
        effects = replace(manager._scope_observation_effects(),
            git=mock.Mock(return_value=subprocess.CompletedProcess([], 0, '')))
        with self.assertRaisesRegex(RuntimeError, 'operation 경로'):
            scope_observation._git_operation_entries(Path('/repo'), effects=effects)

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
            costs = summary['git_snapshots']
            self.assertTrue(all(':capture:' in name for name in costs))
            self.assertEqual(sum(cost['count'] for cost in costs.values()), 2)
            self.assertEqual(sum(cost['commands']['rev-parse']['count'] for cost in costs.values()), 6)
            self.assertEqual(sum(cost['commands']['diff']['count'] for cost in costs.values()), 4)
            self.assertGreater(sum(cost['seconds'] for cost in costs.values()), 0)
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
