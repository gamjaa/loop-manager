"""Execution handlers; all external operations arrive through effect ports."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any
from pathlib import Path
import sys
from scoped_blocking import BlockReasonCode
from execution_runtime import (ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind,
    ExecutionPhase, ExecutionPlanMetadata, LatestBaseCheckpoint)

@dataclass(frozen=True)
class WorkspaceEffects:
    is_directory: Callable[..., Any]
    git: Callable[..., Any]
    make_worktree: Callable[..., Any]
    branch_has_changes: Callable[..., Any]
    remote_branch_oid: Callable[..., Any]
    _rebase_in_progress_for_branch: Callable[..., Any]
    validate_execplan_workspace_identity: Callable[..., Any]
    validate_managed_worktree: Callable[..., Any]
    _plan_id_from_path: Callable[..., Any]
    reserve_execplan_workspace: Callable[..., Any]
    mark_execplan_workspace_owned: Callable[..., Any]
    resume_pr_conflict_rebase: Callable[..., Any]

@dataclass(frozen=True)
class AgentEffects:
    begin_agent_scope: Callable[..., Any]
    capture_git_snapshot: Callable[..., Any]
    clear_agent_scope: Callable[..., Any]
    validate_agent_scope: Callable[..., Any]
    validate_worker_workspace_scope: Callable[..., Any]
    _worktree_execplan_input_fingerprint: Callable[..., Any]
    _guidance_worker_targets: Callable[..., Any]
    guidance_resume_context: Callable[..., Any]
    _published_plan_allows_common_plans: Callable[..., Any]
    is_loop_manager_repository: Callable[..., Any]
    inspect_worker_guidance_result: Callable[..., Any]
    validate_guidance_publication: Callable[..., Any]
    run_agent: Callable[..., Any]
    run_agent_with_retries: Callable[..., Any]
    worker_prompt: Callable[..., Any]
    reviewer_prompt: Callable[..., Any]

@dataclass(frozen=True)
class ValidationEffects:
    verify: Callable[..., Any]
    parse_worker_result: Callable[..., Any]
    parse_reviewer_result: Callable[..., Any]
    validate_active_execplan_metadata: Callable[..., Any]
    ensure_worker_level_state: Callable[..., Any]
    _deserialize_failure_history: Callable[..., Any]
    clear_validation_failure_history: Callable[..., Any]
    transition_validation_failure: Callable[..., Any]

@dataclass(frozen=True)
class ArchiveEffects:
    requirement_archive_sources: Callable[..., Any]
    requirement_archive_candidates: Callable[..., Any]
    requirement_archive_move_set: Callable[..., Any]
    requirement_archive_reconciliation_error: Callable[..., Any]
    requirement_archive_touched_sources: Callable[..., Any]

@dataclass(frozen=True)
class PublicationEffects:
    pr_for_branch: Callable[..., Any]
    reconcile_existing_pull_request: Callable[..., Any]
    record_remote_publication_branch: Callable[..., Any]
    push_managed_publication_branch: Callable[..., Any]
    push_owned_pr_conflict_recovery: Callable[..., Any]
    published_execplan: Callable[..., Any]
    build_pr_body: Callable[..., Any]
    command: Callable[..., Any]

@dataclass(frozen=True)
class BlockingEffects:
    block_active_execplan: Callable[..., Any]
    block_active_execplan_from_revision: Callable[..., Any]

@dataclass(frozen=True)
class EventsEffects:
    operation_log: Callable[..., Any]
    operation_print: Callable[..., Any]

@dataclass(frozen=True)
class ExecutionEffects:
    state: Any
    contract: Any
    workspace: WorkspaceEffects
    agent: AgentEffects
    validation: ValidationEffects
    archive: ArchiveEffects
    publication: PublicationEffects
    blocking: BlockingEffects
    events: EventsEffects


class ExecutionHandlers:
    """Bind phase coordination to existing policy helpers and effect adapters."""

    def __init__(self, effects, *, repo, repo_path, state_dir, base, base_sha, provider, config_path, newly_activated):
        self.effects = effects
        self.repo = repo
        self.repo_path = repo_path
        self.state_dir = state_dir
        self.base = base
        self.base_sha = base_sha
        self.provider = provider
        self.config_path = config_path
        self.newly_activated = newly_activated
        self.catalog_plan = None
        self.plan = None
        self.level = 1
        self.archive_sources = ()
        self.archive_candidates = ()
        self.archive_scope_sources = ()
        self._latest_verify_output = ''
        self._worker_publication = None

    def _row(self):
        return self.effects.state.row()

    def _context(self):
        row = self._row()
        catalog_plan = self.effects.state.active_execplan(self.repo)
        if catalog_plan is None or self.plan is None:
            raise RuntimeError('활성 ExecPlan 실행 context를 복구할 수 없습니다.')
        self.catalog_plan = catalog_plan
        phase = ExecutionPhase(str(catalog_plan['status']))
        return ExecutionContext(
            repo=self.repo,
            repo_path=self.repo_path,
            state_dir=self.state_dir,
            base_branch=self.base,
            base_sha=self.base_sha,
            provider=self.provider,
            config_path=self.config_path,
            plan=ExecutionPlanMetadata(
                plan_id=str(self.plan['id']),
                title=str(self.plan['title']),
                path=str(row['plan_path']),
                plan_sha=str(row['plan_sha']) if row['plan_sha'] is not None else None,
                sources=tuple((str(item) for item in self.plan.get('sources', ()))),
            ),
            phase=phase,
            branch=str(row['branch'] or ''),
            worktree=Path(str(row['worktree'] or '')),
            active_sha=str(row['active_sha'] or ''),
            validated_base_sha=str(row['validated_base_sha']) if row['validated_base_sha'] is not None else None,
            worker_session_id=str(row['worker_session_id']) if row['worker_session_id'] is not None else None,
            reviewer_session_id=str(row['reviewer_session_id']) if row['reviewer_session_id'] is not None else None,
            worker_result=str(row['worker_result']) if row['worker_result'] is not None else None,
            verify_result=str(row['verify_result']) if row['verify_result'] is not None else None,
            review_result=str(row['review_result']) if row['review_result'] is not None else None,
            pr_url=str(row['pr_url']) if row['pr_url'] is not None else None,
            pr_conflict_recovery=bool(row['pr_conflict_recovery']),
            pr_conflict_base_oid=str(row['pr_conflict_base_oid']) if row['pr_conflict_base_oid'] is not None else None,
            resolving_rebase=self._resolving_rebase(row),
            archive_sources=self.archive_sources,
        )

    @staticmethod
    def _final(detail=None):
        return ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK, detail=detail, error=True)

    @staticmethod
    def _retry(detail=None):
        return ExecutionOutcome(ExecutionOutcomeKind.RETRY, detail=detail, error=detail is not None)

    def _step_outcome(self, step_result):
        kind = {0: ExecutionOutcomeKind.SUCCESS, self.effects.contract.PROJECT_STEP_RETRY: ExecutionOutcomeKind.RETRY, self.effects.contract.PROJECT_STEP_TRANSIENT_PAUSE: ExecutionOutcomeKind.TRANSIENT_PAUSE, self.effects.contract.PROJECT_STEP_USAGE_PAUSE: ExecutionOutcomeKind.USAGE_PAUSE}.get(
            step_result,
            ExecutionOutcomeKind.FINAL_BLOCK,
        )
        return ExecutionOutcome(kind, error=kind is ExecutionOutcomeKind.FINAL_BLOCK)

    def _advanced(self):
        return ExecutionOutcome.advanced(self._context())

    def _resolving_rebase(self, row):
        rebase_error = str(row['last_error'] or '')
        rebase_recovery = bool(
            row['phase'] == 'WORKING' and (rebase_error == self.effects.contract.REBASE_IN_PROGRESS_ERROR or rebase_error.startswith(self.effects.contract.REBASE_CONFLICT_PREFIX)),
        )
        worktree = Path(str(row['worktree'] or ''))
        return bool(
            rebase_recovery and self.effects.workspace.is_directory(worktree) and self.effects.workspace._rebase_in_progress_for_branch(worktree, str(row['branch'] or '')),
        )

    def prepare(self):
        row = self._row()
        branch = str(row['branch'] or '')
        worktree = Path(str(row['worktree'] or ''))
        plan_id = self.effects.workspace._plan_id_from_path(str(row['plan_path'] or ''))
        catalog_plan = self.effects.state.active_execplan(self.repo)
        workspace_ownership = int(catalog_plan['workspace_owned'] if catalog_plan else 0)
        workspace_owned = workspace_ownership == self.effects.contract.WORKSPACE_OWNED
        workspace_reserved = workspace_ownership == self.effects.contract.WORKSPACE_RESERVED
        resolving_rebase = self._resolving_rebase(row)
        try:
            if plan_id is None:
                raise self.effects.contract.WorkspaceOwnershipError('활성 ExecPlan 경로에서 ID를 확인할 수 없습니다.')
            if catalog_plan is None or catalog_plan['plan_id'] != plan_id:
                raise self.effects.contract.WorkspaceOwnershipError('활성 ExecPlan의 catalog 상태를 확인할 수 없습니다.')
            self.effects.workspace.validate_execplan_workspace_identity(
                self.state_dir,
                self.repo,
                plan_id,
                branch,
                worktree,
            )
            self.effects.workspace.make_worktree(
                self.repo_path,
                worktree,
                branch,
                self.base,
                allow_create_branch=self.newly_activated and (not workspace_owned) and (not workspace_reserved),
                allow_recovery=workspace_owned or workspace_reserved,
                allow_existing=workspace_owned or workspace_reserved,
                ownership_reserved=workspace_reserved,
                expected_head=str(row['active_sha'] or ''),
                before_create=lambda: self.effects.workspace.reserve_execplan_workspace(self.repo, plan_id),
                allow_rebase=resolving_rebase,
            )
            self.effects.workspace.mark_execplan_workspace_owned(self.repo, plan_id)
        except (RuntimeError, self.effects.contract.WorkspaceOwnershipError) as error:
            workspace_error = f"관리 workspace 소유권/복구 검증 실패: {error}"
            self.effects.blocking.block_active_execplan_from_revision(
                self.repo_path,
                self.repo,
                f"refs/remotes/origin/{self.base}",
                workspace_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
            )
            self.effects.events.operation_print(workspace_error, file=sys.stderr)
            return self._final(workspace_error)
        if int(row['pr_conflict_recovery']):
            recovery_rebase = self.effects.workspace.resume_pr_conflict_rebase(self.repo_path, self.repo, row, worktree, branch)
            if recovery_rebase == 1:
                return self._final()
            row = self._row()
            resolving_rebase = self._resolving_rebase(row)
        rebase_error = str(row['last_error'] or '')
        rebase_recovery = bool(
            row['phase'] == 'WORKING' and (rebase_error == self.effects.contract.REBASE_IN_PROGRESS_ERROR or rebase_error.startswith(self.effects.contract.REBASE_CONFLICT_PREFIX)),
        )
        if rebase_recovery and (not resolving_rebase) and (not int(row['pr_conflict_recovery'])):
            self.effects.state.update_project(self.repo, phase='VERIFYING', last_error=None)
            row = self._row()
        assert catalog_plan is not None
        try:
            self.plan = self.effects.validation.validate_active_execplan_metadata(
                self.repo_path,
                worktree,
                str(row['plan_path']),
                catalog_plan,
            )
            self.level = self.effects.validation.ensure_worker_level_state(self.repo, self.plan)
        except RuntimeError as error:
            metadata_error = str(error)
            self.effects.blocking.block_active_execplan(
                self.repo,
                worktree,
                metadata_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.effects.events.operation_print(metadata_error, file=sys.stderr)
            return self._final(metadata_error)
        try:
            archive_sources = self.effects.archive.requirement_archive_sources(self.repo)
        except self.effects.contract.RequirementArchiveError as error:
            archive_error = f"Requirement archive source 확인 실패: {error}"
            self.effects.blocking.block_active_execplan(
                self.repo,
                worktree,
                archive_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.effects.events.operation_print(archive_error, file=sys.stderr)
            return self._final(archive_error)
        self.archive_candidates = ()
        self.archive_sources = archive_sources
        self.archive_scope_sources = ()
        if row['phase'] == 'WORKING' and archive_sources:
            archive_revision = str(row['active_sha'] or '')
            try:
                self.archive_candidates = self.effects.archive.requirement_archive_candidates(
                    self.repo_path,
                    self.repo,
                    archive_revision,
                    str(self.plan['id']),
                )
                self.archive_scope_sources = tuple(
                    sorted(
                        set(self.archive_candidates) | set(
                            self.effects.archive.requirement_archive_touched_sources(
                                self.repo_path,
                                archive_revision,
                                worktree,
                            ),
                        ),
                    ),
                )
            except self.effects.contract.RequirementArchiveError as error:
                archive_error = f"Requirement archive eligibility 확인 실패: {error}"
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    worktree,
                    archive_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PLAN_INPUT_INVALID,
                )
                self.effects.events.operation_print(archive_error, file=sys.stderr)
                return self._final(archive_error)
        self.catalog_plan = catalog_plan
        return self._context()

    def run(self, context):
        self.effects.state.check_context(context)
        row = self._row()
        catalog_plan = self.catalog_plan
        plan = self.plan
        assert catalog_plan is not None and plan is not None
        self.effects.events.operation_log(f"Worker 시작: {plan['id']} {plan['title']}")
        worker_scope_before = self.effects.agent.begin_agent_scope(
            self.repo,
            context.worktree,
            'worker',
            input_fingerprint=self.effects.agent._worktree_execplan_input_fingerprint(context.worktree, catalog_plan),
        )
        persisted_validation_feedback = []
        if self.effects.validation._deserialize_failure_history(catalog_plan['verify_failure_history']):
            persisted_validation_feedback.append(str(row['verify_result'] or ''))
        if self.effects.validation._deserialize_failure_history(catalog_plan['review_failure_history']):
            persisted_validation_feedback.append(str(row['review_result'] or ''))
        validation_feedback = '\n\n'.join((feedback for feedback in persisted_validation_feedback if feedback))
        guidance_targets = self.effects.agent._guidance_worker_targets(self.repo, str(catalog_plan['plan_id']))
        guidance_context = self.effects.agent.guidance_resume_context(self.repo, str(catalog_plan['plan_id']))
        allow_common_plans = self.effects.agent._published_plan_allows_common_plans(self.repo_path, catalog_plan)
        result = self.effects.agent.run_agent_with_retries(
            lambda current_session_id, session_recovery: self.effects.agent.run_agent(
                self.provider,
                context.worktree,
                self.state_dir,
                self.config_path,
                'worker',
                self.effects.agent.worker_prompt(
                    row['plan_path'],
                    row['last_error'],
                    row['worker_result'],
                    validation_feedback,
                    archive_candidates=self.archive_candidates,
                    session_recovery=session_recovery,
                    guidance_common_allowed=self.effects.agent.is_loop_manager_repository(self.repo_path),
                    guidance_context=guidance_context,
                    allow_common_plans=allow_common_plans,
                ),
                read_only=False,
                level=self.level,
                persist_session=True,
                session_id=current_session_id,
                on_session_started=lambda started_session_id: self.effects.state.update_project(self.repo, worker_session_id=started_session_id),
            ),
            session_id=row['worker_session_id'],
        )
        code, message = result
        if result.session_id:
            self.effects.state.update_project(self.repo, worker_session_id=result.session_id)
        try:
            self.plan = self.effects.agent.validate_worker_workspace_scope(
                self.repo_path,
                context.worktree,
                context.branch,
                self.base,
                str(row['plan_path']),
                catalog_plan,
                worker_scope_before,
                allow_rebase=context.resolving_rebase,
                allowed_requirement_archives=self.archive_scope_sources,
                allowed_guidance_targets=guidance_targets,
                allow_common_plans=allow_common_plans,
            )
            guidance_blocked = self.effects.agent.inspect_worker_guidance_result(
                self.repo_path,
                self.repo,
                context.worktree,
                catalog_plan,
                message,
            )
        except (self.effects.contract.AgentScopeError, self.effects.contract.WorkspaceOwnershipError, RuntimeError) as error:
            scope_error = str(error)
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                scope_error,
                str(row['verify_result'] or ''),
                reason=getattr(error, 'reason_code', BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
            )
            self.effects.events.operation_print(scope_error, file=sys.stderr)
            return self._final(scope_error)
        if guidance_blocked:
            blocked = self.effects.state.blocked()
            self.effects.events.operation_print(
                str(blocked['last_error'] if blocked else 'guidance 승인 대기'),
                file=sys.stderr,
            )
            return self._final()
        self.effects.agent.clear_agent_scope(self.repo)
        if code:
            worker_error = message or f"Agent 종료 코드 {code}"
            if result.result_kind is self.effects.contract.AgentResultKind.TRANSIENT_FAILURE:
                worker_error = f"{self.effects.contract.TRANSIENT_AGENT_ERROR_PREFIX} {worker_error}"
                outcome = ExecutionOutcomeKind.TRANSIENT_PAUSE
            elif result.result_kind is self.effects.contract.AgentResultKind.USAGE_LIMIT:
                worker_error = f"{self.effects.contract.USAGE_LIMIT_ERROR_PREFIX} {worker_error}"
                outcome = ExecutionOutcomeKind.USAGE_PAUSE
            elif context.resolving_rebase:
                worker_error = self.effects.contract.REBASE_CONFLICT_PREFIX + worker_error
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    context.worktree,
                    worker_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
                )
                self.effects.events.operation_print(worker_error, file=sys.stderr)
                return self._final(worker_error)
            else:
                outcome = ExecutionOutcomeKind.FINAL_BLOCK
            if context.resolving_rebase:
                worker_error = self.effects.contract.REBASE_CONFLICT_PREFIX + worker_error
            self.effects.state.update_project(self.repo, phase='WORKING', last_error=worker_error)
            self.effects.events.operation_print(worker_error, file=sys.stderr)
            return ExecutionOutcome(outcome, detail=worker_error, error=True)
        try:
            self.effects.validation.parse_worker_result(message, self.plan)
        except self.effects.contract.WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            if context.resolving_rebase:
                validation_error = self.effects.contract.REBASE_CONFLICT_PREFIX + validation_error
            self.effects.state.update_project(self.repo, phase='WORKING', last_error=validation_error)
            self.effects.events.operation_print(validation_error, file=sys.stderr)
            return self._final(validation_error)
        return self._finish_worker(context, row, message)

    def _finish_worker(self, context, row, message):
        assert self.catalog_plan is not None and self.plan is not None
        latest_sha = str(row['active_sha'] or '')
        if context.resolving_rebase:
            self.effects.workspace.git(context.worktree, 'add', '-A')
            staged = self.effects.workspace.git(context.worktree, 'diff', '--cached', '--quiet', check=False)
            if staged.returncode not in {0, 1}:
                staging_error = f"rebase 충돌 해결 결과 확인 실패 (git 종료 코드 {staged.returncode})."
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    context.worktree,
                    staging_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                )
                self.effects.events.operation_print(staging_error, file=sys.stderr)
                return self._final(staging_error)
            if staged.returncode == 0:
                continued = self.effects.workspace.git(context.worktree, 'rebase', '--skip', check=False)
            else:
                continued = self.effects.workspace.git(
                    context.worktree,
                    '-c',
                    'core.editor=true',
                    'rebase',
                    '--continue',
                    check=False,
                )
            if continued.returncode:
                continuation_error = self.effects.contract.REBASE_CONFLICT_PREFIX + continued.stdout[-8000:]
                if not self.effects.workspace._rebase_in_progress_for_branch(context.worktree, context.branch):
                    self.effects.blocking.block_active_execplan(
                        self.repo,
                        context.worktree,
                        continuation_error,
                        str(row['verify_result'] or ''),
                        reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
                    )
                else:
                    self.effects.state.update_project(self.repo, phase='WORKING', last_error=continuation_error)
                self.effects.events.operation_print(continuation_error, file=sys.stderr)
                return self._final(continuation_error)
            if not self.effects.workspace.branch_has_changes(context.worktree, self.base):
                no_change_error = 'rebase 충돌 해결 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    context.worktree,
                    no_change_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PLAN_INPUT_INVALID,
                )
                self.effects.events.operation_print(no_change_error, file=sys.stderr)
                return self._final(no_change_error)
            latest_sha = str(row['pr_conflict_base_oid']) if int(row['pr_conflict_recovery']) else self.effects.workspace.git(context.worktree, 'rev-parse', f"origin/{self.base}").stdout.strip()
            self.effects.state.update_project(self.repo, active_sha=latest_sha)
            if self.effects.agent.validate_guidance_publication(
                self.repo,
                context.worktree,
                self.catalog_plan,
                message,
                previous_base=str(row['active_sha']),
                latest_base=latest_sha,
            ):
                return self._final()
        if context.archive_sources:
            post_worker_revision = latest_sha if context.resolving_rebase else str(row['active_sha'] or '')
            try:
                expected_archives = self.effects.archive.requirement_archive_candidates(
                    self.repo_path,
                    self.repo,
                    post_worker_revision,
                    str(self.plan['id']),
                )
                actual_archives = self.effects.archive.requirement_archive_move_set(
                    self.repo_path,
                    post_worker_revision,
                    context.worktree,
                )
            except self.effects.contract.RequirementArchiveError as error:
                archive_error = f"Requirement archive 이동 검증 실패: {error}"
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    context.worktree,
                    archive_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PLAN_INPUT_INVALID,
                )
                self.effects.events.operation_print(archive_error, file=sys.stderr)
                return self._final(archive_error)
            archive_error = self.effects.archive.requirement_archive_reconciliation_error(expected_archives, actual_archives)
            if archive_error is not None:
                self.effects.state.update_project(
                    self.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    worker_result=message,
                    verify_result=None,
                    review_result=None,
                    last_error=archive_error,
                )
                self.effects.events.operation_print(archive_error, file=sys.stderr)
                return self._retry(archive_error)
        self.effects.state.update_project(
            self.repo,
            phase='VERIFYING',
            validated_base_sha=None,
            worker_result=message,
            verify_result=None,
            last_error=None,
        )
        return self._advanced()

    def _verify_once(self, context, *, after_rebase=False):
        assert self.catalog_plan is not None and self.plan is not None
        suffix = ' (rebase 후)' if after_rebase else ''
        self.effects.events.operation_log(f"전체 verify 시작: {self.plan['id']} {self.plan['title']}{suffix}")
        verify_scope_before = self.effects.agent.begin_agent_scope(
            self.repo,
            context.worktree,
            'worker',
            input_fingerprint=self.effects.agent._worktree_execplan_input_fingerprint(context.worktree, self.catalog_plan),
        )
        passed, output = self.effects.validation.verify(context.worktree)
        try:
            self.plan = self.effects.agent.validate_worker_workspace_scope(
                self.repo_path,
                context.worktree,
                context.branch,
                self.base,
                context.plan.path,
                self.catalog_plan,
                verify_scope_before,
            )
        except (self.effects.contract.AgentScopeError, self.effects.contract.WorkspaceOwnershipError, RuntimeError) as error:
            scope_error = str(error)
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                scope_error,
                output,
                reason=getattr(error, 'reason_code', BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
            )
            self.effects.events.operation_print(scope_error, file=sys.stderr)
            return (self._final(scope_error), output)
        self.effects.agent.clear_agent_scope(self.repo)
        if not passed:
            stage = 'rebase 후 전체 verify 실패' if after_rebase else '전체 verify 실패'
            self.effects.events.operation_log(
                f"Worker 반환: {self.plan['id']} {self.plan['title']} ({stage})",
                error=True,
            )
            decision = self.effects.validation.transition_validation_failure(
                self.repo,
                context.worktree,
                'verify',
                output,
            )
            self.effects.events.operation_print(output, file=sys.stderr)
            if decision.blocked:
                return (self._final(output), output)
            return (self._retry(output), output)
        self.effects.validation.clear_validation_failure_history(self.repo, 'verify')
        return (None, output)

    def verify(self, context, latest_base):
        self.effects.state.check_context(context)
        row = self._row()
        assert self.catalog_plan is not None and self.plan is not None
        if self.effects.agent.validate_guidance_publication(
            self.repo,
            context.worktree,
            self.catalog_plan,
            str(row['worker_result'] or ''),
        ):
            return self._final()
        try:
            worker_publication = self.effects.validation.parse_worker_result(str(row['worker_result'] or ''), self.plan)
        except self.effects.contract.WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            self.effects.state.update_project(self.repo, phase='WORKING', last_error=validation_error)
            self.effects.events.operation_print(validation_error, file=sys.stderr)
            return self._final(validation_error)
        outcome, output = self._verify_once(context)
        if outcome is not None:
            return outcome
        dirty = bool(self.effects.workspace.git(context.worktree, 'status', '--porcelain').stdout.strip())
        if not dirty and (not self.effects.workspace.branch_has_changes(context.worktree, self.base)):
            no_change_error = 'Worker가 변경을 만들지 않아 PR을 생성할 수 없습니다. ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다.'
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                no_change_error,
                output,
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.effects.events.operation_print(no_change_error, file=sys.stderr)
            return self._final(no_change_error)
        if dirty:
            self.effects.workspace.git(context.worktree, 'add', '-A')
            self.plan = self.effects.validation.validate_active_execplan_metadata(
                self.repo_path,
                context.worktree,
                context.plan.path,
                self.catalog_plan,
            )
            self.effects.workspace.git(context.worktree, 'commit', '-m', worker_publication.commit_title)
        self._latest_verify_output = output
        self._worker_publication = worker_publication
        latest_outcome = latest_base.reconcile(context, LatestBaseCheckpoint.AFTER_VERIFY)
        if latest_outcome is not None:
            return latest_outcome
        return self._finish_verified(context)

    def reconcile(self, context, checkpoint):
        if checkpoint is LatestBaseCheckpoint.AFTER_VERIFY:
            return self._latest_after_verify(context)
        return self._latest_before_publication(context)

    def _fetch_latest_base(self):
        self.effects.workspace.git(self.repo_path, 'fetch', 'origin')
        latest_sha = self.effects.workspace.git(self.repo_path, 'rev-parse', f"origin/{self.base}").stdout.strip()
        return latest_sha

    def _rebase_latest(self, context, row, latest_sha, *, publishing, evidence):
        """Persist rebase intent, observe Git, then record the applied base."""
        prefix = '게시 직전 ' if publishing else ''
        self.effects.state.update_project(
            self.repo,
            phase='WORKING',
            validated_base_sha=None,
            pr_conflict_base_oid=latest_sha if int(row['pr_conflict_recovery']) else row['pr_conflict_base_oid'],
            last_error=self.effects.contract.REBASE_IN_PROGRESS_ERROR,
        )
        rebased = self.effects.workspace.git(context.worktree, 'rebase', latest_sha, check=False)
        if rebased.returncode:
            if self.effects.workspace._rebase_in_progress_for_branch(context.worktree, context.branch):
                if self.effects.agent.validate_guidance_publication(
                    self.repo,
                    context.worktree,
                    self.catalog_plan,
                    str(row['worker_result'] or ''),
                    previous_base=str(row['active_sha']),
                    latest_base=latest_sha,
                ):
                    return self._final()
                rebase_failure = self.effects.contract.REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:]
                self.effects.state.update_project(
                    self.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    last_error=rebase_failure,
                )
            else:
                rebase_failure = f"{prefix}rebase 실패:\n{rebased.stdout[-8000:]}"
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    context.worktree,
                    rebase_failure,
                    evidence,
                    reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
                )
            self.effects.events.operation_print(
                rebase_failure if publishing else rebased.stdout,
                file=sys.stderr,
            )
            return self._final(rebase_failure)
        self.effects.state.update_project(self.repo, active_sha=latest_sha)
        if self.effects.agent.validate_guidance_publication(
            self.repo,
            context.worktree,
            self.catalog_plan,
            str(row['worker_result'] or ''),
            previous_base=str(row['active_sha']),
            latest_base=latest_sha,
        ):
            return self._final()
        return None

    def _latest_after_verify(self, context):
        row = self._row()
        assert self.catalog_plan is not None and self.plan is not None
        assert self._worker_publication is not None
        output = self._latest_verify_output
        latest_sha = self._fetch_latest_base()
        if latest_sha != row['active_sha']:
            outcome = self._rebase_latest(context, row, latest_sha, publishing=False, evidence=output)
            if outcome is not None:
                return outcome
            rebased_context = context.at_phase(ExecutionPhase.VERIFYING, active_sha=latest_sha)
            outcome, output = self._verify_once(rebased_context, after_rebase=True)
            if outcome is not None:
                return outcome
            if self.effects.workspace.git(context.worktree, 'status', '--porcelain').stdout.strip():
                self.effects.workspace.git(context.worktree, 'add', '-A')
                self.plan = self.effects.validation.validate_active_execplan_metadata(
                    self.repo_path,
                    context.worktree,
                    context.plan.path,
                    self.catalog_plan,
                )
                self.effects.workspace.git(context.worktree, 'commit', '-m', self._worker_publication.commit_title)
            self.effects.state.update_project(
                self.repo,
                active_sha=latest_sha,
                validated_base_sha=latest_sha,
                verify_result=output,
            )
        else:
            self.effects.state.update_project(
                self.repo,
                validated_base_sha=str(row['active_sha'] or ''),
                verify_result=output,
            )
        self._latest_verify_output = output
        return None

    def _finish_verified(self, context):
        assert self.catalog_plan is not None
        output = self._latest_verify_output
        try:
            self.plan = self.effects.validation.validate_active_execplan_metadata(
                self.repo_path,
                context.worktree,
                context.plan.path,
                self.catalog_plan,
            )
        except RuntimeError as error:
            metadata_error = str(error)
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                metadata_error,
                output,
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.effects.events.operation_print(metadata_error, file=sys.stderr)
            return self._final(metadata_error)
        if not self.effects.workspace.branch_has_changes(context.worktree, self.base):
            no_change_error = 'rebase 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다. ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다.'
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                no_change_error,
                output,
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.effects.events.operation_print(no_change_error, file=sys.stderr)
            return self._final(no_change_error)
        self.effects.state.update_project(self.repo, phase='REVIEWING', last_error=None)
        return self._advanced()

    def review(self, context):
        self.effects.state.check_context(context)
        row = self._row()
        assert self.catalog_plan is not None and self.plan is not None
        protocol_error: str | None = None
        verdict: str | None = None
        review = ''
        for protocol_attempt in range(1, self.effects.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS + 1):
            if protocol_attempt == 1:
                self.effects.events.operation_log(f"Reviewer 시작: {self.plan['id']} {self.plan['title']}")
            else:
                self.effects.events.operation_log(
                    f"Reviewer 프로토콜 교정 재시도: {self.plan['id']} {self.plan['title']} ({protocol_attempt}/{self.effects.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS})",
                )
            reviewer_scope_before = self.effects.agent.begin_agent_scope(
                self.repo,
                context.worktree,
                'reviewer',
                input_fingerprint=self.effects.agent._worktree_execplan_input_fingerprint(context.worktree, self.catalog_plan),
            )
            review_result = self.effects.agent.run_agent_with_retries(
                lambda current_session_id, session_recovery: self.effects.agent.run_agent(
                    self.provider,
                    context.worktree,
                    self.state_dir,
                    self.config_path,
                    'reviewer',
                    self.effects.agent.reviewer_prompt(
                        self.base,
                        row['worker_result'],
                        row['verify_result'],
                        row['review_result'],
                        session_recovery=session_recovery,
                        protocol_error=protocol_error,
                    ),
                    read_only=True,
                    persist_session=True,
                    session_id=current_session_id,
                    on_session_started=lambda started_session_id: self.effects.state.update_project(self.repo, reviewer_session_id=started_session_id),
                ),
                session_id=row['reviewer_session_id'],
            )
            code, review = review_result
            if review_result.session_id:
                self.effects.state.update_project(self.repo, reviewer_session_id=review_result.session_id)
            try:
                reviewer_scope_after = self.effects.agent.capture_git_snapshot(context.worktree)
                self.effects.agent.validate_agent_scope(
                    'reviewer',
                    context.worktree,
                    reviewer_scope_before,
                    reviewer_scope_after,
                    external_worktree=self.repo_path,
                    base_branch=self.base,
                )
                self.effects.workspace.validate_managed_worktree(self.repo_path, context.worktree, context.branch)
            except (self.effects.contract.AgentScopeError, self.effects.contract.WorkspaceOwnershipError, RuntimeError) as error:
                scope_error = str(error)
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    context.worktree,
                    scope_error,
                    str(row['verify_result'] or ''),
                    reason=getattr(error, 'reason_code', BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
                )
                self.effects.events.operation_print(scope_error, file=sys.stderr)
                return self._final(scope_error)
            self.effects.agent.clear_agent_scope(self.repo)
            if code:
                reviewer_error = review or f"Reviewer 종료 코드 {code}"
                if review_result.result_kind is self.effects.contract.AgentResultKind.TRANSIENT_FAILURE:
                    reviewer_error = f"{self.effects.contract.TRANSIENT_AGENT_ERROR_PREFIX} {reviewer_error}"
                    outcome = ExecutionOutcomeKind.TRANSIENT_PAUSE
                elif review_result.result_kind is self.effects.contract.AgentResultKind.USAGE_LIMIT:
                    reviewer_error = f"{self.effects.contract.USAGE_LIMIT_ERROR_PREFIX} {reviewer_error}"
                    outcome = ExecutionOutcomeKind.USAGE_PAUSE
                else:
                    outcome = ExecutionOutcomeKind.FINAL_BLOCK
                self.effects.state.update_project(
                    self.repo,
                    phase='REVIEWING',
                    last_error=reviewer_error,
                    review_result=review,
                )
                self.effects.events.operation_print(reviewer_error, file=sys.stderr)
                return ExecutionOutcome(outcome, detail=reviewer_error, error=True)
            try:
                verdict, _issue_ids = self.effects.validation.parse_reviewer_result(review)
            except self.effects.contract.ReviewerProtocolError as error:
                protocol_error = str(error)
                reviewer_error = f"Reviewer 결과 프로토콜 오류: {protocol_error}"
                self.effects.state.update_project(
                    self.repo,
                    phase='REVIEWING',
                    last_error=reviewer_error,
                    review_result=review,
                )
                if protocol_attempt == self.effects.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS:
                    exhausted_error = f"{reviewer_error} (프로토콜 교정 {self.effects.contract.MAX_REVIEWER_PROTOCOL_ATTEMPTS}회 실패)"
                    self.effects.blocking.block_active_execplan(
                        self.repo,
                        context.worktree,
                        exhausted_error,
                        str(row['verify_result'] or ''),
                        reason=BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
                    )
                    self.effects.events.operation_print(exhausted_error, file=sys.stderr)
                    return self._final(exhausted_error)
                self.effects.events.operation_print(reviewer_error, file=sys.stderr)
                row = self._row()
                continue
            break
        assert verdict is not None
        if verdict == 'FAIL':
            decision = self.effects.validation.transition_validation_failure(
                self.repo,
                context.worktree,
                'review',
                review,
            )
            self.effects.events.operation_log(
                f"Worker 반환: {self.plan['id']} {self.plan['title']} (Review 실패)",
                error=True,
            )
            self.effects.events.operation_print(review or 'Reviewer 결과가 유효하지 않습니다.', file=sys.stderr)
            if decision.blocked:
                return self._final(review)
            return self._retry(review)
        self.effects.validation.clear_validation_failure_history(self.repo, 'review')
        self.effects.state.update_project(
            self.repo,
            phase='PUBLISHING',
            last_error=None,
            review_result=review,
        )
        return self._advanced()

    def _latest_before_publication(self, context):
        row = self._row()
        assert self.catalog_plan is not None
        latest_sha = self._fetch_latest_base()
        remote_oid = None if int(row['pr_conflict_recovery']) else self.effects.workspace.remote_branch_oid(self.repo_path, context.branch)
        try:
            if not int(row['pr_conflict_recovery']):
                self.effects.publication.record_remote_publication_branch(
                    self.repo_path,
                    self.state_dir,
                    self.repo,
                    context.branch,
                    context.worktree,
                    remote_oid,
                )
        except self.effects.contract.PublicationSafetyError as error:
            publication_error = str(error)
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                publication_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
            self.effects.events.operation_print(publication_error, file=sys.stderr)
            return self._final(publication_error)
        if latest_sha != row['validated_base_sha']:
            if latest_sha != row['active_sha']:
                outcome = self._rebase_latest(
                    context,
                    row,
                    latest_sha,
                    publishing=True,
                    evidence=str(row['verify_result'] or ''),
                )
                if outcome is not None:
                    return outcome
                if not self.effects.workspace.branch_has_changes(context.worktree, self.base):
                    no_change_error = '게시 직전 rebase 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
                    self.effects.blocking.block_active_execplan(
                        self.repo,
                        context.worktree,
                        no_change_error,
                        str(row['verify_result'] or ''),
                        reason=BlockReasonCode.PLAN_INPUT_INVALID,
                    )
                    self.effects.events.operation_print(no_change_error, file=sys.stderr)
                    return self._final(no_change_error)
                self.effects.state.update_project(
                    self.repo,
                    active_sha=latest_sha,
                    validated_base_sha=None,
                    phase='VERIFYING',
                    verify_result=None,
                    review_result=None,
                    last_error=None,
                )
            else:
                self.effects.state.update_project(
                    self.repo,
                    validated_base_sha=None,
                    phase='VERIFYING',
                    verify_result=None,
                    review_result=None,
                    last_error=None,
                )
            self.effects.events.operation_print(
                '게시 직전 base가 변경되어 전체 verify와 Review를 다시 수행합니다.',
                file=sys.stderr,
            )
            return self._retry()
        if row['active_sha'] != latest_sha:
            publication_error = '검증 base와 관리 branch의 반영 base가 일치하지 않습니다.'
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                publication_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE,
            )
            self.effects.events.operation_print(publication_error, file=sys.stderr)
            return self._final(publication_error)
        return None

    def publish(self, context, latest_base):
        self.effects.state.check_context(context)
        row = self._row()
        assert self.catalog_plan is not None and self.plan is not None
        existing_pr = self.effects.publication.pr_for_branch(
            self.repo_path,
            context.branch,
            str(row['pr_url']) if int(row['pr_conflict_recovery']) else None,
        )
        if existing_pr:
            try:
                reconciliation = self.effects.publication.reconcile_existing_pull_request(
                    self.repo_path,
                    self.state_dir,
                    self.repo,
                    row,
                    existing_pr,
                )
                if reconciliation is not None:
                    return self._step_outcome(reconciliation)
                row = self._row()
            except self.effects.contract.PublicationSafetyError as error:
                publication_error = f"기존 PR 복구 거부: {error}"
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    context.worktree,
                    publication_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
                self.effects.events.operation_print(publication_error, file=sys.stderr)
                return self._final(publication_error)
        latest_outcome = latest_base.reconcile(context, LatestBaseCheckpoint.BEFORE_PUBLICATION)
        if latest_outcome is not None:
            return latest_outcome
        row = self._row()
        latest_sha = str(row['active_sha'] or '')
        if context.archive_sources:
            try:
                final_archive_candidates = self.effects.archive.requirement_archive_candidates(
                    self.repo_path,
                    self.repo,
                    latest_sha,
                    str(self.plan['id']),
                )
                final_archive_moves = self.effects.archive.requirement_archive_move_set(
                    self.repo_path,
                    latest_sha,
                    context.worktree,
                    tracked_revision='HEAD',
                )
            except self.effects.contract.RequirementArchiveError as error:
                archive_error = f"게시 직전 Requirement archive 확인 실패: {error}"
                self.effects.blocking.block_active_execplan(
                    self.repo,
                    context.worktree,
                    archive_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PLAN_INPUT_INVALID,
                )
                self.effects.events.operation_print(archive_error, file=sys.stderr)
                return self._final(archive_error)
            archive_error = self.effects.archive.requirement_archive_reconciliation_error(
                final_archive_candidates,
                final_archive_moves,
            )
            if archive_error is not None:
                self.effects.state.update_project(
                    self.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    verify_result=None,
                    review_result=None,
                    last_error=archive_error,
                )
                self.effects.events.operation_print(archive_error, file=sys.stderr)
                return self._retry(archive_error)
        if self.effects.agent.validate_guidance_publication(
            self.repo,
            context.worktree,
            self.catalog_plan,
            str(row['worker_result'] or ''),
        ):
            return self._final()
        if not self.effects.workspace.branch_has_changes(context.worktree, self.base):
            no_change_error = '게시할 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                no_change_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.effects.events.operation_print(no_change_error, file=sys.stderr)
            return self._final(no_change_error)
        if int(row['pr_conflict_recovery']):
            return self._publish_conflict_recovery(context, row)
        return self._publish_new_pull_request(context, row)

    def _publish_conflict_recovery(self, context, row):
        current_pr = self.effects.publication.pr_for_branch(self.repo_path, context.branch, str(row['pr_url'] or ''))
        if current_pr is None:
            publication_error = '게시 직전 저장된 기존 PR을 다시 조회할 수 없습니다.'
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                publication_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
            self.effects.events.operation_print(publication_error, file=sys.stderr)
            return self._final(publication_error)
        reconciliation = self.effects.publication.reconcile_existing_pull_request(
            self.repo_path,
            self.state_dir,
            self.repo,
            row,
            current_pr,
        )
        if reconciliation is not None:
            return self._step_outcome(reconciliation)
        row = self._row()
        try:
            head_oid = self.effects.publication.push_owned_pr_conflict_recovery(
                self.repo_path,
                self.state_dir,
                self.repo,
                context.branch,
                context.worktree,
                current_pr,
            )
        except self.effects.contract.PublicationSafetyError as error:
            publication_error = str(error)
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                publication_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
            self.effects.events.operation_print(publication_error, file=sys.stderr)
            return self._final(publication_error)
        except RuntimeError as error:
            publication_error = str(error)
            self.effects.state.update_project(self.repo, phase='PUBLISHING', last_error=publication_error)
            self.effects.events.operation_print(publication_error, file=sys.stderr)
            return self._final(publication_error)
        self.effects.state.open_pull_request(
            self.repo,
            publication_remote_oid=head_oid,
            pr_head_oid=head_oid,
            pr_conflict_recovery=0,
            pr_conflict_head_oid=None,
            pr_conflict_base_oid=None,
        )
        self.effects.events.operation_print(f"기존 PR 충돌 복구 게시 완료: {row['pr_url']}")
        return ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)

    def _publish_new_pull_request(self, context, row):
        assert self.plan is not None
        plan_sha = str(row['plan_sha'] or '')
        if not plan_sha:
            plan_error = '계획 commit SHA가 없습니다.'
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                plan_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.effects.events.operation_print(plan_error, file=sys.stderr)
            return self._final(plan_error)
        plan_text = self.effects.workspace.git(context.worktree, 'show', f"{plan_sha}:{row['plan_path']}").stdout
        self.plan = self.effects.publication.published_execplan(str(row['plan_path']), plan_text)
        try:
            worker_publication = self.effects.validation.parse_worker_result(str(row['worker_result'] or ''), self.plan)
        except self.effects.contract.WorkerResultError as error:
            validation_error = f"Worker 결과 검증 실패:\n{error}"
            self.effects.state.update_project(
                self.repo,
                phase='WORKING',
                validated_base_sha=None,
                last_error=validation_error,
            )
            self.effects.events.operation_print(validation_error, file=sys.stderr)
            return self._final(validation_error)
        origin_url = self.effects.workspace.git(context.worktree, 'remote', 'get-url', 'origin').stdout.strip()
        body = self.effects.publication.build_pr_body(
            worker_publication,
            self.plan,
            str(row['plan_path']),
            plan_text,
            origin_url,
            plan_sha,
        )
        try:
            head_oid = self.effects.publication.push_managed_publication_branch(
                self.repo_path,
                self.state_dir,
                self.repo,
                context.branch,
                context.worktree,
            )
        except self.effects.contract.PublicationSafetyError as error:
            publication_error = str(error)
            self.effects.blocking.block_active_execplan(
                self.repo,
                context.worktree,
                publication_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
            )
            self.effects.events.operation_print(publication_error, file=sys.stderr)
            return self._final(publication_error)
        except RuntimeError as error:
            publication_error = str(error)
            self.effects.state.update_project(self.repo, phase='PUBLISHING', last_error=publication_error)
            self.effects.events.operation_print(publication_error, file=sys.stderr)
            return self._final(publication_error)
        title = f"Implement {self.plan['id']}: {self.plan['title']}"
        created = self.effects.publication.command(
            ['gh', 'pr', 'create', '--base', self.base, '--head', context.branch, '--title', title, '--body', body],
            cwd=context.worktree,
        )
        url = created.stdout.strip().splitlines()[-1]
        self.effects.state.open_pull_request(self.repo, pr_url=url, pr_head_oid=head_oid)
        self.effects.events.operation_print(f"PR 생성 완료: {url}")
        return ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)
