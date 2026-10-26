"""Rebase execution policy and narrow typed ports."""
from __future__ import annotations
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome
from dataclasses import dataclass, replace
from typing import Callable, Any, Mapping
from pathlib import Path
import json
import os
from loop_manager_internal.git import rebase_file_evidence
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.git import managed_rebase_evidence
from loop_manager_internal.git import managed_rebase_publication
from loop_manager_internal.git.managed_rebase_validation import RebaseStatus
import re
import subprocess
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure, classify_exception
from loop_manager_internal.execution.execution_runtime import ExecutionPlanMetadata
from loop_manager_internal.git.rebase_correction import CorrectionAction, CorrectionSource, execute_git_step, observe_git, stage_evidence_preserved
from loop_manager_internal.storage.rebase_correction_state import CorrectionRace, RebaseCorrectionRepository

from loop_manager_internal.execution.execution_effects import ExecutionContract
from loop_manager_internal.execution.execution_session import ExecutionIdentity, PreparationSnapshot, RebaseWorkspaceContext
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository

@dataclass(frozen=True)
class RebaseHandlerPorts:
    """Only this responsibility's effects and collaboration callbacks."""
    identity: ExecutionIdentity
    snapshot: Callable[[], PreparationSnapshot]
    checkpoint: ExecutionCheckpointRepository | None
    contract: ExecutionContract
    rebase_corrections: RebaseCorrectionRepository
    state: ExecutionStatePort
    _rebase_in_progress_for_branch: Callable[..., Any]
    branch_has_changes: Callable[..., Any]
    capture_git_snapshot: Callable[..., Any]
    current_rebase_service: Callable[..., Any] | None
    git: Callable[..., Any]
    is_directory: Callable[..., Any]
    path_signature: Callable[..., str]
    pr_for_branch: Callable[..., Any]
    validate_managed_worktree: Callable[..., Any]
    _begin_rebase_continuation: Callable[..., Any]
    _checkpoint_remote_oid: Callable[..., Any]
    _complete_rebase_intent: Callable[..., Any]
    _current_rebase_service: Callable[..., Any]
    _finish_rebase_journal: Callable[..., Any]
    _legacy_current_rebase: Callable[..., Any]
    _owned_checkpoint_remote: Callable[..., Any]
    _publish_rebased_checkpoint: Callable[..., Any]
    _rebase_conflict: Callable[..., Any]
    _rebase_journal_state: Callable[..., Any]
    _rebase_metadata: Callable[..., Any]
    _rebase_stage_evidence: Callable[..., Any]
    _recover: Callable[..., ExecutionOutcome]
    _row: Callable[[], Mapping[str, object]]
    _stage_evidence_preserved: Callable[..., Any]
    _unmerged_stages: Callable[..., Any]
    git_bytes: Callable[..., bytes] | None = None


