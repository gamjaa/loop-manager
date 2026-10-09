from tests.support.git import isolated_git_test
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
                audit = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
                    str(source), '').snapshot['manager_metadata_agent_baseline']
                self.assertEqual(audit['original'], hashlib.sha256(before.serialize().encode()).hexdigest())
                self.assertIn(str(runtime.path), loop_manager.GitSnapshot.deserialize(audit['snapshot']).worktrees)

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


@isolated_git_test
class BootstrapSavedScopeResumeTests(unittest.TestCase):
    # Real Git and child CLI are the contract here: imported runtime B, saved
    # Agent scopes, and later base C must stay connected across process restart.
    """Real subprocess regressions; use the coordinated verification runner."""

    def _run_child(self, source, manager_file, state, config):
        import sys
        from loop_manager_internal.execution.verify_process import run_verify_process
        # Native Windows file observations exceed 90s under parallel verify.
        timeout = 180 if sys.platform == 'win32' else 90
        command = [sys.executable, '-B', str(manager_file), '--state-dir', str(state),
                   '--config', str(config), 'run']
        _, timed_out, output = run_verify_process(command, source, timeout, job_scope=True)
        self.assertFalse(timed_out, output)
        return subprocess.CompletedProcess(command, output.exit_code, output)

    def _assert_saved_scope_resume(self, advance_after_selection=False):
        import json
        import shutil
        import sys
        from tests.support.git import make_remote_repository, copy_manager_import_fixture
        from tests.support.catalog import write_execplan
        from tests.support.profiles import profile_config
        from tests.support.runtime import record_saved_scope_invocation
        from loop_manager_internal.execution.worker_session import SessionBinding
        from loop_manager_internal.planning.planning_workspace import planning_workspace_path
        from loop_manager_internal.verification.timing import _current_writer

        for base in ('main', 'develop'):
            for role in ('planner', 'worker', 'reviewer'):
                with self.subTest(base=base, role=role), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as cleanup:
                    source = make_remote_repository(directory)
                    git(source, 'config', 'core.longpaths', 'true')
                    state = Path(directory) / 'state'
                    state.mkdir()
                    copy_manager_import_fixture(source)
                    # Keep bootstrap, identity, SQLite, scope and dispatch real.
                    # Optional source-fact prompt enrichment has no assertion
                    # here; its Git/source parsing is covered by code-map tests.
                    manager_file = source / 'loop_manager.py'
                    manager_file.write_text(manager_file.read_text().replace(
                        'if __name__ == "__main__":\n    raise SystemExit(main())',
                        '''if __name__ == "__main__":
    def fake_agent(worktree, state_dir, config, role, prompt, **options):
        proof = state_dir / 'resumed-agent.json'
        proof.write_text(json.dumps({'role': role, 'session': options.get('session_id'),
                                    'runtime': str(ROOT),
                                    'sha': git(ROOT, 'rev-parse', 'HEAD').stdout.strip(),
                                    'worktree': str(worktree)}))
        if (state_dir / 'expected-role').read_text() == 'reviewer' and role == 'worker':
            return CodexResult(0, 'WORKER_OUTCOME: {"kind":"ready_for_validation"}\\n'
                'COMMIT_TITLE: Preserve saved candidate\\nPR_SUMMARY: Preserve saved candidate\\n'
                'TARGETED_TESTS: fixture unit PASS', options.get('session_id'))
        return CodexResult(1, 'usage limit reached', options.get('session_id'))
    if '--runtime-child' not in sys.argv:
        stop_file = Path(sys.argv[sys.argv.index('--state-dir') + 1]) / 'stop-after-registration'
        if stop_file.exists():
            from loop_manager_internal.git import manager_git_journal
            finish = manager_git_journal._finish
            def interrupted_finish(slot, active, intent, *args, **kwargs):
                if intent['kind'] == 'runtime_create':
                    stop_file.unlink()
                    raise RuntimeError('test interruption after runtime registration')
                return finish(slot, active, intent, *args, **kwargs)
            manager_git_journal._finish = interrupted_finish
    github_repository = lambda value: 'owner/repo'
    validate_runtime_prerequisites = lambda *args, **kwargs: None
    pr_for_branch = lambda *args, **kwargs: None
    codex_model_efforts = lambda *args: {model: frozenset({'medium', 'high', 'max', 'xhigh'})
        for model in ('test-fast', 'test-balanced', 'test-strong')}
    run_codex = fake_agent
    from loop_manager_internal.maintenance import tiered_ports
    tiered_ports.prepare_query_context = lambda *args, **kwargs: ''
    if '--runtime-child' in sys.argv:
        from loop_manager_internal.runtime.bootstrap import BootstrapService
        validate_mutation = BootstrapService.validate_runtime_mutation
        def advance_remote_then_validate(service, source, state_dir, *args, **kwargs):
            advance = state_dir / 'advance-after-selection.json'
            if advance.exists():
                writer, base = json.loads(advance.read_text())
                subprocess.run(['git', '-C', writer, 'push', 'origin', 'HEAD:' + base], check=True)
                subprocess.run(['git', '-C', str(source), 'fetch', 'origin', base], check=True)
                advance.unlink()
            return validate_mutation(service, source, state_dir, *args, **kwargs)
        BootstrapService.validate_runtime_mutation = advance_remote_then_validate
    from loop_manager_internal.verification.timing import EventWriter, phase
    from loop_manager_internal.verification.git_timing import git_command_timing
    costs = Path(sys.argv[sys.argv.index('--state-dir') + 1]) / 'bootstrap-costs'
    costs.mkdir(exist_ok=True)
    cost_writer = EventWriter(costs / (str(os.getpid()) + '.jsonl'))
    cost_writer.current_test = BOOTSTRAP_TEST_LABEL
    try:
        with cost_writer.activate(), phase('bootstrap:runtime_child_cli' if '--runtime-child' in sys.argv
                                          else 'bootstrap:parent_cli'), git_command_timing():
            code = main()
    finally:
        cost_writer.close()
    raise SystemExit(code)''').replace('BOOTSTRAP_TEST_LABEL', repr(self.id())), encoding='utf-8')
                    for name in ('PLANS.md', 'AGENTS.common.md', 'AGENTS.planner.md',
                                 'AGENTS.planner_check.md', 'AGENTS.worker.md', 'AGENTS.reviewer.md',
                                 'AGENTS.ponytail.md'):
                        shutil.copy(PROJECT_FILES / 'docs' / name, source / 'docs' / name)
                    (source / 'scripts').mkdir(exist_ok=True)
                    (source / 'scripts/verify.sh').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
                    (source / 'scripts/verify.ps1').write_text('exit 0\n', encoding='utf-8')
                    (state / 'expected-role').write_text(role, encoding='utf-8')
                    (source / 'docs/plans').mkdir()
                    plan_file = write_execplan(source, 'G-001', 'Resume saved scope')
                    plan_path = plan_file.relative_to(source).as_posix()
                    if base != 'main':
                        git(source, 'checkout', '-b', base)
                    sha_a = commit_all(source, 'Manager fixture at A')
                    git(source, 'push', 'origin', f'HEAD:{base}')
                    git(source, 'fetch', 'origin')
                    if base == 'develop':
                        # Keep main at another SHA: it cannot serve as the runtime base.
                        git(source, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/develop')

                    connection = loop_manager.connect_database(state, create=True)
                    cleanup.callback(connection.close)
                    with connection:
                        connection.execute('INSERT INTO projects (repo, base_branch, origin) VALUES (?, ?, ?)',
                                           (str(source), base, 'owner/repo'))
                    fingerprint = loop_manager.planning_input_fingerprint(source, base)
                    if role == 'planner':
                        workspace = planning_workspace_path(state, str(source), fingerprint)
                        workspace.parent.mkdir(parents=True, exist_ok=True)
                        git(source, 'worktree', 'add', '--detach', str(workspace), sha_a)
                        branch = None
                        with connection:
                            connection.execute("UPDATE projects SET phase='PLANNING', worktree=?, "
                                "plan_sha=?, active_sha=?, planning_attempt_fingerprint=?, planning_workspace_owned=1, "
                                "planner_session_id='planner-existing' WHERE repo=?",
                                (str(workspace), sha_a, sha_a, fingerprint, str(source)))
                    else:
                        loop_manager.register_execplans(connection, str(source), source,
                            [(plan_path, loop_manager._parse_execplan(source, plan_file))], sha_a, fingerprint)
                        branch, workspace = loop_manager.expected_execplan_workspace(state, str(source), 'G-001')
                        loop_manager.make_worktree(source, workspace, branch, base)
                        with connection:
                            connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', active_sha=?, "
                                "branch=?, worktree=?, workspace_owned=1, worker_session_id='worker-existing', "
                                "reviewer_session_id='reviewer-existing', worker_session_binding=? "
                                "WHERE repo=? AND plan_id='G-001'", (sha_a, branch, str(workspace),
                                    SessionBinding('codex', 'test-fast', 'medium').serialize(), str(source)))
                    if role != 'planner':
                        with connection:
                            connection.execute("UPDATE projects SET phase='WORKING', worktree=?, branch=?, "
                                "plan_path=?, plan_sha=?, active_sha=?, worker_session_id='worker-existing', "
                                "reviewer_session_id='reviewer-existing', worker_session_binding=? WHERE repo=?",
                                (str(workspace), branch, plan_path, sha_a, sha_a,
                                 SessionBinding('codex', 'test-fast', 'medium').serialize(), str(source)))
                    if role == 'reviewer':
                        (workspace / 'src/app.py').write_text('VALUE = 2\n', encoding='utf-8')
                        commit_all(workspace, 'Saved Worker candidate awaiting Reviewer')
                    before = loop_manager.begin_agent_scope(connection, str(source), workspace, role)
                    if role == 'worker':
                        (workspace / 'src/app.py').write_text('VALUE = 2\n', encoding='utf-8')
                    saved_result = replace(loop_manager.capture_git_snapshot(workspace),
                        scope_id=before.scope_id, scope_start_digest=before.scope_start_digest)
                    record_saved_scope_invocation(connection, source, state, workspace, role, before,
                        role + '-existing', plan_id='G-001' if role != 'planner' else None)
                    with connection:
                        connection.execute('UPDATE projects SET agent_role=?, agent_snapshot=?, '
                            'agent_result_snapshot=? WHERE repo=?',
                            (role, before.serialize(), saved_result.serialize(), str(source)))
                        if role != 'planner':
                            connection.execute('UPDATE exec_plans SET agent_role=?, agent_snapshot=?, '
                                'agent_result_snapshot=? WHERE repo=? AND plan_id=?',
                                (role, before.serialize(), saved_result.serialize(), str(source), 'G-001'))
                    from loop_manager_internal.agents import common_guidance
                    from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
                    guidance_repository = GuidanceRepository(connection)
                    context = InvocationContext(str(source), 'G-001' if role != 'planner' else None,
                                                task_id='planning:' + fingerprint if role == 'planner' else None)
                    guidance = common_guidance.resolve(guidance_repository, context, role, workspace, state)
                    for session in ('planner-existing',) if role == 'planner' else ('worker-existing', 'reviewer-existing'):
                        guidance_repository.bind_session(str(source), 'codex', session, guidance.task_id)
                    if role == 'reviewer':
                        with connection:
                            connection.execute("UPDATE exec_plans SET status='REVIEWING', "
                                "worker_result=?, verify_result=? WHERE repo=? AND plan_id='G-001'",
                                ('WORKER_OUTCOME: {"kind":"ready_for_validation"}',
                                 'historical fixture verification', str(source)))
                            connection.execute("UPDATE projects SET phase='REVIEWING', worker_result=?, "
                                "verify_result=? WHERE repo=?",
                                ('WORKER_OUTCOME: {"kind":"ready_for_validation"}',
                                 'historical fixture verification', str(source)))
                    connection.commit()
                    connection.close()

                    writer = Path(directory) / 'writer'
                    git(source, 'clone', '-c', 'core.longpaths=true', '-c', 'core.autocrlf=false',
                        str(Path(directory) / 'remote.git'), str(writer))
                    configure_identity(writer)
                    git(writer, 'checkout', base)
                    # B changes executable-independent input, retaining the saved plan definition.
                    (writer / 'README.md').write_text('Remote B\n', encoding='utf-8')
                    sha_b = commit_all(writer, 'Advance to B')
                    git(writer, 'push', 'origin', f'HEAD:{base}')
                    if advance_after_selection:
                        (writer / 'README.md').write_text('External C\n', encoding='utf-8')
                        sha_c = commit_all(writer, 'External base C after runtime B selection')
                        (state / 'advance-after-selection.json').write_text(json.dumps([str(writer), base]), encoding='utf-8')
                    config = profile_config(self)
                    # All branch/role pairs still exercise the real child. The
                    # common crash and same-SHA reuse need one process regression;
                    # registration/scope rules are paired with BootstrapRuntimeTests
                    # and BootstrapSavedScopeFixtureUnitTests below. B/C transitions
                    # retain both resumes for each role's distinct saved state.
                    # ponytail: crash/reuse process coverage is one main Worker;
                    # add a role/base case if those pre-dispatch steps gain such logic.
                    interrupted_registration = not advance_after_selection and (base, role) == ('main', 'worker')
                    if interrupted_registration:
                        (state / 'stop-after-registration').touch()
                        interrupted = self._run_child(source, manager_file, state, config)
                        self.assertNotEqual(interrupted.returncode, 0, interrupted.stdout)
                        self.assertIn('test interruption after runtime registration', interrupted.stdout)
                        self.assertFalse((state / 'resumed-agent.json').exists())
                        self.assertTrue(list((state / 'manager-git-journal').glob('*/active.json')))
                    attempts = 2 if advance_after_selection or interrupted_registration else 1
                    for attempt in range(attempts):
                        completed = self._run_child(source, manager_file, state, config)
                        proof_path = state / 'resumed-agent.json'
                        self.assertTrue(proof_path.exists(), completed.stdout)
                        proof = json.loads(proof_path.read_text())
                        expected_role = role
                        self.assertEqual(proof['role'], expected_role)
                        self.assertEqual(proof['session'], expected_role + '-existing')
                        self.assertEqual(proof['sha'], sha_c if advance_after_selection and attempt else sha_b)
                        self.assertEqual(proof['worktree'], str(workspace))
                        self.assertIn('runtime mutation 검증', completed.stdout)
                        if attempt == 0:
                            self.assertIn('runtime scope 수락', completed.stdout)
                        if role != 'planner':
                            self.assertEqual((workspace / 'src/app.py').read_text(), 'VALUE = 2\n')
                        with contextlib.closing(sqlite3.connect(state / 'loop-manager.db')) as check, check:
                            self.assertEqual(check.execute('SELECT COUNT(*) FROM project_blocks').fetchone()[0], 0, completed.stdout)
                            if role != 'planner':
                                row = check.execute("SELECT status, worker_session_id, reviewer_session_id, retry_count "
                                    "FROM exec_plans WHERE plan_id='G-001'").fetchone()
                                self.assertEqual(row, ('REVIEWING' if role == 'reviewer' else 'IMPLEMENTING',
                                                       'worker-existing', 'reviewer-existing', 0))
                        # A later parent must dispatch again, using the same SHA
                        # in the crash/reuse case or C after the B/C transition.
                        proof_path.unlink()
                    # Child instrumentation stays in this owned fixture. Forward
                    # original PID/time values so nested CLI durations aren't
                    # presented as independent end-to-end time.
                    records = []
                    parent_writer = _current_writer.get()
                    for path in sorted((state / 'bootstrap-costs').glob('*.jsonl')):
                        contents = path.read_text(encoding='utf-8')
                        records.extend(json.loads(line) for line in contents.splitlines())
                        if parent_writer is not None:
                            parent_writer.stream.write(contents)
                    if parent_writer is not None:
                        parent_writer.stream.flush()
                    ends = [event for event in records if event['kind'] == 'phase_end']
                    self.assertEqual(sum(event['name'] == 'bootstrap:parent_cli' for event in ends),
                                     attempts + int(interrupted_registration))
                    self.assertEqual(sum(event['name'] == 'bootstrap:runtime_child_cli' for event in ends), attempts)
                    self.assertTrue(any(event['kind'] == 'git_cost' and event['count'] > 0 for event in records))

    def test_remote_bootstrap_child_resumes_saved_scope_without_identity_injection(self):
        self._assert_saved_scope_resume()

    def test_selected_runtime_b_survives_external_base_c_before_child_prevalidation(self):
        self._assert_saved_scope_resume(advance_after_selection=True)


