"""Structured plan/project blocking state and persistence boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import sqlite3
import hashlib
import json

from loop_manager_internal.storage import runtime_state


class BlockScope(str, Enum):
    PLAN = "plan"
    PROJECT = "project"


class BlockReasonCode(str, Enum):
    """Code-owned reasons whose scope must never be inferred from prose."""

    VERIFY_RETRY_EXHAUSTED = "verify_retry_exhausted"
    VERIFY_TIMEOUT_LIMIT_INSUFFICIENT = "verify_timeout_limit_insufficient"
    REVIEW_RETRY_EXHAUSTED = "review_retry_exhausted"
    REVIEW_PROTOCOL_EXHAUSTED = "review_protocol_exhausted"
    REVIEW_SPECIFICATION_DEADLOCK = "review_specification_deadlock"
    GUIDANCE_APPROVAL_REQUIRED = "guidance_approval_required"
    USER_DECISION_REQUIRED = "user_decision_required"
    COMMON_DEFECT_PENDING = "common_defect_pending"
    PULL_REQUEST_CLOSED = "pull_request_closed"
    MANAGED_WORKTREE_CONFLICT = "managed_worktree_conflict"
    MANAGED_WORKTREE_SCOPE_VIOLATION = "managed_worktree_scope_violation"
    PLAN_INPUT_INVALID = "plan_input_invalid"
    WORKER_OUTPUT_INVALID = "worker_output_invalid"
    REMOTE_BRANCH_DELETED = "remote_branch_deleted"
    WORKSPACE_OWNERSHIP_LOCAL = "workspace_ownership_local"

    DEFAULT_WORKTREE_MUTATION = "default_worktree_mutation"
    SHARED_REF_MUTATION = "shared_ref_mutation"
    TURN_DIFF_REF_MUTATION = "turn_diff_ref_mutation"
    OUTSIDE_MANAGED_WORKTREE_MUTATION = "outside_managed_worktree_mutation"
    SHARED_GIT_CONFIG_MUTATION = "shared_git_config_mutation"
    REPOSITORY_STATE_UNSAFE = "repository_state_unsafe"
    WORKSPACE_OWNERSHIP_UNCERTAIN = "workspace_ownership_uncertain"
    PUBLICATION_OWNERSHIP_UNCERTAIN = "publication_ownership_uncertain"
    EXECPLAN_CATALOG_INVALID = "execplan_catalog_invalid"
    LEGACY_UNSCOPED_BLOCK = "legacy_unscoped_block"


PR_CLOSED_BLOCK_REASON = BlockReasonCode.PULL_REQUEST_CLOSED.value
GUIDANCE_BLOCK_REASON = BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED.value


_PLAN_REASONS = frozenset(
    {
        BlockReasonCode.VERIFY_RETRY_EXHAUSTED,
        BlockReasonCode.VERIFY_TIMEOUT_LIMIT_INSUFFICIENT,
        BlockReasonCode.REVIEW_RETRY_EXHAUSTED,
        BlockReasonCode.REVIEW_PROTOCOL_EXHAUSTED,
        BlockReasonCode.REVIEW_SPECIFICATION_DEADLOCK,
        BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED,
        BlockReasonCode.USER_DECISION_REQUIRED,
        BlockReasonCode.COMMON_DEFECT_PENDING,
        BlockReasonCode.PULL_REQUEST_CLOSED,
        BlockReasonCode.MANAGED_WORKTREE_CONFLICT,
        BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION,
        BlockReasonCode.PLAN_INPUT_INVALID,
        BlockReasonCode.WORKER_OUTPUT_INVALID,
        BlockReasonCode.REMOTE_BRANCH_DELETED,
        BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL,
    }
)

# Retired Git access diagnoses are rechecked against current operation conditions.
SCOPE_BASELINE_REASONS = frozenset({
    BlockReasonCode.DEFAULT_WORKTREE_MUTATION.value,
    BlockReasonCode.SHARED_REF_MUTATION.value,
    BlockReasonCode.TURN_DIFF_REF_MUTATION.value,
    BlockReasonCode.SHARED_GIT_CONFIG_MUTATION.value,
    BlockReasonCode.OUTSIDE_MANAGED_WORKTREE_MUTATION.value,
    BlockReasonCode.MANAGED_WORKTREE_SCOPE_VIOLATION.value,
})


def is_scope_baseline_block(block) -> bool:
    return block is not None and (block['reason_code'] in SCOPE_BASELINE_REASONS
        or block['reason_code'] in {BlockReasonCode.REPOSITORY_STATE_UNSAFE.value,
                                   BlockReasonCode.LEGACY_UNSCOPED_BLOCK.value}
        and str(block['evidence'] or '').startswith('Agent 범위 선검증 실패:'))


def reason_scope(reason: BlockReasonCode) -> BlockScope:
    """Return the single allowed scope for a code-owned reason."""

    return BlockScope.PLAN if reason in _PLAN_REASONS else BlockScope.PROJECT


PLAN_BLOCKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_blocks (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope = 'plan'),
    reason_code TEXT NOT NULL,
    evidence TEXT NOT NULL,
    release_fingerprint TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    recovery_snapshot TEXT,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo, plan_id) REFERENCES exec_plans(repo, plan_id)
)
"""


