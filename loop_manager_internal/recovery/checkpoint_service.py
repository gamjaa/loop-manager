"""recovery checkpoint service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.agents.agent_invocation import MAX_TRANSIENT_AGENT_RETRIES
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.application.contracts import PROJECT_STEP_TRANSIENT_PAUSE
from loop_manager_internal.application.contracts import PROJECT_STEP_USAGE_PAUSE
from loop_manager_internal.application.contracts import ReviewerProtocolError
from loop_manager_internal.documents import reviewer_specification
from loop_manager_internal.execution.execution_runtime import ExecutionOutcome
from loop_manager_internal.execution.execution_runtime import ExecutionOutcomeKind
from loop_manager_internal.execution.execution_runtime import ExecutionPhase
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery import checkpoint_adapter
from loop_manager_internal.recovery import checkpoint_recovery
from loop_manager_internal.recovery import recovery_runtime
from loop_manager_internal.recovery import scope_reconciliation
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.checkpoint_adapter import _BlockedCheckpointResults
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON
from loop_manager_internal.runtime.contracts import PROJECT_STEP_PLANNING_PENDING
from loop_manager_internal.runtime.contracts import PROJECT_STEP_RETRY
from loop_manager_internal.storage import blocked_checkpoint_state
from loop_manager_internal.storage import recovery_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import hashlib
import json
import sqlite3


def _remote_checkpoint_input_identity(
    blocked: sqlite3.Row, snapshot: GitSnapshot | None,
    ignored_paths: tuple[str, ...] = (),
) -> str:
    payload = (
        str(blocked["blocked_reason"] or ""),
        str(blocked["blocked_fingerprint"] or ""),
        str(blocked["blocked_worktree_fingerprint"] or ""),
        snapshot.serialize() if snapshot is not None else "unavailable",
        ignored_paths,
    )
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _remote_checkpoint_refusal(
    connection: sqlite3.Connection,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    row: sqlite3.Row,
    oid: str | None,
    input_identity: str,
    reason: str,
    *, retryable: bool = False, preserve_cause: bool = False,
) -> None:
    if (
        row["inspected_remote_oid"] == oid
        and row["inspected_input"] == input_identity
        and row["inspection_reason"] == reason
    ):
        return
    with runtime_state._transaction(connection):
        repository.record_remote_refusal(
            request, oid, input_identity, reason, commit=False,
            retryable=retryable,
        )
        runtime = runtime_state.RuntimeStateRepository(connection)
        projected = runtime.projected_row(request.repo)
        if preserve_cause and (
            scoped_blocking.project_block(connection, request.repo) is not None
            or projected is not None and projected['plan_id'] != request.plan_id
        ):
            runtime.update_plan(request.repo, request.plan_id, plan_values={"last_error": reason}, enqueue=False)
        else:
            runtime.transition(
                request.repo, request.plan_id,
                plan_values={"last_error": reason},
                project_values={"last_error": reason},
                enqueue=False,
            )
        if not preserve_cause:
            connection.execute(
                "UPDATE execplan_blocks SET evidence = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE repo = ? AND plan_id = ?",
                (reason, request.repo, request.plan_id),
            )
        notifications.enqueue_current_project_events(connection, request.repo)


@dataclass(frozen=True)
class ResumeRemoteCorrectedExecplanEffects:
    """Named effects for _resume_remote_corrected_execplan; no entry-point/module access."""
    hydrate_execplan_project: Callable[..., object]


def _resume_remote_corrected_execplan(
    effects: ResumeRemoteCorrectedExecplanEffects, connection: sqlite3.Connection, repo: str, plan: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    candidate_oid: str, input_identity: str, reason: str,
    *, new_failure_stage: str | None = None, assessment: str | None = None,
) -> None:
    """Treat a user's committed correction as unverified Worker input."""
    plan_id = str(plan["plan_id"])
    # Hydration commits; finish it before the atomic release boundary.
    effects.hydrate_execplan_project(connection, repo, plan)
    with runtime_state._transaction(connection):
        runtime_state.RuntimeStateRepository(connection).transition(
            repo, plan_id,
            plan_values={
                "status": "IMPLEMENTING", "blocked_reason": None,
                "blocked_fingerprint": None, "blocked_worktree_fingerprint": None,
                "blocked_review_fingerprint": None, "last_error": None,
                "retry_count": 0, "worker_result": None,
                "verify_result": assessment if new_failure_stage == "verify" else None,
                "review_result": assessment if new_failure_stage == "review" else None,
                "validated_base_sha": None, "reviewer_session_id": None,
                "worker_input_token": None, "archive_token": None,
                "verify_token": None, "review_token": None,
                "publication_token": None, "publication_remote_oid": None,
                "verify_failure_history": plan["verify_failure_history"] if new_failure_stage else "{}",
                "review_failure_history": plan["review_failure_history"] if new_failure_stage else "{}",
                "effective_worker_level": plan["initial_worker_level"],
                "review_level_history": "{}",
                "review_correction_state": "{}",
                "agent_role": None, "agent_snapshot": None,
                "agent_baseline": None, "agent_recovery": None,
                "agent_input_plan": None, "agent_result_plan": None,
                "agent_input_fingerprint": None,
                "agent_result_snapshot": None,
                "agent_manager_snapshot": None,
            },
            project_values={
                "phase": "WORKING", "last_error": None,
                "worker_result": None,
                "verify_result": assessment if new_failure_stage == "verify" else None,
                "review_result": assessment if new_failure_stage == "review" else None, "validated_base_sha": None,
                "worker_session_id": plan["worker_session_id"],
                "agent_role": None, "agent_snapshot": None,
                "agent_baseline": None, "agent_recovery": None,
                "agent_input_plan": None, "agent_result_plan": None,
                "agent_input_fingerprint": None,
                "agent_result_snapshot": None,
                "agent_manager_snapshot": None,
            },
            enqueue=False,
        )
        scoped_blocking.clear_plan_block(connection, repo, plan_id)
        repository.record_remote_inspection(
            request, candidate_oid, input_identity, reason, commit=False,
        )


