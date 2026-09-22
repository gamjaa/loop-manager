import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import blocked_checkpoint
import blocked_checkpoint_state
import loop_manager
import notifications
import runtime_state


class BlockedCheckpointPolicyTest(unittest.TestCase):
    def observation(self, **changes):
        values = {
            "workspace_identity_matches": True,
            "workspace_owned": True,
            "head_oid": "a" * 40,
            "actual_branch": "loop-manager/G-034",
            "expected_branch": "loop-manager/G-034",
            "dirty_paths": (
                blocked_checkpoint.DirtyPath(
                    "code.py", ("unstaged",), True
                ),
            ),
        }
        values.update(changes)
        return blocked_checkpoint.SafetyObservation(**values)

    def test_assessor_accepts_only_owned_worker_paths_and_clean_git_state(self):
        verdict = blocked_checkpoint.assess_safety(self.observation())
        self.assertTrue(verdict.safe)
        self.assertEqual(verdict.included_paths, ("code.py",))

        cases = (
            (
                {"actual_branch": None},
                blocked_checkpoint.SafetyScope.PLAN,
                "detached_head",
            ),
            (
                {"operations": ("rebase-merge",)},
                blocked_checkpoint.SafetyScope.PLAN,
                "git_operation_in_progress",
            ),
            (
                {"agent_scope_recovery_pending": True},
                blocked_checkpoint.SafetyScope.PLAN,
                "agent_scope_recovery_pending",
            ),
            (
                {
                    "dirty_paths": (
                        blocked_checkpoint.DirtyPath(
                            "AGENTS.md", ("staged",), False
                        ),
                    )
                },
                blocked_checkpoint.SafetyScope.PLAN,
                "dirty_scope_violation",
            ),
            (
                {"shared_metadata_owned": False},
                blocked_checkpoint.SafetyScope.PROJECT,
                "shared_ref_mutation",
            ),
        )
        for changes, scope, reason in cases:
            with self.subTest(reason=reason):
                verdict = blocked_checkpoint.assess_safety(
                    self.observation(**changes)
                )
                self.assertFalse(verdict.safe)
                self.assertIs(verdict.scope, scope)
                self.assertEqual(verdict.reason_code, reason)

    def test_identity_message_and_push_policy_are_deterministic_and_narrow(self):
        fields = {
            "plan_id": "G-034",
            "reason_code": "verify_retry_exhausted",
            "evidence": "same failure",
            "parent_oid": "a" * 40,
            "tree_oid": "b" * 40,
            "included_paths": ("z.py", "a.py"),
        }
        identity = blocked_checkpoint.checkpoint_identity(**fields)
        self.assertEqual(identity, blocked_checkpoint.checkpoint_identity(**fields))
        message = blocked_checkpoint.checkpoint_message(
            plan_id=fields["plan_id"],
            reason_code=fields["reason_code"],
            evidence=fields["evidence"],
            identity=identity,
            included_paths=fields["included_paths"],
        )
        self.assertTrue(message.startswith("[G-034] BLOCKED checkpoint"))
        self.assertIn('Evidence: "same failure"', message)
        self.assertLess(message.index("- a.py"), message.index("- z.py"))
        self.assertIn(f"Checkpoint-Identity: {identity}", message)

        create = blocked_checkpoint.decide_push(
            local_oid="b" * 40,
            remote_oid=None,
            expected_remote_oid=None,
            remote_observed=True,
            fast_forward=False,
            rebase_recovery=False,
        )
        self.assertIs(create.mode, blocked_checkpoint.PushMode.CREATE)
        mismatch = blocked_checkpoint.decide_push(
            local_oid="b" * 40,
            remote_oid="c" * 40,
            expected_remote_oid="a" * 40,
            remote_observed=True,
            fast_forward=False,
            rebase_recovery=True,
        )
        self.assertIs(mismatch.mode, blocked_checkpoint.PushMode.REJECT)
        lease = blocked_checkpoint.decide_push(
            local_oid="b" * 40,
            remote_oid="a" * 40,
            expected_remote_oid="a" * 40,
            remote_observed=True,
            fast_forward=False,
            rebase_recovery=True,
        )
        self.assertIs(lease.mode, blocked_checkpoint.PushMode.REBASE_LEASE)

    def test_dispatcher_bounds_transient_push_retries_and_publishes_once(self):
        request = blocked_checkpoint.CheckpointRequest(
            repo="repo",
            plan_id="G-034",
            request_identity="request",
            phase=blocked_checkpoint.CheckpointPhase.REQUESTED,
            reason_code="verify_retry_exhausted",
            evidence="failure",
            branch="loop-manager/G-034",
            worktree="/state/G-034",
        )
        local_oid = "b" * 40
        state = mock.Mock()
        state.pending.return_value = (request,)
        state.record_remote_observation.side_effect = lambda item, remote: replace(
            item,
            expected_remote_oid=None,
            remote_observed=True,
            remote_oid=remote,
        )
        safety = mock.Mock()
        safety.assess.return_value = blocked_checkpoint.SafetyVerdict(
            True,
            blocked_checkpoint.SafetyScope.PLAN,
            None,
            None,
            "a" * 40,
            ("code.py",),
        )
        prepared = blocked_checkpoint.PreparedCheckpoint(
            "a" * 40,
            "c" * 40,
            "identity",
            "message",
            ("code.py",),
        )
        publication = mock.Mock()
        publication.existing_pull_request.return_value = None
        publication.prepare.return_value = prepared
        publication.ensure_local_commit.return_value = local_oid
        publication.observe_remote.side_effect = (None, local_oid)
        publication.push.side_effect = (
            blocked_checkpoint.CheckpointPushError("network", retryable=True),
            blocked_checkpoint.CheckpointPushError("network", retryable=True),
            None,
        )
        results = mock.Mock()

        completed = blocked_checkpoint.BlockedCheckpointDispatcher(
            state, safety, publication, results, max_push_attempts=3
        ).dispatch("repo")

        self.assertEqual(completed, 1)
        self.assertEqual(publication.push.call_count, 3)
        observed_request = replace(request, remote_observed=True, remote_oid=None)
        state.finish_published.assert_called_once_with(
            observed_request, local_oid
        )
        state.finish_unpublished.assert_not_called()
        results.finalized.assert_called_once_with(observed_request)

    def test_dispatcher_does_not_retry_permission_rejection(self):
        request = blocked_checkpoint.CheckpointRequest(
            repo="repo",
            plan_id="G-034",
            request_identity="request",
            phase=blocked_checkpoint.CheckpointPhase.LOCAL_COMMITTED,
            reason_code="review_retry_exhausted",
            evidence="failure",
            branch="loop-manager/G-034",
            worktree="/state/G-034",
        )
        state = mock.Mock()
        state.pending.return_value = (request,)
        state.record_remote_observation.side_effect = lambda item, remote: replace(
            item,
            expected_remote_oid=None,
            remote_observed=True,
            remote_oid=remote,
        )
        safety = mock.Mock()
        safety.assess.return_value = blocked_checkpoint.SafetyVerdict(
            True,
            blocked_checkpoint.SafetyScope.PLAN,
            None,
            None,
            "a" * 40,
            (),
        )
        publication = mock.Mock()
        publication.existing_pull_request.return_value = None
        publication.prepare.return_value = blocked_checkpoint.PreparedCheckpoint(
            "a" * 40, "c" * 40, "identity", "message", ()
        )
        publication.ensure_local_commit.return_value = "b" * 40
        publication.observe_remote.return_value = None
        publication.push.side_effect = blocked_checkpoint.CheckpointPushError(
            "permission denied", retryable=False
        )
        results = mock.Mock()

        blocked_checkpoint.BlockedCheckpointDispatcher(
            state, safety, publication, results, max_push_attempts=3
        ).dispatch("repo")

        publication.push.assert_called_once()
        state.finish_unpublished.assert_called_once_with(
            replace(request, remote_observed=True, remote_oid=None),
            "permission denied",
            retryable=False,
        )


