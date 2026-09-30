"""Durable, generation-checked intent for Manager Git operations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from recovery_runtime import RecoveryFailure, RecoveryReason
from managed_rebase_validation import valid_acceptance, fingerprint


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

    def rebase_publication_intent(self):
        """Return only the immutable intent bound to this selected checkpoint."""
        intent = (self.snapshot or {}).get('rebase_publication_intent')
        fields = {'kind', 'version', 'repo', 'plan_id', 'repository_identity', 'branch',
                  'worktree', 'target_base_oid', 'head', 'remote_before', 'acceptance',
                  'generation', 'id'}
        if not isinstance(intent, dict) or set(intent) != fields:
            return None
        accepted = intent['acceptance']
        if (intent['kind'] != 'intermediate_rebase_publication' or intent['version'] != 1
                or type(intent['generation']) is not int or intent['generation'] < 0
                or not valid_acceptance(accepted)
                or not intent['remote_before'] or accepted['remote']['pull_request'] is not None
                or accepted['remote']['oid'] != intent['remote_before']
                or any(accepted[key] != intent[key] for key in (
                    'repo', 'plan_id', 'repository_identity', 'branch', 'worktree',
                    'target_base_oid', 'head'))
                or intent['id'] != fingerprint({k: v for k, v in intent.items() if k != 'id'})
                or self.generation <= intent['generation']
                or self.expected_head != intent['head'] or self.target_base_oid != intent['target_base_oid']
                or (self.operation == 'rebase_publish' and self.observed_remote_oid != intent['remote_before'])):
            return None
        return intent


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
        current = self.get(repo, plan_id)
        if current.operation == 'rebase_publish':
            saved = current.snapshot or {}
            intent = current.rebase_publication_intent()
            supplied = snapshot or {}
            completed = supplied.get('rebase_publication_result') or {}
            same_candidate = (intent is not None
                and supplied.get('rebase_publication_intent') == intent
                and expected_head == current.expected_head
                and target_base_oid == current.target_base_oid)
            pending = (operation == 'rebase_publish'
                       and observed_remote_oid == current.observed_remote_oid)
            confirmed = (operation is None and intent is not None
                         and observed_remote_oid == intent['head']
                         and completed.get('intent_id') == intent['id']
                         and completed.get('head') == intent['head'])
            if not same_candidate or not (pending or confirmed):
                raise CheckpointRace('intermediate publication must finish before another Git operation')
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
                              acceptance: dict, prior_artifacts: dict | None = None) -> CheckpointRecord:
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
        if prior_artifacts is not None:
            snapshot.setdefault('rebase_revalidation_audit', []).append(prior_artifacts)
        snapshot.pop('worker_candidate_token', None)
        return self.record(
            repo, plan_id, expected_generation=current.generation,
            operation=None, expected_head=acceptance['head'],
            target_base_oid=acceptance['target_base_oid'],
            observed_remote_oid=acceptance['remote']['oid'], snapshot=snapshot,
        )

    def begin_rebase_publication(self, repo, plan_id, *, expected_generation,
                                 acceptance, remote_before):
        """Persist one immutable lease and candidate for intermediate publication."""
        current = self.get(repo, plan_id)
        if (current.generation != expected_generation or current.operation is not None
                or not valid_acceptance(acceptance) or acceptance['repo'] != repo
                or acceptance['plan_id'] != plan_id
                or current.expected_head != acceptance['head']
                or current.target_base_oid != acceptance['target_base_oid']
                or acceptance['remote']['oid'] != remote_before or not remote_before
                or acceptance['remote']['pull_request'] is not None):
            raise CheckpointRace('intermediate rebase publication acceptance changed')
        intent = {key: acceptance[key] for key in (
            'repo', 'plan_id', 'repository_identity', 'branch', 'worktree',
            'target_base_oid', 'head')}
        intent.update(kind='intermediate_rebase_publication', version=1,
                      remote_before=remote_before, acceptance=dict(acceptance),
                      generation=current.generation)
        intent['id'] = fingerprint(intent)
        saved = dict(current.snapshot or {})
        if 'rebase_publication_intent' in saved:
            saved.setdefault('rebase_publication_history', []).append({
                'intent': saved['rebase_publication_intent'],
                'result': saved.get('rebase_publication_result')})
        saved['rebase_publication_intent'] = intent
        return self.record(repo, plan_id, expected_generation=current.generation,
                           operation='rebase_publish', expected_head=intent['head'],
                           target_base_oid=intent['target_base_oid'],
                           observed_remote_oid=remote_before, snapshot=saved)

    def finish_rebase_publication(self, repo, plan_id, *, intent, acceptance):
        """CAS the confirmed result; the caller atomically updates the runtime lease."""
        current = self.get(repo, plan_id)
        saved = dict(current.snapshot or {})
        if (current.rebase_publication_intent() != intent
                or intent.get('id') != fingerprint({k: v for k, v in intent.items() if k != 'id'})
                or not valid_acceptance(acceptance)
                or any(acceptance[key] != intent[key] for key in (
                    'repo', 'plan_id', 'repository_identity', 'branch', 'worktree',
                    'target_base_oid', 'head'))
                or intent['repo'] != repo or intent['plan_id'] != plan_id
                or acceptance['remote']['oid'] != intent['head']
                or acceptance['remote']['pull_request'] is not None):
            raise CheckpointRace('intermediate rebase publication result changed')
        if current.operation is None and saved.get('rebase_publication_result', {}).get('intent_id') == intent['id']:
            return current
        if (current.operation != 'rebase_publish' or current.expected_head != intent['head']
                or current.observed_remote_oid != intent['remote_before']):
            raise CheckpointRace('intermediate rebase publication intent changed')
        saved['rebase_publication_result'] = {
            'intent_id': intent['id'], 'head': intent['head'],
            'acceptance_id': saved['current_rebase_acceptance']['id'],
            'current_acceptance': dict(acceptance)}
        saved['pushed_checkpoint_head'] = intent['head']
        saved['last_adopted_remote_oid'] = intent['head']
        return self.record(repo, plan_id, expected_generation=current.generation,
                           operation=None, expected_head=intent['head'],
                           target_base_oid=intent['target_base_oid'],
                           observed_remote_oid=intent['head'], snapshot=saved)
