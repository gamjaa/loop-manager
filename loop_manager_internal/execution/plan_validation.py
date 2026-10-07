"""execution plan validation boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.application.contracts import ExecPlanMetadataError
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.execution import execution_revision
from loop_manager_internal.git.command_adapter import ExternalObservationError, revision_document_or_none
from loop_manager_internal.runtime.contracts import GitLockError
from pathlib import Path
import hashlib
import json
import sqlite3
import stat


def _execplan_input_fingerprint(entries: list[tuple[str, str | None]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"loop-manager-blocked-input-v1\0")
    for relative_path, contents in entries:
        path_bytes = relative_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        if contents is None:
            digest.update(b"\0")
            continue
        normalized = contents.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        digest.update(b"\1")
        digest.update(len(normalized).to_bytes(8, "big"))
        digest.update(normalized)
    return digest.hexdigest()


@dataclass(frozen=True)
class WorktreeExecplanInputFingerprintEffects:
    """Named effects for _worktree_execplan_input_fingerprint; no entry-point/module access."""
    _execplan_input_fingerprint: Callable[..., object]
    _execplan_input_paths: Callable[..., object]


def _worktree_execplan_input_fingerprint(effects: WorktreeExecplanInputFingerprintEffects, worktree: Path, plan: sqlite3.Row) -> str:
    entries = []
    for relative_path in effects._execplan_input_paths(plan):
        path = worktree / relative_path
        try:
            regular = stat.S_ISREG(path.stat().st_mode)
        except FileNotFoundError:
            regular = False
        contents = path.read_text(encoding="utf-8") if regular else None
        entries.append((relative_path, contents))
    return effects._execplan_input_fingerprint(entries)


@dataclass(frozen=True)
class RevisionExecplanInputFingerprintEffects:
    """Named effects for _revision_execplan_input_fingerprint; no entry-point/module access."""
    _execplan_input_fingerprint: Callable[..., object]
    _execplan_input_paths: Callable[..., object]
    git: Callable[..., object]


def _revision_execplan_input_fingerprint(
    effects: RevisionExecplanInputFingerprintEffects, repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    entries = []
    for relative_path in effects._execplan_input_paths(plan):
        contents = revision_document_or_none(effects.git, repo_path, revision, relative_path)
        entries.append((relative_path, contents))
    return effects._execplan_input_fingerprint(entries)


@dataclass(frozen=True)
class RevisionExecplanSourceFingerprintEffects:
    """Named effects for _revision_execplan_source_fingerprint; no entry-point/module access."""
    _execplan_input_fingerprint: Callable[..., object]
    git: Callable[..., object]


def _revision_execplan_source_fingerprint(
    effects: RevisionExecplanSourceFingerprintEffects, repo_path: Path,
    revision: str,
    plan: sqlite3.Row,
) -> str:
    """Fingerprint only current source documents; the plan body is classified separately."""

    entries = []
    for relative_path in json.loads(str(plan["sources"])):
        if relative_path == "manager:verify-inputs":
            from loop_manager_internal.catalog.manager_sources import source_content
            entries.append((relative_path, source_content(relative_path)))
            continue
        contents = revision_document_or_none(effects.git, repo_path, revision, relative_path)
        if contents is None and relative_path != "docs/PRODUCT.md":
            archive_path = execplan_catalog.requirement_archive_path(
                str(relative_path)
            )
            contents = revision_document_or_none(effects.git, repo_path, revision, archive_path)
        entries.append((str(relative_path), contents))
    return effects._execplan_input_fingerprint(entries)


@dataclass(frozen=True)
class RevisionExecplanBodyHashEffects:
    """Named effects for _revision_execplan_body_hash; no entry-point/module access."""
    git: Callable[..., object]


def _revision_execplan_body_hash(
    effects: RevisionExecplanBodyHashEffects, repo_path: Path,
    revision: str,
    path: str,
) -> str:
    contents = revision_document_or_none(effects.git, repo_path, revision, path)
    if contents is None:
        raise RuntimeError(f"최신 ExecPlan 본문을 읽을 수 없습니다: {path}")
    return execution_revision.execplan_body_hash(contents)


@dataclass(frozen=True)
class RevisionChangedPathsEffects:
    """Named effects for _revision_changed_paths; no entry-point/module access."""
    git: Callable[..., object]


def _revision_changed_paths(
    effects: RevisionChangedPathsEffects, repo_path: Path,
    previous_revision: str,
    current_revision: str,
) -> tuple[str, ...]:
    if not previous_revision or previous_revision == current_revision:
        return ()
    changed = effects.git(
        repo_path,
        "diff",
        "--name-only",
        "--find-renames",
        previous_revision,
        current_revision,
        "--",
        check=False,
    )
    if changed.returncode:
        raise RuntimeError("실행 base revision 차이를 확인할 수 없습니다.")
    return tuple(
        sorted(
            {
                line.strip()
                for line in changed.stdout.splitlines()
                if line.strip()
            }
        )
    )


def unmet_execplan_dependencies(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[str, ...]:
    """Return every direct or indirect dependency not confirmed MERGED."""

    rows = connection.execute(
        "SELECT plan_id, depends_on, status FROM exec_plans WHERE repo = ?",
        (repo,),
    ).fetchall()
    graph = {
        str(row["plan_id"]): (
            tuple(str(item) for item in json.loads(str(row["depends_on"]))),
            str(row["status"]),
        )
        for row in rows
    }
    if plan_id not in graph:
        raise RuntimeError(f"dependency를 확인할 ExecPlan이 없습니다: {plan_id}")
    from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
    held = catalog_held_plan_ids(connection, repo)
    unmet: set[str] = {plan_id} if plan_id in held else set()
    visited: set[str] = set()

    def visit(current: str) -> None:
        if current in visited:
            return
        visited.add(current)
        dependencies, _status = graph[current]
        for dependency in dependencies:
            stored = graph.get(dependency)
            if dependency in held or stored is None or stored[1] != "MERGED":
                unmet.add(dependency)
            if stored is not None:
                visit(dependency)

    visit(plan_id)
    return tuple(sorted(unmet))


@dataclass(frozen=True)
class RevisionReviewIssueFingerprintEffects:
    """Named effects for _revision_review_issue_fingerprint; no entry-point/module access."""
    _execplan_input_fingerprint: Callable[..., object]
    _review_issue_paths: Callable[..., object]
    git: Callable[..., object]


def _revision_review_issue_fingerprint(
    effects: RevisionReviewIssueFingerprintEffects, repo_path: Path,
    revision: str,
    review_result: object,
) -> str | None:
    paths = effects._review_issue_paths(review_result)
    if not paths:
        return None
    entries = []
    for relative_path in paths:
        contents = revision_document_or_none(effects.git, repo_path, revision, relative_path)
        entries.append((relative_path, contents))
    return effects._execplan_input_fingerprint(entries)


def _blocked_validation_base_revision(
    connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
) -> str:
    project = connection.execute(
        "SELECT active_sha FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    return str(
        (project["active_sha"] if project else None)
        or plan["active_sha"]
        or plan["current_revision_sha"]
        or plan["plan_sha"]
        or ""
    )


@dataclass(frozen=True)
class BlockedValidationInputFingerprintsEffects:
    """Named effects for _blocked_validation_input_fingerprints; no entry-point/module access."""
    _blocked_validation_base_revision: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    git: Callable[..., object]


def _blocked_validation_input_fingerprints(
    effects: BlockedValidationInputFingerprintsEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
) -> tuple[str, str]:
    """Capture comparable base and worktree inputs at the blocking boundary."""

    worktree_fingerprint = effects._worktree_execplan_input_fingerprint(worktree, plan)
    revision = effects._blocked_validation_base_revision(connection, repo, plan)
    if not revision:
        return worktree_fingerprint, worktree_fingerprint
    resolved = effects.git(
        repo_path,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
        check=False,
    )
    if resolved.returncode:
        return worktree_fingerprint, worktree_fingerprint
    return (
        effects._revision_execplan_input_fingerprint(repo_path, revision, plan),
        worktree_fingerprint,
    )


@dataclass(frozen=True)
class ExecplanMetadataIdentityErrorEffects:
    """Named effects for _execplan_metadata_identity_error; no entry-point/module access."""
    _current_execplan_metadata: Callable[..., object]


def _execplan_metadata_identity_error(
    effects: ExecplanMetadataIdentityErrorEffects, repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
    subject: str,
) -> str | None:
    del repo_path
    if current["id"] != plan["plan_id"]:
        return f"{subject}의 ID가 reconciled catalog identity와 다릅니다."
    reconciled = effects._current_execplan_metadata(plan, current)
    metadata_fields = (
        "title",
        "sources",
        "depends_on",
        "complexity",
        "risk",
        "priority",
    )
    if any(current[field] != reconciled[field] for field in metadata_fields):
        return f"{subject} metadata가 reconciled catalog와 다릅니다."
    return None


def _current_execplan_metadata(
    plan: sqlite3.Row,
    fallback: Mapping[str, object],
) -> dict[str, object]:
    """Expose the reconciled declaration cache without publication-audit fields."""

    complexity = plan["current_complexity"] or fallback.get("complexity")
    risk = plan["current_risk"] or fallback.get("risk")
    return {
        "id": str(plan["plan_id"]),
        "title": str(plan["title"]),
        "sources": list(json.loads(str(plan["sources"]))),
        "depends_on": list(json.loads(str(plan["depends_on"]))),
        "complexity": str(complexity),
        "risk": str(risk),
        "priority": str(plan["priority"]),
    }


@dataclass(frozen=True)
class BlockedExecplanMetadataIdentityErrorEffects:
    """Named effects for _blocked_execplan_metadata_identity_error; no entry-point/module access."""
    _execplan_metadata_identity_error: Callable[..., object]


def _blocked_execplan_metadata_identity_error(
    effects: BlockedExecplanMetadataIdentityErrorEffects, repo_path: Path,
    current: dict[str, object],
    plan: sqlite3.Row,
) -> str | None:
    return effects._execplan_metadata_identity_error(
        repo_path,
        current,
        plan,
        "BLOCKED ExecPlan",
    )


@dataclass(frozen=True)
class ValidateActiveExecplanMetadataEffects:
    """Named effects for validate_active_execplan_metadata; no entry-point/module access."""
    _current_execplan_metadata: Callable[..., object]
    _execplan_metadata_identity_error: Callable[..., object]
    validate_execplan: Callable[..., object]


def validate_active_execplan_metadata(
    effects: ValidateActiveExecplanMetadataEffects, repo_path: Path,
    worktree: Path,
    relative_path: str,
    plan: sqlite3.Row,
    *,
    rebase_definition: execution_revision.DefinitionSnapshot | None = None,
    rebase_target_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    try:
        current = effects.validate_execplan(worktree, relative_path)
        if current.get("draft"):
            raise ExecPlanMetadataError("사용자 판단 대기 draft ExecPlan은 실행할 수 없습니다.")
    except (ExternalObservationError, GitLockError):
        raise
    except OSError:
        raise
    except RuntimeError as error:
        raise ExecPlanMetadataError(str(error)) from error
    if rebase_target_metadata is not None:
        # Only the checkpoint-bound rebase service supplies this boundary.
        # Compare whole declarations, never a mixture of old and new fields.
        # Normal execution and final publication still compare with catalog.
        if (rebase_definition is None or rebase_definition.path != relative_path
                or relative_path != str(plan['path'])
                or current['id'] != plan['plan_id']
                or rebase_target_metadata['id'] != plan['plan_id']
                or rebase_target_metadata.get('draft')):
            raise ExecPlanMetadataError('소유된 rebase ExecPlan identity가 다릅니다.')
        fields = ('title', 'sources', 'depends_on', 'complexity', 'risk', 'priority')
        applied = {field: getattr(rebase_definition, field) for field in fields}
        for field in ('sources', 'depends_on'):
            applied[field] = list(applied[field])
        if not any(all(current[field] == declaration[field] for field in fields)
                   for declaration in (applied, rebase_target_metadata)):
            raise ExecPlanMetadataError(
                '소유된 rebase ExecPlan metadata가 적용된 선언 또는 target base와 다릅니다.')
        return current
    comparison_plan = plan
    if rebase_definition is not None:
        # A paused rebase still contains its applied declaration. Scheduling,
        # profile and dependency fields may lag during conflict resolution;
        # all other identity fields and all normal validation remain strict.
        if rebase_definition.path != relative_path:
            raise ExecPlanMetadataError("rebase 기준 ExecPlan 경로가 현재 작업 경로와 다릅니다.")
        comparison_plan = dict(plan)
        for field, column in (
            ('priority', 'priority'),
            ('complexity', 'current_complexity'),
            ('risk', 'current_risk'),
        ):
            applied = getattr(rebase_definition, field)
            if current[field] == applied:
                comparison_plan[column] = applied
        if tuple(current['depends_on']) == rebase_definition.depends_on:
            comparison_plan['depends_on'] = json.dumps(rebase_definition.depends_on)
    error = effects._execplan_metadata_identity_error(
        repo_path,
        current,
        comparison_plan,
        "활성 ExecPlan",
    )
    if error is not None:
        raise ExecPlanMetadataError(error)
    if relative_path != str(plan["path"]):
        raise ExecPlanMetadataError("활성 ExecPlan 경로가 reconciled catalog와 다릅니다.")
    return effects._current_execplan_metadata(plan, current)
