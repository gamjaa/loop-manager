from loop_manager_internal.storage import database
import json
import sqlite3

from loop_manager_internal.notifications import notifications


def runtime_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(database.SCHEMA)
    database._ensure_execplans_schema(connection)
    notifications.ensure_schema(connection)
    connection.execute(
        """
        INSERT INTO projects (
            repo, base_branch, provider, planning_fingerprint,
            planning_attempt_fingerprint, phase
        ) VALUES ('repo', 'main', 'codex', 'planning-kept',
                  'planning-cleared', NULL)
        """
    )
    connection.execute(
        """
        INSERT INTO exec_plans (
            repo, plan_id, path, title, sources, depends_on, priority,
            content_hash, status, plan_sha, active_sha, branch, worktree,
            workspace_owned
        ) VALUES (
            'repo', 'G-047', 'docs/plans/[G-047] Runtime.md', 'Runtime',
            '["docs/PRODUCT.md"]', '[]', 'normal', 'hash', 'READY',
            'plan-sha', NULL, NULL, NULL, 1
        )
        """
    )
    connection.commit()
    return connection


class AutomationCatalogFixture:
    """Shared scenario setup; inheriting this fixture collects no tests."""

    def _connection(self, repo: str = "repo") -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(database.SCHEMA)
        database._ensure_execplans_schema(connection)
        connection.execute(
            "INSERT INTO projects (repo, base_branch) VALUES (?, 'main')",
            (repo,),
        )
        connection.commit()
        return connection

    def _insert_plan(
        self,
        connection: sqlite3.Connection,
        plan_id: str,
        status: str,
        *,
        priority: str = "normal",
        depends_on: tuple[str, ...] = (),
        repo: str = "repo",
        title: str | None = None,
        plan_sha: str = "plan-sha",
    ) -> None:
        title = title or f"Plan {plan_id}"
        connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, status, plan_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo,
                plan_id,
                f"docs/plans/[{plan_id}] {title}.md",
                title,
                '["docs/PRODUCT.md"]',
                json.dumps(depends_on),
                priority,
                f"hash-{plan_id}",
                status,
                plan_sha,
            ),
        )
        connection.commit()
