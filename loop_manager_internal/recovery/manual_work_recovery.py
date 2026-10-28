"""Shared PR evidence and released waits for manual submission groups."""
from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace

from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository, digest
from loop_manager_internal.execution.manual_candidate import (
    candidate_token, stage_input_tokens, stage_result_token,
)
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.storage import execution_checkpoint_state


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


def revalidate_publication_stage(connection, repo, plan_id, stage, *, pr_url,
                                 pr_head_oid, confirmed_push_head=None):
    """Restart the stale manual stage after PR ownership has been confirmed."""
    if stage not in {'verify', 'review'}:
        return False
    store=ManualWorkRepository(connection)
    group=store.for_plan(repo,plan_id)
    if (group is None or group['state']!='AUTOMATIC' or not group['request_id']
            or plan_id not in json.loads(group['members'])):
        return False
    store.workspace_owner(repo,plan_id)
    runtime=runtime_state.RuntimeStateRepository(connection)
    selected=runtime.projected(repo)
    if selected is None or selected.plan_id!=plan_id or selected.status!='PUBLISHING':
        return False
    members=json.loads(group['members'])
    for member in members:
        if runtime.get(repo,member).status not in {'PUBLISHING','MANUAL_PENDING','PR_OPEN'}:
            return False
    checkpoint_repository=execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    checkpoint=checkpoint_repository.get(repo,plan_id)
    if confirmed_push_head is not None and checkpoint.operation not in {None,'push'}:
        return False
    if (confirmed_push_head is not None and checkpoint.operation=='push'
            and checkpoint.expected_head!=confirmed_push_head):
        return False

    phase='VERIFYING' if stage=='verify' else 'REVIEWING'
    reason=f'{stage} 설정 입력이 변경되어 자동 재검증합니다.'
    plan_values={'status':'MANUAL_PENDING','review_result':None,'review_token':None,
        'publication_token':None}
    project_values={'phase':phase,'last_error':reason,'pr_url':pr_url,'pr_head_oid':pr_head_oid,
        'review_result':None,'review_token':None,'publication_token':None}
    if stage=='verify':
        plan_values.update(verify_result=None,verify_token=None,validated_base_sha=None)
        project_values.update(verify_result=None,verify_token=None,validated_base_sha=None)
    if confirmed_push_head is not None:
        project_values.update(publication_remote_oid=confirmed_push_head,pr_conflict_recovery=0,
            pr_conflict_head_oid=None,pr_conflict_base_oid=None)
    with runtime_state._transaction(connection):
        for member in members:
            runtime.update_plan(repo,member,plan_values=plan_values,enqueue=False)
        runtime.update_project(repo,project_values)
        if confirmed_push_head is not None and checkpoint.operation=='push':
            checkpoint_repository.record(repo,plan_id,
                expected_generation=checkpoint.generation,operation=None,
                expected_head=confirmed_push_head,target_base_oid=checkpoint.target_base_oid,
                observed_remote_oid=confirmed_push_head,
                snapshot={**(checkpoint.snapshot or {}),
                          'confirmed_manual_stage_revalidation':stage})
        store.update(group,state='AUTOMATIC',active_id=plan_id,resume_phase=phase,
                     last_error=reason)
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
    with ExitStack() as git_leases:
        try:
            git_observation = manager_git_journal.project_mutation_identity(state_dir, repo, connection)
        except manager_git_journal.MutationEvidenceError:
            return
        if manager_git_journal.has_conflicting_project_mutation(
                state_dir, repo, stage='manual_release', connection=connection, git=git,
                worktrees=(Path(selected['worktree']),),
                refs=(f"refs/heads/{selected['branch']}", f"refs/remotes/origin/{selected['branch']}"),
                lease_stack=git_leases):
            return
        worktree=Path(selected['worktree'])
        if git(worktree,'diff','--name-only','--diff-filter=U').stdout.strip():
            return
        with connection:
            if (not manager_git_journal.project_mutation_leases_owned(state_dir, repo, git_observation)
                    or manager_git_journal.project_mutation_identity(state_dir, repo, connection) != git_observation
                    or dict(runtime.active_row(repo) or {}) != dict(selected)
                    or dict(store.for_plan(repo, selected['plan_id']) or {}) != dict(group)):
                return
            store.update(group,resume_phase=selected['status'],last_error=outcome.detail)
            runtime.transition_and_clear(repo,selected['plan_id'],expected_status=selected['status'],
                                         plan_values={'status':'MANUAL_PENDING'},enqueue=False)
