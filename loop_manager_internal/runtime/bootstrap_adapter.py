"""runtime bootstrap adapter boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.application.contracts import CleanupAssessment
from loop_manager_internal.git import cleanup_service, manager_git_journal
from loop_manager_internal.runtime import bootstrap
from loop_manager_internal.runtime.contracts import RuntimeMutation
from loop_manager_internal.runtime.contracts import RuntimeWorktree, RuntimeRefresh
from loop_manager_internal.storage import catalog_migration
from pathlib import Path


@dataclass(frozen=True)
class BootstrapServiceEffects:
    """Named effects for _bootstrap_service; no entry-point/module access."""
    ROOT: Path
    _git_operation_entries: Callable[..., object]
    _ignored_worktree_paths: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    _validate_managed_path_boundary: Callable[..., object]
    _worktree_catalog: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., bytes]
    github_repository: Callable[..., object]
    operation_log: Callable[..., object]
    repository_origin: Callable[..., object]
    synchronize_base_worktree: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _bootstrap_service(effects: BootstrapServiceEffects, ) -> bootstrap.BootstrapService:
    return bootstrap.BootstrapService(
        bootstrap.BootstrapGitEffects(
            git=effects.git,
            git_bytes=effects.git_bytes,
            _git_operation_entries=effects._git_operation_entries,
            _validate_managed_path_boundary=effects._validate_managed_path_boundary,
            validate_managed_worktree=effects.validate_managed_worktree,
            _worktree_catalog=effects._worktree_catalog,
            _ignored_worktree_paths=effects._ignored_worktree_paths,
            _recorded_base_fetch=effects._recorded_base_fetch,
            github_repository=effects.github_repository,
            synchronize_base_worktree=effects.synchronize_base_worktree,
            repository_origin=effects.repository_origin,
            operation_log=effects.operation_log,
        ), runtime_root=effects.ROOT,
    )


@dataclass(frozen=True)
class CatalogMigrationEffectsEffects:
    """Named effects for _catalog_migration_effects; no entry-point/module access."""
    _LegacyCatalogMergeProof: Callable[..., object]
    _current_execplan_sha: Callable[..., object]
    pr_for_branch: Callable[..., object]
    revision_execplan_snapshot: Callable[..., object]


def _catalog_migration_effects(effects: CatalogMigrationEffectsEffects, ) -> catalog_migration.CatalogMigrationEffects:
    return catalog_migration.CatalogMigrationEffects(
        revision_execplan_snapshot=effects.revision_execplan_snapshot,
        pr_for_branch=effects.pr_for_branch,
        _current_execplan_sha=effects._current_execplan_sha,
        _LegacyCatalogMergeProof=effects._LegacyCatalogMergeProof,
    )


@dataclass(frozen=True)
class ValidateRuntimeWorktreeEffects:
    """Named effects for validate_runtime_worktree; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def validate_runtime_worktree(
    effects: ValidateRuntimeWorktreeEffects, source: Path,
    state_dir: Path,
    runtime: Path,
    commit_sha: str,
    *, command_id: str | None = None,
) -> None:
    return effects._bootstrap_service().validate_runtime_worktree(source, state_dir, runtime, commit_sha, command_id=command_id)


@dataclass(frozen=True)
class RuntimeCatalogRecordEffects:
    """Named effects for _runtime_catalog_record; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def _runtime_catalog_record(effects: RuntimeCatalogRecordEffects, source: Path, runtime: Path) -> dict[str, str | bool]:
    return effects._bootstrap_service()._runtime_catalog_record(source, runtime)


@dataclass(frozen=True)
class RuntimeTransitionStateEffects:
    """Named effects for _runtime_transition_state; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def _runtime_transition_state(effects: RuntimeTransitionStateEffects, source: Path, runtime: Path) -> dict[str, object]:
    return effects._bootstrap_service()._runtime_transition_state(source, runtime)


