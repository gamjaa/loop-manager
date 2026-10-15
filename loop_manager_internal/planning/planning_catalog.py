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

from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot


class PlanningResultInvalid(RuntimeError):
    """A Planner result that may be repaired in the same Agent session."""



class PlannerValidationError(PlanningResultInvalid):
    """A Planner output error that is eligible for a correction attempt."""



@dataclass(frozen=True)
class MutableReadySnapshot:
    """PR-free declarations that one general Planner turn may revise."""

    repo: str
    active_sha: str
    plans: tuple[ReadyPlanSnapshot, ...]
    token: str
    recovery_goals: tuple[str, ...] = ()

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
                "deleted_goals": [
                    {"id": goal["id"], "path": goal["path"],
                     "deletion_revision": goal["deletion_revision"], "affected_ids": goal["affected_ids"],
                     "text": json.loads(goal["evidence"])["text"],
                     "kind": goal.get("kind", "cancelled"),
                     "unknown_reason": json.loads(goal["evidence"]).get("unknown_reason"),
                     "plan_revision": (json.loads(goal["evidence"])["row"].get("current_revision_sha")
                                       or json.loads(goal["evidence"])["row"].get("plan_sha")),
                     "sources": json.loads(json.loads(goal["evidence"])["row"]["sources"])}
                    for goal in (json.loads(raw) for raw in self.recovery_goals)
                ],
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
                        "draft": plan.draft,
                        "started": bool(plan.execution_state and json.loads(plan.execution_state).get("ever_started")),
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
    judgment: dict | None = None


def unstarted_snapshot(snapshot: MutableReadySnapshot) -> MutableReadySnapshot:
    """코드 전용 점검 Planner의 기존 미착수 범위를 유지한다."""
    from loop_manager_internal.maintenance.maintenance_orchestration import is_unowned_ready
    from loop_manager_internal.storage.planning_publication_state import _MUTABLE_READY_EVIDENCE_FIELDS
    def eligible(plan):
        if plan.execution_state is None:
            return True
        row = json.loads(plan.execution_state)
        return (is_unowned_ready(row) and not row["retry_count"]
                and all(row[field] is None for field in _MUTABLE_READY_EVIDENCE_FIELDS)
                and all(row[field] == "{}" for field in
                        ("verify_failure_history", "review_failure_history", "review_level_history")))
    plans = tuple(plan for plan in snapshot.plans if eligible(plan))
    return MutableReadySnapshot(snapshot.repo, snapshot.active_sha, plans,
        mutable_snapshot_token(snapshot.repo, snapshot.active_sha, plans, snapshot.recovery_goals), snapshot.recovery_goals)


def mutable_snapshot_token(
    repo: str,
    active_sha: str,
    plans: Sequence[ReadyPlanSnapshot],
    recovery_goals: tuple[str, ...] = (),
) -> str:
    """Hash every CAS-relevant declaration into a deterministic token."""

    payload = {
        "version": 1,
        "repo": repo,
        "active_sha": active_sha,
        "plans": [snapshot_to_dict(plan) for plan in plans],
    }
    if recovery_goals:
        payload["deleted_goals"] = list(recovery_goals)
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_to_dict(snapshot: ReadyPlanSnapshot) -> dict[str, object]:
    payload = {
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
    if snapshot.current_metadata_recorded:
        payload["current_complexity"] = snapshot.current_complexity
        payload["current_risk"] = snapshot.current_risk
    if snapshot.execution_state is not None:
        payload["execution_state"] = snapshot.execution_state
    if snapshot.manual_ownership_state is not None:
        payload["manual_ownership_state"] = snapshot.manual_ownership_state
    return payload


def snapshot_from_dict(value: Mapping[str, object]) -> ReadyPlanSnapshot:
    try:
        cache_fields = {"current_complexity", "current_risk"}
        recorded = cache_fields.issubset(value)
        if cache_fields.intersection(value) and not recorded:
            raise ValueError("incomplete current metadata")
        current_complexity = value.get("current_complexity")
        current_risk = value.get("current_risk")
        execution_state = value.get("execution_state")
        if execution_state is not None:
            if not isinstance(execution_state, str) or not isinstance(json.loads(execution_state), dict):
                raise ValueError("invalid execution snapshot")
        manual_ownership_state = value.get("manual_ownership_state")
        if manual_ownership_state is not None and not isinstance(manual_ownership_state, str):
            raise ValueError("invalid manual ownership snapshot")
        raw_sources = value["sources"]
        raw_dependencies = value["depends_on"]
        if (
            not isinstance(raw_sources, list)
            or not raw_sources
            or any(not isinstance(item, str) for item in raw_sources)
            or not isinstance(raw_dependencies, list)
            or any(not isinstance(item, str) for item in raw_dependencies)
        ):
            raise ValueError("invalid snapshot collections")
        sources = tuple(raw_sources)
        depends_on = tuple(raw_dependencies)
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
            current_complexity=(
                str(current_complexity) if current_complexity is not None else None
            ),
            current_risk=str(current_risk) if current_risk is not None else None,
            current_metadata_recorded=recorded,
            execution_state=execution_state,
            manual_ownership_state=manual_ownership_state,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("mutable READY snapshot manifest가 유효하지 않습니다.") from error
