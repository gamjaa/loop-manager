"""recovery project block service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents import agent_scope_decision
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.documents.guidance_approval import GuidanceFormatError
from loop_manager_internal.git import managed_rebase_adoption
from loop_manager_internal.git import managed_rebase_validation
from loop_manager_internal.git import rebase_block_reconciliation
from loop_manager_internal.recovery import blocked_remote_observation
from loop_manager_internal.recovery import checkpoint_ownership
from loop_manager_internal.recovery import scope_reconciliation
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON
from loop_manager_internal.runtime.contracts import REBASE_IN_PROGRESS_ERROR
from loop_manager_internal.runtime.contracts import RuntimeMutation
from loop_manager_internal.runtime.contracts import WORKSPACE_OWNED
from loop_manager_internal.runtime.contracts import WorkspaceOwnershipError
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class ReconcileGuidanceDoubleBlockEffects:
    """Named effects for _reconcile_guidance_double_block; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    _guidance_checkpoint_commit_evidence: Callable[..., object]
    _guidance_document: Callable[..., object]
    _guidance_document_matches_rows: Callable[..., object]
    _guidance_rows: Callable[..., object]
    _guidance_target_state: Callable[..., object]
    _owned_checkpoint_remote_oid: Callable[..., object]
    _proven_manager_base_refs: Callable[..., object]
    _proven_merged_cleanup_paths: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    git: Callable[..., object]
    operation_log: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _reconcile_guidance_double_block(
    effects: ReconcileGuidanceDoubleBlockEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    block: sqlite3.Row,
    project: sqlite3.Row,
    plan: sqlite3.Row,
) -> bool:
    """Restore a published guidance wait only from independent Git and DB proof."""
    false_positive_reasons = {
        BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION.value,
        BlockReasonCode.SHARED_REF_MUTATION.value,
        BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION.value,
        BlockReasonCode.REPOSITORY_STATE_UNSAFE.value,
    }
    if (
        block["reason_code"] not in false_positive_reasons
        or plan["status"] != "PLAN_BLOCKED"
        or plan["blocked_reason"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}
        or plan["agent_role"] != "worker"
        or not plan["guidance_plan_hash"]
        or not plan["publication_remote_oid"]
        or not plan["worker_session_id"]
        or project["plan_path"] != plan["path"]
        or project["branch"] != plan["branch"]
        or project["worktree"] != plan["worktree"]
        or project["agent_role"] != "worker"
        or project["agent_snapshot"] != plan["agent_snapshot"]
        or project["worker_session_id"] != plan["worker_session_id"]
        or int(plan["workspace_owned"] or 0) != WORKSPACE_OWNED
    ):
        return False
    checkpoint = connection.execute(
        "SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
        (repo, plan["plan_id"]),
    ).fetchone()
    recorded_block = connection.execute(
        "SELECT * FROM execplan_blocks WHERE repo = ? AND plan_id = ?",
        (repo, plan["plan_id"]),
    ).fetchone()
    if checkpoint is None or recorded_block is None:
        return False
    branch = str(plan["branch"])
    worktree = Path(str(plan["worktree"]))
    oid = str(plan["publication_remote_oid"])
    try:
        effects.validate_execplan_workspace_identity(
            state_dir, repo, str(plan["plan_id"]), branch, worktree,
        )
        effects.validate_managed_worktree(repo_path, worktree, branch)
        before = GitSnapshot.deserialize(str(plan["agent_snapshot"]))
        current = effects.capture_git_snapshot(worktree)
        request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
            repo, str(plan["plan_id"]),
        )
        if effects._owned_checkpoint_remote_oid(connection, repo_path, request, plan, current) != oid:
            raise ValueError("게시 OID와 원격 head가 일치하지 않습니다.")
        evidence = effects._guidance_checkpoint_commit_evidence(
            repo_path, str(plan["plan_id"]), oid,
        )
        if evidence is None:
            raise ValueError("guidance checkpoint commit identity 또는 tree가 유효하지 않습니다.")
        document = effects._guidance_document(worktree, str(plan["path"]))
        rows = effects._guidance_rows(connection, repo, str(plan["plan_id"]))
        if (
            document is None or document.structure_hash != plan["guidance_plan_hash"]
            or not rows or any(row["applied"] or row["invalidated_decision_hash"] for row in rows)
        ):
            raise ValueError("guidance 제안 또는 승인 대기 상태가 변경됐습니다.")
        proposals, new = effects._guidance_document_matches_rows(
            document, rows, require_decisions=False,
        )
        if new or any(
            row["decision_hash"]
            and proposals[str(row["proposal_id"])].decision.identity != row["decision_hash"]
            for row in rows
        ):
            raise ValueError("guidance User Decision과 저장된 증거가 다릅니다.")
        for target in {str(row["target_path"]) for row in rows}:
            row = next(row for row in rows if row["target_path"] == target)
            if effects._guidance_target_state(worktree, target) != (
                bytes(row["baseline_content"]), int(row["baseline_mode"]),
            ):
                raise ValueError(f"승인 전 AGENTS.md 내용이 변경됐습니다: {target}")
        own_path = str(effects._canonical_path(worktree))
        cleaned = effects._proven_merged_cleanup_paths(connection, repo, before, current)
        fetched_refs = effects._proven_manager_base_refs(
            connection, repo_path, repo, str(plan["plan_id"]), before, current,
        )
        remote_ref = f"refs/remotes/origin/{branch}"
        own_ref = f"refs/heads/{branch}"
        if (
            not before.shared_metadata_complete or not current.shared_metadata_complete
            or before.head != oid or current.head != oid
            or before.branch != branch or current.branch != branch
            or before.index != current.index
            or before.config != current.config
            or before.operations != current.operations
            or any(path != str(plan["path"]) for path in current.dirty)
            or any(
                before.refs.get(name) != current.refs.get(name)
                for name in before.refs.keys() | current.refs.keys()
                if name not in {remote_ref, own_ref} | fetched_refs
                and not name.startswith("refs/codex/turn-diffs/")
            )
            or any(
                before.worktrees.get(path) != current.worktrees.get(path)
                for path in before.worktrees.keys() | current.worktrees.keys()
                if path not in cleaned | {own_path}
            )
        ):
            raise ValueError("저장된 Worker snapshot 밖의 Git 변경이 있습니다.")
        if before.worktrees.get(own_path) != current.worktrees.get(own_path):
            raise ValueError("guidance worktree 등록이 변경됐습니다.")
        if checkpoint["phase"] == "published" and checkpoint["published_oid"] != oid:
            raise ValueError("저장된 checkpoint 게시 OID가 다릅니다.")
        if checkpoint["phase"] == "published" and checkpoint["reason_code"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}:
            raise ValueError("저장된 checkpoint 차단 원인이 guidance와 무관합니다.")
        if checkpoint["phase"] != "published" and checkpoint["reason_code"] not in false_positive_reasons:
            raise ValueError("교체된 checkpoint 원인의 소유권을 확인할 수 없습니다.")
        if recorded_block["reason_code"] not in false_positive_reasons | {GUIDANCE_BLOCK_REASON}:
            raise ValueError("계획 차단 원인이 guidance와 무관합니다.")
        commit_message = effects.git(repo_path, "show", "-s", "--format=%B", oid).stdout
        commit_fields = dict(line.split(": ", 1) for line in commit_message.splitlines() if ": " in line)
        tree = effects.git(repo_path, "rev-parse", f"{oid}^{{tree}}").stdout.strip()
    except (AgentScopeError, GuidanceFormatError, RuntimeError, ValueError, KeyError, TypeError, WorkspaceOwnershipError) as error:
        effects.operation_log(f"guidance 이중 차단 복구 보류 ({plan['plan_id']}): {error}", error=True)
        return False
    identity = blocked_checkpoint_state.request_identity(
        repo, str(plan["plan_id"]), GUIDANCE_BLOCK_REASON, evidence,
        branch, str(worktree), str(plan["guidance_plan_hash"]),
    )
    with runtime_state._transaction(connection):
        connection.execute(
            "UPDATE exec_plans SET blocked_reason = ?, blocked_fingerprint = NULL, "
            "blocked_worktree_fingerprint = NULL, last_error = ? "
            "WHERE repo = ? AND plan_id = ?",
            (GUIDANCE_BLOCK_REASON, evidence, repo, plan["plan_id"]),
        )
        connection.execute(
            "UPDATE execplan_blocks SET reason_code = ?, evidence = ?, "
            "release_fingerprint = ? WHERE repo = ? AND plan_id = ?",
            (GUIDANCE_BLOCK_REASON, evidence, plan["guidance_plan_hash"],
             repo, plan["plan_id"]),
        )
        connection.execute(
            "UPDATE blocked_checkpoints SET request_identity = ?, phase = 'published', "
            "reason_code = ?, evidence = ?, branch = ?, worktree = ?, "
            "commit_identity = ?, tree_oid = ?, local_oid = ?, remote_oid = ?, "
            "published_oid = ?, accepted_remote_oid = ?, result = 'published', "
            "unpublished_reason = NULL, retryable = 0 WHERE repo = ? AND plan_id = ?",
            (identity, GUIDANCE_BLOCK_REASON, evidence, branch, str(worktree),
             commit_fields["Checkpoint-Identity"], tree, oid, oid, oid, oid,
             repo, plan["plan_id"]),
        )
        scoped_blocking.clear_project_block(connection, repo)
        connection.execute(
            "UPDATE projects SET phase = 'PLAN_BLOCKED', last_error = ? WHERE repo = ?",
            (evidence, repo),
        )
    return True


