"""Explicit requests survive management saturation without stealing live calls."""
from __future__ import annotations

import json
from datetime import datetime, timezone


def ensure_schema(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS task_management_admissions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        repo TEXT NOT NULL, plan_id TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0, parked INTEGER NOT NULL DEFAULT 1,
        release_evidence TEXT, retry_order INTEGER, resumed INTEGER,
        pending INTEGER NOT NULL DEFAULT 0, handoff INTEGER NOT NULL DEFAULT 0,
        UNIQUE(repo,plan_id))''')

    columns = {row[1] for row in connection.execute('PRAGMA table_info(task_management_admissions)')}
    for name, declaration in [('retry_order', 'INTEGER'), ('resumed', 'INTEGER'),
                              ('pending', 'INTEGER NOT NULL DEFAULT 0'),
                              ('handoff', 'INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:
            connection.execute(f'ALTER TABLE task_management_admissions ADD COLUMN {name} {declaration}')
    if 'pending' not in columns:
        # Legacy rows prove a queued explicit request, but no RETRY order.
        connection.execute('UPDATE task_management_admissions SET pending=1 WHERE parked=1 AND priority>0')


def parked(connection, repo, plan_id):
    return connection.execute('SELECT 1 FROM task_management_admissions '
        'WHERE repo=? AND plan_id=? AND parked=1', (repo, plan_id)).fetchone() is not None


def explicit_request(connection, repo, plan_id):
    return connection.execute('SELECT 1 FROM task_management_admissions '
        'WHERE repo=? AND plan_id=? AND priority>0',(repo,plan_id)).fetchone() is not None


def request(connection, repo, plan_id):
    from .runtime_state import _transaction
    with _transaction(connection):
        sequence = connection.execute('SELECT COALESCE(MAX(priority),0)+1 FROM task_management_admissions').fetchone()[0]
        connection.execute('INSERT INTO task_management_admissions(repo,plan_id,priority,pending) VALUES (?,?,?,1) '
            'ON CONFLICT(repo,plan_id) DO UPDATE SET '
            'priority=CASE WHEN priority=0 THEN excluded.priority ELSE priority END,'
            'pending=CASE WHEN priority=0 THEN 1 ELSE pending END,'
            'parked=CASE WHEN priority=0 THEN 1 ELSE parked END', (repo,plan_id,sequence))


def reconcile(connection, repo, limit, *, busy=(), eligible=None):
    """Drain the last-ranked units; keep their physical reservations until return."""
    from loop_manager_internal.application.task_scheduling import Task
    from .runtime_state import _transaction
    with _transaction(connection):
        if connection.execute('SELECT 1 FROM task_management_admissions WHERE repo=?',(repo,)).fetchone() is None:
            return
        rows = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND status IN "
            "('IMPLEMENTING','VERIFYING','REVIEWING','PUBLISHING')", (repo,)).fetchall()
        groups = dict(connection.execute('SELECT plan_id,group_id FROM manual_work_members WHERE repo=?', (repo,)))
        units = {}
        for row in rows:
            if eligible is not None and parked(connection,repo,row["plan_id"]) and not eligible(row["plan_id"]):
                connection.execute('UPDATE task_management_admissions SET pending=CASE WHEN handoff=1 AND priority>0 THEN 1 ELSE pending END,'
                    'handoff=0 WHERE repo=? AND plan_id=?', (repo,row['plan_id']))
                continue
            units.setdefault(groups.get(row['plan_id'], row['plan_id']), []).append(row)
        ranked = sorted(units, key=lambda unit: min(
            (Task(repo, row['plan_id'], connection).rank(), row['plan_id']) for row in units[unit]))
        saved = {row['plan_id']: row for row in connection.execute(
            'SELECT * FROM task_management_admissions WHERE repo=?', (repo,))}
        def held(members):
            return any(row['plan_id'] not in saved or not saved[row['plan_id']]['parked']
                       or saved[row['plan_id']]['handoff'] for row in members)
        incumbents = {unit for unit in ranked if held(units[unit])}
        requests = {unit for unit in ranked if any(
            row['plan_id'] in saved and saved[row['plan_id']]['pending'] for row in units[unit])}
        # Only a fresh eligible request authorizes replacing incumbents.
        preferred = ranked[:limit]
        replacing = bool(set(preferred) & (requests - incumbents))
        selected_units = set(preferred) if replacing else set(incumbents)
        for unit in ranked:
            if len(selected_units) >= limit:
                break
            selected_units.add(unit)
        selected = {row['plan_id'] for unit in selected_units for row in units[unit]}
        occupied = set(busy) | {row[0] for row in connection.execute(
            'SELECT plan_id FROM task_call_reservations WHERE repo=?', (repo,))}
        draining = occupied - selected
        for members in units.values():
            for row in members:
                pid = row['plan_id']
                was_held = pid not in saved or not saved[pid]['parked']
                waiting = pid in selected and not was_held and bool(draining)
                connection.execute('INSERT INTO task_management_admissions(repo,plan_id,parked,handoff) VALUES (?,?,?,?) '
                    'ON CONFLICT(repo,plan_id) DO UPDATE SET parked=excluded.parked,handoff=excluded.handoff,'
                    'pending=CASE WHEN excluded.parked=0 OR excluded.handoff=1 THEN 0 ELSE pending END',
                    (repo,pid,int(pid not in selected or waiting),int(waiting)))


def release_blocks(connection, repo, plan_ids):
    from . import runtime_state
    from loop_manager_internal.recovery.scoped_blocking import clear_plan_block
    def apply(db):
        if db.execute('SELECT 1 FROM projects WHERE repo=? AND registered=1', (repo,)).fetchone() is None:
            raise ValueError('등록된 프로젝트가 아닙니다.')
        for pid in dict.fromkeys(plan_ids):
            row = db.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',(repo,pid)).fetchone()
            if row is None:
                raise ValueError(f'{pid}: 작업이 없습니다.')
            if row['status'] != 'PLAN_BLOCKED':
                continue
            block = db.execute('SELECT * FROM execplan_blocks WHERE repo=? AND plan_id=?',(repo,pid)).fetchone()
            if block is None or block['reason_code'] not in {'user_decision_required','common_defect_pending'}:
                raise ValueError(f'{pid}: 안전·소유권 차단은 원인별 복구가 필요합니다.')
            if db.execute('SELECT 1 FROM agent_invocations WHERE repo=? AND plan_id=? AND finished_at IS NULL', (repo,pid)).fetchone():
                raise ValueError(f'{pid}: 종료 미확인 호출이 있습니다.')
            previous = db.execute('SELECT release_evidence FROM task_management_admissions WHERE repo=? AND plan_id=?',(repo,pid)).fetchone()
            audit = json.loads(previous[0] or '[]') if previous else []
            audit.append({'at':datetime.now(timezone.utc).isoformat(), 'block':dict(block),
                'last_error':row['last_error'], 'validation_success':False})
            request(db,repo,pid)
            db.execute('UPDATE task_management_admissions SET release_evidence=?,pending=1,parked=1,handoff=0 WHERE repo=? AND plan_id=?',
                (json.dumps(audit,ensure_ascii=False),repo,pid))
            # It is queued, so restoring the stage does not acquire a management slot.
            runtime_state.RuntimeStateRepository(db).update_plan(repo,pid,plan_values={
                'status':'IMPLEMENTING','blocked_reason':None,'blocked_fingerprint':None,
                'blocked_worktree_fingerprint':None,'blocked_review_fingerprint':None,'last_error':None},enqueue=False)
            clear_plan_block(db,repo,pid)
        return list(dict.fromkeys(plan_ids))
    return connection.run_request(apply)