@dataclass(frozen=True)
class ManagedWorktreeTransitionStateEffects:
    """Named effects for _managed_worktree_transition_state; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _worktree_catalog: Callable[..., object]
    git: Callable[..., object]


def _managed_worktree_transition_state(effects: ManagedWorktreeTransitionStateEffects, repo: Path, worktree: Path) -> dict[str, object]:
    """Observe the registration and index tree of one ExecPlan workspace."""
    record = next((item for item in effects._worktree_catalog(repo)
                   if effects._canonical_path(Path(str(item.get("worktree", ""))))
                   == effects._canonical_path(worktree)), None)
    exists = worktree.exists() or worktree.is_symlink()
    index = None
    if exists and record is not None:
        unmerged = effects.git(worktree, 'ls-files', '-u', '-z').stdout
        index = {'unmerged': unmerged} if unmerged else effects.git(worktree, 'write-tree').stdout.strip()
    return {
        "exists": exists,
        "registration": ({"head": record.get("HEAD"),
                          "branch": record.get("branch"),
                          "locked": record.get("locked")}
                         if record is not None else None),
        "index": index,
    }


def _managed_worktree_receipt(
    state_dir: Path, intent: Mapping[str, object], kind: str,
) -> Path:
    return state_dir / "managed-worktree-intents" / f"{intent['id']}.{kind}.json"


@dataclass(frozen=True)
class ManagedWorktreeMarkerEffects:
    """Named effects for _managed_worktree_marker; no entry-point/module access."""
    _managed_worktree_receipt: Callable[..., object]


def _managed_worktree_marker(
    effects: ManagedWorktreeMarkerEffects, state_dir: Path, intent: Mapping[str, object], kind: str,
) -> bool:
    receipt = effects._managed_worktree_receipt(state_dir, intent, kind)
    return receipt.is_file() and manager_git_journal._read(receipt) == {
        "id": intent["id"], "repo": intent["repo"],
        "target": intent["target"], "before": intent["before"],
        "expected": intent["expected"],
    }


@dataclass(frozen=True)
class WriteManagedWorktreeReceiptEffects:
    """Named effects for _write_managed_worktree_receipt; no entry-point/module access."""
    _canonical_path: Callable[..., object]


def _write_managed_worktree_receipt(
    effects: WriteManagedWorktreeReceiptEffects, state_dir: Path, token: str, repo: Path, worktree: Path,
    before: Mapping[str, object], expected: Mapping[str, object], kind: str,
) -> None:
    root = state_dir / "managed-worktree-intents"
    root.mkdir(parents=True, exist_ok=True)
    manager_git_journal._sync_directory(state_dir)
    manager_git_journal._write_new(root / f"{token}.{kind}.json", {
        "id": token, "repo": str(effects._canonical_path(repo)),
        "target": str(effects._canonical_path(worktree)), "before": dict(before),
        "expected": dict(expected),
    })


@dataclass(frozen=True)
class RecoverManagedWorktreeRemovalEffects:
    """Named effects for _recover_managed_worktree_removal; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _managed_worktree_marker: Callable[..., object]
    _managed_worktree_transition_state: Callable[..., object]
    git: Callable[..., object]


def _recover_managed_worktree_removal(
    effects: RecoverManagedWorktreeRemovalEffects, state_dir: Path, repo: Path, worktree: Path,
) -> bool:
    return manager_git_journal.recover_active(
        state_dir, repo=str(effects._canonical_path(repo)),
        kind="managed_worktree_remove", target=str(effects._canonical_path(worktree)),
        observe=lambda: effects._managed_worktree_transition_state(repo, worktree),
        marker=lambda intent: effects._managed_worktree_marker(
            state_dir, intent, "remove",
        ),
        restore_intermediate=lambda intent, observed: cleanup_service.restore_cleanup_unlock(
            intent, observed, repo, worktree, git=effects.git,
            observe=lambda: effects._managed_worktree_transition_state(repo, worktree),
        ),
    )


@dataclass(frozen=True)
class AssessMissingManagedCleanupEffects:
    """Named effects for _assess_missing_managed_cleanup; no entry-point/module access."""
    _recover_managed_worktree_removal: Callable[..., object]


def _assess_missing_managed_cleanup(
    effects: AssessMissingManagedCleanupEffects, state_dir: Path, repo: Path, worktree: Path,
) -> CleanupAssessment:
    try:
        effects._recover_managed_worktree_removal(state_dir, repo, worktree)
    except RuntimeError as error:
        return CleanupAssessment("unverifiable", f"worktree 정리 기록 복구 실패: {error}")
    return CleanupAssessment("missing")


@dataclass(frozen=True)
class RuntimeTransitionMarkerEffects:
    """Named effects for _runtime_transition_marker; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def _runtime_transition_marker(
    effects: RuntimeTransitionMarkerEffects, state_dir: Path, source: Path, runtime: Path, sha: str,
    intent: Mapping[str, object],
) -> bool:
    return effects._bootstrap_service()._runtime_transition_marker(state_dir, source, runtime, sha, intent)


@dataclass(frozen=True)
class ValidateRuntimeLockOriginEffects:
    """Named effects for _validate_runtime_lock_origin; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def _validate_runtime_lock_origin(
    effects: ValidateRuntimeLockOriginEffects, state_dir: Path, source: Path, runtime: Path, sha: str, lock_reason: object,
) -> None:
    return effects._bootstrap_service()._validate_runtime_lock_origin(state_dir, source, runtime, sha, lock_reason)


