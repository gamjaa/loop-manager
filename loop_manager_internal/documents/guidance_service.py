"""documents guidance service boundaries and named composition effects."""
from __future__ import annotations
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.documents.guidance_approval import GuidanceDocument
from loop_manager_internal.documents.guidance_approval import GuidanceFormatError
from loop_manager_internal.documents.guidance_approval import GuidanceProposal
from loop_manager_internal.documents.guidance_approval import parse_guidance_document
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON
from loop_manager_internal.runtime.contracts import REBASE_IN_PROGRESS_ERROR
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import stat
import subprocess


def _validate_guidance_proposal_scope(
    proposals: tuple[GuidanceProposal, ...],
    *,
    common_allowed: bool,
) -> None:
    for proposal in proposals:
        if proposal.scope == "common" and not common_allowed:
            raise AgentScopeError(
                "신규 AGENTS.md 제안은 자체 저장소도 Scope project만 허용됩니다: "
                f"{proposal.proposal_id}"
            )


def legacy_common_identities(git, repo_path: Path, revision: str, plan_path: str) -> set[str]:
    """Recognize original proposals before injection, including prepared docs.

    The common document existed as a draft before runtime injection. Its mere
    presence cannot invalidate an old pending root-AGENTS approval.
    """
    marker = "loop_manager_internal/agents/common_guidance.py"
    if not revision:
        return set()
    baseline = revision
    # Retain the migration boundary even if a later refactor moves its module.
    additions = git(repo_path, "log", "--reverse", "--format=%H", "--diff-filter=A",
                    revision, "--", marker).stdout.splitlines()
    if additions:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", additions[0]):
            raise AgentScopeError("과거 common 제안의 주입 전 기준을 확인할 수 없습니다.")
        baseline = additions[0] + "^"
    published = git(repo_path, "show", f"{baseline}:{plan_path}", check=False)
    original = parse_guidance_document(published.stdout) if published.returncode == 0 else None
    return {p.identity for p in original.proposals if p.scope == "common"} if original else set()


