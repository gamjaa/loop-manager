"""주 Requirement별 단일 계획 PR과 원격 PR 관측."""
import hashlib
import json
import os
from pathlib import Path
import re

from loop_manager_internal.catalog.observer import Document,catalog,related_plans
from loop_manager_internal.git.repository import GitError,MergeConflict
from loop_manager_internal.storage.state import packed,TERMINAL


METADATA = re.compile(r'<!-- loop-manager:metadata (\{[^\n]+\}) -->')


def metadata(kind,primary,plan_ids,source_oid=None):
    return '<!-- loop-manager:metadata '+packed({'kind':kind,'primary_requirement_id':primary,
                                                'plan_ids':plan_ids,**({'source_oid':source_oid} if source_oid else {})})+' -->'


def classification(pull):
    match = METADATA.search(pull.get('body') or '')
    if match:
        try:
            value = json.loads(match.group(1))
        except ValueError:
            value={}
        if (isinstance(value,dict) and value.get('kind') in ('planning','implementation')
                and isinstance(value.get('plan_ids'),list)
                and all(isinstance(item,str) and re.fullmatch(r'G-[0-9]{3,}',item) for item in value['plan_ids'])
                and (value.get('primary_requirement_id') is None or isinstance(value['primary_requirement_id'],str)
                     and re.fullmatch(r'R-[0-9]{3,}',value['primary_requirement_id']))
                and (value['kind']!='planning' or value.get('primary_requirement_id'))):
            return value
    branch = pull['head']['ref']
    if re.fullmatch(r'loop-manager/G-[0-9]{3,}',branch):
        return {'kind':'implementation','primary_requirement_id':None,'plan_ids':[branch.rsplit('/',1)[-1]]}
    return None


