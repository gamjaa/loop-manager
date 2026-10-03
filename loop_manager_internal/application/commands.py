"""application commands boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents.agent_invocation import InvocationContext
from loop_manager_internal.runtime import project_registry
from loop_manager_internal.runtime.contracts import RuntimeMutation
from loop_manager_internal.storage import agent_invocation_state
from pathlib import Path
import argparse
import sys


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
        with effects.connect_database(state_dir, create=True):
            pass
        effects.operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0

    with effects.connect_database(state_dir) as connection:
        if args.config_command == "list":
            projects = project_registry.list_projects(connection)
            if not projects:
                effects.operation_print("등록된 프로젝트가 없습니다.")
            for project in projects:
                approval = "approved" if project.agents_approved else "common"
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
    with effects.connect_database(state_dir) as connection:
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
                    invocation=InvocationContext(repo, None),
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


def execute_runtime_command(
    effects: ExecuteRuntimeCommandEffects, args: argparse.Namespace,
    state_dir: Path,
    config_path: Path,
    runtime_mutation: RuntimeMutation | None = None,
) -> int:
    config = effects.load_managed_config(config_path)
    if args.command == "init":
        with effects.connect_database(state_dir, create=True):
            pass
        effects.operation_print(f"상태 DB 초기화 완료: {state_dir / 'loop-manager.db'}")
        return 0
    if args.command == "config":
        return effects.configure_registry(args, state_dir, config_path)
    if args.command == "document-drafts":
        return effects._document_drafts_command(args, state_dir, config_path)
    if args.command == "maintenance-review":
        from uuid import uuid4
        from loop_manager_internal.maintenance import tiered_review
        repo = str(Path(args.repository).expanduser().resolve())
        with effects.connect_database(state_dir) as connection:
            if not connection.execute("SELECT 1 FROM projects WHERE repo = ? AND registered = 1", (repo,)).fetchone():
                raise ValueError("전체 정밀 점검은 등록된 프로젝트만 요청할 수 있습니다.")
            tiered_review.ensure_schema(connection)
            connection.execute("INSERT INTO tiered_maintenance_requests VALUES (?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET request_id = excluded.request_id", (repo, uuid4().hex))
            connection.commit()
        effects.operation_print(f"전체 정밀 점검 요청 저장: {repo}; 다음 안전한 작업 선택 경계에서 실행합니다.")
        return 0
    with effects.connect_database(state_dir) as connection:
        agent_invocation_state.InvocationRepository(connection).reconcile_interrupted()
        return effects.run_registered_projects(
            connection, state_dir, config,
            **({"runtime_mutation": runtime_mutation} if runtime_mutation is not None else {}),
        )
