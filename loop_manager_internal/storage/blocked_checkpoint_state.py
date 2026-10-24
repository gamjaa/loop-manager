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
    pr_url TEXT,
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
    connection.execute("""CREATE TABLE IF NOT EXISTS diagnostic_preservations (
        repo TEXT NOT NULL, scope TEXT NOT NULL, owner TEXT NOT NULL,
        input_identity TEXT NOT NULL, ref TEXT, tree_oid TEXT, commit_oid TEXT,
        payload TEXT NOT NULL, retryable INTEGER NOT NULL DEFAULT 0,
        observed_order INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (repo, scope, owner, input_identity)
    )""")
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
    connection.execute("""CREATE TABLE IF NOT EXISTS diagnostic_ref_uses (
        repo TEXT NOT NULL, scope TEXT NOT NULL, owner TEXT NOT NULL,
        ref TEXT NOT NULL, cause_identity TEXT NOT NULL,
        input_identity TEXT NOT NULL, origin TEXT NOT NULL,
        original_branch TEXT NOT NULL, tree_oid TEXT NOT NULL,
        commit_oid TEXT NOT NULL, cause TEXT NOT NULL,
        confirmed_oid TEXT, publication_pending INTEGER NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (repo, scope, owner, ref, cause_identity, input_identity, origin)
    )""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(blocked_checkpoints)")}
    for name in (
        "published_oid", "accepted_remote_oid", "pending_remote_oid",
        "inspected_remote_oid", "inspected_input", "inspection_reason",
        "identity_reason_code", "identity_evidence", "pr_url",
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
    pr_url: str | None = None,
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
    if pr_url:
        value += "\0" + pr_url
    return hashlib.sha256(value.encode()).hexdigest()


def scope_request_row(connection: sqlite3.Connection, repo: str, plan_id: str) -> sqlite3.Row | None:
    """Observe scope publication identity without initializing checkpoint schema."""
    return connection.execute(
        "SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?", (repo, plan_id),
    ).fetchone()


class BlockedCheckpointRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)

    def reconcile_cause(self, request):
        """CAS a stale request to this owned plan's current cause before inspection."""
        with self.connection:
            if not self.connection.in_transaction:
                self.connection.execute('BEGIN IMMEDIATE')
            row = self.connection.execute('SELECT * FROM blocked_checkpoints WHERE repo=? AND plan_id=?',
                                          (request.repo, request.plan_id)).fetchone()
            if row is None or row['request_identity'] != request.request_identity:
                return None  # Another dispatcher owns the newer request.
            plan = self.connection.execute('SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
                                           (request.repo, request.plan_id)).fetchone()
            block = self.connection.execute('SELECT * FROM execplan_blocks WHERE repo=? AND plan_id=?',
                                            (request.repo, request.plan_id)).fetchone()
            project_block = self.connection.execute('SELECT * FROM project_blocks WHERE repo=?',
                                                    (request.repo,)).fetchone()
            if project_block is not None:
                return request  # Preserve the separate project safety gate.
            if plan is None or (plan['branch'], plan['worktree'], plan['pr_url']) != (
                    request.branch, request.worktree, request.pr_url):
                return request  # A resource/lease change still needs safety rejection.
            if block is None or plan['status'] != 'PLAN_BLOCKED':
                self._update(request, phase=CheckpointPhase.UNPUBLISHED.value,
                             unpublished_reason='checkpoint 원인이 해소되어 요청을 취소했습니다.', retryable=0)
                return None
            if (block['reason_code'], block['evidence']) == (request.reason_code, request.evidence):
                return request
            history = json.loads(row['publication_history'])
            history.append({'superseded_request': {key: row[key] for key in row.keys()
                if key not in {'publication_history', 'created_at', 'updated_at'}}})
            identity = request_identity(request.repo, request.plan_id, block['reason_code'],
                block['evidence'], request.branch, request.worktree, block['release_fingerprint'], request.pr_url)
            self._update(request, request_identity=identity, reason_code=block['reason_code'],
                evidence=block['evidence'], phase=CheckpointPhase.REQUESTED.value,
                commit_identity=None, tree_oid=None, local_oid=None,
                publication_history=json.dumps(history, sort_keys=True), result=None,
                unpublished_reason=None, retryable=0)
            return self.get(request.repo, request.plan_id)

    def accept_superseded_publication(self, request: CheckpointRequest, verified_oid: str) -> None:
        """Advance only the old lease of a live-verified, archived local commit."""
        with self.connection:
            if not self.connection.in_transaction:
                self.connection.execute('BEGIN IMMEDIATE')
            row = scope_request_row(self.connection, request.repo, request.plan_id)
            if row is None or row['request_identity'] != request.request_identity:
                return
            plan = self.connection.execute('SELECT branch, worktree, pr_url FROM exec_plans WHERE repo=? AND plan_id=?',
                (request.repo, request.plan_id)).fetchone()
            if plan is None or tuple(plan) != (request.branch, request.worktree, request.pr_url):
                return
            for entry in json.loads(row['publication_history']):
                prior = entry.get('superseded_request', {})
                if (prior.get('local_oid') == verified_oid and prior.get('commit_identity')
                        and prior.get('tree_oid') and request.expected_remote_oid == prior.get('expected_remote_oid')
                        and row['expected_remote_oid'] == request.expected_remote_oid
                        and (prior.get('branch'), prior.get('worktree'), prior.get('pr_url'))
                        == (request.branch, request.worktree, request.pr_url)):
                    self._update(request, expected_remote_oid=verified_oid, remote_observed=1)
                    return

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
        plan = self.connection.execute(
            "SELECT pr_url FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id)).fetchone()
        pr_url = plan['pr_url'] if plan else None
        identity = request_identity(
            repo, plan_id, reason_code, evidence, branch, worktree,
            release_fingerprint, pr_url,
        )
        current = self.connection.execute(
            "SELECT * FROM blocked_checkpoints "
            "WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if current is None or str(current['request_identity']) != identity:
            if current is not None and current['phase'] == 'published':
                prior = {key: current[key] for key in current.keys()
                         if key not in {'publication_history', 'created_at', 'updated_at'}}
                history = json.loads(current['publication_history'])
                history.append({'superseded_request': prior})
                self._update(self._from_row(current),
                             publication_history=json.dumps(history, sort_keys=True))
            self.connection.execute(
                """
                INSERT INTO blocked_checkpoints (
                    repo, plan_id, request_identity, phase, reason_code,
                    evidence, branch, worktree, expected_remote_oid,
                    remote_observed, rebase_recovery, pr_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    pr_url = excluded.pr_url,
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
                    pr_url,
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
            pr_url=row["pr_url"],
        )

    def get(self, repo: str, plan_id: str) -> CheckpointRequest:
        row = self.connection.execute(
            "SELECT * FROM blocked_checkpoints WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"checkpoint request를 찾을 수 없습니다: {plan_id}")
        return self._from_row(row)

    def pending(
        self, repo: str, plan_id: str | None = None,
    ) -> tuple[CheckpointRequest, ...]:
        sql = (
            "SELECT * FROM blocked_checkpoints WHERE repo = ? "
            "AND phase IN (?, ?, ?, ?)"
        )
        parameters: tuple[object, ...] = (
            repo,
            CheckpointPhase.REQUESTED.value,
            CheckpointPhase.PREPARED.value,
            CheckpointPhase.LOCAL_COMMITTED.value,
            CheckpointPhase.RETRY_PENDING.value,
        )
        if plan_id is not None:
            sql += " AND plan_id = ?"
            parameters += (plan_id,)
        rows = self.connection.execute(sql + " ORDER BY plan_id", parameters).fetchall()
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
            row = scope_request_row(self.connection, request.repo, request.plan_id)
            history = json.loads(row['publication_history'])
            if current.phase is CheckpointPhase.PUBLISHED:
                prior = {key: row[key] for key in row.keys()
                         if key not in {'publication_history', 'created_at', 'updated_at'}}
                history.append({'superseded_request': prior})
            self._update(
                request, phase=CheckpointPhase.REQUESTED.value,
                expected_remote_oid=expected_remote_oid, remote_observed=1,
                result=None, unpublished_reason=None, retryable=0,
                publication_history=json.dumps(history, sort_keys=True),
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
        self, request: CheckpointRequest, remote_oid: str, *, reobserve=None
    ) -> None:
        from loop_manager_internal.storage.runtime_state import _transaction, RuntimeStateRepository

        runtime = RuntimeStateRepository(self.connection)
        plan = runtime.get(request.repo, request.plan_id)
        confirmed = reobserve() if request.pr_url and reobserve is not None else None
        with _transaction(self.connection):
            if runtime.get(request.repo, request.plan_id) != plan:
                raise RuntimeError('checkpoint 작업 소유권이 변경됐습니다.')
            if request.pr_url:
                if ((plan.branch, plan.worktree, plan.pr_url) != (request.branch, request.worktree, request.pr_url)
                        or reobserve is None or confirmed != remote_oid
                        or runtime.get(request.repo, request.plan_id) != plan):
                    raise RuntimeError('checkpoint 확정 직전 PR·작업 공간·원격 결과가 변경됐습니다.')
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
            values = {'publication_remote_oid': remote_oid}
            if request.pr_url:
                values.update(pr_head_oid=remote_oid, worker_input_token=None, archive_token=None,
                              verify_token=None, review_token=None, publication_token=None,
                              validated_base_sha=None, worker_result=None, verify_result=None,
                              review_result=None)
                if plan.pr_conflict_recovery:
                    values['pr_conflict_head_oid'] = remote_oid
            projected = runtime.projected(request.repo)
            if request.pr_url and projected and projected.plan_id == request.plan_id:
                runtime.update_project(request.repo, values, enqueue=False)
            else:
                runtime.update_plan(request.repo, request.plan_id, plan_values=values, enqueue=False)
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
            request.branch, request.worktree, release_fingerprint, request.pr_url,
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

    def preservation(self, repo, scope, owner, identity):
        return self.connection.execute(
            'SELECT * FROM diagnostic_preservations WHERE repo = ? AND scope = ? '
            'AND owner = ? AND input_identity = ?', (repo, scope, owner, identity)).fetchone()

    def _record_use(self, repo, scope, owner, identity, payload, *, ref, tree,
                    commit, pending, confirm=False):
        """Keep every cause/target binding; a cached receipt is not a new intent.

        Called inside the existing intent/receipt transaction. Missing legacy
        evidence stays unconfirmed rather than acquiring ownership by backfill.
        """
        cause_identity = payload.get('cause_identity')
        branch = payload.get('original_branch')
        if not all((ref, tree, commit, cause_identity, branch)):
            return
        project = self.connection.execute(
            'SELECT origin FROM projects WHERE repo = ?', (repo,)).fetchone()
        origin = (project['origin'] or '') if project else ''
        key = (repo, scope, owner, ref, cause_identity, identity, origin)
        previous = self.connection.execute(
            'SELECT * FROM diagnostic_ref_uses WHERE repo=? AND scope=? AND owner=? '
            'AND ref=? AND cause_identity=? AND input_identity=? AND origin=?', key).fetchone()
        cause = json.dumps(payload.get('cause', {}), sort_keys=True)
        if previous and (previous['original_branch'], previous['tree_oid'],
                         previous['commit_oid'], previous['cause']) != (branch, tree, commit, cause):
            raise RuntimeError('진단 ref 사용의 원인·게시 identity가 변경됐습니다.')
        confirmed = previous['confirmed_oid'] if previous else None
        if previous and previous['publication_pending'] and confirm:
            confirmed = commit
        self.connection.execute(
            'INSERT INTO diagnostic_ref_uses '
            '(repo, scope, owner, ref, cause_identity, input_identity, origin, '
            'original_branch, tree_oid, commit_oid, cause, confirmed_oid, publication_pending) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(repo, scope, owner, ref, cause_identity, input_identity, origin) '
            'DO UPDATE SET confirmed_oid=excluded.confirmed_oid, '
            'publication_pending=excluded.publication_pending, generation=generation+1 '
            'WHERE confirmed_oid IS NOT excluded.confirmed_oid '
            'OR publication_pending != excluded.publication_pending',
            (*key, branch, tree, commit, cause, confirmed, int(pending)))

    def preserve(self, repo, scope, owner, identity, payload, *, ref=None,
                 tree=None, commit=None, retryable=False, revalidate=None):
        with self.connection:
            if revalidate is not None:
                revalidate()
            previous = self.preservation(repo, scope, owner, identity)
            if previous and previous['commit_oid'] and (
                    previous['ref'], previous['tree_oid'], previous['commit_oid']) != (ref, tree, commit):
                raise RuntimeError('진단 보존 intent가 변경됐습니다.')
            self._record_use(repo, scope, owner, identity, payload, ref=ref,
                tree=tree, commit=commit, pending=retryable,
                confirm=bool(previous and json.loads(previous['payload']).get('result') != 'published'
                             and payload.get('result') == 'published'))
            self.connection.execute(
                'INSERT INTO diagnostic_preservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '
                '(SELECT COALESCE(MAX(observed_order), 0) + 1 FROM diagnostic_preservations)) '
                'ON CONFLICT(repo, scope, owner, input_identity) DO UPDATE SET '
                'ref = excluded.ref, tree_oid = excluded.tree_oid, commit_oid = excluded.commit_oid, '
                'payload = excluded.payload, retryable = excluded.retryable, observed_order = excluded.observed_order',
                (repo, scope, owner, identity, ref, tree, commit,
                 json.dumps(payload, sort_keys=True), int(retryable)))

    def latest_preservation(self, repo, scope, owner):
        row = self.connection.execute(
            'SELECT payload FROM diagnostic_preservations WHERE repo = ? AND scope = ? '
            'AND owner = ? ORDER BY observed_order DESC LIMIT 1', (repo, scope, owner)).fetchone()
        return json.loads(row[0]) if row else None

    def intent(self, request, *, base_oid, head_oid, tree_oid, cause):
        from loop_manager_internal.recovery import scoped_blocking

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
            block = scoped_blocking.project_block(self.connection, request.repo)
            if block is not None and block['plan_id'] == request.plan_id:
                self._record_use(request.repo, 'project', request.plan_id, '',
                    dict(original_branch=request.branch, cause=dict(block),
                         cause_identity=scoped_blocking.project_cause_identity(block)),
                    ref=ref, tree=tree_oid, commit=head_oid, pending=True)
        return row

    def result(self, repo, plan_id, cause_identity, payload, *, intent=None):
        from loop_manager_internal.recovery import scoped_blocking

        with self.connection:
            block = scoped_blocking.project_block(self.connection, repo)
            plan = self.connection.execute(
                'SELECT branch, worktree FROM exec_plans WHERE repo = ? AND plan_id = ?',
                (repo, plan_id),
            ).fetchone()
            if (block is None or block['plan_id'] != plan_id or plan is None
                    or scoped_blocking.project_cause_identity(block) != cause_identity):
                raise RuntimeError('진단 결과의 project 원인 또는 계획 소유권이 변경됐습니다.')
            if intent is not None:
                request = self.connection.execute(
                    'SELECT branch, worktree FROM blocked_checkpoints '
                    'WHERE repo = ? AND plan_id = ?', (repo, plan_id),
                ).fetchone()
                current_intent = self.connection.execute(
                    'SELECT * FROM blocked_diagnostic_publications '
                    'WHERE repo = ? AND plan_id = ? AND ref = ?',
                    (repo, plan_id, intent['ref']),
                ).fetchone()
                if (request is None or request['branch'] != plan['branch']
                        or request['worktree'] != plan['worktree']
                        or payload.get('result') != 'published'
                        or payload.get('diagnostic') is not True
                        or payload.get('original_branch') != plan['branch']
                        or payload.get('base_oid') != intent['base_oid']
                        or current_intent is None or any(
                        current_intent[field] != intent[field] for field in
                        ('repo', 'plan_id', 'ref', 'original_branch', 'base_oid',
                         'head_oid', 'tree_oid', 'cause'))
                        or intent['repo'] != repo or intent['plan_id'] != plan_id
                        or intent['original_branch'] != plan['branch']
                        or payload.get('commit_oid') != intent['head_oid']
                        or payload.get('branch') != intent['ref']):
                    raise RuntimeError('진단 ref·확인 OID가 저장된 intent와 다릅니다.')
                # The intent retains its original cause across identical-content
                # reuse. The current block is guarded by cause_identity above.
                changed = self.connection.execute(
                    'UPDATE blocked_diagnostic_publications SET confirmed_oid = head_oid '
                    'WHERE repo = ? AND plan_id = ? AND ref = ? '
                    'AND (confirmed_oid IS NULL OR confirmed_oid = head_oid)',
                    (repo, plan_id, intent['ref']))
                if changed.rowcount != 1:
                    raise RuntimeError('진단 게시 intent 결과가 변경됐습니다.')
                self._record_use(repo, 'project', plan_id, '',
                    dict(payload, cause=dict(block), cause_identity=cause_identity),
                    ref=intent['ref'], tree=intent['tree_oid'],
                    commit=intent['head_oid'], pending=False, confirm=True)
            elif payload.get('result') == 'published':
                request = self.connection.execute(
                    'SELECT branch, worktree, phase, remote_oid FROM blocked_checkpoints '
                    'WHERE repo = ? AND plan_id = ?', (repo, plan_id),
                ).fetchone()
                if (request is None or request['phase'] != 'published'
                        or request['branch'] != plan['branch']
                        or request['worktree'] != plan['worktree']
                        or payload.get('branch') != request['branch']
                        or payload.get('commit_oid') != request['remote_oid']):
                    raise RuntimeError('원래 branch 게시 결과의 소유권이 변경됐습니다.')
            elif (payload.get('result') != 'local_only'
                  or payload.get('original_branch') != plan['branch']
                  or payload.get('worktree') != plan['worktree']):
                raise RuntimeError('local-only 진단 결과의 계획 identity가 변경됐습니다.')
            self.connection.execute(
                'INSERT INTO project_checkpoint_results VALUES (?, ?, ?, ?) '
                'ON CONFLICT(repo, plan_id) DO UPDATE SET cause_identity = excluded.cause_identity, '
                'payload = excluded.payload',
                (repo, plan_id, cause_identity, json.dumps(payload, sort_keys=True)))
            notifications.enqueue_current_project_events(self.connection, repo)
