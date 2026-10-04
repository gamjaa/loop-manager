import sqlite3
import unittest
from pathlib import Path
import loop_manager
from loop_manager_internal.storage import planning_input_state
from loop_manager_internal.agents import agent_write_isolation as isolation
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push


def symlink_or_skip(link: Path, target, *, target_is_directory=False):
    """Windows의 symlink 권한 부재만 건너뛰고 다른 오류는 실패시킨다."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if getattr(error, 'winerror', None) == 1314:
            raise unittest.SkipTest('Windows symlink 생성 권한이 없습니다.') from error
        raise


class WorkspaceFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _workspace(
        self, directory: str
    ) -> tuple[Path, Path, str, Path]:
        root = Path(directory)
        repo = make_remote_repository(directory)
        state_dir = root / "state"
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-012"
        )
        loop_manager.make_worktree(repo, worktree, branch, "main")
        return repo, state_dir, branch, worktree

    def _ready_execplan(self, directory: str):
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-012", "Recovery Safety")
        commit_and_push(repo, "Add recovery safety plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(repo),),
        )
        connection.commit()
        relative_plan = plan.relative_to(repo).as_posix()
        loop_manager.register_execplans(
            connection,
            str(repo),
            repo,
            [(relative_plan, loop_manager._parse_execplan(repo, plan))],
            plan_sha,
            loop_manager.planning_input_fingerprint(repo, "main"),
        )
        with connection:
            planning_input_state.record_general(
                connection, str(repo), plan_sha,
                loop_manager.plan_declarations(repo, plan_sha),
            )
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-012"
        )
        return repo, state_dir, connection, plan, plan_sha, branch, worktree


class CleanupFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _managed_worktree(self, root: Path, name: str = "managed"):
        repo = make_remote_repository(str(root))
        worktree = root / name
        branch = f"loop-manager/{name}"
        git_command(repo, "worktree", "add", "-b", branch, str(worktree), "origin/main")
        (worktree / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")
        git_command(worktree, "add", "src/app.py")
        git_command(worktree, "commit", "-m", "Implement cleanup fixture")
        return repo, worktree, branch, git_command(worktree, "rev-parse", "HEAD")

    def _memory_connection(self, repo: Path):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(loop_manager.CLEANUP_PENDING_SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(repo),),
        )
        connection.commit()
        return connection


def native_agent_boundary(root: Path):
    """Build synthetic linked-worktree/config files for native provider unit tests."""
    root = root.resolve()
    state_root = root / 'state'
    selected = state_root / 'worktrees' / 'project' / 'G-088'
    source = root / 'source'
    other = root / 'other'
    common = source / '.git'
    git_dir = common / 'worktrees' / 'G-088'
    home = root / 'user-codex'
    for path in (selected, other, git_dir, home):
        path.mkdir(parents=True)
    (selected / '.git').write_text('gitdir: ' + str(git_dir))
    for path in (common / 'HEAD', git_dir / 'HEAD'):
        path.write_text('ref: refs/heads/main\n')
    (home / 'auth.json').write_text('private-auth-placeholder')
    (home / 'config.toml').write_text('model="test"\n')
    return isolation.WorktreeBoundary(selected, source, (other,), git_dir, common,
                                     'repository'), state_root, home
