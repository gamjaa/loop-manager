"""planning agent ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents.agent_invocation import AgentResultKind
from loop_manager_internal.agents.agent_invocation import AttemptSequence
from loop_manager_internal.agents.agent_invocation import TrackedAgentCall
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents.scope_contracts import GitSnapshot
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


def run_scoped_planner_turn(
    agent: PlanningAgentPort,
    repo: str,
    role: str,
    worktree: Path,
    prompt: str,
    session_id: str | None,
    save_session: Callable[[str], None],
) -> tuple[PlanningAgentResult, PlanningScopeResult]:
    """Run one turn, preserving session writes and failed scope evidence.

    Callback exceptions propagate; consumers retain their own error priorities
    and result validation. Scope cleanup follows only a successful scope check.
    """
    before = agent.begin_scope(role, worktree)
    result = agent.run_planner(worktree, prompt, session_id, save_session)
    if result.session_id:
        save_session(result.session_id)
    scope = agent.finish_scope(role, worktree, before)
    if not scope.error:
        agent.clear_scope(repo)
    return result, scope


@dataclass(frozen=True)
class PlanningAgentPortsEffects:
    """Named effects for _PlanningAgentPorts; no entry-point/module access."""
    _agent_git_resources: Callable[..., object]
    _reconciled_manager_scope_metadata: Callable[..., object]
    begin_agent_scope: Callable[..., object]
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
    ) -> PlanningAgentResult:
        context = self._context
        guard = self._planner_mutex or contextlib.nullcontext()
        with guard:
            with self._connection:
                resumed = notifications.record_work_start(
                    self._connection, context.repo, "Planner",
                )
            if resumed:
                self._effects.operation_log(f"차단 해제 후 Planner 작업 재개: {context.repo}")
            result = self._effects.run_agent_with_retries(
                TrackedAgentCall(lambda current_session_id, _session_recovery, invocation: self._effects.run_agent(
                    context.provider,
                    worktree,
                    context.state_dir,
                    context.config_path,
                    "planner",
                    prompt,
                    read_only=False,
                    persist_session=True,
                    session_id=current_session_id,
                    on_session_started=on_session_started,
                    connection=self._connection,
                    invocation=invocation,
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

    def clear_scope(self, repo: str) -> None:
        self._effects.clear_agent_scope(self._connection, repo)
