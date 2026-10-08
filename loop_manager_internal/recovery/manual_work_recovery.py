"""Shared PR evidence and released waits for manual submission groups."""
import json
from pathlib import Path
from types import SimpleNamespace

from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository, digest
from loop_manager_internal.execution.manual_candidate import (
    candidate_token, stage_input_tokens, stage_result_token,
)
from loop_manager_internal.runtime.contracts import PublicationSafetyError


def rotate_correction(connection, repo):
    """Move the one execution projection to the G that failed shared Review."""
    runtime=runtime_state.RuntimeStateRepository(connection)
    active=runtime.active_row(repo)
    if active is None or active['status']!='IMPLEMENTING' or active['agent_snapshot']:
        return
    store=ManualWorkRepository(connection)
    group=store.for_plan(repo,active['plan_id'])
    if not group or group['active_id']==active['plan_id']:
        return
    target=group['active_id']
    store.workspace_owner(repo,target)
    selected=runtime.get(repo,target)
    if selected.status!='MANUAL_PENDING':
        return
    values={key:active[key] for key in runtime_state.MIRRORED_FIELDS}
    values.update(status='IMPLEMENTING',worker_result=None,worker_input_token=None,
                  worker_session_id=None,worker_session_binding=None,worker_session_transition=None)
    with runtime_state._transaction(connection):
        runtime.update_plan(repo,active['plan_id'],plan_values={'status':'MANUAL_PENDING'},enqueue=False)
        runtime.update_plan(repo,target,plan_values=values,enqueue=False)
        runtime.hydrate(repo,target)


def validate_merge(connection, repo, plan, url, head, *, pr_for_branch):
    store=ManualWorkRepository(connection)
    group=store.for_plan(repo,str(plan['plan_id']))
    if group is None:
        return
    if group['pending_source_head']:
        raise PublicationSafetyError('아직 반영되지 않은 수락된 source 후보가 있습니다. Merge 완료를 확정하지 않습니다.')
    store.workspace_owner(repo,str(plan['plan_id']))
    publication=connection.execute('SELECT * FROM manual_work_publications WHERE repo=? AND group_id=?',
                                  (repo,group['group_id'])).fetchone()
    if not publication or publication['pr_url']!=url or publication['head']!=head \
            or publication['request_id']!=group['request_id']:
        raise PublicationSafetyError('실제 Merge HEAD에 묶음 전체 G의 최종 게시 근거가 없습니다. 작업물을 보존합니다.')
    from loop_manager_internal.application.manual_work import confirm_dependencies
    base=connection.execute('SELECT base_branch FROM projects WHERE repo=?',(repo,)).fetchone()[0]
    if pr_for_branch is None:
        raise PublicationSafetyError('수동 제출 dependency의 원격 Merge 확인 경계가 없습니다.')
    actual=pr_for_branch(Path(repo),group['branch'],url)
    if not actual or actual.get('url')!=url or actual.get('headRefName')!=group['branch'] \
            or actual.get('baseRefName')!=base or actual.get('headRefOid')!=head \
            or not (actual.get('state')=='MERGED' or actual.get('mergedAt')):
        raise PublicationSafetyError('묶음 소유 branch·등록 base의 실제 PR Merge를 확인할 수 없습니다.')
    confirm_dependencies(connection,SimpleNamespace(pr_for_branch=pr_for_branch),Path(repo),
                         json.loads(group['documents']),base)
    rows={row['plan_id']:row for row in connection.execute('SELECT * FROM exec_plans WHERE repo=?',(repo,))}
    required={(member,stage) for member in json.loads(group['members']) for stage in ('verify','review')}
    evidence=json.loads(publication['evidence'])
    if not required<={(row['plan_id'],row['stage']) for row in evidence} or any(
            rows[member]['pr_url']!=url or rows[member]['branch']!=group['branch']
            or rows[member]['pr_head_oid']!=head for member in json.loads(group['members'])):
        raise PublicationSafetyError('묶음 구성 G의 branch·PR·최종 근거 identity가 일치하지 않습니다.')


