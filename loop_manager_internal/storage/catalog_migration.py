"""Conservative legacy catalog migration using injected revision/merge proof.

The caller chooses transaction ownership; runtime transitions continue through
the runtime repository and catalog audit through the catalog repository.
"""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3

from loop_manager_internal.catalog import execplan_catalog
from . import execplan_catalog_state, runtime_state
from .runtime_state import ACTIVE_STATUSES as ACTIVE_EXECPLAN_STATUSES
from .execplan_catalog_state import serialize_plan_metadata, worker_level


@dataclass(frozen=True)
class CatalogMigrationEffects:
    revision_execplan_snapshot: Callable
    pr_for_branch: Callable
    _current_execplan_sha: Callable
    _LegacyCatalogMergeProof: Callable


def _insert_recovered_execplan(
    connection: sqlite3.Connection,
    repo: str,
    entry: execplan_catalog.ExecPlanCatalogEntry,
    *,
    path: str,
    content_hash: str,
    status: str,
    plan_sha: str | None,
    pr_url: str | None = None,
    project: sqlite3.Row | None = None,
) -> None:
    existing = connection.execute(
        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
        (repo, entry.plan_id),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["publication_path"] or existing["path"]) != path
            or str(
                existing["publication_content_hash"] or existing["content_hash"]
            ) != content_hash
            or (
                (existing["publication_sha"] or existing["plan_sha"])
                and plan_sha
                and str(existing["publication_sha"] or existing["plan_sha"])
                != plan_sha
            )
        ):
            raise RuntimeError(
                f"복구된 ExecPlan identity가 기존 catalog와 충돌합니다: {entry.plan_id}"
            )
        if status == "MERGED" and str(existing["status"]) != "MERGED":
            runtime_state.RuntimeStateRepository(connection).update_plan(
                repo,
                entry.plan_id,
                plan_values={"status": "MERGED"},
                enqueue=False,
            )
        return

    plan = entry.metadata()
    sources, dependencies, priority = serialize_plan_metadata(plan)
    initial_level = worker_level(plan)
    is_active = project is not None
    connection.execute(
        """
        INSERT INTO exec_plans (
            repo, plan_id, path, title, sources, depends_on, priority,
            content_hash, current_complexity, current_risk,
            current_revision_sha, publication_path,
            publication_content_hash, publication_sha,
            status, plan_sha, active_sha, branch, worktree,
            pr_url, last_error, worker_session_id, worker_result, verify_result,
            initial_worker_level, effective_worker_level, workspace_owned
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo,
            entry.plan_id,
            path,
            entry.title,
            sources,
            dependencies,
            priority,
            content_hash,
            entry.complexity,
            entry.risk,
            plan_sha,
            path,
            content_hash,
            plan_sha,
            status,
            plan_sha,
            project["active_sha"] if is_active else None,
            project["branch"] if is_active else None,
            project["worktree"] if is_active else None,
            project["pr_url"] if is_active else pr_url,
            project["last_error"] if is_active else None,
            project["worker_session_id"] if is_active else None,
            project["worker_result"] if is_active else None,
            project["verify_result"] if is_active else None,
            initial_level,
            initial_level,
            1 if is_active else 0,
        ),
    )


def _quarantine_unverified_historical_runtime(
    connection: sqlite3.Connection,
    repo: str,
    recovery: execplan_catalog_state.CatalogRecoveryResult,
) -> None:
    """Keep an archived diagnostic from resuming an old active runtime row."""

    diagnostic_ids = {item.plan_id for item in recovery.diagnostics}
    if not diagnostic_ids:
        return
    repository = runtime_state.RuntimeStateRepository(connection)
    project = connection.execute(
        "SELECT plan_path FROM projects WHERE repo = ?", (repo,)
    ).fetchone()
    projected_path = str(project["plan_path"] or "") if project else ""
    for plan_id in sorted(diagnostic_ids):
        row = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None or str(row["status"]) not in ACTIVE_EXECPLAN_STATUSES:
            continue
        plan_values = {
            "status": "PLAN_BLOCKED",
            "archive_resume_status": str(row["status"]),
        }
        if projected_path == str(row["path"]):
            repository.transition(
                repo,
                plan_id,
                plan_values=plan_values,
                project_values=runtime_state.clear_project_values(),
                expected_status=str(row["status"]),
                enqueue=False,
            )
            projected_path = ""
        else:
            repository.update_plan(
                repo,
                plan_id,
                plan_values=plan_values,
                expected_status=str(row["status"]),
                enqueue=False,
            )


def _apply_execplan_catalog(
    connection: sqlite3.Connection,
    *,
    effects: CatalogMigrationEffects,
    reconcile_history: bool,
    target_repo: str | None,
    manage_transaction: bool = True,
    pinned_catalog: execplan_catalog_state.PinnedCatalog | None = None,
) -> None:
    phase_status = {
        "WORKING": "IMPLEMENTING",
        "VERIFYING": "VERIFYING",
        "REVIEWING": "REVIEWING",
        "PUBLISHING": "PUBLISHING",
        "PR_OPEN": "PR_OPEN",
        "BLOCKED": "PLAN_BLOCKED",
        "PLAN_BLOCKED": "PLAN_BLOCKED",
    }
    query = "SELECT * FROM projects ORDER BY repo"
    parameters: tuple[str, ...] = ()
    if target_repo is not None:
        query = "SELECT * FROM projects WHERE repo = ?"
        parameters = (target_repo,)
    projects = connection.execute(query, parameters).fetchall()

    with connection if manage_transaction else contextlib.nullcontext():
        for project in projects:
            planning = project["phase"] == "PLANNING"
            if planning and (not reconcile_history or pinned_catalog is None):
                continue
            root = Path(str(project["repo"]))
            revision = f"refs/remotes/origin/{project['base_branch']}"
            if pinned_catalog is not None:
                revision = pinned_catalog.revision_sha
                snapshot = pinned_catalog.snapshot
            else:
                snapshot = effects.revision_execplan_snapshot(
                    root, revision,
                    archive_conflict_plan_ids=execplan_catalog_state._merged_execplan_ids(
                        connection, str(project["repo"])
                    ),
                )
            active_path = str(project["plan_path"] or "")
            rows = connection.execute(
                "SELECT plan_id, path, content_hash, plan_sha, status, "
                "publication_path, publication_content_hash, publication_sha "
                "FROM exec_plans WHERE repo = ?",
                (project["repo"],),
            ).fetchall()
            stored = {str(row["plan_id"]): execplan_catalog_state.publication_identity(row)
                      for row in rows}
            historical_ids = {
                str(row["plan_id"])
                for row in connection.execute(
                    "SELECT plan_id FROM execplan_catalog_history WHERE repo = ?",
                    (project["repo"],),
                )
            }
            catalog_initialized = connection.execute(
                "SELECT 1 FROM execplan_catalog_revisions WHERE repo = ? "
                "UNION SELECT 1 FROM execplan_catalog_views WHERE repo = ?",
                (project["repo"], project["repo"]),
            ).fetchone() is not None
            missing_stored_ids = set(stored) - snapshot.observed_ids
            legacy_backfill_allowed = (
                not catalog_initialized and not missing_stored_ids
            )
            unknown_active_ids = set(snapshot.active) - set(stored) - historical_ids
            unknown_archived_ids = (
                set(snapshot.archived) - set(stored) - historical_ids
            )
            unknown_ids = sorted(unknown_active_ids | unknown_archived_ids)
            if missing_stored_ids and not planning:
                missing_id = sorted(missing_stored_ids)[0]
                if unknown_ids:
                    replacement_id = unknown_ids[0]
                    raise execplan_catalog.ExecPlanCatalogError(
                        "원격 ExecPlan ID 변경을 기존 실행 이력과 하나로 "
                        f"대응할 수 없습니다: {missing_id} -> {replacement_id}",
                        code="ambiguous-id-change",
                        path=snapshot.all[replacement_id].path,
                        related_paths=(stored[missing_id].path,),
                    )
                raise execplan_catalog.ExecPlanCatalogError(
                    "SQLite ExecPlan에 대응하는 원격 파일이 없습니다: "
                    f"{missing_id}",
                    code="missing-plan",
                    path=stored[missing_id].path,
                )
            if catalog_initialized and unknown_active_ids and not planning:
                unknown_id = sorted(unknown_active_ids)[0]
                raise execplan_catalog.ExecPlanCatalogError(
                    "원격 ExecPlan ID를 SQLite 실행 이력과 대응할 수 없습니다: "
                    f"{unknown_id}",
                    code="unknown-plan-id",
                    path=snapshot.all[unknown_id].path,
                )
            catalog_repository = execplan_catalog_state.CatalogStateRepository(
                connection, ensure=False
            )
            runtime_repository = runtime_state.RuntimeStateRepository(connection)
            # Planning owns its workspace; only pinned archive recovery may run alongside it.
            active_entries = () if planning else snapshot.active.values()
            for entry in active_entries:
                row = connection.execute(
                    "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                    (project["repo"], entry.plan_id),
                ).fetchone()
                if (
                    row is not None
                    and row["status"] == "PLAN_BLOCKED"
                    and row["archive_resume_status"] in ACTIVE_EXECPLAN_STATUSES
                    and row["path"] == entry.active_path
                    and row["content_hash"] == entry.content_hash
                    and project["phase"] is None
                    and runtime_repository.active_row(str(project["repo"])) is None
                ):
                    runtime_repository.update_plan(
                        str(project["repo"]), entry.plan_id,
                        plan_values={
                            "status": row["archive_resume_status"],
                            "archive_resume_status": None,
                        },
                        expected_status="PLAN_BLOCKED", enqueue=False,
                    )
                    runtime_repository.hydrate(str(project["repo"]), entry.plan_id)
            catalog_repository.mark_active(str(project["repo"]), snapshot.active)

            for entry in active_entries:
                is_active = (
                    entry.path == active_path
                    and str(project["phase"] or "") in phase_status
                )
                if not is_active and not reconcile_history:
                    continue
                identity = stored.get(entry.plan_id)
                if identity is not None:
                    continue
                if (
                    not legacy_backfill_allowed
                    and entry.plan_id not in historical_ids
                ):
                    # A newly observed ID is a reconciliation diagnostic, not
                    # legacy state that may be registered ahead of validation.
                    continue
                pull_request = None
                if is_active:
                    status = phase_status[str(project["phase"])]
                    plan_sha = str(project["plan_sha"] or "") or None
                else:
                    pull_request = effects.pr_for_branch(
                        root, f"loop-manager/{entry.plan_id}"
                    )
                    if pull_request is None:
                        status = "READY"
                    elif pull_request["state"] == "MERGED" or pull_request.get("mergedAt"):
                        status = "MERGED"
                    else:
                        raise RuntimeError(
                            "Legacy ExecPlan의 PR 상태를 자동 이관할 수 없습니다: "
                            f"{entry.plan_id} ({pull_request['state']})"
                        )
                    plan_sha = effects._current_execplan_sha(
                        root, revision, entry
                    )
                _insert_recovered_execplan(
                    connection,
                    str(project["repo"]),
                    entry,
                    path=entry.active_path,
                    content_hash=entry.content_hash,
                    status=status,
                    plan_sha=plan_sha,
                    pr_url=(str(pull_request["url"]) if pull_request else None),
                    project=(project if is_active else None),
                )

            if not reconcile_history:
                continue
            for entry in snapshot.archived.values():
                if entry.plan_id not in stored:
                    execplan_catalog_state.insert_document_fallback(
                        connection, str(project["repo"]), entry,
                        snapshot.revision_sha or revision,
                    )
                    row = connection.execute(
                        "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
                        (project["repo"], entry.plan_id),
                    ).fetchone()
                    stored[entry.plan_id] = execplan_catalog_state.publication_identity(row)
            recovery_snapshot = execplan_catalog.ExecPlanCatalogSnapshot(
                active=snapshot.active,
                archived={
                    plan_id: entry
                    for plan_id, entry in snapshot.archived.items()
                    if legacy_backfill_allowed
                    or plan_id in stored
                    or plan_id in historical_ids
                },
                revision_sha=snapshot.revision_sha,
            )
            recovery = execplan_catalog_state.CatalogRecoveryService(
                catalog_repository,
                effects._LegacyCatalogMergeProof(root, revision, stored),
            ).reconcile(str(project["repo"]), recovery_snapshot, stored)
            _quarantine_unverified_historical_runtime(
                connection, str(project["repo"]), recovery
            )
            for recovered in recovery.merged:
                _insert_recovered_execplan(
                    connection,
                    str(project["repo"]),
                    recovered.entry,
                    path=recovered.original_path,
                    content_hash=recovered.original_content_hash,
                    status="MERGED",
                    plan_sha=recovered.plan_sha,
                    pr_url=recovered.proof.pr_url,
                )


def _migration_state(connection, repo):
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    state = []
    for (name,) in tables:
        quoted = '"' + name.replace('"', '""') + '"'
        columns = {row[1] for row in connection.execute('PRAGMA table_info(' + quoted + ')')}
        if 'repo' in columns:
            rows = connection.execute('SELECT * FROM ' + quoted + (' WHERE repo=?' if repo is not None else ''),
                                      (repo,) if repo is not None else ()).fetchall()
            state.append((name, sorted((tuple(row) for row in rows), key=repr)))
    return state


def _migrate_execplan_catalog(connection, *, effects, reconcile_history, target_repo,
                             manage_transaction=True, pinned_catalog=None, prepare_only=False,
                             prepared=None, registration=None):
    """Observe legacy Git/PR effects on an independent snapshot, then CAS the live rows."""
    from .db_access import CoordinatedConnection
    if not isinstance(connection, CoordinatedConnection) and not prepare_only and prepared is None:
        # 독립 SQLite 호출자는 기존의 명시적 transaction 소유권을 유지한다.
        return _apply_execplan_catalog(connection, effects=effects, reconcile_history=reconcile_history,
            target_repo=target_repo, manage_transaction=manage_transaction, pinned_catalog=pinned_catalog)
    if prepared is None:
        if connection.in_transaction:
            raise RuntimeError('legacy catalog 관측 전에 DB transaction을 종료해야 합니다.')
        clone = sqlite3.connect(':memory:')
        clone.row_factory = sqlite3.Row
        try:
            connection.backup(clone)
            for name, sql in connection.execute("SELECT name,sql FROM sqlite_temp_master WHERE type='table'"):
                clone.execute(sql.replace("CREATE TABLE", "CREATE TEMP TABLE", 1))
                quoted = '"' + name.replace('"', '""') + '"'
                rows = connection.execute('SELECT * FROM temp.' + quoted).fetchall()
                if rows:
                    clone.executemany('INSERT INTO temp.' + quoted + ' VALUES (' + ','.join('?' for _ in rows[0]) + ')', rows)
            if registration is not None:
                from loop_manager_internal.runtime import project_registry
                project_registry.add_project(clone, registration)
            expected = _migration_state(clone, target_repo)
            cache = {}
            sealed = [False]
            def memo(name, function):
                def call(*args, **kwargs):
                    key = (name, repr(args), repr(kwargs))
                    if key not in cache:
                        if sealed[0]:
                            raise RuntimeError('legacy catalog 관측 이후 입력이 변경됐습니다.')
                        try:
                            cache[key] = (True, function(*args, **kwargs))
                        except Exception as error:
                            cache[key] = (False, error)
                    success, value = cache[key]
                    if not success:
                        raise value
                    return value
                return call
            class ObservedProof:
                def __init__(self, proof):
                    self.proof_for = memo(('proof_for', id(proof)), proof.proof_for)
                    self.verify_stored = memo(('verify_stored', id(proof)), proof.verify_stored)
            def proof(*args):
                return ObservedProof(effects._LegacyCatalogMergeProof(*args))
            observed = replace(effects,
                revision_execplan_snapshot=memo('snapshot', effects.revision_execplan_snapshot),
                pr_for_branch=memo('pr', effects.pr_for_branch),
                _current_execplan_sha=memo('sha', effects._current_execplan_sha),
                _LegacyCatalogMergeProof=memo('proof', proof))
            _apply_execplan_catalog(clone, effects=observed, reconcile_history=reconcile_history,
                                   target_repo=target_repo, pinned_catalog=pinned_catalog)
            sealed[0] = True
            prepared = (expected, observed)
        finally:
            clone.close()
    if prepare_only:
        return prepared
    from .runtime_state import _transaction
    with _transaction(connection):
        if _migration_state(connection, target_repo) != prepared[0]:
            raise RuntimeError('legacy catalog 적용 전 DB 상태가 변경됐습니다.')
        return _apply_execplan_catalog(connection, effects=prepared[1], reconcile_history=reconcile_history,
            target_repo=target_repo, manage_transaction=False, pinned_catalog=pinned_catalog)
