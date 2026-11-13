"""확정 완료 뒤 Archive·실행 데이터 정리. 정리 실패는 실행을 차단하지 않는다."""
import json
from pathlib import Path
import shutil

from loop_manager_internal.catalog.frontmatter import requirement_identity,parse_frontmatter
from loop_manager_internal.git.repository import GitError
from loop_manager_internal.execution.processes import still_running
from loop_manager_internal.storage.state import packed


def archive(store,repository,kind,identity,source):
    repo = repository.repo
    key = f'archive:{repo}:{identity}:{source.oid}'
    with store.transaction() as db:
        db.execute('INSERT INTO external_jobs(job_key,repo,kind,target,expected_input) VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING',
                   (key,repo,'archive',identity,source.oid))
    rows = store.rows('SELECT remote_result FROM external_jobs WHERE job_key=?',(key,))
    saved = rows[0]['remote_result'] if rows else None
    def reserved(path):
        with store.transaction() as db:
            db.execute('UPDATE external_jobs SET remote_result=? WHERE job_key=?',(str(path),key))
    path = repository.prepare(repository.base,saved_path=saved,published=True,on_reserved=reserved)
    source_path = Path(path)/source.path
    destination = source_path.parent/'archive'/source_path.name
    allowed = {source.path,destination.relative_to(path).as_posix()}
    changed = set(repository.git('diff','--name-only','HEAD',cwd=path).splitlines())
    changed.update(repository.git('ls-files','--others','--exclude-standard',cwd=path).splitlines())
    if changed-allowed:
        raise GitError('Archive 공간에 별도 사용자 변경이 있습니다. 게시하지 않고 보존합니다.')
    if not source_path.exists():
        if not destination.is_file():
            raise GitError('Archive 원본과 목적지를 확인할 수 없습니다.')
        content = destination.read_text(encoding='utf-8')
        actual = requirement_identity(content)[0] if kind=='requirement' else parse_frontmatter(content)['id']
        if actual!=identity:
            raise GitError('Archive ID가 다릅니다.')
    else:
        actual_oid = repository.git('hash-object','--',source.path,cwd=path)
        if actual_oid!=source.oid or source_path.read_text(encoding='utf-8').replace('\r\n','\n')!=source.text.replace('\r\n','\n'):
            raise GitError('Archive 직전 문서가 바뀌었습니다. 최신 완료 근거를 다시 확인합니다.')
        if destination.exists():
            raise GitError('Archive 목적지 문서를 덮어쓰지 않습니다.')
        destination.parent.mkdir(parents=True,exist_ok=True)
        source_path.rename(destination)
    repository.commit(path,repository.base,f'{identity} 완료 문서 보관')
    repository.publish(path,repository.base,published=True)
    observed = repository.snapshot(repository.refresh())
    archived = observed.archived_requirements if kind=='requirement' else observed.archived_plans
    if identity not in archived:
        raise GitError('원격 Archive 게시가 아직 확인되지 않았습니다.')
    with store.transaction() as db:
        db.execute('DELETE FROM external_jobs WHERE job_key=?',(key,))
    repository.cleanup(path,repository.base)


def sessions(store,state_dir,repo,scope):
    calls = store.rows('SELECT provider,session_id,invocation_id FROM agent_calls WHERE repo=? AND scope=?',(repo,scope))
    for call in calls:
        session = call['session_id']
        if session and all(char.isalnum() or char=='-' for char in session):
            shared = store.rows('SELECT 1 FROM agent_calls WHERE session_id=? AND (repo!=? OR scope!=?) LIMIT 1',
                                (session,repo,scope))
            if not shared:
                home = Path(state_dir)/f'automation-{call["provider"]}'
                for folder in ('sessions','archived_sessions','projects'):
                    if not (home/folder).is_dir():
                        continue
                    for path in (home/folder).rglob(f'*{session}*'):
                        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(home.resolve()):
                            path.unlink()
        for directory in Path(state_dir).glob(f'agent-{call["invocation_id"]}-*'):
            if directory.is_dir() and not directory.is_symlink() and directory.resolve().parent==Path(state_dir).resolve():
                shutil.rmtree(directory)
    with store.transaction() as db:
        db.execute('DELETE FROM agent_calls WHERE repo=? AND scope=?',(repo,scope))


