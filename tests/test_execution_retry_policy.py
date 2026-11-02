from tests.support.recovery import BoundedRecoveryFixture
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import loop_manager
from tests.support.worker import worker_result
from tests.support.execution import execution_policy_fixture, run_execution_policy
from tests.support.worker import dummy_worker_sessions


class ExecutionRetryPolicyTest(BoundedRecoveryFixture, unittest.TestCase):
    def test_git_observation_failure_pauses_without_discarding_saved_worker(self):
        fixture = execution_policy_fixture(self)
        fixture.state.update_project(
            fixture.repo, worker_result='saved worker evidence', worker_session_id='worker-session')
        observe = fixture.effects.workspace.git.side_effect
        fetch = ('fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')

        def unavailable(path, *args, **kwargs):
            if args == fetch:
                raise loop_manager.GitLockError('external Git lock')
            return observe(path, *args, **kwargs)

        fixture.effects.workspace.git.side_effect = unavailable
        result = run_execution_policy(fixture)
        project = fixture.connection.execute(
            'SELECT * FROM projects WHERE repo = ?', (fixture.repo,)).fetchone()
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.TRANSIENT_PAUSE)
        self.assertEqual(project['worker_result'], 'saved worker evidence')
        self.assertEqual(project['worker_session_id'], 'worker-session')
        fixture.effects.agent.run_agent.assert_not_called()
        self.assertEqual(sum(call.args[1:] == fetch for call in fixture.effects.workspace.git.call_args_list), 3)
        recovery = fixture.connection.execute(
            'SELECT * FROM recovery_records WHERE repo = ?', (fixture.repo,)).fetchone()
        self.assertEqual(recovery['attempts'], 3)
        with self.assertRaisesRegex(AssertionError, 'unexpected policy Git'):
            observe(Path('/repo'), 'unexpected-command')

    @dummy_worker_sessions
    def test_verify_port_uses_supplied_timeout_without_rereading_config(self):
        from tests.support.database import runtime_connection
        from loop_manager_internal.storage import runtime_state

        connection = runtime_connection()
        self.addCleanup(connection.close)
        repository = runtime_state.RuntimeStateRepository(connection)
        repository.activate(
            'repo', 'G-047', activation_sha='base',
            branch='loop-manager/G-047', worktree='/state/G-047',
        )
        with mock.patch.object(loop_manager, 'verify', return_value=(True, 'OK')) as verify:
            ports = loop_manager._ExecutionLifecyclePorts(
                connection, repository, repo='repo', repo_path=Path('/repo'),
                state_dir=Path('/state'), base='main', base_sha='base',
                provider='codex', config_path=Path('unused.ini'),
                verify_timeout_seconds=42,
                newly_activated=False,
            )
            self.assertEqual(ports.effects.validation.verify(Path('/state/G-047')), (True, 'OK'))
        verify.assert_called_once_with(Path('/state/G-047'), 42)


    @dummy_worker_sessions
    def test_agent_retry_usage_limit_and_session_fallback_are_bounded(self):
        transient_calls = []

        def transient_then_success(session_id, recovery):
            transient_calls.append((session_id, recovery))
            if len(transient_calls) <= loop_manager.MAX_TRANSIENT_AGENT_RETRIES:
                return loop_manager.CodexResult(1, "service unavailable", session_id)
            return loop_manager.CodexResult(0, "ok", "resumed-session")

        recovered = loop_manager.run_agent_with_retries(
            transient_then_success,
            session_id="resumed-session",
        )
        self.assertEqual(recovered.result_kind, loop_manager.AgentResultKind.SUCCESS)
        self.assertEqual(
            len(transient_calls), loop_manager.MAX_TRANSIENT_AGENT_RETRIES + 1
        )

        timeout_calls = []

        def timeout_then_success(session_id, recovery):
            timeout_calls.append((session_id, recovery))
            if len(timeout_calls) == 1:
                raise subprocess.TimeoutExpired(["codex"], 1)
            return loop_manager.CodexResult(0, "ok", session_id)

        timeout_recovered = loop_manager.run_agent_with_retries(
            timeout_then_success,
            session_id="timeout-session",
        )
        self.assertEqual(
            timeout_recovered.result_kind, loop_manager.AgentResultKind.SUCCESS
        )
        self.assertEqual(len(timeout_calls), 2)

        usage_calls = []
        usage = loop_manager.run_agent_with_retries(
            lambda session_id, recovery: (
                usage_calls.append((session_id, recovery))
                or loop_manager.CodexResult(1, "usage limit reached", session_id)
            ),
            session_id="usage-session",
        )
        self.assertEqual(usage.result_kind, loop_manager.AgentResultKind.USAGE_LIMIT)
        self.assertEqual(len(usage_calls), 1)

        fallback_calls = []

        def fallback(session_id, recovery):
            fallback_calls.append((session_id, recovery))
            if session_id:
                return loop_manager.CodexResult(
                    1, "session not found", session_id
                )
            return loop_manager.CodexResult(0, "fresh", "fresh-session")

        fresh = loop_manager.run_agent_with_retries(
            fallback,
            session_id="missing-session",
        )
        self.assertEqual(fresh.session_id, "fresh-session")
        self.assertEqual(
            fallback_calls,
            [("missing-session", False), (None, True)],
        )
        recovered_prompt = loop_manager.worker_prompt(
            "docs/plans/[G-013] Recovery.md",
            "temporary CLI failure",
            "previous Worker result",
            "verify failure output",
            session_recovery=True,
        )
        self.assertNotIn("이전 Worker 결과", recovered_prompt)
        self.assertNotIn("previous Worker result", recovered_prompt)
        self.assertIn("verify failure output", recovered_prompt)

    @dummy_worker_sessions
    def test_project_results_distinguish_pause_block_and_operational_error(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = str((Path(directory) / "repo").resolve())
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            connection.execute(
                "INSERT INTO projects (repo, base_branch, phase, last_error) "
                "VALUES (?, 'main', 'WORKING', 'saved')",
                (repo,),
            )
            connection.commit()

            cases = (
                (
                    loop_manager.PROJECT_STEP_TRANSIENT_PAUSE,
                    "WORKING",
                    loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                    0,
                ),
                (
                    loop_manager.PROJECT_STEP_USAGE_PAUSE,
                    "REVIEWING",
                    loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE,
                    0,
                ),
                (1, "PROJECT_BLOCKED", loop_manager.ProjectRunKind.BLOCKED, 1),
                (
                    1,
                    "WORKING",
                    loop_manager.ProjectRunKind.OPERATIONAL_ERROR,
                    1,
                ),
            )
            for step, phase, expected_kind, expected_code in cases:
                with self.subTest(kind=expected_kind):
                    connection.execute(
                        "UPDATE projects SET phase = ? WHERE repo = ?",
                        (phase, repo),
                    )
                    connection.commit()
                    with mock.patch(
                        "loop_manager._run_project_step", return_value=step
                    ):
                        result = loop_manager.run_project_result(
                            connection,
                            Path(directory) / "state",
                            repo,
                            Path("unused.ini"),
                            "main",
                            False,
                        )
                    self.assertEqual(result.kind, expected_kind)
                    self.assertEqual(result.exit_code, expected_code)

    def test_distinct_verify_failures_recover_in_one_run_with_persistent_sessions(self):
        fixture = execution_policy_fixture(self)
        roles, worker_sessions, reviewer_sessions = [], [], []

        def agent(*args, **kwargs):
            role_name = args[4]
            roles.append(role_name)
            kwargs['on_session_started'](role_name + '-session')
            if role_name == 'worker':
                worker_sessions.append(kwargs['session_id'])
                fixture.dirty = True
                return loop_manager.AgentResult(
                    0, worker_result(f'Record recovery {len(worker_sessions)}'),
                    session_id='worker-session')
            reviewer_sessions.append(kwargs['session_id'])
            return loop_manager.AgentResult(0, 'VERDICT: PASS', session_id='reviewer-session')

        fixture.effects.agent.run_agent.side_effect = agent
        verify = fixture.effects.validation.verify
        verify.side_effect = [
            (False, 'failure A'), (False, 'failure B'),
            (False, 'failure C'), (False, 'failure D'), (True, 'verify: PASS'),
        ]
        result = run_execution_policy(fixture)
        project = fixture.connection.execute(
            'SELECT * FROM projects WHERE repo = ?', (fixture.repo,)).fetchone()
        plan = fixture.connection.execute(
            'SELECT * FROM exec_plans WHERE repo = ?', (fixture.repo,)).fetchone()

        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(verify.call_count, 5)
        self.assertEqual(roles, ["worker"] * 5 + ["reviewer"])
        self.assertEqual(worker_sessions, [None] + ["worker-session"] * 4)
        self.assertEqual(reviewer_sessions, [None])
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(plan["worker_session_id"], "worker-session")
        self.assertEqual(plan["reviewer_session_id"], "reviewer-session")
        self.assertEqual(json.loads(plan["verify_failure_history"]), {})
        self.assertEqual(json.loads(plan["review_failure_history"]), {})

    @dummy_worker_sessions
    def test_distinct_review_issues_wait_before_fourth_and_resume_in_same_run(self):
        messages = (
            "ISSUE_ID: app.py::issue-a::blocking\nissue A\nVERDICT: FAIL",
            "ISSUE_ID: app.py::issue-b::blocking\nissue B\nVERDICT: FAIL",
            "ISSUE_ID: app.py::issue-c::blocking\nissue C\nVERDICT: FAIL",
            "ISSUE_ID: app.py::issue-d::blocking\nissue D\nVERDICT: FAIL",
            "VERDICT: PASS",
        )
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory, messages, publish=True, retry_waits=True
            )

        result = data["result"]
        transitions = data["transitions"]
        project = data["project"]
        plan = data["plan"]
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(data["verify_calls"], 5)
        self.assertEqual(data["roles"], ["worker", "reviewer"] * 5)
        self.assertEqual(
            data["reviewer_sessions"],
            [None] + ["reviewer-session"] * 4,
        )
        self.assertEqual(
            [
                (stage, occurrences, blocked)
                for stage, _fingerprint, occurrences, blocked in transitions
            ],
            [("review", 1, False)] * 4,
        )
        self.assertEqual(
            len(
                {
                    fingerprint
                    for _stage, fingerprint, _occurrences, _blocked in transitions
                }
            ),
            4,
        )
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(len(data["review_histories"][-1]), 4)
        self.assertEqual(json.loads(plan["review_failure_history"]), {})
        self.assertAlmostEqual(sum(data["retry_delays"]), 30, places=3)
        self.assertEqual(plan['effective_worker_level'], 2)
        self.assertEqual(set(json.loads(plan['review_level_history'])), {'1'})

    @dummy_worker_sessions
    def test_review_issue_in_changing_sets_keeps_history_across_same_run_wait(self):
        messages = (
            "ISSUE_ID: app.py::a-shared-issue::blocking\n"
            "ISSUE_ID: app.py::z-extra-a::blocking\nVERDICT: FAIL",
            "ISSUE_ID: app.py::a-shared-issue::blocking\n"
            "ISSUE_ID: app.py::z-extra-b::blocking\nVERDICT: FAIL",
            "ISSUE_ID: app.py::a-shared-issue::blocking\n"
            "ISSUE_ID: app.py::z-extra-c::blocking\nVERDICT: FAIL",
            "ISSUE_ID: app.py::a-shared-issue::blocking\n"
            "ISSUE_ID: app.py::z-extra-d::blocking\nVERDICT: FAIL",
            "VERDICT: PASS",
        )
        with tempfile.TemporaryDirectory() as directory:
            data = self._run_review_sequence(
                directory, messages, publish=True, retry_waits=True
            )

        result = data["result"]
        transitions = data["transitions"]
        project = data["project"]
        plan = data["plan"]
        history = data["review_histories"][-1]
        self.assertEqual(json.loads(plan["review_failure_history"]), {})
        self.assertAlmostEqual(sum(data["retry_delays"]), 30, places=3)
        self.assertEqual(result.kind, loop_manager.ProjectRunKind.WAITING)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(data["verify_calls"], 5)
        self.assertEqual(data["roles"], ["worker", "reviewer"] * 5)
        self.assertEqual(
            data["reviewer_sessions"],
            [None] + ["reviewer-session"] * 4,
        )
        self.assertEqual(
            [
                (stage, occurrences, blocked)
                for stage, _fingerprint, occurrences, blocked in transitions
            ],
            [
                ("review", 1, False),
                ("review", 2, False),
                ("review", 3, False),
                ("review", 4, False),
            ],
        )
        self.assertEqual(
            len(
                {
                    fingerprint
                    for _stage, fingerprint, _occurrences, _blocked in transitions
                }
            ),
            1,
        )
        self.assertIsNone(project["phase"])
        self.assertEqual(plan["status"], "PR_OPEN")
        self.assertEqual(
            sorted(record["occurrences"] for record in history.values()),
            [1, 1, 1, 1, 4],
        )
        self.assertEqual(plan['effective_worker_level'], 2)
        # The fourth result establishes the new level's baseline; it is not
        # another failed correction at that level despite the shared issue.
        self.assertEqual(sorted(json.loads(plan['review_level_history'])), ['1', '2'])
