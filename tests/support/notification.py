"""Shared SQLite fixture for notification unit tests."""
import sqlite3
from pathlib import Path
import loop_manager
from loop_manager_internal.notifications import notifications

class NotificationFixture:
    def connection(
        self,
        *,
        enabled: bool = True,
        database: str | Path = ":memory:",
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(loop_manager.SCHEMA)
            loop_manager._ensure_execplans_schema(connection)
            notifications.ensure_schema(connection)
            notifications.configure_session(connection, enabled=enabled)
            connection.execute(
                "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
                ("/projects/alpha",),
            )
            connection.execute(
                """
                INSERT INTO exec_plans (
                    repo, plan_id, path, title, sources, depends_on, priority,
                    content_hash, status
                ) VALUES (?, 'G-018', ?, 'Reliable notices', ?, '[]', 'high', 'hash', 'READY')
                """,
                (
                    "/projects/alpha",
                    "docs/plans/[G-018] Reliable notices.md",
                    '["docs/PRODUCT.md"]',
                ),
            )
            connection.execute(
                "UPDATE projects SET plan_path = ? WHERE repo = ?",
                ("docs/plans/[G-018] Reliable notices.md", "/projects/alpha"),
            )
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def events(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM notification_outbox ORDER BY event_type"
        ).fetchall()
