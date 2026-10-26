"""Preserve stage source evidence independently of current approval tokens."""
from dataclasses import asdict
import hashlib
import json

from loop_manager_internal.agents.scope_observation import RAW_DIFF_OPTIONS
from loop_manager_internal.git.managed_rebase_observation import parse_raw_changes
from loop_manager_internal.storage import runtime_state, verify_receipts


def preserve(ports, context, *, reason):
    """Archive observed results before invalidation; never infer a success.

    Git reads precede the short ownership/generation transaction. Missing past
    inputs stay missing, including unobservable candidate changes on legacy rows.
    The returned identity names evidence, not approval for the current HEAD.
    """
    checkpoints = ports.checkpoint
    if checkpoints is None:
        return None
    row = dict(ports.state.row())
    if all(row[key] is None for key in ('worker_result', 'verify_result', 'review_result')):
        return None
    repo, plan_id = ports.identity.repo, context.plan.plan_id
    current = checkpoints.get(repo, plan_id)
    selection = verify_receipts.selected(ports.state.connection, repo, plan_id)
    evidence = {key: row[key] for key in (
        'active_sha', 'validated_base_sha', 'definition_snapshot',
        'phase', 'branch', 'worktree', 'pr_url', 'pr_head_oid',
        'worker_session_id', 'worker_session_binding', 'worker_session_transition',
        'reviewer_session_id', 'worker_result', 'worker_input_token', 'archive_token',
        'verify_result', 'verify_token', 'review_result', 'review_token', 'publication_token',
        'agent_input_fingerprint', 'agent_input_plan', 'agent_result_plan', 'agent_result_snapshot',
    )}
    evidence.update(version=1, repo=repo, plan_id=plan_id,
        verify_selection=selection,
        verify_receipt=verify_receipts.current_receipt(ports.state.connection, selection),
        worker_candidate_token=(current.snapshot or {}).get('worker_candidate_token'),
        observed_head=None, changes=None)
    try:
        head = ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        if not head or not row['active_sha']:
            raise ValueError('candidate or original base unavailable')
        if ports.git(context.worktree, 'status', '--porcelain').stdout.strip():
            raise ValueError('candidate has uncommitted inputs')
        changes = parse_raw_changes(ports.git(context.worktree, 'diff', *RAW_DIFF_OPTIONS,
                                             row['active_sha'], head, '--').stdout)
        if ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip() != head:
            raise ValueError('candidate changed during observation')
        evidence.update(observed_head=head, changes=[asdict(change) for change in changes])
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        evidence['observation_error'] = type(error).__name__
    encoded = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    identifier = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    with runtime_state._transaction(ports.state.connection):
        if (dict(ports.state.row()) != row or checkpoints.get(repo, plan_id) != current
                or verify_receipts.selected(ports.state.connection, repo, plan_id) != selection
                or verify_receipts.current_receipt(ports.state.connection, selection)
                != evidence['verify_receipt']):
            raise runtime_state.RuntimeStateError('stage result provenance changed during observation')
        origins = dict((current.snapshot or {}).get('stage_result_origins', {}))
        if identifier not in origins:
            origins[identifier] = {'evidence': evidence, 'reason': reason}
            checkpoints.record(repo, plan_id, expected_generation=current.generation,
                operation=current.operation, expected_head=current.expected_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'stage_result_origins': origins})
    return identifier


