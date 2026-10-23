"""Session ownership bindings; legacy instruction versions remain audit-only."""
from __future__ import annotations

import sqlite3


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS common_guidance_versions (
        task_id TEXT PRIMARY KEY, repo TEXT NOT NULL, content_hash TEXT NOT NULL,
        source_revision TEXT NOT NULL, snapshot_path TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS common_guidance_sessions (
        repo TEXT NOT NULL, provider TEXT NOT NULL, session_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        PRIMARY KEY (repo, provider, session_id))""")
    if connection.execute('PRAGMA foreign_key_list(common_guidance_sessions)').fetchall():
        # Existing sessions keep their task identity without requiring a saved instruction version.
        with connection:
            connection.execute("ALTER TABLE common_guidance_sessions RENAME TO legacy_guidance_sessions")
            connection.execute("""CREATE TABLE common_guidance_sessions (
                repo TEXT NOT NULL, provider TEXT NOT NULL, session_id TEXT NOT NULL,
                task_id TEXT NOT NULL, PRIMARY KEY (repo, provider, session_id))""")
            connection.execute("INSERT INTO common_guidance_sessions SELECT * FROM legacy_guidance_sessions")
            connection.execute("DROP TABLE legacy_guidance_sessions")



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
        if row:
            return str(row[0])
        columns = {item[1] for item in self.connection.execute('PRAGMA table_info(agent_invocations)')}
        if {'guidance_task_id', 'execution_finish_evidence', 'execution_status'}.issubset(columns):
            rows = self.connection.execute(
                "SELECT DISTINCT guidance_task_id FROM agent_invocations "
                "WHERE repo=? AND provider=? AND session_id=? AND finished_at IS NOT NULL "
                "AND execution_status='executed' AND execution_finish_evidence IS NOT NULL "
                "AND guidance_task_id IS NOT NULL", (repo, provider, session_id)).fetchall()
            if len(rows) > 1:
                raise RuntimeError('Agent session의 지침 작업 귀속이 일치하지 않습니다.')
            if rows:
                return str(rows[0][0])
        return None

    def bind_session(self, repo: str, provider: str, session_id: str, task_id: str) -> None:
        with self.connection:
            prior = self.session_task(repo, provider, session_id)
            if prior is not None and prior != task_id:
                raise RuntimeError('Agent session이 다른 공통 지침 작업에 결합되어 있습니다.')
            self.connection.execute(
                "INSERT OR IGNORE INTO common_guidance_sessions VALUES (?, ?, ?, ?)",
                (repo, provider, session_id, task_id),
            )
            if self.session_task(repo, provider, session_id) != task_id:
                raise RuntimeError("Agent session이 다른 공통 지침 작업에 결합되어 있습니다.")
