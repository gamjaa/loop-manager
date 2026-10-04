import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.storage import blocked_checkpoint_state
import loop_manager
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import checkpoint_ownership


from tests.support.blocked_git import CheckpointGitRecoveryFixture


class GuidanceCheckpointGitRecoveryTests(CheckpointGitRecoveryFixture, unittest.TestCase):
    def test_guidance_remote_decision_resumes_after_real_base_rebase(self):
        # Manager integration coverage: never run by the Worker.
        from tests.support.guidance import guidance_plan
        repo = str(self.repo.resolve())
        path = 'docs/plans/[G-034] Checkpoint.md'
        def document(**options):
            return (guidance_plan(**options).replace('G-020', 'G-034')
                    .replace('title: Guidance', 'title: Checkpoint')
                    .replace('priority: high', 'priority: critical'))
        (self.repo / 'AGENTS.md').write_text('# Agents\n', encoding='utf-8')
        (self.repo / path).write_text(document(), encoding='utf-8')
        self.run_git(self.repo, 'add', 'AGENTS.md', path)
        self.run_git(self.repo, 'commit', '-m', 'Guidance proposal fixture')
        self.run_git(self.repo, 'push', 'origin', 'main')
        self.run_git(self.worktree, 'merge', '--ff-only', 'origin/main')
        initial = loop_manager.capture_git_snapshot(self.worktree)
        self.connection.execute(
            "UPDATE exec_plans SET agent_role = 'worker', agent_snapshot = ?, worker_session_id = 'session'",
            (initial.serialize(),))
        self.connection.commit()
        (self.worktree / 'code.py').write_text('checkpoint work\n', encoding='utf-8')
        plan = self.connection.execute('SELECT * FROM exec_plans').fetchone()
        pending = loop_manager.parse_guidance_document(document())
        with mock.patch.object(loop_manager, 'is_loop_manager_repository', return_value=False):
            loop_manager._block_for_new_guidance(self.connection, self.repo, repo, self.worktree,
                plan, pending, pending.proposals, 'proposal')
            loop_manager.dispatch_blocked_checkpoints(self.connection, self.repo, self.state_dir, repo)
        checkpoint = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(repo, 'G-034')
        self.assertEqual(checkpoint.phase.value, 'published')
        snapshot = self.connection.execute('SELECT agent_snapshot FROM exec_plans').fetchone()[0]
        successor = self.user_commit(path, document(decision='approve', revision='user-1'))
        upstream = Path(self.temporary.name) / 'base-writer'
        self.run_git(self.repo, 'clone', '--branch', 'main', str(self.remote), str(upstream))
        self.run_git(upstream, 'config', 'user.name', 'User')
        self.run_git(upstream, 'config', 'user.email', 'user@example.com')
        (upstream / 'dependency.py').write_text('dependency = True\n', encoding='utf-8')
        self.run_git(upstream, 'add', 'dependency.py')
        self.run_git(upstream, 'commit', '-m', 'Advance base after guidance checkpoint')
        self.run_git(upstream, 'push', 'origin', 'main')
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        original_resume = loop_manager.resume_guidance_approval_if_decided
        def resume(*args, **kwargs):
            self.assertEqual(self.connection.execute('SELECT agent_snapshot FROM exec_plans').fetchone()[0], snapshot)
            return original_resume(*args, **kwargs)
        with (mock.patch.object(loop_manager, 'is_loop_manager_repository', return_value=False),
              mock.patch.object(loop_manager, 'resume_guidance_approval_if_decided', side_effect=resume)):
            self.assertTrue(loop_manager.resume_plan_blocks(self.connection, self.repo, self.state_dir, repo, 'main'),
                            self.connection.execute('SELECT last_error FROM exec_plans').fetchone()[0])
        head = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(head, successor)
        self.run_git(self.worktree, 'merge-base', '--is-ancestor', 'origin/main', head)
        self.assertEqual(self.connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'IMPLEMENTING')
        self.assertEqual(self.connection.execute('SELECT decision FROM guidance_approvals').fetchone()[0], 'approve')
        self.assertEqual(self.connection.execute('SELECT worker_session_id FROM exec_plans').fetchone()[0], 'session')


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


    def test_guidance_plan_only_checkpoint_tree_survives_restart(self):
        self._assert_guidance_checkpoint_tree_and_restart(
            implementation_changes=False,
        )


    def test_guidance_code_and_test_checkpoint_tree_survives_restart(self):
        self._assert_guidance_checkpoint_tree_and_restart(
            implementation_changes=True,
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
            loop_manager.checkpoint_adapter.BlockedCheckpointAdapter,
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
            loop_manager.checkpoint_adapter.BlockedCheckpointAdapter, "push",
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


class CheckpointGitSafetyTests(CheckpointGitRecoveryFixture, unittest.TestCase):
    def test_chained_url_rewrite_pushes_to_the_once_validated_target(self):
        other = Path(self.temporary.name) / 'other-remote.git'
        self.run_git(self.repo, 'init', '--bare', str(other))
        self.run_git(self.repo, 'remote', 'set-url', 'origin', 'alias://project')
        self.run_git(self.repo, 'config', '--add', f'url.{self.remote}.insteadOf', 'alias://project')
        self.run_git(self.repo, 'config', '--add', f'url.{other}.insteadOf', str(self.remote))
        request = self.block()
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()),
        )
        verdict = adapter.assess(request)
        self.assertTrue(verdict.safe, verdict.detail)
        self.assertEqual(adapter._validated_push_url(request), 'alias://project')
        local_oid = adapter.ensure_local_commit(request, adapter.prepare(request, verdict))
        adapter.push(request, local_oid, blocked_checkpoint.PushDecision(
            blocked_checkpoint.PushMode.CREATE))
        ref = 'refs/heads/loop-manager/G-034'
        self.assertEqual(self.run_git(self.remote, 'rev-parse', ref), local_oid)
        self.assertNotEqual(loop_manager.git(other, 'rev-parse', '--verify', ref, check=False).returncode, 0)

    def test_raw_push_target_cannot_resolve_as_another_remote_name(self):
        other = Path(self.temporary.name) / 'other-remote.git'
        self.run_git(self.repo, 'init', '--bare', str(other))
        self.run_git(self.repo, 'remote', 'add', 'other', str(other))
        self.run_git(self.repo, 'remote', 'set-url', 'origin', 'other')
        self.run_git(self.repo, 'config', '--add', f'url.{self.remote}.insteadOf', 'other')
        request = self.block()
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()),
        )
        with self.assertRaisesRegex(RuntimeError, '명시적 push URL'):
            adapter._validated_push_url(request)
        with self.assertRaises(blocked_checkpoint.CheckpointPushError):
            adapter.push(request, self.base_oid, blocked_checkpoint.PushDecision(
                blocked_checkpoint.PushMode.CREATE))
        ref = 'refs/heads/loop-manager/G-034'
        self.assertNotEqual(loop_manager.git(other, 'rev-parse', '--verify', ref, check=False).returncode, 0)

    def test_push_rechecks_registered_origin_after_safe_assessment(self):
        request = self.block()
        adapter = loop_manager._BlockedCheckpointAdapter(
            self.connection, self.repo, self.state_dir, str(self.repo.resolve()),
        )
        verdict = adapter.assess(request)
        self.assertTrue(verdict.safe, verdict.detail)
        local_oid = adapter.ensure_local_commit(request, adapter.prepare(request, verdict))
        other = Path(self.temporary.name) / "other-remote.git"
        self.run_git(self.repo, "init", "--bare", str(other))
        self.run_git(self.repo, "remote", "set-url", "origin", str(other))
        with self.assertRaises(blocked_checkpoint.CheckpointPushError):
            adapter.push(request, local_oid, blocked_checkpoint.PushDecision(
                blocked_checkpoint.PushMode.CREATE))
        self.assertTrue(self.worktree.exists())
        self.run_git(self.repo, "remote", "set-url", "origin", str(self.remote))
        adapter.push(request, local_oid, blocked_checkpoint.PushDecision(
            blocked_checkpoint.PushMode.CREATE))
        self.assertEqual(adapter.observe_remote(request), local_oid)

    def test_clean_checkpoint_identity_survives_reclassification(self):
        from loop_manager_internal.recovery import checkpoint_ownership
        repo = str(self.repo.resolve())
        store = blocked_checkpoint_state.BlockedCheckpointRepository(self.connection)
        request = self.block()
        head = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        tree = self.run_git(self.worktree, 'rev-parse', 'HEAD^{tree}')
        identity = blocked_checkpoint.checkpoint_identity(
            plan_id='G-034', reason_code=request.reason_code,
            evidence=request.evidence, parent_oid=head, tree_oid=tree,
            included_paths=(),
        )
        store.record_prepared(request, blocked_checkpoint.PreparedCheckpoint(
            head, tree, identity, '', (),
        ))
        store.record_local(request, head)
        self.run_git(self.worktree, 'push', 'origin', 'HEAD:refs/heads/loop-manager/G-034')
        store.finish_published(request, head)
        # Existing rows have no identity inputs until their first reclassification.
        self.connection.execute(
            'UPDATE blocked_checkpoints SET identity_reason_code = NULL, '
            'identity_evidence = NULL WHERE repo = ? AND plan_id = ?',
            (repo, 'G-034'),
        )
        for reason in ('changed-cause', 'changed-again'):
            store.reclassify_published(
                store.get(repo, 'G-034'), reason_code=reason, evidence='new evidence',
                release_fingerprint=reason, accepted_oid=head,
            )
            self.assertEqual(checkpoint_ownership.owned_remote_oid(
                self.connection, self.repo, repo, 'G-034', 'loop-manager/G-034',
                self.worktree, head, loop_manager.git,
            ), head)
        self.connection.execute("UPDATE blocked_checkpoints SET commit_identity = 'forged'")
        self.assertIsNone(checkpoint_ownership.owned_remote_oid(
            self.connection, self.repo, repo, 'G-034', 'loop-manager/G-034',
            self.worktree, head, loop_manager.git,
        ))


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
        loop_manager._perform_manager_base_fetch(
            self.connection, self.repo, intent, "main",
        )
        current = loop_manager.capture_git_snapshot(self.worktree)
        saved_db = Path(self.temporary.name) / "pending-fetch.sqlite"
        copy = sqlite3.connect(saved_db)
        try:
            self.connection.backup(copy)
        finally:
            copy.close()
        reopened = sqlite3.connect(saved_db)
        reopened.row_factory = sqlite3.Row
        try:
            resumed = loop_manager._BlockedCheckpointAdapter(
                reopened, self.repo, self.state_dir, repo,
            )
            self.assertIsNone(resumed._shared_safety_error(
                request, resumed._plan(request), current,
            ))
            self.assertEqual(loop_manager.validate_persisted_agent_scope(
                reopened, self.repo, self.state_dir, repo,
            ).refs["refs/remotes/origin/main"][0], current.refs["refs/remotes/origin/main"][0])
        finally:
            reopened.close()
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


    def test_interrupted_base_fetch_chain_survives_later_fetch_and_restart(self):
        repo, _, _ = self.prepare_guidance_block(implementation_changes=False)
        request = blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection,
        ).get(repo, "G-034")
        before = loop_manager.capture_git_snapshot(self.worktree)
        self._advance_base_remote()
        first = loop_manager._record_manager_base_fetch_intent(
            self.connection, self.repo, repo, "G-034", "main", before,
        )
        loop_manager._perform_manager_base_fetch(
            self.connection, self.repo, first, "main",
        )
        intermediate = loop_manager.capture_git_snapshot(self.worktree)
        self.assertEqual(loop_manager._proven_manager_base_refs(
            self.connection, self.repo, repo, "G-034", before, intermediate,
        ), frozenset({"refs/remotes/origin/main"}))

        other = Path(self.temporary.name) / "base-writer"
        (other / "dependency.py").write_text("dependency = False\n")
        self.run_git(other, "commit", "-am", "advance base again")
        self.run_git(other, "push", "origin", "main")
        second = loop_manager._record_manager_base_fetch_intent(
            self.connection, self.repo, repo, "G-034", "main", intermediate,
        )
        loop_manager._perform_manager_base_fetch(
            self.connection, self.repo, second, "main",
        )
        loop_manager._finish_manager_base_fetch(self.connection, self.repo, second)
        current = loop_manager.capture_git_snapshot(self.worktree)
        saved_db = Path(self.temporary.name) / "fetch-chain.sqlite"
        with sqlite3.connect(saved_db) as copy:
            self.connection.backup(copy)
        copy.close()
        reopened = sqlite3.connect(saved_db)
        reopened.row_factory = sqlite3.Row
        try:
            self.assertEqual(reopened.execute(
                "SELECT phase FROM manager_base_fetches WHERE id = ?", (first,),
            ).fetchone()[0], "pending")
            resumed = loop_manager._BlockedCheckpointAdapter(
                reopened, self.repo, self.state_dir, repo,
            )
            self.assertIsNone(resumed._shared_safety_error(
                request, resumed._plan(request), current,
            ))
            self.assertEqual(loop_manager.validate_persisted_agent_scope(
                reopened, self.repo, self.state_dir, repo,
            ).refs["refs/remotes/origin/main"], current.refs["refs/remotes/origin/main"])

            # A later completed intent cannot bridge missing proof of the first.
            self.run_git(self.repo, "reflog", "expire", "--expire=all",
                         "refs/remotes/origin/main")
            self.assertEqual(resumed._shared_safety_error(
                request, resumed._plan(request), current,
            ).reason_code, "shared_ref_mutation")
            with self.assertRaises(loop_manager.AgentScopeError):
                loop_manager.validate_persisted_agent_scope(
                    reopened, self.repo, self.state_dir, repo,
                )
        finally:
            reopened.close()


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
        with self.assertRaises(RuntimeError):
            loop_manager._perform_manager_base_fetch(
                self.connection, self.repo, intent, "main",
            )
        with self.assertRaisesRegex(RuntimeError, "ref 갱신 증거"):
            loop_manager._finish_manager_base_fetch(
                self.connection, self.repo, intent,
            )
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
