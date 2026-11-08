"""recovery waiting pr service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.application.contracts import PRIORITY_ORDER
from loop_manager_internal.application.contracts import PullRequestSyncResult
from loop_manager_internal.application.contracts import WaitingPullRequestObservation
from loop_manager_internal.notifications import notifications
from loop_manager_internal.git.managed_rebase_adoption import revalidation_values
from loop_manager_internal.recovery import blocked_plan_reconciliation
from loop_manager_internal.recovery import pull_request_recovery
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import PR_CLOSED_BLOCK_REASON
from loop_manager_internal.recovery.recovery_runtime import RecoveryReason, RecoveryFailure
from loop_manager_internal.runtime.contracts import PublicationSafetyError
from loop_manager_internal.runtime.contracts import REBASE_IN_PROGRESS_ERROR
from loop_manager_internal.application.contracts import PROJECT_STEP_TRANSIENT_PAUSE
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
from pathlib import Path
import json
import sqlite3
import sys


@dataclass(frozen=True)
class ProjectPrMergeConfirmedEffects:
    """Named effects for _project_pr_merge_confirmed; no entry-point/module access."""
    _plan_id_from_path: Callable[..., object]


def _project_pr_merge_confirmed(
    effects: ProjectPrMergeConfirmedEffects, connection: sqlite3.Connection,
    repo: str,
    project: sqlite3.Row,
) -> bool:
    """Resolve a stale projection against this plan's durable execution row."""

    path = str(project["plan_path"] or "").replace(
        "docs/plans/archive/", "docs/plans/", 1,
    )
    plan_id = effects._plan_id_from_path(path)
    if plan_id is None:
        return False
    plan = connection.execute(
        "SELECT status, branch FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()
    return bool(
        plan is not None
        and plan["status"] == "MERGED"
        and project["branch"] == (plan["branch"] or f"loop-manager/{plan_id}")
    )


@dataclass(frozen=True)
class ObserveWaitingPullRequestsEffects:
    """Named effects for observe_waiting_pull_requests; no entry-point/module access."""
    _block_closed_pull_request: Callable[..., object]
    _recorded_base_fetch: Callable[..., object]
    _resume_reopened_pull_request: Callable[..., object]
    classify_pull_request_mergeability: Callable[..., object]
    complete_merged_execplan_worktree: Callable[..., object]
    operation_log: Callable[..., object]
    operation_print: Callable[..., object]
    pr_for_branch: Callable[..., object]
    base_head: Callable[..., object] | None = None
    resolve_profile: Callable[..., object] | None = None
    git: Callable[..., object] | None = None


def observe_waiting_pull_requests(
    effects: ObserveWaitingPullRequestsEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    require_base_identity: bool = False,
    read_only: bool = False,
    completion_only: bool = False,
    config_path: Path | None = None,
) -> WaitingPullRequestObservation:
    """Commit every merge-wait observation before source or conflict work."""

    from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
    manual_store=ManualWorkRepository(connection)
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND status <> 'MERGED' ORDER BY plan_id",
        (repo,),
    ).fetchall()
    registered_base = ""
    if rows:
        project = connection.execute(
            "SELECT base_branch,provider,origin FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if project is None or not project["base_branch"]:
            raise RuntimeError("Merge 대기 PR의 등록 base branch를 확인할 수 없습니다.")
        registered_base = str(project["base_branch"])
    conflicts: list[tuple[sqlite3.Row, Mapping[str, object]]] = []
    merged = False
    merged_pr_urls: list[str] = []
    seen_groups=set()
    for stored in rows:
        current = connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                     (repo,stored['plan_id'])).fetchone()
        if current and current['status'] == 'MERGED':
            continue  # Shared cleanup already completed atomically.
        stored=current or stored
        group=manual_store.for_plan(repo,stored['plan_id'])
        if group:
            manual_store.workspace_owner(repo,stored['plan_id'])
            if group['group_id'] in seen_groups:
                continue
            seen_groups.add(group['group_id'])
        waiting = (stored['status'] == 'PR_OPEN' or (
            stored['status'] == 'PLAN_BLOCKED' and stored['blocked_reason'] in {
                PR_CLOSED_BLOCK_REASON, BlockReasonCode.REMOTE_BRANCH_DELETED.value,
                BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value}))
        branch = str(stored["branch"] or f"loop-manager/{stored['plan_id']}")
        checkpoint_store = ExecutionCheckpointRepository(connection)
        checkpoint = checkpoint_store.get(repo, str(stored['plan_id']))
        evidence = (checkpoint.snapshot or {}).get('external_completion')
        observing = True
        try:
            if not stored['pr_url']:
                # Catalog registration supplies the ID; full PR history supplies the
                # repository/base/head link, including an already deleted branch.
                if not project['origin']:
                    continue
                # An empty all-state lookup has no PR identity to adopt. Only
                # a discovered history needs the registered repository check.
                pull_request = effects.pr_for_branch(repo_path, branch)
                if pull_request is not None:
                    from loop_manager_internal.git.git_adapter import github_repository
                    pull_request = effects.pr_for_branch(
                        repo_path, branch, history=True,
                        expected_repository=github_repository(str(project['origin'])),
                        expected_base=registered_base,
                    )
            else:
                pull_request = effects.pr_for_branch(repo_path, branch, str(stored['pr_url']))
            if pull_request is None:
                if not stored['pr_url']:
                    if evidence and evidence.get('kind') == 'github-pr':
                        raise PublicationSafetyError('이전에 관측한 Merge의 현재 PR 연결을 확인할 수 없습니다.')
                    if not read_only and (checkpoint.snapshot or {}).get('external_completion'):
                        with connection:
                            checkpoint_store.record_completion_observation(
                                repo, str(stored['plan_id']), expected=checkpoint,
                                observation={**checkpoint.snapshot['external_completion'],
                                             'db_pending': False, 'resolution': 'no-pr'})
                    continue
                raise RuntimeError(
                    f"저장된 Merge 대기 PR의 GitHub 상태를 확인할 수 없습니다: {branch}"
                )
            observed_url = str(pull_request.get("url") or "")
            stored_url = str(stored["pr_url"] or "")
            if stored_url and observed_url and observed_url != stored_url:
                raise PublicationSafetyError(
                    f"저장된 PR URL과 GitHub PR identity가 다릅니다: {branch}"
                )
            head_name = pull_request.get("headRefName")
            if head_name and str(head_name) != branch:
                raise PublicationSafetyError(
                    f"저장된 PR head branch와 GitHub PR identity가 다릅니다: {branch}"
                )
            observed_base = pull_request.get("baseRefName")
            if observed_base is not None and str(observed_base) != registered_base:
                raise PublicationSafetyError(
                    f"GitHub PR base branch가 등록 base와 다릅니다: {branch}"
                )
            if require_base_identity and not observed_base:
                raise PublicationSafetyError(
                    f"self PR base branch identity를 확인할 수 없습니다: {branch}"
                )
            url = str(pull_request.get("url") or stored["pr_url"] or "")
            if not url:
                raise RuntimeError(f"저장된 Merge 대기 PR URL을 확인할 수 없습니다: {branch}")
            head_value = pull_request.get("headRefOid")
            head_oid = str(head_value) if head_value else stored["pr_head_oid"]
            state = str(pull_request.get("state") or "OPEN").upper()
            if read_only:
                continue
            if state == "MERGED" or pull_request.get("mergedAt"):
                if not observed_url or not head_oid or (not stored_url and (not head_name or not observed_base)):
                    raise PublicationSafetyError('Merge PR의 URL/head/base/revision identity가 불완전합니다.')
                evidence = {
                    'version': 1, 'repo': repo, 'plan_id': stored['plan_id'],
                    'branch': branch, 'pr_url': url, 'head': head_oid,
                    'base': registered_base, 'repository': project['origin'],
                    'kind': 'github-pr',
                    'revision': (pull_request.get('mergeCommit') or {}).get('oid') or pull_request.get('mergedAt') or head_oid,
                    'db_pending': True,
                }
                with connection:
                    checkpoint = checkpoint_store.record_completion_observation(
                        repo, str(stored['plan_id']), expected=checkpoint, observation=evidence)
                assessment = effects.complete_merged_execplan_worktree(
                    connection,
                    repo_path,
                    repo,
                    state_dir,
                    stored,
                    url,
                    str(head_oid) if head_oid else None,
                    observed_registration=(project['origin'], project['base_branch']),
                    observed_checkpoint_generation=checkpoint.generation,
                    **({'merge_commit_oid': pull_request['mergeCommit']['oid']}
                       if isinstance(pull_request.get('mergeCommit'), dict) and pull_request['mergeCommit'].get('oid') else {}),
                )
                merged = True
                merged_pr_urls.append(url)
                if assessment.status in {"safe", "missing"}:
                    effects.operation_print(f"Merge 확인 및 worktree 정리 완료: {url}")
                else:
                    effects.operation_print(
                        f"Merge 확인 완료; worktree 보존: {stored['worktree']} "
                        f"({assessment.reason or assessment.status})",
                        file=sys.stderr,
                    )
                continue
            if evidence and evidence.get('kind') == 'github-pr':
                raise PublicationSafetyError('이전에 관측한 Merge와 현재 PR 상태가 충돌합니다.')
            if (checkpoint.snapshot or {}).get('external_completion'):
                with connection:
                    checkpoint = checkpoint_store.record_completion_observation(
                        repo, str(stored['plan_id']), expected=checkpoint,
                        observation={**checkpoint.snapshot['external_completion'], 'db_pending': False,
                                     'resolution': state})
            if completion_only or scoped_blocking.project_block(connection, repo) is not None or not waiting:
                continue
            observing = False
            if group and group['state']=='AUTOMATIC' and state == 'OPEN':
                continue
            if state == "CLOSED":
                if stored["blocked_reason"] != PR_CLOSED_BLOCK_REASON:
                    effects._block_closed_pull_request(connection, repo, stored, url, head_oid)
                continue
            if stored['blocked_reason'] == BlockReasonCode.REMOTE_BRANCH_DELETED.value:
                continue
            if (stored['status'] == 'PLAN_BLOCKED'
                    and stored['blocked_reason'] == BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value):
                # Remote terminal transitions remain observable. OPEN is not proof
                # of recovery; the cause-specific recheck owns admission/release.
                from loop_manager_internal.notifications.policy import block_recovery
                recovery = block_recovery(connection, repo, str(stored['plan_id']), 'plan')
                if (recovery is not None and recovery['reason_code'] == RecoveryReason.MANAGED_CONFLICT_UNRECOVERABLE.value
                        and recovery['pending_action'] == 'final_block'):
                    base_oid = effects.base_head(repo_path, registered_base) if effects.base_head else None
                    if ((head_oid and head_oid != stored['pr_head_oid'])
                            or (base_oid and base_oid != stored['pr_conflict_base_oid'])
                            or effects.classify_pull_request_mergeability(pull_request) == 'mergeable'):
                        with connection:
                            recovery_state.RecoveryRepository(connection).defer_conflict_assessment(repo, str(stored['plan_id']))
                            notifications.enqueue_current_project_events(connection, repo)
                continue
            from loop_manager_internal.recovery.work_branch_integration import published_branch
            if effects.git is not None and published_branch(connection, repo, stored):
                from loop_manager_internal.git.manager_git_journal import live_remote_branch_oid
                from loop_manager_internal.recovery.work_branch_integration import require_remote_present
                remote = live_remote_branch_oid(effects.git, repo_path, branch)
                if remote != live_remote_branch_oid(effects.git, repo_path, branch):
                    raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '삭제 확인 중 원격 branch가 변경됐습니다.')
                try:
                    require_remote_present(connection, repo, stored, remote, pull_request)
                except RecoveryFailure as error:
                    if error.reason is not RecoveryReason.REMOTE_BRANCH_DELETED:
                        raise
                    with connection:
                        scoped_blocking.record_plan_block(connection, repo, str(stored['plan_id']),
                            BlockReasonCode.REMOTE_BRANCH_DELETED, str(error), None)
                    continue
            if state != "OPEN":
                raise RuntimeError(f"지원하지 않는 GitHub PR 상태입니다: {state}")
            if group and effects.base_head and stored['status']=='PR_OPEN':
                from loop_manager_internal.recovery import manual_work_recovery
                if manual_work_recovery.revalidate_waiting(connection,repo,stored,pull_request,
                        effects.base_head(repo_path,registered_base),config_path=config_path,
                        provider=project['provider'],resolve_profile=effects.resolve_profile):
                    continue
            if (
                stored["status"] == "PLAN_BLOCKED"
                and stored["blocked_reason"] == PR_CLOSED_BLOCK_REASON
            ):
                expected_head = str(stored["pr_head_oid"] or "")
                if expected_head and head_oid and str(head_oid) != expected_head:
                    if not runtime_state.RuntimeStateRepository(
                        connection
                    ).recovery_slot_available(repo):
                        # Keep the closed-PR block until its head can be checked
                        # without replacing an unrelated planning projection.
                        continue
                    if effects.git is not None:
                        from loop_manager_internal.git.manager_git_journal import live_remote_branch_oid
                        if live_remote_branch_oid(effects.git, repo_path, branch) != head_oid:
                            raise RecoveryFailure(RecoveryReason.REMOTE_SNAPSHOT_RACE, '재개 PR과 원격 branch의 관측 시점이 다릅니다.')
                effects._resume_reopened_pull_request(
                    connection,
                    repo,
                    stored,
                    url,
                    head_oid,
                    recovery=False,
                )
                stored = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                    (repo, stored["plan_id"]),
                ).fetchone()
                if stored["status"] != "PR_OPEN":
                    # An unvalidated candidate resumes execution when its slot is
                    # available; it never joins the Merge-wait conflict selection.
                    continue
                if group and effects.base_head:
                    from loop_manager_internal.recovery import manual_work_recovery
                    if manual_work_recovery.revalidate_waiting(connection,repo,stored,pull_request,
                            effects.base_head(repo_path,registered_base),config_path=config_path,
                            provider=project['provider'],resolve_profile=effects.resolve_profile):
                        continue
            with connection:
                resumed = notifications.record_work_start(
                    connection, repo, "PR handling", plan_id=str(stored["plan_id"]),
                )
            if resumed:
                effects.operation_log(f"차단 해제 후 PR 처리 재개: {stored['plan_id']}")
            mergeability = effects.classify_pull_request_mergeability(pull_request)
            with connection:
                plan_values: dict[str, object] = {"pr_url": url}
                # Conflict recovery must compare GitHub's head with the stored
                # ownership OID before adopting a changed head.
                if mergeability != "conflicting":
                    plan_values["pr_head_oid"] = head_oid
                runtime_state.RuntimeStateRepository(connection).update_plan(
                    repo,
                    str(stored["plan_id"]),
                    plan_values=plan_values,
                    enqueue=False,
                )
                notifications.enqueue_current_project_events(connection, repo)
            if mergeability == "conflicting":
                conflicts.append((stored, pull_request))
        except (RuntimeError, sqlite3.Error) as error:
            current = connection.execute(
                'SELECT status FROM exec_plans WHERE repo=? AND plan_id=?',
                (repo, stored['plan_id']),
            ).fetchone()
            if current and current['status'] == 'MERGED':
                merged = True
                merged_pr_urls.append(str(stored['pr_url'] or (evidence or {}).get('pr_url') or ''))
                effects.operation_print(f"Merge 확인 완료; 정리 재시도 대기: {stored['plan_id']} ({error})", file=sys.stderr)
                continue
            if read_only or require_base_identity or not observing:
                raise
            pending = evidence or {
                'version': 1, 'repo': repo, 'plan_id': stored['plan_id'],
                'branch': branch, 'pr_url': stored['pr_url'], 'kind': 'unconfirmed',
                'revision': None, 'db_pending': True,
            }
            from loop_manager_internal.execution.validation_feedback import redact_secrets
            pending = {**pending, 'db_pending': True, 'error': redact_secrets(str(error))}
            try:
                with connection:
                    checkpoint_store.record_completion_observation(
                        repo, str(stored['plan_id']), expected=checkpoint, observation=pending)
            except (RuntimeError, sqlite3.Error) as persistence_error:
                effects.operation_print(f"완료 관측 저장 보류: {stored['plan_id']} ({persistence_error})", file=sys.stderr)
                raise error
            effects.operation_print(f"완료 확인/반영 보류: {stored['plan_id']} ({error})", file=sys.stderr)
            continue

    # Each completed transition is committed before a remote fetch can fail.
    # A later run fetches again and resumes source synchronization from SQLite.
    if merged and not completion_only and scoped_blocking.project_block(connection, repo) is None:
        project = connection.execute(
            "SELECT base_branch FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        if project is None or not project["base_branch"]:
            raise RuntimeError("Merge 뒤 fetch할 프로젝트 base branch를 확인할 수 없습니다.")
        base = str(project["base_branch"])
        effects._recorded_base_fetch(repo_path, state_dir, base, owner="project")
    return WaitingPullRequestObservation(
        tuple(conflicts), merged, tuple(merged_pr_urls)
    )


@dataclass(frozen=True)
class SelectWaitingPullRequestConflictEffects:
    """Named effects for select_waiting_pull_request_conflict; no entry-point/module access."""
    active_execplan: Callable[..., object]
    hydrate_execplan_project: Callable[..., object]
    reconcile_existing_pull_request: Callable[..., object]


def select_waiting_pull_request_conflict(
    effects: SelectWaitingPullRequestConflictEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    observation: WaitingPullRequestObservation,
    *,
    config_path: Path | None = None,
) -> PullRequestSyncResult:
    """Start one eligible recovery, passing over durable plan-local waits."""

    if not runtime_state.RuntimeStateRepository(
        connection
    ).recovery_slot_available(repo):
        return PullRequestSyncResult(merged=observation.merged)
    if scoped_blocking.project_block(connection, repo) is not None:
        return PullRequestSyncResult(merged=observation.merged)
    current_rows = {
        str(row["plan_id"]): row
        for row in connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ?", (repo,)
        )
    }
    statuses = {
        plan_id: str(row["status"]) for plan_id, row in current_rows.items()
    }
    held = scoped_blocking.catalog_held_plan_ids(connection, repo)
    eligible: list[tuple[sqlite3.Row, Mapping[str, object]]] = []
    for stored, pull_request in observation.conflicts:
        current = current_rows.get(str(stored["plan_id"]))
        if current is None or current["status"] != "PR_OPEN" or str(current["plan_id"]) in held:
            continue
        if all(
            str(dependency) not in held and statuses.get(str(dependency)) == "MERGED"
            for dependency in json.loads(str(current["depends_on"]))
        ):
            eligible.append((current, pull_request))
    from loop_manager_internal.storage.task_calls import management_available
    for plan, pull_request in sorted(
        eligible,
        key=lambda item: (
            PRIORITY_ORDER[str(item[0]["priority"])],
            int(str(item[0]["plan_id"])[2:]),
        ),
    ):
        if not management_available(connection,repo,str(plan['plan_id'])):
            return PullRequestSyncResult(merged=observation.merged)
        recovery_options = {"config_path": config_path} if config_path is not None else {}
        previous_recovery = recovery_state.RecoveryRepository(connection).row(repo, str(plan['plan_id']))
        with runtime_state.selected_execution(repo, str(plan['plan_id']), include_blocked=True):
            effects.hydrate_execplan_project(connection, repo, plan)
            project = runtime_state.project_row(connection, repo)
            assert project is not None
            result = effects.reconcile_existing_pull_request(
                connection,
                repo_path,
                state_dir,
                repo,
                project,
                pull_request,
                **recovery_options,
            )
        current = connection.execute(
            'SELECT * FROM exec_plans WHERE repo=? AND plan_id=?', (repo, plan['plan_id']),
        ).fetchone()
        recorded = recovery_state.RecoveryRepository(connection).row(repo, str(plan['plan_id']))
        if (result == PROJECT_STEP_TRANSIENT_PAUSE and current['status'] == 'PR_OPEN' and recorded is not None
                and recorded['generation'] > (previous_recovery['generation'] if previous_recovery is not None else 0)
                and recorded['reason_code'] == RecoveryReason.GIT_INPUT_UNCERTAIN.value
                and recorded['pending_action'] == 'pause' and recorded['detail'] == current['last_error']
                and scoped_blocking.project_block(connection, repo) is None):
            continue
        if (current['status'] == 'PLAN_BLOCKED'
                and current['blocked_reason'] == BlockReasonCode.MANAGED_WORKTREE_CONFLICT.value
                and scoped_blocking.plan_block(connection, repo, str(plan['plan_id'])) is not None
                and scoped_blocking.project_block(connection, repo) is None):
            # selected_execution kept writes in this plan's source row. Do not
            # replace an independent project mirror with the failed recovery.
            continue
        effects.hydrate_execplan_project(connection, repo, current)
        return PullRequestSyncResult(result, result is None, observation.merged)
    return PullRequestSyncResult(merged=observation.merged)