@dataclass(frozen=True)
class RemoteCheckpointHasNewFailureEffects:
    """Named effects for _remote_checkpoint_has_new_failure; no entry-point/module access."""
    _deserialize_failure_history: Callable[..., object]
    validation_failure_fingerprints: Callable[..., object]


def _remote_checkpoint_has_new_failure(
    effects: RemoteCheckpointHasNewFailureEffects, connection: sqlite3.Connection, repo: str, blocked: sqlite3.Row,
    stage: str, output: str, worktree: Path,
) -> bool:
    """Use the normal validation fingerprint contract to recognize progress."""
    history = effects._deserialize_failure_history(blocked[f"{stage}_failure_history"])
    previous = blocked[f"{stage}_result"]
    known = set(history)
    if previous:
        known.update(key for key, _ in effects.validation_failure_fingerprints(stage, previous, worktree))
    if not known:
        request = blocked_checkpoint_state.BlockedCheckpointRepository(connection).get(
            repo, str(blocked["plan_id"]),
        )
        if request and request.evidence:
            try:
                known.update(key for key, _ in effects.validation_failure_fingerprints(
                    stage, request.evidence, worktree,
                ))
            except ReviewerProtocolError:
                return False
    return bool(known) and any(
        key not in known for key, _ in effects.validation_failure_fingerprints(stage, output, worktree)
    )


@dataclass(frozen=True)
class RemoteCheckpointReviewRevisionEffects:
    """Named effects for _remote_checkpoint_review_revision; no entry-point/module access."""
    _recorded_base_fetch: Callable[..., object]
    git: Callable[..., object]