@dataclass(frozen=True)
class ValidatedRuntimeRegistrationMarkerEffects:
    """Named effects for _validated_runtime_registration_marker; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def _validated_runtime_registration_marker(
    effects: ValidatedRuntimeRegistrationMarkerEffects, source: Path,
    state_dir: Path,
    runtime: Path,
    sha: str,
    mutation_id: str | None,
    *,
    legacy: bool,
    record_legacy_adoption: bool = False,
) -> str | None:
    return effects._bootstrap_service()._validated_runtime_registration_marker(source, state_dir, runtime, sha, mutation_id, legacy=legacy, record_legacy_adoption=record_legacy_adoption)


@dataclass(frozen=True)
class PrepareRuntimeWorktreeEffects:
    """Named effects for prepare_runtime_worktree; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def prepare_runtime_worktree(
    effects: PrepareRuntimeWorktreeEffects, source: Path, state_dir: Path, *, mutation_id: str | None = None,
    commit_sha: str | None = None, concurrent: bool = False,
) -> RuntimeWorktree:
    options = {"concurrent": True} if concurrent else {}
    return effects._bootstrap_service().prepare_runtime_worktree(source, state_dir, mutation_id=mutation_id, commit_sha=commit_sha, **options)


@dataclass(frozen=True)
class RemoveRuntimeWorktreeEffects:
    """Named effects for remove_runtime_worktree; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def remove_runtime_worktree(
    effects: RemoveRuntimeWorktreeEffects, source: Path, state_dir: Path, runtime: RuntimeWorktree,
) -> None:
    return effects._bootstrap_service().remove_runtime_worktree(source, state_dir, runtime)


@dataclass(frozen=True)
class BootstrapBaseBranchEffects:
    """Named effects for bootstrap_base_branch; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def bootstrap_base_branch(effects: BootstrapBaseBranchEffects, source: Path, state_dir: Path, origin: str) -> str:
    return effects._bootstrap_service().bootstrap_base_branch(source, state_dir, origin)


@dataclass(frozen=True)
class TrackingRefOidEffects:
    """Named effects for _tracking_ref_oid; no entry-point/module access."""
    git: Callable[..., object]


def _tracking_ref_oid(effects: TrackingRefOidEffects, repo: Path, refname: str) -> str | None:
    result = effects.git(repo, "rev-parse", "--verify", f"{refname}^{{commit}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


@dataclass(frozen=True)
class RunRuntimeChildEffects:
    """Named effects for run_runtime_child; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def run_runtime_child(
    effects: RunRuntimeChildEffects, source: Path,
    state_dir: Path,
    config_path: Path,
    runtime: RuntimeWorktree,
    arguments: list[str],
) -> int | RuntimeRefresh:
    return effects._bootstrap_service().run_runtime_child(source, state_dir, config_path, runtime, arguments)


@dataclass(frozen=True)
class ValidateRuntimeChildEffects:
    """Named effects for validate_runtime_child; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def validate_runtime_child(
    effects: ValidateRuntimeChildEffects, source: Path,
    state_dir: Path,
    commit_sha: str,
    *, command_id: str | None = None,
) -> None:
    return effects._bootstrap_service().validate_runtime_child(source, state_dir, commit_sha, command_id=command_id)


@dataclass(frozen=True)
class ValidateRuntimeMutationEffects:
    """Named effects for validate_runtime_mutation; no entry-point/module access."""
    _bootstrap_service: Callable[..., object]


def validate_runtime_mutation(
    effects: ValidateRuntimeMutationEffects, source: Path, state_dir: Path, sha: str, mutation_id: str | None,
    *, concurrent: bool = False,
) -> RuntimeMutation:
    return effects._bootstrap_service().validate_runtime_mutation(source, state_dir, sha, mutation_id, concurrent=concurrent)


@dataclass(frozen=True)
class GitCommonDirectoryEffects:
    """Named effects for _git_common_directory; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    git: Callable[..., object]


def _git_common_directory(effects: GitCommonDirectoryEffects, repository: Path) -> Path:
    value = effects.git(
        repository,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    ).stdout.strip()
    path = Path(value)
    return effects._canonical_path(path if path.is_absolute() else repository / path)


@dataclass(frozen=True)
class IsLoopManagerRepositoryEffects:
    """Named effects for is_loop_manager_repository; no entry-point/module access."""
    ROOT: Path
    _canonical_path: Callable[..., object]
    _git_common_directory: Callable[..., object]


def is_loop_manager_repository(effects: IsLoopManagerRepositoryEffects, repository: Path) -> bool:
    """Identify the registered Loop Manager source across its managed worktrees."""

    try:
        return effects._git_common_directory(repository) == effects._git_common_directory(effects.ROOT)
    except RuntimeError:
        return effects._canonical_path(repository) == effects._canonical_path(effects.ROOT)
