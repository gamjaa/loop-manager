"""planning agent ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents.agent_invocation import AgentResultKind
from loop_manager_internal.agents.agent_invocation import AttemptSequence
from loop_manager_internal.agents.agent_invocation import TrackedAgentCall
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents.scope_recovery import ignored_comparison_view
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.application.contracts import TRANSIENT_AGENT_ERROR_PREFIX
from loop_manager_internal.application.contracts import USAGE_LIMIT_ERROR_PREFIX
from loop_manager_internal.notifications import notifications
from loop_manager_internal.planning.planning_contracts import PlanningAgentPort
from loop_manager_internal.planning.planning_contracts import PlanningAgentResult
from loop_manager_internal.planning.planning_contracts import PlanningAgentResultKind
from loop_manager_internal.planning.planning_contracts import PlanningContext
from loop_manager_internal.planning.planning_contracts import PlanningScopeResult
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from pathlib import Path
import contextlib
import sqlite3
import threading
import time


@contextlib.contextmanager
def planner_progress(events, label: str, *, interval: float = 30):
    """Log elapsed time during a long call without touching SQLite on the thread."""
    stopped = threading.Event()
    started = time.monotonic()
    def report():
        while not stopped.wait(interval):
            events.log(f"{label}: 모델 호출 진행 중, {time.monotonic() - started:.0f}초 경과")
    reporter = threading.Thread(target=report, daemon=True)
    reporter.start()
    try:
        yield
    finally:
        stopped.set()
        reporter.join(timeout=1)


def run_scoped_planner_turn(
    agent: PlanningAgentPort,
    repo: str,
    role: str,
    worktree: Path,
    prompt: str,
    session_id: str | None,
    save_session: Callable[[str], None],
    *, read_only: bool = False,
) -> tuple[PlanningAgentResult, PlanningScopeResult]:
    """Run one turn, preserving session writes and failed scope evidence.

    Callback exceptions propagate; consumers retain their own error priorities
    and result validation. Read-only check scope stays available until its
    caller validates the result; other roles retain their existing cleanup.
    """
    before = agent.begin_scope(role, worktree)
    profile = {"profile_role": role} if role in {"maintenance_planner", "planner_check"} else {}
    if role == "planner_check":
        read_only = True
    if read_only:
        profile["read_only"] = True
    result = agent.run_planner(worktree, prompt, session_id, save_session, **profile)
    if result.session_id:
        save_session(result.session_id)
    scope = agent.finish_scope(role, worktree, before)
    if not scope.error and role != "planner_check":
        agent.clear_scope(repo)
    return result, scope


@dataclass(frozen=True)
class PlanningAgentPortsEffects:
    """Named effects for _PlanningAgentPorts; no entry-point/module access."""
    _agent_git_resources: Callable[..., object]
    _agent_profile: Callable[..., object]
    _reconciled_manager_scope_metadata: Callable[..., object]
    begin_agent_scope: Callable[..., object]
    _with_proven_legacy_ignore_config: Callable[..., object]
    _normalize_legacy_ignored_snapshot: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    clear_agent_scope: Callable[..., object]
    operation_log: Callable[..., object]
    run_agent: Callable[..., object]
    run_agent_with_retries: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


class _PlanningAgentPorts:
    """Bind Planner CLI and Git scope validation to the coordinator."""

    def __init__(
        self,
        effects: PlanningAgentPortsEffects, connection: sqlite3.Connection,
        context: PlanningContext,
        planner_mutex: threading.Lock | None,
    ):
        self._effects = effects
        self._connection = connection
        self._context = context
        self._planner_mutex = planner_mutex

    def begin_scope(self, role: str, worktree: Path) -> GitSnapshot:
        return self._effects.begin_agent_scope(
            self._connection, self._context.repo, worktree, role,
            state_dir=self._context.state_dir,
        )

    def run_planner(
        self,
        worktree: Path,
        prompt: str,
        session_id: str | None,
        on_session_started: Callable[[str], None],
        *, profile_role: str | None = None, read_only: bool = False,
    ) -> PlanningAgentResult:
        from loop_manager_internal.planning.manual_planning_context import protect
        prompt=protect(self._connection,self._context.repo,prompt)
        context = self._context
        role_name = "planner_check" if profile_role == "planner_check" else "planner"
        if role_name == "planner_check":
            read_only = True
        if session_id is not None:
            try:
                prior = self._connection.execute(
                    "SELECT role FROM agent_invocations WHERE repo = ? AND session_id = ? "
                    "ORDER BY rowid DESC LIMIT 1", (context.repo, session_id),
                ).fetchone()
            except sqlite3.OperationalError as error:
                if role_name == "planner_check" or "no such table: agent_invocations" not in str(error):
                    raise RuntimeError("점검 Planner session 기록을 확인할 수 없습니다.")
                prior = None  # Legacy Planner fixtures may have no invocation ledger.
            if (role_name == "planner_check" and (prior is None or prior[0] != role_name)
                    or role_name == "planner" and prior is not None and prior[0] == "planner_check"):
                raise RuntimeError("점검 Planner session 역할이 일치하지 않습니다.")
        guard = self._planner_mutex or contextlib.nullcontext()
        with guard:
            profile_arguments = {}
            if profile_role is not None:
                profile = self._effects._agent_profile(
                    context.config_path, context.provider, profile_role, None, worktree,
                )
                profile_arguments["resolved_profile"] = profile
                self._effects.operation_log(
                    f"{profile_role} 설정: {context.provider}, {profile.model}, {profile.effort}")
            result = self._effects.run_agent_with_retries(
                TrackedAgentCall(lambda current_session_id, _session_recovery, invocation: self._effects.run_agent(
                    context.provider,
                    worktree,
                    context.state_dir,
                    context.config_path,
                    role_name,
                    prompt,
                    read_only=read_only,
                    persist_session=True,
                    session_id=current_session_id,
                    on_session_started=on_session_started,
                    connection=self._connection,
                    invocation=invocation,
                    **profile_arguments,
                ), AttemptSequence(context.repo, None)),
                session_id=session_id,
            )
        kind = {
            AgentResultKind.SUCCESS: PlanningAgentResultKind.SUCCESS,
            AgentResultKind.TRANSIENT_FAILURE: (
                PlanningAgentResultKind.TRANSIENT_FAILURE
            ),
            AgentResultKind.USAGE_LIMIT: PlanningAgentResultKind.USAGE_LIMIT,
            AgentResultKind.PERMANENT_CONFIGURATION: (
                PlanningAgentResultKind.PERMANENT_CONFIGURATION
            ),
        }[result.result_kind]
        message = result.message
        if result.code and kind is PlanningAgentResultKind.TRANSIENT_FAILURE:
            message = f"{TRANSIENT_AGENT_ERROR_PREFIX} {message}"
        elif result.code and kind is PlanningAgentResultKind.USAGE_LIMIT:
            message = f"{USAGE_LIMIT_ERROR_PREFIX} {message}"
        return PlanningAgentResult(
            result.code,
            message,
            result.session_id,
            kind,
        )

    def finish_scope(
        self, role: str, worktree: Path, before: GitSnapshot
    ) -> PlanningScopeResult:
        after: GitSnapshot | None = None
        try:
            after = self._effects.capture_git_snapshot(worktree)
            self._effects.validate_agent_scope(
                role,
                worktree,
                before,
                after,
                external_worktree=self._context.repo_path,
                base_branch=self._context.base_branch,
                connection=self._connection,
            )
            self._effects.validate_managed_worktree(
                self._context.repo_path, worktree, None
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            return PlanningScopeResult(
                str(error), after.serialize() if after is not None else None
            )
        return PlanningScopeResult()

    def validate_saved_scope(
        self, role: str, worktree: Path, snapshot: str
    ) -> PlanningScopeResult:
        after: GitSnapshot | None = None
        try:
            before = GitSnapshot.deserialize(snapshot)
            after = self._effects.capture_git_snapshot(worktree)
            before = ignored_comparison_view(worktree, before, before, after, effects=self._effects)
            before = self._effects._reconciled_manager_scope_metadata(
                self._connection, self._context.repo_path, self._context.state_dir,
                self._context.repo, None, before, after,
                self._effects._agent_git_resources(self._connection, self._context.repo_path,
                                     worktree, before, role))
            self._effects.validate_agent_scope(
                role,
                worktree,
                before,
                after,
                external_worktree=self._context.repo_path,
                base_branch=self._context.base_branch,
                connection=self._connection,
            )
            self._effects.validate_managed_worktree(
                self._context.repo_path, worktree, None
            )
        except (AgentScopeError, WorkspaceOwnershipError, RuntimeError) as error:
            return PlanningScopeResult(
                str(error), after.serialize() if after is not None else None
            )
        return PlanningScopeResult()

    def recover_ignored_scope_error(self, expected: runtime_state.PlanningSnapshot) -> bool:
        """Release only a proven legacy ignored false positive, atomically."""
        context = self._context
        repository = runtime_state.RuntimeStateRepository(self._connection)
        row = self._connection.execute(
            "SELECT * FROM projects WHERE repo = ?", (context.repo,),
        ).fetchone()
        if repository.planning(context.repo) != expected:
            return False
        values = dict(row)
        if expected.planning_preserved_state:
            values.update(runtime_state._preserved_planning_values(expected.planning_preserved_state))
        if (values['phase'] != 'PLANNING' or values['planning_workspace_owned'] != 1
                or values['agent_role'] not in {'planner', 'maintenance_planner'}
                or values['agent_recovery'] or not values['agent_snapshot']
                or not values['agent_result_snapshot']
                or values['planning_error'] != values['planning_validation_error']
                or not values['planner_session_id']
                or values['planning_result'] is None):
            return False
        worktree = Path(str(values['worktree'] or ''))
        try:
            start = GitSnapshot.deserialize(values['agent_snapshot'])
            result = GitSnapshot.deserialize(values['agent_result_snapshot'])
            current = self._effects.capture_git_snapshot(worktree)
            before = ignored_comparison_view(worktree, start, start, current, effects=self._effects)
            after = ignored_comparison_view(worktree, start, result, current, effects=self._effects)
            if before.dirty == start.dirty and after.dirty == result.dirty:
                return False
            resources = self._effects._agent_git_resources(
                self._connection, context.repo_path, worktree, start, values['agent_role'])
            def validate(older, newer, role=None):
                older = self._effects._reconciled_manager_scope_metadata(
                    self._connection, context.repo_path, context.state_dir,
                    context.repo, None, older, current, resources)
                self._effects.validate_agent_scope(role or values['agent_role'], worktree, older, newer,
                    external_worktree=context.repo_path, base_branch=context.base_branch,
                    connection=self._connection, resources=resources)
            # The same validator must reject the original evidence for exactly
            # the stored cause. A coincidental ignored delta cannot free an error.
            try:
                validate(start, result)
            except AgentScopeError as error:
                if str(error) != values['planning_error']:
                    return False
            else:
                return False
            validate(before, after)
            validate(after, current, role='reviewer')
            self._effects.validate_managed_worktree(context.repo_path, worktree, None)
        except (RuntimeError, ValueError, OSError):
            return False
        with runtime_state._transaction(self._connection):
            if (repository.planning(context.repo) != expected
                    or scoped_blocking.project_block(self._connection, context.repo) is not None
                    or self._effects.capture_git_snapshot(worktree) != current):
                return False
            if expected.planning_preserved_state:
                repository.resume_planning(expected)
            repository._update_project_row(context.repo, {
                'planning_error': None, 'planning_error_fingerprint': None,
                'planning_error_origin': None, 'planning_validation_error': None,
                'planning_result_completed': 1, 'planning_result_pending_validation': 0,
                'last_error': None,
            })
        return True

    def clear_scope(self, repo: str) -> None:
        self._effects.clear_agent_scope(self._connection, repo)