def _remote_checkpoint_review_revision(
    effects: RemoteCheckpointReviewRevisionEffects, repo_path: Path, worktree: Path, base_branch: str,
    state_dir: Path,
) -> tuple[str, str] | None:
    """Read the latest base and a candidate HEAD that contains that base."""
    base = effects._recorded_base_fetch(
        repo_path, state_dir, base_branch, owner="project",
    )
    head = effects.git(worktree, "rev-parse", "HEAD^{commit}").stdout.strip()
    if not base or not head:
        raise RuntimeError("원격 checkpoint 검증 revision을 확인할 수 없습니다.")
    ancestry = effects.git(
        repo_path, "merge-base", "--is-ancestor", base, head, check=False,
    ).returncode
    if ancestry not in {0, 1}:
        raise RuntimeError("원격 checkpoint 후보의 base ancestry를 확인할 수 없습니다.")
    return (base, head) if ancestry == 0 else None


@dataclass(frozen=True)
class EvaluateRemoteCheckpointCauseEffects:
    """Named effects for _evaluate_remote_checkpoint_cause; no entry-point/module access."""
    _checkpoint_recovery_effects_evaluate_remote_checkpoint_cause: Callable[..., object]


def _evaluate_remote_checkpoint_cause(
    effects: EvaluateRemoteCheckpointCauseEffects, connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, blocked: sqlite3.Row, config_path: Path | None,
    timeout_seconds: int | None,
) -> tuple[bool, str, str | None]:
    return checkpoint_recovery._evaluate_remote_checkpoint_cause(
        effects._checkpoint_recovery_effects_evaluate_remote_checkpoint_cause(),
        connection, repo_path, state_dir, repo, blocked, config_path, timeout_seconds,
    )


@dataclass(frozen=True)
class ReclassifyRemoteSpecificationDeadlockEffects:
    """Named effects for _reclassify_remote_specification_deadlock; no entry-point/module access."""
    _reclassify_remote_checkpoint_block: Callable[..., object]
    parse_reviewer_result: Callable[..., object]


def _reclassify_remote_specification_deadlock(
    effects: ReclassifyRemoteSpecificationDeadlockEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    blocked: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    accepted_oid: str,
    worktree: Path,
    review: str,
) -> None:
    """Persist a newly discovered specification block beside its published checkpoint."""

    verdict, details = effects.parse_reviewer_result(review)
    if verdict != "SPEC_BLOCKED" or not isinstance(
        details, reviewer_specification.SpecificationDeadlock
    ):
        raise ReviewerProtocolError("원격 checkpoint 명세 교착 증거가 유효하지 않습니다.")
    effects._reclassify_remote_checkpoint_block(
        connection, repo_path, repo, blocked, repository, request,
        accepted_oid, worktree,
        reason=BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
        evidence=review, review_fingerprint_source=review,
        user_targets=details.paths,
    )


@dataclass(frozen=True)
class ReclassifyRemoteCheckpointBlockEffects:
    """Named effects for _reclassify_remote_checkpoint_block; no entry-point/module access."""
    _revision_execplan_input_fingerprint: Callable[..., object]
    _revision_review_issue_fingerprint: Callable[..., object]
    _worktree_execplan_input_fingerprint: Callable[..., object]


