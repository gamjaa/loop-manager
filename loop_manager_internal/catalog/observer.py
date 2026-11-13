"""원격 Git 트리의 문서 식별과 확정 정의 반영. 조회 실패는 삭제가 아니다."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import json
import re

from loop_manager_internal.catalog.frontmatter import parse_frontmatter, requirement_identity
from loop_manager_internal.storage.state import packed, TERMINAL


@dataclass(frozen=True)
class Document:
    path: str
    oid: str
    text: str


@dataclass
class Catalog:
    revision: str
    requirements: dict = field(default_factory=dict)
    plans: dict = field(default_factory=dict)
    archived_requirements: dict = field(default_factory=dict)
    archived_plans: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)


def parse_plan(document):
    values = parse_frontmatter(document.text)
    if not values or not isinstance(values.get('id'),str) or not re.fullmatch(r'G-[0-9]{3,}',values['id']):
        raise ValueError('G-ID frontmatter가 필요합니다.')
    if int(values['id'][2:]) == 0:
        raise ValueError('G-ID는 1부터 시작합니다.')
    for key in ('title','complexity','risk'):
        if not isinstance(values.get(key),str) or not values[key].strip():
            raise ValueError(f'필수 metadata가 없습니다: {key}')
    for key, allowed in (('complexity',('low','medium','high')),
                         ('risk',('low','medium','high','critical')),
                         ('priority',('critical','high','normal','low'))):
        if key == 'priority':
            values.setdefault(key,'normal')
        if values[key] not in allowed:
            raise ValueError(f'잘못된 {key}: {values[key]}')
    for key in ('sources','depends_on'):
        if not isinstance(values.get(key),list) or any(not isinstance(item,str) or not item for item in values[key]):
            raise ValueError(f'{key}는 경로/ID 목록이어야 합니다.')
        if len(values[key]) != len(set(values[key])):
            raise ValueError(f'{key}에 중복 값이 있습니다.')
    if not values['sources']:
        raise ValueError('sources가 비어 있습니다.')
    for path in values['sources']:
        parsed = PurePosixPath(path)
        if (parsed.is_absolute() or '..' in parsed.parts
                or not (path == 'docs/PRODUCT.md' or re.fullmatch(r'docs/requirements/[^/]+\.md',path))):
            raise ValueError(f'유효하지 않은 source 경로: {path}')
    if any(not re.fullmatch(r'G-[0-9]{3,}',item) for item in values['depends_on']):
        raise ValueError('유효하지 않은 dependency ID입니다.')
    draft = values.get('draft','false')
    if draft not in ('true','false'):
        raise ValueError('draft는 true/false만 허용합니다.')
    values['draft'] = draft == 'true'
    if values['draft'] and any(not re.search(rf'^###? {name}\s*$',document.text,re.M)
                               for name in ('Draft Decision','Question','Facts','Options','Impact')):
        raise ValueError('draft G에 Draft Decision과 Question/Facts/Options/Impact가 필요합니다.')
    return {**values,'document':document}


def catalog(revision, documents):
    result = Catalog(revision)
    all_ids = {}
    for document in documents:
        path = PurePosixPath(document.path)
        if path.suffix != '.md' or str(path.parent) not in (
                'docs/requirements','docs/requirements/archive','docs/plans','docs/plans/archive'):
            continue
        try:
            if path.parts[1] == 'requirements':
                identity,title = requirement_identity(document.text)
                value = {'id':identity,'title':title,'document':document}
                collection = result.archived_requirements if 'archive' in path.parts else result.requirements
            else:
                value = parse_plan(document)
                identity = value['id']
                collection = result.archived_plans if 'archive' in path.parts else result.plans
            if identity in all_ids:
                result.errors[all_ids[identity]] = f'중복 ID: {identity}'
                raise ValueError(f'중복 ID: {identity}')
            all_ids[identity] = document.path
            collection[identity] = value
        except (RuntimeError,ValueError) as error:
            result.errors[document.path] = str(error)
    all_plans = {**result.archived_plans,**result.plans}
    for identity,plan in all_plans.items():
        for dependency in plan['depends_on']:
            if dependency == identity or dependency not in all_plans:
                result.errors[plan['document'].path] = f'잘못된 dependency: {dependency}'
    visiting,visited = [],set()
    def visit(identity):
        if identity in visiting:
            for cycle_id in visiting[visiting.index(identity):]:
                result.errors[all_plans[cycle_id]['document'].path] = '순환 dependency'
            return
        if identity in visited:
            return
        visiting.append(identity)
        for dependency in all_plans[identity]['depends_on']:
            if dependency in all_plans:
                visit(dependency)
        visiting.pop()
        visited.add(identity)
    for identity in all_plans:
        visit(identity)
    return result


def synchronize(store, repo, snapshot):
    """완전히 관측한 Git 트리만 받는다. 오류 문서는 보존하고 독립 G를 반영한다."""
    old_requirements = {row['requirement_id']:row for row in store.rows(
        'SELECT * FROM requirement_states WHERE repo=?',(repo,))}
    changed = {identity for identity,item in snapshot.requirements.items()
               if identity not in old_requirements or old_requirements[identity]['source_oid'] != item['document'].oid}
    missing = set(old_requirements) - snapshot.requirements.keys() - snapshot.archived_requirements.keys()
    # 파싱 실패한 문서가 있으면 그 정체를 삭제로 추정할 수 없다.
    if snapshot.errors:
        missing = set()
    with store.transaction() as db:
        for identity,item in snapshot.requirements.items():
            doc = item['document']
            if doc.path in snapshot.errors:
                continue
            db.execute('INSERT INTO requirement_states(repo,requirement_id,source_path,source_oid) '
                       'VALUES(?,?,?,?) ON CONFLICT(repo,requirement_id) DO UPDATE '
                       'SET source_path=excluded.source_path,source_oid=excluded.source_oid',
                       (repo,identity,doc.path,doc.oid))
        source_ids = {item['document'].path:identity for identity,item in snapshot.requirements.items()}
        existing = {row['plan_id']:dict(row) for row in db.execute('SELECT * FROM exec_plans WHERE repo=?',(repo,))}
        for identity,item in {**snapshot.archived_plans,**snapshot.plans}.items():
            doc = item['document']
            if doc.path in snapshot.errors:
                continue
            if db.execute('SELECT 1 FROM exec_plan_tombstones WHERE repo=? AND plan_id=?',(repo,identity)).fetchone():
                snapshot.errors[doc.path] = '취소된 G-ID를 재사용할 수 없습니다.'
                continue
            old = existing.get(identity)
            if old and old['status'] in TERMINAL:
                continue
            primary = (old or {}).get('primary_requirement_id')
            if primary is None:
                primary = next((source_ids[source] for source in item['sources'] if source in source_ids),None)
            db.execute('INSERT INTO exec_plans(repo,plan_id,source_path,source_oid,title,primary_requirement_id,'
                       'sources,depends_on,complexity,risk,priority,draft,branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(repo,plan_id) DO UPDATE SET source_path=excluded.source_path,source_oid=excluded.source_oid,'
                       'title=excluded.title,sources=excluded.sources,depends_on=excluded.depends_on,'
                       'complexity=excluded.complexity,risk=excluded.risk,priority=excluded.priority,draft=excluded.draft',
                       (repo,identity,doc.path,doc.oid,item['title'],primary,packed(item['sources']),
                        packed(item['depends_on']),item['complexity'],item['risk'],item['priority'],
                        int(item['draft']),f'loop-manager/{identity}'))
            db.execute('INSERT INTO exec_plan_runs(repo,plan_id) VALUES(?,?) ON CONFLICT DO NOTHING',(repo,identity))
            relevant = {source_ids[source] for source in item['sources'] if source in source_ids}
            if old and (old['source_oid'] != doc.oid or relevant & changed):
                db.execute('UPDATE exec_plan_runs SET generation=generation+1,input_fingerprint=NULL,worker_result=NULL '
                           'WHERE repo=? AND plan_id=?',(repo,identity))
                db.execute("UPDATE exec_plans SET status='READY' WHERE repo=? AND plan_id=? AND status!='BLOCKED'",
                           (repo,identity))
        db.execute('UPDATE projects SET observed_base_sha=? WHERE repo=?',(snapshot.revision,repo))
    if not snapshot.errors:
        for identity,old in existing.items():
            if identity not in snapshot.plans and identity not in snapshot.archived_plans:
                store.cancel(repo,identity,snapshot.revision,'원격 base의 전체 트리에서 G 철회 확인')
    return changed,missing


def planning_targets(store, repo, snapshot):
    """코드·PRODUCT·G만 바뀌어서는 Planner를 호출하지 않는다."""
    targets = []
    rows = {row['requirement_id']:row for row in store.rows('SELECT * FROM requirement_states WHERE repo=?',(repo,))}
    for identity,item in sorted(snapshot.requirements.items(),key=lambda pair:int(pair[0][2:]),reverse=True):
        row = rows.get(identity)
        if item['document'].path in snapshot.errors:
            continue
        if row and row['judgment']=='UNRESOLVED' and row['judgment_revision']==item['document'].oid:
            continue
        if row is None or row['last_planned_source_oid'] != item['document'].oid or row['judgment']=='REPLAN':
            targets.append((identity,'requirement_changed'))
            continue
        related = [plan for plan in store.rows('SELECT * FROM exec_plans WHERE repo=?',(repo,))
                   if item['document'].path in json.loads(plan['sources'])]
        if related and all(plan['status'] in TERMINAL for plan in related) and row['judgment'] not in ('SATISFIED','UNRESOLVED'):
            targets.append((identity,'final_acceptance'))
    return targets
