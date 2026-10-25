"""runtime registration service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.agents.agent_profiles import AGENT_PROVIDERS
from loop_manager_internal.application.contracts import ProjectRegistrationInspection
from loop_manager_internal.config import execution_path
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.storage import database, execplan_catalog_state
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import hashlib
import os
import shutil
import sqlite3


@dataclass(frozen=True)
class ValidateRuntimePrerequisitesEffects:
    """Named effects for validate_runtime_prerequisites; no entry-point/module access."""
    validate_provider_runtime: Callable[..., object]


def validate_runtime_prerequisites(
    effects: ValidateRuntimePrerequisitesEffects, path: Path,
    *,
    provider: str,
    config_path: Path | None,
    state_dir: Path | None = None,
) -> None:
    """Validate executable, authentication, and profile prerequisites only."""

    if not shutil.which("git") or not shutil.which("gh"):
        raise RuntimeError(execution_path.with_path_recovery_hint(
            "git과 gh CLI가 PATH에 있어야 합니다."))
    if provider not in AGENT_PROVIDERS:
        raise RuntimeError(f"지원하지 않는 Agent provider입니다: {provider}")
    if config_path is not None:
        effects.validate_provider_runtime(config_path, provider, path,
                                         **({'state_dir': state_dir} if state_dir is not None else {}))
    elif not shutil.which(provider):
        raise RuntimeError(execution_path.with_path_recovery_hint(
            f"선택한 {provider.capitalize()} CLI가 PATH에 설치되어 있지 않습니다."))


@dataclass(frozen=True)
class RepositoryOriginEffects:
    """Named effects for repository_origin; no entry-point/module access."""
    git: Callable[..., object]
    github_repository: Callable[..., object]


def repository_origin(effects: RepositoryOriginEffects, root: Path) -> str:
    return effects.github_repository(effects.git(root, "remote", "get-url", "origin").stdout)


@dataclass(frozen=True)
class ValidateRepositoryEffects:
    """Named effects for validate_repository; no entry-point/module access."""
    git: Callable[..., object]
    repository_origin: Callable[..., object]
    validate_runtime_prerequisites: Callable[..., object]


def validate_repository(
    effects: ValidateRepositoryEffects, path: Path,
    *,
    allow_agents_mismatch: bool,
    provider: str = "codex",
    config_path: Path | None = None,
    registration_checks: bool = True,
    expected_origin: str | None = None,
    origin_result: list[str] | None = None,
    state_dir: Path | None = None,
) -> Path:
    """Validate runtime prerequisites and stable repository identity only."""

    del allow_agents_mismatch, registration_checks
    path = path.expanduser().resolve()
    effects.validate_runtime_prerequisites(
        path,
        provider=provider,
        config_path=config_path,
        **({'state_dir': state_dir} if state_dir is not None else {}),
    )
    root = Path(effects.git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if root != path:
        raise RuntimeError(f"저장소 루트 경로를 지정하세요: {root}")
    current_origin = effects.repository_origin(root)
    if origin_result is not None:
        origin_result.append(current_origin)
    if expected_origin is not None and current_origin != expected_origin:
        raise RuntimeError(
            "등록된 GitHub origin과 현재 origin이 다릅니다: "
            f"{current_origin} (expected {expected_origin})"
        )
    return root


@dataclass(frozen=True)
class RemoteBaseEffects:
    """Named effects for remote_base; no entry-point/module access."""
    _recorded_project_fetch: Callable[..., object]
    git: Callable[..., object]


def remote_base(
    effects: RemoteBaseEffects, repo: Path, requested: str | None, *, state_dir: Path | None = None,
) -> str:
    if state_dir is None:
        effects.git(repo, "fetch", "origin")
    if requested:
        base = requested
    else:
        reference = effects.git(
            repo,
            "symbolic-ref",
            "--short",
            "refs/remotes/origin/HEAD",
        ).stdout.strip()
        base = reference.removeprefix("origin/")
    if state_dir is not None:
        effects._recorded_project_fetch(repo, state_dir, base)
    effects.git(repo, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}")
    return base


@dataclass(frozen=True)
class RevisionBlobEffects:
    """Named effects for _revision_blob; no entry-point/module access."""
    git_bytes: Callable[..., object]


def _revision_blob(
    effects: RevisionBlobEffects, root: Path,
    entries: Mapping[bytes, tuple[bytes, bytes]],
    relative_path: str,
) -> bytes | None:
    entry = entries.get(os.fsencode(relative_path))
    if entry is None:
        return None
    object_type, object_id = entry
    if object_type != b"blob":
        raise RuntimeError(f"등록 파일이 blob이 아닙니다: {relative_path}")
    return effects.git_bytes(root, "cat-file", "blob", object_id.decode("ascii"))


@dataclass(frozen=True)
class InspectRegistrationFilesEffects:
    """Named effects for inspect_registration_files; no entry-point/module access."""
    ROOT: Path
    _git_tree_entries: Callable[..., object]
    _is_active_requirement: Callable[..., object]
    _revision_blob: Callable[..., object]
    file_hash: Callable[..., object]
    normalized_text: Callable[..., object]


def inspect_registration_files(
    effects: InspectRegistrationFilesEffects, root: Path,
    revision: str,
    *,
    approve_agents_mismatch: bool,
) -> tuple[str | None, bool]:
    """Validate registration documents from one immutable commit tree."""

    entries = effects._git_tree_entries(root, revision)
    required_paths = ("docs/PLANS.md", "docs/ARCHITECTURE.md")
    blobs = {
        relative_path: effects._revision_blob(root, entries, relative_path)
        for relative_path in required_paths
    }
    missing = [path for path, contents in blobs.items() if contents is None]
    product = effects._revision_blob(root, entries, "docs/PRODUCT.md")
    active_requirements = tuple(
        path for path in entries if effects._is_active_requirement(path)
    )
    if product is None and not active_requirements:
        missing.append("docs/PRODUCT.md 또는 docs/requirements/*.md")
    if missing:
        raise RuntimeError("필수 파일이 없습니다: " + ", ".join(missing))

    plans = blobs["docs/PLANS.md"]
    architecture = blobs["docs/ARCHITECTURE.md"]
    assert plans is not None and architecture is not None
    if plans.replace(b"\r\n", b"\n") != effects.normalized_text(effects.ROOT / "docs/PLANS.md").encode("utf-8"):
        raise RuntimeError("docs/PLANS.md가 Loop Manager 기준 파일과 다릅니다.")
    if architecture.replace(b"\r\n", b"\n") == effects.normalized_text(
        effects.ROOT / "docs/ARCHITECTURE.template.md"
    ).encode("utf-8"):
        raise RuntimeError(
            "docs/ARCHITECTURE.md가 변경되지 않은 템플릿이라 등록할 수 없습니다."
        )
    if product is not None and product.replace(b"\r\n", b"\n") == effects.normalized_text(
        effects.ROOT / "docs/PRODUCT.template.md"
    ).encode("utf-8"):
        raise RuntimeError(
            "docs/PRODUCT.md가 변경되지 않은 템플릿이라 등록할 수 없습니다."
        )

    # Legacy registration fields remain nullable for existing audit records.
    return None, False


@dataclass(frozen=True)
class InspectProjectRegistrationEffects:
    """Named effects for inspect_project_registration; no entry-point/module access."""
    git: Callable[..., object]
    inspect_registration_files: Callable[..., object]
    remote_base: Callable[..., object]
    validate_repository: Callable[..., object]


def inspect_project_registration(
    effects: InspectProjectRegistrationEffects, path: Path,
    *,
    config_path: Path,
    provider: str,
    requested_base: str | None,
    approve_agents_mismatch: bool,
    state_dir: Path | None = None,
) -> ProjectRegistrationInspection:
    """Validate one project from its fetched origin revision before registry writes."""

    origins: list[str] = []
    root = effects.validate_repository(
        path,
        allow_agents_mismatch=approve_agents_mismatch,
        provider=provider,
        config_path=config_path,
        registration_checks=False,
        origin_result=origins,
        **({'state_dir': state_dir} if state_dir is not None else {}),
    )
    if not origins:
        raise RuntimeError("검증한 GitHub origin을 확인할 수 없습니다.")
    origin = origins[0]
    base_branch = effects.remote_base(root, requested_base, state_dir=state_dir)
    revision = effects.git(
        root,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{base_branch}^{{commit}}",
    ).stdout.strip()
    agents_hash, agents_approved = effects.inspect_registration_files(
        root,
        revision,
        approve_agents_mismatch=approve_agents_mismatch,
    )
    return ProjectRegistrationInspection(
        root=root,
        base_branch=base_branch,
        provider=provider,
        agents_hash=agents_hash,
        agents_approved=agents_approved,
        origin=origin,
        source_revision=revision,
    )


@dataclass(frozen=True)
class AddRegisteredProjectEffects:
    """Named effects for add_registered_project; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]
    _merged_execplan_ids: Callable[..., object]
    git: Callable[..., object]
    inspect_project_registration: Callable[..., object]
    migrate_legacy_execplans: Callable[..., object]
    revision_execplan_snapshot: Callable[..., object]


