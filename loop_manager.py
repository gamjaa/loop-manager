#!/usr/bin/env python3
"""AI Agent가 Loop Manager의 다음 PR을 만들게 하는 부트스트랩 실행기."""

from __future__ import annotations

from pathlib import Path
from loop_manager_internal.application.compatibility import install_exports as _install_exports
from loop_manager_internal.config import execution_path

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
            command.add_argument('--skip-verify', action='store_true')
            command.add_argument('--skip-review', action='store_true')
            command.add_argument('--expected-head')
            command.add_argument('--skip-reason')

    maintenance = subcommands.add_parser("maintenance-review", help="등록 프로젝트의 전체 정밀 점검을 다음 안전한 경계에 요청")
    maintenance.add_argument("repository", help="등록된 Git 저장소 루트")

    recover = subcommands.add_parser(
        "recover", help="등록 프로젝트의 로컬 환경 원인을 제한해 재검사",
    )
    recover.add_argument("repository", help="재검사할 등록 저장소 루트")
    recover.add_argument("--plan", help="이 프로젝트 안의 특정 ExecPlan만 재검사")

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
            return execute_runtime_command(
                args, state_dir, config_path, runtime_mutation,
            )

        source = ROOT
        mutation_id = secrets.token_hex(16)
        concurrent = args.command in {"manual", "recover"}
        lock_options = {"name": f"manual-{mutation_id}.lock"} if concurrent else {}
        runtime_options = {"concurrent": True} if concurrent else {}
        with process_lock(state_dir, identity=f"bootstrap:{mutation_id}", **lock_options) as lock_handle:
            runtime = prepare_bootstrap_runtime(source, state_dir, mutation_id, **runtime_options)
            _write_process_lock_identity(lock_handle, f"runtime:{mutation_id}")
            return run_runtime_child(
                source,
                state_dir,
                config_path,
                runtime,
                raw_arguments,
            )
    except ProcessLockBusyError as error:
        if args.command == "run" and getattr(args, "skip_if_running", False):
            return 0
        operation_log(f"오류: {error}", error=True)
        return 1
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
