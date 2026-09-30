"""Durable, generation-checked intent for Manager Git operations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from recovery_runtime import RecoveryFailure, RecoveryReason
from managed_rebase_validation import valid_acceptance


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

    def current_rebase_acceptance(self) -> dict | None:
        """Read a current-result acceptance separately from legacy execution proof."""
        result = (self.snapshot or {}).get('current_rebase_acceptance')
        if (not isinstance(result, dict) or not valid_acceptance(result)
                or self.operation is not None
                or self.generation != result['generation'] + 1
                or self.expected_head != result['head']
                or self.target_base_oid != result['target_base_oid']
                or self.observed_remote_oid != result['remote'].get('oid')):
            return None
        return result


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

    def accept_current_rebase(self, repo: str, plan_id: str, *,
                              acceptance: dict) -> CheckpointRecord:
        """CAS an acceptance while preserving prior evidence and audit records.

        The runtime repository owns the transaction and live-state recheck.
        This method only stores the already checked result.
        """
        if (not valid_acceptance(acceptance) or acceptance['repo'] != repo
                or acceptance['plan_id'] != plan_id):
            raise CheckpointRace('current rebase acceptance identity is invalid')
        current = self.get(repo, plan_id)
        if current.current_rebase_acceptance() == acceptance:
            return current
        if (current.generation != acceptance['generation']
                or current.operation not in {None, 'rebase'}
                or (current.target_base_oid is not None
                    and current.target_base_oid != acceptance['target_base_oid'])
                or (current.observed_remote_oid is not None
                    and current.observed_remote_oid != acceptance['remote']['oid'])):
            raise CheckpointRace('current rebase checkpoint generation or target changed')
        snapshot = dict(current.snapshot or {})
        history = list(snapshot.get('current_rebase_acceptance_history', []))
        if 'current_rebase_acceptance' in snapshot:
            history.append(snapshot['current_rebase_acceptance'])
        snapshot['current_rebase_acceptance_history'] = history
        snapshot['current_rebase_acceptance'] = dict(acceptance)
        return self.record(
            repo, plan_id, expected_generation=current.generation,
            operation=None, expected_head=acceptance['head'],
            target_base_oid=acceptance['target_base_oid'],
            observed_remote_oid=acceptance['remote']['oid'], snapshot=snapshot,
        )