@dataclass(frozen=True)
class PublishedReviewCommitIntentMatchesEffects:
    """Named effects for _published_review_commit_intent_matches; no entry-point/module access."""
    git: Callable[..., object]


def _published_review_commit_intent_matches(
    effects: PublishedReviewCommitIntentMatchesEffects, repo_path: Path, checkpoint: execution_checkpoint_state.CheckpointRecord,
    head: str,
) -> bool:
    """Prove an unfinished Manager commit intent produced the current HEAD."""
    saved = checkpoint.snapshot or {}
    if (checkpoint.operation != 'commit' or not checkpoint.expected_head
            or checkpoint.expected_head == head
            or not saved.get('commit_tree_oid')):
        return False
    return checkpoint_ownership.committed_tree_matches_intent(
        repo_path, checkpoint.expected_head, head, saved, effects.git,
    )


@dataclass(frozen=True)
class ResumeOwnedCheckpointProjectBlockEffects:
    """Named effects for _resume_owned_checkpoint_project_block; no entry-point/module access."""
    _scope_reconciliation_effects_resume_owned_checkpoint_project_block: Callable[..., object]


def _resume_owned_checkpoint_project_block(
    effects: ResumeOwnedCheckpointProjectBlockEffects, connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, block: sqlite3.Row, project: sqlite3.Row, plan: sqlite3.Row,
) -> bool:
    return scope_reconciliation._resume_owned_checkpoint_project_block(
        effects._scope_reconciliation_effects_resume_owned_checkpoint_project_block(),
        connection, repo_path, state_dir, repo, block, project, plan,
    )


