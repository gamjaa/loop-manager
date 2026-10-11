"""agents agent adapter boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents import agent_profiles
from loop_manager_internal.agents import agent_write_isolation
from loop_manager_internal.agents import invocation_service
from loop_manager_internal.agents import provider_adapters
from loop_manager_internal.agents import scope_baseline
from loop_manager_internal.agents import scope_lifecycle
from loop_manager_internal.agents import scope_recovery
from loop_manager_internal.agents import scope_validation
from loop_manager_internal.agents.agent_invocation import AgentProfile
from loop_manager_internal.agents.agent_invocation import AgentResult
from loop_manager_internal.agents.agent_invocation import InvocationContext
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.agents.provider_adapters import AgentProvider
from loop_manager_internal.agents.provider_adapters import ClaudeAgentAdapter
from loop_manager_internal.agents.provider_adapters import CodexAgentAdapter
from loop_manager_internal.application.contracts import CodexResult
from loop_manager_internal.runtime.handoff_gate import _runtime_handoff_signal
from loop_manager_internal.storage import agent_invocation_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class ProviderAdaptersEffectsEffects:
    """Named effects for _provider_adapters_effects; no entry-point/module access."""
    _agent_profile: Callable[..., object]
    _handoff_popen: Callable[..., object]
    _handoff_run: Callable[..., object]
    role: Callable[..., object]
    worker_profile: Callable[..., object]


def _provider_adapters_effects(effects: ProviderAdaptersEffectsEffects, ) -> provider_adapters.ProviderEffects:
    return provider_adapters.ProviderEffects(effects.role, effects.worker_profile, effects._agent_profile,
                                            effects._handoff_popen, effects._handoff_run)


@dataclass(frozen=True)
class InvocationServiceEffectsEffects:
    """Named effects for _invocation_service_effects; no entry-point/module access."""
    _agent_profile: Callable[..., object]
    _check_runtime_handoff: Callable[..., object]
    _start_agent_invocation: Callable[..., object]
    agent_provider: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]


def _invocation_service_effects(effects: InvocationServiceEffectsEffects, ) -> invocation_service.InvocationEffects:
    return invocation_service.InvocationEffects(effects._agent_profile, effects._check_runtime_handoff,
        effects._start_agent_invocation, effects.capture_git_snapshot, effects.git,
        effects.agent_provider)


def _start_agent_invocation(
    ledger: agent_invocation_state.InvocationRepository,
    invocation: InvocationContext,
    role_name: str,
    provider: str,
    profile: ResolvedProfile,
    session_id: str | None,
    *,
    scope_id: str | None,
) -> str:
    gate = _runtime_handoff_signal.get()
    if gate is None:
        return ledger.start(invocation, role_name, provider, profile, session_id, scope_id=scope_id)
    with gate.launch_lock:
        gate.check()
        return ledger.start(invocation, role_name, provider, profile, session_id, scope_id=scope_id)


@dataclass(frozen=True)
class ScopeValidationEffectsEffects:
    """Named effects for _scope_validation_effects; no entry-point/module access."""
    _agent_git_resources: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _git_common_directory: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    _scope_snapshot_with_start_ignore_rules: Callable[..., object]
    agent_snapshot_changes: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    operation_log: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _scope_validation_effects(effects: ScopeValidationEffectsEffects, ) -> scope_validation.ScopeValidationEffects:
    return scope_validation.ScopeValidationEffects(
        _agent_git_resources=effects._agent_git_resources,
        repository=runtime_state.RuntimeStateRepository,
        _current_rebase_service=effects._current_rebase_service,
        _git_common_directory=effects._git_common_directory,
        _owned_rebase_state_dir=effects._owned_rebase_state_dir,
        _plan_id_from_path=effects._plan_id_from_path,
        _rebase_in_progress_for_branch=effects._rebase_in_progress_for_branch,
        _scope_snapshot_with_start_ignore_rules=effects._scope_snapshot_with_start_ignore_rules,
        agent_snapshot_changes=effects.agent_snapshot_changes,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        operation_log=effects.operation_log,
        validate_managed_worktree=effects.validate_managed_worktree,
    )


@dataclass(frozen=True)
class ScopeLifecycleEffectsEffects:
    """Named effects for _scope_lifecycle_effects; no entry-point/module access."""
    _agent_git_resources: Callable[..., object]
    _capture_agent_baseline: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _reconciled_manager_scope_metadata: Callable[..., object]
    _remote_adoption_agent_baseline: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    project_execplan: Callable[..., object]
    update_project: Callable[..., object]
    validate_agent_scope: Callable[..., object]


def _scope_lifecycle_effects(effects: ScopeLifecycleEffectsEffects, ) -> scope_lifecycle.ScopeLifecycleEffects:
    return scope_lifecycle.ScopeLifecycleEffects(
        repository=runtime_state.RuntimeStateRepository,
        _agent_git_resources=effects._agent_git_resources,
        _capture_agent_baseline=effects._capture_agent_baseline,
        _current_rebase_service=effects._current_rebase_service,
        _owned_rebase_state_dir=effects._owned_rebase_state_dir,
        _plan_id_from_path=effects._plan_id_from_path,
        _published_plan_allows_common_plans=effects._published_plan_allows_common_plans,
        _reconciled_manager_scope_metadata=effects._reconciled_manager_scope_metadata,
        _remote_adoption_agent_baseline=effects._remote_adoption_agent_baseline,
        capture_git_snapshot=effects.capture_git_snapshot,
        project_execplan=effects.project_execplan,
        update_project=effects.update_project,
        validate_agent_scope=effects.validate_agent_scope,
    )


@dataclass(frozen=True)
class ScopeRecoveryEffectsEffects:
    """Named effects for _scope_recovery_effects; no entry-point/module access."""
    _agent_git_resources: Callable[..., object]
    _config_entries: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _normalize_legacy_ignored_snapshot: Callable[..., object]
    _owned_checkpoint_remote_oid: Callable[..., object]
    _owned_rebase_state_dir: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    _proven_manager_base_refs: Callable[..., object]
    _proven_merged_cleanup_paths: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _reconciled_manager_scope_metadata: Callable[..., object]
    _validate_runtime_lock_origin: Callable[..., object]
    _validated_runtime_registration_marker: Callable[..., object]
    _with_proven_legacy_ignore_config: Callable[..., object]
    active_execplan: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    github_repository: Callable[..., object]
    operation_log: Callable[..., object]
    project_execplan: Callable[..., object]
    recover_persisted_agent_worktree: Callable[..., object]
    requirement_archive_candidates: Callable[..., object]
    update_project: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    validate_runtime_worktree: Callable[..., object]


def _scope_recovery_effects(effects: ScopeRecoveryEffectsEffects, ) -> scope_recovery.ScopeRecoveryEffects:
    return scope_recovery.ScopeRecoveryEffects(
        repository=runtime_state.RuntimeStateRepository,
        _agent_git_resources=effects._agent_git_resources,
        _config_entries=effects._config_entries,
        _current_rebase_service=effects._current_rebase_service,
        _normalize_legacy_ignored_snapshot=effects._normalize_legacy_ignored_snapshot,
        _owned_checkpoint_remote_oid=effects._owned_checkpoint_remote_oid,
        _owned_rebase_state_dir=effects._owned_rebase_state_dir,
        _plan_id_from_path=effects._plan_id_from_path,
        _proven_manager_base_refs=effects._proven_manager_base_refs,
        _proven_merged_cleanup_paths=effects._proven_merged_cleanup_paths,
        _published_plan_allows_common_plans=effects._published_plan_allows_common_plans,
        _reconciled_manager_scope_metadata=effects._reconciled_manager_scope_metadata,
        _validate_runtime_lock_origin=effects._validate_runtime_lock_origin,
        _validated_runtime_registration_marker=effects._validated_runtime_registration_marker,
        _with_proven_legacy_ignore_config=effects._with_proven_legacy_ignore_config,
        active_execplan=effects.active_execplan,
        capture_git_snapshot=effects.capture_git_snapshot,
        git=effects.git,
        github_repository=effects.github_repository,
        operation_log=effects.operation_log,
        project_execplan=effects.project_execplan,
        recover_persisted_agent_worktree=effects.recover_persisted_agent_worktree,
        requirement_archive_candidates=effects.requirement_archive_candidates,
        update_project=effects.update_project,
        validate_agent_scope=effects.validate_agent_scope,
        validate_runtime_worktree=effects.validate_runtime_worktree,
    )


@dataclass(frozen=True)
class ScopeBaselineEffectsEffects:
    """Named effects for _scope_baseline_effects; no entry-point/module access."""
    _handoff_run: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    command: Callable[..., object]
    git: Callable[..., object]


def _scope_baseline_effects(effects: ScopeBaselineEffectsEffects, ) -> scope_baseline.ScopeBaselineEffects:
    return scope_baseline.ScopeBaselineEffects(
        _handoff_run=effects._handoff_run,
        capture_git_snapshot=effects.capture_git_snapshot,
        command=effects.command,
        git=effects.git,
    )


@dataclass(frozen=True)
class AgentProfilesEffectsEffects:
    """Named effects for _agent_profiles_effects; no entry-point/module access."""
    claude_model_efforts: Callable[..., object]
    claude_role: Callable[..., object]
    claude_worker_profile: Callable[..., object]
    codex_model_efforts: Callable[..., object]
    command: Callable[..., object]
    role: Callable[..., object]
    worker_profile: Callable[..., object]


def _agent_profiles_effects(effects: AgentProfilesEffectsEffects, ) -> agent_profiles.ProfileEffects:
    return agent_profiles.ProfileEffects(
        command=effects.command,
        role=effects.role,
        worker_profile=effects.worker_profile,
        claude_role=effects.claude_role,
        claude_worker_profile=effects.claude_worker_profile,
        codex_model_efforts=effects.codex_model_efforts,
        claude_model_efforts=effects.claude_model_efforts,
    )


@dataclass(frozen=True)
class CodexProfileEffects:
    """Named effects for codex_profile; no entry-point/module access."""
    codex_model_efforts: Callable[..., object]
    provider_profile: Callable[..., object]


def codex_profile(
    effects: CodexProfileEffects, config_path: Path,
    section: str,
    name: str,
    label: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return effects.provider_profile(
        config_path,
        "codex",
        section,
        name,
        label,
        effects.codex_model_efforts(worktree),
    )


@dataclass(frozen=True)
class RoleEffects:
    """Named effects for role; no entry-point/module access."""
    codex_profile: Callable[..., object]


def role(effects: RoleEffects, config_path: Path, name: str, worktree: Path | None = None) -> tuple[str, str]:
    return effects.codex_profile(config_path, "codex.roles", name, f"Codex {name}", worktree)


@dataclass(frozen=True)
class WorkerProfileEffects:
    """Named effects for worker_profile; no entry-point/module access."""
    codex_model_efforts: Callable[..., object]
    provider_worker_profile: Callable[..., object]


def worker_profile(
    effects: WorkerProfileEffects, config_path: Path,
    level: int,
    worktree: Path | None = None,
) -> tuple[str, str]:
    if type(level) is not int or not 1 <= level <= 8:
        raise RuntimeError(f"Codex Worker level이 유효하지 않습니다: {level}")
    return effects.provider_worker_profile(
        config_path,
        "codex",
        level,
        effects.codex_model_efforts(worktree),
    )


@dataclass(frozen=True)
class ClaudeModelEffortsEffects:
    """Named effects for claude_model_efforts; no entry-point/module access."""
    _agent_profiles_effects: Callable[..., object]


def claude_model_efforts(effects: ClaudeModelEffortsEffects, config_path: Path, worktree: Path | None=None) -> dict[str, frozenset[str]]:
    return agent_profiles.claude_model_efforts(config_path, worktree, effects=effects._agent_profiles_effects())


@dataclass(frozen=True)
class ClaudeRoleEffects:
    """Named effects for claude_role; no entry-point/module access."""
    claude_model_efforts: Callable[..., object]
    provider_profile: Callable[..., object]


def claude_role(
    effects: ClaudeRoleEffects, config_path: Path,
    name: str,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return effects.provider_profile(
        config_path,
        "claude",
        "claude.roles",
        name,
        f"Claude {name}",
        effects.claude_model_efforts(config_path, worktree),
    )


@dataclass(frozen=True)
class ClaudeWorkerProfileEffects:
    """Named effects for claude_worker_profile; no entry-point/module access."""
    claude_model_efforts: Callable[..., object]
    provider_worker_profile: Callable[..., object]


def claude_worker_profile(
    effects: ClaudeWorkerProfileEffects, config_path: Path,
    level: int,
    worktree: Path | None = None,
) -> tuple[str, str]:
    return effects.provider_worker_profile(
        config_path,
        "claude",
        level,
        effects.claude_model_efforts(config_path, worktree),
    )


@dataclass(frozen=True)
class RunCodexEffects:
    """Named effects for run_codex; no entry-point/module access."""
    _provider_adapters_effects: Callable[..., object]


def run_codex(
    effects: RunCodexEffects, worktree: Path,
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
    resolved_profile: ResolvedProfile | None = None,
    isolation_arguments: tuple[str, ...] | None = None,
    isolation_evidence: agent_write_isolation.IsolationEvidence | None = None,
    isolation_snapshot_digest: Callable[[], str] | None = None,
    expected_repository: str | None = None,
    on_isolation_checked: Callable[[agent_write_isolation.IsolationEvidence], None] | None = None,
    on_process_started: Callable[[], None] | None = None,
    on_process_finished: Callable[[], None] | None = None,
    common_guidance: object | None = None,
) -> tuple[int, str] | CodexResult:
    return provider_adapters.run_codex(
        worktree,
        state_dir,
        config_path,
        role_name,
        prompt,
        read_only=read_only,
        level=level,
        persist_session=persist_session,
        session_id=session_id,
        on_session_started=on_session_started,
        resolved_profile=resolved_profile,
        isolation_arguments=isolation_arguments,
        isolation_evidence=isolation_evidence,
        isolation_snapshot_digest=isolation_snapshot_digest,
        expected_repository=expected_repository,
        on_isolation_checked=on_isolation_checked,
        common_guidance=common_guidance,
        on_process_started=on_process_started,
        on_process_finished=on_process_finished,
        effects=effects._provider_adapters_effects(),
    )


@dataclass(frozen=True)
class AgentProfileEffects:
    """Named effects for _agent_profile; no entry-point/module access."""
    _agent_profiles_effects: Callable[..., object]


def _agent_profile(
    effects: AgentProfileEffects, config_path: Path,
    provider: str,
    role_name: str,
    level: int | None,
    worktree: Path,
) -> AgentProfile:
    return agent_profiles._agent_profile(
        config_path,
        provider,
        role_name,
        level,
        worktree,
        effects=effects._agent_profiles_effects(),
    )


@dataclass(frozen=True)
class ValidateProviderRuntimeEffects:
    """Named effects for validate_provider_runtime; no entry-point/module access."""
    _agent_profiles_effects: Callable[..., object]


def validate_provider_runtime(effects: ValidateProviderRuntimeEffects, config_path: Path, provider: str, worktree: Path | None=None) -> None:
    return agent_profiles.validate_provider_runtime(
        config_path,
        provider,
        worktree,
        effects=effects._agent_profiles_effects(),
    )


@dataclass(frozen=True)
class RunClaudeEffects:
    """Named effects for run_claude; no entry-point/module access."""
    _provider_adapters_effects: Callable[..., object]


def run_claude(
    effects: RunClaudeEffects, worktree: Path,
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
    resolved_profile: ResolvedProfile | None = None,
    on_isolation_checked: Callable[[agent_write_isolation.IsolationEvidence], None] | None = None,
    isolation_snapshot_digest: Callable[[], str] | None = None,
    expected_repository: str | None = None,
    on_process_started: Callable[[], None] | None = None,
    on_process_finished: Callable[[], None] | None = None,
    stream_output: bool = False,
    common_guidance: object | None = None,
) -> tuple[int, str] | AgentResult:
    return provider_adapters.run_claude(
        worktree,
        state_dir,
        config_path,
        role_name,
        prompt,
        read_only=read_only,
        level=level,
        persist_session=persist_session,
        session_id=session_id,
        on_session_started=on_session_started,
        resolved_profile=resolved_profile,
        on_isolation_checked=on_isolation_checked,
        isolation_snapshot_digest=isolation_snapshot_digest,
        expected_repository=expected_repository,
        common_guidance=common_guidance,
        on_process_started=on_process_started,
        on_process_finished=on_process_finished,
        stream_output=stream_output,
        effects=effects._provider_adapters_effects(),
    )


@dataclass(frozen=True)
class AgentProviderEffects:
    """Named effects for agent_provider; no entry-point/module access."""
    run_claude: Callable[..., object]
    run_codex: Callable[..., object]


def agent_provider(effects: AgentProviderEffects, name: str) -> AgentProvider:
    if name == "codex":
        return CodexAgentAdapter(effects.run_codex)
    if name == "claude":
        return ClaudeAgentAdapter(effects.run_claude)
    raise RuntimeError(f"지원하지 않는 Agent provider입니다: {name}")


@dataclass(frozen=True)
class RunAgentEffects:
    """Named effects for run_agent; no entry-point/module access."""
    _invocation_service_effects: Callable[..., object]


def run_agent(
    effects: RunAgentEffects, provider: str,
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
) -> tuple[int, str] | AgentResult:
    return invocation_service.run_agent(
        provider,
        worktree,
        state_dir,
        config_path,
        role_name,
        prompt,
        read_only=read_only,
        level=level,
        persist_session=persist_session,
        session_id=session_id,
        on_session_started=on_session_started,
        connection=connection,
        invocation=invocation,
        resolved_profile=resolved_profile,
        effects=effects._invocation_service_effects(),
    )


@dataclass(frozen=True)
class RunAgentWithConnectionEffects:
    """Named effects for _run_agent_with_connection; no entry-point/module access."""
    run_agent: Callable[..., object]


def _run_agent_with_connection(
    effects: RunAgentWithConnectionEffects, connection: sqlite3.Connection, *args: object, **kwargs: object
) -> tuple[int, str] | AgentResult:
    """Keep execution coordinator unaware of the ledger connection."""
    return effects.run_agent(*args, connection=connection, **kwargs)
