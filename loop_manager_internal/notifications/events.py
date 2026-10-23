"""Project event selection and completion and waiting episodes."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from loop_manager_internal.documents import reviewer_specification
from loop_manager_internal.execution.validation_feedback import redact_secrets
from .work_exhaustion import WorkReasonSnapshot
from .policy import is_current, cancel_stale_events, block_recovery, SOURCE_ACTION_REASONS
from .repository import (adopt_legacy_block_notices, ensure_schema, _enabled,
                         _insert)
from .incidents import (_key, _remote_inspection_suffix, _queue_block,
                        _plan_block_key, _project_block_key,
                        _project_checkpoint, _project_checkpoint_suffix, record_work_start)
from .incidents import _preservation_checkpoint
from .rendering import _checkpoint_link


@dataclass(frozen=True)
class _BlockedPlanProjection:
    """Named inputs for one blocked-plan notification payload."""

    plan_id: str
    title: str
    path: str
    sources: str
    reason_code: str
    evidence: str
    checkpoint_result: str | None
    checkpoint_remote_oid: str | None
    checkpoint_unpublished_reason: str | None
    checkpoint_branch: str | None
    origin: str | None
    checkpoint_inspected_remote_oid: str | None
    checkpoint_inspection_reason: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> _BlockedPlanProjection:
        def optional(name: str) -> str | None:
            value = row[name]
            return str(value) if value is not None else None

        return cls(
            plan_id=str(row["plan_id"]),
            title=str(row["plan_title"]),
            path=str(row["plan_path"]),
            sources=str(row["plan_sources"]),
            reason_code=str(row["reason_code"]),
            evidence=str(row["evidence"]),
            checkpoint_result=optional("checkpoint_result"),
            checkpoint_remote_oid=optional("checkpoint_remote_oid"),
            checkpoint_unpublished_reason=optional("checkpoint_unpublished_reason"),
            checkpoint_branch=optional("checkpoint_branch"),
            origin=optional("origin"),
            checkpoint_inspected_remote_oid=optional(
                "checkpoint_inspected_remote_oid"
            ),
            checkpoint_inspection_reason=optional("checkpoint_inspection_reason"),
        )


def enqueue_current_project_events(connection: sqlite3.Connection, repo: str) -> None:
    """Enqueue all plan/project events after their owning update and before commit."""

    ensure_schema(connection)
    adopt_legacy_block_notices(connection)
    cancel_stale_events(connection, repo)
    if not _enabled(connection):
        return
    project = connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None:
        return
    name = Path(repo).name or repo
    def final_recovery(plan_id, scope):
        row = block_recovery(connection, repo, plan_id, scope)
        if row is None or row['final_evidence'] is None:
            return None, None
        try:
            evidence = json.loads(row['final_evidence'])
        except (TypeError, ValueError):
            return None, None
        return (str(row['identity']), evidence) if isinstance(evidence, dict) else (None, None)

    enqueue_draft_decisions(connection, repo)

    for plan in connection.execute(
        "SELECT plan_id, title, pr_url FROM exec_plans "
        "WHERE repo = ? AND status = 'PR_OPEN' AND pr_url IS NOT NULL",
        (repo,),
    ).fetchall():
        payload = {
            "project": name,
            "execplan_id": str(plan[0]),
            "execplan_title": str(plan[1]),
            "pr_url": str(plan[2]),
        }
        if is_current(connection, repo, "pr_created", payload):
            _insert(connection, _key("pr", repo, str(plan[2])), repo, "pr_created", payload)


    plan_blocks = connection.execute(
        "SELECT p.plan_id AS plan_id, p.title AS plan_title, p.path AS plan_path, "
        "p.sources AS plan_sources, b.reason_code AS reason_code, "
        "b.evidence AS evidence, c.result AS checkpoint_result, "
        "c.remote_oid AS checkpoint_remote_oid, "
        "c.unpublished_reason AS checkpoint_unpublished_reason, "
        "c.branch AS checkpoint_branch, c.phase AS checkpoint_phase, r.origin AS origin, "
        "c.inspected_remote_oid AS checkpoint_inspected_remote_oid, "
        "c.inspection_reason AS checkpoint_inspection_reason "
        "FROM execplan_blocks AS b JOIN exec_plans AS p USING (repo, plan_id) "
        "JOIN projects AS r ON r.repo = b.repo "
        "LEFT JOIN blocked_checkpoints AS c USING (repo, plan_id) "
        "WHERE b.repo = ? AND (c.repo IS NULL OR c.phase IN "
        "('published', 'unpublished', 'retry_pending')) ORDER BY p.plan_id",
        (repo,),
    ).fetchall()
    for row in plan_blocks:
        plan = _BlockedPlanProjection.from_row(row)
        preservation_checkpoint = _preservation_checkpoint(connection, repo, 'plan', plan.plan_id)
        if row['checkpoint_phase'] == 'retry_pending' and preservation_checkpoint is None:
            continue
        try:
            sources = json.loads(plan.sources)
        except (TypeError, ValueError):
            sources = []
        stage = "ExecPlan"
        plan_id = plan.plan_id
        plan_title = plan.title
        recovery_identity, recovery_evidence = final_recovery(plan_id, 'plan')
        if plan.reason_code in SOURCE_ACTION_REASONS:
            recovery_identity = None
        paths = (
            list(recovery_evidence.get("user_targets", []))
            if recovery_evidence is not None
            else [plan.path, *(str(item) for item in sources)]
        )
        deadlock = None
        if plan.reason_code == "review_specification_deadlock":
            try:
                deadlock = reviewer_specification.parse_evidence(plan.evidence)
            except ValueError:
                pass
            if deadlock is not None:
                paths = list(deadlock.paths)
        checkpoint = None
        if plan.checkpoint_result == "published":
            checkpoint = {
                "result": "published",
                "branch": plan.checkpoint_branch,
                "branch_url": _checkpoint_link(
                    plan.origin, plan.checkpoint_remote_oid,
                ),
                "commit_oid": plan.checkpoint_remote_oid,
            }
        elif plan.checkpoint_result == "local_only":
            checkpoint = {
                "result": "local_only",
                "reason": (
                    plan.checkpoint_unpublished_reason
                    or "checkpoint를 게시하지 않았습니다."
                ),
            }
        checkpoint = preservation_checkpoint or checkpoint
        guidance_locations = []
        if plan.reason_code == "guidance_approval_required":
            rows = connection.execute(
                "SELECT proposal_id, proposal_line, decision_line "
                "FROM guidance_approvals WHERE repo = ? AND plan_id = ? "
                "AND applied = 0 AND decision = 'pending' ORDER BY sequence, proposal_id",
                (repo, plan_id),
            ).fetchall()
            guidance_locations = [
                {
                    "proposal_id": str(row[0]),
                    "proposal": f"{plan.path}:{row[1]}",
                    "user_decision": f"{plan.path}:{row[2]}",
                }
                for row in rows
            ]
            paths.extend(
                location
                for item in guidance_locations
                for location in (item["proposal"], item["user_decision"])
                if location not in paths
            )
        remote_inspection = _remote_inspection_suffix(
            plan.checkpoint_result,
            plan.checkpoint_inspected_remote_oid,
            plan.checkpoint_inspection_reason,
        )
        remote_inspection += _project_checkpoint_suffix(checkpoint) if checkpoint and checkpoint.get('diagnostic') else ''
        payload = {
            "project": name,
            "stage": stage,
            "scope": "plan",
            "reason_code": plan.reason_code,
            "execplan_id": plan_id,
            "execplan_title": plan_title,
            "cause": plan.evidence,
            "paths": paths,
            "guidance_locations": guidance_locations,
            "checkpoint": checkpoint,
            "recovery": recovery_evidence,
        }
        if recovery_evidence is not None and isinstance(
            recovery_evidence.get("worker_decision"), dict
        ):
            payload["worker_decision"] = recovery_evidence["worker_decision"]
        if deadlock is not None:
            payload["specification_deadlock"] = {
                "kind": deadlock.kind,
                "locations": [
                    {"path": path, "section": section}
                    for path, section in deadlock.locations
                ],
                "problem": deadlock.problem,
                "implementation_choice": deadlock.implementation_choice,
                "question": deadlock.question,
            }
        suppress_persistent_failure_for_block(connection, repo, plan_id)
        _queue_block(
            connection,
            repo,
            f"plan:{plan_id}",
            _plan_block_key(repo, plan.reason_code, plan_id,
                            recovery_identity, remote_inspection),
            payload,
        )

    project_block = connection.execute(
        "SELECT b.reason_code, b.plan_id, b.evidence, p.title, p.path, p.sources "
        "FROM project_blocks AS b LEFT JOIN exec_plans AS p "
        "ON p.repo = b.repo AND p.plan_id = b.plan_id WHERE b.repo = ?",
        (repo,),
    ).fetchone()
    if project_block is not None:
        checkpoint = (_preservation_checkpoint(connection, repo, 'project', project_block[1])
                      or _project_checkpoint(connection, repo, project_block[1]))
        recovery_identity, recovery_evidence = final_recovery(
            project_block[1], "project"
        )
        try:
            project_sources = json.loads(str(project_block[5])) if project_block[5] else []
        except (TypeError, ValueError):
            project_sources = []
        project_paths = (
            [
                str(project_block[4]),
                *(str(item) for item in project_sources),
            ]
            if project_block[4]
            else []
        )
        if recovery_evidence is not None:
            project_paths = list(recovery_evidence.get("user_targets", []))
        suppress_persistent_failure_for_block(
            connection, repo, str(project_block[1] or "__project__"),
        )
        _queue_block(
            connection,
            repo,
            "project",
            _project_block_key(
                repo, project_block[0], project_block[1], recovery_identity,
                _project_checkpoint_suffix(checkpoint),
            ),
            {
                "project": name,
                "stage": "project",
                "scope": "project",
                "reason_code": str(project_block[0]),
                "execplan_id": project_block[1],
                "execplan_title": project_block[3],
                "cause": str(project_block[2]),
                "paths": project_paths,
                "guidance_locations": [],
                "recovery": recovery_evidence,
                "checkpoint": checkpoint,
            },
        )



def cancel_pending_persistent_execution_failure_event(
    connection: sqlite3.Connection, repo: str, subject: str, episode: int,
    phase: str, reason_code: str, fingerprint: str,
) -> None:
    """Cancel remaining legacy failure pieces while retaining delivery audit."""

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'notification_outbox'"
    ).fetchone() is None:
        return
    identity = "\0".join((subject, str(episode), phase, reason_code, fingerprint))
    connection.execute(
        "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, "
        "delivery_started_at = NULL WHERE event_key = ? "
        "AND sent_at IS NULL AND canceled_at IS NULL",
        (_key("persistent_failure", repo, identity),),
    )


def _persistent_failure_matches_current_block(
    connection: sqlite3.Connection, repo: str, subject: str, failure,
) -> bool:
    available = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('recovery_records', 'project_blocks', 'execplan_blocks')"
        )
    }
    if available != {"recovery_records", "project_blocks", "execplan_blocks"}:
        return False
    recovery = connection.execute(
        "SELECT reason_code, pending_action, phase, fingerprint, "
        "progress_marker, detail, final_evidence FROM recovery_records "
        "WHERE repo = ? AND subject = ?", (repo, subject),
    ).fetchone()
    if (recovery is None or recovery[1] != "final_block"
            or any(str(recovery[index]) != str(failure[name]) for index, name in (
                (0, "reason_code"), (2, "phase"), (3, "fingerprint"),
                (4, "progress_marker"), (5, "detail"),
            ))):
        return False
    try:
        final_evidence = json.loads(str(recovery[6]))
    except (TypeError, ValueError):
        return False
    if (not isinstance(final_evidence, dict)
            or final_evidence.get("reason_code") != recovery[0]):
        return False
    block_reason = final_evidence.get("block_reason_code")
    if subject == "__project__":
        block = connection.execute(
            "SELECT reason_code, evidence FROM project_blocks WHERE repo = ?",
            (repo,),
        ).fetchone()
        return bool(
            block is not None and block[0] == block_reason
            and block[1] == recovery[5]
        )
    blocks = connection.execute(
        "SELECT reason_code, evidence FROM execplan_blocks "
        "WHERE repo = ? AND plan_id = ? UNION ALL "
        "SELECT reason_code, evidence FROM project_blocks "
        "WHERE repo = ? AND plan_id = ?",
        (repo, subject, repo, subject),
    ).fetchall()
    return any(
        block[0] == block_reason and block[1] == recovery[5]
        for block in blocks
    )


def suppress_persistent_failure_for_block(
    connection: sqlite3.Connection, repo: str, subject: str,
) -> None:
    """Make a final BLOCKED notice own its pending failure incident."""

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'persistent_execution_failures'"
    ).fetchone() is None:
        return
    row = connection.execute(
        "SELECT episode, phase, reason_code, fingerprint, progress_marker, detail "
        "FROM persistent_execution_failures WHERE repo = ? AND subject = ? "
        "AND active = 1", (repo, subject),
    ).fetchone()
    if row is None or not _persistent_failure_matches_current_block(
        connection, repo, subject,
        {"phase": row[1], "reason_code": row[2], "fingerprint": row[3],
         "progress_marker": row[4], "detail": row[5]},
    ):
        return
    cancel_pending_persistent_execution_failure_event(
        connection, repo, subject, int(row[0]), str(row[1]), str(row[2]),
        str(row[3]),
    )
    connection.execute(
        "UPDATE persistent_execution_failures SET active = 0, "
        "notification_queued = 0, suppressed_by_block = 1, "
        "updated_at = CURRENT_TIMESTAMP, resolved_at = CURRENT_TIMESTAMP "
        "WHERE repo = ? AND subject = ?", (repo, subject),
    )


def record_merged_plan(
    connection: sqlite3.Connection, repo: str, plan_id: str, title: str
) -> None:
    ensure_schema(connection)
    connection.execute(
        """
        INSERT INTO project_completion_cycles
            (repo, last_completed_plan_id, last_completed_title)
        VALUES (?, ?, ?)
        ON CONFLICT(repo) DO UPDATE SET
            last_completed_plan_id = excluded.last_completed_plan_id,
            last_completed_title = excluded.last_completed_title
        """,
        (repo, plan_id, title),
    )


def enqueue_completion_if_ready(
    connection: sqlite3.Connection, repo: str,
    snapshot: WorkReasonSnapshot | None = None,
) -> bool:
    """Record completion only for an already verified stable selection."""

    from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
    if catalog_held_plan_ids(connection, repo):
        return False
    if snapshot is None or not snapshot.completed:
        return False
    ensure_schema(connection)
    project = connection.execute(
        "SELECT phase, planning_fingerprint, planning_error "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    has_historical_catalog = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'execplan_catalog_history'"
    ).fetchone() is not None
    unfinished_query = (
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' "
        "AND NOT EXISTS (SELECT 1 FROM execplan_catalog_history h "
        "WHERE h.repo = exec_plans.repo AND h.currently_archived = 1 "
        "AND h.plan_id = exec_plans.plan_id) LIMIT 1"
        if has_historical_catalog else
        "SELECT 1 FROM exec_plans WHERE repo = ? AND status != 'MERGED' LIMIT 1"
    )
    if (
        project is None
        or project[0] is not None
        or not project[1]
        or str(project[1]) != snapshot.cycle
        or project[2]
        or connection.execute(unfinished_query, (repo,)).fetchone() is not None
        or connection.execute(
            "SELECT 1 FROM project_blocks WHERE repo = ?", (repo,),
        ).fetchone() is not None
    ):
        return False

    cycle_key = str(project[1])
    with connection:
        _cancel_pending_waiting(connection, repo)
        connection.execute(
            """
            INSERT INTO project_completion_cycles (repo, cycle_key, completed_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo) DO UPDATE SET
                cycle_key = excluded.cycle_key,
                completed_at = excluded.completed_at
            WHERE project_completion_cycles.cycle_key IS NOT excluded.cycle_key
                OR project_completion_cycles.completed_at IS NULL
            """,
            (repo, cycle_key),
        )
    return True


def _cancel_pending_waiting(connection: sqlite3.Connection, repo: str) -> None:
    connection.execute(
        "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP "
        "WHERE repo = ? AND event_type = 'work_waiting' "
        "AND sent_at IS NULL AND canceled_at IS NULL",
        (repo,),
    )


def mark_work_available(connection: sqlite3.Connection, repo: str) -> None:
    """End a waiting incident when selection can proceed again."""

    ensure_schema(connection)
    with connection:
        connection.execute(
            "UPDATE work_reason_episodes SET signature = '__work_available__' "
            "WHERE repo = ? AND signature != '__work_available__'", (repo,),
        )
        _cancel_pending_waiting(connection, repo)


def enqueue_waiting_reason(
    connection: sqlite3.Connection, repo: str, snapshot: WorkReasonSnapshot,
) -> bool:
    """Record stable waiting episodes without creating a Telegram notice."""

    if snapshot.completed:
        return False
    ensure_schema(connection)
    payload = snapshot.payload(Path(repo).name or repo)
    enqueue_draft_decisions(connection, repo)
    signature = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode()
    ).hexdigest()
    with connection:
        previous = connection.execute(
            "SELECT signature, generation FROM work_reason_episodes WHERE repo = ?",
            (repo,),
        ).fetchone()
        generation = (
            int(previous[1]) if previous and previous[0] == signature
            else int(previous[1]) + 1 if previous else 1
        )
        connection.execute(
            "INSERT INTO work_reason_episodes (repo, signature, generation) "
            "VALUES (?, ?, ?) ON CONFLICT(repo) DO UPDATE SET "
            "signature = excluded.signature, generation = excluded.generation",
            (repo, signature, generation),
        )
        _cancel_pending_waiting(connection, repo)
    return False


def enqueue_progress(connection, repo, kind, identity, **payload):
    """Persist a boundary observation in the owning state transaction."""
    if kind not in {"draft_decisions", "draft_decision", "pr_created"} or not _enabled(connection):
        return
    ensure_schema(connection)
    plan_id = payload.get("execplan_id")
    if plan_id:
        plan = connection.execute("SELECT title FROM exec_plans WHERE repo = ? AND plan_id = ?",
                                  (repo, plan_id)).fetchone()
        if plan:
            payload.setdefault("execplan_title", str(plan[0]))
    if isinstance(payload.get("cause"), str):
        payload["cause"] = redact_secrets(payload["cause"])
    payload = {"project": Path(repo).name or repo, **payload}
    if is_current(connection, repo, kind, payload):
        _insert(connection, _key(kind, repo, identity), repo, kind, payload)


def enqueue_review_failure(connection, repo, plan_id, cause, *, status):
    """Compatibility hook; Review diagnostics are retained by recovery, without Telegram."""


def enqueue_worker_failure(connection, repo, plan_id, cause, *, status):
    """Compatibility hook; Worker diagnostics are retained by recovery, without Telegram."""


def record_process_start(connection, repo, role, identity, *, plan_id=None, resumed=False):
    """A physical process has started, rather than just been selected."""
    if role not in {"worker", "planner", "maintenance_planner"}:
        return
    from loop_manager_internal.storage import execplan_draft_state
    ensure_schema(connection)
    if connection.execute(
        "SELECT 1 FROM notified_block_incidents WHERE repo = ? AND start_identity = ?", (repo, identity),
    ).fetchone():
        return
    if connection.execute("SELECT 1 FROM project_blocks WHERE repo = ?", (repo,)).fetchone():
        return
    if plan_id and (plan_id in execplan_draft_state.pending_ids(connection, repo) or connection.execute(
        "SELECT 1 FROM execplan_blocks WHERE repo = ? AND plan_id = ?", (repo, plan_id),
    ).fetchone()):
        return
    operation = "Worker" if role == "worker" else "Planner"
    record_work_start(connection, repo, operation, plan_id=plan_id,
                      start_identity=identity, resumed=resumed)


def record_planning_publication(connection, repo, plans, revision):
    if not _enabled(connection):
        return
    enqueue_draft_decisions(connection, repo)


def enqueue_draft_decisions(connection, repo):
    from loop_manager_internal.storage import execplan_draft_state
    if not _enabled(connection):
        return
    plans = []
    for plan_id in sorted(execplan_draft_state.user_question_ids(connection, repo)):
        saved = execplan_draft_state.evidence(connection, repo, plan_id)
        plan = connection.execute("SELECT title FROM exec_plans WHERE repo = ? AND plan_id = ?",
                                  (repo, plan_id)).fetchone()
        if saved and saved.get("pending") and plan:
            plans.append({"id": plan_id, "title": str(plan[0]), "url": saved.get("url"),
                          "decision_identity": saved.get("identity"), "decision_revision": saved["revision"]})
    if not plans:
        ensure_schema(connection)
        connection.execute(
            "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND event_type = 'draft_decisions' "
            "AND sent_at IS NULL AND canceled_at IS NULL", (repo,))
        return
    if plans:
        ensure_schema(connection)
        previous = connection.execute(
            "SELECT event_key, payload, canceled_at FROM notification_outbox WHERE repo = ? "
            "AND event_type = 'draft_decisions' ORDER BY rowid DESC LIMIT 1", (repo,),
        ).fetchone()
        def decisions(items):
            return [(plan["id"], plan["decision_identity"], plan["decision_revision"]) for plan in items]
        if previous and not previous[2] and decisions(json.loads(previous[1])["plans"]) == decisions(plans):
            return
        identity = str(previous[0] if previous else "") + "\0" + json.dumps(decisions(plans), ensure_ascii=False)
        connection.execute(
            "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND event_type = 'draft_decisions' "
            "AND sent_at IS NULL AND canceled_at IS NULL", (repo,),
        )
        enqueue_progress(connection, repo, "draft_decisions", identity,
                         plans=plans, status="사용자 판단 대기", silent=False)