class RebaseHandler:
    """Rebase policy; durable transitions stay in the existing repositories."""
    def __init__(self, ports: RebaseHandlerPorts):
        self.ports = ports

    def _resolving_rebase(self, row):
        checkpoint = self.ports.checkpoint
        worktree = Path(str(row['worktree'] or ''))
        if (checkpoint is not None and self.ports.snapshot().catalog_plan is not None
                and self.ports.current_rebase_service is not None
                and self.ports.is_directory(worktree)):
            current = checkpoint.get(self.ports.identity.repo, self.ports.snapshot().catalog_plan['plan_id'])
            if (int(self.ports.snapshot().catalog_plan['workspace_owned']) == self.ports.contract.WORKSPACE_RESERVED
                    and current.operation != 'rebase'):
                return False
            if (current.operation == 'rebase'
                    or self.ports._rebase_in_progress_for_branch(worktree, str(row['branch'] or ''))
                    or (int(row['pr_conflict_recovery']) and self.ports._unmerged_stages(worktree))):
                # Validate detached ownership before the ordinary branch gate;
                # a diagnostic string or metadata directory alone is insufficient.
                context = RebaseWorkspaceContext(
                    plan=ExecutionPlanMetadata(
                        plan_id=str(self.ports.snapshot().catalog_plan['plan_id']), title='', path='', plan_sha=None),
                    worktree=worktree, branch=str(row['branch'] or ''))
                try:
                    options = ({'allow_reserved': True} if current.operation == 'rebase'
                               and int(self.ports.snapshot().catalog_plan['workspace_owned']) == self.ports.contract.WORKSPACE_RESERVED else {})
                    service = self.ports._current_rebase_service(context, **options)
                    if current.generation == 0:
                        _, decision = self.ports.state.recover_rebase_checkpoint(checkpoint, service)
                    else:
                        try:
                            _, decision = service.require_owned(checkpoint=current)
                        except manager_git_journal.MutationEvidenceError as error:
                            if str(error) != (
                                    'current rebase rejected: workspace_or_input_identity_mismatch'):
                                raise
                            # Manager may update the selected ExecPlan or its
                            # linked inputs while this owned rebase is pending.
                            # Rebuild once against those current inputs and run
                            # the complete ownership check again; never treat a
                            # changed fingerprint as ownership evidence.
                            service = self.ports._current_rebase_service(context, **options)
                            _, decision = service.require_owned(checkpoint=current)
                except manager_git_journal.MutationEvidenceError as error:
                    if current.generation != 0:
                        raise
                    raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN, str(error)) from error
                return decision.status is RebaseStatus.WAITING
        rebase_error = str(row['last_error'] or '')
        rebase_recovery = bool(
            row['phase'] == 'WORKING' and (rebase_error == self.ports.contract.REBASE_IN_PROGRESS_ERROR or rebase_error.startswith(self.ports.contract.REBASE_CONFLICT_PREFIX)),
        )
        if not rebase_recovery or not self.ports.is_directory(worktree):
            return False
        if self.ports._rebase_in_progress_for_branch(worktree, str(row['branch'] or '')):
            return True
        if int(row['pr_conflict_recovery']) and row['pr_conflict_base_oid']:
            return bool(self.ports._unmerged_stages(worktree))
        return bool(
            checkpoint is not None
            and checkpoint.get(self.ports.identity.repo, self.ports.snapshot().catalog_plan['plan_id']).operation == 'rebase'
            and self.ports._unmerged_stages(worktree)
        ) if self.ports.snapshot().catalog_plan is not None else False

    def _unmerged_stages(self, worktree):
        return self.ports.git(worktree, 'ls-files', '-u', '-z').stdout

    def _rebase_observation(self, context, *, target=None, stash_oid=None):
        checkpoint = self.ports.checkpoint
        intent = checkpoint.get(self.ports.identity.repo, context.plan.plan_id) if checkpoint else None
        snapshot = (intent.snapshot or {}) if intent else {}
        row = self.ports._row()
        in_progress = self.ports._rebase_in_progress_for_branch(
            context.worktree, context.branch
        )
        return observe_git(
            plan_revision=(f'{context.plan.definition_revision_sha}:'
                           f'{context.plan.content_hash}'),
            target_base=str(target or (intent.target_base_oid if intent else None)
                            or row['pr_conflict_base_oid'] or row['active_sha'] or ''),
            before_head=str((intent.expected_head if intent else None)
                            or row['pr_conflict_head_oid'] or row['active_sha'] or ''),
            source=(CorrectionSource.REBASE if in_progress
                    else CorrectionSource.AUTOSTASH),
            stash_oid=stash_oid or snapshot.get('stash_oid'), worktree=context.worktree,
            git=self.ports.git,
            capture_snapshot=self.ports.capture_git_snapshot,
        )

    def _rebase_conflict(self, context, detail, *, corrected=False, worker_result=None,
                         abnormal_result=False, target=None, stash_oid=None,
                         recovery_reason=RecoveryReason.REBASE_CONFLICT, targeted_evidence_missing=False):
        """Track actual Git progress before considering the final safety gate."""
        try:
            observation = self._rebase_observation(
                context, target=target, stash_oid=stash_oid,
            )
            if not observation.target_base or not observation.before_head:
                return self.ports._recover(
                    RecoveryReason.OWNERSHIP_UNCERTAIN,
                    'rebase intent cannot identify its original head and target base',
                    context=context,
                    final_block_reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                )
            if observation.source is CorrectionSource.AUTOSTASH and not observation.stash_oid:
                return self.ports._recover(
                    RecoveryReason.OWNERSHIP_UNCERTAIN, 'autostash_identity_unavailable',
                    context=context,
                    final_block_reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                )
            current = self.ports.rebase_corrections.get(self.ports.identity.repo, context.plan.plan_id)
            saved = self.ports.rebase_corrections.observe(
                self.ports.identity.repo, context.plan.plan_id, observation,
                corrected=corrected,
                count_correction=recovery_reason not in {
                    RecoveryReason.TARGETED_TEST_CORRECTION, RecoveryReason.TARGETED_COLLECTION_PENDING},
                expected_generation=current.generation if current else None,
                worker_session=self.ports._row()['worker_session_id'],
                worker_result=worker_result,
                # This result has been consumed by the correction decision.
                # Only finish_turn() may retain an unconsumed result for restart.
                persist_worker_result=False,
            )
        except CorrectionRace:
            return self.ports._recover(RecoveryReason.REMOTE_SNAPSHOT_RACE,
                                 'rebase correction generation changed', context=context)
        except (RuntimeError, OSError, ValueError) as error:
            return self.ports._recover(
                classify_exception(error, operation=RecoveryReason.STATUS_FAILED),
                str(error), context=context,
            )
        if recovery_reason in {RecoveryReason.TARGETED_TEST_CORRECTION, RecoveryReason.TARGETED_COLLECTION_PENDING}:
            return self.ports._recover(recovery_reason, detail, context=context,
                attempts=int(targeted_evidence_missing), fingerprint=saved.fingerprint,
                progress_marker=saved.progress)
        # An abnormal Agent exit is the first failed correction. Block only
        # after three further identical, progress-free results.
        correction_limit = 4 if abnormal_result else 3
        if saved.occurrence >= correction_limit:
            if (self.ports.checkpoint is not None and self.ports.current_rebase_service is not None
                    and observation.conflict_paths):
                from loop_manager_internal.git import rebase_documents
                rebase_documents.resolve(context=context, identity=self.ports.identity,
                    checkpoints=self.ports.checkpoint, git=self.ports.git, git_bytes=self.ports.git_bytes,
                    require_owned=lambda: self.ports._current_rebase_service(context).require_owned(),
                    capture_snapshot=self.ports.capture_git_snapshot, lookup_pr=self.ports.pr_for_branch,
                    failed_files=observation.conflict_paths,
                    failure_reason=f'{saved.occurrence}회 동일 충돌 correction 실패: {detail}')
                self.ports.state.update_project(self.ports.identity.repo, last_error=
                    '충돌 복구 실패 파일을 원격 원본으로 복원했습니다. 해당 파일을 재작업하세요: '
                    + ', '.join(observation.conflict_paths), worker_result=None,
                    verify_token=None, review_token=None, publication_token=None)
                return self.ports._recover(RecoveryReason.WORKER_CORRECTION,
                    '복원된 파일의 요구사항을 다시 구현합니다.', context=context)
            attempts = [
                {
                    'occurrence': event['occurrence'],
                    'fingerprint': event['fingerprint'],
                    'progress': event['progress'],
                    'worker_result': str(event.get('worker_result') or '')[-1000:],
                }
                for event in saved.audit if event['occurrence'] > 0
            ][-3:]
            detail = (
                f'{detail}\n동일 충돌·진전 없는 Worker correction {saved.occurrence}회; '
                f'마지막 Worker 결과: {worker_result or self.ports._row()["worker_result"] or ""}; '
                f'보존된 worktree: {context.worktree}; '
                f'기존 HEAD: {saved.before_head}; 대상 base: {saved.target_base}; '
                f'Worker session: {saved.worker_session}; '
                f'fingerprint: {saved.fingerprint}; progress: {saved.progress}; '
                f'수행한 correction: {json.dumps(attempts, ensure_ascii=False)}'
            )
            return self.ports._recover(
                RecoveryReason.REBASE_CONFLICT, detail, context=context,
                fingerprint=saved.fingerprint, progress_marker=saved.progress,
                correction_limit=1,
            )
        return self.ports._recover(
            recovery_reason, detail, context=context,
            fingerprint=saved.fingerprint, progress_marker=saved.progress,
            correction_limit=10**9,
        )

    def _record_rebase_intent(self, context, target_oid):
        checkpoint = self.ports.checkpoint
        if checkpoint is None:
            return
        self.ports._publish_rebased_checkpoint(context)
        current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        if self.ports._legacy_current_rebase(context, current):
            self.ports._complete_rebase_intent(context)
            current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        if current.operation == 'rebase':
            # Reentry observes the saved operation before deciding another step.
            observed, decision = self.ports._current_rebase_service(context).require_owned()
            if decision.status is RebaseStatus.START:
                if current.expected_head != observed.head:
                    current = checkpoint.record(
                        self.ports.identity.repo, context.plan.plan_id,
                        expected_generation=current.generation, operation='rebase',
                        expected_head=observed.head, target_base_oid=current.target_base_oid,
                        observed_remote_oid=observed.remote.oid,
                        snapshot={**(current.snapshot or {}), 'before_head': observed.head,
                            'rebase_operation_history': [
                                *(current.snapshot or {}).get('rebase_operation_history', ()),
                                {'before_head': current.expected_head,
                                 'target_base': current.target_base_oid}]},
                    )
                manager_git_journal.begin_current_rebase(
                    self.ports.identity.state_dir, owner=f'plan:{context.plan.plan_id}',
                    repo=str(self.ports.identity.repo_path.resolve()), target=str(context.worktree.resolve()),
                    before=self.ports._rebase_journal_state(context), expected={
                        'branch': context.branch, 'before_head': current.expected_head,
                        'target_base_oid': current.target_base_oid,
                        'remote_oid': observed.remote.oid})
            return
        head = self.ports.git(context.worktree, 'rev-parse', 'HEAD').stdout.strip()
        remote_oid = self.ports._checkpoint_remote_oid(self.ports.identity.repo_path, context.branch)
        live = manager_git_journal.live_remote_branch_oid(
            self.ports.git, self.ports.identity.repo_path, context.branch)
        row = self.ports._row()
        pr_head = str(row['pr_head_oid'] or row['pr_conflict_head_oid'] or '') or None
        if (current.observed_remote_oid not in (None, remote_oid)
                or live != remote_oid or (pr_head and pr_head != remote_oid)):
            raise manager_git_journal.MutationEvidenceError('rebase start remote or PR lease differs')
        from loop_manager_internal.execution import stage_results
        origin = stage_results.preserve(self.ports, context, reason='rebase_start')
        current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        saved = json.loads(json.dumps(current.snapshot or {}))
        if saved:
            saved['rebase_operation_history'] = [
                *saved.get('rebase_operation_history', ()),
                {key: value for key, value in current.snapshot.items()
                 if key not in {'rebase_operation_history', 'stage_result_origins'}}]
        if 'current_rebase_acceptance' in saved:
            saved.setdefault('current_rebase_acceptance_history', []).append(saved.pop('current_rebase_acceptance'))
        for key in ('pending', 'stage_evidence', 'worker_result', 'stash_oid', 'after_head'):
            saved.pop(key, None)
        checkpoint.record(
            self.ports.identity.repo, context.plan.plan_id, expected_generation=current.generation,
            operation='rebase', expected_head=head, target_base_oid=target_oid,
            observed_remote_oid=remote_oid,
            snapshot={**saved, 'before_head': head, 'target_base': target_oid,
                      'rebase_remote_oid': remote_oid, 'rebase_policy': 'current_state',
                      'rebase_stage_origin': origin},
        )
        manager_git_journal.begin_current_rebase(
            self.ports.identity.state_dir, owner=f'plan:{context.plan.plan_id}',
            repo=str(self.ports.identity.repo_path.resolve()), target=str(context.worktree.resolve()),
            before=self.ports._rebase_journal_state(context), expected={
                'branch': context.branch, 'before_head': head,
                'target_base_oid': target_oid, 'remote_oid': remote_oid,
                'agent_session_id': getattr(context, 'worker_session_id', None),
            })

    def _rebase_metadata(self, context):
        for directory in ('rebase-merge', 'rebase-apply'):
            values = {}
            for name in ('orig-head', 'head-name', 'onto'):
                result = self.ports.git(
                    context.worktree, 'rev-parse', '--git-path',
                    f'{directory}/{name}', check=False,
                )
                if result.returncode:
                    break
                path = Path(result.stdout.strip())
                if not path.is_absolute():
                    path = context.worktree / path
                if not path.is_file():
                    break
                values[name] = path.read_text(encoding='utf-8').strip()
            if len(values) == 3:
                return values
        return None

    def _rebase_journal_state(self, context):
        snapshot = self.ports.capture_git_snapshot(context.worktree)
        key = str(context.worktree.resolve())
        ref = f'refs/heads/{context.branch}'
        metadata = self.ports._rebase_metadata(context)
        rebase_head = None
        if metadata is not None:
            result = self.ports.git(
                context.worktree, 'rev-parse', '--verify', 'REBASE_HEAD', check=False,
            )
            if result.returncode == 0:
                rebase_head = result.stdout.strip()
        return json.loads(json.dumps({
            'head': snapshot.head, 'branch': snapshot.branch,
            'ref': snapshot.refs.get(ref), 'index': snapshot.index,
            'dirty': snapshot.dirty,
            'file_evidence': rebase_file_evidence.capture(
                context.worktree, snapshot.head, snapshot.dirty,
            ),
            'registration': snapshot.worktrees.get(key),
            'operations': snapshot.operations,
            'metadata': metadata,
            'rebase_head': rebase_head,
            'unmerged': bool(self.ports._unmerged_stages(context.worktree)),
            'index_clean': self.ports.git(
                context.worktree, 'diff', '--cached', '--quiet', check=False,
            ).returncode == 0,
        }, sort_keys=True))

    def _begin_rebase_continuation(self, context, action):
        current = self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        observed, decision = self.ports._current_rebase_service(context).require_owned()
        if current.operation != 'rebase' or decision.status is not RebaseStatus.WAITING:
            raise manager_git_journal.MutationEvidenceError('rebase continuation is no longer pending')
        return manager_git_journal.begin_current_rebase(
            self.ports.identity.state_dir, owner=f'plan:{context.plan.plan_id}',
            repo=str(self.ports.identity.repo_path.resolve()), target=str(context.worktree.resolve()),
            before=self.ports._rebase_journal_state(context), expected={
                'branch': context.branch, 'before_head': current.expected_head,
                'target_base_oid': current.target_base_oid,
                'remote_oid': observed.remote.oid, 'action': action.value,
                'agent_session_id': getattr(context, 'worker_session_id', None),
            })

    def _execute_rebase_step(self, context, action, *, resolve_documents=True):
        rebase_git = self.ports.git
        token = None
        if action in {CorrectionAction.CONTINUE, CorrectionAction.SKIP}:
            token = self.ports._begin_rebase_continuation(context, action)
            environment = dict(os.environ)
            environment['GIT_REFLOG_ACTION'] = f'loop-manager-rebase:{token}'

            def rebase_git(worktree, *arguments, **kwargs):
                return self.ports.git(
                    worktree, *arguments, env=environment, **kwargs,
                )
        result = execute_git_step(
            action, git=rebase_git, worktree=context.worktree,
        )
        checkpoint = self.ports.checkpoint
        current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id) if checkpoint else None
        if (token is not None and result.returncode == 0 and current is not None
                and (current.snapshot or {}).get('rebase_evidence_version') == 2):
            manager_git_journal.record_rebase_invocation(
                self.ports.identity.state_dir, str(self.ports.identity.repo_path.resolve()),
                str(context.worktree.resolve()), token,
                returncode=result.returncode,
            )
        if resolve_documents and result.returncode:
            automatic = self._advance_document_rebase(context)
            if automatic is not None:
                result = replace(automatic, output=result.output + '\n' + automatic.output)
        return result

    def _rebase_journal_checks(self, context):
        def validate(_intent, _observed):
            return self.ports._current_rebase_service(context).require_owned()[1]
        return None, validate

    def _finish_rebase_journal(self, context, *, allow_equal=False):
        # Autostash restoration remains a correction boundary until its index
        # is clean; do not seal it as a completed current result.
        if self.ports._unmerged_stages(context.worktree) and not self.ports._rebase_metadata(context):
            return False
        self.ports._current_rebase_service(context).normalize_frontmatter()
        observed, decision = self.ports._current_rebase_service(context).require_owned()
        from loop_manager_internal.git.managed_rebase_validation import journal_observation
        return manager_git_journal.record_current_rebase_result(
            self.ports.identity.state_dir, repo=str(self.ports.identity.repo_path.resolve()),
            target=str(context.worktree.resolve()), owner=f'plan:{context.plan.plan_id}',
            decision=decision, observed=lambda: journal_observation(observed))

    def _run_managed_rebase(self, context, target_oid, *, autostash=False):
        if self.ports.current_rebase_service is not None:
            service = self.ports._current_rebase_service(context)
            _, decision = service.require_owned()
            if service.expected.target_base_oid != target_oid:
                raise manager_git_journal.MutationEvidenceError('current rebase target differs')
            if decision.status is RebaseStatus.COMPLETE:
                return subprocess.CompletedProcess([], 0, 'current rebase already complete')
            if decision.status is not RebaseStatus.START:
                raise manager_git_journal.MutationEvidenceError('rebase start is no longer pending')
        slot = manager_git_journal._slot(
            self.ports.identity.state_dir, str(self.ports.identity.repo_path.resolve()), 'managed_rebase',
            str(context.worktree.resolve()),
        )
        intent = manager_git_journal._read(slot / 'active.json')
        if intent.get('expected', {}).get('target_base_oid') != target_oid:
            raise manager_git_journal.MutationEvidenceError(
                'Manager rebase 대상이 durable intent와 다릅니다.'
            )
        environment = dict(os.environ)
        environment['GIT_REFLOG_ACTION'] = f"loop-manager-rebase:{intent['id']}"
        rebased = self.ports.git(
            context.worktree, 'rebase',
            *(('--autostash',) if autostash else ()), target_oid,
            check=False, env=environment,
        )
        if rebased.returncode == 0:
            manager_git_journal.record_rebase_invocation(
                self.ports.identity.state_dir, str(self.ports.identity.repo_path.resolve()),
                str(context.worktree.resolve()), intent['id'],
                returncode=rebased.returncode,
            )
        if rebased.returncode:
            # Automatic document replay can finish (and restore the stash)
            # before _post_rebase_observation sees the initial Git output.
            # Persist its identity before any interruptible document mutation.
            stash_match = re.search(r'Created autostash: ([0-9a-fA-F]{7,64})', rebased.stdout)
            if stash_match and self.ports.checkpoint is not None:
                stash = self.ports.git(context.worktree, 'rev-parse', '--verify',
                                       f'{stash_match.group(1)}^{{commit}}').stdout.strip()
                current = self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
                self.ports.checkpoint.record(
                    self.ports.identity.repo, context.plan.plan_id,
                    expected_generation=current.generation, operation=current.operation,
                    expected_head=current.expected_head, target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid,
                    snapshot={**(current.snapshot or {}), 'stash_oid': stash})
            automatic = self._advance_document_rebase(context)
            if automatic is not None:
                rebased = subprocess.CompletedProcess(rebased.args, automatic.returncode, rebased.stdout + '\n' + automatic.output)
        return rebased

    def _record_rebase_suboperation(self, context, pending, *, worker_result=None):
        checkpoint = self.ports.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        if current.operation != 'rebase':
            return
        stage_evidence = (
            {'stage_evidence': self.ports._rebase_stage_evidence(context),
             'rebase_worker_input': (self.ports.snapshot()._definition_snapshot.serialize()
                 if self.ports.snapshot()._definition_snapshot else None)}
            if pending == 'stage' else {}
        )
        checkpoint.record(
            self.ports.identity.repo, context.plan.plan_id,
            expected_generation=current.generation, operation='rebase',
            expected_head=current.expected_head,
            target_base_oid=current.target_base_oid,
            observed_remote_oid=current.observed_remote_oid,
            snapshot={
                **(current.snapshot or {}), 'pending': pending,
                'worker_result': worker_result or (current.snapshot or {}).get('worker_result'),
                **stage_evidence,
            },
        )

    def _rebase_stage_evidence(self, context):
        observed = self.ports.capture_git_snapshot(context.worktree)
        evidence = {
            'head': observed.head, 'branch': observed.branch,
            'files': {path: signature for path, (_, signature) in observed.dirty.items()},
        }
        if hasattr(observed, 'index'):
            evidence['index'] = {
                path: list(entries) for path, entries in observed.index.items()
            }
        checkpoint = self.ports.checkpoint
        current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id) if checkpoint else None
        if current and (current.snapshot or {}).get('rebase_evidence_version') == 2:
            evidence['modes'] = rebase_file_evidence.capture_modes(
                context.worktree, evidence['files'], self.ports.path_signature,
            )
        return evidence

    def _stage_rebase_correction(self, context):
        """Stage only paths accepted by the Worker scope validator."""
        checkpoint = self.ports.checkpoint
        intent = checkpoint.get(self.ports.identity.repo, context.plan.plan_id) if checkpoint else None
        accepted = (intent.snapshot or {}).get('stage_evidence') if intent else None
        observed = self.ports._rebase_stage_evidence(context)
        tracked = self.ports.git(
            context.worktree, 'ls-files', '--cached', '-z'
        ).stdout
        untracked = self.ports.git(
            context.worktree, 'ls-files', '--others', '--exclude-standard', '-z'
        ).stdout
        tracked_paths = {path for path in tracked.split('\0') if path}
        if self.ports.current_rebase_service is not None:
            self.ports._current_rebase_service(context).require_owned()
        elif accepted is not None and not self.ports._stage_evidence_preserved(
                context, accepted, observed, tracked_paths):
            raise RecoveryFailure(RecoveryReason.OWNERSHIP_UNCERTAIN,
                                  'rebase correction changed after Worker scope validation')
        stageable = tracked_paths | {path for path in untracked.split('\0') if path}
        paths = set(observed['files']) & stageable
        if self.ports.current_rebase_service is not None:
            local = self.ports.capture_git_snapshot(context.worktree)
            paths = {path for path in paths if set(local.dirty[path][0]) - {'staged'}}
        return execute_git_step(
            CorrectionAction.STAGE, git=self.ports.git,
            worktree=context.worktree, paths=tuple(sorted(paths)),
        )

    def _stage_evidence_preserved(self, context, accepted, observed, tracked_paths):
        accepted_files = accepted.get('files') if isinstance(accepted, dict) else None
        disappeared = (accepted_files.keys() - observed['files'].keys()
                       if isinstance(accepted_files, dict) else set())
        clean_signatures = {
            path: self.ports.path_signature(context.worktree, path)
            for path in disappeared & tracked_paths
        }
        return stage_evidence_preserved(
            accepted, observed, tracked_paths, clean_signatures,
        )

    def _resolve_document_conflicts(self, context):
        from loop_manager_internal.git import rebase_documents
        if self.ports.checkpoint is None or self.ports.current_rebase_service is None:
            return False
        current = self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        pending = (current.snapshot or {}).get('rebase_files')
        if pending and pending['state'] == 'prepared':
            return rebase_documents.resolve(context=context, identity=self.ports.identity,
                checkpoints=self.ports.checkpoint, git=self.ports.git, git_bytes=self.ports.git_bytes,
                require_owned=lambda: self.ports._current_rebase_service(context).require_owned(),
                capture_snapshot=self.ports.capture_git_snapshot, lookup_pr=self.ports.pr_for_branch,
                failed_files=pending['paths'])
        return rebase_documents.resolve(
            context=context, identity=self.ports.identity,
            checkpoints=self.ports.checkpoint, git=self.ports.git,
            git_bytes=self.ports.git_bytes,
            require_owned=lambda: self.ports._current_rebase_service(context).require_owned(),
            capture_snapshot=self.ports.capture_git_snapshot,
            lookup_pr=self.ports.pr_for_branch,
        )

    def _advance_document_rebase(self, context):
        # Each continue may stop on another replayed commit. Resolve documents
        # at every stop, using a durable per-position intent rather than recursion.
        result = None
        while self.ports._rebase_in_progress_for_branch(context.worktree, context.branch):
            if not self._resolve_document_conflicts(context):
                break
            if self.ports._unmerged_stages(context.worktree):
                break
            staged = self.ports.git(context.worktree, 'diff', '--cached', '--quiet', check=False)
            if staged.returncode not in {0, 1}:
                raise RecoveryFailure(RecoveryReason.STATUS_FAILED, 'document resolution index unavailable')
            action = CorrectionAction.CONTINUE if staged.returncode else CorrectionAction.SKIP
            self._record_rebase_suboperation(context, 'continue')
            continued = self._execute_rebase_step(context, action, resolve_documents=False)
            result = continued
            if continued.returncode and not self.ports._unmerged_stages(context.worktree):
                raise RecoveryFailure(RecoveryReason.REBASE_CONTINUE_FAILED, continued.output)
        return result

    def _post_rebase_observation(self, context, target_oid, output, *, corrected=False,
                                 worker_result=None):
        """A successful exit may leave autostash conflicts without rebase metadata."""
        automatic = self._advance_document_rebase(context)
        if automatic is not None:
            output = automatic.output
        unmerged = self.ports._unmerged_stages(context.worktree)
        in_progress = self.ports._rebase_in_progress_for_branch(
            context.worktree, context.branch
        )
        checkpoint = self.ports.checkpoint
        stash_oid = None
        if checkpoint is not None and unmerged:
            current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            stash_match = re.search(r'Created autostash: ([0-9a-fA-F]{7,64})', output)
            stash_oid = None
            if stash_match:
                resolved = self.ports.git(
                    context.worktree, 'rev-parse', '--verify',
                    f'{stash_match.group(1)}^{{commit}}', check=False,
                )
                if resolved.returncode == 0:
                    stash_oid = resolved.stdout.strip()
            snapshot = dict(current.snapshot or {})
            if snapshot.get('pending') == 'continue':
                # Continue is recorded only after all prior index stages clear.
                # New unmerged stages consume that result, never fail it again.
                snapshot.setdefault('rebase_worker_input_history', []).append({
                    'input_snapshot': snapshot.get('rebase_worker_input'),
                    'worker_result': snapshot.get('worker_result'),
                    'stage_evidence': snapshot.get('stage_evidence'),
                    'pending': 'continue', 'reason': 'next_conflict',
                })
                for key in ('pending', 'worker_result', 'stage_evidence', 'rebase_worker_input'):
                    snapshot.pop(key, None)
            checkpoint.record(
                self.ports.identity.repo, context.plan.plan_id,
                expected_generation=current.generation, operation='rebase',
                expected_head=current.expected_head,
                target_base_oid=target_oid,
                observed_remote_oid=current.observed_remote_oid,
                snapshot={
                    **snapshot,
                    'after_head': self.ports.git(
                        context.worktree, 'rev-parse', 'HEAD'
                    ).stdout.strip(),
                    'unmerged_index': unmerged,
                    'stash_oid': stash_oid or (current.snapshot or {}).get('stash_oid'),
                },
            )
            if not in_progress and not (stash_oid or (current.snapshot or {}).get('stash_oid')):
                return self.ports._recover(
                    RecoveryReason.OWNERSHIP_UNCERTAIN, 'autostash_identity_unavailable',
                    context=context,
                    final_block_reason=BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                )
        if not unmerged and not in_progress:
            if checkpoint is not None:
                self.ports._current_rebase_service(context).normalize_frontmatter()
                from loop_manager_internal.git.rebase_documents import validate_final
                validate_final(context=context,
                    checkpoint=checkpoint.get(self.ports.identity.repo, context.plan.plan_id),
                    git_bytes=self.ports.git_bytes)
                self.ports.validate_managed_worktree(
                    self.ports.identity.repo_path, context.worktree, context.branch
                )
                ancestry = self.ports.git(
                    context.worktree, 'merge-base', '--is-ancestor',
                    target_oid, 'HEAD', check=False,
                )
                if ancestry.returncode != 0:
                    raise RecoveryFailure(
                        RecoveryReason.STATUS_FAILED,
                        'rebase completed without expected target ancestry',
                    )
            try:
                self.ports._finish_rebase_journal(context, allow_equal=True)
            except manager_git_journal.MutationEvidenceError:
                if self.ports.branch_has_changes(
                    context.worktree, self.ports.identity.base,
                ):
                    raise
                # A dropped Worker patch is not journal evidence. Keep the
                # intent unresolved and let the existing no-change gate block.
            return None
        self.ports._finish_rebase_journal(context, allow_equal=True)
        message = self.ports.contract.REBASE_CONFLICT_PREFIX + output[-8000:]
        self.ports.state.update_project(
            self.ports.identity.repo, phase='WORKING', validated_base_sha=None,
            last_error=message,
        )
        return self.ports._rebase_conflict(
            context, message, corrected=corrected, worker_result=worker_result,
            target=target_oid,
            stash_oid=(stash_oid if unmerged and not in_progress else None),
        )

    def _completed_autostash_correction(
        self, context, intent, sealed, live, *, rejection=None,
    ):
        def reject(reason):
            if rejection is not None:
                rejection.append(reason)
            return False

        current = self.ports.checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        saved = current.snapshot or {}
        stage = saved.get('stage_evidence')
        accepted = stage.get('files') if isinstance(stage, dict) else None
        modes = stage.get('modes') if isinstance(stage, dict) else None
        sealed_operations = sealed.get('operations')
        live_operations = live.get('operations')
        operation_markers_match = (
            live_operations == sealed_operations
            or (isinstance(sealed_operations, dict) and live_operations == {}
                and set(sealed_operations) <= {
                    'MERGE_MSG', 'MERGE_MODE', 'REBASE_HEAD',
                })
        )
        if (intent['expected'].get('evidence_version') != 2
                or not saved.get('stash_oid') or saved.get('pending') != 'continue'
                or intent['expected'].get('agent_session_id')
                != saved.get('rebase_start_session_id')
                or not saved.get('worker_result') or not isinstance(accepted, dict)
                or not accepted or not isinstance(modes, dict)
                or sealed.get('metadata') is not None or not sealed.get('unmerged')
                or live.get('metadata') is not None or live.get('unmerged')):
            return reject('checkpoint or conflict identity')
        if not operation_markers_match:
            return reject('Git operation markers')
        if any(live.get(field) != sealed.get(field) for field in (
                'head', 'branch', 'ref', 'registration')):
            return reject('HEAD, branch or registration')
        tracked = {
            path for path in self.ports.git(
                context.worktree, 'ls-files', '--cached', '-z',
            ).stdout.split('\0') if path
        }
        observed_stage = self.ports._rebase_stage_evidence(context)
        # Normal staging changes the index. Compare the approved file
        # signatures here; stage_matches below checks every index entry.
        file_fields = ('head', 'branch', 'files')
        if not self.ports._stage_evidence_preserved(
                context,
                {field: stage.get(field) for field in file_fields},
                {field: observed_stage.get(field) for field in file_fields},
                tracked):
            return reject('approved stage evidence')
        if any(sealed['dirty'].get(path) != live['dirty'].get(path)
               for path in sealed['dirty'].keys() | live['dirty'].keys()
               if path not in accepted):
            return reject('unapproved dirty path')
        sealed_files = sealed.get('file_evidence') or {}
        live_files = live.get('file_evidence') or {}
        if any(sealed_files.get(path) != live_files.get(path)
               for path in sealed_files.keys() | live_files.keys()
               if path not in accepted):
            return reject('unapproved file evidence')
        if not managed_rebase_evidence.stage_matches(
                before_index=sealed['index'], staged_index=live['index'],
                allowed_paths=list(accepted), accepted_files=accepted,
                accepted_modes=modes,
                staged_dirty={path: [[], signature]
                              for path, signature in accepted.items()}):
            return reject('staged index or mode')
        for path in accepted:
            entries = live['index'].get(path)
            if modes[path] is None:
                if entries or (context.worktree / path).exists() or (
                        context.worktree / path).is_symlink():
                    return reject('approved deletion')
                continue
            if not isinstance(entries, list) or len(entries) != 1:
                return reject('approved index entry')
            staged_oid = entries[0].split()[2]
            blob = self.ports.git(
                context.worktree, 'hash-object', f'--path={path}', '--', path,
                check=False,
            )
            if blob.returncode or blob.stdout.strip() != staged_oid:
                return reject('approved blob')
        return True

    def _complete_rebase_intent(self, context, *, project_values=None, worker_definition=None):
        checkpoint = self.ports.checkpoint
        if checkpoint is None:
            return
        current = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
        saved = current.snapshot or {}
        legacy_completed = (current.operation is None and not saved.get('current_rebase_acceptance')
                            and (saved.get('completed_rebase') or saved.get('rebase_completion')
                                 or self.ports._legacy_current_rebase(context, current)))
        if current.operation != 'rebase' and not legacy_completed:
            return
        service = self.ports._current_rebase_service(context)

        def validate_ownership(observed, decision):
            selected = checkpoint.get(self.ports.identity.repo, context.plan.plan_id)
            if selected != current or not self.ports._owned_checkpoint_remote(
                    context, self.ports._row(), service.expected.remote.oid, selected,
                    rebase_observation=(service.expected, observed, decision)):
                raise manager_git_journal.MutationEvidenceError('current checkpoint remote ownership differs')

        prior = self.ports._row()
        service.adopt(self.ports.state, checkpoint, validate_ownership=validate_ownership)
        self.ports._publish_rebased_checkpoint(context)
        if worker_definition is not None:
            self.ports.state.retain_clean_rebase_worker(prior, worker_definition, checkpoint)

    def _publish_rebased_checkpoint(self, context):
        if self.ports.checkpoint is None:
            return
        managed_rebase_publication.publish_checkpoint(
            state=self.ports.state, checkpoints=self.ports.checkpoint,
            repo=self.ports.identity.repo, plan_id=context.plan.plan_id, repo_path=self.ports.identity.repo_path,
            worktree=context.worktree, branch=context.branch,
            service=lambda **options: self.ports._current_rebase_service(context, **options),
            owns_remote=lambda oid, selected, **evidence: self.ports._owned_checkpoint_remote(
                context, self.ports._row(), oid, selected, **evidence),
            git=self.ports.git,
            lookup_pr=self.ports.pr_for_branch)
