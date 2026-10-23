"""Durable general-planning publication intent and hash validation."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import json
import hashlib
import sqlite3

from loop_manager_internal.planning.planning_catalog import (
    PlanDeletion, PlannerValidationError, mutable_snapshot_token,
    snapshot_from_dict,
)
from loop_manager_internal.maintenance.maintenance_planning import PlanRevision
from .execplan_catalog_state import serialize_plan_metadata, worker_level


@dataclass(frozen=True)
class GeneralPlanningPublication:
    repo: str
    stage: str
    base_sha: str
    commit_sha: str
    snapshot_token: str
    specification_fingerprint: str
    planning_fingerprint: str
    manifest: str
    manifest_hash: str


def general_planning_publication(
    connection: sqlite3.Connection, repo: str
) -> GeneralPlanningPublication | None:
    row = connection.execute(
        "SELECT * FROM general_planning_publications WHERE repo = ?", (repo,)
    ).fetchone()
    if row is None:
        return None
    publication = GeneralPlanningPublication(
        repo=str(row["repo"]),
        stage=str(row["stage"]),
        base_sha=str(row["base_sha"]),
        commit_sha=str(row["commit_sha"]),
        snapshot_token=str(row["snapshot_token"]),
        specification_fingerprint=str(row["specification_fingerprint"]),
        planning_fingerprint=str(row["planning_fingerprint"]),
        manifest=str(row["manifest"]),
        manifest_hash=str(row["manifest_hash"]),
    )
    if hashlib.sha256(publication.manifest.encode("utf-8")).hexdigest() != (
        publication.manifest_hash
    ):
        raise PlannerValidationError(
            "보존된 general planning publication manifest hash가 다릅니다."
        )
    return publication


def record_general_planning_publication(
    connection: sqlite3.Connection, publication: GeneralPlanningPublication,
) -> GeneralPlanningPublication:
    repo = publication.repo
    expected = (
        "prepared",
        publication.base_sha,
        publication.commit_sha,
        publication.snapshot_token,
        publication.specification_fingerprint,
        publication.planning_fingerprint,
        publication.manifest_hash,
        publication.manifest,
    )
    from .runtime_state import _transaction
    with _transaction(connection):
        if (
            not isinstance(publication.manifest, str)
            or not isinstance(publication.manifest_hash, str)
            or hashlib.sha256(publication.manifest.encode("utf-8")).hexdigest()
            != publication.manifest_hash
        ):
            raise PlannerValidationError(
                "general planning publication manifest hash가 다릅니다."
            )
        # Import here because the publication transition also uses this state module.
        from loop_manager_internal.planning.planner_publication import (
            PlanningResultFromManifestEffects, _planning_result_from_manifest,
        )
        result = _planning_result_from_manifest(
            PlanningResultFromManifestEffects(
                mutable_snapshot_token=mutable_snapshot_token,
                snapshot_from_dict=snapshot_from_dict,
            ),
            publication.manifest,
        )
        snapshot = result.mutable_snapshot
        if snapshot is None or (
            snapshot.repo != repo
            or snapshot.active_sha != publication.base_sha
            or snapshot.token != publication.snapshot_token
        ):
            raise PlannerValidationError(
                "general planning publication과 mutable READY snapshot이 다릅니다."
            )
        current = general_planning_publication(connection, repo)
        if current is not None:
            actual = (
                current.stage,
                current.base_sha,
                current.commit_sha,
                current.snapshot_token,
                current.specification_fingerprint,
                current.planning_fingerprint,
                current.manifest_hash,
                current.manifest,
            )
            if actual != expected:
                raise PlannerValidationError(
                    "다른 general planning publication intent가 이미 있습니다."
                )
            return current
        connection.execute(
            """
            INSERT INTO general_planning_publications (
                repo, stage, base_sha, commit_sha, snapshot_token,
                specification_fingerprint, planning_fingerprint,
                manifest, manifest_hash
            ) VALUES (?, 'prepared', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo,
                publication.base_sha,
                publication.commit_sha,
                publication.snapshot_token,
                publication.specification_fingerprint,
                publication.planning_fingerprint,
                publication.manifest,
                publication.manifest_hash,
            ),
        )
    stored = general_planning_publication(connection, repo)
    assert stored is not None
    return stored