@dataclass(frozen=True)
class GuidanceRowsEffects:
    """Named effects for _guidance_rows; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]


def _guidance_rows(
    effects: GuidanceRowsEffects, connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[sqlite3.Row, ...]:
    effects._ensure_execplans_schema(connection)
    return tuple(
        connection.execute(
            "SELECT * FROM guidance_approvals "
            "WHERE repo = ? AND plan_id = ? ORDER BY batch_id, sequence, proposal_id",
            (repo, plan_id),
        ).fetchall()
    )


def _guidance_worker_targets(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT DISTINCT target_path FROM guidance_approvals "
        "WHERE repo = ? AND plan_id = ? AND applied = 0 "
        "AND decision != 'pending' ORDER BY target_path",
        (repo, plan_id),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


@dataclass(frozen=True)
class PublishedPlanAllowsCommonPlansEffects:
    """Named effects for _published_plan_allows_common_plans; no entry-point/module access."""
    git: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]


def _published_plan_allows_common_plans(
    effects: PublishedPlanAllowsCommonPlansEffects, repo_path: Path,
    catalog_plan: Mapping[str, object],
) -> bool:
    if not effects.is_loop_manager_repository(repo_path):
        return False
    current_revision_sha = (
        catalog_plan["current_revision_sha"]
        if "current_revision_sha" in catalog_plan.keys()
        else None
    )
    plan_sha = str(
        current_revision_sha or catalog_plan["plan_sha"] or ""
    )
    if not plan_sha:
        return False
    shown = effects.git(
        repo_path,
        "show",
        f"{plan_sha}:{catalog_plan['path']}",
        check=False,
    )
    if shown.returncode:
        return False
    acceptance = re.search(
        r"(?ms)^## Acceptance Criteria\s*\n+(.*?)(?=^## |\Z)",
        shown.stdout,
    )
    return bool(
        acceptance
        and re.search(
            r"(?im)^-\s+(?:공통\s+)?`docs/PLANS\.md`(?:에|를|을|의|와|과|\s)"
            r".*(?:변경|문서화|갱신|수정|설명|change|update|document|describe)",
            acceptance.group(1),
        )
    )


def _guidance_target_state(
    worktree: Path,
    target: str,
) -> tuple[bytes, int]:
    path = worktree / target
    try:
        target_stat = path.lstat()
    except FileNotFoundError as error:
        raise GuidanceFormatError(f"guidance 대상 파일이 없습니다: {target}") from error
    if path.is_symlink() or not path.is_file():
        raise GuidanceFormatError(
            f"guidance 대상은 일반 파일이어야 합니다: {target}"
        )
    # Git records executable permission, not local umask/group write bits.
    git_mode = 0o755 if target_stat.st_mode & stat.S_IXUSR else 0o644
    return path.read_bytes(), git_mode


def _guidance_block_message(
    plan: sqlite3.Row,
    proposals: tuple[GuidanceProposal, ...],
    detail: str,
) -> str:
    locations = []
    for proposal in proposals:
        locations.append(
            f"제안 {proposal.proposal_id}: {plan['path']}:{proposal.line}; "
            f"User Decision: {plan['path']}:{proposal.decision.line}"
        )
    return (
        f"AGENTS.md guidance 사용자 결정 대기 ({plan['plan_id']}).\n"
        f"ExecPlan: {plan['path']}\n{detail}\n" + "\n".join(locations)
    )


def _guidance_batch_id(
    repo: str,
    plan_id: str,
    document: GuidanceDocument,
    proposals: tuple[GuidanceProposal, ...],
) -> str:
    encoded = json.dumps(
        {
            "repo": repo,
            "plan_id": plan_id,
            "structure": document.structure_hash,
            "proposals": [proposal.identity for proposal in proposals],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class BlockForNewGuidanceEffects:
    """Named effects for _block_for_new_guidance; no entry-point/module access."""
    _final_block_recovery_decision: Callable[..., object]
    _guidance_batch_id: Callable[..., object]
    _guidance_block_message: Callable[..., object]
    _guidance_target_state: Callable[..., object]
    _validate_guidance_proposal_scope: Callable[..., object]
    apply_patch_to_content: Callable[..., object]
    capture_git_snapshot: Callable[..., object]
    guidance_patch_hash: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    git: Callable[..., object]


def _block_for_new_guidance(
    effects: BlockForNewGuidanceEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    document: GuidanceDocument,
    new_proposals: tuple[GuidanceProposal, ...],
    worker_result: str,
) -> None:
    if not new_proposals:
        raise RuntimeError("차단할 새 guidance 제안이 없습니다.")
    # A saved/published pre-migration pending proposal remains bound to its
    # original root target. A Worker cannot introduce another common proposal.
    revision = str((plan['current_revision_sha'] if 'current_revision_sha' in plan.keys() else None)
                   or plan['plan_sha'] or '')
    is_self = effects.is_loop_manager_repository(repo_path)
    legacy = (legacy_common_identities(effects.git, repo_path, revision, str(plan['path']))
              if is_self and any(p.scope == 'common' for p in new_proposals) else set())
    for proposal in new_proposals:
        effects._validate_guidance_proposal_scope(
            (proposal,), common_allowed=(is_self and proposal.identity in legacy),
        )
    for proposal in new_proposals:
        if proposal.decision.decision != "pending":
            raise AgentScopeError(
                "Worker가 새 guidance 제안의 User Decision을 미리 확정할 수 없습니다: "
                f"{proposal.proposal_id}"
            )

    batch_id = effects._guidance_batch_id(
        repo,
        str(plan["plan_id"]),
        document,
        new_proposals,
    )
    baselines: dict[str, tuple[bytes, int]] = {}
    for proposal in new_proposals:
        if proposal.target not in baselines:
            baselines[proposal.target] = effects._guidance_target_state(
                worktree,
                proposal.target,
            )
    for proposal in new_proposals:
        baseline, _baseline_mode = baselines[proposal.target]
        effects.apply_patch_to_content(
            baseline,
            proposal.target,
            proposal.patch,
        )
    message = effects._guidance_block_message(
        plan,
        new_proposals,
        "각 User Decision의 Decision과 Revision을 사용자가 직접 확정해야 합니다.",
    )
    result_snapshot = effects.capture_git_snapshot(worktree).serialize()
    with connection:
        for sequence, proposal in enumerate(new_proposals):
            baseline, baseline_mode = baselines[proposal.target]
            connection.execute(
                """
                INSERT INTO guidance_approvals (
                    repo, plan_id, proposal_id, proposal_hash, target_path,
                    scope, proposal_patch, proposal_patch_hash,
                    proposal_line, decision_line, batch_id, sequence,
                    baseline_content, baseline_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo,
                    plan["plan_id"],
                    proposal.proposal_id,
                    proposal.identity,
                    proposal.target,
                    proposal.scope,
                    proposal.patch,
                    effects.guidance_patch_hash(proposal.target, proposal.patch),
                    proposal.line,
                    proposal.decision.line,
                    batch_id,
                    sequence,
                    baseline,
                    baseline_mode,
                ),
            )
        effects._final_block_recovery_decision(
            connection, repo, plan, "WORKING",
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED, message,
            document.structure_hash, None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=GUIDANCE_BLOCK_REASON,
            evidence=message,
            release_fingerprint=document.structure_hash,
            plan_values={
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "guidance_plan_hash": document.structure_hash,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
            },
            project_values={
                "phase": "PLAN_BLOCKED",
                "last_error": message,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
            },
        )


