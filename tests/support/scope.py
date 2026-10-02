import sqlite3
from pathlib import Path
import loop_manager
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push


class AgentScopeFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _repository_with_plan(self, directory: str) -> tuple[Path, str]:
        repo = make_remote_repository(directory)
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-001", "Scope Safety")
        commit_and_push(repo, "Add scope safety plan")
        return repo, plan.relative_to(repo).as_posix()

    def _persist_interrupted_worker(
        self,
        state_dir: Path,
        repo: Path,
        plan_path: str,
        snapshot: loop_manager.GitSnapshot,
    ) -> None:
        state_dir.mkdir()
        database = sqlite3.connect(state_dir / "loop-manager.db")
        database.row_factory = sqlite3.Row
        database.execute(loop_manager.SCHEMA)
        database.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (str(repo),),
        )
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        loop_manager.register_execplans(
            database,
            str(repo),
            repo,
            [(plan_path, loop_manager._parse_execplan(repo, repo / plan_path))],
            plan_sha,
            loop_manager.planning_input_fingerprint(repo, "main"),
        )
        branch = "loop-manager/G-001"
        with database:
            database.execute(
                """
                UPDATE exec_plans
                SET status = 'IMPLEMENTING', active_sha = ?, branch = ?,
                    worktree = ?, workspace_owned = 1
                WHERE repo = ? AND plan_id = 'G-001'
                """,
                (plan_sha, branch, str(repo), str(repo)),
            )
            database.execute(
                """
                UPDATE projects
                SET active_sha = ?, plan_sha = ?, phase = 'WORKING',
                    branch = ?, worktree = ?, plan_path = ?,
                    agent_role = 'worker', agent_snapshot = ?
                WHERE repo = ?
                """,
                (
                    plan_sha,
                    plan_sha,
                    branch,
                    str(repo),
                    plan_path,
                    snapshot.serialize(),
                    str(repo),
                ),
            )
        database.close()