def _reclassify_remote_checkpoint_block(
    effects: ReclassifyRemoteCheckpointBlockEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    blocked: sqlite3.Row,
    repository: blocked_checkpoint_state.BlockedCheckpointRepository,
    request: blocked_checkpoint.CheckpointRequest,
    accepted_oid: str,
    worktree: Path,
    *,
    reason: BlockReasonCode,
    evidence: str,
    review_fingerprint_source: str | None,
    user_targets: tuple[str, ...],
) -> None:
    """Replace one final cause without resetting its published checkpoint."""

    base_revision = str(
        blocked["active_sha"] or blocked["current_revision_sha"] or blocked["plan_sha"] or ""
    )
    release_fingerprint = effects._revision_execplan_input_fingerprint(
        repo_path, base_revision, blocked,
    )
    review_fingerprint = (
        effects._revision_review_issue_fingerprint(
            repo_path, base_revision, review_fingerprint_source,
        )
        if review_fingerprint_source is not None else None
    )
    worktree_fingerprint = effects._worktree_execplan_input_fingerprint(worktree, blocked)
    observation = recovery_runtime.RecoveryObservation(
        reason=recovery_runtime.reason_from_block(reason),
        repo=repo, plan_id=str(blocked["plan_id"]), phase="PLAN_BLOCKED",
        plan_revision=str(blocked["current_revision_sha"] or blocked["plan_sha"] or ""),
        base_revision=base_revision,
        fingerprint=hashlib.sha256(
            f"{release_fingerprint}|{review_fingerprint or evidence}".encode()
        ).hexdigest(),
        progress_marker=accepted_oid, detail=evidence,
        user_targets=user_targets,
    )
    with runtime_state._transaction(connection):
        decision = recovery_state.RecoveryRepository(connection).observe(
            observation, final_block_reason=reason,
        ).decision
        recovery_runtime.require_final(decision)
        runtime_state.RuntimeStateRepository(connection).transition(
            repo, str(blocked["plan_id"]), expected_status="PLAN_BLOCKED",
            plan_values={
                "blocked_reason": reason.value,
                "blocked_fingerprint": release_fingerprint,
                "blocked_worktree_fingerprint": worktree_fingerprint,
                "blocked_review_fingerprint": review_fingerprint,
                "review_result": evidence,
                "last_error": evidence,
                "review_failure_history": "{}",
                "review_level_history": "{}",
                "review_correction_state": "{}",
                "retry_count": 0,
            },
            project_values={"last_error": evidence},
            enqueue=False,
        )
        changed = connection.execute(
            "UPDATE execplan_blocks SET reason_code = ?, evidence = ?, "
            "release_fingerprint = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND plan_id = ? AND reason_code = ?",
            (
                reason.value, evidence, release_fingerprint, repo,
                str(blocked["plan_id"]), str(blocked["blocked_reason"]),
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("원격 checkpoint 차단 원인이 변경됐습니다.")
        repository.reclassify_published(
            request, reason_code=reason.value, evidence=evidence,
            release_fingerprint=release_fingerprint,
            accepted_oid=accepted_oid,
        )
        notifications.enqueue_current_project_events(connection, repo)


@dataclass(frozen=True)
class ReconcilePublishedBlockedCheckpointEffects:
    """Named effects for _reconcile_published_blocked_checkpoint; no entry-point/module access."""
    _checkpoint_recovery_effects_reconcile_published_blocked_checkpoint: Callable[..., object]


def _reconcile_published_blocked_checkpoint(
    effects: ReconcilePublishedBlockedCheckpointEffects, connection: sqlite3.Connection,
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
    return checkpoint_recovery._reconcile_published_blocked_checkpoint(
        effects._checkpoint_recovery_effects_reconcile_published_blocked_checkpoint(),
        connection, repo_path, state_dir, repo, blocked, row, request, repository, config_path, verify_timeout_seconds, adopt_only=adopt_only, expected_remote_oid=expected_remote_oid, project_recovery_context=project_recovery_context,
    )


@dataclass(frozen=True)
class ResumeLegacyReviewCountBlockEffects:
    """Named effects for _resume_legacy_review_count_block; no entry-point/module access."""
    _scope_reconciliation_effects_resume_legacy_review_count_block: Callable[..., object]


def _resume_legacy_review_count_block(
    effects: ResumeLegacyReviewCountBlockEffects, connection: sqlite3.Connection, repo_path: Path, state_dir: Path,
    repo: str, plan: sqlite3.Row,
) -> bool:
    return scope_reconciliation._resume_legacy_review_count_block(
        effects._scope_reconciliation_effects_resume_legacy_review_count_block(),
        connection, repo_path, state_dir, repo, plan,
    )


@dataclass(frozen=True)
class ResumePlanBlocksEffects:
    """Named effects for resume_plan_blocks; no entry-point/module access."""
    _scope_reconciliation_effects_resume_plan_blocks: Callable[..., object]


def resume_plan_blocks(
    effects: ResumePlanBlocksEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    base: str,
    config_path: Path | None = None,
    verify_timeout_seconds: int | None = None,
    plan_id: str | None = None,
    force_timeout_recheck: bool = False,
    recovery_request_id: str | None = None,
) -> bool:
    return scope_reconciliation.resume_plan_blocks(
        effects._scope_reconciliation_effects_resume_plan_blocks(), connection,
        repo_path, state_dir, repo, base, config_path, verify_timeout_seconds,
        plan_id, force_timeout_recheck, recovery_request_id,
    )


@dataclass(frozen=True)
class OwnedCheckpointRemoteOidEffects:
    """Named effects for _owned_checkpoint_remote_oid; no entry-point/module access."""
    git: Callable[..., object]


def _owned_checkpoint_remote_oid(
    effects: OwnedCheckpointRemoteOidEffects, connection: sqlite3.Connection,
    repo_path: Path,
    request: blocked_checkpoint.CheckpointRequest,
    plan: sqlite3.Row,
    current: GitSnapshot,
) -> str | None:
    """Prove a changed tracking ref against durable publication and live origin."""
    if not request.branch or current.branch != request.branch:
        return None
    remote_ref = f"refs/remotes/origin/{request.branch}"
    local_ref = f"refs/heads/{request.branch}"
    tracked = current.refs.get(remote_ref)
    if tracked is None or tracked[1] or current.refs.get(local_ref) != (current.head, ""):
        return None
    candidate = tracked[0]
    published = str(plan["publication_remote_oid"] or (plan["pr_head_oid"] if request.pr_url else None) or "")
    durable = candidate == published and bool(published)
    if not durable and request.local_oid == candidate and request.tree_oid and request.commit_identity:
        try:
            durable = (
                current.head == candidate
                and effects.git(repo_path, "rev-parse", f"{candidate}^{{tree}}").stdout.strip()
                == request.tree_oid
                and f"Checkpoint-Identity: {request.commit_identity}" in
                effects.git(repo_path, "show", "-s", "--format=%B", candidate).stdout
            )
        except RuntimeError:
            durable = False
    if not durable:
        return None
    try:
        observed = effects.git(
            repo_path, "ls-remote", "--heads", "origin",
            f"refs/heads/{request.branch}", check=False,
        )
    except RuntimeError:
        return None
    rows = [line.split() for line in observed.stdout.splitlines() if line.strip()]
    if observed.returncode or rows != [[candidate, f"refs/heads/{request.branch}"]]:
        return None
    return candidate


@dataclass(frozen=True)
class GuidanceCheckpointCommitEvidenceEffects:
    """Named effects for _guidance_checkpoint_commit_evidence; no entry-point/module access."""
    _nul_git_paths: Callable[..., object]
    git: Callable[..., object]


def _guidance_checkpoint_commit_evidence(
    effects: GuidanceCheckpointCommitEvidenceEffects, repo_path: Path, plan_id: str, oid: str,
) -> str | None:
    """Return the original guidance evidence only for an intact Manager commit."""
    try:
        message = effects.git(repo_path, "show", "-s", "--format=%B", oid).stdout
        parent_line = effects.git(repo_path, "show", "-s", "--format=%P", oid).stdout.strip()
        parents = parent_line.split()
        tree = effects.git(repo_path, "rev-parse", f"{oid}^{{tree}}").stdout.strip()
        changed = tuple(sorted(effects._nul_git_paths(
            repo_path, "diff", "--no-renames", "--name-only", "-z", parents[0], oid,
        ))) if len(parents) == 1 else ()
        author = effects.git(repo_path, "show", "-s", "--format=%an <%ae>%n%cn <%ce>", oid).stdout.splitlines()
        fields = dict(line.split(": ", 1) for line in message.splitlines() if ": " in line)
        evidence = json.loads(fields["Evidence"])
        paths = tuple(sorted(
            line[2:] for line in message.split("Paths:\n", 1)[1].splitlines()
            if line.startswith("- ") and line != "- (none)"
        ))
        if (
            len(parents) != 1
            or author != ["Loop Manager <loop-manager@localhost>"] * 2
            or not isinstance(evidence, str)
            or fields.get("Reason") != GUIDANCE_BLOCK_REASON
            or fields.get("Scope") != "plan"
            or fields.get("Evidence-SHA256") != hashlib.sha256(evidence.encode()).hexdigest()
            or paths != changed
            or message.splitlines()[0] != f"[{plan_id}] BLOCKED checkpoint"
            or fields.get("Checkpoint-Identity") != blocked_checkpoint.checkpoint_identity(
                plan_id=plan_id, reason_code=GUIDANCE_BLOCK_REASON,
                evidence=evidence, parent_oid=parents[0], tree_oid=tree,
                included_paths=paths,
            )
        ):
            return None
        return evidence
    except (KeyError, ValueError, IndexError, RuntimeError, json.JSONDecodeError):
        return None


@dataclass(frozen=True)
class BlockedCheckpointAdapterEffects:
    """Named effects for _BlockedCheckpointAdapter; no entry-point/module access."""
    _checkpoint_publication_effects: Callable[..., object]
    _checkpoint_safety_effects: Callable[..., object]


def _BlockedCheckpointAdapter(
    effects: BlockedCheckpointAdapterEffects, connection: sqlite3.Connection, repo_path: Path, state_dir: Path, repo: str,
    *, reconciliation_scope: bool = False, project_recovery_scope: bool = False,
) -> checkpoint_adapter.BlockedCheckpointAdapter:
    return checkpoint_adapter.BlockedCheckpointAdapter(
        connection, repo_path, state_dir, repo,
        reconciliation_scope=reconciliation_scope,
        project_recovery_scope=project_recovery_scope,
        safety_effects=effects._checkpoint_safety_effects,
        publication_effects=effects._checkpoint_publication_effects,
    )


@dataclass(frozen=True)
class PushDiagnosticCheckpointEffects:
    """Named effects for push_diagnostic_checkpoint; no entry-point/module access."""
    _diagnostic_publication_effects: Callable[..., object]


def push_diagnostic_checkpoint(effects: PushDiagnosticCheckpointEffects, adapter, request, head):
    return checkpoint_adapter.push_diagnostic_checkpoint(effects._diagnostic_publication_effects(), adapter, request, head)


@dataclass(frozen=True)
class DispatchBlockedCheckpointsEffects:
    """Named effects for dispatch_blocked_checkpoints; no entry-point/module access."""
    _BlockedCheckpointAdapter: Callable[..., object]
    preserve_blocked_diagnostics: Callable[..., object] | None = None


def dispatch_blocked_checkpoints(
    effects: DispatchBlockedCheckpointsEffects, connection: sqlite3.Connection,
    repo_path: Path,
    state_dir: Path,
    repo: str,
    *,
    plan_id: str | None = None,
    skip_identities: frozenset[str] = frozenset(),
) -> int:
    """Commit prior block state, then process its network-independent request."""

    connection.commit()
    repository = blocked_checkpoint_state.BlockedCheckpointRepository(connection)
    adapter = effects._BlockedCheckpointAdapter(
        connection, repo_path, state_dir, repo
    )
    results = _BlockedCheckpointResults(connection)
    dispatcher = blocked_checkpoint.BlockedCheckpointDispatcher(
        repository,
        adapter,
        adapter,
        results,
        max_push_attempts=MAX_TRANSIENT_AGENT_RETRIES,
    )
    completed = 0
    for request in repository.pending(repo, plan_id=plan_id):
        if request.request_identity in skip_identities:
            continue
        try:
            completed += int(dispatcher._dispatch_one(request))
        except blocked_checkpoint.CheckpointPushError as error:
            repository.finish_unpublished(request, str(error), retryable=error.retryable)
            # A failed observation says nothing about requests not yet tried.
            break
        except (OSError, RuntimeError) as error:
            repository.finish_unpublished(
                request, f"checkpoint 처리 실패: {error}", retryable=True,
            )
            break
    if effects.preserve_blocked_diagnostics is not None:
        for row in connection.execute(
                "SELECT plan_id FROM blocked_checkpoints WHERE repo = ? AND phase IN ('unpublished', 'retry_pending')", (repo,)).fetchall():
            request = repository.get(repo, row['plan_id'])
            if request.request_identity not in skip_identities:
                effects.preserve_blocked_diagnostics(connection, repo_path, state_dir, request)
    return completed


@dataclass(frozen=True)
class PreserveBlockedDiagnosticsEffects:
    preservation_effects: Callable
    adapter_factory: Callable


def preserve_blocked_diagnostics(effects: PreserveBlockedDiagnosticsEffects, connection, repo_path,
                                 state_dir, request, *, project_scope=False):
    """Preservation failure is a receipt, never a new block or an approval."""
    from loop_manager_internal.recovery import diagnostic_preservation
    project_block = scoped_blocking.project_block(connection, request.repo)
    if project_block is not None and project_block['plan_id'] == request.plan_id:
        project_scope = True
    adapter = effects.adapter_factory(connection, repo_path, state_dir, request.repo,
                              project_recovery_scope=project_scope)
    row = connection.execute(
        'SELECT unpublished_reason FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
        (request.repo, request.plan_id)).fetchone()
    original_reason = (row[0] if row else None) or '원래 branch 게시 조건을 충족하지 못했습니다.'
    try:
        source = adapter.diagnostic_source(request)
        payload = diagnostic_preservation.preserve(
            effects.preservation_effects(), connection, state_dir, source, adapter, request, original_reason)
    except (OSError, RuntimeError, ValueError) as error:
        scope = 'project' if project_scope else 'plan'
        block = scoped_blocking.project_block(connection, request.repo) if project_scope else scoped_blocking.plan_block(
            connection, request.repo, request.plan_id)
        cause_identity = hashlib.sha256(json.dumps(dict(block), sort_keys=True).encode()).hexdigest() if block else ''
        payload = dict(result='local_only', diagnostic=True, original_branch=request.branch,
                       worktree=request.worktree, complete=False, omitted={'all': 'ownership or collection unavailable'},
                       cause_identity=cause_identity,
                       original_unpublished_reason=diagnostic_preservation.redact_detail(original_reason),
                       reason=diagnostic_preservation.redact_detail(str(error)))
        blocked_checkpoint_state.DiagnosticPublicationRepository(connection).preserve(
            request.repo, scope, request.plan_id, request.request_identity, payload, retryable=True)
    notifications.enqueue_current_project_events(connection, request.repo)
    return payload


def _execution_outcome_step_result(outcome: ExecutionOutcome) -> int:
    """Translate a typed execution result at the process adapter boundary."""

    if (outcome.recovery is not None
            and outcome.recovery.action is recovery_runtime.RecoveryAction.CORRECT
            and outcome.context is not None):
        reason = outcome.recovery.observation.reason
        if (
            reason is recovery_runtime.RecoveryReason.WORKER_CORRECTION
            and outcome.context.phase is ExecutionPhase.IMPLEMENTING
        ) or (
            reason is recovery_runtime.RecoveryReason.REBASE_CONFLICT
            and outcome.context.phase is not ExecutionPhase.IMPLEMENTING
        ):
            # Stop after an initial Manager rebase conflict or a Worker output
            # protocol error; the saved session receives that feedback next run.
            return 1
    if outcome.kind in {
        ExecutionOutcomeKind.SUCCESS,
        ExecutionOutcomeKind.STABLE_WAIT,
    }:
        return 0
    if outcome.kind in {
        ExecutionOutcomeKind.PHASE_ADVANCED,
        ExecutionOutcomeKind.RETRY,
    }:
        return PROJECT_STEP_RETRY
    if outcome.kind is ExecutionOutcomeKind.TRANSIENT_PAUSE:
        return PROJECT_STEP_TRANSIENT_PAUSE
    if outcome.kind is ExecutionOutcomeKind.USAGE_PAUSE:
        return PROJECT_STEP_USAGE_PAUSE
    if outcome.kind is ExecutionOutcomeKind.PLANNING_PENDING:
        return PROJECT_STEP_PLANNING_PENDING
    return 1
