"""Physical invocation orchestration with explicit profile, process and audit ports."""
from __future__ import annotations
from loop_manager_internal.runtime import operation_events as events

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
from loop_manager_internal.storage.db_access import CoordinatedConnection, DatabaseWaitTimeout, open_connection
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
from loop_manager_internal.application.task_scheduling import scheduled_call, current_scheduler, current_task


@dataclass(frozen=True)
class InvocationEffects:
    """Explicit effects supplied by the composition boundary."""

    _agent_profile: Callable[[Path, str, str, int | None, Path], ResolvedProfile]
    _check_runtime_handoff: Callable[[], None]
    start_invocation: Callable[..., str]
    capture_git_snapshot: Callable[[Path], GitSnapshot]
    git: Callable[..., object]
    agent_provider: Callable[[str], AgentProvider]


def _database_request(connection, operation):
    """Replay only DB preparation, never a provider or caller callback."""
    if isinstance(connection, CoordinatedConnection):
        return connection.run_request(lambda current: operation())
    # Repository methods retain transaction ownership on the public sqlite3 port.
    return operation()


@scheduled_call("agent")
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
            owned_connection = open_connection(state_dir / "loop-manager.db", timeout=5)
        guidance_connection = connection if connection is not None else owned_connection
        guidance_repository = _database_request(
            guidance_connection, lambda: GuidanceRepository(guidance_connection))
        guidance_context = _database_request(guidance_connection, lambda: common_guidance.independent_context(
            guidance_connection, guidance_context, worktree))
        guidance = common_guidance.resolve(
            guidance_repository, guidance_context, role_name, worktree, state_dir,
        )
        session_task = (_database_request(guidance_connection, lambda: guidance_repository.session_task(
            guidance_context.repo, provider, session_id))
                        if session_id else None)
        if session_task is not None and session_task != guidance.task_id:
            raise common_guidance.GuidanceError("Agent session과 공통 지침 작업 identity가 다릅니다.")
    except BaseException as error:
        if owned_connection is not None:
            owned_connection.close()
        if isinstance(error, DatabaseWaitTimeout) or not isinstance(
                error, (common_guidance.GuidanceError, sqlite3.Error, OSError)):
            raise
        return AgentResult(1, f"공통 지침 호출 준비 실패: {error}", session_id,
                           AgentResultKind.PERMANENT_CONFIGURATION)
    resolved: ResolvedProfile | None = resolved_profile
    collection_status = "unknown"
    adapter = effects.agent_provider(provider)
    prepared_launch = None
    observation_receipt = None
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
    request_sha256 = hashlib.sha256(json.dumps([
        provider, role_name, str(worktree.resolve()), prompt, read_only, level, persist_session,
        guidance.task_id, guidance.content_hash, invocation.definition_revision if invocation else None,
        invocation.purpose if invocation else None, invocation.target if invocation else None,
        asdict(resolved) if resolved else None,
    ], ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    if ledger is not None and invocation is not None:
        from .process_receipt import observation, result_message, delivered
        from loop_manager_internal.storage.task_calls import CallDeferred
        # ponytail: O(n) receipt scan within one task/role; add a pending index
        # only if long sessions make this costly, without duplicating the ledger.
        try:
            previous = _database_request(connection, lambda: connection.execute('''
                SELECT invocation_id, process_receipt_path FROM agent_invocations
                WHERE repo=? AND plan_id IS ? AND role=? AND process_receipt_path IS NOT NULL
                ORDER BY rowid DESC''', (invocation.repo, invocation.plan_id, role_name)).fetchall())
        except DatabaseWaitTimeout:
            raise
        except sqlite3.Error:
            previous = []
            collection_status = 'error'
        for previous_identity, path in previous:
            try:
                saved = observation(path, state_dir, previous_identity)
                if saved is None or not saved.get('database_pending'):
                    continue
                if 'started_at' not in saved and saved.get('outcome', {}).get('kind') == 'database_wait':
                    continue  # No physical invocation to replay or consume.
                if saved.get('request_sha256') != request_sha256:
                    raise ValueError('저장 대기한 Agent 호출의 요청 입력이 다릅니다.')
                outcome = saved.get('outcome')
                if outcome is None or outcome['code'] is None:
                    raise ValueError('저장 대기한 Agent 호출의 역할 결과가 부족합니다.')
                if ledger.reconcile_observations(state_dir=state_dir, invocation_id=previous_identity):
                    raise DatabaseWaitTimeout('기존 Agent 호출의 DB 확정 대기')
                stored = connection.execute('''SELECT result_kind, result_code, session_id,
                    input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens
                    FROM agent_invocations WHERE invocation_id=?''', (previous_identity,)).fetchone()
                if stored is None or tuple(stored) != (outcome['kind'], outcome['code'], outcome['session_id'],
                        *(outcome['usage'][key] for key in ('input_tokens', 'cached_input_tokens',
                          'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens'))):
                    raise ValueError('기존 Agent 관측과 확정된 invocation 결과가 다릅니다.')
                if outcome['session_id']:
                    _database_request(guidance_connection, lambda: guidance_repository.bind_session(
                        guidance_context.repo, provider, outcome['session_id'], guidance.task_id))
                message = result_message(path, saved['message_sha256'])
                pending = False
                if role_name == 'worker' and invocation.plan_id:
                    from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
                    pending = TargetedTestRepository(connection).pending(invocation.repo, invocation.plan_id)
                result = AgentResult(outcome['code'], message, outcome['session_id'],
                    AgentResultKind.TRANSIENT_FAILURE if outcome['kind'] == 'timeout' else
                    AgentResultKind(outcome['kind']), Usage(**outcome['usage']),
                    outcome['collection_status'], previous_identity, pending)
                if current_scheduler.get() is None or current_task.get() is None:
                    delivered(path, state_dir, previous_identity)
                return result
            except DatabaseWaitTimeout:
                raise
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                raise CallDeferred('기존 Agent 결과의 저장·원문 확인 대기: ' + type(error).__name__) from error
    identity: str | None = None
    execution_started = False
    execution_started_at: float | None = None
    scope_id = None
    if ledger is not None and invocation is not None and resolved is not None:
        def prepare_invocation():
            nonlocal scope_id
            # Re-read the current scope on every rolled-back preparation attempt.
            scope_id = None
            try:
                scope_id = ledger.invocation_scope_id(invocation.repo, role_name, str(worktree))
            except DatabaseWaitTimeout:
                raise
            except (sqlite3.Error, KeyError, IndexError, TypeError, ValueError, RuntimeError):
                pass  # Legacy/minimal records cannot grant reduced monitoring.
            prepared_identity = effects.start_invocation(
                ledger, invocation, role_name, provider, resolved, session_id, scope_id=scope_id)
            ledger.bind_guidance(prepared_identity, guidance.task_id, guidance.content_hash)
            return prepared_identity

        try:
            identity = _database_request(connection, prepare_invocation)
            collection_status = "pending"
        except DatabaseWaitTimeout:
            raise
        except sqlite3.Error:
            collection_status = "error"

    observations = {'request_sha256': request_sha256}
    def remember(**facts):
        nonlocal collection_status
        observations.update(facts)
        if observation_receipt is not None and identity is not None:
            from .process_receipt import observe
            try:
                observe(observation_receipt, identity, observations)
            except (OSError, ValueError, TypeError) as error:
                collection_status = 'error'
                events.emit('Agent 호출 관측 근거 보존 실패', level='WARN', invocation_id=identity,
                            error_type=type(error).__name__)

    def remember_outcome(kind, code, result_session, usage):
        remember(database_pending=True, outcome=dict(kind=kind, code=code, session_id=result_session, usage=asdict(usage),
                              duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                              collection_status='error' if collection_status == 'error' else usage.status,
                              observed_at=agent_invocation_state.utc_now()))

    def confirm_observations():
        def operation():
            try:
                _database_request(connection, lambda: ledger.apply_observation(identity, observations))
            except DatabaseWaitTimeout:
                raise
            except sqlite3.Error:
                # An unavailable optional callback column must not discard an
                # already observed provider outcome. Missing safety proof stays missing.
                outcome = observations['outcome']
                _database_request(connection, lambda: ledger.finish(
                    identity, outcome['kind'], outcome['code'], outcome['duration_ms'], outcome['session_id'],
                    Usage(**outcome['usage']), 'error', replace_session=True,
                    execution_status='executed' if execution_started else 'not_executed',
                    observed_at=outcome['observed_at']))
        try:
            operation()
        except DatabaseWaitTimeout:
            from loop_manager_internal.storage.task_calls import current_call
            scheduler, task, owner = current_scheduler.get(), current_task.get(), current_call.get()
            if scheduler is None or invocation is None or (owner is not None and owner.connection is not connection):
                raise
            scheduler.finish_agent_storage(task or invocation, operation, stopped='finished_at' in observations)

    def observe_session(started_session_id: str) -> None:
        nonlocal collection_status
        remember(session_id=started_session_id)
        try:
            _database_request(guidance_connection, lambda: guidance_repository.bind_session(
                guidance_context.repo, provider, started_session_id, guidance.task_id))
        except DatabaseWaitTimeout:
            collection_status = 'error'
        if ledger is not None and identity is not None:
            try:
                _database_request(connection, lambda: ledger.observe_session(identity, started_session_id))
            except sqlite3.Error:
                collection_status = "error"
        if on_session_started is not None:
            try:
                on_session_started(started_session_id)
            except DatabaseWaitTimeout:
                collection_status = 'error'

    def observe_isolation(evidence: agent_write_isolation.IsolationEvidence) -> None:
        if ledger is None or identity is None:
            raise agent_write_isolation.IsolationError(
                "Agent 격리 증거를 저장할 invocation 상태가 없습니다."
            )
        try:
            _database_request(connection, lambda: ledger.record_isolation(identity, evidence))
        except DatabaseWaitTimeout:
            raise
        except sqlite3.Error as error:
            raise agent_write_isolation.IsolationError(
                "Agent 격리 증거 저장 실패"
            ) from error

    def observe_execution_identity(value: dict[str, str]) -> None:
        if ledger is None or identity is None:
            raise agent_write_isolation.IsolationError('Agent 실제 실행 입력을 저장할 invocation이 없습니다.')
        try:
            _database_request(connection, lambda: ledger.record_execution_identity(identity, value))
        except DatabaseWaitTimeout:
            raise
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
        if execution_started:
            return
        execution_started = True
        execution_started_at = time.monotonic()
        remember(started_at=agent_invocation_state.utc_now())
        events.emit("Agent 실제 실행 시작", invocation_id=identity,
                    provider=provider, model=resolved.model if resolved else None,
                    effort=resolved.effort if resolved else None,
                    purpose=invocation.purpose if invocation else None,
                    attempt=invocation.attempt if invocation else 1,
                    session_transition=invocation.transition if invocation else None)
        if ledger is not None and identity is not None:
            try:
                _database_request(connection, lambda: ledger.mark_execution_started(
                    identity, observed_at=observations['started_at']))
            except sqlite3.Error:
                collection_status = "error"
        if notify_process_start:
            observe_process()

    def observe_cli_process_finished() -> None:
        nonlocal collection_status
        if ledger is None or identity is None or execution_started_at is None:
            return
        if 'finished_at' not in observations:
            remember(finished_at=agent_invocation_state.utc_now(),
                     duration_ms=max(0, int((time.monotonic() - execution_started_at) * 1000)))
        try:
            _database_request(connection, lambda: ledger.apply_observation(identity, observations))
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

    def bind_receipt(receipt):
        from loop_manager_internal.storage import task_calls
        task_calls.bind_agent_receipt(receipt)
        if identity is not None:
            task_calls.bind_agent(identity)
        if ledger is not None and identity is not None:
            ledger.bind_process_receipt(identity, str(receipt))

    def observe_targeted(directory, receipt):
        nonlocal observation_receipt
        if role_name != 'worker' or invocation is None or not invocation.plan_id:
            return
        if ledger is None or identity is None:
            raise agent_write_isolation.IsolationError('targeted test 기록을 결합할 invocation이 없습니다.')
        from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
        try:
            TargetedTestRepository(connection).bind(identity, directory, receipt, state_dir=state_dir)
            observation_receipt = Path(receipt)
            _database_request(connection, lambda: bind_receipt(receipt))
        except DatabaseWaitTimeout:
            # bind validates this Manager-owned path before attempting SQL.
            observation_receipt = Path(receipt)
            from .process_receipt import not_started
            try:
                not_started(observation_receipt)
            except OSError as error:
                events.emit('Agent 미시작 근거 보존 실패', level='WARN', invocation_id=identity,
                            error_type=type(error).__name__)
            raise
        except (OSError, ValueError, sqlite3.Error) as error:
            raise agent_write_isolation.IsolationError('targeted test 호출 결합 저장 실패') from error

    def observe_launch(launch):
        nonlocal prepared_launch, observation_receipt
        prepared_launch = launch
        from .process_receipt import receipt_path
        receipt = receipt_path(launch)
        observation_receipt = receipt
        try:
            _database_request(guidance_connection, lambda: bind_receipt(receipt))
            observe_targeted(launch.attempt, receipt)
        except BaseException:
            # This callback has not returned to the adapter: no spawn can have
            # occurred. Preserve that fact even if the DB is still unavailable.
            from .process_receipt import not_started
            try:
                not_started(receipt)
            except OSError as error:
                events.emit('Agent 미시작 근거 보존 실패', level='WARN', invocation_id=identity,
                            error_type=type(error).__name__)
            raise

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
        on_targeted_prepared=observe_targeted,
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
                from loop_manager_internal.runtime.handoff_gate import observe_runtime
                observe_runtime()  # DB preparation is complete; this is an actual stage entry.
                with events.log_context(invocation_id=identity):
                    raw = adapter.run(request)
            except common_guidance.GuidanceError as error:
                raw = AgentResult(1, f"공통 지침 provider 준비 실패: {error}", session_id,
                                  AgentResultKind.PERMANENT_CONFIGURATION)
            completed = _coerce_agent_result(raw, session_id)
            remember(message_sha256=hashlib.sha256(completed.message.encode('utf-8')).hexdigest())
            remember_outcome(completed.ledger_kind, completed.code, completed.session_id, completed.usage)
            if completed.session_id:
                try:
                    _database_request(guidance_connection, lambda: guidance_repository.bind_session(
                        guidance_context.repo, provider, completed.session_id, guidance.task_id))
                except sqlite3.Error as error:
                    collection_status = 'error'
                    events.emit('Agent 실제 결과 보존; session 저장 대기', level='WARN',
                                invocation_id=identity, error_type=type(error).__name__)
    except BaseException as error:
        events.emit("Agent 호출 종료 관측", level="WARN", invocation_id=identity,
                    result=type(error).__name__, execution_status="executed" if execution_started else "not_executed",
                    elapsed_seconds=round(time.monotonic() - started, 1))
        if ledger is not None and identity is not None:
            kind = ("database_wait" if isinstance(error, DatabaseWaitTimeout) and not execution_started else
                    "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else
                    "timeout" if isinstance(error, subprocess.TimeoutExpired) else
                    "transient_failure" if isinstance(error, OSError) else "exception")
            if completed is None:
                remember_outcome(kind, None, observations.get('session_id', session_id), Usage())
            else:
                remember(outcome={**observations['outcome'], 'collection_status': 'error'})
            try:
                confirm_observations()
                if completed is None:
                    remember(database_pending=False)
            except BaseException as storage_error:
                events.emit('Agent 원래 예외 보존; 호출 종료 저장 대기', level='WARN', invocation_id=identity,
                            error_type=type(storage_error).__name__, original_error_type=type(error).__name__)
        raise
    finally:
        if role_name == 'worker' and connection is not None and invocation is not None and invocation.plan_id:
            from loop_manager_internal.storage.targeted_test_state import TargetedTestRepository
            try:
                repository = TargetedTestRepository(connection)
                repository.reconcile(state_dir=state_dir, repo=invocation.repo, plan_id=invocation.plan_id)
                if repository.pending(invocation.repo, invocation.plan_id):
                    targeted_collection_error = 'targeted test 실행 근거의 종료 확인·재수집 대기'
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
        if 'outcome' not in observations:
            remember_outcome(result.ledger_kind, result.code, result.session_id, result.usage)
        remember(outcome={**observations['outcome'],
                          'collection_status': 'error' if collection_status == 'error' else result.usage.status})
        try:
            confirm_observations()
            if collection_status != "error":
                collection_status = result.usage.status
            if current_scheduler.get() is None or current_task.get() is None:
                remember(database_pending=False)
        except sqlite3.Error as error:
            collection_status = "error"
            events.emit('Agent 실제 결과 보존; 호출 종료 저장 대기', level='WARN', invocation_id=identity,
                        error_type=type(error).__name__)
    events.emit("Agent 호출 결과 관측", level="WARN" if result.code or collection_status == "error" else "INFO",
                invocation_id=identity, result=result.ledger_kind, result_code=result.code,
                execution_status="executed" if execution_started else "not_executed",
                elapsed_seconds=round(time.monotonic() - started, 1),
                usage_status=result.usage.status, collection_status=collection_status,
                input_tokens=result.usage.input_tokens, cached_input_tokens=result.usage.cached_input_tokens,
                cache_write_input_tokens=result.usage.cache_write_input_tokens,
                output_tokens=result.usage.output_tokens, reasoning_output_tokens=result.usage.reasoning_output_tokens)
    if targeted_collection_error and result.code == 0:
        return replace(result, collection_status='error', isolation_invocation_id=identity,
                       targeted_collection_pending=True)
    if isinstance(raw, AgentResult):
        return replace(raw, collection_status=collection_status,
                       isolation_invocation_id=identity)
    if identity is not None:
        return replace(result, collection_status=collection_status,
                       isolation_invocation_id=identity)
    return raw
