import sqlite3
from pathlib import Path
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push


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
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        connection.execute(loop_manager.CLEANUP_PENDING_SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(repo),),
        )
        connection.commit()
        return connection
