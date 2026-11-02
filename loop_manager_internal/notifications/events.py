"""Project event selection and completion and waiting episodes."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from urllib.parse import quote
from loop_manager_internal.documents import reviewer_specification
from loop_manager_internal.execution.validation_feedback import redact_secrets
from .work_exhaustion import WorkReasonSnapshot
from .policy import (is_current, cancel_stale_events, block_recovery,
                     draft_questions_for_notice,
                     cancel_event, pr_review_candidate, SOURCE_ACTION_REASONS,
                     confirmed_publication_keys)
from .repository import (adopt_legacy_block_notices, ensure_schema, _enabled,
                         _insert)
from .incidents import (_key, _remote_inspection_suffix, _queue_block,
                        _plan_block_key, _project_block_key,
                        _project_checkpoint, _project_checkpoint_suffix, record_work_start)
from .incidents import _preservation_checkpoint
from .rendering import _checkpoint_link, _pieces


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


def record_pr_review_candidate(
    connection: sqlite3.Connection, repo: str, plan_id: str, *,
    publication_token: str, review_input_identity: str,
) -> None:
    """Persist the exact published review token and its rebase-stable input identity."""
    if not publication_token or not review_input_identity:
        return
    ensure_schema(connection)
    connection.execute(
        "INSERT INTO pr_review_candidates "
        "(repo, plan_id, publication_token, review_input_identity) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(repo, plan_id) DO UPDATE SET "
        "publication_token=excluded.publication_token, "
        "review_input_identity=excluded.review_input_identity",
        (repo, plan_id, publication_token, review_input_identity),
    )
    connection.execute(
        "UPDATE pr_creation_publications SET review_input_identity=? "
        "WHERE repo=? AND plan_id=? AND publication_token=? AND review_input_identity IS NULL",
        (review_input_identity, repo, plan_id, publication_token),
    )


def record_pr_creation(connection, repo, plan_id, pr_url):
    """생성 성공 또는 소유권을 확인한 pr_create 복구 경계에서만 기록한다."""
    ensure_schema(connection)
    row = connection.execute(
        "SELECT publication_token FROM exec_plans WHERE repo=? AND plan_id=?",
        (repo, plan_id),
    ).fetchone()
    if row is None or not row['publication_token'] or not pr_url:
        return
    proof = connection.execute(
        "SELECT review_input_identity FROM pr_review_candidates "
        "WHERE repo=? AND plan_id=? AND publication_token=?",
        (repo, plan_id, row['publication_token']),
    ).fetchone()
    connection.execute(
        "INSERT OR IGNORE INTO pr_creation_publications "
        "(repo, plan_id, pr_url, publication_token, review_input_identity) VALUES (?, ?, ?, ?, ?)",
        (repo, plan_id, pr_url, row['publication_token'], proof[0] if proof else None),
    )


def cancel_pr_review_events_for_plans(
    connection: sqlite3.Connection, repo: str, plan_ids,
) -> None:
    """Cancel pending notices when a conflict observation confirms a different PR head."""
    stale_plan_ids = {str(plan_id) for plan_id in plan_ids}
    if not stale_plan_ids:
        return
    for row in connection.execute(
        "SELECT event_key, payload FROM notification_outbox "
        "WHERE repo=? AND event_type='pr_created' "
        "AND sent_at IS NULL AND canceled_at IS NULL", (repo,),
    ).fetchall():
        try:
            payload = json.loads(str(row[1]))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get('execplan_id') or '') in stale_plan_ids:
            cancel_event(connection, str(row[0]))


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
        candidate = pr_review_candidate(
            connection, repo, str(plan[0]), str(plan[2]), creation=True,
        ) or pr_review_candidate(
            connection, repo, str(plan[0]), str(plan[2]),
        )
        if candidate is None:
            continue
        payload = {
            "project": name,
            "execplan_id": str(plan[0]),
            "execplan_title": str(plan[1]),
            "pr_url": str(plan[2]),
            **candidate,
        }
        if is_current(connection, repo, "pr_created", payload):
            identity = "\0".join((
                str(plan[2]), str(candidate["candidate_identity"]),
                str(candidate["user_action"]),
            ))
            _insert(connection, _key("pr", repo, identity), repo, "pr_created", payload)


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

    if snapshot is None or not snapshot.completed:
        return False
    ensure_schema(connection)
    from .policy import completion_current
    from loop_manager_internal.storage import planning_review_state
    with connection:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        work_cycle = planning_review_state.work_cycle(connection, repo)
        if not completion_current(connection, repo, snapshot.cycle, work_cycle):
            return False
        previous = connection.execute(
            "SELECT cycle_key, completed_at, generation FROM project_completion_cycles WHERE repo=?", (repo,),
        ).fetchone()
        _cancel_pending_waiting(connection, repo)
        if previous is None or previous[0] != snapshot.cycle:
            connection.execute(
                "UPDATE notification_outbox SET canceled_at=CURRENT_TIMESTAMP "
                "WHERE repo=? AND event_type='project_completed' AND sent_at IS NULL AND canceled_at IS NULL",
                (repo,),
            )
        connection.execute(
            "INSERT INTO project_completion_cycles (repo, cycle_key, completed_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(repo) DO UPDATE SET "
            "cycle_key=excluded.cycle_key, completed_at=excluded.completed_at "
            "WHERE project_completion_cycles.cycle_key IS NOT excluded.cycle_key "
            "OR project_completion_cycles.completed_at IS NULL", (repo, snapshot.cycle),
        )
        if _enabled(connection) and (previous is None or previous[0] != snapshot.cycle or not previous[1]):
            saved = connection.execute(
                "SELECT last_completed_plan_id, last_completed_title, generation FROM project_completion_cycles WHERE repo=?",
                (repo,),
            ).fetchone()
            payload = {"project": Path(repo).name or repo, "cycle": snapshot.cycle,
                       "work_cycle": work_cycle, "generation": saved[2], "reason": snapshot.describe(),
                       "last_completed_plan_id": saved[0], "last_completed_title": saved[1] or ''}
            _insert(connection, _key('project_completed', repo, snapshot.cycle + '\0' + work_cycle + '\0' + str(saved[2])),
                    repo, 'project_completed', payload)
    from .delivery import flush_project_events
    flush_project_events(connection, repo)
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
        connection.execute("UPDATE project_completion_cycles SET completed_at=NULL, generation=generation+1 "
                           "WHERE repo=? AND completed_at IS NOT NULL", (repo,))
        connection.execute(
            "UPDATE notification_outbox SET canceled_at=CURRENT_TIMESTAMP "
            "WHERE repo=? AND event_type='project_completed' AND sent_at IS NULL AND canceled_at IS NULL", (repo,),
        )


def enqueue_waiting_reason(
    connection: sqlite3.Connection, repo: str, snapshot: WorkReasonSnapshot,
) -> bool:
    """Record the stable episode and queue its independently validated notice."""

    if snapshot.completed:
        return False
    ensure_schema(connection)
    payload = snapshot.payload(Path(repo).name or repo)
    enqueue_draft_decisions(connection, repo)
    from .work_exhaustion import episode_signature
    signature = episode_signature(snapshot)
    queued = False
    with connection:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
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
        if previous is None or previous[0] != signature:
            _cancel_pending_waiting(connection, repo)
        payload.update(signature=signature, generation=generation)
        current = is_current(connection, repo, 'work_waiting', payload)
        if current:
            connection.execute("UPDATE project_completion_cycles SET completed_at=NULL, generation=generation+1 "
                           "WHERE repo=? AND completed_at IS NOT NULL", (repo,))
            connection.execute(
                "UPDATE notification_outbox SET canceled_at=CURRENT_TIMESTAMP "
                "WHERE repo=? AND event_type='project_completed' AND sent_at IS NULL AND canceled_at IS NULL", (repo,),
            )
        if _enabled(connection) and current:
            key = _key('work_waiting', repo, str(generation))
            existing = connection.execute("SELECT 1 FROM notification_outbox WHERE event_key=?", (key,)).fetchone()
            _insert(connection, key, repo, 'work_waiting', payload)
            queued = existing is None
    from .delivery import flush_project_events
    flush_project_events(connection, repo)
    return queued


def enqueue_progress(connection, repo, kind, identity, **payload):
    """Persist a boundary observation in the owning state transaction."""
    if kind not in {"draft_decisions", "draft_decision", "pr_created", "plans_published"} or not _enabled(connection):
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
    project = connection.execute('SELECT origin FROM projects WHERE repo=?', (repo,)).fetchone()
    link = _checkpoint_link(project['origin'], revision) if project else None
    origin = link.split('/tree/', 1)[0] if link else None
    items = []
    drafts = []
    for path, plan in plans:
        if plan.get('draft'):
            drafts.append((path, plan))
            continue
        row = connection.execute(
            'SELECT title, depends_on FROM exec_plans WHERE repo=? AND plan_id=?',
            (repo, plan['id']),
        ).fetchone()
        if row is None:
            continue
        dependencies = json.loads(row['depends_on'])
        waiting = []
        for dependency in dependencies:
            state = connection.execute('SELECT status FROM exec_plans WHERE repo=? AND plan_id=?',
                                       (repo, dependency)).fetchone()
            if state is None or state['status'] != 'MERGED':
                waiting.append(dependency)
        items.append({'id': str(plan['id']), 'title': str(row['title']), 'source_path': path,
                      'url': f'{origin}/blob/{revision}/{quote(path, safe="/")}' if origin else None,
                      'waiting_for': waiting})
    if items:
        enqueue_progress(connection, repo, 'plans_published', str(revision),
                         publication_revision=str(revision), plans=items, silent=True,
                         status='실행 또는 선행 작업 대기')
    enqueue_draft_decisions(connection, repo, publications=(drafts, str(revision)))


def retain_unconfirmed_published_plans(connection, repo, event_key):
    """Continue only valid, unconfirmed items when a fixed batch becomes stale."""
    if not _enabled(connection):
        return
    row = connection.execute(
        "SELECT payload, rendered_text, confirmed_pieces FROM notification_outbox "
        "WHERE event_key=? AND repo=? AND event_type='plans_published' "
        "AND sent_at IS NULL AND canceled_at IS NULL", (event_key, repo),
    ).fetchone()
    if row is None:
        return
    try:
        payload = json.loads(row[0])
        revision = payload['publication_revision']
        plans = payload['plans']
        if not isinstance(plans, list):
            return
        rendered = row[1] if isinstance(row[1], str) else ''
        confirmed = ''.join(_pieces(rendered)[:int(row[2] or 0)])
        confirmed_elsewhere = confirmed_publication_keys(connection, repo, 'plans_published',
                                                         exclude_event_key=event_key)
        remaining = []
        for item in plans:
            if not isinstance(item, dict):
                continue
            if (item.get('id'), revision) in confirmed_elsewhere:
                continue
            marker = f"\n• {item.get('id')} · "
            position = rendered.find(marker)
            if position >= 0 and position + len(f"\n• {item.get('id')}") <= len(confirmed):
                continue
            if is_current(connection, repo, 'plans_published',
                          {'plans': [item], 'publication_revision': revision}):
                remaining.append(item)
        if remaining:
            payload['plans'] = remaining
            _insert(connection, _key('plans_published', repo, event_key), repo,
                    'plans_published', payload)
    except (AttributeError, KeyError, TypeError, ValueError):
        return


def enqueue_draft_decisions(connection, repo, *, publications=None):
    if not _enabled(connection):
        return
    ensure_schema(connection)
    previous = connection.execute(
        "SELECT event_key, payload, canceled_at, sent_at, rendered_text, confirmed_pieces FROM notification_outbox WHERE repo = ? "
        "AND event_type = 'draft_decisions' ORDER BY rowid DESC LIMIT 1", (repo,),
    ).fetchone()
    created = []
    if publications:
        entries, revision = publications
        project = connection.execute('SELECT origin FROM projects WHERE repo=?', (repo,)).fetchone()
        link = _checkpoint_link(project['origin'], revision) if project else None
        origin = link.split('/tree/', 1)[0] if link else None
        represented = set()
        for row in connection.execute("SELECT payload FROM notification_outbox WHERE repo=? AND event_type='draft_decisions'", (repo,)):
            try:
                saved = json.loads(row[0])
                represented.update((item['id'], item['publication_revision']) for item in saved['plans']
                                   if 'publication_revision' in item)
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        for path, plan in entries:
            plan_id = str(plan['id'])
            if (plan_id, revision) in represented:
                continue
            row = connection.execute('SELECT title FROM exec_plans WHERE repo=? AND plan_id=?',
                                     (repo, plan_id)).fetchone()
            item = {'id': plan_id, 'title': str(row['title']) if row else '', 'source_path': path,
                    'url': f'{origin}/blob/{revision}/{quote(path, safe="/")}' if origin else None,
                    'publication_revision': revision}
            if row and is_current(connection, repo, 'plans_published',
                                  {'plans': [item], 'publication_revision': revision}):
                created.append(item)
    if previous and not previous[2] and not previous[3]:
        try:
            payload = json.loads(previous[1])
        except (TypeError, ValueError):
            payload = None
        if not created and isinstance(payload, dict) and is_current(
                connection, repo, "draft_decisions", payload, event_key=str(previous[0])):
            return
        if isinstance(payload, dict) and isinstance(payload.get('plans'), list):
            rendered = previous[4] if isinstance(previous[4], str) else ''
            confirmed = ''.join(_pieces(rendered)[:int(previous[5] or 0)])
            confirmed_elsewhere = confirmed_publication_keys(
                connection, repo, 'draft_decisions', exclude_event_key=str(previous[0]))
            for item in payload['plans']:
                if not isinstance(item, dict) or not item.get('publication_revision'):
                    continue
                if (item.get('id'), item['publication_revision']) in confirmed_elsewhere:
                    continue
                marker = f"\n• {item.get('id')} · "
                position = rendered.find(marker)
                if position >= 0 and position + len(f"\n• {item.get('id')}") <= len(confirmed):
                    continue
                if is_current(connection, repo, 'plans_published', {
                        'plans': [item], 'publication_revision': item['publication_revision']}):
                    created.append({key: item[key] for key in (
                        'id', 'title', 'source_path', 'url', 'publication_revision', 'publication_path') if key in item})

    connection.execute(
        "UPDATE notification_outbox SET canceled_at = CURRENT_TIMESTAMP, delivery_started_at = NULL "
        "WHERE repo = ? AND event_type = 'draft_decisions' "
        "AND sent_at IS NULL AND canceled_at IS NULL", (repo,),
    )
    plans = draft_questions_for_notice(connection, repo)
    by_id = {item['id']: item for item in plans}
    for item in created:
        if item['id'] in by_id:
            by_id[item['id']]['publication_revision'] = item['publication_revision']
            by_id[item['id']]['publication_path'] = item.get('publication_path', item['source_path'])
        else:
            by_id[item['id']] = item
    plans = [by_id[plan_id] for plan_id in sorted(by_id)]
    if not plans:
        return
    decisions = [(plan['id'], plan.get('decision_identity'), plan.get('decision_revision'),
                  plan.get('publication_revision')) for plan in plans]
    identity = str(previous[0] if previous else "") + "\0" + json.dumps(decisions, ensure_ascii=False)
    enqueue_progress(connection, repo, "draft_decisions", identity,
                     plans=plans, status="사용자 판단 대기" if any(
                         item.get('decision_identity') for item in plans) else "draft 게시", silent=False)
