"""Revision-proven cancellation and Planner-owned dependency goal recovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from loop_manager_internal.catalog import execplan_catalog as catalog
from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
from loop_manager_internal.maintenance.maintenance_orchestration import is_unowned_ready
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.storage.planning_publication_state import _mutable_ready_cas_where


@dataclass(frozen=True)
class RemoteDeletion:
    snapshot: ReadyPlanSnapshot
    evidence: str


def revisable_row(row):
    return (str(row["status"]) in {"READY", "IMPLEMENTING", "VERIFYING", "REVIEWING", "PUBLISHING", "PLAN_BLOCKED"}
            and all(row[field] is None for field in ("pr_url", "pr_head_oid", "pr_conflict_head_oid", "pr_conflict_base_oid"))
            and not int(row["pr_conflict_recovery"] or 0))


def mutable_row(connection, repo, plan_id):
    row = connection.execute(
        "SELECT * FROM exec_plans WHERE repo=? AND plan_id=? AND " + _mutable_ready_cas_where(),
        (repo, plan_id),
    ).fetchone()
    if row is None or not is_unowned_ready(row):
        return None
    project = connection.execute("SELECT phase, plan_path, branch, worktree, pr_url FROM projects WHERE repo=?", (repo,)).fetchone()
    if (project is not None and project["plan_path"] == row["path"] and project["phase"] != "PLANNING"
            and (project["phase"] or project["branch"] or project["worktree"] or project["pr_url"])):
        return None
    return row


def reference_issues(connection, repo, repository, pinned, *, git_bytes):
    """Observe deletion impacts independently of cancellation eligibility."""
    rows = connection.execute("SELECT * FROM exec_plans WHERE repo=?", (repo,)).fetchall()
    missing = {str(row["plan_id"]): str(row["path"]) for row in rows
               if str(row["plan_id"]) not in pinned.snapshot.observed_ids}
    if not missing:
        return ()
    latest_contents = {key: entry.text.encode("utf-8") for key, entry in pinned.snapshot.all.items()}
    paths = {str(row["plan_id"]): str(row["path"]) for row in rows}
    withheld_paths = {key: path for key, path, _hash in pinned.snapshot.withheld}
    paths.update(withheld_paths)
    paths.update({key: entry.path for key, entry in pinned.snapshot.all.items()})
    previous_contents = {}
    for row in rows:
        revision = str(row["current_revision_sha"] or row["plan_sha"] or "")
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
            contents = git_bytes(repository, "show", f"{revision}:{row['path']}")
            if catalog.plan_content_hash(contents) == row["content_hash"]:
                previous_contents[str(row["plan_id"])] = contents
    prior = [(str(row["plan_id"]), json.loads(row["evidence"])) for row in connection.execute(
        "SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo=?", (repo,))]
    issues = []
    for key, path in missing.items():
        reference = re.compile(rb"(?<![A-Za-z0-9-])" + key.encode("ascii") + rb"(?![A-Za-z0-9-])")
        reference_path = path.encode("utf-8")
        affected = {str(row["plan_id"]) for row in rows if key in json.loads(row["depends_on"])}
        affected.update(other for other, contents in previous_contents.items()
                        if reference_path in contents or reference.search(contents))
        for other in pinned.snapshot.observed_ids:
            latest = latest_contents.get(other)
            latest_path = withheld_paths.get(other)
            if (latest is None and latest_path is not None and pinned.regular_paths is not None
                    and latest_path in pinned.regular_paths):
                latest = git_bytes(repository, "show", f"{pinned.revision_sha}:{latest_path}")
                latest_contents[other] = latest
            if latest is not None and (reference_path in latest or reference.search(latest)):
                affected.add(other)
        # Keep unadopted references until their missing prerequisite returns;
        # removing a reference cannot establish that its goal is unnecessary.
        for other, evidence in prior:
            for cause in evidence.get("causes", ()):
                if (cause["plan_id"] == other and cause["code"] == "missing-plan-reference"
                        and cause.get("source") == path):
                    affected.add(other)
                    paths.setdefault(other, evidence["path"])
        approvals = dict(prior).get(key, {}).get("resolved_references", {})
        for other, approval in approvals.items():
            contents = latest_contents.get(other)
            dependencies = next((json.loads(row["depends_on"]) for row in rows
                                 if row["plan_id"] == other), ())
            if (contents is not None and key not in dependencies
                    and approval.get("path") == paths.get(other)
                    and approval.get("content_hash") == catalog.plan_content_hash(contents)):
                affected.discard(other)
        issues.extend(catalog.CatalogIssue(other, paths[other], "missing-plan-reference",
            f"삭제된 계획의 참조 근거를 보존합니다: {other} -> {key}", path)
            for other in sorted(affected - {key}))
    return tuple(issues)


def prove_deletions(connection, repo, repository, pinned, *, git, git_bytes, references=None):
    """Absence alone cannot cancel a row: prove its last adopted bytes/ancestry."""
    from loop_manager_internal.storage.execplan_catalog_state import DefinitionStateRepository
    state = DefinitionStateRepository(connection).capture(repo)
    reserved = {item[0] for item in state.reserved}
    if references is None:
        references = reference_issues(connection, repo, repository, pinned, git_bytes=git_bytes)
    deletions = []
    rows = connection.execute("SELECT * FROM exec_plans WHERE repo=?", (repo,)).fetchall()
    for row in rows:
        key = str(row["plan_id"])
        if key in pinned.snapshot.observed_ids or key in reserved or mutable_row(connection, repo, key) is None:
            continue
        revision = str(row["current_revision_sha"] or row["plan_sha"] or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
            continue
        ancestry = git(repository, "merge-base", "--is-ancestor", revision, pinned.revision_sha, check=False)
        if ancestry.returncode == 1:
            continue
        if ancestry.returncode:
            raise RuntimeError("삭제 계획의 채택 revision 이력을 확인할 수 없습니다.")
        path = str(row["path"])
        tree = git_bytes(repository, "ls-tree", "-z", revision, "--", path)
        if not tree.startswith((b"100644 blob ", b"100755 blob ")):
            continue
        contents = git_bytes(repository, "show", f"{revision}:{path}")
        if catalog.plan_content_hash(contents) != row["content_hash"]:
            continue
        try:
            text = contents.decode("utf-8")
            plan = catalog.parse_execplan_text(path, text, validate_sources=False)
        except (UnicodeError, RuntimeError):
            continue  # An unproven legacy declaration stays held.
        if (plan["id"] != key or plan["title"] != row["title"]
                or plan["sources"] != json.loads(row["sources"])
                or plan["depends_on"] != json.loads(row["depends_on"])
                or plan["priority"] != row["priority"]):
            continue
        original = ReadyPlanSnapshot(
            key, path, str(plan["title"]), tuple(plan["sources"]), tuple(plan["depends_on"]),
            str(plan["complexity"]), str(plan["risk"]), str(plan["priority"]),
            str(row["content_hash"]), revision, text,
            current_complexity=row["current_complexity"], current_risk=row["current_risk"],
            current_metadata_recorded=True,
        )
        affected = {issue.plan_id for issue in references if issue.source == path}
        evidence = json.dumps({"text": text, "row": dict(row), "affected": sorted(affected),
                               "resolved": {}}, ensure_ascii=False, sort_keys=True)
        deletions.append(RemoteDeletion(original, evidence))
    return tuple(deletions)


def recovery_definitions(connection, repo, repository, pinned, *, git_bytes):
    """Adopt affected READY and new declarations under a hold for Planner repair."""
    affected = {key for raw in recovery_goals(connection, repo) for key in json.loads(raw)["affected_ids"]}
    affected.update(key for item in pinned.deletions for key in json.loads(item.evidence)["affected"])
    affected.update(issue.plan_id for issue in pinned.snapshot.issues if issue.code == "missing-plan-reference")
    paths = {key: path for key, path, _hash in pinned.snapshot.withheld}
    paths.update({key: entry.path for key, entry in pinned.snapshot.all.items()})
    stored_ids = {str(row[0]) for row in connection.execute("SELECT plan_id FROM exec_plans WHERE repo=?", (repo,))}
    entries = []
    for key in sorted(affected):
        row = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?", (repo, key)).fetchone()
        if (key not in paths or (row is not None and not revisable_row(row))
                or any(issue.plan_id == key and issue.code not in
                       {"missing-dependency", "missing-plan-reference", "missing-source"}
                       for issue in pinned.snapshot.issues)):
            continue
        path = paths[key]
        if pinned.regular_paths is None or path not in pinned.regular_paths:
            continue
        text = git_bytes(repository, "show", f"{pinned.revision_sha}:{path}").decode("utf-8")
        plan = catalog.parse_execplan_text(path, text, validate_sources=False)
        entry = catalog._entry(path, text, plan)
        if key in stored_ids or not entry.archived:
            entries.append(entry)
    return tuple(entries)


def recovery_goals(connection, repo):
    """Keep cancellation evidence immutable; resolution is publication-bound."""
    from loop_manager_internal.recovery.scoped_blocking import catalog_held_plan_ids
    goals = []
    rows = connection.execute("SELECT * FROM exec_plans WHERE repo=?", (repo,)).fetchall()
    observed_ids = {str(row["plan_id"]) for row in rows}
    held_ids = catalog_held_plan_ids(connection, repo)
    observed_ids.update(held_ids)
    for cancelled in connection.execute(
        "SELECT * FROM execplan_cancellations WHERE repo=? AND recovery_evidence IS NOT NULL ORDER BY plan_id", (repo,),
    ):
        try:
            evidence = json.loads(cancelled["recovery_evidence"])
            if (not isinstance(evidence, dict) or not isinstance(evidence.get("affected"), list)
                    or not all(isinstance(item, str) for item in evidence["affected"])
                    or not isinstance(evidence.get("resolved"), dict)
                    or not isinstance(evidence.get("text"), str) or not isinstance(evidence.get("row"), dict)):
                raise ValueError("invalid deletion evidence")
        except (ValueError, TypeError) as error:
            raise PlannerValidationError("삭제 목표 감사 JSON이 유효하지 않습니다.") from error
        key = str(cancelled["plan_id"])
        affected = sorted(((set(evidence["affected"]) & observed_ids) - evidence["resolved"].keys())
            | {str(row["plan_id"]) for row in rows if key in json.loads(row["depends_on"])})
        if affected:
            goals.append(json.dumps({"id": key, "path": cancelled["path"],
                "deletion_revision": cancelled["deletion_commit"], "evidence": cancelled["recovery_evidence"],
                "affected_ids": affected}, ensure_ascii=False, sort_keys=True))
    if not held_ids:
        return tuple(goals)
    holds = {str(row["plan_id"]): json.loads(row["evidence"]) for row in connection.execute(
        "SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo=?", (repo,))}
    for row in rows:
        key, path = str(row["plan_id"]), str(row["path"])
        held = holds.get(key, {})
        if not any(cause["plan_id"] == key and cause["code"] == "missing-plan"
                   for cause in held.get("causes", ())):
            continue
        affected = {cause["plan_id"] for evidence in holds.values()
                    for cause in evidence.get("causes", ())
                    if cause["code"] == "missing-plan-reference" and cause.get("source") == path}
        affected.update(str(item["plan_id"]) for item in rows if key in json.loads(item["depends_on"]))
        approvals = held.get("resolved_references", {})
        affected.difference_update(str(item["plan_id"]) for item in rows
            if key not in json.loads(item["depends_on"])
            and approvals.get(str(item["plan_id"]), {}).get("path") == item["path"]
            and approvals.get(str(item["plan_id"]), {}).get("content_hash") == item["content_hash"])
        definition = json.loads(row["definition_snapshot"]) if row["definition_snapshot"] else {}
        evidence = json.dumps({"text": definition.get("body_text") or "", "row": dict(row),
                               "affected": sorted(affected), "resolved": {}},
                              ensure_ascii=False, sort_keys=True)
        goals.append(json.dumps({"id": key, "path": path, "kind": "missing-plan",
            "deletion_revision": held.get("revision"), "evidence": evidence,
            "affected_ids": sorted(affected)}, ensure_ascii=False, sort_keys=True))
    return tuple(goals)


def recoverable_ids(connection, repo, held):
    """실행 보류와 참조 복구 검토 권한을 분리한다."""
    allowed = set()
    dependencies = {str(row["plan_id"]): json.loads(row["depends_on"]) for row in connection.execute(
        "SELECT plan_id, depends_on FROM exec_plans WHERE repo=?", (repo,))}
    for row in connection.execute("SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo=?", (repo,)):
        evidence = json.loads(row["evidence"])
        causes = evidence.get("causes", ())
        closure, pending = set(), [str(row["plan_id"])]
        while pending:
            key = pending.pop()
            if key not in closure:
                closure.add(key)
                pending.extend(dependencies.get(key, ()))
        relevant = [item for item in causes if item["plan_id"] in closure]
        if relevant and all(item["code"] in {"missing-dependency", "deleted-goal", "missing-plan",
                                             "missing-plan-reference", "missing-source"} for item in relevant):
            allowed.add(str(row["plan_id"]))
    affected = {key for raw in recovery_goals(connection, repo) for key in json.loads(raw)["affected_ids"]}
    return frozenset(allowed & affected & set(held))


def validate_recovery(worktree, snapshot, current_plans, catalog_metadata):
    """Require a Planner judgment and retained goal gates, never infer meaning."""
    for raw in snapshot.recovery_goals:
        goal = json.loads(raw)
        for key in set(goal["affected_ids"]) & snapshot.by_id.keys():
            changed = [(path, plan) for path, plan in current_plans.items() if plan["id"] == key]
            if (not changed and snapshot.by_id[key].path not in current_plans
                    and (worktree / snapshot.by_id[key].path).exists()):
                continue  # Uncertain goals remain held while independent work is published.
            if len(changed) != 1:
                raise PlannerValidationError(f"삭제 목표 복구에는 영향 계획의 명시적 개정이 필요합니다: {key}")
            path, plan = changed[0]
            text = (worktree / path).read_text(encoding="utf-8")
            log = text.partition("## Decision Log")[2]
            decisions = []
            for line in log.splitlines():
                if line.startswith("DEPENDENCY_RECOVERY:"):
                    try:
                        decisions.append(json.loads(line.partition(":")[2]))
                    except ValueError as error:
                        raise PlannerValidationError("DEPENDENCY_RECOVERY JSON이 유효하지 않습니다.") from error
            matching = [item for item in decisions if isinstance(item, dict) and item.get("deleted_id") == goal["id"]]
            if len(matching) != 1:
                raise PlannerValidationError(f"삭제된 선행 목표의 판단이 필요합니다: {key} -> {goal['id']}")
            decision = matching[0]
            sources = decision.get("sources")
            if (not isinstance(decision.get("reason"), str) or not decision["reason"].strip()
                    or not isinstance(sources, list) or not sources
                    or not all(isinstance(source, str) and source in plan["sources"] for source in sources)):
                raise PlannerValidationError("삭제 목표 판단에는 최신 source와 구체적인 이유가 필요합니다.")
            replacements = decision.get("replacement_ids")
            if not isinstance(replacements, list) or not all(isinstance(item, str) for item in replacements):
                raise PlannerValidationError("삭제 목표의 replacement_ids가 유효하지 않습니다.")
            if decision.get("outcome") == "replaced":
                if (not replacements or not set(replacements) <= set(plan["depends_on"])
                        or any(item == key or item not in catalog_metadata for item in replacements)):
                    raise PlannerValidationError("필요한 선행 목표를 유효한 대체 dependency로 연결해야 합니다.")
            elif decision.get("outcome") == "obsolete":
                if replacements:
                    raise PlannerValidationError("불필요한 목표 판단에 대체 ID를 사용할 수 없습니다.")
            else:
                raise PlannerValidationError("삭제 목표가 불확실하면 영향 범위를 계속 보류해야 합니다.")


def resolve_recovery(connection, snapshot, commit, revised_ids):
    """Apply validated judgments with the same transaction as the whole batch."""
    for raw in snapshot.recovery_goals:
        goal = json.loads(raw)
        targets = set(goal["affected_ids"]) & snapshot.by_id.keys() & revised_ids
        if goal.get("kind") == "missing-plan":
            if not targets:
                continue
            row = connection.execute("SELECT evidence FROM execplan_catalog_holds WHERE repo=? AND plan_id=?",
                                     (snapshot.repo, goal["id"])).fetchone()
            if row is None:
                raise PlannerValidationError("참조 복구의 삭제 보류 근거가 변경됐습니다.")
            held = json.loads(row[0])
            approvals = held.setdefault("resolved_references", {})
            for key in targets:
                plan = connection.execute("SELECT path, content_hash FROM exec_plans WHERE repo=? AND plan_id=?",
                                          (snapshot.repo, key)).fetchone()
                approvals[key] = {"revision": commit, "path": plan["path"], "content_hash": plan["content_hash"]}
            connection.execute("UPDATE execplan_catalog_holds SET evidence=? WHERE repo=? AND plan_id=?",
                (json.dumps(held, ensure_ascii=False, sort_keys=True), snapshot.repo, goal["id"]))
            continue
        row = connection.execute("SELECT recovery_evidence FROM execplan_cancellations WHERE repo=? AND plan_id=?",
                                 (snapshot.repo, goal["id"])).fetchone()
        if row is None or row[0] != goal["evidence"]:
            raise PlannerValidationError("삭제 목표 감사가 planning snapshot 이후 변경됐습니다.")
        evidence = json.loads(row[0])
        evidence["resolved"].update({key: commit for key in targets})
        connection.execute("UPDATE execplan_cancellations SET recovery_evidence=? WHERE repo=? AND plan_id=?",
            (json.dumps(evidence, ensure_ascii=False, sort_keys=True), snapshot.repo, goal["id"]))
    targets = {key for raw in snapshot.recovery_goals for key in json.loads(raw)["affected_ids"]} & snapshot.by_id.keys() & revised_ids
    for key in targets:
        # 실행 중 보류의 resume_status는 다음 catalog 검증에서 복원한다.
        connection.execute("DELETE FROM execplan_catalog_holds WHERE repo=? AND plan_id=? "
            "AND NOT EXISTS (SELECT 1 FROM exec_plans WHERE repo=? AND plan_id=? AND status='PLAN_BLOCKED')",
            (snapshot.repo, key, snapshot.repo, key))
