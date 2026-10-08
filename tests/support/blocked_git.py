from tests.support.lifecycle import fixture_cleanup
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest import mock
from loop_manager_internal.storage import blocked_checkpoint_state
import loop_manager
from loop_manager_internal.notifications import notifications
from loop_manager_internal.storage import runtime_state


class BlockedGitFixture:
    """Reusable setup and adapters; no discoverable test methods."""

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

    def setUp(self, *, directory=None):
        pr_query = mock.patch.object(loop_manager, 'pr_for_branch', return_value=None)
        pr_query.start()
        self.addCleanup(pr_query.stop)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temporary.cleanup)
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
        self.addCleanup(self.connection.close)
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


def blocked_git_fixture(test, *, directory=None):
    fixture = fixture_cleanup(test, BlockedGitFixture())
    fixture.setUp(directory=directory)
    test.addCleanup(fixture.tearDown)
    return fixture


from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode

class CheckpointGitRecoveryFixture(BlockedGitFixture):
    def _project_block_fixture(self, *, known=False, advance=True):
        if known:
            repo, original = self._remote_refresh_fixture(base_changed=advance)
        else:
            self.block()
            repo, original = str(self.repo.resolve()), self.base_oid
            self.run_git(self.repo, 'push', 'origin', f'{original}:refs/heads/loop-manager/G-034')
            (self.worktree / 'code.py').write_text('diagnostic artifact\n', encoding='utf-8')
            if advance:
                (self.repo / 'dependency.py').write_text('dependency = True\n', encoding='utf-8')
                self.run_git(self.repo, 'add', 'dependency.py')
                self.run_git(self.repo, 'commit', '-m', 'Advance diagnostic recovery base')
                self.run_git(self.repo, 'push', 'origin', 'main')
        self.connection.execute("UPDATE projects SET phase = 'PROJECT_BLOCKED', last_error = 'preserved project cause'")
        self.connection.execute(
            "INSERT INTO project_blocks (repo, scope, reason_code, plan_id, evidence, previous_phase) "
            "VALUES (?, 'project', 'publication_ownership_uncertain', 'G-034', 'preserved project cause', 'PLAN_BLOCKED')",
            (repo,))
        self.connection.commit()
        project = self.connection.execute('SELECT * FROM projects').fetchone()
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        coordinator = loop_manager.project_blocked_recovery.ProjectBlockedReconciler(
            loop_manager._project_blocked_recovery_effects(), self.connection, self.repo, self.state_dir, repo, 'main')
        return repo, original, coordinator


    def _remote_refresh_fixture(self, *, base_changed=False, branch_changed=False):
        self.block()
        (self.worktree / 'code.py').write_text('checkpoint\n', encoding='utf-8')
        repo = str(self.repo.resolve())
        loop_manager.dispatch_blocked_checkpoints(self.connection, self.repo, self.state_dir, repo)
        published = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        if branch_changed:
            self.user_commit()
        if base_changed:
            (self.repo / 'dependency.py').write_text('dependency = True\n', encoding='utf-8')
            self.run_git(self.repo, 'add', 'dependency.py')
            self.run_git(self.repo, 'commit', '-m', 'Advance dependency without changing specification')
            self.run_git(self.repo, 'push', 'origin', 'main')
        project = self.connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()
        loop_manager.observe_blocked_plan_remotes(self.connection, self.repo, repo, project)
        return repo, published


    def _assert_remote_refresh(self, *, base_changed, branch_changed, resolved):
        repo, published = self._remote_refresh_fixture(base_changed=base_changed, branch_changed=branch_changed)
        source_head = self.run_git(self.repo, 'rev-parse', 'HEAD')
        with mock.patch.object(loop_manager, '_evaluate_remote_checkpoint_cause',
                               return_value=(resolved, 'original cause assessment', None)) as assess:
            self.assertEqual(loop_manager.resume_plan_blocks(
                self.connection, self.repo, self.state_dir, repo, 'main'), resolved)
            plan = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id = 'G-034'").fetchone()
            head = self.run_git(self.worktree, 'rev-parse', 'HEAD')
            self.assertEqual(plan['status'], 'IMPLEMENTING' if resolved else 'PLAN_BLOCKED')
            self.assertEqual(self.run_git(self.repo, 'rev-parse', 'HEAD'), source_head)
            if base_changed:
                self.run_git(self.worktree, 'merge-base', '--is-ancestor', source_head, head)
                self.assertNotEqual(head, published)
            if branch_changed:
                self.assertEqual((self.worktree / 'code.py').read_text(), 'corrected\n')
            remote = loop_manager._BlockedCheckpointAdapter(self.connection, self.repo, self.state_dir, repo).observe_remote(
                blocked_checkpoint_state.BlockedCheckpointRepository(self.connection).get(repo, 'G-034'))
            self.assertEqual(remote, head)
            if not resolved:
                assess.reset_mock()
                with mock.patch.object(loop_manager, '_journaled_direct_rebase') as rebase:
                    self.assertFalse(loop_manager.resume_plan_blocks(
                        self.connection, self.repo, self.state_dir, repo, 'main'))
                assess.assert_not_called()
                rebase.assert_not_called()


    def _owned_checkpoint_release(self, *, dirty=False, tamper=False,
                                  tamper_preserved=False, after_save=False, staged=False):
        repo_id = str(self.repo.resolve())
        (self.worktree / 'code.py').write_text('checkpoint\n', encoding='utf-8')
        self.run_git(self.worktree, 'add', 'code.py')
        self.run_git(
            self.worktree, 'commit', '-m',
            '[G-034] BLOCKED checkpoint\n\nScope: plan\n'
            'Checkpoint-Identity: owned-identity',
        )
        published = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        tree = self.run_git(self.worktree, 'rev-parse', 'HEAD^{tree}')
        request = self.block(evidence='code.py: failed')
        self.run_git(
            self.worktree, 'push', 'origin',
            'HEAD:refs/heads/loop-manager/G-034',
        )
        blocked_checkpoint_state.BlockedCheckpointRepository(
            self.connection,
        ).finish_published(request, published)
        self.connection.execute(
            "UPDATE blocked_checkpoints SET commit_identity = 'owned-identity', "
            "tree_oid = ? WHERE repo = ? AND plan_id = 'G-034'",
            (tree, repo_id),
        )
        plan = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = 'G-034'",
            (repo_id,),
        ).fetchone()
        old_input = loop_manager._revision_execplan_input_fingerprint(
            self.repo, 'refs/remotes/origin/main', plan,
        )
        old_worktree = loop_manager._worktree_execplan_input_fingerprint(
            self.worktree, plan,
        )
        self.connection.execute(
            "UPDATE exec_plans SET blocked_fingerprint = ?, "
            "blocked_worktree_fingerprint = ? WHERE repo = ? AND plan_id = 'G-034'",
            (old_input, old_worktree, repo_id),
        )
        self.connection.commit()
        (self.repo / 'docs/PRODUCT.md').write_text('product\nnew input\n')
        self.run_git(self.repo, 'add', 'docs/PRODUCT.md')
        self.run_git(self.repo, 'commit', '-m', 'Change product input')
        self.run_git(self.repo, 'push', 'origin', 'main')

        if dirty:
            if staged:
                (self.worktree / 'code.py').write_text(
                    'checkpoint\npreserved local edit\n', encoding='utf-8',
                )
                self.run_git(self.worktree, 'add', 'code.py')
            else:
                (self.worktree / 'dependency.py').write_text(
                    'preserved local edit\n', encoding='utf-8',
                )
        record = loop_manager._record_blocked_resume_head

        def interrupt_after_rebase(*arguments):
            if arguments[6] is not None:
                if after_save:
                    record(*arguments)
                raise KeyboardInterrupt('after rebase before checkpoint save')
            return record(*arguments)

        actual_git = loop_manager.git

        def interrupt_autostash(*arguments, **kwargs):
            result = actual_git(*arguments, **kwargs)
            if staged and arguments[1] == 'rebase':
                raise KeyboardInterrupt('after autostash before index restore')
            return result

        with (
            mock.patch.object(loop_manager, 'git', side_effect=interrupt_autostash),
            mock.patch.object(loop_manager, '_record_blocked_resume_head',
                              side_effect=interrupt_after_rebase),
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop_manager.resume_blocked_execplan_if_inputs_changed(
                    self.connection, self.repo, repo_id, 'main', self.state_dir,
                )
        pending = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(repo_id, 'G-034')
        self.assertEqual(bool(pending.snapshot['blocked_resume']['after_head']), after_save)
        if staged:
            self.assertEqual(
                self.run_git(self.worktree, 'diff', '--cached', '--name-only'), '',
            )
        if tamper:
            (self.worktree / 'docs/requirements').mkdir(parents=True, exist_ok=True)
            (self.worktree / 'docs/requirements/Unexpected.md').write_text('unexpected scope edit\n')
        if tamper_preserved:
            (self.worktree / 'dependency.py').write_text('changed after rebase\n')
        resumed = loop_manager.resume_blocked_execplan_if_inputs_changed(
            self.connection, self.repo, repo_id, 'main', self.state_dir,
        )

        if tamper or tamper_preserved:
            self.assertFalse(resumed)
            self.assertEqual(self.connection.execute(
                "SELECT status FROM exec_plans WHERE plan_id = 'G-034'",
            ).fetchone()[0], 'PLAN_BLOCKED')
            return
        if dirty:
            self.assertTrue(resumed)
            if staged:
                self.assertEqual(self.run_git(self.worktree, 'diff', '--cached', '--name-only'), 'code.py')
                self.assertEqual((self.worktree / 'code.py').read_text(),
                                 'checkpoint\npreserved local edit\n')
            else:
                self.assertEqual((self.worktree / 'dependency.py').read_text(),
                                 'preserved local edit\n')
            return
        self.assertTrue(resumed, self.connection.execute(
            "SELECT last_error FROM exec_plans WHERE repo = ? AND plan_id = 'G-034'",
            (repo_id,),
        ).fetchone()[0])
        plan = self.connection.execute(
            "SELECT status, publication_remote_oid FROM exec_plans "
            "WHERE repo = ? AND plan_id = 'G-034'", (repo_id,),
        ).fetchone()
        rebased = self.run_git(self.worktree, 'rev-parse', 'HEAD')
        self.assertNotEqual(rebased, published)
        self.assertEqual(tuple(plan), ('READY', rebased))
        self.assertEqual(self.run_git(self.repo, 'ls-remote', '--heads', 'origin',
                                      'refs/heads/loop-manager/G-034').split()[0],
                         rebased)
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        ).get(repo_id, 'G-034')
        self.assertEqual(checkpoint.snapshot['blocked_resume']['before_head'], published)
        self.assertEqual(checkpoint.snapshot['blocked_resume']['after_head'], rebased)
        self.assertEqual(checkpoint.observed_remote_oid, rebased)
        self.assertEqual(checkpoint.snapshot['last_adopted_remote_oid'], rebased)
        self.assertEqual(checkpoint.snapshot['rebase_publication_intent']['remote_before'], published)

        # Independently recreate a pre-publication legacy project block. The
        # immediate checkpoint above has already been verified against origin.
        self.run_git(self.remote, 'update-ref', 'refs/heads/loop-manager/G-034', published, rebased)
        self.run_git(self.repo, 'fetch', 'origin')

        target = self.run_git(self.repo, 'rev-parse', 'refs/remotes/origin/main')
        runtime_state.RuntimeStateRepository(self.connection).activate(
            repo_id, 'G-034', activation_sha=target,
            branch='loop-manager/G-034', worktree=str(self.worktree),
        )
        checkpoint_store = execution_checkpoint_state.ExecutionCheckpointRepository(
            self.connection,
        )
        current = checkpoint_store.get(repo_id, 'G-034')
        checkpoint_store.record(
            repo_id, 'G-034', expected_generation=current.generation,
            operation=None, expected_head=rebased, target_base_oid=target,
            observed_remote_oid=published,
            snapshot={'reactivated_from_head': published,
                      'base_oid': target, 'local_head': rebased},
        )
        with self.connection:
            self.connection.execute(
                "UPDATE exec_plans SET publication_remote_oid = NULL, "
                "worker_session_id = 'retained-session' "
                "WHERE repo = ? AND plan_id = 'G-034'", (repo_id,),
            )
            self.connection.execute(
                "UPDATE projects SET publication_remote_oid = NULL, "
                "phase = 'PROJECT_BLOCKED' WHERE repo = ?", (repo_id,),
            )
            self.connection.execute(
                "INSERT INTO project_blocks "
                "(repo, scope, reason_code, plan_id, evidence, previous_phase) "
                "VALUES (?, 'project', 'publication_ownership_uncertain', "
                "'G-034', 'external_remote_oid', 'WORKING')", (repo_id,),
            )
        with mock.patch.object(loop_manager, 'pr_for_branch', return_value=None):
            self.assertTrue(loop_manager.resume_project_block_if_safe(
                self.connection, self.repo, self.state_dir, repo_id,
            ))
        recovered = self.connection.execute(
            "SELECT status, publication_remote_oid, worker_session_id "
            "FROM exec_plans WHERE repo = ? AND plan_id = 'G-034'",
            (repo_id,),
        ).fetchone()
        self.assertEqual(tuple(recovered),
                         ('IMPLEMENTING', published, 'retained-session'))
        self.assertIsNone(scoped_blocking.project_block(self.connection, repo_id))
        restored_checkpoint = checkpoint_store.get(repo_id, 'G-034')
        self.assertEqual(restored_checkpoint.snapshot['blocked_resume'], {
            'owner_oid': published, 'before_head': published,
            'after_head': rebased, 'target_base_oid': target,
        })


    def publish_block(self, reason="verify_retry_exhausted",
                      evidence="code.py: same verify failure"):
        (self.worktree / "code.py").write_text("checkpoint\n", encoding="utf-8")
        self.run_git(self.worktree, "add", "code.py")
        self.run_git(self.worktree, "commit", "-m", "Blocked checkpoint")
        if reason == "review_retry_exhausted":
            self.connection.execute(
                "UPDATE exec_plans SET worker_result = ? WHERE plan_id = 'G-034'",
                (
                    "COMMIT_TITLE: Repair checkpoint review issue\n"
                    "PR_SUMMARY: Preserve the blocked review evidence.\n"
                    "TARGETED_TESTS:\ntest_checkpoint: PASS",
                ),
            )
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


    def prepare_guidance_block(self, *, implementation_changes=True):
        from tests.support.guidance import guidance_plan

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
        after = loop_manager.capture_git_snapshot(self.worktree)
        loop_manager.update_project(self.connection, repo, agent_result_snapshot=after.serialize())
        runtime_state.RuntimeStateRepository(self.connection).block_plan(
            repo, str(plan['plan_id']), reason_code=loop_manager.GUIDANCE_BLOCK_REASON,
            evidence='historical wait', release_fingerprint='historical-document',
            plan_values={'guidance_plan_hash': 'historical-document'},
        )
        return repo, plan_path, text


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
