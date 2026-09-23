"""Execution handlers; all external operations arrive through effect ports."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Callable, Any
from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import re
import sys
from scoped_blocking import BlockReasonCode
from recovery_runtime import (
    RecoveryAction, RecoveryObservation, RecoveryReason, RecoveryFailure,
    classify_exception,
    TRANSIENT, RECONCILE, reason_from_block,
)
from recovery_dispatcher import RecoveryDispatcher
from execution_revision import (DefinitionDelta, DefinitionSnapshot, RevisionGateKind,
    StageArtifacts, WORKER_INVALIDATING_DELTAS, decide_revision_gate)
from execution_runtime import (ExecutionContext, ExecutionOutcome, ExecutionOutcomeKind,
    ExecutionPhase, ExecutionPlanMetadata, LatestBaseCheckpoint)
from requirement_archive_state import ArchiveTargetRepository, ArchiveTargetConflict
from requirement_archive_recovery import (
    ArchiveRecoveryPorts, ArchiveRecoveryService,
)
from execution_checkpoint import CheckpointAction, assess, observe
from execution_checkpoint_state import CheckpointRace
from runtime_state import RuntimeStateError
from publication_resume import PublicationNext, PublicationResume, assess_publication

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
    git_operation_entries: Callable[..., Any] | None = None

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
    requirement_archive_snapshot: Callable[..., Any]
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
class RevisionEffects:
    reconcile_catalog: Callable[..., Any]
    source_fingerprint: Callable[..., Any]
    body_hash: Callable[..., Any]
    changed_paths: Callable[..., Any]
    unmet_dependencies: Callable[..., Any]

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
    revision: RevisionEffects
    checkpoint: Any = None


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
        self._archive_snapshot = None
        self._latest_verify_output = ''
        self._worker_publication = None
        self._definition_snapshot = None
        self.recovery = RecoveryDispatcher(
            effects.state.connection,
            effects.blocking.block_active_execplan,
            effects.blocking.block_active_execplan_from_revision,
        )

    def _row(self):
        return self.effects.state.row()

    def recover_failure(self, context, error):
        return self._recover(error.reason, str(error), context=context)

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
                definition_revision_sha=(
                    str(self.catalog_plan['current_revision_sha'])
                    if self.catalog_plan['current_revision_sha'] is not None
                    else None
                ),
                content_hash=str(self.catalog_plan['content_hash']),
                depends_on=tuple(
                    str(item) for item in self.plan.get('depends_on', ())
                ),
                complexity=str(self.plan.get('complexity')),
                risk=str(self.plan.get('risk')),
                priority=str(self.plan.get('priority')),
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
            archive_snapshot_token=(
                self._archive_snapshot.token
                if self._archive_snapshot is not None else None
            ),
            archive_candidate_scope=self.archive_candidates,
            current_definition_token=(
                self._definition_snapshot.definition_token
                if self._definition_snapshot is not None
                else None
            ),
            current_base_token=str(row['active_sha'] or ''),
            worker_input_token=(
                str(row['worker_input_token'])
                if row['worker_input_token'] is not None else None
            ),
            archive_token=(
                str(row['archive_token'])
                if row['archive_token'] is not None else None
            ),
            verify_token=(
                str(row['verify_token'])
                if row['verify_token'] is not None else None
            ),
            review_token=(
                str(row['review_token'])
                if row['review_token'] is not None else None
            ),
            publication_token=(
                str(row['publication_token'])
                if row['publication_token'] is not None else None
            ),
        )

    @staticmethod
    def _final(detail=None):
        return ExecutionOutcome(ExecutionOutcomeKind.FINAL_BLOCK, detail=detail, error=True)

    @staticmethod
    def _retry(detail=None):
        return ExecutionOutcome(ExecutionOutcomeKind.RETRY, detail=detail, error=detail is not None)

    def _recover(self, reason, detail, *, context=None, fingerprint=None,
                 progress_marker=None, final_block_reason=None,
                 from_revision=False, correction_limit=None, attempts=0,
                 user_targets=(), verify_result=None):
        """Persist a typed decision and apply the final gate in one transaction."""

        row = self._row()
        plan_id = str(self.plan['id']) if self.plan is not None else self.effects.state.plan_id
        base = str(row['active_sha'] or self.base_sha)
        progress = progress_marker or '|'.join(str(row[key] or '') for key in (
            'active_sha', 'worker_session_id', 'reviewer_session_id',
            'worker_input_token', 'verify_token', 'review_token'))
        fingerprint_input = (
            f"{reason.value}|{row['phase']}|{base}"
            if reason in TRANSIENT or reason in RECONCILE
            else f"{reason.value}|{detail}"
        )
        stable_fingerprint = fingerprint or hashlib.sha256(
            fingerprint_input.encode()
        ).hexdigest()
        observation = RecoveryObservation(
            reason=reason, repo=self.repo, plan_id=plan_id,
            phase=str(row['phase'] or ''),
            plan_revision=str(self.catalog_plan['current_revision_sha'] or '')
            if self.catalog_plan is not None else None,
            base_revision=base, fingerprint=stable_fingerprint,
            progress_marker=progress, detail=detail,
            user_targets=tuple(user_targets),
        )
        decision = self.recovery.apply(
            observation,
            worktree=Path(str(row['worktree'] or '')),
            repo_path=self.repo_path,
            revision=f"refs/remotes/origin/{self.base}",
            verify_result=(str(row['verify_result'] or '')
                           if verify_result is None else verify_result),
            from_revision=from_revision,
            correction_limit=correction_limit,
            attempts=attempts,
            final_block_reason=final_block_reason,
        )
        kind = {
            RecoveryAction.RETRY: ExecutionOutcomeKind.RETRY,
            RecoveryAction.RECONCILE: ExecutionOutcomeKind.RETRY,
            RecoveryAction.CORRECT: ExecutionOutcomeKind.RETRY,
            RecoveryAction.PAUSE: ExecutionOutcomeKind.TRANSIENT_PAUSE,
            RecoveryAction.FINAL_BLOCK: ExecutionOutcomeKind.FINAL_BLOCK,
        }[decision.action]
        return ExecutionOutcome(kind, context=context, detail=detail,
                                error=kind is ExecutionOutcomeKind.FINAL_BLOCK,
                                recovery=decision)

    def _block_final(self, repo, worktree, detail, verify_result, *, reason,
                     recovery_reason=None, user_targets=()):
        return self._recover(recovery_reason or reason_from_block(reason), detail,
                             final_block_reason=reason,
                             user_targets=user_targets,
                             verify_result=verify_result)

    def _metadata_correction(self, context, error, *, verify_result=None):
        detail = f"ExecPlan metadata 검증 실패:\n{error}"
        if context.resolving_rebase:
            detail = self.effects.contract.REBASE_CONFLICT_PREFIX + detail
        values = {'phase': 'WORKING', 'last_error': detail}
        if verify_result is not None:
            values['verify_result'] = verify_result
        self.effects.state.update_project(self.repo, **values)
        self.effects.events.operation_print(detail, file=sys.stderr)
        return self._recover(
            RecoveryReason.WORKER_CORRECTION, detail, context=context,
            verify_result=verify_result,
            final_block_reason=BlockReasonCode.PLAN_INPUT_INVALID,
        )

    def _block_from_revision_final(self, repo_path, repo, revision, detail,
                                   verify_result='', *, reason):
        return self._recover(
            RecoveryReason.OWNERSHIP_UNCERTAIN, detail,
            final_block_reason=reason, from_revision=True,
            verify_result=verify_result,
        )

    def _step_outcome(self, step_result):
        kind = {0: ExecutionOutcomeKind.SUCCESS, 1: ExecutionOutcomeKind.FINAL_BLOCK, self.effects.contract.PROJECT_STEP_RETRY: ExecutionOutcomeKind.RETRY, self.effects.contract.PROJECT_STEP_TRANSIENT_PAUSE: ExecutionOutcomeKind.TRANSIENT_PAUSE, self.effects.contract.PROJECT_STEP_USAGE_PAUSE: ExecutionOutcomeKind.USAGE_PAUSE, self.effects.contract.PROJECT_STEP_PLANNING_PENDING: ExecutionOutcomeKind.PLANNING_PENDING}.get(
            step_result,
            ExecutionOutcomeKind.OPERATIONAL_ERROR,
        )
        return ExecutionOutcome(kind, error=kind in {
            ExecutionOutcomeKind.FINAL_BLOCK,
            ExecutionOutcomeKind.OPERATIONAL_ERROR,
        })

    def _advanced(self):
        return ExecutionOutcome.advanced(self._context())

    def _resolving_rebase(self, row):
        rebase_error = str(row['last_error'] or '')
        rebase_recovery = bool(
            row['phase'] == 'WORKING' and (rebase_error == self.effects.contract.REBASE_IN_PROGRESS_ERROR or rebase_error.startswith(self.effects.contract.REBASE_CONFLICT_PREFIX)),
        )
        worktree = Path(str(row['worktree'] or ''))
        if not rebase_recovery or not self.effects.workspace.is_directory(worktree):
            return False
        if self.effects.workspace._rebase_in_progress_for_branch(worktree, str(row['branch'] or '')):
            return True
        if int(row['pr_conflict_recovery']) and row['pr_conflict_base_oid']:
            return bool(self._unmerged_stages(worktree))
        checkpoint = self.effects.checkpoint
        return bool(
            checkpoint is not None
            and checkpoint.get(self.repo, self.catalog_plan['plan_id']).operation == 'rebase'
            and self._unmerged_stages(worktree)
        ) if self.catalog_plan is not None else False

    def _unmerged_stages(self, worktree):
        return self.effects.workspace.git(worktree, 'ls-files', '-u', '-z').stdout

    def _record_rebase_intent(self, context, target_oid):
        checkpoint = self.effects.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.repo, context.plan.plan_id)
        head = self.effects.workspace.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        checkpoint.record(
            self.repo, context.plan.plan_id,
            expected_generation=current.generation, operation='rebase',
            expected_head=head, target_base_oid=target_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={
                'before_head': head,
                'target_base': target_oid,
                'staged_paths': self.effects.workspace.git(
                    context.worktree, 'diff', '--cached', '--name-only', '-z'
                ).stdout.split('\0'),
                'staged_blobs': self.effects.workspace.git(
                    context.worktree, 'diff', '--cached', '--raw',
                    '--no-abbrev', '-z',
                ).stdout,
                'worktree_status': self.effects.workspace.git(
                    context.worktree, 'status', '--porcelain=v2', '-z'
                ).stdout,
            },
        )

    def _workspace_boundary(self, plan_id, row, *, pending):
        checkpoint = self.effects.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.repo, plan_id)
        checkpoint.record(
            self.repo, plan_id, expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={
                **(current.snapshot or {}),
                'worktree_recovery_pending': pending,
                'worktree': str(row['worktree'] or ''),
                'branch': str(row['branch'] or ''),
                'stored_active_sha': str(row['active_sha'] or ''),
            },
        )

    def _record_rebase_suboperation(self, context, pending, *, worker_result=None):
        checkpoint = self.effects.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.repo, context.plan.plan_id)
        if current.operation != 'rebase':
            return
        checkpoint.record(
            self.repo, context.plan.plan_id,
            expected_generation=current.generation, operation='rebase',
            expected_head=current.expected_head,
            target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={
                **(current.snapshot or {}), 'pending': pending,
                'worker_result': worker_result or (current.snapshot or {}).get('worker_result'),
            },
        )

    def _post_rebase_observation(self, context, target_oid, output):
        """A successful exit may leave autostash conflicts without rebase metadata."""
        unmerged = self._unmerged_stages(context.worktree)
        in_progress = self.effects.workspace._rebase_in_progress_for_branch(
            context.worktree, context.branch
        )
        checkpoint = self.effects.checkpoint
        if checkpoint is not None and unmerged:
            current = checkpoint.get(self.repo, context.plan.plan_id)
            stash_match = re.search(r'Created autostash: ([0-9a-fA-F]{7,64})', output)
            stash_oid = None
            if stash_match:
                resolved = self.effects.workspace.git(
                    context.worktree, 'rev-parse', '--verify',
                    f'{stash_match.group(1)}^{{commit}}', check=False,
                )
                if resolved.returncode == 0:
                    stash_oid = resolved.stdout.strip()
            checkpoint.record(
                self.repo, context.plan.plan_id,
                expected_generation=current.generation, operation='rebase',
                expected_head=current.expected_head,
                target_base_oid=target_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={
                    **(current.snapshot or {}),
                    'after_head': self.effects.workspace.git(
                        context.worktree, 'rev-parse', 'HEAD'
                    ).stdout.strip(),
                    'unmerged_index': unmerged,
                    'stash_oid': stash_oid or (current.snapshot or {}).get('stash_oid'),
                },
            )
            if not in_progress and not (stash_oid or (current.snapshot or {}).get('stash_oid')):
                return self._recover(
                    RecoveryReason.STATUS_FAILED, 'autostash_identity_unavailable',
                    context=context, attempts=3,
                )
        if not unmerged and not in_progress:
            if checkpoint is not None:
                self.effects.workspace.validate_managed_worktree(
                    self.repo_path, context.worktree, context.branch
                )
                ancestry = self.effects.workspace.git(
                    context.worktree, 'merge-base', '--is-ancestor',
                    target_oid, 'HEAD', check=False,
                )
                if ancestry.returncode != 0:
                    raise RecoveryFailure(
                        RecoveryReason.STATUS_FAILED,
                        'rebase completed without expected target ancestry',
                    )
            return None
        message = self.effects.contract.REBASE_CONFLICT_PREFIX + output[-8000:]
        self.effects.state.update_project(
            self.repo, phase='WORKING', validated_base_sha=None,
            last_error=message,
        )
        return self._recover(RecoveryReason.REBASE_CONFLICT, message, context=context)

    def _complete_rebase_intent(self, context):
        checkpoint = self.effects.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.repo, context.plan.plan_id)
        if current.operation != 'rebase':
            return
        head = self.effects.workspace.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        checkpoint.record(
            self.repo, context.plan.plan_id,
            expected_generation=current.generation, operation=None,
            expected_head=head, target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot=current.snapshot,
        )

    def _publication_intent(self, context, operation, remote_oid):
        checkpoint = self.effects.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.repo, context.plan.plan_id)
        head = self.effects.workspace.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        if current.operation == operation and current.expected_head == head:
            return
        checkpoint.record(
            self.repo, context.plan.plan_id,
            expected_generation=current.generation, operation=operation,
            expected_head=head, target_base_oid=context.active_sha,
            observed_remote_oid=remote_oid,
            snapshot={'phase': 'PUBLISHING', 'head': head,
                      'remote_before': remote_oid},
        )

    def _commit_owned(self, context, title):
        checkpoint = self.effects.checkpoint
        if checkpoint is not None:
            current = checkpoint.get(self.repo, context.plan.plan_id)
            before = self.effects.workspace.git(
                context.worktree, 'rev-parse', 'HEAD'
            ).stdout.strip()
            checkpoint.record(
                self.repo, context.plan.plan_id,
                expected_generation=current.generation, operation='commit',
                expected_head=before, target_base_oid=context.active_sha,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}),
                          'commit_message': title, 'before_head': before},
            )
        result = self.effects.workspace.git(
            context.worktree, 'commit', '-m', title,
        )
        if checkpoint is not None:
            current = checkpoint.get(self.repo, context.plan.plan_id)
            after = self.effects.workspace.git(
                context.worktree, 'rev-parse', 'HEAD'
            ).stdout.strip()
            checkpoint.record(
                self.repo, context.plan.plan_id,
                expected_generation=current.generation, operation=None,
                expected_head=after, target_base_oid=current.target_base_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'after_head': after},
            )
        return result

    def _complete_publication_intent(self, context):
        checkpoint = self.effects.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.repo, context.plan.plan_id)
        if current.operation not in {'push', 'pr_create'}:
            return
        checkpoint.record(
            self.repo, context.plan.plan_id,
            expected_generation=current.generation, operation=None,
            expected_head=current.expected_head,
            target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot=current.snapshot,
        )

    def recover(self, context):
        """Observe owned Git and reconcile stale markers before phase dispatch."""
        checkpoint = self.effects.checkpoint
        if checkpoint is None:
            return context
        try:
            self.effects.state.check_context(context)
            row = self._row()
            catalog = self.effects.state.active_execplan(self.repo)
            catalog_marker = (
                int(catalog['catalog_generation']),
                str(catalog['current_revision_sha'])
                if catalog['current_revision_sha'] is not None else None,
            )
            current = checkpoint.get(self.repo, context.plan.plan_id)
            if self.newly_activated and current.operation is None and current.expected_head:
                # A previously blocked plan can be requeued after its inputs
                # change. Its old execution HEAD does not describe this new
                # activation, whose owned workspace was just prepared.
                current = checkpoint.record(
                    self.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=None, target_base_oid=None,
                    observed_remote_oid=None,
                    snapshot={'reactivated_from_head': current.expected_head},
                )
            current = checkpoint.record(
                self.repo, context.plan.plan_id,
                expected_generation=current.generation,
                operation=current.operation,
                expected_head=current.expected_head,
                target_base_oid=current.target_base_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'fetch_in_progress': True},
            )
            base_oid = self._fetch_latest_base()
            current = checkpoint.record(
                self.repo, context.plan.plan_id,
                expected_generation=current.generation,
                operation=current.operation,
                expected_head=current.expected_head,
                target_base_oid=current.target_base_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'fetch_in_progress': False,
                          'fetched_base_oid': base_oid},
            )
            remote_oid = self.effects.workspace.remote_branch_oid(self.repo_path, context.branch)
            # A PR needs a remote branch.  With neither a remote branch nor a
            # recorded PR there is no publication state to reconcile here;
            # the publication service will query again before creating one.
            inspect_pr = remote_oid is not None or bool(row['pr_url'])
            pull_request = (
                self.effects.publication.pr_for_branch(
                    self.repo_path, context.branch, str(row['pr_url'] or '') or None
                ) if inspect_pr else None
            )
            snapshot = observe(
                plan_id=context.plan.plan_id,
                row={**row, 'phase': context.phase.value},
                definition_revision=catalog_marker[1],
                base_oid=base_oid, branch=context.branch,
                worktree=context.worktree, owned=True,
                git=self.effects.workspace.git,
                rebase_in_progress=self.effects.workspace._rebase_in_progress_for_branch,
                remote_oid=remote_oid, pull_request=pull_request,
                rebase_intent=(
                    current.operation == 'rebase'
                    or bool(
                        row['pr_conflict_recovery'] and row['pr_conflict_base_oid']
                        and row['phase'] == 'WORKING'
                        and (
                            row['last_error'] == self.effects.contract.REBASE_IN_PROGRESS_ERROR
                            or str(row['last_error'] or '').startswith(
                                self.effects.contract.REBASE_CONFLICT_PREFIX
                            )
                        )
                    )
                ),
                rebase_before_head=(current.expected_head or row['pr_conflict_head_oid']),
                rebase_target_oid=(current.target_base_oid or row['pr_conflict_base_oid']),
                stash_oid=(current.snapshot or {}).get('stash_oid'),
                recorded_local_head=current.expected_head,
                pending_operation=current.operation,
                operation_markers=(
                    self.effects.workspace.git_operation_entries(context.worktree)
                    if self.effects.workspace.git_operation_entries else None
                ),
            )
            stable_base = self.effects.workspace.git(
                self.repo_path, 'rev-parse', f'origin/{self.base}'
            ).stdout.strip()
            stable_remote = self.effects.workspace.remote_branch_oid(
                self.repo_path, context.branch
            )
            stable_pr = (
                self.effects.publication.pr_for_branch(
                    self.repo_path, context.branch, str(row['pr_url'] or '') or None
                ) if inspect_pr else None
            )
            if (stable_base != base_oid or stable_remote != remote_oid
                    or stable_pr != pull_request):
                return self._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                     'checkpoint observation changed', context=context)
            current_catalog = self.effects.state.active_execplan(self.repo)
            if (
                int(current_catalog['catalog_generation']),
                str(current_catalog['current_revision_sha'])
                if current_catalog['current_revision_sha'] is not None else None,
            ) != catalog_marker:
                return self._recover(
                    RecoveryReason.REMOTE_SNAPSHOT_RACE,
                    'checkpoint catalog revision changed', context=context,
                )
            self.effects.state.check_context(context)
            if (current.operation == 'push' and remote_oid == snapshot.local_head
                    or current.operation == 'pr_create' and pull_request is not None):
                current = checkpoint.record(
                    self.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=snapshot.local_head,
                    target_base_oid=current.target_base_oid,
                    observed_remote_oid=remote_oid,
                    snapshot=current.snapshot,
                )
            current = checkpoint.record(
                self.repo, context.plan.plan_id,
                expected_generation=current.generation,
                operation=current.operation,
                expected_head=current.expected_head or snapshot.local_head,
                target_base_oid=current.target_base_oid,
                observed_remote_oid=remote_oid,
                snapshot={**(current.snapshot or {}), **asdict(snapshot)},
            )
            self.effects.state.check_context(context)
            decision = assess(snapshot)
            if (current.operation == 'rebase'
                    and decision.action not in {CheckpointAction.USER_ACTION, CheckpointAction.PAUSE}
                    and (current.snapshot or {}).get('pending') == 'stage'
                    and (current.snapshot or {}).get('worker_result')
                    and not snapshot.unmerged_stages):
                # Staging may have completed before its completion marker was
                # saved. Reuse the accepted correction instead of rerunning
                # the Worker, including an autostash without rebase metadata.
                resumed = self._finish_worker(
                    replace(context, resolving_rebase=True), row,
                    current.snapshot['worker_result'],
                )
                return (resumed.context
                        if resumed.kind is ExecutionOutcomeKind.PHASE_ADVANCED
                        else resumed)
            if (current.operation == 'rebase'
                    and decision.action not in {CheckpointAction.USER_ACTION, CheckpointAction.PAUSE}
                    and (current.snapshot or {}).get('pending') == 'continue'
                    and snapshot.git_operation == 'rebase'
                    and not snapshot.unmerged_stages):
                staged = self.effects.workspace.git(
                    context.worktree, 'diff', '--cached', '--quiet', check=False
                )
                if staged.returncode not in {0, 1}:
                    return self._recover(RecoveryReason.STAGE_FAILED,
                                         'staged rebase correction observation failed',
                                         context=context)
                arguments = (
                    ('rebase', '--skip') if staged.returncode == 0 else
                    ('-c', 'core.editor=true', 'rebase', '--continue')
                )
                continued = self.effects.workspace.git(
                    context.worktree, *arguments, check=False
                )
                conflict = self._post_rebase_observation(
                    context, str(current.target_base_oid or base_oid),
                    continued.stdout,
                )
                if conflict is not None:
                    return conflict
                if continued.returncode:
                    return self._recover(RecoveryReason.REBASE_CONTINUE_FAILED,
                                         continued.stdout[-8000:], context=context)
                self.effects.state.correct_checkpoint(
                    phase='VERIFYING',
                    active_sha=str(current.target_base_oid or base_oid),
                    validated_base_sha=None, invalidate_validation=True,
                    expected_catalog=catalog_marker,
                )
                self._complete_rebase_intent(context)
                return self._context()
            if decision.action is CheckpointAction.CONTINUE:
                return context
            if decision.action is CheckpointAction.CONFLICT:
                # A correction already in progress owns its diagnostic and
                # Worker session.  Re-observation must not replace the
                # actionable failure with a generic index description.
                if context.resolving_rebase:
                    return context
                self.effects.state.update_project(
                    self.repo, phase='WORKING', validated_base_sha=None,
                    revision_resume_status='VERIFYING',
                    last_error=self.effects.contract.REBASE_CONFLICT_PREFIX + decision.reason,
                )
                return self._context()
            if decision.action is CheckpointAction.CORRECT:
                self.effects.state.correct_checkpoint(
                    phase=decision.phase, active_sha=decision.active_sha,
                    validated_base_sha=decision.validated_base_sha,
                    invalidate_validation=decision.invalidate_validation,
                    expected_catalog=catalog_marker,
                )
                checkpoint.record(
                    self.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=None,
                    expected_head=snapshot.local_head,
                    target_base_oid=base_oid, observed_remote_oid=remote_oid,
                    snapshot=asdict(snapshot),
                )
                return self._context()
            if decision.action is CheckpointAction.REBASE:
                # The revision gate decides which artifacts a changed base
                # invalidates.  VERIFYING also owns its verify-before-rebase
                # contract.  Starting rebase during preparation skips both.
                return context
            if decision.action is CheckpointAction.PAUSE:
                return self._recover(RecoveryReason.STATUS_FAILED, decision.reason,
                                     context=context, attempts=3)
            if decision.reason in {'external_remote_oid', 'external_pr_head'}:
                return self._recover(
                    RecoveryReason.REMOTE_OID_UNCERTAIN,
                    '관측하지 않은 원격 branch 또는 PR head 갱신을 거부합니다: '
                    + decision.reason,
                    context=context,
                )
            return self._recover(
                RecoveryReason.OWNERSHIP_UNCERTAIN, decision.reason,
                context=context,
                final_block_reason=BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN,
            )
        except CheckpointRace:
            return self._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                 'checkpoint generation changed', context=context)
        except RuntimeStateError as error:
            return ExecutionOutcome(
                ExecutionOutcomeKind.TRANSIENT_PAUSE,
                detail=f'execution row changed during checkpoint observation: {error}',
            )
        except RecoveryFailure as error:
            return self._recover(error.reason, str(error), context=context)
        except (RuntimeError, OSError) as error:
            return self._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                str(error), context=context,
            )

    @staticmethod
    def _cached_plan(catalog_plan):
        return {
            'id': str(catalog_plan['plan_id']),
            'title': str(catalog_plan['title']),
            'sources': list(json.loads(str(catalog_plan['sources']))),
            'depends_on': list(json.loads(str(catalog_plan['depends_on']))),
            'complexity': str(catalog_plan['current_complexity'] or 'low'),
            'risk': str(catalog_plan['current_risk'] or 'low'),
            'priority': str(catalog_plan['priority']),
        }

    def prepare(self):
        row = self._row()
        branch = str(row['branch'] or '')
        worktree = Path(str(row['worktree'] or ''))
        plan_id = self.effects.workspace._plan_id_from_path(str(row['plan_path'] or ''))
        catalog_plan = self.effects.state.active_execplan(self.repo)
        self.catalog_plan = catalog_plan
        workspace_ownership = int(catalog_plan['workspace_owned'] if catalog_plan else 0)
        workspace_owned = workspace_ownership == self.effects.contract.WORKSPACE_OWNED
        workspace_reserved = workspace_ownership == self.effects.contract.WORKSPACE_RESERVED
        try:
            resolving_rebase = self._resolving_rebase(row)
        except (RecoveryFailure, RuntimeError, OSError) as error:
            return self._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                str(error),
            )
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
            self._workspace_boundary(plan_id, row, pending=True)
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
            self._workspace_boundary(plan_id, row, pending=False)
        except self.effects.contract.WorkspaceOwnershipError as error:
            workspace_error = f"관리 workspace 소유권/복구 검증 실패: {error}"
            outcome = self._recover(
                RecoveryReason.OWNERSHIP_UNCERTAIN, workspace_error,
                from_revision=True,
            )
            self.effects.events.operation_print(workspace_error, file=sys.stderr)
            return outcome
        except RecoveryFailure as error:
            return self._recover(error.reason, str(error))
        except (RuntimeError, OSError) as error:
            detail = f"관리 workspace 복구 재시도: {error}"
            self.effects.events.operation_print(detail, file=sys.stderr)
            return self._recover(
                classify_exception(error, operation=RecoveryReason.WORKTREE_RECOVERY_FAILED),
                detail,
            )
        if int(row['pr_conflict_recovery']):
            recovery_rebase = self.effects.workspace.resume_pr_conflict_rebase(self.repo_path, self.repo, row, worktree, branch)
            if recovery_rebase == 1:
                return self._final()
            if recovery_rebase == self.effects.contract.PROJECT_STEP_RETRY:
                return self._recover(
                    RecoveryReason.REBASE_CONTINUE_FAILED,
                    str(self._row()['last_error'] or 'PR rebase 복구 재시도'),
                )
            row = self._row()
            resolving_rebase = self._resolving_rebase(row)
        rebase_error = str(row['last_error'] or '')
        rebase_recovery = bool(
            row['phase'] == 'WORKING' and (rebase_error == self.effects.contract.REBASE_IN_PROGRESS_ERROR or rebase_error.startswith(self.effects.contract.REBASE_CONFLICT_PREFIX)),
        )
        if rebase_recovery and (not resolving_rebase) and (not int(row['pr_conflict_recovery'])):
            resume_status = str(row['revision_resume_status'] or 'VERIFYING')
            resume_phase = 'WORKING' if resume_status == 'IMPLEMENTING' else resume_status
            self.effects.state.update_project(
                self.repo,
                phase=resume_phase,
                revision_resume_status=None,
                last_error=None,
            )
            row = self._row()
        assert catalog_plan is not None
        self.plan = self._cached_plan(catalog_plan)
        self.level = self.effects.validation.ensure_worker_level_state(
            self.repo, self.plan
        )
        self.catalog_plan = catalog_plan
        return self._context()

    @staticmethod
    def _project_phase(status):
        return 'WORKING' if status == 'IMPLEMENTING' else status

    def _revision_state_values(
        self, snapshot, decision, latest_sha, *, legacy_baseline=False
    ):
        row = self._row()
        target = decision.phase
        values = {
            'definition_snapshot': snapshot.serialize(),
            'phase': self._project_phase(target),
            'revision_resume_status': None,
        }
        if decision.kind is RevisionGateKind.REFRESH_WORKTREE:
            # The refresh intent marker is crash recovery state. Once rebase
            # succeeds it must not be mistaken for an unfinished legacy
            # latest-base rebase on the next process step.
            values['last_error'] = None
        if legacy_baseline:
            artifact_base = str(
                row['validated_base_sha'] or row['active_sha'] or latest_sha
            )
            if row['worker_result'] is not None:
                values['worker_input_token'] = snapshot.worker_token
                values['archive_token'] = snapshot.archive_token
            if row['verify_result'] is not None:
                values['verify_token'] = snapshot.verify_token(artifact_base)
            if row['review_result'] is not None:
                values['review_token'] = snapshot.review_token(artifact_base)
            if row['publication_token'] is not None:
                values['publication_token'] = snapshot.publication_token(
                    artifact_base
                )
        if target == 'IMPLEMENTING' and (
            decision.kind is RevisionGateKind.INVALIDATE_TO_PHASE
            or str(row['phase']) != 'WORKING'
            or bool(decision.deltas & WORKER_INVALIDATING_DELTAS)
        ):
            values.update(
                validated_base_sha=None,
                worker_result=None,
                verify_result=None,
                review_result=None,
                worker_input_token=None,
                archive_token=None,
                verify_token=None,
                review_token=None,
                publication_token=None,
                last_error=None,
            )
        elif target == 'VERIFYING' and (
            decision.kind is RevisionGateKind.INVALIDATE_TO_PHASE
            or str(row['phase']) not in {'WORKING', 'VERIFYING'}
        ):
            values.update(
                validated_base_sha=None,
                verify_result=None,
                review_result=None,
                verify_token=None,
                review_token=None,
                publication_token=None,
                last_error=None,
            )
        elif target == 'REVIEWING' and (
            decision.kind is RevisionGateKind.INVALIDATE_TO_PHASE
            or str(row['phase']) == 'PUBLISHING'
        ):
            values.update(
                review_result=None,
                review_token=None,
                publication_token=None,
                last_error=None,
            )
        if decision.preserve_validation:
            if row['verify_result'] is not None:
                values['validated_base_sha'] = latest_sha
                values['verify_token'] = snapshot.verify_token(latest_sha)
            if row['review_result'] is not None:
                values['review_token'] = snapshot.review_token(latest_sha)
            if row['publication_token'] is not None:
                values['publication_token'] = snapshot.publication_token(latest_sha)
        return values

    def _install_revision_state(
        self, snapshot, decision, latest_sha, *, legacy_baseline=False
    ):
        self.effects.state.update_project(
            self.repo,
            **self._revision_state_values(
                snapshot,
                decision,
                latest_sha,
                legacy_baseline=legacy_baseline,
            ),
        )
        self._definition_snapshot = snapshot
        self.catalog_plan = self.effects.state.active_execplan(self.repo)
        assert self.catalog_plan is not None
        self.plan = self._cached_plan(self.catalog_plan)

    def _refresh_revision_base(
        self,
        context,
        snapshot,
        decision,
        latest_sha,
        *,
        legacy_baseline=False,
    ):
        row = self._row()
        if context.phase is ExecutionPhase.PUBLISHING and not int(
            row['pr_conflict_recovery']
        ):
            remote_oid = self.effects.workspace.remote_branch_oid(
                self.repo_path, context.branch
            )
            try:
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
                self._block_final(
                    self.repo,
                    context.worktree,
                    publication_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
                )
                self.effects.events.operation_print(
                    publication_error, file=sys.stderr
                )
                return self._final(publication_error)
            row = self._row()
        if (
            context.phase is ExecutionPhase.VERIFYING
            and decision.phase == 'VERIFYING'
            and self.effects.workspace.git(
                context.worktree, 'status', '--porcelain'
            ).stdout.strip()
        ):
            # Preserve the uncommitted Worker patch as a local checkpoint so
            # a conflicting base rebase remains a resumable Git operation.
            worker_publication = self.effects.validation.parse_worker_result(
                str(row['worker_result'] or ''), self.plan
            )
            self.plan = self.effects.validation.validate_active_execplan_metadata(
                self.repo_path,
                context.worktree,
                context.plan.path,
                self.catalog_plan,
            )
            self.effects.workspace.git(context.worktree, 'add', '-A')
            self._commit_owned(context, worker_publication.commit_title)
        refresh_values = {
            'phase': 'WORKING',
            'validated_base_sha': None,
            'revision_resume_status': decision.phase,
            'last_error': self.effects.contract.REBASE_IN_PROGRESS_ERROR,
        }
        if int(row['pr_conflict_recovery']):
            refresh_values['pr_conflict_base_oid'] = latest_sha
        self.effects.state.update_project(self.repo, **refresh_values)
        self._record_rebase_intent(context, latest_sha)
        rebased = self.effects.workspace.git(
            context.worktree,
            'rebase',
            '--autostash',
            latest_sha,
            check=False,
        )
        conflict = self._post_rebase_observation(context, latest_sha, rebased.stdout)
        if conflict is not None:
            return conflict
        if rebased.returncode:
            rebase_failure = self.effects.contract.REBASE_CONFLICT_PREFIX + rebased.stdout[-8000:]
            in_progress = self.effects.workspace._rebase_in_progress_for_branch(
                context.worktree, context.branch
            )
            if in_progress:
                self.effects.state.update_project(
                    self.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    last_error=rebase_failure,
                )
            self.effects.events.operation_print(rebase_failure, file=sys.stderr)
            return self._recover(
                RecoveryReason.REBASE_CONFLICT if in_progress else RecoveryReason.WORKTREE_RECOVERY_FAILED,
                rebase_failure, context=context,
            )
        self.effects.state.update_project(self.repo, active_sha=latest_sha)
        self._complete_rebase_intent(context)
        if (
            decision.phase != 'IMPLEMENTING'
            and not self.effects.workspace.branch_has_changes(
                context.worktree, self.base
            )
        ):
            prefix = (
                '게시 직전 '
                if context.phase is ExecutionPhase.PUBLISHING
                else ''
            )
            no_change_error = (
                f'{prefix}rebase 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
            )
            self._block_final(
                self.repo,
                context.worktree,
                no_change_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
                recovery_reason=RecoveryReason.NO_CHANGE,
            )
            self.effects.events.operation_print(no_change_error, file=sys.stderr)
            return self._final(no_change_error)
        self._install_revision_state(
            snapshot,
            decision,
            latest_sha,
            legacy_baseline=legacy_baseline,
        )
        return self._retry('최신 ExecPlan revision을 관리 worktree에 반영했습니다.')

    def _revision_error(self, context, detail):
        self._block_final(
            self.repo,
            context.worktree,
            detail,
            str(self._row()['verify_result'] or ''),
            reason=BlockReasonCode.PLAN_INPUT_INVALID,
            user_targets=(context.plan.path,),
        )
        self.effects.events.operation_print(detail, file=sys.stderr)
        return self._final(detail)

    def _archive_collision(self, context, error):
        return self._recover(
            RecoveryReason.PLAN_INPUT_INVALID, str(error), context=context,
            user_targets=(error.path,),
        )

    def _archive_workspace_owned(self, context):
        active = self.effects.state.active_execplan(self.repo)
        if not (
            active is not None
            and str(active['plan_id']) == context.plan.plan_id
            and int(active['workspace_owned']) == self.effects.contract.WORKSPACE_OWNED
            and str(active['branch'] or '') == context.branch
            and str(active['worktree'] or '') == str(context.worktree)
            and context.plan.plan_sha
        ):
            return False
        try:
            self.effects.workspace.validate_managed_worktree(
                self.repo_path, context.worktree, context.branch
            )
            return self.effects.workspace.git(
                context.worktree, 'merge-base', '--is-ancestor',
                context.plan.plan_sha, 'HEAD', check=False,
            ).returncode == 0
        except self.effects.contract.WorkspaceOwnershipError:
            return False
        except self.effects.contract.GitLockError as error:
            raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
        except (RuntimeError, OSError) as error:
            raise RecoveryFailure(RecoveryReason.STATUS_FAILED, str(error)) from error

    def guard(self, context):
        """Refresh and reconcile before any phase effect consumes its snapshot."""

        self.effects.state.check_context(context)
        latest_sha = self._fetch_latest_base()
        try:
            result = self.effects.revision.reconcile_catalog(
                self.repo_path, self.repo, self.base
            )
        except self.effects.contract.GitLockError as error:
            return self._recover(RecoveryReason.GIT_LOCK, str(error), context=context)
        except (RuntimeError, OSError) as error:
            return self._recover(
                RecoveryReason.STATUS_FAILED, str(error), context=context
            )
        kind = getattr(getattr(result, 'kind', None), 'value', None)
        if kind == 'stale':
            return self._recover(
                RecoveryReason.CATALOG_CACHE_STALE,
                'ExecPlan catalog revision이 변경되어 다시 평가합니다.',
                context=context,
            )
        if kind == 'invalid-catalog':
            diagnostic = getattr(result, 'diagnostic', None)
            detail = str(getattr(diagnostic, 'message', None) or '원격 ExecPlan catalog가 유효하지 않습니다.')
            return self._recover(
                RecoveryReason.CATALOG_INVALID, detail, context=context,
                user_targets=tuple(
                    path for path in (getattr(diagnostic, 'path', None),
                                      getattr(diagnostic, 'source', None)) if path
                ),
            )
        if str(getattr(result, 'revision_sha', '') or '') != latest_sha:
            return self._recover(
                RecoveryReason.REMOTE_SNAPSHOT_RACE,
                '원격 base와 reconciled ExecPlan revision이 달라 다시 평가합니다.',
                context=context,
            )

        catalog_plan = self.effects.state.active_execplan(self.repo)
        if catalog_plan is None:
            return ExecutionOutcome(
                ExecutionOutcomeKind.OPERATIONAL_ERROR,
                detail='revision gate에서 활성 ExecPlan을 찾을 수 없습니다.',
                error=True,
            )
        self.catalog_plan = catalog_plan
        self.plan = self._cached_plan(catalog_plan)
        try:
            archive_sources = self.effects.archive.requirement_archive_sources(
                self.repo
            )
            archive_snapshot = (
                self.effects.archive.requirement_archive_snapshot(
                    self.repo_path,
                    self.repo,
                    latest_sha,
                    str(catalog_plan['plan_id']),
                )
                if archive_sources else None
            )
            archive_candidates = (
                archive_snapshot.candidates
                if archive_snapshot is not None else ()
            )
            snapshot = DefinitionSnapshot(
                revision_sha=latest_sha,
                path=str(catalog_plan['path']),
                title=str(catalog_plan['title']),
                sources=tuple(json.loads(str(catalog_plan['sources']))),
                depends_on=tuple(json.loads(str(catalog_plan['depends_on']))),
                complexity=str(catalog_plan['current_complexity'] or self.plan['complexity']),
                risk=str(catalog_plan['current_risk'] or self.plan['risk']),
                priority=str(catalog_plan['priority']),
                body_hash=self.effects.revision.body_hash(
                    self.repo_path, latest_sha, str(catalog_plan['path'])
                ),
                source_fingerprint=self.effects.revision.source_fingerprint(
                    self.repo_path, latest_sha, catalog_plan
                ),
                archive_candidates=tuple(sorted(archive_candidates)),
            )
            previous = DefinitionSnapshot.deserialize(
                self._row()['definition_snapshot']
            )
            unmet = self.effects.revision.unmet_dependencies(
                self.repo, str(catalog_plan['plan_id'])
            )
            changed_paths = self.effects.revision.changed_paths(
                self.repo_path,
                str(self._row()['active_sha'] or ''),
                latest_sha,
            )
        except RecoveryFailure:
            raise
        except self.effects.contract.GitLockError as error:
            return self._recover(RecoveryReason.GIT_LOCK, str(error), context=context)
        except self.effects.contract.RequirementArchiveCollisionError as error:
            return self._archive_collision(context, error)
        except self.effects.contract.RequirementArchiveError as error:
            if self._fetch_latest_base() != latest_sha:
                return self._recover(
                    RecoveryReason.REMOTE_SNAPSHOT_RACE,
                    'Requirement archive 계산 중 원격 base가 변경됐습니다.',
                    context=context,
                )
            return self._revision_error(
                context, f"ExecPlan revision gate 확인 실패: {error}"
            )
        except (RuntimeError, ValueError) as error:
            return self._revision_error(
                context, f"ExecPlan revision gate 확인 실패: {error}"
            )

        row = self._row()
        base_requires_validation = any(
            not path.startswith('docs/plans/') for path in changed_paths
        )
        active_sha = str(row['active_sha'] or '')
        legacy_input_paths = {
            str(catalog_plan['path']),
            *(str(item) for item in json.loads(str(catalog_plan['sources']))),
        }
        legacy_definition_changed = any(
            path in legacy_input_paths
            or path.startswith('docs/plans/')
            or (bool(archive_sources) and path.startswith('docs/requirements/'))
            for path in changed_paths
        )
        legacy_baseline = bool(
            previous is None
            and active_sha
            and not legacy_definition_changed
        )
        artifact_base = str(
            row['validated_base_sha'] or row['active_sha'] or latest_sha
        )
        decision = decide_revision_gate(
            phase=ExecutionPhase(str(catalog_plan['status'])).value,
            # Rows created before G-037 have valid phase artifacts but no
            # revision tokens. Adopt the reconciled definition as their
            # baseline, then let real base and definition deltas invalidate
            # only the stages that must run again.
            previous=snapshot if legacy_baseline else previous,
            current=snapshot,
            active_base_token=active_sha,
            current_base_token=latest_sha,
            artifacts=StageArtifacts(
                worker_result=row['worker_result'] is not None,
                verify_result=row['verify_result'] is not None,
                review_result=row['review_result'] is not None,
                worker_input_token=(
                    snapshot.worker_token
                    if legacy_baseline and row['worker_result'] is not None
                    else str(row['worker_input_token'])
                    if row['worker_input_token'] is not None
                    else None
                ),
                archive_token=(
                    snapshot.archive_token
                    if legacy_baseline and row['worker_result'] is not None
                    else str(row['archive_token'])
                    if row['archive_token'] is not None
                    else None
                ),
                verify_token=(
                    snapshot.verify_token(artifact_base)
                    if legacy_baseline and row['verify_result'] is not None
                    else str(row['verify_token'])
                    if row['verify_token'] is not None
                    else None
                ),
                review_token=(
                    snapshot.review_token(artifact_base)
                    if legacy_baseline and row['review_result'] is not None
                    else str(row['review_token'])
                    if row['review_token'] is not None
                    else None
                ),
                publication_token=(str(row['publication_token']) if row['publication_token'] is not None else None),
            ),
            unmet_dependencies=tuple(unmet),
            base_requires_validation=base_requires_validation,
        )
        self.archive_sources = tuple(archive_sources)
        self.archive_candidates = tuple(archive_candidates)
        self.archive_scope_sources = tuple(sorted(set(archive_candidates)))
        self._archive_snapshot = archive_snapshot

        if decision.kind is RevisionGateKind.FINAL_SAFETY_ERROR:
            return self._revision_error(context, str(decision.detail))
        if decision.kind is RevisionGateKind.WAIT:
            # A conflicted rebase cannot accept a second refresh or clear its
            # recovery marker. Still reconcile dependencies before Worker.
            if not context.resolving_rebase:
                self._install_revision_state(
                    snapshot,
                    decision,
                    latest_sha,
                    legacy_baseline=legacy_baseline,
                )
            self.effects.events.operation_log(str(decision.detail))
            return ExecutionOutcome(
                ExecutionOutcomeKind.STABLE_WAIT,
                context=context if context.resolving_rebase else self._context(),
                detail=decision.detail,
            )
        if context.resolving_rebase:
            if (
                (previous is None and not legacy_baseline)
                or decision.deltas & (
                    WORKER_INVALIDATING_DELTAS - {DefinitionDelta.DEPENDENCIES}
                )
            ):
                detail = (
                    'rebase 충돌 복구 중 ExecPlan 구현 입력이 변경되어 기존 '
                    'Worker 변경과 안전하게 결합할 수 없습니다.'
                )
                self._block_final(
                    self.repo,
                    context.worktree,
                    detail,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
                )
                self.effects.events.operation_print(detail, file=sys.stderr)
                return self._final(detail)
            if DefinitionDelta.DEPENDENCIES in decision.deltas:
                # Dependency readiness was checked above. Resolve the existing
                # conflict first, but retain the applied definition so the next
                # gate refreshes the worktree and reruns Worker on the new DAG.
                self.effects.state.update_project(
                    self.repo,
                    validated_base_sha=None,
                    worker_result=None,
                    verify_result=None,
                    review_result=None,
                    worker_input_token=None,
                    archive_token=None,
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                )
            # The ongoing Git rebase remains pinned to its original target.
            # Do not install the new stage state until it has been resolved.
            self._definition_snapshot = snapshot
            self.level = self.effects.validation.ensure_worker_level_state(
                self.repo, self.plan
            )
            return self._context()
        if str(row['active_sha'] or '') != latest_sha:
            if self._archive_snapshot is not None:
                try:
                    workspace_owned = self._archive_workspace_owned(context)
                except RecoveryFailure as error:
                    return self._recover(error.reason, str(error), context=context)
                if not workspace_owned:
                    return self._recover(
                        RecoveryReason.OWNERSHIP_UNCERTAIN,
                        'Requirement archive 관리 worktree와 ExecPlan HEAD의 소유권을 확인할 수 없습니다.',
                        context=context,
                    )
                repository = ArchiveTargetRepository(self.effects.state.connection)
                previous_target = repository.get(self.repo, str(self.plan['id']))
                try:
                    repository.put(
                        self.repo, self._archive_snapshot,
                        expected_execution=self.effects.state.expected,
                        expected_generation=(
                            previous_target.generation if previous_target else None
                        ),
                        base_is_current=lambda oid: self._fetch_latest_base() == oid,
                        snapshot_is_current=lambda: (
                            self.effects.archive.requirement_archive_snapshot(
                                self.repo_path, self.repo, latest_sha,
                                str(self.plan['id']),
                            ).token == self._archive_snapshot.token
                        ),
                        stage='OBSERVED',
                    )
                except ArchiveTargetConflict:
                    return self._recover(
                        RecoveryReason.REMOTE_SNAPSHOT_RACE,
                        'Requirement archive rebase 대상 snapshot이 변경됐습니다.',
                        context=context,
                    )
                except self.effects.contract.RequirementArchiveCollisionError as error:
                    return self._archive_collision(context, error)
                except self.effects.contract.RequirementArchiveError as error:
                    return self._recover(
                        RecoveryReason.CATALOG_INVALID, str(error), context=context
                    )
            return self._refresh_revision_base(
                context,
                snapshot,
                decision,
                latest_sha,
                legacy_baseline=legacy_baseline,
            )
        self._install_revision_state(
            snapshot,
            decision,
            latest_sha,
            legacy_baseline=legacy_baseline,
        )
        if decision.kind is RevisionGateKind.INVALIDATE_TO_PHASE:
            return self._retry('ExecPlan revision 변경으로 필요한 실행 단계부터 재개합니다.')
        try:
            self.plan = self.effects.validation.validate_active_execplan_metadata(
                self.repo_path,
                context.worktree,
                str(self._row()['plan_path']),
                self.catalog_plan,
            )
            self.level = self.effects.validation.ensure_worker_level_state(
                self.repo, self.plan
            )
            if self.archive_sources and self._row()['phase'] == 'WORKING':
                self.archive_scope_sources = tuple(
                    sorted(
                        set(self.archive_candidates)
                        | set(
                            self.effects.archive.requirement_archive_touched_sources(
                                self.repo_path,
                                latest_sha,
                                context.worktree,
                            )
                        )
                    )
                )
        except RecoveryFailure:
            raise
        except RuntimeError as error:
            return self._revision_error(context, str(error))
        if self._archive_snapshot is not None:
            def require_next_guard(_base):
                raise RecoveryFailure(
                    RecoveryReason.BASE_BEHIND,
                    'Requirement archive 복구 중 새 원격 base를 관측했습니다.',
                )

            def reconcile_catalog(_base):
                try:
                    return self.effects.revision.reconcile_catalog(
                        self.repo_path, self.repo, self.base
                    )
                except self.effects.contract.GitLockError as error:
                    raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error

            def applied_base():
                try:
                    return self.effects.workspace.git(
                        context.worktree, 'merge-base', 'HEAD',
                        f'origin/{self.base}',
                    ).stdout.strip()
                except self.effects.contract.GitLockError as error:
                    raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
                except RuntimeError as error:
                    raise RecoveryFailure(RecoveryReason.STATUS_FAILED, str(error)) from error

            def calculate_snapshot(oid):
                try:
                    return self.effects.archive.requirement_archive_snapshot(
                        self.repo_path, self.repo, oid, str(self.plan['id'])
                    )
                except self.effects.contract.GitLockError as error:
                    raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error

            ports = ArchiveRecoveryPorts(
                fetch_base=self._fetch_latest_base,
                observe_pull_request=lambda: self.effects.publication.pr_for_branch(
                    self.repo_path, context.branch, context.pr_url or ''
                ),
                reconcile_catalog=reconcile_catalog,
                workspace_owned=lambda: self._archive_workspace_owned(context),
                applied_base=applied_base,
                refresh_worktree=require_next_guard,
                calculate_snapshot=calculate_snapshot,
                current_base=self._fetch_latest_base,
                execution_snapshot=lambda: self.effects.state.expected,
                plan_revision=lambda: (
                    str(self.catalog_plan['current_revision_sha'])
                    if self.catalog_plan is not None
                    and self.catalog_plan['current_revision_sha'] is not None
                    else None
                ),
            )
            try:
                recovered = ArchiveRecoveryService(
                    ArchiveTargetRepository(self.effects.state.connection),
                    ports,
                ).run()
            except RecoveryFailure as error:
                return self._recover(
                    error.reason, str(error), context=context
                )
            except self.effects.contract.RequirementArchiveCollisionError as error:
                return self._archive_collision(context, error)
            except self.effects.contract.RequirementArchiveError as error:
                reason = (
                    RecoveryReason.REMOTE_SNAPSHOT_RACE
                    if self._fetch_latest_base() != latest_sha
                    else RecoveryReason.CATALOG_INVALID
                )
                return self._recover(reason, str(error), context=context)
            if recovered.decision is not None:
                return self._recover(
                    recovered.decision.observation.reason,
                    recovered.decision.observation.detail,
                    context=context, attempts=recovered.decision.attempts,
                    user_targets=recovered.decision.observation.user_targets,
                )
            if recovered.snapshot is None or recovered.snapshot.token != self._archive_snapshot.token:
                return self._recover(
                    RecoveryReason.CATALOG_CACHE_STALE,
                    'Requirement archive snapshot이 변경되어 최신 선언으로 다시 평가합니다.',
                    context=context,
                )
        return self._context()

    def _post_effect_guard(self, context):
        # Direct policy unit tests can still exercise a handler without the
        # coordinator. Real execution always installs this snapshot first.
        if self._definition_snapshot is None:
            return context
        return self.guard(self._context())

    def _archive_candidates_at(self, context, revision):
        """Require the archive input used by this phase before reading moves."""

        try:
            if self._archive_snapshot is None:
                return self.effects.archive.requirement_archive_candidates(
                    self.repo_path, self.repo, revision, str(self.plan['id'])
                )
            snapshot = self.effects.archive.requirement_archive_snapshot(
                self.repo_path, self.repo, revision, str(self.plan['id'])
            )
        except self.effects.contract.GitLockError as error:
            raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
        if (
            snapshot.token != self._archive_snapshot.token
            or snapshot.token != context.archive_snapshot_token
        ):
            raise RecoveryFailure(
                RecoveryReason.CATALOG_CACHE_STALE,
                'Requirement archive base/catalog/state snapshot이 변경됐습니다.',
            )
        return snapshot.candidates

    def _record_archive_target(self, stage, *, moves=None):
        if self._archive_snapshot is None:
            return
        snapshot = (
            self._archive_snapshot.with_moves(tuple(moves))
            if moves is not None else self._archive_snapshot
        )
        repository = ArchiveTargetRepository(self.effects.state.connection)
        previous = repository.get(self.repo, str(self.plan['id']))
        try:
            repository.put(
                self.repo, snapshot,
                expected_execution=self.effects.state.expected,
                expected_generation=previous.generation if previous else None,
                base_is_current=lambda oid: self._fetch_latest_base() == oid,
                snapshot_is_current=lambda: (
                    self.effects.archive.requirement_archive_snapshot(
                        self.repo_path, self.repo, snapshot.base_oid,
                        str(self.plan['id']),
                    ).token == snapshot.token
                ),
                stage=stage,
            )
        except ArchiveTargetConflict as error:
            raise RecoveryFailure(
                RecoveryReason.REMOTE_SNAPSHOT_RACE,
                'Requirement archive target 저장 중 snapshot이 변경됐습니다.',
            ) from error
        except self.effects.contract.GitLockError as error:
            raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
        except self.effects.contract.RequirementArchiveError as error:
            raise RecoveryFailure(RecoveryReason.CATALOG_INVALID, str(error)) from error

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
                **({'rebase_definition': DefinitionSnapshot.deserialize(
                    row['definition_snapshot']
                )} if context.resolving_rebase else {}),
                allowed_requirement_archives=self.archive_scope_sources,
                allowed_guidance_targets=guidance_targets,
                allow_common_plans=allow_common_plans,
            )
        except (self.effects.contract.AgentScopeError, self.effects.contract.WorkspaceOwnershipError) as error:
            scope_error = str(error)
            self._block_final(
                self.repo,
                context.worktree,
                scope_error,
                str(row['verify_result'] or ''),
                reason=getattr(error, 'reason_code', BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
            )
            self.effects.events.operation_print(scope_error, file=sys.stderr)
            return self._final(scope_error)
        except self.effects.contract.ExecPlanMetadataError as error:
            return self._metadata_correction(context, error)
        except RuntimeError as error:
            detail = f"Worker workspace 상태 조회 재시도: {error}"
            self.effects.events.operation_print(detail, file=sys.stderr)
            return self._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                detail, context=context,
            )
        self.effects.agent.clear_agent_scope(self.repo)
        guarded = self._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
        row = self._row()
        catalog_plan = self.catalog_plan
        assert catalog_plan is not None
        guidance_blocked = self.effects.agent.inspect_worker_guidance_result(
            self.repo_path,
            self.repo,
            context.worktree,
            catalog_plan,
            message,
        )
        if guidance_blocked:
            blocked = self.effects.state.blocked()
            self.effects.events.operation_print(
                str(blocked['last_error'] if blocked else 'guidance 승인 대기'),
                file=sys.stderr,
            )
            return self._final()
        if code:
            worker_error = message or f"Agent 종료 코드 {code}"
            if result.result_kind is self.effects.contract.AgentResultKind.TRANSIENT_FAILURE:
                worker_error = f"{self.effects.contract.TRANSIENT_AGENT_ERROR_PREFIX} {worker_error}"
                if context.resolving_rebase:
                    worker_error = self.effects.contract.REBASE_CONFLICT_PREFIX + worker_error
                self.effects.state.update_project(self.repo, phase='WORKING', last_error=worker_error)
                self.effects.events.operation_print(worker_error, file=sys.stderr)
                return self._recover(
                    RecoveryReason.SERVICE_UNAVAILABLE, worker_error,
                    context=context, attempts=3,
                )
            elif result.result_kind is self.effects.contract.AgentResultKind.USAGE_LIMIT:
                worker_error = f"{self.effects.contract.USAGE_LIMIT_ERROR_PREFIX} {worker_error}"
                outcome = ExecutionOutcomeKind.USAGE_PAUSE
            elif context.resolving_rebase:
                worker_error = self.effects.contract.REBASE_CONFLICT_PREFIX + worker_error
                self.effects.state.update_project(self.repo, phase='WORKING', last_error=worker_error)
                self.effects.events.operation_print(worker_error, file=sys.stderr)
                return self._recover(
                    RecoveryReason.REBASE_CONFLICT, worker_error,
                    context=context,
                )
            else:
                outcome = ExecutionOutcomeKind.OPERATIONAL_ERROR
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
            return self._recover(
                RecoveryReason.WORKER_CORRECTION, validation_error,
                context=context,
            )
        return self._finish_worker(context, row, message)

    def _finish_worker(self, context, row, message):
        assert self.catalog_plan is not None and self.plan is not None
        latest_sha = str(row['active_sha'] or '')
        if context.resolving_rebase:
            self.effects.state.update_project(self.repo, worker_result=message)
            self._record_rebase_suboperation(
                context, 'stage', worker_result=message,
            )
            self.effects.workspace.git(context.worktree, 'add', '-A')
            if self._unmerged_stages(context.worktree):
                detail = self.effects.contract.REBASE_CONFLICT_PREFIX + 'unmerged index remains after Worker correction'
                self.effects.state.update_project(self.repo, phase='WORKING', last_error=detail)
                return self._recover(RecoveryReason.REBASE_CONFLICT, detail, context=context)
            staged = self.effects.workspace.git(context.worktree, 'diff', '--cached', '--quiet', check=False)
            if staged.returncode not in {0, 1}:
                staging_error = f"rebase 충돌 해결 결과 확인 실패 (git 종료 코드 {staged.returncode})."
                self.effects.events.operation_print(staging_error, file=sys.stderr)
                return self._recover(RecoveryReason.STAGE_FAILED, staging_error,
                                     context=context)
            self._record_rebase_suboperation(context, 'continue')
            rebase_metadata = self.effects.workspace._rebase_in_progress_for_branch(
                context.worktree, context.branch
            )
            if not rebase_metadata:
                # Autostash application can conflict after Git has removed its
                # rebase directory. The resolved index is the original patch.
                continued = type('GitResult', (), {'returncode': 0, 'stdout': ''})()
            elif staged.returncode == 0:
                continued = self.effects.workspace.git(context.worktree, 'rebase', '--skip', check=False)
            else:
                continued = self.effects.workspace.git(
                    context.worktree,
                    '-c', 'core.editor=true', 'rebase', '--continue',
                    check=False,
                )
            post_conflict = self._post_rebase_observation(
                context, str(
                    (self.effects.checkpoint.get(self.repo, context.plan.plan_id).target_base_oid
                     if self.effects.checkpoint is not None else None)
                    or row['pr_conflict_base_oid'] or row['active_sha'] or ''
                ),
                continued.stdout,
            )
            if post_conflict is not None:
                return post_conflict
            if continued.returncode:
                continuation_error = self.effects.contract.REBASE_CONFLICT_PREFIX + continued.stdout[-8000:]
                in_progress = self.effects.workspace._rebase_in_progress_for_branch(context.worktree, context.branch)
                if in_progress:
                    self.effects.state.update_project(self.repo, phase='WORKING', last_error=continuation_error)
                self.effects.events.operation_print(continuation_error, file=sys.stderr)
                return self._recover(
                    RecoveryReason.REBASE_CONFLICT if in_progress else RecoveryReason.REBASE_CONTINUE_FAILED,
                    continuation_error, context=context,
                )
            if not self.effects.workspace.branch_has_changes(context.worktree, self.base):
                no_change_error = 'rebase 충돌 해결 후 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
                self._block_final(
                    self.repo,
                    context.worktree,
                    no_change_error,
                    str(row['verify_result'] or ''),
                    reason=BlockReasonCode.PLAN_INPUT_INVALID,
                    recovery_reason=RecoveryReason.NO_CHANGE,
                )
                self.effects.events.operation_print(no_change_error, file=sys.stderr)
                return self._final(no_change_error)
            # The conflict may have been resolved on an older rebase target
            # while origin advanced again. Keep the last *proven* base until
            # the next revision gate can refresh the completed worktree.
            latest_sha = (
                str(row['pr_conflict_base_oid'])
                if int(row['pr_conflict_recovery'])
                else str(
                    (self.effects.checkpoint.get(self.repo, context.plan.plan_id).target_base_oid
                     if self.effects.checkpoint is not None else None)
                    or row['active_sha'] or ''
                )
            )
            self.effects.state.update_project(self.repo, active_sha=latest_sha)
            self._complete_rebase_intent(context)
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
                expected_archives = self._archive_candidates_at(
                    context, post_worker_revision
                )
            except RecoveryFailure as error:
                return self._recover(error.reason, str(error), context=context)
            except self.effects.contract.RequirementArchiveCollisionError as error:
                return self._archive_collision(context, error)
            except self.effects.contract.RequirementArchiveError as error:
                archive_error = f"Requirement archive eligibility 확인 실패: {error}"
                if self._fetch_latest_base() != post_worker_revision:
                    return self._recover(
                        RecoveryReason.REMOTE_SNAPSHOT_RACE,
                        archive_error, context=context,
                    )
                outcome = self._recover(
                    RecoveryReason.CATALOG_INVALID,
                    archive_error, context=context,
                )
                self.effects.events.operation_print(archive_error, file=sys.stderr)
                return outcome
            try:
                actual_archives = self.effects.archive.requirement_archive_move_set(
                    self.repo_path,
                    post_worker_revision,
                    context.worktree,
                )
            except self.effects.contract.GitLockError as error:
                return self._recover(RecoveryReason.GIT_LOCK, str(error), context=context)
            except self.effects.contract.RequirementArchiveCollisionError as error:
                return self._archive_collision(context, error)
            except self.effects.contract.RequirementArchiveError as error:
                archive_error = (
                    f"Requirement archive 이동 검증 실패: {error}\n"
                    f"기준 revision: {post_worker_revision}; "
                    f"기대: {json.dumps(expected_archives, ensure_ascii=False)}"
                )
                self.effects.state.update_project(
                    self.repo, phase='WORKING', validated_base_sha=None,
                    worker_result=message,
                    worker_input_token=None, archive_token=None,
                    verify_token=None, review_token=None,
                    publication_token=None, last_error=archive_error,
                )
                self.effects.events.operation_print(
                    'Requirement archive 이동을 같은 Worker 세션에서 다시 확인합니다.',
                    file=sys.stderr,
                )
                try:
                    self._record_archive_target('CORRECTION_PENDING')
                except RecoveryFailure as race:
                    return self._recover(race.reason, str(race), context=context)
                return self._recover(
                    RecoveryReason.ARCHIVE_MOVE_MISMATCH,
                    archive_error, context=context,
                )
            archive_error = self.effects.archive.requirement_archive_reconciliation_error(
                expected_archives, actual_archives,
                revision=post_worker_revision,
            )
            if archive_error is not None:
                self.effects.state.update_project(
                    self.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    worker_result=message,
                    worker_input_token=None,
                    archive_token=None,
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                    last_error=archive_error,
                )
                self.effects.events.operation_print(
                    'Requirement archive 이동 집합을 같은 Worker 세션에서 다시 맞춥니다.',
                    file=sys.stderr,
                )
                try:
                    self._record_archive_target(
                        'CORRECTION_PENDING', moves=actual_archives
                    )
                except RecoveryFailure as error:
                    return self._recover(error.reason, str(error), context=context)
                return self._recover(
                    RecoveryReason.ARCHIVE_MOVE_MISMATCH,
                    archive_error, context=context,
                    fingerprint=hashlib.sha256(
                        json.dumps((post_worker_revision, context.archive_snapshot_token,
                                    sorted(expected_archives or ()), sorted(actual_archives or ()))).encode()
                    ).hexdigest(),
                )
            try:
                self._record_archive_target(
                    'WORKTREE_UPDATED', moves=actual_archives
                )
            except RecoveryFailure as error:
                return self._recover(error.reason, str(error), context=context)
        self.effects.state.update_project(
            self.repo,
            phase='VERIFYING',
            validated_base_sha=None,
            worker_result=message,
            verify_result=None,
            review_result=None,
            worker_input_token=(
                self._definition_snapshot.worker_token
                if self._definition_snapshot is not None else None
            ),
            archive_token=(
                self._definition_snapshot.archive_token
                if self._definition_snapshot is not None else None
            ),
            verify_token=None,
            review_token=None,
            publication_token=None,
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
        except (self.effects.contract.AgentScopeError, self.effects.contract.WorkspaceOwnershipError) as error:
            scope_error = str(error)
            self._block_final(
                self.repo,
                context.worktree,
                scope_error,
                output,
                reason=getattr(error, 'reason_code', BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
            )
            self.effects.events.operation_print(scope_error, file=sys.stderr)
            return (self._final(scope_error), output)
        except self.effects.contract.ExecPlanMetadataError as error:
            return (self._metadata_correction(
                context, error, verify_result=output,
            ), output)
        except RuntimeError as error:
            detail = f"verify workspace 상태 조회 재시도: {error}"
            self.effects.events.operation_print(detail, file=sys.stderr)
            return (self._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                detail, context=context,
            ), output)
        self.effects.agent.clear_agent_scope(self.repo)
        guarded = self._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return (guarded, output)
        context = guarded
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
                return (ExecutionOutcome(
                    ExecutionOutcomeKind.FINAL_BLOCK, detail=output, error=True,
                    recovery=getattr(decision, 'recovery', None),
                ), output)
            return (self._recover(
                RecoveryReason.WORKER_CORRECTION, output,
                context=context, fingerprint=decision.fingerprint,
                correction_limit=self.effects.contract.MAX_VALIDATION_FINGERPRINT_REPEATS + 1,
            ), output)
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
            return self._recover(
                RecoveryReason.WORKER_CORRECTION, validation_error,
                context=context,
            )
        outcome, output = self._verify_once(context)
        if outcome is not None:
            return outcome
        dirty = bool(self.effects.workspace.git(context.worktree, 'status', '--porcelain').stdout.strip())
        if not dirty and (not self.effects.workspace.branch_has_changes(context.worktree, self.base)):
            no_change_error = 'Worker가 변경을 만들지 않아 PR을 생성할 수 없습니다. ExecPlan은 연결된 PR의 Merge가 확인된 경우에만 MERGED가 됩니다.'
            self._block_final(
                self.repo,
                context.worktree,
                no_change_error,
                output,
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
                recovery_reason=RecoveryReason.NO_CHANGE,
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
            self._commit_owned(context, worker_publication.commit_title)
        guarded = self._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
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
        try:
            self.effects.workspace.git(self.repo_path, 'fetch', 'origin')
            return self.effects.workspace.git(
                self.repo_path, 'rev-parse', f"origin/{self.base}"
            ).stdout.strip()
        except self.effects.contract.GitLockError as error:
            raise RecoveryFailure(RecoveryReason.GIT_LOCK, str(error)) from error
        except (RuntimeError, OSError) as error:
            raise RecoveryFailure(RecoveryReason.FETCH_FAILED, str(error)) from error

    def _rebase_latest(self, context, row, latest_sha, *, publishing, evidence):
        """Persist rebase intent, observe Git, then record the applied base."""
        prefix = '게시 직전 ' if publishing else ''
        self.effects.state.update_project(
            self.repo,
            phase='WORKING',
            validated_base_sha=None,
            revision_resume_status='VERIFYING',
            pr_conflict_base_oid=latest_sha if int(row['pr_conflict_recovery']) else row['pr_conflict_base_oid'],
            last_error=self.effects.contract.REBASE_IN_PROGRESS_ERROR,
        )
        self._record_rebase_intent(context, latest_sha)
        rebased = self.effects.workspace.git(context.worktree, 'rebase', latest_sha, check=False)
        conflict = self._post_rebase_observation(context, latest_sha, rebased.stdout)
        if conflict is not None:
            return conflict
        if rebased.returncode:
            in_progress = self.effects.workspace._rebase_in_progress_for_branch(context.worktree, context.branch)
            if in_progress:
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
            self.effects.events.operation_print(
                rebase_failure if publishing else rebased.stdout,
                file=sys.stderr,
            )
            return self._recover(
                RecoveryReason.REBASE_CONFLICT if in_progress else RecoveryReason.WORKTREE_RECOVERY_FAILED,
                rebase_failure, context=context,
            )
        self.effects.state.update_project(
            self.repo,
            active_sha=latest_sha,
            phase='VERIFYING',
            revision_resume_status=None,
        )
        self._complete_rebase_intent(context)
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
                self._commit_owned(context, self._worker_publication.commit_title)
            self.effects.state.update_project(
                self.repo,
                active_sha=latest_sha,
                validated_base_sha=latest_sha,
                verify_result=output,
                verify_token=(
                    self._definition_snapshot.verify_token(latest_sha)
                    if self._definition_snapshot is not None else None
                ),
                review_token=None,
                publication_token=None,
            )
        else:
            self.effects.state.update_project(
                self.repo,
                validated_base_sha=str(row['active_sha'] or ''),
                verify_result=output,
                verify_token=(
                    self._definition_snapshot.verify_token(
                        str(row['active_sha'] or '')
                    )
                    if self._definition_snapshot is not None else None
                ),
                review_token=None,
                publication_token=None,
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
        except RecoveryFailure:
            raise
        except RuntimeError as error:
            metadata_error = str(error)
            self._block_final(
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
            self._block_final(
                self.repo,
                context.worktree,
                no_change_error,
                output,
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
                recovery_reason=RecoveryReason.NO_CHANGE,
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
            except (self.effects.contract.AgentScopeError, self.effects.contract.WorkspaceOwnershipError) as error:
                scope_error = str(error)
                self._block_final(
                    self.repo,
                    context.worktree,
                    scope_error,
                    str(row['verify_result'] or ''),
                    reason=getattr(error, 'reason_code', BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN),
                )
                self.effects.events.operation_print(scope_error, file=sys.stderr)
                return self._final(scope_error)
            except RuntimeError as error:
                detail = f"Reviewer workspace 상태 조회 재시도: {error}"
                self.effects.events.operation_print(detail, file=sys.stderr)
                return self._recover(
                    classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                    detail, context=context,
                )
            self.effects.agent.clear_agent_scope(self.repo)
            guarded = self._post_effect_guard(context)
            if isinstance(guarded, ExecutionOutcome):
                return guarded
            context = guarded
            row = self._row()
            if code:
                reviewer_error = review or f"Reviewer 종료 코드 {code}"
                if review_result.result_kind is self.effects.contract.AgentResultKind.TRANSIENT_FAILURE:
                    reviewer_error = f"{self.effects.contract.TRANSIENT_AGENT_ERROR_PREFIX} {reviewer_error}"
                    self.effects.state.update_project(
                        self.repo, phase='REVIEWING', last_error=reviewer_error,
                        review_result=review,
                    )
                    self.effects.events.operation_print(reviewer_error, file=sys.stderr)
                    return self._recover(
                        RecoveryReason.SERVICE_UNAVAILABLE, reviewer_error,
                        context=context, attempts=3,
                    )
                elif review_result.result_kind is self.effects.contract.AgentResultKind.USAGE_LIMIT:
                    reviewer_error = f"{self.effects.contract.USAGE_LIMIT_ERROR_PREFIX} {reviewer_error}"
                    outcome = ExecutionOutcomeKind.USAGE_PAUSE
                else:
                    outcome = ExecutionOutcomeKind.OPERATIONAL_ERROR
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
                    self._block_final(
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
                return ExecutionOutcome(
                    ExecutionOutcomeKind.FINAL_BLOCK, detail=review, error=True,
                    recovery=getattr(decision, 'recovery', None),
                )
            return self._recover(
                RecoveryReason.WORKER_CORRECTION, review,
                context=context, fingerprint=decision.fingerprint,
                correction_limit=self.effects.contract.MAX_VALIDATION_FINGERPRINT_REPEATS + 1,
            )
        self.effects.validation.clear_validation_failure_history(self.repo, 'review')
        self.effects.state.update_project(
            self.repo,
            phase='PUBLISHING',
            last_error=None,
            review_result=review,
            review_token=(
                self._definition_snapshot.review_token(
                    str(row['validated_base_sha'] or row['active_sha'] or '')
                )
                if self._definition_snapshot is not None else None
            ),
            publication_token=None,
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
            self._block_final(
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
                    self._block_final(
                        self.repo,
                        context.worktree,
                        no_change_error,
                        str(row['verify_result'] or ''),
                        reason=BlockReasonCode.PLAN_INPUT_INVALID,
                        recovery_reason=RecoveryReason.NO_CHANGE,
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
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                    last_error=None,
                )
            else:
                self.effects.state.update_project(
                    self.repo,
                    validated_base_sha=None,
                    phase='VERIFYING',
                    verify_result=None,
                    review_result=None,
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                    last_error=None,
                )
            self.effects.events.operation_print(
                '게시 직전 base가 변경되어 전체 verify와 Review를 다시 수행합니다.',
                file=sys.stderr,
            )
            return self._retry()
        if row['active_sha'] != latest_sha:
            ancestry = self.effects.workspace.git(
                context.worktree, 'merge-base', '--is-ancestor',
                latest_sha, 'HEAD', check=False,
            )
            if ancestry.returncode not in {0, 1}:
                return self._recover(
                    RecoveryReason.STATUS_FAILED,
                    f"게시 checkpoint ancestry 조회 실패: {ancestry.stdout[-8000:]}",
                    context=context,
                )
            if ancestry.returncode == 1:
                outcome = self._rebase_latest(
                    context, row, latest_sha, publishing=True,
                    evidence=str(row['verify_result'] or ''),
                )
                if outcome is not None:
                    return outcome
            self.effects.state.update_project(
                self.repo, active_sha=latest_sha, validated_base_sha=None,
                phase='VERIFYING', verify_result=None, review_result=None,
                verify_token=None, review_token=None, publication_token=None,
                last_error=None,
            )
            return self._recover(
                RecoveryReason.CHECKPOINT_STALE,
                '관리 branch의 실제 HEAD와 최신 base에서 게시 checkpoint를 재구성했습니다.',
                context=context,
            )
        return None

    def _publication_resume(self):
        return PublicationResume(
            self.effects.publication.pr_for_branch,
            self.effects.workspace.remote_branch_oid,
            self.effects.workspace.git,
        )

    def publish(self, context, latest_base):
        self.effects.state.check_context(context)
        row = self._row()
        assert self.catalog_plan is not None and self.plan is not None
        publication = self._publication_resume().observe(
            self.repo_path, context.worktree, context.branch,
            str(row['publication_remote_oid'] or '') or None,
            str(row['pr_url']) if int(row['pr_conflict_recovery']) else None,
        )
        existing_pr = publication.pull_request
        # Owned PR conflict recovery deliberately starts from its saved PR
        # head; its existing lease gate authorizes the later rebased push.
        attachment = (replace(publication, local_oid=str(row['pr_conflict_head_oid'] or ''))
                      if int(row['pr_conflict_recovery']) else publication)
        if assess_publication(attachment) is PublicationNext.USER_ACTION:
            return self._block_final(
                self.repo, context.worktree,
                '원격 branch 또는 PR head가 관측한 소유 OID와 다릅니다.',
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN,
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
                self._block_final(
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
                final_archive_candidates = self._archive_candidates_at(
                    context, latest_sha
                )
            except RecoveryFailure as error:
                return self._recover(error.reason, str(error), context=context)
            except self.effects.contract.RequirementArchiveCollisionError as error:
                return self._archive_collision(context, error)
            except self.effects.contract.RequirementArchiveError as error:
                archive_error = f"게시 직전 Requirement archive eligibility 확인 실패: {error}"
                if self._fetch_latest_base() != latest_sha:
                    return self._recover(
                        RecoveryReason.REMOTE_SNAPSHOT_RACE,
                        archive_error, context=context,
                    )
                outcome = self._recover(
                    RecoveryReason.CATALOG_INVALID,
                    archive_error, context=context,
                )
                self.effects.events.operation_print(archive_error, file=sys.stderr)
                return outcome
            try:
                final_archive_moves = self.effects.archive.requirement_archive_move_set(
                    self.repo_path,
                    latest_sha,
                    context.worktree,
                    tracked_revision='HEAD',
                )
            except self.effects.contract.GitLockError as error:
                return self._recover(RecoveryReason.GIT_LOCK, str(error), context=context)
            except self.effects.contract.RequirementArchiveCollisionError as error:
                return self._archive_collision(context, error)
            except self.effects.contract.RequirementArchiveError as error:
                archive_error = (
                    f"게시 직전 Requirement archive 확인 실패: {error}\n"
                    f"기준 revision: {latest_sha}; "
                    f"기대: {json.dumps(final_archive_candidates, ensure_ascii=False)}"
                )
                self.effects.state.update_project(
                    self.repo, phase='WORKING', validated_base_sha=None,
                    worker_input_token=None, archive_token=None,
                    verify_token=None, review_token=None,
                    publication_token=None, last_error=archive_error,
                )
                self.effects.events.operation_print(
                    '게시 직전 Requirement archive 이동을 다시 확인합니다.',
                    file=sys.stderr,
                )
                try:
                    self._record_archive_target('CORRECTION_PENDING')
                except RecoveryFailure as race:
                    return self._recover(race.reason, str(race), context=context)
                return self._recover(
                    RecoveryReason.ARCHIVE_MOVE_MISMATCH,
                    archive_error, context=context,
                )
            archive_error = self.effects.archive.requirement_archive_reconciliation_error(
                final_archive_candidates,
                final_archive_moves,
                revision=latest_sha,
            )
            if archive_error is not None:
                self.effects.state.update_project(
                    self.repo,
                    phase='WORKING',
                    validated_base_sha=None,
                    worker_input_token=None,
                    archive_token=None,
                    verify_token=None,
                    review_token=None,
                    publication_token=None,
                    last_error=archive_error,
                )
                self.effects.events.operation_print(
                    '게시 직전 Requirement archive 이동 집합을 다시 맞춥니다.',
                    file=sys.stderr,
                )
                try:
                    self._record_archive_target(
                        'CORRECTION_PENDING', moves=final_archive_moves
                    )
                except RecoveryFailure as error:
                    return self._recover(error.reason, str(error), context=context)
                return self._recover(
                    RecoveryReason.ARCHIVE_MOVE_MISMATCH,
                    archive_error, context=context,
                    fingerprint=hashlib.sha256(
                        json.dumps((latest_sha, context.archive_snapshot_token,
                                    sorted(final_archive_candidates or ()), sorted(final_archive_moves or ()))).encode()
                    ).hexdigest(),
                )
            try:
                self._record_archive_target('VALIDATED', moves=final_archive_moves)
            except RecoveryFailure as error:
                return self._recover(error.reason, str(error), context=context)
        if self.effects.agent.validate_guidance_publication(
            self.repo,
            context.worktree,
            self.catalog_plan,
            str(row['worker_result'] or ''),
        ):
            return self._final()
        guarded = self._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
        row = self._row()
        if not self.effects.workspace.branch_has_changes(context.worktree, self.base):
            no_change_error = '게시할 Worker 변경이 남지 않아 PR을 생성할 수 없습니다.'
            self._block_final(
                self.repo,
                context.worktree,
                no_change_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
                recovery_reason=RecoveryReason.NO_CHANGE,
            )
            self.effects.events.operation_print(no_change_error, file=sys.stderr)
            return self._final(no_change_error)
        if self._definition_snapshot is not None:
            self.effects.state.update_project(
                self.repo,
                publication_token=self._definition_snapshot.publication_token(
                    str(self._row()['validated_base_sha'] or '')
                ),
            )
            context = self._context()
        if int(row['pr_conflict_recovery']):
            return self._publish_conflict_recovery(context, row)
        return self._publish_new_pull_request(context, row)

    def _publish_conflict_recovery(self, context, row):
        current_pr = self.effects.publication.pr_for_branch(self.repo_path, context.branch, str(row['pr_url'] or ''))
        if current_pr is None:
            publication_error = '게시 직전 저장된 기존 PR을 다시 조회할 수 없습니다.'
            self._block_final(
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
            self._block_final(
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
            return self._recover(
                classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                publication_error, context=context,
            )
        guarded = self._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
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
        assert self.catalog_plan is not None
        plan_sha = str(
            self.catalog_plan['publication_sha']
            or self.catalog_plan['plan_sha']
            or ''
        )
        publication_path = str(
            self.catalog_plan['publication_path']
            or self.catalog_plan['path']
        )
        if not plan_sha:
            plan_error = '계획 commit SHA가 없습니다.'
            self._block_final(
                self.repo,
                context.worktree,
                plan_error,
                str(row['verify_result'] or ''),
                reason=BlockReasonCode.PLAN_INPUT_INVALID,
            )
            self.effects.events.operation_print(plan_error, file=sys.stderr)
            return self._final(plan_error)
        plan_text = self.effects.workspace.git(
            context.worktree, 'show', f"{plan_sha}:{publication_path}"
        ).stdout
        self.effects.publication.published_execplan(publication_path, plan_text)
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
            return self._recover(
                RecoveryReason.WORKER_CORRECTION, validation_error,
                context=context,
            )
        origin_url = self.effects.workspace.git(context.worktree, 'remote', 'get-url', 'origin').stdout.strip()
        body = self.effects.publication.build_pr_body(
            worker_publication,
            self.plan,
            publication_path,
            plan_text,
            origin_url,
            plan_sha,
        )
        try:
            publication = self._publication_resume().observe(
                self.repo_path, context.worktree, context.branch,
                str(row['publication_remote_oid'] or '') or None,
            )
            action = assess_publication(publication)
            if action is PublicationNext.USER_ACTION:
                raise self.effects.contract.PublicationSafetyError(
                    '게시 직전 원격 branch 또는 PR head가 변경됐습니다.'
                )
            if action is PublicationNext.ATTACH_PR:
                reconciliation = self.effects.publication.reconcile_existing_pull_request(
                    self.repo_path, self.state_dir, self.repo, row,
                    publication.pull_request,
                )
                return (self._step_outcome(reconciliation) if reconciliation is not None
                        else self._retry('기존 PR을 연결했습니다.'))
            if action is PublicationNext.CREATE_PR:
                self.effects.publication.record_remote_publication_branch(
                    self.repo_path, self.state_dir, self.repo,
                    context.branch, context.worktree, publication.remote_oid,
                )
                head_oid = publication.local_oid
            else:
                self._publication_intent(
                    context, 'push', publication.remote_oid,
                )
                head_oid = self.effects.publication.push_managed_publication_branch(
                    self.repo_path, self.state_dir, self.repo,
                    context.branch, context.worktree,
                )
                self._complete_publication_intent(context)
        except self.effects.contract.PublicationSafetyError as error:
            publication_error = str(error)
            self._block_final(
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
            return self._recover(
                classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                publication_error, context=context,
            )
        guarded = self._post_effect_guard(context)
        if isinstance(guarded, ExecutionOutcome):
            return guarded
        context = guarded
        existing_after_push = self.effects.publication.pr_for_branch(
            self.repo_path, context.branch,
        )
        if existing_after_push is not None:
            reconciliation = self.effects.publication.reconcile_existing_pull_request(
                self.repo_path, self.state_dir, self.repo, self._row(),
                existing_after_push,
            )
            return (self._step_outcome(reconciliation) if reconciliation is not None
                    else self._retry('push 뒤 기존 PR을 연결했습니다.'))
        title = f"Implement {self.plan['id']}: {self.plan['title']}"
        try:
            self._publication_intent(context, 'pr_create', head_oid)
            created = self.effects.publication.command(
                ['gh', 'pr', 'create', '--base', self.base, '--head', context.branch, '--title', title, '--body', body],
                cwd=context.worktree,
            )
        except RuntimeError as error:
            publication_error = str(error)
            self.effects.state.update_project(
                self.repo, phase='PUBLISHING', last_error=publication_error,
            )
            self.effects.events.operation_print(publication_error, file=sys.stderr)
            return self._recover(
                classify_exception(error, operation=RecoveryReason.PUBLICATION_RETRY),
                publication_error, context=context,
            )
        url = created.stdout.strip().splitlines()[-1]
        self.effects.state.open_pull_request(self.repo, pr_url=url, pr_head_oid=head_oid)
        self._complete_publication_intent(context)
        self.effects.events.operation_print(f"PR 생성 완료: {url}")
        return ExecutionOutcome(ExecutionOutcomeKind.SUCCESS)
