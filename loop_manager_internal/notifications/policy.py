"""Current user actions from durable observations, without external effects."""
from __future__ import annotations

import json

from loop_manager_internal.documents import reviewer_specification
from loop_manager_internal.storage import execplan_draft_state


def _block_action(connection, repo, payload):
    """Return concrete remaining action evidence, never infer it from BLOCKED."""
    if payload.get('stage') == 'planning':
        return None
    plan_id = payload.get('execplan_id')
    scope = payload.get('scope')
    if scope == 'plan':
        block = connection.execute(
            'SELECT reason_code, evidence, release_fingerprint FROM execplan_blocks WHERE repo=? AND plan_id=?',
            (repo, plan_id)).fetchone()
    elif scope == 'project':
        block = connection.execute(
            'SELECT reason_code, evidence, release_fingerprint FROM project_blocks WHERE repo=? AND plan_id IS ?',
            (repo, plan_id)).fetchone()
    else:
        return None
    if (block is None or block[0] != payload.get('reason_code')
            or block[1] != payload.get('cause')):
        return None
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                              (repo, plan_id)).fetchone()
    if (plan is None and scope == 'plan') or (plan is not None and plan['status'] == 'MERGED'):
        return None
    sources = json.loads(plan['sources']) if plan is not None else []
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        return None
    allowed = {*sources, 'docs/PRODUCT.md', 'docs/ARCHITECTURE.md'}
    if plan is not None:
        allowed.add(plan['path'])
    recovery_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'").fetchone()
    recovery = (connection.execute(
        'SELECT * FROM recovery_records WHERE repo=? AND subject=?',
        (repo, plan_id or '__project__')).fetchone() if recovery_exists else None)
    evidence = None
    if recovery is not None:
        # Pending recovery and inconsistent observations are insufficient even
        # when a still-visible block carries an explicit-looking question.
        if (recovery['pending_action'] != 'final_block' or recovery['detail'] != block[1]
                or block[2] is not None and recovery['fingerprint'] != block[2]):
            return None
        evidence = json.loads(recovery['final_evidence'] or 'null')
        if (not isinstance(evidence, dict) or evidence.get('block_reason_code') != block[0]
                or evidence.get('reason_code') != recovery['reason_code']
                or evidence.get('unresolved') != block[1]):
            return None
        revision = (plan['current_revision_sha'] or plan['plan_sha']) if plan is not None else None
        if revision and recovery['plan_revision'] and revision != recovery['plan_revision']:
            return None
    reason = block[0]
    action = None
    if (plan is not None and reason == 'user_decision_required' and evidence is not None
            and recovery['reason_code'] == 'user_decision'):
        decision = evidence.get('worker_decision')
        if (isinstance(decision, dict) and decision.get('kind') == 'needs_user_decision'
                and all(isinstance(decision.get(key), str) and decision[key].strip()
                        for key in ('problem', 'proposal', 'question'))
                and isinstance(decision.get('locations'), list) and decision['locations']
                and all(isinstance(item, dict) and item.get('path') in allowed
                        and isinstance(item.get('section'), str) and item['section'].strip()
                        for item in decision['locations'])
                and payload.get('worker_decision', decision) == decision):
            action = {'kind': 'decision', 'decision': decision}
    elif plan is not None and reason == 'review_specification_deadlock':
        deadlock = reviewer_specification.parse_evidence(block[1])
        reviewer_specification.validate_locations(deadlock, plan['path'], tuple(sources))
        decision = {'kind': deadlock.kind, 'question': deadlock.question,
                    'problem': deadlock.problem, 'implementation_choice': deadlock.implementation_choice,
                    'locations': [{'path': path, 'section': section} for path, section in deadlock.locations]}
        if payload.get('specification_deadlock', decision) != decision:
            return None
        action = {'kind': 'specification', 'decision': decision}
    elif plan is not None and reason == 'guidance_approval_required':
        rows = connection.execute(
            "SELECT proposal_id, proposal_hash, target_path, proposal_line, decision_line "
            "FROM guidance_approvals WHERE repo=? AND plan_id=? AND applied=0 "
            "AND decision='pending' ORDER BY sequence, proposal_id", (repo, plan_id)).fetchall()
        if rows and all(row[0] and row[1] and row[3] > 0 and row[4] > 0 for row in rows):
            action = {'kind': 'guidance', 'proposals': [list(row) for row in rows]}
    elif plan is not None and recovery is not None and reason == 'pull_request_closed':
        if (recovery['reason_code'] == 'pull_request_closed' and plan['pr_url']
                and plan['status'] == 'PLAN_BLOCKED' and plan['blocked_reason'] == reason):
            action = {'kind': 'reopen_pr', 'pr_url': plan['pr_url']}
    elif plan is not None and recovery is not None and reason == 'remote_branch_deleted':
        from loop_manager_internal.recovery.work_branch_integration import published_branch
        saved = connection.execute('SELECT snapshot FROM execution_checkpoints WHERE repo=? AND plan_id=?',
                                   (repo, plan_id)).fetchone()
        snapshot = json.loads(saved[0] or '{}') if saved else {}
        if not isinstance(snapshot, dict):
            return None
        restart = snapshot.get('deleted_branch_restart')
        if restart is not None and not isinstance(restart, dict):
            return None
        consumed = bool(restart and (restart.get('state') != 'complete'
            or not isinstance(restart.get('block'), dict)
            or all(restart['block'].get(key) == value for key, value in (
                ('reason_code', block[0]), ('evidence', block[1]), ('release_fingerprint', block[2])))))
        if (recovery['reason_code'] == reason and plan['branch']
                and published_branch(connection, repo, plan)
                and not consumed):
            action = {'kind': 'restart', 'branch': plan['branch']}
    elif plan is not None and recovery is not None and reason == 'verify_timeout_limit_insufficient':
        limit = str(block[2] or '')
        configured = None
        if any(row[1] == 'verify_timeout_seconds' for row in connection.execute('PRAGMA table_info(notification_session)')):
            current = connection.execute('SELECT verify_timeout_seconds FROM notification_session LIMIT 1').fetchone()
            configured = current[0] if current else None
        if (type(configured) is int and configured > 0
                and recovery['reason_code'] == reason and limit.isascii() and limit.isdecimal()
                and 0 < int(limit) == configured and plan['verify_result']
                and f'verify_timeout_seconds={limit}' in plan['verify_result']):
            action = {'kind': 'verify_timeout', 'seconds': int(limit)}
    elif evidence is not None and not evidence.get('user_targets') and not evidence.get('worker_decision'):
        routes = evidence.get('unavailable_recovery_routes')
        if (isinstance(routes, dict) and set(routes) == {
                'reobservation', 'retry', 'correction', 'condition_wait', 'next_run', 'safe_recovery'}
                and all(isinstance(value, str) and value.strip() for value in routes.values())):
            action = {'kind': 'unrecoverable', 'recovery_identity': recovery['identity']}
    if payload.get('action', action) != action:
        return None
    return action


