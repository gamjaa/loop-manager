"""Durable, generation-checked intent for Manager Git operations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from recovery_runtime import RecoveryFailure, RecoveryReason


SCHEMA = """
CREATE TABLE IF NOT EXISTS execution_checkpoints (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    operation TEXT,
    expected_head TEXT,
    target_base_oid TEXT,
    observed_remote_oid TEXT,
    snapshot TEXT,
    PRIMARY KEY (repo, plan_id)
)
"""


class CheckpointRace(RecoveryFailure):
    """The selected generation or observed state changed before persistence."""

    def __init__(self, detail: str):
        super().__init__(RecoveryReason.REMOTE_SNAPSHOT_RACE, detail)


@dataclass(frozen=True)
class CheckpointRecord:
    generation: int = 0
    operation: str | None = None
    expected_head: str | None = None
    target_base_oid: str | None = None
    observed_remote_oid: str | None = None
    snapshot: dict | None = None

    def completed_rebase(self) -> dict | None:
        """Return the sealed rebase identity only while this generation retains it."""
        proof = (self.snapshot or {}).get('completed_rebase')
        if not isinstance(proof, dict) or self.operation == 'rebase':
            return None
        if (not isinstance(proof.get('generation'), int)
                or proof['generation'] >= self.generation):
            return None
        return proof


class ExecutionCheckpointRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute(SCHEMA)

    def get(self, repo: str, plan_id: str) -> CheckpointRecord:
        row = self.connection.execute(
            "SELECT * FROM execution_checkpoints WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None:
            return CheckpointRecord()
        return CheckpointRecord(
            generation=int(row["generation"]), operation=row["operation"],
            expected_head=row["expected_head"],
            target_base_oid=row["target_base_oid"],
            observed_remote_oid=row["observed_remote_oid"],
            snapshot=json.loads(row["snapshot"]) if row["snapshot"] else None,
        )

    def record(
        self, repo: str, plan_id: str, *, expected_generation: int,
        operation: str | None, expected_head: str | None,
        target_base_oid: str | None, observed_remote_oid: str | None,
        snapshot: dict | None,
    ) -> CheckpointRecord:
        """Commit an intent or completion only against the observed generation."""
        payload = json.dumps(snapshot, sort_keys=True) if snapshot is not None else None
        self.connection.execute("SAVEPOINT execution_checkpoint_record")
        try:
            if expected_generation == 0:
                result = self.connection.execute(
                    "INSERT OR IGNORE INTO execution_checkpoints "
                    "(repo, plan_id, generation, operation, expected_head, "
                    "target_base_oid, observed_remote_oid, snapshot) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                    (repo, plan_id, operation, expected_head, target_base_oid,
                     observed_remote_oid, payload),
                )
            else:
                result = self.connection.execute(
                    "UPDATE execution_checkpoints SET generation = generation + 1, "
                    "operation = ?, expected_head = ?, target_base_oid = ?, "
                    "observed_remote_oid = ?, snapshot = ? WHERE repo = ? "
                    "AND plan_id = ? AND generation = ?",
                    (operation, expected_head, target_base_oid, observed_remote_oid,
                     payload, repo, plan_id, expected_generation),
                )
            if result.rowcount != 1:
                raise CheckpointRace("execution checkpoint generation changed")
            self.connection.execute("RELEASE SAVEPOINT execution_checkpoint_record")
        except BaseException:
            self.connection.execute("ROLLBACK TO SAVEPOINT execution_checkpoint_record")
            self.connection.execute("RELEASE SAVEPOINT execution_checkpoint_record")
            raise
        return self.get(repo, plan_id)
