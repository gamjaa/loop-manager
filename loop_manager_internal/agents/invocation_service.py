"""Physical invocation orchestration with explicit profile, process and audit ports."""
from __future__ import annotations
from loop_manager_internal.runtime import operation_events as events

from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import time
import shutil

from loop_manager_internal.agents.agent_invocation import AgentRequest, AgentResult, AgentResultKind, InvocationContext, ResolvedProfile, Usage, _coerce_agent_result
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.agents.provider_adapters import AgentProvider
from loop_manager_internal.storage import agent_invocation_state
from loop_manager_internal.agents import agent_write_isolation
from loop_manager_internal.agents import common_guidance
from loop_manager_internal.agents import agent_profiles
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
    adapter = effects.agent_provider(provider)
    prepared_launch = None
    prepare = getattr(type(adapter), 'prepare_environment', None)
    if invocation is not None and resolved is None and callable(prepare):
        try:
            prepared_launch = prepare(adapter, AgentRequest(
                worktree, state_dir, config_path, role_name, prompt, read_only,
                expected_repository=invocation.repo if invocation else str(worktree.resolve())))
        except (agent_write_isolation.IsolationError, OSError) as error:
            if owned_connection is not None:
                owned_connection.close()
            return AgentResult(1, f'Agent 실행 환경 준비 실패: {error}', session_id,
                               AgentResultKind.PERMANENT_CONFIGURATION)
    if invocation is not None and resolved is None:
        try:
            environment = (prepared_launch.environment if prepared_launch is not None
                           else agent_profiles.agent_environment(provider, role_name, state_dir=state_dir))
            with agent_profiles.prepared_environment(provider, environment):
                resolved = effects._agent_profile(config_path, provider, role_name, level, worktree)
        except (RuntimeError, OSError):
            # Profile observation must not alter the provider's own result.
            # A real adapter still resolves and validates before launching CLI.
            collection_status = "error"
    ledger = agent_invocation_state.InvocationRepository(connection) if connection is not None else None
    identity: str | None = None
    execution_started = False
    execution_started_at: float | None = None
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

    def observe_execution_identity(value: dict[str, str]) -> None:
        if ledger is None or identity is None:
            raise agent_write_isolation.IsolationError('Agent 실제 실행 입력을 저장할 invocation이 없습니다.')
        try:
            ledger.record_execution_identity(identity, value)
        except (sqlite3.Error, ValueError) as error:
            raise agent_write_isolation.IsolationError('Agent 실제 실행 입력 증거 저장 실패') from error

    from loop_manager_internal.notifications import notifications

    def observe_process() -> None:
        if connection is None or identity is None or invocation is None:
            return
        if role_name not in {"worker", "planner", "planner_check", "maintenance_planner"}:
            return
        try:
            # A planner_check is a read-only call, but its physical start may
            # consume a previously notified planning incident after release.
            observed_role = "planner" if role_name == "planner_check" else role_name
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

    def observe_cli_process_started() -> None:
        nonlocal execution_started, execution_started_at, collection_status
        execution_started = True
        execution_started_at = time.monotonic()
        events.emit("Agent 실제 실행 시작", invocation_id=identity,
                    provider=provider, model=resolved.model if resolved else None,
                    effort=resolved.effort if resolved else None,
                    purpose=invocation.purpose if invocation else None,
                    attempt=invocation.attempt if invocation else 1,
                    session_transition=invocation.transition if invocation else None)
        if ledger is not None and identity is not None:
            try:
                ledger.mark_execution_started(identity)
            except sqlite3.Error:
                collection_status = "error"
        if notify_process_start:
            observe_process()

    def observe_cli_process_finished() -> None:
        nonlocal collection_status
        if ledger is None or identity is None or execution_started_at is None:
            return
        try:
            ledger.mark_execution_finished(
                identity, max(0, int((time.monotonic() - execution_started_at) * 1000)),
            )
        except sqlite3.Error:
            collection_status = "error"

    if connection is not None and invocation is not None:
        notifications.flush_project_events(connection, invocation.repo)
    has_notification_session = (connection is not None and connection.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE name = 'notification_session'"
    ).fetchone() is not None)
    def preflight_scope_digest() -> str:
        # Bind policy evidence to the invocation identity, not mutable Git state.
        identity = (role_name, str(_canonical_path(worktree)),
                    invocation.repo if invocation is not None else '',
                    invocation.plan_id or '' if invocation is not None else '')
        return hashlib.sha256("\0".join(identity).encode()).hexdigest()

    notify_process_start = (role_name in {"worker", "planner", "planner_check", "maintenance_planner"}
                            and has_notification_session and invocation is not None)
    def observe_launch(launch):
        nonlocal prepared_launch
        prepared_launch = launch
        if ledger is not None and identity is not None:
            from .process_receipt import receipt_path
            ledger.bind_process_receipt(identity, str(receipt_path(launch)))

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
        on_process_started=(observe_cli_process_started if invocation is not None else None),
        on_process_finished=(observe_cli_process_finished if invocation is not None else None),
        profile=resolved,
        on_isolation_checked=observe_isolation,
        isolation_snapshot_digest=preflight_scope_digest,
        expected_repository=invocation.repo if invocation is not None else None,
        common_guidance=guidance,
        prepared_launch=prepared_launch,
        on_execution_identity=observe_execution_identity,
        on_launch_prepared=observe_launch,
    )
    started = time.monotonic()
    targeted_collection_error = None
    completed = None
    try:
        if session_id and session_task is None:
            # The existing retry runner rebuilds role/task context once. Keep
            # the old session and artifacts, without claiming it had these rules.
            raw = AgentResult(1, "cannot resume: 기존 session에 공통 지침 버전 기록이 없습니다. "
                              "최소 문맥으로 새 session에서 작업을 이어가야 합니다.", session_id)
        else:
            try:
                with events.log_context(invocation_id=identity):
                    raw = adapter.run(request)
            except common_guidance.GuidanceError as error:
                raw = AgentResult(1, f"공통 지침 provider 준비 실패: {error}", session_id,
                                  AgentResultKind.PERMANENT_CONFIGURATION)
            completed = _coerce_agent_result(raw, session_id)
            if completed.session_id:
                guidance_repository.bind_session(guidance_context.repo, provider,
                                                 completed.session_id, guidance.task_id)
    except BaseException as error:
        events.emit("Agent 호출 종료 관측", level="WARN", invocation_id=identity,
                    result=type(error).__name__, execution_status="executed" if execution_started else "not_executed",
                    elapsed_seconds=round(time.monotonic() - started, 1))
        if ledger is not None and identity is not None:
            kind = ("interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else
                    "timeout" if isinstance(error, subprocess.TimeoutExpired) else
                    "transient_failure" if isinstance(error, OSError) else "exception")
            try:
                ledger.finish(identity, kind, None,
                              max(0, int((time.monotonic() - started) * 1000)),
                              None, Usage(),
                              execution_status="executed" if execution_started else "not_executed")
            except sqlite3.Error:
                pass
        raise
    finally:
        if role_name == 'worker' and prepared_launch is not None and connection is not None and invocation is not None and invocation.plan_id:
            from loop_manager_internal.verification.targeted import collect
            from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
            try:
                TargetedTestRepository(connection).observe(invocation.repo, invocation.plan_id,
                                                          collect(prepared_launch.attempt))
            except (OSError, ValueError, sqlite3.Error) as error:
                targeted_collection_error = f'targeted test 실행 근거 수집 실패: {type(error).__name__}'
                events.emit(targeted_collection_error, level='WARN', invocation_id=identity)
        if execution_started and identity is not None:
            try:
                from loop_manager_internal.agents.provider_adapters import _provider_log_path
                destination = _provider_log_path(worktree, state_dir, provider, role_name, identity)
                if prepared_launch is not None:
                    source = prepared_launch.attempt / "provider.log"
                    if source.is_file():
                        shutil.copyfile(source, destination)
                if destination.is_file():
                    events.emit("Agent 호출별 상세 기록 보존", invocation_id=identity, log_path=destination)
                    latest = destination.parent / f"{'claude-' if provider == 'claude' else ''}{role_name}.log"
                    if completed is None or completed.code != 125:
                        shutil.copyfile(destination, latest)
            except OSError as error:
                collection_status = "error"
                events.emit("Agent 상세 기록 보존 실패", level="WARN", invocation_id=identity,
                            error_type=type(error).__name__)
        if owned_connection is not None:
            owned_connection.close()
    result = _coerce_agent_result(raw, session_id)
    if targeted_collection_error:
        collection_status = 'error'
    if result.collection_status == "error":
        collection_status = "error"
    if ledger is not None and identity is not None:
        try:
            ledger.finish(identity, result.ledger_kind, result.code,
                          max(0, int((time.monotonic() - started) * 1000)),
                          result.session_id, result.usage,
                          "error" if collection_status == "error" else None,
                          replace_session=True,
                          execution_status="executed" if execution_started else "not_executed")
            if collection_status != "error":
                collection_status = result.usage.status
        except sqlite3.Error:
            collection_status = "error"
    events.emit("Agent 호출 결과 관측", level="WARN" if result.code or collection_status == "error" else "INFO",
                invocation_id=identity, result=result.ledger_kind, result_code=result.code,
                execution_status="executed" if execution_started else "not_executed",
                elapsed_seconds=round(time.monotonic() - started, 1),
                usage_status=result.usage.status, collection_status=collection_status,
                input_tokens=result.usage.input_tokens, cached_input_tokens=result.usage.cached_input_tokens,
                cache_write_input_tokens=result.usage.cache_write_input_tokens,
                output_tokens=result.usage.output_tokens, reasoning_output_tokens=result.usage.reasoning_output_tokens)
    if targeted_collection_error:
        return replace(result, code=1, message=targeted_collection_error,
                       kind=AgentResultKind.PERMANENT_CONFIGURATION,
                       collection_status='error', isolation_invocation_id=identity)
    if isinstance(raw, AgentResult):
        return replace(raw, collection_status=collection_status,
                       isolation_invocation_id=identity)
    if identity is not None:
        return replace(result, collection_status=collection_status,
                       isolation_invocation_id=identity)
    return raw