PROJECT_BLOCKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_blocks (
    repo TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope = 'project'),
    reason_code TEXT NOT NULL,
    plan_id TEXT,
    evidence TEXT NOT NULL,
    release_fingerprint TEXT,
    previous_phase TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


@dataclass(frozen=True)
class BlockRecord:
    scope: BlockScope
    reason: BlockReasonCode
    repo: str
    plan_id: str | None
    evidence: str
    release_fingerprint: str | None
    previous_phase: str | None = None


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(PLAN_BLOCKS_SCHEMA)
    if not any(row[1] == 'recovery_snapshot' for row in connection.execute('PRAGMA table_info(execplan_blocks)')):
        connection.execute('ALTER TABLE execplan_blocks ADD COLUMN recovery_snapshot TEXT')
    connection.execute(PROJECT_BLOCKS_SCHEMA)


def _reason(value: object) -> BlockReasonCode:
    try:
        return BlockReasonCode(str(value))
    except ValueError as error:
        raise RuntimeError(f"알 수 없는 구조화된 차단 원인입니다: {value}") from error


def catalog_held_plan_ids(connection: sqlite3.Connection, repo: str) -> frozenset[str]:
    """Catalog and unresolved completion scopes never replace execution blocks."""
    held = set()
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_catalog_holds'"
    ).fetchone():
        held.update(str(row[0]) for row in connection.execute(
            "SELECT plan_id FROM execplan_catalog_holds WHERE repo = ?", (repo,),
        ))
    from loop_manager_internal.storage.execution_checkpoint_state import pending_completion_plan_ids
    pending = set(pending_completion_plan_ids(connection, repo))
    dependencies = [(str(row[0]), json.loads(row[1])) for row in connection.execute(
        "SELECT plan_id,depends_on FROM exec_plans WHERE repo=? AND status<>'MERGED'", (repo,),
    )] if pending else ()
    # ponytail: repeated scans are quadratic in dependency depth; use a reverse
    # adjacency queue if large pending catalog graphs make this boundary costly.
    while True:
        dependent = {key for key, requires in dependencies if pending.intersection(requires)} - pending
        if not dependent:
            break
        pending.update(dependent)
    held.update(pending)
    return frozenset(held)


def plan_blocks(connection: sqlite3.Connection, repo: str) -> tuple[sqlite3.Row, ...]:
    return tuple(
        connection.execute(
            "SELECT b.*, p.* FROM execplan_blocks AS b "
            "JOIN exec_plans AS p USING (repo, plan_id) "
            "WHERE b.repo = ? ORDER BY p.plan_id",
            (repo,),
        ).fetchall()
    )