def block_action(connection, repo, payload):
    try:
        return _block_action(connection, repo, payload)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def is_current(connection, repo, kind, payload):
    """Shared creation/claim boundary; callers provide already recorded facts."""
    try:
        if kind == 'blocked':
            action = block_action(connection, repo, payload)
            return action is not None and (action['kind'] != 'unrecoverable' or payload.get('action') == action)
        if kind == 'pr_created':
            row = connection.execute('SELECT status, pr_url FROM exec_plans WHERE repo=? AND plan_id=?',
                                     (repo, payload.get('execplan_id'))).fetchone()
            return bool(row and row[0] == 'PR_OPEN' and row[1] and row[1] == payload.get('pr_url'))
        if kind == 'draft_decisions':
            plans = payload['plans']
            if not plans or {item['id'] for item in plans} != execplan_draft_state.user_question_ids(connection, repo):
                return False
            for item in plans:
                saved = execplan_draft_state.evidence(connection, repo, item['id'])
                if (not isinstance(saved, dict) or not saved.get('pending') or saved['revision'] != item['decision_revision']
                        or saved.get('identity') != item['decision_identity']):
                    return False
            return True
        if kind == 'draft_decision':
            saved = execplan_draft_state.evidence(connection, repo, payload['execplan_id'])
            return bool(isinstance(saved, dict) and saved.get('pending') and saved.get('question')
                        and not saved.get('reassessment') and saved['revision'] == payload.get('decision_revision'))
    except (AttributeError, KeyError, TypeError, ValueError):
        # Malformed or unconfirmed evidence is preserved but cannot authorize a send.
        return False
    return False


def cancel_event(connection, event_key):
    connection.execute(
        'UPDATE notification_outbox SET canceled_at=CURRENT_TIMESTAMP, delivery_started_at=NULL '
        'WHERE event_key=? AND sent_at IS NULL AND canceled_at IS NULL', (event_key,))


def cancel_stale_events(connection, repo):
    for row in connection.execute(
        'SELECT event_key, event_type, payload FROM notification_outbox '
        'WHERE repo=? AND sent_at IS NULL AND canceled_at IS NULL', (repo,)).fetchall():
        try:
            payload = json.loads(row[2])
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict) or not is_current(connection, repo, row[1], payload):
            cancel_event(connection, row[0])
