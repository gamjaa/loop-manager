"""Serial recovery of a linked project block without starting an Agent.

Current-state scope is independent of publication lineage. Diagnostic refs
preserve artifacts but never update the original branch's ownership records.
"""
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from dataclasses import dataclass, replace
from collections.abc import Callable
import json
from pathlib import Path
from loop_manager_internal.runtime.resource_lock import resource_locks
from loop_manager_internal.runtime.contracts import ProcessLockBusyError

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery.blocked_checkpoint import CheckpointPushError, PreparedCheckpoint, PushMode, decide_diagnostic_push
from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository, DiagnosticPublicationRepository
from loop_manager_internal.recovery.blocked_plan_reconciliation import ARTIFACT_FIELDS, IDENTITY_FIELDS, TERMINAL, AssessmentPending, BlockedPlanReconciler, InputChanged, slot_available, RecoveryWorkspaceEffects
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from loop_manager_internal.git.managed_rebase_adoption import revalidation_values
from loop_manager_internal.git.managed_rebase_observation import observe_current_rebase
from loop_manager_internal.git.managed_rebase_validation import ObservationFailure, valid_acceptance
from loop_manager_internal.recovery import blocked_remote_observation as observations
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
from loop_manager_internal.storage.execplan_catalog_state import ReconciliationKind


@dataclass(frozen=True)
class ProjectRecoveryEffects(RecoveryWorkspaceEffects):
    """Explicit effects for this service; persisted decisions remain in repositories."""
    _publish_direct_rebase_checkpoint: Callable[..., object]
    push_diagnostic_checkpoint: Callable[..., object]
    reconcile_execplan_catalog: Callable[..., object]
    resume_project_block_if_safe: Callable[..., object]
    preserve_blocked_diagnostics: Callable[..., object] | None = None


@dataclass(frozen=True)
class CurrentScopeEffects:
    """Read current rebase scope independently of publication lineage."""
    _current_rebase_service: Callable[..., object]


class LocalWorkPending(RuntimeError):
    """The same unresolved work needs no repeated scope/rebase attempt."""


def project_slot_available(connection, repo, plan_id):
    block = scoped_blocking.project_block(connection, repo)
    runtime = runtime_state.RuntimeStateRepository(connection)
    projected = runtime.projected_row(repo)
    return bool(block is not None and block['plan_id'] == plan_id
                and slot_available(connection, repo, plan_id, project_recovery=True)
                and (projected is None or projected['plan_id'] == plan_id))


def independent_scope(ports, connection, repo_path, state_dir, repo, plan,
                      *, target, remote_oid):
    """Inspect the selected work only; old rebase execution records are unused."""
    if not project_slot_available(connection, repo, plan['plan_id']):
        raise RuntimeError('project 복구의 대상 계획 또는 실행 자리를 증명할 수 없습니다.')
    if plan['agent_recovery']:
        raise RuntimeError('별도 Agent 범위 복구가 끝나지 않아 local-only로 보존합니다.')
    worktree, branch = Path(plan['worktree']), plan['branch']
    service = ports._current_rebase_service(
        connection, repo_path, state_dir, repo, plan['plan_id'], worktree, branch,
        target=target, remote_oid=remote_oid)
    observed, scope = observe_current_rebase(service.expected, service.adapters)
    if (isinstance(observed, ObservationFailure) or scope is None or not scope.allowed
            or observed.remote != service.expected.remote):
        detail = observed.detail if isinstance(observed, ObservationFailure) else '현재 작업 범위 또는 원격 기대값 불일치'
        raise RuntimeError('project 복구 작업물을 검증할 수 없습니다: ' + detail)
    checkpoint = ExecutionCheckpointRepository(connection).get(repo, plan['plan_id'])
    if observed.operations or observed.metadata is not None:
        # An owned pending rebase is preserved for manual resolution. It is
        # never a committable/publishable checkpoint.
        service.require_owned(checkpoint=checkpoint)
        raise LocalWorkPending('미해결 Git operation 또는 rebase 충돌을 local-only로 보존합니다.')
    registration = set(observed.registration or ())
    if (observed.branch != branch or observed.branch_ref != (observed.head, '')
            or observed.branch_worktrees != (str(worktree.resolve()),)
            or f'HEAD={observed.head}' not in registration
            or f'branch=refs/heads/{branch}' not in registration
            or any(item.startswith(('prunable=', 'detached=')) for item in registration)
            or any(entry.stage for entry in observed.index)):
        raise RuntimeError('project 복구 HEAD·branch·등록·index 또는 PR 상태가 안전하지 않습니다.')
    return service, observed


