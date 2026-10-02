"""Physical invocation orchestration with explicit profile, process and audit ports."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import time

from loop_manager_internal.agents.agent_invocation import AgentRequest, AgentResult, InvocationContext, ResolvedProfile, Usage, _coerce_agent_result
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents.provider_adapters import AgentProvider
from loop_manager_internal.storage import agent_invocation_state
from loop_manager_internal.agents import agent_write_isolation


@dataclass(frozen=True)
class InvocationEffects:
    """Explicit effects supplied by the composition boundary."""

    _agent_profile: Callable[[Path, str, str, int | None, Path], ResolvedProfile]
    _check_runtime_handoff: Callable[[], None]
    start_invocation: Callable[..., str]
    capture_git_snapshot: Callable[[Path], GitSnapshot]
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
            collection_status = "pending"
        except sqlite3.Error:
            collection_status = "error"

    def observe_session(started_session_id: str) -> None:
        nonlocal collection_status
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
        profile=resolved,
        on_isolation_checked=observe_isolation,
        isolation_snapshot_digest=lambda: hashlib.sha256(
            effects.capture_git_snapshot(worktree).serialize().encode()).hexdigest(),
        expected_repository=invocation.repo if invocation is not None else None,
    )
    started = time.monotonic()
    try:
        raw = effects.agent_provider(provider).run(request)
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
    result = _coerce_agent_result(raw, session_id)
    if ledger is not None and identity is not None:
        try:
            ledger.finish(identity, result.ledger_kind, result.code,
                          max(0, int((time.monotonic() - started) * 1000)),
                          result.session_id, result.usage,
                          "error" if collection_status == "error" else None)
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
