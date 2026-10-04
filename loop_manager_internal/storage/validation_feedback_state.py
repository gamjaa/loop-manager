"""Immutable validation audit and durable Worker feedback delivery cursors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from uuid import uuid4

from loop_manager_internal.execution.validation_feedback import Feedback, bounded_diagnostic, normalize_feedback
from loop_manager_internal.storage.runtime_state import _transaction


def ensure_schema(connection: sqlite3.Connection) -> None:
    schema = """
        CREATE TABLE IF NOT EXISTS validation_results (
            id INTEGER PRIMARY KEY,
            repo TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            definition_revision TEXT,
            stage TEXT NOT NULL,
            classification TEXT NOT NULL,
            raw_output TEXT NOT NULL,
            identities TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS validation_feedback_events (
            sequence INTEGER PRIMARY KEY,
            result_id INTEGER NOT NULL REFERENCES validation_results(id),
            repo TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            identity TEXT NOT NULL,
            occurrence INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            diagnostic TEXT NOT NULL,
            active INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS validation_feedback_current (
            repo TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            identity TEXT NOT NULL,
            occurrence INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            diagnostic TEXT NOT NULL,
            active INTEGER NOT NULL,
            sequence INTEGER NOT NULL,
            PRIMARY KEY (repo, plan_id, identity)
        );
        CREATE TABLE IF NOT EXISTS validation_delivery_cursors (
            repo TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            PRIMARY KEY (repo, plan_id, session_id)
        );
        CREATE TABLE IF NOT EXISTS validation_recovery_sessions (
            repo TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            required INTEGER NOT NULL,
            PRIMARY KEY (repo, plan_id, session_id)
        );
        CREATE TABLE IF NOT EXISTS validation_delivery_attempts (
            id TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            session_id TEXT,
            sequence INTEGER NOT NULL,
            recovery INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK (status IN ('pending', 'delivered')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS validation_feedback_migrations (
            repo TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            PRIMARY KEY (repo, plan_id)
        );
        CREATE TABLE IF NOT EXISTS validation_delivery_items (
            attempt_id TEXT NOT NULL REFERENCES validation_delivery_attempts(id),
            sequence INTEGER NOT NULL REFERENCES validation_feedback_events(sequence),
            PRIMARY KEY (attempt_id, sequence)
        );
    """
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)
    columns = {row[1] for row in connection.execute(
        "PRAGMA table_info(validation_delivery_attempts)"
    )}
    if "recovery" not in columns:
        connection.execute(
            "ALTER TABLE validation_delivery_attempts "
            "ADD COLUMN recovery INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS validation_feedback_events_identity "
        "ON validation_feedback_events(repo,plan_id,identity,sequence)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS validation_delivery_attempts_cursor "
        "ON validation_delivery_attempts(repo,plan_id,session_id,status,sequence)"
    )
    for table in ("validation_results", "validation_feedback_events"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}_immutable "
                f"BEFORE {action} ON {table} BEGIN "
                "SELECT RAISE(ABORT, 'validation audit is immutable'); END"
            )


@dataclass(frozen=True)
class Delivery:
    attempt_id: str
    rows: tuple[dict[str, object], ...]
    recovery: bool


class ValidationFeedbackRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def _result(self, repo: str, plan_id: str, revision: str | None,
                stage: str, classification: str, raw: str,
                feedback: tuple[Feedback, ...]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.connection.execute(
            "INSERT INTO validation_results "
            "(repo,plan_id,definition_revision,stage,classification,raw_output,identities,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (repo, plan_id, revision, stage, classification, raw,
             json.dumps([item.identity for item in feedback]), now),
        )
        result_id = cursor.lastrowid
        assert result_id is not None
        return result_id

    def record_result(self, repo: str, plan_id: str, revision: str | None,
                      stage: str, classification: str, raw: str,
                      feedback: tuple[Feedback, ...] = ()) -> None:
        """Audit an invalid or failed Agent result without changing feedback."""
        with _transaction(self.connection):
            self._mark_migrated(repo, plan_id)
            self._result(repo, plan_id, revision, stage, classification, raw, feedback)

    def _mark_migrated(self, repo: str, plan_id: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO validation_feedback_migrations (repo,plan_id) VALUES (?,?)",
            (repo, plan_id),
        )

    def observe(self, repo: str, plan_id: str, revision: str | None,
                stage: str, passed: bool, raw: str,
                feedback: tuple[Feedback, ...] = ()) -> None:
        """Append an execution and replace only the materialized active view."""
        if stage not in {"verify", "review"}:
            raise ValueError(stage)
        with _transaction(self.connection):
            self._mark_migrated(repo, plan_id)
            result_id = self._result(repo, plan_id, revision, stage,
                                     "success" if passed else "failure", raw, feedback)
            identities = {item.identity for item in feedback}
            previous = self.connection.execute(
                "SELECT * FROM validation_feedback_current "
                "WHERE repo=? AND plan_id=? AND stage=? AND active=1",
                (repo, plan_id, stage),
            ).fetchall()
            for row in previous:
                if row["identity"] not in identities:
                    self._event(result_id, repo, plan_id, stage, row["identity"],
                                int(row["occurrence"]), int(row["revision"]) + 1,
                                str(row["diagnostic"]), False)
            for item in feedback:
                row = self.connection.execute(
                    "SELECT * FROM validation_feedback_current "
                    "WHERE repo=? AND plan_id=? AND identity=?",
                    (repo, plan_id, item.identity),
                ).fetchone()
                self._event(result_id, repo, plan_id, stage, item.identity,
                            int(row["occurrence"]) + 1 if row else 1,
                            int(row["revision"]) + 1 if row else 1,
                            item.diagnostic, True)

    def _event(self, result_id: int, repo: str, plan_id: str, stage: str,
               identity: str, occurrence: int, revision: int,
               diagnostic: str, active: bool) -> None:
        cursor = self.connection.execute(
            "INSERT INTO validation_feedback_events "
            "(result_id,repo,plan_id,stage,identity,occurrence,revision,diagnostic,active) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (result_id, repo, plan_id, stage, identity, occurrence,
             revision, diagnostic, int(active)),
        )
        self.connection.execute(
            "INSERT INTO validation_feedback_current "
            "(repo,plan_id,stage,identity,occurrence,revision,diagnostic,active,sequence) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(repo,plan_id,identity) DO UPDATE SET "
            "occurrence=excluded.occurrence,revision=excluded.revision,"
            "diagnostic=excluded.diagnostic,active=excluded.active,sequence=excluded.sequence",
            (repo, plan_id, stage, identity, occurrence, revision,
             diagnostic, int(active), cursor.lastrowid),
        )

    def prepare(self, repo: str, plan_id: str, session_id: str | None,
                *, recovery: bool = False) -> Delivery:
        """Persist an invocation snapshot before a provider can receive it."""
        with _transaction(self.connection):
            interrupted_recovery = bool(session_id and self.connection.execute(
                "SELECT 1 FROM validation_recovery_sessions "
                "WHERE repo=? AND plan_id=? AND session_id=? AND required=1",
                (repo, plan_id, session_id),
            ).fetchone())
            recovery = recovery or interrupted_recovery
            cursor = self.connection.execute(
                "SELECT sequence FROM validation_delivery_cursors "
                "WHERE repo=? AND plan_id=? AND session_id=?",
                (repo, plan_id, session_id),
            ).fetchone() if session_id and not recovery else None
            since = int(cursor["sequence"]) if cursor else 0
            rows = self.connection.execute(
                "SELECT stage,identity,occurrence,revision,diagnostic,sequence "
                "FROM validation_feedback_current WHERE repo=? AND plan_id=? "
                "AND active=1 AND sequence>? ORDER BY stage,identity",
                (repo, plan_id, since),
            ).fetchall()
            rendered = []
            for row in rows:
                prior = self.connection.execute(
                    "SELECT e.diagnostic FROM validation_feedback_events e "
                    "JOIN validation_delivery_items i ON i.sequence=e.sequence "
                    "JOIN validation_delivery_attempts a ON a.id=i.attempt_id AND a.repo=e.repo "
                    "AND a.plan_id=e.plan_id AND a.session_id=? AND a.status='delivered' "
                    "WHERE e.repo=? AND e.plan_id=? "
                    "AND e.identity=? AND e.active=1 ORDER BY e.sequence DESC LIMIT 1",
                    (session_id, repo, plan_id, row["identity"]),
                ).fetchone() if session_id and not recovery else None
                item = dict(row)
                if prior is not None and prior["diagnostic"] == row["diagnostic"]:
                    item["diagnostic"] = ""
                rendered.append(item)
            maximum = self.connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM validation_feedback_events "
                "WHERE repo=? AND plan_id=?", (repo, plan_id),
            ).fetchone()[0]
            attempt_id = uuid4().hex
            self.connection.execute(
                "INSERT INTO validation_delivery_attempts "
                "(id,repo,plan_id,session_id,sequence,recovery,status,created_at) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (attempt_id, repo, plan_id, session_id, maximum, int(recovery),
                 datetime.now(timezone.utc).isoformat()),
            )
            self.connection.executemany(
                "INSERT INTO validation_delivery_items (attempt_id,sequence) VALUES (?,?)",
                [(attempt_id, row["sequence"]) for row in rows],
            )
            return Delivery(attempt_id, tuple(rendered), recovery)

    def bind_session(self, attempt_id: str, session_id: str) -> None:
        """Attach a newly started session before exposing it to runtime state."""
        with _transaction(self.connection):
            attempt = self.connection.execute(
                "SELECT repo,plan_id,recovery FROM validation_delivery_attempts "
                "WHERE id=? AND status='pending'", (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise ValueError("pending validation delivery was not found")
            self.connection.execute(
                "UPDATE validation_delivery_attempts SET session_id=? "
                "WHERE id=? AND status='pending'",
                (session_id, attempt_id),
            )
            if attempt["recovery"]:
                self.connection.execute(
                    "INSERT INTO validation_recovery_sessions "
                    "(repo,plan_id,session_id,required) VALUES (?,?,?,1) "
                    "ON CONFLICT(repo,plan_id,session_id) DO UPDATE SET required=1",
                    (attempt["repo"], attempt["plan_id"], session_id),
                )

    def delivered(self, attempt_id: str, session_id: str) -> None:
        """Advance the cursor only for the successfully validated invocation."""
        with _transaction(self.connection):
            attempt = self.connection.execute(
                "SELECT * FROM validation_delivery_attempts WHERE id=? AND status='pending'",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise ValueError("pending validation delivery was not found")
            self.connection.execute(
                "INSERT INTO validation_delivery_cursors (repo,plan_id,session_id,sequence) "
                "VALUES (?,?,?,?) ON CONFLICT(repo,plan_id,session_id) DO UPDATE SET "
                "sequence=MAX(sequence,excluded.sequence)",
                (attempt["repo"], attempt["plan_id"], session_id,
                 attempt["sequence"]),
            )
            self.connection.execute(
                "UPDATE validation_delivery_attempts SET status='delivered',session_id=? WHERE id=?",
                (session_id, attempt_id),
            )
            self.connection.execute(
                "UPDATE validation_recovery_sessions SET required=0 "
                "WHERE repo=? AND plan_id=? AND session_id=?",
                (attempt["repo"], attempt["plan_id"], session_id),
            )

    def migrate_legacy(self) -> None:
        """Seed only interpretable active legacy failures; keep old columns intact."""
        columns = {row[1] for row in self.connection.execute(
            "PRAGMA table_info(exec_plans)"
        )}
        required = {
            "repo", "plan_id", "current_revision_sha", "verify_result",
            "review_result", "verify_failure_history", "review_failure_history",
        }
        if not required <= columns:
            # Some older catalog rows have only ownership fields.  Their
            # validation history cannot be inferred from missing columns.
            return
        rows = self.connection.execute(
            "SELECT repo,plan_id,current_revision_sha,verify_result,review_result,"
            "verify_failure_history,review_failure_history FROM exec_plans"
        ).fetchall()
        for row in rows:
            repo, plan_id = row["repo"], row["plan_id"]
            try:
                with _transaction(self.connection):
                    # Native observations supersede legacy columns. Legacy-only
                    # audits may be a partial import from an older implementation.
                    if self.connection.execute(
                        "SELECT 1 FROM validation_results WHERE repo=? AND plan_id=? "
                        "AND classification != 'legacy_failure' LIMIT 1",
                        (repo, plan_id),
                    ).fetchone():
                        self._mark_migrated(repo, plan_id)
                        continue
                    for stage in ("verify", "review"):
                        raw = row[f"{stage}_result"]
                        history = json.loads(row[f"{stage}_failure_history"] or "{}")
                        if not isinstance(history, dict):
                            raise ValueError("invalid legacy failure history")
                        for record in history.values():
                            occurrence = record.get("occurrences", 1) if isinstance(record, dict) else None
                            if (type(occurrence) is not int
                                    or not 1 <= occurrence <= 9223372036854775807):
                                raise ValueError("invalid legacy failure occurrence")
                        if not raw or not history:
                            continue
                        if stage == "verify":
                            if len(history) != 1:
                                # Rolling history has no latest-result link. A
                                # truncated raw result cannot establish one.
                                continue
                            fingerprint = next(iter(history))
                            feedback = (Feedback(stage, "verify:" + fingerprint,
                                                 bounded_diagnostic(str(raw))),)
                        else:
                            if not str(raw).strip().endswith("VERDICT: FAIL"):
                                continue
                            feedback = normalize_feedback(stage, str(raw))
                        if feedback:
                            if self.connection.execute(
                                "SELECT 1 FROM validation_results WHERE repo=? AND plan_id=? "
                                "AND stage=? AND classification='legacy_failure' LIMIT 1",
                                (repo, plan_id, stage),
                            ).fetchone():
                                continue
                            result_id = self._result(
                                repo, plan_id, row["current_revision_sha"],
                                stage, "legacy_failure", str(raw), feedback,
                            )
                            for item in feedback:
                                if stage == "verify":
                                    fingerprint = item.identity.split(":", 1)[1]
                                else:
                                    normalized = "ISSUE_ID:" + item.identity.split(":", 1)[1].casefold()
                                    digest = hashlib.sha256()
                                    digest.update(b"loop-manager-validation-failure-v1\0")
                                    digest.update(b"review\0")
                                    digest.update(normalized.encode())
                                    fingerprint = digest.hexdigest()
                                record = history.get(fingerprint, {})
                                occurrence = int(record.get("occurrences", 1))
                                self._event(result_id, repo, plan_id, stage,
                                            item.identity, occurrence, 1,
                                            item.diagnostic, True)
                    self._mark_migrated(repo, plan_id)
            except (TypeError, ValueError, KeyError):
                # Roll back new audit rows, preserve immutable older audits, and
                # revoke an old false completion so corrected input can retry.
                with _transaction(self.connection):
                    self.connection.execute(
                        "DELETE FROM validation_feedback_migrations WHERE repo=? AND plan_id=?",
                        (repo, plan_id),
                    )
                continue
