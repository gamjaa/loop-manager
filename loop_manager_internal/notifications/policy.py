"""Current user actions from durable observations, without external effects."""
from __future__ import annotations

import json
import re
from urllib.parse import quote, unquote, urlsplit

from loop_manager_internal.documents import reviewer_specification
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.storage import execplan_draft_state
from .rendering import _checkpoint_link, _pieces


SOURCE_ACTION_REASONS = frozenset({'guidance_approval_required', 'review_specification_deadlock'})
PR_REVIEW_ACTION = 'review_merge'


def pr_review_candidate(connection, repo, plan_id, pr_url=None, *, creation=False):
    """Return a confirmed published review candidate and its remaining user action."""
    try:
        row = connection.execute(
            'SELECT status, pr_url, pr_head_oid, validated_base_sha, '
            'definition_snapshot, review_result, review_token, publication_token '
            'FROM exec_plans WHERE repo=? AND plan_id=?',
            (repo, plan_id),
        ).fetchone()
        if (row is None or row['status'] != 'PR_OPEN' or not row['pr_url']
                or (pr_url is not None and row['pr_url'] != pr_url)):
            return None
        head = str(row['pr_head_oid'] or '')
        base = str(row['validated_base_sha'] or '')
        if not head or not base or not row['review_result']:
            return None
        definition = DefinitionSnapshot.deserialize(row['definition_snapshot'])
        if definition is None:
            return None
        review_token = definition.review_token(base, head)
        publication_token = definition.publication_token(base, head)
        if (row['review_token'] != review_token
                or row['publication_token'] != publication_token):
            return None
        proof = connection.execute(
            'SELECT publication_token, review_input_identity FROM pr_review_candidates '
            'WHERE repo=? AND plan_id=?', (repo, plan_id),
        ).fetchone()
        valid_proof = bool(proof and proof['publication_token'] == publication_token
                           and proof['review_input_identity'])
        if creation:
            saved = connection.execute(
                'SELECT review_input_identity FROM pr_creation_publications '
                'WHERE repo=? AND plan_id=? AND pr_url=?',
                (repo, plan_id, row['pr_url']),
            ).fetchone()
            if saved is None:
                return None
            # 최초 identity가 없으면 이후 증명만으로 후보 변경을 단정할 수 없다.
            if (saved[0] and valid_proof and proof['review_input_identity'] != saved[0]):
                return None
            return {
                'notice_kind': 'creation',
                'candidate_identity': 'creation:' + str(row['pr_url']),
                'candidate_head_oid': head,
                'candidate_publication_token': publication_token,
                'user_action': PR_REVIEW_ACTION,
            }
        if not valid_proof:
            return None
        return {
            'candidate_identity': str(proof['review_input_identity']),
            'candidate_head_oid': head,
            'candidate_publication_token': publication_token,
            'user_action': PR_REVIEW_ACTION,
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        # Missing or malformed proof cannot authorize a PR notification.
        return None


def block_recovery(connection, repo, plan_id, scope):
    """Keep pending same-scope recovery authoritative; retain independent plan proof."""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'").fetchone()
    row = (connection.execute('SELECT * FROM recovery_records WHERE repo=? AND subject=?',
                              (repo, plan_id or '__project__')).fetchone() if exists else None)
    if row is not None and row['pending_action'] in {'final_block', 'reconcile'}:
        from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode, reason_scope
        try:
            evidence = json.loads(row['final_evidence'])
            recorded_scope = reason_scope(BlockReasonCode(evidence['block_reason_code'])).value
        except (KeyError, TypeError, ValueError):
            return row  # Malformed evidence must still fail the action validation.
        if recorded_scope != scope:
            row = None
    if row is None and scope == 'plan' and any(
            column[1] == 'recovery_snapshot' for column in connection.execute('PRAGMA table_info(execplan_blocks)')):
        saved = connection.execute('SELECT recovery_snapshot FROM execplan_blocks WHERE repo=? AND plan_id=?',
                                   (repo, plan_id)).fetchone()
        try:
            snapshot = json.loads(saved[0]) if saved else None
        except (TypeError, ValueError):
            return None
        if isinstance(snapshot, dict) and all(key in snapshot for key in (
                'identity', 'reason_code', 'phase', 'plan_revision', 'base_revision',
                'fingerprint', 'progress_marker', 'detail', 'pending_action', 'final_evidence')):
            return snapshot
    return row


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
    recovery = block_recovery(connection, repo, plan_id, scope)
    evidence = None
    if recovery is not None:
        # Pending recovery and inconsistent observations are insufficient even
        # when a still-visible block carries an explicit-looking question.
        if recovery['pending_action'] != 'final_block' or recovery['detail'] != block[1]:
            return None
        # Recovery fingerprints identify the failure; block release fingerprints
        # identify the input/configuration to recheck. Native Worker decisions
        # intentionally use different values, so validate the recovery's own identity.
        from loop_manager_internal.recovery.recovery_runtime import RecoveryObservation, RecoveryReason
        observed = RecoveryObservation(RecoveryReason(recovery['reason_code']), repo, plan_id,
            recovery['phase'], recovery['plan_revision'], recovery['base_revision'],
            recovery['fingerprint'], recovery['progress_marker'], recovery['detail'])
        if observed.identity != recovery['identity']:
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
    if reason == 'common_defect_pending':
        return None  # A correction condition is not a user action.
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
        from loop_manager_internal.recovery.recovery_runtime import unavailable_routes_complete
        if reason == 'managed_worktree_conflict':
            from loop_manager_internal.storage.recovery_state import conflict_evidence_current
            proof = evidence.get('conflict_evidence')
            if (plan is None or plan['status'] != 'PLAN_BLOCKED'
                    or recovery['reason_code'] != 'managed_conflict_unrecoverable'
                    or not conflict_evidence_current(connection, repo, plan_id, proof)
                    or payload.get('recovery', {}).get('conflict_evidence', proof) != proof):
                return None
        if unavailable_routes_complete(routes):
            action = {'kind': 'unrecoverable', 'recovery_identity': recovery['identity']}
            if reason == 'managed_worktree_conflict':
                action['conflict_evidence'] = proof
    if payload.get('action', action) != action:
        return None
    return action


def block_action(connection, repo, payload):
    try:
        return _block_action(connection, repo, payload)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _draft_question_key(item):
    plan_id = item.get('id', item.get('execplan_id'))
    identity = item.get('decision_identity')
    revision = item.get('decision_revision')
    if not all(isinstance(value, str) and value.strip()
               for value in (plan_id, identity, revision)):
        return None
    return plan_id, identity, revision


def _draft_source_path(item):
    path = item.get('source_path', item.get('path'))
    if isinstance(path, str) and path:
        return path
    url = item.get('url')
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    parts = unquote(parsed.path).lstrip('/').split('/')
    if parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com' or len(parts) < 5:
        return None
    if parts[2] != 'blob' or not parts[3]:
        return None
    return '/'.join(parts[4:])


def _valid_draft_source_path(path, plan_id):
    return isinstance(path, str) and bool(re.fullmatch(
        rf'docs/plans/\[{re.escape(plan_id)}\] .+\.md', path,
    ))


def _current_draft_question(connection, repo, plan_id):
    try:
        saved = execplan_draft_state.evidence(connection, repo, plan_id)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if (not isinstance(saved, dict) or not saved.get('pending')
            or not isinstance(saved.get('question'), str) or not saved['question'].strip()
            or saved.get('reassessment')):
        return None
    key = _draft_question_key({
        'id': plan_id, 'decision_identity': saved.get('identity'),
        'decision_revision': saved.get('revision'),
    })
    if key is None:
        return None
    plan = connection.execute(
        'SELECT path, title, COALESCE(current_revision_sha, plan_sha) AS revision '
        'FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan_id),
    ).fetchone()
    project = connection.execute('SELECT origin FROM projects WHERE repo=?', (repo,)).fetchone()
    if (plan is None or project is None or saved.get('path') != plan['path']
            or not _valid_draft_source_path(str(plan['path']), plan_id)):
        return None
    revision = str(plan['revision'] or '')
    link = _checkpoint_link(project['origin'], revision)
    url = None
    if link:
        origin = link.split('/tree/', 1)[0]
        url = f"{origin}/blob/{revision}/{quote(str(plan['path']), safe='/')}"
    return {
        'id': plan_id, 'title': str(plan['title']), 'url': url,
        'source_path': str(plan['path']), 'decision_identity': key[1],
        'decision_revision': key[2],
    }


def _current_draft_questions(connection, repo):
    try:
        plan_ids = sorted(execplan_draft_state.user_question_ids(connection, repo))
    except (AttributeError, KeyError, TypeError, ValueError):
        return []
    return [snapshot for plan_id in plan_ids
            if (snapshot := _current_draft_question(connection, repo, plan_id)) is not None]


def _confirmed_draft_question_keys(connection, repo, *, exclude_event_key=None):
    rows = connection.execute(
        "SELECT event_key, event_type, payload, rendered_text, confirmed_pieces "
        "FROM notification_outbox WHERE repo=? AND event_type IN ('draft_decisions', 'draft_decision')",
        (repo,),
    ).fetchall()
    confirmed = set()
    for row in rows:
        if str(row['event_key']) == exclude_event_key or int(row['confirmed_pieces'] or 0) <= 0:
            continue
        try:
            payload = json.loads(str(row['payload']))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or not isinstance(row['rendered_text'], str):
            continue
        rendered_text = row['rendered_text']
        confirmed_text = ''.join(_pieces(rendered_text)[:int(row['confirmed_pieces'])])
        if row['event_type'] == 'draft_decisions':
            for item in payload.get('plans', []):
                if not isinstance(item, dict):
                    continue
                key = _draft_question_key(item)
                source_path = _draft_source_path(item)
                if key is None or not _valid_draft_source_path(source_path, key[0]):
                    continue
                marker = f"\n• {key[0]} · "
                position = rendered_text.find(marker)
                id_end = position + len(f"\n• {key[0]}")
                if position >= 0 and id_end <= len(confirmed_text):
                    confirmed.add(key)
        else:
            key = _draft_question_key(payload)
            source_path = _draft_source_path(payload)
            question = payload.get('question')
            if (key is not None and _valid_draft_source_path(source_path, key[0])
                    and isinstance(question, str) and question
                    and f"{key[0]}: " in confirmed_text and question in confirmed_text):
                confirmed.add(key)
    return confirmed


def confirmed_publication_keys(connection, repo, kind, *, exclude_event_key=None):
    """Identify published items whose ID was included in a confirmed piece."""
    confirmed = set()
    rows = connection.execute(
        'SELECT event_key, payload, rendered_text, confirmed_pieces FROM notification_outbox '
        'WHERE repo=? AND event_type=? AND confirmed_pieces>0', (repo, kind),
    ).fetchall()
    for row in rows:
        if str(row['event_key']) == exclude_event_key or not isinstance(row['rendered_text'], str):
            continue
        try:
            payload = json.loads(row['payload'])
            rendered = row['rendered_text']
            limit = len(''.join(_pieces(rendered)[:int(row['confirmed_pieces'])]))
            for item in payload['plans']:
                plan_id = item['id']
                revision = item.get('publication_revision', payload.get('publication_revision'))
                if not isinstance(plan_id, str) or not isinstance(revision, str) or not revision:
                    continue
                position = rendered.find(f'\n• {plan_id} · ')
                if position >= 0 and position + len(f'\n• {plan_id}') <= limit:
                    confirmed.add((plan_id, revision))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return confirmed


def _pending_legacy_draft_question_keys(connection, repo):
    rows = connection.execute(
        "SELECT payload FROM notification_outbox WHERE repo=? AND event_type='draft_decision' "
        "AND sent_at IS NULL AND canceled_at IS NULL", (repo,),
    ).fetchall()
    pending = set()
    for row in rows:
        try:
            payload = json.loads(str(row['payload']))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        plan_id = payload.get('execplan_id')
        current = (_current_draft_question(connection, repo, plan_id)
                   if isinstance(plan_id, str) else None)
        if current and _matches_current_draft_item(connection, repo, payload, current, legacy=True):
            pending.add(_draft_question_key(current))
    return pending


def draft_questions_for_notice(connection, repo):
    """Current questions not already confirmed or owned by a valid legacy notice."""
    confirmed = _confirmed_draft_question_keys(connection, repo)
    legacy = _pending_legacy_draft_question_keys(connection, repo)
    return [item for item in _current_draft_questions(connection, repo)
            if _draft_question_key(item) not in confirmed | legacy]


def _matches_current_draft_item(connection, repo, item, current, *, legacy=False):
    if _draft_question_key(item) != _draft_question_key(current):
        return False
    source_path = _draft_source_path(item)
    if source_path != current['source_path'] or item.get('url') != current['url']:
        return False
    if legacy:
        return item.get('question') == (execplan_draft_state.evidence(
            connection, repo, current['id'],
        ) or {}).get('question')
    return item.get('title') == current['title']


def is_current(connection, repo, kind, payload, *, event_key=None):
    """Shared creation/claim boundary; callers provide already recorded facts."""
    try:
        if kind == 'plans_published':
            plans = payload['plans']
            revision = payload['publication_revision']
            if not isinstance(plans, list) or not plans or not isinstance(revision, str) or not revision:
                return False
            confirmed = (confirmed_publication_keys(connection, repo, kind, exclude_event_key=event_key)
                         if event_key else set())
            for item in plans:
                if not isinstance(item, dict) or not _valid_draft_source_path(
                        item.get('publication_path', item.get('source_path')), item.get('id', '')):
                    return False
                if (item['id'], revision) in confirmed:
                    return False
                row = connection.execute(
                    'SELECT path, publication_path, publication_sha, status FROM exec_plans '
                    'WHERE repo=? AND plan_id=?', (repo, item['id']),
                ).fetchone()
                if (row is None or not _valid_draft_source_path(row['path'], item['id'])
                        or row['publication_path'] != item.get('publication_path', item.get('source_path'))
                        or row['publication_sha'] != revision or row['status'] == 'CANCELED'):
                    return False
            return True
        if kind == 'blocked':
            action = block_action(connection, repo, payload)
            return action is not None and (action['kind'] != 'unrecoverable' or payload.get('action') == action)
        if kind == 'pr_created':
            candidate = pr_review_candidate(
                connection, repo, payload.get('execplan_id'), payload.get('pr_url'),
                creation=payload.get('notice_kind') == 'creation',
            )
            return bool(candidate and all(
                payload.get(key) == candidate[key]
                for key in ('candidate_identity', 'user_action')
            ))
        if kind == 'draft_decisions':
            plans = payload['plans']
            if not isinstance(plans, list) or not plans:
                return False
            confirmed = _confirmed_draft_question_keys(connection, repo, exclude_event_key=event_key)
            legacy = _pending_legacy_draft_question_keys(connection, repo)
            confirmed_creations = (confirmed_publication_keys(connection, repo, kind, exclude_event_key=event_key)
                                   if event_key else set())
            expected = [item for item in _current_draft_questions(connection, repo)
                        if _draft_question_key(item) not in confirmed | legacy]
            questions = [item for item in plans if isinstance(item, dict) and _draft_question_key(item)]
            if len(questions) != len(expected) or any(
                    not _matches_current_draft_item(connection, repo, item, current)
                    for item, current in zip(questions, expected)):
                return False
            for item in plans:
                if not isinstance(item, dict):
                    return False
                revision = item.get('publication_revision')
                if revision is not None:
                    if (item.get('id'), revision) in confirmed_creations:
                        return False
                    if not is_current(connection, repo, 'plans_published',
                                      {'plans': [item], 'publication_revision': revision}):
                        return False
                elif not _draft_question_key(item):
                    return False
            if [item.get('id') for item in plans] != sorted(item['id'] for item in plans):
                return False
            return True
        if kind == 'draft_decision':
            plan_id = payload.get('execplan_id')
            current = (_current_draft_question(connection, repo, plan_id)
                       if isinstance(plan_id, str) else None)
            if current is None or not _matches_current_draft_item(
                    connection, repo, payload, current, legacy=True):
                return False
            saved = execplan_draft_state.evidence(connection, repo, plan_id)
            if payload.get('question') != saved.get('question'):
                return False
            return True
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
        if not isinstance(payload, dict) or not is_current(
                connection, repo, row[1], payload, event_key=str(row[0])):
            if row[1] == 'plans_published':
                from .repository import _enabled
                if not _enabled(connection):
                    continue
                from .events import retain_unconfirmed_published_plans
                retain_unconfirmed_published_plans(connection, repo, str(row[0]))
            if (row[1] == 'draft_decisions' and isinstance(payload, dict)
                    and isinstance(payload.get('plans'), list)
                    and any(isinstance(item, dict) and item.get('publication_revision')
                            for item in payload['plans'])):
                from .repository import _enabled
                if not _enabled(connection):
                    continue
                from .events import enqueue_draft_decisions
                enqueue_draft_decisions(connection, repo)
            cancel_event(connection, row[0])