@dataclass(frozen=True)
class SynchronizeWaitingPullRequestsEffects:
    """Named effects for synchronize_waiting_pull_requests; no entry-point/module access."""
    observe_waiting_pull_requests: Callable[..., object]
    select_waiting_pull_request_conflict: Callable[..., object]


def synchronize_waiting_pull_requests(
    effects: SynchronizeWaitingPullRequestsEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    after_reconciliation: Callable[[], None] | None = None,
    after_observation: Callable[[WaitingPullRequestObservation], None] | None = None,
    require_base_identity: bool = False,
    config_path: Path | None = None,
) -> PullRequestSyncResult:
    observation = effects.observe_waiting_pull_requests(
        connection, repo_path, state_dir, repo,
        require_base_identity=require_base_identity,
        config_path=config_path,
    )
    if after_observation is not None:
        after_observation(observation)
    if after_reconciliation is not None:
        after_reconciliation()
    return effects.select_waiting_pull_request_conflict(
        connection, repo_path, state_dir, repo, observation,
        config_path=config_path,
    )


@dataclass(frozen=True)
class BlockClosedPullRequestEffects:
    """Named effects for _block_closed_pull_request; no entry-point/module access."""
    _final_block_recovery_decision: Callable[..., object]


def _block_closed_pull_request(
    effects: BlockClosedPullRequestEffects, connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    url: str,
    head_oid: object,
) -> None:
    error = "PR이 Merge 없이 닫혔습니다."
    mirrored = runtime_state.project_row(connection, repo)
    clear_projection = bool(
        mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
        and mirrored["plan_path"] == plan["path"]
    )
    with runtime_state._transaction(connection):
        resume_phase = str(plan["status"])
        if resume_phase == "PLAN_BLOCKED" and plan["blocked_reason"] == PR_CLOSED_BLOCK_REASON:
            saved = recovery_state.RecoveryRepository(connection).row(repo, str(plan["plan_id"]))
            if saved is not None and saved["reason_code"] == RecoveryReason.PULL_REQUEST_CLOSED.value:
                resume_phase = str(saved["phase"])
        effects._final_block_recovery_decision(
            connection, repo, plan, resume_phase,
            BlockReasonCode.PULL_REQUEST_CLOSED, error,
            str(head_oid or ""), None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=BlockReasonCode.PULL_REQUEST_CLOSED.value,
            evidence=error,
            release_fingerprint=str(head_oid or "") or None,
            plan_values={
                "pr_url": url,
                "pr_head_oid": head_oid,
            },
            clear_projection=clear_projection,
        )
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
        ManualWorkRepository(connection).reflect_pr_state(repo,str(plan['plan_id']))


