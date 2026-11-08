"""Durable, generation-checked intent for Manager Git operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import hashlib
import re
import sqlite3
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.git.managed_rebase_validation import valid_acceptance, fingerprint
from .execplan_catalog_state import is_cancelled


def pending_completion_plan_ids(connection, repo):
    """Read unresolved observations without remote I/O or schema mutation."""
    if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_checkpoints'").fetchone():
        return frozenset()
    return frozenset(str(row[0]) for row in connection.execute(
        "SELECT x.plan_id,x.snapshot FROM execution_checkpoints x JOIN exec_plans p "
        "ON p.repo=x.repo AND p.plan_id=x.plan_id WHERE x.repo=? AND p.status<>'MERGED'", (repo,),
    ) if ((json.loads(row[1] or '{}') or {}).get('external_completion') or {}).get('db_pending'))


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
            if (scope.branch not in (saved_row['branch'], 'refs/heads/' + saved_row['branch'])
                    or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', scope.head)):
                return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, RecursionError):
        return False


def _cancelled_rebase_history(snapshot, repo, plan_id, saved_row, origins):
    """Validate retired ownership, never reissue an acceptance/publication token."""
    def owned(acceptance):
        from loop_manager_internal.maintenance.maintenance_validation import _plan_id_from_path
        return (valid_acceptance(acceptance)
                and _plan_id_from_path(acceptance['plan_path']) == plan_id
                and all(acceptance[field] == expected for field, expected in (
                    ('repo', repo), ('plan_id', plan_id),
                    ('branch', saved_row['branch']), ('worktree', saved_row['worktree'])))
                and (acceptance['remote']['pull_request'] or {}).get('state') != 'MERGED')

    def publication(intent, result):
        # The existing parser validates the immutable format/hash. This
        # temporary record is only a format reader, not a persisted generation.
        if not isinstance(intent, dict) or not owned(intent.get('acceptance')):
            return False
        record = CheckpointRecord(generation=intent['generation'] + 1, expected_head=intent['head'],
            target_base_oid=intent['target_base_oid'], snapshot={'rebase_publication_intent': intent})
        if record.rebase_publication_intent() != intent:
            return False
        if result is None:
            return True  # Retiring an abandoned request does not claim a push.
        return (isinstance(result, dict) and set(result) == {
                    'intent_id', 'head', 'acceptance_id', 'current_acceptance'}
                and result['intent_id'] == intent['id'] and result['head'] == intent['head']
                and isinstance(result['acceptance_id'], str) and re.fullmatch(r'[0-9a-f]{64}', result['acceptance_id'])
                and owned(result['current_acceptance'])
                and all(result['current_acceptance'][field] == intent[field] for field in (
                    'repository_identity', 'branch', 'worktree', 'target_base_oid', 'head'))
                and result['current_acceptance']['remote']['oid'] == intent['head'])
    try:
        if 'current_rebase_acceptance' in snapshot and not owned(snapshot['current_rebase_acceptance']):
            return False
        acceptances = snapshot.get('current_rebase_acceptance_history', [])
        if not isinstance(acceptances, list) or not all(owned(item) for item in acceptances):
            return False
        retired = snapshot.get('retired_rebase_publications', [])
        if not isinstance(retired, list):
            return False
        for item in retired:
            if (not isinstance(item, dict) or set(item) != {'intent', 'observed_remote', 'confirmed'}
                    or item['confirmed'] is not False or not publication(item['intent'], None)
                    or item['observed_remote'] is not None and (not isinstance(item['observed_remote'], str)
                        or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', item['observed_remote']))):
                return False
        intent, result = snapshot.get('rebase_publication_intent'), snapshot.get('rebase_publication_result')
        if intent is not None:
            if not publication(intent, result):
                return False
        elif result is not None:
            previous = next((item['intent'] for item in retired if item['intent']['id'] == result.get('intent_id')), None)
            if not publication(previous, result):
                return False
        superseded = snapshot.get('superseded_rebases', [])
        if not isinstance(superseded, list):
            return False
        for item in superseded:
            if (not isinstance(item, dict) or set(item) != {
                    'operation', 'expected_head', 'target_base_oid', 'observed_remote_oid', 'snapshot'}
                    or item['operation'] != 'rebase'
                    or any(item[field] is not None and (not isinstance(item[field], str)
                        or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', item[field]))
                           for field in ('expected_head', 'target_base_oid', 'observed_remote_oid'))
                    or not cancelled_snapshot_without_new_resources(item['snapshot'], repo, plan_id, saved_row,
                        _origins=origins, _historical=True)):
                return False
        publications = snapshot.get('rebase_publication_history', [])
        if not isinstance(publications, list) or any(not isinstance(item, dict)
                or set(item) != {'intent', 'result'} or not publication(item['intent'], item['result'])
                for item in publications):
            return False
        if snapshot.get('integration_repo', repo) != repo:
            return False
        for field in ('base_rebase_restart', 'work_branch_integration'):
            frames = snapshot.get(field + '_history', [])
            if not isinstance(frames, list):
                return False
            frames = [*frames, *([snapshot[field]] if snapshot.get(field) is not None else [])]
            for frame in frames:
                if not isinstance(frame, dict) or set(frame) - {
                        'remote', 'original_head', 'snapshot', 'baseline', 'phase', 'remote_before',
                        'head', 'acceptance_id', 'branch', 'worktree', 'before', 'local_tree',
                        'local_commit', 'restart'} or frame.get('phase') not in (None, 'preserving', 'rebasing', 'complete'):
                    return False
                if any(name in frame and frame[name] != expected for name, expected in (
                        ('branch', saved_row['branch']), ('worktree', saved_row['worktree']))):
                    return False
                if any(frame.get(name) is not None and (not isinstance(frame[name], str)
                        or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', frame[name]))
                        for name in ('remote', 'original_head', 'remote_before', 'head', 'local_tree', 'local_commit')):
                    return False
                for item in (frame, *([frame['restart']] if 'restart' in frame else [])):
                    if (not isinstance(item, dict) or (item is not frame and set(item) != {
                            'remote', 'original_head', 'snapshot', 'baseline'})
                            or not historical_agent_snapshots_match(saved_row,
                                (item[name] for name in ('snapshot', 'before') if name in item))):
                        return False
                    # Baseline identities and their actual ref absence are
                    # checked by the caller alongside the parent Agent baseline.
                    if item.get('baseline') is not None and not isinstance(item['baseline'], str):
                        return False
        history = snapshot.get('rebase_operation_history', [])
        if not isinstance(history, list) or not all(
                cancelled_snapshot_without_new_resources(item, repo, plan_id, saved_row, _origins=origins, _historical=True) for item in history):
            return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, RecursionError):
        return False


def cancelled_branch_restart(snapshot, repo, plan_id, saved_row):
    """Read the native completed reset intent without granting a reset or cleanup."""
    from pathlib import Path
    intent = snapshot.get('deleted_branch_restart')
    if intent is None:
        return None
    try:
        fields = {'state', 'base', 'plan', 'block', 'snapshot', 'archive', 'archive_branch',
                  'checkpoint', 'operation', 'publication', 'recovery'}
        if (not isinstance(intent, dict) or set(intent) - fields - {'correction'} or not fields <= set(intent)
                or intent['state'] != 'complete' or not isinstance(intent['base'], str)
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', intent['base'])):
            raise ValueError('unknown restart intent')
        old = intent['plan']
        if (not isinstance(old, dict) or (old['repo'], old['plan_id']) != (repo, plan_id)
                or old['status'] == 'MERGED' or old['branch'] != 'loop-manager/' + plan_id
                or not isinstance(old['worktree'], str) or not Path(old['worktree']).is_absolute()
                or old['branch'] != saved_row['branch'] or old['worktree'] != saved_row['worktree']
                or not historical_agent_snapshots_match(old, (intent['snapshot'],))):
            raise ValueError('restart owner differs')
        archive = Path(intent['archive'])
        if (not archive.is_absolute() or not re.fullmatch(r'[0-9a-f]{32}', archive.name)
                or archive != Path(old['worktree']).parent / 'retired' / archive.name
                or intent['archive_branch'] != f'loop-manager/retired/{plan_id}/{archive.name}'):
            raise ValueError('restart archive differs')
        for name in ('block', 'publication', 'correction'):
            item = intent.get(name)
            if item is not None and (not isinstance(item, dict)
                    or (item.get('repo'), item.get('plan_id')) != (repo, plan_id)):
                raise ValueError('restart audit owner differs')
        recovery = intent['recovery']
        if recovery is not None and (not isinstance(recovery, dict) or recovery.get('repo') != repo
                or recovery.get('subject') not in (plan_id, 'plan:' + plan_id)):
            raise ValueError('restart recovery owner differs')
        if not cancelled_snapshot_without_new_resources(intent['checkpoint'], repo, plan_id, old):
            raise ValueError('restart checkpoint owner is unknown')
        return intent
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, RecursionError) as error:
        raise ValueError('unknown retired branch restart resources') from error


def cancelled_snapshot_without_new_resources(snapshot, repo, plan_id, saved_row, *, _origins=None, _historical=False):
    """Recognize owned history; this never grants a stage-result reuse token."""
    from dataclasses import fields
    from loop_manager_internal.execution.execution_checkpoint import CheckpointSnapshot
    native_fields = {field.name for field in fields(CheckpointSnapshot)}
    history_fields = {'document_revision', 'retired_document_assessment', 'stage_result_origins',
                      'worker_candidate_token', 'retired_agent_scopes', 'fetch_in_progress', 'fetched_base_oid',
                      'current_rebase_acceptance', 'current_rebase_acceptance_history',
                      'rebase_publication_intent', 'rebase_publication_result', 'rebase_publication_history',
                      'rebase_operation_history', 'before_head', 'target_base', 'rebase_remote_oid',
                      'rebase_policy', 'rebase_stage_origin', 'accepted_work_branch', 'pushed_checkpoint_head',
                      'last_adopted_remote_oid', 'rebase_revalidation_audit', 'integration_repo',
                      'base_rebase_restart', 'base_rebase_restart_history', 'work_branch_integration',
                      'work_branch_integration_history', 'pending_work_branch_input', 'deleted_branch_restart', 'fresh_remote_creation',
                      'retired_rebase_publications', 'superseded_rebases'}
    metadata_dicts = {'rebase_completion', 'completed_rebase', 'rebase_documents', 'rebase_frontmatter',
        'rebase_files', 'stage_evidence', 'commit_recovery', 'document_record', 'blocked_resume',
        'published_review_resume', 'remote_adoption', 'remote_adoption_scope', 'worker_checkpoint_request',
        'worker_checkpoint_result', 'worker_reconnection', 'planning_reassessment', 'planning_resume_artifacts',
        'user_decision_assessment', 'common_defect_assessment', 'manager_metadata_agent_baseline',
        'workspace_identity_recovery', 'recovered_rebase_metadata', 'rebase_resolution_commit',
        'worker_checkpoint_pending', 'remote_adoption_intent', 'rebase_block_release',
        'confirmed_manual_stage_revalidation', 'metadata_block_git', 'external_completion'}
    metadata_lists = {'worker_checkpoint_requests', 'worker_frontmatter_restores', 'workspace_scope_audit',
        'user_decision_assessment_history', 'common_defect_assessment_history', 'rebase_block_recovery_audit',
        'scope_block_release_audit', 'publication_block_release_audit', 'retired_publications'}
    metadata_strings = {'commit_message', 'worker_result', 'rebase_run_id', 'rebase_start_session_id',
        'rebase_block_release_state', 'scope_block_resume_status', 'rebase_publication_deferred', 'pending'}
    metadata_flags = {'worktree_recovery_pending', 'workspace_reservation_recovered'}
    metadata_oids = {'after_head', 'commit_tree_oid', 'head', 'reactivated_from_head', 'remote_before',
        'remote_user_input', 'stored_active_sha', 'manual_candidate_head', 'stale_review_candidate', 'pending_work_branch_input'}
    known = native_fields | history_fields | metadata_dicts | metadata_lists | metadata_strings | metadata_flags | metadata_oids | {
        'worktree', 'rebase_evidence_version', 'workspace_ownership_hold'}
    if not isinstance(snapshot, dict) or set(snapshot) - known:
        return False
    if any(snapshot.get(field) is not None and not isinstance(snapshot[field], dict) for field in metadata_dicts):
        return False
    if any(field in snapshot and not isinstance(snapshot[field], list) for field in metadata_lists):
        return False
    if any(snapshot.get(field) is not None and not isinstance(snapshot[field], str) for field in metadata_strings):
        return False
    if any(field in snapshot and type(snapshot[field]) is not bool for field in metadata_flags):
        return False
    if any(snapshot.get(field) is not None and not (field == 'stored_active_sha' and snapshot[field] == '')
            and (not isinstance(snapshot[field], str)
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', snapshot[field])) for field in metadata_oids):
        return False
    if snapshot.get('pr_state') == 'MERGED':
        return False
    if snapshot.get('worktree', saved_row['worktree']) not in ((None, '') if saved_row['worktree'] is None else (saved_row['worktree'],)):
        return False
    if snapshot.get('rebase_evidence_version', 2) != 2:
        return False
    if snapshot.get('workspace_ownership_hold'):
        return False
    if snapshot.get('pending') not in (None, 'stage', 'continue'):
        return False
    for field in metadata_dicts:
        item = snapshot.get(field)
        if item is not None and (('version' in item and (type(item['version']) is not int or item['version'] != 1))
                or any(name in item and item[name] != expected for name, expected in (
                    ('repo', repo), ('plan_id', plan_id), ('branch', saved_row['branch']), ('worktree', saved_row['worktree'])))):
            return False
    for item in snapshot.get('workspace_scope_audit', []):
        if (not isinstance(item, dict) or set(item) != {'scope', 'plan_id', 'branch', 'worktree', 'baseline', 'observation'}
                or (item['scope'], item['plan_id'], item['branch'], item['worktree']) != (
                    'plan', plan_id, saved_row['branch'], saved_row['worktree'])
                or not historical_agent_snapshots_match(saved_row, (item['baseline'], item['observation']))):
            return False
    try:
        cancelled_branch_restart(snapshot, repo, plan_id, saved_row)
    except ValueError:
        return False
    if 'fresh_remote_creation' in snapshot and type(snapshot['fresh_remote_creation']) is not bool:
        return False
    origins = snapshot.get('stage_result_origins', _origins or {})
    if not isinstance(origins, dict):
        return False
    if not _cancelled_rebase_history(snapshot, repo, plan_id, saved_row, origins):
        return False
    for field in ('before_head', 'target_base', 'rebase_remote_oid', 'accepted_work_branch',
                  'pushed_checkpoint_head', 'last_adopted_remote_oid'):
        if snapshot.get(field) is not None and (not isinstance(snapshot[field], str)
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', snapshot[field])):
            return False
    if snapshot.get('rebase_policy') not in (None, 'current_state'):
        return False
    if snapshot.get('rebase_stage_origin') is not None and (
            not isinstance(snapshot['rebase_stage_origin'], str)
            or snapshot['rebase_stage_origin'] not in origins):
        return False
    audits = snapshot.get('rebase_revalidation_audit', [])
    artifact_fields = {'active_sha', 'validated_base_sha', 'definition_snapshot', 'status', 'branch', 'worktree',
        'pr_url', 'pr_head_oid', 'worker_session_id', 'worker_session_binding', 'worker_session_transition',
        'reviewer_session_id', 'agent_input_fingerprint', 'agent_input_plan', 'agent_result_plan',
        'agent_result_snapshot', 'worker_result', 'worker_input_token', 'archive_token', 'verify_result',
        'review_result', 'verify_token', 'review_token', 'publication_token', 'last_error', 'version', 'repo',
        'plan_id', 'before_head', 'target_base_oid', 'checkpoint_generation', 'verify_selection',
        'verify_receipt', 'released_rebase_block', 'prior_agent_recovery', 'rebase_documents'}
    if not isinstance(audits, list):
        return False
    for audit in audits:
        if (not isinstance(audit, dict) or set(audit) - artifact_fields
                or any(field in audit and audit[field] != expected for field, expected in (
                    ('repo', repo), ('plan_id', plan_id), ('branch', saved_row['branch']), ('worktree', saved_row['worktree'])))
                or audit.get('version', 1) != 1):
            return False
    if 'fetch_in_progress' in snapshot and type(snapshot['fetch_in_progress']) is not bool:
        return False
    if 'fetched_base_oid' in snapshot and (not isinstance(snapshot['fetched_base_oid'], str)
            or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', snapshot['fetched_base_oid'])):
        return False
    if set(snapshot) & native_fields:
        # A complete native observation is history only after its original
        # workspace has retired; pending operations/stashes still own resources.
        complete_observation = native_fields <= set(snapshot)
        if not complete_observation:
            if not saved_row.get('branch') or not saved_row.get('worktree'):
                if (not {'worktree_recovery_pending', 'worktree', 'branch', 'stored_active_sha'} <= set(snapshot)
                        or snapshot['branch'] not in (None, '') or snapshot['worktree'] not in (None, '')):
                    return False
            elif (not _historical and (snapshot.get('branch') != saved_row['branch']
                    or snapshot.get('worktree') != saved_row['worktree'])
                    or snapshot.get('branch', saved_row['branch']) != saved_row['branch']):
                return False
        elif (not saved_row.get('workspace_owned')
                or snapshot['plan_id'] != plan_id or snapshot['branch'] != saved_row['branch']
                or snapshot['local_branch'] not in (None, saved_row['branch'])
                or snapshot['phase'] not in {'IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING'}
                or type(snapshot['worktree_owned']) is not bool
                or snapshot['base_is_ancestor'] is not None and type(snapshot['base_is_ancestor']) is not bool
                or type(snapshot['rebase_intent']) is not bool
                or any(snapshot[field] is not None and not isinstance(snapshot[field], str)
                       for field in ('pending_operation', 'git_operation'))
                or any(not isinstance(snapshot[field], (list, tuple)) for field in ('git_operation_markers', 'unmerged_stages'))
                or not isinstance(snapshot['staged_paths'], (list, tuple))
                or any(not isinstance(path, str) for path in snapshot['staged_paths'])
                or snapshot['pr_state'] not in (None, 'OPEN', 'CLOSED')):
            return False
        oid_fields = {'base_oid', 'local_head', 'recorded_local_head', 'active_sha', 'validated_base_sha',
                      'rebase_before_head', 'rebase_target_oid', 'remote_oid', 'recorded_remote_oid',
                      'pr_head_oid', 'recorded_pr_head_oid', 'stash_oid'}
        if any(snapshot.get(field) is not None and (not isinstance(snapshot[field], str)
                or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', snapshot[field])) for field in oid_fields):
            return False
        if any(snapshot.get(field) is not None and not isinstance(snapshot[field], str)
                for field in ('definition_revision', 'verify_token', 'review_token', 'archive_token', 'publication_token')):
            return False
    token = snapshot.get('worker_candidate_token')
    if token is not None and (not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token)):
        return False
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

    def confirmed_rebase_publication_intent(self):
        """Resolve the last success across active, completed and retired intents."""
        saved = self.snapshot or {}
        result = saved.get('rebase_publication_result') or {}
        if not isinstance(result, dict) or not valid_acceptance(result.get('current_acceptance')):
            return None
        accepted = result['current_acceptance']
        candidates = [saved.get('rebase_publication_intent')]
        for field in ('rebase_publication_history', 'retired_rebase_publications'):
            history = saved.get(field, ())
            if not isinstance(history, (list, tuple)) or any(not isinstance(entry, dict) for entry in history):
                return None
            for entry in history:
                intent = entry.get('intent')
                if isinstance(intent, dict) and intent.get('id') == result.get('intent_id'):
                    if field == 'rebase_publication_history' and entry.get('result') != result:
                        return None
                    candidates.append(intent)
        matches = [intent for intent in candidates
                   if isinstance(intent, dict) and intent.get('id') == result.get('intent_id')]
        if not matches or any(intent != matches[0] for intent in matches):
            return None
        intent = matches[0]
        previous = replace(self, operation=None, expected_head=accepted['head'],
                           target_base_oid=accepted['target_base_oid'],
                           snapshot={'rebase_publication_intent': intent})
        if (previous.rebase_publication_intent() != intent
                or self.generation <= accepted['generation']
                or result.get('head') != accepted['head'] or accepted['remote']['oid'] != accepted['head']
                or any(accepted[key] != intent[key] for key in (
                    'repo', 'plan_id', 'repository_identity', 'branch', 'worktree', 'target_base_oid', 'head'))
                or any(accepted['remote'][key] != intent['acceptance']['remote'][key]
                       for key in ('repository', 'branch'))
                or (accepted['remote']['pull_request'] or {}).get('identity')
                   != (intent['acceptance']['remote']['pull_request'] or {}).get('identity')):
            return None
        return intent

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
        if current.snapshot and 'external_completion' in current.snapshot:
            snapshot = {**(snapshot or {}), 'external_completion': current.snapshot['external_completion']}
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

    def record_completion_observation(self, repo, plan_id, *, expected, observation):
        """Retain remote evidence without replacing Git intents or prior stage audits."""
        raw = self.connection.execute(
            'SELECT snapshot FROM execution_checkpoints WHERE repo=? AND plan_id=?', (repo, plan_id),
        ).fetchone()
        if self.get(repo, plan_id) != expected or is_cancelled(self.connection, repo, plan_id):
            raise CheckpointRace('completion observation checkpoint changed')
        payload = json.dumps({**(expected.snapshot or {}), 'external_completion': observation}, sort_keys=True)
        # Native generation binds rebase acceptance/publication contracts. This
        # metadata has its own snapshot CAS and must not invalidate those proofs.
        if raw is None:
            changed = self.connection.execute(
                'INSERT OR IGNORE INTO execution_checkpoints(repo,plan_id,generation,snapshot) VALUES (?,?,1,?)',
                (repo, plan_id, payload))
        else:
            changed = self.connection.execute(
                'UPDATE execution_checkpoints SET snapshot=? WHERE repo=? AND plan_id=? AND generation=? AND snapshot IS ?',
                (payload, repo, plan_id, expected.generation, raw[0]))
        if changed.rowcount != 1:
            raise CheckpointRace('completion observation snapshot changed')
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