def _guidance_document_matches_rows(
    document: GuidanceDocument,
    rows: tuple[sqlite3.Row, ...],
    *,
    require_decisions: bool,
) -> tuple[dict[str, GuidanceProposal], tuple[GuidanceProposal, ...]]:
    proposals = {proposal.proposal_id: proposal for proposal in document.proposals}
    stored = {str(row["proposal_id"]): row for row in rows}
    missing = sorted(set(stored) - set(proposals))
    if missing:
        raise AgentScopeError(
            "기존 guidance 제안은 삭제할 수 없습니다: " + ", ".join(missing)
        )
    for proposal_id, row in stored.items():
        proposal = proposals[proposal_id]
        if proposal.identity != str(row["proposal_hash"]):
            raise AgentScopeError(
                f"기존 guidance 제안은 수정할 수 없습니다: {proposal_id}"
            )
        decision_is_immutable = require_decisions or bool(row["applied"])
        if decision_is_immutable and row["decision_hash"]:
            if proposal.decision.identity != str(row["decision_hash"]):
                raise AgentScopeError(
                    "Worker는 확정된 guidance User Decision을 수정할 수 없습니다: "
                    f"{proposal_id}"
                )
    new = tuple(
        proposal
        for proposal in document.proposals
        if proposal.proposal_id not in stored
    )
    return proposals, new


@dataclass(frozen=True)
class GuidanceFinalExpectedEffects:
    """Named effects for _guidance_final_expected; no entry-point/module access."""
    apply_patch_to_content: Callable[..., object]
    finalized_patch: Callable[..., object]
    guidance_content_hash: Callable[..., object]


def _guidance_final_expected(
    effects: GuidanceFinalExpectedEffects, rows: tuple[sqlite3.Row, ...],
    proposals: Mapping[str, GuidanceProposal],
) -> tuple[
    dict[tuple[str, str], tuple[bytes, int]],
    dict[str, str],
]:
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(
            (str(row["batch_id"]), str(row["target_path"])),
            [],
        ).append(row)
    expected: dict[tuple[str, str], tuple[bytes, int]] = {}
    final_hashes: dict[str, str] = {}
    for key, group in grouped.items():
        ordered = sorted(group, key=lambda row: int(row["sequence"]))
        contents = bytes(ordered[0]["baseline_content"])
        mode = int(ordered[0]["baseline_mode"])
        for row in ordered:
            proposal = proposals[str(row["proposal_id"])]
            patch = effects.finalized_patch(proposal)
            if patch is not None:
                contents = effects.apply_patch_to_content(
                    contents,
                    proposal.target,
                    patch,
                )
        expected[key] = (contents, mode)
        final_hash = effects.guidance_content_hash(contents)
        for row in ordered:
            final_hashes[str(row["proposal_id"])] = final_hash
    return expected, final_hashes