def record_pull(store,repo,pull,info,*,input_revision=None,apply_plans=True):
    input_revision=input_revision or info.get('source_oid')
    if input_revision is not None and not isinstance(input_revision,str):
        raise ValueError('계획 PR의 source_oid는 문자열이어야 합니다.')
    state = 'MERGED' if pull.get('merged_at') else 'CLOSED' if pull['state']=='closed' else 'OPEN'
    head,base = pull['head']['sha'],pull['base']['sha']
    mergeability = {True:'CLEAN',False:'CONFLICT',None:'UNKNOWN'}[pull.get('mergeable')]
    with store.transaction() as db:
        previous = db.execute('SELECT * FROM managed_prs WHERE repo=? AND pr_number=?',(repo,pull['number'])).fetchone()
        if previous:
            info={**info,'kind':previous['kind'],'primary_requirement_id':previous['primary_requirement_id']}
            if previous['kind']=='implementation':
                info['plan_ids']=json.loads(previous['plan_ids'])
        if (previous and previous['conflict_head_sha']==head and previous['conflict_base_sha']==base
                and 'mergeable' not in pull):
            mergeability = previous['mergeability']
        observed_conflict = 'mergeable' in pull
        db.execute('INSERT INTO managed_prs(repo,pr_number,kind,primary_requirement_id,plan_ids,branch,url,head_sha,'
                   'base_sha,state,draft,mergeability,conflict_head_sha,conflict_base_sha,input_revision) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repo,pr_number) DO UPDATE SET '
                   'head_sha=excluded.head_sha,base_sha=excluded.base_sha,state=excluded.state,draft=excluded.draft,'
                   'mergeability=excluded.mergeability,conflict_head_sha=excluded.conflict_head_sha,'
                   'conflict_base_sha=excluded.conflict_base_sha,plan_ids=excluded.plan_ids,'
                   'input_revision=COALESCE(excluded.input_revision,managed_prs.input_revision)',
                   (repo,pull['number'],info['kind'],info.get('primary_requirement_id'),packed(info['plan_ids']),
                    pull['head']['ref'],pull['html_url'],head,base,state,int(pull['draft']),mergeability,
                    head if observed_conflict else previous['conflict_head_sha'] if previous else None,
                    base if observed_conflict else previous['conflict_base_sha'] if previous else None,input_revision))
        if info['kind']=='implementation' and apply_plans:
            for identity in info['plan_ids']:
                db.execute('UPDATE exec_plans SET pr_url=?,status=CASE WHEN ? THEN ? ELSE status END '
                           'WHERE repo=? AND plan_id=?',
                           (pull['html_url'],state=='MERGED','MERGED',repo,identity))
                if state=='MERGED' and (previous is None or previous['state']!='MERGED'):
                    db.execute('UPDATE exec_plan_runs SET generation=generation+1,worker_result=NULL WHERE repo=? AND plan_id=?',
                               (repo,identity))
                    plan=db.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',(repo,identity)).fetchone()
                    if plan:
                        for requirement in db.execute('SELECT * FROM requirement_states WHERE repo=?',(repo,)).fetchall():
                            if related_plans(dict(requirement),[dict(plan)]):
                                db.execute("UPDATE requirement_states SET judgment=NULL WHERE repo=? AND requirement_id=? AND judgment NOT IN ('SATISFIED','WITHDRAWN')",
                                           (repo,requirement['requirement_id']))
                    store.cancel_notifications(db,repo,identity)
                elif state=='CLOSED':
                    db.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE repo=? AND plan_id=? AND status NOT IN ('MERGED','CANCELLED','BLOCKED')",
                               (repo,identity))
                    db.execute('UPDATE exec_plan_runs SET last_error=? WHERE repo=? AND plan_id=?',
                               ('구현 PR이 Merge 없이 닫혔습니다. 같은 PR을 다시 열면 재관측합니다.',repo,identity))
                if state=='CLOSED' and not db.execute('SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=?',(repo,identity)).fetchone():
                    db.execute('INSERT INTO exec_plan_tombstones VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
                               (repo,identity,base,'원격의 닫힌 미Merge 관리 PR 이력과 현재 확정 G 부재'))
        if info['kind']=='planning' and apply_plans:
            for identity in info['plan_ids']:
                history=db.execute("SELECT primary_requirement_id,plan_ids FROM managed_prs WHERE repo=? AND kind='planning' ORDER BY pr_number",(repo,)).fetchall()
                owner=next((row['primary_requirement_id'] for row in history if identity in json.loads(row['plan_ids'])),info.get('primary_requirement_id'))
                db.execute('UPDATE exec_plans SET primary_requirement_id=? WHERE repo=? AND plan_id=?',
                           (owner,repo,identity))
        if info['kind']=='planning' and apply_plans and state=='OPEN' and (
                previous and previous['head_sha']!=head or mergeability=='CONFLICT' and (
                    previous is None or previous['conflict_head_sha']!=head or previous['conflict_base_sha']!=base
                    or previous['mergeability']!='CONFLICT')):
            db.execute("UPDATE requirement_states SET judgment='REPLAN' WHERE repo=? AND requirement_id=?",
                       (repo,info.get('primary_requirement_id')))
        return previous


