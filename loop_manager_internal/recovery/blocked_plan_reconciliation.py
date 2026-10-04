"""Serial, restartable remote refresh through existing ownership boundaries.

The additive recovery state preserves original causes and publication audit.
Current rebase acceptance never requires historical execution evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON, PR_CLOSED_BLOCK_REASON
from loop_manager_internal.recovery import blocked_checkpoint
import sqlite3
import subprocess

from loop_manager_internal.recovery import blocked_remote_observation as observations
from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.storage import maintenance_state
from loop_manager_internal.git.managed_rebase_adoption import revalidation_values
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
from loop_manager_internal.agents.scope_contracts import GitSnapshot


SCHEMA = """
CREATE TABLE IF NOT EXISTS blocked_plan_reconciliations (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    input_identity TEXT NOT NULL,
    stage TEXT NOT NULL,
    context TEXT NOT NULL,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo, plan_id) REFERENCES exec_plans(repo, plan_id)
)
"""
IDENTITY_FIELDS = ('path', 'branch', 'worktree', 'workspace_owned', 'pr_url',
                   'content_hash', 'sources', 'depends_on')
ARTIFACT_FIELDS = ('worker_result', 'verify_result', 'review_result',
                   'worker_input_token', 'verify_token', 'review_token',
                   'publication_token', 'agent_snapshot', 'agent_result_snapshot')
TERMINAL = {'complete', 'refused'}


class InputChanged(RuntimeError):
    """Leave input pending so the next cycle can use a newly pinned revision."""


class AssessmentPending(RuntimeError):
    """An external cause probe did not produce a definitive assessment."""


def slot_available(connection, repo, plan_id) -> bool:
    runtime = runtime_state.RuntimeStateRepository(connection)
    project = connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()
    active = runtime.active_row(repo)
    projected = runtime.projected_row(repo)
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    publication = (connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'general_planning_publications'").fetchone()
        and connection.execute('SELECT 1 FROM general_planning_publications WHERE repo = ?', (repo,)).fetchone())
    return bool(project is not None and runtime.recovery_slot_available(repo)
                and not project['pr_conflict_recovery']
                and (active is None or active['plan_id'] == plan_id)
                and (project['phase'] not in {'WORKING', 'IMPLEMENTING', 'VERIFYING',
                                             'REVIEWING', 'PUBLISHING'}
                     or projected is not None and projected['plan_id'] == plan_id)
                and cycle.phase is maintenance_state.MaintenancePhase.IDLE
                and cycle.publication_stage is None and not publication)


def remote_adoption_is_current(checkpoint) -> bool:
    """Select the latest Manager transition without discarding rebase audit.

    An unfinished adoption is newer than an idle rebase receipt. A completed
    adoption owns the comparison only while its exact HEAD remains the current
    checkpoint; a subsequent rebase operation or acceptance takes precedence.
    Identity and actual Git scope are still checked by the caller.
    """
    if checkpoint.operation is not None:
        return False
    saved = checkpoint.snapshot or {}
    if saved.get('remote_adoption_intent'):
        return True
    adopted = saved.get('remote_adoption_scope')
    accepted = saved.get('current_rebase_acceptance') or {}
    return bool(adopted and adopted.get('head') == checkpoint.expected_head
                and adopted.get('head') == checkpoint.observed_remote_oid
                and accepted.get('head') != adopted.get('head'))


def validated_scope_refs(
    connection: sqlite3.Connection, repo_path: Path, repo: str, base: str,
    plan_id: str | None, before: GitSnapshot, result: GitSnapshot | None,
    current: GitSnapshot, *, git: Callable[..., subprocess.CompletedProcess[str]],
    github_repository: Callable[[str], str], owned_remote_oid: Callable[..., str | None],
) -> frozenset[str]:
    """Classify post-Agent tracking updates, never approve a remote adoption.

    A published tracking OID remains Manager-owned when origin has a newer
    successor. Base tracking must equal the freshly checked observed base.
    R-039 still checks that successor before any worktree change or release.
    """
    if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'blocked_remote_observations'"
    ).fetchone() is None:
        return frozenset()
    row = connection.execute(
        'SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
        (repo, plan_id)).fetchone()
    if row is None or not row['registered_origin']:
        return frozenset()
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                              (repo, plan_id)).fetchone()
    project = connection.execute('SELECT origin, base_branch FROM projects WHERE repo = ?',
                                 (repo,)).fetchone()
    if (plan is None or plan['status'] != 'PLAN_BLOCKED' or plan['workspace_owned'] != 1
            or not plan['branch'] or not plan['worktree'] or project is None
            or project['origin'] != row['registered_origin'] or project['base_branch'] != base
            or row['base_ref'] != f'refs/heads/{base}'
            or row['branch_ref'] != f"refs/heads/{plan['branch']}"):
        return frozenset()
    base_ref, branch_ref = f'refs/remotes/origin/{base}', f"refs/remotes/origin/{plan['branch']}"
    blocked = result if result is not None else before
    candidates = {ref for ref in (base_ref, branch_ref)
                  if before.refs.get(ref) == blocked.refs.get(ref)
                  and before.refs.get(ref) != current.refs.get(ref)}
    if not candidates:
        return frozenset()
    allowed = set()
    try:
        origin = git(repo_path, 'remote', 'get-url', 'origin').stdout.strip()
        if origin != row['origin_url'] or github_repository(origin) != project['origin']:
            return frozenset()
        live_base, live_branch = observations.observe_refs(repo_path, origin, (row['base_ref'], row['branch_ref']))
        if (base_ref in candidates and row['base_result'] == 'present'
                and live_base.result == 'present' and live_base.oid == row['base_oid']
                and current.refs.get(base_ref) == (live_base.oid, '')):
            allowed.add(base_ref)
        tracked = current.refs.get(branch_ref)
        if (branch_ref in candidates and live_branch.result == 'present'
                and tracked is not None and not tracked[1]
                and current.branch == plan['branch']
                and current.refs.get(f"refs/heads/{plan['branch']}") == (current.head, '')
                and owned_remote_oid(
                    connection, repo_path, repo, plan_id, plan['branch'], Path(plan['worktree']),
                    tracked[0], git, before_head=current.head,
                    recorded_oid=plan['publication_remote_oid'], require_live=False,
                    strict_observation=True) == tracked[0]):
            allowed.add(branch_ref)
    except RecoveryFailure:
        raise
    except (OSError, RuntimeError, ValueError):
        return frozenset()
    return frozenset(allowed)


@dataclass(frozen=True)
class RecoveryWorkspaceEffects:
    """Shared ref, identity, adoption and checkpoint effects; no release policy."""
    _BlockedCheckpointAdapter: Callable[..., object]
    _BlockedCheckpointResults: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _journaled_direct_rebase: Callable[..., object]
    _reconcile_published_blocked_checkpoint: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    _worktree_state_signature: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    github_repository: Callable[..., object]
    observe_blocked_plan_remotes: Callable[..., object]
    owned_remote_oid: Callable[..., object]
    pr_for_branch: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


@dataclass(frozen=True)
class PlanRecoveryEffects(RecoveryWorkspaceEffects):
    """Explicit effects for this service; persisted decisions remain in repositories."""
    ROOT: Path
    _blocked_execplan_metadata_error: Callable[..., object]
    _complete_direct_rebase_checkpoint: Callable[..., object]
    _evaluate_remote_checkpoint_cause: Callable[..., object]
    _non_default_index_flags: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]
    load_managed_config: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]


class BlockedPlanReconciler:
    def __init__(self, ports, connection, repo_path, state_dir, repo, base,
                 config_path=None, timeout_seconds=None):
        self.ports, self.connection = ports, connection
        self.repo_path, self.state_dir = repo_path, state_dir
        self.repo, self.base = repo, base
        self.config_path, self.timeout = config_path, timeout_seconds
        connection.execute(SCHEMA)

    def _plan(self, plan_id):
        return self.connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                       (self.repo, plan_id)).fetchone()

    def _save(self, plan_id, key, stage, context):
        with runtime_state._transaction(self.connection):
            self.connection.execute(
                'INSERT INTO blocked_plan_reconciliations VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(repo, plan_id) DO UPDATE SET input_identity = excluded.input_identity, '
                'stage = excluded.stage, context = excluded.context',
                (self.repo, plan_id, key, stage, json.dumps(context, sort_keys=True)))

    def _check_owner(self, plan_id, context):
        plan = self._plan(plan_id)
        block = scoped_blocking.plan_block(self.connection, self.repo, plan_id)
        if (plan is None or plan['status'] != 'PLAN_BLOCKED' or block is None
                or not slot_available(self.connection, self.repo, plan_id)
                or {field: plan[field] for field in IDENTITY_FIELDS} != context['identity']
                or any(block[field] != context['block'][field]
                       for field in ('reason_code', 'release_fingerprint', 'evidence'))
                or self.ports.unmet_execplan_dependencies(self.connection, self.repo, plan_id)):
            raise RuntimeError('차단 계획의 identity·원인·dependency 또는 실행 자리가 변경됐습니다.')
        return plan

    def _remote(self, context, *, allow_published=False):
        project = self.connection.execute('SELECT * FROM projects WHERE repo = ?', (self.repo,)).fetchone()
        if project['origin'] != context['registered_origin'] or project['base_branch'] != self.base:
            raise RuntimeError('등록 origin 또는 base branch가 변경됐습니다.')
        origin = self.ports.git(self.repo_path, 'remote', 'get-url', 'origin').stdout.strip()
        if origin != context['origin_url'] or self.ports.github_repository(origin) != project['origin']:
            raise RuntimeError('원격 저장소 identity가 관측과 다릅니다.')
        base, branch = observations.observe_refs(
            self.repo_path, origin, (context['base_ref'], context['branch_ref']))
        if base.result == 'error' or branch.result == 'error':
            raise AssessmentPending('차단 계획 원격 ref 조회를 다음 실행에서 재시도합니다.')
        if base.result != 'present':
            raise RuntimeError('차단 계획 원격 ref 조회 실패 또는 base 삭제')
        # Finish a saved target/publication before considering a later base.
        checkpoint = ExecutionCheckpointRepository(self.connection).get(self.repo, context['plan_id'])
        intent = checkpoint.rebase_publication_intent()
        allowed = {context['branch_oid']}
        if allow_published and intent is not None:
            allowed.add(intent['head'])
        if allow_published and self._plan(context['plan_id'])['publication_remote_oid']:
            allowed.add(self._plan(context['plan_id'])['publication_remote_oid'])
        if branch.oid not in allowed:
            raise RuntimeError('원격 작업 branch가 관측 이후 변경·삭제됐습니다.')
        if not allow_published and base.oid != context['base_oid']:
            raise RuntimeError('원격 base가 관측 이후 변경됐습니다.')
        if self.ports.pr_for_branch(self.repo_path, context['identity']['branch']) is not None:
            raise RuntimeError('기존 PR이 있는 작업 branch의 차단 checkpoint 복구를 거부합니다.')
        return branch.oid

    def _validate(self, plan, *, rebase=False):
        worktree, branch = Path(plan['worktree']), str(plan['branch'])
        self.ports.validate_execplan_workspace_identity(self.state_dir, self.repo, plan['plan_id'], branch, worktree)
        self.ports.validate_managed_worktree(self.repo_path, worktree, branch, allow_rebase=rebase)
        self.ports.validate_blocked_agent_scope(
            self.connection, self.repo_path, self.repo, self.base, worktree, plan['path'],
            accepted_rebase=rebase, saved_plan=plan)
        return worktree, branch

    def _adopt(self, plan, context, *, project_recovery_context=None):
        checkpoint = ExecutionCheckpointRepository(self.connection).get(self.repo, plan['plan_id'])
        if checkpoint.operation in {'rebase', 'rebase_publish'}:
            self.ports._current_rebase_service(
                self.connection, self.repo_path, self.state_dir, self.repo, plan['plan_id'],
                Path(plan['worktree']), plan['branch']).require_owned(checkpoint=checkpoint)
            return
        row = self.connection.execute('SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                                      (self.repo, plan['plan_id'])).fetchone()
        if row is not None and row['phase'] == 'published':
            repository = BlockedCheckpointRepository(self.connection)
            result = self.ports._reconcile_published_blocked_checkpoint(
                self.connection, self.repo_path, self.state_dir, self.repo, plan, row,
                repository.get(self.repo, plan['plan_id']), repository,
                adopt_only=True, expected_remote_oid=context['branch_oid'],
                project_recovery_context=project_recovery_context)
            if result not in {'unchanged', 'adopted'}:
                inspection = self.connection.execute(
                    'SELECT inspection_retryable FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                    (self.repo, plan['plan_id'])).fetchone()
                if inspection is not None and inspection['inspection_retryable']:
                    raise AssessmentPending(self._plan(plan['plan_id'])['last_error'] or
                                            '원격 checkpoint 검사를 다음 실행에서 재시도합니다.')
                raise RuntimeError(self._plan(plan['plan_id'])['last_error'] or
                                   '원격 checkpoint 후속 commit의 안전한 채택이 거부됐습니다.')
            current = self._plan(plan['plan_id'])
            head = self.ports.git(Path(plan['worktree']), 'rev-parse', 'HEAD^{commit}').stdout.strip()
            if self.ports.owned_remote_oid(
                    self.connection, self.repo_path, self.repo, plan['plan_id'], plan['branch'],
                    Path(plan['worktree']), context['branch_oid'], self.ports.git,
                    before_head=head, recorded_oid=current['publication_remote_oid'],
                    strict_observation=True) is None:
                raise RuntimeError('마지막 채택 원격 OID와 게시 checkpoint 소유권을 증명할 수 없습니다.')
            checkpoints = ExecutionCheckpointRepository(self.connection)
            checkpoint = checkpoints.get(self.repo, plan['plan_id'])
            if checkpoint.operation is None and checkpoint.observed_remote_oid != context['branch_oid']:
                checkpoints.record(
                    self.repo, plan['plan_id'], expected_generation=checkpoint.generation,
                    operation=None, expected_head=head, target_base_oid=checkpoint.target_base_oid,
                    observed_remote_oid=context['branch_oid'], snapshot=checkpoint.snapshot)
        elif context['branch_oid'] is not None:
            raise RuntimeError('게시 checkpoint가 없는 기존 원격 작업 branch의 소유권을 증명할 수 없습니다.')

    def _incorporate(self, plan, context):
        plan_id = str(plan['plan_id'])
        checkpoints = ExecutionCheckpointRepository(self.connection)
        checkpoint = checkpoints.get(self.repo, plan_id)
        pending = checkpoint.operation in {'rebase', 'rebase_publish'}
        accepted = (checkpoint.snapshot or {}).get('current_rebase_acceptance') or {}
        completed = (checkpoint.target_base_oid == context['base_oid']
                     and accepted.get('head') == checkpoint.expected_head
                     and bool(accepted))
        worktree, branch = self._validate(plan, rebase=pending or bool(
            (checkpoint.snapshot or {}).get('current_rebase_acceptance')))
        if not pending and not completed:
            try:
                fetched_oid = self.ports._recorded_base_fetch(
                    self.repo_path, self.state_dir, self.base, owner=f'plan:{plan_id}')
            except (OSError, RuntimeError) as error:
                raise AssessmentPending('차단 계획 base fetch 미완료: ' + str(error)) from error
            if fetched_oid != context['base_oid']:
                raise InputChanged('차단 계획 base 조회·fetch 사이 OID 경쟁')
            remote = self._remote(context, allow_published=True)
            if remote != context['branch_oid']:
                # A checkpoint push may have finished before the recovery
                # context reached SQLite. Its existing publication identity
                # must prove the new lease; a mere observed OID cannot do so.
                self._adopt(plan, {**context, 'branch_oid': remote})
                context['branch_oid'] = remote
            snapshot = self.ports.capture_git_snapshot(worktree)
            if (snapshot.operations
                    or any(self.ports._non_default_index_flags(entries) for entries in snapshot.index.values())):
                raise RuntimeError('미해결 Git 작업 또는 예상 밖 로컬/index 변경을 보존합니다.')
            if snapshot.dirty:
                # Preserve allowed local edits through the existing durable
                # checkpoint boundary before asking rebase to handle a clean
                # index. An unsafe scope is refused without staging any path.
                self._publish(plan, context)
                request = BlockedCheckpointRepository(self.connection).get(self.repo, plan_id)
                if request.phase.value != 'published':
                    reason = self.connection.execute(
                        'SELECT unpublished_reason FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                        (self.repo, plan_id)).fetchone()[0]
                    raise RuntimeError(reason or '로컬 차단 작업물을 안전한 checkpoint로 보존할 수 없습니다.')
                context['branch_oid'] = request.remote_oid
                self._save(plan_id, context['input_identity'], 'adopted', context)
                checkpoints.record(
                    self.repo, plan_id, expected_generation=checkpoint.generation,
                    operation=None, expected_head=request.local_oid, target_base_oid=checkpoint.target_base_oid,
                    observed_remote_oid=request.remote_oid, snapshot=checkpoint.snapshot)
            metadata = self.ports._blocked_execplan_metadata_error(self.repo_path, worktree, plan)
            if metadata:
                raise RuntimeError(metadata)
            ancestor = self.ports.git(worktree, 'merge-base', '--is-ancestor', context['base_oid'], 'HEAD', check=False).returncode
            if ancestor not in {0, 1}:
                raise RuntimeError('차단 계획 target base ancestry를 확인할 수 없습니다.')
            if ancestor == 1:
                self._check_owner(plan_id, context)
                rebased = self.ports._journaled_direct_rebase(
                    self.connection, self.repo_path, self.repo, plan_id, worktree, branch,
                    context['base_oid'], agent_session_id=plan['worker_session_id'])
                if rebased.returncode:
                    raise RuntimeError('차단 계획 관리 rebase가 충돌 해결을 기다립니다: ' + rebased.stdout[-2000:])
        checkpoint = checkpoints.get(self.repo, plan_id)
        if checkpoint.operation == 'rebase':
            _, decision = self.ports._current_rebase_service(
                self.connection, self.repo_path, self.state_dir, self.repo, plan_id,
                worktree, branch).require_owned(checkpoint=checkpoint)
            if decision.status.value == 'ready_to_start':
                rebased = self.ports._journaled_direct_rebase(
                    self.connection, self.repo_path, self.repo, plan_id, worktree, branch,
                    checkpoint.target_base_oid, agent_session_id=plan['worker_session_id'])
                if rebased.returncode:
                    raise RuntimeError('차단 계획 관리 rebase가 충돌 해결을 기다립니다: ' + rebased.stdout[-2000:])
                checkpoint = checkpoints.get(self.repo, plan_id)
        if checkpoint.operation in {'rebase', 'rebase_publish'} or completed:
            self.ports.hydrate_execplan_project(self.connection, self.repo, self._plan(plan_id))
            self.ports._complete_direct_rebase_checkpoint(
                self.connection, self.repo, plan_id, worktree, checkpoint.target_base_oid,
                repo_path=self.repo_path, branch=branch, agent_session_id=plan['worker_session_id'])
        if checkpoints.get(self.repo, plan_id).operation is not None:
            raise RuntimeError('관리 rebase 또는 중간 checkpoint 게시가 아직 완료되지 않았습니다.')
        self.ports._current_rebase_service(
            self.connection, self.repo_path, self.state_dir, self.repo, plan_id,
            worktree, branch, target=checkpoint.target_base_oid if pending else context['base_oid'],
        ).require_owned(checkpoint=checkpoints.get(self.repo, plan_id))
        current = checkpoints.get(self.repo, plan_id)
        intent = current.rebase_publication_intent()
        result = (current.snapshot or {}).get('rebase_publication_result') or {}
        if intent is not None and result.get('intent_id') == intent['id']:
            # Recheck the common current-state boundary before advancing the
            # checkpoint used by R-039's next successor adoption.
            observed, decision = self.ports._current_rebase_service(
                self.connection, self.repo_path, self.state_dir, self.repo, plan_id,
                worktree, branch).require_owned(checkpoint=current)
            if decision.status.value != 'complete' or observed.head != intent['head']:
                raise RuntimeError('중간 checkpoint 게시 뒤 현재 rebase 결과가 다릅니다.')
            repository = BlockedCheckpointRepository(self.connection)
            with runtime_state._transaction(self.connection):
                self._check_owner(plan_id, context)
                repository.record_rebased_publication(
                    repository.get(self.repo, plan_id), intent['head'],
                    self.ports.git(worktree, 'rev-parse', 'HEAD^{tree}').stdout.strip())
        if pending and current.target_base_oid != context['base_oid']:
            # The saved result is now published. Only now may a newer target
            # start, and the original cause still has not been inspected.
            return self._incorporate(self._plan(plan_id), context)
        context['candidate'] = self.ports._worktree_state_signature(worktree)

    def _evaluate(self, plan, context):
        reason = context['block']['reason_code']
        assessment_plan = {**dict(plan), **context['artifacts']}
        if reason in {BlockReasonCode.VERIFY_RETRY_EXHAUSTED.value,
                      BlockReasonCode.REVIEW_RETRY_EXHAUSTED.value,
                      BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED.value}:
            try:
                resolved, detail, stage = self.ports._evaluate_remote_checkpoint_cause(
                    self.connection, self.repo_path, self.state_dir, self.repo, assessment_plan,
                    self.config_path, self.timeout)
            except VerifyTerminationUncertain as error:
                with runtime_state._transaction(self.connection):
                    scoped_blocking.record_project_block(
                        self.connection, self.repo, BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                        str(error), context['input_identity'], plan_id=plan['plan_id'], previous_phase=None)
                    notifications.enqueue_current_project_events(self.connection, self.repo)
                raise AssessmentPending(str(error)) from error
            except (OSError, RuntimeError, ValueError) as error:
                raise AssessmentPending(str(error)) from error
            if stage in {'stale_base', 'revision_drift'}:
                raise InputChanged(detail)
            if reason != BlockReasonCode.VERIFY_RETRY_EXHAUSTED.value and stage == 'verify':
                resolved = False
            return dict(resolved=resolved, detail=detail, stage=stage)
        if reason in {GUIDANCE_BLOCK_REASON, PR_CLOSED_BLOCK_REASON}:
            return dict(resolved=False, detail=context['block']['evidence'], stage=None)
        if reason == BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value:
            timeout = self.timeout if self.timeout is not None else self.ports.load_managed_config(
                self.config_path or self.ports.ROOT / 'config/config.ini').verify_timeout_seconds
            return dict(resolved=timeout > int(plan['blocked_fingerprint']),
                        detail=context['block']['evidence'], stage=None)
        if reason in {BlockReasonCode.PLAN_INPUT_INVALID.value,
                      BlockReasonCode.WORKER_OUTPUT_INVALID.value,
                      BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK.value,
                      BlockReasonCode.USER_DECISION_REQUIRED.value}:
            fingerprint = self.ports._worktree_execplan_input_fingerprint(Path(plan['worktree']), plan)
            stored = plan['blocked_fingerprint']
            base_input = self.ports._revision_execplan_input_fingerprint(
                self.repo_path, context['base_oid'], plan)
            resolved = bool(stored and (base_input != stored or
                            fingerprint != stored and fingerprint != plan['blocked_worktree_fingerprint']))
            return dict(resolved=resolved, detail=context['block']['evidence'], stage=None)
        return dict(resolved=False, detail=context['block']['evidence'], stage=None)

    def _publish(self, plan, context):
        repository = BlockedCheckpointRepository(self.connection)
        request = repository.get(self.repo, plan['plan_id'])
        adapter = self.ports._BlockedCheckpointAdapter(self.connection, self.repo_path, self.state_dir, self.repo,
                                                     reconciliation_scope=True)
        expected = self._remote(context, allow_published=True)
        remote = adapter.observe_remote(request)
        if remote != expected:
            raise RuntimeError('checkpoint 게시 직전 원격 작업 branch가 변경됐습니다.')
        head = self.ports.git(Path(plan['worktree']), 'rev-parse', 'HEAD^{commit}').stdout.strip()
        if (remote == head and request.phase.value == 'published'
                and not self.ports.capture_git_snapshot(Path(plan['worktree'])).dirty):
            return
        repository.refresh(request, expected_remote_oid=remote)
        results = self.ports._BlockedCheckpointResults(self.connection)
        blocked_checkpoint.BlockedCheckpointDispatcher(repository, adapter, adapter, results)._dispatch_one(
            repository.get(self.repo, plan['plan_id']))
        if repository.get(self.repo, plan['plan_id']).phase.value == 'retry_pending':
            raise RuntimeError('차단 checkpoint의 일시적 게시 실패를 같은 요청으로 재개합니다.')

    def _resume(self, plan, context):
        values = revalidation_values(context['base_oid'])
        values.pop('phase')
        values.update(status='IMPLEMENTING', blocked_reason=None, blocked_fingerprint=None,
                      blocked_worktree_fingerprint=None, blocked_review_fingerprint=None,
                      last_error='원격 차단 복구 후 현재 후보를 Worker부터 다시 확인합니다.\n'
                                 + context['evaluation']['detail'])
        with runtime_state._transaction(self.connection):
            self._check_owner(plan['plan_id'], context)
            self._candidate_unchanged(plan, context)
            runtime = runtime_state.RuntimeStateRepository(self.connection)
            resumed = runtime.transition(
                self.repo, plan['plan_id'], plan_values=values,
                project_values={'phase': 'WORKING', 'last_error': values['last_error']},
                expected_status='PLAN_BLOCKED', enqueue=False)
            scoped_blocking.clear_plan_block(self.connection, self.repo, plan['plan_id'])
            runtime.hydrate_snapshot(self.repo, resumed)
            self._save(plan['plan_id'], context['input_identity'], 'complete', context)

    def _candidate_unchanged(self, plan, context):
        self._remote(context, allow_published=True)
        if self.ports._worktree_state_signature(Path(plan['worktree'])) != context['candidate']:
            raise RuntimeError('차단 원인 검사 뒤 후보 HEAD·index·작업물이 변경됐습니다.')
        _, decision = self.ports._current_rebase_service(
            self.connection, self.repo_path, self.state_dir, self.repo, plan['plan_id'],
            Path(plan['worktree']), plan['branch'], target=context['base_oid'],
        ).require_owned(checkpoint=ExecutionCheckpointRepository(self.connection).get(self.repo, plan['plan_id']))
        if decision.status.value != 'complete':
            raise RuntimeError('차단 후보가 현재 소유 범위·target base·clean index 조건을 충족하지 않습니다.')

    def reconcile(self, plan_id):
        row = self.connection.execute('SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
                                      (self.repo, plan_id)).fetchone()
        if row is None:
            return 'unhandled'
        # Legacy projects can reach the existing document/scope recovery before
        # repository validation has backfilled their registered origin. Their
        # unavailable observation is not a remote-refresh input or a reason to
        # replace the saved block; keep that original recovery boundary.
        if not row['registered_origin']:
            return 'unhandled'
        if not slot_available(self.connection, self.repo, plan_id):
            return 'deferred'
        if self.ports.unmet_execplan_dependencies(self.connection, self.repo, plan_id):
            return 'deferred'
        saved = self.connection.execute('SELECT * FROM blocked_plan_reconciliations WHERE repo = ? AND plan_id = ?',
                                        (self.repo, plan_id)).fetchone()
        if saved is None or saved['stage'] in TERMINAL:
            if not observations.pending_input(row):
                if self._plan(plan_id)['blocked_reason'] == BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value:
                    return 'unhandled'
                return 'blocked'
            plan = self._plan(plan_id)
            block = scoped_blocking.plan_block(self.connection, self.repo, plan_id)
            if (plan is None or block is None or plan['status'] != 'PLAN_BLOCKED'
                    or not plan['worktree'] or not plan['branch'] or plan['pr_url']
                    or plan['workspace_owned'] != 1):
                return 'unhandled'
            context = {field: row[field] for field in ('registered_origin', 'origin_url', 'base_ref',
                       'branch_ref', 'base_oid', 'branch_oid')}
            context.update(plan_id=plan_id, identity={field: plan[field] for field in IDENTITY_FIELDS},
                           block=dict(block), artifacts={field: plan[field] for field in ARTIFACT_FIELDS})
            key, stage = observations.input_identity(row), 'observed'
            context['input_identity'] = key
            self._save(plan_id, key, stage, context)
        else:
            key, stage, context = saved['input_identity'], saved['stage'], json.loads(saved['context'])
        try:
            plan = self._check_owner(plan_id, context)
            if stage == 'observed':
                self._remote(context)
                checkpoint = ExecutionCheckpointRepository(self.connection).get(self.repo, plan_id)
                self._validate(plan, rebase=checkpoint.operation in {'rebase', 'rebase_publish'})
                self._adopt(plan, context)
                stage = 'adopted'
                self._save(plan_id, key, stage, context)
            if stage == 'adopted':
                self._incorporate(self._check_owner(plan_id, context), context)
                stage = 'incorporated'
                self._save(plan_id, key, stage, context)
            if stage == 'incorporated':
                plan = self._check_owner(plan_id, context)
                self._candidate_unchanged(plan, context)
                context['evaluation'] = self._evaluate(plan, context)
                stage = 'evaluated'
                self._save(plan_id, key, stage, context)
            if stage == 'evaluated':
                plan = self._check_owner(plan_id, context)
                self._candidate_unchanged(plan, context)
                if context['evaluation']['resolved'] and scoped_blocking.project_block(self.connection, self.repo) is None:
                    self._resume(plan, context)
                    outcome = 'resumed'
                else:
                    self._publish(plan, context)
                    outcome = 'blocked'
                project = self.connection.execute('SELECT * FROM projects WHERE repo = ?', (self.repo,)).fetchone()
                self.ports.observe_blocked_plan_remotes(self.connection, self.repo_path, self.repo, project, plan_id=plan_id)
                current = self.connection.execute('SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
                                                  (self.repo, plan_id)).fetchone()
                with runtime_state._transaction(self.connection):
                    self._save(plan_id, key, 'complete', context)
                    if (current is not None and current['base_oid'] == context['base_oid']
                            and current['branch_oid'] == self._plan(plan_id)['publication_remote_oid']):
                        observations.mark_processed(self.connection, self.repo, plan_id, observations.input_identity(current))
                return outcome
        except (OSError, RuntimeError, ValueError) as error:
            checkpoint = ExecutionCheckpointRepository(self.connection).get(self.repo, plan_id)
            request = self.connection.execute(
                'SELECT phase FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                (self.repo, plan_id)).fetchone()
            retry = (checkpoint.operation is not None or isinstance(error, AssessmentPending)
                     or isinstance(error, RecoveryFailure)
                     and error.reason is RecoveryReason.PUBLICATION_RETRY
                     or request is not None and request['phase'] == 'retry_pending')
            self.ports._update_blocked_error(self.connection, self.repo, plan_id, str(error), preserve_cause=True)
            if not retry:
                with runtime_state._transaction(self.connection):
                    self._save(plan_id, key, 'refused', context)
                    if not isinstance(error, InputChanged):
                        observations.mark_processed(self.connection, self.repo, plan_id, key)
            return 'blocked'
        return 'blocked'
