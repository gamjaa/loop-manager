"""프로젝트가 정의한 Verify 명령과 후보별 검증 입력."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys

from loop_manager_internal.execution.processes import run
from loop_manager_internal.execution.validation_feedback import bounded_diagnostic


@dataclass(frozen=True)
class VerifyCommand:
    argv: list
    cwd: Path
    environment: dict
    timeout: float
    fingerprint: str
    reusable: bool


def verify_command(worktree,timeout):
    root = Path(worktree).resolve()
    manifest = root/'scripts/verify.json'
    values = json.loads(manifest.read_text(encoding='utf-8')) if manifest.is_file() else {}
    if values.get('version')==2:
        platform = 'windows' if os.name=='nt' else 'posix'
        argv = values['commands'][platform]
        if not isinstance(argv,list) or not argv or any(not isinstance(item,str) or not item for item in argv):
            raise ValueError('Verify command는 문자열 argv 목록이어야 합니다.')
        argv = [sys.executable if item=='{python}' else item for item in argv]
        cwd = (root/values.get('cwd','.')).resolve()
        if not cwd.is_relative_to(root) or not cwd.is_dir():
            raise ValueError('Verify cwd는 프로젝트 안의 디렉터리여야 합니다.')
        supplied = values.get('environment',{})
        if not isinstance(supplied,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in supplied.items()):
            raise ValueError('Verify environment는 문자열 key/value여야 합니다.')
        declared_inputs = values.get('inputs')
    else:
        script = root/'scripts'/('verify.ps1' if os.name=='nt' else 'verify.sh')
        if not script.is_file():
            raise ValueError(f'필수 Verify 스크립트가 없습니다: {script}')
        argv = ['pwsh','-NoProfile','-File',str(script)] if os.name=='nt' else ['bash',str(script)]
        cwd,supplied,declared_inputs = root,{},None
    environment = dict(os.environ,**supplied)
    fingerprint = hashlib.sha256(json.dumps([argv,str(cwd),environment,values],sort_keys=True).encode())
    reusable = isinstance(declared_inputs,list) and bool(declared_inputs)
    if reusable:
        files = set()
        for pattern in declared_inputs:
            if not isinstance(pattern,str) or Path(pattern).is_absolute() or '..' in Path(pattern).parts:
                raise ValueError('Verify input 경로가 올바르지 않습니다.')
            matches = list(root.glob(pattern))
            if not matches:
                reusable = False
            for path in matches:
                if path.is_dir():
                    files.update(item for item in path.rglob('*') if item.is_file())
                elif path.is_file():
                    files.add(path)
        for path in sorted(files):
            if not path.resolve().is_relative_to(root):
                raise ValueError('Verify input이 프로젝트 밖을 가리킵니다.')
            fingerprint.update(str(path.relative_to(root)).encode())
            with path.open('rb') as stream:
                while block := stream.read(1024*1024):
                    fingerprint.update(block)
    return VerifyCommand(argv,cwd,environment,timeout,fingerprint.hexdigest(),reusable)


def fingerprint(repository,plan,command,instruction_hash):
    value = [repository.head(plan['worktree']),plan['source_oid'],plan['sources'],
             command.fingerprint,instruction_hash]
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def verify(specification,**callbacks):
    result = run(specification.argv,specification.cwd,specification.timeout,environment=specification.environment,**callbacks)
    evidence = f'종료 코드={result.code}, timeout={result.timed_out}\n'+bounded_diagnostic(result.output)
    return result.code==0 and not result.timed_out,evidence