def _clear_project_block_without_losing_planning_error(
    connection: sqlite3.Connection, repo: str, project: sqlite3.Row,
) -> None:
    scoped_blocking.clear_project_block(connection, repo)
    if (
        runtime_state.planning_owns_execution_slot(project)
        and project["planning_error"]
    ):
        connection.execute(
            "UPDATE projects SET last_error = ? WHERE repo = ?",
            (project["planning_error"], repo),
        )


@dataclass(frozen=True)
class ReconcileRebaseEvidenceBlockEffects:
    """Named effects for _reconcile_rebase_evidence_block; no entry-point/module access."""
    _current_rebase_service: Callable[..., object]
    _revision_execplan_input_fingerprint: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    operation_log: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]


def _reconcile_rebase_evidence_block(
    effects: ReconcileRebaseEvidenceBlockEffects, connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, block: sqlite3.Row, *, plan_block: bool,
) -> bool:
    """Recheck the owned current rebase and release only its saved cause."""
    plan_id = str(block["plan_id"] or "")
    plan = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?", (repo, plan_id),
    ).fetchone()
    project = connection.execute("SELECT * FROM projects WHERE repo = ?", (repo,)).fetchone()
    if plan is None or project is None or block['repo'] != repo:
        return False
    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    checkpoint = checkpoints.get(repo, plan_id)
    recovery = recovery_state.RecoveryRepository(connection).row(repo, plan_id)
    if not rebase_block_reconciliation.block_matches(block, recovery, checkpoint):
        return False
    if scoped_blocking.is_scope_baseline_block(block) and (
            not plan['agent_snapshot'] or plan['agent_recovery']):
        return False
    active = runtime_state.RuntimeStateRepository(connection).active_row(repo)
    if (runtime_state.planning_owns_execution_slot(project) or project['planning_error']
            or (active is not None and active['plan_id'] != plan_id)
            or int(plan['workspace_owned'] or 0) != WORKSPACE_OWNED
            or not plan['branch'] or not plan['worktree']
            or effects.unmet_execplan_dependencies(connection, repo, plan_id)
            or (plan_block and (plan['status'] != 'PLAN_BLOCKED'
                or plan['blocked_reason'] != block['reason_code']
                or scoped_blocking.project_block(connection, repo) is not None))
            or (not plan_block and project['phase'] != 'PROJECT_BLOCKED')):
        return False
    worktree, branch = Path(str(plan['worktree'])), str(plan['branch'])
    try:
        service = effects._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id, worktree, branch)
        def observe():
            observation = service.require_owned(checkpoint=checkpoints.get(repo, plan_id))
            effects.validate_blocked_agent_scope(
                connection, repo_path, repo, str(project['base_branch']), worktree,
                str(plan['path']), accepted_rebase=True, saved_plan=plan)
            if (observation[1].status is managed_rebase_validation.RebaseStatus.WAITING
                    and plan['agent_result_snapshot']
                    and plan['blocked_fingerprint'] and plan['blocked_worktree_fingerprint']):
                before = GitSnapshot.deserialize(str(plan['agent_result_snapshot']))
                current = effects.capture_git_snapshot(worktree)
                if rebase_block_reconciliation.pending_block_unchanged(
                        plan, block, recovery, before, current,
                        revision_input=effects._revision_execplan_input_fingerprint(
                            repo_path, str(plan['current_revision_sha'] or plan['plan_sha']), plan),
                        worktree_input=effects._worktree_execplan_input_fingerprint(worktree, plan)):
                    raise RuntimeError('unchanged blocked rebase still needs user input or Git progress')
            return observation
        observation = observe()
        runtime_state.RuntimeStateRepository(connection).resume_rebase_block(
            plan, project, block, checkpoints, observation, expected_checkpoint=checkpoint,
            is_plan_block=plan_block,
            reobserve=observe,
            dependencies_ready=lambda: not effects.unmet_execplan_dependencies(connection, repo, plan_id),
            pending_error=REBASE_IN_PROGRESS_ERROR)
        return True
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as error:
        effects.operation_log(f"관리 rebase 차단 재평가 보류 ({plan_id}): {error}", error=True)
        return False


