"""Plan file permissions and recovery of retired approval waits."""
from __future__ import annotations
from loop_manager_internal.storage.manual_work_state import manual_workspace_owner
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.agents.scope_contracts import AgentScopeError
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import GUIDANCE_BLOCK_REASON
from loop_manager_internal.runtime.contracts import REBASE_IN_PROGRESS_ERROR
from loop_manager_internal.storage import execution_checkpoint_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import re
import sqlite3


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
class ResumeRetiredGuidanceBlockEffects:
    """Named effects for resume_retired_guidance_block; no entry-point/module access."""
    _current_rebase_service: Callable[..., object]
    _plan_block_selected_for_release: Callable[..., object]
    _rebase_in_progress_for_branch: Callable[..., object]
    _update_blocked_error: Callable[..., object]
    clear_agent_scope: Callable[..., object]
    make_worktree: Callable[..., object]
    update_project: Callable[..., object]
    validate_blocked_agent_scope: Callable[..., object]
    validate_execplan_workspace_identity: Callable[..., object]


def resume_retired_guidance_block(
    effects: ResumeRetiredGuidanceBlockEffects, connection: sqlite3.Connection,
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
        with runtime_state._transaction(connection):
            runtime_state.RuntimeStateRepository(connection).transition(
                repo, str(plan["plan_id"]),
                plan_values={"status": "IMPLEMENTING", "blocked_reason": None,
                             "blocked_fingerprint": None, "blocked_worktree_fingerprint": None,
                             "last_error": None},
                project_values={"phase": "WORKING", "last_error": None},
                enqueue=False,
            )
            scoped_blocking.clear_plan_block(connection, repo, str(plan["plan_id"]))
        if resuming_rebase:
            effects.update_project(connection, repo, last_error=REBASE_IN_PROGRESS_ERROR,
                                   validated_base_sha=None)
        effects.clear_agent_scope(connection, repo)
        return True
    except (AgentScopeError, RuntimeError) as error:
        effects._update_blocked_error(connection, repo, str(plan["plan_id"]),
                                     f"과거 대기 작업의 소유권·범위 확인 실패:\n{error}")
        return False
