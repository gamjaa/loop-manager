"""SQLite connection and additive schema initialization.

Savepoints preserve caller transactions; repository migrations retain their
existing order and connection-local initialization marker.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3

from loop_manager_internal.runtime import project_registry
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import scoped_blocking, blocked_remote_observation
from . import (execplan_catalog_state, execplan_archive_state, requirement_archive_state,
               agent_invocation_state, validation_feedback_state, recovery_state,
               blocked_checkpoint_state, maintenance_state)


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    repo TEXT PRIMARY KEY,
    base_branch TEXT NOT NULL,
    processed_sha TEXT,
    provider TEXT NOT NULL DEFAULT 'codex',
    origin TEXT,
    registered INTEGER NOT NULL DEFAULT 1,
    agents_hash TEXT,
    agents_approved INTEGER NOT NULL DEFAULT 0,
    specification_fingerprint TEXT,
    planning_fingerprint TEXT,
    planning_attempt_fingerprint TEXT,
    planning_workspace_owned INTEGER NOT NULL DEFAULT 0,
    planning_retry_count INTEGER NOT NULL DEFAULT 0,
    planner_session_id TEXT,
    worker_session_id TEXT,
    worker_session_binding TEXT,
    worker_session_transition TEXT,
    reviewer_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
    review_result TEXT,
    definition_snapshot TEXT,
    worker_input_token TEXT,
    archive_token TEXT,
    verify_token TEXT,
    review_token TEXT,
    publication_token TEXT,
    revision_resume_status TEXT,
    planning_validation_error TEXT,
    planning_result_pending_validation INTEGER NOT NULL DEFAULT 0,
    planning_result_completed INTEGER NOT NULL DEFAULT 0,
    planning_result TEXT,
    planning_error TEXT,
    planning_error_fingerprint TEXT,
    active_sha TEXT,
    validated_base_sha TEXT,
    publication_remote_oid TEXT,
    pr_conflict_recovery INTEGER NOT NULL DEFAULT 0,
    pr_conflict_head_oid TEXT,
    pr_conflict_base_oid TEXT,
    plan_sha TEXT,
    phase TEXT,
    branch TEXT,
    worktree TEXT,
    plan_path TEXT,
    pr_url TEXT,
    pr_head_oid TEXT,
    last_error TEXT,
    agent_role TEXT,
    agent_snapshot TEXT,
    agent_input_fingerprint TEXT,
    agent_baseline TEXT,
    agent_recovery TEXT,
    agent_input_plan TEXT,
    agent_result_plan TEXT,
    agent_result_snapshot TEXT,
    agent_manager_snapshot TEXT
)
"""


CLEANUP_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanup_pending (
    repo TEXT NOT NULL,
    worktree TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    pr_head_oid TEXT,
    reason TEXT NOT NULL,
    workspace_owned INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, worktree),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


MERGED_WORKTREE_CLEANUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS merged_worktree_cleanups (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    worktree TEXT NOT NULL,
    branch TEXT NOT NULL,
    registration TEXT NOT NULL,
    pr_head_oid TEXT NOT NULL,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo, plan_id) REFERENCES exec_plans(repo, plan_id)
)
"""


MANAGER_BASE_FETCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS manager_base_fetches (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    refname TEXT NOT NULL,
    before_ref TEXT,
    target_oid TEXT NOT NULL,
    action_token TEXT,
    phase TEXT NOT NULL
)
"""


