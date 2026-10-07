"""Durable publication reservations shared by automatic and manual selection.

Callers check these inside BEGIN IMMEDIATE together with their own reservation.
Prepared intents survive process exit and remain owned through remote/DB recovery.
"""
from __future__ import annotations

from dataclasses import replace
import json

from .planning_publication_state import general_planning_publication


def publication_claims(connection, repo):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='general_planning_publications'").fetchone():
        return frozenset(), frozenset()
    intent = general_planning_publication(connection, repo)
    if intent is None:
        return frozenset(), frozenset()
    payload = json.loads(intent.manifest)
    ids = {str(plan['id']) for _path, plan in payload['plans']}
    ids.update(str(item['snapshot']['plan_id']) for item in payload['revisions'])
    ids.update(str(item['plan_id']) for item in payload['deletions'])
    paths = {str(path) for path in payload['publication_paths']}
    return frozenset(ids), frozenset(paths)


def publication_intake_sources(connection, repo):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='general_planning_publications'").fetchone():
        return frozenset()
    intent = general_planning_publication(connection, repo)
    if intent is None:
        return frozenset()
    payload = json.loads(intent.manifest)
    return frozenset(path for _name, plan in payload['plans'] for path in plan['sources'])


def affected_snapshot(result):
    ids = {item.snapshot.plan_id for item in (*result.revisions, *result.deletions)}
    return replace(result.mutable_snapshot,
                   plans=tuple(plan for plan in result.mutable_snapshot.plans if plan.plan_id in ids))


def assert_new_ids_available(connection, repo, ids):
    ids = set(ids)
    if not ids:
        return
    from .execplan_catalog_state import DefinitionStateRepository
    snapshot = DefinitionStateRepository(connection).capture(repo)
    used = {row.plan_id for row in snapshot.rows} | {key for key, _path in snapshot.reserved}
    collision = set(ids) & used
    if collision:
        raise RuntimeError('G ID 소유 예약 충돌: ' + ', '.join(sorted(collision)))


def assert_manual_scopes_available(connection, repo, result):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='manual_work_groups'").fetchone():
        return
    from .manual_work_state import live_requirement_paths
    collision = set(result.publication_paths) & live_requirement_paths(connection, repo)
    sources = {path for _name, plan in result.plans for path in plan['sources']}
    for row in connection.execute("SELECT requirements FROM manual_work_groups WHERE repo=? AND kind='submit' "
            "AND state NOT IN ('MERGED','PREPARATION_CONFLICT')", (repo,)):
        collision.update(sources & set(json.loads(row['requirements'])))
    if collision:
        raise RuntimeError('수동 제출·문서 소유 예약과 Planner 게시 충돌: ' + ', '.join(sorted(collision)))


def live_manual_tasks(connection, state_dir):
    """Called under the writer reservation before changing invocation records."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='manual_work_groups'").fetchone():
        return frozenset()
    from loop_manager_internal.agents.agent_invocation import InvocationContext
    from loop_manager_internal.agents.common_guidance import task_identity
    from loop_manager_internal.runtime.resource_lock import resource_is_held
    from pathlib import Path
    protected = set()
    for group in connection.execute("SELECT repo,group_id,requirements FROM manual_work_groups "
            "WHERE kind='submit' AND state NOT IN ('MERGED','PREPARATION_CONFLICT')"):
        if any(resource_is_held(state_dir, group['repo'], 'requirement:' + path)
               for path in json.loads(group['requirements'])):
            protected.add(task_identity(InvocationContext(group['repo'],None,
                task_id='manual-submit:'+group['group_id']), 'planner', Path(group['repo'])))
    return frozenset(protected)
