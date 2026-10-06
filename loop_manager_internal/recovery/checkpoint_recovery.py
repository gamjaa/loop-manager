"""Checkpoint successor adoption and cause-specific candidate revalidation."""
from __future__ import annotations
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
from loop_manager_internal.execution.validation_feedback import normalize_feedback
from loop_manager_internal.execution.verify_process import VerifyTerminationUncertain
from loop_manager_internal.git import managed_rebase_adoption, managed_rebase_validation
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
    accepted = str(row["accepted_remote_oid"] or row["published_oid"] or row["remote_oid"])
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
    if (
        project_recovery_context is None
        and not row["pending_remote_oid"]
        and row["inspected_remote_oid"] == observed
        and row["inspected_input"] == input_identity
        and row["inspection_reason"]
        and not row["inspection_retryable"]
        and blocked["blocked_reason"]
        != BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT.value
    ):
        return "blocked"
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
        return refuse("게시된 원격 checkpoint branch가 삭제됐습니다.")
    if not row["published_oid"]:
        return refuse("게시 OID가 증명되지 않은 legacy checkpoint의 원격 변경은 채택할 수 없습니다.")
    if row["pending_remote_oid"] and row["pending_remote_oid"] != observed and not integrating:
        return refuse("미완료 원격 checkpoint 채택 중 branch OID가 다시 변경됐습니다.")
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
        fetched = effects.git(
            repo_path, "fetch", "--no-tags", *refmap_options, "origin",
            f"refs/heads/{branch}", check=False,
        )
    except (OSError, RuntimeError) as error:
        return retry("원격 checkpoint branch fetch 미완료: " + str(error))
    if fetched.returncode:
        return retry("원격 checkpoint branch fetch 실패: " + fetched.stdout[-2000:])
    fetched_result = effects.git(
        repo_path, "rev-parse", "FETCH_HEAD^{commit}", check=False,
    )
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
    local_history = effects.git(
        repo_path, "merge-base", "--is-ancestor", accepted, history_head,
        check=False,
    )
    if local_history.returncode not in {0, 1}:
        return retry("로컬 checkpoint ancestry를 확인할 수 없습니다.")
    if local_history.returncode == 1:
        acceptance = (current.snapshot or {}).get('current_rebase_acceptance') or {}
        if (current.operation is not None or current.expected_head != original_head
                or not managed_rebase_validation.valid_acceptance(acceptance)
                or any(acceptance[key] != value for key, value in (
                    ('repo', repo), ('plan_id', str(blocked['plan_id'])),
                    ('branch', branch), ('worktree', str(_canonical_path(worktree))),
                    ('head', original_head)))
                or acceptance['remote']['oid'] != accepted):
            return refuse('로컬 branch 계보와 현재 Manager rebase 반영 근거를 증명할 수 없습니다.')
        try:
            _, decision = effects._current_rebase_service(
                connection, repo_path, state_dir, repo, str(blocked['plan_id']),
                worktree, branch, remote_oid=observed).require_owned(checkpoint=current)
            if decision.status.value != 'complete':
                return retry('이전 Manager rebase의 현재 완료를 먼저 확인해야 합니다.')
        except (OSError, RuntimeError, ValueError) as error:
            return refuse(str(error))
    ancestors = [(accepted, '마지막 확인 head'), (str(row['published_oid']), '게시 checkpoint')]
    if integration and integrating:
        ancestors.append((str(integration['remote']), '진행 중인 원격 입력'))
    if integrating:
        saved = current.snapshot or {}
        base_restart = saved.get('base_rebase_restart') or {}
        for ancestor, label in (
                ((saved.get('remote_adoption_intent') or {}).get('head'), '진행 중인 채택 입력'),
                (((integration or {}).get('restart') or {}).get('remote'), '작업 branch 충돌 재시작 입력'),
                (base_restart.get('remote') if base_restart.get('phase') != 'complete' else None,
                 'base 충돌 재시작 입력')):
            if ancestor:
                ancestors.append((ancestor, label))
    for ancestor, label in ancestors:
        relation = effects.git(
            repo_path, "merge-base", "--is-ancestor", ancestor, observed,
            check=False,
        )
        if relation.returncode not in {0, 1}:
            return retry(f"원격 branch의 {label} ancestry를 확인할 수 없습니다.")
        if relation.returncode == 1:
            return refuse(f"원격 branch가 {label}의 후속이 아닙니다 (삭제/재작성/분기 또는 검증 실패).")
    try:
        changed_paths = effects._nul_git_paths(
            repo_path, "diff", "--no-renames", "--name-only", "-z",
            accepted, observed, "--",
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
            resources = used_git_resources(
                role='worker', phase='PLAN_BLOCKED',
                base_branch=connection.execute('SELECT base_branch FROM projects WHERE repo = ?', (repo,)).fetchone()[0],
                branch=branch, worktree_key=str(_canonical_path(worktree)))
            current_snapshot = effects.capture_git_snapshot(worktree)
            before_commit, at_commit = scoped_metadata(adopted_snapshot, current_snapshot, resources)
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
    try:
        worker_publication = effects.parse_worker_result(
            str(blocked["worker_result"] or ""),
            {"id": str(blocked["plan_id"]), "title": str(blocked["title"])},
        )
    except effects.WorkerResultError as error:
        return False, f"원격 checkpoint Worker 결과 검증 실패: {error}", "worker"
    targeted_test_reason = effects.reviewer_targeted_test_reason(
        worker_publication.test_evidence,
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
                str(project["base_branch"]), targeted_test_reason, previous_review,
                session_recovery=True,
                protocol_error=protocol_error,
            ),
            read_only=True,
            connection=connection,
            invocation=InvocationContext(repo, str(blocked["plan_id"])),
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
