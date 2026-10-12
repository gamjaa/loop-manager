"""Durable linkage from a Review feedback turn to its verified candidate."""
from __future__ import annotations

import json
import sqlite3

from loop_manager_internal.storage.runtime_state import RuntimeStateRepository


def deserialize(value: object) -> dict:
    state = json.loads(str(value or '{}'))
    if not isinstance(state, dict):
        raise RuntimeError('저장된 Review 교정 상태가 유효하지 않습니다.')
    candidate = state.get('candidate')
    for turn in (candidate, state.get('pending')):
        if isinstance(turn, dict) and 'partial_head' in turn and (
                not isinstance(turn['partial_head'], str) or not turn['partial_head']):
            raise RuntimeError('저장된 부분 검증 후보가 유효하지 않습니다.')
    if candidate is not None and (
        not isinstance(candidate, dict)
        or type(candidate.get('level')) is not int or not 1 <= candidate['level'] <= 8
        or type(candidate.get('completed')) is not bool
        or (candidate.get('verified_head') is not None and (
            not isinstance(candidate['verified_head'], str) or not candidate['verified_head']))
    ):
        raise RuntimeError('저장된 Worker 검증 후보가 유효하지 않습니다.')
    # A candidate can precede the first FAIL or survive lost Review feedback.
    if state.keys() - {'candidate'}:
        if (state.get('version') != 1
                or type(state.get('level')) is not int
                or not 1 <= state['level'] <= 8
                or type(state.get('closed', False)) is not bool
                or not isinstance(state.get('audit', []), list)
                or not isinstance(state.get('baseline'), list)
                or any(not isinstance(issue, str) or not issue for issue in state['baseline'])
                or type(state.get('failures')) is not int or state['failures'] < 0
                or not isinstance(state.get('issues'), dict)
                or any(not isinstance(issue, str) or not issue or type(count) is not int or count < 1
                       for issue, count in state['issues'].items())):
            raise RuntimeError('저장된 Review 교정 집계가 유효하지 않습니다.')
        for event in state.get('audit', []):
            if (not isinstance(event, dict) or event.get('kind') not in {'baseline', 'correction'}
                    or type(event.get('level')) is not int or not 1 <= event['level'] <= 8
                    or not isinstance(event.get('issues'), list)
                    or any(not isinstance(issue, str) or not issue for issue in event['issues'])
                    or type(event.get('effective_level')) is not int
                    or not event['level'] <= event['effective_level'] <= min(8, event['level'] + 1)
                    or (event['kind'] == 'correction' and (
                        type(event.get('source_review_id')) is not int or event['source_review_id'] < 1
                        or not (isinstance(event.get('verified_head'), str) and event['verified_head']
                                or isinstance(event.get('partial_head'), str) and event['partial_head'])))):
                raise RuntimeError('저장된 Review 교정 감사가 유효하지 않습니다.')
        pending = state.get('pending')
        if pending is not None and (
            not isinstance(pending, dict)
            or type(pending.get('source')) is not int or pending['source'] < 1
            or type(pending.get('level')) is not int or not 1 <= pending['level'] <= 8
            or type(pending.get('completed')) is not bool
            or (pending.get('verified_head') is not None
                and (not isinstance(pending['verified_head'], str) or not pending['verified_head']))
        ):
            raise RuntimeError('저장된 Review 교정 turn이 유효하지 않습니다.')
    return state


def end_episode(state: dict, effective_level: int) -> dict:
    """PASS clears the live streak, preserving completed-turn linkage and baselines."""
    if 'level' not in state:
        return state
    return {**state, 'level': effective_level, 'closed': True,
            'baseline': [], 'failures': 0, 'issues': {}, 'pending': None}


def load(connection: sqlite3.Connection, repo: str, plan_id: str) -> dict:
    row = connection.execute(
        'SELECT review_correction_state FROM exec_plans WHERE repo = ? AND plan_id = ?',
        (repo, plan_id),
    ).fetchone()
    if row is None:
        raise RuntimeError('Review 교정 상태의 ExecPlan이 없습니다.')
    return deserialize(row[0])


def save(connection: sqlite3.Connection, repo: str, plan_id: str, state: dict) -> None:
    RuntimeStateRepository(connection).update_plan(
        repo, plan_id, plan_values={'review_correction_state': json.dumps(state, sort_keys=True)},
        enqueue=False,
    )


def begin_worker(connection: sqlite3.Connection, repo: str, plan_id: str,
                 level: int, source: int | None) -> None:
    """Retries and verify remediation retain the same feedback identity."""
    state = load(connection, repo, plan_id)
    state['candidate'] = {'level': level, 'completed': False, 'verified_head': None}
    pending = state.get('pending')
    if source is not None and 'level' in state:
        pending = {'source': source, 'level': level, 'completed': False, 'verified_head': None}
    if pending is not None:
        pending.update(completed=False, verified_head=None)
        pending.pop('partial_head', None)
        state['pending'] = pending
    save(connection, repo, plan_id, state)


def complete_worker(connection: sqlite3.Connection, repo: str, plan_id: str, level: int) -> None:
    state = load(connection, repo, plan_id)
    candidate = state.get('candidate')
    pending = state.get('pending')
    if candidate is not None and candidate['level'] == level:
        candidate.update(completed=True, verified_head=None)
        candidate.pop('partial_head', None)
    if pending is not None and pending['level'] == level:
        pending.update(completed=True, verified_head=None)
        pending.pop('partial_head', None)
    if candidate is not None or pending is not None:
        save(connection, repo, plan_id, state)


def verified(connection: sqlite3.Connection, repo: str, plan_id: str, head: str) -> None:
    state = load(connection, repo, plan_id)
    candidate = state.get('candidate')
    pending = state.get('pending')
    if candidate is not None and candidate['completed']:
        candidate['verified_head'] = head
    if pending is not None and pending['completed']:
        pending['verified_head'] = head
    if candidate is not None or pending is not None:
        save(connection, repo, plan_id, state)


def partially_verified(connection, repo, plan_id, head):
    """Link a completed correction to partial evidence without setting verified_head."""
    state = load(connection, repo, plan_id)
    changed = False
    for key in ('candidate', 'pending'):
        turn = state.get(key)
        if turn and turn['completed']:
            turn['partial_head'] = head
            changed = True
    if changed:
        save(connection, repo, plan_id, state)
