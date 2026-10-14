from tests.support.git import isolated_git_test
from tests.support.scope import AgentScopeFixture
from tests.support.git import prepare_manager_import_fixture
import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from pathlib import Path
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, commit_and_push


@isolated_git_test
class ScopeIgnoredArtifactsTest(AgentScopeFixture, unittest.TestCase):
    def test_ignored_untracked_creation_change_and_deletion_are_allowed(self):
        scenarios = tuple(
            (role, before, after)
            for role in ('planner', 'worker', 'reviewer')
            for before, after in ((None, 'created\n'), ('old\n', 'changed\n'), ('old\n', None))
        )
        for role_name, before_contents, after_contents in scenarios:
            with self.subTest(role=role_name), tempfile.TemporaryDirectory() as directory:
                repo, _plan_path = self._repository_with_plan(directory)
                (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
                commit_and_push(repo, "Ignore diagnostic fixture")
                ignored = repo / "ignored.txt"
                if before_contents is not None:
                    ignored.write_text(before_contents, encoding="utf-8")
                before = loop_manager.capture_git_snapshot(repo)

                if after_contents is None:
                    ignored.unlink()
                else:
                    ignored.write_text(after_contents, encoding="utf-8")
                after = loop_manager.capture_git_snapshot(repo)

                self.assertNotIn("ignored.txt", before.dirty)
                self.assertNotIn("ignored.txt", after.dirty)
                loop_manager.validate_agent_scope(
                    role_name, repo, before, after,
                    selected_plan_path="docs/plans/[G-001] Scope Safety.md"
                    if role_name == "worker" else None,
                )

    def test_legacy_ignored_snapshot_requires_unchanged_tracked_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            (repo / "cache.bin").write_bytes(b"old")
            current = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                current,
                dirty={"cache.bin": (("untracked",), "old-signature")},
                config={
                    key: value for key, value in current.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            normalized = loop_manager._normalize_legacy_ignored_snapshot(
                repo, legacy, current
            )
            self.assertEqual(normalized.dirty, {})

            (repo / ".gitignore").write_text("cache.bin\nnew.bin\n", encoding="utf-8")
            changed = loop_manager.capture_git_snapshot(repo)
            self.assertEqual(
                loop_manager._normalize_legacy_ignored_snapshot(repo, legacy, changed),
                legacy,
            )

    def test_persisted_worker_snapshot_resumes_with_legacy_ignored_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            exclude = repo / '.git/legacy-empty-exclude'
            exclude.write_bytes(b'')
            git_command(repo, 'config', 'core.excludesFile', str(exclude))
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            (repo / "cache.bin").write_bytes(b"old")
            current = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                current,
                dirty={"cache.bin": (("untracked",), "old-signature")},
                config={
                    key: value for key, value in current.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            state_dir = Path(directory) / "state"
            self._persist_interrupted_worker(state_dir, repo, plan_path, legacy)
            connection = sqlite3.connect(state_dir / "loop-manager.db")
            connection.row_factory = sqlite3.Row
            try:
                self.assertEqual(
                    loop_manager.validate_persisted_agent_scope(
                        connection, repo, state_dir, str(repo)
                    ),
                    current,
                )
            finally:
                connection.close()

    def test_historical_and_blocked_ignored_results_preserve_original_evidence(self):
        for role in ('planner', 'worker', 'reviewer'):
            for action in ('create', 'modify', 'delete'):
                with self.subTest(role=role, action=action), tempfile.TemporaryDirectory() as directory:
                    repo, plan_path = self._repository_with_plan(directory)
                    exclude = repo / '.git/legacy-empty-exclude'
                    exclude.write_bytes(b'')
                    git_command(repo, 'config', 'core.excludesFile', str(exclude))
                    (repo / '.gitignore').write_text('cache.bin\n', encoding='utf-8')
                    commit_and_push(repo, 'Ignore cache')
                    git_command(repo, 'switch', '-c', 'loop-manager/G-001')
                    cache = repo / 'cache.bin'
                    if action != 'create':
                        cache.write_bytes(b'old')
                    current = loop_manager.capture_git_snapshot(repo)
                    start = replace(current, dirty=({'cache.bin': (('untracked',), 'old')}
                                                    if action != 'create' else {}))
                    result = replace(current, dirty=({'cache.bin': (('untracked',), 'result')}
                                                     if action != 'delete' else {}))
                    legacy_config = {key: value for key, value in current.config.items()
                                     if not key.startswith('ignore-source:')}
                    start = replace(start, config=legacy_config)
                    result = replace(result, config=legacy_config)
                    if action == 'delete':
                        cache.unlink()
                    else:
                        cache.write_bytes(b'changed')
                    state = Path(directory) / 'state'
                    self._persist_interrupted_worker(state, repo, plan_path, start)
                    connection = sqlite3.connect(state / 'loop-manager.db')
                    connection.row_factory = sqlite3.Row
                    try:
                        loop_manager._ensure_execplans_schema(connection)
                        with connection:
                            connection.execute("UPDATE projects SET agent_role = ?, agent_result_snapshot = ?",
                                               (role, result.serialize()))
                            connection.execute("UPDATE exec_plans SET agent_role = ?, agent_snapshot = ?, "
                                               "agent_result_snapshot = ?, worker_session_id = 'saved', retry_count = 3",
                                               (role, start.serialize(), result.serialize()))
                        loop_manager.validate_persisted_agent_scope(connection, repo, state, str(repo))
                        manager = loop_manager.ManagerGitCheckpoint(result, frozenset()).serialize()
                        with connection:
                            connection.execute('UPDATE projects SET agent_manager_snapshot = ?', (manager,))
                            connection.execute('UPDATE exec_plans SET agent_manager_snapshot = ?', (manager,))
                        loop_manager.validate_persisted_agent_scope(connection, repo, state, str(repo))
                        with connection:
                            connection.execute("UPDATE projects SET phase = 'PLAN_BLOCKED'")
                            connection.execute("UPDATE exec_plans SET status = 'PLAN_BLOCKED'")
                        loop_manager.validate_persisted_agent_scope(connection, repo, state, str(repo))
                        plan = connection.execute('SELECT * FROM exec_plans').fetchone()
                        loop_manager.validate_blocked_agent_scope(connection, repo, str(repo), 'main',
                            repo, plan_path, saved_plan=plan, require_safe_result=True)
                        saved = connection.execute('SELECT * FROM exec_plans').fetchone()
                        self.assertEqual(saved['agent_snapshot'], start.serialize())
                        self.assertEqual(saved['agent_result_snapshot'], result.serialize())
                        self.assertEqual(saved['agent_manager_snapshot'], manager)
                        self.assertEqual((saved['worker_session_id'], saved['retry_count']), ('saved', 3))
                        if role == 'reviewer':
                            with connection:
                                connection.execute("UPDATE exec_plans SET status = 'REVIEWING', "
                                    "worker_result = 'saved worker', verify_result = 'saved verify', "
                                    "worker_input_token = 'worker-token', verify_token = 'verify-token'")
                                loop_manager._clear_active_in_current_transaction(connection, str(repo))
                            runtime = loop_manager.runtime_state.RuntimeStateRepository(connection)
                            runtime.block_plan(str(repo), 'G-001',
                                reason_code='managed_worktree_scope_violation', evidence='legacy cache',
                                release_fingerprint='same')
                            from loop_manager_internal.recovery import scoped_blocking
                            block = scoped_blocking.plan_block(connection, str(repo), 'G-001')
                            with mock.patch.object(loop_manager, 'validate_execplan_workspace_identity'):
                                self.assertTrue(loop_manager._reconcile_scope_baseline_plan_block(
                                    connection, repo, state, str(repo), block))
                                self.assertFalse(loop_manager._reconcile_scope_baseline_plan_block(
                                    connection, repo, state, str(repo), block))
                            resumed = connection.execute('SELECT * FROM exec_plans').fetchone()
                            self.assertEqual(resumed['status'], 'REVIEWING')
                            self.assertEqual((resumed['worker_result'], resumed['verify_result'],
                                              resumed['worker_input_token'], resumed['verify_token']),
                                             ('saved worker', 'saved verify', 'worker-token', 'verify-token'))
                        if action != 'delete':
                            self.assertEqual(cache.read_bytes(), b'changed')
                    finally:
                        connection.close()

    def test_external_ignore_rule_requires_recorded_unchanged_start_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._repository_with_plan(directory)
            exclude = repo / '.git/info/exclude'
            exclude.write_text(exclude.read_text() + '\ncache.bin\n')
            current = loop_manager.capture_git_snapshot(repo)
            legacy = replace(current, dirty={'cache.bin': (('untracked',), 'old')})
            self.assertEqual(loop_manager._normalize_legacy_ignored_snapshot(
                repo, legacy, current, start=legacy).dirty, {})
            unproven = replace(legacy, config={key: value for key, value in legacy.config.items()
                                              if not key.startswith('ignore-source:')})
            self.assertEqual(loop_manager._normalize_legacy_ignored_snapshot(
                repo, legacy, current, start=unproven), legacy)
            exclude.write_text(exclude.read_text() + 'new.bin\n')
            changed = loop_manager.capture_git_snapshot(repo)
            self.assertEqual(loop_manager._normalize_legacy_ignored_snapshot(
                repo, legacy, changed, start=legacy), legacy)

    def test_result_ignore_rule_must_be_proven_at_invocation_start(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._repository_with_plan(directory)
            (repo / '.gitignore').write_text('old.bin\n', encoding='utf-8')
            commit_and_push(repo, 'Starting ignore rule')
            start = loop_manager.capture_git_snapshot(repo)
            (repo / '.gitignore').write_text('old.bin\nnew.bin\n', encoding='utf-8')
            commit_and_push(repo, 'Agent hides a new path')
            current = loop_manager.capture_git_snapshot(repo)
            result = replace(current, dirty={'new.bin': (('untracked',), 'hidden')})
            self.assertEqual(loop_manager._normalize_legacy_ignored_snapshot(
                repo, result, current, start=start), result)

    def test_negated_ignore_rule_cannot_hide_deleted_nonignored_input(self):
        for source in ('tracked', 'repository-exclude'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                repo, _ = self._repository_with_plan(directory)
                rule = repo / ('.gitignore' if source == 'tracked' else '.git/info/exclude')
                rule.write_text('*.cache\n!visible.cache\n', encoding='utf-8')
                if source == 'tracked':
                    commit_and_push(repo, 'Keep visible cache authoritative')
                path = repo / 'visible.cache'
                path.write_bytes(b'nonignored input')
                before = loop_manager.capture_git_snapshot(repo)
                self.assertIn('visible.cache', before.dirty)
                path.unlink()
                current = loop_manager.capture_git_snapshot(repo)
                self.assertEqual(loop_manager._normalize_legacy_ignored_snapshot(
                    repo, before, current), before)

    def test_ignored_planner_project_block_requires_current_workspace_ownership(self):
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._repository_with_plan(directory)
            (repo / '.gitignore').write_text('cache.bin\n', encoding='utf-8')
            commit_and_push(repo, 'Ignore cache')
            git_command(repo, 'switch', '--detach')
            current = loop_manager.capture_git_snapshot(repo)
            start = replace(current, dirty={'cache.bin': (('untracked',), 'legacy')})
            state = Path(directory) / 'state'
            state.mkdir()
            connection = sqlite3.connect(':memory:')
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute("INSERT INTO projects (repo, base_branch, phase, worktree, "
                "agent_role, agent_snapshot, agent_result_snapshot, planning_workspace_owned, "
                "planning_attempt_fingerprint) VALUES (?, 'main', 'PLANNING', ?, 'planner', ?, ?, 0, 'same')",
                (str(repo), str(repo), start.serialize(), current.serialize()))
            connection.commit()
            scoped_blocking.record_project_block(connection, str(repo),
                BlockReasonCode.DEFAULT_WORKTREE_MUTATION, 'legacy cache', 'same',
                plan_id=None, previous_phase='PLANNING')
            original = dict(scoped_blocking.project_block(connection, str(repo)))
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                connection, repo, state, str(repo)))
            self.assertEqual(dict(scoped_blocking.project_block(connection, str(repo))), original)
            with connection:
                connection.execute('UPDATE projects SET planning_workspace_owned = 1')
            # A confirmed flag cannot replace the deterministic owned path.
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                connection, repo, state, str(repo)))
            self.assertEqual(dict(scoped_blocking.project_block(connection, str(repo))), original)

    def test_owned_ignored_planner_project_resume_rechecks_owner_and_saved_result(self):
        from loop_manager_internal.recovery import scoped_blocking
        from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
        from loop_manager_internal.planning.planning_workspace import planning_workspace_path
        with tempfile.TemporaryDirectory() as directory:
            repo, _ = self._repository_with_plan(directory)
            (repo / '.gitignore').write_text('cache.bin\n', encoding='utf-8')
            commit_and_push(repo, 'Ignore cache')
            state = Path(directory) / 'state'
            state.mkdir()
            managed = planning_workspace_path(state, str(repo), 'same')
            git_command(repo, 'worktree', 'add', '--detach', str(managed), 'HEAD')
            cache = managed / 'cache.bin'
            cache.write_bytes(b'kept artifact')
            current = loop_manager.capture_git_snapshot(managed)
            start = replace(current, dirty={'cache.bin': (('untracked',), 'legacy')})
            result = replace(current, dirty={'cache.bin': (('untracked',), 'result')})
            connection = sqlite3.connect(':memory:')
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute("INSERT INTO projects (repo, base_branch, phase, worktree, "
                "agent_role, agent_snapshot, agent_result_snapshot, planning_workspace_owned, "
                "planning_attempt_fingerprint, planning_error, planner_session_id) "
                "VALUES (?, 'main', 'PLANNING', ?, 'planner', ?, ?, 1, 'same', 'independent', 'saved')",
                (str(repo), str(managed), start.serialize(), result.serialize()))
            connection.commit()
            scoped_blocking.record_project_block(connection, str(repo),
                BlockReasonCode.DEFAULT_WORKTREE_MUTATION, 'legacy cache', 'same',
                plan_id=None, previous_phase='PLANNING')
            block = dict(scoped_blocking.project_block(connection, str(repo)))
            original_validator = loop_manager.validate_planning_worktree_ownership
            calls = 0
            def changed_owner(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    connection.execute('UPDATE projects SET planning_workspace_owned = 0')
                return original_validator(*args)
            with mock.patch.object(loop_manager, 'validate_planning_worktree_ownership',
                                   side_effect=changed_owner):
                self.assertFalse(loop_manager.resume_project_block_if_safe(
                    connection, repo, state, str(repo)))
            self.assertEqual(calls, 2)
            self.assertEqual(dict(scoped_blocking.project_block(connection, str(repo))), block)
            with connection:
                connection.execute('UPDATE projects SET planning_workspace_owned = 1')
            # New local input does not reintroduce a retired access diagnosis.
            new_plan = write_execplan(managed, 'G-002', 'Later input')
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                connection, repo, state, str(repo)))
            self.assertTrue(new_plan.is_file())
            self.assertFalse(loop_manager.resume_project_block_if_safe(
                connection, repo, state, str(repo)))
            saved = connection.execute('SELECT * FROM projects').fetchone()
            self.assertEqual((saved['phase'], saved['planning_error'], saved['planner_session_id']),
                             ('PLANNING', 'independent', 'saved'))
            self.assertEqual(saved['agent_snapshot'], start.serialize())
            self.assertEqual(saved['agent_result_snapshot'], result.serialize())
            self.assertEqual(cache.read_bytes(), b'kept artifact')

    def test_legacy_snapshot_does_not_trust_unrecorded_exclude_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            legacy = replace(
                before,
                config={
                    key: value for key, value in before.config.items()
                    if not key.startswith("ignore-source:")
                },
            )
            state_dir = Path(directory) / "state"
            self._persist_interrupted_worker(state_dir, repo, plan_path, legacy)
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nnew-cache.bin\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(state_dir / "loop-manager.db")
            connection.row_factory = sqlite3.Row
            try:
                with self.subTest():
                    loop_manager.validate_persisted_agent_scope(
                        connection, repo, state_dir, str(repo)
                    )
            finally:
                connection.close()

    def test_forced_staged_ignored_file_stays_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("cache.bin\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / "cache.bin").write_bytes(b"staged")
            git_command(repo, "add", "-f", "cache.bin")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("cache.bin", after.index)
            with self.subTest():
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_tracked_file_matching_ignore_rule_stays_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("src/app.py\n", encoding="utf-8")
            commit_and_push(repo, "Ignore tracked path fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / "src/app.py").write_text("VALUE = 3\n", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("src/app.py", after.dirty)
            with self.subTest():
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_new_ignore_rule_cannot_hide_untracked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            (repo / ".gitignore").write_text("*.cache\n", encoding="utf-8")
            commit_and_push(repo, "Ignore cache fixture")
            before = loop_manager.capture_git_snapshot(repo)
            (repo / ".gitignore").write_text("*.cache\nhidden.txt\n", encoding="utf-8")
            (repo / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("hidden.txt", after.dirty)
            with self.subTest():
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    @unittest.skipIf(os.name == 'nt', 'Windows 파일명에는 콜론을 사용할 수 없습니다.')
    def test_colon_in_ignore_rule_path_cannot_hide_untracked_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            rule_directory = repo / "rules:old"
            rule_directory.mkdir()
            rule = rule_directory / ".gitignore"
            rule.write_text("*.cache\n", encoding="utf-8")
            git_command(repo, "add", "rules:old/.gitignore")
            commit_and_push(repo, "Add ignore rule with colon in path")
            before = loop_manager.capture_git_snapshot(repo)
            rule.write_text("*.cache\nhidden.txt\n", encoding="utf-8")
            (rule_directory / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertIn("rules:old/hidden.txt", after.dirty)
            with self.subTest():
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_repository_exclude_change_cannot_hide_new_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            before = loop_manager.capture_git_snapshot(repo)
            exclude = Path(git_command(repo, "rev-parse", "--git-path", "info/exclude"))
            if not exclude.is_absolute():
                exclude = repo / exclude
            exclude.write_text(
                exclude.read_text(encoding="utf-8") + "\nhidden.txt\n",
                encoding="utf-8",
            )
            (repo / "hidden.txt").write_text("new", encoding="utf-8")
            after = loop_manager.capture_git_snapshot(repo)
            self.assertNotIn("hidden.txt", after.dirty)
            with self.subTest():
                loop_manager.validate_agent_scope("reviewer", repo, before, after)

    def test_planner_import_bytecode_is_ignored_without_hiding_scope_violations(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, _plan_path = self._repository_with_plan(directory)
            prepare_manager_import_fixture(repo)

            planner_environment = loop_manager.agent_environment("codex", "planner")
            import_result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import loop_manager; "
                    "from loop_manager_internal.config import managed_config; "
                    "from loop_manager_internal.maintenance import maintenance_git, maintenance_planning; "
                    "from loop_manager_internal.storage import maintenance_state; "
                    "from loop_manager_internal.runtime import project_registry",
                ],
                cwd=repo,
                env=planner_environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            self.assertTrue(any(repo.rglob("*.pyc")))

            existing_cache = repo / "__pycache__" / "preserved.pyc"
            existing_cache.parent.mkdir(exist_ok=True)
            existing_cache.write_bytes(b"existing cache")
            before = loop_manager.capture_git_snapshot(repo)
            import_result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import loop_manager; "
                    "from loop_manager_internal.config import managed_config; "
                    "from loop_manager_internal.maintenance import maintenance_git, maintenance_planning; "
                    "from loop_manager_internal.storage import maintenance_state; "
                    "from loop_manager_internal.runtime import project_registry",
                ],
                cwd=repo,
                env=planner_environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(import_result.returncode, 0, import_result.stderr)
            self.assertEqual(existing_cache.read_bytes(), b"existing cache")
            write_execplan(repo, "G-002", "Imported Without Bytecode")
            after = loop_manager.capture_git_snapshot(repo)
            loop_manager.validate_agent_scope("planner", repo, before, after)

            explicit_cache = repo / "__pycache__" / "explicit.pyc"
            explicit_cache.write_bytes(b"explicit cache")
            explicit_after = loop_manager.capture_git_snapshot(repo)
            loop_manager.validate_agent_scope("planner", repo, after, explicit_after)
            (repo / "unexpected.txt").write_text("publishable\n", encoding="utf-8")
            with self.assertRaisesRegex(loop_manager.AgentScopeError, "unexpected.txt"):
                loop_manager.validate_agent_scope(
                    "planner", repo, after, loop_manager.capture_git_snapshot(repo)
                )

    def test_codex_turn_diff_refs_are_ignored_for_persisted_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, plan_path = self._repository_with_plan(directory)
            head = git_command(repo, "rev-parse", "HEAD")
            old_ref = "refs/codex/turn-diffs/captures/old/session/base"
            new_ref = "refs/codex/turn-diffs/captures/new/session/base"
            git_command(repo, "update-ref", old_ref, head)
            before = loop_manager.capture_git_snapshot(repo)

            git_command(repo, "update-ref", "-d", old_ref)
            git_command(repo, "update-ref", new_ref, head)
            after = loop_manager.capture_git_snapshot(repo)

            self.assertIn(old_ref, before.refs)
            self.assertNotIn(old_ref, after.refs)
            self.assertNotIn(new_ref, before.refs)
            self.assertIn(new_ref, after.refs)
            loop_manager.validate_agent_scope(
                "worker",
                repo,
                before,
                after,
                selected_plan_path=plan_path,
            )
