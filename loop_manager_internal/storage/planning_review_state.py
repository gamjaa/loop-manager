"""General planning comparisons and final specification receipts.

Attempts do not replace success evidence until the normal remote publication
transaction confirms them. Maintenance assessment and Requirement judgments
remain separate evidence.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3

from loop_manager_internal.storage import maintenance_state


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS planning_reviews (
        repo TEXT NOT NULL, slot TEXT NOT NULL,
        purpose TEXT NOT NULL, revision TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL, specification_fingerprint TEXT NOT NULL,
        work_cycle TEXT NOT NULL, comparison_revision TEXT,
        result TEXT, outcome TEXT, confirmed_revision TEXT,
        PRIMARY KEY (repo, slot)
    )""")
    fields = {row[1] for row in connection.execute("PRAGMA table_info(planning_reviews)")}
    for name in ("common_defect_snapshot", "common_defect_links"):
        if name not in fields:
            connection.execute(f"ALTER TABLE planning_reviews ADD COLUMN {name} TEXT NOT NULL DEFAULT '[]'")
    from loop_manager_internal.storage import common_defect_state
    common_defect_state.ensure_schema(connection)


def work_cycle(connection: sqlite3.Connection, repo: str) -> str:
    maintenance = maintenance_state.MaintenanceRepository(connection)
    evidence = json.dumps(validation_evidence(connection, repo), sort_keys=True)
    return json.dumps([maintenance.merged_count(repo), maintenance.get(repo).revision,
                       hashlib.sha256(evidence.encode()).hexdigest()])


def validation_evidence(connection: sqlite3.Connection, repo: str) -> list[dict]:
    """Expose confirmed Merge validation identities without copying full logs."""
    rows = maintenance_state.MaintenanceRepository(connection).plan_rows(repo)
    evidence = []
    for row in sorted(rows, key=lambda row: row["plan_id"]):
        if row["status"] != "MERGED":
            continue
        fields = set(row.keys())
        value = {key: row[key] if key in fields else None for key in (
            "plan_id", "pr_head_oid", "verify_token", "review_token")}
        for key in ("verify_result", "review_result"):
            result = row[key] if key in fields else None
            value[key + "_sha256"] = hashlib.sha256(str(result).encode()).hexdigest() if result is not None else None
        evidence.append(value)
    return evidence


def get(connection: sqlite3.Connection, repo: str, slot: str):
    ensure_schema(connection)
    return connection.execute(
        "SELECT * FROM planning_reviews WHERE repo = ? AND slot = ?", (repo, slot),
    ).fetchone()


