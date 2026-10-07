"""Durable manual intake, group ownership and candidate-specific stage receipts.

Execution rows remain authoritative. This journal records intent before Git effects
and keeps user waivers out of Worker results and the Agent invocation ledger.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from loop_manager_internal.storage import runtime_state

WAITING_STATUSES = frozenset({"MANUAL_WORKING", "MANUAL_PENDING"})
TERMINAL = frozenset({"MERGED", "PR_OPEN"})


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS manual_work_groups (
        repo TEXT NOT NULL, group_id TEXT NOT NULL, kind TEXT NOT NULL,
        source_branch TEXT NOT NULL, requirements TEXT NOT NULL,
        source_head TEXT NOT NULL, base_sha TEXT NOT NULL,
        state TEXT NOT NULL, members TEXT NOT NULL, documents TEXT NOT NULL,
        owner_id TEXT, branch TEXT, worktree TEXT, prepared_head TEXT,
        active_id TEXT, request_id TEXT, last_error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(repo, group_id), UNIQUE(repo, kind, source_branch, requirements)
    )""")
    columns = {row[1] for row in connection.execute('PRAGMA table_info(manual_work_groups)')}
    for name, definition in (('resume_phase', "TEXT NOT NULL DEFAULT 'VERIFYING'"),
                             ('initial_source_head', "TEXT"), ('pending_source_head', 'TEXT'),
                             ('source_update_candidate','TEXT'),
                             ('source_update_conflict', 'INTEGER NOT NULL DEFAULT 0'),
                             ('workspace_created','INTEGER NOT NULL DEFAULT 0'),
                             ('definition_head','TEXT')):
        if name not in columns:
            connection.execute(f'ALTER TABLE manual_work_groups ADD COLUMN {name} {definition}')
    connection.execute("""CREATE TABLE IF NOT EXISTS manual_work_publications (
        repo TEXT NOT NULL, group_id TEXT NOT NULL, pr_url TEXT NOT NULL,
        head TEXT NOT NULL, base_sha TEXT NOT NULL, evidence TEXT NOT NULL,
        PRIMARY KEY(repo, group_id)
    )""")
    if 'input_token' not in {row[1] for row in connection.execute('PRAGMA table_info(manual_work_publications)')}:
        connection.execute('ALTER TABLE manual_work_publications ADD COLUMN input_token TEXT')
    if 'request_id' not in {row[1] for row in connection.execute('PRAGMA table_info(manual_work_publications)')}:
        connection.execute('ALTER TABLE manual_work_publications ADD COLUMN request_id TEXT')
    connection.execute("""CREATE TABLE IF NOT EXISTS manual_work_members (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, group_id TEXT NOT NULL,
        PRIMARY KEY(repo, plan_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS manual_work_requests (
        request_id TEXT PRIMARY KEY, repo TEXT NOT NULL, group_id TEXT NOT NULL,
        source_head TEXT NOT NULL, base_sha TEXT NOT NULL, input_token TEXT NOT NULL,
        skip_verify INTEGER NOT NULL, skip_review INTEGER NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS manual_work_results (
        repo TEXT NOT NULL, group_id TEXT NOT NULL, plan_id TEXT NOT NULL,
        stage TEXT NOT NULL, head TEXT NOT NULL, base_sha TEXT NOT NULL,
        input_token TEXT NOT NULL, result TEXT NOT NULL,
        detail TEXT NOT NULL, request_id TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(repo, group_id, plan_id, stage, head, base_sha, input_token, result)
    )""")


def manual_workspace_owner(connection, repo, plan_id):
    """Resolve shared resources only through the complete SQLite membership proof."""
    if not isinstance(connection,sqlite3.Connection):
        return plan_id
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_work_members'").fetchone():
        return plan_id
    return ManualWorkRepository(connection).workspace_owner(repo, plan_id)


def live_requirement_paths(connection, repo):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_work_groups'").fetchone():
        return frozenset()
    return frozenset(source for group in connection.execute(
        "SELECT requirements,documents FROM manual_work_groups WHERE repo=? AND state NOT IN ('MERGED','PREPARATION_CONFLICT')",(repo,))
        for source in (*json.loads(group['requirements']),
            *(path for item in json.loads(group['documents']) for path in item['plan']['sources'])))


