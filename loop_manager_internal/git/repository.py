"""격리 Git 공간과 GitHub PR. 공개 이력은 Merge로만 통합한다."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from loop_manager_internal.catalog.observer import Document, catalog
from loop_manager_internal.runtime.process_lock import process_lock


class GitError(RuntimeError):
    """작업물을 보존한 채 재관측할 Git 오류."""


class MergeConflict(GitError):
    """Git이 양쪽 변경을 자동 병합하지 못한 상태."""


class RemoteDeleted(GitError):
    """미완료 원격 브랜치 삭제. 사용자 복구 입력이 필요하다."""


def command(argv, cwd=None, *, data=None, timeout=120, check=True):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GH_PROMPT_DISABLED='1')
    result = subprocess.run(argv,cwd=cwd,input=data,capture_output=True,timeout=timeout,
                            env=environment,encoding='utf-8',errors='strict')
    if check and result.returncode:
        raise GitError(f'{argv[0]} 명령 실패 ({result.returncode}): {result.stderr.strip()}')
    return result


def github_repository(url):
    match = re.fullmatch(r'(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?',url)
    if not match or any(part in ('.','..') for part in match.group(1).split('/')):
        raise ValueError('GitHub origin URL이 필요합니다.')
    return match.group(1)


class Repository:
    def __init__(self, project, state_dir, store):
        self.project = project
        self.repo = project['repo']
        self.remote = project['remote_url']
        self.base = project['base_branch']
        self.state_dir = Path(state_dir).resolve()
        self.store = store
        self.identity = hashlib.sha256(self.repo.encode()).hexdigest()[:20]
        self.mirror = self.state_dir/'repositories'/f'{self.identity}.git'

    def git(self, *args, cwd=None, check=True):
        return command(['git',*args],cwd=cwd or self.mirror,check=check).stdout.strip()

    def refresh(self):
        with process_lock(self.state_dir,name=f'git-{self.identity}.lock'):
            if not self.mirror.exists():
                self.mirror.parent.mkdir(parents=True,exist_ok=True)
                command(['git','clone','--bare','--no-hardlinks','--',self.remote,str(self.mirror)])
            if self.git('remote','get-url','origin') != self.remote:
                raise GitError('관측 저장소의 origin이 등록 URL과 다릅니다.')
            self.git('fetch','--prune','--no-tags','origin','+refs/heads/*:refs/remotes/origin/*')
            return self.git('rev-parse','--verify',f'refs/remotes/origin/{self.base}^{{commit}}')

    def snapshot(self, revision):
        raw = self.git('ls-tree','-rz','--full-tree',revision,'--','docs/requirements','docs/plans')
        documents = []
        for record in raw.split('\0'):
            if not record:
                continue
            header,path = record.split('\t',1)
            mode,kind,oid = header.split()
            if kind == 'blob' and mode in ('100644','100755') and path.endswith('.md'):
                content = command(['git','show',f'{revision}:{path}'],cwd=self.mirror).stdout
                documents.append(Document(path,oid,content))
        return catalog(revision,documents)

    def remote_head(self, branch):
        lines = self.git('ls-remote','--heads','origin',f'refs/heads/{branch}').splitlines()
        return lines[0].split()[0] if lines else None

    def validate_workspace(self, path, branch):
        path = Path(path).resolve()
        root = self.state_dir/'workspaces'/self.identity
        if not path.is_relative_to(root.resolve()) or path.is_symlink():
            raise GitError('등록된 격리 공간 밖의 Git 쓰기를 거부합니다.')
        if self.git('rev-parse','--show-toplevel',cwd=path).replace('\\','/') != str(path).replace('\\','/'):
            raise GitError('Git 작업 디렉터리가 일치하지 않습니다.')
        if self.git('remote','get-url','origin',cwd=path) != self.remote:
            raise GitError('Git 게시 대상 origin이 다릅니다.')
        if self.git('symbolic-ref','--short','HEAD',cwd=path) != branch:
            raise GitError('작업 브랜치가 바뀌었습니다. 파일을 보존합니다.')
        return path

    def prepare(self, branch, *, saved_path=None, published=False, on_reserved=None):
        remote_head = self.remote_head(branch)
        if published and remote_head is None:
            raise RemoteDeleted('관리 원격 브랜치가 삭제됐습니다. 자동 재생성하지 않습니다.')
        if saved_path and Path(saved_path).is_dir():
            try:
                return self.validate_workspace(saved_path,branch)
            except GitError:
                if remote_head is None and any(Path(saved_path).iterdir()):
                    raise
        root = self.state_dir/'workspaces'/self.identity
        root.mkdir(parents=True,exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=branch.rsplit('/',1)[-1]+'-',dir=root))
        if on_reserved:
            on_reserved(path)
        command(['git','clone','--no-checkout','--no-hardlinks','--',self.remote,str(path)])
        if remote_head:
            self.git('checkout','-b',branch,f'origin/{branch}',cwd=path)
        else:
            self.git('checkout','-b',branch,f'origin/{self.base}',cwd=path)
        for name,value in (('user.name','Loop Manager'),('user.email','loop-manager@users.noreply.github.com')):
            if not self.git('config','--get',name,cwd=path,check=False):
                self.git('config',name,value,cwd=path)
        return self.validate_workspace(path,branch)

    def head(self,path):
        return self.git('rev-parse','HEAD',cwd=path)

    def dirty(self,path):
        return bool(self.git('status','--porcelain','--untracked-files=all',cwd=path))

    def merge(self,path,branch,target):
        self.validate_workspace(path,branch)
        if self.dirty(path):
            raise GitError('미커밋 변경을 보존합니다. Merge 전에 Checkpoint가 필요합니다.')
        result = command(['git','merge','--no-edit','--',target],cwd=path,check=False)
        if result.returncode:
            if self.git('diff','--name-only','--diff-filter=U',cwd=path):
                raise MergeConflict('양쪽 변경을 보존하며 충돌을 교정해야 합니다.')
            raise GitError(result.stderr.strip() or result.stdout.strip())
        return self.head(path)

    def commit(self,path,branch,message):
        self.validate_workspace(path,branch)
        if self.git('diff','--name-only','--diff-filter=U',cwd=path):
            raise MergeConflict('미해결 Merge 충돌이 있습니다.')
        self.git('add','--all',cwd=path)
        merging = Path(self.git('rev-parse','--git-path','MERGE_HEAD',cwd=path))
        if not merging.is_absolute():
            merging = Path(path)/merging
        if self.git('diff','--cached','--name-only',cwd=path) or merging.exists():
            self.git('commit','-m',message,cwd=path)
        return self.head(path)

    def publish(self,path,branch,*,published=False):
        self.validate_workspace(path,branch)
        if self.dirty(path):
            raise GitError('미커밋 작업을 게시 성공으로 처리할 수 없습니다.')
        candidate = self.head(path)
        key = f'push:{self.repo}:{branch}:{candidate}'
        with self.store.transaction() as db:
            db.execute('INSERT INTO external_jobs(job_key,repo,kind,target,expected_input) VALUES(?,?,?,?,?) '
                       'ON CONFLICT DO NOTHING',(key,self.repo,'push',branch,candidate))
        remote = self.remote_head(branch)
        if published and remote is None:
            raise RemoteDeleted('기존 원격 브랜치가 삭제됐습니다. 작업물을 보존합니다.')
        if remote != candidate:
            if remote:
                self.git('fetch','origin',f'refs/heads/{branch}:refs/remotes/origin/{branch}',cwd=path)
                if command(['git','merge-base','--is-ancestor',remote,candidate],cwd=path,check=False).returncode:
                    self.merge(path,branch,f'origin/{branch}')
                    # Merge가 후보를 바꾸면 이전 Verify·Review를 재사용할 수 없다.
                    raise GitError('원격 사용자 변경을 Merge했습니다. 변경된 후보를 다시 검증합니다.')
            command(['git','push','origin',f'HEAD:refs/heads/{branch}'],cwd=path)
        if self.remote_head(branch) != candidate:
            raise GitError('원격 게시 결과가 아직 일치하지 않습니다.')
        with self.store.transaction() as db:
            db.execute('DELETE FROM external_jobs WHERE job_key=?',(key,))
        return candidate

    def cleanup(self,path,branch):
        path = self.validate_workspace(path,branch)
        if self.dirty(path) or self.git('diff','--name-only','--diff-filter=U',cwd=path):
            return False
        remote = self.remote_head(branch)
        if remote is None:
            return False
        self.git('fetch','origin',f'refs/heads/{branch}:refs/remotes/origin/{branch}',cwd=path)
        if command(['git','merge-base','--is-ancestor',self.head(path),remote],cwd=path,check=False).returncode:
            return False
        if any((path/'.git'/name).exists() for name in ('MERGE_HEAD','rebase-merge','rebase-apply','index.lock')):
            return False
        shutil.rmtree(path)
        return True


MANAGED_START = '<!-- loop-manager:start -->'
MANAGED_END = '<!-- loop-manager:end -->'


def managed_body(existing, content):
    block = f'{MANAGED_START}\n{content}\n{MANAGED_END}'
    if MANAGED_START in existing or MANAGED_END in existing:
        if existing.count(MANAGED_START)!=1 or existing.count(MANAGED_END)!=1:
            raise ValueError('PR 관리 영역 구분자가 모호합니다.')
        start,end = existing.index(MANAGED_START),existing.index(MANAGED_END)+len(MANAGED_END)
        if end < start:
            raise ValueError('PR 관리 영역 순서가 잘못됐습니다.')
        return existing[:start]+block+existing[end:]
    return (existing.rstrip()+'\n\n' if existing.strip() else '')+block


class GitHub:
    def __init__(self, repo):
        self.repo = repo

    def api(self,path,*,method='GET',payload=None):
        argv = ['gh','api',path,'--method',method]
        if payload is not None:
            argv += ['--input','-']
        raw = command(argv,data=json.dumps(payload) if payload is not None else None).stdout
        return json.loads(raw) if raw.strip() else None

    def pulls(self):
        result,page = [],1
        while True:
            batch = self.api(f'repos/{self.repo}/pulls?state=all&per_page=100&page={page}')
            result.extend(batch)
            if len(batch)<100:
                return result
            page += 1

    def pull(self,number):
        return self.api(f'repos/{self.repo}/pulls/{number}')

    def ensure_pull(self,branch,base,title,body,*,draft=False):
        existing = [item for item in self.pulls() if item['head']['ref']==branch
                    and item['head']['repo'] and item['head']['repo']['full_name']==self.repo]
        opened = [item for item in existing if item['state']=='open']
        if len(opened)>1:
            raise GitError('같은 작업 브랜치의 열린 PR이 여러 개입니다.')
        if not opened:
            if existing:
                raise GitError('기존에 닫힌 PR을 자동으로 재생성하지 않습니다.')
            return self.api(f'repos/{self.repo}/pulls',method='POST',
                            payload={'head':branch,'base':base,'title':title,'body':managed_body('',body),'draft':draft})
        current = self.pull(opened[0]['number'])
        updated = managed_body(current.get('body') or '',body)
        if updated != (current.get('body') or ''):
            current = self.api(f'repos/{self.repo}/pulls/{current["number"]}',method='PATCH',payload={'body':updated})
        if current['draft'] != draft:
            self.set_draft(current,draft)
            current = self.pull(current['number'])
        return current

    def set_draft(self,pull,draft):
        operation = 'convertPullRequestToDraft' if draft else 'markPullRequestReadyForReview'
        return self.api('graphql',method='POST',payload={
            'query':f'mutation($id:ID!){{{operation}(input:{{pullRequestId:$id}}){{pullRequest{{id}}}}}}',
            'variables':{'id':pull['node_id']}})

    def close(self,pull,expected_head):
        current = self.pull(pull['number'])
        if current['head']['sha'] != expected_head:
            raise GitError('PR 사용자 변경이 있어 Close 판단을 다시 확인해야 합니다.')
        if current['merged_at']:
            raise GitError('이미 Merge된 PR을 Close하지 않습니다.')
        if current['state']=='closed':
            return current
        return self.api(f'repos/{self.repo}/pulls/{pull["number"]}',method='PATCH',payload={'state':'closed'})