@dataclass(frozen=True)
class PlanBlockSelectedForReleaseEffects:
    """Named effects for _plan_block_selected_for_release; no entry-point/module access."""
    hydrate_execplan_project: Callable[..., object]
    project_execplan: Callable[..., object]


def _plan_block_selected_for_release(
    effects: PlanBlockSelectedForReleaseEffects, connection: sqlite3.Connection,
    repo: str,
    *,
    blocked_reason: str | None = None,
    require_fingerprint: bool = False,
    plan_id: str | None = None,
) -> sqlite3.Row | None:
    """Return the mirrored block, or hydrate one uniquely matching block."""

    def matches(candidate: sqlite3.Row | None) -> bool:
        return bool(
            candidate is not None
            and candidate["status"] == "PLAN_BLOCKED"
            and (blocked_reason is None or candidate["blocked_reason"] == blocked_reason)
            and (not require_fingerprint or candidate["blocked_fingerprint"])
        )

    if plan_id is not None:
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        return plan if matches(plan) else None
    plan = effects.project_execplan(connection, repo)
    if matches(plan):
        return plan
    clauses = ["repo = ?", "status = 'PLAN_BLOCKED'"]
    parameters: list[object] = [repo]
    if blocked_reason is not None:
        clauses.append("blocked_reason = ?")
        parameters.append(blocked_reason)
    if require_fingerprint:
        clauses.append("blocked_fingerprint IS NOT NULL")
    rows = connection.execute(
        "SELECT * FROM exec_plans WHERE " + " AND ".join(clauses) + " ORDER BY plan_id",
        parameters,
    ).fetchall()
    if len(rows) != 1:
        return None
    effects.hydrate_execplan_project(connection, repo, rows[0])
    return rows[0]