class ManualWorkRepository:
    def __init__(self, connection):
        self.connection = connection
        ensure_schema(connection)

    def get(self, repo, group_id):
        return self.connection.execute("SELECT * FROM manual_work_groups WHERE repo=? AND group_id=?",
                                       (repo, group_id)).fetchone()

    def for_plan(self, repo, plan_id):
        return self.connection.execute("SELECT g.* FROM manual_work_groups g JOIN manual_work_members m "
            "ON g.repo=m.repo AND g.group_id=m.group_id WHERE m.repo=? AND m.plan_id=?",
            (repo, plan_id)).fetchone()

    def for_source(self, repo, branch, requirements):
        return self.connection.execute("SELECT * FROM manual_work_groups WHERE repo=? AND kind='submit' "
            "AND source_branch=? AND requirements=?", (repo, branch, json.dumps(sorted(requirements)))).fetchone()

    def pending(self, repo):
        return self.connection.execute("SELECT * FROM manual_work_groups WHERE repo=? "
            "AND state IN ('GENERATING','REGISTERING','AUTOMATIC') AND request_id IS NOT NULL ORDER BY rowid", (repo,)).fetchall()

    def create(self, repo, kind, branch, requirements, head, base, *, members=(), documents=()):
        group_id = uuid4().hex
        self.connection.execute("INSERT INTO manual_work_groups "
            "(repo,group_id,kind,source_branch,requirements,source_head,base_sha,state,members,documents) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (repo, group_id, kind, branch, json.dumps(sorted(requirements)),
            head, base, 'STARTING' if kind == 'start' else 'GENERATING', json.dumps(list(members)),
            json.dumps(list(documents), ensure_ascii=False)))
        for plan_id in members:
            self.connection.execute("INSERT INTO manual_work_members VALUES (?,?,?)", (repo, plan_id, group_id))
        self.connection.execute('UPDATE manual_work_groups SET initial_source_head=? WHERE repo=? AND group_id=?',
                                (head,repo,group_id))
        return self.get(repo, group_id)

    def update(self, group, **values):
        allowed = {'state','members','documents','owner_id','branch','worktree','prepared_head',
                   'active_id','request_id','last_error','attempts','source_head','base_sha',
                   'resume_phase','pending_source_head','source_update_candidate','source_update_conflict',
                   'workspace_created','definition_head'}
        if not values or not set(values) <= allowed:
            raise ValueError('invalid manual group update')
        self.connection.execute("UPDATE manual_work_groups SET " + ','.join(f'{key}=?' for key in values)
            + " WHERE repo=? AND group_id=?", (*values.values(), group['repo'], group['group_id']))
        return self.get(group['repo'], group['group_id'])

    def accept(self, group, *, head, base, input_token, skip_verify=False, skip_review=False, reason=''):
        values = (group['repo'], group['group_id'], head, base, input_token,
                  int(skip_verify), int(skip_review), reason)
        current = self.request(group)
        if current and tuple(current[key] for key in ('repo','group_id','source_head','base_sha',
                'input_token','skip_verify','skip_review','reason')) == values:
            return current
        request_id = uuid4().hex
        self.connection.execute("INSERT INTO manual_work_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
            (request_id, *values, datetime.now(timezone.utc).isoformat()))
        self.update(group, request_id=request_id)
        return self.connection.execute("SELECT * FROM manual_work_requests WHERE request_id=?", (request_id,)).fetchone()

    def request(self, group):
        return self.connection.execute("SELECT * FROM manual_work_requests WHERE request_id=?",
                                       (group['request_id'],)).fetchone()

    def record(self, group, stage, head, base, token, result, detail, *, plan_ids=None):
        if result not in {'NOT_RUN','PASS','FAIL','SKIPPED_BY_USER'} or stage not in {'verify','review'}:
            raise ValueError('invalid manual stage receipt')
        for plan_id in plan_ids or json.loads(group['members']):
            self.connection.execute("INSERT OR IGNORE INTO manual_work_results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (group['repo'], group['group_id'], plan_id, stage, head, base, token, result,
                 detail, group['request_id'] or '', datetime.now(timezone.utc).isoformat()))
        self.connection.commit()

    def failed(self, group, stage, head, base, token):
        return self.connection.execute("SELECT 1 FROM manual_work_results WHERE repo=? AND group_id=? "
            "AND stage=? AND head=? AND base_sha=? AND input_token=? AND result='FAIL' LIMIT 1",
            (group['repo'],group['group_id'],stage,head,base,token)).fetchone() is not None

    def workspace_owner(self, repo, plan_id):
        group = self.for_plan(repo, plan_id)
        if group is None:
            return plan_id
        members = json.loads(group['members'])
        stored = [row[0] for row in self.connection.execute("SELECT plan_id FROM manual_work_members "
            "WHERE repo=? AND group_id=? ORDER BY plan_id", (repo, group['group_id']))]
        if sorted(members) != stored or plan_id not in members or group['owner_id'] not in members:
            raise runtime_state.RuntimeStateError('수동 제출 묶음의 전체 소유 목록이 다릅니다.')
        return group['owner_id']

    def publish_group(self, repo, plan_id, values):
        group = self.for_plan(repo, plan_id)
        if group is None:
            return
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        head = values.get('pr_head_oid')
        selected = runtime.get(repo, plan_id)
        from loop_manager_internal.execution.manual_candidate import candidate_token
        token = candidate_token(self.connection,repo,head,selected.validated_base_sha,json.loads(group['documents']))
        stage_tokens={stage:digest((token,stage,selected.verify_result if stage=='review' else None,group['members']))
                      for stage in ('verify','review')}
        evidence = [row for row in self.connection.execute("SELECT plan_id,stage,result,input_token FROM manual_work_results "
            "WHERE repo=? AND group_id=? AND head=? AND base_sha=? AND result IN ('PASS','SKIPPED_BY_USER')",
            (repo,group['group_id'],head,selected.validated_base_sha)).fetchall()
            if row['input_token']==stage_tokens[row['stage']]]
        covered = {(row['plan_id'],row['stage']) for row in evidence}
        required = {(member,stage) for member in json.loads(group['members']) for stage in ('verify','review')}
        if not head or not required <= covered:
            raise runtime_state.RuntimeStateError('묶음 전체 G의 최종 후보 검증·Review 근거가 없습니다.')
        self.connection.execute('INSERT INTO manual_work_publications VALUES (?,?,?,?,?,?,?,?) '
            'ON CONFLICT(repo,group_id) DO UPDATE SET pr_url=excluded.pr_url,head=excluded.head,'
            'base_sha=excluded.base_sha,evidence=excluded.evidence,input_token=excluded.input_token,request_id=excluded.request_id',
            (repo,group['group_id'],values['pr_url'],head,selected.validated_base_sha,
             json.dumps([dict(row) for row in evidence]),token,group['request_id']))
        for member in json.loads(group['members']):
            if member != plan_id:
                runtime.update_plan(repo, member, plan_values={'status':'PR_OPEN',
                    'verify_result':selected.verify_result,'review_result':selected.review_result,
                    'validated_base_sha':selected.validated_base_sha,**values}, enqueue=False)
        self.update(group, state='PR_OPEN', active_id=None)

    def mark_group_merged(self, repo, plan_id):
        group = self.for_plan(repo, plan_id)
        if group is None:
            return
        runtime = runtime_state.RuntimeStateRepository(self.connection)
        with runtime_state._transaction(self.connection):
            for member in json.loads(group['members']):
                if member != plan_id:
                    runtime.mark_merged(repo, member)
            self.update(group, state='MERGED', active_id=None)

    def reflect_pr_state(self, repo, plan_id):
        """Apply a shared PR recovery transition without copying Agent outcomes."""
        group=self.for_plan(repo,plan_id)
        if group is None:
            return
        self.workspace_owner(repo,plan_id)
        runtime=runtime_state.RuntimeStateRepository(self.connection)
        selected=runtime.get(repo,plan_id)
        selected_reason=self.connection.execute('SELECT blocked_reason FROM exec_plans WHERE repo=? AND plan_id=?',
                                                (repo,plan_id)).fetchone()[0]
        with runtime_state._transaction(self.connection):
            for member in json.loads(group['members']):
                if member==plan_id:
                    continue
                if selected.status=='PLAN_BLOCKED' and selected_reason=='pull_request_closed':
                    runtime.block_plan(repo,member,reason_code='pull_request_closed',
                        evidence=selected.last_error or 'PR이 Merge 없이 닫혔습니다.',
                        release_fingerprint=selected.pr_head_oid,
                        plan_values={'pr_url':selected.pr_url,'pr_head_oid':selected.pr_head_oid},
                        clear_projection=False)
                else:
                    from loop_manager_internal.recovery import scoped_blocking
                    current=runtime.get(repo,member)
                    reason=self.connection.execute('SELECT blocked_reason FROM exec_plans WHERE repo=? AND plan_id=?',
                                                   (repo,member)).fetchone()[0]
                    if current.status=='PLAN_BLOCKED' and reason!='pull_request_closed':
                        continue
                    scoped_blocking.clear_plan_block(self.connection,repo,member)
                    runtime.update_plan(repo,member,plan_values={
                        'status':'PR_OPEN' if selected.status=='PR_OPEN' else 'MANUAL_PENDING',
                        'pr_url':selected.pr_url,'pr_head_oid':selected.pr_head_oid,
                        'blocked_reason':None,'last_error':None},enqueue=False)
            if selected.status=='PLAN_BLOCKED' and selected_reason=='pull_request_closed':
                self.update(group,state='CLOSED',active_id=None,
                    resume_phase='PR_OPEN' if group['state']=='PR_OPEN' else group['resume_phase'],
                    last_error=selected.last_error)
            else:
                self.update(group,state='PR_OPEN' if selected.status=='PR_OPEN' else 'AUTOMATIC',
                    active_id=None if selected.status=='PR_OPEN' else plan_id,
                    resume_phase=selected.status,last_error=selected.last_error)
