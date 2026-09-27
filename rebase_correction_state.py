"""Durable correction occurrences and Worker turn identity, separate from validation history."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from uuid import uuid4

from rebase_correction import ConflictObservation


SCHEMA = """
CREATE TABLE IF NOT EXISTS rebase_corrections (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    plan_revision TEXT,
    target_base TEXT NOT NULL,
    before_head TEXT NOT NULL,
    source TEXT NOT NULL,
    stash_oid TEXT,
    observation TEXT NOT NULL,
    history TEXT NOT NULL DEFAULT '{}',
    audit TEXT NOT NULL DEFAULT '[]',
    pending_turn TEXT,
    worker_session TEXT,
    worker_result TEXT,
    worker_code INTEGER,
    result_kind TEXT,
    scope_marker TEXT,
    outcome_contract_version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, plan_id)
)
"""


class CorrectionRace(RuntimeError):
    pass


@dataclass(frozen=True)
class CorrectionRecord:
    generation: int
    plan_revision: str | None
    target_base: str
    before_head: str
    source: str
    stash_oid: str | None
    fingerprint: str
    progress: str
    occurrence: int
    audit: tuple[dict, ...]
    pending_turn: str | None
    worker_session: str | None
    worker_result: str | None
    worker_code: int | None
    result_kind: str | None
    scope_marker: str | None
    outcome_contract_version: int = 0


class RebaseCorrectionRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute(SCHEMA)
        columns = {row[1] for row in connection.execute('PRAGMA table_info(rebase_corrections)')}
        if 'outcome_contract_version' not in columns:
            connection.execute(
                'ALTER TABLE rebase_corrections ADD COLUMN '
                'outcome_contract_version INTEGER NOT NULL DEFAULT 0'
            )

    @staticmethod
    def _key(observation: ConflictObservation) -> str:
        return json.dumps((observation.fingerprint, observation.progress),
                          separators=(",", ":"))

    def get(self, repo: str, plan_id: str) -> CorrectionRecord | None:
        row = self.connection.execute(
            "SELECT * FROM rebase_corrections WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None:
            return None
        observed = json.loads(row["observation"])
        history = json.loads(row["history"])
        return CorrectionRecord(
            int(row["generation"]), row["plan_revision"], row["target_base"],
            row["before_head"], row["source"], row["stash_oid"],
            observed["fingerprint"], observed["progress"],
            history.get(json.dumps((observed["fingerprint"], observed["progress"]),
                                   separators=(",", ":")), 0),
            tuple(json.loads(row["audit"])),
            row["pending_turn"], row["worker_session"], row["worker_result"],
            row["worker_code"], row["result_kind"],
            row["scope_marker"], int(row["outcome_contract_version"]),
        )

    def observe(self, repo: str, plan_id: str, observation: ConflictObservation,
                *, corrected: bool = False, expected_generation: int | None = None,
                worker_session: str | None = None, worker_result: str | None = None,
                scope_marker: str | None = None,
                persist_worker_result: bool = True) -> CorrectionRecord:
        row = self.connection.execute(
            "SELECT * FROM rebase_corrections WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        generation = int(row["generation"]) if row else None
        if expected_generation is not None and generation != expected_generation:
            raise CorrectionRace("rebase correction generation changed")
        history = json.loads(row["history"]) if row else {}
        key = self._key(observation)
        seen_before = key in history
        if corrected and seen_before:
            history[key] += 1
        else:
            history.setdefault(key, 0)
        payload = json.dumps({"fingerprint": observation.fingerprint,
                              "progress": observation.progress}, sort_keys=True)
        history_payload = json.dumps(history, sort_keys=True)
        same = bool(row and json.loads(row["observation"]) == json.loads(payload))
        audit = json.loads(row["audit"]) if row else []
        if corrected or not seen_before:
            audit.append({
                "plan_revision": observation.plan_revision,
                "target_base": observation.target_base,
                "before_head": observation.before_head,
                "source": observation.source.value,
                "stash_oid": observation.stash_oid,
                "stages": observation.stages,
                "fingerprint": observation.fingerprint,
                "progress": observation.progress,
                "occurrence": history[key],
                "worker_session": worker_session,
                "worker_result": worker_result,
            })
        audit_payload = json.dumps(audit, sort_keys=True, ensure_ascii=False)
        if row is None:
            try:
                self.connection.execute(
                    "INSERT INTO rebase_corrections (repo, plan_id, plan_revision, target_base, "
                    "before_head, source, stash_oid, observation, history, audit, "
                    "worker_session, worker_result, scope_marker) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (repo, plan_id, observation.plan_revision, observation.target_base,
                     observation.before_head, observation.source.value, observation.stash_oid,
                     payload, history_payload, audit_payload, worker_session,
                     worker_result if persist_worker_result else None,
                     scope_marker if persist_worker_result else None),
                )
            except sqlite3.IntegrityError as error:
                raise CorrectionRace("rebase correction record appeared") from error
        else:
            changed = self.connection.execute(
                "UPDATE rebase_corrections SET generation = generation + 1, "
                "plan_revision = ?, target_base = ?, before_head = ?, source = ?, "
                "stash_oid = ?, observation = ?, history = ?, audit = ?, "
                "pending_turn = ?, worker_session = ?, "
                "worker_result = ?, worker_code = ?, result_kind = ?, scope_marker = ? "
                "WHERE repo = ? AND plan_id = ? "
                "AND generation = ?",
                (observation.plan_revision, observation.target_base,
                 observation.before_head, observation.source.value,
                 observation.stash_oid, payload, history_payload, audit_payload,
                 row["pending_turn"] if same and not corrected and worker_result is None else None,
                 worker_session or row["worker_session"],
                 (row["worker_result"] if same and not corrected and worker_result is None
                  else worker_result) if persist_worker_result else None,
                 (row["worker_code"] if same and not corrected and worker_result is None
                  else None),
                 (row["result_kind"] if same and not corrected and worker_result is None
                  else None),
                 (row["scope_marker"] if same and not corrected and scope_marker is None
                  else scope_marker), repo, plan_id, generation),
            )
            if changed.rowcount != 1:
                raise CorrectionRace("rebase correction generation changed")
        return self.get(repo, plan_id)

    def begin_turn(self, repo: str, plan_id: str, *, expected_generation: int,
                   session: str | None) -> CorrectionRecord:
        current = self.get(repo, plan_id)
        if current is None or current.generation != expected_generation:
            raise CorrectionRace("rebase correction generation changed")
        if current.pending_turn:
            return current
        changed = self.connection.execute(
            "UPDATE rebase_corrections SET generation = generation + 1, pending_turn = ?, "
            "worker_session = ?, worker_result = NULL, worker_code = NULL, "
            "result_kind = NULL, scope_marker = NULL "
            "WHERE repo = ? AND plan_id = ? AND generation = ?",
            (uuid4().hex, session, repo, plan_id, expected_generation),
        )
        if changed.rowcount != 1:
            raise CorrectionRace("rebase correction generation changed")
        return self.get(repo, plan_id)

    def finish_turn(self, repo: str, plan_id: str, *, expected_generation: int,
                    turn: str, session: str | None, result: str,
                    scope_marker: str, code: int = 0,
                    result_kind: str = 'success',
                    outcome_contract_version: int = 1) -> CorrectionRecord:
        changed = self.connection.execute(
            "UPDATE rebase_corrections SET generation = generation + 1, "
            "worker_session = ?, worker_result = ?, worker_code = ?, "
            "result_kind = ?, scope_marker = ?, outcome_contract_version = ? "
            "WHERE repo = ? AND plan_id = ? AND generation = ? AND pending_turn = ?",
            (session, result, code, result_kind, scope_marker, outcome_contract_version,
             repo, plan_id, expected_generation, turn),
        )
        if changed.rowcount != 1:
            raise CorrectionRace("rebase correction turn changed")
        return self.get(repo, plan_id)

    def clear(self, repo: str, plan_id: str) -> None:
        self.connection.execute("DELETE FROM rebase_corrections WHERE repo = ? AND plan_id = ?",
                                (repo, plan_id))

    def abandon_turn(self, repo: str, plan_id: str, *, expected_generation: int) -> None:
        changed = self.connection.execute(
            "UPDATE rebase_corrections SET generation = generation + 1, "
            "pending_turn = NULL, worker_result = NULL, worker_code = NULL, "
            "result_kind = NULL, scope_marker = NULL "
            "WHERE repo = ? AND plan_id = ? AND generation = ?",
            (repo, plan_id, expected_generation),
        )
        if changed.rowcount != 1:
            raise CorrectionRace("rebase correction turn changed")
