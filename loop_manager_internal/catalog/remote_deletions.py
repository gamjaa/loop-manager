"""Revision-proven cancellation and Planner-owned dependency goal recovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from loop_manager_internal.catalog import execplan_catalog as catalog
from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
from loop_manager_internal.maintenance.maintenance_orchestration import is_unowned_ready
from loop_manager_internal.planning.planning_catalog import PlannerValidationError
from loop_manager_internal.storage import execplan_catalog_state
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
    verified_merged_rows = execplan_catalog_state.verified_merged_archive_execution_ids(
        connection, repo, rows,
    )
    missing = {str(row["plan_id"]): str(row["path"]) for row in rows
               if str(row["plan_id"]) not in pinned.snapshot.observed_ids
               and str(row["plan_id"]) not in verified_merged_rows}
    cancellations = connection.execute(
        "SELECT * FROM execplan_cancellations WHERE repo=? ORDER BY plan_id", (repo,),
    ).fetchall() if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_cancellations'"
    ).fetchone() else ()
    for row in cancellations:
        key = str(row["plan_id"])
        if key not in pinned.snapshot.observed_ids:
            missing.setdefault(key, str(row["path"]))
    latest_contents = {key: entry.text.encode("utf-8") for key, entry in pinned.snapshot.all.items()}
    paths = {str(row["plan_id"]): str(row["path"]) for row in rows}
    paths.update({str(row["plan_id"]): str(row["path"]) for row in cancellations})
    withheld_paths = {key: path for key, path, _hash in pinned.snapshot.withheld}
    paths.update(withheld_paths)
    paths.update({key: entry.path for key, entry in pinned.snapshot.all.items()})
    previous_contents = {}
    for row in rows:
        revision = str(row["current_revision_sha"] or row["plan_sha"] or "")
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
            try:
                contents = git_bytes(repository, "show", f"{revision}:{row['path']}")
            except (RuntimeError, OSError):
                continue
            if catalog.plan_content_hash(contents) == row["content_hash"]:
                previous_contents[str(row["plan_id"])] = contents
    for row in cancellations:
        key, path = str(row["plan_id"]), str(row["path"])
        if key not in missing or key in previous_contents:
            continue
        revision = str(row["plan_sha"] or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
            continue
        try:
            contents = git_bytes(repository, "show", f"{revision}:{path}")
            text = contents.decode("utf-8")
            plan = catalog.parse_execplan_text(path, text, validate_sources=False)
        except (RuntimeError, OSError, UnicodeError):
            continue
        if (catalog.plan_content_hash(contents) == row["content_hash"]
                and plan["id"] == key and plan["title"] == row["title"]
                and plan["sources"] == json.loads(row["sources"])
                and plan["depends_on"] == json.loads(row["depends_on"])
                and plan["priority"] == row["priority"]):
            previous_contents[key] = contents
    prior = [(str(row["plan_id"]), json.loads(row["evidence"])) for row in connection.execute(
        "SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo=?", (repo,))]
    prior_cancellations = {}
    for row in cancellations:
        if not row["recovery_evidence"]:
            continue
        try:
            prior_cancellations[str(row["plan_id"])] = json.loads(row["recovery_evidence"])
        except (TypeError, ValueError) as error:
            raise PlannerValidationError("삭제 목표 감사 JSON이 유효하지 않습니다.") from error
    issues = []
    for key, path in missing.items():
        reference = re.compile(rb"(?<![A-Za-z0-9-])" + key.encode("ascii") + rb"(?![A-Za-z0-9-])")
        reference_path = path.encode("utf-8")
        def refers(contents):
            declaration = b"\n".join(
                line for line in contents.splitlines()
                if not line.lstrip().startswith(b"DEPENDENCY_RECOVERY:")
            )
            return reference_path in declaration or reference.search(declaration)

        current_references = {str(row["plan_id"]) for row in rows
                              if key in json.loads(row["depends_on"])}
        affected = set(current_references)
        affected.update(other for other, contents in previous_contents.items()
                        if refers(contents))
        for other in pinned.snapshot.observed_ids:
            latest = latest_contents.get(other)
            latest_path = withheld_paths.get(other)
            if (latest is None and latest_path is not None and pinned.regular_paths is not None
                    and latest_path in pinned.regular_paths):
                latest = git_bytes(repository, "show", f"{pinned.revision_sha}:{latest_path}")
                latest_contents[other] = latest
            if latest is not None and refers(latest):
                affected.add(other)
                current_references.add(other)
        # Keep unadopted references until their missing prerequisite returns;
        # removing a reference cannot establish that its goal is unnecessary.
        for other, evidence in prior:
            for cause in evidence.get("causes", ()):
                if (cause["plan_id"] == other and cause["code"] == "missing-plan-reference"
                        and cause.get("source") == path):
                    affected.add(other)
                    paths.setdefault(other, evidence["path"])
        saved = prior_cancellations.get(key, {})
        affected.update((set(saved.get("affected", ())) - set(saved.get("resolved", {}))) &
                        (set(pinned.snapshot.observed_ids) | set(str(row["plan_id"]) for row in rows)))
        approvals = dict(prior).get(key, {}).get("resolved_references", {})
        for other, approval in approvals.items():
            contents = latest_contents.get(other)
            dependencies = next((json.loads(row["depends_on"]) for row in rows
                                 if row["plan_id"] == other), ())
            if (contents is not None and key not in dependencies
                    and approval.get("path") == paths.get(other)
                    and approval.get("content_hash") == catalog.plan_content_hash(contents)):
                affected.discard(other)
        # A published recovery judgment resolves the old declaration evidence.
        # Keep it held if the current declaration still points back to the deleted plan.
        for other in set(saved.get("resolved", {})) - current_references:
            if latest_contents.get(other) is not None:
                affected.discard(other)
        issues.extend(catalog.CatalogIssue(other, paths[other], "missing-plan-reference",
            f"삭제된 계획의 참조 근거를 보존합니다: {other} -> {key}", path)
            for other in sorted(affected - {key}))
    return tuple(issues)


def cancellation_recovery_evidence(connection, repo, repository, pinned, references, *, git, git_bytes):
    """Pin newly observed references to rowless cancellation audit without recreating a plan row."""
    result = {}
    affected_by_path = {}
    for issue in references:
        if issue.code == "missing-plan-reference" and issue.source:
            affected_by_path.setdefault(issue.source, set()).add(issue.plan_id)
    if not affected_by_path:
        return result
    for row in connection.execute(
        "SELECT * FROM execplan_cancellations WHERE repo=? ORDER BY plan_id", (repo,),
    ):
        key, path = str(row["plan_id"]), str(row["path"])
        affected = affected_by_path.get(path, set())
        if not affected or key in pinned.snapshot.observed_ids:
            continue
        prior_text = row["recovery_evidence"]
        if prior_text:
            try:
                evidence = json.loads(prior_text)
                if (not isinstance(evidence, dict) or not isinstance(evidence.get("text"), str)
                        or not isinstance(evidence.get("row"), dict)
                        or not isinstance(evidence.get("affected"), list)
                        or not isinstance(evidence.get("resolved"), dict)):
                    raise ValueError("invalid cancellation evidence")
            except (ValueError, TypeError) as error:
                raise PlannerValidationError("삭제 목표 감사 JSON이 유효하지 않습니다.") from error
        else:
            evidence = {"text": "", "row": dict(row), "affected": [], "resolved": {}}
            evidence["identity"] = {
                "id": key, "path": path, "content_hash": str(row["content_hash"]),
                "plan_sha": str(row["plan_sha"]), "deletion_revision": str(row["deletion_commit"]),
            }
            evidence["observed_revision"] = pinned.revision_sha
            evidence["unknown_reason"] = "삭제 원문 revision과 identity를 검증하지 못했습니다."
            plan_sha = str(row["plan_sha"] or "")
            deletion_revision = str(row["deletion_commit"] or "")
            valid_sha = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
            if valid_sha.fullmatch(plan_sha) and valid_sha.fullmatch(deletion_revision):
                try:
                    deletion_ancestor = git(repository, "merge-base", "--is-ancestor",
                                            deletion_revision, pinned.revision_sha, check=False)
                    plan_ancestor = git(repository, "merge-base", "--is-ancestor",
                                        plan_sha, deletion_revision, check=False)
                except (RuntimeError, OSError):
                    deletion_ancestor = plan_ancestor = None
                if (deletion_ancestor is not None and plan_ancestor is not None
                        and deletion_ancestor.returncode == 0 and plan_ancestor.returncode == 0):
                    try:
                        contents = git_bytes(repository, "show", f"{plan_sha}:{path}")
                        text = contents.decode("utf-8")
                        parsed = catalog.parse_execplan_text(path, text, validate_sources=False)
                    except (RuntimeError, OSError, UnicodeError):
                        pass
                    else:
                        if (catalog.plan_content_hash(contents) == row["content_hash"]
                                and parsed["id"] == key and parsed["title"] == row["title"]
                                and parsed["sources"] == json.loads(row["sources"])
                                and parsed["depends_on"] == json.loads(row["depends_on"])
                                and parsed["complexity"] == row["complexity"]
                                and parsed["risk"] == row["risk"]
                                and parsed["priority"] == row["priority"]):
                            evidence["text"] = text
                            evidence["unknown_reason"] = None
            elif not plan_sha or not deletion_revision:
                evidence["unknown_reason"] = "취소 감사에 source revision 근거가 없습니다."
        old_affected = set(evidence["affected"])
        resolution_history = evidence.setdefault("resolution_history", {})
        if not isinstance(resolution_history, dict):
            raise PlannerValidationError("삭제 목표 감사 JSON이 유효하지 않습니다.")
        reopened = set(affected) & set(evidence["resolved"])
        for plan_id in reopened:
            previous = evidence["resolved"].pop(plan_id)
            history = resolution_history.setdefault(plan_id, [])
            if not isinstance(history, list) or not all(isinstance(item, str) for item in history):
                raise PlannerValidationError("삭제 목표 감사 JSON이 유효하지 않습니다.")
            if previous not in history:
                history.append(previous)
        evidence["affected"] = sorted(old_affected | affected)
        if evidence["affected"] != sorted(old_affected) or reopened or not prior_text:
            result[key] = (prior_text, json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return result


def prove_deletions(connection, repo, repository, pinned, *, git, git_bytes, references=None, pr_for_branch=None):
    """Absence alone cannot cancel a row: prove its last adopted bytes/ancestry."""
    from loop_manager_internal.storage.execplan_catalog_state import DefinitionStateRepository
    state = DefinitionStateRepository(connection).capture(repo)
    reserved = {item[0] for item in state.reserved}
    if references is None:
        references = reference_issues(connection, repo, repository, pinned, git_bytes=git_bytes)
    deletions = []
    rows = connection.execute("SELECT * FROM exec_plans WHERE repo=?", (repo,)).fetchall()
    manual = {str(row[0]) for row in connection.execute("SELECT plan_id FROM manual_work_members WHERE repo=?", (repo,))}
    for row in rows:
        key = str(row["plan_id"])
        completed = any(row[field] is not None for field in ("worker_result", "verify_result", "review_result", "agent_result_snapshot"))
        started = bool(row["ever_started"] and completed and key not in manual and revisable_row(row) and row["agent_role"] is None)
        if started and row["branch"]:
            if pr_for_branch is None:
                continue
            try:
                if pr_for_branch(repository, str(row["branch"])) is not None:
                    continue
            except RuntimeError:
                continue  # 실제 PR 부재를 확인하지 못한 작업만 보류한다.
        if (key in pinned.snapshot.observed_ids or key in reserved and not started
                or not started and mutable_row(connection, repo, key) is None):
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
            execution_state=json.dumps({key: row[key] for key in row.keys() if key != "path"}, ensure_ascii=False, sort_keys=True) if started else None,
        )
        affected = {issue.plan_id for issue in references if issue.source == path}
        evidence = json.dumps({"text": text, "row": dict(row), "affected": sorted(affected),
                               "resolved": {}, "cleanup_pending": bool(row["branch"] or row["worktree"]),
                               "cleanup_reason": "취소한 소유 자원의 작업물·PR·writer 안전 재확인 필요"},
                              ensure_ascii=False, sort_keys=True)
        deletions.append(RemoteDeletion(original, evidence))
    return tuple(deletions)


def requirement_deletions(connection, repo, repository, pinned, *, git, git_bytes):
    """이전 source 원문과 현재 실제 부재를 원격 revision에 결합한다."""
    result = {}
    rows = list(connection.execute("SELECT * FROM exec_plans WHERE repo=?", (repo,)).fetchall())
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execplan_cancellations'"
    ).fetchone():
        rows.extend(connection.execute(
            "SELECT * FROM execplan_cancellations WHERE repo=?", (repo,),
        ).fetchall())
    if not any(source.startswith("docs/requirements/") for row in rows for source in json.loads(row["sources"])):
        return result
    from loop_manager_internal.catalog import requirement_sources
    from loop_manager_internal.git.command_adapter import revision_requirement_documents
    documents = revision_requirement_documents(git_bytes, repository, pinned.revision_sha)
    for row in rows:
        current_revision = row["current_revision_sha"] if "current_revision_sha" in row.keys() else None
        revision = str(current_revision or row["plan_sha"] or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
            continue
        for source in json.loads(row["sources"]):
            if not source.startswith("docs/requirements/") or source.count("/") != 2:
                continue
            resolution = requirement_sources.resolve_requirement_source(source, documents, allow_archive=True)
            if resolution.status is not requirement_sources.ResolutionStatus.MISSING:
                continue
            ancestry = git(repository, "merge-base", "--is-ancestor", revision, pinned.revision_sha, check=False)
            if ancestry.returncode != 0:
                continue
            original = git(repository, "show", f"{revision}:{source}", check=False)
            if original.returncode:
                continue
            proof = result.setdefault(source, {"text": original.stdout, "revision": revision,
                "deletion_revision": pinned.revision_sha, "row": dict(row), "affected": []})
            proof["affected"].append(str(row["plan_id"]))
    return result


def recovery_definitions(connection, repo, repository, pinned, *, git_bytes):
    """Adopt affected READY and new declarations under a hold for Planner repair."""
    affected = {key for raw in recovery_goals(connection, repo) for key in json.loads(raw)["affected_ids"]}
    affected.update(key for item in pinned.deletions for key in json.loads(item.evidence)["affected"])
    affected.update(key for proof in (pinned.requirement_deletions or {}).values() for key in proof["affected"])
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
    sources = {}
    for held in holds.values():
        sources.update(held.get("deleted_sources", {}))
    for source, proof in sorted(sources.items()):
        affected = sorted((set(proof["affected"]) - set(proof.get("resolved", {}))) & observed_ids & held_ids)
        if affected:
            evidence = json.dumps({"text": proof["text"], "row": proof["row"],
                "affected": affected, "resolved": {}}, ensure_ascii=False, sort_keys=True)
            goals.append(json.dumps({"id": "requirement:" + source, "path": source, "kind": "missing-source",
                "deletion_revision": proof["deletion_revision"], "evidence": evidence,
                "affected_ids": affected}, ensure_ascii=False, sort_keys=True))
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


def requirement_judgments(connection, repo):
    if connection is None or repo is None:
        return {}
    row = connection.execute("SELECT planning_result FROM projects WHERE repo=?", (repo,)).fetchone()
    lines = [line.partition(":")[2] for line in str(row[0] or "").splitlines()
             if line.startswith("REQUIREMENT_RECOVERY:")] if row else []
    if not lines:
        return {}
    try:
        if len(lines) != 1:
            raise ValueError("중복 결과")
        values = json.loads(lines[0])
        if not isinstance(values, list):
            raise ValueError("결과 배열 필요")
        result = {}
        for item in values:
            if (not isinstance(item, dict) or set(item) != {"id", "source", "outcome", "reason"}
                    or item["outcome"] not in {"withdrawn", "retained", "uncertain"}
                    or not isinstance(item["reason"], str) or not item["reason"].strip()
                    or not isinstance(item["id"], str) or not isinstance(item["source"], str)
                    or (item["id"], item["source"]) in result):
                raise ValueError("잘못된 철회 판단")
            result[item["id"], item["source"]] = item
        return result
    except (ValueError, TypeError, KeyError) as error:
        raise PlannerValidationError("Requirement 삭제 판단 결과가 유효하지 않습니다.") from error


def prepare_withdrawals(connection, repo, worktree, snapshot):
    """시작한 작업의 직접 삭제 대신 안전한 Manager 문서 전환을 준비한다."""
    decisions = requirement_judgments(connection, repo)
    prepared = {}
    for raw in snapshot.recovery_goals:
        goal = json.loads(raw)
        if goal.get("kind") != "missing-source":
            continue
        for key in set(goal["affected_ids"]) & snapshot.by_id.keys():
            decision = decisions.get((key, goal["path"]))
            if not decision or decision["outcome"] != "withdrawn":
                continue
            original = snapshot.by_id[key]
            if original.execution_state:
                from loop_manager_internal.planning.planner_validation import planning_execution_state
                row = connection.execute("SELECT * FROM exec_plans WHERE repo=? AND plan_id=?", (repo, key)).fetchone()
                if row is None or planning_execution_state(row) != original.execution_state or row["agent_role"]:
                    raise PlannerValidationError("작업 철회 전에 실행 소유권·writer가 변경됐습니다.")
                if connection.execute("SELECT 1 FROM manual_work_members WHERE repo=? AND plan_id=?", (repo, key)).fetchone():
                    raise PlannerValidationError("수동 작업 소유권이 있는 계획을 철회할 수 없습니다.")
            prepared[key] = {"decision": decision, "goal": goal}
            if original.execution_state and json.loads(original.execution_state).get("ever_started"):
                path = worktree / original.path
                held = connection.execute("SELECT evidence FROM execplan_catalog_holds WHERE repo=? AND plan_id=?", (repo, key)).fetchone()
                evidence = json.loads(held[0]) if held else {}
                receipt = evidence.get("prepared_withdrawal")
                if receipt and receipt.get("decision") == decision and receipt.get("goal") == goal and not path.exists():
                    prepared[key] = receipt
                    continue
                if not path.is_file() or path.is_symlink() or not held:
                    raise PlannerValidationError("시작한 작업은 Planner가 직접 삭제할 수 없습니다.")
                prepared[key]["preserved_text"] = path.read_text(encoding="utf-8")
                evidence["prepared_withdrawal"] = prepared[key]
                from loop_manager_internal.storage.runtime_state import _transaction
                with _transaction(connection):
                    changed = connection.execute("UPDATE execplan_catalog_holds SET evidence=? WHERE repo=? AND plan_id=? AND evidence=?",
                        (json.dumps(evidence, ensure_ascii=False, sort_keys=True), repo, key, held[0]))
                    if changed.rowcount != 1:
                        raise PlannerValidationError("작업 철회 보존 근거가 변경됐습니다.")
                path.unlink()
    return prepared


def validate_recovery(worktree, snapshot, current_plans, catalog_metadata, *, connection=None, repo=None):
    """Require a Planner judgment and retained goal gates, never infer meaning."""
    judgments = requirement_judgments(connection, repo)
    for raw in snapshot.recovery_goals:
        goal = json.loads(raw)
        for key in set(goal["affected_ids"]) & snapshot.by_id.keys():
            changed = [(path, plan) for path, plan in current_plans.items() if plan["id"] == key]
            if (not changed and snapshot.by_id[key].path not in current_plans
                    and (worktree / snapshot.by_id[key].path).exists()):
                continue  # Uncertain goals remain held while independent work is published.
            if goal.get("kind") == "missing-source":
                decision = judgments.get((key, goal["path"]))
                if not decision or decision["outcome"] == "uncertain":
                    raise PlannerValidationError("Requirement 삭제의 목표 철회·유지 판단이 필요합니다.")
                if decision["outcome"] == "withdrawn":
                    if changed or (worktree / snapshot.by_id[key].path).exists():
                        raise PlannerValidationError("전체 철회된 작업의 활성 문서를 보존할 수 없습니다.")
                elif len(changed) != 1 or goal["path"] in changed[0][1]["sources"]:
                    raise PlannerValidationError("유지하는 작업에 유효한 현행 source·남은 목표가 필요합니다.")
                continue
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
        if goal.get("kind") == "missing-source":
            for row in connection.execute("SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo=?", (snapshot.repo,)).fetchall():
                held = json.loads(row["evidence"])
                proof = held.get("deleted_sources", {}).get(goal["path"])
                if proof is not None:
                    proof.setdefault("resolved", {}).update({key: commit for key in targets})
                    connection.execute("UPDATE execplan_catalog_holds SET evidence=? WHERE repo=? AND plan_id=?",
                        (json.dumps(held, ensure_ascii=False, sort_keys=True), snapshot.repo, row["plan_id"]))
            continue  # 시작한 작업의 resume_status는 다음 catalog 재검증까지 보존한다.
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
