"""Manual submission evidence consumed by the existing execution phases."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import platform
import re
import sys

from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository, digest

MANUAL_TEST_EVIDENCE = 'MANUAL_EXTERNAL_TARGETED_TESTS_UNCONFIRMED'


def get_manual(state):
    candidate = getattr(state, 'manual', None)
    return candidate if isinstance(candidate, ManualCandidate) else None


def environment_identity(repo_path):
    # Never persist environment variable values (possibly credentials).
    # A hash still detects changes relevant to external build tool configuration.
    return (str(Path(repo_path).resolve()), platform.platform(), sys.executable,
            sys.version, digest(sorted(os.environ.items())))


def candidate_token(connection, repo, head, base, documents):
    """Bind receipts to the common version of every member, outside Git HEAD."""
    from loop_manager_internal.agents import common_guidance
    guidance=tuple((item['plan']['id'],common_guidance.plan_content_hash(
        connection,str(repo),item['plan']['id']), common_guidance.worker_content_hash(
        connection,str(repo),item['plan']['id'])) for item in documents)
    return digest((head,base,documents,environment_identity(repo),guidance))


@dataclass(frozen=True)
class ManualPublication:
    """Publication metadata from a user intake, never a Worker success result."""
    commit_title: str
    summary: str
    test_evidence: str = MANUAL_TEST_EVIDENCE


class ManualCandidate:
    def __init__(self, connection, repo, plan_id, git, git_bytes, pr_for_branch=None):
        self.store = ManualWorkRepository(connection)
        self.connection, self.repo, self.plan_id = connection, repo, plan_id
        self.git, self.git_bytes = git, git_bytes
        self.pr_for_branch = pr_for_branch

    def group(self):
        return self.store.for_plan(self.repo, self.plan_id)

    def enabled(self):
        group = self.group()
        return group is not None and group['state'] == 'AUTOMATIC' and bool(group['request_id'])

    def adopt_remote(self, context, remote, base, pr, state, checkpoints):
        """Adopt a clean fast-forward; all other work uses collaborative recovery."""
        if not self.enabled() or not remote or context.resolving_rebase:
            return False
        group=self.group()
        self.store.workspace_owner(self.repo,self.plan_id)
        if context.branch!=group['branch'] or str(context.worktree)!=group['worktree']:
            raise RuntimeError('수동 원격 반영 자원의 묶음 소유 identity가 다릅니다.')
        if self.git(context.worktree,'status','--porcelain').stdout.strip():
            return False
        local=self.git(context.worktree,'rev-parse','HEAD').stdout.strip()
        if pr and (pr.get('headRefName')!=context.branch or pr.get('baseRefName')!=context.base_branch
                or (context.pr_url and pr.get('url')!=context.pr_url)
                or pr.get('headRefOid')!=remote or pr.get('state')!='OPEN'):
            return False
        if local==remote:
            current=self.connection.execute('SELECT publication_remote_oid FROM exec_plans '
                'WHERE repo=? AND plan_id=?',(self.repo,self.plan_id)).fetchone()
            if current['publication_remote_oid']!=remote or (pr and not context.pr_url):
                # A user may commit and push in the existing managed worktree.
                # Its clean HEAD is already adopted, but its lease still needs
                # the same observed remote identity as a fast-forward intake.
                values={'publication_remote_oid':remote}
                if pr:
                    values.update(pr_url=pr['url'],pr_head_oid=remote)
                state.update_project(self.repo,**values)
                return True
            return False
        if self.git(context.worktree,'merge-base','--is-ancestor',local,remote,check=False).returncode:
            return False
        documents=self.documents(context.worktree,remote)
        definition_head=remote
        if group['kind']=='start' and group['definition_head']==base and self.git(
                context.worktree,'merge-base','--is-ancestor',base,remote,check=False).returncode:
            # The revision gate will first integrate the user's newer base G.
            documents=json.loads(group['documents'])
            definition_head=base
        from loop_manager_internal.git import manager_git_journal
        from loop_manager_internal.runtime.contracts import PublicationSafetyError
        def observation():
            return {'head':self.git(context.worktree,'rev-parse','HEAD').stdout.strip(),
                'branch':f'refs/heads/{context.branch}',
                'index':{'tree':self.git(context.worktree,'write-tree').stdout.strip()},
                'changes':[],'operations':[]}
        before=observation()
        expected={**before,'head':remote,'index':{'tree':self.git(context.worktree,'rev-parse',f'{remote}^{{tree}}').stdout.strip()}}
        def apply(_token):
            if manager_git_journal.live_remote_branch_oid(self.git,Path(self.repo),context.branch)!=remote:
                raise PublicationSafetyError('수동 후보 fast-forward 직전 원격 HEAD가 변경됐습니다.')
            self.git(context.worktree,'merge','--ff-only',remote)
        manager_git_journal.execute(context.state_dir,owner=f"plan:{group['owner_id']}",kind='manual_fast_forward',
            repo=self.repo,target=str(context.worktree),before=before,expected=expected,
            observe=observation,apply=apply,marker=lambda _intent:True)
        values={'phase':group['resume_phase'] if group['source_update_conflict'] else 'VERIFYING',
            'worker_result':None,'verify_result':None,'review_result':None,
            'worker_input_token':None,'archive_token':None,'verify_token':None,'review_token':None,
            'publication_token':None,'validated_base_sha':None,'publication_remote_oid':remote}
        if pr:
            values.update(pr_url=pr['url'],pr_head_oid=remote)
        state.update_project(self.repo,**values)
        with self.connection:
            self.store.update(group,documents=json.dumps(documents,ensure_ascii=False),definition_head=definition_head)
        current=checkpoints.get(self.repo,self.plan_id)
        checkpoints.record(self.repo,self.plan_id,expected_generation=current.generation,operation=None,
            expected_head=remote,target_base_oid=base,observed_remote_oid=remote,
            snapshot={'manual_candidate_head':remote})
        return True

    def user_submission(self, row):
        return self.enabled() and not row['worker_result']

    def documents(self, worktree, head):
        group = self.group()
        entries=self.git_bytes(worktree,'ls-tree','-rz',head).split(b'\0')
        regular=[entry.partition(b'\t')[2].decode('utf-8') for entry in entries
                 if entry.startswith((b'100644 blob ', b'100755 blob '))]
        documents = []
        for stored in json.loads(group['documents']):
            candidates=[path for path in regular if re.fullmatch(r'docs/plans/\['+
                re.escape(stored['plan']['id'])+r'\] [^/]+\.md',path)]
            if len(candidates)!=1:
                raise RuntimeError(f"수동 후보의 G 문서가 없거나 중복됐습니다: {stored['plan']['id']}")
            path=candidates[0]
            text = self.git(worktree,'show',f'{head}:{path}').stdout
            metadata = execplan_catalog.parse_execplan_text(path,text,validate_sources=False)
            if metadata['id'] != stored['plan']['id']:
                raise RuntimeError('제출 묶음의 G identity가 변경됐습니다.')
            documents.append({'path':path,'text':text,'plan':metadata})
        from loop_manager_internal.application.manual_work import validate_group_dependencies, validate_dependency_scope
        validate_group_dependencies(documents)
        validate_dependency_scope(self.connection,self.repo,documents)
        # Validate every member and all R against the candidate, including R
        # documents that have not yet reached the registered base catalog.
        known = self.connection.execute('SELECT plan_id FROM exec_plans WHERE repo=?',(self.repo,)).fetchall()
        # Graph dependency existence is checked by the authoritative catalog;
        # each member is independently validated here with its external edges.
        for item in documents:
            from loop_manager_internal.storage import verify_input_requests
            verify_input_requests.validate(self.connection, self.repo, item['text'], item['plan']['sources'])
            if any(dep not in {row[0] for row in known} for dep in item['plan']['depends_on']):
                raise RuntimeError('제출 G의 외부 dependency가 등록되지 않았습니다.')
            isolated = execplan_catalog.snapshot_from_documents(
                {item['path']:item['text'].encode()},validate_sources=True,
                available_regular_paths=frozenset(regular),isolate_errors=True)
            issues = [issue for issue in isolated.issues if issue.code != 'missing-dependency']
            if issues:
                raise RuntimeError('; '.join(issue.message for issue in issues))
        return documents

    def definition(self, context, catalog_plan, base):
        head = self.git(context.worktree,'rev-parse','HEAD').stdout.strip()
        self.documents(context.worktree,head)
        group = self.group()
        documents = json.loads(group['documents'])
        selected = next(item for item in documents if item['plan']['id']==self.plan_id)
        plan = selected['plan']
        sources = sorted({path for item in documents for path in item['plan']['sources']})
        source_texts=[]
        for path in sources:
            from loop_manager_internal.catalog.manager_sources import SOURCE, source_content
            if path == SOURCE:
                source_texts.append((path, source_content(path)))
                continue
            result=self.git(context.worktree,'show',f'{head}:{path}',check=False)
            if result.returncode:
                # The revision gate integrates this observed user base before
                # any stage runs; this is definition input, never stage PASS.
                if group['kind']!='start' or self.git(context.worktree,'merge-base','--is-ancestor',base,head,
                                                     check=False).returncode==0:
                    raise RuntimeError(f'수동 후보의 R 입력이 없습니다: {path}')
                result=self.git(Path(self.repo),'show',f'{base}:{path}')
            source_texts.append((path,result.stdout))
        from loop_manager_internal.agents import common_guidance
        snapshot = DefinitionSnapshot(base,selected['path'],plan['title'],tuple(sources),
            tuple(sorted({dep for item in documents for dep in item['plan']['depends_on']})),
            plan['complexity'],plan['risk'],plan['priority'],digest(documents),digest(source_texts),(),
            guidance_hash=common_guidance.plan_content_hash(
                self.connection,self.repo,self.plan_id),
            worker_guidance_hash=common_guidance.worker_content_hash(
                self.connection,self.repo,self.plan_id))
        return snapshot

    def confirm_dependencies(self, context):
        from loop_manager_internal.application.manual_work import confirm_dependencies
        from types import SimpleNamespace
        if self.pr_for_branch is not None:
            confirm_dependencies(self.connection,SimpleNamespace(pr_for_branch=self.pr_for_branch),
                                 Path(self.repo),json.loads(self.group()['documents']),context.base_branch)

    def publication(self, message, plan, fallback):
        if message or not self.enabled():
            return fallback(message,plan)
        group=self.group()
        results=self.connection.execute('SELECT plan_id,stage,result,head,detail FROM manual_work_results '
            'WHERE repo=? AND group_id=? ORDER BY created_at', (self.repo,group['group_id'])).fetchall()
        evidence='\n'.join(f"- {row['plan_id']} {row['stage']}: {row['result']} @{row['head']} "
                           f"{row['detail'][:1000]}" for row in results)
        return ManualPublication(f"{plan['id']}: {plan['title']}",
            f"수동 제출 묶음 {group['group_id']}: {', '.join(json.loads(group['members']))}\n"
            f"Source: {group['source_branch']}@{group['source_head']}\n"
            '외부 targeted test 수행 결과는 Manager가 확인하지 않았습니다.\n'+evidence)

    def inputs(self, context, stage):
        group=self.group()
        head=self.git(context.worktree,'rev-parse','HEAD').stdout.strip()
        base=str(self.git(Path(self.repo),'rev-parse',f'refs/remotes/origin/{context.base_branch}').stdout.strip())
        token=candidate_token(self.connection,self.repo,head,base,json.loads(group['documents']))
        row=self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (self.repo,self.plan_id)).fetchone()
        stage_token=digest((token,stage,row['verify_result'] if stage=='review' else None,group['members']))
        return group,head,base,token,stage_token

    def waive(self, context, stage, *, record=True):
        if not self.enabled():
            return None
        group,head,base,token,stage_token=self.inputs(context,stage)
        request=self.store.request(group)
        if not request or not request[f'skip_{stage}']:
            return None
        if request['source_head']!=head or request['base_sha']!=base or request['input_token']!=token:
            return None
        if self.documents(context.worktree,head)!=json.loads(group['documents']):
            return None
        # Actual failure on this candidate can only be resolved by correction
        # or a successful actual rerun, never by changing command flags.
        failed=self.connection.execute("SELECT 1 FROM manual_work_results WHERE repo=? AND group_id=? "
            "AND stage=? AND head=? AND result='FAIL' LIMIT 1",(self.repo,group['group_id'],stage,head)).fetchone()
        if failed:
            return None
        detail=f"SKIPPED_BY_USER @{head}; {request['reason']}"
        if record:
            self.store.record(group,stage,head,base,stage_token,'SKIPPED_BY_USER',detail)
        return detail

    def note_pending(self, context):
        if not self.enabled():
            return
        for stage in ('verify','review'):
            group,head,base,_token,stage_token=self.inputs(context,stage)
            if not self.connection.execute('SELECT 1 FROM manual_work_results WHERE repo=? AND group_id=? '
                'AND stage=? AND head=? AND base_sha=? AND input_token=?',
                (self.repo,group['group_id'],stage,head,base,stage_token)).fetchone():
                self.store.record(group,stage,head,base,stage_token,'NOT_RUN','수락된 요청의 자동 처리 대기')

    def valid_evidence(self, context, stage, result):
        if result is None:
            return False
        if str(result).startswith('SKIPPED_BY_USER '):
            return self.waive(context,stage,record=False) is not None
        if not self.enabled():
            return True
        group,head,base,_token,stage_token=self.inputs(context,stage)
        required={(member,stage) for member in json.loads(group['members'])}
        passed={(row[0],stage) for row in self.connection.execute(
            "SELECT plan_id FROM manual_work_results WHERE repo=? AND group_id=? AND stage=? "
            "AND head=? AND base_sha=? AND input_token=? AND result='PASS'",
            (self.repo,group['group_id'],stage,head,base,stage_token))}
        return required<=passed

    def invalid_waiver_notice(self, context, stage):
        if not self.enabled():
            return None
        group=self.group()
        request=self.store.request(group)
        if request and request[f'skip_{stage}'] and self.waive(context,stage,record=False) is None:
            _group,head,base,_token,stage_token=self.inputs(context,stage)
            detail=f'{stage} 생략 무효화: 제출 SHA·base·G/R·환경·단계 입력 또는 실제 실패 근거가 달라 기본 실행합니다.'
            self.store.record(group,stage,head,base,stage_token,'NOT_RUN',detail)
            return detail
        return None

    def record(self, context, stage, passed, detail, *, plan_ids=None):
        if self.enabled():
            group,head,base,_token,stage_token=self.inputs(context,stage)
            self.store.record(group,stage,head,base,stage_token,'PASS' if passed else 'FAIL',detail,plan_ids=plan_ids)

    def prompt(self, prompt, *, review=False):
        if not self.enabled():
            return prompt
        group=self.group()
        docs=json.loads(group['documents'])
        content='\n\n'.join(f"### {item['path']}\n{item['text']}" for item in docs)
        row=self.connection.execute('SELECT verify_result FROM exec_plans WHERE repo=? AND plan_id=?',
                                    (self.repo,self.plan_id)).fetchone()
        instruction=(
            '\n수동 제출입니다. 외부 targeted test 결과는 확인되지 않았으며 PASS로 가정하지 마세요.\n'
            f"전체 verify 근거: {row['verify_result'] or 'NOT_RUN'}\n"
            '현재 후보의 R 원문과 아래 모든 G를 함께 확인하세요. 한 G도 누락하지 마세요.\n')
        if group['pending_source_head'] and group['source_update_conflict']:
            incoming=group['pending_source_head']
            source_diff=self.git(Path(self.repo),'diff',group['initial_source_head'],incoming).stdout
            instruction += (f'\n반복 source 제출 {incoming}의 Merge 충돌로 자동 교정 중입니다. '
                '두 이력은 보존돼 있지만 새 source 변경은 아직 구현에 반영되지 않았습니다. '
                '아래 incoming diff를 현재 사용자/Worker 변경과 함께 보존·통합하세요. '
                'R 의미를 확정할 수 없는 충돌은 기존 사용자 명세 결정 절차를 적용하세요.\n'+source_diff)
        if review:
            instruction += ('각 G별 결과를 `MANUAL_PLAN_RESULT: G-000 PASS` 또는 FAIL로 한 줄씩 기록하세요. '
                '전체 VERDICT는 모든 G가 PASS일 때만 PASS입니다. SPEC_BLOCKED는 기존 명세 결정 형식을 사용하세요.\n')
        return prompt+instruction+content

    def review_results(self, context, verdict, message):
        if not self.enabled() or verdict=='SPEC_BLOCKED':
            return
        members=json.loads(self.group()['members'])
        matches=re.findall(r'(?m)^MANUAL_PLAN_RESULT: (G-\d{3,}) (PASS|FAIL)\s*$',message)
        results=dict(matches)
        if len(matches)!=len(members) or set(results)!=set(members) \
                or (verdict=='PASS' and any(value!='PASS' for value in results.values())) \
                or (verdict=='FAIL' and all(value=='PASS' for value in results.values())):
            from loop_manager_internal.application.contracts import ReviewerProtocolError
            raise ReviewerProtocolError('수동 제출 묶음의 모든 G에 정확한 개별 Review 결과가 필요합니다.')
        for member,value in results.items():
            self.record(context,'review',value=='PASS',message,plan_ids=(member,))

        if verdict=='FAIL':
            with self.connection:
                self.store.update(self.group(),active_id=next(member for member in members if results[member]=='FAIL'))
        elif self.group()['source_update_conflict']:
            with self.connection:
                self.store.update(self.group(),pending_source_head=None,source_update_conflict=0)

    def decorate_body(self, body):
        if not self.enabled():
            return body
        group=self.group()
        head=self.git(Path(group['worktree']),'rev-parse','HEAD').stdout.strip()
        results=self.connection.execute('SELECT plan_id,stage,result,detail FROM manual_work_results '
            'WHERE repo=? AND group_id=? AND head=? ORDER BY created_at', (self.repo,group['group_id'],head)).fetchall()
        rows='\n'.join(f"- {row['plan_id']} {row['stage']}: {row['result']} — {row['detail'][:1000]}" for row in results)
        members='\n'.join(f"- {item['plan']['id']}: {item['path']} (R: {', '.join(item['plan']['sources'])})"
                          for item in json.loads(group['documents']))
        return (body+f"\n\n## Manual submission\nGroup: {group['group_id']}\n"
            f"Source: {group['source_branch']}@{group['initial_source_head']}\nCandidate: {head}\n"
            '외부 targeted test 수행 결과는 Manager가 확인하지 않았습니다.\n'+members+'\n\n'+rows)
