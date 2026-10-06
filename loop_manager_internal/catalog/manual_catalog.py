"""The explicit, identity-bound catalog exception for unmerged manual intake."""
from dataclasses import replace
import json
import re
from pathlib import Path

from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository


def declaration(git, repo, revision, plan_id):
    records=git(Path(repo),'ls-tree','-rz',revision,'--','docs/plans').stdout.split('\0')
    paths=[record.partition('\t')[2] for record in records
           if record.startswith(('100644 blob ','100755 blob '))
           and re.fullmatch(r'docs/plans/\['+re.escape(plan_id)+r'\] [^/]+\.md',record.partition('\t')[2])]
    if len(paths)!=1:
        raise RuntimeError(f'원격 base의 G 선언을 확인할 수 없습니다: {plan_id}')
    text=git(Path(repo),'show',f'{revision}:{paths[0]}').stdout
    plan=execplan_catalog.parse_execplan_text(paths[0],text,validate_sources=False)
    if plan['id']!=plan_id:
        raise RuntimeError('원격 base G의 문서 identity가 다릅니다.')
    return {'path':paths[0],'text':text,'plan':plan}


def updated_base_documents(git, repo, group, revision, documents, *, candidate_head=None):
    """Prefer a later user base declaration over an older manual candidate."""
    if group['kind']!='start':
        return documents
    current=[declaration(git,repo,revision,item['plan']['id']) for item in documents]
    prior=[declaration(git,repo,group['base_sha'],item['plan']['id']) for item in documents]
    if current==prior or candidate_head and git(Path(repo),'merge-base','--is-ancestor',revision,
                                               candidate_head,check=False).returncode==0:
        return documents
    return current


def overlay(connection, repo, pinned, *, git=None):
    store = ManualWorkRepository(connection)
    groups = connection.execute("SELECT * FROM manual_work_groups WHERE repo=? "
        "AND state IN ('STARTING','MANUAL_WORKING','REGISTERING','AUTOMATIC','PR_OPEN','CLOSED')", (repo,)).fetchall()
    active = dict(pinned.snapshot.active)
    paths = set(pinned.regular_paths or ())
    revisions = dict(pinned.definition_revisions)
    pending_ids = set(pinned.reserved_intake_ids)
    for group in groups:
        members = json.loads(group['members'])
        if group['state']=='REGISTERING':
            pending_ids.update(members)
            continue
        if not group['prepared_head'] or not members:
            continue
        documents=json.loads(group['documents'])
        if group['kind']=='start':
            # Existing Gs remain declarations of the registered base. The
            # branch registration exception cannot conceal a user deletion.
            if any(member not in pinned.snapshot.active for member in members):
                continue
            if git is not None:
                updated=updated_base_documents(git,repo,group,pinned.revision_sha,documents)
                if updated!=documents:
                    with connection:
                        group=store.update(group,documents=json.dumps(updated,ensure_ascii=False),
                            base_sha=pinned.revision_sha,definition_head=pinned.revision_sha)
                    documents=updated
        for item in documents:
            key = item['plan']['id']
            store.workspace_owner(repo, key)  # Validate full recorded membership.
            row = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo,key)).fetchone()
            if row is None or row['branch']!=group['branch'] or row['worktree']!=group['worktree']:
                raise RuntimeError('branch 등록된 수동 제출 G의 SQLite 소유 identity가 다릅니다.')
            if key not in pinned.snapshot.archived:
                active[key] = execplan_catalog._entry(item['path'],item['text'],item['plan'])
                paths.update((item['path'],*item['plan']['sources']))
                revisions[key] = group['definition_head'] or group['prepared_head']
    return replace(pinned, snapshot=replace(pinned.snapshot,active=active), regular_paths=frozenset(paths),
                   definition_revisions=tuple(sorted(revisions.items())),reserved_intake_ids=frozenset(pending_ids))
