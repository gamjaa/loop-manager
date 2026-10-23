#!/usr/bin/env python3
"""AI Agent가 Loop Manager의 다음 PR을 만들게 하는 부트스트랩 실행기."""

from __future__ import annotations

from pathlib import Path
import os
from loop_manager_internal.runtime import operation_events
from loop_manager_internal.application.compatibility import install_exports as _install_exports
from loop_manager_internal.config import execution_path
from loop_manager_internal.runtime import refresh
from loop_manager_internal.runtime.contracts import RuntimeRefresh

ROOT = Path(__file__).resolve().parent
_install_exports(__name__, ROOT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="기본 운영체제 상태 디렉터리 대신 사용할 경로")
    parser.add_argument("--config", default=str(ROOT / "config/config.ini"), help="config.ini 경로")
    parser.add_argument(
        "--runtime-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--runtime-source", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-sha", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-mutation-id", help=argparse.SUPPRESS)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help=argparse.SUPPRESS)

    configuration = subcommands.add_parser(
        "config", help="상태 DB와 프로젝트 registry를 명시적으로 관리"
    )
    config_commands = configuration.add_subparsers(
        dest="config_command", required=True
    )
    config_commands.add_parser("init", help="상태 DB 초기화")
    config_commands.add_parser("list", help="등록 프로젝트 목록 조회")
    task_limit = config_commands.add_parser("set", help="등록 프로젝트의 관리 대상 한도 설정")
    task_limit.add_argument("repository", help="등록된 저장소 루트")
    task_limit.add_argument("--max-tasks-per-project", type=int, required=True,
                            help="관리 대상 상한. 0이면 config 기본값 상속")
    status = subcommands.add_parser("status", help="프로젝트 관리 한도와 저장된 관리 대상 수 조회")
    status.add_argument("repository", help="등록된 저장소 루트")
    status.add_argument("--json", action="store_true", help="조회 결과를 JSON으로 출력")
    add = subcommands.add_parser("add", help="프로젝트 검증 및 등록")
    add.add_argument("repository", help="대상 Git 저장소 루트")
    add.add_argument("--base", help="기준 branch. 생략하면 origin/HEAD 사용")
    add.add_argument(
        "--provider",
        choices=sorted(AGENT_PROVIDERS),
        default="codex",
        help="프로젝트 Agent provider (기본값: codex)",
    )
    add.add_argument(
        "--approve-agents-mismatch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    remove = subcommands.add_parser("remove", help="프로젝트 등록 해제")
    remove.add_argument("repository", help="등록 해제할 저장소 루트")

    drafts = subcommands.add_parser(
        "document-drafts", help="Loop Manager 전수 평가 문서 수정본 검토와 결정"
    )
    drafts.add_argument("repository", help="등록된 Loop Manager 저장소 루트")
    draft_commands = drafts.add_subparsers(dest="draft_command", required=True)
    draft_commands.add_parser("list", help="문서 후보와 저장된 수정본 조회")
    generate_draft = draft_commands.add_parser("generate", help="평가 발견에서 읽기 전용 수정본 생성")
    generate_draft.add_argument("target", help="평가 결과의 활성 문서 경로")
    create_draft = draft_commands.add_parser("create", help="외부 파일의 수정본 저장")
    create_draft.add_argument("target", help="평가 결과의 활성 문서 경로")
    create_draft.add_argument("content_file", help="원본 checkout 밖의 UTF-8 수정본 파일")
    create_draft.add_argument("--meaning-evidence", required=True,
                              help="기존 의미가 보존된 구체적인 근거")
    show_draft = draft_commands.add_parser("show", help="전체 원본·수정본·diff 조회")
    show_draft.add_argument("draft_id")
    decide_draft = draft_commands.add_parser("decide", help="정확한 수정본 승인 또는 거절")
    decide_draft.add_argument("draft_id")
    decide_draft.add_argument("proposed_hash", help="show의 Proposed SHA256")
    decide_draft.add_argument("decision", choices=("approve", "reject"))
    decide_draft.add_argument("--uncertain-reason", default="",
                              help="의미 보존이 불확실하면 승인 대신 새 검토 요구")
    draft_commands.add_parser("approved", help="현재 원본에 유효한 승인 조회")

    manual = subcommands.add_parser('manual', help='수동 G 구현 시작·완료와 원격 R·코드 제출')
    manual_commands = manual.add_subparsers(dest='manual_command', required=True)
    for name in ('start', 'complete', 'submit'):
        command = manual_commands.add_parser(name)
        command.add_argument('repository', help='등록된 Git 저장소 루트')
        if name == 'submit':
            command.add_argument('--branch', required=True, help='읽기 입력 원격 branch')
            command.add_argument('--requirement', action='append', required=True, help='제출 commit의 R 문서 경로')
        else:
            command.add_argument('plan_id', help='정확한 G ID')
        if name != 'start':
            command.add_argument('--skip-verify', action='store_true', help='대상 후보의 verify 생략')
            command.add_argument('--skip-review', action='store_true', help='대상 후보의 Reviewer 검토 생략')
            command.add_argument('--expected-head', help='생략 시 필요한 원격 commit 전체 SHA')
            command.add_argument('--skip-reason', help='생략 옵션의 선택 사유. 생략·빈 값·공백은 결과에 사유 미입력으로 표시')

    maintenance = subcommands.add_parser("maintenance-review", help="등록 프로젝트의 전체 정밀 점검을 다음 안전한 경계에 요청")
    maintenance.add_argument("repository", help="등록된 Git 저장소 루트")

    recover = subcommands.add_parser(
        "recover", help="등록 프로젝트의 로컬 환경 원인을 제한해 재검사",
    )
    recover.add_argument("repository", help="재검사할 등록 저장소 루트")
    recover.add_argument("--plan", help="이 프로젝트 안의 특정 ExecPlan만 재검사")

    reset = subcommands.add_parser('reset', help='원격 branch 삭제로 중단된 자동 작업의 명시적 재시작')
    reset.add_argument('repository', help='등록된 Git 저장소 루트')
    reset.add_argument('plan_id', help='삭제로 중단된 ExecPlan ID')

    run = subcommands.add_parser("run", help="등록 프로젝트의 다음 작업을 처리")
    run.add_argument(
        "--skip-if-running",
        action="store_true",
        help="다른 Loop Manager가 실행 중이면 스케줄러용으로 조용히 성공 종료",
    )
    run.add_argument(
        "repositories",
        nargs="*",
        help="처리할 등록 프로젝트 경로. 생략하면 전체 프로젝트를 처리",
    )
    return parser


def main() -> int:
    run_id = os.environ.get("LOOP_MANAGER_RUN_ID") if "--runtime-child" in sys.argv else None
    with operation_events.log_context(run=run_id or secrets.token_hex(16), stage="COMMAND",
                            result_output="--json" in sys.argv or "--raw" in sys.argv):
        raw_arguments = sys.argv[1:]
        args = build_parser().parse_args(raw_arguments)
        if args.command == 'manual' and args.manual_command != 'start':
            try:
                manual_work.validate_options(args)
            except ValueError as error:
                build_parser().error(str(error))
        state_dir = state_directory(args.state_dir)
        config_path = source_config_path(ROOT, args.config)
        try:
            if args.command == 'run' and refresh.paused(state_dir):
                operation_log('전체 pause: 실행 상태를 보존합니다.')
                return 0
            if args.command == "status" or args.command == "config" and args.config_command == "set":
                return configure_registry(args, state_dir, config_path)
            if args.command == "run" and not args.runtime_child:
                raw_arguments = ["run"]
                if args.skip_if_running:
                    raw_arguments.append("--skip-if-running")
                if args.repositories:
                    raw_arguments.append("--")
                    raw_arguments.extend(
                        str(Path(repository).expanduser().resolve())
                        for repository in args.repositories
                    )
            if not args.runtime_child:
                execution_path.apply_execution_path(ROOT)
            if args.runtime_child:
                if not args.runtime_source or not args.runtime_sha:
                    raise RuntimeError("runtime 자식 소유권 정보가 없습니다.")
                source = Path(args.runtime_source).expanduser().resolve()
                concurrent = args.command in {"manual", "recover"}
                runtime_options = {"concurrent": True} if concurrent else {}
                child_options = {"command_id": args.runtime_mutation_id} if concurrent else {}
                validate_runtime_child(source, state_dir, str(args.runtime_sha), **child_options)
                runtime_mutation = validate_runtime_mutation(
                    source, state_dir, str(args.runtime_sha), args.runtime_mutation_id, **runtime_options,
                )
                # Mutation validation binds the selected revision and verifies any
                # later live base advance; equality here would reject that proof.
                handoff_token = os.environ.get('LOOP_MANAGER_RUNTIME_HANDOFF_TOKEN')
                channel_token = refresh.channel.set((handoff_token, operation_events.current_context()['run'])
                    if handoff_token else None)
                try:
                    return execute_runtime_command(args, state_dir, config_path, runtime_mutation)
                finally:
                    refresh.channel.reset(channel_token)

            source = ROOT
            mutation_id = secrets.token_hex(16)
            concurrent = args.command in {"manual", "recover"}
            lock_options = {"name": f"manual-{mutation_id}.lock"} if concurrent else {}
            runtime_options = {"concurrent": True} if concurrent else {}
            with process_lock(state_dir, identity=f"bootstrap:{mutation_id}", **lock_options) as lock_handle:
                request = None
                seen = set()
                while True:
                    if args.command == 'run' and refresh.paused(state_dir):
                        return 0
                    with operation_events.log_context(stage="BOOTSTRAP"), operation_events.activity("runtime 준비", heartbeat=True) as report:
                        runtime = prepare_bootstrap_runtime(source, state_dir, mutation_id, **runtime_options)
                        report.update(result="준비 완료", runtime_sha=runtime.commit_sha)
                    if request is not None:
                        _bootstrap_service().validate_refresh(source, state_dir, request, runtime.commit_sha)
                        if runtime.commit_sha in seen:
                            raise RuntimeError('같은 revision의 runtime 재시작을 거부합니다.')
                    if args.command == 'run' and refresh.paused(state_dir):
                        return 0
                    seen.add(runtime.commit_sha)
                    _write_process_lock_identity(lock_handle, f"runtime:{mutation_id}")
                    result = run_runtime_child(source, state_dir, config_path, runtime, raw_arguments)
                    if not isinstance(result, RuntimeRefresh):
                        return result
                    request = result
                    mutation_id = secrets.token_hex(16)
                    _write_process_lock_identity(lock_handle, f"bootstrap:{mutation_id}")
        except ProcessLockBusyError as error:
            if args.command == "run" and getattr(args, "skip_if_running", False):
                return 0
            operation_log(f"오류: {error}", error=True)
            return 1
        except refresh.RuntimePaused:
            operation_log('사용자 pause: 실행 상태를 보존합니다.')
            return 0
        except refresh.RuntimeDrainUncertain as error:
            operation_log(str(error), error=True)
            return refresh.UNSETTLED_EXIT
        except (
            ManagedConfigError,
            project_registry.ProjectRegistryError,
            RuntimeError,
            OSError,
            sqlite3.DatabaseError,
            json.JSONDecodeError,
        ) as error:
            operation_log(f"오류: {error}", error=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