_MUTABLE_READY_EVIDENCE_FIELDS = (
    "active_sha",
    "validated_base_sha",
    "publication_remote_oid",
    "branch",
    "worktree",
    "pr_url",
    "pr_head_oid",
    "last_error",
    "blocked_fingerprint",
    "blocked_worktree_fingerprint",
    "blocked_review_fingerprint",
    "worker_session_id",
    "reviewer_session_id",
    "worker_result",
    "verify_result",
    "review_result",
    "blocked_reason",
    "guidance_plan_hash",
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


def _mutable_ready_cas_where() -> str:
    null_fields = " AND ".join(
        f"{field} IS NULL" for field in _MUTABLE_READY_EVIDENCE_FIELDS
    )
    return (
        "status = 'READY' AND ever_started = 0 "
        "AND workspace_owned = 0 AND retry_count = 0 "
        "AND pr_conflict_recovery = 0 "
        "AND verify_failure_history = '{}' "
        "AND review_failure_history = '{}' "
        "AND review_level_history = '{}' AND "
        + null_fields
    )


def apply_mutable_catalog_changes(
    connection: sqlite3.Connection, repo: str, *,
    deletions: tuple[PlanDeletion, ...], revisions: tuple[PlanRevision, ...],
    snapshot_token: str, plan_commit: str, content_hash: Callable[[str], str],
    draft_inputs: Callable | None = None, agent: bool = True,
) -> None:
    """Apply cache changes and cancellation audit in the caller's transaction."""
    cas = _mutable_ready_cas_where()
    for deletion in deletions:
        original = deletion.snapshot
        from .work_claims import cancellation_available
        row = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?", (repo, original.plan_id)).fetchone()
        if not cancellation_available(connection, repo, original.plan_id, row):
            raise PlannerValidationError("취소 전에 호출 예약·writer 종료·Git operation·수동·공유 자원 소유권을 확인할 수 없습니다.")
        from . import execplan_draft_state
        if agent and original.plan_id in execplan_draft_state.pending_ids(connection, repo):
            raise PlannerValidationError("사용자 판단 대기 draft를 삭제할 수 없습니다.")
        deletion_cas = cas
        preserved = None
        if original.execution_state and json.loads(original.execution_state).get("ever_started"):
            from loop_manager_internal.planning.planner_validation import planning_execution_state
            if (row is None or planning_execution_state(row) != original.execution_state or row["agent_role"]
                    or row["pr_url"] or row["pr_head_oid"] or row["status"] == "MERGED"):
                raise PlannerValidationError("취소 전에 작업의 writer·소유권·PR이 변경됐습니다.")
            if agent and not deletion.judgment:
                raise PlannerValidationError("시작한 작업의 철회 판단이 없습니다.")
            deletion_cas = "agent_role IS NULL AND pr_url IS NULL AND pr_head_oid IS NULL AND status != 'MERGED'"
            affected = [str(item[0]) for item in connection.execute("SELECT plan_id, depends_on FROM exec_plans WHERE repo=?", (repo,))
                        if item[0] != original.plan_id and original.plan_id in json.loads(item[1])]
            preserved = {"text": original.text, "row": dict(row), "affected": affected, "resolved": {},
                         "judgment": deletion.judgment,
                         "cleanup_pending": bool(row["branch"] or row["worktree"]),
                         "cleanup_reason": "취소한 소유 자원의 작업물·PR·writer 안전 재확인 필요"}
        checkpoint_audit = {}
        for table in ("execution_checkpoints", "blocked_checkpoints"):
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone():
                checkpoint = connection.execute(
                    f"SELECT * FROM {table} WHERE repo=? AND plan_id=?", (repo, original.plan_id),
                ).fetchone()
                if checkpoint is not None:
                    checkpoint_audit[table] = dict(checkpoint)
                    connection.execute(
                        f"DELETE FROM {table} WHERE repo=? AND plan_id=?", (repo, original.plan_id),
                    )
        changed = connection.execute(
            f"""
            DELETE FROM exec_plans
            WHERE repo = ? AND plan_id = ? AND path = ? AND title = ?
              AND sources = ? AND depends_on = ? AND priority = ?
              AND current_complexity IS ? AND current_risk IS ?
              AND content_hash = ?
              AND COALESCE(current_revision_sha, plan_sha) = ? AND {deletion_cas}
            """,
            (
                repo,
                original.plan_id,
                original.path,
                original.title,
                json.dumps(original.sources, ensure_ascii=False, separators=(",", ":")),
                json.dumps(original.depends_on, ensure_ascii=False, separators=(",", ":")),
                original.priority,
                original.current_complexity,
                original.current_risk,
                original.content_hash,
                original.plan_sha,
            ),
        )
        if changed.rowcount != 1:
            raise PlannerValidationError(
                "publication 직전 mutable READY deletion이 변경됐습니다: "
                f"{original.plan_id}"
            )
        connection.execute(
            """
            INSERT INTO execplan_cancellations (
                repo, plan_id, path, title, sources, depends_on,
                complexity, risk, priority, content_hash, plan_sha,
                deletion_commit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo,
                original.plan_id,
                original.path,
                original.title,
                json.dumps(original.sources, ensure_ascii=False, separators=(",", ":")),
                json.dumps(original.depends_on, ensure_ascii=False, separators=(",", ":")),
                original.complexity,
                original.risk,
                original.priority,
                original.content_hash,
                original.plan_sha,
                plan_commit,
            ),
        )
        execplan_draft_state.cancel(connection, repo, original.plan_id, plan_commit)
        from loop_manager_internal.notifications.incidents import cancel_plan_notifications
        cancel_plan_notifications(connection, repo, original.plan_id)
        from .maintenance_state import cycle_for_plan
        cycle = cycle_for_plan(connection, repo, original.plan_id)
        maintenance_audit = dict(cycle) if cycle is not None else None
        if preserved or deletion.judgment or checkpoint_audit or maintenance_audit:
            audit = preserved or {"text": original.text, "row": json.loads(original.execution_state or "{}"),
                                  "affected": [], "resolved": {}, "judgment": deletion.judgment}
            if checkpoint_audit:
                audit["checkpoints"] = checkpoint_audit
            if maintenance_audit:
                audit['maintenance_cycle'] = maintenance_audit
            connection.execute("UPDATE execplan_cancellations SET recovery_evidence=? WHERE repo=? AND plan_id=?",
                               (json.dumps(audit, ensure_ascii=False, sort_keys=True), repo, original.plan_id))
        project = connection.execute("SELECT phase, plan_path FROM projects WHERE repo=?", (repo,)).fetchone()
        if project and project["plan_path"] == original.path and project["phase"] != "PLANNING":
            from .runtime_state import RuntimeStateRepository
            RuntimeStateRepository(connection).clear(repo)

    staged_paths: dict[str, str] = {}
    for revision in revisions:
        original = revision.snapshot
        if revision.path == original.path:
            continue
        staged = (
            f"docs/plans/.planning-{snapshot_token[:12]}-"
            f"{original.plan_id}.md"
        )
        changed = connection.execute(
            f"""
            UPDATE exec_plans SET path = ?
            WHERE repo = ? AND plan_id = ? AND path = ? AND title = ?
              AND sources = ? AND depends_on = ? AND priority = ?
              AND current_complexity IS ? AND current_risk IS ?
              AND content_hash = ?
              AND COALESCE(current_revision_sha, plan_sha) = ? AND {cas}
            """,
            (
                staged,
                repo,
                original.plan_id,
                original.path,
                original.title,
                json.dumps(original.sources, ensure_ascii=False, separators=(",", ":")),
                json.dumps(original.depends_on, ensure_ascii=False, separators=(",", ":")),
                original.priority,
                original.current_complexity,
                original.current_risk,
                original.content_hash,
                original.plan_sha,
            ),
        )
        if changed.rowcount != 1:
            raise PlannerValidationError(
                "publication 직전 mutable READY rename이 변경됐습니다: "
                f"{original.plan_id}"
            )
        staged_paths[original.plan_id] = staged

    for revision in revisions:
        original = revision.snapshot
        plan = dict(revision.plan)
        if original.execution_state is not None:
            from loop_manager_internal.planning.planner_validation import planning_execution_state
            row = connection.execute("SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                                     (repo, original.plan_id)).fetchone()
            if row is None or planning_execution_state(row) != original.execution_state:
                raise PlannerValidationError(f"계획 갱신 중 실행 근거가 변경됐습니다: {original.plan_id}")
            revision_cas = ("status IN ('READY', 'IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING', 'PLAN_BLOCKED') "
                            "AND pr_url IS NULL AND pr_head_oid IS NULL AND pr_conflict_recovery = 0")
        else:
            revision_cas = cas
        sources, dependencies, priority = serialize_plan_metadata(plan)
        initial_level = worker_level(plan)
        expected_path = staged_paths.get(original.plan_id, original.path)
        changed = connection.execute(
            f"""
            UPDATE exec_plans
            SET path = ?, title = ?, sources = ?, depends_on = ?,
                priority = ?, content_hash = ?, current_revision_sha = ?,
                current_complexity = ?, current_risk = ?,
                initial_worker_level = ?, effective_worker_level =
                    CASE WHEN ever_started THEN MAX(effective_worker_level, ?) ELSE ? END
            WHERE repo = ? AND plan_id = ? AND path = ? AND title = ?
              AND sources = ? AND depends_on = ? AND priority = ?
              AND current_complexity IS ? AND current_risk IS ?
              AND content_hash = ?
              AND COALESCE(current_revision_sha, plan_sha) = ? AND {revision_cas}
            """,
            (
                revision.path,
                str(plan["title"]),
                sources,
                dependencies,
                priority,
                content_hash(revision.path),
                plan_commit,
                str(plan["complexity"]),
                str(plan["risk"]),
                initial_level,
                initial_level,
                initial_level,
                repo,
                original.plan_id,
                expected_path,
                original.title,
                json.dumps(original.sources, ensure_ascii=False, separators=(",", ":")),
                json.dumps(original.depends_on, ensure_ascii=False, separators=(",", ":")),
                original.priority,
                original.current_complexity,
                original.current_risk,
                original.content_hash,
                original.plan_sha,
            ),
        )
        if changed.rowcount != 1:
            raise PlannerValidationError(
                "publication 직전 mutable READY revision이 변경됐습니다: "
                f"{original.plan_id}"
            )
        from . import execplan_draft_state
        resolution = plan.get("_draft_resolution")
        if resolution:
            execplan_draft_state.resolve(connection, repo, revision.path, plan,
                content_hash(revision.path), plan_commit, resolution,
                inputs=draft_inputs(plan) if draft_inputs else None)
        else:
            execplan_draft_state.observe(connection, repo, revision.path, plan,
                content_hash(revision.path), plan_commit, agent=agent,
                inputs=draft_inputs(plan) if draft_inputs else None)


def delete_general_planning_publication(connection: sqlite3.Connection, repo: str) -> None:
    connection.execute("DELETE FROM general_planning_publications WHERE repo = ?", (repo,))


def retain_planning_after_publication(
    connection: sqlite3.Connection, repo: str, revision: str, *,
    processed_sha: str | None = None, specification_fingerprint: str | None = None,
    planning_fingerprint: str | None = None,
) -> None:
    """Keep the owned planning slot until external worktree cleanup completes."""
    changed = connection.execute(
        "UPDATE projects SET publication_remote_oid=?, "
        "processed_sha=COALESCE(?, processed_sha), "
        "specification_fingerprint=COALESCE(?, specification_fingerprint), "
        "planning_fingerprint=COALESCE(?, planning_fingerprint) "
        "WHERE repo=? AND phase='PLANNING' AND planning_workspace_owned=1 "
        "AND worktree IS NOT NULL AND planning_attempt_fingerprint IS NOT NULL",
        (revision, processed_sha, specification_fingerprint, planning_fingerprint, repo),
    )
    if changed.rowcount != 1:
        raise PlannerValidationError("게시 후 정리할 planning 소유 상태가 변경됐습니다.")
