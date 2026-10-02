"""Durable archived ExecPlan observations and recovery decisions.

This module owns historical catalog audit data.  It intentionally does not
write execution phases or the ``projects`` compatibility projection; callers
apply confirmed recovery decisions through the runtime-state boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import re
import sqlite3
from typing import Callable, Iterator, Mapping, Protocol
from uuid import uuid4

from loop_manager_internal.catalog.execplan_catalog import (
    ExecPlanCatalogEntry,
    ExecPlanCatalogSnapshot,
    final_content_matches,
    parse_execplan_text,
)


HISTORICAL_CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_catalog_history (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    original_path TEXT NOT NULL,
    observed_path TEXT NOT NULL,
    original_content_hash TEXT NOT NULL,
    final_content_hash TEXT NOT NULL,
    plan_sha TEXT,
    merge_confirmed INTEGER NOT NULL DEFAULT 0,
    identity_verified INTEGER NOT NULL DEFAULT 0,
    currently_archived INTEGER NOT NULL DEFAULT 1,
    proof_kind TEXT,
    proof_revision TEXT,
    pr_url TEXT,
    diagnostic_code TEXT,
    diagnostic_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id)
)
"""

CATALOG_DIAGNOSTICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_catalog_diagnostics (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    observed_path TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id, code)
)
"""

CATALOG_REVISIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_catalog_revisions (
    repo TEXT PRIMARY KEY,
    revision_sha TEXT NOT NULL,
    catalog_fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class MergeProofKind(str, Enum):
    GITHUB_PR = "github-pr"
    GIT_ANCESTRY = "git-ancestry"
    STORED_AND_GIT = "stored-and-git"
    NONE = "none"


@dataclass(frozen=True)
class MergeProof:
    merged: bool
    kind: MergeProofKind = MergeProofKind.NONE
    plan_sha: str | None = None
    publication_content_hash: str | None = None
    revision: str | None = None
    pr_url: str | None = None
    detail: str | None = None


class MergeProofPort(Protocol):
    """External Git/GitHub evidence supplied to the pure recovery service."""

    def proof_for(self, entry: ExecPlanCatalogEntry) -> MergeProof: ...

    def verify_stored(
        self, entry: ExecPlanCatalogEntry, identity: StoredPlanIdentity,
    ) -> MergeProof: ...


@dataclass(frozen=True)
class StoredPlanIdentity:
    plan_id: str
    path: str
    content_hash: str
    plan_sha: str | None
    status: str | None = None
    current_content_hash: str | None = None


@dataclass(frozen=True)
class CatalogDiagnostic:
    plan_id: str
    observed_path: str
    code: str
    message: str


@dataclass(frozen=True)
class RecoveredHistoricalPlan:
    entry: ExecPlanCatalogEntry
    original_path: str
    original_content_hash: str
    plan_sha: str
    proof: MergeProof


@dataclass(frozen=True)
class CatalogRecoveryResult:
    merged: tuple[RecoveredHistoricalPlan, ...]
    diagnostics: tuple[CatalogDiagnostic, ...]


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(HISTORICAL_CATALOG_SCHEMA)
    connection.execute(CATALOG_DIAGNOSTICS_SCHEMA)
    connection.execute(CATALOG_REVISIONS_SCHEMA)
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(execplan_catalog_history)"
        )
    }
    if "identity_verified" not in columns:
        connection.execute(
            "ALTER TABLE execplan_catalog_history "
            "ADD COLUMN identity_verified INTEGER NOT NULL DEFAULT 0"
        )
    if "currently_archived" not in columns:
        connection.execute(
            "ALTER TABLE execplan_catalog_history "
            "ADD COLUMN currently_archived INTEGER NOT NULL DEFAULT 1"
        )


class ReconciliationKind(str, Enum):
    RECONCILED = "reconciled"
    STALE = "stale"
    INVALID_CATALOG = "invalid-catalog"


@dataclass(frozen=True)
class PublicationAudit:
    plan_id: str
    path: str
    content_hash: str
    revision_sha: str | None


@dataclass(frozen=True)
class CurrentPlanDefinition:
    plan_id: str
    path: str
    title: str
    sources: tuple[str, ...]
    depends_on: tuple[str, ...]
    complexity: str | None
    risk: str | None
    priority: str
    content_hash: str
    revision_sha: str | None

    def metadata(self) -> dict[str, object]:
        return {
            "id": self.plan_id,
            "title": self.title,
            "sources": list(self.sources),
            "depends_on": list(self.depends_on),
            "complexity": self.complexity,
            "risk": self.risk,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class CatalogRowVersion:
    """Declaration plus state/ownership values protected by reconciliation CAS."""

    plan_id: str
    values: tuple[object, ...]


@dataclass(frozen=True)
class CatalogStateSnapshot:
    rows: tuple[CatalogRowVersion, ...]
    project_values: tuple[object, ...] | None
    reserved: tuple[tuple[str, str], ...] = ()

    @property
    def plan_ids(self) -> frozenset[str]:
        return frozenset(row.plan_id for row in self.rows)


@dataclass(frozen=True)
class PinnedCatalog:
    revision_sha: str
    snapshot: ExecPlanCatalogSnapshot
    regular_paths: frozenset[str] | None = None


@dataclass(frozen=True)
class ReconciliationDiagnostic:
    revision_sha: str
    code: str
    message: str
    path: str | None
    source: str | None
    fingerprint: str
    related_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogReconciliationResult:
    kind: ReconciliationKind
    revision_sha: str | None = None
    changed_plan_ids: tuple[str, ...] = ()
    diagnostic: ReconciliationDiagnostic | None = None


class InvalidPinnedCatalog(RuntimeError):
    """Typed failure returned by a revision-pinned catalog input port."""

    def __init__(self, diagnostic: ReconciliationDiagnostic):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


class CatalogDefinitionPort(Protocol):
    def load(self) -> PinnedCatalog: ...

    def current_revision(self) -> str: ...


_ROW_VERSION_FIELDS = (
    "plan_id",
    "path",
    "title",
    "sources",
    "depends_on",
    "priority",
    "content_hash",
    "plan_sha",
    "publication_path",
    "publication_content_hash",
    "publication_sha",
    "current_complexity",
    "current_risk",
    "current_revision_sha",
    "catalog_generation",
    "status",
    "ever_started",
    "retry_count",
    "verify_failure_history",
    "review_failure_history",
    "initial_worker_level",
    "effective_worker_level",
    "review_level_history",
    "blocked_reason",
    "guidance_plan_hash",
    "blocked_fingerprint",
    "blocked_worktree_fingerprint",
    "blocked_review_fingerprint",
    "worker_session_id",
    "worker_session_binding",
    "worker_session_transition",
    "reviewer_session_id",
    "worker_result",
    "verify_result",
    "review_result",
    "definition_snapshot",
    "worker_input_token",
    "archive_token",
    "archive_resume_status",
    "verify_token",
    "review_token",
    "publication_token",
    "revision_resume_status",
    "branch",
    "worktree",
    "workspace_owned",
    "pr_url",
    "last_error",
    "pr_head_oid",
    "publication_remote_oid",
    "pr_conflict_recovery",
    "pr_conflict_head_oid",
    "pr_conflict_base_oid",
    "agent_role",
    "agent_snapshot",
    "agent_input_fingerprint",
    "agent_baseline",
    "agent_recovery",
    "agent_input_plan",
    "agent_result_plan",
    "agent_result_snapshot",
    "agent_manager_snapshot",
    "active_sha",
    "validated_base_sha",
)

_PROJECT_VERSION_FIELDS = (
    "phase",
    "plan_path",
    "branch",
    "worktree",
    "worker_session_id",
    "reviewer_session_id",
    "worker_result",
    "verify_result",
    "review_result",
    "definition_snapshot",
    "worker_input_token",
    "archive_token",
    "verify_token",
    "review_token",
    "publication_token",
    "revision_resume_status",
    "pr_url",
    "pr_head_oid",
    "publication_remote_oid",
    "pr_conflict_recovery",
    "pr_conflict_head_oid",
    "pr_conflict_base_oid",
    "agent_role",
    "agent_snapshot",
    "agent_input_fingerprint",
    "agent_baseline",
    "agent_recovery",
    "agent_input_plan",
    "agent_result_plan",
    "agent_result_snapshot",
    "agent_manager_snapshot",
    "active_sha",
    "validated_base_sha",
)


def _decode_list(value: object, field: str, plan_id: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"ExecPlan {plan_id}의 {field} cache가 유효하지 않습니다."
        ) from error
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise RuntimeError(
            f"ExecPlan {plan_id}의 {field} cache가 유효하지 않습니다."
        )
    return tuple(parsed)


def _worker_level(complexity: str, risk: str) -> int:
    return min(
        {"low": 1, "medium": 3, "high": 5}[complexity]
        + {"low": 0, "medium": 1, "high": 2, "critical": 3}[risk],
        7,
    )


def insert_published_plan_row(
    connection: sqlite3.Connection,
    repo: str,
    path: str,
    plan: Mapping[str, object],
    content_hash: str,
    revision_sha: str,
) -> None:
    """Register validated publication metadata without reading a worktree."""

    sources, dependencies, priority = serialize_plan_metadata(plan)
    initial_level = _worker_level(str(plan["complexity"]), str(plan["risk"]))
    connection.execute(
        """
        INSERT INTO exec_plans (
            repo, plan_id, path, title, sources, depends_on, priority,
            content_hash, current_complexity, current_risk,
            current_revision_sha, publication_path,
            publication_content_hash, publication_sha,
            status, plan_sha, initial_worker_level, effective_worker_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, ?, ?)
        """,
        (
            repo, str(plan["id"]), path, str(plan["title"]), sources,
            dependencies, priority, content_hash,
            str(plan["complexity"]), str(plan["risk"]), revision_sha,
            path, content_hash, revision_sha, revision_sha,
            initial_level, initial_level,
        ),
    )


def _catalog_fingerprint(snapshot: ExecPlanCatalogSnapshot) -> str:
    digest = hashlib.sha256(b"loop-manager-execplan-catalog-v1\0")
    for plan_id, entry in snapshot.all.items():
        for value in (plan_id, entry.path, entry.content_hash):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def diagnostic_fingerprint(
    revision_sha: str,
    code: str,
    message: str,
    paths: tuple[str, ...],
) -> str:
    digest = hashlib.sha256(b"loop-manager-invalid-catalog-v1\0")
    for value in (revision_sha, code, message, *sorted(paths)):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return
    savepoint = f"catalog_reconciliation_{uuid4().hex}"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {savepoint}")
        connection.execute(f"RELEASE {savepoint}")
        raise
    else:
        connection.execute(f"RELEASE {savepoint}")


class _StaleCatalogSnapshot(RuntimeError):
    pass


class DefinitionStateRepository:
    """Own immutable publication audit and the mutable declaration cache."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ):
        self.connection = connection
        self._fault_hook = fault_hook

    def _fault(self, stage: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage)

    def capture(self, repo: str) -> CatalogStateSnapshot:
        fields = ", ".join(_ROW_VERSION_FIELDS)
        rows = self.connection.execute(
            f"SELECT {fields} FROM exec_plans WHERE repo = ? ORDER BY plan_id",
            (repo,),
        ).fetchall()
        project = self.connection.execute(
            "SELECT " + ", ".join(_PROJECT_VERSION_FIELDS)
            + " FROM projects WHERE repo = ?",
            (repo,),
        ).fetchone()
        reserved = self.connection.execute(
            "SELECT plan_id, original_path AS path FROM execplan_catalog_history "
            "WHERE repo = ? UNION SELECT plan_id, path FROM "
            "execplan_cancellations WHERE repo = ? ORDER BY plan_id, path",
            (repo, repo),
        ).fetchall()
        reserved_values = {(str(row[0]), str(row[1])) for row in reserved}
        available_tables = {
            str(row[0]) for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        # Some durable audit tables can outlive their execution row after an
        # interrupted migration. An ID linked to any such history stays owned.
        for table, path_column in (
            ("execplan_archive_items", "active_path"),
            ("execution_checkpoints", None),
            ("blocked_checkpoints", None),
            ("rebase_corrections", None),
            ("agent_invocations", None),
            ("validation_results", None),
            ("validation_feedback_events", None),
            ("validation_feedback_current", None),
            ("validation_delivery_cursors", None),
            ("validation_recovery_sessions", None),
            ("validation_delivery_attempts", None),
            ("validation_feedback_migrations", None),
            ("guidance_approvals", None),
            ("requirement_archive_targets", None),
            ("execplan_blocks", None),
            ("project_blocks", None),
        ):
            if table not in available_tables:
                continue
            selected_path = path_column or "''"
            reserved_values.update(
                (str(row[0]), str(row[1]))
                for row in self.connection.execute(
                    f"SELECT DISTINCT plan_id, {selected_path} FROM {table} "
                    "WHERE repo = ? AND plan_id IS NOT NULL",
                    (repo,),
                )
            )
        if "project_completion_cycles" in available_tables:
            reserved_values.update(
                (str(row[0]), "") for row in self.connection.execute(
                    "SELECT last_completed_plan_id FROM project_completion_cycles "
                    "WHERE repo = ? AND last_completed_plan_id IS NOT NULL",
                    (repo,),
                )
            )
        return CatalogStateSnapshot(
            rows=tuple(
                CatalogRowVersion(
                    str(row["plan_id"]),
                    tuple(row[field] for field in _ROW_VERSION_FIELDS),
                )
                for row in rows
            ),
            project_values=(
                tuple(project[field] for field in _PROJECT_VERSION_FIELDS)
                if project is not None
                else None
            ),
            reserved=tuple(sorted(reserved_values)),
        )

    def has_publication_intent(self, repo: str) -> bool:
        general = self.connection.execute(
            "SELECT 1 FROM general_planning_publications WHERE repo = ?",
            (repo,),
        ).fetchone()
        if general is not None:
            return True
        if self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'maintenance_cycles'"
        ).fetchone() is None:
            return False
        maintenance = self.connection.execute(
            "SELECT 1 FROM maintenance_cycles WHERE repo = ? "
            "AND publication_stage = 'prepared'",
            (repo,),
        ).fetchone()
        return maintenance is not None

    def publication_audit(self, repo: str, plan_id: str) -> PublicationAudit:
        row = self.connection.execute(
            "SELECT plan_id, status, path, content_hash, plan_sha, publication_path, "
            "publication_content_hash, publication_sha "
            "FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"ExecPlan publication audit가 없습니다: {plan_id}")
        identity = publication_identity(row)
        return PublicationAudit(
            identity.plan_id, identity.path, identity.content_hash, identity.plan_sha,
        )

    def current_definition(
        self, repo: str, plan_id: str
    ) -> CurrentPlanDefinition:
        row = self.connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"ExecPlan current definition이 없습니다: {plan_id}")
        return CurrentPlanDefinition(
            plan_id=plan_id,
            path=str(row["path"]),
            title=str(row["title"]),
            sources=_decode_list(row["sources"], "sources", plan_id),
            depends_on=_decode_list(row["depends_on"], "depends_on", plan_id),
            complexity=(
                str(row["current_complexity"])
                if row["current_complexity"] is not None
                else None
            ),
            risk=(
                str(row["current_risk"])
                if row["current_risk"] is not None
                else None
            ),
            priority=str(row["priority"]),
            content_hash=str(row["content_hash"]),
            revision_sha=(
                str(row["current_revision_sha"])
                if row["current_revision_sha"]
                else None
            ),
        )

    def historical_ids(self, repo: str) -> frozenset[str]:
        return frozenset(
            str(row[0])
            for row in self.connection.execute(
                "SELECT plan_id FROM execplan_catalog_history WHERE repo = ?",
                (repo,),
            )
        )

    @staticmethod
    def _has_execution_history(row: sqlite3.Row) -> bool:
        return bool(
            str(row["status"]) != "READY"
            or int(row["retry_count"] or 0)
            or row["worker_session_id"]
            or row["reviewer_session_id"]
            or row["branch"]
            or row["worktree"]
            or row["pr_url"]
            or str(row["verify_failure_history"] or "{}") != "{}"
            or str(row["review_failure_history"] or "{}") != "{}"
            or str(row["review_level_history"] or "{}") != "{}"
        )

    def apply(
        self,
        repo: str,
        pinned: PinnedCatalog,
        expected: CatalogStateSnapshot,
        *,
        revision_is_current: Callable[[], bool],
        new_plan_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...] | None:
        """CAS all declaration deltas, rolling back on either stale boundary."""

        changed: list[str] = []
        try:
            with _transaction(self.connection):
                if (self.capture(repo) != expected or self.has_publication_intent(repo)
                        or not revision_is_current()):
                    raise _StaleCatalogSnapshot
                project = self.connection.execute(
                    "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
                ).fetchone()
                projected_path = str(project["plan_path"] or "") if project else ""
                for plan_id, entry in pinned.snapshot.all.items():
                    row = self.connection.execute(
                        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                        (repo, plan_id),
                    ).fetchone()
                    if row is None:
                        if plan_id in new_plan_ids:
                            insert_published_plan_row(
                                self.connection, repo, entry.path,
                                entry.metadata(), entry.content_hash,
                                pinned.revision_sha,
                            )
                            changed.append(plan_id)
                            self._fault(f"after-plan:{plan_id}")
                        continue
                    sources = json.dumps(
                        entry.sources, ensure_ascii=False, separators=(",", ":")
                    )
                    dependencies = json.dumps(
                        entry.depends_on,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    initial_level = _worker_level(entry.complexity, entry.risk)
                    old_initial = row["initial_worker_level"]
                    old_effective = row["effective_worker_level"]
                    if self._has_execution_history(row):
                        effective_level = max(
                            initial_level,
                            int(old_effective or old_initial or initial_level),
                        )
                    else:
                        effective_level = initial_level
                    declaration = (
                        str(row["path"]),
                        str(row["title"]),
                        str(row["sources"]),
                        str(row["depends_on"]),
                        str(row["priority"]),
                        str(row["content_hash"]),
                        row["current_complexity"],
                        row["current_risk"],
                        row["current_revision_sha"],
                        row["initial_worker_level"],
                        row["effective_worker_level"],
                    )
                    target = (
                        entry.path,
                        entry.title,
                        sources,
                        dependencies,
                        entry.priority,
                        entry.content_hash,
                        entry.complexity,
                        entry.risk,
                        pinned.revision_sha,
                        initial_level,
                        effective_level,
                    )
                    if declaration == target:
                        continue
                    self.connection.execute(
                        """
                        UPDATE exec_plans
                        SET path = ?, title = ?, sources = ?, depends_on = ?,
                            priority = ?, content_hash = ?,
                            current_complexity = ?, current_risk = ?,
                            current_revision_sha = ?, initial_worker_level = ?,
                            effective_worker_level = ?,
                            catalog_generation = catalog_generation + 1
                        WHERE repo = ? AND plan_id = ?
                        """,
                        (*target, repo, plan_id),
                    )
                    if projected_path == str(row["path"]):
                        self.connection.execute(
                            "UPDATE projects SET plan_path = ? WHERE repo = ?",
                            (entry.path, repo),
                        )
                        projected_path = entry.path
                    changed.append(plan_id)
                    self._fault(f"after-plan:{plan_id}")
                self.connection.execute(
                    """
                    INSERT INTO execplan_catalog_revisions (
                        repo, revision_sha, catalog_fingerprint
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(repo) DO UPDATE SET
                        revision_sha = excluded.revision_sha,
                        catalog_fingerprint = excluded.catalog_fingerprint,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        repo,
                        pinned.revision_sha,
                        _catalog_fingerprint(pinned.snapshot),
                    ),
                )
                self._fault("before-revision-recheck")
                if not revision_is_current():
                    raise _StaleCatalogSnapshot
        except _StaleCatalogSnapshot:
            return None
        return tuple(changed)


class CatalogReconciliationService:
    """Validate identity mapping and converge a pinned catalog atomically."""

    def __init__(
        self,
        repository: DefinitionStateRepository,
        catalog: CatalogDefinitionPort,
    ):
        self.repository = repository
        self.catalog = catalog

    @staticmethod
    def _invalid(
        revision_sha: str,
        code: str,
        message: str,
        *,
        path: str | None = None,
        source: str | None = None,
        related_paths: tuple[str, ...] = (),
    ) -> CatalogReconciliationResult:
        paths = tuple(
            item
            for item in (path, source, *related_paths)
            if item is not None
        )
        diagnostic = ReconciliationDiagnostic(
            revision_sha=revision_sha,
            code=code,
            message=message,
            path=path,
            source=source,
            related_paths=related_paths,
            fingerprint=diagnostic_fingerprint(
                revision_sha, code, message, paths
            ),
        )
        return CatalogReconciliationResult(
            ReconciliationKind.INVALID_CATALOG,
            revision_sha=revision_sha,
            diagnostic=diagnostic,
        )

    def reconcile(self, repo: str) -> CatalogReconciliationResult:
        # The owning Planner must recover its manifest before unrelated IDs
        # can be classified as user publications.
        if self.repository.has_publication_intent(repo):
            return CatalogReconciliationResult(ReconciliationKind.STALE)
        try:
            pinned = self.catalog.load()
        except InvalidPinnedCatalog as error:
            return CatalogReconciliationResult(
                ReconciliationKind.INVALID_CATALOG,
                revision_sha=error.diagnostic.revision_sha,
                diagnostic=error.diagnostic,
            )
        expected = self.repository.capture(repo)
        stored_ids = expected.plan_ids
        catalog_ids = frozenset(pinned.snapshot.all)
        missing_files = sorted(stored_ids - catalog_ids)
        historical_ids = self.repository.historical_ids(repo)
        missing_history = sorted(
            historical_ids - stored_ids - catalog_ids
        )
        unknown_active = sorted(set(pinned.snapshot.active) - stored_ids)
        unknown_archived = sorted(
            set(pinned.snapshot.archived) - stored_ids - historical_ids
        )
        unknown = [*unknown_active, *unknown_archived]
        if missing_files and unknown:
            old = self.repository.current_definition(repo, missing_files[0])
            entry = pinned.snapshot.all[unknown[0]]
            return self._invalid(
                pinned.revision_sha,
                "ambiguous-id-change",
                "원격 ExecPlan ID 변경을 기존 실행 이력과 하나로 대응할 수 "
                f"없습니다: {missing_files[0]} -> {unknown[0]}",
                path=entry.path,
                related_paths=(old.path,),
            )
        if missing_history and unknown:
            entry = pinned.snapshot.all[unknown[0]]
            old_path = next(
                path for reserved_id, path in expected.reserved
                if reserved_id == missing_history[0]
            )
            return self._invalid(
                pinned.revision_sha, "ambiguous-id-change",
                "원격 archive ID 누락과 신규 ExecPlan을 동시에 관측했습니다: "
                f"{missing_history[0]} -> {unknown[0]}",
                path=entry.path, related_paths=(old_path,),
            )
        if missing_files:
            plan_id = missing_files[0]
            current = self.repository.current_definition(repo, plan_id)
            return self._invalid(
                pinned.revision_sha,
                "missing-plan",
                f"SQLite ExecPlan에 대응하는 원격 파일이 없습니다: {plan_id}",
                path=current.path,
            )
        if missing_history:
            plan_id = missing_history[0]
            return self._invalid(
                pinned.revision_sha, "missing-plan",
                f"SQLite archive 이력에 대응하는 원격 파일이 없습니다: {plan_id}",
                path=next(
                    path for reserved_id, path in expected.reserved
                    if reserved_id == plan_id
                ),
            )
        if unknown_archived:
            plan_id = unknown_archived[0]
            entry = pinned.snapshot.archived[plan_id]
            return self._invalid(
                pinned.revision_sha, "unknown-plan-id",
                f"신규 archive ExecPlan은 자동 등록할 수 없습니다: {plan_id}",
                path=entry.path,
            )
        if unknown_active:
            if pinned.regular_paths is None:
                entry = pinned.snapshot.active[unknown_active[0]]
                return self._invalid(
                    pinned.revision_sha, "unknown-plan-id",
                    "신규 ExecPlan source를 검증할 원격 tree가 없습니다: "
                    f"{entry.plan_id}", path=entry.path,
                )
            all_reserved = (
                {(row.plan_id, str(row.values[1])) for row in expected.rows}
                | set(expected.reserved)
            )
            max_number = max(
                (
                    int(plan_id[2:]) for plan_id, _ in all_reserved
                    if plan_id.startswith("G-") and plan_id[2:].isdigit()
                ), default=0
            )
            reserved_paths = {path for _, path in all_reserved}
            for offset, plan_id in enumerate(
                sorted(unknown_active, key=lambda value: int(value[2:]))
            ):
                entry = pinned.snapshot.active[plan_id]
                number = int(plan_id[2:])
                if number != max_number + offset + 1 or entry.path in reserved_paths:
                    return self._invalid(
                        pinned.revision_sha, "unknown-plan-id",
                        "신규 ExecPlan ID가 저장된 최대 번호 다음부터 연속되지 "
                        f"않거나 경로가 예약 이력과 충돌합니다: {plan_id}",
                        path=entry.path,
                    )
                try:
                    parse_execplan_text(
                        entry.path, entry.text, require_priority=True,
                        validate_sources=False,
                    )
                except RuntimeError as error:
                    return self._invalid(
                        pinned.revision_sha, "invalid-plan",
                        f"신규 ExecPlan 형식이 유효하지 않습니다: {error}",
                        path=entry.path,
                    )
                for source in entry.sources:
                    if source not in pinned.regular_paths:
                        return self._invalid(
                            pinned.revision_sha, "missing-source",
                            f"신규 ExecPlan의 활성 source가 없습니다: {source}",
                            path=entry.path, source=source,
                        )
        if self.catalog.current_revision() != pinned.revision_sha:
            return CatalogReconciliationResult(
                ReconciliationKind.STALE, revision_sha=pinned.revision_sha
            )
        changed = self.repository.apply(
            repo,
            pinned,
            expected,
            revision_is_current=(
                lambda: self.catalog.current_revision() == pinned.revision_sha
            ),
            new_plan_ids=tuple(unknown_active),
        )
        if changed is None:
            return CatalogReconciliationResult(
                ReconciliationKind.STALE, revision_sha=pinned.revision_sha
            )
        return CatalogReconciliationResult(
            ReconciliationKind.RECONCILED,
            revision_sha=pinned.revision_sha,
            changed_plan_ids=changed,
        )


class CatalogStateRepository:
    """SQLite repository for observations, hashes, and typed diagnostics."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        ensure: bool = True,
    ):
        self.connection = connection
        if ensure:
            ensure_schema(connection)

    def record(
        self,
        repo: str,
        entry: ExecPlanCatalogEntry,
        *,
        original_path: str,
        original_content_hash: str,
        plan_sha: str | None,
        proof: MergeProof,
        diagnostic: CatalogDiagnostic | None,
        identity_verified: bool,
    ) -> None:
        """Retain confirmed merge evidence while recording current diagnostics.

        merge_confirmed is durable; archive consumers must also require no
        diagnostic before using that confirmation to satisfy dependencies.
        """

        self.connection.execute(
            """
            INSERT INTO execplan_catalog_history (
                repo, plan_id, original_path, observed_path,
                original_content_hash, final_content_hash, plan_sha,
                merge_confirmed, identity_verified, currently_archived,
                proof_kind, proof_revision, pr_url,
                diagnostic_code, diagnostic_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, plan_id) DO UPDATE SET
                original_path = CASE
                    WHEN execplan_catalog_history.identity_verified = 0
                     AND excluded.identity_verified = 1
                    THEN excluded.original_path
                    ELSE execplan_catalog_history.original_path
                END,
                original_content_hash = CASE
                    WHEN execplan_catalog_history.identity_verified = 0
                     AND excluded.identity_verified = 1
                    THEN excluded.original_content_hash
                    ELSE execplan_catalog_history.original_content_hash
                END,
                observed_path = excluded.observed_path,
                final_content_hash = CASE
                    WHEN execplan_catalog_history.merge_confirmed = 1
                     AND excluded.diagnostic_code IS NOT NULL
                    THEN execplan_catalog_history.final_content_hash
                    ELSE excluded.final_content_hash
                END,
                plan_sha = CASE
                    WHEN execplan_catalog_history.identity_verified = 0
                     AND excluded.identity_verified = 1
                    THEN excluded.plan_sha
                    ELSE COALESCE(execplan_catalog_history.plan_sha, excluded.plan_sha)
                END,
                merge_confirmed = MAX(
                    execplan_catalog_history.merge_confirmed,
                    excluded.merge_confirmed
                ),
                identity_verified = MAX(
                    execplan_catalog_history.identity_verified,
                    excluded.identity_verified
                ),
                currently_archived = 1,
                proof_kind = CASE
                    WHEN execplan_catalog_history.merge_confirmed = 1
                     AND excluded.diagnostic_code IS NOT NULL
                    THEN execplan_catalog_history.proof_kind
                    ELSE excluded.proof_kind
                END,
                proof_revision = CASE
                    WHEN execplan_catalog_history.merge_confirmed = 1
                     AND excluded.diagnostic_code IS NOT NULL
                    THEN execplan_catalog_history.proof_revision
                    ELSE excluded.proof_revision
                END,
                pr_url = COALESCE(execplan_catalog_history.pr_url, excluded.pr_url),
                diagnostic_code = excluded.diagnostic_code,
                diagnostic_message = excluded.diagnostic_message,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                repo,
                entry.plan_id,
                original_path,
                entry.path,
                original_content_hash,
                entry.content_hash,
                plan_sha,
                1 if proof.merged and diagnostic is None else 0,
                1 if identity_verified else 0,
                proof.kind.value,
                proof.revision,
                proof.pr_url,
                diagnostic.code if diagnostic else None,
                diagnostic.message if diagnostic else None,
            ),
        )
        self.connection.execute(
            "DELETE FROM execplan_catalog_diagnostics "
            "WHERE repo = ? AND plan_id = ?",
            (repo, entry.plan_id),
        )
        if diagnostic is not None:
            self.connection.execute(
                """
                INSERT INTO execplan_catalog_diagnostics (
                    repo, plan_id, observed_path, code, message
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo, plan_id, code) DO UPDATE SET
                    observed_path = excluded.observed_path,
                    message = excluded.message,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    repo,
                    diagnostic.plan_id,
                    diagnostic.observed_path,
                    diagnostic.code,
                    diagnostic.message,
                ),
            )

    def historical_row(self, repo: str, plan_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM execplan_catalog_history WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()

    def diagnostics(self, repo: str) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                "SELECT d.* FROM execplan_catalog_diagnostics d "
                "JOIN execplan_catalog_history h "
                "ON h.repo = d.repo AND h.plan_id = d.plan_id "
                "WHERE d.repo = ? AND h.currently_archived = 1 "
                "ORDER BY d.plan_id, d.code",
                (repo,),
            ).fetchall()
        )

    def mark_active(
        self,
        repo: str,
        entries: Mapping[str, ExecPlanCatalogEntry],
    ) -> None:
        """Preserve historical audit while ending current archive exclusion."""

        for entry in entries.values():
            self.connection.execute(
                """
                UPDATE execplan_catalog_history
                SET observed_path = ?, currently_archived = 0,
                    diagnostic_code = NULL, diagnostic_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND plan_id = ?
                """,
                (entry.path, repo, entry.plan_id),
            )
            self.connection.execute(
                "DELETE FROM execplan_catalog_diagnostics "
                "WHERE repo = ? AND plan_id = ?",
                (repo, entry.plan_id),
            )