def observe_pulls(store,repository,github):
    repo = repository.repo
    with store.transaction() as db:
        known={row[0] for row in db.execute('SELECT plan_id FROM exec_plans WHERE repo=?',(repo,))}
    branches=repository.plan_branches()
    with store.transaction() as db:
        for identity in branches:
            if identity in known:
                db.execute('UPDATE exec_plan_runs SET published=1 WHERE repo=? AND plan_id=?',(repo,identity))
            else:
                db.execute('INSERT INTO exec_plan_tombstones VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
                           (repo,identity,'remote-branch','원격 관리 브랜치에서 이미 사용된 G-ID'))
    stored = {row['pr_number']:row for row in store.rows('SELECT * FROM managed_prs WHERE repo=?',(repo,))}
    observed = []
    for pull in sorted(github.pulls(),key=lambda item:item['state']=='open'):
        if pull['head']['repo'] and pull['head']['repo']['full_name'].casefold()!=repo.casefold():
            continue
        info = classification(pull)
        previous = stored.get(pull['number'])
        if previous:
            info = {'kind':previous['kind'],'primary_requirement_id':previous['primary_requirement_id'],
                    'plan_ids':json.loads(previous['plan_ids'])}
            if previous['kind']=='planning' and classification(pull):
                info['plan_ids']=classification(pull)['plan_ids']
                info['source_oid']=classification(pull).get('source_oid')
        if info is None:
            continue
        if pull['state']=='open' and (not previous or previous['conflict_head_sha']!=pull['head']['sha']
                or previous['conflict_base_sha']!=pull['base']['sha'] or previous['mergeability']=='UNKNOWN'):
            pull = github.pull(pull['number'])
        if info['kind']=='planning' and pull['state']=='open':
            existing=store.rows("SELECT pr_number FROM managed_prs WHERE repo=? AND kind='planning' AND state='OPEN' "
                                'AND primary_requirement_id=? AND pr_number!=?',(repo,info.get('primary_requirement_id'),pull['number']))
            if existing:
                with store.transaction() as db:
                    db.execute('UPDATE projects SET last_error=? WHERE repo=?',('같은 주 R의 열린 계획 PR이 여러 개입니다. 원격 PR을 확인하세요.',repo))
                continue
        selected_base=pull['base'].get('ref',repository.base)==repository.base
        record_pull(store,repo,pull,info,apply_plans=selected_base)
        if selected_base:
            observed.append(pull)
    return observed


def workspace_catalog(path,revision):
    documents = []
    for folder in ('requirements','plans'):
        for pattern in ('*.md','archive/*.md'):
            for file in (Path(path)/'docs'/folder).glob(pattern):
                if file.is_symlink() or not file.is_file():
                    raise ValueError('문서는 일반 파일이어야 합니다.')
                content = file.read_bytes()
                oid = hashlib.sha1(f'blob {len(content)}\0'.encode()+content).hexdigest()
                documents.append(Document(file.relative_to(path).as_posix(),oid,content.decode('utf-8')))
    return catalog(revision,documents)


def file_signature(root,name):
    path=Path(root)/name
    if path.is_symlink():
        return ('link',os.readlink(path))
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def usage_body(store,repo,scope):
    rows = store.rows('SELECT role,provider,model,COUNT(*) AS calls,'
                      'CASE WHEN COUNT(input_tokens)=COUNT(*) THEN SUM(input_tokens) END AS input_tokens,'
                      'CASE WHEN COUNT(cache_read_tokens)=COUNT(*) THEN SUM(cache_read_tokens) END AS cache_read_tokens,'
                      'CASE WHEN COUNT(cache_write_tokens)=COUNT(*) THEN SUM(cache_write_tokens) END AS cache_write_tokens,'
                      'CASE WHEN COUNT(output_tokens)=COUNT(*) THEN SUM(output_tokens) END AS output_tokens '
                      'FROM agent_calls WHERE repo=? AND scope=? GROUP BY role,provider,model',(repo,scope))
    lines = ['| 역할 | Provider | 모델 | 호출 | 입력 | 캐시 읽기 | 캐시 쓰기 | 출력 |',
             '|---|---|---|---:|---:|---:|---:|---:|']
    for row in rows:
        values = [str(row[key]) if row[key] is not None else '확인 불가' for key in
                  ('role','provider','model','calls','input_tokens','cache_read_tokens','cache_write_tokens','output_tokens')]
        lines.append('| '+' | '.join(values)+' |')
    if not rows:
        lines.append('| 확인 불가 | | | | | | | |')
    return '<!-- loop-manager:usage -->\n'+'\n'.join(lines)+'\n\n입력·캐시 토큰은 Provider의 정의를 유지하며 중복 합산하지 않습니다.\n<!-- loop-manager:usage-end -->'


