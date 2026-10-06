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


def prove_deletions(connection, repo, repository, pinned, *, git, git_bytes):
    """Absence alone cannot cancel a row: prove its last adopted bytes/ancestry."""
    from loop_manager_internal.storage.execplan_catalog_state import DefinitionStateRepository
    state = DefinitionStateRepository(connection).capture(repo)
    reserved = {item[0] for item in state.reserved}
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
        affected = []
        for other in rows:
            other_id = str(other["plan_id"])
            if other_id == key:
                continue
            if key in json.loads(other["depends_on"]):
                affected.append(other_id)
                continue
            adopted = str(other["current_revision_sha"] or other["plan_sha"] or "")
            if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", adopted):
                previous = git_bytes(repository, "show", f"{adopted}:{other['path']}")
                if (catalog.plan_content_hash(previous) == other["content_hash"]
                        and (path.encode("utf-8") in previous or re.search(
                            rb"(?<![A-Za-z0-9-])" + key.encode("ascii") + rb"(?![A-Za-z0-9-])", previous))):
                    affected.append(other_id)
        evidence = json.dumps({"text": text, "row": dict(row), "affected": sorted(affected),
                               "resolved": {}}, ensure_ascii=False, sort_keys=True)
        deletions.append(RemoteDeletion(original, evidence))
    return tuple(deletions)


def recovery_definitions(connection, repo, repository, pinned, *, git_bytes):
    """Adopt valid READY declarations under a hold, even if the user edited them."""
    affected = {key for raw in recovery_goals(connection, repo) for key in json.loads(raw)["affected_ids"]}
    affected.update(key for item in pinned.deletions for key in json.loads(item.evidence)["affected"])
    paths = {key: path for key, path, _hash in pinned.snapshot.withheld}
    paths.update({key: entry.path for key, entry in pinned.snapshot.all.items()})
    entries = []
    for key in sorted(affected):
        if (key not in paths or mutable_row(connection, repo, key) is None
                or any(issue.plan_id == key and issue.code != "missing-dependency" for issue in pinned.snapshot.issues)):
            continue
        path = paths[key]
        if pinned.regular_paths is None or path not in pinned.regular_paths:
            continue
        text = git_bytes(repository, "show", f"{pinned.revision_sha}:{path}").decode("utf-8")
        plan = catalog.parse_execplan_text(path, text, validate_sources=False)
        entries.append(catalog._entry(path, text, plan))
    return tuple(entries)


def recovery_goals(connection, repo):
    """Keep cancellation evidence immutable; resolution is publication-bound."""
    goals = []
    rows = connection.execute("SELECT * FROM exec_plans WHERE repo=?", (repo,)).fetchall()
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
        affected = sorted(str(row["plan_id"]) for row in rows
            if (str(row["plan_id"]) in evidence["affected"] or key in json.loads(row["depends_on"]))
            and (str(row["plan_id"]) not in evidence["resolved"] or key in json.loads(row["depends_on"])))
        if affected:
            goals.append(json.dumps({"id": key, "path": cancelled["path"],
                "deletion_revision": cancelled["deletion_commit"], "evidence": cancelled["recovery_evidence"],
                "affected_ids": affected}, ensure_ascii=False, sort_keys=True))
    return tuple(goals)


def recoverable_ids(connection, repo, held):
    """Only deletion-goal holds may enter the ordinary mutable READY snapshot."""
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
        if relevant and all(item["code"] in {"missing-dependency", "deleted-goal"} for item in relevant):
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
        connection.execute("DELETE FROM execplan_catalog_holds WHERE repo=? AND plan_id=?", (snapshot.repo, key))
