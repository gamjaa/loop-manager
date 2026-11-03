from tests.support.bootstrap_saved_scope import BootstrapSavedScopeFixture
from tests.support.git import isolated_git_test
from tests.support.fixed_samples import use_fixed_guidance
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
from tests.support.fixed_samples import PROJECT_FILES
from tests.support.workspace import pin_common_guidance
from loop_manager_internal.runtime import bootstrap


@isolated_git_test
class BootstrapRuntimeTests(OriginScopeFixture, unittest.TestCase):
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
                service = loop_manager._bootstrap_service()
                runtime = service.prepare_runtime_worktree(
                    source, state, mutation_id="a" * 32, commit_sha=sha)
                remove = service.remove_runtime_worktree
                def checked_remove(*args):
                    self.assertFalse((runtime.path / "__pycache__").exists())
                    self.assertFalse(loop_manager._ignored_worktree_paths(runtime.path))
                    remove(*args)
                with mock.patch.object(service, "remove_runtime_worktree", side_effect=checked_remove) as cleanup:
                    self.assertEqual(service.run_runtime_child(
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
        with mock.patch(
            "loop_manager.git",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        ), mock.patch.object(loop_manager.subprocess, "run", side_effect=child), mock.patch.object(
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
                self.assertEqual(connection.execute(
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




class BootstrapSavedScopeFixtureUnitTests(unittest.TestCase):
    def test_bootstrap_cost_collection_survives_child_and_proof_outcomes(self):
        import json
        from types import SimpleNamespace
        from tests.support.bootstrap_saved_scope import _track_bootstrap_costs
        from loop_manager_internal.verification.timing import EventWriter

        event = {'kind': 'phase_start', 'name': 'bootstrap:parent_cli', 'phase_id': 'p',
                 'pid': 123, 'at': 10, 'utc': 'original', 'clock_domain': 'test'}
        child = {**event, 'name': 'bootstrap:runtime_child_cli', 'pid': 456}
        for outcome in ('success', 'timeout', 'error', 'proof'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as root, contextlib.ExitStack() as resources:
                writer = resources.enter_context(contextlib.closing(EventWriter(Path(root) / 'parent.jsonl')))
                state_path = None
                with writer.activate(), mock.patch('subprocess.Popen', side_effect=AssertionError('unexpected process')):
                    try:
                        with tempfile.TemporaryDirectory(dir=root) as directory, contextlib.ExitStack() as cleanup:
                            state_path = Path(directory)
                            costs = state_path / 'bootstrap-costs'
                            costs.mkdir()
                            (costs / '123.jsonl').write_text(json.dumps(event) + '\n', encoding='utf-8')
                            (costs / '456.jsonl').write_text(json.dumps(child) + '\n', encoding='utf-8')
                            records, collect = _track_bootstrap_costs(cleanup, state_path)
                            result = SimpleNamespace(exit_code=0)
                            effect = RuntimeError('child error') if outcome == 'error' else (None, outcome == 'timeout', result)
                            with mock.patch('loop_manager_internal.execution.verify_process.run_verify_process', side_effect=effect if isinstance(effect, Exception) else None, return_value=effect):
                                BootstrapSavedScopeFixture()._run_child(state_path, state_path / 'manager', state_path, state_path / 'config')
                            if outcome == 'proof':
                                raise AssertionError('proof failure')
                            self.assertTrue(collect())
                            self.assertEqual(records, [event, child])
                    except (AssertionError, RuntimeError) as exc:
                        self.assertNotEqual(outcome, 'success')
                        if outcome == 'error':
                            self.assertEqual(str(exc), 'child error')
                        elif outcome == 'proof':
                            self.assertEqual(str(exc), 'proof failure')
                        else:
                            self.assertIn(str(result), str(exc))
                    else:
                        self.assertEqual(outcome, 'success')
                self.assertFalse(state_path.exists())
                self.assertEqual([json.loads(line) for line in writer.path.read_text().splitlines()], [event, child])

    def test_bootstrap_cost_collection_preserves_complete_lines_and_limits(self):
        import io
        import json
        from tests.support.bootstrap_saved_scope import _track_bootstrap_costs
        from loop_manager_internal.verification.timing import EventWriter

        event = {'kind': 'phase_start', 'name': 'bootstrap:parent_cli', 'phase_id': 'p',
                 'pid': 123, 'at': 10, 'utc': 'original', 'clock_domain': 'test'}
        for case in ('missing', 'partial', 'json', 'read', 'no_writer'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as resources:
                root = Path(directory)
                costs = root / 'bootstrap-costs'
                costs.mkdir()
                path = costs / '123.jsonl'
                if case != 'missing':
                    path.write_bytes((json.dumps(event) + '\n').encode() +
                                     (b'{partial' if case == 'partial' else b'{bad}\n' if case == 'json' else b''))
                writer = resources.enter_context(contextlib.closing(EventWriter(root / 'parent.jsonl')))
                stderr = io.StringIO()
                original_open = Path.open
                def open_file(target, *args, **kwargs):
                    if case == 'read' and target == path:
                        raise OSError('read failed')
                    return original_open(target, *args, **kwargs)
                with contextlib.redirect_stderr(stderr), mock.patch.object(Path, 'open', open_file):
                    with (writer.activate() if case != 'no_writer' else contextlib.nullcontext()):
                        with self.assertRaisesRegex(RuntimeError, 'original failure'):
                            with contextlib.ExitStack() as cleanup:
                                records, collect = _track_bootstrap_costs(cleanup, root)
                                raise RuntimeError('original failure')
                expected = [event] if case in ('partial', 'json', 'no_writer') else []
                self.assertEqual(records, expected)
                self.assertEqual([json.loads(line) for line in writer.path.read_text().splitlines()],
                                 expected if case != 'no_writer' else [])
                self.assertEqual('collection incomplete' in stderr.getvalue(), case != 'no_writer')

    def test_bootstrap_cost_normal_checks_require_complete_and_expected_records(self):
        import json
        from tests.support.bootstrap_saved_scope import _track_bootstrap_costs

        parent = {'kind': 'phase_end', 'name': 'bootstrap:parent_cli', 'pid': 1, 'at': 1}
        child = {'kind': 'phase_end', 'name': 'bootstrap:runtime_child_cli', 'pid': 2, 'at': 2}
        cost = {'kind': 'git_cost', 'pid': 2, 'at': 3, 'count': 1, 'seconds': 0.1}
        fixture = BootstrapSavedScopeFixture()
        for scenario in ('complete', 'partial', 'missing_parent', 'missing_child', 'missing_git'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                costs = state / 'bootstrap-costs'
                costs.mkdir()
                events = [parent, child, cost]
                if scenario.startswith('missing_'):
                    events.remove({'missing_parent': parent, 'missing_child': child, 'missing_git': cost}[scenario])
                (costs / '1.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events)
                    + ('{' if scenario == 'partial' else ''), encoding='utf-8')
                with contextlib.ExitStack() as cleanup:
                    records, collect = _track_bootstrap_costs(cleanup, state)
                    if scenario == 'complete':
                        fixture._assert_bootstrap_costs(records, collect, 1, False)
                    else:
                        with self.assertRaises(AssertionError):
                            fixture._assert_bootstrap_costs(records, collect, 1, False)

    def test_collected_incomplete_phase_keeps_unknown_elapsed_in_diagnostic(self):
        import json
        from tests.support.bootstrap_saved_scope import _track_bootstrap_costs
        from loop_manager_internal.verification.timing import EventWriter, diagnostic, summarize

        for domain in (None, 'foreign'):
            with self.subTest(domain=domain), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / 'state'
                costs = state / 'bootstrap-costs'
                costs.mkdir(parents=True)
                output = root / 'output'
                output.mkdir()
                event = {'kind': 'phase_start', 'name': 'bootstrap:parent_cli',
                         'phase_id': 'original', 'pid': 123, 'at': 10}
                if domain is not None:
                    event['clock_domain'] = domain
                (costs / '123.jsonl').write_text(json.dumps(event) + '\n', encoding='utf-8')
                writer = EventWriter(output / 'worker.jsonl')
                with contextlib.closing(writer), writer.activate(), contextlib.ExitStack() as cleanup:
                    records, collect = _track_bootstrap_costs(cleanup, state)
                    self.assertTrue(collect())
                    self.assertEqual(records, [event])
                active = summarize(output, now=20)['active']
                self.assertEqual(len(active), 1)
                self.assertIsNone(active[0]['elapsed'])
                self.assertEqual(active[0]['pid'], 123)
                self.assertIn('미완료 경과 시간 미확인: bootstrap:parent_cli', diagnostic(output))

    def test_saved_scope_setup_combines_git_isolation_and_fixed_guidance(self):
        import os
        from loop_manager_internal.agents import common_guidance

        read = common_guidance._read
        def read_sample(path):
            self.assertTrue(path.is_relative_to(PROJECT_FILES), path)
            return read(path)
        with (mock.patch.object(common_guidance, 'RUNTIME_ROOT', Path('/missing-runtime')),
              mock.patch.object(common_guidance, '_read', side_effect=read_sample),
              mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': 'user-config'}),
              mock.patch('subprocess.Popen', side_effect=AssertionError('unexpected process'))):
            fixture = BootstrapSavedScopeFixture()
            try:
                fixture.setUp()
                self.assertEqual(os.environ['GIT_CONFIG_GLOBAL'], os.devnull)
                self.assertEqual(common_guidance.RUNTIME_ROOT, PROJECT_FILES)
                self.assertEqual(len(common_guidance.runtime_content_hash()), 64)
            finally:
                fixture.doCleanups()
            self.assertEqual(common_guidance.RUNTIME_ROOT, Path('/missing-runtime'))
            self.assertEqual(os.environ['GIT_CONFIG_GLOBAL'], 'user-config')

    def test_saved_invocation_proof_is_bound_to_role_session_repository_and_scope(self):
        from types import SimpleNamespace
        from tests.support.database import runtime_connection
        from tests.support.runtime import record_saved_scope_invocation
        from loop_manager_internal.agents import scope_validation
        from tests.support.scope import snapshot

        connection = runtime_connection()
        self.addCleanup(connection.close)
        ledger = agent_invocation_state.InvocationRepository(connection)
        for role in ('planner', 'worker', 'reviewer'):
            with self.subTest(role=role):
                before = snapshot(scope_id=role + '-scope', scope_start_digest='c' * 64,
                                  branch=None if role == 'planner' else 'loop-manager/G-047')
                plan_id = None if role == 'planner' else 'G-047'
                worktree = Path('/managed')
                repository_identity = hashlib.sha256((str(Path('/common')) + '\0origin').encode()).hexdigest()
                with mock.patch('loop_manager_internal.agents.agent_write_isolation.inspect_worktree',
                                return_value=SimpleNamespace(repository_identity=repository_identity)):
                    record_saved_scope_invocation(connection, 'repo', Path('/state'), worktree,
                                                  role, before, role + '-existing', plan_id=plan_id)
                digest = connection.execute('SELECT snapshot_digest FROM agent_isolation_evidence '
                    'JOIN agent_invocations USING (invocation_id) WHERE scope_id=?',
                    (before.scope_id,)).fetchone()[0]
                expected = dict(repo='repo', plan_id=plan_id, role=role, provider='codex',
                                session_id=role + '-existing', worktree=str(worktree.resolve()),
                                repository_identity=repository_identity, start_digest=digest)
                self.assertTrue(ledger.verified_scope(before.scope_id, **expected))
                for key, value in (('role', 'other'), ('session_id', 'other'),
                                   ('repository_identity', 'other'), ('worktree', '/other'),
                                   ('start_digest', 'd' * 64), ('plan_id', 'G-999')):
                    with self.subTest(mismatch=key):
                        self.assertFalse(ledger.verified_scope(before.scope_id, **{**expected, key: value}))
                self.assertFalse(ledger.verified_scope('unbound-scope', **expected))
                owner = {'agent_snapshot': before.serialize(), 'agent_role': role,
                         'worktree': str(worktree), 'plan_path': 'docs/plans/[G-047] Runtime.md',
                         'provider': 'codex', 'base_branch': 'main', 'branch': before.branch,
                         'phase': 'PLANNING' if role == 'planner' else
                                  'REVIEWING' if role == 'reviewer' else 'WORKING',
                         role + '_session_id': role + '-existing'}
                effects = mock.Mock()
                effects.repository.return_value.scope_owner.return_value = owner
                effects._plan_id_from_path.return_value = plan_id
                effects._git_common_directory.return_value = Path('/common')
                effects.git.return_value = SimpleNamespace(stdout='origin\n')
                effects._rebase_in_progress_for_branch.return_value = False
                resources = scope_validation._agent_git_resources(
                    connection, Path('repo'), worktree, before, role, effects=effects)
                self.assertIsNotNone(resources)
                self.assertEqual(resources.worktrees, frozenset((str(worktree.resolve()),)))
                if role == 'reviewer':
                    from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
                    owner['phase'] = 'WORKING'
                    effects._owned_rebase_state_dir.return_value = Path('/state')
                    effects._current_rebase_service.return_value.require_owned.return_value = (
                        None, SimpleNamespace(status=RebaseStatus.COMPLETE))
                    self.assertIsNotNone(scope_validation._agent_git_resources(
                        connection, Path('repo'), worktree, before, role, effects=effects))
                    effects._current_rebase_service.return_value.require_owned.return_value = (
                        None, SimpleNamespace(status=RebaseStatus.WAITING))
                    self.assertIsNotNone(scope_validation._agent_git_resources(
                        connection, Path('repo'), worktree, before, role, effects=effects))
                    owner['phase'] = 'REVIEWING'
                owner[role + '_session_id'] = 'unmatched-session'
                self.assertIsNotNone(scope_validation._agent_git_resources(
                    connection, Path('repo'), worktree, before, role, effects=effects))


class ReviewerRebaseScopeHandoffUnitTests(unittest.TestCase):
    def test_owned_completed_rebase_rechecks_reviewer_before_fresh_worker_scope(self):
        from types import SimpleNamespace
        from loop_manager_internal.agents import scope_lifecycle
        from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
        from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
        from tests.support.database import runtime_connection
        from tests.support.scope import snapshot

        connection = runtime_connection()
        self.addCleanup(connection.close)
        before = snapshot(branch='loop-manager/G-047', scope_id='a' * 32,
                          scope_start_digest='c' * 64,
                          worktrees={str(Path('/managed').resolve()): (
                              'HEAD=a', 'branch=refs/heads/loop-manager/G-047')},
                          refs={'refs/heads/loop-manager/G-047': ('a', '')})
        current = replace(before, head='b', refs={'refs/heads/loop-manager/G-047': ('b', '')})
        row = {'agent_snapshot': before.serialize(), 'agent_result_snapshot': before.serialize(),
               'agent_role': 'reviewer', 'plan_path': 'docs/plans/[G-047] Runtime.md',
               'branch': before.branch, 'base_branch': 'main'}
        checkpoint = ExecutionCheckpointRepository(connection)
        checkpoint.record('repo', 'G-047', expected_generation=0, operation=None,
                          expected_head='b', target_base_oid='b', observed_remote_oid=None,
                          snapshot={'current_rebase_acceptance': {'fixture': 'owned'},
                              'manager_metadata_agent_baseline': {
                                  'role': 'reviewer',
                                  'original': hashlib.sha256(before.serialize().encode()).hexdigest(),
                                  'snapshot': replace(before, refs={**before.refs,
                                      'refs/remotes/origin/main': ('b', '')}).serialize()}})
        effects = mock.Mock()
        effects.repository.return_value.scope_owner.return_value = row
        effects._plan_id_from_path.return_value = 'G-047'
        effects._owned_rebase_state_dir.return_value = Path('/state')
        effects.capture_git_snapshot.return_value = current
        effects._capture_agent_baseline.return_value = None
        effects.validate_agent_scope.side_effect = AssertionError('historical Reviewer comparison')
        for status in (RebaseStatus.WAITING, RebaseStatus.COMPLETE):
            effects._current_rebase_service.return_value.require_owned.return_value = (
                None, SimpleNamespace(status=status))
            resumed = scope_lifecycle.begin_agent_scope(
                connection, 'repo', Path('/managed'), 'worker', effects=effects)
            self.assertEqual(resumed.head, current.head)
            self.assertNotEqual(resumed.scope_id, before.scope_id)
            effects.validate_agent_scope.assert_not_called()
            effects._current_rebase_service.assert_not_called()
            self.assertEqual(effects.update_project.call_args.kwargs['agent_role'], 'worker')
            audit = checkpoint.get('repo', 'G-047').snapshot['retired_agent_scopes'][-1]
            self.assertEqual(audit['agent_role'], 'reviewer')
            self.assertEqual(audit['agent_snapshot'], before.serialize())