def plan_block(
    connection: sqlite3.Connection, repo: str, plan_id: str,
) -> sqlite3.Row | None:
    """Read the current structured cause for one blocked plan."""

    return connection.execute(
        "SELECT * FROM execplan_blocks WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    ).fetchone()


def workspace_held_resources(connection, repo):
    """Return residual workspace owners, including a hold beside another cause."""
    ids = {row[0] for row in connection.execute(
        "SELECT plan_id FROM execplan_blocks WHERE repo=? AND reason_code=?",
        (repo, BlockReasonCode.WORKSPACE_OWNERSHIP_LOCAL.value))}
    ids.update(row[0] for row in connection.execute(
        'SELECT plan_id FROM execution_checkpoints WHERE repo=? AND workspace_ownership_held=1', (repo,)))
    rows = connection.execute('SELECT plan_id,status,workspace_owned,branch,worktree FROM exec_plans WHERE repo=?', (repo,))
    return tuple(dict(row) for row in rows if row['plan_id'] in ids
                 or row['workspace_owned'] and row['status'] in {'IMPLEMENTING','VERIFYING','REVIEWING','PUBLISHING','PLAN_BLOCKED','PR_OPEN'})


def workspace_resource_conflicts(owners, *, plan_id, branch, worktree):
    return tuple(owner for owner in owners if owner['plan_id'] == plan_id
                 or branch and branch == owner['branch']
                 or worktree and owner['worktree']
                 and Path(worktree).resolve() == Path(owner['worktree']).resolve())


def project_block(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM project_blocks WHERE repo = ?", (repo,)
    ).fetchone()


def project_cause_identity(block) -> str:
    return hashlib.sha256(json.dumps(
        {key: block[key] for key in ('repo', 'plan_id', 'reason_code', 'evidence',
                                    'release_fingerprint', 'created_at')},
        sort_keys=True, separators=(',', ':'),
    ).encode()).hexdigest()


def project_cause_matches(connection, expected) -> bool:
    """Recovery can revalidate only the original, still-current cause."""
    current = project_block(connection, expected['repo'])
    return current is not None and dict(current) == dict(expected)


def record_plan_block(
    connection: sqlite3.Connection,
    repo: str,
    plan_id: str,
    reason: BlockReasonCode,
    evidence: str,
    release_fingerprint: str | None,
) -> None:
    if reason_scope(reason) is not BlockScope.PLAN:
        raise ValueError(f"프로젝트 범위 원인을 계획 차단에 사용할 수 없습니다: {reason.value}")
    from loop_manager_internal.recovery import recovery_runtime
    from loop_manager_internal.storage import recovery_state

    try:
        with runtime_state._transaction(connection):
            plan = connection.execute(
                "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                (repo, plan_id),
            ).fetchone()
            if plan is None:
                raise runtime_state.RuntimeStateError("plan missing")
            observation = recovery_runtime.RecoveryObservation(
                reason=recovery_runtime.reason_from_block(reason),
                repo=repo, plan_id=plan_id, phase=str(plan["status"]),
                plan_revision=str(plan["current_revision_sha"] or plan["plan_sha"] or ""),
                base_revision=str(plan["active_sha"] or ""),
                fingerprint=release_fingerprint or hashlib.sha256(
                    f"{reason.value}|{plan_id}".encode()
                ).hexdigest(),
                progress_marker=str(plan["active_sha"] or ""), detail=evidence,
                user_targets=recovery_state.block_user_targets(
                    connection, repo, plan_id, reason, str(plan["path"]),
                ),
            )
            decision = recovery_state.RecoveryRepository(connection).observe(
                observation, final_block_reason=reason,
            ).decision
            recovery_runtime.require_final(decision)
            runtime_state.RuntimeStateRepository(connection).block_plan(
                repo,
                plan_id,
                reason_code=reason.value,
                evidence=evidence,
                release_fingerprint=release_fingerprint,
                enqueue=False,
            )
    except runtime_state.RuntimeStateError as error:
        raise RuntimeError(
            f"차단할 ExecPlan을 찾을 수 없습니다: {plan_id}"
        ) from error


def record_project_block(
    connection: sqlite3.Connection,
    repo: str,
    reason: BlockReasonCode,
    evidence: str,
    release_fingerprint: str | None,
    *,
    plan_id: str | None,
    previous_phase: str | None,
    recovery_decision: object | None = None,
) -> None:
    with runtime_state._transaction(connection):
        _record_project_block_in_transaction(
            connection, repo, reason, evidence, release_fingerprint,
            plan_id=plan_id, previous_phase=previous_phase,
            recovery_decision=recovery_decision,
        )


def _record_project_block_in_transaction(
    connection: sqlite3.Connection,
    repo: str,
    reason: BlockReasonCode,
    evidence: str,
    release_fingerprint: str | None,
    *,
    plan_id: str | None,
    previous_phase: str | None,
    recovery_decision: object | None = None,
) -> None:
    if reason_scope(reason) is not BlockScope.PROJECT:
        raise ValueError(f"계획 범위 원인을 프로젝트 차단에 사용할 수 없습니다: {reason.value}")
    project = connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        raise RuntimeError(f"차단할 프로젝트를 찾을 수 없습니다: {repo}")
    # Import locally: the recovery policy uses this module's scope mapping.
    from loop_manager_internal.recovery import recovery_runtime
    from loop_manager_internal.storage import recovery_state

    if recovery_decision is None:
        project_state = connection.execute(
            "SELECT phase, active_sha FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        plan = connection.execute(
            "SELECT current_revision_sha, plan_sha FROM exec_plans "
            "WHERE repo = ? AND plan_id = ?", (repo, plan_id),
        ).fetchone() if plan_id is not None else None
        fingerprint = release_fingerprint or hashlib.sha256(
            f"{reason.value}|{plan_id}|{previous_phase}".encode()
        ).hexdigest()
        observation = recovery_runtime.RecoveryObservation(
            reason=recovery_runtime.reason_from_block(reason), repo=repo,
            plan_id=plan_id, phase=str(project_state["phase"] or ""),
            plan_revision=(str(plan["current_revision_sha"] or plan["plan_sha"] or "")
                           if plan is not None else None),
            base_revision=str(project_state["active_sha"] or ""),
            fingerprint=fingerprint,
            progress_marker=str(project_state["active_sha"] or ""),
            detail=evidence,
        )
        recovery_decision = recovery_state.RecoveryRepository(connection).observe(
            observation, final_block_reason=reason,
        ).decision
    recovery_runtime.require_final(recovery_decision)
    if recovery_decision.block_reason != reason or recovery_decision.observation.repo != repo or recovery_decision.observation.plan_id != plan_id:
        raise ValueError("recovery decision and project block target disagree")
    connection.execute(
        """
        INSERT INTO project_blocks (
            repo, scope, reason_code, plan_id, evidence,
            release_fingerprint, previous_phase
        ) VALUES (?, 'project', ?, ?, ?, ?, ?)
        ON CONFLICT(repo) DO UPDATE SET
            scope = excluded.scope,
            reason_code = excluded.reason_code,
            plan_id = excluded.plan_id,
            evidence = excluded.evidence,
            release_fingerprint = excluded.release_fingerprint,
            previous_phase = CASE
                WHEN project_blocks.previous_phase IS NULL
                THEN excluded.previous_phase
                ELSE project_blocks.previous_phase
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            repo,
            reason.value,
            plan_id,
            evidence,
            release_fingerprint,
            previous_phase,
        ),
    )
    connection.execute(
        "UPDATE projects SET phase = 'PROJECT_BLOCKED', last_error = ? WHERE repo = ?",
        (evidence, repo),
    )


def clear_plan_block(connection: sqlite3.Connection, repo: str, plan_id: str) -> None:
    from loop_manager_internal.notifications import notifications

    notifications.release_block(connection, repo, f"plan:{plan_id}")
    connection.execute(
        "DELETE FROM execplan_blocks WHERE repo = ? AND plan_id = ?",
        (repo, plan_id),
    )


def clear_revalidated_rebase_block(connection, expected, *, is_plan_block, acceptance=None,
                                   pending_work=False, pending_target=None):
    """Release the selected cause only; the caller owns the transaction."""
    current = (plan_block(connection, expected['repo'], expected['plan_id'])
               if is_plan_block else project_block(connection, expected['repo']))
    if current is None or dict(current) != dict(expected):
        raise runtime_state.RuntimeStateError('rebase block changed during revalidation')
    if acceptance is not None or pending_target is not None:
        from loop_manager_internal.storage.blocked_checkpoint_state import record_diagnostic_cause_resolution
        record_diagnostic_cause_resolution(connection, expected, acceptance,
            pending_work=pending_work, pending_target=pending_target)
    if is_plan_block:
        clear_plan_block(connection, expected['repo'], expected['plan_id'])
    else:
        clear_project_block(connection, expected['repo'])


def clear_project_block(connection: sqlite3.Connection, repo: str) -> str | None:
    row = project_block(connection, repo)
    if row is None:
        return None
    previous_phase = str(row["previous_phase"]) if row["previous_phase"] else None
    from loop_manager_internal.notifications import notifications

    notifications.release_block(connection, repo, "project")
    connection.execute("DELETE FROM project_blocks WHERE repo = ?", (repo,))
    connection.execute(
        "UPDATE projects SET phase = ?, last_error = NULL WHERE repo = ?",
        (previous_phase, repo),
    )
    return previous_phase


def migrate_legacy_blocks(connection: sqlite3.Connection) -> None:
    """Conservatively give legacy free-form BLOCKED rows a durable scope."""

    ensure_schema(connection)
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not {"exec_plans", "projects"}.issubset(tables):
        return
    project_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(projects)")
    }
    project_phase = "r.phase" if "phase" in project_columns else "NULL"
    rows = connection.execute(
        f"SELECT p.*, {project_phase} AS project_phase FROM exec_plans AS p "
        "JOIN projects AS r USING (repo) WHERE p.status = 'BLOCKED'"
    ).fetchall()
    for row in rows:
        project = runtime_state.project_row(connection, row["repo"])
        project_columns = set(project.keys()) if project is not None else set()
        agent_fields = (
            "agent_role",
            "agent_snapshot",
            "agent_input_fingerprint",
            "agent_baseline",
            "agent_recovery",
            "agent_input_plan",
            "agent_result_plan",
            "agent_result_snapshot",
            "agent_manager_snapshot",
        )
        available_agent_fields = tuple(
            field for field in agent_fields if field in project_columns
        )
        has_agent_context = bool(
            project is not None
            and "agent_snapshot" in project_columns
            and project["agent_snapshot"]
        )
        if has_agent_context:
            assignments = ", ".join(
                f"{field} = ?" for field in available_agent_fields
            )
            connection.execute(
                f"UPDATE exec_plans SET {assignments} "
                "WHERE repo = ? AND plan_id = ?",
                (
                    *(project[field] for field in available_agent_fields),
                    row["repo"],
                    row["plan_id"],
                ),
            )
        legacy_reason = str(row["blocked_reason"] or "")
        if legacy_reason == "guidance_approval":
            reason = BlockReasonCode.GUIDANCE_APPROVAL_REQUIRED
        elif legacy_reason == "pull-request-closed":
            reason = BlockReasonCode.PULL_REQUEST_CLOSED
        elif row["blocked_fingerprint"]:
            reason = BlockReasonCode.PLAN_INPUT_INVALID
        else:
            reason = BlockReasonCode.LEGACY_UNSCOPED_BLOCK
        if reason_scope(reason) is BlockScope.PLAN:
            record_plan_block(
                connection,
                str(row["repo"]),
                str(row["plan_id"]),
                reason,
                str(row["last_error"] or "legacy BLOCKED"),
                str(row["blocked_fingerprint"] or "") or None,
            )
            if row["project_phase"] == "BLOCKED":
                if has_agent_context:
                    connection.execute(
                        "UPDATE projects SET phase = 'PLAN_BLOCKED' WHERE repo = ?",
                        (row["repo"],),
                    )
                else:
                    connection.execute(
                        "UPDATE projects SET phase = NULL, plan_path = NULL, "
                        "branch = NULL, worktree = NULL, pr_url = NULL, "
                        "pr_head_oid = NULL WHERE repo = ?",
                        (row["repo"],),
                    )
        else:
            # The original active stage cannot be reconstructed. Preserve the
            # plan as blocked as well as applying the conservative project gate.
            connection.execute(
                "UPDATE exec_plans SET status = 'PLAN_BLOCKED' WHERE repo = ? AND plan_id = ?",
                (row["repo"], row["plan_id"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO execplan_blocks "
                "(repo, plan_id, scope, reason_code, evidence, release_fingerprint) "
                "VALUES (?, ?, 'plan', ?, ?, ?)",
                (
                    row["repo"],
                    row["plan_id"],
                    BlockReasonCode.PLAN_INPUT_INVALID.value,
                    str(row["last_error"] or "legacy BLOCKED"),
                    str(row["blocked_fingerprint"] or "") or None,
                ),
            )
            record_project_block(
                connection,
                str(row["repo"]),
                reason,
                str(row["last_error"] or "legacy BLOCKED 범위를 확인할 수 없습니다."),
                str(row["blocked_fingerprint"] or "") or None,
                plan_id=str(row["plan_id"]),
                previous_phase=None,
            )


def validate_records(connection: sqlite3.Connection, repo: str) -> None:
    """Reject corrupted reason/scope pairs at the repository boundary."""

    for row in connection.execute(
        "SELECT scope, reason_code FROM execplan_blocks WHERE repo = ?", (repo,)
    ):
        reason = _reason(row["reason_code"])
        if row["scope"] != BlockScope.PLAN.value or reason_scope(reason) is not BlockScope.PLAN:
            raise RuntimeError("ExecPlan 차단 원인과 scope가 일치하지 않습니다.")
    row = project_block(connection, repo)
    if row is not None:
        reason = _reason(row["reason_code"])
        if row["scope"] != BlockScope.PROJECT.value or reason_scope(reason) is not BlockScope.PROJECT:
            raise RuntimeError("프로젝트 차단 원인과 scope가 일치하지 않습니다.")
