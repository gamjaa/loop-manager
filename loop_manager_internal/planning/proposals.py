"""주 Requirement별 단일 계획 PR과 원격 PR 관측."""
import hashlib
import json
from pathlib import Path
import re

from loop_manager_internal.catalog.observer import Document,catalog
from loop_manager_internal.git.repository import GitError,MergeConflict
from loop_manager_internal.storage.state import packed,TERMINAL


METADATA = re.compile(r'<!-- loop-manager:metadata (\{[^\n]+\}) -->')


def metadata(kind,primary,plan_ids):
    return '<!-- loop-manager:metadata '+packed({'kind':kind,'primary_requirement_id':primary,
                                                'plan_ids':plan_ids})+' -->'


def classification(pull):
    match = METADATA.search(pull.get('body') or '')
    if match:
        value = json.loads(match.group(1))
        if value.get('kind') in ('planning','implementation') and isinstance(value.get('plan_ids'),list):
            return value
    branch = pull['head']['ref']
    if re.fullmatch(r'loop-manager/G-[0-9]{3,}',branch):
        return {'kind':'implementation','primary_requirement_id':None,'plan_ids':[branch.rsplit('/',1)[-1]]}
    return None


def record_pull(store,repo,pull,info,*,input_revision=None):
    state = 'MERGED' if pull.get('merged_at') else 'CLOSED' if pull['state']=='closed' else 'OPEN'
    head,base = pull['head']['sha'],pull['base']['sha']
    mergeability = {True:'CLEAN',False:'CONFLICT',None:'UNKNOWN'}[pull.get('mergeable')]
    with store.transaction() as db:
        previous = db.execute('SELECT * FROM managed_prs WHERE repo=? AND pr_number=?',(repo,pull['number'])).fetchone()
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
        if info['kind']=='implementation':
            for identity in info['plan_ids']:
                db.execute('UPDATE exec_plans SET pr_url=?,status=CASE WHEN ? THEN ? ELSE status END '
                           'WHERE repo=? AND plan_id=?',
                           (pull['html_url'],state=='MERGED','MERGED',repo,identity))
        elif previous and previous['head_sha']!=head and state=='OPEN':
            db.execute("UPDATE requirement_states SET judgment='REPLAN' WHERE repo=? AND requirement_id=?",
                       (repo,info.get('primary_requirement_id')))
        return previous


def observe_pulls(store,repository,github):
    repo = repository.repo
    stored = {row['pr_number']:row for row in store.rows('SELECT * FROM managed_prs WHERE repo=?',(repo,))}
    observed = []
    for pull in github.pulls():
        if not pull['head']['repo'] or pull['head']['repo']['full_name'].casefold()!=repo.casefold():
            continue
        info = classification(pull)
        previous = stored.get(pull['number'])
        if info is None and previous:
            info = {'kind':previous['kind'],'primary_requirement_id':previous['primary_requirement_id'],
                    'plan_ids':json.loads(previous['plan_ids'])}
        if info is None:
            continue
        if pull['state']=='open' and (not previous or previous['conflict_head_sha']!=pull['head']['sha']
                or previous['conflict_base_sha']!=pull['base']['sha'] or previous['mergeability']=='UNKNOWN'):
            pull = github.pull(pull['number'])
        record_pull(store,repo,pull,info)
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
    return '\n'.join(lines)+'\n\n입력·캐시 토큰은 Provider의 정의를 유지하며 중복 합산하지 않습니다.'


