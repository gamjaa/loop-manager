"""Durable, generation-checked intent for Manager Git operations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import re
import sqlite3
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.git.managed_rebase_validation import valid_acceptance, fingerprint
from .execplan_catalog_state import is_cancelled


def historical_agent_snapshots_match(saved_row, snapshots):
    """Scope history may refer only to the task workspace proved retired by the caller."""
    from loop_manager_internal.agents.scope_contracts import GitSnapshot
    try:
        for raw in snapshots:
            if raw is None:
                continue
            if not saved_row['branch'] or not saved_row['worktree'] or not isinstance(raw, str):
                return False
            scope = GitSnapshot.deserialize(raw)
            if (scope.branch != 'refs/heads/' + saved_row['branch']
                    or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', scope.head)):
                return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, RecursionError):
        return False


def cancelled_snapshot_without_new_resources(snapshot, repo, plan_id, saved_row):
    """Recognize owned history; this never grants a stage-result reuse token."""
    from dataclasses import fields
    from loop_manager_internal.execution.execution_checkpoint import CheckpointSnapshot
    native_fields = {field.name for field in fields(CheckpointSnapshot)}
    history_fields = {'document_revision', 'retired_document_assessment', 'stage_result_origins',
                      'worker_candidate_token', 'retired_agent_scopes', 'fetch_in_progress', 'fetched_base_oid'}
    if not isinstance(snapshot, dict) or set(snapshot) - native_fields - history_fields:
        return False
    if 'fetch_in_progress' in snapshot and type(snapshot['fetch_in_progress']) is not bool:
        return False
    if 'fetched_base_oid' in snapshot and (not isinstance(snapshot['fetched_base_oid'], str)
            or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', snapshot['fetched_base_oid'])):
        return False
    if set(snapshot) & native_fields:
        # A complete native observation is history only after its original
        # workspace has retired; pending operations/stashes still own resources.
        if (not native_fields <= set(snapshot) or not saved_row.get('workspace_owned')
                or snapshot['plan_id'] != plan_id or snapshot['branch'] != saved_row['branch']
                or snapshot['local_branch'] not in (None, saved_row['branch'])
                or snapshot['phase'] not in {'IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING'}
                or type(snapshot['worktree_owned']) is not bool
                or snapshot['base_is_ancestor'] is not None and type(snapshot['base_is_ancestor']) is not bool
                or snapshot['rebase_intent'] is not False
                or any(snapshot[field] is not None for field in ('pending_operation', 'git_operation', 'stash_oid'))
                or any(snapshot[field] not in ([], ()) for field in ('git_operation_markers', 'unmerged_stages'))
                or not isinstance(snapshot['staged_paths'], (list, tuple))
                or any(not isinstance(path, str) for path in snapshot['staged_paths'])
                or snapshot['pr_state'] not in (None, 'OPEN', 'CLOSED')):
            return False
        oid_fields = {'base_oid', 'local_head', 'recorded_local_head', 'active_sha', 'validated_base_sha',
                      'rebase_before_head', 'rebase_target_oid', 'remote_oid', 'recorded_remote_oid',
                      'pr_head_oid', 'recorded_pr_head_oid'}
        if any(snapshot[field] is not None and (not isinstance(snapshot[field], str)
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', snapshot[field])) for field in oid_fields):
            return False
        if any(snapshot[field] is not None and not isinstance(snapshot[field], str)
                for field in ('definition_revision', 'verify_token', 'review_token', 'archive_token', 'publication_token')):
            return False
    token = snapshot.get('worker_candidate_token')
    if token is not None and (not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token)):
        return False
    origins = snapshot.get('stage_result_origins', {})
    if not isinstance(origins, dict):
        return False
    for identifier, item in origins.items():
        if (not isinstance(item, dict) or set(item) != {'evidence', 'reason'}
                or not isinstance(item['reason'], str) or not item['reason'].strip()):
            return False
        evidence = item['evidence']
        if (not isinstance(evidence, dict) or evidence.get('version') != 1
                or (evidence.get('repo'), evidence.get('plan_id')) != (repo, plan_id)
                or any(field not in evidence or evidence[field] != saved_row[field]
                       for field in ('branch', 'worktree', 'pr_url', 'pr_head_oid'))):
            return False
        encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        if identifier != hashlib.sha256(encoded.encode('utf-8')).hexdigest():
            return False
    scopes = snapshot.get('retired_agent_scopes', [])
    if not isinstance(scopes, list):
        return False
    for item in scopes:
        if (not isinstance(item, dict) or set(item) != {
                'agent_role', 'agent_snapshot', 'agent_result_snapshot', 'agent_manager_snapshot'}
                or item['agent_role'] not in (None, 'worker', 'reviewer')
                or not historical_agent_snapshots_match(saved_row, (item[field] for field in (
                    'agent_snapshot', 'agent_result_snapshot', 'agent_manager_snapshot')))):
            return False
    return True


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
    workspace_ownership_held INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, plan_id)
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    if 'workspace_ownership_held' not in {row[1] for row in connection.execute('PRAGMA table_info(execution_checkpoints)')}:
        connection.execute('SAVEPOINT checkpoint_workspace_hold')
        try:
            connection.execute('ALTER TABLE execution_checkpoints ADD COLUMN workspace_ownership_held INTEGER NOT NULL DEFAULT 0')
            keys = connection.execute('SELECT repo,plan_id FROM execution_checkpoints').fetchall()
            for repo, plan_id in keys:
                row = connection.execute('SELECT snapshot FROM execution_checkpoints WHERE repo=? AND plan_id=?', (repo, plan_id)).fetchone()
                snapshot = json.loads(row[0]) if row[0] else {}
                connection.execute('UPDATE execution_checkpoints SET workspace_ownership_held=? WHERE repo=? AND plan_id=?',
                                   (int(bool((snapshot or {}).get('workspace_ownership_hold'))), repo, plan_id))
            connection.execute('RELEASE SAVEPOINT checkpoint_workspace_hold')
        except BaseException:
            connection.execute('ROLLBACK TO SAVEPOINT checkpoint_workspace_hold')
            connection.execute('RELEASE SAVEPOINT checkpoint_workspace_hold')
            raise
    connection.execute('CREATE INDEX IF NOT EXISTS checkpoint_workspace_holds ON execution_checkpoints(repo,workspace_ownership_held,plan_id)')


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
                or not intent['remote_before']
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
        ensure_schema(connection)

    def get(self, repo: str, plan_id: str) -> CheckpointRecord:
        if is_cancelled(self.connection, repo, plan_id):
            return CheckpointRecord()
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
        for key in ('document_revision', 'worker_frontmatter_restores', 'planning_reassessment',
                    'user_decision_assessment', 'user_decision_assessment_history', 'planning_resume_artifacts',
                    'common_defect_assessment', 'common_defect_assessment_history'):
            if current.snapshot and key in current.snapshot and key not in (snapshot or {}):
                snapshot = {**(snapshot or {}), key: current.snapshot[key]}
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
        workspace_held = int(bool((snapshot or {}).get('workspace_ownership_hold')))
        self.connection.execute("SAVEPOINT execution_checkpoint_record")
        try:
            if is_cancelled(self.connection, repo, plan_id):
                raise CheckpointRace(f"취소된 작업의 checkpoint를 기록할 수 없습니다: {plan_id}")
            if expected_generation == 0:
                result = self.connection.execute(
                    "INSERT OR IGNORE INTO execution_checkpoints "
                    "(repo, plan_id, generation, operation, expected_head, "
                    "target_base_oid, observed_remote_oid, snapshot, workspace_ownership_held) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
                    (repo, plan_id, operation, expected_head, target_base_oid,
                     observed_remote_oid, payload, workspace_held),
                )
            else:
                result = self.connection.execute(
                    "UPDATE execution_checkpoints SET generation = generation + 1, "
                    "operation = ?, expected_head = ?, target_base_oid = ?, "
                    "observed_remote_oid = ?, snapshot = ?, workspace_ownership_held = ? WHERE repo = ? "
                    "AND plan_id = ? AND generation = ?",
                    (operation, expected_head, target_base_oid, observed_remote_oid,
                     payload, workspace_held, repo, plan_id, expected_generation),
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
        if 'rebase_documents' in snapshot:
            snapshot.setdefault('rebase_revalidation_audit', []).append(
                {'rebase_documents': snapshot.pop('rebase_documents')})
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
                or acceptance['remote']['oid'] != remote_before or not remote_before):
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
                or (acceptance['remote']['pull_request'] or {}).get('identity')
                   != (intent['acceptance']['remote']['pull_request'] or {}).get('identity')):
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
        saved['accepted_work_branch'] = intent['head']
        return self.record(repo, plan_id, expected_generation=current.generation,
                           operation=None, expected_head=intent['head'],
                           target_base_oid=intent['target_base_oid'],
                           observed_remote_oid=intent['head'], snapshot=saved)
