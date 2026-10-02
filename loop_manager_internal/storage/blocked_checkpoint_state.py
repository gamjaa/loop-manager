"""SQLite repository for durable PLAN_BLOCKED checkpoint progress."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import sqlite3
from loop_manager_internal.notifications import notifications

from loop_manager_internal.recovery.blocked_checkpoint import (
    CheckpointPhase,
    CheckpointRequest,
    PreparedCheckpoint,
    checkpoint_identity,
    diagnostic_branch,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS blocked_checkpoints (
    repo TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    request_identity TEXT NOT NULL,
    phase TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    evidence TEXT NOT NULL,
    branch TEXT,
    worktree TEXT,
    expected_remote_oid TEXT,
    remote_observed INTEGER NOT NULL DEFAULT 0,
    commit_identity TEXT,
    identity_reason_code TEXT,
    identity_evidence TEXT,
    tree_oid TEXT,
    local_oid TEXT,
    remote_oid TEXT,
    published_oid TEXT,
    accepted_remote_oid TEXT,
    pending_remote_oid TEXT,
    inspected_remote_oid TEXT,
    inspected_input TEXT,
    inspection_reason TEXT,
    inspection_retryable INTEGER NOT NULL DEFAULT 0,
    rebase_recovery INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    unpublished_reason TEXT,
    retryable INTEGER NOT NULL DEFAULT 0,
    publication_history TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, plan_id),
    FOREIGN KEY (repo, plan_id) REFERENCES exec_plans(repo, plan_id)
)
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    connection.execute("""CREATE TABLE IF NOT EXISTS blocked_diagnostic_publications (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, ref TEXT NOT NULL,
        original_branch TEXT NOT NULL, base_oid TEXT NOT NULL,
        head_oid TEXT NOT NULL, tree_oid TEXT NOT NULL, cause TEXT NOT NULL,
        confirmed_oid TEXT, PRIMARY KEY (repo, plan_id, ref)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS project_checkpoint_results (
        repo TEXT NOT NULL, plan_id TEXT NOT NULL, cause_identity TEXT NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY (repo, plan_id)
    )""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(blocked_checkpoints)")}
    for name in (
        "published_oid", "accepted_remote_oid", "pending_remote_oid",
        "inspected_remote_oid", "inspected_input", "inspection_reason",
        "identity_reason_code", "identity_evidence",
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE blocked_checkpoints ADD COLUMN {name} TEXT")
    if "inspection_retryable" not in columns:
        connection.execute(
            "ALTER TABLE blocked_checkpoints ADD COLUMN inspection_retryable "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "publication_history" not in columns:
        connection.execute("ALTER TABLE blocked_checkpoints ADD COLUMN publication_history TEXT NOT NULL DEFAULT '[]'")


def request_identity(
    repo: str,
    plan_id: str,
    reason_code: str,
    evidence: str,
    branch: str | None,
    worktree: str | None,
    release_fingerprint: str | None = None,
) -> str:
    value = "\0".join(
        (
            repo,
            plan_id,
            reason_code,
            evidence,
            branch or "",
            worktree or "",
            release_fingerprint or "",
        )
    )
    return hashlib.sha256(value.encode()).hexdigest()


class BlockedCheckpointRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)

    def request(
        self,
        *,
        repo: str,
        plan_id: str,
        reason_code: str,
        evidence: str,
        branch: str | None,
        worktree: str | None,
        expected_remote_oid: str | None,
        rebase_recovery: bool,
        release_fingerprint: str | None = None,
    ) -> CheckpointRequest:
        identity = request_identity(
            repo,
            plan_id,
            reason_code,
            evidence,
            branch,
            worktree,
            release_fingerprint,
        )
        current = self.connection.execute(
            "SELECT request_identity FROM blocked_checkpoints "
            "WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if current is None or str(current[0]) != identity:
            self.connection.execute(
                """
                INSERT INTO blocked_checkpoints (
                    repo, plan_id, request_identity, phase, reason_code,
                    evidence, branch, worktree, expected_remote_oid,
                    remote_observed, rebase_recovery
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, plan_id) DO UPDATE SET
                    request_identity = excluded.request_identity,
                    phase = excluded.phase,
                    reason_code = excluded.reason_code,
                    evidence = excluded.evidence,
                    branch = excluded.branch,
                    worktree = excluded.worktree,
                    expected_remote_oid = excluded.expected_remote_oid,
                    remote_observed = excluded.remote_observed,
                    commit_identity = NULL,
                    identity_reason_code = NULL,
                    identity_evidence = NULL,
                    tree_oid = NULL,
                    local_oid = NULL,
                    remote_oid = NULL,
                    published_oid = NULL,
                    accepted_remote_oid = NULL,
                    pending_remote_oid = NULL,
                    inspected_remote_oid = NULL,
                    inspected_input = NULL,
                    inspection_reason = NULL,
                    inspection_retryable = 0,
                    rebase_recovery = excluded.rebase_recovery,
                    result = NULL,
                    unpublished_reason = NULL,
                    retryable = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    repo,
                    plan_id,
                    identity,
                    CheckpointPhase.REQUESTED.value,
                    reason_code,
                    evidence,
                    branch,
                    worktree,
                    expected_remote_oid,
                    int(expected_remote_oid is not None),
                    int(rebase_recovery),
                ),
            )
        return self.get(repo, plan_id)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CheckpointRequest:
        return CheckpointRequest(
            repo=str(row["repo"]),
            plan_id=str(row["plan_id"]),
            request_identity=str(row["request_identity"]),
            phase=CheckpointPhase(str(row["phase"])),
            reason_code=str(row["reason_code"]),
            evidence=str(row["evidence"]),
            branch=str(row["branch"]) if row["branch"] is not None else None,
            worktree=str(row["worktree"]) if row["worktree"] is not None else None,
            expected_remote_oid=(
                str(row["expected_remote_oid"])
                if row["expected_remote_oid"] is not None
                else None
            ),
            remote_observed=bool(row["remote_observed"]),
            commit_identity=(
                str(row["commit_identity"])
                if row["commit_identity"] is not None
                else None
            ),
            tree_oid=str(row["tree_oid"]) if row["tree_oid"] is not None else None,
            local_oid=str(row["local_oid"]) if row["local_oid"] is not None else None,
            remote_oid=(
                str(row["remote_oid"]) if row["remote_oid"] is not None else None
            ),
            rebase_recovery=bool(row["rebase_recovery"]),
        )

    def get(self, repo: str, plan_id: str) -> CheckpointRequest:
        row = self.connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"checkpoint request를 찾을 수 없습니다: {plan_id}")
        return self._from_row(row)

    def pending(self, repo: str) -> tuple[CheckpointRequest, ...]:
        rows = self.connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE repo = ? "
            "AND phase IN (?, ?, ?, ?) ORDER BY plan_id",
            (
                repo,
                CheckpointPhase.REQUESTED.value,
                CheckpointPhase.PREPARED.value,
                CheckpointPhase.LOCAL_COMMITTED.value,
                CheckpointPhase.RETRY_PENDING.value,
            ),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def _update(self, request: CheckpointRequest, **values: object) -> None:
        if not values:
            return
        assignments = ", ".join(f"{field} = ?" for field in values)
        changed = self.connection.execute(
            f"UPDATE blocked_checkpoints SET {assignments}, "
            "updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND plan_id = ? "
            "AND request_identity = ?",
            (*values.values(), request.repo, request.plan_id, request.request_identity),
        )
        if changed.rowcount != 1:
            raise RuntimeError("checkpoint request identity가 변경됐습니다.")

    def record_prepared(
        self, request: CheckpointRequest, prepared: PreparedCheckpoint
    ) -> None:
        with self.connection:
            row = self.connection.execute(
                'SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
                (request.repo, request.plan_id)).fetchone()
            if row is not None and row['published_oid'] and row['commit_identity']:
                publication = {key: row[key] for key in (
                    'published_oid', 'accepted_remote_oid', 'commit_identity', 'tree_oid',
                    'identity_reason_code', 'identity_evidence')}
                history = json.loads(row['publication_history'])
                if not history or history[-1] != publication:
                    self._update(request, publication_history=json.dumps([*history, publication], sort_keys=True))
            self._update(
                request,
                phase=CheckpointPhase.PREPARED.value,
                commit_identity=prepared.commit_identity,
                identity_reason_code=request.reason_code,
                identity_evidence=request.evidence,
                tree_oid=prepared.tree_oid,
                local_oid=None,
                result=None,
                unpublished_reason=None,
                retryable=0,
            )

    def refresh(self, request: CheckpointRequest, *, expected_remote_oid: str | None) -> None:
        """Reopen a terminal request, retaining its last published ownership."""
        current = self.get(request.repo, request.plan_id)
        if current.request_identity != request.request_identity:
            raise RuntimeError("checkpoint refresh identity가 변경됐습니다.")
        if current.phase not in {CheckpointPhase.PUBLISHED, CheckpointPhase.UNPUBLISHED}:
            return
        with self.connection:
            self._update(
                request, phase=CheckpointPhase.REQUESTED.value,
                expected_remote_oid=expected_remote_oid, remote_observed=1,
                result=None, unpublished_reason=None, retryable=0,
            )

    def record_rebased_publication(self, request: CheckpointRequest, head: str, tree: str) -> None:
        """Keep the prior publication as audit and identify the new clean result.

        The caller has accepted current Git state and confirmed G-104 publication.
        This is the existing no-new-commit identity, not a synthetic Git receipt.
        """
        row = self.connection.execute(
            'SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?',
            (request.repo, request.plan_id)).fetchone()
        if row is None or row['request_identity'] != request.request_identity or row['phase'] != 'published':
            raise RuntimeError('rebase checkpoint publication identity가 변경됐습니다.')
        if row['accepted_remote_oid'] == head:
            return
        history = json.loads(row['publication_history'])
        history.append({key: row[key] for key in (
            'published_oid', 'accepted_remote_oid', 'commit_identity', 'tree_oid',
            'identity_reason_code', 'identity_evidence')})
        identity = checkpoint_identity(plan_id=request.plan_id, reason_code=request.reason_code,
                                       evidence=request.evidence, parent_oid=head, tree_oid=tree,
                                       included_paths=())
        self._update(request, published_oid=head, accepted_remote_oid=head,
                     local_oid=head, remote_oid=head, expected_remote_oid=head,
                     commit_identity=identity, tree_oid=tree,
                     identity_reason_code=request.reason_code, identity_evidence=request.evidence,
                     publication_history=json.dumps(history, sort_keys=True))

    def record_local(self, request: CheckpointRequest, local_oid: str) -> None:
        with self.connection:
            self._update(
                request,
                phase=CheckpointPhase.LOCAL_COMMITTED.value,
                local_oid=local_oid,
            )

    def record_remote_observation(
        self, request: CheckpointRequest, remote_oid: str | None
    ) -> CheckpointRequest:
        expected = request.expected_remote_oid
        observed = request.remote_observed
        if not observed:
            observed = True
        with self.connection:
            self._update(
                request,
                expected_remote_oid=expected,
                remote_observed=int(observed),
                remote_oid=remote_oid,
            )
        return replace(
            request,
            expected_remote_oid=expected,
            remote_observed=observed,
            remote_oid=remote_oid,
        )

    def finish_published(
        self, request: CheckpointRequest, remote_oid: str
    ) -> None:
        with self.connection:
            self._update(
                request,
                phase=CheckpointPhase.PUBLISHED.value,
                result="published",
                local_oid=remote_oid,
                remote_oid=remote_oid,
                published_oid=remote_oid,
                accepted_remote_oid=remote_oid,
                pending_remote_oid=None,
                inspected_remote_oid=None,
                inspected_input=None,
                inspection_reason=None,
                inspection_retryable=0,
                unpublished_reason=None,
                retryable=0,
            )
            self.connection.execute(
                "UPDATE exec_plans SET publication_remote_oid = ? "
                "WHERE repo = ? AND plan_id = ?",
                (remote_oid, request.repo, request.plan_id),
            )
            notifications.enqueue_current_project_events(self.connection, request.repo)

    def start_remote_adoption(self, request: CheckpointRequest, oid: str) -> None:
        with self.connection:
            self._update(request, pending_remote_oid=oid)

    def record_remote_inspection(
        self, request: CheckpointRequest, oid: str, input_identity: str,
        reason: str, *, commit: bool = True,
    ) -> None:
        if commit:
            with self.connection:
                self.record_remote_inspection(
                    request, oid, input_identity, reason, commit=False,
                )
                notifications.enqueue_current_project_events(
                    self.connection, request.repo,
                )
            return
        self._update(
            request, accepted_remote_oid=oid, pending_remote_oid=None,
            inspected_remote_oid=oid, inspected_input=input_identity,
            inspection_reason=reason, inspection_retryable=0,
        )

    def reclassify_published(
        self,
        request: CheckpointRequest,
        *,
        reason_code: str,
        evidence: str,
        release_fingerprint: str,
        accepted_oid: str,
    ) -> None:
        """Keep the published checkpoint; let document release checks run again."""

        if request.phase is not CheckpointPhase.PUBLISHED:
            raise RuntimeError("게시되지 않은 checkpoint의 차단 원인을 바꿀 수 없습니다.")
        identity = request_identity(
            request.repo, request.plan_id, reason_code, evidence,
            request.branch, request.worktree, release_fingerprint,
        )
        # Legacy rows must retain the original identity inputs before reclassification.
        self.connection.execute(
            "UPDATE blocked_checkpoints SET "
            "identity_reason_code = COALESCE(identity_reason_code, reason_code), "
            "identity_evidence = COALESCE(identity_evidence, evidence) "
            "WHERE repo = ? AND plan_id = ? AND request_identity = ?",
            (request.repo, request.plan_id, request.request_identity),
        )
        self._update(
            request,
            request_identity=identity,
            reason_code=reason_code,
            evidence=evidence,
            remote_oid=accepted_oid,
            accepted_remote_oid=accepted_oid,
            pending_remote_oid=None,
            inspected_remote_oid=None,
            inspected_input=None,
            inspection_reason=None,
            inspection_retryable=0,
        )

    def record_remote_refusal(
        self, request: CheckpointRequest, oid: str | None,
        input_identity: str, reason: str, *, commit: bool = True,
        retryable: bool = False,
    ) -> None:
        if commit:
            with self.connection:
                self.record_remote_refusal(
                    request, oid, input_identity, reason, commit=False,
                    retryable=retryable,
                )
            return
        self._update(
            request, pending_remote_oid=None,
            inspected_remote_oid=oid,
            inspected_input=input_identity, inspection_reason=reason,
            inspection_retryable=int(retryable),
        )

    def finish_unpublished(
        self,
        request: CheckpointRequest,
        reason: str,
        *,
        retryable: bool,
    ) -> None:
        phase = (
            CheckpointPhase.RETRY_PENDING
            if retryable
            else CheckpointPhase.UNPUBLISHED
        )
        with self.connection:
            self._update(
                request,
                phase=phase.value,
                result="local_only",
                unpublished_reason=reason,
                retryable=int(retryable),
            )
            # RETRY_PENDING preserves local progress, not a final outcome.
            # Reserving the event key now would hide a later published result.
            if not retryable:
                notifications.enqueue_current_project_events(self.connection, request.repo)


class DiagnosticPublicationRepository:
    """Immutable diagnostic intents never confer original-branch ownership."""

    def __init__(self, connection):
        self.connection = connection
        ensure_schema(connection)

    def intent(self, request, *, base_oid, head_oid, tree_oid, cause):
        ref = diagnostic_branch(request.repo, request.plan_id, request.branch,
                                base_oid, head_oid, tree_oid)
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO blocked_diagnostic_publications '
                '(repo, plan_id, ref, original_branch, base_oid, head_oid, tree_oid, cause) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (request.repo, request.plan_id, ref, request.branch, base_oid,
                 head_oid, tree_oid, cause))
        row = self.connection.execute(
            'SELECT * FROM blocked_diagnostic_publications WHERE repo = ? AND plan_id = ? AND ref = ?',
            (request.repo, request.plan_id, ref)).fetchone()
        if (row['original_branch'], row['base_oid'], row['head_oid'], row['tree_oid']) != (
                request.branch, base_oid, head_oid, tree_oid):
            raise RuntimeError('진단 게시 intent의 작업물 identity가 변경됐습니다.')
        return row

    def result(self, repo, plan_id, cause_identity, payload, *, intent=None):
        with self.connection:
            if intent is not None:
                if payload.get('commit_oid') != intent['head_oid'] or payload.get('branch') != intent['ref']:
                    raise RuntimeError('진단 ref·확인 OID가 저장된 intent와 다릅니다.')
                self.connection.execute(
                    'UPDATE blocked_diagnostic_publications SET confirmed_oid = head_oid '
                    'WHERE repo = ? AND plan_id = ? AND ref = ?',
                    (repo, plan_id, intent['ref']))
            self.connection.execute(
                'INSERT INTO project_checkpoint_results VALUES (?, ?, ?, ?) '
                'ON CONFLICT(repo, plan_id) DO UPDATE SET cause_identity = excluded.cause_identity, '
                'payload = excluded.payload',
                (repo, plan_id, cause_identity, json.dumps(payload, sort_keys=True)))
            notifications.enqueue_current_project_events(self.connection, repo)
