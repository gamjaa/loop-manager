"""Immutable contracts for mutable READY planning batches.

This module deliberately contains no SQLite or Git access.  The bootstrap
adapter creates snapshots from those two sources and the planning lifecycle
passes the immutable values through prompts, validation and publication.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Mapping, Sequence

from maintenance_planning import ReadyPlanSnapshot


@dataclass(frozen=True)
class MutableReadySnapshot:
    """Never-started READY plans that one general Planner turn may mutate."""

    repo: str
    active_sha: str
    plans: tuple[ReadyPlanSnapshot, ...]
    token: str

    @property
    def by_id(self) -> dict[str, ReadyPlanSnapshot]:
        return {plan.plan_id: plan for plan in self.plans}

    @property
    def by_path(self) -> dict[str, ReadyPlanSnapshot]:
        return {plan.path: plan for plan in self.plans}

    def prompt_payload(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "plans": [
                    {
                        "id": plan.plan_id,
                        "path": plan.path,
                        "title": plan.title,
                        "sources": list(plan.sources),
                        "depends_on": list(plan.depends_on),
                        "complexity": plan.complexity,
                        "risk": plan.risk,
                        "priority": plan.priority,
                        "content_hash": plan.content_hash,
                        "plan_revision": plan.plan_sha,
                    }
                    for plan in self.plans
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


@dataclass(frozen=True)
class PlanDeletion:
    """One snapshot-bound READY plan removed by a general Planner batch."""

    snapshot: ReadyPlanSnapshot


def mutable_snapshot_token(
    repo: str,
    active_sha: str,
    plans: Sequence[ReadyPlanSnapshot],
) -> str:
    """Hash every CAS-relevant declaration into a deterministic token."""

    payload = {
        "version": 1,
        "repo": repo,
        "active_sha": active_sha,
        "plans": [snapshot_to_dict(plan) for plan in plans],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_to_dict(snapshot: ReadyPlanSnapshot) -> dict[str, object]:
    return {
        "plan_id": snapshot.plan_id,
        "path": snapshot.path,
        "title": snapshot.title,
        "sources": list(snapshot.sources),
        "depends_on": list(snapshot.depends_on),
        "complexity": snapshot.complexity,
        "risk": snapshot.risk,
        "priority": snapshot.priority,
        "content_hash": snapshot.content_hash,
        "plan_sha": snapshot.plan_sha,
        "text": snapshot.text,
    }


def snapshot_from_dict(value: Mapping[str, object]) -> ReadyPlanSnapshot:
    try:
        sources = tuple(str(item) for item in value["sources"])  # type: ignore[arg-type]
        depends_on = tuple(
            str(item) for item in value["depends_on"]  # type: ignore[arg-type]
        )
        return ReadyPlanSnapshot(
            plan_id=str(value["plan_id"]),
            path=str(value["path"]),
            title=str(value["title"]),
            sources=sources,
            depends_on=depends_on,
            complexity=str(value["complexity"]),
            risk=str(value["risk"]),
            priority=str(value["priority"]),
            content_hash=str(value["content_hash"]),
            plan_sha=str(value["plan_sha"]),
            text=str(value["text"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("mutable READY snapshot manifest가 유효하지 않습니다.") from error
