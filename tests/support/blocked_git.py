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

    def setUp(self):
        pr_query = mock.patch.object(loop_manager, 'pr_for_branch', return_value=None)
        pr_query.start()
        self.addCleanup(pr_query.stop)
        self.temporary = tempfile.TemporaryDirectory()
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


def blocked_git_fixture(test):
    fixture = fixture_cleanup(test, BlockedGitFixture())
    fixture.setUp()
    test.addCleanup(fixture.tearDown)
    return fixture