@dataclass(frozen=True)
class ResumeReopenedPullRequestEffects:
    """Named effects for _resume_reopened_pull_request; no entry-point/module access."""
    _clear_active_values: Callable[..., object]
    operation_log: Callable[..., object]


def _resume_reopened_pull_request(
    effects: ResumeReopenedPullRequestEffects, connection: sqlite3.Connection,
    repo: str,
    plan: sqlite3.Row,
    url: str,
    head_oid: object,
    *,
    recovery: bool,
    conflict_oids: tuple[str, str] | None = None,
) -> None:
    if conflict_oids is not None and not recovery:
        raise ValueError("PR 충돌 복구 OID는 복구 전이에만 기록할 수 있습니다.")
    checkpoint = ExecutionCheckpointRepository(connection).get(repo, str(plan["plan_id"]))
    saved_recovery = recovery_state.RecoveryRepository(connection).row(repo, str(plan["plan_id"]))
    definition = DefinitionSnapshot.deserialize(plan["definition_snapshot"])
    publication_approved = bool(definition is not None and plan["validated_base_sha"]
        and plan["publication_token"] == definition.publication_token(
            str(plan["validated_base_sha"]), str(head_oid or "")))
    from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
    manual_store=ManualWorkRepository(connection)
    group=manual_store.for_plan(repo,str(plan['plan_id']))
    if group and group['state']=='CLOSED' and group['resume_phase']=='PR_OPEN':
        publication=connection.execute('SELECT * FROM manual_work_publications WHERE repo=? AND group_id=?',
                                       (repo,group['group_id'])).fetchone()
        publication_approved=bool(publication and publication['pr_url']==url
                                  and publication['head']==str(head_oid or ''))
    revalidate_candidate = bool(
        plan["blocked_reason"] == PR_CLOSED_BLOCK_REASON
        and (checkpoint.operation in {"rebase", "rebase_publish"}
             or saved_recovery is not None
             and saved_recovery["reason_code"] == RecoveryReason.PULL_REQUEST_CLOSED.value
             and saved_recovery["phase"] in runtime_state.ACTIVE_STATUSES
             and (saved_recovery["phase"] != "PUBLISHING" or not publication_approved)))
    confirm_publication = bool(
        not recovery and not revalidate_candidate
        and plan['blocked_reason'] == PR_CLOSED_BLOCK_REASON and publication_approved
        and checkpoint.operation == 'push' and checkpoint.expected_head == head_oid
        and (checkpoint.snapshot or {}).get('remote_before') == plan['publication_remote_oid'])
    recovery = recovery or revalidate_candidate
    phase = "WORKING" if recovery else "PUBLISHING" if confirm_publication else "PR_OPEN"
    status = "IMPLEMENTING" if recovery else phase
    from loop_manager_internal.storage.task_calls import management_available
    if status in runtime_state.ACTIVE_STATUSES and not management_available(connection,repo,str(plan['plan_id'])):
        return
    last_error = REBASE_IN_PROGRESS_ERROR if recovery and not revalidate_candidate else None
    execplan_values: dict[str, object] = {
        "status": status,
        "blocked_reason": None,
        "blocked_fingerprint": None,
        "blocked_worktree_fingerprint": None,
        "blocked_review_fingerprint": None,
        "pr_url": url,
        "pr_head_oid": head_oid,
        "last_error": last_error,
    }
    project_values: dict[str, object] = {
        "phase": phase,
        "pr_url": url,
        "pr_head_oid": head_oid,
        "last_error": last_error,
    }
    if recovery:
        for values in (execplan_values, project_values):
            values.update(
                validated_base_sha=None,
                verify_result=None,
                review_result=None,
            )
            if conflict_oids is not None:
                values.update(
                    pr_conflict_recovery=1,
                    pr_conflict_head_oid=conflict_oids[0],
                    pr_conflict_base_oid=conflict_oids[1],
                )
    if revalidate_candidate:
        values = revalidation_values(checkpoint.target_base_oid or plan["active_sha"])
        project_values.update(values)
        execplan_values.update({key: value for key, value in values.items() if key != "phase"})
    with runtime_state._transaction(connection):
        mirrored = runtime_state.project_row(connection, repo)
        repository = runtime_state.RuntimeStateRepository(connection)
        if revalidate_candidate or confirm_publication:
            if (scoped_blocking.project_block(connection, repo) is not None
                    or not blocked_plan_reconciliation.slot_available(connection, repo, str(plan["plan_id"]))):
                return
            resumed_candidate = repository.update_plan(
                repo, str(plan["plan_id"]), plan_values=execplan_values, enqueue=False)
            repository.hydrate_snapshot(repo, resumed_candidate)
        elif (mirrored and not runtime_state.planning_owns_execution_slot(mirrored)
                and mirrored["plan_path"] == plan["path"]):
            projection = project_values if recovery else effects._clear_active_values()
            repository.transition(
                repo,
                str(plan["plan_id"]),
                plan_values=execplan_values,
                project_values=projection,
                enqueue=False,
            )
        else:
            repository.update_plan(
                repo,
                str(plan["plan_id"]),
                plan_values=execplan_values,
                enqueue=False,
            )
        scoped_blocking.clear_plan_block(
            connection, repo, str(plan["plan_id"])
        )
        manual_store.reflect_pr_state(repo,str(plan['plan_id']))
        resumed = notifications.record_work_start(
            connection, repo, "PR handling", plan_id=str(plan["plan_id"]),
        )
        notifications.enqueue_current_project_events(connection, repo)
    if resumed:
        effects.operation_log(f"차단 해제 후 PR 처리 재개: {plan['plan_id']}")