class BootstrapSavedScopeFixtureUnitTests(unittest.TestCase):
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
                expected = dict(repo='repo', plan_id=plan_id, role=role, provider='codex',
                                session_id=role + '-existing', worktree=str(worktree.resolve()),
                                repository_identity=repository_identity, start_digest='c' * 64)
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
                    self.assertIsNone(scope_validation._agent_git_resources(
                        connection, Path('repo'), worktree, before, role, effects=effects))
                    owner['phase'] = 'REVIEWING'
                owner[role + '_session_id'] = 'unmatched-session'
                self.assertIsNone(scope_validation._agent_git_resources(
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
                          refs={'refs/heads/loop-manager/G-047': ('a', 'commit')})
        current = replace(before, head='b', refs={'refs/heads/loop-manager/G-047': ('b', 'commit')})
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
        for status in (RebaseStatus.WAITING, RebaseStatus.COMPLETE):
            effects._current_rebase_service.return_value.require_owned.return_value = (
                None, SimpleNamespace(status=status))
            if status is RebaseStatus.WAITING:
                with self.assertRaises(RuntimeError):
                    scope_lifecycle.begin_agent_scope(connection, 'repo', Path('/managed'), 'worker', effects=effects)
                effects.update_project.assert_not_called()
                continue
            resumed = scope_lifecycle.begin_agent_scope(
                connection, 'repo', Path('/managed'), 'worker', effects=effects)
            self.assertNotEqual(resumed.scope_id, before.scope_id)
            self.assertEqual(effects.validate_agent_scope.call_count, 2)
            self.assertEqual(effects.validate_agent_scope.call_args_list[0].args[0], 'reviewer')
            first_check = effects.validate_agent_scope.call_args_list[0]
            self.assertEqual(first_check.args[2].refs, first_check.args[3].refs)
            self.assertEqual(first_check.args[2].refs['refs/remotes/origin/main'], ('b', ''))
            self.assertNotIn('refs/remotes/origin/main', before.refs)
            self.assertEqual(effects.update_project.call_args.kwargs['agent_role'], 'worker')
            audit = checkpoint.get('repo', 'G-047').snapshot['current_rebase_agent_baseline']
            self.assertEqual(audit['role'], 'reviewer')
            self.assertEqual(audit['original'], hashlib.sha256(before.serialize().encode()).hexdigest())
        effects.reset_mock()
        effects.validate_agent_scope.side_effect = loop_manager.AgentScopeError('Reviewer write rejected')
        prior = checkpoint.get('repo', 'G-047')
        with self.assertRaises(loop_manager.AgentScopeError):
            scope_lifecycle.begin_agent_scope(connection, 'repo', Path('/managed'), 'worker', effects=effects)
        effects.update_project.assert_not_called()
        self.assertEqual(checkpoint.get('repo', 'G-047'), prior)
        effects.validate_agent_scope.side_effect = None
        effects.reset_mock()
        checkpoint.record('repo', 'G-047', expected_generation=checkpoint.get('repo', 'G-047').generation,
                          operation=None, expected_head='b', target_base_oid='b', observed_remote_oid=None,
                          snapshot={})
        with self.assertRaises(RuntimeError):
            scope_lifecycle.begin_agent_scope(connection, 'repo', Path('/managed'), 'worker', effects=effects)
        effects.update_project.assert_not_called()