@dataclass(frozen=True)
class ResumeGuidanceApprovalIfDecidedEffects:
    """Named effects for resume_guidance_approval_if_decided; no entry-point/module access."""
    _current_rebase_service: Callable[..., object]
    _guidance_block_message: Callable[..., object]
    _guidance_document: Callable[..., object]
    _guidance_document_matches_rows: Callable[..., object]
    _guidance_final_expected: Callable[..., object]
    _guidance_rows: Callable[..., object]
    _guidance_target_state: Callable[..., object]
    _plan_block_selected_for_release: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    _validate_guidance_proposal_scope: Callable[..., object]
    clear_agent_scope: Callable[..., object]
    finalized_patch: Callable[..., object]
    guidance_patch_hash: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]
    make_worktree: Callable[..., object]
    update_project: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def resume_guidance_approval_if_decided(
    effects: ResumeGuidanceApprovalIfDecidedEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base: str,
    state_dir: Path,
    *, plan_id: str | None = None,
) -> bool:
    plan = effects._plan_block_selected_for_release(
        connection,
        repo,
        blocked_reason=GUIDANCE_BLOCK_REASON,
        plan_id=plan_id,
    )
    if plan is None:
        return False
    worktree = Path(str(plan["worktree"] or ""))
    branch = str(plan["branch"] or "")
    resuming_rebase = effects._rebase_in_progress_for_branch(worktree, branch)
    try:
        effects.validate_execplan_workspace_identity(
            state_dir,
            repo,
            manual_workspace_owner(connection, repo, str(plan["plan_id"])),
            branch,
            worktree,
        )
        effects.make_worktree(
            repo_path,
            worktree,
            branch,
            base,
            journal_state_dir=state_dir,
            journal_owner=f"plan:{plan['plan_id']}",
            allow_create_branch=False,
            allow_recovery=True,
            allow_existing=True,
            allow_rebase=resuming_rebase,
        )
        checkpoint = execution_checkpoint_state.ExecutionCheckpointRepository(connection).get(
            repo, str(plan['plan_id']))
        accepted_rebase = bool((checkpoint.snapshot or {}).get('current_rebase_acceptance'))
        if accepted_rebase:
            _, decision = effects._current_rebase_service(
                connection, repo_path, state_dir, repo, str(plan['plan_id']),
                worktree, branch).require_owned(checkpoint=checkpoint)
            if checkpoint.operation is not None or decision.status.value != 'complete':
                return False
        effects.validate_blocked_agent_scope(
            connection,
            repo_path,
            repo,
            base,
            worktree,
            str(plan["path"]),
            accepted_rebase=accepted_rebase, saved_plan=plan,
        )
        document = effects._guidance_document(worktree, str(plan["path"]))
        if document is None:
            raise AgentScopeError("저장된 guidance 제안이 ExecPlan에서 사라졌습니다.")
        if document.structure_hash != str(plan["guidance_plan_hash"] or ""):
            raise AgentScopeError(
                "User Decision 이외의 ExecPlan 변경으로 guidance 승인을 해제할 수 없습니다."
            )
        rows = effects._guidance_rows(connection, repo, str(plan["plan_id"]))
        proposals, new = effects._guidance_document_matches_rows(
            document,
            rows,
            require_decisions=False,
        )
        if new:
            raise AgentScopeError(
                "사용자 결정 대기 중 새 guidance 제안을 추가할 수 없습니다."
            )
        effects._validate_guidance_proposal_scope(
            document.proposals,
            common_allowed=effects.is_loop_manager_repository(repo_path),
        )
        pending_rows = tuple(row for row in rows if not int(row["applied"]))
        for row in pending_rows:
            proposal = proposals[str(row["proposal_id"])]
            if proposal.decision.decision == "pending":
                return False
            invalidated = str(row["invalidated_decision_hash"] or "")
            if invalidated and proposal.decision.identity == invalidated:
                effects._update_blocked_error(
                    connection,
                    repo,
                    str(plan["plan_id"]),
                    effects._guidance_block_message(
                        plan,
                        (proposal,),
                        "무효화된 결정과 다른 Revision 또는 대체 patch가 필요합니다.",
                    ),
                )
                return False
        _expected, final_hashes = effects._guidance_final_expected(
            pending_rows,
            proposals,
        )
        checked_targets: set[str] = set()
        for row in pending_rows:
            target = str(row["target_path"])
            if target in checked_targets:
                continue
            checked_targets.add(target)
            try:
                current_contents, current_mode = effects._guidance_target_state(worktree, target)
            except GuidanceFormatError:
                if row["invalidated_decision_hash"]:
                    continue
                raise
            if (
                not row["invalidated_decision_hash"]
                and (
                    current_contents != bytes(row["baseline_content"])
                    or current_mode != int(row["baseline_mode"])
                )
            ):
                raise AgentScopeError(
                    f"승인 전 guidance 대상이 변경됐습니다: {target}"
                )
        with connection:
            for row in pending_rows:
                proposal = proposals[str(row["proposal_id"])]
                patch = effects.finalized_patch(proposal)
                connection.execute(
                    """
                    UPDATE guidance_approvals
                    SET decision = ?, decision_revision = ?,
                        decision_hash = ?, approved_patch = ?,
                        approved_patch_hash = ?, expected_content_hash = ?,
                        invalidated_decision_hash = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE repo = ? AND plan_id = ? AND proposal_id = ?
                    """,
                    (
                        proposal.decision.decision,
                        proposal.decision.revision,
                        proposal.decision.identity,
                        patch,
                        effects.guidance_patch_hash(proposal.target, patch or ""),
                        final_hashes[proposal.proposal_id],
                        repo,
                        plan["plan_id"],
                        proposal.proposal_id,
                    ),
                )
            runtime_state.RuntimeStateRepository(connection).transition(
                repo,
                str(plan["plan_id"]),
                plan_values={
                    "status": "IMPLEMENTING",
                    "blocked_reason": None,
                    "blocked_fingerprint": None,
                    "blocked_worktree_fingerprint": None,
                    "last_error": None,
                },
                project_values={
                    "phase": "WORKING",
                    "last_error": None,
                },
                enqueue=False,
            )
            scoped_blocking.clear_plan_block(
                connection, repo, str(plan["plan_id"])
            )
        if resuming_rebase:
            effects.update_project(
                connection, repo, last_error=REBASE_IN_PROGRESS_ERROR,
                validated_base_sha=None,
            )
        effects.clear_agent_scope(connection, repo)
        return True
    except (AgentScopeError, GuidanceFormatError, RuntimeError) as error:
        effects._update_blocked_error(
            connection,
            repo,
            str(plan["plan_id"]),
            f"guidance 승인 해제 실패:\n{error}",
        )
        return False


