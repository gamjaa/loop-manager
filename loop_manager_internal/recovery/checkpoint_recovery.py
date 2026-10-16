"""Checkpoint successor adoption and cause-specific candidate revalidation."""
from __future__ import annotations
from loop_manager_internal.runtime.resource_lock import resource_locks
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
from loop_manager_internal.agents.agent_invocation import InvocationContext
from loop_manager_internal.agents.agent_prompts import reviewer_prompt
from loop_manager_internal.agents.agent_scope_decision import _non_default_index_flags
from loop_manager_internal.agents.agent_scope_decision import scoped_metadata, used_git_resources
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.documents import reviewer_specification
from loop_manager_internal.execution import execution_revision
from loop_manager_internal.execution.result_validation import reviewer_targeted_test_evidence
from loop_manager_internal.execution.validation_feedback import normalize_feedback
from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
from loop_manager_internal.git import managed_rebase_adoption, managed_rebase_validation
from loop_manager_internal.git.base_fetch_service import reconcile_external_base
from loop_manager_internal.git.managed_rebase_validation import _safe_path
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import project_blocked_recovery
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery import work_branch_integration
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.runtime.runtime_identity import _canonical_path
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage import validation_feedback_state
from pathlib import Path
import json
import sqlite3

@dataclass(frozen=True)
class ReconcilePublishedBlockedCheckpointEffects:
    """Effects for reconcile published blocked checkpoint; preserve existing safety gates."""
    _BlockedCheckpointAdapter: Callable[..., object]
    _blocked_execplan_revision_metadata_error: Callable[..., object]
    _current_rebase_service: Callable[..., object]
    _evaluate_remote_checkpoint_cause: Callable[..., object]
    _nul_git_paths: Callable[..., object]
    _published_plan_allows_common_plans: Callable[..., object]
    _reclassify_remote_checkpoint_block: Callable[..., object]
    _reclassify_remote_specification_deadlock: Callable[..., object]
    _remote_checkpoint_input_identity: Callable[..., object]
    _remote_checkpoint_refusal: Callable[..., object]
    _resume_remote_corrected_execplan: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    github_repository: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]
    _capture_agent_baseline: Callable[..., object]
    _git_with_index: Callable[..., object]
    _commit_tree: Callable[..., object]
    _journaled_direct_rebase: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]


@dataclass(frozen=True)
class EvaluateRemoteCheckpointCauseEffects:
    """Effects for evaluate remote checkpoint cause; preserve existing safety gates."""
    MAX_REVIEWER_PROTOCOL_ATTEMPTS: int
    ROOT: Path
    git: Callable[..., object]
    ReviewerProtocolError: type[Exception]
    WorkerResultError: type[Exception]
    _remote_checkpoint_has_new_failure: Callable[..., object]
    _remote_checkpoint_review_revision: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    load_managed_config: Callable[..., object]
    parse_reviewer_result: Callable[..., object]
    parse_worker_result: Callable[..., object]
    reviewer_targeted_test_reason: Callable[..., object]
    run_agent: Callable[..., object]
    validate_agent_scope: Callable[..., object]
    verify: Callable[..., object]


