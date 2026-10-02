"""application self runtime boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import WaitingPullRequestObservation
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.runtime.contracts import BaseWorktreeSyncResult
from loop_manager_internal.runtime.contracts import RuntimeMutation
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
    """Named effects for _finish_self_merge_handoff; no entry-point/module access."""
    _registered_self_runtime_project: Callable[..., object]
    ensure_safe_base_worktree: Callable[..., object]
    git: Callable[..., object]
    log_base_worktree_sync: Callable[..., object]
    operation_log: Callable[..., object]


def _finish_self_merge_handoff(
    effects: FinishSelfMergeHandoffEffects, connection: sqlite3.Connection,
    project: project_registry.RegisteredProject,
    state_dir: Path,
    runtime: RuntimeMutation,
    observation: WaitingPullRequestObservation,
) -> None:
    """Use the durable merge observation to decide a normal runtime exit."""

    if not observation.merged:
        return
    if not effects._registered_self_runtime_project(project, runtime, state_dir):
        raise RuntimeError("self Merge 뒤 source identity가 변경됐습니다.")
    repo_path = Path(project.repo)
    base = project.base_branch
    remote_sha = effects.git(
        repo_path, "rev-parse", "--verify",
        f"refs/remotes/origin/{base}^{{commit}}",
    ).stdout.strip()
    if scoped_blocking.project_block(connection, project.repo) is None:
        sync = effects.ensure_safe_base_worktree(
            repo_path, base, remote_sha=remote_sha, state_dir=state_dir,
        )
    else:
        sync = BaseWorktreeSyncResult(
            "skipped", "프로젝트 안전 차단 중 source checkout 보존", remote_sha,
        )
    effects.log_base_worktree_sync(repo_path, sync)
    if effects.git(
        repo_path, "rev-parse", "--verify",
        f"refs/remotes/origin/{base}^{{commit}}",
    ).stdout.strip() != remote_sha:
        raise RuntimeError("self Merge 뒤 origin base ref가 변경됐습니다.")
    if not effects._registered_self_runtime_project(project, runtime, state_dir):
        raise RuntimeError("self Merge 뒤 source identity가 변경됐습니다.")
    if remote_sha == runtime.commit_sha:
        return
    ancestor = effects.git(
        repo_path, "merge-base", "--is-ancestor",
        runtime.commit_sha, remote_sha, check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError(
            "self Merge 뒤 runtime과 원격 base의 fast-forward 관계를 "
            "확인할 수 없습니다."
        )
    gate = _runtime_handoff_signal.get()
    if gate is not None:
        gate.stop()
    effects.operation_log(
        f"self Merge runtime handoff: repository={project.repo} "
        f"base={base} runtime={runtime.commit_sha} remote={remote_sha} "
        f"prs={','.join(observation.merged_pr_urls)} "
        f"source_sync={sync.status} ({sync.reason})"
    )
    raise SelfMergeRuntimeHandoff()


@dataclass(frozen=True)
class PreflightSelfMergeHandoffEffects:
    """Named effects for _preflight_self_merge_handoff; no entry-point/module access."""
    _finish_self_merge_handoff: Callable[..., object]
    _registered_self_runtime_project: Callable[..., object]
    observe_waiting_pull_requests: Callable[..., object]


def _preflight_self_merge_handoff(
    effects: PreflightSelfMergeHandoffEffects, connection: sqlite3.Connection,
    projects: tuple[project_registry.RegisteredProject, ...],
    state_dir: Path,
    runtime: RuntimeMutation | None,
) -> None:
    """Reconcile self PRs before any project thread can start new work."""

    if runtime is None:
        return
    for project in projects:
        if not effects._registered_self_runtime_project(project, runtime, state_dir):
            continue
        observation = effects.observe_waiting_pull_requests(
            connection, Path(project.repo), state_dir, project.repo,
            require_base_identity=True,
        )
        effects._finish_self_merge_handoff(
            connection, project, state_dir, runtime, observation,
        )
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