class BlockedCheckpointGitAdapterTest(unittest.TestCase):
    def run_git(self, cwd, *arguments):
        result = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if result.returncode:
            self.fail(result.stdout)
        return result.stdout.strip()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.remote = root / "remote.git"
        self.repo = root / "project"
        self.state_dir = root / "state"
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        self.run_git(self.repo, "config", "user.name", "Test")
        self.run_git(self.repo, "config", "user.email", "test@example.com")
        (self.repo / "code.py").write_text("before\n", encoding="utf-8")
        plan_path = self.repo / "docs/plans/[G-034] Checkpoint.md"
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text(
            "---\nid: G-034\ntitle: Checkpoint\nsources:\n"
            "  - docs/PRODUCT.md\ndepends_on: []\ncomplexity: high\n"
            "risk: critical\npriority: critical\n---\n\n"
            "## Acceptance Criteria\n\n- checkpoint\n",
            encoding="utf-8",
        )
        (self.repo / "docs/PRODUCT.md").write_text("product\n", encoding="utf-8")
        self.run_git(self.repo, "add", ".")
        self.run_git(self.repo, "commit", "-m", "base")
        self.run_git(self.repo, "branch", "-M", "main")
        self.run_git(self.repo, "push", "-u", "origin", "main")
        self.base_oid = self.run_git(self.repo, "rev-parse", "HEAD")
        self.worktree = loop_manager.expected_execplan_workspace(
            self.state_dir, str(self.repo.resolve()), "G-034"
        )[1]
        self.worktree.parent.mkdir(parents=True)
        self.run_git(
            self.repo,
            "worktree",
            "add",
            "-b",
            "loop-manager/G-034",
            str(self.worktree),
            self.base_oid,
        )
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(self.connection)
        notifications.ensure_schema(self.connection)
        notifications.configure_session(self.connection, enabled=True)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, origin) VALUES (?, 'main', ?)",
            (str(self.repo.resolve()), str(self.remote)),
        )
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha, active_sha, branch, worktree,
                workspace_owned
            ) VALUES (?, 'G-034', ?, 'Checkpoint', '["docs/PRODUCT.md"]',
                      '[]', 'critical', 'hash', 'IMPLEMENTING', ?, ?, ?, ?, 1)
            """,
            (
                str(self.repo.resolve()),
                "docs/plans/[G-034] Checkpoint.md",
                self.base_oid,
                self.base_oid,
                "loop-manager/G-034",
                str(self.worktree),
            ),
        )
        self.connection.commit()
        normalize = loop_manager.github_repository
        normalizer = mock.patch(
            "loop_manager.github_repository",
            side_effect=lambda url: "example/project" if url == str(self.remote) else normalize(url),
        )
        normalizer.start()
        self.addCleanup(normalizer.stop)
        self.connection.execute("UPDATE projects SET origin = 'example/project'")
        self.connection.commit()

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def block(self):
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET agent_result_snapshot = ?",
                (loop_manager.capture_git_snapshot(self.worktree).serialize(),),
            )
            runtime_state.RuntimeStateRepository(self.connection).block_plan(
                str(self.repo.resolve()),
                "G-034",
                reason_code="verify_retry_exhausted",
                evidence="same verify failure",
                release_fingerprint="fingerprint",
                clear_projection=True,
            )
        return blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        ).get(str(self.repo.resolve()), "G-034")

    def test_safe_dirty_paths_make_one_commit_and_create_owned_remote_branch(self):
        (self.worktree / "code.py").write_text("after\n", encoding="utf-8")
        request = self.block()
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection,
            self.repo,
            self.state_dir,
            str(self.repo.resolve()),
        )
        verdict = adapter.assess(request)
        self.assertTrue(verdict.safe, verdict.detail)
        prepared = adapter.prepare(request, verdict)
        local_oid = adapter.ensure_local_commit(request, prepared)
        self.assertNotEqual(local_oid, self.base_oid)
        self.assertEqual(
            self.run_git(self.worktree, "status", "--porcelain"), ""
        )
        self.assertIn(
            "Reason: verify_retry_exhausted",
            self.run_git(self.worktree, "show", "-s", "--format=%B", "HEAD"),
        )
        self.assertIsNone(adapter.observe_remote(request))
        adapter.push(
            request,
            local_oid,
            blocked_checkpoint.PushDecision(blocked_checkpoint.PushMode.CREATE),
        )
        self.assertEqual(adapter.observe_remote(request), local_oid)
        request = replace(request, local_oid=local_oid)

        clean_verdict = adapter.assess(request)
        clean_prepared = adapter.prepare(request, clean_verdict)
        self.assertEqual(
            adapter.ensure_local_commit(request, clean_prepared), local_oid
        )

        (self.worktree / "code.py").write_text("after again\n", encoding="utf-8")
        next_verdict = adapter.assess(request)
        next_prepared = adapter.prepare(request, next_verdict)
        next_oid = adapter.ensure_local_commit(request, next_prepared)
        self.assertTrue(adapter.is_fast_forward(local_oid, next_oid))
        adapter.push(
            replace(
                request,
                expected_remote_oid=local_oid,
                remote_observed=True,
            ),
            next_oid,
            blocked_checkpoint.PushDecision(
                blocked_checkpoint.PushMode.FAST_FORWARD
            ),
        )
        self.assertEqual(adapter.observe_remote(request), next_oid)

    def test_ignored_symlink_and_unowned_staged_paths_are_rejected_without_commit(self):
        (self.worktree / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        self.run_git(self.worktree, "add", ".gitignore")
        self.run_git(self.worktree, "commit", "-m", "ignore rule")
        (self.worktree / "ignored.txt").write_text("secret\n", encoding="utf-8")
        (self.worktree / "AGENTS.md").write_text("outside\n", encoding="utf-8")
        self.run_git(self.worktree, "add", "-f", "AGENTS.md")
        (self.worktree / "link.py").symlink_to("code.py")
        request = self.block()
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection,
            self.repo,
            self.state_dir,
            str(self.repo.resolve()),
        )
        verdict = adapter.assess(request)
        self.assertFalse(verdict.safe)
        self.assertEqual(verdict.reason_code, "dirty_scope_violation")
        self.assertIn("AGENTS.md", verdict.detail)
        self.assertIn("ignored.txt", verdict.detail)
        self.assertIn("link.py", verdict.detail)

    def test_existing_pr_short_circuits_dispatch_after_safety_before_push(self):
        request = self.block()
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        )
        safety = mock.Mock()
        publication = mock.Mock()
        publication.existing_pull_request.return_value = {"state": "CLOSED"}
        results = mock.Mock()
        dispatcher = blocked_checkpoint.BlockedCheckpointDispatcher(
            repository, safety, publication, results
        )

        self.assertEqual(dispatcher.dispatch(request.repo), 1)
        safety.assess.assert_called_once_with(request)
        publication.push.assert_not_called()
        row = self.connection.execute(
            "SELECT phase, unpublished_reason FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(row["phase"], "unpublished")
        self.assertIn("기존 PR", row["unpublished_reason"])

    def test_deleted_tracked_symlink_is_rejected_staged_or_unstaged(self):
        link = self.worktree / "link.py"
        link.symlink_to("code.py")
        self.run_git(self.worktree, "add", "link.py")
        self.run_git(self.worktree, "commit", "-m", "symlink fixture")
        link.unlink()
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    self.run_git(self.worktree, "add", "link.py")
                request = self.block()
                adapter = loop_manager._BlockedCheckpointAdapter(
                    self.connection, self.repo, self.state_dir, request.repo
                )
                before = loop_manager.capture_git_snapshot(self.worktree)
                verdict = adapter.assess(request)
                self.assertFalse(verdict.safe)
                self.assertEqual(verdict.reason_code, "dirty_scope_violation")
                self.assertIn("symlink", verdict.detail)
                self.assertEqual(before, loop_manager.capture_git_snapshot(self.worktree))

    def test_validation_block_preserves_snapshot_before_later_config_mutation(self):
        repo = str(self.repo.resolve())
        loop_manager.hydrate_execplan_project(
            self.connection, repo,
            self.connection.execute("SELECT * FROM exec_plans").fetchone(),
        )
        with mock.patch("loop_manager._blocked_validation_input_fingerprints", return_value=("input", "worktree")):
            for _ in range(loop_manager.MAX_VALIDATION_FINGERPRINT_REPEATS + 1):
                decision = loop_manager.transition_validation_failure(
                    self.connection, repo, self.worktree, "verify", "same failure"
                )
        self.assertTrue(decision.blocked)
        request = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(repo, "G-034")
        adapter = loop_manager._BlockedCheckpointAdapter(self.connection, self.repo, self.state_dir, repo)
        self.assertTrue(adapter.assess(request).safe)
        self.run_git(self.repo, "config", "core.hooksPath", "/tmp/changed-after-block")
        verdict = adapter.assess(request)
        self.assertFalse(verdict.safe)
        self.assertEqual(verdict.reason_code, "shared_git_config_mutation")

    def test_shared_mutations_block_before_any_remote_access(self):
        cases = ("origin", "pushurl", "config", "ref", "external")
        for mutation in cases:
            with self.subTest(mutation=mutation):
                request = self.block()
                if mutation == "origin":
                    self.run_git(self.repo, "remote", "set-url", "origin", "https://github.com/other/repo.git")
                elif mutation == "pushurl":
                    self.run_git(self.repo, "config", "remote.origin.pushurl", "https://github.com/other/repo.git")
                elif mutation == "config":
                    self.run_git(self.repo, "config", "core.hooksPath", "/tmp/untrusted")
                elif mutation == "ref":
                    self.run_git(self.repo, "update-ref", "refs/heads/foreign", self.base_oid)
                else:
                    (self.repo / "code.py").write_text("external change\n")
                adapter = loop_manager._BlockedCheckpointAdapter(
                    self.connection, self.repo, self.state_dir, request.repo
                )
                publication = mock.Mock()
                results = loop_manager._BlockedCheckpointResults(self.connection)
                repository = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
                blocked_checkpoint.BlockedCheckpointDispatcher(repository, adapter, publication, results).dispatch(request.repo)
                publication.existing_pull_request.assert_not_called()
                publication.push.assert_not_called()
                stored = self.connection.execute(
                    "SELECT scope, reason_code FROM project_blocks"
                ).fetchone()
                self.assertEqual(stored["scope"], "project")
                self.assertEqual(stored["reason_code"], {
                    "origin": "shared_git_config_mutation",
                    "pushurl": "shared_git_config_mutation",
                    "config": "shared_git_config_mutation",
                    "ref": "shared_ref_mutation",
                    "external": "outside_managed_worktree_mutation",
                }[mutation])
                self.run_git(self.repo, "remote", "set-url", "origin", str(self.remote))
                if mutation == "pushurl":
                    self.run_git(self.repo, "config", "--unset", "remote.origin.pushurl")
                if mutation == "config":
                    self.run_git(self.repo, "config", "--unset", "core.hooksPath")
                # Give each subcase a fresh durable request after restoration.
                self.connection.execute("DELETE FROM blocked_checkpoints")
                self.connection.execute("DELETE FROM project_blocks")
                self.connection.execute("UPDATE projects SET phase = NULL")
                self.connection.commit()