def _reconcile_published_blocked_checkpoint(effects: ReconcilePublishedBlockedCheckpointEffects,
    connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    blocked: sqlite3.Row,
    row: sqlite3.Row,
    request: blocked_checkpoint.CheckpointRequest,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    config_path: Path | None = None,
    verify_timeout_seconds: int | None = None,
    *, adopt_only: bool = False, expected_remote_oid: str | None = None,
    project_recovery_context: dict | None = None,
) -> str:
    """Inspect one exact remote ref without adopting unproven or unsafe work."""
    adapter = effects._BlockedCheckpointAdapter(
        connection, repo_path, state_dir, repo,
        project_recovery_scope=project_recovery_context is not None,
    )
    snapshot = None
    worktree = Path(str(blocked["worktree"] or ""))
    branch = str(blocked["branch"] or "")
    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    current = checkpoints.get(repo, str(blocked['plan_id']))
    integration = (current.snapshot or {}).get('work_branch_integration')
    base_completion = (current.snapshot or {}).get('base_rebase_restart') or {}
    integrating = integration is not None and (
        integration.get('phase') != 'complete' or current.operation == 'rebase'
        or (current.snapshot or {}).get('remote_adoption_intent'))
    integrating = integrating or blocked_plan_reconciliation.base_adoption_pending(current, branch, str(worktree))
    try:
        observed = adapter.observe_remote(request)
    except (OSError, RuntimeError) as error:
        observed = None
        observation_error = f"원격 checkpoint를 재검증할 수 없습니다: {error}"
    else:
        observation_error = None
    base_restarting = (adopt_only and current.operation == 'rebase'
                       and current.observed_remote_oid != observed
                       and (not integration or integration.get('phase') == 'complete'))
    integrating = integrating or base_restarting
    accepted = str(row["accepted_remote_oid"] or row["published_oid"] or row["remote_oid"] or current.expected_head or "")
    unchanged = observed == accepted and not row["pending_remote_oid"]
    try:
        effects.validate_execplan_workspace_identity(
            state_dir, repo, manual_workspace_owner(connection, repo, str(blocked["plan_id"])), branch, worktree,
        )
        effects.validate_managed_worktree(repo_path, worktree, branch, allow_rebase=integrating)
        if integrating and current.operation == 'rebase':
            effects._current_rebase_service(
                connection, repo_path, state_dir, repo, str(blocked['plan_id']),
                worktree, branch, remote_oid=observed).require_owned(checkpoint=current)
        snapshot = effects.capture_git_snapshot(worktree)
        ignored_paths = tuple(effects._nul_git_paths(
            worktree, "ls-files", "--others", "--ignored",
            "--exclude-standard", "-z",
        ))
    except (OSError, RuntimeError, WorkspaceOwnershipError) as error:
        safety_error = f"관리 branch/worktree 소유권을 증명할 수 없습니다: {error}"
        ignored_paths = ()
    else:
        safety_error = None
    input_identity = effects._remote_checkpoint_input_identity(
        blocked, snapshot, ignored_paths,
    )

    def refuse(message: str) -> str:
        effects._remote_checkpoint_refusal(
            connection, repository, request, row, observed, input_identity,
            message, preserve_cause=adopt_only,
        )
        return "blocked"

    def retry(message: str) -> str:
        effects._remote_checkpoint_refusal(
            connection, repository, request, row, observed, input_identity,
            message, retryable=True, preserve_cause=adopt_only,
        )
        return "blocked"

    if adopt_only and observed != expected_remote_oid:
        return retry("원격 checkpoint branch가 계획별 관측 이후 변경됐습니다.")
    if adopt_only and unchanged:
        return "unchanged"
    if unchanged:
        return "unchanged"
    if observation_error:
        return retry(observation_error)
    if project_recovery_context is not None:
        # A historical publication may outlive its PLAN_BLOCKED cause. The
        # selected project recovery owns adoption, never a synthetic plan block.
        context = project_recovery_context
        current_block = scoped_blocking.plan_block(connection, repo, request.plan_id)
        if (
            not adopt_only or request.plan_id != blocked["plan_id"]
            or context['plan_id'] != request.plan_id
            or context['status'] != blocked['status']
            or context['identity'] != {
                field: blocked[field] for field in blocked_plan_reconciliation.IDENTITY_FIELDS
            }
            or context['plan_block'] != (dict(current_block) if current_block else None)
            or not scoped_blocking.project_cause_matches(connection, context['project_block'])
            or not project_blocked_recovery.project_slot_available(connection, repo, request.plan_id)
        ):
            return refuse("checkpoint와 현재 PROJECT_BLOCKED 원인/계획 identity가 다릅니다.")
    else:
        current_block = scoped_blocking.plan_block(connection, repo, request.plan_id)
        if (
            current_block is None
            or current_block["reason_code"] != request.reason_code
            or blocked["blocked_reason"] != request.reason_code
            or request.plan_id != blocked["plan_id"]
        ):
            return refuse("checkpoint와 현재 PLAN_BLOCKED 원인/계획 identity가 다릅니다.")
    if observed is None:
        from loop_manager_internal.recovery.recovery_runtime import RecoveryFailure, RecoveryReason
        try:
            work_branch_integration.require_remote_present(connection, repo, blocked, observed, None)
        except RecoveryFailure as error:
            if error.reason is not RecoveryReason.REMOTE_BRANCH_DELETED:
                raise
            scoped_blocking.record_plan_block(connection, repo, str(blocked['plan_id']),
                BlockReasonCode.REMOTE_BRANCH_DELETED, str(error), None)
            return 'blocked'
        return retry('원격 작업 branch의 현재 상태를 다시 확인합니다.')
    if row["pending_remote_oid"] and row["pending_remote_oid"] != observed and not integrating:
        return retry("미완료 원격 checkpoint 채택 중 branch OID가 다시 변경됐습니다.")
    if int(blocked["workspace_owned"] or 0) != WORKSPACE_OWNED:
        return refuse("관리 workspace 소유권 때문에 원격 checkpoint를 채택할 수 없습니다.")
    if request.branch != branch or request.worktree != str(worktree):
        return refuse("저장된 checkpoint branch/worktree가 ExecPlan 소유권과 다릅니다.")
    if safety_error:
        return refuse(safety_error)
    assert snapshot is not None
    if snapshot.operations and not integrating:
        return refuse("미해결 Git operation: " + ", ".join(sorted(snapshot.operations)))
    if (snapshot.branch != branch and not integrating) or not snapshot.head:
        return refuse("관리 worktree의 branch 또는 HEAD가 저장된 ExecPlan과 다릅니다.")
    own_ref = f"refs/heads/{branch}"
    own_worktree = snapshot.worktrees.get(str(_canonical_path(worktree)), ())
    if (
        not snapshot.shared_metadata_complete
        or not integrating and (snapshot.refs.get(own_ref, (None, None))[0] != snapshot.head
                               or f"branch={own_ref}" not in own_worktree)
    ):
        return refuse("관리 branch ref와 worktree 등록 상태의 소유권을 증명할 수 없습니다.")
    if any(_non_default_index_flags(entries) for entries in snapshot.index.values()):
        return refuse("관리 worktree index flag가 안전하지 않습니다.")
    project = connection.execute(
        "SELECT origin FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    expected_origin = str(project["origin"] or "") if project else ""
    try:
        if not expected_origin:
            raise RuntimeError("등록 origin이 없습니다.")
        for context in (repo_path, worktree):
            for args in (("remote", "get-url", "--all", "origin"),
                         ("remote", "get-url", "--push", "--all", "origin")):
                urls = effects.git(context, *args).stdout.splitlines()
                if len(urls) != 1 or effects.github_repository(urls[0]) != expected_origin:
                    raise RuntimeError("등록 origin과 fetch/push 대상이 다릅니다.")
    except RuntimeError as error:
        return refuse(f"원격 저장소 소유권을 증명할 수 없습니다: {error}")
    try:
        # Project recovery may not trust configured mappings to shared refs.
        refmap_options = ("--refmap=",)
        with resource_locks(state_dir, str(repo_path.resolve()), ['git:fetch'], owner='checkpoint-fetch'):
            fetched = effects.git(
                repo_path, "fetch", "--no-tags", *refmap_options, "origin",
                f"refs/heads/{branch}", check=False,
            )
            fetched_result = effects.git(
                repo_path, "rev-parse", "FETCH_HEAD^{commit}", check=False,
            ) if not fetched.returncode else None
    except (OSError, RuntimeError) as error:
        return retry("원격 checkpoint branch fetch 미완료: " + str(error))
    if fetched.returncode:
        return retry("원격 checkpoint branch fetch 실패: " + fetched.stdout[-2000:])
    assert fetched_result is not None
    if fetched_result.returncode:
        return retry("fetch된 원격 checkpoint OID를 확인할 수 없습니다.")
    fetched_oid = fetched_result.stdout.strip()
    if fetched_oid != observed:
        return retry("원격 checkpoint branch가 조회와 fetch 사이에 변경됐습니다.")
    original_head = snapshot.head
    if integrating:
        original_head = (current.expected_head if base_restarting or not integration or integration['phase'] == 'complete'
                         else GitSnapshot.deserialize(integration['before']).head)
    history_head = original_head
    acceptance = (current.snapshot or {}).get('current_rebase_acceptance') or {}
    if (base_completion.get('acceptance_id') == acceptance.get('id')
            and managed_rebase_validation.valid_acceptance(acceptance)
            and base_completion.get('remote_before') == accepted
            and base_completion.get('remote') == acceptance['remote']['oid']
            and base_completion.get('head') == original_head
            and all(acceptance[key] == value for key, value in (
                ('repo', repo), ('plan_id', str(blocked['plan_id'])), ('plan_path', str(blocked['path'])),
                ('branch', branch), ('worktree', str(_canonical_path(worktree))), ('head', original_head)))):
        # This stable completed base result may rewrite accepted's OID. Its
        # recorded original start still has to contain that accepted input.
        # During a later work-branch rebase, the current operation was checked
        # above; before it starts, recheck the completed base result itself.
        if current.operation is None:
            try:
                observed_result, decision = effects._current_rebase_service(
                    connection, repo_path, state_dir, repo, str(blocked['plan_id']), worktree, branch,
                    target=acceptance['target_base_oid'], remote_oid=observed).require_owned(checkpoint=current)
                if decision.status.value != 'complete' or observed_result.head != original_head:
                    return retry('완료된 base rebase의 현재 결과가 변경됐습니다.')
            except (OSError, RuntimeError, ValueError) as error:
                return refuse(str(error))
        history_head = base_completion['original_head']
    if effects.git(repo_path, 'merge-base', original_head, observed, check=False).returncode:
        return refuse('현재 로컬·원격 작업 branch의 공통 이력을 확인할 수 없습니다.')
    try:
        diff_base = accepted or original_head
        if effects.git(repo_path, 'cat-file', '-e', diff_base + '^{commit}', check=False).returncode:
            diff_base = original_head
        changed_paths = effects._nul_git_paths(
            repo_path, "diff", "--no-renames", "--name-only", "-z",
            diff_base, observed, "--",
        )
    except RuntimeError as error:
        return retry(f"원격 checkpoint 변경 경로를 확인할 수 없습니다: {error}")
    try:
        sources = tuple(str(source) for source in json.loads(str(blocked["sources"])))
    except (TypeError, ValueError):
        return refuse("ExecPlan source 목록을 검증할 수 없습니다.")
    for path in changed_paths:
        # Remote successor commits are user input, not Worker output. File
        # safety and current ExecPlan identity remain independent gates.
        if not _safe_path(path):
            return refuse(f'원격 checkpoint의 경로가 안전하지 않습니다: {path}')
        entry = effects.git(repo_path, "ls-tree", observed, "--", path).stdout
        if entry and not entry.startswith("100644 ") and not entry.startswith("100755 "):
            return refuse(f"원격 checkpoint에 symlink 또는 특수 파일이 있습니다: {path}")
    for local_path in ignored_paths:
        if any(
            local_path == remote_path
            or local_path.startswith(remote_path + "/")
            or remote_path.startswith(local_path + "/")
            for remote_path in changed_paths
        ):
            return refuse(f"ignored 로컬 경로와 원격 변경이 겹칩니다: {local_path}")
    metadata_error = effects._blocked_execplan_revision_metadata_error(
        repo_path, observed, blocked,
    )
    if metadata_error:
        return refuse(metadata_error)
    if project_recovery_context is not None:
        scope_base = blocked['active_sha'] or blocked['plan_sha']
        if not scope_base:
            return refuse('현재 계획의 기존 실행 base를 확정할 수 없어 원격 checkpoint 채택을 중단합니다.')
        try:
            # Inspect the complete artifact against its existing execution
            # base, which retains archive sources. The latest base may not be
            # fetched yet; incorporation subsequently fetches and checks it.
            project_blocked_recovery.independent_scope(
                project_blocked_recovery.CurrentScopeEffects(effects._current_rebase_service), connection, repo_path, state_dir, repo, blocked,
                target=scope_base, remote_oid=observed,
            )
        except (OSError, RuntimeError, ValueError) as error:
            return refuse(str(error))
    if not integrating:
        try:
            effects.validate_blocked_agent_scope(
                connection, repo_path, repo,
                connection.execute('SELECT base_branch FROM projects WHERE repo = ?', (repo,)).fetchone()[0],
                worktree, str(blocked['path']), saved_plan=blocked, require_safe_result=True)
        except (OSError, RuntimeError, ValueError) as error:
            return refuse(str(error))
    if adopt_only:
        checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
        current = checkpoints.get(repo, str(blocked['plan_id']))
        pending_scope = (current.snapshot or {}).get('remote_adoption_intent')
        if pending_scope is None:
            saved_agent = (GitSnapshot.deserialize(str(blocked['agent_snapshot']))
                           if blocked['agent_snapshot'] else None)
            comparison_snapshot = replace(
                snapshot, scope_id=saved_agent.scope_id if saved_agent else None,
                scope_start_digest=saved_agent.scope_start_digest if saved_agent else None)
            pending_scope = {
                'head': observed, 'branch': branch, 'worktree': str(worktree),
                'request_identity': request.request_identity,
                'agent_snapshot': blocked['agent_snapshot'],
                'agent_result_snapshot': blocked['agent_result_snapshot'],
                'snapshot': comparison_snapshot.serialize(),
            }
            with runtime_state._transaction(connection):
                checkpoints.record(
                    repo, str(blocked['plan_id']), expected_generation=current.generation,
                    operation=current.operation, expected_head=current.expected_head,
                    target_base_oid=current.target_base_oid,
                    observed_remote_oid=current.observed_remote_oid,
                    snapshot={**(current.snapshot or {}), 'remote_adoption_intent': pending_scope})
        elif (integrating
              and (pending_scope['branch'], pending_scope['worktree'], pending_scope['request_identity'])
              == (branch, str(worktree), request.request_identity)):
            # Every saved input, including this pending head and a restart
            # target, was verified above as an ancestor of the exact fetch.
            pending_scope = {**pending_scope, 'head': observed}
            checkpoints.record(
                repo, str(blocked['plan_id']), expected_generation=current.generation,
                operation=current.operation, expected_head=current.expected_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
                snapshot={**(current.snapshot or {}), 'remote_adoption_intent': pending_scope})
        elif (pending_scope['head'], pending_scope['branch'], pending_scope['worktree'],
              pending_scope['request_identity']) != (observed, branch, str(worktree), request.request_identity):
            return refuse('저장된 원격 채택 intent와 현재 요청이 다릅니다.')
    if base_restarting:
        try:
            work_branch_integration.restart_base_rebase(
                effects, connection, repo_path, state_dir, repo, blocked, observed, snapshot)
        except (OSError, RuntimeError, ValueError) as error:
            return retry(str(error))
        # The pending adoption identity survives either abort or completion,
        # including interruption before the successor is actually integrated.
        return _reconcile_published_blocked_checkpoint(
            effects, connection, repo_path, state_dir, repo, blocked,
            connection.execute('SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                               (repo, blocked['plan_id'])).fetchone(),
            request, repository, config_path, verify_timeout_seconds,
            adopt_only=adopt_only, expected_remote_oid=expected_remote_oid,
            project_recovery_context=project_recovery_context)
    current = checkpoints.get(repo, str(blocked['plan_id']))
    checkpoints.record(
        repo, str(blocked['plan_id']), expected_generation=current.generation,
        operation=current.operation, expected_head=current.expected_head,
        target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
        snapshot={**(current.snapshot or {}), 'remote_user_input': observed})
    repository.start_remote_adoption(request, observed)
    relation = effects.git(repo_path, 'merge-base', '--is-ancestor',
                           snapshot.head, observed, check=False).returncode
    if relation not in {0, 1}:
        return retry('작업 branch의 로컬·원격 관계를 확인할 수 없습니다.')
    combined = integrating or bool(snapshot.dirty) or relation == 1
    if combined:
        try:
            snapshot = work_branch_integration.integrate(
                effects, connection, repo_path, state_dir, repo, blocked, observed, snapshot)
        except (OSError, RuntimeError, ValueError) as error:
            return retry(str(error))
    elif snapshot.head != observed:
        advanced = effects.git(
            worktree, "-c", "core.hooksPath=/dev/null",
            "merge", "--ff-only", "--no-edit", observed, check=False,
        )
        if advanced.returncode:
            return retry("로컬 checkpoint fast-forward 실패: " + advanced.stdout[-2000:])
    adopted_head = effects.git(worktree, "rev-parse", "HEAD^{commit}").stdout.strip()
    if not combined and adopted_head != observed:
        return refuse("fast-forward 뒤 관리 worktree HEAD가 원격 후보와 다릅니다.")
    adopted_snapshot = effects.capture_git_snapshot(worktree)
    input_identity = effects._remote_checkpoint_input_identity(
        blocked, adopted_snapshot, ignored_paths,
    )
    if adopt_only:
        scope_snapshot = managed_rebase_adoption.selected_baseline(
            GitSnapshot.deserialize(pending_scope['snapshot']), adopted_snapshot,
            branch, str(_canonical_path(worktree)),
            publication=checkpoints.get(repo, str(blocked['plan_id'])))
        remote_ref = f'refs/remotes/origin/{branch}'
        if adopted_snapshot.refs.get(remote_ref) == (observed, ''):
            scope_snapshot = replace(scope_snapshot, refs={
                **scope_snapshot.refs, remote_ref: (observed, '')})
        with runtime_state._transaction(connection):
            project = connection.execute(
                'SELECT base_branch, origin FROM projects WHERE repo = ?', (repo,),
            ).fetchone()
            base_branch = project['base_branch']
            resources = used_git_resources(
                role='worker', phase='PLAN_BLOCKED',
                base_branch=base_branch,
                branch=branch, worktree_key=str(_canonical_path(worktree)))
            current_snapshot = effects.capture_git_snapshot(worktree)
            reconciled_adopted = reconcile_external_base(
                effects.git, repo_path, str(base_branch), adopted_snapshot,
                current_snapshot, resources, registered_origin=str(project['origin'] or ''),
            )
            before_commit, at_commit = scoped_metadata(
                reconciled_adopted, current_snapshot, resources,
                default_worktree=str(_canonical_path(repo_path)),
                base_branch=str(base_branch),
            )
            if (before_commit != at_commit
                    or adapter.observe_remote(request) != observed):
                return retry('원격 채택 확정 전 선택 후보·사용 자원 또는 원격 lease가 변경됐습니다.')
            selected = connection.execute(
                'SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?', (repo, blocked['plan_id'])).fetchone()
            if selected is None or any(selected[field] != blocked[field] for field in (
                    *blocked_plan_reconciliation.IDENTITY_FIELDS, 'status', 'blocked_reason',
                    'agent_snapshot', 'agent_result_snapshot', 'publication_remote_oid',
                    'pr_head_oid', 'pr_conflict_recovery', 'pr_conflict_head_oid')):
                return retry('원격 채택 확정 전 계획 소유권 또는 차단 원인이 변경됐습니다.')
            repository.record_remote_inspection(
                request, observed, input_identity,
                "원격 후속 commit 채택 완료; base 반영 뒤 원래 차단 원인을 검사합니다.",
                commit=False,
            )
            values = dict(publication_remote_oid=observed, validated_base_sha=None,
                          worker_input_token=None, archive_token=None, verify_token=None,
                          review_token=None, publication_token=None)
            if selected['pr_url']:
                values['pr_head_oid'] = observed
                if int(selected['pr_conflict_recovery']):
                    values['pr_conflict_head_oid'] = observed
            runtime = runtime_state.RuntimeStateRepository(connection)
            projected = runtime.projected_row(repo)
            if projected is not None and projected['plan_id'] == blocked['plan_id']:
                runtime.transition(
                    repo, str(blocked['plan_id']), plan_values=values, project_values=values,
                    expected_status=selected['status'], enqueue=False)
            else:
                runtime.update_plan(
                    repo, str(blocked['plan_id']), plan_values=values,
                    expected_status=selected['status'], enqueue=False)
            checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
            current = checkpoints.get(repo, str(blocked['plan_id']))
            if current.operation is not None:
                raise RuntimeError("원격 후속 commit 채택 중 다른 Git intent가 생겼습니다.")
            checkpoints.record(
                repo, str(blocked['plan_id']), expected_generation=current.generation,
                operation=None, expected_head=adopted_head,
                target_base_oid=current.target_base_oid, observed_remote_oid=observed,
                snapshot={**(current.snapshot or {}), 'last_adopted_remote_oid': observed,
                          'accepted_work_branch': observed,
                          'remote_adoption_intent': None,
                          'remote_adoption_scope': {
                              'head': adopted_head, 'remote': observed, 'branch': branch, 'worktree': str(worktree),
                              'agent_snapshot': blocked['agent_snapshot'],
                              'agent_result_snapshot': blocked['agent_result_snapshot'],
                              'snapshot': scope_snapshot.serialize(),
                          }},
            )
        return "adopted"
    failure_evidence = "\n".join(
        str(value) for value in (
            request.evidence,
            blocked["verify_result"]
            if blocked["blocked_reason"] == BlockReasonCode.VERIFY_RETRY_EXHAUSTED.value
            else blocked["review_result"],
        ) if value
    )
    corrected_paths = execution_revision.remote_failure_correction_paths(
        str(blocked["blocked_reason"]), changed_paths, failure_evidence,
    )
    if corrected_paths:
        try:
            resolved, assessment, new_failure_stage = effects._evaluate_remote_checkpoint_cause(
                connection, repo_path, state_dir, repo, blocked, config_path,
                verify_timeout_seconds,
            )
        except VerifyTerminationUncertain as error:
            with connection:
                scoped_blocking.record_project_block(
                    connection, repo, BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                    str(error), request.request_identity,
                    plan_id=request.plan_id, previous_phase=None,
                )
                notifications.enqueue_current_project_events(connection, repo)
            return "project-blocked"
        except (OSError, RuntimeError, ValueError) as error:
            return retry(f"원격 checkpoint 원인 재평가 미완료: {error}")
        if new_failure_stage == "specification":
            try:
                effects._reclassify_remote_specification_deadlock(
                    connection, repo_path, repo, blocked, repository, request,
                    observed, worktree, assessment,
                )
            except (OSError, RuntimeError, ValueError) as error:
                return retry(f"원격 checkpoint 명세 교착 기록 미완료: {error}")
            return "blocked"
        if new_failure_stage == "protocol_exhausted":
            try:
                effects._reclassify_remote_checkpoint_block(
                    connection, repo_path, repo, blocked, repository, request,
                    observed, worktree,
                    reason=BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
                    evidence=assessment, review_fingerprint_source=None,
                    user_targets=(
                        str(blocked["path"]),
                        *(str(source) for source in json.loads(str(blocked["sources"]))),
                    ),
                )
            except (OSError, RuntimeError, ValueError) as error:
                return retry(f"원격 checkpoint Reviewer 프로토콜 차단 기록 미완료: {error}")
            return "blocked"
        if new_failure_stage == "revision_drift":
            return retry(assessment)
        if not resolved:
            detail = "원격 checkpoint 원인 유지:\n" + assessment
            with connection:
                connection.execute(
                    "UPDATE execplan_blocks SET evidence = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE repo = ? AND plan_id = ?",
                    (detail, repo, request.plan_id),
                )
                connection.execute(
                    "UPDATE exec_plans SET last_error = ? WHERE repo = ? AND plan_id = ?",
                    (detail, repo, request.plan_id),
                )
                repository.record_remote_inspection(
                    request, observed, input_identity, detail, commit=False,
                )
                notifications.enqueue_current_project_events(connection, repo)
            return "blocked"
        effects._resume_remote_corrected_execplan(
            connection, repo, blocked, repository, request, observed,
            input_identity, "원격 checkpoint 진행 확인 후 Worker부터 재검증: " + assessment,
            new_failure_stage=new_failure_stage, assessment=assessment,
        )
        return "resumed"
    if blocked["blocked_reason"] == BlockReasonCode.PLAN_INPUT_INVALID.value and (
        blocked["blocked_fingerprint"]
        and effects._revision_execplan_input_fingerprint(repo_path, observed, blocked)
        != str(blocked["blocked_fingerprint"])
    ):
        effects._resume_remote_corrected_execplan(
            connection, repo, blocked, repository, request, observed,
            input_identity, "변경된 ExecPlan 입력을 Worker부터 재검증",
        )
        return "resumed"
    reason = str(blocked["last_error"] or "저장된 차단 원인의 해제 조건이 아직 충족되지 않았습니다.")
    repository.record_remote_inspection(request, observed, input_identity, reason)
    return "adopted"


def _assess_user_decision_cause(effects, connection, repo_path, state_dir, repo, blocked, config):
    import difflib
    import hashlib
    import re
    from loop_manager_internal.storage import execplan_draft_state
    if str(blocked['plan_id']) in execplan_draft_state.pending_ids(connection, repo):
        return False, '저장된 draft 사용자 판단은 기존 출처·답변 검증 경로에서 확인합니다.', None
    project = runtime_state.project_row(connection, repo)
    revision = effects.git(repo_path, 'rev-parse', f"refs/remotes/origin/{project['base_branch']}").stdout.strip()
    prior_revision = blocked['active_sha'] or blocked['plan_sha']
    paths = sorted(set(json.loads(blocked['sources'])) | {blocked['path'], 'docs/PRODUCT.md', 'docs/ARCHITECTURE.md'})
    current, changes = {}, {}
    requirement_documents = {}
    def source(revision, path):
        from loop_manager_internal.catalog.manager_sources import SOURCE, source_content
        if path == SOURCE:
            return source_content(path)
        result = effects.git(repo_path, 'show', f'{revision}:{path}', check=False)
        if result.returncode == 0:
            return result.stdout
        from loop_manager_internal.catalog import requirement_sources
        from loop_manager_internal.git.command_adapter import revision_requirement_documents
        if requirement_sources.source_requirement_id(path) is not None:
            if revision not in requirement_documents:
                requirement_documents[revision] = revision_requirement_documents(
                    lambda repo, *args: effects.git(repo, *args).stdout.encode('utf-8'), repo_path, revision)
            resolved = requirement_sources.resolve_requirement_source(path, requirement_documents[revision], allow_archive=True)
            if resolved.status is requirement_sources.ResolutionStatus.RESOLVED:
                return resolved.contents
        raise RuntimeError('차단 재평가의 이전·현재 source를 읽을 수 없습니다: ' + path)
    def meaning(text):
        text = re.sub(r'\A---\r?\n.*?\r?\n---\r?\n', '', text, count=1, flags=re.S)
        return ' '.join(re.sub(r'\[([^]]*)\]\([^)]*\)', r'\1', text).split())
    for path in paths:
        from loop_manager_internal.catalog.manager_sources import SOURCE
        if path != SOURCE and not _safe_path(path):
            raise RuntimeError('차단 재평가 source 경로가 유효하지 않습니다.')
        old, latest = source(prior_revision, path), source(revision, path)
        current[path] = latest
        if meaning(old) != meaning(latest):
            changes[path] = ''.join(difflib.unified_diff(old.splitlines(True), latest.splitlines(True)))
    if not changes:
        return False, '기존 사용자 질문을 다시 판단할 명세·계획 본문 변경이 없습니다.', None
    block = scoped_blocking.plan_block(connection, repo, str(blocked['plan_id']))
    if block is None or block['reason_code'] != BlockReasonCode.USER_DECISION_REQUIRED.value:
        raise RuntimeError('사용자 질문의 현재 차단 원인이 변경됐습니다.')
    question = str(block['evidence'])
    identity = hashlib.sha256(json.dumps([question, current], sort_keys=True).encode()).hexdigest()
    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    checkpoint = checkpoints.get(repo, str(blocked['plan_id']))
    saved = (checkpoint.snapshot or {}).get('user_decision_assessment', {})
    if saved.get('identity') == identity:
        return saved['resolved'], saved['detail'], None
    prompt = ("기존 사용자 판단 차단의 원인만 읽기 전용으로 재평가하라. "
        "문서 수정·질문 삭제·Planner 판단을 사용자 승인으로 간주하지 마라. "
        "최신 Requirement와 ExecPlan에서 기존 질문이 더는 필요 없거나 이미 명시적으로 결정된 경우만 resolved=true다. "
        "실제 선택이 남거나 두 문서가 충돌하면 false로 유지하라. 새 사용자 승인·답변을 추론하지 마라. "
        "링크·표현 변화나 무관한 변경은 해소 근거가 아니다. "
        "필요하면 Git의 지정 revision에서 source 원문을 읽어라. 파일을 수정하지 마라. "
        "한 줄 USER_DECISION_ASSESSMENT: JSON 객체를 반환하라. 필드는 resolved(bool), detail(이유), "
        "evidence(근거별 source·quote 배열)다. true에는 변경된 source의 현재 원문 인용과 "
        "ExecPlan 이외의 상위 명세가 질문 해소를 뒷받침하는 인용이 모두 필요하다.\n"
        + json.dumps(dict(question=question, revision=revision, sources=paths,
                         execplan=current[blocked['path']], changes=changes,
                         manager_inputs={path: current[path] for path in paths if path.startswith('manager:')}), ensure_ascii=False))
    worktree = Path(blocked['worktree'])
    baseline = effects.capture_git_snapshot(worktree)
    code, output = effects.run_agent(str(project['provider']), worktree, state_dir, config, 'reviewer', prompt,
        read_only=True, connection=connection,
        invocation=InvocationContext(repo, str(blocked['plan_id']), purpose='review', target=str(blocked['plan_id'])))
    effects.validate_agent_scope('reviewer', worktree, baseline, effects.capture_git_snapshot(worktree),
                                 external_worktree=repo_path)
    lines = [line.removeprefix('USER_DECISION_ASSESSMENT:').strip() for line in str(output).splitlines()
             if line.startswith('USER_DECISION_ASSESSMENT:')]
    if code or len(lines) != 1:
        raise RuntimeError('사용자 질문 재평가 결과가 미완료이거나 모호합니다.')
    result = json.loads(lines[0])
    if (not isinstance(result, dict) or set(result) != {'resolved', 'detail', 'evidence'}
            or type(result.get('resolved')) is not bool
            or not isinstance(result.get('detail'), str) or not result['detail'].strip()
            or not isinstance(result.get('evidence'), list)):
        raise RuntimeError('사용자 질문 재평가 결과 형식이 유효하지 않습니다.')
    if result['resolved'] and (not result['evidence'] or any(
            not isinstance(item, dict) or item.get('source') not in current
            or not isinstance(item.get('quote'), str) or not item['quote'].strip()
            or item['quote'] not in current[item['source']] for item in result['evidence'])
            or not any(item['source'] in changes for item in result['evidence'])
            or not any(item['source'] != blocked['path'] for item in result['evidence'])):
        raise RuntimeError('사용자 질문 해소의 현재 명세 근거를 확인할 수 없습니다.')
    if effects.git(repo_path, 'rev-parse', f"refs/remotes/origin/{project['base_branch']}").stdout.strip() != revision:
        raise RuntimeError('사용자 질문 재평가 중 원격 입력이 변경됐습니다.')
    with runtime_state._transaction(connection):
        latest_plan = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?",
                                         (repo, blocked['plan_id'])).fetchone()
        if (latest_plan is None or any(latest_plan[key] != blocked[key]
                for key in ('status', 'path', 'sources', 'content_hash', 'current_revision_sha'))
                or checkpoints.get(repo, str(blocked['plan_id'])) != checkpoint
                or dict(scoped_blocking.plan_block(connection, repo, str(blocked['plan_id'])) or {}) != dict(block or {})):
            raise RuntimeError('사용자 질문 재평가 중 차단 원인 또는 checkpoint가 변경됐습니다.')
        assessment = dict(identity=identity, revision=revision, question=question, **result)
        checkpoints.record(repo, str(blocked['plan_id']), expected_generation=checkpoint.generation,
            operation=checkpoint.operation, expected_head=checkpoint.expected_head,
            target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
            snapshot={**(checkpoint.snapshot or {}), 'user_decision_assessment': assessment,
                      'user_decision_assessment_history': [
                          *(checkpoint.snapshot or {}).get('user_decision_assessment_history', []), assessment]})
    return result['resolved'], result['detail'], None


def _evaluate_remote_checkpoint_cause(effects: EvaluateRemoteCheckpointCauseEffects,
    connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, blocked: sqlite3.Row, config_path: Path | None,
    timeout_seconds: int | None,
) -> tuple[bool, str, str | None]:
    """Inspect the candidate while retaining the block and all execution evidence."""
    worktree = Path(str(blocked["worktree"]))
    config = config_path or effects.ROOT / "config/config.ini"
    timeout = (timeout_seconds if timeout_seconds is not None
               else effects.load_managed_config(config).verify_timeout_seconds)
    project = connection.execute(
        "SELECT provider, base_branch FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if blocked['blocked_reason'] == BlockReasonCode.USER_DECISION_REQUIRED.value:
        return _assess_user_decision_cause(effects, connection, repo_path, state_dir, repo, blocked, config)
    revision = effects._remote_checkpoint_review_revision(
        repo_path, worktree, str(project["base_branch"]), state_dir,
    )
    if revision is None:
        return False, "원격 checkpoint 후보가 최신 base를 포함하지 않습니다.", "stale_base"
    before = effects.capture_git_snapshot(worktree)
    passed, output = effects.verify(worktree, timeout)
    effects.validate_agent_scope(
        "reviewer", worktree, before, effects.capture_git_snapshot(worktree),
        external_worktree=repo_path,
    )
    validation_feedback_state.ValidationFeedbackRepository(connection).record_result(
        repo, str(blocked["plan_id"]), revision[0], "verify",
        "success" if passed else "failure",
        str(getattr(output, "fingerprint_source", output)),
    )
    if not passed:
        return effects._remote_checkpoint_has_new_failure(
            connection, repo, blocked, "verify", output, worktree,
        ), str(output), "verify"
    if blocked["blocked_reason"] == BlockReasonCode.VERIFY_RETRY_EXHAUSTED.value:
        return True, str(output), None
    saved_candidate = str(blocked["publication_remote_oid"] or blocked["pr_head_oid"] or "")
    if not saved_candidate or saved_candidate != revision[1]:
        return (
            True,
            "원격 checkpoint Worker 결과가 현재 후보 HEAD와 연결되지 않아 Worker부터 재검증합니다.",
            "worker",
        )
    try:
        worker_publication = effects.parse_worker_result(
            str(blocked["worker_result"] or ""),
            {"id": str(blocked["plan_id"]), "title": str(blocked["title"])},
        )
    except effects.WorkerResultError as error:
        return False, f"원격 checkpoint Worker 결과 검증 실패: {error}", "worker"
    targeted_test_evidence = (
        'Worker 보고:\n'
        + reviewer_targeted_test_evidence(worker_publication.test_evidence)
    )
    verify_output = str(getattr(output, 'fingerprint_source', output))
    verify_evidence = (
        f"Manager 확인: 전체 verify PASS (checkpoint 복구 중 실제 실행); "
        f"base={revision[0]}; candidate HEAD={revision[1]}; "
        f"{'기록된 출력 없음' if not verify_output else '비공개로 저장된 출력 있음'}."
    )
    previous_review = blocked["review_result"]
    protocol_error = None
    for attempt in range(1, effects.MAX_REVIEWER_PROTOCOL_ATTEMPTS + 1):
        if effects._remote_checkpoint_review_revision(
            repo_path, worktree, str(project["base_branch"]), state_dir,
        ) != revision:
            return False, "원격 checkpoint verify 뒤 base 또는 HEAD가 변경됐습니다.", "revision_drift"
        review_before = effects.capture_git_snapshot(worktree)
        code, review = effects.run_agent(
            str(project["provider"]), worktree, state_dir, config, "reviewer",
            reviewer_prompt(
                str(project["base_branch"]), targeted_test_evidence, previous_review,
                verify_evidence=verify_evidence,
                session_recovery=True,
                protocol_error=protocol_error,
            ),
            read_only=True,
            connection=connection,
            invocation=InvocationContext(repo, str(blocked["plan_id"]),
                                         purpose="review", target=str(blocked["plan_id"])),
        )
        effects.validate_agent_scope(
            "reviewer", worktree, review_before, effects.capture_git_snapshot(worktree),
            external_worktree=repo_path,
        )
        if code:
            raise RuntimeError(f"원격 checkpoint Reviewer 실행 미완료: {review}")
        try:
            verdict, details = effects.parse_reviewer_result(review)
            if verdict == "SPEC_BLOCKED":
                assert isinstance(details, reviewer_specification.SpecificationDeadlock)
                try:
                    reviewer_specification.validate_locations(
                        details, str(blocked["path"]),
                        tuple(str(source) for source in json.loads(str(blocked["sources"]))),
                    )
                except ValueError as error:
                    raise effects.ReviewerProtocolError(str(error)) from error
        except effects.ReviewerProtocolError as error:
            protocol_error = str(error)
            previous_review = review
            if attempt == effects.MAX_REVIEWER_PROTOCOL_ATTEMPTS:
                evidence = (
                    f"Reviewer 결과 프로토콜 오류: {protocol_error} "
                    f"(프로토콜 교정 {effects.MAX_REVIEWER_PROTOCOL_ATTEMPTS}회 실패)\n"
                    f"직전 결과:\n{review}"
                )
                return False, evidence, "protocol_exhausted"
            continue
        if verdict == "SPEC_BLOCKED":
            return False, review, "specification"
        validation_feedback_state.ValidationFeedbackRepository(connection).observe(
            repo, str(blocked["plan_id"]), revision[0], "review", verdict == "PASS", review,
            normalize_feedback("review", review) if verdict == "FAIL" else (),
        )
        return verdict == "PASS" or effects._remote_checkpoint_has_new_failure(
            connection, repo, blocked, "review", review, worktree,
        ), review, "review" if verdict != "PASS" else None
    raise AssertionError("Reviewer 프로토콜 교정 횟수가 소진되지 않았습니다.")
