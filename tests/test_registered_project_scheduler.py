import json
import contextlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
import loop_manager


class ConcurrentRegisteredProjectSchedulerTest(unittest.TestCase):
    def test_handoff_run_reaps_child_and_closes_pipes_on_interruption(self):
        for error in (KeyboardInterrupt(), RuntimeError("communication failed")):
            with self.subTest(error=type(error).__name__):
                gate = loop_manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
                token = loop_manager._runtime_handoff_signal.set(gate)
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    with mock.patch.object(
                        loop_manager.RuntimeHandoffGate, "popen", return_value=process,
                    ), mock.patch.object(process, "communicate", side_effect=error):
                        with self.assertRaises(type(error)) as raised:
                            loop_manager._handoff_run(["agent"], input=b"prompt")
                    self.assertIs(raised.exception, error)
                    self.assertIsNotNone(process.returncode)
                    self.assertIsNotNone(process.poll())
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        self.assertTrue(pipe.closed)
                finally:
                    loop_manager._runtime_handoff_signal.reset(token)
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        pipe.close()

    def test_handoff_launch_gate_allows_concurrent_running_agents(self):
        gate = loop_manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
        both_started = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        starts = 0
        results: list[int] = []

        class Process:
            returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def communicate(self, _input, *, timeout):
                self_test.assertTrue(release.wait(timeout=2))
                return "", None

        self_test = self

        def start_process(*_args, **_kwargs):
            nonlocal starts
            with guard:
                starts += 1
                if starts == 2:
                    both_started.set()
            return Process()

        def run_one() -> None:
            token = loop_manager._runtime_handoff_signal.set(gate)
            try:
                result = loop_manager._handoff_run(
                    ["agent"], input="prompt", text=True, timeout=2,
                )
                results.append(result.returncode)
            finally:
                loop_manager._runtime_handoff_signal.reset(token)

        with mock.patch("loop_manager.subprocess.Popen", side_effect=start_process):
            workers = [threading.Thread(target=run_one) for _ in range(2)]
            for worker in workers:
                worker.start()
            try:
                self.assertTrue(both_started.wait(timeout=2))
            finally:
                release.set()
                for worker in workers:
                    worker.join(timeout=2)
        self.assertEqual(sorted(results), [0, 0])

    def test_handoff_between_entry_check_and_launch_blocks_agent_and_verify(self):
        for phase in ("agent", "verify"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                worktree = Path(directory)
                (worktree / "scripts").mkdir()
                (worktree / "scripts/verify.sh").write_text("#!/bin/sh\nexit 0\n")
                gate = loop_manager.RuntimeHandoffGate(
                    threading.Event(), threading.Lock(),
                )
                entered = threading.Event()
                release = threading.Event()
                observed: list[type[BaseException]] = []

                def wait_before_launch(*_args, **_kwargs):
                    entered.set()
                    self.assertTrue(release.wait(timeout=2))
                    if phase == "agent":
                        return {}
                    return gate.popen(*_args, **_kwargs)

                def run_one() -> None:
                    token = loop_manager._runtime_handoff_signal.set(gate)
                    try:
                        if phase == "agent":
                            loop_manager.run_codex(
                                worktree, worktree / "state", worktree / "config.ini",
                                "planner", "prompt", read_only=True,
                            )
                        else:
                            loop_manager.verify(worktree)
                    except BaseException as error:
                        observed.append(type(error))
                    finally:
                        loop_manager._runtime_handoff_signal.reset(token)

                with mock.patch(
                    "loop_manager.role", return_value=("model", "medium"),
                ), mock.patch(
                    "loop_manager_internal.agents.provider_adapters.agent_environment",
                    side_effect=wait_before_launch,
                ), (
                    mock.patch(
                        "loop_manager._handoff_popen", side_effect=wait_before_launch,
                    ) if phase == "verify" else contextlib.nullcontext()
                ), mock.patch("loop_manager.subprocess.Popen") as popen:
                    worker = threading.Thread(target=run_one)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(timeout=2))
                        gate.stop()
                    finally:
                        release.set()
                        worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
                    popen.assert_not_called()
                self.assertEqual(observed, [loop_manager.SelfMergeRuntimeHandoff])

    def test_handoff_gate_blocks_new_publication_git_process(self):
        gate = loop_manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
        token = loop_manager._runtime_handoff_signal.set(gate)
        try:
            gate.stop()
            with mock.patch("loop_manager.subprocess.Popen") as popen:
                with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                    loop_manager.command(["git", "push", "origin", "branch"])
                popen.assert_not_called()
        finally:
            loop_manager._runtime_handoff_signal.reset(token)

    def test_preflight_handoff_returns_success_without_post_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            _state_dir, connection, config, _repos = self._scheduler_state(
                directory, ("manager",), max_workers=1,
            )
            try:
                with mock.patch(
                    "loop_manager.schedule_registered_projects",
                    side_effect=loop_manager.SelfMergeRuntimeHandoff,
                ), mock.patch(
                    "loop_manager.notifications.dispatch_pending",
                ) as dispatch:
                    self.assertEqual(
                        loop_manager.run_registered_projects(
                            connection, Path(directory) / "state", config,
                        ),
                        0,
                    )
                    dispatch.assert_called_once()
            finally:
                connection.close()

    def test_late_handoff_signal_stops_new_agent_verify_and_project_work(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, repos = self._scheduler_state(
                directory, ("manager",), max_workers=1,
            )
            gate = loop_manager.RuntimeHandoffGate(threading.Event(), threading.Lock())
            project = loop_manager.project_registry.list_projects(connection)[0]
            try:
                with mock.patch(
                    "loop_manager.run_project_result",
                    side_effect=loop_manager.SelfMergeRuntimeHandoff,
                ):
                    result = loop_manager._run_registered_project(
                        project, state_dir, config.path, "", "", threading.Lock(),
                        config.refactoring_review_interval,
                        handoff_gate=gate,
                    )
                self.assertEqual(
                    result.result.kind, loop_manager.ProjectRunKind.RUNTIME_HANDOFF,
                )
                self.assertTrue(gate.event.is_set())
                token = loop_manager._runtime_handoff_signal.set(gate)
                try:
                    with mock.patch("loop_manager.agent_provider") as agent:
                        with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                            loop_manager.run_agent(
                                "codex", Path(repos[0]), state_dir, config.path,
                                "worker", "prompt", read_only=False,
                            )
                        agent.assert_not_called()
                    with mock.patch("loop_manager.run_verify_process") as verify_process:
                        with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                            loop_manager.verify(Path(repos[0]))
                        verify_process.assert_not_called()
                finally:
                    loop_manager._runtime_handoff_signal.reset(token)
            finally:
                connection.close()

    def test_runtime_handoff_finishes_external_run_without_failure_event(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, repos = self._scheduler_state(
                directory, ("manager",), max_workers=1,
            )
            try:
                loop_manager.notifications.configure_session(connection, enabled=False)
                connection.commit()
                with mock.patch(
                    "loop_manager._run_project_result",
                    side_effect=loop_manager.SelfMergeRuntimeHandoff,
                ):
                    with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                        loop_manager.run_project_result(
                            connection, state_dir, repos[0], config.path,
                            None, False, registered_only=True,
                        )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM recovery_records WHERE repo = ?",
                        (repos[0],),
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_merge_observation_handoff_precedes_source_and_conflict_work(self):
        observation = loop_manager.WaitingPullRequestObservation(
            (), True, ("https://example.test/pr/1",),
        )
        after_source = mock.Mock()
        with mock.patch(
            "loop_manager.observe_waiting_pull_requests", return_value=observation,
        ), mock.patch("loop_manager.select_waiting_pull_request_conflict") as conflict:
            with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                loop_manager.synchronize_waiting_pull_requests(
                    mock.Mock(), Path("/repo"), Path("/state"), "/repo",
                    after_observation=mock.Mock(
                        side_effect=loop_manager.SelfMergeRuntimeHandoff,
                    ),
                    after_reconciliation=after_source,
                )
        after_source.assert_not_called()
        conflict.assert_not_called()

    def test_self_merge_handoff_uses_registered_base_after_skipped_or_failed_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, _config, repos = self._scheduler_state(
                directory, ("manager",), max_workers=1,
            )
            with connection:
                connection.execute(
                    "UPDATE projects SET base_branch = 'develop' WHERE repo = ?",
                    (repos[0],),
                )
            project = loop_manager.project_registry.list_projects(connection)[0]
            runtime = loop_manager.RuntimeMutation(
                Path(repos[0]), state_dir / "runtime", "a" * 40, "b" * 32,
            )
            observation = loop_manager.WaitingPullRequestObservation(
                (), True, ("https://example.test/pr/1",),
            )
            try:
                for status in ("skipped", "failed"):
                    with self.subTest(status=status), mock.patch(
                        "loop_manager._registered_self_runtime_project", return_value=True
                    ), mock.patch(
                        "loop_manager.observe_waiting_pull_requests",
                        return_value=observation,
                    ), mock.patch(
                        "loop_manager.ensure_safe_base_worktree",
                        return_value=loop_manager.BaseWorktreeSyncResult(
                            status, "local checkout preserved", "c" * 40,
                        ),
                    ) as synchronize, mock.patch(
                        "loop_manager.git",
                        side_effect=lambda _repo, command, *args, **_kwargs: subprocess.CompletedProcess(
                            [command, *args], 0, stdout=("c" * 40 + "\n")
                            if command == "rev-parse" else "",
                        ),
                    ):
                        with self.assertRaises(loop_manager.SelfMergeRuntimeHandoff):
                            loop_manager._preflight_self_merge_handoff(
                                connection, (project,), state_dir, runtime,
                            )
                        self.assertEqual(synchronize.call_args.args[1], "develop")
                        self.assertEqual(
                            synchronize.call_args.kwargs["remote_sha"], "c" * 40,
                        )
            finally:
                connection.close()

    def test_self_preflight_keeps_running_without_new_merge_and_rejects_uncertain_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, _config, repos = self._scheduler_state(
                directory, ("manager",), max_workers=1,
            )
            project = loop_manager.project_registry.list_projects(connection)[0]
            runtime = loop_manager.RuntimeMutation(
                Path(repos[0]), state_dir / "runtime", "a" * 40, "b" * 32,
            )
            try:
                with mock.patch(
                    "loop_manager._registered_self_runtime_project", return_value=True
                ), mock.patch(
                    "loop_manager.observe_waiting_pull_requests",
                    return_value=loop_manager.WaitingPullRequestObservation((), False),
                ), mock.patch("loop_manager.ensure_safe_base_worktree") as synchronize:
                    self.assertIsNone(loop_manager._preflight_self_merge_handoff(
                        connection, (project,), state_dir, runtime,
                    ))
                    synchronize.assert_not_called()
                with mock.patch(
                    "loop_manager._registered_self_runtime_project", return_value=True
                ), mock.patch(
                    "loop_manager.observe_waiting_pull_requests",
                    side_effect=RuntimeError("fetch failed"),
                ), mock.patch("loop_manager.ensure_safe_base_worktree") as synchronize:
                    with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                        loop_manager._preflight_self_merge_handoff(
                            connection, (project,), state_dir, runtime,
                        )
                    synchronize.assert_not_called()
            finally:
                connection.close()

    def test_self_merge_rejects_non_fast_forward_runtime_relation(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, _config, repos = self._scheduler_state(
                directory, ("manager",), max_workers=1,
            )
            project = loop_manager.project_registry.list_projects(connection)[0]
            runtime = loop_manager.RuntimeMutation(
                Path(repos[0]), state_dir / "runtime", "a" * 40, "b" * 32,
            )
            observation = loop_manager.WaitingPullRequestObservation(
                (), True, ("https://example.test/pr/1",),
            )
            def git_result(_repo, command, *arguments, **_kwargs):
                if command == "merge-base":
                    return subprocess.CompletedProcess(
                        [command, *arguments], 1, stdout="",
                    )
                return subprocess.CompletedProcess(
                    [command, *arguments], 0, stdout="c" * 40 + "\n",
                )
            try:
                with mock.patch(
                    "loop_manager._registered_self_runtime_project", return_value=True
                ), mock.patch(
                    "loop_manager.git", side_effect=git_result,
                ), mock.patch(
                    "loop_manager.ensure_safe_base_worktree",
                    return_value=loop_manager.BaseWorktreeSyncResult(
                        "skipped", "local checkout preserved", "c" * 40,
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "fast-forward"):
                        loop_manager._finish_self_merge_handoff(
                            connection, project, state_dir, runtime, observation,
                        )
            finally:
                connection.close()

    def test_other_project_merge_does_not_signal_runtime_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, _config, repos = self._scheduler_state(
                directory, ("manager", "peer"), max_workers=2,
            )
            runtime = loop_manager.RuntimeMutation(
                Path(repos[0]), state_dir / "runtime", "a" * 40, "b" * 32,
            )
            try:
                with mock.patch(
                    "loop_manager._registered_self_runtime_project",
                    return_value=False,
                ), mock.patch("loop_manager._finish_self_merge_handoff") as finish:
                    loop_manager._handoff_observed_self_merge(
                        connection, repos[1], state_dir, runtime,
                        loop_manager.WaitingPullRequestObservation(
                            (), True, ("https://example.test/pr/peer",),
                        ),
                    )
                    finish.assert_not_called()
            finally:
                connection.close()

    def _scheduler_state(
        self,
        directory: str,
        project_names: tuple[str, ...],
        *,
        max_workers: int,
    ) -> tuple[Path, sqlite3.Connection, loop_manager.ManagedConfig, tuple[str, ...]]:
        state_dir = Path(directory) / "state"
        state_dir.mkdir()
        connection = loop_manager.connect_database(state_dir, create=True)
        repos = tuple(
            str((Path(directory) / project_name).resolve())
            for project_name in project_names
        )
        connection.executemany(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            ((repo,) for repo in repos),
        )
        connection.commit()
        config = loop_manager.ManagedConfig(
            path=Path(directory) / "config.ini",
            max_workers=max_workers,
            telegram_token="",
            telegram_chat_id="",
        )
        return state_dir, connection, config, repos

    def test_worker_pool_bounds_parallel_projects_and_isolates_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, repos = self._scheduler_state(
                directory,
                ("repo-0", "repo-1", "repo-2", "repo-3"),
                max_workers=2,
            )
            guard = threading.Lock()
            both_workers_started = threading.Event()
            connections: list[sqlite3.Connection] = []
            thread_ids: set[int] = set()
            planner_mutex_ids: set[int] = set()
            external_run_ids: set[str] = set()
            active_workers = 0
            maximum_workers = 0
            active_planners = 0
            maximum_planners = 0

            def run_one(
                worker_connection,
                _state_dir,
                repo,
                _config_path,
                _requested_base,
                _allow_agents_mismatch,
                *,
                registered_only,
                planner_mutex,
                refactoring_review_interval,
                verify_timeout_seconds,
                on_step_completed,
                external_run_id,
            ):
                nonlocal active_workers, maximum_workers
                nonlocal active_planners, maximum_planners
                self.assertTrue(registered_only)
                self.assertTrue(callable(on_step_completed))
                self.assertEqual(
                    refactoring_review_interval,
                    config.refactoring_review_interval,
                )
                self.assertEqual(
                    verify_timeout_seconds,
                    config.verify_timeout_seconds,
                )
                self.assertTrue(external_run_id)
                with guard:
                    connections.append(worker_connection)
                    thread_ids.add(threading.get_ident())
                    planner_mutex_ids.add(id(planner_mutex))
                    external_run_ids.add(external_run_id)
                    active_workers += 1
                    maximum_workers = max(maximum_workers, active_workers)
                    if active_workers == config.max_workers:
                        both_workers_started.set()
                self.assertTrue(both_workers_started.wait(timeout=1))
                with planner_mutex:
                    with guard:
                        active_planners += 1
                        maximum_planners = max(
                            maximum_planners, active_planners
                        )
                    time.sleep(0.02)
                    with guard:
                        active_planners -= 1
                worker_connection.execute(
                    "UPDATE projects SET last_error = ? WHERE repo = ?",
                    (Path(repo).name, repo),
                )
                worker_connection.commit()
                with guard:
                    active_workers -= 1
                return loop_manager.ProjectRunResult(
                    loop_manager.ProjectRunKind.WAITING,
                    0,
                )

            try:
                with mock.patch(
                    "loop_manager.run_project_result",
                    side_effect=run_one,
                ):
                    outcomes = loop_manager.schedule_registered_projects(
                        connection,
                        state_dir,
                        config,
                    )
                stored = dict(
                    connection.execute(
                        "SELECT repo, last_error FROM projects ORDER BY repo"
                    ).fetchall()
                )
            finally:
                connection.close()

        self.assertEqual(tuple(item.repo for item in outcomes), repos)
        self.assertEqual(maximum_workers, config.max_workers)
        self.assertEqual(len(external_run_ids), 1)
        self.assertGreaterEqual(len(thread_ids), 2)
        self.assertEqual(maximum_planners, 1)
        self.assertEqual(len(planner_mutex_ids), 1)
        self.assertEqual(len({id(item) for item in connections}), len(repos))
        self.assertNotIn(id(connection), {id(item) for item in connections})
        self.assertEqual(
            stored,
            {repo: Path(repo).name for repo in repos},
        )

    def test_scheduler_forwards_runtime_mutation_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, repos = self._scheduler_state(
                directory, ("manager",), max_workers=1,
            )
            mutation = loop_manager.RuntimeMutation(
                Path(repos[0]), state_dir / "runtime", "a" * 40, "b" * 32,
            )
            try:
                with mock.patch(
                    "loop_manager.run_project_result",
                    return_value=loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.WAITING, 0,
                    ),
                ) as run_project:
                    outcomes = loop_manager.schedule_registered_projects(
                        connection, state_dir, config, runtime_mutation=mutation,
                    )
                self.assertEqual(tuple(item.repo for item in outcomes), repos)
                self.assertIs(run_project.call_args.kwargs["runtime_mutation"], mutation)
            finally:
                connection.close()

    def test_project_failures_are_classified_without_cancelling_peers(self):
        names = (
            "blocked",
            "quota",
            "transient",
            "configuration",
            "dirty",
            "healthy",
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, repos = self._scheduler_state(
                directory,
                names,
                max_workers=3,
            )
            calls: list[str] = []
            guard = threading.Lock()

            def run_one(
                _connection,
                _state_dir,
                repo,
                _config_path,
                _requested_base,
                _allow_agents_mismatch,
                **_kwargs,
            ):
                name = Path(repo).name
                with guard:
                    calls.append(name)
                if name == "dirty":
                    raise RuntimeError("unsafe base worktree")
                if name == "configuration":
                    raise loop_manager.ManagedConfigError("invalid managed config")
                results = {
                    "blocked": loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.BLOCKED, 1, "blocked"
                    ),
                    "quota": loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE, 0, "quota"
                    ),
                    "transient": loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.TRANSIENT_PAUSE, 0, "retry"
                    ),
                    "healthy": loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.COMPLETED, 0
                    ),
                }
                return results[name]

            try:
                with mock.patch(
                    "loop_manager.run_project_result",
                    side_effect=run_one,
                ), mock.patch("loop_manager.operation_log"):
                    outcomes = loop_manager.schedule_registered_projects(
                        connection,
                        state_dir,
                        config,
                    )
                by_name = {
                    Path(item.repo).name: item.result.kind for item in outcomes
                }
                with mock.patch(
                    "loop_manager.schedule_registered_projects",
                    return_value=outcomes,
                ):
                    exit_code = loop_manager.run_registered_projects(
                        connection,
                        state_dir,
                        config,
                    )
            finally:
                connection.close()

        self.assertEqual(set(calls), set(names))
        self.assertEqual(
            by_name,
            {
                "blocked": loop_manager.ProjectRunKind.BLOCKED,
                "quota": loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE,
                "transient": loop_manager.ProjectRunKind.TRANSIENT_PAUSE,
                "configuration": loop_manager.ProjectRunKind.CONFIGURATION_ERROR,
                "dirty": loop_manager.ProjectRunKind.OPERATIONAL_ERROR,
                "healthy": loop_manager.ProjectRunKind.COMPLETED,
            },
        )
        self.assertEqual(exit_code, 1)

    def test_zero_one_and_nonfatal_project_sets_exit_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir, connection, config, _repos = self._scheduler_state(
                directory,
                (),
                max_workers=3,
            )
            try:
                self.assertEqual(
                    loop_manager.schedule_registered_projects(
                        connection, state_dir, config
                    ),
                    (),
                )
                self.assertEqual(
                    loop_manager.run_registered_projects(
                        connection, state_dir, config
                    ),
                    0,
                )
                single_repo = str((Path(directory) / "single").resolve())
                connection.execute(
                    "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                    (single_repo,),
                )
                connection.commit()
                with mock.patch(
                    "loop_manager.run_project_result",
                    return_value=loop_manager.ProjectRunResult(
                        loop_manager.ProjectRunKind.WAITING, 0
                    ),
                ):
                    single = loop_manager.schedule_registered_projects(
                        connection, state_dir, config
                    )
                self.assertEqual(tuple(item.repo for item in single), (single_repo,))

                pauses = (
                    loop_manager.ScheduledProjectResult(
                        "quota",
                        loop_manager.ProjectRunResult(
                            loop_manager.ProjectRunKind.USAGE_LIMIT_PAUSE, 0
                        ),
                    ),
                    loop_manager.ScheduledProjectResult(
                        "waiting",
                        loop_manager.ProjectRunResult(
                            loop_manager.ProjectRunKind.WAITING, 0
                        ),
                    ),
                )
                with mock.patch(
                    "loop_manager.schedule_registered_projects",
                    return_value=pauses,
                ):
                    self.assertEqual(
                        loop_manager.run_registered_projects(
                            connection, state_dir, config
                        ),
                        0,
                    )
            finally:
                connection.close()

    def test_one_project_selects_only_the_ready_dependency_priority_winner(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(loop_manager.SCHEMA)
        loop_manager._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES ('repo', 'main')"
        )
        plans = (
            ("G-001", "normal", ()),
            ("G-002", "critical", ("G-099",)),
            ("G-003", "high", ()),
            ("G-004", "high", ()),
            ("G-099", "low", ()),
        )
        connection.executemany(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status
            ) VALUES ('repo', ?, ?, ?, '["docs/PRODUCT.md"]', ?, ?, 'hash', 'READY')
            """,
            (
                (
                    plan_id,
                    f"docs/plans/[{plan_id}] Plan.md",
                    plan_id,
                    json.dumps(depends_on),
                    priority,
                )
                for plan_id, priority, depends_on in plans
            ),
        )
        connection.commit()

        selected = loop_manager.select_ready_execplan(connection, "repo")
        self.assertEqual(selected["plan_id"], "G-003")
        connection.execute(
            "UPDATE exec_plans SET status = 'IMPLEMENTING' "
            "WHERE repo = 'repo' AND plan_id = 'G-003'"
        )
        connection.commit()
        self.assertIsNone(loop_manager.select_ready_execplan(connection, "repo"))
        with self.assertRaises(sqlite3.IntegrityError):
            with connection:
                connection.execute(
                    "UPDATE exec_plans SET status = 'IMPLEMENTING' "
                    "WHERE repo = 'repo' AND plan_id = 'G-004'"
                )
        connection.close()
