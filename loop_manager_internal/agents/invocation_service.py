"""Physical invocation orchestration with explicit profile, process and audit ports."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import time

from loop_manager_internal.agents.agent_invocation import AgentRequest, AgentResult, AgentResultKind, InvocationContext, ResolvedProfile, Usage, _coerce_agent_result
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents.provider_adapters import AgentProvider
from loop_manager_internal.storage import agent_invocation_state
from loop_manager_internal.agents import agent_write_isolation
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.agents import common_guidance
from loop_manager_internal.git.base_fetch_service import reconcile_external_base
from loop_manager_internal.storage.common_guidance_state import GuidanceRepository
from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
from loop_manager_internal.runtime.runtime_identity import _canonical_path


@dataclass(frozen=True)
class InvocationEffects:
    """Explicit effects supplied by the composition boundary."""

    _agent_profile: Callable[[Path, str, str, int | None, Path], ResolvedProfile]
    _check_runtime_handoff: Callable[[], None]
    start_invocation: Callable[..., str]
    capture_git_snapshot: Callable[[Path], GitSnapshot]
    git: Callable[..., object]
    agent_provider: Callable[[str], AgentProvider]


def run_agent(
    provider: str,
    worktree: Path,
    state_dir: Path,
    config_path: Path,
    role_name: str,
    prompt: str,
    *,
    read_only: bool,
    level: int | None = None,
    persist_session: bool = False,
    session_id: str | None = None,
    on_session_started: Callable[[str], None] | None = None,
    connection: sqlite3.Connection | None = None,
    invocation: InvocationContext | None = None,
    resolved_profile: ResolvedProfile | None = None,
    effects: InvocationEffects,
) -> tuple[int, str] | AgentResult:
    """Execute one provider-neutral Agent request."""

    effects._check_runtime_handoff()
    # Managed calls already supply this connection. Preserve the public one-shot
    # port by using the same state DB, never a provider-local instruction cache.
    owned_connection = None
    guidance_context = invocation or InvocationContext(str(worktree.resolve()), None)
    try:
        common_guidance.validate_state_root(guidance_context, worktree, state_dir)
        if connection is None:
            state_dir.mkdir(parents=True, exist_ok=True)
            owned_connection = sqlite3.connect(state_dir / "loop-manager.db")
        guidance_repository = GuidanceRepository(connection or owned_connection)
        guidance_context = common_guidance.independent_context(
            guidance_repository.connection, guidance_context, worktree,
        )
        guidance = common_guidance.resolve(
            guidance_repository, guidance_context, role_name, worktree, state_dir,
        )
        session_task = (guidance_repository.session_task(guidance_context.repo, provider, session_id)
                        if session_id else None)
        if session_task is not None and session_task != guidance.task_id:
            raise common_guidance.GuidanceError("Agent session과 공통 지침 작업 identity가 다릅니다.")
    except (common_guidance.GuidanceError, sqlite3.Error, OSError) as error:
        if owned_connection is not None:
            owned_connection.close()
        return AgentResult(1, f"공통 지침 호출 준비 실패: {error}", session_id,
                           AgentResultKind.PERMANENT_CONFIGURATION)
    resolved: ResolvedProfile | None = resolved_profile
    collection_status = "unknown"
    if invocation is not None and resolved is None:
        try:
            resolved = effects._agent_profile(config_path, provider, role_name, level, worktree)
        except (RuntimeError, OSError):
            # Profile observation must not alter the provider's own result.
            # A real adapter still resolves and validates before launching CLI.
            collection_status = "error"
    ledger = agent_invocation_state.InvocationRepository(connection) if connection is not None else None
    identity: str | None = None
    scope_id = None
    if connection is not None and invocation is not None:
        try:
            scope_id = ledger.invocation_scope_id(invocation.repo, role_name, str(worktree))
        except (sqlite3.Error, KeyError, IndexError, TypeError, ValueError, RuntimeError):
            pass  # Legacy/minimal records cannot grant reduced monitoring.
    if ledger is not None and invocation is not None and resolved is not None:
        try:
            identity = effects.start_invocation(ledger, invocation, role_name, provider,
                                        resolved, session_id, scope_id=scope_id)
            ledger.bind_guidance(identity, guidance.task_id, guidance.content_hash)
            collection_status = "pending"
        except sqlite3.Error:
            collection_status = "error"

    def observe_session(started_session_id: str) -> None:
        nonlocal collection_status
        guidance_repository.bind_session(guidance_context.repo, provider, started_session_id, guidance.task_id)
        if ledger is not None and identity is not None:
            try:
                ledger.observe_session(identity, started_session_id)
            except sqlite3.Error:
                collection_status = "error"
        if on_session_started is not None:
            on_session_started(started_session_id)

    def observe_isolation(evidence: agent_write_isolation.IsolationEvidence) -> None:
        if ledger is None or identity is None:
            raise agent_write_isolation.IsolationError(
                "Agent 격리 증거를 저장할 invocation 상태가 없습니다."
            )
        try:
            ledger.record_isolation(identity, evidence)
        except sqlite3.Error as error:
            raise agent_write_isolation.IsolationError(
                "Agent 격리 증거 저장 실패"
            ) from error

    from loop_manager_internal.notifications import notifications

    def observe_process() -> None:
        if connection is None or identity is None or invocation is None:
            return
        if role_name not in {"worker", "planner", "maintenance_planner"}:
            return
        try:
            observed_role = role_name
            if role_name == "planner" and scope_id is not None:
                from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
                owner = RuntimeStateRepository(connection).scope_owner(invocation.repo)
                if owner is not None and owner["agent_role"] == "maintenance_planner":
                    observed_role = "maintenance_planner"
            with connection:
                notifications.record_process_start(
                    connection, invocation.repo, observed_role, invocation.notification_id or identity,
                    plan_id=invocation.plan_id, resumed=bool(session_id),
                )
            notifications.flush_project_events(connection, invocation.repo)
        except sqlite3.Error:
            pass  # A failed event write must not change the physical call.

    if connection is not None and invocation is not None:
        notifications.flush_project_events(connection, invocation.repo)
    has_notification_session = (connection is not None and connection.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE name = 'notification_session'"
    ).fetchone() is not None)
    preflight_baseline: GitSnapshot | None = None
    preflight_digest: str | None = None

    def preflight_scope_digest() -> str:
        nonlocal preflight_baseline, preflight_digest
        snapshot = effects.capture_git_snapshot(worktree)
        if connection is None or invocation is None:
            return hashlib.sha256(snapshot.serialize().encode()).hexdigest()
        repository = RuntimeStateRepository(connection)
        try:
            project = repository.scope_owner(invocation.repo)
            owner = repository.scope_owner(invocation.repo, invocation.plan_id) if invocation.plan_id else project
        except sqlite3.OperationalError as error:
            if "no such table:" not in str(error).lower():
                raise
            return hashlib.sha256(snapshot.serialize().encode()).hexdigest()
        if project is None or owner is None or not project['base_branch']:
            return hashlib.sha256(snapshot.serialize().encode()).hexdigest()
        phase = {
            'planner': 'PLANNING', 'planner_check': 'PLANNING',
            'maintenance_planner': 'PLANNING', 'worker': 'IMPLEMENTING',
            'reviewer': 'REVIEWING',
        }.get(role_name)
        if phase is None:
            return hashlib.sha256(snapshot.serialize().encode()).hexdigest()
        branch = (str(owner['branch'] or '') or None
                  if role_name in {'worker', 'reviewer'} else None)
        if role_name in {'worker', 'reviewer'} and branch is None:
            return hashlib.sha256(snapshot.serialize().encode()).hexdigest()
        base_branch = str(project['base_branch'])
        resources = agent_scope_decision.used_git_resources(
            role=role_name, phase=phase, base_branch=base_branch, branch=branch,
            worktree_key=str(_canonical_path(worktree)),
        )
        if resources is None:
            return hashlib.sha256(snapshot.serialize().encode()).hexdigest()
        default_worktree = str(_canonical_path(Path(invocation.repo)))
        def selected_digest(value: GitSnapshot) -> str:
            return agent_scope_decision.invocation_scope_digest(
                value, role=role_name, phase=phase, base_branch=base_branch,
                branch=branch, worktree_key=str(_canonical_path(worktree)),
                default_worktree=default_worktree,
            )
        if preflight_baseline is None:
            preflight_baseline = snapshot
            preflight_digest = selected_digest(snapshot)
            return preflight_digest
        reconciled = reconcile_external_base(
            effects.git, worktree, base_branch, preflight_baseline, snapshot, resources,
            registered_origin=str(project['origin'] or ''),
        )
        baseline_view, current_view = agent_scope_decision.scoped_metadata(
            reconciled, snapshot, resources, default_worktree=default_worktree,
            base_branch=base_branch,
        )
        if baseline_view == current_view and preflight_digest is not None:
            if selected_digest(preflight_baseline) == preflight_digest:
                return preflight_digest
        return selected_digest(snapshot)

    request = AgentRequest(
        worktree=worktree,
        state_dir=state_dir,
        config_path=config_path,
        role_name=role_name,
        prompt=prompt,
        read_only=read_only,
        level=level,
        persist_session=persist_session,
        session_id=session_id,
        on_session_started=observe_session,
        on_process_started=(observe_process if role_name in {"worker", "planner", "maintenance_planner"}
                            and has_notification_session and invocation is not None else None),
        profile=resolved,
        on_isolation_checked=observe_isolation,
        isolation_snapshot_digest=preflight_scope_digest,
        expected_repository=invocation.repo if invocation is not None else None,
        common_guidance=guidance,
    )
    started = time.monotonic()
    try:
        if session_id and session_task is None:
            # The existing retry runner rebuilds role/task context once. Keep
            # the old session and artifacts, without claiming it had these rules.
            raw = AgentResult(1, "cannot resume: 기존 session에 공통 지침 버전 기록이 없습니다. "
                              "최소 문맥으로 새 session에서 작업을 이어가야 합니다.", session_id)
        else:
            try:
                raw = effects.agent_provider(provider).run(request)
            except common_guidance.GuidanceError as error:
                raw = AgentResult(1, f"공통 지침 provider 준비 실패: {error}", session_id,
                                  AgentResultKind.PERMANENT_CONFIGURATION)
            completed = _coerce_agent_result(raw, session_id)
            if completed.session_id:
                guidance_repository.bind_session(guidance_context.repo, provider,
                                                 completed.session_id, guidance.task_id)
    except BaseException as error:
        if ledger is not None and identity is not None:
            kind = ("interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else
                    "timeout" if isinstance(error, subprocess.TimeoutExpired) else
                    "transient_failure" if isinstance(error, OSError) else "exception")
            try:
                ledger.finish(identity, kind, None,
                              max(0, int((time.monotonic() - started) * 1000)),
                              None, Usage())
            except sqlite3.Error:
                pass
        raise
    finally:
        if owned_connection is not None:
            owned_connection.close()
    result = _coerce_agent_result(raw, session_id)
    if result.collection_status == "error":
        collection_status = "error"
    if ledger is not None and identity is not None:
        try:
            ledger.finish(identity, result.ledger_kind, result.code,
                          max(0, int((time.monotonic() - started) * 1000)),
                          result.session_id, result.usage,
                          "error" if collection_status == "error" else None,
                          replace_session=True)
            if collection_status != "error":
                collection_status = result.usage.status
        except sqlite3.Error:
            collection_status = "error"
    if isinstance(raw, AgentResult):
        return replace(raw, collection_status=collection_status,
                       isolation_invocation_id=identity)
    if identity is not None:
        return replace(result, collection_status=collection_status,
                       isolation_invocation_id=identity)
    return raw
