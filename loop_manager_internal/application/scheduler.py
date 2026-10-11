"""application scheduler boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from loop_manager_internal.application.contracts import ProjectRunKind
from loop_manager_internal.application.contracts import ProjectRunResult
from loop_manager_internal.application.contracts import ScheduledProjectResult
from loop_manager_internal.application.commands import (
    LocalRecoveryExecutionEffects, execute_local_recovery_request, next_local_recovery_request,
)
from loop_manager_internal.config.managed_config import ManagedConfig, fast_mode_settings
from loop_manager_internal.config.managed_config import ManagedConfigError
from loop_manager_internal.notifications import notifications
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.runtime.contracts import RuntimeMutation
from loop_manager_internal.runtime.handoff_gate import RuntimeHandoffGate
from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
from loop_manager_internal.runtime.handoff_gate import _runtime_handoff_signal
from pathlib import Path
import secrets
import sqlite3
import threading
from loop_manager_internal.verification.coordination import ExecutionSettings, VerifyCoordinator, settings as verify_settings


@dataclass(frozen=True)
class RunRegisteredProjectEffects:
    """Named effects for _run_registered_project; no entry-point/module access."""
    _check_runtime_handoff: Callable[..., object]
    connect_database: Callable[..., object]
    operation_log: Callable[..., object]
    run_project_result: Callable[..., object]


def _run_registered_project(
    effects: RunRegisteredProjectEffects, project: project_registry.RegisteredProject,
    state_dir: Path,
    config_path: Path,
    telegram_token: str,
    telegram_chat_id: str,
    planner_mutex: threading.Lock,
    refactoring_review_interval: int,
    verify_timeout_seconds: int = 600,
    runtime_mutation: RuntimeMutation | None = None,
    external_run_id: str | None = None,
    handoff_gate: RuntimeHandoffGate | None = None,
    verify_coordinator: VerifyCoordinator | None = None,
    verify_parallelism: str | int = "auto",
    managed_config: ManagedConfig | None = None,
) -> ScheduledProjectResult:
    """Run one registry row with state isolated to this worker connection."""

    config_token = fast_mode_settings.set(managed_config) if managed_config is not None else None
    handoff_token = _runtime_handoff_signal.set(handoff_gate)
    verify_token = (verify_settings.set(ExecutionSettings(verify_coordinator, verify_parallelism, state_dir))
                    if verify_coordinator is not None else None)
    try:
        effects._check_runtime_handoff()
        # The scheduler connection completed migrations before starting any
        # project threads. Repeating DDL here races with another project's
        # state transaction and can fail with SQLITE_BUSY.
        with closing(effects.connect_database(state_dir, run_migrations=False)) as connection, connection:
            notifications.configure_session(
                connection, enabled=bool(telegram_token and telegram_chat_id)
            )
            # TEMP session setup opens a SQLite transaction. Close it before
            # runtime-state repository operations so each lifecycle change
            # owns and durably commits its intended transaction boundary.
            connection.commit()

            def deliver_new_notifications() -> None:
                try:
                    notifications.flush_project_events(connection, project.repo)
                except Exception as error:
                    effects.operation_log(
                        f"즉시 알림 전송 실패 ({type(error).__name__})",
                        error=True,
                    )

            with notifications.immediate_delivery(connection, telegram_token, telegram_chat_id, project.repo):
                request = next_local_recovery_request(connection, project.repo)
                if request is not None:
                    result = execute_local_recovery_request(
                        LocalRecoveryExecutionEffects(effects.operation_log, effects.run_project_result),
                        connection, state_dir, request, config_path,
                        planner_mutex=planner_mutex,
                        refactoring_review_interval=refactoring_review_interval,
                        verify_timeout_seconds=verify_timeout_seconds,
                        runtime_mutation=runtime_mutation,
                        on_step_completed=deliver_new_notifications,
                    )
                else:
                    # An unrelated active owner keeps progressing; the recovery
                    # request stays pending for a later scheduler visit.
                    result = effects.run_project_result(
                        connection, state_dir, project.repo, config_path,
                        None, False, registered_only=True,
                        planner_mutex=planner_mutex,
                        refactoring_review_interval=refactoring_review_interval,
                        verify_timeout_seconds=verify_timeout_seconds,
                        on_step_completed=deliver_new_notifications,
                        external_run_id=external_run_id,
                        **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
                    )
    except SelfMergeRuntimeHandoff:
        if handoff_gate is not None:
            handoff_gate.stop()
        result = ProjectRunResult(ProjectRunKind.RUNTIME_HANDOFF, 0)
    except (ManagedConfigError, project_registry.ProjectRegistryError) as error:
        result = ProjectRunResult(
            ProjectRunKind.CONFIGURATION_ERROR,
            1,
            str(error),
        )
        effects.operation_log(
            f"프로젝트 설정 실패 ({project.repo}): {error}",
            error=True,
        )
    except Exception as error:
        result = ProjectRunResult(
            ProjectRunKind.OPERATIONAL_ERROR,
            1,
            str(error),
        )
        effects.operation_log(
            f"프로젝트 처리 실패 ({project.repo}): {error}",
            error=True,
        )
    finally:
        if verify_token is not None:
            verify_settings.reset(verify_token)
        _runtime_handoff_signal.reset(handoff_token)
        if config_token is not None:
            fast_mode_settings.reset(config_token)
    return ScheduledProjectResult(project.repo, result)


@dataclass(frozen=True)
class ScheduleRegisteredProjectsEffects:
    """Named effects for schedule_registered_projects; no entry-point/module access."""
    _preflight_self_merge_handoff: Callable[..., object]
    _run_registered_project: Callable[..., object]


def schedule_registered_projects(
    effects: ScheduleRegisteredProjectsEffects, connection: sqlite3.Connection,
    state_dir: Path,
    config: ManagedConfig,
    runtime_mutation: RuntimeMutation | None = None,
) -> tuple[ScheduledProjectResult, ...]:
    """Run each registered project once with bounded cross-project concurrency."""

    projects = project_registry.list_projects(connection)
    if not projects:
        return ()

    if runtime_mutation is not None:
        notifications.configure_session(
            connection, enabled=bool(config.telegram_token and config.telegram_chat_id),
        )
        connection.commit()
    effects._preflight_self_merge_handoff(
        connection, projects, state_dir, runtime_mutation, config_path=config.path,
    )

    planner_mutex = threading.Lock()
    handoff_gate = RuntimeHandoffGate(threading.Event(), threading.Lock())
    external_run_id = secrets.token_hex(16)
    worker_count = min(config.max_workers, len(projects))
    with VerifyCoordinator() as verify_coordinator, ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="loop-project",
    ) as pool:
        futures = tuple(
            pool.submit(
                effects._run_registered_project,
                project,
                state_dir,
                config.path,
                config.telegram_token,
                config.telegram_chat_id,
                planner_mutex,
                config.refactoring_review_interval,
                config.verify_timeout_seconds,
                external_run_id=external_run_id,
                handoff_gate=handoff_gate,
                verify_coordinator=verify_coordinator,
                verify_parallelism=config.verify_parallelism,
                managed_config=config,
                **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
            )
            for project in projects
        )
        return tuple(future.result() for future in futures)


@dataclass(frozen=True)
class RunRegisteredProjectsEffects:
    """Named effects for run_registered_projects; no entry-point/module access."""
    operation_log: Callable[..., object]
    schedule_registered_projects: Callable[..., object]


def run_registered_projects(
    effects: RunRegisteredProjectsEffects, connection: sqlite3.Connection,
    state_dir: Path,
    config: ManagedConfig,
    runtime_mutation: RuntimeMutation | None = None,
) -> int:
    """Run all registered projects and aggregate outcomes after every worker exits."""

    try:
        with connection:
            notifications.configure_session(
                connection, enabled=bool(config.telegram_token and config.telegram_chat_id),
            )
            notifications.recover_interrupted_starts(connection)
        notifications.dispatch_pending(
            connection, config.telegram_token, config.telegram_chat_id
        )
    except Exception as error:
        effects.operation_log(
            f"알림 재시도 실패 ({type(error).__name__})", error=True
        )
    try:
        results = effects.schedule_registered_projects(
            connection, state_dir, config,
            **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
        )
    except SelfMergeRuntimeHandoff:
        return 0
    if any(item.result.kind is ProjectRunKind.RUNTIME_HANDOFF for item in results):
        return 0
    try:
        notifications.dispatch_pending(
            connection, config.telegram_token, config.telegram_chat_id
        )
    except Exception as error:
        effects.operation_log(
            f"알림 재시도 실패 ({type(error).__name__})", error=True
        )
    return 1 if any(item.result.exit_code for item in results) else 0
