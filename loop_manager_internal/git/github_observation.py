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

def pr_for_branch(effects: PrForBranchEffects, repo: Path, branch: str, url: str | None = None,
                  *, history: bool = False) -> dict[str, object] | None:
    fields = "url,state,mergedAt,headRefOid,headRefName,baseRefName,mergeable"
    if history:
        fields += ",mergeCommit"
    if url:
        result = effects.command(["gh", "pr", "view", url, "--json", fields], cwd=repo)
    else:
        result = effects.command(
            ["gh", "pr", "list", "--head", branch, "--state", "all",
             "--limit", "1000" if history else "1", "--json", fields],
            cwd=repo,
        )
    if result.returncode:
        raise RuntimeError("GitHub PR 관측 명령에 실패했습니다.")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise RuntimeError("GitHub PR 관측 응답이 유효한 JSON이 아닙니다.") from error
    if not url:
        if (not isinstance(payload, list)
                or (history and len(payload) >= 1000)
                or (not history and len(payload) > 1)):
            raise RuntimeError("GitHub PR 목록 응답 형식이 유효하지 않습니다.")
        if not payload:
            return None
        pull_requests = payload if history else [payload[0]]
    else:
        pull_requests = [payload]
    for pull_request in pull_requests:
        if not isinstance(pull_request, dict) or not isinstance(pull_request.get("url"), str) or not pull_request["url"]:
            raise RuntimeError("GitHub PR 응답 identity가 유효하지 않습니다.")
        if (not {"url", "state", "mergedAt", "headRefOid", "headRefName", "baseRefName", "mergeable"}.issubset(pull_request)
                or not isinstance(pull_request["state"], str)
                or pull_request["state"] not in {"OPEN", "CLOSED", "MERGED"}
                or not isinstance(pull_request["baseRefName"], str)
                or not pull_request["baseRefName"]
                or not isinstance(pull_request["headRefName"], str)
                or not pull_request["headRefName"]
                or (pull_request["headRefOid"] is not None
                    and not isinstance(pull_request["headRefOid"], str))
                or (pull_request["mergedAt"] is not None
                    and not isinstance(pull_request["mergedAt"], str))
                or (pull_request["mergeable"] is not None
                    and not isinstance(pull_request["mergeable"], str))):
            raise RuntimeError("GitHub PR 응답 필드가 유효하지 않습니다.")
        merge_commit = pull_request.get("mergeCommit")
        if (merge_commit is not None and (not isinstance(merge_commit, dict)
                or merge_commit.get("oid") is not None
                and not isinstance(merge_commit.get("oid"), str))):
            raise RuntimeError("GitHub PR mergeCommit 응답이 유효하지 않습니다.")
    if history:
        merged = [item for item in pull_requests if item["state"] == "MERGED" or item["mergedAt"]]
        if merged:
            return merged[0]
        opened = [item for item in pull_requests if item["state"] == "OPEN"]
        if len(opened) > 1:
            raise RuntimeError("동일 작업 branch에 열린 PR이 여러 개 있어 자동 연결할 수 없습니다.")
        return opened[0] if opened else pull_requests[0]
    return pull_requests[0]


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
