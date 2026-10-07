"""Project event selection and completion and waiting episodes."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from loop_manager_internal.documents import reviewer_specification
from .work_exhaustion import WorkReasonSnapshot
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

    if not _enabled(connection):
        return
    ensure_schema(connection)
    adopt_legacy_block_notices(connection)
    project = connection.execute(
        "SELECT phase, plan_path, pr_url, last_error, planning_error, "
        "planning_attempt_fingerprint, planning_validation_error, active_sha "
        "FROM projects WHERE repo = ?",
        (repo,),
    ).fetchone()
    if project is None:
        return
    name = Path(repo).name or repo
    planning_failure = project[3] or project[6]
    if project[0] == "PLANNING" and planning_failure and not project[4]:
        enqueue_progress(connection, repo, "stage_failed",
                         str(project[5]) + "\0" + str(project[7]) + "\0" + str(planning_failure),
                         operation="Planner", cause=str(planning_failure),
                         status="다음 실행 또는 자동 교정 대기", silent=True)
    if project[0] == "REVIEWING" and project[3]:
        plan = connection.execute(
            "SELECT plan_id, current_revision_sha, verify_token FROM exec_plans "
            "WHERE repo = ? AND path = ?", (repo, project[1]),
        ).fetchone()
        if plan:
            enqueue_progress(
                connection, repo, "stage_failed",
                "review\0" + "\0".join(str(value) for value in (*plan, project[3])),
                execplan_id=str(plan[0]), operation="Reviewer", cause=str(project[3]),
                status="자동 교정·재시도 대기", silent=True,
            )
    recovery_available = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_records'"
    ).fetchone() is not None

    def final_recovery(subject: str) -> tuple[str | None, dict | None]:
        if not recovery_available:
            return None, None
        row = connection.execute(
            "SELECT identity, final_evidence FROM recovery_records "
            "WHERE repo = ? AND subject = ?", (repo, subject),
        ).fetchone()
        if row is None or row[1] is None:
            return None, None
        return str(row[0]), json.loads(str(row[1]))

    from loop_manager_internal.storage import execplan_draft_state
    for draft_id in execplan_draft_state.pending_ids(connection, repo):
        draft = connection.execute("SELECT title FROM exec_plans WHERE repo = ? AND plan_id = ?",
                                   (repo, draft_id)).fetchone()
        if draft:
            enqueue_draft_decision(connection, repo, draft_id, str(draft[0]))

    for plan in connection.execute(
        "SELECT plan_id, title, pr_url FROM exec_plans "
        "WHERE repo = ? AND status = 'PR_OPEN' AND pr_url IS NOT NULL",
        (repo,),
    ).fetchall():
        _insert(
            connection,
            _key("pr", repo, str(plan[2])),
            repo,
            "pr_created",
            {
                "project": name,
                "execplan_id": str(plan[0]),
                "execplan_title": str(plan[1]),
                "pr_url": str(plan[2]),
            },
        )

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
        recovery_identity, recovery_evidence = final_recovery(plan_id)
        paths = (
            list(recovery_evidence["user_targets"])
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
                "AND applied = 0 ORDER BY sequence, proposal_id",
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
            str(project_block[1] or "__project__")
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
            project_paths = list(recovery_evidence["user_targets"])
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

    if project[4] and project_block is None:
        checkpoint = _preservation_checkpoint(connection, repo, 'planning', 'planning')
        _queue_block(
            connection,
            repo,
            "planning",
            _key("blocked", repo, "project\0planning_error\0" + _project_checkpoint_suffix(checkpoint)),
            {
                "project": name,
                "stage": "planning",
                "scope": "project",
                "reason_code": "planning_error",
                "execplan_id": None,
                "execplan_title": None,
                "cause": str(project[4]),
                "paths": [
                    "docs/PRODUCT.md",
                    "docs/requirements/*.md",
                    "docs/ARCHITECTURE.md",
                ],
                "guidance_locations": [],
                "checkpoint": checkpoint,
            },
        )


def enqueue_persistent_execution_failure_events(
    connection: sqlite3.Connection, repo: str,
) -> None:
    """Queue one actionable notice for each eligible external-run streak.

    The recovery projection is retained even when Telegram is disabled.  The
    outbox is intentionally populated only for an enabled notification session,
    matching the established behaviour for all other project events.
    """

    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'persistent_execution_failures'"
    ).fetchone()
    if exists is None or not _enabled(connection):
        return
    ensure_schema(connection)
    project = connection.execute(
        "SELECT 1 FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    if project is None:
        return
    rows = connection.execute(
        "SELECT * FROM persistent_execution_failures WHERE repo = ? "
        "AND active = 1 AND notification_eligible = 1 "
        "AND notification_queued = 0 AND suppressed_by_block = 0 "
        "ORDER BY subject",
        (repo,),
    ).fetchall()
    for row in rows:
        subject = str(row["subject"])
        plan_id = None if subject == "__project__" else subject
        identity = "\0".join((subject, str(row["episode"]), str(row["phase"]),
                              str(row["reason_code"]), str(row["fingerprint"])))
        payload = {
            "project": Path(repo).name or repo,
            "stage": str(row["phase"]),
            "scope": "execution",
            "execplan_id": plan_id,
            "reason_code": str(row["reason_code"]),
            "cause": str(row["detail"]),
            "consecutive_external_runs": int(row["consecutive_runs"]),
            "recovery_attempts": json.loads(str(row["recovery_attempts"])),
            "required_action": (
                "Review the structured failure and fix the reported "
                "configuration, permission, or repository condition."
            ),
        }
        _insert(
            connection, _key("persistent_failure", repo, identity), repo,
            "persistent_execution_failure", payload,
        )
        connection.execute(
            "UPDATE persistent_execution_failures SET notification_queued = 1, "
            "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND subject = ? "
            "AND episode = ? AND notification_eligible = 1",
            (repo, subject, int(row["episode"])),
        )


def cancel_pending_persistent_execution_failure_event(
    connection: sqlite3.Connection, repo: str, subject: str, episode: int,
    phase: str, reason_code: str, fingerprint: str,
) -> None:
    """Withdraw a notice only before its first delivery attempt."""

    identity = "\0".join((subject, str(episode), phase, reason_code, fingerprint))
    connection.execute(
        "DELETE FROM notification_outbox WHERE event_key = ? "
        "AND sent_at IS NULL AND attempts = 0",
        (_key("persistent_failure", repo, identity),),
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
        "SELECT episode, phase, reason_code, fingerprint "
        "FROM persistent_execution_failures WHERE repo = ? AND subject = ? "
        "AND active = 1", (repo, subject),
    ).fetchone()
    if row is None:
        return
    cancel_pending_persistent_execution_failure_event(
        connection, repo, subject, int(row[0]), str(row[1]), str(row[2]),
        str(row[3]),
    )
    connection.execute(
        "UPDATE persistent_execution_failures SET active = 0, "
        "consecutive_runs = 0, notification_eligible = 0, "
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
    completion = connection.execute(
        "SELECT last_completed_plan_id, last_completed_title "
        "FROM project_completion_cycles WHERE repo = ?",
        (repo,),
    ).fetchone()
    last_id = str(completion[0]) if completion and completion[0] else None
    last_title = str(completion[1]) if completion and completion[1] else None
    if last_id is None:
        merged = connection.execute(
            "SELECT plan_id, title FROM exec_plans "
            "WHERE repo = ? AND status = 'MERGED'",
            (repo,),
        ).fetchall()
        if merged:
            def plan_order(row: sqlite3.Row) -> tuple[int, str]:
                value = str(row[0])
                try:
                    return int(value.removeprefix("G-")), value
                except ValueError:
                    return -1, value

            latest = max(merged, key=plan_order)
            last_id, last_title = str(latest[0]), str(latest[1])
    last_title = last_title or "Planner confirmed no additional work"
    with connection:
        _cancel_pending_waiting(connection, repo)
        if not _enabled(connection):
            return False
        connection.execute(
            """
            INSERT INTO project_completion_cycles (repo, cycle_key, completed_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo) DO UPDATE SET
                cycle_key = excluded.cycle_key,
                completed_at = excluded.completed_at
            """,
            (repo, cycle_key),
        )
        _insert(
            connection,
            _key("completed", repo, cycle_key),
            repo,
            "project_completed",
            {
                "project": Path(repo).name or repo,
                "reason": snapshot.describe(),
                "last_completed_plan_id": last_id,
                "last_completed_title": last_title,
            },
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
    """Create one waiting incident per stable cause and cause transition."""

    if snapshot.completed:
        return False
    ensure_schema(connection)
    payload = snapshot.payload(Path(repo).name or repo)
    for plan in payload["plans"]:
        if plan.get("question"):
            enqueue_draft_decision(connection, repo, plan["id"], plan["title"])
    payload["plans"] = [plan for plan in payload["plans"] if not plan.get("question")]
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
        if not snapshot.has_distinct_wait or not (payload["plans"] or payload["requirements"]):
            _cancel_pending_waiting(connection, repo)
            return False
        event_key = _key("waiting", repo, f"{generation}\0{signature}")
        connection.execute(
            "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP "
            "WHERE repo = ? AND event_type = 'work_waiting' "
            "AND event_key != ? AND sent_at IS NULL AND canceled_at IS NULL",
            (repo, event_key),
        )
        if not _enabled(connection):
            return False
        payload["silent"] = not (payload.get("project_block") or payload.get("planning_error") or any(
            plan.get("status") in {"PR_OPEN", "PLAN_BLOCKED", "CATALOG_HELD", "MANUAL_PENDING", "MANUAL_WORKING"}
            for plan in payload["plans"]
        ))
        payload["status"] = "다음 작업 대기"
        _insert(connection, event_key, repo, "work_waiting", payload)
    return True


def enqueue_progress(connection, repo, kind, identity, **payload):
    """Persist a boundary observation in the owning state transaction."""
    if not _enabled(connection):
        return
    ensure_schema(connection)
    plan_id = payload.get("execplan_id")
    if plan_id:
        plan = connection.execute("SELECT title FROM exec_plans WHERE repo = ? AND plan_id = ?",
                                  (repo, plan_id)).fetchone()
        if plan:
            payload.setdefault("execplan_title", str(plan[0]))
    _insert(connection, _key(kind, repo, identity), repo, kind,
            {"project": Path(repo).name or repo, **payload})


def record_process_start(connection, repo, role, identity, *, plan_id=None, resumed=False):
    """A physical process has started, rather than just been selected."""
    from loop_manager_internal.storage import execplan_draft_state
    ensure_schema(connection)
    if connection.execute(
        "SELECT 1 FROM notification_outbox WHERE event_key = ?", (_key("work_started", repo, identity),),
    ).fetchone() or connection.execute(
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
    blocked_resume = record_work_start(connection, repo, operation, plan_id=plan_id, start_identity=identity)
    # Delivery of a BLOCKED piece may still be in flight. The incident will
    # materialize its resumption when confirmation arrives; suppress the generic start too.
    pending_resume = connection.execute(
        "SELECT 1 FROM notified_block_incidents i JOIN notification_outbox o "
        "ON o.event_key = i.event_key WHERE i.repo = ? AND i.start_identity = ? "
        "AND i.released_at IS NOT NULL AND i.resumed_at IS NULL "
        "AND o.delivery_started_at IS NOT NULL LIMIT 1",
        (repo, identity),
    ).fetchone()
    if not blocked_resume and not pending_resume:
        enqueue_progress(connection, repo, "work_started", identity,
                         execplan_id=plan_id, operation="유지보수 평가" if role == "maintenance_planner" else operation,
                         status="작업 재개 중" if resumed else "작업 중", silent=True)


def record_planning_publication(connection, repo, plans, revision):
    if not _enabled(connection):
        return
    from loop_manager_internal.storage import execplan_draft_state
    ordinary = []
    for path, plan in plans:
        plan_id = str(plan["id"])
        if plan_id in execplan_draft_state.pending_ids(connection, repo):
            enqueue_draft_decision(connection, repo, plan_id, str(plan["title"]))
        else:
            ordinary.append({"id": plan_id, "title": str(plan["title"])})
    if ordinary:
        enqueue_progress(connection, repo, "plans_published", revision,
                         plans=ordinary, status="실행 또는 dependency 대기", silent=True)


def enqueue_draft_decision(connection, repo, plan_id, title):
    from loop_manager_internal.storage import execplan_draft_state
    saved = execplan_draft_state.evidence(connection, repo, plan_id)
    if saved and saved.get("pending"):
        enqueue_progress(connection, repo, "draft_decision",
                         str(saved.get("identity")) + "\0" + str(saved["revision"]),
                         execplan_id=plan_id, execplan_title=title,
                         question=saved["question"], url=saved.get("url"), decision_revision=saved["revision"],
                         status="사용자 판단 대기", silent=False)
