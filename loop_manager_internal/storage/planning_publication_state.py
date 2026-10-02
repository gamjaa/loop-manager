"""Durable general-planning publication intent and hash validation."""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import json
import hashlib
import sqlite3

from loop_manager_internal.planning.planning_catalog import PlanDeletion, PlannerValidationError
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
    )
    with connection:
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
) -> None:
    """Apply cache changes and cancellation audit in the caller's transaction."""
    cas = _mutable_ready_cas_where()
    for deletion in deletions:
        original = deletion.snapshot
        changed = connection.execute(
            f"""
            DELETE FROM exec_plans
            WHERE repo = ? AND plan_id = ? AND path = ? AND title = ?
              AND sources = ? AND depends_on = ? AND priority = ?
              AND current_complexity IS ? AND current_risk IS ?
              AND content_hash = ?
              AND COALESCE(current_revision_sha, plan_sha) = ? AND {cas}
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
        sources, dependencies, priority = serialize_plan_metadata(plan)
        initial_level = worker_level(plan)
        expected_path = staged_paths.get(original.plan_id, original.path)
        changed = connection.execute(
            f"""
            UPDATE exec_plans
            SET path = ?, title = ?, sources = ?, depends_on = ?,
                priority = ?, content_hash = ?, current_revision_sha = ?,
                current_complexity = ?, current_risk = ?,
                initial_worker_level = ?, effective_worker_level = ?
            WHERE repo = ? AND plan_id = ? AND path = ? AND title = ?
              AND sources = ? AND depends_on = ? AND priority = ?
              AND current_complexity IS ? AND current_risk IS ?
              AND content_hash = ?
              AND COALESCE(current_revision_sha, plan_sha) = ? AND {cas}
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


def delete_general_planning_publication(connection: sqlite3.Connection, repo: str) -> None:
    connection.execute("DELETE FROM general_planning_publications WHERE repo = ?", (repo,))
