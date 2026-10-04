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
    else:
        result = effects.command(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json", fields],
            cwd=repo,
        )
    if result.returncode:
        raise RuntimeError("GitHub PR 관측 명령에 실패했습니다.")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeError("GitHub PR 관측 응답이 유효한 JSON이 아닙니다.") from error
    if not url:
        if not isinstance(payload, list) or len(payload) > 1:
            raise RuntimeError("GitHub PR 목록 응답 형식이 유효하지 않습니다.")
        if not payload:
            return None
        payload = payload[0]
    if not isinstance(payload, dict) or not isinstance(payload.get("url"), str) or not payload["url"]:
        raise RuntimeError("GitHub PR 응답 identity가 유효하지 않습니다.")
    if (not set(fields.split(",")).issubset(payload)
            or not isinstance(payload["state"], str)
            or payload["state"] not in {"OPEN", "CLOSED", "MERGED"}
            or not isinstance(payload["baseRefName"], str)
            or not payload["baseRefName"]
            or not isinstance(payload["headRefName"], str)
            or not payload["headRefName"]
            or (payload["headRefOid"] is not None
                and not isinstance(payload["headRefOid"], str))
            or (payload["mergedAt"] is not None
                and not isinstance(payload["mergedAt"], str))
            or (payload["mergeable"] is not None
                and not isinstance(payload["mergeable"], str))):
        raise RuntimeError("GitHub PR 응답 필드가 유효하지 않습니다.")
    return payload


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