@dataclass(frozen=True)
class ReconcileExistingPullRequestEffects:
    """Named effects for reconcile_existing_pull_request; no entry-point/module access."""
    _pull_request_recovery_effects_reconcile_existing_pull_request: Callable[..., object]


def reconcile_existing_pull_request(
    effects: ReconcileExistingPullRequestEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    pull_request: Mapping[str, object],
    *,
    config_path: Path | None = None,
) -> int | None:
    return pull_request_recovery.reconcile_existing_pull_request(
        effects._pull_request_recovery_effects_reconcile_existing_pull_request(),
        connection, repo_path, state_dir, repo, project, pull_request,
        config_path=config_path,
    )


@dataclass(frozen=True)
class ResumePrConflictRebaseEffects:
    """Named effects for resume_pr_conflict_rebase; no entry-point/module access."""
    _pull_request_recovery_effects_resume_pr_conflict_rebase: Callable[..., object]


def resume_pr_conflict_rebase(
    effects: ResumePrConflictRebaseEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    project: sqlite3.Row,
    worktree: Path,
    branch: str,
) -> int | None:
    return pull_request_recovery.resume_pr_conflict_rebase(effects._pull_request_recovery_effects_resume_pr_conflict_rebase(), connection, repo_path, repo, project, worktree, branch)
