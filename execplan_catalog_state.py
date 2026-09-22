"""Durable archived ExecPlan observations and recovery decisions.

This module owns historical catalog audit data.  It intentionally does not
write execution phases or the ``projects`` compatibility projection; callers
apply confirmed recovery decisions through the runtime-state boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import sqlite3
from typing import Mapping, Protocol

from execplan_catalog import ExecPlanCatalogEntry, ExecPlanCatalogSnapshot


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


@dataclass(frozen=True)
class StoredPlanIdentity:
    plan_id: str
    path: str
    content_hash: str
    plan_sha: str | None
    status: str | None = None


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
        """Promote observations only when publication evidence verifies identity."""

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
                final_content_hash = excluded.final_content_hash,
                plan_sha = CASE
                    WHEN execplan_catalog_history.identity_verified = 0
                     AND excluded.identity_verified = 1
                    THEN excluded.plan_sha
                    ELSE COALESCE(execplan_catalog_history.plan_sha, excluded.plan_sha)
                END,
                merge_confirmed = excluded.merge_confirmed,
                identity_verified = MAX(
                    execplan_catalog_history.identity_verified,
                    excluded.identity_verified
                ),
                currently_archived = 1,
                proof_kind = excluded.proof_kind,
                proof_revision = excluded.proof_revision,
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
            trusted_identity = identity is not None and audit is None
            if identity is not None:
                original_path = identity.path
                original_hash = identity.content_hash
                stable_plan_sha = identity.plan_sha
            elif audit is not None:
                original_path = str(audit["original_path"])
                original_hash = str(audit["original_content_hash"])
                stable_plan_sha = (
                    str(audit["plan_sha"]) if audit["plan_sha"] else None
                )
                trusted_identity = bool(audit["identity_verified"])
            else:
                original_path = entry.active_path
                original_hash = entry.content_hash
                stable_plan_sha = None
            proof = MergeProof(False)
            diagnostic: CatalogDiagnostic | None = None

            if original_path != entry.active_path:
                diagnostic = self._diagnostic(
                    entry,
                    "stable-identity-mismatch",
                    "archived ExecPlan의 최초 active 경로가 저장 identity와 다릅니다: "
                    f"{original_path} != {entry.active_path}",
                )
            elif trusted_identity and original_hash != entry.content_hash:
                diagnostic = self._diagnostic(
                    entry,
                    "archived-content-hash-mismatch",
                    "archived ExecPlan byte hash가 저장된 원본 content hash와 다릅니다: "
                    f"{entry.path}",
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
                else:
                    stable_plan_sha = stable_plan_sha or proof.plan_sha
                    if not proof.merged:
                        diagnostic = self._diagnostic(
                            entry,
                            "merge-proof-missing",
                            "archive 파일 위치만으로 MERGED를 확인할 수 없습니다: "
                            f"{entry.path}",
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
                        if (
                            proof.publication_content_hash != original_hash
                            or proof.publication_content_hash
                            != entry.content_hash
                        ):
                            diagnostic = self._diagnostic(
                                entry,
                                "archived-content-hash-mismatch",
                                "archived ExecPlan byte hash가 게시 revision의 "
                                "본문 hash와 다릅니다: "
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
