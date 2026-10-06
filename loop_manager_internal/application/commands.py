"""application commands boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from contextlib import closing
from loop_manager_internal.application.contracts import ProjectRunKind
from loop_manager_internal.notifications import notifications
from loop_manager_internal.agents.agent_invocation import InvocationContext
from loop_manager_internal.config.managed_config import ManagedConfigError
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.runtime.contracts import RuntimeMutation
from loop_manager_internal.storage import agent_invocation_state, local_recovery_state
from pathlib import Path
import argparse
import sys
import threading


@dataclass(frozen=True)
class ConfigureRegistryEffects:
    """Named effects for configure_registry; no entry-point/module access."""
    add_registered_project: Callable[..., object]
    connect_database: Callable[..., object]
    load_managed_config: Callable[..., object]
    operation_print: Callable[..., object]


def configure_registry(
    effects: ConfigureRegistryEffects, args: argparse.Namespace,
    state_dir: Path,
    config_path: Path,
) -> int:
    if args.config_command == "init":
        effects.load_managed_config(config_path)
        with closing(effects.connect_database(state_dir, create=True)) as connection, connection:
            pass
        effects.operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0

    with closing(effects.connect_database(state_dir)) as connection, connection:
        if args.config_command == "list":
            projects = project_registry.list_projects(connection)
            if not projects:
                effects.operation_print("등록된 프로젝트가 없습니다.")
            for project in projects:
                approval = "legacy-approved" if project.agents_approved else "project"
                effects.operation_print(
                    f"{project.repo}\t{project.base_branch}\t"
                    f"{project.provider}\tAGENTS:{approval}"
                )
            return 0
        if args.config_command == "add":
            config = effects.load_managed_config(config_path)
            project = effects.add_registered_project(
                connection,
                Path(args.repository),
                config_path=config.path,
                provider=args.provider,
                requested_base=args.base,
                approve_agents_mismatch=args.approve_agents_mismatch,
                state_dir=state_dir,
            )
            approval = " (AGENTS.md 불일치 승인 저장)" if project.agents_approved else ""
            effects.operation_print(
                f"프로젝트 등록 완료: {project.repo} "
                f"({project.base_branch}, {project.provider}){approval}"
            )
            return 0
        repo = str(Path(args.repository).expanduser().resolve())
        project_registry.remove_project(connection, repo)
        effects.operation_print(f"프로젝트 등록 해제 완료: {repo}")
        return 0


@dataclass(frozen=True)
class DocumentDraftsCommandEffects:
    """Named effects for _document_drafts_command; no entry-point/module access."""
    ROOT: Path
    _coerce_agent_result: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    connect_database: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    operation_print: Callable[..., object]
    run_agent: Callable[..., object]


def _document_drafts_command(
    effects: DocumentDraftsCommandEffects, args: argparse.Namespace, state_dir: Path, config_path: Path,
) -> int:
    from loop_manager_internal.documents import document_drafts

    repository = Path(args.repository).expanduser().resolve()
    if not effects.is_loop_manager_repository(repository):
        raise document_drafts.DocumentDraftError(
            "일회성 문서 수정본은 Loop Manager 저장소 자체에서만 사용할 수 있습니다."
        )
    repo = str(repository)
    with closing(effects.connect_database(state_dir)) as connection, connection:
        project = connection.execute(
            "SELECT base_branch, provider FROM projects WHERE repo = ? AND registered = 1",
            (repo,),
        ).fetchone()
        if project is None:
            raise document_drafts.DocumentDraftError("등록된 Loop Manager 저장소가 아닙니다.")
        base_branch = str(project["base_branch"])
        drafts = document_drafts.DocumentDraftRepository(connection)

        def display_review(draft: document_drafts.DocumentDraft) -> None:
            sys.stdout.flush()
            output = sys.stdout.buffer
            output.write(document_drafts.review_artifact_bytes(draft))
            output.flush()

        if args.draft_command == "list":
            for path, findings in drafts.candidates(repo).items():
                effects.operation_print(f"candidate\t{path}\t{','.join(findings)}")
            for draft in drafts.list(repo):
                effects.operation_print(
                    f"{draft.state}\t{draft.draft_id}\t{draft.proposed_hash}\t"
                    f"{draft.target_path}"
                )
            return 0
        if args.draft_command == "show":
            draft = drafts.get(args.draft_id)
            if draft.repo != repo:
                raise document_drafts.DocumentDraftError("다른 저장소의 수정본입니다.")
            display_review(draft)
            return 0
        effects._recorded_base_fetch(
            repository, state_dir, base_branch, owner="project",
        )
        if args.draft_command == "generate":
            prompt, identity = drafts.generation_prompt(
                repo, repository, base_branch, args.target,
            )
            result = effects._coerce_agent_result(
                effects.run_agent(
                    str(project["provider"]), effects.ROOT, state_dir, config_path,
                    "planner", prompt, read_only=True,
                    connection=connection,
                    invocation=InvocationContext(repo, None, task_id='document-draft:' + str(identity)),
                ),
                None,
            )
            if result.code:
                raise document_drafts.DocumentDraftError(
                    "문서 수정본 Agent가 성공하지 못했습니다. 원본과 결정은 그대로입니다."
                )
            generated = document_drafts.parse_generated_draft(result.message)
            if generated is None:
                effects.operation_print("의미 보존을 확정하지 못해 수정본을 만들지 않았습니다.")
                return 0
            draft = drafts.create(
                repo, repository, base_branch, args.target, *generated,
                expected_identity=identity,
            )
            connection.commit()
            display_review(draft)
        elif args.draft_command == "create":
            content_file = Path(args.content_file).expanduser().resolve()
            if content_file.is_relative_to(repository):
                raise document_drafts.DocumentDraftError(
                    "수정본 입력 파일은 source checkout 밖에 두어야 합니다."
                )
            draft = drafts.create(
                repo, repository, base_branch, args.target,
                content_file.read_bytes(), args.meaning_evidence,
            )
            connection.commit()
            display_review(draft)
        elif args.draft_command == "decide":
            draft = drafts.get(args.draft_id)
            if draft.repo != repo:
                raise document_drafts.DocumentDraftError("다른 저장소의 수정본입니다.")
            draft = drafts.decide(
                args.draft_id, args.proposed_hash, args.decision,
                repository, base_branch,
                uncertain_reason=args.uncertain_reason,
            )
            connection.commit()
            effects.operation_print(
                f"{draft.state}\t{draft.draft_id}\t{draft.proposed_hash}\t"
                f"{draft.decision_revision}"
            )
            if draft.decision_reason:
                effects.operation_print(draft.decision_reason)
        else:
            for draft in drafts.approved(repo, repository, base_branch):
                effects.operation_print(
                    f"approved\t{draft.draft_id}\t{draft.proposed_hash}\t"
                    f"{draft.target_path}"
                )
    return 0


@dataclass(frozen=True)
class ExecuteRuntimeCommandEffects:
    """Named effects for execute_runtime_command; no entry-point/module access."""
    _document_drafts_command: Callable[..., object]
    configure_registry: Callable[..., object]
    connect_database: Callable[..., object]
    load_managed_config: Callable[..., object]
    operation_print: Callable[..., object]
    run_registered_projects: Callable[..., object]
    run_project_result: Callable[..., object]
    manual_command: Callable[..., object] | None = None


def _local_recovery_causes_remain(after: dict, plan_id: str | None) -> bool:
    if after["phase"] == "PROJECT_BLOCKED" or after["planning_error_sha256"]:
        return True
    relevant = [item for item in after["plans"] if plan_id is None or item["plan_id"] == plan_id]
    if any(item["status"] == "PLAN_BLOCKED" for item in relevant):
        return True
    relevant_blocks = [
        item for item in after["blocks"]
        if item["scope"] == "project"
        or plan_id is None or item["plan_id"] == plan_id
    ]
    if after["current_failures"]:
        return True
    if relevant_blocks:
        return True
    return False


def _local_recovery_in_progress(after: dict, plan_id: str | None) -> bool:
    active = {"IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING"}
    if plan_id is None:
        return any(item["status"] in active for item in after["plans"])
    return any(
        item["plan_id"] == plan_id and item["status"] in active
        for item in after["plans"]
    )


def recover_local_environment(
    effects: "LocalEnvironmentRecoveryCommandEffects",
    args: argparse.Namespace,
    state_dir: Path,
    config_path: Path,
) -> int:
    """Recheck one registered project under the normal process lock and recovery paths."""
    repository = str(Path(args.repository).expanduser().resolve())
    plan_id = args.plan
    with closing(effects.connect_database(state_dir)) as connection, connection:
        project = connection.execute(
            "SELECT provider FROM projects WHERE repo = ? AND registered = 1", (repository,),
        ).fetchone()
        if project is None:
            raise ValueError(f"등록된 프로젝트가 아닙니다: {repository}")
        if plan_id is not None and connection.execute(
            "SELECT 1 FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repository, plan_id),
        ).fetchone() is None:
            raise ValueError(f"등록 프로젝트에 없는 계획입니다: {plan_id}")

        request = local_recovery_state.begin_request(connection, repository, plan_id)
        try:
            config = effects.load_managed_config(config_path)
        except ManagedConfigError as error:
            raise ManagedConfigError(
                f"{error} 로컬 재검사를 위해 {config_path.expanduser().resolve()}를 수정한 뒤 "
                "같은 `recover` 명령을 다시 실행하세요."
            ) from error
        notifications.configure_session(
            connection, enabled=bool(config.telegram_token and config.telegram_chat_id),
        )
        connection.commit()
        outcome = effects.run_project_result(
            connection,
            state_dir,
            repository,
            config.path,
            None,
            False,
            str(project["provider"]),
            registered_only=True,
            planner_mutex=threading.Lock(),
            refactoring_review_interval=config.refactoring_review_interval,
            verify_timeout_seconds=config.verify_timeout_seconds,
            external_run_id=request.request_id,
            recovery_request_id=request.request_id,
            recovery_plan_id=plan_id,
        )
        after = local_recovery_state.snapshot(connection, repository, plan_id)
        unresolved = _local_recovery_causes_remain(after, plan_id)
        in_progress = _local_recovery_in_progress(after, plan_id)
        paused = outcome.kind not in {ProjectRunKind.COMPLETED, ProjectRunKind.WAITING}
        result_code = 1 if outcome.exit_code or paused or unresolved or in_progress else 0
        result = "resolved" if result_code == 0 else (
            "in_progress" if in_progress and not unresolved and not outcome.exit_code and not paused
            else "unresolved"
        )
        if result == "in_progress":
            local_recovery_state.record_progress(connection, request, after)
        else:
            local_recovery_state.complete_request(connection, request, result)
        request_action = "재개" if request.resumed else "요청"
        effects.operation_print(
            f"로컬 환경 재검사 {'완료' if result_code == 0 else '진행 중' if result == 'in_progress' else '미해결'}: "
            f"{repository}; 계획={plan_id or 'project'}; {request_action}={request.request_id}"
        )
        if result_code:
            from loop_manager_internal.execution.validation_feedback import redact_secrets
            for table, scope in (("project_blocks", "project"), ("execplan_blocks", "plan")):
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,),
                ).fetchone()
                if exists is None:
                    continue
                sql = f"SELECT plan_id, reason_code, evidence FROM {table} WHERE repo = ?"
                parameters: tuple[object, ...] = (repository,)
                if scope == "plan" and plan_id is not None:
                    sql += " AND plan_id = ?"
                    parameters += (plan_id,)
                for block in connection.execute(sql, parameters):
                    evidence = redact_secrets(str(block[2] or ""))
                    effects.operation_print(
                        f"남은 차단: {scope} {block[0] or repository} ({block[1]})\n{evidence}",
                        error=True,
                    )
            if outcome.detail:
                effects.operation_print(redact_secrets(str(outcome.detail)), error=True)
        return result_code


@dataclass(frozen=True)
class LocalEnvironmentRecoveryCommandEffects:
    """Explicit effects for recover; no entry-point/module access."""
    connect_database: Callable[..., object]
    load_managed_config: Callable[..., object]
    operation_print: Callable[..., object]
    run_project_result: Callable[..., object]


def execute_runtime_command(
    effects: ExecuteRuntimeCommandEffects, args: argparse.Namespace,
    state_dir: Path,
    config_path: Path,
    runtime_mutation: RuntimeMutation | None = None,
) -> int:
    if args.command == "recover":
        return recover_local_environment(
            LocalEnvironmentRecoveryCommandEffects(
                connect_database=effects.connect_database,
                load_managed_config=effects.load_managed_config,
                operation_print=effects.operation_print,
                run_project_result=effects.run_project_result,
            ),
            args,
            state_dir,
            config_path,
        )
    config = effects.load_managed_config(config_path)
    if args.command == "init":
        with closing(effects.connect_database(state_dir, create=True)) as connection, connection:
            pass
        effects.operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0
    if args.command == "config":
        return effects.configure_registry(args, state_dir, config_path)
    if args.command == "document-drafts":
        return effects._document_drafts_command(args, state_dir, config_path)
    if args.command == "manual":
        if effects.manual_command is None:
            raise RuntimeError('수동 명령 composition이 없습니다.')
        with closing(effects.connect_database(state_dir)) as connection, connection:
            agent_invocation_state.InvocationRepository(connection).reconcile_interrupted()
        return effects.manual_command(args, state_dir, config_path, runtime_mutation)
    if args.command == "maintenance-review":
        from uuid import uuid4
        from loop_manager_internal.maintenance import tiered_review
        repo = str(Path(args.repository).expanduser().resolve())
        with closing(effects.connect_database(state_dir)) as connection, connection:
            if not connection.execute("SELECT 1 FROM projects WHERE repo = ? AND registered = 1", (repo,)).fetchone():
                raise ValueError("전체 정밀 점검은 등록된 프로젝트만 요청할 수 있습니다.")
            tiered_review.ensure_schema(connection)
            connection.execute("INSERT INTO tiered_maintenance_requests VALUES (?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET request_id = excluded.request_id", (repo, uuid4().hex))
            connection.commit()
        effects.operation_print(f"전체 정밀 점검 요청 저장: {repo}; 다음 안전한 작업 선택 경계에서 실행합니다.")
        return 0
    with closing(effects.connect_database(state_dir)) as connection, connection:
        agent_invocation_state.InvocationRepository(connection).reconcile_interrupted()
        return effects.run_registered_projects(
            connection, state_dir, config,
            **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
        )
