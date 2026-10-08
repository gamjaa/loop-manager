"""Shared origin fixtures; no test classes are collected here."""
from pathlib import Path
import subprocess

import loop_manager
from loop_manager_internal.storage import database
from tests.support.git import git_command as git


def configure_identity(repo: Path) -> None:
    git(repo, "config", "user.email", "loop@example.com")
    git(repo, "config", "user.name", "Loop Test")
    git(repo, "config", "core.autocrlf", "false")



def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")



def write_registration_files(repo: Path) -> None:
    (repo / "docs/requirements").mkdir(parents=True)
    (repo / "AGENTS.md").write_bytes((loop_manager.ROOT / "AGENTS.md").read_bytes())
    (repo / "docs/PLANS.md").write_bytes(
        (loop_manager.ROOT / "docs/PLANS.md").read_bytes()
    )
    (repo / "docs/ARCHITECTURE.md").write_text(
        "# Test Architecture\n\nCommitted design.\n",
        encoding="utf-8",
    )
    (repo / "docs/PRODUCT.md").write_text(
        "# Test Product\n\nCommitted behavior.\n",
        encoding="utf-8",
    )



def remote_fixture(directory: str) -> tuple[Path, Path, Path]:
    root = Path(directory)
    remote = root / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    seed = root / "seed"
    subprocess.run(
        ["git", "clone", str(remote), str(seed)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    configure_identity(seed)
    git(seed, "checkout", "-b", "main")
    (seed / ".gitignore").write_text(
        "config/config.ini\n.venv/\n.cache/\nignored/\n",
        encoding="utf-8",
    )
    (seed / "tracked.txt").write_text("one\n", encoding="utf-8")
    (seed / "loop_manager.py").write_text("VALUE = 'origin-one'\n", encoding="utf-8")
    commit_all(seed, "Initial")
    git(seed, "push", "-u", "origin", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    primary = root / "primary"
    writer = root / "writer"
    for checkout in (primary, writer):
        subprocess.run(
            ["git", "clone", str(remote), str(checkout)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        configure_identity(checkout)
    return remote, primary, writer



def advance_remote(writer: Path, *, path: str = "tracked.txt", value: str = "two\n") -> str:
    target = writer / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")
    git(writer, "add", "-f", "--", path)
    git(writer, "commit", "-m", f"Advance {path}")
    git(writer, "push", "origin", "main")
    return git(writer, "rev-parse", "HEAD")



class OriginScopeFixture:
    def _persist_planner_scope(self, connection, repo: Path):
        before = loop_manager.capture_git_snapshot(repo)
        connection.execute(database.SCHEMA)
        connection.execute(
            "INSERT INTO projects (repo, base_branch, phase, worktree, agent_role, agent_snapshot) "
            "VALUES (?, 'main', 'PLANNING', ?, 'planner', ?)",
            (str(repo), str(repo), before.serialize()),
        )
        connection.commit()
        return before
