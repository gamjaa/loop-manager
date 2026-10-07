"""Preserve repeated source submissions without changing the user's branch."""
import json
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.runtime.resource_lock import resource_locks


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
    before=group['source_update_remote_oid']
    target=f"refs/heads/{group['branch']}"
    owner=f"plan:{group['owner_id']}"
    def observe():
        return {'oid':manager_git_journal.live_remote_branch_oid(effects.git,service.repo_path,group['branch'])}
    def matches(intent):
        return (intent['owner']==owner and intent['before']=={'oid':before}
                and intent['expected']=={'oid':candidate})
    if candidate:
        if not before or effects.git(service.repo_path,'rev-list','--parents','-n','1',candidate).stdout.split() != [candidate,*dict.fromkeys((before,incoming))]:
            raise RuntimeError('저장 source 후보의 원격 기준·제출 commit 근거가 없습니다. 후보와 작업물을 보존했습니다.')
        if effects.git(service.repo_path,'merge-base','--is-ancestor',before,remote,check=False).returncode:
            raise RuntimeError('후보 생성 뒤 원격 이력이 재작성됐습니다. 후보와 작업물을 보존했습니다.')
        published=not effects.git(service.repo_path,'merge-base','--is-ancestor',candidate,remote,check=False).returncode
        slot=manager_git_journal._slot(service.state_dir,service.repo,'manual_branch_push',target)
        if not published:
            for receipt in slot.glob('*.result.json'):
                result=manager_git_journal._read(receipt)
                if result['status'] in {'complete','equal'} and result['observed']=={'oid':candidate}:
                    intent=manager_git_journal._read(slot/f"{result['id']}.intent.json")
                    if matches(intent):
                        manager_git_journal.verified_transition(intent,result['observed'],True)
                        raise RuntimeError('게시 완료한 source 후보가 원격 이력에서 제거됐습니다. 후보와 작업물을 보존했습니다.')
        if remote not in (before,candidate):
            active=slot/'active.json'
            with resource_locks(service.state_dir,service.repo,['git:'+target],owner=owner):
                if active.exists():
                    intent=manager_git_journal._read(active)
                    receipt=slot/f"{intent['id']}.result.json"
                    if receipt.exists():
                        result=manager_git_journal._read(receipt)
                        if (not matches(intent) or intent['repo']!=service.repo
                                or intent['kind']!='manual_branch_push' or intent['target']!=target
                                or manager_git_journal._read(slot/f"{intent['id']}.intent.json")!=intent
                                or result['id']!=intent['id'] or result['status'] not in {'complete','equal'}
                                or result['candidates']!=manager_git_journal.verified_transition(intent,result['observed'],True)):
                            raise manager_git_journal.MutationEvidenceError('저장 source 후보의 게시 완료 근거가 다릅니다.')
                        # 결과 저장 뒤 active 제거 전 중단: 완료 감사는 덮지 않는다.
                        manager_git_journal._finish(slot,active,intent,result['observed'],result['candidates'],result['status'])
            def retire(intent, current):
                if not matches(intent) or current!={'oid':remote}:
                    raise manager_git_journal.MutationEvidenceError('저장 source 후보와 게시 intent의 원격 기준이 다릅니다.')
                # 현재 후속 이력은 위에서 검증했다. Git을 되돌리지 않고
                # 이전 exact 전이를 종료해 관측과 후보를 감사에 남긴다.
                return current
            manager_git_journal.recover_active(service.state_dir,repo=service.repo,
                kind='manual_branch_push',target=target,observe=observe,
                marker=matches,restore_intermediate=retire)
        if published:
            # 같은 후보 또는 그 후속 commit은 게시가 끝난 상태다.
            # equal은 새 lease로 저장 후보를 다시 push하지 않는다.
            manager_git_journal.execute(service.state_dir,owner=owner,kind='manual_branch_push',
                repo=service.repo,target=target,before={'oid':remote},expected={'oid':remote},
                observe=observe,apply=lambda _token:None,marker=matches)
            candidate=remote
        elif remote!=before:
            candidate=None
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
                source_update_remote_oid=remote,
                source_update_conflict=int(conflict),last_error=result.stdout if conflict else None)
        before=remote
    # A conflicted intake retains both commits, but remains IMPLEMENTING until
    # ordinary bounded Worker correction integrates the recorded incoming diff.
    if candidate!=remote:
        manager_git_journal.execute(service.state_dir,owner=owner,kind='manual_branch_push',
            repo=service.repo,target=target,before={'oid':before},expected={'oid':candidate},
            observe=observe,apply=lambda _token:effects.git(service.repo_path,'push',
                manager_git_journal.exact_branch_lease(group['branch'],before),'origin',
                f'{candidate}:{target}'),marker=matches)
    with service.connection:
        for member in json.loads(group['members']):
            values={
                'verify_token':None,'review_token':None,'publication_token':None,'worker_result':None,
                'worker_input_token':None,'archive_token':None,
                'verify_result':None,'review_result':None,'validated_base_sha':None}
            if service.runtime.get(service.repo,member).status!='PLAN_BLOCKED':
                values['status']='MANUAL_PENDING'
            service.runtime.update_plan(service.repo,member,plan_values=values,enqueue=False)
        group=store.update(group,state='AUTOMATIC',source_head=incoming,source_update_candidate=None,
            source_update_remote_oid=None,
            pending_source_head=incoming if conflict else None,resume_phase='IMPLEMENTING' if conflict else 'VERIFYING',
            prepared_head=candidate)
    return group
