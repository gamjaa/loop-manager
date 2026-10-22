"""application self runtime boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import WaitingPullRequestObservation
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.runtime.contracts import RuntimeMutation, RuntimeRefresh
from loop_manager_internal.application import runtime_inputs
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.runtime.handoff_gate import SelfMergeRuntimeHandoff
from loop_manager_internal.runtime.handoff_gate import _runtime_handoff_signal
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class RegisteredSelfRuntimeProjectEffects:
    """Named effects for _registered_self_runtime_project; no entry-point/module access."""
    ROOT: Path
    _canonical_path: Callable[..., object]
    _git_common_directory: Callable[..., object]
    bootstrap_base_branch: Callable[..., object]
    git: Callable[..., object]
    repository_origin: Callable[..., object]


def _registered_self_runtime_project(
    effects: RegisteredSelfRuntimeProjectEffects, project: project_registry.RegisteredProject,
    runtime: RuntimeMutation,
    state_dir: Path,
) -> bool:
    """Match the parent's source checkout, not an alias or another clone."""

    source = effects._canonical_path(runtime.source)
    if effects._canonical_path(runtime.path) != effects._canonical_path(effects.ROOT):
        return False
    if project.repo != str(source) or Path(project.repo).is_symlink():
        return False
    repository = Path(project.repo)
    top_level = effects._canonical_path(Path(effects.git(
        repository, "rev-parse", "--show-toplevel"
    ).stdout.strip()))
    if top_level != source or effects._git_common_directory(repository) != effects._git_common_directory(source):
        raise RuntimeError("등록된 self source repository identity가 다릅니다.")
    if effects._git_common_directory(runtime.path) != effects._git_common_directory(source):
        raise RuntimeError("runtime과 self source repository identity가 다릅니다.")
    source_origin = effects.repository_origin(source)
    if effects.repository_origin(repository) != source_origin or effects.repository_origin(runtime.path) != source_origin:
        raise RuntimeError("runtime과 등록된 self source origin이 다릅니다.")
    if project.origin is not None and project.origin != source_origin:
        raise RuntimeError("등록된 self origin이 현재 origin과 다릅니다.")
    if not project.base_branch:
        raise RuntimeError("등록된 self base branch가 비어 있습니다.")
    if effects.bootstrap_base_branch(source, state_dir, source_origin) != project.base_branch:
        raise RuntimeError("runtime과 등록된 self base branch가 다릅니다.")
    if effects.git(runtime.path, "rev-parse", "HEAD").stdout.strip() != runtime.commit_sha:
        raise RuntimeError("현재 runtime SHA가 선택된 SHA와 다릅니다.")
    return True


@dataclass(frozen=True)
class SelfRuntimeProjectEffects:
    """Named effects for _self_runtime_project; no entry-point/module access."""
    _registered_self_runtime_project: Callable[..., object]


def _self_runtime_project(
    effects: SelfRuntimeProjectEffects, connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    runtime: RuntimeMutation | None,
) -> project_registry.RegisteredProject | None:
    if runtime is None:
        return None
    for project in project_registry.list_projects(connection):
        if project.repo == repo:
            return project if effects._registered_self_runtime_project(
                project, runtime, state_dir,
            ) else None
    return None


@dataclass(frozen=True)
class FinishSelfMergeHandoffEffects:
    _registered_self_runtime_project: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., bytes]
    operation_log: Callable[..., object]
    repository_origin: Callable[..., object] | None = None


def _finish_self_merge_handoff(
    effects: FinishSelfMergeHandoffEffects, connection: sqlite3.Connection,
    project: project_registry.RegisteredProject, state_dir: Path,
    runtime: RuntimeMutation, observation: WaitingPullRequestObservation | None = None,
) -> None:
    """Compare the actual running revision independently of PR and local checkout."""
    try:
        if not effects._registered_self_runtime_project(project, runtime, state_dir):
            raise RuntimeError('등록된 self source identity가 변경됐습니다.')
        remote_sha = effects._recorded_base_fetch(
            Path(project.repo), state_dir, project.base_branch, owner='runtime-observation')
        if remote_sha == runtime.commit_sha:
            return
        if effects.git(Path(project.repo), 'merge-base', '--is-ancestor',
                       runtime.commit_sha, remote_sha, check=False).returncode:
            raise RuntimeError('runtime과 원격 base의 fast-forward 관계를 확인할 수 없습니다.')
        code_changed = runtime_inputs.changed(effects.git_bytes, Path(project.repo), runtime.commit_sha, remote_sha)
        if not effects._registered_self_runtime_project(project, runtime, state_dir):
            raise RuntimeError('runtime 비교 뒤 source identity가 변경됐습니다.')
        effects.operation_log(
            f'runtime 입력 비교: repository={project.repo} base={project.base_branch} '
            f'runtime={runtime.commit_sha} remote={remote_sha} code_changed={code_changed}')
        if not code_changed:
            return
        origin = project.origin or (effects.repository_origin(Path(project.repo)) if effects.repository_origin else None)
        if not runtime.mutation_id or not origin:
            raise RuntimeError('runtime 인계 소유 identity가 없습니다.')
        request = RuntimeRefresh(project.repo, origin, project.base_branch,
                                 runtime.commit_sha, remote_sha, runtime.mutation_id)
        gate = _runtime_handoff_signal.get()
        if gate is not None:
            gate.stop(request)
        raise SelfMergeRuntimeHandoff(request)
    except (RuntimeError, OSError, ValueError) as error:
        raise RecoveryFailure(RecoveryReason.FETCH_FAILED,
                              f'runtime 원격 코드 관측 미완료: {error}') from error


@dataclass(frozen=True)
class PreflightSelfMergeHandoffEffects:
    _finish_self_merge_handoff: Callable[..., object]
    _registered_self_runtime_project: Callable[..., object]


def _preflight_self_merge_handoff(
    effects: PreflightSelfMergeHandoffEffects, connection: sqlite3.Connection,
    projects: tuple[project_registry.RegisteredProject, ...], state_dir: Path,
    runtime: RuntimeMutation | None, *, config_path: Path | None = None,
) -> None:
    if runtime is None:
        return
    # Self identity is a property of the runtime, even when self is not selected.
    for project in project_registry.list_projects(connection):
        if effects._registered_self_runtime_project(project, runtime, state_dir):
            effects._finish_self_merge_handoff(connection, project, state_dir, runtime)
            return


@dataclass(frozen=True)
class HandoffObservedSelfMergeEffects:
    """Named effects for _handoff_observed_self_merge; no entry-point/module access."""
    _finish_self_merge_handoff: Callable[..., object]
    _self_runtime_project: Callable[..., object]


def _handoff_observed_self_merge(
    effects: HandoffObservedSelfMergeEffects, connection: sqlite3.Connection,
    repo: str,
    state_dir: Path,
    runtime: RuntimeMutation | None,
    observation: WaitingPullRequestObservation,
) -> None:
    """Guard a merge newly observed after the scheduler preflight."""

    if runtime is None or not observation.merged:
        return
    project = effects._self_runtime_project(connection, repo, state_dir, runtime)
    if project is not None:
        effects._finish_self_merge_handoff(
            connection, project, state_dir, runtime, observation,
        )
