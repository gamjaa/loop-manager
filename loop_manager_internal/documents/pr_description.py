"""documents pr description boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.application.contracts import WorkerPublication


@dataclass(frozen=True)
class BuildPrBodyEffects:
    """Named effects for build_pr_body; no entry-point/module access."""
    execplan_purpose: Callable[..., object]
    immutable_github_blob_url: Callable[..., object]


def build_pr_body(
    effects: BuildPrBodyEffects, worker: WorkerPublication,
    plan: dict[str, object],
    plan_path: str,
    plan_text: str,
    origin_url: str,
    plan_sha: str,
) -> str:
    plan_id = str(plan["id"])
    plan_title = str(plan["title"])
    plan_link = effects.immutable_github_blob_url(origin_url, plan_sha, plan_path)
    sources = plan["sources"]
    assert isinstance(sources, list)
    source_lines = [
        f"- [{source}]({effects.immutable_github_blob_url(origin_url, plan_sha, str(source))})"
        for source in sources
    ]
    return "\n".join(
        [
            "## Summary",
            worker.summary,
            "",
            "## ExecPlan",
            f"- [{plan_id}: {plan_title}]({plan_link})",
            f"- Purpose: {effects.execplan_purpose(plan_text)}",
            "",
            "## Sources",
            *source_lines,
        ]
    )
