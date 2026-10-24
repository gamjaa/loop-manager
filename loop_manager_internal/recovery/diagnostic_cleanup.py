"""Read-only diagnostic eligibility observations; never delete or release work.

Preparations are disposable observations bound to SQLite and external evidence.
The eventual deletion boundary must repeat these checks and acquire its writer.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Callable

from loop_manager_internal.git.manager_git_journal import parse_remote_branch_oid
from loop_manager_internal.git.managed_rebase_validation import valid_acceptance
from loop_manager_internal.recovery import diagnostic_preservation
from loop_manager_internal.recovery.checkpoint_adapter import _frozen_push_target
from loop_manager_internal.recovery.scoped_blocking import project_cause_identity
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from loop_manager_internal.storage.execution_checkpoint_state import CheckpointRecord, ExecutionCheckpointRepository
from loop_manager_internal.storage.task_calls import management_available


@dataclass(frozen=True)
class CleanupEffects:
    git: Callable
    capture_git_snapshot: Callable
    validate_execplan_workspace_identity: Callable
    validate_managed_worktree: Callable
    adapter_factory: Callable
    current_rebase_service: Callable


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _snapshot(connection, repo, ref):
    def rows(table, where='repo=? AND ref=?', args=(repo, ref)):
        return [dict(row) for row in connection.execute(f'SELECT * FROM {table} WHERE {where}', args)]
    uses = rows('diagnostic_ref_uses')
    preservations = rows('diagnostic_preservations')
    publications = rows('blocked_diagnostic_publications')
    owners = sorted({row['owner'] for row in uses + preservations} |
                    {row['plan_id'] for row in publications})
    project = connection.execute('SELECT origin, base_branch FROM projects WHERE repo=?', (repo,)).fetchone()
    state = dict(repo=repo, ref=ref, project=dict(project) if project else None,
                 uses=uses, preservations=preservations, publications=publications,
                 plans=[], resolutions=[], blocks=[], checkpoints=[], reservations=[])
    for owner in owners:
        plan = connection.execute('SELECT plan_id, branch, worktree, workspace_owned, publication_remote_oid '
            'FROM exec_plans WHERE repo=? AND plan_id=?', (repo, owner)).fetchone()
        if plan:
            state['plans'].append(dict(plan))
        causes = {row['cause_identity'] for row in uses if row['owner'] == owner}
        state['resolutions'].extend(row for row in rows('diagnostic_cause_resolutions',
            'repo=? AND owner=?', (repo, owner)) if row['cause_identity'] in causes)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='execution_checkpoints'").fetchone():
            state['checkpoints'].extend(rows('execution_checkpoints', 'repo=? AND plan_id=?', (repo, owner)))
        state['blocks'].extend(rows('execplan_blocks', 'repo=? AND plan_id=?', (repo, owner)))
        state['reservations'].extend(rows('task_call_reservations', 'repo=? AND plan_id=?', (repo, owner)))
    state['blocks'].extend(rows('project_blocks', 'repo=?', (repo,)))
    # Older exact-generation acceptances can already carry the released cause.
    # Never pair an arbitrary history entry with a later unrelated acceptance.
    for row in state['checkpoints']:
        saved = json.loads(row['snapshot'] or '{}')
        record = CheckpointRecord(**{key: row[key] for key in (
            'generation', 'operation', 'expected_head', 'target_base_oid', 'observed_remote_oid')}, snapshot=saved)
        proof = record.current_rebase_acceptance()
        audit = saved.get('rebase_revalidation_audit', [])
        cause = audit[-1].get('released_rebase_block') if audit and isinstance(audit[-1], dict) else None
        if proof is None or not isinstance(cause, dict):
            continue
        identities = {_digest(cause)}
        if cause.get('scope') == 'project':
            identities.add(project_cause_identity(cause))
        for use in uses:
            if (use['cause_identity'] not in identities or use['scope'] != cause.get('scope')
                    or use['owner'] != cause.get('plan_id') or cause.get('repo') != repo
                    or row['plan_id'] != use['owner']
                    or any(item['cause_identity'] == use['cause_identity'] for item in state['resolutions'])):
                continue
            state['resolutions'].append(dict(repo=repo, scope=use['scope'], owner=use['owner'],
                cause_identity=use['cause_identity'], origin=use['origin'],
                original_branch=proof['branch'], worktree=proof['worktree'],
                payload=json.dumps(dict(cause=cause, acceptance=proof, pending_work=True), sort_keys=True)))
    return state


def _remote(effects, repo_path, url, ref):
    options, operand = _frozen_push_target(url)
    result = effects.git(repo_path, *options, 'ls-remote', '--heads', operand,
                         'refs/heads/' + ref, check=False)
    if result.returncode:
        raise RuntimeError('진단 정리 준비의 원격 ref 조회 실패')
    return parse_remote_branch_oid(result.stdout, 'refs/heads/' + ref)


def _observe(effects, connection, state_dir, state):
    repo, ref = state['repo'], state['ref']
    facts = dict(owners={}, artifacts={}, resolutions={}, remote_oid=None)
    for saved in state['preservations']:
        assets, manifest = diagnostic_preservation.load_verified_artifacts(state_dir, saved)
        conflict = any(set(versions['index']) & {'1', '2', '3'}
                       for versions in manifest['files'].values())
        facts['artifacts'][saved['input_identity']] = dict(
            digest=_digest({name: hashlib.sha256(data).hexdigest() for name, data in assets.items()}),
            conflict=bool(conflict), tree_oid=saved['tree_oid'], commit_oid=saved['commit_oid'])
    for plan in state['plans']:
        owner, branch, worktree = plan['plan_id'], plan['branch'], plan['worktree']
        if not branch or not worktree or plan['workspace_owned'] != 1:
            raise RuntimeError('진단 정리 준비의 작업 공간 소유권 부족')
        if (any(row['plan_id'] == owner for row in state['reservations'])
                or not management_available(connection, repo, owner)):
            raise RuntimeError('진단 자료를 사용하는 실행이 진행 중입니다.')
        path = Path(worktree)
        effects.validate_execplan_workspace_identity(state_dir, repo,
            manual_workspace_owner(connection, repo, owner), branch, path)
        effects.validate_managed_worktree(Path(repo), path, branch)
        current = effects.capture_git_snapshot(path)
        if current.branch != branch or current.operations or any(entry.split()[-1] != '0'
                for entries in current.index.values() for entry in entries):
            raise RuntimeError('진단 원인의 Git operation 또는 index 충돌이 남아 있습니다.')
        adapter = effects.adapter_factory(connection, Path(repo), state_dir, repo)
        request = SimpleNamespace(repo=repo, worktree=worktree)
        url = adapter._validated_push_url(request)
        normal = _remote(effects, Path(repo), url, branch)
        if normal is None:
            raise RuntimeError('정상 작업 branch의 원격 게시를 확인할 수 없습니다.')
        heads = set()
        for row in state['resolutions']:
            if row['owner'] != owner:
                continue
            saved = json.loads(row['payload'])
            proof = saved['acceptance']
            pending_work = saved.get('pending_work', True)
            if proof is None:
                target = saved.get('pending_target')
                if not isinstance(target, str) or not target:
                    raise RuntimeError('미완료 원인 해제의 target 근거가 없습니다.')
                service = effects.current_rebase_service(connection, Path(repo), state_dir, repo,
                    owner, path, branch, target=target, remote_oid=plan['publication_remote_oid'])
                observed, decision = service.require_owned(
                    checkpoint=ExecutionCheckpointRepository(connection).get(repo, owner))
                if decision.status.value != 'complete' or not valid_acceptance(decision.acceptance):
                    raise RuntimeError('차단 해제 후의 rebase가 아직 완료되지 않았습니다.')
                proof, pending_work = decision.acceptance, bool(observed.files)
                if proof['target_base_oid'] != target:
                    raise RuntimeError('원인 해제의 원래 rebase target이 변경됐습니다.')
            heads.add(proof['head'])
            facts['resolutions'][row['cause_identity']] = dict(acceptance=proof, pending_work=pending_work)
        if plan['publication_remote_oid']:
            heads.add(plan['publication_remote_oid'])
        ancestors = {}
        for head in sorted(heads):
            result = effects.git(path, 'merge-base', '--is-ancestor', head, normal, check=False)
            if result.returncode not in (0, 1):
                raise RuntimeError('해결 결과의 원격 포함 관계를 확인할 수 없습니다.')
            ancestors[head] = result.returncode == 0
        facts['owners'][owner] = dict(normal_oid=normal, ancestors=ancestors,
            snapshot_digest=_digest(current.serialize()), head=current.head, dirty=bool(current.dirty),
            target_digest=hashlib.sha256(url.encode()).hexdigest())
        observed = _remote(effects, Path(repo), url, ref)
        if facts['remote_oid'] is not None and observed != facts['remote_oid']:
            raise RuntimeError('진단 ref 원격 관측이 변경됐습니다.')
        facts['remote_oid'] = observed
    return facts


def assess_ref(state, facts):
    """Pure decision: every recorded user must have complete matching evidence."""
    # ponytail: matching costs O(uses * causes); index by cause if histories grow.
    uses, ref, project = state['uses'], state['ref'], state['project']
    if not uses or project is None or not project['origin']:
        return '게시 대상·원인 사용 근거가 부족합니다.'
    owners = {row['owner'] for row in uses}
    if len(owners) != 1:
        return '다른 작업이 같은 진단 ref를 사용합니다.'
    expected = {row['confirmed_oid'] for row in uses}
    if len(expected) != 1 or None in expected:
        return '모든 원인의 진단 게시가 확정되지 않았습니다.'
    expected_oid = next(iter(expected))
    if any(row['confirmed_oid'] != expected_oid or row['head_oid'] != expected_oid
           or row['plan_id'] not in owners for row in state['publications']):
        return '진행 중이거나 미확정인 진단 게시 intent가 있습니다.'
    if facts['remote_oid'] not in (None, expected_oid):
        return '진단 ref의 원격 OID가 저장된 게시 OID와 다릅니다.'
    plans = {row['plan_id']: row for row in state['plans']}
    if len(plans) != len(owners):
        return '원래 작업의 소유권 기록이 없습니다.'
    if {row['owner'] for row in state['preservations']} - owners:
        return '다른 작업의 원문 자료가 연결돼 있습니다.'
    for saved in state['preservations']:
        payload = json.loads(saved['payload'])
        if saved['retryable'] or payload.get('result') != 'published' or not any(row['input_identity'] == saved['input_identity']
                and row['cause_identity'] == payload.get('cause_identity') for row in uses):
            return '진행 중이거나 미확정인 진단 보존 사용이 있습니다.'
    for use in uses:
        plan = plans[use['owner']]
        if (use['scope'] not in {'plan', 'project'} or use['publication_pending']
                or use['origin'] != project['origin'] or use['commit_oid'] != expected_oid
                or use['original_branch'] != plan['branch']
                or ref in {plan['branch'], project['base_branch']}
                or not ref.startswith('loop-manager/diagnostics/')):
            return '진단 사용의 원인·origin·branch 또는 진행 상태가 유효하지 않습니다.'
        artifact = next((item for item in facts['artifacts'].values()
            if (item['tree_oid'], item['commit_oid']) == (use['tree_oid'], expected_oid)), None)
        if artifact is None or not artifact['conflict']:
            return '충돌 원문의 독립적인 로컬 복구 근거가 없습니다.'
        for block in state['blocks']:
            identities = {_digest(dict(block))}
            if block['scope'] == 'project':
                identities.add(project_cause_identity(block))
            if use['cause_identity'] in identities:
                return '해당 진단 원인이 아직 활성 상태입니다.'
        resolution = next((row for row in state['resolutions']
            if (row['scope'], row['owner'], row['cause_identity']) ==
               (use['scope'], use['owner'], use['cause_identity'])), None)
        if resolution is None:
            return '해당 원인의 해소 확정 기록이 없습니다.'
        resolved = json.loads(resolution['payload'])
        observed_resolution = facts['resolutions'][use['cause_identity']]
        proof = observed_resolution['acceptance']
        identities = {_digest(resolved['cause'])}
        if resolution['scope'] == 'project':
            identities.add(project_cause_identity(resolved['cause']))
        if (not valid_acceptance(proof) or resolution['origin'] != use['origin']
                or resolution['original_branch'] != use['original_branch']
                or resolution['worktree'] != plan['worktree']
                or use['cause_identity'] not in identities
                or (proof['repo'], proof['plan_id'], proof['branch'], proof['worktree'],
                    proof['remote']['repository']) !=
                   (state['repo'], use['owner'], use['original_branch'], plan['worktree'], state['repo'])):
            return '원인 해소 확정의 소유권 identity가 다릅니다.'
        owner = facts['owners'].get(use['owner'])
        if (owner is None or not plan['publication_remote_oid']
                or not owner['ancestors'].get(proof['head'])
                or not owner['ancestors'].get(plan['publication_remote_oid'])):
            return '해결 결과의 정상 작업 branch 게시가 확정되지 않았습니다.'
        if observed_resolution['pending_work'] is not False and (
                owner['dirty'] or owner['head'] != plan['publication_remote_oid']
                or owner['head'] == proof['head']):
            return '해소 당시 남은 작업물의 정상 branch 게시가 확인되지 않았습니다.'
    return None


def prepare_recorded(effects: 'CleanupEffects', connection, state_dir, repo):
    """Reobserve recorded refs once; preparation failure never gates the caller."""
    refs = connection.execute('SELECT ref FROM diagnostic_preservations WHERE repo=? AND ref IS NOT NULL '
        'UNION SELECT ref FROM blocked_diagnostic_publications WHERE repo=? '
        'UNION SELECT ref FROM diagnostic_ref_uses WHERE repo=?', (repo, repo, repo)).fetchall()
    results = []
    for item in refs:
        ref = item['ref']
        try:
            state = _snapshot(connection, repo, ref)
            facts = {}
            try:
                facts = _observe(effects, connection, state_dir, state)
                reason = assess_ref(state, facts)
                if _observe(effects, connection, state_dir, state) != facts:
                    reason = '관측 중 자료·소유권·원격 상태가 변경됐습니다.'
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
                reason = diagnostic_preservation.redact_detail(str(error))
            payload = dict(ready=reason is None, reason=reason, expected_oid=(
                state['uses'][0]['confirmed_oid'] if state['uses'] else None), facts=facts)
            basis = _digest(state)
            with runtime_state._transaction(connection):
                if _snapshot(connection, repo, ref) != state:
                    continue  # A new user/generation needs a fresh observation.
                connection.execute('INSERT INTO diagnostic_cleanup_preparations VALUES (?, ?, ?, ?) '
                    'ON CONFLICT(repo, ref) DO UPDATE SET basis=excluded.basis, payload=excluded.payload',
                    (repo, ref, basis, json.dumps(payload, sort_keys=True)))
            results.append(dict(ref=ref, basis=basis, **payload))
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error):
            continue  # Keep the original work/diagnostic; retry at the next boundary.
    return results
