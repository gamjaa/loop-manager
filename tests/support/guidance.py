import sqlite3
import subprocess
from pathlib import Path


PATCH = """diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1 +1,2 @@
 # Agents
+Approved rule
"""


def guidance_plan(*, decision: str = "pending", revision: str = "", patch: str = "") -> str:
    return f"""---
id: G-020
title: Guidance
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: high
risk: critical
priority: high
---

## Purpose

Test.

## Decision Log

### Agent Guidance Proposal: worker-rule
Target: `AGENTS.md`
Scope: `project`
Reason: The current plan needs this rule.
Impact: The Worker cannot finish safely without it.
Patch:
```diff
{PATCH}```

### Agent Guidance User Decision: worker-rule
Decision: `{decision}`
Revision: `{revision}`
Patch:
```diff
{patch}```
"""


def git_result(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def git(repo: Path, *arguments: str) -> str:
    return git_result(repo, *arguments).stdout.strip()


def repository(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "loop@example.com")
    git(repo, "config", "user.name", "Loop Test")
    git(repo, "config", "core.autocrlf", "false")
    files = {
        "AGENTS.md": "# Agents\n",
        "docs/PRODUCT.md": "# Product\n",
        "docs/ARCHITECTURE.md": "# Architecture\n",
        "docs/PLANS.md": "# Plans\n",
        "docs/plans/[G-020] Guidance.md": guidance_plan(),
    }
    for relative, contents in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Initial")
    return repo, "docs/plans/[G-020] Guidance.md"


def active_connection(
    repo: Path,
    plan_path: str,
    database: str | Path = ":memory:",
) -> sqlite3.Connection:
    import loop_manager

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(loop_manager.SCHEMA)
    loop_manager._ensure_execplans_schema(connection)
    loop_manager.notifications.ensure_schema(connection)
    head = git(repo, "rev-parse", "HEAD")
    connection.execute(
        "INSERT INTO projects (repo, base_branch, phase, branch, worktree, plan_path, "
        "active_sha, plan_sha, worker_session_id) "
        "VALUES (?, 'main', 'WORKING', 'main', ?, ?, ?, ?, 'worker-session')",
        (str(repo), str(repo), plan_path, head, head),
    )
    connection.execute(
        """
        INSERT INTO exec_plans (
            repo, plan_id, path, title, sources, depends_on, priority,
            content_hash, status, plan_sha, active_sha, branch, worktree,
            worker_session_id, workspace_owned
        ) VALUES (?, 'G-020', ?, 'Guidance', '["docs/PRODUCT.md"]', '[]',
                  'high', 'hash', 'IMPLEMENTING', ?, ?, 'main', ?,
                  'worker-session', 1)
        """,
        (str(repo), plan_path, head, head, str(repo)),
    )
    connection.commit()
    return connection