@dataclass(frozen=True)
class InvalidateGuidanceApprovalEffects:
    """Named effects for _invalidate_guidance_approval; no entry-point/module access."""
    _final_block_recovery_decision: Callable[..., object]
    _guidance_block_message: Callable[..., object]
    capture_git_snapshot: Callable[..., object]


def _invalidate_guidance_approval(
    effects: InvalidateGuidanceApprovalEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    document: GuidanceDocument,
    proposals: tuple[GuidanceProposal, ...],
    worker_result: str,
    detail: str,
) -> None:
    message = effects._guidance_block_message(plan, proposals, detail)
    result_snapshot = effects.capture_git_snapshot(worktree).serialize()
    proposal_ids = tuple(proposal.proposal_id for proposal in proposals)
    placeholders = ", ".join("?" for _ in proposal_ids)
    with connection:
        connection.execute(
            f"""
            UPDATE guidance_approvals
            SET invalidated_decision_hash = decision_hash,
                applied = 0, updated_at = CURRENT_TIMESTAMP
            WHERE repo = ? AND plan_id = ?
              AND proposal_id IN ({placeholders})
            """,
            (repo, plan["plan_id"], *proposal_ids),
        )
        # Manager rebase/verify may already have cleared the Agent scope.
        # Freeze the diagnosed state so only the User Decision can change.
        effects._final_block_recovery_decision(
            connection, repo, plan, "WORKING",
            BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED, message,
            document.structure_hash, None,
        )
        runtime_state.RuntimeStateRepository(connection).block_plan(
            repo,
            str(plan["plan_id"]),
            reason_code=GUIDANCE_BLOCK_REASON,
            evidence=message,
            release_fingerprint=document.structure_hash,
            plan_values={
                "blocked_fingerprint": None,
                "blocked_worktree_fingerprint": None,
                "guidance_plan_hash": document.structure_hash,
                "worker_result": worker_result,
                "agent_result_snapshot": result_snapshot,
                "agent_manager_snapshot": None,
                "agent_role": "worker",
                "agent_snapshot": result_snapshot,
                "agent_baseline": None,
                "agent_recovery": None,
                "agent_input_plan": None,
                "agent_result_plan": None,
                "agent_input_fingerprint": None,
            },
            clear_projection=True,
        )


@dataclass(frozen=True)
class InspectWorkerGuidanceResultEffects:
    """Named effects for inspect_worker_guidance_result; no entry-point/module access."""
    _block_for_new_guidance: Callable[..., object]
    _guidance_document: Callable[..., object]
    _guidance_document_matches_rows: Callable[..., object]
    _guidance_final_expected: Callable[..., object]
    _guidance_rows: Callable[..., object]
    _guidance_target_state: Callable[..., object]
    _invalidate_guidance_approval: Callable[..., object]
    _validate_guidance_proposal_scope: Callable[..., object]
    is_loop_manager_repository: Callable[..., object]


