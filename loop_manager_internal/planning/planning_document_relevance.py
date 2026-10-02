"""Identify changed documents that can affect the current Planner result."""
from __future__ import annotations

from pathlib import Path
import stat

from loop_manager_internal.catalog.execplan_catalog import parse_execplan_text


def _blob(git, repository: Path, revision: str, path: str) -> str | None:
    result = git(repository, "rev-parse", "--verify", f"{revision}:{path}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _content_changed(git, repository: Path, old: str, new: str, path: str) -> bool:
    previous = _blob(git, repository, old, path)
    current = _blob(git, repository, new, path)
    if previous == current:
        return False
    if previous and current is None:
        # A completed Requirement/ExecPlan archive move preserves its bytes.
        if path.startswith("docs/requirements/"):
            archived = "docs/requirements/archive/" + path.rsplit("/", 1)[-1]
        elif path.startswith("docs/plans/"):
            archived = "docs/plans/archive/" + path.rsplit("/", 1)[-1]
        else:
            archived = ""
        if archived and _blob(git, repository, new, archived) == previous:
            return False
    return True


def relevant_document_changes(
    git, repository: Path, worktree: Path, old: str, new: str,
    edited_paths: list[str], catalog_rows,
) -> tuple[str, ...]:
    """Return changed source, dependency and active-plan documents only."""
    changed = set(git(repository, "diff", "--name-only", old, new, "--", "docs").stdout.splitlines())
    if not changed:
        return ()
    rows = {str(row["plan_id"]): row for row in catalog_rows}
    plan_paths = [
        path for path in edited_paths
        if path.startswith("docs/plans/") and path.endswith(".md")
        and path.count("/") == 2
    ]
    relevant = set(plan_paths)
    relevant.update(("docs/PRODUCT.md", "docs/ARCHITECTURE.md"))
    for path in plan_paths:
        file = worktree / path
        try:
            if not stat.S_ISREG(file.lstat().st_mode):
                continue
            parsed = parse_execplan_text(
                path, file.read_text(encoding="utf-8"),
                validate_sources=False,
            )
        except (OSError, UnicodeError, RuntimeError):
            continue  # The ordinary Planner validation reports incomplete files.
        relevant.update(str(source) for source in parsed["sources"])
        for dependency in parsed["depends_on"]:
            row = rows.get(str(dependency))
            if row is not None:
                relevant.add(str(row["path"]))
    if not plan_paths:
        # No current plan declaration exists yet: current product and active
        # requirements remain the assessment's input.
        relevant.update(path for path in changed if path == "docs/PRODUCT.md"
                        or path.startswith("docs/requirements/") and path.count("/") == 2)
    return tuple(sorted(
        path for path in changed & relevant
        if _content_changed(git, repository, old, new, path)
    ))
