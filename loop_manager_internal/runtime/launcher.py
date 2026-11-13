"""원격 runtime을 선택하고 자식 실행·안전 인계를 감독한다."""
import os
from pathlib import Path
import secrets
import subprocess
import sys

from loop_manager_internal.application import direct_commands
from loop_manager_internal.application.coordinator import Coordinator
from loop_manager_internal.config.settings import Settings
from loop_manager_internal.git.repository import Repository,command,github_repository
from loop_manager_internal.runtime.process_lock import process_lock,_held_process_lock_identity
from loop_manager_internal.runtime.contracts import ProcessLockBusyError
from loop_manager_internal.runtime import revision_runtime


def own_project(root,store):
    remote=command(['git','remote','get-url','origin'],cwd=root).stdout.strip()
    repo=github_repository(remote)
    registered=store.rows('SELECT * FROM projects WHERE repo=? AND enabled=1',(repo,))
    if registered:
        return registered[0]
    observation=command(['git','ls-remote','--symref',remote,'HEAD'],cwd=root).stdout
    base=next((line.split()[1].removeprefix('refs/heads/') for line in observation.splitlines()
               if line.startswith('ref: refs/heads/')),None)
    if not base:
        raise RuntimeError('자신의 원격 base를 확인할 수 없습니다. base를 명시해 등록하세요.')
    return {'repo':repo,'remote_url':remote,'base_branch':base,'provider':'codex','path':str(root)}


def launch(args,root,store,state,configuration):
    token=secrets.token_hex(16)
    lock_name='bootstrap.lock' if args.command=='run' else f'bootstrap-manual-{token}.lock'
    try:
        with process_lock(state,identity=token,name=lock_name):
            repository=Repository(own_project(root,store),state,store)
            revision=repository.refresh()
            seen=set()
            receipt=state/f'handoff-{token}.json'
            try:
                while True:
                    if revision in seen:
                        raise RuntimeError('같은 runtime으로 반복 인계하지 않습니다.')
                    seen.add(revision)
                    with process_lock(state,name=f'runtime-{revision}.lock'):
                        runtime=revision_runtime.prepare(repository,state,revision)
                    argv=[sys.executable,'-u',str(runtime/'loop_manager.py'),
                          '--state-dir',str(state),'--config',str(configuration),'--runtime-child',
                          '--runtime-origin',repository.remote,'--runtime-base',repository.base,
                          '--runtime-sha',revision,'--runtime-token',token,'--runtime-lock',lock_name]
                    if args.command=='run':
                        argv+=['run',*(['--skip-if-running'] if args.skip_if_running else []),*args.repositories]
                    else:
                        argv+=['manual','start',args.repository,args.plan_id]
                    result=subprocess.run(argv,env=dict(os.environ,LOOP_MANAGER_HANDOFF_PATH=str(receipt)),check=False)
                    if result.returncode!=revision_runtime.HANDOFF_EXIT:
                        return result.returncode
                    revision=revision_runtime.consume_handoff(receipt,token,revision)
            finally:
                receipt.unlink(missing_ok=True)
    except ProcessLockBusyError:
        if args.command=='run' and args.skip_if_running:
            return 0
        raise


def child(args,root,store,state,configuration):
    if not all((args.runtime_origin,args.runtime_base,args.runtime_sha,args.runtime_token,args.runtime_lock)):
        raise RuntimeError('runtime 실행 식별 정보가 없습니다.')
    expected=(state/'runtimes'/args.runtime_sha).resolve()
    if root!=expected:
        raise RuntimeError('선택한 runtime 경로가 일치하지 않습니다.')
    if _held_process_lock_identity(state,name=args.runtime_lock)!=args.runtime_token:
        raise RuntimeError('runtime bootstrap 실행 잠금이 일치하지 않습니다.')
    project={'repo':github_repository(args.runtime_origin),'remote_url':args.runtime_origin,
             'base_branch':args.runtime_base,'provider':'codex','path':str(root)}
    repository=Repository(project,state,store)
    if repository.head(root)!=args.runtime_sha or repository.git('remote','get-url','origin',cwd=root)!=args.runtime_origin:
        raise RuntimeError('runtime 저장소 또는 commit이 일치하지 않습니다.')
    if args.command=='manual':
        direct_commands.output(direct_commands.execute(args,store,state),store)
        return 0
    selected=None
    if args.repositories:
        selected={direct_commands.project(store,value)['repo'] for value in args.repositories}
    try:
        with process_lock(state,identity='runtime:'+args.runtime_token):
            refresh=revision_runtime.Refresh(repository,args.runtime_sha)
            parent_lost=False
            def boundary():
                nonlocal parent_lost
                try:
                    parent_lost=_held_process_lock_identity(state,name=args.runtime_lock)!=args.runtime_token
                except RuntimeError:
                    parent_lost=True
                return args.runtime_sha if parent_lost else refresh.boundary()
            coordinator=Coordinator(store,state,root,Settings(configuration),runtime_boundary=boundary)
            errors=coordinator.run(selected)
            for error in errors:
                print('보류: '+str(error),file=sys.stderr)
            if coordinator.handoff_target and not parent_lost:
                receipt=Path(os.environ.get('LOOP_MANAGER_HANDOFF_PATH','')).resolve()
                if receipt!=state/f'handoff-{args.runtime_token}.json':
                    raise RuntimeError('runtime 인계 경로가 일치하지 않습니다.')
                revision_runtime.write_handoff(receipt,args.runtime_token,args.runtime_sha,coordinator.handoff_target)
                return revision_runtime.HANDOFF_EXIT
            return 1 if errors else 0
    except ProcessLockBusyError:
        if args.skip_if_running:
            return 0
        raise