def reconnect_worker(ports, context, definition):
    """Reconnect a completed Worker after adopted rebase, including restart.

    General planning supplies the semantic reassessment; raw changes supply
    candidate/base identity. Neither evidence substitutes for the other.
    """
    from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
    from loop_manager_internal.execution.worker_outcome import parse_worker_outcome
    from loop_manager_internal.git.managed_rebase_validation import valid_acceptance
    from loop_manager_internal.storage import planning_review_state
    checkpoints = ports.checkpoint
    if checkpoints is None:
        return False
    repo, plan_id = ports.identity.repo, context.plan.plan_id
    current = checkpoints.get(repo, plan_id)
    saved = current.snapshot or {}
    accepted = saved.get('current_rebase_acceptance')
    origin_id = saved.get('rebase_stage_origin')
    origin = saved.get('stage_result_origins', {}).get(origin_id, {}).get('evidence')
    row = dict(ports.state.row())
    if (current.operation is not None or not valid_acceptance(accepted) or not origin
            or (saved.get('worker_reconnection') or {}).get('acceptance_id') == accepted['id']
            or row['phase'] != 'WORKING' or row['last_error'] is not None or row['worker_result'] is not None
            or row['pr_url'] is not None
            or row['active_sha'] != definition.revision_sha
            or any(accepted[key] != value for key, value in (
                ('repo', repo), ('plan_id', plan_id), ('branch', row['branch']),
                ('worktree', row['worktree']), ('head', current.expected_head),
                ('target_base_oid', row['active_sha'])))
            or origin.get('repo') != repo or origin.get('plan_id') != plan_id
            or origin.get('phase') not in {'VERIFYING', 'REVIEWING', 'PUBLISHING'}
            or not origin.get('observed_head') or origin.get('changes') is None
            or not origin.get('worker_result')
            or any(origin.get(key) != row[key] for key in (
                'branch', 'worktree', 'worker_session_id', 'worker_session_binding', 'worker_session_transition'))):
        return False
    try:
        previous = DefinitionSnapshot.deserialize(origin.get('definition_snapshot'))
        if (previous is None or previous.path != definition.path
                or previous.worker_token != definition.worker_token
                or origin.get('worker_input_token') != previous.worker_token
                or origin.get('archive_token') != definition.archive_token
                or origin.get('worker_candidate_token') != previous.worker_candidate_token(origin['observed_head'])):
            return False
        outcome = parse_worker_outcome(origin['worker_result'], plan_path=previous.path,
            sources=previous.sources, documents={}, allow_legacy_ready=True)
        if outcome.kind != 'ready_for_validation':
            return False
        selection = verify_receipts.selected(ports.state.connection, repo, plan_id)
        if selection is None or selection != origin.get('verify_selection'):
            return False
        if planning_review_state.general_pending(ports.state.connection, repo):
            return False
        general_row = planning_review_state.get(ports.state.connection, repo, 'general')
        general = dict(general_row) if general_row is not None else None
        if (not general or general['confirmed_revision'] != definition.revision_sha
                or general['purpose'] not in {'full', 'delta', 'final'}
                or not general['result'] or not general['outcome']):
            return False
        # A planning receipt must cover the original base, not merely name
        # today's revision. Missing history takes the existing Worker path.
        for descendant in (origin['observed_head'], general['revision']):
            ancestry = ports.git(context.worktree, 'merge-base', '--is-ancestor',
                                 origin['active_sha'], descendant, check=False)
            if ancestry.returncode != 0:
                return False
        head = ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        if head != accepted['head'] or ports.git(context.worktree, 'status', '--porcelain').stdout.strip():
            return False
        base_changes = parse_raw_changes(ports.git(context.worktree, 'diff', *RAW_DIFF_OPTIONS,
            origin['active_sha'], definition.revision_sha, '--').stdout)
        if any(not (change.path.startswith('docs/') and change.path.endswith('.md')) for change in base_changes):
            return False
        changes = parse_raw_changes(ports.git(context.worktree, 'diff', *RAW_DIFF_OPTIONS,
            definition.revision_sha, head, '--').stdout)
        if [asdict(change) for change in changes] != origin['changes']:
            return False
        if (ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip() != head
                or ports.git(context.worktree, 'status', '--porcelain').stdout.strip()):
            return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    with runtime_state._transaction(ports.state.connection):
        current_general = planning_review_state.get(ports.state.connection, repo, 'general')
        if (dict(ports.state.row()) != row or checkpoints.get(repo, plan_id) != current
                or verify_receipts.selected(ports.state.connection, repo, plan_id) != selection
                or planning_review_state.general_pending(ports.state.connection, repo)
                or current_general is None or dict(current_general) != general):
            return False
        checkpoints.record(repo, plan_id, expected_generation=current.generation,
            operation=None, expected_head=current.expected_head, target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={**saved, 'worker_candidate_token': definition.worker_candidate_token(head),
                'worker_reconnection': {'acceptance_id': accepted['id'], 'origin': origin_id,
                    'head': head, 'definition': definition.serialize(), 'planning': general}})
        ports.state.update_project(repo, preserve_transaction=True, phase='VERIFYING',
            definition_snapshot=definition.serialize(), worker_result=origin['worker_result'],
            worker_input_token=definition.worker_token, archive_token=definition.archive_token,
            verify_token=None, review_token=None, publication_token=None)
    return True
