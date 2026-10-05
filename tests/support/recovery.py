import json
from contextlib import nullcontext
import sqlite3
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import make_catalog_root, write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push
from tests.support.worker import worker_result
from tests.support.publication import matching_branch_pr_lookup


class BoundedRecoveryFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _failure_state(
        self,
        directory: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[Path, sqlite3.Connection]:
        root = make_catalog_root(directory)
        plan = write_execplan(root, "G-013", "Bounded Recovery")
        owns_connection = connection is None
        connection = connection or sqlite3.connect(":memory:")
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                """
                INSERT INTO projects (
                    repo, base_branch, phase, plan_path, worktree,
                    worker_session_id, reviewer_session_id
                ) VALUES (?, 'main', 'VERIFYING', ?, ?, 'worker-session', 'reviewer-session')
                """,
                (str(root), plan.relative_to(root).as_posix(), str(root)),
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status, worktree, worker_session_id,
                    reviewer_session_id
                ) VALUES (?, 'G-013', ?, 'Bounded Recovery', ?, '[]', 'critical',
                          'hash', 'VERIFYING', ?, 'worker-session', 'reviewer-session')
                """,
                (
                    str(root),
                    plan.relative_to(root).as_posix(),
                    json.dumps(["docs/PRODUCT.md"]),
                    str(root),
                ),
            )
            connection.commit()
        except BaseException:
            if owns_connection:
                connection.close()
            raise
        if owns_connection:
            self.addCleanup(connection.close)
        return root, connection

    def _active_worker_project(
        self, directory: str
    ) -> tuple[Path, Path, sqlite3.Connection]:
        repo = make_remote_repository(directory)
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        (repo / "docs/plans").mkdir()
        plan = write_execplan(repo, "G-001", "Worker Boundary")
        commit_and_push(repo, "Add worker plan")
        plan_sha = git_command(repo, "rev-parse", "origin/main")
        branch, worktree = loop_manager.expected_execplan_workspace(
            state_dir, str(repo), "G-001"
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

    def _run_review_sequence(
        self,
        directory: str,
        review_messages: tuple[str, ...],
        *,
        publish: bool,
        boundary=None,
    ) -> dict[str, object]:
        if boundary is not None and boundary.saved:
            repo, state_dir, connection = boundary.review_state
            boundary.restore(connection)
        else:
            repo, state_dir, connection = self._active_worker_project(directory)
        from loop_manager_internal.execution.execution_handlers import ExecutionHandlers
        review_entry = ExecutionHandlers.review

        def save_review_entry(handler, context):
            if not boundary.saved:
                boundary.review_state = repo, state_dir, connection
                boundary.save(connection)
            return review_entry(handler, context)
        roles = []
        reviewer_sessions = []
        reviewer_prompts = []
        worker_version = 1
        transitions = []
        original_transition = loop_manager.transition_validation_failure

        def record_transition(*args, **kwargs):
            decision = original_transition(*args, **kwargs)
            transitions.append(
                (
                    decision.stage,
                    decision.fingerprint,
                    decision.occurrences,
                    decision.blocked,
                )
            )
            return decision

        def agent(worktree, _state, _config, role_name, _prompt, **kwargs):
            nonlocal worker_version
            roles.append(role_name)
            if role_name == "worker":
                worker_version += 1
                (worktree / "src/app.py").write_text(
                    f"VALUE = {worker_version}\n", encoding="utf-8"
                )
                return loop_manager.CodexResult(
                    0,
                    worker_result(f"Record review sequence {worker_version}"),
                    "worker-session",
                )
            reviewer_sessions.append(kwargs["session_id"])
            reviewer_prompts.append(_prompt)
            return loop_manager.CodexResult(
                0, next(responses), "reviewer-session"
            )

        responses = iter(review_messages)

        def existing_pr(*_args, **_kwargs):
            phase = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?", (str(repo),)
            ).fetchone()["phase"]
            if publish and phase == "PUBLISHING":
                return {"url": "https://example.test/pr/13"}
            return None

        with (mock.patch.object(ExecutionHandlers, 'review', save_review_entry)
              if boundary is not None else nullcontext()), mock.patch(
            "loop_manager.validate_repository", return_value=repo
        ), mock.patch(
            "loop_manager.remote_base", return_value="main"
        ), mock.patch(
            "loop_manager.ensure_safe_base_worktree"
        ), mock.patch(
            "loop_manager.pr_for_branch", side_effect=matching_branch_pr_lookup(existing_pr)
        ), mock.patch(
            "loop_manager.run_codex", side_effect=agent
        ), mock.patch(
            "loop_manager.verify", return_value=(True, "verify: PASS")
        ) as verify, mock.patch(
            "loop_manager.transition_validation_failure",
            side_effect=record_transition,
        ):
            result = loop_manager.run_project_result(
                connection,
                state_dir,
                str(repo),
                Path("unused.ini"),
                "main",
                False,
            )

        project = connection.execute(
            "SELECT phase, worker_session_id, reviewer_session_id, "
            "last_error, review_result FROM projects WHERE repo = ?",
            (str(repo),),
        ).fetchone()
        plan = connection.execute(
            "SELECT status, last_error, review_result, review_failure_history, "
            "review_level_history, effective_worker_level "
            "FROM exec_plans WHERE repo = ? AND plan_id = 'G-001'",
            (str(repo),),
        ).fetchone()
        return {
            "result": result,
            "roles": roles,
            "reviewer_sessions": reviewer_sessions,
            "reviewer_prompts": reviewer_prompts,
            "transitions": transitions,
            "project": project,
            "plan": plan,
            "verify_calls": verify.call_count,
        }