def inspect_worker_guidance_result(
    effects: InspectWorkerGuidanceResultEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    worker_result: str,
) -> bool:
    """Validate approved targets, then block on any newly proposed guidance."""

    document = effects._guidance_document(worktree, str(plan["path"]))
    rows = effects._guidance_rows(connection, repo, str(plan["plan_id"]))
    if document is None:
        if rows:
            raise AgentScopeError("기존 guidance 제안이 ExecPlan에서 사라졌습니다.")
        return False
    effects._validate_guidance_proposal_scope(
        document.proposals,
        common_allowed=effects.is_loop_manager_repository(repo_path),
    )
    proposals, new = effects._guidance_document_matches_rows(
        document,
        rows,
        require_decisions=True,
    )
    unapplied = tuple(row for row in rows if not int(row["applied"]))
    if unapplied:
        expected, _final_hashes = effects._guidance_final_expected(
            unapplied,
            proposals,
        )
        mismatched: list[GuidanceProposal] = []
        for (_batch_id, target), (expected_contents, expected_mode) in expected.items():
            try:
                actual_contents, actual_mode = effects._guidance_target_state(worktree, target)
                exact = actual_contents == expected_contents and actual_mode == expected_mode
            except GuidanceFormatError:
                exact = False
            if not exact:
                mismatched.extend(
                    proposals[str(row["proposal_id"])]
                    for row in unapplied
                    if str(row["target_path"]) == target
                )
        if mismatched:
            effects._invalidate_guidance_approval(
                connection,
                repo,
                worktree,
                plan,
                document,
                tuple(mismatched),
                worker_result,
                "실제 AGENTS.md 결과가 승인된 patch 결과와 다릅니다. "
                "User Decision Revision을 갱신해 다시 결정해야 합니다.",
            )
            return True
        with connection:
            connection.executemany(
                """
                UPDATE guidance_approvals
                SET applied = 1, updated_at = CURRENT_TIMESTAMP
                WHERE repo = ? AND plan_id = ? AND proposal_id = ?
                """,
                (
                    (repo, plan["plan_id"], str(row["proposal_id"]))
                    for row in unapplied
                ),
            )
    if new:
        effects._block_for_new_guidance(
            connection,
            repo_path,
            repo,
            worktree,
            plan,
            document,
            new,
            worker_result,
        )
        return True
    runtime_state.RuntimeStateRepository(connection).update_plan(
        repo,
        str(plan["plan_id"]),
        plan_values={"guidance_plan_hash": document.structure_hash},
        enqueue=False,
    )
    return False


@dataclass(frozen=True)
class ValidateGuidancePublicationEffects:
    """Named effects for validate_guidance_publication; no entry-point/module access."""
    _guidance_document: Callable[..., object]
    _guidance_document_matches_rows: Callable[..., object]
    _guidance_target_state: Callable[..., object]
    _invalidate_guidance_approval: Callable[..., object]
    git: Callable[..., object]
    guidance_content_hash: Callable[..., object]


