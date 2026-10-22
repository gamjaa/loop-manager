import json
from dataclasses import replace
from contextlib import ExitStack
import sqlite3
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.catalog import make_catalog_root, write_execplan
from tests.support.worker import make_active_worker_project, worker_result
from tests.support.publication import matching_branch_pr_lookup
from loop_manager_internal.verification.timing import phase
from tests.support.lifecycle import register_connection_cleanup
from tests.support.execution import execution_policy_fixture, run_execution_policy


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
        close_connection = None
        if owns_connection:
            connection = sqlite3.connect(":memory:")
            close_connection = register_connection_cleanup(self, connection)
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
            if close_connection is not None:
                close_connection()
            raise
        return root, connection

    def _active_worker_project(
        self, directory: str
    ) -> tuple[Path, Path, sqlite3.Connection]:
        return make_active_worker_project(
            self,
            directory,
            plan_id="G-001",
            title="Worker Boundary",
            record_general_input=True,
            commit_message="Add worker plan",
        )

    def _run_review_sequence(
        self,
        directory: str,
        review_messages: tuple[str, ...],
        *,
        publish: bool,
        boundary=None,
    ) -> dict[str, object]:
        if boundary is None:
            return self._mock_review_sequence(review_messages, publish=publish)
        # This explicit boundary variant verifies restoration of the same real
        # Git/index evidence and SQLite preparation, not just Review policy.
        if boundary is not None and Path(directory).resolve() != Path(boundary.directory).resolve():
            raise RuntimeError('Review 준비 경로가 복구 경계와 다릅니다')
        reused = boundary is not None and boundary.saved
        if reused:
            if boundary.review_inputs != (publish,):
                raise RuntimeError('Review 준비 입력이 다릅니다')
            repo, state_dir, connection = boundary.review_state
            connection = boundary.restore(connection)
            boundary.review_state = repo, state_dir, connection
            with phase('git_fixture:review_confirmation'):
                # Compare freshly read evidence at the same owned paths. The
                # saved snapshot is a comparison, never a current receipt.
                if loop_manager.capture_git_snapshot(boundary.review_worktree) != boundary.review_snapshot:
                    raise RuntimeError('Review 준비 Git 상태가 다릅니다')
        else:
            with phase('git_fixture:review_initialization'):
                repo, state_dir, connection = self._active_worker_project(directory)
        from loop_manager_internal.execution.execution_handlers import ExecutionHandlers
        review_entry = ExecutionHandlers.review

        def save_review_entry(handler, context):
            preparation.close()
            if boundary is not None and not boundary.saved:
                boundary.review_state = repo, state_dir, connection
                boundary.review_inputs = (publish,)
                boundary.review_worker_version = worker_version
                boundary.review_worktree = context.worktree
                with phase('git_fixture:review_confirmation'):
                    boundary.review_snapshot = loop_manager.capture_git_snapshot(context.worktree)
                boundary.save(connection)
            return review_entry(handler, context)
        roles = []
        reviewer_sessions = []
        reviewer_prompts = []
        worker_version = boundary.review_worker_version if reused else 1
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

        with mock.patch('loop_manager_internal.git.publication_service.push_worker_checkpoint_commits',
                        return_value=(None, 0)), ExitStack() as preparation, mock.patch.object(
            ExecutionHandlers, 'review', save_review_entry
        ), mock.patch(
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
            if not reused:
                preparation.enter_context(phase('git_fixture:review_worker_verify'))
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

    def _mock_review_sequence(self, review_messages, *, publish):
        fixture = execution_policy_fixture(self)
        roles, sessions, prompts, transitions = [], [], [], []
        responses = iter(review_messages)
        original = fixture.effects.validation.transition_validation_failure

        def transition(*args, **kwargs):
            decision = original(*args, **kwargs)
            transitions.append((decision.stage, decision.fingerprint,
                                decision.occurrences, decision.blocked))
            return decision

        fixture.effects = replace(fixture.effects, validation=replace(
            fixture.effects.validation, transition_validation_failure=transition))
        fixture.handler.effects = fixture.effects

        def agent(*args, **kwargs):
            role, prompt = args[4:6]
            roles.append(role)
            kwargs['on_session_started'](role + '-session')
            if role == 'worker':
                fixture.dirty = True
                return loop_manager.AgentResult(0, worker_result(
                    f'Record review sequence {len(roles)}'), session_id='worker-session')
            sessions.append(kwargs['session_id'])
            prompts.append(prompt)
            return loop_manager.AgentResult(0, next(responses), session_id='reviewer-session')

        fixture.effects.agent.run_agent.side_effect = agent
        result = run_execution_policy(fixture)
        if not publish:
            fixture.effects.publication.command.assert_not_called()
            fixture.effects.publication.push_managed_publication_branch.assert_not_called()
        project = fixture.connection.execute(
            'SELECT * FROM projects WHERE repo = ?', (fixture.repo,)).fetchone()
        plan = fixture.connection.execute(
            'SELECT * FROM exec_plans WHERE repo = ?', (fixture.repo,)).fetchone()
        checkpoint = fixture.connection.execute(
            'SELECT phase, published_oid FROM blocked_checkpoints WHERE repo = ?',
            (fixture.repo,)).fetchone()
        return dict(result=result, roles=roles, reviewer_sessions=sessions,
                    reviewer_prompts=prompts, transitions=transitions,
                    project=project, plan=plan, blocked_checkpoint=checkpoint,
                    verify_calls=fixture.effects.validation.verify.call_count)