def revalidate_waiting(connection, repo, plan, pr, base_head, *, config_path=None,
                        provider=None, worktree=None, resolve_profile=None):
    """A changed shared PR becomes pending automatically, without taking a slot."""
    store=ManualWorkRepository(connection)
    group=store.for_plan(repo,str(plan['plan_id']))
    if group is None:
        return False
    publication=connection.execute('SELECT * FROM manual_work_publications WHERE repo=? AND group_id=?',
                                  (repo,group['group_id'])).fetchone()
    head=str(pr.get('headRefOid') or '')
    token=candidate_token(connection,repo,head,base_head,json.loads(group['documents']))
    current_worktree=worktree or group['worktree']
    if current_worktree is not None:
        current_worktree=Path(current_worktree)
    verify_input,review_input=stage_input_tokens(config_path,provider,current_worktree,resolve_profile)
    selected=runtime_state.RuntimeStateRepository(connection).get(repo,group['owner_id'])
    stage_tokens={
        'verify':stage_result_token(token,'verify',verify_input,selected.verify_result,group['members']),
        'review':stage_result_token(token,'review',review_input,selected.verify_result,group['members']),
    }
    try:
        evidence=json.loads(publication['evidence']) if publication else []
        covered={stage:{row['plan_id'] for row in evidence
                        if row['stage']==stage and row['input_token']==stage_tokens[stage]}
                 for stage in ('verify','review')}
    except (TypeError, ValueError, KeyError):
        covered={'verify':set(),'review':set()}
    members=set(json.loads(group['members']))
    candidate_changed=not publication or publication['head']!=head or publication['input_token']!=token
    verify_changed=(candidate_changed or verify_input is None or not members<=covered['verify'])
    review_changed=(candidate_changed or review_input is None or not members<=covered['review'] or verify_changed)
    if not verify_changed and not review_changed:
        return False
    if candidate_changed:
        reason='PR 후보, base 또는 환경 입력이 변경되어'
    elif verify_changed:
        reason='verify 설정 입력이 변경되어'
    else:
        reason='Reviewer profile 입력이 변경되어'
    with connection:
        runtime=runtime_state.RuntimeStateRepository(connection)
        for member in json.loads(group['members']):
            current=runtime.get(repo,member)
            if current.status=='PR_OPEN':
                values={'status':'MANUAL_PENDING','review_result':None,'review_token':None,
                    'publication_token':None,'worker_input_token':None,'archive_token':None,'worker_result':None}
                if verify_changed:
                    values.update(verify_result=None,verify_token=None,validated_base_sha=None)
                runtime.update_plan(repo,member,plan_values=values,enqueue=False)
        phase='VERIFYING' if verify_changed else 'REVIEWING'
        store.update(group,state='AUTOMATIC',active_id=group['owner_id'],resume_phase=phase,
                     last_error=f'{reason} 자동 재검증합니다.')
    return True


def release_wait(connection, repo, outcome, state_dir, git):
    from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
    if outcome.kind not in {ExecutionOutcomeKind.STABLE_WAIT,ExecutionOutcomeKind.TRANSIENT_PAUSE,
                            ExecutionOutcomeKind.USAGE_PAUSE}:
        return
    runtime=runtime_state.RuntimeStateRepository(connection)
    selected=runtime.active_row(repo)
    if selected is None:
        return
    store=ManualWorkRepository(connection)
    group=store.for_plan(repo,selected['plan_id'])
    if not group or selected['agent_snapshot']:
        return
    from loop_manager_internal.git import manager_git_journal
    if manager_git_journal.has_pending_project_mutation(state_dir,repo):
        return
    worktree=Path(selected['worktree'])
    if git(worktree,'diff','--name-only','--diff-filter=U').stdout.strip():
        return
    with connection:
        store.update(group,resume_phase=selected['status'],last_error=outcome.detail)
        runtime.transition_and_clear(repo,selected['plan_id'],expected_status=selected['status'],
                                     plan_values={'status':'MANUAL_PENDING'},enqueue=False)
