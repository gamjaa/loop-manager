"""Preserve repeated source submissions without changing the user's branch."""
import json
from loop_manager_internal.git import manager_git_journal


def update_source(service, group):
    incoming=group['pending_source_head']
    if not incoming:
        return group
    effects,store=service.effects,service.store
    remote=service.fetch(group['branch'])
    row=service.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                  (service.repo,group['owner_id'])).fetchone()
    if row is None or row['branch']!=group['branch'] or int(row['workspace_owned'])!=1:
        raise RuntimeError('반복 submit의 소유 branch identity를 확인할 수 없습니다.')
    store.workspace_owner(service.repo,group['owner_id'])
    if effects.git(service.repo_path,'merge-base','--is-ancestor',group['prepared_head'],remote,check=False).returncode:
        raise RuntimeError('소유 branch의 예상 밖 이력 변경을 보존했습니다. 기존 협업 복구가 필요합니다.')
    candidate=group['source_update_candidate']
    conflict=bool(group['source_update_conflict'])
    if not candidate:
        result=effects.git(service.repo_path,'merge-tree','--write-tree',remote,incoming,check=False)
        conflict=result.returncode!=0
        if result.returncode not in (0,1):
            raise RuntimeError('source 후보의 보존 Merge 계산을 재시도해야 합니다: '+result.stdout)
        tree=(effects.git(service.repo_path,'rev-parse',f'{remote}^{{tree}}').stdout.strip()
              if conflict else result.stdout.splitlines()[0])
        candidate=effects.git(service.repo_path,'commit-tree',tree,'-p',remote,'-p',incoming,'-m',
            f"{group['owner_id']}: Preserve repeated manual source submission").stdout.strip()
        with service.connection:
            group=store.update(group,source_update_candidate=candidate,
                source_update_conflict=int(conflict),last_error=result.stdout if conflict else None)
    # A conflicted intake retains both commits, but remains IMPLEMENTING until
    # ordinary bounded Worker correction integrates the recorded incoming diff.
    manager_git_journal.execute(service.state_dir,owner=f"plan:{group['owner_id']}",kind='manual_branch_push',
        repo=service.repo,target=f"refs/heads/{group['branch']}",before={'oid':remote},expected={'oid':candidate},
        observe=lambda:{'oid':manager_git_journal.live_remote_branch_oid(effects.git,service.repo_path,group['branch'])},
        apply=lambda _token:effects.git(service.repo_path,'push',
            manager_git_journal.exact_branch_lease(group['branch'],remote),'origin',
            f"{candidate}:refs/heads/{group['branch']}"),marker=lambda _intent:True)
    with service.connection:
        for member in json.loads(group['members']):
            service.runtime.update_plan(service.repo,member,plan_values={'status':'MANUAL_PENDING',
                'verify_token':None,'review_token':None,'publication_token':None,'worker_result':None,
                'verify_result':None,'review_result':None,'validated_base_sha':None},enqueue=False)
        group=store.update(group,state='AUTOMATIC',source_head=incoming,source_update_candidate=None,
            pending_source_head=incoming if conflict else None,resume_phase='IMPLEMENTING' if conflict else 'VERIFYING',
            prepared_head=candidate)
    return group