class CatalogRecoveryService:
    """Compare archived files with stable identity and injected merge proof."""

    def __init__(
        self,
        repository: CatalogStateRepository,
        merge_proof: MergeProofPort,
    ):
        self.repository = repository
        self.merge_proof = merge_proof

    @staticmethod
    def _diagnostic(
        entry: ExecPlanCatalogEntry,
        code: str,
        message: str,
    ) -> CatalogDiagnostic:
        return CatalogDiagnostic(entry.plan_id, entry.path, code, message)

    @staticmethod
    def _verified_history_proof(
        entry: ExecPlanCatalogEntry,
        audit: sqlite3.Row | None,
    ) -> MergeProof | None:
        """Return only a complete, identity-bound durable merge observation."""

        if audit is None or not (
            audit["merge_confirmed"]
            and audit["identity_verified"]
            and audit["plan_id"] == entry.plan_id
            and re.fullmatch(
                rf"docs/plans/\[{re.escape(entry.plan_id)}\] [^/]+\.md",
                str(audit["original_path"]),
            )
            and audit["original_content_hash"]
            and audit["final_content_hash"]
            and audit["plan_sha"]
            and audit["proof_revision"]
        ):
            return None
        try:
            kind = MergeProofKind(str(audit["proof_kind"]))
        except ValueError:
            return None
        if kind is MergeProofKind.NONE:
            return None
        return MergeProof(
            True,
            kind=kind,
            plan_sha=str(audit["plan_sha"]),
            publication_content_hash=str(audit["original_content_hash"]),
            revision=str(audit["proof_revision"]),
            pr_url=str(audit["pr_url"] or "") or None,
        )

    @staticmethod
    def _stored_identity_conflicts_with_history(
        identity: StoredPlanIdentity,
        audit: sqlite3.Row,
    ) -> bool:
        return any(
            stored and stored != recorded
            for stored, recorded in (
                (identity.path, str(audit["original_path"])),
                (identity.content_hash, str(audit["original_content_hash"])),
                (identity.plan_sha, str(audit["plan_sha"] or "") or None),
            )
        )

    def reconcile(
        self,
        repo: str,
        snapshot: ExecPlanCatalogSnapshot,
        stored: Mapping[str, StoredPlanIdentity],
    ) -> CatalogRecoveryResult:
        merged: list[RecoveredHistoricalPlan] = []
        diagnostics: list[CatalogDiagnostic] = []

        for plan_id, entry in snapshot.archived.items():
            identity = stored.get(plan_id)
            audit = self.repository.historical_row(repo, plan_id)
            history_proof = self._verified_history_proof(entry, audit)
            trusted_identity = bool(
                identity is not None
                or (audit is not None and audit["identity_verified"])
            )
            if identity is not None:
                original_path = identity.path
                original_hash = identity.content_hash
                current_hash = (
                    identity.current_content_hash or identity.content_hash
                )
                stable_plan_sha = identity.plan_sha
            elif audit is not None:
                original_path = str(audit["original_path"])
                original_hash = str(audit["original_content_hash"])
                # An audit-only row has no synchronized declaration cache.
                # Diagnostics must retain the last verified archive bytes
                # and durable merge evidence for subsequent recovery.
                current_hash = (
                    str(audit["final_content_hash"])
                    if history_proof is not None
                    else original_hash
                )
                stable_plan_sha = (
                    str(audit["plan_sha"]) if audit["plan_sha"] else None
                )
                trusted_identity = bool(audit["identity_verified"])
            else:
                original_path = entry.active_path
                original_hash = entry.content_hash
                current_hash = entry.content_hash
                stable_plan_sha = None
            proof = MergeProof(False)
            diagnostic: CatalogDiagnostic | None = None

            if (
                audit is not None
                and audit["identity_verified"]
                and identity is not None
                and self._stored_identity_conflicts_with_history(identity, audit)
            ):
                diagnostic = self._diagnostic(
                    entry,
                    "stable-identity-mismatch",
                    "archived ExecPlan의 SQLite identity와 검증된 archive 이력이 다릅니다: "
                    f"{entry.plan_id}",
                )
            elif not trusted_identity and original_path != entry.active_path:
                diagnostic = self._diagnostic(
                    entry,
                    "stable-identity-mismatch",
                    "archived ExecPlan의 최초 active 경로가 저장 identity와 다릅니다: "
                    f"{original_path} != {entry.active_path}",
                )
            elif audit is not None and audit["merge_confirmed"] and history_proof is None:
                diagnostic = self._diagnostic(
                    entry, "publication-proof-missing",
                    "확정된 Merge 이력의 게시 identity 또는 증명이 불완전합니다: "
                    f"{entry.plan_id}",
                )
            elif history_proof is not None and (
                identity is None or identity.status == "MERGED"
            ):
                try:
                    proof = self.merge_proof.verify_stored(
                        entry,
                        StoredPlanIdentity(
                            plan_id, original_path, original_hash,
                            stable_plan_sha, "MERGED", current_hash,
                        ),
                    )
                    proof = replace(proof, pr_url=history_proof.pr_url)
                except Exception as error:
                    diagnostic = self._diagnostic(
                        entry, "merge-proof-error",
                        f"archived ExecPlan 로컬 Git 검증에 실패했습니다: {error}",
                    )
            else:
                try:
                    proof = self.merge_proof.proof_for(entry)
                except Exception as error:  # external port errors become data, not state
                    diagnostic = self._diagnostic(
                        entry,
                        "merge-proof-error",
                        f"archived ExecPlan Merge 이력 조회에 실패했습니다: {error}",
                    )
            if diagnostic is None:
                stable_plan_sha = stable_plan_sha or proof.plan_sha
                if not proof.merged:
                    diagnostic = self._diagnostic(
                        entry,
                        "merge-proof-missing",
                        "archive 파일 위치만으로 MERGED를 확인할 수 없습니다: "
                        f"{entry.path}" + (f" ({proof.detail})" if proof.detail else ""),
                    )
                elif not stable_plan_sha:
                    diagnostic = self._diagnostic(
                        entry,
                        "publication-proof-missing",
                        "archived ExecPlan의 최초 게시 revision을 확인할 수 없습니다: "
                        f"{entry.path}",
                    )
                elif not proof.publication_content_hash:
                    diagnostic = self._diagnostic(
                        entry,
                        "publication-content-proof-missing",
                        "archived ExecPlan의 게시 본문 hash를 확인할 수 없습니다: "
                        f"{entry.path}",
                    )
                else:
                    if not trusted_identity:
                        original_hash = proof.publication_content_hash
                    publication_mismatch = (
                        proof.publication_content_hash != original_hash
                    )
                    current_mismatch = not final_content_matches(
                        entry.content_hash,
                        current_hash if trusted_identity
                        else proof.publication_content_hash,
                    )
                    if publication_mismatch or current_mismatch:
                        diagnostic = self._diagnostic(
                            entry,
                            "archived-content-hash-mismatch",
                            "archived ExecPlan byte hash가 게시 revision 또는 "
                            "마지막 동기화 본문 hash와 다릅니다: "
                            f"{entry.path}",
                        )

            self.repository.record(
                repo,
                entry,
                original_path=original_path,
                original_content_hash=original_hash,
                plan_sha=stable_plan_sha,
                proof=proof,
                diagnostic=diagnostic,
                identity_verified=bool(
                    trusted_identity
                    or (
                        proof.merged
                        and stable_plan_sha
                        and proof.publication_content_hash
                    )
                ),
            )
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                continue
            assert stable_plan_sha is not None
            merged.append(
                RecoveredHistoricalPlan(
                    entry=entry,
                    original_path=original_path,
                    original_content_hash=original_hash,
                    plan_sha=stable_plan_sha,
                    proof=proof,
                )
            )

        return CatalogRecoveryResult(tuple(merged), tuple(diagnostics))