def proposal(store,repository,github,agent,snapshot,identity,reason):
    repo = repository.repo
    source = snapshot.requirements[identity]['document']
    rows = store.rows("SELECT * FROM managed_prs WHERE repo=? AND kind='planning' "
                      "AND primary_requirement_id=? ORDER BY pr_number DESC",(repo,identity))
    opened = next((row for row in rows if row['state']=='OPEN'),None)
    requirement=store.rows('SELECT * FROM requirement_states WHERE repo=? AND requirement_id=?',(repo,identity))[0]
    closing=requirement['judgment']=='CLOSING'
    if closing:
        expected=json.loads(requirement['judgment_revision'])
        matching=next((row for row in rows if row['pr_number']==expected[2] and row['head_sha']==expected[3]),None)
        if matching is None:
            closing=False
            with store.transaction() as db:
                db.execute("UPDATE requirement_states SET judgment='REPLAN',evidence=NULL WHERE repo=? AND requirement_id=?",(repo,identity))
        elif expected[:2]==[source.oid,snapshot.revision]:
            github.close({'number':expected[2]},expected[3],expected[1])
            workspaces=store.rows("SELECT remote_result FROM external_jobs WHERE repo=? AND target=? AND kind='planning_publish'",(repo,identity))
            with store.transaction() as db:
                db.execute("UPDATE managed_prs SET state='CLOSED' WHERE repo=? AND pr_number=?",(repo,expected[2]))
                db.execute("UPDATE requirement_states SET judgment='SATISFIED',judgment_revision=?,last_planned_source_oid=?,"
                           'last_planned_base_sha=? WHERE repo=? AND requirement_id=?',
                           (packed(expected[:2]),source.oid,snapshot.revision,repo,identity))
                db.execute("DELETE FROM external_jobs WHERE repo=? AND target=? AND kind='planning_publish'",(repo,identity))
            for job in workspaces:
                if job['remote_result']:
                    repository.defer_cleanup(job['remote_result'],matching['branch'])
            return True
    if not closing and not opened and any(row['state']=='CLOSED' and row['input_revision']==source.oid for row in rows):
        return False
    suffix=f'-after-{rows[0]["pr_number"]}' if rows else ''
    branch = opened['branch'] if opened else f'loop-manager/plans/{identity.lower()}-{source.oid[:10]}{suffix}'
    job_key = f'planning:{repo}:{identity}:{source.oid}'
    jobs = store.rows('SELECT * FROM external_jobs WHERE job_key=?',(job_key,))
    saved = jobs[0]['remote_result'] if jobs else None
    with store.transaction() as db:
        db.execute('INSERT INTO external_jobs(job_key,repo,kind,target,expected_input) VALUES(?,?,?,?,?) '
                   'ON CONFLICT DO NOTHING',(job_key,repo,'planning_publish',identity,source.oid))
    def reserved(path):
        with store.transaction() as db:
            db.execute('UPDATE external_jobs SET remote_result=? WHERE job_key=?',(str(path),job_key))
    path = repository.prepare(branch,saved_path=saved,published=bool(opened),on_reserved=reserved)
    repository.git('fetch','origin',repository.base,cwd=path)
    unmerged=bool(repository.git('diff','--name-only','--diff-filter=U',cwd=path))
    if opened and unmerged:
        # 불확실한 로컬 충돌 공간은 보존하고 게시된 후보에서 안전하게 다시 통합한다.
        path=repository.prepare(branch,published=True,on_reserved=reserved)
        repository.git('fetch','origin',repository.base,cwd=path)
        unmerged=False
    if opened and not unmerged:
        repository.synchronize_head(path,branch)
    initial_head = repository.head(path)
    pr_head=initial_head
    manager_changes={}
    if opened and opened['mergeability']=='CONFLICT' and not unmerged:
        repository.git('fetch','origin',repository.base,cwd=path)
        try:
            repository.merge(path,branch,f'origin/{repository.base}')
        except MergeConflict:
            manager_changes={name:file_signature(path,name) for name in repository.git(
                'diff','--name-only','-z',initial_head,cwd=path).split('\0') if name}
        else:
            initial_head=repository.head(path)
    all_ids = set(snapshot.plans)|set(snapshot.archived_plans)
    for row in store.rows('SELECT plan_id FROM exec_plan_tombstones WHERE repo=?',(repo,)):
        all_ids.add(row['plan_id'])
    for row in store.rows('SELECT plan_ids FROM managed_prs WHERE repo=?',(repo,)):
        all_ids.update(json.loads(row['plan_ids']))
    reserved_ids=set(all_ids)
    before=workspace_catalog(path,snapshot.revision)
    all_ids.update(before.plans)
    tombstones={row['plan_id'] for row in store.rows('SELECT plan_id FROM exec_plan_tombstones WHERE repo=?',(repo,))}
    next_id = max((int(value[2:]) for value in all_ids),default=0)+1
    prompt = (f'주 Requirement: {identity}\n평가 사유: {reason}\n최신 base: {snapshot.revision}\n'
              f'다음 사용 가능 G-ID: G-{next_id:03d}\n'
              f'확정 구현·테스트는 git show {snapshot.revision}:<path>로 최신 base에서 확인하세요. '
              '계획 브랜치의 미Merge 코드를 R 충족 근거로 쓰지 않습니다. '
              '최신 Requirement 전문·코드·테스트·활성/완료 G·현재 계획 PR 사용자 편집을 대조하세요. '
              '현재 파악 가능한 전체 작업을 이 주 R의 계획 PR 하나로 제안합니다. '
              '다른 R도 충족하는 공유 G는 중복 생성하지 않습니다. 실제 충돌 규칙만 높은 R-ID가 우선합니다. '
              'docs/plans 바로 아래의 이 R 관련 제안 G만 작성·개정하세요. 완료/archive G와 사용자 명세·코드는 수정하지 않습니다. '
              '미확정 정책은 draft: true와 Draft Decision의 Question/Facts/Options/Impact로 남기세요. '
              '최종 응답은 result(PLANNED/SATISFIED/UNRESOLVED), evidence의 JSON 객체입니다. '
              'SATISFIED는 최신 R 전체 AC와 실제 구현·테스트로 남은 목표가 없음을 확인한 경우만 사용합니다.\n'
              '다른 R의 충돌/공유 목표에 영향이 확인됐다면 affected_requirements에 해당 R-ID를 반환하세요. '
              '새 제안 없이 기존 확정 G로 진행할 목표는 reuses에 G-ID를 반환하세요.\n'
              f'최신 주 R 전문:\n{source.text}\n모든 활성 Requirement:\n'+
              '\n\n'.join(item['document'].text for _,item in sorted(snapshot.requirements.items()))+
              '\n\n최신 제품·설계:\n'+'\n\n'.join(item.text for item in snapshot.specifications.values())+
              '\n\n완료 G의 보존된 검증 근거:\n'+packed(store.rows('SELECT plan_id,stage,evidence FROM validation_receipts WHERE repo=?',(repo,)))+
              '\n\n이전 대기/교정 근거:\n'+str(store.rows('SELECT last_error FROM projects WHERE repo=?',(repo,))[0]['last_error'] or '없음'))
    result = agent.call(repository.project,identity,'planner',path,prompt)
    if result.kind!='SUCCESS':
        raise GitError(f'Planner {result.kind}: {result.message}')
    changed = repository.git('diff','--name-only','-z',initial_head,cwd=path).split('\0')
    changed += repository.git('ls-files','--others','--exclude-standard','-z',cwd=path).split('\0')
    changed=[name for name in set(changed) if name and
             (name not in manager_changes or file_signature(path,name)!=manager_changes[name])]
    for item in changed:
        if not re.fullmatch(r'docs/plans/[^/]+\.md',item):
            raise ValueError(f'Planner의 허용 범위를 벗어난 변경을 보존합니다: {item}')
    candidate = workspace_catalog(path,snapshot.revision)
    if candidate.errors:
        raise ValueError(f'Planner 문서 오류: {candidate.errors}')
    linked={row['plan_id'] for row in related_plans(requirement,store.rows('SELECT * FROM exec_plans WHERE repo=?',(repo,)))}
    linked.update(json.loads(opened['plan_ids']) if opened else [])
    associated = [item for item in candidate.plans.values() if source.path in item['sources'] or item['id'] in linked]
    proposed=[item for item in associated if item['id'] not in snapshot.plans
              or item['document'].text.replace('\r\n','\n')!=snapshot.plans[item['id']]['document'].text.replace('\r\n','\n')]
    allowed_existing = set(json.loads(opened['plan_ids'])) if opened else set()
    for item in proposed:
        if item['id'] in tombstones:
            raise ValueError(f'취소된 G-ID를 재사용할 수 없습니다: {item["id"]}')
        existing = snapshot.plans.get(item['id'])
        if item['id'] in reserved_ids and existing is None and item['id'] not in allowed_existing:
            raise ValueError(f'원격 이력에 있는 G-ID를 재사용할 수 없습니다: {item["id"]}')
    permitted={item['document'].path for item in proposed}
    for item in changed:
        removed=next((value for value in before.plans.values() if value['document'].path==item
                      and source.path in value['sources'] and value['id'] not in snapshot.plans),None)
        if (item not in permitted and removed is None) or item in {value['document'].path for value in snapshot.archived_plans.values()}:
            raise ValueError(f'다른 목표의 G 변경을 보존하고 계획 게시를 보류합니다: {item}')
        original=next((value for value in snapshot.plans.values() if value['document'].path==item),None)
        if original:
            definition=store.plan(repo,original['id'])
            if definition['status'] in TERMINAL:
                raise ValueError('완료·취소한 G를 소급 개정하지 않습니다.')
            if definition['primary_requirement_id'] not in (None,identity) or original['id'] not in linked and source.path not in original['sources']:
                raise ValueError('다른 주 R의 공유 G는 소속 R에서 재평가하도록 affected_requirements를 반환하세요.')
    outcome = result.decision
    reuses=outcome.get('reuses',[])
    affected=outcome.get('affected_requirements',[])
    if not isinstance(reuses,list) or any(not isinstance(key,str) or key not in snapshot.plans for key in reuses):
        raise ValueError('재사용 G는 현재 base의 확정 G-ID여야 합니다.')
    if not isinstance(affected,list) or any(not isinstance(key,str) or key not in snapshot.requirements for key in affected):
        raise ValueError('영향받는 R은 현재 활성 R-ID여야 합니다.')
    with store.transaction() as db:
        for key in set(affected)-{identity}:
            db.execute("UPDATE requirement_states SET judgment='REPLAN' WHERE repo=? AND requirement_id=?",(repo,key))
    if outcome['result']=='PLANNED' and not changed and not opened and (reuses or associated):
        existing=set(reuses)|{item['id'] for item in associated if item['id'] in snapshot.plans
                              and store.plan(repo,item['id'])['status'] not in TERMINAL}
        if existing:
            with store.transaction() as db:
                db.execute("UPDATE requirement_states SET last_planned_source_oid=?,last_planned_base_sha=?,judgment='WAITING',evidence=? "
                           'WHERE repo=? AND requirement_id=?',(source.oid,snapshot.revision,
                           packed({'summary':outcome['evidence'],'reuses':sorted(existing)}),repo,identity))
                db.execute('DELETE FROM external_jobs WHERE job_key=?',(job_key,))
            repository.defer_cleanup(path,branch)
            return True
    if outcome['result']=='SATISFIED':
        requirement=store.rows('SELECT * FROM requirement_states WHERE repo=? AND requirement_id=?',(repo,identity))[0]
        active = [row for row in related_plans(requirement,store.rows('SELECT * FROM exec_plans WHERE repo=?',(repo,)))
                  if row['status'] not in TERMINAL]
        if active:
            raise ValueError('미완료 확정 G가 남아 있어 Requirement 완료를 보류합니다.')
        current = repository.refresh()
        current_catalog = repository.snapshot(current)
        if (current!=snapshot.revision or identity not in current_catalog.requirements
                or current_catalog.requirements[identity]['document'].oid!=source.oid):
            raise GitError('완료 판단 중 Requirement가 바뀌었습니다.')
        if opened:
            with store.transaction() as db:
                db.execute("UPDATE requirement_states SET judgment='CLOSING',judgment_revision=?,evidence=? "
                           'WHERE repo=? AND requirement_id=?',
                           (packed([source.oid,current,opened['pr_number'],pr_head]),outcome['evidence'],repo,identity))
            github.close({'number':opened['pr_number']},pr_head,current)
            with store.transaction() as db:
                db.execute("UPDATE managed_prs SET state='CLOSED' WHERE repo=? AND pr_number=?",(repo,opened['pr_number']))
        with store.transaction() as db:
            db.execute("UPDATE requirement_states SET judgment='SATISFIED',judgment_revision=?,evidence=?,"
                       'last_planned_source_oid=?,last_planned_base_sha=? WHERE repo=? AND requirement_id=?',
                       (packed([source.oid,current]),outcome['evidence'],source.oid,current,repo,identity))
            db.execute('DELETE FROM external_jobs WHERE job_key=?',(job_key,))
        repository.defer_cleanup(path,branch)
        return True
    if outcome['result']=='UNRESOLVED':
        with store.transaction() as db:
            db.execute("UPDATE requirement_states SET judgment='UNRESOLVED',judgment_revision=?,evidence=? "
                       'WHERE repo=? AND requirement_id=?',(source.oid,outcome['evidence'],repo,identity))
            store.notify(db,f'unresolved:{repo}:{identity}:{source.oid}',repo,identity,
                         f'{identity} 계획 판단 보류: {outcome["evidence"]}')
        return False
    if not proposed:
        raise ValueError('PLANNED 결과에 유효한 제안 G가 없습니다.')
    candidate_sha = repository.commit(path,branch,f'{identity} 구현 계획 제안')
    repository.publish(path,branch,published=bool(opened))
    latest = repository.snapshot(repository.refresh())
    if identity not in latest.requirements or latest.requirements[identity]['document'].oid!=source.oid:
        raise GitError('계획 게시 중 Requirement가 바뀌었습니다. 최신 R을 다시 평가합니다.')
    draft = any(item['draft'] for item in proposed)
    plan_ids = [item['id'] for item in proposed]
    info = {'kind':'planning','primary_requirement_id':identity,'plan_ids':plan_ids}
    body = metadata(**{'kind':'planning','primary':identity,'plan_ids':plan_ids,'source_oid':source.oid})+'\n\n'+outcome['evidence']
    for item in proposed:
        body += f'\n\n- {item["id"]}: {item["title"]}'+(' (사용자 판단 필요)' if item['draft'] else '')
        if item['draft']:
            body += '\n'+item['document'].text.split('## Draft Decision',1)[-1]
    body += '\n\n'+usage_body(store,repo,identity)
    pull = github.ensure_pull(branch,repository.base,f'{identity} 계획: {snapshot.requirements[identity]["title"]}',body,draft=draft)
    if pull['head']['sha']!=candidate_sha:
        raise GitError('계획 PR 후보가 바뀌었습니다. 원격 사용자 편집을 재관측합니다.')
    record_pull(store,repo,pull,info,input_revision=source.oid)
    with store.transaction() as db:
        db.execute('UPDATE requirement_states SET last_planned_source_oid=?,last_planned_base_sha=?,judgment=NULL '
                   'WHERE repo=? AND requirement_id=?',(source.oid,snapshot.revision,repo,identity))
        store.notify(db,f'pr:{repo}:{pull["number"]}:created',repo,identity,
                     f'계획 PR: {pull["html_url"]}\n'+('사용자 판단이 필요한 계획이 포함됐습니다.\n'+body if draft else '계획을 검토하고 Merge하세요.'))
        db.execute('DELETE FROM external_jobs WHERE job_key=?',(job_key,))
    repository.defer_cleanup(path,branch)
    return True
