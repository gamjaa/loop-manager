"""catalog requirement archive service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.storage.project_state_adapter import KNOWN_EXECPLAN_STATUSES
from loop_manager_internal.application.contracts import RequirementArchiveCollisionError
from loop_manager_internal.application.contracts import RequirementArchiveError
from loop_manager_internal.catalog import requirement_archive
from loop_manager_internal.git import command_adapter
from loop_manager_internal.runtime.contracts import GitLockError
from loop_manager_internal.storage import execplan_catalog_state
from pathlib import Path
import json
import sqlite3


@dataclass(frozen=True)
class PublishedExecplanEffects:
    """Named effects for published_execplan; no entry-point/module access."""
    parse_execplan_text: Callable[..., object]


def published_execplan(effects: PublishedExecplanEffects, plan_path: str, plan_text: str) -> dict[str, object]:
    """Read metadata from the immutable ExecPlan commit without live source checks."""
    return effects.parse_execplan_text(plan_path, plan_text, validate_sources=False)


@dataclass(frozen=True)
class RequirementArchiveTreeEffects:
    """Named effects for _requirement_archive_tree; no entry-point/module access."""
    _git_tree_entries: Callable[..., object]


def _requirement_archive_tree(
    effects: RequirementArchiveTreeEffects, repo_path: Path,
    revision: str,
) -> dict[bytes, tuple[bytes, bytes]]:
    try:
        return effects._git_tree_entries(repo_path, revision)
    except GitLockError:
        raise
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Git revision의 Requirement/ExecPlan 이력을 확인할 수 없습니다: "
            f"{revision} ({error})"
        ) from error


def _stored_requirement_sources(rows: list[sqlite3.Row]) -> tuple[str, ...]:
    try:
        return requirement_archive.stored_requirement_sources(rows)
    except requirement_archive.ArchiveSnapshotError as error:
        raise RequirementArchiveError(str(error)) from error


@dataclass(frozen=True)
class RequirementArchiveSourcesEffects:
    """Named effects for requirement_archive_sources; no entry-point/module access."""
    _stored_requirement_sources: Callable[..., object]


def requirement_archive_sources(
    effects: RequirementArchiveSourcesEffects, connection: sqlite3.Connection,
    repo: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT plan_id, sources FROM exec_plans WHERE repo = ?",
        (repo,),
    ).fetchall()
    return effects._stored_requirement_sources(rows)


@dataclass(frozen=True)
class LaterPublishedExecplanEffects:
    """Named effects for _later_published_execplan; no entry-point/module access."""
    git: Callable[..., object]
    git_bytes: Callable[..., object]
    published_execplan: Callable[..., object]


def _later_published_execplan(
    effects: LaterPublishedExecplanEffects, repo_path: Path,
    revision: str,
    row: sqlite3.Row,
) -> dict[str, object] | None:
    plan_id = str(row["plan_id"])
    plan_sha = str(row["publication_sha"] or row["plan_sha"] or "")
    if not plan_sha:
        return None
    ancestry = effects.git(
        repo_path,
        "merge-base",
        "--is-ancestor",
        revision,
        plan_sha,
        check=False,
    )
    if ancestry.returncode == 1:
        return None
    if ancestry.returncode != 0:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: "
            f"{ancestry.stdout[-2000:]}"
        )
    path = str(row["publication_path"] or row["path"])
    try:
        plan_text = effects.git_bytes(
            repo_path,
            "show",
            f"{plan_sha}:{path}",
        ).decode("utf-8")
        plan = effects.published_execplan(path, plan_text)
    except (UnicodeDecodeError, RuntimeError) as error:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: {error}"
        ) from error
    if plan["id"] != plan_id:
        raise RequirementArchiveError(
            f"ExecPlan {plan_id}의 게시 이력을 확정할 수 없습니다: ID 불일치"
        )
    return plan


@dataclass(frozen=True)
class RevisionExecplanCatalogEffects:
    """Named effects for _revision_execplan_catalog; no entry-point/module access."""
    revision_execplan_snapshot: Callable[..., object]


def _revision_execplan_catalog(
    effects: RevisionExecplanCatalogEffects, repo_path: Path,
    revision: str,
    *,
    archive_conflict_plan_ids: frozenset[str] = frozenset(),
    satisfied_dependencies: frozenset[str] = frozenset(),
) -> dict[str, dict[str, object]]:
    try:
        snapshot = effects.revision_execplan_snapshot(
            repo_path,
            revision,
            archive_conflict_plan_ids=archive_conflict_plan_ids,
            satisfied_dependencies=satisfied_dependencies,
        )
    except GitLockError:
        raise
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Git revision의 ExecPlan source 관계를 확정할 수 없습니다: {error}"
        ) from error
    return {
        plan_id: {
            **entry.metadata(),
            "path": entry.path,
            "content_hash": entry.content_hash,
        }
        for plan_id, entry in snapshot.all.items()
    }


@dataclass(frozen=True)
class RequirementArchiveSnapshotEffects:
    """Named effects for requirement_archive_snapshot; no entry-point/module access."""
    _is_active_requirement: Callable[..., object]
    _later_published_execplan: Callable[..., object]
    _requirement_archive_tree: Callable[..., object]
    _revision_execplan_catalog: Callable[..., object]
    _validate_dependency_graph: Callable[..., object]
    git_bytes: Callable[..., bytes]


def requirement_archive_snapshot(
    effects: RequirementArchiveSnapshotEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    current_plan_id: str,
    *,
    allow_future_publication: bool = False,
) -> requirement_archive.ArchiveSnapshot:
    """Read candidate scope from one immutable Git and SQLite view."""

    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
    ).fetchall()
    states = {str(row["plan_id"]): row for row in rows}
    if current_plan_id not in states:
        raise RequirementArchiveError(
            f"현재 ExecPlan의 SQLite 상태 없음: {current_plan_id}"
        )
    for plan_id, row in states.items():
        if str(row["status"]) not in KNOWN_EXECPLAN_STATUSES:
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 source 관계 또는 상태를 확정할 수 없습니다."
            )
    verified_history = execplan_catalog_state.verified_historical_merge_ids(connection, repo)
    for plan_id in verified_history - set(states):
        states[plan_id] = {"plan_id": plan_id, "status": "MERGED"}
    catalog = effects._revision_execplan_catalog(
        repo_path,
        revision,
        archive_conflict_plan_ids=frozenset(
            plan_id for plan_id, row in states.items()
            if str(row["status"]) == "MERGED"
        ),
        satisfied_dependencies=verified_history,
    )
    for plan_id in verified_history - set(catalog):
        audit = connection.execute(
            "SELECT observed_path FROM execplan_catalog_history WHERE repo=? AND plan_id=?",
            (repo, plan_id),
        ).fetchone()
        catalog[plan_id] = {
            "id": plan_id, "title": plan_id, "path": str(audit[0]),
            "sources": [], "depends_on": [], "complexity": None, "risk": None,
            "priority": "normal", "content_hash": "",
        }
    historical_rows = connection.execute(
        "SELECT plan_id, merge_confirmed, diagnostic_code FROM execplan_catalog_history "
        "WHERE repo = ? AND currently_archived = 1",
        (repo,),
    ).fetchall()
    unverified_historical_ids = {
        str(row["plan_id"])
        for row in historical_rows
        if not int(row["merge_confirmed"]) or row["diagnostic_code"]
    }
    state_ids = set(states)
    if allow_future_publication:
        for plan_id in sorted(state_ids - set(catalog)):
            later_plan = effects._later_published_execplan(
                repo_path,
                revision,
                states[plan_id],
            )
            if later_plan is not None:
                catalog[plan_id] = later_plan
    try:
        effects._validate_dependency_graph(catalog)
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"ExecPlan source 관계를 확정할 수 없습니다: {error}"
        ) from error

    catalog_ids = set(catalog)
    missing_states = catalog_ids - state_ids
    unexpected_missing_states = missing_states - unverified_historical_ids
    if unexpected_missing_states or state_ids - catalog_ids:
        missing_plans = sorted(state_ids - catalog_ids)
        details = []
        if unexpected_missing_states:
            details.append(
                "SQLite 상태 없음: "
                + ", ".join(sorted(unexpected_missing_states))
            )
        if missing_plans:
            details.append("Git ExecPlan 없음: " + ", ".join(missing_plans))
        raise RequirementArchiveError(
            "ExecPlan source 관계 또는 상태를 확정할 수 없습니다 ("
            + "; ".join(details)
            + ")."
        )

    tree = effects._requirement_archive_tree(repo_path, revision)
    try:
        requirement_documents = command_adapter.revision_requirement_documents(
            effects.git_bytes, repo_path, revision,
        )
    except GitLockError:
        raise
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Git revision Requirement source를 읽을 수 없습니다: {error}"
        ) from error
    try:
        snapshot = requirement_archive.calculate(
            base_oid=revision,
            catalog=catalog,
            states={plan_id: str(row["status"]) for plan_id, row in states.items()},
            active_requirements=tuple(
                path.decode("utf-8") for path in tree if effects._is_active_requirement(path)
            ),
            archived_requirements=frozenset(
                path.decode("utf-8") for path in tree
                if path.startswith(b"docs/requirements/archive/")
            ),
            current_plan_id=current_plan_id,
            unverified_historical_ids=frozenset(unverified_historical_ids),
            known_statuses=frozenset(KNOWN_EXECPLAN_STATUSES),
            requirement_documents=requirement_documents,
            cache_versions={
                plan_id: tuple(
                    row[field] if field in row.keys() else None
                    for field in (
                        "path", "title", "sources", "depends_on",
                        "current_complexity", "current_risk", "priority",
                        "content_hash", "current_revision_sha",
                    )
                )
                for plan_id, row in states.items()
            },
        )
    except requirement_archive.ArchiveCollisionError as error:
        raise RequirementArchiveCollisionError(error.path) from error
    except requirement_archive.ArchiveSnapshotError as error:
        raise RequirementArchiveError(str(error)) from error
    return snapshot


@dataclass(frozen=True)
class RequirementArchiveCandidatesEffects:
    """Named effects for requirement_archive_candidates; no entry-point/module access."""
    requirement_archive_snapshot: Callable[..., object]
    requirement_archive_sources: Callable[..., object]


def requirement_archive_candidates(
    effects: RequirementArchiveCandidatesEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    revision: str,
    current_plan_id: str,
) -> tuple[str, ...]:
    """Compatibility projection of a revision-pinned archive snapshot."""

    states = {
        str(row["plan_id"]): str(row["status"])
        for row in connection.execute(
            "SELECT plan_id, status FROM exec_plans WHERE repo = ?", (repo,)
        )
    }
    if current_plan_id not in states:
        raise RequirementArchiveError(
            f"현재 ExecPlan의 SQLite 상태 없음: {current_plan_id}"
        )
    for plan_id, status in states.items():
        if status not in KNOWN_EXECPLAN_STATUSES:
            raise RequirementArchiveError(
                f"ExecPlan {plan_id}의 source 관계 또는 상태를 확정할 수 없습니다."
            )
    if not effects.requirement_archive_sources(connection, repo):
        return ()
    return effects.requirement_archive_snapshot(
        connection, repo_path, repo, revision, current_plan_id,
        allow_future_publication=True,
    ).candidates


@dataclass(frozen=True)
class RequirementArchiveTouchedSourcesEffects:
    """Named effects for requirement_archive_touched_sources; no entry-point/module access."""
    _is_active_requirement: Callable[..., object]
    _requirement_archive_tree: Callable[..., object]
    requirement_archive_path: Callable[..., object]


def requirement_archive_touched_sources(
    effects: RequirementArchiveTouchedSourcesEffects, repo_path: Path,
    revision: str,
    worktree: Path,
) -> tuple[str, ...]:
    """Find active Requirement paths whose move pair differs from the base tree."""

    tree = effects._requirement_archive_tree(repo_path, revision)
    touched = []
    for path in sorted(tree):
        if not effects._is_active_requirement(path):
            continue
        source = path.decode("utf-8")
        archived = effects.requirement_archive_path(source)
        if archived.encode("utf-8") in tree:
            continue
        if not (worktree / source).is_file() or (worktree / archived).exists():
            touched.append(source)
    return tuple(touched)


@dataclass(frozen=True)
class RequirementArchiveBlobEffects:
    """Named effects for _requirement_archive_blob; no entry-point/module access."""
    git_bytes: Callable[..., object]


def _requirement_archive_blob(
    effects: RequirementArchiveBlobEffects, repo: Path,
    revision: str,
    archived: str,
) -> bytes:
    try:
        output = effects.git_bytes(
            repo,
            "ls-tree",
            "-z",
            revision,
            "--",
            archived,
        )
    except RuntimeError as error:
        raise RequirementArchiveError(
            f"Requirement archive의 Git tree 상태를 확인할 수 없습니다: "
            f"{archived} ({error})"
        ) from error
    records = [record for record in output.split(b"\0") if record]
    expected_path = archived.encode("utf-8")
    if len(records) == 1:
        metadata, separator, actual_path = records[0].partition(b"\t")
        parts = metadata.split()
    else:
        separator = b""
        actual_path = b""
        parts = []
    if (
        not separator
        or actual_path != expected_path
        or len(parts) != 3
        or parts[0] not in {b"100644", b"100755"}
        or parts[1] != b"blob"
    ):
        raise RequirementArchiveError(
            f"Requirement archive가 {revision}에 일반 blob으로 추적되지 않았습니다: "
            f"{archived}"
        )
    return parts[2]


@dataclass(frozen=True)
class RequirementArchiveMoveSetEffects:
    """Named effects for requirement_archive_move_set; no entry-point/module access."""
    _requirement_archive_blob: Callable[..., object]
    _requirement_archive_tree: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., object]


def requirement_archive_move_set(
    effects: RequirementArchiveMoveSetEffects, repo_path: Path,
    revision: str,
    worktree: Path,
    *,
    tracked_revision: str | None = None,
) -> tuple[str, ...]:
    """Return content-preserving Requirement moves relative to one base revision."""

    tree = effects._requirement_archive_tree(repo_path, revision)
    def ignore_status(path: str) -> tuple[int, str]:
        result = effects.git(
            worktree, "check-ignore", "--quiet", "--", path, check=False
        )
        return result.returncode, result.stdout

    def tracked_contents(path: str) -> bytes:
        object_id = effects._requirement_archive_blob(worktree, tracked_revision, path)
        return effects.git_bytes(worktree, "cat-file", "blob", object_id.decode("ascii"))

    try:
        return requirement_archive.validated_moves(
            tree, worktree,
            blob_contents=lambda object_id: effects.git_bytes(
                repo_path, "cat-file", "blob", object_id.decode("ascii")
            ),
            tracked_contents=tracked_contents if tracked_revision is not None else None,
            ignore_status=ignore_status if tracked_revision is None else None,
        )
    except requirement_archive.ArchiveCollisionError as error:
        raise RequirementArchiveCollisionError(error.path) from error
    except requirement_archive.ArchiveMoveError as error:
        raise RequirementArchiveError(str(error)) from error


def requirement_archive_reconciliation_error(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
    *,
    revision: str | None = None,
) -> str | None:
    if expected == actual:
        return None
    return (
        "Requirement archive 이동 집합이 최신 eligibility와 다릅니다.\n"
        + (f"기준 revision: {revision}\n" if revision else "")
        + f"기대: {json.dumps(expected, ensure_ascii=False)}\n"
        f"현재: {json.dumps(actual, ensure_ascii=False)}\n"
        "기대 집합만 내용 변경 없이 docs/requirements/archive/로 이동하라."
    )