def proposal(store,repository,github,agent,snapshot,identity,reason):
    repo = repository.repo
    source = snapshot.requirements[identity]['document']
    rows = store.rows("SELECT * FROM managed_prs WHERE repo=? AND kind='planning' "
                      "AND primary_requirement_id=? ORDER BY pr_number DESC",(repo,identity))
    opened = next((row for row in rows if row['state']=='OPEN'),None)
    if not opened and any(row['state']=='CLOSED' and row['input_revision']==source.oid for row in rows):
        return False
    branch = opened['branch'] if opened else f'loop-manager/plans/{identity.lower()}-{source.oid[:10]}'
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
    initial_head = repository.head(path)
    if opened and opened['mergeability']=='CONFLICT':
        repository.git('fetch','origin',repository.base,cwd=path)
        try:
            repository.merge(path,branch,f'origin/{repository.base}')
        except MergeConflict:
            pass
    all_ids = set(snapshot.plans)|set(snapshot.archived_plans)
    for row in store.rows('SELECT plan_id FROM exec_plan_tombstones WHERE repo=?',(repo,)):
        all_ids.add(row['plan_id'])
    for row in store.rows('SELECT plan_ids FROM managed_prs WHERE repo=?',(repo,)):
        all_ids.update(json.loads(row['plan_ids']))
    next_id = max((int(value[2:]) for value in all_ids),default=0)+1
    prompt = (f'주 Requirement: {identity}\n평가 사유: {reason}\n최신 base: {snapshot.revision}\n'
              f'다음 사용 가능 G-ID: G-{next_id:03d}\n'
              '최신 Requirement 전문·코드·테스트·활성/완료 G·현재 계획 PR 사용자 편집을 대조하세요. '
              '현재 파악 가능한 전체 작업을 이 주 R의 계획 PR 하나로 제안합니다. '
              '다른 R도 충족하는 공유 G는 중복 생성하지 않습니다. 실제 충돌 규칙만 높은 R-ID가 우선합니다. '
              'docs/plans 바로 아래의 이 R 관련 제안 G만 작성·개정하세요. 완료/archive G와 사용자 명세·코드는 수정하지 않습니다. '
              '미확정 정책은 draft: true와 Draft Decision의 Question/Facts/Options/Impact로 남기세요. '
              '최종 응답은 result(PLANNED/SATISFIED/UNRESOLVED), evidence의 JSON 객체입니다. '
              'SATISFIED는 최신 R 전체 AC와 실제 구현·테스트로 남은 목표가 없음을 확인한 경우만 사용합니다.\n'
              f'최신 주 R 전문:\n{source.text}')
    result = agent.call(repository.project,identity,'planner',path,prompt)
    if result.kind!='SUCCESS':
        raise GitError(f'Planner {result.kind}: {result.message}')
    changed = repository.git('diff','--name-only',initial_head,cwd=path).splitlines()
    changed += repository.git('ls-files','--others','--exclude-standard',cwd=path).splitlines()
    for item in changed:
        if not re.fullmatch(r'docs/plans/[^/]+\.md',item):
            raise ValueError(f'Planner의 허용 범위를 벗어난 변경을 보존합니다: {item}')
    candidate = workspace_catalog(path,snapshot.revision)
    if candidate.errors:
        raise ValueError(f'Planner 문서 오류: {candidate.errors}')
    proposed = [item for item in candidate.plans.values() if source.path in item['sources']]
    allowed_existing = set(json.loads(opened['plan_ids'])) if opened else set()
    for item in proposed:
        existing = snapshot.plans.get(item['id'])
        if item['id'] in all_ids and existing is None and item['id'] not in allowed_existing:
            raise ValueError(f'원격 이력에 있는 G-ID를 재사용할 수 없습니다: {item["id"]}')
    outcome = result.decision
    if outcome['result']=='SATISFIED':
        active = [row for row in store.rows('SELECT status,sources FROM exec_plans WHERE repo=?',(repo,))
                  if source.path in json.loads(row['sources']) and row['status'] not in TERMINAL]
        if active:
            raise ValueError('미완료 확정 G가 남아 있어 Requirement 완료를 보류합니다.')
        current = repository.refresh()
        current_catalog = repository.snapshot(current)
        if (current!=snapshot.revision or identity not in current_catalog.requirements
                or current_catalog.requirements[identity]['document'].oid!=source.oid):
            raise GitError('완료 판단 중 Requirement가 바뀌었습니다.')
        if opened:
            github.close({'number':opened['pr_number']},initial_head)
            with store.transaction() as db:
                db.execute("UPDATE managed_prs SET state='CLOSED' WHERE repo=? AND pr_number=?",(repo,opened['pr_number']))
        with store.transaction() as db:
            db.execute("UPDATE requirement_states SET judgment='SATISFIED',judgment_revision=?,evidence=?,"
                       'last_planned_source_oid=?,last_planned_base_sha=? WHERE repo=? AND requirement_id=?',
                       (packed([source.oid,current]),outcome['evidence'],source.oid,current,repo,identity))
            db.execute('DELETE FROM external_jobs WHERE job_key=?',(job_key,))
        return True
    if outcome['result']=='UNRESOLVED':
        with store.transaction() as db:
            db.execute("UPDATE requirement_states SET judgment='UNRESOLVED',judgment_revision=?,evidence=? "
                       'WHERE repo=? AND requirement_id=?',(source.oid,outcome['evidence'],repo,identity))
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
    body = metadata(**{'kind':'planning','primary':identity,'plan_ids':plan_ids})+'\n\n'+outcome['evidence']
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
    return True
