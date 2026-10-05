import json
import sqlite3
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import make_catalog_root, write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import worker_result
from tests.support.publication import publishing_branch_pr_lookup


class WorkerEscalationFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _level_state(
        self,
        directory: str,
        *,
        initial_level: int,
    ) -> tuple[Path, Path, sqlite3.Connection]:
        root = make_catalog_root(str(Path(directory) / "catalog"))
        plan = write_execplan(root, "G-019", "Review Escalation")
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        connection = loop_manager.connect_database(state_dir, create=True)
        try:
            connection.execute(
                """
                INSERT INTO projects (repo, base_branch, phase, plan_path, worktree)
                VALUES (?, 'main', 'REVIEWING', ?, ?)
                """,
                (str(root), plan.relative_to(root).as_posix(), str(root)),
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, worktree, initial_worker_level,
                    effective_worker_level
                ) VALUES (?, 'G-019', ?, 'Review Escalation', ?, '[]', 'critical',
                          'hash', 'REVIEWING', ?, ?, ?)
                """,
                (
                    str(root),
                    plan.relative_to(root).as_posix(),
                    json.dumps(["docs/PRODUCT.md"]),
                    str(root),
                    initial_level,
                    initial_level,
                ),
            )
            connection.commit()
        except BaseException:
            connection.close()
            raise
        self.addCleanup(connection.close)
        return root, state_dir, connection

    def _active_worker_project(
        self,
        directory: str,
    ) -> tuple[Path, Path, sqlite3.Connection]:
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-019", "Review Escalation")
        commit_and_push(repo, "Add escalation plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-019"
        )
        loop_manager.make_worktree(repo, worktree, branch, "main")
        connection = sqlite3.connect(":memory:")
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, active_sha, plan_sha, phase, branch,
                    worktree, plan_path
                ) VALUES (?, 'main', ?, ?, 'WORKING', ?, ?, ?)
                """,
                (
                    str(repo),
                    plan_sha,
                    plan_sha,
                    branch,
                    str(worktree),
                    plan.relative_to(repo).as_posix(),
                ),
            )
            connection.commit()
        except BaseException:
            connection.close()
            raise
        self.addCleanup(connection.close)
        return repo, state_dir, connection

    def _run_reviews(
        self,
        directory: str,
        reviewer_results: tuple[loop_manager.CodexResult, ...],
        *,
        publish: bool,
    ) -> tuple[
        loop_manager.ProjectRunResult,
        list[int],
        sqlite3.Row,
    ]:
        repo, state_dir, connection = self._active_worker_project(directory)
        worker_levels: list[int] = []
        worker_version = 1
        responses = iter(reviewer_results)

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            nonlocal worker_version
            if role_name == "worker":
                worker_levels.append(kwargs["level"])
                worker_version += 1
                (worktree / "src/app.py").write_text(
                    f"VALUE = {worker_version}\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(
                    0,
                    worker_result(f"Record escalation {worker_version}"),
                    "worker-session",
                )
            return next(responses)

        pr_lookup = (publishing_branch_pr_lookup(connection, "https://example.test/pr/19")
                     if publish else lambda *_args, **_kwargs: None)

        with mock.patch(
            "loop_manager.validate_repository", return_value=repo
        ), mock.patch(
            "loop_manager.remote_base", return_value="main"
        ), mock.patch(
            "loop_manager.ensure_safe_base_worktree"
        ), mock.patch(
            "loop_manager.pr_for_branch", side_effect=pr_lookup
        ), mock.patch(
            "loop_manager.run_codex", side_effect=agent
        ), mock.patch(
            "loop_manager.verify", return_value=(True, "verify: PASS")
        ):
            result = loop_manager.run_project_result(
                connection,
                state_dir,
                str(repo),
                Path("unused.ini"),
                "main",
                False,
            )

        plan = connection.execute(
            """
            SELECT status, review_failure_history, initial_worker_level,
                   effective_worker_level, review_level_history
            FROM exec_plans
            WHERE repo = ? AND plan_id = 'G-019'
            """,
            (str(repo),),
        ).fetchone()
        connection.close()
        return result, worker_levels, plan