def serialize_plan_metadata(plan: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        json.dumps(plan["sources"], ensure_ascii=False, separators=(",", ":")),
        json.dumps(plan["depends_on"], ensure_ascii=False, separators=(",", ":")),
        str(plan["priority"]),
    )


def publication_identity(row: sqlite3.Row) -> StoredPlanIdentity:
    """Interpret the immutable audit fallback separately from current cache."""
    return StoredPlanIdentity(
        plan_id=str(row["plan_id"]),
        path=str(row["publication_path"] or row["path"]),
        content_hash=str(row["publication_content_hash"] or row["content_hash"]),
        plan_sha=(str(row["publication_sha"] or row["plan_sha"])
                  if row["publication_sha"] or row["plan_sha"] else None),
        status=str(row["status"]),
        current_content_hash=str(row["content_hash"]),
    )


def _merged_execplan_ids(
    connection: sqlite3.Connection, repo: str
) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT plan_id FROM exec_plans WHERE repo = ? AND status = 'MERGED'",
            (repo,),
        )
    )


def register_published_plan_row(
    connection: sqlite3.Connection, repo: str, path: str,
    plan: Mapping[str, object], content_hash: str, plan_commit: str,
) -> None:
    """Apply a validated publication row without committing the caller event."""
    plan_id = str(plan["id"])
    sources, dependencies, priority = serialize_plan_metadata(plan)
    initial_level = worker_level(plan)
    historical = connection.execute(
        "SELECT plan_id FROM execplan_catalog_history "
        "WHERE repo = ? AND (plan_id = ? OR original_path = ?)",
        (repo, plan_id, path),
    ).fetchone()
    if historical is not None:
        raise RuntimeError(
            "게시된 ExecPlan이 archived historical identity와 충돌합니다: "
            f"{plan_id}"
        )
    existing = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND (plan_id = ? OR path = ?)",
        (repo, plan_id, path),
    ).fetchone()
    if existing:
        immutable = (
            existing["plan_id"], existing["path"], existing["title"],
            existing["sources"], existing["depends_on"], existing["priority"],
            existing["content_hash"], existing["plan_sha"],
        )
        expected = (
            plan_id, path, str(plan["title"]), sources, dependencies,
            priority, content_hash, plan_commit,
        )
        if immutable != expected:
            raise RuntimeError(f"게시된 ExecPlan 등록 정보가 충돌합니다: {plan_id}")
        if existing["initial_worker_level"] is None:
            connection.execute(
                "UPDATE exec_plans SET initial_worker_level = ?, "
                "effective_worker_level = ? WHERE repo = ? AND plan_id = ?",
                (initial_level, initial_level, repo, plan_id),
            )
        return
    insert_published_plan_row(
        connection, repo, path, plan, content_hash, plan_commit,
    )


def worker_level(plan: Mapping[str, object]) -> int:
    """Keep the public metadata validation while sharing the score calculation."""
    try:
        return _worker_level(plan["complexity"], plan["risk"])
    except (KeyError, TypeError) as error:
        raise ValueError("검증된 ExecPlan complexity와 risk가 필요합니다.") from error
