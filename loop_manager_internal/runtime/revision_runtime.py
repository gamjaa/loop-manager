"""원격 실행 코드의 실제 변화만 감지하고 안전한 반환 뒤 runtime을 인계한다."""
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import subprocess
import sys

from loop_manager_internal.git.repository import command,GitError


HANDOFF_EXIT=75
RUNTIME_MARKER='REWRITE_RUNTIME_VERSION = 1'


def execution_input(path):
    parsed=PurePosixPath(path)
    return (path in ('loop_manager.py','requirements.txt','run.sh','run.ps1')
            or parsed.parts[0] in ('loop_manager_internal','scripts','.agents')
            or path.startswith('docs/AGENTS.') and path.endswith('.md')
            or path.startswith('docs/') and path.endswith('.template.md')
            or path=='docs/PLANS.md')


def revision_fingerprint(repository,revision):
    records=repository.git('ls-tree','-rz','--full-tree',revision)
    digest=hashlib.sha256()
    for record in records.split('\0'):
        if not record:
            continue
        _,path=record.split('\t',1)
        if execution_input(path):
            digest.update(record.encode())
            digest.update(b'\0')
    return digest.hexdigest()


def prepare(repository,state_dir,revision):
    if not re.fullmatch(r'[0-9a-f]{40,64}',revision):
        raise ValueError('runtime commit SHA가 올바르지 않습니다.')
    root=Path(state_dir).resolve()/'runtimes'
    root.mkdir(parents=True,exist_ok=True)
    path=root/revision
    if not path.exists():
        command(['git','clone','--no-checkout','--no-hardlinks','--',repository.remote,str(path)])
        repository.git('checkout','--detach',revision,cwd=path)
    if (repository.git('remote','get-url','origin',cwd=path)!=repository.remote
            or repository.head(path)!=revision or repository.dirty(path)):
        raise GitError('선택 runtime의 저장소·HEAD·작업 내용이 일치하지 않습니다.')
    entry=path/'loop_manager.py'
    if RUNTIME_MARKER not in entry.read_text(encoding='utf-8'):
        raise GitError('선택 원격 runtime은 신규 DB 실행 계약을 지원하지 않습니다.')
    for file in [entry,*(path/'loop_manager_internal').rglob('*.py')]:
        compile(file.read_bytes(),str(file),'exec')
    return path


class Refresh:
    def __init__(self,repository,running_revision):
        self.repository=repository
        self.running_revision=running_revision
        self.fingerprint=revision_fingerprint(repository,running_revision)
        self.last_observed=running_revision
        self.target=None

    def boundary(self):
        revision=self.repository.refresh()
        if revision==self.last_observed:
            return self.target
        self.last_observed=revision
        if revision_fingerprint(self.repository,revision)!=self.fingerprint:
            self.target=revision
        return self.target


def write_handoff(path,token,running,target):
    path=Path(path)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'token':token,'running':running,'target':target}),encoding='utf-8')
    temporary.replace(path)


def consume_handoff(path,token,running):
    path=Path(path)
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('token')!=token or value.get('running')!=running or not re.fullmatch(r'[0-9a-f]{40,64}',value.get('target','')):
        raise GitError('runtime 인계 영수증이 현재 실행과 일치하지 않습니다.')
    path.unlink()
    return value['target']
