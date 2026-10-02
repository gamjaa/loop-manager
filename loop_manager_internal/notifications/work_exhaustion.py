"""Deterministic reasons for a stable, non-runnable project selection."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3


@dataclass(frozen=True)
class WorkReasonSnapshot:
    cycle: str
    plans: tuple[dict[str, object], ...]
    requirements: tuple[dict[str, object], ...]
    project_block: dict[str, str] | None
    planning_error: str | None

    @property
    def completed(self) -> bool:
        return not (self.plans or self.requirements or self.project_block or self.planning_error)

    @property
    def has_distinct_wait(self) -> bool:
        # A BLOCKED event already owns a wait consisting only of blocks.
        if any(plan["status"] != "PLAN_BLOCKED" for plan in self.plans):
            return True
        if not self.plans:
            return bool(self.requirements)
        return any(
            requirement.get("independent_wait") is True
            for requirement in self.requirements
        )

    def payload(self, project: str) -> dict[str, object]:
        return {
            "project": project,
            "cycle": self.cycle,
            "plans": list(self.plans),
            "requirements": list(self.requirements),
            "project_block": self.project_block,
            "planning_error": self.planning_error,
        }

    def describe(self) -> str:
        if self.completed:
            return "Planner가 추가 작업이 없음을 확인했고 활성 Requirement와 비종료 ExecPlan이 없습니다."
        parts = []
        for plan in self.plans:
            reason = plan.get("reason") or plan["status"]
            if plan.get("action_targets"):
                reason = f"{reason} (사용자 확인 대상: {', '.join(plan['action_targets'])})"
            parts.append(f"{plan['id']} {plan['title']}: {reason}")
        for requirement in self.requirements:
            parts.append(f"{requirement['id']}: {requirement['reason']}")
        if self.project_block:
            parts.append("PROJECT_BLOCKED: " + self.project_block["reason"])
        if self.planning_error:
            parts.append("planning_error: " + self.planning_error)
        return "; ".join(parts)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _action_targets(connection: sqlite3.Connection, repo: str, plan_id: str) -> list[str]:
    if not _table_exists(connection, "recovery_records"):
        return []
    record = connection.execute(
        "SELECT final_evidence FROM recovery_records "
        "WHERE repo = ? AND subject = ?", (repo, plan_id),
    ).fetchone()
    if not record or not record[0]:
        return []
    try:
        evidence = json.loads(str(record[0]))
    except (TypeError, ValueError):
        return []
    targets = evidence.get("user_targets") if isinstance(evidence, dict) else None
    return [str(target) for target in targets] if isinstance(targets, list) else []


def collect(
    connection: sqlite3.Connection,
    repo: str,
    *,
    cycle: str,
    active_requirements: tuple[str, ...],
) -> WorkReasonSnapshot | None:
    """Read only confirmed SQLite causes; None means work may still be runnable."""

    history = {}
    if _table_exists(connection, "execplan_catalog_history"):
        history = {
            str(row[0]): bool(row[1]) and not row[2]
            for row in connection.execute(
                "SELECT plan_id, merge_confirmed, diagnostic_code FROM execplan_catalog_history "
                "WHERE repo = ? AND currently_archived = 1", (repo,),
            )
        }
    rows = connection.execute(
        "SELECT plan_id, title, status, depends_on, pr_url, sources "
        "FROM exec_plans WHERE repo = ? ORDER BY plan_id", (repo,),
    ).fetchall()
    statuses = {
        str(row["plan_id"]): (
            "MERGED" if history.get(str(row["plan_id"])) else
            "HISTORICAL_UNVERIFIED" if str(row["plan_id"]) in history else
            str(row["status"])
        )
        for row in rows
    }
    blocks = {}
    if _table_exists(connection, "execplan_blocks"):
        blocks = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT plan_id, reason_code, evidence FROM execplan_blocks "
                "WHERE repo = ?", (repo,),
            )
        }
    plans = []
    for row in rows:
        plan_id = str(row["plan_id"])
        if plan_id in history or row["status"] == "MERGED":
            continue
        status = str(row["status"])
        item: dict[str, object] = {
            "id": plan_id, "title": str(row["title"]), "status": status,
        }
        if status == "READY":
            dependencies = json.loads(str(row["depends_on"]))
            missing = [
                {"id": str(dependency), "status": statuses.get(str(dependency), "UNKNOWN")}
                for dependency in dependencies
                if statuses.get(str(dependency)) != "MERGED"
            ]
            if not missing:
                return None
            item["dependencies"] = missing
            item["reason"] = "미충족 dependency " + ", ".join(
                f"{entry['id']} ({entry['status']})" for entry in missing
            )
        elif status == "PR_OPEN":
            item["reason"] = "PR Merge 대기"
            if row["pr_url"]:
                item["pr_url"] = str(row["pr_url"])
        elif status == "PLAN_BLOCKED":
            block = blocks.get(plan_id)
            if block:
                item["reason_code"], item["evidence"] = block
                item["reason"] = f"{block[0]}: {block[1]}"
            else:
                item["reason"] = "구조화된 차단 원인 확인 대기"
            targets = _action_targets(connection, repo, plan_id)
            if targets:
                item["action_targets"] = targets
        else:
            # An active execution or unknown state is not stable exhaustion.
            return None
        plans.append(item)

    project = connection.execute(
        "SELECT planning_error FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    planning_error = str(project[0]) if project and project[0] else None
    project_block = None
    if _table_exists(connection, "project_blocks"):
        block = connection.execute(
            "SELECT reason_code, evidence FROM project_blocks WHERE repo = ?", (repo,),
        ).fetchone()
        if block:
            project_block = {"code": str(block[0]), "reason": str(block[1])}

    requirement_reasons = []
    for path in sorted(active_requirements):
        match = re.match(r"docs/requirements/(\[R-\d+\])", path)
        identifier = match.group(1) if match else path
        related = []
        for row in rows:
            try:
                sources = json.loads(str(row["sources"]))
            except (TypeError, ValueError):
                sources = []
            if path in sources:
                related.append(statuses[str(row["plan_id"])])
        if related and all(status == "MERGED" for status in related):
            reason = "관련 계획은 Merge됐고 archive 게시 확인을 기다립니다"
        elif not related:
            reason = "Planner는 추가 계획이 없다고 확인했지만 활성 Requirement가 남아 있습니다"
        elif all(status in {"MERGED", "PLAN_BLOCKED"} for status in related):
            reason = "관련 PLAN_BLOCKED 계획의 사용자 조치를 기다립니다"
        else:
            reason = "관련 계획 또는 archive 확인을 기다립니다"
        independent_wait = not related or all(
            status == "MERGED" for status in related
        ) or any(status in {"READY", "PR_OPEN"} for status in related)
        requirement_reasons.append({
            "id": identifier, "path": path, "reason": reason,
            "independent_wait": independent_wait,
        })

    return WorkReasonSnapshot(
        cycle, tuple(plans), tuple(requirement_reasons), project_block, planning_error,
    )
