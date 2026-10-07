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


class OriginIsolationScopeTest(OriginScopeFixture, unittest.TestCase):
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
                    runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id=mutation_id,
                    )
                    loop_manager._write_process_lock_identity(handle, f"runtime:{mutation_id}")
                    with mock.patch.object(loop_manager, "ROOT", runtime.path):
                        identity = loop_manager._bootstrap_service().validate_runtime_mutation(
                            source, state_dir, selected_sha, mutation_id,
                        )
                        loop_manager.validate_persisted_agent_scope(
                            connection, source, state_dir, str(source),
                            runtime_mutation=identity,
                        )
                    saved = loop_manager.GitSnapshot.deserialize(connection.execute(
                        "SELECT agent_snapshot FROM projects WHERE repo = ?", (str(source),)
                    ).fetchone()[0])
                    self.assertEqual(saved, before)
                    audit = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
                        str(source), '').snapshot['manager_metadata_agent_baseline']
                    comparison = loop_manager.GitSnapshot.deserialize(audit['snapshot'])
                    self.assertIn(str(runtime.path), comparison.worktrees)
                    self.assertEqual(comparison.refs["refs/remotes/origin/main"][0], selected_sha)

                with mock.patch.object(loop_manager, "ROOT", runtime.path):
                    with self.assertRaisesRegex(RuntimeError, "process lock"):
                        loop_manager._bootstrap_service().validate_runtime_mutation(
                            source, state_dir, selected_sha, mutation_id,
                        )
                with self.assertRaisesRegex(loop_manager.AgentScopeError, "process lock"):
                    loop_manager.validate_persisted_agent_scope(
                        connection, source, state_dir, str(source),
                        runtime_mutation=identity,
                    )

                with loop_manager.process_lock(state_dir) as handle:
                    next_id = "b" * 32
                    reused = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id=next_id,
                    )
                    self.assertEqual(reused.path, runtime.path)
                    loop_manager._write_process_lock_identity(handle, f"runtime:{next_id}")
                    with mock.patch.object(loop_manager, "ROOT", reused.path):
                        identity = loop_manager._bootstrap_service().validate_runtime_mutation(
                            source, state_dir, selected_sha, next_id,
                        )
                        loop_manager.validate_persisted_agent_scope(
                            connection, source, state_dir, str(source),
                            runtime_mutation=identity,
                        )
                self.assertEqual(
                    git(source, "worktree", "list", "--porcelain").count(runtime.path.as_posix()), 1,
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
                        runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(
                            source, state_dir, mutation_id=mutation_id,
                        )
                        loop_manager._write_process_lock_identity(handle, f"runtime:{mutation_id}")
                        with mock.patch.object(loop_manager, "ROOT", runtime.path):
                            identity = loop_manager._bootstrap_service().validate_runtime_mutation(
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
                    first = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id=first_id,
                    )
                with loop_manager.process_lock(state_dir, identity=f"bootstrap:{second_id}") as handle:
                    reused = loop_manager._bootstrap_service().prepare_runtime_worktree(
                        source, state_dir, mutation_id=second_id,
                    )
                    self.assertEqual(reused.path, first.path)
                    loop_manager._write_process_lock_identity(handle, f"runtime:{second_id}")
                    with mock.patch.object(loop_manager, "ROOT", reused.path):
                        identity = loop_manager._bootstrap_service().validate_runtime_mutation(
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
                        runtime = loop_manager._bootstrap_service().prepare_runtime_worktree(source, state_dir)
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
                            identity = loop_manager._bootstrap_service().validate_runtime_mutation(
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
                        self.assertEqual(saved["agent_snapshot"], before.serialize())
                connection.close()


    def test_git_lock_outside_synchronizer_is_transient_and_preserves_state(self):
        connection = sqlite3.connect(":memory:")
        with mock.patch(
            "loop_manager._run_project_step",
            side_effect=loop_manager.GitLockError("index.lock exists"),
        ):
            result = loop_manager.run_project_result(
                connection,
                Path("/state"),
                str(Path("/repo").resolve()),
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
                (str(Path("/repo").resolve()),),
            ).fetchone(),
            ("git_lock", "pause"),
        )



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
        self.validate()  # Verified isolation and live fast-forward preserve this scope.
        with mock.patch.object(loop_manager, '_agent_git_resources', return_value=None):
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
        self.connection.execute(loop_manager.SCHEMA)
        self.source = Path('/source').resolve()
        self.worktree = Path('/managed').resolve()
        self.ref = 'refs/remotes/origin/main'
        self.before = loop_manager.GitSnapshot('owned', 'loop-manager/G-092', {},
            {'app.py': (('unstaged',), 'worker-file')},
            {self.ref: ('a' * 40, '')}, {str(self.worktree): ('HEAD=owned',)}, {}, {})
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

    def record_fetch(self, *, interrupted=False, before_oid='a' * 40, target_oid='b' * 40,
                     sqlite=False):
        current = {'oid': before_oid}
        def apply(token):
            current['oid'] = target_oid
            self.reflog = f"{target_oid}\0loop-manager-base-fetch:{token}\n" + self.reflog
        options = dict(owner='project', kind='project_fetch', repo=str(self.source), target=self.ref,
            before={'oid': before_oid}, expected={'oid': target_oid},
            observe=lambda: dict(current), apply=apply, marker=lambda _intent: True)
        if sqlite:
            from types import SimpleNamespace
            loop_manager._ensure_cleanup_pending_schema(self.connection)
            def git(_repo, *args, **kwargs):
                if args[:1] == ('ls-remote',):
                    output = target_oid + '\trefs/heads/main\n'
                elif args[:1] == ('rev-parse',):
                    output = target_oid if args[-1] == 'FETCH_HEAD^{commit}' else current['oid']
                elif args[:1] == ('update-ref',):
                    self.assertEqual(args[-3:], (self.ref, target_oid, before_oid))
                    current['oid'] = target_oid
                    self.reflog = target_oid + '\0' + args[3] + '\n' + self.reflog
                    output = ''
                elif args[:2] == ('reflog', 'show'):
                    output = self.reflog
                elif args == ('fetch', '--no-tags', '--refmap=', 'origin', 'refs/heads/main'):
                    output = ''
                else:
                    raise AssertionError('Unexpected Git call: ' + repr(args))
                return SimpleNamespace(stdout=output, returncode=0)
            with mock.patch.object(loop_manager, 'git', side_effect=git):
                before = replace(self.before, refs={self.ref: (before_oid, '')})
                identity = loop_manager._record_manager_base_fetch_intent(
                    self.connection, self.source, str(self.source), 'G-092', 'main', before)
                if interrupted:
                    with mock.patch.object(manager_git_journal, '_finish', side_effect=RuntimeError('interrupted')):
                        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                            loop_manager._journaled_manager_base_fetch(
                                self.connection, self.source, self.state, identity, 'main', 'G-092')
                else:
                    loop_manager._journaled_manager_base_fetch(
                        self.connection, self.source, self.state, identity, 'main', 'G-092')
            return
        if interrupted:
            with mock.patch.object(manager_git_journal, '_finish', side_effect=RuntimeError('interrupted')):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    manager_git_journal.execute(self.state, **options)
        else:
            manager_git_journal.execute(self.state, **options)

    def reconcile(self):
        return loop_manager._reconciled_manager_scope_metadata(
            self.connection, self.source, self.state, str(self.source), 'G-092', self.before, self.current)

    def test_resume_records_independent_metadata_before_a_failed_attempt(self):
        from loop_manager_internal.agents import agent_scope_decision
        worktree = self.state / 'managed'
        worktree.mkdir()
        own_ref = 'refs/heads/loop-manager/G-092'
        independent_ref = 'refs/heads/main'
        self.before = replace(self.before, refs={self.ref: ('a' * 40, ''),
            own_ref: ('owned', ''), independent_ref: ('old-source', '')},
            worktrees={str(worktree): ('HEAD=owned',), '/source': ('HEAD=old-source',)})
        self.current = replace(self.before,
            refs={**self.before.refs, independent_ref: ('new-source', '')},
            worktrees={**self.before.worktrees, '/source': ('HEAD=new-source',)})
        self.prepare_project(worktree)
        resources = agent_scope_decision.used_git_resources(role='worker', phase='WORKING',
            base_branch='main', branch=self.before.branch, worktree_key=str(worktree))
        for proof in (None, resources):
            with self.subTest(isolated=proof is not None), \
                    mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=self.current), \
                    mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=self.state), \
                    mock.patch.object(loop_manager, '_agent_git_resources', return_value=proof):
                effective = loop_manager.begin_agent_scope(self.connection, str(self.source), worktree, 'worker')
            if proof is None:
                self.assertEqual(effective, self.before)
                continue
            self.assertEqual(effective.refs, self.current.refs)
            self.assertEqual(effective.worktrees, self.current.worktrees)
            self.assertEqual(effective.dirty, self.before.dirty)
            checkpoints = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
            prior = checkpoints.get(str(self.source), 'G-092')
            with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=self.current), \
                    mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=self.state), \
                    mock.patch.object(loop_manager, '_agent_git_resources', return_value=proof):
                self.assertEqual(loop_manager.begin_agent_scope(self.connection, str(self.source), worktree, 'worker'), effective)
            self.assertEqual(checkpoints.get(str(self.source), 'G-092'), prior)
            self.assertEqual(self.connection.execute('SELECT agent_snapshot FROM projects').fetchone()[0],
                             self.before.serialize())
            audit = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(self.connection).get(
                str(self.source), 'G-092').snapshot
            self.assertEqual(agent_scope_decision.manager_comparison_baseline(
                self.before, self.before, audit, 'worker'), effective)
            # Loss of proof after a failed attempt cannot attribute pre-call
            # metadata to this call, or hide any new shared write during it.
            loop_manager.validate_agent_scope('worker', worktree, effective, self.current,
                selected_plan_path='docs/plans/[G-092] Work.md')
            for changed in (replace(self.current, refs={**self.current.refs, independent_ref: ('during-call', '')}),
                            replace(self.current, refs={**self.current.refs, own_ref: ('wrong-owner', '')}),
                            replace(self.current, worktrees={**self.current.worktrees, '/source': ('HEAD=during-call',)})):
                with self.assertRaises(loop_manager.AgentScopeError):
                    loop_manager.validate_agent_scope('worker', worktree, effective, changed,
                        selected_plan_path='docs/plans/[G-092] Work.md')

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
        rebase = manager_git_journal._slot(self.state, str(self.source), 'managed_rebase', str(self.worktree))
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

    def test_new_agent_baseline_ignores_old_fetch_before_external_change(self):
        self.record_fetch()
        self.reflog = 'c' * 40 + '\0external update\n' + self.reflog
        self.before = replace(self.before, refs={self.ref: ('c' * 40, '')})
        self.record_fetch(before_oid='c' * 40, target_oid='d' * 40)
        self.current = replace(self.current, refs={self.ref: ('d' * 40, '')})
        audit = {path: path.read_bytes() for path in self.state.rglob('*.json')}
        self.assertEqual(self.reconcile().refs[self.ref], ('d' * 40, ''))
        self.assertEqual(self.reconcile().dirty, self.before.dirty)
        self.assertEqual({path: path.read_bytes() for path in self.state.rglob('*.json')}, audit)
        # The same external change is unsafe when it follows this baseline.
        self.before = replace(self.before, refs={self.ref: ('a' * 40, '')})
        with self.assertRaises(manager_git_journal.MutationEvidenceError):
            self.reconcile()

    def test_old_fetch_from_repeated_baseline_oid_does_not_shadow_current_fetch(self):
        self.record_fetch()
        self.reflog = 'a' * 40 + '\0external revert\n' + self.reflog
        self.record_fetch(target_oid='d' * 40)
        self.current = replace(self.current, refs={self.ref: ('d' * 40, '')})
        self.assertEqual(self.reconcile().refs[self.ref], ('d' * 40, ''))

    def test_mixed_fetch_tokens_accept_both_orders_and_interrupted_sqlite_effect(self):
        state = self.state
        for order in ((False, True), (True, False), (False, True, False)):
            for interrupted in (False, True):
                with self.subTest(order=order, interrupted=interrupted):
                    self.state = state / (str(order) + str(interrupted))
                    loop_manager._ensure_cleanup_pending_schema(self.connection)
                    self.connection.execute('DELETE FROM manager_base_fetches')
                    self.reflog = ''
                    oids = ['a' * 40, 'b' * 40, 'c' * 40, 'd' * 40]
                    for index, sqlite_fetch in enumerate(order):
                        self.record_fetch(before_oid=oids[index], target_oid=oids[index + 1],
                            sqlite=sqlite_fetch, interrupted=interrupted and sqlite_fetch)
                    self.current = replace(self.current, refs={self.ref: (oids[len(order)], '')})
                    audit = {path: path.read_bytes() for path in self.state.rglob('*.json')}
                    self.assertEqual(self.reconcile().refs[self.ref], (oids[len(order)], ''))
                    self.assertEqual(self.before.refs[self.ref], ('a' * 40, ''))
                    self.assertEqual({path: path.read_bytes() for path in self.state.rglob('*.json')}, audit)

    def test_mixed_fetch_chain_requires_exact_sqlite_identity_and_reflog(self):
        self.record_fetch()
        self.record_fetch(before_oid='b' * 40, target_oid='c' * 40, sqlite=True)
        self.record_fetch(before_oid='c' * 40, target_oid='d' * 40)
        self.current = replace(self.current, refs={self.ref: ('d' * 40, '')})
        self.assertEqual(self.reconcile().refs[self.ref], ('d' * 40, ''))
        for column, value in (('repo', '/other'), ('plan_id', 'G-099'),
                ('refname', 'refs/remotes/origin/other'), ('phase', 'aborted'),
                ('action_token', 'f' * 32), ('before_ref', '["' + 'a' * 40 + '", ""]'),
                ('target_oid', 'e' * 40)):
            with self.subTest(column=column):
                self.connection.execute('SAVEPOINT mutation')
                try:
                    self.connection.execute('UPDATE manager_base_fetches SET ' + column + ' = ?', (value,))
                    with self.assertRaises(manager_git_journal.MutationEvidenceError):
                        self.reconcile()
                finally:
                    self.connection.execute('ROLLBACK TO mutation')
                    self.connection.execute('RELEASE mutation')
        reflog = self.reflog
        newest, previous = reflog.split('\n', 1)
        self.reflog = newest + '\n' + 'b' * 40 + '\0external round trip\n' + previous
        with self.assertRaises(manager_git_journal.MutationEvidenceError):
            self.reconcile()
        self.reflog = reflog
        self.assertEqual(self.reconcile().refs[self.ref], ('d' * 40, ''))

    def test_archive_push_keeps_tracking_ref_for_owned_fetch_and_later_scope_refresh(self):
        from types import SimpleNamespace
        from loop_manager_internal.catalog import execplan_archive

        self.record_fetch()
        commit = 'c' * 40
        prepared = execplan_archive.PreparedArchive('archive', 'b' * 40, 'tree', commit)
        port = loop_manager._ExecPlanArchiveGitPort(self.source, self.state, str(self.source))
        remote, fetched = 'b' * 40, None
        url = 'https://example.test/repo.git'

        def git(_repo, *args, **kwargs):
            nonlocal remote, fetched
            output = ''
            if args == ('remote', 'get-url', '--push', '--all', 'origin'):
                output = url
            elif args[:1] == ('push',):
                self.assertEqual(args, ('push', '--', url, commit + ':refs/heads/main'))
                self.assertEqual(self.current.refs[self.ref], ('b' * 40, ''))
                remote = commit
            elif args[:1] == ('ls-remote',):
                output = remote + '\trefs/heads/main\n'
            elif args == ('fetch', '--no-tags', '--refmap=', 'origin', 'refs/heads/main'):
                fetched = remote
            elif args == ('rev-parse', '--verify', 'FETCH_HEAD^{commit}'):
                output = fetched
            elif args == ('rev-parse', '--verify', self.ref + '^{commit}'):
                output = self.current.refs[self.ref][0]
            elif args[:1] == ('update-ref',):
                self.assertEqual(args[-3:], (self.ref, commit, 'b' * 40))
                self.current = replace(self.current, refs={self.ref: (commit, '')})
                self.reflog = commit + '\0' + args[3] + '\n' + self.reflog
            elif args[:2] == ('reflog', 'show'):
                output = self.reflog
            else:
                raise AssertionError('Unexpected Git call: ' + repr(args))
            return SimpleNamespace(returncode=0, stdout=output)

        self.git.side_effect = git
        result = port.push_fast_forward(prepared, 'main')
        self.assertTrue(result.success)
        self.assertEqual(self.reconcile().refs[self.ref], (commit, ''))
        self.assertEqual(self.before.refs[self.ref], ('a' * 40, ''))
        self.assertEqual(self.reconcile().dirty, self.before.dirty)
        # A later unowned transition still invalidates the fetch chain.
        self.reflog = commit + '\0external update\n' + self.reflog
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
        self.prepare_project(self.worktree)
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=self.current), \
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=self.state), \
                mock.patch.object(loop_manager, '_agent_git_resources', return_value=None):
            effective = loop_manager.begin_agent_scope(self.connection, str(self.source), self.worktree, 'worker')
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

    def planner_ports(self):
        from types import SimpleNamespace
        worktree = self.state / 'planner'
        worktree.mkdir()
        self.before = replace(self.before, branch=None, dirty={})
        self.current = replace(self.before, refs={self.ref: ('b' * 40, '')})
        self.prepare_project(worktree)
        self.connection.execute("UPDATE projects SET phase = 'PLANNING', plan_path = NULL, "
            "branch = NULL, agent_role = 'planner', planner_session_id = 'saved-planner', "
            "agent_input_fingerprint = 'same-input'")
        context = SimpleNamespace(repo=str(self.source), repo_path=self.source,
                                  state_dir=self.state, base_branch='main')
        return worktree, loop_manager._PlanningAgentPorts(self.connection, context, None)

    def test_planner_resume_and_saved_result_use_proven_fetch_baseline(self):
        worktree, ports = self.planner_ports()
        self.record_fetch(interrupted=True)
        original = self.before.serialize()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, 'validate_managed_worktree'):
            for role in ('planner', 'maintenance_planner'):
                with self.subTest(role=role):
                    self.connection.execute('UPDATE projects SET agent_role = ?', (role,))
                    resumed = ports.begin_scope(role, worktree)
                    self.assertEqual(resumed.refs, self.current.refs)
                    self.assertIsNone(ports.finish_scope(role, worktree, resumed).error)
                    self.assertIsNone(ports.validate_saved_scope(role, worktree, original).error)
                    row = self.connection.execute('SELECT * FROM projects').fetchone()
                    self.assertEqual(row['agent_snapshot'], original)
                    self.assertEqual(row['planner_session_id'], 'saved-planner')
                    self.assertEqual(row['agent_input_fingerprint'], 'same-input')

    def test_planner_resume_rejects_unproven_refs_and_saved_file_violation(self):
        worktree, ports = self.planner_ports()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, 'validate_managed_worktree'):
            resumed = ports.begin_scope('planner', worktree)
            self.assertIsNotNone(ports.finish_scope('planner', worktree, resumed).error)
            self.assertIsNotNone(ports.validate_saved_scope('planner', worktree, self.before.serialize()).error)
            self.record_fetch()
            self.current = replace(self.current, dirty={'app.py': (('unstaged',), 'forbidden')})
            resumed = ports.begin_scope('planner', worktree)
            self.assertIsNotNone(ports.finish_scope('planner', worktree, resumed).error)
            self.assertIsNotNone(ports.validate_saved_scope('planner', worktree, self.before.serialize()).error)

    def test_planner_finish_keeps_baseline_fixed_during_agent_turn(self):
        worktree, ports = self.planner_ports()
        self.record_fetch()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, 'validate_managed_worktree'):
            resumed = ports.begin_scope('planner', worktree)
            self.record_fetch(before_oid='b' * 40, target_oid='c' * 40)
            self.current = replace(self.current, refs={self.ref: ('c' * 40, '')})
            self.assertIsNotNone(ports.finish_scope('planner', worktree, resumed).error)

    def test_saved_planner_result_survives_block_release_and_common_prevalidation(self):
        worktree, ports = self.planner_ports()
        self.record_fetch()
        original = self.before.serialize()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, '_config_entries', return_value={}):
            for role in ('planner', 'maintenance_planner'):
                with self.subTest(role=role):
                    self.connection.execute('UPDATE projects SET agent_role = ?', (role,))
                    resumed = ports.begin_scope(role, worktree)
                    self.assertEqual(resumed.refs, self.current.refs)
                    saved = self.current.serialize()
                    self.connection.execute('UPDATE projects SET agent_result_snapshot = ?, '
                        'planning_validation_error = NULL', (saved,))
                    loop_manager.scoped_blocking.record_project_block(
                        self.connection, str(self.source),
                        loop_manager.BlockReasonCode.SHARED_REF_MUTATION,
                        'stale Planner baseline', 'same-input', plan_id=None,
                        previous_phase='PLANNING')
                    self.assertTrue(loop_manager.resume_project_block_if_safe(
                        self.connection, self.source, self.state, str(self.source)))
                    loop_manager.validate_persisted_agent_scope(
                        self.connection, self.source, self.state, str(self.source))
                    row = self.connection.execute('SELECT * FROM projects').fetchone()
                    self.assertEqual(row['agent_snapshot'], original)
                    self.assertEqual(row['agent_result_snapshot'], saved)
                    self.assertEqual(row['planner_session_id'], 'saved-planner')
                    self.assertEqual(row['agent_input_fingerprint'], 'same-input')
                    for changed in (
                            replace(self.current, refs={self.ref: ('c' * 40, '')}),
                            replace(self.current, dirty={'app.py': (('unstaged',), 'forbidden')})):
                        self.connection.execute('UPDATE projects SET agent_result_snapshot = ?',
                                                (changed.serialize(),))
                        with self.assertRaises(loop_manager.AgentScopeError):
                            loop_manager.validate_persisted_agent_scope(
                                self.connection, self.source, self.state, str(self.source))
                    self.connection.execute('UPDATE projects SET agent_result_snapshot = ?', (saved,))

    def test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation(self):
        self.source = self.state / 'source'
        self.source.mkdir()
        runtime_path = self.state / 'runtime'
        self.before = replace(self.before, head='source-head', branch=None, dirty={},
            refs={self.ref: ('a' * 40, ''),
                  'refs/remotes/origin/HEAD': ('a' * 40, self.ref)},
            worktrees={str(self.source): ('HEAD=source-head', 'detached=True')})
        self.current = replace(self.before,
            refs={self.ref: ('b' * 40, ''), 'refs/remotes/origin/HEAD': ('b' * 40, self.ref)},
            worktrees={**self.before.worktrees, str(runtime_path):
                ('HEAD=' + 'b' * 40, 'detached=True', 'locked=runtime-marker')})
        self.prepare_project(self.source)
        self.connection.execute("UPDATE projects SET phase = 'PLANNING', agent_role = 'planner', "
            'branch = NULL, plan_path = NULL, agent_result_snapshot = ?', (self.current.serialize(),))
        loop_manager.scoped_blocking.record_project_block(self.connection, str(self.source),
            loop_manager.BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION,
            'Agent 범위 선검증 실패:\nruntime', 'unchanged', plan_id=None, previous_phase='PLANNING')
        mutation = loop_manager.RuntimeMutation(self.source, runtime_path, 'b' * 40, None,
            legacy=True, registration_lock_reason='runtime-marker')
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, '_config_entries', return_value={}), \
                mock.patch.object(loop_manager, '_agent_git_resources', return_value=None), \
                mock.patch.object(loop_manager, 'git_bytes', return_value=b''), \
                mock.patch.object(loop_manager.scope_recovery, '_held_process_lock_identity', return_value='0'), \
                mock.patch.object(bootstrap.BootstrapService, 'validate_runtime_worktree'), \
                mock.patch.object(bootstrap.BootstrapService, '_validated_runtime_registration_marker',
                                  return_value='runtime-marker') as marker, \
                mock.patch.object(loop_manager.runtime_scope, 'proven_runtime_recreation', return_value=False):
            project = self.connection.execute('SELECT * FROM projects').fetchone()
            block = loop_manager.scoped_blocking.project_block(self.connection, str(self.source))
            self.assertTrue(loop_manager.reconcile_false_positive_runtime_block(
                self.connection, self.source, self.state, str(self.source), project, block, mutation))
            self.assertEqual(self.connection.execute('SELECT agent_result_snapshot FROM projects').fetchone()[0],
                             self.current.serialize())
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, self.source, self.state, str(self.source)))
            for changed in (
                    replace(self.current, refs={**self.current.refs, 'refs/tags/foreign': ('foreign', '')}),
                    replace(self.current, refs={**self.current.refs, self.ref: ('foreign', '')}),
                    replace(self.current, head='foreign')):
                with self.subTest(changed=changed):
                    self.connection.execute('UPDATE projects SET agent_snapshot = ?, agent_result_snapshot = ?',
                        (self.before.serialize(), changed.serialize()))
                    with self.assertRaises(loop_manager.AgentScopeError):
                        loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                            str(self.source), runtime_mutation=mutation)
                    self.assertEqual(self.connection.execute('SELECT agent_snapshot FROM projects').fetchone()[0],
                                     self.before.serialize())
            self.connection.execute('UPDATE projects SET agent_result_snapshot = ?', (self.current.serialize(),))
            marker.return_value = 'foreign-marker'
            with self.assertRaisesRegex(loop_manager.AgentScopeError, 'runtime mutation 검증 실패'):
                loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                    str(self.source), runtime_mutation=mutation)
            marker.return_value = 'runtime-marker'
            self.current = replace(self.current,
                worktrees={**self.current.worktrees, '/foreign': ('HEAD=foreign',)})
            self.connection.execute('UPDATE projects SET agent_result_snapshot = ?', (self.current.serialize(),))
            project = self.connection.execute('SELECT * FROM projects').fetchone()
            self.assertFalse(loop_manager.reconcile_false_positive_runtime_block(
                self.connection, self.source, self.state, str(self.source), project, block, mutation))

    def test_runtime_revalidation_binds_worker_checkpoint_to_original_start(self):
        self.source = self.state / 'source'
        self.source.mkdir()
        runtime = self.state / 'runtime'
        own_ref = 'refs/heads/loop-manager/G-092'
        for base in ('main', 'develop'):
            with self.subTest(base=base):
                ref = 'refs/remotes/origin/' + base
                self.before = replace(self.before, dirty={}, index={},
                    refs={own_ref: ('owned', ''), ref: ('a' * 40, ''),
                          'refs/remotes/origin/HEAD': ('a' * 40, ref)},
                    worktrees={str(self.source): ('HEAD=owned', 'branch=' + own_ref)})
                result = replace(self.before, dirty={'src/app.py': (('staged',), 'worker')},
                    index={'src/app.py': ('H 100644 ' + 'c' * 40 + ' 0',)})
                self.current = replace(result,
                    refs={**result.refs, ref: ('b' * 40, ''), 'refs/remotes/origin/HEAD': ('b' * 40, ref)},
                    worktrees={**result.worktrees, str(runtime): ('HEAD=' + 'b' * 40,
                        'detached=True', 'locked=runtime-marker')})
                self.connection.execute('DELETE FROM projects')
                self.prepare_project(self.source)
                checkpoint = loop_manager.ManagerGitCheckpoint(result, frozenset({'src/app.py'}))
                self.connection.execute('DELETE FROM exec_plans')
                self.connection.execute('INSERT INTO exec_plans '
                    '(repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, '
                    'branch, worktree, workspace_owned, agent_role, agent_snapshot) '
                    "VALUES (?, 'G-092', 'docs/plans/[G-092] Work.md', 'Work', '[]', '[]', 'normal', 'hash', "
                    "'IMPLEMENTING', 'loop-manager/G-092', ?, 1, 'worker', ?)",
                    (str(self.source), str(self.source), self.before.serialize()))
                self.connection.execute('UPDATE projects SET base_branch=?, agent_result_snapshot=?, agent_manager_snapshot=?',
                    (base, result.serialize(), checkpoint.serialize()))
                mutation = loop_manager.RuntimeMutation(self.source, runtime, 'b' * 40, None,
                    legacy=True, registration_lock_reason='runtime-marker')
                with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                        mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False), \
                        mock.patch.object(loop_manager, 'requirement_archive_candidates', return_value=()), \
                        mock.patch.object(loop_manager, '_config_entries', return_value={}), \
                        mock.patch.object(loop_manager, '_agent_git_resources', return_value=None), \
                        mock.patch.object(loop_manager, 'git_bytes', return_value=b''), \
                        mock.patch.object(loop_manager.scope_recovery, '_held_process_lock_identity', return_value='0'), \
                        mock.patch.object(bootstrap.BootstrapService, 'validate_runtime_worktree'), \
                        mock.patch.object(bootstrap.BootstrapService, '_validated_runtime_registration_marker',
                                          return_value='runtime-marker'), \
                        mock.patch.object(loop_manager.runtime_scope, 'proven_runtime_recreation', return_value=False):
                    original = dict(self.connection.execute('SELECT * FROM projects').fetchone())
                    for _ in range(2):
                        loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                            str(self.source), runtime_mutation=mutation)
                    saved = dict(self.connection.execute('SELECT * FROM projects').fetchone())
                    for field in ('agent_snapshot', 'agent_result_snapshot', 'agent_manager_snapshot'):
                        self.assertEqual(saved[field], original[field])
                    effective = loop_manager._reconciled_manager_scope_metadata(
                        self.connection, self.source, self.state, str(self.source), 'G-092', result, self.current)
                    self.assertEqual(effective.refs, self.current.refs)
                    self.assertEqual(effective.index, result.index)
                    self.assertEqual(effective.dirty, result.dirty)
                    altered = replace(result, refs={**result.refs, ref: ('foreign', '')})
                    self.connection.execute('UPDATE projects SET agent_manager_snapshot=?',
                        (replace(checkpoint, snapshot=altered).serialize(),))
                    with self.assertRaises(loop_manager.AgentScopeError):
                        loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                            str(self.source), runtime_mutation=mutation)

    def test_runtime_prevalidation_persists_all_operands_and_is_idempotent(self):
        self.source = self.state / 'source'
        self.source.mkdir()
        runtime_path = self.state / 'runtime'
        for base in ('main', 'develop'):
            with self.subTest(base=base):
                ref = 'refs/remotes/origin/' + base
                self.before = replace(self.before, head='source-head', branch=None, dirty={},
                    refs={ref: ('a' * 40, ''), 'refs/remotes/origin/HEAD': ('a' * 40, ref)},
                    worktrees={str(self.source): ('HEAD=source-head', 'detached=True')})
                self.current = replace(self.before,
                    refs={ref: ('b' * 40, ''), 'refs/remotes/origin/HEAD': ('b' * 40, ref)},
                    worktrees={**self.before.worktrees, str(runtime_path):
                        ('HEAD=' + 'b' * 40, 'detached=True', 'locked=runtime-marker')})
                self.connection.execute('DELETE FROM projects')
                self.prepare_project(self.source)
                saved_checkpoint = loop_manager.ManagerGitCheckpoint(self.before, frozenset())
                self.connection.execute("UPDATE projects SET phase='PLANNING', agent_role='planner', "
                    'branch=NULL, plan_path=NULL, base_branch=?, agent_result_snapshot=?, '
                    'agent_manager_snapshot=?', (base, self.before.serialize(), saved_checkpoint.serialize()))
                mutation = loop_manager.RuntimeMutation(self.source, runtime_path, 'b' * 40, None,
                    legacy=True, registration_lock_reason='runtime-marker')
                with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                        mock.patch.object(loop_manager, '_config_entries', return_value={}), \
                        mock.patch.object(loop_manager, '_agent_git_resources', return_value=None), \
                        mock.patch.object(loop_manager, 'git_bytes', return_value=b''), \
                        mock.patch.object(loop_manager.scope_recovery, '_held_process_lock_identity', return_value='0'), \
                        mock.patch.object(bootstrap.BootstrapService, 'validate_runtime_worktree'), \
                        mock.patch.object(bootstrap.BootstrapService, '_validated_runtime_registration_marker',
                                          return_value='runtime-marker'), \
                        mock.patch.object(loop_manager.runtime_scope, 'proven_runtime_recreation', return_value=False):
                    original_fields = dict(self.connection.execute('SELECT * FROM projects').fetchone())
                    loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                        str(self.source), runtime_mutation=mutation)
                    saved = self.connection.execute('SELECT * FROM projects').fetchone()
                    for key, value in original_fields.items():
                        if key not in {'agent_snapshot', 'agent_result_snapshot', 'agent_manager_snapshot'}:
                            self.assertEqual(saved[key], value, key)
                    self.assertEqual(saved['agent_snapshot'], self.before.serialize())
                    self.assertEqual(saved['agent_result_snapshot'], self.before.serialize())
                    self.assertEqual(loop_manager.ManagerGitCheckpoint.deserialize(
                        saved['agent_manager_snapshot']), saved_checkpoint)
                    # A second prevalidation must neither reject the saved
                    # result nor produce another baseline update.
                    with mock.patch.object(loop_manager, 'update_project') as update:
                        loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                            str(self.source), runtime_mutation=mutation)
                        update.assert_not_called()
                    # Rebase rechecks the historical result after runtime
                    # metadata has already advanced its comparison baseline.
                    from types import SimpleNamespace
                    from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
                    checkpoints = loop_manager.execution_checkpoint_state.ExecutionCheckpointRepository(self.connection)
                    audit = checkpoints.get(str(self.source), '')
                    comparison = dict(audit.snapshot['manager_metadata_agent_baseline'], role='reviewer')
                    checkpoints.record(str(self.source), '', expected_generation=audit.generation,
                        operation='rebase', expected_head=self.current.head, target_base_oid='b' * 40,
                        observed_remote_oid=None,
                        snapshot={**audit.snapshot, 'manager_metadata_agent_baseline': comparison})
                    self.connection.execute("UPDATE projects SET agent_role='reviewer', phase='REVIEWING'")
                    service = mock.Mock()
                    service.require_owned.return_value = (None, SimpleNamespace(status=RebaseStatus.COMPLETE))
                    with mock.patch.object(loop_manager.managed_rebase_adoption, 'needs_agent_comparison', return_value=True), \
                            mock.patch.object(loop_manager.managed_rebase_adoption, 'selected_baseline', side_effect=lambda value, *_a, **_k: value), \
                            mock.patch.object(loop_manager, '_current_rebase_service', return_value=service):
                        loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                            str(self.source), runtime_mutation=mutation)
                        violation = replace(self.before, refs={**self.before.refs, ref: ('foreign', '')})
                        self.connection.execute('UPDATE projects SET agent_result_snapshot=?', (violation.serialize(),))
                        with self.assertRaises(loop_manager.AgentScopeError):
                            loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                                str(self.source), runtime_mutation=mutation)
                    self.connection.execute("UPDATE projects SET agent_role='planner', phase='PLANNING'")
                    checkpoints.record(str(self.source), '', expected_generation=checkpoints.get(str(self.source), '').generation,
                        operation=audit.operation, expected_head=audit.expected_head,
                        target_base_oid=audit.target_base_oid, observed_remote_oid=audit.observed_remote_oid,
                        snapshot=audit.snapshot)
                    violation = replace(self.before, refs={**self.before.refs,
                        ref: ('foreign', '')})
                    self.connection.execute('UPDATE projects SET agent_snapshot=?, '
                        'agent_result_snapshot=?, agent_manager_snapshot=?',
                        (self.before.serialize(), violation.serialize(), saved_checkpoint.serialize()))
                    original = dict(self.connection.execute('SELECT * FROM projects').fetchone())
                    with self.assertRaises(loop_manager.AgentScopeError):
                        loop_manager.validate_persisted_agent_scope(self.connection, self.source, self.state,
                            str(self.source), runtime_mutation=mutation)
                    self.assertEqual(dict(self.connection.execute('SELECT * FROM projects').fetchone()), original)

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
        self.prepare_project(self.worktree)
        loop_manager.scoped_blocking.record_project_block(self.connection, str(self.source),
            loop_manager.BlockReasonCode.DEFAULT_WORKTREE_MUTATION, 'unrelated source', 'same',
            plan_id='G-092', previous_phase='WORKING')
        with mock.patch.object(loop_manager, '_git_common_directory', return_value=Path('/common')), \
                mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(stdout='origin', returncode=0)), \
                mock.patch.object(loop_manager, 'validate_managed_worktree'), \
                mock.patch.object(loop_manager, '_rebase_in_progress_for_branch', return_value=False), \
                mock.patch.object(agent_invocation_state.InvocationRepository, 'verified_scope', return_value=True) as proof:
            resources = loop_manager._agent_git_resources(
                self.connection, self.source, self.worktree, self.before, 'worker',
                selected_plan_path='docs/plans/[G-092] Work.md')
            self.assertIsNotNone(resources)
            self.assertEqual(resources.worktrees, frozenset({str(self.worktree)}))
            self.assertEqual(proof.call_args.kwargs['session_id'], 'saved-worker')
            proof.return_value = False
            self.assertIsNone(loop_manager._agent_git_resources(
                self.connection, self.source, self.worktree, self.before, 'worker'))

    def test_recreated_worktree_uses_only_its_create_receipt_and_keeps_rebase_audit_independent(self):
        worktree = Path('/managed/G-092').resolve()
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
        self.prepare_project(self.worktree)
        with mock.patch.object(loop_manager, 'capture_git_snapshot', return_value=result), \
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=self.state), \
                mock.patch.object(loop_manager, '_agent_git_resources', return_value=None):
            loop_manager.begin_agent_scope(self.connection, str(self.source), self.worktree, 'worker')
        self.record_fetch(before_oid='b' * 40, target_oid='c' * 40)
        self.current = replace(self.current, refs={**self.current.refs, self.ref: ('c' * 40, '')})
        worktree = self.worktree
        self.connection.execute('INSERT INTO exec_plans '
            '(repo, plan_id, path, title, sources, depends_on, priority, content_hash, status, '
            'branch, worktree, workspace_owned, agent_role, agent_snapshot, agent_result_snapshot) '
            "VALUES (?, 'G-092', 'docs/plans/[G-092] Work.md', 'Work', '[]', '[]', 'normal', 'hash', "
            "'PLAN_BLOCKED', 'loop-manager/G-092', ?, 1, 'worker', ?, ?)",
            (str(self.source), str(self.worktree), self.before.serialize(), result.serialize()))
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        with mock.patch.object(loop_manager, 'capture_git_snapshot', side_effect=lambda *_: self.current), \
                mock.patch.object(loop_manager, '_owned_rebase_state_dir', return_value=self.state), \
                mock.patch.object(loop_manager, '_published_plan_allows_common_plans', return_value=False):
            loop_manager.validate_blocked_agent_scope(self.connection, self.source, str(self.source),
                'main', worktree, plan['path'], saved_plan=plan, accepted_rebase=True)
        self.assertEqual(self.connection.execute('SELECT agent_snapshot FROM exec_plans').fetchone()[0],
                         self.before.serialize())
