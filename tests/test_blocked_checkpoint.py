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

    def block(self, reason="verify_retry_exhausted"):
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET agent_result_snapshot = ?",
                (loop_manager.capture_git_snapshot(self.worktree).serialize(),),
            )
            runtime_state.RuntimeStateRepository(self.connection).block_plan(
                str(self.repo.resolve()),
                "G-034",
                reason_code=reason,
                evidence="same verify failure",
                release_fingerprint="fingerprint",
                clear_projection=True,
            )
        return blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection
        ).get(str(self.repo.resolve()), "G-034")

    def publish_block(self, reason="verify_retry_exhausted"):
        (self.worktree / "code.py").write_text("checkpoint\n", encoding="utf-8")
        self.run_git(self.worktree, "add", "code.py")
        self.run_git(self.worktree, "commit", "-m", "Blocked checkpoint")
        request = self.block(reason)
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
        self.user_commit("cache.txt", "remote cache\n")

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

    def prepare_guidance_block(self):
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
        (self.worktree / "code.py").write_text("safe implementation\n")
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
