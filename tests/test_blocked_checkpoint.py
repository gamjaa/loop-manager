import json
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
import scoped_blocking
from scoped_blocking import BlockReasonCode
from worker_session import SessionBinding


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
        (self.repo / "dependency.py").write_text("dependency = False\n", encoding="utf-8")
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
        # Cause probes are external effects; unit tests never run full verify/CLI.
        verify_probe = mock.patch.object(loop_manager, "verify", return_value=(True, "OK"))
        self.verify_probe = verify_probe.start()
        self.addCleanup(verify_probe.stop)
        reviewer_probe = mock.patch.object(loop_manager, "run_agent", return_value=(0, "VERDICT: PASS"))
        self.reviewer_probe = reviewer_probe.start()
        self.addCleanup(reviewer_probe.stop)
        config_probe = mock.patch.object(
            loop_manager, "load_managed_config",
            return_value=mock.Mock(verify_timeout_seconds=600),
        )
        config_probe.start()
        self.addCleanup(config_probe.stop)
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

    def block(self, reason="verify_retry_exhausted", evidence="same verify failure"):
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET agent_result_snapshot = ?",
                (loop_manager.capture_git_snapshot(self.worktree).serialize(),),
            )
            runtime_state.RuntimeStateRepository(self.connection).block_plan(
                str(self.repo.resolve()),
                "G-034",
                reason_code=reason,
                evidence=evidence,
                release_fingerprint="fingerprint",
                clear_projection=True,
            )
        return blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        ).get(str(self.repo.resolve()), "G-034")

    def publish_block(self, reason="verify_retry_exhausted",
                      evidence="code.py: same verify failure"):
        (self.worktree / "code.py").write_text("checkpoint\n", encoding="utf-8")
        self.run_git(self.worktree, "add", "code.py")
        self.run_git(self.worktree, "commit", "-m", "Blocked checkpoint")
        request = self.block(reason, evidence=evidence)
        oid = self.run_git(self.worktree, "rev-parse", "HEAD")
        self.run_git(
            self.worktree, "push", "origin",
            "HEAD:refs/heads/loop-manager/G-034",
        )
        blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        ).finish_published(request, oid)
        return oid

    def user_commit(self, path="code.py", contents="corrected\n", *, rewrite=False):
        user = Path(self.temporary.name) / "user"
        if not user.exists():
            self.run_git(self.repo, "clone", str(self.remote), str(user))
            self.run_git(user, "config", "user.name", "User")
            self.run_git(user, "config", "user.email", "user@example.com")
            self.run_git(user, "checkout", "-b", "loop-manager/G-034",
                         "origin/loop-manager/G-034" if not rewrite else "origin/main")
        target = user / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        self.run_git(user, "add", "--", path)
        self.run_git(user, "commit", "-m", "User correction")
        self.run_git(user, "push", "--force" if rewrite else "--porcelain",
                     "origin", "HEAD:refs/heads/loop-manager/G-034")
        return self.run_git(user, "rev-parse", "HEAD")

    def test_published_remote_code_successor_resumes_worker_on_same_workspace(self):
        published = self.publish_block()
        binding = SessionBinding("codex", "model", "high").serialize()
        self.connection.execute(
            "UPDATE exec_plans SET worker_result = 'old', verify_result = 'old', "
            "review_result = 'old', worker_session_id = 'session', "
            "worker_session_binding = ? WHERE plan_id = 'G-034'",
            (binding,),
        )
        self.connection.commit()
        corrected = self.user_commit()
        self.assertNotEqual(corrected, published)

        resumed = loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        )

        self.assertTrue(resumed)
        plan = self.connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(plan["status"], "IMPLEMENTING")
        self.assertEqual(plan["worker_session_id"], "session")
        self.assertEqual(plan["worker_session_binding"], binding)
        self.assertIsNone(plan["worker_result"])
        self.assertIsNone(plan["verify_result"])
        self.assertIsNone(plan["review_result"])
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "main"), self.base_oid)
        stored = self.connection.execute(
            "SELECT published_oid, accepted_remote_oid FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(stored), (published, corrected))

    def test_pathless_diagnostics_reassess_checkpoint_implementation_changes(self):
        self.publish_block(evidence='File "tests/test_code.py", line 12\nAssertionError: 1 != 2')
        self.connection.execute(
            "UPDATE exec_plans SET worker_result = 'old', verify_result = 'AssertionError', "
            "review_result = 'old' WHERE plan_id = 'G-034'"
        )
        self.connection.commit()
        corrected = self.user_commit("code.py", "corrected implementation\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, worker_result, verify_result, review_result "
            "FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", None, None, None))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "main"), self.base_oid)

    def test_rewritten_remote_branch_keeps_local_checkpoint_blocked(self):
        published = self.publish_block()
        before_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        self.user_commit(rewrite=True)

        resumed = loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        )

        self.assertFalse(resumed)
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        plan = self.connection.execute(
            "SELECT status, last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertIn("후속이 아닙니다", plan["last_error"])
        after_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        self.assertEqual(after_events, before_events + 1)
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], after_events)

    def test_dirty_local_worktree_keeps_remote_successor_unadopted(self):
        published = self.publish_block()
        self.user_commit()
        (self.worktree / "code.py").write_text("local work\n", encoding="utf-8")

        resumed = loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        )

        self.assertFalse(resumed)
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertEqual((self.worktree / "code.py").read_text(), "local work\n")
        self.assertIn("로컬", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])

    def test_local_unpublished_commit_is_not_overwritten_by_remote_divergence(self):
        self.publish_block()
        corrected = self.user_commit()
        (self.worktree / "code.py").write_text("local commit\n", encoding="utf-8")
        self.run_git(self.worktree, "add", "code.py")
        self.run_git(self.worktree, "commit", "-m", "Local unpublished work")
        local = self.run_git(self.worktree, "rev-parse", "HEAD")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), local)
        self.assertEqual(self.run_git(self.repo, "ls-remote", "origin",
                                      "refs/heads/loop-manager/G-034").split()[0],
                         corrected)

    def test_unpublished_checkpoint_does_not_adopt_remote_commit(self):
        self.block()
        self.run_git(self.worktree, "push", "origin",
                     "HEAD:refs/heads/loop-manager/G-034")
        corrected = self.user_commit()
        with mock.patch.object(loop_manager, "resume_blocked_execplan_if_inputs_changed",
                               return_value=False):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        self.assertNotEqual(self.run_git(self.worktree, "rev-parse", "HEAD"),
                            corrected)
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM blocked_checkpoints"
        ).fetchone()[0], "requested")

    def test_closed_pr_block_keeps_reopen_gate(self):
        published = self.publish_block()
        self.connection.execute(
            "UPDATE exec_plans SET blocked_reason = 'pull_request_closed', "
            "pr_url = 'https://example.test/pr/1' WHERE plan_id = 'G-034'"
        )
        self.connection.commit()
        self.user_commit()

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")

    def test_legacy_published_oid_cannot_be_inferred_from_remote_head(self):
        published = self.publish_block()
        self.connection.execute(
            "UPDATE blocked_checkpoints SET published_oid = NULL, "
            "accepted_remote_oid = NULL"
        )
        self.connection.commit()
        self.user_commit()

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIn("legacy checkpoint", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])

    def test_remote_agents_file_change_is_outside_worker_scope(self):
        published = self.publish_block()
        self.user_commit("AGENTS.md", "foreign guidance\n")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIn("AGENTS.md", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])

    def test_remote_noncode_change_remains_blocked_and_records_inspection(self):
        published = self.publish_block()
        before_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        corrected = self.user_commit("docs/FUTURE_IDEAS.md", "later\n")
        with mock.patch.object(loop_manager, "resume_blocked_execplan_if_inputs_changed",
                               return_value=False):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        row = self.connection.execute("SELECT * FROM blocked_checkpoints").fetchone()
        self.assertEqual(row["published_oid"], published)
        self.assertEqual(row["accepted_remote_oid"], corrected)
        self.assertEqual(row["inspected_remote_oid"], corrected)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        after_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        self.assertEqual(after_events, before_events + 1)

        remote_calls = loop_manager.git
        with mock.patch.object(loop_manager, "git", wraps=remote_calls) as git_calls, \
             mock.patch.object(loop_manager, "resume_blocked_execplan_if_inputs_changed",
                               return_value=False):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        self.assertFalse(any(
            call.args[1:2] == ("fetch",) for call in git_calls.call_args_list
        ))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], after_events)

        next_oid = self.user_commit("code.py", "next correction\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), next_oid)

    def test_readme_stays_blocked_but_new_execution_input_is_reassessed(self):
        self.publish_block(evidence='File "tests/test_code.py", line 12\nAssertionError: 1 != 2')
        readme_oid = self.user_commit("README.md", "unrelated notes\n")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        corrected = self.user_commit("dependency.py", "corrected_dependency = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, worker_result, verify_result, review_result "
            "FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", None, None, None))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.run_git(self.repo, "rev-parse", "main"), self.base_oid)
        self.assertNotEqual(readme_oid, corrected)

    def test_unrelated_code_keeps_cause_and_history_until_probe_passes(self):
        self.publish_block(evidence="AssertionError: original cause")
        self.connection.execute(
            "UPDATE exec_plans SET verify_failure_history = ?, "
            "retry_count = 3, verify_result = 'AssertionError: original cause' WHERE plan_id = 'G-034'",
            ('{"old": {"occurrences": 3}}',),
        )
        self.connection.commit()
        self.verify_probe.return_value = (False, "AssertionError: original cause")
        unrelated = self.user_commit("unrelated.py", "unrelated = True\n")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, retry_count, verify_failure_history, verify_result "
            "FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("PLAN_BLOCKED", 3, '{"old": {"occurrences": 3}}', "AssertionError: original cause"))
        inspected = self.connection.execute(
            "SELECT inspected_remote_oid, inspection_reason FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(inspected), (unrelated, "원격 checkpoint 원인 유지:\nAssertionError: original cause"))
        evidence = self.connection.execute(
            "SELECT evidence FROM execplan_blocks WHERE plan_id = 'G-034'"
        ).fetchone()[0]
        self.assertEqual(evidence, inspected["inspection_reason"])
        payloads = self.connection.execute("SELECT payload FROM notification_outbox").fetchall()
        self.assertTrue(any(evidence in json.loads(row[0]).get("cause", "") for row in payloads))
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.verify_probe.assert_called_once()
        self.verify_probe.return_value = (True, "OK")
        self.user_commit("dependency.py", "corrected_dependency = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.verify_probe.call_count, 2)

    def test_new_verify_failure_resumes_worker_with_new_diagnostic(self):
        self.publish_block(evidence="AssertionError: original cause")
        self.verify_probe.return_value = (False, "AssertionError: new cause")
        self.user_commit("dependency.py", "fixed = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, verify_result FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", "AssertionError: new cause"))

    def test_diagnostic_resume_interruption_rolls_back_release(self):
        self.publish_block(evidence="AssertionError: original cause")
        history = '{"previous": {"occurrences": 3}}'
        self.connection.execute(
            "UPDATE exec_plans SET verify_failure_history = ?, review_failure_history = ? "
            "WHERE plan_id = 'G-034'", (history, history),
        )
        self.connection.commit()
        database = Path(self.temporary.name) / "resume.sqlite"
        durable = sqlite3.connect(database)
        self.connection.backup(durable)
        self.connection.close()
        self.connection = durable
        self.connection.row_factory = sqlite3.Row
        self.verify_probe.return_value = (False, "AssertionError: new cause")
        corrected = self.user_commit("dependency.py", "fixed = True\n")
        original = loop_manager.scoped_blocking.clear_plan_block

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt

        with mock.patch.object(loop_manager.scoped_blocking, "clear_plan_block", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
                )
        self.connection.close()
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        row = self.connection.execute(
            "SELECT status, verify_failure_history, review_failure_history FROM exec_plans "
            "WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("PLAN_BLOCKED", history, history))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM execplan_blocks").fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints"
        ).fetchone()[0], corrected)
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        row = self.connection.execute(
            "SELECT status, verify_result, verify_failure_history, review_failure_history "
            "FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(tuple(row), ("IMPLEMENTING", "AssertionError: new cause", history, history))
        self.assertEqual(self.connection.execute(
            "SELECT verify_result FROM projects"
        ).fetchone()[0], "AssertionError: new cause")

    def test_new_review_issue_resumes_worker(self):
        old = "ISSUE_ID: code.py::old::bug\nOld issue\nVERDICT: FAIL"
        self.publish_block(reason="review_retry_exhausted", evidence=old)
        new = "ISSUE_ID: dependency.py::new::bug\nNew issue\nVERDICT: FAIL"
        self.reviewer_probe.return_value = (0, new)
        self.user_commit("dependency.py", "fixed = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT review_result FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], new)

    def test_uncertain_probe_termination_blocks_project_without_retry(self):
        self.publish_block()
        self.user_commit("dependency.py", "fixed = True\n")
        self.verify_probe.side_effect = loop_manager.VerifyTerminationUncertain("process may be alive")
        for _ in range(2):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
            ))
        block = loop_manager.scoped_blocking.project_block(self.connection, str(self.repo.resolve()))
        self.assertEqual(block["reason_code"], "repository_state_unsafe")
        self.verify_probe.assert_called_once()
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")

    def test_cause_probe_error_retries_same_candidate_without_release(self):
        self.publish_block()
        self.user_commit("dependency.py", "fixed = True\n")
        self.verify_probe.side_effect = RuntimeError("temporarily unavailable")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        self.verify_probe.side_effect = None
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.verify_probe.call_count, 2)

    def test_review_cause_requires_pass_before_release(self):
        self.publish_block(reason="review_retry_exhausted", evidence="original issue")
        self.reviewer_probe.return_value = (
            0, "ISSUE_ID: code.py::function::bug\nStill present\nVERDICT: FAIL",
        )
        self.user_commit("unrelated.py", "unrelated = True\n")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        self.reviewer_probe.return_value = (0, "VERDICT: PASS")
        self.user_commit("dependency.py", "fixed = True\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()), "main",
        ))
        self.assertTrue(self.reviewer_probe.call_args.kwargs["read_only"])

    def test_review_result_path_controls_remote_release(self):
        self.publish_block(
            reason="review_retry_exhausted", evidence="review failed",
        )
        self.connection.execute(
            "UPDATE exec_plans SET review_result = 'code.py: blocking issue' "
            "WHERE plan_id = 'G-034'"
        )
        self.connection.commit()
        self.user_commit("README.md", "unrelated notes\n")
        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")

        corrected = self.user_commit("code.py", "fixed review issue\n")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"),
                         corrected)

    def test_interruption_after_release_does_not_restore_old_agent_snapshot(self):
        self.publish_block()
        stale = loop_manager.capture_git_snapshot(self.worktree).serialize()
        self.connection.execute(
            "UPDATE exec_plans SET agent_role = 'worker', agent_snapshot = ?, "
            "agent_result_snapshot = ?, agent_manager_snapshot = ?, "
            "agent_baseline = 'old' WHERE plan_id = 'G-034'",
            (stale, stale, stale),
        )
        self.connection.commit()
        corrected = self.user_commit()
        original = loop_manager._resume_remote_corrected_execplan

        def interrupted(*arguments, **keywords):
            original(*arguments, **keywords)
            raise KeyboardInterrupt

        with mock.patch.object(loop_manager, "_resume_remote_corrected_execplan",
                               side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir,
                    str(self.repo.resolve()), "main",
                )

        stored = self.connection.execute(
            "SELECT status, agent_role, agent_snapshot, agent_result_snapshot, "
            "agent_manager_snapshot, agent_baseline FROM exec_plans "
            "WHERE plan_id = 'G-034'"
        ).fetchone()
        self.assertEqual(stored["status"], "IMPLEMENTING")
        self.assertTrue(all(value is None for value in tuple(stored)[1:]))
        loop_manager.hydrate_active_project(
            self.connection, str(self.repo.resolve()),
        )
        project = self.connection.execute(
            "SELECT agent_role, agent_snapshot, agent_result_snapshot, "
            "agent_manager_snapshot FROM projects"
        ).fetchone()
        self.assertTrue(all(value is None for value in project))
        resumed_scope = loop_manager.begin_agent_scope(
            self.connection, str(self.repo.resolve()), self.worktree, "worker",
        )
        self.assertEqual(resumed_scope.head, corrected)

    def test_remote_plan_input_correction_restarts_worker(self):
        self.publish_block(reason="plan_input_invalid")
        blocked = self.connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()
        original_input = loop_manager._revision_execplan_input_fingerprint(
            self.repo, "HEAD", blocked,
        )
        self.connection.execute(
            "UPDATE exec_plans SET blocked_fingerprint = ? WHERE plan_id = 'G-034'",
            (original_input,),
        )
        self.connection.commit()
        user = Path(self.temporary.name) / "user"
        self.run_git(self.repo, "clone", str(self.remote), str(user))
        self.run_git(user, "config", "user.name", "User")
        self.run_git(user, "config", "user.email", "user@example.com")
        self.run_git(user, "checkout", "-b", "loop-manager/G-034",
                     "origin/loop-manager/G-034")
        path = "docs/plans/[G-034] Checkpoint.md"
        target = user / path
        target.write_text(target.read_text() + "\n- corrected acceptance\n")
        self.run_git(user, "add", "--", path)
        self.run_git(user, "commit", "-m", "Correct plan input")
        self.run_git(user, "push", "origin", "HEAD:refs/heads/loop-manager/G-034")
        corrected = self.run_git(user, "rev-parse", "HEAD")

        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "IMPLEMENTING")

    def test_deleted_remote_branch_is_reported_without_local_mutation(self):
        published = self.publish_block()
        self.run_git(self.worktree, "push", "origin",
                     ":refs/heads/loop-manager/G-034")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIn("삭제", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])

    def test_remote_plan_identity_change_is_rejected(self):
        published = self.publish_block()
        plan_path = "docs/plans/[G-034] Checkpoint.md"
        user = Path(self.temporary.name) / "user"
        self.run_git(self.repo, "clone", str(self.remote), str(user))
        self.run_git(user, "config", "user.name", "User")
        self.run_git(user, "config", "user.email", "user@example.com")
        self.run_git(user, "checkout", "-b", "loop-manager/G-034",
                     "origin/loop-manager/G-034")
        target = user / plan_path
        target.write_text(target.read_text().replace("id: G-034", "id: G-999"))
        self.run_git(user, "add", "--", plan_path)
        self.run_git(user, "commit", "-m", "wrong plan")
        self.run_git(user, "push", "origin", "HEAD:refs/heads/loop-manager/G-034")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIn("ExecPlan", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])

    def test_ignored_path_overlap_refuses_fast_forward(self):
        published = self.publish_block()
        exclude = self.run_git(self.worktree, "rev-parse", "--git-path", "info/exclude")
        Path(exclude).write_text("cache.txt\n", encoding="utf-8")
        (self.worktree / "cache.txt").write_text("local cache\n", encoding="utf-8")
        cache_oid = self.user_commit("cache.txt", "remote cache\n")

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertEqual((self.worktree / "cache.txt").read_text(), "local cache\n")
        self.assertIn("ignored", self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0])

        (self.worktree / "cache.txt").unlink()
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), cache_oid)
        self.assertEqual((self.worktree / "cache.txt").read_text(), "remote cache\n")
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "IMPLEMENTING")

    def test_unrelated_ignored_file_survives_remote_fast_forward(self):
        self.publish_block()
        exclude = self.run_git(self.worktree, "rev-parse", "--git-path", "info/exclude")
        Path(exclude).write_text("cache.txt\n", encoding="utf-8")
        (self.worktree / "cache.txt").write_text("local cache\n", encoding="utf-8")
        corrected = self.user_commit("code.py", "remote code\n")

        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual((self.worktree / "cache.txt").read_text(), "local cache\n")

    def test_crash_after_fast_forward_retries_same_candidate(self):
        self.publish_block()
        corrected = self.user_commit()
        with mock.patch.object(
            blocked_checkpoint_state.BlockedCheckpointRepository,
            "record_remote_inspection", side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir,
                    str(self.repo.resolve()), "main",
                )
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)
        self.assertEqual(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints"
        ).fetchone()[0], corrected)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM exec_plans WHERE plan_id = 'G-034'"
        ).fetchone()[0], "PLAN_BLOCKED")
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints"
        ).fetchone()[0], None)

    def test_crash_after_fetch_retries_without_touching_local_head(self):
        published = self.publish_block()
        corrected = self.user_commit()
        with mock.patch.object(
            blocked_checkpoint_state.BlockedCheckpointRepository,
            "start_remote_adoption", side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir,
                    str(self.repo.resolve()), "main",
                )
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        self.assertIsNone(self.connection.execute(
            "SELECT pending_remote_oid FROM blocked_checkpoints"
        ).fetchone()[0])
        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)

    def test_fetch_failure_marks_remote_inspection_retryable(self):
        published = self.publish_block()
        before_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        corrected = self.user_commit()
        original_git = loop_manager.git

        def unavailable_fetch(path, *arguments, **options):
            if arguments and arguments[0] == "fetch":
                return subprocess.CompletedProcess(
                    ["git", "fetch"], 1, "network unavailable",
                )
            return original_git(path, *arguments, **options)

        with mock.patch.object(loop_manager, "git", side_effect=unavailable_fetch):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)
        inspection = self.connection.execute(
            "SELECT inspected_remote_oid, inspection_retryable "
            "FROM blocked_checkpoints"
        ).fetchone()
        self.assertEqual(tuple(inspection), (corrected, 1))
        failure_events = self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0]
        self.assertEqual(failure_events, before_events + 1)
        with mock.patch.object(loop_manager, "git", side_effect=unavailable_fetch):
            self.assertFalse(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir,
                str(self.repo.resolve()), "main",
            ))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM notification_outbox"
        ).fetchone()[0], failure_events)

        self.assertTrue(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), corrected)

    def test_remote_refusal_and_notification_retry_after_interruption(self):
        published = self.publish_block()
        self.user_commit(rewrite=True)
        with mock.patch.object(
            loop_manager.notifications, "enqueue_current_project_events",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_plan_blocks(
                    self.connection, self.repo, self.state_dir,
                    str(self.repo.resolve()), "main",
                )
        self.assertIsNone(self.connection.execute(
            "SELECT inspected_input FROM blocked_checkpoints"
        ).fetchone()[0])
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), published)

        self.assertFalse(loop_manager.resume_plan_blocks(
            self.connection, self.repo, self.state_dir,
            str(self.repo.resolve()), "main",
        ))
        self.assertIsNotNone(self.connection.execute(
            "SELECT inspected_input FROM blocked_checkpoints"
        ).fetchone()[0])

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
        unrecorded = adapter.assess(request)
        self.assertFalse(unrecorded.safe)
        self.assertEqual(unrecorded.reason_code, "shared_ref_mutation")
        blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        ).finish_published(request, local_oid)
        request = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        ).get(str(self.repo.resolve()), "G-034")

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

    def test_symlink_and_unowned_staged_paths_remain_rejected_with_ignored_artifact(self):
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
        self.assertNotIn("ignored.txt", verdict.detail)
        self.assertIn("link.py", verdict.detail)
        self.assertEqual(
            (self.worktree / "ignored.txt").read_text(encoding="utf-8"),
            "secret\n",
        )

    def test_ignored_only_artifact_is_preserved_without_checkpoint_commit(self):
        (self.worktree / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        self.run_git(self.worktree, "add", ".gitignore")
        self.run_git(self.worktree, "commit", "-m", "ignore rule")
        head = self.run_git(self.worktree, "rev-parse", "HEAD")
        ignored = self.worktree / "ignored.txt"
        ignored.write_text("local cache\n", encoding="utf-8")
        request = self.block()
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection,
            self.repo,
            self.state_dir,
            str(self.repo.resolve()),
        )
        verdict = adapter.assess(request)
        self.assertTrue(verdict.safe, verdict.detail)
        self.assertEqual(verdict.included_paths, ())
        prepared = adapter.prepare(request, verdict)
        self.assertEqual(adapter.ensure_local_commit(request, prepared), head)
        self.assertEqual(
            self.run_git(self.worktree, "ls-files", "--stage", "ignored.txt"),
            "",
        )
        self.assertEqual(ignored.read_text(encoding="utf-8"), "local cache\n")

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

    def prepare_guidance_block(self, *, implementation_changes=True):
        from tests.test_guidance_approval import guidance_plan

        repo = str(self.repo.resolve())
        plan_path = "docs/plans/[G-034] Checkpoint.md"
        (self.worktree / "AGENTS.md").write_text("# Agents\n")
        self.run_git(self.worktree, "add", "AGENTS.md")
        self.run_git(self.worktree, "commit", "-m", "guidance baseline")
        plan = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        loop_manager.hydrate_execplan_project(self.connection, repo, plan)
        loop_manager.update_project(self.connection, repo, worker_session_id="guidance-session")
        before = loop_manager.begin_agent_scope(self.connection, repo, self.worktree, "worker")
        text = guidance_plan().replace("G-020", "G-034").replace("title: Guidance", "title: Checkpoint")
        (self.worktree / plan_path).write_text(text)
        if implementation_changes:
            (self.worktree / "code.py").write_text("safe implementation\n")
            test_path = self.worktree / "tests/test_app.py"
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text("def test_safe():\n    assert True\n")
        loop_manager.validate_agent_scope(
            "worker", self.worktree, before, loop_manager.capture_git_snapshot(self.worktree),
            selected_plan_path=plan_path,
        )
        document = loop_manager._guidance_document(self.worktree, plan_path)
        with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
            loop_manager._block_for_new_guidance(
                self.connection, self.repo, repo, self.worktree, plan,
                document, document.proposals, "proposal recorded",
            )
        return repo, plan_path, text

    def _assert_guidance_checkpoint_tree_and_restart(self, *, implementation_changes):
        repo, plan_path, _ = self.prepare_guidance_block(
            implementation_changes=implementation_changes,
        )
        with mock.patch("loop_manager.pr_for_branch", return_value=None):
            self.assertEqual(loop_manager.dispatch_blocked_checkpoints(
                self.connection, self.repo, self.state_dir, repo,
            ), 1)
        oid = self.run_git(self.worktree, "rev-parse", "HEAD")
        paths = set(self.run_git(
            self.worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", oid,
        ).splitlines())
        expected = {plan_path}
        if implementation_changes:
            expected.update(("code.py", "tests/test_app.py"))
        self.assertEqual(paths, expected)
        self.assertEqual(
            set(self.run_git(self.worktree, "ls-tree", "-r", "--name-only", oid).splitlines())
            - set(self.run_git(self.worktree, "ls-tree", "-r", "--name-only", self.base_oid).splitlines()),
            ({"AGENTS.md", "tests/test_app.py"} if implementation_changes
             else {"AGENTS.md"}),
        )
        notified = self.connection.execute(
            "SELECT count(*) FROM notification_outbox WHERE event_type = 'blocked'",
        ).fetchone()[0]
        restored = sqlite3.connect(":memory:")
        restored.row_factory = sqlite3.Row
        self.connection.backup(restored)
        self.addCleanup(restored.close)
        self.assertEqual(
            loop_manager.validate_persisted_agent_scope(
                restored, self.repo, self.state_dir, repo,
            ).head,
            oid,
        )
        with mock.patch("loop_manager.pr_for_branch", return_value=None) as lookup:
            self.assertEqual(loop_manager.dispatch_blocked_checkpoints(
                restored, self.repo, self.state_dir, repo,
            ), 0)
        lookup.assert_not_called()
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), oid)
        self.assertEqual(restored.execute(
            "SELECT count(*) FROM notification_outbox WHERE event_type = 'blocked'",
        ).fetchone()[0], notified)
        self.assertEqual(restored.execute(
            "SELECT blocked_reason FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()[0], loop_manager.GUIDANCE_BLOCK_REASON)

    def test_guidance_plan_only_checkpoint_tree_survives_restart(self):
        self._assert_guidance_checkpoint_tree_and_restart(
            implementation_changes=False,
        )

    def test_guidance_code_and_test_checkpoint_tree_survives_restart(self):
        self._assert_guidance_checkpoint_tree_and_restart(
            implementation_changes=True,
        )

    def test_replaced_checkpoint_request_owns_only_published_remote_ref(self):
        repo, _, _ = self.prepare_guidance_block()
        with mock.patch("loop_manager.pr_for_branch", return_value=None):
            loop_manager.dispatch_blocked_checkpoints(
                self.connection, self.repo, self.state_dir, repo,
            )
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET blocked_reason = 'verify_retry_exhausted'"
            )
            self.connection.execute(
                "UPDATE execplan_blocks SET reason_code = 'verify_retry_exhausted', "
                "evidence = 'new failure'"
            )
            request = repository.request(
                repo=repo, plan_id="G-034", reason_code="verify_retry_exhausted",
                evidence="new failure", branch="loop-manager/G-034",
                worktree=str(self.worktree), expected_remote_oid=None,
                rebase_recovery=False,
            )
        self.assertIsNone(request.local_oid)
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection, self.repo, self.state_dir, repo,
        )
        plan = adapter._plan(request)
        current = loop_manager.capture_git_snapshot(self.worktree)
        self.assertIsNone(adapter._shared_safety_error(request, plan, current))
        self.run_git(self.repo, "tag", "foreign-tag", self.base_oid)
        verdict = adapter._shared_safety_error(
            request, plan, loop_manager.capture_git_snapshot(self.worktree),
        )
        self.assertEqual(verdict.reason_code, "shared_ref_mutation")
        self.run_git(self.repo, "tag", "-d", "foreign-tag")
        self.run_git(
            self.repo, "push", "--force", "origin",
            f"{self.base_oid}:refs/heads/loop-manager/G-034",
        )
        verdict = adapter._shared_safety_error(
            request, plan, loop_manager.capture_git_snapshot(self.worktree),
        )
        self.assertEqual(verdict.reason_code, "shared_ref_mutation")

    def test_manager_base_fetch_intent_owns_only_observed_base_oid(self):
        repo, _, _ = self.prepare_guidance_block(implementation_changes=False)
        request = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection,
        ).get(repo, "G-034")
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection, self.repo, self.state_dir, repo,
        )
        plan = adapter._plan(request)
        before = loop_manager.capture_git_snapshot(self.worktree)
        self._advance_base_remote()
        intent = loop_manager._record_manager_base_fetch_intent(
            self.connection, self.repo, repo, "G-034", "main", before,
        )
        loop_manager.remote_base(self.repo, "main", narrow_fetch=True)
        current = loop_manager.capture_git_snapshot(self.worktree)
        self.assertEqual(
            adapter._shared_safety_error(request, plan, current).reason_code,
            "shared_ref_mutation",
        )
        loop_manager._finish_manager_base_fetch(self.connection, self.repo, intent)
        self.assertIsNone(adapter._shared_safety_error(request, plan, current))
        self.assertEqual(loop_manager.validate_persisted_agent_scope(
            self.connection, self.repo, self.state_dir, repo,
        ).refs[f"refs/remotes/origin/main"][0], current.refs[f"refs/remotes/origin/main"][0])
        self.run_git(self.repo, "tag", "foreign-tag", self.base_oid)
        verdict = adapter._shared_safety_error(
            request, plan, loop_manager.capture_git_snapshot(self.worktree),
        )
        self.assertEqual(verdict.reason_code, "shared_ref_mutation")

    def _advance_base_remote(self):
        other = Path(self.temporary.name) / "base-writer"
        self.run_git(self.repo, "clone", "-b", "main", str(self.remote), str(other))
        self.run_git(other, "config", "user.name", "Other")
        self.run_git(other, "config", "user.email", "other@example.com")
        (other / "dependency.py").write_text("dependency = True\n")
        self.run_git(other, "add", "dependency.py")
        self.run_git(other, "commit", "-m", "advance base")
        self.run_git(other, "push", "origin", "main")
        return self.run_git(other, "rev-parse", "HEAD")

    def test_pending_base_fetch_does_not_own_external_ref_update(self):
        repo, _, _ = self.prepare_guidance_block(implementation_changes=False)
        request = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection,
        ).get(repo, "G-034")
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection, self.repo, self.state_dir, repo,
        )
        before = loop_manager.capture_git_snapshot(self.worktree)
        target = self._advance_base_remote()
        intent = loop_manager._record_manager_base_fetch_intent(
            self.connection, self.repo, repo, "G-034", "main", before,
        )
        self.assertEqual(self.connection.execute(
            "SELECT phase FROM manager_base_fetches WHERE id = ?", (intent,),
        ).fetchone()[0], "pending")
        self.run_git(
            self.repo, "fetch", "--no-tags", "--refmap=", "origin",
            "+refs/heads/main:refs/heads/external-staging",
        )
        self.run_git(self.repo, "update-ref", "refs/remotes/origin/main", target)
        self.run_git(self.repo, "update-ref", "-d", "refs/heads/external-staging")
        current = loop_manager.capture_git_snapshot(self.worktree)
        self.assertEqual(current.refs["refs/remotes/origin/main"][0], target)
        self.assertEqual(
            adapter._shared_safety_error(request, adapter._plan(request), current).reason_code,
            "shared_ref_mutation",
        )
        with self.assertRaises(loop_manager.AgentScopeError):
            loop_manager.validate_persisted_agent_scope(
                self.connection, self.repo, self.state_dir, repo,
            )

    def test_guidance_restart_rejects_foreign_ref_change(self):
        repo, _, _ = self.prepare_guidance_block(implementation_changes=False)
        with mock.patch("loop_manager.pr_for_branch", return_value=None):
            loop_manager.dispatch_blocked_checkpoints(
                self.connection, self.repo, self.state_dir, repo,
            )
        self.run_git(self.repo, "tag", "foreign-tag", self.base_oid)
        with self.assertRaises(loop_manager.AgentScopeError):
            loop_manager.validate_persisted_agent_scope(
                self.connection, self.repo, self.state_dir, repo,
            )

    def test_recorded_merge_cleanup_is_exactly_allowed(self):
        repo = str(self.repo.resolve())
        other = loop_manager.expected_execplan_workspace(
            self.state_dir, repo, "G-035",
        )[1]
        other.parent.mkdir(parents=True, exist_ok=True)
        self.run_git(
            self.repo, "worktree", "add", "-b", "loop-manager/G-035",
            str(other), self.base_oid,
        )
        self.connection.execute(
            "INSERT INTO exec_plans (repo, plan_id, path, title, sources, depends_on, "
            "priority, content_hash, status, branch, worktree, workspace_owned, "
            "pr_url, pr_head_oid) VALUES (?, 'G-035', 'docs/plans/[G-035] Work.md', "
            "'Work', '[]', '[]', 'normal', 'hash', 'PR_OPEN', ?, ?, 1, ?, ?)",
            (repo, "loop-manager/G-035", str(other), "https://example/pr/2", self.base_oid),
        )
        self.connection.commit()
        self.prepare_guidance_block(implementation_changes=False)
        other_plan = self.connection.execute(
            "SELECT * FROM exec_plans WHERE plan_id = 'G-035'",
        ).fetchone()
        assessment = loop_manager.complete_merged_execplan_worktree(
            self.connection, self.repo, repo, self.state_dir, other_plan,
            "https://example/pr/2", self.base_oid,
        )
        self.assertEqual(assessment.status, "safe")
        repository = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        request = repository.get(repo, "G-034")
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection, self.repo, self.state_dir, repo,
        )
        plan = adapter._plan(request)
        current = loop_manager.capture_git_snapshot(self.worktree)
        self.assertIsNone(adapter._shared_safety_error(request, plan, current))
        self.assertEqual(loop_manager.validate_persisted_agent_scope(
            self.connection, self.repo, self.state_dir, repo,
        ).worktrees.get(str(other)), None)
        self.connection.execute("DELETE FROM merged_worktree_cleanups")
        verdict = adapter._shared_safety_error(request, plan, current)
        self.assertEqual(verdict.reason_code, "outside_managed_worktree_mutation")
        with self.assertRaises(loop_manager.AgentScopeError):
            loop_manager.validate_persisted_agent_scope(
                self.connection, self.repo, self.state_dir, repo,
            )

    def _induce_guidance_double_block(self, *, decide=False):
        repo, plan_path, text = self.prepare_guidance_block()
        with mock.patch("loop_manager.pr_for_branch", return_value=None):
            loop_manager.dispatch_blocked_checkpoints(
                self.connection, self.repo, self.state_dir, repo,
            )
        oid = self.run_git(self.worktree, "rev-parse", "HEAD")
        if decide:
            (self.worktree / plan_path).write_text(
                text.replace("Decision: `pending`", "Decision: `reject`")
                .replace("Revision: ``", "Revision: `user-1`"),
            )
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET blocked_reason = 'managed_worktree_scope_violation'"
            )
            self.connection.execute(
                "UPDATE execplan_blocks SET reason_code = 'managed_worktree_scope_violation', "
                "evidence = 'false positive'"
            )
            blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).request(
                repo=repo, plan_id="G-034",
                reason_code="managed_worktree_scope_violation",
                evidence="false positive", branch="loop-manager/G-034",
                worktree=str(self.worktree), expected_remote_oid=None,
                rebase_recovery=False,
            )
            scoped_blocking.record_project_block(
                self.connection, repo, BlockReasonCode.SHARED_REF_MUTATION,
                "Agent 범위 선검증 실패: checkpoint ref", None,
                plan_id="G-034", previous_phase="PLAN_BLOCKED",
            )
        return repo, oid

    def test_proven_guidance_double_block_restores_original_wait(self):
        repo, oid = self._induce_guidance_double_block()
        self.assertTrue(loop_manager.resume_project_block_if_safe(
            self.connection, self.repo, self.state_dir, repo,
        ))
        self.assertIsNone(scoped_blocking.project_block(self.connection, repo))
        self.assertEqual(self.connection.execute(
            "SELECT blocked_reason FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()[0], loop_manager.GUIDANCE_BLOCK_REASON)
        checkpoint = self.connection.execute(
            "SELECT phase, reason_code, published_oid FROM blocked_checkpoints",
        ).fetchone()
        self.assertEqual(tuple(checkpoint), (
            "published", loop_manager.GUIDANCE_BLOCK_REASON, oid,
        ))
        self.assertEqual(self.connection.execute(
            "SELECT decision, decision_revision FROM guidance_approvals",
        ).fetchone()[:], ("pending", ""))

    def test_guidance_double_block_preserves_user_decision_and_rejects_tampering(self):
        repo, oid = self._induce_guidance_double_block(decide=True)
        target = self.worktree / "AGENTS.md"
        target.write_text("changed without approval\n")
        self.assertFalse(loop_manager.resume_project_block_if_safe(
            self.connection, self.repo, self.state_dir, repo,
        ))
        self.assertIsNotNone(scoped_blocking.project_block(self.connection, repo))
        self.assertEqual(self.connection.execute(
            "SELECT reason_code FROM blocked_checkpoints",
        ).fetchone()[0], "managed_worktree_scope_violation")
        target.write_text("# Agents\n")
        self.assertTrue(loop_manager.resume_project_block_if_safe(
            self.connection, self.repo, self.state_dir, repo,
        ))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), oid)
        self.assertIn("Decision: `reject`", (self.worktree / self.connection.execute(
            "SELECT path FROM exec_plans WHERE plan_id = 'G-034'",
        ).fetchone()[0]).read_text())
        self.assertEqual(self.connection.execute(
            "SELECT decision, decision_revision FROM guidance_approvals",
        ).fetchone()[:], ("pending", ""))

    def test_validated_guidance_dispatches_checkpoint_then_resumes_session(self):
        repo, plan_path, text = self.prepare_guidance_block()
        with mock.patch("loop_manager.pr_for_branch", return_value=None):
            self.assertEqual(loop_manager.dispatch_blocked_checkpoints(
                self.connection, self.repo, self.state_dir, repo
            ), 1)
        checkpoint = self.connection.execute("SELECT * FROM blocked_checkpoints").fetchone()
        self.assertEqual(checkpoint["phase"], "published", checkpoint["unpublished_reason"])
        head = self.run_git(self.worktree, "rev-parse", "HEAD")
        self.assertEqual(checkpoint["remote_oid"], head)
        plan = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        self.assertEqual(plan["status"], "PLAN_BLOCKED")
        self.assertEqual(plan["worker_session_id"], "guidance-session")
        self.assertEqual(loop_manager.GitSnapshot.deserialize(plan["agent_snapshot"]).head, head)
        self.assertIsNone(self.connection.execute("SELECT * FROM project_blocks").fetchone())
        (self.worktree / plan_path).write_text(
            text.replace("Decision: `pending`", "Decision: `reject`").replace("Revision: ``", "Revision: `user-1`")
        )
        with mock.patch("loop_manager.is_loop_manager_repository", return_value=False):
            self.assertTrue(loop_manager.resume_guidance_approval_if_decided(
                self.connection, self.repo, repo, "main", self.state_dir
            ))
        resumed = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        self.assertEqual(resumed["status"], "IMPLEMENTING")
        self.assertEqual(resumed["worker_session_id"], "guidance-session")
        self.assertEqual(resumed["worktree"], str(self.worktree))

    def test_invalidated_guidance_is_not_published(self):
        repo, _, _ = self.prepare_guidance_block()
        self.connection.execute("UPDATE guidance_approvals SET invalidated_decision_hash = 'invalid'")
        self.connection.commit()
        with mock.patch("loop_manager.pr_for_branch") as query:
            loop_manager.dispatch_blocked_checkpoints(self.connection, self.repo, self.state_dir, repo)
        query.assert_not_called()
        row = self.connection.execute("SELECT phase FROM blocked_checkpoints").fetchone()
        self.assertEqual(row["phase"], "unpublished")

    def test_guidance_commit_interruption_resumes_without_duplicate_commit(self):
        repo, _, _ = self.prepare_guidance_block()
        with mock.patch("loop_manager.pr_for_branch", return_value=None), mock.patch.object(
            loop_manager._BlockedCheckpointAdapter,
            "_record_guidance_checkpoint_scope", side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.dispatch_blocked_checkpoints(self.connection, self.repo, self.state_dir, repo)
        committed = self.run_git(self.worktree, "rev-parse", "HEAD")
        self.assertEqual(self.connection.execute("SELECT phase FROM blocked_checkpoints").fetchone()[0], "prepared")
        with mock.patch("loop_manager.pr_for_branch", return_value=None):
            loop_manager.dispatch_blocked_checkpoints(self.connection, self.repo, self.state_dir, repo)
        checkpoint = self.connection.execute("SELECT * FROM blocked_checkpoints").fetchone()
        self.assertEqual(checkpoint["phase"], "published", checkpoint["unpublished_reason"])
        self.assertEqual(checkpoint["remote_oid"], committed)
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), committed)
        plan = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        self.assertEqual(loop_manager.GitSnapshot.deserialize(plan["agent_snapshot"]).head, committed)

    def test_guidance_push_interruption_reuses_published_oid(self):
        repo, _, _ = self.prepare_guidance_block()
        with mock.patch("loop_manager.pr_for_branch", return_value=None), mock.patch.object(
            blocked_checkpoint_state.BlockedCheckpointRepository,
            "finish_published", side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.dispatch_blocked_checkpoints(
                    self.connection, self.repo, self.state_dir, repo,
                )
        oid = self.run_git(self.worktree, "rev-parse", "HEAD")
        self.assertEqual(self.connection.execute(
            "SELECT phase, local_oid FROM blocked_checkpoints",
        ).fetchone()[:], ("local_committed", oid))
        with mock.patch("loop_manager.pr_for_branch", return_value=None), mock.patch.object(
            loop_manager._BlockedCheckpointAdapter, "push",
            side_effect=AssertionError("duplicate push"),
        ):
            self.assertEqual(loop_manager.dispatch_blocked_checkpoints(
                self.connection, self.repo, self.state_dir, repo,
            ), 1)
        self.assertEqual(self.connection.execute(
            "SELECT phase, published_oid FROM blocked_checkpoints",
        ).fetchone()[:], ("published", oid))
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), oid)

    def test_closed_pr_without_agent_snapshot_stays_plan_blocked_and_reopens(self):
        repo = str(self.repo.resolve())
        plan = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        loop_manager.hydrate_execplan_project(self.connection, repo, plan)
        loop_manager.clear_agent_scope(self.connection, repo)
        runtime_state.RuntimeStateRepository(self.connection).open_pull_request(
            repo, pr_url="https://github.com/example/project/pull/1", pr_head_oid=self.base_oid
        )
        plan = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        self.assertIsNone(plan["agent_snapshot"])
        self.assertIsNone(plan["agent_result_snapshot"])
        loop_manager._block_closed_pull_request(self.connection, repo, plan, plan["pr_url"], self.base_oid)
        with mock.patch("loop_manager.pr_for_branch") as query:
            loop_manager.dispatch_blocked_checkpoints(self.connection, self.repo, self.state_dir, repo)
        query.assert_not_called()
        self.assertIsNone(self.connection.execute("SELECT * FROM project_blocks").fetchone())
        blocked = self.connection.execute("SELECT * FROM exec_plans").fetchone()
        self.assertEqual(blocked["status"], "PLAN_BLOCKED")
        self.assertEqual(blocked["blocked_reason"], "pull_request_closed")
        self.assertEqual(self.run_git(self.worktree, "rev-parse", "HEAD"), self.base_oid)
        loop_manager._resume_reopened_pull_request(
            self.connection, repo, blocked, blocked["pr_url"], self.base_oid, recovery=False
        )
        self.assertEqual(self.connection.execute("SELECT status FROM exec_plans").fetchone()[0], "PR_OPEN")

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