def _confirmed_original_publication(checkpoint, plan):
    """Read the current acceptance stored by a confirmed intermediate push.

    Publication advances both generation and remote OID beyond the original
    rebase acceptance. Its result must still match this selected plan and HEAD.
    """
    intent = checkpoint.rebase_publication_intent()
    result = (checkpoint.snapshot or {}).get('rebase_publication_result')
    if intent is None or not isinstance(result, dict):
        return None
    accepted = result.get('current_acceptance')
    if (checkpoint.operation is not None or not valid_acceptance(accepted)
            or checkpoint.generation != accepted['generation'] + 1
            or result.get('intent_id') != intent['id']
            or result.get('head') != intent['head']
            or any(accepted[key] != intent[key] for key in (
                'repo', 'plan_id', 'repository_identity', 'branch', 'worktree',
                'target_base_oid', 'head'))
            or (plan['repo'], plan['plan_id'], plan['branch'], plan['worktree'])
            != (intent['repo'], intent['plan_id'], intent['branch'], intent['worktree'])
            or (accepted['remote']['pull_request'] or {}).get('identity') != (plan['pr_url'] or None)
            or accepted['remote']['oid'] != intent['head']
            or checkpoint.observed_remote_oid != intent['head']
            or plan['publication_remote_oid'] != intent['head']):
        return None
    return accepted


def checkpoint_scope_verified(ports, connection, repo_path, state_dir, request, plan):
    try:
        row = connection.execute(
            'SELECT base_oid, branch_oid FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
            (request.repo, request.plan_id)).fetchone()
        checkpoint = ExecutionCheckpointRepository(connection).get(request.repo, request.plan_id)
        target = checkpoint.target_base_oid or (row['base_oid'] if row else None)
        if not target or row is None:
            return False
        accepted = checkpoint.current_rebase_acceptance() or _confirmed_original_publication(checkpoint, plan)
        remote = checkpoint.observed_remote_oid if accepted else row['branch_oid']
        independent_scope(ports, connection, repo_path, state_dir, request.repo, plan,
                          target=target, remote_oid=remote)
        return True
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