@dataclass(frozen=True)
class ReconcileScopeBaselinePlanBlockEffects:
    """Named effects for _reconcile_scope_baseline_plan_block; no entry-point/module access."""
    _owned_checkpoint_remote_oid: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    operation_log: Callable[..., object]
    unmet_execplan_dependencies: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]
    validate_managed_worktree: Callable[..., object]


def _reconcile_scope_baseline_plan_block(effects: ReconcileScopeBaselinePlanBlockEffects, connection, repo_path, state_dir, repo, block):
    """Release a scope false positive without input changes or retry resets."""
    plan_id = block['plan_id']
    runtime = runtime_state.RuntimeStateRepository(connection)
    plan = connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                              (repo, plan_id)).fetchone()
    project = connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()
    if (plan is None or project is None or plan['status'] != 'PLAN_BLOCKED'
            or not scoped_blocking.is_scope_baseline_block(block)
            or plan['blocked_reason'] != block['reason_code']
            or not plan['agent_snapshot'] or plan['agent_recovery']
            or int(plan['workspace_owned'] or 0) != WORKSPACE_OWNED
            or scoped_blocking.project_block(connection, repo) is not None
            or runtime_state.planning_owns_execution_slot(project) or project['planning_error']
            or not runtime.recovery_slot_available(repo) or runtime.active_row(repo) is not None
            or effects.unmet_execplan_dependencies(connection, repo, plan_id)):
        return False
    worktree, branch = Path(str(plan['worktree'] or '')), str(plan['branch'] or '')
    checkpoints = execution_checkpoint_state.ExecutionCheckpointRepository(connection)
    checkpoint = checkpoints.get(repo, plan_id)
    try:
        def observe():
            current = effects.capture_git_snapshot(worktree)
            effects.validate_execplan_workspace_identity(state_dir, repo, plan_id, branch, worktree)
            effects.validate_managed_worktree(repo_path, worktree, branch)
            effects.validate_blocked_agent_scope(connection, repo_path, repo, project['base_branch'],
                                         worktree, plan['path'], saved_plan=plan, require_safe_result=True)
            # Reuse the live remote/PR ownership gate even without a rebase.
            request = connection.execute('SELECT 1 FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                                         (repo, plan_id)).fetchone()
            if request is not None:
                saved = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(repo, plan_id)
                if saved.phase.value == 'published' and effects._owned_checkpoint_remote_oid(
                        connection, repo_path, saved, plan, current) is None:
                    raise AgentScopeError('scope 차단의 게시 checkpoint 소유권 불일치')
            elif plan['publication_remote_oid'] or plan['pr_url']:
                raise AgentScopeError('scope 차단의 원격 소유권 checkpoint가 없습니다.')
            if effects.capture_git_snapshot(worktree) != current:
                raise AgentScopeError('scope 차단 검사 중 Git 상태가 변경됐습니다.')
            return current
        observed = observe()
        with runtime_state._transaction(connection):
            if (dict(connection.execute('SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?',
                                         (repo, plan_id)).fetchone()) != dict(plan)
                    or dict(connection.execute('SELECT * FROM projects WHERE repo = ?', (repo,)).fetchone()) != dict(project)
                    or checkpoints.get(repo, plan_id) != checkpoint or observe() != observed):
                raise AgentScopeError('scope 차단 해제 중 소유 상태가 변경됐습니다.')
            scoped_blocking.clear_revalidated_rebase_block(connection, block, is_plan_block=True)
            recovery_state.RecoveryRepository(connection).resolve_rebase_block(repo, plan_id, block)
            checkpoints.record(repo, plan_id, expected_generation=checkpoint.generation,
                operation=checkpoint.operation, expected_head=checkpoint.expected_head,
                target_base_oid=checkpoint.target_base_oid, observed_remote_oid=checkpoint.observed_remote_oid,
                snapshot={**(checkpoint.snapshot or {}), 'scope_block_release_audit': [
                    *(checkpoint.snapshot or {}).get('scope_block_release_audit', ()),
                    {'block': dict(block), 'candidate': observed.serialize(),
                     'prior_artifacts': {key: plan[key] for key in (
                         'worker_result', 'verify_result', 'review_result', 'last_error')}}]})
            values = managed_rebase_adoption.revalidation_values(plan['active_sha'])
            values.pop('phase')
            values.update(status='IMPLEMENTING', blocked_reason=None, blocked_fingerprint=None,
                          blocked_worktree_fingerprint=None, blocked_review_fingerprint=None)
            updated = runtime.update_plan(repo, plan_id, plan_values=values, enqueue=False)
            runtime.hydrate_snapshot(repo, updated)
        return True
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as error:
        effects.operation_log(f'Agent scope 차단 재평가 보류 ({plan_id}): {error}', error=True)
        return False


@dataclass(frozen=True)
class ResumeProjectBlockIfSafeEffects:
    """Named effects for resume_project_block_if_safe; no entry-point/module access."""
    _scope_reconciliation_effects_resume_project_block_if_safe: Callable[..., object]


def resume_project_block_if_safe(
    effects: ResumeProjectBlockIfSafeEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
) -> bool:
    return scope_reconciliation.resume_project_block_if_safe(effects._scope_reconciliation_effects_resume_project_block_if_safe(), connection, repo_path, state_dir, repo)


@dataclass(frozen=True)
class ReconcileFalsePositiveRuntimeBlockEffects:
    """Named effects for reconcile_false_positive_runtime_block; no entry-point/module access."""
    _canonical_path: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    operation_log: Callable[..., object]
    validate_persisted_agent_scope: Callable[..., object]


def reconcile_false_positive_runtime_block(
    effects: ReconcileFalsePositiveRuntimeBlockEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    project: sqlite3.Row,
    block: sqlite3.Row,
    runtime_mutation: RuntimeMutation,
) -> bool:
    """Reconcile a saved prevalidation result only when its entire delta is ours."""
    if (
        effects._canonical_path(repo_path) != effects._canonical_path(runtime_mutation.source)
        or not str(block["evidence"] or "").startswith("Agent 범위 선검증 실패:")
        or not project["agent_snapshot"]
        or not project["agent_result_snapshot"]
    ):
        return False
    try:
        before = GitSnapshot.deserialize(str(project["agent_snapshot"]))
        result = GitSnapshot.deserialize(str(project["agent_result_snapshot"]))
        saved_worktree = Path(str(project["worktree"] or repo))
        current = effects.capture_git_snapshot(saved_worktree)
        runtime_path = str(effects._canonical_path(runtime_mutation.path))
        expected = agent_scope_decision.reconcile_runtime_snapshot(
            before, before, current, runtime_path,
        )
        if result != current:
            effects.operation_log(
                f"runtime 차단 재조정 거부: 결과 snapshot 불일치 "
                f"path={runtime_path} sha={runtime_mutation.commit_sha}",
                error=True,
            )
            return False
        if expected != current:
            deltas = []
            for label, older, newer in (
                ("refs", expected.refs, current.refs),
                ("worktrees", expected.worktrees, current.worktrees),
                ("config", expected.config, current.config),
                ("operations", expected.operations, current.operations),
            ):
                changed = sorted(
                    key for key in set(older) | set(newer)
                    if older.get(key) != newer.get(key)
                )
                if changed:
                    deltas.append(f"{label}={','.join(changed[:8])}")
            if any(getattr(expected, key) != getattr(current, key)
                   for key in ("head", "branch", "index", "dirty")):
                deltas.append("local-state")
            effects.operation_log(
                f"runtime 차단 재조정 거부: path={runtime_path} "
                f"sha={runtime_mutation.commit_sha} delta={'; '.join(deltas)}",
                error=True,
            )
            return False
        effects.validate_persisted_agent_scope(
            connection, repo_path, state_dir, repo,
            runtime_mutation=runtime_mutation,
        )
    except (AgentScopeError, RuntimeError, ValueError, KeyError):
        return False
    return True


@dataclass(frozen=True)
class ObserveBlockedPlanRemotesEffects:
    """Named effects for observe_blocked_plan_remotes; no entry-point/module access."""
    _worktree_state_signature: Callable[..., object]
    git: Callable[..., object]
    github_repository: Callable[..., object]


def observe_blocked_plan_remotes(
    effects: ObserveBlockedPlanRemotesEffects, connection: sqlite3.Connection, repo_path: Path, repo: str,
    project: sqlite3.Row,
    *, plan_id: str | None = None,
) -> None:
    """Persist blocked-plan remote input before any project execution gate."""

    blocked_remote_observation.ensure_schema(connection)
    project_block = scoped_blocking.project_block(connection, repo)
    linked_plan_id = str(project_block["plan_id"] or "") if project_block else ""
    plans = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND "
        "(status = 'PLAN_BLOCKED' OR plan_id = ? OR plan_id = ?) "
        "AND (? IS NULL OR plan_id = ?) ORDER BY plan_id",
        (repo, linked_plan_id, plan_id, plan_id, plan_id),
    ).fetchall()
    if not plans:
        return
    configured = effects.git(repo_path, "remote", "get-url", "origin", check=False)
    origin = configured.stdout.strip() if configured.returncode == 0 else ""
    origin_failure = "origin-unavailable"
    registered_origin = str(project["origin"] or "")
    query_origin = ""
    if not registered_origin:
        origin_failure = "registered-origin-missing"
    elif origin:
        try:
            origin_identity = effects.github_repository(origin)
        except RuntimeError:
            origin_identity = origin
        if origin_identity == registered_origin:
            query_origin = origin
        else:
            origin_failure = "registered-origin-mismatch"
    base_ref = f"refs/heads/{project['base_branch']}"
    for plan in plans:
        branch_ref = f"refs/heads/{plan['branch']}" if plan["branch"] else ""
        base, branch = blocked_remote_observation.observe_refs(
            repo_path, query_origin, (base_ref, branch_ref),
            unavailable_reason=origin_failure,
        )
        block = connection.execute(
            "SELECT reason_code, release_fingerprint FROM execplan_blocks "
            "WHERE repo = ? AND plan_id = ?",
            (repo, plan["plan_id"]),
        ).fetchone()
        if block is not None:
            reason = str(block["reason_code"])
            fingerprint = str(block["release_fingerprint"] or "")
        elif plan["status"] == "PLAN_BLOCKED":
            reason = str(plan["blocked_reason"] or "")
            fingerprint = str(plan["blocked_fingerprint"] or "")
        else:
            reason = None
            fingerprint = None
        linked_project_block = project_block if linked_plan_id == plan["plan_id"] else None
        worktree_fingerprint = str(plan["blocked_worktree_fingerprint"] or "")
        if plan["worktree"]:
            try:
                worktree_fingerprint = effects._worktree_state_signature(Path(plan["worktree"]))
            except (OSError, RuntimeError, ValueError):
                worktree_fingerprint = "unavailable"
        blocked_remote_observation.record(
            connection, repo=repo, plan_id=str(plan["plan_id"]),
            registered_origin=registered_origin or None,
            origin_url=origin or None, base_ref=base_ref, branch_ref=branch_ref,
            base=base, branch=branch, blocked_reason=reason,
            blocked_fingerprint=fingerprint,
            project_blocked_reason=(
                str(linked_project_block["reason_code"])
                if linked_project_block else None
            ),
            project_blocked_fingerprint=(
                str(linked_project_block["release_fingerprint"] or "")
                if linked_project_block else None
            ),
            worktree_fingerprint=worktree_fingerprint,
        )
    connection.commit()
