"""Bind diagnostic owner records to a final-erasure observation, without Git writes."""
import hashlib
import json
import re
from pathlib import Path

from .task_calls import CallDeferred


OWNER_TABLES = ('diagnostic_preservations', 'diagnostic_ref_uses', 'diagnostic_cause_resolutions')
REF_TABLES = ('diagnostic_cleanup_preparations', 'diagnostic_cleanup_attempts')


def marked_owners(connection, repo):
    """Find legacy scope records that survived an already-confirmed final deletion."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'deleted_plan_ids' not in tables:
        return ()
    owners = set()
    for table in (*OWNER_TABLES, 'blocked_diagnostic_publications'):
        if table in tables:
            field = 'plan_id' if table == 'blocked_diagnostic_publications' else 'owner'
            owners.update(row[0] for row in connection.execute('SELECT d.plan_id FROM deleted_plan_ids d JOIN '
                + table + ' r ON r.repo=d.repo AND r.' + field + '=d.plan_id WHERE d.repo=?', (repo,)))
    return tuple(sorted(owners))


def capture(connection, repo, plan_id):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {'owners': {}, 'publications': [], 'ref_records': {}, 'refs': [], 'worktrees': [], 'related_owners': {}}
    project = connection.execute('SELECT origin,base_branch FROM projects WHERE repo=?', (repo,)).fetchone()
    result['project'] = dict(project) if project else None
    refs, worktrees = set(), set()
    try:
        for table in OWNER_TABLES:
            if table not in tables:
                continue
            rows = [dict(row) for row in connection.execute('SELECT * FROM ' + table + ' WHERE repo=? AND owner=? ORDER BY rowid', (repo, plan_id))]
            if any(row['scope'] not in ('plan', 'project') for row in rows):
                raise ValueError('unknown diagnostic owner scope')
            if table == 'diagnostic_ref_uses' and any(row['origin'] != ((result['project'] or {}).get('origin') or '') for row in rows):
                raise ValueError('diagnostic origin changed')
            result['owners'][table] = rows
            refs.update(row['ref'] for row in rows if row.get('ref') is not None)
            for row in rows:
                if 'worktree' in row and row['worktree'] is not None:
                    worktrees.add(row['worktree'])
                if 'payload' in row:
                    payload = json.loads(row['payload'])
                    if not isinstance(payload, dict):
                        raise ValueError('unknown diagnostic owner payload')
                    if payload.get('worktree') is not None:
                        worktrees.add(payload['worktree'])
        if 'blocked_diagnostic_publications' in tables:
            result['publications'] = [dict(row) for row in connection.execute(
                'SELECT * FROM blocked_diagnostic_publications WHERE repo=? AND plan_id=? ORDER BY rowid', (repo, plan_id))]
            refs.update(row['ref'] for row in result['publications'])
        if any(not isinstance(ref, str) or not re.fullmatch(
                r'loop-manager/diagnostics/' + re.escape(plan_id) + r'/[0-9a-f]{64}', ref) for ref in refs):
            raise ValueError('unknown diagnostic ref owner')
        if any(not isinstance(path, str) or not Path(path).is_absolute() for path in worktrees):
            raise ValueError('unknown diagnostic workspace')
        result['worktrees'] = sorted(worktrees)
        for table in (*OWNER_TABLES[:2], 'blocked_diagnostic_publications'):
            if table in tables:
                result['related_owners'][table] = [dict(row) for row in connection.execute(
                    'SELECT * FROM ' + table + ' WHERE repo=? ORDER BY rowid', (repo,)) if row['ref'] in refs]
        for ref in refs:
            if connection.execute('SELECT 1 FROM exec_plans WHERE repo=? AND branch=?', (repo, ref)).fetchone():
                raise CallDeferred('실행 작업이 사용하는 진단 ref는 먼저 해제해야 합니다.')
            if 'manual_work_groups' in tables and connection.execute(
                    'SELECT 1 FROM manual_work_groups WHERE repo=? AND (branch=? OR source_branch=?)', (repo, ref, ref)).fetchone():
                raise CallDeferred('수동 작업의 진단 ref 참조를 보존합니다.')
        for table in REF_TABLES:
            if table not in tables:
                continue
            related = []
            for row in connection.execute('SELECT * FROM ' + table + ' WHERE repo=? ORDER BY rowid', (repo,)):
                if row['ref'] not in refs:
                    continue
                payload = json.loads(row['payload'])
                if not isinstance(payload, dict):
                    raise ValueError('unknown diagnostic cleanup payload')
                if table == 'diagnostic_cleanup_attempts':
                    state = payload['state']
                    if (not isinstance(state, dict) or (state['repo'], state['ref']) != (repo, row['ref'])
                            or any(not isinstance(state[field], list) for field in ('uses', 'preservations', 'publications'))):
                        raise ValueError('unknown diagnostic receipt scope')
                    owners = {item['owner'] for field in ('uses', 'preservations') for item in state[field]}
                    owners.update(item['plan_id'] for item in state['publications'])
                else:
                    facts = payload['facts']
                    if (not isinstance(facts, dict) or 'owners' not in facts and (
                            payload.get('ready') is not False or not isinstance(payload.get('reason'), str))):
                        raise ValueError('unknown diagnostic preparation facts')
                    owners = set(facts.get('owners', {}))
                    for source, records in result['related_owners'].items():
                        owners.update(item['plan_id'] if source == 'blocked_diagnostic_publications' else item['owner']
                                      for item in records if item['ref'] == row['ref'])
                # A shared immutable observation is retained until its native
                # owners separate it; erasure cannot invent a replacement receipt.
                if owners - {plan_id}:
                    raise CallDeferred('독립 소유자가 포함된 진단 정리 관측을 분리하기 전에는 제거할 수 없습니다.')
                related.append(dict(row))
            result['ref_records'][table] = related
        if 'project_blocks' in tables:
            project_block = connection.execute('SELECT * FROM project_blocks WHERE repo=?', (repo,)).fetchone()
            if project_block is not None:
                if project_block['plan_id'] == plan_id:
                    raise CallDeferred('대상이 소유한 현재 프로젝트 차단은 먼저 해소해야 합니다.')
                from loop_manager_internal.recovery.scoped_blocking import project_cause_identity
                identities = {project_cause_identity(project_block), hashlib.sha256(
                    json.dumps(dict(project_block), sort_keys=True).encode()).hexdigest()}
                for records in result['owners'].values():
                    for row in records:
                        if row['scope'] == 'project':
                            cause = row.get('cause_identity')
                            if cause is None and 'payload' in row:
                                cause = json.loads(row['payload']).get('cause_identity')
                            if cause is None or cause in identities:
                                raise CallDeferred('현재 공유 프로젝트 원인이 사용하는 진단 근거를 보존합니다.')
        result['refs'] = sorted(refs)
        return result
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise CallDeferred('진단 기록의 실제 대상·공유 귀속을 확인할 수 없습니다.') from error


def forget(connection, repo, plan_id, expected):
    current = capture(connection, repo, plan_id)
    if current != expected:
        raise CallDeferred('진단 기록이 외부 자원 관측 이후 변경됐습니다.')
    for table, rows in current['owners'].items():
        if rows:
            connection.execute('DELETE FROM ' + table + ' WHERE repo=? AND owner=?', (repo, plan_id))
    for table, rows in current['ref_records'].items():
        for row in rows:
            if table == 'diagnostic_cleanup_preparations':
                connection.execute('DELETE FROM ' + table + ' WHERE repo=? AND ref=?', (repo, row['ref']))
            else:
                connection.execute('DELETE FROM ' + table + ' WHERE repo=? AND ref=? AND basis=?', (repo, row['ref'], row['basis']))