EXEC_PLANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_plans (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    sources TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    priority TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    current_complexity TEXT,
    current_risk TEXT,
    current_revision_sha TEXT,
    catalog_generation INTEGER NOT NULL DEFAULT 0,
    publication_path TEXT,
    publication_content_hash TEXT,
    publication_sha TEXT,
    status TEXT NOT NULL,
    plan_sha TEXT,
    active_sha TEXT,
    validated_base_sha TEXT,
    publication_remote_oid TEXT,
    pr_conflict_recovery INTEGER NOT NULL DEFAULT 0,
    pr_conflict_head_oid TEXT,
    pr_conflict_base_oid TEXT,
    branch TEXT,
    worktree TEXT,
    pr_url TEXT,
    pr_head_oid TEXT,
    last_error TEXT,
    blocked_fingerprint TEXT,
    blocked_worktree_fingerprint TEXT,
    blocked_review_fingerprint TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    worker_session_id TEXT,
    worker_session_binding TEXT,
    worker_session_transition TEXT,
    reviewer_session_id TEXT,
    worker_result TEXT,
    verify_result TEXT,
    review_result TEXT,
    definition_snapshot TEXT,
    worker_input_token TEXT,
    archive_token TEXT,
    verify_token TEXT,
    review_token TEXT,
    publication_token TEXT,
    revision_resume_status TEXT,
    verify_failure_history TEXT NOT NULL DEFAULT '{}',
    review_failure_history TEXT NOT NULL DEFAULT '{}',
    initial_worker_level INTEGER,
    effective_worker_level INTEGER,
    review_level_history TEXT NOT NULL DEFAULT '{}',
    blocked_reason TEXT,
    guidance_plan_hash TEXT,
    workspace_owned INTEGER NOT NULL DEFAULT 0,
    ever_started INTEGER NOT NULL DEFAULT 0,
    agent_role TEXT,
    agent_snapshot TEXT,
    agent_input_fingerprint TEXT,
    agent_baseline TEXT,
    agent_recovery TEXT,
    agent_input_plan TEXT,
    agent_result_plan TEXT,
    agent_result_snapshot TEXT,
    agent_manager_snapshot TEXT,
    PRIMARY KEY (repo, plan_id),
    UNIQUE (repo, path),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


GUIDANCE_APPROVALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS guidance_approvals (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    proposal_hash TEXT NOT NULL,
    target_path TEXT NOT NULL,
    scope TEXT NOT NULL,
    proposal_patch TEXT NOT NULL,
    proposal_patch_hash TEXT NOT NULL,
    proposal_line INTEGER NOT NULL,
    decision_line INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',
    decision_revision TEXT NOT NULL DEFAULT '',
    decision_hash TEXT,
    approved_patch TEXT,
    approved_patch_hash TEXT,
    baseline_content BLOB NOT NULL,
    baseline_mode INTEGER NOT NULL,
    expected_content_hash TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    invalidated_decision_hash TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id, proposal_id),
    FOREIGN KEY (repo, plan_id) REFERENCES exec_plans(repo, plan_id)
)
"""


EXEC_PLANS_ACTIVE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS one_active_execplan_per_project
ON exec_plans(repo)
WHERE status IN ('IMPLEMENTING', 'VERIFYING', 'REVIEWING', 'PUBLISHING')
"""


