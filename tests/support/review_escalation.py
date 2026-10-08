import json
import sqlite3
from pathlib import Path
import loop_manager
from tests.support.catalog import make_catalog_root, write_execplan
from tests.support.worker import make_active_worker_project, worker_result
from tests.support.execution import execution_policy_fixture, run_execution_policy


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
        return root, state_dir, connection

    def _active_worker_project(
        self,
        directory: str,
    ) -> tuple[Path, Path, sqlite3.Connection]:
        return make_active_worker_project(
            self,
            directory,
            plan_id="G-019",
            title="Review Escalation",
            record_general_input=False,
            commit_message="Add escalation plan",
        )

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
        fixture = execution_policy_fixture(self)
        worker_levels = []
        responses = iter(reviewer_results)

        def agent(*args, **kwargs):
            role = args[4]
            kwargs['on_session_started'](role + '-session')
            if role == 'worker':
                worker_levels.append(kwargs['level'])
                fixture.dirty = True
                return loop_manager.AgentResult(
                    0, worker_result(f'Record escalation {len(worker_levels)}'),
                    session_id='worker-session',
                )
            return next(responses)

        fixture.effects.agent.run_agent.side_effect = agent
        result = run_execution_policy(fixture)
        if not publish:
            fixture.effects.publication.command.assert_not_called()
            fixture.effects.publication.push_managed_publication_branch.assert_not_called()
        plan = fixture.connection.execute(
            'SELECT * FROM exec_plans WHERE repo = ?', (fixture.repo,),
        ).fetchone()
        return result, worker_levels, plan
