import json
import contextlib
import sqlite3
from pathlib import Path
from unittest import mock
import loop_manager
from loop_manager_internal.storage import planning_input_state
from loop_manager_internal.execution.worker_session import SessionBinding
from tests.support.catalog import write_execplan
from tests.support.git import git_command, make_remote_repository, commit_and_push


def worker_result(
    title: str = "Preserve worker test evidence",
    summary: str = "Stores the Worker result for review.",
    tests: str = "targeted test: PASS",
) -> str:
    return (
        'WORKER_OUTCOME: {"kind":"ready_for_validation"}\n'
        f"COMMIT_TITLE: {title}\nPR_SUMMARY: {summary}\n"
        f"TARGETED_TESTS:\n{tests}"
    )

def isolated_scope_resources(_connection, _repo, worktree, before, role_name, **_kwargs):
    """These Agent fakes model a proven isolated invocation, tested elsewhere."""
    return loop_manager.agent_scope_decision.used_git_resources(
        role=role_name, phase='PLAN_BLOCKED', base_branch='main',
        branch=before.branch, worktree_key=str(worktree.resolve()))


@contextlib.contextmanager
def dummy_worker_context(*, roles=("worker",)):
    """Opt legacy unused.ini roles into fake profile/session preflight."""
    real_profile = loop_manager._agent_profile
    real_ports = loop_manager._ExecutionLifecyclePorts

    def profile(config_path, provider, role_name, level, worktree):
        if (role_name in roles and Path(config_path).name == 'unused.ini'
                and not Path(config_path).exists()):
            return loop_manager.AgentProfile('fixture-worker-model', 'medium')
        return real_profile(config_path, provider, role_name, level, worktree)

    def ports(connection, repository, **identity):
        if Path(identity['config_path']).name == 'unused.ini':
            saved = repository.projected(identity['repo'])
            if saved and saved.worker_session_id and saved.worker_session_binding is None:
                repository.update_project(identity['repo'], {
                    'worker_session_binding': SessionBinding(
                        identity['provider'], 'fixture-worker-model', 'medium',
                    ).serialize(),
                }, enqueue=False)
        return real_ports(connection, repository, **identity)

    with mock.patch.object(loop_manager, '_agent_profile', side_effect=profile), \
         mock.patch.object(loop_manager, '_ExecutionLifecyclePorts', side_effect=ports):
        yield


def dummy_worker_sessions(test):
    """Limit compatibility patches to the explicitly decorated test method."""
    from functools import wraps

    @wraps(test)
    def run(*args, **kwargs):
        with dummy_worker_context():
            return test(*args, **kwargs)
    return run


@contextlib.contextmanager
def legacy_planning_ports():
    """Exercise preserved planning flows without the newer tiered review port."""
    real_ports = loop_manager._PlanningLifecyclePorts

    def ports_without_tiered(*args, **kwargs):
        ports = real_ports(*args, **kwargs)
        del ports.tiered
        return ports

    with mock.patch.object(
        loop_manager, '_PlanningLifecyclePorts', side_effect=ports_without_tiered
    ):
        yield


def decision(**changes):
    value = {
        "kind": "needs_user_decision",
        "problem": "설계 승인이 필요함",
        "proposal": "실행 경계 변경",
        "locations": [{"path": "docs/ARCHITECTURE.md",
                       "section": "Architecture Change Policy"}],
        "question": "이 변경을 승인합니까?",
    }
    value.update(changes)
    return "WORKER_OUTCOME: " + json.dumps(value, ensure_ascii=False)


class ActiveWorkerFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _make_active_worker_project(self, directory: str):
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
        planning_input_state.record_general(
            connection, str(repo), plan_sha,
            loop_manager.plan_declarations(repo, plan_sha),
        )
        connection.commit()
        return repo, state_dir, connection
