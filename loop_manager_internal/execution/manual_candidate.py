"""Manual submission evidence consumed by the existing execution phases."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import platform
import re
import sys
from types import SimpleNamespace

from loop_manager_internal.catalog import execplan_catalog, requirement_sources
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.git.command_adapter import revision_requirement_documents
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository, digest

MANUAL_TEST_EVIDENCE = 'MANUAL_EXTERNAL_TARGETED_TESTS_UNCONFIRMED'
_UNSET = object()


@dataclass(frozen=True)
class ManualStageSnapshot:
    """Candidate and effective settings observed immediately before a manual stage."""
    group_id: str
    head: str
    base: str
    candidate_token: str
    setting_token: str | None
    input_token: str
    known: bool
    resolved_profile: object | None = None
    current_settings_match: bool | None = None


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


def stage_input_tokens(config_path, provider, worktree, resolve_profile, *, stages=('verify','review'),
                       verify_timeout_seconds=_UNSET, verify_parallelism=_UNSET,
                       resolved_profile=_UNSET):
    """Observe only the effective global settings that affect manual stages."""
    config = None
    if (verify_timeout_seconds is _UNSET or verify_parallelism is _UNSET
            or ('review' in stages and resolved_profile is _UNSET)):
        if config_path is None:
            return None, None
        try:
            from loop_manager_internal.config.managed_config import load_managed_config
            config = load_managed_config(Path(config_path))
        except Exception:
            return None, None
    timeout = (config.verify_timeout_seconds if verify_timeout_seconds is _UNSET
               else verify_timeout_seconds)
    parallelism = (config.verify_parallelism if verify_parallelism is _UNSET
                   else verify_parallelism)
    verify = digest(('verify', timeout, parallelism)) if timeout is not None and parallelism is not None else None
    review = None
    if 'review' in stages:
        try:
            profile = resolved_profile
            if profile is _UNSET:
                if resolve_profile is None:
                    return verify, None
                profile = resolve_profile(Path(config_path), provider, 'reviewer', None, Path(worktree))
            if profile is not None and verify is not None:
                review = digest(('review', provider, verify, profile.model, profile.effort))
        except Exception:
            pass
    return verify, review


def stage_result_token(candidate, stage, settings, verify_result, members):
    return digest((candidate, stage, settings, verify_result if stage == 'review' else None, members))


@dataclass(frozen=True)
class ManualPublication:
    """Publication metadata from a user intake, never a Worker success result."""
    commit_title: str
    summary: str
    test_evidence: str = MANUAL_TEST_EVIDENCE


class ManualCandidate:
    def __init__(self, connection, repo, plan_id, git, git_bytes, pr_for_branch=None,
                 config_path=None, provider=None, resolve_profile=None,
                 verify_timeout_seconds=None, verify_parallelism=None):
        self.store = ManualWorkRepository(connection)
        self.connection, self.repo, self.plan_id = connection, repo, plan_id
        self.git, self.git_bytes = git, git_bytes
        self.pr_for_branch = pr_for_branch
        self.config_path, self.provider, self.resolve_profile = config_path, provider, resolve_profile
        self.verify_timeout_seconds = verify_timeout_seconds
        self.verify_parallelism = verify_parallelism

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
        requirement_documents = revision_requirement_documents(
            self.git_bytes, worktree, head,
        )
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
                allow_archive_fallback=group['kind'] != 'submit',
                available_regular_paths=frozenset(regular),
                requirement_documents=requirement_documents,isolate_errors=True)
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
        requirement_documents = revision_requirement_documents(
            self.git_bytes, context.worktree, head,
        )
        base_requirement_documents = None
        for path in sources:
            from loop_manager_internal.catalog.manager_sources import SOURCE, source_content
            if path == SOURCE:
                source_texts.append((path, source_content(path)))
                continue
            if path.startswith('docs/requirements/') and path.count('/') == 2:
                resolved = requirement_sources.resolve_requirement_source(
                    path, requirement_documents, allow_archive=True,
                )
                if resolved.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                    raise RuntimeError(
                        f'수동 후보의 Requirement source가 모호합니다: {path} '
                        f"({', '.join(resolved.candidates)})"
                    )
                if resolved.status is requirement_sources.ResolutionStatus.RESOLVED:
                    source_texts.append((path, resolved.contents))
                    continue
            else:
                result=self.git(context.worktree,'show',f'{head}:{path}',check=False)
                if result.returncode == 0:
                    source_texts.append((path,result.stdout))
                    continue
            if group['kind']!='start' or self.git(context.worktree,'merge-base','--is-ancestor',base,head,
                                                 check=False).returncode==0:
                raise RuntimeError(f'수동 후보의 R 입력이 없습니다: {path}')
            if path.startswith('docs/requirements/') and path.count('/') == 2:
                if base_requirement_documents is None:
                    base_requirement_documents = revision_requirement_documents(
                        self.git_bytes, Path(self.repo), base,
                    )
                resolved = requirement_sources.resolve_requirement_source(
                    path, base_requirement_documents, allow_archive=True,
                )
                if resolved.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                    raise RuntimeError(
                        f'수동 후보의 base Requirement source가 모호합니다: {path} '
                        f"({', '.join(resolved.candidates)})"
                    )
                if resolved.status is requirement_sources.ResolutionStatus.RESOLVED:
                    source_texts.append((path, resolved.contents))
                    continue
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

    def inputs(self, context, stage, *, setting_token=_UNSET):
        group=self.group()
        head=self.git(context.worktree,'rev-parse','HEAD').stdout.strip()
        base=str(self.git(Path(self.repo),'rev-parse',f'refs/remotes/origin/{context.base_branch}').stdout.strip())
        token=candidate_token(self.connection,self.repo,head,base,json.loads(group['documents']))
        row=self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (self.repo,self.plan_id)).fetchone()
        if setting_token is _UNSET:
            settings=stage_input_tokens(self.config_path,self.provider,context.worktree,self.resolve_profile,
                                       stages=(stage,))
            setting_token=settings[0 if stage=='verify' else 1]
        stage_token=stage_result_token(token,stage,setting_token,row['verify_result'],group['members'])
        return group,head,base,token,setting_token,stage_token,setting_token is not None

    def capture_stage(self, context, stage):
        """Freeze the actual settings and candidate before verify or Reviewer runs."""
        if not self.enabled():
            return None
        profile = _UNSET
        if stage == 'review':
            try:
                profile = (self.resolve_profile(Path(self.config_path), self.provider, 'reviewer', None,
                                                Path(context.worktree))
                           if self.resolve_profile is not None and self.config_path is not None else None)
            except Exception:
                profile = None
        settings = stage_input_tokens(
            self.config_path, self.provider, context.worktree, self.resolve_profile,
            stages=(stage,), verify_timeout_seconds=self.verify_timeout_seconds,
            verify_parallelism=self.verify_parallelism,
            **({'resolved_profile': profile} if stage == 'review' else {}),
        )
        setting_token=settings[0 if stage=='verify' else 1]
        current_settings=stage_input_tokens(
            self.config_path, self.provider, context.worktree, self.resolve_profile,
            stages=(stage,),
            **({'resolved_profile': profile} if stage == 'review' else {}),
        )
        current_setting_token=current_settings[0 if stage=='verify' else 1]
        current_settings_match=(
            None if current_setting_token is None or setting_token is None
            else current_setting_token == setting_token
        )
        group,head,base,token,setting_token,stage_token,known=self.inputs(
            context,stage,setting_token=setting_token,
        )
        return ManualStageSnapshot(group['group_id'],head,base,token,setting_token,
                                    stage_token,known,None if profile is _UNSET else profile,
                                    current_settings_match)

    def _snapshot_inputs(self, snapshot):
        group=self.group()
        if group is None or group['group_id'] != snapshot.group_id:
            return None
        return (group,snapshot.head,snapshot.base,snapshot.candidate_token,
                snapshot.setting_token,snapshot.input_token,snapshot.known)

    def publication_stage_tokens(self, context):
        group = self.group()
        if (group is None or not group['request_id']
                or group['state'] not in {'AUTOMATIC', 'PR_OPEN'}):
            return None
        result={}
        for stage in ('verify','review'):
            _group,_head,_base,_token,_setting,stage_token,known=self.inputs(context,stage)
            if not known:
                return None
            result[stage]=stage_token
        return result

    def waive(self, context, stage, *, record=True, snapshot=None):
        if not self.enabled():
            return None
        if snapshot is not None and snapshot.current_settings_match is not True:
            return None
        values=self._snapshot_inputs(snapshot) if snapshot is not None else self.inputs(context,stage)
        if values is None:
            return None
        group,head,base,token,setting_token,stage_token,known=values
        request=self.store.request(group)
        if not request or not request[f'skip_{stage}'] or not known:
            return None
        if (request['source_head']!=head or request['base_sha']!=base or request['input_token']!=token
                or request[f'{stage}_input_token'] is None
                or request[f'{stage}_input_token']!=setting_token):
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
            group,head,base,_token,_setting,stage_token,_known=self.inputs(context,stage)
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
        group,head,base,_token,_setting,stage_token,known=self.inputs(context,stage)
        if not known:
            return False
        required={(member,stage) for member in json.loads(group['members'])}
        passed={(row[0],stage) for row in self.connection.execute(
            "SELECT plan_id FROM manual_work_results WHERE repo=? AND group_id=? AND stage=? "
            "AND head=? AND base_sha=? AND input_token=? AND result='PASS'",
            (self.repo,group['group_id'],stage,head,base,stage_token))}
        return required<=passed

    def invalid_waiver_notice(self, context, stage, *, snapshot=None):
        if not self.enabled():
            return None
        group=self.group()
        request=self.store.request(group)
        if request and request[f'skip_{stage}'] and self.waive(
                context,stage,record=False,snapshot=snapshot) is None:
            values=self._snapshot_inputs(snapshot) if snapshot is not None else self.inputs(context,stage)
            if values is None:
                return None
            _group,head,base,token,setting_token,stage_token,known=values
            if snapshot is not None and snapshot.current_settings_match is None:
                reason='현재 단계 설정을 확인할 수 없어 생략하지 않습니다.'
            elif snapshot is not None and snapshot.current_settings_match is False:
                reason='실행 구성 이후 단계 설정이 달라져 생략하지 않습니다.'
            elif not known:
                reason='현재 설정 입력을 확인할 수 없어 생략하지 않습니다.'
            elif request[f'{stage}_input_token'] is None:
                reason='수락 당시 단계 설정 지문이 없어 저장된 요청을 생략 근거로 쓸 수 없습니다.'
            elif request[f'{stage}_input_token']!=setting_token:
                reason=('요청 수락 후 verify 설정 지문이 달라졌습니다.' if stage=='verify'
                        else '요청 수락 후 Reviewer profile·verify 설정 지문이 달라졌습니다.')
            elif request['source_head']!=head or request['base_sha']!=base or request['input_token']!=token:
                reason='제출 SHA·base·G/R·환경 입력이 달라졌습니다.'
            else:
                reason='같은 후보에 실제 실패 근거가 남아 있습니다.'
            detail=f'{stage} 생략 무효화: {reason} 기본 단계를 실행합니다.'
            self.store.record(group,stage,head,base,stage_token,'NOT_RUN',detail)
            return detail
        return None

    def record(self, context, stage, passed, detail, *, plan_ids=None, snapshot=None):
        if snapshot is not None:
            if not self.enabled():
                return
            values=self._snapshot_inputs(snapshot)
            if values is None:
                return
            group,head,base,_token,_setting,stage_token,_known=values
            self.store.record(group,stage,head,base,stage_token,'PASS' if passed else 'FAIL',detail,plan_ids=plan_ids)
        elif self.enabled():
            group,head,base,_token,_setting,stage_token,_known=self.inputs(context,stage)
            self.store.record(group,stage,head,base,stage_token,'PASS' if passed else 'FAIL',detail,plan_ids=plan_ids)

    def worker_context(self):
        """Separate stable intake documents from each changed stage observation."""
        if not self.enabled():
            return {}
        group = self.group()
        documents = json.loads(group['documents'])
        context = {'manual_documents': (
            '수동 제출입니다. 외부 targeted test 결과는 확인되지 않았으며 PASS로 가정하지 마세요.\n'
            '현재 후보의 R 원문과 아래 모든 G를 함께 확인하세요. 한 G도 누락하지 마세요.\n'
            + '\n\n'.join(f"### {item['path']}\n{item['text']}" for item in documents))}
        rows = self.connection.execute(
            'SELECT plan_id,stage,result,head,detail FROM manual_work_results '
            'WHERE repo=? AND group_id=? ORDER BY created_at,rowid',
            (self.repo, group['group_id'])).fetchall()
        for row in rows:
            detail = ''
            if row['result'] == 'SKIPPED_BY_USER' and ';' in row['detail']:
                detail = '; ' + redact_secrets(row['detail'].split(';', 1)[1].strip())
            # A later result replaces only this plan/stage's observation. Raw
            # verify/Review logs stay in their audit and feedback repositories.
            context[f"manual_stage:{row['plan_id']}:{row['stage']}"] = (
                f"수동 단계 결과: {row['plan_id']} {row['stage']}: {row['result']} @{row['head']}{detail}")
        if group['pending_source_head'] and group['source_update_conflict']:
            incoming = group['pending_source_head']
            source_diff = self.git(Path(self.repo), 'diff', group['initial_source_head'], incoming).stdout
            context['manual_source'] = (
                f'반복 source 제출 {incoming}의 Merge 충돌로 자동 교정 중입니다. '
                '두 이력을 보존·통합하고 미해결 R 의미는 사용자 판단으로 넘기세요.\n' + source_diff)
        return context

    def prompt(self, prompt, *, review=False):
        if not self.enabled():
            return prompt
        group=self.group()
        docs=json.loads(group['documents'])
        content='\n\n'.join(f"### {item['path']}\n{item['text']}" for item in docs)
        instruction='\n수동 제출입니다. 외부 targeted test 결과는 확인되지 않았으며 PASS로 가정하지 마세요.\n'
        if review:
            instruction += (
                '아래는 모든 G의 실제 verify/Review 단계 상태와 후보 identity입니다. '
                '상태가 SKIPPED_BY_USER이면 실행 성공으로 취급하지 마세요.\n'
            )
        instruction += '현재 후보의 R 원문과 아래 모든 G를 함께 확인하세요. 한 G도 누락하지 마세요.\n'
        result_rows=self.connection.execute('SELECT plan_id,stage,result,head,detail FROM manual_work_results '
            'WHERE repo=? AND group_id=? ORDER BY created_at', (self.repo,group['group_id'])).fetchall()
        rows=[]
        for item in result_rows:
            detail = ''
            if item['result']=='SKIPPED_BY_USER' and ';' in item['detail']:
                detail='; '+redact_secrets(item['detail'].split(';',1)[1].strip())
            rows.append(f"- {item['plan_id']} {item['stage']}: {item['result']} @{item['head']}{detail}")
        instruction += '\n수동 단계 결과 (로그 본문 생략):\n'+'\n'.join(rows)+'\n'
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

    def review_results(self, context, verdict, message, *, snapshot=None):
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
            self.record(context,'review',value=='PASS',message,plan_ids=(member,),snapshot=snapshot)

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


def recovery_publication_stage_tokens(connection, repo, plan_id, config_path, provider,
                                      resolve_profile, git, git_bytes=None):
    """Observe the same effective stage inputs when recovery attaches a PR."""
    group = ManualWorkRepository(connection).for_plan(repo, plan_id)
    if group is None or not group['worktree']:
        return None
    worktree = Path(group['worktree'])
    if git_bytes is None:
        git_bytes = lambda path, *args: git(path, *args).stdout.encode('utf-8')
    candidate = ManualCandidate(
        connection, repo, plan_id, git, git_bytes,
        config_path=config_path, provider=provider, resolve_profile=resolve_profile,
    )
    project = connection.execute(
        'SELECT base_branch FROM projects WHERE repo=?', (repo,),
    ).fetchone()
    if project is None or not project['base_branch']:
        return None
    return candidate.publication_stage_tokens(SimpleNamespace(
        worktree=worktree,
        base_branch=str(project['base_branch']),
    ))