def complete(store,repository,github,snapshot):
    repo = repository.repo
    failures = []
    for plan in store.rows("SELECT p.*,r.worktree FROM exec_plans p JOIN exec_plan_runs r USING(repo,plan_id) "
                            "WHERE repo=? AND status IN ('MERGED','CANCELLED')",(repo,)):
        identity = plan['plan_id']
        try:
            processes = store.rows('SELECT process_pid,process_identity FROM exec_plan_runs WHERE repo=? AND plan_id=?',
                                   (repo,identity))
            processes += store.rows('SELECT process_pid,process_identity FROM agent_calls WHERE repo=? AND scope=?',(repo,identity))
            if any(still_running(row['process_pid'],row['process_identity']) for row in processes):
                continue
            if plan['status']=='MERGED' and identity in snapshot.plans:
                archive(store,repository,'plan',identity,snapshot.plans[identity]['document'])
            if plan['status']=='CANCELLED':
                prs = store.rows("SELECT * FROM managed_prs WHERE repo=? AND kind='implementation' AND state='OPEN'",(repo,))
                for row in prs:
                    if identity in json.loads(row['plan_ids']):
                        github.close({'number':row['pr_number']},row['head_sha'])
                        with store.transaction() as db:
                            db.execute("UPDATE managed_prs SET state='CLOSED' WHERE repo=? AND pr_number=?",(repo,row['pr_number']))
            if store.rows("SELECT 1 FROM notification_outbox WHERE repo=? AND scope=? AND state='PENDING'",(repo,identity)):
                continue
            if plan['worktree'] and Path(plan['worktree']).exists():
                if not repository.cleanup(plan['worktree'],plan['branch']):
                    continue
            # 최종 PR 본문 집계를 확인할 때까지 호출 원본은 유지한다.
            managed = store.rows("SELECT * FROM managed_prs WHERE repo=? AND kind='implementation'",(repo,))
            if plan['status']=='MERGED' and not any(identity in json.loads(row['plan_ids']) for row in managed):
                continue
            sessions(store,repository.state_dir,repo,identity)
            with store.transaction() as db:
                db.execute('UPDATE exec_plan_runs SET worktree=NULL,candidate_sha=NULL,last_error=NULL,retry_after=NULL,'
                           'request_kind=NULL,request_id=NULL,request_result=NULL,manual_mode=NULL WHERE repo=? AND plan_id=?',
                           (repo,identity))
        except (OSError,RuntimeError,ValueError) as error:
            failures.append(f'{identity} 후속 정리: {error}')
    for row in store.rows("SELECT * FROM requirement_states WHERE repo=? AND judgment='SATISFIED'",(repo,)):
        identity = row['requirement_id']
        try:
            if identity in snapshot.requirements:
                source = snapshot.requirements[identity]['document']
                if row['last_planned_source_oid']!=source.oid:
                    continue
                archive(store,repository,'requirement',identity,source)
            elif identity not in snapshot.archived_requirements:
                continue
            sessions(store,repository.state_dir,repo,identity)
            with store.transaction() as db:
                db.execute('UPDATE requirement_states SET evidence=NULL WHERE repo=? AND requirement_id=?',(repo,identity))
                # 미완료 R의 최종 판단에 필요한 Verify 근거는 남긴다.
                active_sources = {item['document'].path for key,item in snapshot.requirements.items() if key!=identity}
                for plan in db.execute("SELECT plan_id,sources,status FROM exec_plans WHERE repo=? AND status IN ('MERGED','CANCELLED')",(repo,)):
                    if not active_sources.intersection(json.loads(plan['sources'])):
                        db.execute('DELETE FROM validation_receipts WHERE repo=? AND plan_id=?',(repo,plan['plan_id']))
        except (OSError,RuntimeError,ValueError) as error:
            failures.append(f'{identity} 후속 보관: {error}')
    return failures
