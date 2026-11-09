"""Deterministic reasons for a stable, non-runnable project selection."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import hashlib
import re
import sqlite3
from urllib.parse import quote
from .rendering import _checkpoint_link
from loop_manager_internal.catalog import requirement_sources
from loop_manager_internal.storage import execplan_draft_state


@dataclass(frozen=True)
class WorkReasonSnapshot:
    cycle: str
    plans: tuple[dict[str, object], ...]
    requirements: tuple[dict[str, object], ...]
    project_block: dict[str, str] | None
    planning_error: str | None
    source_relations: dict[str, dict[str, object]] | None = None
    blocked_selection: bool = False

    @property
    def completed(self) -> bool:
        return not (self.plans or self.requirements or self.project_block or self.planning_error)

    @property
    def has_distinct_wait(self) -> bool:
        return not self.project_block and bool(self.plans or self.requirements)

    def payload(self, project: str) -> dict[str, object]:
        return {
            "project": project,
            "cycle": self.cycle,
            "plans": list(self.plans),
            "requirements": list(self.requirements),
            "project_block": self.project_block,
            "planning_error": self.planning_error,
            "source_relations": self.source_relations,
            **({"blocked_selection": True} if self.blocked_selection else {}),
        }

    def describe(self) -> str:
        if self.completed:
            return "Planner가 추가 작업이 없음을 확인했고 활성 Requirement와 비종료 ExecPlan이 없습니다."
        parts = []
        for plan in self.plans:
            reason = plan.get("reason") or plan["status"]
            if plan.get("action_targets"):
                reason = f"{reason} (사용자 확인 대상: {', '.join(plan['action_targets'])})"
            if plan.get("question"):
                from loop_manager_internal.catalog.execplan_drafts import fields
                reason += ": " + fields(str(plan["question"])).get("Question", str(plan["question"]))
            if plan.get("url"):
                reason += " " + str(plan["url"])
            if plan.get("revision"):
                reason += " (Revision: " + str(plan["revision"]) + ")"
            if self.blocked_selection and plan.get('path'):
                reason += " (문서: " + str(plan['path']) + ")"
            parts.append(f"{plan['id']} {plan['title']}: {reason}")
        for requirement in self.requirements:
            parts.append(f"{requirement['id']}: {requirement['reason']}")
        if self.project_block:
            parts.append("PROJECT_BLOCKED: " + self.project_block["reason"])
        if self.planning_error:
            parts.append("planning_error: " + self.planning_error)
        return ("진행 불가 (시작 가능한 작업 없음): " if self.blocked_selection else "") + "; ".join(parts)


def episode_signature(snapshot: WorkReasonSnapshot) -> str:
    identity = {
        **({"blocked_selection": True} if snapshot.blocked_selection else {}),
        "plans": [{key: value for key, value in plan.items()
                   if key not in {"url", "pr_url", "path", "revision"}}
                  for plan in snapshot.plans],
        "requirements": [{key: value for key, value in requirement.items()
                          if key not in {"path", "url"}} for requirement in snapshot.requirements],
        "project_block": snapshot.project_block,
        "planning_error": snapshot.planning_error,
        "sources": {plan_id: [requirement_sources.source_requirement_id(source) or source
                              for source in item["sources"]]
                    for plan_id, item in (snapshot.source_relations or {}).items()
                    if any(plan["id"] == plan_id for plan in snapshot.plans)},
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def execution_pending(connection: sqlite3.Connection, repo: str) -> bool:
    """Unfinished or uncertain calls remain authoritative across projections."""
    for table, condition in (("task_call_reservations", "1"),
                             ("agent_invocations", "finished_at IS NULL")):
        if _table_exists(connection, table) and connection.execute(
            f"SELECT 1 FROM {table} WHERE repo=? AND {condition} LIMIT 1", (repo,),
        ).fetchone():
            return True
    return False


def _action_targets(connection: sqlite3.Connection, repo: str, plan_id: str,
                    reason_code: str, cause: str) -> list[str]:
    # Raw recovery user_targets alone do not establish a current user action.
    from .policy import block_action
    action = block_action(connection, repo, {
        "scope": "plan", "execplan_id": plan_id, "reason_code": reason_code, "cause": cause,
    })
    if not action:
        return []
    decision = action.get("decision")
    if isinstance(decision, dict):
        return [location["path"] for location in decision.get("locations", [])
                if isinstance(location, dict) and isinstance(location.get("path"), str)]
    if action.get("kind") == "guidance":
        return [proposal[2] for proposal in action["proposals"]]
    return []


def collect(
    connection: sqlite3.Connection,
    repo: str,
    *,
    cycle: str,
    active_requirements: tuple[str, ...],
    requirement_documents: dict[str, bytes] | None = None,
    base_revision: str | None = None,
    source_relations: dict[str, dict[str, object]] | None = None,
) -> WorkReasonSnapshot | None:
    """Read only confirmed SQLite causes; None means work may still be runnable."""

    history = {}
    if _table_exists(connection, "execplan_catalog_history"):
        history = {
            str(row[0]): False
            for row in connection.execute(
                "SELECT plan_id FROM execplan_catalog_history "
                "WHERE repo = ? AND currently_archived = 1", (repo,),
            )
        }
        from loop_manager_internal.storage.execplan_catalog_state import verified_historical_merge_ids
        history.update({plan_id: True for plan_id in verified_historical_merge_ids(connection, repo)})
    rows = connection.execute(
        "SELECT plan_id, title, status, depends_on, pr_url, sources, path "
        "FROM exec_plans WHERE repo = ? ORDER BY plan_id", (repo,),
    ).fetchall()
    holds = {}
    if _table_exists(connection, "execplan_catalog_holds"):
        holds = {str(row[0]): str(row[1]) for row in connection.execute(
            "SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo = ?", (repo,),
        )}
    statuses = {
        str(row["plan_id"]): (
            "CATALOG_HELD" if str(row["plan_id"]) in holds else
            "MERGED" if history.get(str(row["plan_id"])) else
            "HISTORICAL_UNVERIFIED" if str(row["plan_id"]) in history else
            str(row["status"])
        )
        for row in rows
    }
    statuses.update({plan_id: "MERGED" for plan_id, confirmed in history.items()
                     if confirmed and plan_id not in statuses})
    drafts = execplan_draft_state.pending_ids(connection, repo)
    statuses.update({plan_id: "DRAFT" for plan_id in drafts if plan_id not in holds})
    blocks = {}
    if _table_exists(connection, "execplan_blocks"):
        blocks = {
            str(row[0]): (str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT plan_id, reason_code, evidence FROM execplan_blocks "
                "WHERE repo = ?", (repo,),
            )
        }
    project = connection.execute(
        "SELECT planning_error, origin FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    tree = _checkpoint_link(project["origin"], base_revision) if project else None
    origin = tree.split("/tree/", 1)[0] if tree else None
    def document_url(path: str) -> str | None:
        return f'{origin}/blob/{base_revision}/{quote(path, safe="/")}' if origin else None

    project_block = None
    if _table_exists(connection, "project_blocks"):
        block = connection.execute(
            "SELECT reason_code, evidence FROM project_blocks WHERE repo = ?", (repo,),
        ).fetchone()
        if block:
            project_block = {"code": str(block[0]), "reason": str(block[1])}

    plans = []
    for row in rows:
        plan_id = str(row["plan_id"])
        if plan_id not in holds and (plan_id in history or row["status"] == "MERGED"):
            continue
        status = statuses[plan_id]
        item: dict[str, object] = {
            "id": plan_id, "title": str(row["title"]), "status": status,
            "path": str(row["path"]),
        }
        if document_url(str(row["path"])):
            item["url"] = document_url(str(row["path"]))
        if status == "CATALOG_HELD":
            item.update(reason="ExecPlan catalog 오류의 교정 대기",
                        reason_code="execplan_catalog_invalid", evidence=holds[plan_id])
        elif status == "DRAFT":
            waiting = execplan_draft_state.evidence(connection, repo, plan_id)
            item.update(reason="ExecPlan 기존 결정 유효성 재검토" if waiting.get("reassessment") else "ExecPlan 사용자 판단 대기", question=waiting["question"],
                        path=waiting["path"], revision=waiting["revision"])
            item.pop("url", None)
            if waiting.get("url"):
                item["url"] = waiting["url"]
        elif status in {"READY", "IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING"}:
            dependencies = json.loads(str(row["depends_on"]))
            missing = [
                {"id": str(dependency), "status": statuses.get(str(dependency), "UNKNOWN")}
                for dependency in dependencies
                if statuses.get(str(dependency)) != "MERGED"
            ]
            if not missing:
                if project_block is None or status != "READY":
                    return None
                item["reason"] = "프로젝트 차단 해소 후 실행 조건을 다시 확인합니다"
            else:
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
            if not block or not block[0].strip() or not block[1].strip():
                return None
            item["reason_code"], item["evidence"] = block
            item["reason"] = f"{block[0]}: {block[1]}"
            targets = _action_targets(connection, repo, plan_id, block[0], block[1])
            if targets:
                item["action_targets"] = targets
        else:
            # An active execution or unknown state is not stable exhaustion.
            return None
        plans.append(item)

    known_ids = {str(row["plan_id"]) for row in rows}
    plans.extend({"id": key, "title": key, "status": "CATALOG_HELD",
                  "reason": "ExecPlan catalog 오류의 교정 대기",
                  "reason_code": "execplan_catalog_invalid", "evidence": holds[key]}
                 for key in sorted(holds.keys() - known_ids))
    planning_error = str(project["planning_error"]) if project and project["planning_error"] else None

    relation_evidence = {}
    for row in rows:
        try:
            sources = json.loads(str(row["sources"]))
        except (TypeError, ValueError):
            return None
        if not isinstance(sources, list) or not all(isinstance(source, str) for source in sources):
            return None
        plan_id = str(row["plan_id"])
        if source_relations is not None:
            saved = source_relations.get(plan_id)
            if not isinstance(saved, dict) or saved.get("sources") != sources or not isinstance(saved.get("requirements"), list):
                return None
        relation_evidence[plan_id] = {"sources": sources, "requirements": []}
    if source_relations is not None and any(
        not isinstance(source_relations[plan_id], dict)
        or source_relations[plan_id].get("requirements") != []
        for plan_id in set(source_relations) - set(relation_evidence)
    ):
        return None

    requirement_reasons = []
    for path in sorted(active_requirements):
        match = re.match(r"docs/requirements/(\[R-\d+\])", path)
        identifier = match.group(1) if match else path
        related = []
        for row in rows:
            plan_id = str(row["plan_id"])
            sources = relation_evidence[plan_id]["sources"]
            relation = path in sources
            if source_relations is not None:
                relation = path in source_relations[plan_id]["requirements"]
            elif not relation and requirement_documents is not None:
                for source in sources:
                    if (not isinstance(source, str)
                        or not source.startswith("docs/requirements/")
                        or source.count("/") != 2):
                        continue
                    try:
                        resolved = requirement_sources.resolve_requirement_source(
                            source, requirement_documents,
                        )
                    except requirement_sources.RequirementSourceLookupError:
                        return None
                    if resolved.status is requirement_sources.ResolutionStatus.AMBIGUOUS:
                        return None
                    if resolved.path == path:
                        relation = True
                        break
            if relation:
                relation_evidence[plan_id]["requirements"].append(path)
                related.append(statuses[plan_id])
        if related and all(status == "MERGED" for status in related):
            judgment = None
            if _table_exists(connection, "requirement_judgments"):
                judgment = connection.execute(
                    "SELECT outcome, invalidation_reason, criteria_evidence FROM requirement_judgments "
                    "WHERE repo = ? AND source_path = ?", (repo, path),
                ).fetchone()
            if judgment is None or judgment[1]:
                reason = "관련 계획 Merge 후 일반 Planner의 전체 충족 판단을 기다립니다"
            elif judgment[0] == "fulfilled":
                reason = "전체 충족 판단 후 Manager의 내용 보존 이동 게시를 기다립니다"
            elif judgment[0] == "unfulfilled":
                reason = "전체 기준 미충족 판단에 따른 후속 계획을 기다립니다"
            else:
                evidence = json.loads(str(judgment[2]))
                reason = (
                    "전체 충족 판단이 불확실하여 사용자 결정을 기다립니다: "
                    + "; ".join(str(item) for item in evidence)
                    + f". 사용자 확인 대상: {path}. 해당 문서에 결정·근거를 명시해 수정하면 "
                    "일반 Planner가 변경 입력을 재평가합니다."
                )
        elif not related:
            reason = "Planner는 추가 계획이 없다고 확인했지만 활성 Requirement가 남아 있습니다"
        elif all(status in {"MERGED", "PLAN_BLOCKED"} for status in related):
            reason = "관련 PLAN_BLOCKED 계획의 차단 해소와 재평가를 기다립니다"
        else:
            reason = "관련 계획 또는 archive 확인을 기다립니다"
        independent_wait = not related or all(
            status == "MERGED" for status in related
        ) or any(status in {"READY", "PR_OPEN"} for status in related)
        requirement_reasons.append({
            "id": identifier, "path": path, "reason": reason,
            "independent_wait": independent_wait,
            **({"url": document_url(path)} if document_url(path) else {}),
        })

    return WorkReasonSnapshot(
        cycle, tuple(plans), tuple(requirement_reasons), project_block, planning_error,
        relation_evidence,
    )


def blocked_selection(connection: sqlite3.Connection, repo: str) -> WorkReasonSnapshot | None:
    """Explain a completed selection with durable blockers, not a stale error."""
    from loop_manager_internal.storage import maintenance_state, planning_review_state, runtime_state
    project = connection.execute('SELECT * FROM projects WHERE repo=?', (repo,)).fetchone()
    if (project is None or project['phase'] not in
            {None, 'WORKING', 'VERIFYING', 'REVIEW', 'PUBLISHING', 'PR_OPEN', 'PLAN_BLOCKED', 'PROJECT_BLOCKED'}
            or project['pr_conflict_recovery'] or runtime_state.planning_owns_execution_slot(project)
            or execution_pending(connection, repo)
            or planning_review_state.general_pending(connection, repo)):
        return None
    cycle = maintenance_state.MaintenanceRepository(connection).get(repo)
    if cycle.phase is not maintenance_state.MaintenancePhase.IDLE or cycle.publication_stage is not None:
        return None
    snapshot = collect(connection, repo, cycle=str(project['planning_fingerprint'] or ''),
                       active_requirements=(), base_revision=project['processed_sha'])
    if snapshot is None or (not snapshot.project_block and not any(
            plan['status'] in {'DRAFT', 'PLAN_BLOCKED', 'CATALOG_HELD'} for plan in snapshot.plans)):
        return None
    return replace(snapshot, blocked_selection=True)
