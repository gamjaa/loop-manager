"""즉시 CLI와 원격 runtime 실행의 조립 경계."""
import argparse
import configparser
from pathlib import Path
import sqlite3
import sys

from loop_manager_internal.application import direct_commands
from loop_manager_internal.config import execution_path
from loop_manager_internal.execution.validation_feedback import redact_secrets
from loop_manager_internal.runtime.process_lock import state_directory
from loop_manager_internal.storage.state import Store,StateError


def main(argv=None, *, root=None):
    root=Path(root or Path(__file__).resolve().parents[2]).resolve()
    parser=direct_commands.build_parser()
    for name in ('runtime-origin','runtime-base','runtime-sha','runtime-token','runtime-lock'):
        parser.add_argument('--'+name,help=argparse.SUPPRESS)
    parser.add_argument('--runtime-child',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    state=state_directory(args.state_dir)
    store=Store(state/'loop-manager.db')
    configuration=Path(args.config).expanduser().resolve() if args.config else root/'config/config.ini'
    try:
        if args.command=='run' or args.command=='manual' and args.manual_command=='start':
            from loop_manager_internal.runtime import launcher
            if args.runtime_child:
                return launcher.child(args,root,store,state,configuration)
            if store.paused('*'):
                if args.command=='run':
                    return 0
                raise StateError('전체 일시중지 상태입니다. resume 후 수동 준비하세요.')
            if args.command=='manual':
                registered=direct_commands.project(store,args.repository)
                if not registered['enabled']:
                    raise StateError('등록이 해제된 프로젝트는 수동 준비할 수 없습니다.')
                if store.paused(registered['repo']):
                    raise StateError('프로젝트 일시중지 상태입니다. resume 후 수동 준비하세요.')
            elif args.repositories:
                selected={direct_commands.project(store,value)['repo'] for value in args.repositories}
                if all(store.paused(repo) for repo in selected):
                    return 0
            execution_path.apply_execution_path(root)
            return launcher.launch(args,root,store,state,configuration)
        if args.command=='add':
            execution_path.apply_execution_path(root)
        direct_commands.output(direct_commands.execute(args,store,state),store)
        return 0
    except (OSError,RuntimeError,ValueError,sqlite3.DatabaseError,configparser.Error) as error:
        print('오류: '+redact_secrets(str(error)),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
