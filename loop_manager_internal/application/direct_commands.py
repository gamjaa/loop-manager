"""재구현 CLI의 직접 DB 명령. runtime 실행 잠금은 획득하지 않는다."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from loop_manager_internal.git.repository import Repository, command, github_repository
from loop_manager_internal.storage.state import Store, StateError


def build_parser():
    parser = argparse.ArgumentParser(description='Requirement에서 계획·구현 PR까지 관리합니다.')
    parser.add_argument('--state-dir',help='운영 상태 디렉터리')
    parser.add_argument('--config',help='모델·실행·Telegram 설정 파일')
    commands = parser.add_subparsers(dest='command',required=True)
    commands.add_parser('init',help='신규 WAL DB 초기화')
    commands.add_parser('list',help='등록 프로젝트 조회')
    add = commands.add_parser('add',help='프로젝트 등록 요청')
    add.add_argument('repository',help='로컬 Git 저장소')
    add.add_argument('--base',help='원격 base 브랜치')
    add.add_argument('--provider',choices=('codex','claude'),default='codex')
    remove = commands.add_parser('remove',help='등록 해제 요청')
    remove.add_argument('repository')
    for name in ('pause','resume'):
        item = commands.add_parser(name,help='신규 단계 배정 일시중지' if name=='pause' else '배정 재개')
        item.add_argument('repository',nargs='?',help='생략하면 전체 프로젝트')
    for name in ('status','usage','logs'):
        item = commands.add_parser(name,help={'status':'실행·대기·요청 상태','usage':'역할·모델별 실제 사용량','logs':'현재 오류·호출 결과'}[name])
        item.add_argument('repository',nargs='?')
    unblock = commands.add_parser('unblock',help='BLOCKED G의 재평가 요청')
    unblock.add_argument('repository')
    unblock.add_argument('plan_id')
    manual = commands.add_parser('manual',help='확정 G의 수동 구현·제출')
    modes = manual.add_subparsers(dest='manual_command',required=True)
    for name in ('start','complete'):
        item = modes.add_parser(name)
        item.add_argument('repository')
        item.add_argument('plan_id')
    run = commands.add_parser('run',help='등록 프로젝트 실행')
    run.add_argument('repositories',nargs='*')
    run.add_argument('--skip-if-running',action='store_true')
    return parser


def project(store, reference):
    candidates = store.rows('SELECT * FROM projects WHERE repo=? OR path=?',
                            (reference,str(Path(reference).expanduser().resolve())))
    if len(candidates)!=1:
        raise StateError(f'등록된 프로젝트를 찾을 수 없습니다: {reference}')
    return candidates[0]


def query(store, kind, repo=None):
    condition,parameters = (' WHERE repo=?',(repo,)) if repo else ('',())
    if kind=='list':
        return store.rows('SELECT repo,path,base_branch,provider,enabled,request_id,request_kind,request_result '
                          'FROM projects'+condition+' ORDER BY repo',parameters)
    if kind=='status':
        return {'projects':query(store,'list',repo),
                'pause':store.rows('SELECT * FROM pause_scopes'+condition,parameters),
                'plans':store.rows('SELECT p.repo,p.plan_id,p.title,p.status,p.draft,p.pr_url,p.block_reason,'
                                   'r.generation,r.manual_mode,r.worktree,r.retry_after,r.last_error,r.request_id,'
                                   'r.request_kind,r.request_result FROM exec_plans p JOIN exec_plan_runs r '
                                   'USING(repo,plan_id)'+condition+' ORDER BY repo,plan_id',parameters)}
    if kind=='usage':
        return store.rows('SELECT repo,scope,role,provider,model,COUNT(*) AS calls,'
                          'CASE WHEN COUNT(input_tokens)=COUNT(*) THEN SUM(input_tokens) END AS input_tokens,'
                          'CASE WHEN COUNT(cache_read_tokens)=COUNT(*) THEN SUM(cache_read_tokens) END AS cache_read_tokens,'
                          'CASE WHEN COUNT(cache_write_tokens)=COUNT(*) THEN SUM(cache_write_tokens) END AS cache_write_tokens,'
                          'CASE WHEN COUNT(output_tokens)=COUNT(*) THEN SUM(output_tokens) END AS output_tokens '
                          'FROM agent_calls'+condition+' GROUP BY repo,scope,role,provider,model',parameters)
    if kind=='logs':
        return {'current':store.rows('SELECT repo,plan_id,last_error,retry_after FROM exec_plan_runs'+condition,parameters),
                'calls':store.rows('SELECT invocation_id,repo,scope,role,result,created_at FROM agent_calls'+condition+
                                   ' ORDER BY created_at DESC LIMIT 100',parameters)}
    raise ValueError('지원하지 않는 조회입니다.')


def register(store,args):
    path = Path(args.repository).expanduser().resolve()
    root = Path(command(['git','rev-parse','--show-toplevel'],cwd=path).stdout.strip()).resolve()
    if path != root:
        raise ValueError('프로젝트의 Git 루트 경로를 지정하세요.')
    remote = command(['git','remote','get-url','origin'],cwd=path).stdout.strip()
    repo = github_repository(remote)
    base = args.base
    if base is None:
        base = command(['git','symbolic-ref','--short','refs/remotes/origin/HEAD'],cwd=path).stdout.strip()
        if not base.startswith('origin/'):
            raise ValueError('origin/HEAD를 확인하거나 --base를 지정하세요.')
        base = base[len('origin/'):]
    command(['git','check-ref-format','--branch',base],cwd=path)
    return {'request_id':store.register(repo,path,remote,base,args.provider),'result':'등록 요청 접수'}


def manual_start(store, state_dir, item):
    repo,identity = item['repo'],item['plan_id']
    existing = store.plan(repo,identity)
    if store.paused(repo):
        raise StateError('일시중지 상태입니다. 수동 준비를 시작하려면 resume을 실행하세요.')
    repository = Repository(project(store,repo),state_dir,store)
    if existing['manual_mode']=='MANUAL_WORKING':
        path = repository.validate_workspace(existing['worktree'],existing['branch'])
        return {'result':'수동 준비 완료','worktree':str(path),'branch':existing['branch']}
    request_id = store.request(repo,identity,'manual_start')
    current = store.plan(repo,identity)
    def reserved(path):
        with store.transaction() as db:
            if not db.execute("UPDATE exec_plan_runs SET worktree=? WHERE repo=? AND plan_id=? "
                              "AND request_id=? AND manual_mode='PREPARING'",
                              (str(path),repo,identity,request_id)).rowcount:
                raise StateError('수동 준비 예약이 변경됐습니다.')
    try:
        path = repository.prepare(current['branch'],saved_path=current['worktree'],published=bool(current['pr_url']),
                                  on_reserved=reserved)
    except (OSError,RuntimeError) as error:
        # 예약을 유지하므로 재실행이 자동 Worker와 경쟁하지 않는다.
        with store.transaction() as db:
            db.execute('UPDATE exec_plan_runs SET last_error=? WHERE repo=? AND plan_id=? AND request_id=?',
                       (str(error),repo,identity,request_id))
        raise
    with store.transaction() as db:
        updated = db.execute("UPDATE exec_plan_runs SET worktree=? WHERE repo=? AND plan_id=? "
                             "AND request_id=? AND manual_mode='PREPARING'",(str(path),repo,identity,request_id)).rowcount
        if not updated:
            raise StateError('수동 준비 요청이 바뀌었습니다. Git 작업물은 보존합니다.')
    store.finish_request(repo,identity,request_id,'수동 준비 완료',cli_final=True,
                         manual_mode='MANUAL_WORKING',worktree=str(path))
    return {'request_id':request_id,'result':'수동 준비 완료','worktree':str(path),'branch':current['branch']}


def execute(args,store,state_dir):
    if args.command=='init':
        store.initialize()
        return {'result':'신규 WAL DB 준비 완료','database':str(store.path)}
    if args.command=='add':
        return register(store,args)
    reference = getattr(args,'repository',None)
    repo = project(store,reference)['repo'] if reference else None
    if args.command in ('list','status','usage','logs'):
        return query(store,args.command,repo)
    if args.command in ('pause','resume'):
        store.pause(repo or '*',args.command=='pause')
        return {'result':'일시중지 반영' if args.command=='pause' else '재개 반영','repo':repo or '*'}
    if args.command=='remove':
        import uuid
        with store.transaction() as db:
            row = db.execute('SELECT * FROM projects WHERE repo=?',(repo,)).fetchone()
            if row['request_kind']:
                if row['request_kind']=='remove':
                    return {'request_id':row['request_id'],'result':'등록 해제 요청 접수'}
                raise StateError('프로젝트에 처리 중인 요청이 있습니다.')
            request_id = uuid.uuid4().hex
            db.execute("UPDATE projects SET enabled=0,revision=revision+1,request_id=?,request_kind='remove',"
                       "feedback_mode='TELEGRAM_PENDING',request_result=NULL WHERE repo=? AND revision=?",
                       (request_id,repo,row['revision']))
        return {'request_id':request_id,'result':'등록 해제 요청 접수'}
    if args.command=='unblock':
        return {'request_id':store.request(repo,args.plan_id,'unblock'),'result':'차단 재평가 요청 접수'}
    if args.command=='manual':
        if args.manual_command=='start':
            return manual_start(store,state_dir,{'repo':repo,'plan_id':args.plan_id})
        return {'request_id':store.request(repo,args.plan_id,'manual_complete'),'result':'수동 제출 요청 접수'}
    raise ValueError('실행 명령은 Coordinator가 담당합니다.')


def output(value):
    print(json.dumps(value,ensure_ascii=False,indent=2))
