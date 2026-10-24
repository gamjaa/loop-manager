"""Manual command policy and restartable handoff to the ordinary execution loop."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
import json
import re
from contextlib import closing, nullcontext
from .manual_errors import ManualWorkError
from .manual_concurrency import (ManualConflictError, manual_preparation, preparation_gate,
                                 group_resources, serial_manual_drive)
from loop_manager_internal.runtime.resource_lock import resource_locks
from loop_manager_internal.runtime.contracts import (ProcessLockBusyError, WorkspaceOwnershipError,
                                                     PublicationSafetyError, GitLockError)
from loop_manager_internal.storage import work_claims
from loop_manager_internal.config.managed_config import fast_mode_scope
from loop_manager_internal.runtime import operation_events as events

from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.git import manager_git_journal, workspace
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage import execplan_catalog_state, runtime_state, maintenance_state
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository, digest, live_requirement_paths


class ManualDependencyWait(ManualWorkError):
    pass


@dataclass(frozen=True)
class ManualWorkEffects:
    connect_database: Callable
    validate_repository: Callable
    fetch_base: Callable
    git: Callable
    git_bytes: Callable
    make_worktree: Callable
    validate_workspace: Callable
    capture_snapshot: Callable
    pr_for_branch: Callable
    generate_plans: Callable
    run_project: Callable
    operation_print: Callable
    resolve_profile: Callable


@dataclass(frozen=True)
class ManualProjectDriverEffects:
    load_managed_config: Callable
    run_project_result: Callable


def run_manual_project(effects: ManualProjectDriverEffects, connection, state_dir, repo,
                       config_path, base, allow_mismatch, **kwargs):
    config=effects.load_managed_config(config_path)
    from loop_manager_internal.verification.coordination import VerifyCoordinator, execution_settings
    with fast_mode_scope(config), VerifyCoordinator() as coordinator, execution_settings(
            coordinator, config.verify_parallelism, Path(state_dir)):
        return effects.run_project_result(connection,state_dir,repo,config_path,base,allow_mismatch,
            verify_timeout_seconds=config.verify_timeout_seconds,
            refactoring_review_interval=config.refactoring_review_interval,**kwargs)


def validate_options(args):
    skipping = args.skip_verify or args.skip_review
    if args.expected_head and not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', args.expected_head):
        raise ValueError('--expected-head는 commit 전체 SHA여야 합니다.')
    if skipping and not args.expected_head:
        raise ValueError('생략에는 --expected-head가 필요합니다.')
    if args.skip_reason is not None and not skipping:
        raise ValueError('--skip-reason은 생략 옵션과 함께 사용해야 합니다.')


def normalize_skip_reason(reason):
    return reason if isinstance(reason, str) and reason.strip() else ''


def validate_group_dependencies(documents):
    ids = {item['plan']['id'] for item in documents}
    for item in documents:
        internal = ids.intersection(item['plan']['depends_on'])
        if internal:
            raise ManualWorkError(f"{item['plan']['id']} depends_on {', '.join(sorted(internal))}: "
                '묶음 내부 선행 Merge는 지원하지 않습니다. 별도 branch/PR로 순차 제출하세요.')


def validate_dependency_scope(connection, repo, documents):
    """Validate the reachable DAG without holding unrelated invalid plans."""
    declared={item['plan']['id']:item['plan'] for item in documents}
    graph={}
    pending=list(declared)
    while pending:
        key=pending.pop()
        if key in graph:
            continue
        plan=declared.get(key)
        if plan is None:
            row=connection.execute('SELECT depends_on FROM exec_plans WHERE repo=? AND plan_id=?',(repo,key)).fetchone()
            if row is None:
                raise ManualWorkError(f'등록되지 않은 dependency ID입니다: {key}')
            plan={'depends_on':json.loads(row[0])}
        graph[key]=plan
        pending.extend(plan['depends_on'])
    execplan_catalog.validate_dependency_graph(graph)


def confirm_dependencies(connection, effects, repo_path, documents, base):
    validate_group_dependencies(documents)
    validate_dependency_scope(connection,str(repo_path),documents)
    dependencies = sorted({key for item in documents for key in item['plan']['depends_on']})
    errors = []
    for key in dependencies:
        row = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                 (str(repo_path), key)).fetchone()
        if row is None or row['status'] != 'MERGED' or not row['branch'] or not row['pr_url']:
            errors.append(f"{key}: {row['status'] if row else 'UNKNOWN'}; 실제 PR Merge 근거 없음")
            continue
        pr = effects.pr_for_branch(repo_path, row['branch'], row['pr_url'])
        if not pr or pr.get('url') != row['pr_url'] or pr.get('headRefName') != row['branch'] \
                or not row['pr_head_oid'] or pr.get('headRefOid')!=row['pr_head_oid'] \
                or pr.get('baseRefName') != base or not (pr.get('state') == 'MERGED' or pr.get('mergedAt')):
            errors.append(f'{key}: MERGED; 등록 origin/{base}의 동일 PR Merge를 확인할 수 없음')
    if errors:
        raise ManualDependencyWait('미충족 dependency: ' + '; '.join(errors))


def candidate_input(effects, repo_path, head, base, documents, *, connection,
                    config_path, provider, worktree):
    # The candidate token covers repository, environment and pinned guidance;
    # stage tokens separately cover effective external operating settings.
    from loop_manager_internal.execution.manual_candidate import candidate_token, stage_input_tokens
    profile_worktree = Path(worktree) if worktree else Path(repo_path)
    verify_token,review_token=stage_input_tokens(
        config_path,provider,profile_worktree,effects.resolve_profile)
    return (candidate_token(connection,repo_path,head,base,documents),verify_token,review_token)


class ManualWorkService:
    def __init__(self, effects, connection, state_dir, repo_path, config_path, *, runtime_mutation=None):
        self.effects, self.connection = effects, connection
        self.state_dir, self.repo_path = state_dir, repo_path
        self.repo, self.config_path = str(repo_path), config_path
        self.runtime_mutation = runtime_mutation
        self.store = ManualWorkRepository(connection)
        self.runtime = runtime_state.RuntimeStateRepository(connection)
        self.project = connection.execute('SELECT * FROM projects WHERE repo=? AND registered=1', (self.repo,)).fetchone()
        if self.project is None:
            raise ManualWorkError('등록된 저장소와 기존 운영 SQLite가 필요합니다.')
        self.base = str(self.project['base_branch'])

    def validate_repository(self):
        self.effects.validate_repository(self.repo_path, allow_agents_mismatch=False, provider=self.project['provider'],
            config_path=self.config_path, registration_checks=False, expected_origin=self.project['origin'],
            state_dir=self.state_dir)

    def slot_available(self, *, recovering=None, releasing_plan=None):
        row = self.connection.execute('SELECT * FROM projects WHERE repo=?', (self.repo,)).fetchone()
        cycle = maintenance_state.MaintenanceRepository(self.connection).get(self.repo)
        def recoverable(intent):
            return bool(recovering and intent['owner']==f"plan:{recovering['owner_id']}" and
                ((intent['kind']=='managed_worktree_create' and intent['target']==recovering['worktree'])
                 or (intent['kind']=='manual_branch_push' and intent['target']==f"refs/heads/{recovering['branch']}")))
        own_block = bool(releasing_plan is not None and releasing_plan['status']=='PLAN_BLOCKED'
                         and row['plan_path']==releasing_plan['path'] and row['phase'] in {'BLOCKED','PLAN_BLOCKED'})
        from loop_manager_internal.application.task_scheduling import current_scheduler, managed_count
        scheduler = current_scheduler.get()
        if scheduler is not None:
            from loop_manager_internal.runtime.project_registry import task_limit
            _, limit, _ = task_limit(self.connection, self.repo, scheduler.config.max_tasks_per_project)
            return not (managed_count(self.connection, self.repo) >= limit
                or runtime_state.planning_owns_execution_slot(row)
                or scoped_blocking.project_block(self.connection, self.repo)
                or cycle.phase is not maintenance_state.MaintenancePhase.IDLE
                or manager_git_journal.has_pending_project_mutation(self.state_dir, self.repo, recoverable=recoverable))
        return not (row['phase'] and not own_block or self.runtime.active_row(self.repo) or row['agent_snapshot']
            or runtime_state.planning_owns_execution_slot(row)
            or scoped_blocking.project_block(self.connection, self.repo)
            or cycle.phase is not maintenance_state.MaintenancePhase.IDLE
            or manager_git_journal.has_pending_project_mutation(self.state_dir, self.repo,recoverable=recoverable))

    def fetch(self, branch):
        if branch.startswith('-') or self.effects.git(self.repo_path, 'check-ref-format', f'refs/heads/{branch}', check=False).returncode:
            raise ManualWorkError('유효하지 않은 원격 branch입니다.')
        observed = manager_git_journal.live_remote_branch_oid(self.effects.git, self.repo_path, branch)
        if observed is None:
            raise ManualWorkError(f'원격 branch가 없습니다: {branch}')
        # Use the existing fetch journal; no user source branch is mutated.
        self.effects.fetch_base(self.repo_path, self.state_dir, branch, owner='project')
        fetched = self.effects.git(self.repo_path, 'rev-parse', '--verify',
                                 f'refs/remotes/origin/{branch}^{{commit}}').stdout.strip()
        if fetched != observed:
            raise ManualWorkError('원격 HEAD가 관측 중 변경됐습니다. 다시 실행하세요.')
        return fetched

    def document(self, path, head):
        entry = self.effects.git(self.repo_path, 'ls-tree', head, '--', path).stdout
        if not entry.startswith(('100644 blob ', '100755 blob ')):
            raise ManualWorkError(f'원격 commit의 일반 문서가 없습니다: {path}')
        return self.effects.git_bytes(self.repo_path, 'show', f'{head}:{path}').decode('utf-8')

    @manual_preparation
    def start(self, plan_id):
        group = self.store.for_plan(self.repo, plan_id)
        row = self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (self.repo, plan_id)).fetchone()
        if row is None:
            raise ManualWorkError(f'G 계획이 없습니다: {plan_id}')
        from loop_manager_internal.storage.execplan_draft_state import pending_ids
        if plan_id in pending_ids(self.connection,self.repo):
            raise ManualWorkError(f'{plan_id}: draft의 사용자 판단 대기를 수동 시작으로 해제할 수 없습니다.')
        if group and (group['kind'] != 'start' or group['state'] not in {'STARTING','MANUAL_WORKING'}):
            raise ManualWorkError('제출 묶음 구성원 또는 이미 자동 처리 중인 계획은 개별 start할 수 없습니다.')
        if group and group['state'] == 'MANUAL_WORKING':
            self.display(group, accepted=False)
            return 0
        if row['status'] not in {'READY','PLAN_BLOCKED','MANUAL_WORKING'}:
            raise ManualWorkError(f"{plan_id}: {row['status']}에서 수동 시작할 수 없습니다.")
        if plan_id in scoped_blocking.catalog_held_plan_ids(self.connection, self.repo):
            raise ManualWorkError('catalog hold 상태입니다.')
        if row['status'] == 'PLAN_BLOCKED' and row['blocked_reason'] not in {
                'verify_retry_exhausted','review_retry_exhausted','review_protocol_exhausted','worker_output_invalid'}:
            raise ManualWorkError(f"수동 인계할 수 없는 차단: {row['blocked_reason']}")
        preparation_gate(self, plan_ids=[plan_id], group=group)
        base_head = self.fetch(self.base)
        text = self.document(str(row['path']), base_head)
        validated_plan=execplan_catalog.parse_execplan_text(str(row['path']),text,validate_sources=False)
        if validated_plan.get('draft'):
            raise ManualWorkError(f'{plan_id}: draft의 사용자 판단을 먼저 확정해야 합니다.')
        plan = validated_plan
        documents = [{'path':str(row['path']), 'text':text, 'plan':plan}]
        confirm_dependencies(self.connection, self.effects, self.repo_path, documents, self.base)
        branch, worktree = workspace.expected_execplan_workspace(self.state_dir, self.repo, plan_id)
        if row['branch'] and row['branch'] != branch or row['worktree'] and Path(row['worktree']) != worktree:
            raise ManualWorkError('저장된 branch·worktree 소유 identity가 다릅니다.')
        owned = int(row['workspace_owned']) == 1
        if owned and worktree.is_dir():
            self.effects.validate_workspace(self.repo_path,worktree,branch)
            if self.effects.capture_snapshot(worktree).operations:
                raise ManualWorkError('소유 worktree의 미완료 Git operation을 보존합니다. 수동 인계하지 않습니다.')
        if row['pr_url']:
            pr=self.effects.pr_for_branch(self.repo_path,branch,row['pr_url'])
            if not pr or pr.get('url')!=row['pr_url'] or pr.get('headRefName')!=branch \
                    or pr.get('baseRefName')!=self.base or pr.get('state')!='OPEN':
                raise ManualWorkError('기존 연결 PR의 소유 identity와 OPEN 상태를 확인할 수 없습니다.')
        if not owned and group is None:
            if worktree.exists() or self.effects.git(self.repo_path,'show-ref','--verify','--quiet',
                    f'refs/heads/{branch}',check=False).returncode == 0 \
                    or manager_git_journal.live_remote_branch_oid(self.effects.git,self.repo_path,branch) is not None:
                raise ManualWorkError('동일 이름의 기존 branch/worktree에 SQLite 소유 근거가 없습니다.')
        start_base=(row['active_sha'] or base_head) if owned or group is not None else base_head
        with runtime_state._transaction(self.connection):
            preparation_gate(self, plan_ids=[plan_id], group=group, inspect_workspace=False)
            current = self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                             (self.repo, plan_id)).fetchone()
            if dict(current) != dict(row):
                raise ManualWorkError(f'{plan_id}: 준비 중 계획·실행 소유권이 변경됐습니다. 다시 실행하세요.')
            collision = self.connection.execute('SELECT plan_id FROM exec_plans WHERE repo=? AND plan_id!=? '
                'AND (branch=? OR worktree=?)', (self.repo, plan_id, branch, str(worktree))).fetchone()
            if collision:
                raise ManualWorkError(f"자원 소유자 {collision['plan_id']}와 branch·worktree 충돌: {branch}; {worktree}")
            if group is None:
                group = self.store.create(self.repo,'start',branch,(),base_head,base_head,
                                          members=(plan_id,),documents=documents)
                group = self.store.update(group,owner_id=plan_id,active_id=plan_id,
                                          branch=branch,worktree=str(worktree))
            self.runtime.update_plan(self.repo,plan_id,plan_values={
                'status':'MANUAL_WORKING','ever_started':1,'branch':branch,'worktree':str(worktree),
                'workspace_owned':1 if owned else 2,'active_sha':start_base},enqueue=False)
            if row['status']=='PLAN_BLOCKED':
                scoped_blocking.clear_plan_block(self.connection,self.repo,plan_id)
                projection=self.connection.execute('SELECT plan_path FROM projects WHERE repo=?',(self.repo,)).fetchone()
                if projection['plan_path']==row['path']:
                    self.runtime.clear(self.repo)
        if owned and worktree.is_dir() and row['blocked_reason'] in {'verify_retry_exhausted','review_retry_exhausted'}:
            stage='verify' if row['blocked_reason']=='verify_retry_exhausted' else 'review'
            known_head=self.effects.git(worktree,'rev-parse','HEAD').stdout.strip()
            from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
            checkpoint=ExecutionCheckpointRepository(self.connection).get(self.repo,plan_id)
            if checkpoint.expected_head==known_head and row[f'{stage}_result']:
                self.store.record(group,stage,known_head,base_head,'prior_failure','FAIL',row[f'{stage}_result'])
        self.prepare(group)
        group = self.store.get(self.repo,group['group_id'])
        with self.connection:
            self.store.update(group,state='MANUAL_WORKING',active_id=None,last_error=None)
        self.display(self.store.get(self.repo,group['group_id']),accepted=False)
        return 0

    def prepare(self, group):
        documents = json.loads(group['documents'])
        if group['kind']=='submit':
            for item in documents:
                existing=self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                    (self.repo,item['plan']['id'])).fetchone()
                if existing is not None and (existing['branch']!=group['branch'] or existing['worktree']!=group['worktree']):
                    raise ManualConflictError(f"예약된 수동 G ID에 기존 계획 충돌이 있습니다: {item['plan']['id']}")
        confirm_dependencies(self.connection,self.effects,self.repo_path,documents,self.base)
        owner = group['owner_id']
        row = self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (self.repo,owner)).fetchone()
        worktree, branch = Path(group['worktree']), group['branch']
        reserved = not group['workspace_created'] and (row is None or int(row['workspace_owned']) == 2)
        start_head = group['source_head'] if group['kind'] == 'submit' else str(row['active_sha'])
        preparation_gate(self, plan_ids=json.loads(group['members']), group=group)
        self.effects.make_worktree(self.repo_path,worktree,branch,self.base,allow_create_branch=False,
            allow_recovery=True,allow_existing=True,ownership_reserved=reserved,expected_head=start_head,
            journal_state_dir=self.state_dir,journal_owner=f'plan:{owner}')
        with self.connection:
            group = self.store.update(group,workspace_created=1)
            if row:
                self.runtime.update_plan(self.repo,owner,plan_values={'workspace_owned':1},enqueue=False)
        if group['kind'] == 'submit' and not group['prepared_head']:
            self.commit_documents(group, worktree)
            group = self.store.get(self.repo,group['group_id'])
        head = self.effects.git(worktree,'rev-parse','HEAD').stdout.strip()
        remote = manager_git_journal.live_remote_branch_oid(self.effects.git,self.repo_path,branch)
        manager_git_journal.recover_active(self.state_dir,repo=self.repo,kind='manual_branch_push',
            target=f'refs/heads/{branch}',
            observe=lambda:{'oid':manager_git_journal.live_remote_branch_oid(self.effects.git,self.repo_path,branch)},
            marker=lambda _intent:True)
        if remote is None:
            manager_git_journal.execute(self.state_dir,owner=f'plan:{owner}',kind='manual_branch_push',
                repo=self.repo,target=f'refs/heads/{branch}',before={'oid':None},expected={'oid':head},
                observe=lambda:{'oid':manager_git_journal.live_remote_branch_oid(self.effects.git,self.repo_path,branch)},
                apply=lambda _token:self.effects.git(worktree,'push',
                    manager_git_journal.exact_branch_lease(branch,None),'origin',f'{head}:refs/heads/{branch}'),
                marker=lambda _intent:True)
            remote = head
        elif (group['state']=='REGISTERING' or not group['prepared_head']) and remote != head:
            # A successful push interrupted before SQLite confirmation is
            # recoverable only from the saved intent. Other commits are kept.
            if not row or not row['publication_remote_oid']:
                raise ManualConflictError(f'원격 게시 대상의 소유 checkpoint와 충돌: {branch}; 원격={remote}; 예상={head}. 변경을 보존했습니다.')
        with self.connection:
            if group['kind'] == 'submit':
                for item in documents:
                    member = item['plan']['id']
                    if not self.connection.execute('SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=?', (self.repo,member)).fetchone():
                        execplan_catalog_state.insert_published_plan_row(self.connection,self.repo,item['path'],
                            item['plan'],execplan_catalog.plan_content_hash(item['text']),head)
                    self.runtime.update_plan(self.repo,member,plan_values={
                        'status':'MANUAL_PENDING','ever_started':1,'branch':branch,'worktree':str(worktree),
                        'workspace_owned':1,'publication_remote_oid':remote,
                        'active_sha':group['base_sha']},enqueue=False)
            else:
                self.runtime.update_plan(self.repo,owner,plan_values={'workspace_owned':1,
                    'publication_remote_oid':remote},enqueue=False)
            self.store.update(group,prepared_head=head,definition_head=head,
                state='AUTOMATIC' if group['kind']=='submit' else 'MANUAL_WORKING',last_error=None)

    def commit_documents(self, group, worktree):
        docs = json.loads(group['documents'])
        head = self.effects.git(worktree,'rev-parse','HEAD').stdout.strip()
        if head != group['source_head']:
            parent = self.effects.git(worktree,'rev-parse','HEAD^').stdout.strip()
            paths = set(self.effects.git(worktree,'diff','--name-only',parent,head).stdout.splitlines())
            if parent != group['source_head'] or paths != {item['path'] for item in docs}:
                raise ManualWorkError('생성 G commit의 복구 identity가 다릅니다.')
            for item in docs:
                if self.document(item['path'],head) != item['text']:
                    raise ManualWorkError('생성 G commit 내용이 예약된 초안과 다릅니다.')
        else:
            changes = self.effects.git(worktree,'status','--porcelain','-z').stdout.split('\0')
            expected = {item['path'] for item in docs}
            if any(record and record[3:] not in expected for record in changes):
                raise ManualWorkError('G 등록 workspace에 예상 밖 사용자 변경이 있습니다. 보존했습니다.')
            for item in docs:
                target = worktree/item['path']
                target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists() and target.read_text(encoding='utf-8') != item['text']:
                    raise ManualWorkError('생성 G 경로에 다른 사용자 내용이 있습니다.')
                target.write_text(item['text'],encoding='utf-8')
            self.effects.git(worktree,'add','--',*[item['path'] for item in docs])
            self.effects.git(worktree,'commit','-m',f"{group['owner_id']}: Track manual submission")
            head = self.effects.git(worktree,'rev-parse','HEAD').stdout.strip()
        with self.connection:
            self.store.update(group,prepared_head=head)

    @manual_preparation
    def complete(self, args):
        group = self.store.for_plan(self.repo,args.plan_id)
        row = self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (self.repo,args.plan_id)).fetchone()
        if row is None or group is None:
            raise ManualWorkError('수동 시작 또는 submit으로 등록된 G만 완료 요청할 수 있습니다.')
        if row['status'] == 'MERGED':
            self.display(group,accepted=True)
            return 0
        head = self.fetch(group['branch'])
        if args.expected_head and args.expected_head != head:
            raise ManualWorkError(f'수락 전 HEAD 불일치: expected {args.expected_head}, actual {head}')
        if row['pr_url']:
            pr=self.effects.pr_for_branch(self.repo_path,group['branch'],row['pr_url'])
            if not pr or pr.get('url')!=row['pr_url'] or pr.get('headRefName')!=group['branch'] \
                    or pr.get('baseRefName')!=self.base:
                raise ManualWorkError('기존 연결 PR의 원격 identity를 확인할 수 없습니다.')
            if (pr.get('state')=='MERGED' or pr.get('mergedAt')) and head!=row['pr_head_oid']:
                raise ManualWorkError('실제 Merge된 PR의 branch를 새 후보로 재사용할 수 없습니다.')
            if pr.get('state')=='CLOSED':
                raise ManualWorkError('기존 PR을 다시 열어 기존 복구 gate를 통과하세요.')
        if row['status']=='PR_OPEN' and head == row['pr_head_oid']:
            self.display(group,accepted=True)
            return 0
        if group['state']=='STARTING':
            raise ManualWorkError('수동 시작의 원격 준비가 끝나지 않았습니다. start를 다시 실행하세요.')
        if group['state']=='CLOSED':
            raise ManualWorkError('기존 PR이 닫혀 있습니다. 같은 PR을 다시 열어 기존 복구 gate를 통과하세요.')
        base_head = self.fetch(self.base)
        from loop_manager_internal.execution.manual_candidate import ManualCandidate
        candidate_documents=ManualCandidate(self.connection,self.repo,args.plan_id,self.effects.git,self.effects.git_bytes).documents(self.repo_path,head)
        from loop_manager_internal.catalog.manual_catalog import updated_base_documents
        documents=updated_base_documents(self.effects.git,self.repo,group,base_head,candidate_documents,candidate_head=head)
        definition_head=head if documents==candidate_documents else base_head
        previous_request=self.store.request(group)
        repaired=bool(previous_request and previous_request['source_head']!=head)
        candidate,verify_input,review_input=candidate_input(
            self.effects,self.repo_path,head,base_head,documents,connection=self.connection,
            config_path=self.config_path,provider=self.project['provider'],worktree=group['worktree'])
        from loop_manager_internal.execution.manual_candidate import candidate_token
        with runtime_state._transaction(self.connection):
            if (dict(self.store.get(self.repo, group['group_id'])) != dict(group)
                    or candidate_token(self.connection,self.repo_path,head,base_head,documents) != candidate):
                raise ManualConflictError('수동 완료 관측 뒤 요청·지침 입력이 변경됐습니다.')
            preparation_gate(self, plan_ids=json.loads(group['members']), group=group, inspect_workspace=False)
            group=self.store.update(group,documents=json.dumps(documents,ensure_ascii=False),definition_head=definition_head)
            self.store.accept(group,head=head,base=base_head,input_token=candidate,
                verify_input_token=verify_input,review_input_token=review_input,
                skip_verify=args.skip_verify,skip_review=args.skip_review,
                reason=normalize_skip_reason(args.skip_reason))
            self.store.update(group,state='AUTOMATIC',base_sha=base_head,last_error=None)
            for member in json.loads(group['members']):
                current = self.runtime.get(self.repo,member)
                if current.status=='MANUAL_WORKING':
                    self.runtime.update_plan(self.repo,member,plan_values={'status':'MANUAL_PENDING'},enqueue=False)
                elif current.status=='PLAN_BLOCKED' and repaired:
                    blocked=self.connection.execute('SELECT blocked_reason FROM exec_plans WHERE repo=? AND plan_id=?',
                                                    (self.repo,member)).fetchone()[0]
                    if blocked in {'verify_retry_exhausted','review_retry_exhausted','review_protocol_exhausted','worker_output_invalid'}:
                        scoped_blocking.clear_plan_block(self.connection,self.repo,member)
                        self.runtime.update_plan(self.repo,member,plan_values={'status':'MANUAL_PENDING',
                            'last_error':None,'verify_result':None,'review_result':None,'worker_result':None,
                            'verify_token':None,'review_token':None,'publication_token':None,'worker_input_token':None},enqueue=False)
                        self.store.update(group,resume_phase='VERIFYING')
        return group['group_id']

    def drive(self, group_id):
        with serial_manual_drive(self.state_dir) as available:
            if available and self.slot_available():
                self.effects.run_project(self.connection,self.state_dir,self.repo,self.config_path,
                    self.base,False,registered_only=True,runtime_mutation=self.runtime_mutation)
        group = self.store.get(self.repo,group_id)
        self.display(group,accepted=True)
        return 0 if group['state'] in {'PR_OPEN','MERGED'} else 1

    def resume(self):
        from loop_manager_internal.recovery.manual_work_recovery import rotate_correction
        rotate_correction(self.connection,self.repo)
        pending=self.store.pending(self.repo)
        recovering=next((group for group in pending if group['state']=='REGISTERING'
                         or group['source_update_candidate']),None)
        if not self.slot_available(recovering=recovering):
            return
        for group in pending:
            from loop_manager_internal.application.task_scheduling import current_scheduler
            owner = group['active_id'] or group['owner_id']
            if current_scheduler.get() is not None and owner:
                current = self.runtime.get(self.repo, owner)
                if current.status in runtime_state.ACTIVE_STATUSES:
                    continue
            try:
                with resource_locks(self.state_dir, self.repo, group_resources(group), owner='manual-resume'):
                    self._resume_group(group)
                    if self.runtime.active_row(self.repo) is not None:
                        return
            except (ProcessLockBusyError, runtime_state.TaskLimitReached):
                continue
            except (RuntimeError, OSError, ValueError, TypeError, KeyError) as error:
                with self.connection:
                    self.store.update(group,last_error=str(error),
                        attempts=group['attempts']+(0 if isinstance(error,ManualDependencyWait) else 1))
                self.effects.operation_print(f"수락됨·보존됨: {group['group_id']}; {error}")

    def _resume_group(self, group):
        group = self.store.get(self.repo, group['group_id'])
        if group['state'] not in {'GENERATING','REGISTERING','AUTOMATIC'} or not group['request_id']:
            return
        if group['state'] in {'GENERATING','REGISTERING'} and group['attempts']>=3:
            return
        if group['state']=='GENERATING':
            self.generate(group)
            group = self.store.get(self.repo,group['group_id'])
        if group['state']=='REGISTERING':
            self.prepare(group)
            group = self.store.get(self.repo,group['group_id'])
        if group['pending_source_head'] and (not group['source_update_conflict'] or group['source_update_candidate']):
            from loop_manager_internal.git.manual_source_update import update_source
            group=update_source(self,group)
        confirm_dependencies(self.connection,self.effects,self.repo_path,json.loads(group['documents']),self.base)
        owner = group['active_id'] or group['owner_id']
        row = self.runtime.get(self.repo,owner)
        if row.status=='PLAN_BLOCKED':
            return  # Existing scoped recovery owns final blocks.
        if row.status in {'PR_OPEN','MERGED'}:
            return
        from loop_manager_internal.execution.manual_candidate import ManualCandidate
        from types import SimpleNamespace
        ManualCandidate(self.connection,self.repo,owner,self.effects.git,self.effects.git_bytes,
            config_path=self.config_path,provider=self.project['provider'],
            resolve_profile=self.effects.resolve_profile).note_pending(
            SimpleNamespace(worktree=Path(group['worktree']),base_branch=self.base))
        from .task_scheduling import current_scheduler, Task
        scheduler = current_scheduler.get()
        with (scheduler.admission(Task(self.repo,owner,self.connection))
              if scheduler is not None else nullcontext()):
            if not self.slot_available(recovering=group):
                return
            with self.connection:
                self.runtime.update_plan(self.repo,owner,plan_values={'status':group['resume_phase']},enqueue=False)
                self.runtime.hydrate(self.repo,owner)
                self.store.update(group,active_id=owner)
        return

    def display(self, group, *, accepted):
        from loop_manager_internal.execution.validation_feedback import redact_secrets
        members = ', '.join(json.loads(group['members'])) or '(G 등록 준비 중)'
        rows = self.connection.execute('SELECT plan_id,status,pr_url FROM exec_plans WHERE repo=? '
            'AND plan_id IN (SELECT plan_id FROM manual_work_members WHERE repo=? AND group_id=?)',
            (self.repo,self.repo,group['group_id'])).fetchall()
        receipts = self.connection.execute('SELECT r.plan_id,r.stage,r.result,r.head,r.detail,'
            'q.reason AS request_reason FROM manual_work_results r LEFT JOIN manual_work_requests q '
            'ON q.request_id=r.request_id WHERE r.repo=? AND r.group_id=? ORDER BY r.created_at',
            (self.repo,group['group_id'])).fetchall()
        request=self.store.request(group)
        current=group['definition_head'] or group['prepared_head'] or group['source_head']
        if group['worktree'] and Path(group['worktree']).is_dir() and group['workspace_created']:
            current=self.effects.git(Path(group['worktree']),'rev-parse','HEAD').stdout.strip()
        self.effects.operation_print(redact_secrets(f"수동 준비됨={group['state']=='MANUAL_WORKING'}; "
            f"자동 제출 수락됨={accepted}; 보존됨=True; {group['state']}; "
            f"묶음={group['group_id']}; G={members}; source={group['source_branch']}@{group['source_head']}; "
            f"최초 source={group['initial_source_head']}; 수락 HEAD={request['source_head'] if request else '없음'}; "
            f"현재 후보={current}; 소유 branch={group['branch']}; worktree={group['worktree']}; "
            f"base=origin/{self.base}; {group['last_error'] or ''}"))
        for row in rows:
            self.effects.operation_print(f"{row['plan_id']} {row['status']} {row['pr_url'] or ''}")
            for stage in ('verify','review'):
                if not any(receipt['plan_id']==row['plan_id'] and receipt['stage']==stage for receipt in receipts):
                    self.effects.operation_print(f"{row['plan_id']} {stage} NOT_RUN")
        from loop_manager_internal.execution.manual_candidate import manual_skip_reason_label
        for receipt in receipts:
            detail=receipt['detail']
            if receipt['result']=='SKIPPED_BY_USER':
                detail=manual_skip_reason_label(receipt['result'],receipt['request_reason'])
            self.effects.operation_print(redact_secrets(f"{receipt['plan_id']} {receipt['stage']} "
                f"{receipt['result']} @{receipt['head']}: {detail[:1000]}"))
        if group['state']=='MANUAL_WORKING':
            head=manager_git_journal.live_remote_branch_oid(self.effects.git,self.repo_path,group['branch'])
            self.effects.operation_print(f'현재 원격 HEAD={head}; worktree={group["worktree"]}')
            self.effects.operation_print(f"git fetch origin; git switch --track origin/{group['branch']}\n"
                f"완료: python loop_manager.py manual complete {self.repo} {group['owner_id']}")

    @manual_preparation
    def submit(self, args):
        requirements = sorted(set(args.requirement))
        if not all(re.fullmatch(r'docs/requirements/[^/\\]+\.md',path) for path in requirements):
            raise ManualWorkError('--requirement는 docs/requirements/ 바로 아래의 R 문서여야 합니다.')
        head = self.fetch(args.branch)
        if args.expected_head and head!=args.expected_head:
            raise ManualWorkError(f'수락 전 HEAD 불일치: expected {args.expected_head}, actual {head}')
        for path in requirements:
            if not self.document(path,head).strip():
                raise ManualWorkError(f'빈 R 문서: {path}')
        base = self.fetch(self.base)
        group = self.store.for_source(self.repo,args.branch,requirements)
        if group and group['state']=='MERGED':
            if head != group['source_head']:
                raise ManualWorkError('이미 Merge된 제출 범위입니다. 새로운 작업은 별도 source branch로 제출하세요.')
            self.display(group,accepted=True)
            return 0
        source_changed=bool(group and group['source_head']!=head)
        if group and group['source_update_candidate'] and group['pending_source_head']!=head:
            raise ManualWorkError('이전 source 후보의 게시가 미확정입니다. 기존 요청·후보를 먼저 복구해야 합니다.')
        if group and group['state']=='PR_OPEN' and not source_changed:
            self.display(group,accepted=True)
            return 0
        if source_changed and group['state']=='PR_OPEN':
            owner=self.runtime.get(self.repo,group['owner_id'])
            pr=self.effects.pr_for_branch(self.repo_path,group['branch'],owner.pr_url)
            if pr and (pr.get('state')=='MERGED' or pr.get('mergedAt')):
                raise ManualWorkError('실제 Merge된 제출 묶음은 새 후보로 재사용할 수 없습니다.')
        if group is None:
            active = self.connection.execute("SELECT plan_id,sources FROM exec_plans WHERE repo=? AND status!='MERGED'",(self.repo,)).fetchall()
            overlap = [row['plan_id'] for row in active if set(json.loads(row['sources'])) & set(requirements)]
            if overlap:
                raise ManualWorkError('진행 중인 기존 G와 같은 R입니다. 목표 중복을 안전하게 배제할 수 없습니다: '+', '.join(overlap))
            if set(requirements) & live_requirement_paths(self.connection,self.repo):
                raise ManualWorkError('같은 R의 미완료 수동 제출이 있습니다. 기존 source·R 범위로 다시 제출하세요.')
        with runtime_state._transaction(self.connection):
            preparation_gate(self, plan_ids=json.loads(group['members']) if group else [],
                             group=group, requirements=requirements, inspect_workspace=False)
            if group is None:
                current_overlap = self.connection.execute("SELECT plan_id,sources FROM exec_plans WHERE repo=? AND status!='MERGED'", (self.repo,)).fetchall()
                if any(set(json.loads(row['sources'])) & set(requirements) for row in current_overlap) or set(requirements) & live_requirement_paths(self.connection,self.repo):
                    raise ManualWorkError('같은 R의 실행·수동 소유 예약이 준비 중 변경됐습니다.')
                group = self.store.create(self.repo,'submit',args.branch,requirements,head,base)
            if source_changed:
                if group['state']=='GENERATING':
                    self.store.update(group,source_head=head,base_sha=base,attempts=0,last_error=None)
                else:
                    self.store.update(group,pending_source_head=head,attempts=0,last_error=None,
                        source_update_conflict=group['source_update_conflict'] if group['source_update_candidate'] else 0,
                        state=group['state'] if group['state']=='REGISTERING' else 'AUTOMATIC')
        group = self.store.get(self.repo, group['group_id'])
        if group['state']=='PREPARATION_CONFLICT':
            with self.connection:
                group=self.store.update(group, state='REGISTERING' if group['owner_id'] else 'GENERATING',
                                        last_error=None, attempts=0)
        try:
            if group['state']=='GENERATING':
                self.generate(group)
                group = self.store.get(self.repo, group['group_id'])
            if group['state']=='REGISTERING':
                self.prepare(group)
                group = self.store.get(self.repo, group['group_id'])
            if group['pending_source_head'] and (not group['source_update_conflict'] or group['source_update_candidate']):
                from loop_manager_internal.git.manual_source_update import update_source
                update_source(self, group)
        except (ManualConflictError, ProcessLockBusyError, WorkspaceOwnershipError, PublicationSafetyError, GitLockError) as error:
            with self.connection:
                if not group['request_id']:
                    self.store.update(group, state='PREPARATION_CONFLICT', last_error=str(error))
            raise ManualConflictError(str(error)) from error
        except (RuntimeError, OSError, ValueError, TypeError, KeyError) as error:
            with self.connection:
                self.store.update(group, last_error=str(error),
                    attempts=group['attempts']+(0 if isinstance(error,ManualDependencyWait) else 1))
            self._accept_submit(group, args, head, base)
            self.display(self.store.get(self.repo, group['group_id']), accepted=True)
            return 1
        self._accept_submit(group, args, head, base)
        return group['group_id']

    def _accept_submit(self, group, args, head, base):
        group=self.store.get(self.repo, group['group_id'])
        documents=json.loads(group['documents'])
        candidate,verify_input,review_input=candidate_input(
            self.effects,self.repo_path,head,base,documents,connection=self.connection,
            config_path=self.config_path,provider=self.project['provider'],worktree=group['worktree'])
        from loop_manager_internal.execution.manual_candidate import candidate_token
        with runtime_state._transaction(self.connection):
            if (dict(self.store.get(self.repo, group['group_id'])) != dict(group)
                    or candidate_token(self.connection,self.repo_path,head,base,documents) != candidate):
                raise ManualConflictError('수동 제출 관측 뒤 요청·지침 입력이 변경됐습니다.')
            preparation_gate(self, plan_ids=json.loads(group['members']), group=group,
                             requirements=json.loads(group['requirements']), inspect_workspace=False)
            self.store.accept(group,head=head,base=base,input_token=candidate,
                verify_input_token=verify_input,review_input_token=review_input,
                skip_verify=args.skip_verify,skip_review=args.skip_review,
                reason=normalize_skip_reason(args.skip_reason))

    def generate(self, group):
        if group['attempts'] >= 3:
            raise ManualWorkError('G 생성·등록 재시도 한도에 도달했습니다. 기존 제출 초안을 보존했습니다.')
        requirements=json.loads(group['requirements'])
        r_text={path:self.document(path,group['source_head']) for path in requirements}
        response=self.effects.generate_plans(self.connection,self.repo_path,self.state_dir,self.config_path,
            self.project['provider'],group,r_text)
        payload=json.loads(response)
        drafts=payload.get('plans')
        if not isinstance(drafts,list) or not drafts or len(drafts)>32:
            raise ManualWorkError('완성된 G 목록과 확정 가능한 제출 범위가 필요합니다.')
        catalog=self.effects.git(self.repo_path,'ls-tree','-r','--name-only',group['base_sha'],'--','docs/plans').stdout
        source_catalog=self.effects.git(self.repo_path,'ls-tree','-r','--name-only',group['source_head'],'--','docs/plans').stdout
        with runtime_state._transaction(self.connection):
            preparation_gate(self, group=group, requirements=requirements, inspect_workspace=False)
            records=self.connection.execute('SELECT plan_id FROM exec_plans WHERE repo=? UNION SELECT plan_id '
                'FROM manual_work_members WHERE repo=? UNION SELECT plan_id FROM execplan_catalog_history WHERE repo=? '
                'UNION SELECT plan_id FROM execplan_cancellations WHERE repo=?', (self.repo,)*4).fetchall()
            used=set(re.findall(r'G-\d{3,}',catalog)) | {row[0] for row in records}
            used.update(re.findall(r'G-\d{3,}',source_catalog))
            used.update(key for key,_path in execplan_catalog_state.DefinitionStateRepository(self.connection).capture(self.repo).reserved)
            used.update(work_claims.publication_claims(self.connection, self.repo)[0])
            first=max((int(key[2:]) for key in used),default=0)+1
            ids=[f'G-{first+index:03d}' for index in range(len(drafts))]
            aliases={str(item.get('id',index)):ids[index] for index,item in enumerate(drafts)}
            if len(aliases)!=len(drafts):
                raise ManualWorkError('생성 G의 임시 ID가 중복됐습니다. dependency를 임의 변경하지 않습니다.')
            if set(aliases)&used:
                raise ManualWorkError('생성 G의 임시 ID가 기존 G ID와 충돌합니다. 외부 dependency를 임의 변경하지 않습니다.')
            documents=[]
            for index,item in enumerate(drafts):
                title=item['title']
                if not isinstance(title,str) or not title.strip() or re.search(r'[/\\\n\r:]',title):
                    raise ManualWorkError('생성 G title이 유효하지 않습니다.')
                sources=item['sources']
                if not sources or not set(sources)<=set(requirements) or not set(sources)&set(requirements):
                    raise ManualWorkError('생성 G sources는 제출한 R 범위여야 합니다.')
                dependencies=[aliases.get(key,key) for key in item.get('depends_on',[])]
                plan={'id':ids[index],'title':title,'sources':sources,'depends_on':dependencies,
                      'complexity':item['complexity'],'risk':item['risk'],'priority':item['priority']}
                lines=['---',f"id: {plan['id']}",f'title: {title}','sources:',*[f'  - {path}' for path in sources],
                    'depends_on:' if dependencies else 'depends_on: []',*[f'  - {key}' for key in dependencies],
                    f"complexity: {plan['complexity']}",f"risk: {plan['risk']}",f"priority: {plan['priority']}",'---','',item['body']]
                text='\n'.join(lines).rstrip()+'\n'
                for section in ('Purpose','Acceptance Criteria','Implementation','Validation','Progress','Decision Log'):
                    if not re.search(rf'(?m)^## {section}\s*$',text):
                        raise ManualWorkError(f'생성 G에 {section}이 없습니다.')
                path=f"docs/plans/[{ids[index]}] {title}.md"
                snapshot=execplan_catalog.snapshot_from_documents({path:text.encode()},validate_sources=False,isolate_errors=True)
                issues=[issue for issue in snapshot.issues if issue.code!='missing-dependency']
                if issues:
                    raise ManualWorkError('; '.join(issue.message for issue in issues))
                plan=execplan_catalog.parse_execplan_text(path,text,validate_sources=False,require_priority=True)
                from loop_manager_internal.catalog import execplan_drafts
                execplan_drafts.validate_agent_change(plan)
                documents.append({'path':path,'text':text,'plan':plan})
            if set(requirements)!={path for item in documents for path in item['plan']['sources']}:
                raise ManualWorkError('생성 G가 제출한 R 전체를 추적하지 않습니다.')
            validate_group_dependencies(documents)
            validate_dependency_scope(self.connection,self.repo,documents)
            branch,worktree=workspace.expected_execplan_workspace(self.state_dir,self.repo,ids[0])
        if worktree.exists() or self.effects.git(self.repo_path,'show-ref','--verify','--quiet',
                f'refs/heads/{branch}',check=False).returncode==0 \
                or manager_git_journal.live_remote_branch_oid(self.effects.git,self.repo_path,branch):
            raise ManualConflictError(f'생성 G 자원 충돌: {ids[0]}; branch={branch}; worktree={worktree}')
        with runtime_state._transaction(self.connection):
            preparation_gate(self, group=group, requirements=requirements, inspect_workspace=False)
            current_records=self.connection.execute('SELECT plan_id FROM exec_plans WHERE repo=? UNION SELECT plan_id '
                'FROM manual_work_members WHERE repo=? UNION SELECT plan_id FROM execplan_catalog_history WHERE repo=? '
                'UNION SELECT plan_id FROM execplan_cancellations WHERE repo=?', (self.repo,)*4).fetchall()
            current_ids={row[0] for row in current_records}
            current_ids.update(key for key,_path in execplan_catalog_state.DefinitionStateRepository(self.connection).capture(self.repo).reserved)
            current_ids.update(work_claims.publication_claims(self.connection, self.repo)[0])
            if current_ids & set(ids):
                raise ManualConflictError('생성 G ID의 소유 예약이 준비 중 변경됐습니다.')
            validate_dependency_scope(self.connection,self.repo,documents)
            for key in ids:
                self.connection.execute('INSERT INTO manual_work_members VALUES (?,?,?)',(self.repo,key,group['group_id']))
            self.store.update(group,members=json.dumps(ids),documents=json.dumps(documents,ensure_ascii=False),
                    owner_id=ids[0],active_id=ids[0],branch=branch,worktree=str(worktree),state='REGISTERING')

def resume_manual_work(effects: ManualWorkEffects, connection, state_dir, repo_path,
                       config_path, runtime_mutation=None):
    if not ManualWorkRepository(connection).pending(str(repo_path)):
        return
    ManualWorkService(effects,connection,state_dir,repo_path,config_path,
                      runtime_mutation=runtime_mutation).resume()


def manual_command(effects: ManualWorkEffects, args, state_dir, config_path, runtime_mutation=None):
    repository = Path(args.repository).expanduser().resolve()
    command = args.manual_command
    plan_id = getattr(args, "plan_id", None)
    target = plan_id or getattr(args, "branch", "project")
    with events.log_context(project=str(repository), plan=plan_id, stage=f"MANUAL_{command.upper()}"):
        with events.activity(
            f"수동 {command} 처리", heartbeat=True, error_level="ERROR",
            command=command, target=target,
            requirement_count=len(getattr(args, "requirement", ())),
            requirements=",".join(getattr(args, "requirement", ())),
        ) as report:
            with closing(effects.connect_database(state_dir)) as connection:
                service=ManualWorkService(effects,connection,state_dir,repository,
                                          config_path,runtime_mutation=runtime_mutation)
                try:
                    service.validate_repository()
                    if command == 'start':
                        result = service.start(args.plan_id)
                    elif command == 'complete':
                        result = service.complete(args)
                    else:
                        result = service.submit(args)
                    report.update(
                        result="수동 작업 준비 결과 기록" if command == "start"
                        else "수락 또는 기존 게시 상태 확인",
                        next_action=(
                            "수동 작업 안내에 따라 구현 후 complete 실행" if command == "start"
                            else "실제 검증·게시 결과는 이후 run에서 확인"
                        ),
                    )
                    return result
                except (RuntimeError, OSError, ValueError) as error:
                    from loop_manager_internal.execution.validation_feedback import redact_secrets
                    level = "WARN" if isinstance(error, ManualDependencyWait) else "ERROR"
                    report.update(
                        result="수동 명령 실패", reason=events.summary(error),
                        next_action=("dependency Merge 확인 뒤 다시 실행"
                                     if isinstance(error, ManualDependencyWait)
                                     else "실패 원인 확인; 저장된 intent와 작업물 보존"),
                        level=level,
                    )
                    effects.operation_print(redact_secrets(
                        f'수동 명령 실패: {error}; 저장된 intent와 작업물은 보존됨'
                    ))
                    return 1
