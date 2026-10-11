"""Durable Manager correction candidates; execution and decision state stay untouched."""
from __future__ import annotations

import json
import sqlite3

from loop_manager_internal.storage.runtime_state import _transaction


class RequirementCorrectionRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS requirement_correction_publications (
            repo TEXT NOT NULL, operation_id TEXT NOT NULL, manifest TEXT NOT NULL,
            commit_oid TEXT, stage TEXT NOT NULL, reason TEXT,
            PRIMARY KEY (repo, operation_id))""")

    def pending(self, repo):
        row = self.connection.execute(
            "SELECT * FROM requirement_correction_publications WHERE repo=? "
            "AND stage='prepared' ORDER BY rowid LIMIT 1", (repo,),
        ).fetchone()
        return dict(row) if row else None

    def record(self, repo, operation_id, manifest, *, current=lambda: True):
        encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
        with _transaction(self.connection):
            self.connection.execute(
                "INSERT OR IGNORE INTO requirement_correction_publications "
                "(repo,operation_id,manifest,stage) VALUES (?,?,?,'prepared')",
                (repo, operation_id, encoded),
            )
            row = self.connection.execute(
                "SELECT manifest,stage FROM requirement_correction_publications "
                "WHERE repo=? AND operation_id=?", (repo, operation_id),
            ).fetchone()
            if row['manifest'] != encoded or row['stage'] not in {'prepared', 'superseded'}:
                raise RuntimeError('Requirement 교정 intent가 기존 후보와 다릅니다.')
            if not current():
                raise RuntimeError('Requirement 교정 소유 예약 중 원격·참조 입력이 변경됐습니다.')
            if row['stage'] == 'superseded':
                # Reuse the saved commit and worktree when the exact input
                # returns. The publisher still validates their actual result.
                self.connection.execute(
                    "UPDATE requirement_correction_publications SET stage='prepared' "
                    "WHERE repo=? AND operation_id=?", (repo, operation_id),
                )

    def update(self, repo, operation_id, *, stage='prepared', commit_oid=None, reason=None):
        with _transaction(self.connection):
            self.connection.execute(
                "UPDATE requirement_correction_publications SET stage=?, "
                "commit_oid=COALESCE(?,commit_oid),reason=? WHERE repo=? AND operation_id=?",
                (stage, commit_oid, reason, repo, operation_id),
            )

    def confirmed(self, repo):
        return tuple(dict(row) for row in self.connection.execute(
            "SELECT * FROM requirement_correction_publications WHERE repo=? AND stage='confirmed'",
            (repo,),
        ))


def claimed_paths(connection, repo):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='requirement_correction_publications'"
    ).fetchone():
        return frozenset()
    return frozenset(path for row in connection.execute(
        "SELECT manifest FROM requirement_correction_publications WHERE repo=? AND stage='prepared'",
        (repo,),
    ) for path in json.loads(row[0])['before'])
