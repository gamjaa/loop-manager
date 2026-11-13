"""단계별 자원 배정과 generation에 결합된 결과 적용."""
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
import hashlib
import json
from pathlib import Path
import time

from loop_manager_internal.agents.runner import Agent,instructions
from loop_manager_internal.catalog.observer import synchronize,planning_targets,parse_plan,Document
from loop_manager_internal.execution.processes import still_running,VerifyTerminationUncertain
from loop_manager_internal.execution.validation import verify_command,verify,fingerprint
from loop_manager_internal.execution.validation_feedback import bounded_diagnostic
from loop_manager_internal.git.repository import Repository,GitHub,GitError,MergeConflict,RemoteDeleted
from loop_manager_internal.notifications.outbox import deliver
from loop_manager_internal.planning.proposals import proposal,observe_pulls,record_pull,metadata,usage_body,classification
from loop_manager_internal.storage.state import packed,TERMINAL,StateError
from loop_manager_internal.application.completion import complete
from loop_manager_internal.config.settings import Settings
from loop_manager_internal.runtime.process_lock import process_lock
from loop_manager_internal.runtime.contracts import ProcessLockBusyError


class Coordinator:
    def __init__(self,store,state_dir,runtime_root,settings,*,repository_factory=Repository,github_factory=GitHub,agent=None,
                 runtime_boundary=None):
        self.store,self.state_dir,self.root,self.settings = store,Path(state_dir),Path(runtime_root),settings
        self.repository_factory,self.github_factory = repository_factory,github_factory
        self.agent = agent or Agent(store,state_dir,runtime_root,settings)
        self.repositories,self.github,self.catalogs = {},{},{}
        self.active,self.completed_verifies,self.completed_reviews = {},set(),set()
        self.planned_inputs = set()
        self.runtime_boundary = runtime_boundary
        self.handoff_target = None
        self.configuration_hash=None

    def settings_boundary(self):
        if isinstance(self.settings,Settings):
            content=self.settings.path.read_bytes()
            current=hashlib.sha256(content).hexdigest()
            if current!=self.configuration_hash:
                self.settings=Settings(self.settings.path)
                self.agent.settings=self.settings
            self.configuration_hash=current

    def boundary(self):
        if self.runtime_boundary and self.handoff_target is None:
            self.handoff_target = self.runtime_boundary()
        return self.handoff_target is None

    def observe(self,project):
        repository=self.repositories.setdefault(project['repo'],self.repository_factory(project,self.state_dir,self.store))
        with repository.observation():
            return self._observe(project)

    def _observe(self,project):
        repo = project['repo']
        repository = self.repositories.setdefault(repo,self.repository_factory(project,self.state_dir,self.store))
        github = self.github.setdefault(repo,self.github_factory(repo))
        revision = repository.refresh()
        snapshot = repository.snapshot(revision)
        previous={row['plan_id']:row for row in self.store.rows('SELECT p.plan_id,p.source_oid,p.status,r.specification_fingerprint '
                  'FROM exec_plans p JOIN exec_plan_runs r USING(repo,plan_id) WHERE repo=?',(repo,))}
        changed,_=synchronize(self.store,repo,snapshot)
        pulls=observe_pulls(self.store,repository,github)
        self.catalogs[repo] = snapshot
        for pull in pulls:
            info=classification(pull)
            if not info or info['kind']!='implementation' or pull['state']!='open' or pull.get('merged_at'):
                continue
            for identity in info['plan_ids']:
                if identity not in snapshot.plans or (repo,identity) in self.active or self.live_process(repo,identity):
                    continue
                plan=self.store.plan(repo,identity)
                if plan['status'] in TERMINAL or plan['status']=='BLOCKED' or plan['manual_mode'] in ('PREPARING','MANUAL_WORKING'):
                    continue
                current_sources=set(json.loads(plan['sources']))
                changed_sources={snapshot.requirements[key]['document'].path for key in changed if key in snapshot.requirements}
                rework=identity in previous and (previous[identity]['specification_fingerprint']!=plan['specification_fingerprint']
                                                 or bool(current_sources & changed_sources))
                if not rework and plan['candidate_sha']!=pull['head']['sha']:
                    if not pull['draft']:
                        github.set_draft(pull,True)
                    path=repository.prepare(plan['branch'],published=True)
                    with self.store.transaction() as db:
                        db.execute("UPDATE exec_plans SET status='VERIFYING' WHERE repo=? AND plan_id=?",(repo,identity))
                        db.execute('UPDATE exec_plan_runs SET generation=generation+1,input_fingerprint=NULL,worktree=?,candidate_sha=?,retry_after=NULL '
                                   'WHERE repo=? AND plan_id=?',(str(path),pull['head']['sha'],repo,identity))
        if project['request_kind']=='register':
            self.store.finish_project_request(repo,project['request_id'],f'{repo} 등록 확인 완료')
        return snapshot

    def retry(self,repo,identity,error):
        message = bounded_diagnostic(str(error))
        with self.store.transaction() as db:
            if identity.startswith('G-'):
                db.execute('UPDATE exec_plan_runs SET retry_after=?,last_error=? WHERE repo=? AND plan_id=?',
                           (time.time()+30,message,repo,identity))
            else:
                db.execute('UPDATE projects SET last_error=? WHERE repo=?',(message,repo))

    def live_process(self,repo,identity):
        rows = self.store.rows('SELECT process_pid,process_identity FROM agent_calls '
                               'WHERE repo=? AND scope=? AND process_pid IS NOT NULL',(repo,identity))
        rows += self.store.rows('SELECT process_pid,process_identity FROM exec_plan_runs '
                                'WHERE repo=? AND plan_id=? AND process_pid IS NOT NULL',(repo,identity))
        return any(still_running(row['process_pid'],row['process_identity']) for row in rows)

    def stalled(self):
        for row in self.store.rows("SELECT p.repo,p.plan_id,r.progress_at FROM exec_plans p JOIN exec_plan_runs r USING(repo,plan_id) "
                                   "WHERE p.status='WORKING' AND r.manual_mode IS NULL AND r.progress_at<? "
                                   "AND (SELECT COUNT(*) FROM agent_calls a WHERE a.repo=p.repo AND a.scope=p.plan_id "
                                   "AND a.role='worker' AND a.created_at>=r.progress_at AND a.result IS NOT NULL "
                                   "AND a.result NOT IN ('USAGE_LIMIT','SESSION_LOST'))>=2",(time.time()-86400,)):
            with self.store.transaction() as db:
                self.store.notify(db,f'stalled:{row["repo"]}:{row["plan_id"]}:{row["progress_at"]}',row['repo'],row['plan_id'],
                                  f'{row["plan_id"]}: 여러 실제 Worker 시도 후 24시간 동안 후보 진전이 확인되지 않았습니다. logs를 확인하세요.')

    def external_resources(self):
        calls=[row for row in self.store.rows('SELECT repo,scope,role,process_pid,process_identity FROM agent_calls WHERE process_pid IS NOT NULL')
               if (row['repo'],row['scope']) not in self.active and still_running(row['process_pid'],row['process_identity'])]
        verifies=any((row['repo'],row['plan_id']) not in self.active and still_running(row['process_pid'],row['process_identity'])
                     for row in self.store.rows('SELECT repo,plan_id,process_pid,process_identity FROM exec_plan_runs WHERE process_pid IS NOT NULL'))
        return len(calls),{row['repo'] for row in calls if row['role']=='worker'},verifies

    def requests(self,repo):
        for row in self.store.rows('SELECT p.status,p.block_reason,r.* FROM exec_plans p JOIN exec_plan_runs r '
                                   'USING(repo,plan_id) WHERE repo=? AND r.request_kind IS NOT NULL',(repo,)):
            identity = row['plan_id']
            if (repo,identity) in self.active or self.live_process(repo,identity):
                continue
            if self.store.paused(repo):
                continue
            kind = row['request_kind']
            if kind=='manual_start':
                # CLI가 반환하지 못한 준비 결과만 기존 Git에서 재관측한다.
                plan = self.store.plan(repo,identity)
                snapshot=self.catalogs[repo]
                valid=(identity in snapshot.plans and not plan['draft'] and plan['source_path'] not in snapshot.errors
                       and all(self.store.plan(repo,key)['status']=='MERGED' for key in json.loads(plan['depends_on'])))
                if not valid:
                    with self.store.transaction() as db:
                        if self.store.finish_request_in(db,repo,identity,row['request_id'],'수동 준비 거부: 최신 확정 G·문서·선행 Merge 조건을 확인하세요.'):
                            db.execute('UPDATE exec_plan_runs SET manual_mode=NULL WHERE repo=? AND plan_id=?',(repo,identity))
                    continue
                key=hashlib.sha256((repo+'\0'+identity).encode()).hexdigest()[:24]
                try:
                    with process_lock(self.state_dir,name=f'manual-{key}.lock'):
                        def reserved(path):
                            with self.store.transaction() as db:
                                updated=db.execute("UPDATE exec_plan_runs SET worktree=? WHERE repo=? AND plan_id=? AND request_id=? "
                                           "AND request_kind='manual_start' AND request_result IS NULL",
                                           (str(path),repo,identity,row['request_id']))
                                if not updated.rowcount:
                                    raise StateError('수동 준비 예약이 종료됐습니다.')
                        path=self.repositories[repo].prepare(plan['branch'],saved_path=row['worktree'],
                                                            published=bool(plan['published'] or plan['pr_url']),on_reserved=reserved)
                        self.store.finish_request(repo,identity,row['request_id'],'수동 준비 복구 완료',
                                                  manual_mode='MANUAL_WORKING',worktree=str(path))
                except (OSError,RuntimeError) as error:
                    self.retry(repo,identity,error)
                continue
            if kind=='unblock':
                snapshot = self.catalogs[repo]
                if identity not in snapshot.plans or snapshot.plans[identity]['draft']:
                    self.store.finish_request(repo,identity,row['request_id'],'재개 거부: 확정된 비 draft G가 필요합니다.')
                    continue
                if snapshot.plans[identity]['document'].path in snapshot.errors:
                    self.store.finish_request(repo,identity,row['request_id'],'재개 거부: 문서 유효성 오류를 수정하세요.')
                    continue
                completed=self.store.rows("SELECT 1 FROM agent_calls WHERE repo=? AND scope=? AND role='worker' "
                                          "AND generation=? AND result IN ('SUCCESS','CONFIGURATION') LIMIT 1",
                                          (repo,identity,row['generation']))
                if completed and row['request_generation'] is not None and row['generation']>row['request_generation']:
                    if row['status']=='BLOCKED':
                        self.store.finish_request(repo,identity,row['request_id'],'재개 불가: '+str(row['block_reason']))
                        continue
                    if row['status'] in ('VERIFYING','REVIEWING','PR_OPEN','MERGED'):
                        self.store.finish_request(repo,identity,row['request_id'],'차단 재평가 후 재개가 확인됐습니다.')
                        continue
                if row['status']!='BLOCKED':
                    continue
                with self.store.transaction() as db:
                    updated = db.execute("UPDATE exec_plans SET status='READY',block_reason=NULL WHERE repo=? AND plan_id=? "
                                         "AND status='BLOCKED'",(repo,identity)).rowcount
                if not updated:
                    self.store.finish_request(repo,identity,row['request_id'],'재개 거부: 현재 상태가 변경됐습니다.')
            elif kind=='manual_complete':
                if row['manual_mode']=='SUBMITTED':
                    continue
                repository = self.repositories[repo]
                plan = self.store.plan(repo,identity)
                remote = repository.remote_head(plan['branch'])
                if not remote:
                    self.retry(repo,identity,'수동 제출 원격 브랜치를 확인할 수 없습니다.')
                    continue
                path = repository.prepare(plan['branch'],saved_path=plan['worktree'],published=True)
                repository.synchronize_head(path,plan['branch'])
                if repository.head(path)!=remote or repository.dirty(path):
                    self.retry(repo,identity,'수동 공간에 제출 원격과 다른 미게시 작업이 있어 보존합니다.')
                    continue
                with self.store.transaction() as db:
                    db.execute("UPDATE exec_plans SET status='VERIFYING' WHERE repo=? AND plan_id=? AND status NOT IN ('MERGED','CANCELLED')",
                               (repo,identity))
                    db.execute("UPDATE exec_plan_runs SET manual_mode='SUBMITTED',generation=generation+1,worktree=?,candidate_sha=?,"
                               'input_fingerprint=NULL,retry_after=NULL WHERE repo=? AND plan_id=? AND request_id=?',
                               (str(path),remote,repo,identity,row['request_id']))

    def candidate(self,repo,plan):
        self.workspace_definition(repo,plan)
        if self.repositories[repo].dirty(plan['worktree']):
            with self.store.transaction() as db:
                db.execute("UPDATE exec_plans SET status='WORKING' WHERE repo=? AND plan_id=? AND status IN ('VERIFYING','REVIEWING')",
                           (repo,plan['plan_id']))
            raise GitError('후보에 미커밋 변경이 있습니다. 작업물을 보존하고 같은 G에서 교정합니다.')
        specification = verify_command(plan['worktree'],self.settings.verify_timeout)
        hashes = [instructions(self.root,role)[1] for role in ('worker','reviewer')]
        requirements=[(key,item['document'].oid) for key,item in sorted(self.catalogs[repo].requirements.items())]
        requirements.extend((path,item.oid) for path,item in sorted(self.catalogs[repo].specifications.items()))
        value = fingerprint(self.repositories[repo],plan,specification,packed(hashes),requirements)
        return specification,value

    def workspace_definition(self,repo,plan):
        path=Path(plan['worktree'])/plan['source_path']
        if not path.is_file() or path.is_symlink():
            raise GitError('현재 작업 브랜치의 선택 G 문서를 확인할 수 없습니다.')
        item=parse_plan(Document(plan['source_path'],'workspace',path.read_text(encoding='utf-8')))
        if item['id']!=plan['plan_id'] or item['draft']:
            raise GitError('작업 브랜치 G의 ID·draft가 바뀌었습니다. 사용자 문서를 보존합니다.')
        for dependency in item['depends_on']:
            if self.store.plan(repo,dependency)['status']!='MERGED':
                raise GitError(f'작업 브랜치 G의 선행 Merge가 필요합니다: {dependency}')
        return item

    def latest_inputs(self,repo):
        repository=self.repositories[repo]
        with repository.observation():
            snapshot=repository.snapshot(repository.refresh())
            synchronize(self.store,repo,snapshot)
        return snapshot

    def prepare_worker(self,repo,plan):
        repository,github = self.repositories[repo],self.github[repo]
        prs = self.store.rows("SELECT * FROM managed_prs WHERE repo=? AND kind='implementation' AND state='OPEN'",(repo,))
        current_pr = next((row for row in prs if plan['plan_id'] in json.loads(row['plan_ids'])),None)
        if current_pr and not current_pr['draft']:
            github.set_draft(github.pull(current_pr['pr_number']),True)
        def reserved(path):
            with self.store.transaction() as db:
                db.execute('UPDATE exec_plan_runs SET worktree=? WHERE repo=? AND plan_id=?',(str(path),repo,plan['plan_id']))
        path = repository.prepare(plan['branch'],saved_path=plan['worktree'],published=bool(plan['published'] or plan['pr_url']),on_reserved=reserved)
        unmerged=bool(repository.git('diff','--name-only','--diff-filter=U',cwd=path))
        if repository.remote_head(plan['branch']) and not unmerged:
            try:
                repository.synchronize_head(path,plan['branch'])
            except MergeConflict:
                unmerged=True
        with self.store.transaction() as db:
            db.execute('UPDATE exec_plan_runs SET worktree=? WHERE repo=? AND plan_id=?',(str(path),repo,plan['plan_id']))
        if current_pr and current_pr['mergeability']=='CONFLICT' and not unmerged:
            if repository.dirty(path):
                repository.commit(path,plan['branch'],f'{plan["plan_id"]} 충돌 복구 전 작업 보존')
            repository.git('fetch','origin',repository.base,cwd=path)
            try:
                repository.merge(path,plan['branch'],f'origin/{repository.base}')
            except MergeConflict:
                pass
        self.workspace_definition(repo,{**plan,'worktree':str(path)})
        return path

    def worker(self,repo,plan,generation,input_fingerprint):
        repository = self.repositories[repo]
        snapshot=self.latest_inputs(repo)
        current=self.store.plan(repo,plan['plan_id'])
        if current['generation']!=generation or current['status']!='WORKING':
            return ('STALE',None,None)
        effective=self.workspace_definition(repo,plan)
        document = snapshot.plans[plan['plan_id']]['document']
        sources='\n\n'.join(f'[{key}] {item["document"].path}\n{item["document"].text}' for key,item in
                              sorted(snapshot.requirements.items(),key=lambda pair:int(pair[0][2:]),reverse=True))
        sources+='\n\n'+'\n\n'.join(item.text for item in snapshot.specifications.values())
        prompt = (f'선택 G: {plan["plan_id"]}\n현재 base: {snapshot.revision}\n'
                  '선택 G와 최신 관련 Requirement·현재 브랜치 사용자 변경을 보존하며 구현·관련 테스트를 완료하세요. '
                  '실제 충돌 규칙만 높은 R-ID가 우선하며 미확정 정책은 사용자 판단을 요청합니다. '
                  'Git Commit/Push/PR은 Manager가 담당합니다. '
                  '최종 응답은 result(CANDIDATE/CHECKPOINT/USER_DECISION/UNRESOLVABLE_CONFLICT), evidence, '
                  'tests(실제 targeted test 결과 또는 미실행 사유)의 JSON입니다. '
                  'CHECKPOINT는 Verify/Review 성공이 아닙니다.\n'
                  f'현재 base 계획:\n{document.text}\n현재 활성 Requirement 전문 (실제 충돌 규칙만 높은 ID 우선):\n{sources}\n'
                  f'이전 교정 근거:\n{plan.get("last_error") or "없음"}')
        result = self.agent.call(repository.project,plan['plan_id'],'worker',plan['worktree'],prompt,
                                 generation=generation,complexity=effective['complexity'],risk=effective['risk'])
        latest=self.latest_inputs(repo)
        current = self.store.plan(repo,plan['plan_id'])
        if (current['generation']!=generation or current['input_fingerprint']!=input_fingerprint
                or current['status']!='WORKING' or plan['source_path'] in latest.errors
                or any(source in latest.errors for source in json.loads(plan['sources'])) or plan['plan_id'] not in latest.plans):
            return ('STALE',None,None)
        if {key:item['document'].oid for key,item in snapshot.requirements.items()}!={key:item['document'].oid for key,item in latest.requirements.items()}:
            self.store.apply(repo,plan['plan_id'],generation,input_fingerprint,'WORKING','READY')
            return ('STALE',None,None)
        if result.kind!='SUCCESS':
            return (result.kind,result.message,None)
        outcome = result.decision
        if outcome['result'] in ('USER_DECISION','UNRESOLVABLE_CONFLICT'):
            if outcome['result']=='UNRESOLVABLE_CONFLICT' and not repository.git(
                    'diff','--name-only','--diff-filter=U',cwd=plan['worktree']):
                return ('TRANSIENT','확인된 Merge 충돌 근거가 없어 재관측합니다.',None)
            return ('BLOCKED',outcome['evidence'],None)
        if not isinstance(outcome.get('tests'),str) or not outcome['tests'].strip():
            return ('INVALID_OUTPUT','실제 관련 테스트 결과 또는 미실행 사유가 필요합니다.',None)
        sha = repository.commit(plan['worktree'],plan['branch'],f'{plan["plan_id"]}: {plan["title"]}')
        evidence = outcome['evidence']+'\n'+outcome['tests']
        with self.store.transaction() as db:
            saved = db.execute('UPDATE exec_plan_runs SET candidate_sha=?,worker_result=? WHERE repo=? AND plan_id=? '
                               'AND generation=? AND input_fingerprint=?',
                               (sha,packed({'result':outcome['result'],'evidence':evidence,'generation':generation}),
                                repo,plan['plan_id'],generation,input_fingerprint)).rowcount
            if not saved:
                return ('STALE',None,None)
        repository.publish(plan['worktree'],plan['branch'],published=bool(plan['published'] or plan['pr_url']))
        return (outcome['result'],evidence,sha)

    def resume_worker_publication(self,repo,plan):
        self.latest_inputs(repo)
        if self.store.plan(repo,plan['plan_id'])['generation']!=plan['generation']:
            return False
        result = json.loads(plan['worker_result'])
        if result['generation']!=plan['generation']:
            return False
        repository=self.repositories[repo]
        if not plan['worktree'] or repository.head(plan['worktree'])!=plan['candidate_sha']:
            with self.store.transaction() as db:
                db.execute('UPDATE exec_plan_runs SET worker_result=NULL WHERE repo=? AND plan_id=?',(repo,plan['plan_id']))
            return False
        repository.publish(plan['worktree'],plan['branch'],published=bool(plan['published'] or plan['pr_url']))
        updated=self.store.apply(repo,plan['plan_id'],plan['generation'],plan['input_fingerprint'],'WORKING',
                                 'VERIFYING' if result['result']=='CANDIDATE' else 'WORKING',candidate_sha=plan['candidate_sha'])
        if updated:
            with self.store.transaction() as db:
                db.execute('UPDATE exec_plan_runs SET worker_result=NULL WHERE repo=? AND plan_id=? AND generation=?',
                           (repo,plan['plan_id'],plan['generation']))
        return updated

    def verify(self,repo,plan,generation,specification):
        def started(pid,identity):
            with self.store.transaction() as db:
                db.execute('UPDATE exec_plan_runs SET process_pid=?,process_identity=? WHERE repo=? AND plan_id=? AND generation=?',
                           (pid,identity,repo,plan['plan_id'],generation))
        def stopped():
            with self.store.transaction() as db:
                db.execute('UPDATE exec_plan_runs SET process_pid=NULL,process_identity=NULL WHERE repo=? AND plan_id=? AND generation=?',
                           (repo,plan['plan_id'],generation))
        return verify(specification,on_started=started,on_stopped=stopped)

    def reviewer(self,repo,plan,generation,input_fingerprint):
        receipts = self.store.rows("SELECT evidence FROM validation_receipts WHERE repo=? AND plan_id=? AND stage='verify' AND success=1",
                                   (repo,plan['plan_id']))
        if not receipts:
            raise StateError('현재 후보의 Verify 성공 근거가 없습니다.')
        repository = self.repositories[repo]
        changed = repository.git('diff','--name-only',f'origin/{repository.base}...HEAD',cwd=plan['worktree'])
        diff = repository.git('diff',f'origin/{repository.base}...HEAD',cwd=plan['worktree'])
        prompt = (f'{plan["plan_id"]} 현재 후보 {plan["candidate_sha"]}를 독립 검토하세요. '
                  '변경·명세·관련 테스트·아래 실제 Verify 근거를 확인하고 전체 Verify를 반복하지 마세요. '
                  '파일을 수정하지 않습니다. 최종 응답은 result(PASS/FAIL/USER_DECISION), evidence의 JSON입니다.\n'
                  f'Verify 근거:\n{receipts[0]["evidence"]}\n변경 파일:\n{changed}\n'
                  f'현재 diff (길면 앞부분; 필요한 파일은 직접 읽으세요):\n{diff[:60000]}\n'
                  '현재 base의 활성 Requirement (브랜치 편집과 함께 대조):\n'+
                  '\n\n'.join(item['document'].text for _,item in sorted(self.catalogs[repo].requirements.items()))+
                  '\n\n'+'\n\n'.join(item.text for item in self.catalogs[repo].specifications.values()))
        return self.agent.call(self.repositories[repo].project,plan['plan_id'],'reviewer',plan['worktree'],prompt,
                               generation=generation)

    def publish(self,repo,plan,input_fingerprint):
        repository,github = self.repositories[repo],self.github[repo]
        self.catalogs[repo]=self.latest_inputs(repo)
        current_plan=self.store.plan(repo,plan['plan_id'])
        if current_plan['generation']!=plan['generation'] or current_plan['status']!='REVIEWING':
            raise GitError('게시 전 최신 확정 G가 바뀌었습니다.')
        if self.store.paused(repo):
            return False
        _,current = self.candidate(repo,plan)
        if current!=input_fingerprint:
            raise GitError('게시 전 후보·검증 입력이 바뀌었습니다.')
        repository.publish(plan['worktree'],plan['branch'],published=bool(plan['published'] or plan['pr_url']))
        primary = plan['primary_requirement_id']
        body = metadata('implementation',primary,[plan['plan_id']])+f'\n\n{plan["title"]}\n\n'
        for receipt in self.store.rows('SELECT stage,evidence FROM validation_receipts WHERE repo=? AND plan_id=? AND success=1',
                                       (repo,plan['plan_id'])):
            body += f'{receipt["stage"]}: {receipt["evidence"]}\n\n'
        planning = self.store.rows("SELECT url FROM managed_prs WHERE repo=? AND kind='planning' AND primary_requirement_id=? "
                                   'ORDER BY pr_number DESC LIMIT 1',(repo,primary))
        if planning:
            body += f'공유 Planner 계획 PR: {planning[0]["url"]}\n\n'
        body += usage_body(self.store,repo,plan['plan_id'])
        key = f'pr:{repo}:{plan["plan_id"]}:{plan["candidate_sha"]}'
        with self.store.transaction() as db:
            db.execute('INSERT INTO external_jobs(job_key,repo,kind,target,expected_input) VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING',
                       (key,repo,'implementation_pr',plan['plan_id'],plan['candidate_sha']))
        pull = github.ensure_pull(plan['branch'],repository.base,f'{plan["plan_id"]}: {plan["title"]}',body,draft=True)
        pull = github.pull(pull['number'])
        if pull['head']['sha']!=plan['candidate_sha']:
            raise GitError('구현 PR의 실제 후보가 달라졌습니다.')
        info = {'kind':'implementation','primary_requirement_id':primary,'plan_ids':[plan['plan_id']]}
        if pull.get('mergeable') is False:
            record_pull(self.store,repo,pull,info)
            with self.store.transaction() as db:
                db.execute("UPDATE exec_plans SET status='WORKING' WHERE repo=? AND plan_id=? AND status='REVIEWING'",(repo,plan['plan_id']))
            raise MergeConflict('구현 PR의 Merge 충돌을 복구해야 합니다.')
        if pull.get('mergeable') is None:
            raise GitError('구현 PR 충돌 여부를 아직 확인할 수 없습니다.')
        self.catalogs[repo]=self.latest_inputs(repo)
        current_plan=self.store.plan(repo,plan['plan_id'])
        if (current_plan['generation']!=plan['generation'] or current_plan['status']!='REVIEWING'
                or self.candidate(repo,current_plan)[1]!=input_fingerprint):
            raise GitError('Ready 전환 전 현재 후보·확정 명세가 바뀌었습니다.')
        if pull['draft']:
            github.set_draft(pull,False)
            pull = github.pull(pull['number'])
            if pull['head']['sha']!=plan['candidate_sha']:
                github.set_draft(pull,True)
                raise GitError('Ready 전환 중 원격 후보가 바뀌어 다시 Draft로 보존합니다.')
        record_pull(self.store,repo,pull,info)
        with self.store.transaction() as db:
            updated = db.execute("UPDATE exec_plans SET status='PR_OPEN',pr_url=? WHERE repo=? AND plan_id=? AND status='REVIEWING' "
                                 'AND EXISTS(SELECT 1 FROM exec_plan_runs WHERE repo=? AND plan_id=? AND generation=? AND input_fingerprint=?)',
                                 (pull['html_url'],repo,plan['plan_id'],repo,plan['plan_id'],plan['generation'],input_fingerprint)).rowcount
            if updated:
                message = f'구현 PR: {pull["html_url"]}\n검토하고 Merge하세요.'
                event = f'pr:{repo}:{pull["number"]}:created' if not plan['pr_url'] else f'pr:{repo}:{pull["number"]}:ready:{plan["candidate_sha"]}'
                if plan['request_kind']=='manual_complete':
                    self.store.finish_request_in(db,repo,plan['plan_id'],plan['request_id'],'수동 제출 완료. '+message)
                else:
                    self.store.notify(db,event,repo,plan['plan_id'],message)
                db.execute('DELETE FROM external_jobs WHERE job_key=?',(key,))
        return bool(updated)

    def finish(self,job,future):
        kind,repo,identity,generation,input_fingerprint = job
        if kind=='planner':
            future.result()
            return
        plan = self.store.plan(repo,identity)
        if plan['generation']!=generation or plan['input_fingerprint']!=input_fingerprint or plan['status'] in TERMINAL:
            return
        result = future.result()
        if kind in ('verify','reviewer') and self.candidate(repo,plan)[1]!=input_fingerprint:
            self.retry(repo,identity,'호출 후 후보가 바뀌었습니다. 검증을 다시 실행합니다.')
            return
        if kind=='worker':
            outcome,evidence,sha = result
            if outcome=='STALE':
                return
            if outcome in ('BLOCKED','CONFIGURATION'):
                self.store.apply(repo,identity,generation,input_fingerprint,'WORKING','BLOCKED',reason=evidence)
                if plan['request_kind']=='unblock':
                    self.store.finish_request(repo,identity,plan['request_id'],f'재개 불가: {evidence}')
                else:
                    with self.store.transaction() as db:
                        self.store.notify(db,f'blocked:{repo}:{identity}:{generation}',repo,identity,
                                          f'{identity} 사용자 조치 필요: {evidence}')
            elif outcome in ('CANDIDATE','CHECKPOINT'):
                updated=self.store.apply(repo,identity,generation,input_fingerprint,'WORKING',
                                         'VERIFYING' if outcome=='CANDIDATE' else 'WORKING',candidate_sha=sha)
                if updated:
                    with self.store.transaction() as db:
                        db.execute('UPDATE exec_plan_runs SET worker_result=NULL WHERE repo=? AND plan_id=? AND generation=?',
                                   (repo,identity,generation))
                if plan['request_kind']=='unblock':
                    self.store.finish_request(repo,identity,plan['request_id'],'차단 재평가 후 재개가 확인됐습니다.')
            else:
                self.retry(repo,identity,f'{outcome}: {evidence}')
        elif kind=='verify':
            passed,evidence = result
            self.store.apply(repo,identity,generation,input_fingerprint,'VERIFYING','REVIEWING' if passed else 'WORKING',
                             stage='verify',success=passed,evidence=evidence)
            if passed:
                self.completed_verifies.add((repo,identity,input_fingerprint))
            else:
                self.retry(repo,identity,evidence)
        elif kind=='reviewer':
            passed = result.kind=='SUCCESS' and result.decision['result']=='PASS'
            evidence = result.decision['evidence'] if result.decision else result.message
            blocked = result.kind=='CONFIGURATION' or result.kind=='SUCCESS' and result.decision['result']=='USER_DECISION'
            self.store.apply(repo,identity,generation,input_fingerprint,'REVIEWING',
                             'BLOCKED' if blocked else 'REVIEWING' if passed else 'WORKING',
                             stage='review',success=passed,evidence=evidence,reason=evidence if blocked else None)
            if passed:
                self.completed_reviews.add((repo,identity,input_fingerprint))
            elif blocked:
                if plan['request_kind']=='manual_complete':
                    self.store.finish_request(repo,identity,plan['request_id'],'수동 제출에 사용자 조치가 필요합니다: '+evidence)
                else:
                    with self.store.transaction() as db:
                        self.store.notify(db,f'blocked:{repo}:{identity}:{generation}',repo,identity,
                                          f'{identity} 사용자 조치 필요: {evidence}')
            elif not blocked:
                self.retry(repo,identity,evidence)

    def run(self,selected=None):
        projects = self.store.rows('SELECT * FROM projects WHERE enabled=1 ORDER BY repo')
        if selected:
            projects = [project for project in projects if project['repo'] in selected]
        errors = []
        for project in projects:
            try:
                self.observe(project)
            except (OSError,RuntimeError,ValueError) as error:
                errors.append(str(error))
                self.retry(project['repo'],'project',error)
        from contextlib import ExitStack
        with ExitStack() as resources:
            capacity=self.settings.max_ai
            pool=resources.enter_context(ThreadPoolExecutor(max_workers=capacity+1))
            futures = {}
            while True:
                self.settings_boundary()
                if self.settings.max_ai>capacity:
                    capacity=self.settings.max_ai
                    pool=resources.enter_context(ThreadPoolExecutor(max_workers=capacity+1))
                current_projects=self.store.rows('SELECT * FROM projects ORDER BY repo')
                if selected:
                    current_projects=[project for project in current_projects if project['repo'] in selected]
                for project in current_projects:
                    repo=project['repo']
                    if not project['enabled'] and project['request_kind']=='remove' and not any(job[1]==repo for job in futures.values()):
                        if not any(self.live_process(repo,row['plan_id']) for row in self.store.rows('SELECT plan_id FROM exec_plans WHERE repo=?',(repo,))):
                            self.store.finish_project_request(repo,project['request_id'],f'{repo} 등록 해제 완료. 작업물은 보존합니다.')
                    elif project['enabled']:
                        previous=self.repositories.get(repo)
                        changed=previous is None or any(previous.project.get(key)!=project.get(key) for key in ('remote_url','base_branch','provider','revision'))
                        if changed and not any(job[1]==repo for job in futures.values()):
                            self.repositories.pop(repo,None)
                            self.catalogs.pop(repo,None)
                            try:
                                self.observe(project)
                            except (OSError,RuntimeError,ValueError) as error:
                                self.retry(repo,'project',error)
                projects=[project for project in current_projects if project['enabled']]
                can_dispatch = self.boundary()
                for project in projects:
                    repo = project['repo']
                    if repo in self.catalogs:
                        try:
                            self.requests(repo)
                        except (OSError,RuntimeError,ValueError) as error:
                            self.retry(repo,'project',error)
                external_ai,external_workers,external_verify=self.external_resources()
                busy_ai = external_ai+sum(job[0] in ('planner','worker','reviewer') for job in futures.values())
                busy_verify = external_verify or any(job[0]=='verify' for job in futures.values())
                dispatched = False
                for project in projects:
                    repo = project['repo']
                    if not can_dispatch or repo not in self.catalogs or self.store.paused(repo):
                        continue
                    snapshot = self.catalogs[repo]
                    if busy_ai<self.settings.max_ai and not any(job[0]=='planner' and job[1]==repo for job in futures.values()):
                        for identity,reason in planning_targets(self.store,repo,snapshot):
                            key = (repo,identity,snapshot.requirements[identity]['document'].oid,reason)
                            if key in self.planned_inputs or (repo,identity) in self.active:
                                continue
                            self.planned_inputs.add(key)
                            if not self.boundary():
                                break
                            future = pool.submit(proposal,self.store,self.repositories[repo],self.github[repo],self.agent,snapshot,identity,reason)
                            job = ('planner',repo,identity,None,None)
                            futures[future]=job
                            self.active[(repo,identity)]=job
                            busy_ai+=1
                            dispatched=True
                            break
                    plans = self.store.rows('SELECT p.*,r.generation,r.input_fingerprint,r.worktree,r.candidate_sha,r.worker_result,r.manual_mode,'
                                            'r.retry_after,r.last_error,r.published,r.progress_at,r.request_id,r.request_kind FROM exec_plans p JOIN exec_plan_runs r '
                                            'USING(repo,plan_id) WHERE repo=? AND status NOT IN (?,?,?) '
                                            "ORDER BY CASE WHEN status!='READY' THEN 0 ELSE 1 END,"
                                            "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END,plan_id",
                                            (repo,'MERGED','CANCELLED','BLOCKED'))
                    latest=self.store.rows("SELECT scope FROM agent_calls WHERE repo=? AND role='worker' ORDER BY created_at DESC LIMIT 1",(repo,))
                    last=next((item for item in plans if latest and item['plan_id']==latest[0]['scope'] and item['status']=='WORKING'),None)
                    if last:
                        newcomer=next((item for item in plans if item['status']=='READY' and item['priority']==last['priority']
                                      and not item['draft'] and item['manual_mode'] is None),None)
                        if newcomer:
                            plans.remove(newcomer)
                            plans.insert(plans.index(last),newcomer)
                    for plan in plans:
                        identity = plan['plan_id']
                        if ((repo,identity) in self.active or identity not in snapshot.plans or plan['draft']
                                or plan['source_path'] in snapshot.errors or any(source in snapshot.errors for source in json.loads(plan['sources']))
                                or plan['manual_mode'] in ('PREPARING','MANUAL_WORKING')
                                or (plan['retry_after'] and plan['retry_after']>time.time()) or self.live_process(repo,identity)):
                            continue
                        try:
                            if not self.boundary():
                                break
                            status = plan['status']
                            if status=='PR_OPEN':
                                conflicts = self.store.rows("SELECT plan_ids FROM managed_prs WHERE repo=? AND state='OPEN' AND mergeability='CONFLICT'",(repo,))
                                if not any(identity in json.loads(row['plan_ids']) for row in conflicts):
                                    continue
                                with self.store.transaction() as db:
                                    db.execute("UPDATE exec_plans SET status='WORKING' WHERE repo=? AND plan_id=? AND status='PR_OPEN'",(repo,identity))
                                status = 'WORKING'
                            if status in ('READY','WORKING'):
                                if status=='WORKING' and plan['worker_result']:
                                    if self.resume_worker_publication(repo,plan):
                                        dispatched=True
                                        continue
                                project_worker = repo in external_workers or any(job[0]=='worker' and job[1]==repo for job in futures.values())
                                if busy_ai>=self.settings.max_ai or project_worker:
                                    continue
                                value = hashlib.sha256(packed([snapshot.revision,plan['source_oid'],plan['sources']]).encode()).hexdigest()
                                generation = self.store.claim(repo,identity,status,'WORKING',value)
                                if generation is None:
                                    continue
                                path = self.prepare_worker(repo,plan)
                                plan['worktree']=str(path)
                                future = pool.submit(self.worker,repo,plan,generation,value)
                                kind='worker'
                                busy_ai+=1
                            else:
                                actual=self.repositories[repo].head(plan['worktree'])
                                if actual!=plan['candidate_sha']:
                                    with self.store.transaction() as db:
                                        db.execute("UPDATE exec_plans SET status='VERIFYING' WHERE repo=? AND plan_id=?",(repo,identity))
                                        db.execute('UPDATE exec_plan_runs SET candidate_sha=? WHERE repo=? AND plan_id=?',(actual,repo,identity))
                                    plan['candidate_sha']=actual
                                    status='VERIFYING'
                                specification,value = self.candidate(repo,plan)
                                receipts = {row['stage']:row for row in self.store.rows('SELECT * FROM validation_receipts WHERE repo=? AND plan_id=?',(repo,identity))}
                                if status=='REVIEWING':
                                    evidence = receipts.get('verify')
                                    valid = evidence and evidence['success'] and evidence['input_fingerprint']==value
                                    if not valid or (not specification.reusable and (repo,identity,value) not in self.completed_verifies):
                                        with self.store.transaction() as db:
                                            db.execute("UPDATE exec_plans SET status='VERIFYING' WHERE repo=? AND plan_id=?",(repo,identity))
                                        status='VERIFYING'
                                if status=='VERIFYING':
                                    if busy_verify:
                                        continue
                                    generation = self.store.claim(repo,identity,status,status,value)
                                    if generation is None:
                                        continue
                                    future = pool.submit(self.verify,repo,plan,generation,specification)
                                    kind='verify'
                                    busy_verify=True
                                else:
                                    evidence = receipts.get('review')
                                    if (evidence and evidence['success'] and evidence['input_fingerprint']==value
                                            and (specification.reusable or (repo,identity,value) in self.completed_reviews)):
                                        self.publish(repo,plan,value)
                                        dispatched=True
                                        continue
                                    if busy_ai>=self.settings.max_ai:
                                        continue
                                    generation = self.store.claim(repo,identity,status,status,value)
                                    if generation is None:
                                        continue
                                    future=pool.submit(self.reviewer,repo,plan,generation,value)
                                    kind='reviewer'
                                    busy_ai+=1
                            job=(kind,repo,identity,generation,value)
                            futures[future]=job
                            self.active[(repo,identity)]=job
                            dispatched=True
                        except (OSError,RuntimeError,ValueError) as error:
                            self.retry(repo,identity,error)
                deliver(self.store,self.settings)
                if not futures:
                    if dispatched:
                        continue
                    break
                completed,_=wait(futures,return_when=FIRST_COMPLETED)
                for future in completed:
                    job=futures.pop(future)
                    _,repo,identity,_,_=job
                    self.active.pop((repo,identity),None)
                    try:
                        current = self.store.rows('SELECT * FROM projects WHERE repo=?',(repo,))[0]
                        self.observe(current)
                        self.finish(job,future)
                    except VerifyTerminationUncertain:
                        raise
                    except (OSError,RuntimeError,ValueError) as error:
                        errors.append(str(error))
                        self.retry(repo,identity,error)
        self.stalled()
        deliver(self.store,self.settings)
        if self.handoff_target is None:
            for repo,snapshot in self.catalogs.items():
                if any(project['repo']==repo for project in projects):
                    errors.extend(complete(self.store,self.repositories[repo],self.github[repo],snapshot))
        return errors