def general_pending(connection: sqlite3.Connection, repo: str) -> bool:
    """Unconfirmed planning cannot supply cause-resolution evidence."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return bool(
        ('planning_reviews' in tables and connection.execute(
            "SELECT 1 FROM planning_reviews WHERE repo=? AND slot='attempt'", (repo,)).fetchone())
        or ('general_planning_publications' in tables and connection.execute(
            'SELECT 1 FROM general_planning_publications WHERE repo=?', (repo,)).fetchone()))


def reassessment(connection: sqlite3.Connection, repo: str, plan_id: str) -> dict:
    from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
    return (ExecutionCheckpointRepository(connection).get(repo, plan_id).snapshot or {}).get('planning_reassessment', {})


def reassessment_key(connection: sqlite3.Connection, repo: str, plan_id: str) -> str:
    return json.dumps(reassessment(connection, repo, plan_id), sort_keys=True, ensure_ascii=False)


def _queue_reassessments(connection, repo, attempt, confirmed_revision, revised_plan_ids):
    from loop_manager_internal.planning.planning_document_relevance import blocked_plan_inputs
    from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
    inputs = blocked_plan_inputs(attempt['result'], attempt['revision'])
    if not inputs and not revised_plan_ids:
        return
    # The same publication transaction has already applied revised catalog rows.
    rows = connection.execute("SELECT plan_id, current_revision_sha FROM exec_plans "
                              "WHERE repo=? AND status='PLAN_BLOCKED'", (repo,)).fetchall()
    checkpoints = ExecutionCheckpointRepository(connection)
    for row in rows:
        plan_id = str(row['plan_id'])
        if plan_id not in inputs and plan_id not in revised_plan_ids:
            continue
        current = checkpoints.get(repo, plan_id)
        previous = (current.snapshot or {}).get('planning_reassessment', {})
        related = {item['source']: item for item in previous.get('requirements', [])}
        related.update({item['source']: dict(item, revision=attempt['revision'],
                                           confirmed_revision=confirmed_revision)
                        for item in inputs.get(plan_id, [])})
        pending = dict(revision=attempt['revision'], confirmed_revision=confirmed_revision,
                       requirements=[related[path] for path in sorted(related)])
        if previous == pending:
            continue
        checkpoints.record(repo, plan_id, expected_generation=current.generation,
            operation=current.operation, expected_head=current.expected_head,
            target_base_oid=current.target_base_oid, observed_remote_oid=current.observed_remote_oid,
            snapshot={**(current.snapshot or {}), 'planning_reassessment': pending})


def current_final(connection: sqlite3.Connection, repo: str):
    final = get(connection, repo, "final")
    general = get(connection, repo, "general")
    if (final is None or general is None or general["purpose"] != "final"
            or any(final[key] != general[key] for key in (
                "revision", "confirmed_revision", "input_fingerprint",
                "specification_fingerprint", "work_cycle", "result", "outcome"))):
        return None
    return final


def begin(connection: sqlite3.Connection, repo: str, *, purpose: str,
          revision: str, input_fingerprint: str, specification_fingerprint: str,
          work_cycle: str, comparison_revision: str | None) -> None:
    ensure_schema(connection)
    connection.execute("""INSERT INTO planning_reviews
        (repo, slot, purpose, revision, input_fingerprint, specification_fingerprint,
         work_cycle, comparison_revision) VALUES (?, 'attempt', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, slot) DO UPDATE SET purpose=excluded.purpose,
        revision=excluded.revision, input_fingerprint=excluded.input_fingerprint,
        specification_fingerprint=excluded.specification_fingerprint,
        work_cycle=excluded.work_cycle, comparison_revision=excluded.comparison_revision,
        result=NULL, outcome=NULL, confirmed_revision=NULL,
        common_defect_snapshot='[]', common_defect_links='[]'""",
        (repo, purpose, revision, input_fingerprint, specification_fingerprint,
         work_cycle, comparison_revision))


def bind_common_defect_snapshot(connection, repo, revision, report_ids):
    changed = connection.execute(
        "UPDATE planning_reviews SET common_defect_snapshot=?,result=NULL,outcome=NULL,common_defect_links='[]' "
        "WHERE repo=? AND slot='attempt' AND revision=?",
        (json.dumps(sorted(report_ids)), repo, revision))
    if changed.rowcount != 1:
        raise RuntimeError("공통 결함 보고를 전달할 planning 입력이 변경됐습니다.")


def validate(connection: sqlite3.Connection, repo: str, revision: str,
             result: str, outcome: str, *, common_defect_links=None) -> None:
    from loop_manager_internal.storage import common_defect_state
    if common_defect_links:
        attempt = get(connection, repo, 'attempt')
        delivered = set(json.loads(attempt['common_defect_snapshot'])) if attempt is not None else set()
        if any(not set(link['reports']) <= delivered for link in common_defect_links):
            raise ValueError('planning 호출에 전달되지 않은 공통 결함 보고를 연결할 수 없습니다.')
        common_defect_state.check_reuse(connection, repo, common_defect_links)
    changed = connection.execute(
        "UPDATE planning_reviews SET result = ?, outcome = ?, common_defect_links = ? "
        "WHERE repo = ? AND slot = 'attempt' AND revision = ?",
        (result, outcome, json.dumps(common_defect_links or [], sort_keys=True), repo, revision),
    )
    if changed.rowcount != 1:
        raise RuntimeError("일반 planning 검토 입력이 변경됐습니다.")


def confirm(connection: sqlite3.Connection, repo: str, reviewed_revision: str,
            confirmed_revision: str, *, revised_plan_ids=()) -> None:
    attempt = get(connection, repo, "attempt")
    if attempt is None or attempt["revision"] != reviewed_revision:
        return  # Legacy publication recovery has no review receipt.
    if attempt["result"] is None or attempt["outcome"] is None:
        raise RuntimeError("검증되지 않은 일반 planning 검토를 확정할 수 없습니다.")
    if attempt['purpose'] != 'requirement':
        from loop_manager_internal.storage import common_defect_state
        try:
            common_defect_state.confirm(connection, repo, reviewed_revision, confirmed_revision,
                                        json.loads(attempt['common_defect_links']))
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        _queue_reassessments(connection, repo, attempt, confirmed_revision, revised_plan_ids)
    slots = ["requirement"] if attempt["purpose"] == "requirement" else ["general"]
    if attempt["purpose"] == "final":
        slots.append("final")
    for slot in slots:
        connection.execute("""INSERT INTO planning_reviews
            (repo, slot, purpose, revision, input_fingerprint, specification_fingerprint,
             work_cycle, comparison_revision, result, outcome, confirmed_revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, slot) DO UPDATE SET purpose=excluded.purpose,
            revision=excluded.revision, input_fingerprint=excluded.input_fingerprint,
            specification_fingerprint=excluded.specification_fingerprint,
            work_cycle=excluded.work_cycle, comparison_revision=excluded.comparison_revision,
            result=excluded.result, outcome=excluded.outcome,
            confirmed_revision=excluded.confirmed_revision""",
            (repo, slot, attempt["purpose"], attempt["revision"],
             attempt["input_fingerprint"], attempt["specification_fingerprint"],
             attempt["work_cycle"], attempt["comparison_revision"],
             attempt["result"], attempt["outcome"], confirmed_revision))
        connection.execute("UPDATE planning_reviews SET common_defect_snapshot=?,common_defect_links=? "
                           "WHERE repo=? AND slot=?",
                           (attempt['common_defect_snapshot'], attempt['common_defect_links'], repo, slot))
    connection.execute("DELETE FROM planning_reviews WHERE repo = ? AND slot = 'attempt'", (repo,))
