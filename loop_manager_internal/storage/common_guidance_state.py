"""Durable, additive bindings between work identities and common instructions."""
from __future__ import annotations

import sqlite3


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS common_guidance_versions (
        task_id TEXT PRIMARY KEY, repo TEXT NOT NULL, content_hash TEXT NOT NULL,
        source_revision TEXT NOT NULL, snapshot_path TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS common_guidance_sessions (
        repo TEXT NOT NULL, provider TEXT NOT NULL, session_id TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES common_guidance_versions(task_id),
        PRIMARY KEY (repo, provider, session_id))""")


class GuidanceRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        ensure_schema(connection)

    def get(self, task_id: str):
        return self.connection.execute(
            "SELECT content_hash, source_revision, snapshot_path FROM common_guidance_versions "
            "WHERE task_id = ?", (task_id,),
        ).fetchone()

    def save(self, task_id: str, repo: str, content_hash: str,
             source_revision: str, snapshot_path: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO common_guidance_versions "
                "(task_id, repo, content_hash, source_revision, snapshot_path) VALUES (?, ?, ?, ?, ?)",
                (task_id, repo, content_hash, source_revision, snapshot_path),
            )

    def session_task(self, repo: str, provider: str, session_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT task_id FROM common_guidance_sessions WHERE repo = ? AND provider = ? AND session_id = ?",
            (repo, provider, session_id),
        ).fetchone()
        return str(row[0]) if row else None

    def bind_session(self, repo: str, provider: str, session_id: str, task_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO common_guidance_sessions VALUES (?, ?, ?, ?)",
                (repo, provider, session_id, task_id),
            )
            if self.session_task(repo, provider, session_id) != task_id:
                raise RuntimeError("Agent session이 다른 공통 지침 작업에 결합되어 있습니다.")