def add_registered_project(
    effects: AddRegisteredProjectEffects, connection: sqlite3.Connection,
    path: Path,
    *,
    config_path: Path,
    provider: str,
    requested_base: str | None,
    approve_agents_mismatch: bool,
    state_dir: Path | None = None,
) -> project_registry.RegisteredProject:
    inspection = effects.inspect_project_registration(
        path,
        config_path=config_path,
        provider=provider,
        requested_base=requested_base,
        approve_agents_mismatch=approve_agents_mismatch,
        state_dir=state_dir,
    )
    project = project_registry.RegisteredProject(
        repo=str(inspection.root),
        base_branch=inspection.base_branch,
        provider=inspection.provider,
        agents_hash=inspection.agents_hash,
        agents_approved=inspection.agents_approved,
        origin=inspection.origin,
    )
    # Schema migration is independent of registration and may commit. Finish it
    # before the registration transaction so a stale remote revision can roll
    # back the registry row together with recovered catalog and baseline data.
    project_registry.ensure_registry_schema(connection)
    effects._ensure_execplans_schema(connection)
    maintenance_state.ensure_schema(connection)
    pinned = None
    prepared = None
    before_project = connection.execute('SELECT * FROM projects WHERE repo=?', (project.repo,)).fetchone()
    bootstrap = database.has_remote_work_adoption_bootstrap(connection)
    if inspection.root.is_dir():
        current_sha = effects.git(inspection.root, 'rev-parse', '--verify',
            f'refs/remotes/origin/{project.base_branch}^{{commit}}').stdout.strip()
        source_sha = inspection.source_revision or current_sha
        if current_sha != source_sha:
            raise RuntimeError('등록 중 원격 source revision이 변경됐습니다.')
        snapshot = effects.revision_execplan_snapshot(inspection.root, source_sha,
            archive_conflict_plan_ids=effects._merged_execplan_ids(connection, project.repo))
        pinned = execplan_catalog_state.PinnedCatalog(source_sha, snapshot)
        if not bootstrap:
            prepared = effects.migrate_legacy_execplans(connection, reconcile_history=True, target_repo=project.repo,
                ensure_schema=False, pinned_catalog=pinned, prepare_only=True, registration=project)
        current_sha = effects.git(inspection.root, 'rev-parse', '--verify',
            f'refs/remotes/origin/{project.base_branch}^{{commit}}').stdout.strip()
        if current_sha != source_sha:
            raise RuntimeError('등록 중 원격 ExecPlan catalog revision이 변경됐습니다.')
    with runtime_state._transaction(connection):
        if connection.execute('SELECT * FROM projects WHERE repo=?', (project.repo,)).fetchone() != before_project:
            raise RuntimeError('등록 중 프로젝트 상태가 변경됐습니다.')
        project_registry.add_project(connection, project, manage_transaction=False)
        if pinned is not None and not bootstrap:
            effects.migrate_legacy_execplans(connection, reconcile_history=True, target_repo=project.repo,
                ensure_schema=False, manage_transaction=False, pinned_catalog=pinned, prepared=prepared)
            connection.execute(
                "INSERT INTO execplan_catalog_revisions "
                "(repo, revision_sha, catalog_fingerprint) VALUES (?, ?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET "
                "revision_sha = excluded.revision_sha, "
                "catalog_fingerprint = excluded.catalog_fingerprint",
                (project.repo, source_sha,
                 execplan_catalog_state._catalog_fingerprint(snapshot)),
            )
    return project