EXECPLAN_CANCELLATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS execplan_cancellations (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    sources TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    complexity TEXT NOT NULL,
    risk TEXT NOT NULL,
    priority TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    plan_sha TEXT NOT NULL,
    deletion_commit TEXT NOT NULL,
    cancelled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


GENERAL_PLANNING_PUBLICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS general_planning_publications (
    repo TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    snapshot_token TEXT NOT NULL,
    specification_fingerprint TEXT NOT NULL,
    planning_fingerprint TEXT NOT NULL,
    manifest TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repo) REFERENCES projects(repo)
)
"""


class StateConnection(sqlite3.Connection):
    """Keep completed schema initialization local to one database connection."""

    execplans_schema_ready = False


def _ensure_execplans_schema(connection: sqlite3.Connection) -> None:
    if isinstance(connection, StateConnection) and connection.execplans_schema_ready:
        return
    connection.execute(EXEC_PLANS_SCHEMA)
    execplan_catalog_state.ensure_schema(connection)
    execplan_archive_state.ensure_schema(connection)
    requirement_archive_state.ensure_schema(connection)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(exec_plans)")}
    if "blocked_fingerprint" not in columns:
        connection.execute("ALTER TABLE exec_plans ADD COLUMN blocked_fingerprint TEXT")
    if "blocked_worktree_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN blocked_worktree_fingerprint TEXT"
        )
    if "blocked_review_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN blocked_review_fingerprint TEXT"
        )
    if "workspace_owned" not in columns:
        connection.execute(
            "ALTER TABLE exec_plans ADD COLUMN workspace_owned "
            "INTEGER NOT NULL DEFAULT 0"
        )
    migrations = {
        "archive_resume_status": "TEXT",
        "reviewer_session_id": "TEXT",
        "worker_session_binding": "TEXT",
        "worker_session_transition": "TEXT",
        "review_result": "TEXT",
        "validated_base_sha": "TEXT",
        "publication_remote_oid": "TEXT",
        "pr_conflict_recovery": "INTEGER NOT NULL DEFAULT 0",
        "pr_conflict_head_oid": "TEXT",
        "pr_conflict_base_oid": "TEXT",
        "pr_head_oid": "TEXT",
        "verify_failure_history": "TEXT NOT NULL DEFAULT '{}'",
        "review_failure_history": "TEXT NOT NULL DEFAULT '{}'",
        "initial_worker_level": "INTEGER",
        "effective_worker_level": "INTEGER",
        "review_level_history": "TEXT NOT NULL DEFAULT '{}'",
        "blocked_reason": "TEXT",
        "guidance_plan_hash": "TEXT",
        "agent_role": "TEXT",
        "agent_snapshot": "TEXT",
        "agent_input_fingerprint": "TEXT",
        "agent_baseline": "TEXT",
        "agent_recovery": "TEXT",
        "agent_input_plan": "TEXT",
        "agent_result_plan": "TEXT",
        "agent_result_snapshot": "TEXT",
        "agent_manager_snapshot": "TEXT",
        "current_complexity": "TEXT",
        "current_risk": "TEXT",
        "current_revision_sha": "TEXT",
        "catalog_generation": "INTEGER NOT NULL DEFAULT 0",
        "publication_path": "TEXT",
        "publication_content_hash": "TEXT",
        "publication_sha": "TEXT",
        "definition_snapshot": "TEXT",
        "worker_input_token": "TEXT",
        "archive_token": "TEXT",
        "verify_token": "TEXT",
        "review_token": "TEXT",
        "publication_token": "TEXT",
        "revision_resume_status": "TEXT",
        "ever_started": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(
                f"ALTER TABLE exec_plans ADD COLUMN {column} {definition}"
            )
    if {
        "path",
        "content_hash",
        "plan_sha",
    }.issubset(columns):
        # RELEASE commits a standalone backfill, but preserves an existing
        # caller transaction. Re-run after interrupted column additions too.
        connection.execute("SAVEPOINT publication_backfill")
        try:
            connection.execute(
                """
                UPDATE exec_plans
                SET publication_path = COALESCE(publication_path, path),
                    publication_content_hash = COALESCE(
                        publication_content_hash, content_hash
                    ),
                    publication_sha = COALESCE(publication_sha, plan_sha),
                    current_revision_sha = COALESCE(current_revision_sha, plan_sha)
                WHERE publication_path IS NULL
                   OR publication_content_hash IS NULL
                   OR (publication_sha IS NULL AND plan_sha IS NOT NULL)
                   OR (current_revision_sha IS NULL AND plan_sha IS NOT NULL)
                """
            )
        except BaseException:
            connection.execute("ROLLBACK TO publication_backfill")
            connection.execute("RELEASE publication_backfill")
            raise
        else:
            connection.execute("RELEASE publication_backfill")
    # A prior process may have committed the column before backfilling rows.
    # Repeat the monotonic evidence check on every new connection.
    migrated_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(exec_plans)")
    }
    evidence = []
    if "status" in migrated_columns:
        evidence.append("status != 'READY'")
    for field in (
        "active_sha",
        "validated_base_sha",
        "publication_remote_oid",
        "pr_conflict_head_oid",
        "pr_conflict_base_oid",
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
    ):
        if field in migrated_columns:
            evidence.append(f"{field} IS NOT NULL")
    for field in (
        "pr_conflict_recovery",
        "retry_count",
        "workspace_owned",
    ):
        if field in migrated_columns:
            evidence.append(f"{field} != 0")
    for field in (
        "verify_failure_history",
        "review_failure_history",
        "review_level_history",
    ):
        if field in migrated_columns:
            evidence.append(f"{field} != '{{}}'")
    if evidence:
        # RELEASE commits a standalone repair without committing a caller's
        # existing transaction, matching the publication backfill above.
        connection.execute("SAVEPOINT ever_started_backfill")
        try:
            connection.execute(
                "UPDATE exec_plans SET ever_started = 1 WHERE ever_started = 0 "
                "AND (" + " OR ".join(evidence) + ")"
            )
        except BaseException:
            connection.execute("ROLLBACK TO ever_started_backfill")
            connection.execute("RELEASE ever_started_backfill")
            raise
        else:
            connection.execute("RELEASE ever_started_backfill")
    connection.execute(GUIDANCE_APPROVALS_SCHEMA)
    connection.execute(EXECPLAN_CANCELLATIONS_SCHEMA)
    connection.execute(GENERAL_PLANNING_PUBLICATIONS_SCHEMA)
    agent_invocation_state.ensure_schema(connection)
    validation_feedback_state.ensure_schema(connection)
    validation_feedback_state.ValidationFeedbackRepository(connection).migrate_legacy()
    scoped_blocking.ensure_schema(connection)
    if not getattr(connection, "execplans_schema_ready", False):
        recovery_state.ensure_schema(connection)
    blocked_checkpoint_state.ensure_schema(connection)
    blocked_remote_observation.ensure_schema(connection)
    connection.execute("DROP INDEX IF EXISTS one_active_execplan_per_project")
    connection.execute(EXEC_PLANS_ACTIVE_INDEX)
    scoped_blocking.migrate_legacy_blocks(connection)
    if isinstance(connection, StateConnection):
        connection.execplans_schema_ready = True


def connect_database(
    state_dir: Path,
    *,
    create: bool = False,
    run_migrations: bool = True,
) -> sqlite3.Connection:
    database = state_dir / "loop-manager.db"
    if not create and not database.is_file():
        raise RuntimeError(
            "상태 DB가 없습니다. 먼저 `loop_manager.py config init`을 실행하세요."
        )
    connection = sqlite3.connect(database, timeout=30.0, factory=StateConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    if create:
        connection.execute(SCHEMA)
        project_registry.ensure_registry_schema(connection)
        _ensure_execplans_schema(connection)
        _ensure_cleanup_pending_schema(connection)
        maintenance_state.ensure_schema(connection)
        notifications.ensure_schema(connection)
        blocked_checkpoint_state.ensure_schema(connection)
        blocked_remote_observation.ensure_schema(connection)
        connection.commit()
    else:
        try:
            connection.execute("SELECT 1 FROM projects LIMIT 1")
        except sqlite3.DatabaseError as error:
            connection.close()
            raise RuntimeError("상태 DB를 읽을 수 없습니다. 백업을 복원하거나 다시 초기화하세요.") from error
    if not run_migrations:
        # Scheduler workers inherit the schema initialized by the parent.
        connection.execplans_schema_ready = True
        return connection
    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    migrations = {
        "provider": "TEXT NOT NULL DEFAULT 'codex'",
        "specification_fingerprint": "TEXT",
        "plan_path": "TEXT",
        "planning_fingerprint": "TEXT",
        "planning_attempt_fingerprint": "TEXT",
        "planning_workspace_owned": "INTEGER NOT NULL DEFAULT 0",
        "planning_retry_count": "INTEGER NOT NULL DEFAULT 0",
        "planner_session_id": "TEXT",
        "plan_sha": "TEXT",
        "worker_session_id": "TEXT",
        "worker_session_binding": "TEXT",
        "worker_session_transition": "TEXT",
        "reviewer_session_id": "TEXT",
        "worker_result": "TEXT",
        "verify_result": "TEXT",
        "review_result": "TEXT",
        "planning_validation_error": "TEXT",
        "planning_result_pending_validation": "INTEGER NOT NULL DEFAULT 0",
        "planning_result_completed": "INTEGER NOT NULL DEFAULT 0",
        "planning_result": "TEXT",
        "planning_error": "TEXT",
        "planning_error_fingerprint": "TEXT",
        "validated_base_sha": "TEXT",
        "publication_remote_oid": "TEXT",
        "pr_conflict_recovery": "INTEGER NOT NULL DEFAULT 0",
        "pr_conflict_head_oid": "TEXT",
        "pr_conflict_base_oid": "TEXT",
        "pr_head_oid": "TEXT",
        "agent_role": "TEXT",
        "agent_snapshot": "TEXT",
        "agent_input_fingerprint": "TEXT",
        "agent_baseline": "TEXT",
        "agent_recovery": "TEXT",
        "agent_input_plan": "TEXT",
        "agent_result_plan": "TEXT",
        "agent_result_snapshot": "TEXT",
        "agent_manager_snapshot": "TEXT",
        "definition_snapshot": "TEXT",
        "worker_input_token": "TEXT",
        "archive_token": "TEXT",
        "verify_token": "TEXT",
        "review_token": "TEXT",
        "publication_token": "TEXT",
        "revision_resume_status": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {definition}")
    if not migrations.keys() <= columns:
        connection.commit()
    project_registry.ensure_registry_schema(connection)
    _ensure_execplans_schema(connection)
    _ensure_cleanup_pending_schema(connection)
    maintenance_state.ensure_schema(connection)
    notifications.ensure_schema(connection)
    blocked_checkpoint_state.ensure_schema(connection)
    blocked_remote_observation.ensure_schema(connection)
    connection.commit()
    # Repository-aware migrations belong in run_project, after persisted Agent
    # config has been checked against its pre-invocation snapshot.
    return connection


def _ensure_cleanup_pending_schema(connection: sqlite3.Connection) -> None:
    connection.execute(CLEANUP_PENDING_SCHEMA)
    connection.execute(MERGED_WORKTREE_CLEANUP_SCHEMA)
    connection.execute(MANAGER_BASE_FETCH_SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(cleanup_pending)")
    }
    if "workspace_owned" not in columns:
        connection.execute(
            "ALTER TABLE cleanup_pending ADD COLUMN workspace_owned "
            "INTEGER NOT NULL DEFAULT 1"
        )
    fetch_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(manager_base_fetches)")
    }
    if "action_token" not in fetch_columns:
        connection.execute(
            "ALTER TABLE manager_base_fetches ADD COLUMN action_token TEXT"
        )