class ProjectBlockedReconciler(BlockedPlanReconciler):
    """Reuse G-106 ref/adoption services with a separate project safety gate."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connection.execute('''CREATE TABLE IF NOT EXISTS project_blocked_reconciliations (
            repo TEXT NOT NULL, plan_id TEXT NOT NULL, input_identity TEXT NOT NULL,
            stage TEXT NOT NULL, context TEXT NOT NULL, PRIMARY KEY (repo, plan_id)
        )''')

    def _save(self, plan_id, key, stage, context):
        with runtime_state._transaction(self.connection):
            self.connection.execute(
                'INSERT INTO project_blocked_reconciliations VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(repo, plan_id) DO UPDATE SET input_identity = excluded.input_identity, '
                'stage = excluded.stage, context = excluded.context',
                (self.repo, plan_id, key, stage, json.dumps(context, sort_keys=True)))

    def _check_owner(self, plan_id, context):
        plan = self._plan(plan_id)
        if (plan is None or plan['status'] != context['status']
                or not project_slot_available(self.connection, self.repo, plan_id)
                or {field: plan[field] for field in IDENTITY_FIELDS} != context['identity']
                or not scoped_blocking.project_cause_matches(self.connection, context['project_block'])
                or self.ports.unmet_execplan_dependencies(self.connection, self.repo, plan_id)):
            raise RuntimeError('project 복구의 계획·원인·소유권 또는 실행 자리가 변경됐습니다.')
        block = scoped_blocking.plan_block(self.connection, self.repo, plan_id)
        if (dict(block) if block else None) != context['plan_block']:
            raise RuntimeError('project 복구 중 독립된 계획 차단 원인이 변경됐습니다.')
        return plan

    def _remote(self, context, *, allow_published=False):
        checkpoint = ExecutionCheckpointRepository(self.connection).get(self.repo, context['plan_id'])
        pinned = (checkpoint.operation in {'rebase', 'rebase_publish'}
                  and checkpoint.target_base_oid == context['base_oid'])
        return super()._remote(context, allow_published=allow_published or pinned)

    def _request(self, plan, context):
        store = BlockedCheckpointRepository(self.connection)
        row = self.connection.execute('SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                                      (self.repo, plan['plan_id'])).fetchone()
        if row is not None:
            request = store.get(self.repo, plan['plan_id'])
            if (request.branch, request.worktree) != (plan['branch'], plan['worktree']):
                raise RuntimeError('저장된 checkpoint와 현재 작업 공간 identity가 다릅니다.')
        else:
            request = store.request(
                repo=self.repo, plan_id=plan['plan_id'], reason_code=context['block']['reason_code'],
                evidence=context['block']['evidence'], branch=plan['branch'], worktree=plan['worktree'],
                expected_remote_oid=plan['publication_remote_oid'] or plan['pr_head_oid'], rebase_recovery=False)
            self.connection.commit()
        return store, request

    def _lineage_known(self, plan, context):
        row = self.connection.execute('SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                                      (self.repo, plan['plan_id'])).fetchone()
        original = (row['accepted_remote_oid'] or row['published_oid']) if row else None
        original = original or plan['publication_remote_oid'] or plan['pr_head_oid']
        if plan['pr_url'] and original == plan['pr_head_oid'] == context['branch_oid']:
            # The persisted PR head and the live PR/ref are a publication owner
            # even when the first blocked checkpoint has not yet been created.
            return self._remote(context) == original
        if original:
            # Publication lineage is proved at its last accepted OID. A
            # currently valid rebased HEAD need not contain that old commit.
            return self.ports.owned_remote_oid(
                self.connection, self.repo_path, self.repo, plan['plan_id'], plan['branch'],
                Path(plan['worktree']), original, self.ports.git,
                before_head=original, recorded_oid=original, require_live=False,
                strict_observation=True) == original
        # Creating an absent original ref is safe only without any earlier
        # publication. A deleted known ref must never be recreated here.
        return bool(context['branch_oid'] is None and row is not None
                    and not row['remote_observed'] and row['publication_history'] == '[]')

    def _adopt(self, plan, context):
        row = self.connection.execute('SELECT phase FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                                      (self.repo, plan['plan_id'])).fetchone()
        if row is not None and row['phase'] == 'published':
            self._check_owner(plan['plan_id'], context)
            return super()._adopt(plan, context, project_recovery_context=context)
        if context['branch_oid'] not in (None, plan['publication_remote_oid']):
            raise RuntimeError('기존 checkpoint 없이 원격 후속 commit을 채택하지 않습니다.')

    def _validate(self, plan, *, rebase=False):
        self.ports.validate_execplan_workspace_identity(
            self.state_dir, self.repo, manual_workspace_owner(self.connection, self.repo, plan['plan_id']), plan['branch'], Path(plan['worktree']))
        self.ports.validate_managed_worktree(self.repo_path, Path(plan['worktree']), plan['branch'], allow_rebase=rebase)
        request = BlockedCheckpointRepository(self.connection).get(self.repo, plan['plan_id'])
        adapter = self.ports._BlockedCheckpointAdapter(
            self.connection, self.repo_path, self.state_dir, self.repo, project_recovery_scope=True)
        verdict = adapter._origin_safety_error(request, self.ports.capture_git_snapshot(Path(plan['worktree'])))
        if verdict is not None:
            raise RuntimeError(verdict.detail)
        return Path(plan['worktree']), plan['branch']

    def _local_checkpoint(self, plan, context, adapter):
        store, request = self._request(plan, context)
        verdict = adapter.assess(request)
        if not verdict.safe:
            raise RuntimeError(verdict.detail or 'project checkpoint scope 불확실')
        if 'prepared' not in context:
            prepared = adapter.prepare(request, verdict)
            context['prepared'] = dict(parent_oid=prepared.parent_oid, tree_oid=prepared.tree_oid,
                                       commit_identity=prepared.commit_identity, message=prepared.message,
                                       included_paths=prepared.included_paths)
            self._save(plan['plan_id'], context['input_identity'], 'selected', context)
            store.record_prepared(request, prepared)
        prepared = PreparedCheckpoint(**{**context['prepared'],
                                        'included_paths': tuple(context['prepared']['included_paths'])})
        self._check_owner(plan['plan_id'], context)
        local = adapter.ensure_local_commit(request, prepared)
        store.record_local(request, local)
        return local

    def _incorporate(self, plan, context):
        plan_id, worktree, branch = plan['plan_id'], Path(plan['worktree']), plan['branch']
        checkpoints = ExecutionCheckpointRepository(self.connection)
        self._validate(plan, rebase=True)
        current = checkpoints.get(self.repo, plan_id)
        if current.operation not in {None, 'rebase', 'rebase_publish'}:
            raise RuntimeError('다른 미완료 Git intent가 있어 local-only로 보존합니다.')
        if current.operation == 'rebase_publish':
            if context['diagnostic']:
                raise RuntimeError('원래 branch의 미완료 게시 intent를 진단 게시로 대체하지 않습니다.')
            self.ports._publish_direct_rebase_checkpoint(
                self.connection, self.repo_path, self.state_dir, self.repo, plan_id, worktree, branch)
            current = checkpoints.get(self.repo, plan_id)
        target = current.target_base_oid if current.operation == 'rebase' else context['base_oid']
        if target != context['base_oid']:
            raise InputChanged('미완료 rebase의 target을 먼저 복구해야 합니다.')
        remote = self._remote(context, allow_published=True)
        if remote != context['branch_oid']:
            if context['diagnostic']:
                raise InputChanged('진단 복구 중 원래 branch OID가 변경됐습니다.')
            intent = current.rebase_publication_intent()
            result = (current.snapshot or {}).get('rebase_publication_result') or {}
            if not (intent and result.get('intent_id') == intent['id']
                    and intent['head'] == remote == self._plan(plan_id)['publication_remote_oid']):
                self._adopt(plan, {**context, 'branch_oid': remote})
            context['branch_oid'] = remote
        accepted = current.current_rebase_acceptance() or _confirmed_original_publication(current, self._plan(plan_id))
        completed = bool(accepted and accepted['target_base_oid'] == target
                         and accepted['remote']['oid'] == remote
                         and accepted['head'] == self.ports.git(
                             worktree, 'rev-parse', 'HEAD^{commit}').stdout.strip())
        if current.operation != 'rebase' and not completed:
            with resource_locks(self.state_dir, str(self.repo_path.resolve()), ['git:fetch'], owner='project-recovery-fetch'):
                fetched = self.ports.git(self.repo_path, 'fetch', '--no-tags', '--refmap=', 'origin', context['base_ref'], check=False)
                if fetched.returncode:
                    raise AssessmentPending('project 복구 base fetch 실패: ' + fetched.stdout[-2000:])
                if self.ports.git(self.repo_path, 'rev-parse', 'FETCH_HEAD^{commit}').stdout.strip() != target:
                    raise InputChanged('project 복구 base 조회·fetch 사이 OID 경쟁')
            self._remote(context, allow_published=True)
        if current.operation != 'rebase' and not completed:
            # Before HEAD is a current identity, not historical replay evidence.
            head = self.ports.git(worktree, 'rev-parse', 'HEAD^{commit}').stdout.strip()
            current = checkpoints.record(
                self.repo, plan_id, expected_generation=current.generation, operation=None,
                expected_head=head, target_base_oid=target, observed_remote_oid=remote,
                snapshot={**(current.snapshot or {}), 'before_head': head})
        service, observed = independent_scope(
            self.ports, self.connection, self.repo_path, self.state_dir, self.repo, plan,
            target=target, remote_oid=remote)
        adapter = self.ports._BlockedCheckpointAdapter(
            self.connection, self.repo_path, self.state_dir, self.repo, project_recovery_scope=True)
        if observed.files or observed.staged or (
                context.get('prepared') and current.operation != 'rebase' and not completed):
            self._local_checkpoint(plan, context, adapter)
            current = checkpoints.get(self.repo, plan_id)
            head = self.ports.git(worktree, 'rev-parse', 'HEAD^{commit}').stdout.strip()
            current = checkpoints.record(
                self.repo, plan_id, expected_generation=current.generation, operation=None,
                expected_head=head, target_base_oid=target, observed_remote_oid=remote,
                snapshot={**(current.snapshot or {}), 'before_head': head})
        service = self.ports._current_rebase_service(
            self.connection, self.repo_path, self.state_dir, self.repo, plan_id, worktree, branch,
            target=target, remote_oid=remote)
        observed, decision = service.require_owned(checkpoint=current)
        if decision.status.value == 'ready_to_start':
            self._check_owner(plan_id, context)
            rebased = self.ports._journaled_direct_rebase(
                self.connection, self.repo_path, self.repo, plan_id, worktree, branch, target,
                agent_session_id=plan['worker_session_id'], observed_remote=(remote,))
            if rebased.returncode:
                raise LocalWorkPending('project 복구 rebase 충돌을 local-only로 보존합니다: ' + rebased.stdout[-2000:])
            current = checkpoints.get(self.repo, plan_id)
            service = self.ports._current_rebase_service(
                self.connection, self.repo_path, self.state_dir, self.repo, plan_id, worktree, branch,
                target=target, remote_oid=remote)
            observed, decision = service.require_owned(checkpoint=current)
        if decision.status.value != 'complete' or observed.files:
            raise LocalWorkPending('rebase 미완료 또는 미커밋 작업을 local-only로 보존합니다.')
        confirmed = service.observe(checkpoint=current)[1]
        with runtime_state._transaction(self.connection):
            self._check_owner(plan_id, context)
            if checkpoints.get(self.repo, plan_id) != current or confirmed != decision:
                raise InputChanged('project 복구 결과의 현재 Git 상태가 변경됐습니다.')
            values = revalidation_values(target)
            for field in ('phase', 'last_error', 'revision_resume_status'):
                values.pop(field)
            if not completed:
                checkpoints.accept_current_rebase(self.repo, plan_id, acceptance=decision.acceptance,
                                                  prior_artifacts=context['artifacts'])
            runtime_state.RuntimeStateRepository(self.connection).update_plan(
                self.repo, plan_id, plan_values=values, enqueue=False)
        if not context['diagnostic']:
            # G-104 owns the existing exact lease. No project hydration occurs.
            self.ports._publish_direct_rebase_checkpoint(
                self.connection, self.repo_path, self.state_dir, self.repo, plan_id, worktree, branch)
        context['candidate'] = self.ports._worktree_state_signature(worktree)

    def _publish(self, plan, context):
        store, request = self._request(plan, context)
        adapter = self.ports._BlockedCheckpointAdapter(
            self.connection, self.repo_path, self.state_dir, self.repo, project_recovery_scope=True)
        expected = self._remote(context, allow_published=True)
        head = self.ports.git(Path(plan['worktree']), 'rev-parse', 'HEAD^{commit}').stdout.strip()
        repository = DiagnosticPublicationRepository(self.connection)
        if context['diagnostic']:
            tree = self.ports.git(Path(plan['worktree']), 'rev-parse', 'HEAD^{tree}').stdout.strip()
            ref = blocked_checkpoint.diagnostic_branch(request.repo, request.plan_id,
                request.branch, context['base_oid'], head, tree)
            with resource_locks(self.state_dir, self.repo, ['diagnostic:' + ref], owner='diagnostic-publication'):
                retired = repository.cleanup_receipt(self.repo, ref, context['cause_identity'])
                if retired is not None:
                    return  # Read-side projection retains the historical receipt.
                intent = repository.intent(request, base_oid=context['base_oid'], head_oid=head,
                                           tree_oid=tree, cause=context['project_block']['evidence'])
                target = replace(request, branch=intent['ref'])
                try:
                    oid = adapter.observe_remote(target)
                except (OSError, RuntimeError) as error:
                    raise AssessmentPending('진단 ref 원격 조회를 재시도합니다.') from error
                decision = decide_diagnostic_push(head, oid)
                if decision.mode is PushMode.REJECT:
                    raise RuntimeError(decision.reason)
                self._candidate_unchanged(plan, context)
                if decision.mode is PushMode.CREATE:
                    self.ports.push_diagnostic_checkpoint(adapter, target, head)
                try:
                    confirmed = adapter.observe_remote(target)
                except (OSError, RuntimeError) as error:
                    raise AssessmentPending('진단 ref push 결과 조회를 재시도합니다.') from error
                if confirmed != head:
                    raise AssessmentPending('진단 ref push 결과의 OID를 확인할 수 없습니다.')
                payload = dict(result='published', diagnostic=True, branch=intent['ref'], commit_oid=head,
                               original_branch=request.branch, base_oid=context['base_oid'])
                repository.result(self.repo, plan['plan_id'], context['cause_identity'], payload, intent=intent)
        else:
            store.refresh(request, expected_remote_oid=expected)
            request = store.get(self.repo, plan['plan_id'])
            try:
                blocked_checkpoint.BlockedCheckpointDispatcher(
                    store, adapter, adapter, self.ports._BlockedCheckpointResults(self.connection))._dispatch_one(request)
            except (OSError, RuntimeError) as error:
                raise AssessmentPending('원래 branch checkpoint 게시 관측을 재시도합니다.') from error
            request = store.get(self.repo, plan['plan_id'])
            if request.phase.value != 'published':
                row = self.connection.execute('SELECT unpublished_reason FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                                              (self.repo, plan['plan_id'])).fetchone()
                raise AssessmentPending(row[0] or '원래 branch checkpoint 게시 미완료')
            repository.result(self.repo, plan['plan_id'], context['cause_identity'],
                              dict(result='published', branch=request.branch, commit_oid=request.remote_oid))

    def _candidate_unchanged(self, plan, context):
        self._check_owner(plan['plan_id'], context)
        self._remote(context, allow_published=True)
        if self.ports._worktree_state_signature(Path(plan['worktree'])) != context['candidate']:
            raise InputChanged('project 복구 후보 HEAD·index·작업물이 변경됐습니다.')
        observed, decision = self.ports._current_rebase_service(
            self.connection, self.repo_path, self.state_dir, self.repo, plan['plan_id'],
            Path(plan['worktree']), plan['branch'], target=context['base_oid'],
            remote_oid=self._remote(context, allow_published=True),
        ).require_owned(checkpoint=ExecutionCheckpointRepository(self.connection).get(self.repo, plan['plan_id']))
        if decision.status.value != 'complete':
            raise RuntimeError('project 복구 후보의 현재 rebase 상태가 완료되지 않았습니다.')
        context['cleanup_pending_work'] = bool(observed.files) if decision.acceptance else True
        return decision.acceptance

    def _revalidate_project_cause(self, plan, context):
        with runtime_state._transaction(self.connection):
            self._check_owner(plan['plan_id'], context)
            acceptance = self._candidate_unchanged(plan, context)
            reason = context['project_block']['reason_code']
            proven_publication = False
            if reason == scoped_blocking.BlockReasonCode.PUBLICATION_OWNERSHIP_UNCERTAIN.value and not context['diagnostic']:
                row = self.connection.execute(
                    'SELECT cause_identity, payload FROM project_checkpoint_results WHERE repo = ? AND plan_id = ?',
                    (self.repo, plan['plan_id'])).fetchone()
                result = json.loads(row['payload']) if row else {}
                proven_publication = bool(row and row['cause_identity'] == context['cause_identity']
                                          and result.get('result') == 'published' and not result.get('diagnostic')
                                          and result.get('commit_oid') == self._plan(plan['plan_id'])['publication_remote_oid']
                                          and self._lineage_known(self._plan(plan['plan_id']), context))
            if proven_publication or reason == scoped_blocking.BlockReasonCode.WORKSPACE_OWNERSHIP_UNCERTAIN.value:
                scoped_blocking.clear_revalidated_rebase_block(
                    self.connection, context['project_block'], is_plan_block=False,
                    acceptance=acceptance, pending_work=context.get('cleanup_pending_work', True))
            elif plan['agent_snapshot']:
                self.ports.resume_project_block_if_safe(self.connection, self.repo_path, self.state_dir, self.repo)
            released = scoped_blocking.project_block(self.connection, self.repo) is None
            if released and scoped_blocking.plan_block(self.connection, self.repo, plan['plan_id']) is None:
                # Gate release and candidate invalidation must commit together.
                values = revalidation_values(context['base_oid'])
                values.pop('phase')
                values.update(status='IMPLEMENTING')
                runtime = runtime_state.RuntimeStateRepository(self.connection)
                updated = runtime.update_plan(self.repo, plan['plan_id'], plan_values=values, enqueue=False)
                runtime.hydrate_snapshot(self.repo, updated)
            if released:
                current = self.connection.execute(
                    'SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
                    (self.repo, plan['plan_id']),
                ).fetchone()
                if current is not None and observations.input_identity(current) == context['input_identity']:
                    observations.mark_processed(
                        self.connection, self.repo, plan['plan_id'], context['input_identity'])
                self._save(plan['plan_id'], context['input_identity'], 'complete', context)
            return released

    def refresh_catalog(self, plan_id):
        """Prove the linked scope before fetching declarations for recovery."""
        if not project_slot_available(self.connection, self.repo, plan_id):
            return False
        plan = self._plan(plan_id)
        row = self.connection.execute(
            'SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
            (self.repo, plan_id),
        ).fetchone()
        if plan is None or row is None or not row['base_oid']:
            return False
        try:
            self._validate(plan, rebase=True)
            independent_scope(
                self.ports, self.connection, self.repo_path, self.state_dir,
                self.repo, plan, target=row['base_oid'], remote_oid=row['branch_oid'],
            )
            self.ports._recorded_base_fetch(
                self.repo_path, self.state_dir, self.base, owner='project',
            )
            result = self.ports.reconcile_execplan_catalog(
                self.connection, self.repo_path, self.repo, self.base,
                record_invalid=False,
            )
            return result.kind is ReconciliationKind.RECONCILED
        except (OSError, RuntimeError, ValueError, TypeError):
            return False

    def reconcile(self, plan_id):
        if self._plan(plan_id) is None:
            # Legacy project projections may name a plan absent from the
            # catalog. Preserve that safety block without attempting recovery.
            return 'blocked'
        if not project_slot_available(self.connection, self.repo, plan_id):
            return 'deferred'
        if self.ports.unmet_execplan_dependencies(self.connection, self.repo, plan_id):
            return 'deferred'
        row = self.connection.execute('SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
                                      (self.repo, plan_id)).fetchone()
        if row is None:
            return 'blocked'
        saved = self.connection.execute('SELECT * FROM project_blocked_reconciliations WHERE repo = ? AND plan_id = ?',
                                        (self.repo, plan_id)).fetchone()
        if saved is not None and saved['stage'] not in TERMINAL:
            context = json.loads(saved['context'])
            key, stage = saved['input_identity'], saved['stage']
            if context.get('waiting_input') == observations.input_identity(row):
                if self.ports.preserve_blocked_diagnostics is not None:
                    request = BlockedCheckpointRepository(self.connection).get(self.repo, plan_id)
                    if request:
                        self.ports.preserve_blocked_diagnostics(self.connection, self.repo_path, self.state_dir, request, project_scope=True)
                return 'blocked'
            context.pop('waiting_input', None)
        else:
            if not observations.pending_input(row):
                if (self.ports.preserve_blocked_diagnostics is not None
                        and (saved is None or saved['stage'] != 'complete')):
                    request = BlockedCheckpointRepository(self.connection).get(self.repo, plan_id)
                    if request:
                        self.ports.preserve_blocked_diagnostics(self.connection, self.repo_path, self.state_dir, request, project_scope=True)
                return 'blocked'
            plan = self._plan(plan_id)
            block = scoped_blocking.project_block(self.connection, self.repo)
            cause = scoped_blocking.plan_block(self.connection, self.repo, plan_id)
            key, stage = observations.input_identity(row), 'observed'
            context = {field: row[field] for field in ('registered_origin', 'origin_url', 'base_ref',
                       'branch_ref', 'base_oid', 'branch_oid')}
            context.update(plan_id=plan_id, input_identity=key, status=plan['status'],
                           identity={field: plan[field] for field in IDENTITY_FIELDS},
                           block=dict(cause or block), plan_block=dict(cause) if cause else None,
                           project_block=dict(block), cause_identity=scoped_blocking.project_cause_identity(block),
                           artifacts={field: plan[field] for field in ARTIFACT_FIELDS})
            checkpoint = ExecutionCheckpointRepository(self.connection).get(self.repo, plan_id)
            if checkpoint.operation in {'rebase', 'rebase_publish'} and checkpoint.target_base_oid:
                context['base_oid'] = checkpoint.target_base_oid
            self._save(plan_id, key, stage, context)
        try:
            plan = self._check_owner(plan_id, context)
            if not context['registered_origin'] or not context['base_oid']:
                raise RuntimeError('현재 repository·base·원격 기대값을 확정할 수 없어 local-only로 보존합니다.')
            if not plan['branch'] or not plan['worktree'] or plan['workspace_owned'] != 1:
                raise RuntimeError('현재 plan·branch·worktree 소유권을 증명할 수 없습니다.')
            if stage == 'observed':
                self._remote(context)
                self._request(plan, context)
                self._validate(plan, rebase=True)
                context['diagnostic'] = not self._lineage_known(plan, context)
                if not context['diagnostic']:
                    self._adopt(plan, context)
                stage = 'selected'
                self._save(plan_id, key, stage, context)
            if stage == 'selected':
                self._incorporate(self._check_owner(plan_id, context), context)
                stage = 'incorporated'
                self._save(plan_id, key, stage, context)
            if stage == 'incorporated':
                self._candidate_unchanged(plan, context)
                # The original project cause is rechecked by its existing
                # read-only safety boundary, after the artifact is preserved.
                self._publish(plan, context)
                stage = 'published'
                self._save(plan_id, key, stage, context)
            if stage == 'published':
                self._check_owner(plan_id, context)
                if self._revalidate_project_cause(plan, context):
                    return 'recovered'
                project = runtime_state.project_row(self.connection, self.repo)
                self.ports.observe_blocked_plan_remotes(self.connection, self.repo_path, self.repo, project, plan_id=plan_id)
                current = self.connection.execute('SELECT * FROM blocked_remote_observations WHERE repo = ? AND plan_id = ?',
                                                  (self.repo, plan_id)).fetchone()
                with runtime_state._transaction(self.connection):
                    self._save(plan_id, key, 'complete', context)
                    if (current is not None and current['base_oid'] == context['base_oid']
                            and current['branch_oid'] == self._remote(context, allow_published=True)
                            and current['worktree_fingerprint'] == context['candidate']):
                        observations.mark_processed(self.connection, self.repo, plan_id, observations.input_identity(current))
                return 'blocked' if scoped_blocking.project_block(self.connection, self.repo) else 'recovered'
        except (OSError, RuntimeError, ValueError) as error:
            # A local-only diagnostic is a result, never a replacement cause.
            self.ports._update_blocked_error(self.connection, self.repo, plan_id, str(error), preserve_cause=True)
            checkpoint = ExecutionCheckpointRepository(self.connection).get(self.repo, plan_id)
            if isinstance(error, LocalWorkPending):
                waiting = dict(row)
                waiting['worktree_fingerprint'] = self.ports._worktree_state_signature(Path(context['identity']['worktree']))
                context['waiting_input'] = observations.input_identity(waiting)
                self._save(plan_id, key, stage, context)
            retryable = (isinstance(error, (AssessmentPending, ProcessLockBusyError))
                         or isinstance(error, RecoveryFailure)
                         and error.reason is RecoveryReason.PUBLICATION_RETRY
                         or isinstance(error, CheckpointPushError) and error.retryable)
            if stage != 'published' and not retryable and not isinstance(error, InputChanged):
                try:
                    DiagnosticPublicationRepository(self.connection).result(
                        self.repo, plan_id, context['cause_identity'],
                        dict(result='local_only', reason=str(error), original_branch=context['identity']['branch'],
                             base_oid=context['base_oid'], worktree=context['identity']['worktree']))
                except RuntimeError:
                    # A newer owner/cause has replaced this recovery attempt.
                    return 'blocked'
            if checkpoint.operation is None and not retryable and not isinstance(error, LocalWorkPending):
                with runtime_state._transaction(self.connection):
                    self._save(plan_id, key, 'refused', context)
                    if not isinstance(error, InputChanged):
                        observations.mark_processed(self.connection, self.repo, plan_id, key)
            if self.ports.preserve_blocked_diagnostics is not None:
                request = BlockedCheckpointRepository(self.connection).get(self.repo, plan_id)
                if request:
                    self.ports.preserve_blocked_diagnostics(self.connection, self.repo_path, self.state_dir, request, project_scope=True)
            return 'blocked'
