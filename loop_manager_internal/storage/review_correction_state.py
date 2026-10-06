"""Durable linkage from a Review feedback turn to its verified candidate."""
from __future__ import annotations

import json
import sqlite3

from loop_manager_internal.storage.runtime_state import RuntimeStateRepository


def deserialize(value: object) -> dict:
    state = json.loads(str(value or '{}'))
    if not isinstance(state, dict):
        raise RuntimeError('저장된 Review 교정 상태가 유효하지 않습니다.')
    observation = state.get('observation')
    if observation is not None and (
        not isinstance(observation, dict)
        or type(observation.get('level')) is not int or not 1 <= observation['level'] <= 8
        or not isinstance(observation.get('head'), str) or not observation['head']
    ):
        raise RuntimeError('저장된 Review 관측 identity가 유효하지 않습니다.')
    # An observation-only value is staged and consumed inside the same transaction
    # for the first baseline, including an obsolete-level result.
    if state and set(state) != {'observation'}:
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
                        or not isinstance(event.get('verified_head'), str) or not event['verified_head']))):
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
    if not state:
        return {}
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
    if not state:
        return  # Legacy feedback has no trustworthy baseline linkage yet.
    pending = state.get('pending')
    if source is not None:
        pending = {'source': source, 'level': level, 'completed': False, 'verified_head': None}
    if pending is not None:
        pending.update(completed=False, verified_head=None)
        state['pending'] = pending
        save(connection, repo, plan_id, state)


def complete_worker(connection: sqlite3.Connection, repo: str, plan_id: str, level: int) -> None:
    state = load(connection, repo, plan_id)
    pending = state.get('pending')
    if pending is not None and pending['level'] == level:
        pending.update(completed=True, verified_head=None)
        save(connection, repo, plan_id, state)


def verified(connection: sqlite3.Connection, repo: str, plan_id: str, head: str) -> None:
    state = load(connection, repo, plan_id)
    pending = state.get('pending')
    if pending is not None and pending['completed']:
        pending['verified_head'] = head
        save(connection, repo, plan_id, state)