def validate_guidance_publication(
    effects: ValidateGuidancePublicationEffects, connection: sqlite3.Connection,
    repo: str,
    worktree: Path,
    plan: sqlite3.Row,
    worker_result: str,
    *,
    previous_base: str | None = None,
    latest_base: str | None = None,
) -> bool:
    """Revalidate the latest approval per target, including after Manager rebase."""

    rows = tuple(connection.execute(
        "SELECT * FROM guidance_approvals WHERE repo = ? AND plan_id = ? "
        "ORDER BY rowid",
        (repo, plan["plan_id"]),
    ).fetchall())
    if not rows:
        return False
    document = effects._guidance_document(worktree, str(plan["path"]))
    if document is None:
        raise AgentScopeError("게시할 ExecPlan에 guidance 결정이 없습니다.")
    proposals, new = effects._guidance_document_matches_rows(
        document, rows, require_decisions=True
    )
    if new:
        raise AgentScopeError("게시 전에 승인되지 않은 guidance 제안이 있습니다.")
    latest_batches = {str(row["target_path"]): str(row["batch_id"]) for row in rows}
    selected = tuple(
        row for row in rows
        if str(row["batch_id"]) == latest_batches[str(row["target_path"])]
    )
    mismatched = []
    new_baselines: dict[str, tuple[bytes, int]] = {}
    for target in latest_batches:
        target_rows = tuple(row for row in selected if row["target_path"] == target)
        last = target_rows[-1]
        try:
            contents, mode = effects._guidance_target_state(worktree, target)
            exact = (
                all(int(row["applied"]) for row in target_rows)
                and effects.guidance_content_hash(contents) == last["expected_content_hash"]
                and mode == int(last["baseline_mode"])
            )
        except GuidanceFormatError:
            exact = False
        if previous_base and latest_base:
            old = effects.git(worktree, "rev-parse", f"{previous_base}:{target}", check=False)
            new_blob = effects.git(worktree, "rev-parse", f"{latest_base}:{target}", check=False)
            old_mode = effects.git(worktree, "ls-tree", previous_base, "--", target).stdout
            new_mode = effects.git(worktree, "ls-tree", latest_base, "--", target).stdout
            if old.returncode or new_blob.returncode or old_mode != new_mode:
                # Reapproval during a paused rebase already binds the decision
                # to this base. Do not invalidate it again after --continue.
                base_approved = False
                shown = subprocess.run(
                    ["git", "show", f"{latest_base}:{target}"],
                    cwd=worktree, check=False, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if shown.returncode == 0 and new_mode.startswith(("100644 ", "100755 ")):
                    new_baselines[target] = (
                        shown.stdout,
                        0o755 if new_mode.startswith("100755 ") else 0o644,
                    )
                    base_approved = all(
                        bytes(item["baseline_content"]) == shown.stdout
                        and int(item["baseline_mode"]) == new_baselines[target][1]
                        for item in target_rows
                    )
                exact = exact and base_approved
        if not exact:
            mismatched.extend(proposals[str(row["proposal_id"])] for row in target_rows)
    if not mismatched:
        return False
    effects._invalidate_guidance_approval(
        connection, repo, worktree, plan, document, tuple(mismatched),
        worker_result,
        "최신 base 또는 게시할 AGENTS.md가 승인 기준과 다릅니다. "
        "새 기준에 적용할 정확한 대체 patch와 User Decision Revision이 필요합니다.",
    )
    with connection:
        for target, (contents, mode) in new_baselines.items():
            connection.execute(
                "UPDATE guidance_approvals SET baseline_content = ?, baseline_mode = ? "
                "WHERE repo = ? AND plan_id = ? AND target_path = ? AND batch_id = ?",
                (contents, mode, repo, plan["plan_id"], target, latest_batches[target]),
            )
    return True


@dataclass(frozen=True)
class GuidanceResumeContextEffects:
    """Named effects for guidance_resume_context; no entry-point/module access."""
    _guidance_rows: Callable[..., object]


def guidance_resume_context(
    effects: GuidanceResumeContextEffects, connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
) -> str:
    rows = tuple(
        row
        for row in effects._guidance_rows(connection, repo, plan_id)
        if not int(row["applied"])
    )
    if not rows:
        return ""
    payload = [
        {
            "proposal_id": str(row["proposal_id"]),
            "target": str(row["target_path"]),
            "scope": str(row["scope"]),
            "decision": str(row["decision"]),
            "revision": str(row["decision_revision"]),
            "patch": str(row["approved_patch"] or ""),
            "patch_hash": str(row["approved_patch_hash"] or ""),
            "expected_content_hash": str(row["expected_content_hash"] or ""),
        }
        for row in rows
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)
