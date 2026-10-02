"""Read-only GitHub pull request observation."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
import json


@dataclass(frozen=True)
class PrForBranchEffects:
    """Only the effects consumed by this boundary; no Manager module access."""
    command: Callable[..., object]

def pr_for_branch(effects: PrForBranchEffects, repo: Path, branch: str, url: str | None = None) -> dict[str, object] | None:
    fields = "url,state,mergedAt,headRefOid,headRefName,baseRefName,mergeable"
    if url:
        result = effects.command(["gh", "pr", "view", url, "--json", fields], cwd=repo)
        return json.loads(result.stdout)
    result = effects.command(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json", fields],
        cwd=repo,
    )
    rows = json.loads(result.stdout)
    return rows[0] if rows else None


def classify_pull_request_mergeability(
    pull_request: Mapping[str, object],
) -> str:
    """Normalize GitHub's asynchronous mergeability result."""

    value = str(pull_request.get("mergeable") or "UNKNOWN").upper()
    if value == "CONFLICTING":
        return "conflicting"
    if value == "MERGEABLE":
        return "mergeable"
    return "unknown"
